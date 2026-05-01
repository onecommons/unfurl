// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! CloudMap [`DataFormat`] implementation.
//!
//! Recognises documents with `kind: CloudMap` and extracts records
//! under the five path-prefix sections (`services`, `repositories`,
//! `artifacts`, `instantiations`, `types`).
//!
//! The helpers port logic from the Python codebase:
//!
//! - [`CloudMapFormat::is_format`] matches `unfurl/cloudmap.py:518`
//!   (`kind == "CloudMap"`).
//! - [`CloudMapFormat::path_prefixes`] matches `unfurl/cloudmap.py:533–565`.
//! - [`CloudMapFormat::find_alias`] ports `VersionedRecord._load_versions`
//!   (`unfurl/tosca_plugins/cloudmap_defs.py:416–441`) plus
//!   `join_resource_url` (`unfurl/tosca_plugins/cloudmap_defs.py:64–92`).
//! - [`CloudMapFormat::follow`] ports `CloudMapGraphWalker._walk_edges`
//!   (`unfurl/reporting.py:658–740`).

use std::collections::BTreeSet;

use url::Url;

use crate::format::DataFormat;
use crate::{Order, Record};

const PATH_PREFIXES: &[&str] = &[
    "services",
    "repositories",
    "artifacts",
    "instantiations",
    "types",
];

/// The CloudMap [`DataFormat`] implementation.
///
/// Zero-sized; construct with `CloudMapFormat` or
/// [`CloudMapFormat::new`]. Pre-registered by
/// [`crate::FormatRegistry::with_builtins`].
#[derive(Debug, Default, Clone)]
pub struct CloudMapFormat;

impl CloudMapFormat {
    pub fn new() -> Self {
        Self
    }

    /// Decode the JSON-pointer escape `~0`/`~1` and return the original
    /// segment. Inverse of [`escape_pointer_segment`]. Kept available
    /// for callers still parsing externally-supplied pointers.
    #[allow(dead_code)]
    pub(crate) fn unescape_pointer_segment(seg: &str) -> String {
        // RFC 6901 § 4: `~1` → `/`, `~0` → `~`. The replace order matters.
        seg.replace("~1", "/").replace("~0", "~")
    }

    /// Top-level prefix of a record (e.g. `"repositories"`) — the record
    /// path is just the parent JSON-pointer (e.g. `/repositories`),
    /// so the section is the single segment after the leading slash.
    /// Returns `None` when the record is not under any known prefix.
    fn record_section(record: &Record) -> Option<&'static str> {
        let head = record.path.trim_start_matches('/');
        PATH_PREFIXES.iter().copied().find(|p| *p == head)
    }
}

impl DataFormat for CloudMapFormat {
    fn name(&self) -> &str {
        "cloudmap"
    }

    fn is_format(&self, json: &serde_json::Value) -> bool {
        // `unfurl/cloudmap.py:518`: default_db sets kind = "CloudMap".
        json.get("kind").and_then(|v| v.as_str()) == Some("CloudMap")
    }

    fn path_prefixes(&self) -> &[&str] {
        PATH_PREFIXES
    }

    fn find_alias(&self, record: &Record) -> Vec<(String, String)> {
        // Versioned records (services, instantiations, types in some
        // cases — and repositories via their `versions` field, see
        // `unfurl/tosca_plugins/cloudmap_defs.py:416–441`) expose one
        // alias per (parent_url, version_key) pair, computed via
        // `join_resource_url` (`cloudmap_defs.py:64–92`).
        if Self::record_section(record).is_none() {
            return Vec::new();
        }
        // The parent URL is now the unescaped key directly.
        let parent_key = record.key.as_str();

        let Some(versions) = record.json.get("versions").and_then(|v| v.as_object()) else {
            return Vec::new();
        };

        let mut out = Vec::new();
        for version_key in versions.keys() {
            // Ports `_load_versions`: child url = join_resource_url(parent_url, version_key).
            let joined = join_resource_url(parent_key, version_key);
            out.push((record.path.clone(), joined));
        }
        out
    }

    fn follow(&self, record: &Record) -> Vec<String> {
        // Equivalent to CloudMapGraphWalker._walk_edges in `unfurl/reporting.py`.
        // Only URL-shaped edges are returned (`_is_url`, line 632).
        let Some(prefix) = Self::record_section(record) else {
            return Vec::new();
        };

        let mut urls: Vec<String> = Vec::new();
        let json = &record.json;

        match prefix {
            "repositories" => {
                push_str(&mut urls, json.get("fork_of"));
                push_str(&mut urls, json.get("mirror_of"));
                push_str(&mut urls, json.get("service"));
                if let Some(notable) = json.get("notable").and_then(|v| v.as_object()) {
                    for nd in notable.values() {
                        push_str(&mut urls, nd.get("artifact"));
                    }
                }
            }
            "artifacts" => {
                collect_typed_url_keys(json.get("notable"), &mut urls);
                collect_typed_url_keys(json.get("references"), &mut urls);
                collect_typed_url_keys(json.get("dependencies"), &mut urls);
                collect_typed_url_keys(json.get("instantiated_by"), &mut urls);
                // `instantiates` → only label-shaped (type names); skip.
            }
            "instantiations" => {
                push_str(&mut urls, json.get("source"));
                collect_typed_url_keys(json.get("instantiated"), &mut urls);
                collect_typed_url_keys(json.get("inputs"), &mut urls);
            }
            "services" => {
                collect_typed_url_keys(json.get("connections"), &mut urls);
                collect_typed_url_keys(json.get("instantiated_by"), &mut urls);
            }
            "types" => {
                push_str(&mut urls, json.get("source"));
                push_str(&mut urls, json.get("model"));
                if let Some(impls) = json.get("implementations").and_then(|v| v.as_array()) {
                    for v in impls {
                        push_str(&mut urls, Some(v));
                    }
                }
                // `extends` is type-name labelled, not URL-shaped — skip.
            }
            _ => {}
        }

        // Filter URL-shaped values, plus — matching
        // CloudMapGraphWalker — strip the `#fragment` from `git:` URLs
        // and emit the bare repository URL alongside the original.
        let mut out: BTreeSet<String> = BTreeSet::new();
        for url in urls {
            if !is_url(&url) {
                continue;
            }
            if let Some(stripped) = strip_git_fragment(&url) {
                out.insert(stripped);
            }
            out.insert(url);
        }
        out.into_iter().collect()
    }

    fn get_order(&self, path: &str) -> Order {
        // Only the top-level cloudmap sections (`repositories`,
        // `artifacts`, etc.) get key-sorted so git diffs stay stable
        // across runs and clients. Anything else (e.g. envelope keys
        // like `apiVersion`/`kind`, or sections added by a future
        // schema extension we don't know about) keeps its existing
        // key order.
        if PATH_PREFIXES.iter().any(|p| *p == path) {
            Order::Sort
        } else {
            Order::PreserveOrder
        }
    }
}

/// If `url` starts with `git:` and contains a `#` fragment, return the
/// bare repository URL: everything before the first `#`, with `.git`
/// appended when the stripped path doesn't already end in `.git`.
///
/// Cloudmap repository keys conventionally include the `.git` suffix
/// (e.g. `git://unfurl.cloud/onecommons/std.git`), but reference URLs
/// in artifacts often omit it (e.g.
/// `git://unfurl.cloud/onecommons/unfurl-types#v0.7.7:.`). Normalising
/// the stripped form to always end in `.git` lets follow-edges resolve
/// to the canonical repository record.
fn strip_git_fragment(url: &str) -> Option<String> {
    if !url.starts_with("git:") {
        return None;
    }
    let (head, _frag) = url.split_once('#')?;
    if head.ends_with(".git") {
        Some(head.to_string())
    } else {
        Some(format!("{head}.git"))
    }
}

/// Predicate from `unfurl/reporting.py:632` (`_is_url`).
fn is_url(s: &str) -> bool {
    s.contains("://") || s.starts_with("pkg:")
}

fn push_str(out: &mut Vec<String>, value: Option<&serde_json::Value>) {
    if let Some(v) = value.and_then(|v| v.as_str()) {
        if !v.is_empty() {
            out.push(v.to_string());
        }
    }
}

/// Walk a "typed-urls" mapping (`{ url-or-label: TypeRefs|null }`) and
/// pick out only the URL-shaped keys.
fn collect_typed_url_keys(value: Option<&serde_json::Value>, out: &mut Vec<String>) {
    let Some(obj) = value.and_then(|v| v.as_object()) else {
        return;
    };
    for key in obj.keys() {
        if is_url(key) {
            out.push(key.clone());
        }
    }
}

/// JSON-pointer escape per RFC 6901 § 4: `~` → `~0`, `/` → `~1`.
/// Retained for callers that still need to construct external pointer
/// strings; the in-DB representation now stores keys verbatim, so this
/// helper is no longer used by the sync layer itself.
#[allow(dead_code)]
pub(crate) fn escape_pointer_segment(seg: &str) -> String {
    seg.replace('~', "~0").replace('/', "~1")
}

/// Port of `unfurl/tosca_plugins/cloudmap_defs.py:64–92`
/// (`join_resource_url`).
///
/// - If `base_url` has no scheme → return `join_url`.
/// - If `join_url` is absolute, or has neither fragment / query / `@` →
///   return `join_url`.
/// - Otherwise replace fragment, merge query, handle `@`-prefixed path.
pub(crate) fn join_resource_url(base_url: &str, join_url: &str) -> String {
    if join_url.is_empty() {
        return join_url.to_string();
    }
    let Ok(base) = Url::parse(base_url) else {
        // Python's `not base.scheme` check.
        return join_url.to_string();
    };
    // Accept join URLs that the Python branch treats literally.
    let join_parsed = Url::parse(join_url).ok();

    let join_fragment = match &join_parsed {
        Some(j) => j.fragment().map(str::to_string),
        None => parse_fragment(join_url),
    };
    let join_query = match &join_parsed {
        Some(j) => j.query().map(str::to_string),
        None => parse_query(join_url),
    };
    let join_path = match &join_parsed {
        Some(j) => Some(j.path().to_string()),
        None => Some(strip_fragment_query(join_url)),
    };

    let join_has_at = join_path.as_deref().is_some_and(|p| p.contains('@'));

    // "absolute URL or a bare name without a purl version" → return as-is.
    if join_parsed.as_ref().is_some_and(|j| !j.scheme().is_empty()) {
        return join_url.to_string();
    }
    if join_fragment.is_none() && join_query.is_none() && !join_has_at {
        return join_url.to_string();
    }

    // Build replacement.
    let mut new_url = base.clone();

    if let Some(frag) = join_fragment.as_deref() {
        new_url.set_fragment(Some(frag));
    }

    if let Some(jq) = join_query.as_deref() {
        if let Some(bq) = base.query() {
            if jq.contains('=') {
                let base_pairs: Vec<(String, String)> = parse_qsl(bq);
                let join_pairs: Vec<(String, String)> = parse_qsl(jq);
                let join_keys: BTreeSet<String> =
                    join_pairs.iter().map(|(k, _)| k.clone()).collect();
                let mut merged: Vec<(String, String)> = base_pairs
                    .into_iter()
                    .filter(|(k, _)| !join_keys.contains(k))
                    .collect();
                merged.extend(join_pairs);
                new_url.set_query(Some(&urlencode(&merged)));
            } else {
                new_url.set_query(Some(jq));
            }
        } else {
            new_url.set_query(Some(jq));
        }
    }

    if let Some(jp) = join_path.as_deref() {
        if !jp.is_empty() {
            if let Some(rest) = jp.strip_prefix('@') {
                // Treat as PURL version: append to base path with @.
                let base_path = base.path();
                let cut = base_path.find('@').unwrap_or(base_path.len());
                let new_path = format!("{}@{}", &base_path[..cut], rest);
                set_path_preserving(&mut new_url, &new_path);
            } else {
                set_path_preserving(&mut new_url, jp);
            }
        }
    }

    new_url.to_string()
}

fn parse_fragment(s: &str) -> Option<String> {
    s.split_once('#').map(|(_, frag)| frag.to_string())
}

fn parse_query(s: &str) -> Option<String> {
    let head = s.split_once('#').map(|(h, _)| h).unwrap_or(s);
    head.split_once('?').map(|(_, q)| q.to_string())
}

fn strip_fragment_query(s: &str) -> String {
    let no_frag = s.split_once('#').map(|(h, _)| h).unwrap_or(s);
    let no_query = no_frag.split_once('?').map(|(h, _)| h).unwrap_or(no_frag);
    no_query.to_string()
}

fn parse_qsl(q: &str) -> Vec<(String, String)> {
    q.split('&')
        .filter(|s| !s.is_empty())
        .map(|pair| match pair.split_once('=') {
            Some((k, v)) => (k.to_string(), v.to_string()),
            None => (pair.to_string(), String::new()),
        })
        .collect()
}

fn urlencode(pairs: &[(String, String)]) -> String {
    // Python uses `urlencode(safe=":/@")`; the Python helper here only
    // produces the merged query, which is round-tripped as text. To
    // match the Python output the safe set is preserved verbatim.
    pairs
        .iter()
        .map(|(k, v)| {
            if v.is_empty() {
                k.clone()
            } else {
                format!("{}={}", k, v)
            }
        })
        .collect::<Vec<_>>()
        .join("&")
}

/// Set a URL's path while preserving the rest of the URL. `Url::set_path`
/// handles relative paths the way we want (it keeps host/scheme).
fn set_path_preserving(u: &mut Url, new_path: &str) {
    u.set_path(new_path);
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn join_at_version() {
        // `("git://x.git", "@v1") → "git://x.git@v1"` per the plan.
        let got = join_resource_url("git://x.git/", "@v1");
        // url crate normalises trailing slash; the join semantics we
        // care about is that `@v1` is appended to the base path.
        assert!(got.ends_with("@v1"), "got: {}", got);
    }

    #[test]
    fn join_no_scheme_returns_join() {
        assert_eq!(join_resource_url("x", "y"), "y");
    }

    #[test]
    fn join_absolute_returns_join() {
        assert_eq!(
            join_resource_url("git://a.com/", "https://b.com/"),
            "https://b.com/"
        );
    }

    #[test]
    fn join_bare_name_returns_join() {
        assert_eq!(join_resource_url("git://a.com/x", "bare"), "bare");
    }

    #[test]
    fn is_url_predicate() {
        assert!(is_url("git://x"));
        assert!(is_url("https://x"));
        assert!(is_url("pkg:oci/odoo"));
        assert!(!is_url("just-a-name"));
    }

    #[test]
    fn cloudmap_is_format() {
        let f = CloudMapFormat;
        assert!(f.is_format(&json!({"kind":"CloudMap"})));
        assert!(!f.is_format(&json!({"kind":"Other"})));
        assert!(!f.is_format(&json!({})));
    }

    #[test]
    fn strip_git_fragment_normalises_to_dot_git() {
        // .git suffix is preserved when present.
        assert_eq!(
            strip_git_fragment("git://unfurl.cloud/x/y.git#v1.0:."),
            Some("git://unfurl.cloud/x/y.git".to_string()),
        );
        // .git suffix is appended when missing.
        assert_eq!(
            strip_git_fragment("git://unfurl.cloud/x/y#v1.0:."),
            Some("git://unfurl.cloud/x/y.git".to_string()),
        );
        // No fragment → no strip.
        assert_eq!(strip_git_fragment("git://unfurl.cloud/x/y.git"), None);
        // Non-git URLs are not normalised.
        assert_eq!(strip_git_fragment("https://example.com/x#frag"), None,);
        assert_eq!(strip_git_fragment("pkg:oci/x?repo=y&tag=z"), None);
    }

    #[test]
    fn pointer_escape_round_trip() {
        // The escape helpers are retained even though the sync layer
        // no longer escapes record keys: keys are stored verbatim in
        // the dedicated `key` column.
        let raw = "git://example.com/x.git";
        let esc = escape_pointer_segment(raw);
        assert_eq!(esc, "git:~1~1example.com~1x.git");
        let back = CloudMapFormat::unescape_pointer_segment(&esc);
        assert_eq!(back, raw);
    }
}
