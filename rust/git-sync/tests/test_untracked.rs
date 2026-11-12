// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A handle opened with [`OpenOptions::untracked`] takes in the files its
//! pathspecs match that the index doesn't have (docs/untracked-and-gitless.md).

use std::path::Path;

use serde_json::json;
use tempfile::TempDir;
use unfurl_git_sync::{git, DbConfig, Error, FormatRegistry, OpenOptions, ScanOptions, SyncedRepo};

const NEW: &str = "maps/new.yaml";
const OUTSIDE: &str = "outside.yaml";
const REPOS: &str = "/repositories";
const A: &str = "git://e.com/a.git";

fn cloudmap(path: &str) -> String {
    format!("apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  {A}:\n    path: {path}\n")
}

/// A repository with one commit, and `maps/new.yaml` and `outside.yaml`
/// on disk, in no commit and not in the index.
fn repo() -> (TempDir, String) {
    let tmp = tempfile::tempdir().expect("tempdir");
    git::init_with_files(
        tmp.path(),
        &[("maps/old.yaml".to_string(), cloudmap("old").into_bytes())],
        "initial",
    )
    .expect("init repo");
    std::fs::write(tmp.path().join(NEW), cloudmap("new")).expect("write");
    std::fs::write(tmp.path().join(OUTSIDE), cloudmap("outside")).expect("write");
    let db = format!("sqlite://{}?mode=rwc", tmp.path().join("sync.db").display());
    (tmp, db)
}

async fn open(path: &Path, db: &str, options: OpenOptions) -> unfurl_git_sync::Result<SyncedRepo> {
    SyncedRepo::open_with(
        path,
        DbConfig::Sqlite { url: db.into() },
        FormatRegistry::with_builtins(),
        git::Trust::Full,
        options,
    )
    .await
}

async fn open_untracked(path: &Path, db: &str) -> SyncedRepo {
    open(
        path,
        db,
        OpenOptions::default().pathspecs(["maps/"]).untracked(true),
    )
    .await
    .expect("open")
}

async fn scan(sync: &SyncedRepo) -> unfurl_git_sync::SyncOutcome {
    let outcome = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert!(outcome.conflicts.is_empty(), "{:?}", outcome.conflicts);
    outcome
}

async fn path_in(sync: &SyncedRepo, file: &str) -> Option<serde_json::Value> {
    sync.get_record(file, REPOS, A)
        .await
        .expect("get")
        .map(|r| r.json["path"].clone())
}

#[tokio::test]
async fn untracked_needs_pathspecs() {
    let (tmp, db) = repo();
    let opened = open(tmp.path(), &db, OpenOptions::default().untracked(true)).await;
    assert!(
        matches!(opened, Err(Error::InvalidPathspec { .. })),
        "{:?}",
        opened.err()
    );
}

/// Only the files the pathspecs match, and only with the option.
#[tokio::test]
async fn an_untracked_file_is_taken_in() {
    let (tmp, db) = repo();
    let tracked_only = open(tmp.path(), &db, OpenOptions::default().pathspecs(["maps/"]))
        .await
        .expect("open");
    scan(&tracked_only).await;
    assert_eq!(path_in(&tracked_only, NEW).await, None);
    drop(tracked_only);

    let sync = open_untracked(tmp.path(), &db).await;
    scan(&sync).await;
    let r = sync
        .get_record(NEW, REPOS, A)
        .await
        .expect("get")
        .expect("record");
    assert_eq!(r.json["path"], json!("new"));
    assert_eq!(r.commit_id, None);
    assert_eq!(path_in(&sync, "maps/old.yaml").await, Some(json!("old")));
    assert_eq!(path_in(&sync, OUTSIDE).await, None);
}

/// A hand edit to an untracked file is taken in; deleting it drops its
/// records.
#[tokio::test]
async fn an_untracked_file_follows_the_disk() {
    let (tmp, db) = repo();
    let sync = open_untracked(tmp.path(), &db).await;
    scan(&sync).await;
    std::fs::write(tmp.path().join(NEW), cloudmap("hand-edited")).expect("write");
    scan(&sync).await;
    assert_eq!(path_in(&sync, NEW).await, Some(json!("hand-edited")));

    std::fs::remove_file(tmp.path().join(NEW)).expect("rm");
    let outcome = scan(&sync).await;
    assert_eq!(outcome.files_deleted, 1, "{outcome:?}");
    assert_eq!(path_in(&sync, NEW).await, None);
    let saved = sync.save_changes().await.expect("save");
    assert!(saved.written.is_empty(), "{:?}", saved.written);
    assert!(!tmp.path().join(NEW).exists());
}

/// A file `save_changes` creates isn't in the index, and isn't read as
/// deleted by the next scan.
#[tokio::test]
async fn a_file_save_created_survives_a_scan() {
    let (tmp, db) = repo();
    let sync = open_untracked(tmp.path(), &db).await;
    scan(&sync).await;
    const CREATED: &str = "maps/created.yaml";
    sync.upsert_record(
        Some(CREATED),
        REPOS,
        A,
        json!({"path": "created"}),
        None,
        false,
    )
    .await
    .expect("upsert");
    let saved = sync.save_changes().await.expect("save");
    assert!(tmp.path().join(CREATED).exists(), "{saved:?}");
    let outcome = scan(&sync).await;
    assert_eq!(outcome.files_deleted, 0, "{outcome:?}");
    assert_eq!(path_in(&sync, CREATED).await, Some(json!("created")));
}

/// A commit through the handle carries an untracked file, which then
/// holds the commit.
#[tokio::test]
async fn a_commit_carries_an_untracked_file() {
    let (tmp, db) = repo();
    let sync = open_untracked(tmp.path(), &db).await;
    scan(&sync).await;
    let committed = sync
        .commit_repository("add new", Default::default())
        .await
        .expect("commit");
    let commit = committed.commit.expect("a commit");
    let r = sync
        .get_record(NEW, REPOS, A)
        .await
        .expect("get")
        .expect("record");
    assert_eq!(r.commit_id, Some(commit));
    let repo = gix::open(tmp.path()).expect("repo");
    assert!(git::tree_blobs(&repo, commit)
        .expect("tree")
        .contains_key(NEW));
    scan(&sync).await;
    assert_eq!(path_in(&sync, NEW).await, Some(json!("new")));
}

/// Another repository's files and `.git` aren't walked.
#[tokio::test]
async fn a_nested_repository_is_skipped() {
    let (tmp, db) = repo();
    let nested = tmp.path().join("maps/nested");
    git::init_with_files(
        &nested,
        &[("inner.yaml".to_string(), cloudmap("inner").into_bytes())],
        "inner",
    )
    .expect("nested repo");
    let sync = open_untracked(tmp.path(), &db).await;
    scan(&sync).await;
    assert_eq!(path_in(&sync, "maps/nested/inner.yaml").await, None);
    assert_eq!(path_in(&sync, NEW).await, Some(json!("new")));
}

/// A repository with no commit and no index yet has only untracked files.
#[tokio::test]
async fn an_untracked_file_on_an_unborn_branch_is_taken_in() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let out = std::process::Command::new("git")
        .args(["init", "-q", "-b", "main"])
        .current_dir(tmp.path())
        .output()
        .expect("git");
    assert!(out.status.success(), "{out:?}");
    std::fs::create_dir(tmp.path().join("maps")).expect("mkdir");
    std::fs::write(tmp.path().join(NEW), cloudmap("new")).expect("write");
    let db = format!("sqlite://{}?mode=rwc", tmp.path().join("sync.db").display());
    let sync = open_untracked(tmp.path(), &db).await;
    scan(&sync).await;
    assert_eq!(path_in(&sync, NEW).await, Some(json!("new")));
}

/// A committed file taken out of the index and edited is untracked: the
/// records it changes hold no commit, and a hand edit after is taken in.
#[tokio::test]
async fn a_file_taken_out_of_the_index_holds_no_commit() {
    let (tmp, db) = repo();
    let sync = open_untracked(tmp.path(), &db).await;
    scan(&sync).await;
    let old = "maps/old.yaml";
    let r = sync
        .get_record(old, REPOS, A)
        .await
        .expect("get")
        .expect("record");
    assert!(r.commit_id.is_some());

    let out = std::process::Command::new("git")
        .args(["rm", "-q", "--cached", old])
        .current_dir(tmp.path())
        .output()
        .expect("git");
    assert!(out.status.success(), "{out:?}");
    std::fs::write(tmp.path().join(old), cloudmap("edited")).expect("write");
    scan(&sync).await;
    let r = sync
        .get_record(old, REPOS, A)
        .await
        .expect("get")
        .expect("record");
    assert_eq!(r.json["path"], json!("edited"));
    assert_eq!(r.commit_id, None);

    std::fs::write(tmp.path().join(old), cloudmap("by-hand")).expect("write");
    scan(&sync).await;
    assert_eq!(path_in(&sync, old).await, Some(json!("by-hand")));
}

fn git_in(dir: &Path, args: &[&str]) -> String {
    let out = std::process::Command::new("git")
        .args(["-c", "user.name=t", "-c", "user.email=t@e"])
        .args(args)
        .current_dir(dir)
        .output()
        .expect("git");
    assert!(out.status.success(), "{out:?}");
    String::from_utf8(out.stdout).expect("utf-8")
}

/// Save an edit to `file`'s record, hand-edit the same record in the file,
/// and rescan: the conflicts the rescan reports.
async fn save_then_hand_edit(
    sync: &SyncedRepo,
    root: &Path,
    file: &str,
) -> Vec<unfurl_git_sync::RecordConflict> {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    sync.upsert_record(Some(file), REPOS, A, json!({"path": "saved"}), None, false)
        .await
        .expect("upsert");
    let saved = sync.save_changes().await.expect("save");
    assert!(
        saved.failed.is_empty() && saved.conflicts.is_empty(),
        "{saved:?}"
    );
    std::fs::write(root.join(file), cloudmap("by-hand")).expect("write");
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan")
        .conflicts
}

/// A save settles what it writes to a file the index doesn't have: a hand
/// edit of the same record after it is taken in.
#[tokio::test]
async fn a_save_settles_an_untracked_file() {
    let (tmp, db) = repo();
    let sync = open_untracked(tmp.path(), &db).await;
    let conflicts = save_then_hand_edit(&sync, tmp.path(), NEW).await;
    assert!(conflicts.is_empty(), "{conflicts:?}");
    assert_eq!(path_in(&sync, NEW).await, Some(json!("by-hand")));
}

/// The same for an ignored file a literal pathspec names.
#[tokio::test]
async fn a_save_settles_an_ignored_file() {
    let (tmp, db) = repo();
    std::fs::write(tmp.path().join(".gitignore"), "new.yaml\n").expect("write");
    std::fs::rename(tmp.path().join(NEW), tmp.path().join("new.yaml")).expect("mv");
    let sync = open(
        tmp.path(),
        &db,
        OpenOptions::default()
            .pathspecs([":(literal)new.yaml"])
            .untracked(true),
    )
    .await
    .expect("open");
    let conflicts = save_then_hand_edit(&sync, tmp.path(), "new.yaml").await;
    assert!(conflicts.is_empty(), "{conflicts:?}");
    assert_eq!(path_in(&sync, "new.yaml").await, Some(json!("by-hand")));
}

/// A staged file's saved edit stays pending until a commit: a hand edit of
/// the same record conflicts with it.
#[tokio::test]
async fn a_save_leaves_a_staged_files_edit_pending() {
    let (tmp, db) = repo();
    git_in(tmp.path(), &["add", NEW]);
    let sync = open_untracked(tmp.path(), &db).await;
    let conflicts = save_then_hand_edit(&sync, tmp.path(), NEW).await;
    assert_eq!(conflicts.len(), 1, "{conflicts:?}");
}

/// After a settled save, a commit still carries the file and its records
/// take the commit; after `git add`, a hand edit is still taken in.
#[tokio::test]
async fn a_settled_untracked_file_commits() {
    let (tmp, db) = repo();
    let sync = open_untracked(tmp.path(), &db).await;
    let conflicts = save_then_hand_edit(&sync, tmp.path(), NEW).await;
    assert!(conflicts.is_empty(), "{conflicts:?}");

    git_in(tmp.path(), &["add", NEW]);
    std::fs::write(tmp.path().join(NEW), cloudmap("staged-hand")).expect("write");
    let scanned = scan(&sync).await;
    assert!(scanned.conflicts.is_empty(), "{scanned:?}");
    assert_eq!(path_in(&sync, NEW).await, Some(json!("staged-hand")));

    let committed = sync
        .commit_repository("add new", Default::default())
        .await
        .expect("commit");
    let commit = committed.commit.expect("a commit");
    assert_eq!(
        git_in(tmp.path(), &["rev-parse", "HEAD"]).trim(),
        commit.to_string()
    );
    assert!(git_in(tmp.path(), &["ls-tree", "-r", "HEAD"]).contains(NEW));
    let r = sync
        .get_record(NEW, REPOS, A)
        .await
        .expect("get")
        .expect("record");
    assert_eq!(r.commit_id, Some(commit));
    assert_eq!(git_in(tmp.path(), &["status", "--porcelain", NEW]), "");
}

/// A handle that doesn't take in untracked files leaves a new file's saved
/// edits pending: a rescan, which doesn't see the file, keeps them.
#[tokio::test]
async fn a_save_without_untracked_stays_pending() {
    let (tmp, db) = repo();
    let sync = open(tmp.path(), &db, OpenOptions::default().pathspecs(["maps/"]))
        .await
        .expect("open");
    scan(&sync).await;
    let created = "maps/created.yaml";
    sync.upsert_record(
        Some(created),
        REPOS,
        A,
        json!({"path": "created"}),
        None,
        false,
    )
    .await
    .expect("upsert");
    let saved = sync.save_changes().await.expect("save");
    assert!(saved.failed.is_empty(), "{saved:?}");
    assert!(tmp.path().join(created).exists());
    scan(&sync).await;
    assert_eq!(path_in(&sync, created).await, Some(json!("created")));
}
