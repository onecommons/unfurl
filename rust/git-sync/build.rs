// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Build script: generate Rust types from the cloudmap JSON Schema
//! (`unfurl/cloudmap/cloudmap-schema.json`) using [`typify`] and shell
//! out to `rustfmt` for formatting.
//!
//! Mirrors the pattern used by `unfurl-server`'s `build.rs` for
//! `src/unfurl_types.rs`:
//!
//! - The generated module is written to a *committed* normal Rust
//!   module at `src/formats/cloudmap_types.rs`.
//! - Builds without `rustfmt` on `PATH` use the committed snapshot
//!   verbatim and return early (rustfmt ships in `rustup`'s default
//!   profile, but the minimal profile / some CI images skip it).
//! - The file is overwritten only when its contents actually change so
//!   unrelated builds don't churn cargo's rebuild graph.
//!
//! [`CloudMapFormat`](crate::CloudMapFormat) uses these types to walk
//! records against a schema-derived shape, so a schema change that
//! renames or removes a field surfaces as a Rust compile error rather
//! than silently producing empty follow-edges at runtime.
//!
//! Only the *data* types from `cloudmap-schema.json` are generated
//! here. The OpenAPI request/response types used by the HTTP server
//! continue to be generated in `unfurl-server` from `openapi.json` (the
//! cloudmap definitions are embedded there too, so both stay in sync
//! transitively).

use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};

/// Default location of the cloudmap JSON Schema, relative to this
/// crate's directory. Overridable via the [`SCHEMA_PATH_ENV`] env var
/// (e.g. for out-of-tree builds or vendored copies).
const DEFAULT_SCHEMA_PATH: &str = "../../unfurl/cloudmap/cloudmap-schema.json";
/// Env var that overrides [`DEFAULT_SCHEMA_PATH`] when set and non-empty.
const SCHEMA_PATH_ENV: &str = "UNFURL_CLOUDMAP_SCHEMA";

fn main() {
    println!("cargo:rerun-if-env-changed={SCHEMA_PATH_ENV}");

    // Resolve the schema path: env override if set and non-empty,
    // otherwise the hard-coded default.
    let schema_path = match std::env::var(SCHEMA_PATH_ENV) {
        Ok(v) if !v.trim().is_empty() => v,
        _ => DEFAULT_SCHEMA_PATH.to_string(),
    };

    println!("cargo:rerun-if-changed={schema_path}");
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=src/formats/cloudmap_types.rs");

    let manifest_dir =
        PathBuf::from(std::env::var_os("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR"));
    let committed = manifest_dir.join("src/formats/cloudmap_types.rs");

    // Probe `rustfmt`. Match server/build.rs's policy: missing
    // formatter → trust the committed snapshot.
    if !Command::new("rustfmt")
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
    {
        assert!(
            committed.exists(),
            "rustfmt is not installed and the committed fallback \
             src/formats/cloudmap_types.rs is missing.\n\
             Install with:\n  \
             rustup component add rustfmt"
        );
        return;
    }

    // A missing or unreadable schema is treated like a missing
    // `rustfmt`: skip regeneration and trust the committed snapshot.
    // This keeps the crate buildable from a packaged source tree (or
    // when the schema is temporarily moved) as long as the generated
    // module is checked in. Only hard-fail if there's no fallback.
    let schema_text = match fs::read_to_string(&schema_path) {
        Ok(text) => text,
        Err(e) => {
            println!(
                "cargo:warning=git-sync: cannot read {schema_path} ({e}); \
                 using existing src/formats/cloudmap_types.rs"
            );
            return;
        }
    };
    // Malformed JSON, on the other hand, is a real error worth
    // surfacing — the schema exists but is broken, so don't silently
    // mask it behind a possibly-stale snapshot.
    let schema: schemars::schema::RootSchema =
        serde_json::from_str(&schema_text).expect("parse cloudmap-schema.json as draft-07 schema");

    let settings = typify::TypeSpaceSettings::default();
    let mut type_space = typify::TypeSpace::new(&settings);
    type_space
        .add_root_schema(schema)
        .expect("typify cloudmap-schema.json");

    let raw = type_space.to_stream().to_string();
    let formatted = rustfmt(&raw);

    let header = "// AUTO-GENERATED from unfurl/cloudmap/cloudmap-schema.json by typify.\n\
                  // Do not edit by hand — change the JSON Schema and rebuild.\n\
                  //\n\
                  // Used by `crate::formats::cloudmap::CloudMapFormat` so a schema\n\
                  // change that renames or removes a field surfaces as a compile\n\
                  // error here rather than silently producing empty follow-edges.\n\
                  #![allow(unused_imports, dead_code, clippy::all)]\n";
    let new_contents = format!("{header}{formatted}");

    // Only overwrite if the contents actually differ — keeps the file's
    // mtime stable so cargo doesn't trigger downstream rebuilds for no
    // reason.
    let needs_write = match fs::read_to_string(&committed) {
        Ok(existing) => existing != new_contents,
        Err(_) => true,
    };
    if needs_write {
        fs::write(&committed, new_contents).expect("write src/formats/cloudmap_types.rs");
    }
}

/// Pipe `src` through `rustfmt --emit stdout` (edition 2021) and return
/// the formatted output. Panics on a non-zero rustfmt exit, since the
/// only thing that would trigger that is `typify` emitting unparsable
/// tokens — a generator bug worth surfacing loudly.
fn rustfmt(src: &str) -> String {
    let mut child = Command::new("rustfmt")
        .arg("--edition")
        .arg("2021")
        .arg("--emit")
        .arg("stdout")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn rustfmt");
    {
        let mut stdin = child.stdin.take().expect("rustfmt stdin");
        stdin
            .write_all(src.as_bytes())
            .expect("write to rustfmt stdin");
        // stdin is dropped here, closing the pipe so rustfmt gets EOF
    }
    let out = child.wait_with_output().expect("wait for rustfmt");
    if !out.status.success() {
        panic!(
            "rustfmt failed with status {:?}: {}",
            out.status,
            String::from_utf8_lossy(&out.stderr)
        );
    }
    String::from_utf8(out.stdout).expect("rustfmt output is utf-8")
}
