// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Cloning the cloudmap database's worktrees at startup (`clone::prepare`),
//! against a bare repository standing in for the cloud server.

use std::path::{Path, PathBuf};

use tempfile::TempDir;
use unfurl_git_sync::{DbConfig, FormatRegistry, ScanOptions, SyncedRepo};
use unfurl_server::clone::{prepare, serve, Checkout, DefaultCloudmap};
use unfurl_server::cloudmap::ProjectCheck;

fn git(dir: &Path, args: &[&str]) -> String {
    let out = std::process::Command::new("git")
        .args([
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "init.defaultBranch=main",
        ])
        .args(args)
        .current_dir(dir)
        .output()
        .expect("git");
    assert!(out.status.success(), "git {args:?}: {out:?}");
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

/// A cloud server with project `org/proj` on `main`, a database tracking
/// it, and an empty clone root.
struct World {
    tmp: TempDir,
    db: DbConfig,
}

impl World {
    async fn new() -> Self {
        let tmp = tempfile::tempdir().expect("tempdir");
        let db = DbConfig::Sqlite {
            url: format!(
                "sqlite://{}?mode=rwc",
                tmp.path().join("db.sqlite").display()
            ),
        };
        Self::track(&tmp, &db, "org/proj").await;
        World { tmp, db }
    }

    /// A project on the server, `seed` its checkout to push from, which the
    /// database tracks through a checkout elsewhere.
    async fn track(tmp: &TempDir, db: &DbConfig, project: &str) {
        let bare = tmp.path().join(format!("server/{project}.git"));
        std::fs::create_dir_all(&bare).expect("mkdir");
        git(&bare, &["init", "-q", "--bare"]);
        let seed = tmp.path().join("seed").join(project);
        std::fs::create_dir_all(&seed).expect("mkdir");
        git(&seed, &["init", "-q"]);
        std::fs::write(seed.join("cloudmap.yaml"), "repositories: {}\n").expect("write");
        git(&seed, &["add", "."]);
        git(&seed, &["commit", "-q", "-m", "seed"]);
        git(&seed, &["remote", "add", "origin", bare.to_str().unwrap()]);
        git(&seed, &["push", "-q", "origin", "main"]);
        // the worktree the database tracks: a checkout elsewhere, scanned
        let elsewhere = tmp.path().join("elsewhere").join(project);
        git(
            tmp.path(),
            &[
                "clone",
                "-q",
                &format!("{}/{project}.git", Self::server_of(tmp)),
                elsewhere.to_str().unwrap(),
            ],
        );
        let sync = SyncedRepo::open(&elsewhere, db.clone(), FormatRegistry::with_builtins())
            .await
            .expect("open");
        sync.update_from_working_dir(ScanOptions::default())
            .await
            .expect("scan");
    }

    fn server_of(tmp: &TempDir) -> String {
        format!("file://{}", tmp.path().join("server").display())
    }

    fn server(&self) -> String {
        Self::server_of(&self.tmp)
    }

    fn root(&self) -> PathBuf {
        self.tmp.path().join("clones")
    }

    fn public(&self) -> PathBuf {
        self.root().join("public/org/proj/main")
    }

    async fn prepare(&self) -> Vec<Checkout> {
        prepare(
            &self.db,
            &self.root(),
            &self.server(),
            None,
            None,
            unfurl_git_sync::git::Trust::Reduced,
            &[],
        )
        .await
    }

    /// A commit on the server's `main`, made from the seed checkout.
    fn push_upstream(&self, name: &str) -> String {
        let seed = self.tmp.path().join("seed/org/proj");
        std::fs::write(seed.join(name), name).expect("write");
        git(&seed, &["add", "."]);
        git(&seed, &["commit", "-q", "-m", name]);
        git(&seed, &["push", "-q", "origin", "main"]);
        git(&seed, &["rev-parse", "HEAD"])
    }
}

#[tokio::test]
async fn a_worktree_with_no_checkout_is_cloned() {
    let world = World::new().await;
    let got = world.prepare().await;
    assert_eq!(
        got,
        [Checkout {
            path: world.public(),
            project: "org/proj".into(),
            branch: "main".into(),
            default_cloudmap: false,
            pulled: Some("cloned"),
        }]
    );
    assert!(world.public().join("cloudmap.yaml").exists());
    assert!(!world.public().with_extension("lock").exists());
}

impl World {
    /// Push a commit adding `name`, a symlink to a file outside the checkout.
    fn push_symlink_upstream(&self, name: &str) {
        let seed = self.tmp.path().join("seed/org/proj");
        std::os::unix::fs::symlink("/etc/hosts", seed.join(name)).expect("symlink");
        git(&seed, &["add", "."]);
        git(&seed, &["commit", "-q", "-m", name]);
        git(&seed, &["push", "-q", "origin", "main"]);
    }
}

/// A committed symlink would expose a file outside the checkout, so every
/// checkout, the first and each one after, writes it as a plain file.
#[tokio::test]
async fn a_clone_never_checks_out_a_symlink() {
    let world = World::new().await;
    world.push_symlink_upstream("first");
    world.prepare().await;
    world.push_symlink_upstream("later");
    world.prepare().await;
    for name in ["first", "later"] {
        let path = world.public().join(name);
        assert!(path.exists() && !path.is_symlink(), "{name} is a symlink");
    }
}

/// A checkout cloned before symlinks were turned off has them turned off
/// when it's next pulled.
#[tokio::test]
async fn an_existing_checkout_stops_checking_out_symlinks() {
    let world = World::new().await;
    world.prepare().await;
    git(&world.public(), &["config", "--unset", "core.symlinks"]);
    world.push_symlink_upstream("later");
    world.prepare().await;
    assert!(!world.public().join("later").is_symlink());
}

#[tokio::test]
async fn a_checkout_behind_its_remote_is_fast_forwarded() {
    let world = World::new().await;
    world.prepare().await;
    let upstream = world.push_upstream("later");
    assert_eq!(world.prepare().await.len(), 1);
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), upstream);
}

/// A checkout with a commit its remote hasn't got, which can't be
/// fast-forwarded: served as it is, the commit kept.
#[tokio::test]
async fn a_diverged_checkout_is_served_as_it_is() {
    let world = World::new().await;
    world.prepare().await;
    world.push_upstream("theirs");
    std::fs::write(world.public().join("ours"), "ours").expect("write");
    git(&world.public(), &["add", "."]);
    git(&world.public(), &["commit", "-q", "-m", "ours"]);
    let ours = git(&world.public(), &["rev-parse", "HEAD"]);
    assert_eq!(world.prepare().await.len(), 1);
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), ours);
}

#[tokio::test]
async fn a_checkout_python_is_cloning_is_left_out() {
    let world = World::new().await;
    world.prepare().await;
    let before = git(&world.public(), &["rev-parse", "HEAD"]);
    world.push_upstream("later");
    let lock = PathBuf::from(format!("{}.lock", world.public().display()));
    std::fs::write(&lock, "1").expect("lock");
    assert!(world.prepare().await.is_empty());
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), before);
}

/// A checkout python made with someone's credentials, under `private/`,
/// is never served: the public one is, as python serves a request with
/// none.
#[tokio::test]
async fn a_credentialed_checkout_is_not_used() {
    let world = World::new().await;
    let private = world.root().join("private/org/proj/main");
    std::fs::create_dir_all(private.parent().unwrap()).expect("mkdir");
    git(
        world.tmp.path(),
        &[
            "clone",
            "-q",
            &format!("{}/org/proj.git", world.server()),
            private.to_str().unwrap(),
        ],
    );
    let got = world.prepare().await;
    assert_eq!(got.len(), 1);
    assert_eq!(got[0].path, world.public());
}

/// A project its remote won't hand over without credentials, as a private
/// one: not cloned, so not served, and left to python.
#[tokio::test]
async fn a_private_project_is_left_to_python() {
    use std::os::unix::fs::PermissionsExt;
    let world = World::new().await;
    let bare = world.tmp.path().join("server/org/proj.git");
    std::fs::set_permissions(&bare, std::fs::Permissions::from_mode(0o000)).expect("chmod");
    let got = world.prepare().await;
    std::fs::set_permissions(&bare, std::fs::Permissions::from_mode(0o755)).expect("chmod");
    assert!(got.is_empty(), "{got:?}");
    assert!(!world.public().exists());
}

#[tokio::test]
async fn a_worktree_on_another_server_is_left_out() {
    let world = World::new().await;
    let got = prepare(
        &world.db,
        &world.root(),
        "https://elsewhere.example",
        None,
        None,
        unfurl_git_sync::git::Trust::Reduced,
        &[],
    )
    .await;
    assert!(got.is_empty());
    assert!(!world.root().exists());
}

/// A directory where the checkout goes that isn't one: moved aside, not
/// pulled into or deleted, and the checkout cloned.
#[tokio::test]
async fn a_directory_that_isnt_a_checkout_is_moved_aside() {
    let world = World::new().await;
    std::fs::create_dir_all(world.public()).expect("mkdir");
    std::fs::write(world.public().join("mine"), "mine").expect("write");
    assert_eq!(world.prepare().await.len(), 1);
    assert!(world.public().join(".git").exists());
    let parent = world.public().parent().unwrap().to_path_buf();
    let aside: Vec<_> = std::fs::read_dir(&parent)
        .expect("read")
        .filter_map(|e| e.ok())
        .filter(|e| e.file_name().to_string_lossy().starts_with("main.broken-"))
        .collect();
    assert_eq!(aside.len(), 1, "moved aside");
    assert!(aside[0].path().join("mine").exists());
}

/// Two projects, each cloned and opened against the one database, as
/// startup does, then served together: each answers for its own project.
#[tokio::test]
async fn several_clones_open_on_one_database() {
    let world = World::new().await;
    World::track(&world.tmp, &world.db, "org/two").await;
    let checkouts = world.prepare().await;
    assert_eq!(checkouts.len(), 2, "{checkouts:?}");
    let DbConfig::Sqlite { url } = &world.db else {
        unreachable!()
    };
    let cm = serve(
        checkouts.clone(),
        url,
        Some(ScanOptions::default()),
        |_| true,
        |_, _| String::new(),
        None,
        unfurl_git_sync::git::Trust::Reduced,
    )
    .await
    .expect("serving")
    .expect("checkouts");
    for c in &checkouts {
        let check = cm
            .project_check(Some(&c.project), None, false, &Default::default(), None)
            .await
            .expect("check");
        let ProjectCheck::Serve(target) = check else {
            panic!("{} isn't served", c.project);
        };
        let at = target.synced().get_working_dir().await.expect("dir");
        assert_eq!(at.repo_path, c.path, "{}", c.project);
    }
}

/// A checkout whose scan isn't accepted, or that doesn't open, isn't
/// served; the rest are.
#[tokio::test]
async fn a_checkout_that_fails_is_left_out_of_the_rest() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    let world = World::new().await;
    World::track(&world.tmp, &world.db, "org/two").await;
    let mut checkouts = world.prepare().await;
    assert_eq!(checkouts.len(), 2);
    let DbConfig::Sqlite { url } = &world.db else {
        unreachable!()
    };
    // a directory, outside any repository, that isn't a checkout
    std::fs::create_dir_all(world.tmp.path().join("nowhere")).expect("mkdir");
    checkouts.push(Checkout {
        path: world.tmp.path().join("nowhere"),
        project: "org/nowhere".into(),
        branch: "main".into(),
        default_cloudmap: false,
        pulled: None,
    });
    let scans = AtomicUsize::new(0);
    let cm = serve(
        checkouts.clone(),
        url,
        Some(ScanOptions::default()),
        // the first checkout's scan is refused
        |_| scans.fetch_add(1, Ordering::SeqCst) > 0,
        |_, _| String::new(),
        None,
        unfurl_git_sync::git::Trust::Reduced,
    )
    .await
    .expect("serving")
    .expect("checkouts");
    for (c, served) in [
        (&checkouts[0], false),
        (&checkouts[1], true),
        (&checkouts[2], false),
    ] {
        let check = cm
            .project_check(Some(&c.project), None, false, &Default::default(), None)
            .await
            .expect("check");
        assert_eq!(
            matches!(check, ProjectCheck::Serve(_)),
            served,
            "{}",
            c.project
        );
    }
}

/// Startup's clone waits for the checkout's git lock, held as python
/// holds it while it clones there, and clones once it's released. Needs
/// `UNFURL_TEST_REDIS_URL`.
#[tokio::test]
async fn a_clone_waits_for_its_git_lock() {
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;
    use unfurl_server::gitlock::GitLocks;
    let Some(url) = std::env::var("UNFURL_TEST_REDIS_URL")
        .ok()
        .filter(|u| !u.is_empty())
    else {
        eprintln!("skip: UNFURL_TEST_REDIS_URL not set");
        return;
    };
    let mut conn = redis::Client::open(url.as_str())
        .expect("redis url")
        .get_multiplexed_async_connection()
        .await
        .expect("connect");
    let world = World::new().await;
    let prefix = format!("test-clone-lock-{}:", std::process::id());
    std::fs::create_dir_all(world.root()).expect("mkdir");
    let root = world.root().canonicalize().expect("real");
    let key = format!(
        "{prefix}_git_lock::{}",
        root.join("public/org/proj/main").display()
    );
    let _: () = redis::cmd("SET")
        .arg(&key)
        .arg(1)
        .arg("PX")
        .arg(10_000)
        .query_async(&mut conn)
        .await
        .expect("set");
    let released = Arc::new(AtomicBool::new(false));
    let (flag, mut holder, held) = (released.clone(), conn.clone(), key.clone());
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        flag.store(true, Ordering::SeqCst);
        let _: () = redis::AsyncCommands::del(&mut holder, &held)
            .await
            .expect("del");
    });

    let locks = GitLocks::new(conn.clone(), &prefix);
    let got = prepare(
        &world.db,
        &world.root(),
        &world.server(),
        Some(&locks),
        None,
        unfurl_git_sync::git::Trust::Reduced,
        &[],
    )
    .await;
    assert_eq!(got.len(), 1);
    assert!(
        released.load(Ordering::SeqCst),
        "cloned while the lock was held"
    );
    let left: bool = redis::AsyncCommands::exists(&mut conn, &key)
        .await
        .expect("exists");
    assert!(!left, "the lock is let go");
}

/// Startup's scan of a checkout, and its finishing the exports left
/// unfinished, wait for the checkout's git lock, held as python holds it
/// while it pulls or writes there. Needs `UNFURL_TEST_REDIS_URL`.
#[tokio::test]
async fn a_scan_waits_for_its_git_lock() {
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;
    use unfurl_server::gitlock::GitLocks;
    let Some(url) = std::env::var("UNFURL_TEST_REDIS_URL")
        .ok()
        .filter(|u| !u.is_empty())
    else {
        eprintln!("skip: UNFURL_TEST_REDIS_URL not set");
        return;
    };
    let mut conn = redis::Client::open(url.as_str())
        .expect("redis url")
        .get_multiplexed_async_connection()
        .await
        .expect("connect");
    let world = World::new().await;
    let checkouts = world.prepare().await;
    let prefix = format!("test-scan-lock-{}:", std::process::id());
    let key = format!(
        "{prefix}_git_lock::{}",
        world.public().canonicalize().expect("real").display()
    );
    let _: () = redis::cmd("SET")
        .arg(&key)
        .arg(1)
        .arg("PX")
        .arg(10_000)
        .query_async(&mut conn)
        .await
        .expect("set");
    let released = Arc::new(AtomicBool::new(false));
    let (flag, mut holder, held) = (released.clone(), conn.clone(), key.clone());
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        flag.store(true, Ordering::SeqCst);
        let _: () = redis::AsyncCommands::del(&mut holder, &held)
            .await
            .expect("del");
    });

    let DbConfig::Sqlite { url } = &world.db else {
        unreachable!()
    };
    let cm = serve(
        checkouts,
        url,
        Some(ScanOptions::default()),
        |_| true,
        |_, _| String::new(),
        Some(GitLocks::new(conn.clone(), &prefix)),
        unfurl_git_sync::git::Trust::Reduced,
    )
    .await
    .expect("serve")
    .expect("served");
    assert!(
        released.load(Ordering::SeqCst),
        "scanned while the lock was held"
    );
    let left: bool = redis::AsyncCommands::exists(&mut conn, &key)
        .await
        .expect("exists");
    assert!(!left, "the lock is let go");

    // the state served takes it too, for its writes' commits
    let _: () = redis::cmd("SET")
        .arg(&key)
        .arg(1)
        .arg("PX")
        .arg(10_000)
        .query_async(&mut conn)
        .await
        .expect("set");
    let busy = cm.lock_git(std::time::Duration::from_millis(300)).await;
    let _: () = redis::AsyncCommands::del(&mut conn, &key)
        .await
        .expect("del");
    assert!(busy.is_err(), "the served state's commits take no lock");
}

/// Opening a checkout the startup clone brought up records the default
/// branch the project's remote has, and records it again after a pull
/// finds the remote's changed.
#[tokio::test]
async fn the_default_branch_is_recorded() {
    let world = World::new().await;
    let bare = world.tmp.path().join("server/org/proj.git");
    git(&bare, &["branch", "trunk", "main"]);
    git(&bare, &["symbolic-ref", "HEAD", "refs/heads/trunk"]);
    let recorded = || async {
        let db = unfurl_git_sync::Db::connect(&world.db)
            .await
            .expect("connect");
        db.worktrees(&Default::default()).await.expect("worktrees")[0]
            .default_branch
            .clone()
    };

    let DbConfig::Sqlite { url } = &world.db else {
        unreachable!()
    };
    let open = || async {
        serve(
            world.prepare().await,
            url,
            Some(ScanOptions::default()),
            |_| true,
            |_, _| String::new(),
            None,
            unfurl_git_sync::git::Trust::Reduced,
        )
        .await
        .expect("serve")
        .expect("served")
    };

    open().await;
    assert_eq!(recorded().await.as_deref(), Some("trunk"));
    git(&bare, &["symbolic-ref", "HEAD", "refs/heads/main"]);
    open().await;
    assert_eq!(recorded().await.as_deref(), Some("main"), "followed");
}

impl World {
    async fn prepare_trusting(&self, trust: unfurl_git_sync::git::Trust) -> Vec<Checkout> {
        prepare(
            &self.db,
            &self.root(),
            &self.server(),
            None,
            None,
            trust,
            &[],
        )
        .await
    }
}

/// A hook a checkout's own config points at, left there by another writer
/// of the clone root, doesn't run on the startup pull unless the
/// checkouts are trusted.
#[tokio::test]
async fn a_checkouts_hooks_run_only_when_trusted() {
    use unfurl_git_sync::git::Trust;
    for (trust, runs) in [(Trust::Reduced, false), (Trust::Full, true)] {
        let world = World::new().await;
        world.prepare().await;
        let hooks = world.tmp.path().join("hooks");
        let ran = world.tmp.path().join("ran");
        std::fs::create_dir(&hooks).expect("mkdir");
        let hook = hooks.join("post-merge");
        std::fs::write(&hook, format!("#!/bin/sh\ntouch '{}'\n", ran.display())).expect("write");
        std::fs::set_permissions(&hook, std::os::unix::fs::PermissionsExt::from_mode(0o755))
            .expect("chmod");
        git(
            &world.public(),
            &["config", "core.hooksPath", hooks.to_str().unwrap()],
        );
        world.push_upstream("later");

        assert_eq!(world.prepare_trusting(trust).await.len(), 1, "{trust:?}");
        assert!(world.public().join("later").exists(), "pulled ({trust:?})");
        assert_eq!(ran.exists(), runs, "{trust:?}");
    }
}

/// A checkout whose own config redirects git, which no override undoes, is
/// left out unless the checkouts are trusted.
#[tokio::test]
async fn a_checkout_redirected_by_its_config_is_left_out_unless_trusted() {
    use unfurl_git_sync::git::Trust;
    for (key, value) in [
        (
            "url.https://evil.example/.insteadOf",
            "https://unfurl.cloud/",
        ),
        ("filter.evil.smudge", "cat"),
        ("http.https://unfurl.cloud/.extraHeader", "X-Evil: 1"),
    ] {
        for (trust, served) in [(Trust::Reduced, 0), (Trust::Full, 1)] {
            let world = World::new().await;
            world.prepare().await;
            git(&world.public(), &["config", key, value]);
            assert_eq!(
                world.prepare_trusting(trust).await.len(),
                served,
                "{key} ({trust:?})"
            );
        }
    }
}

/// What leaves a checkout redirected, in a test.
type Redirect = fn(&World);

/// A checkout redirected otherwise than by a plain config key -- a
/// url-specific http setting, a setting in its worktree config, a `.git`
/// file pointing elsewhere, alternate object stores -- is left out too
/// unless the checkouts are trusted.
#[tokio::test]
async fn a_checkout_redirected_otherwise_is_left_out_unless_trusted() {
    use unfurl_git_sync::git::Trust;
    let cases: [(&str, Redirect); 4] = [
        ("http.<url>.proxy", |w| {
            git(
                &w.public(),
                &[
                    "config",
                    "http.https://unfurl.cloud/.proxy",
                    "http://127.0.0.1:9",
                ],
            );
        }),
        ("config.worktree", |w| {
            git(
                &w.public(),
                &["config", "extensions.worktreeConfig", "true"],
            );
            git(
                &w.public(),
                &["config", "--worktree", "filter.evil.smudge", "cat"],
            );
        }),
        (".git file", |w| {
            let elsewhere = w.tmp.path().join("elsewhere.git");
            std::fs::rename(w.public().join(".git"), &elsewhere).expect("move");
            std::fs::write(
                w.public().join(".git"),
                format!("gitdir: {}\n", elsewhere.display()),
            )
            .expect("write");
        }),
        ("alternates", |w| {
            let other = w.tmp.path().join("server/org/proj.git/objects");
            std::fs::write(
                w.public().join(".git/objects/info/alternates"),
                format!("{}\n", other.display()),
            )
            .expect("write");
        }),
    ];
    for (case, redirect) in cases {
        for (trust, served) in [(Trust::Reduced, 0), (Trust::Full, 1)] {
            let world = World::new().await;
            world.prepare().await;
            redirect(&world);
            assert_eq!(
                world.prepare_trusting(trust).await.len(),
                served,
                "{case} ({trust:?})"
            );
        }
    }
}

impl World {
    /// A project `project` on the server, not tracked by the database, with
    /// a cloudmap on `head`, its default branch.
    fn host(&self, project: &str, head: &str) {
        let bare = self.tmp.path().join(format!("server/{project}.git"));
        std::fs::create_dir_all(&bare).expect("mkdir");
        git(&bare, &["init", "-q", "--bare"]);
        let seed = self.tmp.path().join("seed").join(project);
        std::fs::create_dir_all(&seed).expect("mkdir");
        git(&seed, &["init", "-q", "-b", head]);
        std::fs::write(seed.join("cloudmap.yaml"), "repositories: {}\n").expect("write");
        git(&seed, &["add", "."]);
        git(&seed, &["commit", "-q", "-m", "seed"]);
        git(&seed, &["remote", "add", "origin", bare.to_str().unwrap()]);
        git(&seed, &["push", "-q", "origin", head]);
        git(
            &bare,
            &["symbolic-ref", "HEAD", &format!("refs/heads/{head}")],
        );
    }

    async fn prepare_with_defaults(&self, defaults: &[DefaultCloudmap]) -> Vec<Checkout> {
        prepare(
            &self.db,
            &self.root(),
            &self.server(),
            None,
            None,
            unfurl_git_sync::git::Trust::Reduced,
            defaults,
        )
        .await
    }

    async fn serve_all(&self, checkouts: Vec<Checkout>) -> unfurl_server::cloudmap::CloudMapState {
        let DbConfig::Sqlite { url } = &self.db else {
            unreachable!()
        };
        serve(
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
        .expect("served")
    }

    /// The database's worktrees, as `(origin, branch)`, by id.
    async fn worktrees(&self) -> Vec<(i64, String, String)> {
        let db = unfurl_git_sync::Db::connect(&self.db)
            .await
            .expect("connect");
        db.worktrees(&Default::default())
            .await
            .expect("worktrees")
            .into_iter()
            .map(|w| (w.id, w.origin, w.branch))
            .collect()
    }
}

/// A default cloudmap repository the database doesn't track is cloned at startup,
/// on its remote's default branch when the listing names none; opening
/// it adds its worktree row, and a read naming no project reads it alone.
#[tokio::test]
async fn a_default_cloudmap_is_cloned_and_read_without_a_project() {
    use unfurl_server::cloudmap::ReadTarget;
    let world = World::new().await;
    world.host("org/default", "trunk");
    let checkouts = world
        .prepare_with_defaults(&[DefaultCloudmap::parse("org/default").expect("parse")])
        .await;
    let defaults: Vec<_> = checkouts
        .iter()
        .map(|c| (c.project.as_str(), c.branch.as_str(), c.default_cloudmap))
        .collect();
    assert_eq!(
        defaults,
        [("org/proj", "main", false), ("org/default", "trunk", true)]
    );

    let cm = world.serve_all(checkouts).await;
    let row = world
        .worktrees()
        .await
        .into_iter()
        .find(|(_, origin, branch)| origin.ends_with("/org/default") && branch == "trunk")
        .expect("a worktree row for the default cloudmap's checkout");
    let read = cm
        .resolve_read(
            None,
            None,
            false,
            &world.server(),
            &Default::default(),
            None,
        )
        .await
        .expect("resolve");
    let ReadTarget::Read(Some(filter)) = read else {
        panic!("not read here: {read:?}");
    };
    assert_eq!(filter.ids, Some(vec![row.0]));
}

/// A default cloudmap repository whose checkout is on disk already, with no row in
/// the database, is pulled rather than cloned, and gets its row.
#[tokio::test]
async fn a_default_cloudmaps_checkout_on_disk_is_pulled_and_given_its_row() {
    let world = World::new().await;
    world.host("org/there", "main");
    let path = world.root().join("public/org/there/main");
    std::fs::create_dir_all(path.parent().unwrap()).expect("mkdir");
    git(
        world.tmp.path(),
        &[
            "clone",
            "-q",
            &format!("{}/org/there.git", world.server()),
            path.to_str().unwrap(),
        ],
    );
    let marker = path.join(".git/not-recloned");
    std::fs::write(&marker, "").expect("write");

    let checkouts = world
        .prepare_with_defaults(&[DefaultCloudmap::parse("org/there#main").expect("parse")])
        .await;
    assert!(checkouts
        .iter()
        .any(|c| c.default_cloudmap && c.path == path));
    assert!(marker.exists(), "cloned again rather than pulled");
    world.serve_all(checkouts).await;
    assert!(world
        .worktrees()
        .await
        .iter()
        .any(|(_, origin, branch)| origin.ends_with("/org/there") && branch == "main"));
}

/// A clone root inside a repository whose config would redirect git -- a
/// url rewrite of the server's to nowhere -- doesn't affect the startup
/// clone: git never reads the config of a repository above where it runs.
#[tokio::test]
async fn a_repository_around_the_clone_root_is_ignored() {
    let world = World::new().await;
    world.host("org/default", "trunk");
    git(world.tmp.path(), &["init", "-q"]);
    git(
        world.tmp.path(),
        &[
            "config",
            "url.file:///nowhere/.insteadOf",
            &format!("{}/", world.server()),
        ],
    );
    let checkouts = world
        .prepare_with_defaults(&[DefaultCloudmap::parse("org/default").expect("parse")])
        .await;
    let got: Vec<_> = checkouts
        .iter()
        .map(|c| (c.project.as_str(), c.branch.as_str()))
        .collect();
    assert_eq!(got, [("org/proj", "main"), ("org/default", "trunk")]);
}

impl World {
    /// The checkouts startup brings up, served, with a cloner for the rest.
    async fn serve_with_cloner(&self) -> unfurl_server::cloudmap::CloudMapState {
        let served = self.serve_all(self.prepare().await).await;
        let cloner = unfurl_server::clone::Cloner::new(
            served.synced().db().clone(),
            &self.root(),
            &self.server(),
            None,
            None,
            unfurl_git_sync::git::Trust::Reduced,
            |_| true,
        );
        served.with_cloner(cloner)
    }

    /// [`Self::serve_with_cloner`], pulling what it serves when its last
    /// pull is `timeout` seconds old.
    async fn serve_fresh(&self, timeout: i64) -> unfurl_server::cloudmap::CloudMapState {
        self.serve_pushing(timeout, 60).await
    }

    /// [`Self::serve_fresh`], pushing no more often than every
    /// `push_interval` seconds.
    async fn serve_pushing(
        &self,
        timeout: i64,
        push_interval: u64,
    ) -> unfurl_server::cloudmap::CloudMapState {
        let settings = unfurl_server::fresh::Settings {
            timeout,
            cache_key_prefix: "test:".into(),
            cloud_server: self.server(),
            clone_root: Some(self.root()),
            grants: None,
            locks: None,
            trust: unfurl_git_sync::git::Trust::Reduced,
            push_interval,
            record_ttl: 0,
        };
        let fresh = unfurl_server::fresh::Freshener::new(None, settings, |_| true);
        self.serve_with_cloner().await.with_freshener(fresh)
    }

    /// Rewrite the server's `main` to a commit unrelated to its history.
    fn rewrite_upstream(&self) -> String {
        let seed = self.tmp.path().join("seed/org/proj");
        git(&seed, &["checkout", "-q", "--orphan", "rewritten"]);
        std::fs::write(
            seed.join("cloudmap.yaml"),
            "repositories: {}\n# rewritten\n",
        )
        .expect("write");
        git(&seed, &["add", "."]);
        git(&seed, &["commit", "-q", "-m", "rewritten"]);
        git(&seed, &["push", "-q", "-f", "origin", "rewritten:main"]);
        git(&seed, &["rev-parse", "HEAD"])
    }
}

/// `cm`, serving `org/proj`'s checkout for a request with no credentials
/// and `latest_commit`.
async fn serve_proj(
    cm: &unfurl_server::cloudmap::CloudMapState,
    latest_commit: Option<&str>,
) -> unfurl_server::cloudmap::CloudMapState {
    let check = cm
        .project_check(
            Some("org/proj"),
            None,
            false,
            &Default::default(),
            latest_commit,
        )
        .await
        .expect("check");
    let ProjectCheck::Serve(served) = check else {
        panic!("not served");
    };
    served
}

/// The HEAD `cm`'s database records for the checkout it serves.
async fn head(cm: &unfurl_server::cloudmap::CloudMapState) -> String {
    cm.head_commit().await.expect("head").expect("a commit")
}

/// Wait for `done` to hold, up to ten seconds.
async fn eventually(mut done: impl AsyncFnMut() -> bool) -> bool {
    for _ in 0..200 {
        if done().await {
            return true;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    false
}

/// A write, and a read, naming a project with no checkout bring one up --
/// cloned and opened, its worktree in the database -- and are served from
/// it; the rest of the time, as from any checkout.
#[tokio::test]
async fn a_request_for_a_project_with_no_checkout_brings_one_up() {
    use unfurl_server::cloudmap::ReadTarget;
    let world = World::new().await;
    world.host("org/other", "main");
    let cm = world.serve_with_cloner().await;
    assert_eq!(cm.served().len(), 1);

    let check = cm
        .project_check(Some("org/other"), None, false, &Default::default(), None)
        .await
        .expect("check");
    assert!(matches!(check, ProjectCheck::Serve(_)));
    assert!(world.root().join("public/org/other/main/.git").exists());
    assert_eq!(cm.served().len(), 2);

    // a read first brings one up too
    world.host("org/read", "main");
    let read = cm
        .resolve_read(
            Some("org/read"),
            None,
            false,
            &world.server(),
            &Default::default(),
            None,
        )
        .await
        .expect("resolve");
    assert!(matches!(read, ReadTarget::Read(Some(_))), "{read:?}");
    assert!(world.root().join("public/org/read/main/.git").exists());
    assert_eq!(cm.served().len(), 3);
}

/// Two first requests for one project at once bring it up once.
#[tokio::test]
async fn concurrent_first_requests_bring_a_checkout_up_once() {
    let world = World::new().await;
    world.host("org/other", "main");
    let cm = world.serve_with_cloner().await;
    let anonymous = Default::default();
    let (a, b) = tokio::join!(
        cm.project_check(Some("org/other"), None, false, &anonymous, None),
        cm.project_check(Some("org/other"), None, false, &anonymous, None),
    );
    assert!(matches!(a.expect("a"), ProjectCheck::Serve(_)));
    assert!(matches!(b.expect("b"), ProjectCheck::Serve(_)));
    assert_eq!(cm.served().len(), 2);
}

/// A project or branch from a request that isn't a path beneath the clone
/// root is never brought up.
#[tokio::test]
async fn a_hostile_project_or_branch_isnt_brought_up() {
    let world = World::new().await;
    let cm = world.serve_with_cloner().await;
    // `x/../main` would reach the checkout of org/proj on main
    for (project, branch) in [
        ("../org/proj", None),
        ("org/proj", Some("x/../main")),
        ("org/proj", Some("../../x")),
        ("org/x", Some("/etc")),
    ] {
        let check = cm
            .project_check(Some(project), branch, false, &Default::default(), None)
            .await
            .expect("check");
        assert!(
            !matches!(check, ProjectCheck::Serve(_)),
            "{project} {branch:?}"
        );
    }
    assert_eq!(cm.served().len(), 1);
}

/// No gate is kept once a bring-up is done, or for a request never
/// brought up: what requests name can't grow them.
#[tokio::test]
async fn no_gate_outlives_its_bring_up() {
    let world = World::new().await;
    world.host("org/other", "main");
    let cm = world.serve_with_cloner().await;
    let anonymous = Default::default();
    for (project, branch) in [
        ("org/other", None),
        ("org/proj", Some("../x")),
        ("org/nope", Some("b")),
    ] {
        let _ = cm
            .project_check(Some(project), branch, false, &anonymous, None)
            .await;
    }
    assert_eq!(cm.gates_kept(), 0);
}

/// A request private without a secret -- a user alone, or a visibility
/// other than public -- has its checkout brought up under private/, as
/// python puts its clone, anonymously.
#[tokio::test]
async fn a_private_request_without_a_secret_clones_under_private() {
    let world = World::new().await;
    world.host("org/other", "main");
    let cm = world.serve_with_cloner().await;
    let private = unfurl_server::clone::RequestAccess {
        private: true,
        credentials: None,
        ..Default::default()
    };
    let check = cm
        .project_check(Some("org/other"), None, false, &private, None)
        .await
        .expect("check");
    assert!(matches!(check, ProjectCheck::Serve(_)));
    assert!(world.root().join("private/org/other/main/.git").exists());
    assert!(!world.root().join("public/org/other").exists());
}

/// A request naming a branch of a served project that isn't served
/// brings that branch up.
#[tokio::test]
async fn a_new_branch_of_a_served_project_is_brought_up() {
    let world = World::new().await;
    git(
        &world.tmp.path().join("server/org/proj.git"),
        &["branch", "dev", "main"],
    );
    let cm = world.serve_with_cloner().await;
    let check = cm
        .project_check(
            Some("org/proj"),
            Some("dev"),
            false,
            &Default::default(),
            None,
        )
        .await
        .expect("check");
    assert!(matches!(check, ProjectCheck::Serve(_)));
    assert!(world.root().join("public/org/proj/dev/.git").exists());
    assert_eq!(cm.served().len(), 2);
}

/// A checkout brought up whose scan the server refuses, as at startup,
/// isn't served.
#[tokio::test]
async fn a_checkout_whose_scan_is_refused_isnt_served() {
    let world = World::new().await;
    world.host("org/other", "main");
    let served = world.serve_all(world.prepare().await).await;
    let cloner = unfurl_server::clone::Cloner::new(
        served.synced().db().clone(),
        &world.root(),
        &world.server(),
        None,
        None,
        unfurl_git_sync::git::Trust::Reduced,
        |_| false,
    );
    let cm = served.with_cloner(cloner);
    let check = cm
        .project_check(Some("org/other"), None, false, &Default::default(), None)
        .await
        .expect("check");
    assert!(!matches!(check, ProjectCheck::Serve(_)));
    assert_eq!(cm.served().len(), 1);
}

/// A request finding its checkout's last pull too old is served at once,
/// and the checkout is pulled in the background and rescanned.
#[tokio::test]
async fn a_stale_checkout_is_pulled_in_the_background() {
    let world = World::new().await;
    let cm = world.serve_fresh(0).await;
    let new = world.push_upstream("later");
    let served = serve_proj(&cm, None).await;
    assert!(eventually(async || head(&served).await == new).await);
    assert_eq!(cm.pulls_started(), 1);
}

/// A checkout pulled within the timeout isn't pulled again.
#[tokio::test]
async fn a_checkout_pulled_recently_isnt_pulled_again() {
    let world = World::new().await;
    let cm = world.serve_fresh(3600).await;
    let first = world.push_upstream("later");
    // never pulled by this server: pulled now
    let served = serve_proj(&cm, None).await;
    assert!(eventually(async || head(&served).await == first).await);
    world.push_upstream("later still");
    serve_proj(&cm, None).await;
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    assert_eq!(cm.pulls_started(), 1);
    assert_eq!(head(&served).await, first);
}

/// A request naming a commit its checkout lacks has it pulled first,
/// however recent the last pull.
#[tokio::test]
async fn a_request_naming_a_missing_commit_is_pulled_first() {
    let world = World::new().await;
    let cm = world.serve_fresh(3600).await;
    let first = world.push_upstream("first");
    let served = serve_proj(&cm, None).await;
    // the background pull done, and recorded
    assert!(eventually(async || head(&served).await == first).await);
    tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    let new = world.push_upstream("later");
    let served = serve_proj(&cm, Some(&new)).await;
    assert_eq!(head(&served).await, new);
    // one it has, or one dirty, isn't pulled for
    let pulls = cm.pulls_started();
    serve_proj(&cm, Some(&format!("{new}-dirty"))).await;
    assert_eq!(cm.pulls_started(), pulls);
}

/// Requests finding a checkout stale together pull it once.
#[tokio::test]
async fn concurrent_stale_requests_pull_once() {
    let world = World::new().await;
    let cm = world.serve_fresh(3600).await;
    world.push_upstream("later");
    let (a, b) = tokio::join!(serve_proj(&cm, None), serve_proj(&cm, None));
    drop((a, b));
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    assert_eq!(cm.pulls_started(), 1);
}

/// A checkout that can't fast-forward to its remote, rewritten upstream,
/// is reset to it when it holds no work the remote lacks.
#[tokio::test]
async fn a_checkout_with_nothing_unpushed_is_reset_to_a_rewritten_remote() {
    let world = World::new().await;
    let cm = world.serve_fresh(0).await;
    let rewritten = world.rewrite_upstream();
    let served = serve_proj(&cm, None).await;
    assert!(eventually(async || head(&served).await == rewritten).await);
}

/// One holding a commit its remote lacks is pushed before it's pulled;
/// its remote having moved on, the commit goes to a new branch there, and
/// the checkout is reset to its remote.
#[tokio::test]
async fn a_commit_its_remote_moved_on_from_is_diverted() {
    let world = World::new().await;
    let cm = world.serve_fresh(0).await;
    std::fs::write(world.public().join("mine.txt"), "mine").expect("write");
    git(&world.public(), &["add", "."]);
    git(&world.public(), &["commit", "-q", "-m", "mine"]);
    let mine = git(&world.public(), &["rev-parse", "HEAD"]);
    let rewritten = world.rewrite_upstream();
    serve_proj(&cm, None).await;
    assert!(eventually(async || git(&world.public(), &["rev-parse", "HEAD"]) == rewritten).await);
    let bare = world.tmp.path().join("server/org/proj.git");
    let diverted = format!("refs/heads/git-sync/diverted-{}", &mine[..12]);
    assert_eq!(git(&bare, &["rev-parse", &diverted]), mine);
}

/// A broken checkout is moved aside, not deleted, and cloned again.
#[tokio::test]
async fn a_broken_checkout_is_moved_aside_and_cloned_again() {
    let world = World::new().await;
    let cm = world.serve_fresh(0).await;
    let upstream = world.push_upstream("later");
    std::fs::remove_file(world.public().join(".git/HEAD")).expect("break it");
    let served = serve_proj(&cm, None).await;
    assert!(eventually(async || head(&served).await == upstream).await);
    let parent = world.public().parent().unwrap().to_path_buf();
    let aside: Vec<_> = std::fs::read_dir(&parent)
        .expect("read")
        .filter_map(|e| e.ok())
        .filter(|e| e.file_name().to_string_lossy().starts_with("main.broken-"))
        .collect();
    assert_eq!(aside.len(), 1, "moved aside");
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), upstream);
}

/// A checkout whose remote can't be reached is served as it is, kept,
/// and not pulled again until the timeout.
#[tokio::test]
async fn a_checkout_whose_remote_fails_is_kept() {
    let world = World::new().await;
    let cm = world.serve_fresh(3600).await;
    let before = git(&world.public(), &["rev-parse", "HEAD"]);
    let bare = world.tmp.path().join("server/org/proj.git");
    std::fs::rename(&bare, bare.with_extension("gone")).expect("hide");
    serve_proj(&cm, None).await;
    assert!(eventually(async || cm.pulls_started() == 1).await);
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), before);
    serve_proj(&cm, None).await;
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    assert_eq!(cm.pulls_started(), 1, "not again until the timeout");
}

/// A checkout brought up for a request isn't pulled again at once.
#[tokio::test]
async fn a_checkout_brought_up_isnt_pulled_again() {
    let world = World::new().await;
    world.host("org/other", "main");
    let cm = world.serve_fresh(3600).await;
    for _ in 0..2 {
        cm.project_check(Some("org/other"), None, false, &Default::default(), None)
            .await
            .expect("check");
    }
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    assert_eq!(cm.pulls_started(), 0);
}

/// A read finding its project's checkout stale has it pulled too, and one
/// naming no project the default cloudmaps.
#[tokio::test]
async fn a_read_pulls_what_it_reads() {
    let world = World::new().await;
    let cm = world.serve_fresh(0).await;
    let new = world.push_upstream("later");
    cm.resolve_read(
        Some("org/proj"),
        None,
        false,
        &world.server(),
        &Default::default(),
        None,
    )
    .await
    .expect("read");
    assert!(eventually(async || head(&cm).await == new).await);
}

/// The pull record is kept in python's cache, where python reads it: under
/// its key for the clone, as cachelib stores a `(time, outcome)` tuple.
/// Needs `UNFURL_TEST_REDIS_URL`.
#[tokio::test]
async fn the_pull_record_is_pythons() {
    use redis::AsyncCommands;
    let Some(url) = std::env::var("UNFURL_TEST_REDIS_URL")
        .ok()
        .filter(|u| !u.is_empty())
    else {
        eprintln!("skip: UNFURL_TEST_REDIS_URL not set");
        return;
    };
    let mut conn = redis::Client::open(url.as_str())
        .expect("url")
        .get_multiplexed_async_connection()
        .await
        .expect("connect");
    let world = World::new().await;
    let prefix = format!("test-fresh-{}:", std::process::id());
    let settings = unfurl_server::fresh::Settings {
        timeout: 3600,
        cache_key_prefix: prefix.clone(),
        cloud_server: world.server(),
        clone_root: Some(world.root()),
        grants: None,
        locks: None,
        trust: unfurl_git_sync::git::Trust::Reduced,
        push_interval: 60,
        record_ttl: 0,
    };
    let fresh = unfurl_server::fresh::Freshener::new(Some(conn.clone()), settings, |_| true);
    let cm = world.serve_with_cloner().await.with_freshener(fresh);
    serve_proj(&cm, None).await;
    let key = format!("{prefix}org/proj:pull:{}", world.public().display());
    let mut raw: Option<Vec<u8>> = None;
    for _ in 0..200 {
        raw = conn.get(&key).await.expect("get");
        if raw.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let _: () = conn.del(&key).await.expect("del");
    let raw = raw.expect("a pull record");
    let payload = raw.strip_prefix(b"!").expect("cachelib's marker");
    let record = serde_pickle::value_from_slice(payload, Default::default()).expect("record");
    let serde_pickle::Value::Tuple(items) = record else {
        panic!("{record:?}");
    };
    assert!(matches!(items[0], serde_pickle::Value::F64(_)), "{items:?}");
    assert_eq!(items[1], serde_pickle::Value::String("pulled".into()));
}

/// A pull waiting on the git lock, which python holds to pull the same
/// clone, doesn't pull again after python has. Needs
/// `UNFURL_TEST_REDIS_URL`.
#[tokio::test]
async fn a_pull_python_made_while_waiting_isnt_made_again() {
    use redis::AsyncCommands;
    use serde_pickle::{SerOptions, Value};
    let Some(url) = std::env::var("UNFURL_TEST_REDIS_URL")
        .ok()
        .filter(|u| !u.is_empty())
    else {
        eprintln!("skip: UNFURL_TEST_REDIS_URL not set");
        return;
    };
    let conn = redis::Client::open(url.as_str())
        .expect("url")
        .get_multiplexed_async_connection()
        .await
        .expect("connect");
    let world = World::new().await;
    let prefix = format!("test-fresh-wait-{}:", std::process::id());
    let locks = unfurl_server::gitlock::GitLocks::new(conn.clone(), &prefix);
    let settings = unfurl_server::fresh::Settings {
        timeout: 3600,
        cache_key_prefix: prefix.clone(),
        cloud_server: world.server(),
        clone_root: Some(world.root()),
        grants: None,
        locks: Some(locks.clone()),
        trust: unfurl_git_sync::git::Trust::Reduced,
        push_interval: 60,
        record_ttl: 0,
    };
    let fresh = unfurl_server::fresh::Freshener::new(Some(conn.clone()), settings, |_| true);
    let cm = world.serve_with_cloner().await.with_freshener(fresh);
    // python, pulling
    let held = locks
        .lock(&world.public(), std::time::Duration::from_secs(5))
        .await
        .expect("lock")
        .expect("held");
    serve_proj(&cm, None).await;
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    let key = format!("{prefix}org/proj:pull:{}", world.public().display());
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64();
    let record = Value::Tuple(vec![Value::F64(now), Value::String("pulled".into())]);
    let mut raw = b"!".to_vec();
    raw.extend(serde_pickle::value_to_vec(&record, SerOptions::new()).expect("record"));
    let _: () = conn.clone().set(&key, raw).await.expect("set");
    held.release().await;
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    let _: () = conn.clone().del(&key).await.expect("del");
    assert_eq!(cm.pulls_started(), 0);
}

/// At startup too, a checkout that can't fast-forward to its rewritten
/// remote is reset to it when it holds no work the remote lacks.
#[tokio::test]
async fn startup_resets_a_checkout_to_a_rewritten_remote() {
    let world = World::new().await;
    world.prepare().await;
    let rewritten = world.rewrite_upstream();
    world.prepare().await;
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), rewritten);
}

/// But not one holding records the database hasn't committed.
#[tokio::test]
async fn startup_leaves_a_checkout_with_uncommitted_records() {
    let world = World::new().await;
    let cm = world.serve_all(world.prepare().await).await;
    let before = git(&world.public(), &["rev-parse", "HEAD"]);
    cm.synced()
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
    world.rewrite_upstream();
    world.prepare().await;
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), before);
}

/// A refresh refuses a checkout whose own git config it distrusts, rather
/// than taking it for broken or pulling into it.
#[tokio::test]
async fn a_pull_refuses_a_distrusted_config() {
    let world = World::new().await;
    let cm = world.serve_fresh(0).await;
    let before = git(&world.public(), &["rev-parse", "HEAD"]);
    git(&world.public(), &["config", "filter.x.clean", "false"]);
    world.push_upstream("later");
    serve_proj(&cm, None).await;
    assert!(eventually(async || cm.pulls_started() == 1).await);
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), before);
    assert!(world.public().join(".git").exists(), "not moved aside");
}

/// A checkout whose remote can't be reached at startup is served as it
/// is.
#[tokio::test]
async fn startup_serves_a_checkout_whose_remote_is_down() {
    let world = World::new().await;
    world.prepare().await;
    let bare = world.tmp.path().join("server/org/proj.git");
    std::fs::rename(&bare, bare.with_extension("gone")).expect("hide");
    let got = world.prepare().await;
    assert_eq!(got.len(), 1);
    assert_eq!(got[0].path, world.public());
}

/// A checkout with uncommitted changes in its working tree, even ones its
/// own config hides from `git status`, isn't reset to a rewritten remote.
#[tokio::test]
async fn a_checkout_with_changes_in_its_working_tree_isnt_reset() {
    let world = World::new().await;
    let cm = world.serve_fresh(0).await;
    git(
        &world.public(),
        &["config", "status.showUntrackedFiles", "no"],
    );
    std::fs::write(world.public().join("mine.txt"), "mine").expect("write");
    let before = git(&world.public(), &["rev-parse", "HEAD"]);
    world.rewrite_upstream();
    serve_proj(&cm, None).await;
    assert!(eventually(async || cm.pulls_started() == 1).await);
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), before);
    assert!(world.public().join("mine.txt").exists());
}

/// Nor is one whose database holds records not yet committed.
#[tokio::test]
async fn a_checkout_with_uncommitted_records_isnt_reset() {
    let world = World::new().await;
    let cm = world.serve_fresh(0).await;
    let before = git(&world.public(), &["rev-parse", "HEAD"]);
    cm.synced()
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
    world.rewrite_upstream();
    serve_proj(&cm, None).await;
    assert!(eventually(async || cm.pulls_started() == 1).await);
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), before);
}

/// A request naming a commit its checkout lacks is still served when the
/// pull for it fails.
#[tokio::test]
async fn a_request_whose_pull_fails_is_served() {
    let world = World::new().await;
    let cm = world.serve_fresh(3600).await;
    let bare = world.tmp.path().join("server/org/proj.git");
    std::fs::rename(&bare, bare.with_extension("gone")).expect("hide");
    let missing = "0123456789abcdef0123456789abcdef01234567";
    let served = serve_proj(&cm, Some(missing)).await;
    assert_eq!(cm.pulls_started(), 1);
    assert!(!head(&served).await.is_empty());
}

/// A checkout broken where other branches' checkouts sit below it isn't
/// moved aside, taking them with it.
#[tokio::test]
async fn a_broken_checkout_holding_others_isnt_moved() {
    let world = World::new().await;
    let cm = world.serve_fresh(0).await;
    std::fs::remove_dir_all(world.public().join(".git")).expect("break it");
    let nested = world.public().join("1.0");
    git(
        world.tmp.path(),
        &[
            "clone",
            "-q",
            &format!("{}/org/proj.git", world.server()),
            nested.to_str().unwrap(),
        ],
    );
    serve_proj(&cm, None).await;
    assert!(eventually(async || cm.pulls_started() == 1).await);
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    assert!(nested.join(".git").exists(), "left where it is");
}

/// The configured checkout is freshened for a read of its own project,
/// never another's.
#[tokio::test]
async fn a_configured_checkout_is_freshened_for_its_own_project_only() {
    let world = World::new().await;
    world.prepare().await;
    let DbConfig::Sqlite { url } = &world.db else {
        unreachable!()
    };
    let (cm, _) = unfurl_server::cloudmap::CloudMapState::open(
        world.public().to_str().unwrap(),
        url,
        Some(ScanOptions::default()),
    )
    .await
    .expect("open");
    let settings = unfurl_server::fresh::Settings {
        timeout: 0,
        cache_key_prefix: "test:".into(),
        cloud_server: world.server(),
        clone_root: None,
        grants: None,
        locks: None,
        trust: unfurl_git_sync::git::Trust::Full,
        push_interval: 60,
        record_ttl: 0,
    };
    let cm = cm.with_freshener(unfurl_server::fresh::Freshener::new(None, settings, |_| {
        true
    }));
    let read = async |project: &str| {
        cm.resolve_read(
            Some(project),
            None,
            false,
            &world.server(),
            &Default::default(),
            None,
        )
        .await
        .expect("read");
    };
    read("org/other").await;
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    assert_eq!(cm.pulls_started(), 0);
    read("org/proj").await;
    assert!(eventually(async || cm.pulls_started() == 1).await);
}

/// A checkout python's record says it never pulls isn't pulled for a
/// commit it lacks either. Needs `UNFURL_TEST_REDIS_URL`.
#[tokio::test]
async fn a_detached_checkout_isnt_pulled_for_a_missing_commit() {
    use redis::AsyncCommands;
    use serde_pickle::{SerOptions, Value};
    let Some(url) = std::env::var("UNFURL_TEST_REDIS_URL")
        .ok()
        .filter(|u| !u.is_empty())
    else {
        eprintln!("skip: UNFURL_TEST_REDIS_URL not set");
        return;
    };
    let conn = redis::Client::open(url.as_str())
        .expect("url")
        .get_multiplexed_async_connection()
        .await
        .expect("connect");
    let world = World::new().await;
    let prefix = format!("test-fresh-detached-{}:", std::process::id());
    let settings = unfurl_server::fresh::Settings {
        timeout: 3600,
        cache_key_prefix: prefix.clone(),
        cloud_server: world.server(),
        clone_root: Some(world.root()),
        grants: None,
        locks: None,
        trust: unfurl_git_sync::git::Trust::Reduced,
        push_interval: 60,
        record_ttl: 0,
    };
    let fresh = unfurl_server::fresh::Freshener::new(Some(conn.clone()), settings, |_| true);
    let cm = world.serve_with_cloner().await.with_freshener(fresh);
    let key = format!("{prefix}org/proj:pull:{}", world.public().display());
    let record = Value::Tuple(vec![Value::F64(0.0), Value::String("detached".into())]);
    let mut raw = b"!".to_vec();
    raw.extend(serde_pickle::value_to_vec(&record, SerOptions::new()).expect("record"));
    let _: () = conn.clone().set(&key, raw).await.expect("set");
    let new = world.push_upstream("later");
    serve_proj(&cm, Some(&new)).await;
    let _: () = conn.clone().del(&key).await.expect("del");
    assert_eq!(cm.pulls_started(), 0);
}

/// A record written and committed in `cm`'s checkout, as a write with
/// `commit: true` does, and handed to its background push: the commit.
async fn write_and_commit(cm: &unfurl_server::cloudmap::CloudMapState, key: &str) -> String {
    cm.synced()
        .create_record(
            Some("cloudmap.yaml"),
            "/repositories",
            &format!("git://example.com/{key}.git"),
            serde_json::json!({"name": key}),
            None,
            false,
        )
        .await
        .expect("create");
    // as a write commits: holding the checkout, which pulls and pushes do
    let held = cm.hold_checkout(std::time::Duration::from_secs(30)).await;
    let commit = cm.commit(key).await.expect("commit").expect("a commit");
    cm.committed(&commit, &Default::default()).await;
    drop(held);
    commit
}

/// The server's `main`.
fn upstream(world: &World) -> String {
    git(
        &world.tmp.path().join("server/org/proj.git"),
        &["rev-parse", "main"],
    )
}

/// A write's commit is pushed in the background, its outcome recorded for
/// `/events`.
#[tokio::test]
async fn a_commit_is_pushed_in_the_background() {
    let world = World::new().await;
    let cm = serve_proj(&world.serve_pushing(3600, 60).await, None).await;
    // the pull a checkout never pulled gets, done
    drop(cm.hold_checkout(std::time::Duration::from_secs(30)).await);
    let commit = write_and_commit(&cm, "mine").await;
    let pushed = async || {
        cm.push_outcome(&commit)
            .await
            .is_some_and(|o| o.contains("pushed"))
    };
    assert!(eventually(pushed).await);
    assert_eq!(upstream(&world), commit);
    assert_eq!(cm.pushes_started(), 1);
}

/// Commits made within the push interval of the last push wait for it, and
/// go in one push.
#[tokio::test]
async fn pushes_are_no_more_often_than_the_interval() {
    let world = World::new().await;
    let cm = serve_proj(&world.serve_pushing(3600, 2).await, None).await;
    // the pull a checkout never pulled gets, done
    drop(cm.hold_checkout(std::time::Duration::from_secs(30)).await);
    let first = write_and_commit(&cm, "first").await;
    assert!(eventually(async || upstream(&world) == first).await);
    let _second = write_and_commit(&cm, "second").await;
    let third = write_and_commit(&cm, "third").await;
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    assert_eq!(upstream(&world), first, "not yet");
    assert!(cm
        .push_outcome(&third)
        .await
        .is_some_and(|o| o.contains("pending")));
    assert!(eventually(async || upstream(&world) == third).await);
    // counted once each push is done
    assert!(eventually(async || cm.pushes_started() == 2).await);
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    assert_eq!(cm.pushes_started(), 2);
}

/// A push its remote refuses leaves the commit on a local branch, never
/// pushed, and the checkout reset to its remote.
#[tokio::test]
async fn a_refused_push_is_set_aside() {
    let world = World::new().await;
    let cm = serve_proj(&world.serve_pushing(3600, 0).await, None).await;
    // the pull a checkout never pulled gets, done
    drop(cm.hold_checkout(std::time::Duration::from_secs(30)).await);
    let bare = world.tmp.path().join("server/org/proj.git");
    let hook = bare.join("hooks/pre-receive");
    std::fs::write(&hook, "#!/bin/sh\nexit 1\n").expect("hook");
    std::fs::set_permissions(&hook, std::os::unix::fs::PermissionsExt::from_mode(0o755))
        .expect("chmod");
    let before = upstream(&world);
    let commit = write_and_commit(&cm, "refused").await;
    let refused = async || {
        cm.push_outcome(&commit)
            .await
            .is_some_and(|o| o.contains("refused"))
    };
    assert!(eventually(refused).await);
    let outcome = cm.push_outcome(&commit).await.expect("a push record");
    assert!(outcome.contains("message"), "the reason: {outcome}");
    let aside = format!("git-sync/refused-{}", &commit[..12]);
    assert_eq!(git(&world.public(), &["rev-parse", &aside]), commit);
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), before);
    // a later push carries only its own
    std::fs::remove_file(&hook).expect("unhook");
    let next = write_and_commit(&cm, "next").await;
    assert!(eventually(async || upstream(&world) == next).await);
    let remote_refs = git(&bare, &["for-each-ref", "--format=%(refname)"]);
    assert!(!remote_refs.contains("refused"), "{remote_refs}");
    let pushed = git(&bare, &["rev-list", "main"]);
    assert!(!pushed.contains(&commit), "the refused commit stayed here");
}

/// A write's commit its remote has moved on from, with changes that merge
/// cleanly, is merged and pushed; the checkout follows.
#[tokio::test]
async fn a_commit_behind_its_remote_is_merged_when_it_merges_cleanly() {
    let world = World::new().await;
    let cm = serve_proj(&world.serve_pushing(3600, 0).await, None).await;
    // the pull a checkout never pulled gets, done
    drop(cm.hold_checkout(std::time::Duration::from_secs(30)).await);
    let theirs = world.push_upstream("theirs");
    let commit = write_and_commit(&cm, "mine").await;
    let pushed = async || {
        cm.push_outcome(&commit)
            .await
            .is_some_and(|o| o.contains("pushed"))
    };
    assert!(eventually(pushed).await);
    let merge = upstream(&world);
    let bare = world.tmp.path().join("server/org/proj.git");
    let parents = git(&bare, &["rev-list", "--parents", "-n", "1", &merge]);
    assert_eq!(parents, format!("{merge} {commit} {theirs}"));
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), merge);
    assert!(world.public().join("theirs").exists());
    // the database took the merge in
    assert!(eventually(async || head(&cm).await == merge).await);
}

/// One that conflicts goes to a new branch there instead, and the
/// checkout is reset to its remote, with nothing of the merge on disk.
#[tokio::test]
async fn a_commit_behind_its_remote_that_conflicts_is_diverted() {
    let world = World::new().await;
    let cm = serve_proj(&world.serve_pushing(3600, 0).await, None).await;
    // the pull a checkout never pulled gets, done
    drop(cm.hold_checkout(std::time::Duration::from_secs(30)).await);
    let seed = world.tmp.path().join("seed/org/proj");
    std::fs::write(seed.join("cloudmap.yaml"), "repositories:\n  theirs: {}\n").expect("write");
    git(&seed, &["commit", "-q", "-am", "theirs"]);
    git(&seed, &["push", "-q", "origin", "main"]);
    let theirs = git(&seed, &["rev-parse", "HEAD"]);
    let commit = write_and_commit(&cm, "mine").await;
    let diverted = async || {
        cm.push_outcome(&commit)
            .await
            .is_some_and(|o| o.contains("diverted"))
    };
    assert!(eventually(diverted).await);
    let bare = world.tmp.path().join("server/org/proj.git");
    let branch = format!("refs/heads/git-sync/diverted-{}", &commit[..12]);
    assert_eq!(git(&bare, &["rev-parse", &branch]), commit);
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), theirs);
    let on_disk = std::fs::read_to_string(world.public().join("cloudmap.yaml")).expect("read");
    assert!(!on_disk.contains("<<<<<<<"), "{on_disk}");
}

/// One its remote's branch rejects -- protected, say -- goes to a new
/// branch there too.
#[tokio::test]
async fn a_commit_a_protected_branch_rejects_is_diverted() {
    let world = World::new().await;
    let cm = serve_proj(&world.serve_pushing(3600, 0).await, None).await;
    // the pull a checkout never pulled gets, done
    drop(cm.hold_checkout(std::time::Duration::from_secs(30)).await);
    let bare = world.tmp.path().join("server/org/proj.git");
    let hook = bare.join("hooks/pre-receive");
    let script = "#!/bin/sh\nwhile read old new name; do\n  [ \"$name\" = refs/heads/main ] && exit 1\ndone\nexit 0\n";
    std::fs::write(&hook, script).expect("hook");
    std::fs::set_permissions(&hook, std::os::unix::fs::PermissionsExt::from_mode(0o755))
        .expect("chmod");
    let before = upstream(&world);
    let commit = write_and_commit(&cm, "mine").await;
    let diverted = async || {
        cm.push_outcome(&commit)
            .await
            .is_some_and(|o| o.contains("diverted"))
    };
    assert!(eventually(diverted).await);
    let branch = format!("refs/heads/git-sync/diverted-{}", &commit[..12]);
    assert_eq!(git(&bare, &["rev-parse", &branch]), commit);
    assert_eq!(upstream(&world), before);
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), before);
    assert!(eventually(async || head(&cm).await == before).await);
}

/// A `git-sync/` branch made in the checkout, as an export makes one, goes
/// with the next push.
#[tokio::test]
async fn a_git_sync_branch_is_pushed_too() {
    let world = World::new().await;
    let cm = serve_proj(&world.serve_pushing(3600, 0).await, None).await;
    // the pull a checkout never pulled gets, done
    drop(cm.hold_checkout(std::time::Duration::from_secs(30)).await);
    git(&world.public(), &["branch", "git-sync/exported", "HEAD"]);
    let commit = write_and_commit(&cm, "mine").await;
    assert!(eventually(async || upstream(&world) == commit).await);
    let bare = world.tmp.path().join("server/org/proj.git");
    let exported = git(
        &bare,
        &[
            "for-each-ref",
            "--format=%(refname)",
            "refs/heads/git-sync/",
        ],
    );
    assert_eq!(exported, "refs/heads/git-sync/exported");
}

/// A pull that finds nothing to push doesn't count as a push: a commit
/// right after it is pushed at once.
#[tokio::test]
async fn a_pull_with_nothing_to_push_doesnt_hold_up_a_commit() {
    let world = World::new().await;
    let cm = world.serve_pushing(0, 60).await;
    let served = serve_proj(&cm, None).await;
    assert!(eventually(async || cm.pulls_started() == 1).await);
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    let commit = write_and_commit(&served, "mine").await;
    assert!(eventually(async || upstream(&world) == commit).await);
}

/// A push records the branch's head where python records it, for
/// `/events` branch watches. Needs `UNFURL_TEST_REDIS_URL`.
#[tokio::test]
async fn a_push_records_the_branch_head() {
    use redis::AsyncCommands;
    let Some(url) = std::env::var("UNFURL_TEST_REDIS_URL")
        .ok()
        .filter(|u| !u.is_empty())
    else {
        eprintln!("skip: UNFURL_TEST_REDIS_URL not set");
        return;
    };
    let conn = redis::Client::open(url.as_str())
        .expect("url")
        .get_multiplexed_async_connection()
        .await
        .expect("connect");
    let world = World::new().await;
    let prefix = format!("test-push-head-{}:", std::process::id());
    let settings = unfurl_server::fresh::Settings {
        timeout: 3600,
        cache_key_prefix: prefix.clone(),
        cloud_server: world.server(),
        clone_root: Some(world.root()),
        grants: None,
        locks: None,
        trust: unfurl_git_sync::git::Trust::Reduced,
        push_interval: 0,
        record_ttl: 60,
    };
    let fresh = unfurl_server::fresh::Freshener::new(Some(conn.clone()), settings, |_| true);
    let cm = world.serve_with_cloner().await.with_freshener(fresh);
    let served = serve_proj(&cm, None).await;
    let commit = write_and_commit(&served, "mine").await;
    assert!(eventually(async || upstream(&world) == commit).await);
    let head_key = unfurl_server::config::head_key(&prefix, "org/proj", "main");
    let push_key = unfurl_server::config::push_key(&prefix, "org/proj", "main", &commit);
    let mut head: Option<String> = None;
    for _ in 0..100 {
        head = conn.clone().get(&head_key).await.expect("get");
        if head.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let ttl: i64 = conn.clone().ttl(&head_key).await.expect("ttl");
    let _: () = conn
        .clone()
        .del(&[&head_key, &push_key])
        .await
        .expect("del");
    assert_eq!(head.as_deref(), Some(commit.as_str()));
    assert!(ttl > 0 && ttl <= 60, "{ttl}");
}

/// A divert completes though the database holds records staged since the
/// commit, which the reset doesn't touch: they stay staged, to commit
/// onto the remote's branch.
#[tokio::test]
async fn a_divert_keeps_records_staged_since() {
    let world = World::new().await;
    let cm = serve_proj(&world.serve_pushing(3600, 0).await, None).await;
    // the pull a checkout never pulled gets, done
    drop(cm.hold_checkout(std::time::Duration::from_secs(30)).await);
    let seed = world.tmp.path().join("seed/org/proj");
    std::fs::write(seed.join("cloudmap.yaml"), "repositories:\n  theirs: {}\n").expect("write");
    git(&seed, &["commit", "-q", "-am", "theirs"]);
    git(&seed, &["push", "-q", "origin", "main"]);
    let theirs = git(&seed, &["rev-parse", "HEAD"]);
    let create = async |key: &str| {
        cm.synced()
            .create_record(
                Some("cloudmap.yaml"),
                "/repositories",
                &format!("git://example.com/{key}.git"),
                serde_json::json!({"name": key}),
                None,
                false,
            )
            .await
            .expect("create");
    };
    create("mine").await;
    let commit = cm.commit("mine").await.expect("commit").expect("a commit");
    create("staged").await;
    cm.committed(&commit, &Default::default()).await;
    let diverted = async || {
        cm.push_outcome(&commit)
            .await
            .is_some_and(|o| o.contains("diverted"))
    };
    assert!(eventually(diverted).await);
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), theirs);
    assert!(eventually(async || head(&cm).await == theirs).await);
    assert!(
        cm.synced().has_uncommitted().await.expect("uncommitted"),
        "still staged"
    );
}

/// A checkout in the clone root whose remote names another project, for
/// pushing or fetching, isn't pulled: its credentials are for its own.
#[tokio::test]
async fn a_checkout_whose_remote_names_another_project_isnt_pulled() {
    let world = World::new().await;
    let cm = world.serve_fresh(0).await;
    let before = git(&world.public(), &["rev-parse", "HEAD"]);
    let other = format!("{}/org/other.git", world.server());
    git(
        &world.public(),
        &["config", "remote.origin.pushurl", &other],
    );
    world.push_upstream("later");
    serve_proj(&cm, None).await;
    assert!(eventually(async || cm.pulls_started() == 1).await);
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), before);
}

/// A write through the handler, in clone mode, has its commit pushed
/// after it answers.
#[tokio::test]
async fn a_write_through_the_handler_is_pushed() {
    use axum::routing::post;
    use clap::Parser;
    use tower::util::ServiceExt;
    let world = World::new().await;
    let cm = world.serve_pushing(3600, 0).await;
    let served = serve_proj(&cm, None).await;
    drop(
        served
            .hold_checkout(std::time::Duration::from_secs(30))
            .await,
    );
    // where a record the write creates goes
    let file = Some("cloudmap.yaml");
    served
        .synced()
        .set_default_file_path(file)
        .await
        .expect("default");
    let config = unfurl_server::config::Config::parse_from([
        "unfurl-server",
        "--cloud-server",
        &world.server(),
    ]);
    let state = unfurl_server::AppState {
        config: std::sync::Arc::new(config),
        client: reqwest::Client::new(),
        redis: None,
        cloudmap: Some(cm),
        grants: None,
    };
    let app = axum::Router::new()
        .route(
            "/cloudmap",
            post(unfurl_server::cloudmap::post_cloudmap_local),
        )
        .with_state(state);
    let body = serde_json::json!({
        "commit": true,
        "repositories": { "git://example.com/mine.git": { "name": "mine" } },
    });
    let req = axum::http::Request::builder()
        .method("POST")
        .uri("/cloudmap?auth_project=org/proj")
        .header("content-type", "application/json")
        .body(axum::body::Body::from(body.to_string()))
        .unwrap();
    let resp = app.oneshot(req).await.expect("handler");
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .expect("body");
    let response: serde_json::Value = serde_json::from_slice(&bytes).expect("json");
    assert_eq!(status, axum::http::StatusCode::OK, "{response}");
    let commit = response["commit"].as_str().expect("a commit").to_string();
    assert!(eventually(async || upstream(&world) == commit).await);
}

/// A checkout whose working tree holds changes isn't diverted: a reset
/// would lose them. Nothing is pushed or kept, and it's tried again.
#[tokio::test]
async fn a_checkout_with_changes_on_disk_isnt_diverted() {
    let world = World::new().await;
    let cm = serve_proj(&world.serve_pushing(3600, 1).await, None).await;
    drop(cm.hold_checkout(std::time::Duration::from_secs(30)).await);
    let seed = world.tmp.path().join("seed/org/proj");
    std::fs::write(seed.join("cloudmap.yaml"), "repositories:\n  theirs: {}\n").expect("write");
    git(&seed, &["commit", "-q", "-am", "theirs"]);
    git(&seed, &["push", "-q", "origin", "main"]);
    let theirs = git(&seed, &["rev-parse", "HEAD"]);
    std::fs::write(world.public().join("mine.txt"), "mine").expect("write");
    let commit = write_and_commit(&cm, "mine").await;
    assert!(eventually(async || cm.pushes_started() >= 1).await);
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    let bare = world.tmp.path().join("server/org/proj.git");
    let branches = git(&bare, &["for-each-ref", "--format=%(refname)"]);
    assert!(!branches.contains("git-sync/"), "{branches}");
    assert_eq!(git(&world.public(), &["rev-parse", "HEAD"]), commit);
    assert!(world.public().join("mine.txt").exists());
    assert_eq!(upstream(&world), theirs);
    // tried again on its own, the interval after
    assert!(eventually(async || cm.pushes_started() >= 2).await);
}

/// A checkout on another branch than its own isn't pushed: its HEAD would
/// go onto its branch.
#[tokio::test]
async fn a_checkout_on_another_branch_isnt_pushed() {
    let world = World::new().await;
    let cm = serve_proj(&world.serve_pushing(3600, 0).await, None).await;
    drop(cm.hold_checkout(std::time::Duration::from_secs(30)).await);
    let before = upstream(&world);
    // a commit on its branch, made before it went to another, as python
    // could: git-sync commits only on the branch it tracks
    cm.synced()
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
    let commit = cm.commit("mine").await.expect("commit").expect("a commit");
    // tracking main: without the check, its HEAD would go onto main
    git(
        &world.public(),
        &[
            "checkout",
            "-q",
            "-b",
            "elsewhere",
            "--track",
            "origin/main",
        ],
    );
    cm.committed(&commit, &Default::default()).await;
    assert!(eventually(async || cm.pushes_started() >= 1).await);
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    assert_eq!(upstream(&world), before);
    let pending = cm.push_outcome(&commit).await.expect("a push record");
    assert!(pending.contains("pending"), "{pending}");
    // nor tried again on its own, until someone changes it
    tokio::time::sleep(std::time::Duration::from_millis(2500)).await;
    assert_eq!(cm.pushes_started(), 1);
}

/// A checkout whose push url names another project isn't pushed there.
#[tokio::test]
async fn a_push_to_another_project_is_refused() {
    let world = World::new().await;
    world.host("org/other", "main");
    let cm = serve_proj(&world.serve_pushing(3600, 0).await, None).await;
    drop(cm.hold_checkout(std::time::Duration::from_secs(30)).await);
    let other = world.tmp.path().join("server/org/other.git");
    let other_url = format!("{}/org/other.git", world.server());
    git(
        &world.public(),
        &["config", "remote.origin.pushurl", &other_url],
    );
    let before = git(&other, &["rev-parse", "main"]);
    write_and_commit(&cm, "mine").await;
    assert!(eventually(async || cm.pushes_started() >= 1).await);
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    assert_eq!(git(&other, &["rev-parse", "main"]), before);
    let refs = git(&other, &["for-each-ref", "--format=%(refname)"]);
    assert!(!refs.contains("git-sync/"), "{refs}");
}

/// A push that can't reach the remote is tried again on its own, the
/// interval after, without another write or pull.
#[tokio::test]
async fn a_failed_push_is_tried_again() {
    let world = World::new().await;
    let cm = serve_proj(&world.serve_pushing(3600, 1).await, None).await;
    drop(cm.hold_checkout(std::time::Duration::from_secs(30)).await);
    let bare = world.tmp.path().join("server/org/proj.git");
    let hidden = bare.with_extension("gone");
    std::fs::rename(&bare, &hidden).expect("hide");
    let commit = write_and_commit(&cm, "mine").await;
    assert!(eventually(async || cm.pushes_started() >= 1).await);
    std::fs::rename(&hidden, &bare).expect("restore");
    assert!(eventually(async || upstream(&world) == commit).await);
}

/// The configured checkout, the operator's own, isn't pushed by a pull
/// either, though it holds commits its remote lacks.
#[tokio::test]
async fn a_pull_doesnt_push_the_configured_checkout() {
    let world = World::new().await;
    world.prepare().await;
    let DbConfig::Sqlite { url } = &world.db else {
        unreachable!()
    };
    let (cm, _) = unfurl_server::cloudmap::CloudMapState::open(
        world.public().to_str().unwrap(),
        url,
        Some(ScanOptions::default()),
    )
    .await
    .expect("open");
    let settings = unfurl_server::fresh::Settings {
        timeout: 0,
        cache_key_prefix: "test:".into(),
        cloud_server: world.server(),
        clone_root: None,
        grants: None,
        locks: None,
        trust: unfurl_git_sync::git::Trust::Full,
        push_interval: 0,
        record_ttl: 0,
    };
    let cm = cm.with_freshener(unfurl_server::fresh::Freshener::new(None, settings, |_| {
        true
    }));
    let before = upstream(&world);
    std::fs::write(world.public().join("mine.txt"), "mine").expect("write");
    git(&world.public(), &["add", "."]);
    git(&world.public(), &["commit", "-q", "-m", "mine"]);
    cm.resolve_read(
        Some("org/proj"),
        None,
        false,
        &world.server(),
        &Default::default(),
        None,
    )
    .await
    .expect("read");
    assert!(eventually(async || cm.pulls_started() == 1).await);
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    assert_eq!(upstream(&world), before);
    assert_eq!(cm.pushes_started(), 0);
}
