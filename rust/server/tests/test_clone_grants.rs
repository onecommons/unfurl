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
        prepare(
            &self.db,
            &self.root(),
            &self.server.base,
            None,
            grants,
            unfurl_git_sync::git::Trust::Reduced,
            &[],
        )
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

impl World {
    /// The checkouts brought up with `org/proj` a default cloudmap, as
    /// `(path, default_cloudmap)`.
    async fn prepare_as_default(&self, grants: Option<&GrantStore>) -> Vec<(PathBuf, bool)> {
        let defaults =
            [unfurl_server::clone::DefaultCloudmap::parse("org/proj#main").expect("parse")];
        prepare(
            &self.db,
            &self.root(),
            &self.server.base,
            None,
            grants,
            unfurl_git_sync::git::Trust::Reduced,
            &defaults,
        )
        .await
        .into_iter()
        .map(|c| (c.path, c.default_cloudmap))
        .collect()
    }
}

/// A default cloudmap repository found private when it's cloned -- refused
/// anonymously, then cloned with a grant -- is served, but not as a default cloudmap: a
/// request naming no project, which the gateway can't check, doesn't read it.
#[tokio::test(flavor = "multi_thread")]
async fn a_default_cloudmap_found_private_isnt_one() {
    let world = World::new().await;
    world.server.make_private("org/proj.git");
    let grants = world.grants().await;
    world.grant(&grants, USER, TOKEN).await;
    assert_eq!(
        world.prepare_as_default(Some(&grants)).await,
        [(world.checkout("private"), false)]
    );
}

/// A default cloudmap repository gone private since its public checkout was made
/// stays served from that checkout, but not as a default cloudmap.
#[tokio::test(flavor = "multi_thread")]
async fn a_default_cloudmap_gone_private_isnt_one() {
    let world = World::new().await;
    assert_eq!(
        world.prepare_as_default(None).await,
        [(world.checkout("public"), true)]
    );
    world.server.make_private("org/proj.git");
    assert_eq!(
        world.prepare_as_default(None).await,
        [(world.checkout("public"), false)]
    );
}

/// A request naming a private project with no checkout, carrying its own
/// credentials, brings one up with them, under private/, with no grant
/// stored; without credentials, it isn't brought up.
#[tokio::test(flavor = "multi_thread")]
async fn a_request_with_credentials_brings_up_a_private_project() {
    use unfurl_server::cloudmap::ProjectCheck;
    let world = World::new().await;
    // a second project, private, the database doesn't track
    let served = world.tmp.path().join("served");
    let bare = served.join("org/secret.git");
    std::fs::create_dir_all(&bare).expect("mkdir");
    git(&bare, &["init", "-q", "--bare"]);
    git(
        &world.tmp.path().join("seed"),
        &["push", "-q", bare.to_str().unwrap(), "main"],
    );
    world.server.make_private("org/secret.git");

    let checkouts = prepare(
        &world.db,
        &world.root(),
        &world.server.base,
        None,
        None,
        unfurl_git_sync::git::Trust::Reduced,
        &[],
    )
    .await;
    let DbConfig::Sqlite { url } = &world.db else {
        unreachable!()
    };
    let cm = unfurl_server::clone::serve(
        checkouts,
        url,
        Some(ScanOptions::default()),
        |_| true,
        |_, _| String::new(),
        None,
        unfurl_git_sync::git::Trust::Reduced,
    )
    .await
    .expect("serve")
    .expect("served");
    let grants = std::sync::Arc::new(world.grants().await);
    let cloner = unfurl_server::clone::Cloner::new(
        cm.synced().db().clone(),
        &world.root(),
        &world.server.base,
        None,
        Some(grants.clone()),
        unfurl_git_sync::git::Trust::Reduced,
        |_| true,
    );
    let cm = cm.with_cloner(cloner);
    let secret = format!("{}/org/secret", world.server.base);

    let without = cm
        .project_check(Some("org/secret"), None, false, &Default::default(), None)
        .await
        .expect("check");
    assert!(!matches!(without, ProjectCheck::Serve(_)));
    let credentials = unfurl_server::grants::Credentials {
        grant: String::new(),
        user: USER.into(),
        secret: TOKEN.into(),
    };
    let with = cm
        .project_check(
            Some("org/secret"),
            None,
            false,
            &unfurl_server::clone::RequestAccess {
                private: true,
                credentials: Some(credentials),
                username: "alice".into(),
            },
            None,
        )
        .await
        .expect("check");
    assert!(matches!(with, ProjectCheck::Serve(_)));
    let checkout = world.root().join("private/org/secret/main");
    assert!(checkout.join(".git").exists());
    let config = config_of(&checkout);
    assert!(
        !config.contains("k@n") && !config.contains("k%40n"),
        "{config}"
    );
    // kept as a grant, for its later pulls
    let kept = grants.latest_credentials(&secret).await.expect("grants");
    let kept = kept.expect("a grant");
    assert_eq!((kept.user.as_str(), kept.secret.as_str()), (USER, TOKEN));
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let grant = world.database().await.latest_grant(&secret, now).await;
    assert_eq!(grant.expect("grants").expect("a grant").username, "alice");
}

impl World {
    /// The checkouts `prepare` makes with `grants`, served and pulled
    /// whenever a request finds one, with the store's grants too.
    async fn serve_fresh(
        &self,
        grants: Option<std::sync::Arc<GrantStore>>,
    ) -> unfurl_server::cloudmap::CloudMapState {
        let checkouts = unfurl_server::clone::prepare(
            &self.db,
            &self.root(),
            &self.server.base,
            None,
            grants.as_deref(),
            unfurl_git_sync::git::Trust::Reduced,
            &[],
        )
        .await;
        let DbConfig::Sqlite { url } = &self.db else {
            unreachable!()
        };
        let cm = unfurl_server::clone::serve(
            checkouts,
            url,
            Some(ScanOptions::default()),
            |_| true,
            |_, _| String::new(),
            None,
            unfurl_git_sync::git::Trust::Reduced,
        )
        .await
        .expect("serve")
        .expect("served");
        let settings = unfurl_server::fresh::Settings {
            timeout: 0,
            cache_key_prefix: "test:".into(),
            cloud_server: self.server.base.clone(),
            clone_root: Some(self.root()),
            grants,
            locks: None,
            trust: unfurl_git_sync::git::Trust::Reduced,
            // every pull may push
            push_interval: 0,
            record_ttl: 0,
        };
        cm.with_freshener(unfurl_server::fresh::Freshener::new(None, settings, |_| {
            true
        }))
    }

    /// Whether the private checkout reaches `commit` within twenty seconds
    /// of requests to `cm` with `access`, made until it does: a request
    /// finding a pull of it running leaves the pull to that one.
    async fn requested_until_pulled_to(
        &self,
        cm: &unfurl_server::cloudmap::CloudMapState,
        access: &unfurl_server::clone::RequestAccess,
        commit: &str,
    ) -> bool {
        for _ in 0..40 {
            cm.project_check(Some("org/proj"), None, false, access, None)
                .await
                .expect("check");
            if self.pulled_to(commit).await {
                return true;
            }
        }
        false
    }

    /// Whether the private checkout reaches `commit` within ten seconds.
    async fn pulled_to(&self, commit: &str) -> bool {
        for _ in 0..200 {
            // not `git`, which fails a test on a checkout half cloned
            let head = std::process::Command::new("git")
                .args(["rev-parse", "HEAD"])
                .current_dir(self.checkout("private"))
                .output();
            if head.is_ok_and(|out| String::from_utf8_lossy(&out.stdout).trim() == commit) {
                return true;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        false
    }
}

/// A private checkout a request without credentials finds stale is
/// pulled with a grant.
#[tokio::test(flavor = "multi_thread")]
async fn a_stale_private_checkout_is_pulled_with_a_grant() {
    let world = World::new().await;
    world.server.make_private("org/proj.git");
    let grants = std::sync::Arc::new(world.grants().await);
    world.grant(&grants, USER, TOKEN).await;
    let cm = world.serve_fresh(Some(grants)).await;
    let upstream = world.push_upstream("later");
    cm.project_check(Some("org/proj"), None, false, &Default::default(), None)
        .await
        .expect("check");
    assert!(world.pulled_to(&upstream).await);
}

/// One a request with credentials finds stale is pulled with them, as
/// with no grant it couldn't be.
#[tokio::test(flavor = "multi_thread")]
async fn a_stale_private_checkout_is_pulled_with_the_requests_credentials() {
    let world = World::new().await;
    world.server.make_private("org/proj.git");
    let grants = std::sync::Arc::new(world.grants().await);
    let grant = world.grant(&grants, USER, TOKEN).await;
    // brought up with the grant, which is then gone
    let cm = world.serve_fresh(Some(grants.clone())).await;
    grants.forget(&grant).await.expect("forget");
    let upstream = world.push_upstream("later");
    let credentials = base64::engine::general_purpose::STANDARD.encode(format!("{USER}:{TOKEN}"));
    let mut headers = axum::http::HeaderMap::new();
    headers.insert("x-git-credentials", credentials.parse().unwrap());
    let access = unfurl_server::cloudmap::request_access(&headers, None, None);
    let anonymous = Default::default();
    cm.project_check(Some("org/proj"), None, false, &anonymous, None)
        .await
        .expect("check");
    for _ in 0..200 {
        if cm.pulls_started() == 1 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    tokio::time::sleep(Duration::from_millis(300)).await;
    let head = git(&world.checkout("private"), &["rev-parse", "HEAD"]);
    assert_ne!(head, upstream, "not without credentials");
    assert!(
        world
            .requested_until_pulled_to(&cm, &access, &upstream)
            .await
    );
}

/// A grant git refuses during a pull is forgotten, as at startup, so the
/// next pull tries the one before it.
#[tokio::test(flavor = "multi_thread")]
async fn a_grant_refused_during_a_pull_is_forgotten() {
    let world = World::new().await;
    world.server.make_private("org/proj.git");
    let grants = std::sync::Arc::new(world.grants().await);
    world.grant(&grants, USER, TOKEN).await;
    let cm = world.serve_fresh(Some(grants.clone())).await;
    // a later grant, which git refuses
    world.grant(&grants, USER, "wrong").await;
    let origin = format!("{}/org/proj.git", world.server.base);
    let latest = async || {
        let credentials = grants.latest_credentials(&origin).await.expect("grants");
        credentials.expect("a grant").secret
    };
    assert_eq!(latest().await, "wrong");
    cm.project_check(Some("org/proj"), None, false, &Default::default(), None)
        .await
        .expect("check");
    let mut forgotten = false;
    for _ in 0..200 {
        if latest().await == TOKEN {
            forgotten = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(forgotten);
    let upstream = world.push_upstream("later");
    cm.project_check(Some("org/proj"), None, false, &Default::default(), None)
        .await
        .expect("check");
    assert!(world.pulled_to(&upstream).await);
}

/// A request's credentials aren't kept as a grant when git never took
/// them: a checkout already there, whose remote couldn't be reached, is
/// served as it is.
#[tokio::test(flavor = "multi_thread")]
async fn credentials_git_didnt_take_arent_kept() {
    use unfurl_server::cloudmap::ProjectCheck;
    let world = World::new().await;
    // a second project, private, the database doesn't track, with a
    // checkout there already whose remote can't be reached
    let served = world.tmp.path().join("served");
    let bare = served.join("org/secret.git");
    std::fs::create_dir_all(&bare).expect("mkdir");
    git(&bare, &["init", "-q", "--bare"]);
    git(
        &world.tmp.path().join("seed"),
        &["push", "-q", bare.to_str().unwrap(), "main"],
    );
    let checkout = world.root().join("private/org/secret/main");
    std::fs::create_dir_all(checkout.parent().unwrap()).expect("mkdir");
    let url = format!("{}/org/secret.git", world.server.base);
    git(
        world.tmp.path(),
        &["clone", "-q", &url, checkout.to_str().unwrap()],
    );
    world.server.make_private("org/secret.git");
    let grants = std::sync::Arc::new(world.grants().await);
    let cm = world.serve_fresh(None).await;
    let cloner = unfurl_server::clone::Cloner::new(
        cm.synced().db().clone(),
        &world.root(),
        &world.server.base,
        None,
        Some(grants.clone()),
        unfurl_git_sync::git::Trust::Reduced,
        |_| true,
    );
    let cm = cm.with_cloner(cloner);
    world.server.go_down(true);
    let access = unfurl_server::clone::RequestAccess {
        private: true,
        credentials: Some(unfurl_server::grants::Credentials {
            grant: String::new(),
            user: USER.into(),
            secret: TOKEN.into(),
        }),
        username: "alice".into(),
    };
    let check = cm
        .project_check(Some("org/secret"), None, false, &access, None)
        .await
        .expect("check");
    assert!(matches!(check, ProjectCheck::Serve(_)), "served as it is");
    let secret = format!("{}/org/secret", world.server.base);
    let kept = grants.latest_credentials(&secret).await.expect("grants");
    assert!(kept.is_none());
}

/// A grant git refuses while cloning a broken checkout again is forgotten
/// too, so the next pull tries the one before it.
#[tokio::test(flavor = "multi_thread")]
async fn a_grant_refused_cloning_a_broken_checkout_again_is_forgotten() {
    let world = World::new().await;
    world.server.make_private("org/proj.git");
    let grants = std::sync::Arc::new(world.grants().await);
    world.grant(&grants, USER, TOKEN).await;
    let cm = world.serve_fresh(Some(grants.clone())).await;
    world.grant(&grants, USER, "wrong").await;
    std::fs::remove_file(world.checkout("private").join(".git/HEAD")).expect("break it");
    let origin = format!("{}/org/proj.git", world.server.base);
    let latest = async || {
        let credentials = grants.latest_credentials(&origin).await.expect("grants");
        credentials.expect("a grant").secret
    };
    cm.project_check(Some("org/proj"), None, false, &Default::default(), None)
        .await
        .expect("check");
    let mut forgotten = false;
    for _ in 0..200 {
        if latest().await == TOKEN {
            forgotten = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(forgotten);
    let upstream = world.push_upstream("later");
    cm.project_check(Some("org/proj"), None, false, &Default::default(), None)
        .await
        .expect("check");
    assert!(
        world.pulled_to(&upstream).await,
        "cloned again with the one before"
    );
}

/// Commits a checkout is found holding that its remote lacks -- python's,
/// or a push's that failed -- are pushed with a grant before a pull, never
/// with the credentials of the request that found them: those are its
/// own, and the commits may be another's.
#[tokio::test(flavor = "multi_thread")]
async fn commits_found_ahead_are_pushed_with_a_grant_only() {
    let world = World::new().await;
    world.server.make_private("org/proj.git");
    let bare = world.tmp.path().join("served/org/proj.git");
    git(&bare, &["config", "http.receivepack", "true"]);
    let grants = std::sync::Arc::new(world.grants().await);
    let grant = world.grant(&grants, USER, TOKEN).await;
    let cm = world.serve_fresh(Some(grants.clone())).await;
    // a commit made in the checkout, as python's cloudmap writes make one
    let checkout = world.checkout("private");
    std::fs::write(checkout.join("theirs.txt"), "theirs").expect("write");
    git(&checkout, &["add", "."]);
    git(&checkout, &["commit", "-q", "-m", "theirs"]);
    let theirs = git(&checkout, &["rev-parse", "HEAD"]);
    let upstream = || git(&bare, &["rev-parse", "main"]);

    // without a grant, a request's credentials don't push it
    grants.forget(&grant).await.expect("forget");
    let mut headers = axum::http::HeaderMap::new();
    let token = base64::engine::general_purpose::STANDARD.encode(format!("{USER}:{TOKEN}"));
    headers.insert("x-git-credentials", token.parse().unwrap());
    let access = unfurl_server::cloudmap::request_access(&headers, None, None);
    cm.project_check(Some("org/proj"), None, false, &access, None)
        .await
        .expect("check");
    for _ in 0..200 {
        if cm.pulls_started() >= 1 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert_ne!(upstream(), theirs, "not with the request's credentials");

    // with one, the next pull pushes it first
    world.grant(&grants, USER, TOKEN).await;
    let mut pushed = false;
    for _ in 0..200 {
        if upstream() == theirs {
            pushed = true;
            break;
        }
        cm.project_check(Some("org/proj"), None, false, &Default::default(), None)
            .await
            .expect("check");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(pushed);
}

/// A write whose own credentials its remote refuses -- stale, as the
/// gateway let a developer write -- has its commit left to push, which a
/// grant then does.
#[tokio::test(flavor = "multi_thread")]
async fn a_commit_whose_credentials_are_refused_is_pushed_with_a_grant() {
    let world = World::new().await;
    world.server.make_private("org/proj.git");
    let bare = world.tmp.path().join("served/org/proj.git");
    git(&bare, &["config", "http.receivepack", "true"]);
    let grants = std::sync::Arc::new(world.grants().await);
    world.grant(&grants, USER, TOKEN).await;
    let cm = world.serve_fresh(Some(grants.clone())).await;
    // the checkout `cm` serves, without a request, which would pull it
    let served = cm.clone();
    served
        .synced()
        .create_record(
            Some("cloudmap.yaml"),
            "/repositories",
            "git://example.com/mine.git",
            serde_json::json!({"name": "mine"}),
            None,
            false,
        )
        .await
        .expect("create");
    let commit = served
        .commit("mine")
        .await
        .expect("commit")
        .expect("a commit");
    let wrong = unfurl_server::clone::RequestAccess {
        private: true,
        credentials: Some(unfurl_server::grants::Credentials {
            grant: String::new(),
            user: USER.into(),
            secret: "wrong".into(),
        }),
        username: "mallory".into(),
    };
    served.committed(&commit, &wrong).await;
    let mut tried = false;
    for _ in 0..400 {
        if served.pushes_started() >= 1 {
            tried = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(tried);
    let pending = served.push_outcome(&commit).await.expect("a push record");
    assert!(pending.contains("pending"), "{pending}");
    let checkout = world.checkout("private");
    assert_eq!(
        git(&checkout, &["rev-parse", "HEAD"]),
        commit,
        "not set aside"
    );
    // the next pull pushes it first, with the grant
    let mut pushed = false;
    for _ in 0..200 {
        if git(&bare, &["rev-parse", "main"]) == commit {
            pushed = true;
            break;
        }
        cm.project_check(Some("org/proj"), None, false, &Default::default(), None)
            .await
            .expect("check");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(pushed);
}
