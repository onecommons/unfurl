// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! The record CRUD primitives behind [`crate::SyncedRepo`]'s public
//! writes, and the batch driver over them.
//!
//! Each primitive performs its conflict check, its mutation and its
//! alias refresh in a single sqlx transaction. On any error -- an
//! optimistic-concurrency [`crate::Error::Conflict`] included -- the
//! transaction is dropped without commit, so SQLite and Postgres roll
//! it back atomically.
//!
//! Every function here is generic over the pool so one body serves both
//! backends; see the note under "Generic transaction helpers" in
//! [`crate::db::tx`] for why that costs so many `where` clauses.

use crate::db::{self, RecordId};
use crate::error::{Error, Result};
use crate::model::{Applied, BatchOp, BatchOutcome, Failed, Record, TxnMeta, WriteOutcome};
use crate::sync::{CommitRef, SyncedRepo};

// Each CRUD primitive performs its conflict check + mutation + alias
// refresh in a single sqlx transaction. On any error (including a
// `Conflict`) the transaction is dropped without commit, so SQLite /
// Postgres roll it back atomically.

// Note: See the comment under "Generic transaction helpers." in tx.rs if you are wondering why we need all these `where` clauses on these generic functions.

/// Where a CRUD write lands, before the file path is resolved.
///
/// Grouped for the reason [`crate::db::RecordId`] is -- `path` and `key`
/// are adjacent `&str`s a positional call site could transpose -- but
/// distinct from it: `file_path` is optional here because the caller may
/// leave it to the existing record's file or the worktree default, and
/// the resolution is what these functions do first.
pub(crate) struct WriteTarget<'a> {
    pub(crate) file_path: Option<&'a str>,
    pub(crate) path: &'a str,
    pub(crate) key: &'a str,
}

/// Extract the (expected_version, expected_commit) bind pair from a
/// [`CommitRef`] for the SQL-level OCC predicate baked into
/// [`db::tx::update_record`] / [`db::tx::upsert_record`] /
/// [`db::tx::delete_record`].
///
/// `enforce_conflict` is still called separately as an early-bailout
/// (avoids acquiring write locks for clearly-stale clients) — these
/// binds are the second-line race guard against another tx writing
/// between our lookup and our write.
pub(crate) fn occ_binds(expected: Option<&CommitRef>) -> (Option<i64>, Option<&str>) {
    match expected {
        Some(CommitRef::Pending(v)) => (Some(*v), None),
        Some(CommitRef::Commit(c)) => (None, Some(c.as_str())),
        None => (None, None),
    }
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
            // remains valid after `commit_repository` rolls forward
            // (commit attribution doesn't bump version).
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

pub(crate) async fn crud_create_in_pool<DB>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    at: WriteTarget<'_>,
    json: serde_json::Value,
    expected_commit: Option<CommitRef>,
    resolve: bool,
) -> Result<WriteOutcome>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<i64>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    db::tx::LookupRow: for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (String,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    let WriteTarget {
        file_path,
        path,
        key,
    } = at;
    let json_text = serde_json::to_string(&json).map_err(|e| Error::Json {
        path: path.to_string(),
        source: e,
    })?;
    let mut tx = pool.begin().await?;
    let lookup = db::tx::lookup_commits(&mut tx, sync.worktree_id(), file_path, path, key).await?;
    // Resolve the effective file_path: caller-supplied, then existing
    // record's file, then worktree default. NotFound when none of those
    // yield a value (e.g. brand-new key and no `default_file_path` set).
    let resolved_fp: String = file_path
        .map(str::to_string)
        .or_else(|| lookup.record_file_path.clone())
        .or_else(|| lookup.default_file_path.clone())
        .ok_or_else(|| Error::NotFound {
            file_path: String::new(),
            path: path.to_string(),
        })?;
    let file_path: &str = &resolved_fp;
    // Live row → conflict. Tombstones are treated as absent so
    // `create_record` resurrects them.
    if lookup.record_id.is_some() {
        return Err(Error::AlreadyExists {
            file_path: file_path.to_string(),
            path: path.to_string(),
        });
    }
    if let Some(exp) = expected_commit.as_ref() {
        enforce_conflict(
            file_path,
            path,
            exp,
            lookup.record_commit.as_ref(),
            lookup.record_version,
            false,
        )?;
    }
    let version = db::tx::next_version(&mut tx, sync.family_id(), 1).await?;
    let (exp_v, exp_c) = occ_binds(expected_commit.as_ref());
    let id = db::tx::create_record(
        &mut tx,
        RecordId {
            worktree_id: sync.worktree_id(),
            file_path,
            path,
            key,
        },
        &json_text,
        version,
        exp_v,
        exp_c,
    )
    .await?;
    let format_owner = db::tx::file_format(&mut tx, sync.worktree_id(), file_path).await?;
    db::tx::replace_aliases(
        &mut tx,
        id,
        &compute_aliases(
            sync,
            format_owner.as_deref(),
            id,
            file_path,
            path,
            key,
            &json,
        ),
    )
    .await?;
    if resolve {
        db::tx::delete_conflict_record(
            &mut tx,
            RecordId {
                worktree_id: sync.worktree_id(),
                file_path,
                path,
                key,
            },
        )
        .await?;
    }
    tx.commit().await?;
    Ok(WriteOutcome { id, version })
}

pub(crate) async fn crud_update_in_pool<DB>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    at: WriteTarget<'_>,
    json: serde_json::Value,
    expected_commit: Option<CommitRef>,
    resolve: bool,
) -> Result<WriteOutcome>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<i64>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    db::tx::LookupRow: for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (String,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    let WriteTarget {
        file_path,
        path,
        key,
    } = at;
    let json_text = serde_json::to_string(&json).map_err(|e| Error::Json {
        path: path.to_string(),
        source: e,
    })?;
    let mut tx = pool.begin().await?;
    let lookup = db::tx::lookup_commits(&mut tx, sync.worktree_id(), file_path, path, key).await?;
    // Resolve the effective file_path: caller-supplied, then existing
    // record's file. update/delete don't fall back to the worktree
    // default — they require an existing record, and the
    // `record_id.ok_or(NotFound)` below catches the absent case.
    let resolved_fp: String = file_path
        .map(str::to_string)
        .or_else(|| lookup.record_file_path.clone())
        .unwrap_or_default();
    let file_path: &str = &resolved_fp;
    // Tombstone or absent → NotFound.
    let id = lookup.record_id.ok_or_else(|| Error::NotFound {
        file_path: file_path.to_string(),
        path: path.to_string(),
    })?;
    if let Some(exp) = expected_commit.as_ref() {
        enforce_conflict(
            file_path,
            path,
            exp,
            lookup.record_commit.as_ref(),
            lookup.record_version,
            true,
        )?;
    }
    let version = db::tx::next_version(&mut tx, sync.family_id(), 1).await?;
    let (exp_v, exp_c) = occ_binds(expected_commit.as_ref());
    db::tx::update_record(&mut tx, id, &json_text, version, exp_v, exp_c).await?;
    let format_owner = db::tx::file_format(&mut tx, sync.worktree_id(), file_path).await?;
    db::tx::replace_aliases(
        &mut tx,
        id,
        &compute_aliases(
            sync,
            format_owner.as_deref(),
            id,
            file_path,
            path,
            key,
            &json,
        ),
    )
    .await?;
    if resolve {
        db::tx::delete_conflict_record(
            &mut tx,
            RecordId {
                worktree_id: sync.worktree_id(),
                file_path,
                path,
                key,
            },
        )
        .await?;
    }
    tx.commit().await?;
    Ok(WriteOutcome { id, version })
}

pub(crate) async fn crud_upsert_in_pool<DB>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    at: WriteTarget<'_>,
    json: serde_json::Value,
    expected_commit: Option<CommitRef>,
    resolve: bool,
) -> Result<WriteOutcome>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<i64>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    db::tx::LookupRow: for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (String,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    let WriteTarget {
        file_path,
        path,
        key,
    } = at;
    let json_text = serde_json::to_string(&json).map_err(|e| Error::Json {
        path: path.to_string(),
        source: e,
    })?;
    let mut tx = pool.begin().await?;
    let lookup = db::tx::lookup_commits(&mut tx, sync.worktree_id(), file_path, path, key).await?;
    // Resolve the effective file_path: caller-supplied, then existing
    // record's file, then worktree default. NotFound when none of those
    // yield a value (e.g. brand-new key and no `default_file_path` set).
    let resolved_fp: String = file_path
        .map(str::to_string)
        .or_else(|| lookup.record_file_path.clone())
        .or_else(|| lookup.default_file_path.clone())
        .ok_or_else(|| Error::NotFound {
            file_path: String::new(),
            path: path.to_string(),
        })?;
    let file_path: &str = &resolved_fp;
    if let Some(exp) = expected_commit.as_ref() {
        enforce_conflict(
            file_path,
            path,
            exp,
            lookup.record_commit.as_ref(),
            lookup.record_version,
            lookup.record_id.is_some(),
        )?;
    }
    let format_owner = ensure_file_registered(sync, &mut tx, file_path, path).await?;
    let version = db::tx::next_version(&mut tx, sync.family_id(), 1).await?;
    let (exp_v, exp_c) = occ_binds(expected_commit.as_ref());
    let id = db::tx::upsert_record(
        &mut tx,
        RecordId {
            worktree_id: sync.worktree_id(),
            file_path,
            path,
            key,
        },
        &json_text,
        version,
        exp_v,
        exp_c,
    )
    .await?;
    db::tx::replace_aliases(
        &mut tx,
        id,
        &compute_aliases(
            sync,
            format_owner.as_deref(),
            id,
            file_path,
            path,
            key,
            &json,
        ),
    )
    .await?;
    if resolve {
        db::tx::delete_conflict_record(
            &mut tx,
            RecordId {
                worktree_id: sync.worktree_id(),
                file_path,
                path,
                key,
            },
        )
        .await?;
    }
    tx.commit().await?;
    Ok(WriteOutcome { id, version })
}

pub(crate) async fn crud_delete_in_pool<DB>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    at: WriteTarget<'_>,
    expected_commit: Option<CommitRef>,
    resolve: bool,
) -> Result<WriteOutcome>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<i64>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    db::tx::LookupRow: for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    let WriteTarget {
        file_path,
        path,
        key,
    } = at;
    let mut tx = pool.begin().await?;
    let lookup = db::tx::lookup_commits(&mut tx, sync.worktree_id(), file_path, path, key).await?;
    // Resolve the effective file_path: caller-supplied, then existing
    // record's file. update/delete don't fall back to the worktree
    // default — they require an existing record, and the
    // `record_id.ok_or(NotFound)` below catches the absent case.
    let resolved_fp: String = file_path
        .map(str::to_string)
        .or_else(|| lookup.record_file_path.clone())
        .unwrap_or_default();
    let file_path: &str = &resolved_fp;
    // Tombstone or absent → NotFound. We never hard-delete here;
    // `commit_repository` is the only path that purges tombstones once
    // they've been written to disk.
    let id = lookup.record_id.ok_or_else(|| Error::NotFound {
        file_path: file_path.to_string(),
        path: path.to_string(),
    })?;
    if let Some(exp) = expected_commit.as_ref() {
        enforce_conflict(
            file_path,
            path,
            exp,
            lookup.record_commit.as_ref(),
            lookup.record_version,
            true,
        )?;
    }
    let version = db::tx::next_version(&mut tx, sync.family_id(), 1).await?;
    let (exp_v, exp_c) = occ_binds(expected_commit.as_ref());
    db::tx::delete_record(&mut tx, id, version, exp_v, exp_c).await?;
    if resolve {
        db::tx::delete_conflict_record(
            &mut tx,
            RecordId {
                worktree_id: sync.worktree_id(),
                file_path,
                path,
                key,
            },
        )
        .await?;
    }
    tx.commit().await?;
    Ok(WriteOutcome { id, version })
}

/// Apply a single [`BatchOp`] in the caller-owned transaction,
/// returning the new `(id, version)` on success.
///
/// Layered over [`db::tx::lookup_commits`] / [`enforce_conflict`] /
/// [`db::tx::next_version`] / [`db::tx::upsert_record`] /
/// [`db::tx::file_format`] / [`db::tx::replace_aliases`] /
/// [`db::tx::delete_record`].
async fn apply_one_in_tx<DB>(
    sync: &SyncedRepo,
    tx: &mut sqlx::Transaction<'_, DB>,
    op: BatchOp,
    version: i64,
) -> Result<WriteOutcome>
where
    DB: db::tx::Dialect,
    // bind-value bounds (every db::tx::* helper calls `.bind(...)`)
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<i64>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    // arguments + executor (every helper runs a query through `&mut **tx`)
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    // row-decoding bounds: the three concrete row shapes used by
    // lookup_commits / next_version / file_format.
    db::tx::LookupRow: for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (String,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    match op {
        BatchOp::Upsert {
            file_path,
            path: op_path,
            key: op_key,
            json,
            expected,
            resolve,
        } => {
            let json_text = serde_json::to_string(&json).map_err(|e| Error::Json {
                path: op_path.clone(),
                source: e,
            })?;
            let lookup = db::tx::lookup_commits(
                tx,
                sync.worktree_id(),
                file_path.as_deref(),
                &op_path,
                &op_key,
            )
            .await?;
            // Resolve the effective file_path: caller-supplied, then
            // existing record's file, then worktree default. NotFound
            // when none of those yield a value (e.g. brand-new key
            // and no `default_file_path` set).
            let resolved_fp: String = file_path
                .as_deref()
                .map(str::to_string)
                .or_else(|| lookup.record_file_path.clone())
                .or_else(|| lookup.default_file_path.clone())
                .ok_or_else(|| Error::NotFound {
                    file_path: String::new(),
                    path: op_path.clone(),
                })?;
            if let Some(exp) = expected.as_ref() {
                enforce_conflict(
                    &resolved_fp,
                    &op_path,
                    exp,
                    lookup.record_commit.as_ref(),
                    lookup.record_version,
                    lookup.record_id.is_some(),
                )?;
            }
            let format_owner = ensure_file_registered(sync, tx, &resolved_fp, &op_path).await?;
            let (exp_v, exp_c) = occ_binds(expected.as_ref());
            let id = db::tx::upsert_record(
                tx,
                RecordId {
                    worktree_id: sync.worktree_id(),
                    file_path: &resolved_fp,
                    path: &op_path,
                    key: &op_key,
                },
                &json_text,
                version,
                exp_v,
                exp_c,
            )
            .await?;
            db::tx::replace_aliases(
                tx,
                id,
                &compute_aliases(
                    sync,
                    format_owner.as_deref(),
                    id,
                    &resolved_fp,
                    &op_path,
                    &op_key,
                    &json,
                ),
            )
            .await?;
            if resolve {
                db::tx::delete_conflict_record(
                    tx,
                    RecordId {
                        worktree_id: sync.worktree_id(),
                        file_path: &resolved_fp,
                        path: &op_path,
                        key: &op_key,
                    },
                )
                .await?;
            }
            Ok(WriteOutcome { id, version })
        }
        BatchOp::Delete {
            file_path,
            path: op_path,
            key: op_key,
            expected,
            resolve,
        } => {
            let lookup = db::tx::lookup_commits(
                tx,
                sync.worktree_id(),
                file_path.as_deref(),
                &op_path,
                &op_key,
            )
            .await?;
            // Resolve the effective file_path: caller-supplied, then
            // existing record's file. update/delete don't fall back
            // to the worktree default — they require an existing
            // record, and the `record_id.ok_or(NotFound)` below
            // catches the absent case.
            let resolved_fp: String = file_path
                .as_deref()
                .map(str::to_string)
                .or_else(|| lookup.record_file_path.clone())
                .unwrap_or_default();
            let id = lookup.record_id.ok_or_else(|| Error::NotFound {
                file_path: resolved_fp.clone(),
                path: op_path.clone(),
            })?;
            if let Some(exp) = expected.as_ref() {
                enforce_conflict(
                    &resolved_fp,
                    &op_path,
                    exp,
                    lookup.record_commit.as_ref(),
                    lookup.record_version,
                    true,
                )?;
            }
            let (exp_v, exp_c) = occ_binds(expected.as_ref());
            db::tx::delete_record(tx, id, version, exp_v, exp_c).await?;
            if resolve {
                db::tx::delete_conflict_record(
                    tx,
                    RecordId {
                        worktree_id: sync.worktree_id(),
                        file_path: &resolved_fp,
                        path: &op_path,
                        key: &op_key,
                    },
                )
                .await?;
            }
            Ok(WriteOutcome { id, version })
        }
    }
}

/// Generic batch driver: opens one transaction on `pool`, walks
/// `ops`, accumulates [`Applied`] / [`Failed`] entries, and either
/// commits (success / non-atomic with failures) or rolls back (atomic
/// + first failure).
//
pub(crate) async fn apply_batch_inner<DB>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    ops: Vec<BatchOp>,
    atomic: bool,
    meta: Option<TxnMeta>,
) -> Result<BatchOutcome>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<i64>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    db::tx::LookupRow: for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (String,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    let mut outcome = BatchOutcome::default();
    let mut tx = pool.begin().await?;
    // One draw for the batch rather than one per op. The family's
    // counter row is locked until this transaction commits, so drawing
    // per op would hold it for the whole batch and stall every other
    // writer in the family behind a large import. The cost is that an op
    // which fails still consumes its slot, leaving a gap -- reported by
    // `RollupTxn::unaccounted` rather than hidden.
    let base = if ops.is_empty() {
        0
    } else {
        db::tx::next_version(&mut tx, sync.family_id(), ops.len() as i64).await?
    };
    for (index, op) in ops.into_iter().enumerate() {
        let path = op.path().to_string();
        let key = op.key().to_string();
        // Read before `op` moves into the call: a delete tombstones the
        // row, so nothing downstream can tell it from a write.
        let deleted = matches!(op, BatchOp::Delete { .. });
        match apply_one_in_tx(sync, &mut tx, op, base + index as i64).await {
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
                    // Drop the tx without commit → rollback; clear any
                    // "applied" entries we'd optimistically pushed
                    // (they didn't really commit).
                    drop(tx);
                    outcome.applied.clear();
                    outcome.last_version = None;
                    return Ok(outcome);
                }
                // Non-atomic: the application-level conflict didn't
                // poison the SQL tx, so continue.
            }
            Err(other) => {
                return Err(other);
            }
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
        db::tx::insert_txn(
            &mut tx,
            sync.worktree_id(),
            first,
            last,
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
/// cloudmap, say. Records carry a foreign key to the `file` table, so the row
/// has to exist before the insert; the file itself is synthesised on the next
/// [`SyncedRepo::write_file`], which already handles a missing file on disk.
/// The format is taken from whichever registered [`crate::DataFormat`] claims
/// the record's section.
pub(crate) async fn ensure_file_registered<DB>(
    sync: &SyncedRepo,
    tx: &mut sqlx::Transaction<'_, DB>,
    file_path: &str,
    record_path: &str,
) -> Result<Option<String>>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    (String,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    if let Some(existing) = db::tx::file_format(tx, sync.worktree_id(), file_path).await? {
        return Ok(Some(existing));
    }
    // No format claims this section, so there's nothing to record the file as
    // — `file.format` is NOT NULL. Callers writing a section no registered
    // format knows about get a clear error instead of an FK violation.
    let format = sync
        .formats()
        .for_path(record_path)
        .ok_or_else(|| Error::UnknownFormat(record_path.to_string()))?
        .name()
        .to_string();
    db::tx::ensure_file(tx, sync.worktree_id(), file_path, &format).await?;
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
pub(crate) async fn delete_file_in_pool<DB>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    file_path: &str,
    expected_commit: Option<CommitRef>,
) -> Result<Vec<WriteOutcome>>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<i64>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (Option<String>, i64): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    let mut tx = pool.begin().await?;
    let (commit_id, last_version) =
        db::tx::lookup_file_state(&mut tx, sync.worktree_id(), file_path)
            .await?
            .ok_or_else(|| Error::NotFound {
                file_path: file_path.to_string(),
                path: String::new(),
            })?;
    if let Some(expected) = expected_commit.as_ref() {
        // The file stands in for a record here: `record_present` is
        // true because the *file* is what the token is about, and the
        // version is the highest any record in it carries.
        enforce_conflict(
            file_path,
            "",
            expected,
            commit_id.as_ref(),
            Some(last_version),
            true,
        )?;
    }

    let ids = db::tx::list_file_live_record_ids(&mut tx, sync.worktree_id(), file_path).await?;
    let base = if ids.is_empty() {
        0
    } else {
        db::tx::next_version(&mut tx, sync.family_id(), ids.len() as i64).await?
    };
    let mut out = Vec::with_capacity(ids.len());
    for (index, id) in ids.into_iter().enumerate() {
        let version = base + index as i64;
        // No OCC binds: the check above covered the whole file, and a
        // per-row token would be asking a different question.
        db::tx::delete_record(&mut tx, id, version, None, None).await?;
        out.push(WriteOutcome { id, version });
    }
    db::tx::set_file_deleted(&mut tx, sync.worktree_id(), file_path, true).await?;
    tx.commit().await?;
    Ok(out)
}
