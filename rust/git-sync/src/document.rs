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
use crate::format::SectionKind;

/// A parsed file, and whether reading it needed more than strict JSON.
pub(crate) struct Parsed {
    /// The documents the file holds, in the order they appear in it.
    ///
    /// Every syntax yields exactly one; a multi-document one would yield
    /// several, which [`fold_chunks`] is what reduces to the single
    /// value the rest of the crate addresses records in.
    pub(crate) chunks: Vec<serde_json::Value>,
    /// The file used JSON5 syntax — a comment, a trailing comma, an
    /// unquoted key — that strict JSON rejects.
    ///
    /// Worth surfacing because a rewrite emits strict JSON, so this says
    /// the file is about to be normalized and its comments dropped.
    /// Always false for YAML, where the question does not arise.
    pub(crate) extended: bool,
    /// The `literate-yaml` front-matter value of a markdown document —
    /// what names its format, since the YAML it holds carries no header
    /// for [`crate::DataFormat::is_format`] to inspect. `None` for every
    /// other syntax, and for a `.md` that is not a literate document.
    pub(crate) literate: Option<String>,
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
    /// YAML extracted from the fenced code blocks of a markdown
    /// document. See [`crate::markdown`].
    Markdown,
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
            "md" | "markdown" => Some(Self::Markdown),
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
                chunks: vec![from_yaml(text()?).map_err(|e| Error::Yaml {
                    path: file_path.to_string(),
                    message: e.to_string(),
                })?],
                extended: false,
                literate: None,
            }),
            Self::Markdown => {
                // This markdown has embedded YAML
                let (value, literate) = match std::str::from_utf8(bytes)
                    .ok()
                    .and_then(|src| unfurl_merge::markdown::Markdown::parse(file_path, src))
                {
                    Some(md) => (md.value, Some(md.name)),
                    None => (serde_json::Value::Object(serde_json::Map::new()), None),
                };
                Ok(Parsed {
                    chunks: vec![value],
                    extended: false,
                    literate,
                })
            }
            Self::Json | Self::Json5 => match serde_json::from_slice(bytes) {
                Ok(value) => Ok(Parsed {
                    chunks: vec![value],
                    extended: false,
                    literate: None,
                }),
                Err(strict) => match (json5::from_str(text()?), self) {
                    (Ok(value), _) => Ok(Parsed {
                        chunks: vec![value],
                        extended: true,
                        literate: None,
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
            Self::Yaml => Ok(unfurl_merge::emit::to_yaml(root).into_bytes()),
            Self::Json | Self::Json5 => serde_json::to_vec_pretty(root).map_err(|e| Error::Json {
                path: file_path.to_string(),
                source: e,
            }),
            // Unreachable: a markdown document is written by
            // `crate::markdown`, which needs the original prose and so
            // cannot be expressed as a value-to-bytes function. Loud
            // rather than silently emitting YAML over someone's
            // document if a branch is ever missed.
            Self::Markdown => Err(Error::Other(format!(
                "{file_path}: markdown is written by the literate renderer, not serialized"
            ))),
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
        let root = fold_chunks(self.parse(file_path, bytes)?.chunks);
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
    /// Only YAML splices. [`Template`](unfurl_merge::template::Template)
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
            Self::Markdown => unreachable!("write_file branches before rendering markdown"),
        }
    }
}

/// The single value a file's records are addressed in, from the
/// documents [`Syntax::parse`] found in it.
///
/// The one place the scan, the write, and the base-commit lookup agree
/// on what a file's content *is* — they must, because a record found
/// under one shape is looked up under the others. Every syntax yields
/// exactly one document today, so this is that document.
pub(crate) fn fold_chunks(chunks: Vec<serde_json::Value>) -> serde_json::Value {
    chunks.into_iter().next().unwrap_or_default()
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
        // Sorting a singleton's keys would reorder the *record's* fields,
        // which is `field_order`'s job and not this one's.
        if fmt.section_kind(section_name) == SectionKind::Singleton {
            continue;
        }
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

/// What `format` says the section at `section_name` holds, with the
/// default for records no registered format claims.
pub(crate) fn section_kind(
    format: Option<&dyn crate::DataFormat>,
    section_name: &str,
) -> SectionKind {
    format.map_or(SectionKind::Map, |f| f.section_kind(section_name))
}

/// The value a document holds for the record at `(section_name, key)`,
/// or `None` when it holds none.
///
/// One of three functions that between them are the only places a
/// record's position inside a document is known — with
/// [`apply_insert`] and [`apply_delete`]. They have to agree: a reader
/// that looked in a different place from the writer would report every
/// pending edit as a divergence from a file that in fact holds it.
pub(crate) fn record_in<'a>(
    doc: &'a serde_json::Value,
    kind: SectionKind,
    section_name: &str,
    key: &str,
) -> Option<&'a serde_json::Value> {
    match kind {
        SectionKind::Map => doc.get(section_name)?.get(key),
        SectionKind::Singleton => doc.get(section_name),
    }
}

/// Remove the record at `(section_name, key)`. Drops a `Map` section
/// that becomes empty, and a `Singleton` section outright.
///
/// Uses `shift_remove` (not `remove`/`swap_remove`) so the order of the
/// surviving entries is preserved — critical for the
/// "minimally-edited" output the tests assert against.
pub(crate) fn apply_delete(
    root_obj: &mut serde_json::Map<String, serde_json::Value>,
    kind: SectionKind,
    section_name: &str,
    key: &str,
) {
    if kind == SectionKind::Singleton {
        root_obj.shift_remove(section_name);
        return;
    }
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

/// Insert or replace the record at `(section_name, key)`, creating a
/// `Map` section if it's missing and replacing any non-object value.
pub(crate) fn apply_insert(
    root_obj: &mut serde_json::Map<String, serde_json::Value>,
    kind: SectionKind,
    section_name: &str,
    key: String,
    json: serde_json::Value,
    format: Option<&dyn crate::DataFormat>,
) {
    if kind == SectionKind::Singleton {
        // The section *is* the record, so the format's field order for
        // it applies to the value going in whether or not the document
        // already had one -- there is no enclosing map to copy an order
        // from, as `reorder_like` does for a `Map` section's entries.
        let previous = root_obj.get(section_name);
        let json = match previous {
            Some(previous) => reorder_like(previous, json),
            None => match format {
                Some(fmt) => order_fields(json, fmt.field_order(section_name)),
                None => json,
            },
        };
        let at = section_index(root_obj, section_name, format);
        root_obj.shift_insert(at, section_name.to_string(), json);
        return;
    }
    if !root_obj.get(section_name).is_some_and(|v| v.is_object()) {
        let at = section_index(root_obj, section_name, format);
        let empty = serde_json::Value::Object(serde_json::Map::new());
        root_obj.shift_insert(at, section_name.to_string(), empty);
    }
    let section = root_obj
        .get_mut(section_name)
        .expect("the section was just ensured");
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

/// Where `section_name` goes among the document's keys: where it is, or
/// for a new one, before the first key the format's order for the root
/// (`field_order("")`) puts after it, else at the end.
fn section_index(
    root_obj: &serde_json::Map<String, serde_json::Value>,
    section_name: &str,
    format: Option<&dyn crate::DataFormat>,
) -> usize {
    if let Some(at) = root_obj.keys().position(|key| key == section_name) {
        return at;
    }
    let order = format.map_or(&[][..], |fmt| fmt.field_order(""));
    let Some(rank) = order.iter().position(|name| *name == section_name) else {
        return root_obj.len();
    };
    let later = &order[rank + 1..];
    root_obj
        .keys()
        .position(|key| later.contains(&key.as_str()))
        .unwrap_or(root_obj.len())
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
/// Whether two sections hold the same keys in the same order, which is
/// what says an edit did not move anything: each record can be edited
/// where it sits. Otherwise records are added and removed around the
/// others (see `splice_section`).
fn same_keys(have: Option<&serde_json::Value>, want: Option<&serde_json::Value>) -> bool {
    match (
        have.and_then(|v| v.as_object()),
        want.and_then(|v| v.as_object()),
    ) {
        (Some(have), Some(want)) => {
            have.len() == want.len() && have.keys().zip(want.keys()).all(|(a, b)| a == b)
        }
        _ => false,
    }
}

pub(crate) fn splice_touched_sections(
    src: &str,
    rendered: &[u8],
    touched: &[String],
) -> Option<Vec<u8>> {
    let rendered = std::str::from_utf8(rendered).ok()?;
    let original = unfurl_merge::template::Template::parse(src)?;
    let updated = unfurl_merge::template::Template::parse(rendered)?;
    let want: serde_json::Value = from_yaml(rendered).ok()?;
    let have: serde_json::Value = from_yaml(src).ok()?;

    let mut replacements = std::collections::HashMap::new();
    // sections that aren't block bodies on both sides: written as whole
    // top-level entries
    let mut whole: Vec<&str> = Vec::new();
    for name in touched {
        let (Some(section), Some(from)) = (original.section(name), updated.section(name)) else {
            whole.push(name);
            continue;
        };
        let body = unfurl_merge::template::dedent_block(original.body(section), section.indent);
        let fresh = unfurl_merge::template::dedent_block(updated.body(from), from.indent);
        let spliced = splice_section(
            &body,
            &fresh,
            have.get(name.as_str()),
            want.get(name.as_str()),
            section.indent,
        );
        replacements.insert(name.as_str(), spliced.unwrap_or(fresh));
    }
    let mut spliced = original.render(&replacements);
    if !whole.is_empty() {
        spliced = splice_whole_sections(&spliced, rendered, &whole)?;
    }

    let got: serde_json::Value = from_yaml(&spliced).ok()?;
    (got == want).then(|| spliced.into_bytes())
}

/// The body of a section, `body` in the file and `fresh` as rendered, at
/// column 0 for a place `indent` columns in, holding `want` instead of
/// `have`, with every record that didn't change kept as the file has it:
/// an added record is `fresh`'s, placed after the record before it in
/// `want`'s order, a removed one is taken out, and a changed one is
/// edited where it sits.
fn splice_section(
    body: &str,
    fresh: &str,
    have: Option<&serde_json::Value>,
    want: Option<&serde_json::Value>,
    indent: usize,
) -> Option<String> {
    if same_keys(have, want) {
        return unfurl_merge::template::splice_value_at(body, have?, want?, indent);
    }
    let (have, want) = (have?.as_object()?, want?.as_object()?);
    let rendered = unfurl_merge::template::entries(fresh)?;
    let added: Vec<(&str, &str)> = want
        .keys()
        .filter(|key| !have.contains_key(*key))
        .map(|key| {
            let (_, range) = rendered.iter().find(|(k, _)| k == key)?;
            Some((key.as_str(), &fresh[range.clone()]))
        })
        .collect::<Option<_>>()?;
    let removed: Vec<&str> = have
        .keys()
        .filter(|key| !want.contains_key(*key))
        .map(String::as_str)
        .collect();
    let order: Vec<&str> = want.keys().map(String::as_str).collect();
    let moved = unfurl_merge::template::splice_entries(body, &added, &removed, &order)?;
    // what `moved` holds: the kept records as they were, the added ones
    // as wanted
    let held: serde_json::Map<String, serde_json::Value> = want
        .iter()
        .map(|(key, value)| (key.clone(), have.get(key).unwrap_or(value).clone()))
        .collect();
    if held == *want {
        return Some(moved);
    }
    unfurl_merge::template::splice_value_at(
        &moved,
        &serde_json::Value::Object(held),
        &serde_json::Value::Object(want.clone()),
        indent,
    )
}

/// `doc` with each of the top-level `names` replaced by, or inserted as,
/// its entry in `rendered`, or taken out where `rendered` has none.
fn splice_whole_sections(doc: &str, rendered: &str, names: &[&str]) -> Option<String> {
    let fresh = unfurl_merge::template::entries(rendered)?;
    let order: Vec<&str> = fresh.iter().map(|(key, _)| key.as_str()).collect();
    let mut blocks = Vec::new();
    let mut removed = Vec::new();
    for name in names {
        match fresh.iter().find(|(key, _)| key == name) {
            Some((_, range)) => blocks.push((*name, &rendered[range.clone()])),
            None => removed.push(*name),
        }
    }
    unfurl_merge::template::splice_entries(doc, &blocks, &removed, &order)
}

/// `text` parsed as YAML whose booleans are only `true` and `false`, as in
/// YAML 1.2's core schema: `yes`, `on` and `y` are strings.
pub(crate) fn from_yaml<T: serde::de::DeserializeOwned>(
    text: &str,
) -> std::result::Result<T, serde_saphyr::Error> {
    serde_saphyr::from_str_with_options(text, serde_saphyr::options! { strict_booleans: true })
}

/// Atomic-replace `abs` with `bytes` via a tempfile in the same
/// directory, with `abs`'s permissions, or a new file's. Creates the
/// parent directory if needed.
pub(crate) fn stage_write(abs: &Path, bytes: &[u8]) -> Result<tempfile::NamedTempFile> {
    let dir = abs.parent().unwrap_or_else(|| Path::new("."));
    std::fs::create_dir_all(dir)?;
    let mut builder = tempfile::Builder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        // less the umask, as for any new file
        builder.permissions(std::fs::Permissions::from_mode(0o666));
    }
    let mut tmp = builder.tempfile_in(dir)?;
    match std::fs::metadata(abs) {
        Ok(metadata) => tmp.as_file().set_permissions(metadata.permissions())?,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(e.into()),
    }
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
