// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! YAML output post-processing helpers.
//!
//! Lives outside `sync.rs` so the read-modify-write logic there isn't
//! crowded by the textual fix-ups we apply to emitter output.

/// Rewrite trailing `: null` mappings as `:` so the output matches the
/// original YAML idiom where empty mappings are written without an
/// explicit null. `serde-saphyr` (like `serde_yaml`) always emits
/// `: null`, but `key:` (no value) parses identically and is the
/// conventional form across the fixtures we round-trip.
///
/// Block-scalar aware: the YAML emitter renders multi-line strings as
/// literal (`|`/`|-`) or folded (`>`/`>-`) block scalars whose content
/// lines appear verbatim in the output. We track block-scalar regions
/// by indentation and pass their content lines through untouched, so a
/// string whose content happens to include a `foo: null` line is not
/// corrupted. Single-line strings ending in `: null` are quoted by the
/// emitter and don't end with a bare `null`, so they're already safe.
pub fn elide_explicit_nulls(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    // Some(min_content_indent) while inside a block scalar.
    let mut block_content_indent: Option<usize> = None;

    for line in input.split_inclusive('\n') {
        let stripped = line.strip_suffix('\n').unwrap_or(line);
        let leading_spaces = stripped.chars().take_while(|c| *c == ' ').count();
        let is_blank = stripped.trim().is_empty();

        if let Some(min_indent) = block_content_indent {
            if is_blank || leading_spaces >= min_indent {
                // Still inside the block scalar; pass content through.
                out.push_str(line);
                continue;
            }
            // Dedented past the content indent → block scalar ends here.
            block_content_indent = None;
            // Fall through to flow-style handling for this line.
        }

        // Detect a block-scalar header on this line. The content runs
        // until the next line indented at or below the header's indent.
        if let Some(parent_indent) = block_scalar_header_indent(stripped) {
            block_content_indent = Some(parent_indent + 1);
            out.push_str(line);
            continue;
        }

        if let Some(prefix) = stripped.strip_suffix(": null") {
            out.push_str(prefix);
            out.push(':');
            if line.ends_with('\n') {
                out.push('\n');
            }
        } else {
            out.push_str(line);
        }
    }
    out
}

/// Return `Some(parent_indent_in_spaces)` if `line` ends with a YAML
/// block-scalar header indicator (e.g. `key: |`, `key: |-`, `- >2-`).
/// Otherwise `None`.
fn block_scalar_header_indent(line: &str) -> Option<usize> {
    let trimmed = line.trim_end();
    if trimmed.is_empty() {
        return None;
    }
    let bytes = trimmed.as_bytes();
    let mut end = bytes.len();
    // Optional explicit indent indicator (digit).
    while end > 0 && bytes[end - 1].is_ascii_digit() {
        end -= 1;
    }
    // Optional chomping indicator.
    if end > 0 && (bytes[end - 1] == b'+' || bytes[end - 1] == b'-') {
        end -= 1;
    }
    // Style indicator must be `|` or `>`.
    if end == 0 || (bytes[end - 1] != b'|' && bytes[end - 1] != b'>') {
        return None;
    }
    end -= 1;
    // Whitespace must precede the indicator (so we don't false-match a
    // string scalar that happens to end in `>`).
    if end == 0 || !matches!(bytes[end - 1], b' ' | b'\t') {
        return None;
    }
    Some(trimmed.chars().take_while(|c| *c == ' ').count())
}

#[cfg(test)]
mod tests {
    use super::elide_explicit_nulls;

    #[test]
    fn strips_trailing_null_on_mapping_line() {
        let input = "type:\n  cloudmap.artifacts.oci.Image: null\n";
        let want = "type:\n  cloudmap.artifacts.oci.Image:\n";
        assert_eq!(elide_explicit_nulls(input), want);
    }

    #[test]
    fn leaves_inner_null_alone() {
        // "null" inside a string would be quoted by the YAML emitter,
        // so a bare ": null" mid-line is always semantically null in
        // our outputs. We only operate at line end, so any other "null"
        // text passes through.
        let input = "msg: 'value contains null word'\n";
        assert_eq!(elide_explicit_nulls(input), input);
    }

    #[test]
    fn handles_no_trailing_newline() {
        let input = "k: null";
        assert_eq!(elide_explicit_nulls(input), "k:");
    }

    #[test]
    fn does_not_touch_list_item_null() {
        // We deliberately don't strip "- null" — that's a different
        // shape and the user's request was about mapping values.
        let input = "- null\n- 1\n";
        assert_eq!(elide_explicit_nulls(input), input);
    }

    #[test]
    fn preserves_block_scalar_content_with_colon_null() {
        // The YAML emitter writes multi-line strings as `|-` block
        // scalars. A content line ending in `: null` must NOT be
        // stripped.
        let input = "\
description: |-
  alpha
  foo: null
  beta
real_null: null
";
        let want = "\
description: |-
  alpha
  foo: null
  beta
real_null:
";
        assert_eq!(elide_explicit_nulls(input), want);
    }

    #[test]
    fn preserves_folded_block_scalar() {
        let input = "\
note: >-
  line one
  inner: null
done: null
";
        let want = "\
note: >-
  line one
  inner: null
done:
";
        assert_eq!(elide_explicit_nulls(input), want);
    }

    #[test]
    fn block_scalar_with_chomp_and_indent_indicator() {
        // `|-2`, `|+`, `>2-` are all valid headers.
        let input = "\
data: |+2
    keep: null
after: null
";
        let want = "\
data: |+2
    keep: null
after:
";
        assert_eq!(elide_explicit_nulls(input), want);
    }

    #[test]
    fn nested_block_scalar() {
        let input = "\
outer:
  inner: |-
    a: null
    b: 1
  sibling: null
top: null
";
        let want = "\
outer:
  inner: |-
    a: null
    b: 1
  sibling:
top:
";
        assert_eq!(elide_explicit_nulls(input), want);
    }

    #[test]
    fn does_not_match_string_ending_in_pipe() {
        // A flow-style scalar that happens to end in `|` is not a
        // block-scalar header (no preceding whitespace before `|`).
        let input = "k: 'foo|'\nthing: null\n";
        let want = "k: 'foo|'\nthing:\n";
        assert_eq!(elide_explicit_nulls(input), want);
    }
}
