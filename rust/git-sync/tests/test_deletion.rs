// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Removing a file, in both directions.

mod common;

use common::{
    crud_test, dashboard_on_disk, git, head_commit, only_conflict, rename_name, rename_name_in,
    stand_up_conflict, DASHBOARD,
};
use tempfile::TempDir;
use unfurl_git_sync::{
    BatchOp, CommitRef, Error, RecordConflictKind, RecordQuery, ScanOptions, SyncedRepo,
};

// ---------------------------------------------------------------------------
// File deletion
// ---------------------------------------------------------------------------

async fn delete_file_removes_it_from_disk_and_git(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    let path = tmp.path().join("cloudmap.yaml");
    assert!(path.exists());

    let tombstoned = sync
        .delete_file("cloudmap.yaml", None)
        .await
        .expect("delete_file");
    assert!(!tombstoned.is_empty(), "every record went with it");
    // Nothing has touched the disk yet -- the deletion is in flight,
    // and list_changes is where a client sees it.
    assert!(path.exists());
    let pending = sync.list_changes(None, false).await.expect("list");
    assert_eq!(pending.len(), tombstoned.len());
    assert!(pending.iter().all(|r| r.deleted), "{pending:?}");

    let saved = sync.save_changes().await.expect("save");
    assert!(saved.failed.is_empty(), "{saved:?}");
    assert_eq!(saved.files_deleted, 1, "{saved:?}");
    assert!(
        !path.exists(),
        "the file is gone, not left as a header-only stub"
    );

    let oid = sync
        .commit_repository("drop the cloudmap")
        .await
        .expect("commit")
        .expect("a removal is a change");
    let repo = unfurl_git_sync::git::open_repo(tmp.path()).expect("open");
    assert!(
        unfurl_git_sync::git::read_blob_at_commit(&repo, &oid, "cloudmap.yaml")
            .expect("read")
            .is_none(),
        "the commit carries the removal"
    );
    assert!(
        sync.get_file("cloudmap.yaml").await.expect("get").is_none(),
        "the file row was purged with the commit"
    );
    assert!(sync
        .list_changes(None, false)
        .await
        .expect("list")
        .is_empty());
}

async fn delete_file_rejects_a_stale_token(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    // A write to *some* record in the file moves the file's high-water
    // mark, so a token from before it no longer describes the file.
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
    let stale = sync
        .delete_file("cloudmap.yaml", Some(CommitRef::Pending(write.version - 1)))
        .await;
    assert!(matches!(stale, Err(Error::Conflict { .. })), "{stale:?}");

    sync.delete_file("cloudmap.yaml", Some(CommitRef::Pending(write.version)))
        .await
        .expect("a current token passes");
}

async fn a_record_written_back_undoes_a_file_deletion(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    sync.delete_file("cloudmap.yaml", None)
        .await
        .expect("delete_file");
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://example.com/kept.git",
        serde_json::json!({"name": "kept"}),
        None,
        false,
    )
    .await
    .expect("upsert");

    let saved = sync.save_changes().await.expect("save");
    assert_eq!(
        saved.files_deleted, 0,
        "the file has content again: {saved:?}"
    );
    let path = tmp.path().join("cloudmap.yaml");
    assert!(path.exists(), "a file with a record in it plainly exists");
    let doc: serde_json::Value =
        serde_saphyr::from_str(&std::fs::read_to_string(&path).expect("read")).expect("yaml");
    assert!(doc["repositories"]
        .get("git://example.com/kept.git")
        .is_some());
    assert!(
        doc["repositories"].get(DASHBOARD).is_none(),
        "the rest went"
    );
    assert!(
        !sync
            .get_file("cloudmap.yaml")
            .await
            .expect("get")
            .expect("row")
            .deleted,
        "the deletion no longer holds"
    );
}

crud_test!(delete_file_removes_it_from_disk_and_git);
crud_test!(delete_file_rejects_a_stale_token);
crud_test!(a_record_written_back_undoes_a_file_deletion);

async fn a_deleted_file_is_taken_in_and_committed(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    let before = sync
        .find_records(&RecordQuery::default())
        .await
        .expect("find")
        .len();
    assert!(before > 0);

    // Unlinked but still in the index -- a plain `rm`.
    std::fs::remove_file(tmp.path().join("cloudmap.yaml")).expect("rm");
    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(scan.files_deleted, 1, "{scan:?}");
    assert!(
        sync.find_records(&RecordQuery::default())
            .await
            .expect("find")
            .is_empty(),
        "its records went with it"
    );
    assert!(
        sync.get_file("cloudmap.yaml")
            .await
            .expect("get")
            .expect("row")
            .deleted,
        "the database owes git the removal"
    );

    let oid = sync
        .commit_repository("drop it")
        .await
        .expect("commit")
        .expect("the working tree lost a file git still has");
    let repo = unfurl_git_sync::git::open_repo(tmp.path()).expect("open");
    assert!(
        unfurl_git_sync::git::read_blob_at_commit(&repo, &oid, "cloudmap.yaml")
            .expect("read")
            .is_none()
    );
    assert!(
        sync.get_file("cloudmap.yaml").await.expect("get").is_none(),
        "and the row is purged"
    );
}

async fn a_deletion_already_in_git_makes_no_commit(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    std::fs::remove_file(tmp.path().join("cloudmap.yaml")).expect("rm");
    git(tmp.path(), &["rm", "--cached", "cloudmap.yaml"]);
    git(
        tmp.path(),
        &[
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=Tester",
            "commit",
            "-m",
            "remove it by hand",
        ],
    );
    let head = head_commit(sync).await;

    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(scan.files_deleted, 1, "{scan:?}");
    // git already records the removal, so there is nothing to commit --
    // but the row still has to go.
    assert_eq!(
        sync.commit_repository("nothing to do")
            .await
            .expect("commit"),
        None
    );
    assert_eq!(head_commit(sync).await, head, "HEAD did not move");
    assert!(sync.get_file("cloudmap.yaml").await.expect("get").is_none());
}

async fn a_pending_edit_survives_the_file_being_deleted(sync: &SyncedRepo, tmp: &TempDir) {
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
    std::fs::remove_file(tmp.path().join("cloudmap.yaml")).expect("rm");

    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(scan.conflicts.len(), 1, "{scan:?}");
    assert_eq!(scan.conflicts[0].kind, RecordConflictKind::ModifyDelete);
    let row = sync
        .get_record_by_id(write.id)
        .await
        .expect("get")
        .expect("the edit is not collateral of the file going");
    assert_eq!(row.json["name"], "ours");
    assert!(!row.deleted);
}

async fn a_renamed_file_keeps_its_records(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    let before = sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .expect("present");
    // A pending edit, to prove its OCC token survives the move.
    let write = sync
        .update_record(
            Some("cloudmap.yaml"),
            "/repositories",
            "git://unfurl.cloud/onecommons/std.git",
            serde_json::json!({"name": "edited"}),
            None,
            false,
        )
        .await
        .expect("update");
    git(tmp.path(), &["mv", "cloudmap.yaml", "moved.yaml"]);

    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(
        scan.files_renamed,
        vec![("cloudmap.yaml".to_string(), "moved.yaml".to_string())],
        "{scan:?}"
    );
    assert!(scan.conflicts.is_empty(), "a move is not a divergence");
    assert_eq!(scan.files_deleted, 0, "{scan:?}");

    let after = sync
        .get_record("moved.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .expect("present at the new path");
    assert_eq!(after.id, before.id, "same row, not a fresh one");
    assert_eq!(after.version, before.version, "and the same version");
    assert!(sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .is_none());

    // The token minted before the move is still good after it.
    sync.update_record(
        Some("moved.yaml"),
        "/repositories",
        "git://unfurl.cloud/onecommons/std.git",
        serde_json::json!({"name": "edited again"}),
        Some(CommitRef::Pending(write.version)),
        false,
    )
    .await
    .expect("the pending token survived the rename");
}

async fn a_move_that_also_edits_is_not_a_rename(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    git(tmp.path(), &["mv", "cloudmap.yaml", "moved.yaml"]);
    // The bytes no longer match, so there is nothing to pair on and the
    // honest answer is delete-and-add.
    rename_name_in(tmp, "moved.yaml", "dashboard", "moved-and-edited");

    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert!(scan.files_renamed.is_empty(), "{scan:?}");
    assert_eq!(scan.files_deleted, 1, "{scan:?}");
    assert_eq!(
        sync.get_record("moved.yaml", "/repositories", DASHBOARD)
            .await
            .expect("get")
            .expect("present")
            .json["name"],
        "moved-and-edited"
    );
}

crud_test!(a_deleted_file_is_taken_in_and_committed);
crud_test!(a_deletion_already_in_git_makes_no_commit);
crud_test!(a_pending_edit_survives_the_file_being_deleted);
crud_test!(a_renamed_file_keeps_its_records);
crud_test!(a_move_that_also_edits_is_not_a_rename);

async fn a_write_can_settle_the_conflict_it_touches(sync: &SyncedRepo, tmp: &TempDir) {
    stand_up_conflict(sync, tmp).await;

    // Without the flag the write lands but the conflict stands, so a
    // client that never looked at it cannot discard the file's value.
    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": "second thoughts"}),
        None,
        false,
    )
    .await
    .expect("update");
    assert_eq!(only_conflict(sync).await.json["name"], "theirs");
    let saved = sync.save_changes().await.expect("save");
    assert_eq!(saved.conflicts.len(), 1, "still skipped: {saved:?}");
    assert_eq!(dashboard_on_disk(tmp)["name"], "theirs");

    // With it, the same write settles the record.
    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": "decided"}),
        None,
        true,
    )
    .await
    .expect("update");
    assert!(sync.list_conflicts(None).await.expect("list").is_empty());
    let saved = sync.save_changes().await.expect("save");
    assert!(saved.conflicts.is_empty(), "{saved:?}");
    assert_eq!(dashboard_on_disk(tmp)["name"], "decided");
}

async fn a_delete_can_settle_the_conflict_it_touches(sync: &SyncedRepo, tmp: &TempDir) {
    stand_up_conflict(sync, tmp).await;
    sync.delete_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        None,
        true,
    )
    .await
    .expect("delete");

    assert!(sync.list_conflicts(None).await.expect("list").is_empty());
    sync.save_changes().await.expect("save");
    let doc: serde_json::Value = serde_saphyr::from_str(
        &std::fs::read_to_string(tmp.path().join("cloudmap.yaml")).expect("read"),
    )
    .expect("yaml");
    assert!(doc["repositories"].get(DASHBOARD).is_none(), "{doc:?}");
}

async fn resolving_a_record_with_no_conflict_is_a_no_op(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": "ours"}),
        None,
        true,
    )
    .await
    .expect("a write may always claim to settle; there is simply nothing to settle");
    assert!(sync.list_conflicts(None).await.expect("list").is_empty());
}

async fn a_batch_settles_only_the_ops_that_ask(sync: &SyncedRepo, tmp: &TempDir) {
    // Two conflicts, so the batch can settle one and leave the other.
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    const STD: &str = "git://unfurl.cloud/onecommons/std.git";
    for key in [DASHBOARD, STD] {
        sync.update_record(
            Some("cloudmap.yaml"),
            "/repositories",
            key,
            serde_json::json!({"name": "ours"}),
            None,
            false,
        )
        .await
        .expect("update");
    }
    rename_name(tmp, "dashboard", "theirs");
    rename_name(tmp, "std", "theirs-too");
    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(scan.conflicts.len(), 2, "{scan:?}");

    let outcome = sync
        .apply_batch(
            vec![
                BatchOp::Upsert {
                    file_path: Some("cloudmap.yaml".into()),
                    path: "/repositories".into(),
                    key: DASHBOARD.into(),
                    json: serde_json::json!({"name": "settled"}),
                    expected: None,
                    resolve: true,
                },
                BatchOp::Upsert {
                    file_path: Some("cloudmap.yaml".into()),
                    path: "/repositories".into(),
                    key: STD.into(),
                    json: serde_json::json!({"name": "left alone"}),
                    expected: None,
                    resolve: false,
                },
            ],
            true,
            None,
        )
        .await
        .expect("batch");
    assert_eq!(outcome.failed.len(), 0, "{outcome:?}");

    let rows = sync.list_conflicts(None).await.expect("list");
    assert_eq!(rows.len(), 1, "exactly one settled: {rows:?}");
    assert_eq!(rows[0].key, STD);
}

crud_test!(a_write_can_settle_the_conflict_it_touches);
crud_test!(a_delete_can_settle_the_conflict_it_touches);
crud_test!(resolving_a_record_with_no_conflict_is_a_no_op);
crud_test!(a_batch_settles_only_the_ops_that_ask);
