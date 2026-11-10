// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A handle opened with pathspecs takes in and writes only the files they
//! match, as if the repository held nothing else.

use std::path::Path;

use tempfile::TempDir;
use unfurl_git_sync::{git, DbConfig, Error, FormatRegistry, RecordQuery, ScanOptions, SyncedRepo};

const SCOPED: &str = "cloudmap.yaml";
const OTHER: &str = "other/cloudmap.yaml";

fn cloudmap(repo_url: &str) -> Vec<u8> {
    format!(
        "apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  {repo_url}:\n    path: p\n"
    )
    .into_bytes()
}

/// A repository holding the scoped cloudmap, another cloudmap and a file
/// that doesn't parse, with a file-backed database so it can be reopened.
fn repo() -> (TempDir, String) {
    let tmp = tempfile::tempdir().expect("tempdir");
    git::init_with_files(
        tmp.path(),
        &[
            (SCOPED.to_string(), cloudmap("git://example.com/a.git")),
            (OTHER.to_string(), cloudmap("git://example.com/b.git")),
            ("broken.yaml".to_string(), b"key: [unclosed\n".to_vec()),
        ],
        "initial",
    )
    .expect("init repo");
    let db = format!("sqlite://{}?mode=rwc", tmp.path().join("sync.db").display());
    (tmp, db)
}

async fn open_matching(
    path: &Path,
    db: &str,
    pathspecs: &[&str],
) -> unfurl_git_sync::Result<SyncedRepo> {
    SyncedRepo::open_with(
        path,
        DbConfig::Sqlite { url: db.into() },
        FormatRegistry::with_builtins(),
        git::Trust::Full,
        pathspecs,
    )
    .await
}

async fn open_scoped(path: &Path, db: &str) -> unfurl_git_sync::Result<SyncedRepo> {
    open_matching(path, db, &[SCOPED]).await
}

async fn keys_in(sync: &SyncedRepo, file_path: &str) -> Vec<String> {
    let query = RecordQuery {
        file_path: Some(file_path.into()),
        path: Some("/repositories".into()),
        ..Default::default()
    };
    let records = sync.find_records(&query).await.expect("find");
    records.into_iter().map(|r| r.key).collect()
}

#[tokio::test]
async fn a_scoped_scan_reads_only_its_files() {
    let (tmp, db) = repo();
    let sync = open_scoped(tmp.path(), &db).await.expect("open");
    let outcome = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert_eq!(outcome.files_seen, 1, "{outcome:?}");
    assert!(outcome.unparsed.is_empty(), "{outcome:?}");
    assert_eq!(keys_in(&sync, SCOPED).await, ["git://example.com/a.git"]);
    assert!(keys_in(&sync, OTHER).await.is_empty());
}

#[tokio::test]
async fn changes_outside_the_scope_are_not_taken_in() {
    let (tmp, db) = repo();
    let sync = open_scoped(tmp.path(), &db).await.expect("open");
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");

    // on disk, then committed: neither the draft nor the committed side
    // parses it
    std::fs::write(tmp.path().join(OTHER), cloudmap("git://example.com/c.git")).expect("write");
    let outcome = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert_eq!(outcome.files_parsed, 0, "{outcome:?}");
    let repo = git::open_repo(tmp.path()).expect("open repo");
    git::commit_paths(&repo, &[OTHER.to_string()], "change other").expect("commit");
    let outcome = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert_eq!(outcome.files_parsed, 0, "{outcome:?}");
    assert!(keys_in(&sync, OTHER).await.is_empty());
    assert_eq!(keys_in(&sync, SCOPED).await, ["git://example.com/a.git"]);
}

#[tokio::test]
async fn deleting_the_scoped_file_is_taken_in() {
    let (tmp, db) = repo();
    let sync = open_scoped(tmp.path(), &db).await.expect("open");
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    std::fs::remove_file(tmp.path().join(SCOPED)).expect("rm");
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert!(keys_in(&sync, SCOPED).await.is_empty());
}

#[tokio::test]
async fn opening_scoped_over_other_files_is_refused() {
    let (tmp, db) = repo();
    let unscoped = SyncedRepo::open(
        tmp.path(),
        DbConfig::Sqlite { url: db.clone() },
        FormatRegistry::with_builtins(),
    )
    .await
    .expect("open");
    unscoped
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert_eq!(keys_in(&unscoped, OTHER).await, ["git://example.com/b.git"]);
    drop(unscoped);

    let refused = open_scoped(tmp.path(), &db).await;
    assert!(
        matches!(&refused, Err(Error::OutOfScope { file_path }) if file_path == OTHER),
        "{:?}",
        refused.err()
    );
}

#[tokio::test]
async fn writing_outside_the_scope_is_refused() {
    let (tmp, db) = repo();
    let sync = open_scoped(tmp.path(), &db).await.expect("open");
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let record = serde_json::json!({"path": "q"});
    let outside = sync
        .upsert_record(
            Some(OTHER),
            "/repositories",
            "git://example.com/d.git",
            record.clone(),
            None,
            false,
        )
        .await;
    assert!(
        matches!(&outside, Err(Error::OutOfScope { file_path }) if file_path == OTHER),
        "{:?}",
        outside.err()
    );
    sync.upsert_record(
        Some(SCOPED),
        "/repositories",
        "git://example.com/d.git",
        record,
        None,
        false,
    )
    .await
    .expect("write in scope");
    let mut keys = keys_in(&sync, SCOPED).await;
    keys.sort();
    assert_eq!(keys, ["git://example.com/a.git", "git://example.com/d.git"]);
}

#[tokio::test]
async fn another_handles_files_survive_a_scoped_scan() {
    let (tmp, db) = repo();
    let scoped = open_scoped(tmp.path(), &db).await.expect("open");
    scoped
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    // an unscoped handle on the same worktree takes in the other file
    let unscoped = SyncedRepo::open(
        tmp.path(),
        DbConfig::Sqlite { url: db.clone() },
        FormatRegistry::with_builtins(),
    )
    .await
    .expect("open");
    unscoped
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");

    // the scoped handle doesn't see it, so mustn't read it as deleted
    scoped
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert_eq!(keys_in(&unscoped, OTHER).await, ["git://example.com/b.git"]);
}

#[tokio::test]
async fn deletes_outside_the_scope_are_refused_before_any_lookup() {
    let (tmp, db) = repo();
    let scoped = open_scoped(tmp.path(), &db).await.expect("open");
    scoped
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    // another handle takes in the other file, so it exists to delete
    let unscoped = SyncedRepo::open(
        tmp.path(),
        DbConfig::Sqlite { url: db.clone() },
        FormatRegistry::with_builtins(),
    )
    .await
    .expect("open");
    unscoped
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");

    let file = scoped.delete_file(OTHER, None).await;
    assert!(
        matches!(&file, Err(Error::OutOfScope { .. })),
        "{:?}",
        file.err()
    );
    // no such record: still refused for its file, not reported missing
    let record = scoped
        .delete_record(
            Some(OTHER),
            "/repositories",
            "git://example.com/none.git",
            None,
            false,
        )
        .await;
    assert!(
        matches!(&record, Err(Error::OutOfScope { .. })),
        "{:?}",
        record.err()
    );
    assert_eq!(keys_in(&unscoped, OTHER).await, ["git://example.com/b.git"]);
}

#[tokio::test]
async fn a_removed_file_outside_the_scope_does_not_block_opening() {
    let (tmp, db) = repo();
    let unscoped = SyncedRepo::open(
        tmp.path(),
        DbConfig::Sqlite { url: db.clone() },
        FormatRegistry::with_builtins(),
    )
    .await
    .expect("open");
    unscoped
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    unscoped.delete_file(OTHER, None).await.expect("delete");
    drop(unscoped);
    open_scoped(tmp.path(), &db).await.expect("open scoped");
}

/// Each case opens its own database, so none holds another's files.
async fn files_seen(tmp: &TempDir, case: &str, pathspecs: &[&str]) -> usize {
    let db = format!(
        "sqlite://{}?mode=rwc",
        tmp.path().join(format!("{case}.db")).display()
    );
    let sync = open_matching(tmp.path(), &db, pathspecs)
        .await
        .expect("open");
    let outcome = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert!(outcome.unparsed.is_empty(), "{case}: {outcome:?}");
    outcome.files_seen
}

#[tokio::test]
async fn pathspecs_match_as_git_matches_them() {
    let (tmp, _) = repo();
    assert_eq!(files_seen(&tmp, "dir", &["other/"]).await, 1);
    assert_eq!(files_seen(&tmp, "dot", &["./cloudmap.yaml"]).await, 1);
    // `*` crosses directories in a pathspec, as in `git ls-files`
    assert_eq!(
        files_seen(&tmp, "glob", &["*.yaml", ":!broken.yaml"]).await,
        2
    );
}

#[tokio::test]
async fn a_pathspec_that_cant_be_matched_is_refused() {
    let (tmp, db) = repo();
    for bad in [
        ":(attr:a)cloudmap.yaml",
        ":(nonsense)cloudmap.yaml",
        "../outside.yaml",
    ] {
        let opened = open_matching(tmp.path(), &db, &[bad]).await;
        assert!(
            matches!(&opened, Err(Error::InvalidPathspec { pathspec, .. }) if pathspec == bad),
            "{bad:?}: {:?}",
            opened.err()
        );
    }
}
