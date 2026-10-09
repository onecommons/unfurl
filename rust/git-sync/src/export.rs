//! Handing a worktree's conflicts to git: the edits under an unresolved
//! conflict move to a new branch, committed onto their base, and the
//! worktree takes the file's values (docs/branch-segments.md §4.15, C.20).

use std::collections::{BTreeMap, BTreeSet};

use crate::conflict::resolve_conflict_in_tx;
use crate::db::store::{Filter, Scope, Store};
use crate::db::tables::{FileRow, NewRecordRow, RecordRow};
use crate::db::{self, on_pool};
use crate::error::{Error, Result};
use crate::model::{CommitRollup, ConflictState, Exported, Record, Resolution};
use crate::rollup::{build_commit_message, parse_commit_rollup};
use crate::scan::Carried;
use crate::sync::WRITE_ATTEMPTS;
use crate::{git, segments, SyncedRepo};

/// One try at an export.
enum Attempt {
    /// Nothing is in conflict.
    Nothing,
    /// The edits are in branch `n`'s draft, forked at `base`.
    Moved(i64, gix::ObjectId),
    /// A write changed the conflicted edits between the read and the move.
    Raced,
}

/// The steps of an export after its edits move to the branch, where a
/// failure leaves it for the next call to finish.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExportStep {
    /// The edits are in the branch's draft, and its ref is at the base.
    Moved,
    /// The commit is written, unreachable, and the ref still at the base.
    Written,
    /// The draft is folded into the commit, and the ref still at the base.
    Folded,
}

impl SyncedRepo {
    /// Move this worktree's edits under an unresolved conflict to a new
    /// branch `branch`, with no checkout, forked where they were made and
    /// committed there; this worktree resolves each for the file. Merging
    /// the branch back is how they're resolved in git. `None` when nothing
    /// is in conflict. These are the conflicts the database holds: a scan
    /// finds new ones first, as [`Self::commit_repository`] runs one.
    ///
    /// An export that failed after its edits moved is finished by calling
    /// this again with the same `branch`. The branch is only in this
    /// repository until it's pushed: a new clone has neither it nor its
    /// commit.
    ///
    /// # Errors
    ///
    /// [`Error::BranchExists`] when the ref or a worktree of `branch`
    /// exists, other than this worktree's unfinished export;
    /// [`Error::Other`] when writes kept changing the conflicted edits. A
    /// file that can't be rendered at the base fails it with nothing
    /// changed.
    pub async fn export_conflicts(&self, branch: &str) -> Result<Option<Exported>> {
        self.export_racing(branch, async || Ok(()), |_| Ok(()))
            .await
    }

    /// [`Self::export_conflicts`], failing after step `at`.
    #[cfg(feature = "fault-injection")]
    pub async fn export_failing(&self, branch: &str, at: ExportStep) -> Result<Option<Exported>> {
        let fail = |step| match step == at {
            true => Err(Error::Other(format!("stopped after {step:?}"))),
            false => Ok(()),
        };
        self.export_racing(branch, async || Ok(()), fail).await
    }

    /// [`Self::export_conflicts`], running `before_move` between reading
    /// the conflicted edits and moving them, and `after` after each step
    /// from the move on: where another writer, or a failure, would come.
    pub(crate) async fn export_racing(
        &self,
        branch: &str,
        mut before_move: impl AsyncFnMut() -> Result<()>,
        mut after: impl FnMut(ExportStep) -> Result<()>,
    ) -> Result<Option<Exported>> {
        if let Some(exported) = self.resume_export(branch, &mut after).await? {
            return Ok(Some(exported));
        }
        for _ in 0..WRITE_ATTEMPTS {
            match self.try_export(branch, &mut before_move).await? {
                Attempt::Nothing => return Ok(None),
                Attempt::Moved(n, base) => {
                    after(ExportStep::Moved)?;
                    return self
                        .commit_export(branch, n, base, &mut after)
                        .await
                        .map(Some);
                }
                Attempt::Raced => {}
            }
        }
        Err(Error::Other(format!(
            "the conflicts kept changing while being exported to {branch}"
        )))
    }

    /// Finish this worktree's export of `branch` that failed after its
    /// edits moved: the branch's draft still holds them, or they're folded
    /// into a commit its ref isn't at yet. `None` when there's no worktree
    /// of `branch`.
    async fn resume_export(
        &self,
        branch: &str,
        after: &mut impl FnMut(ExportStep) -> Result<()>,
    ) -> Result<Option<Exported>> {
        let this = db::worktree::get(self.db(), self.worktree_id()).await?;
        let Some(n) = db::worktree::find(self.db(), &this.origin, branch).await? else {
            return Ok(None);
        };
        let exporting = db::worktree::get(self.db(), n).await?;
        // only an unfinished export from this worktree: anything else, this
        // one included, has edits and commits of its own
        let repo = self.repo()?;
        let tip = git::branch_tip(&repo, branch)?;
        let (true, Some(at), Some(tip)) = (
            exporting.exporting_from.as_ref() == Some(&this.branch),
            exporting.commit_id,
            tip,
        ) else {
            return Err(exists(branch));
        };
        let at = gix::ObjectId::from_hex(at.as_bytes()).map_err(|e| Error::Git(e.to_string()))?;
        let pending = !on_pool!(self.db(), pool => draft_edits_in_pool(pool, n).await)?.is_empty();
        match (pending, tip == at) {
            (true, true) => self.commit_export(branch, n, at, after).await.map(Some),
            (false, false) if git::parents(&repo, at)? == [tip] => {
                self.finish_export(branch, n, tip, at).await.map(Some)
            }
            _ => Err(exists(branch)),
        }
    }

    async fn try_export(
        &self,
        branch: &str,
        before_move: &mut impl AsyncFnMut() -> Result<()>,
    ) -> Result<Attempt> {
        let w = self.worktree_id();
        let edits = on_pool!(self.db(), pool => conflicted_in_pool(pool, w).await)?;
        let repo = self.repo()?;
        let head = git::worktree_meta(&repo)?.head_oid;
        let (false, Some(head)) = (edits.is_empty(), head) else {
            return Ok(Attempt::Nothing);
        };
        let bases: Vec<String> = edits
            .iter()
            .filter_map(|e| e.base_commit_id.clone())
            .collect();
        let base = git::export_base(&repo, head, &bases)?;
        // what the commit will hold: a file that can't be rendered fails
        // the export here, before anything changes
        self.render_at(&repo, base, edits.iter().cloned().map(|e| e.into_record(w)))?;
        let blobs = git::tree_blobs(&repo, &base.to_string())?;
        let files = self.files_at(&blobs).await?;
        let from = db::worktree::get(self.db(), w).await?;
        // claims the name before anything in the database changes
        git::create_branch(&repo, branch, base)?;
        let ids: Vec<i64> = edits.iter().map(|e| e.id).collect();
        let fork = Fork {
            origin: &from.origin,
            branch,
            from: &from.branch,
            base: &base.to_string(),
            files: &files,
        };
        let moved = match before_move().await {
            Ok(()) => self.move_conflicts(&fork, &ids).await,
            Err(e) => Err(e),
        };
        match moved {
            Ok(Some(n)) => Ok(Attempt::Moved(n, base)),
            Ok(None) => {
                git::delete_branch_if_at(&repo, branch, base)?;
                Ok(Attempt::Raced)
            }
            Err(e) => {
                git::delete_branch_if_at(&repo, branch, base)?;
                Err(e)
            }
        }
    }

    /// `records`, edits, rendered onto their files at `base`: each file
    /// they change there, with its new bytes.
    fn render_at(
        &self,
        repo: &gix::Repository,
        base: gix::ObjectId,
        records: impl IntoIterator<Item = Record>,
    ) -> Result<Vec<(String, Option<Vec<u8>>)>> {
        let mut by_file: BTreeMap<String, Vec<Record>> = BTreeMap::new();
        for r in records {
            by_file.entry(r.file_path.clone()).or_default().push(r);
        }
        let base = base.to_string();
        let mut changed = Vec::new();
        for (file, pending) in by_file {
            let source = git::read_blob_at_commit(repo, &base, &file)?;
            if let Some(bytes) = self.render_onto(&file, source.as_deref(), pending)? {
                changed.push((file, Some(bytes)));
            }
        }
        Ok(changed)
    }

    /// This worktree's files in a tree of `blobs`: each one's path, format
    /// and blob there, for the branch's file rows.
    async fn files_at(
        &self,
        blobs: &std::collections::HashMap<String, gix::ObjectId>,
    ) -> Result<Vec<(String, String, String)>> {
        Ok(db::file::list(self.db(), self.worktree_id())
            .await?
            .into_iter()
            .filter_map(|f| {
                let blob = blobs.get(&f.path)?.to_string();
                Some((f.path, f.format, blob))
            })
            .collect())
    }

    /// C.20's transaction: fork the branch at the base and move the
    /// conflicted edits, `ids`, to it. `None`, with nothing done, when
    /// they're no longer those.
    async fn move_conflicts(&self, fork: &Fork<'_>, ids: &[i64]) -> Result<Option<i64>> {
        let database = db::commit::database_id(self.db()).await?;
        let mut history = crate::ids::History::new(self.formats(), self.repo()?, database);
        on_pool!(self.db(), pool => move_in_pool(self, pool, &mut history, fork, ids).await)
    }

    /// Commit branch `n`'s draft onto `base`, fold it into its head, and
    /// move the ref there.
    async fn commit_export(
        &self,
        branch: &str,
        n: i64,
        base: gix::ObjectId,
        after: &mut impl FnMut(ExportStep) -> Result<()>,
    ) -> Result<Exported> {
        let sib = self.sibling(n);
        let repo = self.repo()?;
        let edits = on_pool!(self.db(), pool => draft_edits_in_pool(pool, n).await)?;
        let files: BTreeSet<String> = edits.iter().map(|e| e.file_path.clone()).collect();
        let changed = self.render_at(&repo, base, edits.into_iter().map(|e| e.into_record(n)))?;
        let watermark = i64::MAX;
        let (rollup, named) = sib.rollup_for(&files, watermark).await?;
        let commit = match changed.is_empty() {
            true => base,
            false => {
                let message =
                    build_commit_message(&format!("Export conflicts to {branch}"), &rollup);
                let c = git::commit_files_onto(&repo, base, &changed, &message)?;
                after(ExportStep::Written)?;
                c
            }
        };
        let oid = commit.to_string();
        // its files at the base, so only those the export changed are parsed
        let known: std::collections::HashMap<String, crate::model::File> =
            db::file::list(self.db(), n)
                .await?
                .into_iter()
                .map(|f| (f.path.clone(), f))
                .collect();
        let head_side = sib.head_files(&repo, Some(&oid), &known, false)?;
        let carried = Carried {
            commit: &oid,
            files: &files,
            removed: &[],
            watermark,
            head_files: &head_side.files,
            named: &named,
        };
        // before the ref moves: the commit is then the branch's in the
        // database, and an export that fails here only has the ref to move
        on_pool!(self.db(), pool => crate::scan::commit_in_pool(&sib, pool, &carried).await)?;
        if commit != base {
            after(ExportStep::Folded)?;
        }
        self.branch_export(branch, n, base, commit, rollup).await
    }

    /// Finish an export whose draft is folded into `commit`: move the ref
    /// there from `base`.
    async fn finish_export(
        &self,
        branch: &str,
        n: i64,
        base: gix::ObjectId,
        commit: gix::ObjectId,
    ) -> Result<Exported> {
        let message = git::commit_message(&self.repo()?, &commit.to_string()).unwrap_or_default();
        let rollup = parse_commit_rollup(&message)?.ok_or_else(|| {
            Error::Other(format!(
                "{branch}: the export's commit {commit} has no rollup"
            ))
        })?;
        self.branch_export(branch, n, base, commit, rollup).await
    }

    /// Move `branch` from `base` to `commit`, and the export is finished.
    async fn branch_export(
        &self,
        branch: &str,
        n: i64,
        base: gix::ObjectId,
        commit: gix::ObjectId,
        rollup: CommitRollup,
    ) -> Result<Exported> {
        if commit != base {
            git::move_branch(&self.repo()?, branch, base, commit)?;
        }
        db::worktree::clear_exporting(self.db(), n).await?;
        let records = rollup
            .txns
            .into_iter()
            .flat_map(|t| t.records)
            .chain(rollup.records)
            .collect();
        Ok(Exported {
            branch: branch.to_string(),
            worktree_id: n,
            commit: commit.to_string(),
            records,
        })
    }
}

fn exists(branch: &str) -> Error {
    Error::BranchExists {
        branch: branch.to_string(),
    }
}

/// Where the branch is forked: its worktree's origin and branch, the
/// exporting worktree's branch, the base commit, and the files there
/// (path, format, blob).
struct Fork<'a> {
    origin: &'a str,
    branch: &'a str,
    from: &'a str,
    base: &'a str,
    files: &'a [(String, String, String)],
}

/// The edits in `w`'s draft under an unresolved conflict, oldest first.
async fn conflicted<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
) -> Result<Vec<RecordRow>> {
    let d = DB::segs(tx, w).await?.draft;
    let open: BTreeSet<(String, String, String)> = DB::rows_in(tx, d, None, true)
        .await?
        .into_iter()
        .filter(|r| r.conflict == Some(ConflictState::Conflict))
        .map(|r| (r.file_path, r.path, r.key))
        .collect();
    let mut edits: Vec<RecordRow> = DB::rows_in(tx, d, None, false)
        .await?
        .into_iter()
        .filter(|r| {
            r.is_edit() && open.contains(&(r.file_path.clone(), r.path.clone(), r.key.clone()))
        })
        .collect();
    edits.sort_by_key(|r| r.id);
    Ok(edits)
}

async fn conflicted_in_pool<DB: Store>(pool: &sqlx::Pool<DB>, w: i64) -> Result<Vec<RecordRow>> {
    let mut tx = pool.begin().await?;
    conflicted(&mut tx, w).await
}

/// The edits in `n`'s draft, oldest first.
async fn draft_edits_in_pool<DB: Store>(pool: &sqlx::Pool<DB>, n: i64) -> Result<Vec<RecordRow>> {
    let mut tx = pool.begin().await?;
    let d = DB::segs(&mut tx, n).await?.draft;
    let mut edits: Vec<RecordRow> = DB::rows_in(&mut tx, d, None, false)
        .await?
        .into_iter()
        .filter(RecordRow::is_edit)
        .collect();
    edits.sort_by_key(|r| r.id);
    Ok(edits)
}

async fn move_in_pool<DB: Store>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    history: &mut crate::ids::History<'_>,
    fork: &Fork<'_>,
    ids: &[i64],
) -> Result<Option<i64>> {
    let w = sync.worktree_id();
    let mut tx = pool.begin().await?;
    // before any read: SQLite can't take the write lock after one
    DB::lock_family(&mut tx, sync.family_id()).await?;
    let edits = conflicted(&mut tx, w).await?;
    if edits.iter().map(|e| e.id).ne(ids.iter().copied()) {
        return Ok(None);
    }
    let Some(n) = crate::fork::fork_in(
        &mut tx,
        history,
        sync.family_id(),
        fork.origin,
        fork.branch,
        fork.base,
    )
    .await?
    else {
        return Err(Error::Other(format!(
            "{}: the export base {} falls nowhere in the family",
            fork.branch, fork.base
        )));
    };
    // its files, as the base has them: a fork has none, and this one has
    // no checkout to scan
    for (path, format, blob) in fork.files {
        let row = FileRow {
            path,
            format,
            commit_id: Some(Some(fork.base)),
            source_oid: Some(Some(blob)),
            committed_oid: Some(Some(blob)),
        };
        DB::upsert_file(&mut tx, n, &row).await?;
    }
    DB::set_exporting_from(&mut tx, n, fork.from).await?;
    copy_edits(&mut tx, n, &edits).await?;
    for e in &edits {
        resolve_conflict_in_tx(&mut tx, sync, e.at(), Resolution::Theirs, None).await?;
    }
    move_batches(&mut tx, w, n, &edits).await?;
    segments::relink(&mut tx, n).await?;
    tx.commit().await?;
    Ok(Some(n))
}

/// C.20 steps 1-3: a copy of each edit in `n`'s draft, as it is; a new
/// record where `n`'s chain has its record at another place.
async fn copy_edits<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    n: i64,
    edits: &[RecordRow],
) -> Result<()> {
    let dn = DB::segs(tx, n).await?.draft;
    for e in edits {
        let (id, _) = DB::insert(tx, dn, NewRecordRow::from(e)).await?;
        let elsewhere = DB::visible(tx, n, Scope::Chain, Filter::KeyId(e.key_id))
            .await?
            .iter()
            .any(|r| !r.deleted && r.at() != e.at());
        if elsewhere {
            let copy = DB::rows_in(tx, dn, Some(&e.file_path), false)
                .await?
                .into_iter()
                .find(|r| r.id == id)
                .expect("just inserted");
            segments::renew(tx, dn, &copy).await?;
        }
    }
    Ok(())
}

/// C.20 step 6: the batches that wrote the exported edits are `n`'s too,
/// copied where some of their edits stay in `w`'s draft, moved where none
/// do -- left behind with nothing in it, one would be outstanding in `w`
/// for good.
async fn move_batches<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    n: i64,
    edits: &[RecordRow],
) -> Result<()> {
    let d = DB::segs(tx, w).await?.draft;
    let left: Vec<i64> = DB::rows_in(tx, d, None, false)
        .await?
        .into_iter()
        .filter(RecordRow::is_edit)
        .map(|r| r.version)
        .collect();
    for t in DB::outstanding_txns(tx, w).await? {
        let range = t.first_version..=t.last_version;
        if !edits.iter().any(|e| range.contains(&e.version)) {
            continue;
        }
        if left.iter().any(|v| range.contains(v)) {
            DB::insert_txn(
                tx,
                n,
                (t.first_version, t.last_version),
                t.meta.author.as_deref(),
                t.meta.message.as_deref(),
                &t.created_at,
            )
            .await?;
        } else {
            DB::move_txn(tx, t.id, n).await?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use crate::SyncedRepo;

    /// A server's handlers hold these futures across awaits, so they must
    /// be `Send`: a `gix::Repository` borrowed across an await isn't.
    #[test]
    fn exporting_and_committing_are_send() {
        fn is_send<T: Send>(_: T) {}
        fn probe(s: &SyncedRepo) {
            is_send(s.export_conflicts("branch"));
            is_send(s.commit_repository("message", Default::default()));
        }
        let _ = probe as fn(&SyncedRepo);
    }
}
