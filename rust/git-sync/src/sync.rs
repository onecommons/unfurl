// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! [`SyncedRepo`], the public top-level handle, plus its [`CommitRef`] token.
//!
//! The methods on `SyncedRepo` are the crate's main API:
//!
//! - [`SyncedRepo::open`], [`SyncedRepo::get_working_dir`].
//! - Sync from disk: [`SyncedRepo::update_from_working_dir`].
//! - Read: [`SyncedRepo::find_records`], [`SyncedRepo::find_records_follow`],
//!   [`SyncedRepo::get_record`], [`SyncedRepo::get_record_by_id`],
//!   [`SyncedRepo::get_file`], [`SyncedRepo::get_worktree`].
//! - Mutate: [`SyncedRepo::create_record`], [`SyncedRepo::update_record`],
//!   [`SyncedRepo::upsert_record`], [`SyncedRepo::delete_record`].
//! - Persist: [`SyncedRepo::save_changes`], [`SyncedRepo::write_file`],
//!   [`SyncedRepo::commit_repository`].
//! - Settle a divergence: [`SyncedRepo::list_conflicts`],
//!   [`SyncedRepo::resolve_conflict`].
//!
//! The work behind them lives next door: [`crate::scan`] takes a file
//! into the database, [`crate::conflict`] decides what a disagreement
//! between the two means, [`crate::crud`] holds the record write
//! primitives, [`crate::document`] parses and re-emits the files, and
//! [`crate::rollup`] renders and reads the commit message.

use crate::commit::CommitId;
use std::collections::BTreeSet;
use std::path::Path;

use crate::db::{self, on_pool, Db, DbConfig};
use crate::dir::SyncedDir;
use crate::error::{Error, Result};
use crate::format::FormatRegistry;
use crate::git;
use crate::model::{CommitOptions, CommitRollup, Committed, OpenOptions, RollupTxn, ScanOptions};
use crate::rollup::build_commit_message;

/// Optimistic-concurrency token used by mutating CRUD calls.
///
/// Pass `Some(token)` as the `expected_commit` argument to
/// [`SyncedRepo::create_record`] / [`SyncedRepo::update_record`] /
/// [`SyncedRepo::upsert_record`] / [`SyncedRepo::delete_record`] to assert
/// what the caller observed about the row before issuing the write.
/// Mismatch returns [`crate::Error::Conflict`] and rolls back the
/// transaction. Pass `None` to skip the check entirely.
///
/// The two variants check different columns. They're checked
/// disjunctively: a write succeeds if **either** the row's `version`
/// passes the [`CommitRef::Pending`] check **or** its `commit_id`
/// matches a [`CommitRef::Commit`] token. A `Pending(v)` token remains
/// valid after [`SyncedRepo::commit_repository`] rolls forward,
/// since commit attribution doesn't bump `version`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CommitRef {
    /// Caller expects the row's `version` to be `<= v` — i.e. no
    /// other writer has rewritten the row since the client's
    /// observation point at version `v`.
    ///
    /// `v` is normally the worktree's batch-level "queueid" (the
    /// largest version the client has seen across any record), so
    /// the check passes for rows the client hasn't touched and any
    /// row still at the version it had when the client read. Two
    /// callers racing on the same record will see one bump the
    /// version past the other's `v`, and the loser gets
    /// [`crate::Error::Conflict`].
    Pending(i64),
    /// Caller expects the row's `commit_id` to equal this hex commit
    /// git oid string (40 hex chars for SHA-1).
    Commit(String),
}

/// Top-level handle bundling a sqlx pool, a gix repository path, and a
/// [`FormatRegistry`].
///
/// Build one with [`SyncedRepo::open`]. Cheaply cloneable — internal state
/// sits behind an [`Arc`], so background tasks can hold their own
/// clone without juggling lifetimes.
#[derive(Clone)]
pub struct SyncedRepo {
    pub(crate) dir: SyncedDir,
}

impl std::ops::Deref for SyncedRepo {
    type Target = SyncedDir;

    fn deref(&self) -> &SyncedDir {
        &self.dir
    }
}

impl SyncedRepo {
    /// Open a working tree and a database, returning a [`SyncedRepo`].
    ///
    /// Connects to the database and runs schema migrations, opens the
    /// gix repository at `working_dir`, derives `(origin, branch)` from
    /// it, and ensures a `worktree` row exists for that pair. The
    /// repo handle is then dropped — each subsequent call re-opens via
    /// `gix::open` to keep `Send` guarantees out of long-lived state.
    ///
    ///
    /// `options` can give the working tree the last word over in-flight
    /// edits, in two ways that answer different questions.
    /// [`ScanOptions::force`] is the operator's blanket assertion that
    /// the working tree is right: every in-flight row it reaches is
    /// overwritten from the file, in conflict or not.
    ///
    /// The other is per record and comes from git itself: a
    /// `Git-Sync-Resolves-Version: N` trailer on the commit that last
    /// touched a file lets whoever wrote that commit settle the
    /// conflicts they knew about. It applies only where the two sides
    /// actually **diverge**, and only to rows at `version <= N`:
    ///
    /// - a diverged row at `version <= N` is overwritten from the file
    ///   and its conflict row dropped — the author saw this one;
    /// - a diverged row written since (a higher version: the client has
    ///   moved on) is preserved and re-reported;
    /// - a row that merely has unsaved edits is untouched. The file
    ///   still holds what that edit was based on, so there is nothing
    ///   for the author to have resolved and overwriting it would
    ///   discard work no one disagreed with. This is narrower than
    ///   `force` deliberately.
    ///
    /// A conflict the client has already marked
    /// [`ConflictState::Resolved`] also stands: that decision was made
    /// knowing about the divergence, which the trailer's author cannot
    /// have been. Only the last commit touching the path is consulted,
    /// so an older trailer stops applying once the file moves again.
    /// # Errors
    ///
    /// Returns [`crate::Error::Db`] / [`crate::Error::Migrate`] for
    /// database failures and [`crate::Error::Git`] if the repo can't
    /// be opened.
    pub async fn open(
        working_dir: impl AsRef<Path>,
        db: DbConfig,
        formats: FormatRegistry,
    ) -> Result<Self> {
        Self::open_with(
            working_dir,
            db,
            formats,
            git::Trust::Full,
            OpenOptions::default(),
        )
        .await
    }

    /// Like [`Self::open`], but open with the given trust level,
    /// with [`git::Trust::Reduced`], all git config settings are ignored except for remote urls
    /// and its user's name and email (see [`git::Opener`]), and `options`.
    ///
    /// A database used through a handle opened with
    /// [`OpenOptions::pathspecs`] should always be opened with the same ones: a rebuild from HEAD sees only what they match,
    /// and would drop records of any other file. The check for that runs
    /// once the worktree is found or forked, so a refused open leaves a new
    /// worktree in place. Handles on other worktrees of the family that this
    /// one reaches, an upstream's for a rebase, see the same files.
    /// [`Self::rebase_onto`] and [`Self::merge_upstream`] still need the
    /// whole working tree clean: they rewrite commits, whatever it matches.
    ///
    /// # Errors
    ///
    /// As [`Self::open`]; [`Error::InvalidPathspec`] for a pathspec that
    /// doesn't parse, uses attribute magic, or reaches outside the working
    /// tree, or for [`OpenOptions::untracked`] without pathspecs;
    /// [`Error::OutOfScope`] when the worktree already holds a file
    /// the pathspecs don't match.
    pub async fn open_with(
        working_dir: impl AsRef<Path>,
        db: DbConfig,
        formats: FormatRegistry,
        trust: git::Trust,
        options: OpenOptions,
    ) -> Result<Self> {
        Self::open_on(
            working_dir,
            Db::connect(&db).await?,
            formats,
            trust,
            options,
        )
        .await
    }

    /// A handle on `worktree_id`, a worktree of this one's family with no
    /// checkout: this handle's database, formats and repository. Only for
    /// what reads git and the database, never a working tree, which would
    /// be this handle's.
    pub(crate) fn sibling(&self, worktree_id: i64) -> SyncedRepo {
        SyncedRepo {
            dir: self.dir.sibling(worktree_id),
        }
    }

    /// [`Self::open_with`], on `db`, an open database whose pool other
    /// handles share.
    ///
    /// # Errors
    ///
    /// As [`Self::open_with`].
    pub async fn open_on(
        working_dir: impl AsRef<Path>,
        db: Db,
        formats: FormatRegistry,
        trust: git::Trust,
        options: OpenOptions,
    ) -> Result<Self> {
        let dir = SyncedDir::open_inner(working_dir, db, formats, trust, options, false).await?;
        Ok(SyncedRepo { dir })
    }

    /// Delete this handle's worktree: see [`SyncedDir::delete_worktree`].
    ///
    /// # Errors
    ///
    /// As [`SyncedDir::delete_worktree`].
    pub async fn delete_worktree(self) -> Result<()> {
        self.dir.delete_worktree().await
    }

    /// Rebuild after a rewrite, from `recorded`, the commit `worktree`'s
    /// chain holds, onto HEAD `n`, which doesn't descend from it.
    pub(crate) async fn rebuild_onto(
        &self,
        worktree: &crate::model::Worktree,
        recorded: CommitId,
        n: CommitId,
        options: &ScanOptions,
        after: &mut impl FnMut(crate::export::ExportStep) -> Result<()>,
    ) -> Result<Option<crate::model::Exported>> {
        let repo = self.repo()?;
        let missing = !git::has_commit(&repo, recorded)?;
        let files = self
            .head_files(&repo, Some(n), &Default::default(), false)?
            .files;
        drop(repo);
        let recovery = match missing && options.recover_missing {
            true => self.prepare_recovery(recorded, n, &files).await?,
            false => None,
        };
        let rb = crate::fork::Rebuild {
            w: self.worktree_id(),
            family: self.family_id(),
            new_head: n,
            files: &files,
            keep: (missing && !options.rebuild_missing).then_some(recorded),
            recover: recovery.as_ref().map(|p| crate::fork::Recover {
                origin: &worktree.origin,
                branch: &p.branch,
                from: &worktree.branch,
                base: p.base,
                files: &p.files,
            }),
        };
        let rebuilt = crate::fork::rebuild(self.db(), self.repo()?, self.formats(), &rb).await;
        match (rebuilt, recovery) {
            (Ok(Some(b)), Some(p)) => {
                after(crate::export::ExportStep::Moved)?;
                let exported = self.commit_export(&p.branch, b, p.base, after);
                exported.await.map(Some)
            }
            (Ok(_), _) => Ok(None),
            (Err(e), _) => Err(self.missing_since(e, n).await),
        }
    }

    /// Where the records commits since `recorded`, which the repository
    /// doesn't have, made would go, when rebuilding onto HEAD `n`, whose
    /// tree is `files`, would lose any: the commit they were made on, and
    /// every file of its tree. `None` when it loses none, or `n`'s history
    /// has no commit of this worktree's chain to recover them at.
    async fn prepare_recovery(
        &self,
        recorded: CommitId,
        n: CommitId,
        files: &[crate::scan::HeadFile],
    ) -> Result<Option<Prepared>> {
        let rows = crate::fork::chain_rows(self.db(), self.worktree_id()).await?;
        let known = crate::fork::chain_commits(self.db(), self.worktree_id()).await?;
        let repo = self.repo()?;
        if !crate::fork::would_lose(&repo, rows, files)? {
            return Ok(None);
        }
        let Some(base) = crate::fork::recovery_base(&repo, n, &known)? else {
            return Ok(None);
        };
        let files = self
            .head_files(&repo, Some(base), &Default::default(), false)?
            .files;
        Ok(Some(Prepared {
            branch: format!("git-sync/recovered-{}", recorded.short(12)),
            base,
            files,
        }))
    }

    /// `e`, with where to read the records a [`Error::CommitMissing`]
    /// would lose: what was written after the nearest commit HEAD `n` has
    /// that this database made.
    async fn missing_since(&self, e: Error, n: CommitId) -> Error {
        let Error::CommitMissing { commit, lost, .. } = e else {
            return e;
        };
        let since = match db::commit::database_id(self.db()).await {
            Ok(database) => self
                .repo()
                .and_then(|repo| crate::fork::written_since(&repo, n, &database))
                .ok()
                .flatten(),
            Err(_) => None,
        };
        Error::CommitMissing {
            commit,
            lost,
            since,
        }
    }

    /// Persist all pending edits and create a git commit.
    ///
    /// Equivalent to [`Self::save_changes`], followed by a gix commit
    /// of the dirty paths under `message`, followed by rolling the
    /// new commit oid into every affected `record` / `file` /
    /// `worktree` row in a single transaction. Tombstones for the
    /// committed paths are purged at the same time.
    ///
    /// A full [`Self::update_from_working_dir`] runs first, so the save
    /// below renders over a current picture and `the fold` only
    /// ever stamps rows whose json the commit actually carries — cheap
    /// when nothing changed on disk (the unchanged-file skip).
    ///
    /// A record the scan finds in conflict is **not** committed: the
    /// save left the file's value in place, so the commit carries that,
    /// and `the fold` stamps the conflict row rather than the
    /// record it shadows. The record stays in flight until
    /// [`Self::resolve_conflict`] settles it, and a later commit
    /// carries it then. Conflicts are logged but not returned here; a
    /// caller that wants them structured should run
    /// [`Self::update_from_working_dir`] itself beforehand and read
    /// [`SyncOutcome::conflicts`], or ask [`Self::list_conflicts`].
    ///
    /// With [`CommitOptions::conflicts_to_branch`], the conflicts the scan
    /// finds go to that branch instead ([`Self::export_conflicts`]) before
    /// anything is saved, and the commit carries the file's values for
    /// them. [`Error::HeadMoved`] is then retried once here, without
    /// exporting again, so the export isn't lost to a caller's retry. An
    /// error after the export leaves the edits on the branch the options
    /// name; calling this again with them finishes an export that stopped
    /// before its commit.
    ///
    /// When outstanding `txn` audit rows exist (batches applied with a
    /// [`TxnMeta`] since the last commit), a "Rollup of N git-sync
    /// transactions" section listing them is appended to `message` —
    /// one commit carries many batches, so this is where their
    /// individual authors and messages survive. Those rows are stamped
    /// with the new oid alongside the records.
    ///
    /// [`Committed::commit`] is the new commit oid as a hex string, or `None` when no
    /// commit was made — either nothing was in flight, or what was in
    /// flight turned out to need no commit: a record whose value the
    /// file already held, or one left unwritten because it is in
    /// conflict. In that last case the in-flight rows are still rolled
    /// forward onto the commit that *does* carry their content (HEAD),
    /// so `None` means "no new commit", not "nothing happened".
    /// Committing regardless would append an empty commit on every
    /// call, and an unresolved conflict would do it forever.
    ///
    /// # Errors
    ///
    /// Surfaces [`crate::Error::Git`] if gix can't construct the
    /// commit, [`crate::Error::Io`] for filesystem trouble during
    /// `save_changes`, and any underlying database error.
    pub async fn commit_repository(
        &self,
        message: &str,
        options: CommitOptions,
    ) -> Result<Committed> {
        self.commit_repository_racing(message, options, async || Ok(()))
            .await
    }

    /// [`Self::commit_repository`], running `after_export` between the
    /// export and the commit: where `HEAD` could move.
    pub(crate) async fn commit_repository_racing(
        &self,
        message: &str,
        options: CommitOptions,
        mut after_export: impl AsyncFnMut() -> Result<()>,
    ) -> Result<Committed> {
        // Take any outside edits in before saving: the writes below
        // then render over a current picture, and the fold can't
        // stamp a stale row with a commit that doesn't carry its json.
        let (_, head) = self.scan(ScanOptions::default()).await?;
        let exported = match &options.conflicts_to_branch {
            Some(branch) => self.export_conflicts(branch).await?,
            None => None,
        };
        after_export().await?;
        let (commit, parent) = match self.commit_scanned(message, head).await {
            Err(Error::HeadMoved { .. }) if exported.is_some() => {
                let (_, head) = self.scan(ScanOptions::default()).await?;
                (self.commit_scanned(message, head).await?, head)
            }
            made => (made?, head),
        };
        Ok(Committed {
            commit,
            parent,
            exported,
        })
    }

    /// The rest of [`Self::commit_repository`], after a scan that took in
    /// `scanned_head`. The commit is made onto it, so [`Error::HeadMoved`]
    /// if `HEAD` has moved since.
    pub(crate) async fn commit_scanned(
        &self,
        message: &str,
        scanned_head: Option<CommitId>,
    ) -> Result<Option<CommitId>> {
        // Snapshot the dirty file list *before* save_changes (which sets
        // no commit_id changes) so we know what to stage even when bytes
        // didn't actually change on disk.
        let mut dirty: Vec<String> =
            db::record::list_dirty_files(self.db(), self.worktree_id()).await?;
        if dirty.is_empty() {
            return Ok(None);
        }
        // Rows written from here on aren't in what the save renders, so
        // the fold leaves them for the next commit.
        let watermark = db::worktree::next_version(self.db(), self.worktree_id()).await?;
        let saved = self.save_changes().await?;
        if !saved.failed.is_empty() {
            // Committing now would capture a partial application: some
            // files hold their pending records, others do not. Surface
            // the first reason -- `save_changes` reports them all -- and
            // leave the already-written files in the working tree, where
            // a retry after the cause is fixed picks them up unchanged.
            let first = saved.failed.into_iter().next().expect("non-empty");
            return Err(first.error);
        }
        // File rows the database owes a removal for. A path through a link
        // is never read: one the scan dropped is removed from the
        // database only, and any other's rows wait as they are.
        let tombstoned: std::collections::HashSet<String> =
            db::file::list(self.db(), self.worktree_id())
                .await?
                .into_iter()
                .filter(|f| f.deleted)
                .map(|f| f.path)
                .collect();
        let linked = |rel: &str| crate::scope::through_link(self.repo_path(), rel);
        dirty.retain(|rel| !linked(rel) || tombstoned.contains(rel));

        let files: BTreeSet<String> = dirty.iter().cloned().collect();
        let (rollup, named) = self.rollup_for(&files, watermark).await?;
        let message = build_commit_message(message, &rollup);

        // Only files whose bytes differ from HEAD go into a commit.
        // Being dirty in this crate's sense — an in-flight row — does
        // not mean the working tree has anything new to record: a
        // conflicted record is deliberately not written, and a write of
        // a value the file already held changes nothing. Committing
        // those anyway would mint an empty commit on every call, and a
        // standing conflict would do it forever.
        let repo = self.repo()?;
        let head = scanned_head;
        let to_stage: Vec<String> = dirty
            .iter()
            .filter(|rel| !linked(rel) && self.differs_from_head(&repo, head, rel))
            .cloned()
            .collect();
        // The tombstoned rows with no file behind. Drawn from `dirty`
        // rather than `to_stage`: a deletion git has already recorded
        // stages nothing, and its row would otherwise stay tombstoned with
        // no commit ever coming to purge it.
        let removed: Vec<String> = dirty
            .iter()
            .filter(|rel| {
                tombstoned.contains(*rel) && (linked(rel) || !self.repo_path().join(rel).exists())
            })
            .cloned()
            .collect();

        let commit = match (to_stage.is_empty(), head) {
            (false, _) => git::commit_paths_onto(&repo, &to_stage, &message, scanned_head)?,
            // Nothing to record, but rows are still in flight: their
            // content is what HEAD already holds, so attribute them to
            // the commit that does carry it rather than manufacture an
            // empty one. (A row whose content is *not* in HEAD is a
            // conflicted one, which `the fold` skips.)
            (true, Some(head)) => head,
            // Nothing staged and no HEAD to fall back on: an unborn
            // repository with nothing to put in its first commit.
            (true, None) => return Ok(None),
        };

        // Fold what the commit carries into the head, in one transaction.
        let known_files: std::collections::HashMap<String, crate::model::File> =
            db::file::list(self.db(), self.worktree_id())
                .await?
                .into_iter()
                .map(|f| (f.path.clone(), f))
                .collect();
        let head_side = self.head_files(&repo, Some(commit), &known_files, false)?;
        let carried = crate::scan::Carried {
            commit,
            files: &files,
            removed: &removed,
            watermark,
            head_files: &head_side.files,
            named: &named,
        };
        on_pool!(self.db(), pool => crate::scan::commit_in_pool(self, pool, &carried).await)?;

        Ok((!to_stage.is_empty()).then_some(commit))
    }

    /// The rollup of a commit carrying `files`' rows written below
    /// `watermark`, and the ids it names by place, which the commit's head
    /// rows take.
    pub(crate) async fn rollup_for(
        &self,
        files: &BTreeSet<String>,
        watermark: i64,
    ) -> Result<(
        CommitRollup,
        std::collections::BTreeMap<crate::db::tables::Place, i64>,
    )> {
        // Read the audit rows before the fold stamps them: this
        // commit is the one that carries their writes. Each batch's
        // records are resolved here too — also before the fold,
        // which purges the tombstones a delete leaves behind.
        let txns = db::commit::list_outstanding(self.db(), self.worktree_id()).await?;
        let worktree = db::worktree::get(self.db(), self.worktree_id()).await?;
        let mut records =
            db::record::committed_records(self.db(), self.worktree_id(), files, watermark).await?;
        let mut entries = Vec::with_capacity(txns.len());
        for txn in txns {
            let (batch, rest) = records
                .into_iter()
                .partition(|r| (txn.first_version..=txn.last_version).contains(&r.version));
            records = rest;
            entries.push(RollupTxn {
                first_version: txn.first_version,
                last_version: txn.last_version,
                branch: worktree.branch.clone(),
                created_at: txn.created_at,
                meta: txn.meta,
                records: batch,
            });
        }
        // The family root's origin identifies the version sequence these
        // ranges came from; skip the lookup when this worktree is its own
        // family, which is the usual case.
        let family = if self.family_id() == self.worktree_id() {
            worktree.origin.clone()
        } else {
            db::worktree::get(self.db(), self.family_id()).await?.origin
        };
        let rollup = CommitRollup {
            origin: Some(worktree.origin.clone()),
            family: Some(family),
            database: Some(db::commit::database_id(self.db()).await?),
            next_version: db::worktree::next_version(self.db(), self.worktree_id()).await?,
            txns: entries,
            records,
        };
        let named: std::collections::BTreeMap<crate::db::tables::Place, i64> = rollup
            .txns
            .iter()
            .flat_map(|t| &t.records)
            .chain(&rollup.records)
            .filter_map(|r| {
                let place = crate::db::tables::Place::new(r.file_path.as_deref()?, &r.path, &r.key);
                Some((place, r.key_id?))
            })
            .collect();

        Ok((rollup, named))
    }

    /// Whether `rel`'s bytes on disk differ from what HEAD records for
    /// it — i.e. whether committing it would change anything.
    ///
    /// An unreadable file counts as differing so the read error
    /// surfaces from [`git::commit_paths`], which is where it was
    /// reported before this check existed.
    fn differs_from_head(&self, repo: &gix::Repository, head: Option<CommitId>, rel: &str) -> bool {
        let Some(head) = head else {
            return true;
        };
        let disk = match std::fs::read(self.repo_path().join(rel)) {
            Ok(bytes) => Some(bytes),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
            // Unreadable for some other reason: stage it and let
            // `commit_paths` report the failure rather than guess.
            Err(_) => return true,
        };
        // Both sides as `Option`, so a path absent from the working tree
        // *and* from HEAD compares equal -- a deletion git already
        // records is not something to commit again.
        disk != git::read_blob_at_commit(repo, head, rel).ok().flatten()
    }
}

/// A recovery branch claimed at `base`, the commit the records it takes
/// were made on, with every file of its tree.
struct Prepared {
    branch: String,
    base: CommitId,
    files: Vec<crate::scan::HeadFile>,
}
