// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Git locks in the Redis at `UNFURL_TEST_REDIS_URL`, held as python's
//! server holds them; skipped without it.

use std::time::Duration;

use redis::AsyncCommands;
use unfurl_server::gitlock::GitLocks;

/// A connection to the Redis at `UNFURL_TEST_REDIS_URL`, and a key prefix
/// of this test's own; `None` without one.
async fn redis() -> Option<(redis::aio::MultiplexedConnection, String)> {
    let url = std::env::var("UNFURL_TEST_REDIS_URL")
        .ok()
        .filter(|u| !u.is_empty())?;
    let client = redis::Client::open(url.as_str()).expect("redis url");
    let conn = client
        .get_multiplexed_async_connection()
        .await
        .expect("connect");
    let started = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("time")
        .as_nanos();
    Some((
        conn,
        format!("test-gitlock-{}-{started}:", std::process::id()),
    ))
}

/// The key python's `_git_lock_key` names the lock on `dir` with.
fn key(prefix: &str, dir: &std::path::Path) -> String {
    let real = dir.canonicalize().expect("real path");
    format!("{prefix}_git_lock::{}", real.display())
}

#[tokio::test]
async fn a_git_lock_is_taken_once_its_holder_lets_go() {
    let Some((mut conn, prefix)) = redis().await else {
        eprintln!("skip: UNFURL_TEST_REDIS_URL not set");
        return;
    };
    let dir = tempfile::tempdir().expect("tmp");
    let key = key(&prefix, dir.path());
    let locks = GitLocks::new(conn.clone(), &prefix);
    // held as python holds it: its pid, with an expiry
    let _: () = redis::cmd("SET")
        .arg(&key)
        .arg(1)
        .arg("PX")
        .arg(10_000)
        .query_async(&mut conn)
        .await
        .expect("set");

    let lock = locks
        .lock(dir.path(), Duration::from_millis(300))
        .await
        .expect("lock");
    assert!(lock.is_none(), "held past the wait");
    let held: Option<i64> = conn.get(&key).await.expect("get");
    assert_eq!(held, Some(1), "the holder's lock is untouched");

    let _: () = conn.del(&key).await.expect("del");
    let lock = locks
        .lock(dir.path(), Duration::from_millis(300))
        .await
        .expect("lock")
        .expect("taken");
    // python's cache reads it back as an int
    let held: Option<String> = conn.get(&key).await.expect("get");
    let held = held.expect("held");
    assert!(held.parse::<u64>().is_ok(), "{held}");

    // it lapsed, and python took it: releasing leaves python's alone
    let _: () = conn.set(&key, 1).await.expect("set");
    lock.release().await;
    let held: Option<i64> = conn.get(&key).await.expect("get");
    assert_eq!(held, Some(1));
    let _: () = conn.del(&key).await.expect("del");
}

/// A held lock is renewed for as long as it's held, past its TTL; one
/// dropped without being released, as a cancelled request's is, is
/// released.
#[tokio::test]
async fn a_held_git_lock_outlasts_its_ttl() {
    let Some((mut conn, prefix)) = redis().await else {
        eprintln!("skip: UNFURL_TEST_REDIS_URL not set");
        return;
    };
    let dir = tempfile::tempdir().expect("tmp");
    let key = key(&prefix, dir.path());
    let locks = GitLocks::new(conn.clone(), &prefix).with_ttl(Duration::from_millis(300));

    let lock = locks
        .lock(dir.path(), Duration::ZERO)
        .await
        .expect("lock")
        .expect("taken");
    tokio::time::sleep(Duration::from_millis(1000)).await;
    let held: bool = conn.exists(&key).await.expect("exists");
    assert!(held, "lapsed while held");
    lock.release().await;
    let held: bool = conn.exists(&key).await.expect("exists");
    assert!(!held, "released");

    let locks = locks.with_ttl(Duration::from_secs(30));
    let lock = locks
        .lock(dir.path(), Duration::ZERO)
        .await
        .expect("lock")
        .expect("taken");
    drop(lock);
    tokio::time::sleep(Duration::from_millis(200)).await;
    let held: bool = conn.exists(&key).await.expect("exists");
    assert!(!held, "held after it was dropped");
}
