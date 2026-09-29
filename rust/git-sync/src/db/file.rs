// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `file` table reads and writes.

use crate::db::store::Store;
use crate::db::Db;
use crate::error::Result;
use crate::model::File;

async fn files<DB: Store>(
    pool: &sqlx::Pool<DB>,
    worktree_id: i64,
    path: Option<&str>,
) -> Result<Vec<File>> {
    let mut tx = pool.begin().await?;
    let files = DB::files(&mut tx, worktree_id, path).await?;
    tx.commit().await?;
    Ok(files)
}

pub(crate) async fn get(db: &Db, worktree_id: i64, file_path: &str) -> Result<Option<File>> {
    Ok(on_pool!(db, pool => files(pool, worktree_id, Some(file_path)).await)?.pop())
}

/// Every file row of the worktree, in one query — the scan compares
/// each tracked file against these to decide whether it changed at all.
pub(crate) async fn list(db: &Db, worktree_id: i64) -> Result<Vec<File>> {
    on_pool!(db, pool => files(pool, worktree_id, None).await)
}

/// Record that a file on disk now holds `oid`, together with `persist`,
/// the write that puts it there.
///
/// The two describe the same fact, so commit atomically: `persist`
/// failing drops the transaction, leaving both untouched.
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
    on_pool!(db, pool => {
        let mut tx = pool.begin().await?;
        Store::set_source_oid(&mut tx, worktree_id, file_path, oid).await?;
        persist()?;
        tx.commit().await?;
        Ok(())
    })
}

/// Mark, or clear, the database's intent to remove `file_path` from the
/// worktree. See [`crate::model::File::deleted`].
pub(crate) async fn set_deleted(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
    deleted: bool,
) -> Result<()> {
    on_pool!(db, pool => {
        let mut tx = pool.begin().await?;
        Store::set_file_deleted(&mut tx, worktree_id, file_path, deleted).await?;
        tx.commit().await?;
        Ok(())
    })
}
