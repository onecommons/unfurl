// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `record` table reads and writes (non-transactional).

use std::collections::BTreeSet;

use crate::db::{Db, RecordId};
use crate::error::{Error, Result};
use crate::model::{FacetColumnRow, FacetPath, FacetRows, FacetSpec, QueryOp, Record, RecordQuery};

/// `(id, worktree_id, file_path, path, key, commit_id, json_text,
/// deleted, version)` — the row shape for the SQLite `get_by_id` query.
type FullRecordRowSqlite = (
    i64,
    i64,
    String,
    String,
    String,
    Option<String>,
    String,
    i64,
    i64,
);

#[cfg(feature = "postgres")]
type FullRecordRowPg = (
    i64,
    i64,
    String,
    String,
    String,
    Option<String>,
    serde_json::Value,
    bool,
    i64,
);

/// Atomically bump `worktree.next_version` and return a fresh version
/// stamp. Called from the from-disk re-sync path
/// ([`crate::SyncedRepo::update_from_working_dir`]).
pub(crate) async fn next_version_pool(db: &Db, worktree_id: i64) -> Result<i64> {
    match db {
        Db::Sqlite(pool) => {
            let row: (i64,) = sqlx::query_as(
                "UPDATE worktree SET next_version = next_version + 1 \
                 WHERE id = ?1 RETURNING next_version - 1",
            )
            .bind(worktree_id)
            .fetch_one(pool)
            .await?;
            Ok(row.0)
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let row: (i64,) = sqlx::query_as(
                "UPDATE worktree SET next_version = next_version + 1 \
                 WHERE id = $1 RETURNING next_version - 1",
            )
            .bind(worktree_id)
            .fetch_one(pool)
            .await?;
            Ok(row.0)
        }
    }
}

pub(crate) async fn upsert(
    db: &Db,
    id: RecordId<'_>,
    json: &serde_json::Value,
    commit_id: Option<&str>,
    version: i64,
) -> Result<i64> {
    let RecordId {
        worktree_id,
        file_path,
        path,
        key,
    } = id;
    let json_text = serde_json::to_string(json).map_err(|e| Error::Json {
        path: path.to_string(),
        source: e,
    })?;
    // Re-syncing from disk: any tombstone for this (path, key) must be
    // cleared since the value is reappearing in the source of truth.
    match db {
        Db::Sqlite(pool) => {
            let row: (i64,) = sqlx::query_as(
                "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, deleted, version) \
                 VALUES (?1, ?2, ?3, ?4, jsonb(?5), ?6, 0, ?7) \
                 ON CONFLICT(worktree_id, file_path, path, key) DO UPDATE SET \
                   json = excluded.json, \
                   commit_id = excluded.commit_id, \
                   deleted = 0, \
                   version = excluded.version \
                 RETURNING id",
            )
            .bind(worktree_id)
            .bind(file_path)
            .bind(path)
            .bind(key)
            .bind(&json_text)
            .bind(commit_id)
            .bind(version)
            .fetch_one(pool)
            .await?;
            Ok(row.0)
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let row: (i64,) = sqlx::query_as(
                "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, deleted, version) \
                 VALUES ($1, $2, $3, $4, $5::jsonb, $6, FALSE, $7) \
                 ON CONFLICT(worktree_id, file_path, path, key) DO UPDATE SET \
                   json = EXCLUDED.json, \
                   commit_id = EXCLUDED.commit_id, \
                   deleted = FALSE, \
                   version = EXCLUDED.version \
                 RETURNING id",
            )
            .bind(worktree_id)
            .bind(file_path)
            .bind(path)
            .bind(key)
            .bind(&json_text)
            .bind(commit_id)
            .bind(version)
            .fetch_one(pool)
            .await?;
            Ok(row.0)
        }
    }
}

pub(crate) async fn delete_missing(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
    keep: &BTreeSet<(String, String)>,
) -> Result<usize> {
    // Find all current (path, key) pairs for the file then delete those
    // not in the keep set. Per-row deletes keep the SQL simple — file
    // record counts are small.
    match db {
        Db::Sqlite(pool) => {
            let rows: Vec<(String, String)> = sqlx::query_as(
                "SELECT path, key FROM record WHERE worktree_id = ?1 AND file_path = ?2",
            )
            .bind(worktree_id)
            .bind(file_path)
            .fetch_all(pool)
            .await?;
            let mut removed = 0usize;
            for (p, k) in rows {
                if keep.contains(&(p.clone(), k.clone())) {
                    continue;
                }
                let res = sqlx::query(
                    "DELETE FROM record WHERE worktree_id = ?1 AND file_path = ?2 \
                     AND path = ?3 AND key = ?4",
                )
                .bind(worktree_id)
                .bind(file_path)
                .bind(&p)
                .bind(&k)
                .execute(pool)
                .await?;
                removed += res.rows_affected() as usize;
            }
            Ok(removed)
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let rows: Vec<(String, String)> = sqlx::query_as(
                "SELECT path, key FROM record WHERE worktree_id = $1 AND file_path = $2",
            )
            .bind(worktree_id)
            .bind(file_path)
            .fetch_all(pool)
            .await?;
            let mut removed = 0usize;
            for (p, k) in rows {
                if keep.contains(&(p.clone(), k.clone())) {
                    continue;
                }
                let res = sqlx::query(
                    "DELETE FROM record WHERE worktree_id = $1 AND file_path = $2 \
                     AND path = $3 AND key = $4",
                )
                .bind(worktree_id)
                .bind(file_path)
                .bind(&p)
                .bind(&k)
                .execute(pool)
                .await?;
                removed += res.rows_affected() as usize;
            }
            Ok(removed)
        }
    }
}

pub(crate) async fn list_dirty_files(db: &Db, worktree_id: i64) -> Result<Vec<String>> {
    // A file is dirty when it has at least one record row with
    // commit_id IS NULL — either an in-flight update / upsert (json
    // pending) or an in-flight delete (tombstone). Both cases need
    // `save_changes` to rewrite the file on disk.
    match db {
        Db::Sqlite(pool) => {
            let rows: Vec<(String,)> = sqlx::query_as(
                "SELECT DISTINCT file_path FROM record \
                 WHERE worktree_id = ?1 AND commit_id IS NULL",
            )
            .bind(worktree_id)
            .fetch_all(pool)
            .await?;
            Ok(rows.into_iter().map(|(p,)| p).collect())
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let rows: Vec<(String,)> = sqlx::query_as(
                "SELECT DISTINCT file_path FROM record \
                 WHERE worktree_id = $1 AND commit_id IS NULL",
            )
            .bind(worktree_id)
            .fetch_all(pool)
            .await?;
            Ok(rows.into_iter().map(|(p,)| p).collect())
        }
    }
}

/// Load all in-flight (`commit_id IS NULL`) record changes for
/// `file_path`, including tombstones (`deleted = TRUE`). Used by
/// `write_file` to apply only the diff against the on-disk document.
pub(crate) async fn load_pending(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
) -> Result<Vec<Record>> {
    let rows = match db {
        Db::Sqlite(pool) => {
            sqlx::query_as::<_, (i64, String, String, Option<String>, String, i64, i64)>(
                "SELECT id, path, key, commit_id, json(json), deleted, version FROM record \
                 WHERE worktree_id = ?1 AND file_path = ?2 AND commit_id IS NULL \
                 ORDER BY id",
            )
            .bind(worktree_id)
            .bind(file_path)
            .fetch_all(pool)
            .await?
            .into_iter()
            .map(|(id, p, k, c, t, d, v)| (id, p, k, c, t, d != 0, v))
            .collect::<Vec<_>>()
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            type PendingRowPg = (
                i64,
                String,
                String,
                Option<String>,
                serde_json::Value,
                bool,
                i64,
            );
            let pg_rows: Vec<PendingRowPg> = sqlx::query_as(
                "SELECT id, path, key, commit_id, json::jsonb, deleted, version FROM record \
                     WHERE worktree_id = $1 AND file_path = $2 AND commit_id IS NULL \
                     ORDER BY id",
            )
            .bind(worktree_id)
            .bind(file_path)
            .fetch_all(pool)
            .await?;
            pg_rows
                .into_iter()
                .map(|(id, p, k, c, v, d, ver)| (id, p, k, c, v.to_string(), d, ver))
                .collect()
        }
    };

    let mut out = Vec::with_capacity(rows.len());
    for (id, path, key, commit_id, json_text, deleted, version) in rows {
        let json: serde_json::Value =
            serde_json::from_str(&json_text).map_err(|e| Error::Json {
                path: path.clone(),
                source: e,
            })?;
        out.push(Record {
            id,
            worktree_id,
            file_path: file_path.to_string(),
            path,
            key,
            commit_id,
            json,
            deleted,
            version,
        });
    }
    Ok(out)
}

/// Search records by optional `file_path` / `path` / `key` filters.
/// All `Some(...)` filters AND together. With `alias = true` and
/// `key = Some(...)`, a record also matches when one of its alias rows
/// has that key (joined on `record_id`).
///
/// `since_version`, when set, restricts results to rows with
/// `version > since_version`. Pushed down into SQL so the database
/// drives the filter rather than the caller.
///
/// `type_names`, when set and non-empty, restricts results to records
/// whose JSON payload has a `type` object declaring at least one of
/// the given names as a key (the cloudmap `typeRef` shape). On
/// Postgres this uses the `?|` key-existence operator so the GIN
/// expression index over `(json -> 'type')` applies; on SQLite it
/// scans with `json_each`.
pub(crate) async fn find(db: &Db, worktree_id: i64, query: &RecordQuery) -> Result<Vec<Record>> {
    match db {
        Db::Sqlite(pool) => find_sqlite(pool, worktree_id, query).await,
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => find_pg(pool, worktree_id, query).await,
    }
}

/// Surface an [`sqlx::Arguments::add`] failure. `add` fails only when
/// a value's `Encode` impl can't represent it on the wire (a `u64`
/// above `i64::MAX` into sqlite, a decimal that doesn't fit postgres
/// `NUMERIC`, ...), and every argument type this module binds --
/// `i64`, strings, `f64`, `text[]` arrays, JSON values -- encodes
/// infallibly. Mapped to an error anyway, rather than unwrapped, so
/// that a future fallible bind surfaces as a query error instead of a
/// panic.
fn arg_err(err: sqlx::error::BoxDynError) -> Error {
    Error::Other(format!("failed to encode query argument: {err}"))
}

/// Append the record-filter clauses shared by [`find`] and [`facet`] to
/// `sql`, numbering placeholders from `*idx` in exactly the order
/// [`add_filter_args_sqlite`] encodes their values. `after` and `limit`
/// stay with [`find_sqlite`]: paging has no meaning in an aggregation.
fn push_filter_sql_sqlite(sql: &mut String, query: &RecordQuery, idx: &mut usize) {
    let alias_active = query.alias_active();
    let type_names = query.effective_type_names();
    if !query.include_deleted {
        sql.push_str(" AND r.deleted = 0");
    }
    if query.file_path.is_some() {
        sql.push_str(&format!(" AND r.file_path = ?{idx}"));
        *idx += 1;
    }
    if query.path.is_some() {
        sql.push_str(&format!(" AND r.path = ?{idx}"));
        *idx += 1;
    }
    if query.key.is_some() {
        if alias_active {
            sql.push_str(&format!(
                " AND (r.key = ?{idx} OR EXISTS (SELECT 1 FROM alias a WHERE a.record_id = r.id AND a.key = ?{idx}))"
            ));
        } else {
            sql.push_str(&format!(" AND r.key = ?{idx}"));
        }
        *idx += 1;
    }
    if let Some(ts) = type_names {
        // `json_each` over the record's `type` typeRef map yields one
        // row per declared type name (`key` column). Missing or
        // non-object `type` yields no matching rows.
        let start = *idx;
        let phs: Vec<String> = (0..ts.len()).map(|i| format!("?{}", start + i)).collect();
        *idx += ts.len();
        sql.push_str(&format!(
            " AND EXISTS (SELECT 1 FROM json_each(r.json, '$.type') jt \
             WHERE jt.key IN ({}))",
            phs.join(", ")
        ));
    }
    for jq in &query.json_queries {
        // `json_each` unwraps whatever is at the path: one row per element for
        // an array and a single row for a scalar, so "contains" and "equals"
        // are the same predicate and the record's shape doesn't have to be
        // known. Object members are *excluded* (`typeof(jq.key) != 'text'`, an
        // array's keys being its integer indexes and a scalar's key NULL) to
        // match postgres, where the equivalent `.*` jsonpath can't use a GIN
        // index; address a member by putting its key in the query path.
        // Booleans and null are matched on `type` because sqlite renders them
        // as 1/0/NULL, which would also equal the numbers 1 and 0.
        if jq.op == QueryOp::Exists {
            // `json_type` returns NULL only when the path doesn't resolve; a
            // JSON null yields the string 'null', so it counts as existing --
            // same as postgres' bare-path `@?`.
            sql.push_str(&format!(" AND json_type(r.json, ?{idx}) IS NOT NULL"));
            *idx += 1;
        } else if jq.op == QueryOp::StartsWith {
            // `jq.type = 'text'` keeps LIKE from coercing a number to text
            // (sqlite would match 4200 for the prefix "42"); postgres'
            // `starts with` is string-only for the same reason.
            sql.push_str(&format!(
                " AND EXISTS (SELECT 1 FROM json_each(r.json, ?{idx}) jq \
                 WHERE jq.type = 'text' AND jq.value LIKE ?{} ESCAPE '\\' \
                 AND typeof(jq.key) != 'text')",
                *idx + 1
            ));
            *idx += 2;
        } else {
            match &jq.value {
                // An array literal is an exact match, not a containment test:
                // compare the whole value at the path. Both sides are minified by
                // sqlite (`json_extract` renders containers as text, `json()`
                // normalises the bound literal), so whitespace doesn't matter.
                serde_json::Value::Array(_) => {
                    sql.push_str(&format!(
                        " AND json_extract(r.json, ?{idx}) = json(?{})",
                        *idx + 1
                    ));
                    *idx += 2;
                }
                serde_json::Value::Bool(b) => {
                    let ty = if *b { "true" } else { "false" };
                    sql.push_str(&format!(
                        " AND EXISTS (SELECT 1 FROM json_each(r.json, ?{idx}) jq \
                     WHERE jq.type = '{ty}' AND typeof(jq.key) != 'text')"
                    ));
                    *idx += 1;
                }
                serde_json::Value::Null => {
                    sql.push_str(&format!(
                        " AND EXISTS (SELECT 1 FROM json_each(r.json, ?{idx}) jq \
                     WHERE jq.type = 'null' AND typeof(jq.key) != 'text')"
                    ));
                    *idx += 1;
                }
                _ => {
                    sql.push_str(&format!(
                        " AND EXISTS (SELECT 1 FROM json_each(r.json, ?{idx}) jq \
                     WHERE jq.value = ?{} AND typeof(jq.key) != 'text')",
                        *idx + 1
                    ));
                    *idx += 2;
                }
            }
        }
    }
    if query.since_version.is_some() {
        sql.push_str(&format!(" AND r.version > ?{idx}"));
        *idx += 1;
    }
}

/// Encode the values for [`push_filter_sql_sqlite`]'s placeholders, in
/// the same order the clauses number them.
fn add_filter_args_sqlite<'q>(
    args: &mut sqlx::sqlite::SqliteArguments<'q>,
    query: &'q RecordQuery,
) -> Result<()> {
    use sqlx::Arguments;
    if let Some(fp) = &query.file_path {
        args.add(fp.as_str()).map_err(arg_err)?;
    }
    if let Some(p) = &query.path {
        args.add(p.as_str()).map_err(arg_err)?;
    }
    if let Some(k) = &query.key {
        args.add(k.as_str()).map_err(arg_err)?;
    }
    if let Some(ts) = query.effective_type_names() {
        for t in ts {
            args.add(t.as_str()).map_err(arg_err)?;
        }
    }
    for jq in &query.json_queries {
        args.add(jq.sql_path()).map_err(arg_err)?;
        if jq.op == QueryOp::Exists {
            // the path is the whole clause; nothing else to bind
        } else if jq.op == QueryOp::StartsWith {
            args.add(jq.like_pattern()).map_err(arg_err)?;
        } else {
            match &jq.value {
                // the `type` clause carries these; no value to bind
                serde_json::Value::Bool(_) | serde_json::Value::Null => {}
                // the whole array, rendered as JSON text for `json()`
                serde_json::Value::Array(_) => args.add(jq.value.to_string()).map_err(arg_err)?,
                serde_json::Value::String(s) => args.add(s.as_str()).map_err(arg_err)?,
                serde_json::Value::Number(n) => {
                    if let Some(i) = n.as_i64() {
                        args.add(i).map_err(arg_err)?;
                    } else {
                        args.add(n.as_f64().unwrap_or_default()).map_err(arg_err)?;
                    }
                }
                // arrays/objects can't be compared element-wise; match their text
                other => args.add(other.to_string()).map_err(arg_err)?,
            }
        }
    }
    if let Some(v) = query.since_version {
        args.add(v).map_err(arg_err)?;
    }
    Ok(())
}

async fn find_sqlite(
    pool: &sqlx::Pool<sqlx::Sqlite>,
    worktree_id: i64,
    query: &RecordQuery,
) -> Result<Vec<Record>> {
    use sqlx::Arguments;
    let RecordQuery { after, limit, .. } = query;
    let mut sql = String::from(
        "SELECT r.id, r.file_path, r.path, r.key, r.commit_id, json(r.json), r.version, \
         r.deleted FROM record r WHERE r.worktree_id = ?1",
    );
    let mut idx: usize = 2;
    push_filter_sql_sqlite(&mut sql, query, &mut idx);
    if after.is_some() {
        // Keyset cursor over the `ORDER BY` below. Row-value comparison
        // (sqlite >= 3.15) is the whole predicate, so the anchor row
        // itself need not still exist -- a record deleted between pages
        // doesn't strand the walk. sqlite's default BINARY collation
        // compares UTF-8 bytes, which is the ordering the page token
        // promises and the other two implementations reproduce.
        sql.push_str(&format!(" AND (r.path, r.key) > (?{idx}, ?{})", idx + 1));
        idx += 2;
    }
    sql.push_str(" ORDER BY r.path, r.key");
    if limit.is_some() {
        sql.push_str(&format!(" LIMIT ?{idx}"));
        idx += 1;
    }
    let _ = idx; // silence unused-assignment lint when the last bind isn't used

    let mut args = sqlx::sqlite::SqliteArguments::default();
    args.add(worktree_id).map_err(arg_err)?;
    add_filter_args_sqlite(&mut args, query)?;
    if let Some((p, k)) = after {
        args.add(p.as_str()).map_err(arg_err)?;
        args.add(k.as_str()).map_err(arg_err)?;
    }
    if let Some(n) = limit {
        args.add(*n).map_err(arg_err)?;
    }
    let rows = sqlx::query_as_with::<
        _,
        (
            i64,
            String,
            String,
            String,
            Option<String>,
            String,
            i64,
            i64,
        ),
        _,
    >(&sql, args)
    .fetch_all(pool)
    .await?;

    let mut out = Vec::with_capacity(rows.len());
    for (id, file_path, path, key, commit_id, json_text, version, deleted) in rows {
        let json: serde_json::Value =
            serde_json::from_str(&json_text).map_err(|e| Error::Json {
                path: path.clone(),
                source: e,
            })?;
        out.push(Record {
            id,
            worktree_id,
            file_path,
            path,
            key,
            commit_id,
            json,
            deleted: deleted != 0,
            version,
        });
    }
    Ok(out)
}

/// Postgres twin of [`push_filter_sql_sqlite`]: append the shared
/// record-filter clauses to `sql`, numbering placeholders from `*idx`
/// in exactly the order [`add_filter_args_pg`] encodes their values.
#[cfg(feature = "postgres")]
fn push_filter_sql_pg(sql: &mut String, query: &RecordQuery, idx: &mut usize) {
    let alias_active = query.alias_active();
    if !query.include_deleted {
        sql.push_str(" AND r.deleted = FALSE");
    }
    if query.file_path.is_some() {
        sql.push_str(&format!(" AND r.file_path = ${idx}"));
        *idx += 1;
    }
    if query.path.is_some() {
        sql.push_str(&format!(" AND r.path = ${idx}"));
        *idx += 1;
    }
    if query.key.is_some() {
        if alias_active {
            sql.push_str(&format!(
                " AND (r.key = ${idx} OR EXISTS (SELECT 1 FROM alias a WHERE a.record_id = r.id AND a.key = ${idx}))"
            ));
        } else {
            sql.push_str(&format!(" AND r.key = ${idx}"));
        }
        *idx += 1;
    }
    if query.effective_type_names().is_some() {
        // `?|` (jsonb key-exists-any) is served by the GIN expression
        // index over `(json -> 'type')`; see the migrations. (`?` here
        // is a jsonb operator, not a bind placeholder — Postgres binds
        // are `$N`.)
        sql.push_str(&format!(" AND r.json -> 'type' ?| ${idx}::text[]"));
        *idx += 1;
    }
    for jq in &query.json_queries {
        // `@?` is the *operator* form of `jsonb_path_exists`, and unlike the
        // function call it can be served by a GIN index over `json` (measured:
        // Bitmap Index Scan vs Seq Scan, and identical plans when no index
        // exists). It takes no `vars`, so the value is written into the
        // jsonpath itself — still one bound parameter. SQL/JSON path in `lax`
        // mode (the default) unwraps arrays and wraps scalars, so `[*]` covers
        // both.
        if jq.is_exact() {
            // Exact array match. `#>` equality is structural but can't use an
            // index, so it rides behind a containment pre-filter, which can:
            // every element of an equal array is contained, so the pre-filter
            // never drops a match.
            sql.push_str(&format!(
                " AND r.json @> ${idx}::jsonb AND r.json #> ${}::text[] = ${}::jsonb",
                *idx + 1,
                *idx + 2
            ));
            *idx += 3;
        } else {
            sql.push_str(&format!(" AND r.json @? ${idx}::jsonpath"));
            *idx += 1;
        }
    }
    if query.since_version.is_some() {
        sql.push_str(&format!(" AND r.version > ${idx}"));
        *idx += 1;
    }
}

/// Encode the values for [`push_filter_sql_pg`]'s placeholders, in the
/// same order the clauses number them.
#[cfg(feature = "postgres")]
fn add_filter_args_pg(args: &mut sqlx::postgres::PgArguments, query: &RecordQuery) -> Result<()> {
    use sqlx::Arguments;
    if let Some(fp) = &query.file_path {
        args.add(fp.as_str()).map_err(arg_err)?;
    }
    if let Some(p) = &query.path {
        args.add(p.as_str()).map_err(arg_err)?;
    }
    if let Some(k) = &query.key {
        args.add(k.as_str()).map_err(arg_err)?;
    }
    if let Some(ts) = query.effective_type_names() {
        args.add(ts).map_err(arg_err)?;
    }
    for jq in &query.json_queries {
        if jq.is_exact() {
            args.add(jq.containment()).map_err(arg_err)?;
            args.add(jq.tokens.clone()).map_err(arg_err)?;
            args.add(jq.value.clone()).map_err(arg_err)?;
        } else {
            args.add(jq.jsonpath()).map_err(arg_err)?;
        }
    }
    if let Some(v) = query.since_version {
        args.add(v).map_err(arg_err)?;
    }
    Ok(())
}

#[cfg(feature = "postgres")]
async fn find_pg(
    pool: &sqlx::Pool<sqlx::Postgres>,
    worktree_id: i64,
    query: &RecordQuery,
) -> Result<Vec<Record>> {
    use sqlx::Arguments;
    let RecordQuery { after, limit, .. } = query;
    let mut sql = String::from(
        "SELECT r.id, r.file_path, r.path, r.key, r.commit_id, r.json, r.version, r.deleted \
         FROM record r WHERE r.worktree_id = $1",
    );
    let mut idx: usize = 2;
    push_filter_sql_pg(&mut sql, query, &mut idx);
    if after.is_some() {
        // Keyset cursor; see the sqlite arm. `COLLATE "C"` here and on
        // the `ORDER BY` below pins byte-wise ordering: the database's
        // default collation is locale-dependent (`en_US.UTF-8` sorts
        // "é" before "z", byte order after), and a page token minted by
        // the sqlite or python implementation has to mean the same thing
        // here or a walk would skip or repeat records.
        sql.push_str(&format!(
            " AND (r.path COLLATE \"C\", r.key COLLATE \"C\") > (${idx}, ${})",
            idx + 1
        ));
        idx += 2;
    }
    sql.push_str(" ORDER BY r.path COLLATE \"C\", r.key COLLATE \"C\"");
    if limit.is_some() {
        sql.push_str(&format!(" LIMIT ${idx}"));
        idx += 1;
    }
    let _ = idx;

    let mut args = sqlx::postgres::PgArguments::default();
    args.add(worktree_id).map_err(arg_err)?;
    add_filter_args_pg(&mut args, query)?;
    if let Some((p, k)) = after {
        args.add(p.as_str()).map_err(arg_err)?;
        args.add(k.as_str()).map_err(arg_err)?;
    }
    if let Some(n) = limit {
        args.add(*n).map_err(arg_err)?;
    }
    let rows = sqlx::query_as_with::<
        _,
        (
            i64,
            String,
            String,
            String,
            Option<String>,
            serde_json::Value,
            i64,
            bool,
        ),
        _,
    >(&sql, args)
    .fetch_all(pool)
    .await?;
    Ok(rows
        .into_iter()
        .map(|(id, fp, p, k, cid, json, version, deleted)| Record {
            id,
            worktree_id,
            file_path: fp,
            path: p,
            key: k,
            commit_id: cid,
            json,
            deleted,
            version,
        })
        .collect())
}

/// Run a facet aggregation: group the records matching `query` by the
/// value at `spec.group`, count distinct records per group, and per
/// facet column per (group, member-values) combination.
///
/// Issues one `COUNT` for [`FacetRows::total`], one aggregation for the
/// per-group counts, and one per facet column, all sharing the same
/// filter clauses as [`find`] (minus `after` / `limit`, which have no
/// meaning in an aggregation). Values come back as extracted --
/// canonicalizing keys and merging spelling variants is the caller's
/// business.
pub(crate) async fn facet(
    db: &Db,
    worktree_id: i64,
    query: &RecordQuery,
    spec: &FacetSpec,
) -> Result<FacetRows> {
    match db {
        Db::Sqlite(pool) => facet_sqlite(pool, worktree_id, query, spec).await,
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => facet_pg(pool, worktree_id, query, spec).await,
    }
}

/// Parse a facet value rendered as JSON text back into a value.
fn parse_facet_value(text: &str) -> Result<serde_json::Value> {
    serde_json::from_str(text)
        .map_err(|e| Error::Other(format!("facet value {text:?} is not valid JSON: {e}")))
}

/// The value one `json_each` lateral row contributes under the facet
/// extraction rule, rendered as JSON text so every shape survives
/// `GROUP BY` and the trip back out: an object member's *key* (a JSON
/// string -- the `type` typeRef-map convention), a container element
/// verbatim, `true` / `false` / `null` by name (sqlite otherwise
/// renders them as 1 / 0 / NULL, colliding with the numbers 1 and 0),
/// and any other scalar through `json_quote`.
fn facet_value_sqlite(alias: &str) -> String {
    format!(
        "CASE WHEN typeof({alias}.key) = 'text' THEN json_quote({alias}.key) \
         WHEN {alias}.type IN ('object','array') THEN {alias}.value \
         WHEN {alias}.type IN ('true','false','null') THEN {alias}.type \
         ELSE json_quote({alias}.value) END"
    )
}

async fn facet_sqlite(
    pool: &sqlx::Pool<sqlx::Sqlite>,
    worktree_id: i64,
    query: &RecordQuery,
    spec: &FacetSpec,
) -> Result<FacetRows> {
    use sqlx::Arguments;
    let mut sql = String::from("SELECT COUNT(*) FROM record r WHERE r.worktree_id = ?1");
    let mut idx: usize = 2;
    push_filter_sql_sqlite(&mut sql, query, &mut idx);
    let _ = idx;
    let mut args = sqlx::sqlite::SqliteArguments::default();
    args.add(worktree_id).map_err(arg_err)?;
    add_filter_args_sqlite(&mut args, query)?;
    let (total,): (i64,) = sqlx::query_as_with(&sql, args).fetch_one(pool).await?;

    let groups = facet_aggregate_sqlite(pool, worktree_id, query, spec, &[])
        .await?
        .into_iter()
        .map(|row| (row.group, row.count))
        .collect();
    let mut columns = Vec::with_capacity(spec.columns.len());
    for members in &spec.columns {
        columns.push(facet_aggregate_sqlite(pool, worktree_id, query, spec, members).await?);
    }
    Ok(FacetRows {
        total,
        groups,
        columns,
    })
}

/// One sqlite facet aggregation over the group path plus `members`
/// (empty for the group-only counts).
///
/// Shape: one `json_each` lateral per path contributing values per
/// [`facet_value_sqlite`]; paths with [`FacetPath::rollup`] LEFT JOIN a
/// `MATERIALIZED` CTE of the `(member, bucket)` pairs (JSON-quoted so
/// both sides compare as JSON text) and group by
/// `COALESCE(bucket, value)` -- a value with pairs counts under each of
/// its buckets, one without falls back to itself. `COUNT(DISTINCT
/// r.id)` keeps a record that reaches the same cell through several
/// values (duplicate array elements, diamond rollup paths) counted
/// once.
async fn facet_aggregate_sqlite(
    pool: &sqlx::Pool<sqlx::Sqlite>,
    worktree_id: i64,
    query: &RecordQuery,
    spec: &FacetSpec,
    members: &[FacetPath],
) -> Result<Vec<FacetColumnRow>> {
    use sqlx::Arguments;
    use sqlx::Row as _;
    let group = &spec.group;
    let with_pairs =
        (group.rollup || members.iter().any(|m| m.rollup)) && !spec.rollup_pairs.is_empty();

    // Placeholder allocation order is bind order: worktree, filters,
    // group path, member paths, pairs. The WHERE fragment is built
    // first so the filters take the low indexes, then spliced in after
    // the joins -- `?N` placeholders don't care about textual position.
    let mut where_sql = String::new();
    let mut idx: usize = 2;
    push_filter_sql_sqlite(&mut where_sql, query, &mut idx);
    let group_idx = idx;
    idx += 1;
    let member_idx: Vec<usize> = members
        .iter()
        .map(|_| {
            let i = idx;
            idx += 1;
            i
        })
        .collect();
    let pairs_idx = with_pairs.then(|| {
        let i = idx;
        idx += 1;
        i
    });
    let _ = idx;

    let mut sql = String::new();
    if let Some(pi) = pairs_idx {
        // MATERIALIZED so the pairs are expanded once and the planner
        // can build an automatic index for the joins below, instead of
        // rescanning json_each per record row.
        sql.push_str(&format!(
            "WITH rollup_pairs(decl, anc) AS MATERIALIZED (\
             SELECT json_quote(c.value ->> 'd'), json_quote(c.value ->> 'a') \
             FROM json_each(?{pi}) c) "
        ));
    }
    let group_expr = facet_value_sqlite("jg");
    let group_out = if group.rollup && with_pairs {
        format!("COALESCE(mg.anc, {group_expr})")
    } else {
        group_expr.clone()
    };
    sql.push_str(&format!("SELECT {group_out} AS g0"));
    let member_exprs: Vec<String> = (0..members.len())
        .map(|i| facet_value_sqlite(&format!("j{i}")))
        .collect();
    for (i, member) in members.iter().enumerate() {
        let out = if member.rollup && with_pairs {
            format!("COALESCE(m{i}.anc, {})", member_exprs[i])
        } else {
            member_exprs[i].clone()
        };
        sql.push_str(&format!(", {out} AS v{i}"));
    }
    sql.push_str(", COUNT(DISTINCT r.id) AS n FROM record r");
    sql.push_str(&format!(" JOIN json_each(r.json, ?{group_idx}) jg"));
    if group.rollup && with_pairs {
        sql.push_str(&format!(
            " LEFT JOIN rollup_pairs mg ON mg.decl = {group_expr}"
        ));
    }
    for (i, member) in members.iter().enumerate() {
        sql.push_str(&format!(" JOIN json_each(r.json, ?{}) j{i}", member_idx[i]));
        if member.rollup && with_pairs {
            sql.push_str(&format!(
                " LEFT JOIN rollup_pairs m{i} ON m{i}.decl = {}",
                member_exprs[i]
            ));
        }
    }
    sql.push_str(" WHERE r.worktree_id = ?1");
    sql.push_str(&where_sql);
    sql.push_str(" GROUP BY g0");
    for i in 0..members.len() {
        sql.push_str(&format!(", v{i}"));
    }

    let mut args = sqlx::sqlite::SqliteArguments::default();
    args.add(worktree_id).map_err(arg_err)?;
    add_filter_args_sqlite(&mut args, query)?;
    args.add(group.sql_path()).map_err(arg_err)?;
    for member in members {
        args.add(member.sql_path()).map_err(arg_err)?;
    }
    if pairs_idx.is_some() {
        let pairs = serde_json::Value::Array(
            spec.rollup_pairs
                .iter()
                .map(|(d, a)| serde_json::json!({ "d": d, "a": a }))
                .collect(),
        );
        args.add(pairs.to_string()).map_err(arg_err)?;
    }
    let rows = sqlx::query_with(&sql, args).fetch_all(pool).await?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let group_text: String = row.try_get(0)?;
        let mut member_values = Vec::with_capacity(members.len());
        for i in 0..members.len() {
            let text: String = row.try_get(i + 1)?;
            member_values.push(parse_facet_value(&text)?);
        }
        let count: i64 = row.try_get(members.len() + 1)?;
        out.push(FacetColumnRow {
            group: parse_facet_value(&group_text)?,
            members: member_values,
            count,
        });
    }
    Ok(out)
}

/// The two laterals extracting one path's facet values on postgres.
///
/// The first lateral hoists `r.json #> $N::text[]` so the (possibly
/// TOASTed) value is extracted once; the second unwraps it per the
/// extraction rule as a `UNION ALL` of the three shapes. The `CASE`
/// guards are load-bearing: a set-returning function is evaluated
/// before any `WHERE` could filter it, so each branch has to feed
/// itself an empty container when the shape doesn't match. A missing
/// path (`jsonb_typeof` of NULL is NULL) falls through all three
/// branches, dropping the record -- the same as sqlite's `json_each`
/// returning no rows.
#[cfg(feature = "postgres")]
fn facet_lateral_pg(hoist: &str, alias: &str, param_idx: usize) -> String {
    format!(
        " CROSS JOIN LATERAL (SELECT r.json #> ${param_idx}::text[] AS v) {hoist} \
         CROSS JOIN LATERAL (\
         SELECT e FROM jsonb_array_elements(CASE WHEN jsonb_typeof({hoist}.v) = 'array' \
         THEN {hoist}.v ELSE '[]'::jsonb END) e \
         UNION ALL \
         SELECT to_jsonb(k) FROM jsonb_object_keys(CASE WHEN jsonb_typeof({hoist}.v) = 'object' \
         THEN {hoist}.v ELSE '{{}}'::jsonb END) k \
         UNION ALL \
         SELECT {hoist}.v WHERE jsonb_typeof({hoist}.v) NOT IN ('array','object')\
         ) {alias}(val)"
    )
}

#[cfg(feature = "postgres")]
async fn facet_pg(
    pool: &sqlx::Pool<sqlx::Postgres>,
    worktree_id: i64,
    query: &RecordQuery,
    spec: &FacetSpec,
) -> Result<FacetRows> {
    use sqlx::Arguments;
    let mut sql = String::from("SELECT COUNT(*) FROM record r WHERE r.worktree_id = $1");
    let mut idx: usize = 2;
    push_filter_sql_pg(&mut sql, query, &mut idx);
    let _ = idx;
    let mut args = sqlx::postgres::PgArguments::default();
    args.add(worktree_id).map_err(arg_err)?;
    add_filter_args_pg(&mut args, query)?;
    let (total,): (i64,) = sqlx::query_as_with(&sql, args).fetch_one(pool).await?;

    let groups = facet_aggregate_pg(pool, worktree_id, query, spec, &[])
        .await?
        .into_iter()
        .map(|row| (row.group, row.count))
        .collect();
    let mut columns = Vec::with_capacity(spec.columns.len());
    for members in &spec.columns {
        columns.push(facet_aggregate_pg(pool, worktree_id, query, spec, members).await?);
    }
    Ok(FacetRows {
        total,
        groups,
        columns,
    })
}

/// Postgres twin of [`facet_aggregate_sqlite`]. Values group as jsonb
/// (semantic equality, so object key order can't split a bucket) and
/// the rollup pairs arrive as two parallel `text[]` binds joined via
/// `unnest`, compared as jsonb strings.
#[cfg(feature = "postgres")]
async fn facet_aggregate_pg(
    pool: &sqlx::Pool<sqlx::Postgres>,
    worktree_id: i64,
    query: &RecordQuery,
    spec: &FacetSpec,
    members: &[FacetPath],
) -> Result<Vec<FacetColumnRow>> {
    use sqlx::Arguments;
    use sqlx::Row as _;
    let group = &spec.group;
    let with_pairs =
        (group.rollup || members.iter().any(|m| m.rollup)) && !spec.rollup_pairs.is_empty();

    // Placeholder allocation order is bind order: worktree, filters,
    // group path, member paths, pair members, pair buckets.
    let mut where_sql = String::new();
    let mut idx: usize = 2;
    push_filter_sql_pg(&mut where_sql, query, &mut idx);
    let group_idx = idx;
    idx += 1;
    let member_idx: Vec<usize> = members
        .iter()
        .map(|_| {
            let i = idx;
            idx += 1;
            i
        })
        .collect();
    let pairs_idx = with_pairs.then(|| {
        let (d, a) = (idx, idx + 1);
        idx += 2;
        (d, a)
    });
    let _ = idx;

    let group_out = if group.rollup && with_pairs {
        "COALESCE(to_jsonb(mg.anc), jg.val)"
    } else {
        "jg.val"
    };
    let mut sql = format!("SELECT {group_out} AS g0");
    for (i, member) in members.iter().enumerate() {
        let out = if member.rollup && with_pairs {
            format!("COALESCE(to_jsonb(m{i}.anc), j{i}.val)")
        } else {
            format!("j{i}.val")
        };
        sql.push_str(&format!(", {out} AS v{i}"));
    }
    sql.push_str(", COUNT(DISTINCT r.id) AS n FROM record r");
    sql.push_str(&facet_lateral_pg("hg", "jg", group_idx));
    if let Some((d_idx, a_idx)) = pairs_idx {
        if group.rollup {
            sql.push_str(&format!(
                " LEFT JOIN unnest(${d_idx}::text[], ${a_idx}::text[]) mg(decl, anc) \
                 ON to_jsonb(mg.decl) = jg.val"
            ));
        }
    }
    for (i, member) in members.iter().enumerate() {
        sql.push_str(&facet_lateral_pg(
            &format!("h{i}"),
            &format!("j{i}"),
            member_idx[i],
        ));
        if let Some((d_idx, a_idx)) = pairs_idx {
            if member.rollup {
                sql.push_str(&format!(
                    " LEFT JOIN unnest(${d_idx}::text[], ${a_idx}::text[]) m{i}(decl, anc) \
                     ON to_jsonb(m{i}.decl) = j{i}.val"
                ));
            }
        }
    }
    sql.push_str(" WHERE r.worktree_id = $1");
    sql.push_str(&where_sql);
    sql.push_str(" GROUP BY g0");
    for i in 0..members.len() {
        sql.push_str(&format!(", v{i}"));
    }

    let mut args = sqlx::postgres::PgArguments::default();
    args.add(worktree_id).map_err(arg_err)?;
    add_filter_args_pg(&mut args, query)?;
    args.add(&group.tokens).map_err(arg_err)?;
    for member in members {
        args.add(&member.tokens).map_err(arg_err)?;
    }
    if pairs_idx.is_some() {
        let decls: Vec<&str> = spec.rollup_pairs.iter().map(|(d, _)| d.as_str()).collect();
        let ancs: Vec<&str> = spec.rollup_pairs.iter().map(|(_, a)| a.as_str()).collect();
        args.add(decls).map_err(arg_err)?;
        args.add(ancs).map_err(arg_err)?;
    }
    let rows = sqlx::query_with(&sql, args).fetch_all(pool).await?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let group_value: serde_json::Value = row.try_get(0)?;
        let mut member_values = Vec::with_capacity(members.len());
        for i in 0..members.len() {
            member_values.push(row.try_get::<serde_json::Value, _>(i + 1)?);
        }
        let count: i64 = row.try_get(members.len() + 1)?;
        out.push(FacetColumnRow {
            group: group_value,
            members: member_values,
            count,
        });
    }
    Ok(out)
}

/// Search records whose `key` matches one of `keys`, optionally
/// resolving alias rows for the same set, and excluding rows whose
/// `id` appears in `exclude_ids`.
///
/// Wider variant of [`find`] for batch lookups: the
/// [`crate::SyncedRepo::find_records_follow`] walker uses it to issue
/// one query per BFS frontier rather than one query per follow edge.
/// Empty `keys` returns an empty `Vec` without touching the database.
pub(crate) async fn find_many(
    db: &Db,
    worktree_id: i64,
    keys: &[&str],
    alias: bool,
    exclude_ids: &[i64],
    since_version: Option<i64>,
) -> Result<Vec<Record>> {
    if keys.is_empty() {
        return Ok(Vec::new());
    }
    match db {
        Db::Sqlite(pool) => {
            find_many_sqlite(pool, worktree_id, keys, alias, exclude_ids, since_version).await
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            find_many_pg(pool, worktree_id, keys, alias, exclude_ids, since_version).await
        }
    }
}

async fn find_many_sqlite(
    pool: &sqlx::Pool<sqlx::Sqlite>,
    worktree_id: i64,
    keys: &[&str],
    alias: bool,
    exclude_ids: &[i64],
    since_version: Option<i64>,
) -> Result<Vec<Record>> {
    let mut sql = String::from(
        "SELECT r.id, r.file_path, r.path, r.key, r.commit_id, json(r.json), r.version FROM record r \
         WHERE r.worktree_id = ?1 AND r.deleted = 0",
    );
    let mut idx: usize = 2;

    let key_start = idx;
    let key_phs: Vec<String> = (0..keys.len())
        .map(|i| format!("?{}", key_start + i))
        .collect();
    idx += keys.len();
    if alias {
        let alias_start = idx;
        let alias_phs: Vec<String> = (0..keys.len())
            .map(|i| format!("?{}", alias_start + i))
            .collect();
        idx += keys.len();
        sql.push_str(&format!(
            " AND (r.key IN ({}) OR EXISTS (SELECT 1 FROM alias a WHERE a.record_id = r.id AND a.key IN ({})))",
            key_phs.join(", "),
            alias_phs.join(", ")
        ));
    } else {
        sql.push_str(&format!(" AND r.key IN ({})", key_phs.join(", ")));
    }

    if !exclude_ids.is_empty() {
        let exclude_start = idx;
        let exclude_phs: Vec<String> = (0..exclude_ids.len())
            .map(|i| format!("?{}", exclude_start + i))
            .collect();
        idx += exclude_ids.len();
        sql.push_str(&format!(" AND r.id NOT IN ({})", exclude_phs.join(", ")));
    }

    if since_version.is_some() {
        sql.push_str(&format!(" AND r.version > ?{idx}"));
        idx += 1;
    }
    sql.push_str(" ORDER BY r.path, r.key");
    let _ = idx;

    let mut q =
        sqlx::query_as::<_, (i64, String, String, String, Option<String>, String, i64)>(&sql)
            .bind(worktree_id);
    for k in keys {
        q = q.bind(*k);
    }
    if alias {
        for k in keys {
            q = q.bind(*k);
        }
    }
    for id in exclude_ids {
        q = q.bind(*id);
    }
    if let Some(v) = since_version {
        q = q.bind(v);
    }
    let rows = q.fetch_all(pool).await?;

    let mut out = Vec::with_capacity(rows.len());
    for (id, file_path, path, key, commit_id, json_text, version) in rows {
        let json: serde_json::Value =
            serde_json::from_str(&json_text).map_err(|e| Error::Json {
                path: path.clone(),
                source: e,
            })?;
        out.push(Record {
            id,
            worktree_id,
            file_path,
            path,
            key,
            commit_id,
            json,
            deleted: false,
            version,
        });
    }
    Ok(out)
}

#[cfg(feature = "postgres")]
async fn find_many_pg(
    pool: &sqlx::Pool<sqlx::Postgres>,
    worktree_id: i64,
    keys: &[&str],
    alias: bool,
    exclude_ids: &[i64],
    since_version: Option<i64>,
) -> Result<Vec<Record>> {
    let mut sql = String::from(
        "SELECT r.id, r.file_path, r.path, r.key, r.commit_id, r.json, r.version FROM record r \
         WHERE r.worktree_id = $1 AND r.deleted = FALSE",
    );
    let mut idx: usize = 2;

    let key_start = idx;
    let key_phs: Vec<String> = (0..keys.len())
        .map(|i| format!("${}", key_start + i))
        .collect();
    idx += keys.len();
    if alias {
        let alias_start = idx;
        let alias_phs: Vec<String> = (0..keys.len())
            .map(|i| format!("${}", alias_start + i))
            .collect();
        idx += keys.len();
        sql.push_str(&format!(
            " AND (r.key IN ({}) OR EXISTS (SELECT 1 FROM alias a WHERE a.record_id = r.id AND a.key IN ({})))",
            key_phs.join(", "),
            alias_phs.join(", ")
        ));
    } else {
        sql.push_str(&format!(" AND r.key IN ({})", key_phs.join(", ")));
    }

    if !exclude_ids.is_empty() {
        let exclude_start = idx;
        let exclude_phs: Vec<String> = (0..exclude_ids.len())
            .map(|i| format!("${}", exclude_start + i))
            .collect();
        idx += exclude_ids.len();
        sql.push_str(&format!(" AND r.id NOT IN ({})", exclude_phs.join(", ")));
    }

    if since_version.is_some() {
        sql.push_str(&format!(" AND r.version > ${idx}"));
        idx += 1;
    }
    sql.push_str(" ORDER BY r.path, r.key");
    let _ = idx;

    let mut q = sqlx::query_as::<
        _,
        (
            i64,
            String,
            String,
            String,
            Option<String>,
            serde_json::Value,
            i64,
        ),
    >(&sql)
    .bind(worktree_id);
    for k in keys {
        q = q.bind(*k);
    }
    if alias {
        for k in keys {
            q = q.bind(*k);
        }
    }
    for id in exclude_ids {
        q = q.bind(*id);
    }
    if let Some(v) = since_version {
        q = q.bind(v);
    }
    let rows = q.fetch_all(pool).await?;
    Ok(rows
        .into_iter()
        .map(|(id, fp, p, k, cid, json, version)| Record {
            id,
            worktree_id,
            file_path: fp,
            path: p,
            key: k,
            commit_id: cid,
            json,
            deleted: false,
            version,
        })
        .collect())
}

pub(crate) async fn get(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
) -> Result<Option<Record>> {
    match db {
        Db::Sqlite(pool) => {
            let row: Option<(i64, Option<String>, String, i64)> = sqlx::query_as(
                "SELECT id, commit_id, json(json), version FROM record \
                 WHERE worktree_id = ?1 AND file_path = ?2 AND path = ?3 AND key = ?4 \
                   AND deleted = 0",
            )
            .bind(worktree_id)
            .bind(file_path)
            .bind(path)
            .bind(key)
            .fetch_optional(pool)
            .await?;
            row_to_record(row, worktree_id, file_path, path, key)
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let row: Option<(i64, Option<String>, serde_json::Value, i64)> = sqlx::query_as(
                "SELECT id, commit_id, json, version FROM record \
                 WHERE worktree_id = $1 AND file_path = $2 AND path = $3 AND key = $4 \
                   AND deleted = FALSE",
            )
            .bind(worktree_id)
            .bind(file_path)
            .bind(path)
            .bind(key)
            .fetch_optional(pool)
            .await?;
            match row {
                Some((id, commit_id, json, version)) => Ok(Some(Record {
                    id,
                    worktree_id,
                    file_path: file_path.to_string(),
                    path: path.to_string(),
                    key: key.to_string(),
                    commit_id,
                    json,
                    deleted: false,
                    version,
                })),
                None => Ok(None),
            }
        }
    }
}

fn row_to_record(
    row: Option<(i64, Option<String>, String, i64)>,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
) -> Result<Option<Record>> {
    match row {
        Some((id, commit_id, json_text, version)) => {
            let json: serde_json::Value =
                serde_json::from_str(&json_text).map_err(|e| Error::Json {
                    path: path.to_string(),
                    source: e,
                })?;
            Ok(Some(Record {
                id,
                worktree_id,
                file_path: file_path.to_string(),
                path: path.to_string(),
                key: key.to_string(),
                commit_id,
                json,
                deleted: false,
                version,
            }))
        }
        None => Ok(None),
    }
}

pub(crate) async fn get_by_id(db: &Db, id: i64) -> Result<Option<Record>> {
    match db {
        Db::Sqlite(pool) => {
            let row: Option<FullRecordRowSqlite> = sqlx::query_as(
                "SELECT id, worktree_id, file_path, path, key, commit_id, json(json), deleted, version \
                     FROM record WHERE id = ?1",
            )
            .bind(id)
            .fetch_optional(pool)
            .await?;
            match row {
                Some((id, wt, fp, p, k, c, t, d, version)) => {
                    let json: serde_json::Value =
                        serde_json::from_str(&t).map_err(|e| Error::Json {
                            path: p.clone(),
                            source: e,
                        })?;
                    Ok(Some(Record {
                        id,
                        worktree_id: wt,
                        file_path: fp,
                        path: p,
                        key: k,
                        commit_id: c,
                        json,
                        deleted: d != 0,
                        version,
                    }))
                }
                None => Ok(None),
            }
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let row: Option<FullRecordRowPg> = sqlx::query_as(
                "SELECT id, worktree_id, file_path, path, key, commit_id, json, deleted, version \
                     FROM record WHERE id = $1",
            )
            .bind(id)
            .fetch_optional(pool)
            .await?;
            match row {
                Some((id, wt, fp, p, k, c, json, d, version)) => Ok(Some(Record {
                    id,
                    worktree_id: wt,
                    file_path: fp,
                    path: p,
                    key: k,
                    commit_id: c,
                    json,
                    deleted: d,
                    version,
                })),
                None => Ok(None),
            }
        }
    }
}

/// Change-detection probe for a section: `(COUNT(*), MAX(version))`
/// over every row with `record.path = path`, **tombstones included**
/// (no `deleted` filter).
///
/// The pair moves whenever the section's contents change: an upsert
/// or CRUD delete bumps that row's `version` (moving `MAX`), and a
/// hard delete during re-sync ([`delete_missing`]) drops `COUNT` —
/// while a simultaneous delete + add still moves `MAX` via the added
/// row's fresh version. Writes to other sections touch neither.
/// Satisfiable from the `(worktree_id, path, key)` index.
pub(crate) async fn section_stat(
    db: &Db,
    worktree_id: i64,
    path: &str,
) -> Result<(i64, Option<i64>)> {
    match db {
        Db::Sqlite(pool) => {
            let row: (i64, Option<i64>) = sqlx::query_as(
                "SELECT COUNT(*), MAX(version) FROM record \
                 WHERE worktree_id = ?1 AND path = ?2",
            )
            .bind(worktree_id)
            .bind(path)
            .fetch_one(pool)
            .await?;
            Ok(row)
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let row: (i64, Option<i64>) = sqlx::query_as(
                "SELECT COUNT(*), MAX(version) FROM record \
                 WHERE worktree_id = $1 AND path = $2",
            )
            .bind(worktree_id)
            .bind(path)
            .fetch_one(pool)
            .await?;
            Ok(row)
        }
    }
}

/// Listing API: when `since` is `Some(v)`, return every record whose
/// `version > v` (committed or in-flight, including tombstones); when
/// `since` is `None`, return only the in-flight (`commit_id IS NULL`)
/// records — what `commit_repository` would write next.
pub(crate) async fn list_changes(
    db: &Db,
    worktree_id: i64,
    since: Option<i64>,
) -> Result<Vec<Record>> {
    match db {
        Db::Sqlite(pool) => {
            let rows: Vec<FullRecordRowSqlite> = match since {
                Some(v) => {
                    sqlx::query_as(
                        "SELECT id, worktree_id, file_path, path, key, commit_id, \
                                json(json), deleted, version \
                         FROM record WHERE worktree_id = ?1 AND version > ?2 \
                         ORDER BY version",
                    )
                    .bind(worktree_id)
                    .bind(v)
                    .fetch_all(pool)
                    .await?
                }
                None => {
                    sqlx::query_as(
                        "SELECT id, worktree_id, file_path, path, key, commit_id, \
                                json(json), deleted, version \
                         FROM record WHERE worktree_id = ?1 AND commit_id IS NULL \
                         ORDER BY version",
                    )
                    .bind(worktree_id)
                    .fetch_all(pool)
                    .await?
                }
            };
            let mut out = Vec::with_capacity(rows.len());
            for (id, wt, fp, p, k, c, t, d, version) in rows {
                let json: serde_json::Value =
                    serde_json::from_str(&t).map_err(|e| Error::Json {
                        path: p.clone(),
                        source: e,
                    })?;
                out.push(Record {
                    id,
                    worktree_id: wt,
                    file_path: fp,
                    path: p,
                    key: k,
                    commit_id: c,
                    json,
                    deleted: d != 0,
                    version,
                });
            }
            Ok(out)
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let rows: Vec<FullRecordRowPg> = match since {
                Some(v) => {
                    sqlx::query_as(
                        "SELECT id, worktree_id, file_path, path, key, commit_id, \
                                json, deleted, version \
                         FROM record WHERE worktree_id = $1 AND version > $2 \
                         ORDER BY version",
                    )
                    .bind(worktree_id)
                    .bind(v)
                    .fetch_all(pool)
                    .await?
                }
                None => {
                    sqlx::query_as(
                        "SELECT id, worktree_id, file_path, path, key, commit_id, \
                                json, deleted, version \
                         FROM record WHERE worktree_id = $1 AND commit_id IS NULL \
                         ORDER BY version",
                    )
                    .bind(worktree_id)
                    .fetch_all(pool)
                    .await?
                }
            };
            Ok(rows
                .into_iter()
                .map(|(id, wt, fp, p, k, c, json, d, version)| Record {
                    id,
                    worktree_id: wt,
                    file_path: fp,
                    path: p,
                    key: k,
                    commit_id: c,
                    json,
                    deleted: d,
                    version,
                })
                .collect())
        }
    }
}
