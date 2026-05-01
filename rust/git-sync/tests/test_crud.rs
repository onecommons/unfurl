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
use unfurl_git_sync::{CommitRef, Error, SyncedRepo};

// ---------------------------------------------------------------------------
// Test bodies
// ---------------------------------------------------------------------------

async fn run_create_update_delete_round_trip(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");

    // create_record fails on existing path.
    let dup = sync
        .create_record(
            Some("cloudmap.yaml"),
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
            Some("cloudmap.yaml"),
            "/repositories",
            "new",
            serde_json::json!({"name":"new"}),
            None,
        )
        .await
        .expect("create");
    assert!(id.id > 0);

    let r = sync
        .get_record("cloudmap.yaml", "/repositories", "new")
        .await
        .expect("get")
        .expect("found");
    assert_eq!(r.json["name"], "new");

    // update_record on a missing path returns NotFound.
    let missing = sync
        .update_record(
            Some("cloudmap.yaml"),
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

    sync.delete_record(Some("cloudmap.yaml"), "/repositories", "new", None)
        .await
        .expect("delete");
    assert!(sync
        .get_record("cloudmap.yaml", "/repositories", "new")
        .await
        .expect("get")
        .is_none());

    // delete_record on a missing path returns NotFound.
    let dne = sync
        .delete_record(Some("cloudmap.yaml"), "/repositories", "new", None)
        .await;
    assert!(matches!(dne, Err(Error::NotFound { .. })));
}

async fn run_save_changes_round_trips_to_disk(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");

    let path = "/repositories";

    // 1) Update one record.
    let updated_key = "git://unfurl.cloud/onecommons/std.git";
    let updated_id = sync
        .update_record(
            Some("cloudmap.yaml"),
            path,
            updated_key,
            serde_json::json!({"name": "renamed"}),
            None,
        )
        .await
        .expect("update");

    // 2) Delete an existing record.
    let deleted_key = "git://unfurl.cloud/feb20a/dashboard.git";
    sync.delete_record(Some("cloudmap.yaml"), path, deleted_key, None)
        .await
        .expect("delete");

    // 3) Add a brand-new record.
    let added_key = "git://example.com/added.git";
    sync.create_record(
        Some("cloudmap.yaml"),
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
        .get_record_by_id(updated_id.id)
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

async fn run_commit_conflict_is_detected(sync: &SyncedRepo, _tmp: &TempDir) {
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
        Some("cloudmap.yaml"),
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
            Some("cloudmap.yaml"),
            path,
            key,
            serde_json::json!({"name":"v3"}),
            Some(CommitRef::Commit(oid_a.clone())),
        )
        .await;
    assert!(
        matches!(res, Err(Error::Conflict { .. })),
        "expected Conflict, got {res:?}"
    );

    // Pending check with a stale version → Conflict. (Pending(v) tokens
    // are valid only when the row's `version` column matches; version=0
    // is below the smallest bumped version, so it's guaranteed stale.)
    let res = sync
        .update_record(
            Some("cloudmap.yaml"),
            path,
            key,
            serde_json::json!({"name":"v3"}),
            Some(CommitRef::Pending(0)),
        )
        .await;
    assert!(matches!(res, Err(Error::Conflict { .. })));

    // Correct token succeeds and clears commit_id back to NULL.
    let id = sync
        .update_record(
            Some("cloudmap.yaml"),
            path,
            key,
            serde_json::json!({"name":"v3"}),
            Some(CommitRef::Commit(oid_b.clone())),
        )
        .await
        .expect("update with correct oid");
    let r = sync
        .get_record_by_id(id.id)
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
        .get_record_by_id(id.id)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(r.commit_id.as_deref(), Some(oid_c.as_str()));
}

async fn run_conflict_rolls_back_alias_writes(sync: &SyncedRepo, _tmp: &TempDir) {
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
            Some("cloudmap.yaml"),
            target_path,
            target_key,
            serde_json::json!({"name": "should-not-stick"}),
            Some(CommitRef::Commit(bogus)),
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

async fn run_create_resurrects_tombstone(sync: &SyncedRepo, tmp: &TempDir) {
    // delete_record only marks `deleted = TRUE` (a tombstone). A
    // subsequent create_record at the same (path, key) must succeed —
    // resurrecting the row — rather than seeing the tombstone as an
    // existing record and returning AlreadyExists.
    sync.update_from_working_dir().await.expect("update");

    let path = "/repositories";
    let key = "git://unfurl.cloud/onecommons/std.git";

    // Tombstone the existing record.
    sync.delete_record(Some("cloudmap.yaml"), path, key, None)
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
            Some("cloudmap.yaml"),
            path,
            key,
            serde_json::json!({"name": "resurrected"}),
            None,
        )
        .await
        .expect("create resurrects tombstone");
    assert!(id.id > 0);

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
        .get_record_by_id(id.id)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(after.commit_id.as_deref(), Some(oid.as_str()));
    assert!(!after.deleted);
}

async fn run_create_with_pending_token_on_committed_file_is_conflict(
    sync: &SyncedRepo,
    _tmp: &TempDir,
) {
    // create_record's expected_commit checks the file's commit when the
    // record row is absent. A `Pending(v)` token requires the row to
    // exist *and* its version to match — neither holds here, so any
    // value yields Conflict.
    sync.update_from_working_dir().await.expect("update");
    let res = sync
        .create_record(
            Some("cloudmap.yaml"),
            "/repositories",
            "brand-new",
            serde_json::json!({"name":"x"}),
            Some(CommitRef::Pending(0)),
        )
        .await;
    assert!(
        matches!(res, Err(Error::Conflict { .. })),
        "expected Conflict, got {res:?}"
    );
}

async fn run_find_records_alias_lookup(sync: &SyncedRepo, _tmp: &TempDir) {
    // The fixture's pkg:oci/odoo OCI artifact has a `versions` map
    // (`@sha256:…`, `?tag=latest`); CloudMapFormat::find_alias turns
    // each into an alias row at (record.path, joined_url). Looking up
    // those alias keys with `alias=true` should resolve to the parent
    // OCI artifact record.
    sync.update_from_working_dir().await.expect("update");

    let parent_path = "/artifacts";
    let parent_key = "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo";

    // Sanity: the parent record exists.
    let direct = sync
        .find_records(
            None,
            Some(parent_path.into()),
            Some(parent_key.into()),
            false,
            None,
        )
        .await
        .expect("find_records direct");
    assert_eq!(direct.len(), 1, "parent OCI record should be found");
    let parent_id = direct[0].id;

    // The version `?tag=latest` joins onto the parent URL by merging
    // the query string → alias key
    // `pkg:oci/odoo?repository_url=…&tag=latest`. Without `alias=true`
    // a search for the alias key returns nothing.
    let alias_key = "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest";
    let no_alias = sync
        .find_records(
            None,
            Some(parent_path.into()),
            Some(alias_key.into()),
            false,
            None,
        )
        .await
        .expect("find_records without alias");
    assert!(
        no_alias.is_empty(),
        "without alias=true, an alias key should not match"
    );

    // With `alias=true`, the same lookup resolves to the parent record.
    let via_alias = sync
        .find_records(
            None,
            Some(parent_path.into()),
            Some(alias_key.into()),
            true,
            None,
        )
        .await
        .expect("find_records via alias");
    assert_eq!(via_alias.len(), 1, "alias lookup should hit the parent");
    assert_eq!(
        via_alias[0].id, parent_id,
        "alias should point at the OCI artifact record"
    );
    assert_eq!(via_alias[0].key, parent_key);

    // alias=true is a no-op when key is None — should give the same
    // result as alias=false.
    let any_artifact = sync
        .find_records(None, Some(parent_path.into()), None, true, None)
        .await
        .expect("find_records no key, alias=true");
    let any_artifact_no_alias = sync
        .find_records(None, Some(parent_path.into()), None, false, None)
        .await
        .expect("find_records no key, alias=false");
    assert_eq!(
        any_artifact.len(),
        any_artifact_no_alias.len(),
        "alias is a no-op when key is None"
    );
}

async fn run_find_records_follow_walk(sync: &SyncedRepo, _tmp: &TempDir) {
    // find_records_follow walks DataFormat::follow edges from each
    // initial match, breadth-first, returning at most `follow` newly
    // visited records.
    //
    // Start: /repositories # git://unfurl.cloud/onecommons/blueprints/odoo.git
    //
    // Breadth-first expectation:
    //
    // 1. The repository's `notable["ensemble-template.yaml#…"]
    //    .artifact` URL adds:
    //        /artifacts # git://…blueprints/odoo.git#:ensemble-template.yaml
    //    (The strip-and-`.git`-normalise step also emits the bare
    //    `git://…blueprints/odoo.git`, but that's the start record —
    //    deduped.)
    //
    // 2. That artifact's `references` block has two URLs.
    //    a. `git://…/unfurl-types#v0.7.7:.` — strip + normalise to
    //       `git://…/unfurl-types.git`, which matches the repository:
    //           /repositories # git://unfurl.cloud/onecommons/unfurl-types.git
    //    b. `pkg:oci/odoo?…&tag=latest` — alias-resolves to:
    //           /artifacts # pkg:oci/odoo?repository_url=docker.io/bitnami/odoo
    //
    // 3. The unfurl-types repository's `notable["dummy-ensemble.yaml"]
    //    .artifact` URL reaches:
    //        /artifacts # git://…/unfurl-types.git#:dummy-ensemble.yaml
    //
    // 4. The OCI image and the dummy-ensemble TypeLibrary have no
    //    follow-shaped fields. BFS ends.
    sync.update_from_working_dir().await.expect("update");

    let start_path = "/repositories";
    let start_key = "git://unfurl.cloud/onecommons/blueprints/odoo.git";

    let expected_walk: Vec<(&str, &str)> = vec![
        (
            "/artifacts",
            "git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml",
        ),
        (
            "/repositories",
            "git://unfurl.cloud/onecommons/unfurl-types.git",
        ),
        (
            "/artifacts",
            "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo",
        ),
        (
            "/artifacts",
            "git://unfurl.cloud/onecommons/unfurl-types.git#:dummy-ensemble.yaml",
        ),
    ];

    // follow=0 → the followed Vec is empty, regardless of edges.
    let (init, follow0) = sync
        .find_records_follow(
            None,
            Some(start_path.into()),
            Some(start_key.into()),
            false,
            0,
            None,
        )
        .await
        .expect("follow 0");
    assert_eq!(init.len(), 1);
    assert_eq!(init[0].path, start_path);
    assert_eq!(init[0].key, start_key);
    assert!(follow0.is_empty(), "follow=0 returns no walked records");

    // follow=10 → walk up to 10 reachable records (more than enough).
    let (init, walked) = sync
        .find_records_follow(
            None,
            Some(start_path.into()),
            Some(start_key.into()),
            false,
            10,
            None,
        )
        .await
        .expect("follow 10");
    assert_eq!(init.len(), 1);
    assert_eq!(init[0].path, start_path);
    assert_eq!(init[0].key, start_key);

    let walked_ids: Vec<(&str, &str)> = walked
        .iter()
        .map(|r| (r.path.as_str(), r.key.as_str()))
        .collect();
    assert_eq!(
        walked_ids, expected_walk,
        "follow walk should reach the four expected records",
    );

    // Spot-check a few payloads to confirm these are the expected
    // records (and not coincidental key matches).
    let ensemble_template = &walked[0];
    assert_eq!(
        ensemble_template
            .json
            .get("type")
            .and_then(|t| t.as_object())
            .and_then(|t| t.keys().next())
            .map(|s| s.as_str()),
        Some("cloudmap.artifacts.tosca.ServiceTemplate"),
    );
    let unfurl_types_repo = &walked[1];
    assert_eq!(
        unfurl_types_repo.json.get("name").and_then(|n| n.as_str()),
        Some("unfurl-types"),
    );
    let oci = &walked[2];
    assert_eq!(
        oci.json
            .get("type")
            .and_then(|t| t.as_object())
            .and_then(|t| t.keys().next())
            .map(|s| s.as_str()),
        Some("cloudmap.artifacts.oci.Image"),
    );
    assert!(
        oci.json.get("versions").is_some(),
        "the OCI artifact still has its versions block"
    );
    let dummy_ensemble = &walked[3];
    assert_eq!(
        dummy_ensemble
            .json
            .get("type")
            .and_then(|t| t.as_object())
            .and_then(|t| t.keys().next())
            .map(|s| s.as_str()),
        Some("cloudmap.artifacts.tosca.TypeLibrary"),
    );

    // follow=1 → BFS truncates after the first hop (ensemble-template).
    let (_, walked_small) = sync
        .find_records_follow(
            None,
            Some(start_path.into()),
            Some(start_key.into()),
            false,
            1,
            None,
        )
        .await
        .expect("follow 1");
    let walked_small_ids: Vec<(&str, &str)> = walked_small
        .iter()
        .map(|r| (r.path.as_str(), r.key.as_str()))
        .collect();
    assert_eq!(walked_small_ids, vec![expected_walk[0]]);
}

async fn run_pending_token_distinguishes_concurrent_updates(sync: &SyncedRepo, _tmp: &TempDir) {
    // Two writers race on the same in-flight record. They both read
    // `Pending(v)` for the same `v`, but only one's update succeeds —
    // the other's `version` no longer matches and gets a Conflict.
    sync.update_from_working_dir().await.expect("update");

    let path = "/repositories";
    let key = "git://unfurl.cloud/onecommons/std.git";

    // First update lifts the row to in-flight (commit_id = NULL) and
    // bumps `version` to v1.
    sync.update_record(
        Some("cloudmap.yaml"),
        path,
        key,
        serde_json::json!({"name": "v1"}),
        None,
    )
    .await
    .expect("update v1");
    let v1 = sync
        .get_record("cloudmap.yaml", path, key)
        .await
        .expect("get")
        .expect("present")
        .version;

    // Both writers observe `Pending(v1)`. Writer A wins.
    sync.update_record(
        Some("cloudmap.yaml"),
        path,
        key,
        serde_json::json!({"name": "v2-A"}),
        Some(CommitRef::Pending(v1)),
    )
    .await
    .expect("A: update with valid Pending(v1)");

    // Writer B still holds Pending(v1) — but the row's version is now
    // v2 (post-A). Conflict.
    let res = sync
        .update_record(
            Some("cloudmap.yaml"),
            path,
            key,
            serde_json::json!({"name": "v2-B"}),
            Some(CommitRef::Pending(v1)),
        )
        .await;
    assert!(
        matches!(res, Err(Error::Conflict { .. })),
        "expected Conflict for stale Pending(v1), got {res:?}"
    );

    // Writer B re-reads, picks up v2, retries — succeeds.
    let v2 = sync
        .get_record("cloudmap.yaml", path, key)
        .await
        .expect("get")
        .expect("present")
        .version;
    assert!(v2 > v1, "version should have advanced past v1");
    sync.update_record(
        Some("cloudmap.yaml"),
        path,
        key,
        serde_json::json!({"name": "v3-B"}),
        Some(CommitRef::Pending(v2)),
    )
    .await
    .expect("B: retry with valid Pending(v2)");
}

async fn run_pending_token_survives_commit_roll_forward(sync: &SyncedRepo, _tmp: &TempDir) {
    // A `Pending(v)` token doesn't depend on `commit_id` — once issued,
    // it stays valid as long as nobody else has rewritten the row,
    // even after `commit_repository` rolls forward.
    sync.update_from_working_dir().await.expect("update");

    let path = "/repositories";
    let key = "git://unfurl.cloud/onecommons/std.git";

    sync.update_record(
        Some("cloudmap.yaml"),
        path,
        key,
        serde_json::json!({"name": "edited"}),
        None,
    )
    .await
    .expect("update");
    let v = sync
        .get_record("cloudmap.yaml", path, key)
        .await
        .expect("get")
        .expect("present")
        .version;

    // Save + commit → record now has a non-null commit_id but version
    // is preserved.
    sync.save_changes().await.expect("save");
    let oid = sync
        .commit_repository("v")
        .await
        .expect("commit")
        .expect("returned");
    let after = sync
        .get_record("cloudmap.yaml", path, key)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(after.commit_id.as_deref(), Some(oid.as_str()));
    assert_eq!(after.version, v, "version preserved across commit");

    // Pending(v) still wins post-commit.
    sync.update_record(
        Some("cloudmap.yaml"),
        path,
        key,
        serde_json::json!({"name": "edited-again"}),
        Some(CommitRef::Pending(v)),
    )
    .await
    .expect("Pending(v) still valid after commit");
}

async fn run_list_changes_pending_only(sync: &SyncedRepo, _tmp: &TempDir) {
    // `list_changes(None)` returns only the in-flight (commit_id IS
    // NULL) records — exactly what `commit_repository` would write.
    sync.update_from_working_dir().await.expect("update");
    assert!(
        sync.list_changes(None).await.expect("list").is_empty(),
        "no pending changes after a fresh update_from_working_dir"
    );

    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://unfurl.cloud/onecommons/std.git",
        serde_json::json!({"name": "edited"}),
        None,
    )
    .await
    .expect("update");
    sync.delete_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://unfurl.cloud/feb20a/dashboard.git",
        None,
    )
    .await
    .expect("delete");

    let pending = sync.list_changes(None).await.expect("list");
    let kinds: Vec<(&str, &str, bool)> = pending
        .iter()
        .map(|r| (r.path.as_str(), r.key.as_str(), r.deleted))
        .collect();
    assert_eq!(
        kinds,
        vec![
            (
                "/repositories",
                "git://unfurl.cloud/onecommons/std.git",
                false,
            ),
            (
                "/repositories",
                "git://unfurl.cloud/feb20a/dashboard.git",
                true,
            ),
        ],
        "pending list should include the update and the tombstone, in version order"
    );
    assert!(
        pending.iter().all(|r| r.commit_id.is_none()),
        "list_changes(None) only yields commit_id IS NULL records"
    );

    // After commit, the listing is empty again (tombstones are purged,
    // updates roll forward).
    sync.save_changes().await.expect("save");
    sync.commit_repository("v")
        .await
        .expect("commit")
        .expect("returned");
    assert!(
        sync.list_changes(None).await.expect("list").is_empty(),
        "no pending changes after commit"
    );
}

async fn run_list_changes_since_version(sync: &SyncedRepo, _tmp: &TempDir) {
    // `list_changes(Some(v))` returns records (committed or not) whose
    // version is greater than `v`. Useful for "sync me forward."
    sync.update_from_working_dir().await.expect("update");

    let path = "/repositories";
    let key_a = "git://unfurl.cloud/onecommons/std.git";

    // Read the current head version: snapshot all records and take the
    // max — anything written after is what we want to enumerate.
    let after_initial = sync
        .list_changes(Some(0))
        .await
        .expect("list since 0")
        .iter()
        .map(|r| r.version)
        .max()
        .expect("at least one record after initial sync");

    // Two writes after the snapshot: one update + one delete.
    sync.update_record(
        Some("cloudmap.yaml"),
        path,
        key_a,
        serde_json::json!({"name": "edited"}),
        None,
    )
    .await
    .expect("update");
    sync.delete_record(
        Some("cloudmap.yaml"),
        path,
        "git://unfurl.cloud/feb20a/dashboard.git",
        None,
    )
    .await
    .expect("delete");

    let since = sync
        .list_changes(Some(after_initial))
        .await
        .expect("list since head");
    let keys: Vec<(&str, bool)> = since.iter().map(|r| (r.key.as_str(), r.deleted)).collect();
    assert_eq!(
        keys,
        vec![
            ("git://unfurl.cloud/onecommons/std.git", false),
            ("git://unfurl.cloud/feb20a/dashboard.git", true),
        ],
        "since={after_initial} should yield the two new writes in version order"
    );

    // Versions are monotonic and strictly greater than the cursor.
    assert!(since.iter().all(|r| r.version > after_initial));
    assert!(since[0].version < since[1].version);

    // A cursor at the very last version yields nothing further.
    let head = since.iter().map(|r| r.version).max().unwrap();
    assert!(sync
        .list_changes(Some(head))
        .await
        .expect("list since head")
        .is_empty());
}

async fn run_default_file_path_set_on_first_update(sync: &SyncedRepo, _tmp: &TempDir) {
    // The fresh fixture has only `cloudmap.yaml`. After the first
    // `update_from_working_dir` run, that should become the default
    // file path. A subsequent run must NOT clobber a manually-set
    // value.
    sync.update_from_working_dir().await.expect("update");
    let wt = sync.get_worktree().await.expect("get_worktree");
    assert_eq!(wt.default_file_path.as_deref(), Some("cloudmap.yaml"));

    // Manually pin a different value, then re-run.
    sync.set_default_file_path(Some("pinned.yaml"))
        .await
        .expect("manual override");
    sync.update_from_working_dir().await.expect("update again");
    let wt2 = sync.get_worktree().await.expect("get_worktree");
    assert_eq!(
        wt2.default_file_path.as_deref(),
        Some("pinned.yaml"),
        "operator override should not be clobbered by a re-sync"
    );
}

async fn run_crud_with_none_file_path_resolves_existing(sync: &SyncedRepo, _tmp: &TempDir) {
    // `update_record(None, ...)` should look up the existing record
    // by `(path, key)` and use *its* file_path.
    sync.update_from_working_dir().await.expect("update");

    let path = "/repositories";
    let key = "git://unfurl.cloud/onecommons/std.git";

    let id = sync
        .update_record(
            None,
            path,
            key,
            serde_json::json!({"name": "via-none"}),
            None,
        )
        .await
        .expect("update with file_path=None");

    let r = sync
        .get_record_by_id(id.id)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(r.file_path, "cloudmap.yaml");
    assert_eq!(r.json["name"], "via-none");
}

async fn run_crud_with_none_file_path_uses_default_for_new(sync: &SyncedRepo, _tmp: &TempDir) {
    // `upsert_record(None, ...)` for a *new* (path, key) falls back
    // to `worktree.default_file_path`.
    sync.update_from_working_dir().await.expect("update");

    let path = "/repositories";
    let key = "git://example.com/brand-new.git";

    let id = sync
        .upsert_record(
            None,
            path,
            key,
            serde_json::json!({"name": "brand-new"}),
            None,
        )
        .await
        .expect("upsert with file_path=None");

    let r = sync
        .get_record_by_id(id.id)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(r.file_path, "cloudmap.yaml");
}

async fn run_crud_none_file_path_no_default_returns_not_found(sync: &SyncedRepo, _tmp: &TempDir) {
    // Sync (which auto-sets default_file_path), then explicitly clear
    // it. `upsert_record(None, ...)` for a brand-new key now has no
    // file to fall back on → NotFound.
    sync.update_from_working_dir().await.expect("update");
    sync.set_default_file_path(None)
        .await
        .expect("clear default");

    let res = sync
        .upsert_record(
            None,
            "/repositories",
            "git://example.com/no-default.git",
            serde_json::json!({}),
            None,
        )
        .await;
    assert!(
        matches!(res, Err(Error::NotFound { .. })),
        "expected NotFound, got {res:?}"
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
crud_test!(find_records_alias_lookup, run_find_records_alias_lookup);
crud_test!(find_records_follow_walk, run_find_records_follow_walk);
crud_test!(
    pending_token_distinguishes_concurrent_updates,
    run_pending_token_distinguishes_concurrent_updates
);
crud_test!(
    pending_token_survives_commit_roll_forward,
    run_pending_token_survives_commit_roll_forward
);
crud_test!(list_changes_pending_only, run_list_changes_pending_only);
crud_test!(list_changes_since_version, run_list_changes_since_version);
crud_test!(
    default_file_path_set_on_first_update,
    run_default_file_path_set_on_first_update
);
crud_test!(
    crud_with_none_file_path_resolves_existing,
    run_crud_with_none_file_path_resolves_existing
);
crud_test!(
    crud_with_none_file_path_uses_default_for_new,
    run_crud_with_none_file_path_uses_default_for_new
);
crud_test!(
    crud_none_file_path_no_default_returns_not_found,
    run_crud_none_file_path_no_default_returns_not_found
);
