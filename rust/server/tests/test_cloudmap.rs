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
    encode_page_token, handle_cloudmap, handle_cloudmap_facets, post_cloudmap_local,
    post_cloudmap_proxy, CloudMapState,
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
        cloudmap_force: false,
        // These fixtures stand in for a server started on a local checkout
        // (`unfurl serve <path>`), which is what turns off the auth_project
        // check. The auth_project tests below build a strict config instead.
        local: Some("/tmp/checkout".into()),
    }
}

/// A multi-tenant config: `auth_project` is checked against the repository.
fn strict_config() -> Config {
    Config {
        local: None,
        ..default_config()
    }
}

fn make_strict_state(cm: CloudMapState) -> AppState {
    AppState {
        config: Arc::new(strict_config()),
        ..make_state(cm)
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
        .update_from_working_dir(unfurl_git_sync::ScanOptions::default())
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
        .route("/cloudmap/facets", get(handle_cloudmap_facets))
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

/// POST with an `auth_project` naming the repo under test. The fixture repos
/// have no remote, so any value satisfies the check; passing one keeps these
/// tests representative of a real client.
async fn post_json(app: Router, body: Value) -> (StatusCode, Value) {
    post_json_as(app, body, Some("onecommons/cloudmap")).await
}

async fn post_json_as(app: Router, body: Value, auth_project: Option<&str>) -> (StatusCode, Value) {
    post_json_with_headers(app, body, auth_project, &[]).await
}

/// [`post_json_as`] plus arbitrary request headers, for the ones the
/// handler reads off the request itself (`X-Unfurl-User`).
async fn post_json_with_headers(
    app: Router,
    body: Value,
    auth_project: Option<&str>,
    headers: &[(&str, &str)],
) -> (StatusCode, Value) {
    let uri = match auth_project {
        Some(p) => format!("/cloudmap?auth_project={}", urlencoding::encode(p)),
        None => "/cloudmap".to_string(),
    };
    let mut builder = Request::builder()
        .method("POST")
        .uri(uri)
        .header("content-type", "application/json");
    for (name, value) in headers {
        builder = builder.header(*name, *value);
    }
    let req = builder
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
    let obj = body.as_object().expect("response object");
    let result = &obj["result"];
    // The result must contain the cloudmap-shaped sections present in the fixture.
    assert!(result.get("repositories").is_some());
    assert!(result.get("artifacts").is_some());
    // No key was supplied, so nothing was followed and the key is absent.
    assert!(!obj.contains_key("followed"));
}

#[tokio::test]
async fn kind_only_returns_one_section() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let (status, body) = get_json(app, "/cloudmap?kind=repositories").await;
    assert_eq!(status, StatusCode::OK);
    let primary = &body["result"];
    assert!(primary.get("repositories").is_some());
    assert!(
        primary.get("artifacts").is_none(),
        "kind=repositories must not include artifacts"
    );
    assert!(
        body.get("followed").is_none(),
        "no follow was requested, so the key is absent"
    );
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
    let primary = &body["result"];
    let repos = primary.get("repositories").expect("repositories section");
    assert_eq!(repos.as_object().unwrap().len(), 1);
    assert!(repos.get(key).is_some(), "expected key under repositories");
    assert!(
        body.get("followed").is_none(),
        "no follow was requested, so the key is absent"
    );
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
    let followed = &body["followed"];
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
    let primary = &body["result"];
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
    let followed = &body["followed"];
    let total: usize = followed
        .as_object()
        .unwrap()
        .values()
        .map(|sec| sec.as_object().map(|m| m.len()).unwrap_or(0))
        .sum();
    assert_eq!(total, 2, "follow=2 caps the followed set at 2 records");
}

#[tokio::test]
async fn follow_zero_omits_followed() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let key = "git://unfurl.cloud/onecommons/blueprints/odoo.git";
    let uri = format!(
        "/cloudmap?kind=repositories&key={}&follow=0",
        urlencoding::encode(key)
    );
    let (status, body) = get_json(app, &uri).await;
    assert_eq!(status, StatusCode::OK);
    assert!(
        body.get("followed").is_none(),
        "follow=0 asks for no walk, so the key is absent"
    );
}

#[tokio::test]
async fn follow_without_key_walks_from_the_matches() {
    // A walk needs no starting key: it seeds from every record the query
    // selected, so a filtered query returns its neighbourhood.
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let (status, body) = get_json(app, "/cloudmap?kind=repositories&follow=100").await;
    assert_eq!(status, StatusCode::OK);
    let roots = body["result"]["repositories"]
        .as_object()
        .expect("repositories");
    assert!(!roots.is_empty(), "fixture should have roots to walk from");

    let followed = body["followed"].as_object().expect("followed");
    assert!(
        !followed.is_empty(),
        "the repositories reference records in other sections: {body:?}"
    );
    let reached: Vec<&String> = followed.keys().collect();
    assert!(
        reached.iter().any(|s| *s != "repositories"),
        "should reach other sections: {reached:?}"
    );
}

#[tokio::test]
async fn follow_caps_a_keyless_walk() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let (status, body) = get_json(app, "/cloudmap?kind=repositories&follow=3").await;
    assert_eq!(status, StatusCode::OK);
    let total: usize = body["followed"]
        .as_object()
        .expect("followed")
        .values()
        .map(|sec| sec.as_object().map(|m| m.len()).unwrap_or(0))
        .sum();
    assert_eq!(total, 3, "follow caps the walk regardless of root count");
}

#[tokio::test]
async fn get_filter_searches_record_contents() {
    // `filter=<json pointer>=<value>` is pushed into the SQL WHERE clause
    // (`json_each`), so the database does the filtering. The same predicate
    // covers arrays, objects and scalars: no record shape is assumed.
    let (cm, _tmp) = open_cloudmap_state().await;

    // an array: `metadata/discovery/sources` contains the url
    let app = router(make_state(cm.clone()));
    let (status, body) = get_json(
        app,
        &format!(
            "/cloudmap?filter={}",
            urlencoding::encode(
                "/metadata/discovery/sources=https://hub.docker.com/v2/repositories/bitnami/odoo/"
            )
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let artifacts = body["result"]["artifacts"]
        .as_object()
        .expect("artifacts matched");
    assert_eq!(artifacts.len(), 1, "one artifact should match: {body:?}");
    assert!(
        artifacts
            .keys()
            .next()
            .expect("key")
            .starts_with("pkg:oci/odoo"),
        "unexpected match: {body:?}"
    );
    assert!(
        body["result"].get("repositories").is_none(),
        "sections without a match are omitted: {body:?}"
    );

    // a scalar: `metadata/homepage_url` equals the url
    let app = router(make_state(cm.clone()));
    let (status, body) = get_json(
        app,
        &format!(
            "/cloudmap?filter={}",
            urlencoding::encode("/metadata/homepage_url=https://unfurl.cloud/feb20a/dashboard")
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        body["result"]["repositories"].as_object().map(|m| m.len()),
        Some(1),
        "one repository should match: {body:?}"
    );

    // combines with `kind`
    let app = router(make_state(cm.clone()));
    let (status, body) = get_json(
        app,
        &format!(
            "/cloudmap?kind=repositories&filter={}",
            urlencoding::encode("/metadata/homepage_url=https://unfurl.cloud/feb20a/dashboard")
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        body["result"]["repositories"].as_object().map(|m| m.len()),
        Some(1)
    );

    // no match -> empty document, not an error
    let app = router(make_state(cm.clone()));
    let (status, body) = get_json(
        app,
        &format!(
            "/cloudmap?filter={}",
            urlencoding::encode("/metadata/homepage_url=https://nope.example.com")
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert!(
        body["result"]
            .as_object()
            .map(|m| m.is_empty())
            .unwrap_or(false),
        "no matches should return an empty document: {body:?}"
    );

    // malformed filter -> 400. A filter with no "=" is an existence test
    // rather than an error, so this uses an empty path segment instead.
    let app = router(make_state(cm));
    let (status, body) = get_json(app, "/cloudmap?filter=//empty/segment").await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body:?}");
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
    body["result"][kind][key]["unfurl.server.version"]
        .as_i64()
        .expect("version present")
}

#[tokio::test]
async fn post_upsert_writes_record() {
    let (cm, tmp) = open_cloudmap_state().await;
    let key = "git://unfurl.cloud/onecommons/std.git";
    let head = head_oid(tmp.path());

    let v_before = read_version(cm.clone(), "repositories", key).await;

    let app = router(make_state(cm.clone()));
    let body = serde_json::json!({
        "repositories": {
            key: { "name": "renamed-via-post" },
        }
    });
    let (status, response) = post_json(app, body).await;
    assert_eq!(status, StatusCode::OK);
    // The Rust handler stages records as in-flight (no git commit yet), so
    // `commit` reports the repository's unchanged HEAD — same as the Python
    // handler answering with `repo.revision` when it didn't commit either. The
    // OCC token for a staged write is `queueid`, not this.
    assert_eq!(
        response.get("commit").and_then(Value::as_str),
        Some(head.as_str()),
        "staged write should report the unchanged HEAD: {response:?}"
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
async fn post_and_get_every_section() {
    // The handler keeps two independent lists of cloudmap sections -- `KIND_TO_PATH`
    // for reads and the `collect!` calls for writes -- and a section missing from
    // either fails silently: the POST answers 200 and writes nothing, or the GET
    // can't map the kind. `components` was missing from both.
    //
    // The list below is deliberately *not* derived from the handler's: a test that
    // iterated over `KIND_TO_PATH` would skip exactly the section that went missing.
    let cases: Vec<(&str, &str, serde_json::Value)> = vec![
        (
            "repositories",
            "git://example.com/new-repo.git",
            serde_json::json!({ "path": "example/new-repo" }),
        ),
        (
            "artifacts",
            "pkg:oci/new-image?repository_url=docker.io/library/new-image",
            serde_json::json!({ "digest": "sha256:abc" }),
        ),
        (
            "components",
            "software.PostgresSchema@example.org",
            serde_json::json!({}),
        ),
        (
            "services",
            "https://example.com/new-service",
            serde_json::json!({ "access": "public" }),
        ),
        (
            "instantiations",
            "https://ci.example.com/runs/42",
            serde_json::json!({ "revision": "abc123" }),
        ),
        ("types", "NewType@example.org", serde_json::json!({})),
    ];

    let (cm, _tmp) = open_cloudmap_state().await;
    for (kind, key, extra) in cases {
        let title = format!("posted-{kind}");
        let mut record = extra.as_object().cloned().unwrap_or_default();
        record.insert(
            "metadata".to_string(),
            serde_json::json!({ "title": title }),
        );
        let body = serde_json::json!({ kind: { key: record } });

        let (status, response) = post_json(router(make_state(cm.clone())), body).await;
        assert_eq!(status, StatusCode::OK, "POST {kind} failed: {response:?}");

        let uri = format!("/cloudmap?kind={kind}&key={}", urlencoding::encode(key));
        let (status, got) = get_json(router(make_state(cm.clone())), &uri).await;
        assert_eq!(status, StatusCode::OK, "GET {kind} failed: {got:?}");
        assert_eq!(
            got["result"][kind][key]["metadata"]["title"].as_str(),
            Some(title.as_str()),
            "{kind}/{key} did not round trip: {got:?}"
        );
    }
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
    synced
        .update_from_working_dir(unfurl_git_sync::ScanOptions::default())
        .await
        .expect("update");

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
    synced
        .update_from_working_dir(unfurl_git_sync::ScanOptions::default())
        .await
        .expect("update");

    let key = "git://unfurl.cloud/onecommons/std.git";
    // Mutate, save, commit so the record now has a non-null commit_id.
    synced
        .upsert_record(
            None,
            "/repositories",
            key,
            serde_json::json!({"name": "via-test"}),
            None,
            false,
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
    let primary = body["result"].as_object().expect("primary object");
    assert_eq!(
        primary.keys().collect::<Vec<_>>(),
        vec!["artifacts"],
        "only artifacts declare the pipeline type"
    );
    let artifacts = primary["artifacts"].as_object().expect("artifacts");
    assert_eq!(artifacts.len(), 4, "keys: {:?}", artifacts.keys());
    assert!(artifacts.keys().all(|k| k.ends_with(".gitlab-ci.yml")));
    assert!(
        body.get("followed").is_none(),
        "no follow was requested, so the key is absent"
    );
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
    let primary = body["result"].as_object().expect("primary object");
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
    assert_eq!(body["result"], serde_json::json!({}));
    assert!(
        body.get("followed").is_none(),
        "no follow was requested, so the key is absent"
    );
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
    assert!(body["result"]["services"].get(key).is_some());

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
    let before = body["result"]["artifacts"]
        .as_object()
        .expect("artifacts")
        .len();
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
    let artifacts = body["result"]["artifacts"].as_object().expect("artifacts");
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
    let artifacts = body["result"]["artifacts"].as_object().expect("artifacts");
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
    let record = &body["result"]["artifacts"][key];
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
    let followed = body["followed"].as_object().expect("followed object");
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
    let metadata = &body["result"]["artifacts"][key]["metadata"];
    assert_eq!(metadata["title"], "Odoo");
    assert_eq!(metadata["version"], 0.1, "whole subtree selected");

    let uri = format!(
        "/cloudmap?kind=artifacts&key={}&select=digest,unfurl.server.version",
        urlencoding::encode(key)
    );
    let (status, body) = get_json(router(state), &uri).await;
    assert_eq!(status, StatusCode::OK);
    let record = body["result"]["artifacts"][key]
        .as_object()
        .expect("record");
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
    synced
        .update_from_working_dir(unfurl_git_sync::ScanOptions::default())
        .await
        .expect("update");
    (CloudMapState::from_synced(synced.clone()), synced, tmp)
}

#[tokio::test]
async fn get_without_cloudmap_path_spans_every_file() {
    let (cm, _synced, _tmp) = open_two_file_state().await;
    let app = router(make_state(cm));
    let (status, body) = get_json(app, "/cloudmap?kind=repositories").await;
    assert_eq!(status, StatusCode::OK);
    let repos = &body["result"]["repositories"];
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
    let repos = body["result"]["repositories"].as_object().expect("object");
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
        body["result"]["repositories"].get(ALT_KEY).is_none(),
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

#[tokio::test]
async fn post_with_new_cloudmap_path_creates_the_file() {
    let (cm, synced, tmp) = open_two_file_state().await;
    let app = router(make_state(cm));
    let new_file = "brand-new-cloudmap.yaml";
    let new_url = "git://example.com/in-new-file.git";
    let body = serde_json::json!({
        "cloudmap_path": new_file,
        "repositories": {
            new_url: { "path": "in/new-file", "name": "in-new-file" },
        }
    });
    let (status, echo) = post_json(app, body).await;
    assert_eq!(status, StatusCode::OK, "response: {echo:?}");

    let rec = synced
        .get_record(new_file, "/repositories", new_url)
        .await
        .expect("get_record")
        .expect("record staged against the new file");
    assert_eq!(rec.file_path, new_file);

    // Flushing pending records synthesises the file on disk.
    let written = synced.write_file(new_file).await.expect("write_file");
    assert!(
        written.written.is_some(),
        "write_file should have created the file"
    );
    let on_disk = std::fs::read_to_string(tmp.path().join(new_file)).expect("file exists");
    assert!(on_disk.contains("in-new-file"), "{on_disk}");

    // The synthesised document must be a *valid cloudmap*, not just the
    // records: `apiVersion` and `kind` are both required by
    // `unfurl/cloudmap/cloudmap-schema.json`, and `kind: CloudMap` is what
    // `CloudMapFormat::is_format` keys off — without it the next
    // `update_from_working_dir` scan skips the file entirely and the records
    // fall out of the index.
    let doc: serde_json::Value = serde_saphyr::from_str(&on_disk).expect("parses as YAML");
    assert_eq!(
        doc.get("kind").and_then(|v| v.as_str()),
        Some("CloudMap"),
        "synthesised file needs the cloudmap `kind` header: {on_disk}"
    );
    assert_eq!(
        doc.get("apiVersion").and_then(|v| v.as_str()),
        Some("unfurl/v1.0.0"),
        "synthesised file needs the `apiVersion` header: {on_disk}"
    );

    // ... and a rescan of the worktree recognises it as a cloudmap, so the
    // record survives a round-trip through the scanner.
    synced
        .update_from_working_dir(unfurl_git_sync::ScanOptions::default())
        .await
        .expect("rescan");
    let rescanned = synced
        .get_record(new_file, "/repositories", new_url)
        .await
        .expect("get_record")
        .expect("record still indexed after a rescan");
    assert_eq!(rescanned.json["name"], "in-new-file");
}

#[tokio::test]
async fn commit_repository_commits_a_newly_created_cloudmap() {
    let (cm, synced, tmp) = open_two_file_state().await;
    let app = router(make_state(cm));
    // A path whose parent directory isn't in HEAD's tree either.
    let new_file = "maps/nested/brand-new-cloudmap.yaml";
    let new_url = "git://example.com/committed-from-new-file.git";
    let (status, echo) = post_json(
        app,
        serde_json::json!({
            "cloudmap_path": new_file,
            "repositories": { new_url: { "path": "in/new", "name": "committed" } },
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "response: {echo:?}");

    let oid = synced
        .commit_repository("add a new cloudmap")
        .await
        .expect("commit_repository")
        .expect("something was dirty, so a commit was made");

    // The file is in the commit's tree even though it was never tracked:
    // `git::commit_paths` builds the tree directly, inserting missing
    // subtrees, rather than staging through the index.
    let out = std::process::Command::new("git")
        .args(["ls-tree", "-r", "--name-only", &oid])
        .current_dir(tmp.path())
        .output()
        .expect("git ls-tree");
    let listed = String::from_utf8_lossy(&out.stdout);
    assert!(
        listed.lines().any(|l| l == new_file),
        "{new_file} missing from commit {oid}:\n{listed}"
    );

    // ... and its content round-trips as a valid cloudmap.
    let on_disk = std::fs::read_to_string(tmp.path().join(new_file)).expect("on disk");
    let doc: serde_json::Value = serde_saphyr::from_str(&on_disk).expect("parses");
    assert_eq!(doc.get("kind").and_then(|v| v.as_str()), Some("CloudMap"));
    assert!(doc["repositories"].get(new_url).is_some(), "{on_disk}");
}

// ---------------------------------------------------------------------------
// commit flag
// ---------------------------------------------------------------------------

/// HEAD's oid, for asserting whether a commit actually happened.
fn head_oid(dir: &Path) -> String {
    let out = std::process::Command::new("git")
        .args(["rev-parse", "HEAD"])
        .current_dir(dir)
        .output()
        .expect("git rev-parse");
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

#[tokio::test]
async fn post_without_commit_flag_stages_only() {
    let (cm, _synced, tmp) = open_two_file_state().await;
    let before = head_oid(tmp.path());
    let app = router(make_state(cm));
    let key = "git://unfurl.cloud/onecommons/std.git";
    let (status, response) = post_json(
        app,
        serde_json::json!({ "repositories": { key: { "name": "staged-only" } } }),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        response.get("commit").and_then(Value::as_str),
        Some(before.as_str()),
        "default is still in-flight staging, so HEAD is reported unchanged: {response:?}"
    );
    assert_eq!(head_oid(tmp.path()), before, "HEAD must not have moved");
}

#[tokio::test]
async fn post_with_commit_flag_commits() {
    let (cm, _synced, tmp) = open_two_file_state().await;
    let before = head_oid(tmp.path());
    let app = router(make_state(cm));
    let key = "git://unfurl.cloud/onecommons/std.git";
    let (status, response) = post_json(
        app,
        serde_json::json!({
            "commit": true,
            "commit_msg": "committed by the handler",
            "repositories": { key: { "name": "committed-by-handler" } },
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{response:?}");
    let oid = response["commit"].as_str().expect("a commit oid");
    assert_ne!(oid, before, "HEAD should have advanced");
    assert_eq!(head_oid(tmp.path()), oid);

    // `commit_msg` from the request body is used for the commit.
    let out = std::process::Command::new("git")
        .args(["log", "-1", "--format=%s"])
        .current_dir(tmp.path())
        .output()
        .expect("git log");
    assert_eq!(
        String::from_utf8_lossy(&out.stdout).trim(),
        "committed by the handler"
    );

    // The change reached the file on disk, too.
    let on_disk = std::fs::read_to_string(tmp.path().join("cloudmap.yaml")).expect("read");
    assert!(on_disk.contains("committed-by-handler"), "{on_disk}");
}

/// The commit message body (everything after the subject) of HEAD.
fn head_commit_body(dir: &Path) -> String {
    let out = std::process::Command::new("git")
        .args(["log", "-1", "--format=%b"])
        .current_dir(dir)
        .output()
        .expect("git log");
    assert!(out.status.success(), "{out:?}");
    String::from_utf8_lossy(&out.stdout).to_string()
}

#[tokio::test]
async fn commit_body_attributes_the_write_to_the_x_unfurl_user_header() {
    let (cm, _synced, tmp) = open_two_file_state().await;
    let app = router(make_state(cm));
    let key = "git://unfurl.cloud/onecommons/std.git";
    let (status, response) = post_json_with_headers(
        app,
        serde_json::json!({
            "commit": true,
            "commit_msg": "Recover from tuple patterns",
            "repositories": { key: { "name": "attributed" } },
        }),
        Some("onecommons/cloudmap"),
        &[("X-Unfurl-User", "Adam Souzis <adam@souzis.com>")],
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{response:?}");

    let body = head_commit_body(tmp.path());
    assert!(
        body.starts_with("Rollup of 1 git-sync transaction:\n"),
        "{body}"
    );
    assert!(body.contains("Adam Souzis <adam@souzis.com>\n"), "{body}");
    // `commit_msg` is both the subject and this batch's own message.
    assert!(
        body.contains("\n   | Recover from tuple patterns\n"),
        "{body}"
    );
    // And the header value reaches the audit trail intact, not just the
    // prose: `%b` drops the subject, so restore one to parse.
    let rollup = unfurl_git_sync::parse_commit_rollup(&format!("x\n\n{body}"))
        .expect("parses")
        .expect("a git-sync commit");
    assert_eq!(rollup.txns.len(), 1, "{body}");
    assert_eq!(
        rollup.txns[0].author.as_deref(),
        Some("Adam Souzis <adam@souzis.com>")
    );
    assert_eq!(rollup.txns[0].records.len(), 1, "{body}");
    assert_eq!(rollup.txns[0].records[0].path, "/repositories", "{body}");
}

#[tokio::test]
async fn commit_body_rolls_up_every_staged_writers_attribution() {
    let (cm, _synced, tmp) = open_two_file_state().await;
    let std_key = "git://unfurl.cloud/onecommons/std.git";
    let other_key = "git://unfurl.cloud/feb20a/dashboard.git";

    // Two staged writes by different people, on distinct keys; neither
    // commits. Without the audit table only the third request's message
    // would survive into the commit.
    for (author, key, name, msg) in [
        (
            "Ada <ada@example.com>",
            std_key,
            "by-ada",
            Some("Point std at the new branch"),
        ),
        // No `commit_msg` — the handler's default is recorded instead.
        ("bob@example.com", other_key, "by-bob", None),
    ] {
        let mut body = serde_json::json!({
            "repositories": { key: { "name": name } },
        });
        if let Some(msg) = msg {
            body["commit_msg"] = serde_json::json!(msg);
        }
        let app = router(make_state(cm.clone()));
        let (status, response) = post_json_with_headers(
            app,
            body,
            Some("onecommons/cloudmap"),
            &[("X-Unfurl-User", author)],
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{response:?}");
    }

    // A bare `commit: true` carries both staged batches into one commit.
    let app = router(make_state(cm));
    let (status, response) = post_json(app, serde_json::json!({ "commit": true })).await;
    assert_eq!(status, StatusCode::OK, "{response:?}");

    let body = head_commit_body(tmp.path());
    assert!(
        body.starts_with("Rollup of 2 git-sync transactions:\n"),
        "{body}"
    );
    assert!(body.contains("Ada <ada@example.com>\n"), "{body}");
    assert!(
        body.contains("\n   | Point std at the new branch\n"),
        "{body}"
    );
    assert!(body.contains("bob@example.com\n"), "{body}");
    assert!(body.contains("\n   | Update cloudmap\n"), "{body}");
    // The handler's writes are attributed to each author's own batch.
    let rollup = unfurl_git_sync::parse_commit_rollup(&format!("x\n\n{body}"))
        .expect("parses")
        .expect("a git-sync commit");
    assert_eq!(rollup.txns.len(), 2, "{body}");
    assert_eq!(
        rollup.txns[0].author.as_deref(),
        Some("Ada <ada@example.com>")
    );
    assert_eq!(rollup.txns[1].author.as_deref(), Some("bob@example.com"));
    for txn in &rollup.txns {
        assert_eq!(txn.records.len(), 1, "one record each: {body}");
        assert!(!txn.records[0].deleted, "{body}");
    }
}

#[tokio::test]
async fn empty_post_with_commit_flag_commits_whatever_is_staged() {
    let (cm, _synced, tmp) = open_two_file_state().await;
    let before = head_oid(tmp.path());
    let key = "git://unfurl.cloud/onecommons/std.git";

    // First request stages without committing (the default).
    let app = router(make_state(cm.clone()));
    let (status, _) = post_json(
        app,
        serde_json::json!({ "repositories": { key: { "name": "staged-then-committed" } } }),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(head_oid(tmp.path()), before);

    // Second request carries no records at all — just `commit: true`.
    let app = router(make_state(cm.clone()));
    let (status, response) = post_json(app, serde_json::json!({ "commit": true })).await;
    assert_eq!(status, StatusCode::OK, "{response:?}");
    let oid = response["commit"].as_str().expect("a commit oid");
    assert_ne!(oid, before);
    let on_disk = std::fs::read_to_string(tmp.path().join("cloudmap.yaml")).expect("read");
    assert!(on_disk.contains("staged-then-committed"), "{on_disk}");

    // Nothing left staged, so a repeat commit is a no-op.
    let app = router(make_state(cm));
    let (status, response) = post_json(app, serde_json::json!({ "commit": true })).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        response.get("commit").and_then(Value::as_str),
        Some(oid),
        "nothing dirty -> no new commit, HEAD reported as-is: {response:?}"
    );
    assert_eq!(head_oid(tmp.path()), oid, "HEAD must not have moved again");
}

#[tokio::test]
async fn commit_leaves_the_working_tree_clean() {
    let (cm, _synced, tmp) = open_two_file_state().await;
    let app = router(make_state(cm));
    let key = "git://unfurl.cloud/onecommons/std.git";
    let (status, response) = post_json(
        app,
        serde_json::json!({
            "commit": true,
            "repositories": { key: { "name": "clean-after-commit" } },
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{response:?}");

    // `commit_paths` builds the tree directly instead of staging through the
    // index, so the index has to be rewritten from the new tree — otherwise
    // git reports the committed file as modified both staged and unstaged.
    let out = std::process::Command::new("git")
        .args(["status", "--porcelain"])
        .current_dir(tmp.path())
        .output()
        .expect("git status");
    let status_out = String::from_utf8_lossy(&out.stdout);
    assert!(
        status_out.trim().is_empty(),
        "working tree should be clean after commit, got:\n{status_out}"
    );
}

// ---------------------------------------------------------------------------
// auth_project scoping
// ---------------------------------------------------------------------------

const REMOTE_PROJECT: &str = "onecommons/cloudmap";

/// Like [`open_cloudmap_state`] but with an `origin` remote configured, so the
/// worktree has a project identity to check `auth_project` against. Without a
/// remote the check can't discriminate and everything is served locally.
async fn open_state_with_remote() -> (CloudMapState, TempDir) {
    let tmp = tempfile::tempdir().expect("tempdir");
    let fixture =
        std::fs::read(Path::new(env!("CARGO_MANIFEST_DIR")).join(FIXTURE)).expect("fixture exists");
    unfurl_git_sync::git::init_with_files(
        tmp.path(),
        &[("cloudmap.yaml".to_string(), fixture)],
        "initial",
    )
    .expect("init repo");
    let out = std::process::Command::new("git")
        .args([
            "remote",
            "add",
            "origin",
            &format!("https://unfurl.cloud/{REMOTE_PROJECT}.git"),
        ])
        .current_dir(tmp.path())
        .output()
        .expect("git remote add");
    assert!(out.status.success(), "{out:?}");

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
        .update_from_working_dir(unfurl_git_sync::ScanOptions::default())
        .await
        .expect("update");
    (CloudMapState::from_synced(synced), tmp)
}

#[tokio::test]
async fn post_without_auth_project_is_rejected_when_repo_has_a_remote() {
    let (cm, _tmp) = open_state_with_remote().await;
    let app = router(make_strict_state(cm));
    let (status, body) = post_json_as(
        app,
        serde_json::json!({ "repositories": {} }),
        None, // no auth_project
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body:?}");
    assert_eq!(body["code"], "BAD_REQUEST");
    assert!(
        body["message"]
            .as_str()
            .unwrap_or("")
            .contains("auth_project"),
        "{body:?}"
    );
}

#[tokio::test]
async fn post_with_matching_auth_project_is_served_locally() {
    let (cm, _tmp) = open_state_with_remote().await;
    let app = router(make_strict_state(cm));
    let key = "git://unfurl.cloud/onecommons/std.git";
    let (status, body) = post_json_as(
        app,
        serde_json::json!({ "repositories": { key: { "name": "scoped-write" } } }),
        Some(REMOTE_PROJECT),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
}

#[tokio::test]
async fn requests_for_another_project_are_proxied() {
    let (cm, _tmp) = open_state_with_remote().await;
    // No python backend is running in this test, so a proxied request fails at
    // the connection — a 502 is how we observe that the handler did *not*
    // answer from its own repository.
    let app = router(make_strict_state(cm.clone()));
    let (status, _body) = get_json(app, "/cloudmap?auth_project=someone/else").await;
    assert_eq!(
        status,
        StatusCode::BAD_GATEWAY,
        "a read for another project must go to python, not the local repo"
    );

    let app = router(make_strict_state(cm));
    let (status, _body) = post_json_as(
        app,
        serde_json::json!({ "repositories": {} }),
        Some("someone/else"),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::BAD_GATEWAY,
        "a write for another project must go to python"
    );
}

#[tokio::test]
async fn dev_mode_serves_a_remote_backed_repo_without_auth_project() {
    // The case the switch exists for: a local checkout that still has a
    // remote, so its origin looks like a real project id and the strict check
    // would demand a matching `auth_project`.
    let (cm, _tmp) = open_state_with_remote().await;
    let app = router(make_state(cm.clone())); // default_config() => dev_mode
    let key = "git://unfurl.cloud/onecommons/std.git";
    let (status, body) = post_json_as(
        app,
        serde_json::json!({ "repositories": { key: { "name": "dev-mode-write" } } }),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");

    // ... and it doesn't proxy a request for another project either: in dev
    // mode there is only the one repository to serve.
    let app = router(make_state(cm));
    let (status, body) =
        get_json(app, "/cloudmap?kind=repositories&auth_project=someone/else").await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
}

// ---------------------------------------------------------------------------
// branch scoping
// ---------------------------------------------------------------------------

/// The branch the fixture repo landed on. `gix::init` follows the machine's
/// `init.defaultBranch`, so the tests derive the name rather than assuming
/// `main` — otherwise they'd pass or fail with the runner's git config.
async fn worktree_branch(cm: &CloudMapState) -> String {
    cm.synced()
        .get_worktree()
        .await
        .expect("worktree row")
        .branch
}

#[tokio::test]
async fn branch_naming_the_checked_out_ref_is_served_locally() {
    let (cm, _tmp) = open_state_with_remote().await;
    let branch = worktree_branch(&cm).await;

    let app = router(make_strict_state(cm.clone()));
    let (status, body) = get_json(
        app,
        &format!("/cloudmap?kind=repositories&auth_project={REMOTE_PROJECT}&branch={branch}"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert!(body["result"].get("repositories").is_some(), "{body:?}");

    // The full ref names the same branch.
    let app = router(make_strict_state(cm));
    let (status, body) = get_json(
        app,
        &format!(
            "/cloudmap?kind=repositories&auth_project={REMOTE_PROJECT}&branch={}",
            urlencoding::encode(&format!("refs/heads/{branch}"))
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
}

#[tokio::test]
async fn requests_for_another_branch_are_proxied() {
    let (cm, _tmp) = open_state_with_remote().await;
    // As in `requests_for_another_project_are_proxied`: no python backend runs
    // here, so the 502 is how we observe the handler declined to answer from
    // its own working tree — which is checked out on one branch only.
    let app = router(make_strict_state(cm.clone()));
    let (status, _body) = get_json(
        app,
        &format!("/cloudmap?auth_project={REMOTE_PROJECT}&branch=some-other-branch"),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::BAD_GATEWAY,
        "a read for another branch must go to python, not the local worktree"
    );

    // Selection on /cloudmap/facets is documented to work exactly as on
    // /cloudmap, so it routes the same way.
    let app = router(make_strict_state(cm.clone()));
    let (status, _body) = get_json(
        app,
        &format!(
            "/cloudmap/facets?group_by=type&auth_project={REMOTE_PROJECT}&branch=some-other-branch"
        ),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::BAD_GATEWAY,
        "facets must route on branch the same way /cloudmap does"
    );

    // A branch mismatch is decided before the project check, so a request that
    // names no project doesn't get served from the wrong ref either.
    let app = router(make_strict_state(cm));
    let (status, _body) = get_json(app, "/cloudmap?branch=some-other-branch").await;
    assert_eq!(status, StatusCode::BAD_GATEWAY, "{status:?}");
}

#[tokio::test]
async fn no_branch_serves_the_checked_out_ref() {
    // Naming no branch keeps reading the configured working tree whatever it
    // is on — deliberately unlike python's `branch or "main"` default, which
    // would proxy every request for a repo checked out elsewhere.
    let (cm, _tmp) = open_state_with_remote().await;
    let app = router(make_strict_state(cm.clone()));
    let (status, body) = get_json(
        app,
        &format!("/cloudmap?kind=repositories&auth_project={REMOTE_PROJECT}"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert!(body["result"].get("repositories").is_some(), "{body:?}");

    // `HEAD` names no particular branch either — the reading `routes.rs` gives
    // it when building cache keys — so it is served, not proxied.
    let app = router(make_strict_state(cm));
    let (status, body) = get_json(
        app,
        &format!("/cloudmap?kind=repositories&auth_project={REMOTE_PROJECT}&branch=HEAD"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert!(body["result"].get("repositories").is_some(), "{body:?}");
}

#[tokio::test]
async fn post_naming_the_checked_out_branch_is_served_locally() {
    // `branch` is an envelope key of the write body, so it reaches the routing
    // gate rather than being rejected as an unknown section.
    let (cm, _tmp) = open_state_with_remote().await;
    let branch = worktree_branch(&cm).await;
    let app = router(make_strict_state(cm));
    let key = "git://unfurl.cloud/onecommons/std.git";
    let (status, body) = post_json_as(
        app,
        serde_json::json!({
            "branch": branch,
            "repositories": { key: { "name": "branch-scoped-write" } },
        }),
        Some(REMOTE_PROJECT),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
}

#[tokio::test]
async fn post_for_another_branch_is_proxied() {
    let (cm, _tmp) = open_state_with_remote().await;
    let key = "git://unfurl.cloud/onecommons/std.git";
    let app = router(make_strict_state(cm.clone()));
    let (status, _body) = post_json_as(
        app,
        serde_json::json!({
            "branch": "some-other-branch",
            "repositories": { key: { "name": "must-not-land" } },
        }),
        Some(REMOTE_PROJECT),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::BAD_GATEWAY,
        "a write for another branch must go to python, not this worktree"
    );

    // The gate runs before anything is applied, so the record is untouched --
    // this worktree can only write to the branch it is checked out on.
    let app = router(make_strict_state(cm));
    let (status, body) = get_json(
        app,
        &format!(
            "/cloudmap?kind=repositories&key={}&auth_project={REMOTE_PROJECT}",
            urlencoding::encode(key)
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert_ne!(
        body["result"]["repositories"][key]["name"], "must-not-land",
        "the proxied write must not have landed locally"
    );
}

#[tokio::test]
async fn dev_mode_ignores_a_branch_mismatch() {
    // Same rationale as the auth_project case: in dev mode there is only the
    // one checkout to serve, and its clients aren't expected to name it.
    let (cm, _tmp) = open_state_with_remote().await;
    let app = router(make_state(cm)); // default_config() => dev_mode
    let (status, body) =
        get_json(app, "/cloudmap?kind=repositories&branch=some-other-branch").await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert!(body["result"].get("repositories").is_some(), "{body:?}");
}

#[tokio::test]
async fn post_with_stale_latest_commit_returns_409() {
    let (cm, _synced, tmp) = open_two_file_state().await;
    let head = head_oid(tmp.path());
    let key = "git://unfurl.cloud/onecommons/std.git";

    // Matching the current HEAD is accepted.
    let app = router(make_state(cm.clone()));
    let (status, body) = post_json(
        app,
        serde_json::json!({
            "latest_commit": head,
            "repositories": { key: { "name": "occ-ok" } },
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");

    // A commit the repository has moved past is refused, before any record is
    // applied.
    let app = router(make_state(cm.clone()));
    let (status, body) = post_json(
        app,
        serde_json::json!({
            "latest_commit": "0000000000000000000000000000000000000000",
            "repositories": { key: { "name": "occ-stale" } },
        }),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "{body:?}");
    let msg = body["message"].as_str().unwrap_or_default();
    assert_eq!(body["code"], "CONFLICT", "{body:?}");
    assert!(msg.contains("latest_commit"), "{body:?}");
    assert!(
        msg.contains(&head),
        "should report the current revision: {body:?}"
    );

    // The refused batch left nothing behind.
    let app = router(make_state(cm));
    let (status, body) = get_json(
        app,
        &format!(
            "/cloudmap?kind=repositories&key={}",
            urlencoding::encode(key)
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["result"]["repositories"][key]["name"], "occ-ok");
}

#[tokio::test]
async fn post_without_latest_commit_skips_the_check() {
    let (cm, _synced, _tmp) = open_two_file_state().await;
    let app = router(make_state(cm));
    let key = "git://unfurl.cloud/onecommons/std.git";
    let (status, body) = post_json(
        app,
        serde_json::json!({ "repositories": { key: { "name": "no-occ" } } }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
}

// ---------------------------------------------------------------------------
// GET /cloudmap paging
// ---------------------------------------------------------------------------

/// Page through `uri` at `page_size`, returning every `(section, key)` seen
/// in the order the server produced it, plus the number of requests made.
async fn walk_pages(
    cm: CloudMapState,
    uri: &str,
    page_size: usize,
) -> (Vec<(String, String)>, usize) {
    let mut seen = Vec::new();
    let mut token: Option<String> = None;
    let mut requests = 0;
    loop {
        let mut paged = format!("{uri}&limit={page_size}");
        if let Some(t) = &token {
            paged.push_str(&format!("&page_token={}", urlencoding::encode(t)));
        }
        let app = router(make_state(cm.clone()));
        let (status, body) = get_json(app, &paged).await;
        requests += 1;
        assert_eq!(status, StatusCode::OK, "{body:?}");
        let env = body.as_object().expect("response object");
        assert!(
            !env.contains_key("followed"),
            "a paged request is keyless, so it never asks to follow"
        );
        for (section, entries) in env["result"].as_object().expect("result") {
            for k in entries.as_object().expect("section").keys() {
                seen.push((section.clone(), k.clone()));
            }
        }
        token = env
            .get("next_page_token")
            .and_then(Value::as_str)
            .map(str::to_string);
        if token.is_none() {
            return (seen, requests);
        }
        assert!(requests < 100, "paging failed to terminate");
    }
}

#[tokio::test]
async fn paged_walk_matches_unpaged() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm.clone()));
    let (_status, body) = get_json(app, "/cloudmap?kind=artifacts").await;
    let mut expected: Vec<String> = body["result"]["artifacts"]
        .as_object()
        .expect("artifacts")
        .keys()
        .cloned()
        .collect();
    expected.sort();

    let (seen, requests) = walk_pages(cm, "/cloudmap?kind=artifacts", 2).await;
    assert!(requests > 1, "fixture should need several pages at limit=2");
    assert!(seen.iter().all(|(s, _)| s == "artifacts"));
    let keys: Vec<String> = seen.into_iter().map(|(_, k)| k).collect();
    let mut sorted = keys.clone();
    sorted.sort();
    assert_eq!(keys, sorted, "records come back in key order");
    assert_eq!(keys, expected, "pages concatenate to the unpaged section");
}

#[tokio::test]
async fn paged_last_page_carries_no_token() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm.clone()));
    let (_status, body) = get_json(app, "/cloudmap?kind=artifacts").await;
    let size = body["result"]["artifacts"]
        .as_object()
        .expect("artifacts")
        .len();

    for limit in [size, size + 5] {
        let app = router(make_state(cm.clone()));
        let (status, body) =
            get_json(app, &format!("/cloudmap?kind=artifacts&limit={limit}")).await;
        assert_eq!(status, StatusCode::OK);
        assert!(
            body.get("next_page_token").is_none(),
            "limit={limit} should answer in one page: {body:?}"
        );
        assert_eq!(
            body["result"]["artifacts"]
                .as_object()
                .expect("artifacts")
                .len(),
            size
        );
    }
}

#[tokio::test]
async fn paged_empty_result_is_an_empty_document() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let (status, body) = get_json(app, "/cloudmap?kind=artifacts&type=no.such.Type&limit=5").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["result"], serde_json::json!({}));
    assert!(body.get("followed").is_none());
    assert!(body.get("next_page_token").is_none());
}

#[tokio::test]
async fn paged_with_type_filter() {
    // The type filter narrows before the page is cut.
    let (cm, _tmp) = open_cloudmap_state().await;
    let uri = "/cloudmap?kind=artifacts&type=cloudmap.artifacts.ci.GitLabPipeline";
    let (seen, _requests) = walk_pages(cm, uri, 1).await;
    assert_eq!(
        seen.len(),
        4,
        "fixture has four pipeline artifacts: {seen:?}"
    );
    assert!(seen.iter().all(|(_, k)| k.ends_with(".gitlab-ci.yml")));
}

#[tokio::test]
async fn paged_survives_a_deleted_anchor() {
    // The cursor is a value, not a row reference: a key that isn't in the
    // document still resumes at the right place.
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm.clone()));
    let (_status, body) = get_json(app, "/cloudmap?kind=artifacts").await;
    let mut keys: Vec<String> = body["result"]["artifacts"]
        .as_object()
        .expect("artifacts")
        .keys()
        .cloned()
        .collect();
    keys.sort();

    let token = encode_page_token("/artifacts", &format!("{}\u{1}gone", keys[0]));
    let app = router(make_state(cm));
    let (status, body) = get_json(
        app,
        &format!(
            "/cloudmap?kind=artifacts&limit=50&page_token={}",
            urlencoding::encode(&token)
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let mut got: Vec<String> = body["result"]["artifacts"]
        .as_object()
        .expect("artifacts")
        .keys()
        .cloned()
        .collect();
    got.sort();
    assert_eq!(got, keys[1..].to_vec());
}

#[tokio::test]
async fn paged_rejects_key_and_bad_parameters() {
    let (cm, _tmp) = open_cloudmap_state().await;

    let app = router(make_state(cm.clone()));
    let (status, _body) = get_json(
        app,
        "/cloudmap?kind=artifacts&limit=2&key=pkg:oci/example/image",
    )
    .await;
    assert_eq!(
        status,
        StatusCode::BAD_REQUEST,
        "a key selects one record, so there is nothing to page"
    );

    for bad in ["0", "-1"] {
        let app = router(make_state(cm.clone()));
        let (status, _body) = get_json(app, &format!("/cloudmap?kind=artifacts&limit={bad}")).await;
        assert_eq!(
            status,
            StatusCode::UNPROCESSABLE_ENTITY,
            "limit={bad} violates its schema bound, as it does on the python side"
        );
    }

    // no delimiter, empty key, and a section that isn't one -- the last
    // would otherwise resume from the wrong place in the ordering
    for bad in ["artifacts", "artifacts/", "nosuchsection/key"] {
        let app = router(make_state(cm.clone()));
        let (status, _body) = get_json(
            app,
            &format!(
                "/cloudmap?kind=artifacts&limit=2&page_token={}",
                urlencoding::encode(bad)
            ),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "page_token={bad}");
    }
}

#[tokio::test]
async fn paged_follows_from_the_page() {
    // A paged walk starts from that page's records -- never from the probe
    // record, which belongs to the next page -- and the cap is per page.
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm.clone()));
    let (status, body) = get_json(app, "/cloudmap?kind=repositories&limit=2&follow=10").await;
    assert_eq!(status, StatusCode::OK);
    let page = body["result"]["repositories"]
        .as_object()
        .expect("repositories");
    assert_eq!(page.len(), 2);
    let followed = body["followed"].as_object().expect("followed");
    assert!(!followed.is_empty(), "the page references other records");
    let total: usize = followed
        .values()
        .map(|sec| sec.as_object().map(|m| m.len()).unwrap_or(0))
        .sum();
    assert!(total <= 10, "the cap applies per page");

    // Walking the same page again is deterministic.
    let app = router(make_state(cm));
    let (_status, again) = get_json(app, "/cloudmap?kind=repositories&limit=2&follow=10").await;
    assert_eq!(again["followed"], body["followed"]);
    assert_eq!(again["result"], body["result"]);
}

#[tokio::test]
async fn paged_follow_excludes_the_probe_records_neighbours() {
    // The page is cut before the walk. Fetching one record with follow must
    // not report anything reachable only from the *second* record, which the
    // limit+1 probe fetched but the page does not contain.
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm.clone()));
    let (_s, one) = get_json(app, "/cloudmap?kind=repositories&limit=1&follow=100").await;
    let first_key = one["result"]["repositories"]
        .as_object()
        .expect("repositories")
        .keys()
        .next()
        .expect("one record")
        .clone();

    // The same single record addressed by key walks to the same neighbourhood.
    let app = router(make_state(cm));
    let (_s, by_key) = get_json(
        app,
        &format!(
            "/cloudmap?kind=repositories&key={}&follow=100",
            urlencoding::encode(&first_key)
        ),
    )
    .await;
    assert_eq!(
        one["followed"], by_key["followed"],
        "a one-record page must walk exactly what that record reaches"
    );
}

#[test]
fn page_token_wire_format_is_pinned() {
    // The python handler's `_encode_page_token` produces this same string;
    // its twin test asserts the identical literal. A client may page across
    // the two implementations, so the encoding cannot drift.
    assert_eq!(encode_page_token("/artifacts", "pkg:x"), "artifacts/pkg:x");
}

#[tokio::test]
async fn since_version_reports_deleted_records() {
    // A delete is invisible to a live read -- the row just stops being
    // returned -- so an incremental read has to carry a tombstone or a
    // client can never learn about it.
    let (cm, _tmp) = open_cloudmap_state().await;
    let key = "git://unfurl.cloud/onecommons/blueprints/odoo.git";

    let synced = cm.synced();
    let watermark = synced
        .find_records(&unfurl_git_sync::RecordQuery::default())
        .await
        .expect("find")
        .iter()
        .map(|r| r.version)
        .max()
        .expect("records");
    synced
        .delete_record(Some("cloudmap.yaml"), "/repositories", key, None, false)
        .await
        .expect("delete");

    // A live read no longer has it...
    let app = router(make_state(cm.clone()));
    let (status, body) = get_json(app, "/cloudmap?kind=repositories").await;
    assert_eq!(status, StatusCode::OK);
    assert!(body["result"]["repositories"].get(key).is_none());

    // ...but catching up from the watermark reports it as gone.
    let app = router(make_state(cm));
    let (status, body) = get_json(app, &format!("/cloudmap?since_version={watermark}")).await;
    assert_eq!(status, StatusCode::OK);
    let tomb = &body["result"]["repositories"][key];
    assert_eq!(
        tomb["unfurl.server.deleted"],
        serde_json::json!(true),
        "the tombstone must be flagged: {body:?}"
    );
}

// ---------------------------------------------------------------------------
// GET /cloudmap/facets
// ---------------------------------------------------------------------------

/// Seed a small, controlled dataset for the facet tests on top of the
/// fixture: a two-level type hierarchy in `/types` and artifacts
/// carrying topics arrays (with a duplicate element), platform objects
/// in *both* key orders, and one record with none of it.
async fn seed_facet_records(cm: CloudMapState) {
    let body = serde_json::json!({
        "types": {
            "FacetBase": {"name": "FacetBase", "extends": ["FacetBase"]},
            "FacetDerived": {"name": "FacetDerived",
                             "extends": ["FacetDerived", "FacetBase"]},
        },
        "artifacts": {
            "facet:a1": {"type": {"FacetDerived": {}},
                "metadata": {"topics": ["db", "web"],
                "platforms": [{"os": "linux", "architecture": "amd64"},
                              {"os": "windows", "architecture": "arm64"}]}},
            "facet:a2": {"type": {"FacetBase": {}},
                "metadata": {"topics": ["db", "db"]}},
            "facet:a3": {"type": {"FacetDerived": {}},
                "metadata": {"topics": ["web"],
                "platforms": [{"architecture": "amd64", "os": "linux"}]}},
            "facet:a4": {"metadata": {"name": "quiet"}},
        }
    });
    let app = router(make_state(cm));
    let (status, _) = post_json(app, body).await;
    assert_eq!(status, StatusCode::OK, "seeding must succeed");
}

/// How many records a `/cloudmap` response holds across its sections.
fn count_records(result: &Value) -> usize {
    result
        .as_object()
        .map(|sections| {
            sections
                .values()
                .filter_map(Value::as_object)
                .map(|records| records.len())
                .sum()
        })
        .unwrap_or(0)
}

#[tokio::test]
async fn facets_group_by_topics() {
    let (cm, _tmp) = open_cloudmap_state().await;
    seed_facet_records(cm.clone()).await;

    let app = router(make_state(cm));
    let (status, body) = get_json(
        app,
        "/cloudmap/facets?kind=artifacts&group_by=metadata/topics",
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert_eq!(body["meta"]["group_by"], "/metadata/topics");
    assert_eq!(body["meta"]["facets"], serde_json::json!([]));
    assert_eq!(
        body["meta"]["subtypes"], false,
        "no type column, so no rollup was applied"
    );
    // 11 fixture artifacts + 4 seeded.
    assert_eq!(body["total"], 15);
    assert_eq!(
        body["groups"],
        serde_json::json!({
            "db":  {"count": 2},
            "web": {"count": 2},
        }),
        "a2's duplicate 'db' counts once; no `facets` key without facet columns"
    );
}

#[tokio::test]
async fn facets_subtypes_invariant_matches_type_filter() {
    // The whole point of the rollup: for every type name, the
    // `group_by=type` bucket equals what `?type=T` selects.
    let (cm, _tmp) = open_cloudmap_state().await;
    seed_facet_records(cm.clone()).await;

    let app = router(make_state(cm.clone()));
    let (status, body) = get_json(app, "/cloudmap/facets?kind=artifacts&group_by=type").await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert_eq!(body["meta"]["subtypes"], true);
    let groups = body["groups"].as_object().expect("groups object");
    assert!(
        groups.contains_key("FacetBase"),
        "rollup must surface the base type: {groups:?}"
    );
    for (type_name, entry) in groups {
        let app = router(make_state(cm.clone()));
        let uri = format!(
            "/cloudmap?kind=artifacts&type={}",
            urlencoding::encode(type_name)
        );
        let (status, selected) = get_json(app, &uri).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(
            entry["count"].as_u64().expect("count") as usize,
            count_records(&selected["result"]),
            "bucket for {type_name:?} must count exactly what ?type= selects"
        );
    }

    // subtypes=false counts exact declared names only: the base type's
    // bucket shrinks to its own record.
    let app = router(make_state(cm));
    let (status, body) = get_json(
        app,
        "/cloudmap/facets?kind=artifacts&group_by=type&subtypes=false",
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert_eq!(body["meta"]["subtypes"], false);
    assert_eq!(body["groups"]["FacetBase"]["count"], 1);
    assert_eq!(body["groups"]["FacetDerived"]["count"], 2);
}

#[tokio::test]
async fn facets_repeated_and_composite_columns() {
    let (cm, _tmp) = open_cloudmap_state().await;
    seed_facet_records(cm.clone()).await;

    // Two columns: a composite (type × platforms) and a simple one --
    // the repeated `facet=` spelling the extractor must support.
    let app = router(make_state(cm));
    let (status, body) = get_json(
        app,
        "/cloudmap/facets?kind=artifacts&group_by=metadata/topics\
         &facet=type,metadata/platforms&facet=type&subtypes=false",
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert_eq!(
        body["meta"]["facets"],
        serde_json::json!([["/type", "/metadata/platforms"], ["/type"]])
    );
    let linux = r#"["FacetDerived",{"architecture":"amd64","os":"linux"}]"#;
    let windows = r#"["FacetDerived",{"architecture":"arm64","os":"windows"}]"#;
    assert_eq!(
        body["groups"]["web"],
        serde_json::json!({
            "count": 2,
            "facets": [
                // composite cells: canonical JSON arrays; a1's and a3's
                // differently-spelled platforms merge into one bucket
                {linux: 2, windows: 1},
                {"FacetDerived": 2},
            ],
        }),
        "{body:?}"
    );
    assert_eq!(
        body["groups"]["db"],
        serde_json::json!({
            "count": 2,
            "facets": [
                // a2 has no platforms, so only a1 reaches the composite
                {linux: 1, windows: 1},
                {"FacetBase": 1, "FacetDerived": 1},
            ],
        }),
        "{body:?}"
    );
}

#[tokio::test]
async fn facets_error_statuses() {
    let (cm, _tmp) = open_cloudmap_state().await;

    // Missing required group_by: schema-level, 422 like APIFlask.
    let app = router(make_state(cm.clone()));
    let (status, _) = get_json(app, "/cloudmap/facets?kind=artifacts").await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);

    // Empty path segment: semantic, 400.
    let app = router(make_state(cm.clone()));
    let (status, body) = get_json(app, "/cloudmap/facets?group_by=metadata//topics").await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body:?}");

    // Bad facet member path: 400 too.
    let app = router(make_state(cm.clone()));
    let (status, body) = get_json(app, "/cloudmap/facets?group_by=type&facet=a//b").await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body:?}");

    // Unknown kind: 404, same as GET /cloudmap.
    let app = router(make_state(cm));
    let (status, body) = get_json(app, "/cloudmap/facets?kind=nope&group_by=type").await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body:?}");
}

#[tokio::test]
async fn facets_missing_group_path_yields_empty_groups() {
    let (cm, _tmp) = open_cloudmap_state().await;
    let app = router(make_state(cm));
    let (status, body) = get_json(app, "/cloudmap/facets?group_by=no/such/path").await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert_eq!(body["groups"], serde_json::json!({}));
    assert!(
        body["total"].as_i64().expect("total") > 0,
        "records without the path still count toward total"
    );
}

#[tokio::test]
async fn facets_non_ascii_canonical_keys() {
    // Canonical keys carry non-ASCII characters raw. The python test
    // test_cloudmap_facets_non_ascii_canonical_keys asserts this same
    // literal, which is what pins the two implementations to one
    // spelling (python's json.dumps would otherwise escape it to
    // "café").
    let (cm, _tmp) = open_cloudmap_state().await;
    let body = serde_json::json!({
        "artifacts": {
            "facet:unicode": {
                "type": {"FacetBase": {}},
                "metadata": {"platforms": [{"os": "linux", "variant": "café"}]},
            }
        }
    });
    let app = router(make_state(cm.clone()));
    let (status, _) = post_json(app, body).await;
    assert_eq!(status, StatusCode::OK);

    let app = router(make_state(cm));
    let (status, body) = get_json(
        app,
        "/cloudmap/facets?kind=artifacts&group_by=metadata/platforms",
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert_eq!(
        body["groups"],
        serde_json::json!({r#"{"os":"linux","variant":"café"}"#: {"count": 1}}),
        "{body:?}"
    );
}

#[tokio::test]
async fn repeated_filters_and_together() {
    // `filter=` repeats: every occurrence must match (the clauses AND in
    // SQL). Extraction goes through the repeated-key extractor, so this
    // also guards the serde_html_form swap on GET /cloudmap.
    let (cm, _tmp) = open_cloudmap_state().await;

    // Both filters hold for the dashboard repo.
    let app = router(make_state(cm.clone()));
    let (status, body) = get_json(
        app,
        &format!(
            "/cloudmap?filter={}&filter={}",
            urlencoding::encode("/private=true"),
            urlencoding::encode("/branches/main=4551885dfab39991cfdb958cb79fcb6aa282481d"),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    let repos = body["result"]["repositories"]
        .as_object()
        .expect("repositories matched");
    assert_eq!(
        repos.keys().collect::<Vec<_>>(),
        vec!["git://unfurl.cloud/feb20a/dashboard.git"],
        "{body:?}"
    );

    // The discriminating case: each filter matches a record on its own,
    // but no record matches both — an OR (or applying only the first
    // occurrence) would return records.
    let app = router(make_state(cm.clone()));
    let (status, body) = get_json(
        app,
        &format!(
            "/cloudmap?filter={}&filter={}",
            urlencoding::encode("/private=true"),
            urlencoding::encode(
                "/metadata/homepage_url=https://unfurl.cloud/onecommons/blueprints/odoo"
            ),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert_eq!(body["result"], serde_json::json!({}), "{body:?}");

    // The same repetition narrows a facet aggregation.
    let app = router(make_state(cm));
    let (status, body) = get_json(
        app,
        &format!(
            "/cloudmap/facets?kind=repositories&group_by=private&filter={}&filter={}",
            urlencoding::encode("/private=true"),
            urlencoding::encode("/branches/main=4551885dfab39991cfdb958cb79fcb6aa282481d"),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body:?}");
    assert_eq!(body["total"], 1, "{body:?}");
    assert_eq!(body["groups"], serde_json::json!({"true": {"count": 1}}));
}

/// Paging with `limit=1` over a key held by two files must still reach
/// every later record: the page that carries the pair exceeds the limit,
/// and the walk has to continue past it.
#[tokio::test]
async fn paging_over_a_duplicated_key_reaches_the_tail() {
    let (cm, synced, _tmp) = open_two_file_state().await;
    let dup = "git://example.com/two-files.git";
    for file in ["cloudmap.yaml", ALT_FILE] {
        synced
            .upsert_record(
                Some(file),
                "/repositories",
                dup,
                serde_json::json!({"name": file}),
                None,
                false,
            )
            .await
            .expect("write");
    }
    let expected: Vec<String> = synced
        .find_records(&unfurl_git_sync::RecordQuery {
            path: Some("/repositories".into()),
            ..Default::default()
        })
        .await
        .expect("all")
        .into_iter()
        .map(|r| r.key)
        .collect::<std::collections::BTreeSet<_>>()
        .into_iter()
        .collect();

    let mut seen: Vec<String> = Vec::new();
    let mut token: Option<String> = None;
    for _ in 0..50 {
        let uri = match &token {
            Some(t) => format!(
                "/cloudmap?kind=repositories&limit=1&page_token={}",
                urlencoding::encode(t)
            ),
            None => "/cloudmap?kind=repositories&limit=1".to_string(),
        };
        let app = router(make_state(cm.clone()));
        let (status, body) = get_json(app, &uri).await;
        assert_eq!(status, StatusCode::OK, "{body:?}");
        if let Some(repos) = body["result"]
            .get("repositories")
            .and_then(Value::as_object)
        {
            seen.extend(repos.keys().cloned());
        }
        match body.get("next_page_token").and_then(Value::as_str) {
            Some(t) => token = Some(t.to_string()),
            None => break,
        }
    }
    seen.sort();
    seen.dedup();
    assert_eq!(
        seen, expected,
        "the walk must reach every key, not stop at the duplicated one"
    );
}

/// Stand up a divergence on `key`: the database holds an in-flight edit
/// and the file on disk holds something else.
async fn stand_up_conflict(cm: &CloudMapState, tmp: &tempfile::TempDir, key: &str) {
    cm.synced()
        .update_record(
            Some("cloudmap.yaml"),
            "/repositories",
            key,
            serde_json::json!({"name": "ours"}),
            None,
            false,
        )
        .await
        .expect("update");
    let path = tmp.path().join("cloudmap.yaml");
    let before = std::fs::read_to_string(&path).expect("read");
    let edited = before.replace("name: std", "name: theirs");
    assert_ne!(edited, before);
    std::fs::write(&path, edited).expect("write");
    let scan = cm
        .synced()
        .update_from_working_dir(unfurl_git_sync::ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(scan.conflicts.len(), 1, "{scan:?}");
}

#[tokio::test]
async fn post_resolve_marker_settles_a_conflict() {
    let key = "git://unfurl.cloud/onecommons/std.git";

    // Without the marker the write lands and the conflict stands, so a
    // client that never read the file's side cannot discard it.
    let (cm, tmp) = open_cloudmap_state().await;
    stand_up_conflict(&cm, &tmp, key).await;
    let body = serde_json::json!({
        "repositories": { key: {"name": "decided"} }
    });
    let (status, _) = post_json(router(make_state(cm.clone())), body).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        cm.synced().list_conflicts(None).await.expect("list").len(),
        1,
        "the conflict survives a plain write"
    );

    // With it, the same write settles the record.
    let (cm, tmp) = open_cloudmap_state().await;
    stand_up_conflict(&cm, &tmp, key).await;
    let body = serde_json::json!({
        "repositories": {
            key: {"name": "decided", "unfurl.server.resolve": true}
        }
    });
    let (status, _) = post_json(router(make_state(cm.clone())), body).await;
    assert_eq!(status, StatusCode::OK);
    assert!(cm
        .synced()
        .list_conflicts(None)
        .await
        .expect("list")
        .is_empty());
    let rec = cm
        .synced()
        .get_record("cloudmap.yaml", "/repositories", key)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(rec.json["name"], "decided");
    assert!(
        rec.json.get("unfurl.server.resolve").is_none(),
        "the marker is popped, not stored: {rec:?}"
    );
}

#[tokio::test]
async fn get_conflicts_reports_the_working_trees_side() {
    let key = "git://unfurl.cloud/onecommons/std.git";
    let (cm, tmp) = open_cloudmap_state().await;
    stand_up_conflict(&cm, &tmp, key).await;

    // Without the parameter the response has no `conflicts` key at
    // all, so a client can tell "didn't ask" from "none".
    let (status, body) = get_json(router(make_state(cm.clone())), "/cloudmap").await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.get("conflicts").is_none(), "{body:?}");
    assert_eq!(
        body["result"]["repositories"][key]["name"], "ours",
        "an ordinary read still sees the database's version"
    );

    let (status, body) = get_json(
        router(make_state(cm.clone())),
        "/cloudmap?kind=repositories&conflicts=true",
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        body["result"]["repositories"][key]["name"], "ours",
        "`result` is unchanged by asking"
    );
    let groups = body["conflicts"].as_array().expect("array");
    assert_eq!(groups.len(), 1, "{groups:?}");
    assert_eq!(
        groups[0]["records"]["repositories"][key]["name"], "theirs",
        "and the working tree's version is what `conflicts` carries"
    );
    assert!(
        groups[0]["commit"].is_null(),
        "the file's version is an uncommitted edit, so no commit carries \
         it yet: {:?}",
        groups[0]["commit"]
    );

    // Commit it, and the group names the commit that now holds it --
    // the record itself stays in flight, so `result` is unchanged.
    let oid = cm
        .synced()
        .commit_repository("carry the hand edit")
        .await
        .expect("commit")
        .expect("the working tree differs from HEAD");
    let (_, body) = get_json(
        router(make_state(cm.clone())),
        "/cloudmap?kind=repositories&conflicts=true",
    )
    .await;
    let groups = body["conflicts"].as_array().expect("array");
    assert_eq!(groups.len(), 1, "{groups:?}");
    assert_eq!(groups[0]["commit"], serde_json::json!(oid));
    assert_eq!(groups[0]["records"]["repositories"][key]["name"], "theirs");
    assert_eq!(body["result"]["repositories"][key]["name"], "ours");

    // Settling it empties the array -- present, because the request
    // asked, but with nothing in it.
    cm.synced()
        .resolve_conflict(
            "cloudmap.yaml",
            "/repositories",
            key,
            unfurl_git_sync::Resolution::Ours,
            None,
        )
        .await
        .expect("resolve");
    let (_, body) = get_json(
        router(make_state(cm)),
        "/cloudmap?kind=repositories&conflicts=true",
    )
    .await;
    assert_eq!(
        body["conflicts"].as_array().expect("array").len(),
        0,
        "{body:?}"
    );
}

#[tokio::test]
async fn post_reports_conflicts_it_could_not_land() {
    let key = "git://unfurl.cloud/onecommons/std.git";
    let (cm, tmp) = open_cloudmap_state().await;
    stand_up_conflict(&cm, &tmp, key).await;

    // Writing the contested record: staged, but it will not reach the
    // file, and the response has to say so.
    let body = serde_json::json!({
        "repositories": { key: {"name": "second thoughts"} }
    });
    let (status, echo) = post_json(router(make_state(cm.clone())), body).await;
    assert_eq!(status, StatusCode::OK);
    let groups = echo["conflicts"].as_array().expect("conflicts reported");
    assert_eq!(groups.len(), 1, "{groups:?}");
    assert_eq!(groups[0]["records"]["repositories"][key]["name"], "theirs");

    // A write to an uncontested record says nothing, so the key's
    // presence is the signal.
    let other = "git://unfurl.cloud/onecommons/unfurl-types.git";
    let body = serde_json::json!({
        "repositories": { other: {"name": "quiet"} }
    });
    let (status, echo) = post_json(router(make_state(cm.clone())), body).await;
    assert_eq!(status, StatusCode::OK);
    assert!(
        echo.get("conflicts").is_none(),
        "scoped to what this request wrote: {echo:?}"
    );

    // And settling it clears the report.
    let body = serde_json::json!({
        "repositories": {
            key: {"name": "decided", "unfurl.server.resolve": true}
        }
    });
    let (_, echo) = post_json(router(make_state(cm)), body).await;
    assert!(echo.get("conflicts").is_none(), "{echo:?}");
}
