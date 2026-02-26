// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Axum route handlers for the unfurl server proxy.

use axum::{
    body::Body,
    extract::{Request, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use serde_json::{json, Value as JsonValue};
use std::collections::HashMap;

use crate::cache;
use crate::proxy;
use crate::queue::{self, QueueItem};
use crate::AppState;

// ---------------------------------------------------------------------------
// Cache-aware GET handlers
// ---------------------------------------------------------------------------

/// Build the Redis cache key for a `/export` request from its query params.
fn export_cache_key(prefix: &str, params: &HashMap<String, String>) -> String {
    let project_id = params.get("auth_project").map(|s| s.as_str()).unwrap_or("");
    let branch = match params.get("branch").map(|s| s.as_str()) {
        Some("HEAD") | None => "",
        Some(b) => b,
    };
    let format = params.get("format").map(|s| s.as_str()).unwrap_or("deployment");
    let deployment_path = params.get("deployment_path").map(|s| s.as_str());

    let file_path = match format {
        "blueprint" => "ensemble-template.yaml".to_string(),
        "environments" => "unfurl.yaml".to_string(),
        _ => {
            // deployment
            match deployment_path {
                None | Some("") => "ensemble.yaml".to_string(),
                Some(p) if p.ends_with(".yaml") => p.to_string(),
                Some(p) => format!("{}/ensemble.yaml", p),
            }
        }
    };

    let key_suffix = match format {
        "blueprint" => "blueprint".to_string(),
        "environments" => {
            if let Some(env) = params.get("environment") {
                format!("environments+{}", env)
            } else {
                "environments".to_string()
            }
        }
        _ => "deployment".to_string(),
    };

    format!("{}{}:{}:{}:{}", prefix, project_id, branch, file_path, key_suffix)
}

/// Build the Redis cache key for a `/types` request.
fn types_cache_key(prefix: &str, params: &HashMap<String, String>) -> String {
    let project_id = params.get("auth_project").map(|s| s.as_str()).unwrap_or("");
    let branch = match params.get("branch").map(|s| s.as_str()) {
        Some("HEAD") | None => "",
        Some(b) => b,
    };
    let file = params
        .get("file")
        .map(|s| s.as_str())
        .unwrap_or("dummy-ensemble.yaml");

    format!("{}{}:{}:{}:blueprint+types", prefix, project_id, branch, file)
}

/// Parse query string into a HashMap.
fn parse_query(uri: &axum::http::Uri) -> HashMap<String, String> {
    uri.query()
        .map(|q| {
            url::form_urlencoded::parse(q.as_bytes())
                .map(|(k, v): (std::borrow::Cow<'_, str>, std::borrow::Cow<'_, str>)| {
                    (k.into_owned(), v.into_owned())
                })
                .collect()
        })
        .unwrap_or_default()
}

/// GET /export -- try Redis cache first, fall back to proxying.
pub async fn handle_export(State(state): State<AppState>, req: Request) -> Response {
    let params = parse_query(req.uri());
    let latest_commit = params.get("latest_commit").map(|s| s.as_str());

    if let Some(ref redis) = state.redis {
        let key = export_cache_key(&state.config.cache_key_prefix, &params);
        let mut conn = redis.clone();
        if let Some(json_val) =
            cache::try_cache(&mut conn, &key, latest_commit, state.config.redis_timeout_secs).await
        {
            tracing::debug!("cache hit for export: {}", key);
            return Json(json_val).into_response();
        }
        tracing::debug!("cache miss for export: {}", key);
    }

    proxy::forward(&state.client, &state.config.backend_url(), req).await
}

/// GET /types -- try Redis cache first, fall back to proxying.
pub async fn handle_types(State(state): State<AppState>, req: Request) -> Response {
    let params = parse_query(req.uri());
    let latest_commit = params.get("latest_commit").map(|s| s.as_str());

    if let Some(ref redis) = state.redis {
        let key = types_cache_key(&state.config.cache_key_prefix, &params);
        let mut conn = redis.clone();
        if let Some(json_val) =
            cache::try_cache(&mut conn, &key, latest_commit, state.config.redis_timeout_secs).await
        {
            tracing::debug!("cache hit for types: {}", key);
            return Json(json_val).into_response();
        }
        tracing::debug!("cache miss for types: {}", key);
    }

    proxy::forward(&state.client, &state.config.backend_url(), req).await
}

// ---------------------------------------------------------------------------
// Write / queue handlers
// ---------------------------------------------------------------------------

/// POST write endpoints -- enqueue to Redis and return immediately.
pub async fn handle_write(State(state): State<AppState>, req: Request) -> Response {
    let endpoint = req
        .uri()
        .path_and_query()
        .map(|pq| pq.as_str().to_string())
        .unwrap_or_else(|| req.uri().path().to_string());
    let headers_map: HashMap<String, String> = req
        .headers()
        .iter()
        .filter_map(|(k, v)| {
            let name = k.as_str();
            if name == "host" || name == "transfer-encoding" || name == "connection" {
                return None;
            }
            v.to_str().ok().map(|val| (name.to_string(), val.to_string()))
        })
        .collect();

    // Read body.
    let body_bytes = match axum::body::to_bytes(req.into_body(), 10 * 1024 * 1024).await {
        Ok(b) => b,
        Err(e) => {
            tracing::error!("failed to read write request body: {}", e);
            return (StatusCode::BAD_REQUEST, "bad request").into_response();
        }
    };

    let body: JsonValue = serde_json::from_slice(&body_bytes).unwrap_or(JsonValue::Null);

    // Use the Redis queue only when Redis is available AND queueing is not disabled.
    // Setting UNFURL_RUST_DISABLE_QUEUE=1 lets tests use Redis for caching while
    // still getting synchronous commit responses from write endpoints.
    let queue_disabled = std::env::var("UNFURL_RUST_DISABLE_QUEUE").as_deref() == Ok("1");
    if !queue_disabled {
        if let Some(ref redis) = state.redis {
            let item = QueueItem {
                endpoint: endpoint.clone(),
                body,
                headers: headers_map,
            };
            let mut conn = redis.clone();
            let queue_key = state.config.queue_key();
            if let Err(e) = queue::enqueue(&mut conn, &queue_key, &item).await {
                tracing::error!("failed to enqueue: {}", e);
                return (StatusCode::INTERNAL_SERVER_ERROR, "queue error").into_response();
            }
            return Json(json!({"queued": true})).into_response();
        }
    }

    // Proxy directly to Python backend (Redis unavailable, or queue disabled).
    // Preserve full path + query string from the original request.
    let path_and_query = req_path_query(&endpoint, "");
    let proxy_req = Request::builder()
        .method(axum::http::Method::POST)
        .uri(format!("{}{}", state.config.backend_url(), path_and_query))
        .body(Body::from(body_bytes))
        .unwrap();
    proxy::forward(&state.client, &state.config.backend_url(), proxy_req).await
}

// ---------------------------------------------------------------------------
// Catch-all pass-through
// ---------------------------------------------------------------------------

/// All other endpoints -- proxy transparently to Python.
pub async fn handle_fallback(State(state): State<AppState>, req: Request) -> Response {
    proxy::forward(&state.client, &state.config.backend_url(), req).await
}

/// Helper: ensure path starts with `/`.
fn req_path_query(path: &str, _default_query: &str) -> String {
    if path.starts_with('/') {
        path.to_string()
    } else {
        format!("/{}", path)
    }
}
