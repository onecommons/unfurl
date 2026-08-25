// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `worktree` table reads and writes.

use crate::db::Db;
use crate::error::Result;

/// Find or create the row for `(origin, branch)`.
///
/// `origin` must already be [`crate::git::normalize_git_url_hard`]
/// output — the match is an exact string compare, so a raw URL would
/// create a second row for a repository that already has one, splitting
/// its records and its version counter. Callers derive it via
/// [`crate::git::worktree_meta`] rather than passing a remote URL
/// through.
pub(crate) async fn upsert(db: &Db, origin: &str, branch: &str) -> Result<i64> {
    match db {
        Db::Sqlite(pool) => {
            let row: Option<(i64,)> =
                sqlx::query_as("SELECT id FROM worktree WHERE origin = ?1 AND branch = ?2")
                    .bind(origin)
                    .bind(branch)
                    .fetch_optional(pool)
                    .await?;
            if let Some((id,)) = row {
                return Ok(id);
            }
            let row: (i64,) = sqlx::query_as(
                "INSERT INTO worktree (origin, branch) VALUES (?1, ?2) RETURNING id",
            )
            .bind(origin)
            .bind(branch)
            .fetch_one(pool)
            .await?;
            Ok(row.0)
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let row: Option<(i64,)> =
                sqlx::query_as("SELECT id FROM worktree WHERE origin = $1 AND branch = $2")
                    .bind(origin)
                    .bind(branch)
                    .fetch_optional(pool)
                    .await?;
            if let Some((id,)) = row {
                return Ok(id);
            }
            let row: (i64,) = sqlx::query_as(
                "INSERT INTO worktree (origin, branch) VALUES ($1, $2) RETURNING id",
            )
            .bind(origin)
            .bind(branch)
            .fetch_one(pool)
            .await?;
            Ok(row.0)
        }
    }
}

pub(crate) async fn update_commit(db: &Db, worktree_id: i64, commit: Option<&str>) -> Result<()> {
    match db {
        Db::Sqlite(pool) => {
            sqlx::query("UPDATE worktree SET commit_id = ?1 WHERE id = ?2")
                .bind(commit)
                .bind(worktree_id)
                .execute(pool)
                .await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            sqlx::query("UPDATE worktree SET commit_id = $1 WHERE id = $2")
                .bind(commit)
                .bind(worktree_id)
                .execute(pool)
                .await?;
        }
    }
    Ok(())
}

pub(crate) async fn get(db: &Db, worktree_id: i64) -> Result<crate::model::Worktree> {
    let row: (i64, String, String, Option<String>, Option<String>) = match db {
        Db::Sqlite(pool) => {
            sqlx::query_as(
                "SELECT id, origin, branch, commit_id, default_file_path \
                 FROM worktree WHERE id = ?1",
            )
            .bind(worktree_id)
            .fetch_one(pool)
            .await?
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            sqlx::query_as(
                "SELECT id, origin, branch, commit_id, default_file_path \
                 FROM worktree WHERE id = $1",
            )
            .bind(worktree_id)
            .fetch_one(pool)
            .await?
        }
    };
    Ok(crate::model::Worktree {
        id: row.0,
        origin: row.1,
        branch: row.2,
        commit_id: row.3,
        default_file_path: row.4,
    })
}

/// The next value [`crate::db::tx::next_version`] will hand out — one
/// past the highest version stamped so far.
///
/// Read (not drawn) at commit time so the commit message can record the
/// counter's high-water mark. It is a snapshot: a concurrent batch may
/// draw again before the commit lands, which is fine — the value only
/// has to cover the writes this commit carries, and the next commit's
/// trailer covers the rest.
pub(crate) async fn next_version(db: &Db, worktree_id: i64) -> Result<i64> {
    let row: (i64,) = match db {
        Db::Sqlite(pool) => {
            sqlx::query_as("SELECT next_version FROM worktree WHERE id = ?1")
                .bind(worktree_id)
                .fetch_one(pool)
                .await?
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            sqlx::query_as("SELECT next_version FROM worktree WHERE id = $1")
                .bind(worktree_id)
                .fetch_one(pool)
                .await?
        }
    };
    Ok(row.0)
}

/// Auto-pick `worktree.default_file_path` if not already set.
///
/// Run once at the end of [`crate::SyncedRepo::update_from_working_dir`].
/// `COALESCE` keeps the existing value when set (so operator
/// overrides survive re-syncs) and otherwise drops in the smallest
/// `file_path` that contributed a record. `MIN()` is deterministic
/// and supported identically on both backends; when no records
/// exist it returns NULL and the column stays NULL.
pub(crate) async fn auto_pick_default_file(db: &Db, worktree_id: i64) -> Result<()> {
    match db {
        Db::Sqlite(pool) => {
            sqlx::query(
                "UPDATE worktree \
                 SET default_file_path = COALESCE( \
                     default_file_path, \
                     (SELECT MIN(file_path) FROM record WHERE worktree_id = ?1)) \
                 WHERE id = ?1",
            )
            .bind(worktree_id)
            .execute(pool)
            .await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            sqlx::query(
                "UPDATE worktree \
                 SET default_file_path = COALESCE( \
                     default_file_path, \
                     (SELECT MIN(file_path) FROM record WHERE worktree_id = $1)) \
                 WHERE id = $1",
            )
            .bind(worktree_id)
            .execute(pool)
            .await?;
        }
    }
    Ok(())
}

/// Unconditionally set `worktree.default_file_path` (or clear it
/// when `value == None`). Used by operators to override the auto-pick
/// done by `update_from_working_dir` on first run.
pub(crate) async fn set_default_file(db: &Db, worktree_id: i64, value: Option<&str>) -> Result<()> {
    match db {
        Db::Sqlite(pool) => {
            sqlx::query("UPDATE worktree SET default_file_path = ?2 WHERE id = ?1")
                .bind(worktree_id)
                .bind(value)
                .execute(pool)
                .await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            sqlx::query("UPDATE worktree SET default_file_path = $2 WHERE id = $1")
                .bind(worktree_id)
                .bind(value)
                .execute(pool)
                .await?;
        }
    }
    Ok(())
}
