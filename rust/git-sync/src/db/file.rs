// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `file` table reads and writes.

use crate::db::Db;
use crate::error::Result;

type FileRowSqlite = (i64, String, String, Option<String>);

#[cfg(feature = "postgres")]
type FileRowPg = (i64, String, String, Option<String>);

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
