// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! [`unfurl_git_sync::db::provision`], against the Postgres server
//! `UNFURL_TEST_PG_URL` names as a superuser; skipped without it.
#![cfg(feature = "postgres")]

use sqlx::postgres::{PgConnectOptions, PgConnection};
use sqlx::{Connection, Executor};
use std::str::FromStr;
use std::sync::atomic::{AtomicU64, Ordering};
use unfurl_git_sync::db::{provision, Admin, DatabaseState, Outcome, RoleChange};
use unfurl_git_sync::{Db, DbConfig};

struct Pg {
    base: url::Url,
    admin_user: String,
    admin_password: String,
}

fn pg() -> Option<Pg> {
    let Some(base) = std::env::var("UNFURL_TEST_PG_URL")
        .ok()
        .filter(|url| !url.is_empty())
    else {
        eprintln!("skip: UNFURL_TEST_PG_URL not set");
        return None;
    };
    let base = url::Url::parse(&base).expect("UNFURL_TEST_PG_URL");
    Some(Pg {
        admin_user: base.username().to_string(),
        admin_password: base.password().unwrap_or_default().to_string(),
        base,
    })
}

fn unique(prefix: &str) -> String {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    format!("{prefix}_{}_{n}", std::process::id())
}

impl Pg {
    /// The URL of `database` as `role`, with `password` if given.
    fn url(&self, role: &str, database: &str, password: Option<&str>) -> String {
        let mut url = self.base.clone();
        url.set_username(role).unwrap();
        url.set_password(password).unwrap();
        url.set_path(database);
        url.into()
    }

    async fn run(&self, role: &str, database: &str, password: &str) -> Outcome {
        self.try_run(role, database, password)
            .await
            .expect("provision")
    }

    async fn try_run(
        &self,
        role: &str,
        database: &str,
        password: &str,
    ) -> unfurl_git_sync::Result<Outcome> {
        let admin = Admin {
            user: &self.admin_user,
            password: &self.admin_password,
            database: "postgres",
        };
        provision(&self.url(role, database, None), password, &admin).await
    }

    async fn admin(&self, database: &str) -> PgConnection {
        let mut url = self.base.clone();
        url.set_path(database);
        PgConnection::connect(url.as_str()).await.expect("admin")
    }

    async fn can_log_in(&self, role: &str, database: &str, password: &str) -> bool {
        let opts = PgConnectOptions::from_str(&self.url(role, database, None))
            .unwrap()
            .password(password);
        PgConnection::connect_with(&opts).await.is_ok()
    }

    /// Drops `database`, if not empty, then `role`.
    async fn drop_all(&self, role: &str, database: &str) {
        let mut conn = self.admin("postgres").await;
        let drops = [
            ("DROP DATABASE IF EXISTS %I WITH (FORCE)", database),
            ("DROP ROLE IF EXISTS %I", role),
        ];
        for (template, name) in drops.into_iter().filter(|(_, name)| !name.is_empty()) {
            let sql: String = sqlx::query_scalar("SELECT format($1, $2)")
                .bind(template)
                .bind(name)
                .fetch_one(&mut conn)
                .await
                .unwrap();
            conn.execute(sql.as_str()).await.expect("drop");
        }
    }
}

#[tokio::test]
async fn a_first_run_creates_a_role_git_sync_can_migrate_as() {
    let Some(pg) = pg() else { return };
    let (role, database) = (unique("cdb_role"), unique("cdb_db"));
    let outcome = pg.run(&role, &database, "first-pw").await;
    assert_eq!(outcome.role_change, RoleChange::Created);
    assert_eq!(outcome.database_state, DatabaseState::Created);

    let url = pg.url(&role, &database, Some("first-pw"));
    match Db::connect(&DbConfig::Postgres {
        url,
        max_connections: None,
    })
    .await
    .expect("migrate")
    {
        Db::Postgres(pool) => pool.close().await,
        Db::Sqlite(_) => unreachable!(),
    }
    let mut conn = pg.admin(&database).await;
    let owners: Vec<String> =
        sqlx::query_scalar("SELECT tableowner::text FROM pg_tables WHERE schemaname = 'public'")
            .fetch_all(&mut conn)
            .await
            .unwrap();
    conn.close().await.unwrap();
    assert!(!owners.is_empty());
    assert!(owners.iter().all(|o| *o == role), "{owners:?}");
    pg.drop_all(&role, &database).await;
}

#[tokio::test]
async fn a_second_run_keeps_the_database_and_rotates_the_password() {
    let Some(pg) = pg() else { return };
    let (role, database) = (unique("cdb_role"), unique("cdb_db"));
    pg.run(&role, &database, "old-pw").await;
    let outcome = pg.run(&role, &database, "new-pw").await;
    assert_eq!(outcome.role_change, RoleChange::PasswordSet);
    assert_eq!(outcome.database_state, DatabaseState::Existed);
    assert!(pg.can_log_in(&role, &database, "new-pw").await);
    assert!(!pg.can_log_in(&role, &database, "old-pw").await);
    pg.drop_all(&role, &database).await;
}

#[tokio::test]
async fn names_that_need_quoting_work() {
    let Some(pg) = pg() else { return };
    let role = unique("it's a \"role\"");
    let database = unique("the 'db' \"x\"");
    let outcome = pg.run(&role, &database, "pw 'quoted'").await;
    assert_eq!(outcome.role, role);
    assert_eq!(outcome.database, database);
    assert_eq!(outcome.database_state, DatabaseState::Created);
    pg.run(&role, &database, "pw2 'quoted'").await;
    assert!(pg.can_log_in(&role, &database, "pw2 'quoted'").await);
    pg.drop_all(&role, &database).await;
}

impl Pg {
    /// Creates `role`, with `password` as text for Postgres to make the
    /// verifier of.
    async fn create_role_as_postgres_does(&self, role: &str, password: &str) {
        let mut conn = self.admin("postgres").await;
        conn.execute("SET password_encryption = 'scram-sha-256'")
            .await
            .unwrap();
        let sql: String =
            sqlx::query_scalar("SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', $1, $2)")
                .bind(role)
                .bind(password)
                .fetch_one(&mut conn)
                .await
                .unwrap();
        conn.execute(sql.as_str()).await.unwrap();
        conn.close().await.unwrap();
    }
}

#[tokio::test]
async fn a_password_logs_in_as_with_a_verifier_postgres_made() {
    let Some(pg) = pg() else { return };
    let (theirs, ours, database) = (unique("cdb_pg"), unique("cdb_role"), unique("cdb_db"));
    let password = "pencil 'and' paper";
    pg.create_role_as_postgres_does(&theirs, password).await;
    pg.run(&ours, &database, password).await;
    assert!(pg.can_log_in(&theirs, &database, password).await);
    assert!(pg.can_log_in(&ours, &database, password).await);

    // sqlx sends a password without saslprep, which Postgres applies to one
    // it's given as text: only our verifier lets this one in
    let (theirs2, ours2, database2) = (unique("cdb_pg"), unique("cdb_role"), unique("cdb_db"));
    let password = "\u{2168}\u{2168}";
    pg.create_role_as_postgres_does(&theirs2, password).await;
    pg.run(&ours2, &database2, password).await;
    assert!(!pg.can_log_in(&theirs2, &database2, password).await);
    assert!(pg.can_log_in(&ours2, &database2, password).await);

    pg.drop_all(&theirs, "").await;
    pg.drop_all(&theirs2, "").await;
    pg.drop_all(&ours, &database).await;
    pg.drop_all(&ours2, &database2).await;
}

impl Pg {
    /// Creates `role` with `attributes` and, if given, `password` as text.
    async fn create_role_with(&self, role: &str, attributes: &str, password: Option<&str>) {
        let mut conn = self.admin("postgres").await;
        let template = format!("CREATE ROLE %I {attributes} PASSWORD %L");
        let sql: String = sqlx::query_scalar("SELECT format($1, $2, $3)")
            .bind(template)
            .bind(role)
            .bind(password)
            .fetch_one(&mut conn)
            .await
            .unwrap();
        conn.execute(sql.as_str()).await.unwrap();
        conn.close().await.unwrap();
    }
}

#[tokio::test]
async fn a_role_without_login_is_given_it() {
    let Some(pg) = pg() else { return };
    let (role, database) = (unique("cdb_role"), unique("cdb_db"));
    pg.create_role_with(&role, "NOLOGIN", None).await;
    let outcome = pg.run(&role, &database, "pw").await;
    assert_eq!(outcome.role_change, RoleChange::PasswordSet);
    assert!(pg.can_log_in(&role, &database, "pw").await);
    pg.drop_all(&role, &database).await;
}

#[tokio::test]
async fn a_privileged_role_keeps_its_password() {
    let Some(pg) = pg() else { return };
    for attribute in ["SUPERUSER", "CREATEROLE"] {
        let (role, database) = (unique("cdb_role"), unique("cdb_db"));
        pg.create_role_with(&role, &format!("LOGIN {attribute}"), Some("orig"))
            .await;
        let err = pg.try_run(&role, &database, "new").await.unwrap_err();
        assert!(err.to_string().contains(attribute), "{err}");
        assert!(pg.can_log_in(&role, "postgres", "orig").await);
        assert!(!pg.can_log_in(&role, "postgres", "new").await);
        pg.drop_all(&role, &database).await;
    }
}

#[tokio::test]
async fn the_admin_can_connect_to_another_database() {
    let Some(pg) = pg() else { return };
    let (role, database) = (unique("cdb_role"), unique("cdb_db"));
    let admin = Admin {
        user: &pg.admin_user,
        password: &pg.admin_password,
        database: pg.base.path().trim_start_matches('/'),
    };
    let outcome = provision(&pg.url(&role, &database, None), "pw", &admin)
        .await
        .expect("provision");
    assert_eq!(outcome.database_state, DatabaseState::Created);
    pg.drop_all(&role, &database).await;
}

#[tokio::test]
async fn a_database_owned_by_another_role_is_left_alone() {
    let Some(pg) = pg() else { return };
    let (role, database) = (unique("cdb_role"), unique("cdb_db"));
    let mut conn = pg.admin("postgres").await;
    conn.execute(format!("CREATE DATABASE \"{database}\"").as_str())
        .await
        .unwrap();
    let outcome = pg.run(&role, &database, "pw").await;
    assert_eq!(
        outcome.database_state,
        DatabaseState::OwnedBy(pg.admin_user.clone())
    );
    let owner: String = sqlx::query_scalar(
        "SELECT pg_get_userbyid(datdba)::text FROM pg_database WHERE datname = $1",
    )
    .bind(&database)
    .fetch_one(&mut conn)
    .await
    .unwrap();
    conn.close().await.unwrap();
    assert_eq!(owner, pg.admin_user);
    pg.drop_all(&role, &database).await;
}

async fn refusal(url: &str, app_password: &str) -> String {
    let admin = Admin {
        user: "postgres",
        password: "admin",
        database: "postgres",
    };
    match provision(url, app_password, &admin).await {
        Err(unfurl_git_sync::Error::Other(msg)) => msg,
        other => panic!("not refused: {other:?}"),
    }
}

#[tokio::test]
async fn bad_arguments_are_refused_before_connecting() {
    // nothing listens on port 1, so reaching the connect would be a Db error
    let msg = refusal("postgres://app:pw@127.0.0.1:1/appdb", "pw").await;
    assert!(msg.contains("must not contain a password"), "{msg}");
    let msg = refusal("postgres://app@127.0.0.1:1/appdb?password=pw", "pw").await;
    assert!(msg.contains("must not contain a password"), "{msg}");
    let msg = refusal("postgres://app@127.0.0.1:1/appdb", "pw\0").await;
    assert!(msg.contains("NUL"), "{msg}");
    let msg = refusal("postgres://postgres@127.0.0.1:1/appdb", "pw").await;
    assert!(msg.contains("admin role"), "{msg}");
    let msg = refusal("postgres://app@127.0.0.1:1", "pw").await;
    assert!(msg.contains("no database"), "{msg}");
}
