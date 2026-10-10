// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Grants: users' GitLab tokens, encrypted, in the cloudmap database, for
//! work done after the request that brought them (`docs/credentials.md`
//! §2.1). git-sync stores them; the key and the encryption are here.

use aes_gcm::aead::rand_core::RngCore;
use aes_gcm::aead::{Aead, AeadCore, KeyInit, OsRng, Payload};
use aes_gcm::{Aes256Gcm, Key, Nonce};
use base64::Engine;
use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::path::Path;
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use unfurl_git_sync::db::Db;
use unfurl_git_sync::git::normalize_git_url_hard;
use unfurl_git_sync::model::Grant;

const NONCE_LEN: usize = 12;
/// Refresh a cached grant's expiry in the database at most this often.
const REFRESH_SECS: i64 = 3600;
/// The cache is cleared when it grows past this many grants.
const CACHE_LIMIT: usize = 10_000;

#[derive(Debug)]
pub enum GrantError {
    /// The key file is missing, unreadable or malformed.
    Key(String),
    /// The database's grants were encrypted with a different key.
    WrongKey,
    /// A stored token didn't decrypt: altered, or moved from another row.
    Decrypt,
    Db(unfurl_git_sync::Error),
}

impl std::fmt::Display for GrantError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            GrantError::Key(msg) => write!(f, "grant key: {msg}"),
            GrantError::WrongKey => write!(
                f,
                "the database's grants are encrypted with a different key \
                 (every replica needs the same key; remove a mistaken key's \
                 row from credential_grant_key)"
            ),
            GrantError::Decrypt => write!(f, "a stored grant didn't decrypt"),
            GrantError::Db(e) => write!(f, "grant database: {e}"),
        }
    }
}

impl std::error::Error for GrantError {}

impl From<unfurl_git_sync::Error> for GrantError {
    fn from(e: unfurl_git_sync::Error) -> Self {
        GrantError::Db(e)
    }
}

/// The key grants are encrypted with: 32 bytes, base64-encoded in a file.
pub struct GrantKey {
    cipher: Aes256Gcm,
    digest: Hmac<Sha256>,
    id: String,
}

impl std::fmt::Debug for GrantKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("GrantKey").field("id", &self.id).finish()
    }
}

impl GrantKey {
    /// The key in `path`, as 32 bytes or their base64: a Kubernetes secret
    /// mounted as a file holds its decoded value.
    pub fn from_file(path: &Path) -> Result<Self, GrantError> {
        let error = |e: &dyn std::fmt::Display| GrantError::Key(format!("{}: {e}", path.display()));
        let contents = std::fs::read(path).map_err(|e| error(&e))?;
        if contents.len() == 32 {
            return Self::from_bytes(&contents);
        }
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(contents.trim_ascii())
            .map_err(|e| error(&e))?;
        Self::from_bytes(&bytes)
    }

    fn from_bytes(bytes: &[u8]) -> Result<Self, GrantError> {
        if bytes.len() != 32 {
            return Err(GrantError::Key(format!(
                "expected 32 bytes or their base64, found {} bytes",
                bytes.len()
            )));
        }
        let mut check = Sha256::new();
        check.update(b"unfurl grant key\0");
        check.update(bytes);
        Ok(GrantKey {
            cipher: Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(bytes)),
            digest: <Hmac<Sha256> as Mac>::new_from_slice(bytes)
                .expect("HMAC takes a key of any length"),
            id: hex(&check.finalize()[..16]),
        })
    }

    /// Identifies the key without revealing it.
    pub fn id(&self) -> &str {
        &self.id
    }

    /// Identifies `token` among grants, keyed so a dump of them can't be
    /// checked against guesses.
    fn digest(&self, token: &str) -> String {
        let mut mac = self.digest.clone();
        mac.update(token.as_bytes());
        hex(&mac.finalize().into_bytes())
    }

    /// `token` encrypted for the grant `grant_id`: a random nonce, then the
    /// ciphertext, which decrypts only for that grant.
    fn encrypt(&self, grant_id: &str, token: &str) -> Vec<u8> {
        let nonce = Aes256Gcm::generate_nonce(&mut OsRng);
        let payload = Payload {
            msg: token.as_bytes(),
            aad: grant_id.as_bytes(),
        };
        let ciphertext = self
            .cipher
            .encrypt(&nonce, payload)
            .expect("AES-GCM encrypts any message under 64 GiB");
        [nonce.as_slice(), &ciphertext].concat()
    }

    fn decrypt(&self, grant_id: &str, stored: &[u8]) -> Result<String, GrantError> {
        if stored.len() < NONCE_LEN {
            return Err(GrantError::Decrypt);
        }
        let (nonce, ciphertext) = stored.split_at(NONCE_LEN);
        let payload = Payload {
            msg: ciphertext,
            aad: grant_id.as_bytes(),
        };
        let token = self
            .cipher
            .decrypt(Nonce::from_slice(nonce), payload)
            .map_err(|_| GrantError::Decrypt)?;
        String::from_utf8(token).map_err(|_| GrantError::Decrypt)
    }
}

/// A grant's id and expiry, cached by token digest and origin.
type Cache = HashMap<(String, String), (String, i64)>;

#[derive(Debug)]
pub struct GrantStore {
    db: Db,
    key: GrantKey,
    ttl: i64,
    cache: Mutex<Cache>,
}

impl GrantStore {
    /// A store on `db` encrypting with `key`, whose grants expire `ttl`
    /// after the last request that brought their token.
    ///
    /// # Errors
    ///
    /// [`GrantError::WrongKey`] if the database's grants use another key.
    pub async fn open(db: Db, key: GrantKey, ttl: Duration) -> Result<Self, GrantError> {
        if db.grant_key(key.id()).await? != key.id() {
            return Err(GrantError::WrongKey);
        }
        Ok(GrantStore {
            db,
            key,
            ttl: i64::try_from(ttl.as_secs()).unwrap_or(i64::MAX),
            cache: Mutex::default(),
        })
    }

    /// The store `config` configures, or why there is none.
    pub async fn from_config(config: &crate::config::Config) -> Result<Self, String> {
        let db_url = config
            .cloudmap_db_url
            .as_deref()
            .ok_or("UNFURL_CLOUDMAP_DB_URL isn't set")?;
        let key_file = config
            .grant_key_file
            .as_deref()
            .ok_or("UNFURL_GRANT_KEY_FILE isn't set")?;
        let key = GrantKey::from_file(Path::new(key_file)).map_err(|e| e.to_string())?;
        let db = Db::connect(&crate::cloudmap::db_config(db_url)?)
            .await
            .map_err(|e| format!("grant database: {e}"))?;
        let ttl = Duration::from_secs(config.grant_ttl_days.saturating_mul(86_400));
        Self::open(db, key, ttl).await.map_err(|e| e.to_string())
    }

    /// The id of `username`'s grant of `token` for the repository
    /// `origin`, created or, if its token was seen before, with its expiry
    /// extended.
    pub async fn grant(
        &self,
        username: &str,
        origin: &str,
        token: &str,
    ) -> Result<String, GrantError> {
        self.grant_at(username, origin, token, now()).await
    }

    async fn grant_at(
        &self,
        username: &str,
        origin: &str,
        token: &str,
        now: i64,
    ) -> Result<String, GrantError> {
        let token_digest = self.key.digest(token);
        let cache_key = (token_digest.clone(), normalize_git_url_hard(origin));
        if let Some((id, expires_at)) = self.cache.lock().unwrap().get(&cache_key) {
            if *expires_at > now && *expires_at - self.ttl + REFRESH_SECS > now {
                return Ok(id.clone());
            }
        }
        let id = random_id();
        let expires_at = now.saturating_add(self.ttl);
        let grant = Grant {
            token: self.key.encrypt(&id, token),
            id,
            username: username.to_string(),
            origin: cache_key.1.clone(),
            scopes: String::new(),
            key_id: self.key.id().to_string(),
            token_digest,
            created_at: now,
            last_used_at: now,
            expires_at,
        };
        let id = self.db.put_grant(&grant).await?;
        let mut cache = self.cache.lock().unwrap();
        if cache.len() >= CACHE_LIMIT {
            cache.clear();
        }
        cache.insert(cache_key, (id.clone(), expires_at));
        Ok(id)
    }

    /// The token of the grant `id`, or `None` if there is no such grant or
    /// it has expired.
    pub async fn token(&self, id: &str) -> Result<Option<String>, GrantError> {
        self.token_at(id, now()).await
    }

    async fn token_at(&self, id: &str, now: i64) -> Result<Option<String>, GrantError> {
        match self.db.grant(id).await? {
            Some(grant) if grant.expires_at > now => {
                if grant.key_id != self.key.id() {
                    return Err(GrantError::WrongKey);
                }
                self.key.decrypt(id, &grant.token).map(Some)
            }
            _ => Ok(None),
        }
    }

    /// Delete expired grants, returning how many.
    pub async fn sweep(&self) -> Result<u64, GrantError> {
        Ok(self.db.delete_expired_grants(now()).await?)
    }
}

fn random_id() -> String {
    let mut id = [0u8; 16];
    OsRng.fill_bytes(&mut id);
    hex(&id)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn now() -> i64 {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    i64::try_from(secs).unwrap_or(i64::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;
    use unfurl_git_sync::db::DbConfig;

    const TTL: i64 = 1000;
    const ORIGIN: &str = "unfurl.cloud/org/proj";

    async fn db(dir: &Path) -> Db {
        let url = format!("sqlite://{}?mode=rwc", dir.join("db.sqlite").display());
        Db::connect(&DbConfig::Sqlite { url }).await.unwrap()
    }

    async fn store(db: Db, key: u8) -> Result<GrantStore, GrantError> {
        let key = GrantKey::from_bytes(&[key; 32]).unwrap();
        GrantStore::open(db, key, Duration::from_secs(TTL as u64)).await
    }

    #[tokio::test]
    async fn a_grant_gives_back_its_token_and_stores_only_ciphertext() {
        let dir = tempfile::tempdir().unwrap();
        let store = store(db(dir.path()).await, 1).await.unwrap();
        let id = store
            .grant_at("alice", ORIGIN, "glpat-secret", 0)
            .await
            .unwrap();
        assert_eq!(
            store.token_at(&id, 1).await.unwrap().as_deref(),
            Some("glpat-secret")
        );
        let stored = store.db.grant(&id).await.unwrap().unwrap();
        assert!(!String::from_utf8_lossy(&stored.token).contains("glpat-secret"));
        assert_eq!(store.token_at("missing", 1).await.unwrap(), None);
    }

    /// The digest is keyed: a plain hash of a guess doesn't find it.
    #[tokio::test]
    async fn the_stored_digest_is_keyed() {
        let dir = tempfile::tempdir().unwrap();
        let store = store(db(dir.path()).await, 1).await.unwrap();
        let id = store.grant_at("alice", ORIGIN, "hunter2", 0).await.unwrap();
        let stored = store.db.grant(&id).await.unwrap().unwrap();
        assert_ne!(stored.token_digest, hex(&Sha256::digest(b"hunter2")));
        let other = GrantKey::from_bytes(&[2; 32]).unwrap();
        assert_ne!(stored.token_digest, other.digest("hunter2"));
    }

    /// A reused nonce would break GCM: each encryption draws its own.
    #[test]
    fn each_encryption_has_its_own_nonce() {
        let key = GrantKey::from_bytes(&[1; 32]).unwrap();
        assert_ne!(
            key.encrypt("g1", "t")[..NONCE_LEN],
            key.encrypt("g1", "t")[..NONCE_LEN]
        );
    }

    #[test]
    fn a_ciphertext_decrypts_only_for_its_own_grant() {
        let key = GrantKey::from_bytes(&[1; 32]).unwrap();
        let stored = key.encrypt("g1", "glpat-secret");
        assert_eq!(key.decrypt("g1", &stored).unwrap(), "glpat-secret");
        assert!(matches!(
            key.decrypt("g2", &stored),
            Err(GrantError::Decrypt)
        ));
        let other = GrantKey::from_bytes(&[2; 32]).unwrap();
        assert!(matches!(
            other.decrypt("g1", &stored),
            Err(GrantError::Decrypt)
        ));
    }

    /// A replica started with another key finds out when it starts.
    #[tokio::test]
    async fn a_store_with_another_key_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        store(db(dir.path()).await, 1).await.unwrap();
        store(db(dir.path()).await, 1).await.unwrap();
        let other = store(db(dir.path()).await, 2).await;
        assert!(matches!(other, Err(GrantError::WrongKey)));
    }

    /// The same token for the same repository is one grant; each request
    /// bringing it extends it, past what the cache remembers.
    #[tokio::test]
    async fn a_token_brought_again_extends_its_grant() {
        let dir = tempfile::tempdir().unwrap();
        let store = store(db(dir.path()).await, 1).await.unwrap();
        let id = store.grant_at("alice", ORIGIN, "t", 0).await.unwrap();
        let other_repo = store.grant_at("alice", "unfurl.cloud/org/x", "t", 0).await;
        assert_ne!(other_repo.unwrap(), id);
        assert_eq!(store.token_at(&id, TTL).await.unwrap(), None, "expired");
        let again = store.grant_at("alice", "https://unfurl.cloud/org/proj.git", "t", TTL + 10);
        assert_eq!(again.await.unwrap(), id);
        assert!(store.token_at(&id, TTL + 20).await.unwrap().is_some());
    }

    /// The server opens a store only with a database and the right key,
    /// and otherwise says which is missing.
    #[tokio::test]
    async fn a_store_opens_from_config_or_says_why_not() {
        use clap::Parser;
        let dir = tempfile::tempdir().unwrap();
        let db_url = format!(
            "sqlite://{}?mode=rwc",
            dir.path().join("db.sqlite").display()
        );
        let key = dir.path().join("key");
        std::fs::write(&key, [1u8; 32]).unwrap();
        let open = |args: &[&str]| {
            let config =
                crate::config::Config::parse_from(["unfurl-server"].iter().chain(args).copied());
            async move { GrantStore::from_config(&config).await.map(|_| ()) }
        };
        let key_arg = key.to_str().unwrap();
        let no_db = open(&["--grant-key-file", key_arg]).await;
        assert!(no_db.unwrap_err().contains("UNFURL_CLOUDMAP_DB_URL"));
        let no_key = open(&["--cloudmap-db-url", &db_url]).await;
        assert!(no_key.unwrap_err().contains("UNFURL_GRANT_KEY_FILE"));
        let args = ["--cloudmap-db-url", &db_url, "--grant-key-file", key_arg];
        open(&args).await.unwrap();
        std::fs::write(&key, [2u8; 32]).unwrap();
        assert!(open(&args).await.unwrap_err().contains("different key"));
    }

    #[test]
    fn the_key_file_holds_32_base64_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("key");
        let engine = base64::engine::general_purpose::STANDARD;
        std::fs::write(&path, format!("{}\n", engine.encode([7u8; 32]))).unwrap();
        let key = GrantKey::from_file(&path).unwrap();
        assert_eq!(key.id(), GrantKey::from_bytes(&[7; 32]).unwrap().id());
        assert!(!format!("{key:?}").contains(&engine.encode([7u8; 32])));
        std::fs::write(&path, [7u8; 32]).unwrap();
        assert_eq!(
            GrantKey::from_file(&path).unwrap().id(),
            key.id(),
            "raw bytes"
        );
        std::fs::write(&path, engine.encode([7u8; 16])).unwrap();
        assert!(matches!(
            GrantKey::from_file(&path),
            Err(GrantError::Key(_))
        ));
    }
}
