// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Test helpers shared across the integration test files.

#![allow(dead_code, unused_imports, unused_macros)]

use std::path::Path;

use tempfile::TempDir;
use unfurl_git_sync::{DbConfig, FormatRegistry, SyncedRepo};

/// Initialise a fresh git repository at `path` and commit
/// `expected_cloudmap.yaml` (copied from this crate's fixtures dir) as
/// `cloudmap.yaml`.
pub async fn init_repo_with_fixture(path: &Path) -> gix::ObjectId {
    let fixture = std::fs::read(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/expected_cloudmap.yaml"),
    )
    .expect("fixture exists");

    unfurl_git_sync::git::init_with_files(
        path,
        &[("cloudmap.yaml".to_string(), fixture)],
        "initial",
    )
    .expect("init repo")
}

/// Spin up an in-memory SQLite-backed `SyncedRepo` over a fresh temp git
/// repo seeded with the cloudmap fixture. Returns `(sync, tempdir)` so
/// the caller controls when the dir is dropped.
pub async fn sqlite_fixture() -> (SyncedRepo, TempDir) {
    let tmp = tempfile::tempdir().expect("tempdir");
    init_repo_with_fixture(tmp.path()).await;

    let sync = SyncedRepo::open(
        tmp.path(),
        DbConfig::Sqlite {
            url: "sqlite::memory:".into(),
        },
        FormatRegistry::with_builtins(),
    )
    .await
    .expect("open SyncedRepo");
    (sync, tmp)
}

// ---------------------------------------------------------------------------
// Postgres parameterisation
// ---------------------------------------------------------------------------
//
// Tests opt-in by calling [`pg_fixture`]. It returns `None` when
// `UNFURL_TEST_PG_URL` is unset, so the same test source compiles for the
// `--no-default-features` SQLite-only build.

#[cfg(feature = "postgres")]
mod pg {
    use super::*;
    use sqlx::Executor;

    pub fn url() -> Option<String> {
        std::env::var("UNFURL_TEST_PG_URL").ok()
    }

    /// Per-test Postgres scope. Each scope is backed by a unique schema
    /// (`unfurl_test_<uuid>`) so concurrent test runs don't collide.
    pub struct PgScope {
        base_url: String,
        schema: String,
    }

    impl PgScope {
        pub async fn setup() -> Option<Self> {
            let base_url = url()?;
            let schema = format!("unfurl_test_{}", uuid::Uuid::new_v4().simple());
            let pool = sqlx::PgPool::connect(&base_url).await.expect("connect");
            pool.execute(&*format!("CREATE SCHEMA \"{}\"", schema))
                .await
                .expect("create schema");
            Some(Self { base_url, schema })
        }

        pub fn db_config(&self) -> DbConfig {
            let connector = if self.base_url.contains('?') {
                "&"
            } else {
                "?"
            };
            DbConfig::Postgres {
                url: format!(
                    "{}{}options=-c%20search_path%3D{}",
                    self.base_url, connector, self.schema
                ),
            }
        }

        pub async fn teardown(self) {
            let pool = sqlx::PgPool::connect(&self.base_url)
                .await
                .expect("connect");
            pool.execute(&*format!("DROP SCHEMA \"{}\" CASCADE", self.schema))
                .await
                .expect("drop schema");
        }
    }

    pub async fn pg_fixture() -> Option<(SyncedRepo, TempDir, PgScope)> {
        let scope = PgScope::setup().await?;
        let tmp = tempfile::tempdir().expect("tempdir");
        init_repo_with_fixture(tmp.path()).await;

        let sync = SyncedRepo::open(
            tmp.path(),
            scope.db_config(),
            FormatRegistry::with_builtins(),
        )
        .await
        .expect("open SyncedRepo");
        Some((sync, tmp, scope))
    }
}

#[cfg(feature = "postgres")]
pub use pg::{pg_fixture, PgScope};

// When the postgres feature is off, tests still compile by skipping the
// pg arm at runtime (each test calls `pg_fixture` and returns when it
// gets `None`). Provide a no-op stub so call sites don't need cfg.
#[cfg(not(feature = "postgres"))]
pub struct PgScope;

#[cfg(not(feature = "postgres"))]
impl PgScope {
    pub async fn teardown(self) {}
}

#[cfg(not(feature = "postgres"))]
pub async fn pg_fixture() -> Option<(SyncedRepo, TempDir, PgScope)> {
    None
}

/// Run `args` as a git subcommand in `dir`, failing the test on a
/// non-zero exit.
pub fn git(dir: &Path, args: &[&str]) {
    let out = std::process::Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .expect("git");
    assert!(out.status.success(), "git {args:?}: {out:?}");
}

/// Run one scenario against both backends.
///
/// `crud_test!(foo)` expects an `async fn foo(&SyncedRepo, &TempDir)`
/// beside it and generates `foo::sqlite` plus, behind the `postgres`
/// feature, `foo::postgres`. The module and the function share a name
/// on purpose -- Rust keeps them in separate namespaces, and it is one
/// name to read rather than two to keep in step.
macro_rules! crud_test {
    ($name:ident) => {
        mod $name {
            #[tokio::test]
            async fn sqlite() {
                let (sync, tmp) = crate::common::sqlite_fixture().await;
                super::$name(&sync, &tmp).await;
            }

            #[cfg(feature = "postgres")]
            #[tokio::test]
            async fn postgres() {
                let Some((sync, tmp, scope)) = crate::common::pg_fixture().await else {
                    eprintln!("skip: UNFURL_TEST_PG_URL not set");
                    return;
                };
                super::$name(&sync, &tmp).await;
                drop(sync);
                drop(tmp);
                scope.teardown().await;
            }
        }
    };
}
pub(crate) use crud_test;

/// Open a `SyncedRepo` over an existing directory and sqlite file.
pub async fn open_at(dir: &std::path::Path, db: &str) -> SyncedRepo {
    SyncedRepo::open(
        dir,
        DbConfig::Sqlite { url: db.into() },
        FormatRegistry::with_builtins(),
    )
    .await
    .expect("open SyncedRepo")
}
