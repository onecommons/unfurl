// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Plain scalars read as YAML 1.2's core schema has them, as ruamel and
//! other 1.2 readers do: `yes`, `on` and `y` are strings, at any depth.

use std::path::Path;

use serde_json::{json, Value};
use unfurl_git_sync::{DbConfig, FormatRegistry, OpenOptions, RecordQuery, ScanOptions, SyncedDir};

const FILE: &str = "cloudmap.yaml";
const REPO: &str = "git://e.com/a.git";

/// Each plain scalar and the value ruamel's safe loader reads.
fn scalars() -> Vec<(&'static str, Value)> {
    let mut table: Vec<(&str, Value)> = [
        "y", "Y", "yes", "Yes", "YES", "n", "N", "no", "NO", "on", "On", "ON", "off", "OFF",
    ]
    .into_iter()
    .map(|w| (w, json!(w)))
    .collect();
    table.extend([
        ("true", json!(true)),
        ("True", json!(true)),
        ("TRUE", json!(true)),
        ("false", json!(false)),
        ("False", json!(false)),
        ("null", Value::Null),
        ("Null", Value::Null),
        ("NULL", Value::Null),
        ("~", Value::Null),
        ("0", json!(0)),
        ("1", json!(1)),
        ("-1", json!(-1)),
        ("+1", json!(1)),
        ("0o10", json!(8)),
        ("0x1F", json!(31)),
        ("1_000", json!(1000)),
        ("1:30", json!("1:30")),
        ("1.5", json!(1.5)),
        ("1e3", json!(1000.0)),
        (".5", json!(0.5)),
    ]);
    table
}

async fn open(root: &Path) -> SyncedDir {
    open_file(root, FILE).await
}

async fn open_file(root: &Path, file: &str) -> SyncedDir {
    let url = format!("sqlite://{}?mode=rwc", root.join("../sync.db").display());
    SyncedDir::open(
        root,
        DbConfig::Sqlite { url },
        FormatRegistry::with_builtins(),
        unfurl_git_sync::git::Trust::Full,
        OpenOptions::default().pathspecs([file]).untracked(true),
    )
    .await
    .expect("open")
}

fn root() -> (tempfile::TempDir, std::path::PathBuf) {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path().join("root");
    std::fs::create_dir(&root).expect("mkdir");
    (tmp, root)
}

async fn record(sync: &SyncedDir) -> Value {
    let outcome = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    assert!(outcome.unparsed.is_empty(), "{outcome:?}");
    let found = sync
        .find_records(&RecordQuery::default())
        .await
        .expect("find");
    assert_eq!(found.len(), 1, "{found:?}");
    found[0].json.clone()
}

/// A cloudmap holding one repository whose YAML below its key is `body`.
fn yaml(body: &str) -> String {
    format!("apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  {REPO}:\n{body}")
}

/// The same, as a literate cloudmap's YAML block.
fn literate(body: &str) -> String {
    format!(
        "---\nliterate-yaml: cloudmap@unfurl/v1.0.0\n---\n\nProse.\n\n```yaml\nrepositories:\n  {REPO}:\n{body}```\n"
    )
}

/// A record body with each scalar at the record's top level, in a mapping
/// and in a sequence.
fn table_body() -> String {
    let mut body = "    path: a\n".to_string();
    for (i, (scalar, _)) in scalars().iter().enumerate() {
        body += &format!(
            "    org.foo.top{i}: {scalar}\n    org.foo.map{i}: {{k: {scalar}}}\n    org.foo.list{i}: [{scalar}]\n"
        );
    }
    body
}

fn assert_table(got: &Value) {
    for (i, (scalar, want)) in scalars().iter().enumerate() {
        let depths = [
            &got[format!("org.foo.top{i}")],
            &got[format!("org.foo.map{i}")]["k"],
            &got[format!("org.foo.list{i}")][0],
        ];
        for value in depths {
            assert_eq!(value, want, "{scalar}");
        }
    }
}

#[tokio::test]
async fn scalars_read_as_yaml_1_2_core() {
    let (_tmp, root) = root();
    std::fs::write(root.join(FILE), yaml(&table_body())).expect("write");
    assert_table(&record(&open(&root).await).await);
}

#[tokio::test]
async fn scalars_in_markdown_read_as_yaml_1_2_core() {
    let (_tmp, root) = root();
    std::fs::write(root.join("cloudmap.md"), literate(&table_body())).expect("write");
    assert_table(&record(&open_file(&root, "cloudmap.md").await).await);
}

/// A save that rewrites a record leaves its `y` as it was in the file, in
/// YAML and in a literate cloudmap.
#[tokio::test]
async fn a_rewritten_record_keeps_its_strings() {
    const BODY: &str =
        "    path: a\n    org.foo.flag: y\n    org.foo.nested: {a: yes, b: [on, n]}\n";
    for (file, doc) in [(FILE, yaml(BODY)), ("cloudmap.md", literate(BODY))] {
        let (_tmp, root) = root();
        std::fs::write(root.join(file), doc).expect("write");
        let sync = open_file(&root, file).await;
        let mut value = record(&sync).await;
        value["path"] = json!("edited");
        sync.upsert_record(Some(file), "/repositories", REPO, value, None, false)
            .await
            .expect("upsert");
        let saved = sync.save_changes().await.expect("save");
        assert!(
            saved.failed.is_empty() && saved.written.len() == 1,
            "{file}: {saved:?}"
        );
        let text = std::fs::read_to_string(root.join(file)).expect("read");
        assert!(text.contains("    org.foo.flag: y\n"), "{file}: {text}");
        let got = record(&sync).await;
        assert_eq!(got["path"], json!("edited"), "{file}");
        assert_eq!(got["org.foo.flag"], json!("y"), "{file}");
        assert_eq!(
            got["org.foo.nested"],
            json!({"a": "yes", "b": ["on", "n"]}),
            "{file}"
        );
    }
}

/// Strings that a YAML 1.1 or 1.2 reader would take for another scalar
/// unquoted are written quoted. `1:30` is the exception: a 1.1 reader
/// takes it for 90, but it's a string in YAML 1.2, so it's written plain.
#[tokio::test]
async fn strings_that_look_like_other_scalars_are_written_quoted() {
    let (_tmp, root) = root();
    std::fs::write(root.join(FILE), yaml("    path: a\n")).expect("write");
    let sync = open(&root).await;
    record(&sync).await;
    let words = [
        "y", "yes", "on", "n", "off", "true", "null", "~", "1", "010", "1.5", "0x1F", "1:30",
    ];
    let mut value = json!({"path": "a"});
    for (i, w) in words.iter().enumerate() {
        value[format!("org.foo.s{i}")] = json!(w);
    }
    sync.upsert_record(Some(FILE), "/repositories", REPO, value, None, false)
        .await
        .expect("upsert");
    let saved = sync.save_changes().await.expect("save");
    assert!(saved.failed.is_empty(), "{saved:?}");
    let text = std::fs::read_to_string(root.join(FILE)).expect("read");
    for (i, w) in words.iter().enumerate() {
        let line = text
            .lines()
            .find(|l| l.trim_start().starts_with(&format!("org.foo.s{i}:")))
            .expect("line");
        let plain = line.trim_end() == format!("    org.foo.s{i}: {w}");
        assert_eq!(plain, *w == "1:30", "{w}: {text}");
    }
    let got = record(&sync).await;
    for (i, w) in words.iter().enumerate() {
        assert_eq!(got[format!("org.foo.s{i}")], json!(w));
    }
}
