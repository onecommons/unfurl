// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Database connection and per-dialect SQL helpers.
//!
//! [`Db`] is a dialect-tagged enum that wraps either a SQLite or a
//! Postgres connection pool. Submodules ([`worktree`], [`mod@file`],
//! [`record`], [`alias`], [`commit`], [`tx`]) hold the SQL helpers
//! used by [`crate::sync`]; sync code only sees these high-level
//! functions, never raw `sqlx::query` invocations.
//!
//! `sqlx::Any` is intentionally avoided — it would erase the dialect
//! at runtime but couldn't express the SQL differences (`jsonb(?)` vs
//! `$N::jsonb`, `?` vs `$1` placeholders, `INTEGER 0/1` vs `BOOLEAN
//! FALSE/TRUE`). The trade-off: every helper that holds a transaction
//! has to branch on [`Db`] once.

use crate::error::{Error, Result};

pub mod alias;
pub mod commit;
pub mod file;
pub mod record;
pub mod tx;
pub mod worktree;

/// User-facing database connection configuration.
///
/// Pass to [`Db::connect`] (or, more commonly, [`crate::SyncedRepo::open`])
/// to choose a backend. `Postgres` is gated behind the `postgres`
/// cargo feature.
#[derive(Debug, Clone)]
pub enum DbConfig {
    /// Connect to SQLite. URL must be in sqlx format, e.g.
    /// `sqlite::memory:` or `sqlite:///absolute/path.db`.
    Sqlite {
        /// Sqlx-style sqlite connection URL.
        url: String,
    },
    /// Connect to Postgres. URL is passed to sqlx unchanged.
    #[cfg(feature = "postgres")]
    Postgres {
        /// Postgres connection URL (libpq syntax).
        url: String,
    },
}

/// Concrete database handle, dialect-tagged so each helper picks the
/// right SQL.
///
/// Cheaply cloneable — both variants wrap an `Arc`-backed sqlx pool.
/// Build one via [`Db::connect`].
#[derive(Clone, Debug)]
pub enum Db {
    /// SQLite-backed pool.
    Sqlite(sqlx::Pool<sqlx::Sqlite>),
    /// Postgres-backed pool.
    #[cfg(feature = "postgres")]
    Postgres(sqlx::Pool<sqlx::Postgres>),
}

impl Db {
    /// Connect to the configured backend, run schema migrations, and
    /// (for SQLite) check that the runtime is recent enough to support
    /// JSONB (≥ 3.45).
    ///
    /// # Errors
    ///
    /// Returns [`crate::Error::Db`] for connection failures,
    /// [`crate::Error::Migrate`] if migrations fail, or
    /// [`crate::Error::Other`] when the SQLite runtime is too old.
    pub async fn connect(cfg: &DbConfig) -> Result<Self> {
        match cfg {
            DbConfig::Sqlite { url } => {
                use sqlx::sqlite::{
                    SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous,
                };
                use std::str::FromStr;
                use std::time::Duration;

                // Concurrency-friendly defaults:
                //
                // - WAL: concurrent readers + serialized writers via the
                //   write-ahead log instead of the rollback journal.
                //   Without WAL, every read blocks every write and vice
                //   versa.
                // - synchronous=NORMAL: safe with WAL and noticeably
                //   faster than FULL. Crash-recoverable; only
                //   power-cycle scenarios may lose the most recent
                //   commit (which we'd lose anyway since git would
                //   reflect the last `commit_repository`).
                // - busy_timeout=5s: the second writer waits up to 5s
                //   for the first to commit instead of erroring
                //   immediately with SQLITE_BUSY. Eliminates the
                //   "deferred-transaction upgrade collision" failure
                //   mode for typical workloads.
                let opts = SqliteConnectOptions::from_str(url)?
                    .journal_mode(SqliteJournalMode::Wal)
                    .synchronous(SqliteSynchronous::Normal)
                    .busy_timeout(Duration::from_secs(5));
                let pool = SqlitePoolOptions::new()
                    .max_connections(5)
                    .connect_with(opts)
                    .await?;

                // Need JSONB (`jsonb()` / `json()` builtins), introduced
                // in SQLite 3.45 (Jan 2024).
                check_sqlite_version(&pool).await?;

                sqlx::migrate!("./migrations/sqlite").run(&pool).await?;
                Ok(Db::Sqlite(pool))
            }
            #[cfg(feature = "postgres")]
            DbConfig::Postgres { url } => {
                use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
                use std::str::FromStr;

                // Defensive timeouts:
                //
                // - statement_timeout=30s: aborts a single statement
                //   that runs longer than 30s. Catches runaway queries
                //   before they pile up locks.
                // - lock_timeout=10s: aborts if waiting for a row /
                //   table lock takes more than 10s. Bounds the time
                //   one stuck writer can block a queue of others.
                // - idle_in_transaction_session_timeout=60s: aborts
                //   sessions that opened a transaction and went idle.
                //   Protects against a client that crashed mid-tx
                //   leaving locks held (postgres' rough equivalent of
                //   SQLite's busy_timeout for stuck-tx scenarios).
                //
                // None of these change correctness — they bound the
                // blast radius of pathological clients.
                let opts = PgConnectOptions::from_str(url)?
                    .options([
                        ("statement_timeout", "30s"),
                        ("lock_timeout", "10s"),
                        ("idle_in_transaction_session_timeout", "60s"),
                    ]);

                // Postgres scales with concurrent connections better
                // than SQLite (no global write lock), so default to a
                // bigger pool. ``UNFURL_GIT_SYNC_DB_POOL_SIZE``
                // overrides for tuning per deployment.
                let max_connections = std::env::var("UNFURL_GIT_SYNC_DB_POOL_SIZE")
                    .ok()
                    .and_then(|s| s.parse::<u32>().ok())
                    .unwrap_or(20);

                let pool = PgPoolOptions::new()
                    .max_connections(max_connections)
                    .connect_with(opts)
                    .await?;
                sqlx::migrate!("./migrations/postgres").run(&pool).await?;
                Ok(Db::Postgres(pool))
            }
        }
    }
}

async fn check_sqlite_version(pool: &sqlx::Pool<sqlx::Sqlite>) -> Result<()> {
    let row: (String,) = sqlx::query_as("SELECT sqlite_version()")
        .fetch_one(pool)
        .await?;
    let v = row.0;
    if !sqlite_version_at_least(&v, 3, 45, 0) {
        return Err(Error::Other(format!(
            "git-sync requires SQLite ≥ 3.45 for JSONB support; found {}",
            v
        )));
    }
    Ok(())
}

fn sqlite_version_at_least(version: &str, maj: u32, min: u32, patch: u32) -> bool {
    let mut parts = version.split('.').filter_map(|p| p.parse::<u32>().ok());
    let a = parts.next().unwrap_or(0);
    let b = parts.next().unwrap_or(0);
    let c = parts.next().unwrap_or(0);
    (a, b, c) >= (maj, min, patch)
}
