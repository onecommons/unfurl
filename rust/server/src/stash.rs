// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Credentials taken out of a queued write's body into grants, and put back
//! when the write is replayed (`docs/credentials.md` §2.2).
//!
//! A grant holds a credential as `X-Git-Credentials` does, the base64 of
//! `user:secret`, whichever field it came from.

use crate::grants::{credentials, decode_credentials, GrantError, GrantStore};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value as JsonValue};
use url::Url;

/// The scopes of a grant of `cloud_vars_url`'s project token.
const VARIABLES_SCOPE: &str = "api:variables:read";

/// The body field a credential was taken from.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Field {
    /// `private_token`, with the body's `username`.
    PrivateToken,
    /// `password`, with the body's `username`.
    Password,
    /// The userinfo of `blueprint_url`.
    BlueprintUrl,
    /// The `private_token` parameter of `cloud_vars_url`.
    CloudVarsUrl,
}

/// Why a body's credentials couldn't be taken out of it.
#[derive(Debug)]
pub enum StashError {
    /// A grant couldn't be stored.
    Store(GrantError),
    /// The field carries a credential in a URL that can't be parsed, so it
    /// can't be taken out.
    Unparsable(&'static str),
}

impl From<GrantError> for StashError {
    fn from(e: GrantError) -> Self {
        StashError::Store(e)
    }
}

/// A credential taken out of a body, and the grant now holding it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Stashed {
    pub field: Field,
    pub grant: String,
}

/// Move the credentials in `body` into grants for `username`, those for
/// the write's own repository under `project_origin`, and return where
/// each came from.
pub async fn stash(
    store: &GrantStore,
    username: &str,
    project_origin: &str,
    body: &mut JsonValue,
) -> Result<Vec<Stashed>, StashError> {
    let Some(fields) = body.as_object_mut() else {
        return Ok(Vec::new());
    };
    let mut stashed = Vec::new();
    let git_user = string(fields, "username").unwrap_or_default().to_string();
    for (field, key) in [
        (Field::PrivateToken, "private_token"),
        (Field::Password, "password"),
    ] {
        if let Some(secret) = take_string(fields, key) {
            let token = credentials(&git_user, &secret);
            let grant = store.grant(username, project_origin, &token, "").await?;
            stashed.push(Stashed { field, grant });
        }
    }
    let blueprint_url = string(fields, "blueprint_url").map(str::to_string);
    if let Some(raw) = &blueprint_url {
        if take_userinfo(raw).is_none() && has_userinfo(raw) {
            return Err(StashError::Unparsable("blueprint_url"));
        }
    }
    if let Some((url, user, secret)) = blueprint_url.as_deref().and_then(take_userinfo) {
        let token = credentials(&user, &secret);
        let grant = store.grant(username, url.as_str(), &token, "").await?;
        fields.insert("blueprint_url".into(), url.to_string().into());
        stashed.push(Stashed {
            field: Field::BlueprintUrl,
            grant,
        });
    }
    let cloud_vars_url = string(fields, "cloud_vars_url").map(str::to_string);
    if let Some(raw) = &cloud_vars_url {
        if take_private_token(raw).is_none() && raw.contains("private_token") {
            return Err(StashError::Unparsable("cloud_vars_url"));
        }
    }
    if let Some((url, secret)) = cloud_vars_url.as_deref().and_then(take_private_token) {
        let mut origin = url.clone();
        origin.set_query(None);
        let token = credentials("", &secret);
        let grant = store
            .grant(username, origin.as_str(), &token, VARIABLES_SCOPE)
            .await?;
        fields.insert("cloud_vars_url".into(), url.to_string().into());
        stashed.push(Stashed {
            field: Field::CloudVarsUrl,
            grant,
        });
    }
    Ok(stashed)
}

/// Put the credentials `stashed` took out of `body` back.
///
/// # Errors
///
/// Why one couldn't be: its grant has expired, or can't be read.
pub async fn restore(
    store: &GrantStore,
    body: &mut JsonValue,
    stashed: &[Stashed],
) -> Result<(), String> {
    for item in stashed {
        let token = match store.token(&item.grant).await {
            Ok(Some(token)) => token,
            Ok(None) => {
                return Err(format!(
                    "the credentials for this write (grant {}) have expired; send it again",
                    item.grant
                ))
            }
            Err(e) => return Err(format!("couldn't read grant {}: {e}", item.grant)),
        };
        let (user, secret) = decode_credentials(&token)
            .ok_or_else(|| format!("grant {} holds no credentials", item.grant))?;
        put_back(body, item.field, &user, &secret)
            .ok_or_else(|| format!("no {:?} to restore grant {} to", item.field, item.grant))?;
    }
    Ok(())
}

fn put_back(body: &mut JsonValue, field: Field, user: &str, secret: &str) -> Option<()> {
    let fields = body.as_object_mut()?;
    match field {
        Field::PrivateToken => fields.insert("private_token".into(), secret.into()),
        Field::Password => fields.insert("password".into(), secret.into()),
        Field::BlueprintUrl => {
            let mut url = Url::parse(string(fields, "blueprint_url")?).ok()?;
            url.set_username(user).ok()?;
            url.set_password(Some(secret)).ok()?;
            fields.insert("blueprint_url".into(), url.to_string().into())
        }
        Field::CloudVarsUrl => {
            let mut url = Url::parse(string(fields, "cloud_vars_url")?).ok()?;
            url.query_pairs_mut().append_pair("private_token", secret);
            fields.insert("cloud_vars_url".into(), url.to_string().into())
        }
    };
    Some(())
}

fn string<'a>(fields: &'a Map<String, JsonValue>, key: &str) -> Option<&'a str> {
    fields.get(key).and_then(JsonValue::as_str)
}

/// Remove `key` from `fields` if it's a non-empty string, and return it.
fn take_string(fields: &mut Map<String, JsonValue>, key: &str) -> Option<String> {
    match fields.get(key) {
        Some(JsonValue::String(value)) if !value.is_empty() => {}
        _ => return None,
    }
    match fields.remove(key) {
        Some(JsonValue::String(value)) => Some(value),
        _ => None,
    }
}

/// Whether `url`'s authority has userinfo, parsed or not.
fn has_userinfo(url: &str) -> bool {
    url.split_once("://").is_some_and(|(_, rest)| {
        rest.split(['/', '?', '#'])
            .next()
            .is_some_and(|authority| authority.contains('@'))
    })
}

/// `url` without its userinfo, and that userinfo, decoded, if it has any.
fn take_userinfo(url: &str) -> Option<(Url, String, String)> {
    let mut url = Url::parse(url).ok()?;
    if url.username().is_empty() && url.password().is_none() {
        return None;
    }
    let user = urlencoding::decode(url.username()).ok()?.into_owned();
    let secret = urlencoding::decode(url.password().unwrap_or_default())
        .ok()?
        .into_owned();
    url.set_username("").ok()?;
    url.set_password(None).ok()?;
    Some((url, user, secret))
}

/// `url` without its `private_token` parameter, and that parameter, if it
/// has one.
fn take_private_token(url: &str) -> Option<(Url, String)> {
    let mut url = Url::parse(url).ok()?;
    let mut token = None;
    let kept: Vec<(String, String)> = url
        .query_pairs()
        .filter_map(|(k, v)| {
            if k == "private_token" {
                token = Some(v.into_owned());
                None
            } else {
                Some((k.into_owned(), v.into_owned()))
            }
        })
        .collect();
    let token = token?;
    if kept.is_empty() {
        url.set_query(None);
    } else {
        url.query_pairs_mut().clear().extend_pairs(kept);
    }
    Some((url, token))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    async fn store(dir: &std::path::Path) -> GrantStore {
        use clap::Parser;
        let key = dir.join("key");
        std::fs::write(&key, [3u8; 32]).unwrap();
        let db_url = format!("sqlite://{}?mode=rwc", dir.join("db.sqlite").display());
        let config = crate::config::Config::parse_from([
            "unfurl-server",
            "--cloudmap-db-url",
            &db_url,
            "--grant-key-file",
            key.to_str().unwrap(),
        ]);
        GrantStore::from_config(&config).await.unwrap()
    }

    const ORIGIN: &str = "https://unfurl.cloud/org/proj";

    /// Every credential leaves the body, and comes back as it was sent.
    #[tokio::test]
    async fn body_credentials_go_into_grants_and_come_back() {
        let dir = tempfile::tempdir().unwrap();
        let store = store(dir.path()).await;
        let sent = json!({
            "username": "deploy",
            "private_token": "pt-secret",
            "password": "pw-secret",
            "blueprint_url": "https://bob:b%40d%3Apw@gitlab.example.com/org/blueprint.git#main",
            "cloud_vars_url": "https://unfurl.cloud/api/v4/projects/7/variables?per_page=100&private_token=vars-secret",
            "patch": [],
        });
        let mut body = sent.clone();
        let stashed = stash(&store, "alice", ORIGIN, &mut body).await.unwrap();
        let fields: Vec<Field> = stashed.iter().map(|s| s.field).collect();
        assert_eq!(
            fields,
            [
                Field::PrivateToken,
                Field::Password,
                Field::BlueprintUrl,
                Field::CloudVarsUrl
            ]
        );
        let stored = body.to_string();
        for secret in ["pt-secret", "pw-secret", "b%40d", "bob", "vars-secret"] {
            assert!(!stored.contains(secret), "{secret} left in {stored}");
        }
        assert_eq!(body["username"], "deploy");
        assert_eq!(
            body["cloud_vars_url"],
            "https://unfurl.cloud/api/v4/projects/7/variables?per_page=100"
        );

        restore(&store, &mut body, &stashed).await.unwrap();
        assert_eq!(body, sent);
    }

    /// The grants are for what each credential opens: the write's project,
    /// the blueprint's repository, and, scoped to them, the variables.
    #[tokio::test]
    async fn each_grant_is_for_what_its_credential_opens() {
        let dir = tempfile::tempdir().unwrap();
        let store = store(dir.path()).await;
        let mut body = json!({
            "username": "deploy",
            "private_token": "pt-secret",
            "blueprint_url": "https://bob:pw@gitlab.example.com/org/blueprint.git",
            "cloud_vars_url": "https://unfurl.cloud/api/v4/projects/7/variables?private_token=v",
        });
        let stashed = stash(&store, "alice", ORIGIN, &mut body).await.unwrap();
        let mut grants = Vec::new();
        for item in &stashed {
            grants.push(store.db_grant(&item.grant).await);
        }
        let seen: Vec<(&str, &str, &str)> = grants
            .iter()
            .map(|g| (g.origin.as_str(), g.scopes.as_str(), g.username.as_str()))
            .collect();
        assert_eq!(
            seen,
            [
                ("unfurl.cloud/org/proj", "", "alice"),
                ("gitlab.example.com/org/blueprint", "", "alice"),
                (
                    "unfurl.cloud/api/v4/projects/7/variables",
                    VARIABLES_SCOPE,
                    "alice"
                ),
            ]
        );
    }

    /// A token alone as a URL's username comes back without a password.
    #[tokio::test]
    async fn a_token_as_username_comes_back_alone() {
        let dir = tempfile::tempdir().unwrap();
        let store = store(dir.path()).await;
        let sent = json!({"blueprint_url": "https://glpat-x@gitlab.example.com/org/bp.git"});
        let mut body = sent.clone();
        let stashed = stash(&store, "alice", ORIGIN, &mut body).await.unwrap();
        assert!(!body.to_string().contains("glpat-x"));
        restore(&store, &mut body, &stashed).await.unwrap();
        assert_eq!(body, sent);
    }

    /// A credential in a URL that can't be parsed can't be taken out, so
    /// the write is refused rather than stored with it.
    #[tokio::test]
    async fn a_credential_in_an_unparsable_url_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let store = store(dir.path()).await;
        for (field, url) in [
            ("blueprint_url", "https://u:secret@bad host/org/bp.git"),
            (
                "cloud_vars_url",
                "https://bad host/api/v4/projects/7/variables?private_token=s",
            ),
        ] {
            let mut body = json!({ field: url });
            let refused = stash(&store, "alice", ORIGIN, &mut body).await;
            assert!(
                matches!(refused, Err(StashError::Unparsable(f)) if f == field),
                "{field}"
            );
        }
        // scp syntax has a user but no secret, and isn't a credential
        let mut body = json!({"blueprint_url": "git@gitlab.example.com:org/bp.git"});
        assert!(stash(&store, "alice", ORIGIN, &mut body)
            .await
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn a_body_without_credentials_is_left_alone() {
        let dir = tempfile::tempdir().unwrap();
        let store = store(dir.path()).await;
        let sent = json!({
            "username": "deploy",
            "private_token": "",
            "password": null,
            "blueprint_url": "https://gitlab.example.com/org/blueprint.git",
            "cloud_vars_url": "https://unfurl.cloud/api/v4/projects/7/variables",
        });
        let mut body = sent.clone();
        assert!(stash(&store, "alice", ORIGIN, &mut body)
            .await
            .unwrap()
            .is_empty());
        assert_eq!(body, sent);
    }

    /// A grant that's gone fails the restore, rather than leaving the write
    /// without its credentials.
    #[tokio::test]
    async fn restoring_a_missing_grant_fails() {
        let dir = tempfile::tempdir().unwrap();
        let store = store(dir.path()).await;
        let mut body = json!({"username": "deploy"});
        let stashed = [Stashed {
            field: Field::PrivateToken,
            grant: "missing".into(),
        }];
        assert!(restore(&store, &mut body, &stashed).await.is_err());
    }
}
