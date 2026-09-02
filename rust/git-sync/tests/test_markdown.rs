// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Cloudmaps written as prose: a markdown document whose YAML lives in
//! fenced code blocks.
//!
//! Same shape as `test_crud.rs` -- each scenario is one `async fn` run
//! against both backends by `crud_test!`. The unit tests for the fence
//! scanner and the merge live beside the code, in `src/markdown.rs`;
//! these are the end-to-end ones, which need a working tree and a
//! database.

mod common;

use common::{crud_test, git};
use tempfile::TempDir;
use unfurl_git_sync::{RecordQuery, ScanOptions, SyncedRepo};

// ---------------------------------------------------------------------------
// literate markdown
// ---------------------------------------------------------------------------

const ORG: &str = "organization@onecommons.org";
/// A second record, so a deletion has something to take.
const RETIRED: &str = "retired@onecommons.org";
/// Tilde-fenced, and holding a block scalar full of markdown.
const DOCUMENTED: &str = "documented@onecommons.org";
/// Not in the fixture at all, so a save has to append a fence for it.
const ARRIVED: &str = "arrived@onecommons.org";

/// Write the literate fixture into the working tree and stage it, so
/// the scan (which walks the git index) sees it.
fn seed_literate(tmp: &TempDir) -> String {
    let src = std::fs::read_to_string(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures/literate_cloudmap.md"),
    )
    .expect("fixture");
    std::fs::write(tmp.path().join("cloudmap.md"), &src).expect("write");
    git(tmp.path(), &["add", "cloudmap.md"]);
    src
}

fn literate_on_disk(tmp: &TempDir) -> String {
    std::fs::read_to_string(tmp.path().join("cloudmap.md")).expect("read")
}

/// The record as the file now says it, merging every fence the way the
/// scan does.
async fn org_record(sync: &SyncedRepo) -> serde_json::Value {
    sync.get_record("cloudmap.md", "/components", ORG)
        .await
        .expect("get")
        .expect("present")
        .json
}

/// Records come from every live fence merged together, and only those:
/// the `# literate-yaml: ignore` block, the ```json block and the block
/// holding no record section all contribute nothing.
async fn literate_markdown_is_indexed(sync: &SyncedRepo, tmp: &TempDir) {
    seed_literate(tmp);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");

    let org = org_record(sync).await;
    assert!(org["type"].get("RealWorldEntity").is_some(), "{org:?}");
    assert_eq!(org["name"], "onecommons", "the second fence merged in");

    let all = sync
        .find_records(&RecordQuery {
            path: Some("/components".into()),
            ..Default::default()
        })
        .await
        .expect("find");
    let mut keys: Vec<&str> = all.iter().map(|r| r.key.as_str()).collect();
    keys.sort_unstable();
    assert_eq!(
        keys,
        [DOCUMENTED, ORG, RETIRED],
        "inert blocks must contribute nothing: {keys:?}"
    );

    // A tilde fence is read like any other, and the `#` lines and the
    // indented fence inside its block scalar are content, not markup.
    let notes = sync
        .get_record("cloudmap.md", "/components", DOCUMENTED)
        .await
        .expect("get")
        .expect("present")
        .json["notes"]
        .as_str()
        .expect("string")
        .to_string();
    assert!(notes.contains("```\nunfurl deploy\n```"), "{notes:?}");
    assert!(notes.contains("# still not a comment"), "{notes:?}");
}

/// An update lands in the block that already defines the field, and
/// every other byte of the document stays where it was.
async fn literate_markdown_updates_in_place(sync: &SyncedRepo, tmp: &TempDir) {
    let before = seed_literate(tmp);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");

    let mut next = org_record(sync).await;
    next["type"] = serde_json::json!({"Renamed": null});
    sync.upsert_record(Some("cloudmap.md"), "/components", ORG, next, None, false)
        .await
        .expect("write");
    sync.save_changes().await.expect("save");

    let after = literate_on_disk(tmp);
    // Only the value's body is re-emitted, so the trailing comment on
    // the key line above it survives the edit.
    assert!(
        after.contains("    type: # the kind of thing this is\n      Renamed:\n"),
        "the type changed in place, keeping its comment:\n{after}"
    );
    // The other record's type is not this record's business.
    assert!(
        after.contains("  retired@onecommons.org: # scheduled for removal"),
        "{after}"
    );
    // The prose, the fence lines, the comment on the section key, the
    // second fence, the block scalar full of markdown, the opted-out
    // block and the json block are all untouched.
    for kept in [
        "# The organization",
        "components: # entities: !!",
        "    name: onecommons",
        "      # text, not a comment",
        "      # still not a comment",
        "# literate-yaml: ignore",
        "```json",
        "apiVersion: unfurl/v1.0.0",
    ] {
        assert!(after.contains(kept), "{kept:?} must survive:\n{after}");
    }
    assert_eq!(
        before.lines().count(),
        after.lines().count(),
        "an in-place edit must not change the document's shape:\n{after}"
    );
    // An anchor block names the record with nothing under it, to hang
    // the prose off. Filling it in would move the record next to a
    // paragraph written to introduce it, not to hold it.
    assert!(
        after.contains(&format!("components:\n  {ORG}:\n```")),
        "the anchor block must stay an anchor:\n{after}"
    );
}

/// A *record* no block holds has nowhere to be placed, so it lands in a
/// new fence at the end -- and the merged document then says what the
/// database says.
///
/// A new *field* is different: it joins the siblings it belongs with,
/// in the first block whose map can take it. Only a record the document
/// has no home for at all is appended.
async fn literate_markdown_new_record_appends_a_fence(sync: &SyncedRepo, tmp: &TempDir) {
    let before = seed_literate(tmp);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");

    sync.upsert_record(
        Some("cloudmap.md"),
        "/components",
        ARRIVED,
        serde_json::json!({"type": {"RealWorldEntity": null}}),
        None,
        false,
    )
    .await
    .expect("write");
    sync.save_changes().await.expect("save");

    let after = literate_on_disk(tmp);
    assert!(after.starts_with(&before), "a new record appends: {after}");
    assert!(after.contains(ARRIVED), "{after}");

    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert!(sync
        .get_record("cloudmap.md", "/components", ARRIVED)
        .await
        .expect("get")
        .is_some());
}

/// A delete has to clear the key from *every* block that names it: the
/// merge would otherwise resurrect it from whichever copy was left.
async fn literate_markdown_delete_clears_every_block(sync: &SyncedRepo, tmp: &TempDir) {
    seed_literate(tmp);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    sync.delete_record(Some("cloudmap.md"), "/components", ORG, None, false)
        .await
        .expect("delete");
    sync.save_changes().await.expect("save");

    let after = literate_on_disk(tmp);
    assert!(!after.contains(ORG), "left a copy behind:\n{after}");
    // The prose framing the now-empty blocks stays.
    assert!(after.contains("# The organization"), "{after}");

    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert!(
        sync.get_record("cloudmap.md", "/components", ORG)
            .await
            .expect("get")
            .is_none(),
        "the record came back from the dead"
    );
}

/// The sharpest end-to-end check: what the write produces must read back
/// as exactly what the database holds. Any asymmetry between the merge
/// and the placement shows up here as a re-upsert.
async fn literate_markdown_save_is_idempotent(sync: &SyncedRepo, tmp: &TempDir) {
    seed_literate(tmp);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");

    let mut next = org_record(sync).await;
    next["name"] = serde_json::json!("renamed");
    sync.upsert_record(Some("cloudmap.md"), "/components", ORG, next, None, false)
        .await
        .expect("write");
    sync.save_changes().await.expect("save");
    git(tmp.path(), &["add", "cloudmap.md"]);

    let stats = sync
        .update_from_working_dir(ScanOptions::default())
        .await
        .expect("rescan");
    assert_eq!(
        stats.records_upserted, 0,
        "the document already says what the database says: {stats:?}"
    );
    assert!(stats.conflicts.is_empty(), "{stats:?}");
    assert_eq!(org_record(sync).await["name"], "renamed");
}

/// A markdown file with no `literate-yaml` front matter is not one of
/// ours, and must not abort the scan of everything else either.
async fn a_plain_markdown_file_is_never_indexed(sync: &SyncedRepo, tmp: &TempDir) {
    std::fs::write(
        tmp.path().join("README.md"),
        b"# Readme\n\n```yaml\ncomponents:\n  from@readme.example:\n    type: NotReal\n```\n",
    )
    .expect("write");
    git(tmp.path(), &["add", "README.md"]);
    seed_literate(tmp);

    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan must not fail on a plain markdown file");
    assert!(sync
        .get_record("README.md", "/components", "from@readme.example")
        .await
        .expect("get")
        .is_none());
    // ...and the literate file beside it was still indexed.
    assert_eq!(org_record(sync).await["name"], "onecommons");
}

/// There is no prose to invent and no front matter to guess, so a write
/// naming a markdown file that is not there fails rather than creating
/// one the next scan would not recognise.
async fn creating_a_literate_markdown_file_is_refused(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");
    sync.upsert_record(
        Some("invented.md"),
        "/components",
        "x",
        serde_json::json!({"type": "T"}),
        None,
        false,
    )
    .await
    .expect("write");

    let outcome = sync.save_changes().await.expect("save reports per file");
    let failure = outcome
        .failed
        .iter()
        .find(|f| f.file_path == "invented.md")
        .unwrap_or_else(|| panic!("{outcome:?}"));
    // The write fails either way -- `Syntax::Markdown` refuses to
    // serialize too -- so what the refusal buys is an error that says
    // how to fix it rather than one about crate internals.
    assert!(
        failure.error.to_string().contains("literate-yaml"),
        "the error has to name the fix: {failure:?}"
    );
    assert!(!tmp.path().join("invented.md").exists());
}

/// The whole document after a save, byte for byte.
///
/// Every other test here asserts one property; this one pins the entire
/// result, so a change to placement, indentation, the trailing fence or
/// the emitted YAML shows up as a diff a reader can judge rather than as
/// six assertions that still happen to pass.
///
/// Regenerate with `UPDATE_FIXTURES=1`, and read the diff rather than
/// accepting it -- this file is the only end-to-end record of what the
/// renderer actually produces.
async fn a_saved_document_matches_the_fixture(sync: &SyncedRepo, tmp: &TempDir) {
    seed_literate(tmp);
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("scan");

    // One save carrying every shape at once: a field updated in the
    // block that defines it, a second field of the *same* record
    // updated in a different block, a key joining the sibling it
    // belongs with, a field no block holds joining its siblings, a
    // whole record deleted, and a whole record added -- which is the
    // one thing the document has no home for, so it appends.
    let mut next = org_record(sync).await;
    next["type"] = serde_json::json!({"Cooperative": null});
    next["name"] = serde_json::json!("onecommons.org");
    next["dependencies"] = serde_json::json!({"foo": null, "bar": null});
    next["description"] = serde_json::json!("a co-operative");
    sync.upsert_record(Some("cloudmap.md"), "/components", ORG, next, None, false)
        .await
        .expect("write");
    sync.delete_record(Some("cloudmap.md"), "/components", RETIRED, None, false)
        .await
        .expect("delete");
    sync.upsert_record(
        Some("cloudmap.md"),
        "/components",
        ARRIVED,
        serde_json::json!({"type": {"RealWorldEntity": null}}),
        None,
        false,
    )
    .await
    .expect("create");
    sync.save_changes().await.expect("save");

    let actual = literate_on_disk(tmp);
    let expected_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/literate_cloudmap_after_save.md");
    if std::env::var("UPDATE_FIXTURES").is_ok() {
        std::fs::write(&expected_path, &actual).expect("write fixture");
    }
    // A key the record gained lands beside the sibling it belongs
    // with, not in a fence at the end of the document -- and the
    // comment on the map holding them survives, because `name`
    // changing beside it is now swapped in place rather than costing
    // the whole record body a re-emit.
    assert!(
        actual.contains(
            "    dependencies: # a new one belongs here, beside foo\n      foo:\n      bar:\n"
        ),
        "bar must join foo, and the comment must stay:\n{actual}"
    );

    // A record no block holds is the one thing that appends.
    assert!(
        actual.ends_with(&format!(
            "```yaml\ncomponents:\n  {ARRIVED}:\n    type:\n      RealWorldEntity:\n```\n"
        )),
        "a new record appends its own fence:\n{actual}"
    );

    let expected = std::fs::read_to_string(&expected_path).expect("read fixture");
    assert_eq!(
        actual,
        expected,
        "the saved document did not match {}",
        expected_path.display()
    );
}

crud_test!(a_saved_document_matches_the_fixture);
crud_test!(literate_markdown_is_indexed);
crud_test!(literate_markdown_updates_in_place);
crud_test!(literate_markdown_new_record_appends_a_fence);
crud_test!(literate_markdown_delete_clears_every_block);
crud_test!(literate_markdown_save_is_idempotent);
crud_test!(a_plain_markdown_file_is_never_indexed);
crud_test!(creating_a_literate_markdown_file_is_refused);
