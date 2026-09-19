// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! What a scan does with files it cannot read.
//!
//! Same shape as `test_crud.rs` -- each scenario is one `async fn` run
//! against both backends by `crud_test!`.

mod common;

use common::{crud_test, git};
use tempfile::TempDir;
use unfurl_git_sync::{Error, RecordQuery, ScanOptions, SyncedRepo};

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

/// An invalid record is refused, and the row it would have replaced
/// survives untouched.
///
/// The bad shape is the real one found in `onecommons/cloudmap` -- a bare
/// string where `typeRef` wants a map of type names. Rejecting it record
/// by record is what keeps the file's other records indexed while
/// refusing to overwrite good data with a value the graph walk cannot
/// read.
async fn an_invalid_record_is_refused_without_touching_its_row(sync: &SyncedRepo, tmp: &TempDir) {
    const GITLAB_CI: &str = "git://unfurl.cloud/feb20a/dashboard.git#:.gitlab-ci.yml";

    // Index the good document first, so there is a row to protect.
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("first scan");
    let good = sync
        .get_record("cloudmap.yaml", "/artifacts", GITLAB_CI)
        .await
        .expect("get")
        .expect("seeded");

    let path = tmp.path().join("cloudmap.yaml");
    let text = std::fs::read_to_string(&path).expect("read fixture");
    let broken = text.replace(
        "  git://unfurl.cloud/feb20a/dashboard.git#:.gitlab-ci.yml:\n    type:\n      cloudmap.artifacts.ci.GitLabPipeline:\n",
        "  git://unfurl.cloud/feb20a/dashboard.git#:.gitlab-ci.yml:\n    type: cloudmap.artifacts.ci.GitLabPipeline\n",
    );
    assert_ne!(
        broken, text,
        "fixture shape changed; the edit matched nothing"
    );
    std::fs::write(&path, &broken).expect("write");

    let outcome = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("an invalid document is data, not an error");

    assert_eq!(outcome.invalid.len(), 1, "{:?}", outcome.invalid);
    let failure = &outcome.invalid[0];
    assert_eq!(failure.file_path, "cloudmap.yaml");
    assert_eq!(failure.format, "cloudmap");
    assert!(
        failure.validation.fatal.is_empty(),
        "{:?}",
        failure.validation
    );
    // Faulted as one record of one section, not as the file.
    let at = ("artifacts".to_string(), Some(GITLAB_CI.to_string()));
    let err = failure
        .validation
        .errors
        .get(&at)
        .unwrap_or_else(|| panic!("keyed by (section, key): {:?}", failure.validation.errors));
    assert!(
        err.to_string().contains("type"),
        "the message must name the offending field: {err}"
    );
    assert_eq!(
        failure.validation.errors.len(),
        1,
        "{:?}",
        failure.validation
    );

    // The refused record keeps the value it already had -- the point of
    // refusing it rather than indexing it.
    let after = sync
        .get_record("cloudmap.yaml", "/artifacts", GITLAB_CI)
        .await
        .expect("get")
        .expect("must not be deleted either");
    assert_eq!(
        after.json, good.json,
        "an invalid record must not replace the valid row it collides with"
    );
    assert_eq!(after.version, good.version, "nor churn its version");
    assert_eq!(
        outcome.records_deleted, 0,
        "nor be pruned as though dropped"
    );

    // Every other record still indexes normally.
    assert!(
        sync.get_record("cloudmap.yaml", "/repositories", DASHBOARD)
            .await
            .expect("get")
            .is_some(),
        "one bad record must not cost the file its others"
    );
    assert!(outcome.unparsed.is_empty(), "{:?}", outcome.unparsed);
}

crud_test!(an_invalid_record_is_refused_without_touching_its_row);

/// A malformed section must not take its records with it.
///
/// `document_records` skips a section that isn't a map, so a whole
/// section replaced by a scalar enumerates as *empty* -- indistinguishable
/// from "every record here was deleted" unless something else says the
/// document is broken. Before `validate_document` existed, this silently
/// hard-deleted every record in the section.
async fn a_malformed_section_does_not_delete_its_records(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("first scan");
    let before = sync
        .find_records(&RecordQuery {
            path: Some("/repositories".into()),
            ..Default::default()
        })
        .await
        .expect("find");
    assert!(
        before.len() >= 2,
        "fixture must seed several: {}",
        before.len()
    );

    let path = tmp.path().join("cloudmap.yaml");
    let text = std::fs::read_to_string(&path).expect("read");
    let start = text.find("\nrepositories:\n").expect("section");
    let end = text[start + 1..]
        .find("\nartifacts:\n")
        .expect("next section")
        + start
        + 1;
    std::fs::write(
        &path,
        format!("{}\nrepositories: oops\n{}", &text[..start], &text[end..]),
    )
    .expect("write");

    let outcome = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");

    assert_eq!(outcome.invalid.len(), 1, "{:?}", outcome.invalid);
    // Faulted as the section: a non-mapping has no enumerable children,
    // so no record can be blamed for it.
    let at = ("repositories".to_string(), None);
    assert!(
        outcome.invalid[0].validation.errors.contains_key(&at),
        "{:?}",
        outcome.invalid[0].validation.errors
    );
    assert_eq!(
        outcome.records_deleted, 0,
        "a rejected section must not have its records cleared"
    );
    let after = sync
        .find_records(&RecordQuery {
            path: Some("/repositories".into()),
            ..Default::default()
        })
        .await
        .expect("find");
    assert_eq!(
        after.len(),
        before.len(),
        "every record in the malformed section survived"
    );
    assert!(
        sync.get_record("cloudmap.yaml", "/repositories", DASHBOARD)
            .await
            .expect("get")
            .is_some(),
        "including the one the rest of the suite looks for"
    );
}

crud_test!(a_malformed_section_does_not_delete_its_records);

/// An `apiVersion` naming an unknown schema is fatal: the file is skipped
/// whole, and nothing it used to hold is cleared.
///
/// Every section below the header is interpreted against *that* version's
/// shape, so reading the records anyway would be guessing. Skipping is the
/// same treatment an unparseable file gets, for the same reason -- a
/// document that cannot be interpreted is no evidence that its records
/// were removed.
async fn an_unknown_api_version_skips_the_file_without_clearing_it(
    sync: &SyncedRepo,
    tmp: &TempDir,
) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("first scan");
    let before = sync
        .find_records(&RecordQuery {
            file_path: Some("cloudmap.yaml".into()),
            ..Default::default()
        })
        .await
        .expect("find");
    assert!(
        before.len() > 4,
        "fixture must seed plenty: {}",
        before.len()
    );

    let path = tmp.path().join("cloudmap.yaml");
    let text = std::fs::read_to_string(&path).expect("read");
    let broken = text.replace("apiVersion: unfurl/v1.0.0", "apiVersion: unfurl/v99");
    assert_ne!(broken, text, "fixture header changed");
    // Drop a section along with the header, so being skipped is
    // observable. Without a removal the records are identical either way
    // and the assertions below would hold whether the file was skipped or
    // read straight through.
    let start = broken.find("\nrepositories:\n").expect("section");
    let end = broken[start + 1..]
        .find("\nartifacts:\n")
        .expect("next section")
        + start
        + 1;
    std::fs::write(&path, format!("{}{}", &broken[..start], &broken[end..])).expect("write");

    let outcome = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("a fatal document is data, not an error");

    assert_eq!(outcome.invalid.len(), 1, "{:?}", outcome.invalid);
    let failure = &outcome.invalid[0];
    assert_eq!(
        failure.validation.fatal.len(),
        1,
        "{:?}",
        failure.validation
    );
    assert!(
        failure.validation.fatal[0]
            .to_string()
            .contains("apiVersion"),
        "{}",
        failure.validation.fatal[0]
    );
    // Nothing below the header was even looked at.
    assert!(
        failure.validation.errors.is_empty(),
        "{:?}",
        failure.validation.errors
    );

    assert_eq!(outcome.records_deleted, 0, "a skipped file loses nothing");
    assert!(
        sync.get_record("cloudmap.yaml", "/repositories", DASHBOARD)
            .await
            .expect("get")
            .is_some(),
        "the section the broken document dropped must survive being skipped"
    );
    let after = sync
        .find_records(&RecordQuery {
            file_path: Some("cloudmap.yaml".into()),
            ..Default::default()
        })
        .await
        .expect("find");
    assert_eq!(
        after.len(),
        before.len(),
        "every record survives the file being skipped"
    );
}

crud_test!(an_unknown_api_version_skips_the_file_without_clearing_it);

/// A singleton section is faulted as the section, because the section is
/// the record -- there is no sub-key to name it by.
async fn an_invalid_singleton_is_faulted_as_its_section(sync: &SyncedRepo, tmp: &TempDir) {
    let path = tmp.path().join("cloudmap.yaml");
    let text = std::fs::read_to_string(&path).expect("read");
    assert!(
        !text.contains("\nmetadata:\n"),
        "fixture already has a top-level metadata section; this test adds one"
    );
    // `metadata` holds fields, so a sequence is the wrong shape for it.
    // A singleton takes whatever is there as its record, object or not,
    // which is exactly why validation has to judge it.
    std::fs::write(&path, format!("{text}\nmetadata:\n  - not a mapping\n")).expect("write");

    let outcome = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");

    assert_eq!(outcome.invalid.len(), 1, "{:?}", outcome.invalid);
    let at = ("metadata".to_string(), None);
    assert!(
        outcome.invalid[0].validation.errors.contains_key(&at),
        "a singleton's fault is its section's: {:?}",
        outcome.invalid[0].validation.errors
    );
    // Refused, so the singleton record never enters the index -- while the
    // rest of the document indexes as usual.
    assert!(
        sync.get_record("cloudmap.yaml", "/metadata", "metadata")
            .await
            .expect("get")
            .is_none(),
        "a rejected singleton must not be indexed"
    );
    assert!(
        sync.get_record("cloudmap.yaml", "/repositories", DASHBOARD)
            .await
            .expect("get")
            .is_some(),
        "the rest of the document still indexes"
    );
}

crud_test!(an_invalid_singleton_is_faulted_as_its_section);
