// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Creating a Postgres database and the role it's used as, the step
//! before [`super::Db::connect`] runs the migrations as that role.
//!
//! Every statement is built by Postgres, with `format()`'s `%I` and `%L`
//! over bind parameters, and run as a statement of its own. The role's
//! password is sent as a SCRAM-SHA-256 verifier, never as text.

use crate::error::{Error, Result};
use sqlx::postgres::{PgConnectOptions, PgConnection};
use sqlx::{Connection, Executor};
use std::str::FromStr;

/// What [`provision`] did to the role.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RoleChange {
    /// The role didn't exist and was created.
    Created,
    /// The role existed and its password was set.
    PasswordSet,
}

/// What [`provision`] found or did to the database.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DatabaseState {
    /// The database didn't exist and was created, owned by the role.
    Created,
    /// The database exists, owned by the role.
    Existed,
    /// The database exists, owned by this other role, and was left alone.
    OwnedBy(String),
}

/// The result of a successful [`provision`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Outcome {
    /// The role the app URL names.
    pub role: String,
    /// The database the app URL names.
    pub database: String,
    /// What was done to the role.
    pub role_change: RoleChange,
    /// What was found or done to the database.
    pub database_state: DatabaseState,
}

/// The role [`provision`] connects as, and the database it connects to.
#[derive(Debug, Clone, Copy)]
pub struct Admin<'a> {
    /// The admin role's name.
    pub user: &'a str,
    /// The admin role's password.
    pub password: &'a str,
    /// The database to connect to, usually `postgres`.
    pub database: &'a str,
}

/// The role and database an app URL names, and the options to connect as
/// the admin to the same server.
struct Target {
    role: String,
    database: String,
    admin: PgConnectOptions,
}

fn refuse(msg: &str) -> Error {
    Error::Other(msg.to_string())
}

fn refuse_nul(value: &str, what: &str) -> Result<()> {
    match value.contains('\0') {
        true => Err(Error::Other(format!("{what} contains a NUL character"))),
        false => Ok(()),
    }
}

fn target(app_url: &str, admin: &Admin<'_>) -> Result<Target> {
    let url = url::Url::parse(app_url)
        .map_err(|e| Error::Other(format!("the database URL is not a URL: {e}")))?;
    if url.password().is_some() || url.query_pairs().any(|(key, _)| key == "password") {
        return Err(refuse("the database URL must not contain a password"));
    }
    if url.username().is_empty() {
        return Err(refuse("the database URL names no user"));
    }
    let app = PgConnectOptions::from_str(app_url)?;
    let role = app.get_username().to_string();
    let database = app
        .get_database()
        .ok_or_else(|| refuse("the database URL names no database"))?
        .to_string();
    if role == admin.user {
        return Err(refuse("the database URL's user must not be the admin role"));
    }
    refuse_nul(&role, "the role name")?;
    refuse_nul(&database, "the database name")?;
    refuse_nul(admin.password, "the admin password")?;
    // all three: `from_str` fills the password in from PGPASSWORD
    let admin = app
        .username(admin.user)
        .password(admin.password)
        .database(admin.database);
    Ok(Target {
        role,
        database,
        admin,
    })
}

/// Postgres's text of the statement that creates `role`, or, if it
/// `exists`, sets its password, with `password` as a SCRAM verifier.
async fn role_statement(
    conn: &mut PgConnection,
    role: &str,
    password: &str,
    exists: bool,
) -> Result<String> {
    let template = match exists {
        true => "ALTER ROLE %I WITH LOGIN PASSWORD %L",
        false => "CREATE ROLE %I LOGIN PASSWORD %L",
    };
    Ok(sqlx::query_scalar("SELECT format($1, $2, $3)")
        .bind(template)
        .bind(role)
        .bind(super::scram::verifier(password)?)
        .fetch_one(conn)
        .await?)
}

fn is_sqlstate(e: &Error, code: &str) -> bool {
    match e {
        Error::Db(sqlx::Error::Database(db)) => db.code().as_deref() == Some(code),
        _ => false,
    }
}

async fn role_exists(conn: &mut PgConnection, role: &str) -> Result<bool> {
    Ok(
        sqlx::query_scalar("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = $1)")
            .bind(role)
            .fetch_one(conn)
            .await?,
    )
}

/// Creates `role`, returning false if it already exists.
async fn create_role(conn: &mut PgConnection, role: &str, password: &str) -> Result<bool> {
    let statement = role_statement(conn, role, password, false).await?;
    match conn.execute(statement.as_str()).await.map_err(Error::from) {
        Ok(_) => Ok(true),
        Err(e) if is_sqlstate(&e, "42710") => Ok(false), // duplicate_object
        Err(e) => Err(e),
    }
}

/// Refuses a `role` that is a superuser, or can create roles or
/// databases, replicate, or bypass row-level security.
async fn refuse_privileged(conn: &mut PgConnection, role: &str) -> Result<()> {
    let privileges: Vec<String> = sqlx::query_scalar(
        "SELECT unnest(array_remove(ARRAY[
            CASE WHEN rolsuper THEN 'SUPERUSER' END,
            CASE WHEN rolcreaterole THEN 'CREATEROLE' END,
            CASE WHEN rolreplication THEN 'REPLICATION' END,
            CASE WHEN rolcreatedb THEN 'CREATEDB' END,
            CASE WHEN rolbypassrls THEN 'BYPASSRLS' END
        ], NULL)) FROM pg_roles WHERE rolname = $1",
    )
    .bind(role)
    .fetch_all(conn)
    .await?;
    match privileges.is_empty() {
        true => Ok(()),
        false => Err(Error::Other(format!(
            "the role {role} exists with {}; refusing to set its password",
            privileges.join(", ")
        ))),
    }
}

async fn set_password(conn: &mut PgConnection, role: &str, password: &str) -> Result<()> {
    refuse_privileged(conn, role).await?;
    let statement = role_statement(conn, role, password, true).await?;
    conn.execute(statement.as_str()).await?;
    Ok(())
}

async fn ensure_role(conn: &mut PgConnection, role: &str, password: &str) -> Result<RoleChange> {
    if !role_exists(conn, role).await? && create_role(conn, role, password).await? {
        return Ok(RoleChange::Created);
    }
    set_password(conn, role, password).await?;
    Ok(RoleChange::PasswordSet)
}

async fn database_owner(conn: &mut PgConnection, database: &str) -> Result<Option<String>> {
    Ok(sqlx::query_scalar(
        "SELECT pg_get_userbyid(datdba)::text FROM pg_database WHERE datname = $1",
    )
    .bind(database)
    .fetch_optional(conn)
    .await?)
}

/// Creates `database` owned by `role`, returning false if it already exists.
async fn create_database(conn: &mut PgConnection, database: &str, role: &str) -> Result<bool> {
    let statement: String =
        sqlx::query_scalar("SELECT format('CREATE DATABASE %I OWNER %I', $1, $2)")
            .bind(database)
            .bind(role)
            .fetch_one(&mut *conn)
            .await?;
    // its own statement: CREATE DATABASE can't run in a transaction or DO block
    match conn.execute(statement.as_str()).await.map_err(Error::from) {
        Ok(_) => Ok(true),
        Err(e) if is_sqlstate(&e, "42P04") => Ok(false), // duplicate_database
        Err(e) => Err(e),
    }
}

fn owner_state(owner: String, role: &str) -> DatabaseState {
    match owner == role {
        true => DatabaseState::Existed,
        false => DatabaseState::OwnedBy(owner),
    }
}

async fn ensure_database(
    conn: &mut PgConnection,
    database: &str,
    role: &str,
) -> Result<DatabaseState> {
    if database_owner(conn, database).await?.is_none()
        && create_database(conn, database, role).await?
    {
        return Ok(DatabaseState::Created);
    }
    let owner = database_owner(conn, database)
        .await?
        .ok_or_else(|| refuse("the database was dropped while being created"))?;
    Ok(owner_state(owner, role))
}

fn log(outcome: &Outcome) {
    let (role, database) = (outcome.role.as_str(), outcome.database.as_str());
    match outcome.role_change {
        RoleChange::Created => tracing::info!(role, "created the role"),
        RoleChange::PasswordSet => tracing::info!(role, "the role exists; set its password"),
    }
    match &outcome.database_state {
        DatabaseState::Created => tracing::info!(database, role, "created the database"),
        DatabaseState::Existed => tracing::info!(database, role, "the database exists"),
        DatabaseState::OwnedBy(owner) => tracing::warn!(
            database,
            role,
            owner = owner.as_str(),
            "the database exists, owned by another role; leaving its owner alone, \
             but the role may need CREATE on its schema public to migrate it"
        ),
    }
}

/// Creates the role and database `app_url` names, connected as `admin` to
/// the same server, and gives the role `app_password`.
///
/// A role that exists has its password set; a database that exists is
/// left as it is, with a warning if another role owns it. Creates no
/// tables: [`super::Db::connect`] as the role does.
///
/// # Errors
///
/// [`Error::Other`] for a URL with a password, or without a user or a
/// database, a user that is the admin, or a name or password with a NUL
/// in it, all before connecting, and for an existing role with
/// `SUPERUSER`, `CREATEROLE`, `REPLICATION`, `CREATEDB` or `BYPASSRLS`;
/// [`Error::Db`] when Postgres fails.
pub async fn provision(app_url: &str, app_password: &str, admin: &Admin<'_>) -> Result<Outcome> {
    refuse_nul(app_password, "the app password")?;
    let target = target(app_url, admin)?;
    let mut conn = PgConnection::connect_with(&target.admin).await?;
    let role_change = ensure_role(&mut conn, &target.role, app_password).await?;
    let database_state = ensure_database(&mut conn, &target.database, &target.role).await?;
    conn.close().await?;
    let outcome = Outcome {
        role: target.role,
        database: target.database,
        role_change,
        database_state,
    };
    log(&outcome);
    Ok(outcome)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn the_password_is_sent_as_a_verifier() {
        let Some(url) = pg_url() else { return };
        let mut conn = PgConnection::connect(&url).await.expect("connect");
        let password = "plain-text-password";
        for exists in [false, true] {
            let sql = role_statement(&mut conn, "some role", password, exists)
                .await
                .unwrap();
            assert!(sql.contains("'SCRAM-SHA-256$4096:"), "{sql}");
            assert!(!sql.contains(password), "{sql}");
        }
        conn.close().await.unwrap();
    }

    fn pg_url() -> Option<String> {
        let url = std::env::var("UNFURL_TEST_PG_URL")
            .ok()
            .filter(|url| !url.is_empty());
        if url.is_none() {
            eprintln!("skip: UNFURL_TEST_PG_URL not set");
        }
        url
    }

    async fn drop(conn: &mut PgConnection, template: &str, name: &str) {
        let sql: String = sqlx::query_scalar("SELECT format($1, $2)")
            .bind(template)
            .bind(name)
            .fetch_one(&mut *conn)
            .await
            .unwrap();
        conn.execute(sql.as_str()).await.unwrap();
    }

    /// A create that loses a race to another creating the same object.
    #[tokio::test]
    async fn creating_what_exists_reports_it_existed() {
        let Some(url) = pg_url() else { return };
        let mut conn = PgConnection::connect(&url).await.expect("connect");
        let role = format!("cdb_race_role_{}", std::process::id());
        let database = format!("cdb_race_db_{}", std::process::id());
        assert!(create_role(&mut conn, &role, "pw").await.unwrap());
        assert!(!create_role(&mut conn, &role, "pw").await.unwrap());
        assert!(create_database(&mut conn, &database, &role).await.unwrap());
        assert!(!create_database(&mut conn, &database, &role).await.unwrap());
        drop(&mut conn, "DROP DATABASE %I WITH (FORCE)", &database).await;
        drop(&mut conn, "DROP ROLE %I", &role).await;
        conn.close().await.unwrap();
    }
}
