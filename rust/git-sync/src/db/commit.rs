// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Commit-roll-forward sequence — used when `commit_repository` finalises
//! a new commit and rolls its oid into all in-flight rows.

use crate::db::Db;
use crate::error::Result;

pub(crate) async fn roll_forward(
    db: &Db,
    worktree_id: i64,
    files: &[String],
    new_commit: &str,
) -> Result<()> {
    if files.is_empty() {
        return Ok(());
    }
    // Order of operations:
    //   1. roll commit forward on live, in-flight rows;
    //   2. purge tombstones (their on-disk effect is already in the
    //      commit);
    //   3. roll commit forward on file rows;
    //   4. roll commit forward on the worktree row.
    match db {
        Db::Sqlite(pool) => {
            let mut tx = pool.begin().await?;
            sqlx::query(
                "UPDATE record SET commit_id = ?1 \
                 WHERE worktree_id = ?2 AND commit_id IS NULL AND deleted = 0",
            )
            .bind(new_commit)
            .bind(worktree_id)
            .execute(&mut *tx)
            .await?;
            sqlx::query("DELETE FROM record WHERE worktree_id = ?1 AND deleted = 1")
                .bind(worktree_id)
                .execute(&mut *tx)
                .await?;
            let placeholders: Vec<String> =
                (0..files.len()).map(|i| format!("?{}", i + 3)).collect();
            let sql = format!(
                "UPDATE file SET commit_id = ?1 WHERE worktree_id = ?2 AND path IN ({})",
                placeholders.join(",")
            );
            let mut q = sqlx::query(&sql).bind(new_commit).bind(worktree_id);
            for f in files {
                q = q.bind(f);
            }
            q.execute(&mut *tx).await?;
            sqlx::query("UPDATE worktree SET commit_id = ?1 WHERE id = ?2")
                .bind(new_commit)
                .bind(worktree_id)
                .execute(&mut *tx)
                .await?;
            tx.commit().await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let mut tx = pool.begin().await?;
            sqlx::query(
                "UPDATE record SET commit_id = $1 \
                 WHERE worktree_id = $2 AND commit_id IS NULL AND deleted = FALSE",
            )
            .bind(new_commit)
            .bind(worktree_id)
            .execute(&mut *tx)
            .await?;
            sqlx::query("DELETE FROM record WHERE worktree_id = $1 AND deleted = TRUE")
                .bind(worktree_id)
                .execute(&mut *tx)
                .await?;
            sqlx::query("UPDATE file SET commit_id = $1 WHERE worktree_id = $2 AND path = ANY($3)")
                .bind(new_commit)
                .bind(worktree_id)
                .bind(files)
                .execute(&mut *tx)
                .await?;
            sqlx::query("UPDATE worktree SET commit_id = $1 WHERE id = $2")
                .bind(new_commit)
                .bind(worktree_id)
                .execute(&mut *tx)
                .await?;
            tx.commit().await?;
        }
    }
    Ok(())
}
