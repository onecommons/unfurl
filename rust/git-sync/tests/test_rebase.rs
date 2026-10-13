// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Rebasing a worktree's local commits onto upstream in the database
//! (docs/branch-segments.md §4.15, C.21), on both backends.

mod common;

use common::{crud_test, head_commit, DASHBOARD};
use tempfile::TempDir;
use unfurl_git_sync::{CommitId, Rebased, RecordQuery, ScanOptions, SyncedRepo};

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

/// A commit off `base` that replaces `from` with `to` in cloudmap.yaml,
/// made without moving HEAD: upstream, as a fetch brings it.
fn upstream(tmp: &TempDir, base: CommitId, from: &str, to: &str) -> CommitId {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("up");
    let up = path.to_str().expect("utf8");
    git(
        tmp.path(),
        &["worktree", "add", "-q", "--detach", up, &base.to_string()],
    );
    let file = path.join("cloudmap.yaml");
    let text = std::fs::read_to_string(&file).expect("read");
    assert!(text.contains(from), "{from:?} is in the file");
    std::fs::write(&file, text.replace(from, to)).expect("write");
    git(&path, &["commit", "-q", "-am", "upstream"]);
    let oid = git(&path, &["rev-parse", "HEAD"]);
    git(tmp.path(), &["worktree", "remove", "--force", up]);
    oid.parse().expect("a commit")
}

async fn scan(sync: &SyncedRepo) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
}

/// Name record `key` `name`, and commit it.
async fn commit_name(sync: &SyncedRepo, key: &str, name: &str) -> CommitId {
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        key,
        serde_json::json!({ "name": name }),
        None,
        false,
    )
    .await
    .expect("write");
    sync.commit_repository(name, Default::default())
        .await
        .expect("commit")
        .commit
        .expect("a commit")
}

/// Every record the view shows: key → (id, name).
async fn names(sync: &SyncedRepo) -> std::collections::BTreeMap<String, (i64, serde_json::Value)> {
    sync.find_records(&RecordQuery::default())
        .await
        .expect("find")
        .into_iter()
        .map(|r| (r.key, (r.id, r.json["name"].clone())))
        .collect()
}

/// Reset the checkout to `to` while HEAD is still `from`, scan, and
/// commit: what the caller does after the database's part.
async fn finish(
    sync: &SyncedRepo,
    tmp: &TempDir,
    from: CommitId,
    to: CommitId,
) -> Option<CommitId> {
    assert_eq!(
        git(tmp.path(), &["rev-parse", "HEAD"]),
        from.to_string(),
        "HEAD is still the rebased commit"
    );
    git(tmp.path(), &["reset", "-q", "--hard", &to.to_string()]);
    scan(sync).await;
    sync.commit_repository("rebased", Default::default())
        .await
        .expect("commit")
        .commit
}

/// A local commit's record goes onto upstream with its id, beside what
/// upstream changed, in one commit whose parent is upstream.
async fn local_commits_go_onto_upstream(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    let l = commit_name(sync, "local", "local").await;
    let id = names(sync).await["local"].0;
    let u = upstream(tmp, b, "name: dashboard", "name: upstream");
    let rebased = sync.rebase_onto(u).await.expect("rebase");
    let head = l;
    assert_eq!(
        rebased,
        Rebased::Unfolded {
            base: b,
            head,
            edits: 1
        }
    );
    let c = finish(sync, tmp, l, u)
        .await
        .expect("a commit onto upstream");
    let kept = git(tmp.path(), &["rev-parse", "refs/git-sync/rebased/main"]);
    assert_eq!(kept, l.to_string(), "the local commits stay reachable");
    assert_eq!(
        git(tmp.path(), &["rev-parse", &format!("{c}^@")]),
        u.to_string()
    );
    let seen = names(sync).await;
    assert_eq!(seen["local"].0, id, "the record keeps its id");
    assert_eq!(seen[DASHBOARD].1, "upstream", "upstream's change stays");
    let message = git(tmp.path(), &["log", "-1", "--format=%B", &c.to_string()]);
    assert!(
        message.contains("local"),
        "the rollup names the record:\n{message}"
    );
}
crud_test!(local_commits_go_onto_upstream);

/// A record both sides changed is a record conflict; the commit holds it
/// back, and the file keeps upstream's value.
async fn both_sides_changing_a_record_conflicts(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    let l = commit_name(sync, DASHBOARD, "mine").await;
    let u = upstream(tmp, b, "name: dashboard", "name: theirs");
    let rebased = sync.rebase_onto(u).await.expect("rebase");
    assert!(matches!(rebased, Rebased::Unfolded { .. }), "{rebased:?}");
    assert_eq!(finish(sync, tmp, l, u).await, None, "nothing to commit");
    // and a second rebase waits for them to be resolved
    let l = commit_name(sync, "other", "other").await;
    let u2 = upstream(tmp, u, "name: Odoo", "name: odoo");
    let Rebased::Refused(why) = sync.rebase_onto(u2).await.expect("rebase") else {
        panic!("rebased over conflicts");
    };
    assert!(why.contains("conflicts"), "{why}");
    assert_eq!(head_commit(sync).await, l);
    let conflicts = sync.list_conflicts(None).await.expect("conflicts");
    assert!(
        conflicts.iter().any(|c| c.key == DASHBOARD),
        "{conflicts:?}"
    );
    let text = std::fs::read_to_string(tmp.path().join("cloudmap.yaml")).expect("read");
    assert!(text.contains("name: theirs"));
}
crud_test!(both_sides_changing_a_record_conflicts);

/// Upstream ahead of HEAD is a fast-forward; one in HEAD's history is
/// nothing to rebase.
async fn a_fast_forward_and_an_ancestor_arent_rebased(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    let u = upstream(tmp, b, "name: dashboard", "name: upstream");
    assert_eq!(
        sync.rebase_onto(u).await.expect("rebase"),
        Rebased::FastForward
    );
    commit_name(sync, "local", "local").await;
    assert_eq!(sync.rebase_onto(b).await.expect("rebase"), Rebased::Ahead);
}
crud_test!(a_fast_forward_and_an_ancestor_arent_rebased);

/// What the refusal says, for a setup made after a scan at `b` and before
/// the rebase onto an upstream off `b`; the view is unchanged.
async fn refused(sync: &SyncedRepo, tmp: &TempDir, setup: impl AsyncFnOnce(&SyncedRepo)) -> String {
    scan(sync).await;
    let b = head_commit(sync).await;
    setup(sync).await;
    let before = names(sync).await;
    let u = upstream(tmp, b, "name: dashboard", "name: upstream");
    let Rebased::Refused(why) = sync.rebase_onto(u).await.expect("rebase") else {
        panic!("rebased");
    };
    assert_eq!(names(sync).await, before, "nothing changed");
    why
}

/// A working tree that isn't HEAD's would lose its changes to the reset.
async fn a_dirty_working_tree_is_refused(sync: &SyncedRepo, tmp: &TempDir) {
    let file = tmp.path().join("cloudmap.yaml");
    let why = refused(sync, tmp, async |sync| {
        commit_name(sync, "local", "local").await;
        let text = std::fs::read_to_string(&file).expect("read");
        std::fs::write(&file, text + "# a hand edit\n").expect("write");
    })
    .await;
    assert!(why.contains("isn't HEAD's"), "{why}");
}
crud_test!(a_dirty_working_tree_is_refused);

/// A local commit that changes what isn't a record has nothing to unfold
/// it into.
async fn a_change_outside_the_records_is_refused(sync: &SyncedRepo, tmp: &TempDir) {
    let why = refused(sync, tmp, async |sync| {
        let file = tmp.path().join("cloudmap.yaml");
        let text = std::fs::read_to_string(&file).expect("read");
        std::fs::write(&file, text + "notes: outside any section\n").expect("write");
        git(tmp.path(), &["commit", "-q", "-am", "outside the records"]);
        scan(sync).await;
    })
    .await;
    assert!(why.contains("aren't its records'"), "{why}");
}
crud_test!(a_change_outside_the_records_is_refused);

/// A file the local commits added has no rows the unfold could go into.
async fn an_added_file_is_refused(sync: &SyncedRepo, tmp: &TempDir) {
    let why = refused(sync, tmp, async |sync| {
        std::fs::write(tmp.path().join("notes.txt"), "notes\n").expect("write");
        git(tmp.path(), &["add", "notes.txt"]);
        git(tmp.path(), &["commit", "-q", "-m", "notes"]);
        scan(sync).await;
    })
    .await;
    assert!(why.contains("added or removed"), "{why}");
}
crud_test!(an_added_file_is_refused);

/// A HEAD the database hasn't scanned isn't what it would unfold.
async fn an_unscanned_head_is_refused(sync: &SyncedRepo, tmp: &TempDir) {
    let why = refused(sync, tmp, async |sync| {
        commit_name(sync, "local", "local").await;
        git(
            tmp.path(),
            &["commit", "-q", "--allow-empty", "-m", "unscanned"],
        );
    })
    .await;
    assert!(why.contains("HEAD isn't"), "{why}");
}
crud_test!(an_unscanned_head_is_refused);

/// Interrupted before the reset, the next scan finds HEAD at the local
/// commits and rebuilds back onto them: the view is as it was, and the
/// unfolded edits are no-ops the next commit folds.
async fn a_rebase_interrupted_before_the_reset_heals(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    commit_name(sync, "local", "local").await;
    let l = head_commit(sync).await;
    let before = names(sync).await;
    let u = upstream(tmp, b, "name: dashboard", "name: upstream");
    let rebased = sync.rebase_onto(u).await.expect("rebase");
    assert!(matches!(rebased, Rebased::Unfolded { .. }), "{rebased:?}");
    scan(sync).await;
    assert_eq!(head_commit(sync).await, l);
    assert_eq!(names(sync).await, before, "the view heals");
    assert!(sync
        .list_conflicts(None)
        .await
        .expect("conflicts")
        .is_empty()); // the unfolded edit stays, a no-op, until a commit folds it
    let left = sync.list_changes(None, false).await.expect("changes");
    assert_eq!(
        left.iter().map(|r| r.key.as_str()).collect::<Vec<_>>(),
        ["local"]
    );
    let c = sync.commit_repository("after", Default::default()).await;
    assert_eq!(c.expect("commit").commit, None, "nothing to commit");
    assert!(sync
        .list_changes(None, false)
        .await
        .expect("changes")
        .is_empty());
}
crud_test!(a_rebase_interrupted_before_the_reset_heals);

/// A record the local commits moved to another file would be two edits of
/// one record in the draft.
async fn a_moved_record_is_refused(sync: &SyncedRepo, tmp: &TempDir) {
    let other = tmp.path().join("other.yaml");
    std::fs::write(
        &other,
        "apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories: {}\n",
    )
    .expect("write");
    git(tmp.path(), &["add", "other.yaml"]);
    git(tmp.path(), &["commit", "-q", "-m", "other"]);
    let why = refused(sync, tmp, async |sync| {
        let file = tmp.path().join("cloudmap.yaml");
        let text = std::fs::read_to_string(&file).expect("read");
        let doc: serde_json::Value = serde_saphyr::from_str(&text).expect("yaml");
        let record = doc["repositories"][DASHBOARD].clone();
        let mut rest = doc.clone();
        rest["repositories"]
            .as_object_mut()
            .expect("map")
            .remove(DASHBOARD);
        std::fs::write(&file, serde_saphyr::to_string(&rest).expect("yaml")).expect("write");
        let moved = serde_json::json!({
            "apiVersion": "unfurl/v1.0.0",
            "kind": "CloudMap",
            "repositories": { DASHBOARD: record },
        });
        std::fs::write(&other, serde_saphyr::to_string(&moved).expect("yaml")).expect("write");
        git(tmp.path(), &["commit", "-q", "-am", "moved"]);
        scan(sync).await;
    })
    .await;
    assert!(why.contains("moved a record"), "{why}");
}
crud_test!(a_moved_record_is_refused);

/// A local commit that leaves the file unreadable has no records to
/// unfold.
async fn an_unparseable_file_is_refused(sync: &SyncedRepo, tmp: &TempDir) {
    let why = refused(sync, tmp, async |sync| {
        let file = tmp.path().join("cloudmap.yaml");
        std::fs::write(&file, "repositories: [unclosed\n").expect("write");
        git(tmp.path(), &["commit", "-q", "-am", "broken"]);
        scan(sync).await;
    })
    .await;
    assert!(why.contains("isn't a document a format reads"), "{why}");
}
crud_test!(an_unparseable_file_is_refused);

/// A mode change has no record to go into.
async fn a_mode_change_is_refused(sync: &SyncedRepo, tmp: &TempDir) {
    let why = refused(sync, tmp, async |sync| {
        git(tmp.path(), &["update-index", "--chmod=+x", "cloudmap.yaml"]);
        git(tmp.path(), &["commit", "-q", "-m", "executable"]);
        scan(sync).await;
    })
    .await;
    assert!(why.contains("mode changed"), "{why}");
}
crud_test!(a_mode_change_is_refused);
