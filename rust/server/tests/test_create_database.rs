// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `unfurl-server create-database`'s refusals, before it connects; what it
//! does to a database is `unfurl_git_sync::db::provision`'s, tested there.

use std::process::Command;

fn run(envs: &[(&str, &str)]) -> (bool, String) {
    let out = Command::new(env!("CARGO_BIN_EXE_unfurl-server"))
        .arg("create-database")
        .env_clear()
        .envs(envs.iter().copied())
        .output()
        .expect("unfurl-server");
    (
        out.status.success(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
    )
}

#[cfg(feature = "postgres")]
#[test]
fn a_url_with_a_password_is_refused() {
    for url in [
        "postgres://app:url-secret@127.0.0.1:1/appdb",
        "postgres://app@127.0.0.1:1/appdb?password=url-secret",
    ] {
        let (ok, stderr) = run(&[
            ("UNFURL_CLOUDMAP_DB_URL", url),
            ("PGPASSWORD", "pw"),
            ("UNFURL_DB_ADMIN_USER", "postgres"),
            ("UNFURL_DB_ADMIN_PASSWORD", "admin"),
        ]);
        assert!(!ok);
        assert!(stderr.contains("must not contain a password"), "{stderr}");
        assert!(!stderr.contains("url-secret"), "{stderr}");
    }
}

#[test]
fn a_missing_pgpassword_is_refused() {
    let (ok, stderr) = run(&[
        ("UNFURL_CLOUDMAP_DB_URL", "postgres://app@127.0.0.1:1/appdb"),
        ("UNFURL_DB_ADMIN_USER", "postgres"),
        ("UNFURL_DB_ADMIN_PASSWORD", "admin"),
    ]);
    assert!(!ok);
    assert!(stderr.contains("PGPASSWORD is required"), "{stderr}");
}
