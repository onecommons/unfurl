// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Axum route handlers for the unfurl server proxy.

// Several handlers in this module return `Result<TypedSuccess,
// axum::response::Response>` so the success body type stays linked
// to the generated OpenAPI schema in the function signature, while
// the `Err` arm carries pre-built raw responses (proxy passthrough,
// 304, error envelopes). axum's `Response` is ~128 bytes which trips
// the `result_large_err` lint on every such return type — boxing it
// would obscure the signature without changing wire behaviour, so we
// allow the lint at module scope instead.
#![allow(clippy::result_large_err)]

use axum::{
    body::Body,
    extract::{Query, Request, State},
    http::{header, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use serde_json::{json, Value as JsonValue};
use std::collections::HashMap;

use crate::cache;
use crate::proxy;
use crate::queue::{self, ExportQueueCheck, QueueIdResult, QueueItem};
use crate::unfurl_types;
use crate::AppState;

// ---------------------------------------------------------------------------
// Cache-aware GET handlers
// ---------------------------------------------------------------------------

/// A query parameter's value, or `None` when it isn't set to one.
fn non_empty(value: Option<&str>) -> Option<&str> {
    value.map(str::trim).filter(|v| !v.is_empty())
}

/// Build the Redis cache key for a `/export` request from its typed
/// query, or `None` when the request doesn't name what the key is made of.
///
/// Python derives its key from `(project_id, branch, file_path, format)`,
/// and — see `_export` in `unfurl/server/serve.py` — puts the branch the
/// request asked for into it **verbatim**, `HEAD` included. It resolves a
/// branch only when the request names none, and then not necessarily to
/// `main`: `get_latest_tag_or_default_branch` can return a package's
/// latest version tag, or a local project's checked-out branch.
///
/// So there is nothing to guess with. A named branch is keyed as given;
/// a request that names none can't be keyed at all, and `None` sends it
/// to Python to resolve and consult its own cache under the right key.
/// Same for a request with no `auth_project`: no project to scope an
/// entry to.
fn export_cache_key(prefix: &str, params: &unfurl_types::GetExportRequestQuery) -> Option<String> {
    let project_id = non_empty(params.auth_project.as_deref())?;
    let branch = non_empty(params.branch.as_deref())?;
    let format = params.format.as_ref();
    let deployment_path = params.deployment_path.as_deref();

    use unfurl_types::GetExportRequestQueryFormat as Fmt;
    let file_path = match format {
        Some(Fmt::Blueprint) => "ensemble-template.yaml".to_string(),
        Some(Fmt::Environments) => "unfurl.yaml".to_string(),
        // deployment (default): Python's _get_filepath returns
        // "ensemble/ensemble.yaml" by default.
        _ => match deployment_path {
            None | Some("") => "ensemble/ensemble.yaml".to_string(),
            Some(p) if p.ends_with(".yaml") => p.to_string(),
            Some(p) => format!("{}/ensemble.yaml", p),
        },
    };

    let key_suffix = match format {
        Some(Fmt::Blueprint) => "blueprint".to_string(),
        Some(Fmt::Environments) => {
            if let Some(env) = params.environment.as_deref() {
                format!("environments+{}", env)
            } else {
                "environments".to_string()
            }
        }
        _ => "deployment".to_string(),
    };

    Some(format!(
        "{}{}:{}:{}:{}",
        prefix, project_id, branch, file_path, key_suffix
    ))
}

/// Build the Redis cache key for a `/types` request from its typed
/// query, or `None` when the request names no project or no branch —
/// see [`export_cache_key`] for why those aren't guessed at.
fn types_cache_key(prefix: &str, params: &unfurl_types::GetTypesRequestQuery) -> Option<String> {
    let project_id = non_empty(params.auth_project.as_deref())?;
    let branch = non_empty(params.branch.as_deref())?;
    let file = params.file.as_deref().unwrap_or("dummy-ensemble.yaml");

    Some(format!(
        "{}{}:{}:{}:blueprint+types",
        prefix, project_id, branch, file
    ))
}

/// Parse query string into a HashMap.
fn parse_query(uri: &axum::http::Uri) -> HashMap<String, String> {
    uri.query()
        .map(|q| {
            url::form_urlencoded::parse(q.as_bytes())
                .map(
                    |(k, v): (std::borrow::Cow<'_, str>, std::borrow::Cow<'_, str>)| {
                        (k.into_owned(), v.into_owned())
                    },
                )
                .collect()
        })
        .unwrap_or_default()
}

/// What `handle_cached_get` produced. Keeps the typed-vs-raw split
/// out of [`handle_cached_get`] so per-route handlers can map each
/// variant to their own typed response enum.
enum CacheOutcome {
    /// Cache hit + the payload deserialized cleanly into the strict
    /// [`unfurl_types::ExportResponse`]. The accompanying `String` is the
    /// `Etag` header to set. Boxed because `ExportResponse` is large
    /// (~900B) and would otherwise dominate the enum's discriminant
    /// size.
    Typed(Box<unfurl_types::ExportResponse>, String),
    /// Cache hit + `If-None-Match` matched the stored etag.
    NotModified,
    /// Cache hit but the typed deserialize failed (legacy entry,
    /// schema drift) — pass the raw JSON through with the etag.
    RawJson(JsonValue, String),
    /// Cache miss, no Redis, or any other reason to fall through to
    /// the Python backend. The contained [`Response`] is the proxy's
    /// raw response.
    Proxied(Response),
}

/// Shared logic for cache-aware GET handlers.
///
/// `key` is what [`export_cache_key`] or [`types_cache_key`] made of this
/// request, and is [`None`] when the request named no project or no branch
/// — nothing to look up without guessing. Handling that here keeps the one
/// thing to do about it (proxy) in a single place.
///
/// Tries the Redis cache with that key; on a hit, honours `If-None-Match`
/// (→ 304) or returns 200 with the cached body and an `Etag` header.  On a
/// miss (or when Redis is not configured, or there is no key) falls through
/// to the Python backend.
///
/// On a cache hit we attempt to deserialize the stored payload into the
/// strict [`unfurl_types::ExportResponse`] type so the wire response is
/// typed (matches the `ExportResponse` schema declared in
/// `openapi.json` for both `/export` and `/types`). If deserialization
/// fails — e.g. a legacy entry whose shape doesn't match the current
/// schema — the [`CacheOutcome::RawJson`] variant lets the caller
/// pass the raw [`JsonValue`] through. `ExportResponse` carries a
/// `#[serde(flatten)] additional_properties` catch-all so unknown
/// top-level keys don't fail the round-trip.
async fn handle_cached_get(
    state: AppState,
    req: Request,
    key: Option<String>,
    latest_commit: Option<String>,
    branch: Option<String>,
) -> CacheOutcome {
    match key {
        // Nothing to look up: the request names no project, or no particular
        // branch, so every candidate key would be a guess. Python resolves
        // both and consults its own cache.
        None => tracing::debug!("request carries no cache key, proxying to backend"),
        Some(key) => {
            if let Some(ref redis) = state.redis {
                let mut conn = redis.clone();
                if let Some((json_val, etag)) = cache::try_cache(
                    &mut conn,
                    &key,
                    latest_commit.as_deref(),
                    state.config.redis_timeout_secs,
                    &state.config.package_digest,
                )
                .await
                {
                    // Return 304 Not Modified if the client already has this version.
                    let if_none_match = req
                        .headers()
                        .get(header::IF_NONE_MATCH)
                        .and_then(|v| v.to_str().ok());
                    if if_none_match == Some(etag.as_str()) {
                        tracing::info!("cache hit, etag matched: {}", key);
                        return CacheOutcome::NotModified;
                    }
                    tracing::info!(
                        "cache hit {} if_none_match={:?} setting etag={}",
                        key,
                        if_none_match,
                        etag
                    );
                    return match serde_json::from_value::<unfurl_types::ExportResponse>(
                        json_val.clone(),
                    ) {
                        Ok(mut typed) => {
                            // we don't know the `default_branch`, so we don't set it here.
                            typed.latest_commit = latest_commit;
                            typed.branch = branch;
                            CacheOutcome::Typed(Box::new(typed), etag)
                        }
                        Err(e) => {
                            tracing::debug!(
                                "cache hit {key}: typed ExportResponse deserialize failed \
                                 ({e}); falling back to raw JSON passthrough"
                            );
                            CacheOutcome::RawJson(json_val, etag)
                        }
                    };
                }
                tracing::info!("cache miss, proxying to backend: {}", key);
            } else {
                tracing::debug!("no Redis configured, skipping cache for: {}", key);
            }
        }
    }
    CacheOutcome::Proxied(
        proxy::forward(
            &state.client,
            &state.config.backend_url(),
            req,
            state.config.max_body_bytes,
        )
        .await,
    )
}

/// Build the final HTTP response for a [`CacheOutcome::Typed`] /
/// [`CacheOutcome::RawJson`] variant, attaching the `Etag` header.
fn with_etag(mut response: Response, etag: &str) -> Response {
    if let Ok(hv) = HeaderValue::from_str(etag) {
        response.headers_mut().insert(header::ETAG, hv);
    }
    response
}

/// GET /export -- try Redis cache first, fall back to proxying.
///
/// The query string is parsed via the generated
/// [`unfurl_types::GetExportRequestQuery`] extractor so the schema stays
/// in lockstep with `unfurl/server/openapi.json`. A malformed
/// `format` value short-circuits with a 422 (via [`ValidatedQuery`])
/// to match the Python backend's APIFlask response.
///
/// Success body type is [`Json<unfurl_types::ExportResponse>`]. 304, raw-fallback, proxy passthrough — and the
/// typed cache hit (which needs an `Etag` header) — flow through the
/// `Err` branch as a raw [`Response`]. axum can't compose extra
/// headers onto a bare `Json<T>` Ok value without a tuple wrapper.
pub async fn handle_export(
    State(state): State<AppState>,
    ValidatedQuery(params): ValidatedQuery<unfurl_types::GetExportRequestQuery>,
    req: Request,
) -> Result<Json<unfurl_types::ExportResponse>, Response> {
    let resolved = match resolve_queued_request(
        &state,
        params.queueid,
        params.latest_commit.as_deref(),
        params.auth_project.as_deref(),
    )
    .await
    {
        Ok(r) => r,
        Err(resp) => return Err(resp),
    };
    // If the queue check produced a new commit, rewrite the request URI
    // so the proxied backend call uses the new commit and no longer
    // carries the consumed `queueid`. Otherwise pass the request
    // through with its original `latest_commit` (or `None`).
    let (latest_commit, req) = if let Some(new_commit) = resolved {
        let new_uri = rewrite_uri_for_resolved_commit(req.uri(), &new_commit);
        let (mut parts, body) = req.into_parts();
        parts.uri = new_uri;
        (Some(new_commit), Request::from_parts(parts, body))
    } else {
        (params.latest_commit.clone(), req)
    };
    // `include_all_deployments=1` (or `=true`) requires composing the
    // primary environments summary with one cache entry per deployment
    // (see `_export` in serve.py) so bypass the cache and let Python handle this.
    // Match Python's truthiness check on `request.args.get("include_all_deployments")`
    if params
        .include_all_deployments
        .as_deref()
        .is_some_and(|s| !s.is_empty())
    {
        return Err(proxy::forward(
            &state.client,
            &state.config.backend_url(),
            req,
            state.config.max_body_bytes,
        )
        .await);
    }
    let key = export_cache_key(&state.config.cache_key_prefix, &params);
    let branch = params.branch.clone();
    match handle_cached_get(state, req, key, latest_commit, branch).await {
        CacheOutcome::Typed(typed, etag) => Err(with_etag(Json(*typed).into_response(), &etag)),
        CacheOutcome::NotModified => Err(StatusCode::NOT_MODIFIED.into_response()),
        CacheOutcome::RawJson(val, etag) => Err(with_etag(Json(val).into_response(), &etag)),
        CacheOutcome::Proxied(resp) => Err(resp),
    }
}

/// How often to re-check the queue while waiting for an in-flight
/// batch to commit on the `/export` / `/types` wait-and-proxy path.
const QUEUE_WAIT_POLL_INTERVAL: std::time::Duration = std::time::Duration::from_millis(100);

/// If a read request (`/export` or `/types`) carries a `queueid > 0`
/// and Redis is available, consult `queue:{project}:{latest_commit}`
/// and either:
///   * return `Ok(Some(new_commit))` — the queue key already records a
///     commit that covers the client's queueid, so the caller should
///     rewrite the URI to use it and proxy with fresh data;
///   * kick the batch worker (so it drains the project's pending list
///     immediately instead of waiting out the rest of its window) and
///     poll the queue key until it advances, then return the new commit;
///   * give up with **503 RETRY** after `proxy_timeout_secs` (matches
///     the worker's `batch_lock` TTL, so anything longer is
///     stuck-worker territory anyway).
///
/// Returns `Ok(None)` when there's nothing to wait on: no queueid (or
/// `queueid == 0`), or no Redis configured. The caller passes the
/// original request through unchanged.  Redis errors surface as
/// **500**.
async fn resolve_queued_request(
    state: &AppState,
    queueid: Option<i64>,
    latest_commit: Option<&str>,
    auth_project: Option<&str>,
) -> Result<Option<String>, Response> {
    let Some(request_queueid) = queueid.filter(|q| *q > 0) else {
        return Ok(None);
    };
    let Some(ref redis) = state.redis else {
        return Ok(None);
    };
    let project_id = auth_project.unwrap_or("");
    let lc = latest_commit.unwrap_or("");
    let mut conn = redis.clone();

    // Fast path: queue key already records a commit covering the client.
    match queue::check_export_queue(&mut conn, &state.config, project_id, lc, request_queueid).await
    {
        Ok(ExportQueueCheck::UseNewCommit(new_commit)) => return Ok(Some(new_commit)),
        Ok(ExportQueueCheck::Retry) => {} // fall through to kick + wait
        Err(e) => {
            tracing::error!("check_export_queue Redis error: {}", e);
            return Err((StatusCode::INTERNAL_SERVER_ERROR, "queue error").into_response());
        }
    }

    // Tell the batch worker to drain this project's queue on its next
    // poll instead of running out the remainder of the batch window.
    if let Err(e) = queue::kick_worker(&mut conn, &state.config, project_id).await {
        tracing::error!("kick_worker Redis error: {}", e);
        return Err((StatusCode::INTERNAL_SERVER_ERROR, "queue error").into_response());
    }

    // Poll until the queue key advances or we hit the wait budget.
    let deadline =
        std::time::Instant::now() + std::time::Duration::from_secs(state.config.proxy_timeout_secs);
    loop {
        tokio::time::sleep(QUEUE_WAIT_POLL_INTERVAL).await;
        match queue::check_export_queue(&mut conn, &state.config, project_id, lc, request_queueid)
            .await
        {
            Ok(ExportQueueCheck::UseNewCommit(new_commit)) => return Ok(Some(new_commit)),
            Ok(ExportQueueCheck::Retry) => {
                if std::time::Instant::now() >= deadline {
                    tracing::warn!(
                        "queue wait timed out: project={} queueid={}",
                        project_id,
                        request_queueid
                    );
                    return Err(queue_retry_response());
                }
            }
            Err(e) => {
                tracing::error!("check_export_queue Redis error during wait: {}", e);
                return Err((StatusCode::INTERNAL_SERVER_ERROR, "queue error").into_response());
            }
        }
    }
}

/// Rewrite a request URI's query string to substitute `latest_commit`
/// with `new_commit` (inserting it if it wasn't present) and drop the
/// `queueid` param entirely.  Called after the queue check resolves a
/// queued export request to a committed commit so the proxied backend
/// request reflects the new revision and no longer carries the
/// consumed `queueid`.
fn rewrite_uri_for_resolved_commit(uri: &axum::http::Uri, new_commit: &str) -> axum::http::Uri {
    let mut pairs: Vec<(String, String)> = Vec::new();
    let mut latest_commit_seen = false;
    if let Some(q) = uri.query() {
        for (k, v) in url::form_urlencoded::parse(q.as_bytes()) {
            match k.as_ref() {
                "queueid" => continue,
                "latest_commit" => {
                    latest_commit_seen = true;
                    pairs.push((k.into_owned(), new_commit.to_string()));
                }
                _ => pairs.push((k.into_owned(), v.into_owned())),
            }
        }
    }
    if !latest_commit_seen {
        pairs.push(("latest_commit".into(), new_commit.to_string()));
    }
    let new_query = url::form_urlencoded::Serializer::new(String::new())
        .extend_pairs(&pairs)
        .finish();
    let path = uri.path();
    let path_and_query = if new_query.is_empty() {
        path.to_string()
    } else {
        format!("{}?{}", path, new_query)
    };
    let mut builder = axum::http::Uri::builder();
    if let Some(scheme) = uri.scheme() {
        builder = builder.scheme(scheme.clone());
    }
    if let Some(auth) = uri.authority() {
        builder = builder.authority(auth.clone());
    }
    builder
        .path_and_query(path_and_query)
        .build()
        .unwrap_or_else(|_| uri.clone())
}

/// Build the 503 response returned when an `/export` request's queued
/// write hasn't been committed yet. Includes a `Retry-After: 1` header
/// so clients with a generic retry policy back off briefly instead of
/// hammering the proxy.
fn queue_retry_response() -> Response {
    let body = Json(json!({
        "error": "RETRY",
        "message": "queued update not yet committed",
    }));
    (
        StatusCode::SERVICE_UNAVAILABLE,
        [(header::RETRY_AFTER, "1")],
        body,
    )
        .into_response()
}

/// GET /types -- try Redis cache first, fall back to proxying.
///
/// The query string is parsed via the generated
/// [`unfurl_types::GetTypesRequestQuery`] extractor. The success and 304
/// paths are produced as [`unfurl_types::GetTypesResponse`] variants
/// (`Ok` and `NotModified` respectively); raw-fallback and proxy
/// passthrough flow through as `Response` via `Result<_, Response>`'s
/// `Err` branch.
pub async fn handle_types(
    State(state): State<AppState>,
    ValidatedQuery(params): ValidatedQuery<unfurl_types::GetTypesRequestQuery>,
    req: Request,
) -> Result<unfurl_types::GetTypesResponse, Response> {
    let resolved = match resolve_queued_request(
        &state,
        params.queueid,
        params.latest_commit.as_deref(),
        params.auth_project.as_deref(),
    )
    .await
    {
        Ok(r) => r,
        Err(resp) => return Err(resp),
    };
    let (latest_commit, req) = if let Some(new_commit) = resolved {
        let new_uri = rewrite_uri_for_resolved_commit(req.uri(), &new_commit);
        let (mut parts, body) = req.into_parts();
        parts.uri = new_uri;
        (Some(new_commit), Request::from_parts(parts, body))
    } else {
        (params.latest_commit.clone(), req)
    };
    let key = types_cache_key(&state.config.cache_key_prefix, &params);
    let branch = params.branch.clone();
    match handle_cached_get(state, req, key, latest_commit, branch).await {
        CacheOutcome::Typed(typed, etag) => {
            // The typed enum's `IntoResponse` impl handles status +
            // body. We still want the `Etag` header on the wire, so
            // route through `RawJson`'s helper to splice it on.
            Err(with_etag(
                unfurl_types::GetTypesResponse::Ok(*typed).into_response(),
                &etag,
            ))
        }
        CacheOutcome::NotModified => Ok(unfurl_types::GetTypesResponse::NotModified),
        CacheOutcome::RawJson(val, etag) => Err(with_etag(Json(val).into_response(), &etag)),
        CacheOutcome::Proxied(resp) => Err(resp),
    }
}

// ---------------------------------------------------------------------------
// Write / queue handlers
// ---------------------------------------------------------------------------

/// Filter the upstream request's headers down to the set we want to
/// forward to the Python backend (or stash on a queued item).
fn filter_forward_headers(headers: &axum::http::HeaderMap) -> HashMap<String, String> {
    headers
        .iter()
        .filter_map(|(k, v)| {
            let name = k.as_str();
            if name == "host"
                || name == "transfer-encoding"
                || name == "connection"
                || name == "content-length"
                || name == "content-type"
            {
                return None;
            }
            v.to_str()
                .ok()
                .map(|val| (name.to_string(), val.to_string()))
        })
        .collect()
}

/// Core POST-write logic, shared by [`handle_patch_ensemble`] and
/// [`handle_patch_environment`].
///
/// Validation happens in the typed wrappers (axum extractors); this
/// function takes the raw client bytes, parses them once for queue
/// inspection (`queueid` / `latest_commit`) and forwards them to the
/// Python backend on the proxy fall-through path. Bytes flow through
/// unchanged — we never re-serialize a validated body.
async fn handle_write(
    state: AppState,
    endpoint: String,
    headers_map: HashMap<String, String>,
    body_bytes: axum::body::Bytes,
) -> Response {
    // Extract project_id from the endpoint's query string for
    // per-project batching.
    let endpoint_uri: axum::http::Uri = endpoint.parse().unwrap_or_default();
    let params = parse_query(&endpoint_uri);
    let project_id = params.get("auth_project").cloned().unwrap_or_default();

    let body: JsonValue = serde_json::from_slice(&body_bytes).unwrap_or(JsonValue::Null);

    // Every write request must declare the commit it is patching against.
    let latest_commit = body
        .get("latest_commit")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if latest_commit.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": "BAD_REQUEST",
                "message": "missing or empty latest_commit",
            })),
        )
            .into_response();
    }

    // ...and which branch it goes to. Python rejects a write that names none
    // rather than committing to `main`, so check it here too: a queued write is
    // answered before it is applied, and the client would never see that error.
    if body
        .get("branch")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .is_empty()
    {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": "BAD_REQUEST",
                "message": "missing or empty branch",
            })),
        )
            .into_response();
    }

    // Use the Redis queue only when Redis is available AND the request body
    // contains a non-null "queueid" field.  When queueid is absent the
    // write request is proxied synchronously to the Python backend.
    let client_queueid = body.get("queueid").and_then(|v| v.as_i64());
    if let Some(queueid) = client_queueid {
        if let Some(ref redis) = state.redis {
            // Atomically validate and increment the queueid.
            let mut conn = redis.clone();
            let qid_result = match queue::inc_queueid(
                &mut conn,
                &state.config,
                &project_id,
                latest_commit,
                queueid,
            )
            .await
            {
                Ok(r) => r,
                Err(e) => {
                    tracing::error!("inc_queueid Redis error: {}", e);
                    return (StatusCode::INTERNAL_SERVER_ERROR, "queue error").into_response();
                }
            };

            match qid_result {
                QueueIdResult::Conflict => {
                    tracing::info!(
                        "queueid conflict: project={} commit={} queueid={}",
                        project_id,
                        latest_commit,
                        queueid
                    );
                    return (
                        StatusCode::CONFLICT,
                        Json(json!({"error": "CONFLICT", "message": "stale queueid"})),
                    )
                        .into_response();
                }
                QueueIdResult::Ok { new_queueid } => {
                    let mut updated_body = body;
                    updated_body["queueid"] = serde_json::json!(new_queueid);
                    let item = QueueItem {
                        endpoint: endpoint.clone(),
                        body: updated_body,
                        headers: headers_map,
                    };
                    if let Err(e) =
                        queue::enqueue(&mut conn, &state.config, &project_id, &item).await
                    {
                        tracing::error!("failed to enqueue: {}", e);
                        return (StatusCode::INTERNAL_SERVER_ERROR, "queue error").into_response();
                    }
                    return Json(json!({"queueid": new_queueid})).into_response();
                }
                QueueIdResult::NewCommit {
                    new_commit,
                    new_queueid,
                } => {
                    // Update the body's latest_commit and queueid before enqueuing
                    // so the worker sends the correct values to batch_patch.
                    let mut updated_body = body;
                    updated_body["latest_commit"] = serde_json::Value::String(new_commit.clone());
                    updated_body["queueid"] = serde_json::json!(new_queueid);
                    let item = QueueItem {
                        endpoint: endpoint.clone(),
                        body: updated_body,
                        headers: headers_map,
                    };
                    if let Err(e) =
                        queue::enqueue(&mut conn, &state.config, &project_id, &item).await
                    {
                        tracing::error!("failed to enqueue: {}", e);
                        return (StatusCode::INTERNAL_SERVER_ERROR, "queue error").into_response();
                    }
                    return Json(json!({"queueid": new_queueid, "commit": new_commit}))
                        .into_response();
                }
            }
        }
    }

    // No queueid → the client wants a synchronous write.  If Redis is
    // available, refuse to proxy when there are still pending writes for
    // `(project_id, branch)` (either queued or being applied by
    // `run_worker`): letting a sync request race with an in-flight batch
    // would let the two writes commit against the same parent commit
    // and clobber each other.
    if let Some(ref redis) = state.redis {
        // non-empty: validated above, so there is no default to fall back on
        let branch = body.get("branch").and_then(|v| v.as_str()).unwrap_or("");
        let mut conn = redis.clone();
        match queue::has_pending_writes(&mut conn, &state.config, &project_id, branch).await {
            Ok(true) => {
                tracing::info!(
                    "rejecting sync write: pending batch for project={} branch={}",
                    project_id,
                    branch
                );
                return (
                    StatusCode::CONFLICT,
                    Json(json!({
                        "error": "CONFLICT",
                        "message": "pending batched writes for this project/branch",
                    })),
                )
                    .into_response();
            }
            Ok(false) => {}
            Err(e) => {
                tracing::error!("has_pending_writes Redis error: {}", e);
                return (StatusCode::INTERNAL_SERVER_ERROR, "queue error").into_response();
            }
        }
    }

    // Proxy directly to Python backend (Redis unavailable, or queue disabled).
    // Preserve full path + query string from the original request.
    let path_and_query = ensure_leading_slash(&endpoint);
    let proxy_req = Request::builder()
        .method(axum::http::Method::POST)
        .uri(format!("{}{}", state.config.backend_url(), path_and_query))
        .body(Body::from(body_bytes))
        .unwrap();
    proxy::forward(
        &state.client,
        &state.config.backend_url(),
        proxy_req,
        state.config.max_body_bytes,
    )
    .await
}

/// We extract raw `Bytes` (rather than `Json<T>`) so the original
/// client bytes flow straight through to `handle_write` for
/// proxy forwarding; the body is validated as
/// [`unfurl_types::`PatchEnsembleBody`]. The
/// success body type on the `Ok` arm is dead but documents the
/// OpenAPI `PatchResponse` schema; the actual response (queue
/// envelope or proxied Python response) flows through `Err`.
pub async fn handle_patch_ensemble(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
    uri: axum::http::Uri,
    body_bytes: axum::body::Bytes,
) -> Result<Json<unfurl_types::PatchResponse>, Response> {
    validate_body::<unfurl_types::PatchEnsembleBody>(&body_bytes)?;
    let endpoint = uri
        .path_and_query()
        .map(|pq| pq.as_str().to_string())
        .unwrap_or_else(|| uri.path().to_string());
    let headers_map = filter_forward_headers(&headers);
    Err(handle_write(state, endpoint, headers_map, body_bytes).await)
}

/// `POST /delete_deployment`, `/update_environment`,
/// `/delete_environment` — typed wrapper around `handle_write`.
/// Validates body as [`unfurl_types::PatchEnvironmentBody`]. Response:
/// [`unfurl_types::PatchResponse`].
pub async fn handle_patch_environment(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
    uri: axum::http::Uri,
    body_bytes: axum::body::Bytes,
) -> Result<Json<unfurl_types::PatchResponse>, Response> {
    validate_body::<unfurl_types::PatchEnvironmentBody>(&body_bytes)?;
    let endpoint = uri
        .path_and_query()
        .map(|pq| pq.as_str().to_string())
        .unwrap_or_else(|| uri.path().to_string());
    let headers_map = filter_forward_headers(&headers);
    Err(handle_write(state, endpoint, headers_map, body_bytes).await)
}

/// Validate raw body bytes as JSON shape `T`, returning a 422
/// response on failure so callers can early-return with `?`.
fn validate_body<T: serde::de::DeserializeOwned>(
    body_bytes: &axum::body::Bytes,
) -> Result<(), Response> {
    serde_json::from_slice::<T>(body_bytes)
        .map(|_| ())
        .map_err(|e| {
            (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"error": "validation_error", "detail": e.to_string()})),
            )
                .into_response()
        })
}

/// Drop-in replacement for [`axum::extract::Query`] that maps a
/// deserialization failure to **422 Unprocessable Entity** (matching
/// APIFlask's convention on the Python backend) instead of axum's
/// default **400 Bad Request**. Use this whenever the query schema is
/// part of the OpenAPI contract — the Python and Rust servers must
/// agree on the status code so clients can pick a single error path.
pub struct ValidatedQuery<T>(pub T);

impl<T, S> axum::extract::FromRequestParts<S> for ValidatedQuery<T>
where
    T: serde::de::DeserializeOwned + Send,
    S: Send + Sync,
{
    type Rejection = Response;

    async fn from_request_parts(
        parts: &mut axum::http::request::Parts,
        state: &S,
    ) -> Result<Self, Self::Rejection> {
        match Query::<T>::from_request_parts(parts, state).await {
            Ok(Query(value)) => Ok(Self(value)),
            Err(rej) => Err((
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"message": rej.to_string()})),
            )
                .into_response()),
        }
    }
}

/// [`ValidatedQuery`] backed by [`axum_extra::extract::Query`], whose
/// `serde_html_form` deserializer collects *repeated* query keys into
/// `Vec` fields — `serde_urlencoded`, behind [`axum::extract::Query`],
/// errors on them. Use it for handlers with repeatable parameters
/// (e.g. `facet=` on `GET /cloudmap/facets`); it is a separate type
/// rather than a swap inside [`ValidatedQuery`] so the extraction
/// semantics of every existing endpoint stay untouched. Same 422
/// mapping as [`ValidatedQuery`].
pub struct ValidatedRepeatedQuery<T>(pub T);

impl<T, S> axum::extract::FromRequestParts<S> for ValidatedRepeatedQuery<T>
where
    T: serde::de::DeserializeOwned + Send,
    S: Send + Sync,
{
    type Rejection = Response;

    async fn from_request_parts(
        parts: &mut axum::http::request::Parts,
        state: &S,
    ) -> Result<Self, Self::Rejection> {
        match axum_extra::extract::Query::<T>::from_request_parts(parts, state).await {
            Ok(axum_extra::extract::Query(value)) => Ok(Self(value)),
            Err(rej) => Err((
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"message": rej.to_string()})),
            )
                .into_response()),
        }
    }
}

/// Drop-in replacement for [`axum::Json`] as a body extractor that
/// maps a deserialization failure to **422 Unprocessable Entity**
/// (matching APIFlask's convention on the Python backend) instead of
/// axum's default **400 Bad Request**. Use this on every POST/PATCH
/// handler whose body shape is part of the OpenAPI contract so the
/// Python and Rust servers return identical statuses on schema
/// violations.
pub struct ValidatedJson<T>(pub T);

impl<T, S> axum::extract::FromRequest<S> for ValidatedJson<T>
where
    T: serde::de::DeserializeOwned,
    S: Send + Sync,
{
    type Rejection = Response;

    async fn from_request(req: axum::extract::Request, state: &S) -> Result<Self, Self::Rejection> {
        match Json::<T>::from_request(req, state).await {
            Ok(Json(value)) => Ok(Self(value)),
            Err(rej) => Err((
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"message": rej.to_string()})),
            )
                .into_response()),
        }
    }
}

// ---------------------------------------------------------------------------
// Catch-all pass-through
// ---------------------------------------------------------------------------

/// All other endpoints -- proxy transparently to Python.
pub async fn handle_fallback(State(state): State<AppState>, req: Request) -> Response {
    tracing::trace!("fallback handler: {} {}", req.method(), req.uri());
    proxy::forward(
        &state.client,
        &state.config.backend_url(),
        req,
        state.config.max_body_bytes,
    )
    .await
}

/// Helper: ensure path starts with `/`.
fn ensure_leading_slash(path: &str) -> String {
    if path.starts_with('/') {
        path.to_string()
    } else {
        format!("/{}", path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse_query_pairs(uri: &axum::http::Uri) -> Vec<(String, String)> {
        uri.query()
            .map(|q| {
                url::form_urlencoded::parse(q.as_bytes())
                    .into_owned()
                    .collect()
            })
            .unwrap_or_default()
    }

    fn export_query(
        auth_project: Option<&str>,
        branch: Option<&str>,
    ) -> unfurl_types::GetExportRequestQuery {
        unfurl_types::GetExportRequestQuery {
            auth_project: auth_project.map(str::to_string),
            branch: branch.map(str::to_string),
            ..Default::default()
        }
    }

    fn types_query(
        auth_project: Option<&str>,
        branch: Option<&str>,
    ) -> unfurl_types::GetTypesRequestQuery {
        unfurl_types::GetTypesRequestQuery {
            auth_project: auth_project.map(str::to_string),
            branch: branch.map(str::to_string),
            // `ValidatedQuery` deserializes an absent parameter to `None`;
            // the schema default that `Default::default()` fills in (`""`)
            // is not what a real request without `file` produces.
            file: None,
            ..Default::default()
        }
    }

    #[test]
    fn cache_keys_match_the_key_python_stores() {
        // The point of building the key here is to read entries Python
        // wrote, so the spelling is a shared contract: prefix, project,
        // branch, file path, suffix.
        assert_eq!(
            export_cache_key("test::", &export_query(Some("acme/prod"), Some("main"))).as_deref(),
            Some("test::acme/prod:main:ensemble/ensemble.yaml:deployment")
        );
        assert_eq!(
            types_cache_key("test::", &types_query(Some("acme/prod"), Some("dev"))).as_deref(),
            Some("test::acme/prod:dev:dummy-ensemble.yaml:blueprint+types")
        );
    }

    #[test]
    fn head_is_keyed_verbatim() {
        // Python keys a requested branch verbatim, so `HEAD` reads exactly
        // the entry Python wrote for the same request. The mapping this
        // replaced (`HEAD` -> `main`) read a different branch's entry.
        assert_eq!(
            export_cache_key("test::", &export_query(Some("acme/prod"), Some("HEAD"))).as_deref(),
            Some("test::acme/prod:HEAD:ensemble/ensemble.yaml:deployment")
        );
    }

    #[test]
    fn cache_key_is_none_without_a_named_branch() {
        // No branch at all is the case that can't be keyed: Python resolves
        // it to the repository's default branch -- or a package's latest
        // version tag -- neither of which is knowable here.
        for branch in [None, Some(""), Some("  ")] {
            assert_eq!(
                export_cache_key("test::", &export_query(Some("acme/prod"), branch)),
                None,
                "branch {branch:?}"
            );
            assert_eq!(
                types_cache_key("test::", &types_query(Some("acme/prod"), branch)),
                None,
                "branch {branch:?}"
            );
        }
    }

    #[test]
    fn cache_key_is_none_without_a_named_project() {
        for project in [None, Some(""), Some("  ")] {
            assert_eq!(
                export_cache_key("test::", &export_query(project, Some("main"))),
                None,
                "auth_project {project:?}"
            );
            assert_eq!(
                types_cache_key("test::", &types_query(project, Some("main"))),
                None,
                "auth_project {project:?}"
            );
        }
    }

    #[test]
    fn rewrite_uri_replaces_latest_commit_and_drops_queueid() {
        let uri: axum::http::Uri =
            "/export?auth_project=foo&latest_commit=old&queueid=3&format=blueprint"
                .parse()
                .unwrap();
        let new = rewrite_uri_for_resolved_commit(&uri, "new_commit_sha");
        assert_eq!(new.path(), "/export");

        let pairs = parse_query_pairs(&new);
        assert!(!pairs.iter().any(|(k, _)| k == "queueid"));
        let lc: Vec<_> = pairs.iter().filter(|(k, _)| k == "latest_commit").collect();
        assert_eq!(lc.len(), 1);
        assert_eq!(lc[0].1, "new_commit_sha");

        // Other params are preserved.
        assert!(pairs.iter().any(|(k, v)| k == "auth_project" && v == "foo"));
        assert!(pairs.iter().any(|(k, v)| k == "format" && v == "blueprint"));
    }

    #[test]
    fn rewrite_uri_inserts_latest_commit_when_absent() {
        let uri: axum::http::Uri = "/export?auth_project=foo&queueid=3".parse().unwrap();
        let new = rewrite_uri_for_resolved_commit(&uri, "new_commit");

        let pairs = parse_query_pairs(&new);
        assert!(!pairs.iter().any(|(k, _)| k == "queueid"));
        assert!(pairs
            .iter()
            .any(|(k, v)| k == "latest_commit" && v == "new_commit"));
    }

    #[test]
    fn rewrite_uri_handles_empty_query() {
        let uri: axum::http::Uri = "/export".parse().unwrap();
        let new = rewrite_uri_for_resolved_commit(&uri, "new_commit");
        assert_eq!(new.path(), "/export");

        let pairs = parse_query_pairs(&new);
        assert_eq!(pairs.len(), 1);
        assert_eq!(pairs[0], ("latest_commit".into(), "new_commit".into()));
    }

    #[test]
    fn rewrite_uri_drops_multiple_queueid_occurrences() {
        // Defensive: someone (or a buggy client) sends queueid twice —
        // both should be removed.
        let uri: axum::http::Uri = "/export?queueid=1&latest_commit=a&queueid=2"
            .parse()
            .unwrap();
        let new = rewrite_uri_for_resolved_commit(&uri, "b");
        let pairs = parse_query_pairs(&new);
        assert!(!pairs.iter().any(|(k, _)| k == "queueid"));
        assert_eq!(
            pairs
                .iter()
                .filter(|(k, _)| k == "latest_commit")
                .collect::<Vec<_>>(),
            vec![&("latest_commit".to_string(), "b".to_string())]
        );
    }
}
