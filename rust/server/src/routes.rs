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
use crate::queue::{self, QueueIdResult, QueueItem};
use crate::unfurl_types;
use crate::AppState;

// ---------------------------------------------------------------------------
// Cache-aware GET handlers
// ---------------------------------------------------------------------------

/// Build the Redis cache key for a `/export` request from its typed
/// query.
fn export_cache_key(prefix: &str, params: &unfurl_types::GetExportRequestQuery) -> String {
    let project_id = params.auth_project.as_deref().unwrap_or("");
    // Python resolves missing/HEAD branch to the repo's default branch (typically "main").
    // Use "main" here to match the key Python stores.
    let branch = match params.branch.as_deref() {
        Some("HEAD") | None => "main",
        Some(b) => b,
    };
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

    format!(
        "{}{}:{}:{}:{}",
        prefix, project_id, branch, file_path, key_suffix
    )
}

/// Build the Redis cache key for a `/types` request from its typed
/// query.
fn types_cache_key(prefix: &str, params: &unfurl_types::GetTypesRequestQuery) -> String {
    let project_id = params.auth_project.as_deref().unwrap_or("");
    let branch = match params.branch.as_deref() {
        Some("HEAD") | None => "main",
        Some(b) => b,
    };
    let file = params.file.as_deref().unwrap_or("dummy-ensemble.yaml");

    format!(
        "{}{}:{}:{}:blueprint+types",
        prefix, project_id, branch, file
    )
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
/// Tries the Redis cache with `key`; on a hit, honours `If-None-Match` (→ 304)
/// or returns 200 with the cached body and an `Etag` header.  On a miss (or
/// when Redis is not configured) falls through to the Python backend.
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
    key: String,
    latest_commit: Option<String>,
) -> CacheOutcome {
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
            return match serde_json::from_value::<unfurl_types::ExportResponse>(json_val.clone()) {
                Ok(typed) => CacheOutcome::Typed(Box::new(typed), etag),
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
    let latest_commit = params.latest_commit.clone();
    let key = export_cache_key(&state.config.cache_key_prefix, &params);
    match handle_cached_get(state, req, key, latest_commit).await {
        CacheOutcome::Typed(typed, etag) => Err(with_etag(Json(*typed).into_response(), &etag)),
        CacheOutcome::NotModified => Err(StatusCode::NOT_MODIFIED.into_response()),
        CacheOutcome::RawJson(val, etag) => Err(with_etag(Json(val).into_response(), &etag)),
        CacheOutcome::Proxied(resp) => Err(resp),
    }
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
    let latest_commit = params.latest_commit.clone();
    let key = types_cache_key(&state.config.cache_key_prefix, &params);
    match handle_cached_get(state, req, key, latest_commit).await {
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

    // Use the Redis queue only when Redis is available AND the request body
    // contains a non-null, non-empty "queueid" field.  When queueid is absent
    // the write request is proxied synchronously to the Python backend.
    let client_queueid = body
        .get("queueid")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .and_then(|s| s.parse::<i64>().ok());
    if let Some(queueid) = client_queueid {
        if let Some(ref redis) = state.redis {
            let latest_commit = body
                .get("latest_commit")
                .and_then(|v| v.as_str())
                .unwrap_or("");

            // Atomically validate and increment the queueid.
            let mut conn = redis.clone();
            let qid_result =
                match queue::inc_queueid(&mut conn, &project_id, latest_commit, queueid).await {
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
                    updated_body["queueid"] = serde_json::Value::String(new_queueid.to_string());
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
                    updated_body["queueid"] = serde_json::Value::String(new_queueid.to_string());
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
