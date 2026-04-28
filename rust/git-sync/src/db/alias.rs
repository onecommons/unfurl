// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `alias` table writes (non-transactional).

use crate::db::Db;
use crate::error::Result;

pub(crate) async fn replace(db: &Db, record_id: i64, aliases: &[(String, String)]) -> Result<()> {
    match db {
        Db::Sqlite(pool) => {
            let mut tx = pool.begin().await?;
            sqlx::query("DELETE FROM alias WHERE record_id = ?1")
                .bind(record_id)
                .execute(&mut *tx)
                .await?;
            for (p, k) in aliases {
                sqlx::query(
                    "INSERT OR IGNORE INTO alias (record_id, path, key) VALUES (?1, ?2, ?3)",
                )
                .bind(record_id)
                .bind(p)
                .bind(k)
                .execute(&mut *tx)
                .await?;
            }
            tx.commit().await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let mut tx = pool.begin().await?;
            sqlx::query("DELETE FROM alias WHERE record_id = $1")
                .bind(record_id)
                .execute(&mut *tx)
                .await?;
            for (p, k) in aliases {
                sqlx::query(
                    "INSERT INTO alias (record_id, path, key) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                )
                .bind(record_id)
                .bind(p)
                .bind(k)
                .execute(&mut *tx)
                .await?;
            }
            tx.commit().await?;
        }
    }
    Ok(())
}
