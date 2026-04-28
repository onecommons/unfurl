// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Transaction-scoped helpers used by the CRUD primitives in `sync`.
//!
//! Each dialect lives in its own submodule because the helpers take
//! `&mut sqlx::Transaction<'_, DB>` and the SQLite/Postgres `Transaction`
//! types are not interchangeable. `sqlx::Any` is intentionally avoided
//! (see `db::Db` for context).

pub mod sqlite;

#[cfg(feature = "postgres")]
pub mod pg;

/// Lookup result from each dialect's `lookup_commits`. Tombstones surface
/// as if absent: the `record_id` and `record_commit` fields are `None`,
/// and the conflict checker falls back to `file_commit`.
pub(crate) struct RecordLookup {
    /// Live record id; `None` when the row is absent or a tombstone.
    pub(crate) record_id: Option<i64>,
    /// Live row's `commit_id`; `None` when absent or a tombstone.
    pub(crate) record_commit: Option<String>,
    /// File row's `commit_id` (used as a fallback in the conflict
    /// check when `record_id.is_none()`).
    pub(crate) file_commit: Option<String>,
}
