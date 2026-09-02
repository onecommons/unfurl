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

    /// A literate markdown document could not be read or updated.
    /// `path` identifies it; `message` says what stood in the way.
    #[error("{path}: {message}")]
    Markdown {
        /// Path of the offending document.
        path: String,
        /// What could not be done, and why.
        message: String,
    },

    /// A merge was rejected — typically because the overlay
    /// specified `+%: error` on a key that exists in the base.
    #[error("merge rejected: {0}")]
    MergeRejected(String),
}

/// Convenience alias for `std::result::Result<T, MergeError>`.
pub type Result<T> = std::result::Result<T, MergeError>;
