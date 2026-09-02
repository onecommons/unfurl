// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Divergence between the database and the working tree: finding it,
//! materializing both sides, and settling it.

mod common;

use common::{
    crud_test, dashboard_on_disk, git, head_commit, head_trailer, only_conflict, rename_name,
    stand_up_conflict, DASHBOARD,
};
use tempfile::TempDir;
use unfurl_git_sync::{
    CloudMapFormat, ConflictState, DataFormat, Error, RecordConflictKind, RecordQuery, Resolution,
    ScanOptions, SyncedRepo,
};

// ---------------------------------------------------------------------------
// source_oid conflict detection
// ---------------------------------------------------------------------------

/// A write over a hand-edited file lands the merge on disk without
/// touching the database: the file's rows keep their pre-edit values
/// and `source_oid` keeps naming the old bytes, so the next scan sees
/// the mismatch and takes the merged file in.
async fn a_write_merges_over_a_hand_edit(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "staged",
        serde_json::json!({"name": "staged"}),
        None,
        false,
    )
    .await
    .expect("write");

    // Someone edits an existing record directly, as a person or another
    // tool would.
    let path = tmp.path().join("cloudmap.yaml");
    let before = std::fs::read_to_string(&path).expect("read");
    let edited = before.replace("name: dashboard", "name: dashboard-by-hand");
    assert_ne!(
        edited, before,
        "the fixture should contain the edited value"
    );
    std::fs::write(&path, &edited).expect("write");

    let outcome = sync.write_file("cloudmap.yaml").await.expect("write");
    outcome.written.as_ref().expect("written");
    assert_eq!(
        outcome.conflicts,
        vec![],
        "different records on each side do not collide"
    );

    // The written file holds both sides...
    let merged: serde_json::Value =
        serde_saphyr::from_str(&std::fs::read_to_string(&path).expect("read")).expect("parse");
    assert_eq!(
        merged["repositories"]["git://unfurl.cloud/feb20a/dashboard.git"]["name"],
        "dashboard-by-hand"
    );
    assert_eq!(merged["repositories"]["staged"]["name"], "staged");

    // ...but the database deliberately was not updated: the write must
    // not claim the merged bytes as taken in, or the hand edit would
    // hide from every future scan behind a matching source_oid.
    let rec = sync
        .get_record(
            "cloudmap.yaml",
            "/repositories",
            "git://unfurl.cloud/feb20a/dashboard.git",
        )
        .await
        .expect("get")
        .expect("present");
    assert_eq!(
        rec.json["name"], "dashboard",
        "a write must not edit record rows"
    );

    // The next scan sees the stale source and heals: the hand edit is
    // taken in, the still-pending create is preserved without noise.
    let stats = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(stats.conflicts, vec![], "stats: {stats:?}");
    let rec = sync
        .get_record(
            "cloudmap.yaml",
            "/repositories",
            "git://unfurl.cloud/feb20a/dashboard.git",
        )
        .await
        .expect("get")
        .expect("present");
    assert_eq!(rec.json["name"], "dashboard-by-hand");
    assert!(
        sync.list_changes(None, false)
            .await
            .expect("list")
            .iter()
            .any(|r| r.key == "staged"),
        "the created record must still be pending"
    );
}

/// When both sides changed the same record, the write lands the pending
/// edit and the report carries the value it replaced — after the write,
/// the report is the only place an uncommitted disk edit survives.
async fn a_write_reports_the_value_it_replaced(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": "ours"}),
        None,
        false,
    )
    .await
    .expect("update");
    hand_edit_dashboard(tmp, "theirs");

    let outcome = sync.write_file("cloudmap.yaml").await.expect("write");
    assert!(
        outcome.written.is_none(),
        "the only pending record is conflicted, so nothing is written: {outcome:?}"
    );
    assert_eq!(outcome.conflicts.len(), 1, "{outcome:?}");
    let c = &outcome.conflicts[0];
    assert_eq!(c.kind, RecordConflictKind::ModifyModify);
    assert_eq!(c.key, DASHBOARD);
    assert_eq!(
        c.theirs.as_ref().expect("the file's value carried")["name"],
        "theirs"
    );

    let written: serde_json::Value = serde_saphyr::from_str(
        &std::fs::read_to_string(tmp.path().join("cloudmap.yaml")).expect("read"),
    )
    .expect("parse");
    assert_eq!(
        written["repositories"][DASHBOARD]["name"], "theirs",
        "the file keeps its own value until someone resolves"
    );

    // ...and the database keeps serving its own, with the file's side
    // materialized beside it.
    assert_eq!(
        sync.get_record("cloudmap.yaml", "/repositories", DASHBOARD)
            .await
            .expect("get")
            .expect("record")
            .json["name"],
        "ours"
    );
    let rows = sync.list_conflicts(None).await.expect("conflicts");
    assert_eq!(rows.len(), 1, "{rows:?}");
    assert_eq!(rows[0].key, DASHBOARD);
    assert_eq!(rows[0].json["name"], "theirs");
    assert_eq!(rows[0].conflict, Some(ConflictState::Conflict));
}

/// Consecutive writes do not conflict with their own output.
async fn repeated_writes_do_not_self_conflict(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    for name in ["first", "second", "third"] {
        sync.upsert_record(
            Some("cloudmap.yaml"),
            "/repositories",
            name,
            serde_json::json!({"name": name}),
            None,
            false,
        )
        .await
        .expect("write");
        sync.write_file("cloudmap.yaml")
            .await
            .unwrap_or_else(|e| panic!("write after {name} must not conflict: {e:?}"));
    }
}

/// A file the database has never parsed has nothing to contradict, so a
/// write to it proceeds.
async fn an_unscanned_file_has_no_conflict(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    sync.upsert_record(
        Some("brand-new.yaml"),
        "/repositories",
        "fresh",
        serde_json::json!({"name": "fresh"}),
        None,
        false,
    )
    .await
    .expect("write");
    sync.write_file("brand-new.yaml")
        .await
        .expect("a synthesised document has no recorded source")
        .written
        .expect("written");
    assert!(tmp.path().join("brand-new.yaml").exists());

    // The synthesised file must carry whatever `is_format` inspects, or
    // the next scan will not claim it and every record in it drops out
    // of the index. Asserting the predicate rather than the header text
    // pins the property `DataFormat::new_document` exists for.
    let doc: serde_json::Value = serde_saphyr::from_str(
        &std::fs::read_to_string(tmp.path().join("brand-new.yaml")).expect("read"),
    )
    .expect("yaml");
    assert!(
        CloudMapFormat.is_format(&doc),
        "a synthesised file no format claims loses its records: {doc:?}"
    );
}

/// A file whose root is a sequence has nowhere to put a record.
///
/// Whether the write refuses or replaces the document is a design
/// choice nobody has made deliberately; that it does not *panic* is
/// not. `apply_pending_records` indexes the root with
/// `as_object_mut().expect(...)`, so letting a non-mapping reach it
/// would take down the whole `save_changes` batch instead of failing
/// this one file.
async fn a_write_over_a_non_mapping_document_does_not_panic(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    std::fs::write(tmp.path().join("list.yaml"), b"- a\n- b\n").expect("seed");
    sync.upsert_record(
        Some("list.yaml"),
        "/repositories",
        "fresh",
        serde_json::json!({"name": "fresh"}),
        None,
        false,
    )
    .await
    .expect("write");

    if let Ok(res) = sync.write_file("list.yaml").await {
        assert!(res.written.is_some(), "{res:?}");
        let doc: serde_json::Value = serde_saphyr::from_str(
            &std::fs::read_to_string(tmp.path().join("list.yaml")).expect("read"),
        )
        .expect("yaml");
        assert_eq!(doc["repositories"]["fresh"]["name"], "fresh", "{doc:?}");
    }
}

crud_test!(a_write_over_a_non_mapping_document_does_not_panic);
crud_test!(a_write_merges_over_a_hand_edit);
crud_test!(a_write_reports_the_value_it_replaced);
crud_test!(repeated_writes_do_not_self_conflict);
crud_test!(an_unscanned_file_has_no_conflict);

/// One unwritable file does not stop the others, and the caller learns
/// which was which.
async fn save_reports_each_file(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    // Two files with pending records; one of them is corrupted behind
    // the database's back, so its take-in — and with it the write —
    // fails while the other proceeds.
    for (file, key) in [("cloudmap.yaml", "in-main"), ("second.yaml", "in-second")] {
        sync.upsert_record(
            Some(file),
            "/repositories",
            key,
            serde_json::json!({"name": key}),
            None,
            false,
        )
        .await
        .expect("write");
    }
    // `second.yaml` was created by the record write, so it has no
    // recorded source and nothing to take in. Give `cloudmap.yaml` the
    // failure instead: stale bytes that no longer parse.
    let path = tmp.path().join("cloudmap.yaml");
    std::fs::write(&path, "[unclosed").expect("write");

    let outcome = sync.save_changes().await.expect("save proceeds");
    assert_eq!(outcome.failed.len(), 1, "{outcome:?}");
    assert_eq!(outcome.failed[0].file_path, "cloudmap.yaml");
    assert!(matches!(outcome.failed[0].error, Error::Yaml { .. }));
    // The other file was still written -- one failure must not strand it
    // unreported, which is what failing fast used to do.
    assert_eq!(
        outcome.written,
        vec![tmp.path().join("second.yaml")],
        "{outcome:?}"
    );
    assert!(tmp.path().join("second.yaml").exists());
    assert_eq!(
        std::fs::read_to_string(&path).expect("read"),
        "[unclosed",
        "a failed take-in must not have touched the file"
    );

    // And a commit refuses while anything is unwritten, rather than
    // capturing a half-applied state.
    let err = sync
        .commit_repository("should not commit")
        .await
        .expect_err("a partial save must not be committed");
    assert!(matches!(err, Error::Yaml { .. }), "{err:?}");
}

crud_test!(save_reports_each_file);

/// The types the sync functions return are nameable from outside the
/// crate. Without the re-export a caller cannot write its return type,
/// which a build inside the crate never notices.
#[test]
fn sync_outcome_types_are_public() {
    fn _takes(_: unfurl_git_sync::SyncOutcome, _: unfurl_git_sync::SaveFailure) {}
}

// ---------------------------------------------------------------------------
// scan-side conflict detection and pending-edit preservation
// ---------------------------------------------------------------------------

/// Hand-edit the fixture's dashboard record on disk.
fn hand_edit_dashboard(tmp: &TempDir, new_name: &str) {
    let path = tmp.path().join("cloudmap.yaml");
    let before = std::fs::read_to_string(&path).expect("read");
    let edited = before.replace("name: dashboard", &format!("name: {new_name}"));
    assert_ne!(edited, before, "fixture should contain the dashboard name");
    std::fs::write(&path, edited).expect("write");
}

/// Both sides changed the same record: the pending edit wins, and the
/// divergence is reported with the commit it was based on.
async fn rescan_reports_modify_modify(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    let base = head_commit(sync).await;

    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": "ours"}),
        None,
        false,
    )
    .await
    .expect("update");
    hand_edit_dashboard(tmp, "theirs");

    let stats = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    let rec = sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(rec.json["name"], "ours", "the pending edit must survive");
    assert_eq!(stats.conflicts.len(), 1, "stats: {stats:?}");
    let c = &stats.conflicts[0];
    assert_eq!(c.kind, RecordConflictKind::ModifyModify);
    assert_eq!(
        (c.file_path.as_str(), c.path.as_str()),
        ("cloudmap.yaml", "/repositories")
    );
    assert_eq!(c.key, DASHBOARD);
    assert_eq!(c.base_commit_id.as_deref(), Some(base.as_str()));
    assert_eq!(c.theirs.as_ref().expect("theirs carried")["name"], "theirs");
    assert!(
        sync.list_changes(None, false)
            .await
            .expect("list")
            .iter()
            .any(|r| r.key == DASHBOARD),
        "the edit must still be queued for save_changes"
    );
}

/// The record under a pending edit was deleted from the file; a pending
/// create, indistinguishable in every way except its missing base, is
/// preserved without noise.
async fn rescan_reports_modify_delete_but_not_creates(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    let base = head_commit(sync).await;

    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": "ours"}),
        None,
        false,
    )
    .await
    .expect("update");
    sync.create_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://example.com/created.git",
        serde_json::json!({"name": "created"}),
        None,
        false,
    )
    .await
    .expect("create");

    // Remove the dashboard repository from the file on disk.
    let path = tmp.path().join("cloudmap.yaml");
    let text = std::fs::read_to_string(&path).expect("read");
    let mut value: serde_json::Value = serde_saphyr::from_str(&text).expect("parse");
    value
        .get_mut("repositories")
        .and_then(|v| v.as_object_mut())
        .expect("repositories section")
        .remove(DASHBOARD)
        .expect("fixture contains the repository");
    std::fs::write(&path, serde_saphyr::to_string(&value).expect("emit")).expect("write");

    let stats = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(stats.conflicts.len(), 1, "stats: {stats:?}");
    let c = &stats.conflicts[0];
    assert_eq!(c.kind, RecordConflictKind::ModifyDelete);
    assert_eq!(c.key, DASHBOARD);
    assert_eq!(c.base_commit_id.as_deref(), Some(base.as_str()));
    assert_eq!(c.theirs, None, "the file side deleted the record");
    for key in [DASHBOARD, "git://example.com/created.git"] {
        assert!(
            sync.get_record("cloudmap.yaml", "/repositories", key)
                .await
                .expect("get")
                .is_some(),
            "{key} must survive the rescan"
        );
    }
}

/// The base survives consecutive edits: the second update must not wipe
/// it just because the first already nulled `commit_id`. Guards the
/// COALESCE in the client-write SQL — without it this reports `AddAdd`
/// with no base.
async fn consecutive_updates_keep_base(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    let base = head_commit(sync).await;

    for name in ["one", "two"] {
        sync.update_record(
            Some("cloudmap.yaml"),
            "/repositories",
            DASHBOARD,
            serde_json::json!({"name": name}),
            None,
            false,
        )
        .await
        .expect("update");
    }
    hand_edit_dashboard(tmp, "theirs");

    let stats = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(stats.conflicts.len(), 1, "stats: {stats:?}");
    let c = &stats.conflicts[0];
    assert_eq!(c.kind, RecordConflictKind::ModifyModify);
    assert_eq!(c.base_commit_id.as_deref(), Some(base.as_str()));
}

/// Across an edit → commit → edit cycle, the base of the second edit is
/// the commit that carried the first — not the original take-in.
async fn base_tracks_the_latest_commit(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");

    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": "one"}),
        None,
        false,
    )
    .await
    .expect("update");
    let oid = sync
        .commit_repository("first edit")
        .await
        .expect("commit")
        .expect("something to commit");

    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": "two"}),
        None,
        false,
    )
    .await
    .expect("update again");
    // The committed write left `name: one` in the file; diverge it.
    let path = tmp.path().join("cloudmap.yaml");
    let text = std::fs::read_to_string(&path).expect("read");
    let edited = text.replace("name: one", "name: theirs");
    assert_ne!(edited, text);
    std::fs::write(&path, edited).expect("write");

    let stats = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(stats.conflicts.len(), 1, "stats: {stats:?}");
    assert_eq!(
        stats.conflicts[0].base_commit_id.as_deref(),
        Some(oid.as_str())
    );
}

/// A dirty file's rows read as committed-at-their-path's-last-commit,
/// not as pending — so a second disk edit is taken in rather than
/// "preserved" against. The case the `commit_id` semantics exist for.
async fn dirty_file_rescan_takes_in_new_edits(sync: &SyncedRepo, tmp: &TempDir) {
    hand_edit_dashboard(tmp, "first-edit");
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan dirty");
    let rec = sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(rec.json["name"], "first-edit");

    let path = tmp.path().join("cloudmap.yaml");
    let text = std::fs::read_to_string(&path).expect("read");
    std::fs::write(&path, text.replace("name: first-edit", "name: second-edit")).expect("write");

    let stats = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan dirty");
    let rec = sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(
        rec.json["name"], "second-edit",
        "a rescan of an uncommitted file must not freeze on its own rows"
    );
    assert_eq!(stats.conflicts, vec![], "nothing was pending: {stats:?}");
}

/// A hand-edited file the scan took in has nothing pending to write,
/// but `commit_repository` must still stage and commit it.
async fn commit_carries_a_hand_edit(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    hand_edit_dashboard(tmp, "by-hand");
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("take in");

    let oid = sync
        .commit_repository("hand edit")
        .await
        .expect("commit")
        .expect("the dirty file must be staged");
    let rec = sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(rec.commit_id.as_deref(), Some(oid.as_str()));
    assert_eq!(head_commit(sync).await, oid, "the commit reached the repo");
    assert!(
        sync.commit_repository("again").await.expect("ok").is_none(),
        "nothing left to commit"
    );
}

/// A rescan of an untouched tree is a no-op: no file re-extracted, no
/// version drawn — the churn is visible through `record.version`.
async fn rescan_of_untouched_tree_is_skipped(sync: &SyncedRepo, _tmp: &TempDir) {
    let first = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    assert!(first.files_updated > 0, "stats: {first:?}");
    let versions = |records: Vec<unfurl_git_sync::Record>| {
        records
            .into_iter()
            .map(|r| ((r.path, r.key), r.version))
            .collect::<std::collections::BTreeMap<_, _>>()
    };
    let before = versions(
        sync.find_records(&RecordQuery::default())
            .await
            .expect("find"),
    );

    let second = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(second.files_updated, 0, "stats: {second:?}");
    assert_eq!(
        second.files_unchanged, first.files_updated,
        "stats: {second:?}"
    );
    assert_eq!(second.records_upserted, 0, "stats: {second:?}");
    let after = versions(
        sync.find_records(&RecordQuery::default())
            .await
            .expect("find"),
    );
    assert_eq!(before, after, "a no-op rescan must not bump versions");
}

crud_test!(rescan_reports_modify_modify);
crud_test!(rescan_reports_modify_delete_but_not_creates);
crud_test!(consecutive_updates_keep_base);
crud_test!(base_tracks_the_latest_commit);
crud_test!(dirty_file_rescan_takes_in_new_edits);
crud_test!(commit_carries_a_hand_edit);
crud_test!(rescan_of_untouched_tree_is_skipped);

/// The record being deleted was edited on disk: the tombstone is not
/// resurrected, and the divergence is reported.
async fn rescan_reports_delete_modify(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    let base = head_commit(sync).await;

    sync.delete_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        None,
        false,
    )
    .await
    .expect("delete");
    hand_edit_dashboard(tmp, "theirs");

    let stats = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert!(
        sync.get_record("cloudmap.yaml", "/repositories", DASHBOARD)
            .await
            .expect("get")
            .is_none(),
        "the rescan resurrected a pending delete"
    );
    assert!(
        sync.list_changes(None, false)
            .await
            .expect("list")
            .iter()
            .any(|r| r.key == DASHBOARD && r.deleted),
        "the tombstone must still be queued for save_changes"
    );
    assert_eq!(stats.conflicts.len(), 1, "stats: {stats:?}");
    let c = &stats.conflicts[0];
    assert_eq!(c.kind, RecordConflictKind::DeleteModify);
    assert_eq!(c.key, DASHBOARD);
    assert_eq!(c.base_commit_id.as_deref(), Some(base.as_str()));
    assert_eq!(c.theirs.as_ref().expect("theirs carried")["name"], "theirs");
}

/// Both sides added the same key independently: the pending create
/// wins, reported with no base — there is no commit it diverged from.
async fn rescan_reports_add_add(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");

    let key = "git://example.com/new.git";
    sync.create_record(
        Some("cloudmap.yaml"),
        "/repositories",
        key,
        serde_json::json!({"name": "ours"}),
        None,
        false,
    )
    .await
    .expect("create");
    let path = tmp.path().join("cloudmap.yaml");
    let text = std::fs::read_to_string(&path).expect("read");
    let edited = text.replace(
        "repositories:\n",
        "repositories:\n  git://example.com/new.git:\n    name: theirs\n",
    );
    assert_ne!(edited, text, "fixture should have a repositories section");
    std::fs::write(&path, edited).expect("write");

    let stats = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    let rec = sync
        .get_record("cloudmap.yaml", "/repositories", key)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(rec.json["name"], "ours", "the pending create must win");
    assert_eq!(stats.conflicts.len(), 1, "stats: {stats:?}");
    let c = &stats.conflicts[0];
    assert_eq!(c.kind, RecordConflictKind::AddAdd);
    assert_eq!(c.key, key);
    assert_eq!(c.base_commit_id, None, "a create has no base");
    assert_eq!(c.theirs.as_ref().expect("theirs carried")["name"], "theirs");
}

crud_test!(rescan_reports_delete_modify);
crud_test!(rescan_reports_add_add);

/// A disk edit to one record bumps only that record's version: its
/// file-mates keep theirs, so their `Pending` OCC tokens stay valid and
/// `list_changes(since)` names only what actually changed.
async fn rescan_bumps_only_changed_records(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    let versions = |records: Vec<unfurl_git_sync::Record>| {
        records
            .into_iter()
            .map(|r| ((r.path, r.key), r.version))
            .collect::<std::collections::BTreeMap<_, _>>()
    };
    let before = versions(
        sync.find_records(&RecordQuery::default())
            .await
            .expect("find"),
    );
    let cursor = *before.values().max().expect("records exist");

    hand_edit_dashboard(tmp, "changed");
    let stats = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(stats.records_upserted, 1, "stats: {stats:?}");

    let after = versions(
        sync.find_records(&RecordQuery::default())
            .await
            .expect("find"),
    );
    let dashboard = ("/repositories".to_string(), DASHBOARD.to_string());
    assert!(
        after[&dashboard] > before[&dashboard],
        "the edited record moved"
    );
    for (k, v) in &before {
        if *k != dashboard {
            assert_eq!(after[k], *v, "untouched record {k:?} must keep its version");
        }
    }
    let changed = sync.list_changes(Some(cursor), false).await.expect("list");
    assert_eq!(
        changed.iter().map(|r| r.key.as_str()).collect::<Vec<_>>(),
        [DASHBOARD],
        "only the edited record is a change since the cursor"
    );
}

crud_test!(rescan_bumps_only_changed_records);

/// A pending edit on X and a disk edit on Y do not conflict: X's disk
/// value is still exactly the base the client edited from. Only a
/// record whose file-side value moved off the base diverges.
async fn edit_of_a_neighbor_is_not_a_conflict(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://unfurl.cloud/onecommons/std.git",
        serde_json::json!({"name": "ours"}),
        None,
        false,
    )
    .await
    .expect("update");
    hand_edit_dashboard(tmp, "theirs"); // a different record

    let stats = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(stats.conflicts, vec![], "stats: {stats:?}");
}

crud_test!(edit_of_a_neighbor_is_not_a_conflict);

/// `save_changes` aggregates the conflicts its writes' take-ins found,
/// alongside the per-file written / failed lists.
async fn save_aggregates_conflicts(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": "ours"}),
        None,
        false,
    )
    .await
    .expect("update");
    sync.upsert_record(
        Some("second.yaml"),
        "/repositories",
        "in-second",
        serde_json::json!({"name": "in-second"}),
        None,
        false,
    )
    .await
    .expect("write");
    hand_edit_dashboard(tmp, "theirs");

    let outcome = sync.save_changes().await.expect("save");
    assert_eq!(outcome.failed.len(), 0, "{outcome:?}");
    assert_eq!(outcome.conflicts.len(), 1, "{outcome:?}");
    assert_eq!(outcome.conflicts[0].kind, RecordConflictKind::ModifyModify);
    assert_eq!(outcome.conflicts[0].file_path, "cloudmap.yaml");
    // One file's only pending record is conflicted, so it is left
    // alone; a conflict in one file doesn't hold up any other.
    assert_eq!(outcome.written, vec![tmp.path().join("second.yaml")]);
}

crud_test!(save_aggregates_conflicts);

/// `commit_repository` scans before saving, so a commit over a
/// hand-edited file carries the merge and attributes rows to a commit
/// that actually holds their json — no manual scan needed first.
async fn commit_takes_outside_edits_in_first(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "staged",
        serde_json::json!({"name": "staged"}),
        None,
        false,
    )
    .await
    .expect("write");
    hand_edit_dashboard(tmp, "by-hand");

    let oid = sync
        .commit_repository("merge it all")
        .await
        .expect("commit")
        .expect("committed");

    // The committed file holds both sides, and the database agrees
    // with it — the pre-save scan is what keeps roll_forward from
    // stamping a row with a commit that doesn't carry its json.
    let rec = sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(rec.json["name"], "by-hand");
    assert_eq!(rec.commit_id.as_deref(), Some(oid.as_str()));
    let on_disk: serde_json::Value = serde_saphyr::from_str(
        &std::fs::read_to_string(tmp.path().join("cloudmap.yaml")).expect("read"),
    )
    .expect("parse");
    assert_eq!(on_disk["repositories"][DASHBOARD]["name"], "by-hand");
    assert_eq!(on_disk["repositories"]["staged"]["name"], "staged");
    assert!(
        sync.list_changes(None, false)
            .await
            .expect("list")
            .is_empty(),
        "everything is committed"
    );
}

crud_test!(commit_takes_outside_edits_in_first);

// ---------------------------------------------------------------------------
// Materialized conflicts
// ---------------------------------------------------------------------------
//
// The principle these all check: the file and the database each keep
// their own view of a record they disagree about, and neither is
// overwritten until someone says which wins.
//
// The outcome space is (conflict kind × what settles it). Cells hold
// the distinguishing part of the test name, so each is greppable as
// written:
//
// kind         | conflict row      | Ours         | Theirs            | Merged         | Delete
// -------------+-------------------+--------------+-------------------+----------------+-------------
// ModifyModify | materializes_both | ours_applies | theirs_takes      | merged_settles | delete_drops
//              |                   | ours_reopens |                   |                |
// ModifyDelete | as_a_tombstone    | ours_keeps   | as_a_tombstone    | (a)            | (a)
// DeleteModify | theirs_resurrects | (b)          | theirs_resurrects | (a)            | (a)
// AddAdd       | has_no_base       | (b)          | has_no_base       | (a)            | (a)
//
// The gaps are deliberate, not missing coverage:
//   (a) `Merged` and `Delete` don't branch on the kind at all -- they
//       name a value (or its absence) and write it, so a second kind
//       would re-run identical code.
//   (b) `Ours` and `Theirs` branch only on whether the *file's* side is
//       deleted, and both halves of that are covered above:
//       ModifyModify/ModifyDelete for `Ours`, ModifyDelete (delete) and
//       DeleteModify (resurrect) for `Theirs`.
//
// Tests below that aren't in the grid cover the mechanism rather than a
// cell of it: a standing conflict surviving a save and a commit, the
// commit stamping the file's side, a pending delete escaping the
// tombstone purge, a divergence ending on its own, `ScanOptions::force`,
// and the `Git-Sync-Resolves-Version` trailer.

/// Set a `/repositories` record in the file, or remove it with `None`.
/// Rewrites the whole document, so unlike [`rename_name`] the record
/// ends up holding exactly what is passed.
fn set_record_on_disk(tmp: &TempDir, key: &str, json: Option<serde_json::Value>) {
    let path = tmp.path().join("cloudmap.yaml");
    let mut doc: serde_json::Value =
        serde_saphyr::from_str(&std::fs::read_to_string(&path).expect("read")).expect("yaml");
    let repos = doc["repositories"].as_object_mut().expect("repositories");
    match json {
        Some(value) => {
            repos.insert(key.to_string(), value);
        }
        None => assert!(repos.remove(key).is_some(), "{key} should be in the file"),
    }
    std::fs::write(&path, serde_saphyr::to_string(&doc).expect("emit")).expect("write");
}

async fn a_conflict_materializes_both_sides(sync: &SyncedRepo, tmp: &TempDir) {
    stand_up_conflict(sync, tmp).await;

    // The API serves the database's row, still in flight...
    let ours = sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(ours.json["name"], "ours");
    assert!(ours.commit_id.is_none(), "not saved yet: {ours:?}");
    assert_eq!(ours.conflict, None, "the database's own row");

    // ...while the file keeps its value. The scan takes in disk
    // changes, but not over an edit that hasn't reached the file.
    assert_eq!(dashboard_on_disk(tmp)["name"], "theirs");

    // A default query sees one row for the key; both, when asked.
    let one_key = RecordQuery {
        path: Some("/repositories".into()),
        key: Some(DASHBOARD.into()),
        ..Default::default()
    };
    assert_eq!(sync.find_records(&one_key).await.expect("find").len(), 1);
    let both = sync
        .find_records(&RecordQuery {
            include_conflicts: true,
            ..one_key
        })
        .await
        .expect("find");
    assert_eq!(both.len(), 2, "{both:?}");

    let theirs = only_conflict(sync).await;
    assert_eq!(theirs.json["name"], "theirs");
    assert_eq!(theirs.conflict, Some(ConflictState::Conflict));
    assert!(!theirs.deleted, "the file still has the record");
}

async fn a_standing_conflict_survives_a_save(sync: &SyncedRepo, tmp: &TempDir) {
    stand_up_conflict(sync, tmp).await;

    let saved = sync.save_changes().await.expect("save");
    assert!(
        saved.written.is_empty(),
        "the file's value stands until someone resolves: {saved:?}"
    );
    assert_eq!(saved.conflicts.len(), 1, "{saved:?}");
    assert_eq!(saved.conflicts[0].kind, RecordConflictKind::ModifyModify);
    assert_eq!(
        saved.conflicts[0].theirs.as_ref().expect("carried")["name"],
        "theirs"
    );
    assert_eq!(dashboard_on_disk(tmp)["name"], "theirs");

    // Repeated saves neither apply it nor lose the report.
    let again = sync.save_changes().await.expect("save");
    assert_eq!(again.conflicts.len(), 1, "{again:?}");
    assert_eq!(dashboard_on_disk(tmp)["name"], "theirs");
    let ours = sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(ours.json["name"], "ours");
    assert!(ours.commit_id.is_none(), "still unsaved: {ours:?}");
}

async fn a_commit_carries_the_file_not_the_record(sync: &SyncedRepo, tmp: &TempDir) {
    stand_up_conflict(sync, tmp).await;
    let oid = sync
        .commit_repository("carry the hand edit")
        .await
        .expect("commit")
        .expect("the hand edit is not in git yet");

    // What the commit holds is the file's value, so that is what gets
    // stamped with it.
    let theirs = only_conflict(sync).await;
    assert_eq!(theirs.json["name"], "theirs");
    assert_eq!(
        theirs.commit_id.as_deref(),
        Some(oid.as_str()),
        "the conflict row is what this commit carries"
    );

    // The record is not in the commit, so it stays in flight.
    let ours = sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(ours.json["name"], "ours");
    assert!(
        ours.commit_id.is_none(),
        "the commit does not carry ours: {ours:?}"
    );

    // And there is nothing left to commit. Without this, a standing
    // conflict would append an empty commit on every call, forever.
    assert_eq!(
        sync.commit_repository("again").await.expect("commit"),
        None,
        "nothing changed on disk"
    );
    assert_eq!(head_commit(sync).await, oid, "HEAD did not move");
}

async fn a_pending_delete_under_a_conflict_survives_a_commit(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    let write = sync
        .delete_record(
            Some("cloudmap.yaml"),
            "/repositories",
            DASHBOARD,
            None,
            false,
        )
        .await
        .expect("delete");
    rename_name(tmp, "dashboard", "theirs");
    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(scan.conflicts.len(), 1, "{scan:?}");
    assert_eq!(scan.conflicts[0].kind, RecordConflictKind::DeleteModify);

    sync.commit_repository("carry the hand edit")
        .await
        .expect("commit")
        .expect("the hand edit is not in git yet");

    // The commit did not apply the delete -- the record is still in the
    // file -- so purging the tombstone would drop the client's delete
    // with nothing to show for it.
    let row = sync
        .get_record_by_id(write.id)
        .await
        .expect("get")
        .expect("the tombstone outlives the commit");
    assert!(row.deleted, "{row:?}");
    assert!(
        row.commit_id.is_none(),
        "not carried by the commit: {row:?}"
    );
    assert_eq!(dashboard_on_disk(tmp)["name"], "theirs");
}

async fn resolve_ours_applies_on_the_next_write(sync: &SyncedRepo, tmp: &TempDir) {
    stand_up_conflict(sync, tmp).await;
    sync.resolve_conflict(
        "cloudmap.yaml",
        "/repositories",
        DASHBOARD,
        Resolution::Ours,
        None,
    )
    .await
    .expect("resolve");

    // Marking it resolved doesn't touch the file: the write does.
    assert_eq!(dashboard_on_disk(tmp)["name"], "theirs");

    let saved = sync.save_changes().await.expect("save");
    assert!(saved.conflicts.is_empty(), "{saved:?}");
    assert_eq!(dashboard_on_disk(tmp)["name"], "ours");
    assert!(
        sync.list_conflicts(None).await.expect("list").is_empty(),
        "the conflict row goes with the resolution it settled"
    );
}

async fn resolve_ours_reopens_when_the_file_moves_again(sync: &SyncedRepo, tmp: &TempDir) {
    stand_up_conflict(sync, tmp).await;
    sync.resolve_conflict(
        "cloudmap.yaml",
        "/repositories",
        DASHBOARD,
        Resolution::Ours,
        None,
    )
    .await
    .expect("resolve");
    // The file moves again before the resolution reaches it, so the
    // decision was made about a value that is gone.
    rename_name(tmp, "theirs", "later");

    let saved = sync.save_changes().await.expect("save");
    assert_eq!(saved.conflicts.len(), 1, "{saved:?}");
    assert_eq!(
        dashboard_on_disk(tmp)["name"],
        "later",
        "nothing was applied over the newer value"
    );
    let theirs = only_conflict(sync).await;
    assert_eq!(theirs.conflict, Some(ConflictState::Conflict), "re-opened");
    assert_eq!(theirs.json["name"], "later");
}

async fn resolve_theirs_takes_the_files_value(sync: &SyncedRepo, tmp: &TempDir) {
    stand_up_conflict(sync, tmp).await;
    sync.resolve_conflict(
        "cloudmap.yaml",
        "/repositories",
        DASHBOARD,
        Resolution::Theirs,
        None,
    )
    .await
    .expect("resolve");

    let rec = sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(rec.json["name"], "theirs");
    assert!(sync.list_conflicts(None).await.expect("list").is_empty());

    // Both sides now say the same thing, so the save has nothing to
    // report and nothing to change.
    let saved = sync.save_changes().await.expect("save");
    assert!(saved.conflicts.is_empty(), "{saved:?}");
    assert_eq!(dashboard_on_disk(tmp)["name"], "theirs");
}

async fn resolve_merged_settles_with_a_third_value(sync: &SyncedRepo, tmp: &TempDir) {
    stand_up_conflict(sync, tmp).await;
    sync.resolve_conflict(
        "cloudmap.yaml",
        "/repositories",
        DASHBOARD,
        Resolution::Merged(serde_json::json!({"name": "merged"})),
        None,
    )
    .await
    .expect("resolve");
    assert!(sync.list_conflicts(None).await.expect("list").is_empty());

    let saved = sync.save_changes().await.expect("save");
    assert!(saved.conflicts.is_empty(), "{saved:?}");
    assert_eq!(dashboard_on_disk(tmp)["name"], "merged");
}

async fn a_record_the_file_dropped_conflicts_as_a_tombstone(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": "ours"}),
        None,
        false,
    )
    .await
    .expect("update");
    set_record_on_disk(tmp, DASHBOARD, None);
    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(scan.conflicts.len(), 1, "{scan:?}");
    assert_eq!(scan.conflicts[0].kind, RecordConflictKind::ModifyDelete);

    // The file's side is "this record is gone", carrying the value it
    // dropped -- and it survives the pass that hard-deletes records the
    // file no longer has.
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan again");
    let theirs = only_conflict(sync).await;
    assert!(theirs.deleted, "{theirs:?}");
    assert_eq!(theirs.json["name"], "dashboard", "the value the file lost");

    // Taking the file's side of a record it no longer has deletes ours.
    sync.resolve_conflict(
        "cloudmap.yaml",
        "/repositories",
        DASHBOARD,
        Resolution::Theirs,
        None,
    )
    .await
    .expect("resolve");
    assert!(sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .is_none());
    assert!(sync.list_conflicts(None).await.expect("list").is_empty());
}

/// Commit everything on disk with a `Git-Sync-Resolves-Version` trailer.
fn commit_resolving(tmp: &TempDir, version: i64) {
    git(tmp.path(), &["add", "-A"]);
    git(
        tmp.path(),
        &[
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=Tester",
            "commit",
            "-m",
            &format!("hand edit\n\nGit-Sync-Resolves-Version: {version}"),
        ],
    );
    // Assert git itself parses the block: a missing blank line makes it
    // prose, which a substring check would happily still match.
    assert_eq!(
        head_trailer(tmp.path(), "Git-Sync-Resolves-Version").as_deref(),
        Some(version.to_string().as_str())
    );
}

async fn the_resolves_version_trailer_settles_a_conflict(sync: &SyncedRepo, tmp: &TempDir) {
    let version = stand_up_conflict(sync, tmp).await;
    // Committing the hand edit as it stands: the bytes are the ones the
    // scan already took in, so this is the "keep the file as it is and
    // drop the database's edit" resolution, which changes nothing on
    // disk and would otherwise be swallowed by the unchanged-file skip.
    commit_resolving(tmp, version);

    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert!(scan.conflicts.is_empty(), "{scan:?}");
    assert_eq!(
        sync.get_record("cloudmap.yaml", "/repositories", DASHBOARD)
            .await
            .expect("get")
            .expect("present")
            .json["name"],
        "theirs",
        "the file won"
    );
    assert!(sync.list_conflicts(None).await.expect("list").is_empty());
    assert!(
        sync.list_changes(None, false)
            .await
            .expect("list")
            .is_empty(),
        "the in-flight edit was settled, not left hanging"
    );
}

async fn an_older_trailer_settles_nothing(sync: &SyncedRepo, tmp: &TempDir) {
    let version = stand_up_conflict(sync, tmp).await;
    // A trailer naming a version *older* than the client's edit: its
    // author cannot have seen that edit, so it does not settle it.
    commit_resolving(tmp, version - 1);

    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(scan.conflicts.len(), 1, "{scan:?}");
    assert_eq!(
        sync.get_record("cloudmap.yaml", "/repositories", DASHBOARD)
            .await
            .expect("get")
            .expect("present")
            .json["name"],
        "ours"
    );
    assert_eq!(
        only_conflict(sync).await.conflict,
        Some(ConflictState::Conflict)
    );
}

async fn the_trailer_spares_an_unrelated_unsaved_edit(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    // An ordinary unsaved edit: the file still holds what it was based
    // on, so there is no divergence for a trailer to settle.
    let write = sync
        .update_record(
            Some("cloudmap.yaml"),
            "/repositories",
            DASHBOARD,
            serde_json::json!({"name": "ours"}),
            None,
            false,
        )
        .await
        .expect("update");
    // Something else in the file changes, so the commit touches the
    // path and its trailer is the one the scan reads.
    rename_name(tmp, "std", "std-edited");
    commit_resolving(tmp, write.version);

    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert!(scan.conflicts.is_empty(), "{scan:?}");
    assert_eq!(
        sync.get_record("cloudmap.yaml", "/repositories", DASHBOARD)
            .await
            .expect("get")
            .expect("present")
            .json["name"],
        "ours",
        "a trailer settles conflicts; it does not discard unsaved work"
    );
    assert_eq!(
        sync.get_record(
            "cloudmap.yaml",
            "/repositories",
            "git://unfurl.cloud/onecommons/std.git"
        )
        .await
        .expect("get")
        .expect("present")
        .json["name"],
        "std-edited",
        "the unrelated disk change was taken in as usual"
    );
}

async fn a_forced_scan_hands_every_record_to_the_file(sync: &SyncedRepo, tmp: &TempDir) {
    stand_up_conflict(sync, tmp).await;
    // A second, non-conflicted unsaved edit: `force` is the blanket
    // assertion, so it takes this one too.
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://unfurl.cloud/onecommons/std.git",
        serde_json::json!({"name": "unsaved"}),
        None,
        false,
    )
    .await
    .expect("upsert");

    let scan = sync
        .update_from_working_dir(ScanOptions { force: true })
        .await
        .expect("forced rescan");
    assert!(scan.conflicts.is_empty(), "{scan:?}");
    assert!(sync.list_conflicts(None).await.expect("list").is_empty());
    assert_eq!(
        sync.get_record("cloudmap.yaml", "/repositories", DASHBOARD)
            .await
            .expect("get")
            .expect("present")
            .json["name"],
        "theirs"
    );
    assert_eq!(
        sync.get_record(
            "cloudmap.yaml",
            "/repositories",
            "git://unfurl.cloud/onecommons/std.git"
        )
        .await
        .expect("get")
        .expect("present")
        .json["name"],
        "std",
        "the unsaved edit went too"
    );
    assert!(
        sync.list_changes(None, false)
            .await
            .expect("list")
            .is_empty(),
        "nothing is in flight after the file wins"
    );
}

crud_test!(a_conflict_materializes_both_sides);
crud_test!(a_standing_conflict_survives_a_save);
crud_test!(a_commit_carries_the_file_not_the_record);
crud_test!(a_pending_delete_under_a_conflict_survives_a_commit);
crud_test!(resolve_ours_applies_on_the_next_write);
crud_test!(resolve_ours_reopens_when_the_file_moves_again);
crud_test!(resolve_theirs_takes_the_files_value);
crud_test!(resolve_merged_settles_with_a_third_value);
crud_test!(a_record_the_file_dropped_conflicts_as_a_tombstone);
crud_test!(the_resolves_version_trailer_settles_a_conflict);
crud_test!(an_older_trailer_settles_nothing);
crud_test!(the_trailer_spares_an_unrelated_unsaved_edit);
crud_test!(a_forced_scan_hands_every_record_to_the_file);

async fn resolve_delete_drops_the_record_from_both_sides(sync: &SyncedRepo, tmp: &TempDir) {
    stand_up_conflict(sync, tmp).await;
    let write = sync
        .resolve_conflict(
            "cloudmap.yaml",
            "/repositories",
            DASHBOARD,
            Resolution::Delete,
            None,
        )
        .await
        .expect("resolve");

    // Neither side's value won: the record goes.
    assert!(sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .is_none());
    let row = sync
        .get_record_by_id(write.id)
        .await
        .expect("get")
        .expect("tombstone");
    assert!(row.deleted, "{row:?}");
    assert!(sync.list_conflicts(None).await.expect("list").is_empty());

    // With nothing left contested, the save applies the delete.
    let saved = sync.save_changes().await.expect("save");
    assert!(saved.conflicts.is_empty(), "{saved:?}");
    let doc: serde_json::Value = serde_saphyr::from_str(
        &std::fs::read_to_string(tmp.path().join("cloudmap.yaml")).expect("read"),
    )
    .expect("yaml");
    assert!(doc["repositories"].get(DASHBOARD).is_none(), "{doc:?}");
}

/// A pending edit of a record the file dropped, settled the other way:
/// the tombstone-shaped conflict row goes to `resolved`, which both the
/// scan and the write have to read as "the file has no value here".
async fn resolve_ours_keeps_a_record_the_file_dropped(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": "ours"}),
        None,
        false,
    )
    .await
    .expect("update");
    set_record_on_disk(tmp, DASHBOARD, None);
    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(scan.conflicts[0].kind, RecordConflictKind::ModifyDelete);

    sync.resolve_conflict(
        "cloudmap.yaml",
        "/repositories",
        DASHBOARD,
        Resolution::Ours,
        None,
    )
    .await
    .expect("resolve");
    let marked = only_conflict(sync).await;
    assert_eq!(marked.conflict, Some(ConflictState::Resolved));
    assert!(
        marked.deleted,
        "the file still has no value here: {marked:?}"
    );

    // A scan in between must let the resolution stand rather than
    // re-open it against a file that has not moved.
    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert!(scan.conflicts.is_empty(), "{scan:?}");
    assert_eq!(
        only_conflict(sync).await.conflict,
        Some(ConflictState::Resolved)
    );

    let saved = sync.save_changes().await.expect("save");
    assert!(saved.conflicts.is_empty(), "{saved:?}");
    assert_eq!(dashboard_on_disk(tmp)["name"], "ours", "put back");
    assert!(sync.list_conflicts(None).await.expect("list").is_empty());
}

/// The same resolution, but with the scan actually reading the file.
///
/// The rescan above skips it -- nothing on disk moved since the conflict
/// -- so it never reaches the keep-set decision. An unrelated edit
/// elsewhere in the file gives the scan a reason to look, and a resolved
/// tombstone left out of the keep set is hard-deleted by
/// `delete_missing`: the resolution is lost before the write can apply
/// it.
async fn a_rescan_keeps_a_resolved_deletion(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": "ours"}),
        None,
        false,
    )
    .await
    .expect("update");
    set_record_on_disk(tmp, DASHBOARD, None);
    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(scan.conflicts[0].kind, RecordConflictKind::ModifyDelete);
    sync.resolve_conflict(
        "cloudmap.yaml",
        "/repositories",
        DASHBOARD,
        Resolution::Ours,
        None,
    )
    .await
    .expect("resolve");

    set_record_on_disk(
        tmp,
        "git://example.com/unrelated.git",
        Some(serde_json::json!({"name": "unrelated"})),
    );
    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert!(scan.conflicts.is_empty(), "{scan:?}");

    let ours = sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get");
    assert!(
        ours.is_some(),
        "the scan dropped the row the resolution was holding open"
    );
    assert_eq!(ours.expect("present").json["name"], "ours");
    assert_eq!(
        only_conflict(sync).await.conflict,
        Some(ConflictState::Resolved)
    );

    sync.save_changes().await.expect("save");
    assert_eq!(dashboard_on_disk(tmp)["name"], "ours", "put back");
}

async fn an_add_add_conflict_has_no_base(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    // A create, so the row has no base commit...
    sync.create_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://example.com/both.git",
        serde_json::json!({"name": "ours"}),
        None,
        false,
    )
    .await
    .expect("create");
    // ...and the same key turns up in the file independently.
    set_record_on_disk(
        tmp,
        "git://example.com/both.git",
        Some(serde_json::json!({"name": "theirs"})),
    );

    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(scan.conflicts.len(), 1, "{scan:?}");
    assert_eq!(scan.conflicts[0].kind, RecordConflictKind::AddAdd);
    assert_eq!(
        scan.conflicts[0].base_commit_id, None,
        "neither side edited a shared ancestor"
    );
    let theirs = only_conflict(sync).await;
    assert_eq!(theirs.json["name"], "theirs");
    assert!(!theirs.deleted);

    sync.resolve_conflict(
        "cloudmap.yaml",
        "/repositories",
        "git://example.com/both.git",
        Resolution::Theirs,
        None,
    )
    .await
    .expect("resolve");
    assert_eq!(
        sync.get_record(
            "cloudmap.yaml",
            "/repositories",
            "git://example.com/both.git"
        )
        .await
        .expect("get")
        .expect("present")
        .json["name"],
        "theirs"
    );
    assert!(sync.list_conflicts(None).await.expect("list").is_empty());
}

async fn a_conflict_ends_when_the_two_sides_converge(sync: &SyncedRepo, tmp: &TempDir) {
    stand_up_conflict(sync, tmp).await;
    // The file is edited to say exactly what the database's row says.
    set_record_on_disk(tmp, DASHBOARD, Some(serde_json::json!({"name": "ours"})));

    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert!(scan.conflicts.is_empty(), "{scan:?}");
    assert!(
        sync.list_conflicts(None).await.expect("list").is_empty(),
        "a divergence that ended on its own leaves no row behind"
    );
    // The record is still the client's to commit -- agreeing with the
    // file is not the same as being in it.
    let ours = sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(ours.json["name"], "ours");
    assert!(ours.commit_id.is_none(), "{ours:?}");
}

async fn a_forced_scan_drops_a_record_the_file_lost(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    let write = sync
        .update_record(
            Some("cloudmap.yaml"),
            "/repositories",
            DASHBOARD,
            serde_json::json!({"name": "ours"}),
            None,
            false,
        )
        .await
        .expect("update");
    set_record_on_disk(tmp, DASHBOARD, None);
    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(scan.conflicts[0].kind, RecordConflictKind::ModifyDelete);

    // The file says the record is gone, and force says the file wins:
    // the in-flight row goes with it rather than being preserved.
    let scan = sync
        .update_from_working_dir(ScanOptions { force: true })
        .await
        .expect("forced rescan");
    assert!(scan.conflicts.is_empty(), "{scan:?}");
    assert!(
        sync.get_record_by_id(write.id)
            .await
            .expect("get")
            .is_none(),
        "the row was hard-deleted, not tombstoned"
    );
    assert!(sync.list_conflicts(None).await.expect("list").is_empty());
}

crud_test!(resolve_delete_drops_the_record_from_both_sides);
crud_test!(resolve_ours_keeps_a_record_the_file_dropped);
crud_test!(a_rescan_keeps_a_resolved_deletion);
crud_test!(an_add_add_conflict_has_no_base);
crud_test!(a_conflict_ends_when_the_two_sides_converge);
crud_test!(a_forced_scan_drops_a_record_the_file_lost);

/// Ours deletes the record, the file edits it, and the file wins: the
/// tombstone has to come back to life rather than merely change value.
async fn resolve_theirs_resurrects_a_record_ours_deleted(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    let write = sync
        .delete_record(
            Some("cloudmap.yaml"),
            "/repositories",
            DASHBOARD,
            None,
            false,
        )
        .await
        .expect("delete");
    rename_name(tmp, "dashboard", "theirs");
    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(scan.conflicts[0].kind, RecordConflictKind::DeleteModify);
    let theirs = only_conflict(sync).await;
    assert!(!theirs.deleted, "the file still has the record: {theirs:?}");

    sync.resolve_conflict(
        "cloudmap.yaml",
        "/repositories",
        DASHBOARD,
        Resolution::Theirs,
        None,
    )
    .await
    .expect("resolve");

    let row = sync
        .get_record_by_id(write.id)
        .await
        .expect("get")
        .expect("same row");
    assert!(!row.deleted, "the tombstone was resurrected: {row:?}");
    assert_eq!(row.json["name"], "theirs");
    assert!(sync.list_conflicts(None).await.expect("list").is_empty());
}

crud_test!(resolve_theirs_resurrects_a_record_ours_deleted);
