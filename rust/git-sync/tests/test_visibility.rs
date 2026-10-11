// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A repository's visibility, as its worktrees read it, on both backends.

mod common;

use std::path::Path;
use unfurl_git_sync::db::{Db, DbConfig};
use unfurl_git_sync::model::{PRIVATE, PUBLIC};
use unfurl_git_sync::{FormatRegistry, SyncedRepo, WorktreeFilter};

/// Run `test` on a database tracking the scanned repositories `repos`
/// (the first of them seeded, the second not), in SQLite and, when
/// `UNFURL_TEST_PG_URL` is set, Postgres.
async fn each_backend(test: impl AsyncFn(&Db, &DbConfig, [&Path; 2])) {
    let (dir, url) = common::file_backed_fixture().await;
    run(dir.path(), DbConfig::Sqlite { url }, &test).await;
    #[cfg(feature = "postgres")]
    if let Some(scope) = common::PgScope::setup().await {
        let (dir, _) = common::file_backed_fixture().await;
        run(dir.path(), scope.db_config(), &test).await;
        scope.teardown().await;
    }
}

async fn run(dir: &Path, db: DbConfig, test: &impl AsyncFn(&Db, &DbConfig, [&Path; 2])) {
    let other = tempfile::tempdir().expect("tempdir");
    common::init_repo_with_fixture(other.path()).await;
    scan(dir, &db).await;
    test(
        &Db::connect(&db).await.expect("connect"),
        &db,
        [dir, other.path()],
    )
    .await;
}

async fn scan(repo: &Path, db: &DbConfig) {
    SyncedRepo::open(repo, db.clone(), FormatRegistry::with_builtins())
        .await
        .expect("open SyncedRepo");
}

/// Each worktree's branch and visibility, for the repository at `repo`.
async fn visibilities(db: &Db, repo: &Path) -> Vec<(String, Option<String>)> {
    let filter = WorktreeFilter {
        origin: Some(repo.display().to_string()),
        branch: None,
    };
    let mut seen: Vec<_> = db
        .worktrees(&filter)
        .await
        .expect("worktrees")
        .into_iter()
        .map(|w| (w.branch, w.visibility))
        .collect();
    seen.sort();
    seen
}

fn origin(repo: &Path) -> String {
    repo.display().to_string()
}

#[tokio::test]
async fn visibility_is_unknown_until_set_and_can_be_cleared() {
    each_backend(async |db: &Db, _, [repo, _]| {
        assert_eq!(visibilities(db, repo).await, [("main".into(), None)]);
        // in another spelling of its url
        db.set_visibility(&format!("{}/", origin(repo)), Some(PUBLIC))
            .await
            .expect("set");
        assert_eq!(
            visibilities(db, repo).await,
            [("main".into(), Some(PUBLIC.into()))]
        );
        db.set_visibility(&origin(repo), None).await.expect("clear");
        assert_eq!(visibilities(db, repo).await, [("main".into(), None)]);
    })
    .await;
}

/// Visibility is the repository's: a worktree created after it was set,
/// for a new branch or a repository not scanned until then, has it too;
/// another repository's doesn't.
#[tokio::test]
async fn a_worktree_created_later_has_its_repositorys_visibility() {
    each_backend(async |db: &Db, config: &DbConfig, [repo, unscanned]| {
        db.set_visibility(&origin(repo), Some(PRIVATE))
            .await
            .expect("set");
        db.set_visibility(&origin(unscanned), Some(PRIVATE))
            .await
            .expect("set");
        common::git(repo, &["checkout", "-q", "-b", "later"]);
        scan(repo, config).await;
        scan(unscanned, config).await;
        let private = |v: Vec<(String, Option<String>)>| {
            v.into_iter().all(|(_, v)| v.as_deref() == Some(PRIVATE))
        };
        let branches = visibilities(db, repo).await;
        assert_eq!(branches.len(), 2, "{branches:?}");
        assert!(private(branches));
        assert!(private(visibilities(db, unscanned).await));

        let third = tempfile::tempdir().expect("tempdir");
        common::init_repo_with_fixture(third.path()).await;
        scan(third.path(), config).await;
        assert_eq!(
            visibilities(db, third.path()).await,
            [("main".into(), None)]
        );
    })
    .await;
}

/// Anything else would read as public.
#[tokio::test]
async fn only_public_or_private_is_recorded() {
    each_backend(async |db: &Db, _, [repo, _]| {
        assert!(db
            .set_visibility(&origin(repo), Some("privat"))
            .await
            .is_err());
    })
    .await;
}

/// What the server's gate reads: a worktree is served unless its
/// repository is known to be private.
#[tokio::test]
async fn only_a_private_repository_is_not_public_or_unknown() {
    each_backend(async |db: &Db, _, [repo, _]| {
        let served = async || {
            let filter = WorktreeFilter {
                origin: Some(origin(repo)),
                branch: None,
            };
            db.worktrees(&filter).await.expect("worktrees")[0].is_public_or_unknown()
        };
        assert!(served().await, "unknown");
        db.set_visibility(&origin(repo), Some(PUBLIC))
            .await
            .expect("set");
        assert!(served().await, "public");
        db.set_visibility(&origin(repo), Some(PRIVATE))
            .await
            .expect("set");
        assert!(!served().await, "private");
    })
    .await;
}

/// The worktree of `repo`'s `main` branch.
async fn main_of(db: &Db, repo: &Path) -> unfurl_git_sync::Worktree {
    let filter = WorktreeFilter {
        origin: Some(origin(repo)),
        branch: Some("main".into()),
    };
    db.worktrees(&filter).await.expect("worktrees").remove(0)
}

/// A repository's default branch, like its visibility, is the
/// repository's: unknown, so `main`, until recorded; and recording one
/// leaves the other as it was.
#[tokio::test]
async fn a_default_branch_is_recorded_beside_visibility() {
    each_backend(async |db: &Db, _, [repo, _]| {
        let w = main_of(db, repo).await;
        assert_eq!(
            (w.default_branch.as_deref(), w.default_branch()),
            (None, "main")
        );

        db.set_visibility(&origin(repo), Some(PRIVATE))
            .await
            .expect("set");
        db.set_default_branch(&format!("{}/", origin(repo)), Some("trunk"))
            .await
            .expect("set");
        let w = main_of(db, repo).await;
        assert_eq!(w.default_branch(), "trunk");
        assert_eq!(w.visibility.as_deref(), Some(PRIVATE), "kept");

        db.set_visibility(&origin(repo), Some(PUBLIC))
            .await
            .expect("set");
        assert_eq!(main_of(db, repo).await.default_branch(), "trunk", "kept");
    })
    .await;
}

/// The default branch a clone recorded for its remote, from
/// `refs/remotes/origin/HEAD`; none where nothing was recorded.
#[test]
fn a_clones_default_branch_is_its_remotes_head() {
    use unfurl_git_sync::git::{open_repo, remote_default_branch};
    let run = |dir: &Path, args: &[&str]| {
        let out = std::process::Command::new("git")
            .args(args)
            .current_dir(dir)
            .output()
            .expect("git");
        assert!(out.status.success(), "{out:?}");
    };
    let tmp = tempfile::tempdir().expect("tempdir");
    let upstream = tmp.path().join("upstream");
    std::fs::create_dir(&upstream).expect("mkdir");
    run(&upstream, &["init", "-q", "-b", "trunk"]);
    run(&upstream, &["commit", "-q", "--allow-empty", "-m", "first"]);
    run(
        tmp.path(),
        &["clone", "-q", upstream.to_str().unwrap(), "clone"],
    );
    let clone = open_repo(&tmp.path().join("clone")).expect("open");
    assert_eq!(
        remote_default_branch(&clone, "origin")
            .expect("read")
            .as_deref(),
        Some("trunk")
    );
    let bare = open_repo(&upstream).expect("open");
    assert_eq!(remote_default_branch(&bare, "origin").expect("read"), None);
}

/// Opening a checkout records its repository's default branch, the one
/// its remote's HEAD named when it was cloned or last fetched from.
#[tokio::test]
async fn opening_a_checkout_records_its_default_branch() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let run = |dir: &Path, args: &[&str]| {
        let out = std::process::Command::new("git")
            .args(["-c", "user.name=t", "-c", "user.email=t@t"])
            .args(args)
            .current_dir(dir)
            .output()
            .expect("git");
        assert!(out.status.success(), "{out:?}");
    };
    let upstream = tmp.path().join("upstream");
    std::fs::create_dir(&upstream).expect("mkdir");
    run(&upstream, &["init", "-q", "-b", "trunk"]);
    std::fs::write(upstream.join("cloudmap.yaml"), "repositories: {}\n").expect("write");
    run(&upstream, &["add", "."]);
    run(&upstream, &["commit", "-q", "-m", "first"]);
    run(
        tmp.path(),
        &["clone", "-q", upstream.to_str().unwrap(), "clone"],
    );
    let db = DbConfig::Sqlite {
        url: format!(
            "sqlite://{}?mode=rwc",
            tmp.path().join("db.sqlite").display()
        ),
    };

    let sync = SyncedRepo::open(
        tmp.path().join("clone"),
        db,
        FormatRegistry::with_builtins(),
    )
    .await
    .expect("open");
    assert_eq!(
        sync.get_worktree()
            .await
            .expect("worktree")
            .default_branch
            .as_deref(),
        Some("trunk")
    );
}
