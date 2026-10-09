// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Database connection and per-dialect SQL helpers.
//!
//! [`Db`] is a dialect-tagged enum that wraps either a SQLite or a
//! Postgres connection pool. The SQL is in three layers:
//!
//! - `tables`: the tables' row types, and how a row is read back.
//! - `store::Store`: row-level statements over those types, run inside
//!   a caller's transaction. The segment code (scans, forks, writes)
//!   builds on it.
//! - [`worktree`], [`mod@file`], [`record`], [`commit`]: queries that
//!   each run in their own transaction and return [`crate::model`]
//!   types.
//!
//! SQL is written once, in SQLite's syntax, and [`sql!`] gives Postgres
//! its spelling ([`pg_write`]) at compile time. A body is written once
//! too: [`on_pool!`] runs it on either pool, and `store::Store` is
//! implemented for both backends from one body. `sqlx::Any` is avoided:
//! it would erase the dialect at runtime but couldn't express the SQL
//! differences.

use crate::error::{Error, Result};

/// Run `$body` with `$pool` bound to whichever pool `$db` holds: one
/// generic body, over [`store::Store`], for both backends.
macro_rules! on_pool {
    ($db:expr, $pool:ident => $body:expr) => {
        match $db {
            $crate::db::Db::Sqlite($pool) => $body,
            #[cfg(feature = "postgres")]
            $crate::db::Db::Postgres($pool) => $body,
        }
    };
}
pub(crate) use on_pool;

/// A backend's pool or transaction, for [`sql!`] to spell a statement for.
pub(crate) trait Backend {
    const POSTGRES: bool;
}

impl<DB: store::Store> Backend for sqlx::Pool<DB> {
    const POSTGRES: bool = DB::POSTGRES;
}

impl<DB: store::Store> Backend for sqlx::Transaction<'_, DB> {
    const POSTGRES: bool = DB::POSTGRES;
}

/// Whether `_backend` is Postgres's.
pub(crate) const fn postgres<B: Backend + ?Sized>(_backend: &B) -> bool {
    B::POSTGRES
}

/// `$sql`, a constant statement written for SQLite, spelled for the
/// backend of `$on` (a pool or a transaction).
macro_rules! sql {
    ($on:expr, $sql:expr $(,)?) => {
        if $crate::db::postgres(&*$on) {
            $crate::db::const_pg!($sql)
        } else {
            $sql
        }
    };
}

/// The Postgres spelling of `$sql`, a constant statement written for
/// SQLite, as a `&'static str` made at compile time.
macro_rules! const_pg {
    ($sql:expr) => {{
        const CONST_PG_SQL: &str = $sql;
        const CONST_PG_LEN: usize = $crate::db::pg_len(CONST_PG_SQL);
        const CONST_PG_BYTES: [u8; CONST_PG_LEN] = {
            let mut out = [0u8; CONST_PG_LEN];
            $crate::db::pg_write(CONST_PG_SQL, &mut out);
            out
        };
        const CONST_PG: &str = match ::core::str::from_utf8(&CONST_PG_BYTES) {
            Ok(s) => s,
            Err(_) => panic!("pg_write splits no character"),
        };
        CONST_PG
    }};
}
pub(crate) use const_pg;

/// The Postgres spelling of `sql`, written for SQLite, built at runtime.
pub(crate) fn pg(sql: &str) -> String {
    let mut out = vec![0u8; pg_len(sql)];
    pg_write(sql, &mut out);
    String::from_utf8(out).expect("pg_write splits no character")
}

/// `sql`, a statement written for SQLite and built at runtime, spelled
/// for `on`'s backend.
pub(crate) fn spell<B: Backend + ?Sized>(on: &B, sql: String) -> String {
    if postgres(on) {
        pg(&sql)
    } else {
        sql
    }
}

/// The length of [`pg_write`]'s spelling of `sql`.
pub(crate) const fn pg_len(sql: &str) -> usize {
    pg_write(sql, &mut [])
}

/// Write the Postgres spelling of `sql`, written for SQLite, into `out`,
/// as far as it fits; returns its full length. `?N` becomes `$N`,
/// `jsonb(?N)` becomes `$N::jsonb`, and `json(col)` becomes
/// `(col)::text`.
pub(crate) const fn pg_write(sql: &str, out: &mut [u8]) -> usize {
    let b = sql.as_bytes();
    let (mut i, mut n) = (0, 0);
    while i < b.len() {
        if starts_at(b, i, b"jsonb(?") {
            let e = digits_end(b, i + 7);
            if e > i + 7 && e < b.len() && b[e] == b')' {
                n = put(out, n, b"$");
                n = put_range(out, n, b, i + 7, e);
                n = put(out, n, b"::jsonb");
                i = e + 1;
                continue;
            }
        }
        if starts_at(b, i, b"json(") && (i == 0 || !b[i - 1].is_ascii_alphanumeric()) {
            let mut e = i + 5;
            while e < b.len() && (b[e].is_ascii_alphanumeric() || b[e] == b'_' || b[e] == b'.') {
                e += 1;
            }
            if e > i + 5 && e < b.len() && b[e] == b')' {
                n = put(out, n, b"(");
                n = put_range(out, n, b, i + 5, e);
                n = put(out, n, b")::text");
                i = e + 1;
                continue;
            }
        }
        if b[i] == b'?' {
            let e = digits_end(b, i + 1);
            if e > i + 1 {
                n = put(out, n, b"$");
                n = put_range(out, n, b, i + 1, e);
                i = e;
                continue;
            }
        }
        n = put_range(out, n, b, i, i + 1);
        i += 1;
    }
    n
}

const fn starts_at(b: &[u8], i: usize, pattern: &[u8]) -> bool {
    if b.len() - i < pattern.len() {
        return false;
    }
    let mut k = 0;
    while k < pattern.len() {
        if b[i + k] != pattern[k] {
            return false;
        }
        k += 1;
    }
    true
}

const fn digits_end(b: &[u8], from: usize) -> usize {
    let mut e = from;
    while e < b.len() && b[e].is_ascii_digit() {
        e += 1;
    }
    e
}

/// Write `bytes` into `out` at `n`, as far as it fits; returns the end.
const fn put(out: &mut [u8], n: usize, bytes: &[u8]) -> usize {
    let mut k = 0;
    while k < bytes.len() {
        if n + k < out.len() {
            out[n + k] = bytes[k];
        }
        k += 1;
    }
    n + bytes.len()
}

/// Write `b[from..to]` into `out` at `n`, as far as it fits.
const fn put_range(out: &mut [u8], n: usize, b: &[u8], from: usize, to: usize) -> usize {
    let mut k = from;
    while k < to {
        if n + k - from < out.len() {
            out[n + k - from] = b[k];
        }
        k += 1;
    }
    n + to - from
}

#[cfg(test)]
mod bench;
pub mod commit;
pub mod file;
pub mod record;
pub(crate) mod store;
pub(crate) mod tables;
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
                let opts = PgConnectOptions::from_str(url)?.options([
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

    /// Every worktree in the database `filter` matches, by id.
    pub async fn worktrees(
        &self,
        filter: &crate::model::WorktreeFilter,
    ) -> Result<Vec<crate::model::Worktree>> {
        worktree::matching(self, filter).await
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

#[cfg(test)]
mod tests {
    use super::pg;

    #[test]
    fn rewrites_for_postgres() {
        assert_eq!(
            pg("SELECT json(r.json), jsonb(?7), CAST(?10 AS TEXT) WHERE a = ?1 AND ?12"),
            "SELECT (r.json)::text, $7::jsonb, CAST($10 AS TEXT) WHERE a = $1 AND $12"
        );
        assert_eq!(
            pg("SELECT jsonb_array_length(x)"),
            "SELECT jsonb_array_length(x)"
        );
        assert_eq!(
            pg("SELECT 'ünïcode' WHERE a = ?1"),
            "SELECT 'ünïcode' WHERE a = $1"
        );
    }

    #[test]
    fn compile_time_matches_runtime() {
        const SQL: &str = "UPDATE record SET json = jsonb(?2) WHERE id = ?1 AND json(json) = ?3";
        assert_eq!(super::const_pg!(SQL), pg(SQL));
    }
}
