// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `record` table reads and writes (non-transactional).

use std::collections::BTreeSet;

use crate::db::Db;
use crate::error::{Error, Result};
use crate::model::Record;

/// `(id, worktree_id, file_path, path, key, commit_id, json_text,
/// deleted)` — the row shape for the SQLite `get_by_id` query.
type FullRecordRowSqlite = (
    i64,
    i64,
    String,
    String,
    String,
    Option<String>,
    String,
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
);

pub(crate) async fn upsert(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
    json: &serde_json::Value,
    commit_id: Option<&str>,
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
                "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, deleted) \
                 VALUES (?1, ?2, ?3, ?4, jsonb(?5), ?6, 0) \
                 ON CONFLICT(worktree_id, file_path, path, key) DO UPDATE SET \
                   json = excluded.json, \
                   commit_id = excluded.commit_id, \
                   deleted = 0 \
                 RETURNING id",
            )
            .bind(worktree_id)
            .bind(file_path)
            .bind(path)
            .bind(key)
            .bind(&json_text)
            .bind(commit_id)
            .fetch_one(pool)
            .await?;
            Ok(row.0)
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let row: (i64,) = sqlx::query_as(
                "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, deleted) \
                 VALUES ($1, $2, $3, $4, $5::jsonb, $6, FALSE) \
                 ON CONFLICT(worktree_id, file_path, path, key) DO UPDATE SET \
                   json = EXCLUDED.json, \
                   commit_id = EXCLUDED.commit_id, \
                   deleted = FALSE \
                 RETURNING id",
            )
            .bind(worktree_id)
            .bind(file_path)
            .bind(path)
            .bind(key)
            .bind(&json_text)
            .bind(commit_id)
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
            sqlx::query_as::<_, (i64, String, String, Option<String>, String, i64)>(
                "SELECT id, path, key, commit_id, json(json), deleted FROM record \
                 WHERE worktree_id = ?1 AND file_path = ?2 AND commit_id IS NULL \
                 ORDER BY id",
            )
            .bind(worktree_id)
            .bind(file_path)
            .fetch_all(pool)
            .await?
            .into_iter()
            .map(|(id, p, k, c, t, d)| (id, p, k, c, t, d != 0))
            .collect::<Vec<_>>()
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let pg_rows: Vec<(i64, String, String, Option<String>, serde_json::Value, bool)> =
                sqlx::query_as(
                    "SELECT id, path, key, commit_id, json::jsonb, deleted FROM record \
                     WHERE worktree_id = $1 AND file_path = $2 AND commit_id IS NULL \
                     ORDER BY id",
                )
                .bind(worktree_id)
                .bind(file_path)
                .fetch_all(pool)
                .await?;
            pg_rows
                .into_iter()
                .map(|(id, p, k, c, v, d)| (id, p, k, c, v.to_string(), d))
                .collect()
        }
    };

    let mut out = Vec::with_capacity(rows.len());
    for (id, path, key, commit_id, json_text, deleted) in rows {
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
        });
    }
    Ok(out)
}

pub(crate) async fn find(
    db: &Db,
    worktree_id: i64,
    targets: &[(String, Option<String>)],
    format: &str,
) -> Result<Vec<Record>> {
    // Split targets into "path-only" (any key under that parent) and
    // "exact (path, key)" buckets so we can build the WHERE clause
    // simply in either dialect.
    let mut path_only: Vec<String> = Vec::new();
    let mut path_key: Vec<(String, String)> = Vec::new();
    for (p, k) in targets {
        match k {
            Some(k) => path_key.push((p.clone(), k.clone())),
            None => path_only.push(p.clone()),
        }
    }

    match db {
        Db::Sqlite(pool) => {
            let mut sql = String::from(
                "SELECT r.id, r.file_path, r.path, r.key, r.commit_id, json(r.json) FROM record r \
                 JOIN file f ON f.worktree_id = r.worktree_id AND f.path = r.file_path \
                 WHERE r.worktree_id = ?1 AND f.format = ?2 AND r.deleted = 0",
            );
            if !targets.is_empty() {
                // Bind layout: ?1 = worktree_id, ?2 = format, then
                // path_only paths, then path_key flattened (path, key)*,
                // and then again for the alias subquery.
                sql.push_str(" AND (");
                let mut idx: usize = 3;
                let mut clauses: Vec<String> = Vec::new();
                if !path_only.is_empty() {
                    let placeholders: Vec<String> = (0..path_only.len())
                        .map(|i| format!("?{}", idx + i))
                        .collect();
                    clauses.push(format!("r.path IN ({})", placeholders.join(",")));
                    idx += path_only.len();
                }
                if !path_key.is_empty() {
                    let pairs: Vec<String> = (0..path_key.len())
                        .map(|i| {
                            let p = idx + i * 2;
                            let k = idx + i * 2 + 1;
                            format!("(r.path = ?{p} AND r.key = ?{k})")
                        })
                        .collect();
                    clauses.push(format!("({})", pairs.join(" OR ")));
                    idx += path_key.len() * 2;
                }
                if !path_key.is_empty() {
                    let pairs: Vec<String> = (0..path_key.len())
                        .map(|i| {
                            let p = idx + i * 2;
                            let k = idx + i * 2 + 1;
                            format!("(a.path = ?{p} AND a.key = ?{k})")
                        })
                        .collect();
                    clauses.push(format!(
                        "EXISTS (SELECT 1 FROM alias a WHERE a.record_id = r.id AND ({}))",
                        pairs.join(" OR ")
                    ));
                }
                sql.push_str(&clauses.join(" OR "));
                sql.push(')');
            }
            sql.push_str(" ORDER BY r.path, r.key");

            let mut q =
                sqlx::query_as::<_, (i64, String, String, String, Option<String>, String)>(&sql)
                    .bind(worktree_id)
                    .bind(format);
            for p in &path_only {
                q = q.bind(p);
            }
            for (p, k) in &path_key {
                q = q.bind(p).bind(k);
            }
            // alias clause repeats path_key.
            for (p, k) in &path_key {
                q = q.bind(p).bind(k);
            }
            let rows = q.fetch_all(pool).await?;

            let mut out = Vec::with_capacity(rows.len());
            for (id, file_path, path, key, commit_id, json_text) in rows {
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
                });
            }
            Ok(out)
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            if targets.is_empty() {
                let rows: Vec<(
                    i64,
                    String,
                    String,
                    String,
                    Option<String>,
                    serde_json::Value,
                )> = sqlx::query_as(
                    "SELECT r.id, r.file_path, r.path, r.key, r.commit_id, r.json \
                         FROM record r \
                         JOIN file f ON f.worktree_id = r.worktree_id AND f.path = r.file_path \
                         WHERE r.worktree_id = $1 AND f.format = $2 AND r.deleted = FALSE \
                         ORDER BY r.path, r.key",
                )
                .bind(worktree_id)
                .bind(format)
                .fetch_all(pool)
                .await?;
                return Ok(rows
                    .into_iter()
                    .map(|(id, fp, p, k, cid, json)| Record {
                        id,
                        worktree_id,
                        file_path: fp,
                        path: p,
                        key: k,
                        commit_id: cid,
                        json,
                        deleted: false,
                    })
                    .collect());
            }

            // Build the SQL with $-placeholders.
            let mut sql = String::from(
                "SELECT r.id, r.file_path, r.path, r.key, r.commit_id, r.json \
                 FROM record r \
                 JOIN file f ON f.worktree_id = r.worktree_id AND f.path = r.file_path \
                 WHERE r.worktree_id = $1 AND f.format = $2 AND r.deleted = FALSE AND (",
            );
            let mut idx: usize = 3;
            let mut clauses: Vec<String> = Vec::new();
            if !path_only.is_empty() {
                let placeholders: Vec<String> = (0..path_only.len())
                    .map(|i| format!("${}", idx + i))
                    .collect();
                clauses.push(format!("r.path IN ({})", placeholders.join(",")));
                idx += path_only.len();
            }
            if !path_key.is_empty() {
                let pairs: Vec<String> = (0..path_key.len())
                    .map(|i| {
                        let p = idx + i * 2;
                        let k = idx + i * 2 + 1;
                        format!("(r.path = ${p} AND r.key = ${k})")
                    })
                    .collect();
                clauses.push(format!("({})", pairs.join(" OR ")));
                idx += path_key.len() * 2;
            }
            if !path_key.is_empty() {
                let pairs: Vec<String> = (0..path_key.len())
                    .map(|i| {
                        let p = idx + i * 2;
                        let k = idx + i * 2 + 1;
                        format!("(a.path = ${p} AND a.key = ${k})")
                    })
                    .collect();
                clauses.push(format!(
                    "EXISTS (SELECT 1 FROM alias a WHERE a.record_id = r.id AND ({}))",
                    pairs.join(" OR ")
                ));
            }
            sql.push_str(&clauses.join(" OR "));
            sql.push_str(") ORDER BY r.path, r.key");

            let mut q = sqlx::query_as::<
                _,
                (
                    i64,
                    String,
                    String,
                    String,
                    Option<String>,
                    serde_json::Value,
                ),
            >(&sql)
            .bind(worktree_id)
            .bind(format);
            for p in &path_only {
                q = q.bind(p);
            }
            for (p, k) in &path_key {
                q = q.bind(p).bind(k);
            }
            for (p, k) in &path_key {
                q = q.bind(p).bind(k);
            }
            let rows = q.fetch_all(pool).await?;
            Ok(rows
                .into_iter()
                .map(|(id, fp, p, k, cid, json)| Record {
                    id,
                    worktree_id,
                    file_path: fp,
                    path: p,
                    key: k,
                    commit_id: cid,
                    json,
                    deleted: false,
                })
                .collect())
        }
    }
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
            let row: Option<(i64, Option<String>, String)> = sqlx::query_as(
                "SELECT id, commit_id, json(json) FROM record \
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
            let row: Option<(i64, Option<String>, serde_json::Value)> = sqlx::query_as(
                "SELECT id, commit_id, json FROM record \
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
                Some((id, commit_id, json)) => Ok(Some(Record {
                    id,
                    worktree_id,
                    file_path: file_path.to_string(),
                    path: path.to_string(),
                    key: key.to_string(),
                    commit_id,
                    json,
                    deleted: false,
                })),
                None => Ok(None),
            }
        }
    }
}

fn row_to_record(
    row: Option<(i64, Option<String>, String)>,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
) -> Result<Option<Record>> {
    match row {
        Some((id, commit_id, json_text)) => {
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
            }))
        }
        None => Ok(None),
    }
}

pub(crate) async fn get_by_id(db: &Db, id: i64) -> Result<Option<Record>> {
    match db {
        Db::Sqlite(pool) => {
            let row: Option<FullRecordRowSqlite> = sqlx::query_as(
                "SELECT id, worktree_id, file_path, path, key, commit_id, json(json), deleted \
                     FROM record WHERE id = ?1",
            )
            .bind(id)
            .fetch_optional(pool)
            .await?;
            match row {
                Some((id, wt, fp, p, k, c, t, d)) => {
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
                    }))
                }
                None => Ok(None),
            }
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let row: Option<FullRecordRowPg> = sqlx::query_as(
                "SELECT id, worktree_id, file_path, path, key, commit_id, json, deleted \
                     FROM record WHERE id = $1",
            )
            .bind(id)
            .fetch_optional(pool)
            .await?;
            match row {
                Some((id, wt, fp, p, k, c, json, d)) => Ok(Some(Record {
                    id,
                    worktree_id: wt,
                    file_path: fp,
                    path: p,
                    key: k,
                    commit_id: c,
                    json,
                    deleted: d,
                })),
                None => Ok(None),
            }
        }
    }
}
