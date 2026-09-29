// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Row-level operations on segments (docs/branch-segments.md §3, §4).
//!
//! [`Store`] is implemented once, by [`store_impl`], for each
//! backend, so its bodies are concrete and need none of the bounds a
//! function generic over [`sqlx::Database`] carries. Code built on it is
//! generic over `DB: Store` alone.
//!
//! Statements are written once, in SQLite's syntax, and
//! [`crate::db::sql!`] spells them for Postgres at compile time: `?N`
//! placeholders, `jsonb(?N)` for JSON going in, and `json(col)` for JSON
//! coming out. Everything else is written to
//! mean the same on both: a bind that can be NULL is cast, booleans are
//! bound rather than written as `0`/`1`.

use std::future::Future;

use crate::db;
use crate::error::{Error, Result};
use crate::model::ConflictState;

/// One version of one record (§3.4).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Row {
    pub(crate) id: i64,
    pub(crate) key_id: i64,
    pub(crate) segment_id: i64,
    pub(crate) file_path: String,
    pub(crate) path: String,
    pub(crate) key: String,
    pub(crate) commit_id: Option<String>,
    pub(crate) json: serde_json::Value,
    pub(crate) deleted: bool,
    pub(crate) version: i64,
    pub(crate) base_commit_id: Option<String>,
    /// The content of the version `base_commit_id` names.
    pub(crate) base_json: Option<serde_json::Value>,
    pub(crate) settled: Vec<i64>,
    pub(crate) conflict: Option<ConflictState>,
}

impl Row {
    pub(crate) fn at(&self) -> At<'_> {
        At {
            file_path: &self.file_path,
            path: &self.path,
            key: &self.key,
        }
    }

    /// A client's edit, rather than a value taken in from the file.
    pub(crate) fn is_edit(&self) -> bool {
        self.commit_id.is_none()
    }
}

/// A record's place: file, section and key.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) struct At<'a> {
    pub(crate) file_path: &'a str,
    pub(crate) path: &'a str,
    pub(crate) key: &'a str,
}

/// An entry a segment holds: the row it's on, and the edit that made it.
#[derive(Debug, Clone)]
pub(crate) struct Entry {
    pub(crate) row: i64,
    pub(crate) file_path: String,
    pub(crate) path: String,
    pub(crate) key: String,
    /// The row's own record, and whether it's a tombstone.
    pub(crate) row_key_id: i64,
    pub(crate) deleted: bool,
    pub(crate) key_id: i64,
}

impl Entry {
    pub(crate) fn at(&self) -> At<'_> {
        At {
            file_path: &self.file_path,
            path: &self.path,
            key: &self.key,
        }
    }
}

/// A worktree's open head segment and its draft.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Segs {
    pub(crate) head: i64,
    pub(crate) draft: i64,
}

/// Which of a worktree's segments a read looks through.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Scope {
    /// Its committed chain and its draft.
    Own,
    /// Its committed chain alone.
    Chain,
}

/// Which rows of a view a read returns.
#[derive(Debug, Clone, Copy)]
pub(crate) enum Filter<'a> {
    At(At<'a>),
    /// A section and key, in any file.
    PathKey(&'a str, &'a str),
    File(&'a str),
    KeyId(i64),
    All,
}

/// A row to insert. `key_id: None` starts a new record, whose id is the
/// row's own.
pub(crate) struct NewRow<'a> {
    pub(crate) at: At<'a>,
    pub(crate) key_id: Option<i64>,
    pub(crate) commit_id: Option<&'a str>,
    pub(crate) json: &'a serde_json::Value,
    pub(crate) deleted: bool,
    pub(crate) version: i64,
    pub(crate) base_commit_id: Option<&'a str>,
    pub(crate) base_json: Option<&'a serde_json::Value>,
    pub(crate) settled: &'a [i64],
    pub(crate) conflict: Option<ConflictState>,
}

/// A file row to record. `Some` on an optional field sets it; the
/// working-tree side (`commit_id`, `source_oid`) and the committed side
/// (`committed_oid`) are set by different passes of a scan.
pub(crate) struct FileRow<'a> {
    pub(crate) path: &'a str,
    pub(crate) format: &'a str,
    pub(crate) commit_id: Option<Option<&'a str>>,
    pub(crate) source_oid: Option<Option<&'a str>>,
    pub(crate) committed_oid: Option<Option<&'a str>>,
}

/// The view of worktree `?1`, as the CTE `v`.
fn view_cte(scope: Scope) -> &'static str {
    match scope {
        Scope::Own => {
            "WITH v AS (SELECT segment_id FROM worktree_segment WHERE worktree_id = ?1 \
             UNION ALL SELECT draft_segment_id FROM worktree WHERE id = ?1) "
        }
        Scope::Chain => {
            "WITH v AS (SELECT segment_id FROM worktree_segment WHERE worktree_id = ?1) "
        }
    }
}

const ROW_COLUMNS: &str = "r.id, r.key_id, r.segment_id, r.file_path, r.path, r.key, \
     r.commit_id, json(r.json), r.deleted, r.version, r.base_commit_id, json(r.settled), \
     r.conflict, json(r.base_json)";

/// A row as [`ROW_COLUMNS`] selects it.
type RowTuple = (
    i64,
    i64,
    i64,
    String,
    String,
    String,
    Option<String>,
    String,
    bool,
    i64,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
);

fn to_row(t: RowTuple) -> Result<Row> {
    let (id, key_id, segment_id, file_path, path, key, commit_id, json, deleted, version) =
        (t.0, t.1, t.2, t.3, t.4, t.5, t.6, t.7, t.8, t.9);
    let json = serde_json::from_str(&json).map_err(|e| Error::Json {
        path: path.clone(),
        source: e,
    })?;
    let settled = match t.11.as_deref() {
        Some(s) => serde_json::from_str(s).map_err(|e| Error::Json {
            path: path.clone(),
            source: e,
        })?,
        None => Vec::new(),
    };
    let base_json = match t.13.as_deref() {
        Some(s) => Some(serde_json::from_str(s).map_err(|e| Error::Json {
            path: path.clone(),
            source: e,
        })?),
        None => None,
    };
    Ok(Row {
        id,
        key_id,
        segment_id,
        file_path,
        path,
        key,
        commit_id,
        json,
        deleted,
        version,
        base_commit_id: t.10,
        base_json,
        settled,
        conflict: ConflictState::from_column(t.12.as_deref()),
    })
}

fn sort_rows(rows: &mut [Row]) {
    rows.sort_by(|a, b| (a.at(), a.id).cmp(&(b.at(), b.id)));
}

/// The store's row-level statements, implemented for each backend. Their futures are
/// `Send`, as the server's handlers need.
pub(crate) trait Store: sqlx::Database + Sized {
    /// Whether this is Postgres, whose statements [`crate::db::sql!`]
    /// spells.
    const POSTGRES: bool;

    fn segs(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
    ) -> impl Future<Output = Result<Segs>> + Send;

    /// The rows `worktree_id`'s view shows: visible in `scope`, not
    /// conflict rows, sorted by place.
    fn visible(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        scope: Scope,
        filter: Filter<'_>,
    ) -> impl Future<Output = Result<Vec<Row>>> + Send;

    /// Segment `seg`'s own rows: its conflict rows with `conflicts`,
    /// otherwise the rest.
    fn rows_in(
        tx: &mut sqlx::Transaction<'_, Self>,
        seg: i64,
        file_path: Option<&str>,
        conflicts: bool,
    ) -> impl Future<Output = Result<Vec<Row>>> + Send;

    /// Insert a row; returns its id and `key_id`.
    fn insert(
        tx: &mut sqlx::Transaction<'_, Self>,
        seg: i64,
        row: NewRow<'_>,
    ) -> impl Future<Output = Result<(i64, i64)>> + Send;

    /// Delete a row, with the entries on it and its aliases.
    fn delete_row(
        tx: &mut sqlx::Transaction<'_, Self>,
        id: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// `superseded(row, seg)`, made by edit `key_id`.
    fn entry(
        tx: &mut sqlx::Transaction<'_, Self>,
        row: i64,
        seg: i64,
        key_id: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Every entry `seg` holds: (row, row's place, key_id).
    fn entries_in(
        tx: &mut sqlx::Transaction<'_, Self>,
        seg: i64,
    ) -> impl Future<Output = Result<Vec<Entry>>> + Send;

    /// Drop one entry.
    fn untag(
        tx: &mut sqlx::Transaction<'_, Self>,
        row: i64,
        seg: i64,
        key_id: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Move a row into `seg`, stamped with the commit that wrote it, and
    /// clear what only a draft row carries.
    fn stamp(
        tx: &mut sqlx::Transaction<'_, Self>,
        row: i64,
        seg: i64,
        commit_id: &str,
    ) -> impl Future<Output = Result<()>> + Send;

    fn set_conflict_state(
        tx: &mut sqlx::Transaction<'_, Self>,
        row: i64,
        state: ConflictState,
        version: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Draw `count` consecutive versions from the family's counter.
    fn next_version(
        tx: &mut sqlx::Transaction<'_, Self>,
        family_id: i64,
        count: i64,
    ) -> impl Future<Output = Result<i64>> + Send;

    /// Give a draft row another record's identity.
    fn set_key_id(
        tx: &mut sqlx::Transaction<'_, Self>,
        row: i64,
        key_id: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Move a draft row to another place, keeping its id and its entries.
    fn relocate(
        tx: &mut sqlx::Transaction<'_, Self>,
        row: i64,
        to: At<'_>,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Whether a draft other than `worktree_id`'s holds a row at `at`.
    fn held_elsewhere(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        at: At<'_>,
    ) -> impl Future<Output = Result<bool>> + Send;

    /// Point `worktree_id`'s head at `commit`: its segment's and its own.
    fn set_head_commit(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        commit: &str,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Record what the committed segments and the working tree hold of a
    /// file: its format, its last commit (NULL while dirty), and the blobs
    /// last taken in from each side.
    fn upsert_file(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        file: &FileRow<'_>,
    ) -> impl Future<Output = Result<()>> + Send;

    /// `worktree_id`'s file rows, or the one at `path`.
    fn files(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        path: Option<&str>,
    ) -> impl Future<Output = Result<Vec<crate::model::File>>> + Send;

    /// The blob the working tree's copy of a file now holds.
    fn set_source_oid(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        path: &str,
        oid: &str,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Create the worktree `(origin, branch)`: its own family, with an
    /// empty head, which is its whole chain, and an empty draft.
    fn create_worktree(
        tx: &mut sqlx::Transaction<'_, Self>,
        origin: &str,
        branch: &str,
    ) -> impl Future<Output = Result<i64>> + Send;

    /// Take family `family`'s lock (§4.14): writes draw versions under it,
    /// so a structural change doesn't interleave with one.
    fn lock_family(
        tx: &mut sqlx::Transaction<'_, Self>,
        family: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// The family of a worktree with origin `origin`, if there is one.
    fn family_of_origin(
        tx: &mut sqlx::Transaction<'_, Self>,
        origin: &str,
    ) -> impl Future<Output = Result<Option<i64>>> + Send;

    /// A segment of `family` whose state is `commit`'s (C.16), an internal
    /// one first, since it needs no closing.
    fn segment_ending_at(
        tx: &mut sqlx::Transaction<'_, Self>,
        family: i64,
        commit: &str,
    ) -> impl Future<Output = Result<Option<i64>>> + Send;

    /// C.13: create worktree `(origin, branch)` at `commit`, in `family`,
    /// on segment `base`, whose state is `commit`'s. An open head that
    /// isn't empty closes, its owner getting a new one above it; an empty
    /// one isn't needed, and its parent is the base instead.
    fn fork_at(
        tx: &mut sqlx::Transaction<'_, Self>,
        family: i64,
        base: i64,
        origin: &str,
        branch: &str,
        commit: &str,
    ) -> impl Future<Output = Result<i64>> + Send;

    /// Stamp the worktree's outstanding `txn` rows with the commit that
    /// carries their writes.
    fn stamp_txns(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        commit: &str,
    ) -> impl Future<Output = Result<()>> + Send;

    fn delete_file(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        path: &str,
    ) -> impl Future<Output = Result<()>> + Send;

    /// The commit the worktree's head is at.
    fn head_commit(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
    ) -> impl Future<Output = Result<Option<String>>> + Send;

    /// The file new records go to when a write names none.
    fn default_file_path(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
    ) -> impl Future<Output = Result<Option<String>>> + Send;

    /// The format `worktree_id` has `file_path` in, if it has the file.
    fn file_format(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        file_path: &str,
    ) -> impl Future<Output = Result<Option<String>>> + Send;

    /// Register a file the worktree hasn't scanned; an existing row is
    /// left as it is.
    fn ensure_file(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        file_path: &str,
        format: &str,
    ) -> impl Future<Output = Result<()>> + Send;

    /// A file row's commit and whether the database owes its removal.
    fn file_state(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        file_path: &str,
    ) -> impl Future<Output = Result<Option<(Option<String>, bool)>>> + Send;

    fn set_file_deleted(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        file_path: &str,
        deleted: bool,
    ) -> impl Future<Output = Result<()>> + Send;

    /// A row's aliases, replacing any it had.
    fn replace_aliases(
        tx: &mut sqlx::Transaction<'_, Self>,
        row: i64,
        aliases: &[(String, String)],
    ) -> impl Future<Output = Result<()>> + Send;

    /// The audit row of one batch write.
    fn insert_txn(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        versions: (i64, i64),
        author: Option<&str>,
        message: Option<&str>,
        created_at: &str,
    ) -> impl Future<Output = Result<()>> + Send;
}

macro_rules! store_impl {
    ($db:ty, $postgres:expr) => {
        impl Store for $db {
            const POSTGRES: bool = $postgres;

            async fn segs(tx: &mut sqlx::Transaction<'_, Self>, worktree_id: i64) -> Result<Segs> {
                let sql = sql!(tx, "SELECT head_segment_id, draft_segment_id FROM worktree WHERE id = ?1",);
                let (head, draft): (Option<i64>, Option<i64>) = sqlx::query_as(&sql)
                    .bind(worktree_id)
                    .fetch_one(&mut **tx)
                    .await?;
                match (head, draft) {
                    (Some(head), Some(draft)) => Ok(Segs { head, draft }),
                    _ => Err(Error::Other(format!(
                        "worktree {worktree_id} has no segments"
                    ))),
                }
            }

            async fn visible(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                scope: Scope,
                filter: Filter<'_>,
            ) -> Result<Vec<Row>> {
                let cond = match filter {
                    Filter::At(_) => "AND r.file_path = ?2 AND r.path = ?3 AND r.key = ?4",
                    Filter::PathKey(..) => "AND r.path = ?2 AND r.key = ?3",
                    Filter::File(_) => "AND r.file_path = ?2",
                    Filter::KeyId(_) => "AND r.key_id = ?2",
                    Filter::All => "",
                };
                let text = format!(
                    "{}SELECT {ROW_COLUMNS} FROM record r JOIN v ON v.segment_id = r.segment_id \
                     WHERE r.conflict IS NULL \
                       AND NOT EXISTS (SELECT 1 FROM superseded x \
                                       JOIN v vx ON vx.segment_id = x.segment_id \
                                       WHERE x.record_id = r.id) {cond}",
                    view_cte(scope)
                );
                let sql = db::spell(&*tx, text);
                let q = sqlx::query_as::<_, RowTuple>(&sql).bind(worktree_id);
                let q = match filter {
                    Filter::At(at) => q.bind(at.file_path).bind(at.path).bind(at.key),
                    Filter::PathKey(path, key) => q.bind(path).bind(key),
                    Filter::File(f) => q.bind(f),
                    Filter::KeyId(id) => q.bind(id),
                    Filter::All => q,
                };
                let mut rows = q
                    .fetch_all(&mut **tx)
                    .await?
                    .into_iter()
                    .map(to_row)
                    .collect::<Result<Vec<_>>>()?;
                sort_rows(&mut rows);
                Ok(rows)
            }

            async fn rows_in(
                tx: &mut sqlx::Transaction<'_, Self>,
                seg: i64,
                file_path: Option<&str>,
                conflicts: bool,
            ) -> Result<Vec<Row>> {
                let text = format!(
                    "SELECT {ROW_COLUMNS} FROM record r WHERE r.segment_id = ?1 \
                       AND (CAST(?2 AS TEXT) IS NULL OR r.file_path = ?2) \
                       AND (r.conflict IS NOT NULL) = ?3"
                );
                let sql = db::spell(&*tx, text);
                let mut rows = sqlx::query_as::<_, RowTuple>(&sql)
                    .bind(seg)
                    .bind(file_path)
                    .bind(conflicts)
                    .fetch_all(&mut **tx)
                    .await?
                    .into_iter()
                    .map(to_row)
                    .collect::<Result<Vec<_>>>()?;
                sort_rows(&mut rows);
                Ok(rows)
            }

            async fn insert(
                tx: &mut sqlx::Transaction<'_, Self>,
                seg: i64,
                row: NewRow<'_>,
            ) -> Result<(i64, i64)> {
                let json = serde_json::to_string(row.json).map_err(|e| Error::Json {
                    path: row.at.path.to_string(),
                    source: e,
                })?;
                let settled = (!row.settled.is_empty())
                    .then(|| serde_json::to_string(row.settled).expect("ids serialize"));
                let base_json = row
                    .base_json
                    .map(|b| serde_json::to_string(b).expect("a value serializes"));
                let sql = sql!(tx, "INSERT INTO record (key_id, segment_id, file_path, path, key, commit_id, \
                         json, deleted, version, base_commit_id, settled, conflict, base_json) \
                     VALUES (COALESCE(CAST(?1 AS BIGINT), 0), ?2, ?3, ?4, ?5, CAST(?6 AS TEXT), \
                         jsonb(?7), ?8, ?9, CAST(?10 AS TEXT), jsonb(?11), CAST(?12 AS TEXT), \
                         jsonb(?13)) \
                     RETURNING id",);
                let (id,): (i64,) = sqlx::query_as(&sql)
                    .bind(row.key_id)
                    .bind(seg)
                    .bind(row.at.file_path)
                    .bind(row.at.path)
                    .bind(row.at.key)
                    .bind(row.commit_id)
                    .bind(json)
                    .bind(row.deleted)
                    .bind(row.version)
                    .bind(row.base_commit_id)
                    .bind(settled)
                    .bind(row.conflict.map(|c| c.as_str()))
                    .bind(base_json)
                    .fetch_one(&mut **tx)
                    .await?;
                let key_id = match row.key_id {
                    Some(k) => k,
                    None => {
                        let sql = sql!(tx, "UPDATE record SET key_id = id WHERE id = ?1");
                        sqlx::query(&sql).bind(id).execute(&mut **tx).await?;
                        id
                    }
                };
                Ok((id, key_id))
            }

            async fn delete_row(tx: &mut sqlx::Transaction<'_, Self>, id: i64) -> Result<()> {
                let sql = sql!(tx, "DELETE FROM record WHERE id = ?1");
                sqlx::query(&sql).bind(id).execute(&mut **tx).await?;
                Ok(())
            }

            async fn entry(
                tx: &mut sqlx::Transaction<'_, Self>,
                row: i64,
                seg: i64,
                key_id: i64,
            ) -> Result<()> {
                let sql = sql!(tx, "INSERT INTO superseded (record_id, segment_id, key_id) VALUES (?1, ?2, ?3) \
                     ON CONFLICT DO NOTHING",);
                sqlx::query(&sql)
                    .bind(row)
                    .bind(seg)
                    .bind(key_id)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn entries_in(
                tx: &mut sqlx::Transaction<'_, Self>,
                seg: i64,
            ) -> Result<Vec<Entry>> {
                let sql = sql!(tx, "SELECT x.record_id, r.file_path, r.path, r.key, r.key_id, r.deleted, x.key_id \
                     FROM superseded x JOIN record r ON r.id = x.record_id \
                     WHERE x.segment_id = ?1 ORDER BY x.record_id, x.key_id",);
                let rows: Vec<(i64, String, String, String, i64, bool, i64)> =
                    sqlx::query_as(&sql).bind(seg).fetch_all(&mut **tx).await?;
                Ok(rows
                    .into_iter()
                    .map(|(row, file_path, path, key, row_key_id, deleted, key_id)| Entry {
                        row,
                        file_path,
                        path,
                        key,
                        row_key_id,
                        deleted,
                        key_id,
                    })
                    .collect())
            }

            async fn untag(
                tx: &mut sqlx::Transaction<'_, Self>,
                row: i64,
                seg: i64,
                key_id: i64,
            ) -> Result<()> {
                let sql = sql!(tx, "DELETE FROM superseded WHERE record_id = ?1 AND segment_id = ?2 AND key_id = ?3",);
                sqlx::query(&sql)
                    .bind(row)
                    .bind(seg)
                    .bind(key_id)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn stamp(
                tx: &mut sqlx::Transaction<'_, Self>,
                row: i64,
                seg: i64,
                commit_id: &str,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE record SET segment_id = ?2, commit_id = ?3, base_commit_id = NULL, \
                         base_json = NULL, settled = NULL WHERE id = ?1",);
                sqlx::query(&sql)
                    .bind(row)
                    .bind(seg)
                    .bind(commit_id)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn set_conflict_state(
                tx: &mut sqlx::Transaction<'_, Self>,
                row: i64,
                state: ConflictState,
                version: i64,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE record SET conflict = ?2, version = ?3 WHERE id = ?1");
                sqlx::query(&sql)
                    .bind(row)
                    .bind(state.as_str())
                    .bind(version)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn next_version(
                tx: &mut sqlx::Transaction<'_, Self>,
                family_id: i64,
                count: i64,
            ) -> Result<i64> {
                let sql = sql!(tx, "UPDATE version_seq SET next_version = next_version + ?2 \
                     WHERE worktree_id = ?1 RETURNING next_version - ?2",);
                let (first,): (i64,) = sqlx::query_as(&sql)
                    .bind(family_id)
                    .bind(count)
                    .fetch_one(&mut **tx)
                    .await?;
                Ok(first)
            }

            async fn set_key_id(
                tx: &mut sqlx::Transaction<'_, Self>,
                row: i64,
                key_id: i64,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE record SET key_id = ?2 WHERE id = ?1");
                sqlx::query(&sql)
                    .bind(row)
                    .bind(key_id)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn relocate(
                tx: &mut sqlx::Transaction<'_, Self>,
                row: i64,
                to: At<'_>,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE record SET file_path = ?2, path = ?3, key = ?4 WHERE id = ?1",);
                sqlx::query(&sql)
                    .bind(row)
                    .bind(to.file_path)
                    .bind(to.path)
                    .bind(to.key)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn held_elsewhere(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                at: At<'_>,
            ) -> Result<bool> {
                let sql = sql!(tx, "SELECT EXISTS (SELECT 1 FROM record r JOIN segment s ON s.id = r.segment_id \
                     WHERE s.kind = 'draft' AND s.owner_id <> ?1 AND r.conflict IS NULL \
                       AND r.file_path = ?2 AND r.path = ?3 AND r.key = ?4)",);
                let (held,): (bool,) = sqlx::query_as(&sql)
                    .bind(worktree_id)
                    .bind(at.file_path)
                    .bind(at.path)
                    .bind(at.key)
                    .fetch_one(&mut **tx)
                    .await?;
                Ok(held)
            }

            async fn set_head_commit(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                commit: &str,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE segment SET head_commit = ?2 \
                     WHERE id = (SELECT head_segment_id FROM worktree WHERE id = ?1)",);
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(commit)
                    .execute(&mut **tx)
                    .await?;
                let sql = sql!(tx, "UPDATE worktree SET commit_id = ?2 WHERE id = ?1");
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(commit)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn upsert_file(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                file: &FileRow<'_>,
            ) -> Result<()> {
                let sql = sql!(tx, "INSERT INTO file (worktree_id, path, format, commit_id, source_oid, committed_oid) \
                     VALUES (?1, ?2, ?3, CAST(?4 AS TEXT), CAST(?5 AS TEXT), CAST(?6 AS TEXT)) \
                     ON CONFLICT (worktree_id, path) DO UPDATE SET \
                       format = excluded.format, \
                       commit_id = CASE WHEN ?7 THEN excluded.commit_id ELSE file.commit_id END, \
                       source_oid = CASE WHEN ?7 THEN excluded.source_oid ELSE file.source_oid END, \
                       committed_oid = CASE WHEN ?8 THEN excluded.committed_oid \
                                            ELSE file.committed_oid END",);
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(file.path)
                    .bind(file.format)
                    .bind(file.commit_id.flatten())
                    .bind(file.source_oid.flatten())
                    .bind(file.committed_oid.flatten())
                    .bind(file.source_oid.is_some())
                    .bind(file.committed_oid.is_some())
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn files(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                path: Option<&str>,
            ) -> Result<Vec<crate::model::File>> {
                let sql = sql!(tx, "SELECT worktree_id, path, format, commit_id, source_oid, committed_oid, deleted \
                     FROM file WHERE worktree_id = ?1 AND (CAST(?2 AS TEXT) IS NULL OR path = ?2) \
                     ORDER BY path",);
                let rows: Vec<(i64, String, String, Option<String>, Option<String>, Option<String>, bool)> =
                    sqlx::query_as(&sql)
                        .bind(worktree_id)
                        .bind(path)
                        .fetch_all(&mut **tx)
                        .await?;
                Ok(rows
                    .into_iter()
                    .map(
                        |(worktree_id, path, format, commit_id, source_oid, committed_oid, deleted)| {
                            crate::model::File {
                                worktree_id,
                                path,
                                format,
                                commit_id,
                                source_oid,
                                committed_oid,
                                deleted,
                            }
                        },
                    )
                    .collect())
            }

            async fn set_source_oid(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                path: &str,
                oid: &str,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE file SET source_oid = ?3 WHERE worktree_id = ?1 AND path = ?2",);
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(path)
                    .bind(oid)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn create_worktree(
                tx: &mut sqlx::Transaction<'_, Self>,
                origin: &str,
                branch: &str,
            ) -> Result<i64> {
                let sql = sql!(tx, "INSERT INTO worktree (origin, branch) VALUES (?1, ?2) RETURNING id",);
                let (w,): (i64,) = sqlx::query_as(&sql)
                    .bind(origin)
                    .bind(branch)
                    .fetch_one(&mut **tx)
                    .await?;
                for sql in [
                    sql!(tx, "INSERT INTO version_seq (worktree_id) VALUES (?1)"),
                    sql!(tx, "UPDATE worktree SET family_id = ?1 WHERE id = ?1"),
                ] {
                    sqlx::query(sql).bind(w).execute(&mut **tx).await?;
                }
                let sql = sql!(tx, "INSERT INTO segment (family_id, kind, owner_id) VALUES (?1, ?2, ?1) RETURNING id",);
                let mut ids = Vec::new();
                for kind in ["head", "draft"] {
                    let (id,): (i64,) = sqlx::query_as(&sql)
                        .bind(w)
                        .bind(kind)
                        .fetch_one(&mut **tx)
                        .await?;
                    ids.push(id);
                }
                let sql = sql!(tx, "UPDATE worktree SET head_segment_id = ?2, draft_segment_id = ?3 WHERE id = ?1",);
                sqlx::query(&sql)
                    .bind(w)
                    .bind(ids[0])
                    .bind(ids[1])
                    .execute(&mut **tx)
                    .await?;
                let sql = sql!(tx, "INSERT INTO worktree_segment (worktree_id, segment_id) VALUES (?1, ?2)",);
                sqlx::query(&sql).bind(w).bind(ids[0]).execute(&mut **tx).await?;
                Ok(w)
            }

            async fn lock_family(tx: &mut sqlx::Transaction<'_, Self>, family: i64) -> Result<()> {
                sqlx::query(sql!(
                    tx,
                    "UPDATE version_seq SET next_version = next_version WHERE worktree_id = ?1"
                ))
                .bind(family)
                .execute(&mut **tx)
                .await?;
                Ok(())
            }

            async fn family_of_origin(
                tx: &mut sqlx::Transaction<'_, Self>,
                origin: &str,
            ) -> Result<Option<i64>> {
                let row: Option<(i64,)> = sqlx::query_as(sql!(
                    tx,
                    "SELECT COALESCE(family_id, id) FROM worktree WHERE origin = ?1 ORDER BY id LIMIT 1"
                ))
                .bind(origin)
                .fetch_optional(&mut **tx)
                .await?;
                Ok(row.map(|(f,)| f))
            }

            async fn segment_ending_at(
                tx: &mut sqlx::Transaction<'_, Self>,
                family: i64,
                commit: &str,
            ) -> Result<Option<i64>> {
                let row: Option<(i64,)> = sqlx::query_as(sql!(
                    tx,
                    "SELECT id FROM segment \
                     WHERE family_id = ?1 AND head_commit = ?2 AND kind <> 'draft' \
                     ORDER BY CASE WHEN kind = 'internal' THEN 0 ELSE 1 END, id LIMIT 1"
                ))
                .bind(family)
                .bind(commit)
                .fetch_optional(&mut **tx)
                .await?;
                Ok(row.map(|(s,)| s))
            }

            async fn fork_at(
                tx: &mut sqlx::Transaction<'_, Self>,
                family: i64,
                base: i64,
                origin: &str,
                branch: &str,
                commit: &str,
            ) -> Result<i64> {
                let (kind, parent, owner): (String, Option<i64>, Option<i64>) = sqlx::query_as(sql!(
                    tx,
                    "SELECT kind, parent_id, owner_id FROM segment WHERE id = ?1"
                ))
                .bind(base)
                .fetch_one(&mut **tx)
                .await?;
                let mut base = Some(base);
                if let ("head", Some(owner)) = (kind.as_str(), owner) {
                    // empty: no rows, and no entries hiding rows below
                    let (empty,): (bool,) = sqlx::query_as(sql!(
                        tx,
                        "SELECT NOT EXISTS (SELECT 1 FROM record WHERE segment_id = ?1) \
                            AND NOT EXISTS (SELECT 1 FROM superseded WHERE segment_id = ?1)"
                    ))
                    .bind(base)
                    .fetch_one(&mut **tx)
                    .await?;
                    if empty {
                        base = parent;
                    } else {
                        let (v,): (i64,) = sqlx::query_as(sql!(
                            tx,
                            "SELECT next_version FROM version_seq WHERE worktree_id = ?1"
                        ))
                        .bind(family)
                        .fetch_one(&mut **tx)
                        .await?;
                        sqlx::query(sql!(
                            tx,
                            "UPDATE segment SET kind = 'internal', owner_id = NULL WHERE id = ?1"
                        ))
                        .bind(base)
                        .execute(&mut **tx)
                        .await?;
                        let (head,): (i64,) = sqlx::query_as(sql!(
                            tx,
                            "INSERT INTO segment (family_id, kind, parent_id, head_commit, owner_id) \
                             VALUES (?1, 'head', ?2, ?3, ?4) RETURNING id"
                        ))
                        .bind(family)
                        .bind(base)
                        .bind(commit)
                        .bind(owner)
                        .fetch_one(&mut **tx)
                        .await?;
                        sqlx::query(sql!(tx, "UPDATE worktree SET head_segment_id = ?2 WHERE id = ?1"))
                            .bind(owner)
                            .bind(head)
                            .execute(&mut **tx)
                            .await?;
                        sqlx::query(sql!(
                            tx,
                            "INSERT INTO worktree_segment \
                             (worktree_id, segment_id, added_version, inherited) \
                             VALUES (?1, ?2, ?3, ?4)"
                        ))
                        .bind(owner)
                        .bind(head)
                        .bind(v)
                        .bind(false)
                        .execute(&mut **tx)
                        .await?;
                    }
                }
                let (w,): (i64,) = sqlx::query_as(sql!(
                    tx,
                    "INSERT INTO worktree (origin, branch, family_id, commit_id) \
                     VALUES (?1, ?2, ?3, ?4) RETURNING id"
                ))
                .bind(origin)
                .bind(branch)
                .bind(family)
                .bind(commit)
                .fetch_one(&mut **tx)
                .await?;
                let (head,): (i64,) = sqlx::query_as(sql!(
                    tx,
                    "INSERT INTO segment (family_id, kind, parent_id, head_commit, owner_id) \
                     VALUES (?1, 'head', ?2, ?3, ?4) RETURNING id"
                ))
                .bind(family)
                .bind(base)
                .bind(commit)
                .bind(w)
                .fetch_one(&mut **tx)
                .await?;
                let (draft,): (i64,) = sqlx::query_as(sql!(
                    tx,
                    "INSERT INTO segment (family_id, kind, owner_id) VALUES (?1, 'draft', ?2) RETURNING id"
                ))
                .bind(family)
                .bind(w)
                .fetch_one(&mut **tx)
                .await?;
                sqlx::query(sql!(
                    tx,
                    "UPDATE worktree SET head_segment_id = ?2, draft_segment_id = ?3 WHERE id = ?1"
                ))
                .bind(w)
                .bind(head)
                .bind(draft)
                .execute(&mut **tx)
                .await?;
                // the base and its ancestors, inherited, then its own head
                if let Some(base) = base {
                    sqlx::query(sql!(
                        tx,
                        "WITH RECURSIVE up(id) AS ( \
                             SELECT CAST(?2 AS BIGINT) \
                             UNION ALL \
                             SELECT s.parent_id FROM segment s JOIN up ON s.id = up.id \
                             WHERE s.parent_id IS NOT NULL) \
                         INSERT INTO worktree_segment (worktree_id, segment_id, added_version, inherited) \
                         SELECT ?1, id, 0, ?3 FROM up"
                    ))
                    .bind(w)
                    .bind(base)
                    .bind(true)
                    .execute(&mut **tx)
                    .await?;
                }
                sqlx::query(sql!(
                    tx,
                    "INSERT INTO worktree_segment (worktree_id, segment_id, added_version, inherited) \
                     VALUES (?1, ?2, 0, ?3)"
                ))
                .bind(w)
                .bind(head)
                .bind(false)
                .execute(&mut **tx)
                .await?;
                Ok(w)
            }

            async fn stamp_txns(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                commit: &str,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE txn SET commit_id = ?2 WHERE worktree_id = ?1 AND commit_id IS NULL",);
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(commit)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn delete_file(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                path: &str,
            ) -> Result<()> {
                let sql = sql!(tx, "DELETE FROM file WHERE worktree_id = ?1 AND path = ?2");
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(path)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn head_commit(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
            ) -> Result<Option<String>> {
                let sql = sql!(tx, "SELECT commit_id FROM worktree WHERE id = ?1");
                let (commit,): (Option<String>,) = sqlx::query_as(&sql)
                    .bind(worktree_id)
                    .fetch_one(&mut **tx)
                    .await?;
                Ok(commit)
            }

            async fn default_file_path(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
            ) -> Result<Option<String>> {
                let sql = sql!(tx, "SELECT default_file_path FROM worktree WHERE id = ?1");
                let (path,): (Option<String>,) = sqlx::query_as(&sql)
                    .bind(worktree_id)
                    .fetch_one(&mut **tx)
                    .await?;
                Ok(path)
            }

            async fn file_format(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                file_path: &str,
            ) -> Result<Option<String>> {
                let sql = sql!(tx, "SELECT format FROM file WHERE worktree_id = ?1 AND path = ?2");
                let row: Option<(String,)> = sqlx::query_as(&sql)
                    .bind(worktree_id)
                    .bind(file_path)
                    .fetch_optional(&mut **tx)
                    .await?;
                Ok(row.map(|(f,)| f))
            }

            async fn ensure_file(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                file_path: &str,
                format: &str,
            ) -> Result<()> {
                let sql = sql!(tx, "INSERT INTO file (worktree_id, path, format) VALUES (?1, ?2, ?3) \
                     ON CONFLICT DO NOTHING",);
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(file_path)
                    .bind(format)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn file_state(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                file_path: &str,
            ) -> Result<Option<(Option<String>, bool)>> {
                let sql = sql!(tx, "SELECT commit_id, deleted FROM file WHERE worktree_id = ?1 AND path = ?2",);
                Ok(sqlx::query_as(&sql)
                    .bind(worktree_id)
                    .bind(file_path)
                    .fetch_optional(&mut **tx)
                    .await?)
            }

            async fn set_file_deleted(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                file_path: &str,
                deleted: bool,
            ) -> Result<()> {
                let sql =
                    sql!(tx, "UPDATE file SET deleted = ?3 WHERE worktree_id = ?1 AND path = ?2");
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(file_path)
                    .bind(deleted)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn replace_aliases(
                tx: &mut sqlx::Transaction<'_, Self>,
                row: i64,
                aliases: &[(String, String)],
            ) -> Result<()> {
                let sql = sql!(tx, "DELETE FROM alias WHERE record_id = ?1");
                sqlx::query(&sql).bind(row).execute(&mut **tx).await?;
                let sql = sql!(tx, "INSERT INTO alias (record_id, path, key) VALUES (?1, ?2, ?3) \
                     ON CONFLICT DO NOTHING",);
                for (path, key) in aliases {
                    sqlx::query(&sql)
                        .bind(row)
                        .bind(path)
                        .bind(key)
                        .execute(&mut **tx)
                        .await?;
                }
                Ok(())
            }

            async fn insert_txn(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                versions: (i64, i64),
                author: Option<&str>,
                message: Option<&str>,
                created_at: &str,
            ) -> Result<()> {
                let sql = sql!(tx, "INSERT INTO txn (worktree_id, first_version, last_version, author, message, \
                         created_at) \
                     VALUES (?1, ?2, ?3, CAST(?4 AS TEXT), CAST(?5 AS TEXT), ?6)",);
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(versions.0)
                    .bind(versions.1)
                    .bind(author)
                    .bind(message)
                    .bind(created_at)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }
        }
    };
}

store_impl!(sqlx::Sqlite, false);
#[cfg(feature = "postgres")]
store_impl!(sqlx::Postgres, true);
