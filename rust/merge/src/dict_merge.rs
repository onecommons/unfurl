// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Deep mapping merge with `+%` strategy directives.
//!
//! Rust port of `unfurl/merge.py::merge_dicts` and `_merge_lists`.
//! See [`merge`] / [`merge_with`] for the entry points.

use crate::node::{Node, Source};
use crate::{MergeError, Result};
use indexmap::IndexMap;

/// Key on a mapping that selects how it is merged with the
/// corresponding mapping in the base. Mirrors `mergeStrategyKey`
/// in `unfurl/merge.py`. Supported values:
///
/// - `"merge"` (default) — recursively deep-merge
/// - `"replace"` — overlay's mapping wins outright; base keys are
///   discarded
/// - `"whiteout"` — drop the key from the result
/// - `"nullout"` — set the key's value to `Null`
/// - `"error"` — refuse the merge with [`MergeError::MergeRejected`]
pub const MERGE_STRATEGY_KEY: &str = "+%";

/// Tunables for [`merge_with`].
///
/// `default_strategy` selects the fallback when neither side
/// declares `+%` on a key. `replace_keys` names keys whose
/// recursive merge should flip the default strategy to `"replace"`
/// for everything below — mirrors `merge.py::merge_dicts(replaceKeys=...)`.
/// Explicit `+%` on either side always overrides the default.
#[derive(Clone, Debug)]
pub struct MergeOptions {
    /// Keys whose subtree merges use `"replace"` as the recursive
    /// default strategy. Empty by default.
    pub replace_keys: Vec<String>,
    /// Strategy used when neither side specifies `+%` at a given
    /// key. Either `"merge"` (default) or `"replace"`.
    pub default_strategy: String,
}

impl Default for MergeOptions {
    fn default() -> Self {
        Self {
            replace_keys: Vec::new(),
            default_strategy: "merge".into(),
        }
    }
}

/// Deep-merge `overlay` into `base`, returning a new tree. Equivalent
/// to [`merge_with`] with [`MergeOptions::default`].
pub fn merge(base: &Node, overlay: &Node) -> Result<Node> {
    merge_with(base, overlay, &MergeOptions::default())
}

/// Deep-merge `overlay` into `base` honoring `opts`.
///
/// When both `base` and `overlay` are mappings, walks the overlay
/// first (entries land in overlay order), then appends any
/// base-only entries (in base order). Nested mappings recurse;
/// nested sequences use [`merge_list_append_unique_with`]. Anything
/// else: overlay wins.
///
/// Merged mappings inherit the base mapping's [`Source`], so error
/// diagnostics point at the file the base came from.
pub fn merge_with(base: &Node, overlay: &Node, opts: &MergeOptions) -> Result<Node> {
    let ctx = MergeCtx {
        replace_keys: &opts.replace_keys,
        default_strategy: &opts.default_strategy,
    };
    merge_ctx(base, overlay, &ctx)
}

/// Append-unique list merge ([`merge_list_append_unique_with`] with
/// default options).
pub fn merge_list_append_unique(base: &[Node], overlay: &[Node]) -> Result<Vec<Node>> {
    merge_list_append_unique_with(base, overlay, &MergeOptions::default())
}

/// Append-unique list merge that honors `opts` when nested mappings
/// inside list items are merged. Mirrors `merge.py::_merge_lists`
/// with `listStrategy == "append_unique"`.
///
/// For each overlay item:
/// - If the item is a mapping with `+%: merge`, merge it
///   positionally with `base[i]` (or append if past base's end).
/// - Else if the item is a single-key mapping, find a single-key
///   mapping in base with the same key and merge into it.
/// - Otherwise append the item only if no structurally-equal copy
///   is already in the base.
pub fn merge_list_append_unique_with(
    base: &[Node],
    overlay: &[Node],
    opts: &MergeOptions,
) -> Result<Vec<Node>> {
    let ctx = MergeCtx {
        replace_keys: &opts.replace_keys,
        default_strategy: &opts.default_strategy,
    };
    merge_list_append_unique_ctx(base, overlay, &ctx)
}

// ----------------------------------------------------------------------
// Internal: ctx-threaded recursion
// ----------------------------------------------------------------------

struct MergeCtx<'a> {
    replace_keys: &'a [String],
    default_strategy: &'a str,
}

impl<'a> MergeCtx<'a> {
    /// Strategy default to use when descending into `key`. If `key`
    /// is in `replace_keys`, the child default flips to `"replace"`;
    /// otherwise `"merge"`. This matches `merge.py:112-113`'s
    /// `childStrategy` computation.
    fn child_default(&self, key: &str) -> &'static str {
        if self.replace_keys.iter().any(|k| k == key) {
            "replace"
        } else {
            "merge"
        }
    }

    fn with_default(&'a self, default_strategy: &'a str) -> MergeCtx<'a> {
        MergeCtx {
            replace_keys: self.replace_keys,
            default_strategy,
        }
    }
}

fn merge_ctx(base: &Node, overlay: &Node, ctx: &MergeCtx<'_>) -> Result<Node> {
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
        merge_mappings(base_entries, overlay_entries, source.clone(), ctx)
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
    ctx: &MergeCtx<'_>,
) -> Result<Node> {
    let mut out: IndexMap<String, Node> = IndexMap::with_capacity(overlay.len() + base.len());
    let mut whited_out: Vec<String> = Vec::new();

    for (key, overlay_value) in overlay {
        if key == MERGE_STRATEGY_KEY {
            continue;
        }

        let child_default = ctx.child_default(key);
        let child_ctx = ctx.with_default(child_default);

        // Sequence-vs-sequence: delegate to list merger.
        if let Node::Sequence(overlay_items) = overlay_value {
            if let Some(Node::Sequence(base_items)) = base.get(key) {
                out.insert(
                    key.clone(),
                    Node::Sequence(merge_list_append_unique_ctx(
                        base_items,
                        overlay_items,
                        &child_ctx,
                    )?),
                );
                continue;
            }
            out.insert(key.clone(), overlay_value.clone());
            continue;
        }

        // Non-mapping value: overlay wins — *except* that Null
        // overlaid on a Mapping is treated as "no change" (matches
        // merge.py:117, :126 — Python's `isinstance(val, Mapping)
        // or val is None` followed by `if not val: continue`). To
        // actually replace a base mapping with Null, callers use
        // `+%: nullout`. Required by the test_expandDoc port.
        let Node::Mapping {
            entries: overlay_entries,
            ..
        } = overlay_value
        else {
            if matches!(overlay_value, Node::Null)
                && matches!(base.get(key), Some(Node::Mapping { .. }))
            {
                continue;
            }
            out.insert(key.clone(), overlay_value.clone());
            continue;
        };

        let strategy_from_overlay = overlay_entries
            .get(MERGE_STRATEGY_KEY)
            .and_then(node_as_str);

        // Both sides are mappings: resolve effective strategy and act on it.
        if let Some(Node::Mapping {
            entries: base_entries_inner,
            source: base_inner_src,
        }) = base.get(key)
        {
            let strategy = strategy_from_overlay
                .or_else(|| {
                    base_entries_inner
                        .get(MERGE_STRATEGY_KEY)
                        .and_then(node_as_str)
                })
                .unwrap_or(ctx.default_strategy);

            match strategy {
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
                        &child_ctx,
                    )?;
                    out.insert(key.clone(), merged);
                    continue;
                }
                "replace" => {
                    // Matches merge.py's fallthrough cp[key] = val:
                    // the overlay mapping is copied verbatim, +%
                    // directive included. A later expand-style pass
                    // (the Python `expand_doc` equivalent, not yet
                    // ported) is responsible for stripping consumed
                    // directives from the final result.
                    out.insert(key.clone(), overlay_value.clone());
                    continue;
                }
                "error" => {
                    return Err(MergeError::MergeRejected(format!(
                        "key {key:?} has +%: error set"
                    )));
                }
                _ => {
                    // whiteout / nullout / unknown: fall through to
                    // the post-mapping handling below.
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

fn merge_list_append_unique_ctx(
    base: &[Node],
    overlay: &[Node],
    ctx: &MergeCtx<'_>,
) -> Result<Vec<Node>> {
    let mut out: Vec<Node> = base.to_vec();
    for (i, item) in overlay.iter().enumerate() {
        if let Node::Mapping { entries, .. } = item {
            let strategy = entries.get(MERGE_STRATEGY_KEY).and_then(node_as_str);
            if strategy == Some("merge") {
                if i >= out.len() {
                    out.push(item.clone());
                } else if matches!(out[i], Node::Mapping { .. }) {
                    out[i] = merge_ctx(&out[i], item, ctx)?;
                } else {
                    out[i] = item.clone();
                }
                continue;
            }
            if entries.len() == 1 {
                let key = entries.keys().next().expect("len == 1");
                if let Some(idx) = find_single_key_map_in_list(&out, key) {
                    out[idx] = merge_ctx(&out[idx], item, ctx)?;
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

/// Build a single-key mapping `{"+%": value}` with the given source.
/// Used by [`diff`] to emit `whiteout` and `nullout` directives.
fn directive(value: &str, source: Source) -> Node {
    let mut entries = IndexMap::with_capacity(1);
    entries.insert(MERGE_STRATEGY_KEY.into(), Node::String(value.into()));
    Node::Mapping { entries, source }
}

// ----------------------------------------------------------------------
// diff / patch / intersect
// ----------------------------------------------------------------------

/// Reverse-engineer a diff such that `merge(old, diff) == new`.
///
/// Walks `old` first (preserving its key order), then appends keys
/// new added. Emits `+%: whiteout` for keys in `old` not in `new`,
/// and `+%: nullout` when `old[k]` is a mapping and `new[k]` is
/// `Null` (otherwise merge would treat `Null` as an empty mapping
/// and the value would survive). Mirrors `merge.py::diff_dicts`.
pub fn diff(old: &Node, new: &Node) -> Node {
    let (
        Node::Mapping {
            entries: old_entries,
            source,
        },
        Node::Mapping {
            entries: new_entries,
            ..
        },
    ) = (old, new)
    else {
        // Not both mappings — the smallest "diff" that merge can
        // apply to land on `new` is `new` itself.
        return new.clone();
    };
    diff_mappings(old_entries, new_entries, source.clone())
}

fn diff_mappings(
    old: &IndexMap<String, Node>,
    new: &IndexMap<String, Node>,
    source: Source,
) -> Node {
    let mut out: IndexMap<String, Node> = IndexMap::new();
    for (key, oldval) in old {
        if let Some(newval) = new.get(key) {
            if oldval != newval {
                let diff_val = match (oldval, newval) {
                    (Node::Mapping { .. }, Node::Mapping { .. }) => diff(oldval, newval),
                    (Node::Mapping { .. }, Node::Null) => directive("nullout", source.clone()),
                    _ => newval.clone(),
                };
                out.insert(key.clone(), diff_val);
            }
        } else {
            out.insert(key.clone(), directive("whiteout", source.clone()));
        }
    }
    for (key, newval) in new {
        if !old.contains_key(key) {
            out.insert(key.clone(), newval.clone());
        }
    }
    Node::Mapping {
        entries: out,
        source,
    }
}

/// Transform `old` into `new` structurally. Returns a new tree;
/// the input is not mutated.
///
/// When `preserve` is `false` (the Python default), keys present
/// in `old` but absent from `new` are dropped, and list values are
/// rewritten to match `new` exactly.
///
/// When `preserve` is `true`, `old`-only keys are kept untouched,
/// and list values become the union of old and new (old's items
/// followed by new items not already present).
///
/// Differences vs `merge.py::patch_dict`: Python's `patch_dict`
/// mutates `old` in place and (with `preserve=False`) tries to
/// preserve object identity by reusing `old`'s items in lists. In
/// Rust we work with owned `Node`s where identity is moot — the
/// `preserve=False` output is structurally identical to the Python
/// version's, but built fresh.
pub fn patch(old: &Node, new: &Node, preserve: bool) -> Node {
    let (
        Node::Mapping {
            entries: old_entries,
            source,
        },
        Node::Mapping {
            entries: new_entries,
            ..
        },
    ) = (old, new)
    else {
        return new.clone();
    };
    patch_mappings(old_entries, new_entries, source.clone(), preserve)
}

fn patch_mappings(
    old: &IndexMap<String, Node>,
    new: &IndexMap<String, Node>,
    source: Source,
    preserve: bool,
) -> Node {
    let mut out: IndexMap<String, Node> = IndexMap::with_capacity(old.len() + new.len());
    for (key, val) in old {
        if let Some(newval) = new.get(key) {
            if val == newval {
                out.insert(key.clone(), val.clone());
            } else {
                let patched = match (val, newval) {
                    (Node::Mapping { .. }, Node::Mapping { .. }) => patch(val, newval, preserve),
                    (Node::Sequence(old_items), Node::Sequence(new_items)) => {
                        if preserve {
                            let mut combined = old_items.clone();
                            for item in new_items {
                                if !combined.iter().any(|e| e == item) {
                                    combined.push(item.clone());
                                }
                            }
                            Node::Sequence(combined)
                        } else {
                            Node::Sequence(new_items.clone())
                        }
                    }
                    _ => newval.clone(),
                };
                out.insert(key.clone(), patched);
            }
        } else if preserve {
            out.insert(key.clone(), val.clone());
        }
        // else: drop the key (not in new, not preserving)
    }
    for (key, newval) in new {
        if !old.contains_key(key) {
            out.insert(key.clone(), newval.clone());
        }
    }
    Node::Mapping {
        entries: out,
        source,
    }
}

/// Keep only keys whose values match `new`. Recurses on
/// matched-but-different mapping pairs; drops the key otherwise.
/// Mirrors `merge.py::intersect_dict`.
pub fn intersect(old: &Node, new: &Node) -> Node {
    let (
        Node::Mapping {
            entries: old_entries,
            source,
        },
        Node::Mapping {
            entries: new_entries,
            ..
        },
    ) = (old, new)
    else {
        // Not both mappings — at top level, the "intersection" is
        // `old` unchanged (the caller's outer mapping handles the
        // drop decision when this is a nested value).
        return old.clone();
    };
    intersect_mappings(old_entries, new_entries, source.clone())
}

fn intersect_mappings(
    old: &IndexMap<String, Node>,
    new: &IndexMap<String, Node>,
    source: Source,
) -> Node {
    let mut out: IndexMap<String, Node> = IndexMap::new();
    for (key, val) in old {
        let Some(newval) = new.get(key) else {
            continue;
        };
        if val == newval {
            out.insert(key.clone(), val.clone());
        } else if matches!((val, newval), (Node::Mapping { .. }, Node::Mapping { .. })) {
            out.insert(key.clone(), intersect(val, newval));
        }
        // else: mismatched non-mapping — drop the key
    }
    Node::Mapping {
        entries: out,
        source,
    }
}
