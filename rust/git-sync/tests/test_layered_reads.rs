// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A read across two branches of one family: a row superseded in either's
//! view is left out, so a record one changed shows only its change, and
//! one both changed comes back once from each (docs/branch-segments.md
//! §4.12), on both backends.

mod common;

use common::{crud_test, DASHBOARD};
use tempfile::TempDir;
use unfurl_git_sync::{FormatRegistry, RecordQuery, ScanOptions, SyncedRepo, WorktreeFilter};

fn git(dir: &std::path::Path, args: &[&str]) -> String {
    let out = std::process::Command::new("git")
        .args(["-c", "user.name=t", "-c", "user.email=t@example.com"])
        .args(["-c", "commit.gpgsign=false"])
        .args(args)
        .current_dir(dir)
        .output()
        .expect("git");
    assert!(out.status.success(), "git {args:?}: {out:?}");
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

/// A branch of `sync`'s repository at a commit git-sync made, so it joins
/// `sync`'s family, opened on the same database and scanned. Its directory
/// is held by the returned `TempDir`.
async fn a_branch(sync: &SyncedRepo, tmp: &TempDir) -> (SyncedRepo, TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://example.com/base.git",
        serde_json::json!({"name": "base"}),
        None,
        false,
    )
    .await
    .expect("write");
    let base = sync
        .commit_repository("base", Default::default())
        .await
        .expect("commit")
        .commit
        .expect("a commit");
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("feature");
    let at = path.to_str().expect("utf8");
    git(
        tmp.path(),
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            "feature",
            at,
            &base.to_string(),
        ],
    );
    let branch = SyncedRepo::open_on(
        &path,
        sync.db().clone(),
        FormatRegistry::with_builtins(),
        unfurl_git_sync::git::Trust::Full,
    )
    .await
    .expect("open");
    branch
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    (branch, dir)
}

async fn name(sync: &SyncedRepo, value: &str) {
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": value}),
        None,
        false,
    )
    .await
    .expect("write");
}

async fn delete(sync: &SyncedRepo) {
    sync.delete_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        None,
        false,
    )
    .await
    .expect("delete");
}

/// The dashboard's live rows in a read across both: `(worktree, name)`,
/// sorted.
async fn read_across(a: &SyncedRepo, b: &SyncedRepo) -> Vec<(i64, String)> {
    let query = RecordQuery {
        worktrees: Some(WorktreeFilter {
            ids: Some(vec![a.worktree_id(), b.worktree_id()]),
            ..Default::default()
        }),
        path: Some("/repositories".into()),
        key: Some(DASHBOARD.into()),
        ..Default::default()
    };
    let mut rows: Vec<(i64, String)> = a
        .find_records(&query)
        .await
        .expect("find")
        .into_iter()
        .map(|r| match r.deleted {
            true => (r.worktree_id, "deleted".to_string()),
            false => (
                r.worktree_id,
                r.json["name"].as_str().unwrap_or("").to_string(),
            ),
        })
        .collect();
    rows.sort();
    rows
}

/// A record only the branch changed shows only the branch's row: main's,
/// which the branch's edit supersedes, isn't a copy of it.
async fn a_change_on_the_branch_alone_is_its_row(sync: &SyncedRepo, tmp: &TempDir) {
    let (branch, _dir) = a_branch(sync, tmp).await;
    name(&branch, "feature").await;
    assert_eq!(
        read_across(sync, &branch).await,
        [(branch.worktree_id(), "feature".into())]
    );
}
crud_test!(a_change_on_the_branch_alone_is_its_row);

/// A record only main changed shows only main's row: the branch's, which
/// it inherited from before, is stale.
async fn a_change_on_main_alone_is_its_row(sync: &SyncedRepo, tmp: &TempDir) {
    let (branch, _dir) = a_branch(sync, tmp).await;
    name(sync, "main").await;
    assert_eq!(
        read_across(sync, &branch).await,
        [(sync.worktree_id(), "main".into())]
    );
}
crud_test!(a_change_on_main_alone_is_its_row);

/// A record both changed comes back once from each.
async fn a_change_on_both_is_two_copies(sync: &SyncedRepo, tmp: &TempDir) {
    let (branch, _dir) = a_branch(sync, tmp).await;
    name(sync, "main").await;
    name(&branch, "feature").await;
    assert_eq!(
        read_across(sync, &branch).await,
        [
            (sync.worktree_id(), "main".into()),
            (branch.worktree_id(), "feature".into())
        ]
    );
}
crud_test!(a_change_on_both_is_two_copies);

/// A record neither changed is the same row in each: nothing is dropped.
async fn an_unchanged_record_is_the_same_row_in_each(sync: &SyncedRepo, tmp: &TempDir) {
    let (branch, _dir) = a_branch(sync, tmp).await;
    let rows = read_across(sync, &branch).await;
    assert_eq!(rows.len(), 2, "{rows:?}");
    assert!(rows.iter().all(|(_, n)| n == "dashboard"), "{rows:?}");
}
crud_test!(an_unchanged_record_is_the_same_row_in_each);

/// A record main deleted and the branch left alone is absent, as from a
/// read of main: the branch's copy is stale.
async fn a_deletion_on_main_alone_is_absent(sync: &SyncedRepo, tmp: &TempDir) {
    let (branch, _dir) = a_branch(sync, tmp).await;
    delete(sync).await;
    assert_eq!(read_across(sync, &branch).await, []);
}
crud_test!(a_deletion_on_main_alone_is_absent);

/// A record main deleted and the branch edited is the branch's edit: a
/// read without tombstones shows what's live.
async fn a_deletion_and_an_edit_is_the_edit(sync: &SyncedRepo, tmp: &TempDir) {
    let (branch, _dir) = a_branch(sync, tmp).await;
    delete(sync).await;
    name(&branch, "feature").await;
    assert_eq!(
        read_across(sync, &branch).await,
        [(branch.worktree_id(), "feature".into())]
    );
}
crud_test!(a_deletion_and_an_edit_is_the_edit);

/// A record both deleted is gone.
async fn a_record_both_deleted_is_gone(sync: &SyncedRepo, tmp: &TempDir) {
    let (branch, _dir) = a_branch(sync, tmp).await;
    delete(sync).await;
    delete(&branch).await;
    assert_eq!(read_across(sync, &branch).await, []);
}
crud_test!(a_record_both_deleted_is_gone);

/// Move the dashboard to `other.yaml` on `branch`'s checkout at `dir`,
/// committed in git and scanned: the same record, in another file.
async fn moved(branch: &SyncedRepo, dir: &std::path::Path) {
    let file = dir.join("cloudmap.yaml");
    let text = std::fs::read_to_string(&file).expect("read");
    let doc: serde_json::Value = serde_saphyr::from_str(&text).expect("yaml");
    let record = doc["repositories"][DASHBOARD].clone();
    let mut left = doc.clone();
    left["repositories"]
        .as_object_mut()
        .expect("map")
        .remove(DASHBOARD);
    std::fs::write(&file, serde_saphyr::to_string(&left).expect("yaml")).expect("write");
    let other = serde_json::json!({
        "apiVersion": doc["apiVersion"],
        "kind": doc["kind"],
        "repositories": { DASHBOARD: record },
    });
    std::fs::write(
        dir.join("other.yaml"),
        serde_saphyr::to_string(&other).expect("yaml"),
    )
    .expect("write");
    git(dir, &["add", "-A"]);
    git(dir, &["commit", "-q", "-m", "moved"]);
    branch
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
}

/// The dashboard's rows in a read of `ids`: `(worktree, file, deleted)`.
async fn read_of(sync: &SyncedRepo, ids: Vec<i64>) -> Vec<(i64, String, bool)> {
    let query = RecordQuery {
        worktrees: Some(WorktreeFilter {
            ids: Some(ids),
            ..Default::default()
        }),
        path: Some("/repositories".into()),
        key: Some(DASHBOARD.into()),
        ..Default::default()
    };
    let mut rows: Vec<_> = sync
        .find_records(&query)
        .await
        .expect("find")
        .into_iter()
        .map(|r| (r.worktree_id, r.file_path, r.deleted))
        .collect();
    rows.sort();
    rows
}

/// A record a branch moved to another file is its live row there: the
/// tombstone it left isn't a deletion, alone or beside main.
async fn a_moved_record_is_its_new_row(sync: &SyncedRepo, tmp: &TempDir) {
    let (branch, dir) = a_branch(sync, tmp).await;
    moved(&branch, &dir.path().join("feature")).await;
    let b = branch.worktree_id();
    let moved_row = (b, "other.yaml".to_string(), false);
    assert_eq!(
        read_of(sync, vec![b]).await,
        std::slice::from_ref(&moved_row)
    );
    assert_eq!(
        read_of(sync, vec![sync.worktree_id(), b]).await,
        [moved_row]
    );
}
crud_test!(a_moved_record_is_its_new_row);

/// A facet across both counts what the read shows: the branch's change,
/// not main's row it supersedes.
async fn a_facet_counts_the_branchs_change_alone(sync: &SyncedRepo, tmp: &TempDir) {
    use unfurl_git_sync::{canonical_facet_key, FacetPath, FacetSpec};
    let (branch, _dir) = a_branch(sync, tmp).await;
    name(&branch, "feature").await;
    let query = RecordQuery {
        worktrees: Some(WorktreeFilter {
            ids: Some(vec![sync.worktree_id(), branch.worktree_id()]),
            ..Default::default()
        }),
        path: Some("/repositories".into()),
        key: Some(DASHBOARD.into()),
        ..Default::default()
    };
    let spec = FacetSpec {
        group: FacetPath::new(vec!["name".into()], false).expect("path"),
        columns: vec![],
    };
    let rows = sync.facet_records(&query, &spec).await.expect("facet");
    let groups: Vec<(String, i64)> = rows
        .groups
        .iter()
        .map(|(v, n)| (canonical_facet_key(v), *n))
        .collect();
    assert_eq!((rows.total, groups), (1, vec![("feature".into(), 1)]));
}
crud_test!(a_facet_counts_the_branchs_change_alone);

/// A branch of `sync`'s repository at its HEAD as it is, with nothing
/// committed first, opened on the same database and scanned: it joins
/// `sync`'s family if git-sync made that commit.
async fn a_branch_at_head(sync: &SyncedRepo, tmp: &TempDir) -> (SyncedRepo, TempDir) {
    let head = git(tmp.path(), &["rev-parse", "HEAD"]);
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("feature");
    let at = path.to_str().expect("utf8");
    git(
        tmp.path(),
        &["worktree", "add", "-q", "-b", "feature", at, &head],
    );
    let branch = SyncedRepo::open_on(
        &path,
        sync.db().clone(),
        FormatRegistry::with_builtins(),
        unfurl_git_sync::git::Trust::Full,
    )
    .await
    .expect("open");
    branch
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    (branch, dir)
}

/// Main saves a record unchanged, the branch is made, main commits, which
/// changes nothing, then the branch edits the record: main's copy is the
/// row the edit hides, so it isn't returned beside the edit.
async fn a_no_op_commit_leaves_main_on_the_shared_row(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    // a commit git-sync made, so the branch joins main's family
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "git://example.com/base.git",
        serde_json::json!({"name": "base"}),
        None,
        false,
    )
    .await
    .expect("write");
    sync.commit_repository("base", Default::default())
        .await
        .expect("commit");
    let shown = sync
        .find_records(&RecordQuery {
            path: Some("/repositories".into()),
            key: Some(DASHBOARD.into()),
            ..Default::default()
        })
        .await
        .expect("find");
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        shown[0].json.clone(),
        None,
        false,
    )
    .await
    .expect("save unchanged");
    let (branch, _dir) = a_branch_at_head(sync, tmp).await;
    let committed = sync
        .commit_repository("no-op", Default::default())
        .await
        .expect("commit");
    assert_eq!(committed.commit, None, "nothing changed");
    name(&branch, "edited").await;
    assert_eq!(
        read_across(sync, &branch).await,
        [(branch.worktree_id(), "edited".into())]
    );
}
crud_test!(a_no_op_commit_leaves_main_on_the_shared_row);

/// A client's token from saving a record unchanged still passes after a
/// commit that changes nothing drops that save: the record's version is
/// then the older committed one.
async fn a_no_op_commit_keeps_a_saves_token(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let shown = sync
        .find_records(&RecordQuery {
            path: Some("/repositories".into()),
            key: Some(DASHBOARD.into()),
            ..Default::default()
        })
        .await
        .expect("find");
    let saved = sync
        .upsert_record(
            Some("cloudmap.yaml"),
            "/repositories",
            DASHBOARD,
            shown[0].json.clone(),
            None,
            false,
        )
        .await
        .expect("save unchanged");
    let committed = sync
        .commit_repository("no-op", Default::default())
        .await
        .expect("commit");
    assert_eq!(committed.commit, None, "nothing changed");
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": "edited"}),
        Some(unfurl_git_sync::CommitRef::Pending(saved.version)),
        false,
    )
    .await
    .expect("the save's token still passes");
}
crud_test!(a_no_op_commit_keeps_a_saves_token);

/// The branch edits a record and main saves it unchanged: main's save is a
/// copy of what the branch's edit was made over, so it's hidden too.
async fn mains_unchanged_save_under_a_branchs_edit_is_hidden(sync: &SyncedRepo, tmp: &TempDir) {
    let (branch, _dir) = a_branch(sync, tmp).await;
    name(&branch, "feature").await;
    let shown = sync
        .find_records(&RecordQuery {
            path: Some("/repositories".into()),
            key: Some(DASHBOARD.into()),
            ..Default::default()
        })
        .await
        .expect("find");
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        shown[0].json.clone(),
        None,
        false,
    )
    .await
    .expect("save unchanged");
    assert_eq!(
        read_across(sync, &branch).await,
        [(branch.worktree_id(), "feature".into())]
    );
}
crud_test!(mains_unchanged_save_under_a_branchs_edit_is_hidden);

/// The branch edits a record, main changes it and commits, then a revert
/// made outside the database brings main's earlier value back: that value
/// is what the branch's edit was made over, so it's hidden.
async fn a_value_main_brings_back_under_a_branchs_edit_is_hidden(sync: &SyncedRepo, tmp: &TempDir) {
    let (branch, _dir) = a_branch(sync, tmp).await;
    name(&branch, "feature").await;
    name(sync, "changed").await;
    sync.commit_repository("change", Default::default())
        .await
        .expect("commit");
    git(tmp.path(), &["revert", "--no-edit", "HEAD"]);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert_eq!(
        read_across(sync, &branch).await,
        [(branch.worktree_id(), "feature".into())]
    );
}
crud_test!(a_value_main_brings_back_under_a_branchs_edit_is_hidden);
