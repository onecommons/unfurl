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

use common::pg_fixture;

#[tokio::test]
async fn cloudmap_end_to_end_postgres() {
    let Some((sync, tmp, scope)) = pg_fixture().await else {
        eprintln!("skip: UNFURL_TEST_PG_URL not set");
        return;
    };

    let stats = sync.update_from_working_dir().await.expect("update");
    assert!(stats.records_upserted >= 5);

    let new_json = serde_json::json!({"name": "x"});
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://example.com/x.git",
        new_json,
        None,
    )
    .await
    .expect("upsert");
    sync.save_changes().await.expect("save");
    let oid = sync
        .commit_repository("test commit")
        .await
        .expect("commit")
        .expect("returned");

    let after = sync
        .find_records(
            None,
            Some("/repositories".into()),
            Some("git://example.com/x.git".into()),
            false,
            None,
            None,
            None,
            None,
            None,
        )
        .await
        .expect("find");
    assert_eq!(after.len(), 1);
    assert_eq!(after[0].commit_id.as_deref(), Some(oid.as_str()));

    drop(sync);
    drop(tmp);
    scope.teardown().await;
}
