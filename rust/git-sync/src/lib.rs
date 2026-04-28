// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Sync JSON/YAML data tracked in a git repository into SQLite or
//! Postgres via gitoxide and sqlx.
//!
//! # Architecture
//!
//! - [`GitSync`] is the top-level handle. It owns a sqlx connection
//!   pool, the gix repository path, and a [`FormatRegistry`].
//! - The [`DataFormat`] trait classifies parsed values, declares which
//!   top-level keys hold individual records, and exposes graph helpers.
//! - The crate ships a single concrete format,
//!   [`CloudMapFormat`](formats::cloudmap::CloudMapFormat).
//!
//! # YAML round-trip
//!
//! Modified records

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

pub use db::{Db, DbConfig};
pub use error::{Error, Result};
pub use format::{DataFormat, FormatRegistry};
pub use formats::cloudmap::CloudMapFormat;
pub use model::{Alias, File, Record, UpdateStats, WorkingDir, Worktree};
pub use sync::{CommitRef, GitSync};
