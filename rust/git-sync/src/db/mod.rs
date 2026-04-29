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
                use sqlx::sqlite::SqlitePoolOptions;
                let pool = SqlitePoolOptions::new()
                    .max_connections(5)
                    .connect(url)
                    .await?;

                // Need JSONB (`jsonb()` / `json()` builtins), introduced
                // in SQLite 3.45 (Jan 2024).
                check_sqlite_version(&pool).await?;

                sqlx::migrate!("./migrations/sqlite").run(&pool).await?;
                Ok(Db::Sqlite(pool))
            }
            #[cfg(feature = "postgres")]
            DbConfig::Postgres { url } => {
                use sqlx::postgres::PgPoolOptions;
                let pool = PgPoolOptions::new().max_connections(5).connect(url).await?;
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
