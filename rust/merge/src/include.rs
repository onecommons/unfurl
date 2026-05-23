// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Merge-include directive parsing and pointer navigation.
//!
//! Rust port of the `+`-prefixed merge-key machinery in
//! `unfurl/merge.py`: [`parse_merge_key`], [`lookup_path`] (JSON-pointer
//! traversal), and [`find_template`] (anchor-/relative-/pointer-based
//! template lookup within a single document).
//!
//! Anchor support (`+*name`) is parsed but not yet resolvable —
//! `find_template` returns `Err` when an anchor is set, matching
//! the "anchors not implemented yet" stance documented in
//! `crate::node`. File-include resolution (`+include: foo.yaml`)
//! lives in a follow-up commit alongside the `IncludeResolver`
//! trait.

use crate::node::{Node, Source};
use crate::{MergeError, Result};
use regex::Regex;
use std::path::PathBuf;
use std::sync::OnceLock;

/// Parsed form of a `+...` merge key.
///
/// Field meanings match `merge.py::MergeKey`:
/// - `key`: the full original key text (e.g. `"+?/foo/bar"`).
/// - `maybe`: `?` was present (the include is optional).
/// - `include`: `"include"` or `"include-foo"` if this is a file
///   include; `None` otherwise.
/// - `anchor`: anchor name (e.g. `"myanchor"` for `+*myanchor`); not
///   yet resolvable.
/// - `relative`: number of `.` characters; 0 = absolute pointer from
///   the document root, `n` = walk up `n-1` levels from the current
///   path before applying the pointer.
/// - `pointer`: JSON-pointer segments (everything after the first
///   `/`, unescaped per RFC 6901).
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct MergeKey {
    pub key: String,
    pub maybe: bool,
    pub include: Option<String>,
    pub anchor: Option<String>,
    pub relative: usize,
    pub pointer: Vec<String>,
}

/// Parse a `+`-prefixed merge key. Returns `None` when `key`
/// doesn't actually start with `+` or isn't a recognisable merge
/// directive; returns `Err` only for malformed JSON-pointer escapes
/// (the same condition Python's `_json_pointer_validate` raises on).
pub fn parse_merge_key(key: &str) -> Result<Option<MergeKey>> {
    if !key.starts_with('+') {
        return Ok(None);
    }
    let body = &key[1..];
    json_pointer_validate(body)?;

    let raw_parts: Vec<&str> = body.split('/').collect();
    let first = json_pointer_unescape(raw_parts[0]);
    let rest: Vec<String> = raw_parts[1..]
        .iter()
        .map(|p| json_pointer_unescape(p))
        .collect();

    let Some(caps) = first_re().captures(&first) else {
        return Ok(None);
    };

    let maybe = caps.get(1).map(|m| !m.as_str().is_empty()).unwrap_or(false);
    let include = caps.get(2).map(|m| m.as_str().to_string());
    let anchor_with_dots = caps.get(3).map(|m| m.as_str().to_string());
    let mut relative = caps.get(4).map(|m| m.as_str().len()).unwrap_or(0);

    // Anchor's trailing dots roll into the relative count, matching
    // merge.py:451's `relative = len(anchor.rstrip(".")) - len(anchor)`
    // (Python's value is negative; we use the absolute count instead).
    let anchor = if let Some(awd) = anchor_with_dots {
        let stripped = awd.trim_end_matches('.');
        relative = awd.len() - stripped.len();
        // Strip the leading `*`.
        Some(stripped[1..].to_string())
    } else {
        None
    };

    // Reject keys whose first segment matched nothing AND there's no
    // pointer portion to fall back on.
    if !maybe && include.is_none() && anchor.is_none() && relative == 0 {
        if !first.is_empty() {
            return Ok(None);
        }
        if rest.is_empty() {
            return Ok(None);
        }
    }

    Ok(Some(MergeKey {
        key: key.to_string(),
        maybe,
        include,
        anchor,
        relative,
        pointer: rest,
    }))
}

/// Walk `doc` along `path`. Returns `None` if any segment doesn't
/// resolve. Integer-looking segments index sequences; everything
/// else looks up by key in mappings.
pub fn lookup_path<'a>(doc: &'a Node, path: &[String]) -> Option<&'a Node> {
    let mut elem = doc;
    for segment in path {
        elem = match elem {
            Node::Mapping { entries, .. } => entries.get(segment)?,
            Node::Sequence(items) => {
                let idx: usize = segment.parse().ok()?;
                items.get(idx)?
            }
            _ => return None,
        };
    }
    Some(elem)
}

/// Resolve a [`MergeKey`] against `doc` to find the template node it
/// points at, plus that template's absolute path from doc root
/// (used by [`expand`](crate::expand) for recursion detection).
///
/// `current_path` is the location of the mapping that contains the
/// merge-key directive; relative pointers walk up from there.
/// Returns `Ok(None)` when the template wasn't found and
/// `key.maybe` is `true`; returns `Err` when the template wasn't
/// found and the include is required.
///
/// File includes (`key.include.is_some()`) are not handled here —
/// the caller is expected to dispatch to an include resolver
/// (introduced in a follow-up commit). Anchors (`key.anchor.is_some()`)
/// return `Err` since they're not yet supported.
pub fn find_template<'a>(
    doc: &'a Node,
    key: &MergeKey,
    current_path: &[String],
) -> Result<Option<(&'a Node, Vec<String>)>> {
    if key.anchor.is_some() {
        return Err(MergeError::MergeRejected(format!(
            "anchor references not supported yet: {}",
            key.key
        )));
    }

    // Compute the starting path: absolute (= empty) for non-relative
    // keys, or `current_path[..current_path.len() - (relative-1)]`
    // for relative ones (one dot = "here", two dots = "parent", etc.).
    let mut template_path: Vec<String> = if key.relative > 0 {
        let drop = key.relative.saturating_sub(1);
        if drop > current_path.len() {
            return missing(key, "could not find relative path");
        }
        current_path[..current_path.len() - drop].to_vec()
    } else {
        Vec::new()
    };

    let Some(mut template) = lookup_path(doc, &template_path) else {
        return missing(key, "could not find relative path");
    };

    for segment in &key.pointer {
        template = match template {
            Node::Mapping { entries, .. } => {
                let Some(child) = entries.get(segment) else {
                    return missing(key, "can not find segment in document");
                };
                child
            }
            Node::Sequence(items) => {
                let Ok(idx) = segment.parse::<usize>() else {
                    return missing(key, "non-integer index into sequence");
                };
                let Some(child) = items.get(idx) else {
                    return missing(key, "sequence index out of range");
                };
                child
            }
            _ => return missing(key, "cannot index into a scalar"),
        };
        template_path.push(segment.clone());
    }

    Ok(Some((template, template_path)))
}

fn missing<T>(key: &MergeKey, why: &str) -> Result<Option<T>> {
    if key.maybe {
        Ok(None)
    } else {
        Err(MergeError::MergeRejected(format!(
            "{why}: include directive {} could not be resolved",
            key.key
        )))
    }
}

/// JSON Pointer (RFC 6901) unescape: `~1` → `/`, `~0` → `~`.
/// Order matters — `~1` must be replaced first so that `~01`
/// becomes `~1` (literal) rather than `/0` then `/`.
pub fn json_pointer_unescape(s: &str) -> String {
    s.replace("~1", "/").replace("~0", "~")
}

/// RFC 6901 escape validation. `~` must be followed by `0` or `1`;
/// anything else (including a trailing `~`) is an error.
pub fn json_pointer_validate(s: &str) -> Result<()> {
    let bytes = s.as_bytes();
    for (i, &b) in bytes.iter().enumerate() {
        if b == b'~' {
            match bytes.get(i + 1) {
                Some(b'0') | Some(b'1') => {}
                _ => {
                    return Err(MergeError::MergeRejected(format!(
                        "invalid JSON pointer escape at position {i} in {s:?}"
                    )));
                }
            }
        }
    }
    Ok(())
}

fn first_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^([?]?)(include[\w-]*)?([*]\S+)?([.]+)?$").unwrap())
}

// ----------------------------------------------------------------------
// IncludeResolver — pluggable file-include loading
// ----------------------------------------------------------------------

/// Loads documents referenced by `+include` directives.
///
/// `expand`-style entry points consult an `IncludeResolver` when
/// they encounter a merge key whose `include` field is set
/// (e.g. `+include: foo.yaml`). The resolver decides how to map
/// the directive value onto a [`Node`] tree: load a local file,
/// fetch a URL, look up a CSAR, etc.
///
/// Implementations should return `Ok(None)` to signal "target not
/// found"; the caller decides whether that's fatal based on the
/// `maybe` flag of the [`MergeKey`].
pub trait IncludeResolver {
    /// Load a document referenced by `target` from the perspective
    /// of `current` (the source of the mapping containing the
    /// `+include` directive). `current.base_dir()` is the natural
    /// anchor for relative paths.
    fn load(&self, current: &Source, target: &str) -> Result<Option<Node>>;
}

/// Resolves `+include` targets as filesystem paths relative to the
/// current document's directory using [`crate::load_file`].
///
/// Missing files return `Ok(None)` (the caller's `maybe` flag
/// decides whether to error). Other I/O errors and YAML parse
/// errors propagate as `Err(MergeError)`.
pub struct FileResolver;

impl IncludeResolver for FileResolver {
    fn load(&self, current: &Source, target: &str) -> Result<Option<Node>> {
        let resolved: PathBuf = current.base_dir().join(target);
        match std::fs::metadata(&resolved) {
            Ok(_) => Ok(Some(crate::load_file(&resolved)?)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(MergeError::Io(e)),
        }
    }
}

/// Resolver that never finds anything. Useful as the default for
/// expand-style entry points that need pointer/relative includes
/// only and shouldn't be able to read the filesystem.
pub struct NullResolver;

impl IncludeResolver for NullResolver {
    fn load(&self, _: &Source, _: &str) -> Result<Option<Node>> {
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::load_file;

    #[test]
    fn parses_absolute_pointer() {
        let mk = parse_merge_key("+/foo/bar").unwrap().unwrap();
        assert!(!mk.maybe);
        assert!(mk.include.is_none());
        assert!(mk.anchor.is_none());
        assert_eq!(mk.relative, 0);
        assert_eq!(mk.pointer, vec!["foo".to_string(), "bar".to_string()]);
    }

    #[test]
    fn parses_maybe_prefix() {
        let mk = parse_merge_key("+?/foo").unwrap().unwrap();
        assert!(mk.maybe);
        assert_eq!(mk.pointer, vec!["foo".to_string()]);
    }

    #[test]
    fn parses_include_directive() {
        let mk = parse_merge_key("+include").unwrap().unwrap();
        assert_eq!(mk.include.as_deref(), Some("include"));
        assert!(mk.pointer.is_empty());
    }

    #[test]
    fn parses_include_with_suffix() {
        let mk = parse_merge_key("+include-raw").unwrap().unwrap();
        assert_eq!(mk.include.as_deref(), Some("include-raw"));
    }

    #[test]
    fn parses_relative_dots() {
        let mk = parse_merge_key("+../foo").unwrap().unwrap();
        assert_eq!(mk.relative, 2);
        assert_eq!(mk.pointer, vec!["foo".to_string()]);
    }

    #[test]
    fn parses_anchor_with_relative_dots() {
        let mk = parse_merge_key("+*myanchor..").unwrap().unwrap();
        assert_eq!(mk.anchor.as_deref(), Some("myanchor"));
        assert_eq!(mk.relative, 2);
    }

    #[test]
    fn rejects_non_merge_key() {
        assert!(parse_merge_key("foo").unwrap().is_none());
        assert!(parse_merge_key("+nonsense").unwrap().is_none());
    }

    #[test]
    fn rejects_invalid_json_pointer_escape() {
        assert!(parse_merge_key("+/foo~bar").is_err());
        assert!(parse_merge_key("+/foo~").is_err());
    }

    #[test]
    fn unescape_handles_order_correctly() {
        assert_eq!(json_pointer_unescape("~01"), "~1");
        assert_eq!(json_pointer_unescape("~10"), "/0");
        assert_eq!(json_pointer_unescape("a~1b~0c"), "a/b~c");
    }

    fn fixture(name: &str) -> Node {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests")
            .join("fixtures")
            .join("simple")
            .join(name);
        load_file(&path).expect("load fixture")
    }

    #[test]
    fn lookup_path_walks_nested_mapping() {
        let doc = fixture("base.yaml");
        let n = lookup_path(&doc, &["nested".into(), "enabled".into()]).unwrap();
        assert!(matches!(n, Node::Bool(true)));
    }

    #[test]
    fn lookup_path_walks_into_sequence_by_index() {
        let doc = fixture("base.yaml");
        let n = lookup_path(&doc, &["items".into(), "0".into()]).unwrap();
        if let Node::String(s) = n {
            assert_eq!(s, "alpha");
        } else {
            panic!("expected string");
        }
    }

    #[test]
    fn lookup_path_returns_none_for_missing_key() {
        let doc = fixture("base.yaml");
        assert!(lookup_path(&doc, &["nope".into()]).is_none());
    }

    #[test]
    fn find_template_absolute_pointer() {
        let doc = fixture("base.yaml");
        let mk = parse_merge_key("+/nested/count").unwrap().unwrap();
        let (template, path) = find_template(&doc, &mk, &[]).unwrap().unwrap();
        assert!(matches!(template, Node::Number(_)));
        assert_eq!(path, vec!["nested".to_string(), "count".to_string()]);
    }

    #[test]
    fn find_template_missing_required_errors() {
        let doc = fixture("base.yaml");
        let mk = parse_merge_key("+/does/not/exist").unwrap().unwrap();
        assert!(find_template(&doc, &mk, &[]).is_err());
    }

    #[test]
    fn find_template_missing_optional_returns_none() {
        let doc = fixture("base.yaml");
        let mk = parse_merge_key("+?/does/not/exist").unwrap().unwrap();
        assert!(find_template(&doc, &mk, &[]).unwrap().is_none());
    }

    #[test]
    fn find_template_relative_walks_up() {
        // base.yaml: { name, version, items, nested: { enabled, count } }
        // current path = ["nested"], +../name should resolve to root's "name"
        let doc = fixture("base.yaml");
        let mk = parse_merge_key("+../name").unwrap().unwrap();
        let (template, path) = find_template(&doc, &mk, &["nested".into()])
            .unwrap()
            .unwrap();
        if let Node::String(s) = template {
            assert_eq!(s, "example");
        } else {
            panic!("expected string");
        }
        assert_eq!(path, vec!["name".to_string()]);
    }

    // ------------------------------------------------------------------
    // IncludeResolver / FileResolver / NullResolver
    // ------------------------------------------------------------------

    use std::sync::Arc;

    fn fake_source_for(file: PathBuf) -> Source {
        Source {
            file: Arc::new(file),
            line: 0,
            col: 0,
        }
    }

    #[test]
    fn file_resolver_loads_sibling_file() {
        let fixture_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests")
            .join("fixtures")
            .join("simple");
        // Pretend a parent doc lives next to base.yaml.
        let parent = fake_source_for(fixture_dir.join("parent.yaml"));
        let node = FileResolver
            .load(&parent, "base.yaml")
            .expect("load")
            .expect("Some");
        // base.yaml's root mapping has the keys we expect.
        if let Node::Mapping { entries, .. } = &node {
            assert!(entries.contains_key("name"));
            assert!(entries.contains_key("nested"));
        } else {
            panic!("expected mapping");
        }
    }

    #[test]
    fn file_resolver_returns_none_for_missing_file() {
        let fixture_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests")
            .join("fixtures")
            .join("simple");
        let parent = fake_source_for(fixture_dir.join("parent.yaml"));
        let result = FileResolver
            .load(&parent, "definitely-not-there.yaml")
            .expect("no IO error");
        assert!(result.is_none());
    }

    #[test]
    fn null_resolver_always_returns_none() {
        let parent = fake_source_for(PathBuf::from("/whatever.yaml"));
        let result = NullResolver.load(&parent, "anything").expect("no error");
        assert!(result.is_none());
    }
}
