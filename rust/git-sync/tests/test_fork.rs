//! Opening a new branch of a tracked origin forks it where its HEAD falls
//! in the family (docs/branch-segments.md §4.5, §4.6).

mod common;

use common::*;
use unfurl_git_sync::{RecordQuery, ScanOptions, SyncedRepo};

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
