// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Crate-wide [`Error`] enum and [`Result`] alias.
//!
//! Every public fallible operation in the crate returns
//! [`Result<T, Error>`](Result). The variants distinguish I/O,
//! database, git, and parser failures from semantic CRUD errors
//! (`NotFound`, `AlreadyExists`, `Conflict`).

use crate::CommitRef;

/// All errors surfaced by the public API.
///
/// Variants split into three groups:
///
/// 1. **Underlying-layer failures** wrap errors from the database
///    layer ([`Db`](Self::Db), [`Migrate`](Self::Migrate)), git
///    ([`Git`](Self::Git)), I/O ([`Io`](Self::Io)), or the document
///    parsers ([`Yaml`](Self::Yaml), [`Json`](Self::Json)).
/// 2. **Semantic CRUD failures**: [`NotFound`](Self::NotFound),
///    [`AlreadyExists`](Self::AlreadyExists), and
///    [`Conflict`](Self::Conflict) — the last is raised when the
///    optimistic-concurrency token supplied to a CRUD call doesn't
///    match the row's recorded commit.
/// 3. **Misuse**: [`UnknownFormat`](Self::UnknownFormat),
///    [`InvalidPointer`](Self::InvalidPointer), [`Other`](Self::Other).
///
/// `Conflict` always rolls back its enclosing transaction before being
/// returned, so the database is left untouched.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    /// A `sqlx` query, decode, or pool error.
    #[error("database error: {0}")]
    Db(#[from] sqlx::Error),
    /// A schema migration failed.
    #[error("migration error: {0}")]
    Migrate(#[from] sqlx::migrate::MigrateError),
    /// A `gitoxide` operation failed; the inner string is the
    /// upstream gix error formatted for display.
    #[error("git error: {0}")]
    Git(String),
    /// Filesystem I/O failed (typically while reading or writing
    /// tracked files inside the working tree).
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    /// YAML parse or emit failure. `path` identifies the offending
    /// file; `message` is the upstream parser/emitter diagnostic.
    #[error("yaml error in {path}: {message}")]
    Yaml {
        /// Working-tree-relative path of the offending file.
        path: String,
        /// Upstream parser / emitter diagnostic.
        message: String,
    },
    /// JSON parse failure. `path` identifies the offending file;
    /// `source` is the underlying [`serde_json::Error`].
    #[error("json parse error in {path}: {source}")]
    Json {
        /// Working-tree-relative path of the offending file.
        path: String,
        /// Underlying serde_json parse error.
        #[source]
        source: serde_json::Error,
    },
    /// A caller asked for a [`crate::DataFormat`] that isn't
    /// registered in the [`crate::FormatRegistry`].
    #[error("unknown format: {0}")]
    UnknownFormat(String),
    /// A JSON-pointer string couldn't be parsed.
    #[error("invalid JSON pointer: {0}")]
    InvalidPointer(String),
    /// A CRUD call targets a record that doesn't exist (or is a
    /// tombstoned row, which surfaces as not-present).
    #[error("record not found: {file_path}#{path}")]
    NotFound {
        /// Working-tree-relative path of the file the record lives in.
        file_path: String,
        /// Parent JSON-pointer the record was looked up under.
        path: String,
    },
    /// `create_record` was called for a `(file_path, path, key)` that
    /// is already present and live (not tombstoned).
    #[error("record already exists: {file_path}#{path}")]
    AlreadyExists {
        /// Working-tree-relative path of the file.
        file_path: String,
        /// Parent JSON-pointer the conflicting record sits under.
        path: String,
    },
    /// Optimistic-concurrency check failed; the enclosing transaction
    /// was rolled back before this error was returned.
    ///
    /// `expected` is the [`CommitRef`] the caller passed; `actual` is
    /// the row's current `commit_id` (`None` when the row is pending
    /// or absent).
    #[error("commit conflict at {file_path}#{path}: expected {expected:?}, actual {actual:?}")]
    Conflict {
        /// Working-tree-relative path of the file.
        file_path: String,
        /// Parent JSON-pointer the row sits under.
        path: String,
        /// Token supplied by the caller.
        expected: CommitRef,
        /// Row's current `commit_id` (or `None` if pending / absent).
        actual: Option<String>,
    },
    /// The file on disk is not the one this worktree's records were
    /// parsed from, so the database's picture of it is out of date.
    ///
    /// Raised before a write rather than after, because a write renders
    /// from the database: proceeding would drop whatever the edit added
    /// and silently replace it with rows that predate it. Re-run
    /// [`crate::SyncedRepo::update_from_working_dir`] to take the file's
    /// current contents in — it preserves in-flight record edits and
    /// reports where the two sides disagree
    /// ([`crate::UpdateStats::conflicts`]) — then retry: the rewrite
    /// applies the pending edits on top of what the file now says.
    #[error("{file_path} changed outside this database: parsed from {expected}, found {actual}")]
    FileChanged {
        /// Working-tree-relative path of the file.
        file_path: String,
        /// Blob OID the records were parsed from.
        expected: String,
        /// Blob OID of what is on disk now.
        actual: String,
    },
    /// Catch-all for failure cases that don't fit the variants above.
    #[error("{0}")]
    Other(String),
}

/// Convenience alias for `std::result::Result<T, Error>`.
pub type Result<T> = std::result::Result<T, Error>;
