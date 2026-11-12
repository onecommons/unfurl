// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A handle on a directory in no git repository
//! ([`SyncedDir::open`], docs/untracked-and-gitless.md).

use std::path::Path;

use serde_json::json;
use tempfile::TempDir;
use unfurl_git_sync::{
    git, CommitRef, DbConfig, Error, FormatRegistry, OpenOptions, RecordQuery, Resolution,
    ScanOptions, SyncedDir, SyncedRepo,
};

const FILE: &str = "maps/cloudmap.yaml";
const REPOS: &str = "/repositories";
const A: &str = "git://e.com/a.git";

fn cloudmap(path: &str) -> String {
    format!("apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  {A}:\n    path: {path}\n")
}

/// A directory holding `maps/cloudmap.yaml`, and a database file beside it.
fn dir() -> (TempDir, String) {
    let tmp = tempfile::tempdir().expect("tempdir");
    std::fs::create_dir(tmp.path().join("maps")).expect("mkdir");
    std::fs::write(tmp.path().join(FILE), cloudmap("a")).expect("write");
    let db = format!("sqlite://{}?mode=rwc", tmp.path().join("sync.db").display());
    (tmp, db)
}

async fn open_with(
    root: &Path,
    db: &str,
    options: OpenOptions,
) -> unfurl_git_sync::Result<SyncedDir> {
    SyncedDir::open(
        root,
        DbConfig::Sqlite { url: db.into() },
        FormatRegistry::with_builtins(),
        git::Trust::Full,
        options,
    )
    .await
}

async fn open(root: &Path, db: &str, pathspec: &str) -> SyncedDir {
    open_with(root, db, OpenOptions::default().pathspecs([pathspec]))
        .await
        .expect("open")
}

async fn scan(sync: &SyncedDir) -> unfurl_git_sync::SyncOutcome {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan")
}

async fn path_of(sync: &SyncedDir, file: &str) -> Option<serde_json::Value> {
    sync.get_record(file, REPOS, A)
        .await
        .expect("get")
        .map(|r| r.json["path"].clone())
}

/// `file`'s record `A` is the file's, not a pending edit: a hand edit to it
/// is taken in without a conflict. The public read shows no commit.
async fn assert_settled(sync: &SyncedDir, root: &Path, file: &str) {
    let r = sync
        .get_record(file, REPOS, A)
        .await
        .expect("get")
        .expect("record");
    assert_eq!(r.commit_id, None, "{file}");
    let text = on_disk(root, file);
    let old = format!("path: {}", r.json["path"].as_str().expect("path"));
    std::fs::write(root.join(file), text.replace(&old, "path: by-hand")).expect("write");
    let outcome = scan(sync).await;
    assert!(outcome.conflicts.is_empty(), "{file}: {outcome:?}");
    assert_eq!(path_of(sync, file).await, Some(json!("by-hand")), "{file}");
}

fn on_disk(root: &Path, file: &str) -> String {
    std::fs::read_to_string(root.join(file)).expect("read")
}

/// `SyncedRepo` fails on it as it always has: with gix's error opening
/// a repository there.
#[tokio::test]
async fn a_directory_without_a_repository_isnt_a_synced_repo() {
    let (tmp, db) = dir();
    let opened = SyncedRepo::open_with(
        tmp.path(),
        DbConfig::Sqlite { url: db },
        FormatRegistry::with_builtins(),
        git::Trust::Full,
        OpenOptions::default().pathspecs(["maps/"]),
    )
    .await;
    let expected = gix::open(tmp.path())
        .expect_err("no repository")
        .to_string();
    assert!(
        matches!(&opened, Err(Error::Git(m)) if *m == expected),
        "{:?}",
        opened.err()
    );
}

#[tokio::test]
async fn a_directory_without_a_repository_needs_pathspecs() {
    let (tmp, db) = dir();
    let opened = open_with(tmp.path(), &db, OpenOptions::default()).await;
    assert!(
        matches!(opened, Err(Error::InvalidPathspec { .. })),
        "{:?}",
        opened.err()
    );
}

/// A directory in a working tree opens as one, with its history.
#[tokio::test]
async fn a_repository_opens_as_one() {
    let (tmp, db) = dir();
    git::init_with_files(
        tmp.path(),
        &[(FILE.to_string(), cloudmap("a").into_bytes())],
        "initial",
    )
    .expect("init");
    let sync = open(tmp.path(), &db, "maps/").await;
    assert!(sync.repository().is_some());
    let wd = sync.get_working_dir().await.expect("working dir");
    assert!(wd.head_commit.is_some());
    assert_ne!(wd.branch, "");
    scan(&sync).await;
    let r = sync
        .get_record(FILE, REPOS, A)
        .await
        .expect("get")
        .expect("record");
    assert_eq!(r.commit_id, wd.head_commit);
}

/// The files under the root the pathspecs match, in any subdirectory,
/// taken in as the file's; the worktree is the directory's, with no
/// branch and no commit.
#[tokio::test]
async fn files_are_taken_in() {
    let (tmp, db) = dir();
    const DEEP: &str = "maps/deep/er/more.yaml";
    std::fs::create_dir_all(tmp.path().join("maps/deep/er")).expect("mkdir");
    std::fs::write(tmp.path().join(DEEP), cloudmap("deep")).expect("write");
    std::fs::write(tmp.path().join("outside.yaml"), cloudmap("outside")).expect("write");
    let sync = open(tmp.path(), &db, "maps/").await;
    let outcome = scan(&sync).await;
    assert!(outcome.conflicts.is_empty(), "{outcome:?}");
    let r = sync
        .get_record(FILE, REPOS, A)
        .await
        .expect("get")
        .expect("record");
    assert_eq!(r.commit_id, None);
    assert_eq!(path_of(&sync, DEEP).await, Some(json!("deep")));
    assert_eq!(path_of(&sync, "outside.yaml").await, None);

    let worktree = sync.get_worktree().await.expect("worktree");
    assert_eq!(worktree.branch, "");
    assert_eq!(worktree.commit_id, None);
    assert_eq!(
        worktree.origin,
        git::normalize_git_url_hard(&tmp.path().to_string_lossy())
    );
    let wd = sync.get_working_dir().await.expect("working dir");
    assert_eq!((wd.branch.as_str(), wd.head_commit), ("", None));
}

/// A hand edit is taken in; a deleted file's records go, and come back
/// with it.
#[tokio::test]
async fn the_disk_is_followed() {
    let (tmp, db) = dir();
    let sync = open(tmp.path(), &db, "maps/").await;
    scan(&sync).await;
    std::fs::write(tmp.path().join(FILE), cloudmap("hand")).expect("write");
    let outcome = scan(&sync).await;
    assert!(outcome.conflicts.is_empty(), "{outcome:?}");
    assert_eq!(path_of(&sync, FILE).await, Some(json!("hand")));

    std::fs::remove_file(tmp.path().join(FILE)).expect("rm");
    scan(&sync).await;
    assert_eq!(path_of(&sync, FILE).await, None);
    std::fs::write(tmp.path().join(FILE), cloudmap("back")).expect("write");
    let outcome = scan(&sync).await;
    assert!(outcome.conflicts.is_empty(), "{outcome:?}");
    assert_eq!(path_of(&sync, FILE).await, Some(json!("back")));
}

/// A save writes the edit, creating a file whose directory is missing,
/// and settles it: the next save writes nothing, the next scan changes
/// nothing.
#[tokio::test]
async fn a_save_writes_and_settles() {
    let (tmp, db) = dir();
    let sync = open(tmp.path(), &db, "maps/").await;
    scan(&sync).await;
    const NEW: &str = "maps/new/dir/created.yaml";
    sync.upsert_record(Some(FILE), REPOS, A, json!({"path": "edited"}), None, false)
        .await
        .expect("upsert");
    sync.upsert_record(Some(NEW), REPOS, A, json!({"path": "created"}), None, false)
        .await
        .expect("upsert");
    let saved = sync.save_changes().await.expect("save");
    assert!(
        saved.failed.is_empty() && saved.conflicts.is_empty(),
        "{saved:?}"
    );
    assert_eq!(saved.written.len(), 2, "{saved:?}");
    assert_eq!(on_disk(tmp.path(), FILE), cloudmap("edited"));
    assert!(on_disk(tmp.path(), NEW).contains("path: created"));
    let again = sync.save_changes().await.expect("save");
    assert!(again.written.is_empty(), "{again:?}");
    let outcome = scan(&sync).await;
    assert!(outcome.conflicts.is_empty(), "{outcome:?}");
    assert_eq!(path_of(&sync, FILE).await, Some(json!("edited")));
    assert_eq!(path_of(&sync, NEW).await, Some(json!("created")));
    for file in [FILE, NEW] {
        assert_settled(&sync, tmp.path(), file).await;
    }
}

/// An edit a hand edit to its record holds back stays pending through
/// the save, and settles once resolved and saved.
#[tokio::test]
async fn a_conflicted_edit_settles_after_its_resolution() {
    let (tmp, db) = dir();
    let sync = open(tmp.path(), &db, "maps/").await;
    scan(&sync).await;
    sync.upsert_record(Some(FILE), REPOS, A, json!({"path": "ours"}), None, false)
        .await
        .expect("upsert");
    std::fs::write(tmp.path().join(FILE), cloudmap("theirs")).expect("write");
    let saved = sync.save_changes().await.expect("save");
    assert_eq!(saved.conflicts.len(), 1, "{saved:?}");
    assert_eq!(on_disk(tmp.path(), FILE), cloudmap("theirs"));
    let r = sync
        .get_record(FILE, REPOS, A)
        .await
        .expect("get")
        .expect("record");
    assert_eq!((r.json["path"].clone(), r.commit_id), (json!("ours"), None));

    sync.resolve_conflict(FILE, REPOS, A, Resolution::Ours, None)
        .await
        .expect("resolve");
    let saved = sync.save_changes().await.expect("save");
    assert!(saved.conflicts.is_empty(), "{saved:?}");
    assert_eq!(on_disk(tmp.path(), FILE), cloudmap("ours"));
    assert_settled(&sync, tmp.path(), FILE).await;
}

/// Deleting the last record takes the file off the disk; recreated by
/// hand, it's taken in again.
#[tokio::test]
async fn a_file_emptied_by_a_save_comes_back() {
    let (tmp, db) = dir();
    let sync = open(tmp.path(), &db, "maps/").await;
    scan(&sync).await;
    sync.delete_record(Some(FILE), REPOS, A, None, false)
        .await
        .expect("delete");
    let saved = sync.save_changes().await.expect("save");
    assert!(saved.failed.is_empty(), "{saved:?}");
    assert!(!on_disk(tmp.path(), FILE).contains("path: a"), "{saved:?}");
    assert_eq!(path_of(&sync, FILE).await, None);

    std::fs::write(tmp.path().join(FILE), cloudmap("back")).expect("write");
    let outcome = scan(&sync).await;
    assert!(outcome.conflicts.is_empty(), "{outcome:?}");
    assert_eq!(path_of(&sync, FILE).await, Some(json!("back")));
    let again = sync.save_changes().await.expect("save");
    assert!(
        again.written.is_empty() && again.conflicts.is_empty(),
        "{again:?}"
    );
}

/// The same file under two roots is two worktrees'.
#[tokio::test]
async fn two_roots_are_two_worktrees() {
    let (tmp, db) = dir();
    let outer = open(tmp.path(), &db, "maps/").await;
    let inner = open(&tmp.path().join("maps"), &db, "cloudmap.yaml").await;
    assert_ne!(outer.worktree_id(), inner.worktree_id());
    scan(&outer).await;
    scan(&inner).await;
    assert_eq!(path_of(&outer, FILE).await, Some(json!("a")));
    assert_eq!(path_of(&inner, "cloudmap.yaml").await, Some(json!("a")));
}

/// A directory in no repository has no history to give.
#[tokio::test]
async fn no_repository_has_no_history() {
    let (tmp, db) = dir();
    let sync = open(tmp.path(), &db, "maps/").await;
    assert!(sync.repository().is_none());
}

/// An edit saves over a hand edit to another record of the file, which
/// the save keeps.
#[tokio::test]
async fn an_edit_saves_over_a_hand_edit_elsewhere() {
    let (tmp, db) = dir();
    const B: &str = "git://e.com/b.git";
    let two = |a: &str, b: &str| {
        format!("apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  {A}:\n    path: {a}\n  {B}:\n    path: {b}\n")
    };
    std::fs::write(tmp.path().join(FILE), two("a", "b")).expect("write");
    let sync = open(tmp.path(), &db, "maps/").await;
    scan(&sync).await;
    sync.upsert_record(Some(FILE), REPOS, A, json!({"path": "ours"}), None, false)
        .await
        .expect("upsert");
    std::fs::write(tmp.path().join(FILE), two("a", "hand")).expect("write");
    let saved = sync.save_changes().await.expect("save");
    assert!(saved.conflicts.is_empty(), "{saved:?}");
    assert_eq!(on_disk(tmp.path(), FILE), two("ours", "hand"));
}

/// An edit a save doesn't put on disk stays pending; the one beside it
/// that it writes settles.
#[tokio::test]
async fn an_edit_the_save_skips_stays_pending() {
    let (tmp, db) = dir();
    let sync = open(tmp.path(), &db, "maps/").await;
    scan(&sync).await;
    sync.upsert_record(Some(FILE), "/", "rootless", json!({"x": 1}), None, false)
        .await
        .expect("upsert");
    sync.upsert_record(Some(FILE), REPOS, A, json!({"path": "edited"}), None, false)
        .await
        .expect("upsert");
    let saved = sync.save_changes().await.expect("save");
    assert!(saved.failed.is_empty(), "{saved:?}");
    assert!(!on_disk(tmp.path(), FILE).contains("rootless"));
    let skipped = sync
        .get_record(FILE, "/", "rootless")
        .await
        .expect("get")
        .expect("record");
    assert_eq!(skipped.commit_id, None);
    assert_settled(&sync, tmp.path(), FILE).await;
    // still pending: a scan of the file, which lacks it, keeps it
    assert!(sync
        .get_record(FILE, "/", "rootless")
        .await
        .expect("get")
        .is_some());
}

/// A repository that can't be read is an error, not a directory with no
/// repository.
#[tokio::test]
async fn an_unreadable_repository_is_an_error() {
    for git_dir in ["file", "empty dir"] {
        let (tmp, db) = dir();
        let dot_git = tmp.path().join(".git");
        match git_dir {
            "file" => std::fs::write(&dot_git, "gitdir: /nowhere/at/all\n").expect("write"),
            _ => std::fs::create_dir(&dot_git).expect("mkdir"),
        }
        let opened = open_with(tmp.path(), &db, OpenOptions::default().pathspecs(["maps/"])).await;
        assert!(
            matches!(opened, Err(Error::Git(_))),
            "{git_dir}: {:?}",
            opened.map(|s| s.worktree_id())
        );
    }
}

/// No read or error shows a commit for a value no commit holds.
#[tokio::test]
async fn no_commit_is_shown() {
    let (tmp, db) = dir();
    let sync = open(tmp.path(), &db, "maps/").await;
    scan(&sync).await;
    let found = sync
        .find_records(&RecordQuery::default().file_path(FILE))
        .await
        .expect("find");
    assert!(!found.is_empty());
    assert!(found.iter().all(|r| r.commit_id.is_none()), "{found:?}");

    let token = CommitRef::Commit("4551885dfab39991cfdb958cb79fcb6aa282481d".to_string());
    let refused = sync
        .update_record(
            Some(FILE),
            REPOS,
            A,
            json!({"path": "x"}),
            Some(token),
            false,
        )
        .await;
    assert!(
        matches!(&refused, Err(Error::Conflict { actual: None, .. })),
        "{refused:?}"
    );
    let stale = sync
        .update_record(
            Some(FILE),
            REPOS,
            A,
            json!({"path": "x"}),
            Some(CommitRef::Pending(0)),
            false,
        )
        .await;
    assert!(
        matches!(&stale, Err(Error::Conflict { actual: None, .. })),
        "{stale:?}"
    );
}
