// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A synchronous write (no `queueid`) must reach python with the client's
//! headers intact.
//!
//! `handle_write`'s sync branch used to build a fresh request carrying only
//! a method, URI and body, so every header was dropped on the way to the
//! backend. Two of them matter: `X-Git-Credentials`, which `_get_body`
//! decodes to inject `username` into the body -- without it
//! `_get_managed_project_repo_dir` picks `repos/public/` and a private
//! project clones anonymously and 500s -- and `Content-Type`, which flask's
//! `request.json` is sensitive to.
//!
//! No Redis needed: with `redis: None` the pending-writes check is skipped
//! and the request goes straight to the forwarder.

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use clap::Parser;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use tokio::net::TcpListener;
use tower::ServiceExt;
use unfurl_server::config::Config;
use unfurl_server::{build_router, AppState};

type Captured = Arc<Mutex<Vec<HashMap<String, String>>>>;

/// Backend that records the headers of everything it receives.
async fn header_capturing_backend() -> (String, Captured) {
    let captured: Captured = Arc::new(Mutex::new(Vec::new()));
    let sink = captured.clone();
    let app = axum::Router::new().fallback(move |req: axum::extract::Request| {
        let sink = sink.clone();
        async move {
            let headers = req
                .headers()
                .iter()
                .filter_map(|(k, v)| {
                    v.to_str()
                        .ok()
                        .map(|val| (k.as_str().to_lowercase(), val.to_string()))
                })
                .collect();
            sink.lock().unwrap().push(headers);
            axum::Json(serde_json::json!({"commit": "abc123"}))
        }
    });
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    (format!("http://{}", addr), captured)
}

/// A body with no `queueid`, which is what selects the synchronous path.
fn sync_body() -> String {
    serde_json::json!({
        "patch": [{"__typename": "DeploymentEnvironment", "name": "staging"}],
        "commit_msg": "create environment",
        "branch": "main",
        "latest_commit": "abc123",
        "deployment_path": "",
        "environment": "staging",
    })
    .to_string()
}

#[tokio::test]
async fn sync_write_forwards_client_headers() {
    let (backend_url, captured) = header_capturing_backend().await;
    let config = Config::parse_from(["unfurl-server", "--backend-url", &backend_url]);
    let state = AppState {
        config: std::sync::Arc::new(config),
        client: reqwest::Client::new(),
        redis: None,
        cloudmap: None,
    };
    let app = build_router(state, None);

    let req = Request::builder()
        .method("POST")
        .uri("/update_environment?auth_project=user%2Fdashboard")
        .header(header::CONTENT_TYPE, "application/json")
        .header("X-Git-Credentials", "dXNlcjp0b2tlbg==")
        .header(header::USER_AGENT, "Mozilla/5.0")
        .body(Body::from(sync_body()))
        .unwrap();
    let res = app.oneshot(req).await.unwrap();
    assert_eq!(res.status(), StatusCode::OK, "expected a proxied response");

    let seen = captured.lock().unwrap().clone();
    assert_eq!(seen.len(), 1, "backend should have received one request");
    let headers = &seen[0];

    assert_eq!(
        headers.get("x-git-credentials").map(String::as_str),
        Some("dXNlcjp0b2tlbg=="),
        "python decodes this to inject `username`; without it a private \
         project clones anonymously. got: {headers:?}"
    );
    assert_eq!(
        headers.get("content-type").map(String::as_str),
        Some("application/json"),
        "flask's request.json needs this. got: {headers:?}"
    );
    // Not load-bearing, but it is what made the bug visible: python's
    // access log showed "-" "-" for the sync path and the browser's
    // user-agent for the queued one.
    assert_eq!(
        headers.get("user-agent").map(String::as_str),
        Some("Mozilla/5.0")
    );
}
