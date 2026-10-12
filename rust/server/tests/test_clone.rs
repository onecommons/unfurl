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

/// A directory where the checkout goes that isn't one: left out, rather
/// than pulled into.
#[tokio::test]
async fn a_directory_that_isnt_a_checkout_is_left_out() {
    let world = World::new().await;
    std::fs::create_dir_all(world.public()).expect("mkdir");
    assert!(world.prepare().await.is_empty());
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
            .project_check(Some(&c.project), None, false)
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
            .project_check(Some(&c.project), None, false)
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
        .resolve_read(None, None, false, &world.server())
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
