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

use std::collections::{BTreeSet, HashMap};

use crate::conflict::{
    classify_conflict, drop_conflict_row, read_base_docs, record_id, refresh_conflict_row,
    TheirSide,
};
use crate::db;
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

/// What deciding one record's fate needs beyond the transaction.
///
/// The two reconcile passes — one for a record the file still holds, one
/// for a record it dropped — ask the same questions of the same four
/// things, so they hang off one context rather than taking a fistful of
/// positional arguments each.
struct Reconcile<'a> {
    sync: &'a SyncedRepo,
    file: &'a ScannedFile<'a>,
    /// The file's content at each base commit an in-flight row names.
    base_docs: &'a HashMap<String, Option<serde_json::Value>>,
    /// The file's side of every divergence already on record, so a scan
    /// can refresh one, let a resolution stand, or drop one whose reason
    /// has gone away.
    conflicts: &'a std::collections::BTreeMap<(String, String), db::tx::ConflictRecord>,
}

/// A pending row's content at the commit its edit was based on.
///
/// `None` whenever any step of the lookup is missing — no base, an
/// unreadable one, or a base that had no such record. Both passes need
/// it, and [`classify_conflict`] reads a `None` as diverged.
fn base_value<'a>(
    base_docs: &'a HashMap<String, Option<serde_json::Value>>,
    base_commit_id: Option<&str>,
    path: &str,
    key: &str,
) -> Option<&'a serde_json::Value> {
    base_docs
        .get(base_commit_id?)?
        .as_ref()?
        .get(path.trim_start_matches('/'))?
        .get(key)
}

/// The `(path, key, value)` of every record a parsed document holds,
/// under each path prefix its format claims.
fn document_records(
    value: &serde_json::Value,
    format: &dyn crate::format::DataFormat,
) -> Vec<(String, String, serde_json::Value)> {
    let mut records: Vec<(String, String, serde_json::Value)> = Vec::new();
    for prefix in format.path_prefixes() {
        let Some(section) = value.get(*prefix).and_then(|v| v.as_object()) else {
            continue;
        };
        for (key, child) in section {
            records.push((format!("/{prefix}"), key.clone(), child.clone()));
        }
    }
    records
}

impl Reconcile<'_> {
    /// Whether the trailer on the file's last commit settles a
    /// divergence involving this row.
    ///
    /// Only ever consulted once the row is *known* to diverge: an
    /// ordinary unsaved edit is not something the file's author had to
    /// resolve, and overwriting it from a file that still holds its base
    /// would destroy work for nothing.
    fn resolved_by_trailer(&self, p: &db::tx::PendingRecord) -> bool {
        self.file.resolves_version.is_some_and(|n| p.version <= n)
    }

    /// What the file holding `child` at `at` means for the row already
    /// there.
    ///
    /// `true` says the file's value is the database's to take: nothing
    /// in-flight stands in its way, or something did and the file's author
    /// settled it. `false` leaves the row alone — the caller must not write.
    /// Either way any conflict row for the key is left describing the
    /// current state of the disagreement, or dropped when there no longer is
    /// one.
    async fn present<DB>(
        &self,
        tx: &mut sqlx::Transaction<'_, DB>,
        at: &(String, String),
        child: &serde_json::Value,
        pending: Option<&db::tx::PendingRecord>,
        stats: &mut SyncOutcome,
    ) -> Result<bool>
    where
        DB: db::tx::Dialect,
        for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
        for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
        for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
        for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
        for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
        (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    {
        let (path, key) = at;
        let id = record_id(self.sync, self.file.rel_path, path, key);
        let existing = self.conflicts.get(at);
        // No in-flight row to protect, or an operator who has declared the
        // working tree authoritative: the file is simply taken in.
        let Some(p) = pending.filter(|_| !self.file.force) else {
            drop_conflict_row(tx, self.sync, id, existing).await?;
            return Ok(true);
        };
        // A resolution the file has not invalidated stands: the client
        // already chose, and the write applies it.
        if matches!(existing, Some(c)
        if c.state == ConflictState::Resolved && !c.deleted && c.json == *child)
        {
            stats.records_preserved += 1;
            return Ok(false);
        }
        let base = base_value(self.base_docs, p.base_commit_id.as_deref(), path, key);
        match classify_conflict(
            &p.json,
            p.deleted,
            p.base_commit_id.is_some(),
            base,
            Some(child),
        ) {
            // The author of the file's last commit settled this one.
            Some(_) if self.resolved_by_trailer(p) => {
                tracing::info!(
                    file = %self.file.rel_path, path = %path, key = %key,
                    "conflict resolved by the file's Git-Sync-Resolves-Version trailer"
                );
                drop_conflict_row(tx, self.sync, id, existing).await?;
                Ok(true)
            }
            Some(kind) => {
                stats.records_preserved += 1;
                tracing::warn!(
                    file = %self.file.rel_path, path = %path, key = %key, kind = ?kind,
                    "file diverges from a pending edit; keeping both sides"
                );
                refresh_conflict_row(
                    tx,
                    self.sync,
                    id,
                    TheirSide {
                        json: child,
                        deleted: false,
                        commit_id: self.file.file_commit_id,
                    },
                    existing,
                )
                .await?;
                stats.conflicts.push(RecordConflict {
                    file_path: self.file.rel_path.to_string(),
                    path: path.clone(),
                    key: key.clone(),
                    kind,
                    base_commit_id: p.base_commit_id.clone(),
                    theirs: Some(child.clone()),
                });
                Ok(false)
            }
            // The two sides agree after all — any conflict row for this key
            // describes a divergence that is over.
            None => {
                stats.records_preserved += 1;
                drop_conflict_row(tx, self.sync, id, existing).await?;
                Ok(false)
            }
        }
    }

    /// What the file no longer holding `at` means for the in-flight row that
    /// still does.
    ///
    /// `true` keeps the row out of the trailing [`db::tx::delete_missing`] —
    /// either both sides agree the record goes, or they disagree and the
    /// disagreement is now on record. `false` lets the delete take it.
    async fn absent<DB>(
        &self,
        tx: &mut sqlx::Transaction<'_, DB>,
        at: &(String, String),
        p: &db::tx::PendingRecord,
        stats: &mut SyncOutcome,
    ) -> Result<bool>
    where
        DB: db::tx::Dialect,
        for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
        for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
        for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
        for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
        for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
        (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    {
        let (path, key) = at;
        let id = record_id(self.sync, self.file.rel_path, path, key);
        let existing = self.conflicts.get(at);
        if self.file.force {
            drop_conflict_row(tx, self.sync, id, existing).await?;
            return Ok(false);
        }
        // The tombstone-shaped resolution: the client chose its own row over
        // the file having dropped the record.
        if matches!(existing, Some(c) if c.state == ConflictState::Resolved && c.deleted) {
            stats.records_preserved += 1;
            return Ok(true);
        }
        match classify_conflict(&p.json, p.deleted, p.base_commit_id.is_some(), None, None) {
            // The file dropped the record and the author of its last commit
            // said that settles it.
            Some(_) if self.resolved_by_trailer(p) => {
                tracing::info!(
                    file = %self.file.rel_path, path = %path, key = %key,
                    "deletion resolved by the file's Git-Sync-Resolves-Version trailer"
                );
                drop_conflict_row(tx, self.sync, id, existing).await?;
                Ok(false)
            }
            Some(kind) => {
                stats.records_preserved += 1;
                tracing::warn!(
                    file = %self.file.rel_path, path = %path, key = %key,
                    "record deleted from file under a pending edit; keeping both sides"
                );
                // The conflict row's json is NOT NULL and the file has no
                // value to give it, so it holds the one the file dropped —
                // the same reading a tombstone's json has.
                let dropped = base_value(self.base_docs, p.base_commit_id.as_deref(), path, key)
                    .unwrap_or(&p.json);
                refresh_conflict_row(
                    tx,
                    self.sync,
                    id,
                    TheirSide {
                        json: dropped,
                        deleted: true,
                        commit_id: self.file.file_commit_id,
                    },
                    existing,
                )
                .await?;
                stats.conflicts.push(RecordConflict {
                    file_path: self.file.rel_path.to_string(),
                    path: path.clone(),
                    key: key.clone(),
                    kind,
                    base_commit_id: p.base_commit_id.clone(),
                    theirs: None,
                });
                Ok(true)
            }
            // An unsaved create, or a tombstone the file already agrees
            // with: nothing to disagree about, so it is simply kept.
            None => {
                stats.records_preserved += 1;
                drop_conflict_row(tx, self.sync, id, existing).await?;
                Ok(true)
            }
        }
    }

    /// Take the file's value for one record: draw a version, upsert the row,
    /// and replace its aliases.
    async fn write_record<DB>(
        &self,
        tx: &mut sqlx::Transaction<'_, DB>,
        at: &(String, String),
        child: serde_json::Value,
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
    {
        let (path, key) = at;
        let json_text = serde_json::to_string(&child).map_err(|e| Error::Json {
            path: path.clone(),
            source: e,
        })?;
        let version = db::tx::next_version(tx, self.sync.family_id(), 1).await?;
        let id = db::tx::sync_upsert_record(
            tx,
            record_id(self.sync, self.file.rel_path, path, key),
            &json_text,
            self.file.record_commit_id,
            version,
        )
        .await?;
        stats.records_upserted += 1;

        let record = Record {
            id,
            worktree_id: self.sync.worktree_id(),
            file_path: self.file.rel_path.to_string(),
            path: path.clone(),
            key: key.clone(),
            commit_id: self.file.record_commit_id.map(|s| s.to_string()),
            json: child,
            deleted: false,
            version,
            conflict: None,
        };
        db::tx::replace_aliases(tx, id, &self.file.format.find_alias(&record)).await?;
        Ok(())
    }
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
    // Everything that doesn't depend on a read inside the transaction
    // is done before it opens. `upsert_file` below takes SQLite's
    // single write lock immediately, so anything after it is holding
    // that lock — and neither of these needs to.

    // Extracting the records from the parsed document depends only on
    // the document and its format.
    let to_upsert = document_records(file.value, file.format);

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
        file.rel_path,
        db::record::pending_bases(sync.db(), sync.worktree_id(), file.rel_path)
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
        file.rel_path,
        file.format.name(),
        file.file_commit_id,
        file.source_oid,
    )
    .await?;

    let pending: std::collections::BTreeMap<(String, String), db::tx::PendingRecord> =
        db::tx::list_pending_records(&mut tx, sync.worktree_id(), file.rel_path)
            .await?
            .into_iter()
            .map(|p| ((p.path.clone(), p.key.clone()), p))
            .collect();
    let committed =
        db::tx::list_committed_records(&mut tx, sync.worktree_id(), file.rel_path).await?;
    let conflict_rows =
        db::tx::list_conflict_records(&mut tx, sync.worktree_id(), file.rel_path).await?;

    // Bases for rows that became pending since the cache was warmed
    // above. Normally empty, so normally no gix work in here.
    let missed: BTreeSet<String> = pending
        .values()
        .filter(|p| !p.deleted)
        .filter_map(|p| p.base_commit_id.as_deref())
        .filter(|commit| !base_docs.contains_key(*commit))
        .map(str::to_string)
        .collect();
    base_docs.extend(read_base_docs(sync, file.rel_path, missed));

    let reconcile = Reconcile {
        sync,
        file: &file,
        base_docs: &base_docs,
        conflicts: &conflict_rows,
    };

    // Every key the file holds, so the trailing delete leaves them
    // alone. Built before the loop below consumes the records.
    let mut keep: BTreeSet<(String, String)> = to_upsert
        .iter()
        .map(|(path, key, _)| (path.clone(), key.clone()))
        .collect();

    for (path, key, child) in to_upsert {
        let at = (path, key);
        if !reconcile
            .present(&mut tx, &at, &child, pending.get(&at), stats)
            .await?
        {
            continue;
        }
        // A record matching what the database already holds — same
        // value, same commit attribution — is left alone. Drawing a
        // version here would report it via `list_changes` and
        // invalidate `Pending` OCC tokens for records that merely
        // share a file with the actual change.
        if committed.get(&at).is_some_and(|(json, commit)| {
            *json == child && Some(commit.as_str()) == file.record_commit_id
        }) {
            continue;
        }
        reconcile.write_record(&mut tx, &at, child, stats).await?;
    }

    // Pending rows the file no longer (or never) contains survive the
    // hard delete below too. A live one with a base is a record the
    // disk deleted out from under a client edit — report it. Without a
    // base it's an unsaved create, added on the next save; a tombstone
    // means both sides agree the record goes.
    for (at, p) in &pending {
        if keep.contains(at) {
            continue; // in the file; the upsert loop handled it
        }
        if reconcile.absent(&mut tx, at, p, stats).await? {
            keep.insert(at.clone());
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
                record_id(sync, file.rel_path, path, key),
                Some(row),
            )
            .await?;
        }
    }

    // Delete records that used to be in the file but are gone now.
    let removed = db::tx::delete_missing(&mut tx, sync.worktree_id(), file.rel_path, &keep).await?;
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
