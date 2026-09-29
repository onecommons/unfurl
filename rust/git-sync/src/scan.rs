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

use std::collections::{BTreeMap, BTreeSet};

use crate::conflict::{classify_conflict, drop_conflict_row, refresh_conflict_row, TheirSide};
use crate::db::seg::{At, FileRow, Filter, NewRow, Row, Scope, Segments};
use crate::error::Result;
use crate::format::SectionKind;
use crate::model::{ConflictState, Record, RecordConflict, SyncOutcome};
use crate::segments::{self, FileWins, Origin, Value};
use crate::sync::SyncedRepo;

/// One file the scan parsed, as the record upsert needs it.
///
/// Grouped because these travel together through the whole sync path,
/// and `rel_path` and `source_oid` are adjacent `&str`s that a positional
/// call site could silently transpose.
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
    /// What the format made of the document — see
    /// [`crate::DataFormat::validate_document`].
    ///
    /// Sections and records it rejected are left out of the index and out
    /// of the prune: a rejected record keeps whatever row it already had
    /// rather than being overwritten by a value known to be wrong, and a
    /// rejected *section* keeps all of its rows, because a section that
    /// isn't a mapping enumerates as empty and would otherwise read as
    /// "every record here was removed".
    pub(crate) validation: &'a crate::Validation,
}

/// A parsed document plus the format that claimed it, as
/// [`SyncedRepo::parse_and_detect`] returns it. The format borrows
/// from the registry, which lives as long as the [`SyncedRepo`].
pub(crate) struct ParsedDoc<'a> {
    pub(crate) format: &'a dyn crate::format::DataFormat,
    pub(crate) value: serde_json::Value,
    /// What [`crate::DataFormat::validate_document`] made of it. A
    /// `fatal` document never gets this far — the scan skips the file.
    pub(crate) validation: std::sync::Arc<crate::Validation>,
}

/// The `(path, key, value)` of every record a parsed document holds,
/// under each path prefix its format claims.
///
/// A [`SectionKind::Singleton`] prefix yields one record for the whole
/// section, keyed by the section's own name; a `Map` prefix yields one
/// per child.
fn document_records(
    value: &serde_json::Value,
    format: &dyn crate::format::DataFormat,
    validation: &crate::Validation,
) -> Vec<(String, String, serde_json::Value)> {
    let mut records: Vec<(String, String, serde_json::Value)> = Vec::new();
    for prefix in format.path_prefixes() {
        if format.section_kind(prefix) == SectionKind::Singleton {
            // Absent is the only way a singleton has no record: unlike a
            // map section there is nothing to enumerate, so whatever is
            // there is the record, object or not. A singleton's key is the
            // section's own name, and so is the only way validation can
            // have addressed it.
            if let Some(child) = value.get(*prefix) {
                if validation.rejects(prefix, prefix) {
                    continue;
                }
                records.push((format!("/{prefix}"), (*prefix).to_string(), child.clone()));
            }
            continue;
        }
        let Some(section) = value.get(*prefix).and_then(|v| v.as_object()) else {
            continue;
        };
        for (key, child) in section {
            // A rejected record is not extracted, so it cannot overwrite
            // the row already there; `upsert_file_and_records_inner` also
            // keeps that row out of the prune, or skipping would delete
            // it instead of preserving it.
            if validation.rejects(prefix, key) {
                continue;
            }
            records.push((format!("/{prefix}"), key.clone(), child.clone()));
        }
    }
    records
}

/// Every `(section, key)` validation rejected as a single record.
///
/// Section-level rejections are excluded: they have no key, and the prune
/// skips their whole path rather than listing keys it cannot enumerate.
fn validation_rejected_records(
    validation: &crate::Validation,
) -> impl Iterator<Item = (&str, String)> {
    validation
        .errors
        .keys()
        .filter_map(|(section, key)| key.as_ref().map(|k| (section.as_str(), k.clone())))
}

/// One file whose blob at HEAD differs from what the committed segments
/// hold: its records at HEAD, or `None` when HEAD no longer has it.
pub(crate) struct HeadFile {
    pub(crate) rel_path: String,
    pub(crate) blob: Option<String>,
    pub(crate) format: String,
    /// `(path, key, value)` of every record, as [`document_records`]
    /// extracts them.
    pub(crate) records: Vec<(String, String, serde_json::Value)>,
    /// Records and sections validation rejected: their rows are left as
    /// they are.
    pub(crate) rejected: BTreeSet<(String, String)>,
    pub(crate) skip_paths: BTreeSet<String>,
}

impl HeadFile {
    pub(crate) fn new(
        rel_path: String,
        blob: Option<String>,
        doc: Option<&ParsedDoc<'_>>,
        format: &str,
    ) -> Self {
        let (records, rejected, skip_paths) = match doc {
            Some(doc) => (
                document_records(&doc.value, doc.format, &doc.validation),
                validation_rejected_records(&doc.validation)
                    .map(|(section, key)| (format!("/{section}"), key))
                    .collect(),
                doc.validation
                    .rejected_sections()
                    .map(|section| format!("/{section}"))
                    .collect(),
            ),
            None => Default::default(),
        };
        HeadFile {
            rel_path,
            blob,
            format: format.to_string(),
            records,
            rejected,
            skip_paths,
        }
    }

    fn skips(&self, path: &str, key: &str) -> bool {
        self.skip_paths.contains(path)
            || self.rejected.contains(&(path.to_string(), key.to_string()))
    }
}

/// A place in a file, owned.
type Place = (String, String, String);

fn place_at(p: &Place) -> At<'_> {
    At {
        file_path: &p.0,
        path: &p.1,
        key: &p.2,
    }
}

/// The committed side of a scan (§4.3): bring the head up to `commit`,
/// whose blobs of `files` differ from what the committed segments hold.
/// Returns the files whose draft side has to be reconciled again: those
/// that changed, and those pending edits followed their records out of.
async fn advance_head<DB: Segments>(
    tx: &mut sqlx::Transaction<'_, DB>,
    sync: &SyncedRepo,
    commit: &str,
    files: &[HeadFile],
    stats: &mut SyncOutcome,
) -> Result<BTreeSet<String>> {
    let w = sync.worktree_id();
    let chain = DB::visible(tx, w, Scope::Chain, Filter::All).await?;
    let live_at = |p: &Place| {
        chain
            .iter()
            .find(|r| !r.deleted && (&r.file_path, &r.path, &r.key) == (&p.0, &p.1, &p.2))
    };
    let mut changes: BTreeMap<Place, Option<serde_json::Value>> = BTreeMap::new();
    for f in files {
        let head: BTreeMap<Place, &serde_json::Value> = f
            .records
            .iter()
            .map(|(path, key, v)| ((f.rel_path.clone(), path.clone(), key.clone()), v))
            .collect();
        let shown: BTreeSet<Place> = chain
            .iter()
            .filter(|r| r.file_path == f.rel_path && !r.deleted)
            .map(|r| (r.file_path.clone(), r.path.clone(), r.key.clone()))
            .collect();
        for p in head.keys().chain(&shown).collect::<BTreeSet<_>>() {
            if f.skips(&p.1, &p.2) {
                continue;
            }
            let now = head.get(p).copied();
            if now != live_at(p).map(|r| &r.json) {
                changes.insert(p.clone(), now.cloned());
            }
        }
    }
    if changes.is_empty() {
        return Ok(BTreeSet::new());
    }
    // A record that left one file and arrived at the same section and key
    // of another in this scan moved: it keeps its `key_id` (§3.5).
    let mut moved: BTreeMap<Place, i64> = BTreeMap::new();
    for (p, change) in &changes {
        if change.is_none() || live_at(p).is_some() {
            continue;
        }
        let from = changes.iter().find(|(q, c)| {
            c.is_none() && q.0 != p.0 && (&q.1, &q.2) == (&p.1, &p.2) && live_at(q).is_some()
        });
        if let Some((q, _)) = from {
            moved.insert(p.clone(), live_at(q).expect("checked").key_id);
        }
    }
    // no two places share an id: those unchanged keep theirs, and each
    // place scanned takes one no earlier place took
    let kept: BTreeSet<i64> = chain
        .iter()
        .filter(|r| !r.deleted)
        .filter(|r| !changes.contains_key(&(r.file_path.clone(), r.path.clone(), r.key.clone())))
        .map(|r| r.key_id)
        .collect();
    let mut given: BTreeSet<i64> = BTreeSet::new();
    let first = DB::next_version(tx, sync.family_id(), changes.len() as i64).await?;
    for (i, (p, change)) in changes.iter().enumerate() {
        let ids = segments::KeyIds {
            moved: moved.get(p).copied(),
            elsewhere: kept.union(&given).copied().collect(),
        };
        let written = segments::scan_key(
            tx,
            w,
            place_at(p),
            change.as_ref(),
            commit,
            first + i as i64,
            ids,
        )
        .await?;
        if let (Some(written), Some(value)) = (written, change) {
            given.insert(written.key_id);
            stats.records_upserted += 1;
            if let Some(format) = sync.formats().for_path(&p.1) {
                let record = Record {
                    id: written.key_id,
                    worktree_id: w,
                    file_path: p.0.clone(),
                    path: p.1.clone(),
                    key: p.2.clone(),
                    commit_id: Some(commit.to_string()),
                    json: value.clone(),
                    deleted: false,
                    version: first + i as i64,
                    conflict: None,
                };
                DB::replace_aliases(tx, written.id, &format.find_alias(&record)).await?;
            }
        } else if change.is_none() {
            stats.records_deleted += 1;
        }
    }
    let mut touched: BTreeSet<String> = changes.keys().map(|p| p.0.clone()).collect();
    touched.extend(segments::follow_records(tx, w).await?);
    Ok(touched)
}

/// One file's working tree, as the draft side of a scan takes it in.
pub(crate) struct DiskFile<'a> {
    pub(crate) file: ScannedFile<'a>,
    /// The file is gone from the working tree: its records are taken in
    /// as deleted and the file row marked for removal.
    pub(crate) gone: bool,
}

/// Whether the file's value `theirs` diverges from pending edit `x`: the
/// three-way decision, against the committed version the edit was made
/// over.
fn diverges(
    x: &Row,
    theirs: Option<&serde_json::Value>,
) -> Option<crate::model::RecordConflictKind> {
    classify_conflict(
        &x.json,
        x.deleted,
        x.base_commit_id.is_some(),
        x.base_json.as_ref(),
        theirs,
    )
}

/// The draft side of a scan (§4.3): take one file's working tree into
/// the draft, key by key, against the committed chain. A pending edit the
/// file disagrees with keeps both sides, as a conflict row, unless the
/// file has the last word ([`FileWins`]).
async fn reconcile_file<DB: Segments>(
    tx: &mut sqlx::Transaction<'_, DB>,
    sync: &SyncedRepo,
    disk: &DiskFile<'_>,
    stats: &mut SyncOutcome,
) -> Result<()> {
    let w = sync.worktree_id();
    let file = &disk.file;
    let d = DB::segs(tx, w).await?.draft;
    let theirs_all: BTreeMap<(String, String), serde_json::Value> = if disk.gone {
        BTreeMap::new()
    } else {
        document_records(file.value, file.format, file.validation)
            .into_iter()
            .map(|(path, key, v)| ((path, key), v))
            .collect()
    };
    let rejected: BTreeSet<(String, String)> = validation_rejected_records(file.validation)
        .map(|(section, key)| (format!("/{section}"), key))
        .collect();
    let skip_paths: BTreeSet<String> = file
        .validation
        .rejected_sections()
        .map(|section| format!("/{section}"))
        .collect();
    let chain = DB::visible(tx, w, Scope::Chain, Filter::File(file.rel_path)).await?;
    let draft = DB::rows_in(tx, d, Some(file.rel_path), false).await?;
    let conflicts = DB::rows_in(tx, d, Some(file.rel_path), true).await?;
    let wins = if file.force {
        FileWins::Always
    } else {
        file.resolves_version
            .map_or(FileWins::Never, FileWins::Diverged)
    };
    let places: BTreeSet<(String, String)> = theirs_all
        .keys()
        .cloned()
        .chain(chain.iter().map(|r| (r.path.clone(), r.key.clone())))
        .chain(draft.iter().map(|r| (r.path.clone(), r.key.clone())))
        .chain(conflicts.iter().map(|r| (r.path.clone(), r.key.clone())))
        .collect();
    for (path, key) in places {
        if skip_paths.contains(&path) || rejected.contains(&(path.clone(), key.clone())) {
            continue;
        }
        let at = At {
            file_path: file.rel_path,
            path: &path,
            key: &key,
        };
        let theirs = theirs_all.get(&(path.clone(), key.clone()));
        let existing = segments::conflict_row(tx, d, at).await?;
        // the edit the file's value replaces: its record continues
        let mut withdrawn = None;
        let mut x = DB::rows_in(tx, d, Some(file.rel_path), false)
            .await?
            .into_iter()
            .find(|r| r.at() == at);
        if let Some(p) = x.as_ref().filter(|x| x.is_edit()) {
            // A resolution the file hasn't moved under stands: the client
            // already chose, and the next write applies it.
            let stands = existing.as_ref().is_some_and(|c| {
                c.conflict == Some(ConflictState::Resolved)
                    && (!c.deleted).then_some(&c.json) == theirs
            });
            let kind = diverges(p, theirs);
            let file_wins = match wins {
                FileWins::Never => false,
                FileWins::Always => true,
                FileWins::Diverged(n) => p.version <= n && !stands && kind.is_some(),
            };
            if !file_wins {
                stats.records_preserved += 1;
                if stands {
                    continue;
                }
                match kind {
                    Some(kind) => {
                        tracing::warn!(
                            file = file.rel_path, path, key, kind = ?kind,
                            "file diverges from a pending edit; keeping both sides"
                        );
                        // With the record gone from the file, the conflict
                        // row holds the value it dropped.
                        let dropped = p.base_json.as_ref().unwrap_or(&p.json);
                        refresh_conflict_row(
                            tx,
                            sync,
                            at,
                            TheirSide {
                                json: theirs.unwrap_or(dropped),
                                deleted: theirs.is_none(),
                                commit_id: file.file_commit_id,
                            },
                            existing.as_ref(),
                        )
                        .await?;
                        stats.conflicts.push(RecordConflict {
                            file_path: file.rel_path.to_string(),
                            path: path.clone(),
                            key: key.clone(),
                            kind,
                            base_commit_id: p.base_commit_id.clone(),
                            theirs: theirs.cloned(),
                        });
                    }
                    None => drop_conflict_row(tx, sync, at, existing.as_ref()).await?,
                }
                continue;
            }
            tracing::info!(
                file = file.rel_path,
                path,
                key,
                "the file's value replaces a pending edit"
            );
            withdrawn = Some(p.key_id);
            segments::remove_draft_row(tx, d, at).await?;
            x = None;
        }
        drop_conflict_row(tx, sync, at, existing.as_ref()).await?;
        let committed = chain.iter().find(|r| r.at() == at && !r.deleted);
        // a value taken in from the file is the record git has at its
        // place, else the draft's there unless git has it at another
        // place, else a new one
        let elsewhere: BTreeSet<i64> = DB::visible(tx, w, Scope::Chain, Filter::All)
            .await?
            .into_iter()
            .filter(|r| !r.deleted && r.at() != at)
            .map(|r| r.key_id)
            .collect();
        if let Some(r) = &x {
            match committed {
                Some(c) if c.key_id != r.key_id => {
                    DB::set_key_id(tx, r.id, c.key_id).await?;
                    segments::retag(tx, d, r.key_id, c.key_id).await?;
                }
                None if elsewhere.contains(&r.key_id) => {
                    let fresh = segments::renew(tx, d, r).await?;
                    x = DB::rows_in(tx, d, Some(file.rel_path), false)
                        .await?
                        .into_iter()
                        .find(|y| y.key_id == fresh);
                }
                _ => {}
            }
        }
        let withdrawn = withdrawn.filter(|id| !elsewhere.contains(id));
        if theirs == committed.map(|c| &c.json) {
            if x.is_some() {
                segments::remove_draft_row(tx, d, at).await?;
            }
            continue;
        }
        if x.as_ref().map(|r| (!r.deleted).then_some(&r.json)) == Some(theirs) {
            continue;
        }
        let version = DB::next_version(tx, sync.family_id(), 1).await?;
        let value = match theirs {
            Some(v) => Value::Live(v),
            None => Value::Deleted,
        };
        let written = segments::write(
            tx,
            w,
            at,
            value,
            Origin::File(file.record_commit_id),
            committed.map(|c| c.key_id).or(withdrawn),
            version,
        )
        .await?;
        match theirs {
            Some(v) => {
                stats.records_upserted += 1;
                let record = Record {
                    id: written.key_id,
                    worktree_id: w,
                    file_path: file.rel_path.to_string(),
                    path: path.clone(),
                    key: key.clone(),
                    commit_id: file.record_commit_id.map(str::to_string),
                    json: v.clone(),
                    deleted: false,
                    version,
                    conflict: None,
                };
                DB::replace_aliases(tx, written.id, &file.format.find_alias(&record)).await?;
            }
            None => stats.records_deleted += 1,
        }
    }
    DB::upsert_file(
        tx,
        w,
        &FileRow {
            path: file.rel_path,
            format: file.format.name(),
            commit_id: Some(file.file_commit_id),
            source_oid: Some((!file.source_oid.is_empty()).then_some(file.source_oid)),
            committed_oid: None,
        },
    )
    .await?;
    if disk.gone {
        DB::set_file_deleted(tx, w, file.rel_path, true).await?;
    }
    Ok(())
}

/// A file moved in the working tree, uncommitted: its records keep their
/// `key_id`s and versions at `to`. The draft's own rows move there; a
/// committed row gets a draft row at `to` and a draft tombstone at
/// `from`, which the next commit folds into the head.
async fn rename_file<DB: Segments>(
    tx: &mut sqlx::Transaction<'_, DB>,
    sync: &SyncedRepo,
    from: &str,
    to: &str,
) -> Result<()> {
    let w = sync.worktree_id();
    let d = DB::segs(tx, w).await?.draft;
    let moved_at = |r: &Row| (r.path.clone(), r.key.clone());
    let mut held: BTreeSet<(String, String)> = BTreeSet::new();
    for x in DB::rows_in(tx, d, Some(from), false)
        .await?
        .into_iter()
        .chain(DB::rows_in(tx, d, Some(from), true).await?)
    {
        let at = At {
            file_path: to,
            path: &x.path,
            key: &x.key,
        };
        DB::relocate(tx, x.id, at).await?;
        if x.conflict.is_none() {
            held.insert(moved_at(&x));
        }
    }
    for c in DB::visible(tx, w, Scope::Chain, Filter::File(from)).await? {
        if c.deleted {
            continue;
        }
        let old = At {
            file_path: from,
            path: &c.path,
            key: &c.key,
        };
        let row = |at, deleted| NewRow {
            at,
            key_id: Some(c.key_id),
            commit_id: c.commit_id.as_deref(),
            json: &c.json,
            deleted,
            version: c.version,
            base_commit_id: None,
            base_json: None,
            settled: &[],
            conflict: None,
        };
        DB::insert(tx, d, row(old, true)).await?;
        DB::entry(tx, c.id, d, c.key_id).await?;
        if held.contains(&moved_at(&c)) {
            continue;
        }
        let new = At {
            file_path: to,
            path: &c.path,
            key: &c.key,
        };
        let (id, _) = DB::insert(tx, d, row(new, false)).await?;
        if let Some(format) = sync.formats().for_path(&c.path) {
            let record = Record {
                id: c.key_id,
                worktree_id: w,
                file_path: to.to_string(),
                path: c.path.clone(),
                key: c.key.clone(),
                commit_id: c.commit_id.clone(),
                json: c.json.clone(),
                deleted: false,
                version: c.version,
                conflict: None,
            };
            DB::replace_aliases(tx, id, &format.find_alias(&record)).await?;
        }
    }
    if let Some(f) = DB::files(tx, w, Some(from)).await?.pop() {
        DB::upsert_file(
            tx,
            w,
            &FileRow {
                path: to,
                format: &f.format,
                commit_id: Some(None),
                source_oid: Some(f.source_oid.as_deref()),
                committed_oid: None,
            },
        )
        .await?;
        DB::set_file_deleted(tx, w, from, true).await?;
    }
    Ok(())
}

/// One scan's changes to the database, in one transaction: the committed
/// side up to `head`, then uncommitted renames, then the draft side of
/// every file that changed on disk or in HEAD, then the re-link (§4.3).
pub(crate) async fn scan_in_pool<DB: Segments>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    head: Option<&str>,
    head_files: &[HeadFile],
    renames: &[(String, String)],
    disk_files: &[DiskFile<'_>],
    stats: &mut SyncOutcome,
) -> Result<()> {
    let w = sync.worktree_id();
    let mut tx = pool.begin().await?;
    if let Some(head) = head {
        advance_head(&mut tx, sync, head, head_files, stats).await?;
        for f in head_files {
            DB::upsert_file(
                &mut tx,
                w,
                &FileRow {
                    path: &f.rel_path,
                    format: &f.format,
                    commit_id: None,
                    source_oid: None,
                    committed_oid: Some(f.blob.as_deref()),
                },
            )
            .await?;
        }
        DB::set_head_commit(&mut tx, w, head).await?;
    }
    for (from, to) in renames {
        rename_file(&mut tx, sync, from, to).await?;
    }
    for disk in disk_files {
        reconcile_file(&mut tx, sync, disk, stats).await?;
    }
    segments::relink(&mut tx, w).await?;
    tx.commit().await?;
    Ok(())
}

/// What a commit carried, for [`commit_in_pool`].
pub(crate) struct Carried<'a> {
    pub(crate) commit: &'a str,
    /// The files it was made for.
    pub(crate) files: &'a BTreeSet<String>,
    /// Those it deletes.
    pub(crate) removed: &'a [String],
    /// The family's next version before the save: rows from here on were
    /// written after it, and stay in the draft.
    pub(crate) watermark: i64,
    /// The files whose blob at `commit` the head doesn't hold yet.
    pub(crate) head_files: &'a [HeadFile],
}

/// After a commit carried files, in one transaction (§4.4,
/// C.12): fold the draft rows it carries into the head, then bring the
/// head up to what the commit holds, which takes the file's value where
/// a conflict held an edit back.
pub(crate) async fn commit_in_pool<DB: Segments>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    carried: &Carried<'_>,
) -> Result<()> {
    let &Carried {
        commit,
        files,
        removed,
        watermark,
        head_files,
    } = carried;
    let w = sync.worktree_id();
    let mut stats = SyncOutcome::default();
    let mut tx = pool.begin().await?;
    segments::fold(&mut tx, w, files, commit, watermark).await?;
    advance_head(&mut tx, sync, commit, head_files, &mut stats).await?;
    for f in head_files {
        DB::upsert_file(
            &mut tx,
            w,
            &FileRow {
                path: &f.rel_path,
                format: &f.format,
                commit_id: None,
                source_oid: None,
                committed_oid: Some(f.blob.as_deref()),
            },
        )
        .await?;
    }
    for path in files {
        if removed.contains(path) {
            DB::delete_file(&mut tx, w, path).await?;
            continue;
        }
        if let Some(f) = DB::files(&mut tx, w, Some(path)).await?.pop() {
            DB::upsert_file(
                &mut tx,
                w,
                &FileRow {
                    path,
                    format: &f.format,
                    commit_id: Some(Some(commit)),
                    source_oid: Some(f.source_oid.as_deref()),
                    committed_oid: None,
                },
            )
            .await?;
        }
    }
    DB::set_head_commit(&mut tx, w, commit).await?;
    DB::stamp_txns(&mut tx, w, commit).await?;
    segments::relink(&mut tx, w).await?;
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
