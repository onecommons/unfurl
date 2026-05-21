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
fn nested_mappings_share_source_with_root() {
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

    // Same Arc, not just equal contents — propagation should be cheap.
    assert!(Arc::ptr_eq(root_src, nested_src));
}
