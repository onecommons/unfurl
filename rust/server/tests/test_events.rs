// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `GET /events` -- the queue subscription.
//!
//! Requires a live Redis; skipped when `UNFURL_TEST_REDIS_URL` is unset,
//! as in `test_batch_queue.rs`.
//!
//! Run with:
//!   UNFURL_TEST_REDIS_URL="redis://127.0.0.1:6399/0" cargo test --test test_events

use axum::body::Body;
use axum::http::{Request, StatusCode};
use std::sync::Arc;
use tower::ServiceExt;
use unfurl_server::config::Config;
use unfurl_server::{build_router, AppState};

fn redis_url() -> Option<String> {
    std::env::var("UNFURL_TEST_REDIS_URL").ok()
}

fn test_config(prefix: &str) -> Config {
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
        batch_window_secs: 0.05,
        worker_poll_interval_secs: 0.05,
        queue_key_ttl_secs: 3600,
        events_budget_secs: 3,
        cloudmap_repo: None,
        cloudmap_db_url: None,
        cloudmap_force: false,
        local: None,
        cors_origins: None,
    }
}

/// A router wired to Redis, plus a connection for planting queue keys.
async fn fixture(
    prefix: &str,
) -> Option<(axum::Router, redis::aio::MultiplexedConnection, Config)> {
    let url = redis_url()?;
    let client = redis::Client::open(url).expect("redis client");
    let conn = client
        .get_multiplexed_tokio_connection()
        .await
        .expect("redis connect");
    let config = test_config(prefix);
    let state = AppState {
        config: Arc::new(config.clone()),
        client: reqwest::Client::new(),
        redis: Some(conn.clone()),
        cloudmap: None,
    };
    Some((build_router(state, None), conn, config))
}

/// Collect the whole stream body as text. The handler closes after its
/// terminal frame, so this returns rather than hanging.
async fn body_text(response: axum::response::Response) -> String {
    let bytes = axum::body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .expect("read body");
    String::from_utf8(bytes.to_vec()).expect("utf-8")
}

async fn get(router: &axum::Router, uri: &str) -> axum::response::Response {
    router
        .clone()
        .oneshot(Request::builder().uri(uri).body(Body::empty()).unwrap())
        .await
        .expect("request")
}

/// A settled write is reported with the commit that superseded it, and
/// the stream ends so the client can close rather than reconnect.
#[tokio::test]
async fn a_settled_write_is_reported_once() {
    let Some((router, mut conn, config)) = fixture("events_ok").await else {
        eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
        return;
    };
    let key = config.queue_entry_key("proj", "main", "aaa");
    let _: () = redis::cmd("SET")
        .arg(&key)
        .arg("bbb,4")
        .query_async(&mut conn)
        .await
        .expect("plant key");

    let body = body_text(get(&router, "/events?auth_project=proj&watch=main:aaa:4").await).await;

    assert!(body.contains(r#""status":"ok""#), "{body}");
    assert!(
        body.contains(r#""branch":"main""#),
        "the client's branch must come back -- the queue key has none: {body}"
    );
    assert!(body.contains(r#""new_commit":"bbb""#), "{body}");
    assert!(body.contains(r#""latest_commit":"aaa""#), "{body}");
    assert!(body.contains(r#""status":"done""#), "{body}");
    assert_eq!(body.matches(r#""status":"ok""#).count(), 1, "{body}");

    let _: () = redis::cmd("DEL")
        .arg(&key)
        .query_async(&mut conn)
        .await
        .expect("cleanup");
}

/// A discarded batch reports the *client's* queueid, not the batch's.
///
/// The client decides whether an event still applies by comparing
/// against what it queued; the batch's last queueid says nothing about
/// that, and a client that wrote again after the batch was cut holds a
/// higher one. Reporting only the batch's would make such a client skip
/// the event and never learn its earlier writes were dropped.
#[tokio::test]
async fn a_discarded_write_reports_the_clients_queueid() {
    let Some((router, mut conn, config)) = fixture("events_failed").await else {
        eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
        return;
    };
    let key = config.queue_entry_key("proj", "main", "aaa");
    let _: () = redis::cmd("SET")
        .arg(&key)
        .arg("failed:500:9")
        .query_async(&mut conn)
        .await
        .expect("plant sentinel");
    // What `mark_batch_failed` stores alongside it: the body Python
    // returned, shaped by `create_error_response`.
    let error_key = config.queue_error_key("proj", "main", "aaa");
    let _: () = redis::cmd("SET")
        .arg(&error_key)
        .arg(
            r#"{"status":500,"code":"INTERNAL_ERROR","message":"Could not apply batch",
                "details":"Traceback (most recent call last):\n  File \"x.py\", line 1\n"}"#,
        )
        .query_async(&mut conn)
        .await
        .expect("plant error");

    let body = body_text(get(&router, "/events?auth_project=proj&watch=main:aaa:3").await).await;

    assert!(body.contains(r#""status":"discarded""#), "{body}");
    assert!(body.contains(r#""code":"WRITE_DISCARDED""#), "{body}");
    assert!(
        body.contains(r#""queueid":3"#),
        "the client's queueid: {body}"
    );
    assert!(
        body.contains(r#""batch_queueid":9"#),
        "the batch's, for diagnosis: {body}"
    );
    // The backend's own report of what failed, nested so the proxy's
    // `code`/`message` and the backend's cannot collide -- and carrying
    // the traceback, which is what makes this diagnosable from a browser
    // rather than only from the server log.
    assert!(
        body.contains(r#""error":{"#) && body.contains(r#""details":"Traceback"#),
        "the backend's error, traceback included: {body}"
    );
    assert!(
        body.contains(r#""code":"INTERNAL_ERROR""#),
        "the backend's code, distinct from WRITE_DISCARDED: {body}"
    );

    for k in [&key, &error_key] {
        let _: () = redis::cmd("DEL")
            .arg(k)
            .query_async(&mut conn)
            .await
            .expect("cleanup");
    }
}

/// Several watches settle independently, each reported once, and the
/// unsettled one does not hold up the others.
#[tokio::test]
async fn watches_settle_independently() {
    let Some((router, mut conn, config)) = fixture("events_multi").await else {
        eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
        return;
    };
    let settled = config.queue_entry_key("proj", "main", "aaa");
    let pending = config.queue_entry_key("proj", "main", "ccc");
    let _: () = redis::cmd("SET")
        .arg(&settled)
        .arg("bbb,2")
        .query_async(&mut conn)
        .await
        .expect("plant");
    // A commit *is* recorded here, but only up to queueid 1 -- the
    // client's write 5 is still pending behind it. Deliberately not a
    // bare counter: that short-circuits before the queueid comparison,
    // so it would leave the comparison untested.
    let _: () = redis::cmd("SET")
        .arg(&pending)
        .arg("ddd,1")
        .query_async(&mut conn)
        .await
        .expect("plant");

    let body = body_text(
        get(
            &router,
            "/events?auth_project=proj&watch=main:aaa:2&watch=dev:ccc:5",
        )
        .await,
    )
    .await;

    assert!(body.contains(r#""latest_commit":"aaa""#), "{body}");
    assert!(
        !body.contains(r#""latest_commit":"ccc""#),
        "a watch whose queueid is still ahead of the key must not be reported: {body}"
    );
    assert!(
        !body.contains(r#""new_commit":"ddd""#),
        "the commit recorded so far does not cover queueid 5: {body}"
    );
    // The budget expired with `ccc` outstanding; the terminal frame is
    // still sent so the client closes instead of reconnecting.
    assert!(body.contains(r#""status":"done""#), "{body}");

    for key in [settled, pending] {
        let _: () = redis::cmd("DEL")
            .arg(&key)
            .query_async(&mut conn)
            .await
            .expect("cleanup");
    }
}

/// Two branches off the same commit are independent: each has its own
/// queue key and is told its own new commit.
///
/// The case the branch in the key exists for. The batch worker
/// partitions by `(latest_commit, branch)`, so these are two batches
/// committing separately -- and when they shared a key, whichever
/// committed last overwrote the other and a client waiting on one
/// branch was sent to the other branch's commit.
#[tokio::test]
async fn two_branches_off_one_commit_are_independent() {
    let Some((router, mut conn, config)) = fixture("events_branches").await else {
        eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
        return;
    };
    let on_main = config.queue_entry_key("proj", "main", "aaa");
    let on_dev = config.queue_entry_key("proj", "dev", "aaa");
    assert_ne!(on_main, on_dev, "one commit, two branches, two keys");
    for (key, value) in [(&on_main, "from-main,2"), (&on_dev, "from-dev,3")] {
        let _: () = redis::cmd("SET")
            .arg(key)
            .arg(value)
            .query_async(&mut conn)
            .await
            .expect("plant");
    }

    let body = body_text(
        get(
            &router,
            "/events?auth_project=proj&watch=main:aaa:2&watch=dev:aaa:3",
        )
        .await,
    )
    .await;

    assert_eq!(
        body.matches(r#""status":"ok""#).count(),
        2,
        "one event per branch: {body}"
    );
    // Each branch is sent to the commit *its* batch produced.
    assert!(
        body.contains(r#""branch":"main","latest_commit":"aaa","new_commit":"from-main""#),
        "{body}"
    );
    assert!(
        body.contains(r#""branch":"dev","latest_commit":"aaa","new_commit":"from-dev""#),
        "{body}"
    );

    for key in [on_main, on_dev] {
        let _: () = redis::cmd("DEL")
            .arg(&key)
            .query_async(&mut conn)
            .await
            .expect("cleanup");
    }
}

/// A malformed or oversized watch set is refused before any Redis work.
#[tokio::test]
async fn a_bad_watch_set_is_refused() {
    let Some((router, _conn, _config)) = fixture("events_bad").await else {
        eprintln!("UNFURL_TEST_REDIS_URL not set, skipping");
        return;
    };
    for watch in [
        "watch=".to_string(),                            // empty
        "watch=aaa:1".to_string(),                       // no branch
        "watch=main:aaa".to_string(),                    // no queueid
        "watch=main:aaa:x".to_string(),                  // non-numeric
        "watch=main:aaa:0".to_string(),                  // queueid 0 is "nothing in flight"
        "watch=main:aaa:1&watch=main:aaa:2".to_string(), // same branch+commit twice
    ] {
        let uri = format!("/events?auth_project=proj&{watch}");
        let response = get(&router, &uri).await;
        assert_eq!(
            response.status(),
            StatusCode::BAD_REQUEST,
            "watch={watch:?} should be refused"
        );
    }
}
