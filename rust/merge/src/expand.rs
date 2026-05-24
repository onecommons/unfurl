// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Document expansion: resolve `+`-prefixed include directives and
//! fold their templates into the surrounding map.
//!
//! Rust port of `unfurl/merge.py::expand_doc` / `expand_dict` /
//! `expand_list`. See [`expand`] / [`expand_with`] for the entry
//! points. Recursion detection and the post-expand
//! whiteout/nullout cleanup pass live here.

use crate::dict_merge::{merge, MERGE_STRATEGY_KEY};
use crate::include::{find_template, parse_merge_key, IncludeResolver, MergeKey, NullResolver};
use crate::node::{Node, Source};
use crate::{MergeError, Result};
use indexmap::IndexMap;
use std::collections::HashMap;

/// In-doc anchor cache populated by `+&: name` declarations and
/// consulted by `+*name` references. Mirrors the
/// `doc._anchorCache` mechanism in `merge.py:516-517` but without
/// the YAML-native anchor walk (we only support manual
/// declarations via the `+&` key).
type Anchors = HashMap<String, Node>;

/// One include directive encountered during expansion.
#[derive(Clone, Debug)]
pub enum IncludeEntry {
    /// Directive whose target was found and merged in.
    Resolved {
        /// The parsed directive key.
        key: MergeKey,
        /// The value of the directive (usually `Null` or a string).
        value: Node,
    },
    /// Directive whose target wasn't found yet. `expand_doc` retries
    /// until missing counts stabilize; required-and-missing then
    /// errors, optional-and-missing are silently removed.
    Missing {
        /// The parsed directive key.
        key: MergeKey,
        /// The value of the directive.
        value: Node,
    },
}

impl IncludeEntry {
    fn is_missing(&self) -> bool {
        matches!(self, IncludeEntry::Missing { .. })
    }

    fn merge_key(&self) -> &MergeKey {
        match self {
            IncludeEntry::Resolved { key, .. } | IncludeEntry::Missing { key, .. } => key,
        }
    }
}

/// Per-path record of include directives encountered. Keys are
/// stringified paths (sequence indices included as base-10 strings,
/// matching `lookup_path`'s convention).
pub type Includes = IndexMap<Vec<String>, Vec<IncludeEntry>>;

/// Expand `doc` using only pointer/relative includes. Equivalent to
/// [`expand_with`] with [`NullResolver`].
pub fn expand(doc: &Node) -> Result<(Includes, Node)> {
    expand_with(doc, &NullResolver)
}

/// Expand `doc`, resolving `+include` directives via `resolver`.
///
/// Walks the document, replacing `+...` keys with their resolved
/// templates (deep-merged with the surrounding map). Iterates until
/// the missing-include count stabilizes; if any required includes
/// remain missing, returns `Err`. Optional missing includes are
/// silently dropped. Finally runs a cleanup pass that removes
/// mappings whose `+%` value is `whiteout` or `nullout`.
///
/// The returned [`Includes`] map records every directive seen and
/// is consumed by `restore_includes` (later commit) to reconstruct
/// the original directive form for round-tripping.
pub fn expand_with<R: IncludeResolver>(doc: &Node, resolver: &R) -> Result<(Includes, Node)> {
    if !matches!(doc, Node::Mapping { .. }) {
        return Err(MergeError::MergeRejected(format!(
            "top level element is not a mapping: {doc:?}"
        )));
    }

    let mut includes = Includes::new();
    let mut anchors: Anchors = HashMap::new();
    let mut expanded = expand_dict(doc, &[], &mut includes, doc, resolver, &mut anchors)?;

    let mut last_missing = 0usize;
    loop {
        let missing_count = count_missing(&includes);
        if missing_count == 0 {
            delete_deleted_keys(&mut expanded);
            return Ok((includes, expanded));
        }
        if missing_count == last_missing {
            // No progress on this pass.
            let required: Vec<String> = includes
                .values()
                .flatten()
                .filter(|e| e.is_missing() && !e.merge_key().maybe)
                .map(|e| e.merge_key().key.clone())
                .collect();
            if !required.is_empty() {
                return Err(MergeError::MergeRejected(format!(
                    "missing includes: {required:?}"
                )));
            }
            // Drop the directive keys of optional misses from the
            // expanded result so they don't survive as data.
            let to_drop: Vec<(Vec<String>, String)> = includes
                .iter()
                .flat_map(|(path, entries)| {
                    entries
                        .iter()
                        .filter(|e| e.is_missing())
                        .map(move |e| (path.clone(), e.merge_key().key.clone()))
                })
                .collect();
            for (path, key) in to_drop {
                drop_directive_key(&mut expanded, &path, &key);
            }
            delete_deleted_keys(&mut expanded);
            return Ok((includes, expanded));
        }
        last_missing = missing_count;
        includes = Includes::new();
        // Don't clear `anchors` — pass 1's `+&: name` registrations
        // are exactly what lets pass 2 resolve forward `+*name`
        // references that came before their declaration in document
        // order. Pass 2 may re-register the same names with newer
        // content; that overwrite is correct.
        expanded = expand_dict(&expanded, &[], &mut includes, doc, resolver, &mut anchors)?;
    }
}

/// Result of expanding a single mapping. Most expansions produce a
/// mapping; pointer-includes that resolve to a non-mapping (and are
/// the only directive present) replace the whole mapping with the
/// resolved value (matches `merge.py:543`'s `return template`).
enum DictExpansion {
    Mapping(Node),
    Value(Node),
}

fn expand_dict<R: IncludeResolver>(
    doc: &Node,
    path: &[String],
    includes: &mut Includes,
    current: &Node,
    resolver: &R,
    anchors: &mut Anchors,
) -> Result<Node> {
    match expand_dict_inner(doc, path, includes, current, resolver, anchors)? {
        DictExpansion::Mapping(n) | DictExpansion::Value(n) => Ok(n),
    }
}

fn expand_dict_inner<R: IncludeResolver>(
    doc: &Node,
    path: &[String],
    includes: &mut Includes,
    current: &Node,
    resolver: &R,
    anchors: &mut Anchors,
) -> Result<DictExpansion> {
    let Node::Mapping {
        entries: cur_entries,
        source: cur_source,
    } = current
    else {
        // Defensive: callers should pass mappings.
        return Ok(DictExpansion::Value(current.clone()));
    };

    let mut cp_entries: IndexMap<String, Node> = IndexMap::with_capacity(cur_entries.len());
    let mut templates: Vec<Node> = Vec::new();
    let mut overlays: Vec<Node> = Vec::new();
    // Anchor names declared via `+&: name` in this mapping. The
    // registration happens after the mapping is fully expanded so
    // the cache entry holds the final (merged) form rather than a
    // partial snapshot. Diverges slightly from `merge.py:516-517`,
    // which captures cp mid-iteration via Python's mutable reference
    // semantics; the retry loop in `expand_with` papers over any
    // ordering-dependent lookups.
    let mut declared_anchors: Vec<String> = Vec::new();

    for (key, value) in cur_entries {
        // Bare `+%` strategy key is data, copied as-is.
        if key == MERGE_STRATEGY_KEY {
            cp_entries.insert(key.clone(), value.clone());
            continue;
        }

        if key.starts_with('+') {
            // `+&: name` — declare this mapping as an anchor under
            // `name`. Mirrors `merge.py:515-518`. We collect names
            // now and register the final result after the loop.
            if key.len() == 2 && key.as_bytes()[1] == b'&' {
                if let Node::String(name) = value {
                    declared_anchors.push(name.clone());
                }
                continue;
            }

            let parsed = parse_merge_key(key)?;
            let Some(mk) = parsed else {
                // Not a recognized merge key; copy as-is.
                cp_entries.insert(key.clone(), value.clone());
                continue;
            };

            // Resolve template: anchor, file include, or pointer/relative.
            let resolution = if mk.anchor.is_some() {
                resolve_anchor(&mk, anchors)
            } else if mk.include.is_some() {
                resolve_include(&mk, value, cur_source, resolver)?
            } else {
                resolve_pointer(doc, &mk, path)?
            };

            match resolution {
                TemplateResolution::Missing => {
                    includes
                        .entry(path.to_vec())
                        .or_default()
                        .push(IncludeEntry::Missing {
                            key: mk,
                            value: value.clone(),
                        });
                    // Keep the directive in cp so the retry loop can
                    // find it on the next pass.
                    cp_entries.insert(key.clone(), value.clone());
                    continue;
                }
                TemplateResolution::Found {
                    template,
                    template_path,
                } => {
                    // Recursion detection (matches merge.py:345-353):
                    // if the template's absolute path is a prefix of
                    // the current path, we'd be including an ancestor
                    // of where we are — error.
                    if mk.include.is_none()
                        && mk.anchor.is_none()
                        && template_path.len() <= path.len()
                        && path[..template_path.len()] == template_path[..]
                    {
                        return Err(MergeError::MergeRejected(format!(
                            "recursive include {:?} in {:?} when including {}",
                            template_path, path, mk.key
                        )));
                    }

                    includes
                        .entry(path.to_vec())
                        .or_default()
                        .push(IncludeEntry::Resolved {
                            key: mk.clone(),
                            value: value.clone(),
                        });

                    // Recursively expand the template (its own
                    // +directives need to resolve too).
                    let expanded_template = if matches!(template, Node::Mapping { .. }) {
                        // Use the template's discovered path so its
                        // own relative includes resolve correctly.
                        expand_dict(doc, &template_path, includes, &template, resolver, anchors)?
                    } else {
                        template
                    };

                    if let Node::Mapping { .. } = &expanded_template {
                        let value_says_overlay = matches!(
                            value,
                            Node::String(s) if s.contains("overlay")
                        );
                        if value_says_overlay {
                            overlays.push(expanded_template);
                        } else {
                            templates.push(expanded_template);
                        }
                    } else {
                        // Template is a non-mapping value. If the
                        // current mapping has nothing else to
                        // contribute, replace it wholesale; else error.
                        if cur_entries.len() > 1 {
                            return Err(MergeError::MergeRejected(format!(
                                "cannot merge {} with non-map value",
                                mk.key
                            )));
                        }
                        return Ok(DictExpansion::Value(expanded_template));
                    }
                }
            }
            continue;
        }

        // Regular key: recurse into mappings and sequences.
        let mut child_path = path.to_vec();
        child_path.push(key.clone());
        let expanded_value = match value {
            Node::Mapping { .. } => {
                expand_dict(doc, &child_path, includes, value, resolver, anchors)?
            }
            Node::Sequence(items) => Node::Sequence(expand_list(
                doc,
                &child_path,
                includes,
                items,
                resolver,
                anchors,
            )?),
            other => other.clone(),
        };
        cp_entries.insert(key.clone(), expanded_value);
    }

    // Compose templates -> cp -> overlays via successive merges.
    let cp_node = Node::Mapping {
        entries: cp_entries,
        source: cur_source.clone(),
    };

    let result = if templates.is_empty() && overlays.is_empty() {
        cp_node
    } else {
        // Merge order: templates..., cp, overlays... (Python pops front
        // from `[t1..., cp, o1...]`, so each subsequent merge has the
        // next item as overlay over the running accumulator).
        let mut chain: Vec<Node> = Vec::with_capacity(templates.len() + overlays.len() + 1);
        chain.append(&mut templates);
        chain.push(cp_node);
        chain.append(&mut overlays);
        let mut accum = chain.remove(0);
        for next in chain {
            accum = merge(&accum, &next)?;
        }
        accum
    };

    // Register any `+&: name` declarations under the final result.
    // A re-declaration with the same name overwrites; matches a
    // later sibling winning if it uses the same anchor name.
    for name in declared_anchors {
        anchors.insert(name, result.clone());
    }

    Ok(DictExpansion::Mapping(result))
}

fn expand_list<R: IncludeResolver>(
    doc: &Node,
    path: &[String],
    includes: &mut Includes,
    items: &[Node],
    resolver: &R,
    anchors: &mut Anchors,
) -> Result<Vec<Node>> {
    let mut out: Vec<Node> = Vec::with_capacity(items.len());
    for (i, item) in items.iter().enumerate() {
        if let Node::Mapping { entries, .. } = item {
            // Items with `+%: whiteout` are dropped from the
            // expanded list (matches merge.py:636-637).
            if entries.get(MERGE_STRATEGY_KEY).and_then(node_str) == Some("whiteout") {
                continue;
            }

            let mut child_path = path.to_vec();
            child_path.push(i.to_string());
            let new_item = expand_dict(doc, &child_path, includes, item, resolver, anchors)?;
            // If the expanded value is a sequence, flatten its items
            // into the outer list (matches merge.py:639-642).
            if let Node::Sequence(inner) = new_item {
                out.extend(inner);
            } else {
                out.push(new_item);
            }
        } else {
            out.push(item.clone());
        }
    }
    Ok(out)
}

enum TemplateResolution {
    Missing,
    Found {
        template: Node,
        template_path: Vec<String>,
    },
}

fn resolve_pointer(
    doc: &Node,
    mk: &MergeKey,
    current_path: &[String],
) -> Result<TemplateResolution> {
    match find_template(doc, mk, current_path)? {
        Some((node, path)) => Ok(TemplateResolution::Found {
            template: node.clone(),
            template_path: path,
        }),
        None => Ok(TemplateResolution::Missing),
    }
}

/// Resolve a `+*name[.../pointer]` directive against the in-doc
/// anchor cache populated by `+&: name` declarations.
///
/// Pointer segments (`mk.pointer`) are walked into the anchor's
/// value after the name lookup (matches `merge.py::_find_template`
/// at line 393, post-anchor branch). `mk.relative` is ignored for
/// anchors — see the comment at `merge.py:376`.
fn resolve_anchor(mk: &MergeKey, anchors: &Anchors) -> TemplateResolution {
    let name = mk.anchor.as_ref().expect("anchor set");
    let Some(mut node) = anchors.get(name) else {
        return TemplateResolution::Missing;
    };
    for segment in &mk.pointer {
        node = match node {
            Node::Mapping { entries, .. } => match entries.get(segment) {
                Some(v) => v,
                None => return TemplateResolution::Missing,
            },
            Node::Sequence(items) => {
                match segment.parse::<usize>().ok().and_then(|i| items.get(i)) {
                    Some(v) => v,
                    None => return TemplateResolution::Missing,
                }
            }
            _ => return TemplateResolution::Missing,
        };
    }
    TemplateResolution::Found {
        template: node.clone(),
        template_path: Vec::new(),
    }
}

fn resolve_include<R: IncludeResolver>(
    mk: &MergeKey,
    value: &Node,
    current_source: &Source,
    resolver: &R,
) -> Result<TemplateResolution> {
    let target = match value {
        Node::String(s) => s.clone(),
        Node::Null => String::new(),
        other => {
            return Err(MergeError::MergeRejected(format!(
                "{}: include target must be a string, got {other:?}",
                mk.key
            )));
        }
    };
    match resolver.load(current_source, &target)? {
        Some(node) => Ok(TemplateResolution::Found {
            template: node,
            // File includes have no in-doc path; use an empty path so
            // the recursion-detection check (which only fires when
            // !mk.include) doesn't kick in anyway.
            template_path: Vec::new(),
        }),
        None => Ok(TemplateResolution::Missing),
    }
}

fn count_missing(includes: &Includes) -> usize {
    includes
        .values()
        .flatten()
        .filter(|e| e.is_missing())
        .count()
}

/// Clean up after the retry loop has accepted that some optional
/// includes will never resolve.
///
/// Mirrors `merge.py:610-616`'s asymmetric behavior:
/// - If `path` is empty (directive at the top level), remove just
///   the `directive_key` from `expanded`.
/// - If `path` is non-empty (directive inside a sub-mapping),
///   remove the *entire sub-mapping* at `path` from its parent. The
///   reasoning is that the parent mapping's purpose was to host the
///   missing include; with the include gone, the mapping is treated
///   as having no useful content.
fn drop_directive_key(expanded: &mut Node, path: &[String], directive_key: &str) {
    if path.is_empty() {
        if let Node::Mapping { entries, .. } = expanded {
            entries.shift_remove(directive_key);
        }
        return;
    }
    let (parent_path, last) = path.split_at(path.len() - 1);
    let last_segment = &last[0];
    let parent = match walk_mut(expanded, parent_path) {
        Some(p) => p,
        None => return,
    };
    if let Node::Mapping { entries, .. } = parent {
        entries.shift_remove(last_segment);
    }
}

fn walk_mut<'a>(node: &'a mut Node, path: &[String]) -> Option<&'a mut Node> {
    let mut elem = node;
    for segment in path {
        elem = match elem {
            Node::Mapping { entries, .. } => entries.get_mut(segment)?,
            Node::Sequence(items) => {
                let idx: usize = segment.parse().ok()?;
                items.get_mut(idx)?
            }
            _ => return None,
        };
    }
    Some(elem)
}

/// Post-expansion cleanup: remove keys whose value is a mapping
/// carrying `+%: whiteout` or `+%: nullout`. Mirrors
/// `merge.py::_delete_deleted_keys`.
fn delete_deleted_keys(expanded: &mut Node) {
    if let Node::Mapping { entries, .. } = expanded {
        let to_delete: Vec<String> = entries
            .iter()
            .filter_map(|(k, v)| {
                if let Node::Mapping { entries: ve, .. } = v {
                    if let Some(s) = ve.get(MERGE_STRATEGY_KEY).and_then(node_str) {
                        if s == "whiteout" || s == "nullout" {
                            return Some(k.clone());
                        }
                    }
                }
                None
            })
            .collect();
        for k in &to_delete {
            entries.shift_remove(k);
        }
        for (_, v) in entries.iter_mut() {
            delete_deleted_keys(v);
        }
    } else if let Node::Sequence(items) = expanded {
        for item in items.iter_mut() {
            delete_deleted_keys(item);
        }
    }
}

fn node_str(n: &Node) -> Option<&str> {
    if let Node::String(s) = n {
        Some(s.as_str())
    } else {
        None
    }
}
