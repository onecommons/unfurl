// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Merging upstream into a worktree's local commits, record by record
//! where git can't (docs/branch-segments.md §4.15, C.22), on both backends.

mod common;

use common::{crud_test, head_commit, DASHBOARD};
use tempfile::TempDir;
use unfurl_git_sync::{CommitId, MergedUpstream, RecordQuery, ScanOptions, SyncedRepo};

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

/// A commit off `base` that `edit`s cloudmap.yaml's text, made without
/// moving HEAD: upstream, as a fetch brings it.
fn upstream(tmp: &TempDir, base: CommitId, edit: impl FnOnce(String) -> String) -> CommitId {
    upstream_with(tmp, base, |dir| {
        let file = dir.join("cloudmap.yaml");
        let text = std::fs::read_to_string(&file).expect("read");
        std::fs::write(&file, edit(text)).expect("write");
    })
}

/// A commit off `base` of what `edit` does to a checkout of it.
fn upstream_with(tmp: &TempDir, base: CommitId, edit: impl FnOnce(&std::path::Path)) -> CommitId {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("up");
    let up = path.to_str().expect("utf8");
    git(
        tmp.path(),
        &["worktree", "add", "-q", "--detach", up, &base.to_string()],
    );
    edit(&path);
    git(&path, &["add", "-A"]);
    git(&path, &["commit", "-q", "-m", "upstream"]);
    let oid = git(&path, &["rev-parse", "HEAD"]);
    git(tmp.path(), &["worktree", "remove", "--force", up]);
    oid.parse().expect("a commit")
}

/// Insert `text` before the line `before` in a document.
fn insert_before(before: &'static str, text: &'static str) -> impl FnOnce(String) -> String {
    move |doc| {
        let at = doc.find(before).expect("the line");
        format!("{}{text}{}", &doc[..at], &doc[at..])
    }
}

/// `git merge-tree` of HEAD and `upstream`: the tree and the files git
/// couldn't merge.
fn merge_tree(tmp: &TempDir, upstream: CommitId) -> (gix::ObjectId, Vec<String>) {
    let out = std::process::Command::new("git")
        .args([
            "merge-tree",
            "--write-tree",
            "--name-only",
            "HEAD",
            &upstream.to_string(),
        ])
        .current_dir(tmp.path())
        .output()
        .expect("git");
    assert!(matches!(out.status.code(), Some(0 | 1)), "{out:?}");
    let text = String::from_utf8(out.stdout).expect("utf8");
    let mut lines = text.lines();
    let tree = gix::ObjectId::from_hex(lines.next().expect("a tree").as_bytes()).expect("hex");
    let conflicted = lines
        .take_while(|l| !l.is_empty())
        .map(str::to_string)
        .collect();
    (tree, conflicted)
}

async fn scan(sync: &SyncedRepo) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
}

/// Write record `key` with `json` and commit it.
async fn commit_record(sync: &SyncedRepo, key: &str, json: serde_json::Value) -> CommitId {
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        key,
        json,
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

/// Merge `upstream`, then what the caller does: fast-forward and scan.
async fn merge(
    sync: &SyncedRepo,
    tmp: &TempDir,
    upstream: CommitId,
) -> (MergedUpstream, Vec<String>) {
    let (tree, conflicted) = merge_tree(tmp, upstream);
    let merged = sync
        .merge_upstream(upstream, tree, &conflicted)
        .await
        .expect("merge");
    if let MergedUpstream::Merged { commit, .. } = &merged {
        git(
            tmp.path(),
            &["merge", "-q", "--ff-only", &commit.to_string()],
        );
        scan(sync).await;
    }
    (merged, conflicted)
}

/// Each record's id and name: key → (id, name).
async fn names(sync: &SyncedRepo) -> std::collections::BTreeMap<String, (i64, serde_json::Value)> {
    sync.find_records(&RecordQuery::default())
        .await
        .expect("find")
        .into_iter()
        .map(|r| (r.key, (r.id, r.json["name"].clone())))
        .collect()
}

fn file(tmp: &TempDir) -> String {
    std::fs::read_to_string(tmp.path().join("cloudmap.yaml")).expect("read")
}

const THEIRS: &str = "  git://example.com/theirs.git:\n    name: theirs\n";

/// Both sides added a different record at one place in a section: git
/// can't merge the lines, but nothing conflicts as records, and both land
/// in a merge whose parents are the two sides.
async fn records_appended_at_one_place_both_land(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    let l = commit_record(
        sync,
        "git://example.com/ours.git",
        serde_json::json!({"name": "ours"}),
    )
    .await;
    let id = names(sync).await["git://example.com/ours.git"].0;
    let u = upstream(
        tmp,
        b,
        insert_before("  git://unfurl.cloud/feb20a/dashboard.git:", THEIRS),
    );
    let (merged, conflicted) = merge(sync, tmp, u).await;
    assert_eq!(
        conflicted,
        ["cloudmap.yaml"],
        "git couldn't merge the lines"
    );
    let MergedUpstream::Merged { commit, head, held } = merged else {
        panic!("{merged:?}");
    };
    assert_eq!(head, l, "what the caller fast-forwards from");
    assert!(held.is_empty(), "{held:?}");
    let parents = git(tmp.path(), &["rev-parse", &format!("{commit}^@")]);
    assert_eq!(parents, format!("{l}\n{u}"));
    let seen = names(sync).await;
    assert_eq!(
        seen["git://example.com/ours.git"],
        (id, "ours".into()),
        "ours keeps its id"
    );
    assert_eq!(seen["git://example.com/theirs.git"].1, "theirs");
    assert!(sync
        .list_conflicts(None)
        .await
        .expect("conflicts")
        .is_empty());
    assert!(!file(tmp).contains("<<<<<<<"));
}
crud_test!(records_appended_at_one_place_both_land);

/// Both sides changed one record: upstream's value is in the merge, and
/// ours is held, in conflict with it, still the view's.
async fn a_record_both_changed_is_held(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    commit_record(sync, DASHBOARD, serde_json::json!({"name": "mine"})).await;
    let u = upstream(tmp, b, |t| t.replace("name: dashboard", "name: theirs"));
    let (merged, _) = merge(sync, tmp, u).await;
    let MergedUpstream::Merged { held, .. } = merged else {
        panic!("{merged:?}");
    };
    assert_eq!(
        held.iter().map(|h| h.key.as_str()).collect::<Vec<_>>(),
        [DASHBOARD]
    );
    assert!(
        file(tmp).contains("name: theirs"),
        "upstream's value is merged"
    );
    let conflicts = sync.list_conflicts(None).await.expect("conflicts");
    assert!(
        conflicts.iter().any(|c| c.key == DASHBOARD),
        "{conflicts:?}"
    );
    assert_eq!(
        names(sync).await[DASHBOARD].1,
        "mine",
        "the view shows ours, in conflict"
    );
}
crud_test!(a_record_both_changed_is_held);

/// Different fields of one record, far enough apart for git: its merge
/// stands, with nothing held.
async fn fields_git_merges_stand(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    let path = tmp.path().join("cloudmap.yaml");
    let text = file(tmp);
    std::fs::write(&path, text.replace("name: dashboard", "name: mine")).expect("write");
    git(tmp.path(), &["commit", "-q", "-am", "ours"]);
    scan(sync).await;
    let u = upstream(tmp, b, |t| {
        t.replacen("default_branch: main", "default_branch: trunk", 1)
    });
    let (merged, conflicted) = merge(sync, tmp, u).await;
    assert!(conflicted.is_empty(), "{conflicted:?}");
    let MergedUpstream::Merged { held, .. } = merged else {
        panic!("{merged:?}");
    };
    assert!(held.is_empty(), "{held:?}");
    let text = file(tmp);
    assert!(text.contains("name: mine") && text.contains("default_branch: trunk"));
    assert!(sync
        .list_conflicts(None)
        .await
        .expect("conflicts")
        .is_empty());
}
crud_test!(fields_git_merges_stand);

/// Both sides added the same record, at different places: git merges the
/// lines into a duplicate key, so the file is built from records, and the
/// record is held as both sides' creation.
async fn a_duplicate_key_git_made_is_built_from_records(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    commit_record(
        sync,
        "git://example.com/theirs.git",
        serde_json::json!({"name": "ours"}),
    )
    .await;
    let u = upstream(tmp, b, insert_before("artifacts:", THEIRS));
    let (merged, conflicted) = merge(sync, tmp, u).await;
    assert!(
        conflicted.is_empty(),
        "git merged the lines: {conflicted:?}"
    );
    let MergedUpstream::Merged { held, .. } = merged else {
        panic!("{merged:?}");
    };
    assert_eq!(held.len(), 1, "{held:?}");
    let text = file(tmp);
    assert_eq!(
        text.matches("git://example.com/theirs.git:").count(),
        1,
        "{text}"
    );
    assert!(text.contains("name: theirs"));
}
crud_test!(a_duplicate_key_git_made_is_built_from_records);

/// A file git couldn't merge, with a change of ours that isn't a record:
/// refused, nothing changed, so the caller diverts.
async fn a_non_record_change_git_cant_merge_is_refused(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    let path = tmp.path().join("cloudmap.yaml");
    std::fs::write(
        &path,
        insert_before("artifacts:", "notes: ours\n")(file(tmp)),
    )
    .expect("write");
    git(tmp.path(), &["commit", "-q", "-am", "ours"]);
    scan(sync).await;
    let l = head_commit(sync).await;
    let u = upstream(tmp, b, insert_before("artifacts:", THEIRS));
    let (merged, _) = merge(sync, tmp, u).await;
    assert!(
        matches!(&merged, MergedUpstream::Refused(why) if why.contains("aren't its records'")),
        "{merged:?}"
    );
    assert_eq!(head_commit(sync).await, l);
}
crud_test!(a_non_record_change_git_cant_merge_is_refused);

/// A conflict elsewhere doesn't hold up a merge, and stays.
async fn a_conflict_elsewhere_is_left(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    commit_record(sync, DASHBOARD, serde_json::json!({"name": "mine"})).await;
    let u = upstream(tmp, b, |t| t.replace("name: dashboard", "name: theirs"));
    merge(sync, tmp, u).await;
    commit_record(
        sync,
        "git://example.com/other.git",
        serde_json::json!({"name": "other"}),
    )
    .await;
    let u2 = upstream(tmp, u, |t| t.replace("name: Odoo", "name: odoo"));
    let (merged, _) = merge(sync, tmp, u2).await;
    assert!(
        matches!(merged, MergedUpstream::Merged { .. }),
        "{merged:?}"
    );
    let conflicts = sync.list_conflicts(None).await.expect("conflicts");
    assert!(
        conflicts.iter().any(|c| c.key == DASHBOARD),
        "{conflicts:?}"
    );
}
crud_test!(a_conflict_elsewhere_is_left);

/// A record both sides changed that's in conflict already: refused.
async fn a_conflict_at_a_held_record_is_refused(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": "pending"}),
        None,
        false,
    )
    .await
    .expect("write");
    let path = tmp.path().join("cloudmap.yaml");
    std::fs::write(&path, file(tmp).replace("name: dashboard", "name: disk")).expect("write");
    git(tmp.path(), &["commit", "-q", "-am", "ours"]);
    scan(sync).await;
    let conflicts = sync.list_conflicts(None).await.expect("conflicts");
    assert!(
        conflicts.iter().any(|c| c.key == DASHBOARD),
        "{conflicts:?}"
    );
    let u = upstream(tmp, b, |t| t.replace("name: dashboard", "name: theirs"));
    let head = head_commit(sync).await;
    let (merged, _) = merge(sync, tmp, u).await;
    assert!(
        matches!(&merged, MergedUpstream::Refused(why) if why.contains("conflict")),
        "{merged:?}"
    );
    assert_eq!(head_commit(sync).await, head);
}
crud_test!(a_conflict_at_a_held_record_is_refused);

/// Upstream ahead of HEAD is a fast-forward; one in HEAD's history is
/// nothing to merge.
async fn a_fast_forward_and_an_ancestor_arent_merged(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    let u = upstream(tmp, b, |t| t.replace("name: dashboard", "name: theirs"));
    let (tree, conflicted) = merge_tree(tmp, u);
    let got = sync
        .merge_upstream(u, tree, &conflicted)
        .await
        .expect("merge");
    assert_eq!(got, MergedUpstream::FastForward);
    commit_record(
        sync,
        "git://example.com/ours.git",
        serde_json::json!({"name": "ours"}),
    )
    .await;
    let (tree, conflicted) = merge_tree(tmp, b);
    let got = sync
        .merge_upstream(b, tree, &conflicted)
        .await
        .expect("merge");
    assert_eq!(got, MergedUpstream::Ahead);
}
crud_test!(a_fast_forward_and_an_ancestor_arent_merged);

/// Stopped before the fast-forward, the held edits are no-ops at HEAD: a
/// scan finds no conflict, and the next commit folds them.
async fn a_merge_stopped_before_the_fast_forward_loses_nothing(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    let l = commit_record(sync, DASHBOARD, serde_json::json!({"name": "mine"})).await;
    let before = names(sync).await;
    let u = upstream(tmp, b, |t| t.replace("name: dashboard", "name: theirs"));
    let (tree, conflicted) = merge_tree(tmp, u);
    let got = sync
        .merge_upstream(u, tree, &conflicted)
        .await
        .expect("merge");
    assert!(matches!(got, MergedUpstream::Merged { .. }), "{got:?}");
    scan(sync).await;
    assert_eq!(head_commit(sync).await, l);
    assert_eq!(names(sync).await, before);
    assert!(sync
        .list_conflicts(None)
        .await
        .expect("conflicts")
        .is_empty());
    let c = sync
        .commit_repository("after", Default::default())
        .await
        .expect("commit");
    assert_eq!(c.commit, None, "nothing to commit");
    assert!(sync
        .list_changes(None, false)
        .await
        .expect("changes")
        .is_empty());
}
crud_test!(a_merge_stopped_before_the_fast_forward_loses_nothing);

/// Both sides added the same key to a JSON document, at different places:
/// JSON's parser would keep the last, so the duplicate is found apart and
/// the file is built from records.
async fn a_duplicate_json_key_git_made_is_built_from_records(sync: &SyncedRepo, tmp: &TempDir) {
    let path = tmp.path().join("other.json");
    let doc = "{\n  \"apiVersion\": \"unfurl/v1.0.0\",\n  \"kind\": \"CloudMap\",\n  \"repositories\": {\n    \"git://example.com/a.git\": {\n      \"name\": \"a\"\n    },\n    \"git://example.com/m.git\": {\n      \"name\": \"m\"\n    },\n    \"git://example.com/z.git\": {\n      \"name\": \"z\"\n    }\n  }\n}\n";
    std::fs::write(&path, doc).expect("write");
    git(tmp.path(), &["add", "other.json"]);
    git(tmp.path(), &["commit", "-q", "-m", "json"]);
    scan(sync).await;
    let b = head_commit(sync).await;
    let ours = doc.replace(
        "    \"git://example.com/a.git\": {",
        "    \"git://example.com/dup.git\": {\n      \"name\": \"ours\"\n    },\n    \"git://example.com/a.git\": {",
    );
    std::fs::write(&path, ours).expect("write");
    git(tmp.path(), &["commit", "-q", "-am", "ours"]);
    scan(sync).await;
    let dir = tempfile::tempdir().expect("tempdir");
    let up_dir = dir.path().join("up");
    let up_path = up_dir.to_str().expect("utf8");
    git(
        tmp.path(),
        &["worktree", "add", "-q", "--detach", up_path, &b.to_string()],
    );
    let theirs = doc.replace(
        "    \"git://example.com/z.git\": {\n      \"name\": \"z\"\n    }",
        "    \"git://example.com/z.git\": {\n      \"name\": \"z\"\n    },\n    \"git://example.com/dup.git\": {\n      \"name\": \"theirs\"\n    }",
    );
    std::fs::write(up_dir.join("other.json"), theirs).expect("write");
    git(&up_dir, &["commit", "-q", "-am", "theirs"]);
    let u: CommitId = git(&up_dir, &["rev-parse", "HEAD"])
        .parse()
        .expect("a commit");
    git(tmp.path(), &["worktree", "remove", "--force", up_path]);
    let (merged, conflicted) = merge(sync, tmp, u).await;
    assert!(
        conflicted.is_empty(),
        "git merged the lines: {conflicted:?}"
    );
    let MergedUpstream::Merged { held, .. } = merged else {
        panic!("{merged:?}");
    };
    assert_eq!(held.len(), 1, "{held:?}");
    let text = std::fs::read_to_string(&path).expect("read");
    assert_eq!(
        text.matches("git://example.com/dup.git").count(),
        1,
        "{text}"
    );
}
crud_test!(a_duplicate_json_key_git_made_is_built_from_records);

/// A HEAD the database hasn't scanned isn't what the merge would take.
async fn an_unscanned_head_is_refused(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    commit_record(
        sync,
        "git://example.com/ours.git",
        serde_json::json!({"name": "ours"}),
    )
    .await;
    git(
        tmp.path(),
        &["commit", "-q", "--allow-empty", "-m", "unscanned"],
    );
    let u = upstream(tmp, b, |t| t.replace("name: dashboard", "name: theirs"));
    let (tree, conflicted) = merge_tree(tmp, u);
    let got = sync
        .merge_upstream(u, tree, &conflicted)
        .await
        .expect("merge");
    assert!(
        matches!(&got, MergedUpstream::Refused(why) if why.contains("HEAD isn't")),
        "{got:?}"
    );
}
crud_test!(an_unscanned_head_is_refused);

/// A tree that dropped a record only this side changed, as a hunk git
/// applied in a shifted context could: the file is built from records.
async fn a_merged_tree_missing_a_change_of_ours_is_rebuilt(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    commit_record(sync, DASHBOARD, serde_json::json!({"name": "mine"})).await;
    let u = upstream(tmp, b, |t| t.replace("name: Odoo", "name: odoo"));
    let (_, conflicted) = merge_tree(tmp, u);
    assert!(conflicted.is_empty(), "{conflicted:?}");
    let wrong: gix::ObjectId = git(tmp.path(), &["rev-parse", &format!("{u}^{{tree}}")])
        .parse()
        .expect("a tree");
    let got = sync.merge_upstream(u, wrong, &[]).await.expect("merge");
    let MergedUpstream::Merged { commit, held, .. } = got else {
        panic!("{got:?}");
    };
    assert!(held.is_empty(), "{held:?}");
    let text = git(tmp.path(), &["show", &format!("{commit}:cloudmap.yaml")]);
    assert!(
        text.contains("name: mine") && text.contains("name: odoo"),
        "{text}"
    );
}
crud_test!(a_merged_tree_missing_a_change_of_ours_is_rebuilt);

/// Merge `u` and expect a refusal saying `why`, with HEAD unmoved.
async fn refused_with(sync: &SyncedRepo, tmp: &TempDir, u: CommitId, why: &str) {
    let head = head_commit(sync).await;
    let (merged, conflicted) = merge(sync, tmp, u).await;
    assert!(!conflicted.is_empty(), "git couldn't merge: {conflicted:?}");
    assert!(
        matches!(&merged, MergedUpstream::Refused(r) if r.contains(why)),
        "{merged:?}"
    );
    assert_eq!(head_commit(sync).await, head);
    assert!(sync
        .list_conflicts(None)
        .await
        .expect("conflicts")
        .is_empty());
}

/// Both sides added a file at one path: refused, as C.21 refuses.
async fn a_file_both_added_is_refused(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    std::fs::write(tmp.path().join("new.yaml"), "a: ours\n").expect("write");
    git(tmp.path(), &["add", "new.yaml"]);
    git(tmp.path(), &["commit", "-q", "-m", "ours"]);
    scan(sync).await;
    let u = upstream_with(tmp, b, |dir| {
        std::fs::write(dir.join("new.yaml"), "a: theirs\n").expect("write");
    });
    refused_with(sync, tmp, u, "added or removed").await;
}
crud_test!(a_file_both_added_is_refused);

/// A mode this side changed, in a file git couldn't merge: refused.
async fn a_mode_change_is_refused(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    git(tmp.path(), &["update-index", "--chmod=+x", "cloudmap.yaml"]);
    git(tmp.path(), &["commit", "-q", "-m", "mode"]);
    scan(sync).await;
    commit_record(sync, DASHBOARD, serde_json::json!({"name": "mine"})).await;
    let u = upstream(tmp, b, |t| t.replace("name: dashboard", "name: theirs"));
    refused_with(sync, tmp, u, "mode changed").await;
}
crud_test!(a_mode_change_is_refused);

/// Upstream's version of a file git couldn't merge doesn't parse: refused.
async fn an_unparseable_upstream_is_refused(sync: &SyncedRepo, tmp: &TempDir) {
    scan(sync).await;
    let b = head_commit(sync).await;
    commit_record(sync, DASHBOARD, serde_json::json!({"name": "mine"})).await;
    let u = upstream(tmp, b, |t| t.replace("name: dashboard", "name: [unclosed"));
    refused_with(sync, tmp, u, "upstream").await;
}
crud_test!(an_unparseable_upstream_is_refused);

/// The server awaits a merge on a multi-threaded runtime.
#[allow(dead_code)]
fn a_merge_is_send(sync: &SyncedRepo, u: CommitId, tree: gix::ObjectId) {
    fn send<T: Send>(_: T) {}
    send(sync.merge_upstream(u, tree, &[]));
}
