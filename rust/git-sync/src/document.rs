// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Reading and writing the documents records live in.
//!
//! Two halves of one round trip. [`Syntax`] decides how a file's bytes
//! parse and serialize, chosen by extension; the rest applies a record
//! change to a parsed document and puts it back on disk, keeping the
//! diff down to what actually changed -- untouched keys keep their
//! position, and comments survive wherever a section can be spliced
//! rather than re-emitted.
//!
//! Which *schema* a document holds is a separate question, answered
//! after parsing by [`crate::DataFormat`] inspecting the value.

use std::io::Write;
use std::path::Path;

use crate::error::{Error, Result};

/// A parsed document, and whether reading it needed more than strict
/// JSON.
pub(crate) struct Parsed {
    pub(crate) value: serde_json::Value,
    /// The file used JSON5 syntax — a comment, a trailing comma, an
    /// unquoted key — that strict JSON rejects.
    ///
    /// Worth surfacing because a rewrite emits strict JSON, so this says
    /// the file is about to be normalized and its comments dropped.
    /// Always false for YAML, where the question does not arise.
    pub(crate) extended: bool,
}

/// The concrete syntax of a tracked file, chosen by its extension.
///
/// One authority for both halves of the round trip. The read scan and
/// the write path used to decide separately, and the scan's "anything
/// that isn't json is yaml" fallback meant a new extension added to only
/// one of them would be silently misparsed rather than rejected.
///
/// Which *schema* a file holds is a separate question, answered after
/// parsing by [`crate::DataFormat`] inspecting the value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Syntax {
    Yaml,
    Json,
    /// JSON5, which also covers JSONC — comments and trailing commas are
    /// the whole of JSONC, and JSON5 is a superset of it.
    Json5,
}

impl Syntax {
    /// `None` for an extension this crate does not read, which is how
    /// the scan skips a file rather than guessing at its syntax.
    pub(crate) fn for_extension(ext: &str) -> Option<Self> {
        match ext {
            "yaml" | "yml" => Some(Self::Yaml),
            // `.json` reads leniently too. Most JSON-with-comments in
            // the wild lives in a plain `.json` -- VS Code settings,
            // tsconfig -- so rejecting those would miss the common case
            // while accepting the rarer explicit spellings.
            "json" => Some(Self::Json),
            "json5" | "jsonc" => Some(Self::Json5),
            _ => None,
        }
    }

    /// Parse to a `serde_json::Value`. The crate enables
    /// `serde_json/preserve_order`, so object keys keep their on-disk
    /// ordering whichever syntax they came from.
    ///
    /// Both JSON dialects try the strict parser first and fall back to
    /// JSON5, which is a strict superset — so success on the first
    /// attempt *is* the answer to "was this strict JSON", reported as
    /// [`Parsed::extended`]. There is no other way to know: a
    /// `serde_json::Value` retains no trace of quoting style, trailing
    /// commas or comments, and the json5 crate exposes nothing about
    /// what syntax it consumed.
    ///
    /// Which parser's error is reported when both fail depends on what
    /// the file claimed to be. A broken `.json` deserves the JSON error;
    /// a JSON5 message about a file nobody meant to be JSON5 would point
    /// at the wrong thing.
    pub(crate) fn parse(self, file_path: &str, bytes: &[u8]) -> Result<Parsed> {
        let text = || {
            std::str::from_utf8(bytes)
                .map_err(|e| Error::Other(format!("{file_path}: file is not valid utf-8: {e}")))
        };
        match self {
            Self::Yaml => Ok(Parsed {
                value: serde_saphyr::from_str(text()?).map_err(|e| Error::Yaml {
                    path: file_path.to_string(),
                    message: e.to_string(),
                })?,
                extended: false,
            }),
            Self::Json | Self::Json5 => match serde_json::from_slice(bytes) {
                Ok(value) => Ok(Parsed {
                    value,
                    extended: false,
                }),
                Err(strict) => match (json5::from_str(text()?), self) {
                    (Ok(value), _) => Ok(Parsed {
                        value,
                        extended: true,
                    }),
                    (Err(_), Self::Json) => Err(Error::Json {
                        path: file_path.to_string(),
                        source: strict,
                    }),
                    (Err(loose), _) => {
                        Err(Error::Other(format!("{file_path}: invalid json5: {loose}")))
                    }
                },
            },
        }
    }

    /// Render a value back out.
    ///
    /// JSON5 and JSONC are written as strict pretty JSON, which is
    /// valid in both. `json5::to_string` is not used even though it
    /// indents: its output is JSON5-flavoured — unquoted keys, trailing
    /// commas — which a plain `.json` must not receive, and both
    /// dialects share this arm.
    ///
    /// The rewrite is lossy for either in the same way it already is
    /// for YAML — the document round-trips through a
    /// `serde_json::Value`, which holds no comments, so comments
    /// anywhere in the file are dropped when any record in it changes.
    /// `json5`'s deserializer discards them at parse time
    /// (`skip_comment`) and serde's data model has nowhere to put them,
    /// so no serializer choice would preserve them; that needs a
    /// lossless parser with byte spans, as the YAML path gets from
    /// [`crate::template`].
    pub(crate) fn serialize(self, root: &serde_json::Value, file_path: &str) -> Result<Vec<u8>> {
        match self {
            Self::Yaml => {
                let s = serde_saphyr::to_string(root).map_err(|e| Error::Yaml {
                    path: file_path.to_string(),
                    message: e.to_string(),
                })?;
                Ok(crate::util::elide_explicit_nulls(&s).into_bytes())
            }
            Self::Json | Self::Json5 => serde_json::to_vec_pretty(root).map_err(|e| Error::Json {
                path: file_path.to_string(),
                source: e,
            }),
        }
    }

    /// Parse `bytes` as a document to apply records to: [`Self::parse`],
    /// but guaranteed to be an object.
    ///
    /// A root that is a sequence or a scalar is replaced with an empty
    /// object rather than rejected — there is nowhere in it to put a
    /// record, so treating it as an empty document is the only reading
    /// under which the write means anything. The records then overwrite
    /// it, which is deliberate: the file did not hold this format.
    pub(crate) fn into_value(self, file_path: &str, bytes: &[u8]) -> Result<serde_json::Value> {
        let root = self.parse(file_path, bytes)?.value;
        Ok(if root.is_object() {
            root
        } else {
            serde_json::Value::Object(serde_json::Map::new())
        })
    }

    /// The bytes to write for a document the pending records have been
    /// applied to, keeping as much of the original file as this syntax
    /// allows.
    ///
    /// Only YAML splices. [`Template`](crate::template::Template)
    /// locates block-style bodies, and `serde_json::to_vec_pretty` puts
    /// every JSON value on its key's line, so a JSON splice would find
    /// no section to replace — and if one ever did, the re-parse check
    /// inside [`splice_touched_sections`] would reject it. Saying so
    /// here rather than leaning on that is the point of matching on the
    /// syntax: for JSON the whole document is re-emitted, and that is
    /// the design, not an accident.
    pub(crate) fn render_document(
        self,
        source: Option<&str>,
        root: &mut serde_json::Value,
        format: Option<&dyn crate::DataFormat>,
        touched: &[String],
        file_path: &str,
    ) -> Result<Vec<u8>> {
        apply_format_ordering(root, format, touched);
        let bytes = self.serialize(root, file_path)?;
        match self {
            // Re-emitting the whole document is correct but drops every
            // comment in it. Where the shape allows, keep the original
            // bytes and swap in only the sections that changed.
            Self::Yaml => Ok(source
                .and_then(|src| splice_touched_sections(src, &bytes, touched))
                .unwrap_or(bytes)),
            // Nothing to preserve: the document round-tripped through a
            // `serde_json::Value`, so any comment or JSON5 spelling was
            // already gone before serialization. See `Self::serialize`.
            Self::Json | Self::Json5 => Ok(bytes),
        }
    }
}

/// Lower-cased extension of `file_path`, or empty when there isn't one.
pub(crate) fn extract_ext(file_path: &str) -> String {
    file_path
        .rsplit_once('.')
        .map(|(_, e)| e.to_ascii_lowercase())
        .unwrap_or_default()
}

/// The document to start from when the file is not on disk yet: the
/// format's header, or an empty object when no format claims the
/// records.
///
/// Whatever [`crate::DataFormat::is_format`] inspects lives in
/// [`new_document`](crate::DataFormat::new_document); a file synthesised
/// without it would not be recognised by the next
/// [`crate::SyncedRepo::update_from_working_dir`] and its records would
/// drop out of the index.
pub(crate) fn new_root(format: Option<&dyn crate::DataFormat>) -> serde_json::Value {
    match format.map(crate::DataFormat::new_document) {
        Some(header @ serde_json::Value::Object(_)) => header,
        _ => serde_json::Value::Object(serde_json::Map::new()),
    }
}

/// Apply `format`'s per-section ordering policy to the sections this batch
/// wrote into. No-op when the records matched no registered format, or when
/// the format opts out of sorting for a section.
pub(crate) fn apply_format_ordering(
    root: &mut serde_json::Value,
    format: Option<&dyn crate::DataFormat>,
    touched_sections: &[String],
) {
    let (Some(fmt), Some(root_obj)) = (format, root.as_object_mut()) else {
        return;
    };
    for section_name in touched_sections {
        if !matches!(fmt.get_order(section_name), crate::Order::Sort) {
            continue;
        }
        if let Some(section_obj) = root_obj
            .get_mut(section_name.as_str())
            .and_then(|v| v.as_object_mut())
        {
            section_obj.sort_keys();
        }
    }
}

/// Remove `key` from `root_obj[section_name]`. Drops the section
/// entirely when it becomes empty. Uses `shift_remove` (not
/// `remove`/`swap_remove`) so the order of the surviving entries is
/// preserved — critical for the "minimally-edited" output the tests
/// assert against.
pub(crate) fn apply_delete(
    root_obj: &mut serde_json::Map<String, serde_json::Value>,
    section_name: &str,
    key: &str,
) {
    if let Some(section) = root_obj
        .get_mut(section_name)
        .and_then(|v| v.as_object_mut())
    {
        section.shift_remove(key);
        if section.is_empty() {
            root_obj.shift_remove(section_name);
        }
    }
}

/// Insert or replace `root_obj[section_name][key] = json`, creating
/// the section if it's missing and replacing any non-object value.
pub(crate) fn apply_insert(
    root_obj: &mut serde_json::Map<String, serde_json::Value>,
    section_name: &str,
    key: String,
    json: serde_json::Value,
    format: Option<&dyn crate::DataFormat>,
) {
    let section = root_obj
        .entry(section_name.to_string())
        .or_insert_with(|| serde_json::Value::Object(serde_json::Map::new()));
    if !section.is_object() {
        *section = serde_json::Value::Object(serde_json::Map::new());
    }
    let section = section.as_object_mut().expect("section is object");
    let json = match section.get(&key) {
        Some(previous) => reorder_like(previous, json),
        // Nothing on disk to copy an order from, so fall back to the
        // format's canonical one.
        None => match format {
            Some(fmt) => order_fields(json, fmt.field_order(section_name)),
            None => json,
        },
    };
    section.insert(key, json);
}

/// Emit `json`'s top-level keys in `order`, appending any it doesn't
/// name in the order they arrived. A non-object, or an empty `order`,
/// passes through untouched.
fn order_fields(json: serde_json::Value, order: &[&str]) -> serde_json::Value {
    let serde_json::Value::Object(mut object) = json else {
        return json;
    };
    if order.is_empty() {
        return serde_json::Value::Object(object);
    }
    let mut out = serde_json::Map::with_capacity(object.len());
    for key in order {
        if let Some(value) = object.shift_remove(*key) {
            out.insert((*key).to_string(), value);
        }
    }
    out.extend(object);
    serde_json::Value::Object(out)
}

/// Re-key `next` to follow `previous`'s key order, recursively.
///
/// The database is an index of the file, not its author. A record read
/// back out carries whatever order the backend stored it in — the
/// writing client's on SQLite, and on Postgres `JSONB`'s normalised
/// order (keys sorted by length, then bytewise). Neither is the order
/// the file was written in, so writing a record back verbatim would
/// rewrite its whole block instead of the field that changed.
///
/// Mirroring the on-disk block keeps the diff down to the actual edit,
/// and does it at every depth: nested objects, maps keyed by data
/// rather than by schema, and objects nested inside arrays all keep the
/// order the file already had. Keys `previous` doesn't have are
/// appended in the order they arrived, so nothing is dropped and
/// additions still show up in the diff.
///
/// A record with no counterpart on disk has nothing to mirror and is
/// written in the order it arrived; see [`crate::DataFormat`] for the
/// canonical field order applied to those.
fn reorder_like(previous: &serde_json::Value, next: serde_json::Value) -> serde_json::Value {
    use serde_json::Value;
    match (previous, next) {
        (Value::Object(previous), Value::Object(mut next)) => {
            let mut out = serde_json::Map::with_capacity(next.len());
            for (key, previous_value) in previous {
                if let Some(value) = next.shift_remove(key) {
                    out.insert(key.clone(), reorder_like(previous_value, value));
                }
            }
            // Whatever is left is new to the file; keep it in arrival order.
            out.extend(next);
            Value::Object(out)
        }
        // Arrays keep their element order (both backends preserve it),
        // but objects *inside* them are subject to the same rewrite, so
        // pair them up positionally.
        (Value::Array(previous), Value::Array(next)) => Value::Array(
            next.into_iter()
                .enumerate()
                .map(|(i, value)| match previous.get(i) {
                    Some(previous_value) => reorder_like(previous_value, value),
                    None => value,
                })
                .collect(),
        ),
        (_, next) => next,
    }
}

/// Rebuild `rendered` as the original `src` with only `touched`
/// sections replaced, so comments and formatting elsewhere survive.
///
/// `None` whenever that cannot be done confidently -- either document
/// failing to split into sections, a touched section missing from one of
/// them, or the result not matching what the caller was going to write.
/// Every one of those falls back to `rendered`, which is what the crate
/// did before this existed.
///
/// The last check is the important one. It compares the spliced document
/// against the intended one *as parsed values*, so a splice that lands
/// wrongly -- for any reason, including ones not anticipated here -- is
/// discarded rather than written. That is what makes this safe to
/// attempt at all: being wrong costs a comment, never a document.
pub(crate) fn splice_touched_sections(
    src: &str,
    rendered: &[u8],
    touched: &[String],
) -> Option<Vec<u8>> {
    let rendered = std::str::from_utf8(rendered).ok()?;
    let original = crate::template::Template::parse(src)?;
    let updated = crate::template::Template::parse(rendered)?;

    let mut replacements = std::collections::HashMap::new();
    for name in touched {
        let from = updated.section(name)?;
        original.section(name)?;
        replacements.insert(
            name.as_str(),
            crate::template::dedent_block(updated.body(from), from.indent),
        );
    }
    let spliced = original.render(&replacements);

    let want: serde_json::Value = serde_saphyr::from_str(rendered).ok()?;
    let got: serde_json::Value = serde_saphyr::from_str(&spliced).ok()?;
    (got == want).then(|| spliced.into_bytes())
}

/// Atomic-replace `abs` with `bytes` via a tempfile in the same
/// directory. Creates the parent directory if needed.
pub(crate) fn stage_write(abs: &Path, bytes: &[u8]) -> Result<tempfile::NamedTempFile> {
    let dir = abs.parent().unwrap_or_else(|| Path::new("."));
    std::fs::create_dir_all(dir)?;
    let mut tmp = tempfile::NamedTempFile::new_in(dir)?;
    tmp.write_all(bytes)?;
    tmp.flush()?;
    // `flush` only empties userspace buffers. Without this the rename can
    // be durable while the contents are not, leaving a truncated file
    // where the database has recorded the full one.
    tmp.as_file().sync_data()?;
    Ok(tmp)
}

#[cfg(test)]
mod tests {
    use super::reorder_like;
    use serde_json::json;

    /// Top-level keys of a JSON object, in order.
    fn keys(value: &serde_json::Value) -> Vec<&str> {
        value
            .as_object()
            .expect("object")
            .keys()
            .map(String::as_str)
            .collect()
    }

    #[test]
    fn reorder_like_mirrors_the_previous_key_order() {
        let previous = json!({"path": "p", "name": "n", "metadata": {"description": "d"}});
        let next = json!({"metadata": {"description": "d2"}, "name": "n2", "path": "p2"});
        let out = reorder_like(&previous, next);
        assert_eq!(keys(&out), ["path", "name", "metadata"]);
        assert_eq!(out["name"], "n2", "values come from `next`");
    }

    #[test]
    fn reorder_like_appends_keys_the_file_lacks() {
        let previous = json!({"path": "p", "name": "n"});
        let next = json!({"tags": {}, "name": "n", "branches": {}, "path": "p"});
        let out = reorder_like(&previous, next);
        assert_eq!(
            keys(&out),
            ["path", "name", "tags", "branches"],
            "known keys first, new ones in arrival order"
        );
    }

    #[test]
    fn reorder_like_recurses_into_nested_and_data_keyed_maps() {
        // `metadata` is schema-shaped, `contains` is keyed by data --
        // mirroring the file covers both without knowing the difference.
        let previous = json!({
            "metadata": {"description": "d", "homepage_url": "h", "issues_url": "i"},
            "contains": {".gitlab-ci.yml": null, "unfurl.yaml": null},
        });
        let next = json!({
            "contains": {"unfurl.yaml": null, ".gitlab-ci.yml": null},
            "metadata": {"issues_url": "i", "description": "d", "homepage_url": "h2"},
        });
        let out = reorder_like(&previous, next);
        assert_eq!(keys(&out), ["metadata", "contains"]);
        assert_eq!(
            keys(&out["metadata"]),
            ["description", "homepage_url", "issues_url"]
        );
        assert_eq!(keys(&out["contains"]), [".gitlab-ci.yml", "unfurl.yaml"]);
    }

    #[test]
    fn reorder_like_pairs_array_elements_positionally() {
        let previous = json!({"release_schedule": [{"version": "1", "date": "d"}]});
        let next = json!({"release_schedule": [{"date": "d2", "version": "2"}, {"b": 1, "a": 2}]});
        let out = reorder_like(&previous, next);
        let items = out["release_schedule"].as_array().expect("array");
        assert_eq!(keys(&items[0]), ["version", "date"], "paired with previous");
        assert_eq!(keys(&items[1]), ["b", "a"], "no counterpart, left alone");
    }

    #[test]
    fn reorder_like_leaves_mismatched_shapes_alone() {
        let previous = json!({"a": {"x": 1}});
        let next = json!({"a": [1, 2]});
        assert_eq!(reorder_like(&previous, next.clone()), next);
        assert_eq!(reorder_like(&json!("scalar"), next.clone()), next);
    }
}
