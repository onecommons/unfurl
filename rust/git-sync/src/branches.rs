// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Branches of a worktree without a checkout of their own: forked at a
//! commit its committed chain holds, sharing its segments, and not cloned
//! at startup (docs/branch-segments.md §4.15).

use crate::db::store::Store;
use crate::db::tables::FileRow;
use crate::db::{self, on_pool};
use crate::error::{Error, Result};
use crate::model::Forked;
use crate::{fork, git, segments, SyncedRepo};

impl SyncedRepo {
    /// A worktree for `branch`, with no checkout, forked from this one at
    /// the commit its head is at, R, sharing every segment up to it. Its
    /// files are as R has them, and it isn't cloned at startup. `pushed` is
    /// where the branch's ref is going: R or a commit on top of it, such as
    /// a merge the database hasn't taken in. Calling it again once it's
    /// made returns the same worktree, as not created by this call: only
    /// that call's caller may take it to be its own to delete.
    ///
    /// # Errors
    ///
    /// [`Error::BranchExists`] when a worktree of `branch` that isn't this
    /// fork, or a local ref of it, exists; [`Error::Other`] when `pushed`
    /// doesn't descend from R, or R moved before the fork.
    pub async fn fork_branch(&self, branch: &str, pushed: &str) -> Result<Forked> {
        let worktree = self.get_worktree().await?;
        let Some(r) = worktree.commit_id else {
            return Err(Error::Other(format!(
                "{branch}: nothing is committed to fork"
            )));
        };
        if let Some(n) = db::worktree::find(self.db(), &worktree.origin, branch).await? {
            let made = db::worktree::get(self.db(), n).await?;
            return match made.commit_id.as_deref() == Some(r.as_str()) && !made.clone_on_start {
                true => Ok(Forked {
                    worktree_id: n,
                    created: false,
                }),
                false => Err(exists(branch)),
            };
        }
        let repo = self.repo()?;
        if git::branch_tip(&repo, branch)?.is_some() {
            return Err(exists(branch));
        }
        if pushed != r && !git::is_ancestor(&repo, &r, pushed)? {
            return Err(Error::Other(format!(
                "{branch}: {pushed} doesn't descend from {r}"
            )));
        }
        let blobs = git::tree_blobs(&repo, &r)?;
        let files = self.files_at(&blobs).await?;
        let database = db::commit::database_id(self.db()).await?;
        let mut history = crate::ids::History::new(self.formats(), repo, database);
        let at = At {
            origin: &worktree.origin,
            branch,
            commit: &r,
            files: &files,
        };
        let n = on_pool!(self.db(), pool => fork_in_pool(self, pool, &mut history, &at).await)?;
        Ok(Forked {
            worktree_id: n,
            created: true,
        })
    }

    /// Delete the worktree of `branch`, one with no checkout of its own
    /// that [`Self::fork_branch`] made, with what only it used; nothing
    /// when there's none.
    ///
    /// # Errors
    ///
    /// [`Error::Other`] when `branch` is this worktree's.
    pub async fn delete_branch(&self, branch: &str) -> Result<()> {
        let worktree = self.get_worktree().await?;
        if branch == worktree.branch {
            return Err(Error::Other(format!("{branch} is this worktree's own")));
        }
        match db::worktree::find(self.db(), &worktree.origin, branch).await? {
            Some(n) => fork::delete(self.db(), n, self.family_id()).await,
            None => Ok(()),
        }
    }

    /// Set whether a server cloning the worktrees clones this one's branch
    /// at startup.
    ///
    /// # Errors
    ///
    /// Returns [`Error::Db`] if the statement fails.
    pub async fn set_clone_on_start(&self, clone: bool) -> Result<()> {
        self.db()
            .set_clone_on_start(self.worktree_id(), clone)
            .await
    }

    /// Set whether this checkout holds commits its remote doesn't have yet.
    ///
    /// # Errors
    ///
    /// Returns [`Error::Db`] if the statement fails.
    pub async fn set_push_pending(&self, pending: bool) -> Result<()> {
        self.db()
            .set_push_pending(self.worktree_id(), pending)
            .await
    }
}

fn exists(branch: &str) -> Error {
    Error::BranchExists {
        branch: branch.to_string(),
    }
}

/// Where a branch is forked: its worktree's origin and branch, the commit,
/// and the files there (path, format, blob).
struct At<'a> {
    origin: &'a str,
    branch: &'a str,
    commit: &'a str,
    files: &'a [(String, String, String)],
}

async fn fork_in_pool<DB: Store>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    history: &mut crate::ids::History<'_>,
    at: &At<'_>,
) -> Result<i64> {
    let mut tx = pool.begin().await?;
    // before any read: SQLite can't take the write lock after one
    DB::lock_family(&mut tx, sync.family_id()).await?;
    let head = DB::head_commit(&mut tx, sync.worktree_id()).await?;
    if head.as_deref() != Some(at.commit) {
        return Err(Error::Other(format!(
            "{}: {} moved from {} before the fork",
            at.branch,
            sync.worktree_id(),
            at.commit
        )));
    }
    let Some(n) = fork::fork_in(
        &mut tx,
        history,
        sync.family_id(),
        at.origin,
        at.branch,
        at.commit,
    )
    .await?
    else {
        return Err(Error::Other(format!(
            "{}: {} falls nowhere in the family",
            at.branch, at.commit
        )));
    };
    // its files, as the commit has them: a fork has none, and this one has
    // no checkout to scan
    for (path, format, blob) in at.files {
        let row = FileRow {
            path,
            format,
            commit_id: Some(Some(at.commit)),
            source_oid: Some(Some(blob)),
            committed_oid: Some(Some(blob)),
        };
        DB::upsert_file(&mut tx, n, &row).await?;
    }
    DB::no_clone_on_start(&mut tx, n).await?;
    segments::relink(&mut tx, n).await?;
    tx.commit().await?;
    Ok(n)
}
