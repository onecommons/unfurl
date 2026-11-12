// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A save that adds, removes or edits records changes only those records'
//! lines: the others keep their bytes, comments and quoting included, so
//! git merges two such saves as it would two hand edits.

use std::path::Path;

use serde_json::json;
use unfurl_git_sync::{DbConfig, FormatRegistry, OpenOptions, ScanOptions, SyncedDir};

const FILE: &str = "c.yaml";
const REPOS: &str = "/repositories";

/// A section with a comment and a blank line between records, and keys
/// quoted two ways.
const BASE: &str = "apiVersion: unfurl/v1.0.0
kind: CloudMap
# repositories we track
repositories:
  git://e.com/alpha.git:
    path: alpha  # the first

  # beta is special
  'git://e.com/beta.git':
    path: beta
  \"git://e.com/delta.git\":
    path: delta
  git://e.com/foxtrot.git:
    path: foxtrot
# artifacts next
artifacts:
  pkg:npm/example:
    digest: sha256:0
";

const BETA: &str = "  'git://e.com/beta.git':\n    path: beta\n";

fn block(name: &str) -> String {
    format!("  git://e.com/{name}.git:\n    path: {name}\n")
}

async fn open(root: &Path, db: &Path) -> SyncedDir {
    let url = format!("sqlite://{}?mode=rwc", db.display());
    let sync = SyncedDir::open(
        root,
        DbConfig::Sqlite { url },
        FormatRegistry::with_builtins(),
        unfurl_git_sync::git::Trust::Full,
        OpenOptions::default().pathspecs([FILE]).untracked(true),
    )
    .await
    .expect("open");
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    sync
}

async fn add(sync: &SyncedDir, name: &str) {
    upsert(sync, name, json!({"path": name})).await;
}

async fn upsert(sync: &SyncedDir, name: &str, value: serde_json::Value) {
    let key = format!("git://e.com/{name}.git");
    sync.upsert_record(Some(FILE), REPOS, &key, value, None, false)
        .await
        .expect("upsert");
}

async fn delete(sync: &SyncedDir, key: &str) {
    sync.delete_record(Some(FILE), REPOS, key, None, false)
        .await
        .expect("delete");
}

/// `BASE` (or `base`) changed by `change` and saved: the file's text.
async fn saved(base: &str, change: impl AsyncFn(&SyncedDir)) -> String {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path().join("root");
    std::fs::create_dir(&root).expect("mkdir");
    std::fs::write(root.join(FILE), base).expect("write");
    let sync = open(&root, &tmp.path().join("db")).await;
    change(&sync).await;
    let outcome = sync.save_changes().await.expect("save");
    assert!(outcome.failed.is_empty(), "{outcome:?}");
    std::fs::read_to_string(root.join(FILE)).expect("read")
}

fn inserted_after(text: &str, after: &str, lines: &str) -> String {
    assert!(text.contains(after), "{after}");
    text.replacen(after, &format!("{after}{lines}"), 1)
}

#[tokio::test]
async fn an_added_record_is_inserted_at_its_sorted_place() {
    let got = saved(BASE, async |s| add(s, "charlie").await).await;
    assert_eq!(got, inserted_after(BASE, BETA, &block("charlie")));
}

#[tokio::test]
async fn an_added_last_record_goes_before_whats_after_the_section() {
    let got = saved(BASE, async |s| add(s, "golf").await).await;
    let foxtrot = "    path: foxtrot\n";
    assert_eq!(got, inserted_after(BASE, foxtrot, &block("golf")));
}

#[tokio::test]
async fn an_added_first_record_goes_before_the_first() {
    let got = saved(BASE, async |s| add(s, "aardvark").await).await;
    let first = "repositories:\n";
    assert_eq!(got, inserted_after(BASE, first, &block("aardvark")));
}

/// The comment above a removed record stays: it isn't the record's.
#[tokio::test]
async fn a_removed_record_takes_only_its_lines() {
    let got = saved(BASE, async |s| delete(s, "git://e.com/beta.git").await).await;
    assert_eq!(got, BASE.replacen(BETA, "", 1));
}

#[tokio::test]
async fn an_add_and_an_edit_change_only_their_records() {
    let got = saved(BASE, async |s| {
        add(s, "charlie").await;
        upsert(s, "foxtrot", json!({"path": "foxtrot2"})).await;
    })
    .await;
    let want = inserted_after(BASE, BETA, &block("charlie")).replacen(
        "    path: foxtrot\n",
        "    path: foxtrot2\n",
        1,
    );
    assert_eq!(got, want);
}

#[tokio::test]
async fn a_new_section_is_appended() {
    let got = saved(BASE, async |s| {
        s.upsert_record(
            Some(FILE),
            "/services",
            "https://svc.example.com",
            json!({"access": "public"}),
            None,
            false,
        )
        .await
        .expect("upsert");
    })
    .await;
    let services = "services:\n  https://svc.example.com:\n    access: public\n";
    assert_eq!(got, format!("{BASE}{services}"));
}

#[tokio::test]
async fn an_empty_section_takes_its_first_record() {
    let base = format!("{BASE}services: {{}}\n# the end\n");
    let got = saved(&base, async |s| {
        s.upsert_record(
            Some(FILE),
            "/services",
            "https://svc.example.com",
            json!({"access": "public"}),
            None,
            false,
        )
        .await
        .expect("upsert");
    })
    .await;
    let services = "services:\n  https://svc.example.com:\n    access: public\n";
    assert_eq!(got, base.replacen("services: {}\n", services, 1));
}

/// Records as `CloudMapDB.save` writes them.
const MERGE_BASE: &str = "apiVersion: unfurl/v1.0.0
kind: CloudMap
repositories:
  git://e.com/alpha.git:
    path: alpha
  git://e.com/charlie.git:
    path: charlie
  git://e.com/echo.git:
    path: echo
  git://e.com/golf.git:
    path: golf
  git://e.com/india.git:
    path: india
";

fn git(dir: &Path, args: &[&str]) -> bool {
    std::process::Command::new("git")
        .args(["-c", "user.name=t", "-c", "user.email=t@e"])
        .args(args)
        .current_dir(dir)
        .output()
        .expect("git")
        .status
        .success()
}

/// A repository holding `MERGE_BASE`, a branch for each change, each saved
/// by git-sync and committed; whether git merges the two cleanly.
async fn merges(a: impl AsyncFn(&SyncedDir), b: impl AsyncFn(&SyncedDir)) -> bool {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path().join("repo");
    std::fs::create_dir(&root).expect("mkdir");
    std::fs::write(root.join(FILE), MERGE_BASE).expect("write");
    assert!(git(&root, &["init", "-q", "-b", "main"]));
    assert!(git(&root, &["add", FILE]));
    assert!(git(&root, &["commit", "-qm", "base"]));
    branch(&root, &tmp.path().join("a.db"), "a", a).await;
    branch(&root, &tmp.path().join("b.db"), "b", b).await;
    assert!(git(&root, &["checkout", "-q", "a"]));
    git(&root, &["merge", "-q", "--no-edit", "b"])
}

/// Branch `name` off main in `root`, with `change` saved and committed.
async fn branch(root: &Path, db: &Path, name: &str, change: impl AsyncFn(&SyncedDir)) {
    assert!(git(root, &["checkout", "-q", "-b", name, "main"]));
    let sync = open(root, db).await;
    change(&sync).await;
    let outcome = sync.save_changes().await.expect("save");
    assert!(outcome.failed.is_empty(), "{outcome:?}");
    assert!(git(root, &["commit", "-qam", name]));
}

#[tokio::test]
async fn two_added_records_merge() {
    assert!(
        merges(
            async |s| add(s, "delta").await,
            async |s| add(s, "hotel").await
        )
        .await
    );
}

#[tokio::test]
async fn an_added_and_an_edited_record_merge() {
    assert!(
        merges(
            async |s| add(s, "delta").await,
            async |s| { upsert(s, "golf", json!({"path": "golf2"})).await }
        )
        .await
    );
}

#[tokio::test]
async fn an_added_and_a_removed_record_merge() {
    assert!(
        merges(
            async |s| add(s, "hotel").await,
            async |s| { delete(s, "git://e.com/charlie.git").await }
        )
        .await
    );
}

/// Both branches adding the same record differently is a real conflict.
#[tokio::test]
async fn one_record_added_twice_conflicts() {
    assert!(
        !merges(
            async |s| upsert(s, "delta", json!({"path": "one"})).await,
            async |s| upsert(s, "delta", json!({"path": "two"})).await
        )
        .await
    );
}
