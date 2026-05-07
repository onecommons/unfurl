// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Local handler for `GET /cloudmap` backed by the `unfurl-git-sync`
//! crate.
//!
//! Wires up a [`unfurl_git_sync::SyncedRepo`] at startup when both
//! `cloudmap_repo` and `cloudmap_db_url` are configured. The handler
//! reads the cloudmap from the working tree, applies optional `kind` /
//! `key` filters, and (when `follow > 0`) walks
//! [`unfurl_git_sync::SyncedRepo::find_records_follow`] to populate the
//! second element of the response pair.
//!
//! When the repo is not configured, the handler proxies the request to
//! the Python backend.

use axum::{
    extract::State,
    http::{Request, StatusCode},
    response::{IntoResponse, Json, Response},
};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::sync::Arc;
use unfurl_git_sync::{BatchOp, CommitRef, DbConfig, FormatRegistry, Record, SyncedRepo};

use crate::proxy;
use crate::routes::{ValidatedJson, ValidatedQuery};
use crate::unfurl_types;
use crate::AppState;

/// Maps cloudmap section name (the URL query value) to its
/// JSON-pointer path stored in the `record.path` column.
const KIND_TO_PATH: &[(&str, &str)] = &[
    ("repositories", "/repositories"),
    ("artifacts", "/artifacts"),
    ("services", "/services"),
    ("instantiations", "/instantiations"),
    ("types", "/types"),
];

/// Reverse lookup: `record.path` → cloudmap section name.
fn section_for_path(path: &str) -> Option<&'static str> {
    for (section, p) in KIND_TO_PATH {
        if *p == path {
            return Some(section);
        }
    }
    None
}

fn path_for_kind(kind: &str) -> Option<&'static str> {
    for (section, p) in KIND_TO_PATH {
        if *section == kind {
            return Some(p);
        }
    }
    None
}

/// Long-lived cloudmap handle stored in [`crate::AppState`].
#[derive(Clone)]
pub struct CloudMapState {
    inner: Arc<SyncedRepo>,
}

impl CloudMapState {
    /// Open the working dir, run an initial scan, and return a handle
    /// suitable for stashing in [`crate::AppState`].
    ///
    /// Both `repo_path` and `db_url` must be set. The DB URL is parsed
    /// to pick the right backend: anything starting with `postgres://`
    /// or `postgresql://` is Postgres (only when the
    /// `unfurl-git-sync/postgres` feature is enabled at build time);
    /// everything else is treated as SQLite.
    pub async fn open(
        repo_path: &str,
        db_url: &str,
    ) -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        let db_cfg = if db_url.starts_with("postgres://") || db_url.starts_with("postgresql://") {
            #[cfg(feature = "postgres")]
            {
                DbConfig::Postgres { url: db_url.into() }
            }
            #[cfg(not(feature = "postgres"))]
            {
                return Err(format!(
                    "cloudmap_db_url is Postgres ({db_url}) but unfurl-server was built \
                     without the `postgres` feature"
                )
                .into());
            }
        } else {
            DbConfig::Sqlite { url: db_url.into() }
        };

        let registry = FormatRegistry::with_builtins();
        let synced = SyncedRepo::open(repo_path, db_cfg, registry).await?;
        synced.update_from_working_dir().await?;
        Ok(Self {
            inner: Arc::new(synced),
        })
    }

    /// Wrap an already-opened [`SyncedRepo`] in a [`CloudMapState`].
    ///
    /// Useful in tests that want to control the working dir, database
    /// URL, and format registry directly rather than going through
    /// [`CloudMapState::open`].
    pub fn from_synced(synced: SyncedRepo) -> Self {
        Self {
            inner: Arc::new(synced),
        }
    }
}

/// Axum handler for `GET /cloudmap`.
///
/// Local fast-path when [`AppState::cloudmap`] is set; otherwise
/// proxies to the Python backend.
pub async fn handle_cloudmap(
    State(state): State<AppState>,
    ValidatedQuery(params): ValidatedQuery<unfurl_types::GetCloudmapRequestQuery>,
    req: Request<axum::body::Body>,
) -> Response {
    let Some(cm) = state.cloudmap.clone() else {
        return proxy::forward(
            &state.client,
            &state.config.backend_url(),
            req,
            state.config.max_body_bytes,
        )
        .await;
    };
    drop(req);

    match build_response(&cm, &params).await {
        Ok(body) => Json(body).into_response(),
        Err(LocalError::NotFound(msg)) => {
            (StatusCode::NOT_FOUND, Json(json!({"error": msg}))).into_response()
        }
        Err(LocalError::Internal(msg)) => {
            tracing::error!("cloudmap handler error: {}", msg);
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": msg})),
            )
                .into_response()
        }
    }
}

enum LocalError {
    NotFound(String),
    Internal(String),
}

/// Build the `[primary, followed]` pair returned by `GET /cloudmap`.
///
/// Each element is a CloudMap-shaped object — declared as
/// [`unfurl_types::CloudMapDocumentPair`] in the OpenAPI spec. We don't
/// strict-deserialize through [`unfurl_types::CloudMapDocument`] on emit
/// because the typed struct has required fields (`apiVersion`,
/// `kind`) that get filled with defaults; Python's `get_cloudmap`
/// returns a bare `{}` for an empty `followed` and we keep wire
/// parity. So we emit each element as a [`Value::Object`] containing
/// only the section maps that actually have records.
async fn build_response(
    cm: &CloudMapState,
    params: &unfurl_types::GetCloudmapRequestQuery,
) -> Result<Vec<Value>, LocalError> {
    let synced = cm.inner.as_ref();
    let kind = params.kind.as_deref();
    let key = params.key.as_deref();

    // Resolve `kind` to its JSON-pointer path up-front so an unknown
    // kind 404s before we issue any DB query.
    let path: Option<&'static str> = match kind {
        Some(k) => Some(
            path_for_kind(k)
                .ok_or_else(|| LocalError::NotFound(format!("section {k:?} not found")))?,
        ),
        None => None,
    };

    // Walking only makes sense when we have a starting key.
    // alias=true when filtering by key (so versioned URLs hit their
    // canonical record); alias=false for the full document.
    let follow = if key.is_some() {
        params.follow.unwrap_or(0).max(0) as u32
    } else {
        0
    };
    let alias = key.is_some();

    // Single DB query for both halves of the response. With follow=0
    // `find_records_follow` returns the initial set and an empty
    // followed Vec — same shape as a plain `find_records` call.
    // `since_version` is pushed into the SQL `WHERE` clause by
    // `db::record::find`, so both the initial set and any follow-walk
    // edge lookups are filtered at the database.
    // `exclude` arrives as a comma-separated list of record primary-key
    // ids; non-numeric tokens are silently dropped. Empty / `None`
    // string means "no exclusion" — matches the schema description.
    let exclude_ids: Vec<i64> = params
        .exclude
        .as_deref()
        .map(|s| {
            s.split(',')
                .filter_map(|tok| tok.trim().parse::<i64>().ok())
                .collect()
        })
        .unwrap_or_default();

    let (initial, followed_records) = synced
        .find_records_follow(
            None,
            path.map(|s| s.to_string()),
            key.map(|s| s.to_string()),
            alias,
            follow,
            params.since_version,
            exclude_ids,
        )
        .await
        .map_err(|e| LocalError::Internal(format!("find_records_follow: {e}")))?;

    if let (Some(kind_str), Some(key_str)) = (kind, key) {
        if initial.is_empty() {
            return Err(LocalError::NotFound(format!(
                "key {key_str:?} not found in {kind_str:?}"
            )));
        }
    }

    let primary = records_to_document(initial);
    let followed = records_to_document(followed_records);
    Ok(vec![primary, followed])
}

/// Group records by section (`record.path`) and emit a CloudMap-shaped
/// object: `{ section: { key: json } }`. An empty input produces `{}`.
///
/// Each record's JSON object is enriched with three extra keys so
/// clients can identify the row and echo back a
/// [`unfurl_git_sync::CommitRef`] for optimistic-concurrency on
/// subsequent writes:
///
/// - `"unfurl.server.id"` — the row's primary-key id (always
///   present); useful as a stable identifier across renames and as
///   an entry in the `exclude` list passed to a follow-up
///   `find_records_follow`.
/// - `"unfurl.server.version"` — the row's [`Record::version`]
///   (always present); use as `CommitRef::Pending(v)`.
/// - `"unfurl.server.commit"` — the row's `commit_id`, or `null`
///   when the record is in-flight; use as `CommitRef::Commit(o)` when
///   non-null.
///
/// Records whose JSON payload isn't an object are left as-is — the
/// cloudmap format only emits map-valued records, so this is a
/// defensive fallthrough.
fn records_to_document(records: Vec<Record>) -> Value {
    let mut sections: BTreeMap<&'static str, Map<String, Value>> = BTreeMap::new();
    for r in records {
        let Some(section) = section_for_path(&r.path) else {
            continue;
        };
        let enriched = annotate_record(r.json, r.id, r.version, r.commit_id);
        sections.entry(section).or_default().insert(r.key, enriched);
    }
    let mut out = Map::new();
    for (section, entries) in sections {
        out.insert(section.to_string(), Value::Object(entries));
    }
    Value::Object(out)
}

/// Splice the row's identity + OCC tokens onto its JSON payload.
/// No-op when the payload isn't a JSON object.
fn annotate_record(json: Value, id: i64, version: i64, commit_id: Option<String>) -> Value {
    let Value::Object(mut map) = json else {
        return json;
    };
    map.insert("unfurl.server.id".to_string(), Value::from(id));
    map.insert("unfurl.server.version".to_string(), Value::from(version));
    map.insert(
        "unfurl.server.commit".to_string(),
        commit_id.map(Value::from).unwrap_or(Value::Null),
    );
    Value::Object(map)
}

// ---------------------------------------------------------------------------
// POST /cloudmap — write handler.
// ---------------------------------------------------------------------------

/// Pop the OCC tokens out of a record's JSON object and convert them
/// into a [`CommitRef`].
///
/// - `unfurl.server.commit` (string) → [`CommitRef::Commit`].
/// - `unfurl.server.version` (i64) → [`CommitRef::Pending`].
/// - When both are present, `Commit` wins (it's the stricter token).
/// - All three identity keys (including `unfurl.server.id`) are
///   popped regardless so none leak into the payload that gets
///   persisted to disk.
fn pop_commit_ref(map: &mut Map<String, Value>) -> Option<CommitRef> {
    let oid = map
        .remove("unfurl.server.commit")
        .and_then(|v| v.as_str().map(str::to_string));
    let version = map.remove("unfurl.server.version").and_then(|v| v.as_i64());
    // `unfurl.server.id` is server-assigned identity; the row already
    // owns its id, so the client's echo is dropped without affecting
    // the OCC decision.
    map.remove("unfurl.server.id");
    if let Some(o) = oid {
        return Some(CommitRef::Commit(o));
    }
    version.map(CommitRef::Pending)
}

/// Local axum handler for `POST /cloudmap`.
///
/// Body is the typed [`unfurl_types::CloudMapDocument`]; the
/// [`ValidatedJson`] extractor turns a shape mismatch into a **422
/// Unprocessable Entity** (matching APIFlask on the Python backend)
/// instead of axum's default 400. Each
/// section maps record keys (URLs) to objects that schema-validate
/// as the corresponding cloudmap entity. Two extension keys on the
/// record drive special behaviour:
///
/// - `unfurl.server.{version,commit}` — optional OCC token gating
///   the write.
/// - `unfurl.server.deleted: true` — delete the record (OCC tokens
///   still honoured).
///
/// Unknown top-level sections produce a **400 Bad Request** with an
/// `error: "unknown section <name>"` body, matching the Python
/// handler's behaviour. Serde alone would silently drop them (the
/// generated type uses `#[serde(flatten)] additional_properties` for
/// forward-compat with new envelope keys); the handler explicitly
/// inspects that bag and rejects truly unknown keys before applying.
/// Record fields not in the schema are still silently dropped.
///
/// Errors fail fast: the first record whose OCC token mismatches
/// returns 409 and the remainder are skipped. Previously-applied
/// edits stay in-flight (queryable via
/// [`unfurl_git_sync::SyncedRepo::list_changes`]); no `save_changes`
/// / `commit_repository` is invoked — the caller drives commit
/// separately.
///
/// This handler is registered only when `state.cloudmap` is `Some`;
/// see [`post_cloudmap_proxy`] for the unconfigured fall-through.
pub async fn post_cloudmap_local(
    State(state): State<AppState>,
    ValidatedJson(body): ValidatedJson<unfurl_types::PostCloudmapRequest>,
) -> Result<Json<unfurl_types::PatchResponse>, ApiError> {
    let cm = state
        .cloudmap
        .as_ref()
        .expect("post_cloudmap_local registered without CloudMapState");
    // Reject unknown top-level keys (sections or envelope) the same
    // way the Python handler does — they end up in the typed
    // request's `additional_properties` bag because oas3-gen reflects
    // the Pydantic `extra="allow"` config as a flatten'd HashMap, and
    // serde otherwise drops them silently.
    if let Some(unknown) = body.additional_properties.keys().next() {
        return Err(WriteError::BadRequest(format!("unknown section {unknown:?}")).into());
    }
    // The Rust handler stages records to the SyncedRepo's database
    // (in-flight, `commit_id IS NULL`). It does not drive a git
    // commit — the caller does that separately — so `commit` is
    // always null here. We return the largest `version` the CRUD
    // calls stamped during this batch as `queueid`: the client can
    // echo it back as `unfurl.server.version` on the next request to
    // gate the optimistic-concurrency check. Versions are monotonic
    // per worktree, so the last write's version is also the largest.
    let atomic = body.atomic.unwrap_or(true);
    let result = apply_writes(cm, body, atomic).await?;
    Ok(Json(unfurl_types::PatchResponse {
        commit: None,
        queueid: result.last_version,
        applied: Some(result.applied),
    }))
}

/// Proxy fallthrough for `POST /cloudmap` when `state.cloudmap` is
/// `None`. Forwards the request verbatim to the Python backend.
///
/// Registered in place of [`post_cloudmap_local`] at server startup
/// when no cloudmap repo is configured; see `main.rs` for the
/// branching.
pub async fn post_cloudmap_proxy(
    State(state): State<AppState>,
    req: Request<axum::body::Body>,
) -> Response {
    proxy::forward(
        &state.client,
        &state.config.backend_url(),
        req,
        state.config.max_body_bytes,
    )
    .await
}

/// Errors returned by [`post_cloudmap_local`]. `IntoResponse` maps
/// each variant to the appropriate HTTP status with a JSON body.
pub struct ApiError {
    status: StatusCode,
    body: Value,
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.status, Json(self.body)).into_response()
    }
}

impl From<WriteError> for ApiError {
    fn from(err: WriteError) -> Self {
        match err {
            WriteError::BadRequest(msg) => Self {
                status: StatusCode::BAD_REQUEST,
                body: json!({"error": msg}),
            },
            WriteError::Conflict {
                section,
                key,
                actual,
                applied,
                failed,
            } => Self {
                status: StatusCode::CONFLICT,
                body: json!({
                    "error": "conflict",
                    "section": section,
                    "key": key,
                    "actual": actual,
                    "applied": applied
                        .iter()
                        .map(|a| json!({
                            "section": a.section,
                            "key": a.key,
                            "version": a.version,
                        }))
                        .collect::<Vec<_>>(),
                    "failed": failed
                        .iter()
                        .map(|f| json!({
                            "section": f.section,
                            "key": f.key,
                            "actual": f.actual,
                            "error": f.error,
                        }))
                        .collect::<Vec<_>>(),
                }),
            },
            WriteError::Internal(msg) => {
                tracing::error!("post_cloudmap error: {}", msg);
                Self {
                    status: StatusCode::INTERNAL_SERVER_ERROR,
                    body: json!({"error": msg}),
                }
            }
        }
    }
}

enum WriteError {
    BadRequest(String),
    Conflict {
        /// First conflicting record's section (back-compat with the
        /// pre-batch response shape).
        section: String,
        /// First conflicting record's key.
        key: String,
        /// First conflicting record's server-side ``commit_id``.
        actual: Option<String>,
        /// All records that committed before the failure (atomic mode
        /// always rolls back, so this is empty there). Non-atomic
        /// mode populates this with everything that landed.
        applied: Vec<unfurl_types::PatchResponseAppliedRecord>,
        /// All records that did not commit. In atomic mode this is the
        /// single record that triggered the rollback; in non-atomic
        /// mode this is every record skipped.
        failed: Vec<FailedJson>,
    },
    Internal(String),
}

/// Per-record failure detail surfaced in the 409 response body.
#[derive(Clone, Debug)]
struct FailedJson {
    section: String,
    key: String,
    actual: Option<String>,
    error: Option<String>,
}

/// Result of applying a CloudMapDocument body via [`apply_writes`].
struct WriteOutcome {
    last_version: Option<i64>,
    applied: Vec<unfurl_types::PatchResponseAppliedRecord>,
}

/// Convert each section of the request body into [`BatchOp`]s, dispatch
/// to [`SyncedRepo::apply_batch`], then map the outcome → either a
/// success [`WriteOutcome`] or a [`WriteError::Conflict`] carrying the
/// per-record applied/failed lists.
async fn apply_writes(
    cm: &CloudMapState,
    body: unfurl_types::PostCloudmapRequest,
    atomic: bool,
) -> Result<WriteOutcome, WriteError> {
    let synced = cm.inner.as_ref();

    // Build (BatchOp, section_name) pairs in a fixed section order so
    // the batch is deterministic across requests.
    let mut ops: Vec<BatchOp> = Vec::new();
    // Parallel vector mapping each op's index → its section name. The
    // batch outcome reports `path` (JSON-pointer) and `key`; we look
    // up the section here for the response, since "/repositories" →
    // "repositories" is well-defined but spelled twice in the body
    // would be confusing if derived in two places.
    let mut sections: Vec<&'static str> = Vec::new();

    macro_rules! collect {
        ($field:expr, $section:literal, $path:literal) => {
            if let Some(records) = $field {
                for (key, record) in records {
                    let value = serde_json::to_value(&record).map_err(|e| {
                        WriteError::Internal(format!(
                            "section {:?} key {:?}: serialize record: {e}",
                            $section, key
                        ))
                    })?;
                    let op = build_batch_op($section, $path, key, value)?;
                    ops.push(op);
                    sections.push($section);
                }
            }
        };
    }

    collect!(body.repositories, "repositories", "/repositories");
    collect!(body.artifacts, "artifacts", "/artifacts");
    collect!(body.services, "services", "/services");
    collect!(body.instantiations, "instantiations", "/instantiations");
    collect!(body.types, "types", "/types");

    if ops.is_empty() {
        return Ok(WriteOutcome {
            last_version: None,
            applied: Vec::new(),
        });
    }

    let outcome = synced
        .apply_batch(ops, atomic)
        .await
        .map_err(|e| WriteError::Internal(format!("apply_batch: {e}")))?;

    let applied: Vec<unfurl_types::PatchResponseAppliedRecord> = outcome
        .applied
        .iter()
        .map(|a| {
            let section = sections.get(a.index).copied().unwrap_or("");
            unfurl_types::PatchResponseAppliedRecord {
                section: section.to_string(),
                key: a.key.clone(),
                version: a.outcome.version,
            }
        })
        .collect();

    if !outcome.failed.is_empty() {
        // Take the first failure as the canonical conflict identity
        // (matches the existing CloudMapProxyConflict's section/key
        // contract) but include the full `failed` list for callers
        // that want to handle non-atomic mode.
        let primary = &outcome.failed[0];
        let primary_section = sections
            .get(primary.index)
            .copied()
            .unwrap_or("")
            .to_string();
        let mut failed_records: Vec<FailedJson> = Vec::with_capacity(outcome.failed.len());
        let mut primary_actual: Option<String> = None;
        for f in &outcome.failed {
            let section = sections.get(f.index).copied().unwrap_or("").to_string();
            let (kind, actual) = classify_failure(&f.error);
            if std::ptr::eq(f, primary) {
                primary_actual.clone_from(&actual);
            }
            failed_records.push(FailedJson {
                section,
                key: f.key.clone(),
                actual,
                error: kind,
            });
        }
        return Err(WriteError::Conflict {
            section: primary_section,
            key: primary.key.clone(),
            actual: primary_actual,
            applied,
            failed: failed_records,
        });
    }

    Ok(WriteOutcome {
        last_version: outcome.last_version,
        applied,
    })
}

/// Convert a single ``(section, path, key, value)`` triple into a
/// [`BatchOp`]. The OCC marker keys (``unfurl.server.{commit,version,
/// id}``) and the ``unfurl.server.deleted`` flag are popped here so
/// the JSON that goes into git-sync is the bare record payload.
fn build_batch_op(
    section: &str,
    path: &'static str,
    key: String,
    value: Value,
) -> Result<BatchOp, WriteError> {
    match value {
        Value::Object(mut map) => {
            let commit_ref = pop_commit_ref(&mut map);
            let is_delete = matches!(map.remove("unfurl.server.deleted"), Some(Value::Bool(true)));
            if is_delete {
                Ok(BatchOp::Delete {
                    file_path: None,
                    path: path.to_string(),
                    key,
                    expected: commit_ref,
                })
            } else {
                Ok(BatchOp::Upsert {
                    file_path: None,
                    path: path.to_string(),
                    key,
                    json: Value::Object(map),
                    expected: commit_ref,
                })
            }
        }
        other => Err(WriteError::BadRequest(format!(
            "section {section:?} key {key:?}: value must be a JSON object, got {other:?}"
        ))),
    }
}

fn classify_failure(err: &unfurl_git_sync::Error) -> (Option<String>, Option<String>) {
    use unfurl_git_sync::Error as E;
    match err {
        E::Conflict { actual, .. } => (Some("conflict".to_string()), actual.clone()),
        E::NotFound { .. } => (Some("not_found".to_string()), None),
        _ => (Some("error".to_string()), None),
    }
}
