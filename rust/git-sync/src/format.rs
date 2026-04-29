// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Pluggable [`DataFormat`] trait and its [`FormatRegistry`].
//!
//! A `DataFormat` describes one kind of YAML/JSON document: how to
//! recognise it, where its records live, and how records cross-reference
//! each other. The crate ships [`crate::CloudMapFormat`]; callers can
//! plug in additional implementations.

use crate::Record;

/// Defines one JSON/YAML on-disk format the crate can index.
///
/// Implementations classify a parsed value (`is_format`), declare which
/// top-level keys hold individual records (`path_prefixes`), and expose
/// graph helpers used by [`crate::SyncedRepo::find_records`] (`find_alias`)
/// and [`crate::SyncedRepo::find_records_follow`] (`follow`). Implementors
/// must be `Send + Sync` so a registry can be shared across tasks.
pub trait DataFormat: Send + Sync {
    /// The format's unique name (e.g. `"cloudmap"`).
    ///
    /// Stored in the `file.format` column and looked up via
    /// [`FormatRegistry::by_name`].
    fn name(&self) -> &str;

    /// Returns `true` if the supplied parsed value matches this format.
    ///
    /// Used by [`FormatRegistry::detect`] to classify newly-seen files.
    /// Implementations typically inspect a `kind` or `apiVersion`
    /// header.
    fn is_format(&self, json: &serde_json::Value) -> bool;

    /// Lists the top-level keys whose direct children are individual
    /// records.
    ///
    /// During [`crate::SyncedRepo::update_from_working_dir`] each entry
    /// `(prefix, key, value)` under `value[prefix]` becomes one
    /// [`Record`] with `path = "/{prefix}"` and the literal map `key`.
    fn path_prefixes(&self) -> &[&str];

    /// Returns alternate `(path, key)` lookups that should resolve to
    /// `record`.
    ///
    /// Used to populate the `alias` table during
    /// [`crate::SyncedRepo::update_from_working_dir`]. `path` is the
    /// parent JSON-pointer (e.g. `/repositories`); `key` is the
    /// unescaped alias key. Returning an empty `Vec` means the record
    /// has no aliases.
    fn find_alias(&self, record: &Record) -> Vec<(String, String)>;

    /// Returns the keys this record points at.
    ///
    /// Used by [`crate::SyncedRepo::find_records_follow`] to walk
    /// outgoing edges; each returned key is matched against
    /// `record.key` and (alias-resolved) `alias.key` across every
    /// record in the worktree. Implementations should only return
    /// URL-shaped (or otherwise resolvable) references; label-only
    /// references should be skipped.
    fn follow(&self, record: &Record) -> Vec<String>;
}

/// Registry of [`DataFormat`] implementations.
///
/// Owns its boxed formats and is consumed (by value) when constructing
/// a [`crate::SyncedRepo`]. Iteration order is registration order; the
/// first format whose [`DataFormat::is_format`] accepts a value wins
/// the [`detect`](Self::detect) call.
#[derive(Default)]
pub struct FormatRegistry {
    formats: Vec<Box<dyn DataFormat>>,
}

impl FormatRegistry {
    /// Create an empty registry.
    pub fn new() -> Self {
        Self::default()
    }

    /// Create a registry pre-loaded with every format the crate ships.
    ///
    /// Currently that's just [`crate::CloudMapFormat`].
    pub fn with_builtins() -> Self {
        let mut r = Self::default();
        r.register(crate::CloudMapFormat);
        r
    }

    /// Register an additional [`DataFormat`] implementation.
    ///
    /// Order matters for [`detect`](Self::detect): formats registered
    /// earlier are tried first.
    pub fn register<F: DataFormat + 'static>(&mut self, fmt: F) {
        self.formats.push(Box::new(fmt));
    }

    /// Return the first registered format whose
    /// [`DataFormat::is_format`] accepts `json`, or `None` if no
    /// format matches.
    pub fn detect(&self, json: &serde_json::Value) -> Option<&dyn DataFormat> {
        self.formats
            .iter()
            .find(|f| f.is_format(json))
            .map(|b| b.as_ref())
    }

    /// Look up a format by its [`DataFormat::name`], or `None` if no
    /// format with that name is registered.
    pub fn by_name(&self, name: &str) -> Option<&dyn DataFormat> {
        self.formats
            .iter()
            .find(|f| f.name() == name)
            .map(|b| b.as_ref())
    }
}
