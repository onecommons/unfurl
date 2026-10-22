// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Rebasing a worktree's local commits onto upstream in the database
//! (docs/branch-segments.md §4.15, C.21).

use std::collections::{BTreeMap, BTreeSet};

use crate::commit::CommitId;
use crate::db::store::{Filter, Scope, Store};
use crate::db::tables::{FileRow, NewRecordRow, Place, RecordRow};
use crate::db::{self, on_pool};
use crate::document::{extract_ext, Syntax};
use crate::error::Result;
use crate::git;
use crate::ids::History;
use crate::model::{Rebased, TxnRecord};
use crate::rollup::parse_commit_rollup;
use crate::scan::HeadFile;
use crate::sync::SyncedRepo;

/// One place the local commits changed, unfolded into an edit.
pub(crate) struct Unfolded {
    pub(crate) place: Place,
    /// The value at the merge base, which the edit is based on.
    pub(crate) was: Option<serde_json::Value>,
    /// The value at HEAD; `None` for a deletion.
    pub(crate) now: Option<serde_json::Value>,
    pub(crate) key_id: Option<i64>,
    pub(crate) version: Option<i64>,
}

/// Where an unfolded edit's id and version come from.
pub(crate) struct Ids {
    /// This family's rollup lines between the base and HEAD.
    named: BTreeMap<Place, TxnRecord>,
    /// The rows the chain holds at HEAD, tombstones included.
    chain: BTreeMap<Place, RecordRow>,
}

impl Ids {
    /// The row the chain holds at HEAD, else the last rollup's line, as
    /// for a deletion.
    fn of(&self, place: &Place) -> (Option<i64>, Option<i64>) {
        let (line, row) = (self.named.get(place), self.chain.get(place));
        (
            row.map(|r| r.key_id).or(line.and_then(|t| t.key_id)),
            row.map(|r| r.version).or(line.map(|t| t.version)),
        )
    }
}

/// What the transaction takes.
struct Rebase<'a> {
    /// HEAD: the last of the local commits, which the database holds.
    head: CommitId,
    /// The merge base of `head` and `upstream`, which the edits are based at.
    base: CommitId,
    /// The fetched commit the head is rebuilt onto.
    upstream: CommitId,
    /// The local commits: those `head` has that `base` doesn't.
    local: &'a [CommitId],
    edits: &'a [Unfolded],
    /// Every file of `upstream`'s tree.
    files: &'a [HeadFile],
}

impl SyncedRepo {
    /// Rebase this worktree's local commits onto `upstream`, a commit the
    /// repository has, in the database: their changes become draft edits
    /// based at the merge base, and the head is rebuilt onto `upstream`.
    /// The caller then resets the working tree to `upstream`, and scans
    /// and commits, which classifies each edit against it, with nothing
    /// scanning the checkout in between: a scan before the reset rebuilds
    /// back onto HEAD, leaving the edits as no-ops the next commit folds.
    ///
    /// The caller resets only while HEAD is still the commit
    /// [`Rebased::Unfolded`] names: a commit made since wasn't unfolded.
    ///
    /// Refused, with nothing changed, when HEAD isn't the commit the
    /// database holds, the working tree isn't HEAD's, the worktree has
    /// conflicts, or the local commits change anything but records, or
    /// move one.
    ///
    /// # Errors
    ///
    /// A failure reading git or the database.
    pub async fn rebase_onto(&self, upstream: CommitId) -> Result<Rebased> {
        let repo = self.repo()?;
        let worktree = db::worktree::get(self.db(), self.worktree_id()).await?;
        let checked_out = git::worktree_meta(&repo)?.head_oid;
        let Some(head) = checked_out.filter(|h| worktree.commit_id == Some(*h)) else {
            return Ok(refused("HEAD isn't the commit the database holds"));
        };
        let Some(base) = git::merge_base(&repo, head, upstream)? else {
            return Ok(refused("HEAD and upstream share no history"));
        };
        if base == upstream {
            return Ok(Rebased::Ahead);
        }
        if base == head {
            return Ok(Rebased::FastForward);
        }
        if let Some(why) = dirty(&repo, head)? {
            return Ok(refused(&why));
        }
        let mut history = History::open(self.formats(), repo, self.db(), self.family_id()).await?;
        let local = git::commits_between(history.repo(), base, head)?;
        let edits = match self.unfold(&mut history, base, head, &local).await? {
            Ok(edits) => edits,
            Err(why) => return Ok(refused(&why)),
        };
        let files = self
            .head_files(history.repo(), Some(upstream), &Default::default(), false)?
            .files;
        let rebase = Rebase {
            head,
            base,
            upstream,
            local: &local,
            edits: &edits,
            files: &files,
        };
        let refusal =
            on_pool!(self.db(), pool => rebase_in_pool(self, pool, &mut history, &rebase).await)?;
        if let Some(why) = refusal {
            return Ok(refused(why));
        }
        // the local commits are referenced by nothing once the caller resets
        let kept = format!("refs/git-sync/rebased/{}", worktree.branch);
        git::set_ref(history.repo(), &kept, head)?;
        Ok(Rebased::Unfolded {
            base,
            head,
            edits: edits.len(),
        })
    }

    /// The edits the `local` commits made between `base` and `head`, or why
    /// they can't be unfolded.
    async fn unfold(
        &self,
        history: &mut History<'_>,
        base: CommitId,
        head: CommitId,
        local: &[CommitId],
    ) -> Result<std::result::Result<Vec<Unfolded>, String>> {
        let ids = self.ids(named(history, local)).await?;
        let (at_base, at_head) = (
            git::tree_entries(history.repo(), base)?,
            git::tree_entries(history.repo(), head)?,
        );
        let mut edits = Vec::new();
        let paths: BTreeSet<&String> = at_base.keys().chain(at_head.keys()).collect();
        for file in paths {
            if at_base.get(file) == at_head.get(file) {
                continue;
            }
            let (Some(was), Some(now)) = (at_base.get(file), at_head.get(file)) else {
                return Ok(Err(format!("{file} was added or removed")));
            };
            if !was.0.is_blob() || was.0 != now.0 {
                return Ok(Err(format!("{file}'s mode changed, or it's a submodule")));
            }
            match self.file_edits(history, base, head, file, &ids)? {
                Ok(ours) => edits.extend(ours),
                Err(why) => return Ok(Err(why)),
            }
        }
        if moved(&edits.iter().collect::<Vec<_>>()) {
            return Ok(Err("the local commits moved a record".into()));
        }
        Ok(Ok(edits))
    }

    /// Where the edits of the `local` commits take their ids and versions:
    /// `named` by their rollups, and the chain's rows.
    pub(crate) async fn ids(&self, named: BTreeMap<Place, TxnRecord>) -> Result<Ids> {
        // tombstones too: one names the record a deletion took, where no
        // rollup does and a merge has no rebuild to name it
        let chain: BTreeMap<Place, RecordRow> =
            crate::fork::chain_rows(self.db(), self.worktree_id())
                .await?
                .into_iter()
                .map(|r| (Place::from(&r), r))
                .collect();
        Ok(Ids { named, chain })
    }

    /// The edits the local commits made to `file`, which is at both `base`
    /// and `head`, or why they can't be unfolded.
    fn file_edits(
        &self,
        history: &mut History<'_>,
        base: CommitId,
        head: CommitId,
        file: &str,
        ids: &Ids,
    ) -> Result<std::result::Result<Vec<Unfolded>, String>> {
        let ours = match self.record_edits(history, base, head, file, ids)? {
            Ok(ours) => ours,
            Err(why) => return Ok(Err(why)),
        };
        Ok(
            match self.unrepresented(history, base, head, file, &ours)? {
                Some(why) => Err(why),
                None => Ok(ours),
            },
        )
    }

    /// The records `file` changed in, from `base` to `head`, as edits; why
    /// not, where it isn't a document a format reads at both, or a record
    /// validation rejects changed.
    pub(crate) fn record_edits(
        &self,
        history: &mut History<'_>,
        base: CommitId,
        head: CommitId,
        file: &str,
        ids: &Ids,
    ) -> Result<std::result::Result<Vec<Unfolded>, String>> {
        if !history.parses(base, file)? || !history.parses(head, file)? {
            return Ok(Err(format!("{file} isn't a document a format reads")));
        }
        let (was, now) = (history.records(base, file)?, history.records(head, file)?);
        let mut ours = Vec::new();
        for at in was.keys().chain(now.keys()).collect::<BTreeSet<_>>() {
            if was.get(at) == now.get(at) {
                continue;
            }
            if history.skips(base, file, &at.0, &at.1)?
                || history.skips(head, file, &at.0, &at.1)?
            {
                return Ok(Err(format!("{file}: a record validation rejects changed")));
            }
            let place = Place::new(file, &at.0, &at.1);
            let (key_id, version) = ids.of(&place);
            ours.push(Unfolded {
                was: was.get(at).cloned(),
                now: now.get(at).cloned(),
                key_id,
                version,
                place,
            });
        }
        Ok(Ok(ours))
    }

    /// Why `file` at `head` isn't `base`'s with `edits` applied, if it isn't:
    /// its value, or a literate document's bytes, whose prose its value
    /// doesn't hold. Layout, comments, key order and empty maps aren't
    /// compared: the commit onto upstream renders the edits onto
    /// upstream's bytes.
    pub(crate) fn unrepresented(
        &self,
        history: &History<'_>,
        base: CommitId,
        head: CommitId,
        file: &str,
        edits: &[Unfolded],
    ) -> Result<Option<String>> {
        let repo = history.repo();
        let source = git::read_blob_at_commit(repo, base, file)?;
        let target = git::read_blob_at_commit(repo, head, file)?.unwrap_or_default();
        let pending = edits
            .iter()
            .map(|e| self.edit_row(e, base, 0).into_record(self.worktree_id()))
            .collect();
        let Ok(rendered) = self.render_onto(file, source.as_deref(), pending) else {
            return Ok(Some(format!("{file} can't be rendered")));
        };
        let rendered = rendered.or(source).unwrap_or_default();
        let same = match Syntax::for_extension(&extract_ext(file)) {
            Some(Syntax::Markdown) | None => rendered == target,
            Some(syntax) => {
                let value = |bytes: &[u8]| syntax.into_value(file, bytes).ok().map(pruned);
                value(&rendered) == value(&target)
            }
        };
        Ok((!same).then(|| format!("{file} has changes that aren't its records'")))
    }

    /// `e` as a draft row based at commit `base`, with `version` where it
    /// has none.
    pub(crate) fn edit_row(&self, e: &Unfolded, base: CommitId, version: i64) -> RecordRow {
        RecordRow {
            id: 0,
            key_id: e.key_id.unwrap_or(0),
            segment_id: 0,
            file_path: e.place.file_path.clone(),
            path: e.place.path.clone(),
            key: e.place.key.clone(),
            commit_id: None,
            // a tombstone's json is the value it deletes
            json: e.now.clone().or(e.was.clone()).unwrap_or_default(),
            deleted: e.now.is_none(),
            version: e.version.unwrap_or(version),
            // a create has no base
            base_commit_id: e.was.as_ref().map(|_| base),
            base_json: e.was.clone(),
            settled: Vec::new(),
            conflict: None,
        }
    }
}

/// `v` without its empty maps, which hold nothing: a section whose last
/// record went is one writer's empty map and another's nothing.
fn pruned(v: serde_json::Value) -> serde_json::Value {
    match v {
        serde_json::Value::Object(map) => serde_json::Value::Object(
            map.into_iter()
                .map(|(k, v)| (k, pruned(v)))
                .filter(|(_, v)| !v.as_object().is_some_and(|m| m.is_empty()))
                .collect(),
        ),
        v => v,
    }
}

/// Whether `edits` may move a record: one id at two places, the record
/// still at one of them, or a record gone from one file where the same
/// section and key of another changed, as a scan pairs them (§3.5) -- its
/// id may be gone with the tombstone a fold purged. The draft holds one
/// edit per record.
pub(crate) fn moved(edits: &[&Unfolded]) -> bool {
    // id: (places, whether the record is still at one); two deletions
    // share a deletion's provisional id, the last record a place lost (C.21)
    let mut ids: BTreeMap<i64, (usize, bool)> = BTreeMap::new();
    for e in edits {
        if let Some(id) = e.key_id {
            let (places, kept) = ids.entry(id).or_default();
            *places += 1;
            *kept |= e.now.is_some();
        }
    }
    let gone = edits.iter().filter(|e| e.now.is_none());
    ids.values().any(|&(places, kept)| places > 1 && kept)
        || gone.into_iter().any(|g| {
            edits.iter().any(|e| {
                e.now.is_some()
                    && e.place.file_path != g.place.file_path
                    && (&e.place.path, &e.place.key) == (&g.place.path, &g.place.key)
            })
        })
}

fn refused(why: &str) -> Rebased {
    Rebased::Refused(why.to_string())
}

/// Why the working tree isn't commit `head`'s tree, if it isn't: a file of
/// the tree or the index whose content isn't the tree's.
pub(crate) fn dirty(repo: &gix::Repository, head: CommitId) -> Result<Option<String>> {
    let tree = git::tree_blobs(repo, head)?;
    let root = repo
        .workdir()
        .ok_or_else(|| crate::error::Error::Git("no working tree".into()))?;
    let indexed: Vec<String> = git::tracked_files(repo)?
        .into_iter()
        .map(|f| f.rel_path)
        .collect();
    let paths: BTreeSet<&String> = tree.keys().chain(&indexed).collect();
    for path in paths {
        let on_disk = match std::fs::read(root.join(path)) {
            Ok(bytes) => Some(git::blob_oid_for_bytes(repo, &bytes)?),
            Err(_) => None,
        };
        if on_disk != tree.get(path).copied() {
            return Ok(Some(format!("{path} isn't HEAD's")));
        }
    }
    Ok(None)
}

/// The record lines this family's rollups in `local` hold, the newest
/// write of each place.
pub(crate) fn named(history: &History<'_>, local: &[CommitId]) -> BTreeMap<Place, TxnRecord> {
    let mut named: BTreeMap<Place, TxnRecord> = BTreeMap::new();
    for c in local {
        let Some(rollup) = git::commit_message(history.repo(), *c)
            .and_then(|m| parse_commit_rollup(&m).ok().flatten())
            .filter(|r| history.owns(r))
        else {
            continue;
        };
        for line in rollup
            .txns
            .iter()
            .flat_map(|t| &t.records)
            .chain(&rollup.records)
        {
            let Some(file) = line.file_path.as_deref() else {
                continue;
            };
            let place = Place::new(file, &line.path, &line.key);
            if named.get(&place).is_none_or(|t| t.version < line.version) {
                named.insert(place, line.clone());
            }
        }
    }
    named
}

/// `e` into draft `d`, based at commit `base`, as record `key_id`, or a
/// new one.
pub(crate) async fn insert_edit<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    sync: &SyncedRepo,
    d: i64,
    e: &Unfolded,
    base: CommitId,
    key_id: Option<i64>,
) -> Result<()> {
    let version = match e.version {
        Some(v) => v,
        None => DB::next_version(tx, sync.family_id(), 1).await?,
    };
    let row = sync.edit_row(e, base, version);
    DB::insert(
        tx,
        d,
        NewRecordRow {
            key_id,
            ..NewRecordRow::from(&row)
        },
    )
    .await?;
    Ok(())
}

/// C.21's transaction: why it changed nothing, if it didn't.
async fn rebase_in_pool<DB: Store>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    history: &mut History<'_>,
    r: &Rebase<'_>,
) -> Result<Option<&'static str>> {
    let (w, family) = (sync.worktree_id(), sync.family_id());
    let mut tx = pool.begin().await?;
    DB::lock_family(&mut tx, family).await?;
    if DB::head_commit(&mut tx, w).await? != Some(r.head) {
        return Ok(Some("HEAD moved in the database"));
    }
    // a commit since the edits were collected wouldn't be unfolded
    if git::worktree_meta(history.repo())?.head_oid != Some(r.head) {
        return Ok(Some("HEAD moved"));
    }
    // phase 3: a user branch's edits over the versions this copies need the
    // entries a merge's old versions do (§4.9); none exist yet
    let d = DB::segs(&mut tx, w).await?.draft;
    // their file side is HEAD's, which the rebuild leaves behind
    if !DB::rows_in(&mut tx, d, None, true).await?.is_empty() {
        return Ok(Some("the worktree has conflicts to resolve first"));
    }
    let pending: BTreeMap<Place, RecordRow> = DB::rows_in(&mut tx, d, None, false)
        .await?
        .into_iter()
        .filter(|row| row.is_edit())
        .map(|row| (Place::from(&row), row))
        .collect();
    let elsewhere = r.edits.iter().any(|e| {
        !pending.contains_key(&e.place) && pending.values().any(|p| Some(p.key_id) == e.key_id)
    });
    if elsewhere {
        return Ok(Some(
            "a record the local commits changed has an edit pending elsewhere",
        ));
    }
    let tombstones = unfold_edits(&mut tx, sync, d, r, &pending).await?;
    for c in r.local {
        DB::unstamp_txns(&mut tx, w, *c).await?;
    }
    crate::fork::onto(&mut tx, history, family, w, r.upstream, r.files).await?;
    // the transaction rolls back as it drops
    if let Some(why) = unfold_tombstones(&mut tx, sync, d, r, &tombstones).await? {
        return Ok(Some(why));
    }
    reread(&mut tx, w, r).await?;
    crate::segments::relink(&mut tx, w).await?;
    DB::compact(&mut tx, family).await?;
    tx.commit().await?;
    Ok(None)
}

/// The unfolded edits but the tombstones into draft `d`, a pending edit at
/// a place taking the base instead: the tombstones, for after the rebuild.
async fn unfold_edits<'r, DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    sync: &SyncedRepo,
    d: i64,
    r: &Rebase<'r>,
    pending: &BTreeMap<Place, RecordRow>,
) -> Result<Vec<&'r Unfolded>> {
    let mut tombstones = Vec::new();
    for e in r.edits {
        if let Some(row) = pending.get(&e.place) {
            // a create has no base
            let based_at = e.was.as_ref().map(|_| r.base);
            DB::rebase_edit(tx, row.id, based_at, e.was.as_ref()).await?;
        } else if e.now.is_none() {
            tombstones.push(e);
        } else {
            insert_edit(tx, sync, d, e, r.base, e.key_id).await?;
        }
    }
    Ok(tombstones)
}

/// Each tombstone into draft `d`, as a deletion of the record the rebuilt
/// chain shows at its place; why not, when that's a record another edit
/// changes, which the local commits moved.
async fn unfold_tombstones<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    sync: &SyncedRepo,
    d: i64,
    r: &Rebase<'_>,
    tombstones: &[&Unfolded],
) -> Result<Option<&'static str>> {
    for e in tombstones {
        let shown = DB::visible(
            tx,
            sync.worktree_id(),
            Scope::Chain,
            Filter::At(e.place.as_at()),
        )
        .await?
        .into_iter()
        .find(|row| !row.deleted)
        .map(|row| row.key_id);
        if shown.is_some() && r.edits.iter().any(|o| o.now.is_some() && o.key_id == shown) {
            return Ok(Some("the local commits moved a record"));
        }
        insert_edit(tx, sync, d, e, r.base, shown.or(e.key_id)).await?;
    }
    Ok(None)
}

/// The files the edits are in are read from the working tree again at the
/// next scan, which classifies the edits against upstream.
async fn reread<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    r: &Rebase<'_>,
) -> Result<()> {
    let files: BTreeSet<&String> = r.edits.iter().map(|e| &e.place.file_path).collect();
    for f in DB::files(tx, w, None).await? {
        if files.contains(&f.path) {
            let file = FileRow {
                path: &f.path,
                format: &f.format,
                commit_id: None,
                source_oid: Some(None),
                committed_oid: None,
            };
            DB::upsert_file(tx, w, &file).await?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn edit(file: &str, key: &str, now: bool, key_id: i64) -> Unfolded {
        Unfolded {
            place: Place::new(file, "/r", key),
            was: Some(serde_json::json!({"v": 1})),
            now: now.then(|| serde_json::json!({"v": 2})),
            key_id: Some(key_id),
            version: None,
        }
    }

    #[test]
    fn one_id_at_two_places_with_the_record_at_one_is_a_move() {
        let (from, to) = (
            edit("a.json", "k0", false, 7),
            edit("a.json", "k1", true, 7),
        );
        assert!(moved(&[&from, &to]));
    }

    #[test]
    fn two_deletions_sharing_an_id_move_nothing() {
        let (one, two) = (
            edit("a.json", "k0", false, 7),
            edit("b.json", "k1", false, 7),
        );
        assert!(!moved(&[&one, &two]));
    }
}
