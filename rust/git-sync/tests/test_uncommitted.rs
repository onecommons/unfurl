// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Files no commit holds: staged on a branch with commits, and every file
//! of an unborn branch (docs/untracked-and-gitless.md).

mod common;

use common::git;
use serde_json::json;
use tempfile::TempDir;
use unfurl_git_sync::{DbConfig, FormatRegistry, RecordQuery, Resolution, ScanOptions, SyncedRepo};

const FILE: &str = "staged.yaml";
const REPOS: &str = "/repositories";
const A: &str = "git://e.com/a.git";

fn cloudmap(path: &str) -> String {
    format!("apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  {A}:\n    path: {path}\n")
}

#[derive(Clone, Copy)]
enum Setup {
    /// Staged on a branch that has a commit.
    Staged,
    /// Staged on a branch with no commit yet.
    Unborn,
}

async fn staged(setup: Setup) -> (SyncedRepo, TempDir) {
    let tmp = tempfile::tempdir().expect("tempdir");
    match setup {
        Setup::Staged => {
            common::init_repo_with_fixture(tmp.path()).await;
        }
        Setup::Unborn => git(tmp.path(), &["init", "-q", "-b", "main"]),
    }
    std::fs::write(tmp.path().join(FILE), cloudmap("a")).expect("write");
    git(tmp.path(), &["add", FILE]);
    let sync = SyncedRepo::open(
        tmp.path(),
        DbConfig::Sqlite {
            url: "sqlite::memory:".into(),
        },
        FormatRegistry::with_builtins(),
    )
    .await
    .expect("open");
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    (sync, tmp)
}

async fn path_of(sync: &SyncedRepo) -> Option<serde_json::Value> {
    sync.get_record(FILE, REPOS, A)
        .await
        .expect("get")
        .map(|r| r.json["path"].clone())
}

/// Every record of the file, and every conflict, holds a commit.
async fn assert_all_committed(sync: &SyncedRepo) {
    let query = RecordQuery::default().file_path(FILE);
    let records = sync.find_records(&query).await.expect("find");
    assert!(!records.is_empty());
    for r in records
        .iter()
        .chain(&sync.list_conflicts(None).await.expect("conflicts"))
    {
        assert!(r.commit_id.is_some(), "{r:?} holds no commit");
    }
}

/// A file no commit holds is taken in as the file's, not as an edit: a
/// hand edit to it is taken in on the next scan, without a conflict.
async fn a_hand_edit_is_taken_in(setup: Setup) {
    let (sync, tmp) = staged(setup).await;
    let r = sync
        .get_record(FILE, REPOS, A)
        .await
        .expect("get")
        .expect("record");
    assert_eq!(r.commit_id, None);

    std::fs::write(tmp.path().join(FILE), cloudmap("hand-edited")).expect("write");
    let scanned = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert!(scanned.conflicts.is_empty(), "{:?}", scanned.conflicts);
    assert_eq!(path_of(&sync).await, Some(json!("hand-edited")));

    let saved = sync.save_changes().await.expect("save");
    assert!(saved.conflicts.is_empty(), "{:?}", saved.conflicts);
    assert!(saved.written.is_empty(), "{:?}", saved.written);
}

#[tokio::test]
async fn a_hand_edit_to_a_staged_file_is_taken_in() {
    a_hand_edit_is_taken_in(Setup::Staged).await;
}

#[tokio::test]
async fn a_hand_edit_on_an_unborn_branch_is_taken_in() {
    a_hand_edit_is_taken_in(Setup::Unborn).await;
}

/// A commit through the handle replaces the marker with itself.
async fn a_commit_replaces_the_marker(setup: Setup) {
    let (sync, _tmp) = staged(setup).await;
    sync.upsert_record(
        Some(FILE),
        REPOS,
        "git://e.com/b.git",
        json!({"path": "b"}),
        None,
        false,
    )
    .await
    .expect("upsert");
    let committed = sync
        .commit_repository("add b", Default::default())
        .await
        .expect("commit");
    let commit = committed.commit.expect("a commit");
    assert_all_committed(&sync).await;
    let r = sync
        .get_record(FILE, REPOS, A)
        .await
        .expect("get")
        .expect("record");
    assert_eq!(r.commit_id, Some(commit));
}

#[tokio::test]
async fn a_commit_replaces_the_marker_of_a_staged_file() {
    a_commit_replaces_the_marker(Setup::Staged).await;
}

#[tokio::test]
async fn a_commit_replaces_the_marker_on_an_unborn_branch() {
    a_commit_replaces_the_marker(Setup::Unborn).await;
}

/// A commit made outside the handle replaces the marker on the next scan.
async fn an_outside_commit_replaces_the_marker(setup: Setup) {
    let (sync, tmp) = staged(setup).await;
    git(
        tmp.path(),
        &[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e",
            "commit",
            "-qm",
            "outside",
        ],
    );
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert_all_committed(&sync).await;
    assert_eq!(path_of(&sync).await, Some(json!("a")));
}

#[tokio::test]
async fn an_outside_commit_replaces_the_marker_of_a_staged_file() {
    an_outside_commit_replaces_the_marker(Setup::Staged).await;
}

#[tokio::test]
async fn an_outside_commit_replaces_the_marker_on_an_unborn_branch() {
    an_outside_commit_replaces_the_marker(Setup::Unborn).await;
}

/// A staged file deleted from disk loses its records; recreated, it has
/// them again, as the file's.
async fn a_deleted_file_comes_back(setup: Setup) {
    let (sync, tmp) = staged(setup).await;
    std::fs::remove_file(tmp.path().join(FILE)).expect("rm");
    let gone = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert!(gone.conflicts.is_empty(), "{:?}", gone.conflicts);
    assert_eq!(path_of(&sync).await, None);

    std::fs::write(tmp.path().join(FILE), cloudmap("back")).expect("write");
    let back = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert!(back.conflicts.is_empty(), "{:?}", back.conflicts);
    assert_eq!(path_of(&sync).await, Some(json!("back")));
    let r = sync
        .get_record(FILE, REPOS, A)
        .await
        .expect("get")
        .expect("record");
    assert_eq!(r.commit_id, None);
}

#[tokio::test]
async fn a_deleted_staged_file_comes_back() {
    a_deleted_file_comes_back(Setup::Staged).await;
}

#[tokio::test]
async fn a_deleted_file_on_an_unborn_branch_comes_back() {
    a_deleted_file_comes_back(Setup::Unborn).await;
}

/// Resolving a conflict to the file's value on an unborn branch takes
/// that value in as the file's, not as an edit.
#[tokio::test]
async fn theirs_on_an_unborn_branch_is_the_files() {
    let (sync, tmp) = staged(Setup::Unborn).await;
    sync.update_record(Some(FILE), REPOS, A, json!({"path": "ours"}), None, false)
        .await
        .expect("update");
    std::fs::write(tmp.path().join(FILE), cloudmap("theirs")).expect("write");
    let scanned = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert_eq!(scanned.conflicts.len(), 1, "{scanned:?}");
    sync.resolve_conflict(FILE, REPOS, A, Resolution::Theirs, None)
        .await
        .expect("resolve");
    let r = sync
        .get_record(FILE, REPOS, A)
        .await
        .expect("get")
        .expect("record");
    assert_eq!(r.json["path"], json!("theirs"));
    assert_eq!(r.commit_id, None);
    let saved = sync.save_changes().await.expect("save");
    assert!(saved.written.is_empty(), "{:?}", saved.written);
    // the file's, not an edit: a hand edit to it is taken in
    std::fs::write(tmp.path().join(FILE), cloudmap("by-hand")).expect("write");
    let scanned = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert!(scanned.conflicts.is_empty(), "{:?}", scanned.conflicts);
    assert_eq!(path_of(&sync).await, Some(json!("by-hand")));
}

const B: &str = "git://e.com/b.git";

fn two(a: &str, b: &str) -> String {
    format!(
        "apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  {A}:\n    path: {a}\n  {B}:\n    path: {b}\n"
    )
}

/// An edit to one record of a file no commit holds survives a hand edit
/// to another record of it: by a scan, or by a save.
async fn an_edit_survives_a_hand_edit_elsewhere(save: bool) {
    let (sync, tmp) = staged(Setup::Unborn).await;
    std::fs::write(tmp.path().join(FILE), two("a", "b")).expect("write");
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    sync.update_record(Some(FILE), REPOS, A, json!({"path": "ours"}), None, false)
        .await
        .expect("update");
    std::fs::write(tmp.path().join(FILE), two("a", "hand")).expect("write");
    let conflicts = match save {
        false => {
            sync.update_from_working_dir(ScanOptions::default())
                .await
                .expect("scan")
                .conflicts
        }
        true => sync.save_changes().await.expect("save").conflicts,
    };
    assert!(conflicts.is_empty(), "{conflicts:?}");
    let saved = sync.save_changes().await.expect("save");
    assert!(saved.conflicts.is_empty(), "{:?}", saved.conflicts);
    let on_disk = std::fs::read_to_string(tmp.path().join(FILE)).expect("read");
    assert_eq!(on_disk, two("ours", "hand"));
}

#[tokio::test]
async fn an_edit_survives_a_scanned_hand_edit_elsewhere() {
    an_edit_survives_a_hand_edit_elsewhere(false).await;
}

#[tokio::test]
async fn an_edit_survives_a_saved_hand_edit_elsewhere() {
    an_edit_survives_a_hand_edit_elsewhere(true).await;
}
