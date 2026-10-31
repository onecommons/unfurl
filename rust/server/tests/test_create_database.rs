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

/// Accepts one connection, asks it for a cleartext password, and returns
/// the password sent.
#[cfg(feature = "postgres")]
fn password_sent_to(listener: std::net::TcpListener) -> String {
    use std::io::{Read, Write};
    fn message(stream: &mut std::net::TcpStream) -> Vec<u8> {
        let mut len = [0u8; 4];
        stream.read_exact(&mut len).unwrap();
        let mut body = vec![0u8; u32::from_be_bytes(len) as usize - 4];
        stream.read_exact(&mut body).unwrap();
        body
    }
    let (mut stream, _) = listener.accept().unwrap();
    message(&mut stream); // startup
    stream.write_all(&[b'R', 0, 0, 0, 8, 0, 0, 0, 3]).unwrap();
    let mut tag = [0u8; 1];
    stream.read_exact(&mut tag).unwrap();
    assert_eq!(tag[0], b'p');
    let body = message(&mut stream);
    String::from_utf8(body)
        .unwrap()
        .trim_end_matches('\0')
        .to_string()
}

/// PGPASSWORD is the app's: the admin sends its own password, or none.
#[cfg(feature = "postgres")]
#[test]
fn the_admin_connection_sends_only_the_admin_password() {
    for (admin_password, expected) in [(Some("admin-secret"), "admin-secret"), (None, "")] {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || password_sent_to(listener));
        let url = format!("postgres://app@127.0.0.1:{port}/appdb?sslmode=disable");
        let mut envs = vec![
            ("UNFURL_CLOUDMAP_DB_URL", url.as_str()),
            ("PGPASSWORD", "app-secret"),
            ("PGPASSFILE", "/nonexistent"),
            ("UNFURL_DB_ADMIN_USER", "postgres"),
        ];
        if let Some(password) = admin_password {
            envs.push(("UNFURL_DB_ADMIN_PASSWORD", password));
        }
        let (ok, stderr) = run(&envs);
        assert!(!ok, "{stderr}");
        assert_eq!(server.join().unwrap(), expected);
    }
}
