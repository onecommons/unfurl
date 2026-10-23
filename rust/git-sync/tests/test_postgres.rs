// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Full end-to-end Postgres test, gated on `UNFURL_TEST_PG_URL`.
//!
//! Per-test scope creates a unique schema (`unfurl_test_<uuid>`) and
//! drops it on teardown so concurrent test runs don't collide. The
//! focused CRUD tests live in `test_crud.rs` and are parameterised
//! across both backends.

#![cfg(feature = "postgres")]

mod common;

use common::{pg_fixture, PgScope};
use unfurl_git_sync::{Db, DbConfig, RecordQuery};

#[tokio::test]
async fn cloudmap_end_to_end_postgres() {
    let Some((sync, tmp, scope)) = pg_fixture().await else {
        eprintln!("skip: UNFURL_TEST_PG_URL not set");
        return;
    };

    let stats = sync
        .update_from_working_dir(unfurl_git_sync::ScanOptions::default())
        .await
        .expect("update");
    assert!(stats.records_upserted >= 5);

    let new_json = serde_json::json!({"name": "x"});
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://example.com/x.git",
        new_json,
        None,
        false,
    )
    .await
    .expect("upsert");
    sync.save_changes().await.expect("save");
    let oid = sync
        .commit_repository("test commit", Default::default())
        .await
        .expect("commit")
        .commit
        .expect("returned");

    let after = sync
        .find_records(&RecordQuery {
            path: Some("/repositories".into()),
            key: Some("git://example.com/x.git".into()),
            ..Default::default()
        })
        .await
        .expect("find");
    assert_eq!(after.len(), 1);
    assert_eq!(after[0].commit_id, Some(oid));

    drop(sync);
    drop(tmp);
    scope.teardown().await;
}

/// A pool opens at most its config's `max_connections`, 20 without one.
#[tokio::test]
async fn a_pool_is_the_size_its_config_says() {
    let Some(scope) = PgScope::setup().await else {
        eprintln!("skip: UNFURL_TEST_PG_URL not set");
        return;
    };
    let DbConfig::Postgres { url, .. } = scope.db_config() else {
        unreachable!("PgScope yields a postgres config")
    };
    for (asked, size) in [(Some(3), 3), (None, 20)] {
        let config = DbConfig::Postgres {
            url: url.clone(),
            max_connections: asked,
        };
        let Db::Postgres(pool) = Db::connect(&config).await.expect("connect") else {
            unreachable!("a postgres config connects to postgres")
        };
        assert_eq!(pool.options().get_max_connections(), size);
        pool.close().await;
    }
    scope.teardown().await;
}
