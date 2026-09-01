// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Commit-roll-forward sequence — used when `commit_repository` finalises
//! a new commit and rolls its oid into the committed files' rows.

use crate::db::Db;
use crate::error::Result;

/// True when the row this is attached to has a conflict sibling — the
/// file's own view of the same record, materialized because the two
/// sides disagree.
///
/// Correlated against the statement's target table by name, so it reads
/// the same in an UPDATE and a DELETE and on both backends.
const HAS_CONFLICT_SIBLING: &str = "EXISTS (SELECT 1 FROM record c \
     WHERE c.worktree_id = record.worktree_id AND c.file_path = record.file_path \
       AND c.path = record.path AND c.key = record.key AND c.conflict IS NOT NULL)";

/// `(rows this commit carries, tombstones it may purge)` as WHERE
/// fragments, spelled with the caller's boolean literals — SQLite stores
/// `deleted` as INTEGER and Postgres as BOOLEAN.
fn record_predicates(false_lit: &str, true_lit: &str) -> (String, String) {
    (
        format!("conflict IS NOT NULL OR (deleted = {false_lit} AND NOT {HAS_CONFLICT_SIBLING})"),
        format!("deleted = {true_lit} AND conflict IS NULL AND NOT {HAS_CONFLICT_SIBLING}"),
    )
}

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
    //   1. roll commit forward on the committed files' live rows —
    //      scoped to `files` rather than `commit_id IS NULL`, both to
    //      re-attribute previously-scanned rows of hand-edited files
    //      and to leave rows written concurrently to *other* files
    //      alone. `base_commit_id` clears with it: a committed row is
    //      no longer diverged from the file;
    //   2. purge those files' tombstones (their on-disk effect is in
    //      the commit — a concurrent tombstone elsewhere is not);
    //   3. roll commit forward on file rows;
    //   4. roll commit forward on the worktree row;
    //   5. roll commit forward on outstanding `txn` audit rows — the
    //      writes they describe are exactly the ones just committed.
    //
    // What a commit stamps is what it carries, which is why both record
    // statements consult `HAS_CONFLICT_SIBLING`:
    //   - a conflict row *is* the file's value, so the commit carries it
    //     whether it is live or the tombstone-shaped kind, and it is
    //     stamped unconditionally. Conflict rows are never purged here —
    //     unlike a tombstone, a divergence outlives the commit that
    //     wrote the file, and only a resolution ends it;
    //   - a row shadowed by one is not in the commit at all: the write
    //     skipped it and left the file's value alone. Stamping it would
    //     claim this commit carries json it does not, and purging its
    //     tombstone would drop a pending delete nobody applied.
    match db {
        Db::Sqlite(pool) => {
            let (live_rows, purgeable) = record_predicates("0", "1");
            let mut tx = pool.begin().await?;
            let placeholders: Vec<String> =
                (0..files.len()).map(|i| format!("?{}", i + 3)).collect();
            let sql = format!(
                "UPDATE record SET commit_id = ?1, base_commit_id = NULL \
                 WHERE worktree_id = ?2 AND ({live_rows}) AND file_path IN ({})",
                placeholders.join(",")
            );
            let mut q = sqlx::query(&sql).bind(new_commit).bind(worktree_id);
            for f in files {
                q = q.bind(f);
            }
            q.execute(&mut *tx).await?;
            let placeholders: Vec<String> =
                (0..files.len()).map(|i| format!("?{}", i + 2)).collect();
            let sql = format!(
                "DELETE FROM record WHERE worktree_id = ?1 AND ({purgeable}) \
                 AND file_path IN ({})",
                placeholders.join(",")
            );
            let mut q = sqlx::query(&sql).bind(worktree_id);
            for f in files {
                q = q.bind(f);
            }
            q.execute(&mut *tx).await?;
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
            sqlx::query(
                "UPDATE txn SET commit_id = ?1 WHERE worktree_id = ?2 AND commit_id IS NULL",
            )
            .bind(new_commit)
            .bind(worktree_id)
            .execute(&mut *tx)
            .await?;
            tx.commit().await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let (live_rows, purgeable) = record_predicates("FALSE", "TRUE");
            let mut tx = pool.begin().await?;
            sqlx::query(&format!(
                "UPDATE record SET commit_id = $1, base_commit_id = NULL \
                 WHERE worktree_id = $2 AND ({live_rows}) AND file_path = ANY($3)"
            ))
            .bind(new_commit)
            .bind(worktree_id)
            .bind(files)
            .execute(&mut *tx)
            .await?;
            sqlx::query(&format!(
                "DELETE FROM record WHERE worktree_id = $1 AND ({purgeable}) \
                 AND file_path = ANY($2)"
            ))
            .bind(worktree_id)
            .bind(files)
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
            sqlx::query(
                "UPDATE txn SET commit_id = $1 WHERE worktree_id = $2 AND commit_id IS NULL",
            )
            .bind(new_commit)
            .bind(worktree_id)
            .execute(&mut *tx)
            .await?;
            tx.commit().await?;
        }
    }
    Ok(())
}

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
/// of the commit message, just before [`roll_forward`] stamps them.
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
