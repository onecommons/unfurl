// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A long value is wrapped as ruamel wraps it at the column it's written
//! at, and an edit to one changes only its lines: the comments beside and
//! above it stay, and a wrapped value is replaced, kept or removed whole.

use std::path::Path;

use serde_json::json;
use unfurl_git_sync::{DbConfig, FormatRegistry, OpenOptions, ScanOptions, SyncedDir};

const FILE: &str = "c.yaml";
const REPOS: &str = "/repositories";
const ALPHA: &str = "git://e.com/alpha.git";

/// A record at a section's indent with a comment after a field and a
/// comment line inside it.
const BASE: &str = "apiVersion: unfurl/v1.0.0
kind: CloudMap
repositories:
  git://e.com/alpha.git:
    path: alpha  # the path
    name: alpha
    metadata:
      # about the description
      description: short  # kept
  git://e.com/beta.git:
    path: beta
";

/// 93 characters: from column 19, ruamel breaks it after the 21st word,
/// past column 80, and continues at column 8. Starting two columns
/// either way moves the break.
fn long() -> String {
    format!("{}end", "ab ".repeat(30))
}

/// `long()` as ruamel wraps it there.
fn long_wrapped() -> String {
    format!("{}ab\n        {}end", "ab ".repeat(20), "ab ".repeat(9))
}

fn wrapped_base() -> String {
    BASE.replace("short", &long_wrapped())
}

fn alpha(description: &str) -> serde_json::Value {
    json!({"path": "alpha", "name": "alpha", "metadata": {"description": description}})
}

async fn open(root: &Path, db: &Path) -> SyncedDir {
    let url = format!("sqlite://{}?mode=rwc", db.display());
    let sync = SyncedDir::open(
        root,
        DbConfig::Sqlite { url },
        FormatRegistry::with_builtins(),
        unfurl_git_sync::git::Trust::Full,
        OpenOptions::default().pathspecs([FILE]).untracked(true),
    )
    .await
    .expect("open");
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    sync
}

async fn upsert(sync: &SyncedDir, key: &str, value: serde_json::Value) {
    sync.upsert_record(Some(FILE), REPOS, key, value, None, false)
        .await
        .expect("upsert");
}

/// The file `base` becomes after `change` and a save.
async fn saved(base: &str, change: impl AsyncFn(&SyncedDir)) -> String {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path().join("root");
    std::fs::create_dir(&root).expect("mkdir");
    std::fs::write(root.join(FILE), base).expect("write");
    let sync = open(&root, &tmp.path().join("db")).await;
    change(&sync).await;
    let outcome = sync.save_changes().await.expect("save");
    assert!(outcome.failed.is_empty(), "{outcome:?}");
    std::fs::read_to_string(root.join(FILE)).expect("read")
}

#[tokio::test]
async fn an_edited_value_wraps_in_place() {
    let got = saved(BASE, async |s| upsert(s, ALPHA, alpha(&long())).await).await;
    assert_eq!(got, wrapped_base());
}

#[tokio::test]
async fn a_wrapped_value_edited_short_is_one_line() {
    let got = saved(&wrapped_base(), async |s| {
        upsert(s, ALPHA, alpha("short")).await
    })
    .await;
    assert_eq!(got, BASE);
}

#[tokio::test]
async fn a_wrapped_value_edited_long_rewraps() {
    let longer = format!("{} more", long());
    let got = saved(&wrapped_base(), async |s| {
        upsert(s, ALPHA, alpha(&longer)).await
    })
    .await;
    assert_eq!(
        got,
        wrapped_base().replace("end  # kept", "end more  # kept")
    );
}

/// Another field's edit leaves a wrapped value's lines alone.
#[tokio::test]
async fn a_wrapped_value_beside_an_edit_keeps_its_lines() {
    let got = saved(&wrapped_base(), async |s| {
        let mut value = alpha(&long());
        value["path"] = json!("alpha2");
        upsert(s, ALPHA, value).await
    })
    .await;
    assert_eq!(
        got,
        wrapped_base().replace("path: alpha  #", "path: alpha2  #")
    );
}

/// A record after one holding a wrapped value goes after all its lines,
/// and that record removed takes them all.
#[tokio::test]
async fn a_record_with_a_wrapped_value_moves_whole() {
    let got = saved(&wrapped_base(), async |s| {
        upsert(s, "git://e.com/alpha2.git", json!({"path": "a2"})).await
    })
    .await;
    let at = wrapped_base().find("  git://e.com/beta.git").expect("beta");
    let mut want = wrapped_base();
    want.insert_str(at, "  git://e.com/alpha2.git:\n    path: a2\n");
    assert_eq!(got, want);

    let got = saved(&wrapped_base(), async |s| {
        s.delete_record(Some(FILE), REPOS, ALPHA, None, false)
            .await
            .expect("delete");
    })
    .await;
    assert_eq!(
        got,
        "apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  git://e.com/beta.git:\n    path: beta\n"
    );
}

/// An added record's long value wraps at the record's column.
#[tokio::test]
async fn an_added_long_value_wraps_at_its_column() {
    let got = saved(BASE, async |s| {
        let value = json!({"metadata": {"description": long()}});
        upsert(s, "git://e.com/gamma.git", value).await
    })
    .await;
    let block = format!(
        "  git://e.com/gamma.git:\n    metadata:\n      description: {}\n",
        long_wrapped()
    );
    assert_eq!(got, format!("{BASE}{block}"));
}
