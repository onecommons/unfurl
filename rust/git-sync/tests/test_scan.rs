// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! What a scan does with files it cannot read.
//!
//! Same shape as `test_crud.rs` -- each scenario is one `async fn` run
//! against both backends by `crud_test!`.

mod common;

use common::{crud_test, git};
use tempfile::TempDir;
use unfurl_git_sync::{Error, ScanOptions, SyncedRepo};

/// A repository in the cloudmap fixture, so a scan that reached it can
/// be told apart from one that stopped short.
const DASHBOARD: &str = "git://unfurl.cloud/feb20a/dashboard.git";

/// One unparseable file is reported and the scan carries on.
///
/// The broken file sorts *before* the fixture, so a scan that fails
/// fast never reaches the good one -- which is what the `?` on
/// `parse_and_detect` used to do, taking the whole index down with it.
async fn unparseable_file_does_not_stop_the_scan(sync: &SyncedRepo, tmp: &TempDir) {
    std::fs::write(tmp.path().join("broken.yaml"), "[unclosed").expect("write");
    git(tmp.path(), &["add", "broken.yaml"]);

    let outcome = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("an unparseable file is data, not an error");

    assert_eq!(outcome.unparsed.len(), 1, "{outcome:?}");
    assert_eq!(outcome.unparsed[0].file_path, "broken.yaml");
    assert!(
        matches!(outcome.unparsed[0].error, Error::Yaml { .. }),
        "{:?}",
        outcome.unparsed[0].error
    );
    assert!(
        sync.get_record("cloudmap.yaml", "/repositories", DASHBOARD)
            .await
            .expect("get")
            .is_some(),
        "the file after the broken one must still have been indexed"
    );
}

crud_test!(unparseable_file_does_not_stop_the_scan);
