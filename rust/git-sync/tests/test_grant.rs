// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `credential_grant` reads and writes, on both backends.

mod common;

use unfurl_git_sync::db::{Db, DbConfig};
use unfurl_git_sync::model::Grant;

/// Run `test` on an empty SQLite database, and on Postgres when
/// `UNFURL_TEST_PG_URL` is set.
async fn each_backend(test: impl AsyncFn(&Db)) {
    let tmp = tempfile::tempdir().expect("tempdir");
    let url = format!(
        "sqlite://{}?mode=rwc",
        tmp.path().join("db.sqlite").display()
    );
    test(
        &Db::connect(&DbConfig::Sqlite { url })
            .await
            .expect("sqlite"),
    )
    .await;
    #[cfg(feature = "postgres")]
    if let Some(scope) = common::PgScope::setup().await {
        let db = Db::connect(&scope.db_config()).await.expect("postgres");
        test(&db).await;
        drop(db);
        scope.teardown().await;
    }
}

fn grant(id: &str, origin: &str, token: &[u8], last_used_at: i64, expires_at: i64) -> Grant {
    Grant {
        id: id.into(),
        username: "alice".into(),
        origin: origin.into(),
        scopes: String::new(),
        token: token.to_vec(),
        key_id: "k1".into(),
        token_digest: format!("digest-of-{}", String::from_utf8_lossy(token)),
        created_at: last_used_at,
        last_used_at,
        expires_at,
    }
}

const ORIGIN: &str = "unfurl.cloud/org/proj";

#[tokio::test]
async fn a_grant_is_stored_under_its_normalized_origin() {
    each_backend(async |db: &Db| {
        let given = grant(
            "g1",
            "https://u:t@unfurl.cloud/org/proj.git",
            b"t1",
            10,
            100,
        );
        assert_eq!(db.put_grant(&given).await.unwrap(), "g1");
        let stored = db.grant("g1").await.unwrap().unwrap();
        assert_eq!(
            stored,
            Grant {
                origin: ORIGIN.into(),
                ..given
            }
        );
        assert_eq!(db.grant("missing").await.unwrap(), None);
    })
    .await;
}

/// The same credential brought again for the same repository extends its
/// grant, which keeps its id and token; for another repository it's a
/// grant of its own.
#[tokio::test]
async fn a_credential_brought_again_extends_its_grant() {
    each_backend(async |db: &Db| {
        db.put_grant(&grant("g1", ORIGIN, b"t1", 10, 100))
            .await
            .unwrap();
        let mut again = grant("g2", ORIGIN, b"t1", 50, 150);
        again.token = b"re-encrypted".to_vec();
        assert_eq!(db.put_grant(&again).await.unwrap(), "g1");
        let stored = db.grant("g1").await.unwrap().unwrap();
        assert_eq!(
            (
                stored.token.as_slice(),
                stored.created_at,
                stored.last_used_at,
                stored.expires_at
            ),
            (b"t1".as_slice(), 10, 50, 150)
        );
        assert_eq!(db.grant("g2").await.unwrap(), None);

        let other = grant("g3", "unfurl.cloud/org/other", b"t1", 50, 150);
        assert_eq!(db.put_grant(&other).await.unwrap(), "g3");
    })
    .await;
}

#[tokio::test]
async fn the_latest_unexpired_grant_for_a_repository() {
    each_backend(async |db: &Db| {
        db.put_grant(&grant("old", ORIGIN, b"t1", 10, 1000))
            .await
            .unwrap();
        db.put_grant(&grant("new", ORIGIN, b"t2", 20, 1000))
            .await
            .unwrap();
        db.put_grant(&grant("expired", ORIGIN, b"t3", 30, 40))
            .await
            .unwrap();
        db.put_grant(&grant(
            "elsewhere",
            "unfurl.cloud/org/other",
            b"t4",
            50,
            1000,
        ))
        .await
        .unwrap();
        let latest = |origin: &'static str, now| async move {
            db.latest_grant(origin, now).await.unwrap().map(|g| g.id)
        };
        assert_eq!(latest(ORIGIN, 35).await.as_deref(), Some("expired"));
        assert_eq!(latest(ORIGIN, 40).await.as_deref(), Some("new"));
        assert_eq!(
            latest("git@unfurl.cloud:org/proj.git", 40).await.as_deref(),
            Some("new")
        );
        assert_eq!(latest(ORIGIN, 1000).await, None);
    })
    .await;
}

#[tokio::test]
async fn expired_grants_are_deleted() {
    each_backend(async |db: &Db| {
        db.put_grant(&grant("live", ORIGIN, b"t1", 10, 101))
            .await
            .unwrap();
        db.put_grant(&grant("expired", ORIGIN, b"t2", 10, 100))
            .await
            .unwrap();
        assert_eq!(db.delete_expired_grants(100).await.unwrap(), 1);
        assert!(db.grant("expired").await.unwrap().is_none());
        assert!(db.grant("live").await.unwrap().is_some());
    })
    .await;
}

/// The first key recorded is the database's; another doesn't replace it.
#[tokio::test]
async fn the_first_key_is_recorded() {
    each_backend(async |db: &Db| {
        assert_eq!(db.grant_key("k1").await.unwrap(), "k1");
        assert_eq!(db.grant_key("k1").await.unwrap(), "k1");
        assert_eq!(db.grant_key("k2").await.unwrap(), "k1");
    })
    .await;
}

/// Two callers starting at once with different keys agree on one.
#[tokio::test]
async fn callers_starting_together_agree_on_a_key() {
    each_backend(async |db: &Db| {
        let (a, b) = tokio::join!(db.grant_key("k1"), db.grant_key("k2"));
        assert_eq!(a.unwrap(), b.unwrap());
    })
    .await;
}

/// A late caller with an older time doesn't shorten a grant.
#[tokio::test]
async fn a_grant_is_never_extended_backwards() {
    each_backend(async |db: &Db| {
        db.put_grant(&grant("g1", ORIGIN, b"t1", 50, 150))
            .await
            .unwrap();
        db.put_grant(&grant("g2", ORIGIN, b"t1", 10, 100))
            .await
            .unwrap();
        let stored = db.grant("g1").await.unwrap().unwrap();
        assert_eq!((stored.last_used_at, stored.expires_at), (50, 150));
    })
    .await;
}
