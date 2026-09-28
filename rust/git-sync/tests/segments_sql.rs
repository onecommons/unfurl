//! The segment design's SQL implementation against the in-memory one
//! (docs/segments-implementation.md §2).
//!
//! Runs the model test's histories with the model's git mirrored into a
//! real repository, which is what the SQL implementation reads. Until
//! that implementation exists, it checks the mirror itself: every commit
//! reads back as the model's tree, and every checkout is at its
//! worktree's head with the model's working tree on disk.

use std::cell::Cell;
use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::path::{Path, PathBuf};

use proptest::prelude::*;
use unfurl_git_sync::{
    DataFormat, DbConfig, FormatRegistry, Record, RecordQuery, ScanOptions, SyncedRepo,
};

include!("segments/common.rs");
include!("segments/imp.rs");
include!("segments/model.rs");
include!("segments/world.rs");
include!("segments/git_mirror.rs");

fn run(ops: &[Op]) {
    let mut world = World::new();
    world.check(0, &Op::NewUser);
    let mut mirror = GitMirror::new();
    mirror.sync(&world);
    mirror.check(&world, 0);
    for (i, op) in ops.iter().enumerate() {
        world.apply(op);
        world.check(i + 1, op);
        mirror.sync(&world);
        mirror.check(&world, i + 1);
    }
    case_done();
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(20))]

    #[test]
    #[ignore]
    fn segments_agree_with_the_implementation(ops in prop::collection::vec(op(), 1..60)) {
        run(&ops);
    }

    #[test]
    #[ignore]
    fn publishing_agrees_with_the_implementation(ops in prop::collection::vec(publish_op(), 1..16)) {
        run(&ops);
    }
}

/// The mirrored repository scans as the model's first commit, through
/// the test format.
#[tokio::test]
async fn mirror_scans_through_the_test_format() {
    let world = World::new();
    let mut mirror = GitMirror::new();
    mirror.sync(&world);
    let mut formats = FormatRegistry::new();
    formats.register(SegTest);
    let sync = SyncedRepo::open(
        &mirror.root,
        DbConfig::Sqlite {
            url: "sqlite::memory:".into(),
        },
        formats,
    )
    .await
    .unwrap();
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .unwrap();
    let records = sync.find_records(&RecordQuery::default()).await.unwrap();
    let got: BTreeMap<Key, Ver> = records
        .iter()
        .map(|r| {
            let file: Key = r.file_path[1..2].parse().unwrap();
            let index: Key = r.key[1..].parse().unwrap();
            let key = file * KEYS_PER_FILE + index;
            (key, r.json["v"].as_u64().unwrap())
        })
        .collect();
    assert_eq!(got, world.git.commits[0]);
    assert!(records.iter().all(|r| r.path == "/r"));
}

include!("segments/histories.rs");
