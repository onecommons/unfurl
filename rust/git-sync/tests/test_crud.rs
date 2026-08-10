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
use unfurl_git_sync::{BatchOp, CommitRef, Error, JsonQuery, SyncedRepo};

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
    let written = sync.save_changes().await.expect("save_changes");
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

async fn run_find_records_type_filter(sync: &SyncedRepo, _tmp: &TempDir) {
    // `type_names` matches records whose `type` typeRef object
    // declares one of the given names as a key. Exact names only —
    // subtype expansion is the caller's job.
    sync.update_from_working_dir().await.expect("update");

    let pipelines = sync
        .find_records(
            None,
            None,
            None,
            false,
            None,
            Some(vec!["cloudmap.artifacts.ci.GitLabPipeline".into()]),
            None,
        )
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
        .find_records(
            None,
            Some("/services".into()),
            None,
            false,
            None,
            Some(vec![
                "Odoo@unfurl.cloud/onecommons/blueprints/odoo".into(),
                "no.such.Type".into(),
            ]),
            None,
        )
        .await
        .expect("multi type filter");
    assert_eq!(multi.len(), 1);
    assert_eq!(multi[0].key, "https://example.com/oodo");

    // Unknown name matches nothing.
    let none = sync
        .find_records(
            None,
            None,
            None,
            false,
            None,
            Some(vec!["no.such.Type".into()]),
            None,
        )
        .await
        .expect("unknown type");
    assert!(none.is_empty());

    // An empty name list is treated as "no filter", not "match none".
    let all = sync
        .find_records(None, None, None, false, None, Some(Vec::new()), None)
        .await
        .expect("empty type list");
    let unfiltered = sync
        .find_records(None, None, None, false, None, None, None)
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
        .find_records(
            None,
            Some(parent_path.into()),
            Some(parent_key.into()),
            false,
            None,
            None,
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
            None,
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
            None,
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
        .find_records(None, Some(parent_path.into()), None, true, None, None, None)
        .await
        .expect("find_records no key, alias=true");
    let any_artifact_no_alias = sync
        .find_records(
            None,
            Some(parent_path.into()),
            None,
            false,
            None,
            None,
            None,
        )
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
            None,
            Some(start_path.into()),
            Some(start_key.into()),
            false,
            0,
            None,
            Vec::new(),
            None,
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
            Vec::new(),
            None,
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
            None,
            Some(start_path.into()),
            Some(start_key.into()),
            false,
            1,
            None,
            Vec::new(),
            None,
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
    let outcome = sync.apply_batch(ops, true).await.expect("apply_batch");
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
    let outcome = sync.apply_batch(ops, true).await.expect("apply_batch");
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
    let outcome = sync.apply_batch(ops, false).await.expect("apply_batch");
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
            sync.find_records(None, None, None, false, None, None, Some(q))
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
            sync.find_records(None, None, None, false, None, None, Some(q))
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
            sync.find_records(None, None, None, false, None, None, Some(q))
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
