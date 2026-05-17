// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Build script: generate Rust types from the unfurl OpenAPI spec
//! using `oas3-gen`.
//!
//! `oas3-gen` is a CLI binary distributed via `cargo install
//! oas3-gen` (see https://crates.io/crates/oas3-gen). When available,
//! the script shells out to it, post-processes the output, and writes
//! the result to `src/unfurl_types.rs`, a committed normal Rust module.
//! Builds without
//! `oas3-gen` installed just use the committed snapshot and return
//! early — no OUT_DIR copy required.
//!
//! We deliberately ignore the generated `server.rs` / `mod.rs` from
//! `server-mod` mode — our handlers are hand-rolled around axum
//! middleware (cache / queue / proxy fallthrough) that doesn't fit the
//! trait-impl pattern the generator assumes.
//!
//! `server-mod` mode (rather than `types`) is used because it
//! derives `Deserialize` on request-body types and `Serialize` on
//! response types, which is the orientation the server needs.

use std::path::PathBuf;
use std::process::Command;

const SPEC_PATH: &str = "../../unfurl/server/openapi.json";
/// File containing the pinned `oas3-gen` version this crate's
/// committed `src/unfurl_types.rs` was generated against. Read by both
/// this build script and CI so the version lives in exactly one place.
const VERSION_FILE: &str = ".oas3-gen-version";

fn main() {
    println!("cargo:rerun-if-changed={SPEC_PATH}");
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=src/unfurl_types.rs");
    println!("cargo:rerun-if-changed={VERSION_FILE}");

    let manifest_dir =
        PathBuf::from(std::env::var_os("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR"));
    let committed = manifest_dir.join("src/unfurl_types.rs");

    let expected_version = std::fs::read_to_string(manifest_dir.join(VERSION_FILE))
        .unwrap_or_else(|e| panic!("read {VERSION_FILE}: {e}"))
        .trim()
        .to_owned();

    // Use OUT_DIR only as a scratch space for oas3-gen's intermediate output.
    let out_dir = PathBuf::from(std::env::var_os("OUT_DIR").expect("OUT_DIR not set"));
    let work_dir = out_dir.join("oas3out");
    std::fs::create_dir_all(&work_dir).expect("create oas3out directory");

    // Probe the installed oas3-gen version first.  Fail fast with the
    // exact install command instead of letting a mismatched generator
    // silently produce a `src/unfurl_types.rs` that won't compile.
    let version_out = Command::new("oas3-gen").arg("--version").output();
    let version_out = match version_out {
        Ok(o) => o,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            // oas3-gen not installed: src/unfurl_types.rs is a committed
            // module, so there's nothing to do.
            assert!(
                committed.exists(),
                "oas3-gen is not installed and the committed fallback \
                 src/unfurl_types.rs is missing.\n\
                 Install with:\n  \
                 cargo install oas3-gen --version {expected_version} --locked"
            );
            return;
        }
        Err(e) => panic!(
            "failed to run `oas3-gen --version`: {e}.\n\
             Install with:\n  \
             cargo install oas3-gen --version {expected_version} --locked\n\
             (the binary lives in ~/.cargo/bin which must be on your PATH)."
        ),
    };
    let stdout = String::from_utf8_lossy(&version_out.stdout);
    // Expected stdout: `oas3-gen 0.25.3\n`.
    let installed_version = stdout
        .split_whitespace()
        .nth(1)
        .unwrap_or("")
        .trim()
        .to_owned();
    if installed_version != expected_version {
        panic!(
            "oas3-gen version mismatch: installed {installed_version:?}, \
             pinned to {expected_version:?} (see rust/server/{VERSION_FILE}).\n\
             Reinstall the pinned version:\n  \
             cargo install oas3-gen --version {expected_version} --locked --force"
        );
    }

    let status = Command::new("oas3-gen")
        .arg("generate")
        .arg("--input")
        .arg(SPEC_PATH)
        .arg("--output")
        .arg(&work_dir)
        .arg("--all-schemas")
        .arg("server-mod")
        .status()
        .unwrap_or_else(|e| panic!("failed to run `oas3-gen generate`: {e}"));
    if !status.success() {
        panic!("oas3-gen exited with status {status:?}");
    }

    // We only consume the types file. The generated server.rs /
    // mod.rs assume a trait-impl handler pattern that doesn't fit
    // our hand-rolled axum middleware.
    //
    // Post-process: strip the inner doc-comment header (`//!`) so the
    // file can be `include!`'d inside a wrapper module — inner doc
    // comments aren't allowed in that context.
    let generated_types = work_dir.join("types.rs");
    let raw = std::fs::read_to_string(&generated_types).expect("read generated types.rs");
    let cleaned = strip_inner_doc_header(&raw);

    // Prepend the allow header so the file is a valid stand-alone module.
    let with_header =
        format!("#![allow(unused_imports, dead_code, deprecated, clippy::all)]\n{cleaned}");
    // Write directly to src/unfurl_types.rs — declared as a normal module in
    // lib.rs and committed to git as the fallback for builds without oas3-gen.
    std::fs::write(&committed, with_header).expect("write src/unfurl_types.rs");
}

/// Remove leading inner-doc-comment (`//!`) lines and any blank
/// lines that immediately follow, then make every `#[derive(...)]`
/// symmetric: any derive that has `Serialize` without `Deserialize`
/// gets `Deserialize` added (and vice versa).
///
/// `oas3-gen`'s `server-mod` mode emits one-sided derives — request
/// body types get `Deserialize` only, response types get `Serialize`
/// only. The asymmetry breaks two scenarios:
///
/// 1. A response type referencing a request-side child fails to
///    derive `Serialize` for the child.
/// 2. A handler that wants to forward a typed request body via
///    `serde_json::to_vec(&typed)` can't reserialize a Deserialize-only
///    body type.
///
/// We rewrite both sides via a placeholder dance so the two passes
/// don't trip over each other (a naive
/// "Serialize → Serialize, Deserialize" pass followed by
/// "Deserialize → Deserialize, Serialize" would double-add to lines
/// the first pass already touched).
fn strip_inner_doc_header(src: &str) -> String {
    let mut lines = src.lines().peekable();
    while let Some(line) = lines.peek() {
        let trimmed = line.trim_start();
        if trimmed.starts_with("//!") || trimmed.is_empty() {
            lines.next();
        } else {
            break;
        }
    }
    let mut out = String::with_capacity(src.len());
    for line in lines {
        out.push_str(line);
        out.push('\n');
    }
    // The originally-generated source has three patterns (which are
    // mutually exclusive on a given derive line):
    //   A) `Serialize, oas3_gen_support::Default` (response-only)
    //   B) `Deserialize, oas3_gen_support::Default` (request-only)
    //   C) `Serialize, Deserialize, oas3_gen_support::Default` (both)
    // We want to lift A and B up to C. The trick: a naive "B → B,
    // Serialize" pass also matches the suffix of C lines (since C
    // ends in B's pattern), producing duplicate `Serialize`. So we
    // first stash C and A under placeholders, run B, then restore.
    //
    // Single-line variant.
    const PLACE_BOTH: &str = "@@BOTH_SINGLE@@ oas3_gen_support::Default";
    const PLACE_SER: &str = "@@SER_ONLY_SINGLE@@ oas3_gen_support::Default";
    let out = out.replace(
        "Serialize, Deserialize, oas3_gen_support::Default",
        PLACE_BOTH,
    );
    let out = out.replace("Serialize, oas3_gen_support::Default", PLACE_SER);
    let out = out.replace(
        "Deserialize, oas3_gen_support::Default",
        "Deserialize, Serialize, oas3_gen_support::Default",
    );
    let out = out.replace(
        PLACE_SER,
        "Serialize, Deserialize, oas3_gen_support::Default",
    );
    let out = out.replace(
        PLACE_BOTH,
        "Serialize, Deserialize, oas3_gen_support::Default",
    );

    // Multi-line variant. Same dance, different needle.
    const PLACE_BOTH_MULTI: &str = "@@BOTH_MULTI@@\n    oas3_gen_support::Default";
    const PLACE_SER_MULTI: &str = "@@SER_ONLY_MULTI@@\n    oas3_gen_support::Default";
    let out = out.replace(
        "Serialize,\n    Deserialize,\n    oas3_gen_support::Default",
        PLACE_BOTH_MULTI,
    );
    let out = out.replace("Serialize,\n    oas3_gen_support::Default", PLACE_SER_MULTI);
    let out = out.replace(
        "Deserialize,\n    oas3_gen_support::Default",
        "Deserialize,\n    Serialize,\n    oas3_gen_support::Default",
    );
    let out = out.replace(
        PLACE_SER_MULTI,
        "Serialize,\n    Deserialize,\n    oas3_gen_support::Default",
    );
    out.replace(
        PLACE_BOTH_MULTI,
        "Serialize,\n    Deserialize,\n    oas3_gen_support::Default",
    )
}
