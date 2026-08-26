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
#[cfg(feature = "postgres")]
use unfurl_git_sync::DbConfig;
use unfurl_git_sync::{BatchOp, CommitRef, Error, JsonQuery, RecordQuery, SyncedRepo, TxnMeta};

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
    let written = sync.save_changes().await.expect("save_changes").written;
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

/// Top-level keys of a JSON object, in order.
fn field_order(value: &serde_json::Value) -> Vec<&str> {
    value
        .as_object()
        .expect("object")
        .keys()
        .map(String::as_str)
        .collect()
}

/// The field order *inside* a record is authored content, like the
/// order of the records themselves: a client that rewrites one field
/// must not reshuffle the rest of the block, or every edit lands in git
/// as a whole-record diff.
///
/// The file is where that order lives, not the database — a record read
/// back out carries the writing client's order on SQLite and JSONB's
/// normalised order (by key length, then bytewise) on Postgres. So the
/// order is asserted on disk, after the write, which is what git sees;
/// `get_record` is only checked for content. Running under `crud_test!`
/// is the point: the two backends disagree about what comes out of the
/// database, and must still agree about what lands in the file.
async fn run_record_field_order_survives_the_db(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");

    let path = "/repositories";
    let key = "git://unfurl.cloud/onecommons/blueprints/odoo.git";
    // As authored in the fixture. Postgres's JSONB ordering would give
    // name, path, tags, branches, contains, metadata, protocols,
    // project_url, default_branch — nothing like it, so this record
    // discriminates the two backends.
    let authored = [
        "path",
        "name",
        "protocols",
        "project_url",
        "metadata",
        "default_branch",
        "branches",
        "tags",
        "contains",
    ];

    // 1) The record round-trips through the database intact. Its key
    //    order at this point is the backend's, not the file's --
    //    `serde_json::Value` compares objects by content, not order.
    let mut rec = sync
        .get_record("cloudmap.yaml", path, key)
        .await
        .expect("get")
        .expect("present");
    let on_disk: serde_json::Value =
        serde_saphyr::from_str(&std::fs::read_to_string(tmp.path().join("cloudmap.yaml")).unwrap())
            .expect("parse fixture");
    assert_eq!(rec.json, on_disk["repositories"][key], "get_record content");

    // 2) The read-modify-write an editing client actually performs:
    //    change one field, put the whole record back, save.
    rec.json["name"] = serde_json::json!("Odoo ERP");
    sync.update_record(Some("cloudmap.yaml"), path, key, rec.json.clone(), None)
        .await
        .expect("update");
    let written = sync.save_changes().await.expect("save_changes").written;
    assert_eq!(written.len(), 1);

    let text = std::fs::read_to_string(tmp.path().join("cloudmap.yaml")).expect("read");
    let saved: serde_json::Value = serde_saphyr::from_str(&text).expect("parse saved yaml");
    let saved_rec = &saved["repositories"][key];
    assert_eq!(saved_rec["name"], "Odoo ERP", "the edit was applied");
    assert_eq!(
        field_order(saved_rec),
        authored,
        "on-disk after save_changes"
    );
}

/// A record the file doesn't already have has no order to preserve, so
/// it gets the format's canonical one — the same order Python's
/// `CloudMapDB.save()` writes, both being derived from the schema.
/// Without it a new record would land in whatever order the database
/// returned, which differs per backend and matches neither tool.
async fn run_new_record_gets_the_canonical_field_order(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");

    // Sent in an order that is neither the schema's nor either
    // backend's, so this can't pass by accident: JSONB would sort these
    // to name, path, branches, protocols, default_branch.
    sync.create_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://example.com/new.git",
        serde_json::json!({
            "branches": {"main": "abc123"},
            "default_branch": "main",
            "not_in_the_schema": true,
            "protocols": ["https"],
            "name": "new",
            "path": "example/new",
        }),
        None,
    )
    .await
    .expect("create");
    sync.save_changes().await.expect("save_changes");

    let text = std::fs::read_to_string(tmp.path().join("cloudmap.yaml")).expect("read");
    let saved: serde_json::Value = serde_saphyr::from_str(&text).expect("parse saved yaml");
    assert_eq!(
        field_order(&saved["repositories"]["git://example.com/new.git"]),
        [
            "path",
            "name",
            "protocols",
            "default_branch",
            "branches",
            // Fields the schema doesn't declare keep their arrival
            // order, after the ones it does.
            "not_in_the_schema",
        ],
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
    let written = sync.save_changes().await.expect("save_changes").written;
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

async fn run_find_records_type_filter(sync: &SyncedRepo, _tmp: &TempDir) {
    // `type_names` matches records whose `type` typeRef object
    // declares one of the given names as a key. Exact names only —
    // subtype expansion is the caller's job.
    sync.update_from_working_dir().await.expect("update");

    let pipelines = sync
        .find_records(&RecordQuery {
            type_names: Some(vec!["cloudmap.artifacts.ci.GitLabPipeline".into()]),
            ..Default::default()
        })
        .await
        .expect("type filter");
    assert_eq!(
        pipelines.len(),
        4,
        "fixture has four .gitlab-ci.yml artifacts: {:?}",
        pipelines.iter().map(|r| &r.key).collect::<Vec<_>>()
    );
    assert!(pipelines
        .iter()
        .all(|r| r.path == "/artifacts" && r.key.ends_with(".gitlab-ci.yml")));

    // Multiple names OR together and compose with the `path` filter.
    let multi = sync
        .find_records(&RecordQuery {
            path: Some("/services".into()),
            type_names: Some(vec![
                "Odoo@unfurl.cloud/onecommons/blueprints/odoo".into(),
                "no.such.Type".into(),
            ]),
            ..Default::default()
        })
        .await
        .expect("multi type filter");
    assert_eq!(multi.len(), 1);
    assert_eq!(multi[0].key, "https://example.com/oodo");

    // Unknown name matches nothing.
    let none = sync
        .find_records(&RecordQuery {
            type_names: Some(vec!["no.such.Type".into()]),
            ..Default::default()
        })
        .await
        .expect("unknown type");
    assert!(none.is_empty());

    // An empty name list is treated as "no filter", not "match none".
    let all = sync
        .find_records(&RecordQuery {
            type_names: Some(Vec::new()),
            ..Default::default()
        })
        .await
        .expect("empty type list");
    let unfiltered = sync
        .find_records(&RecordQuery::default())
        .await
        .expect("unfiltered");
    assert_eq!(all.len(), unfiltered.len());

    // The section-stat probe moves when (and only when) the section
    // changes: an upsert into /types bumps its pair, and leaves other
    // sections' pairs alone.
    let types_before = sync.section_stat("/types").await.expect("stat");
    let artifacts_before = sync.section_stat("/artifacts").await.expect("stat");
    assert!(types_before.0 > 0, "fixture has type records");
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/types",
        "test.NewType",
        serde_json::json!({"kind": "Component"}),
        None,
    )
    .await
    .expect("upsert type record");
    let types_after = sync.section_stat("/types").await.expect("stat after");
    let artifacts_after = sync.section_stat("/artifacts").await.expect("stat after");
    assert_ne!(types_before, types_after, "types probe must move");
    assert_eq!(
        artifacts_before, artifacts_after,
        "other sections' probes must not move"
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
        .find_records(&RecordQuery {
            path: Some(parent_path.into()),
            key: Some(parent_key.into()),
            ..Default::default()
        })
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
        .find_records(&RecordQuery {
            path: Some(parent_path.into()),
            key: Some(alias_key.into()),
            ..Default::default()
        })
        .await
        .expect("find_records without alias");
    assert!(
        no_alias.is_empty(),
        "without alias=true, an alias key should not match"
    );

    // With `alias=true`, the same lookup resolves to the parent record.
    let via_alias = sync
        .find_records(&RecordQuery {
            path: Some(parent_path.into()),
            key: Some(alias_key.into()),
            alias: true,
            ..Default::default()
        })
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
        .find_records(&RecordQuery {
            path: Some(parent_path.into()),
            alias: true,
            ..Default::default()
        })
        .await
        .expect("find_records no key, alias=true");
    let any_artifact_no_alias = sync
        .find_records(&RecordQuery {
            path: Some(parent_path.into()),
            ..Default::default()
        })
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
    // 1. Each `Repository.contains[<file-path>]` key resolves (via
    //    `Repository.artifact_url()`) to an artifact URL. The odoo repo
    //    has three entries (`.gitlab-ci.yml`, `ensemble-template.yaml#…`,
    //    `unfurl.yaml`); the first two have matching artifact records.
    //
    // 2. The `ensemble-template.yaml%23spec/service_template` artifact's
    //    `references` block has two URLs:
    //    a. `git://…/unfurl-types#v0.7.7:.` — strip + normalise to
    //       `git://…/unfurl-types.git`, which matches the repository.
    //    b. `pkg:oci/odoo?…&tag=latest` — alias-resolves to the OCI
    //       artifact `pkg:oci/odoo?repository_url=docker.io/bitnami/odoo`.
    //
    // 3. The unfurl-types repository's `contains` keys resolve to:
    //    `/artifacts # git://…/unfurl-types.git#:.gitlab-ci.yml` and
    //    `/artifacts # git://…/unfurl-types.git#:dummy-ensemble.yaml`.
    //
    // 4. The OCI image and the dummy-ensemble TypeLibrary have no
    //    follow-shaped fields. BFS ends.
    sync.update_from_working_dir().await.expect("update");

    let start_path = "/repositories";
    let start_key = "git://unfurl.cloud/onecommons/blueprints/odoo.git";

    // BFS now batches per-level (one `key IN (...)` query per
    // frontier), so within each level the database returns rows in
    // its `ORDER BY path, key` order. That puts the 2nd-level
    // entries `/artifacts pkg:oci/odoo` before
    // `/repositories git://…unfurl-types.git` here.
    let expected_walk: Vec<(&str, &str)> = vec![
        (
            "/artifacts",
            "git://unfurl.cloud/onecommons/blueprints/odoo.git#:.gitlab-ci.yml",
        ),
        (
            "/artifacts",
            "git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml%23spec/service_template",
        ),
        (
            "/artifacts",
            "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo",
        ),
        (
            "/repositories",
            "git://unfurl.cloud/onecommons/unfurl-types.git",
        ),
        (
            "/artifacts",
            "git://unfurl.cloud/onecommons/unfurl-types.git#:.gitlab-ci.yml",
        ),
        (
            "/artifacts",
            "git://unfurl.cloud/onecommons/unfurl-types.git#:dummy-ensemble.yaml",
        ),
    ];

    // follow=0 → the followed Vec is empty, regardless of edges.
    let (init, follow0) = sync
        .find_records_follow(
            &RecordQuery {
                path: Some(start_path.into()),
                key: Some(start_key.into()),
                ..Default::default()
            },
            0,
            Vec::new(),
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
            &RecordQuery {
                path: Some(start_path.into()),
                key: Some(start_key.into()),
                ..Default::default()
            },
            10,
            Vec::new(),
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
        "follow walk should reach the expected records",
    );

    // Spot-check a few payloads to confirm these are the expected
    // records (and not coincidental key matches).
    let ensemble_template = &walked[1];
    assert_eq!(
        ensemble_template
            .json
            .get("type")
            .and_then(|t| t.as_object())
            .and_then(|t| t.keys().next())
            .map(|s| s.as_str()),
        Some("cloudmap.artifacts.tosca.ServiceTemplate"),
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
    let unfurl_types_repo = &walked[3];
    assert_eq!(
        unfurl_types_repo.json.get("name").and_then(|n| n.as_str()),
        Some("unfurl-types"),
    );
    assert!(
        oci.json.get("versions").is_some(),
        "the OCI artifact still has its versions block"
    );
    let dummy_ensemble = &walked[5];
    assert_eq!(
        dummy_ensemble
            .json
            .get("type")
            .and_then(|t| t.as_object())
            .and_then(|t| t.keys().next())
            .map(|s| s.as_str()),
        Some("cloudmap.artifacts.tosca.TypeLibrary"),
    );

    // follow=1 → BFS truncates after the first hop (`.gitlab-ci.yml`,
    // which is alphabetically the first `contains` entry of the start
    // repository).
    let (_, walked_small) = sync
        .find_records_follow(
            &RecordQuery {
                path: Some(start_path.into()),
                key: Some(start_key.into()),
                ..Default::default()
            },
            1,
            Vec::new(),
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

async fn run_apply_batch_atomic_success(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");

    let ops = vec![
        BatchOp::Upsert {
            file_path: Some("cloudmap.yaml".into()),
            path: "/repositories".into(),
            key: "batch-a".into(),
            json: serde_json::json!({"name": "a"}),
            expected: None,
        },
        BatchOp::Upsert {
            file_path: Some("cloudmap.yaml".into()),
            path: "/repositories".into(),
            key: "batch-b".into(),
            json: serde_json::json!({"name": "b"}),
            expected: None,
        },
    ];
    let outcome = sync
        .apply_batch(ops, true, None)
        .await
        .expect("apply_batch");
    assert!(outcome.failed.is_empty(), "failed: {:?}", outcome.failed);
    assert_eq!(outcome.applied.len(), 2);
    assert!(outcome.last_version.is_some());
    // Both records readable post-commit.
    for k in ["batch-a", "batch-b"] {
        let rec = sync
            .get_record("cloudmap.yaml", "/repositories", k)
            .await
            .expect("get")
            .expect("found");
        assert_eq!(rec.json["name"], k.split('-').nth(1).unwrap());
    }
}

async fn run_apply_batch_atomic_conflict_rolls_back(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");

    // First batch: stage record so we have a known version for the
    // OCC token. Capture the version.
    let first = sync
        .apply_batch(
            vec![BatchOp::Upsert {
                file_path: Some("cloudmap.yaml".into()),
                path: "/repositories".into(),
                key: "tracked".into(),
                json: serde_json::json!({"name": "v1"}),
                expected: None,
            }],
            true,
            None,
        )
        .await
        .expect("first");
    let v1 = first.applied[0].outcome.version;

    // Now mutate "tracked" out-of-band so the next OCC token (=v1) is
    // stale.
    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "tracked",
        serde_json::json!({"name": "v2"}),
        None,
    )
    .await
    .expect("oob update");

    // Submit a batch of two upserts: "fresh" (no conflict) and
    // "tracked" with the stale OCC token. Atomic mode must roll back
    // both.
    let ops = vec![
        BatchOp::Upsert {
            file_path: Some("cloudmap.yaml".into()),
            path: "/repositories".into(),
            key: "fresh".into(),
            json: serde_json::json!({"name": "fresh"}),
            expected: None,
        },
        BatchOp::Upsert {
            file_path: Some("cloudmap.yaml".into()),
            path: "/repositories".into(),
            key: "tracked".into(),
            json: serde_json::json!({"name": "stomp"}),
            expected: Some(CommitRef::Pending(v1)),
        },
    ];
    let outcome = sync
        .apply_batch(ops, true, None)
        .await
        .expect("apply_batch");
    assert!(outcome.applied.is_empty(), "atomic rollback expected");
    assert_eq!(outcome.failed.len(), 1);
    assert_eq!(outcome.failed[0].key, "tracked");
    assert!(matches!(outcome.failed[0].error, Error::Conflict { .. }));
    // The "fresh" record must NOT exist — atomic rollback.
    assert!(sync
        .get_record("cloudmap.yaml", "/repositories", "fresh")
        .await
        .expect("get")
        .is_none());
}

async fn run_apply_batch_non_atomic_partial(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");

    let first = sync
        .apply_batch(
            vec![BatchOp::Upsert {
                file_path: Some("cloudmap.yaml".into()),
                path: "/repositories".into(),
                key: "tracked".into(),
                json: serde_json::json!({"name": "v1"}),
                expected: None,
            }],
            true,
            None,
        )
        .await
        .expect("first");
    let v1 = first.applied[0].outcome.version;

    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "tracked",
        serde_json::json!({"name": "v2"}),
        None,
    )
    .await
    .expect("oob update");

    let ops = vec![
        BatchOp::Upsert {
            file_path: Some("cloudmap.yaml".into()),
            path: "/repositories".into(),
            key: "fresh".into(),
            json: serde_json::json!({"name": "fresh"}),
            expected: None,
        },
        BatchOp::Upsert {
            file_path: Some("cloudmap.yaml".into()),
            path: "/repositories".into(),
            key: "tracked".into(),
            json: serde_json::json!({"name": "stomp"}),
            expected: Some(CommitRef::Pending(v1)),
        },
        BatchOp::Upsert {
            file_path: Some("cloudmap.yaml".into()),
            path: "/repositories".into(),
            key: "after".into(),
            json: serde_json::json!({"name": "after"}),
            expected: None,
        },
    ];
    let outcome = sync
        .apply_batch(ops, false, None)
        .await
        .expect("apply_batch");
    assert_eq!(outcome.applied.len(), 2, "{:?}", outcome.applied);
    assert_eq!(outcome.failed.len(), 1);
    assert_eq!(outcome.failed[0].key, "tracked");
    let applied_keys: Vec<&str> = outcome.applied.iter().map(|a| a.key.as_str()).collect();
    assert!(applied_keys.contains(&"fresh"));
    assert!(applied_keys.contains(&"after"));
    // "tracked" must keep its v2 OOB value (not stomped).
    let rec = sync
        .get_record("cloudmap.yaml", "/repositories", "tracked")
        .await
        .expect("get")
        .expect("found");
    assert_eq!(rec.json["name"], "v2");
    // "fresh" and "after" persisted.
    for k in ["fresh", "after"] {
        assert!(sync
            .get_record("cloudmap.yaml", "/repositories", k)
            .await
            .expect("get")
            .is_some());
    }
}

/// Page a whole section with `after` + `limit` and check the walk is
/// exactly the unpaged result: no record skipped, none seen twice.
async fn run_find_records_paging(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");

    let all = sync
        .find_records(&RecordQuery {
            path: Some("/artifacts".into()),
            ..Default::default()
        })
        .await
        .expect("unpaged");
    assert!(all.len() > 3, "fixture should have several artifacts");
    let expected: Vec<String> = all.iter().map(|r| r.key.clone()).collect();

    // Walk it two at a time, following the cursor.
    let mut seen: Vec<String> = Vec::new();
    let mut after: Option<unfurl_git_sync::Cursor> = None;
    for _ in 0..100 {
        let page = sync
            .find_records(&RecordQuery {
                path: Some("/artifacts".into()),
                after: after.clone(),
                limit: Some(3),
                ..Default::default()
            })
            .await
            .expect("page");
        if page.is_empty() {
            break;
        }
        let last = page.last().expect("non-empty");
        after = Some(unfurl_git_sync::Cursor {
            path: last.path.clone(),
            key: last.key.clone(),
            file_path: Some(last.file_path.clone()),
            worktree_id: Some(last.worktree_id),
        });
        seen.extend(page.iter().map(|r| r.key.clone()));
    }
    assert_eq!(seen, expected, "pages must reassemble the unpaged scan");

    // `limit` alone caps the result and preserves the ordering.
    let capped = sync
        .find_records(&RecordQuery {
            path: Some("/artifacts".into()),
            limit: Some(2),
            ..Default::default()
        })
        .await
        .expect("limit");
    assert_eq!(capped.len(), 2);
    assert_eq!(
        capped.iter().map(|r| r.key.clone()).collect::<Vec<_>>(),
        expected[..2].to_vec()
    );

    // A cursor naming a record that no longer exists still resumes: the
    // bound is a value, not a row reference.
    let ghost = Some(unfurl_git_sync::Cursor::new(
        "/artifacts",
        format!("{}\u{1}gone", expected[0]),
    ));
    let resumed = sync
        .find_records(&RecordQuery {
            path: Some("/artifacts".into()),
            after: ghost,
            ..Default::default()
        })
        .await
        .expect("ghost cursor");
    assert_eq!(
        resumed.iter().map(|r| r.key.clone()).collect::<Vec<_>>(),
        expected[1..].to_vec()
    );
}

/// The page cursor promises byte-wise `(path, key)` ordering. Postgres'
/// default collation is locale-dependent and would sort "e-with-acute"
/// before "z"; the `COLLATE "C"` in `find_pg` is what keeps a token
/// minted by sqlite or python meaning the same thing there.
async fn run_find_records_paging_is_byte_ordered(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");
    for key in ["z-repo", "\u{e9}-repo"] {
        sync.create_record(
            Some("cloudmap.yaml"),
            "/repositories",
            key,
            serde_json::json!({"name": key}),
            None,
        )
        .await
        .expect("create");
    }

    let rows = sync
        .find_records(&RecordQuery {
            path: Some("/repositories".into()),
            ..Default::default()
        })
        .await
        .expect("find");
    let keys: Vec<&str> = rows.iter().map(|r| r.key.as_str()).collect();
    let z = keys.iter().position(|k| *k == "z-repo").expect("z-repo");
    let e = keys
        .iter()
        .position(|k| *k == "\u{e9}-repo")
        .expect("e-repo");
    assert!(
        z < e,
        "UTF-8 byte order puts 'z' (0x7a) before 'e-acute' (0xc3a9): {keys:?}"
    );

    // And a cursor lands between them accordingly.
    let after = Some(unfurl_git_sync::Cursor::new("/repositories", "z-repo"));
    let rest = sync
        .find_records(&RecordQuery {
            path: Some("/repositories".into()),
            after,
            ..Default::default()
        })
        .await
        .expect("after z");
    assert_eq!(
        rest.iter().map(|r| r.key.as_str()).collect::<Vec<_>>(),
        vec!["\u{e9}-repo"],
        "resuming after 'z-repo' yields only the byte-greater key"
    );
}

/// A record that was deleted is invisible to a normal search but must be
/// reachable by a client catching up from a watermark -- otherwise the
/// delete is unobservable, since the row simply stops being returned.
async fn run_find_records_reports_tombstones(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");
    let key = "doomed";
    sync.create_record(
        Some("cloudmap.yaml"),
        "/repositories",
        key,
        serde_json::json!({"name": key}),
        None,
    )
    .await
    .expect("create");

    let before = sync
        .find_records(&RecordQuery::default())
        .await
        .expect("find")
        .iter()
        .map(|r| r.version)
        .max()
        .expect("records");

    sync.delete_record(Some("cloudmap.yaml"), "/repositories", key, None)
        .await
        .expect("delete");

    // The default view hides it, exactly as before.
    let live = sync
        .find_records(&RecordQuery {
            path: Some("/repositories".into()),
            ..Default::default()
        })
        .await
        .expect("live");
    assert!(
        !live.iter().any(|r| r.key == key),
        "a deleted record must not appear in a live view"
    );

    // Catching up from the watermark surfaces it, flagged.
    let caught_up = sync
        .find_records(&RecordQuery {
            since_version: Some(before),
            include_deleted: true,
            ..Default::default()
        })
        .await
        .expect("catch up");
    let tomb = caught_up
        .iter()
        .find(|r| r.key == key)
        .expect("the tombstone should be reported");
    assert!(tomb.deleted, "and be marked as one: {tomb:?}");
    assert!(
        tomb.version > before,
        "with a version past the watermark, so it is seen exactly once"
    );
}

/// Facet aggregation: extraction rule (array elements / object keys /
/// scalars), distinct-record counting, composite columns, rollup
/// remapping, and composition with the shared record filters. The same
/// body runs on both backends; object-valued cells are compared after
/// canonicalize-and-merge because sqlite legitimately splits them by
/// stored key order (the documented approximation the server merges).
async fn run_facet_records(sync: &SyncedRepo, _tmp: &TempDir) {
    use std::collections::BTreeMap;
    use unfurl_git_sync::{canonical_facet_key, FacetColumnRow, FacetPath, FacetSpec};

    sync.update_from_working_dir().await.expect("update");

    // A path outside the fixture's sections keeps the counts hermetic.
    for (key, json) in [
        // two topics; two platform objects; one declared type
        (
            "a1",
            serde_json::json!({"type": {"Derived": {}},
                "metadata": {"topics": ["db", "web"],
                "platforms": [{"os": "linux", "architecture": "amd64"},
                              {"os": "windows", "architecture": "arm64"}]}}),
        ),
        // duplicate array element: must still count once per bucket
        (
            "a2",
            serde_json::json!({"type": {"Base": {}},
                "metadata": {"topics": ["db", "db"]}}),
        ),
        // two declared types; platform spelled in the *other* key order
        (
            "a3",
            serde_json::json!({"type": {"Derived": {}, "Other": {}},
                "metadata": {"topics": ["web"],
                "platforms": [{"architecture": "amd64", "os": "linux"}]}}),
        ),
        // no topics, no type: counts toward total only
        ("a4", serde_json::json!({"metadata": {"name": "quiet"}})),
        // declared type with no rollup pairs: falls back to itself
        ("a5", serde_json::json!({"type": {"Unknown": {}}})),
        // scalar and boolean facet values exercise the non-container arms
        ("a6", serde_json::json!({"flag": true, "level": 3})),
    ] {
        sync.create_record(Some("cloudmap.yaml"), "/facettest", key, json, None)
            .await
            .expect("create facet record");
    }

    let query = RecordQuery {
        path: Some("/facettest".into()),
        ..Default::default()
    };
    let path = |tokens: &[&str], rollup: bool| {
        FacetPath::new(tokens.iter().map(|t| t.to_string()).collect(), rollup)
            .expect("valid facet path")
    };
    // Canonicalize-and-merge, summing counts -- exact here because no
    // record spells the same value two ways.
    fn group_counts(rows: &[(serde_json::Value, i64)]) -> BTreeMap<String, i64> {
        let mut out = BTreeMap::new();
        for (value, n) in rows {
            *out.entry(canonical_facet_key(value)).or_insert(0) += n;
        }
        out
    }
    fn cell_counts(rows: &[FacetColumnRow]) -> BTreeMap<(String, Vec<String>), i64> {
        let mut out = BTreeMap::new();
        for row in rows {
            let key = (
                canonical_facet_key(&row.group),
                row.members.iter().map(canonical_facet_key).collect(),
            );
            *out.entry(key).or_insert(0) += row.count;
        }
        out
    }
    let expect = |pairs: &[(&str, i64)]| -> BTreeMap<String, i64> {
        pairs.iter().map(|(k, n)| (k.to_string(), *n)).collect()
    };

    // 1. Group over an array path: elements fan out, duplicates count once.
    let spec = FacetSpec {
        group: path(&["metadata", "topics"], false),
        columns: vec![],
        rollup_pairs: vec![],
    };
    let rows = sync.facet_records(&query, &spec).await.expect("facet");
    assert_eq!(rows.total, 6, "every record counts toward total");
    assert_eq!(rows.columns, Vec::<Vec<FacetColumnRow>>::new());
    assert_eq!(
        group_counts(&rows.groups),
        expect(&[("db", 2), ("web", 2)]),
        "a2's duplicate 'db' must not double-count"
    );

    // 2. Group over the `type` map: keys are the facet values.
    let spec = FacetSpec {
        group: path(&["type"], false),
        columns: vec![],
        rollup_pairs: vec![],
    };
    let rows = sync.facet_records(&query, &spec).await.expect("facet");
    assert_eq!(
        group_counts(&rows.groups),
        expect(&[("Base", 1), ("Derived", 2), ("Other", 1), ("Unknown", 1)])
    );

    // 3. Scalar and boolean group values.
    let spec = FacetSpec {
        group: path(&["flag"], false),
        columns: vec![],
        rollup_pairs: vec![],
    };
    let rows = sync.facet_records(&query, &spec).await.expect("facet");
    assert_eq!(group_counts(&rows.groups), expect(&[("true", 1)]));
    let spec = FacetSpec {
        group: path(&["level"], false),
        columns: vec![],
        rollup_pairs: vec![],
    };
    let rows = sync.facet_records(&query, &spec).await.expect("facet");
    assert_eq!(group_counts(&rows.groups), expect(&[("3", 1)]));

    // 4. Rollup: Derived counts under Base too (self-pairs included);
    //    Unknown has no pairs and falls back to itself.
    let rollup_pairs = vec![
        ("Derived".to_string(), "Derived".to_string()),
        ("Derived".to_string(), "Base".to_string()),
        ("Base".to_string(), "Base".to_string()),
        ("Other".to_string(), "Other".to_string()),
    ];
    let spec = FacetSpec {
        group: path(&["type"], true),
        columns: vec![],
        rollup_pairs: rollup_pairs.clone(),
    };
    let rows = sync.facet_records(&query, &spec).await.expect("facet");
    assert_eq!(
        group_counts(&rows.groups),
        expect(&[("Base", 3), ("Derived", 2), ("Other", 1), ("Unknown", 1)]),
        "Base's bucket = its own record plus both Derived records"
    );

    // 5. Facet column with rollup on the member: per-topic type
    //    breakdown, rolled up.
    let spec = FacetSpec {
        group: path(&["metadata", "topics"], false),
        columns: vec![vec![path(&["type"], true)]],
        rollup_pairs: rollup_pairs.clone(),
    };
    let rows = sync.facet_records(&query, &spec).await.expect("facet");
    assert_eq!(group_counts(&rows.groups), expect(&[("db", 2), ("web", 2)]));
    let cells = cell_counts(&rows.columns[0]);
    let cell = |g: &str, vs: &[&str]| (g.to_string(), vs.iter().map(|v| v.to_string()).collect());
    assert_eq!(
        cells,
        [
            (cell("db", &["Base"]), 2),
            (cell("db", &["Derived"]), 1),
            (cell("web", &["Base"]), 2),
            (cell("web", &["Derived"]), 2),
            (cell("web", &["Other"]), 1),
        ]
        .into_iter()
        .collect::<BTreeMap<_, _>>()
    );

    // 6. Composite column: per-record cross product of type × platform;
    //    a record missing either member is absent from the column, and
    //    the two platform spellings land in one bucket after
    //    canonicalization (pg merges them in SQL, sqlite in the merge).
    let spec = FacetSpec {
        group: path(&["metadata", "topics"], false),
        columns: vec![vec![
            path(&["type"], false),
            path(&["metadata", "platforms"], false),
        ]],
        rollup_pairs: vec![],
    };
    let rows = sync.facet_records(&query, &spec).await.expect("facet");
    let cells = cell_counts(&rows.columns[0]);
    let linux = r#"{"architecture":"amd64","os":"linux"}"#;
    let windows = r#"{"architecture":"arm64","os":"windows"}"#;
    assert_eq!(
        cells,
        [
            (cell("db", &["Derived", linux]), 1),
            (cell("db", &["Derived", windows]), 1),
            (cell("web", &["Derived", linux]), 2),
            (cell("web", &["Derived", windows]), 1),
            (cell("web", &["Other", linux]), 1),
        ]
        .into_iter()
        .collect::<BTreeMap<_, _>>(),
        "web×Derived×linux reaches 2 via a1 and a3's shuffled spelling"
    );

    // 7. The shared filters compose: a json_query narrows total, groups
    //    and columns alike.
    let narrowed = RecordQuery {
        path: Some("/facettest".into()),
        json_queries: vec![JsonQuery::new(
            vec!["metadata".into(), "topics".into()],
            serde_json::json!("db"),
        )
        .expect("query")],
        ..Default::default()
    };
    let spec = FacetSpec {
        group: path(&["metadata", "topics"], false),
        columns: vec![],
        rollup_pairs: vec![],
    };
    let rows = sync.facet_records(&narrowed, &spec).await.expect("facet");
    assert_eq!(rows.total, 2, "only a1 and a2 match the filter");
    assert_eq!(group_counts(&rows.groups), expect(&[("db", 2), ("web", 1)]));

    // 8. A path no record has: empty groups, full total.
    let spec = FacetSpec {
        group: path(&["nowhere"], false),
        columns: vec![],
        rollup_pairs: vec![],
    };
    let rows = sync.facet_records(&query, &spec).await.expect("facet");
    assert_eq!(rows.total, 6);
    assert!(rows.groups.is_empty());
}

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
crud_test!(
    record_field_order_survives_the_db,
    run_record_field_order_survives_the_db
);
crud_test!(
    new_record_gets_the_canonical_field_order,
    run_new_record_gets_the_canonical_field_order
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
crud_test!(facet_records, run_facet_records);
async fn run_find_records_json_query(sync: &SyncedRepo, _tmp: &TempDir) {
    // A `JsonQuery` is pushed into the SQL WHERE clause. The same predicate has
    // to mean the same thing on both backends, which is what running this test
    // under `crud_test!` checks: sqlite uses `json_each`, postgres the `@?`
    // jsonpath operator (in lax mode, so `[*]` covers arrays and scalars
    // alike).
    sync.update_from_working_dir().await.expect("update");

    let find = |tokens: Vec<&str>, value: serde_json::Value| {
        let q = JsonQuery::new(tokens.into_iter().map(str::to_string).collect(), value)
            .expect("valid query");
        async move {
            sync.find_records(&RecordQuery {
                json_queries: vec![q],
                ..Default::default()
            })
            .await
            .expect("json query")
        }
    };

    // An array: `metadata/discovery/sources` contains the url.
    let hits = find(
        vec!["metadata", "discovery", "sources"],
        serde_json::json!("https://hub.docker.com/v2/repositories/bitnami/odoo/"),
    )
    .await;
    assert_eq!(
        hits.iter().map(|r| r.key.as_str()).collect::<Vec<_>>(),
        vec!["pkg:oci/odoo?repository_url=docker.io/bitnami/odoo"],
        "array contains"
    );

    // A scalar at the same shape of path: `metadata/homepage_url` equals it.
    let hits = find(
        vec!["metadata", "homepage_url"],
        serde_json::json!("https://unfurl.cloud/feb20a/dashboard"),
    )
    .await;
    assert_eq!(
        hits.iter().map(|r| r.key.as_str()).collect::<Vec<_>>(),
        vec!["git://unfurl.cloud/feb20a/dashboard.git"],
        "scalar equals"
    );

    // A member of an object is addressed by putting its key in the path --
    // `branches` maps a branch name to a sha. Searching the object's *values*
    // (a `.*` jsonpath) isn't supported: postgres can't serve it from a GIN
    // index, so neither backend does it.
    let hits = find(
        vec!["branches", "main"],
        serde_json::json!("4551885dfab39991cfdb958cb79fcb6aa282481d"),
    )
    .await;
    assert_eq!(
        hits.iter().map(|r| r.key.as_str()).collect::<Vec<_>>(),
        vec!["git://unfurl.cloud/feb20a/dashboard.git"],
        "member addressed by key"
    );
    assert!(
        find(
            vec!["branches"],
            serde_json::json!("4551885dfab39991cfdb958cb79fcb6aa282481d"),
        )
        .await
        .is_empty(),
        "the object itself doesn't match one of its members"
    );

    // An array of strings.
    let hits = find(vec!["metadata", "topics"], serde_json::json!("library")).await;
    assert!(
        hits.iter()
            .any(|r| r.key == "git://unfurl.cloud/onecommons/std.git"),
        "array of topics: {:?}",
        hits.iter().map(|r| &r.key).collect::<Vec<_>>()
    );

    // An array *literal* is an exact match rather than a containment test:
    // same elements, same order. The fixture's std.git has
    // `metadata/topics: [documentation, library]`.
    let std_repo = "git://unfurl.cloud/onecommons/std.git";
    let hits = find(
        vec!["metadata", "topics"],
        serde_json::json!(["documentation", "library"]),
    )
    .await;
    assert_eq!(
        hits.iter().map(|r| r.key.as_str()).collect::<Vec<_>>(),
        vec![std_repo],
        "exact array"
    );
    // a subset isn't equal...
    assert!(
        find(vec!["metadata", "topics"], serde_json::json!(["library"]))
            .await
            .is_empty(),
        "a subset is not an exact match"
    );
    // ...nor is the same set in a different order
    assert!(
        find(
            vec!["metadata", "topics"],
            serde_json::json!(["library", "documentation"]),
        )
        .await
        .is_empty(),
        "element order matters"
    );
    // while a scalar still means "contains"
    let hits = find(vec!["metadata", "topics"], serde_json::json!("library")).await;
    assert_eq!(
        hits.iter().map(|r| r.key.as_str()).collect::<Vec<_>>(),
        vec![std_repo],
        "scalar still means contains"
    );

    // `^=` (a `StartsWith` query) matches a string prefix, and -- like the
    // scalar `=` form -- any element of an array at the path.
    let prefix = |tokens: Vec<&str>, value: &str| {
        let q = JsonQuery::starts_with(
            tokens.into_iter().map(str::to_string).collect(),
            value.to_string(),
        )
        .expect("valid prefix query");
        async move {
            sync.find_records(&RecordQuery {
                json_queries: vec![q],
                ..Default::default()
            })
            .await
            .expect("prefix query")
        }
    };
    let hits = prefix(
        vec!["metadata", "homepage_url"],
        "https://unfurl.cloud/feb20a",
    )
    .await;
    assert_eq!(
        hits.iter().map(|r| r.key.as_str()).collect::<Vec<_>>(),
        vec!["git://unfurl.cloud/feb20a/dashboard.git"],
        "prefix of a scalar"
    );
    let hits = prefix(vec!["metadata", "topics"], "doc").await;
    assert_eq!(
        hits.iter().map(|r| r.key.as_str()).collect::<Vec<_>>(),
        vec![std_repo],
        "prefix of an array element"
    );
    assert!(
        prefix(vec!["metadata", "homepage_url"], "https://nope")
            .await
            .is_empty(),
        "a prefix that matches nothing"
    );
    // A number never matches a prefix: sqlite's LIKE would coerce it to text
    // without the clause's `jq.type = 'text'` guard, postgres' `starts with`
    // is string-only.
    assert!(
        prefix(vec!["metadata", "version"], "0.").await.is_empty(),
        "a number is not a string with that prefix"
    );
    // "%" and "_" are literal, not sqlite LIKE wildcards.
    assert!(
        prefix(vec!["metadata", "homepage_url"], "https://%")
            .await
            .is_empty(),
        "LIKE metacharacters in the prefix are escaped"
    );

    // A bare path (no value) is an existence test. Both backends count a
    // `null` and an empty container as present -- only a path that doesn't
    // resolve is absent.
    let exists = |tokens: Vec<&str>| {
        let q = JsonQuery::exists(tokens.into_iter().map(str::to_string).collect())
            .expect("valid existence query");
        async move {
            sync.find_records(&RecordQuery {
                json_queries: vec![q],
                ..Default::default()
            })
            .await
            .expect("existence query")
        }
    };
    assert_eq!(
        exists(vec!["metadata", "topics"])
            .await
            .iter()
            .map(|r| r.key.as_str())
            .collect::<Vec<_>>(),
        vec![std_repo],
        "only std.git has metadata/topics"
    );
    // `contains` holds null-valued members, so the path resolves to a null
    assert!(
        !exists(vec!["contains"]).await.is_empty(),
        "a null-valued member still exists"
    );
    assert!(
        exists(vec!["metadata", "no_such_field"]).await.is_empty(),
        "a path that doesn't resolve"
    );

    // An object literal is rejected: postgres compares objects structurally
    // and sqlite by rendered text, so the backends would disagree.
    assert!(JsonQuery::new(
        vec!["metadata".to_string()],
        serde_json::json!({"title": "x"}),
    )
    .is_err());

    // A value that isn't there matches nothing, and neither does a path that
    // doesn't resolve.
    assert!(find(
        vec!["metadata", "homepage_url"],
        serde_json::json!("https://nope.example.com")
    )
    .await
    .is_empty());
    assert!(
        find(vec!["metadata", "nope", "deeper"], serde_json::json!("x"))
            .await
            .is_empty()
    );

    // Types are compared as JSON: the string "1" is not the number 1.
    let numeric = find(vec!["metadata", "version"], serde_json::json!(1)).await;
    let stringy = find(vec!["metadata", "version"], serde_json::json!("1")).await;
    assert_eq!(
        numeric.len(),
        0,
        "fixture has no numeric version 1: {:?}",
        numeric.iter().map(|r| &r.key).collect::<Vec<_>>()
    );
    assert_eq!(stringy.len(), 0);

    // Multiple queries AND together — every one must match. The
    // discriminating case: two filters that each match a record on
    // their own but never the same record must yield nothing (an OR,
    // or applying only the first clause, would return records).
    let find_all = |queries: Vec<(Vec<&str>, serde_json::Value)>| {
        let qs: Vec<JsonQuery> = queries
            .into_iter()
            .map(|(tokens, value)| {
                JsonQuery::new(tokens.into_iter().map(str::to_string).collect(), value)
                    .expect("valid query")
            })
            .collect();
        async move {
            sync.find_records(&RecordQuery {
                json_queries: qs,
                ..Default::default()
            })
            .await
            .expect("anded queries")
        }
    };
    let hits = find_all(vec![
        (vec!["private"], serde_json::json!(true)),
        (
            vec!["branches", "main"],
            serde_json::json!("4551885dfab39991cfdb958cb79fcb6aa282481d"),
        ),
    ])
    .await;
    assert_eq!(
        hits.iter().map(|r| r.key.as_str()).collect::<Vec<_>>(),
        vec!["git://unfurl.cloud/feb20a/dashboard.git"],
        "both filters hold for the dashboard repo"
    );
    assert!(
        find_all(vec![
            (vec!["private"], serde_json::json!(true)),
            (
                vec!["metadata", "homepage_url"],
                serde_json::json!("https://unfurl.cloud/onecommons/blueprints/odoo"),
            ),
        ])
        .await
        .is_empty(),
        "each filter matches a record, but no record matches both"
    );
}

crud_test!(find_records_json_query, run_find_records_json_query);
crud_test!(find_records_type_filter, run_find_records_type_filter);
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
crud_test!(apply_batch_atomic_success, run_apply_batch_atomic_success);
crud_test!(
    apply_batch_atomic_conflict_rolls_back,
    run_apply_batch_atomic_conflict_rolls_back
);
crud_test!(
    apply_batch_non_atomic_partial,
    run_apply_batch_non_atomic_partial
);
crud_test!(find_records_paging, run_find_records_paging);
crud_test!(
    find_records_paging_is_byte_ordered,
    run_find_records_paging_is_byte_ordered
);

/// The `COLLATE "C"` guard, run against a column that is *not* C-collated.
///
/// `run_find_records_paging_is_byte_ordered` above can't catch a missing
/// `COLLATE "C"` on its own: it only discriminates when the database's
/// default collation is locale-based, and a test database is usually
/// created with `C`. So force the adverse condition -- retype the two
/// ordering columns to a locale collation, under which "e-acute" sorts
/// *before* "z" -- and check the query still returns byte order.
#[cfg(feature = "postgres")]
#[tokio::test]
async fn find_records_paging_overrides_a_locale_column_collation() {
    use sqlx::Executor as _;

    let Some((sync, tmp, scope)) = pg_fixture().await else {
        eprintln!("skip: UNFURL_TEST_PG_URL not set");
        return;
    };
    sync.update_from_working_dir().await.expect("update");

    let DbConfig::Postgres { url } = scope.db_config() else {
        unreachable!("pg_fixture yields a postgres config")
    };
    let pool = sqlx::PgPool::connect(&url).await.expect("connect");
    // Servers without this locale can't run the check; skip rather than fail.
    let has_locale: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM pg_collation WHERE collname = 'en_US.UTF-8')",
    )
    .fetch_one(&pool)
    .await
    .expect("collation probe");
    if !has_locale {
        eprintln!("skip: server has no en_US.UTF-8 collation");
        drop(sync);
        drop(tmp);
        scope.teardown().await;
        return;
    }
    pool.execute(
        "ALTER TABLE record \
         ALTER COLUMN path TYPE text COLLATE \"en_US.UTF-8\", \
         ALTER COLUMN key TYPE text COLLATE \"en_US.UTF-8\"",
    )
    .await
    .expect("retype columns to a locale collation");

    for key in ["z-repo", "\u{e9}-repo"] {
        sync.create_record(
            Some("cloudmap.yaml"),
            "/repositories",
            key,
            serde_json::json!({"name": key}),
            None,
        )
        .await
        .expect("create");
    }

    let rows = sync
        .find_records(&RecordQuery {
            path: Some("/repositories".into()),
            ..Default::default()
        })
        .await
        .expect("find");
    let keys: Vec<&str> = rows.iter().map(|r| r.key.as_str()).collect();
    let z = keys.iter().position(|k| *k == "z-repo").expect("z-repo");
    let e = keys
        .iter()
        .position(|k| *k == "\u{e9}-repo")
        .expect("e-repo");
    assert!(
        z < e,
        "the query must impose byte order even on locale-collated columns: {keys:?}"
    );

    // And the cursor agrees, so a token minted by sqlite or python resumes
    // in the same place here.
    let after = Some(unfurl_git_sync::Cursor::new("/repositories", "z-repo"));
    let rest = sync
        .find_records(&RecordQuery {
            path: Some("/repositories".into()),
            after,
            ..Default::default()
        })
        .await
        .expect("after z");
    assert_eq!(
        rest.iter().map(|r| r.key.as_str()).collect::<Vec<_>>(),
        vec!["\u{e9}-repo"]
    );

    drop(sync);
    drop(tmp);
    scope.teardown().await;
}
crud_test!(
    find_records_reports_tombstones,
    run_find_records_reports_tombstones
);

async fn run_resync_deletes_missing_records(sync: &SyncedRepo, tmp: &TempDir) {
    let stats = sync.update_from_working_dir().await.expect("update");
    assert!(stats.records_upserted >= 5, "stats: {stats:?}");
    assert_eq!(stats.records_deleted, 0, "stats: {stats:?}");

    // Remove one repository from the file on disk and re-sync: the
    // vanished record must be hard-deleted, everything else kept.
    let removed_key = "git://unfurl.cloud/feb20a/dashboard.git";
    let kept_key = "git://unfurl.cloud/onecommons/std.git";
    let cloudmap_path = tmp.path().join("cloudmap.yaml");
    let text = std::fs::read_to_string(&cloudmap_path).expect("read cloudmap");
    let mut value: serde_json::Value = serde_saphyr::from_str(&text).expect("parse yaml");
    value
        .get_mut("repositories")
        .and_then(|v| v.as_object_mut())
        .expect("repositories section")
        .remove(removed_key)
        .expect("fixture contains the repository");
    std::fs::write(
        &cloudmap_path,
        serde_saphyr::to_string(&value).expect("emit yaml"),
    )
    .expect("write cloudmap");

    let stats = sync.update_from_working_dir().await.expect("re-sync");
    assert_eq!(stats.records_deleted, 1, "stats: {stats:?}");
    assert!(sync
        .get_record("cloudmap.yaml", "/repositories", removed_key)
        .await
        .expect("get removed")
        .is_none());
    assert!(sync
        .get_record("cloudmap.yaml", "/repositories", kept_key)
        .await
        .expect("get kept")
        .is_some());
}
crud_test!(
    resync_deletes_missing_records,
    run_resync_deletes_missing_records
);

// ---------------------------------------------------------------------------
// txn audit table and the commit-message rollup
// ---------------------------------------------------------------------------

/// HEAD's full commit message.
fn head_commit_body(dir: &std::path::Path) -> String {
    let out = std::process::Command::new("git")
        .args(["log", "-1", "--format=%B"])
        .current_dir(dir)
        .output()
        .expect("git log");
    assert!(out.status.success(), "{out:?}");
    String::from_utf8_lossy(&out.stdout).to_string()
}

/// The value of a trailer on HEAD, **as git itself parses it**.
///
/// Asserting on the raw message text would pass even if the trailer
/// block were malformed -- a missing blank line makes git read the whole
/// thing as prose while a substring check happily still matches. Going
/// through `%(trailers:key=...)` is what proves the block is well-formed.
fn head_trailer(dir: &std::path::Path, key: &str) -> Option<String> {
    let out = std::process::Command::new("git")
        .args([
            "log",
            "-1",
            &format!("--format=%(trailers:key={key},valueonly)"),
        ])
        .current_dir(dir)
        .output()
        .expect("git log");
    assert!(out.status.success(), "{out:?}");
    let value = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (!value.is_empty()).then_some(value)
}

/// A batch upsert into `/repositories`, no OCC token.
fn upsert_op(key: &str) -> BatchOp {
    BatchOp::Upsert {
        file_path: Some("cloudmap.yaml".into()),
        path: "/repositories".into(),
        key: key.into(),
        json: serde_json::json!({"name": key}),
        expected: None,
    }
}

fn delete_op(key: &str) -> BatchOp {
    BatchOp::Delete {
        file_path: Some("cloudmap.yaml".into()),
        path: "/repositories".into(),
        key: key.into(),
        expected: None,
    }
}

const AUTHOR: &str = "Ada Lovelace <ada@example.com>";
const MESSAGE_A: &str = "Point std at the new branch";
// Two paragraphs: the blank line is the case a plain indent would lose
// to trailing-whitespace stripping.
const MESSAGE_B: &str = "Retire the legacy mirror\n\nSuperseded by the dashboard entry.";

async fn run_rollup_round_trips_through_the_commit_message(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");
    // Exists in the fixture, so it can be deleted below.
    let doomed = "git://unfurl.cloud/feb20a/dashboard.git";

    let first = sync
        .apply_batch(
            vec![upsert_op("txn-a"), upsert_op("txn-b")],
            true,
            Some(TxnMeta {
                author: Some(AUTHOR.into()),
                message: Some(MESSAGE_A.into()),
            }),
        )
        .await
        .expect("first batch");
    assert_eq!(first.applied.len(), 2, "{:?}", first.failed);

    // Rewrites txn-a -- so the first batch's write of it is superseded
    // and shows up as a shortfall rather than as a record line.
    let second = sync
        .apply_batch(
            vec![upsert_op("txn-a"), delete_op(doomed)],
            true,
            Some(TxnMeta {
                author: None,
                message: Some(MESSAGE_B.into()),
            }),
        )
        .await
        .expect("second batch");
    assert_eq!(second.applied.len(), 2, "{:?}", second.failed);
    assert!(
        second.applied.iter().any(|a| a.deleted && a.key == doomed),
        "the delete must be reported as one: {:?}",
        second.applied
    );
    assert!(
        second
            .applied
            .iter()
            .any(|a| !a.deleted && a.key == "txn-a"),
        "...and the upsert must not be: {:?}",
        second.applied
    );

    let rows = sync.list_transactions().await.expect("list_transactions");
    assert_eq!(rows.len(), 2, "{rows:?}");
    let wd = sync.get_working_dir().await.expect("get_working_dir");

    let oid = sync
        .commit_repository("Update cloudmap")
        .await
        .expect("commit")
        .expect("something was dirty");

    let body = head_commit_body(tmp.path());

    // --- the parse-back contract -------------------------------------
    // The message is the only machine-readable copy, so the crate's own
    // parser is the assertion that matters: a format change that breaks
    // reconstruction fails here even if the prose still looks right.
    let parsed = unfurl_git_sync::parse_commit_rollup(&body)
        .expect("parses")
        .expect("is a git-sync commit");
    assert_eq!(parsed.txns.len(), 2, "{body}");
    assert!(parsed.origin.is_some(), "{body}");
    // Own family here, so the two agree -- the fork case is covered by
    // `a_fork_records_its_family_in_the_rollup`.
    assert_eq!(parsed.family, parsed.origin, "{body}");
    assert!(
        parsed.next_version > rows[1].last_version,
        "counter must cover every version this commit carried: {parsed:?}"
    );

    let a = &parsed.txns[0];
    assert_eq!(a.first_version, rows[0].first_version);
    assert_eq!(a.last_version, rows[0].last_version);
    assert_eq!(a.branch, wd.branch);
    // Verbatim, not a re-rendered date -- this has to round-trip exactly.
    assert_eq!(a.created_at, rows[0].created_at);
    assert_eq!(a.author.as_deref(), Some(AUTHOR));
    assert_eq!(a.message.as_deref(), Some(MESSAGE_A));
    // txn-a was rewritten by the second batch, so only txn-b still
    // carries a version of this one -- and the difference is reported
    // rather than silently dropped.
    assert_eq!(
        a.records.iter().map(|r| r.key.as_str()).collect::<Vec<_>>(),
        vec!["txn-b"],
        "{body}"
    );
    assert_eq!(a.unaccounted(), 1, "{body}");

    let b = &parsed.txns[1];
    assert_eq!(b.author, None, "an absent author round-trips as absent");
    assert_eq!(b.message.as_deref(), Some(MESSAGE_B), "blank line survives");
    assert_eq!(b.unaccounted(), 0, "{body}");
    let deleted: Vec<&str> = b
        .records
        .iter()
        .filter(|r| r.deleted)
        .map(|r| r.key.as_str())
        .collect();
    assert_eq!(deleted, vec![doomed], "the delete is flagged: {body}");
    assert!(
        b.records.iter().any(|r| !r.deleted && r.key == "txn-a"),
        "{body}"
    );

    // --- the human-readable half -------------------------------------
    assert!(
        body.starts_with("Update cloudmap\n\nRollup of 2 git-sync transactions:\n\n"),
        "{body}"
    );
    assert!(
        body.contains(&format!(
            " - {}-{} on {} {} {AUTHOR}\n   | {MESSAGE_A}\n",
            rows[0].first_version, rows[0].last_version, wd.branch, rows[0].created_at,
        )),
        "{body}"
    );
    // A blank message line is a bare marker, never trailing whitespace.
    assert!(body.contains("\n   |\n"), "{body}");
    assert!(!body.contains("   \n"), "no whitespace-only lines: {body}");
    assert!(
        body.contains(&format!(
            "   * {} D \"/repositories\" \"{doomed}\"\n",
            b.records.iter().find(|r| r.deleted).unwrap().version
        )),
        "{body}"
    );
    assert!(
        body.contains("   ! 1 of 2 writes superseded later in this commit, or rolled back\n"),
        "{body}"
    );

    // --- trailers, as git parses them --------------------------------
    assert_eq!(
        head_trailer(tmp.path(), "Git-Sync-Txn-Count").as_deref(),
        Some("2"),
        "{body}"
    );
    assert!(
        head_trailer(tmp.path(), "Git-Sync-Origin").is_some(),
        "{body}"
    );
    assert_eq!(
        head_trailer(tmp.path(), "Git-Sync-Next-Version")
            .expect("counter trailer")
            .parse::<i64>()
            .expect("an integer"),
        parsed.next_version,
        "git and the crate parser must agree: {body}"
    );

    // Rows survive the commit, stamped with the oid that carried them.
    let rows = sync.list_transactions().await.expect("list_transactions");
    assert_eq!(rows.len(), 2, "roll-forward must not delete rows");
    for row in &rows {
        assert_eq!(row.commit_id.as_deref(), Some(oid.as_str()), "{row:?}");
    }
}

async fn run_keys_needing_quoting_round_trip(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");
    // A key with a space and a quote: bare whitespace-splitting would
    // corrupt this, and there is no second copy to fall back on.
    let awkward = "git://example.com/a repo \"quoted\".git";
    sync.apply_batch(
        vec![upsert_op(awkward)],
        true,
        Some(TxnMeta {
            author: Some("Ada: the first <ada@example.com>".into()),
            message: Some("Handle an awkward key".into()),
        }),
    )
    .await
    .expect("batch");

    sync.commit_repository("Update cloudmap")
        .await
        .expect("commit")
        .expect("dirty");

    let body = head_commit_body(tmp.path());
    let parsed = unfurl_git_sync::parse_commit_rollup(&body)
        .expect("parses")
        .expect("is a git-sync commit");
    assert_eq!(parsed.txns[0].records.len(), 1, "{body}");
    assert_eq!(parsed.txns[0].records[0].key, awkward, "{body}");
    assert_eq!(parsed.txns[0].records[0].path, "/repositories", "{body}");
    // The author is the remainder of the header line, so a colon in it
    // is not a delimiter.
    assert_eq!(
        parsed.txns[0].author.as_deref(),
        Some("Ada: the first <ada@example.com>"),
        "{body}"
    );
}

async fn run_commit_without_txns_still_records_the_counter(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");
    // Single-op CRUD writes never record a txn, so this commit has
    // nothing to roll up -- but it still drew a version.
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "no-rollup",
        serde_json::json!({"name": "no-rollup"}),
        None,
    )
    .await
    .expect("upsert");

    sync.commit_repository("just the subject")
        .await
        .expect("commit")
        .expect("something was dirty");

    let body = head_commit_body(tmp.path());
    assert!(!body.contains("Rollup of"), "nothing to roll up: {body}");
    assert_eq!(
        head_trailer(tmp.path(), "Git-Sync-Txn-Count").as_deref(),
        Some("0"),
        "always emitted, so a dropped trailer is detectable: {body}"
    );

    let parsed = unfurl_git_sync::parse_commit_rollup(&body)
        .expect("parses")
        .expect("still a git-sync commit");
    assert!(parsed.txns.is_empty(), "{parsed:?}");
    let version = sync
        .get_record("cloudmap.yaml", "/repositories", "no-rollup")
        .await
        .expect("get")
        .expect("found")
        .version;
    assert!(
        parsed.next_version > version,
        "counter {} must cover the un-rolled-up write at {version}",
        parsed.next_version
    );
}

async fn run_apply_batch_without_meta_records_no_txn(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");

    // `None` opts out of the audit trail entirely.
    let staged = sync
        .apply_batch(vec![upsert_op("tracked")], true, None)
        .await
        .expect("staging batch");
    assert!(sync
        .list_transactions()
        .await
        .expect("list_transactions")
        .is_empty());
    let v1 = staged.applied[0].outcome.version;

    // A batch that rolls back records nothing either, meta or not: the
    // insert rides the same transaction as the writes it describes.
    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "tracked",
        serde_json::json!({"name": "v2"}),
        None,
    )
    .await
    .expect("out-of-band update makes the token below stale");
    let outcome = sync
        .apply_batch(
            vec![
                upsert_op("fresh"),
                BatchOp::Upsert {
                    file_path: Some("cloudmap.yaml".into()),
                    path: "/repositories".into(),
                    key: "tracked".into(),
                    json: serde_json::json!({"name": "stomp"}),
                    expected: Some(CommitRef::Pending(v1)),
                },
            ],
            true,
            Some(TxnMeta {
                author: Some("someone".into()),
                message: Some("doomed".into()),
            }),
        )
        .await
        .expect("conflicting batch");
    assert!(outcome.applied.is_empty(), "atomic rollback expected");
    assert_eq!(outcome.failed.len(), 1);
    assert!(
        sync.list_transactions()
            .await
            .expect("list_transactions")
            .is_empty(),
        "a rolled-back batch must leave no audit row"
    );
}

crud_test!(
    rollup_round_trips_through_the_commit_message,
    run_rollup_round_trips_through_the_commit_message
);
crud_test!(
    keys_needing_quoting_round_trip,
    run_keys_needing_quoting_round_trip
);
crud_test!(
    commit_without_txns_still_records_the_counter,
    run_commit_without_txns_still_records_the_counter
);
crud_test!(
    apply_batch_without_meta_records_no_txn,
    run_apply_batch_without_meta_records_no_txn
);

// Parser rejection cases. These need no database, so they run once.

#[test]
fn parse_ignores_a_message_that_is_not_from_git_sync() {
    assert!(
        unfurl_git_sync::parse_commit_rollup("Just a commit\n\nWith a body.\n")
            .expect("not an error")
            .is_none()
    );
    // Rollup-looking prose with no trailers is still not a git-sync
    // commit -- someone writing *about* the format.
    let decoy = "Document the format\n\nRollup of 2 git-sync transactions:\n\n - 45-66 on main x\n";
    assert!(unfurl_git_sync::parse_commit_rollup(decoy)
        .expect("not an error")
        .is_none());
}

#[test]
fn parse_rejects_a_message_it_cannot_trust() {
    // Announces itself, then contradicts itself: a squash merge of two
    // git-sync commits looks like this. Reporting "no batches" here
    // would lose history silently, so it must be an error.
    let mismatch = "Subject\n\nRollup of 1 git-sync transaction:\n\n \
                    - 5 on main 2026-08-23T19:46:56-07:00 Ada\n\n\
                    Git-Sync-Txn-Count: 2\nGit-Sync-Next-Version: 9\n";
    assert!(unfurl_git_sync::parse_commit_rollup(mismatch).is_err());

    // The counter trailer without the count trailer.
    let no_count = "Subject\n\nGit-Sync-Next-Version: 9\n";
    assert!(unfurl_git_sync::parse_commit_rollup(no_count).is_err());

    // An unrecognized record flag. The set is documented as closed, so
    // this must be an error rather than a guess -- a parser that fell
    // back to "not a delete" would resurrect deleted records.
    let bad_flag = "Subject\n\nRollup of 1 git-sync transaction:\n\n \
                    - 5 on main 2026-08-23T19:46:56-07:00 Ada\n   \
                    * 5 X \"/repositories\" \"key\"\n\n\
                    Git-Sync-Txn-Count: 1\nGit-Sync-Next-Version: 9\n";
    assert!(unfurl_git_sync::parse_commit_rollup(bad_flag).is_err());

    // A record line that lost its closing quote.
    let truncated = "Subject\n\nRollup of 1 git-sync transaction:\n\n \
                     - 5 on main 2026-08-23T19:46:56-07:00 Ada\n   \
                     * 5 M \"/repositories\" \"unterminated\n\n\
                     Git-Sync-Txn-Count: 1\nGit-Sync-Next-Version: 9\n";
    assert!(unfurl_git_sync::parse_commit_rollup(truncated).is_err());
}

// ---------------------------------------------------------------------------
// origin normalization
// ---------------------------------------------------------------------------
//
// These need two `SyncedRepo` handles sharing one database, so they use a
// file-backed sqlite rather than the `:memory:` fixture, and run once
// instead of through `crud_test!`.

fn git(dir: &std::path::Path, args: &[&str]) {
    let out = std::process::Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .expect("git");
    assert!(out.status.success(), "git {args:?}: {out:?}");
}

/// A repo seeded with the cloudmap fixture plus a sqlite file beside it.
async fn file_backed_fixture() -> (TempDir, String) {
    let tmp = tempfile::tempdir().expect("tempdir");
    common::init_repo_with_fixture(tmp.path()).await;
    let db = format!("sqlite://{}?mode=rwc", tmp.path().join("sync.db").display());
    (tmp, db)
}

async fn open_at(dir: &std::path::Path, db: &str) -> SyncedRepo {
    SyncedRepo::open(
        dir,
        unfurl_git_sync::DbConfig::Sqlite { url: db.into() },
        unfurl_git_sync::FormatRegistry::with_builtins(),
    )
    .await
    .expect("open SyncedRepo")
}

/// One repository reached two ways is one worktree, not two.
#[tokio::test]
async fn url_spellings_resolve_to_one_worktree() {
    let (tmp, db) = file_backed_fixture().await;
    git(
        tmp.path(),
        &[
            "remote",
            "add",
            "origin",
            "https://unfurl.cloud/onecommons/cloudmap.git",
        ],
    );

    let https = open_at(tmp.path(), &db).await;
    https.update_from_working_dir().await.expect("sync");
    https
        .upsert_record(
            Some("cloudmap.yaml"),
            "/repositories",
            "shared",
            serde_json::json!({"name": "shared"}),
            None,
        )
        .await
        .expect("write");
    drop(https);

    // The same person switching their remote to ssh for key auth is the
    // everyday trigger: it must not strand the records or restart the
    // version counter.
    git(
        tmp.path(),
        &[
            "remote",
            "set-url",
            "origin",
            "ssh://git@unfurl.cloud/onecommons/cloudmap.git",
        ],
    );
    let ssh = open_at(tmp.path(), &db).await;
    let found = ssh
        .get_record("cloudmap.yaml", "/repositories", "shared")
        .await
        .expect("get")
        .expect("the https handle's write belongs to this worktree too");
    assert_eq!(found.json["name"], "shared");

    let rows: Vec<(i64, String)> = sqlx::query_as("SELECT id, origin FROM worktree ORDER BY id")
        .fetch_all(&sqlx::SqlitePool::connect(&db).await.expect("connect"))
        .await
        .expect("query");
    assert_eq!(rows.len(), 1, "one repository, one row: {rows:?}");
    assert_eq!(rows[0].1, "unfurl.cloud/onecommons/cloudmap");
}

/// Two files holding the same `(path, key)` are two records, and paging
/// must return both.
async fn run_paging_spans_duplicate_keys_across_files(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");
    // The same key in two files. The unique index is per
    // (worktree, file, path, key), so this is legal storage even though
    // the cloudmap document model would collapse the pair.
    let dup = "git://example.com/same-key.git";
    for file in ["cloudmap.yaml", "second.yaml"] {
        sync.upsert_record(
            Some(file),
            "/repositories",
            dup,
            serde_json::json!({"name": file}),
            None,
        )
        .await
        .expect("write");
    }

    // Walk one at a time, so a page boundary lands inside the pair --
    // the case a `(path, key)` cursor silently skips.
    let mut seen: Vec<(String, String)> = Vec::new();
    let mut after: Option<unfurl_git_sync::Cursor> = None;
    for _ in 0..50 {
        let page = sync
            .find_records(&RecordQuery {
                path: Some("/repositories".into()),
                after: after.clone(),
                limit: Some(1),
                ..Default::default()
            })
            .await
            .expect("page");
        let Some(last) = page.last() else { break };
        after = Some(unfurl_git_sync::Cursor {
            path: last.path.clone(),
            key: last.key.clone(),
            file_path: Some(last.file_path.clone()),
            worktree_id: Some(last.worktree_id),
        });
        seen.extend(page.iter().map(|r| (r.file_path.clone(), r.key.clone())));
    }

    let both: Vec<&(String, String)> = seen.iter().filter(|(_, k)| k == dup).collect();
    assert_eq!(
        both.len(),
        2,
        "both files' records must survive the walk, got {both:?}"
    );
    assert!(both.iter().any(|(f, _)| f == "cloudmap.yaml"), "{both:?}");
    assert!(both.iter().any(|(f, _)| f == "second.yaml"), "{both:?}");

    // ...and no record is returned twice.
    let mut sorted = seen.clone();
    sorted.sort();
    sorted.dedup();
    assert_eq!(sorted.len(), seen.len(), "a record repeated: {seen:?}");
}

crud_test!(
    paging_spans_duplicate_keys_across_files,
    run_paging_spans_duplicate_keys_across_files
);

/// With `whole_groups`, a coarse `(path, key)` cursor walks losslessly:
/// the page overshoots `limit` rather than splitting a group, so
/// resuming past that `(path, key)` can never skip a straggler.
async fn run_whole_groups_keeps_a_coarse_cursor_lossless(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");
    let dup = "git://example.com/same-key.git";
    for file in ["cloudmap.yaml", "second.yaml"] {
        sync.upsert_record(
            Some(file),
            "/repositories",
            dup,
            serde_json::json!({"name": file}),
            None,
        )
        .await
        .expect("write");
    }

    // limit = 1, so every page boundary lands inside the duplicated pair.
    let mut seen: Vec<(String, String)> = Vec::new();
    let mut after: Option<unfurl_git_sync::Cursor> = None;
    let mut overshot = false;
    for _ in 0..50 {
        let page = sync
            .find_records(&RecordQuery {
                path: Some("/repositories".into()),
                after: after.clone(),
                limit: Some(1),
                whole_groups: true,
                ..Default::default()
            })
            .await
            .expect("page");
        let Some(last) = page.last() else { break };
        if page.len() > 1 {
            overshot = true;
        }
        // Deliberately coarse -- the granularity the cloudmap document
        // model and the server's page token can express.
        after = Some(unfurl_git_sync::Cursor::new(
            last.path.clone(),
            last.key.clone(),
        ));
        seen.extend(page.iter().map(|r| (r.file_path.clone(), r.key.clone())));
    }

    assert!(
        overshot,
        "the duplicated pair must arrive as one page over the limit: {seen:?}"
    );
    let both: Vec<&(String, String)> = seen.iter().filter(|(_, k)| k == dup).collect();
    assert_eq!(
        both.len(),
        2,
        "no half of the group may be dropped: {both:?}"
    );
    let mut sorted = seen.clone();
    sorted.sort();
    sorted.dedup();
    assert_eq!(sorted.len(), seen.len(), "a record repeated: {seen:?}");

    // Without it, the same walk silently loses one -- the behaviour this
    // flag exists to avoid.
    let lossy = sync
        .find_records(&RecordQuery {
            path: Some("/repositories".into()),
            limit: Some(1),
            ..Default::default()
        })
        .await
        .expect("page");
    assert_eq!(lossy.len(), 1, "limit is a hard cap without whole_groups");
}

crud_test!(
    whole_groups_keeps_a_coarse_cursor_lossless,
    run_whole_groups_keeps_a_coarse_cursor_lossless
);

// ---------------------------------------------------------------------------
// shared version sequence
// ---------------------------------------------------------------------------

/// Worktrees in one family draw from one sequence, so a version means
/// the same row wherever it turns up.
///
/// A fork or draft has no constructor yet, so the family link is made
/// directly. What is under test is the resolution and the draw, not how
/// a member comes to exist.
#[tokio::test]
async fn a_family_shares_one_version_sequence() {
    let (tmp, db) = file_backed_fixture().await;
    git(
        tmp.path(),
        &[
            "remote",
            "add",
            "origin",
            "https://unfurl.cloud/onecommons/cloudmap.git",
        ],
    );
    let upstream = open_at(tmp.path(), &db).await;
    upstream.update_from_working_dir().await.expect("sync");
    let first = upstream
        .upsert_record(
            Some("cloudmap.yaml"),
            "/repositories",
            "upstream-write",
            serde_json::json!({"name": "u"}),
            None,
        )
        .await
        .expect("write")
        .version;

    // A second checkout of the same repo under a different remote spelling
    // would be the same worktree, so use a distinct one and adopt it into
    // the first's family, the way a fork would be.
    let (tmp2, _) = file_backed_fixture().await;
    git(
        tmp2.path(),
        &[
            "remote",
            "add",
            "origin",
            "https://unfurl.cloud/someone/cloudmap-fork.git",
        ],
    );
    let fork = open_at(tmp2.path(), &db).await;
    let pool = sqlx::SqlitePool::connect(&db).await.expect("connect");
    sqlx::query(
        "UPDATE worktree SET family_id = (SELECT COALESCE(family_id, id) FROM worktree \
         WHERE origin = 'unfurl.cloud/onecommons/cloudmap') \
         WHERE origin = 'unfurl.cloud/someone/cloudmap-fork'",
    )
    .execute(&pool)
    .await
    .expect("join the family");

    // Re-open so the cached family is re-resolved.
    drop(fork);
    let fork = open_at(tmp2.path(), &db).await;
    fork.update_from_working_dir().await.expect("sync");
    let forked = fork
        .upsert_record(
            Some("cloudmap.yaml"),
            "/repositories",
            "fork-write",
            serde_json::json!({"name": "f"}),
            None,
        )
        .await
        .expect("write")
        .version;

    assert!(
        forked > first,
        "the fork must continue the family's sequence, not restart: \
         upstream stamped {first}, fork stamped {forked}"
    );

    // And the upstream carries on above the fork rather than reissuing.
    let after = upstream
        .upsert_record(
            Some("cloudmap.yaml"),
            "/repositories",
            "upstream-again",
            serde_json::json!({"name": "u2"}),
            None,
        )
        .await
        .expect("write")
        .version;
    assert!(
        after > forked,
        "a version must never be handed out twice in a family: \
         fork stamped {forked}, upstream then stamped {after}"
    );

    // One sequence row backs both.
    let seqs: Vec<(i64,)> = sqlx::query_as("SELECT worktree_id FROM version_seq ORDER BY 1")
        .fetch_all(&pool)
        .await
        .expect("query");
    assert_eq!(seqs.len(), 2, "one per family root, forks reuse: {seqs:?}");
}

/// A batch takes its versions in one contiguous block.
async fn run_batch_allocates_a_contiguous_range(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");
    let outcome = sync
        .apply_batch(
            vec![upsert_op("a"), upsert_op("b"), upsert_op("c")],
            true,
            None,
        )
        .await
        .expect("batch");
    let versions: Vec<i64> = outcome.applied.iter().map(|a| a.outcome.version).collect();
    assert_eq!(versions.len(), 3);
    for pair in versions.windows(2) {
        assert_eq!(
            pair[1],
            pair[0] + 1,
            "a batch's range must be gapless so the rollup can name it: {versions:?}"
        );
    }

    // And the batch must *consume* the range it numbered within, not
    // just count off it: a block draw that advanced the counter by less
    // than it handed out would reissue these to the next writer.
    let after = sync
        .upsert_record(
            Some("cloudmap.yaml"),
            "/repositories",
            "after-batch",
            serde_json::json!({"name": "after"}),
            None,
        )
        .await
        .expect("write")
        .version;
    assert!(
        after > *versions.last().expect("non-empty"),
        "the next write must land past the batch: batch took {versions:?}, then {after}"
    );
}

crud_test!(
    batch_allocates_a_contiguous_range,
    run_batch_allocates_a_contiguous_range
);

/// A fork's commits name the upstream as their family, so a reader can
/// tell its ranges came from the sequence it is reconstructing even
/// though the origin differs.
#[tokio::test]
async fn a_fork_records_its_family_in_the_rollup() {
    let (tmp, db) = file_backed_fixture().await;
    git(
        tmp.path(),
        &[
            "remote",
            "add",
            "origin",
            "https://unfurl.cloud/onecommons/cloudmap.git",
        ],
    );
    let upstream = open_at(tmp.path(), &db).await;
    upstream.update_from_working_dir().await.expect("sync");

    let (tmp2, _) = file_backed_fixture().await;
    git(
        tmp2.path(),
        &[
            "remote",
            "add",
            "origin",
            "https://unfurl.cloud/someone/cloudmap-fork.git",
        ],
    );
    let fork = open_at(tmp2.path(), &db).await;
    drop(fork);
    let pool = sqlx::SqlitePool::connect(&db).await.expect("connect");
    sqlx::query(
        "UPDATE worktree SET family_id = (SELECT COALESCE(family_id, id) FROM worktree \
         WHERE origin = 'unfurl.cloud/onecommons/cloudmap') \
         WHERE origin = 'unfurl.cloud/someone/cloudmap-fork'",
    )
    .execute(&pool)
    .await
    .expect("join the family");

    let fork = open_at(tmp2.path(), &db).await;
    fork.update_from_working_dir().await.expect("sync");
    fork.apply_batch(
        vec![upsert_op("from-the-fork")],
        true,
        Some(TxnMeta {
            author: Some("Ada <ada@example.com>".into()),
            message: Some("Edit on a fork".into()),
        }),
    )
    .await
    .expect("batch");
    fork.commit_repository("Update cloudmap")
        .await
        .expect("commit")
        .expect("dirty");

    let body = head_commit_body(tmp2.path());
    let parsed = unfurl_git_sync::parse_commit_rollup(&body)
        .expect("parses")
        .expect("a git-sync commit");
    assert_eq!(
        parsed.origin.as_deref(),
        Some("unfurl.cloud/someone/cloudmap-fork"),
        "origin names the writer: {body}"
    );
    assert_eq!(
        parsed.family.as_deref(),
        Some("unfurl.cloud/onecommons/cloudmap"),
        "family names the sequence, which is the upstream's: {body}"
    );
    assert_ne!(
        parsed.family, parsed.origin,
        "this is the case origin alone cannot decide: {body}"
    );
}

// ---------------------------------------------------------------------------
// json5 / jsonc
// ---------------------------------------------------------------------------

/// A cloudmap written as JSON5 -- comments, trailing commas, unquoted
/// keys, single quotes -- is read, and a write to it produces a file
/// that still parses.
#[tokio::test]
async fn json5_and_jsonc_round_trip() {
    let tmp = tempfile::tempdir().expect("tempdir");
    // JSONC is JSON plus comments and trailing commas; JSON5 adds the
    // unquoted keys and single quotes. One parser covers both, so each
    // file leans on the part of the syntax its extension implies.
    let jsonc = br#"{
  // which cloudmap this is
  "apiVersion": "unfurl/v1alpha1",
  "kind": "CloudMap",
  "repositories": {
    "git://example.com/from-jsonc.git": {
      "name": "from-jsonc", /* trailing comma next */
      "path": "in/jsonc",
    },
  },
}"#;
    let json5 = br#"{
  apiVersion: 'unfurl/v1alpha1',
  kind: 'CloudMap',
  repositories: {
    'git://example.com/from-json5.git': { name: 'from-json5', path: 'in/json5' },
  },
}"#;
    unfurl_git_sync::git::init_with_files(
        tmp.path(),
        &[
            ("a.jsonc".to_string(), jsonc.to_vec()),
            ("b.json5".to_string(), json5.to_vec()),
        ],
        "initial",
    )
    .expect("init repo");

    let db = format!("sqlite://{}?mode=rwc", tmp.path().join("sync.db").display());
    let sync = open_at(tmp.path(), &db).await;
    sync.update_from_working_dir().await.expect("sync");

    for (file, key) in [
        ("a.jsonc", "git://example.com/from-jsonc.git"),
        ("b.json5", "git://example.com/from-json5.git"),
    ] {
        let rec = sync
            .get_record(file, "/repositories", key)
            .await
            .expect("get")
            .unwrap_or_else(|| panic!("{file} should have been read"));
        assert_eq!(rec.json["path"], format!("in/{}", &file[2..]));
    }

    // Writing back produces something that parses again -- which is the
    // point, since the emitted syntax is plain JSON rather than the
    // JSON5 that came in.
    sync.upsert_record(
        Some("b.json5"),
        "/repositories",
        "git://example.com/added.git",
        serde_json::json!({"name": "added"}),
        None,
    )
    .await
    .expect("write");
    sync.save_changes().await.expect("save");

    let text = std::fs::read_to_string(tmp.path().join("b.json5")).expect("read");
    let reparsed: serde_json::Value = json5::from_str(&text).expect("still valid json5");
    assert!(
        reparsed["repositories"]
            .get("git://example.com/added.git")
            .is_some(),
        "{text}"
    );
    // Strict JSON too, so a `.jsonc`/`.json5` file stays readable by a
    // plain JSON parser after a rewrite.
    serde_json::from_str::<serde_json::Value>(&text).expect("also valid json");

    // Re-reading sees the write, so the extension still resolves.
    let sync = open_at(tmp.path(), &db).await;
    sync.update_from_working_dir().await.expect("resync");
    assert!(sync
        .get_record("b.json5", "/repositories", "git://example.com/added.git")
        .await
        .expect("get")
        .is_some());
}

/// Comments do not survive a rewrite -- the document round-trips through
/// a `serde_json::Value`, which cannot hold them. Pinned so the loss is
/// a known property rather than a surprise.
#[tokio::test]
async fn a_rewrite_drops_comments() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let jsonc = br#"{
  // this comment cannot survive
  "apiVersion": "unfurl/v1alpha1",
  "kind": "CloudMap",
  "repositories": {"git://example.com/x.git": {"name": "x"}}
}"#;
    unfurl_git_sync::git::init_with_files(
        tmp.path(),
        &[("c.jsonc".to_string(), jsonc.to_vec())],
        "initial",
    )
    .expect("init repo");
    let db = format!("sqlite://{}?mode=rwc", tmp.path().join("sync.db").display());
    let sync = open_at(tmp.path(), &db).await;
    sync.update_from_working_dir().await.expect("sync");
    sync.upsert_record(
        Some("c.jsonc"),
        "/repositories",
        "git://example.com/x.git",
        serde_json::json!({"name": "changed"}),
        None,
    )
    .await
    .expect("write");
    sync.save_changes().await.expect("save");

    let text = std::fs::read_to_string(tmp.path().join("c.jsonc")).expect("read");
    assert!(
        !text.contains("cannot survive"),
        "comments are dropped, same as for yaml: {text}"
    );
    assert!(text.contains("changed"), "{text}");
}

/// A plain `.json` file with comments and a trailing comma is read --
/// most JSON-with-comments in the wild has a `.json` extension -- and
/// the sync reports that it needed the lenient parser.
#[tokio::test]
async fn a_dot_json_file_may_use_json5_syntax_and_says_so() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let commented = br#"{
  // a plain .json with a comment, as tsconfig.json and friends have
  "apiVersion": "unfurl/v1alpha1",
  "kind": "CloudMap",
  "repositories": {
    "git://example.com/loose.git": {"name": "loose"},
  }
}"#;
    let strict = br#"{"apiVersion": "unfurl/v1alpha1", "kind": "CloudMap",
  "repositories": {"git://example.com/strict.git": {"name": "strict"}}}"#;
    unfurl_git_sync::git::init_with_files(
        tmp.path(),
        &[
            ("loose.json".to_string(), commented.to_vec()),
            ("strict.json".to_string(), strict.to_vec()),
        ],
        "initial",
    )
    .expect("init repo");

    let db = format!("sqlite://{}?mode=rwc", tmp.path().join("sync.db").display());
    let sync = open_at(tmp.path(), &db).await;
    let stats = sync.update_from_working_dir().await.expect("sync");

    assert!(sync
        .get_record("loose.json", "/repositories", "git://example.com/loose.git")
        .await
        .expect("get")
        .is_some());
    // Only the one that needed it is counted -- the strict file must not
    // be reported as lenient, or the signal says nothing.
    assert_eq!(
        stats.files_needing_json5, 1,
        "exactly the commented file: {stats:?}"
    );
}

/// A `.json` that is broken rather than merely lenient reports the JSON
/// error, not a JSON5 one about a file nobody meant to be JSON5.
#[tokio::test]
async fn a_broken_dot_json_reports_the_json_error() {
    let tmp = tempfile::tempdir().expect("tempdir");
    unfurl_git_sync::git::init_with_files(
        tmp.path(),
        &[("bad.json".to_string(), b"{\"kind\": ".to_vec())],
        "initial",
    )
    .expect("init repo");
    let db = format!("sqlite://{}?mode=rwc", tmp.path().join("sync.db").display());
    let sync = open_at(tmp.path(), &db).await;
    let err = sync
        .update_from_working_dir()
        .await
        .expect_err("a truncated document cannot parse either way");
    assert!(
        matches!(err, unfurl_git_sync::Error::Json { .. }),
        "expected the strict parser's error, got {err:?}"
    );
}

// ---------------------------------------------------------------------------
// blob hashing
// ---------------------------------------------------------------------------

/// The OID matches what git itself computes, and hashing does not write
/// an object.
#[tokio::test]
async fn hashing_a_blob_matches_git_and_writes_nothing() {
    let (tmp, db) = file_backed_fixture().await;
    // Content that exists nowhere in the repository's history, so if the
    // sync stores it while hashing, `cat-file -e` will find it.
    let novel = "kind: NotACloudMap\nunique: never-committed-anywhere\n";
    std::fs::write(tmp.path().join("scratch.yaml"), novel).expect("write");
    git(tmp.path(), &["add", "scratch.yaml"]);

    let expected = std::process::Command::new("git")
        .args(["hash-object", "-t", "blob", "--stdin"])
        .current_dir(tmp.path())
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .and_then(|mut c| {
            use std::io::Write as _;
            c.stdin
                .as_mut()
                .expect("stdin")
                .write_all(novel.as_bytes())?;
            c.wait_with_output()
        })
        .expect("git hash-object");
    let expected = String::from_utf8_lossy(&expected.stdout).trim().to_string();

    let repo = unfurl_git_sync::git::open_repo(tmp.path()).expect("open");
    let got = unfurl_git_sync::git::blob_oid_for_bytes(&repo, novel.as_bytes());
    assert_eq!(got.to_string(), expected, "must agree with git's own hash");

    // Hashing is pure: the object must not have been stored. `git add`
    // above does store it, so check against a *second* content that was
    // never staged.
    let unstaged = b"kind: NotACloudMap\nunique: never-staged-either\n";
    let oid = unfurl_git_sync::git::blob_oid_for_bytes(&repo, unstaged);
    let out = std::process::Command::new("git")
        .args(["cat-file", "-e", &oid.to_string()])
        .current_dir(tmp.path())
        .output()
        .expect("git cat-file");
    assert!(
        !out.status.success(),
        "hashing must not store the object; {oid} exists in the odb"
    );

    // And a sync of a dirty file leaves no stray object behind either.
    let dirty = "apiVersion: unfurl/v1alpha1\nkind: CloudMap\nrepositories:\n  \
                 git://example.com/dirty.git: {name: dirty}\n";
    std::fs::write(tmp.path().join("cloudmap.yaml"), dirty).expect("write");
    let sync = open_at(tmp.path(), &db).await;
    sync.update_from_working_dir().await.expect("sync");
    let dirty_oid = unfurl_git_sync::git::blob_oid_for_bytes(&repo, dirty.as_bytes());
    let out = std::process::Command::new("git")
        .args(["cat-file", "-e", &dirty_oid.to_string()])
        .current_dir(tmp.path())
        .output()
        .expect("git cat-file");
    assert!(
        !out.status.success(),
        "a sync must not store the blobs it hashes; {dirty_oid} was written"
    );
    // The dirty file's records still landed, so the sync did its job.
    assert!(sync
        .get_record(
            "cloudmap.yaml",
            "/repositories",
            "git://example.com/dirty.git"
        )
        .await
        .expect("get")
        .is_some());
}

// ---------------------------------------------------------------------------
// source_oid conflict detection
// ---------------------------------------------------------------------------

/// An edit made to the file outside this database stops a write rather
/// than being overwritten by rows that predate it.
async fn run_a_hand_edit_blocks_a_write(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "staged",
        serde_json::json!({"name": "staged"}),
        None,
    )
    .await
    .expect("write");

    // Someone edits an existing record directly, as a person or another
    // tool would.
    let path = tmp.path().join("cloudmap.yaml");
    let before = std::fs::read_to_string(&path).expect("read");
    let edited = before.replace("name: dashboard", "name: dashboard-by-hand");
    assert_ne!(
        edited, before,
        "the fixture should contain the edited value"
    );
    std::fs::write(&path, &edited).expect("write");

    let err = sync
        .write_file("cloudmap.yaml")
        .await
        .expect_err("the database no longer describes this file");
    assert!(
        matches!(err, Error::FileChanged { .. }),
        "expected FileChanged, got {err:?}"
    );
    assert_eq!(
        std::fs::read_to_string(&path).expect("read"),
        edited,
        "a refused write must not have touched the file"
    );

    // Taking the edit in clears the conflict, and the edit is what the
    // database now holds.
    sync.update_from_working_dir().await.expect("resync");
    let rec = sync
        .get_record(
            "cloudmap.yaml",
            "/repositories",
            "git://unfurl.cloud/feb20a/dashboard.git",
        )
        .await
        .expect("get")
        .expect("present");
    assert_eq!(rec.json["name"], "dashboard-by-hand");

    // Note what re-syncing costs: it takes the file as the truth, so the
    // staged record -- written to the database but never to disk -- is
    // gone. "Re-sync and retry" resolves the conflict but is not a
    // lossless recovery for work that had not reached the file yet.
    assert!(
        sync.get_record("cloudmap.yaml", "/repositories", "staged")
            .await
            .expect("get")
            .is_none(),
        "a re-sync drops records the file does not have"
    );
}

/// Consecutive writes do not conflict with their own output.
async fn run_repeated_writes_do_not_self_conflict(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");
    for name in ["first", "second", "third"] {
        sync.upsert_record(
            Some("cloudmap.yaml"),
            "/repositories",
            name,
            serde_json::json!({"name": name}),
            None,
        )
        .await
        .expect("write");
        sync.write_file("cloudmap.yaml")
            .await
            .unwrap_or_else(|e| panic!("write after {name} must not conflict: {e:?}"));
    }
}

/// A file the database has never parsed has nothing to contradict, so a
/// write to it proceeds.
async fn run_an_unscanned_file_has_no_conflict(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");
    sync.upsert_record(
        Some("brand-new.yaml"),
        "/repositories",
        "fresh",
        serde_json::json!({"name": "fresh"}),
        None,
    )
    .await
    .expect("write");
    sync.write_file("brand-new.yaml")
        .await
        .expect("a synthesised document has no recorded source")
        .expect("written");
    assert!(tmp.path().join("brand-new.yaml").exists());
}

crud_test!(a_hand_edit_blocks_a_write, run_a_hand_edit_blocks_a_write);
crud_test!(
    repeated_writes_do_not_self_conflict,
    run_repeated_writes_do_not_self_conflict
);
crud_test!(
    an_unscanned_file_has_no_conflict,
    run_an_unscanned_file_has_no_conflict
);

/// One unwritable file does not stop the others, and the caller learns
/// which was which.
async fn run_save_reports_each_file(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir().await.expect("update");
    // Two files with pending records; one of them gets edited behind the
    // database's back so its write will be refused.
    for (file, key) in [("cloudmap.yaml", "in-main"), ("second.yaml", "in-second")] {
        sync.upsert_record(
            Some(file),
            "/repositories",
            key,
            serde_json::json!({"name": key}),
            None,
        )
        .await
        .expect("write");
    }
    // `second.yaml` was created by the record write, so it has no
    // recorded source and cannot conflict. Give `cloudmap.yaml` the
    // conflict instead.
    let path = tmp.path().join("cloudmap.yaml");
    let text = std::fs::read_to_string(&path).expect("read");
    std::fs::write(
        &path,
        text.replace("name: dashboard", "name: dashboard-by-hand"),
    )
    .expect("write");

    let outcome = sync.save_changes().await.expect("save proceeds");
    assert_eq!(outcome.failed.len(), 1, "{outcome:?}");
    assert_eq!(outcome.failed[0].file_path, "cloudmap.yaml");
    assert!(matches!(outcome.failed[0].error, Error::FileChanged { .. }));
    // The other file was still written -- one failure must not strand it
    // unreported, which is what failing fast used to do.
    assert_eq!(
        outcome.written,
        vec![tmp.path().join("second.yaml")],
        "{outcome:?}"
    );
    assert!(tmp.path().join("second.yaml").exists());

    // And a commit refuses while anything is unwritten, rather than
    // capturing a half-applied state.
    let err = sync
        .commit_repository("should not commit")
        .await
        .expect_err("a partial save must not be committed");
    assert!(matches!(err, Error::FileChanged { .. }), "{err:?}");
}

crud_test!(save_reports_each_file, run_save_reports_each_file);

/// The types `save_changes` returns are nameable from outside the crate.
/// Without the re-export a caller cannot write its return type, which a
/// build inside the crate never notices.
#[test]
fn save_outcome_types_are_public() {
    fn _takes(_: unfurl_git_sync::SaveOutcome, _: unfurl_git_sync::SaveFailure) {}
}

// ---------------------------------------------------------------------------
// comment preservation
// ---------------------------------------------------------------------------

/// A write keeps the comments it did not touch.
async fn run_a_write_keeps_comments_elsewhere(sync: &SyncedRepo, tmp: &TempDir) {
    let path = tmp.path().join("cloudmap.yaml");
    let original = std::fs::read_to_string(&path).expect("read");
    // Comments in the three places people put them: above the document,
    // above a section, and inside one.
    let commented = format!(
        "# what this cloudmap is for\n{}",
        original
            .replacen("repositories:", "# the repos we track\nrepositories:", 1)
            .replacen("artifacts:", "# and the artifacts\nartifacts:", 1)
    );
    std::fs::write(&path, &commented).expect("write");

    sync.update_from_working_dir().await.expect("update");
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://example.com/added.git",
        serde_json::json!({"name": "added"}),
        None,
    )
    .await
    .expect("write");
    sync.save_changes().await.expect("save");

    let after = std::fs::read_to_string(&path).expect("read");
    assert!(
        after.starts_with("# what this cloudmap is for\n"),
        "header comment lost:\n{after}"
    );
    assert!(after.contains("# the repos we track\n"), "{after}");
    assert!(
        after.contains("# and the artifacts\n"),
        "a comment about an untouched section lost:\n{after}"
    );
    // ...and the write itself landed, in a document that still parses.
    let parsed: serde_json::Value = serde_saphyr::from_str(&after).expect("valid yaml");
    assert_eq!(
        parsed["repositories"]["git://example.com/added.git"]["name"],
        "added"
    );
    assert_eq!(parsed["kind"], "CloudMap");
}

/// Rewriting a section drops the comments *inside* it -- the records it
/// annotates are re-sorted, so there is nowhere to put them back. Pinned
/// so the boundary of what survives is stated rather than assumed.
async fn run_comments_inside_a_rewritten_section_are_lost(sync: &SyncedRepo, tmp: &TempDir) {
    let path = tmp.path().join("cloudmap.yaml");
    let original = std::fs::read_to_string(&path).expect("read");
    let commented = original.replacen(
        "  git://unfurl.cloud/onecommons/std.git:",
        "  # a note about std\n  git://unfurl.cloud/onecommons/std.git:",
        1,
    );
    assert_ne!(commented, original, "fixture should contain that key");
    std::fs::write(&path, &commented).expect("write");

    sync.update_from_working_dir().await.expect("update");
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://example.com/added.git",
        serde_json::json!({"name": "added"}),
        None,
    )
    .await
    .expect("write");
    sync.save_changes().await.expect("save");

    let after = std::fs::read_to_string(&path).expect("read");
    assert!(!after.contains("a note about std"), "{after}");
}

crud_test!(
    a_write_keeps_comments_elsewhere,
    run_a_write_keeps_comments_elsewhere
);
crud_test!(
    comments_inside_a_rewritten_section_are_lost,
    run_comments_inside_a_rewritten_section_are_lost
);
