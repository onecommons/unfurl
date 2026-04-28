// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Plain-old-data structs returned from the database.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Worktree {
    pub id: i64,
    pub origin: String,
    pub branch: String,
    pub commit_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct File {
    pub worktree_id: i64,
    pub path: String,
    pub format: String,
    pub commit_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Record {
    pub id: i64,
    pub worktree_id: i64,
    pub file_path: String,
    /// JSON-pointer to the parent map this record lives under (e.g.
    /// `/repositories`). The record sits at `obj[path][key]` in the
    /// on-disk file.
    pub path: String,
    /// Unescaped key under [`Record::path`] Stored verbatim — no
    /// JSON-pointer escaping.
    pub key: String,
    pub commit_id: Option<String>,
    pub json: serde_json::Value,
    /// Tombstone flag. A row with `deleted = true` AND `commit_id IS
    /// NULL` indicates an in-flight delete that has not yet been
    /// committed. CRUD reads (`get_record` / `find_records`) hide
    /// tombstones; only `get_record_by_id` returns them.
    pub deleted: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Alias {
    pub record_id: i64,
    /// Parent JSON-pointer of the alias.
    pub path: String,
    /// Unescaped alias key.
    pub key: String,
}

/// Snapshot of the gix working tree this `GitSync` is bound to.
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

/// Result of [`crate::GitSync::update_from_repository`].
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct UpdateStats {
    pub files_seen: usize,
    pub files_updated: usize,
    pub records_upserted: usize,
    pub records_deleted: usize,
}
