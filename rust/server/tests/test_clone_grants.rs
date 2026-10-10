// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Startup's clones of public and private projects on a cloud server that
//! wants credentials for the private ones: what's served, what's recorded,
//! and the grants used.

#[path = "support/git_http.rs"]
mod git_http;

use std::path::{Path, PathBuf};
use std::time::Duration;

use base64::Engine;
use git_http::GitHttp;
use tempfile::TempDir;
use unfurl_git_sync::db::Db;
use unfurl_git_sync::model::{PRIVATE, PUBLIC};
use unfurl_git_sync::{DbConfig, FormatRegistry, ScanOptions, SyncedRepo, WorktreeFilter};
use unfurl_server::clone::prepare;
use unfurl_server::grants::{GrantKey, GrantStore};

const USER: &str = "deploy";
const TOKEN: &str = "t=k@n:x/y";

fn git(dir: &Path, args: &[&str]) -> String {
    let out = std::process::Command::new("git")
        .args(["-c", "user.name=t", "-c", "user.email=t@t"])
        .args([
            "-c",
            "init.defaultBranch=main",
            "-c",
            "protocol.file.allow=always",
        ])
        .args(args)
        .current_dir(dir)
        .env("GIT_TERMINAL_PROMPT", "0")
        .output()
        .expect("git");
    assert!(out.status.success(), "git {args:?}: {out:?}");
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

/// A cloud server over http with project `org/proj`, a database tracking
/// it, a grant store on that database, and an empty clone root.
struct World {
    tmp: TempDir,
    server: GitHttp,
    db: DbConfig,
}

impl World {
    async fn new() -> Self {
        let tmp = tempfile::tempdir().expect("tempdir");
        let served = tmp.path().join("served");
        let bare = served.join("org/proj.git");
        std::fs::create_dir_all(&bare).expect("mkdir");
        git(&bare, &["init", "-q", "--bare"]);
        let seed = tmp.path().join("seed");
        std::fs::create_dir_all(&seed).expect("mkdir");
        git(&seed, &["init", "-q"]);
        std::fs::write(seed.join("cloudmap.yaml"), "repositories: {}\n").expect("write");
        git(&seed, &["add", "."]);
        git(&seed, &["commit", "-q", "-m", "seed"]);
        git(&seed, &["push", "-q", bare.to_str().unwrap(), "main"]);
        let server = GitHttp::start(&served, USER, TOKEN).await;
        // the worktree the database tracks: a checkout elsewhere, scanned
        let elsewhere = tmp.path().join("elsewhere");
        let url = format!("{}/org/proj.git", server.base);
        git(
            tmp.path(),
            &["clone", "-q", &url, elsewhere.to_str().unwrap()],
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
        World { tmp, server, db }
    }

    fn root(&self) -> PathBuf {
        self.tmp.path().join("clones")
    }

    fn checkout(&self, under: &str) -> PathBuf {
        self.root().join(under).join("org/proj/main")
    }

    async fn database(&self) -> Db {
        Db::connect(&self.db).await.expect("db")
    }

    async fn grants(&self) -> GrantStore {
        let key = self.tmp.path().join("grant.key");
        std::fs::write(&key, [4u8; 32]).expect("write");
        let key = GrantKey::from_file(&key).expect("key");
        GrantStore::open(self.database().await, key, Duration::from_secs(3600))
            .await
            .expect("grants")
    }

    async fn grant(&self, grants: &GrantStore, user: &str, secret: &str) -> String {
        let credentials =
            base64::engine::general_purpose::STANDARD.encode(format!("{user}:{secret}"));
        grants
            .grant(
                "alice",
                &format!("{}/org/proj.git", self.server.base),
                &credentials,
                "",
            )
            .await
            .expect("grant")
    }

    async fn prepare(&self, grants: Option<&GrantStore>) -> Vec<PathBuf> {
        prepare(&self.db, &self.root(), &self.server.base, None, grants)
            .await
            .into_iter()
            .map(|c| c.path)
            .collect()
    }

    async fn visibility(&self) -> Option<String> {
        let worktrees = self
            .database()
            .await
            .worktrees(&WorktreeFilter::default())
            .await
            .expect("worktrees");
        worktrees[0].visibility.clone()
    }

    /// A commit on the served project's `main`.
    fn push_upstream(&self, name: &str) -> String {
        let seed = self.tmp.path().join("seed");
        std::fs::write(seed.join(name), name).expect("write");
        git(&seed, &["add", "."]);
        git(&seed, &["commit", "-q", "-m", name]);
        let bare = self.tmp.path().join("served/org/proj.git");
        git(&seed, &["push", "-q", bare.to_str().unwrap(), "main"]);
        git(&seed, &["rev-parse", "HEAD"])
    }
}

fn config_of(checkout: &Path) -> String {
    std::fs::read_to_string(checkout.join(".git/config")).expect("config")
}

#[tokio::test(flavor = "multi_thread")]
async fn a_public_project_is_served_and_recorded_public() {
    let world = World::new().await;
    assert_eq!(world.prepare(None).await, [world.checkout("public")]);
    assert_eq!(world.visibility().await.as_deref(), Some(PUBLIC));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_project_refused_anonymously_is_recorded_private_and_not_served() {
    let world = World::new().await;
    world.server.make_private("org/proj.git");
    assert!(world.prepare(None).await.is_empty());
    assert_eq!(world.visibility().await.as_deref(), Some(PRIVATE));
    assert!(!world.checkout("public").exists());
}

/// A repository known to be private isn't tried without credentials, even
/// where that would work.
#[tokio::test(flavor = "multi_thread")]
async fn a_known_private_project_isnt_tried_anonymously() {
    let world = World::new().await;
    world
        .database()
        .await
        .set_visibility(
            &format!("{}/org/proj.git", world.server.base),
            Some(PRIVATE),
        )
        .await
        .expect("set");
    let before = world.server.anonymous_requests();
    assert!(world.prepare(None).await.is_empty());
    assert_eq!(world.server.anonymous_requests(), before);
    assert!(!world.checkout("public").exists());
}

/// A private project is cloned with a grant, and served: the gateway
/// checks each reader's access to it. No checkout keeps the credential.
#[tokio::test(flavor = "multi_thread")]
async fn a_private_project_is_cloned_and_served_with_a_grant() {
    let world = World::new().await;
    world.server.make_private("org/proj.git");
    let grants = world.grants().await;
    world.grant(&grants, USER, TOKEN).await;
    assert_eq!(
        world.prepare(Some(&grants)).await,
        [world.checkout("private")]
    );
    let config = config_of(&world.checkout("private"));
    assert!(
        !config.contains("k@n") && !config.contains("k%40n"),
        "{config}"
    );
    assert!(!world.checkout("public").exists());
}

/// A grant git refuses is forgotten, and the next most recent tried.
#[tokio::test(flavor = "multi_thread")]
async fn a_refused_grant_is_forgotten_and_the_next_tried() {
    let world = World::new().await;
    world.server.make_private("org/proj.git");
    let grants = world.grants().await;
    world.grant(&grants, USER, TOKEN).await;
    // the most recently used, so tried first
    tokio::time::sleep(Duration::from_millis(1100)).await;
    let revoked = world.grant(&grants, USER, "revoked").await;
    world.prepare(Some(&grants)).await;
    assert!(world.checkout("private").join("cloudmap.yaml").exists());
    assert!(
        grants.token(&revoked).await.expect("token").is_none(),
        "forgotten"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_private_checkout_is_pulled_with_its_grant() {
    let world = World::new().await;
    world.server.make_private("org/proj.git");
    let grants = world.grants().await;
    world.grant(&grants, USER, TOKEN).await;
    world.prepare(Some(&grants)).await;
    let upstream = world.push_upstream("later");
    world.prepare(Some(&grants)).await;
    assert_eq!(
        git(&world.checkout("private"), &["rev-parse", "HEAD"]),
        upstream
    );
}

/// A project that went private after its public checkout was made, with
/// no grant yet: recorded private, and its checkout served as it is.
#[tokio::test(flavor = "multi_thread")]
async fn a_project_gone_private_is_served_as_it_is_until_a_grant() {
    let world = World::new().await;
    let before = world.prepare(None).await;
    assert_eq!(before, [world.checkout("public")]);
    let head = git(&world.checkout("public"), &["rev-parse", "HEAD"]);
    world.server.make_private("org/proj.git");
    world.push_upstream("later");
    assert_eq!(world.prepare(None).await, before);
    assert_eq!(world.visibility().await.as_deref(), Some(PRIVATE));
    assert_eq!(git(&world.checkout("public"), &["rev-parse", "HEAD"]), head);
}

/// Once a grant opens it, a project gone private is brought up, and
/// served, under private/.
#[tokio::test(flavor = "multi_thread")]
async fn a_project_gone_private_is_served_with_a_grant() {
    let world = World::new().await;
    world.prepare(None).await;
    world.server.make_private("org/proj.git");
    let upstream = world.push_upstream("later");
    let grants = world.grants().await;
    world.grant(&grants, USER, TOKEN).await;
    assert_eq!(
        world.prepare(Some(&grants)).await,
        [world.checkout("private")]
    );
    assert_eq!(
        git(&world.checkout("private"), &["rev-parse", "HEAD"]),
        upstream
    );
}

/// A grant that can't see the project, which GitLab answers with 404, is
/// forgotten like a refused one, and the next tried.
#[tokio::test(flavor = "multi_thread")]
async fn a_grant_that_cant_see_the_project_is_forgotten() {
    let world = World::new().await;
    world.server.make_private("org/proj.git");
    world.server.hide_from_others();
    let grants = world.grants().await;
    world.grant(&grants, USER, TOKEN).await;
    tokio::time::sleep(Duration::from_millis(1100)).await;
    let lost_access = world.grant(&grants, USER, "lost-access").await;
    assert_eq!(
        world.prepare(Some(&grants)).await,
        [world.checkout("private")]
    );
    assert!(
        grants.token(&lost_access).await.expect("token").is_none(),
        "forgotten"
    );
}
