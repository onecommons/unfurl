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
use unfurl_server::cloudmap::{
    handle_cloudmap, post_cloudmap_local, post_cloudmap_proxy, CloudMapState,
};
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
        batch_window_secs: 0.0,
        worker_poll_interval_secs: 0.05,
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
    let cloudmap_route = if state.cloudmap.is_some() {
        get(handle_cloudmap).post(post_cloudmap_local)
    } else {
        get(handle_cloudmap).post(post_cloudmap_proxy)
    };
    Router::new()
        .route("/cloudmap", cloudmap_route)
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

async fn post_json(app: Router, body: Value) -> (StatusCode, Value) {
    let req = Request::builder()
        .method("POST")
        .uri("/cloudmap")
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(&body).unwrap()))
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
        "git://unfurl.cloud/onecommons/blueprints/odoo.git#:.gitlab-ci.yml",
        "git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template",
        "git://unfurl.cloud/onecommons/unfurl-types.git",
        "git://unfurl.cloud/onecommons/unfurl-types.git#:.gitlab-ci.yml",
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
        .get("git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template")
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

// ---------------------------------------------------------------------------
// POST /cloudmap tests
// ---------------------------------------------------------------------------

/// Helper: read the current `version` of an existing record via GET.
async fn read_version(cm: CloudMapState, kind: &str, key: &str) -> i64 {
    let app = router(make_state(cm));
    let uri = format!("/cloudmap?kind={}&key={}", kind, urlencoding::encode(key));
    let (status, body) = get_json(app, &uri).await;
    assert_eq!(status, StatusCode::OK);
    body[0][kind][key]["unfurl.server.version"]
        .as_i64()
        .expect("version present")
}

#[tokio::test]
async fn post_upsert_writes_record() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let key = "git://unfurl.cloud/onecommons/std.git";

    let v_before = read_version(cm.clone(), "repositories", key).await;

    let app = router(make_state(cm.clone()));
    let body = serde_json::json!({
        "repositories": {
            key: { "name": "renamed-via-post" },
        }
    });
    let (status, response) = post_json(app, body).await;
    assert_eq!(status, StatusCode::OK);
    // The Rust handler stages records as in-flight (no git commit
    // yet) so the spec's `commit` field is null/absent here. Mirrors
    // the Python handler when its working tree was already clean.
    // `skip_serializing_none` on the generated PatchResponse means
    // an absent key is equivalent to `null`.
    assert!(
        matches!(response.get("commit"), None | Some(Value::Null)),
        "response should match PatchResponse shape with commit=null: {response:?}"
    );

    // GET confirms the new value is visible and the row's version
    // advanced past `v_before`.
    let v_after = read_version(cm, "repositories", key).await;
    assert!(
        v_after > v_before,
        "version should advance: {v_before} → {v_after}"
    );
}

#[tokio::test]
async fn post_creates_new_record_in_default_file() {
    use unfurl_git_sync::SyncedRepo;

    // Construct the SyncedRepo separately so the test can inspect
    // file_path on the new record.
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
        unfurl_git_sync::DbConfig::Sqlite {
            url: "sqlite::memory:".into(),
        },
        unfurl_git_sync::FormatRegistry::with_builtins(),
    )
    .await
    .expect("open SyncedRepo");
    synced.update_from_working_dir().await.expect("update");

    // Default file path was set on first sync.
    let wt = synced.get_worktree().await.expect("get_worktree");
    assert_eq!(wt.default_file_path.as_deref(), Some("cloudmap.yaml"));

    let cm = CloudMapState::from_synced(synced.clone());
    let app = router(make_state(cm));
    let new_url = "git://example.com/brand-new.git";
    let body = serde_json::json!({
        "repositories": {
            new_url: { "name": "brand-new" },
        }
    });
    let (status, _echo) = post_json(app, body).await;
    assert_eq!(status, StatusCode::OK);

    // The new record's file_path must be the worktree default.
    let rec = synced
        .get_record("cloudmap.yaml", "/repositories", new_url)
        .await
        .expect("get_record")
        .expect("present");
    assert_eq!(rec.file_path, "cloudmap.yaml");
    assert_eq!(rec.json["name"], "brand-new");
}

#[tokio::test]
async fn post_deleted_marker_deletes_record() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let key = "git://unfurl.cloud/onecommons/std.git";
    let app = router(make_state(cm.clone()));
    let body = serde_json::json!({
        "repositories": {
            key: {"unfurl.server.deleted": true},
        }
    });
    let (status, echo) = post_json(app, body).await;
    assert_eq!(status, StatusCode::OK);
    assert!(echo["repositories"][key].is_null());

    // The record is now hidden (tombstoned) — GET returns 404 for the
    // single-key filter.
    let app = router(make_state(cm));
    let (status, _) = get_json(
        app,
        &format!(
            "/cloudmap?kind=repositories&key={}",
            urlencoding::encode(key)
        ),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn post_with_stale_version_returns_409() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let key = "git://unfurl.cloud/onecommons/std.git";
    let v_before = read_version(cm.clone(), "repositories", key).await;

    // First write succeeds and bumps the version.
    let app = router(make_state(cm.clone()));
    let _ = post_json(
        app,
        serde_json::json!({
            "repositories": { key: { "name": "v2" } }
        }),
    )
    .await;

    // Second write submits the *stale* version → 409.
    let app = router(make_state(cm));
    let body = serde_json::json!({
        "repositories": {
            key: {
                "name": "v3",
                "unfurl.server.version": v_before,
            }
        }
    });
    let (status, body) = post_json(app, body).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"], "conflict");
    assert_eq!(body["section"], "repositories");
    assert_eq!(body["key"], key);
}

#[tokio::test]
async fn post_with_oid_token_succeeds_when_matches() {
    use unfurl_git_sync::SyncedRepo;

    // Need to commit first so a real oid exists. Reconstruct so we
    // can drive `commit_repository`.
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
        unfurl_git_sync::DbConfig::Sqlite {
            url: "sqlite::memory:".into(),
        },
        unfurl_git_sync::FormatRegistry::with_builtins(),
    )
    .await
    .expect("open SyncedRepo");
    synced.update_from_working_dir().await.expect("update");

    let key = "git://unfurl.cloud/onecommons/std.git";
    // Mutate, save, commit so the record now has a non-null commit_id.
    synced
        .upsert_record(
            None,
            "/repositories",
            key,
            serde_json::json!({"name": "via-test"}),
            None,
        )
        .await
        .expect("upsert");
    synced.save_changes().await.expect("save");
    let oid = synced
        .commit_repository("test")
        .await
        .expect("commit")
        .expect("returned");
    let rec = synced
        .get_record("cloudmap.yaml", "/repositories", key)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(rec.commit_id.as_deref(), Some(oid.as_str()));

    // POST with the matching commit oid → success.
    let cm = CloudMapState::from_synced(synced);
    let app = router(make_state(cm));
    let body = serde_json::json!({
        "repositories": {
            key: {
                "name": "post-commit",
                "unfurl.server.commit": oid,
            }
        }
    });
    let (status, _) = post_json(app, body).await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn post_schema_violation_returns_422() {
    // The cloudmap schema declares `protocols` as an array of
    // strings; a string fails serde-deser into the typed
    // `generated::CloudMapDocument`. axum's `Json` extractor
    // returns 422 automatically on shape mismatch.
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let key = "git://unfurl.cloud/onecommons/std.git";
    let body = serde_json::json!({
        "repositories": {
            key: {
                "path": "onecommons/std",
                "protocols": "not-an-array",
            }
        }
    });
    let (status, _body) = post_json(app, body).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
}

#[tokio::test]
async fn post_default_atomic_rolls_back_partial_batch() {
    // Default mode (atomic flag absent → true): a single conflict in
    // the batch must roll back every other write.
    let (cm, _tmp) = open_cloudmap_state().await;
    let tracked_key = "git://unfurl.cloud/onecommons/std.git";
    let v_before = read_version(cm.clone(), "repositories", tracked_key).await;

    // Out-of-band update advances the version on `tracked_key` so the
    // batch's stale token will conflict.
    let app = router(make_state(cm.clone()));
    let _ = post_json(
        app,
        serde_json::json!({
            "repositories": { tracked_key: { "name": "v2" } }
        }),
    )
    .await;

    let app = router(make_state(cm.clone()));
    let body = serde_json::json!({
        "repositories": {
            "fresh-key": { "name": "fresh" },
            tracked_key: {
                "name": "v3",
                "unfurl.server.version": v_before, // stale
            },
        }
    });
    let (status, body) = post_json(app, body).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"], "conflict");
    // Atomic rollback: applied is empty, fresh-key did not commit.
    assert_eq!(
        body["applied"].as_array().expect("applied array").len(),
        0,
        "atomic mode: applied must be empty, got {body:?}"
    );
    // The fresh key must not exist.
    let app = router(make_state(cm.clone()));
    let (s, _) = get_json(app, "/cloudmap?kind=repositories&key=fresh-key").await;
    assert_eq!(s, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn post_non_atomic_skips_failures_and_commits_rest() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let tracked_key = "git://unfurl.cloud/onecommons/std.git";
    let v_before = read_version(cm.clone(), "repositories", tracked_key).await;

    // OOB update so the stale token will conflict.
    let app = router(make_state(cm.clone()));
    let _ = post_json(
        app,
        serde_json::json!({
            "repositories": { tracked_key: { "name": "v2" } }
        }),
    )
    .await;

    let app = router(make_state(cm.clone()));
    let body = serde_json::json!({
        "atomic": false,
        "repositories": {
            "fresh-key": { "name": "fresh" },
            tracked_key: {
                "name": "v3",
                "unfurl.server.version": v_before,
            },
            "another-fresh": { "name": "after" },
        }
    });
    let (status, body) = post_json(app, body).await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    let applied = body["applied"].as_array().expect("applied array");
    assert_eq!(
        applied.len(),
        2,
        "expected fresh + after to land, got {body}"
    );
    let applied_keys: Vec<&str> = applied
        .iter()
        .map(|a| a["key"].as_str().expect("key str"))
        .collect();
    assert!(applied_keys.contains(&"fresh-key"));
    assert!(applied_keys.contains(&"another-fresh"));
    let failed = body["failed"].as_array().expect("failed array");
    assert_eq!(failed.len(), 1);
    assert_eq!(failed[0]["key"], tracked_key);
    assert_eq!(failed[0]["error"], "conflict");
    // The fresh records should be retrievable.
    for k in ["fresh-key", "another-fresh"] {
        let app = router(make_state(cm.clone()));
        let (s, _) = get_json(
            app,
            &format!("/cloudmap?kind=repositories&key={}", urlencoding::encode(k)),
        )
        .await;
        assert_eq!(s, StatusCode::OK, "expected {k} to exist");
    }
}

#[tokio::test]
async fn post_success_returns_applied_list() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let body = serde_json::json!({
        "repositories": {
            "k1": { "name": "one" },
            "k2": { "name": "two" },
        }
    });
    let (status, body) = post_json(app, body).await;
    assert_eq!(status, StatusCode::OK);
    let applied = body["applied"].as_array().expect("applied array");
    assert_eq!(applied.len(), 2);
    let keys: std::collections::BTreeSet<&str> = applied
        .iter()
        .map(|a| a["key"].as_str().expect("key str"))
        .collect();
    assert!(keys.contains("k1"));
    assert!(keys.contains("k2"));
    for entry in applied {
        assert_eq!(entry["section"], "repositories");
        assert!(entry["version"].is_i64());
    }
}

#[tokio::test]
async fn post_proxies_when_cloudmap_unconfigured() {
    // No cloudmap state → proxy::forward runs. The default_config()
    // backend points at 127.0.0.1:1 (port 0 + 1) which isn't
    // listening, so the proxy should return 502 Bad Gateway.
    let state = AppState {
        config: Arc::new(default_config()),
        client: reqwest::Client::new(),
        redis: None,
        cloudmap: None,
    };
    let app = router(state);
    let body = serde_json::json!({});
    let (status, _) = post_json(app, body).await;
    assert!(
        status.is_server_error() || status == StatusCode::BAD_GATEWAY,
        "expected proxy failure (no python backend running), got {status}"
    );
}

// ---------------------------------------------------------------------------
// `type` query filter
// ---------------------------------------------------------------------------

#[tokio::test]
async fn type_filter_matches_declared_type() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let (status, body) = get_json(app, "/cloudmap?type=cloudmap.artifacts.ci.GitLabPipeline").await;
    assert_eq!(status, StatusCode::OK);
    let primary = body[0].as_object().expect("primary object");
    assert_eq!(
        primary.keys().collect::<Vec<_>>(),
        vec!["artifacts"],
        "only artifacts declare the pipeline type"
    );
    let artifacts = primary["artifacts"].as_object().expect("artifacts");
    assert_eq!(artifacts.len(), 4, "keys: {:?}", artifacts.keys());
    assert!(artifacts.keys().all(|k| k.ends_with(".gitlab-ci.yml")));
    assert_eq!(body[1], serde_json::json!({}));
}

#[tokio::test]
async fn type_filter_matches_subtypes_via_extends() {
    // services/https://example.com/oodo declares type `Odoo@…`, whose
    // type record (transitively) extends `SoftwareService@…` —
    // querying the base type must match the subtype-declaring record.
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let t = "SoftwareService@unfurl.cloud/onecommons/std:generic_types";
    let (status, body) = get_json(app, &format!("/cloudmap?type={}", urlencoding::encode(t))).await;
    assert_eq!(status, StatusCode::OK);
    let primary = body[0].as_object().expect("primary object");
    assert_eq!(primary.keys().collect::<Vec<_>>(), vec!["services"]);
    let services = primary["services"].as_object().expect("services");
    assert!(services.contains_key("https://example.com/oodo"));
}

#[tokio::test]
async fn type_filter_without_matches_returns_empty_doc() {
    // `tosca.relationships.ConnectsTo` has subtypes in /types
    // (AWSAccount, GoogleCloudProject) but no record *declares* any of
    // them as its own `type` — they only appear as dependency
    // constraints. The filter applies and matches nothing.
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let (status, body) = get_json(app, "/cloudmap?type=tosca.relationships.ConnectsTo").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body[0], serde_json::json!({}));
    assert_eq!(body[1], serde_json::json!({}));
}

#[tokio::test]
async fn type_filter_with_kind_and_key() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let state = make_state(cm);
    let key = "https://example.com/oodo";

    // Matching type (via extends: Odoo@… extends tosca.nodes.Root).
    let uri = format!(
        "/cloudmap?kind=services&key={}&type=tosca.nodes.Root",
        urlencoding::encode(key)
    );
    let (status, body) = get_json(router(state.clone()), &uri).await;
    assert_eq!(status, StatusCode::OK);
    assert!(body[0]["services"].get(key).is_some());

    // Record exists but its type doesn't satisfy the filter → 404.
    let uri = format!(
        "/cloudmap?kind=services&key={}&type=cloudmap.artifacts.oci.Image",
        urlencoding::encode(key)
    );
    let (status, _body) = get_json(router(state), &uri).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn type_filter_cache_invalidated_when_types_change() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let state = make_state(cm);
    let uri = "/cloudmap?type=cloudmap.artifacts.tosca.TypeLibrary";

    // Warm the closure cache.
    let (status, body) = get_json(router(state.clone()), uri).await;
    assert_eq!(status, StatusCode::OK);
    let before = body[0]["artifacts"].as_object().expect("artifacts").len();
    assert_eq!(before, 2, "fixture has two TypeLibrary artifacts");

    // Write a new subtype into /types plus an artifact declaring it.
    let (status, post_body) = post_json(
        router(state.clone()),
        serde_json::json!({
            "types": {
                "test.SubTypeLibrary": {
                    "kind": "Artifact",
                    "extends": ["cloudmap.artifacts.tosca.TypeLibrary"]
                }
            },
            "artifacts": {
                "https://example.com/newlib": {
                    "type": {"test.SubTypeLibrary": null}
                }
            }
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "post failed: {post_body}");

    // The probe must notice the /types change, rebuild the closure,
    // and pick up the record declaring the new subtype.
    let (status, body) = get_json(router(state), uri).await;
    assert_eq!(status, StatusCode::OK);
    let artifacts = body[0]["artifacts"].as_object().expect("artifacts");
    assert_eq!(artifacts.len(), before + 1, "keys: {:?}", artifacts.keys());
    assert!(artifacts.contains_key("https://example.com/newlib"));
}

// ---------------------------------------------------------------------------
// `select` query projection
// ---------------------------------------------------------------------------

#[tokio::test]
async fn select_projects_records_to_requested_properties() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let (status, body) = get_json(app, "/cloudmap?kind=artifacts&select=/type,$key").await;
    assert_eq!(status, StatusCode::OK);
    let artifacts = body[0]["artifacts"].as_object().expect("artifacts");
    assert!(!artifacts.is_empty());
    for (key, record) in artifacts {
        let record = record.as_object().expect("record object");
        // Exactly the selected properties — no digest, metadata, or
        // `unfurl.server.*` annotations.
        assert!(record.len() <= 2, "unexpected keys: {:?}", record.keys());
        assert_eq!(record["$key"], Value::from(key.as_str()));
        assert!(record.get("type").is_some(), "artifacts declare a type");
    }
}

#[tokio::test]
async fn select_reconstructs_nested_paths_and_drops_missing() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let key = "git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template";
    let uri = format!(
        "/cloudmap?kind=artifacts&key={}&select=/metadata/title,/no/such/path",
        urlencoding::encode(key)
    );
    let (status, body) = get_json(app, &uri).await;
    assert_eq!(status, StatusCode::OK);
    let record = &body[0]["artifacts"][key];
    assert_eq!(
        record,
        &serde_json::json!({"metadata": {"title": "Odoo"}}),
        "nested structure kept, unresolvable path omitted"
    );
}

#[tokio::test]
async fn select_applies_to_followed_records_too() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let key = "git://unfurl.cloud/onecommons/blueprints/odoo.git";
    let uri = format!(
        "/cloudmap?kind=repositories&key={}&follow=10&select=$key",
        urlencoding::encode(key)
    );
    let (status, body) = get_json(app, &uri).await;
    assert_eq!(status, StatusCode::OK);
    let followed = body[1].as_object().expect("followed object");
    assert!(!followed.is_empty(), "follow=10 reaches records");
    for (_section, records) in followed {
        for (k, record) in records.as_object().expect("records") {
            assert_eq!(
                record,
                &serde_json::json!({"$key": k}),
                "followed records are projected too"
            );
        }
    }
}

#[tokio::test]
async fn select_prefix_dedup_and_bare_paths() {
    // `/metadata` covers `/metadata/title`; a path without a leading
    // slash gets one prepended; `unfurl.server.*` keys are selectable
    // because projection runs after annotation.
    let (cm, _tmp) = open_cloudmap_state().await;
    let state = make_state(cm);
    let key = "git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template";

    let uri = format!(
        "/cloudmap?kind=artifacts&key={}&select=/metadata,/metadata/title",
        urlencoding::encode(key)
    );
    let (status, body) = get_json(router(state.clone()), &uri).await;
    assert_eq!(status, StatusCode::OK);
    let metadata = &body[0]["artifacts"][key]["metadata"];
    assert_eq!(metadata["title"], "Odoo");
    assert_eq!(metadata["version"], 0.1, "whole subtree selected");

    let uri = format!(
        "/cloudmap?kind=artifacts&key={}&select=digest,unfurl.server.version",
        urlencoding::encode(key)
    );
    let (status, body) = get_json(router(state), &uri).await;
    assert_eq!(status, StatusCode::OK);
    let record = body[0]["artifacts"][key].as_object().expect("record");
    assert!(record["digest"].is_string());
    assert!(record["unfurl.server.version"].is_i64());
    assert_eq!(record.len(), 2, "keys: {:?}", record.keys());
}

// ---------------------------------------------------------------------------
// cloudmap_path — scoping reads and writes to one file
// ---------------------------------------------------------------------------

/// A second, minimal cloudmap document living alongside the fixture.
/// `CloudMapFormat::is_format` keys off `kind: CloudMap`, not the file
/// name, so this is indexed as its own file in the same worktree.
const ALT_FILE: &str = "alt-cloudmap.yaml";
const ALT_KEY: &str = "git://example.com/only-in-alt.git";

/// Repo seeded with two cloudmap files: the shared fixture at
/// `cloudmap.yaml` and [`ALT_FILE`] holding a single repository record.
async fn open_two_file_state() -> (CloudMapState, SyncedRepo, TempDir) {
    let tmp = tempfile::tempdir().expect("tempdir");
    let fixture =
        std::fs::read(Path::new(env!("CARGO_MANIFEST_DIR")).join(FIXTURE)).expect("fixture exists");
    let alt = format!(
        "apiVersion: unfurl/v1alpha1\nkind: CloudMap\nrepositories:\n  {ALT_KEY}:\n    git: {ALT_KEY}\n    path: only/in-alt\n    name: only-in-alt\n"
    );
    unfurl_git_sync::git::init_with_files(
        tmp.path(),
        &[
            ("cloudmap.yaml".to_string(), fixture),
            (ALT_FILE.to_string(), alt.into_bytes()),
        ],
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
    synced.update_from_working_dir().await.expect("update");
    (CloudMapState::from_synced(synced.clone()), synced, tmp)
}

#[tokio::test]
async fn get_without_cloudmap_path_spans_every_file() {
    let (cm, _synced, _tmp) = open_two_file_state().await;
    let app = router(make_state(cm));
    let (status, body) = get_json(app, "/cloudmap?kind=repositories").await;
    assert_eq!(status, StatusCode::OK);
    let repos = &body[0]["repositories"];
    assert!(
        repos.get(ALT_KEY).is_some(),
        "unscoped read should include the alt file's record: {repos:?}"
    );
    assert!(
        repos.as_object().expect("object").len() > 1,
        "unscoped read should also include the fixture's records"
    );
}

#[tokio::test]
async fn get_with_cloudmap_path_scopes_to_that_file() {
    let (cm, _synced, _tmp) = open_two_file_state().await;
    let app = router(make_state(cm.clone()));
    let (status, body) = get_json(
        app,
        &format!("/cloudmap?kind=repositories&cloudmap_path={ALT_FILE}"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let repos = body[0]["repositories"].as_object().expect("object");
    assert_eq!(
        repos.keys().collect::<Vec<_>>(),
        vec![ALT_KEY],
        "scoped read should return only the alt file's record"
    );

    // ... and the fixture file doesn't see the alt record.
    let app = router(make_state(cm));
    let (status, body) = get_json(
        app,
        "/cloudmap?kind=repositories&cloudmap_path=cloudmap.yaml",
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert!(
        body[0]["repositories"].get(ALT_KEY).is_none(),
        "cloudmap.yaml must not report the alt file's record"
    );
}

#[tokio::test]
async fn get_with_cloudmap_path_and_key_404s_across_files() {
    let (cm, _synced, _tmp) = open_two_file_state().await;
    let app = router(make_state(cm));
    // ALT_KEY exists, but not in cloudmap.yaml.
    let (status, _body) = get_json(
        app,
        &format!(
            "/cloudmap?kind=repositories&cloudmap_path=cloudmap.yaml&key={}",
            urlencoding::encode(ALT_KEY)
        ),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn post_with_cloudmap_path_writes_to_that_file() {
    let (cm, synced, _tmp) = open_two_file_state().await;
    let app = router(make_state(cm));
    let new_url = "git://example.com/written-to-alt.git";
    let body = serde_json::json!({
        "cloudmap_path": ALT_FILE,
        "repositories": {
            new_url: { "name": "written-to-alt" },
        }
    });
    let (status, _echo) = post_json(app, body).await;
    assert_eq!(status, StatusCode::OK);

    // It landed in the requested file, not the worktree default.
    let rec = synced
        .get_record(ALT_FILE, "/repositories", new_url)
        .await
        .expect("get_record")
        .expect("present in the alt file");
    assert_eq!(rec.file_path, ALT_FILE);
    assert!(
        synced
            .get_record("cloudmap.yaml", "/repositories", new_url)
            .await
            .expect("get_record")
            .is_none(),
        "the record must not also appear in the default file"
    );
}
