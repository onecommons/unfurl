// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A batch's writes, applied in one transaction, leave what the same
//! writes applied one at a time do: the same view, tombstones, pending
//! changes and conflicts, record for record, and each write the same
//! outcome. A cache of the draft that one write leaves stale for the next
//! shows up here, as the single-write tests can't.

mod common;

use std::collections::BTreeMap;

use common::{head_commit, DASHBOARD};
use proptest::prelude::*;
use unfurl_git_sync::{BatchOp, CommitRef, RecordQuery, ScanOptions, SyncedRepo};

const KEYS: [&str; 4] = [
    DASHBOARD,
    "git://unfurl.cloud/onecommons/std.git",
    "git://example.com/n1.git",
    "git://example.com/n2.git",
];
const FILES: [Option<&str>; 3] = [None, Some("cloudmap.yaml"), Some("second.yaml")];

fn git(dir: &std::path::Path, args: &[&str]) {
    let out = std::process::Command::new("git")
        .args(["-c", "user.name=t", "-c", "user.email=t@example.com"])
        .args(["-c", "commit.gpgsign=false"])
        .args(args)
        .current_dir(dir)
        .output()
        .expect("git");
    assert!(out.status.success(), "git {args:?}: {out:?}");
}

/// One write, before HEAD's commit is known: `token` asks for HEAD's.
#[derive(Debug, Clone)]
struct Write {
    delete: bool,
    key: usize,
    file: usize,
    value: u8,
    token: bool,
    resolve: bool,
}

fn write() -> impl Strategy<Value = Write> {
    (
        prop::bool::weighted(0.25),
        0..KEYS.len(),
        0..FILES.len(),
        0u8..3,
        prop::bool::weighted(0.2),
        prop::bool::weighted(0.3),
    )
        .prop_map(|(delete, key, file, value, token, resolve)| Write {
            delete,
            key,
            file,
            value,
            token,
            resolve,
        })
}

impl Write {
    fn op(&self, head: &str) -> BatchOp {
        let file_path = FILES[self.file].map(str::to_string);
        let (path, key) = ("/repositories".to_string(), KEYS[self.key].to_string());
        let expected = self.token.then(|| CommitRef::Commit(head.to_string()));
        match self.delete {
            true => BatchOp::Delete {
                file_path,
                path,
                key,
                expected,
                resolve: self.resolve,
            },
            false => BatchOp::Upsert {
                file_path,
                path,
                key,
                json: serde_json::json!({ "name": format!("v{}", self.value) }),
                expected,
                resolve: self.resolve,
            },
        }
    }
}

/// A scanned fixture with a pending edit of the dashboard that a commit
/// made outside the database conflicts with, for `resolve` to settle.
async fn fixture() -> (SyncedRepo, tempfile::TempDir) {
    let (sync, tmp) = common::sqlite_fixture().await;
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        DASHBOARD,
        serde_json::json!({"name": "pending"}),
        None,
        false,
    )
    .await
    .expect("write");
    let path = tmp.path().join("cloudmap.yaml");
    let text = std::fs::read_to_string(&path).expect("read");
    std::fs::write(&path, text.replace("name: dashboard", "name: disk")).expect("write");
    git(tmp.path(), &["commit", "-qam", "outside"]);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    (sync, tmp)
}

/// One write applied alone, as the batch reports it: `None` where it
/// applied, else the error's kind.
async fn apply_one(sync: &SyncedRepo, op: BatchOp) -> Option<String> {
    let result = match op {
        BatchOp::Upsert {
            file_path,
            path,
            key,
            json,
            expected,
            resolve,
        } => {
            sync.upsert_record(file_path.as_deref(), &path, &key, json, expected, resolve)
                .await
        }
        BatchOp::Delete {
            file_path,
            path,
            key,
            expected,
            resolve,
        } => {
            sync.delete_record(file_path.as_deref(), &path, &key, expected, resolve)
                .await
        }
    };
    result.err().map(|e| kind(&e))
}

fn kind(e: &unfurl_git_sync::Error) -> String {
    format!("{e:?}")
        .split([' ', '(', '{'])
        .next()
        .unwrap_or_default()
        .to_string()
}

/// A record as compared: its place, content, whether it's a tombstone, its
/// conflict state, and its id renamed by order of first appearance.
type Row = (
    String,
    String,
    String,
    serde_json::Value,
    bool,
    String,
    usize,
);

/// What `sync` shows: the view with tombstones, then the pending changes
/// with conflicts, ids renamed alike across both.
async fn state(sync: &SyncedRepo) -> Vec<Vec<Row>> {
    let view = sync
        .find_records(&RecordQuery {
            since_version: Some(0),
            ..Default::default()
        })
        .await
        .expect("find");
    let changes = sync.list_changes(None, true).await.expect("changes");
    let mut names: BTreeMap<i64, usize> = BTreeMap::new();
    [view, changes]
        .into_iter()
        .map(|records| {
            let mut rows: Vec<_> = records
                .into_iter()
                .map(|r| {
                    let n = names.len();
                    let id = *names.entry(r.id).or_insert(n);
                    let conflict = format!("{:?}", r.conflict);
                    (r.file_path, r.path, r.key, r.json, r.deleted, conflict, id)
                })
                .collect();
            rows.sort_by(|a, b| (&a.0, &a.1, &a.2, &a.5).cmp(&(&b.0, &b.1, &b.2, &b.5)));
            rows
        })
        .collect()
}

fn run(writes: &[Write]) {
    let rt = tokio::runtime::Runtime::new().expect("runtime");
    rt.block_on(async {
        let (batched, _a) = fixture().await;
        let (alone, _b) = fixture().await;
        // each one's own HEAD: the commits' times differ
        let ops = |head: String| -> Vec<BatchOp> { writes.iter().map(|w| w.op(&head)).collect() };
        let ops_alone = ops(head_commit(&alone).await.to_string());
        let ops = ops(head_commit(&batched).await.to_string());
        let outcome = batched
            .apply_batch(ops.clone(), false, None)
            .await
            .expect("batch");
        let mut failed: BTreeMap<usize, String> = outcome
            .failed
            .iter()
            .map(|f| (f.index, kind(&f.error)))
            .collect();
        for (i, op) in ops_alone.into_iter().enumerate() {
            let got = apply_one(&alone, op).await;
            assert_eq!(failed.remove(&i), got, "write {i}: {:?}", writes[i]);
        }
        assert_eq!(state(&batched).await, state(&alone).await, "{writes:?}");
    });
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(32))]

    #[test]
    fn a_batch_leaves_what_its_writes_one_at_a_time_do(
        writes in prop::collection::vec(write(), 1..12)
    ) {
        run(&writes);
    }
}
