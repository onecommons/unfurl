// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Focused tests for the CRUD APIs and the optimistic-concurrency check.
//!
//! Each scenario is implemented once as `run_<name>` and then exercised
//! against both backends:
//! - an in-memory SQLite fixture (always),
//! - a Postgres fixture when `UNFURL_TEST_PG_URL` is set and the
//!   `postgres` cargo feature is enabled.

mod common;

#[cfg(feature = "postgres")]
use common::pg_fixture;
use common::sqlite_fixture;
use tempfile::TempDir;
use unfurl_git_sync::{CommitRef, Error, GitSync};

// ---------------------------------------------------------------------------
// Test bodies
// ---------------------------------------------------------------------------

async fn run_create_update_delete_round_trip(sync: &GitSync, _tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");

    // create_record fails on existing path.
    let dup = sync
        .create_record(
            "cloudmap.yaml",
            "/repositories",
            "git://unfurl.cloud/onecommons/std.git",
            serde_json::json!({}),
            None,
        )
        .await;
    assert!(
        matches!(dup, Err(Error::AlreadyExists { .. })),
        "expected AlreadyExists, got {dup:?}"
    );

    // create on a fresh path.
    let id = sync
        .create_record(
            "cloudmap.yaml",
            "/repositories",
            "new",
            serde_json::json!({"name":"new"}),
            None,
        )
        .await
        .expect("create");
    assert!(id > 0);

    let r = sync
        .get_record("cloudmap.yaml", "/repositories", "new")
        .await
        .expect("get")
        .expect("found");
    assert_eq!(r.json["name"], "new");

    // update_record on a missing path returns NotFound.
    let missing = sync
        .update_record(
            "cloudmap.yaml",
            "/repositories",
            "missing",
            serde_json::json!({}),
            None,
        )
        .await;
    assert!(
        matches!(missing, Err(Error::NotFound { .. })),
        "expected NotFound, got {missing:?}"
    );

    sync.delete_record("cloudmap.yaml", "/repositories", "new", None)
        .await
        .expect("delete");
    assert!(sync
        .get_record("cloudmap.yaml", "/repositories", "new")
        .await
        .expect("get")
        .is_none());

    // delete_record on a missing path returns NotFound.
    let dne = sync
        .delete_record("cloudmap.yaml", "/repositories", "new", None)
        .await;
    assert!(matches!(dne, Err(Error::NotFound { .. })));
}

async fn run_save_changes_round_trips_to_disk(sync: &GitSync, tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");

    let path = "/repositories";

    // 1) Update one record.
    let updated_key = "git://unfurl.cloud/onecommons/std.git";
    let updated_id = sync
        .update_record(
            "cloudmap.yaml",
            path,
            updated_key,
            serde_json::json!({"name": "renamed"}),
            None,
        )
        .await
        .expect("update");

    // 2) Delete an existing record.
    let deleted_key = "git://unfurl.cloud/feb20a/dashboard.git";
    sync.delete_record("cloudmap.yaml", path, deleted_key, None)
        .await
        .expect("delete");

    // 3) Add a brand-new record.
    let added_key = "git://example.com/added.git";
    sync.create_record(
        "cloudmap.yaml",
        path,
        added_key,
        serde_json::json!({
            "name": "added",
            "path": "example/added",
        }),
        None,
    )
    .await
    .expect("create");

    // save_changes rewrites the YAML file on disk.
    let written = sync.save_changes().await.expect("save_changes");
    assert_eq!(written.len(), 1);

    // Compare the on-disk YAML against the fixture.
    let actual = std::fs::read_to_string(tmp.path().join("cloudmap.yaml")).expect("read");
    let expected_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/expected_cloudmap_after_save.yaml");
    if std::env::var("UPDATE_FIXTURES").is_ok() {
        std::fs::write(&expected_path, &actual).expect("write fixture");
    }
    let expected = std::fs::read_to_string(&expected_path).expect("read fixture");
    assert_eq!(
        actual,
        expected,
        "on-disk YAML did not match fixture {}",
        expected_path.display()
    );

    // commit_repository → records, file row, and worktree row should
    // all advance to the new HEAD oid.
    let oid = sync
        .commit_repository("save_changes round-trip")
        .await
        .expect("commit")
        .expect("commit returned");

    // Sanity: HEAD really does point at the new oid in the gix repo.
    let wd = sync.get_working_dir().await.expect("get_working_dir");
    assert_eq!(
        wd.head_commit.as_deref(),
        Some(oid.as_str()),
        "HEAD oid does not match commit_repository return"
    );

    let updated = sync
        .get_record_by_id(updated_id)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(
        updated.commit_id.as_deref(),
        Some(oid.as_str()),
        "modified record commit_id"
    );
    let added = sync
        .get_record("cloudmap.yaml", path, added_key)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(
        added.commit_id.as_deref(),
        Some(oid.as_str()),
        "new record commit_id"
    );

    let file = sync
        .get_file("cloudmap.yaml")
        .await
        .expect("get_file")
        .expect("present");
    assert_eq!(
        file.commit_id.as_deref(),
        Some(oid.as_str()),
        "file commit_id"
    );

    let worktree = sync.get_worktree().await.expect("get_worktree");
    assert_eq!(
        worktree.commit_id.as_deref(),
        Some(oid.as_str()),
        "worktree commit_id"
    );
}

async fn run_commit_conflict_is_detected(sync: &GitSync, _tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");
    let oid_a = sync
        .get_working_dir()
        .await
        .expect("get_working_dir")
        .head_commit
        .expect("repo has a commit");

    let path = "/repositories";
    let key = "git://unfurl.cloud/onecommons/std.git";

    // Edit + commit so the record's commit_id becomes oid_b ≠ oid_a.
    sync.update_record(
        "cloudmap.yaml",
        path,
        key,
        serde_json::json!({"name":"v2"}),
        None,
    )
    .await
    .expect("update v2");
    let oid_b = sync
        .commit_repository("v2")
        .await
        .expect("commit v2")
        .expect("commit returned");
    assert_ne!(oid_a, oid_b);

    // A caller still holding oid_a tries to update → Conflict.
    let res = sync
        .update_record(
            "cloudmap.yaml",
            path,
            key,
            serde_json::json!({"name":"v3"}),
            Some(CommitRef::Oid(oid_a.clone())),
        )
        .await;
    assert!(
        matches!(res, Err(Error::Conflict { .. })),
        "expected Conflict, got {res:?}"
    );

    // Pending check: caller asserts uncommitted, but it IS committed.
    let res = sync
        .update_record(
            "cloudmap.yaml",
            path,
            key,
            serde_json::json!({"name":"v3"}),
            Some(CommitRef::Pending),
        )
        .await;
    assert!(matches!(res, Err(Error::Conflict { .. })));

    // Correct token succeeds and clears commit_id back to NULL.
    let id = sync
        .update_record(
            "cloudmap.yaml",
            path,
            key,
            serde_json::json!({"name":"v3"}),
            Some(CommitRef::Oid(oid_b.clone())),
        )
        .await
        .expect("update with correct oid");
    let r = sync
        .get_record_by_id(id)
        .await
        .expect("get")
        .expect("present");
    assert!(r.commit_id.is_none(), "commit_id was {:?}", r.commit_id);

    // After save_changes + commit_repository the conflict token rolls forward.
    sync.save_changes().await.expect("save");
    let oid_c = sync
        .commit_repository("v3")
        .await
        .expect("commit v3")
        .expect("returned");
    let r = sync
        .get_record_by_id(id)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(r.commit_id.as_deref(), Some(oid_c.as_str()));
}

async fn run_conflict_rolls_back_alias_writes(sync: &GitSync, _tmp: &TempDir) {
    // Regression: the conflict check + mutation + alias refresh must
    // happen in a single transaction. Before the fix, a Conflict
    // returned partway through left stale alias rows in the DB.
    sync.update_from_working_dir().await.expect("update");

    let target_path = "/repositories";
    let target_key = "git://unfurl.cloud/onecommons/std.git";
    let before = sync
        .get_record("cloudmap.yaml", target_path, target_key)
        .await
        .expect("get")
        .expect("present");

    // Try to update with a bogus expected_commit → Conflict. The
    // all-zeros sha1 won't match any real commit.
    let bogus = "0000000000000000000000000000000000000000".to_string();
    let res = sync
        .update_record(
            "cloudmap.yaml",
            target_path,
            target_key,
            serde_json::json!({"name": "should-not-stick"}),
            Some(CommitRef::Oid(bogus)),
        )
        .await;
    assert!(
        matches!(res, Err(Error::Conflict { .. })),
        "expected Conflict, got {res:?}"
    );

    // The record's JSON must be unchanged (write was rolled back).
    let after = sync
        .get_record("cloudmap.yaml", target_path, target_key)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(
        before.json, after.json,
        "record JSON was mutated despite Conflict"
    );
    assert_eq!(
        before.commit_id, after.commit_id,
        "commit_id was changed despite Conflict"
    );
}

async fn run_create_resurrects_tombstone(sync: &GitSync, tmp: &TempDir) {
    // delete_record only marks `deleted = TRUE` (a tombstone). A
    // subsequent create_record at the same (path, key) must succeed —
    // resurrecting the row — rather than seeing the tombstone as an
    // existing record and returning AlreadyExists.
    sync.update_from_working_dir().await.expect("update");

    let path = "/repositories";
    let key = "git://unfurl.cloud/onecommons/std.git";

    // Tombstone the existing record.
    sync.delete_record("cloudmap.yaml", path, key, None)
        .await
        .expect("delete");
    assert!(
        sync.get_record("cloudmap.yaml", path, key)
            .await
            .expect("get")
            .is_none(),
        "tombstone should be hidden from get_record"
    );

    // Re-create the same key with brand-new content. Should succeed,
    // NOT return AlreadyExists.
    let id = sync
        .create_record(
            "cloudmap.yaml",
            path,
            key,
            serde_json::json!({"name": "resurrected"}),
            None,
        )
        .await
        .expect("create resurrects tombstone");
    assert!(id > 0);

    // The resurrected row must be visible and live (deleted = false).
    let r = sync
        .get_record("cloudmap.yaml", path, key)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(r.json["name"], "resurrected");
    assert!(!r.deleted, "resurrected row must not be a tombstone");
    assert!(r.commit_id.is_none(), "resurrected row should be pending");

    // Persist + commit; on-disk file should have the new value, not a
    // missing key (which is what we'd see if the tombstone hadn't been
    // cleared).
    let written = sync.save_changes().await.expect("save_changes");
    assert_eq!(written.len(), 1);
    let oid = sync
        .commit_repository("resurrect")
        .await
        .expect("commit")
        .expect("returned");

    let on_disk = std::fs::read_to_string(tmp.path().join("cloudmap.yaml")).expect("read");
    let parsed: serde_json::Value = serde_saphyr::from_str(&on_disk).expect("yaml");
    let value = parsed
        .get("repositories")
        .and_then(|v| v.get(key))
        .expect("resurrected key present on disk");
    assert_eq!(
        value.get("name").and_then(|v| v.as_str()),
        Some("resurrected")
    );

    // After commit, the resurrected record carries the new oid and
    // there are no leftover tombstones for this worktree.
    let after = sync
        .get_record_by_id(id)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(after.commit_id.as_deref(), Some(oid.as_str()));
    assert!(!after.deleted);
}

async fn run_create_with_pending_token_on_committed_file_is_conflict(
    sync: &GitSync,
    _tmp: &TempDir,
) {
    // create_record's expected_commit checks the file's commit when the
    // record row is absent. Pending on a committed file → Conflict.
    sync.update_from_working_dir().await.expect("update");
    let res = sync
        .create_record(
            "cloudmap.yaml",
            "/repositories",
            "brand-new",
            serde_json::json!({"name":"x"}),
            Some(CommitRef::Pending),
        )
        .await;
    assert!(
        matches!(res, Err(Error::Conflict { .. })),
        "expected Conflict, got {res:?}"
    );
}

// ---------------------------------------------------------------------------
// Backend wrappers
// ---------------------------------------------------------------------------
//
// For each `run_<name>` body we generate one SQLite test (always) and
// one Postgres test (skipped at runtime when `UNFURL_TEST_PG_URL` is
// unset, and compiled away entirely without the `postgres` feature).

macro_rules! crud_test {
    ($name:ident, $body:ident) => {
        mod $name {
            use super::*;

            #[tokio::test]
            async fn sqlite() {
                let (sync, tmp) = sqlite_fixture().await;
                $body(&sync, &tmp).await;
            }

            #[cfg(feature = "postgres")]
            #[tokio::test]
            async fn postgres() {
                let Some((sync, tmp, scope)) = pg_fixture().await else {
                    eprintln!("skip: UNFURL_TEST_PG_URL not set");
                    return;
                };
                $body(&sync, &tmp).await;
                drop(sync);
                drop(tmp);
                scope.teardown().await;
            }
        }
    };
}

crud_test!(
    create_update_delete_round_trip,
    run_create_update_delete_round_trip
);
crud_test!(
    save_changes_round_trips_to_disk,
    run_save_changes_round_trips_to_disk
);
crud_test!(commit_conflict_is_detected, run_commit_conflict_is_detected);
crud_test!(
    conflict_rolls_back_alias_writes,
    run_conflict_rolls_back_alias_writes
);
crud_test!(create_resurrects_tombstone, run_create_resurrects_tombstone);
crud_test!(
    create_with_pending_token_on_committed_file_is_conflict,
    run_create_with_pending_token_on_committed_file_is_conflict
);
