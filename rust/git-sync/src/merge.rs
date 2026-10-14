// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Merging upstream into a worktree's local commits, record by record
//! where git can't (docs/branch-segments.md §4.15, C.22).

use std::collections::{BTreeMap, BTreeSet};

use crate::commit::CommitId;
use crate::db::store::Store;
use crate::db::tables::{Place, RecordRow};
use crate::db::{self, on_pool};
use crate::document::{extract_ext, Syntax};
use crate::error::Result;
use crate::git;
use crate::ids::History;
use crate::model::{HeldRecord, MergedUpstream};
use crate::rebase::{dirty, insert_edit, moved, Unfolded};
use crate::sync::SyncedRepo;

/// What a merge knows of the three sides.
struct Sides {
    head: CommitId,
    base: CommitId,
    upstream: CommitId,
    /// The tree git merged them into.
    merged: gix::ObjectId,
}

/// What the merge commits, and the edits it holds.
struct Plan {
    /// The files built from records, over the merged tree.
    built: Vec<(String, Option<Vec<u8>>)>,
    held: Vec<Unfolded>,
}

impl SyncedRepo {
    /// Merge `upstream`, a commit the repository has, into this worktree's
    /// HEAD, record by record where git can't. `merged` is the tree
    /// `git merge-tree --write-tree` made of the two, and `conflicted` the
    /// files it couldn't merge. Files git merged that both sides changed
    /// must parse, without a duplicate key, and keep every record both
    /// changed valid; those that don't, and those git couldn't merge, are
    /// built from upstream's bytes with this side's record changes applied.
    /// A record both sides changed to different values keeps upstream's
    /// there, and this side's is held as a pending edit, which the scan
    /// after the caller's fast-forward finds in conflict.
    ///
    /// The merge commit is made, but no ref names it: the caller
    /// fast-forwards to it while HEAD is still the commit it merged, then
    /// pushes and scans. Refused, with nothing changed, as
    /// [`Self::rebase_onto`] refuses a file that has to be built from
    /// records, where the worktree has conflicts, or where HEAD isn't the
    /// commit the database holds.
    ///
    /// # Errors
    ///
    /// A failure reading git or the database.
    pub async fn merge_upstream(
        &self,
        upstream: CommitId,
        merged: gix::ObjectId,
        conflicted: &[String],
    ) -> Result<MergedUpstream> {
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
            return Ok(MergedUpstream::Ahead);
        }
        if base == head {
            return Ok(MergedUpstream::FastForward);
        }
        if let Some(why) = dirty(&repo, head)? {
            return Ok(refused(&why));
        }
        let mut history = History::open(self.formats(), repo, self.db(), self.family_id()).await?;
        let sides = Sides {
            head,
            base,
            upstream,
            merged,
        };
        let plan = match self.plan(&mut history, &sides, conflicted).await? {
            Ok(plan) => plan,
            Err(why) => return Ok(refused(&why)),
        };
        // written first, so a failure leaves nothing held: no ref names it
        let message = format!("Merge {upstream} into {}", worktree.branch);
        let commit = git::commit_tree_with(
            history.repo(),
            merged,
            &plan.built,
            &[head, upstream],
            &message,
        )?;
        let refusal = on_pool!(self.db(), pool => hold_in_pool(self, pool, &sides, &plan).await)?;
        if let Some(why) = refusal {
            return Ok(refused(why));
        }
        let held = plan
            .held
            .iter()
            .map(|e| HeldRecord {
                file_path: e.place.file_path.clone(),
                path: e.place.path.clone(),
                key: e.place.key.clone(),
            })
            .collect();
        Ok(MergedUpstream::Merged { commit, head, held })
    }

    /// What the merge commits and holds, or why it can't be made.
    async fn plan(
        &self,
        history: &mut History<'_>,
        sides: &Sides,
        conflicted: &[String],
    ) -> Result<std::result::Result<Plan, String>> {
        let local = git::commits_between(history.repo(), sides.base, sides.head)?;
        let ids = self.ids(crate::rebase::named(history, &local)).await?;
        let repo = history.repo();
        let (at_base, at_head, at_upstream) = (
            git::tree_entries(repo, sides.base)?,
            git::tree_entries(repo, sides.head)?,
            git::tree_entries(repo, sides.upstream)?,
        );
        let changed =
            |at: &std::collections::HashMap<_, _>, f: &String| at_base.get(f) != at.get(f);
        let ours: BTreeSet<String> = at_base
            .keys()
            .chain(at_head.keys())
            .filter(|f| changed(&at_head, f))
            .cloned()
            .collect();
        // this side's records, in every file it changed, or why they can't
        // be read: a move is judged over all of them
        let mut edits: BTreeMap<String, std::result::Result<Vec<Unfolded>, String>> =
            BTreeMap::new();
        for file in &ours {
            let e = self.record_edits(history, sides.base, sides.head, file, &ids)?;
            edits.insert(file.clone(), e);
        }
        let read: Vec<&Unfolded> = edits.values().flatten().flatten().collect();
        if moved(&read) {
            return Ok(Err("the local commits moved a record".into()));
        }
        let mut rebuild: BTreeSet<String> = conflicted.iter().cloned().collect();
        for file in ours.iter().filter(|f| changed(&at_upstream, f)) {
            if !rebuild.contains(file) && !self.merged_ok(history, sides, file)? {
                rebuild.insert(file.clone());
            }
        }
        let mut plan = Plan {
            built: Vec::new(),
            held: Vec::new(),
        };
        for file in &rebuild {
            if let Err(why) = self.build(history, sides, file, edits.remove(file), &mut plan)? {
                return Ok(Err(why));
            }
        }
        Ok(Ok(plan))
    }

    /// Whether git's merge of `file`, which both sides changed, parses
    /// without a duplicate key, has each record only one side changed as
    /// that side has it, and keeps each record both changed valid.
    fn merged_ok(&self, history: &mut History<'_>, sides: &Sides, file: &str) -> Result<bool> {
        let entries = git::tree_entries_of(history.repo(), sides.merged)?;
        let Some(&(_, blob)) = entries.get(file) else {
            // the merge removed it
            return Ok(true);
        };
        if Syntax::for_extension(&extract_ext(file)).is_none() {
            return Ok(true);
        }
        let bytes = git::read_blob(history.repo(), &blob.to_string())?;
        if !duplicate_free(file, &bytes) {
            return Ok(false);
        }
        // a document a format reads, whatever records changed
        if !history.blob_takes(file, blob, "", "")?.0 {
            return Ok(false);
        }
        let was = history.records(sides.base, file)?;
        let ours = history.records(sides.head, file)?;
        let theirs = history.records(sides.upstream, file)?;
        let merged = history.blob_records(file, blob)?;
        let places: BTreeSet<_> = [&was, &ours, &theirs, &merged]
            .into_iter()
            .flat_map(|r| r.keys().cloned())
            .collect();
        for at in places {
            let (b, l, u, m) = (
                was.get(&at),
                ours.get(&at),
                theirs.get(&at),
                merged.get(&at),
            );
            let want = match () {
                _ if l == b || l == u => u,
                _ if u == b => l,
                // both changed it: git's merge of the two stands, if valid
                _ => {
                    if !history.blob_takes(file, blob, &at.0, &at.1)?.1 {
                        return Ok(false);
                    }
                    continue;
                }
            };
            if m != want {
                return Ok(false);
            }
        }
        Ok(true)
    }

    /// `file` built from records into `plan`: upstream's bytes with the
    /// records only this side changed applied, and each both changed to
    /// different values held. Why it can't be, if it can't.
    fn build(
        &self,
        history: &mut History<'_>,
        sides: &Sides,
        file: &str,
        edits: Option<std::result::Result<Vec<Unfolded>, String>>,
        plan: &mut Plan,
    ) -> Result<std::result::Result<(), String>> {
        let repo = history.repo();
        let (base, upstream) = (sides.base, sides.upstream);
        let entry = |c| git::tree_entries(repo, c).map(|t| t.get(file).copied());
        let (at_base, at_head, at_upstream) = (entry(base)?, entry(sides.head)?, entry(upstream)?);
        let (Some(b), Some(h), Some(u)) = (at_base, at_head, at_upstream) else {
            return Ok(Err(format!("{file} was added or removed")));
        };
        if !b.0.is_blob() || b.0 != h.0 || h.0 != u.0 {
            return Ok(Err(format!("{file}'s mode changed, or it's a submodule")));
        }
        let edits = match edits {
            Some(Ok(edits)) => edits,
            Some(Err(why)) => return Ok(Err(why)),
            // a file only upstream changed, which git can't have conflicted
            None => return Ok(Err(format!("{file} has no changes of this side's"))),
        };
        if !history.parses(upstream, file)? {
            return Ok(Err(format!(
                "{file} isn't a document a format reads upstream"
            )));
        }
        if let Some(why) = self.unrepresented(history, base, sides.head, file, &edits)? {
            return Ok(Err(why));
        }
        let theirs = history.records(upstream, file)?;
        let mut apply = Vec::new();
        for e in edits {
            let now = theirs.get(&(e.place.path.clone(), e.place.key.clone()));
            if now == e.was.as_ref() {
                apply.push(e);
            } else if now != e.now.as_ref() {
                plan.held.push(e);
            }
        }
        let source = git::read_blob_at_commit(history.repo(), upstream, file)?;
        let pending = apply
            .iter()
            .map(|e| self.edit_row(e, base, 0).into_record(self.worktree_id()))
            .collect();
        let Ok(rendered) = self.render_onto(file, source.as_deref(), pending) else {
            return Ok(Err(format!("{file} can't be rendered")));
        };
        plan.built.push((
            file.to_string(),
            Some(rendered.or(source).unwrap_or_default()),
        ));
        Ok(Ok(()))
    }
}

fn refused(why: &str) -> MergedUpstream {
    MergedUpstream::Refused(why.to_string())
}

/// Whether `bytes` of a JSON `file` hold no object with a key twice:
/// JSON's parser keeps the last, so it's read as the YAML it also is,
/// whose parser refuses one. Any other syntax's parse refuses one itself.
fn duplicate_free(file: &str, bytes: &[u8]) -> bool {
    match Syntax::for_extension(&extract_ext(file)) {
        Some(Syntax::Json) => std::str::from_utf8(bytes)
            .ok()
            .is_some_and(|text| serde_saphyr::from_str::<serde_json::Value>(text).is_ok()),
        _ => true,
    }
}

/// C.22 step 4: hold the records both sides changed; why it changed
/// nothing, if it didn't.
async fn hold_in_pool<DB: Store>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    sides: &Sides,
    plan: &Plan,
) -> Result<Option<&'static str>> {
    let (w, family) = (sync.worktree_id(), sync.family_id());
    let mut tx = pool.begin().await?;
    DB::lock_family(&mut tx, family).await?;
    if DB::head_commit(&mut tx, w).await? != Some(sides.head) {
        return Ok(Some("HEAD moved in the database"));
    }
    if git::worktree_meta(&sync.repo()?)?.head_oid != Some(sides.head) {
        return Ok(Some("HEAD moved"));
    }
    let d = DB::segs(&mut tx, w).await?.draft;
    let held: BTreeSet<&Place> = plan.held.iter().map(|e| &e.place).collect();
    let conflicted = DB::rows_in(&mut tx, d, None, true).await?;
    if conflicted
        .iter()
        .any(|row| held.contains(&Place::from(row)))
    {
        return Ok(Some(
            "a record both sides changed has a conflict to resolve first",
        ));
    }
    let pending: BTreeMap<Place, RecordRow> = DB::rows_in(&mut tx, d, None, false)
        .await?
        .into_iter()
        .filter(|row| row.is_edit())
        .map(|row| (Place::from(&row), row))
        .collect();
    let elsewhere = plan.held.iter().any(|e| {
        !pending.contains_key(&e.place) && pending.values().any(|p| Some(p.key_id) == e.key_id)
    });
    if elsewhere {
        return Ok(Some(
            "a record both sides changed has an edit pending elsewhere",
        ));
    }
    // an edit pending at the place is based on this side's value, so it's
    // in conflict with upstream's already; moving its base would make it
    // conflict with this side's own commit should the merge stop here
    for e in plan.held.iter().filter(|e| !pending.contains_key(&e.place)) {
        insert_edit(&mut tx, sync, d, e, sides.base, e.key_id).await?;
    }
    crate::segments::relink(&mut tx, w).await?;
    tx.commit().await?;
    Ok(None)
}
