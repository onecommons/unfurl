// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! The record CRUD primitives behind [`crate::SyncedRepo`]'s public
//! writes, and the batch driver over them.
//!
//! A write goes into the worktree's draft as a new version (§4.2, C.9).
//! Each draws its version first, which takes the family's counter lock,
//! so nothing can move the rows it looks up before it writes: the
//! optimistic-concurrency check on that lookup is the whole guard. On any
//! error -- an [`crate::Error::Conflict`] included -- the transaction is
//! dropped without commit, so it rolls back.

use crate::db::store::{Filter, Scope, Store};
use crate::db::tables::{At, RecordRow};
use crate::error::{Error, Result};
use crate::model::{Applied, BatchOp, BatchOutcome, Failed, Record, TxnMeta, WriteOutcome};
use crate::segments::{self, Origin, Value};
use crate::sync::{CommitRef, SyncedRepo};

/// Where a CRUD write lands, before the file path is resolved.
///
/// Grouped for the reason [`crate::db::RecordId`] is -- `path` and `key`
/// are adjacent `&str`s a positional call site could transpose -- but
/// `file_path` is optional here because the caller may leave it to the
/// existing record's file or the worktree default, and the resolution is
/// what these functions do first.
pub(crate) struct WriteTarget<'a> {
    pub(crate) file_path: Option<&'a str>,
    pub(crate) path: &'a str,
    pub(crate) key: &'a str,
}

pub(crate) fn enforce_conflict(
    file_path: &str,
    path: &str,
    expected: &CommitRef,
    existing_record_commit: Option<&String>,
    existing_record_version: Option<i64>,
    record_present: bool,
) -> Result<()> {
    match expected {
        CommitRef::Pending(expected_version) => {
            // The record must exist and its `version` must be <=
            // `expected_version`. Versions are monotonic per record
            // (writes always bump), so `record.version > expected`
            // means someone rewrote the row after the client's last
            // observation — that's the conflict signal. `<=` covers
            // both the unchanged case (`==`) and the case where the
            // client is holding a queueid from a *later* batch in
            // which this particular row wasn't touched.
            //
            // The `commit_id` doesn't matter — a `Pending(v)` token
            // remains valid after `commit_repository` folds the draft
            // (a fold keeps versions).
            if record_present && existing_record_version.is_some_and(|v| v <= *expected_version) {
                Ok(())
            } else {
                Err(Error::Conflict {
                    file_path: file_path.to_string(),
                    path: path.to_string(),
                    expected: expected.clone(),
                    actual: existing_record_commit.cloned(),
                })
            }
        }
        CommitRef::Commit(expected_oid) => {
            // Oid token requires an existing record at the given key,
            // and its commit_id must match.
            match existing_record_commit {
                Some(actual) if actual == expected_oid => Ok(()),
                actual => Err(Error::Conflict {
                    file_path: file_path.to_string(),
                    path: path.to_string(),
                    expected: expected.clone(),
                    actual: actual.cloned(),
                }),
            }
        }
    }
}

/// Which of the CRUD writes this is: what it requires of the record
/// already there.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Create,
    Update,
    Upsert,
    Delete,
}

/// One CRUD write, as the caller asked for it.
struct Request<'a> {
    kind: Kind,
    at: WriteTarget<'a>,
    /// The value to write; `None` for a delete.
    json: Option<serde_json::Value>,
    expected: Option<&'a CommitRef>,
    /// Drop the key's conflict row too.
    resolve: bool,
}

/// One write in the caller's transaction, at an already drawn `version`.
async fn write_in_tx<DB: Store>(
    sync: &SyncedRepo,
    tx: &mut sqlx::Transaction<'_, DB>,
    request: Request<'_>,
    version: i64,
) -> Result<WriteOutcome> {
    let Request {
        kind,
        at,
        json,
        expected,
        resolve,
    } = request;
    let w = sync.worktree_id();
    let WriteTarget {
        file_path,
        path,
        key,
    } = at;
    // The live record the view shows at the key, in the named file or
    // any; a tombstone reads as absent.
    let filter = match file_path {
        Some(file_path) => Filter::At(At {
            file_path,
            path,
            key,
        }),
        None => Filter::PathKey(path, key),
    };
    let live: Option<RecordRow> = DB::visible(tx, w, Scope::Own, filter)
        .await?
        .into_iter()
        .find(|r| !r.deleted);
    // Resolve the effective file_path: caller-supplied, then the existing
    // record's file, then (creating) the worktree default.
    let resolved_fp = match file_path.or(live.as_ref().map(|r| r.file_path.as_str())) {
        Some(f) => f.to_string(),
        None if matches!(kind, Kind::Create | Kind::Upsert) => DB::default_file_path(tx, w)
            .await?
            .ok_or_else(|| Error::NotFound {
                file_path: String::new(),
                path: path.to_string(),
            })?,
        None => String::new(),
    };
    let target = At {
        file_path: &resolved_fp,
        path,
        key,
    };
    match (kind, &live) {
        (Kind::Create, Some(_)) => {
            return Err(Error::AlreadyExists {
                file_path: resolved_fp.clone(),
                path: path.to_string(),
            })
        }
        (Kind::Update | Kind::Delete, None) => {
            return Err(Error::NotFound {
                file_path: resolved_fp.clone(),
                path: path.to_string(),
            })
        }
        _ => {}
    }
    if let Some(exp) = expected {
        enforce_conflict(
            &resolved_fp,
            path,
            exp,
            live.as_ref().and_then(|r| r.commit_id.as_ref()),
            live.as_ref().map(|r| r.version),
            live.is_some(),
        )?;
    }
    // The record it replaces: the draft's own row at the key, tombstone
    // or not, else the live one the view shows; otherwise a new one.
    let d = DB::segs(tx, w).await?.draft;
    let record = DB::rows_in(tx, d, Some(&resolved_fp), false)
        .await?
        .into_iter()
        .find(|r| r.at() == target)
        .map(|r| r.key_id)
        .or(live.as_ref().map(|r| r.key_id));
    let format_owner = match kind {
        Kind::Upsert => ensure_file_registered(sync, tx, &resolved_fp, path).await?,
        _ => DB::file_format(tx, w, &resolved_fp).await?,
    };
    let written = match json {
        Some(mut json) => {
            if let Some(format) = sync.formats().for_path(path) {
                format.prepare_write(path, live.as_ref().map(|r| &r.json), &mut json);
            }
            let written = segments::write(
                tx,
                w,
                target,
                Value::Live(&json),
                Origin::Edit,
                record,
                version,
            )
            .await?;
            let aliases = compute_aliases(
                sync,
                format_owner.as_deref(),
                written.key_id,
                &resolved_fp,
                path,
                key,
                &json,
            );
            DB::replace_aliases(tx, written.id, &aliases).await?;
            written
        }
        None => {
            segments::write(tx, w, target, Value::Deleted, Origin::Edit, record, version).await?
        }
    };
    if resolve {
        segments::drop_conflict(tx, d, target).await?;
    }
    Ok(WriteOutcome {
        id: written.key_id,
        version,
    })
}

/// One write in its own transaction.
async fn write_one<DB: Store>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    request: Request<'_>,
) -> Result<WriteOutcome> {
    let mut tx = pool.begin().await?;
    let version = DB::next_version(&mut tx, sync.family_id(), 1).await?;
    let out = write_in_tx(sync, &mut tx, request, version).await?;
    tx.commit().await?;
    Ok(out)
}

pub(crate) async fn crud_create_in_pool<DB: Store>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    at: WriteTarget<'_>,
    json: serde_json::Value,
    expected_commit: Option<CommitRef>,
    resolve: bool,
) -> Result<WriteOutcome> {
    let request = Request {
        kind: Kind::Create,
        at,
        json: Some(json),
        expected: expected_commit.as_ref(),
        resolve,
    };
    write_one(sync, pool, request).await
}

pub(crate) async fn crud_update_in_pool<DB: Store>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    at: WriteTarget<'_>,
    json: serde_json::Value,
    expected_commit: Option<CommitRef>,
    resolve: bool,
) -> Result<WriteOutcome> {
    let request = Request {
        kind: Kind::Update,
        at,
        json: Some(json),
        expected: expected_commit.as_ref(),
        resolve,
    };
    write_one(sync, pool, request).await
}

pub(crate) async fn crud_upsert_in_pool<DB: Store>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    at: WriteTarget<'_>,
    json: serde_json::Value,
    expected_commit: Option<CommitRef>,
    resolve: bool,
) -> Result<WriteOutcome> {
    let request = Request {
        kind: Kind::Upsert,
        at,
        json: Some(json),
        expected: expected_commit.as_ref(),
        resolve,
    };
    write_one(sync, pool, request).await
}

pub(crate) async fn crud_delete_in_pool<DB: Store>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    at: WriteTarget<'_>,
    expected_commit: Option<CommitRef>,
    resolve: bool,
) -> Result<WriteOutcome> {
    let request = Request {
        kind: Kind::Delete,
        at,
        json: None,
        expected: expected_commit.as_ref(),
        resolve,
    };
    write_one(sync, pool, request).await
}

/// Generic batch driver: opens one transaction on `pool`, walks
/// `ops`, accumulates [`Applied`] / [`Failed`] entries, and either
/// commits (success / non-atomic with failures) or rolls back (atomic
/// + first failure).
pub(crate) async fn apply_batch_inner<DB: Store>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    ops: Vec<BatchOp>,
    atomic: bool,
    meta: Option<TxnMeta>,
) -> Result<BatchOutcome> {
    let mut outcome = BatchOutcome::default();
    let mut tx = pool.begin().await?;
    // One draw for the batch rather than one per op. The cost is that an
    // op which fails still consumes its slot, leaving a gap -- reported by
    // `RollupTxn::unaccounted` rather than hidden.
    let base = if ops.is_empty() {
        0
    } else {
        DB::next_version(&mut tx, sync.family_id(), ops.len() as i64).await?
    };
    for (index, op) in ops.into_iter().enumerate() {
        let path = op.path().to_string();
        let key = op.key().to_string();
        let deleted = matches!(op, BatchOp::Delete { .. });
        let version = base + index as i64;
        let result = match &op {
            BatchOp::Upsert {
                file_path,
                path,
                key,
                json,
                expected,
                resolve,
            } => {
                write_in_tx(
                    sync,
                    &mut tx,
                    Request {
                        kind: Kind::Upsert,
                        at: WriteTarget {
                            file_path: file_path.as_deref(),
                            path,
                            key,
                        },
                        json: Some(json.clone()),
                        expected: expected.as_ref(),
                        resolve: *resolve,
                    },
                    version,
                )
                .await
            }
            BatchOp::Delete {
                file_path,
                path,
                key,
                expected,
                resolve,
            } => {
                write_in_tx(
                    sync,
                    &mut tx,
                    Request {
                        kind: Kind::Delete,
                        at: WriteTarget {
                            file_path: file_path.as_deref(),
                            path,
                            key,
                        },
                        json: None,
                        expected: expected.as_ref(),
                        resolve: *resolve,
                    },
                    version,
                )
                .await
            }
        };
        match result {
            Ok(write) => {
                let v = write.version;
                if outcome.last_version.is_none_or(|cur| v > cur) {
                    outcome.last_version = Some(v);
                }
                outcome.applied.push(Applied {
                    index,
                    path,
                    key,
                    outcome: write,
                    deleted,
                });
            }
            Err(err @ (Error::Conflict { .. } | Error::NotFound { .. })) => {
                outcome.failed.push(Failed {
                    index,
                    path,
                    key,
                    error: err,
                });
                if atomic {
                    drop(tx);
                    outcome.applied.clear();
                    outcome.last_version = None;
                    return Ok(outcome);
                }
            }
            Err(other) => return Err(other),
        }
    }
    // Audit row last, inside the same transaction: it lands only if the
    // writes it describes do. A batch that applied nothing has no
    // version range to record, so it gets no row.
    if let (Some(meta), Some(first), Some(last)) = (
        meta,
        outcome.applied.first().map(|a| a.outcome.version),
        outcome.last_version,
    ) {
        let created_at = chrono::Local::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, false);
        DB::insert_txn(
            &mut tx,
            sync.worktree_id(),
            (first, last),
            meta.author.as_deref(),
            meta.message.as_deref(),
            &created_at,
        )
        .await?;
    }
    tx.commit().await?;
    Ok(outcome)
}

/// Return the format owning `file_path`, registering the file first if the
/// worktree hasn't scanned it.
///
/// A write may name a `file_path` that doesn't exist yet — creating a second
/// cloudmap, say. The file itself is synthesised on the next
/// [`SyncedRepo::write_file`], which already handles a missing file on disk.
/// The format is taken from whichever registered [`crate::DataFormat`] claims
/// the record's section.
pub(crate) async fn ensure_file_registered<DB: Store>(
    sync: &SyncedRepo,
    tx: &mut sqlx::Transaction<'_, DB>,
    file_path: &str,
    record_path: &str,
) -> Result<Option<String>> {
    if let Some(existing) = DB::file_format(tx, sync.worktree_id(), file_path).await? {
        return Ok(Some(existing));
    }
    // No format claims this section, so there's nothing to record the file as
    // — `file.format` is NOT NULL. Callers writing a section no registered
    // format knows about get a clear error.
    let format = sync
        .formats()
        .for_path(record_path)
        .ok_or_else(|| Error::UnknownFormat(record_path.to_string()))?
        .name()
        .to_string();
    DB::ensure_file(tx, sync.worktree_id(), file_path, &format).await?;
    Ok(Some(format))
}

pub(crate) fn compute_aliases(
    sync: &SyncedRepo,
    format_owner: Option<&str>,
    record_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
    json: &serde_json::Value,
) -> Vec<(String, String)> {
    let Some(name) = format_owner else {
        return Vec::new();
    };
    let Some(fmt) = sync.formats().by_name(name) else {
        return Vec::new();
    };
    let record = Record {
        id: record_id,
        worktree_id: sync.worktree_id(),
        file_path: file_path.to_string(),
        path: path.to_string(),
        key: key.to_string(),
        commit_id: None,
        json: json.clone(),
        deleted: false,
        // version isn't read by `DataFormat::find_alias` impls, so a
        // placeholder is fine — this struct is only consulted for
        // alias derivation.
        version: 0,
        conflict: None,
    };
    fmt.find_alias(&record)
}

/// Body of [`SyncedRepo::delete_file`], generic over the pool.
///
/// The file row and every live record in it move together: a tombstoned
/// file whose records were left alone would render as a header-only
/// stub on the next save, which is the shape this whole path exists to
/// avoid.
pub(crate) async fn delete_file_in_pool<DB: Store>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    file_path: &str,
    expected_commit: Option<CommitRef>,
) -> Result<Vec<WriteOutcome>> {
    let w = sync.worktree_id();
    let mut tx = pool.begin().await?;
    let (commit_id, _) = DB::file_state(&mut tx, w, file_path)
        .await?
        .ok_or_else(|| Error::NotFound {
            file_path: file_path.to_string(),
            path: String::new(),
        })?;
    let live: Vec<RecordRow> = DB::visible(&mut tx, w, Scope::Own, Filter::File(file_path))
        .await?
        .into_iter()
        .filter(|r| !r.deleted)
        .collect();
    if let Some(expected) = expected_commit.as_ref() {
        // The file stands in for a record here: `record_present` is true
        // because the *file* is what the token is about, and the version
        // is the highest any record in it carries.
        let last_version = live.iter().map(|r| r.version).max().unwrap_or(0);
        enforce_conflict(
            file_path,
            "",
            expected,
            commit_id.as_ref(),
            Some(last_version),
            true,
        )?;
    }
    let base = if live.is_empty() {
        0
    } else {
        DB::next_version(&mut tx, sync.family_id(), live.len() as i64).await?
    };
    let mut out = Vec::with_capacity(live.len());
    for (index, row) in live.iter().enumerate() {
        let version = base + index as i64;
        let written = segments::write(
            &mut tx,
            w,
            row.at(),
            Value::Deleted,
            Origin::Edit,
            Some(row.key_id),
            version,
        )
        .await?;
        out.push(WriteOutcome {
            id: written.key_id,
            version,
        });
    }
    DB::set_file_deleted(&mut tx, w, file_path, true).await?;
    tx.commit().await?;
    Ok(out)
}
