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
//! can hold it exactly. Nothing is wrapped or folded.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value};

/// The longest key, in bytes as written, that goes on its value's line:
/// YAML readers reject a longer implicit key.
const MAX_IMPLICIT_KEY: usize = 1024;

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
    let mut out = String::new();
    write_node(&mut out, value, 0, Slot::Root);
    out
}

/// What a node follows: nothing, a key's `:`, or a sequence's `-`.
#[derive(Clone, Copy, PartialEq)]
enum Slot {
    Root,
    Key,
    Item,
}

/// `value` written after its slot's indicator, its key or item at
/// column `col`.
fn write_node(out: &mut String, value: &Value, col: usize, slot: Slot) {
    let sep = if slot == Slot::Root { "" } else { " " };
    match value {
        Value::Object(map) if !map.is_empty() => match slot {
            Slot::Root => write_map(out, map, col, false),
            Slot::Key => {
                out.push('\n');
                write_map(out, map, col + 2, false)
            }
            Slot::Item => {
                out.push(' ');
                write_map(out, map, col + 2, true)
            }
        },
        Value::Array(items) if !items.is_empty() => match slot {
            Slot::Root => write_seq(out, items, col, false),
            Slot::Key => {
                out.push('\n');
                write_seq(out, items, col, false)
            }
            Slot::Item => {
                out.push(' ');
                write_seq(out, items, col + 2, true)
            }
        },
        Value::Null if slot == Slot::Key => out.push('\n'),
        Value::String(s) if literal_block(s) => {
            out.push_str(sep);
            write_literal(out, s, col + 2)
        }
        _ => {
            out.push_str(sep);
            out.push_str(&scalar(value));
            out.push('\n');
        }
    }
}

/// The entries of `map` at column `col`, the first on the current line
/// when `inline_first`.
fn write_map(out: &mut String, map: &Map<String, Value>, col: usize, inline_first: bool) {
    for (i, (key, value)) in map.iter().enumerate() {
        if i > 0 || !inline_first {
            pad(out, col);
        }
        let key = string(key);
        if key.len() > MAX_IMPLICIT_KEY {
            out.push_str("? ");
            out.push_str(&key);
            out.push('\n');
            pad(out, col);
        } else {
            out.push_str(&key);
        }
        out.push(':');
        write_node(out, value, col, Slot::Key);
    }
}

/// The items of `items` at column `col`, the first on the current line
/// when `inline_first`.
fn write_seq(out: &mut String, items: &[Value], col: usize, inline_first: bool) {
    for (i, item) in items.iter().enumerate() {
        if i > 0 || !inline_first {
            pad(out, col);
        }
        out.push('-');
        write_node(out, item, col, Slot::Item);
    }
}

fn pad(out: &mut String, col: usize) {
    out.extend(std::iter::repeat_n(' ', col));
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

/// `s` as a one-line scalar: plain if every reader gives it back,
/// else quoted.
fn string(s: &str) -> String {
    if s.chars().any(escaped) {
        double_quoted(s)
    } else if plain(s) {
        s.to_string()
    } else if s.contains('\'') {
        double_quoted(s)
    } else {
        format!("'{s}'")
    }
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

/// `s` as a literal block, its lines at column `col`: `|-` without a
/// final line break, `|` with one, `|+` with more.
fn write_literal(out: &mut String, s: &str, col: usize) {
    let (chomp, body) = match s.strip_suffix('\n') {
        None => ("-", s),
        Some(rest) if rest.ends_with('\n') => ("+", rest),
        Some(rest) => ("", rest),
    };
    out.push('|');
    out.push_str(chomp);
    out.push('\n');
    for line in body.split('\n') {
        if !line.is_empty() {
            pad(out, col);
            out.push_str(line);
        }
        out.push('\n');
    }
}

#[cfg(test)]
mod tests {
    use super::to_yaml;
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

    #[test]
    fn a_long_string_is_one_line() {
        let long = format!("{} {}", "x".repeat(100), "y".repeat(100));
        assert_eq!(to_yaml(&json!({"v": long})), format!("v: {long}\n"));
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

    /// A key over 1024 bytes as written, which readers reject on its
    /// value's line, is an explicit `?` key.
    #[test]
    fn a_long_key_is_an_explicit_key() {
        let fits = "k".repeat(1024);
        assert_eq!(to_yaml(&json!({&fits: 1})), format!("{fits}: 1\n"));
        let quoted = format!("{}:", "k".repeat(1021));
        assert_eq!(to_yaml(&json!({&quoted: 1})), format!("'{quoted}': 1\n"));
        let long = "k".repeat(1025);
        assert_eq!(to_yaml(&json!({&long: 1})), format!("? {long}\n: 1\n"));
        let wide = "\u{1F600}".repeat(257);
        assert!(to_yaml(&json!({&wide: 1})).starts_with("? "));
        for key in [fits, quoted, long, wide] {
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
        ];
        leaf.prop_recursive(3, 24, 4, |inner| {
            prop_oneof![
                prop::collection::vec(inner.clone(), 0..4).prop_map(Value::Array),
                prop::collection::vec((lookalike(), inner), 0..4)
                    .prop_map(|entries| Value::Object(entries.into_iter().collect())),
            ]
        })
    }

    proptest! {
        #[test]
        fn any_value_reads_back_equal(value in json_value()) {
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
