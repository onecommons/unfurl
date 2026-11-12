// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A save keeps a file's permissions, and creates a new file as any program
//! would.

mod common;

use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use common::git;
use serde_json::json;
use unfurl_git_sync::{DbConfig, FormatRegistry, OpenOptions, ScanOptions, SyncedDir};

const A: &str = "git://e.com/a.git";

fn cloudmap(path: &str) -> String {
    format!("apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  {A}:\n    path: {path}\n")
}

fn mode(path: &Path) -> u32 {
    std::fs::metadata(path).expect("stat").permissions().mode() & 0o7777
}

async fn open(root: &Path, file: &str) -> SyncedDir {
    let url = format!("sqlite://{}?mode=rwc", root.join("../sync.db").display());
    SyncedDir::open(
        root,
        DbConfig::Sqlite { url },
        FormatRegistry::with_builtins(),
        unfurl_git_sync::git::Trust::Full,
        OpenOptions::default().pathspecs([file]).untracked(true),
    )
    .await
    .expect("open")
}

/// Scan, edit the record, save.
async fn save_an_edit(sync: &SyncedDir, file: &str) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    sync.upsert_record(
        Some(file),
        "/repositories",
        A,
        json!({"path": "saved"}),
        None,
        false,
    )
    .await
    .expect("upsert");
    let saved = sync.save_changes().await.expect("save");
    assert!(
        saved.failed.is_empty() && saved.written.len() == 1,
        "{saved:?}"
    );
}

fn root() -> (tempfile::TempDir, std::path::PathBuf) {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path().join("root");
    std::fs::create_dir(&root).expect("mkdir");
    (tmp, root)
}

#[tokio::test]
async fn a_save_keeps_the_files_mode() {
    for bits in [0o644, 0o755, 0o600] {
        let (_tmp, root) = root();
        let path = root.join("cloudmap.yaml");
        std::fs::write(&path, cloudmap("a")).expect("write");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(bits)).expect("chmod");
        let sync = open(&root, "cloudmap.yaml").await;
        save_an_edit(&sync, "cloudmap.yaml").await;
        assert!(std::fs::read_to_string(&path)
            .expect("read")
            .contains("saved"));
        assert_eq!(mode(&path), bits, "{bits:o}");
    }
}

/// A new file gets the mode a plain create does: 0666 less the umask.
#[tokio::test]
async fn a_new_file_gets_the_umasks_mode() {
    let (tmp, root) = root();
    let probe = tmp.path().join("probe");
    std::fs::write(&probe, "").expect("write");
    let expected = mode(&probe);
    let sync = open(&root, "new.yaml").await;
    sync.upsert_record(
        Some("new.yaml"),
        "/repositories",
        A,
        json!({"path": "new"}),
        None,
        false,
    )
    .await
    .expect("upsert");
    let saved = sync.save_changes().await.expect("save");
    assert!(saved.failed.is_empty(), "{saved:?}");
    assert_eq!(mode(&root.join("new.yaml")), expected);
}

/// After a save and a commit of an executable tracked file, git sees no
/// change of mode.
#[tokio::test]
async fn an_executable_file_stays_executable_through_save_and_commit() {
    let (_tmp, root) = root();
    unfurl_git_sync::git::init_with_files(
        &root,
        &[("README".to_string(), b"r\n".to_vec())],
        "initial",
    )
    .expect("init");
    let path = root.join("cloudmap.yaml");
    std::fs::write(&path, cloudmap("a")).expect("write");
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).expect("chmod");
    git(&root, &["add", "cloudmap.yaml"]);
    git(
        &root,
        &[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@e",
            "commit",
            "-qm",
            "x",
        ],
    );
    let sync = open(&root, "cloudmap.yaml").await;
    save_an_edit(&sync, "cloudmap.yaml").await;
    let repo = sync.repository().expect("repository");
    let committed = repo
        .commit_repository("edit", Default::default())
        .await
        .expect("commit");
    assert!(committed.commit.is_some());
    assert_eq!(mode(&path), 0o755);
    let out = std::process::Command::new("git")
        .args(["status", "--porcelain"])
        .current_dir(&root)
        .output()
        .expect("git");
    assert_eq!(String::from_utf8_lossy(&out.stdout), "");
}
