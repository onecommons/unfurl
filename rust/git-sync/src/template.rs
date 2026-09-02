// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Rewriting a document by replacing whole sections, leaving the rest of
//! the bytes alone.
//!
//! The record round-trip goes through a `serde_json::Value`, which holds
//! no comments — so re-serializing a whole document drops every comment
//! in it, including ones nowhere near the records that changed. A
//! document's *layout* is generated (sections are key-sorted, fields have
//! a canonical order), so there is nothing to preserve there; what people
//! lose is what they wrote.
//!
//! Sections are the right granularity for that. Record-level spans would
//! be invalidated by the sort — add one record and every later one moves
//! — but sorting happens *within* a section, so a section-sized hole
//! survives it. Everything outside the holes, envelope included, is
//! copied through byte for byte.

use granit_parser::{Event, Parser};
use std::collections::HashMap;
use std::ops::Range;

/// One parser event and the bytes of `src` it covers.
type Located<'a> = (Event<'a>, Range<usize>);

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
        let events = scan(src)?;

        // The stream and document preamble, then the root, which has to
        // be a mapping for sections to mean anything.
        let mut i = 0;
        while matches!(
            events.get(i)?.0,
            Event::StreamStart | Event::DocumentStart(..)
        ) {
            i += 1;
        }
        if !matches!(events.get(i)?.0, Event::MappingStart(..)) {
            return None;
        }
        i += 1;

        let mut sections = HashMap::new();
        while let Some((event, _)) = events.get(i) {
            if matches!(event, Event::MappingEnd) {
                break;
            }
            let name = match event {
                Event::Scalar(text, ..) => Some(text.to_string()),
                // A complex key (`? [a, b]`) names no section, but its
                // pair still has to be stepped over.
                _ => None,
            };
            let value = end_of_node(&events, i);
            i = end_of_node(&events, value);
            let (Some(name), Some((_, first))) = (name, events.get(value)) else {
                continue;
            };

            let value_start = first.start;
            // Take whole lines: the body's first character sits after the
            // indentation, which is already in the source, so a hole that
            // began there would have a replacement indented twice. Widen
            // to the line start and the hole becomes line-aligned.
            let line_start = src[..value_start].rfind('\n').map_or(0, |n| n + 1);
            if !src[line_start..value_start].trim().is_empty() {
                // Something other than indentation precedes the body on
                // its line -- a flow collection, a scalar, or an empty
                // value sharing the key's line. Not offered for
                // replacement; the caller falls back to re-serializing.
                continue;
            }
            let last = last_content_end(&events[value..i]).unwrap_or(value_start);
            sections.insert(
                name,
                Section {
                    indent: value_start - line_start,
                    body: line_start..line_end(src, last),
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

/// Every event in `src` with the bytes it covers, or `None` if the
/// document does not parse or the parser cannot place an event in the
/// source.
///
/// Comments are dropped here. They are the reason this module exists,
/// but they play no part in finding a section: what bounds a body is the
/// last *content* event inside it, and a comment is never that.
fn scan(src: &str) -> Option<Vec<Located<'_>>> {
    let mut events = Vec::new();
    for next in Parser::new_from_str(src) {
        let (event, span) = next.ok()?;
        if matches!(event, Event::Comment(..)) {
            continue;
        }
        events.push((event, span.byte_range()?));
    }
    Some(events)
}

/// The index one past the last event of the node beginning at `i`.
fn end_of_node(events: &[Located<'_>], mut i: usize) -> usize {
    let mut depth = 0usize;
    while let Some((event, _)) = events.get(i) {
        i += 1;
        match event {
            Event::MappingStart(..) | Event::SequenceStart(..) => depth += 1,
            Event::MappingEnd | Event::SequenceEnd => {
                depth = depth.saturating_sub(1);
                if depth == 0 {
                    return i;
                }
            }
            _ if depth == 0 => return i,
            _ => {}
        }
    }
    i
}

/// The end of the last event in `node` that is part of a value.
///
/// A collection's closing event carries the position of whatever comes
/// *after* the collection rather than of the collection itself, so
/// counting it would run every section to the start of the next one --
/// over any comment sitting in between. Content events are what mark
/// where a value really stops, and a line of a block scalar is one of
/// them however much it looks like a comment.
fn last_content_end(node: &[Located<'_>]) -> Option<usize> {
    node.iter()
        .filter(|(event, _)| !matches!(event, Event::MappingEnd | Event::SequenceEnd))
        .map(|(_, range)| range.end)
        .max()
}

/// The start of the line after the one `at` ends on, so that a body is
/// always cut at a line boundary.
///
/// `at` is an exclusive end. When it already sits just past a newline --
/// a block scalar takes in the line break that ends it -- the body stops
/// there instead of swallowing the line that follows.
fn line_end(src: &str, at: usize) -> usize {
    if at > 0 && src.as_bytes()[at - 1] == b'\n' {
        return at;
    }
    src[at..].find('\n').map_or(src.len(), |i| at + i + 1)
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
        // The blank line and the comment fall between the last record of
        // `repositories` and the event that closes it; leaving them out
        // of the body is what keeps a comment about `artifacts` from
        // being deleted when `repositories` is rewritten.
        assert_eq!(
            body(&t, "repositories"),
            "  b:\n    name: b\n  a:\n    name: a\n"
        );
        assert_eq!(body(&t, "artifacts"), "  z:\n    name: z\n");
        assert_eq!(t.section("repositories").expect("section").indent, 2);
    }

    /// A block scalar can hold a line that starts with `#`. It is text,
    /// not a comment, and cutting it off the end of the section that
    /// holds it would take a line out of a value.
    #[test]
    fn a_hash_line_inside_a_block_scalar_is_not_a_comment() {
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
    /// multibyte text, and the parser can report either. A section that
    /// follows such text has to still be cut at the right place -- an
    /// off-by-a-few-bytes hole would splice into the middle of a line.
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

    /// An empty section has no body to replace -- the parser puts its
    /// null value on the key's own line -- so it is left out for the
    /// same reason a flow one is, and a write touching it re-serializes
    /// the document instead.
    #[test]
    fn an_empty_section_is_not_offered() {
        let t = Template::parse("kind: CloudMap\nrepositories:\nartifacts:\n  z:\n    name: z\n")
            .expect("parses");
        assert!(t.section("repositories").is_none());
        assert_eq!(body(&t, "artifacts"), "  z:\n    name: z\n");
    }

    #[test]
    fn a_document_that_is_not_a_mapping_has_no_template() {
        assert!(Template::parse("- a\n- b\n").is_none());
        assert!(Template::parse("just a scalar\n").is_none());
        assert!(Template::parse("key: [unclosed\n").is_none());
    }
}
