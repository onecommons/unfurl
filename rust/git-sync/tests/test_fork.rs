//! Opening a new branch of a tracked origin forks it where its HEAD falls
//! in the family (docs/branch-segments.md §4.5, §4.6); rewriting a
//! branch's HEAD rebuilds its chain and resets its cursors (§4.8, §4.10).

mod common;

use common::*;
use unfurl_git_sync::{
    DbConfig, Error, FormatRegistry, RecordConflictKind, RecordQuery, ScanOptions, SyncedRepo,
    WorktreeFilter,
};

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
    sync.commit_repository(key, Default::default())
        .await
        .expect("commit")
        .commit
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

/// `git` in `dir`, with an identity: its output.
fn git_out(dir: &std::path::Path, args: &[&str]) -> String {
    let out = std::process::Command::new("git")
        .args(["-c", "user.name=t", "-c", "user.email=t@t"])
        .args(args)
        .current_dir(dir)
        .output()
        .expect("git");
    assert!(out.status.success(), "git {args:?}: {out:?}");
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

/// From `from`, a commit made upstream, changing a record git-sync's
/// commits since `from` didn't: its oid.
fn upstream_from(dir: &std::path::Path, from: &str) -> String {
    git(dir, &["reset", "-q", "--hard", from]);
    let file = dir.join("cloudmap.yaml");
    let text = std::fs::read_to_string(&file).expect("read");
    std::fs::write(&file, text.replace("name: dashboard", "name: upstream")).expect("write");
    git_out(dir, &["commit", "-q", "-am", "upstream"]);
    git_out(dir, &["rev-parse", "HEAD"])
}

/// Move HEAD to `to` and prune what that leaves unreachable, so commits
/// git-sync made since are gone, as from a new clone of a remote that
/// never got them.
fn reset_and_prune(dir: &std::path::Path, to: &str, gone: &str) {
    git(dir, &["reset", "-q", "--hard", to]);
    git(dir, &["reflog", "expire", "--expire=now", "--all"]);
    git(dir, &["gc", "-q", "--prune=now"]);
    let missing = std::process::Command::new("git")
        .args(["cat-file", "-e", &format!("{gone}^{{commit}}")])
        .current_dir(dir)
        .status()
        .expect("git");
    assert!(!missing.success(), "{gone} is pruned");
}

/// A scan whose recorded commit the repository no longer has refuses to
/// rebuild the committed view when that loses a record, and changes
/// nothing; `rebuild_missing` rebuilds anyway.
async fn a_missing_commit_that_loses_records_is_refused(
    sync: &SyncedRepo,
    tmp: &tempfile::TempDir,
) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let base = write_and_commit(sync, "one").await;
    let gone = write_and_commit(sync, "two").await;
    let up = upstream_from(tmp.path(), &base);
    reset_and_prune(tmp.path(), &up, &gone);

    // the record upstream changed isn't lost
    let err = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect_err("refused");
    let Error::CommitMissing {
        commit,
        lost: 1,
        since: Some(since),
    } = &err
    else {
        panic!("{err:?}");
    };
    assert_eq!(*commit, gone);
    assert!(names(sync).await.contains_key("two"), "nothing changed");
    // the record lost is among those written since
    let written = RecordQuery {
        since_version: Some(*since),
        ..Default::default()
    };
    let keys: Vec<String> = sync
        .find_records(&written)
        .await
        .expect("find")
        .into_iter()
        .map(|r| r.key)
        .collect();
    assert!(keys.contains(&"two".to_string()), "{keys:?}");
    assert!(!keys.contains(&"one".to_string()), "{keys:?}");

    let rebuild = ScanOptions {
        rebuild_missing: true,
        ..Default::default()
    };
    sync.update_from_working_dir(rebuild)
        .await
        .expect("rebuilt");
    assert!(!names(sync).await.contains_key("two"), "the record went");
}

crud_test!(a_missing_commit_that_loses_records_is_refused);

/// A missing recorded commit whose records HEAD has anyway, as when what
/// it committed reached the remote as another commit: the rebuild loses
/// nothing, so it goes ahead.
async fn a_missing_commit_that_loses_nothing_rebuilds(sync: &SyncedRepo, tmp: &tempfile::TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let base = write_and_commit(sync, "one").await;
    let gone = write_and_commit(sync, "two").await;
    let again = git_out(
        tmp.path(),
        &[
            "commit-tree",
            &format!("{gone}^{{tree}}"),
            "-p",
            &base,
            "-m",
            "the same, made again",
        ],
    );
    let up = upstream_from(tmp.path(), &again);
    reset_and_prune(tmp.path(), &up, &gone);

    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("rebuilt");
    assert!(names(sync).await.contains_key("two"));
    let head = sync.get_worktree().await.expect("worktree").commit_id;
    assert_eq!(head.as_deref(), Some(up.as_str()));
}

crud_test!(a_missing_commit_that_loses_nothing_rebuilds);

/// A pending edit survives a rebuild. One to a record the rewrite left
/// alone stays pending; one to a record the rewrite changed conflicts with
/// the value the new HEAD has.
async fn a_rebuild_keeps_pending_edits(sync: &SyncedRepo, tmp: &tempfile::TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let base = write_and_commit(sync, "one").await;
    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "one",
        serde_json::json!({ "name": "one-later" }),
        None,
        false,
    )
    .await
    .expect("update");
    let dropped = sync
        .commit_repository("later", Default::default())
        .await
        .expect("commit")
        .commit
        .expect("a commit");
    for (key, name) in [(DASHBOARD, "ours"), ("one", "ours")] {
        sync.update_record(
            Some("cloudmap.yaml"),
            "/repositories",
            key,
            serde_json::json!({ "name": name }),
            None,
            false,
        )
        .await
        .expect("pending edit");
    }

    let cursor = sync.watermark(None).await.expect("watermark");

    git(tmp.path(), &["reset", "-q", "--hard", &base]);
    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan after the reset");
    // the reset version moved: the scan rebuilt rather than took the
    // reset in as a hand edit
    let err = sync
        .list_changes(Some(cursor), false)
        .await
        .expect_err("a cursor from before the rebuild");
    assert!(matches!(err, Error::Reset { .. }), "{err:?}");
    assert_eq!(scan.conflicts.len(), 1, "{:?}", scan.conflicts);
    let conflict = &scan.conflicts[0];
    assert_eq!(conflict.key, "one");
    assert_eq!(conflict.kind, RecordConflictKind::ModifyModify);
    assert_eq!(conflict.base_commit_id.as_deref(), Some(dropped.as_str()));
    assert_eq!(conflict.theirs.as_ref().expect("theirs")["name"], "one");

    for key in [DASHBOARD, "one"] {
        let record = sync
            .get_record("cloudmap.yaml", "/repositories", key)
            .await
            .expect("get")
            .expect("present");
        assert_eq!(record.json["name"], "ours", "{key}");
        assert!(record.commit_id.is_none(), "{key} is still pending");
    }
}
crud_test!(a_rebuild_keeps_pending_edits);

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

/// A fork that splits main's head at a commit before a record went
/// invalid leaves main's view alone: the scan kept that record's row, so
/// the split takes it as unchanged.
#[tokio::test]
async fn a_split_leaves_a_record_main_rejects_alone() {
    const GITLAB_CI: &str = "git://unfurl.cloud/feb20a/dashboard.git#:.gitlab-ci.yml";
    let (tmp, db) = file_backed_fixture().await;
    git(
        tmp.path(),
        &["remote", "add", "origin", "https://example.com/fork.git"],
    );
    let main = open_at(tmp.path(), &db).await;
    main.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let sibling = |name: &str| {
        tmp.path().parent().expect("parent").join(format!(
            "{}-{name}",
            tmp.path().file_name().expect("name").to_string_lossy()
        ))
    };
    // a fork at main's HEAD closes main's first head
    let first = sibling("first");
    git(
        tmp.path(),
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            "first",
            first.to_str().expect("utf8"),
            "HEAD",
        ],
    );
    open_at(&first, &db).await;
    let c1 = write_and_commit(&main, "one").await;

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
    git(
        tmp.path(),
        &[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "-am",
            "outside",
        ],
    );
    main.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan the rejected record");
    let before = main
        .get_record("cloudmap.yaml", "/artifacts", GITLAB_CI)
        .await
        .expect("get")
        .expect("the scan keeps it");

    let feature = sibling("feature");
    git(
        tmp.path(),
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            "feature",
            feature.to_str().expect("utf8"),
            &c1,
        ],
    );
    open_at(&feature, &db).await;
    let after = main
        .get_record("cloudmap.yaml", "/artifacts", GITLAB_CI)
        .await
        .expect("get")
        .expect("main still shows it");
    assert_eq!(after.json, before.json);
    std::fs::remove_dir_all(&first).ok();
    std::fs::remove_dir_all(&feature).ok();
}

/// A fork at a commit where a record was invalid sees the row the scan
/// kept for it then, not a deletion. (Here that row is below main's head;
/// one the head has since replaced in place is lost.)
#[tokio::test]
async fn a_fork_where_a_record_was_invalid_keeps_its_row() {
    const GITLAB_CI: &str = "git://unfurl.cloud/feb20a/dashboard.git#:.gitlab-ci.yml";
    const VALID: &str = "  git://unfurl.cloud/feb20a/dashboard.git#:.gitlab-ci.yml:\n    type:\n      cloudmap.artifacts.ci.GitLabPipeline:\n";
    let (tmp, db) = file_backed_fixture().await;
    git(
        tmp.path(),
        &["remote", "add", "origin", "https://example.com/fork.git"],
    );
    let main = open_at(tmp.path(), &db).await;
    main.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let sibling = |name: &str| {
        tmp.path().parent().expect("parent").join(format!(
            "{}-{name}",
            tmp.path().file_name().expect("name").to_string_lossy()
        ))
    };
    // a fork at main's HEAD closes main's first head, which keeps the row
    let first = sibling("first-invalid");
    git(
        tmp.path(),
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            "first",
            first.to_str().expect("utf8"),
            "HEAD",
        ],
    );
    open_at(&first, &db).await;
    let original = main
        .get_record("cloudmap.yaml", "/artifacts", GITLAB_CI)
        .await
        .expect("get")
        .expect("seeded");
    let file = tmp.path().join("cloudmap.yaml");
    let text = std::fs::read_to_string(&file).expect("read");
    assert!(text.contains(VALID), "fixture shape changed");
    let commit = |edited: String, message: &str| {
        std::fs::write(&file, edited).expect("write");
        git(
            tmp.path(),
            &[
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                "commit",
                "-q",
                "-am",
                message,
            ],
        );
    };
    commit(
        text.replace(
            VALID,
            "  git://unfurl.cloud/feb20a/dashboard.git#:.gitlab-ci.yml:\n    type: cloudmap.artifacts.ci.GitLabPipeline\n",
        ),
        "invalid",
    );
    main.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan the rejected record");
    let rejected_at = head_commit(&main).await;
    commit(
        text.replace(VALID, &format!("{VALID}    name: fixed\n")),
        "fixed",
    );
    main.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan the fix");

    let dir = sibling("invalid");
    git(
        tmp.path(),
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            "then",
            dir.to_str().expect("utf8"),
            &rejected_at,
        ],
    );
    let then = open_at(&dir, &db).await;
    let seen = then
        .get_record("cloudmap.yaml", "/artifacts", GITLAB_CI)
        .await
        .expect("get")
        .expect("the row the scan kept");
    assert_eq!(seen.json, original.json);
    std::fs::remove_dir_all(&first).ok();
    std::fs::remove_dir_all(&dir).ok();
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

/// Deleting a fork leaves main's records; the fork's root can't go while
/// the fork belongs to its family, and can once it's alone, taking the
/// family with it: the checkout opens afresh as the origin's one worktree.
async fn deleting_worktrees_on(tmp: &tempfile::TempDir, config: DbConfig) {
    let open = |dir: std::path::PathBuf| {
        let config = config.clone();
        async move {
            SyncedRepo::open(&dir, config, FormatRegistry::with_builtins())
                .await
                .expect("open")
        }
    };
    git(
        tmp.path(),
        &["remote", "add", "origin", "https://example.com/fork.git"],
    );
    let main = open(tmp.path().to_path_buf()).await;
    main.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    write_and_commit(&main, "one").await;
    let dir = tmp.path().parent().expect("parent").join(format!(
        "{}-deleted",
        tmp.path().file_name().expect("name").to_string_lossy()
    ));
    git(
        tmp.path(),
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            "gone",
            dir.to_str().expect("utf8"),
            "HEAD",
        ],
    );
    let fork = open(dir.clone()).await;
    fork.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    write_and_commit(&fork, "theirs").await;
    let before = names(&main).await;

    let err = open(tmp.path().to_path_buf())
        .await
        .delete_worktree()
        .await
        .expect_err("main is the family's root");
    assert!(matches!(err, Error::FamilyInUse { members: 1 }), "{err:?}");

    fork.delete_worktree().await.expect("delete the fork");
    assert_eq!(names(&main).await, before, "main's view is unchanged");
    main.delete_worktree().await.expect("a root alone");

    let again = open(tmp.path().to_path_buf()).await;
    again
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan afresh");
    assert!(names(&again).await.contains_key("one"), "HEAD's records");
    let origin = WorktreeFilter {
        origin: Some("https://example.com/fork.git".into()),
        branch: None,
    };
    let left = again.worktrees(&origin).await.expect("worktrees");
    assert_eq!(left.len(), 1, "{left:?}");
    std::fs::remove_dir_all(&dir).ok();
}

#[tokio::test]
async fn deleting_worktrees() {
    let (tmp, db) = file_backed_fixture().await;
    deleting_worktrees_on(&tmp, DbConfig::Sqlite { url: db }).await;
}

#[cfg(feature = "postgres")]
#[tokio::test]
async fn deleting_worktrees_on_postgres() {
    let Some(scope) = PgScope::setup().await else {
        eprintln!("skip: UNFURL_TEST_PG_URL not set");
        return;
    };
    let tmp = tempfile::tempdir().expect("tempdir");
    init_repo_with_fixture(tmp.path()).await;
    deleting_worktrees_on(&tmp, scope.db_config()).await;
    scope.teardown().await;
}
