// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Integration tests for `GET /cloudmap` against a real `SyncedRepo`
//! seeded from the cloudmap fixture in
//! `rust/git-sync/tests/fixtures/expected_cloudmap.yaml`.
//!
//! Reuses the fixture-loading helpers from `rust/git-sync/tests/common`
//! by referencing the same fixture file (relative path) and calling
//! `unfurl_git_sync::git::init_with_files` directly.

use std::path::Path;
use std::sync::Arc;

use axum::{
    body::Body,
    http::{Request, StatusCode},
    routing::get,
    Router,
};
use serde_json::Value;
use tempfile::TempDir;
use tower::util::ServiceExt;
use unfurl_git_sync::{DbConfig, FormatRegistry, SyncedRepo};
use unfurl_server::cloudmap::{handle_cloudmap, CloudMapState};
use unfurl_server::config::Config;
use unfurl_server::AppState;

const FIXTURE: &str = "../git-sync/tests/fixtures/expected_cloudmap.yaml";

fn default_config() -> Config {
    Config {
        host: "127.0.0.1".into(),
        port: 0,
        backend_url: None,
        redis_url: None,
        redis_host: None,
        redis_port: 6379,
        redis_password: None,
        redis_db: 0,
        cache_key_prefix: "test::".into(),
        secret: String::new(),
        proxy_timeout_secs: 1,
        redis_timeout_secs: 1,
        package_digest: String::new(),
        max_body_bytes: 10 * 1024 * 1024,
        batch_window_secs: 0,
        cloudmap_repo: None,
        cloudmap_db_url: None,
    }
}

/// Stand up a fresh git repo seeded with the cloudmap fixture, an
/// in-memory SQLite database, and a configured `SyncedRepo`.
async fn open_cloudmap_state() -> (CloudMapState, TempDir) {
    let tmp = tempfile::tempdir().expect("tempdir");
    let fixture =
        std::fs::read(Path::new(env!("CARGO_MANIFEST_DIR")).join(FIXTURE)).expect("fixture exists");
    unfurl_git_sync::git::init_with_files(
        tmp.path(),
        &[("cloudmap.yaml".to_string(), fixture)],
        "initial",
    )
    .expect("init repo");

    let synced = SyncedRepo::open(
        tmp.path(),
        DbConfig::Sqlite {
            url: "sqlite::memory:".into(),
        },
        FormatRegistry::with_builtins(),
    )
    .await
    .expect("open SyncedRepo");
    synced
        .update_from_working_dir()
        .await
        .expect("update_from_working_dir");

    // Re-derive a CloudMapState from the same repo. CloudMapState::open
    // is the public path used in production.
    let cm = CloudMapState::from_synced(synced);
    (cm, tmp)
}

fn router(state: AppState) -> Router {
    Router::new()
        .route("/cloudmap", get(handle_cloudmap))
        .with_state(state)
}

async fn get_json(app: Router, uri: &str) -> (StatusCode, Value) {
    let req = Request::builder()
        .method("GET")
        .uri(uri)
        .body(Body::empty())
        .unwrap();
    let resp = app.oneshot(req).await.expect("handler");
    let status = resp.status();
    let body_bytes = axum::body::to_bytes(resp.into_body(), 16 * 1024 * 1024)
        .await
        .expect("read body");
    let body: Value = serde_json::from_slice(&body_bytes)
        .unwrap_or(Value::String(String::from_utf8_lossy(&body_bytes).into()));
    (status, body)
}

fn make_state(cm: CloudMapState) -> AppState {
    AppState {
        config: Arc::new(default_config()),
        client: reqwest::Client::new(),
        redis: None,
        cloudmap: Some(cm),
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn full_document_returns_all_sections() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let (status, body) = get_json(app, "/cloudmap").await;
    assert_eq!(status, StatusCode::OK);
    let arr = body.as_array().expect("array");
    assert_eq!(arr.len(), 2, "response is a [primary, followed] pair");
    let primary = &arr[0];
    let followed = &arr[1];
    // Primary must contain the cloudmap-shaped sections present in the fixture.
    assert!(primary.get("repositories").is_some());
    assert!(primary.get("artifacts").is_some());
    // Followed defaults to {} when no key was supplied.
    assert_eq!(followed, &serde_json::json!({}));
}

#[tokio::test]
async fn kind_only_returns_one_section() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let (status, body) = get_json(app, "/cloudmap?kind=repositories").await;
    assert_eq!(status, StatusCode::OK);
    let primary = &body[0];
    assert!(primary.get("repositories").is_some());
    assert!(
        primary.get("artifacts").is_none(),
        "kind=repositories must not include artifacts"
    );
    assert_eq!(body[1], serde_json::json!({}));
}

#[tokio::test]
async fn kind_and_key_returns_single_record() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let key = "git://unfurl.cloud/onecommons/blueprints/odoo.git";
    let uri = format!(
        "/cloudmap?kind=repositories&key={}",
        urlencoding::encode(key)
    );
    let (status, body) = get_json(app, &uri).await;
    assert_eq!(status, StatusCode::OK);
    let primary = &body[0];
    let repos = primary.get("repositories").expect("repositories section");
    assert_eq!(repos.as_object().unwrap().len(), 1);
    assert!(repos.get(key).is_some(), "expected key under repositories");
    assert_eq!(body[1], serde_json::json!({}));
}

#[tokio::test]
async fn missing_key_returns_404() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let (status, _body) = get_json(
        app,
        "/cloudmap?kind=repositories&key=git://no/such/repo.git",
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn follow_walks_graph_when_key_supplied() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let key = "git://unfurl.cloud/onecommons/blueprints/odoo.git";
    let uri = format!(
        "/cloudmap?kind=repositories&key={}&follow=10",
        urlencoding::encode(key)
    );
    let (status, body) = get_json(app, &uri).await;
    assert_eq!(status, StatusCode::OK);
    let followed = &body[1];
    let followed_obj = followed.as_object().expect("followed is object");
    assert!(
        !followed_obj.is_empty(),
        "follow=10 should reach at least one record"
    );

    // The four records expected by the git-sync test fixture (matches
    // run_find_records_follow_walk in test_crud.rs).
    let mut keys: Vec<&str> = Vec::new();
    for (_section, records) in followed_obj {
        for k in records.as_object().expect("records").keys() {
            keys.push(k.as_str());
        }
    }
    keys.sort();
    let mut expected = vec![
        "git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml",
        "git://unfurl.cloud/onecommons/unfurl-types.git",
        "git://unfurl.cloud/onecommons/unfurl-types.git#:dummy-ensemble.yaml",
        "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo",
    ];
    expected.sort();
    assert_eq!(keys, expected);

    // Spot-check: the artifact under odoo.git should declare a TOSCA type.
    let artifacts = followed_obj
        .get("artifacts")
        .and_then(|v| v.as_object())
        .expect("artifacts in followed");
    let ensemble = artifacts
        .get("git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml")
        .expect("ensemble-template artifact");
    assert!(ensemble.get("type").is_some(), "artifact has a type field");

    // Each record carries OCC tokens the client can echo back as a
    // CommitRef on subsequent writes.
    assert!(
        ensemble["unfurl.server.version"].is_i64(),
        "version token present"
    );
    assert!(
        ensemble.get("unfurl.server.commit").is_some(),
        "commit token present (may be null when in-flight)"
    );
}

#[tokio::test]
async fn records_carry_occ_tokens() {
    // Every record in the response — primary or followed — gets
    // `unfurl.server.version` and `unfurl.server.commit` keys.
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let (status, body) = get_json(app, "/cloudmap").await;
    assert_eq!(status, StatusCode::OK);
    let primary = &body[0];
    let repos = primary
        .get("repositories")
        .and_then(|v| v.as_object())
        .expect("repositories section");
    assert!(!repos.is_empty(), "fixture has at least one repository");
    for (k, record) in repos {
        let obj = record.as_object().expect("record is an object");
        let v = obj
            .get("unfurl.server.version")
            .expect("version token")
            .as_i64()
            .expect("version is i64");
        assert!(v > 0, "{k} has positive version, got {v}");
        // commit token is present (and may be a string OR null).
        let commit = obj.get("unfurl.server.commit").expect("commit token");
        assert!(
            commit.is_string() || commit.is_null(),
            "{k} commit token should be string or null, got {commit:?}"
        );
    }
}

#[tokio::test]
async fn follow_caps_record_count() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let key = "git://unfurl.cloud/onecommons/blueprints/odoo.git";
    // Request follow=2 — only two records should come back even though
    // the BFS could reach more.
    let uri = format!(
        "/cloudmap?kind=repositories&key={}&follow=2",
        urlencoding::encode(key)
    );
    let (status, body) = get_json(app, &uri).await;
    assert_eq!(status, StatusCode::OK);
    let followed = &body[1];
    let total: usize = followed
        .as_object()
        .unwrap()
        .values()
        .map(|sec| sec.as_object().map(|m| m.len()).unwrap_or(0))
        .sum();
    assert_eq!(total, 2, "follow=2 caps the followed set at 2 records");
}

#[tokio::test]
async fn follow_zero_returns_empty_dict() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let key = "git://unfurl.cloud/onecommons/blueprints/odoo.git";
    let uri = format!(
        "/cloudmap?kind=repositories&key={}&follow=0",
        urlencoding::encode(key)
    );
    let (status, body) = get_json(app, &uri).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body[1], serde_json::json!({}), "follow=0 → empty dict");
}

#[tokio::test]
async fn follow_without_key_returns_empty_dict() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let (status, body) = get_json(app, "/cloudmap?follow=10").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        body[1],
        serde_json::json!({}),
        "follow without key → empty dict"
    );
}
