//! A record keeps its id through a merge made outside the database, by the
//! rollup of the commit that set its value, when that rollup is this
//! database's and its family's (docs/branch-segments.md §4.9).

mod common;

use std::path::{Path, PathBuf};

use common::*;
use unfurl_git_sync::{RecordQuery, ScanOptions, SyncedRepo};

/// The id of the record `key` that `sync` shows, if any.
async fn id_of(sync: &SyncedRepo, key: &str) -> Option<i64> {
    sync.find_records(&RecordQuery::default())
        .await
        .expect("find")
        .into_iter()
        .find(|r| r.key == key)
        .map(|r| r.id)
}

/// Write the record `key` through the database and commit it.
async fn write_and_commit(sync: &SyncedRepo, key: &str) {
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        key,
        serde_json::json!({ "name": key }),
        None,
        false,
    )
    .await
    .expect("write");
    sync.commit_repository(key, Default::default())
        .await
        .expect("commit")
        .commit
        .expect("a commit");
}

/// `git` in `dir`, with an identity.
fn git_as(dir: &Path, args: &[&str]) {
    let mut all = vec!["-c", "user.name=t", "-c", "user.email=t@t"];
    all.extend(args);
    git(dir, &all);
}

/// A directory beside `tmp`'s checkout, named for `what`.
fn beside(tmp: &Path, what: &str) -> PathBuf {
    let name = tmp.file_name().expect("name").to_string_lossy();
    tmp.parent().expect("parent").join(format!("{name}-{what}"))
}

async fn scan(sync: &SyncedRepo) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
}

/// A branch's commit merged into main by a merge commit made outside the
/// database: main's scan takes the record with the id the branch gave it.
#[tokio::test]
async fn a_branch_merged_back_keeps_its_records_ids() {
    let (tmp, db) = file_backed_fixture().await;
    git(
        tmp.path(),
        &["remote", "add", "origin", "https://example.com/m.git"],
    );
    let main = open_at(tmp.path(), &db).await;
    scan(&main).await;
    let dir = beside(tmp.path(), "feature");
    let path = dir.to_str().expect("utf8");
    git(
        tmp.path(),
        &["worktree", "add", "-q", "-b", "feature", path],
    );
    let feature = open_at(&dir, &db).await;
    scan(&feature).await;
    write_and_commit(&feature, "merged").await;
    let id = id_of(&feature, "merged").await.expect("written");

    git_as(
        tmp.path(),
        &["merge", "-q", "--no-ff", "-m", "merge", "feature"],
    );
    scan(&main).await;
    assert_eq!(id_of(&main, "merged").await, Some(id), "the branch's id");
    std::fs::remove_dir_all(&dir).ok();
}

/// A squash merge keeps no rollup: the record is new to main.
#[tokio::test]
async fn a_squash_merge_gives_new_ids() {
    let (tmp, db) = file_backed_fixture().await;
    git(
        tmp.path(),
        &["remote", "add", "origin", "https://example.com/m.git"],
    );
    let main = open_at(tmp.path(), &db).await;
    scan(&main).await;
    let dir = beside(tmp.path(), "squashed");
    let path = dir.to_str().expect("utf8");
    git(
        tmp.path(),
        &["worktree", "add", "-q", "-b", "squashed", path],
    );
    let feature = open_at(&dir, &db).await;
    scan(&feature).await;
    write_and_commit(&feature, "squashed").await;
    let id = id_of(&feature, "squashed").await.expect("written");

    git_as(tmp.path(), &["merge", "-q", "--squash", "squashed"]);
    git_as(tmp.path(), &["commit", "-q", "-m", "squash"]);
    scan(&main).await;
    let got = id_of(&main, "squashed").await.expect("merged");
    assert_ne!(got, id, "nothing names the branch's id");
    std::fs::remove_dir_all(&dir).ok();
}

/// Another family's commits merged in: their rollups name that family's
/// records, so the scan takes none of their ids.
#[tokio::test]
async fn another_familys_ids_are_not_taken() {
    let (tmp, db) = file_backed_fixture().await;
    git(
        tmp.path(),
        &["remote", "add", "origin", "https://example.com/a.git"],
    );
    let other = beside(tmp.path(), "other");
    let src = tmp.path().to_str().expect("utf8");
    git(
        tmp.path(),
        &["clone", "-q", src, other.to_str().expect("utf8")],
    );
    git(
        &other,
        &["remote", "set-url", "origin", "https://example.com/b.git"],
    );
    // opened before the first family commits: no trailer joins it to it
    let b = open_at(&other, &db).await;
    scan(&b).await;
    let a = open_at(tmp.path(), &db).await;
    scan(&a).await;
    write_and_commit(&a, "shared").await;
    let id = id_of(&a, "shared").await.expect("written");

    git(&other, &["fetch", "-q", src, "HEAD"]);
    git_as(
        &other,
        &["merge", "-q", "--no-ff", "-m", "merge", "FETCH_HEAD"],
    );
    scan(&b).await;
    let got = id_of(&b, "shared").await.expect("merged");
    assert_ne!(got, id, "the other family's id");
    std::fs::remove_dir_all(&other).ok();
}
