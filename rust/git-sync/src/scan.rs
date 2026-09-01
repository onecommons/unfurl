// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Taking one file's contents into the database.
//!
//! The per-file transaction behind
//! [`crate::SyncedRepo::update_from_working_dir`]: upsert the file row,
//! upsert every record the document holds, delete the ones that
//! vanished, and leave in-flight client edits alone -- the scan's job
//! is taking in disk changes, not undoing writes that haven't reached
//! the file yet. Where the two disagree, [`crate::conflict`] decides
//! what that means and records it.

use std::collections::BTreeSet;

use crate::conflict::{
    classify_conflict, drop_conflict_row, read_base_docs, record_id, refresh_conflict_row,
    TheirSide,
};
use crate::db::{self, RecordId};
use crate::error::{Error, Result};
use crate::model::{ConflictState, Record, RecordConflict, SyncOutcome};
use crate::sync::SyncedRepo;

/// One file the scan parsed, as the record upsert needs it.
///
/// Grouped for the same reason as [`crate::db::RecordId`]: these travel
/// together through the whole sync path, and `rel_path` and `source_oid`
/// are adjacent `&str`s that a positional call site could silently
/// transpose.
pub(crate) struct ScannedFile<'a> {
    /// Working-tree-relative path of the file.
    pub(crate) rel_path: &'a str,
    /// Commit that last touched the path — what scanned records are
    /// stamped with, clean or dirty. `None` only for a path no commit
    /// has ever carried.
    pub(crate) record_commit_id: Option<&'a str>,
    /// The same commit when the on-disk blob matches the index entry,
    /// `None` when the file is dirty — what the file row gets, so a
    /// NULL there means "this content is not in git".
    pub(crate) file_commit_id: Option<&'a str>,
    /// Blob OID of the exact bytes `value` was parsed from.
    pub(crate) source_oid: &'a str,
    /// The parsed document.
    pub(crate) value: &'a serde_json::Value,
    /// The format that claimed it.
    pub(crate) format: &'a dyn crate::format::DataFormat,
    /// The file wins over every in-flight edit — see
    /// [`crate::ScanOptions::force`].
    pub(crate) force: bool,
    /// `Git-Sync-Resolves-Version: N` from the commit that last touched
    /// the path: in-flight rows at `version <= N` are the file author's
    /// to overwrite. `None` when the commit carries no such trailer.
    pub(crate) resolves_version: Option<i64>,
}

/// A parsed document plus the format that claimed it, as
/// [`SyncedRepo::parse_and_detect`] returns it. The format borrows
/// from the registry, which lives as long as the [`SyncedRepo`].
pub(crate) struct ParsedDoc<'a> {
    pub(crate) format: &'a dyn crate::format::DataFormat,
    pub(crate) value: serde_json::Value,
}

/// Transactional body of [`SyncedRepo::upsert_file_and_records`]: one
/// SQL transaction per file covering the file-row upsert, every record
/// upsert (each stamped with a version drawn *inside* the tx), and the
/// trailing delete of records that vanished from the file.
///
/// In-flight client edits (`commit_id IS NULL`) are **preserved**: the
/// scan's job is taking in disk changes, not undoing writes that
/// haven't reached the file yet. Their `(path, key)`s are skipped by
/// the upsert loop and unioned into [`db::tx::delete_missing`]'s keep
/// set, so a pending update keeps its json, a pending tombstone stays
/// deleted, and a pending create survives having no file-side entry.
/// A tombstone whose key is gone from the file too is quietly kept
/// rather than hard-deleted, so the pending delete stays visible in
/// `list_changes` until [`SyncedRepo::commit_repository`] purges it.
///
/// Where the file *disagrees* with a preserved row — a different value,
/// or the key removed while an edit of it (`base_commit_id` set) is in
/// flight — the file's side is written down too, as a conflict row
/// holding its value (or, when the file dropped the record, the value
/// it dropped). The divergence is reported in
/// [`SyncOutcome::conflicts`], classified by [`RecordConflictKind`],
/// and logged. Conflict rows are kept current here rather than merely
/// created: one whose file value moves again is refreshed, one whose
/// reason has gone — the two sides agree now, or the record it
/// shadowed is no longer in flight — is dropped, and a resolution the
/// file has not invalidated is left standing for the write to apply.
///
/// The file gets the last word instead wherever
/// [`crate::ScanOptions::force`] or a `Git-Sync-Resolves-Version`
/// trailer says so: the in-flight row is overwritten from disk (or, if
/// the file no longer has the record, dropped) and the conflict ends.
///
/// Drawing versions inside the transaction holds the worktree-row lock
/// from the first draw to the commit, so a version becomes visible at
/// exactly the commit that stamps it — a re-sync can no longer surface
/// a lower version after a CRUD batch committed a higher one, which
/// would let a `list_changes` cursor skip records. It also makes each
/// file's sync atomic: a crash mid-file rolls back to the previous
/// state instead of leaving a half-synced file.
///
/// Lock ordering matches [`apply_one_in_tx`]: file row first, then the
/// worktree counter row. Writing the file row first also means the
/// SQLite transaction takes the write lock immediately, sidestepping
/// the deferred-upgrade `SQLITE_BUSY_SNAPSHOT` hazard.
pub(crate) async fn upsert_file_and_records_inner<DB>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    file: ScannedFile<'_>,
    stats: &mut SyncOutcome,
) -> Result<()>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (String, String): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (String, String, String, i64, Option<String>, i64):
        for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (String, String, String, String):
        for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (i64, String, String, String, i64, String):
        for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    let ScannedFile {
        rel_path,
        record_commit_id,
        file_commit_id,
        source_oid,
        value,
        format,
        force,
        resolves_version,
    } = file;

    // Everything that doesn't depend on a read inside the transaction
    // is done before it opens. `upsert_file` below takes SQLite's
    // single write lock immediately, so anything after it is holding
    // that lock — and neither of these needs to.

    // Extracting the records from the parsed document depends only on
    // the document and its format.
    let mut new_keys: BTreeSet<(String, String)> = BTreeSet::new();
    let mut to_upsert: Vec<(String, String, serde_json::Value)> = Vec::new();
    for prefix in format.path_prefixes() {
        let Some(section) = value.get(*prefix).and_then(|v| v.as_object()) else {
            continue;
        };
        for (key, child) in section {
            let path = format!("/{prefix}");
            let key = key.clone();
            new_keys.insert((path.clone(), key.clone()));
            to_upsert.push((path, key, child.clone()));
        }
    }

    // Base-commit content for the file's in-flight rows. Whether the
    // file diverges from a pending edit is a *three-way* question —
    // theirs must have moved off the base, not merely differ from ours,
    // which any unsaved edit trivially does.
    //
    // This is gix work — open the repository, walk a tree, parse the
    // file as it was at that commit — so it is the last thing that
    // should sit inside a write lock. The bases are read
    // non-transactionally to warm the cache; a row that becomes pending
    // between that read and the authoritative one below simply misses,
    // and is read inside the transaction instead. Rare, and it keeps
    // the classification exact rather than falling back on the
    // conservative "unknown base" path.
    let mut base_docs = read_base_docs(
        sync,
        rel_path,
        db::record::pending_bases(sync.db(), sync.worktree_id(), rel_path)
            .await?
            .into_values()
            .flatten()
            .collect::<BTreeSet<String>>(),
    );

    let mut tx = pool.begin().await?;

    // Upsert file row.
    db::tx::upsert_file(
        &mut tx,
        sync.worktree_id(),
        rel_path,
        format.name(),
        file_commit_id,
        source_oid,
    )
    .await?;

    let pending: std::collections::BTreeMap<(String, String), db::tx::PendingRecord> =
        db::tx::list_pending_records(&mut tx, sync.worktree_id(), rel_path)
            .await?
            .into_iter()
            .map(|p| ((p.path.clone(), p.key.clone()), p))
            .collect();
    let committed = db::tx::list_committed_records(&mut tx, sync.worktree_id(), rel_path).await?;
    // The file's side of every divergence already on record, so this
    // scan can refresh one, let a resolution stand, or drop one whose
    // reason has gone away.
    let conflict_rows =
        db::tx::list_conflict_records(&mut tx, sync.worktree_id(), rel_path).await?;
    // Whether the trailer on the file's last commit settles a
    // divergence involving this row. Only ever consulted once the row
    // is *known* to diverge: an ordinary unsaved edit is not something
    // the file's author had to resolve, and overwriting it from a file
    // that still holds its base would destroy work for nothing.
    let resolved_by_trailer =
        |p: &db::tx::PendingRecord| resolves_version.is_some_and(|n| p.version <= n);

    // Bases for rows that became pending since the cache was warmed
    // above. Normally empty, so normally no gix work in here.
    let missed: BTreeSet<String> = pending
        .values()
        .filter(|p| !p.deleted)
        .filter_map(|p| p.base_commit_id.as_deref())
        .filter(|commit| !base_docs.contains_key(*commit))
        .map(str::to_string)
        .collect();
    base_docs.extend(read_base_docs(sync, rel_path, missed));

    for (path, key, child) in to_upsert {
        let at = (path.clone(), key.clone());
        let existing = conflict_rows.get(&at);
        // No in-flight row to protect, or an operator who has declared
        // the working tree authoritative: the file is simply taken in.
        let mut take_file = !pending.contains_key(&at) || force;
        if take_file {
            drop_conflict_row(
                &mut tx,
                sync,
                record_id(sync, rel_path, &path, &key),
                existing,
            )
            .await?;
        } else {
            let p = &pending[&at];
            // A resolution the file has not invalidated stands: the
            // client already chose, and the write applies it.
            if matches!(existing, Some(c)
                if c.state == ConflictState::Resolved && !c.deleted && c.json == child)
            {
                stats.records_preserved += 1;
                continue;
            }
            let base_value = p
                .base_commit_id
                .as_deref()
                .and_then(|base| base_docs.get(base))
                .and_then(|doc| doc.as_ref())
                .and_then(|doc| doc.get(path.trim_start_matches('/')))
                .and_then(|section| section.get(key.as_str()));
            match classify_conflict(
                &p.json,
                p.deleted,
                p.base_commit_id.is_some(),
                base_value,
                Some(&child),
            ) {
                // The author of the file's last commit settled this one.
                Some(_) if resolved_by_trailer(p) => {
                    tracing::info!(
                        file = %rel_path, path = %path, key = %key,
                        "conflict resolved by the file's Git-Sync-Resolves-Version trailer"
                    );
                    drop_conflict_row(
                        &mut tx,
                        sync,
                        record_id(sync, rel_path, &path, &key),
                        existing,
                    )
                    .await?;
                    take_file = true;
                }
                Some(kind) => {
                    stats.records_preserved += 1;
                    tracing::warn!(
                        file = %rel_path, path = %path, key = %key, kind = ?kind,
                        "file diverges from a pending edit; keeping both sides"
                    );
                    refresh_conflict_row(
                        &mut tx,
                        sync,
                        record_id(sync, rel_path, &path, &key),
                        TheirSide {
                            json: &child,
                            deleted: false,
                            commit_id: file_commit_id,
                        },
                        existing,
                    )
                    .await?;
                    stats.conflicts.push(RecordConflict {
                        file_path: rel_path.to_string(),
                        path,
                        key,
                        kind,
                        base_commit_id: p.base_commit_id.clone(),
                        theirs: Some(child),
                    });
                    continue;
                }
                // The two sides agree after all — any conflict row for
                // this key describes a divergence that is over.
                None => {
                    stats.records_preserved += 1;
                    drop_conflict_row(
                        &mut tx,
                        sync,
                        record_id(sync, rel_path, &path, &key),
                        existing,
                    )
                    .await?;
                    continue;
                }
            }
        }
        debug_assert!(take_file);
        // A record matching what the database already holds — same
        // value, same commit attribution — is left alone. Drawing a
        // version here would report it via `list_changes` and
        // invalidate `Pending` OCC tokens for records that merely
        // share a file with the actual change.
        if committed
            .get(&(path.clone(), key.clone()))
            .is_some_and(|(json, commit)| {
                *json == child && Some(commit.as_str()) == record_commit_id
            })
        {
            continue;
        }
        let json_text = serde_json::to_string(&child).map_err(|e| Error::Json {
            path: path.clone(),
            source: e,
        })?;
        let version = db::tx::next_version(&mut tx, sync.family_id(), 1).await?;
        let id = db::tx::sync_upsert_record(
            &mut tx,
            RecordId {
                worktree_id: sync.worktree_id(),
                file_path: rel_path,
                path: &path,
                key: &key,
            },
            &json_text,
            record_commit_id,
            version,
        )
        .await?;
        stats.records_upserted += 1;

        // Aliases.
        let record = Record {
            id,
            worktree_id: sync.worktree_id(),
            file_path: rel_path.to_string(),
            path: path.clone(),
            key: key.clone(),
            commit_id: record_commit_id.map(|s| s.to_string()),
            json: child,
            deleted: false,
            version,
            conflict: None,
        };
        db::tx::replace_aliases(&mut tx, id, &format.find_alias(&record)).await?;
    }

    // Pending rows the file no longer (or never) contains survive the
    // hard delete below too. A live one with a base is a record the
    // disk deleted out from under a client edit — report it. Without a
    // base it's an unsaved create, added on the next save; a tombstone
    // means both sides agree the record goes.
    let mut keep = new_keys;
    for (at, p) in &pending {
        if keep.contains(at) {
            continue; // in the file; the upsert loop handled it
        }
        let (path, key) = at;
        let existing = conflict_rows.get(at);
        if force {
            // Left out of `keep`, so the hard delete below takes it.
            drop_conflict_row(
                &mut tx,
                sync,
                record_id(sync, rel_path, path, key),
                existing,
            )
            .await?;
            continue;
        }
        // The tombstone-shaped resolution: the client chose its own row
        // over the file having dropped the record.
        if matches!(existing, Some(c) if c.state == ConflictState::Resolved && c.deleted) {
            keep.insert(at.clone());
            stats.records_preserved += 1;
            continue;
        }
        match classify_conflict(&p.json, p.deleted, p.base_commit_id.is_some(), None, None) {
            // The file dropped the record and the author of its last
            // commit said that settles it.
            Some(_) if resolved_by_trailer(p) => {
                tracing::info!(
                    file = %rel_path, path = %path, key = %key,
                    "deletion resolved by the file's Git-Sync-Resolves-Version trailer"
                );
                drop_conflict_row(
                    &mut tx,
                    sync,
                    record_id(sync, rel_path, path, key),
                    existing,
                )
                .await?;
                continue;
            }
            Some(kind) => {
                keep.insert(at.clone());
                stats.records_preserved += 1;
                tracing::warn!(
                    file = %rel_path, path = %path, key = %key,
                    "record deleted from file under a pending edit; keeping both sides"
                );
                // The conflict row's json is NOT NULL and the file has
                // no value to give it, so it holds the one the file
                // dropped — the same reading a tombstone's json has.
                let dropped = p
                    .base_commit_id
                    .as_deref()
                    .and_then(|base| base_docs.get(base))
                    .and_then(|doc| doc.as_ref())
                    .and_then(|doc| doc.get(path.trim_start_matches('/')))
                    .and_then(|section| section.get(key.as_str()))
                    .unwrap_or(&p.json);
                refresh_conflict_row(
                    &mut tx,
                    sync,
                    record_id(sync, rel_path, path, key),
                    TheirSide {
                        json: dropped,
                        deleted: true,
                        commit_id: file_commit_id,
                    },
                    existing,
                )
                .await?;
                stats.conflicts.push(RecordConflict {
                    file_path: rel_path.to_string(),
                    path: path.clone(),
                    key: key.clone(),
                    kind,
                    base_commit_id: p.base_commit_id.clone(),
                    theirs: None,
                });
            }
            // An unsaved create, or a tombstone the file already agrees
            // with: nothing to disagree about, so it is simply kept.
            None => {
                keep.insert(at.clone());
                stats.records_preserved += 1;
                drop_conflict_row(
                    &mut tx,
                    sync,
                    record_id(sync, rel_path, path, key),
                    existing,
                )
                .await?;
            }
        }
    }

    // A conflict row with nothing left to disagree with — the record it
    // shadowed was committed, resolved, or dropped — describes a
    // divergence that no longer exists.
    for (at, row) in &conflict_rows {
        if !pending.contains_key(at) {
            let (path, key) = at;
            drop_conflict_row(
                &mut tx,
                sync,
                record_id(sync, rel_path, path, key),
                Some(row),
            )
            .await?;
        }
    }

    // Delete records that used to be in the file but are gone now.
    let removed = db::tx::delete_missing(&mut tx, sync.worktree_id(), rel_path, &keep).await?;
    stats.records_deleted += removed;

    tx.commit().await?;
    Ok(())
}

/// Pair each orphaned file with the new path holding its exact bytes.
///
/// `arrivals` is `(path, blob oid)` for every tracked path the database
/// has no row for. Only an unambiguous pairing counts -- one orphan and
/// one arrival sharing a blob. Two identical files where one moved, or
/// a copy rather than a move, leave one side ambiguous, and a wrong
/// guess would silently re-point a file's whole record set, so those
/// fall back to delete-and-add: noisier, but never invented.
pub(crate) fn match_renames(
    orphans: &[String],
    known_files: &std::collections::HashMap<String, crate::model::File>,
    arrivals: &[(&str, &str)],
) -> Vec<(String, String)> {
    use std::collections::HashMap;
    let mut by_blob: HashMap<&str, Vec<&str>> = HashMap::new();
    for (path, blob) in arrivals {
        by_blob.entry(blob).or_default().push(path);
    }
    let mut departures: HashMap<&str, Vec<&str>> = HashMap::new();
    for path in orphans {
        if let Some(oid) = known_files[path].source_oid.as_deref() {
            departures.entry(oid).or_default().push(path.as_str());
        }
    }
    let mut out: Vec<(String, String)> = departures
        .iter()
        .filter_map(
            |(oid, from)| match (from.as_slice(), by_blob.get(oid).map(Vec::as_slice)) {
                ([from], Some([to])) => Some(((*from).to_string(), (*to).to_string())),
                _ => None,
            },
        )
        .collect();
    out.sort();
    out
}
