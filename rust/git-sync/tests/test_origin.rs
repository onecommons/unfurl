// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Which remote names a worktree's repository, and a database keyed under
//! another of its remotes being carried over when the choice changes.

mod common;

use std::path::Path;

use tempfile::TempDir;
use unfurl_git_sync::{
    git, DbConfig, FormatRegistry, OpenOptions, ScanOptions, SyncedRepo, WorktreeFilter,
};

const CLOUDMAP: &str = "cloudmap.yaml";

fn repo() -> TempDir {
    let tmp = tempfile::tempdir().expect("tempdir");
    git::init_with_files(
        tmp.path(),
        &[(
            CLOUDMAP.to_string(),
            b"apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories: {}\n".to_vec(),
        )],
        "initial",
    )
    .expect("init repo");
    tmp
}

fn add_remote(dir: &Path, name: &str, host: &str) {
    common::git(
        dir,
        &[
            "remote",
            "add",
            name,
            &format!("https://{host}/org/proj.git"),
        ],
    );
}

async fn open(dir: &Path, db: &DbConfig, options: OpenOptions) -> SyncedRepo {
    SyncedRepo::open_with(
        dir,
        db.clone(),
        FormatRegistry::with_builtins(),
        git::Trust::Full,
        options,
    )
    .await
    .expect("open")
}

async fn origins(sync: &SyncedRepo) -> Vec<String> {
    let all = sync
        .worktrees(&WorktreeFilter::default())
        .await
        .expect("worktrees");
    all.into_iter().map(|w| w.origin).collect()
}

async fn the_origin_is_the_preferred_remote_then_origin(db: DbConfig) {
    let tmp = repo();
    // sorts before origin and canonical
    add_remote(tmp.path(), "aaa", "aaa.example.com");
    add_remote(tmp.path(), "origin", "origin.example.com");
    add_remote(tmp.path(), "canonical", "canonical.example.com");
    let repo = git::open_repo(tmp.path()).expect("open repo");
    let chosen = |preferred| git::worktree_meta_preferring(&repo, preferred).expect("meta");
    assert_eq!(chosen(None).origin, "origin.example.com/org/proj");
    assert_eq!(
        chosen(Some("canonical")).origin,
        "canonical.example.com/org/proj"
    );
    // a preferred remote the clone hasn't: origin
    assert_eq!(
        chosen(Some("missing")).origin,
        "origin.example.com/org/proj"
    );

    let sync = open(tmp.path(), &db, OpenOptions::default().remote("canonical")).await;
    assert_eq!(origins(&sync).await, ["canonical.example.com/org/proj"]);
}

async fn a_database_keyed_under_another_remote_is_carried_over(db: DbConfig) {
    let tmp = repo();
    add_remote(tmp.path(), "upstream", "upstream.example.com");
    let before = open(tmp.path(), &db, OpenOptions::default()).await;
    before
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let pending = serde_json::json!({"path": "p"});
    before
        .upsert_record(
            Some(CLOUDMAP),
            "/repositories",
            "git://example.com/pending.git",
            pending.clone(),
            None,
            false,
        )
        .await
        .expect("upsert");
    let id = before.worktree_id();
    assert_eq!(origins(&before).await, ["upstream.example.com/org/proj"]);
    drop(before);

    // origin now wins the choice
    add_remote(tmp.path(), "origin", "origin.example.com");
    let after = open(tmp.path(), &db, OpenOptions::default()).await;
    assert_eq!(after.worktree_id(), id, "the same worktree, re-keyed");
    assert_eq!(origins(&after).await, ["origin.example.com/org/proj"]);
    let kept = after
        .get_record(CLOUDMAP, "/repositories", "git://example.com/pending.git")
        .await
        .expect("get")
        .expect("the pending edit");
    assert_eq!(kept.json, pending);
    assert!(kept.commit_id.is_none(), "still pending");
}

async fn a_worktree_keyed_under_a_removed_remote_is_left_behind(db: DbConfig) {
    let tmp = repo();
    add_remote(tmp.path(), "upstream", "upstream.example.com");
    let before = open(tmp.path(), &db, OpenOptions::default()).await;
    let id = before.worktree_id();
    drop(before);
    common::git(tmp.path(), &["remote", "remove", "upstream"]);
    add_remote(tmp.path(), "origin", "origin.example.com");
    // nothing names the old origin any more, so it can't be matched
    let after = open(tmp.path(), &db, OpenOptions::default()).await;
    assert_ne!(after.worktree_id(), id);
    let mut all = origins(&after).await;
    all.sort();
    assert_eq!(
        all,
        [
            "origin.example.com/org/proj",
            "upstream.example.com/org/proj"
        ]
    );
}

/// `$name` run against a file-backed SQLite database, and with the
/// `postgres` feature, a Postgres schema of its own.
macro_rules! both_backends {
    ($name:ident) => {
        mod $name {
            #[tokio::test]
            async fn sqlite() {
                let tmp = tempfile::tempdir().expect("tempdir");
                let url = format!("sqlite://{}?mode=rwc", tmp.path().join("sync.db").display());
                super::$name(unfurl_git_sync::DbConfig::Sqlite { url }).await;
            }

            #[cfg(feature = "postgres")]
            #[tokio::test]
            async fn postgres() {
                let Some(scope) = crate::common::PgScope::setup().await else {
                    eprintln!("skip: UNFURL_TEST_PG_URL not set");
                    return;
                };
                super::$name(scope.db_config()).await;
                scope.teardown().await;
            }
        }
    };
}

both_backends!(the_origin_is_the_preferred_remote_then_origin);
both_backends!(a_database_keyed_under_another_remote_is_carried_over);
both_backends!(a_worktree_keyed_under_a_removed_remote_is_left_behind);
