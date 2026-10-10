// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `credential_grant` table reads and writes. Grants' tokens are opaque
//! here: their caller encrypts them and keeps the key.

use crate::db::Db;
use crate::error::Result;
use crate::git::normalize_git_url_hard;
use crate::model::Grant;

/// `credential_grant`'s columns, in [`Grant`]'s order.
macro_rules! columns {
    () => {
        "id, username, origin, scopes, token, key_id, token_digest, \
         created_at, last_used_at, expires_at"
    };
}

impl Db {
    /// Store `grant`, or, if its credential already has a grant for the
    /// same repository, extend that one to `grant`'s `last_used_at` and
    /// `expires_at` (never back). Returns the stored grant's id. An
    /// existing grant keeps its own id and token, so a credential brought
    /// again isn't re-encrypted.
    ///
    /// # Errors
    ///
    /// Returns [`crate::Error::Db`] if the statement fails.
    pub async fn put_grant(&self, grant: &Grant) -> Result<String> {
        const SQL: &str = "INSERT INTO credential_grant (id, username, origin, scopes, token, \
             key_id, token_digest, created_at, last_used_at, expires_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10) \
             ON CONFLICT (token_digest, origin) DO UPDATE SET \
             last_used_at = CASE WHEN excluded.last_used_at > credential_grant.last_used_at \
                 THEN excluded.last_used_at ELSE credential_grant.last_used_at END, \
             expires_at = CASE WHEN excluded.expires_at > credential_grant.expires_at \
                 THEN excluded.expires_at ELSE credential_grant.expires_at END \
             RETURNING id";
        let origin = normalize_git_url_hard(&grant.origin);
        Ok(on_pool!(self, pool => {
            sqlx::query_scalar(sql!(pool, SQL))
                .bind(&grant.id)
                .bind(&grant.username)
                .bind(&origin)
                .bind(&grant.scopes)
                .bind(&grant.token)
                .bind(&grant.key_id)
                .bind(&grant.token_digest)
                .bind(grant.created_at)
                .bind(grant.last_used_at)
                .bind(grant.expires_at)
                .fetch_one(pool)
                .await?
        }))
    }

    /// The grant `id`, expired or not.
    ///
    /// # Errors
    ///
    /// Returns [`crate::Error::Db`] if the query fails.
    pub async fn grant(&self, id: &str) -> Result<Option<Grant>> {
        const SQL: &str = concat!(
            "SELECT ",
            columns!(),
            " FROM credential_grant WHERE id = ?1"
        );
        Ok(on_pool!(self, pool => {
            sqlx::query_as(sql!(pool, SQL))
                .bind(id)
                .fetch_optional(pool)
                .await?
        }))
    }

    /// The most recently used grant for `origin` that hasn't expired at
    /// `now`, for work no request is behind.
    ///
    /// # Errors
    ///
    /// Returns [`crate::Error::Db`] if the query fails.
    pub async fn latest_grant(&self, origin: &str, now: i64) -> Result<Option<Grant>> {
        const SQL: &str = concat!(
            "SELECT ",
            columns!(),
            " FROM credential_grant WHERE origin = ?1 AND expires_at > ?2 \
             ORDER BY last_used_at DESC LIMIT 1"
        );
        let origin = normalize_git_url_hard(origin);
        Ok(on_pool!(self, pool => {
            sqlx::query_as(sql!(pool, SQL))
                .bind(&origin)
                .bind(now)
                .fetch_optional(pool)
                .await?
        }))
    }

    /// Have `username`'s grants for `origin`, other than `except`, expire by
    /// `by` if they would later, as when they bring a new credential for
    /// it. Returns how many it shortened.
    ///
    /// # Errors
    ///
    /// Returns [`crate::Error::Db`] if the statement fails.
    pub async fn shorten_grants(
        &self,
        username: &str,
        origin: &str,
        except: &str,
        by: i64,
    ) -> Result<u64> {
        const SQL: &str = "UPDATE credential_grant SET expires_at = ?4 \
             WHERE username = ?1 AND origin = ?2 AND id <> ?3 AND expires_at > ?4";
        let origin = normalize_git_url_hard(origin);
        Ok(on_pool!(self, pool => {
            sqlx::query(sql!(pool, SQL))
                .bind(username)
                .bind(&origin)
                .bind(except)
                .bind(by)
                .execute(pool)
                .await?
                .rows_affected()
        }))
    }

    /// Delete the grant `id`, as when its credential is refused, returning
    /// whether there was one.
    ///
    /// # Errors
    ///
    /// Returns [`crate::Error::Db`] if the statement fails.
    pub async fn delete_grant(&self, id: &str) -> Result<bool> {
        Ok(on_pool!(self, pool => {
            sqlx::query(sql!(pool, "DELETE FROM credential_grant WHERE id = ?1"))
                .bind(id)
                .execute(pool)
                .await?
                .rows_affected()
                > 0
        }))
    }

    /// Delete the grants expired at `now`, returning how many.
    ///
    /// # Errors
    ///
    /// Returns [`crate::Error::Db`] if the statement fails.
    pub async fn delete_expired_grants(&self, now: i64) -> Result<u64> {
        Ok(on_pool!(self, pool => {
            sqlx::query(sql!(pool, "DELETE FROM credential_grant WHERE expires_at <= ?1"))
                .bind(now)
                .execute(pool)
                .await?
                .rows_affected()
        }))
    }

    /// The id of the key grants are encrypted with, after recording
    /// `key_id` as it if there is none.
    ///
    /// # Errors
    ///
    /// Returns [`crate::Error::Db`] if a statement fails.
    pub async fn grant_key(&self, key_id: &str) -> Result<String> {
        Ok(on_pool!(self, pool => {
            sqlx::query(sql!(
                pool,
                "INSERT INTO credential_grant_key (id, key_id) VALUES (1, ?1) \
                 ON CONFLICT (id) DO NOTHING"
            ))
            .bind(key_id)
            .execute(pool)
            .await?;
            sqlx::query_scalar(sql!(pool, "SELECT key_id FROM credential_grant_key"))
                .fetch_one(pool)
                .await?
        }))
    }
}
