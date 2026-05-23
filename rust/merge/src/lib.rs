// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Deep-merge engine for YAML/JSON with file-include directives and
//! source provenance.
//!
//! Rust port of `unfurl/merge.py`. Loads YAML into a [`Node`] tree
//! that attaches a [`Source`] (file + line + col) to every mapping,
//! merges trees with `+`-prefixed directives, and (later) resolves
//! `+include:` references against a caller-supplied
//! `IncludeResolver`. Conversion to [`serde_json::Value`] and typed
//! extraction via [`Node::deserialize_into`] cover the storage and
//! consumer paths.
//!
//! This crate has no git, SQL, or async runtime dependencies — just
//! `saphyr`, `indexmap`, `serde`, `serde_json`, and `thiserror` —
//! so it can be embedded in pyo3 extensions or other pure-data
//! tooling without dragging in a backend stack.

#![deny(rust_2018_idioms)]

pub mod dict_merge;
pub mod error;
pub mod include;
pub mod node;

#[doc(inline)]
pub use dict_merge::{
    diff, intersect, merge, merge_list_append_unique, merge_list_append_unique_with, merge_with,
    patch, MergeOptions, MERGE_STRATEGY_KEY,
};
#[doc(inline)]
pub use error::{MergeError, Result};
#[doc(inline)]
pub use include::{
    find_template, lookup_path, parse_merge_key, FileResolver, IncludeResolver, MergeKey,
    NullResolver,
};
#[doc(inline)]
pub use node::{load_file, Node, Source};
