// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Committing a handle's saved changes the way Unfurl's `CloudMap` will:
//! through `commit_repository`, including after someone committed the saved
//! file with git first.

use std::path::Path;
use std::process::Command;

use tempfile::TempDir;
use unfurl_git_sync::{
    git, CommitOptions, DbConfig, FormatRegistry, OpenOptions, ScanOptions, SyncedRepo,
};

const CLOUDMAP: &str = "cloudmap.yaml";
const KEPT: &str = "git://example.com/a.git";
const ADDED: &str = "git://example.com/b.git";

fn cloudmap() -> Vec<u8> {
    format!("apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  {KEPT}:\n    path: p\n")
        .into_bytes()
}

fn run_git(dir: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .args(["-c", "user.name=t", "-c", "user.email=t@example.com"])
        .args(args)
        .current_dir(dir)
        .output()
        .expect("run git");
    assert!(out.status.success(), "git {args:?}: {out:?}");
    String::from_utf8(out.stdout)
        .expect("utf8")
        .trim()
        .to_string()
}

async fn open(tmp: &TempDir) -> SyncedRepo {
    let db = format!("sqlite://{}?mode=rwc", tmp.path().join("sync.db").display());
    SyncedRepo::open_with(
        tmp.path(),
        DbConfig::Sqlite { url: db },
        FormatRegistry::with_builtins(),
        git::Trust::Full,
        OpenOptions::default().pathspecs([CLOUDMAP]),
    )
    .await
    .expect("open")
}

/// A scoped handle that has saved an added record and a deleted one, not yet
/// committed.
async fn saved_changes() -> (TempDir, SyncedRepo) {
    let tmp = tempfile::tempdir().expect("tempdir");
    git::init_with_files(tmp.path(), &[(CLOUDMAP.to_string(), cloudmap())], "initial")
        .expect("init repo");
    let sync = open(&tmp).await;
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let record = serde_json::json!({"path": "q"});
    sync.upsert_record(Some(CLOUDMAP), "/repositories", ADDED, record, None, false)
        .await
        .expect("upsert");
    sync.delete_record(Some(CLOUDMAP), "/repositories", KEPT, None, false)
        .await
        .expect("delete");
    let saved = sync.save_changes().await.expect("save");
    assert!(saved.conflicts.is_empty(), "{saved:?}");
    (tmp, sync)
}

/// The changes are committed with `head`, and a fresh handle finds nothing
/// in flight.
async fn assert_committed_with(tmp: &TempDir, sync: SyncedRepo, head: &str) {
    let added = sync
        .get_record(CLOUDMAP, "/repositories", ADDED)
        .await
        .expect("get")
        .expect("the added record");
    assert_eq!(
        added.commit_id.map(|c| c.to_string()).as_deref(),
        Some(head)
    );
    let deleted = sync
        .get_record(CLOUDMAP, "/repositories", KEPT)
        .await
        .expect("get");
    assert!(deleted.is_none(), "{deleted:?}");
    drop(sync);
    let reopened = open(tmp).await;
    let outcome = reopened
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert!(outcome.conflicts.is_empty(), "{outcome:?}");
    assert_eq!(outcome.records_upserted, 0, "{outcome:?}");
}

#[tokio::test]
async fn saved_changes_are_committed_through_git_sync() {
    let (tmp, sync) = saved_changes().await;
    let committed = sync
        .commit_repository("update cloudmap", CommitOptions::default())
        .await
        .expect("commit");
    let head = run_git(tmp.path(), &["rev-parse", "HEAD"]);
    assert_eq!(
        committed.commit.map(|c| c.to_string()).as_deref(),
        Some(head.as_str())
    );
    // the caller's message, then git-sync's rollup of what it carries
    let message = run_git(tmp.path(), &["log", "-1", "--format=%B"]);
    assert!(message.starts_with("update cloudmap\n"), "{message}");
    assert_committed_with(&tmp, sync, &head).await;
}

#[tokio::test]
async fn saved_changes_someone_committed_with_git_are_rolled_forward() {
    let (tmp, sync) = saved_changes().await;
    run_git(tmp.path(), &["add", CLOUDMAP]);
    run_git(tmp.path(), &["commit", "-q", "-m", "by hand"]);
    let head = run_git(tmp.path(), &["rev-parse", "HEAD"]);
    // HEAD has the changes already: no new commit, but they're its
    let committed = sync
        .commit_repository("update cloudmap", CommitOptions::default())
        .await
        .expect("commit");
    assert!(committed.commit.is_none(), "{committed:?}");
    assert_eq!(run_git(tmp.path(), &["rev-parse", "HEAD"]), head);
    assert_committed_with(&tmp, sync, &head).await;
}
