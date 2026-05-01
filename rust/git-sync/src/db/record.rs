// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `record` table reads and writes (non-transactional).

use std::collections::BTreeSet;

use crate::db::Db;
use crate::error::{Error, Result};
use crate::model::Record;

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
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
    json: &serde_json::Value,
    commit_id: Option<&str>,
    version: i64,
) -> Result<i64> {
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
pub(crate) async fn find(
    db: &Db,
    worktree_id: i64,
    file_path: Option<&str>,
    path: Option<&str>,
    key: Option<&str>,
    alias: bool,
    since_version: Option<i64>,
) -> Result<Vec<Record>> {
    // The alias OR-clause is a no-op without a key.
    let alias_active = alias && key.is_some();
    match db {
        Db::Sqlite(pool) => {
            find_sqlite(
                pool,
                worktree_id,
                file_path,
                path,
                key,
                alias_active,
                since_version,
            )
            .await
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            find_pg(
                pool,
                worktree_id,
                file_path,
                path,
                key,
                alias_active,
                since_version,
            )
            .await
        }
    }
}

async fn find_sqlite(
    pool: &sqlx::Pool<sqlx::Sqlite>,
    worktree_id: i64,
    file_path: Option<&str>,
    path: Option<&str>,
    key: Option<&str>,
    alias_active: bool,
    since_version: Option<i64>,
) -> Result<Vec<Record>> {
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
    if since_version.is_some() {
        sql.push_str(&format!(" AND r.version > ?{idx}"));
        idx += 1;
    }
    sql.push_str(" ORDER BY r.path, r.key");
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
async fn find_pg(
    pool: &sqlx::Pool<sqlx::Postgres>,
    worktree_id: i64,
    file_path: Option<&str>,
    path: Option<&str>,
    key: Option<&str>,
    alias_active: bool,
    since_version: Option<i64>,
) -> Result<Vec<Record>> {
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
    if let Some(fp) = file_path {
        q = q.bind(fp);
    }
    if let Some(p) = path {
        q = q.bind(p);
    }
    if let Some(k) = key {
        q = q.bind(k);
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
