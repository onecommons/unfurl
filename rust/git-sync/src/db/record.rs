// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `record` table reads and writes (non-transactional).

use std::collections::BTreeSet;

use crate::db::{Db, RecordId};
use crate::error::{Error, Result};
use crate::model::{QueryOp, Record, RecordQuery};

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

async fn find_sqlite(
    pool: &sqlx::Pool<sqlx::Sqlite>,
    worktree_id: i64,
    query: &RecordQuery,
) -> Result<Vec<Record>> {
    let RecordQuery {
        file_path,
        path,
        key,
        since_version,
        json_query,
        after,
        limit,
        ..
    } = query;
    let alias_active = query.alias_active();
    let type_names = query.effective_type_names();
    let mut sql = String::from(
        "SELECT r.id, r.file_path, r.path, r.key, r.commit_id, json(r.json), r.version FROM record r \
         WHERE r.worktree_id = ?1 AND r.deleted = 0",
    );
    let mut idx: usize = 2;
    if file_path.is_some() {
        sql.push_str(&format!(" AND r.file_path = ?{idx}"));
        idx += 1;
    }
    if path.is_some() {
        sql.push_str(&format!(" AND r.path = ?{idx}"));
        idx += 1;
    }
    if let Some(_k) = key {
        if alias_active {
            sql.push_str(&format!(
                " AND (r.key = ?{idx} OR EXISTS (SELECT 1 FROM alias a WHERE a.record_id = r.id AND a.key = ?{idx}))"
            ));
        } else {
            sql.push_str(&format!(" AND r.key = ?{idx}"));
        }
        idx += 1;
    }
    if let Some(ts) = type_names {
        // `json_each` over the record's `type` typeRef map yields one
        // row per declared type name (`key` column). Missing or
        // non-object `type` yields no matching rows.
        let start = idx;
        let phs: Vec<String> = (0..ts.len()).map(|i| format!("?{}", start + i)).collect();
        idx += ts.len();
        sql.push_str(&format!(
            " AND EXISTS (SELECT 1 FROM json_each(r.json, '$.type') jt \
             WHERE jt.key IN ({}))",
            phs.join(", ")
        ));
    }
    if let Some(jq) = json_query {
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
            idx += 1;
        } else if jq.op == QueryOp::StartsWith {
            // `jq.type = 'text'` keeps LIKE from coercing a number to text
            // (sqlite would match 4200 for the prefix "42"); postgres'
            // `starts with` is string-only for the same reason.
            sql.push_str(&format!(
                " AND EXISTS (SELECT 1 FROM json_each(r.json, ?{idx}) jq \
                 WHERE jq.type = 'text' AND jq.value LIKE ?{} ESCAPE '\\' \
                 AND typeof(jq.key) != 'text')",
                idx + 1
            ));
            idx += 2;
        } else {
            match &jq.value {
                // An array literal is an exact match, not a containment test:
                // compare the whole value at the path. Both sides are minified by
                // sqlite (`json_extract` renders containers as text, `json()`
                // normalises the bound literal), so whitespace doesn't matter.
                serde_json::Value::Array(_) => {
                    sql.push_str(&format!(
                        " AND json_extract(r.json, ?{idx}) = json(?{})",
                        idx + 1
                    ));
                    idx += 2;
                }
                serde_json::Value::Bool(b) => {
                    let ty = if *b { "true" } else { "false" };
                    sql.push_str(&format!(
                        " AND EXISTS (SELECT 1 FROM json_each(r.json, ?{idx}) jq \
                     WHERE jq.type = '{ty}' AND typeof(jq.key) != 'text')"
                    ));
                    idx += 1;
                }
                serde_json::Value::Null => {
                    sql.push_str(&format!(
                        " AND EXISTS (SELECT 1 FROM json_each(r.json, ?{idx}) jq \
                     WHERE jq.type = 'null' AND typeof(jq.key) != 'text')"
                    ));
                    idx += 1;
                }
                _ => {
                    sql.push_str(&format!(
                        " AND EXISTS (SELECT 1 FROM json_each(r.json, ?{idx}) jq \
                     WHERE jq.value = ?{} AND typeof(jq.key) != 'text')",
                        idx + 1
                    ));
                    idx += 2;
                }
            }
        }
    }
    if since_version.is_some() {
        sql.push_str(&format!(" AND r.version > ?{idx}"));
        idx += 1;
    }
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

    let mut q =
        sqlx::query_as::<_, (i64, String, String, String, Option<String>, String, i64)>(&sql)
            .bind(worktree_id);
    if let Some(fp) = file_path {
        q = q.bind(fp);
    }
    if let Some(p) = path {
        q = q.bind(p);
    }
    if let Some(k) = key {
        q = q.bind(k);
    }
    if let Some(ts) = type_names {
        for t in ts {
            q = q.bind(t.as_str());
        }
    }
    if let Some(jq) = json_query {
        q = q.bind(jq.sql_path());
        if jq.op == QueryOp::Exists {
            // the path is the whole clause; nothing else to bind
        } else if jq.op == QueryOp::StartsWith {
            q = q.bind(jq.like_pattern());
        } else {
            match &jq.value {
                // the `type` clause carries these; no value to bind
                serde_json::Value::Bool(_) | serde_json::Value::Null => {}
                // the whole array, rendered as JSON text for `json()`
                serde_json::Value::Array(_) => q = q.bind(jq.value.to_string()),
                serde_json::Value::String(s) => q = q.bind(s.clone()),
                serde_json::Value::Number(n) => {
                    if let Some(i) = n.as_i64() {
                        q = q.bind(i);
                    } else {
                        q = q.bind(n.as_f64().unwrap_or_default());
                    }
                }
                // arrays/objects can't be compared element-wise; match their text
                other => q = q.bind(other.to_string()),
            }
        }
    }
    if let Some(v) = since_version {
        q = q.bind(v);
    }
    if let Some((p, k)) = after {
        q = q.bind(p).bind(k);
    }
    if let Some(n) = limit {
        q = q.bind(n);
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
async fn find_pg(
    pool: &sqlx::Pool<sqlx::Postgres>,
    worktree_id: i64,
    query: &RecordQuery,
) -> Result<Vec<Record>> {
    let RecordQuery {
        file_path,
        path,
        key,
        since_version,
        json_query,
        after,
        limit,
        ..
    } = query;
    let alias_active = query.alias_active();
    let type_names = query.effective_type_names();
    let mut sql = String::from(
        "SELECT r.id, r.file_path, r.path, r.key, r.commit_id, r.json, r.version FROM record r \
         WHERE r.worktree_id = $1 AND r.deleted = FALSE",
    );
    let mut idx: usize = 2;
    if file_path.is_some() {
        sql.push_str(&format!(" AND r.file_path = ${idx}"));
        idx += 1;
    }
    if path.is_some() {
        sql.push_str(&format!(" AND r.path = ${idx}"));
        idx += 1;
    }
    if key.is_some() {
        if alias_active {
            sql.push_str(&format!(
                " AND (r.key = ${idx} OR EXISTS (SELECT 1 FROM alias a WHERE a.record_id = r.id AND a.key = ${idx}))"
            ));
        } else {
            sql.push_str(&format!(" AND r.key = ${idx}"));
        }
        idx += 1;
    }
    if type_names.is_some() {
        // `?|` (jsonb key-exists-any) is served by the GIN expression
        // index over `(json -> 'type')`; see the migrations. (`?` here
        // is a jsonb operator, not a bind placeholder — Postgres binds
        // are `$N`.)
        sql.push_str(&format!(" AND r.json -> 'type' ?| ${idx}::text[]"));
        idx += 1;
    }
    if json_query.is_some() {
        // `@?` is the *operator* form of `jsonb_path_exists`, and unlike the
        // function call it can be served by a GIN index over `json` (measured:
        // Bitmap Index Scan vs Seq Scan, and identical plans when no index
        // exists). It takes no `vars`, so the value is written into the
        // jsonpath itself — still one bound parameter. SQL/JSON path in `lax`
        // mode (the default) unwraps arrays and wraps scalars, so `[*]` covers
        // both.
        if json_query.as_ref().is_some_and(|jq| jq.is_exact()) {
            // Exact array match. `#>` equality is structural but can't use an
            // index, so it rides behind a containment pre-filter, which can:
            // every element of an equal array is contained, so the pre-filter
            // never drops a match.
            sql.push_str(&format!(
                " AND r.json @> ${idx}::jsonb AND r.json #> ${}::text[] = ${}::jsonb",
                idx + 1,
                idx + 2
            ));
            idx += 3;
        } else {
            sql.push_str(&format!(" AND r.json @? ${idx}::jsonpath"));
            idx += 1;
        }
    }
    if since_version.is_some() {
        sql.push_str(&format!(" AND r.version > ${idx}"));
        idx += 1;
    }
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
    if let Some(fp) = file_path {
        q = q.bind(fp);
    }
    if let Some(p) = path {
        q = q.bind(p);
    }
    if let Some(k) = key {
        q = q.bind(k);
    }
    if let Some(ts) = type_names {
        q = q.bind(ts);
    }
    if let Some(jq) = json_query {
        if jq.is_exact() {
            q = q.bind(jq.containment());
            q = q.bind(jq.tokens.clone());
            q = q.bind(jq.value.clone());
        } else {
            q = q.bind(jq.jsonpath());
        }
    }
    if let Some(v) = since_version {
        q = q.bind(v);
    }
    if let Some((p, k)) = after {
        q = q.bind(p).bind(k);
    }
    if let Some(n) = limit {
        q = q.bind(n);
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
