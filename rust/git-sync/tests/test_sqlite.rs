// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! End-to-end happy-path tests on an in-memory SQLite database.

mod common;

use common::sqlite_fixture;

#[tokio::test]
async fn cloudmap_end_to_end_sqlite() {
    let (sync, tmp) = sqlite_fixture().await;

    let stats = sync.update_from_working_dir().await.expect("update");
    assert!(stats.records_upserted >= 5, "stats: {stats:?}");

    let repos = sync
        .find_records(None, None, None, false, None, None, None, None, None)
        .await
        .expect("find_records");
    assert!(
        repos.iter().any(|r| r.path == "/repositories"),
        "expected /repositories records, got: {:?}",
        repos.iter().map(|r| (&r.path, &r.key)).collect::<Vec<_>>()
    );

    // Edit through upsert_record + persist to disk + commit.
    let new_json = serde_json::json!({"name": "x", "path": "x"});
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://example.com/x.git",
        new_json,
        None,
    )
    .await
    .expect("upsert");

    let written = sync.save_changes().await.expect("save_changes");
    assert_eq!(written, vec![tmp.path().join("cloudmap.yaml")]);

    let on_disk_text = std::fs::read_to_string(tmp.path().join("cloudmap.yaml")).expect("read");
    let on_disk: serde_json::Value = serde_saphyr::from_str(&on_disk_text).expect("yaml");
    let new_repo = on_disk
        .get("repositories")
        .and_then(|v| v.get("git://example.com/x.git"))
        .expect("new repository url present");
    assert_eq!(new_repo.get("name").and_then(|v| v.as_str()), Some("x"));

    let oid = sync
        .commit_repository("test commit")
        .await
        .expect("commit_repository")
        .expect("commit produced");

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
        .expect("find_records after commit");
    assert_eq!(after.len(), 1);
    assert_eq!(after[0].commit_id.as_deref(), Some(oid.as_str()));
}
