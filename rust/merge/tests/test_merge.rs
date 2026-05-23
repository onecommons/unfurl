// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT

use std::path::PathBuf;
use std::sync::Arc;
use unfurl_merge::{
    diff, expand, intersect, load_file, merge, merge_with, patch, MergeError, MergeOptions, Node,
};

fn fixtures_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
}

#[test]
fn load_simple_yaml_preserves_structure_and_attaches_source() {
    let path = fixtures_root().join("simple").join("base.yaml");
    let node = load_file(&path).expect("load");

    // Root mapping carries the file path it was loaded from.
    match &node {
        Node::Mapping { source, .. } => {
            assert_eq!(&**source.file, &path);
            assert_eq!(source.base_dir(), path.parent().unwrap());
        }
        other => panic!("expected root mapping, got {other:?}"),
    }

    // Conversion to serde_json::Value reproduces the document.
    let json = node.to_json_value();
    assert_eq!(json["name"].as_str(), Some("example"));
    assert_eq!(json["version"].as_f64(), Some(1.0));
    assert_eq!(json["items"][0].as_str(), Some("alpha"));
    assert_eq!(json["items"][1].as_str(), Some("beta"));
    assert_eq!(json["nested"]["enabled"].as_bool(), Some(true));
    assert_eq!(json["nested"]["count"].as_i64(), Some(3));
}

#[test]
fn nested_mappings_share_file_arc_with_root() {
    let path = fixtures_root().join("simple").join("base.yaml");
    let node = load_file(&path).expect("load");

    let Node::Mapping {
        entries,
        source: root_src,
    } = &node
    else {
        panic!("expected root mapping");
    };
    let Node::Mapping {
        source: nested_src, ..
    } = &entries["nested"]
    else {
        panic!("expected nested mapping");
    };

    // Per-mapping line/col differs, but the file path is shared by Arc.
    assert!(Arc::ptr_eq(&root_src.file, &nested_src.file));
}

#[test]
fn mapping_source_records_line_numbers() {
    // base.yaml layout:
    //   1: name: example
    //   2: version: 1.0
    //   3: items:
    //   4:   - alpha
    //   5:   - beta
    //   6: nested:
    //   7:   enabled: true
    //   8:   count: 3
    let path = fixtures_root().join("simple").join("base.yaml");
    let node = load_file(&path).expect("load");

    let Node::Mapping {
        entries,
        source: root_src,
    } = &node
    else {
        panic!("expected root mapping");
    };
    let Node::Mapping {
        source: nested_src, ..
    } = &entries["nested"]
    else {
        panic!("expected nested mapping");
    };

    // Root mapping begins at line 1.
    assert_eq!(root_src.line, 1, "root line = {}", root_src.line);
    // Nested mapping's first key (`enabled`) is on line 7.
    assert_eq!(nested_src.line, 7, "nested line = {}", nested_src.line);
}

#[derive(serde::Deserialize, Debug, PartialEq)]
struct Doc {
    name: String,
    version: f64,
    items: Vec<String>,
    nested: Nested,
}

#[derive(serde::Deserialize, Debug, PartialEq)]
struct Nested {
    enabled: bool,
    count: i64,
}

#[test]
fn deserialize_into_typed_struct() {
    let path = fixtures_root().join("simple").join("base.yaml");
    let node = load_file(&path).expect("load");

    let doc: Doc = node.deserialize_into().expect("deserialize");
    assert_eq!(
        doc,
        Doc {
            name: "example".into(),
            version: 1.0,
            items: vec!["alpha".into(), "beta".into()],
            nested: Nested {
                enabled: true,
                count: 3,
            },
        }
    );
}

#[test]
fn deserialize_into_reports_type_mismatch() {
    let path = fixtures_root().join("simple").join("base.yaml");
    let node = load_file(&path).expect("load");

    // `count` is an integer in the fixture; asking for a String fails.
    #[derive(serde::Deserialize, Debug)]
    #[allow(dead_code)]
    struct WrongShape {
        nested: WrongNested,
    }
    #[derive(serde::Deserialize, Debug)]
    #[allow(dead_code)]
    struct WrongNested {
        count: String,
    }

    let err = node
        .deserialize_into::<WrongShape>()
        .expect_err("should fail");
    let msg = err.to_string();
    assert!(
        msg.contains("count") || msg.contains("string"),
        "unexpected error: {msg}"
    );
}

// ----------------------------------------------------------------------
// merge() primitive — fixture-driven round-trip tests
// ----------------------------------------------------------------------

fn assert_merge(name: &str) {
    let dir = fixtures_root().join(name);
    let base = load_file(&dir.join("base.yaml")).expect("base");
    let overlay = load_file(&dir.join("overlay.yaml")).expect("overlay");
    let expected = load_file(&dir.join("expected.yaml")).expect("expected");
    let result = merge(&base, &overlay).expect("merge");
    assert_eq!(result, expected, "{name}");
}

#[test]
fn merge_deep_mappings() {
    assert_merge("deep_merge");
}

#[test]
fn merge_whiteout_drops_key() {
    assert_merge("whiteout");
}

#[test]
fn merge_nullout_replaces_with_null() {
    assert_merge("nullout");
}

#[test]
fn merge_listmerge_positional() {
    // Ported from tests/test_runtime.py::ExpandDocTest::test_listmerge.
    assert_merge("listmerge");
}

#[test]
fn merge_listmerge_dicts_by_key() {
    // Ported from tests/test_runtime.py::ExpandDocTest::test_listmerge_dicts.
    assert_merge("listmerge_dicts");
}

#[test]
fn merge_error_strategy_refuses_merge() {
    let dir = fixtures_root().join("error_strategy");
    let base = load_file(&dir.join("base.yaml")).expect("base");
    let overlay = load_file(&dir.join("overlay.yaml")).expect("overlay");
    let err = merge(&base, &overlay).expect_err("should be rejected");
    assert!(
        matches!(err, MergeError::MergeRejected(ref msg) if msg.contains("protected")),
        "unexpected error: {err}"
    );
}

#[test]
fn merge_preserves_base_source_on_merged_mapping() {
    // The merged mapping should inherit the base's Source (matches
    // merge.py:67's make_map_with_base behavior).
    let dir = fixtures_root().join("deep_merge");
    let base = load_file(&dir.join("base.yaml")).expect("base");
    let overlay = load_file(&dir.join("overlay.yaml")).expect("overlay");
    let result = merge(&base, &overlay).expect("merge");

    let Node::Mapping { source, .. } = &result else {
        panic!("expected mapping");
    };
    assert!(
        source.file.ends_with("deep_merge/base.yaml"),
        "got {:?}",
        source.file
    );
}

// ----------------------------------------------------------------------
// replace strategy / replace_keys
// ----------------------------------------------------------------------

fn assert_merge_with(name: &str, opts: &MergeOptions) {
    let dir = fixtures_root().join(name);
    let base = load_file(&dir.join("base.yaml")).expect("base");
    let overlay = load_file(&dir.join("overlay.yaml")).expect("overlay");
    let expected = load_file(&dir.join("expected.yaml")).expect("expected");
    let result = merge_with(&base, &overlay, opts).expect("merge");
    assert_eq!(result, expected, "{name}");
}

#[test]
fn merge_explicit_replace_drops_base_keys() {
    // Overlay's `db` mapping carries `+%: replace`, so base's
    // port/user are dropped — only the overlay survives under `db`.
    assert_merge("explicit_replace");
}

#[test]
fn merge_replace_keys_flips_subtree_default() {
    // Without replace_keys, base.env.db.port would survive. With
    // replace_keys=["env"], the env subtree's default strategy
    // flips to "replace", and env.db (mapping-vs-mapping under the
    // replace default) is taken from overlay outright.
    let opts = MergeOptions {
        replace_keys: vec!["env".into()],
        ..Default::default()
    };
    assert_merge_with("replace_keys", &opts);
}

#[test]
fn merge_replace_keys_can_be_opt_out_with_explicit_merge_directive() {
    // Same replace_keys=["env"] as above, but the overlay's env.db
    // mapping declares `+%: merge`, restoring deep-merge for that
    // subtree. base.env.db.port survives.
    let opts = MergeOptions {
        replace_keys: vec!["env".into()],
        ..Default::default()
    };
    assert_merge_with("replace_keys_opt_back", &opts);
}

#[test]
fn merge_default_strategy_replace_at_top_level() {
    // default_strategy="replace" makes every key in overlay replace
    // the base outright, even without explicit +% directives.
    let dir = fixtures_root().join("deep_merge");
    let base = load_file(&dir.join("base.yaml")).expect("base");
    let overlay = load_file(&dir.join("overlay.yaml")).expect("overlay");
    let opts = MergeOptions {
        default_strategy: "replace".into(),
        ..Default::default()
    };
    let result = merge_with(&base, &overlay, &opts).expect("merge");

    let Node::Mapping { entries, .. } = &result else {
        panic!("expected mapping");
    };
    // a is a mapping in both — under replace default, overlay's a wins.
    let Node::Mapping { entries: a, .. } = &entries["a"] else {
        panic!("expected nested mapping");
    };
    assert_eq!(
        a.get("b1").and_then(|n| n.to_json_value().as_i64()),
        Some(99)
    );
    assert_eq!(
        a.get("b3").and_then(|n| n.to_json_value().as_i64()),
        Some(3)
    );
    // base.a.b2 is gone under replace.
    assert!(!a.contains_key("b2"));
    // base-only top-level keys still survive (they're handled in pass 2,
    // not by the strategy switch).
    assert!(entries.contains_key("keep_me"));
}

// ----------------------------------------------------------------------
// diff / patch / intersect
// ----------------------------------------------------------------------

#[test]
fn diff_basic_and_roundtrips_through_merge() {
    // Ported from tests/test_runtime.py::ExpandDocTest::test_diff.
    let dir = fixtures_root().join("diff_basic");
    let old = load_file(&dir.join("old.yaml")).expect("old");
    let new = load_file(&dir.join("new.yaml")).expect("new");
    let expected_diff = load_file(&dir.join("expected_diff.yaml")).expect("expected_diff");

    let d = diff(&old, &new);
    assert_eq!(d, expected_diff, "diff(old, new)");

    // merge(old, diff) round-trips to new (the diff's defining property).
    let merged = merge(&old, &d).expect("merge round-trip");
    assert_eq!(merged, new, "merge(old, diff) should equal new");

    // patch(old, new) over the same data — matches the trailing
    // `patch_dict(old, new); assertEqual(old, new)` assertion in
    // tests/test_runtime.py::ExpandDocTest::test_diff.
    let patched = patch(&old, &new, false);
    assert_eq!(patched, new, "patch(old, new, false) should equal new");
}

#[test]
fn patch_basic_no_preserve_drops_old_only_keys_and_rewrites_lists() {
    let dir = fixtures_root().join("patch_basic");
    let old = load_file(&dir.join("old.yaml")).expect("old");
    let new = load_file(&dir.join("new.yaml")).expect("new");
    let expected = load_file(&dir.join("expected_no_preserve.yaml")).expect("expected");

    let patched = patch(&old, &new, false);
    assert_eq!(patched, expected);
    // In Rust, patch(_, _, false) is structurally identical to `new`.
    assert_eq!(patched, new, "preserve=false collapses to new");
}

#[test]
fn patch_basic_preserve_keeps_old_keys_and_unions_lists() {
    let dir = fixtures_root().join("patch_basic");
    let old = load_file(&dir.join("old.yaml")).expect("old");
    let new = load_file(&dir.join("new.yaml")).expect("new");
    let expected = load_file(&dir.join("expected_preserve.yaml")).expect("expected");

    let patched = patch(&old, &new, true);
    assert_eq!(patched, expected);
}

#[test]
fn intersect_keeps_matching_keys_only() {
    let dir = fixtures_root().join("intersect_basic");
    let old = load_file(&dir.join("old.yaml")).expect("old");
    let new = load_file(&dir.join("new.yaml")).expect("new");
    let expected = load_file(&dir.join("expected.yaml")).expect("expected");

    let result = intersect(&old, &new);
    assert_eq!(result, expected);
}

#[test]
fn diff_treats_old_mapping_versus_new_null_as_nullout() {
    // Specifically asserts the merge.py:667 special case.
    let old_node = load_file(&fixtures_root().join("diff_basic").join("old.yaml")).expect("old");
    let new_node = load_file(&fixtures_root().join("diff_basic").join("new.yaml")).expect("new");
    let d = diff(&old_node, &new_node);
    let Node::Mapping { entries, .. } = &d else {
        panic!("diff should be a mapping");
    };
    let f_diff = entries.get("f").expect("f should be in diff");
    let Node::Mapping {
        entries: f_entries, ..
    } = f_diff
    else {
        panic!("f's diff value should be a mapping (the nullout directive)");
    };
    assert_eq!(
        f_entries
            .get("+%")
            .and_then(|n| if let Node::String(s) = n {
                Some(s.as_str())
            } else {
                None
            }),
        Some("nullout")
    );
}

// ----------------------------------------------------------------------
// expand_doc / expand_dict / expand_list  (Phase 2 includes)
// ----------------------------------------------------------------------

#[test]
fn expand_doc_resolves_pointer_includes_and_overlays() {
    // Direct port of tests/test_runtime.py::ExpandDocTest::test_expandDoc.
    // Covers: pointer includes (+/t2), nested includes inside values,
    // scalar template return (test3's d → "val"), list-resolves-into-list
    // (test2's +/t4), overlay vs template via the "overlay" string
    // value (test6), and the null-overlay-on-mapping convention
    // (test4's a: null preserves base's a mapping).
    let dir = fixtures_root().join("expand_doc");
    let doc = load_file(&dir.join("base.yaml")).expect("base");
    let expected = load_file(&dir.join("expected.yaml")).expect("expected");
    let (_includes, expanded) = expand(&doc).expect("expand");
    assert_eq!(expanded, expected);
}

#[test]
fn expand_doc_silently_drops_missing_optional_includes() {
    // Ported from test_missingInclude's doc3 case: +?/path with no
    // target should drop the directive without erroring.
    let dir = fixtures_root().join("expand_missing_optional");
    let doc = load_file(&dir.join("base.yaml")).expect("base");
    let expected = load_file(&dir.join("expected.yaml")).expect("expected");
    let (_includes, expanded) = expand(&doc).expect("expand");
    assert_eq!(expanded, expected);
}

#[test]
fn expand_doc_errors_on_missing_required_pointer_include() {
    // Ported from test_missingInclude's doc2 case: +/path without
    // the ? prefix to a non-existent target should error.
    let dir = fixtures_root().join("expand_missing_required");
    let doc = load_file(&dir.join("base.yaml")).expect("base");
    let err = expand(&doc).expect_err("should fail");
    assert!(matches!(err, MergeError::MergeRejected(_)), "got {err:?}");
}

#[test]
fn expand_doc_detects_recursive_pointer_include() {
    // Ported from test_recursion's first case: a deeply-nested key
    // includes one of its own ancestors via absolute pointer.
    let dir = fixtures_root().join("expand_recursion_pointer");
    let doc = load_file(&dir.join("base.yaml")).expect("base");
    let err = expand(&doc).expect_err("should fail");
    let msg = err.to_string();
    assert!(
        msg.contains("recursive") && msg.contains("test3"),
        "got: {msg}"
    );
}

#[test]
fn expand_doc_detects_recursive_relative_include() {
    // Ported from test_recursion's second case: a node references
    // its own parent via +../ syntax.
    let dir = fixtures_root().join("expand_recursion_relative");
    let doc = load_file(&dir.join("base.yaml")).expect("base");
    let err = expand(&doc).expect_err("should fail");
    let msg = err.to_string();
    assert!(
        msg.contains("recursive") && msg.contains("test4"),
        "got: {msg}"
    );
}
