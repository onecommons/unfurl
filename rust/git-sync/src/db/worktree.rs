// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `worktree` table reads and writes.

use crate::db::Db;
use crate::error::Result;

/// The row for `(origin, branch)`, if there is one.
///
/// `origin` must already be [`crate::git::normalize_git_url_hard`]
/// output — the match is an exact string compare, so a raw URL would
/// create a second row for a repository that already has one, splitting
/// its records and its version counter. Callers derive it via
/// [`crate::git::worktree_meta`] rather than passing a remote URL
/// through.
pub(crate) async fn find(db: &Db, origin: &str, branch: &str) -> Result<Option<i64>> {
    let row: Option<(i64,)> = on_pool!(db, pool => {
        sqlx::query_as(sql!(pool, "SELECT id FROM worktree WHERE origin = ?1 AND branch = ?2"))
            .bind(origin)
            .bind(branch)
            .fetch_optional(pool)
            .await?
    });
    Ok(row.map(|(id,)| id))
}

pub(crate) async fn update_commit(db: &Db, worktree_id: i64, commit: Option<&str>) -> Result<()> {
    on_pool!(db, pool => {
        sqlx::query(sql!(pool, "UPDATE worktree SET commit_id = ?1 WHERE id = ?2"))
            .bind(commit)
            .bind(worktree_id)
            .execute(pool)
            .await?;
    });
    Ok(())
}

/// Every worktree `filter` matches, by id.
pub(crate) async fn matching(
    db: &Db,
    filter: &crate::model::WorktreeFilter,
) -> Result<Vec<crate::model::Worktree>> {
    const SQL: &str = "SELECT id, origin, branch, commit_id, default_file_path FROM worktree \
         WHERE (CAST(?1 AS TEXT) IS NULL OR origin = ?1) \
           AND (CAST(?2 AS TEXT) IS NULL OR branch = ?2) ORDER BY id";
    let (origin, branch) = filter.normalized();
    Ok(on_pool!(db, pool => {
        sqlx::query_as(sql!(pool, SQL))
            .bind(origin)
            .bind(branch)
            .fetch_all(pool)
            .await?
    }))
}

pub(crate) async fn get(db: &Db, worktree_id: i64) -> Result<crate::model::Worktree> {
    const SQL: &str =
        "SELECT id, origin, branch, commit_id, default_file_path FROM worktree WHERE id = ?1";
    Ok(on_pool!(db, pool => {
        sqlx::query_as(sql!(pool, SQL))
            .bind(worktree_id)
            .fetch_one(pool)
            .await?
    }))
}

/// The root worktree of `worktree_id`'s family: the upstream it and its
/// sibling forks and drafts all draw versions from.
///
/// `COALESCE(family_id, id)` — a worktree with no family recorded is its
/// own, so a row that predates the column, or one written by a path that
/// forgot to set it, still resolves to a usable sequence rather than to
/// NULL.
pub(crate) async fn family_id(db: &Db, worktree_id: i64) -> Result<i64> {
    let row: (i64,) = on_pool!(db, pool => {
        sqlx::query_as(sql!(pool, "SELECT COALESCE(family_id, id) FROM worktree WHERE id = ?1"))
            .bind(worktree_id)
            .fetch_one(pool)
            .await?
    });
    Ok(row.0)
}

/// The next value the family's counter will hand out — one
/// past the highest version stamped so far.
///
/// Read (not drawn) at commit time so the commit message can record the
/// counter's high-water mark. It is a snapshot: a concurrent batch may
/// draw again before the commit lands, which is fine — the value only
/// has to cover the writes this commit carries, and the next commit's
/// trailer covers the rest.
pub(crate) async fn next_version(db: &Db, worktree_id: i64) -> Result<i64> {
    let row: (i64,) = on_pool!(db, pool => {
        sqlx::query_as(sql!(
            pool,
            "SELECT s.next_version FROM version_seq s JOIN worktree w \
             ON s.worktree_id = COALESCE(w.family_id, w.id) WHERE w.id = ?1"
        ))
        .bind(worktree_id)
        .fetch_one(pool)
        .await?
    });
    Ok(row.0)
}

/// Auto-pick `worktree.default_file_path` if not already set.
///
/// Run once at the end of [`crate::SyncedRepo::update_from_working_dir`].
/// `COALESCE` keeps the existing value when set (so operator
/// overrides survive re-syncs) and otherwise drops in the smallest
/// file the worktree has. `MIN()` is deterministic
/// and supported identically on both backends; when no records
/// exist it returns NULL and the column stays NULL.
pub(crate) async fn auto_pick_default_file(db: &Db, worktree_id: i64) -> Result<()> {
    on_pool!(db, pool => {
        sqlx::query(sql!(
            pool,
            "UPDATE worktree \
             SET default_file_path = COALESCE( \
                 default_file_path, \
                 (SELECT MIN(path) FROM file WHERE worktree_id = ?1)) \
             WHERE id = ?1"
        ))
        .bind(worktree_id)
        .execute(pool)
        .await?;
    });
    Ok(())
}

/// Unconditionally set `worktree.default_file_path` (or clear it
/// when `value == None`). Used by operators to override the auto-pick
/// done by `update_from_working_dir` on first run.
pub(crate) async fn set_default_file(db: &Db, worktree_id: i64, value: Option<&str>) -> Result<()> {
    on_pool!(db, pool => {
        sqlx::query(sql!(pool, "UPDATE worktree SET default_file_path = ?2 WHERE id = ?1"))
            .bind(worktree_id)
            .bind(value)
            .execute(pool)
            .await?;
    });
    Ok(())
}
