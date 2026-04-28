// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Pluggable data format trait + registry.

use crate::Record;

/// A pluggable JSON/YAML-on-disk format.
///
/// Implementations classify a parsed value, declare which top-level keys
/// hold individual records, and expose graph-walking helpers used by
/// callers that need aliases or follow-pointers.
pub trait DataFormat: Send + Sync {
    /// Format key (e.g. `"cloudmap"`).
    fn name(&self) -> &str;

    /// Quick predicate to decide whether a parsed JSON value belongs to
    /// this format.
    fn is_format(&self, json: &serde_json::Value) -> bool;

    /// Top-level keys whose direct children are individual records.
    fn path_prefixes(&self) -> &[&str];

    /// Alternative `(path, key)` pairs under which this record should
    /// also be findable. Used to populate the `alias` table. `path` is
    /// the parent JSON-pointer; `key` is the unescaped alias key.
    fn find_alias(&self, record: &Record) -> Vec<(String, String)>;

    /// `(path, key)` pairs (within the same file or worktree) of
    /// records this record references. Used by
    /// `find_records(.., follow=true)`.
    fn follow(&self, record: &Record) -> Vec<(String, String)>;
}

/// Registry of [`DataFormat`] implementations. Owns the boxed trait
/// objects.
#[derive(Default)]
pub struct FormatRegistry {
    formats: Vec<Box<dyn DataFormat>>,
}

impl FormatRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Return a registry pre-loaded with every format the crate ships.
    /// Currently that's just [`crate::CloudMapFormat`].
    pub fn with_builtins() -> Self {
        let mut r = Self::default();
        r.register(crate::CloudMapFormat);
        r
    }

    pub fn register<F: DataFormat + 'static>(&mut self, fmt: F) {
        self.formats.push(Box::new(fmt));
    }

    /// Return the first registered format whose `is_format` accepts the
    /// supplied value.
    pub fn detect(&self, json: &serde_json::Value) -> Option<&dyn DataFormat> {
        self.formats
            .iter()
            .find(|f| f.is_format(json))
            .map(|b| b.as_ref())
    }

    /// Look up a format by name.
    pub fn by_name(&self, name: &str) -> Option<&dyn DataFormat> {
        self.formats
            .iter()
            .find(|f| f.name() == name)
            .map(|b| b.as_ref())
    }
}
