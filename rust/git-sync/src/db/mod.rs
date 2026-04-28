// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Database connection / dialect abstraction.
//!
//! A dialect-tagged enum (`Db::Sqlite | Db::Postgres`) with:
//!
//! - per-dialect SQL where the syntax differs (`jsonb(?)` vs `$N::jsonb`,
//!   bind placeholders `?` vs `$1`, etc.);
//! - feature-gated compilation so a build with only the `sqlite` feature
//!   never pulls in postgres deps.

use crate::error::{Error, Result};

pub mod alias;
pub mod commit;
pub mod file;
pub mod record;
pub mod tx;
pub mod worktree;

/// User-facing connection configuration.
#[derive(Debug, Clone)]
pub enum DbConfig {
    /// SQLite. URL must be in sqlx format, e.g. `sqlite::memory:` or
    /// `sqlite:///absolute/path.db`.
    Sqlite { url: String },
    /// Postgres. URL passed to sqlx unchanged.
    #[cfg(feature = "postgres")]
    Postgres { url: String },
}

/// Concrete database handle. Dialect-tagged so each call site knows which
/// flavour of SQL to run.
#[derive(Clone, Debug)]
pub enum Db {
    Sqlite(sqlx::Pool<sqlx::Sqlite>),
    #[cfg(feature = "postgres")]
    Postgres(sqlx::Pool<sqlx::Postgres>),
}

impl Db {
    /// Connect, run migrations, and (for SQLite) verify the JSONB-capable
    /// minimum version.
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
