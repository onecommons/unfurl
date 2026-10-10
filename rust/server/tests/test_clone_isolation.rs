// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! The startup clones' git doesn't take the host's git configuration. A
//! file of its own: it sets this process's environment, which every test
//! in a binary shares.

use std::path::Path;
use unfurl_git_sync::{DbConfig, FormatRegistry, ScanOptions, SyncedRepo};
use unfurl_server::clone::prepare;

fn git(dir: &Path, args: &[&str]) {
    let out = std::process::Command::new("git")
        .args(["-c", "user.name=t", "-c", "user.email=t@t"])
        .args(["-c", "init.defaultBranch=main"])
        .args(args)
        .current_dir(dir)
        .output()
        .expect("git");
    assert!(out.status.success(), "git {args:?}: {out:?}");
}

/// A credential helper or url rewrite configured on the host would reach
/// every clone of a user's repository; a clone takes neither.
#[tokio::test]
async fn a_clone_ignores_the_hosts_git_configuration() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let server = tmp.path().join("server");
    let bare = server.join("org/proj.git");
    std::fs::create_dir_all(&bare).expect("mkdir");
    git(&bare, &["init", "-q", "--bare"]);
    let seed = tmp.path().join("seed");
    std::fs::create_dir_all(&seed).expect("mkdir");
    git(&seed, &["init", "-q"]);
    std::fs::write(seed.join("cloudmap.yaml"), "repositories: {}\n").expect("write");
    git(&seed, &["add", "."]);
    git(&seed, &["commit", "-q", "-m", "seed"]);
    git(&seed, &["push", "-q", bare.to_str().unwrap(), "main"]);
    let server_url = format!("file://{}", server.display());
    let elsewhere = tmp.path().join("elsewhere");
    let project_url = format!("{server_url}/org/proj.git");
    git(
        tmp.path(),
        &["clone", "-q", &project_url, elsewhere.to_str().unwrap()],
    );
    let db = DbConfig::Sqlite {
        url: format!(
            "sqlite://{}?mode=rwc",
            tmp.path().join("db.sqlite").display()
        ),
    };
    let sync = SyncedRepo::open(&elsewhere, db.clone(), FormatRegistry::with_builtins())
        .await
        .expect("open");
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");

    // the host's: both would send the clone somewhere else
    let rewrite = "url.file:///nonexistent/.insteadOf";
    // a global config is found through HOME, which git keeps
    let home = tmp.path().join("home");
    std::fs::create_dir_all(&home).expect("mkdir");
    std::fs::write(
        home.join(".gitconfig"),
        format!("[url \"file:///nonexistent/\"]\n\tinsteadOf = {server_url}/\n"),
    )
    .expect("write");
    std::env::set_var("HOME", &home);
    std::env::remove_var("GIT_CONFIG_GLOBAL");
    std::env::remove_var("XDG_CONFIG_HOME");
    std::env::set_var(
        "GIT_CONFIG_PARAMETERS",
        format!("'{rewrite}'='{server_url}/'"),
    );

    let root = tmp.path().join("clones");
    let ready = prepare(&db, &root, &server_url, None, None).await;
    assert_eq!(ready.len(), 1, "the clone failed");
    assert!(root.join("public/org/proj/main/cloudmap.yaml").exists());
}
