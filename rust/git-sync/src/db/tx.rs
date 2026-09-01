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

use std::collections::BTreeSet;

use sqlx::{Database, Encode, Executor, IntoArguments, Type};

use crate::db::RecordId;
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
    /// `UPDATE version_seq SET next_version = next_version + ?2
    /// WHERE worktree_id = ?1 RETURNING next_version - ?2`. Draws `?2`
    /// consecutive version stamps inside the same transaction as the
    /// record write, returning the first.
    ///
    /// Bound to the *family* root, not the worktree, so an upstream and
    /// its forks and drafts share one sequence — a version has to mean
    /// the same thing everywhere it can turn up in one response. Drawing
    /// a whole batch in a single statement rather than one at a time
    /// keeps the row lock held for a statement instead of the length of
    /// the batch, which is what stops a large import blocking every
    /// other writer in the family.
    const NEXT_VERSION: &'static str;
    /// Joined SELECT for the conflict-check, with `deleted` cast to INTEGER.
    const LOOKUP_COMMITS: &'static str;
    /// `SELECT format FROM file WHERE worktree_id = ? AND path = ?`.
    const FILE_FORMAT: &'static str;
    /// `INSERT INTO file (...) … <conflict-skipping clause>`. Registers a
    /// file the worktree hasn't scanned yet so record rows can satisfy the
    /// `record -> file` foreign key; an existing row is left untouched.
    const INSERT_FILE: &'static str;
    /// `DELETE FROM alias WHERE record_id = ?`.
    const DELETE_ALIASES: &'static str;
    /// `INSERT INTO alias (...) … <conflict-skipping clause>`.
    const INSERT_ALIAS: &'static str;
    /// `INSERT … ON CONFLICT (…) WHERE conflict IS NULL DO UPDATE …
    /// RETURNING id` — used by both `create_record` (with the
    /// resurrect-tombstone semantics) and `upsert_record` (where
    /// overwrite-live is the documented behaviour). The two SQL bodies
    /// are identical so we share one constant. Sets `version` to the
    /// supplied bind value. The `WHERE` on the conflict target names the
    /// partial unique index over the database's own rows, so an upsert
    /// can never collide with a conflict row.
    const UPSERT_RECORD: &'static str;
    /// `UPDATE record SET json = …, commit_id = NULL, deleted = 0/FALSE,
    /// version = ? WHERE id = ?`.
    const UPDATE_RECORD: &'static str;
    /// `UPDATE record SET deleted = 1/TRUE, commit_id = NULL,
    /// version = ? WHERE id = ?`.
    const TOMBSTONE_RECORD: &'static str;
    /// `SELECT path, key, json, deleted, base_commit_id, version FROM
    /// record … WHERE commit_id IS NULL AND conflict IS NULL` — the
    /// file's in-flight rows, as the re-sync path needs them to preserve
    /// client edits over a scan.
    const LIST_PENDING_RECORDS: &'static str;
    /// `SELECT path, key, json, commit_id FROM record … WHERE
    /// commit_id IS NOT NULL AND conflict IS NULL` — the file's synced
    /// rows, as the re-sync path needs them to leave untouched records'
    /// versions alone. Never a tombstone: tombstoning nulls `commit_id`.
    const LIST_COMMITTED_RECORDS: &'static str;
    /// `INSERT INTO file (...) … ON CONFLICT DO UPDATE` — full upsert
    /// that overwrites `format` and `commit_id`. Used by the from-disk
    /// re-sync path, where the scanned file is the source of truth
    /// (unlike [`Dialect::INSERT_FILE`], which leaves an existing row
    /// untouched).
    const UPSERT_FILE: &'static str;
    /// Re-sync record upsert: like [`Dialect::UPSERT_RECORD`] but
    /// `commit_id` comes from a bind (the commit that last touched the
    /// path, possibly NULL for a never-committed one) instead of being
    /// forced to NULL, and there is no OCC predicate. The working tree
    /// is the source of truth for the rows this reaches — the caller
    /// keeps in-flight client edits out of its way (see
    /// `upsert_file_and_records_inner`).
    const SYNC_UPSERT_RECORD: &'static str;
    /// `SELECT path, key FROM record WHERE worktree_id = ? AND
    /// file_path = ? AND conflict IS NULL`.
    const LIST_FILE_RECORD_KEYS: &'static str;
    /// `DELETE FROM record … RETURNING id` — single-row hard delete
    /// keyed by `(worktree_id, file_path, path, key)`. Conflict rows are
    /// excluded: the key names two rows and this one means the
    /// database's own.
    const DELETE_RECORD_BY_KEY: &'static str;
    /// `INSERT INTO txn (...)` — audit row for one batch write.
    /// `commit_id` is left NULL (outstanding) for
    /// `db::commit::roll_forward` to stamp.
    const INSERT_TXN: &'static str;
    /// `SELECT id, path, key, json, deleted, conflict FROM record …
    /// WHERE conflict IS NOT NULL` — the file's side of every record the
    /// two sides disagree about.
    const LIST_CONFLICT_RECORDS: &'static str;
    /// `INSERT … ON CONFLICT (…) WHERE conflict IS NOT NULL DO UPDATE …
    /// RETURNING id` — create or refresh a conflict row. Targets the
    /// second partial unique index, so it can never collide with the
    /// database's own row for the same key.
    const UPSERT_CONFLICT_RECORD: &'static str;
    /// `UPDATE record SET conflict = ?, version = ? … WHERE conflict IS
    /// NOT NULL RETURNING id` — flip a conflict row's state.
    const SET_CONFLICT_STATE: &'static str;
    /// `DELETE FROM record … WHERE conflict IS NOT NULL` — drop a
    /// conflict row once the divergence is settled or gone.
    const DELETE_CONFLICT_RECORD: &'static str;
    /// `SELECT id, commit_id, version, CAST(deleted AS INTEGER) FROM
    /// record … WHERE conflict IS NULL` — the database's own row at a
    /// key, **tombstones included**. Unlike [`Dialect::LOOKUP_COMMITS`],
    /// which reports a tombstone as absent because a CRUD write treats
    /// it that way; resolving a conflict has to see an in-flight delete,
    /// that being one of the two sides.
    const LOOKUP_OWN_RECORD: &'static str;
}

impl Dialect for sqlx::Sqlite {
    const NEXT_VERSION: &'static str = "UPDATE version_seq SET next_version = next_version + ?2 \
         WHERE worktree_id = ?1 RETURNING next_version - ?2";
    const LOOKUP_COMMITS: &'static str =
        "SELECT r.id, r.commit_id, CASE WHEN r.deleted THEN 1 ELSE 0 END, r.version, \
                r.file_path, w.default_file_path \
         FROM worktree w \
         LEFT JOIN record r ON r.worktree_id = w.id \
                           AND r.path = ?3 \
                           AND r.key = ?4 \
                           AND r.conflict IS NULL \
                           AND (?2 IS NULL OR r.file_path = ?2) \
         WHERE w.id = ?1 \
         LIMIT 1";
    const FILE_FORMAT: &'static str =
        "SELECT format FROM file WHERE worktree_id = ?1 AND path = ?2";
    const INSERT_FILE: &'static str =
        "INSERT OR IGNORE INTO file (worktree_id, path, format, commit_id) \
         VALUES (?1, ?2, ?3, NULL)";
    const DELETE_ALIASES: &'static str = "DELETE FROM alias WHERE record_id = ?1";
    const INSERT_ALIAS: &'static str =
        "INSERT OR IGNORE INTO alias (record_id, path, key) VALUES (?1, ?2, ?3)";
    // Embeds the OCC check in the ON CONFLICT DO UPDATE WHERE: the
    // existing row's version must be <= expected_version (when set)
    // and its commit_id must equal expected_commit (when set). If
    // either bind is NULL (caller skipped OCC), the corresponding
    // arm short-circuits true. RETURNING returns 0 rows when the
    // WHERE filtered out, which the caller maps to Error::Conflict.
    // Every client write snapshots the commit its edit is based on:
    // `base_commit_id = COALESCE(commit_id, base_commit_id)` next to
    // `commit_id = NULL`. The COALESCE keeps the base across
    // consecutive edits (the second one sees commit_id already NULL),
    // and ordering against `commit_id = NULL` is safe because SQL
    // evaluates SET right-hand sides against the pre-update row.
    const UPSERT_RECORD: &'static str =
        "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, base_commit_id, deleted, version) \
         VALUES (?1, ?2, ?3, ?4, jsonb(?5), NULL, NULL, 0, ?6) \
         ON CONFLICT(worktree_id, file_path, path, key) WHERE conflict IS NULL DO UPDATE SET \
           json = excluded.json, \
           base_commit_id = COALESCE(record.commit_id, record.base_commit_id), \
           commit_id = NULL, deleted = 0, version = excluded.version \
         WHERE (?7 IS NULL OR record.version <= ?7) \
           AND (?8 IS NULL OR record.commit_id = ?8) \
         RETURNING id";
    // Same OCC predicate appended to the UPDATE WHERE clause; the
    // ``RETURNING id`` lets the caller distinguish "no rows matched"
    // (race detected → Error::Conflict) from a genuine update via
    // fetch_optional, without needing a portable rows_affected() bound.
    const UPDATE_RECORD: &'static str = "UPDATE record SET json = jsonb(?1), \
           base_commit_id = COALESCE(commit_id, base_commit_id), \
           commit_id = NULL, deleted = 0, version = ?3 \
         WHERE id = ?2 \
           AND (?4 IS NULL OR version <= ?4) \
           AND (?5 IS NULL OR commit_id = ?5) \
         RETURNING id";
    const TOMBSTONE_RECORD: &'static str = "UPDATE record SET deleted = 1, \
           base_commit_id = COALESCE(commit_id, base_commit_id), \
           commit_id = NULL, version = ?2 \
         WHERE id = ?1 \
           AND (?3 IS NULL OR version <= ?3) \
           AND (?4 IS NULL OR commit_id = ?4) \
         RETURNING id";
    const LIST_PENDING_RECORDS: &'static str =
        "SELECT path, key, json(json), CASE WHEN deleted THEN 1 ELSE 0 END, base_commit_id, version \
         FROM record WHERE worktree_id = ?1 AND file_path = ?2 \
           AND commit_id IS NULL AND conflict IS NULL";
    const LIST_COMMITTED_RECORDS: &'static str = "SELECT path, key, json(json), commit_id \
         FROM record WHERE worktree_id = ?1 AND file_path = ?2 \
           AND commit_id IS NOT NULL AND conflict IS NULL";
    const UPSERT_FILE: &'static str =
        "INSERT INTO file (worktree_id, path, format, commit_id, source_oid) \
         VALUES (?1, ?2, ?3, ?4, ?5) \
         ON CONFLICT(worktree_id, path) DO UPDATE SET \
           format = excluded.format, \
           commit_id = excluded.commit_id, \
           source_oid = excluded.source_oid";
    const SYNC_UPSERT_RECORD: &'static str =
        "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, base_commit_id, deleted, version) \
         VALUES (?1, ?2, ?3, ?4, jsonb(?5), ?6, NULL, 0, ?7) \
         ON CONFLICT(worktree_id, file_path, path, key) WHERE conflict IS NULL DO UPDATE SET \
           json = excluded.json, \
           commit_id = excluded.commit_id, \
           base_commit_id = NULL, \
           deleted = 0, \
           version = excluded.version \
         RETURNING id";
    const LIST_FILE_RECORD_KEYS: &'static str = "SELECT path, key FROM record \
         WHERE worktree_id = ?1 AND file_path = ?2 AND conflict IS NULL";
    const DELETE_RECORD_BY_KEY: &'static str =
        "DELETE FROM record WHERE worktree_id = ?1 AND file_path = ?2 \
         AND path = ?3 AND key = ?4 AND conflict IS NULL RETURNING id";
    const INSERT_TXN: &'static str =
        "INSERT INTO txn (worktree_id, first_version, last_version, author, message, created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)";
    const LIST_CONFLICT_RECORDS: &'static str =
        "SELECT id, path, key, json(json), CASE WHEN deleted THEN 1 ELSE 0 END, conflict \
         FROM record WHERE worktree_id = ?1 AND file_path = ?2 AND conflict IS NOT NULL";
    const UPSERT_CONFLICT_RECORD: &'static str =
        "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, base_commit_id, deleted, version, conflict) \
         VALUES (?1, ?2, ?3, ?4, jsonb(?5), ?6, NULL, ?7, ?8, ?9) \
         ON CONFLICT(worktree_id, file_path, path, key) WHERE conflict IS NOT NULL DO UPDATE SET \
           json = excluded.json, \
           commit_id = excluded.commit_id, \
           deleted = excluded.deleted, \
           version = excluded.version, \
           conflict = excluded.conflict \
         RETURNING id";
    const SET_CONFLICT_STATE: &'static str = "UPDATE record SET conflict = ?5, version = ?6 \
         WHERE worktree_id = ?1 AND file_path = ?2 AND path = ?3 AND key = ?4 \
           AND conflict IS NOT NULL \
         RETURNING id";
    const DELETE_CONFLICT_RECORD: &'static str =
        "DELETE FROM record WHERE worktree_id = ?1 AND file_path = ?2 \
         AND path = ?3 AND key = ?4 AND conflict IS NOT NULL";
    const LOOKUP_OWN_RECORD: &'static str =
        "SELECT id, commit_id, version, CASE WHEN deleted THEN 1 ELSE 0 END FROM record \
         WHERE worktree_id = ?1 AND file_path = ?2 AND path = ?3 AND key = ?4 \
           AND conflict IS NULL";
}

#[cfg(feature = "postgres")]
impl Dialect for sqlx::Postgres {
    const NEXT_VERSION: &'static str = "UPDATE version_seq SET next_version = next_version + $2 \
         WHERE worktree_id = $1 RETURNING next_version - $2";
    const LOOKUP_COMMITS: &'static str =
        "SELECT r.id, r.commit_id, CASE WHEN r.deleted THEN 1::BIGINT ELSE 0::BIGINT END, r.version, \
                r.file_path, w.default_file_path \
         FROM worktree w \
         LEFT JOIN record r ON r.worktree_id = w.id \
                           AND r.path = $3 \
                           AND r.key = $4 \
                           AND r.conflict IS NULL \
                           AND ($2::TEXT IS NULL OR r.file_path = $2) \
         WHERE w.id = $1 \
         LIMIT 1";
    const FILE_FORMAT: &'static str =
        "SELECT format FROM file WHERE worktree_id = $1 AND path = $2";
    const INSERT_FILE: &'static str = "INSERT INTO file (worktree_id, path, format, commit_id) \
         VALUES ($1, $2, $3, NULL) ON CONFLICT (worktree_id, path) DO NOTHING";
    const DELETE_ALIASES: &'static str = "DELETE FROM alias WHERE record_id = $1";
    const INSERT_ALIAS: &'static str =
        "INSERT INTO alias (record_id, path, key) VALUES ($1, $2, $3) \
         ON CONFLICT DO NOTHING";
    // Embeds the OCC check in the ON CONFLICT DO UPDATE WHERE
    // (mirrors the SQLite impl; ``::BIGINT`` / ``::TEXT`` casts on
    // the bind sites are required because postgres can't infer the
    // type of a NULL parameter).
    // See the sqlite constants for the `base_commit_id` COALESCE
    // rationale; the semantics are identical here.
    const UPSERT_RECORD: &'static str =
        "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, base_commit_id, deleted, version) \
         VALUES ($1, $2, $3, $4, $5::jsonb, NULL, NULL, FALSE, $6) \
         ON CONFLICT(worktree_id, file_path, path, key) WHERE conflict IS NULL DO UPDATE SET \
           json = EXCLUDED.json, \
           base_commit_id = COALESCE(record.commit_id, record.base_commit_id), \
           commit_id = NULL, deleted = FALSE, version = EXCLUDED.version \
         WHERE ($7::BIGINT IS NULL OR record.version <= $7) \
           AND ($8::TEXT IS NULL OR record.commit_id = $8) \
         RETURNING id";
    const UPDATE_RECORD: &'static str = "UPDATE record SET json = $1::jsonb, \
           base_commit_id = COALESCE(commit_id, base_commit_id), \
           commit_id = NULL, deleted = FALSE, version = $3 \
         WHERE id = $2 \
           AND ($4::BIGINT IS NULL OR version <= $4) \
           AND ($5::TEXT IS NULL OR commit_id = $5) \
         RETURNING id";
    const TOMBSTONE_RECORD: &'static str = "UPDATE record SET deleted = TRUE, \
           base_commit_id = COALESCE(commit_id, base_commit_id), \
           commit_id = NULL, version = $2 \
         WHERE id = $1 \
           AND ($3::BIGINT IS NULL OR version <= $3) \
           AND ($4::TEXT IS NULL OR commit_id = $4) \
         RETURNING id";
    const LIST_PENDING_RECORDS: &'static str =
        "SELECT path, key, json::text, CASE WHEN deleted THEN 1::BIGINT ELSE 0::BIGINT END, base_commit_id, version \
         FROM record WHERE worktree_id = $1 AND file_path = $2 \
           AND commit_id IS NULL AND conflict IS NULL";
    const LIST_COMMITTED_RECORDS: &'static str = "SELECT path, key, json::text, commit_id \
         FROM record WHERE worktree_id = $1 AND file_path = $2 \
           AND commit_id IS NOT NULL AND conflict IS NULL";
    const UPSERT_FILE: &'static str =
        "INSERT INTO file (worktree_id, path, format, commit_id, source_oid) \
         VALUES ($1, $2, $3, $4, $5) \
         ON CONFLICT(worktree_id, path) DO UPDATE SET \
           format = EXCLUDED.format, \
           commit_id = EXCLUDED.commit_id, \
           source_oid = EXCLUDED.source_oid";
    const SYNC_UPSERT_RECORD: &'static str =
        "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, base_commit_id, deleted, version) \
         VALUES ($1, $2, $3, $4, $5::jsonb, $6, NULL, FALSE, $7) \
         ON CONFLICT(worktree_id, file_path, path, key) WHERE conflict IS NULL DO UPDATE SET \
           json = EXCLUDED.json, \
           commit_id = EXCLUDED.commit_id, \
           base_commit_id = NULL, \
           deleted = FALSE, \
           version = EXCLUDED.version \
         RETURNING id";
    const LIST_FILE_RECORD_KEYS: &'static str = "SELECT path, key FROM record \
         WHERE worktree_id = $1 AND file_path = $2 AND conflict IS NULL";
    const DELETE_RECORD_BY_KEY: &'static str =
        "DELETE FROM record WHERE worktree_id = $1 AND file_path = $2 \
         AND path = $3 AND key = $4 AND conflict IS NULL RETURNING id";
    // No `::TEXT` casts on the nullable binds: an INSERT into named
    // columns gives postgres the target type, unlike the WHERE-position
    // NULLs above.
    const INSERT_TXN: &'static str =
        "INSERT INTO txn (worktree_id, first_version, last_version, author, message, created_at) \
         VALUES ($1, $2, $3, $4, $5, $6)";
    const LIST_CONFLICT_RECORDS: &'static str =
        "SELECT id, path, key, json::text, CASE WHEN deleted THEN 1::BIGINT ELSE 0::BIGINT END, conflict \
         FROM record WHERE worktree_id = $1 AND file_path = $2 AND conflict IS NOT NULL";
    // `$7 <> 0` keeps the `deleted` bind an i64 on both backends, so the
    // shared helper body binds one type.
    const UPSERT_CONFLICT_RECORD: &'static str =
        "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, base_commit_id, deleted, version, conflict) \
         VALUES ($1, $2, $3, $4, $5::jsonb, $6, NULL, ($7 <> 0), $8, $9) \
         ON CONFLICT(worktree_id, file_path, path, key) WHERE conflict IS NOT NULL DO UPDATE SET \
           json = EXCLUDED.json, \
           commit_id = EXCLUDED.commit_id, \
           deleted = EXCLUDED.deleted, \
           version = EXCLUDED.version, \
           conflict = EXCLUDED.conflict \
         RETURNING id";
    const SET_CONFLICT_STATE: &'static str = "UPDATE record SET conflict = $5, version = $6 \
         WHERE worktree_id = $1 AND file_path = $2 AND path = $3 AND key = $4 \
           AND conflict IS NOT NULL \
         RETURNING id";
    const DELETE_CONFLICT_RECORD: &'static str =
        "DELETE FROM record WHERE worktree_id = $1 AND file_path = $2 \
         AND path = $3 AND key = $4 AND conflict IS NOT NULL";
    const LOOKUP_OWN_RECORD: &'static str = "SELECT id, commit_id, version, \
                CASE WHEN deleted THEN 1::BIGINT ELSE 0::BIGINT END FROM record \
         WHERE worktree_id = $1 AND file_path = $2 AND path = $3 AND key = $4 \
           AND conflict IS NULL";
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

/// Draw `count` consecutive version stamps from the family's sequence
/// and return the first, inside the same transaction as the record
/// mutation.
///
/// `family_id` is the family's root worktree (see
/// [`crate::db::worktree::family_id`]), not the worktree being written
/// to. A batch draws its whole range in one call: the row lock that
/// makes a range contiguous is then held for one statement rather than
/// for every write in the batch.
pub(crate) async fn next_version<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    family_id: i64,
    count: i64,
) -> Result<i64>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    let row: (i64,) = sqlx::query_as(DB::NEXT_VERSION)
        .bind(family_id)
        .bind(count)
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

/// Register `file_path` in the `file` table if it isn't there already.
///
/// Record rows carry a `record (worktree_id, file_path) -> file
/// (worktree_id, path)` foreign key, so a write naming a file the worktree
/// hasn't scanned yet (a cloudmap that doesn't exist on disk, say) has to
/// register it first. The file itself is synthesised later, by
/// [`crate::SyncedRepo::write_file`]. An existing row is left untouched so
/// this never clobbers a scanned file's format or `commit_id`.
pub(crate) async fn ensure_file<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    worktree_id: i64,
    file_path: &str,
    format: &str,
) -> Result<()>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
{
    sqlx::query(DB::INSERT_FILE)
        .bind(worktree_id)
        .bind(file_path)
        .bind(format)
        .execute(&mut **tx)
        .await?;
    Ok(())
}

/// Full file-row upsert for the from-disk re-sync: overwrites `format`
/// and `commit_id`, the scanned working tree being the source of
/// truth. Contrast [`ensure_file`], which registers a row only when
/// missing.
pub(crate) async fn upsert_file<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    worktree_id: i64,
    file_path: &str,
    format: &str,
    commit_id: Option<&str>,
    source_oid: &str,
) -> Result<()>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> Option<&'q str>: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
{
    sqlx::query(DB::UPSERT_FILE)
        .bind(worktree_id)
        .bind(file_path)
        .bind(format)
        .bind(commit_id)
        .bind(source_oid)
        .execute(&mut **tx)
        .await?;
    Ok(())
}

/// Record one batch write in the `txn` audit table.
///
/// Call inside the batch's own transaction, after its last record write
/// and before the commit, so the row lands only if the batch does.
/// `first_version..=last_version` is the inclusive range of
/// `record.version` stamps the batch drew; ranges recorded by concurrent
/// batches never interleave, because the first [`next_version`] draw
/// holds the worktree row lock until the transaction commits.
///
/// `commit_id` starts NULL — [`crate::db::commit::roll_forward`] stamps
/// it when the batch's writes reach a git commit.
pub(crate) async fn insert_txn<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    worktree_id: i64,
    first_version: i64,
    last_version: i64,
    author: Option<&str>,
    message: Option<&str>,
    created_at: &str,
) -> Result<()>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> Option<&'q str>: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
{
    sqlx::query(DB::INSERT_TXN)
        .bind(worktree_id)
        .bind(first_version)
        .bind(last_version)
        .bind(author)
        .bind(message)
        .bind(created_at)
        .execute(&mut **tx)
        .await?;
    Ok(())
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
    id: RecordId<'_>,
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
    let RecordId {
        worktree_id,
        file_path,
        path,
        key,
    } = id;
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
    id: RecordId<'_>,
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
    let RecordId {
        worktree_id,
        file_path,
        path,
        key,
    } = id;
    create_record(
        tx,
        RecordId {
            worktree_id,
            file_path,
            path,
            key,
        },
        json_text,
        version,
        expected_version,
        expected_commit,
    )
    .await
}

/// Re-sync record upsert: INSERT-or-overwrite, clearing any tombstone.
/// Unlike [`upsert_record`] the row's `commit_id` is set from the
/// `commit_id` bind (the file's resolved last commit, `None` when the
/// file is dirty) rather than forced to NULL, and no OCC predicate
/// applies — the working tree is the source of truth on this path.
pub(crate) async fn sync_upsert_record<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    id: RecordId<'_>,
    json_text: &str,
    commit_id: Option<&str>,
    version: i64,
) -> Result<i64>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> Option<&'q str>: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    let RecordId {
        worktree_id,
        file_path,
        path,
        key,
    } = id;
    let row: (i64,) = sqlx::query_as(DB::SYNC_UPSERT_RECORD)
        .bind(worktree_id)
        .bind(file_path)
        .bind(path)
        .bind(key)
        .bind(json_text)
        .bind(commit_id)
        .bind(version)
        .fetch_one(&mut **tx)
        .await?;
    Ok(row.0)
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

/// Hard-delete every record of `file_path` whose `(path, key)` isn't
/// in `keep`; returns how many went away. Re-sync counterpart of the
/// tombstoning [`delete_record`]: rows vanish because the file no
/// longer contains them, so there is no pending edit to preserve.
/// Per-row deletes keep the SQL simple — file record counts are small.
/// The `RETURNING id` + `fetch_all` count avoids needing a portable
/// `rows_affected()` bound (see the [`Dialect::UPDATE_RECORD`] note).
pub(crate) async fn delete_missing<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    worktree_id: i64,
    file_path: &str,
    keep: &BTreeSet<(String, String)>,
) -> Result<usize>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
    (String, String): for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    let rows: Vec<(String, String)> = sqlx::query_as(DB::LIST_FILE_RECORD_KEYS)
        .bind(worktree_id)
        .bind(file_path)
        .fetch_all(&mut **tx)
        .await?;
    let mut removed = 0usize;
    for pk in rows {
        if keep.contains(&pk) {
            continue;
        }
        let deleted: Vec<(i64,)> = sqlx::query_as(DB::DELETE_RECORD_BY_KEY)
            .bind(worktree_id)
            .bind(file_path)
            .bind(pk.0.as_str())
            .bind(pk.1.as_str())
            .fetch_all(&mut **tx)
            .await?;
        removed += deleted.len();
    }
    Ok(removed)
}

/// One in-flight row of a file, as [`list_pending_records`] returns it.
pub(crate) struct PendingRecord {
    pub(crate) path: String,
    pub(crate) key: String,
    pub(crate) json: serde_json::Value,
    pub(crate) deleted: bool,
    /// Commit the client's edit is based on; `None` for a create.
    pub(crate) base_commit_id: Option<String>,
    /// The row's version stamp, against which a
    /// `Git-Sync-Resolves-Version` trailer is compared.
    pub(crate) version: i64,
}

/// The database's own row at one key — id, attribution and version.
/// A tombstone is one of these like any other row; `deleted` is not
/// carried because every resolution rewrites the row outright.
pub(crate) struct OwnRecord {
    pub(crate) id: i64,
    pub(crate) commit_id: Option<String>,
    pub(crate) version: i64,
}

/// Read [`Dialect::LOOKUP_OWN_RECORD`]. `None` when the key has no row
/// of the database's own — only, say, a conflict row left behind.
pub(crate) async fn lookup_own_record<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    id: RecordId<'_>,
) -> Result<Option<OwnRecord>>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (i64, Option<String>, i64, i64):
        for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    let RecordId {
        worktree_id,
        file_path,
        path,
        key,
    } = id;
    let row: Option<(i64, Option<String>, i64, i64)> = sqlx::query_as(DB::LOOKUP_OWN_RECORD)
        .bind(worktree_id)
        .bind(file_path)
        .bind(path)
        .bind(key)
        .fetch_optional(&mut **tx)
        .await?;
    Ok(row.map(|(id, commit_id, version, _deleted)| OwnRecord {
        id,
        commit_id,
        version,
    }))
}

/// One conflict row of a file — the file's side of a record the two
/// sides disagree about, as [`list_conflict_records`] returns it.
pub(crate) struct ConflictRecord {
    pub(crate) json: serde_json::Value,
    /// The file no longer has this record: the tombstone-shaped
    /// conflict, where `json` is the value the file dropped.
    pub(crate) deleted: bool,
    pub(crate) state: crate::model::ConflictState,
}

/// The file's conflict rows, keyed by `(path, key)`.
pub(crate) async fn list_conflict_records<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    worktree_id: i64,
    file_path: &str,
) -> Result<std::collections::BTreeMap<(String, String), ConflictRecord>>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (i64, String, String, String, i64, String):
        for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    let rows: Vec<(i64, String, String, String, i64, String)> =
        sqlx::query_as(DB::LIST_CONFLICT_RECORDS)
            .bind(worktree_id)
            .bind(file_path)
            .fetch_all(&mut **tx)
            .await?;
    rows.into_iter()
        .map(|(_id, path, key, json_text, deleted, state)| {
            let json = serde_json::from_str(&json_text).map_err(|e| crate::error::Error::Json {
                path: path.clone(),
                source: e,
            })?;
            Ok((
                (path, key),
                ConflictRecord {
                    json,
                    deleted: deleted != 0,
                    state: crate::model::ConflictState::from_column(Some(&state))
                        .unwrap_or(crate::model::ConflictState::Conflict),
                },
            ))
        })
        .collect()
}

/// Create or refresh the conflict row at `id` — the file's value for a
/// record the database's own row disagrees with.
///
/// `commit_id` is the commit that last touched the path, which is what
/// carries this value; `deleted` marks the file having dropped the
/// record, in which case `json_text` is the value it dropped (the
/// column is NOT NULL and the tombstone convention already reads json as
/// "the value this removes"). Draw a fresh `version` only when something
/// actually moved — the row is a `list_changes` entry like any other, so
/// re-stamping it on every scan of the file would be churn.
pub(crate) async fn upsert_conflict_record<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    id: RecordId<'_>,
    json_text: &str,
    commit_id: Option<&str>,
    deleted: bool,
    version: i64,
    state: crate::model::ConflictState,
) -> Result<i64>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> Option<&'q str>: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    let RecordId {
        worktree_id,
        file_path,
        path,
        key,
    } = id;
    let row: (i64,) = sqlx::query_as(DB::UPSERT_CONFLICT_RECORD)
        .bind(worktree_id)
        .bind(file_path)
        .bind(path)
        .bind(key)
        .bind(json_text)
        .bind(commit_id)
        .bind(i64::from(deleted))
        .bind(version)
        .bind(state.as_str())
        .fetch_one(&mut **tx)
        .await?;
    Ok(row.0)
}

/// Flip an existing conflict row's state, stamping `version`.
/// `Ok(None)` when there is no conflict row at that key.
pub(crate) async fn set_conflict_state<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    id: RecordId<'_>,
    state: crate::model::ConflictState,
    version: i64,
) -> Result<Option<i64>>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    let RecordId {
        worktree_id,
        file_path,
        path,
        key,
    } = id;
    let row: Option<(i64,)> = sqlx::query_as(DB::SET_CONFLICT_STATE)
        .bind(worktree_id)
        .bind(file_path)
        .bind(path)
        .bind(key)
        .bind(state.as_str())
        .bind(version)
        .fetch_optional(&mut **tx)
        .await?;
    Ok(row.map(|(id,)| id))
}

/// Drop the conflict row at `id`, if there is one — the divergence is
/// settled, or has gone away on its own.
pub(crate) async fn delete_conflict_record<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    id: RecordId<'_>,
) -> Result<()>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
{
    let RecordId {
        worktree_id,
        file_path,
        path,
        key,
    } = id;
    sqlx::query(DB::DELETE_CONFLICT_RECORD)
        .bind(worktree_id)
        .bind(file_path)
        .bind(path)
        .bind(key)
        .execute(&mut **tx)
        .await?;
    Ok(())
}

/// The file's in-flight (`commit_id IS NULL`) rows, which a re-sync
/// must keep out of [`sync_upsert_record`] / [`delete_missing`]'s way.
pub(crate) async fn list_pending_records<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    worktree_id: i64,
    file_path: &str,
) -> Result<Vec<PendingRecord>>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (String, String, String, i64, Option<String>, i64):
        for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    let rows: Vec<(String, String, String, i64, Option<String>, i64)> =
        sqlx::query_as(DB::LIST_PENDING_RECORDS)
            .bind(worktree_id)
            .bind(file_path)
            .fetch_all(&mut **tx)
            .await?;
    rows.into_iter()
        .map(|(path, key, json_text, deleted, base_commit_id, version)| {
            let json = serde_json::from_str(&json_text).map_err(|e| crate::error::Error::Json {
                path: path.clone(),
                source: e,
            })?;
            Ok(PendingRecord {
                path,
                key,
                json,
                deleted: deleted != 0,
                base_commit_id,
                version,
            })
        })
        .collect()
}

/// The file's synced (`commit_id IS NOT NULL`) rows as
/// `(path, key) → (json, commit_id)`, for the re-sync path to compare
/// the freshly parsed file against — a record matching both is left
/// alone rather than re-upserted with a fresh version.
pub(crate) async fn list_committed_records<DB: Dialect>(
    tx: &mut sqlx::Transaction<'_, DB>,
    worktree_id: i64,
    file_path: &str,
) -> Result<std::collections::BTreeMap<(String, String), (serde_json::Value, String)>>
where
    for<'q> i64: Encode<'q, DB> + Type<DB>,
    for<'q> &'q str: Encode<'q, DB> + Type<DB>,
    for<'q> <DB as Database>::Arguments<'q>: IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: Executor<'c, Database = DB>,
    (String, String, String, String):
        for<'r> sqlx::FromRow<'r, <DB as Database>::Row> + Send + Unpin,
{
    let rows: Vec<(String, String, String, String)> = sqlx::query_as(DB::LIST_COMMITTED_RECORDS)
        .bind(worktree_id)
        .bind(file_path)
        .fetch_all(&mut **tx)
        .await?;
    rows.into_iter()
        .map(|(path, key, json_text, commit_id)| {
            let json = serde_json::from_str(&json_text).map_err(|e| crate::error::Error::Json {
                path: path.clone(),
                source: e,
            })?;
            Ok(((path, key), (json, commit_id)))
        })
        .collect()
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
