// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `file` table reads and writes.

use crate::db::Db;
use crate::error::Result;

type FileRowSqlite = (i64, String, String, Option<String>, Option<String>);

#[cfg(feature = "postgres")]
type FileRowPg = (i64, String, String, Option<String>, Option<String>);

pub(crate) async fn get(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
) -> Result<Option<crate::model::File>> {
    match db {
        Db::Sqlite(pool) => {
            let row: Option<FileRowSqlite> = sqlx::query_as(
                "SELECT worktree_id, path, format, commit_id, source_oid \
                 FROM file WHERE worktree_id = ?1 AND path = ?2",
            )
            .bind(worktree_id)
            .bind(file_path)
            .fetch_optional(pool)
            .await?;
            Ok(row.map(
                |(wt, path, format, commit_id, source_oid)| crate::model::File {
                    worktree_id: wt,
                    path,
                    format,
                    commit_id,
                    source_oid,
                },
            ))
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let row: Option<FileRowPg> = sqlx::query_as(
                "SELECT worktree_id, path, format, commit_id, source_oid \
                 FROM file WHERE worktree_id = $1 AND path = $2",
            )
            .bind(worktree_id)
            .bind(file_path)
            .fetch_optional(pool)
            .await?;
            Ok(row.map(
                |(wt, path, format, commit_id, source_oid)| crate::model::File {
                    worktree_id: wt,
                    path,
                    format,
                    commit_id,
                    source_oid,
                },
            ))
        }
    }
}

/// Every file row of the worktree, in one query — the scan compares
/// each tracked file against these to decide whether it changed at all.
pub(crate) async fn list(db: &Db, worktree_id: i64) -> Result<Vec<crate::model::File>> {
    let map = |(wt, path, format, commit_id, source_oid): FileRowSqlite| crate::model::File {
        worktree_id: wt,
        path,
        format,
        commit_id,
        source_oid,
    };
    match db {
        Db::Sqlite(pool) => {
            let rows: Vec<FileRowSqlite> = sqlx::query_as(
                "SELECT worktree_id, path, format, commit_id, source_oid \
                 FROM file WHERE worktree_id = ?1",
            )
            .bind(worktree_id)
            .fetch_all(pool)
            .await?;
            Ok(rows.into_iter().map(map).collect())
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let rows: Vec<FileRowPg> = sqlx::query_as(
                "SELECT worktree_id, path, format, commit_id, source_oid \
                 FROM file WHERE worktree_id = $1",
            )
            .bind(worktree_id)
            .fetch_all(pool)
            .await?;
            Ok(rows.into_iter().map(map).collect())
        }
    }
}

/// Record `oid` as the file's source and run `persist` — the rename that
/// puts the new bytes in place — inside one transaction.
///
/// The two describe the same fact, so commit atomically:
///  `persist` failing drops the
/// transaction, leaving both untouched.
///
/// `persist` should be the rename only. The bytes are written and flushed
/// beforehand, so the transaction spans a constant-time operation rather
/// than an I/O proportional to the document — which matters because
/// holding it open is holding a row lock, and on SQLite that is the
/// single writer.
///
/// One window survives and cannot be closed: a crash between the rename
/// and the commit leaves the file ahead of the database. That direction
/// is the recoverable one — the bytes already contain the pending
/// records, so re-syncing takes them back in.
pub(crate) async fn commit_write<F>(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
    oid: &str,
    persist: F,
) -> Result<()>
where
    F: FnOnce() -> Result<()>,
{
    match db {
        Db::Sqlite(pool) => {
            let mut tx = pool.begin().await?;
            sqlx::query("UPDATE file SET source_oid = ?3 WHERE worktree_id = ?1 AND path = ?2")
                .bind(worktree_id)
                .bind(file_path)
                .bind(oid)
                .execute(&mut *tx)
                .await?;
            persist()?;
            tx.commit().await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let mut tx = pool.begin().await?;
            sqlx::query("UPDATE file SET source_oid = $3 WHERE worktree_id = $1 AND path = $2")
                .bind(worktree_id)
                .bind(file_path)
                .bind(oid)
                .execute(&mut *tx)
                .await?;
            persist()?;
            tx.commit().await?;
        }
    }
    Ok(())
}
