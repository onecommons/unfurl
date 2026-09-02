// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT

use std::path::PathBuf;
use std::sync::Arc;
use unfurl_merge::{
    diff, expand, expand_with, intersect, load_file, merge, merge_list_append_unique,
    merge_list_append_unique_with, merge_with, patch, FileResolver, MergeError, MergeOptions, Node,
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

#[test]
fn expand_with_file_resolver_loads_sibling_yaml() {
    // End-to-end: parent.yaml carries `+include: shared.yaml` inside
    // app.env; FileResolver loads shared.yaml relative to parent's
    // directory; the result merges shared into app.env with
    // parent's own keys winning on conflict (LOG_LEVEL: debug vs
    // shared's LOG_LEVEL: info).
    let dir = fixtures_root().join("expand_file_include");
    let doc = load_file(&dir.join("parent.yaml")).expect("parent");
    let expected = load_file(&dir.join("expected.yaml")).expect("expected");
    let (_includes, expanded) = expand_with(&doc, &FileResolver).expect("expand");
    assert_eq!(expanded, expected);
}

#[test]
fn expand_preserves_included_files_source_on_merged_mapping() {
    // After expanding parent.yaml (which has `+include: shared.yaml`
    // inside app.env), the merged app.env mapping should carry
    // shared.yaml's Source — not parent.yaml's — so that any
    // further `+include` directives nested *inside* the merged
    // content would resolve relative to shared.yaml's directory.
    //
    // This is the load-bearing property behind the per-mapping
    // Source tracking. PartialEq on Node ignores Source, so the
    // structural-equality assertion in
    // `expand_with_file_resolver_loads_sibling_yaml` doesn't catch
    // regressions here — that's why this test exists separately.
    let dir = fixtures_root().join("expand_file_include");
    let doc = load_file(&dir.join("parent.yaml")).expect("parent");
    let (_includes, expanded) = expand_with(&doc, &FileResolver).expect("expand");

    let Node::Mapping { entries: root, .. } = &expanded else {
        panic!("expected root mapping");
    };
    let Node::Mapping { entries: app, .. } = &root["app"] else {
        panic!("expected app mapping");
    };
    let Node::Mapping {
        source: env_src, ..
    } = &app["env"]
    else {
        panic!("expected env mapping");
    };

    assert!(
        env_src.file.ends_with("shared.yaml"),
        "expected app.env source to point at shared.yaml, got {:?}",
        env_src.file
    );

    // The parent's own un-merged mapping (`app` itself) should
    // still point at parent.yaml — we're not clobbering source
    // everywhere, just where merge actually happened.
    let Node::Mapping {
        source: app_src, ..
    } = &root["app"]
    else {
        unreachable!()
    };
    assert!(
        app_src.file.ends_with("parent.yaml"),
        "expected app's source to remain parent.yaml, got {:?}",
        app_src.file
    );
}

#[test]
fn expand_with_file_resolver_map_form_loads_file() {
    // Map-form +include: {file: shared.yaml}. Same behavior as
    // string form (which is tested elsewhere) — the map form is
    // just an alternative spelling that also allows `repository`
    // and `merge` keys.
    let dir = fixtures_root().join("expand_include_map");
    let doc = load_file(&dir.join("parent.yaml")).expect("parent");
    let expected = load_file(&dir.join("expected.yaml")).expect("expected");
    let (_includes, expanded) = expand_with(&doc, &FileResolver).expect("expand");
    assert_eq!(expanded, expected);
}

#[test]
fn expand_with_file_resolver_map_form_with_merge_raw_preserves_inner_directives() {
    // +include: {file: child.yaml, merge: raw}. The inner `merge:`
    // value is what drives the raw/overlay decision, not the outer
    // value. So the child's +/parent_data directive should be
    // preserved verbatim in the output.
    let dir = fixtures_root().join("expand_include_map_raw");
    let doc = load_file(&dir.join("parent.yaml")).expect("parent");
    let expected = load_file(&dir.join("expected.yaml")).expect("expected");
    let (_includes, expanded) = expand_with(&doc, &FileResolver).expect("expand");
    assert_eq!(expanded, expected);
}

#[test]
fn expand_with_file_resolver_map_form_without_file_key_errors() {
    let dir = fixtures_root().join("expand_include_map_invalid");
    let doc = load_file(&dir.join("parent.yaml")).expect("parent");
    let err = expand_with(&doc, &FileResolver).expect_err("should fail");
    let msg = err.to_string();
    assert!(msg.contains("file"), "got: {msg}");
}

#[test]
fn expand_resolves_anchor_declared_and_referenced_inside_same_included_file() {
    // child.yaml declares `+&: shared` in `defaults` and references
    // `+*shared` in `db`. parent.yaml +include's child.yaml. The
    // expected behavior: the anchor cache is threaded through the
    // recursive expand of the included file, so the in-file
    // declaration and reference resolve against the same cache.
    let dir = fixtures_root().join("expand_anchor_in_included_file");
    let doc = load_file(&dir.join("parent.yaml")).expect("parent");
    let expected = load_file(&dir.join("expected.yaml")).expect("expected");
    let (_includes, expanded) = expand_with(&doc, &FileResolver).expect("expand");
    assert_eq!(expanded, expected);
}

#[test]
fn expand_with_file_resolver_errors_on_missing_required_include() {
    let dir = fixtures_root().join("expand_file_include");
    let doc = load_file(&dir.join("missing_required.yaml")).expect("doc");
    let err = expand_with(&doc, &FileResolver).expect_err("should fail");
    assert!(matches!(err, MergeError::MergeRejected(_)), "got {err:?}");
}

#[test]
fn expand_doc_resolves_inline_anchor_declarations() {
    // `+&: name` declares the surrounding mapping as an anchor;
    // `+*name` elsewhere pulls in that mapping's expanded content.
    // The anchor name is removed from the registering mapping.
    let dir = fixtures_root().join("expand_anchor");
    let doc = load_file(&dir.join("base.yaml")).expect("base");
    let expected = load_file(&dir.join("expected.yaml")).expect("expected");
    let (_includes, expanded) = expand(&doc).expect("expand");
    assert_eq!(expanded, expected);
}

#[test]
fn expand_doc_resolves_forward_anchor_references_via_retry() {
    // The `+*` reference appears before the `+&` declaration in
    // document order. First pass marks the reference as Missing;
    // anchors persists across retry passes (intentionally), so the
    // second pass finds the now-registered anchor and resolves.
    let dir = fixtures_root().join("expand_anchor_forward");
    let doc = load_file(&dir.join("base.yaml")).expect("base");
    let expected = load_file(&dir.join("expected.yaml")).expect("expected");
    let (_includes, expanded) = expand(&doc).expect("expand");
    assert_eq!(expanded, expected);
}

#[test]
fn expand_raw_value_skips_recursive_expansion() {
    // When the include directive's value contains "raw", the
    // resolved template is used as-is — its own +directives stay
    // verbatim in the result. Matches merge.py:340's `_is_raw`
    // gate. Contrast service_normal (which does recursively
    // expand the included template's +/shared_data directive)
    // with service_raw (which preserves it).
    let dir = fixtures_root().join("expand_raw");
    let doc = load_file(&dir.join("base.yaml")).expect("base");
    let expected = load_file(&dir.join("expected.yaml")).expect("expected");
    let (_includes, expanded) = expand(&doc).expect("expand");
    assert_eq!(expanded, expected);
}

#[test]
fn expand_doc_errors_on_missing_required_anchor_reference() {
    let dir = fixtures_root().join("expand_anchor_missing");
    let doc = load_file(&dir.join("base.yaml")).expect("base");
    let err = expand(&doc).expect_err("should fail");
    let msg = err.to_string();
    assert!(
        msg.contains("never_declared") || msg.contains("missing"),
        "got: {msg}"
    );
}

#[test]
fn expand_with_file_resolver_drops_missing_optional_include() {
    // `+?include` to a non-existent file: the directive is dropped
    // along with its enclosing sub-mapping (matches merge.py's
    // delete_path behavior when path is non-empty — the parent
    // mapping was created to host the include, and once that fails
    // it's treated as having no useful content). Unrelated sibling
    // keys at a different path level survive.
    let dir = fixtures_root().join("expand_file_include");
    let doc = load_file(&dir.join("missing_optional.yaml")).expect("doc");
    let (_includes, expanded) = expand_with(&doc, &FileResolver).expect("expand");
    let json = expanded.to_json_value();
    // app.env is removed (it only held the failed include); app
    // itself stays (now empty); the unrelated `keep: 1` survives.
    assert!(json["app"]["env"].is_null(), "expected app.env gone");
    assert_eq!(json["keep"].as_i64(), Some(1));
}

// ----------------------------------------------------------------------
// Direct primitive tests (coverage for the public list-merge wrappers
// and the anchor-with-sequence-pointer path that the fixture-driven
// tests never reach indirectly).
// ----------------------------------------------------------------------

#[test]
fn merge_list_append_unique_called_directly_on_node_slices() {
    // The public `merge_list_append_unique` wrapper is covered
    // indirectly when merge() encounters sequence-vs-sequence keys,
    // but no test calls the wrapper directly. Pulls the `spec`
    // lists out of the listmerge_dicts fixture and merges them
    // standalone — same data, different entry point.
    let dir = fixtures_root().join("listmerge_dicts");
    let base = load_file(&dir.join("base.yaml")).expect("base");
    let overlay = load_file(&dir.join("overlay.yaml")).expect("overlay");
    let expected = load_file(&dir.join("expected.yaml")).expect("expected");

    let base_spec = extract_spec_list(&base);
    let overlay_spec = extract_spec_list(&overlay);
    let expected_spec = extract_spec_list(&expected);

    let result = merge_list_append_unique(base_spec, overlay_spec).expect("merge");
    assert_eq!(result, expected_spec);
}

#[test]
fn merge_list_append_unique_with_default_options_matches_no_options_variant() {
    // The `_with` wrapper threads MergeOptions through to nested
    // mapping merges inside list items. With default options the
    // behavior should be identical to the no-options variant.
    let dir = fixtures_root().join("listmerge_dicts");
    let base = load_file(&dir.join("base.yaml")).expect("base");
    let overlay = load_file(&dir.join("overlay.yaml")).expect("overlay");

    let base_spec = extract_spec_list(&base);
    let overlay_spec = extract_spec_list(&overlay);

    let plain = merge_list_append_unique(base_spec, overlay_spec).expect("plain");
    let with_opts =
        merge_list_append_unique_with(base_spec, overlay_spec, &MergeOptions::default())
            .expect("with");
    assert_eq!(plain, with_opts);
}

fn extract_spec_list(node: &Node) -> &[Node] {
    let Node::Mapping { entries, .. } = node else {
        panic!("expected root mapping");
    };
    let Node::Sequence(items) = &entries["spec"] else {
        panic!("expected spec to be a sequence");
    };
    items.as_slice()
}

#[test]
fn expand_anchor_reference_with_sequence_index_pointer() {
    // `+*my_template/items/1` exercises resolve_anchor's
    // sequence-arm: after looking up `my_template` (a mapping),
    // the pointer descends into `items` (a sequence) and indexes
    // position 1. None of the other anchor tests use a sub-pointer
    // into a list, so this is the only test that hits expand.rs:450.
    let dir = fixtures_root().join("expand_anchor_sequence_pointer");
    let doc = load_file(&dir.join("base.yaml")).expect("base");
    let expected = load_file(&dir.join("expected.yaml")).expect("expected");
    let (_includes, expanded) = expand(&doc).expect("expand");
    assert_eq!(expanded, expected);
}

// ---------------------------------------------------------------------------
// file-in, yaml-out entry points
// ---------------------------------------------------------------------------

fn fixture(rel: &str) -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(rel)
}

/// Every live fence merged, in document order, and nothing from the
/// ones that opted out or are not YAML.
#[test]
fn extract_file_emits_the_embedded_yaml() {
    let yaml = unfurl_merge::extract_file(&fixture("literate/document.md")).expect("extract");
    let value: serde_json::Value = serde_saphyr::from_str(&yaml).expect("valid yaml");
    assert_eq!(value["components"]["org"]["type"], "RealWorldEntity");
    assert_eq!(
        value["components"]["org"]["name"], "onecommons",
        "the second fence merged in"
    );
    assert!(value["components"].get("never").is_none(), "{yaml}");
    assert!(value["components"].get("from-json").is_none(), "{yaml}");
    // The prose is gone -- this is the document's data, not its text.
    assert!(!yaml.contains("An organization"), "{yaml}");
}

/// A markdown file that is not literate is a distinct answer from one
/// holding nothing, so it is an error rather than an empty document.
#[test]
fn extract_file_rejects_a_plain_markdown_file() {
    let err = unfurl_merge::extract_file(&fixture("literate/../expand_file_include/parent.yaml"))
        .expect_err("not literate");
    assert!(err.to_string().contains("literate-yaml"), "{err}");
}

/// `+include` resolved against the filesystem, emitted as YAML.
#[test]
fn expand_file_resolves_includes_and_emits_yaml() {
    let yaml =
        unfurl_merge::expand_file(&fixture("expand_file_include/parent.yaml")).expect("expand");
    let got: serde_json::Value = serde_saphyr::from_str(&yaml).expect("valid yaml");
    let want: serde_json::Value = serde_saphyr::from_str(
        &std::fs::read_to_string(fixture("expand_file_include/expected.yaml")).expect("expected"),
    )
    .expect("valid yaml");
    assert_eq!(got, want, "{yaml}");
    // The directive itself is gone -- this entry point is one-way.
    assert!(!yaml.contains("+include"), "{yaml}");
}

/// A required include that resolves to nothing is an error, not a
/// silently thinner document.
#[test]
fn expand_file_reports_a_missing_required_include() {
    assert!(
        unfurl_merge::expand_file(&fixture("expand_file_include/missing_required.yaml")).is_err()
    );
}

/// The yaml embedded in a markdown document, with its includes resolved
/// afterwards -- and resolved relative to *that document's* directory,
/// which is the whole reason `expand_text` takes a path rather than
/// just text.
#[test]
fn extract_then_expand_resolves_includes_beside_the_markdown() {
    let path = fixture("literate/with_include.md");
    let yaml = unfurl_merge::extract_file(&path).expect("extract");
    assert!(
        yaml.contains("+include"),
        "extract leaves the directive alone: {yaml}"
    );

    let expanded = unfurl_merge::expand_text(&yaml, &path).expect("expand");
    let value: serde_json::Value = serde_saphyr::from_str(&expanded).expect("valid yaml");
    assert_eq!(value["app"]["env"]["DATABASE_URL"], "postgres://localhost");
    assert_eq!(
        value["app"]["env"]["LOG_LEVEL"], "debug",
        "the document's own value wins over the included one"
    );
    assert!(!expanded.contains("+include"), "{expanded}");
}

/// The byte forms answer the same as the file forms for the same
/// document -- the file form is just the one that does the reading.
#[test]
fn the_byte_forms_agree_with_the_file_forms() {
    for rel in ["literate/document.md", "literate/with_include.md"] {
        let path = fixture(rel);
        let bytes = std::fs::read(&path).expect("read");
        assert_eq!(
            unfurl_merge::extract_bytes(&bytes, &path).expect("bytes"),
            unfurl_merge::extract_file(&path).expect("file"),
            "{rel}"
        );
    }
    let path = fixture("expand_file_include/parent.yaml");
    let bytes = std::fs::read(&path).expect("read");
    assert_eq!(
        unfurl_merge::expand_bytes(&bytes, &path).expect("bytes"),
        unfurl_merge::expand_file(&path).expect("file"),
    );
}

/// `path` is not read by the byte forms -- it names the document, and
/// for expand it is what includes resolve against. So bytes that never
/// were a file still resolve an include, as long as the path says where
/// they came from.
#[test]
fn the_byte_forms_never_read_the_path_they_are_given() {
    let never = fixture("literate/does-not-exist.md");
    assert!(!never.exists());

    let src = b"---\nliterate-yaml: x\n---\n\n```yaml\nenv:\n  \"+include\": shared.yaml\n```\n";
    let yaml = unfurl_merge::extract_bytes(src, &never).expect("extract");
    let expanded = unfurl_merge::expand_bytes(yaml.as_bytes(), &never).expect("expand");
    let value: serde_json::Value = serde_saphyr::from_str(&expanded).expect("valid yaml");
    assert_eq!(
        value["env"]["DATABASE_URL"], "postgres://localhost",
        "resolved beside the path, not beside the process: {expanded}"
    );
}

/// Bytes that are not text fail naming the document, rather than as a
/// bare io or utf-8 error with nothing to locate it by.
#[test]
fn the_byte_forms_name_the_document_in_a_utf8_failure() {
    let path = fixture("literate/whatever.md");
    for message in [
        unfurl_merge::extract_bytes(b"---\nliterate-yaml: x\n---\n\xff", &path)
            .expect_err("not utf-8")
            .to_string(),
        unfurl_merge::expand_bytes(b"a: \xff", &path)
            .expect_err("not utf-8")
            .to_string(),
    ] {
        assert!(message.contains("whatever.md"), "{message}");
        assert!(message.contains("utf-8"), "{message}");
    }
}

/// Reads only as far as the front matter, and distinguishes "read it,
/// not literate" from "could not read it" -- a caller skipping files
/// must not skip one it failed to open.
#[test]
fn find_literate_directive_reads_only_the_head() {
    assert_eq!(
        unfurl_merge::find_literate_directive(&fixture("literate/document.md")).expect("read"),
        Some("cloudmap@unfurl/v1.0.0".to_string())
    );
    // A document that opens with something else is settled by line one.
    assert_eq!(
        unfurl_merge::find_literate_directive(&fixture("expand_file_include/parent.yaml"))
            .expect("read"),
        None
    );
    assert!(unfurl_merge::find_literate_directive(&fixture("literate/nope.md")).is_err());
}

/// Front matter that never closes is not front matter. Without the
/// limit this would read to the end of whatever it was pointed at.
#[test]
fn find_literate_directive_gives_up_on_a_block_that_never_closes() {
    // Asserted rather than assumed: the fixture below is sized from
    // this, so a limit that stopped bounding anything would hang the
    // test rather than fail it.
    let limit = unfurl_merge::markdown::FRONT_MATTER_LIMIT;
    assert!(limit <= 1 << 20, "the limit has to bound the read: {limit}");

    let path = std::env::temp_dir().join("unfurl_merge_long_front_matter.md");
    let mut src = String::from("---\nliterate-yaml: x\n");
    while src.len() <= limit {
        src.push_str("filler: 1\n");
    }
    // The block *does* close -- past the limit, which is the only
    // reason this is not a literate document.
    src.push_str("---\n");
    std::fs::write(&path, &src).expect("write");
    assert_eq!(
        unfurl_merge::find_literate_directive(&path).expect("read"),
        None
    );
    std::fs::remove_file(&path).ok();
}
