// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Error type and `Result` alias for the crate.

use crate::CommitRef;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("database error: {0}")]
    Db(#[from] sqlx::Error),
    #[error("migration error: {0}")]
    Migrate(#[from] sqlx::migrate::MigrateError),
    #[error("git error: {0}")]
    Git(String),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("yaml error in {path}: {message}")]
    Yaml { path: String, message: String },
    #[error("json parse error in {path}: {source}")]
    Json {
        path: String,
        #[source]
        source: serde_json::Error,
    },
    #[error("unknown format: {0}")]
    UnknownFormat(String),
    #[error("invalid JSON pointer: {0}")]
    InvalidPointer(String),
    #[error("record not found: {file_path}#{path}")]
    NotFound { file_path: String, path: String },
    #[error("record already exists: {file_path}#{path}")]
    AlreadyExists { file_path: String, path: String },
    /// Optimistic-concurrency check failed; transaction was rolled back.
    #[error("commit conflict at {file_path}#{path}: expected {expected:?}, actual {actual:?}")]
    Conflict {
        file_path: String,
        path: String,
        expected: CommitRef,
        actual: Option<String>,
    },
    #[error("{0}")]
    Other(String),
}

pub type Result<T> = std::result::Result<T, Error>;
