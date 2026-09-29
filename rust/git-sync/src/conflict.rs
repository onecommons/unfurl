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

use crate::crud::{compute_aliases, enforce_conflict};
use unfurl_merge::markdown::Applied;

use crate::db::store::{At, Filter, Row, Scope, Store};
use crate::document::{apply_delete, apply_insert};
use crate::error::{Error, Result};
use crate::model::{
    ConflictState, Record, RecordConflict, RecordConflictKind, Resolution, WriteOutcome,
};
use crate::segments::{self, Origin, Value};
use crate::sync::{CommitRef, SyncedRepo};

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

/// The file's side of a divergence, as a conflict row records it.
///
/// Grouped because the three
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
pub(crate) async fn refresh_conflict_row<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    sync: &SyncedRepo,
    at: At<'_>,
    theirs: TheirSide<'_>,
    existing: Option<&Row>,
) -> Result<()> {
    if matches!(existing, Some(c)
        if c.conflict == Some(ConflictState::Conflict)
            && c.deleted == theirs.deleted
            && c.json == *theirs.json)
    {
        return Ok(());
    }
    let d = DB::segs(tx, sync.worktree_id()).await?.draft;
    let version = DB::next_version(tx, sync.family_id(), 1).await?;
    let value = if theirs.deleted {
        Value::Deleted
    } else {
        Value::Live(theirs.json)
    };
    segments::set_conflict(tx, d, at, value, theirs.json, theirs.commit_id, version).await
}

/// Drop the conflict row at this key, if `existing` says there is one.
pub(crate) async fn drop_conflict_row<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    sync: &SyncedRepo,
    at: At<'_>,
    existing: Option<&Row>,
) -> Result<()> {
    if existing.is_none() {
        return Ok(());
    }
    let d = DB::segs(tx, sync.worktree_id()).await?.draft;
    segments::drop_conflict(tx, d, at).await
}

/// Inputs for the write path's conflict detection, precomputed by
/// [`SyncedRepo::write_file`] when the file on disk holds an edit the
/// database never took in.
pub(crate) struct ConflictCheck {
    /// Each pending edit's base content, by (path, key).
    pub(crate) base_values: std::collections::HashMap<(String, String), Option<serde_json::Value>>,
}

/// One change [`apply_pending_records`] wants made to the conflict rows
/// of the file it just rendered.
pub(crate) struct ConflictOp {
    pub(crate) path: String,
    pub(crate) key: String,
    pub(crate) kind: ConflictOpKind,
}

/// What [`apply_conflict_ops_in_pool`] does to the row.
pub(crate) enum ConflictOpKind {
    /// Record (or refresh) the file's side of a divergence.
    Open {
        /// The file's value — or, when `deleted`, the one it dropped.
        json: serde_json::Value,
        /// The file no longer has this record.
        deleted: bool,
    },
    /// Drop the conflict row: its resolution has just been applied.
    Clear,
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
        let section_kind = crate::document::section_kind(format, &section_name);
        let theirs =
            crate::document::record_in(root, section_kind, &section_name, &rec.key).cloned();
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
                    out.ops.push(ConflictOp {
                        path: rec.path.clone(),
                        key: rec.key.clone(),
                        kind: ConflictOpKind::Clear,
                    });
                } else {
                    // The file changed under the resolution, so the
                    // decision was made about a value that is gone.
                    let kind = conflict_kind(rec.deleted, theirs.is_none(), base_commit.is_some());
                    tracing::warn!(
                        file = %file_path, path = %rec.path, key = %rec.key, kind = ?kind,
                        "file moved again since the conflict was resolved; re-opening it"
                    );
                    out.ops.push(ConflictOp {
                        path: rec.path.clone(),
                        key: rec.key.clone(),
                        kind: ConflictOpKind::Open {
                            // A file that dropped the record leaves no
                            // value to hold, so the last one it had
                            // stands in.
                            json: theirs.clone().unwrap_or_else(|| row.json.clone()),
                            deleted: theirs.is_none(),
                        },
                    });
                    out.conflicts
                        .push(report(kind, &base_commit, theirs.as_ref()));
                    continue;
                }
            }
            None => {
                if let Some(check) = check {
                    let base_value = check.base_values.get(&at).and_then(Option::as_ref);
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
                        out.ops.push(ConflictOp {
                            path: rec.path.clone(),
                            key: rec.key.clone(),
                            kind: ConflictOpKind::Open {
                                json: theirs
                                    .clone()
                                    .or_else(|| base_value.cloned())
                                    .unwrap_or_else(|| rec.json.clone()),
                                deleted: theirs.is_none(),
                            },
                        });
                        out.conflicts
                            .push(report(kind, &base_commit, theirs.as_ref()));
                        continue;
                    }
                }
            }
        }

        // Already what the file holds: nothing to write, and rendering an
        // untouched section would only restate its bytes.
        let holds = match &theirs {
            Some(value) => !rec.deleted && *value == rec.json,
            None => rec.deleted,
        };
        if holds {
            continue;
        }
        let root_obj = root.as_object_mut().expect("root is object");
        let (key, deleted) = (rec.key.clone(), rec.deleted);
        if rec.deleted {
            apply_delete(root_obj, section_kind, &section_name, &rec.key);
        } else {
            apply_insert(
                root_obj,
                section_kind,
                &section_name,
                rec.key,
                rec.json,
                format,
            );
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

/// Draft conflict rows of `file_path`, by (path, key).
pub(crate) async fn conflict_rows<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    sync: &SyncedRepo,
    file_path: &str,
) -> Result<std::collections::BTreeMap<(String, String), Row>> {
    let d = DB::segs(tx, sync.worktree_id()).await?.draft;
    Ok(DB::rows_in(tx, d, Some(file_path), true)
        .await?
        .into_iter()
        .map(|r| ((r.path.clone(), r.key.clone()), r))
        .collect())
}

/// Persist [`ConflictOp`]s in one transaction, drawing a version for
/// each row that actually moves (see [`refresh_conflict_row`]).
pub(crate) async fn apply_conflict_ops_in_pool<DB: Store>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    file_path: &str,
    commit_id: Option<&str>,
    ops: &[ConflictOp],
) -> Result<()> {
    let mut tx = pool.begin().await?;
    let existing = conflict_rows(&mut tx, sync, file_path).await?;
    for op in ops {
        let place = (op.path.clone(), op.key.clone());
        let at = At {
            file_path,
            path: &op.path,
            key: &op.key,
        };
        match &op.kind {
            ConflictOpKind::Open { json, deleted } => {
                refresh_conflict_row(
                    &mut tx,
                    sync,
                    at,
                    TheirSide {
                        json,
                        deleted: *deleted,
                        commit_id,
                    },
                    existing.get(&place),
                )
                .await?;
            }
            ConflictOpKind::Clear => {
                drop_conflict_row(&mut tx, sync, at, existing.get(&place)).await?;
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
pub(crate) async fn resolve_conflict_in_pool<DB: Store>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    file_path: &str,
    path: &str,
    key: &str,
    resolution: Resolution,
    expected_commit: Option<CommitRef>,
) -> Result<WriteOutcome> {
    let not_found = || Error::NotFound {
        file_path: file_path.to_string(),
        path: path.to_string(),
    };
    let w = sync.worktree_id();
    let at = At {
        file_path,
        path,
        key,
    };
    let mut tx = pool.begin().await?;
    let version = DB::next_version(&mut tx, sync.family_id(), 1).await?;
    let d = DB::segs(&mut tx, w).await?.draft;
    let theirs = segments::conflict_row(&mut tx, d, at)
        .await?
        .ok_or_else(not_found)?;
    // The draft's edit, tombstones included: an in-flight delete is a
    // side of the argument like any other.
    let ours = DB::rows_in(&mut tx, d, Some(file_path), false)
        .await?
        .into_iter()
        .find(|r| r.at() == at && r.is_edit())
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

    // `Ours` restates no value, so it cannot be checked against the file
    // now — it records the decision and the next write checks it.
    if resolution == Resolution::Ours {
        DB::set_conflict_state(&mut tx, theirs.id, ConflictState::Resolved, version).await?;
        tx.commit().await?;
        return Ok(WriteOutcome {
            id: theirs.key_id,
            version,
        });
    }
    segments::drop_conflict(&mut tx, d, at).await?;
    let key_id = match &resolution {
        // The edit is withdrawn, and the file's value stands: taken in
        // where the committed chain doesn't already hold it.
        Resolution::Theirs => {
            segments::remove_draft_row(&mut tx, d, at).await?;
            let committed = DB::visible(&mut tx, w, Scope::Chain, Filter::At(at))
                .await?
                .into_iter()
                .find(|r| !r.deleted);
            let shown = committed.as_ref().map(|r| &r.json);
            let file = (!theirs.deleted).then_some(&theirs.json);
            if shown != file {
                let value = match file {
                    Some(v) => Value::Live(v),
                    None => Value::Deleted,
                };
                // the file's value continues the record, unless git has it
                // at another place
                let elsewhere = DB::visible(&mut tx, w, Scope::Chain, Filter::KeyId(ours.key_id))
                    .await?
                    .iter()
                    .any(|r| !r.deleted && r.at() != at);
                let record = match &committed {
                    Some(r) => Some(r.key_id),
                    None if elsewhere => None,
                    None => Some(ours.key_id),
                };
                // taken in from the file, so not a pending edit: it keeps
                // a commit that holds the file
                let commit = match committed.as_ref().and_then(|r| r.commit_id.clone()) {
                    Some(c) => Some(c),
                    None => DB::head_commit(&mut tx, w).await?,
                };
                let written = segments::write(
                    &mut tx,
                    w,
                    at,
                    value,
                    Origin::File(commit.as_deref()),
                    record,
                    version,
                )
                .await?;
                written.key_id
            } else {
                committed.map_or(ours.key_id, |r| r.key_id)
            }
        }
        Resolution::Delete => {
            segments::write(
                &mut tx,
                w,
                at,
                Value::Deleted,
                Origin::Edit,
                Some(ours.key_id),
                version,
            )
            .await?
            .key_id
        }
        Resolution::Merged(json) => {
            let written = segments::write(
                &mut tx,
                w,
                at,
                Value::Live(json),
                Origin::Edit,
                Some(ours.key_id),
                version,
            )
            .await?;
            let format_owner = DB::file_format(&mut tx, w, file_path).await?;
            let aliases = compute_aliases(
                sync,
                format_owner.as_deref(),
                written.key_id,
                file_path,
                path,
                key,
                json,
            );
            DB::replace_aliases(&mut tx, written.id, &aliases).await?;
            written.key_id
        }
        Resolution::Ours => unreachable!("handled above"),
    };
    tx.commit().await?;
    Ok(WriteOutcome {
        id: key_id,
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
