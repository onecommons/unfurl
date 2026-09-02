// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Divergence between the file and the database: naming it, recording
//! it, and settling it.
//!
//! Both sync directions meet here. A scan finds the file disagreeing
//! with an in-flight edit; a write finds it while rendering over a
//! document that moved underneath. Either way the answer is the same
//! and lives in one place: neither side is overwritten, the file's
//! value becomes a conflict row beside the database's own, and the
//! record waits for [`crate::SyncedRepo::resolve_conflict`].
//!
//! [`classify_conflict`] is the three-way decision -- theirs has to
//! have moved off the *base*, not merely differ from ours, which any
//! unsaved edit trivially does -- and [`conflict_kind`] names the
//! result. The rest is bookkeeping over the conflict rows themselves.

use std::collections::BTreeSet;

use crate::crud::{compute_aliases, enforce_conflict, occ_binds};
use unfurl_merge::markdown::Applied;

use crate::db::{self, RecordId};
use crate::document::{apply_delete, apply_insert, extract_ext, Syntax};
use crate::error::{Error, Result};
use crate::git;
use crate::model::{
    ConflictState, Record, RecordConflict, RecordConflictKind, Resolution, WriteOutcome,
};
use crate::sync::{CommitRef, SyncedRepo};

/// `rel_path`'s parsed document at `commit` — a pending edit's base
/// content — or `None` when the commit, the path, or the parse is
/// unavailable. The caller treats an unknown base as diverged: failing
/// open would hide a conflict, failing closed only re-reports one.
pub(crate) fn doc_at_commit(
    repo: &gix::Repository,
    commit: &str,
    rel_path: &str,
) -> Option<serde_json::Value> {
    let bytes = git::read_blob_at_commit(repo, commit, rel_path).ok()??;
    let syntax = Syntax::for_extension(&extract_ext(rel_path))?;
    syntax.parse(rel_path, &bytes).ok().map(|p| p.value)
}

/// Parse `rel_path` as it was at each commit in `bases`.
///
/// Synchronous, and takes the gix handle for the length of one call, so
/// the (non-`Sync`) `Repository` never lives across an await. A commit
/// whose blob or parse is unavailable maps to `None`, which the
/// classification treats as diverged — failing open would hide a
/// conflict, failing closed only re-reports one.
pub(crate) fn read_base_docs(
    sync: &SyncedRepo,
    rel_path: &str,
    bases: BTreeSet<String>,
) -> std::collections::HashMap<String, Option<serde_json::Value>> {
    if bases.is_empty() {
        return Default::default();
    }
    match sync.repo() {
        Ok(repo) => bases
            .into_iter()
            .map(|commit| {
                let doc = doc_at_commit(&repo, &commit, rel_path);
                (commit, doc)
            })
            .collect(),
        // Unreadable repo → unknown bases → report conservatively.
        Err(_) => bases.into_iter().map(|commit| (commit, None)).collect(),
    }
}

/// The three-way conflict decision both sync directions share: does
/// the file-side value at a pending row's key diverge from what the
/// row's edit was based on?
///
/// `ours` / `deleted` / `has_base` describe the pending row; `theirs`
/// is the file's current value at its key (`None` = key absent);
/// `base` is the record's content at the base commit (`None` = absent
/// there, or unknowable — treated as diverged, failing closed: a
/// missed conflict hides data loss, a spurious one only re-reports).
pub(crate) fn classify_conflict(
    ours: &serde_json::Value,
    deleted: bool,
    has_base: bool,
    base: Option<&serde_json::Value>,
    theirs: Option<&serde_json::Value>,
) -> Option<RecordConflictKind> {
    match theirs {
        // `Value` map equality is order-insensitive, so a mere
        // reordering on disk is not a divergence.
        Some(t) if *t == *ours => None,
        // A tombstone's json is the value it deletes — the base — so
        // ours-vs-theirs already is base-vs-theirs.
        Some(_) if deleted => Some(conflict_kind(deleted, false, has_base)),
        Some(t) if has_base => {
            if base == Some(t) {
                // The file still holds exactly what this edit was
                // based on: the only change is ours, not yet saved.
                None
            } else {
                Some(conflict_kind(deleted, false, has_base))
            }
        }
        Some(_) => Some(conflict_kind(deleted, false, has_base)),
        // Key absent from the file: a tombstone agrees and a create
        // was never there — only an edit of a record the file dropped
        // diverges.
        None if !deleted && has_base => Some(conflict_kind(deleted, true, has_base)),
        None => None,
    }
}

/// Name a divergence from the shape of the two sides.
///
/// Split out of [`classify_conflict`] because a materialized conflict
/// row is *already* known to diverge — re-deciding that from a base the
/// write path may not have read would risk the two disagreeing. This
/// only names what the row records, and stays the single authority for
/// the naming.
pub(crate) fn conflict_kind(
    ours_deleted: bool,
    theirs_deleted: bool,
    has_base: bool,
) -> RecordConflictKind {
    match (ours_deleted, theirs_deleted) {
        // Ours deletes a record whose value the file has moved on from,
        // so the record being deleted is not the one the client saw.
        (true, _) => RecordConflictKind::DeleteModify,
        (false, true) => RecordConflictKind::ModifyDelete,
        // Without a base neither side edited a shared ancestor: both
        // introduced the key.
        (false, false) if has_base => RecordConflictKind::ModifyModify,
        (false, false) => RecordConflictKind::AddAdd,
    }
}

/// A [`crate::db::RecordId`] for one record of `file_path`, so the four
/// parts travel as a unit rather than as loose positional strings.
pub(crate) fn record_id<'a>(
    sync: &SyncedRepo,
    file_path: &'a str,
    path: &'a str,
    key: &'a str,
) -> RecordId<'a> {
    RecordId {
        worktree_id: sync.worktree_id(),
        file_path,
        path,
        key,
    }
}

/// The file's side of a divergence, as a conflict row records it.
///
/// Grouped for the same reason as [`crate::db::RecordId`]: the three
/// travel together and a positional call site could transpose them.
pub(crate) struct TheirSide<'a> {
    /// The file's value -- or, when `deleted`, the one it dropped. The
    /// column is NOT NULL and a tombstone's json already reads as "the
    /// value this removes".
    pub(crate) json: &'a serde_json::Value,
    /// The file no longer has this record.
    pub(crate) deleted: bool,
    /// Commit carrying this value, or `None` when it is not in git --
    /// an uncommitted hand edit. Not "the commit that last touched the
    /// path", which is a different question and would name a commit
    /// that does not hold what this row records.
    pub(crate) commit_id: Option<&'a str>,
}

/// Record the file's value for a diverged record, creating the conflict
/// row or refreshing the one already there.
///
/// A fresh version is drawn only when the value, its presence, or the
/// state actually moves. The row is a `list_changes` entry like any
/// other, so re-stamping it every time an unrelated record in the same
/// file changes would be churn — and would invalidate `Pending` tokens
/// for a conflict nobody touched. The consequence is that `commit_id`
/// tracks the value rather than the file: a commit made *outside* this
/// crate that changes nothing about the divergence leaves it naming the
/// older commit. It is informational — `db::commit::roll_forward`
/// restamps it on the next commit made through here, and nothing reads
/// it to decide anything.
pub(crate) async fn refresh_conflict_row<DB>(
    tx: &mut sqlx::Transaction<'_, DB>,
    sync: &SyncedRepo,
    at: RecordId<'_>,
    theirs: TheirSide<'_>,
    existing: Option<&db::tx::ConflictRecord>,
) -> Result<()>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    if matches!(existing, Some(c)
        if c.state == ConflictState::Conflict
            && c.deleted == theirs.deleted
            && c.json == *theirs.json)
    {
        return Ok(());
    }
    let json_text = serde_json::to_string(theirs.json).map_err(|e| Error::Json {
        path: at.path.to_string(),
        source: e,
    })?;
    let version = db::tx::next_version(tx, sync.family_id(), 1).await?;
    db::tx::upsert_conflict_record(
        tx,
        at,
        &json_text,
        theirs.commit_id,
        theirs.deleted,
        version,
        ConflictState::Conflict,
    )
    .await?;
    Ok(())
}

/// Drop the conflict row at this key, if `existing` says there is one.
pub(crate) async fn drop_conflict_row<DB>(
    tx: &mut sqlx::Transaction<'_, DB>,
    _sync: &SyncedRepo,
    at: RecordId<'_>,
    existing: Option<&db::tx::ConflictRecord>,
) -> Result<()>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
{
    if existing.is_none() {
        return Ok(());
    }
    db::tx::delete_conflict_record(tx, at).await
}

/// Inputs for the write path's conflict detection, precomputed by
/// [`SyncedRepo::write_file`] when the file on disk holds an edit the
/// database never took in.
pub(crate) struct ConflictCheck {
    /// Base commit oid → the file's parsed document at that commit.
    pub(crate) base_docs: std::collections::HashMap<String, Option<serde_json::Value>>,
}

/// One change [`apply_pending_records`] wants made to the conflict rows
/// of the file it just rendered.
pub(crate) enum ConflictOp {
    /// Record (or refresh) the file's side of a divergence.
    Open {
        path: String,
        key: String,
        /// The file's value — or, when `deleted`, the one it dropped.
        json: serde_json::Value,
        /// The file no longer has this record.
        deleted: bool,
    },
    /// Drop the conflict row: its resolution has just been applied.
    Clear { path: String, key: String },
}

impl ConflictOp {
    fn path(&self) -> &str {
        match self {
            Self::Open { path, .. } | Self::Clear { path, .. } => path,
        }
    }
    fn key(&self) -> &str {
        match self {
            Self::Open { key, .. } | Self::Clear { key, .. } => key,
        }
    }
}

/// What [`apply_pending_records`] worked out about one file.
pub(crate) struct Applying {
    /// Top-level section names this batch wrote into (insertion-order,
    /// no duplicates), so the caller can re-sort just those.
    pub(crate) touched: Vec<String>,
    /// Divergences, whether newly found or already on record.
    pub(crate) conflicts: Vec<RecordConflict>,
    /// Conflict-row bookkeeping for the caller to persist.
    pub(crate) ops: Vec<ConflictOp>,
    /// Every record this batch actually applied, in order.
    ///
    /// What a renderer that cannot use the mutated `root` needs
    /// instead — [`crate::markdown`] writes into the source's own
    /// blocks. Reported here rather than derived from `pending` by the
    /// caller, because `pending` also holds the records the conflict
    /// logic *declined*, and writing one of those into a document is
    /// exactly what a conflict is supposed to prevent.
    pub(crate) applied: Vec<Applied>,
}

/// Apply every pending record to `root` in order, leaving conflicted
/// ones alone.
///
/// Three ways a record can be skipped, and they differ in what the
/// database is asked to remember:
///
/// - a standing conflict row: the file's value was already declared the
///   one to keep until someone resolves, so nothing is applied and
///   nothing changes;
/// - a resolved one whose file value has moved since: the resolution was
///   made against a value that is gone, so it re-opens as a conflict;
/// - a newly-found divergence, which only `check` (a stale source) can
///   turn up: it becomes a conflict row.
///
/// A resolution the file has *not* invalidated is the one case where a
/// conflict row leads to a write: the record is applied and the row
/// cleared.
pub(crate) fn apply_pending_records(
    root: &mut serde_json::Value,
    file_path: &str,
    pending: Vec<Record>,
    format: Option<&dyn crate::DataFormat>,
    bases: &std::collections::HashMap<(String, String), Option<String>>,
    conflict_rows: &std::collections::HashMap<(String, String), Record>,
    check: Option<&ConflictCheck>,
) -> Applying {
    let mut out = Applying {
        touched: Vec::new(),
        conflicts: Vec::new(),
        ops: Vec::new(),
        applied: Vec::new(),
    };
    for rec in pending {
        let section_name = rec.path.trim_start_matches('/').to_string();
        // v1 supports single-segment parents only.
        if section_name.is_empty() {
            continue;
        }
        let at = (rec.path.clone(), rec.key.clone());
        let theirs = root
            .get(&section_name)
            .and_then(|s| s.get(&rec.key))
            .cloned();
        let base_commit = bases.get(&at).cloned().flatten();
        let report = |kind, base_commit: &Option<String>, theirs: Option<&serde_json::Value>| {
            RecordConflict {
                file_path: file_path.to_string(),
                path: rec.path.clone(),
                key: rec.key.clone(),
                kind,
                base_commit_id: base_commit.clone(),
                theirs: theirs.cloned(),
            }
        };

        match conflict_rows.get(&at).and_then(|row| row.conflict) {
            Some(ConflictState::Conflict) => {
                let row = &conflict_rows[&at];
                out.conflicts.push(report(
                    conflict_kind(rec.deleted, row.deleted, base_commit.is_some()),
                    &base_commit,
                    theirs.as_ref(),
                ));
                continue;
            }
            Some(ConflictState::Resolved) => {
                let row = &conflict_rows[&at];
                let unmoved = match &theirs {
                    Some(value) => !row.deleted && *value == row.json,
                    None => row.deleted,
                };
                if unmoved {
                    out.ops.push(ConflictOp::Clear {
                        path: rec.path.clone(),
                        key: rec.key.clone(),
                    });
                } else {
                    // The file changed under the resolution, so the
                    // decision was made about a value that is gone.
                    let kind = conflict_kind(rec.deleted, theirs.is_none(), base_commit.is_some());
                    tracing::warn!(
                        file = %file_path, path = %rec.path, key = %rec.key, kind = ?kind,
                        "file moved again since the conflict was resolved; re-opening it"
                    );
                    out.ops.push(ConflictOp::Open {
                        path: rec.path.clone(),
                        key: rec.key.clone(),
                        // A file that dropped the record leaves no value
                        // to hold, so the last one it had stands in.
                        json: theirs.clone().unwrap_or_else(|| row.json.clone()),
                        deleted: theirs.is_none(),
                    });
                    out.conflicts
                        .push(report(kind, &base_commit, theirs.as_ref()));
                    continue;
                }
            }
            None => {
                if let Some(check) = check {
                    let base_value = base_commit
                        .as_deref()
                        .and_then(|base| check.base_docs.get(base))
                        .and_then(|doc| doc.as_ref())
                        .and_then(|doc| doc.get(&section_name))
                        .and_then(|section| section.get(&rec.key));
                    if let Some(kind) = classify_conflict(
                        &rec.json,
                        rec.deleted,
                        base_commit.is_some(),
                        base_value,
                        theirs.as_ref(),
                    ) {
                        tracing::warn!(
                            file = %file_path, path = %rec.path, key = %rec.key, kind = ?kind,
                            "file diverges from a pending edit; keeping both sides"
                        );
                        out.ops.push(ConflictOp::Open {
                            path: rec.path.clone(),
                            key: rec.key.clone(),
                            json: theirs
                                .clone()
                                .or_else(|| base_value.cloned())
                                .unwrap_or_else(|| rec.json.clone()),
                            deleted: theirs.is_none(),
                        });
                        out.conflicts
                            .push(report(kind, &base_commit, theirs.as_ref()));
                        continue;
                    }
                }
            }
        }

        let root_obj = root.as_object_mut().expect("root is object");
        let (key, deleted) = (rec.key.clone(), rec.deleted);
        if rec.deleted {
            apply_delete(root_obj, &section_name, &rec.key);
        } else {
            apply_insert(root_obj, &section_name, rec.key, rec.json, format);
        }
        out.applied.push(Applied {
            section: section_name.clone(),
            key: key.clone(),
            deleted,
        });
        if !out.touched.contains(&section_name) {
            out.touched.push(section_name);
        }
    }
    out
}

/// Persist [`ConflictOp`]s in one transaction, drawing a version for
/// each row that actually moves (see [`refresh_conflict_row`]).
pub(crate) async fn apply_conflict_ops_in_pool<DB>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    file_path: &str,
    commit_id: Option<&str>,
    ops: &[ConflictOp],
) -> Result<()>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (i64, String, String, String, i64, String):
        for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    let mut tx = pool.begin().await?;
    let existing = db::tx::list_conflict_records(&mut tx, sync.worktree_id(), file_path).await?;
    for op in ops {
        let key = (op.path().to_string(), op.key().to_string());
        match op {
            ConflictOp::Open { json, deleted, .. } => {
                refresh_conflict_row(
                    &mut tx,
                    sync,
                    record_id(sync, file_path, op.path(), op.key()),
                    TheirSide {
                        json,
                        deleted: *deleted,
                        commit_id,
                    },
                    existing.get(&key),
                )
                .await?;
            }
            ConflictOp::Clear { .. } => {
                drop_conflict_row(
                    &mut tx,
                    sync,
                    record_id(sync, file_path, op.path(), op.key()),
                    existing.get(&key),
                )
                .await?;
            }
        }
    }
    tx.commit().await?;
    Ok(())
}

/// Body of [`SyncedRepo::resolve_conflict`], generic over the pool.
///
/// Both rows move together or not at all: rewriting the record without
/// dropping the conflict row would leave the record looking settled
/// while every read still treats it as contested.
pub(crate) async fn resolve_conflict_in_pool<DB>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    file_path: &str,
    path: &str,
    key: &str,
    resolution: Resolution,
    expected_commit: Option<CommitRef>,
) -> Result<WriteOutcome>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<i64>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (String,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (i64, Option<String>, i64, i64):
        for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (i64, String, String, String, i64, String):
        for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    let not_found = || Error::NotFound {
        file_path: file_path.to_string(),
        path: path.to_string(),
    };
    let id = RecordId {
        worktree_id: sync.worktree_id(),
        file_path,
        path,
        key,
    };
    let mut tx = pool.begin().await?;
    let at = (path.to_string(), key.to_string());
    let theirs = db::tx::list_conflict_records(&mut tx, sync.worktree_id(), file_path)
        .await?
        .remove(&at)
        .ok_or_else(not_found)?;
    // The record itself, tombstones included: an in-flight delete is a
    // side of the argument like any other.
    let ours = db::tx::lookup_own_record(&mut tx, id)
        .await?
        .ok_or_else(not_found)?;
    if let Some(exp) = expected_commit.as_ref() {
        enforce_conflict(
            file_path,
            path,
            exp,
            ours.commit_id.as_ref(),
            Some(ours.version),
            true,
        )?;
    }
    let version = db::tx::next_version(&mut tx, sync.family_id(), 1).await?;
    let (exp_v, exp_c) = occ_binds(expected_commit.as_ref());

    // `Ours` restates no value, so it cannot be checked against the file
    // now — it records the decision and the next write checks it. Every
    // other variant names one, so the conflict row goes immediately.
    if resolution == Resolution::Ours {
        let resolved = db::tx::set_conflict_state(&mut tx, id, ConflictState::Resolved, version)
            .await?
            .ok_or_else(not_found)?;
        tx.commit().await?;
        return Ok(WriteOutcome {
            id: resolved,
            version,
        });
    }

    // What the record becomes. Taking the file's side of a record it no
    // longer has means deleting ours too.
    let winner = match &resolution {
        Resolution::Delete => None,
        Resolution::Merged(json) => Some(json.clone()),
        Resolution::Theirs if theirs.deleted => None,
        Resolution::Theirs => Some(theirs.json.clone()),
        Resolution::Ours => unreachable!("handled above"),
    };
    match winner {
        Some(json) => {
            let json_text = serde_json::to_string(&json).map_err(|e| Error::Json {
                path: path.to_string(),
                source: e,
            })?;
            db::tx::update_record(&mut tx, ours.id, &json_text, version, exp_v, exp_c).await?;
            let format_owner = db::tx::file_format(&mut tx, sync.worktree_id(), file_path).await?;
            db::tx::replace_aliases(
                &mut tx,
                ours.id,
                &compute_aliases(
                    sync,
                    format_owner.as_deref(),
                    ours.id,
                    file_path,
                    path,
                    key,
                    &json,
                ),
            )
            .await?;
        }
        None => db::tx::delete_record(&mut tx, ours.id, version, exp_v, exp_c).await?,
    }
    db::tx::delete_conflict_record(&mut tx, id).await?;
    tx.commit().await?;
    Ok(WriteOutcome {
        id: ours.id,
        version,
    })
}

#[cfg(test)]
mod tests {
    use super::classify_conflict;
    use crate::RecordConflictKind::*;

    /// Every cell of the three-way decision table, one assertion each.
    #[test]
    fn classify_conflict_covers_every_pairing() {
        use serde_json::json;
        let ours = &json!({"name": "ours"});
        let base = json!({"name": "base"});
        let theirs = json!({"name": "theirs"});
        // The file already holds ours: agreement, whatever the row is.
        assert_eq!(
            classify_conflict(ours, false, true, Some(&base), Some(ours)),
            None
        );
        assert_eq!(
            classify_conflict(ours, true, true, Some(&base), Some(ours)),
            None
        );
        // The file still holds the base: an ordinary unsaved edit.
        assert_eq!(
            classify_conflict(ours, false, true, Some(&base), Some(&base)),
            None
        );
        // The file moved off the base under a pending edit.
        assert_eq!(
            classify_conflict(ours, false, true, Some(&base), Some(&theirs)),
            Some(ModifyModify)
        );
        // An unknowable base fails closed.
        assert_eq!(
            classify_conflict(ours, false, true, None, Some(&theirs)),
            Some(ModifyModify)
        );
        // A tombstone's json is the base, so any other value diverges.
        assert_eq!(
            classify_conflict(ours, true, true, Some(&base), Some(&theirs)),
            Some(DeleteModify)
        );
        // Both sides added the key independently.
        assert_eq!(
            classify_conflict(ours, false, false, None, Some(&theirs)),
            Some(AddAdd)
        );
        // Key absent from the file: only a based edit diverges —
        // a create was never there, a tombstone agrees.
        assert_eq!(
            classify_conflict(ours, false, true, Some(&base), None),
            Some(ModifyDelete)
        );
        assert_eq!(classify_conflict(ours, false, false, None, None), None);
        assert_eq!(classify_conflict(ours, true, true, Some(&base), None), None);
    }
}
