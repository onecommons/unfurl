// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `file` table reads and writes.

use crate::db::Db;
use crate::error::Result;

type FileRowSqlite = (i64, String, String, Option<String>);

#[cfg(feature = "postgres")]
type FileRowPg = (i64, String, String, Option<String>);

pub(crate) async fn upsert(
    db: &Db,
    worktree_id: i64,
    path: &str,
    format: &str,
    commit_id: Option<&str>,
) -> Result<()> {
    match db {
        Db::Sqlite(pool) => {
            sqlx::query(
                "INSERT INTO file (worktree_id, path, format, commit_id) \
                 VALUES (?1, ?2, ?3, ?4) \
                 ON CONFLICT(worktree_id, path) DO UPDATE SET \
                   format = excluded.format, \
                   commit_id = excluded.commit_id",
            )
            .bind(worktree_id)
            .bind(path)
            .bind(format)
            .bind(commit_id)
            .execute(pool)
            .await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            sqlx::query(
                "INSERT INTO file (worktree_id, path, format, commit_id) \
                 VALUES ($1, $2, $3, $4) \
                 ON CONFLICT(worktree_id, path) DO UPDATE SET \
                   format = EXCLUDED.format, \
                   commit_id = EXCLUDED.commit_id",
            )
            .bind(worktree_id)
            .bind(path)
            .bind(format)
            .bind(commit_id)
            .execute(pool)
            .await?;
        }
    }
    Ok(())
}

pub(crate) async fn get(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
) -> Result<Option<crate::model::File>> {
    match db {
        Db::Sqlite(pool) => {
            let row: Option<FileRowSqlite> = sqlx::query_as(
                "SELECT worktree_id, path, format, commit_id \
                 FROM file WHERE worktree_id = ?1 AND path = ?2",
            )
            .bind(worktree_id)
            .bind(file_path)
            .fetch_optional(pool)
            .await?;
            Ok(row.map(|(wt, path, format, commit_id)| crate::model::File {
                worktree_id: wt,
                path,
                format,
                commit_id,
            }))
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let row: Option<FileRowPg> = sqlx::query_as(
                "SELECT worktree_id, path, format, commit_id \
                 FROM file WHERE worktree_id = $1 AND path = $2",
            )
            .bind(worktree_id)
            .bind(file_path)
            .fetch_optional(pool)
            .await?;
            Ok(row.map(|(wt, path, format, commit_id)| crate::model::File {
                worktree_id: wt,
                path,
                format,
                commit_id,
            }))
        }
    }
}
