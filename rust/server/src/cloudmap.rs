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
use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::sync::Arc;
use unfurl_git_sync::{DbConfig, FormatRegistry, Record, SyncedRepo};

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

/// Query parameters for `GET /cloudmap`.
#[derive(Debug, Deserialize)]
pub struct CloudMapParams {
    pub kind: Option<String>,
    pub key: Option<String>,
    #[serde(default)]
    pub follow: u32,
}

/// Axum handler for `GET /cloudmap`.
///
/// Local fast-path when [`AppState::cloudmap`] is set; otherwise
/// proxies to the Python backend.
pub async fn handle_cloudmap(
    State(state): State<AppState>,
    Query(params): Query<CloudMapParams>,
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

async fn build_response(cm: &CloudMapState, params: &CloudMapParams) -> Result<Value, LocalError> {
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
    let follow = if key.is_some() { params.follow } else { 0 };
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
    Ok(Value::Array(vec![primary, followed]))
}

/// Group records by section (`record.path`) and emit a CloudMap-shaped
/// object: `{ section: { key: json } }`.
fn records_to_document(records: Vec<Record>) -> Value {
    let mut sections: BTreeMap<&'static str, Map<String, Value>> = BTreeMap::new();
    for r in records {
        let Some(section) = section_for_path(&r.path) else {
            continue;
        };
        sections.entry(section).or_default().insert(r.key, r.json);
    }
    let mut out = Map::new();
    for (section, entries) in sections {
        out.insert(section.to_string(), Value::Object(entries));
    }
    Value::Object(out)
}
