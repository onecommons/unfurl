// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Build script: generate Rust types from the unfurl OpenAPI spec
//! using `oas3-gen`.
//!
//! `oas3-gen` is a CLI binary distributed via `cargo install
//! oas3-gen` (see https://crates.io/crates/oas3-gen). The script
//! shells out to it to produce a `types.rs` that the runtime crate
//! includes via `src/generated.rs`. We deliberately ignore the
//! generated `server.rs` / `mod.rs` from `server-mod` mode — our
//! handlers are hand-rolled around axum middleware (cache / queue /
//! proxy fallthrough) that doesn't fit the trait-impl pattern the
//! generator assumes.
//!
//! `server-mod` mode (rather than `types`) is used because it
//! derives `Deserialize` on request-body types and `Serialize` on
//! response types, which is the orientation the server needs.

use std::path::PathBuf;
use std::process::Command;

const SPEC_PATH: &str = "../../unfurl/server/openapi.json";

fn main() {
    println!("cargo:rerun-if-changed={SPEC_PATH}");
    println!("cargo:rerun-if-changed=build.rs");

    let out_dir = std::env::var_os("OUT_DIR").expect("OUT_DIR not set");
    let out_dir = PathBuf::from(out_dir);
    let work_dir = out_dir.join("oas3out");
    std::fs::create_dir_all(&work_dir).expect("create oas3out directory");

    let status = Command::new("oas3-gen")
        .arg("generate")
        .arg("--input")
        .arg(SPEC_PATH)
        .arg("--output")
        .arg(&work_dir)
        .arg("--all-schemas")
        .arg("server-mod")
        .status();

    let status = match status {
        Ok(s) => s,
        Err(e) => panic!(
            "failed to run `oas3-gen`: {e}.\n\
             Install with `cargo install oas3-gen` (the binary lives \
             in ~/.cargo/bin which must be on your PATH).",
        ),
    };
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
    let dest = out_dir.join("unfurl_types.rs");
    std::fs::write(&dest, &cleaned).expect("write cleaned types.rs into OUT_DIR");

    // Also drop a copy at a stable path for ergonomic inspection
    // (`rust/target/oas3-gen/types.rs`). The OUT_DIR path embeds a
    // build-fingerprint hash so it's hard to find by eye; the stable
    // copy is the same content, easier to grep.
    if let Some(target_dir) = out_dir
        .ancestors()
        .find(|p| p.file_name().and_then(|n| n.to_str()) == Some("target"))
    {
        let stable_dir = target_dir.join("oas3-gen");
        let _ = std::fs::create_dir_all(&stable_dir);
        let _ = std::fs::write(stable_dir.join("unfurl_types.rs"), &cleaned);
    }
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
