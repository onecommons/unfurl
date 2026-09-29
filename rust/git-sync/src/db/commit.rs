// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! The `txn` audit rows a commit reports, and the database identity it
//! names its ids under.

use crate::db::Db;
use crate::error::Result;

/// `(id, worktree_id, first_version, last_version, author, message,
/// created_at, commit_id)` — the column order of [`TXN_COLUMNS`].
type TxnRow = (
    i64,
    i64,
    i64,
    i64,
    Option<String>,
    Option<String>,
    String,
    Option<String>,
);

const TXN_COLUMNS: &str =
    "id, worktree_id, first_version, last_version, author, message, created_at, commit_id";

fn to_txn(row: TxnRow) -> crate::model::Txn {
    let (id, worktree_id, first_version, last_version, author, message, created_at, commit_id) =
        row;
    crate::model::Txn {
        id,
        worktree_id,
        first_version,
        last_version,
        author,
        message,
        created_at,
        commit_id,
    }
}

/// The worktree's batch-audit rows that haven't reached a git commit
/// yet, oldest range first. Read by
/// [`crate::SyncedRepo::commit_repository`] to build the rollup section
/// of the commit message, just before the fold stamps them.
pub(crate) async fn list_outstanding(db: &Db, worktree_id: i64) -> Result<Vec<crate::model::Txn>> {
    list_where(db, worktree_id, true).await
}

/// Every batch-audit row of the worktree, oldest range first.
pub(crate) async fn list_all(db: &Db, worktree_id: i64) -> Result<Vec<crate::model::Txn>> {
    list_where(db, worktree_id, false).await
}

async fn list_where(
    db: &Db,
    worktree_id: i64,
    outstanding_only: bool,
) -> Result<Vec<crate::model::Txn>> {
    let filter = if outstanding_only {
        " AND commit_id IS NULL"
    } else {
        ""
    };
    match db {
        Db::Sqlite(pool) => {
            let sql = format!(
                "SELECT {TXN_COLUMNS} FROM txn WHERE worktree_id = ?1{filter} \
                 ORDER BY first_version, id"
            );
            let rows: Vec<TxnRow> = sqlx::query_as(&sql)
                .bind(worktree_id)
                .fetch_all(pool)
                .await?;
            Ok(rows.into_iter().map(to_txn).collect())
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let sql = format!(
                "SELECT {TXN_COLUMNS} FROM txn WHERE worktree_id = $1{filter} \
                 ORDER BY first_version, id"
            );
            let rows: Vec<TxnRow> = sqlx::query_as(&sql)
                .bind(worktree_id)
                .fetch_all(pool)
                .await?;
            Ok(rows.into_iter().map(to_txn).collect())
        }
    }
}

/// This database's identity, which a commit's rollup names its ids under.
pub(crate) async fn database_id(db: &Db) -> Result<String> {
    const SQL: &str = "SELECT uuid FROM database_identity";
    Ok(match db {
        Db::Sqlite(pool) => sqlx::query_scalar(SQL).fetch_one(pool).await?,
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => sqlx::query_scalar(SQL).fetch_one(pool).await?,
    })
}
