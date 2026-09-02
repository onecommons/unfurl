// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Records written as prose: a markdown document whose YAML lives in
//! fenced code blocks.
//!
//! A document opts in through front matter naming the format it holds:
//!
//! ```text
//! ---
//! literate-yaml: cloudmap@unfurl/v1.0.0
//! ---
//! ```
//!
//! Reading merges every `yaml` fence, in document order, into one value
//! that the rest of the crate treats like any other parsed document.
//! Writing puts each change back in the block that already holds it,
//! leaving the prose alone.
//!
//! # The parity invariant
//!
//! [`blocks`] is the *only* enumeration of a document's fences, and both
//! directions use it. A block the reader skips the writer must skip too:
//! were they to disagree, a record could be placed in a trailing fence
//! while a stale copy in a skipped one still merged over it. Everything
//! a block can be excluded for — unparseable, not a mapping, opted out
//! with `# literate-yaml: ignore` — is funnelled into one signal,
//! [`Block::value`] being `None`, so the two paths cannot drift.

use std::collections::{HashMap, HashSet};
use std::ops::Range;

use crate::conflict::Applied;
use crate::error::{Error, Result};
use crate::template::{dedent_block, indent_block, Template};

/// Opts a fenced block out of the document, as its first non-blank line.
///
/// A comment rather than an info-string attribute so it survives
/// renderers that drop unknown attributes, and so it is visible to
/// someone reading the raw file.
const IGNORE_DIRECTIVE: &str = "# literate-yaml: ignore";

/// A markdown document's YAML: which format its front matter names, the
/// fenced blocks it was extracted from, and their merge.
#[derive(Debug)]
pub(crate) struct Markdown {
    /// The `literate-yaml` front-matter value, to be matched against
    /// [`crate::DataFormat::is_literate_format`].
    pub(crate) name: String,
    pub(crate) blocks: Vec<Block>,
    /// Every live block merged in document order.
    pub(crate) value: serde_json::Value,
}

/// One `yaml` fence of a markdown document.
#[derive(Debug)]
pub(crate) struct Block {
    /// Byte range of the fence *body* — the lines between the opening
    /// and closing fence lines, so the fence lines and their info
    /// string splice through verbatim.
    pub(crate) body: Range<usize>,
    /// Column the opening fence sits at. Content is dedented by this on
    /// the way in and re-indented on the way out.
    pub(crate) indent: usize,
    /// The dedented body text, which the write path splices into.
    pub(crate) text: String,
    /// `None` when the block is inert to both directions: it did not
    /// parse, its root is not a mapping, or it opted out with
    /// [`IGNORE_DIRECTIVE`].
    pub(crate) value: Option<serde_json::Value>,
}

impl Markdown {
    /// The document `src` holds, or `None` when it is not literate —
    /// no front matter, or none naming a format.
    ///
    /// Never fails. A `parse_and_detect` error aborts the whole scan,
    /// so one unreadable `.md` in a working tree must not stop every
    /// other file being indexed; an unreadable document is simply not
    /// one of ours.
    pub(crate) fn parse(file_path: &str, src: &str) -> Option<Self> {
        let name = front_matter_name(src)?;
        let blocks = blocks(file_path, src);
        let mut value = serde_json::Value::Object(serde_json::Map::new());
        for live in blocks.iter().filter_map(|b| b.value.as_ref()) {
            merge_into(&mut value, live.clone());
        }
        Some(Markdown {
            name,
            blocks,
            value,
        })
    }
}

/// The `literate-yaml` value of `src`'s front matter.
///
/// Recognised only when the very first line of the file (after an
/// optional byte-order mark) is exactly `---`, running to the next line
/// that is exactly `---` or `...`. Every failure — no front matter, no
/// terminator, unparseable YAML, no `literate-yaml` key, a non-string
/// value — is `None` rather than an error, per [`Markdown::parse`].
pub(crate) fn front_matter_name(src: &str) -> Option<String> {
    let src = src.strip_prefix('\u{feff}').unwrap_or(src);
    let rest = src
        .strip_prefix("---\n")
        .or_else(|| src.strip_prefix("---\r\n"))?;

    let mut end = 0;
    let terminator = rest.split_inclusive('\n').find_map(|line| {
        let at = end;
        end += line.len();
        matches!(line.trim_end(), "---" | "...").then_some(at)
    })?;

    let doc: serde_json::Value = serde_saphyr::from_str(&rest[..terminator]).ok()?;
    let name = doc.get("literate-yaml")?.as_str()?;
    (!name.is_empty()).then(|| name.to_string())
}

/// Every `yaml` fence in `src`, in document order.
///
/// A fence whose info string names something else is consumed but not
/// returned, so a ```` ```yaml ```` line *inside* it is not mistaken for
/// an opening fence.
///
/// The grammar is a deliberate CommonMark subset: 0–3 leading spaces,
/// three or more backticks or tildes, a closer of the same character and
/// at least as long, and end-of-file closing an unterminated fence.
/// Fences nested in list items or blockquotes are not recognised — their
/// line prefix is not stripped.
pub(crate) fn blocks(file_path: &str, src: &str) -> Vec<Block> {
    let mut out = Vec::new();
    let mut open: Option<Fence> = None;
    let mut body_start = 0;
    let mut pos = 0;

    for line in src.split_inclusive('\n') {
        let line_start = pos;
        pos += line.len();
        let line = line.trim_end_matches(['\n', '\r']);
        match &open {
            None => {
                if let Some(fence) = Fence::opening(line) {
                    open = Some(fence);
                    body_start = pos;
                }
            }
            Some(fence) => {
                if fence.closed_by(line) {
                    if fence.yaml {
                        out.push(block(file_path, src, body_start..line_start, fence.indent));
                    }
                    open = None;
                }
            }
        }
    }
    // CommonMark: end of document closes an open fence.
    if let Some(fence) = open.filter(|f| f.yaml) {
        out.push(block(file_path, src, body_start..src.len(), fence.indent));
    }
    out
}

/// An open code fence: what it takes to close it, and whether its
/// contents are ours.
struct Fence {
    char: char,
    len: usize,
    indent: usize,
    yaml: bool,
}

impl Fence {
    fn opening(line: &str) -> Option<Self> {
        let indent = line.len() - line.trim_start_matches(' ').len();
        if indent > 3 {
            return None;
        }
        let rest = &line[indent..];
        let char = rest.chars().next().filter(|c| *c == '`' || *c == '~')?;
        let len = rest.chars().take_while(|c| *c == char).count();
        let info = rest[len..].trim();
        // CommonMark: a backtick fence's info string may not hold a
        // backtick, or `` `foo` `` inline code would open one.
        if len < 3 || (char == '`' && info.contains('`')) {
            return None;
        }
        let first = info.split_whitespace().next().unwrap_or_default();
        Some(Fence {
            char,
            len,
            indent,
            yaml: first.eq_ignore_ascii_case("yaml") || first.eq_ignore_ascii_case("yml"),
        })
    }

    fn closed_by(&self, line: &str) -> bool {
        let indent = line.len() - line.trim_start_matches(' ').len();
        if indent > 3 {
            return false;
        }
        let rest = &line[indent..];
        let len = rest.chars().take_while(|c| *c == self.char).count();
        len >= self.len && rest[len..].trim().is_empty()
    }
}

/// One block, dedented and parsed. See [`Block::value`] for what makes
/// it inert.
fn block(file_path: &str, src: &str, body: Range<usize>, indent: usize) -> Block {
    let text = dedent_block(&src[body.clone()], indent);
    let value = if ignored(&text) {
        None
    } else {
        match serde_saphyr::from_str::<serde_json::Value>(&text) {
            Ok(value) if value.is_object() => Some(value),
            // Prose routinely holds YAML that is elided, abbreviated, or
            // deliberately wrong. Skipping is the only reading that
            // lets a document explain itself; the alternative loses
            // every record in the file to one typo.
            Ok(_) => None,
            Err(e) => {
                tracing::warn!(
                    file = %file_path,
                    "yaml block does not parse; ignoring it: {e}"
                );
                None
            }
        }
    };
    Block {
        body,
        indent,
        text,
        value,
    }
}

/// Whether the block's first non-blank line opts it out.
fn ignored(text: &str) -> bool {
    text.lines()
        .find(|line| !line.trim().is_empty())
        .is_some_and(|line| line.trim() == IGNORE_DIRECTIVE)
}

/// Fold `next` into `acc`, the merge that makes several fences one
/// document.
///
/// - Two maps merge per key: keys already in `acc` keep their position
///   and merge recursively, new ones append.
/// - A map and a null yield the map, **in either order**. This is the
///   literate idiom — a later block restating an ancestor path with
///   nothing under the leaf, purely to anchor the prose around it —
///   and it must not blank out what an earlier block said.
/// - Anything else: `next` wins, so the last block naming a key
///   decides it. **Sequences included**: merging them positionally
///   would be a guess about which element is which, and concatenating
///   would make a rewrite non-idempotent. A record field holding a list
///   must therefore live wholly in one block.
pub(crate) fn merge_into(acc: &mut serde_json::Value, next: serde_json::Value) {
    match (acc, next) {
        (serde_json::Value::Object(into), serde_json::Value::Object(from)) => {
            for (key, value) in from {
                match into.get_mut(&key) {
                    Some(slot) => merge_into(slot, value),
                    None => {
                        into.insert(key, value);
                    }
                }
            }
        }
        (serde_json::Value::Object(_), serde_json::Value::Null) => {}
        (slot, next) => *slot = next,
    }
}

// ---------------------------------------------------------------------------
// Writing
// ---------------------------------------------------------------------------

/// `src` with the applied records put back where they already live.
///
/// Each eligible block — one holding at least one of the format's
/// [`path_prefixes`](crate::DataFormat::path_prefixes) — is updated in
/// place for the records it already carries, *field by field*: a field
/// the block defines takes the database's value, one the record no
/// longer has is removed, and one the block never had is left for the
/// trailing fence. Anything no block absorbed is appended as a single
/// new `yaml` fence.
///
/// Field-level rather than record-level because the merge has no notion
/// of a primary block. With block A holding `{x: {type: T}}` and B
/// holding `{x: {name: n}}`, writing the whole record into A alone
/// leaves B's `name` merging *after* it and resurrecting the stale
/// value.
pub(crate) fn render(
    file_path: &str,
    src: &str,
    root: &serde_json::Value,
    applied: &[Applied],
    format: Option<&dyn crate::DataFormat>,
) -> Result<Vec<u8>> {
    let md = Markdown::parse(file_path, src).ok_or_else(|| {
        Error::Other(format!(
            "{file_path}: not a literate markdown document; \
             add `literate-yaml` front matter naming a format first"
        ))
    })?;
    let prefixes = format.map(|f| f.path_prefixes()).unwrap_or_default();

    // Every live block after its edits, and the text of the ones that
    // moved. An ineligible block still merges, so it still counts
    // towards what the trailing fence has to carry.
    // Every path some block already holds, seeded so that a key living
    // in another block is updated *there* rather than copied here, and
    // a genuinely new one is written once -- into the first block whose
    // map at its parent path can take it.
    let mut placed: HashSet<String> = HashSet::new();
    for live in md.blocks.iter().filter_map(|b| b.value.as_ref()) {
        paths_of(live, &mut Vec::new(), &mut placed);
    }
    let mut covered = serde_json::Value::Object(serde_json::Map::new());
    let mut rewritten: Vec<(&Block, String)> = Vec::new();
    for block in &md.blocks {
        let Some(before) = &block.value else { continue };
        let after = if eligible(before, prefixes) {
            place(before, root, applied, &mut placed)
        } else {
            before.clone()
        };
        merge_into(&mut covered, after.clone());
        if after != *before {
            // A block the deletes emptied is written as an empty
            // body rather than `{}`. An empty fence reads back as
            // inert, which is what a block holding nothing should be,
            // and it leaves the prose framing it without a husk.
            let text = if after.as_object().is_some_and(serde_json::Map::is_empty) {
                String::new()
            } else {
                splice_value(&block.text, before, &after)
                    .map(Ok)
                    .unwrap_or_else(|| emit(&after, file_path))?
            };
            rewritten.push((block, text));
        }
    }

    let out = assemble(
        src,
        &rewritten,
        leftover(root, &covered, prefixes),
        file_path,
    )?;

    // The document must still say what the database says. Value
    // equality is order-independent (`preserve_order` gives an
    // `IndexMap`), so this compares meaning, not layout.
    if reads_back_as(file_path, &out, root) {
        return Ok(out.into_bytes());
    }
    additive_fallback(file_path, src, root, applied, &md, prefixes)
}

/// Every `a/b/c` path `value` holds, however deep.
fn paths_of(value: &serde_json::Value, at: &mut Vec<String>, out: &mut HashSet<String>) {
    let Some(obj) = value.as_object() else {
        return;
    };
    for (key, child) in obj {
        at.push(key.clone());
        out.insert(at.join("/"));
        paths_of(child, at, out);
        at.pop();
    }
}

/// Whether a block holds any of the format's record sections. One that
/// does not is read but never written into — the user's "skip blocks
/// that don't contain a path_prefixes() key".
fn eligible(value: &serde_json::Value, prefixes: &[&str]) -> bool {
    value
        .as_object()
        .is_some_and(|obj| prefixes.iter().any(|p| obj.contains_key(*p)))
}

/// One block's value after the records it already carries are updated.
fn place(
    before: &serde_json::Value,
    root: &serde_json::Value,
    applied: &[Applied],
    placed: &mut HashSet<String>,
) -> serde_json::Value {
    let mut after = before.clone();
    let Some(obj) = after.as_object_mut() else {
        return after;
    };
    for act in applied {
        let Some(section) = obj.get_mut(&act.section).and_then(|s| s.as_object_mut()) else {
            continue;
        };
        if !section.contains_key(&act.key) {
            continue;
        }
        if act.deleted {
            // Every block, including one holding only a null anchor:
            // `document_records` reads a bare `key:` as a record, so a
            // copy left anywhere resurrects the deletion as an empty
            // record on the next scan.
            section.shift_remove(&act.key);
            if section.is_empty() {
                obj.shift_remove(&act.section);
            }
            continue;
        }
        let Some(next) = root.get(&act.section).and_then(|s| s.get(&act.key)) else {
            continue;
        };
        let mut at = vec![act.section.clone(), act.key.clone()];
        let projected = project(&section[&act.key], next, &mut at, placed);
        section.insert(act.key.clone(), projected);
    }
    after
}

/// `next` as this block should hold it.
///
/// Recurses only while both sides are maps. A field `existing` has takes
/// its value from `next`; a field `next` dropped is removed; a field
/// only `next` has joins its siblings *here*, in the first block whose
/// map at that path can take it — `placed` is what keeps it from being
/// written into every such block. A null is an anchor: it owns no
/// fields, so it keeps its place and the whole value goes to the
/// trailing fence, where the merge puts the map back over it.
fn project(
    existing: &serde_json::Value,
    next: &serde_json::Value,
    at: &mut Vec<String>,
    placed: &mut HashSet<String>,
) -> serde_json::Value {
    match (existing, next) {
        (serde_json::Value::Null, _) => serde_json::Value::Null,
        (serde_json::Value::Object(have), serde_json::Value::Object(want)) => {
            let mut kept = serde_json::Map::new();
            for (key, hv) in have {
                let Some(wv) = want.get(key) else { continue };
                at.push(key.clone());
                kept.insert(key.clone(), project(hv, wv, at, placed));
                at.pop();
            }
            // A map whose keys the record replaced outright -- renaming
            // the one type under `type:`, say -- is written here whole
            // rather than emptied. Field-level placement would
            // otherwise leave a `{}` husk in the document and append
            // the new value, which is correct under the merge but
            // accumulates litter the author never wrote.
            if kept.is_empty() && !have.is_empty() && !want.is_empty() {
                return next.clone();
            }
            // New keys append after the ones already here, so `bar:`
            // lands beside the `foo:` it belongs with rather than in a
            // fence at the far end of the document.
            for (key, wv) in want {
                if have.contains_key(key) {
                    continue;
                }
                at.push(key.clone());
                let first_time = placed.insert(at.join("/"));
                at.pop();
                if first_time {
                    kept.insert(key.clone(), wv.clone());
                }
            }
            serde_json::Value::Object(kept)
        }
        _ => next.clone(),
    }
}

/// What `covered` does not already say, restricted to record sections so
/// a non-record top-level key never migrates into the trailing fence.
///
/// Taken from the post-apply `root` rather than the raw records:
/// `apply_insert` has already run `reorder_like` against the merged
/// prior value, so the appended fence inherits the document's key order.
fn leftover(
    root: &serde_json::Value,
    covered: &serde_json::Value,
    prefixes: &[&str],
) -> Option<serde_json::Value> {
    let mut out = serde_json::Map::new();
    for prefix in prefixes {
        let Some(want) = root.get(*prefix) else {
            continue;
        };
        let missing = match covered.get(*prefix) {
            Some(have) => deep_diff(want, have),
            None => Some(want.clone()),
        };
        if let Some(missing) = missing {
            out.insert((*prefix).to_string(), missing);
        }
    }
    (!out.is_empty()).then(|| serde_json::Value::Object(out))
}

/// The part of `want` that merging over `have` would still change, or
/// `None` when `have` already says it.
fn deep_diff(want: &serde_json::Value, have: &serde_json::Value) -> Option<serde_json::Value> {
    if want == have {
        return None;
    }
    let (serde_json::Value::Object(w), serde_json::Value::Object(h)) = (want, have) else {
        return Some(want.clone());
    };
    let out: serde_json::Map<String, serde_json::Value> = w
        .iter()
        .filter_map(|(k, wv)| {
            let diff = match h.get(k) {
                Some(hv) => deep_diff(wv, hv)?,
                None => wv.clone(),
            };
            Some((k.clone(), diff))
        })
        .collect();
    (!out.is_empty()).then(|| serde_json::Value::Object(out))
}

/// `body` rewritten to hold `after` instead of `before`, replacing only
/// the sub-bodies that differ, or `None` when its shape does not allow
/// it.
///
/// The generalisation of [`crate::document::splice_touched_sections`] to
/// arbitrary depth. Sound here and not for plain YAML for the reason
/// [`crate::template`] gives: record-level spans are invalidated by the
/// key-sort a cloudmap section gets on write, and a literate block is
/// never sorted — it is updated in place.
///
/// A key added or removed at a level makes that level un-spliceable, so
/// the failure widens the diff by one enclosing body rather than
/// escaping to the whole document.
fn splice_value(
    body: &str,
    before: &serde_json::Value,
    after: &serde_json::Value,
) -> Option<String> {
    let (before, after) = (before.as_object()?, after.as_object()?);
    // A key *removed* at this level leaves a hole `Template` cannot
    // close, so the caller re-serializes the enclosing body. A key
    // added is fine: it goes after the ones already here.
    if before.keys().any(|k| !after.contains_key(k)) {
        return None;
    }
    let template = Template::parse(body)?;
    let mut replacements: HashMap<&str, String> = HashMap::new();
    for (key, was) in before {
        let now = &after[key];
        if was == now {
            continue;
        }
        let text = match template.section(key) {
            Some(section) => {
                let nested = dedent_block(template.body(section), section.indent);
                splice_value(&nested, was, now).or_else(|| emit(now, "").ok())?
            }
            // A value sharing its key's line is swapped where it sits,
            // so the comment after it and every comment elsewhere in
            // this body survive -- which is the whole difference for a
            // scalar field, the most common thing an edit touches.
            // Only while it stays one line: a block scalar cannot go on
            // a key's line, and widening the hole to fit one would
            // swallow the comment this exists to keep.
            None => {
                template.inline(key)?;
                let text = emit(now, "").ok()?;
                let line = text.trim_end_matches('\n');
                if line.contains('\n') {
                    return None;
                }
                line.to_string()
            }
        };
        replacements.insert(key.as_str(), text);
    }
    let mut out = template.render(&replacements);

    let added: serde_json::Map<String, serde_json::Value> = after
        .iter()
        .filter(|(key, _)| !before.contains_key(*key))
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect();
    if !added.is_empty() {
        if !out.is_empty() && !out.ends_with('\n') {
            out.push('\n');
        }
        out.push_str(&emit(&serde_json::Value::Object(added), "").ok()?);
    }
    Some(out)
}

/// Serialize a fence body: YAML at column 0, with the explicit nulls
/// elided so an anchor written `x:` does not come back as `x: null`.
fn emit(value: &serde_json::Value, file_path: &str) -> Result<String> {
    let text = serde_saphyr::to_string(value).map_err(|e| Error::Yaml {
        path: file_path.to_string(),
        message: e.to_string(),
    })?;
    Ok(crate::util::elide_explicit_nulls(&text))
}

/// Splice the rewritten blocks back into `src` and append the trailing
/// fence, leaving every other byte — prose, fence lines, front matter —
/// exactly where it was.
fn assemble(
    src: &str,
    rewritten: &[(&Block, String)],
    leftover: Option<serde_json::Value>,
    file_path: &str,
) -> Result<String> {
    let mut out = String::with_capacity(src.len());
    let mut cursor = 0;
    for (block, text) in rewritten {
        out.push_str(&src[cursor..block.body.start]);
        out.push_str(&indent_block(text, block.indent));
        cursor = block.body.end;
    }
    out.push_str(&src[cursor..]);

    if let Some(leftover) = leftover {
        if !out.is_empty() && !out.ends_with('\n') {
            out.push('\n');
        }
        out.push_str("\n```yaml\n");
        out.push_str(&emit(&leftover, file_path)?);
        out.push_str("```\n");
    }
    Ok(out)
}

/// Whether re-reading `text` as a literate document yields `root`.
fn reads_back_as(file_path: &str, text: &str, root: &serde_json::Value) -> bool {
    Markdown::parse(file_path, text).is_some_and(|md| md.value == *root)
}

/// The last resort when the in-place edit did not read back correctly.
///
/// Appending can express an addition and nothing else: `merge(map, map)`
/// recurses, so a stale field in an earlier block survives, and
/// `merge(map, null)` keeps the map, so a deletion cannot be appended at
/// all. A purely additive change is therefore safe to redo as
/// "original document, plus one fence"; anything else has to fail rather
/// than write a document whose merge still holds a record the database
/// deleted.
fn additive_fallback(
    file_path: &str,
    src: &str,
    root: &serde_json::Value,
    applied: &[Applied],
    md: &Markdown,
    prefixes: &[&str],
) -> Result<Vec<u8>> {
    // Checked up front rather than left to the read-back below, which
    // would also catch it: this is the one failure with a cause worth
    // naming, and a caller told only "it did not read back" cannot tell
    // a removal that is impossible here from a bug that is not.
    if applied.iter().any(|a| a.deleted) || deep_diff(&md.value, root).is_some() {
        return Err(Error::Other(format!(
            "{file_path}: the fenced blocks could not be updated in place, and this \
             edit removes something -- which appending a block cannot express. \
             The edit was not written"
        )));
    }
    let out = assemble(src, &[], leftover(root, &md.value, prefixes), file_path)?;
    if !reads_back_as(file_path, &out, root) {
        return Err(Error::Other(format!(
            "{file_path}: could not update the fenced blocks without changing what \
             the document means; the edit was not written"
        )));
    }
    Ok(out.into_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn values(src: &str) -> Vec<Option<serde_json::Value>> {
        blocks("t.md", src).into_iter().map(|b| b.value).collect()
    }

    fn merged(src: &str) -> serde_json::Value {
        Markdown::parse("t.md", src).expect("literate").value
    }

    const DOC: &str = "\
---
literate-yaml: cloudmap@unfurl/v1.0.0
---

Prose about the organization.

```yaml
components: # entities: !!
  # but this is more like an instance!
  org:
    type: RealWorldEntity
```

More prose.

```yaml
components:
  org:
    name: onecommons
```
";

    #[test]
    fn front_matter_names_the_format() {
        assert_eq!(
            front_matter_name(DOC).as_deref(),
            Some("cloudmap@unfurl/v1.0.0")
        );
    }

    #[test]
    fn a_document_without_front_matter_is_not_literate() {
        assert!(front_matter_name("# A readme\n\n```yaml\na: 1\n```\n").is_none());
        // A `---` rule further down is not front matter.
        assert!(front_matter_name("intro\n\n---\nliterate-yaml: x\n---\n").is_none());
        // Front matter that names nothing.
        assert!(front_matter_name("---\ntitle: hi\n---\n").is_none());
    }

    /// A `parse_and_detect` error aborts the whole scan, so a malformed
    /// block must read as "not ours", never as a failure.
    #[test]
    fn malformed_front_matter_is_not_an_error() {
        assert!(front_matter_name("---\n: : :\n---\n").is_none());
        assert!(
            front_matter_name("---\nliterate-yaml: x\n").is_none(),
            "no terminator"
        );
        assert!(
            front_matter_name("---\nliterate-yaml: []\n---\n").is_none(),
            "not a string"
        );
    }

    #[test]
    fn only_yaml_info_strings_are_blocks() {
        let src = "```json\n{\"a\": 1}\n```\n\n```\na: 1\n```\n\n```yamlish\na: 1\n```\n";
        assert!(values(src).is_empty(), "{:?}", values(src));
        for info in ["yaml", "yml", "YAML", "yaml title=\"x\""] {
            let src = format!("```{info}\na: 1\n```\n");
            assert_eq!(values(&src).len(), 1, "{info} should open a block");
        }
    }

    /// A yaml fence inside a longer fence is that fence's content, not a
    /// block -- otherwise a document showing how to write one would
    /// index the example.
    #[test]
    fn a_yaml_fence_inside_a_longer_fence_is_not_a_block() {
        let src = "````markdown\n```yaml\na: 1\n```\n````\n";
        assert!(values(src).is_empty(), "{:?}", values(src));
    }

    /// A backtick line inside a tilde fence is content, not a closer --
    /// so the block runs past it and `b` is still in the document.
    #[test]
    fn a_tilde_fence_closes_only_on_tildes() {
        let src = "~~~yaml\na: 1\n# ```\nb: 2\n~~~\n";
        assert_eq!(values(src)[0].as_ref().expect("live")["b"], 2);
    }

    #[test]
    fn an_indented_fence_is_dedented_by_its_own_indent() {
        let src = "  ```yaml\n  a:\n    b: 1\n  ```\n";
        assert_eq!(values(src)[0].as_ref().expect("live")["a"]["b"], 1);
    }

    #[test]
    fn an_unterminated_fence_runs_to_the_end() {
        assert_eq!(values("```yaml\na: 1\n")[0].as_ref().expect("live")["a"], 1);
    }

    #[test]
    fn a_block_that_is_not_a_mapping_is_inert() {
        assert_eq!(values("```yaml\n- a\n- b\n```\n"), vec![None]);
        assert_eq!(values("```yaml\njust a scalar\n```\n"), vec![None]);
        assert_eq!(values("```yaml\nkey: [unclosed\n```\n"), vec![None]);
        assert_eq!(values("```yaml\n```\n"), vec![None]);
    }

    #[test]
    fn the_ignore_directive_makes_a_block_inert() {
        let src = "```yaml\n# literate-yaml: ignore\ncomponents:\n  org: {}\n```\n";
        assert_eq!(values(src), vec![None]);
        // Leading blank lines are stepped over...
        let src = "```yaml\n\n# literate-yaml: ignore\ncomponents:\n  org: {}\n```\n";
        assert_eq!(values(src), vec![None]);
        // ...but it has to be the first thing that is not blank.
        let src = "```yaml\ncomponents:\n  org: {}\n# literate-yaml: ignore\n```\n";
        assert!(values(src)[0].is_some(), "only the first line opts out");
    }

    #[test]
    fn matching_maps_merge_recursively() {
        let doc = merged(DOC);
        assert_eq!(doc["components"]["org"]["type"], "RealWorldEntity");
        assert_eq!(doc["components"]["org"]["name"], "onecommons");
    }

    /// The literate idiom: a later block restates an ancestor path with
    /// nothing under the leaf, to hang prose off it.
    #[test]
    fn a_null_yields_to_a_map_in_either_order() {
        let mut acc = serde_json::json!({"components": {"org": {"type": "T"}}});
        merge_into(&mut acc, serde_json::json!({"components": {"org": null}}));
        assert_eq!(acc["components"]["org"]["type"], "T", "map + null");

        let mut acc = serde_json::json!({"components": {"org": null}});
        merge_into(
            &mut acc,
            serde_json::json!({"components": {"org": {"type": "T"}}}),
        );
        assert_eq!(acc["components"]["org"]["type"], "T", "null + map");
    }

    #[test]
    fn a_sequence_is_replaced_not_merged() {
        let mut acc = serde_json::json!({"protocols": ["https", "ssh"]});
        merge_into(&mut acc, serde_json::json!({"protocols": ["git"]}));
        assert_eq!(acc["protocols"], serde_json::json!(["git"]));
    }

    #[test]
    fn the_last_scalar_wins() {
        let mut acc = serde_json::json!({"name": "old", "kept": 1});
        merge_into(&mut acc, serde_json::json!({"name": "new"}));
        assert_eq!(acc, serde_json::json!({"name": "new", "kept": 1}));
    }

    #[test]
    fn existing_keys_keep_their_position_and_new_ones_append() {
        let mut acc = serde_json::json!({"b": 1, "a": 1});
        merge_into(&mut acc, serde_json::json!({"a": 2, "c": 3}));
        let keys: Vec<&String> = acc.as_object().expect("object").keys().collect();
        assert_eq!(keys, ["b", "a", "c"]);
    }

    /// An inert block contributes nothing, so the merged document is
    /// exactly what the live blocks said.
    #[test]
    fn an_inert_block_contributes_nothing() {
        let src = "\
---
literate-yaml: x
---

```yaml
components:
  org: {type: T}
```

```yaml
# literate-yaml: ignore
components:
  org: {type: WRONG}
```

```yaml
key: [unclosed
```
";
        assert_eq!(merged(src)["components"]["org"]["type"], "T");
    }

    fn one_block(body: &str) -> String {
        format!("---\nliterate-yaml: x\n---\n\nprose\n\n```yaml\n{body}```\n")
    }

    fn act(key: &str, deleted: bool) -> Vec<Applied> {
        vec![Applied {
            section: "components".into(),
            key: key.into(),
            deleted,
        }]
    }

    /// The fallback exists for a splice that did not read back, and it
    /// can only ever append -- so an addition is safe to redo that way.
    #[test]
    fn an_additive_write_falls_back_to_appending() {
        let src = one_block("components:\n  a:\n    t: 1\n");
        let md = Markdown::parse("t.md", &src).expect("literate");
        let root = serde_json::json!({"components": {"a": {"t": 1}, "b": {"t": 2}}});
        let out = additive_fallback("t.md", &src, &root, &act("b", false), &md, &["components"])
            .expect("an addition is expressible by appending");
        let out = String::from_utf8(out).expect("utf-8");
        assert!(
            out.starts_with(&src),
            "the original document is kept:\n{out}"
        );
        assert_eq!(
            Markdown::parse("t.md", &out).expect("literate").value,
            root,
            "and the appended fence makes up the difference"
        );
    }

    /// `merge(map, null)` keeps the map, so a deletion cannot be
    /// appended at all. Writing anyway would leave a document whose
    /// merge still holds a record the database deleted.
    #[test]
    fn a_deleting_write_refuses_rather_than_appending() {
        let src = one_block("components:\n  a:\n    t: 1\n");
        let md = Markdown::parse("t.md", &src).expect("literate");
        let root = serde_json::json!({"components": {}});
        let err = additive_fallback("t.md", &src, &root, &act("a", true), &md, &["components"])
            .expect_err("a deletion cannot be appended");
        assert!(err.to_string().contains("removes something"), "{err}");
    }

    /// The same for a record that merely *lost a field*: no delete is
    /// reported, but appending still cannot take one away.
    #[test]
    fn a_write_that_drops_a_field_refuses_too() {
        let src = one_block("components:\n  a:\n    t: 1\n    u: 2\n");
        let md = Markdown::parse("t.md", &src).expect("literate");
        let root = serde_json::json!({"components": {"a": {"t": 1}}});
        let err = additive_fallback("t.md", &src, &root, &act("a", false), &md, &["components"])
            .expect_err("a dropped field cannot be appended");
        assert!(err.to_string().contains("removes something"), "{err}");
    }

    /// `foo:` is null and `foo: {}` is an empty map -- different
    /// values, and `elide_explicit_nulls` rewrites only the first. An
    /// anchor that came back as `{}` would stop being an anchor, and an
    /// empty map that came back as null would lose a record's field.
    #[test]
    fn an_emitted_null_anchor_stays_distinct_from_an_empty_map() {
        let value = serde_json::json!({"anchor": null, "empty": {}, "nested": {"a": null}});
        let text = emit(&value, "t.md").expect("emit");
        assert_eq!(text, "anchor:\nempty: {}\nnested:\n  a:\n");
        assert_eq!(
            serde_saphyr::from_str::<serde_json::Value>(&text).expect("yaml"),
            value
        );
    }

    /// A block emptied by deletes is written with no body at all, which
    /// reads back as null rather than as an empty map. The two differ
    /// as *values* -- but not as blocks: one is inert and the other
    /// merges nothing, so the document says the same either way. The
    /// empty body is written because it leaves no husk in the prose.
    #[test]
    fn an_emptied_block_says_the_same_as_an_empty_map() {
        let empty_body = one_block("");
        let empty_map = one_block("{}\n");
        assert!(
            blocks("t.md", &empty_body)[0].value.is_none(),
            "an empty body is inert"
        );
        assert_eq!(
            blocks("t.md", &empty_map)[0].value,
            Some(serde_json::json!({})),
            "an empty map is a live block holding nothing"
        );
        assert_eq!(
            Markdown::parse("t.md", &empty_body)
                .expect("literate")
                .value,
            Markdown::parse("t.md", &empty_map).expect("literate").value,
        );
    }

    /// A sequence cannot go where a scalar was: spliced onto the key's
    /// line it reads `name: - a`, and the rest of it lands at the wrong
    /// indentation entirely. The one-line guard is what stops that --
    /// a block scalar survives the same treatment only because the
    /// enclosing body is re-indented around it, and a sequence does
    /// not.
    #[test]
    fn a_value_that_stops_being_one_line_is_not_spliced_inline() {
        let before = serde_json::json!({"name": "short"});
        let after = serde_json::json!({"name": ["a", "b"]});
        assert!(
            splice_value("name: short\n", &before, &after).is_none(),
            "the caller must re-emit the enclosing body instead"
        );
    }

    /// A block scalar cannot sit on a key's line either, so a value that
    /// grows to several lines falls back to re-emitting the enclosing
    /// body rather than splicing in something the document cannot hold.
    #[test]
    fn a_value_that_becomes_multiline_is_not_spliced_inline() {
        let before = serde_json::json!({"a": {"name": "short"}});
        let after = serde_json::json!({"a": {"name": "one\ntwo"}});
        let out = splice_value("a:\n  name: short\n", &before, &after)
            .expect("the enclosing body is re-emitted");
        assert!(out.contains("|-"), "written as a block scalar: {out:?}");
        assert_eq!(
            serde_saphyr::from_str::<serde_json::Value>(&out).expect("yaml"),
            after
        );
    }

    #[test]
    fn the_block_body_excludes_the_fence_lines() {
        let src = "intro\n\n```yaml\na: 1\n```\n";
        let blocks = blocks("t.md", src);
        assert_eq!(&src[blocks[0].body.clone()], "a: 1\n");
    }
}
