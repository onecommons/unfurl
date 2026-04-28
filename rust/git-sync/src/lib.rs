// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Sync JSON and YAML records from a git working tree into SQL.
//!
//! The crate parses YAML and JSON files in a working tree, indexes
//! records into SQLite or Postgres via [`sqlx`](https://docs.rs/sqlx), lets callers mutate
//! those records via a CRUD API using git commits as optimistic-concurrency tokens,
//! writes the changes back to disk, and commits them through
//! [`gitoxide`](https://docs.rs/gix). Out-of-the-box support for the
//! cloudmap format ships in [`formats::cloudmap`].
//!
//! # Architecture
//!
//! - [`GitSync`] is the top-level handle, bundling a sqlx pool, the
//!   gix repository path, and a [`FormatRegistry`].
//! - [`DataFormat`] classifies parsed values, declares which top-level
//!   keys hold records, and exposes graph helpers used by
//!   [`GitSync::find_records_follow`].
//! - The shipped concrete format is [`CloudMapFormat`].
//!
//! # YAML round-trip
//!
//! Files are modified in place: [`GitSync::save_changes`] reads the
//! existing file from disk, applies in-flight record changes
//! (`commit_id IS NULL`) — set, replace, or delete — and writes the
//! result back. Untouched keys preserve their original ordering and
//! content. YAML parsing and serialization uses [`serde_saphyr`](https://docs.rs/serde_saphyr).
//!
//! # Backends
//!
//! SQLite is always compiled in. Postgres is opt-in via the
//! `postgres` cargo feature. Both backends can be linked together; the
//! [`Db`] enum dispatches at runtime. SQLite ≥ 3.45 is required for
//! its native JSONB type.
//!
//! # Example
//!
//! ```no_run
//! use unfurl_git_sync::{DbConfig, FormatRegistry, GitSync};
//!
//! # async fn run() -> unfurl_git_sync::Result<()> {
//! let sync = GitSync::open(
//!     "/path/to/working/dir",
//!     DbConfig::Sqlite { url: "sqlite::memory:".into() },
//!     FormatRegistry::with_builtins(),
//! )
//! .await?;
//!
//! sync.update_from_working_dir().await?;
//! let repos = sync.find_records(None, Some("/repositories".into()), None, false).await?;
//! # let _ = repos;
//! # Ok(())
//! # }
//! ```

#![deny(rust_2018_idioms)]
#![allow(clippy::too_many_arguments)]

pub mod db;
pub mod error;
pub mod format;
pub mod formats;
pub mod git;
pub mod model;
pub mod sync;
mod util;

#[doc(inline)]
pub use db::{Db, DbConfig};
#[doc(inline)]
pub use error::{Error, Result};
#[doc(inline)]
pub use format::{DataFormat, FormatRegistry};
#[doc(inline)]
pub use formats::cloudmap::CloudMapFormat;
#[doc(inline)]
pub use model::{Alias, File, Record, UpdateStats, WorkingDir, Worktree};
#[doc(inline)]
pub use sync::{CommitRef, GitSync};
