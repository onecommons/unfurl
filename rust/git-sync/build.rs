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
use std::path::{Path, PathBuf};
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
    let mut schema_json: serde_json::Value =
        serde_json::from_str(&schema_text).expect("parse cloudmap-schema.json as json");
    open_record_definitions(&mut schema_json);
    let schema: schemars::schema::RootSchema =
        serde_json::from_value(schema_json).expect("parse cloudmap-schema.json as a JSON schema");

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

    write_if_changed(&committed, &new_contents, "src/formats/cloudmap_types.rs");

    // Second pass over the same schema, for the canonical field order.
    // typify can't supply it: it reads `schemars`' `Map`, a `BTreeMap`,
    // so declared property order is gone before it sees the document
    // (which is also why its structs come out alphabetical). Parsing the
    // text again as a `serde_json::Value` — with `preserve_order` on for
    // this build script — keeps it.
    let order_module = manifest_dir.join("src/formats/cloudmap_field_order.rs");
    let order_contents = rustfmt(&field_order_source(&schema_text));
    write_if_changed(
        &order_module,
        &order_contents,
        "src/formats/cloudmap_field_order.rs",
    );
}

/// Write `contents` to `path` only when they differ from what's there,
/// so the file's mtime stays put and cargo doesn't rebuild downstream
/// crates for no reason. `label` names the file in the panic message.
fn write_if_changed(path: &Path, contents: &str, label: &str) {
    let needs_write = match fs::read_to_string(path) {
        Ok(existing) => existing != contents,
        Err(_) => true,
    };
    if needs_write {
        fs::write(path, contents).unwrap_or_else(|e| panic!("write {label}: {e}"));
    }
}

/// Render the `FIELD_ORDER` table: one entry per top-level cloudmap
/// section, listing that section's record fields in the order the schema
/// declares them.
///
/// Both ends of each entry come from the schema, so neither can drift
/// from it: the section names are the root object's `properties` that
/// `$ref` a definition, either through `additionalProperties` (a map of
/// records) or directly (a section that is one record), and the field
/// list is the referenced definition's properties, with `allOf` branches
/// expanded in the order they appear.
/// That ordering is load-bearing: it must match what the Python
/// dataclasses in `unfurl/tosca_plugins/cloudmap_defs.py` emit.
///
/// Also renders `EXTENSION_PATTERNS`: for each section whose definition
/// closes its properties, the `patternProperties` pattern an undeclared
/// property name must match; and `METADATA_FIELDS`: for each section whose
/// records' `metadata` is closed, the keys it declares and that pattern.
fn field_order_source(schema_text: &str) -> String {
    let root: serde_json::Value =
        serde_json::from_str(schema_text).expect("parse cloudmap-schema.json as json");
    let defs = root
        .get("$defs")
        .or_else(|| root.get("definitions"))
        .and_then(|v| v.as_object())
        .expect("cloudmap-schema.json has definitions");

    let mut entries = String::new();
    let mut patterns = String::new();
    let mut metadata = String::new();
    let sections = root
        .get("properties")
        .and_then(|v| v.as_object())
        .expect("cloudmap-schema.json has top-level properties");
    // The root's own entry, under "": the order of the sections.
    let root_fields = quoted(sections.keys().cloned());
    entries.push_str(&format!("    (\"\", &[{root_fields}]),\n"));
    for (section, node) in sections {
        // A section is either a map of records -- `additionalProperties`
        // is the `$ref` naming the definition each of its values conforms
        // to -- or one record itself, which is a `$ref` directly.
        let record_ref = node.get("additionalProperties").and_then(|v| v.get("$ref"));
        let Some(name) = record_ref
            .or_else(|| node.get("$ref"))
            .and_then(|v| v.as_str())
            .and_then(|r| r.rsplit('/').next())
        else {
            continue;
        };
        let Some(definition) = defs.get(name) else {
            continue;
        };
        let fields = quoted(schema_properties(definition, defs));
        entries.push_str(&format!("    ({section:?}, &[{fields}]),\n"));
        if let Some(pattern) = extension_pattern(definition, defs) {
            patterns.push_str(&format!("    ({section:?}, {pattern:?}),\n"));
        }
        if record_ref.is_some() {
            metadata.push_str(&metadata_entry(section, definition, defs));
        }
    }

    format!(
        "// AUTO-GENERATED from unfurl/cloudmap/cloudmap-schema.json by build.rs.\n\
         // Do not edit by hand — change the JSON Schema and rebuild.\n\
         //\n\
         // Consumed by `CloudMapFormat::field_order`, which orders the fields of\n\
         // a record that has no counterpart on disk to copy an order from, and\n\
         // by `validate_record`, which checks the names of undeclared fields.\n\
         \n\
         /// Record field order per top-level section, as declared in the schema,\n\
         /// and under `\"\"` the order of the document's own keys, the sections.\n\
         pub const FIELD_ORDER: &[(&str, &[&str])] = &[\n{entries}];\n\
         \n\
         /// Per section of closed records, the pattern a property name not in\n\
         /// `FIELD_ORDER` must match.\n\
         pub const EXTENSION_PATTERNS: &[(&str, &str)] = &[\n{patterns}];\n\
         \n\
         /// Per section whose records' `metadata` is closed, the keys it\n\
         /// declares and the pattern any other key must match.\n\
         pub const METADATA_FIELDS: &[(&str, &[&str], &str)] = &[\n{metadata}];\n"
    )
}

/// `names` as a comma-separated list of Rust string literals.
fn quoted(names: impl IntoIterator<Item = String>) -> String {
    names
        .into_iter()
        .map(|name| format!("{name:?}"))
        .collect::<Vec<_>>()
        .join(", ")
}

/// The `METADATA_FIELDS` entry for the records of `section`, or "" if
/// their `metadata` allows any key.
fn metadata_entry(
    section: &str,
    definition: &serde_json::Value,
    defs: &serde_json::Map<String, serde_json::Value>,
) -> String {
    let Some(node) = definition.get("properties").and_then(|p| p.get("metadata")) else {
        return String::new();
    };
    let node = resolved(node, defs);
    let Some(pattern) = extension_pattern(node, defs) else {
        return String::new();
    };
    let fields = quoted(schema_properties(node, defs));
    format!("    ({section:?}, &[{fields}], {pattern:?}),\n")
}

/// The definition `node` names by `$ref`, or `node` itself if it has none.
fn resolved<'a>(
    node: &'a serde_json::Value,
    defs: &'a serde_json::Map<String, serde_json::Value>,
) -> &'a serde_json::Value {
    node.get("$ref")
        .and_then(|v| v.as_str())
        .and_then(|r| r.rsplit('/').next())
        .and_then(|name| defs.get(name))
        .unwrap_or(node)
}

/// The single `patternProperties` pattern of a node that disallows other
/// properties, by `additionalProperties: false` or `unevaluatedProperties:
/// false`, found on the node or on the `allOf` branches it builds on; `None`
/// if the node allows any property.
fn extension_pattern(
    node: &serde_json::Value,
    defs: &serde_json::Map<String, serde_json::Value>,
) -> Option<String> {
    let closed = ["additionalProperties", "unevaluatedProperties"]
        .iter()
        .any(|keyword| node.get(*keyword) == Some(&serde_json::Value::Bool(false)));
    if !closed {
        return None;
    }
    let branches = node
        .get("allOf")
        .and_then(|v| v.as_array())
        .map(|v| v.as_slice())
        .unwrap_or_default();
    let patterns: Vec<&String> = std::iter::once(node)
        .chain(branches.iter().map(|branch| resolved(branch, defs)))
        .filter_map(|n| n.get("patternProperties").and_then(|v| v.as_object()))
        .flat_map(|o| o.keys())
        .collect();
    assert_eq!(
        patterns.len(),
        1,
        "a closed definition has exactly one patternProperties pattern"
    );
    Some(patterns[0].clone())
}

/// Replaces `patternProperties` and `unevaluatedProperties` with
/// `additionalProperties: true` in every definition and its subschemas.
/// typify ignores both, emits `deny_unknown_fields` for a closed record
/// that allows extension properties, and can't merge an `allOf` branch with
/// `patternProperties`; `validate_record` enforces a record's pattern
/// instead.
fn open_record_definitions(schema: &mut serde_json::Value) {
    if let Some(defs) = schema.get_mut("definitions") {
        open_extra_properties(defs);
    }
}

fn open_extra_properties(node: &mut serde_json::Value) {
    match node {
        serde_json::Value::Object(obj) => {
            let pattern = obj.remove("patternProperties");
            let unevaluated = obj.remove("unevaluatedProperties");
            if pattern.is_some() || unevaluated.is_some() {
                obj.insert("additionalProperties".into(), serde_json::Value::Bool(true));
            }
            obj.values_mut().for_each(open_extra_properties);
        }
        serde_json::Value::Array(items) => items.iter_mut().for_each(open_extra_properties),
        _ => {}
    }
}

/// Property names of a schema node in declaration order, expanding
/// `allOf`. A branch that is only a `$ref` contributes the referenced
/// definition's properties at the position the branch appears in.
fn schema_properties(
    node: &serde_json::Value,
    defs: &serde_json::Map<String, serde_json::Value>,
) -> Vec<String> {
    let mut names: Vec<String> = node
        .get("properties")
        .and_then(|v| v.as_object())
        .map(|o| o.keys().cloned().collect())
        .unwrap_or_default();
    let branches = node
        .get("allOf")
        .and_then(|v| v.as_array())
        .map(|v| v.as_slice())
        .unwrap_or_default();
    for branch in branches {
        let resolved = match branch.get("$ref").and_then(|v| v.as_str()) {
            Some(r) if branch.get("properties").is_none() => {
                match r.rsplit('/').next().and_then(|n| defs.get(n)) {
                    Some(d) => d,
                    None => continue,
                }
            }
            _ => branch,
        };
        for name in schema_properties(resolved, defs) {
            if !names.contains(&name) {
                names.push(name);
            }
        }
    }
    names
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
