// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Transparent HTTP proxy to the Python backend.

use axum::{
    body::Body,
    extract::Request,
    http::{header, HeaderMap, HeaderValue, StatusCode, Uri},
    response::{IntoResponse, Response},
};
use reqwest::Client;

/// Forward an incoming request to the Python backend and stream the
/// response back to the client.
pub async fn forward(
    client: &Client,
    backend_url: &str,
    req: Request,
    max_body_bytes: usize,
) -> Response {
    // Build the target URL preserving path and query string.
    let path_and_query = req
        .uri()
        .path_and_query()
        .map(|pq| pq.as_str().to_owned())
        .unwrap_or_else(|| "/".to_owned());

    let target = match format!("{}{}", backend_url, path_and_query).parse::<Uri>() {
        Ok(u) => u,
        Err(e) => {
            tracing::error!("invalid backend URI: {}", e);
            return (StatusCode::BAD_GATEWAY, "bad gateway").into_response();
        }
    };

    // Build the proxied request.
    let method = req.method().clone();
    let headers = req.headers().clone();
    let body_bytes = match axum::body::to_bytes(req.into_body(), max_body_bytes).await {
        Ok(b) => b,
        Err(e) => {
            tracing::error!("failed to read request body: {}", e);
            return (StatusCode::BAD_REQUEST, "bad request").into_response();
        }
    };

    let mut builder = client.request(
        reqwest::Method::from_bytes(method.as_str().as_bytes()).unwrap(),
        target.to_string(),
    );

    // Copy headers, skipping hop-by-hop.
    for (name, value) in headers.iter() {
        let n = name.as_str();
        if n == "host" || n == "transfer-encoding" || n == "connection" {
            continue;
        }
        if let Ok(v) = value.to_str() {
            builder = builder.header(n, v);
        }
    }
    builder = builder.body(body_bytes);

    let target_str = target.to_string();
    match builder.send().await {
        Ok(resp) => {
            let status = resp.status();
            tracing::debug!(
                "proxy {} {} -> backend status {}",
                method,
                target_str,
                status
            );
            convert_response(resp).await
        }
        Err(e) => {
            tracing::error!("proxy {} {} -> backend error: {}", method, target_str, e);
            (StatusCode::BAD_GATEWAY, "bad gateway").into_response()
        }
    }
}

/// RFC 2616 / RFC 7230 hop-by-hop headers that must not be forwarded by a proxy.
fn is_hop_by_hop(name: &str) -> bool {
    matches!(
        name,
        "connection"
            | "keep-alive"
            | "proxy-authenticate"
            | "proxy-authorization"
            | "te"
            | "trailers"
            | "transfer-encoding"
            | "upgrade"
    )
}

/// Pseudonym this proxy identifies itself by in `Via`.
const VIA_PSEUDONYM: &str = "unfurl-server";

/// `received-protocol` for a `Via` entry: the HTTP version the response
/// arrived on. The protocol name is omitted for HTTP, per RFC 9110.
fn via_protocol(version: reqwest::Version) -> &'static str {
    match version {
        reqwest::Version::HTTP_09 => "0.9",
        reqwest::Version::HTTP_10 => "1.0",
        reqwest::Version::HTTP_2 => "2",
        reqwest::Version::HTTP_3 => "3",
        // HTTP_11 and anything newer than this match knows about
        _ => "1.1",
    }
}

/// Convert a reqwest::Response into an axum Response.
async fn convert_response(resp: reqwest::Response) -> Response {
    let status =
        StatusCode::from_u16(resp.status().as_u16()).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    let via = format!("{} {}", via_protocol(resp.version()), VIA_PSEUDONYM);
    let mut headers = HeaderMap::new();
    for (name, value) in resp.headers().iter() {
        if is_hop_by_hop(name.as_str()) {
            continue;
        }
        if let Ok(v) = HeaderValue::from_bytes(value.as_bytes()) {
            headers.insert(name.clone(), v);
        }
    }
    // Appended, not inserted: an upstream that already sent one has its
    // own entry, and the chain is ordered. Only responses that actually
    // passed through here get one -- a cache hit or a queue 409 is
    // produced by this process, which makes it the origin rather than an
    // intermediary, and `Server` identifies it there instead.
    if let Ok(v) = HeaderValue::from_str(&via) {
        headers.append(header::VIA, v);
    }
    let body = resp.bytes().await.unwrap_or_default();
    let mut response = Response::new(Body::from(body));
    *response.status_mut() = status;
    *response.headers_mut() = headers;
    response
}
