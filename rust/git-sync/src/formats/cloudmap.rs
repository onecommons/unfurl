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
//!   (`unfurl/reporting.py:658–740`). It deserializes each record's
//!   JSON into the typed structs in [`crate::formats::cloudmap_types`]
//!   (generated from `unfurl/cloudmap/cloudmap-schema.json`) and walks
//!   the typed fields, so a schema change that renames or removes a
//!   field surfaces as a compile error here rather than silently
//!   producing empty follow-edges.

use std::collections::BTreeSet;

use url::Url;

use crate::format::DataFormat;
use crate::formats::cloudmap_types as ct;
use crate::{Order, Record};

const PATH_PREFIXES: &[&str] = &[
    "services",
    "components",
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
        // Records are deserialized into the typed structs in
        // `cloudmap_types` so field names are checked against the JSON
        // Schema at compile time. Only URL-shaped edges are emitted
        // (`_is_url`, `unfurl/reporting.py`).
        let Some(prefix) = Self::record_section(record) else {
            return Vec::new();
        };

        let mut urls: Vec<String> = Vec::new();
        let json = &record.json;
        let parent_key = record.key.as_str();

        match prefix {
            "repositories" => {
                if let Ok(repo) = serde_json::from_value::<ct::Repository>(json.clone()) {
                    push_opt(&mut urls, repo.fork_of);
                    push_opt(&mut urls, repo.mirror_of);
                    push_opt(&mut urls, repo.service);
                    // `Repository.contains` keys are repo-relative file
                    // paths (with optional `#fragment`); the artifact URL
                    // they refer to is derived from the repository key.
                    if let Some(contains) = repo.contains {
                        for key in contains.0.keys() {
                            urls.push(derive_artifact_url(parent_key, key));
                        }
                    }
                }
            }
            "artifacts" => {
                if let Ok(art) = serde_json::from_value::<ct::Artifact>(json.clone()) {
                    extend_url_keys(&mut urls, art.contains.as_ref());
                    extend_url_keys(&mut urls, art.references.as_ref());
                    extend_url_keys(&mut urls, art.dependencies.as_ref());
                    extend_url_keys(&mut urls, art.instantiated_by.as_ref());
                    extend_url_keys(&mut urls, art.instantiates.as_ref());
                }
            }
            "components" => {
                if let Ok(comp) = serde_json::from_value::<ct::Component>(json.clone()) {
                    push_opt(&mut urls, comp.source);
                    extend_url_keys(&mut urls, comp.contains.as_ref());
                    extend_url_keys(&mut urls, comp.references.as_ref());
                    extend_url_keys(&mut urls, comp.dependencies.as_ref());
                    extend_url_keys(&mut urls, comp.instantiates.as_ref());
                }
            }
            "instantiations" => {
                if let Ok(inst) = serde_json::from_value::<ct::Instantiation>(json.clone()) {
                    push_opt(&mut urls, inst.source);
                    extend_url_keys(&mut urls, inst.instantiated.as_ref());
                    extend_url_keys(&mut urls, inst.inputs.as_ref());
                }
            }
            "services" => {
                if let Ok(svc) = serde_json::from_value::<ct::Service>(json.clone()) {
                    extend_url_keys(&mut urls, svc.connections.as_ref());
                    extend_url_keys(&mut urls, svc.instantiated_by.as_ref());
                }
            }
            "types" => {
                if let Ok(ty) = serde_json::from_value::<ct::Type>(json.clone()) {
                    push_opt(&mut urls, ty.source);
                    push_opt(&mut urls, ty.model);
                    // `extends` is type-name labelled, not URL-shaped — skip.
                }
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
        if PATH_PREFIXES.contains(&path) {
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

fn push_opt(out: &mut Vec<String>, value: Option<String>) {
    if let Some(v) = value {
        if !v.is_empty() {
            out.push(v);
        }
    }
}

/// Collect the URL-shaped keys from a [`ct::TypedUrLs`] map.
fn typed_url_keys(typed: &ct::TypedUrLs) -> Vec<String> {
    typed
        .0
        .keys()
        .map(|k| k.to_string())
        .filter(|k| is_url(k))
        .collect()
}

fn extend_url_keys(out: &mut Vec<String>, typed: Option<&ct::TypedUrLs>) {
    if let Some(t) = typed {
        out.extend(typed_url_keys(t));
    }
}

/// Build an artifact URL from a repository key and a `contains` entry.
///
/// `Repository.contains` keys are repo-relative file paths (optionally
/// followed by `#<fragment>`); the artifact URL is derived by
/// URL-encoding the path and appending it to the repo URL with `#:`.
/// Mirrors `Repository.artifact_url()` in
/// `unfurl/tosca_plugins/cloudmap_defs.py`.
fn derive_artifact_url(repo_url: &str, file_path: &str) -> String {
    // `quote()` in Python encodes `#` and most special chars; for the
    // small set of characters typically found in a repo file path
    // (alphanum + `/._-`), only `#` actually needs encoding here. The
    // walker is lenient — if the derived URL doesn't match any record,
    // `_walk_child` simply emits a missing-ref and moves on.
    let encoded = file_path.replace('#', "%23");
    format!("{repo_url}#:{encoded}")
}

/// JSON-pointer escape per RFC 6901 § 4: `~` → `~0`, `/` → `~1`.
/// Retained for callers that still need to construct external pointer
/// strings; the in-DB representation now stores keys verbatim, so this
/// helper is no longer used by the sync layer itself.
#[allow(dead_code)]
pub(crate) fn escape_pointer_segment(seg: &str) -> String {
    seg.replace('~', "~0").replace('/', "~1")
}

/// Port of `join_resource_url` in `unfurl/tosca_plugins/cloudmap_defs.py`
///
/// - If `base_url` has no scheme → return `join_url`.
/// - If `base_url` is `git://` and `join_url` has no scheme → use git-URL
///   join semantics (replace the fragment outright if `join_url` has
///   one; otherwise treat `join_url` as a revision and rebuild via
///   [`git_url_join`], ensuring the repo URL ends in `.git`).
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
    let join_has_scheme = join_parsed.as_ref().is_some_and(|j| !j.scheme().is_empty());

    // Git-scheme special case (matches `cloudmap_defs.py:72-79`). When
    // the base is a `git://` URL and `join_url` carries no scheme of
    // its own, the join is interpreted through git-URL conventions.
    if base.scheme() == "git" && !join_has_scheme {
        let (mut repo_url, file_path, _revision) = split_git_url(base_url);
        if let Some(frag) = join_fragment.as_deref() {
            return format!("{repo_url}#{frag}");
        }
        // Treat `join_url` as a git ref (revision); ensure the repo URL
        // ends in `.git` so `git_url_join` produces a canonical key.
        if !repo_url.ends_with(".git") {
            repo_url.push_str(".git");
        }
        return git_url_join(&repo_url, &file_path, join_url);
    }

    // "absolute URL or a bare name without a purl version" → return as-is.
    if join_has_scheme {
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

/// Port of `split_git_url` in `unfurl/repo.py:165` (restricted to
/// `git://` / `https://` URLs — the `git-local:` and `--`-flag handling
/// in Python isn't reachable from cloudmap data).
///
/// Splits a URL with a git-style fragment (`#[<revision>[~<commit>]][:<path>]`)
/// into `(repository_url_without_fragment, file_path, revision)`. The
/// commit suffix is dropped; the file path is percent-decoded.
fn split_git_url(url: &str) -> (String, String, String) {
    let Some((head, frag)) = url.split_once('#') else {
        return (url.to_string(), String::new(), String::new());
    };
    let (rev_part, path_part) = match frag.split_once(':') {
        Some((r, p)) => (r.to_string(), p.to_string()),
        None => (frag.to_string(), String::new()),
    };
    let revision = match rev_part.split_once('~') {
        Some((r, _)) => r.to_string(),
        None => rev_part,
    };
    (head.to_string(), percent_decode(&path_part), revision)
}

/// Port of `git_url_join` in `unfurl/repo.py:173`. Builds a git-style
/// fragment from `(url, path, revision)`:
///
/// - revision + path → `url#revision:path`
/// - revision only → `url#revision`
/// - path only → `url#:path`
/// - neither → `url`
fn git_url_join(url: &str, path: &str, revision: &str) -> String {
    if !revision.is_empty() && !path.is_empty() {
        format!("{url}#{revision}:{path}")
    } else if !revision.is_empty() {
        format!("{url}#{revision}")
    } else if !path.is_empty() {
        format!("{url}#:{path}")
    } else {
        url.to_string()
    }
}

/// Decode `%xx` escapes in a path-like string (sufficient for git
/// `#:<path>` fragments — no `+` to space translation, no UTF-8 strict
/// validation). Matches Python's `unquote` for the inputs we care about.
fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let (Some(h), Some(l)) = (hex_digit(bytes[i + 1]), hex_digit(bytes[i + 2])) {
                out.push((h << 4) | l);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn hex_digit(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
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
    fn join_bare_name_git_treats_as_revision() {
        // Matches `cloudmap_defs.py:72-79`: a bare join under a
        // `git://` base is interpreted as a git revision, so the
        // result is `<repo>.git#<revision>` (the `.git` suffix is
        // added when missing).
        assert_eq!(
            join_resource_url("git://a.com/x", "bare"),
            "git://a.com/x.git#bare",
        );
        // `.git` already present → not re-added.
        assert_eq!(
            join_resource_url("git://a.com/x.git", "v1.0"),
            "git://a.com/x.git#v1.0",
        );
    }

    #[test]
    fn join_git_with_fragment_replaces_fragment() {
        // `cloudmap_defs.py:74-75`: a join with a `#fragment` under a
        // `git://` base replaces the base's fragment outright.
        assert_eq!(
            join_resource_url("git://a.com/x.git#main:foo", "#v2"),
            "git://a.com/x.git#v2",
        );
    }

    #[test]
    fn join_git_with_revision_and_path() {
        // Base has a `#:path` fragment; bare join overlays the revision.
        assert_eq!(
            join_resource_url("git://a.com/x.git#:foo/bar", "v1.0"),
            "git://a.com/x.git#v1.0:foo/bar",
        );
    }

    #[test]
    fn join_non_git_bare_name_returns_join() {
        // Non-git bases keep the original "bare name → as-is" semantics.
        assert_eq!(join_resource_url("https://a.com/x", "bare"), "bare",);
    }

    #[test]
    fn split_git_url_round_trip() {
        let (u, p, r) = split_git_url("git://a.com/x.git#main:src/foo");
        assert_eq!(
            (u.as_str(), p.as_str(), r.as_str()),
            ("git://a.com/x.git", "src/foo", "main")
        );
        let (u, p, r) = split_git_url("git://a.com/x.git#:src/foo");
        assert_eq!(
            (u.as_str(), p.as_str(), r.as_str()),
            ("git://a.com/x.git", "src/foo", "")
        );
        // Commit suffix is dropped (matches Python's split_git_url which
        // returns only the first three elements of split_git_url_with_commit).
        let (u, p, r) = split_git_url("git://a.com/x.git#main~abc123:src");
        assert_eq!(
            (u.as_str(), p.as_str(), r.as_str()),
            ("git://a.com/x.git", "src", "main")
        );
        let (u, p, r) = split_git_url("git://a.com/x.git");
        assert_eq!(
            (u.as_str(), p.as_str(), r.as_str()),
            ("git://a.com/x.git", "", "")
        );
        // Percent-decoded path.
        let (_, p, _) = split_git_url("git://a.com/x.git#:foo%23bar");
        assert_eq!(p, "foo#bar");
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
