// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! The txn audit table and the commit-message rollup.

mod common;

use common::{crud_test, head_commit_body, head_trailer, upsert_op};
use tempfile::TempDir;
use unfurl_git_sync::{BatchOp, CommitRef, ScanOptions, SyncedRepo, TxnMeta};

// ---------------------------------------------------------------------------
// txn audit table and the commit-message rollup
// ---------------------------------------------------------------------------

fn delete_op(key: &str) -> BatchOp {
    BatchOp::Delete {
        file_path: Some("cloudmap.yaml".into()),
        path: "/repositories".into(),
        key: key.into(),
        expected: None,
        resolve: false,
    }
}

const AUTHOR: &str = "Ada Lovelace <ada@example.com>";
const MESSAGE_A: &str = "Point std at the new branch";
// Two paragraphs: the blank line is the case a plain indent would lose
// to trailing-whitespace stripping.
const MESSAGE_B: &str = "Retire the legacy mirror\n\nSuperseded by the dashboard entry.";

async fn rollup_round_trips_through_the_commit_message(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    // Exists in the fixture, so it can be deleted below.
    let doomed = "git://unfurl.cloud/feb20a/dashboard.git";

    let first = sync
        .apply_batch(
            vec![upsert_op("txn-a"), upsert_op("txn-b")],
            true,
            Some(TxnMeta {
                author: Some(AUTHOR.into()),
                message: Some(MESSAGE_A.into()),
            }),
        )
        .await
        .expect("first batch");
    assert_eq!(first.applied.len(), 2, "{:?}", first.failed);

    // Rewrites txn-a -- so the first batch's write of it is superseded
    // and shows up as a shortfall rather than as a record line.
    let second = sync
        .apply_batch(
            vec![upsert_op("txn-a"), delete_op(doomed)],
            true,
            Some(TxnMeta {
                author: None,
                message: Some(MESSAGE_B.into()),
            }),
        )
        .await
        .expect("second batch");
    assert_eq!(second.applied.len(), 2, "{:?}", second.failed);
    assert!(
        second.applied.iter().any(|a| a.deleted && a.key == doomed),
        "the delete must be reported as one: {:?}",
        second.applied
    );
    assert!(
        second
            .applied
            .iter()
            .any(|a| !a.deleted && a.key == "txn-a"),
        "...and the upsert must not be: {:?}",
        second.applied
    );

    let rows = sync.list_transactions().await.expect("list_transactions");
    assert_eq!(rows.len(), 2, "{rows:?}");
    let wd = sync.get_working_dir().await.expect("get_working_dir");

    let oid = sync
        .commit_repository("Update cloudmap")
        .await
        .expect("commit")
        .expect("something was dirty");

    let body = head_commit_body(tmp.path());

    // --- the parse-back contract -------------------------------------
    // The message is the only machine-readable copy, so the crate's own
    // parser is the assertion that matters: a format change that breaks
    // reconstruction fails here even if the prose still looks right.
    let parsed = unfurl_git_sync::parse_commit_rollup(&body)
        .expect("parses")
        .expect("is a git-sync commit");
    assert_eq!(parsed.txns.len(), 2, "{body}");
    assert!(parsed.origin.is_some(), "{body}");
    // Own family here, so the two agree -- the fork case is covered by
    // `a_fork_records_its_family_in_the_rollup`.
    assert_eq!(parsed.family, parsed.origin, "{body}");
    assert!(
        parsed.next_version > rows[1].last_version,
        "counter must cover every version this commit carried: {parsed:?}"
    );

    let a = &parsed.txns[0];
    assert_eq!(a.first_version, rows[0].first_version);
    assert_eq!(a.last_version, rows[0].last_version);
    assert_eq!(a.branch, wd.branch);
    // Verbatim, not a re-rendered date -- this has to round-trip exactly.
    assert_eq!(a.created_at, rows[0].created_at);
    assert_eq!(a.author.as_deref(), Some(AUTHOR));
    assert_eq!(a.message.as_deref(), Some(MESSAGE_A));
    // txn-a was rewritten by the second batch, so only txn-b still
    // carries a version of this one -- and the difference is reported
    // rather than silently dropped.
    assert_eq!(
        a.records.iter().map(|r| r.key.as_str()).collect::<Vec<_>>(),
        vec!["txn-b"],
        "{body}"
    );
    assert_eq!(a.unaccounted(), 1, "{body}");

    let b = &parsed.txns[1];
    assert_eq!(b.author, None, "an absent author round-trips as absent");
    assert_eq!(b.message.as_deref(), Some(MESSAGE_B), "blank line survives");
    assert_eq!(b.unaccounted(), 0, "{body}");
    let deleted: Vec<&str> = b
        .records
        .iter()
        .filter(|r| r.deleted)
        .map(|r| r.key.as_str())
        .collect();
    assert_eq!(deleted, vec![doomed], "the delete is flagged: {body}");
    assert!(
        b.records.iter().any(|r| !r.deleted && r.key == "txn-a"),
        "{body}"
    );

    // --- the human-readable half -------------------------------------
    assert!(
        body.starts_with("Update cloudmap\n\nRollup of 2 git-sync transactions:\n\n"),
        "{body}"
    );
    assert!(
        body.contains(&format!(
            " - {}-{} on {} {} {AUTHOR}\n   | {MESSAGE_A}\n",
            rows[0].first_version, rows[0].last_version, wd.branch, rows[0].created_at,
        )),
        "{body}"
    );
    // A blank message line is a bare marker, never trailing whitespace.
    assert!(body.contains("\n   |\n"), "{body}");
    assert!(!body.contains("   \n"), "no whitespace-only lines: {body}");
    assert!(
        body.contains(&format!(
            "   * {} D \"/repositories\" \"{doomed}\"\n",
            b.records.iter().find(|r| r.deleted).unwrap().version
        )),
        "{body}"
    );
    assert!(
        body.contains("   ! 1 of 2 writes superseded later in this commit, or rolled back\n"),
        "{body}"
    );

    // --- trailers, as git parses them --------------------------------
    assert_eq!(
        head_trailer(tmp.path(), "Git-Sync-Txn-Count").as_deref(),
        Some("2"),
        "{body}"
    );
    assert!(
        head_trailer(tmp.path(), "Git-Sync-Origin").is_some(),
        "{body}"
    );
    assert_eq!(
        head_trailer(tmp.path(), "Git-Sync-Next-Version")
            .expect("counter trailer")
            .parse::<i64>()
            .expect("an integer"),
        parsed.next_version,
        "git and the crate parser must agree: {body}"
    );

    // Rows survive the commit, stamped with the oid that carried them.
    let rows = sync.list_transactions().await.expect("list_transactions");
    assert_eq!(rows.len(), 2, "roll-forward must not delete rows");
    for row in &rows {
        assert_eq!(row.commit_id.as_deref(), Some(oid.as_str()), "{row:?}");
    }
}

async fn keys_needing_quoting_round_trip(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    // A key with a space and a quote: bare whitespace-splitting would
    // corrupt this, and there is no second copy to fall back on.
    let awkward = "git://example.com/a repo \"quoted\".git";
    sync.apply_batch(
        vec![upsert_op(awkward)],
        true,
        Some(TxnMeta {
            author: Some("Ada: the first <ada@example.com>".into()),
            message: Some("Handle an awkward key".into()),
        }),
    )
    .await
    .expect("batch");

    sync.commit_repository("Update cloudmap")
        .await
        .expect("commit")
        .expect("dirty");

    let body = head_commit_body(tmp.path());
    let parsed = unfurl_git_sync::parse_commit_rollup(&body)
        .expect("parses")
        .expect("is a git-sync commit");
    assert_eq!(parsed.txns[0].records.len(), 1, "{body}");
    assert_eq!(parsed.txns[0].records[0].key, awkward, "{body}");
    assert_eq!(parsed.txns[0].records[0].path, "/repositories", "{body}");
    // The author is the remainder of the header line, so a colon in it
    // is not a delimiter.
    assert_eq!(
        parsed.txns[0].author.as_deref(),
        Some("Ada: the first <ada@example.com>"),
        "{body}"
    );
}

async fn commit_without_txns_still_records_the_counter(sync: &SyncedRepo, tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");
    // Single-op CRUD writes never record a txn, so this commit has
    // nothing to roll up -- but it still drew a version.
    sync.upsert_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "no-rollup",
        serde_json::json!({"name": "no-rollup"}),
        None,
        false,
    )
    .await
    .expect("upsert");

    sync.commit_repository("just the subject")
        .await
        .expect("commit")
        .expect("something was dirty");

    let body = head_commit_body(tmp.path());
    assert!(!body.contains("Rollup of"), "nothing to roll up: {body}");
    assert_eq!(
        head_trailer(tmp.path(), "Git-Sync-Txn-Count").as_deref(),
        Some("0"),
        "always emitted, so a dropped trailer is detectable: {body}"
    );

    let parsed = unfurl_git_sync::parse_commit_rollup(&body)
        .expect("parses")
        .expect("still a git-sync commit");
    assert!(parsed.txns.is_empty(), "{parsed:?}");
    let version = sync
        .get_record("cloudmap.yaml", "/repositories", "no-rollup")
        .await
        .expect("get")
        .expect("found")
        .version;
    assert!(
        parsed.next_version > version,
        "counter {} must cover the un-rolled-up write at {version}",
        parsed.next_version
    );
}

async fn apply_batch_without_meta_records_no_txn(sync: &SyncedRepo, _tmp: &TempDir) {
    sync.update_from_working_dir(ScanOptions::default())
        .await
        .expect("update");

    // `None` opts out of the audit trail entirely.
    let staged = sync
        .apply_batch(vec![upsert_op("tracked")], true, None)
        .await
        .expect("staging batch");
    assert!(sync
        .list_transactions()
        .await
        .expect("list_transactions")
        .is_empty());
    let v1 = staged.applied[0].outcome.version;

    // A batch that rolls back records nothing either, meta or not: the
    // insert rides the same transaction as the writes it describes.
    sync.update_record(
        Some("cloudmap.yaml"),
        "/repositories",
        "tracked",
        serde_json::json!({"name": "v2"}),
        None,
        false,
    )
    .await
    .expect("out-of-band update makes the token below stale");
    let outcome = sync
        .apply_batch(
            vec![
                upsert_op("fresh"),
                BatchOp::Upsert {
                    file_path: Some("cloudmap.yaml".into()),
                    path: "/repositories".into(),
                    key: "tracked".into(),
                    json: serde_json::json!({"name": "stomp"}),
                    expected: Some(CommitRef::Pending(v1)),
                    resolve: false,
                },
            ],
            true,
            Some(TxnMeta {
                author: Some("someone".into()),
                message: Some("doomed".into()),
            }),
        )
        .await
        .expect("conflicting batch");
    assert!(outcome.applied.is_empty(), "atomic rollback expected");
    assert_eq!(outcome.failed.len(), 1);
    assert!(
        sync.list_transactions()
            .await
            .expect("list_transactions")
            .is_empty(),
        "a rolled-back batch must leave no audit row"
    );
}

crud_test!(rollup_round_trips_through_the_commit_message);
crud_test!(keys_needing_quoting_round_trip);
crud_test!(commit_without_txns_still_records_the_counter);
crud_test!(apply_batch_without_meta_records_no_txn);

// Parser rejection cases. These need no database, so they run once.

#[test]
fn parse_ignores_a_message_that_is_not_from_git_sync() {
    assert!(
        unfurl_git_sync::parse_commit_rollup("Just a commit\n\nWith a body.\n")
            .expect("not an error")
            .is_none()
    );
    // Rollup-looking prose with no trailers is still not a git-sync
    // commit -- someone writing *about* the format.
    let decoy = "Document the format\n\nRollup of 2 git-sync transactions:\n\n - 45-66 on main x\n";
    assert!(unfurl_git_sync::parse_commit_rollup(decoy)
        .expect("not an error")
        .is_none());
}

#[test]
fn parse_rejects_a_message_it_cannot_trust() {
    // Announces itself, then contradicts itself: a squash merge of two
    // git-sync commits looks like this. Reporting "no batches" here
    // would lose history silently, so it must be an error.
    let mismatch = "Subject\n\nRollup of 1 git-sync transaction:\n\n \
                    - 5 on main 2026-08-23T19:46:56-07:00 Ada\n\n\
                    Git-Sync-Txn-Count: 2\nGit-Sync-Next-Version: 9\n";
    assert!(unfurl_git_sync::parse_commit_rollup(mismatch).is_err());

    // The counter trailer without the count trailer.
    let no_count = "Subject\n\nGit-Sync-Next-Version: 9\n";
    assert!(unfurl_git_sync::parse_commit_rollup(no_count).is_err());

    // An unrecognized record flag. The set is documented as closed, so
    // this must be an error rather than a guess -- a parser that fell
    // back to "not a delete" would resurrect deleted records.
    let bad_flag = "Subject\n\nRollup of 1 git-sync transaction:\n\n \
                    - 5 on main 2026-08-23T19:46:56-07:00 Ada\n   \
                    * 5 X \"/repositories\" \"key\"\n\n\
                    Git-Sync-Txn-Count: 1\nGit-Sync-Next-Version: 9\n";
    assert!(unfurl_git_sync::parse_commit_rollup(bad_flag).is_err());

    // A record line that lost its closing quote.
    let truncated = "Subject\n\nRollup of 1 git-sync transaction:\n\n \
                     - 5 on main 2026-08-23T19:46:56-07:00 Ada\n   \
                     * 5 M \"/repositories\" \"unterminated\n\n\
                     Git-Sync-Txn-Count: 1\nGit-Sync-Next-Version: 9\n";
    assert!(unfurl_git_sync::parse_commit_rollup(truncated).is_err());
}
