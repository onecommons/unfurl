// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Exporting a worktree's conflicts to a branch (docs/branch-segments.md
//! §4.15, C.20).

mod common;

use common::{crud_test, dashboard_on_disk, head_commit, stand_up_conflict, upsert_op, DASHBOARD};
use tempfile::TempDir;
use unfurl_git_sync::{
    parse_commit_rollup, BatchOp, CommitOptions, Error, ScanOptions, SyncedRepo, TxnMeta,
};

const BRANCH: &str = "conflicts";

/// `git` in the fixture's repository, with an identity: its output, and
/// whether it succeeded.
fn git(tmp: &TempDir, args: &[&str]) -> (bool, String) {
    let out = std::process::Command::new("git")
        .args(["-c", "user.name=t", "-c", "user.email=t@example.com"])
        .args(["-c", "commit.gpgsign=false"])
        .args(args)
        .current_dir(tmp.path())
        .output()
        .expect("git");
    let text = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (out.status.success(), text)
}

fn git_ok(tmp: &TempDir, args: &[&str]) -> String {
    let (ok, out) = git(tmp, args);
    assert!(ok, "git {args:?} failed: {out}");
    out
}

/// The dashboard record's `name` in `file` at `commit`.
fn name_at(tmp: &TempDir, commit: &str, file: &str) -> serde_json::Value {
    let text = git_ok(tmp, &["show", &format!("{commit}:{file}")]);
    let doc: serde_json::Value = serde_saphyr::from_str(&text).expect("yaml");
    doc["repositories"][DASHBOARD]["name"].clone()
}

async fn name_of(sync: &SyncedRepo, key: &str) -> serde_json::Value {
    let record = sync
        .get_record("cloudmap.yaml", "/repositories", key)
        .await
        .expect("get")
        .expect("present");
    record.json["name"].clone()
}

fn meta(message: &str) -> Option<TxnMeta> {
    Some(TxnMeta {
        author: Some("Ada <ada@example.com>".into()),
        message: Some(message.into()),
    })
}

/// The edit under the conflict moves to the branch, committed onto its
/// base; the worktree shows the file's value at once and keeps its other
/// edits.
async fn an_export_moves_the_conflicted_edit(sync: &SyncedRepo, tmp: &TempDir) {
    let base = head_commit(sync).await;
    stand_up_conflict(sync, tmp).await;
    sync.apply_batch(vec![upsert_op("other")], true, None)
        .await
        .expect("an edit that doesn't conflict");

    let exported = sync
        .export_conflicts(BRANCH)
        .await
        .expect("export")
        .expect("a conflict to export");

    assert!(sync
        .list_conflicts(None)
        .await
        .expect("conflicts")
        .is_empty());
    assert_eq!(
        name_of(sync, DASHBOARD).await,
        "theirs",
        "no scan or commit needed"
    );
    let other = sync
        .get_record("cloudmap.yaml", "/repositories", "other")
        .await
        .expect("get")
        .expect("still here");
    assert!(other.commit_id.is_none(), "still pending: {other:?}");

    assert_eq!(exported.branch, BRANCH);
    assert_eq!(
        git_ok(tmp, &["rev-parse", &format!("refs/heads/{BRANCH}")]),
        exported.commit
    );
    assert_eq!(
        git_ok(tmp, &["rev-parse", &format!("{}^", exported.commit)]),
        base
    );
    assert_eq!(name_at(tmp, &exported.commit, "cloudmap.yaml"), "ours");
    assert!(
        exported.records.iter().any(|r| r.key == DASHBOARD),
        "{:?}",
        exported.records
    );
    assert_eq!(
        dashboard_on_disk(tmp)["name"],
        "theirs",
        "the checkout is untouched"
    );
    assert_eq!(head_commit(sync).await, base, "HEAD didn't move");
}

/// Nothing in conflict: nothing exported, and no branch.
async fn no_conflicts_export_nothing(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    sync.apply_batch(vec![upsert_op("pending")], true, None)
        .await
        .expect("edit");
    assert!(sync
        .export_conflicts(BRANCH)
        .await
        .expect("export")
        .is_none());
    let (exists, _) = git(
        tmp,
        &["rev-parse", "--verify", &format!("refs/heads/{BRANCH}")],
    );
    assert!(!exists, "no branch made");
}

/// A branch of that name already: nothing changes.
async fn an_existing_branch_is_refused(sync: &SyncedRepo, tmp: &TempDir) {
    stand_up_conflict(sync, tmp).await;
    git_ok(tmp, &["branch", BRANCH]);
    let err = sync.export_conflicts(BRANCH).await.expect_err("taken");
    assert!(
        matches!(&err, Error::BranchExists { branch } if branch == BRANCH),
        "{err:?}"
    );
    assert_eq!(sync.list_conflicts(None).await.expect("conflicts").len(), 1);
    assert_eq!(name_of(sync, DASHBOARD).await, "ours");
}

/// Merging the branch back conflicts in git where the records did, and
/// once that's resolved the worktree takes the merge in with no record
/// conflict left.
async fn merging_the_branch_back_conflicts_in_git(sync: &SyncedRepo, tmp: &TempDir) {
    stand_up_conflict(sync, tmp).await;
    sync.export_conflicts(BRANCH)
        .await
        .expect("export")
        .expect("exported");
    sync.commit_repository("carry the hand edit", CommitOptions::default())
        .await
        .expect("commit")
        .commit
        .expect("the hand edit is new");

    let (merged, _) = git(tmp, &["merge", "--no-edit", BRANCH]);
    assert!(!merged, "the merge conflicts");
    let (_, unmerged) = git(tmp, &["diff", "--name-only", "--diff-filter=U"]);
    assert_eq!(unmerged, "cloudmap.yaml");
    git_ok(tmp, &["checkout", "--theirs", "cloudmap.yaml"]);
    git_ok(tmp, &["add", "cloudmap.yaml"]);
    git_ok(tmp, &["commit", "--no-edit"]);

    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert!(sync
        .list_conflicts(None)
        .await
        .expect("conflicts")
        .is_empty());
    assert_eq!(
        name_of(sync, DASHBOARD).await,
        "ours",
        "the merge took the branch's side"
    );
}

/// A batch with records on both sides of the export is named by both
/// commits' rollups, each with its share; one wholly exported is named
/// only by the branch's.
async fn batches_follow_their_records(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let ours = BatchOp::Upsert {
        file_path: Some("cloudmap.yaml".into()),
        path: "/repositories".into(),
        key: DASHBOARD.into(),
        json: serde_json::json!({"name": "ours"}),
        expected: None,
        resolve: false,
    };
    sync.apply_batch(vec![ours, upsert_op("split")], true, meta("split batch"))
        .await
        .expect("split batch");
    common::rename_name(tmp, "dashboard", "theirs");
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert_eq!(sync.list_conflicts(None).await.expect("conflicts").len(), 1);

    let committed = sync
        .commit_repository(
            "commit the rest",
            CommitOptions {
                conflicts_to_branch: Some(BRANCH.into()),
            },
        )
        .await
        .expect("commit");
    let exported = committed.exported.expect("exported");
    let main = committed.commit.expect("committed");

    let keys = |commit: &str| -> Vec<(String, Vec<String>)> {
        let message = git_ok(tmp, &["log", "-1", "--format=%B", commit]);
        let rollup = parse_commit_rollup(&message).unwrap().expect("a rollup");
        rollup
            .txns
            .iter()
            .map(|t| {
                let keys = t.records.iter().map(|r| r.key.clone()).collect();
                (t.meta.message.clone().unwrap_or_default(), keys)
            })
            .collect()
    };
    assert_eq!(
        keys(&exported.commit),
        vec![("split batch".to_string(), vec![DASHBOARD.to_string()])]
    );
    assert_eq!(
        keys(&main),
        vec![("split batch".to_string(), vec!["split".to_string()])]
    );
    assert_eq!(name_at(tmp, &main, "cloudmap.yaml"), "theirs");
    assert!(sync
        .list_conflicts(None)
        .await
        .expect("conflicts")
        .is_empty());
}

/// A batch whose records all went is the branch's alone: the worktree's
/// next commit doesn't name it.
async fn a_wholly_exported_batch_leaves(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let ours = BatchOp::Upsert {
        file_path: Some("cloudmap.yaml".into()),
        path: "/repositories".into(),
        key: DASHBOARD.into(),
        json: serde_json::json!({"name": "ours"}),
        expected: None,
        resolve: false,
    };
    sync.apply_batch(vec![ours], true, meta("exported batch"))
        .await
        .expect("batch");
    common::rename_name(tmp, "dashboard", "theirs");
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    sync.export_conflicts(BRANCH)
        .await
        .expect("export")
        .expect("exported");
    sync.apply_batch(vec![upsert_op("later")], true, meta("later batch"))
        .await
        .expect("later");
    let main = sync
        .commit_repository("later", CommitOptions::default())
        .await
        .expect("commit")
        .commit
        .expect("committed");
    let message = git_ok(tmp, &["log", "-1", "--format=%B", &main]);
    let rollup = parse_commit_rollup(&message).unwrap().expect("a rollup");
    let messages: Vec<Option<String>> =
        rollup.txns.iter().map(|t| t.meta.message.clone()).collect();
    assert_eq!(messages, vec![Some("later batch".to_string())], "{message}");
}

/// Main moved the record to another file since the edit was made: the
/// branch, forked before the move, has the record where it was, so the
/// edit is a new record there, and the branch holds both.
async fn an_edit_whose_record_moved_is_a_new_record(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    // a commit git-sync makes names the record's id in its rollup, which is
    // what the fork at it takes the id from
    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": "base"}),
        None,
        false,
    )
    .await
    .expect("edit");
    let base = sync
        .commit_repository("base", CommitOptions::default())
        .await
        .expect("commit")
        .commit
        .expect("committed");
    let edit = sync
        .update_record(
            Some("cloudmap.yaml"),
            "/repositories",
            DASHBOARD,
            serde_json::json!({"name": "ours"}),
            None,
            false,
        )
        .await
        .expect("edit");

    // a commit made elsewhere moves the record to moved.yaml, renamed
    let path = tmp.path().join("cloudmap.yaml");
    let mut doc: serde_json::Value =
        serde_saphyr::from_str(&std::fs::read_to_string(&path).expect("read")).expect("yaml");
    let mut moved = doc["repositories"]
        .as_object_mut()
        .expect("repositories")
        .remove(DASHBOARD)
        .expect("there");
    std::fs::write(&path, serde_saphyr::to_string(&doc).expect("emit")).expect("write");
    moved["name"] = "theirs".into();
    let other = serde_json::json!({
        "apiVersion": doc["apiVersion"],
        "kind": doc["kind"],
        "repositories": { DASHBOARD: moved },
    });
    std::fs::write(
        tmp.path().join("moved.yaml"),
        serde_saphyr::to_string(&other).expect("emit"),
    )
    .expect("write");
    git_ok(tmp, &["add", "cloudmap.yaml", "moved.yaml"]);
    git_ok(tmp, &["commit", "-q", "-m", "move the dashboard"]);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let conflicts = sync.list_conflicts(None).await.expect("conflicts");
    assert_eq!(conflicts.len(), 1, "{conflicts:?}");
    assert_eq!(
        conflicts[0].file_path, "moved.yaml",
        "the edit followed its record"
    );

    let exported = sync
        .export_conflicts(BRANCH)
        .await
        .expect("export")
        .expect("exported");
    let commit = &exported.commit;
    assert_eq!(git_ok(tmp, &["rev-parse", &format!("{commit}^")]), base);
    assert_eq!(
        name_at(tmp, commit, "cloudmap.yaml"),
        "base",
        "the record where the base has it"
    );
    assert_eq!(name_at(tmp, commit, "moved.yaml"), "ours");
    let record = exported
        .records
        .iter()
        .find(|r| r.file_path.as_deref() == Some("moved.yaml"))
        .expect("the moved file's record");
    assert_ne!(record.key_id, Some(edit.id), "a new record on the branch");
}

/// Edits made before HEAD moved fork from the commit they were made on.
async fn the_base_is_where_the_edits_were_made(sync: &SyncedRepo, tmp: &TempDir) {
    let base = head_commit(sync).await;
    stand_up_conflict(sync, tmp).await;
    std::fs::write(tmp.path().join("notes.txt"), "later\n").expect("write");
    git_ok(tmp, &["add", "notes.txt"]);
    git_ok(tmp, &["commit", "-q", "-m", "later"]);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert_ne!(head_commit(sync).await, base);
    let exported = sync
        .export_conflicts(BRANCH)
        .await
        .expect("export")
        .expect("exported");
    assert_eq!(
        git_ok(tmp, &["rev-parse", &format!("{}^", exported.commit)]),
        base
    );
}

crud_test!(an_export_moves_the_conflicted_edit);
crud_test!(an_edit_whose_record_moved_is_a_new_record);
crud_test!(the_base_is_where_the_edits_were_made);
crud_test!(no_conflicts_export_nothing);
crud_test!(an_existing_branch_is_refused);
crud_test!(merging_the_branch_back_conflicts_in_git);
crud_test!(batches_follow_their_records);
crud_test!(a_wholly_exported_batch_leaves);
