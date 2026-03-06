// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Integration tests for Redis-backed batch queue.
//!
//! These tests require a live Redis instance.  They only run when the
//! `UNFURL_TEST_REDIS_URL` environment variable is set.
//!
//! Run with:
//!   UNFURL_TEST_REDIS_URL="redis://localhost:6379/2" cargo test --test test_batch_queue

use serde_json::{json, Value as JsonValue};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use tokio::net::TcpListener;
use unfurl_server::config::Config;
use unfurl_server::queue::{self, QueueIdResult, QueueItem};

/// Return the Redis URL from the environment, or `None` to skip.
fn redis_url() -> Option<String> {
    std::env::var("UNFURL_TEST_REDIS_URL").ok()
}

/// Build a test Config with a unique key prefix to avoid collisions.
fn test_config(prefix: &str, batch_window_secs: u64) -> Config {
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
    let config = test_config(prefix, 1); // 1-second window for fast tests

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
    let config = test_config(prefix, 1);

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
    let config = test_config(prefix, 1);

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
    let config = test_config(prefix, 1);

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
    let config = test_config(prefix, 1);

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

/// Helper to clean up queue keys.
async fn cleanup_queue_keys(conn: &mut redis::aio::MultiplexedConnection, project_id: &str) {
    let pattern = format!("queue:{}:*", project_id);
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
    cleanup_queue_keys(&mut conn, project).await;

    // First patch with queueid=0 should succeed and return 1.
    let result = queue::inc_queueid(&mut conn, project, "abc123", 0)
        .await
        .unwrap();
    assert_eq!(result, QueueIdResult::Ok { new_queueid: 1 });

    cleanup_queue_keys(&mut conn, project).await;
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
    cleanup_queue_keys(&mut conn, project).await;

    // First patch.
    let r1 = queue::inc_queueid(&mut conn, project, "abc", 0)
        .await
        .unwrap();
    assert_eq!(r1, QueueIdResult::Ok { new_queueid: 1 });

    // Second patch with queueid=1 should return 2.
    let r2 = queue::inc_queueid(&mut conn, project, "abc", 1)
        .await
        .unwrap();
    assert_eq!(r2, QueueIdResult::Ok { new_queueid: 2 });

    // Third patch with queueid=2 should return 3.
    let r3 = queue::inc_queueid(&mut conn, project, "abc", 2)
        .await
        .unwrap();
    assert_eq!(r3, QueueIdResult::Ok { new_queueid: 3 });

    cleanup_queue_keys(&mut conn, project).await;
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
    cleanup_queue_keys(&mut conn, project).await;

    // Create initial queue.
    let _ = queue::inc_queueid(&mut conn, project, "abc", 0)
        .await
        .unwrap();
    let _ = queue::inc_queueid(&mut conn, project, "abc", 1)
        .await
        .unwrap();

    // Stale queueid=0 should conflict (current is 2, not 0).
    let r = queue::inc_queueid(&mut conn, project, "abc", 0)
        .await
        .unwrap();
    assert_eq!(r, QueueIdResult::Conflict);

    // Stale queueid=1 should also conflict (current is 2, not 1).
    let r = queue::inc_queueid(&mut conn, project, "abc", 1)
        .await
        .unwrap();
    assert_eq!(r, QueueIdResult::Conflict);

    // Correct queueid=2 should succeed and return 3.
    let r = queue::inc_queueid(&mut conn, project, "abc", 2)
        .await
        .unwrap();
    assert_eq!(r, QueueIdResult::Ok { new_queueid: 3 });

    cleanup_queue_keys(&mut conn, project).await;
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
    cleanup_queue_keys(&mut conn, project).await;

    // queueid > 0 but no key exists → conflict.
    let r = queue::inc_queueid(&mut conn, project, "abc", 1)
        .await
        .unwrap();
    assert_eq!(r, QueueIdResult::Conflict);

    cleanup_queue_keys(&mut conn, project).await;
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
    cleanup_queue_keys(&mut conn, project).await;

    // Simulate: first patch queued.
    let _ = queue::inc_queueid(&mut conn, project, "commit_a", 0)
        .await
        .unwrap();

    // Simulate: batch_patch committed and stored "commit_b,1" in the key.
    let queue_key = format!("queue:{}:commit_a", project);
    let _: () = redis::cmd("SET")
        .arg(&queue_key)
        .arg("commit_b,1")
        .query_async(&mut conn)
        .await
        .unwrap();

    // Next patch with queueid=1 should get redirected to commit_b.
    let r = queue::inc_queueid(&mut conn, project, "commit_a", 1)
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
    let new_key = format!("queue:{}:commit_b", project);
    let val: String = redis::cmd("GET")
        .arg(&new_key)
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(val, "1");

    cleanup_queue_keys(&mut conn, project).await;
}
