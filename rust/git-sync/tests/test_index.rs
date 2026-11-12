// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A commit through the handle changes the index only at the paths it
//! commits, as `git commit <path>` does.

mod common;

use std::path::Path;

use common::git;
use serde_json::json;
use unfurl_git_sync::{OpenOptions, ScanOptions, SyncedRepo};

const FILE: &str = "cloudmap.yaml";
const REPOS: &str = "/repositories";

fn git_out(dir: &Path, args: &[&str]) -> String {
    let out = std::process::Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .expect("git");
    assert!(out.status.success(), "git {args:?}: {out:?}");
    String::from_utf8(out.stdout).expect("utf-8")
}

/// The blob the index has at `path`, if any.
fn staged(dir: &Path, path: &str) -> Option<String> {
    let line = git_out(dir, &["ls-files", "-s", "--", path]);
    line.split_whitespace().nth(1).map(str::to_string)
}

fn hash(dir: &Path, path: &str) -> String {
    git_out(dir, &["hash-object", path]).trim().to_string()
}

/// A line of `git status --porcelain` for `path`, if it has one.
fn status(dir: &Path, path: &str) -> Option<String> {
    git_out(dir, &["status", "--porcelain", "--", path])
        .lines()
        .next()
        .map(str::to_string)
}

async fn open(dir: &Path) -> SyncedRepo {
    let db = format!("sqlite://{}?mode=rwc", dir.join(".git/sync.db").display());
    SyncedRepo::open_with(
        dir,
        unfurl_git_sync::DbConfig::Sqlite { url: db },
        unfurl_git_sync::FormatRegistry::with_builtins(),
        unfurl_git_sync::git::Trust::Full,
        OpenOptions::default().pathspecs([FILE]).untracked(true),
    )
    .await
    .expect("open")
}

async fn commit_an_edit(sync: &SyncedRepo) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    sync.upsert_record(
        Some(FILE),
        REPOS,
        "git://e.com/new.git",
        json!({"path": "new"}),
        None,
        false,
    )
    .await
    .expect("upsert");
    let committed = sync
        .commit_repository("edit", Default::default())
        .await
        .expect("commit");
    assert!(committed.commit.is_some());
}

/// Staged work on other paths survives; the committed path is clean.
#[tokio::test]
async fn unrelated_staged_work_stays_staged() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let dir = tmp.path();
    common::init_repo_with_fixture(dir).await;
    std::fs::write(dir.join("other.txt"), "committed\n").expect("write");
    git(dir, &["add", "other.txt"]);
    git(
        dir,
        &[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e",
            "commit",
            "-qm",
            "other",
        ],
    );
    std::fs::write(dir.join("new.txt"), "new\n").expect("write");
    std::fs::write(dir.join("other.txt"), "staged edit\n").expect("write");
    git(dir, &["add", "new.txt", "other.txt"]);
    let staged_edit = staged(dir, "other.txt");

    let sync = open(dir).await;
    commit_an_edit(&sync).await;

    assert_eq!(status(dir, "new.txt").as_deref(), Some("A  new.txt"));
    assert_eq!(staged(dir, "other.txt"), staged_edit);
    assert_eq!(status(dir, "other.txt").as_deref(), Some("M  other.txt"));
    assert_eq!(status(dir, FILE), None);
    assert_eq!(staged(dir, FILE), Some(hash(dir, FILE)));
}

/// A committed path's own staged version gives way to what was committed.
#[tokio::test]
async fn a_committed_paths_staged_version_is_replaced() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let dir = tmp.path();
    common::init_repo_with_fixture(dir).await;
    let fixture = std::fs::read_to_string(dir.join(FILE)).expect("read");
    std::fs::write(dir.join(FILE), fixture.replace("dashboard", "hand-staged")).expect("write");
    git(dir, &["add", FILE]);

    let sync = open(dir).await;
    commit_an_edit(&sync).await;

    assert_eq!(status(dir, FILE), None);
    assert_eq!(staged(dir, FILE), Some(hash(dir, FILE)));
}

/// A commit that deletes a path leaves the index without it.
#[tokio::test]
async fn a_committed_deletion_leaves_no_entry() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let dir = tmp.path();
    common::init_repo_with_fixture(dir).await;
    std::fs::write(dir.join("new.txt"), "new\n").expect("write");
    git(dir, &["add", "new.txt"]);

    let sync = open(dir).await;
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    sync.delete_file(FILE, None).await.expect("delete");
    sync.commit_repository("delete", Default::default())
        .await
        .expect("commit");

    assert_eq!(staged(dir, FILE), None);
    assert_eq!(status(dir, FILE), None);
    assert_eq!(status(dir, "new.txt").as_deref(), Some("A  new.txt"));
}

/// On a branch with no commit and no index file yet, the first commit
/// indexes what it committed and nothing else.
#[tokio::test]
async fn a_first_commit_without_an_index() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let dir = tmp.path();
    git(dir, &["init", "-q", "-b", "main"]);
    std::fs::write(
        dir.join(FILE),
        "apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  git://e.com/a.git:\n    path: a\n",
    )
    .expect("write");
    std::fs::write(dir.join("untracked.txt"), "u\n").expect("write");
    assert!(!dir.join(".git/index").exists());

    let sync = open(dir).await;
    commit_an_edit(&sync).await;

    assert_eq!(status(dir, FILE), None);
    assert_eq!(git_out(dir, &["ls-files"]).trim(), FILE);
    assert_eq!(
        status(dir, "untracked.txt").as_deref(),
        Some("?? untracked.txt")
    );
}

const SUB_FILE: &str = "maps/cloudmap.yaml";

/// A repository whose index `git` wrote with a cache-tree: `maps/cloudmap.yaml`
/// and `other.txt`, committed by `git` itself.
fn committed_by_git(dir: &Path) {
    git(dir, &["init", "-q", "-b", "main"]);
    std::fs::create_dir(dir.join("maps")).expect("mkdir");
    std::fs::write(
        dir.join(SUB_FILE),
        "apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  git://e.com/a.git:\n    path: a\n",
    )
    .expect("write");
    std::fs::write(dir.join("other.txt"), "committed\n").expect("write");
    git(dir, &["add", "."]);
    git(
        dir,
        &[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e",
            "commit",
            "-qm",
            "initial",
        ],
    );
    // `git commit` leaves a valid cache-tree in the index
    git_out(dir, &["write-tree"]);
}

/// Commit an edit of `maps/cloudmap.yaml` through the handle.
async fn commit_an_edit_in_a_subdirectory(dir: &Path) {
    let sync = an_edit_in_a_subdirectory(dir).await;
    let committed = sync
        .commit_repository("edit", Default::default())
        .await
        .expect("commit");
    assert!(committed.commit.is_some());
}

/// The index's cached trees don't outlive the commit: with nothing else
/// staged, the index is the new HEAD's tree.
#[tokio::test]
async fn the_index_is_the_commits_tree() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let dir = tmp.path();
    committed_by_git(dir);
    commit_an_edit_in_a_subdirectory(dir).await;

    let index_tree = git_out(dir, &["write-tree"]);
    let head_tree = git_out(dir, &["rev-parse", "HEAD^{tree}"]);
    assert_eq!(index_tree, head_tree);
    git_out(dir, &["fsck", "--no-progress"]);
}

/// A `git commit` after one made here holds exactly the work staged
/// meanwhile, and leaves the cloudmap as committed.
#[tokio::test]
async fn a_later_git_commit_holds_only_the_staged_work() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let dir = tmp.path();
    committed_by_git(dir);
    std::fs::write(dir.join("new.txt"), "new\n").expect("write");
    std::fs::write(dir.join("other.txt"), "staged edit\n").expect("write");
    git(dir, &["add", "new.txt", "other.txt"]);
    commit_an_edit_in_a_subdirectory(dir).await;

    git(
        dir,
        &[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e",
            "commit",
            "-qm",
            "staged work",
        ],
    );
    let changed = git_out(dir, &["diff", "--name-status", "HEAD~1", "HEAD"]);
    assert_eq!(changed, "A\tnew.txt\nM\tother.txt\n");
    git_out(dir, &["fsck", "--no-progress"]);
}

/// The handle on `maps/cloudmap.yaml` in `dir`, scanned, with an edit
/// pending.
async fn an_edit_in_a_subdirectory(dir: &Path) -> SyncedRepo {
    let db = format!("sqlite://{}?mode=rwc", dir.join(".git/sync.db").display());
    let sync = SyncedRepo::open_with(
        dir,
        unfurl_git_sync::DbConfig::Sqlite { url: db },
        unfurl_git_sync::FormatRegistry::with_builtins(),
        unfurl_git_sync::git::Trust::Full,
        OpenOptions::default().pathspecs([SUB_FILE]),
    )
    .await
    .expect("open");
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    sync.upsert_record(
        Some(SUB_FILE),
        REPOS,
        "git://e.com/new.git",
        json!({"path": "new"}),
        None,
        false,
    )
    .await
    .expect("upsert");
    sync
}

/// A split index is read whole and written whole: git reads the result as
/// the new commit's tree.
#[tokio::test]
async fn a_split_index_is_written_whole() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let dir = tmp.path();
    committed_by_git(dir);
    git(dir, &["update-index", "--split-index"]);
    let sync = an_edit_in_a_subdirectory(dir).await;
    sync.commit_repository("edit", Default::default())
        .await
        .expect("commit");
    assert_eq!(
        git_out(dir, &["write-tree"]),
        git_out(dir, &["rev-parse", "HEAD^{tree}"])
    );
    assert_eq!(status(dir, SUB_FILE), None);
    git_out(dir, &["fsck", "--no-progress"]);
}

/// A sparse index isn't updated by a commit made here: the commit fails
/// before anything is made.
#[tokio::test]
async fn a_sparse_index_is_refused() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let dir = tmp.path();
    committed_by_git(dir);
    std::fs::create_dir(dir.join("elsewhere")).expect("mkdir");
    std::fs::write(dir.join("elsewhere/x.txt"), "x\n").expect("write");
    git(dir, &["add", "elsewhere"]);
    git(
        dir,
        &[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e",
            "commit",
            "-qm",
            "elsewhere",
        ],
    );
    git(
        dir,
        &["sparse-checkout", "set", "--cone", "--sparse-index", "maps"],
    );
    let head = git_out(dir, &["rev-parse", "HEAD"]);

    let sync = an_edit_in_a_subdirectory(dir).await;
    let refused = sync.commit_repository("edit", Default::default()).await;
    assert!(
        matches!(&refused, Err(unfurl_git_sync::Error::Git(m)) if m.contains("sparse index")),
        "{refused:?}"
    );
    assert_eq!(git_out(dir, &["rev-parse", "HEAD"]), head);
}

/// A commit made here keeps an executable file executable, in the tree and
/// the index alike.
#[tokio::test]
async fn an_executable_file_stays_executable() {
    use std::os::unix::fs::PermissionsExt;
    let tmp = tempfile::tempdir().expect("tempdir");
    let dir = tmp.path();
    committed_by_git(dir);
    std::fs::set_permissions(dir.join(SUB_FILE), std::fs::Permissions::from_mode(0o755))
        .expect("chmod");
    git(dir, &["add", SUB_FILE]);
    git(
        dir,
        &[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e",
            "commit",
            "-qm",
            "executable",
        ],
    );
    commit_an_edit_in_a_subdirectory(dir).await;

    assert!(git_out(dir, &["ls-tree", "HEAD", SUB_FILE]).starts_with("100755 "));
    assert!(git_out(dir, &["ls-files", "-s", SUB_FILE]).starts_with("100755 "));
    assert_eq!(git_out(dir, &["diff", "--cached", "--name-only"]), "");
}
