// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Writes a [`serde_json::Value`] as block-style YAML: two-space indents,
//! a sequence at its key's column, `{}` and `[]` for empty collections,
//! `key:` for a null in a mapping and `- null` in a sequence.
//!
//! A string is written plain when YAML 1.2 readers, YAML 1.1 readers
//! and ruamel's resolver all read it back as that string; otherwise
//! single-quoted, or double-quoted when it holds a `'` or a character
//! that needs an escape. A multi-line value is a literal block when one
//! can hold it exactly. A plain or single-quoted string running past
//! column 80 is wrapped as ruamel wraps it; nothing is folded, and a
//! double-quoted string stays on one line.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value};

/// The longest key, in bytes as written, that goes on its value's line:
/// YAML readers reject a longer implicit key.
const MAX_IMPLICIT_KEY: usize = 1024;

/// The most characters ruamel writes on its value's line as a key: its
/// limit, 1024, counts the key's `!!str` tag too.
const MAX_RUAMEL_KEY: usize = 1018;

/// The column past which ruamel breaks a line at its next single space.
const WIDTH: usize = 80;

/// Plain scalars a YAML 1.1 or 1.2 reader, or ruamel's, takes for
/// something other than a string: null, booleans (YAML 1.1's in any
/// case), numbers, timestamps, and the merge and value keys.
static NOT_A_STRING: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?x)^(?:
            (?i:true|false|null|y|yes|n|no|on|off) | ~ | = | <<
          | [-+]?(?:0b[01_]+|0o[0-7_]+|0x[0-9a-fA-F_]+)
          | [-+][0-9_]+ | [0-9][0-9_]*
          | [-+]?(?:[0-9][0-9_]*\.[0-9_]*|\.[0-9_]+)(?:[eE][-+]?[0-9]+)?
          | [-+]?[0-9][0-9_]*[eE][-+]?[0-9]+
          | [-+]?\.(?i:inf|nan)
          | [0-9]{4}-[0-9]{2}-[0-9]{2}
          | [0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt]|[\ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}
            (?:\.[0-9]*)?(?:[\ \t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?
        )$",
    )
    .expect("valid regex")
});

/// `value` as a YAML document, ending with a newline.
pub fn to_yaml(value: &Value) -> String {
    to_yaml_at(value, 0)
}

/// `value` as a YAML body at column 0 that goes `column` columns in:
/// its long strings are wrapped where they cross column 80 there.
pub fn to_yaml_at(value: &Value, column: usize) -> String {
    let mut writer = Writer {
        out: String::new(),
        base: column,
    };
    writer.node(value, 0, Slot::Root);
    writer.out
}

/// `value` as written on its key's line: starting at `column` of a body
/// whose column 0 is `base` columns in, its continuation lines at
/// `indent`. `None` for a value that can't share its key's line: a
/// non-empty collection or a literal block.
pub fn inline_scalar(value: &Value, column: usize, indent: usize, base: usize) -> Option<String> {
    let mut writer = Writer {
        out: " ".repeat(column),
        base,
    };
    match value {
        Value::String(s) if literal_block(s) => return None,
        Value::String(s) => writer.string(s, indent, true),
        Value::Array(items) if !items.is_empty() => return None,
        Value::Object(map) if !map.is_empty() => return None,
        other => writer.out.push_str(&scalar(other)),
    }
    Some(writer.out.split_off(column))
}

/// What a node follows: nothing, a key's `:`, or a sequence's `-`.
#[derive(Clone, Copy, PartialEq)]
enum Slot {
    Root,
    Key,
    Item,
}

/// YAML text being written for a place `base` columns in.
struct Writer {
    out: String,
    base: usize,
}

impl Writer {
    /// The column the text ends at, in characters, as ruamel counts.
    fn column(&self) -> usize {
        let line = &self.out[self.out.rfind('\n').map_or(0, |n| n + 1)..];
        self.base + line.chars().count()
    }

    fn pad(&mut self, col: usize) {
        self.out.extend(std::iter::repeat_n(' ', col));
    }

    /// `value` written after its slot's indicator, its key or item at
    /// column `col`.
    fn node(&mut self, value: &Value, col: usize, slot: Slot) {
        let sep = if slot == Slot::Root { "" } else { " " };
        match value {
            Value::Object(map) if !map.is_empty() => match slot {
                Slot::Root => self.map(map, col, false),
                Slot::Key => {
                    self.out.push('\n');
                    self.map(map, col + 2, false)
                }
                Slot::Item => {
                    self.out.push(' ');
                    self.map(map, col + 2, true)
                }
            },
            Value::Array(items) if !items.is_empty() => match slot {
                Slot::Root => self.seq(items, col, false),
                Slot::Key => {
                    self.out.push('\n');
                    self.seq(items, col, false)
                }
                Slot::Item => {
                    self.out.push(' ');
                    self.seq(items, col + 2, true)
                }
            },
            Value::Null if slot == Slot::Key => self.out.push('\n'),
            Value::String(s) if literal_block(s) => {
                self.out.push_str(sep);
                self.literal(s, col + 2)
            }
            // a root scalar isn't wrapped: a continuation line at column 0
            // starting `--- ` or `... ` would end the document
            Value::String(s) => {
                self.out.push_str(sep);
                self.string(s, col + 2, slot != Slot::Root);
                self.out.push('\n');
            }
            _ => {
                self.out.push_str(sep);
                self.out.push_str(&scalar(value));
                self.out.push('\n');
            }
        }
    }

    /// The entries of `map` at column `col`, the first on the current
    /// line when `inline_first`.
    fn map(&mut self, map: &Map<String, Value>, col: usize, inline_first: bool) {
        for (i, (key, value)) in map.iter().enumerate() {
            if i > 0 || !inline_first {
                self.pad(col);
            }
            // never wrapped, though ruamel wraps an explicit key: a line
            // break in a URL key broke its readers
            let text = string(key);
            if key.chars().count() > MAX_RUAMEL_KEY || text.len() > MAX_IMPLICIT_KEY {
                // the value follows `: ` as an item follows `- `
                self.out.push_str("? ");
                self.out.push_str(&text);
                self.out.push('\n');
                self.pad(col);
                self.out.push(':');
                match value {
                    Value::Null => self.out.push('\n'),
                    _ => self.node(value, col, Slot::Item),
                }
            } else {
                self.out.push_str(&text);
                self.out.push(':');
                self.node(value, col, Slot::Key);
            }
        }
    }

    /// The items of `items` at column `col`, the first on the current
    /// line when `inline_first`.
    fn seq(&mut self, items: &[Value], col: usize, inline_first: bool) {
        for (i, item) in items.iter().enumerate() {
            if i > 0 || !inline_first {
                self.pad(col);
            }
            self.out.push('-');
            self.node(item, col, Slot::Item);
        }
    }

    /// `s` on one line, or wrapped with its continuation lines at
    /// `indent` when `wrap` and it is plain or single-quoted.
    fn string(&mut self, s: &str, indent: usize, wrap: bool) {
        match style(s) {
            Style::Plain if wrap => self.wrapped(s, None, indent),
            Style::Single if wrap => self.wrapped(s, Some('\''), indent),
            style => self.out.push_str(&render(s, style)),
        }
    }

    /// `text` as ruamel writes a plain or single-quoted scalar: a lone
    /// space reached past column 80 becomes a line break, except a
    /// quoted string's first or last character.
    fn wrapped(&mut self, text: &str, quote: Option<char>, indent: usize) {
        self.out.extend(quote);
        let mut column = self.column();
        let mut start = 0;
        let mut spaces = false;
        let ends = text.char_indices().map(|(i, c)| (i, Some(c)));
        for (end, ch) in ends.chain([(text.len(), None)]) {
            if spaces && ch != Some(' ') {
                let edge = quote.is_some() && (start == 0 || end == text.len());
                if end == start + 1 && column > WIDTH && !edge {
                    self.out.push('\n');
                    self.pad(indent);
                    column = self.base + indent;
                } else {
                    self.out.push_str(&text[start..end]);
                    column += end - start;
                }
                start = end;
            } else if !spaces && ch.is_none_or(|c| c == ' ') {
                self.out.push_str(&text[start..end]);
                column += text[start..end].chars().count();
                start = end;
            }
            if let Some(c) = ch {
                spaces = c == ' ';
            }
        }
        self.out.extend(quote);
    }

    /// `s` as a literal block, its lines at column `col`: `|-` without a
    /// final line break, `|` with one, `|+` with more.
    fn literal(&mut self, s: &str, col: usize) {
        let (chomp, body) = match s.strip_suffix('\n') {
            None => ("-", s),
            Some(rest) if rest.ends_with('\n') => ("+", rest),
            Some(rest) => ("", rest),
        };
        self.out.push('|');
        self.out.push_str(chomp);
        self.out.push('\n');
        for line in body.split('\n') {
            if !line.is_empty() {
                self.pad(col);
                self.out.push_str(line);
            }
            self.out.push('\n');
        }
    }
}

/// A one-line scalar.
fn scalar(value: &Value) -> String {
    match value {
        Value::Null => "null".to_string(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) if n.is_f64() => float(&n.to_string()),
        Value::Number(n) => n.to_string(),
        Value::String(s) => string(s),
        Value::Array(_) => "[]".to_string(),
        Value::Object(_) => "{}".to_string(),
    }
}

/// serde_json's float text, whose exponent has a sign, with a `.` in the
/// mantissa (`1e+300` is `1.0e+300`), which YAML 1.1 readers need.
fn float(text: &str) -> String {
    match text.split_once('e') {
        Some((mantissa, exponent)) if !mantissa.contains('.') => {
            format!("{mantissa}.0e{exponent}")
        }
        _ => text.to_string(),
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Style {
    Plain,
    Single,
    Double,
}

/// How `s` is written: plain if every reader gives it back, else quoted.
fn style(s: &str) -> Style {
    if s.chars().any(escaped) {
        Style::Double
    } else if plain(s) {
        Style::Plain
    } else if s.contains('\'') {
        Style::Double
    } else {
        Style::Single
    }
}

fn render(s: &str, style: Style) -> String {
    match style {
        Style::Plain => s.to_string(),
        Style::Single => format!("'{s}'"),
        Style::Double => double_quoted(s),
    }
}

/// `s` as a one-line scalar.
fn string(s: &str) -> String {
    render(s, style(s))
}

/// Whether `c` is written as an escape: a character YAML can't hold
/// as it is, a line break, a tab, or a byte order mark.
fn escaped(c: char) -> bool {
    !matches!(c, ' '..='~' | '\u{A0}'..='\u{D7FF}' | '\u{E000}'..='\u{FFFD}' | '\u{10000}'..)
        || matches!(c, '\u{2028}' | '\u{2029}' | '\u{FEFF}')
}

/// Whether `s`, holding no character [`escaped`], reads back as itself
/// written plain, as a key or a value.
fn plain(s: &str) -> bool {
    let mut chars = s.chars();
    let (Some(first), second) = (chars.next(), chars.next()) else {
        return false;
    };
    let indicator = match first {
        '-' | '?' | ':' => second.is_none_or(|c| c == ' '),
        _ => "!&*|>'\"%@`#,[]{}".contains(first),
    };
    !(indicator
        || s.starts_with(' ')
        || s.ends_with([' ', ':'])
        || s.starts_with("---")
        || s.starts_with("...")
        || s.contains(": ")
        || s.contains(" #")
        // serde-saphyr resolves a plain scalar with Unicode whitespace
        // such as U+00A0 trimmed
        || NOT_A_STRING.is_match(s.trim()))
}

fn double_quoted(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if escaped(c) => out.push_str(&escape(c)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn escape(c: char) -> String {
    let named = match c {
        '\0' => '0',
        '\u{7}' => 'a',
        '\u{8}' => 'b',
        '\t' => 't',
        '\n' => 'n',
        '\u{B}' => 'v',
        '\u{C}' => 'f',
        '\r' => 'r',
        '\u{1B}' => 'e',
        '\u{85}' => 'N',
        '\u{2028}' => 'L',
        '\u{2029}' => 'P',
        c if (c as u32) <= 0xFF => return format!("\\x{:02X}", c as u32),
        c => return format!("\\u{:04X}", c as u32),
    };
    format!("\\{named}")
}

/// Whether `s` is written as a literal block: it has a line break, its
/// first line is neither empty nor starts with a space (either would
/// need an indentation indicator), no line ends in a space or tab, and
/// it has no other character that needs an escape.
fn literal_block(s: &str) -> bool {
    s.contains('\n')
        && !s.starts_with(['\n', ' '])
        && !s.ends_with([' ', '\t'])
        && !s.contains(" \n")
        && !s.contains("\t\n")
        && !s.chars().any(|c| c != '\n' && c != '\t' && escaped(c))
}

#[cfg(test)]
mod tests {
    use super::{inline_scalar, to_yaml, to_yaml_at};
    use proptest::prelude::*;
    use serde_json::{json, Value};

    fn strict(text: &str) -> Value {
        serde_saphyr::from_str_with_options(text, serde_saphyr::options! { strict_booleans: true })
            .unwrap_or_else(|e| panic!("{text:?}: {e}"))
    }

    /// What a YAML 1.1 reader makes of booleans.
    fn yaml_1_1(text: &str) -> Value {
        serde_saphyr::from_str(text).unwrap_or_else(|e| panic!("{text:?}: {e}"))
    }

    fn assert_round_trips(value: &Value) {
        let text = to_yaml(value);
        assert_eq!(&strict(&text), value, "{text}");
        assert_eq!(&yaml_1_1(&text), value, "{text}");
    }

    /// Each string and how it is written on one line, as a key or a value.
    const STRINGS: &[(&str, &str)] = &[
        // plain, as ruamel writes them
        ("it's", "it's"),
        ("say \"hi\"", "say \"hi\""),
        ("https://e.com/a?b=c&d", "https://e.com/a?b=c&d"),
        ("git://e.com/a.git", "git://e.com/a.git"),
        ("1:30", "1:30"),
        ("190:20:30", "190:20:30"),
        ("a:b", "a:b"),
        ("a#b", "a#b"),
        ("x#", "x#"),
        ("a :b", "a :b"),
        ("-dash", "-dash"),
        ("?q", "?q"),
        (":colon", ":colon"),
        ("a,b", "a,b"),
        ("a[b]", "a[b]"),
        ("a ... b", "a ... b"),
        ("\u{1F600}", "\u{1F600}"),
        (
            "\u{1F468}\u{200D}\u{1F469}\u{200D}\u{1F467}",
            "\u{1F468}\u{200D}\u{1F469}\u{200D}\u{1F467}",
        ),
        ("\u{2764}\u{FE0F}", "\u{2764}\u{FE0F}"),
        ("\u{1F1FA}\u{1F1F8}", "\u{1F1FA}\u{1F1F8}"),
        ("\u{1F600} first", "\u{1F600} first"),
        ("a\u{A0}b", "a\u{A0}b"),
        ("\u{A0}a\u{A0}", "\u{A0}a\u{A0}"),
        ("\u{A0}0", "'\u{A0}0'"),
        ("true\u{2003}", "'true\u{2003}'"),
        ("a\u{200B}b", "a\u{200B}b"),
        ("e\u{E9}", "e\u{E9}"),
        ("1.2.3", "1.2.3"),
        ("v1.2", "v1.2"),
        ("1,000", "1,000"),
        ("nan", "nan"),
        ("inf", "inf"),
        ("0b2", "0b2"),
        ("2001-1-4", "2001-1-4"),
        ("_", "_"),
        ("<<x", "<<x"),
        // single-quoted
        ("a: b", "'a: b'"),
        ("a #b", "'a #b'"),
        (" lead", "' lead'"),
        ("trail ", "'trail '"),
        ("", "''"),
        ("*star", "'*star'"),
        ("- dash", "'- dash'"),
        ("-", "'-'"),
        ("? q", "'? q'"),
        (": colon", "': colon'"),
        ("{brace", "'{brace'"),
        ("}brace", "'}brace'"),
        ("[sq", "'[sq'"),
        (",comma", "',comma'"),
        ("@at", "'@at'"),
        ("%pct", "'%pct'"),
        ("#hash", "'#hash'"),
        ("!bang", "'!bang'"),
        ("&amp", "'&amp'"),
        ("|pipe", "'|pipe'"),
        (">gt", "'>gt'"),
        ("`tick", "'`tick'"),
        ("\"dq", "'\"dq'"),
        ("a:", "'a:'"),
        ("---", "'---'"),
        ("--- x", "'--- x'"),
        ("...", "'...'"),
        ("=", "'='"),
        ("<<", "'<<'"),
        ("true", "'true'"),
        ("False", "'False'"),
        ("tRUE", "'tRUE'"),
        ("null", "'null'"),
        ("nULL", "'nULL'"),
        ("~", "'~'"),
        ("1", "'1'"),
        ("-1", "'-1'"),
        ("+0", "'+0'"),
        ("010", "'010'"),
        ("08", "'08'"),
        ("1_000", "'1_000'"),
        ("1.5", "'1.5'"),
        ("1.", "'1.'"),
        (".5", "'.5'"),
        ("1e3", "'1e3'"),
        (".5e5", "'.5e5'"),
        ("0x1F", "'0x1F'"),
        ("0o17", "'0o17'"),
        ("0b101", "'0b101'"),
        (".inf", "'.inf'"),
        ("-.Inf", "'-.Inf'"),
        (".NaN", "'.NaN'"),
        ("2001-12-14", "'2001-12-14'"),
        ("2001-12-14T21:59:43Z", "'2001-12-14T21:59:43Z'"),
        ("2001-12-14 21:59:43.10 -5", "'2001-12-14 21:59:43.10 -5'"),
        // YAML 1.1 booleans, which ruamel writes plain
        ("y", "'y'"),
        ("Y", "'Y'"),
        ("yes", "'yes'"),
        ("Yes", "'Yes'"),
        ("yEs", "'yEs'"),
        ("n", "'n'"),
        ("no", "'no'"),
        ("NO", "'NO'"),
        ("on", "'on'"),
        ("oN", "'oN'"),
        ("off", "'off'"),
        ("OFF", "'OFF'"),
        // double-quoted: quoted, holding a '
        ("'quote", "\"'quote\""),
        ("it's: x", "\"it's: x\""),
        ("'both\"", "\"'both\\\"\""),
        // double-quoted: escapes
        ("tab\there", "\"tab\\there\""),
        ("\tlead tab", "\"\\tlead tab\""),
        ("bell\u{7}", "\"bell\\a\""),
        ("nul\0", "\"nul\\0\""),
        ("esc\u{1B}", "\"esc\\e\""),
        ("del\u{7F}", "\"del\\x7F\""),
        ("c1\u{80}", "\"c1\\x80\""),
        ("bom\u{FEFF}", "\"bom\\uFEFF\""),
        ("\u{FEFF}bom", "\"\\uFEFFbom\""),
        ("nel\u{85}x", "\"nel\\Nx\""),
        ("ls\u{2028}x", "\"ls\\Lx\""),
        ("ps\u{2029}x", "\"ps\\Px\""),
        ("non\u{FFFE}", "\"non\\uFFFE\""),
        ("back\\slash\t", "\"back\\\\slash\\t\""),
        ("\u{1F600}\t", "\"\u{1F600}\\t\""),
    ];

    /// Each string as a value, a key, a sequence item and a document.
    #[test]
    fn strings_are_written_plain_or_quoted() {
        for (s, want) in STRINGS {
            assert_eq!(to_yaml(&json!({"v": s})), format!("v: {want}\n"), "{s:?}");
            assert_eq!(to_yaml(&json!({*s: 1})), format!("{want}: 1\n"), "{s:?}");
            assert_eq!(to_yaml(&json!([s])), format!("- {want}\n"), "{s:?}");
            assert_eq!(to_yaml(&json!(s)), format!("{want}\n"), "{s:?}");
            for value in [json!({"v": s}), json!({*s: [s]}), json!(s)] {
                assert_round_trips(&value);
            }
        }
    }

    /// Each multi-line string and how it is written as a value: a literal
    /// block where one holds it, else double-quoted.
    const MULTI_LINE: &[(&str, &str)] = &[
        ("a\nb", "v: |-\n  a\n  b\n"),
        ("a\nb\n", "v: |\n  a\n  b\n"),
        ("a\n\nb\n\n", "v: |+\n  a\n\n  b\n\n"),
        ("a\n b", "v: |-\n  a\n   b\n"),
        ("\tx\ny", "v: |-\n  \tx\n  y\n"),
        (
            "# not a comment\n---\n...",
            "v: |-\n  # not a comment\n  ---\n  ...\n",
        ),
        ("\nlead", "v: \"\\nlead\"\n"),
        (" lead\nx", "v: \" lead\\nx\"\n"),
        ("trail \nx", "v: \"trail \\nx\"\n"),
        ("trail\t\nx", "v: \"trail\\t\\nx\"\n"),
        ("a\nb ", "v: \"a\\nb \"\n"),
        ("a\r\nb", "v: \"a\\r\\nb\"\n"),
        ("a\nb\u{85}", "v: \"a\\nb\\N\"\n"),
        ("\n", "v: \"\\n\"\n"),
    ];

    #[test]
    fn multi_line_strings_are_literal_blocks_where_they_can_be() {
        for (s, want) in MULTI_LINE {
            assert_eq!(&to_yaml(&json!({"v": s})), want, "{s:?}");
            assert_round_trips(&json!({"v": s, "l": [s, {"k": s}], "n": {"k": s}}));
        }
        assert_eq!(to_yaml(&json!({"a\nb": 1})), "\"a\\nb\": 1\n");
        assert_eq!(to_yaml(&json!(["a\nb"])), "- |-\n  a\n  b\n");
        assert_eq!(
            to_yaml(&json!({"l": [{"k": "a\nb"}]})),
            "l:\n- k: |-\n    a\n    b\n"
        );
        assert_eq!(to_yaml(&json!("top\nlevel")), "|-\n  top\n  level\n");
    }

    /// Values and the text CloudMapDB's ruamel writes for them,
    /// generated with it.
    const RUAMEL_WRAPS: &[(&str, &str)] = &[
        (
            "{\"k\": \"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx y\"}",
            "k: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx y\n",
        ),
        (
            "{\"k\": \"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx y\"}",
            "k: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n  y\n",
        ),
        (
            "{\"key\": \"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx y\"}",
            "key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx y\n",
        ),
        (
            "{\"key\": \"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx y\"}",
            "key: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n  y\n",
        ),
        (
            "{\"k\": \"\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9} y\"}",
            "k: \u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9} y\n",
        ),
        (
            "{\"k\": \"\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9} y\"}",
            "k: \u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\u{E9}\n  y\n",
        ),
        (
            "{\"k\": \"\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600} y\"}",
            "k: \u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600}\u{1F600} y\n",
        ),
        (
            "{\"\u{E9}\u{E9}\u{E9}\": \"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx y z\"}",
            "\u{E9}\u{E9}\u{E9}: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx y z\n",
        ),
        (
            "{\"k\": \"\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \"}",
            "k: '\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\n  \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\n  \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} \u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D}\u{4E2D} '\n",
        ),
        (
            "{\"k\": \"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  y\"}",
            "k: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  y\n",
        ),
        (
            "{\"k\": \"w00  w01  w02  w03  w04  w05  w06  w07  w08  w09  w10  w11  w12  w13  w14  w15  w16  w17  w18  w19  w20  w21  w22  w23  w24  w25  w26  w27  w28  w29  w30  w31  w32  w33  w34  w35  w36  w37  w38  w39\"}",
            "k: w00  w01  w02  w03  w04  w05  w06  w07  w08  w09  w10  w11  w12  w13  w14  w15  w16  w17  w18  w19  w20  w21  w22  w23  w24  w25  w26  w27  w28  w29  w30  w31  w32  w33  w34  w35  w36  w37  w38  w39\n",
        ),
        (
            "{\"k\": \"aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa bb  cc bb  cc bb  cc bb  cc bb  cc bb  cc \"}",
            "k: 'aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa bb  cc\n  bb  cc bb  cc bb  cc bb  cc bb  cc '\n",
        ),
        (
            "{\"k\": \"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"}",
            "k: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n  w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19\n  w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\n",
        ),
        (
            "{\"k\": \"aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx tail\"}",
            "k: aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n  tail\n",
        ),
        (
            "{\"k\": \"xxxxxxxxxx\u{A0}xxxxxxxxxx\u{A0}xxxxxxxxxx\u{A0}xxxxxxxxxx\u{A0}xxxxxxxxxx\u{A0}xxxxxxxxxx\u{A0}xxxxxxxxxx\u{A0}xxxxxxxxxx\u{A0}xxxxxxxxxx\u{A0} y\"}",
            "k: xxxxxxxxxx\u{A0}xxxxxxxxxx\u{A0}xxxxxxxxxx\u{A0}xxxxxxxxxx\u{A0}xxxxxxxxxx\u{A0}xxxxxxxxxx\u{A0}xxxxxxxxxx\u{A0}xxxxxxxxxx\u{A0}xxxxxxxxxx\u{A0}\n  y\n",
        ),
        (
            "{\"k\": \"w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"}",
            "k: w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19\n  w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\n",
        ),
        (
            "{\"a\": {\"b\": {\"k\": \"w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"}}}",
            "a:\n  b:\n    k: w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18\n      w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37\n      w38 w39\n",
        ),
        (
            "[\"w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"]",
            "- w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19\n  w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\n",
        ),
        (
            "{\"a\": [\"w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"]}",
            "a:\n- w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19\n  w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\n",
        ),
        (
            "{\"a\": [{\"k\": \"w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"}]}",
            "a:\n- k: w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19\n    w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\n",
        ),
        (
            "{\"a\": [[\"w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"]]}",
            "a:\n- - w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19\n    w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\n",
        ),
        (
            "{\"a\": [{\"k\": [\"w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"]}]}",
            "a:\n- k:\n  - w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19\n    w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\n",
        ),
        (
            "{\"k\": \"*w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"}",
            "k: '*w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19\n  w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39'\n",
        ),
        (
            "{\"k\": \"w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39:\"}",
            "k: 'w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19\n  w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39:'\n",
        ),
        (
            "{\"k\": \" w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"}",
            "k: ' w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19\n  w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39'\n",
        ),
        (
            "{\"k\": \"w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39 \"}",
            "k: 'w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19\n  w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39 '\n",
        ),
        (
            "{\"k\": \" xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx y\"}",
            "k: ' xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n  y'\n",
        ),
        (
            "{\"k\": \"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \"}",
            "k: 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx '\n",
        ),
        (
            "{\"k\": \"*xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx y\"}",
            "k: '*xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n  y'\n",
        ),
        (
            "{\"k\": \"aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa - bbb ccc\"}",
            "k: aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa - bbb\n  ccc\n",
        ),
        (
            "{\"k\": \"aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa ? bbb ccc\"}",
            "k: aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa ? bbb\n  ccc\n",
        ),
        (
            "{\"k\": \"aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa :x ccc\"}",
            "k: aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa :x\n  ccc\n",
        ),
        (
            "{\"k\": \"aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa *x &y !z\"}",
            "k: aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa *x\n  &y !z\n",
        ),
        (
            "{\"k\": \"aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa 'q' \\\"r\\\" ccc\"}",
            "k: aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa 'q'\n  \"r\" ccc\n",
        ),
        (
            "{\"k\": \"aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa [x] {y} ccc\"}",
            "k: aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa [x]\n  {y} ccc\n",
        ),
        (
            "{\"k\": \"aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa --- x ... y\"}",
            "k: aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa ---\n  x ... y\n",
        ),
        (
            "{\"k\": \"aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa #x y\"}",
            "k: 'aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa aaa #x\n  y'\n",
        ),
    ];

    /// The same for values nested `a:` deep, generated with it.
    const RUAMEL_NESTED: &[(&str, &str)] = &[
        (
            "{\"a\": {\"k\": \"w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"}}",
            "a:\n  k: w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19\n    w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\n",
        ),
        (
            "{\"a\": {\"k\": \"*w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"}}",
            "a:\n  k: '*w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18\n    w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38\n    w39'\n",
        ),
        (
            "{\"a\": {\"k\": \"aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa b\"}}",
            "a:\n  k: aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa\n    aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa b\n",
        ),
        (
            "{\"a\": {\"a\": {\"k\": \"w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"}}}",
            "a:\n  a:\n    k: w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18\n      w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37\n      w38 w39\n",
        ),
        (
            "{\"a\": {\"a\": {\"k\": \"*w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"}}}",
            "a:\n  a:\n    k: '*w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18\n      w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37\n      w38 w39'\n",
        ),
        (
            "{\"a\": {\"a\": {\"k\": \"aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa b\"}}}",
            "a:\n  a:\n    k: aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa\n      aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa b\n",
        ),
        (
            "{\"a\": {\"a\": {\"a\": {\"k\": \"w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"}}}}",
            "a:\n  a:\n    a:\n      k: w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18\n        w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37\n        w38 w39\n",
        ),
        (
            "{\"a\": {\"a\": {\"a\": {\"k\": \"*w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"}}}}",
            "a:\n  a:\n    a:\n      k: '*w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17\n        w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36\n        w37 w38 w39'\n",
        ),
        (
            "{\"a\": {\"a\": {\"a\": {\"k\": \"aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa b\"}}}}",
            "a:\n  a:\n    a:\n      k: aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa\n        aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa\n        b\n",
        ),
        (
            "{\"a\": {\"a\": {\"a\": {\"a\": {\"a\": {\"k\": \"w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"}}}}}}",
            "a:\n  a:\n    a:\n      a:\n        a:\n          k: w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17\n            w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35\n            w36 w37 w38 w39\n",
        ),
        (
            "{\"a\": {\"a\": {\"a\": {\"a\": {\"a\": {\"k\": \"*w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16 w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34 w35 w36 w37 w38 w39\"}}}}}}",
            "a:\n  a:\n    a:\n      a:\n        a:\n          k: '*w00 w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 w16\n            w17 w18 w19 w20 w21 w22 w23 w24 w25 w26 w27 w28 w29 w30 w31 w32 w33 w34\n            w35 w36 w37 w38 w39'\n",
        ),
        (
            "{\"a\": {\"a\": {\"a\": {\"a\": {\"a\": {\"k\": \"aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa b\"}}}}}}",
            "a:\n  a:\n    a:\n      a:\n        a:\n          k: aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa\n            aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa aaaa\n            aaaa aaaa b\n",
        ),
    ];

    /// ruamel's text for a value `depth` levels down is that value's body
    /// for a place `2 * depth` columns in.
    #[test]
    fn a_nested_value_wraps_as_its_body_does_further_in() {
        for (text, want) in RUAMEL_NESTED {
            let mut value: Value = serde_json::from_str(text).expect("json");
            assert_eq!(&to_yaml(&value), want, "{text}");
            let mut depth = 0;
            while let Some(inner) = value.get("a") {
                value = inner.clone();
                depth += 1;
            }
            let body: String = want
                .lines()
                .skip(depth)
                .map(|line| format!("{}\n", &line[2 * depth..]))
                .collect();
            assert_eq!(to_yaml_at(&value, 2 * depth), body, "{text}");
        }
    }

    #[test]
    fn long_strings_wrap_as_ruamel_wraps_them() {
        for (text, want) in RUAMEL_WRAPS {
            let value: Value = serde_json::from_str(text).expect("json");
            assert_eq!(&to_yaml(&value), want, "{text}");
            assert_round_trips(&value);
        }
    }

    /// A root scalar and a double-quoted string stay on one line, where
    /// ruamel wraps both.
    #[test]
    fn a_root_or_double_quoted_string_is_one_line() {
        let long = format!("{} y", "x".repeat(90));
        assert_eq!(to_yaml(&json!(long)), format!("{long}\n"));
        assert_eq!(
            to_yaml(&json!({"k": format!("\t{long}")})),
            format!("k: \"\\t{long}\"\n")
        );
        let quote = format!("it's: {long}");
        assert_eq!(to_yaml(&json!({"k": quote})), format!("k: \"{quote}\"\n"));
    }

    /// A body for a place further in wraps where it crosses column 80
    /// there.
    #[test]
    fn a_body_further_in_wraps_sooner() {
        let x = "x".repeat(74);
        let value = json!({"k": format!("{x} y")});
        assert_eq!(to_yaml(&value), format!("k: {x} y\n"));
        assert_eq!(to_yaml_at(&value, 3), format!("k: {x} y\n"));
        assert_eq!(to_yaml_at(&value, 4), format!("k: {x}\n  y\n"));
    }

    #[test]
    fn an_inline_scalar_wraps_from_its_column() {
        let x = "x".repeat(70);
        let long = json!(format!("{x} y"));
        assert_eq!(inline_scalar(&long, 6, 2, 4), Some(format!("{x} y")));
        assert_eq!(inline_scalar(&long, 6, 2, 5), Some(format!("{x}\n  y")));
        assert_eq!(
            inline_scalar(&json!("yes"), 6, 2, 0).as_deref(),
            Some("'yes'")
        );
        assert_eq!(
            inline_scalar(&Value::Null, 6, 2, 0).as_deref(),
            Some("null")
        );
        assert_eq!(inline_scalar(&json!([]), 6, 2, 0).as_deref(), Some("[]"));
        assert_eq!(inline_scalar(&json!("a\nb"), 6, 2, 0), None);
        assert_eq!(inline_scalar(&json!([1]), 6, 2, 0), None);
        assert_eq!(inline_scalar(&json!({"a": 1}), 6, 2, 0), None);
    }

    #[test]
    fn a_null_is_an_empty_value_in_a_mapping_and_null_in_a_sequence() {
        let value = json!({
            "anchor": null,
            "list": [null, {}, []],
            "nested": {"a": null, "b": {}},
            "empty": {},
            "none": [],
        });
        let text = to_yaml(&value);
        assert_eq!(
            text,
            "anchor:\nlist:\n- null\n- {}\n- []\nnested:\n  a:\n  b: {}\nempty: {}\nnone: []\n"
        );
        assert_round_trips(&value);
        assert_eq!(to_yaml(&Value::Null), "null\n");
        assert_eq!(to_yaml(&json!({})), "{}\n");
        assert_eq!(to_yaml(&json!([])), "[]\n");
    }

    /// Sequences sit at their key's column; a mapping or sequence in a
    /// sequence starts on its `-` line.
    #[test]
    fn collections_nest_in_block_style() {
        let value = json!({"a": {"b": [1, {"c": 2, "d": [3, 4]}, [5, [6]], "x"]}, "e": "f"});
        assert_eq!(
            to_yaml(&value),
            "a:\n  b:\n  - 1\n  - c: 2\n    d:\n    - 3\n    - 4\n  - - 5\n    - - 6\n  - x\ne: f\n"
        );
        assert_round_trips(&value);
    }

    #[test]
    fn numbers_read_back_equal() {
        let value = json!({
            "int": 1, "neg": i64::MIN, "big": u64::MAX, "half": 1.5, "whole": 1.0,
            "huge": 1e300, "tiny": 1e-7, "negative_zero": -0.0, "true": true,
        });
        assert_eq!(
            to_yaml(&value),
            "int: 1\nneg: -9223372036854775808\nbig: 18446744073709551615\nhalf: 1.5\n\
             whole: 1.0\nhuge: 1.0e+300\ntiny: 1.0e-7\nnegative_zero: -0.0\n'true': true\n"
        );
        assert_round_trips(&value);
    }

    /// A key over 1018 characters, as ruamel writes it, or over 1024
    /// bytes as written, which readers reject on its value's line, is an
    /// explicit `?` key, on one line, where ruamel wraps it.
    #[test]
    fn a_long_key_is_an_explicit_key() {
        let fits = "k".repeat(1018);
        assert_eq!(to_yaml(&json!({&fits: 1})), format!("{fits}: 1\n"));
        let quoted = format!("{}:", "k".repeat(1017));
        assert_eq!(to_yaml(&json!({&quoted: 1})), format!("'{quoted}': 1\n"));
        let long = "k".repeat(1019);
        assert_eq!(to_yaml(&json!({&long: 1})), format!("? {long}\n: 1\n"));
        let wide = "\u{1F600}".repeat(257);
        assert!(to_yaml(&json!({&wide: 1})).starts_with("? "));
        let escaped = "\t".repeat(600);
        assert!(to_yaml(&json!({&escaped: 1})).starts_with("? \""));
        assert_eq!(
            to_yaml(&json!({&long: {"a": 1, "b": [2]}})),
            format!("? {long}\n: a: 1\n  b:\n  - 2\n")
        );
        assert_eq!(
            to_yaml(&json!({&long: [1, {"c": 2}]})),
            format!("? {long}\n: - 1\n  - c: 2\n")
        );
        assert_eq!(to_yaml(&json!({&long: null})), format!("? {long}\n:\n"));
        let words = format!("git://e.com/{}", "a b ".repeat(272));
        assert_eq!(words.len(), 1100);
        assert_eq!(to_yaml(&json!({&words: 1})), format!("? '{words}'\n: 1\n"));
        for key in [fits, quoted, long, wide, escaped, words] {
            assert_round_trips(&json!({&key: 1, "l": [{&key: [1, 2]}, {&key: {"a": null}}]}));
        }
    }

    /// Strings a reader might take for another scalar, with or without
    /// YAML 1.1's booleans.
    fn lookalike() -> impl Strategy<Value = String> {
        prop_oneof![
            "[-+.0-9a-fxobeEinfNa_:~ ]{1,8}",
            "[yYeEsSnNoOfFtTrRuUlLaA~]{1,5}",
            "[-?:#,\\[\\]{}&*!|>'\"%@` a-z\t]{0,8}",
            "[a-z \t\n]{0,12}",
            "[\u{A0}\u{2003}\u{3000}\u{85} ]{0,2}(true|0|1.5|null|~|yes|-1|0x1F)[\u{A0}\u{2003} ]{0,2}",
            any::<String>(),
        ]
    }

    /// Strings long enough to wrap: words of lookalike and indicator
    /// characters joined by one or two spaces, some beside U+00A0.
    fn long_string() -> impl Strategy<Value = String> {
        let word = "[-?:#,&*!|>'\"%@`a-z0-9.~\u{A0}\u{E9}\u{1F600}]{1,14}";
        let gap = prop_oneof![
            Just(" "),
            Just(" "),
            Just("  "),
            Just(" \u{A0}"),
            Just("\u{A0} ")
        ];
        prop::collection::vec((word, gap), 6..40)
            .prop_map(|words| words.into_iter().map(|(w, gap)| w + gap).collect())
    }

    /// Keys of varied width, so values start at varied columns.
    fn key() -> impl Strategy<Value = String> {
        prop_oneof![lookalike(), "[a-z\u{E9}\u{1F600}. ]{1,40}"]
    }

    fn json_value() -> impl Strategy<Value = Value> {
        let leaf = prop_oneof![
            Just(Value::Null),
            any::<bool>().prop_map(Value::Bool),
            any::<i64>().prop_map(Value::from),
            any::<u64>().prop_map(Value::from),
            any::<f64>()
                .prop_filter("finite", |f| f.is_finite())
                .prop_map(Value::from),
            lookalike().prop_map(Value::String),
            long_string().prop_map(Value::String),
        ];
        leaf.prop_recursive(3, 24, 4, |inner| {
            prop_oneof![
                prop::collection::vec(inner.clone(), 0..4).prop_map(Value::Array),
                prop::collection::vec((key(), inner), 0..4)
                    .prop_map(|entries| Value::Object(entries.into_iter().collect())),
            ]
        })
    }

    proptest! {
        #[test]
        fn any_value_reads_back_equal(value in json_value(), base in 0usize..40) {
            let at = to_yaml_at(&value, base);
            let got: Value = serde_saphyr::from_str(&at).expect("at a column");
            prop_assert_eq!(&got, &value, "{}", at);
            let text = to_yaml(&value);
            let got: Value = serde_saphyr::from_str_with_options(
                &text, serde_saphyr::options! { strict_booleans: true }).expect("strict");
            prop_assert_eq!(&got, &value, "{}", text);
            let got: Value = serde_saphyr::from_str(&text).expect("1.1 booleans");
            prop_assert_eq!(&got, &value, "{}", text);
            if let Value::Object(map) = &value {
                let keys: Vec<String> = crate::template::entries(&text)
                    .expect("entries")
                    .into_iter()
                    .map(|(key, _)| key)
                    .collect();
                prop_assert_eq!(keys, map.keys().cloned().collect::<Vec<_>>(), "{}", text);
            }
        }
    }
}
