// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Transaction-scoped helpers used by the CRUD primitives in `sync`.
//!
//! Each helper is generic over a `Dialect` which supplies the
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
/// absent: the `record_*` fields are `None`. The `record_file_path`
/// and `default_file_path` fields let the caller resolve a missing
/// `file_path` argument (existing record's file, else worktree
/// default) without a second query.
pub(crate) struct RecordLookup {
    /// Live record id; `None` when the row is absent or a tombstone.
    pub(crate) record_id: Option<i64>,
    /// Live row's `commit_id`; `None` when absent or a tombstone.
    pub(crate) record_commit: Option<String>,
    /// Live row's `version` stamp; `None` when absent or a tombstone.
    pub(crate) record_version: Option<i64>,
    /// Live row's `file_path`; `None` when absent or a tombstone.
    pub(crate) record_file_path: Option<String>,
    /// Worktree's `default_file_path`. `None` when the operator
    /// hasn't pinned one and `update_from_working_dir` hasn't run
    /// yet.
    pub(crate) default_file_path: Option<String>,
}

/// `(record.id, record.commit_id, CAST(record.deleted AS INTEGER),
/// record.version, record.file_path, worktree.default_file_path)` —
/// the row shape of [`lookup_commits`]. Always returns one row
/// (worktree-driven LEFT JOIN onto record). The integer cast
/// normalizes the `deleted` column across dialects.
pub(crate) type LookupRow = (
    Option<i64>,
    Option<String>,
    Option<i64>,
    Option<i64>,
    Option<String>,
    Option<String>,
);

// ---------------------------------------------------------------------------
// Dialect: per-database SQL strings.
// ---------------------------------------------------------------------------

pub(crate) trait Dialect: Database {
    /// `UPDATE worktree SET next_version = next_version + 1
    /// WHERE id = ? RETURNING next_version - 1`. Used to pull a fresh,
    /// monotonic version stamp inside the same transaction as the
    /// record write.
    const NEXT_VERSION: &'static str;
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
    /// Sets `version` to the supplied bind value.
    const UPSERT_RECORD: &'static str;
    /// `UPDATE record SET json = …, commit_id = NULL, deleted = 0/FALSE,
    /// version = ? WHERE id = ?`.
    const UPDATE_RECORD: &'static str;
    /// `UPDATE record SET deleted = 1/TRUE, commit_id = NULL,
    /// version = ? WHERE id = ?`.
    const TOMBSTONE_RECORD: &'static str;
}

impl Dialect for sqlx::Sqlite {
    const NEXT_VERSION: &'static str = "UPDATE worktree SET next_version = next_version + 1 \
         WHERE id = ?1 RETURNING next_version - 1";
    const LOOKUP_COMMITS: &'static str =
        "SELECT r.id, r.commit_id, CASE WHEN r.deleted THEN 1 ELSE 0 END, r.version, \
                r.file_path, w.default_file_path \
         FROM worktree w \
         LEFT JOIN record r ON r.worktree_id = w.id \
                           AND r.path = ?3 \
                           AND r.key = ?4 \
                           AND (?2 IS NULL OR r.file_path = ?2) \
         WHERE w.id = ?1 \
         LIMIT 1";
    const FILE_FORMAT: &'static str =
        "SELECT format FROM file WHERE worktree_id = ?1 AND path = ?2";
    const DELETE_ALIASES: &'static str = "DELETE FROM alias WHERE record_id = ?1";
    const INSERT_ALIAS: &'static str =
        "INSERT OR IGNORE INTO alias (record_id, path, key) VALUES (?1, ?2, ?3)";
    // Embeds the OCC check in the ON CONFLICT DO UPDATE WHERE: the
    // existing row's version must be <= expected_version (when set)
    // and its commit_id must equal expected_commit (when set). If
    // either bind is NULL (caller skipped OCC), the corresponding
    // arm short-circuits true. RETURNING returns 0 rows when the
    // WHERE filtered out, which the caller maps to Error::Conflict.
    const UPSERT_RECORD: &'static str =
        "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, deleted, version) \
         VALUES (?1, ?2, ?3, ?4, jsonb(?5), NULL, 0, ?6) \
         ON CONFLICT(worktree_id, file_path, path, key) DO UPDATE SET \
           json = excluded.json, commit_id = NULL, deleted = 0, version = excluded.version \
         WHERE (?7 IS NULL OR record.version <= ?7) \
           AND (?8 IS NULL OR record.commit_id = ?8) \
         RETURNING id";
    // Same OCC predicate appended to the UPDATE WHERE clause; the
    // ``RETURNING id`` lets the caller distinguish "no rows matched"
    // (race detected → Error::Conflict) from a genuine update via
    // fetch_optional, without needing a portable rows_affected() bound.
    const UPDATE_RECORD: &'static str =
        "UPDATE record SET json = jsonb(?1), commit_id = NULL, deleted = 0, version = ?3 \
         WHERE id = ?2 \
           AND (?4 IS NULL OR version <= ?4) \
           AND (?5 IS NULL OR commit_id = ?5) \
         RETURNING id";
    const TOMBSTONE_RECORD: &'static str =
        "UPDATE record SET deleted = 1, commit_id = NULL, version = ?2 \
         WHERE id = ?1 \
           AND (?3 IS NULL OR version <= ?3) \
           AND (?4 IS NULL OR commit_id = ?4) \
         RETURNING id";
}

#[cfg(feature = "postgres")]
impl Dialect for sqlx::Postgres {
    const NEXT_VERSION: &'static str = "UPDATE worktree SET next_version = next_version + 1 \
         WHERE id = $1 RETURNING next_version - 1";
    const LOOKUP_COMMITS: &'static str =
        "SELECT r.id, r.commit_id, CASE WHEN r.deleted THEN 1::BIGINT ELSE 0::BIGINT END, r.version, \
                r.file_path, w.default_file_path \
         FROM worktree w \
         LEFT JOIN record r ON r.worktree_id = w.id \
                           AND r.path = $3 \
                           AND r.key = $4 \
                           AND ($2::TEXT IS NULL OR r.file_path = $2) \
         WHERE w.id = $1 \
         LIMIT 1";
    const FILE_FORMAT: &'static str =
        "SELECT format FROM file WHERE worktree_id = $1 AND path = $2";
    const DELETE_ALIASES: &'static str = "DELETE FROM alias WHERE record_id = $1";
    const INSERT_ALIAS: &'static str =
        "INSERT INTO alias (record_id, path, key) VALUES ($1, $2, $3) \
         ON CONFLICT DO NOTHING";
    // Embeds the OCC check in the ON CONFLICT DO UPDATE WHERE
    // (mirrors the SQLite impl; ``::BIGINT`` / ``::TEXT`` casts on
    // the bind sites are required because postgres can't infer the
    // type of a NULL parameter).
    const UPSERT_RECORD: &'static str =
        "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, deleted, version) \
         VALUES ($1, $2, $3, $4, $5::jsonb, NULL, FALSE, $6) \
         ON CONFLICT(worktree_id, file_path, path, key) DO UPDATE SET \
           json = EXCLUDED.json, commit_id = NULL, deleted = FALSE, version = EXCLUDED.version \
         WHERE ($7::BIGINT IS NULL OR record.version <= $7) \
           AND ($8::TEXT IS NULL OR record.commit_id = $8) \
         RETURNING id";
    const UPDATE_RECORD: &'static str =
        "UPDATE record SET json = $1::jsonb, commit_id = NULL, deleted = FALSE, version = $3 \
         WHERE id = $2 \
           AND ($4::BIGINT IS NULL OR version <= $4) \
           AND ($5::TEXT IS NULL OR commit_id = $5) \
         RETURNING id";
    const TOMBSTONE_RECORD: &'static str =
        "UPDATE record SET deleted = TRUE, commit_id = NULL, version = $2 \
         WHERE id = $1 \
           AND ($3::BIGINT IS NULL OR version <= $3) \
           AND ($4::TEXT IS NULL OR commit_id = $4) \
         RETURNING id";
}

// ---------------------------------------------------------------------------
// Generic transaction helpers.
// ---------------------------------------------------------------------------
//
// Why do we need all these `where` clauses on all these generic functions??
//
// The sqlx Database supertrait of the Dialect trait includes GATs that are referenced
// via impls for primitives like i64, &str, String, Vec<u8>, e.g. `Encode<'q, DB>`
// and `Decode<'r,DB>` etc. So each concrete type
// passed to a generic helper function via one of those traits needs a where bound.
// And we can't hoist these where bounds onto our Dialect trait because
// GATs with lifetimes require us to use HRTB where bounds like for<'q>
// and Rust does not yet support HRTB as implied bounds.

/// One-query precondition fetch for the CRUD helpers.
///
/// Runs the worktree-driven [`Dialect::LOOKUP_COMMITS`] join: looks
/// for a live record at `(worktree_id, path, key)` (optionally
/// further constrained to `file_path`) and returns the worktree's
/// `default_file_path` alongside, so the caller can resolve a
/// missing `file_path` argument as
/// `caller.or(record_file_path).or(default_file_path)` without a
/// second query.
///
/// Always returns one row — the join is anchored on the `worktree`
/// row, so an absent record surfaces as `record_id == None` rather
/// than a missing row. Tombstones (`deleted == TRUE`) are treated as
/// absent: their `record_id`, `record_commit`, `record_version`, and
/// `record_file_path` come back as `None`. The `default_file_path`
/// column is populated regardless.
///
/// `file_path == None` drops the `r.file_path = ?2` filter from the
/// join, so the returned record (if any) is whichever live record
/// matches `(path, key)` in any file. The cloudmap format treats
/// `(path, key)` as globally unique within a worktree, so this is
/// well-defined; if multiple records ever match, `LIMIT 1` picks
/// one arbitrarily.
pub(crate) async fn lookup_commits<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    worktree_id: i64,
    file_path: Option<&str>,
    path: &str,
    key: &str,
) -> Result<RecordLookup>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> Option<&'q str>: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    LookupRow: for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    // Always returns one row (worktree-driven), with optional record
    // fields and `default_file_path`. Caller resolves the effective
    // `file_path` as `caller.or(record_file_path).or(default_file_path)`.
    let row: LookupRow = sqlx::query_as(DB::LOOKUP_COMMITS)
        .bind(worktree_id)
        .bind(file_path)
        .bind(path)
        .bind(key)
        .fetch_one(&mut **tx)
        .await?;
    let (raw_id, rec_commit, deleted, rec_version, rec_file_path, default_file_path) = row;
    let is_tombstone = matches!(deleted, Some(d) if d != 0);
    let record_id = match (raw_id, is_tombstone) {
        (Some(id), false) => Some(id),
        _ => None,
    };
    let (record_commit, record_version, record_file_path) = if record_id.is_some() {
        (rec_commit, rec_version, rec_file_path)
    } else {
        (None, None, None)
    };
    Ok(RecordLookup {
        record_id,
        record_commit,
        record_version,
        record_file_path,
        default_file_path,
    })
}

/// Atomically increment `worktree.next_version` and return the value
/// that should be stamped on the record being written. Call this once
/// per CRUD write inside the same transaction as the record mutation.
pub(crate) async fn next_version<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    worktree_id: i64,
) -> Result<i64>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    let row: (i64,) = sqlx::query_as(DB::NEXT_VERSION)
        .bind(worktree_id)
        .fetch_one(&mut **tx)
        .await?;
    Ok(row.0)
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
///
/// On the `ON CONFLICT DO UPDATE` branch the SQL also enforces an
/// optional OCC predicate against the existing row's
/// `(version, commit_id)`. Pass `None`/`None` to skip the check;
/// otherwise pass the expected upper-bound `version` (Pending token)
/// or expected `commit_id` (Commit token) and the SQL will only
/// rewrite the row if it still matches. A WHERE-mismatch returns 0
/// rows; this function maps that to [`crate::Error::Conflict`].
///
/// `version` is the new value stamped onto the row.
pub(crate) async fn create_record<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
    json_text: &str,
    version: i64,
    expected_version: Option<i64>,
    expected_commit: Option<&str>,
) -> Result<i64>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> Option<i64>: Encode<'q, DB> + Type<DB>,
    for<'q> Option<&'q str>: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    let row: Option<(i64,)> = sqlx::query_as(DB::UPSERT_RECORD)
        .bind(worktree_id)
        .bind(file_path)
        .bind(path)
        .bind(key)
        .bind(json_text)
        .bind(version)
        .bind(expected_version)
        .bind(expected_commit)
        .fetch_optional(&mut **tx)
        .await?;
    match row {
        Some((id,)) => Ok(id),
        None => {
            // The DO UPDATE WHERE filtered out — the row exists but
            // its (version, commit_id) doesn't match what the caller
            // expected. (If the row didn't exist at all, the INSERT
            // arm would have returned the new id.) The caller's
            // earlier `enforce_conflict` snapshot was racy: another
            // tx wrote between the lookup and this statement.
            Err(crate::error::Error::Conflict {
                file_path: file_path.to_string(),
                path: path.to_string(),
                expected: race_expected(expected_version, expected_commit),
                actual: None,
            })
        }
    }
}

pub(crate) async fn update_record<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    id: i64,
    json_text: &str,
    version: i64,
    expected_version: Option<i64>,
    expected_commit: Option<&str>,
) -> Result<()>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> Option<i64>: Encode<'q, DB> + Type<DB>,
    for<'q> Option<&'q str>: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    let row: Option<(i64,)> = sqlx::query_as(DB::UPDATE_RECORD)
        .bind(json_text)
        .bind(id)
        .bind(version)
        .bind(expected_version)
        .bind(expected_commit)
        .fetch_optional(&mut **tx)
        .await?;
    if row.is_none() {
        // Either the row vanished or its (version, commit_id) drifted
        // from what the caller's snapshot saw. Map both to Conflict
        // since the caller's intent (write into a known state) didn't
        // hold.
        return Err(crate::error::Error::Conflict {
            file_path: String::new(),
            path: String::new(),
            expected: race_expected(expected_version, expected_commit),
            actual: None,
        });
    }
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
    version: i64,
    expected_version: Option<i64>,
    expected_commit: Option<&str>,
) -> Result<i64>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> Option<i64>: Encode<'q, DB> + Type<DB>,
    for<'q> Option<&'q str>: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    create_record(
        tx,
        worktree_id,
        file_path,
        path,
        key,
        json_text,
        version,
        expected_version,
        expected_commit,
    )
    .await
}

/// Tombstone the record and drop its aliases. We never hard-delete here;
/// `commit_repository` is the only path that purges tombstones once
/// they've been written to disk. `version` is stamped onto the row so
/// the tombstone shows up in `list_changes(Some(prev_version))`.
///
/// The OCC predicate behaves the same as in [`update_record`] — pass
/// `None`/`None` to skip, otherwise the SQL will only mutate the row
/// if its `(version, commit_id)` still matches the bound expectation.
pub(crate) async fn delete_record<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    id: i64,
    version: i64,
    expected_version: Option<i64>,
    expected_commit: Option<&str>,
) -> Result<()>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> Option<i64>: Encode<'q, DB> + Type<DB>,
    for<'q> Option<&'q str>: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    let row: Option<(i64,)> = sqlx::query_as(DB::TOMBSTONE_RECORD)
        .bind(id)
        .bind(version)
        .bind(expected_version)
        .bind(expected_commit)
        .fetch_optional(&mut **tx)
        .await?;
    if row.is_none() {
        return Err(crate::error::Error::Conflict {
            file_path: String::new(),
            path: String::new(),
            expected: race_expected(expected_version, expected_commit),
            actual: None,
        });
    }
    sqlx::query(DB::DELETE_ALIASES)
        .bind(id)
        .execute(&mut **tx)
        .await?;
    Ok(())
}

/// Reconstruct a [`crate::CommitRef`] from the (expected_version,
/// expected_commit) bind pair so the SQL-level conflict carries the
/// same shape as the early-bailout one from `enforce_conflict`.
fn race_expected(expected_version: Option<i64>, expected_commit: Option<&str>) -> crate::CommitRef {
    match (expected_commit, expected_version) {
        (Some(c), _) => crate::CommitRef::Commit(c.to_string()),
        (None, Some(v)) => crate::CommitRef::Pending(v),
        // Both None — caller didn't pass an OCC token. We still
        // synthesize a Pending(0) for the error so the variant
        // construction is total. In practice this branch is hit
        // only when the row was hard-deleted between lookup and
        // write, which is a real concurrency anomaly worth
        // surfacing.
        (None, None) => crate::CommitRef::Pending(0),
    }
}
