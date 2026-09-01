// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `file` table reads and writes.

use crate::db::Db;
use crate::error::Result;

type FileRowSqlite = (i64, String, String, Option<String>, Option<String>, i64);

#[cfg(feature = "postgres")]
type FileRowPg = (i64, String, String, Option<String>, Option<String>, bool);

pub(crate) async fn get(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
) -> Result<Option<crate::model::File>> {
    match db {
        Db::Sqlite(pool) => {
            let row: Option<FileRowSqlite> = sqlx::query_as(
                "SELECT worktree_id, path, format, commit_id, source_oid, deleted \
                 FROM file WHERE worktree_id = ?1 AND path = ?2",
            )
            .bind(worktree_id)
            .bind(file_path)
            .fetch_optional(pool)
            .await?;
            Ok(row.map(
                |(wt, path, format, commit_id, source_oid, deleted)| crate::model::File {
                    worktree_id: wt,
                    path,
                    format,
                    commit_id,
                    source_oid,
                    deleted: deleted != 0,
                },
            ))
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let row: Option<FileRowPg> = sqlx::query_as(
                "SELECT worktree_id, path, format, commit_id, source_oid, deleted \
                 FROM file WHERE worktree_id = $1 AND path = $2",
            )
            .bind(worktree_id)
            .bind(file_path)
            .fetch_optional(pool)
            .await?;
            Ok(row.map(
                |(wt, path, format, commit_id, source_oid, deleted)| crate::model::File {
                    worktree_id: wt,
                    path,
                    format,
                    commit_id,
                    source_oid,
                    deleted,
                },
            ))
        }
    }
}

/// Every file row of the worktree, in one query — the scan compares
/// each tracked file against these to decide whether it changed at all.
pub(crate) async fn list(db: &Db, worktree_id: i64) -> Result<Vec<crate::model::File>> {
    // The two row shapes no longer agree -- SQLite decodes `deleted` as
    // INTEGER and Postgres as BOOLEAN -- so each arm maps its own.
    match db {
        Db::Sqlite(pool) => {
            let rows: Vec<FileRowSqlite> = sqlx::query_as(
                "SELECT worktree_id, path, format, commit_id, source_oid, deleted \
                 FROM file WHERE worktree_id = ?1",
            )
            .bind(worktree_id)
            .fetch_all(pool)
            .await?;
            Ok(rows
                .into_iter()
                .map(
                    |(wt, path, format, commit_id, source_oid, deleted)| crate::model::File {
                        worktree_id: wt,
                        path,
                        format,
                        commit_id,
                        source_oid,
                        deleted: deleted != 0,
                    },
                )
                .collect())
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let rows: Vec<FileRowPg> = sqlx::query_as(
                "SELECT worktree_id, path, format, commit_id, source_oid, deleted \
                 FROM file WHERE worktree_id = $1",
            )
            .bind(worktree_id)
            .fetch_all(pool)
            .await?;
            Ok(rows
                .into_iter()
                .map(
                    |(wt, path, format, commit_id, source_oid, deleted)| crate::model::File {
                        worktree_id: wt,
                        path,
                        format,
                        commit_id,
                        source_oid,
                        deleted,
                    },
                )
                .collect())
        }
    }
}

/// Point `file_path`'s row and its synced records at fresh commit
/// attribution, in one transaction.
///
/// The content-free counterpart of a re-scan, for a file whose bytes
/// the database already took in (`source_oid` match) but whose git
/// state moved — e.g. a hand edit taken in while dirty was since
/// committed outside this database. Pending records (`commit_id IS
/// NULL`) and `version`s are untouched, so `Pending` OCC tokens stay
/// valid and `list_changes` cursors don't surface a change that never
/// altered content.
pub(crate) async fn reattribute(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
    file_commit: Option<&str>,
    record_commit: Option<&str>,
) -> Result<()> {
    match db {
        Db::Sqlite(pool) => {
            let mut tx = pool.begin().await?;
            sqlx::query("UPDATE file SET commit_id = ?3 WHERE worktree_id = ?1 AND path = ?2")
                .bind(worktree_id)
                .bind(file_path)
                .bind(file_commit)
                .execute(&mut *tx)
                .await?;
            sqlx::query(
                "UPDATE record SET commit_id = ?3 \
                 WHERE worktree_id = ?1 AND file_path = ?2 AND commit_id IS NOT NULL",
            )
            .bind(worktree_id)
            .bind(file_path)
            .bind(record_commit)
            .execute(&mut *tx)
            .await?;
            tx.commit().await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let mut tx = pool.begin().await?;
            sqlx::query("UPDATE file SET commit_id = $3 WHERE worktree_id = $1 AND path = $2")
                .bind(worktree_id)
                .bind(file_path)
                .bind(file_commit)
                .execute(&mut *tx)
                .await?;
            sqlx::query(
                "UPDATE record SET commit_id = $3 \
                 WHERE worktree_id = $1 AND file_path = $2 AND commit_id IS NOT NULL",
            )
            .bind(worktree_id)
            .bind(file_path)
            .bind(record_commit)
            .execute(&mut *tx)
            .await?;
            tx.commit().await?;
        }
    }
    Ok(())
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

/// Mark, or clear, the database's intent to remove `file_path` from the
/// worktree. See [`crate::model::File::deleted`].
pub(crate) async fn set_deleted(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
    deleted: bool,
) -> Result<()> {
    match db {
        Db::Sqlite(pool) => {
            sqlx::query("UPDATE file SET deleted = ?3 WHERE worktree_id = ?1 AND path = ?2")
                .bind(worktree_id)
                .bind(file_path)
                .bind(i64::from(deleted))
                .execute(pool)
                .await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            sqlx::query("UPDATE file SET deleted = $3 WHERE worktree_id = $1 AND path = $2")
                .bind(worktree_id)
                .bind(file_path)
                .bind(deleted)
                .execute(pool)
                .await?;
        }
    }
    Ok(())
}

/// Move every row of `from` to `to`, keeping the records themselves --
/// ids, versions, bases, pending flags, conflict rows and aliases all
/// stay put, so a `Pending` token minted before the move is still good
/// after it.
///
/// Ordered rather than done with an `ON UPDATE CASCADE`, which the
/// schema deliberately does not have: insert the destination row,
/// re-point the records, then drop the source, whose cascade then finds
/// nothing left to take.
pub(crate) async fn rename(db: &Db, worktree_id: i64, from: &str, to: &str) -> Result<()> {
    match db {
        Db::Sqlite(pool) => {
            let mut tx = pool.begin().await?;
            sqlx::query(
                "INSERT INTO file (worktree_id, path, format, commit_id, source_oid, deleted) \
                 SELECT worktree_id, ?3, format, commit_id, source_oid, deleted FROM file \
                 WHERE worktree_id = ?1 AND path = ?2",
            )
            .bind(worktree_id)
            .bind(from)
            .bind(to)
            .execute(&mut *tx)
            .await?;
            sqlx::query(
                "UPDATE record SET file_path = ?3 WHERE worktree_id = ?1 AND file_path = ?2",
            )
            .bind(worktree_id)
            .bind(from)
            .bind(to)
            .execute(&mut *tx)
            .await?;
            sqlx::query("DELETE FROM file WHERE worktree_id = ?1 AND path = ?2")
                .bind(worktree_id)
                .bind(from)
                .execute(&mut *tx)
                .await?;
            tx.commit().await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let mut tx = pool.begin().await?;
            sqlx::query(
                "INSERT INTO file (worktree_id, path, format, commit_id, source_oid, deleted) \
                 SELECT worktree_id, $3, format, commit_id, source_oid, deleted FROM file \
                 WHERE worktree_id = $1 AND path = $2",
            )
            .bind(worktree_id)
            .bind(from)
            .bind(to)
            .execute(&mut *tx)
            .await?;
            sqlx::query(
                "UPDATE record SET file_path = $3 WHERE worktree_id = $1 AND file_path = $2",
            )
            .bind(worktree_id)
            .bind(from)
            .bind(to)
            .execute(&mut *tx)
            .await?;
            sqlx::query("DELETE FROM file WHERE worktree_id = $1 AND path = $2")
                .bind(worktree_id)
                .bind(from)
                .execute(&mut *tx)
                .await?;
            tx.commit().await?;
        }
    }
    Ok(())
}
