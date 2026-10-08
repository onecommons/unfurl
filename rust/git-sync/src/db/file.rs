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
