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

use crate::commit::CommitId;
use crate::db::store::{Filter, Scope, Store};
use crate::db::tables::{At, Place, RecordRow};
use crate::error::{Error, Result};
use crate::format::FormatRegistry;
use crate::model::{Applied, BatchOp, BatchOutcome, Failed, Record, TxnMeta, WriteOutcome};
use crate::segments::{Draft, Origin, Value};
use crate::sync::CommitRef;

/// What the write path needs of the worktree it writes in.
#[derive(Clone, Copy)]
pub(crate) struct Writer<'a> {
    pub(crate) worktree_id: i64,
    pub(crate) family_id: i64,
    pub(crate) formats: &'a FormatRegistry,
}

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
    existing_record_commit: Option<&CommitId>,
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
                    actual: existing_record_commit.map(CommitId::to_string),
                })
            }
        }
        CommitRef::Commit(expected_oid) => {
            // Oid token requires an existing record at the given key,
            // and its commit_id must match.
            match existing_record_commit {
                // the client's token is its text, compared as such
                Some(actual) if actual.to_string() == *expected_oid => Ok(()),
                actual => Err(Error::Conflict {
                    file_path: file_path.to_string(),
                    path: path.to_string(),
                    expected: expected.clone(),
                    actual: actual.map(CommitId::to_string),
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
    /// Check the value with [`crate::DataFormat::validate_record`].
    validate: bool,
}

/// The live one of `rows`, at `(path, key)` in `file_path` or, with none
/// named, in any file: [`Error::Ambiguous`] where that's more than one.
fn only_live(
    rows: Vec<RecordRow>,
    file_path: Option<&str>,
    path: &str,
    key: &str,
) -> Result<Option<RecordRow>> {
    let mut live: Vec<RecordRow> = rows.into_iter().filter(|r| !r.deleted).collect();
    if file_path.is_none() && live.len() > 1 {
        return Err(Error::Ambiguous {
            path: path.to_string(),
            key: key.to_string(),
            files: live.into_iter().map(|r| r.file_path).collect(),
        });
    }
    Ok(live.pop())
}

/// What a write is checked against before anything is written.
struct Checked {
    /// Where it writes: the caller's file, else the live record's, else
    /// (creating) the worktree's default.
    place: Place,
    /// The live record the view shows at the key.
    live: Option<RecordRow>,
    /// The record it replaces: the draft's own row at the key, tombstone or
    /// not, else the live one; `None` for a new record.
    record: Option<i64>,
}

/// One write in the caller's transaction, at an already drawn `version`.
async fn write_in_tx<DB: Store>(
    writer: Writer<'_>,
    tx: &mut sqlx::Transaction<'_, DB>,
    draft: &mut Draft,
    request: Request<'_>,
    version: i64,
) -> Result<WriteOutcome> {
    let checked = check(tx, draft, &request).await?;
    apply(writer, tx, draft, request, checked, version).await
}

/// `request` against the view: the record at its key, its file, and the
/// checks of its kind and token.
async fn check<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    draft: &Draft,
    request: &Request<'_>,
) -> Result<Checked> {
    let worktree_id = draft.worktree_id;
    let WriteTarget {
        file_path,
        path,
        key,
    } = request.at;
    // a tombstone reads as absent
    let filter = match file_path {
        Some(file_path) => Filter::At(At {
            file_path,
            path,
            key,
        }),
        None => Filter::PathKey(path, key),
    };
    let rows = DB::visible(tx, worktree_id, Scope::Own, filter).await?;
    let live = only_live(rows, file_path, path, key)?;
    let file_path = match file_path.or(live.as_ref().map(|r| r.file_path.as_str())) {
        Some(f) => f.to_string(),
        None if matches!(request.kind, Kind::Create | Kind::Upsert) => {
            DB::default_file_path(tx, worktree_id)
                .await?
                .ok_or_else(|| Error::NotFound {
                    file_path: String::new(),
                    path: path.to_string(),
                })?
        }
        None => String::new(),
    };
    match (request.kind, &live) {
        (Kind::Create, Some(_)) => {
            return Err(Error::AlreadyExists {
                file_path,
                path: path.to_string(),
            })
        }
        (Kind::Update | Kind::Delete, None) => {
            return Err(Error::NotFound {
                file_path,
                path: path.to_string(),
            })
        }
        _ => {}
    }
    if let Some(expected) = request.expected {
        enforce_conflict(
            &file_path,
            path,
            expected,
            live.as_ref().and_then(|r| r.commit_id.as_ref()),
            live.as_ref().map(|r| r.version),
            live.is_some(),
        )?;
    }
    let place = Place::new(&file_path, path, key);
    let record = draft
        .row_at(place.as_at())
        .map(|r| r.key_id)
        .or(live.as_ref().map(|r| r.key_id));
    Ok(Checked {
        place,
        live,
        record,
    })
}

/// The write `request`, `checked` already: its row, its aliases, and with
/// `resolve`, its key's conflict row dropped.
async fn apply<DB: Store>(
    writer: Writer<'_>,
    tx: &mut sqlx::Transaction<'_, DB>,
    draft: &mut Draft,
    request: Request<'_>,
    checked: Checked,
    version: i64,
) -> Result<WriteOutcome> {
    let (w, target) = (writer.worktree_id, checked.place.as_at());
    let format_owner = match request.kind {
        Kind::Upsert => ensure_file_registered(writer, tx, target.file_path, target.path).await?,
        _ => DB::file_format(tx, w, target.file_path).await?,
    };
    let (edit, record) = (Origin::Edit, checked.record);
    let written = match request.json {
        Some(mut json) => {
            let previous = checked.live.as_ref().map(|r| &r.json);
            prepare_record(
                writer.formats,
                target,
                previous,
                &mut json,
                request.validate,
            )?;
            let value = Value::Live(&json);
            let written = draft
                .write(tx, target, value, edit, record, version)
                .await?;
            let owner = format_owner.as_deref();
            let aliases = compute_aliases(writer, owner, written.key_id, target, &json);
            DB::replace_aliases(tx, written.id, &aliases).await?;
            written
        }
        None => {
            draft
                .write(tx, target, Value::Deleted, edit, record, version)
                .await?
        }
    };
    if request.resolve {
        draft.drop_conflict(tx, target).await?;
    }
    Ok(WriteOutcome {
        id: written.key_id,
        version,
    })
}

/// [`crate::DataFormat::prepare_write`] on `json`, then, if `validate`,
/// [`crate::DataFormat::validate_record`].
fn prepare_record(
    formats: &FormatRegistry,
    target: At<'_>,
    previous: Option<&serde_json::Value>,
    json: &mut serde_json::Value,
    validate: bool,
) -> Result<()> {
    let Some(format) = formats.for_path(target.path) else {
        return Ok(());
    };
    format.prepare_write(target.path, previous, json);
    match validate.then(|| format.validate_record(target.path, json)) {
        Some(Some(err)) => Err(Error::Invalid {
            file_path: target.file_path.to_string(),
            path: target.path.to_string(),
            key: target.key.to_string(),
            message: err.to_string(),
        }),
        _ => Ok(()),
    }
}

/// One write in its own transaction.
async fn write_one<DB: Store>(
    writer: Writer<'_>,
    pool: &sqlx::Pool<DB>,
    request: Request<'_>,
) -> Result<WriteOutcome> {
    let mut tx = pool.begin().await?;
    let version = DB::next_version(&mut tx, writer.family_id, 1).await?;
    let mut draft = Draft::load(&mut tx, writer.worktree_id).await?;
    let out = write_in_tx(writer, &mut tx, &mut draft, request, version).await?;
    #[cfg(debug_assertions)]
    draft.assert_current(&mut tx).await?;
    tx.commit().await?;
    Ok(out)
}

pub(crate) async fn crud_create_in_pool<DB: Store>(
    writer: Writer<'_>,
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
        validate: false,
    };
    write_one(writer, pool, request).await
}

pub(crate) async fn crud_update_in_pool<DB: Store>(
    writer: Writer<'_>,
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
        validate: false,
    };
    write_one(writer, pool, request).await
}

pub(crate) async fn crud_upsert_in_pool<DB: Store>(
    writer: Writer<'_>,
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
        validate: false,
    };
    write_one(writer, pool, request).await
}

pub(crate) async fn crud_delete_in_pool<DB: Store>(
    writer: Writer<'_>,
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
        validate: false,
    };
    write_one(writer, pool, request).await
}

/// Generic batch driver: opens one transaction on `pool`, walks
/// `ops`, accumulates [`Applied`] / [`Failed`] entries, and either
/// commits (success / non-atomic with failures) or rolls back (atomic
/// + first failure).
pub(crate) async fn apply_batch_inner<DB: Store>(
    writer: Writer<'_>,
    pool: &sqlx::Pool<DB>,
    ops: Vec<BatchOp>,
    atomic: bool,
    validate: bool,
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
        DB::next_version(&mut tx, writer.family_id, ops.len() as i64).await?
    };
    // after the draw, whose lock keeps other writers out of it
    let mut draft = Draft::load(&mut tx, writer.worktree_id).await?;
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
                    writer,
                    &mut tx,
                    &mut draft,
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
                        validate,
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
                    writer,
                    &mut tx,
                    &mut draft,
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
                        validate,
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
            Err(
                err @ (Error::Conflict { .. } | Error::NotFound { .. } | Error::Ambiguous { .. }),
            ) => {
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
    #[cfg(debug_assertions)]
    draft.assert_current(&mut tx).await?;
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
            writer.worktree_id,
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
    writer: Writer<'_>,
    tx: &mut sqlx::Transaction<'_, DB>,
    file_path: &str,
    record_path: &str,
) -> Result<Option<String>> {
    if let Some(existing) = DB::file_format(tx, writer.worktree_id, file_path).await? {
        return Ok(Some(existing));
    }
    // No format claims this section, so there's nothing to record the file as
    // — `file.format` is NOT NULL. Callers writing a section no registered
    // format knows about get a clear error.
    let format = writer
        .formats
        .for_path(record_path)
        .ok_or_else(|| Error::UnknownFormat(record_path.to_string()))?
        .name()
        .to_string();
    DB::ensure_file(tx, writer.worktree_id, file_path, &format).await?;
    Ok(Some(format))
}

pub(crate) fn compute_aliases(
    writer: Writer<'_>,
    format_owner: Option<&str>,
    record_id: i64,
    at: At<'_>,
    json: &serde_json::Value,
) -> Vec<(String, String)> {
    let Some(name) = format_owner else {
        return Vec::new();
    };
    let Some(fmt) = writer.formats.by_name(name) else {
        return Vec::new();
    };
    let record = Record {
        id: record_id,
        worktree_id: writer.worktree_id,
        file_path: at.file_path.to_string(),
        path: at.path.to_string(),
        key: at.key.to_string(),
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
    writer: Writer<'_>,
    pool: &sqlx::Pool<DB>,
    file_path: &str,
    expected_commit: Option<CommitRef>,
) -> Result<Vec<WriteOutcome>> {
    let w = writer.worktree_id;
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
        DB::next_version(&mut tx, writer.family_id, live.len() as i64).await?
    };
    let mut draft = Draft::load(&mut tx, w).await?;
    let mut out = Vec::with_capacity(live.len());
    for (index, row) in live.iter().enumerate() {
        let version = base + index as i64;
        let (at, record) = (row.at(), Some(row.key_id));
        let written = draft
            .write(&mut tx, at, Value::Deleted, Origin::Edit, record, version)
            .await?;
        out.push(WriteOutcome {
            id: written.key_id,
            version,
        });
    }
    #[cfg(debug_assertions)]
    draft.assert_current(&mut tx).await?;
    DB::set_file_deleted(&mut tx, w, file_path, true).await?;
    tx.commit().await?;
    Ok(out)
}
