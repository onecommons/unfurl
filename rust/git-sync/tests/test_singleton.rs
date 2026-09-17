// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Sections that hold fields rather than a map of records, and so are
//! one record each -- `SectionKind::Singleton`. A cloudmap's
//! `metadata:` is the one built-in case.
//!
//! Same shape as `test_crud.rs`: each scenario is one `async fn` run
//! against both backends by `crud_test!`.

mod common;

use common::crud_test;
use tempfile::TempDir;
use unfurl_git_sync::{RecordConflictKind, ScanOptions, SyncedRepo};

const FILE: &str = "cloudmap.yaml";
const TITLE: &str = "Example cloud map";

/// Give the fixture cloudmap a `metadata:` section. Prepended rather
/// than appended so it lands at the top level whatever the last section
/// in the file is.
fn add_metadata(tmp: &TempDir, title: &str) {
    let path = tmp.path().join(FILE);
    let body = format!(
        "metadata:\n  title: {title}\n  vendor: onecommons\n{}",
        std::fs::read_to_string(&path).expect("read")
    );
    std::fs::write(&path, body).expect("write");
}

/// The file as a value, for asserting on structure rather than text.
fn on_disk(tmp: &TempDir) -> serde_json::Value {
    serde_saphyr::from_str(&std::fs::read_to_string(tmp.path().join(FILE)).expect("read"))
        .expect("valid yaml")
}

/// The whole section is one record; its fields are not records of their
/// own.
async fn a_singleton_section_is_one_record(sync: &SyncedRepo, tmp: &TempDir) {
    add_metadata(tmp, TITLE);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");

    let record = sync
        .get_record(FILE, "/metadata", "metadata")
        .await
        .expect("get")
        .expect("the section is keyed by its own name");
    assert_eq!(record.json["title"], TITLE);
    assert_eq!(record.json["vendor"], "onecommons");

    assert!(
        sync.get_record(FILE, "/metadata", "title")
            .await
            .expect("get")
            .is_none(),
        "a singleton's fields must not become records of their own"
    );
}

/// A write puts the record back as the section itself, not nested under
/// a key inside it.
async fn writing_a_singleton_replaces_the_section(sync: &SyncedRepo, tmp: &TempDir) {
    add_metadata(tmp, TITLE);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");

    sync.update_record(
        Some(FILE),
        "/metadata",
        "metadata",
        serde_json::json!({"title": "Renamed", "vendor": "onecommons"}),
        None,
        false,
    )
    .await
    .expect("update");
    sync.save_changes().await.expect("save");

    let doc = on_disk(tmp);
    assert_eq!(doc["metadata"]["title"], "Renamed", "{doc}");
    assert!(
        doc["metadata"].get("metadata").is_none(),
        "the record must be the section, not a child of it: {doc}"
    );
}

/// Deleting it takes the section with it.
async fn deleting_a_singleton_removes_the_section(sync: &SyncedRepo, tmp: &TempDir) {
    add_metadata(tmp, TITLE);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");

    sync.delete_record(Some(FILE), "/metadata", "metadata", None, false)
        .await
        .expect("delete");
    sync.save_changes().await.expect("save");

    let doc = on_disk(tmp);
    assert!(doc.get("metadata").is_none(), "{doc}");
}

/// A section the file didn't have is written in the schema's field
/// order, like any other new record.
async fn a_new_singleton_gets_the_schema_field_order(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");

    // Deliberately reversed against the schema, which declares `title`
    // before `description` before `vendor`.
    sync.create_record(
        Some(FILE),
        "/metadata",
        "metadata",
        serde_json::json!({"vendor": "onecommons", "description": "d", "title": "t"}),
        None,
        false,
    )
    .await
    .expect("create");
    sync.save_changes().await.expect("save");

    let text = std::fs::read_to_string(tmp.path().join(FILE)).expect("read");
    let at = |needle: &str| {
        text.find(needle)
            .unwrap_or_else(|| panic!("{needle}: {text}"))
    };
    assert!(
        at("title: t") < at("description: d") && at("description: d") < at("vendor: onecommons"),
        "fields should follow the schema order: {text}"
    );
}

/// Both sides changed the singleton: the pending edit survives and the
/// divergence is reported.
///
/// The guard for the invariant that matters most here -- reading a
/// record, writing one, and looking one up in the base commit must all
/// agree on where it lives. A reader that looked in a different place
/// from the writer would report every pending edit as a divergence from
/// a file that in fact holds it, or miss a real one.
async fn a_diverged_singleton_is_reported(sync: &SyncedRepo, tmp: &TempDir) {
    add_metadata(tmp, TITLE);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    // Commit, so the pending edit below has a base to diverge from --
    // without one the divergence classifies as AddAdd and never
    // consults the base document.
    sync.commit_repository("add metadata")
        .await
        .expect("commit");

    sync.update_record(
        Some(FILE),
        "/metadata",
        "metadata",
        serde_json::json!({"title": "ours", "vendor": "onecommons"}),
        None,
        false,
    )
    .await
    .expect("update");

    let path = tmp.path().join(FILE);
    let edited = std::fs::read_to_string(&path)
        .expect("read")
        .replace(&format!("title: {TITLE}"), "title: theirs");
    std::fs::write(&path, edited).expect("write");

    let stats = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");

    assert_eq!(stats.conflicts.len(), 1, "{stats:?}");
    let c = &stats.conflicts[0];
    assert_eq!(c.kind, RecordConflictKind::ModifyModify);
    assert_eq!((c.path.as_str(), c.key.as_str()), ("/metadata", "metadata"));
    assert_eq!(
        c.theirs.as_ref().map(|t| &t["title"]),
        Some(&serde_json::json!("theirs")),
        "the file's side is the whole section: {c:?}"
    );

    let record = sync
        .get_record(FILE, "/metadata", "metadata")
        .await
        .expect("get")
        .expect("present");
    assert_eq!(
        record.json["title"], "ours",
        "the pending edit must survive"
    );
}

/// A pending edit over a file that still holds what it was based on is
/// not a divergence -- only the edit is unsaved.
///
/// The guard that actually pins the base-commit lookup. The divergence
/// test above cannot: with the base missing, `classify_conflict` still
/// answers `ModifyModify`, so a lookup that searched the wrong place
/// would pass it. Here a missing base turns "nothing to report" into a
/// spurious conflict.
async fn an_unchanged_singleton_under_a_pending_edit_is_not_a_conflict(
    sync: &SyncedRepo,
    tmp: &TempDir,
) {
    add_metadata(tmp, TITLE);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    sync.commit_repository("add metadata")
        .await
        .expect("commit");

    sync.update_record(
        Some(FILE),
        "/metadata",
        "metadata",
        serde_json::json!({"title": "ours", "vendor": "onecommons"}),
        None,
        false,
    )
    .await
    .expect("update");

    // Touch a *different* record, so the file is re-scanned rather than
    // skipped as unchanged -- the metadata section itself still holds
    // exactly what the pending edit was based on.
    let path = tmp.path().join(FILE);
    let edited = std::fs::read_to_string(&path)
        .expect("read")
        .replace("name: dashboard", "name: renamed");
    std::fs::write(&path, edited).expect("write");

    let stats = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert!(
        stats.conflicts.is_empty(),
        "the file still holds the edit's base: {stats:?}"
    );

    let record = sync
        .get_record(FILE, "/metadata", "metadata")
        .await
        .expect("get")
        .expect("present");
    assert_eq!(record.json["title"], "ours");
}

crud_test!(a_singleton_section_is_one_record);
crud_test!(an_unchanged_singleton_under_a_pending_edit_is_not_a_conflict);
crud_test!(writing_a_singleton_replaces_the_section);
crud_test!(deleting_a_singleton_removes_the_section);
crud_test!(a_new_singleton_gets_the_schema_field_order);
crud_test!(a_diverged_singleton_is_reported);
