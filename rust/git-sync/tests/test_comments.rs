// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! What a rewrite keeps, and what it cannot.
//!
//! A record round-trips through a `serde_json::Value`, which holds no
//! comments, so anything re-emitted loses them. These pin where that
//! boundary actually falls -- it moves as the splice gets finer, and a
//! boundary nobody states is one nobody notices moving.

mod common;

use common::{crud_test, open_at};
use tempfile::TempDir;
use unfurl_git_sync::{ScanOptions, SyncedRepo};

/// A write keeps the comments it did not touch.
async fn a_write_keeps_comments_elsewhere(sync: &SyncedRepo, tmp: &TempDir) {
    let path = tmp.path().join("cloudmap.yaml");
    let original = std::fs::read_to_string(&path).expect("read");
    // Comments in the three places people put them: above the document,
    // above a section, and inside one.
    let commented = format!(
        "# what this cloudmap is for\n{}",
        original
            .replacen("repositories:", "# the repos we track\nrepositories:", 1)
            .replacen("artifacts:", "# and the artifacts\nartifacts:", 1)
    );
    std::fs::write(&path, &commented).expect("write");

    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://example.com/added.git",
        serde_json::json!({"name": "added"}),
        None,
        false,
    )
    .await
    .expect("write");
    sync.save_changes().await.expect("save");

    let after = std::fs::read_to_string(&path).expect("read");
    assert!(
        after.starts_with("# what this cloudmap is for\n"),
        "header comment lost:\n{after}"
    );
    assert!(after.contains("# the repos we track\n"), "{after}");
    assert!(
        after.contains("# and the artifacts\n"),
        "a comment about an untouched section lost:\n{after}"
    );
    // ...and the write itself landed, in a document that still parses.
    let parsed: serde_json::Value = serde_saphyr::from_str(&after).expect("valid yaml");
    assert_eq!(
        parsed["repositories"]["git://example.com/added.git"]["name"],
        "added"
    );
    assert_eq!(parsed["kind"], "CloudMap");
}

/// Rewriting a section drops the comments *inside* it -- the records it
/// annotates are re-sorted, so there is nowhere to put them back. Pinned
/// so the boundary of what survives is stated rather than assumed.
async fn comments_inside_a_rewritten_section_are_lost(sync: &SyncedRepo, tmp: &TempDir) {
    let path = tmp.path().join("cloudmap.yaml");
    let original = std::fs::read_to_string(&path).expect("read");
    let commented = original.replacen(
        "  git://unfurl.cloud/onecommons/std.git:",
        "  # a note about std\n  git://unfurl.cloud/onecommons/std.git:",
        1,
    );
    assert_ne!(commented, original, "fixture should contain that key");
    std::fs::write(&path, &commented).expect("write");

    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://example.com/added.git",
        serde_json::json!({"name": "added"}),
        None,
        false,
    )
    .await
    .expect("write");
    sync.save_changes().await.expect("save");

    let after = std::fs::read_to_string(&path).expect("read");
    assert!(!after.contains("a note about std"), "{after}");
    // ...and this is why: the section is still sorted, so the added
    // record sits *before* the one the comment annotated. Splicing the
    // records where they were would have left it unsorted.
    let added = after.find("git://example.com/added.git").expect("added");
    let std = after
        .find("git://unfurl.cloud/onecommons/std.git")
        .expect("std");
    assert!(added < std, "the section is still key-sorted:\n{after}");
}

/// Editing a record the file already has moves nothing, so the section
/// is spliced where it sits and the comments between its records stay.
///
/// The complement of `comments_inside_a_rewritten_section_are_lost`:
/// that one *adds* a record, which re-sorts the section and leaves the
/// comments nowhere to go.
async fn comments_inside_an_edited_section_survive(sync: &SyncedRepo, tmp: &TempDir) {
    const STD: &str = "git://unfurl.cloud/onecommons/std.git";
    let path = tmp.path().join("cloudmap.yaml");
    let original = std::fs::read_to_string(&path).expect("read");
    let commented = original.replacen(
        "  git://unfurl.cloud/onecommons/std.git:",
        "  # a note about std\n  git://unfurl.cloud/onecommons/std.git:",
        1,
    );
    assert_ne!(commented, original, "fixture should contain that key");
    std::fs::write(&path, &commented).expect("write");

    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    let mut record = sync
        .get_record("cloudmap.yaml", "/repositories", STD)
        .await
        .expect("get")
        .expect("present")
        .json;
    record["name"] = serde_json::json!("renamed");
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        STD,
        record,
        None,
        false,
    )
    .await
    .expect("write");
    sync.save_changes().await.expect("save");

    let after = std::fs::read_to_string(&path).expect("read");
    assert!(after.contains("name: renamed"), "the edit landed:\n{after}");
    assert!(
        after.contains("# a note about std"),
        "and the comment beside it stayed:\n{after}"
    );
}

crud_test!(comments_inside_an_edited_section_survive);
crud_test!(a_write_keeps_comments_elsewhere);
crud_test!(comments_inside_a_rewritten_section_are_lost);

/// Comments do not survive a rewrite -- the document round-trips through
/// a `serde_json::Value`, which cannot hold them. Pinned so the loss is
/// a known property rather than a surprise.
#[tokio::test]
async fn a_rewrite_drops_comments() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let jsonc = br#"{
  // this comment cannot survive
  "apiVersion": "unfurl/v1alpha1",
  "kind": "CloudMap",
  "repositories": {"git://example.com/x.git": {"name": "x"}}
}"#;
    unfurl_git_sync::git::init_with_files(
        tmp.path(),
        &[("c.jsonc".to_string(), jsonc.to_vec())],
        "initial",
    )
    .expect("init repo");
    let db = format!("sqlite://{}?mode=rwc", tmp.path().join("sync.db").display());
    let sync = open_at(tmp.path(), &db).await;
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("sync");
    sync.upsert_record(
        Some("c.jsonc"),
        "/repositories",
        "git://example.com/x.git",
        serde_json::json!({"name": "changed"}),
        None,
        false,
    )
    .await
    .expect("write");
    sync.save_changes().await.expect("save");

    let text = std::fs::read_to_string(tmp.path().join("c.jsonc")).expect("read");
    assert!(
        !text.contains("cannot survive"),
        "comments are dropped, same as for yaml: {text}"
    );
    assert!(text.contains("changed"), "{text}");
}
