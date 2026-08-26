// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Rewriting a document by replacing whole sections, leaving the rest of
//! the bytes alone.
//!
//! The record round-trip goes through a `serde_json::Value`, which holds
//! no comments — so re-serializing a whole document drops every comment
//! in it, including ones nowhere near the records that changed. A
//! cloudmap's *layout* is generated (sections are key-sorted, fields have
//! a canonical order), so there is nothing to preserve there; what people
//! lose is what they wrote.
//!
//! Sections are the right granularity for that. Record-level spans would
//! be invalidated by the sort — add one record and every later one moves
//! — but sorting happens *within* a section, so a section-sized hole
//! survives it. Everything outside the holes, envelope included, is
//! copied through byte for byte.

use std::collections::HashMap;
use std::ops::Range;

/// Where one top-level section's body sits in the source.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Section {
    /// Byte range of the body — not including the `key:` line, which
    /// stays in the template.
    pub(crate) body: Range<usize>,
    /// Column the body is indented to, so a replacement can be written
    /// to match.
    pub(crate) indent: usize,
}

/// A document split into the parts that stay and the sections that can
/// be replaced.
#[derive(Debug)]
pub(crate) struct Template {
    source: String,
    sections: HashMap<String, Section>,
}

impl Template {
    /// Split `src`, or `None` when it is not a YAML document whose root
    /// is a mapping — the only shape this can rewrite. A caller that
    /// gets `None` should fall back to re-serializing the whole
    /// document.
    pub(crate) fn parse(src: &str) -> Option<Self> {
        use saphyr::{LoadableYamlNode, MarkedYaml, YamlData};

        let docs = MarkedYaml::load_from_str(src).ok()?;
        let root = docs.first()?;
        let YamlData::Mapping(map) = &root.data else {
            return None;
        };

        // `saphyr` reports positions as char indices, despite
        // `Marker::index` being documented as "the index (in bytes)" --
        // its scanner counts characters. Every offset below is a byte
        // offset, so translate once rather than per lookup;
        // `a_section_after_multibyte_text_is_still_located_correctly`
        // pins which of the two it really is.
        let bytes: Vec<usize> = src
            .char_indices()
            .map(|(b, _)| b)
            .chain(std::iter::once(src.len()))
            .collect();
        let byte_of = |chars: usize| *bytes.get(chars).unwrap_or(&src.len());

        let mut sections = HashMap::new();
        for (key, value) in map {
            let YamlData::Value(scalar) = &key.data else {
                continue;
            };
            let Some(name) = scalar.as_str() else {
                continue;
            };
            let value_start = byte_of(value.span.start.index());
            // Take whole lines: the body's first character sits after the
            // indentation, which is already in the source, so a hole that
            // began there would have a replacement indented twice. Widen
            // to the line start and the hole becomes line-aligned.
            let line_start = src[..value_start].rfind('\n').map_or(0, |i| i + 1);
            if !src[line_start..value_start].trim().is_empty() {
                // Something other than indentation precedes the body on
                // its line -- a flow collection or scalar sharing the
                // key's line. Not offered for replacement; the caller
                // falls back to re-serializing.
                continue;
            }
            // Trim only past the last scalar the section actually
            // contains. Without that floor a `#` line inside a block
            // scalar reads as a comment and the value loses its last
            // line.
            let floor = last_scalar_end(value).map_or(line_start, byte_of);
            let end =
                trim_trailing_prose(src, floor.max(line_start), byte_of(value.span.end.index()));
            sections.insert(
                name.to_string(),
                Section {
                    indent: value_start - line_start,
                    body: line_start..end,
                },
            );
        }
        Some(Template {
            source: src.to_string(),
            sections,
        })
    }

    /// The text of a section's body, at the indentation it sits at.
    pub(crate) fn body(&self, section: &Section) -> &str {
        &self.source[section.body.clone()]
    }

    /// The named section, if the document has one.
    pub(crate) fn section(&self, name: &str) -> Option<&Section> {
        self.sections.get(name)
    }

    /// The document with each named section's body replaced. Sections
    /// not named are copied through unchanged, as is everything between
    /// them.
    ///
    /// A replacement is the body's text at column 0; it is indented here
    /// to match where it is going, so a document indented differently
    /// from whatever produced the replacement still comes out
    /// consistent.
    pub(crate) fn render(&self, replacements: &HashMap<&str, String>) -> String {
        let mut holes: Vec<(&Range<usize>, usize, &String)> = replacements
            .iter()
            .filter_map(|(name, text)| {
                let section = self.sections.get(*name)?;
                Some((&section.body, section.indent, text))
            })
            .collect();
        holes.sort_by_key(|(range, _, _)| range.start);

        let mut out = String::with_capacity(self.source.len());
        let mut cursor = 0;
        for (range, indent, text) in holes {
            out.push_str(&self.source[cursor..range.start]);
            out.push_str(&indent_block(text, indent));
            cursor = range.end;
        }
        out.push_str(&self.source[cursor..]);
        out
    }
}

/// The end of the last scalar anywhere under `node`, in char indices —
/// the point past which nothing in the source is part of a value.
///
/// Only scalars count. A collection's span runs to wherever the next
/// token begins, which is exactly the over-reach this is meant to bound,
/// and the innermost collection over-reaches to the same place as its
/// outermost parent — so including them would leave nothing to trim.
fn last_scalar_end(node: &saphyr::MarkedYaml<'_>) -> Option<usize> {
    use saphyr::YamlData;
    match &node.data {
        YamlData::Value(_) | YamlData::Representation(..) => Some(node.span.end.index()),
        // Keys as well as values: a section can end on a key whose value
        // is empty, and that key is still the last of its content.
        YamlData::Mapping(map) => map
            .iter()
            .flat_map(|(k, v)| [last_scalar_end(k), last_scalar_end(v)])
            .flatten()
            .max(),
        YamlData::Sequence(items) => items.iter().filter_map(last_scalar_end).max(),
        YamlData::Tagged(_, inner) => last_scalar_end(inner),
        YamlData::Alias(_) | YamlData::BadValue => None,
    }
}

/// Pull `end` back over trailing blank and comment lines, stopping at
/// `floor`.
///
/// A section's span runs to wherever the next key begins, so a comment
/// written above the *next* section lands inside the previous one's
/// body. Replacing that body would delete a comment about something
/// else, so those lines are left in the template — which also means a
/// comment genuinely trailing the last record stays put textually,
/// rather than moving with a record that has been re-sorted away.
///
/// `floor` is what keeps this from reading a value as prose: a line
/// inside a block scalar may look exactly like a comment, so the caller
/// passes the end of the section's last scalar and nothing before it is
/// considered.
fn trim_trailing_prose(src: &str, floor: usize, mut end: usize) -> usize {
    while end > floor {
        // A body ends just past the newline before the next key, so look
        // at the line before that newline rather than at an empty tail.
        let content_end = src[..end].strip_suffix('\n').map_or(end, str::len);
        let line_start = src[..content_end].rfind('\n').map_or(0, |i| i + 1);
        // `floor` usually lands mid-line -- a scalar ends before the
        // newline that follows it -- so a line it falls inside is the
        // last line of the value and stays.
        if line_start < floor {
            break;
        }
        let line = src[line_start..content_end].trim_start();
        if line.is_empty() || line.starts_with('#') {
            end = line_start;
        } else {
            break;
        }
    }
    end
}

/// Strip up to `indent` leading spaces from every line, the inverse of
/// [`indent_block`]. Used to take a section body out of one document at
/// its indentation before putting it into another at that one\'s.
pub(crate) fn dedent_block(text: &str, indent: usize) -> String {
    let mut out = String::with_capacity(text.len());
    for line in text.lines() {
        let strip = line.len() - line.trim_start_matches(' ').len();
        out.push_str(&line[strip.min(indent)..]);
        out.push('\n');
    }
    out
}

/// Prefix every non-empty line of `text` with `indent` spaces.
fn indent_block(text: &str, indent: usize) -> String {
    let pad = " ".repeat(indent);
    let mut out = String::with_capacity(text.len() + text.lines().count() * indent);
    for line in text.lines() {
        if !line.is_empty() {
            out.push_str(&pad);
            out.push_str(line);
        }
        out.push('\n');
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const DOC: &str = "\
# what this file is
apiVersion: unfurl/v1alpha1
kind: CloudMap
repositories:
  b:
    name: b
  a:
    name: a

# about artifacts
artifacts:
  z:
    name: z
";

    fn body<'a>(t: &'a Template, name: &str) -> &'a str {
        &t.source[t.section(name).expect("section").body.clone()]
    }

    #[test]
    fn a_body_stops_before_a_comment_about_what_follows() {
        let t = Template::parse(DOC).expect("parses");
        // The blank line and the comment sit inside the span saphyr
        // reports for `repositories`; leaving them out of the body is
        // what keeps a comment about `artifacts` from being deleted when
        // `repositories` is rewritten.
        assert_eq!(
            body(&t, "repositories"),
            "  b:\n    name: b\n  a:\n    name: a\n"
        );
        assert_eq!(body(&t, "artifacts"), "  z:\n    name: z\n");
        assert_eq!(t.section("repositories").expect("section").indent, 2);
    }

    /// A block scalar can hold a line that starts with `#`. It is text,
    /// not a comment, and trimming it off the end of the section that
    /// holds it would cut into a value.
    #[test]
    fn a_hash_line_inside_a_block_scalar_is_not_trailing_prose() {
        const BLOCK: &str = "\
kind: CloudMap
repositories:
  a:
    notes: |
      first line
      # text, not a comment
artifacts:
  z:
    name: z
";
        let t = Template::parse(BLOCK).expect("parses");
        assert_eq!(
            body(&t, "repositories"),
            "  a:\n    notes: |\n      first line\n      # text, not a comment\n"
        );
    }

    /// The same, at the end of the document, where there is no next key
    /// to bound the section.
    #[test]
    fn a_trailing_block_scalar_keeps_its_hash_lines() {
        const BLOCK: &str = "\
kind: CloudMap
repositories:
  a:
    notes: |
      first line
      # text, not a comment
";
        let t = Template::parse(BLOCK).expect("parses");
        assert_eq!(
            body(&t, "repositories"),
            "  a:\n    notes: |\n      first line\n      # text, not a comment\n"
        );
        // And the value survives a round trip through a replacement of
        // some *other* part of the document.
        let out = t.render(&HashMap::from([("kind", "CloudMap\n".to_string())]));
        let parsed: serde_json::Value = serde_saphyr::from_str(&out).expect("still valid yaml");
        assert_eq!(
            parsed["repositories"]["a"]["notes"],
            "first line\n# text, not a comment\n"
        );
    }

    #[test]
    fn replacing_a_section_leaves_every_other_byte_alone() {
        let t = Template::parse(DOC).expect("parses");
        let out = t.render(&HashMap::from([(
            "repositories",
            "a:\n  name: a\nc:\n  name: c\n".to_string(),
        )]));
        assert_eq!(
            out,
            "\
# what this file is
apiVersion: unfurl/v1alpha1
kind: CloudMap
repositories:
  a:
    name: a
  c:
    name: c

# about artifacts
artifacts:
  z:
    name: z
"
        );
    }

    #[test]
    fn replacing_every_section_still_keeps_the_envelope_and_comments() {
        let t = Template::parse(DOC).expect("parses");
        let out = t.render(&HashMap::from([
            ("repositories", "x:\n  name: x\n".to_string()),
            ("artifacts", "y:\n  name: y\n".to_string()),
        ]));
        assert!(out.starts_with("# what this file is\napiVersion:"), "{out}");
        assert!(out.contains("# about artifacts\n"), "{out}");
        assert!(out.contains("  x:\n    name: x\n"), "{out}");
        assert!(out.contains("  y:\n    name: y\n"), "{out}");
        assert!(!out.contains("name: z"), "{out}");
    }

    #[test]
    fn a_replacement_round_trips_as_yaml() {
        let t = Template::parse(DOC).expect("parses");
        let out = t.render(&HashMap::from([(
            "repositories",
            "a:\n  name: a\n".to_string(),
        )]));
        let parsed: serde_json::Value = serde_saphyr::from_str(&out).expect("still valid yaml");
        assert_eq!(parsed["repositories"]["a"]["name"], "a");
        assert_eq!(parsed["artifacts"]["z"]["name"], "z");
        assert_eq!(parsed["kind"], "CloudMap");
    }

    /// The shapes a real cloudmap has -- URL keys containing `#`, block
    /// sequences, nested maps -- rather than a hand-made two-key toy.
    #[test]
    fn the_real_fixture_splits_into_sections() {
        let src = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/expected_cloudmap.yaml"
        ))
        .expect("fixture");
        let t = Template::parse(&src).expect("parses");
        for name in [
            "repositories",
            "artifacts",
            "services",
            "instantiations",
            "types",
        ] {
            let section = t.section(name).unwrap_or_else(|| panic!("{name} missing"));
            assert_eq!(section.indent, 2, "{name}");
            assert!(!src[section.body.clone()].is_empty(), "{name} is empty");
        }
        // Replacing one section leaves the document parseable and every
        // other section untouched.
        let out = t.render(&HashMap::from([(
            "services",
            "only:\n  type: {}\n".to_string(),
        )]));
        let parsed: serde_json::Value = serde_saphyr::from_str(&out).expect("still valid yaml");
        assert!(parsed["services"].get("only").is_some(), "{out}");
        let before: serde_json::Value = serde_saphyr::from_str(&src).expect("fixture parses");
        assert_eq!(parsed["repositories"], before["repositories"]);
        assert_eq!(parsed["types"], before["types"]);
    }

    /// Byte and character offsets diverge as soon as a document holds
    /// multibyte text, and the parser reports one of the two. A section
    /// that follows such text has to still be cut at the right place --
    /// an off-by-a-few-bytes hole would splice into the middle of a
    /// line.
    #[test]
    fn a_section_after_multibyte_text_is_still_located_correctly() {
        let doc = "\
kind: CloudMap
repositories:
  a:
    name: caf\u{e9} \u{2615}
artifacts:
  z:
    name: z
";
        let t = Template::parse(doc).expect("parses");
        assert_eq!(body(&t, "artifacts"), "  z:\n    name: z\n");
        assert_eq!(
            body(&t, "repositories"),
            "  a:\n    name: caf\u{e9} \u{2615}\n"
        );
        let out = t.render(&HashMap::from([(
            "artifacts",
            "spliced:\n  name: spliced\n".to_string(),
        )]));
        let parsed: serde_json::Value = serde_saphyr::from_str(&out).expect("still valid yaml");
        assert_eq!(parsed["repositories"]["a"]["name"], "caf\u{e9} \u{2615}");
        assert_eq!(parsed["artifacts"]["spliced"]["name"], "spliced");
    }

    /// A section written in flow style shares its key's line, so there
    /// is no line-aligned hole to cut. It must be left out rather than
    /// spliced -- widening to the line start would swallow the key.
    #[test]
    fn a_flow_style_section_is_not_offered() {
        let t = Template::parse("kind: CloudMap\nrepositories: {a: {name: a}}\n").expect("parses");
        assert!(t.section("repositories").is_none());
        assert!(t.section("kind").is_none(), "a scalar shares its line too");
    }

    #[test]
    fn a_document_that_is_not_a_mapping_has_no_template() {
        assert!(Template::parse("- a\n- b\n").is_none());
        assert!(Template::parse("just a scalar\n").is_none());
        assert!(Template::parse("key: [unclosed\n").is_none());
    }
}
