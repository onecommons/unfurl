// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT

use std::path::PathBuf;
use std::sync::Arc;
use unfurl_git_sync::merge::{load_file, Node};

fn fixtures_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("merge")
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
