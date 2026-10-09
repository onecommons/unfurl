// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Cloning the cloudmap database's worktrees at startup (`clone::prepare`),
//! against a bare repository standing in for the cloud server.

use std::path::{Path, PathBuf};

use tempfile::TempDir;
use unfurl_git_sync::{DbConfig, FormatRegistry, ScanOptions, SyncedRepo};
use unfurl_server::clone::{prepare, serve, Checkout};
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
        prepare(&self.db, &self.root(), &self.server()).await
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
        }]
    );
    assert!(world.public().join("cloudmap.yaml").exists());
    assert!(!world.public().with_extension("lock").exists());
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
    let got = prepare(&world.db, &world.root(), "https://elsewhere.example").await;
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
    });
    let scans = AtomicUsize::new(0);
    let cm = serve(
        checkouts.clone(),
        url,
        Some(ScanOptions::default()),
        // the first checkout's scan is refused
        |_| scans.fetch_add(1, Ordering::SeqCst) > 0,
        |_, _| String::new(),
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
