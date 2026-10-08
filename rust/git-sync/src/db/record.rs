// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `record` table queries that each run in their own transaction and
//! return [`crate::model`] types.

use std::collections::BTreeSet;

use crate::db::store::{Filter, Scope, Store};
use crate::db::tables::{At, RecordRow};
use crate::db::Db;
use crate::error::{Error, Result};
use crate::model::{
    ConflictState, FacetColumnRow, FacetPath, FacetRows, FacetSpec, QueryOp, Record, RecordQuery,
    WorktreeFilter,
};

impl RecordRow {
    /// The row as the API reports it: the record's `key_id` is its id.
    pub(crate) fn into_record(self, worktree_id: i64) -> Record {
        Record {
            id: self.key_id,
            worktree_id,
            file_path: self.file_path,
            path: self.path,
            key: self.key,
            commit_id: self.commit_id,
            json: self.json,
            deleted: self.deleted,
            version: self.version,
            conflict: self.conflict,
        }
    }
}

/// A record as the find queries select it: `key_id AS id`, the worktree
/// it was read through, and its JSON as text. The follow queries select
/// neither `deleted` nor `conflict`: they return live, settled records.
#[derive(sqlx::FromRow)]
struct FoundColumns {
    id: i64,
    worktree_id: i64,
    file_path: String,
    path: String,
    key: String,
    commit_id: Option<String>,
    json: String,
    version: i64,
    #[sqlx(default)]
    deleted: bool,
    #[sqlx(default)]
    conflict: Option<String>,
}

impl TryFrom<FoundColumns> for Record {
    type Error = Error;

    fn try_from(c: FoundColumns) -> Result<Self> {
        Ok(Record {
            json: serde_json::from_str(&c.json).map_err(|source| Error::Json {
                path: c.path.clone(),
                source,
            })?,
            conflict: ConflictState::from_column(c.conflict.as_deref()),
            id: c.id,
            worktree_id: c.worktree_id,
            file_path: c.file_path,
            path: c.path,
            key: c.key,
            commit_id: c.commit_id,
            deleted: c.deleted,
            version: c.version,
        })
    }
}

/// Run `f` over the worktree's segments in a read transaction.
async fn read<DB: Store, T>(
    pool: &sqlx::Pool<DB>,
    f: impl AsyncFnOnce(&mut sqlx::Transaction<'_, DB>) -> Result<T>,
) -> Result<T> {
    let mut tx = pool.begin().await?;
    let out = f(&mut tx).await?;
    tx.commit().await?;
    Ok(out)
}

/// The records a commit of `files` changes, in version order, with their
/// ids: what its rollup names. `watermark` is the family's next version
/// before the save; rows from there on stay in the draft.
pub(crate) async fn committed_records(
    db: &Db,
    worktree_id: i64,
    files: &std::collections::BTreeSet<String>,
    watermark: i64,
) -> Result<Vec<crate::model::TxnRecord>> {
    on_pool!(db, pool => read(pool, async |tx| {
        let mut rows = crate::segments::changed(tx, worktree_id, files, watermark).await?;
        rows.sort_by_key(|r| r.version);
        Ok(rows
            .into_iter()
            .map(|r| crate::model::TxnRecord {
                path: r.path,
                key: r.key,
                version: r.version,
                deleted: r.deleted,
                key_id: Some(r.key_id),
                file_path: Some(r.file_path),
            })
            .collect())
    }).await)
}

/// Files a commit has something to carry for: a pending edit -- an
/// update, create or delete, which `save_changes` still has to write --
/// or a file row whose own commit_id is NULL (a hand-edited file the
/// scan took in, whose bytes are right but not yet committed) or which
/// the database owes a removal.
pub(crate) async fn list_dirty_files(db: &Db, worktree_id: i64) -> Result<Vec<String>> {
    on_pool!(db, pool => read(pool, async |tx| {
        let d = Store::segs(tx, worktree_id).await?.draft;
        let mut files: BTreeSet<String> = Store::rows_in(tx, d, None, false)
            .await?
            .into_iter()
            .filter(RecordRow::is_edit)
            .map(|r| r.file_path)
            .collect();
        files.extend(
            Store::files(tx, worktree_id, None)
                .await?
                .into_iter()
                .filter(|f| f.commit_id.is_none() || f.deleted)
                .map(|f| f.path),
        );
        Ok(files.into_iter().collect())
    }).await)
}

/// What a render of a file reads from the database: all of it from one
/// snapshot, so its parts describe the same moment.
pub(crate) struct RenderInputs {
    /// The file row, if there is one.
    pub(crate) file: Option<crate::model::File>,
    /// Its `write_seq`: what the write checks hasn't moved since.
    pub(crate) write_seq: Option<i64>,
    /// A deleted file still has a live record in it. Always `false` for a
    /// file that isn't deleted, where nothing asks.
    pub(crate) live: bool,
    /// The file's side of each record in conflict, by `(path, key)`.
    pub(crate) conflicts: std::collections::HashMap<(String, String), Record>,
    /// The pending edits -- the draft's rows a client wrote, tombstones
    /// included -- in the order they were written.
    pub(crate) pending: Vec<Record>,
    /// Each pending edit's base commit, by `(path, key)`.
    pub(crate) bases: std::collections::HashMap<(String, String), Option<String>>,
    /// Each pending edit's base content, by `(path, key)`: what a stale
    /// file is checked against.
    pub(crate) base_values: std::collections::HashMap<(String, String), Option<serde_json::Value>>,
}

/// Everything a render of `file_path` reads from the database, in one
/// read-only snapshot.
pub(crate) async fn render_inputs(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
) -> Result<RenderInputs> {
    render_inputs_with(db, worktree_id, file_path, async || Ok(())).await
}

/// [`render_inputs`], running `mid` once the snapshot has begun and before
/// the rest is read: where another writer would commit.
#[cfg(test)]
pub(crate) async fn render_inputs_racing(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
    mid: impl AsyncFnMut() -> Result<()>,
) -> Result<RenderInputs> {
    render_inputs_with(db, worktree_id, file_path, mid).await
}

async fn render_inputs_with(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
    mut mid: impl AsyncFnMut() -> Result<()>,
) -> Result<RenderInputs> {
    on_pool!(db, pool => read(pool, async |tx| {
        Store::begin_snapshot(tx).await?;
        let file = Store::files(tx, worktree_id, Some(file_path)).await?.pop();
        mid().await?;
        let write_seq = Store::write_seq(tx, worktree_id, file_path).await?;
        let d = Store::segs(tx, worktree_id).await?.draft;
        let conflicts = Store::rows_in(tx, d, Some(file_path), true)
            .await?
            .into_iter()
            .map(|r| ((r.path.clone(), r.key.clone()), r.into_record(worktree_id)))
            .collect();
        let live = match &file {
            Some(f) if f.deleted => Store::visible(tx, worktree_id, Scope::Own, Filter::File(file_path))
                .await?
                .iter()
                .any(|r| !r.deleted),
            _ => false,
        };
        let mut edits: Vec<RecordRow> = Store::rows_in(tx, d, Some(file_path), false)
            .await?
            .into_iter()
            .filter(RecordRow::is_edit)
            .collect();
        edits.sort_by_key(|r| r.id);
        let at = |r: &RecordRow| (r.path.clone(), r.key.clone());
        let bases = edits.iter().map(|r| (at(r), r.base_commit_id.clone())).collect();
        let base_values = edits.iter().map(|r| (at(r), r.base_json.clone())).collect();
        let pending = edits.into_iter().map(|r| r.into_record(worktree_id)).collect();
        Ok(RenderInputs {
            file,
            write_seq,
            live,
            conflicts,
            pending,
            bases,
            base_values,
        })
    }).await)
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
/// scans with `json_each`. With `subtypes`, the names are first expanded
/// by [`subtypes`] and bound as a constant list: computed inside the
/// statement, the planner can't estimate the list's size.
pub(crate) async fn find(db: &Db, worktree_id: i64, query: &RecordQuery) -> Result<Vec<Record>> {
    check_reset(db, worktree_id, query).await?;
    let expanded = with_subtypes(db, worktree_id, query).await?;
    let query = expanded.as_ref().unwrap_or(query);
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

/// The worktrees a read covers, bound as parameters 1-3 of
/// [`views`]: the handle's own
/// worktree, or every one `filter` matches.
struct WorktreeScope {
    id: Option<i64>,
    origin: Option<String>,
    branch: Option<String>,
}

impl WorktreeScope {
    fn new(worktree_id: i64, filter: Option<&WorktreeFilter>) -> Self {
        match filter {
            None => Self {
                id: Some(worktree_id),
                origin: None,
                branch: None,
            },
            Some(f) => {
                let (origin, branch) = f.normalized();
                Self {
                    id: None,
                    origin,
                    branch,
                }
            }
        }
    }
}

/// The highest `reset_version` of the worktrees in `scope`: a layered
/// view resets when any of its worktrees does (§4.10).
async fn reset_version(db: &Db, scope: &WorktreeScope) -> Result<i64> {
    const SQL: &str = "SELECT COALESCE(MAX(reset_version), 0), \
         COUNT(DISTINCT COALESCE(w.family_id, w.id)) FROM worktree w \
         WHERE (CAST(?1 AS BIGINT) IS NULL OR w.id = ?1) \
           AND (CAST(?2 AS TEXT) IS NULL OR w.origin = ?2) \
           AND (CAST(?3 AS TEXT) IS NULL OR w.branch = ?3)";
    let (reset_version, families): (i64, i64) = on_pool!(db, pool => {
        sqlx::query_as(sql!(pool, SQL))
            .bind(scope.id)
            .bind(scope.origin.as_deref())
            .bind(scope.branch.as_deref())
            .fetch_one(pool)
            .await?
    });
    one_family(families)?;
    Ok(reset_version)
}

/// Versions are drawn per family, so a cursor means nothing across two.
fn one_family(families: i64) -> Result<()> {
    if families > 1 {
        return Err(Error::CursorAcrossFamilies { families });
    }
    Ok(())
}

/// The highest version drawn so far by the families of the worktrees in
/// the scope: a cursor that sees every row written after it.
pub(crate) async fn watermark(
    db: &Db,
    worktree_id: i64,
    filter: Option<&WorktreeFilter>,
) -> Result<i64> {
    const SQL: &str = "SELECT COALESCE(MAX(q.next_version), 1) - 1, \
         COUNT(DISTINCT q.worktree_id) FROM worktree w \
         JOIN version_seq q ON q.worktree_id = COALESCE(w.family_id, w.id) \
         WHERE (CAST(?1 AS BIGINT) IS NULL OR w.id = ?1) \
           AND (CAST(?2 AS TEXT) IS NULL OR w.origin = ?2) \
           AND (CAST(?3 AS TEXT) IS NULL OR w.branch = ?3)";
    let scope = WorktreeScope::new(worktree_id, filter);
    let (watermark, families): (i64, i64) = on_pool!(db, pool => {
        sqlx::query_as(sql!(pool, SQL))
            .bind(scope.id)
            .bind(scope.origin.as_deref())
            .bind(scope.branch.as_deref())
            .fetch_one(pool)
            .await?
    });
    one_family(families)?;
    Ok(watermark)
}

/// [`Error::Reset`] when `query.since_version` is below its view's
/// `reset_version`.
async fn check_reset(db: &Db, worktree_id: i64, query: &RecordQuery) -> Result<()> {
    match query.since_version {
        Some(since) => {
            let scope = WorktreeScope::new(worktree_id, query.worktrees.as_ref());
            check_since(db, &scope, since).await
        }
        None => Ok(()),
    }
}

async fn check_since(db: &Db, scope: &WorktreeScope, since: i64) -> Result<()> {
    let reset_version = reset_version(db, scope).await?;
    if since < reset_version {
        return Err(Error::Reset {
            since,
            reset_version,
        });
    }
    Ok(())
}

/// The views of the worktrees in the [`WorktreeScope`] bound as
/// parameters 1-3, as `(worktree_id, segment_id)`: each one's committed
/// chain and its draft. Worktrees are few, so the `IS NULL OR` tests only
/// ever scan that table. Postgres can't infer a parameter's type from
/// `IS NULL`, hence the casts.
fn views(pg: bool) -> String {
    let (id, origin, branch) = if pg {
        ("$1::bigint", "$2::text", "$3::text")
    } else {
        ("?1", "?2", "?3")
    };
    let scope = format!(
        "({id} IS NULL OR w.id = {id}) AND ({origin} IS NULL OR w.origin = {origin}) \
         AND ({branch} IS NULL OR w.branch = {branch})"
    );
    format!(
        "(SELECT ws.worktree_id, ws.segment_id FROM worktree_segment ws \
         JOIN worktree w ON w.id = ws.worktree_id WHERE {scope} \
         UNION ALL SELECT w.id, w.draft_segment_id FROM worktree w WHERE {scope})"
    )
}

/// Joined after `FROM record r`: a row for each worktree in scope whose
/// view holds `r`'s segment, as `v`.
fn view_join(pg: bool) -> String {
    format!(" JOIN {} v ON v.segment_id = r.segment_id", views(pg))
}

/// `r` is visible in worktree `v`: nothing in its view supersedes it
/// (§3.3). The worktree match is in the `WHERE`, so the planner makes it
/// a hash anti-join.
fn visible(pg: bool) -> String {
    format!(
        "NOT EXISTS (SELECT 1 FROM superseded x JOIN {} vx ON vx.segment_id = x.segment_id \
         WHERE x.record_id = r.id AND vx.worktree_id = v.worktree_id)",
        views(pg)
    )
}

/// `query` with its `type_names` expanded by [`subtypes`], or `None` when
/// it doesn't ask for that.
async fn with_subtypes(
    db: &Db,
    worktree_id: i64,
    query: &RecordQuery,
) -> Result<Option<RecordQuery>> {
    if !query.subtypes {
        return Ok(None);
    }
    let Some(names) = query.effective_type_names() else {
        return Ok(None);
    };
    let type_names = subtypes(db, worktree_id, query, names).await?;
    Ok(Some(RecordQuery {
        type_names: Some(type_names),
        subtypes: false,
        ..query.clone()
    }))
}

/// `names` plus every type whose `/types` record's `extends` list reaches
/// one of them, transitively, among the `/types` records in `query`'s
/// worktrees and file.
async fn subtypes(
    db: &Db,
    worktree_id: i64,
    query: &RecordQuery,
    names: &[String],
) -> Result<Vec<String>> {
    let scope = WorktreeScope::new(worktree_id, query.worktrees.as_ref());
    match db {
        Db::Sqlite(pool) => {
            let sql = format!(
                "WITH RECURSIVE {}, sub(name) AS (SELECT value FROM json_each(?5) \
                 UNION SELECT e.child FROM edges e JOIN sub s ON e.parent = s.name) \
                 SELECT name FROM sub ORDER BY name",
                type_edges_cte_sqlite(4)
            );
            let names = serde_json::to_string(names).map_err(|e| Error::Other(e.to_string()))?;
            Ok(sqlx::query_scalar(&sql)
                .bind(scope.id)
                .bind(scope.origin)
                .bind(scope.branch)
                .bind(query.file_path.as_deref())
                .bind(names)
                .fetch_all(pool)
                .await?)
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let sql = format!(
                "WITH RECURSIVE {}, sub(name) AS (SELECT unnest($5::text[]) \
                 UNION SELECT e.child FROM edges e JOIN sub s ON e.parent = s.name) \
                 SELECT name FROM sub ORDER BY name",
                type_edges_cte_pg(4)
            );
            // planned per call, as in `find_pg`
            Ok(sqlx::query_scalar(&sql)
                .persistent(false)
                .bind(scope.id)
                .bind(scope.origin)
                .bind(scope.branch)
                .bind(query.file_path.as_deref())
                .bind(names)
                .fetch_all(pool)
                .await?)
        }
    }
}

/// The CTE `edges(child, parent)`: one row per name in a `/types`
/// record's `extends` list, over the records in the
/// [`WorktreeScope`] bound as `?1`-`?3` and the file bound as
/// `?{file_idx}` (NULL for every file).
fn type_edges_cte_sqlite(file_idx: usize) -> String {
    // `typeof(e.key) = 'integer'` keeps array elements only: an object's
    // members have text keys and a scalar's key is NULL.
    format!(
        "edges(child, parent) AS (SELECT r.key, e.value \
         FROM record r{}, json_each(r.json, '$.extends') e \
         WHERE {} AND r.path = '/types' AND r.deleted = 0 \
         AND r.conflict IS NULL AND (?{file_idx} IS NULL OR r.file_path = ?{file_idx}) \
         AND typeof(e.key) = 'integer' AND e.type = 'text')",
        view_join(false),
        visible(false)
    )
}

/// Postgres twin of [`type_edges_cte_sqlite`].
#[cfg(feature = "postgres")]
fn type_edges_cte_pg(file_idx: usize) -> String {
    // `jsonb_array_elements` raises on a non-array, hence the CASE.
    format!(
        "edges(child, parent) AS (SELECT r.key, e.value #>> '{{}}' \
         FROM record r{} CROSS JOIN LATERAL jsonb_array_elements(\
         CASE WHEN jsonb_typeof(r.json -> 'extends') = 'array' \
         THEN r.json -> 'extends' ELSE '[]'::jsonb END) e(value) \
         WHERE {} AND r.path = '/types' AND r.deleted = FALSE \
         AND r.conflict IS NULL AND (${file_idx}::text IS NULL OR r.file_path = ${file_idx}) \
         AND jsonb_typeof(e.value) = 'string')",
        view_join(true),
        visible(true)
    )
}

/// The CTE `closure(decl, anc)`: every type with each ancestor its
/// `extends` lists reach, over `edges`. `UNION` makes a cycle terminate.
const TYPE_CLOSURE_CTE: &str = "closure(decl, anc) AS (SELECT child, parent FROM edges \
     UNION SELECT e.child, c.anc FROM edges e JOIN closure c ON e.parent = c.decl)";

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
    if !query.include_conflicts {
        sql.push_str(" AND r.conflict IS NULL");
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
        "SELECT r.key_id AS id, v.worktree_id, r.file_path, r.path, r.key, r.commit_id, \
         json(r.json) AS json, r.version, r.deleted, r.conflict FROM record r",
    );
    sql.push_str(&view_join(false));
    sql.push_str(" WHERE ");
    sql.push_str(&visible(false));
    let mut idx: usize = 4;
    push_filter_sql_sqlite(&mut sql, query, &mut idx);
    if after.is_some() {
        // Keyset cursor over the `ORDER BY` below. Row-value comparison
        // (sqlite >= 3.15) is the whole predicate, so the anchor row
        // itself need not still exist -- a record deleted between pages
        // doesn't strand the walk. `file_path` is in the tuple because
        // the unique index is per (worktree, file, path, key): two files
        // may hold the same (path, key), and without the tiebreak a page
        // ending on the first would resume past the second, dropping it
        // silently. sqlite's default BINARY collation
        // compares UTF-8 bytes, which is the ordering the page token
        // promises and the other two implementations reproduce.
        let cols = after.as_ref().expect("checked").columns();
        let binds: Vec<String> = (0..cols.len()).map(|i| format!("?{}", idx + i)).collect();
        sql.push_str(&format!(
            " AND ({}) > ({})",
            cols.join(", "),
            binds.join(", ")
        ));
        idx += cols.len();
    }
    // Total order, whatever the cursor constrains: this is the unique
    // index rearranged, so no two rows tie and a page boundary always
    // falls in a well-defined place. `r.id` is the final tiebreak
    // because that index is *partial* -- with `include_conflicts` the
    // two sides of a contested record share all four columns, and
    // without it nothing ties, so it costs nothing when off.
    sql.push_str(" ORDER BY r.path, r.key, r.file_path, v.worktree_id, r.id");
    if limit.is_some() {
        sql.push_str(&format!(" LIMIT ?{idx}"));
        idx += 1;
    }
    let _ = idx; // silence unused-assignment lint when the last bind isn't used

    let mut args = sqlx::sqlite::SqliteArguments::default();
    let scope = WorktreeScope::new(worktree_id, query.worktrees.as_ref());
    args.add(scope.id).map_err(arg_err)?;
    args.add(scope.origin).map_err(arg_err)?;
    args.add(scope.branch).map_err(arg_err)?;
    add_filter_args_sqlite(&mut args, query)?;
    if let Some(c) = after {
        args.add(c.path.as_str()).map_err(arg_err)?;
        args.add(c.key.as_str()).map_err(arg_err)?;
        if c.columns().len() > 2 {
            args.add(c.file_path.as_deref().unwrap_or_default())
                .map_err(arg_err)?;
        }
        if c.columns().len() > 3 {
            args.add(c.worktree_id.unwrap_or_default())
                .map_err(arg_err)?;
        }
    }
    if let Some(n) = limit {
        args.add(*n).map_err(arg_err)?;
    }
    let rows = sqlx::query_as_with::<_, FoundColumns, _>(&sql, args)
        .fetch_all(pool)
        .await?;

    rows.into_iter().map(Record::try_from).collect()
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
    if !query.include_conflicts {
        sql.push_str(" AND r.conflict IS NULL");
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
    // The page is found from ids and sort keys, then joined to the rest of
    // its rows: Postgres computes the select list below the sort, so every
    // candidate's JSON would be rendered and carried through the anti-join.
    // SQLite computes it only for the rows the limit keeps, so its arm
    // doesn't need this.
    let mut sql = String::from("SELECT r.id, v.worktree_id FROM record r");
    sql.push_str(&view_join(true));
    sql.push_str(" WHERE ");
    sql.push_str(&visible(true));
    let mut idx: usize = 4;
    push_filter_sql_pg(&mut sql, query, &mut idx);
    if after.is_some() {
        // Keyset cursor; see the sqlite arm. `COLLATE "C"` here and on
        // the `ORDER BY` below pins byte-wise ordering: the database's
        // default collation is locale-dependent (`en_US.UTF-8` sorts
        // "é" before "z", byte order after), and a page token minted by
        // the sqlite or python implementation has to mean the same thing
        // here or a walk would skip or repeat records.
        let cols = after.as_ref().expect("checked").columns();
        // `COLLATE "C"` on the text columns only -- worktree_id is a
        // BIGINT and collating it is an error.
        let compared: Vec<String> = cols
            .iter()
            .map(|c| {
                if *c == "v.worktree_id" {
                    (*c).to_string()
                } else {
                    format!("{c} COLLATE \"C\"")
                }
            })
            .collect();
        let binds: Vec<String> = (0..cols.len()).map(|i| format!("${}", idx + i)).collect();
        sql.push_str(&format!(
            " AND ({}) > ({})",
            compared.join(", "),
            binds.join(", ")
        ));
        idx += cols.len();
    }
    // `r.id` last -- see the sqlite arm.
    if limit.is_some() {
        sql.push_str(&format!(
            " ORDER BY r.path COLLATE \"C\", r.key COLLATE \"C\", \
             r.file_path COLLATE \"C\", v.worktree_id, r.id LIMIT ${idx}"
        ));
        idx += 1;
    }
    let _ = idx;
    let sql = format!(
        "SELECT r.key_id AS id, p.worktree_id, r.file_path, r.path, r.key, r.commit_id, \
         r.json::text AS json, r.version, r.deleted, r.conflict \
         FROM ({sql}) p JOIN record r ON r.id = p.id \
         ORDER BY r.path COLLATE \"C\", r.key COLLATE \"C\", \
         r.file_path COLLATE \"C\", p.worktree_id, r.id"
    );

    let mut args = sqlx::postgres::PgArguments::default();
    let scope = WorktreeScope::new(worktree_id, query.worktrees.as_ref());
    args.add(scope.id).map_err(arg_err)?;
    args.add(scope.origin).map_err(arg_err)?;
    args.add(scope.branch).map_err(arg_err)?;
    add_filter_args_pg(&mut args, query)?;
    if let Some(c) = after {
        args.add(c.path.as_str()).map_err(arg_err)?;
        args.add(c.key.as_str()).map_err(arg_err)?;
        if c.columns().len() > 2 {
            args.add(c.file_path.as_deref().unwrap_or_default())
                .map_err(arg_err)?;
        }
        if c.columns().len() > 3 {
            args.add(c.worktree_id.unwrap_or_default())
                .map_err(arg_err)?;
        }
    }
    if let Some(n) = limit {
        args.add(*n).map_err(arg_err)?;
    }
    // Unnamed, so Postgres plans it with the values bound. A cached statement
    // gets a generic plan, which can't see the type list's length or how many
    // worktrees the scope matches, and probes the GIN index once per segment.
    let rows = sqlx::query_as_with::<_, FoundColumns, _>(&sql, args)
        .persistent(false)
        .fetch_all(pool)
        .await?;
    rows.into_iter().map(Record::try_from).collect()
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
    check_reset(db, worktree_id, query).await?;
    let expanded = with_subtypes(db, worktree_id, query).await?;
    let query = expanded.as_ref().unwrap_or(query);
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
    let mut sql = format!(
        "SELECT COUNT(DISTINCT r.id) FROM record r{} WHERE {}",
        view_join(false),
        visible(false)
    );
    let mut idx: usize = 4;
    push_filter_sql_sqlite(&mut sql, query, &mut idx);
    let _ = idx;
    let mut args = sqlx::sqlite::SqliteArguments::default();
    let scope = WorktreeScope::new(worktree_id, query.worktrees.as_ref());
    args.add(scope.id).map_err(arg_err)?;
    args.add(scope.origin).map_err(arg_err)?;
    args.add(scope.branch).map_err(arg_err)?;
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

/// Count the cells of `extracted`, a query yielding `id`, the group value
/// `g` and each member's value `u{i}`, one row per combination: rollup paths
/// LEFT JOIN `rollup_pairs` and group by `COALESCE(bucket, value)`, so a
/// value with pairs counts under each of its buckets and one without under
/// itself. A record reaching a cell through several values (duplicate array
/// elements, diamond rollup paths) counts once: `DISTINCT` then `COUNT(*)`,
/// which can hash where `COUNT(DISTINCT r.id)` sorts every row.
fn facet_cells_sql(group_rollup: bool, members: &[FacetPath], extracted: &str) -> String {
    let group_out = if group_rollup {
        "COALESCE(mg.anc, f.g)"
    } else {
        "f.g"
    };
    let mut cells = format!("SELECT DISTINCT {group_out} AS g0");
    for (i, member) in members.iter().enumerate() {
        let out = if member.rollup {
            format!("COALESCE(m{i}.anc, f.u{i})")
        } else {
            format!("f.u{i}")
        };
        cells.push_str(&format!(", {out} AS v{i}"));
    }
    cells.push_str(&format!(", f.id FROM ({extracted}) f"));
    if group_rollup {
        cells.push_str(" LEFT JOIN rollup_pairs mg ON mg.decl = f.g");
    }
    for (i, member) in members.iter().enumerate() {
        if member.rollup {
            cells.push_str(&format!(
                " LEFT JOIN rollup_pairs m{i} ON m{i}.decl = f.u{i}"
            ));
        }
    }
    let cols: String = (0..members.len()).map(|i| format!(", v{i}")).collect();
    format!("SELECT g0{cols}, COUNT(*) AS n FROM ({cells}) d GROUP BY g0{cols}")
}

/// One sqlite facet aggregation over the group path plus `members`
/// (empty for the group-only counts).
///
/// Shape: one `json_each` lateral per path contributing values per
/// [`facet_value_sqlite`], counted by [`facet_cells_sql`]. The
/// `rollup_pairs` it joins are a `MATERIALIZED` CTE of `(type, bucket)`
/// pairs -- each type with an ancestor paired with itself and each of its
/// ancestors, JSON-quoted so both sides compare as JSON text.
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
    let with_pairs = group.rollup || members.iter().any(|m| m.rollup);

    // Placeholder allocation order is bind order: worktree, filters,
    // group path, member paths, the types' file. The WHERE fragment is built
    // first so the filters take the low indexes, then spliced in after
    // the joins -- `?N` placeholders don't care about textual position.
    let mut where_sql = String::new();
    let mut idx: usize = 4;
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
    let types_file_idx = with_pairs.then(|| {
        let i = idx;
        idx += 1;
        i
    });
    let _ = idx;

    let mut sql = String::new();
    if let Some(fi) = types_file_idx {
        // MATERIALIZED so the pairs are computed once and the planner
        // can build an automatic index for the joins below.
        sql.push_str(&format!(
            "WITH RECURSIVE {}, {TYPE_CLOSURE_CTE}, \
             rollup_pairs(decl, anc) AS MATERIALIZED (\
             SELECT json_quote(decl), json_quote(anc) FROM closure \
             UNION SELECT json_quote(decl), json_quote(decl) FROM closure) ",
            type_edges_cte_sqlite(fi)
        ));
    }
    let mut extracted = format!("SELECT r.id, {} AS g", facet_value_sqlite("jg"));
    for i in 0..members.len() {
        extracted.push_str(&format!(
            ", {} AS u{i}",
            facet_value_sqlite(&format!("j{i}"))
        ));
    }
    extracted.push_str(" FROM record r");
    extracted.push_str(&view_join(false));
    extracted.push_str(&format!(" JOIN json_each(r.json, ?{group_idx}) jg"));
    for (i, idx) in member_idx.iter().enumerate() {
        extracted.push_str(&format!(" JOIN json_each(r.json, ?{idx}) j{i}"));
    }
    extracted.push_str(" WHERE ");
    extracted.push_str(&visible(false));
    extracted.push_str(&where_sql);
    sql.push_str(&facet_cells_sql(group.rollup, members, &extracted));

    let mut args = sqlx::sqlite::SqliteArguments::default();
    let scope = WorktreeScope::new(worktree_id, query.worktrees.as_ref());
    args.add(scope.id).map_err(arg_err)?;
    args.add(scope.origin).map_err(arg_err)?;
    args.add(scope.branch).map_err(arg_err)?;
    add_filter_args_sqlite(&mut args, query)?;
    args.add(group.sql_path()).map_err(arg_err)?;
    for member in members {
        args.add(member.sql_path()).map_err(arg_err)?;
    }
    if types_file_idx.is_some() {
        args.add(query.file_path.as_deref()).map_err(arg_err)?;
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

/// The lateral extracting one path's facet values on postgres, through
/// the `facet_values` function (see its migration): an array's elements,
/// an object's keys, or a scalar itself. A missing path yields no rows,
/// dropping the record -- the same as sqlite's `json_each` returning none.
#[cfg(feature = "postgres")]
fn facet_lateral_pg(alias: &str, param_idx: usize) -> String {
    format!(" CROSS JOIN LATERAL facet_values(r.json #> ${param_idx}::text[]) {alias}(val)")
}

#[cfg(feature = "postgres")]
async fn facet_pg(
    pool: &sqlx::Pool<sqlx::Postgres>,
    worktree_id: i64,
    query: &RecordQuery,
    spec: &FacetSpec,
) -> Result<FacetRows> {
    use sqlx::Arguments;
    let mut sql = format!(
        "SELECT COUNT(DISTINCT r.id) FROM record r{} WHERE {}",
        view_join(true),
        visible(true)
    );
    let mut idx: usize = 4;
    push_filter_sql_pg(&mut sql, query, &mut idx);
    let _ = idx;
    let mut args = sqlx::postgres::PgArguments::default();
    let scope = WorktreeScope::new(worktree_id, query.worktrees.as_ref());
    args.add(scope.id).map_err(arg_err)?;
    args.add(scope.origin).map_err(arg_err)?;
    args.add(scope.branch).map_err(arg_err)?;
    add_filter_args_pg(&mut args, query)?;
    // planned per call, as in `find_pg`
    let (total,): (i64,) = sqlx::query_as_with(&sql, args)
        .persistent(false)
        .fetch_one(pool)
        .await?;

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
/// (semantic equality, so object key order can't split a bucket), and
/// the rollup pairs are jsonb strings.
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
    let with_pairs = group.rollup || members.iter().any(|m| m.rollup);

    // Placeholder allocation order is bind order: worktree, filters,
    // group path, member paths, the types' file.
    let mut where_sql = String::new();
    let mut idx: usize = 4;
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
    let types_file_idx = with_pairs.then(|| {
        let i = idx;
        idx += 1;
        i
    });
    let _ = idx;

    let mut sql = String::new();
    if let Some(fi) = types_file_idx {
        sql.push_str(&format!(
            "WITH RECURSIVE {}, {TYPE_CLOSURE_CTE}, \
             rollup_pairs(decl, anc) AS MATERIALIZED (\
             SELECT to_jsonb(decl), to_jsonb(anc) FROM closure \
             UNION SELECT to_jsonb(decl), to_jsonb(decl) FROM closure) ",
            type_edges_cte_pg(fi)
        ));
    }
    let mut extracted = String::from("SELECT r.id, jg.val AS g");
    for i in 0..members.len() {
        extracted.push_str(&format!(", j{i}.val AS u{i}"));
    }
    extracted.push_str(" FROM record r");
    extracted.push_str(&view_join(true));
    extracted.push_str(&facet_lateral_pg("jg", group_idx));
    for (i, idx) in member_idx.iter().enumerate() {
        extracted.push_str(&facet_lateral_pg(&format!("j{i}"), *idx));
    }
    extracted.push_str(" WHERE ");
    extracted.push_str(&visible(true));
    extracted.push_str(&where_sql);
    // A fence: without it the planner joins the rollup pairs first and
    // extracts the members once per bucket instead of once per record.
    extracted.push_str(" OFFSET 0");
    sql.push_str(&facet_cells_sql(group.rollup, members, &extracted));

    let mut args = sqlx::postgres::PgArguments::default();
    let scope = WorktreeScope::new(worktree_id, query.worktrees.as_ref());
    args.add(scope.id).map_err(arg_err)?;
    args.add(scope.origin).map_err(arg_err)?;
    args.add(scope.branch).map_err(arg_err)?;
    add_filter_args_pg(&mut args, query)?;
    args.add(&group.tokens).map_err(arg_err)?;
    for member in members {
        args.add(&member.tokens).map_err(arg_err)?;
    }
    if types_file_idx.is_some() {
        args.add(query.file_path.as_deref()).map_err(arg_err)?;
    }
    // planned per call, as in `find_pg`
    let rows = sqlx::query_with(&sql, args)
        .persistent(false)
        .fetch_all(pool)
        .await?;
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
    worktrees: Option<&WorktreeFilter>,
    keys: &[&str],
    alias: bool,
    exclude_ids: &[i64],
    since_version: Option<i64>,
) -> Result<Vec<Record>> {
    if keys.is_empty() {
        return Ok(Vec::new());
    }
    let scope = WorktreeScope::new(worktree_id, worktrees);
    match db {
        Db::Sqlite(pool) => {
            find_many_sqlite(pool, scope, keys, alias, exclude_ids, since_version).await
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            find_many_pg(pool, scope, keys, alias, exclude_ids, since_version).await
        }
    }
}

async fn find_many_sqlite(
    pool: &sqlx::Pool<sqlx::Sqlite>,
    scope: WorktreeScope,
    keys: &[&str],
    alias: bool,
    exclude_ids: &[i64],
    since_version: Option<i64>,
) -> Result<Vec<Record>> {
    let mut sql = String::from(
        "SELECT r.key_id AS id, v.worktree_id, r.file_path, r.path, r.key, r.commit_id, \
         json(r.json) AS json, r.version FROM record r",
    );
    sql.push_str(&view_join(false));
    sql.push_str(" WHERE ");
    sql.push_str(&visible(false));
    sql.push_str(" AND r.deleted = 0 AND r.conflict IS NULL");
    let mut idx: usize = 4;

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
        sql.push_str(&format!(
            " AND r.key_id NOT IN ({})",
            exclude_phs.join(", ")
        ));
    }

    if since_version.is_some() {
        sql.push_str(&format!(" AND r.version > ?{idx}"));
        idx += 1;
    }
    sql.push_str(" ORDER BY r.path, r.key, v.worktree_id");
    let _ = idx;

    let mut q = sqlx::query_as::<_, FoundColumns>(&sql)
        .bind(scope.id)
        .bind(scope.origin)
        .bind(scope.branch);
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

    rows.into_iter().map(Record::try_from).collect()
}

#[cfg(feature = "postgres")]
async fn find_many_pg(
    pool: &sqlx::Pool<sqlx::Postgres>,
    scope: WorktreeScope,
    keys: &[&str],
    alias: bool,
    exclude_ids: &[i64],
    since_version: Option<i64>,
) -> Result<Vec<Record>> {
    let mut sql = String::from(
        "SELECT r.key_id AS id, v.worktree_id, r.file_path, r.path, r.key, r.commit_id, \
         r.json::text AS json, r.version FROM record r",
    );
    sql.push_str(&view_join(true));
    sql.push_str(" WHERE ");
    sql.push_str(&visible(true));
    sql.push_str(" AND r.deleted = FALSE AND r.conflict IS NULL");
    let mut idx: usize = 4;

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
        sql.push_str(&format!(
            " AND r.key_id NOT IN ({})",
            exclude_phs.join(", ")
        ));
    }

    if since_version.is_some() {
        sql.push_str(&format!(" AND r.version > ${idx}"));
        idx += 1;
    }
    // byte-wise, as in `find_pg`, whatever the database's collation
    sql.push_str(" ORDER BY r.path COLLATE \"C\", r.key COLLATE \"C\", v.worktree_id");
    let _ = idx;

    // planned per call, as in `find_pg`
    let mut q = sqlx::query_as::<_, FoundColumns>(&sql)
        .persistent(false)
        .bind(scope.id)
        .bind(scope.origin)
        .bind(scope.branch);
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
    rows.into_iter().map(Record::try_from).collect()
}

pub(crate) async fn get(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
) -> Result<Option<Record>> {
    let at = At {
        file_path,
        path,
        key,
    };
    on_pool!(db, pool => read(pool, async |tx| {
        Ok(Store::visible(tx, worktree_id, Scope::Own, Filter::At(at))
            .await?
            .into_iter()
            .find(|r| !r.deleted)
            .map(|r| r.into_record(worktree_id)))
    }).await)
}

/// The record with `key_id` `id` as the worktree's view shows it,
/// tombstones included.
pub(crate) async fn get_by_id(db: &Db, worktree_id: i64, id: i64) -> Result<Option<Record>> {
    on_pool!(db, pool => read(pool, async |tx| {
        Ok(Store::visible(tx, worktree_id, Scope::Own, Filter::KeyId(id))
            .await?
            .into_iter()
            .next()
            .map(|r| r.into_record(worktree_id)))
    }).await)
}

/// Listing API: when `since` is `Some(v)`, every row the worktree's view
/// shows at a `version > v`, committed or pending, tombstones included;
/// when `since` is `None`, only the pending edits — what
/// `commit_repository` would write next.
///
/// `include_conflicts` adds the file's side of conflicted records. They
/// draw versions like any other row, so a poller catching up from a
/// watermark can discover a conflict the moment it is materialized —
/// but only if it asks, since a caller merely tracking record state
/// would otherwise see each conflicted record twice.
pub(crate) async fn list_changes(
    db: &Db,
    worktree_id: i64,
    since: Option<i64>,
    include_conflicts: bool,
) -> Result<Vec<Record>> {
    if let Some(since) = since {
        check_since(db, &WorktreeScope::new(worktree_id, None), since).await?;
    }
    on_pool!(db, pool => read(pool, async |tx| {
        let d = Store::segs(tx, worktree_id).await?.draft;
        let mut rows: Vec<RecordRow> = match since {
            Some(v) => Store::visible(tx, worktree_id, Scope::Own, Filter::Since(v)).await?,
            None => Store::rows_in(tx, d, None, false)
                .await?
                .into_iter()
                .filter(RecordRow::is_edit)
                .collect(),
        };
        if include_conflicts {
            rows.extend(
                Store::rows_in(tx, d, None, true)
                    .await?
                    .into_iter()
                    .filter(|r| since.is_none_or(|v| r.version > v)),
            );
        }
        rows.sort_by_key(|r| (r.version, r.id));
        Ok(rows.into_iter().map(|r| r.into_record(worktree_id)).collect())
    }).await)
}

/// Every conflict row of the worktree, optionally narrowed to one file,
/// ordered like [`find`] so a caller can zip them against the records
/// they shadow.
pub(crate) async fn list_conflicts(
    db: &Db,
    worktree_id: i64,
    file_path: Option<&str>,
) -> Result<Vec<Record>> {
    find(
        db,
        worktree_id,
        &RecordQuery {
            file_path: file_path.map(str::to_string),
            include_conflicts: true,
            // A conflict row is a tombstone whenever the file dropped the
            // record, which is half of what there is to report.
            include_deleted: true,
            ..Default::default()
        },
    )
    .await
    .map(|rows| {
        rows.into_iter()
            .filter(|r| r.conflict.is_some())
            .collect::<Vec<_>>()
    })
}
