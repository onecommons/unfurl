//! Opening a new branch of a tracked origin forks it where its HEAD falls
//! in the family (docs/branch-segments.md §4.5, §4.6); rewriting a
//! branch's HEAD rebuilds its chain and resets its cursors (§4.8, §4.10).

mod common;

use common::*;
use unfurl_git_sync::{Error, RecordQuery, ScanOptions, SyncedRepo, WorktreeFilter};

/// Every record `sync` shows: key → (name, id).
async fn names(sync: &SyncedRepo) -> std::collections::BTreeMap<String, (String, i64)> {
    sync.find_records(&RecordQuery::default())
        .await
        .expect("find")
        .into_iter()
        .map(|r| {
            let name = r.json["name"].as_str().unwrap_or_default().to_string();
            (r.key, (name, r.id))
        })
        .collect()
}

async fn write_and_commit(sync: &SyncedRepo, key: &str) -> String {
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        key,
        serde_json::json!({ "name": key }),
        None,
        false,
    )
    .await
    .expect("write");
    sync.commit_repository(key)
        .await
        .expect("commit")
        .expect("a commit")
}

/// A branch that diverged with a commit the database never saw forks at
/// its merge-base with main, splitting main's segment there, and its
/// first scan brings it up to its HEAD: it shares main's records from
/// before the merge-base, and main's view doesn't change.
#[tokio::test]
async fn a_diverged_branch_forks_at_its_merge_base() {
    let (tmp, db) = file_backed_fixture().await;
    git(
        tmp.path(),
        &["remote", "add", "origin", "https://example.com/fork.git"],
    );
    let main = open_at(tmp.path(), &db).await;
    main.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let base = write_and_commit(&main, "one").await;
    write_and_commit(&main, "two").await;
    let before = names(&main).await;

    let dir = tmp.path().parent().expect("parent").join(format!(
        "{}-feature",
        tmp.path().file_name().expect("name").to_string_lossy()
    ));
    let path = dir.to_str().expect("utf8");
    git(
        tmp.path(),
        &["worktree", "add", "-q", "-b", "feature", path, &base],
    );
    let file = dir.join("cloudmap.yaml");
    let text = std::fs::read_to_string(&file).expect("read");
    std::fs::write(&file, text.replace("name: dashboard", "name: feature")).expect("write");
    git(
        &dir,
        &[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "-am",
            "outside git-sync",
        ],
    );

    let feature = open_at(&dir, &db).await;
    feature
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let got = names(&feature).await;
    assert_eq!(
        got["one"], before["one"],
        "the record from before the merge-base is main's"
    );
    assert!(
        !got.contains_key("two"),
        "main's later record isn't the branch's"
    );
    assert_eq!(
        got[DASHBOARD].0, "feature",
        "the branch's own commit is taken in"
    );
    assert_eq!(
        got[DASHBOARD].1, before[DASHBOARD].1,
        "and continues main's record"
    );
    assert_eq!(names(&main).await, before, "main's view is unchanged");
    std::fs::remove_dir_all(&dir).ok();
}

/// A HEAD reset below the recorded commit rebuilds the chain at it: the
/// record the dropped commit added leaves the view without a tombstone,
/// so a cursor from before the reset is refused, and a re-read resumes
/// from the watermark it took first.
#[tokio::test]
async fn a_reset_head_rebuilds_and_resets_cursors() {
    let (tmp, db) = file_backed_fixture().await;
    let sync = open_at(tmp.path(), &db).await;
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let base = write_and_commit(&sync, "one").await;
    write_and_commit(&sync, "two").await;
    let cursor = sync.watermark(None).await.expect("watermark");

    git(tmp.path(), &["reset", "-q", "--hard", &base]);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan after the reset");

    let got = names(&sync).await;
    assert!(got.contains_key("one"), "the kept commit's record stays");
    assert!(!got.contains_key("two"), "the dropped commit's record goes");
    let stale = RecordQuery {
        since_version: Some(cursor),
        ..Default::default()
    };
    let err = sync.find_records(&stale).await.expect_err("stale find");
    assert!(matches!(err, Error::Reset { .. }), "{err:?}");
    let err = sync
        .list_changes(Some(cursor), false)
        .await
        .expect_err("stale list");
    assert!(matches!(err, Error::Reset { .. }), "{err:?}");

    let resume = sync.watermark(None).await.expect("watermark");
    names(&sync).await;
    let fresh = RecordQuery {
        since_version: Some(resume),
        ..Default::default()
    };
    assert!(
        sync.find_records(&fresh)
            .await
            .expect("fresh find")
            .is_empty(),
        "nothing changed since the re-read"
    );
}

/// `git reset --mixed` keeps the working tree: the scan after the
/// rebuild takes the undone commit's records back in, as edits.
#[tokio::test]
async fn a_mixed_reset_keeps_the_working_trees_records() {
    let (tmp, db) = file_backed_fixture().await;
    let sync = open_at(tmp.path(), &db).await;
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let base = write_and_commit(&sync, "one").await;
    write_and_commit(&sync, "two").await;

    git(tmp.path(), &["reset", "-q", "--mixed", &base]);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan after the reset");
    assert!(
        names(&sync).await.contains_key("two"),
        "the file on disk still holds it"
    );
}

/// A branch reset onto main's later tip rebuilds on main's head: that
/// head closes, so main's later writes stay main's, and the branch takes
/// main's records from its chain, not as edits of its own.
#[tokio::test]
async fn a_branch_reset_onto_main_rebuilds_on_mains_chain() {
    let (tmp, db) = file_backed_fixture().await;
    git(
        tmp.path(),
        &["remote", "add", "origin", "https://example.com/fork.git"],
    );
    let main = open_at(tmp.path(), &db).await;
    main.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let base = write_and_commit(&main, "one").await;

    let dir = tmp.path().parent().expect("parent").join(format!(
        "{}-rebased",
        tmp.path().file_name().expect("name").to_string_lossy()
    ));
    let path = dir.to_str().expect("utf8");
    git(
        tmp.path(),
        &["worktree", "add", "-q", "-b", "feature", path, &base],
    );
    let feature = open_at(&dir, &db).await;
    feature
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    write_and_commit(&feature, "feat").await;
    let tip = write_and_commit(&main, "two").await;

    git(&dir, &["reset", "-q", "--hard", &tip]);
    feature
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan after the reset");
    let got = names(&feature).await;
    assert!(
        !got.contains_key("feat"),
        "the dropped commit's record goes"
    );
    assert_eq!(got, names(&main).await, "the branch shows main's tip");
    assert!(
        feature
            .list_changes(None, false)
            .await
            .expect("pending")
            .is_empty(),
        "main's records are the branch's history, not its edits"
    );

    write_and_commit(&main, "three").await;
    assert!(
        !names(&feature).await.contains_key("three"),
        "main's later write stays main's"
    );
    std::fs::remove_dir_all(&dir).ok();
}

/// A rewritten HEAD whose tree fails a record's validation leaves that
/// record's row as it was, as a scan does, rather than deleting it.
#[tokio::test]
async fn a_rebuild_keeps_a_record_its_tree_rejects() {
    const GITLAB_CI: &str = "git://unfurl.cloud/feb20a/dashboard.git#:.gitlab-ci.yml";
    let (tmp, db) = file_backed_fixture().await;
    let sync = open_at(tmp.path(), &db).await;
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    write_and_commit(&sync, "one").await;
    let good = sync
        .get_record("cloudmap.yaml", "/artifacts", GITLAB_CI)
        .await
        .expect("get")
        .expect("seeded");

    let file = tmp.path().join("cloudmap.yaml");
    let text = std::fs::read_to_string(&file).expect("read");
    let broken = text.replace(
        "  git://unfurl.cloud/feb20a/dashboard.git#:.gitlab-ci.yml:\n    type:\n      cloudmap.artifacts.ci.GitLabPipeline:\n",
        "  git://unfurl.cloud/feb20a/dashboard.git#:.gitlab-ci.yml:\n    type: cloudmap.artifacts.ci.GitLabPipeline\n",
    );
    assert_ne!(
        broken, text,
        "fixture shape changed; the edit matched nothing"
    );
    std::fs::write(&file, broken).expect("write");
    let commit = |args: &[&str]| {
        let mut all = vec!["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q"];
        all.extend_from_slice(args);
        git(tmp.path(), &all);
    };
    commit(&["-am", "outside git-sync"]);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan the rejected record");

    commit(&["--amend", "-m", "rewritten"]);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan after the rewrite");
    let after = sync
        .get_record("cloudmap.yaml", "/artifacts", GITLAB_CI)
        .await
        .expect("get")
        .expect("a rejected record isn't deleted");
    assert_eq!(after.json, good.json);
}

/// A handle's working tree switched to another branch refuses to scan
/// rather than write that branch's files into this one's records.
#[tokio::test]
async fn a_scan_after_switching_branch_is_refused() {
    let (tmp, db) = file_backed_fixture().await;
    let sync = open_at(tmp.path(), &db).await;
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    git(tmp.path(), &["checkout", "-q", "-b", "elsewhere"]);
    let err = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect_err("scan on another branch");
    assert!(
        matches!(&err, Error::BranchChanged { found, .. } if found == "elsewhere"),
        "{err:?}"
    );
}

/// A detached HEAD — a rebase or bisect in progress — holds the scan
/// until a branch is checked out again.
#[tokio::test]
async fn a_scan_on_a_detached_head_waits_for_the_branch() {
    let (tmp, db) = file_backed_fixture().await;
    let sync = open_at(tmp.path(), &db).await;
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    git(tmp.path(), &["checkout", "-q", "--detach"]);
    let err = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect_err("scan while detached");
    assert!(matches!(err, Error::Detached { .. }), "{err:?}");
    git(tmp.path(), &["checkout", "-q", "-"]);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan back on the branch");
}

/// Versions are counted per family, so a cursor over two repositories'
/// worktrees is refused; one repository's is fine.
#[tokio::test]
async fn a_cursor_across_families_is_refused() {
    let (tmp, db) = file_backed_fixture().await;
    let other = tempfile::tempdir().expect("tempdir");
    init_repo_with_fixture(other.path()).await;
    let one = open_at(tmp.path(), &db).await;
    let two = open_at(other.path(), &db).await;
    for sync in [&one, &two] {
        sync.update_from_working_dir(ScanOptions::default())
            .await
            .expect("scan");
    }
    let branch = WorktreeFilter {
        origin: None,
        branch: Some(main_branch(tmp.path())),
    };
    let err = one
        .watermark(Some(&branch))
        .await
        .expect_err("two families");
    assert!(
        matches!(err, Error::CursorAcrossFamilies { families: 2 }),
        "{err:?}"
    );
    let since = RecordQuery {
        since_version: Some(0),
        worktrees: Some(branch),
        ..Default::default()
    };
    let err = one.find_records(&since).await.expect_err("two families");
    assert!(matches!(err, Error::CursorAcrossFamilies { .. }), "{err:?}");

    let own = one.watermark(None).await.expect("one family");
    let mine = RecordQuery {
        since_version: Some(own),
        ..Default::default()
    };
    assert!(one
        .find_records(&mine)
        .await
        .expect("one family")
        .is_empty());
}

fn main_branch(dir: &std::path::Path) -> String {
    let out = std::process::Command::new("git")
        .args(["rev-parse", "--abbrev-ref", "HEAD"])
        .current_dir(dir)
        .output()
        .expect("git");
    String::from_utf8(out.stdout)
        .expect("utf8")
        .trim()
        .to_string()
}

/// A checkout opened detached, as CI and pinned images are, scans again
/// and again.
#[tokio::test]
async fn a_worktree_opened_detached_keeps_scanning() {
    let (tmp, db) = file_backed_fixture().await;
    git(tmp.path(), &["checkout", "-q", "--detach"]);
    let sync = open_at(tmp.path(), &db).await;
    for _ in 0..2 {
        sync.update_from_working_dir(ScanOptions::default())
            .await
            .expect("scan while detached");
    }
}
