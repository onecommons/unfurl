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
    extract::{Query, State},
    http::{Request, StatusCode},
    response::{IntoResponse, Json, Response},
};
use serde::Serialize;
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;
use unfurl_git_sync::{CommitRef, DbConfig, FormatRegistry, Record, SyncedRepo};

use crate::generated;
use crate::proxy;
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
    Query(params): Query<generated::GetCloudmapRequestQuery>,
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
/// [`generated::CloudMapDocumentPair`] in the OpenAPI spec. We don't
/// strict-deserialize through [`generated::CloudMapDocument`] on emit
/// because the typed struct has required fields (`apiVersion`,
/// `kind`) that get filled with defaults; Python's `get_cloudmap`
/// returns a bare `{}` for an empty `followed` and we keep wire
/// parity. So we emit each element as a [`Value::Object`] containing
/// only the section maps that actually have records.
async fn build_response(
    cm: &CloudMapState,
    params: &generated::GetCloudmapRequestQuery,
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
    let (initial, followed_records) = synced
        .find_records_follow(
            None,
            path.map(|s| s.to_string()),
            key.map(|s| s.to_string()),
            alias,
            follow,
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
/// Each record's JSON object is enriched with two extra keys so
/// clients can echo back a [`unfurl_git_sync::CommitRef`] for
/// optimistic-concurrency on subsequent writes:
///
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
        let enriched = annotate_record(r.json, r.version, r.commit_id);
        sections.entry(section).or_default().insert(r.key, enriched);
    }
    let mut out = Map::new();
    for (section, entries) in sections {
        out.insert(section.to_string(), Value::Object(entries));
    }
    Value::Object(out)
}

/// Splice the OCC tokens onto a record's JSON payload. No-op when the
/// payload isn't a JSON object.
fn annotate_record(json: Value, version: i64, commit_id: Option<String>) -> Value {
    let Value::Object(mut map) = json else {
        return json;
    };
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
/// - Both keys are popped regardless so neither leaks into the
///   payload that gets persisted to disk.
fn pop_commit_ref(map: &mut Map<String, Value>) -> Option<CommitRef> {
    let oid = map
        .remove("unfurl.server.commit")
        .and_then(|v| v.as_str().map(str::to_string));
    let version = map.remove("unfurl.server.version").and_then(|v| v.as_i64());
    if let Some(o) = oid {
        return Some(CommitRef::Commit(o));
    }
    version.map(CommitRef::Pending)
}

/// Local axum handler for `POST /cloudmap`.
///
/// Body is the typed [`generated::CloudMapDocument`]; axum's `Json`
/// extractor returns a 422 automatically on shape mismatch. Each
/// section maps record keys (URLs) to objects that schema-validate
/// as the corresponding cloudmap entity. Two extension keys on the
/// record drive special behaviour:
///
/// - `unfurl.server.{version,commit}` — optional OCC token gating
///   the write.
/// - `unfurl.server.deleted: true` — delete the record (OCC tokens
///   still honoured).
///
/// Unknown top-level sections are silently ignored (no
/// `#[serde(deny_unknown_fields)]` on the generated type); record
/// fields not in the schema are also silently dropped.
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
    Json(body): Json<generated::CloudMapDocument>,
) -> Result<Json<generated::PatchResponse>, ApiError> {
    let cm = state
        .cloudmap
        .as_ref()
        .expect("post_cloudmap_local registered without CloudMapState");
    // The Rust handler stages records to the SyncedRepo's database
    // (in-flight, `commit_id IS NULL`). It does not drive a git
    // commit — the caller does that separately — so `commit` is
    // always null here. We return the largest `version` the CRUD
    // calls stamped during this batch as `queueid`: the client can
    // echo it back as `unfurl.server.version` on the next request to
    // gate the optimistic-concurrency check. Versions are monotonic
    // per worktree, so the last write's version is also the largest.
    let last_version = apply_writes(cm, body).await?;
    Ok(Json(generated::PatchResponse {
        commit: None,
        queueid: last_version,
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
            WriteError::NotFound(msg) => Self {
                status: StatusCode::NOT_FOUND,
                body: json!({"error": msg}),
            },
            WriteError::Conflict {
                section,
                key,
                actual,
            } => Self {
                status: StatusCode::CONFLICT,
                body: json!({
                    "error": "conflict",
                    "section": section,
                    "key": key,
                    "actual": actual,
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
    NotFound(String),
    Conflict {
        section: String,
        key: String,
        actual: Option<String>,
    },
    Internal(String),
}

/// Apply each section of the request body and return the largest
/// `version` stamped on any single CRUD write — `None` if the body
/// produced no writes. Versions are monotonic per worktree, so the
/// last write's version is also the maximum.
async fn apply_writes(
    cm: &CloudMapState,
    body: generated::CloudMapDocument,
) -> Result<Option<i64>, WriteError> {
    let synced = cm.inner.as_ref();
    let mut last_version: Option<i64> = None;

    if let Some(records) = body.repositories {
        update_last(
            &mut last_version,
            apply_section(synced, "repositories", "/repositories", records).await?,
        );
    }
    if let Some(records) = body.artifacts {
        update_last(
            &mut last_version,
            apply_section(synced, "artifacts", "/artifacts", records).await?,
        );
    }
    if let Some(records) = body.services {
        update_last(
            &mut last_version,
            apply_section(synced, "services", "/services", records).await?,
        );
    }
    if let Some(records) = body.instantiations {
        update_last(
            &mut last_version,
            apply_section(synced, "instantiations", "/instantiations", records).await?,
        );
    }
    if let Some(records) = body.types {
        update_last(
            &mut last_version,
            apply_section(synced, "types", "/types", records).await?,
        );
    }

    Ok(last_version)
}

/// Roll the most recent CRUD `version` into `last`. Both arguments are
/// monotonic in batch-order, so a plain `Some(v)` overwrite of `last`
/// is correct.
fn update_last(last: &mut Option<i64>, latest: Option<i64>) {
    if let Some(v) = latest {
        *last = Some(v);
    }
}

/// Serialize each typed record back to a JSON `Value` (this flattens
/// the OCC marker keys captured in `additional_properties` back onto
/// the top level) and dispatch to [`apply_one`]. Records from the
/// spec come in as `HashMap<String, T>` for sections like
/// `repositories` (T = `CloudmapRepository`) or
/// `HashMap<String, Box<T>>` for sections that recurse (artifacts,
/// services, instantiations); the bound is just `Serialize` so a
/// single helper covers both.
async fn apply_section<T: Serialize>(
    synced: &SyncedRepo,
    section: &str,
    path: &'static str,
    records: HashMap<String, T>,
) -> Result<Option<i64>, WriteError> {
    let mut last: Option<i64> = None;
    for (key, record) in records {
        let value = serde_json::to_value(&record).map_err(|e| {
            WriteError::Internal(format!(
                "section {section:?} key {key:?}: serialize record: {e}"
            ))
        })?;
        last = Some(apply_one(synced, section, path, &key, value).await?);
    }
    Ok(last)
}

/// Apply a single `(section, key, value)` write. Returns the
/// `version` stamped on the resulting CRUD row.
async fn apply_one(
    synced: &SyncedRepo,
    section: &str,
    path: &'static str,
    key: &str,
    value: Value,
) -> Result<i64, WriteError> {
    match value {
        Value::Object(mut map) => {
            let commit_ref = pop_commit_ref(&mut map);
            let is_delete = matches!(map.remove("unfurl.server.deleted"), Some(Value::Bool(true)));
            if is_delete {
                do_delete(synced, section, path, key, commit_ref).await
            } else {
                do_upsert(synced, section, path, key, map, commit_ref).await
            }
        }
        other => Err(WriteError::BadRequest(format!(
            "section {section:?} key {key:?}: value must be a JSON object, got {other:?}"
        ))),
    }
}

async fn do_upsert(
    synced: &SyncedRepo,
    section: &str,
    path: &'static str,
    key: &str,
    payload: Map<String, Value>,
    commit_ref: Option<CommitRef>,
) -> Result<i64, WriteError> {
    let json = Value::Object(payload);
    let outcome = synced
        .upsert_record(None, path, key, json, commit_ref)
        .await
        .map_err(|e| map_git_sync_err(e, section, key))?;
    Ok(outcome.version)
}

async fn do_delete(
    synced: &SyncedRepo,
    section: &str,
    path: &'static str,
    key: &str,
    commit_ref: Option<CommitRef>,
) -> Result<i64, WriteError> {
    let outcome = synced
        .delete_record(None, path, key, commit_ref)
        .await
        .map_err(|e| map_git_sync_err(e, section, key))?;
    Ok(outcome.version)
}

/// Convert a [`unfurl_git_sync::Error`] into a typed [`WriteError`],
/// carrying the offending section/key into the user-facing JSON.
fn map_git_sync_err(err: unfurl_git_sync::Error, section: &str, key: &str) -> WriteError {
    use unfurl_git_sync::Error as E;
    match err {
        E::Conflict { actual, .. } => WriteError::Conflict {
            section: section.to_string(),
            key: key.to_string(),
            actual,
        },
        E::NotFound { .. } => WriteError::NotFound(format!(
            "section {section:?} key {key:?}: record not found (and no default file path set)"
        )),
        E::AlreadyExists { .. } => WriteError::Internal(format!(
            "section {section:?} key {key:?}: AlreadyExists from upsert (unexpected)"
        )),
        other => WriteError::Internal(format!("section {section:?} key {key:?}: {other}")),
    }
}
