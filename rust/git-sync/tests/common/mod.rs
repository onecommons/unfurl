// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Test helpers shared across the integration test files.

#![allow(dead_code, unused_imports, unused_macros)]

use std::path::Path;

use tempfile::TempDir;
use unfurl_git_sync::{
    BatchOp, DbConfig, FormatRegistry, Record, RecordConflictKind, ScanOptions, SyncedRepo,
};

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

// ---------------------------------------------------------------------------
// Fixture helpers shared across the test files
// ---------------------------------------------------------------------------

pub const DASHBOARD: &str = "git://unfurl.cloud/feb20a/dashboard.git";

/// HEAD as a hex string, for asserting conflict bases.
pub async fn head_commit(sync: &SyncedRepo) -> String {
    sync.get_working_dir()
        .await
        .expect("working dir")
        .head_commit
        .expect("repo has a HEAD")
}

/// The dashboard record as the file now has it.
pub fn dashboard_on_disk(tmp: &TempDir) -> serde_json::Value {
    let doc: serde_json::Value = serde_saphyr::from_str(
        &std::fs::read_to_string(tmp.path().join("cloudmap.yaml")).expect("read"),
    )
    .expect("yaml");
    doc["repositories"][DASHBOARD].clone()
}

/// The same, in a named file.
pub fn rename_name_in(tmp: &TempDir, file: &str, from: &str, to: &str) {
    let path = tmp.path().join(file);
    let before = std::fs::read_to_string(&path).expect("read");
    let edited = before.replace(&format!("name: {from}"), &format!("name: {to}"));
    assert_ne!(edited, before, "expected `name: {from}` on disk");
    std::fs::write(&path, edited).expect("write");
}

/// Rewrite a record's `name` in `cloudmap.yaml`, by its current value.
pub fn rename_name(tmp: &TempDir, from: &str, to: &str) {
    rename_name_in(tmp, "cloudmap.yaml", from, to);
}

/// A standing ModifyModify conflict on the dashboard record: the
/// database holds `ours`, the file holds `theirs`. Returns the version
/// stamped on the client's edit, for the trailer tests.
pub async fn stand_up_conflict(sync: &SyncedRepo, tmp: &TempDir) -> i64 {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    let write = sync
        .update_record(
            Some("cloudmap.yaml"),
            "/repositories",
            DASHBOARD,
            serde_json::json!({"name": "ours"}),
            None,
            false,
        )
        .await
        .expect("update");
    rename_name(tmp, "dashboard", "theirs");
    let scan = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(scan.conflicts.len(), 1, "{scan:?}");
    assert_eq!(scan.conflicts[0].kind, RecordConflictKind::ModifyModify);
    write.version
}

/// The one conflict row this worktree has, or a failure naming what it
/// found instead.
pub async fn only_conflict(sync: &SyncedRepo) -> unfurl_git_sync::Record {
    let rows = sync.list_conflicts(None).await.expect("list_conflicts");
    assert_eq!(rows.len(), 1, "expected exactly one conflict: {rows:?}");
    rows.into_iter().next().expect("checked")
}

/// HEAD's full commit message.
pub fn head_commit_body(dir: &std::path::Path) -> String {
    let out = std::process::Command::new("git")
        .args(["log", "-1", "--format=%B"])
        .current_dir(dir)
        .output()
        .expect("git log");
    assert!(out.status.success(), "{out:?}");
    String::from_utf8_lossy(&out.stdout).to_string()
}

/// The value of a trailer on HEAD, **as git itself parses it**.
///
/// Asserting on the raw message text would pass even if the trailer
/// block were malformed -- a missing blank line makes git read the whole
/// thing as prose while a substring check happily still matches. Going
/// through `%(trailers:key=...)` is what proves the block is well-formed.
pub fn head_trailer(dir: &std::path::Path, key: &str) -> Option<String> {
    let out = std::process::Command::new("git")
        .args([
            "log",
            "-1",
            &format!("--format=%(trailers:key={key},valueonly)"),
        ])
        .current_dir(dir)
        .output()
        .expect("git log");
    assert!(out.status.success(), "{out:?}");
    let value = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (!value.is_empty()).then_some(value)
}

/// A batch upsert into `/repositories`, no OCC token.
pub fn upsert_op(key: &str) -> BatchOp {
    BatchOp::Upsert {
        file_path: Some("cloudmap.yaml".into()),
        path: "/repositories".into(),
        key: key.into(),
        json: serde_json::json!({"name": key}),
        expected: None,
        resolve: false,
    }
}
