// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Transaction-scoped helpers — Postgres dialect.

#![cfg(feature = "postgres")]

use crate::db::tx::RecordLookup;
use crate::error::Result;

type LookupRow = (Option<i64>, Option<String>, Option<bool>, Option<String>);

pub(crate) async fn ensure_file_row(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    worktree_id: i64,
    file_path: &str,
) -> Result<()> {
    sqlx::query(
        "INSERT INTO file (worktree_id, path, format, commit_id) \
         VALUES ($1, $2, 'unknown', NULL) \
         ON CONFLICT(worktree_id, path) DO NOTHING",
    )
    .bind(worktree_id)
    .bind(file_path)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

pub(crate) async fn lookup_commits(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
) -> Result<RecordLookup> {
    let row: Option<LookupRow> = sqlx::query_as(
        "SELECT r.id, r.commit_id, r.deleted, f.commit_id FROM file f \
         LEFT JOIN record r ON r.worktree_id = f.worktree_id \
                           AND r.file_path = f.path \
                           AND r.path = $3 \
                           AND r.key = $4 \
         WHERE f.worktree_id = $1 AND f.path = $2",
    )
    .bind(worktree_id)
    .bind(file_path)
    .bind(path)
    .bind(key)
    .fetch_optional(&mut **tx)
    .await?;
    let (raw_id, rec_commit, deleted, file_commit) = row.unwrap_or((None, None, None, None));
    let is_tombstone = matches!(deleted, Some(true));
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

pub(crate) async fn file_format(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    worktree_id: i64,
    file_path: &str,
) -> Result<Option<String>> {
    let row: Option<(String,)> =
        sqlx::query_as("SELECT format FROM file WHERE worktree_id = $1 AND path = $2")
            .bind(worktree_id)
            .bind(file_path)
            .fetch_optional(&mut **tx)
            .await?;
    Ok(row.map(|(f,)| f))
}

pub(crate) async fn replace_aliases(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    record_id: i64,
    aliases: &[(String, String)],
) -> Result<()> {
    sqlx::query("DELETE FROM alias WHERE record_id = $1")
        .bind(record_id)
        .execute(&mut **tx)
        .await?;
    for (p, k) in aliases {
        sqlx::query(
            "INSERT INTO alias (record_id, path, key) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
        )
        .bind(record_id)
        .bind(p)
        .bind(k)
        .execute(&mut **tx)
        .await?;
    }
    Ok(())
}

pub(crate) async fn create_record(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
    json_text: &str,
) -> Result<i64> {
    let row: (i64,) = sqlx::query_as(
        "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, deleted) \
         VALUES ($1, $2, $3, $4, $5::jsonb, NULL, FALSE) \
         ON CONFLICT(worktree_id, file_path, path, key) DO UPDATE SET \
           json = EXCLUDED.json, commit_id = NULL, deleted = FALSE \
         RETURNING id",
    )
    .bind(worktree_id)
    .bind(file_path)
    .bind(path)
    .bind(key)
    .bind(json_text)
    .fetch_one(&mut **tx)
    .await?;
    Ok(row.0)
}

pub(crate) async fn update_record(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    id: i64,
    json_text: &str,
) -> Result<()> {
    sqlx::query(
        "UPDATE record SET json = $1::jsonb, commit_id = NULL, deleted = FALSE \
         WHERE id = $2",
    )
    .bind(json_text)
    .bind(id)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

pub(crate) async fn upsert_record(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
    json_text: &str,
) -> Result<i64> {
    let row: (i64,) = sqlx::query_as(
        "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, deleted) \
         VALUES ($1, $2, $3, $4, $5::jsonb, NULL, FALSE) \
         ON CONFLICT(worktree_id, file_path, path, key) DO UPDATE SET \
           json = EXCLUDED.json, commit_id = NULL, deleted = FALSE \
         RETURNING id",
    )
    .bind(worktree_id)
    .bind(file_path)
    .bind(path)
    .bind(key)
    .bind(json_text)
    .fetch_one(&mut **tx)
    .await?;
    Ok(row.0)
}

pub(crate) async fn delete_record(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    id: i64,
) -> Result<()> {
    sqlx::query("UPDATE record SET deleted = TRUE, commit_id = NULL WHERE id = $1")
        .bind(id)
        .execute(&mut **tx)
        .await?;
    sqlx::query("DELETE FROM alias WHERE record_id = $1")
        .bind(id)
        .execute(&mut **tx)
        .await?;
    Ok(())
}
