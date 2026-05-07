// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Plain-old-data structs returned from the database.
//!
//! Each struct mirrors one row of its corresponding SQL table, plus
//! [`WorkingDir`] (a derived view over the gix repo) and
//! [`UpdateStats`] (a return value).

use serde::{Deserialize, Serialize};

/// One row of the `worktree` table — a `(origin, branch)` pair the
/// crate has indexed.
///
/// `commit_id` is the most recent HEAD oid observed on the branch; it
/// advances whenever [`crate::SyncedRepo::update_from_working_dir`] or
/// [`crate::SyncedRepo::commit_repository`] runs.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Worktree {
    /// Auto-assigned primary key.
    pub id: i64,
    /// Remote URL with scheme stripped (or the working-tree path when
    /// no remote is configured).
    pub origin: String,
    /// Short branch name, e.g. `main`.
    pub branch: String,
    /// Last HEAD oid the crate has observed for this `(origin, branch)`.
    pub commit_id: Option<String>,
    /// Working-tree-relative path of the file new records go to when a
    /// CRUD call passes `file_path = None`. Set on the first
    /// [`crate::SyncedRepo::update_from_working_dir`] run; never
    /// overwritten afterwards (operators can pin it manually).
    pub default_file_path: Option<String>,
}

/// One row of the `file` table — a tracked file within a worktree.
///
/// `format` is the [`crate::DataFormat::name`] that classified the
/// file's contents on the most recent
/// [`crate::SyncedRepo::update_from_working_dir`]; `commit_id` is the
/// last-known commit that touched this path.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct File {
    /// Foreign key into [`Worktree`].
    pub worktree_id: i64,
    /// Working-tree-relative path of the file.
    pub path: String,
    /// Name of the [`crate::DataFormat`] that classified this file.
    pub format: String,
    /// Last commit oid known to have touched this path.
    pub commit_id: Option<String>,
}

/// One row of the `record` table — a single extracted JSON value.
///
/// Records sit at `obj[path][key]` inside their owning file, where
/// `path` is the JSON-pointer to the parent map (e.g. `/repositories`)
/// and `key` is the literal map key. `commit_id == None` indicates an
/// in-flight edit that hasn't been committed yet.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Record {
    /// Auto-assigned primary key.
    pub id: i64,
    /// Foreign key into [`Worktree`].
    pub worktree_id: i64,
    /// Working-tree-relative path of the file this record came from.
    pub file_path: String,
    /// JSON-pointer to the parent map this record lives under (e.g.
    /// `/repositories`).
    pub path: String,
    /// Unescaped key under [`Record::path`]; stored verbatim — no
    /// JSON-pointer escaping.
    pub key: String,
    /// Last commit oid that committed this record's value, or `None`
    /// when the record is in-flight (uncommitted).
    pub commit_id: Option<String>,
    /// The record's JSON payload.
    pub json: serde_json::Value,
    /// Tombstone flag.
    ///
    /// A row with `deleted == true` AND `commit_id == None` is an
    /// in-flight delete waiting for the next
    /// [`crate::SyncedRepo::commit_repository`] to purge it.
    /// [`crate::SyncedRepo::get_record`] and
    /// [`crate::SyncedRepo::find_records`] hide tombstones; only
    /// [`crate::SyncedRepo::get_record_by_id`] returns them.
    pub deleted: bool,
    /// Monotonic per-worktree version. Bumped on every CRUD write and
    /// preserved across commit roll-forward, so it doubles as both the
    /// optimistic-concurrency token (see
    /// [`crate::CommitRef::Pending`]) and a cursor for
    /// [`crate::SyncedRepo::list_changes`].
    pub version: i64,
}

/// One row of the `alias` table — an alternate `(path, key)` lookup
/// pointing at a record.
///
/// Aliases let callers find a record by a synonym (e.g. a versioned
/// URL) via [`crate::SyncedRepo::find_records`] with `alias = true`.
/// They are populated by [`crate::DataFormat::find_alias`].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Alias {
    /// Foreign key into [`Record`].
    pub record_id: i64,
    /// Parent JSON-pointer of the alias.
    pub path: String,
    /// Unescaped alias key.
    pub key: String,
}

/// Snapshot of the gix working tree this [`crate::SyncedRepo`] is bound to.
///
/// Returned by [`crate::SyncedRepo::get_working_dir`]. `head_commit` is
/// `None` for an empty / unborn repository.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkingDir {
    /// Absolute filesystem path to the working directory.
    pub repo_path: std::path::PathBuf,
    /// Branch name (e.g. `main`), or `HEAD` for a detached HEAD.
    pub branch: String,
    /// Current HEAD commit oid as a hex string. `None` for an unborn
    /// or empty repository.
    pub head_commit: Option<String>,
}

/// Result of a CRUD write
/// ([`crate::SyncedRepo::create_record`] /
/// [`crate::SyncedRepo::update_record`] /
/// [`crate::SyncedRepo::upsert_record`] /
/// [`crate::SyncedRepo::delete_record`]).
///
/// `version` is the worktree-scoped monotonic counter stamped on this
/// write — pass it back as a [`crate::CommitRef::Pending`] token on the
/// next request to scope the optimistic-concurrency check.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct WriteOutcome {
    /// Primary key of the affected `record` row. For `delete_record`,
    /// this is the id of the row that was tombstoned.
    pub id: i64,
    /// Worktree-scoped monotonic version stamped on this write.
    pub version: i64,
}

/// Counters returned by [`crate::SyncedRepo::update_from_working_dir`].
///
/// `files_updated` ≤ `files_seen`; `records_upserted` and
/// `records_deleted` are totals across the whole sync pass.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct UpdateStats {
    /// Tracked files visited by the sync pass.
    pub files_seen: usize,
    /// Files whose records were re-extracted into the database.
    pub files_updated: usize,
    /// Total records inserted or refreshed in this pass.
    pub records_upserted: usize,
    /// Records hard-deleted because they disappeared from disk.
    pub records_deleted: usize,
}

/// One operation in a batch passed to
/// [`crate::SyncedRepo::apply_batch`].
#[derive(Debug, Clone)]
pub enum BatchOp {
    /// Insert-or-update — same semantics as
    /// [`crate::SyncedRepo::upsert_record`].
    Upsert {
        /// Effective file path; `None` falls back to the existing
        /// record's file then the worktree's `default_file_path`.
        file_path: Option<String>,
        /// Parent JSON-pointer the record sits under.
        path: String,
        /// Record key.
        key: String,
        /// Record payload.
        json: serde_json::Value,
        /// Optional OCC token gating the write.
        expected: Option<crate::CommitRef>,
    },
    /// Tombstone — same semantics as
    /// [`crate::SyncedRepo::delete_record`].
    Delete {
        /// Effective file path; `None` falls back to the existing
        /// record's file (deletes have no default-path fallback).
        file_path: Option<String>,
        /// Parent JSON-pointer.
        path: String,
        /// Record key.
        key: String,
        /// Optional OCC token gating the delete.
        expected: Option<crate::CommitRef>,
    },
}

impl BatchOp {
    /// Parent JSON-pointer this op targets.
    pub fn path(&self) -> &str {
        match self {
            BatchOp::Upsert { path, .. } | BatchOp::Delete { path, .. } => path,
        }
    }
    /// Record key this op targets.
    pub fn key(&self) -> &str {
        match self {
            BatchOp::Upsert { key, .. } | BatchOp::Delete { key, .. } => key,
        }
    }
}

/// A single [`BatchOp`] that landed successfully.
#[derive(Debug, Clone)]
pub struct Applied {
    /// Index of the op in the original batch.
    pub index: usize,
    /// Op's parent JSON-pointer.
    pub path: String,
    /// Op's record key.
    pub key: String,
    /// `(id, version)` stamped on the row.
    pub outcome: WriteOutcome,
}

/// A single [`BatchOp`] that did not land.
///
/// In atomic mode, a populated `failed` always means the whole batch
/// was rolled back (so [`BatchOutcome::applied`] is empty). In
/// non-atomic mode, ``failed`` and ``applied`` may both be non-empty:
/// the failed records were skipped, the others committed.
#[derive(Debug)]
pub struct Failed {
    /// Index of the op in the original batch.
    pub index: usize,
    /// Op's parent JSON-pointer.
    pub path: String,
    /// Op's record key.
    pub key: String,
    /// The error raised when applying this op.
    pub error: crate::Error,
}

/// Result of [`crate::SyncedRepo::apply_batch`].
#[derive(Debug, Default)]
pub struct BatchOutcome {
    /// Records successfully applied (committed to the database).
    pub applied: Vec<Applied>,
    /// Records that were skipped.
    pub failed: Vec<Failed>,
    /// Largest `version` stamped during this batch, or ``None`` when
    /// nothing was applied.
    pub last_version: Option<i64>,
}
