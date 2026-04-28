// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `worktree` table reads and writes.

use crate::db::Db;
use crate::error::Result;

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
    let row: (i64, String, String, Option<String>) = match db {
        Db::Sqlite(pool) => {
            sqlx::query_as("SELECT id, origin, branch, commit_id FROM worktree WHERE id = ?1")
                .bind(worktree_id)
                .fetch_one(pool)
                .await?
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            sqlx::query_as("SELECT id, origin, branch, commit_id FROM worktree WHERE id = $1")
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
    })
}
