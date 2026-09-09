// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! CORS wiring on the real router built by [`unfurl_server::build_router`].
//!
//! No Redis or backend is required: every assertion here is about headers
//! the [`CorsLayer`] adds or the preflight it answers before the request
//! reaches a handler.

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use clap::Parser;
use tower::ServiceExt;
use unfurl_server::config::Config;
use unfurl_server::{build_router, AppState};

const ORIGIN: &str = "https://unfurl.cloud";

fn config_with_cors(origins: Option<&str>) -> Config {
    let mut argv: Vec<String> = vec!["unfurl-server".into()];
    if let Some(o) = origins {
        argv.push("--cors-origins".into());
        argv.push(o.into());
    }
    // Parsing rather than a struct literal keeps this honest about the
    // clap wiring: a renamed flag fails here instead of silently passing.
    Config::parse_from(argv)
}

fn state(config: Config) -> AppState {
    AppState {
        config: std::sync::Arc::new(config),
        client: reqwest::Client::new(),
        redis: None,
        cloudmap: None,
    }
}

/// Send `req` through the real router and return (status, headers).
async fn send(origins: Option<&str>, req: Request<Body>) -> (StatusCode, axum::http::HeaderMap) {
    let config = config_with_cors(origins);
    let cors = config.cors_layer().expect("valid cors config");
    let app = build_router(state(config), cors);
    let res = app.oneshot(req).await.expect("router response");
    (res.status(), res.headers().clone())
}

fn preflight(path: &str, origin: &str) -> Request<Body> {
    Request::builder()
        .method("OPTIONS")
        .uri(path)
        .header(header::ORIGIN, origin)
        .header(header::ACCESS_CONTROL_REQUEST_METHOD, "GET")
        .body(Body::empty())
        .unwrap()
}

/// The bug this wiring fixes: `/export` is registered `get()`-only, so
/// without a CORS layer a browser's preflight is rejected by the method
/// router before any handler or proxy sees it.
#[tokio::test]
async fn preflight_is_405_without_cors() {
    let (status, headers) = send(None, preflight("/export", ORIGIN)).await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED);
    assert!(!headers.contains_key(header::ACCESS_CONTROL_ALLOW_ORIGIN));
}

#[tokio::test]
async fn preflight_answered_for_allowed_origin() {
    let (status, headers) = send(Some(ORIGIN), preflight("/export", ORIGIN)).await;
    assert!(
        status.is_success(),
        "preflight should be answered by the cors layer, got {status}"
    );
    assert_eq!(
        headers.get(header::ACCESS_CONTROL_ALLOW_ORIGIN).unwrap(),
        ORIGIN
    );
}

/// Preflight must also be answered on a path that only exists as the
/// proxy fallback -- `.route_layer` would skip these.
#[tokio::test]
async fn preflight_answered_on_fallback_path() {
    let (status, headers) = send(Some(ORIGIN), preflight("/anything/else", ORIGIN)).await;
    assert!(status.is_success(), "got {status}");
    assert_eq!(
        headers.get(header::ACCESS_CONTROL_ALLOW_ORIGIN).unwrap(),
        ORIGIN
    );
}

#[tokio::test]
async fn disallowed_origin_gets_no_allow_origin_header() {
    // Assert the allowed origin first: a bare "header is absent" check
    // also passes when the layer is missing entirely, so it can't tell
    // "rejected" from "not wired up".
    let (_status, allowed) = send(Some(ORIGIN), preflight("/export", ORIGIN)).await;
    assert_eq!(
        allowed.get(header::ACCESS_CONTROL_ALLOW_ORIGIN).unwrap(),
        ORIGIN,
        "layer must be live for the negative case below to mean anything"
    );
    let (_status, headers) = send(Some(ORIGIN), preflight("/export", "https://evil.test")).await;
    // tower-http omits the header rather than rejecting; the browser is
    // what refuses the response.
    assert!(!headers.contains_key(header::ACCESS_CONTROL_ALLOW_ORIGIN));
}

#[tokio::test]
async fn wildcard_allows_any_origin() {
    let (status, headers) = send(Some("*"), preflight("/export", "https://anywhere.test")).await;
    assert!(status.is_success(), "got {status}");
    assert_eq!(
        headers.get(header::ACCESS_CONTROL_ALLOW_ORIGIN).unwrap(),
        "*"
    );
}

#[tokio::test]
async fn multiple_origins_split_on_whitespace_and_commas() {
    // Python splits on whitespace; its docstring promises commas.
    let list = "https://a.test, https://b.test https://c.test";
    for origin in ["https://a.test", "https://b.test", "https://c.test"] {
        let (_status, headers) = send(Some(list), preflight("/export", origin)).await;
        assert_eq!(
            headers.get(header::ACCESS_CONTROL_ALLOW_ORIGIN).unwrap(),
            origin,
            "origin {origin} should be allowed"
        );
    }
    let (_status, headers) = send(Some(list), preflight("/export", "https://d.test")).await;
    assert!(!headers.contains_key(header::ACCESS_CONTROL_ALLOW_ORIGIN));
}

/// The python backend runs flask-cors, so a proxied response already
/// carries `Access-Control-Allow-Origin`. Two of them makes a browser
/// reject the response, so the layer must replace rather than append.
#[tokio::test]
async fn proxied_response_has_exactly_one_allow_origin() {
    let backend = axum::Router::new().fallback(|| async {
        (
            [(header::ACCESS_CONTROL_ALLOW_ORIGIN, ORIGIN)],
            "from backend",
        )
    });
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, backend).await.unwrap() });

    let config = Config::parse_from([
        "unfurl-server",
        "--cors-origins",
        ORIGIN,
        "--backend-url",
        &format!("http://{addr}"),
    ]);
    let cors = config.cors_layer().expect("valid cors config");
    let app = build_router(state(config), cors);
    let req = Request::builder()
        .uri("/some/proxied/path")
        .header(header::ORIGIN, ORIGIN)
        .body(Body::empty())
        .unwrap();
    let res = app.oneshot(req).await.unwrap();

    let values: Vec<_> = res
        .headers()
        .get_all(header::ACCESS_CONTROL_ALLOW_ORIGIN)
        .iter()
        .collect();
    assert_eq!(values.len(), 1, "got {values:?}");
    assert_eq!(values[0], ORIGIN);
}

#[test]
fn empty_and_whitespace_origins_add_no_layer() {
    for raw in [None, Some(""), Some("   ")] {
        let config = config_with_cors(raw);
        assert!(
            config.cors_layer().expect("no error").is_none(),
            "{raw:?} should not build a cors layer"
        );
    }
}

#[test]
fn invalid_origin_is_an_error() {
    // A newline can't go in a header value; the server exits rather than
    // starting with an origin list that silently drops an entry.
    let config = config_with_cors(Some("https://ok.test \u{7f}bad"));
    assert!(config.cors_layer().is_err());
}
