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

/// `apiVersion` stamped on a cloudmap this crate creates. Matches
/// `unfurl.util.API_VERSION`; the schema's enum also still accepts the older
/// `unfurl/v1alpha1` for documents written elsewhere.
const API_VERSION: &str = "unfurl/v1.0.0";

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

    fn new_document(&self) -> serde_json::Value {
        // Same header as Python's `CloudMapDB._load` default_db. Both keys
        // are `required` by `unfurl/cloudmap/cloudmap-schema.json`, and
        // `kind` is what `is_format` above matches on.
        serde_json::json!({ "apiVersion": API_VERSION, "kind": "CloudMap" })
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
                    extend_url_keys(&mut urls, comp.contains.as_ref());
                    extend_url_keys(&mut urls, comp.references.as_ref());
                    extend_url_keys(&mut urls, comp.dependencies.as_ref());
                    extend_url_keys(&mut urls, comp.instantiates.as_ref());
                    extend_url_keys(&mut urls, comp.instantiated_by.as_ref());
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
    let head = &url[..fragment_start(url)?];
    // an expression can expand into the ".git" suffix, so leave it alone
    if head.ends_with(".git") || trailing_template(head).is_some() {
        Some(head.to_string())
    } else {
        Some(format!("{head}.git"))
    }
}

/// A URL starts with a valid URI scheme followed by `:` (RFC 3986 § 3.1:
/// `ALPHA *( ALPHA / DIGIT / "+" / "-" / "." )`) or with a URI template
/// expression, which can expand into the scheme.
/// Simple test to distinguish global type names from URLs; the Python side
/// makes the same distinction with `is_label` in
/// `unfurl/tosca_plugins/cloudmap_defs.py` (a key that isn't a label is a URL).
/// This matches the `typedURLs` propertyNames pattern in `unfurl/cloudmap/cloudmap-schema.json`.
pub fn is_url(s: &str) -> bool {
    // a URI template expands into a url, and can expand into the scheme itself
    // (e.g. "{+urlvar}"), so a key containing one is a url rather than a label
    url_scheme(s).is_some() || has_uri_template(s)
}

/// Port of `url_scheme` in `unfurl/tosca_plugins/cloudmap_defs.py`: the scheme
/// of `s` (RFC 3986 § 3.1: `ALPHA *( ALPHA / DIGIT / "+" / "-" / "." )`), or
/// None if it doesn't have one.
fn url_scheme(s: &str) -> Option<&str> {
    let (scheme, _rest) = s.split_once(':')?;
    let mut chars = scheme.chars();
    match chars.next() {
        Some(c) if c.is_ascii_alphabetic() => {}
        _ => return None,
    }
    chars
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
        .then_some(scheme)
}

/// Predicate from `is_label` in `unfurl/tosca_plugins/cloudmap_defs.py`:
/// A key is a label if every character matches `[\w.-]`. URLs and file paths
/// (which contain `:` or `/`) are not labels.
pub fn is_label(s: &str) -> bool {
    !s.is_empty()
        && s.chars()
            .all(|c| c.is_alphanumeric() || matches!(c, '_' | '-' | '.'))
}

fn push_opt(out: &mut Vec<String>, value: Option<String>) {
    if let Some(v) = value {
        if !v.is_empty() {
            out.push(v);
        }
    }
}

/// Collect the URL-shaped targets from a [`ct::TypedUrLs`] map.
///
/// Mirrors `_walk_typed_urls` in `unfurl/reporting.py`.
fn extend_url_keys(out: &mut Vec<String>, typed: Option<&ct::TypedUrLs>) {
    if let Some(t) = typed {
        for (key, value) in t.0.iter() {
            let key = key.to_string();
            if is_url(&key) {
                out.push(key);
            } else if let ct::TypedUrLsValue::Variant2(map) = value {
                for nested_key in map.keys() {
                    let nested_key = nested_key.to_string();
                    if is_url(&nested_key) {
                        out.push(nested_key);
                    }
                }
            }
        }
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

/// Operators a URI template expression can start with (RFC 6570 § 2.2). The
/// ones the RFC reserves for future extensions ("=,!@|") aren't valid in a
/// template, so an expression starting with one has no operator as far as this
/// is concerned.
const TEMPLATE_OPERATORS: &str = "+#./;?&";
/// Operators that set the same part of a url as the operator they are listed for.
const QUERY_OPERATORS: &[char] = &['?', '&'];
const FRAGMENT_OPERATORS: &[char] = &['#'];

/// Port of `has_uri_template` in `unfurl/tosca_plugins/cloudmap_defs.py`.
fn has_uri_template(s: &str) -> bool {
    match s.find('{') {
        Some(start) => s[start..].contains('}'),
        None => false,
    }
}

/// Split a URI template expression at the start of `s` into its operator (if
/// any) and its variable list, e.g. `"{?tag,digest}"` → `(Some('?'), "tag,digest")`.
///
/// Ports the `_URI_TEMPLATE_EXPRESSION.match()` call in `join_resource_url`.
fn leading_template_expression(s: &str) -> Option<(Option<char>, &str)> {
    let body = s.strip_prefix('{')?;
    let end = body.find('}')?;
    let body = &body[..end];
    match body.chars().next() {
        Some(operator) if TEMPLATE_OPERATORS.contains(operator) => {
            Some((Some(operator), &body[operator.len_utf8()..]))
        }
        _ => Some((None, body)),
    }
}

/// Port of `_split_url_parts` in `unfurl/tosca_plugins/cloudmap_defs.py`:
/// split `url` into the part before its query, its query and its fragment.
///
/// A "?" or "#" inside a URI template expression is part of the expression, not
/// a delimiter, so [`Url`] can't be used here.
fn split_url_parts(url: &str) -> (&str, &str, &str) {
    let fragment_at = fragment_start(url);
    let end = fragment_at.unwrap_or(url.len());
    let fragment = fragment_at.map_or("", |i| &url[i + 1..]);
    let head = &url[..end];
    match delimiter_start(head, '?') {
        Some(i) => (&head[..i], &head[i + 1..], fragment),
        None => (head, "", fragment),
    }
}

/// The offset of the "#" that starts `url`'s fragment, if it has one.
///
/// Ports `split_url_fragment` in `unfurl/util.py`.
fn fragment_start(url: &str) -> Option<usize> {
    delimiter_start(url, '#')
}

/// The offset of the first `delimiter` in `url` that isn't the operator of a
/// URI template expression.
///
/// An expression's operator is the character after its "{" (`{#var}`,
/// `{?var}`), so a delimiter there belongs to the expression.
fn delimiter_start(url: &str, delimiter: char) -> Option<usize> {
    url.char_indices()
        .find(|&(i, c)| c == delimiter && !url[..i].ends_with('{'))
        .map(|(i, _)| i)
}

/// The offset of a trailing URI template expression in `part` and its operator,
/// if `part` ends with one.
fn trailing_template(part: &str) -> Option<(usize, Option<char>)> {
    let start = part.rfind('{')?;
    let body = part.strip_suffix('}')?.get(start + 1..)?;
    if body.contains('{') || body.contains('}') {
        return None;
    }
    let operator = body
        .chars()
        .next()
        .filter(|c| TEMPLATE_OPERATORS.contains(*c));
    Some((start, operator))
}

/// Port of `_strip_trailing_template`: the offset of a trailing expression in
/// `part` whose operator sets the same part of a url, if there is one.
fn trailing_template_start(part: &str, operators: &[char]) -> Option<usize> {
    let (start, operator) = trailing_template(part)?;
    // an expression without an operator doesn't set a part of the url
    operators.contains(&operator?).then_some(start)
}

/// Port of `_join_template_url` in `unfurl/tosca_plugins/cloudmap_defs.py`:
/// merge a version key that starts with a URI template expression into `base_url`.
///
/// The expression's operator says which part of the base url the key sets: "?"
/// and "&" the query, "#" the fragment, and "/", ";" and "." the path. "+"
/// expands to reserved characters too, so the key can be a whole url and
/// doesn't say what part of the url it is; such a key is returned unchanged.
fn join_template_url(base_url: &str, join_url: &str, operator: char, varnames: &str) -> String {
    if operator == '+' {
        return join_url.to_string();
    }
    let (mut prefix, base_query, base_fragment) = split_url_parts(base_url);
    let mut query = base_query.to_string();
    let mut fragment = base_fragment;
    let mut join_url = join_url.to_string();

    if operator == '#' {
        fragment = "";
    } else if matches!(operator, '?' | '&') && !query.is_empty() && !has_uri_template(&query) {
        // Drop the parameters the expression sets, the way literal keys merge.
        let names: BTreeSet<&str> = varnames
            .split(',')
            .map(|name| name.split(':').next().unwrap_or(name).trim_end_matches('*'))
            .collect();
        let kept: Vec<(String, String)> = parse_qsl(&query)
            .into_iter()
            .filter(|(key, _)| !names.contains(key.as_str()))
            .collect();
        query = urlencode(&kept);
    }

    // A url has one query and one fragment, so an expression already setting
    // that part is replaced instead of appended to.
    if matches!(operator, '?' | '&' | '#') {
        let same_part = if operator == '#' {
            FRAGMENT_OPERATORS
        } else {
            QUERY_OPERATORS
        };
        if !query.is_empty() && operator != '#' {
            if let Some(start) = trailing_template_start(&query, same_part) {
                query.truncate(start);
            }
        } else if let Some(start) = trailing_template_start(prefix, same_part) {
            prefix = &prefix[..start];
        }
    }

    if matches!(operator, '?' | '&') {
        // A query string starts with "?" and continues with "&".
        let wanted = if query.is_empty() { '?' } else { '&' };
        if operator != wanted {
            join_url = format!("{{{wanted}{}", &join_url[1 + operator.len_utf8()..]);
        }
    }

    let mut joined = String::from(prefix);
    if matches!(operator, '/' | ';' | '.') {
        joined.push_str(&join_url); // a path segment, parameter or label
    }
    if !query.is_empty() {
        joined.push('?');
        joined.push_str(&query);
    }
    if matches!(operator, '?' | '&') {
        joined.push_str(&join_url);
    }
    if !fragment.is_empty() {
        joined.push('#');
        joined.push_str(fragment);
    }
    if operator == '#' {
        joined.push_str(&join_url);
    }
    joined
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
    if url_scheme(base_url).is_none() {
        // Python's `not url_scheme(base_url)` check.
        return join_url.to_string();
    }
    if let Some((Some(operator), varnames)) = leading_template_expression(join_url) {
        // The key is a URI template; its operator says what part of the url it
        // is. `Url` normalises away (and percent-encodes) expressions, so this
        // is merged textually. (The default operator is percent-encoded so it
        // expands to a single value, like a bare version key: fall through and
        // treat it as one.)
        return join_template_url(base_url, join_url, operator, varnames);
    }
    let Ok(base) = Url::parse(base_url) else {
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
    let Some(at) = fragment_start(url) else {
        return (url.to_string(), String::new(), String::new());
    };
    let (head, frag) = (&url[..at], &url[at + 1..]);
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
    fn join_uri_template_version_keys() {
        // "?" starts a query string, "&" continues one -- either way the
        // parameters the expression sets replace the base's, like literal
        // query keys do.
        let purl = "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest";
        let merged = "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo{&tag}";
        assert_eq!(join_resource_url(purl, "{?tag}"), merged);
        assert_eq!(join_resource_url(purl, "{&tag}"), merged);
        assert_eq!(
            join_resource_url("pkg:oci/name?tag=base1&tag=base2&keep=yes", "{?tag,digest}"),
            "pkg:oci/name?keep=yes{&tag,digest}"
        );
        assert_eq!(
            join_resource_url("https://example.com/app", "{?tag}"),
            "https://example.com/app{?tag}"
        );
        assert_eq!(
            join_resource_url("https://example.com/app?a=1#frag", "{?tag}"),
            "https://example.com/app?a=1{&tag}#frag"
        );
        // A url has one query and one fragment, so an expression setting the
        // same part of the base is replaced, not appended to.
        assert_eq!(
            join_resource_url("https://example.com/app{?tag,digest}", "{?tag}"),
            "https://example.com/app{?tag}"
        );
        assert_eq!(
            join_resource_url("https://example.com/app?a=1{&tag}", "{?tag}"),
            "https://example.com/app?a=1{&tag}"
        );
        assert_eq!(
            join_resource_url("https://example.com/app{#version}", "{#v2}"),
            "https://example.com/app{#v2}"
        );
        // "#" is the fragment, "/", ";" and "." extend the path.
        assert_eq!(
            join_resource_url("https://example.com/app#v1", "{#version}"),
            "https://example.com/app{#version}"
        );
        assert_eq!(
            join_resource_url("git://x.com/r.git", "{#branch}"),
            "git://x.com/r.git{#branch}"
        );
        assert_eq!(
            join_resource_url("https://example.com/app?a=1", "{/segment}"),
            "https://example.com/app{/segment}?a=1"
        );
        assert_eq!(
            join_resource_url("https://example.com/app", "{;matrix}"),
            "https://example.com/app{;matrix}"
        );
        assert_eq!(
            join_resource_url("https://example.com/app", "{.label}"),
            "https://example.com/app{.label}"
        );
        // "+" can expand into a whole url, so the key is used as-is.
        assert_eq!(
            join_resource_url("https://example.com/app", "{+urlvar}"),
            "{+urlvar}"
        );
        assert_eq!(
            join_resource_url("git://a.com/x.git", "{+urlvar}"),
            "{+urlvar}"
        );
        // The default operator expands to a single percent-encoded value, so
        // the key is merged like a bare version key: a git ref for a git url,
        // else as-is.
        assert_eq!(
            join_resource_url("https://example.com/app", "{version}"),
            "{version}"
        );
        assert_eq!(
            join_resource_url("git://a.com/x.git", "{version}"),
            "git://a.com/x.git#{version}"
        );
        // The operators RFC 6570 reserves aren't valid, so those keys are plain too.
        assert_eq!(
            join_resource_url("git://a.com/x.git", "{=var}"),
            "git://a.com/x.git#{=var}"
        );
        assert_eq!(
            join_resource_url("https://example.com/app", "{,var}"),
            "{,var}"
        );
        // A templated base isn't parsed as a url, so it survives verbatim.
        assert_eq!(
            join_resource_url("https://example.com/{v}/app", "{?tag}"),
            "https://example.com/{v}/app{?tag}"
        );
    }

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
        assert!(is_url("git+https://x"));
        assert!(!is_url("just-a-name"));
        assert!(!is_url(""));
        // type names contain ':' (namespace) but the prefix is not a scheme
        assert!(!is_url("WebApp@unfurl.cloud/onecommons/std:generic_types"));
        assert!(!is_url("software.Nginx"));
        // a leading ':' or non-alpha scheme start is not a url
        assert!(!is_url(":foo"));
        assert!(!is_url("1abc:foo"));
        // a URI template can expand into a url (Python's is_label agrees:
        // these keys aren't labels, so they are urls)
        assert!(is_url("{+urlvar}"));
        assert!(is_url("docker.io/{name}"));
        assert!(is_url("{version}"));
        assert!(!is_url("v1.0"));
    }

    #[test]
    fn git_fragment_ignores_uri_templates() {
        // a "#" inside an expression is part of the expression, not the fragment
        let (u, p, r) = split_git_url("git://a.com/x.git{#ref}");
        assert_eq!(
            (u.as_str(), p.as_str(), r.as_str()),
            ("git://a.com/x.git{#ref}", "", "")
        );
        let (u, p, r) = split_git_url("git://a.com/x.git#{+ref}:src/{name}");
        assert_eq!(
            (u.as_str(), p.as_str(), r.as_str()),
            ("git://a.com/x.git", "src/{name}", "{+ref}")
        );
        assert_eq!(strip_git_fragment("git://a.com/x.git{#ref}"), None);
        // an expression can expand into the ".git" suffix, so it isn't added
        assert_eq!(
            strip_git_fragment("git://a.com/x{#ref}#v1.0:."),
            Some("git://a.com/x{#ref}".to_string())
        );
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
