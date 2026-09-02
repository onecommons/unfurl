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
//! [`markdown`] reads the same data out of prose: a document whose YAML
//! lives in fenced code blocks, merged into one value, and written back
//! into the blocks it came from. [`template`] is the byte-level splicer
//! both of those rest on — it replaces the spans that changed and copies
//! everything else through, which is how comments survive an edit.

#![deny(rust_2018_idioms)]

pub mod dict_merge;
pub mod error;
pub mod expand;
pub mod include;
pub mod markdown;
pub mod node;
pub mod template;
pub mod util;

#[doc(inline)]
pub use dict_merge::{
    diff, intersect, merge, merge_list_append_unique, merge_list_append_unique_with, merge_with,
    patch, MergeOptions, MERGE_STRATEGY_KEY,
};
#[doc(inline)]
pub use error::{MergeError, Result};
#[doc(inline)]
pub use expand::{
    expand, expand_bytes, expand_file, expand_text, expand_with, IncludeEntry, Includes,
};
#[doc(inline)]
pub use include::{
    find_template, lookup_path, parse_merge_key, FileResolver, IncludeResolver, IncludeTarget,
    MergeKey, NullResolver,
};
#[doc(inline)]
pub use markdown::{extract_bytes, extract_file, Applied, Markdown};
#[doc(inline)]
pub use node::{load_file, load_text, Node, Source};
#[doc(inline)]
pub use template::Template;
