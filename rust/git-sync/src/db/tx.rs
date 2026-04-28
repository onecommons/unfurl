// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Transaction-scoped helpers used by the CRUD primitives in `sync`.
//!
//! Each helper is generic over a [`Dialect`] which supplies the
//! per-dialect SQL string. Two impls — `sqlx::Sqlite` (always
//! compiled) and `sqlx::Postgres` (gated on the `postgres` feature) —
//! hold the dialect-specific text. The function bodies (bind, execute,
//! row-decode) are shared.
//!
//! `lookup_commits` casts the `deleted` column to `INTEGER` in SQL so
//! both dialects return the same row tuple shape; that's the only
//! reason the row decoding can be shared (Postgres stores `BOOLEAN`,
//! SQLite stores `INTEGER`).
//!
//! `sqlx::Any` is intentionally avoided (see `db::Db` for context).

use sqlx::{Database, Encode, Executor, IntoArguments, Type};

use crate::error::Result;

/// Lookup result from [`lookup_commits`]. Tombstones surface as if
/// absent: the `record_id` and `record_commit` fields are `None`, and
/// the conflict checker falls back to `file_commit`.
pub(crate) struct RecordLookup {
    /// Live record id; `None` when the row is absent or a tombstone.
    pub(crate) record_id: Option<i64>,
    /// Live row's `commit_id`; `None` when absent or a tombstone.
    pub(crate) record_commit: Option<String>,
    /// File row's `commit_id` (used as a fallback in the conflict
    /// check when `record_id.is_none()`).
    pub(crate) file_commit: Option<String>,
}

/// `(record.id, record.commit_id, CAST(record.deleted AS INTEGER),
/// file.commit_id)` — the row shape of [`lookup_commits`]. The
/// integer cast normalizes the `deleted` column across dialects.
type LookupRow = (Option<i64>, Option<String>, Option<i64>, Option<String>);

// ---------------------------------------------------------------------------
// Dialect: per-database SQL strings.
// ---------------------------------------------------------------------------

pub(crate) trait Dialect: Database {
    /// `INSERT … ON CONFLICT(worktree_id, path) DO NOTHING` for the file row.
    const ENSURE_FILE_ROW: &'static str;
    /// Joined SELECT for the conflict-check, with `deleted` cast to INTEGER.
    const LOOKUP_COMMITS: &'static str;
    /// `SELECT format FROM file WHERE worktree_id = ? AND path = ?`.
    const FILE_FORMAT: &'static str;
    /// `DELETE FROM alias WHERE record_id = ?`.
    const DELETE_ALIASES: &'static str;
    /// `INSERT INTO alias (...) … <conflict-skipping clause>`.
    const INSERT_ALIAS: &'static str;
    /// `INSERT … ON CONFLICT DO UPDATE … RETURNING id` — used by both
    /// `create_record` (with the resurrect-tombstone semantics) and
    /// `upsert_record` (where overwrite-live is the documented behaviour).
    /// The two SQL bodies are identical so we share one constant.
    const UPSERT_RECORD: &'static str;
    /// `UPDATE record SET json = …, commit_id = NULL, deleted = 0/FALSE
    /// WHERE id = ?`.
    const UPDATE_RECORD: &'static str;
    /// `UPDATE record SET deleted = 1/TRUE, commit_id = NULL WHERE id = ?`.
    const TOMBSTONE_RECORD: &'static str;
}

impl Dialect for sqlx::Sqlite {
    const ENSURE_FILE_ROW: &'static str =
        "INSERT INTO file (worktree_id, path, format, commit_id) \
         VALUES (?1, ?2, 'unknown', NULL) \
         ON CONFLICT(worktree_id, path) DO NOTHING";
    const LOOKUP_COMMITS: &'static str =
        "SELECT r.id, r.commit_id, CASE WHEN r.deleted THEN 1 ELSE 0 END, f.commit_id \
         FROM file f \
         LEFT JOIN record r ON r.worktree_id = f.worktree_id \
                           AND r.file_path = f.path \
                           AND r.path = ?3 \
                           AND r.key = ?4 \
         WHERE f.worktree_id = ?1 AND f.path = ?2";
    const FILE_FORMAT: &'static str =
        "SELECT format FROM file WHERE worktree_id = ?1 AND path = ?2";
    const DELETE_ALIASES: &'static str = "DELETE FROM alias WHERE record_id = ?1";
    const INSERT_ALIAS: &'static str =
        "INSERT OR IGNORE INTO alias (record_id, path, key) VALUES (?1, ?2, ?3)";
    const UPSERT_RECORD: &'static str =
        "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, deleted) \
         VALUES (?1, ?2, ?3, ?4, jsonb(?5), NULL, 0) \
         ON CONFLICT(worktree_id, file_path, path, key) DO UPDATE SET \
           json = excluded.json, commit_id = NULL, deleted = 0 \
         RETURNING id";
    const UPDATE_RECORD: &'static str =
        "UPDATE record SET json = jsonb(?1), commit_id = NULL, deleted = 0 \
         WHERE id = ?2";
    const TOMBSTONE_RECORD: &'static str =
        "UPDATE record SET deleted = 1, commit_id = NULL WHERE id = ?1";
}

#[cfg(feature = "postgres")]
impl Dialect for sqlx::Postgres {
    const ENSURE_FILE_ROW: &'static str =
        "INSERT INTO file (worktree_id, path, format, commit_id) \
         VALUES ($1, $2, 'unknown', NULL) \
         ON CONFLICT(worktree_id, path) DO NOTHING";
    const LOOKUP_COMMITS: &'static str =
        "SELECT r.id, r.commit_id, CASE WHEN r.deleted THEN 1::BIGINT ELSE 0::BIGINT END, f.commit_id \
         FROM file f \
         LEFT JOIN record r ON r.worktree_id = f.worktree_id \
                           AND r.file_path = f.path \
                           AND r.path = $3 \
                           AND r.key = $4 \
         WHERE f.worktree_id = $1 AND f.path = $2";
    const FILE_FORMAT: &'static str =
        "SELECT format FROM file WHERE worktree_id = $1 AND path = $2";
    const DELETE_ALIASES: &'static str = "DELETE FROM alias WHERE record_id = $1";
    const INSERT_ALIAS: &'static str =
        "INSERT INTO alias (record_id, path, key) VALUES ($1, $2, $3) \
         ON CONFLICT DO NOTHING";
    const UPSERT_RECORD: &'static str =
        "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, deleted) \
         VALUES ($1, $2, $3, $4, $5::jsonb, NULL, FALSE) \
         ON CONFLICT(worktree_id, file_path, path, key) DO UPDATE SET \
           json = EXCLUDED.json, commit_id = NULL, deleted = FALSE \
         RETURNING id";
    const UPDATE_RECORD: &'static str =
        "UPDATE record SET json = $1::jsonb, commit_id = NULL, deleted = FALSE \
         WHERE id = $2";
    const TOMBSTONE_RECORD: &'static str =
        "UPDATE record SET deleted = TRUE, commit_id = NULL WHERE id = $1";
}

// ---------------------------------------------------------------------------
// Generic transaction helpers.
// ---------------------------------------------------------------------------
//
// Each helper repeats the same four bind/exec bounds. Rust's
// implied-bounds machinery doesn't propagate supertrait `where`
// clauses into function bodies, and `where` clauses don't accept
// macro expansion, so the bounds stay inline.

pub(crate) async fn ensure_file_row<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    worktree_id: i64,
    file_path: &str,
) -> Result<()>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
{
    sqlx::query(DB::ENSURE_FILE_ROW)
        .bind(worktree_id)
        .bind(file_path)
        .execute(&mut **tx)
        .await?;
    Ok(())
}

pub(crate) async fn lookup_commits<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
) -> Result<RecordLookup>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    LookupRow: for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    let row: Option<LookupRow> = sqlx::query_as(DB::LOOKUP_COMMITS)
        .bind(worktree_id)
        .bind(file_path)
        .bind(path)
        .bind(key)
        .fetch_optional(&mut **tx)
        .await?;
    let (raw_id, rec_commit, deleted, file_commit) = row.unwrap_or((None, None, None, None));
    let is_tombstone = matches!(deleted, Some(d) if d != 0);
    let record_id = match (raw_id, is_tombstone) {
        (Some(id), false) => Some(id),
        _ => None,
    };
    let record_commit = if record_id.is_some() {
        rec_commit
    } else {
        None
    };
    Ok(RecordLookup {
        record_id,
        record_commit,
        file_commit,
    })
}

pub(crate) async fn file_format<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    worktree_id: i64,
    file_path: &str,
) -> Result<Option<String>>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (String,): for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    let row: Option<(String,)> = sqlx::query_as(DB::FILE_FORMAT)
        .bind(worktree_id)
        .bind(file_path)
        .fetch_optional(&mut **tx)
        .await?;
    Ok(row.map(|(f,)| f))
}

pub(crate) async fn replace_aliases<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    record_id: i64,
    aliases: &[(String, String)],
) -> Result<()>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
{
    sqlx::query(DB::DELETE_ALIASES)
        .bind(record_id)
        .execute(&mut **tx)
        .await?;
    for (p, k) in aliases {
        sqlx::query(DB::INSERT_ALIAS)
            .bind(record_id)
            .bind(p.as_str())
            .bind(k.as_str())
            .execute(&mut **tx)
            .await?;
    }
    Ok(())
}

/// INSERT-or-resurrect: if no live row exists, insert a fresh record;
/// if a tombstone exists for this `(worktree_id, file_path, path, key)`,
/// resurrect it by clearing the tombstone bit. The unique index makes
/// both branches converge on the same row id.
pub(crate) async fn create_record<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
    json_text: &str,
) -> Result<i64>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    let row: (i64,) = sqlx::query_as(DB::UPSERT_RECORD)
        .bind(worktree_id)
        .bind(file_path)
        .bind(path)
        .bind(key)
        .bind(json_text)
        .fetch_one(&mut **tx)
        .await?;
    Ok(row.0)
}

pub(crate) async fn update_record<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    id: i64,
    json_text: &str,
) -> Result<()>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
{
    sqlx::query(DB::UPDATE_RECORD)
        .bind(json_text)
        .bind(id)
        .execute(&mut **tx)
        .await?;
    Ok(())
}

/// Same SQL shape as [`create_record`] but called from `crud_upsert`,
/// where overwriting an existing live row is the documented behaviour.
pub(crate) async fn upsert_record<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
    json_text: &str,
) -> Result<i64>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    create_record(tx, worktree_id, file_path, path, key, json_text).await
}

/// Tombstone the record and drop its aliases. We never hard-delete here;
/// `commit_repository` is the only path that purges tombstones once
/// they've been written to disk.
pub(crate) async fn delete_record<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    id: i64,
) -> Result<()>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
{
    sqlx::query(DB::TOMBSTONE_RECORD)
        .bind(id)
        .execute(&mut **tx)
        .await?;
    sqlx::query(DB::DELETE_ALIASES)
        .bind(id)
        .execute(&mut **tx)
        .await?;
    Ok(())
}
