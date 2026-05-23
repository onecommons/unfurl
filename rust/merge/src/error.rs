// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Crate-wide [`MergeError`] enum and [`Result`] alias.

/// Errors surfaced by the public API.
#[derive(Debug, thiserror::Error)]
pub enum MergeError {
    /// Filesystem I/O failed while reading an input or include file.
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    /// YAML parse failure. `path` identifies the offending file;
    /// `message` is the upstream parser diagnostic.
    #[error("yaml error in {path}: {message}")]
    Yaml {
        /// Path of the offending file.
        path: String,
        /// Upstream parser diagnostic.
        message: String,
    },
}

/// Convenience alias for `std::result::Result<T, MergeError>`.
pub type Result<T> = std::result::Result<T, MergeError>;
