// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! SCRAM-SHA-256 password verifiers in Postgres's `rolpassword` format.

use base64::{engine::general_purpose::STANDARD, Engine as _};
use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};

/// The iteration count Postgres uses by default (`scram_iterations`).
pub(crate) const ITERATIONS: u32 = 4096;

const SALT_LEN: usize = 16;

/// A verifier for `password` with a random salt:
/// `SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>`.
///
/// The password is used as given, without SASLprep, as sqlx's client
/// sends it; Postgres and libpq would normalise a non-ASCII one first.
pub(crate) fn verifier(password: &str) -> crate::Result<String> {
    let mut salt = [0u8; SALT_LEN];
    getrandom::fill(&mut salt)
        .map_err(|e| crate::Error::Other(format!("no random salt for the verifier: {e}")))?;
    Ok(verifier_with_salt(password, &salt, ITERATIONS))
}

/// [`verifier`] with the given salt and iteration count.
pub(crate) fn verifier_with_salt(password: &str, salt: &[u8], iterations: u32) -> String {
    let salted = pbkdf2_sha256(password.as_bytes(), salt, iterations);
    let client_key = hmac_sha256(&salted, b"Client Key");
    let stored_key = Sha256::digest(client_key);
    let server_key = hmac_sha256(&salted, b"Server Key");
    format!(
        "SCRAM-SHA-256${iterations}:{}${}:{}",
        STANDARD.encode(salt),
        STANDARD.encode(stored_key),
        STANDARD.encode(server_key)
    )
}

fn hmac_sha256(key: &[u8], data: &[u8]) -> [u8; 32] {
    let mut mac = Hmac::<Sha256>::new_from_slice(key).expect("HMAC takes a key of any length");
    mac.update(data);
    mac.finalize().into_bytes().into()
}

/// PBKDF2-HMAC-SHA-256 with a 32-byte output: SCRAM's `Hi()`.
fn pbkdf2_sha256(password: &[u8], salt: &[u8], iterations: u32) -> [u8; 32] {
    let mut block = salt.to_vec();
    block.extend_from_slice(&1u32.to_be_bytes());
    let mut u = hmac_sha256(password, &block);
    let mut out = u;
    for _ in 1..iterations {
        u = hmac_sha256(password, &u);
        out.iter_mut().zip(u).for_each(|(o, b)| *o ^= b);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex(bytes: &[u8]) -> String {
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }

    #[test]
    fn pbkdf2_matches_the_published_vectors() {
        assert_eq!(
            hex(&pbkdf2_sha256(b"password", b"salt", 1)),
            "120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b"
        );
        assert_eq!(
            hex(&pbkdf2_sha256(b"password", b"salt", 4096)),
            "c5e478d59288c841aa530db6845c4c8d962893a001ce4e11a4963873aa98134a"
        );
    }

    /// RFC 7677's example exchange: user "user", password "pencil".
    #[test]
    fn the_keys_reproduce_rfc_7677s_server_signature() {
        let salt = STANDARD.decode("W22ZaJ0SNY7soEsUEjb6gQ==").unwrap();
        let verifier = verifier_with_salt("pencil", &salt, 4096);
        let server_key = STANDARD
            .decode(verifier.rsplit(':').next().unwrap())
            .unwrap();
        let auth_message = "n=user,r=rOprNGfwEbeRWgbNEkqO,\
            r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,\
            c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0";
        let signature = hmac_sha256(&server_key, auth_message.as_bytes());
        assert_eq!(
            STANDARD.encode(signature),
            "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
        );
    }

    #[test]
    fn a_verifier_has_postgres_layout_and_a_fresh_salt() {
        let a = verifier("secret").unwrap();
        let b = verifier("secret").unwrap();
        assert!(a.starts_with("SCRAM-SHA-256$4096:"), "{a}");
        assert!(!a.contains("secret"));
        assert_ne!(a, b);
        let (_, keys) = a.split_once('$').unwrap().1.split_once('$').unwrap();
        let (stored, server) = keys.split_once(':').unwrap();
        assert_eq!(STANDARD.decode(stored).unwrap().len(), 32);
        assert_eq!(STANDARD.decode(server).unwrap().len(), 32);
    }

    /// The verifier Postgres stores for `password`, from a role created
    /// with it as text in a transaction that's rolled back.
    async fn postgres_verifier(url: &str, password: &str) -> String {
        use sqlx::{Connection, Executor};
        let mut conn = sqlx::PgConnection::connect(url).await.expect("connect");
        let mut tx = conn.begin().await.unwrap();
        tx.execute("SET LOCAL password_encryption = 'scram-sha-256'")
            .await
            .unwrap();
        let role = format!("scram_test_{}", std::process::id());
        let create: String =
            sqlx::query_scalar("SELECT format('CREATE ROLE %I PASSWORD %L', $1, $2)")
                .bind(&role)
                .bind(password)
                .fetch_one(&mut *tx)
                .await
                .unwrap();
        tx.execute(create.as_str()).await.unwrap();
        let stored: String =
            sqlx::query_scalar("SELECT rolpassword FROM pg_authid WHERE rolname = $1")
                .bind(&role)
                .fetch_one(&mut *tx)
                .await
                .unwrap();
        tx.rollback().await.unwrap();
        stored
    }

    /// Ours with the salt and iteration count of Postgres's `stored`.
    fn ours_like(stored: &str, password: &str) -> String {
        let params = stored.split('$').nth(1).unwrap();
        let (iterations, salt) = params.split_once(':').unwrap();
        let salt = STANDARD.decode(salt).unwrap();
        verifier_with_salt(password, &salt, iterations.parse().unwrap())
    }

    #[tokio::test]
    async fn a_verifier_is_the_one_postgres_makes_but_for_saslprep() {
        let Some(url) = std::env::var("UNFURL_TEST_PG_URL")
            .ok()
            .filter(|url| !url.is_empty())
        else {
            eprintln!("skip: UNFURL_TEST_PG_URL not set");
            return;
        };
        let password = "pencil 'and' paper";
        let stored = postgres_verifier(&url, password).await;
        assert!(stored.starts_with("SCRAM-SHA-256$4096:"), "{stored}");
        assert_eq!(ours_like(&stored, password), stored);
        // Postgres saslpreps a password it is given as text, ours doesn't
        let password = "\u{2168}\u{2168}";
        let stored = postgres_verifier(&url, password).await;
        assert_ne!(ours_like(&stored, password), stored);
        assert_eq!(ours_like(&stored, "IXIX"), stored);
    }
}
