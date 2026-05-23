// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Deep mapping merge with `+%` strategy directives.
//!
//! Rust port of `unfurl/merge.py::merge_dicts` and `_merge_lists`.
//! See [`merge`] for the entry point.

use crate::node::{Node, Source};
use crate::{MergeError, Result};
use indexmap::IndexMap;

/// Key on a mapping that selects how it is merged with the
/// corresponding mapping in the base. Mirrors `mergeStrategyKey`
/// in `unfurl/merge.py`. Supported values:
///
/// - `"merge"` (default) — recursively deep-merge
/// - `"whiteout"` — drop the key from the result
/// - `"nullout"` — set the key's value to `Null`
/// - `"error"` — refuse the merge with [`MergeError::MergeRejected`]
pub const MERGE_STRATEGY_KEY: &str = "+%";

/// Deep-merge `overlay` into `base`, returning a new tree.
///
/// When both `base` and `overlay` are mappings, walks the overlay
/// first (entries land in overlay order), then appends any
/// base-only entries (in base order). Nested mappings recurse;
/// nested sequences use [`merge_list_append_unique`]. Anything
/// else: overlay wins.
///
/// Merged mappings inherit the base's [`Source`], so error
/// diagnostics point at the file the base came from.
pub fn merge(base: &Node, overlay: &Node) -> Result<Node> {
    if let (
        Node::Mapping {
            entries: base_entries,
            source,
        },
        Node::Mapping {
            entries: overlay_entries,
            ..
        },
    ) = (base, overlay)
    {
        merge_mappings(base_entries, overlay_entries, source.clone())
    } else {
        // Not both mappings — overlay wins, matching merge.py's
        // "otherwise a replaces b" fallthrough at the leaf level.
        Ok(overlay.clone())
    }
}

fn merge_mappings(
    base: &IndexMap<String, Node>,
    overlay: &IndexMap<String, Node>,
    source: Source,
) -> Result<Node> {
    let mut out: IndexMap<String, Node> = IndexMap::with_capacity(overlay.len() + base.len());
    let mut whited_out: Vec<String> = Vec::new();

    for (key, overlay_value) in overlay {
        if key == MERGE_STRATEGY_KEY {
            continue;
        }

        // Sequence-vs-sequence: delegate to list merger.
        if let Node::Sequence(overlay_items) = overlay_value {
            if let Some(Node::Sequence(base_items)) = base.get(key) {
                out.insert(
                    key.clone(),
                    Node::Sequence(merge_list_append_unique(base_items, overlay_items)?),
                );
                continue;
            }
            out.insert(key.clone(), overlay_value.clone());
            continue;
        }

        // Anything that's not a mapping: overlay wins.
        let Node::Mapping {
            entries: overlay_entries,
            ..
        } = overlay_value
        else {
            out.insert(key.clone(), overlay_value.clone());
            continue;
        };

        let strategy_from_overlay = overlay_entries
            .get(MERGE_STRATEGY_KEY)
            .and_then(node_as_str);

        // Both sides are mappings: try recursive merge per strategy.
        if let Some(Node::Mapping {
            entries: base_entries_inner,
            source: base_inner_src,
        }) = base.get(key)
        {
            let strategy = strategy_from_overlay.or_else(|| {
                base_entries_inner
                    .get(MERGE_STRATEGY_KEY)
                    .and_then(node_as_str)
            });
            match strategy.unwrap_or("merge") {
                "merge" => {
                    // Empty overlay mapping = treat as missing key,
                    // matches merge.py:126.
                    if overlay_entries.is_empty() {
                        continue;
                    }
                    let merged = merge_mappings(
                        base_entries_inner,
                        overlay_entries,
                        base_inner_src.clone(),
                    )?;
                    out.insert(key.clone(), merged);
                    continue;
                }
                "error" => {
                    return Err(MergeError::MergeRejected(format!(
                        "key {key:?} has +%: error set"
                    )));
                }
                _ => {
                    // whiteout / nullout / unknown: fall through to
                    // the post-mapping handling below so they apply
                    // even when both sides are mappings.
                }
            }
        }

        // Strategy handling outside the both-mappings branch.
        match strategy_from_overlay {
            Some("whiteout") => {
                whited_out.push(key.clone());
            }
            Some("nullout") => {
                out.insert(key.clone(), Node::Null);
            }
            _ => {
                // Overlay's mapping wins outright.
                out.insert(key.clone(), overlay_value.clone());
            }
        }
    }

    // Pass 2: fill in base-only keys, preserving base order.
    for (key, base_value) in base {
        if key == MERGE_STRATEGY_KEY {
            continue;
        }
        if !out.contains_key(key) && !whited_out.iter().any(|w| w == key) {
            out.insert(key.clone(), base_value.clone());
        }
    }

    Ok(Node::Mapping {
        entries: out,
        source,
    })
}

/// Merge two sequences using append-unique semantics (`unfurl/
/// merge.py::_merge_lists` with `listStrategy == "append_unique"`).
///
/// For each overlay item:
/// - If the item is a mapping with `+%: merge`, merge it
///   positionally with `base[i]` (or append if past base's end).
/// - Else if the item is a single-key mapping, find a single-key
///   mapping in base with the same key and merge into it.
/// - Otherwise append the item only if no structurally-equal copy
///   is already in the base.
pub fn merge_list_append_unique(base: &[Node], overlay: &[Node]) -> Result<Vec<Node>> {
    let mut out: Vec<Node> = base.to_vec();
    for (i, item) in overlay.iter().enumerate() {
        if let Node::Mapping { entries, .. } = item {
            let strategy = entries.get(MERGE_STRATEGY_KEY).and_then(node_as_str);
            if strategy == Some("merge") {
                if i >= out.len() {
                    out.push(item.clone());
                } else if matches!(out[i], Node::Mapping { .. }) {
                    out[i] = merge(&out[i], item)?;
                } else {
                    out[i] = item.clone();
                }
                continue;
            }
            if entries.len() == 1 {
                let key = entries.keys().next().expect("len == 1");
                if let Some(idx) = find_single_key_map_in_list(&out, key) {
                    out[idx] = merge(&out[idx], item)?;
                    continue;
                }
            }
        }
        if !out.iter().any(|existing| existing == item) {
            out.push(item.clone());
        }
    }
    Ok(out)
}

fn find_single_key_map_in_list(seq: &[Node], key: &str) -> Option<usize> {
    seq.iter().position(|item| {
        matches!(item, Node::Mapping { entries, .. } if entries.len() == 1 && entries.contains_key(key))
    })
}

fn node_as_str(n: &Node) -> Option<&str> {
    if let Node::String(s) = n {
        Some(s.as_str())
    } else {
        None
    }
}
