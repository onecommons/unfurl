// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A shallow clone, whose oldest commit names a parent it doesn't have.

mod common;

use common::{git, init_repo_with_fixture, open_at, DASHBOARD};
use unfurl_git_sync::ScanOptions;

#[tokio::test]
async fn a_shallow_clone_scans() {
    let origin = tempfile::tempdir().expect("tempdir");
    init_repo_with_fixture(origin.path()).await;
    std::fs::write(origin.path().join("other.yaml"), "a: 1\n").expect("write");
    git(origin.path(), &["add", "other.yaml"]);
    git(origin.path(), &["commit", "-qm", "second"]);

    let clone = tempfile::tempdir().expect("tempdir");
    let url = format!("file://{}", origin.path().display());
    git(clone.path(), &["clone", "-q", "--depth", "1", &url, "."]);

    let db = format!(
        "sqlite://{}?mode=rwc",
        clone.path().join("sync.db").display()
    );
    let sync = open_at(clone.path(), &db).await;
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    let before = sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .expect("indexed");
    drop(sync);

    git(clone.path(), &["fetch", "-q", "--unshallow"]);
    let sync = open_at(clone.path(), &db).await;
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan after unshallowing");
    let after = sync
        .get_record("cloudmap.yaml", "/repositories", DASHBOARD)
        .await
        .expect("get")
        .expect("indexed");
    assert_eq!(before.id, after.id);
}
