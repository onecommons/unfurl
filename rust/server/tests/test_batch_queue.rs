// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Integration tests for Redis-backed batch queue.
//!
//! These tests require a live Redis instance.  They only run when the
//! `UNFURL_TEST_REDIS_URL` environment variable is set.
//!
//! Run with:
//!   UNFURL_TEST_REDIS_URL="redis://localhost:6379/2" cargo test --test test_batch_queue

use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value as JsonValue};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use tokio::net::TcpListener;
use tower::ServiceExt;
use unfurl_server::config::Config;
use unfurl_server::queue::{self, ExportQueueCheck, QueueIdResult, QueueItem};
use unfurl_server::{build_router, AppState};

/// Return the Redis URL from the environment, or `None` to skip.
fn redis_url() -> Option<String> {
    std::env::var("UNFURL_TEST_REDIS_URL").ok()
}

/// Build a test Config with a unique key prefix to avoid collisions.
fn test_config(prefix: &str, batch_window_secs: f64) -> Config {
    Config {
        host: "127.0.0.1".into(),
        port: 0,
        backend_url: None,
        redis_url: redis_url(),
        redis_host: None,
        redis_port: 6379,
        redis_password: None,
        redis_db: 0,
        cache_key_prefix: format!("test_{}::", prefix),
        secret: String::new(),
        proxy_timeout_secs: 10,
        redis_timeout_secs: 5,
        package_digest: String::new(),
        max_body_bytes: 10 * 1024 * 1024,
        batch_window_secs,
        worker_poll_interval_secs: 0.05,
        queue_key_ttl_secs: 3600,
        events_budget_secs: 10,
        cloudmap_repo: None,
        cloudmap_db_url: None,
        cloudmap_force: false,
        local: None,
        cors_origins: None,
    }
}

/// Clean up all Redis keys matching the test prefix.
async fn cleanup_keys(conn: &mut redis::aio::MultiplexedConnection, prefix: &str) {
    let pattern = format!("test_{}::*", prefix);
    let keys: Vec<String> = redis::cmd("KEYS")
        .arg(&pattern)
        .query_async(conn)
        .await
        .unwrap_or_default();
    if !keys.is_empty() {
        let mut cmd = redis::cmd("DEL");
        for k in &keys {
            cmd.arg(k);
        }
        let _: Result<i64, _> = cmd.query_async(conn).await;
    }
}

/// Spin up a tiny axum server that captures POST bodies.
/// Returns (base_url, handle to received requests).
async fn mock_backend() -> (String, Arc<Mutex<Vec<(String, JsonValue)>>>) {
    let received: Arc<Mutex<Vec<(String, JsonValue)>>> = Arc::new(Mutex::new(Vec::new()));
    let received_clone = received.clone();

    let app = axum::Router::new().fallback(move |req: axum::extract::Request| {
        let recv = received_clone.clone();
        async move {
            let uri = req.uri().to_string();
            let body_bytes = axum::body::to_bytes(req.into_body(), 1024 * 1024)
                .await
                .unwrap_or_default();
            let body: JsonValue = serde_json::from_slice(&body_bytes).unwrap_or(JsonValue::Null);
            recv.lock().unwrap().push((uri, body));
            axum::http::StatusCode::OK
        }
    });

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });

    (format!("http://{}", addr), received)
}

fn make_item(
    endpoint: &str,
    branch: &str,
    commit: &str,
    patches: JsonValue,
    msg: &str,
) -> QueueItem {
    QueueItem {
        endpoint: endpoint.into(),
        body: json!({
            "patch": patches,
            "commit_msg": msg,
            "branch": branch,
            "latest_commit": commit,
            "deployment_path": "",
            "environment": "production",
        }),
        headers: HashMap::new(),
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_enqueue_and_drain_single_project() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };

    let prefix = "batch_single";
    let config = test_config(prefix, 1.0); // 1-second window for fast tests

    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    cleanup_keys(&mut conn, prefix).await;

    let (backend_url, received) = mock_backend().await;

    // Enqueue 3 items to the same project, same branch/commit — should batch.
    let project_id = "proj_alpha";
    for i in 0..3 {
        let item = make_item(
            &format!("/update_ensemble?auth_project={}", project_id),
            "main",
            "abc123",
            json!([{ "__typename": "ResourceTemplate", "name": format!("t{}", i) }]),
            &format!("change {}", i),
        );
        queue::enqueue(&mut conn, &config, project_id, &item)
            .await
            .unwrap();
    }

    // Verify items are in Redis.
    let list_key = config.batch_list_key(project_id);
    let len: i64 = redis::cmd("LLEN")
        .arg(&list_key)
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(len, 3, "expected 3 items in batch list");

    // Start the worker and let it drain after the 1-second window.
    let worker_conn = client.get_multiplexed_async_connection().await.unwrap();
    let worker_config = config.clone();
    let worker_backend = backend_url.clone();
    let worker_client = reqwest::Client::new();
    let worker_handle = tokio::spawn(async move {
        queue::run_worker(worker_conn, worker_config, worker_backend, worker_client).await;
    });

    // Wait for the batch window to expire + worker poll + processing time.
    tokio::time::sleep(std::time::Duration::from_millis(2500)).await;

    // The worker should have drained the list.
    let len_after: i64 = redis::cmd("LLEN")
        .arg(&list_key)
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(len_after, 0, "batch list should be empty after drain");

    // Check what the mock backend received.
    let reqs = received.lock().unwrap().clone();
    assert_eq!(reqs.len(), 1, "expected exactly 1 batch request");

    let (uri, body) = &reqs[0];
    assert!(
        uri.starts_with("/batch_patch?auth_project="),
        "expected /batch_patch endpoint, got: {}",
        uri
    );

    // All 3 original requests should be in the requests array.
    let requests = body.get("requests").and_then(|v| v.as_array()).unwrap();
    assert_eq!(requests.len(), 3, "expected 3 requests in batch");

    // Each request should have the endpoint name.
    for req in requests {
        assert_eq!(
            req.get("endpoint").and_then(|v| v.as_str()).unwrap(),
            "update_ensemble"
        );
    }

    worker_handle.abort();
    cleanup_keys(&mut conn, prefix).await;
}

#[tokio::test]
async fn test_different_branches_produce_separate_batches() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };

    let prefix = "batch_branches";
    let config = test_config(prefix, 1.0);

    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    cleanup_keys(&mut conn, prefix).await;

    let (backend_url, received) = mock_backend().await;

    let project_id = "proj_beta";

    // Item on "main" branch.
    let item_main = make_item(
        &format!("/update_ensemble?auth_project={}", project_id),
        "main",
        "abc",
        json!([{"name": "main_resource"}]),
        "main change",
    );
    queue::enqueue(&mut conn, &config, project_id, &item_main)
        .await
        .unwrap();

    // Item on "dev" branch — different partition key.
    let item_dev = make_item(
        &format!("/update_ensemble?auth_project={}", project_id),
        "dev",
        "abc",
        json!([{"name": "dev_resource"}]),
        "dev change",
    );
    queue::enqueue(&mut conn, &config, project_id, &item_dev)
        .await
        .unwrap();

    // Start worker.
    let worker_conn = client.get_multiplexed_async_connection().await.unwrap();
    let worker_handle = tokio::spawn({
        let config = config.clone();
        let backend_url = backend_url.clone();
        async move {
            queue::run_worker(worker_conn, config, backend_url, reqwest::Client::new()).await;
        }
    });

    tokio::time::sleep(std::time::Duration::from_millis(2500)).await;

    let reqs = received.lock().unwrap().clone();
    assert_eq!(
        reqs.len(),
        2,
        "expected 2 separate batch requests (one per branch)"
    );

    // Each should have exactly 1 request.
    for (_, body) in &reqs {
        let requests = body.get("requests").and_then(|v| v.as_array()).unwrap();
        assert_eq!(requests.len(), 1);
    }

    worker_handle.abort();
    cleanup_keys(&mut conn, prefix).await;
}

#[tokio::test]
async fn test_mixed_endpoints_in_single_batch() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };

    let prefix = "batch_mixed";
    let config = test_config(prefix, 1.0);

    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    cleanup_keys(&mut conn, prefix).await;

    let (backend_url, received) = mock_backend().await;

    let project_id = "proj_gamma";

    // First item: update_ensemble.
    let item1 = make_item(
        &format!("/update_ensemble?auth_project={}", project_id),
        "main",
        "def456",
        json!([{"name": "r1"}]),
        "update",
    );
    queue::enqueue(&mut conn, &config, project_id, &item1)
        .await
        .unwrap();

    // Second item: create_provider — different endpoint, same branch/commit.
    let item2 = make_item(
        &format!("/create_provider?auth_project={}", project_id),
        "main",
        "def456",
        json!([{"name": "r2"}]),
        "create provider",
    );
    queue::enqueue(&mut conn, &config, project_id, &item2)
        .await
        .unwrap();

    let worker_conn = client.get_multiplexed_async_connection().await.unwrap();
    let worker_handle = tokio::spawn({
        let config = config.clone();
        let backend_url = backend_url.clone();
        async move {
            queue::run_worker(worker_conn, config, backend_url, reqwest::Client::new()).await;
        }
    });

    tokio::time::sleep(std::time::Duration::from_millis(2500)).await;

    let reqs = received.lock().unwrap().clone();
    assert_eq!(reqs.len(), 1);

    let (_, body) = &reqs[0];
    let requests = body.get("requests").and_then(|v| v.as_array()).unwrap();
    assert_eq!(requests.len(), 2, "both requests should be in one batch");

    // Order preserved.
    assert_eq!(
        requests[0]
            .get("endpoint")
            .and_then(|v| v.as_str())
            .unwrap(),
        "update_ensemble"
    );
    assert_eq!(
        requests[1]
            .get("endpoint")
            .and_then(|v| v.as_str())
            .unwrap(),
        "create_provider"
    );

    worker_handle.abort();
    cleanup_keys(&mut conn, prefix).await;
}

#[tokio::test]
async fn test_multiple_projects_batched_independently() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };

    let prefix = "batch_multiproj";
    let config = test_config(prefix, 1.0);

    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    cleanup_keys(&mut conn, prefix).await;

    let (backend_url, received) = mock_backend().await;

    // Enqueue to two different projects.
    for pid in &["proj_one", "proj_two"] {
        for i in 0..2 {
            let item = make_item(
                &format!("/update_ensemble?auth_project={}", pid),
                "main",
                "same_commit",
                json!([{"name": format!("{}_{}", pid, i)}]),
                &format!("{} change {}", pid, i),
            );
            queue::enqueue(&mut conn, &config, pid, &item)
                .await
                .unwrap();
        }
    }

    // Verify ready set has 2 entries (one per project).
    let ready_key = config.batch_ready_set_key();
    let count: i64 = redis::cmd("ZCARD")
        .arg(&ready_key)
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(
        count, 2,
        "expected 2 entries in ready set (one per project)"
    );

    let worker_conn = client.get_multiplexed_async_connection().await.unwrap();
    let worker_handle = tokio::spawn({
        let config = config.clone();
        let backend_url = backend_url.clone();
        async move {
            queue::run_worker(worker_conn, config, backend_url, reqwest::Client::new()).await;
        }
    });

    tokio::time::sleep(std::time::Duration::from_millis(2500)).await;

    let reqs = received.lock().unwrap().clone();
    assert_eq!(reqs.len(), 2, "expected 2 batch requests (one per project)");

    // Each batch should have 2 requests.
    for (uri, body) in &reqs {
        assert!(uri.contains("/batch_patch?auth_project="));
        let requests = body.get("requests").and_then(|v| v.as_array()).unwrap();
        assert_eq!(
            requests.len(),
            2,
            "each project batch should have 2 requests"
        );
    }

    worker_handle.abort();
    cleanup_keys(&mut conn, prefix).await;
}

#[tokio::test]
async fn test_new_items_after_drain_start_fresh_window() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };

    let prefix = "batch_fresh";
    let config = test_config(prefix, 1.0);

    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    cleanup_keys(&mut conn, prefix).await;

    let (backend_url, received) = mock_backend().await;

    let project_id = "proj_fresh";

    // Enqueue first batch.
    let item1 = make_item(
        &format!("/update_ensemble?auth_project={}", project_id),
        "main",
        "aaa",
        json!([{"name": "first"}]),
        "first batch",
    );
    queue::enqueue(&mut conn, &config, project_id, &item1)
        .await
        .unwrap();

    // Start worker.
    let worker_conn = client.get_multiplexed_async_connection().await.unwrap();
    let worker_handle = tokio::spawn({
        let config = config.clone();
        let backend_url = backend_url.clone();
        async move {
            queue::run_worker(worker_conn, config, backend_url, reqwest::Client::new()).await;
        }
    });

    // Wait for first batch to drain.
    tokio::time::sleep(std::time::Duration::from_millis(2000)).await;

    let first_count = received.lock().unwrap().len();
    assert_eq!(first_count, 1, "first batch should have been drained");

    // Enqueue second batch (new items after drain).
    let item2 = make_item(
        &format!("/create_ensemble?auth_project={}", project_id),
        "main",
        "bbb",
        json!([{"name": "second"}]),
        "second batch",
    );
    queue::enqueue(&mut conn, &config, project_id, &item2)
        .await
        .unwrap();

    // Wait for second batch to drain.
    tokio::time::sleep(std::time::Duration::from_millis(2000)).await;

    let reqs = received.lock().unwrap().clone();
    assert_eq!(
        reqs.len(),
        2,
        "expected 2 separate batch requests (first drain + second drain)"
    );

    // Second batch should have 1 request with create_ensemble endpoint.
    let (_, body) = &reqs[1];
    let requests = body.get("requests").and_then(|v| v.as_array()).unwrap();
    assert_eq!(requests.len(), 1);
    assert_eq!(
        requests[0]
            .get("endpoint")
            .and_then(|v| v.as_str())
            .unwrap(),
        "create_ensemble"
    );

    worker_handle.abort();
    cleanup_keys(&mut conn, prefix).await;
}

// ---------------------------------------------------------------------------
// inc_queueid tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_inc_queueid_first_patch() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };

    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let project = "qid_first";
    let config = test_config(project, 60.0);
    cleanup_keys(&mut conn, project).await;

    // First patch with queueid=0 should succeed and return 1.
    let result = queue::inc_queueid(&mut conn, &config, project, "main", "abc123", 0)
        .await
        .unwrap();
    assert_eq!(result, QueueIdResult::Ok { new_queueid: 1 });

    cleanup_keys(&mut conn, project).await;
}

#[tokio::test]
async fn test_inc_queueid_sequential_increments() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };

    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let project = "qid_seq";
    let config = test_config(project, 60.0);
    cleanup_keys(&mut conn, project).await;

    // First patch.
    let r1 = queue::inc_queueid(&mut conn, &config, project, "main", "abc", 0)
        .await
        .unwrap();
    assert_eq!(r1, QueueIdResult::Ok { new_queueid: 1 });

    // Second patch with queueid=1 should return 2.
    let r2 = queue::inc_queueid(&mut conn, &config, project, "main", "abc", 1)
        .await
        .unwrap();
    assert_eq!(r2, QueueIdResult::Ok { new_queueid: 2 });

    // Third patch with queueid=2 should return 3.
    let r3 = queue::inc_queueid(&mut conn, &config, project, "main", "abc", 2)
        .await
        .unwrap();
    assert_eq!(r3, QueueIdResult::Ok { new_queueid: 3 });

    cleanup_keys(&mut conn, project).await;
}

#[tokio::test]
async fn test_inc_queueid_stale_conflict() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };

    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let project = "qid_stale";
    let config = test_config(project, 60.0);
    cleanup_keys(&mut conn, project).await;

    // Create initial queue.
    let _ = queue::inc_queueid(&mut conn, &config, project, "main", "abc", 0)
        .await
        .unwrap();
    let _ = queue::inc_queueid(&mut conn, &config, project, "main", "abc", 1)
        .await
        .unwrap();

    // Stale queueid=0 should conflict (current is 2, not 0).
    let r = queue::inc_queueid(&mut conn, &config, project, "main", "abc", 0)
        .await
        .unwrap();
    assert_eq!(r, QueueIdResult::Conflict);

    // Stale queueid=1 should also conflict (current is 2, not 1).
    let r = queue::inc_queueid(&mut conn, &config, project, "main", "abc", 1)
        .await
        .unwrap();
    assert_eq!(r, QueueIdResult::Conflict);

    // Correct queueid=2 should succeed and return 3.
    let r = queue::inc_queueid(&mut conn, &config, project, "main", "abc", 2)
        .await
        .unwrap();
    assert_eq!(r, QueueIdResult::Ok { new_queueid: 3 });

    cleanup_keys(&mut conn, project).await;
}

#[tokio::test]
async fn test_inc_queueid_missing_key_with_nonzero() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };

    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let project = "qid_missing";
    let config = test_config(project, 60.0);
    cleanup_keys(&mut conn, project).await;

    // queueid > 0 but no key exists → conflict.
    let r = queue::inc_queueid(&mut conn, &config, project, "main", "abc", 1)
        .await
        .unwrap();
    assert_eq!(r, QueueIdResult::Conflict);

    cleanup_keys(&mut conn, project).await;
}

#[tokio::test]
async fn test_inc_queueid_new_commit_redirect() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };

    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let project = "qid_newcommit";
    let config = test_config(project, 60.0);
    cleanup_keys(&mut conn, project).await;

    // Simulate: first patch queued.
    let _ = queue::inc_queueid(&mut conn, &config, project, "main", "commit_a", 0)
        .await
        .unwrap();

    // Simulate: batch_patch committed and stored "commit_b,1" in the key.
    let queue_key = config.queue_entry_key(project, "main", "commit_a");
    let _: () = redis::cmd("SET")
        .arg(&queue_key)
        .arg("commit_b,1")
        .query_async(&mut conn)
        .await
        .unwrap();

    // Next patch with queueid=1 should get redirected to commit_b.
    let r = queue::inc_queueid(&mut conn, &config, project, "main", "commit_a", 1)
        .await
        .unwrap();
    assert_eq!(
        r,
        QueueIdResult::NewCommit {
            new_commit: "commit_b".into(),
            new_queueid: 1,
        }
    );

    // Verify the new key was created.
    let new_key = config.queue_entry_key(project, "main", "commit_b");
    let val: String = redis::cmd("GET")
        .arg(&new_key)
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(val, "1");

    cleanup_keys(&mut conn, project).await;
}

// ---------------------------------------------------------------------------
// check_export_queue tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_check_export_queue_redirects_to_new_commit() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };

    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let project = "exp_q_redirect";
    let config = test_config(project, 60.0);
    cleanup_keys(&mut conn, project).await;

    // Simulate batch_patch having committed: queue key stores "{new_commit},{last_queueid}".
    let queue_key = config.queue_entry_key(project, "main", "commit_a");
    let _: () = redis::cmd("SET")
        .arg(&queue_key)
        .arg("commit_b,5")
        .query_async(&mut conn)
        .await
        .unwrap();

    // Request with queueid <= last_queueid (5) should redirect to commit_b.
    let r = queue::check_export_queue(&mut conn, &config, project, "main", "commit_a", 5)
        .await
        .unwrap();
    assert_eq!(r, ExportQueueCheck::UseNewCommit("commit_b".into()));

    // Earlier queueid (e.g. 3) also redirects: those patches were
    // bundled into the same batch and committed.
    let r = queue::check_export_queue(&mut conn, &config, project, "main", "commit_a", 3)
        .await
        .unwrap();
    assert_eq!(r, ExportQueueCheck::UseNewCommit("commit_b".into()));

    cleanup_keys(&mut conn, project).await;
}

#[tokio::test]
async fn test_check_export_queue_retry_when_no_commit() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };

    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let project = "exp_q_pending";
    let config = test_config(project, 60.0);
    cleanup_keys(&mut conn, project).await;

    // Queue key exists but no commit produced yet (plain integer).
    let queue_key = config.queue_entry_key(project, "main", "commit_a");
    let _: () = redis::cmd("SET")
        .arg(&queue_key)
        .arg("3")
        .query_async(&mut conn)
        .await
        .unwrap();

    let r = queue::check_export_queue(&mut conn, &config, project, "main", "commit_a", 3)
        .await
        .unwrap();
    assert_eq!(r, ExportQueueCheck::Retry);

    cleanup_keys(&mut conn, project).await;
}

#[tokio::test]
async fn test_check_export_queue_retry_when_queueid_stale() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };

    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let project = "exp_q_stale";
    let config = test_config(project, 60.0);
    cleanup_keys(&mut conn, project).await;

    // New commit recorded with last_queueid=5; client at queueid=7 is
    // ahead — their patch is still queued past the committed batch.
    let queue_key = config.queue_entry_key(project, "main", "commit_a");
    let _: () = redis::cmd("SET")
        .arg(&queue_key)
        .arg("commit_b,5")
        .query_async(&mut conn)
        .await
        .unwrap();

    let r = queue::check_export_queue(&mut conn, &config, project, "main", "commit_a", 7)
        .await
        .unwrap();
    assert_eq!(r, ExportQueueCheck::Retry);

    cleanup_keys(&mut conn, project).await;
}

#[tokio::test]
async fn test_check_export_queue_retry_when_key_missing() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };

    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let project = "exp_q_missing";
    let config = test_config(project, 60.0);
    cleanup_keys(&mut conn, project).await;

    // No queue key at all → caller should retry; we have no evidence
    // the client's queued write has been processed.
    let r = queue::check_export_queue(&mut conn, &config, project, "main", "commit_a", 1)
        .await
        .unwrap();
    assert_eq!(r, ExportQueueCheck::Retry);

    cleanup_keys(&mut conn, project).await;
}

// ---------------------------------------------------------------------------
// has_pending_writes tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_has_pending_writes_empty() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let project = "pending_empty";
    let config = test_config(project, 60.0);
    cleanup_keys(&mut conn, project).await;

    assert!(
        !queue::has_pending_writes(&mut conn, &config, project, "main")
            .await
            .unwrap()
    );

    cleanup_keys(&mut conn, project).await;
}

#[tokio::test]
async fn test_has_pending_writes_matches_branch() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let project = "pending_match";
    let config = test_config(project, 60.0);
    cleanup_keys(&mut conn, project).await;

    // Enqueue an item targeting branch "main".
    let item = make_item("/update_ensemble", "main", "abc", json!([{"a": 1}]), "msg");
    queue::enqueue(&mut conn, &config, project, &item)
        .await
        .unwrap();

    // Matching branch → busy.
    assert!(
        queue::has_pending_writes(&mut conn, &config, project, "main")
            .await
            .unwrap()
    );
    // Different branch → not busy.
    assert!(
        !queue::has_pending_writes(&mut conn, &config, project, "dev")
            .await
            .unwrap()
    );

    cleanup_keys(&mut conn, project).await;
}

#[tokio::test]
async fn test_has_pending_writes_when_worker_lock_held() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let project = "pending_lock";
    let config = test_config(project, 60.0);
    cleanup_keys(&mut conn, project).await;

    // Simulate `run_worker` holding the per-project batch lock while
    // forwarding to Python (items already drained, list is empty).
    let lock_key = format!(
        "{}{}",
        config.batch_lock_prefix(),
        config.batch_list_key(project)
    );
    let _: () = redis::cmd("SET")
        .arg(&lock_key)
        .arg("1")
        .arg("EX")
        .arg(60)
        .query_async(&mut conn)
        .await
        .unwrap();

    // List is empty but the lock is held → busy regardless of branch.
    assert!(
        queue::has_pending_writes(&mut conn, &config, project, "main")
            .await
            .unwrap()
    );
    assert!(
        queue::has_pending_writes(&mut conn, &config, project, "dev")
            .await
            .unwrap()
    );

    cleanup_keys(&mut conn, project).await;
}

// ---------------------------------------------------------------------------
// kick_worker tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_kick_worker_is_noop_when_no_work_queued() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let project = "kick_noop";
    let config = test_config(project, 60.0);
    cleanup_keys(&mut conn, project).await;

    // No prior enqueue → ready set is empty for this project.
    queue::kick_worker(&mut conn, &config, project)
        .await
        .unwrap();

    // ZADD XX must not insert the list_key because it didn't exist.
    let score: Option<f64> = redis::cmd("ZSCORE")
        .arg(config.batch_ready_set_key())
        .arg(config.batch_list_key(project))
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(
        score, None,
        "kick_worker must not create a phantom ready-set entry"
    );

    cleanup_keys(&mut conn, project).await;
}

#[tokio::test]
async fn test_kick_worker_lowers_deadline() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    let project = "kick_lowers";
    // Long window so the deadline set by `enqueue` is comfortably in the future.
    let config = test_config(project, 600.0);
    cleanup_keys(&mut conn, project).await;

    // Enqueue an item to populate the ready set with a future deadline.
    let item = make_item("/update_ensemble", "main", "abc", json!([{"a": 1}]), "msg");
    queue::enqueue(&mut conn, &config, project, &item)
        .await
        .unwrap();
    let initial_score: f64 = redis::cmd("ZSCORE")
        .arg(config.batch_ready_set_key())
        .arg(config.batch_list_key(project))
        .query_async(&mut conn)
        .await
        .unwrap();

    // Read the Redis "now" timestamp to compare against.
    let (now_secs, now_micros): (i64, i64) =
        redis::cmd("TIME").query_async(&mut conn).await.unwrap();
    let now = now_secs as f64 + now_micros as f64 / 1_000_000.0;
    assert!(
        initial_score > now + 1.0,
        "enqueue should set a future deadline ({initial_score} vs now {now})"
    );

    queue::kick_worker(&mut conn, &config, project)
        .await
        .unwrap();

    let kicked_score: f64 = redis::cmd("ZSCORE")
        .arg(config.batch_ready_set_key())
        .arg(config.batch_list_key(project))
        .query_async(&mut conn)
        .await
        .unwrap();
    // After kick, the deadline is "now" — i.e. no later than the
    // current Redis time (allowing a small slack for clock drift).
    let (now_secs, now_micros): (i64, i64) =
        redis::cmd("TIME").query_async(&mut conn).await.unwrap();
    let now_after = now_secs as f64 + now_micros as f64 / 1_000_000.0;
    assert!(
        kicked_score <= now_after + 0.5 && kicked_score < initial_score,
        "kick_worker should lower the deadline to ~now; \
         initial={initial_score}, kicked={kicked_score}, now={now_after}"
    );

    cleanup_keys(&mut conn, project).await;
}

/// Spin up a backend that rejects every request with `status`.
async fn failing_backend(status: u16) -> String {
    let code = axum::http::StatusCode::from_u16(status).unwrap();
    let app = axum::Router::new().fallback(move || async move { (code, "Missing credentials") });
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    format!("http://{}", addr)
}

/// A rejected batch must leave a mark on the queue key.
///
/// The items are LPOP'd before the forward, and python only writes the
/// queue key on success, so before the sentinel a failed batch left the
/// key holding the pre-batch queueid: the writes were gone and the
/// client's next write was told everything was fine.
#[tokio::test]
async fn failed_batch_marks_queue_key() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };

    let prefix = "batch_failed";
    let config = test_config(prefix, 1.0);
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    cleanup_keys(&mut conn, prefix).await;

    let project = "proj_failed";
    let commit = "commit_a";

    // Claim the first queueid the way handle_write does, so the key exists
    // with the pre-batch value a stale client would be handed back.
    let first = queue::inc_queueid(&mut conn, &config, project, "main", commit, 0)
        .await
        .unwrap();
    assert_eq!(first, QueueIdResult::Ok { new_queueid: 1 });

    let item = make_item(
        &format!("/update_ensemble?auth_project={}", project),
        "main",
        commit,
        json!([{ "__typename": "ResourceTemplate", "name": "t0" }]),
        "change 0",
    );
    queue::enqueue(&mut conn, &config, project, &item)
        .await
        .unwrap();

    // 401 is the failure that exposed this: batched sub-requests were
    // reaching python without the credentials from the outer request.
    let backend_url = failing_backend(401).await;
    let worker_conn = client.get_multiplexed_async_connection().await.unwrap();
    let worker_config = config.clone();
    let handle = tokio::spawn(async move {
        queue::run_worker(
            worker_conn,
            worker_config,
            backend_url,
            reqwest::Client::new(),
        )
        .await;
    });
    tokio::time::sleep(std::time::Duration::from_millis(2500)).await;

    let list_key = config.batch_list_key(project);
    let len: i64 = redis::cmd("LLEN")
        .arg(&list_key)
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(len, 0, "the writes are gone from the list either way");

    let queue_key = config.queue_entry_key(project, "main", commit);
    let value: Option<String> = redis::cmd("GET")
        .arg(&queue_key)
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(
        value.as_deref(),
        Some("failed:401:0"),
        "queue key should record the failure, not keep the pre-batch queueid"
    );

    // What the client's next write sees.
    let next = queue::inc_queueid(&mut conn, &config, project, "main", commit, 1)
        .await
        .unwrap();
    assert_eq!(
        next,
        QueueIdResult::Failed {
            status: 401,
            queueid: 0
        },
    );

    // The read path has to report it too: a create-then-read flow never
    // issues the second write that inc_queueid above would fail, so
    // without this /export polls 503 to a timeout and the UI reports
    // success. Asserting Failed also keeps the "no comma in the
    // sentinel" guard honest -- a comma would make the left side parse
    // as a revision and return UseNewCommit for a commit that was never
    // created.
    let export = queue::check_export_queue(&mut conn, &config, project, "main", commit, 1)
        .await
        .unwrap();
    assert_eq!(
        export,
        ExportQueueCheck::Failed {
            status: 401,
            queueid: 0
        },
    );

    // The sentinel is bounded: a poisoned commit key must not outlive
    // the clients that could still be holding that commit.
    let ttl: i64 = redis::cmd("TTL")
        .arg(&queue_key)
        .query_async(&mut conn)
        .await
        .unwrap();
    assert!(
        ttl > 0 && ttl <= config.queue_key_ttl_secs as i64,
        "sentinel should expire, got TTL {ttl}"
    );

    cleanup_keys(&mut conn, prefix).await;
    handle.abort();
}

/// Every key `inc_queueid` writes expires, not just the failed sentinel.
///
/// Without this a project accumulates one key per commit ever written
/// against it, for its whole life -- the success path never deletes them
/// and nothing else reaps them.
#[tokio::test]
async fn queue_keys_written_by_inc_queueid_expire() {
    let Some(url) = redis_url() else {
        eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
        return;
    };
    let prefix = "ttl_counter";
    let config = test_config(prefix, 1.0);
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    cleanup_keys(&mut conn, prefix).await;

    let project = "proj_ttl";
    // The first write creates the counter -- the key that used to be
    // written bare.
    queue::inc_queueid(&mut conn, &config, project, "main", "commit_ttl", 0)
        .await
        .expect("inc_queueid");

    let key = config.queue_entry_key(project, "main", "commit_ttl");
    let value: Option<String> = redis::cmd("GET")
        .arg(&key)
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(value.as_deref(), Some("1"), "counter should exist");

    let ttl: i64 = redis::cmd("TTL")
        .arg(&key)
        .query_async(&mut conn)
        .await
        .unwrap();
    assert!(
        ttl > 0 && ttl <= config.queue_key_ttl_secs as i64,
        "the counter should expire, got TTL {ttl} (-1 means no expiry)"
    );

    cleanup_keys(&mut conn, prefix).await;
}

/// The other failure arm: no response at all.
///
/// A backend that is down or drops the connection takes the `Err(_)` path,
/// which has no status to report and records 502. This is a common way a
/// batch dies, and it is a different branch from the non-2xx one.
#[tokio::test]
async fn unreachable_backend_marks_queue_key() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };

    let prefix = "batch_unreachable";
    let config = test_config(prefix, 1.0);
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    cleanup_keys(&mut conn, prefix).await;

    let project = "proj_unreachable";
    let commit = "commit_a";

    // Bind to claim a port, read it, then drop the listener: nothing is
    // accepting there, so the forward fails without a response.
    let dead = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let backend_url = format!("http://{}", dead.local_addr().unwrap());
    drop(dead);

    let item = make_item(
        &format!("/update_ensemble?auth_project={}", project),
        "main",
        commit,
        json!([{ "__typename": "ResourceTemplate", "name": "t0" }]),
        "change 0",
    );
    queue::enqueue(&mut conn, &config, project, &item)
        .await
        .unwrap();

    let worker_conn = client.get_multiplexed_async_connection().await.unwrap();
    let worker_config = config.clone();
    let handle = tokio::spawn(async move {
        queue::run_worker(
            worker_conn,
            worker_config,
            backend_url,
            reqwest::Client::new(),
        )
        .await;
    });
    tokio::time::sleep(std::time::Duration::from_millis(2500)).await;

    let queue_key = config.queue_entry_key(project, "main", commit);
    let value: Option<String> = redis::cmd("GET")
        .arg(&queue_key)
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(value.as_deref(), Some("failed:502:0"));

    let next = queue::inc_queueid(&mut conn, &config, project, "main", commit, 0)
        .await
        .unwrap();
    assert_eq!(
        next,
        QueueIdResult::Failed {
            status: 502,
            queueid: 0
        },
    );

    cleanup_keys(&mut conn, prefix).await;
    handle.abort();
}

// ---------------------------------------------------------------------------
// The 409 a read gets when the write it is waiting on was discarded
// ---------------------------------------------------------------------------

/// `GET /export?queueid=N` against the real router, with Redis wired up.
///
/// `redis: None` (what the other router tests use) makes
/// `resolve_queued_request` return before it ever consults the queue, so
/// reaching the queue paths at all needs a live connection here.
async fn export_with_queueid(
    config: &Config,
    conn: redis::aio::MultiplexedConnection,
    project: &str,
    commit: &str,
    queueid: i64,
) -> (StatusCode, JsonValue) {
    let state = AppState {
        config: Arc::new(config.clone()),
        client: reqwest::Client::new(),
        redis: Some(conn),
        cloudmap: None,
    };
    let uri = format!(
        "/export?auth_project={}&branch=main&latest_commit={}&queueid={}",
        urlencoding::encode(project),
        commit,
        queueid
    );
    let req = Request::builder().uri(uri).body(Body::empty()).unwrap();
    let res = build_router(state, None).oneshot(req).await.unwrap();
    let status = res.status();
    let bytes = axum::body::to_bytes(res.into_body(), 64 * 1024)
        .await
        .unwrap();
    let body = serde_json::from_slice(&bytes).unwrap_or(JsonValue::Null);
    (status, body)
}

/// A queueid with no branch is refused, not answered from the stale
/// commit it arrived with.
///
/// The queue key is per branch, so such a request cannot be resolved.
/// Passing it through instead looks like success and serves the
/// pre-write state -- which is how a real CI failure presented: the
/// export returned "initial" where the test had patched "target", with
/// no error anywhere.
#[tokio::test]
async fn a_queueid_without_a_branch_is_refused() {
    let Some(url) = redis_url() else {
        eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
        return;
    };
    let config = test_config("no_branch", 1.0);
    let client = redis::Client::open(url.as_str()).unwrap();
    let conn = client.get_multiplexed_async_connection().await.unwrap();
    let state = AppState {
        config: Arc::new(config),
        client: reqwest::Client::new(),
        redis: Some(conn),
        cloudmap: None,
    };
    let res = build_router(state, None)
        .oneshot(
            Request::builder()
                .uri("/export?auth_project=proj&latest_commit=aaa&queueid=3&format=deployment")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    let bytes = axum::body::to_bytes(res.into_body(), 64 * 1024)
        .await
        .unwrap();
    let body: JsonValue = serde_json::from_slice(&bytes).unwrap_or(JsonValue::Null);
    assert!(
        body["message"]
            .as_str()
            .unwrap_or_default()
            .contains("branch"),
        "{body}"
    );
}

/// The shape the unfurl-gui client matches on.
///
/// `check_export_queue` returning `Failed` is only half the fix -- the
/// route has to turn it into a terminal answer. 409 and not 503 because
/// retrying is futile, and the client already treats 409 as "clear your
/// stored commit". `latest_commit` rides along so it can re-read without
/// a round trip to find out where it stands.
#[tokio::test]
async fn discarded_write_answers_export_with_409() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };
    let prefix = "export_discarded";
    let config = test_config(prefix, 1.0);
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    cleanup_keys(&mut conn, prefix).await;

    let project = "proj_discarded";
    let commit = "commit_x";

    // No sentinel yet: the request must not be answered 409 on the
    // strength of a queueid alone, or an ordinary wait looks like a
    // dropped write. Given its own 1s wait budget because this is the
    // one case that deliberately runs the budget out.
    let impatient = Config {
        proxy_timeout_secs: 1,
        ..config.clone()
    };
    let (status, _) = export_with_queueid(&impatient, conn.clone(), project, commit, 3).await;
    assert_eq!(
        status,
        StatusCode::SERVICE_UNAVAILABLE,
        "a write still in flight is a retry, not a discard"
    );

    // What `mark_batch_failed` leaves behind -- the sentinel and the
    // backend's error body beside it. Written directly so this test pins
    // the route's behaviour and not the worker's.
    let queue_key = config.queue_entry_key(project, "main", commit);
    let _: () = redis::cmd("SET")
        .arg(&queue_key)
        .arg("failed:401:2")
        .query_async(&mut conn)
        .await
        .unwrap();
    let error_key = config.queue_error_key(project, "main", commit);
    let _: () = redis::cmd("SET")
        .arg(&error_key)
        .arg(r#"{"status":401,"code":"UNAUTHORIZED","message":"nope","rolled_back":true}"#)
        .query_async(&mut conn)
        .await
        .unwrap();

    let started = std::time::Instant::now();
    let (status, body) = export_with_queueid(&config, conn.clone(), project, commit, 3).await;
    let waited = started.elapsed();
    assert_eq!(status, StatusCode::CONFLICT, "body: {body}");
    // Answered by the check before the wait loop, not by the one inside
    // it. Both arms return the same 409, so elapsed time is what tells
    // them apart: the loop sleeps a full poll interval (100ms) before its
    // first look, and the connection is already warm from the request
    // above.
    assert!(
        waited < std::time::Duration::from_millis(100),
        "a sentinel that is already there should not go through the wait \
         loop, took {waited:?}"
    );
    assert_eq!(body["code"], "WRITE_DISCARDED", "body: {body}");
    // The backend's own report travels with it, nested so its `code` and
    // the proxy's do not collide, and carrying `rolled_back` -- how a
    // client tells "nothing survived" from "something did".
    assert_eq!(body["error"]["code"], "UNAUTHORIZED", "body: {body}");
    assert_eq!(body["error"]["rolled_back"], true, "body: {body}");
    assert_eq!(body["latest_commit"], commit, "body: {body}");
    assert_eq!(body["queueid"], 2, "body: {body}");
    // The backend status belongs in the message: "your last change wasn't
    // saved" reads differently from a real conflict, and the client shows
    // it verbatim.
    assert!(
        body["message"].as_str().unwrap_or_default().contains("401"),
        "message should name the status that discarded it: {body}"
    );

    cleanup_keys(&mut conn, prefix).await;
}

/// A batch can fail while a client is already blocked in the wait loop.
///
/// Creation is a write-then-read flow, so the read usually arrives
/// *before* the worker has forwarded anything -- it goes past the fast
/// path into the poll loop, and the failure lands there. Without the arm
/// inside the loop this polls until `proxy_timeout_secs` and answers 503,
/// telling the client to retry a write that is already gone.
#[tokio::test]
async fn discarded_write_is_reported_to_a_waiting_reader() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };
    let prefix = "export_discarded_wait";
    let config = test_config(prefix, 1.0);
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    cleanup_keys(&mut conn, prefix).await;

    let project = "proj_waiting";
    let commit = "commit_y";
    let queue_key = config.queue_entry_key(project, "main", commit);

    // The pre-batch value a client's write left behind: no commit yet, so
    // the fast path says Retry and the request enters the loop.
    let _: () = redis::cmd("SET")
        .arg(&queue_key)
        .arg("1")
        .query_async(&mut conn)
        .await
        .unwrap();

    // Fail the batch after the reader is already waiting. The loop polls
    // every 100ms and `test_config` gives it a 10s budget, so this lands
    // mid-wait with plenty of room.
    let mut writer = client.get_multiplexed_async_connection().await.unwrap();
    let key = queue_key.clone();
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(400)).await;
        let _: () = redis::cmd("SET")
            .arg(&key)
            .arg("failed:502:1")
            .query_async(&mut writer)
            .await
            .unwrap();
    });

    let started = std::time::Instant::now();
    let (status, body) = export_with_queueid(&config, conn.clone(), project, commit, 1).await;
    let waited = started.elapsed();

    assert_eq!(status, StatusCode::CONFLICT, "body: {body}");
    assert_eq!(body["code"], "WRITE_DISCARDED", "body: {body}");
    // It has to have gone through the loop to be this test rather than a
    // repeat of the fast-path one, and it has to have come back before the
    // wait budget or a 503 timeout would be indistinguishable.
    assert!(
        waited >= std::time::Duration::from_millis(400),
        "should have waited for the sentinel, returned after {waited:?}"
    );
    assert!(
        waited < std::time::Duration::from_secs(config.proxy_timeout_secs),
        "should not have run out the wait budget: {waited:?}"
    );

    cleanup_keys(&mut conn, prefix).await;
}

// ---------------------------------------------------------------------------
// A batch that commits nothing
// ---------------------------------------------------------------------------

/// The deadlock this fixes, from a real stuck project.
///
/// `batch_patch` records `{repo.revision},{queueid}` unconditionally, so a
/// batch that committed nothing -- a no-op patch, or a working copy that was
/// already dirty -- writes a value naming the key's own commit. There is then
/// no newer commit to redirect anyone to, and both branches of the script
/// rejected every writer: a lower queueid as stale, and an equal or higher one
/// because `newer_key` resolves to this very key and reads as a patch already
/// in flight. The commit became permanently unwritable at any queueid, and
/// re-reading returned the same commit, so no client could recover.
#[tokio::test]
async fn settled_queue_key_stays_writable() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };
    let prefix = "settled_queue";
    let config = test_config(prefix, 1.0);
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    cleanup_keys(&mut conn, prefix).await;

    let project = "proj_settled";
    let commit = "83b9478d9e7250c91c900faa3b7e5adb935b8683";
    let queue_key = config.queue_entry_key(project, "main", commit);

    // Every queueid a client can send, including the 0 of a fresh page load.
    for (sent, expected) in [(0, 2), (1, 2), (5, 2)] {
        let _: () = redis::cmd("SET")
            .arg(&queue_key)
            .arg(format!("{commit},1"))
            .query_async(&mut conn)
            .await
            .unwrap();
        let result = queue::inc_queueid(&mut conn, &config, project, "main", commit, sent)
            .await
            .unwrap();
        assert_eq!(
            result,
            QueueIdResult::Ok {
                new_queueid: expected
            },
            "queueid {sent} against a settled key"
        );
        // Collapsed back to a plain counter, so the next writer just INCRs.
        let value: String = redis::cmd("GET")
            .arg(&queue_key)
            .query_async(&mut conn)
            .await
            .unwrap();
        assert_eq!(value, expected.to_string());
    }

    // ...and the counter keeps advancing from there.
    let next = queue::inc_queueid(&mut conn, &config, project, "main", commit, 2)
        .await
        .unwrap();
    assert_eq!(next, QueueIdResult::Ok { new_queueid: 3 });

    // A client below the recorded queueid is admitted too. It looks stale,
    // but the queueid only orders writes against a commit and this batch
    // produced none: HEAD is what the client already has, so there is
    // nothing for it to be behind and nothing a 409 would make it re-read.
    let _: () = redis::cmd("SET")
        .arg(&queue_key)
        .arg(format!("{commit},3"))
        .query_async(&mut conn)
        .await
        .unwrap();
    let behind = queue::inc_queueid(&mut conn, &config, project, "main", commit, 1)
        .await
        .unwrap();
    assert_eq!(
        behind,
        QueueIdResult::Ok { new_queueid: 4 },
        "queueid 1 against a settled key recording 3"
    );

    cleanup_keys(&mut conn, prefix).await;
}

/// A real redirect must still conflict: the guard is scoped to a value that
/// names its *own* commit, not to every `{commit},{n}`.
#[tokio::test]
async fn redirect_to_a_taken_commit_still_conflicts() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };
    let prefix = "settled_redirect";
    let config = test_config(prefix, 1.0);
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    cleanup_keys(&mut conn, prefix).await;

    let project = "proj_redirect";
    let old_commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    let new_commit = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    // The old commit redirects to a new one that already has a queue.
    let _: () = redis::cmd("SET")
        .arg(config.queue_entry_key(project, "main", old_commit))
        .arg(format!("{new_commit},1"))
        .query_async(&mut conn)
        .await
        .unwrap();
    let _: () = redis::cmd("SET")
        .arg(config.queue_entry_key(project, "main", new_commit))
        .arg("1")
        .query_async(&mut conn)
        .await
        .unwrap();

    let result = queue::inc_queueid(&mut conn, &config, project, "main", old_commit, 1)
        .await
        .unwrap();
    assert_eq!(result, QueueIdResult::Conflict);

    cleanup_keys(&mut conn, prefix).await;
}

/// The read half of the settled-key fix. A value naming the key's own commit
/// means the batch finished without moving HEAD, so the export the reader
/// already asked for is current and it should be let through. Nothing else
/// releases it: no further commit is coming, so a `Retry` here polls until
/// `proxy_timeout_secs` and answers 503.
#[tokio::test]
async fn settled_queue_key_releases_waiting_readers() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };
    let prefix = "settled_reader";
    let config = test_config(prefix, 1.0);
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    cleanup_keys(&mut conn, prefix).await;

    let project = "proj_settled_reader";
    let commit = "83b9478d9e7250c91c900faa3b7e5adb935b8683";
    let _: () = redis::cmd("SET")
        .arg(config.queue_entry_key(project, "main", commit))
        .arg(format!("{commit},3"))
        .query_async(&mut conn)
        .await
        .unwrap();

    // Every reader the settled batch covers proceeds at the commit it has.
    for qid in [1, 2, 3] {
        let result = queue::check_export_queue(&mut conn, &config, project, "main", commit, qid)
            .await
            .unwrap();
        assert_eq!(
            result,
            ExportQueueCheck::UseNewCommit(commit.to_string()),
            "reader holding queueid {qid}"
        );
    }

    // A reader ahead of the settled batch has writes still outstanding.
    let result = queue::check_export_queue(&mut conn, &config, project, "main", commit, 4)
        .await
        .unwrap();
    assert_eq!(result, ExportQueueCheck::Retry);

    cleanup_keys(&mut conn, prefix).await;
}

/// Repairing a settled key resumes above the queueids it already handed out.
/// A reader holding one of those is released by the next real commit only if
/// the counter never went backwards: resuming at 1 would record "{next},1",
/// and every reader at 2 or above would fail `last_queueid < request_queueid`
/// and poll to its deadline. That is a reader-side timeout caused by a
/// writer-side constant, so it is pinned here rather than left implied.
#[tokio::test]
async fn repaired_queue_key_still_covers_earlier_readers() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };
    let prefix = "settled_resume";
    let config = test_config(prefix, 1.0);
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    cleanup_keys(&mut conn, prefix).await;

    let project = "proj_resume";
    let commit = "cccccccccccccccccccccccccccccccccccccccc";
    let next_commit = "dddddddddddddddddddddddddddddddddddddddd";
    let queue_key = config.queue_entry_key(project, "main", commit);

    // A settled batch that had issued three queueids; a reader is still
    // holding the last of them.
    let _: () = redis::cmd("SET")
        .arg(&queue_key)
        .arg(format!("{commit},3"))
        .query_async(&mut conn)
        .await
        .unwrap();
    let repaired = queue::inc_queueid(&mut conn, &config, project, "main", commit, 0)
        .await
        .unwrap();
    let QueueIdResult::Ok { new_queueid } = repaired else {
        panic!("expected the settled key to be repaired, got {repaired:?}");
    };

    // That write commits for real, recorded the way batch_patch records it.
    let _: () = redis::cmd("SET")
        .arg(&queue_key)
        .arg(format!("{next_commit},{new_queueid}"))
        .query_async(&mut conn)
        .await
        .unwrap();

    let result = queue::check_export_queue(&mut conn, &config, project, "main", commit, 3)
        .await
        .unwrap();
    assert_eq!(
        result,
        ExportQueueCheck::UseNewCommit(next_commit.to_string()),
        "reader holding a queueid issued before the settled batch"
    );

    cleanup_keys(&mut conn, prefix).await;
}

/// Once collapsed back to a counter the key is ordinary again: a later batch
/// that does produce a commit redirects from it normally, rather than the
/// repair leaving it in a state the redirect path no longer recognises.
#[tokio::test]
async fn repaired_queue_key_redirects_after_a_real_commit() {
    let url = match redis_url() {
        Some(u) => u,
        None => {
            eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
            return;
        }
    };
    let prefix = "settled_then_commit";
    let config = test_config(prefix, 1.0);
    let client = redis::Client::open(url.as_str()).unwrap();
    let mut conn = client.get_multiplexed_async_connection().await.unwrap();
    cleanup_keys(&mut conn, prefix).await;

    let project = "proj_then_commit";
    let commit = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
    let next_commit = "ffffffffffffffffffffffffffffffffffffffff";
    let queue_key = config.queue_entry_key(project, "main", commit);

    let _: () = redis::cmd("SET")
        .arg(&queue_key)
        .arg(format!("{commit},1"))
        .query_async(&mut conn)
        .await
        .unwrap();
    let repaired = queue::inc_queueid(&mut conn, &config, project, "main", commit, 0)
        .await
        .unwrap();
    assert_eq!(repaired, QueueIdResult::Ok { new_queueid: 2 });

    let _: () = redis::cmd("SET")
        .arg(&queue_key)
        .arg(format!("{next_commit},2"))
        .query_async(&mut conn)
        .await
        .unwrap();
    let result = queue::inc_queueid(&mut conn, &config, project, "main", commit, 2)
        .await
        .unwrap();
    assert_eq!(
        result,
        QueueIdResult::NewCommit {
            new_commit: next_commit.to_string(),
            new_queueid: 1,
        }
    );
    let started: String = redis::cmd("GET")
        .arg(config.queue_entry_key(project, "main", next_commit))
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(started, "1");

    cleanup_keys(&mut conn, prefix).await;
}
