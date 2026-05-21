// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Deep-merge engine with file-include directives.
//!
//! Rust port of `unfurl/merge.py`. Loads YAML into a [`Node`] tree
//! (which tracks the originating file on each mapping), merges trees
//! with `+`-prefixed directives, and converts the result into
//! [`serde_json::Value`] for the rest of the crate.
//!
//! This first cut covers loading and source-tracking; merging and
//! include resolution land in follow-up commits.

pub mod node;

pub use node::{load_file, Node, Source};
