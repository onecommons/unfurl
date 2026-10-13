// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A branch forked from a worktree at its committed head, with no checkout
//! of its own (docs/branch-segments.md §4.15), on both backends.

mod common;

use common::{crud_test, head_commit, upsert_op};
use tempfile::TempDir;
use unfurl_git_sync::{CommitId, Error, RecordQuery, ScanOptions, SyncedRepo, WorktreeFilter};

const BRANCH: &str = "git-sync/diverted-abc";

fn git(tmp: &TempDir, args: &[&str]) -> String {
    let out = std::process::Command::new("git")
        .args(["-c", "user.name=t", "-c", "user.email=t@example.com"])
        .args(["-c", "commit.gpgsign=false"])
        .args(args)
        .current_dir(tmp.path())
        .output()
        .expect("git");
    assert!(out.status.success(), "git {args:?}: {out:?}");
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

fn of(branch: &str) -> WorktreeFilter {
    WorktreeFilter {
        origin: None,
        branch: Some(branch.into()),
        ids: None,
    }
}

/// The records `sync`'s database has on `branch`, as `(key, id)`, sorted.
async fn records_on(sync: &SyncedRepo, branch: &str) -> Vec<(String, i64)> {
    let query = RecordQuery {
        worktrees: Some(of(branch)),
        ..Default::default()
    };
    let mut seen: Vec<_> = sync
        .find_records(&query)
        .await
        .expect("records")
        .into_iter()
        .map(|r| (r.key, r.id))
        .collect();
    seen.sort();
    seen
}

/// A fork has the worktree's committed records, with their ids, and none
/// of its staged ones; it isn't cloned at startup, and its files are
/// committed: it holds no work of its own.
async fn a_fork_has_the_committed_records(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let main = sync.get_worktree().await.expect("worktree").branch;
    let committed = records_on(sync, &main).await;
    assert!(!committed.is_empty());
    sync.apply_batch(vec![upsert_op("staged")], true, None)
        .await
        .expect("a staged edit");
    let r = head_commit(sync).await;
    let forked = sync.fork_branch(BRANCH, r).await.expect("fork");
    assert!(forked.created);
    let n = forked.worktree_id;
    assert_eq!(records_on(sync, BRANCH).await, committed);
    let rows = sync.worktrees(&of(BRANCH)).await.expect("worktrees");
    assert_eq!(rows[0].id, n);
    assert_eq!(rows[0].commit_id, Some(r));
    assert!(!rows[0].clone_on_start);
    assert!(!sync.db().has_uncommitted(n).await.expect("uncommitted"));
    let own = sync.get_worktree().await.expect("worktree");
    assert!(own.clone_on_start, "the worktree's own is cloned");
    // made again: the same, not created by this call
    let again = sync.fork_branch(BRANCH, r).await.expect("again");
    assert_eq!((again.worktree_id, again.created), (n, false));
}

/// It forks at the commit the database has, though the branch's ref is
/// going to a commit on top of it; but not at one that isn't.
async fn a_fork_is_at_the_recorded_commit(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let r = head_commit(sync).await;
    git(tmp, &["commit", "-q", "--allow-empty", "-m", "on top"]);
    let on_top: CommitId = git(tmp, &["rev-parse", "HEAD"]).parse().expect("a commit");
    sync.fork_branch(BRANCH, on_top).await.expect("fork");
    let rows = sync.worktrees(&of(BRANCH)).await.expect("worktrees");
    assert_eq!(rows[0].commit_id, Some(r));
    git(tmp, &["checkout", "-q", "--orphan", "unrelated"]);
    git(tmp, &["commit", "-q", "--allow-empty", "-m", "unrelated"]);
    let unrelated: CommitId = git(tmp, &["rev-parse", "HEAD"]).parse().expect("a commit");
    let refused = sync.fork_branch("git-sync/other", unrelated).await;
    assert!(matches!(refused, Err(Error::Other(_))), "{refused:?}");
}

/// A branch with a local ref, or a worktree that isn't this fork, is
/// refused.
async fn an_existing_branch_is_refused(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let r = head_commit(sync).await;
    git(tmp, &["branch", "git-sync/taken"]);
    let taken = sync.fork_branch("git-sync/taken", r).await;
    assert!(
        matches!(taken, Err(Error::BranchExists { .. })),
        "{taken:?}"
    );
    let main = sync.get_worktree().await.expect("worktree").branch;
    let own = sync.fork_branch(&main, r).await;
    assert!(matches!(own, Err(Error::BranchExists { .. })), "{own:?}");
}

/// Deleting a fork takes its worktree, and leaves the worktree's own.
async fn a_fork_is_deleted(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let r = head_commit(sync).await;
    sync.fork_branch(BRANCH, r).await.expect("fork");
    sync.delete_branch(BRANCH).await.expect("delete");
    assert!(sync
        .worktrees(&of(BRANCH))
        .await
        .expect("worktrees")
        .is_empty());
    let main = sync.get_worktree().await.expect("worktree").branch;
    assert!(!records_on(sync, &main).await.is_empty());
    sync.delete_branch(BRANCH).await.expect("none: nothing");
    assert!(sync.delete_branch(&main).await.is_err(), "not its own");
}

crud_test!(a_fork_has_the_committed_records);
crud_test!(a_fork_is_at_the_recorded_commit);
crud_test!(an_existing_branch_is_refused);
crud_test!(a_fork_is_deleted);
