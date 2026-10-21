// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A record moved to another file by a commit made outside the database
//! takes over the entries other worktrees' drafts made over it (§3.5),
//! on both backends.

mod common;

use common::crud_test;
use tempfile::TempDir;
use unfurl_git_sync::{Db, FormatRegistry, RecordQuery, ScanOptions, SyncedRepo, WorktreeFilter};

const MOVER: &str = "git://example.com/mover.git";

fn git(dir: &std::path::Path, args: &[&str]) -> String {
    let out = std::process::Command::new("git")
        .args(["-c", "user.name=t", "-c", "user.email=t@example.com"])
        .args(["-c", "commit.gpgsign=false"])
        .args(args)
        .current_dir(dir)
        .output()
        .expect("git");
    assert!(out.status.success(), "git {args:?}: {out:?}");
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

async fn put(sync: &SyncedRepo, file: &str, key: &str, name: &str) {
    sync.upsert_record(
        Some(file),
        "/repositories",
        key,
        serde_json::json!({"name": name}),
        None,
        false,
    )
    .await
    .expect("write");
}

/// A branch of `sync`'s repository at its HEAD, opened on the same
/// database and scanned, so it joins `sync`'s family.
async fn a_branch(sync: &SyncedRepo, tmp: &TempDir) -> (SyncedRepo, TempDir) {
    let base = git(tmp.path(), &["rev-parse", "HEAD"]);
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("feature");
    let at = path.to_str().expect("utf8");
    git(
        tmp.path(),
        &["worktree", "add", "-q", "-b", "feature", at, &base],
    );
    let branch = SyncedRepo::open_on(
        &path,
        sync.db().clone(),
        FormatRegistry::with_builtins(),
        unfurl_git_sync::git::Trust::Full,
    )
    .await
    .expect("open");
    branch
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    (branch, dir)
}

/// The live rows of `key` that worktree `w`'s draft supersedes:
/// `(file, the row's key_id, the entry's tag)`, sorted.
async fn hidden(db: &Db, w: i64, key: &str) -> Vec<(String, i64, i64)> {
    let sql = "SELECT r.file_path, r.key_id, x.key_id FROM superseded x \
         JOIN record r ON r.id = x.record_id \
         WHERE x.segment_id = (SELECT draft_segment_id FROM worktree WHERE id = ?1) \
         AND r.key = ?2 AND NOT r.deleted";
    let mut rows: Vec<(String, i64, i64)> = match db {
        Db::Sqlite(pool) => sqlx::query_as(sql)
            .bind(w)
            .bind(key)
            .fetch_all(pool)
            .await
            .expect("query"),
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => sqlx::query_as(&sql.replace("?1", "$1").replace("?2", "$2"))
            .bind(w)
            .bind(key)
            .fetch_all(pool)
            .await
            .expect("query"),
    };
    rows.sort();
    rows
}

/// `key`'s rows in a read across worktrees `ids`: `(worktree, file, record
/// id, name, deleted)`, sorted.
async fn read_across(
    sync: &SyncedRepo,
    ids: Vec<i64>,
    key: &str,
) -> Vec<(i64, String, i64, Option<String>, bool)> {
    let query = RecordQuery {
        worktrees: Some(WorktreeFilter {
            ids: Some(ids),
            ..Default::default()
        }),
        path: Some("/repositories".into()),
        key: Some(key.into()),
        ..Default::default()
    };
    let mut rows: Vec<_> = sync
        .find_records(&query)
        .await
        .expect("find")
        .into_iter()
        .map(|r| {
            (
                r.worktree_id,
                r.file_path,
                r.id,
                r.json["name"].as_str().map(str::to_string),
                r.deleted,
            )
        })
        .collect();
    rows.sort();
    rows
}

/// A record one commit moves into two files keeps its id in one, and only
/// that one takes over the edit a branch's draft made over it: the other
/// is a new record, which the edit was never made over.
async fn a_move_into_two_files_hands_the_entry_to_one(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    put(sync, "cloudmap.yaml", MOVER, "mover").await;
    put(sync, "second.yaml", "git://example.com/s.git", "s").await;
    put(sync, "third.yaml", "git://example.com/t.git", "t").await;
    sync.commit_repository("records", Default::default())
        .await
        .expect("commit");
    let (branch, _dir) = a_branch(sync, tmp).await;
    put(&branch, "cloudmap.yaml", MOVER, "edited").await;
    let before = hidden(sync.db(), branch.worktree_id(), MOVER).await;
    let [(_, id, tag)] = before.as_slice() else {
        panic!("the edit hides main's row: {before:?}")
    };
    assert_eq!(id, tag);

    // outside the database: the record leaves cloudmap.yaml for both others
    let block = format!("  \"{MOVER}\":\n    name: mover\n");
    let path = tmp.path().join("cloudmap.yaml");
    let text = std::fs::read_to_string(&path).expect("read");
    assert!(text.contains(&block), "{text}");
    std::fs::write(&path, text.replace(&block, "")).expect("write");
    for f in ["second.yaml", "third.yaml"] {
        let path = tmp.path().join(f);
        let text = std::fs::read_to_string(&path).expect("read");
        std::fs::write(&path, format!("{text}{block}")).expect("write");
    }
    git(tmp.path(), &["commit", "-qam", "move"]);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");

    let after = hidden(sync.db(), branch.worktree_id(), MOVER).await;
    let moved: Vec<_> = after
        .iter()
        .filter(|(f, ..)| f != "cloudmap.yaml")
        .collect();
    assert_eq!(moved.len(), 1, "one destination takes the entry: {after:?}");
    let (_, row, tag) = moved[0];
    assert_eq!((row, tag), (id, id), "the one keeping the id: {after:?}");

    // a read across both: the branch's edit hides main's row that kept the
    // record's id, but not the new record in the other file
    let (main, b) = (sync.worktree_id(), branch.worktree_id());
    let (kept, mut want): (Vec<_>, Vec<_>) = read_across(sync, vec![main], MOVER)
        .await
        .into_iter()
        .partition(|r| r.2 == *id);
    assert_eq!(kept.len(), 1, "main keeps the id in one file: {kept:?}");
    want.push((b, "cloudmap.yaml".into(), *id, Some("edited".into()), false));
    want.sort();
    assert_eq!(read_across(sync, vec![main, b], MOVER).await, want);
}
crud_test!(a_move_into_two_files_hands_the_entry_to_one);
