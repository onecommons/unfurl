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

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::conflict::{
    apply_conflict_ops_in_tx, apply_pending_records, resolve_conflict_in_pool, Applying,
    ConflictCheck, ConflictOp,
};
use crate::crud::{
    apply_batch_inner, crud_create_in_pool, crud_delete_in_pool, crud_update_in_pool,
    crud_upsert_in_pool, delete_file_in_pool, WriteTarget,
};
use crate::db::{self, on_pool, Db, DbConfig};
use crate::document::{extract_ext, new_root, stage_write, Syntax};
use crate::error::{Error, Result};
use crate::format::{FormatRegistry, GENERIC_LITERATE_FORMAT};
use crate::git;
use crate::model::{
    BatchOp, BatchOutcome, CommitOptions, CommitRollup, Committed, Record, RecordQuery, Resolution,
    RollupTxn, ScanOptions, SyncOutcome, Txn, TxnMeta, WriteFileOutcome, WriteOutcome,
};
use crate::rollup::{build_commit_message, resolves_version_from_message};
use crate::scan::{DiskFile, HeadFile, ParsedDoc, ScannedFile};

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
    inner: Arc<SyncedRepoInner>,
}

struct SyncedRepoInner {
    db: Db,
    repo_path: PathBuf,
    /// Shared with [`SyncedRepo::sibling`]'s handles.
    formats: Arc<FormatRegistry>,
    worktree_id: i64,
    /// Root of this worktree's version family — see
    /// [`db::worktree::family_id`]. Resolved once at open because every
    /// write draws from it.
    family_id: i64,
    /// Files found in no format, so a scan doesn't parse them again until
    /// they change. In memory only: the formats are fixed at open, and a
    /// new process parses each once.
    unrecognized: std::sync::Mutex<Unrecognized>,
    /// The HEAD the last scan walked history at. While HEAD stays there, a
    /// clean file's last commit is the one on record.
    walked_head: std::sync::Mutex<Option<gix::ObjectId>>,
}

/// How many findings of one grade are logged individually before the rest
/// are left to the count in the summary.
///
/// A document can be wrong in every record it has, and one log line each
/// would bury everything else the scan reported.
const MAX_LOGGED_FINDINGS: usize = 10;

/// Emits one event per validation finding, plus a summary line.
///
/// `warn` for anything the scan acted on, `info` for the advisory grade:
/// a document skipped for a bad header and one skipped for bad syntax
/// have the same consequence, so they log at the same level.
fn log_validation(rel_path: &str, format: &str, validation: &crate::Validation) {
    if validation.is_empty() {
        return;
    }
    for error in validation.fatal.iter().take(MAX_LOGGED_FINDINGS) {
        tracing::warn!(
            file = rel_path,
            format,
            error = error.to_string().as_str(),
            "document is unreadable as this format; skipping the file"
        );
    }
    let mut refused_sections = 0usize;
    let mut refused_records = 0usize;
    for ((section, key), error) in &validation.errors {
        match key {
            None => {
                refused_sections += 1;
                if refused_sections <= MAX_LOGGED_FINDINGS {
                    tracing::warn!(
                        file = rel_path,
                        format,
                        path = format!("/{section}").as_str(),
                        error = error.to_string().as_str(),
                        "section refused; the records it holds are left as they are"
                    );
                }
            }
            Some(key) => {
                refused_records += 1;
                if refused_records <= MAX_LOGGED_FINDINGS {
                    tracing::warn!(
                        file = rel_path,
                        format,
                        path = format!("/{section}").as_str(),
                        key = key.as_str(),
                        error = error.to_string().as_str(),
                        "record refused; the row already indexed is left as it is"
                    );
                }
            }
        }
    }
    for ((section, key), error) in validation.warnings.iter().take(MAX_LOGGED_FINDINGS) {
        // `key` omitted rather than empty when the warning is the whole
        // section's: a field that is always present but sometimes blank is
        // one a filter cannot use.
        match key {
            None => tracing::info!(
                file = rel_path,
                format,
                path = format!("/{section}").as_str(),
                error = error.to_string().as_str(),
                "schema warning; indexed anyway"
            ),
            Some(key) => tracing::info!(
                file = rel_path,
                format,
                path = format!("/{section}").as_str(),
                key = key.as_str(),
                error = error.to_string().as_str(),
                "schema warning; indexed anyway"
            ),
        }
    }
    // The line to grep first: one per file, with counts rather than
    // contents, and the only one emitted for a file whose findings ran
    // past the per-grade cap.
    tracing::warn!(
        file = rel_path,
        format,
        fatal = validation.fatal.len(),
        refused_sections,
        refused_records,
        warnings = validation.warnings.len(),
        "document does not conform to its schema"
    );
}

/// What a scan's committed side found at HEAD.
pub(crate) struct HeadSide {
    /// Every blob in HEAD's tree, by path.
    blobs: std::collections::HashMap<String, gix::ObjectId>,
    /// The files whose blob isn't the one the committed segments hold.
    pub(crate) files: Vec<HeadFile>,
    /// How many of HEAD's files were parsed to find them.
    parsed: usize,
}

/// The blob each file was found in no format at, by path: the working
/// tree's copy and HEAD's, which differ while the file is dirty.
#[derive(Default)]
struct Unrecognized {
    disk: std::collections::HashMap<String, gix::ObjectId>,
    head: std::collections::HashMap<String, gix::ObjectId>,
}

impl Unrecognized {
    fn contains(&self, path: &str, blob: gix::ObjectId) -> bool {
        self.disk.get(path) == Some(&blob) || self.head.get(path) == Some(&blob)
    }
}

/// What a scan's first pass reads each tracked file against.
struct FirstPass<'a> {
    repo: &'a gix::Repository,
    options: &'a ScanOptions,
    /// HEAD is where the last scan walked history.
    head_unmoved: bool,
    head_blobs: &'a std::collections::HashMap<String, gix::ObjectId>,
    /// The files HEAD changed since the committed segments took them in.
    head_changed: &'a BTreeSet<String>,
    known_files: &'a std::collections::HashMap<String, crate::model::File>,
}

/// What a scan's first pass makes of one tracked file.
enum Classified<'a> {
    /// Not a document, unreadable, or in no format.
    Skip,
    /// Still in the index, gone from the working tree.
    Vanished,
    Candidate(Candidate<'a>),
}

/// What a scan's first pass learned about one tracked file.
struct Candidate<'a> {
    tf: &'a git::TrackedFile,
    clean: bool,
    disk_blob: String,
    /// The parsed document, present when the bytes differ from
    /// what the database last took in, or HEAD changed the file.
    parsed_doc: Option<ParsedDoc<'a>>,
    db_commit: Option<String>,
    /// Its last commit may have moved, so history is walked for it.
    needs_history: bool,
}

impl Candidate<'_> {
    /// The last commit to touch the file: from the walk, or for a file not
    /// walked, the one on record.
    fn last_commit<'a>(
        &'a self,
        last_commits: &'a std::collections::HashMap<String, String>,
    ) -> Option<&'a str> {
        match last_commits.get(&self.tf.rel_path) {
            Some(commit) => Some(commit),
            None if !self.needs_history => self.db_commit.as_deref(),
            None => None,
        }
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
        let repo_path = working_dir.as_ref().to_path_buf();
        let db = Db::connect(&db).await?;

        // Inspect the repo once at open time to get origin/branch, and to
        // place a new worktree in its family, which consumes it: we re-open
        // per call to keep `Send` guarantees out of long-lived state.
        let repo = git::open_repo(&repo_path)?;
        let meta = git::worktree_meta(&repo)?;
        let head = meta.head_oid.map(|o| o.to_string());
        let worktree_id = crate::fork::open(
            &db,
            repo,
            &formats,
            &meta.origin,
            &meta.branch,
            head.as_deref(),
        )
        .await?;
        let family_id = db::worktree::family_id(&db, worktree_id).await?;

        Ok(Self {
            inner: Arc::new(SyncedRepoInner {
                db,
                repo_path,
                formats: Arc::new(formats),
                worktree_id,
                family_id,
                unrecognized: Default::default(),
                walked_head: Default::default(),
            }),
        })
    }

    pub(crate) fn repo(&self) -> Result<gix::Repository> {
        git::open_repo(&self.inner.repo_path)
    }

    /// The id of the worktree this handle is bound to.
    pub fn worktree_id(&self) -> i64 {
        self.inner.worktree_id
    }

    /// Root worktree of the version family this one draws from: itself,
    /// or the upstream it was forked from.
    pub(crate) fn family_id(&self) -> i64 {
        self.inner.family_id
    }

    pub(crate) fn db(&self) -> &Db {
        &self.inner.db
    }

    pub(crate) fn formats(&self) -> &FormatRegistry {
        &self.inner.formats
    }

    /// A handle on `worktree_id`, a worktree of this one's family with no
    /// checkout: this handle's database, formats and repository. Only for
    /// what reads git and the database, never a working tree, which would
    /// be this handle's.
    pub(crate) fn sibling(&self, worktree_id: i64) -> SyncedRepo {
        SyncedRepo {
            inner: Arc::new(SyncedRepoInner {
                db: self.inner.db.clone(),
                repo_path: self.inner.repo_path.clone(),
                formats: Arc::clone(&self.inner.formats),
                worktree_id,
                family_id: self.inner.family_id,
                unrecognized: Default::default(),
                walked_head: Default::default(),
            }),
        }
    }

    /// Returns a [`crate::model::WorkingDir`] snapshot of this handle.
    ///
    /// Reads `(repo_path, branch, head_commit)` directly from the gix
    /// repository — `head_commit` is `None` for an unborn / empty
    /// repo. Useful for stashing a reference to the current commit
    /// before issuing CRUD calls so that conflict tokens can be
    /// constructed later.
    pub async fn get_working_dir(&self) -> Result<crate::model::WorkingDir> {
        let repo = self.repo()?;
        let meta = git::worktree_meta(&repo)?;
        Ok(crate::model::WorkingDir {
            repo_path: self.inner.repo_path.clone(),
            branch: meta.branch,
            head_commit: meta.head_oid.map(|o| o.to_string()),
        })
    }

    /// Walk the working tree, parse new/changed files, and upsert
    /// records into the database.
    ///
    /// Two-pass implementation: the first pass reads and hashes each
    /// tracked `.yaml` / `.yml` / `.json` file, parsing and classifying
    /// (via the registry's [`FormatRegistry::detect`]) only those whose
    /// bytes differ from what the database last took in; the second
    /// pass resolves the commit that last touched each candidate path
    /// by walking ancestors of HEAD once via
    /// [`crate::git::last_commits_for_paths`], then processes each
    /// file. A file whose bytes *and* git state (dirty, or clean as of
    /// commit X) both match the last take-in is skipped whole —
    /// counted in [`SyncOutcome::files_unchanged`], its records left
    /// untouched, and not re-classified against the format registry.
    /// A file whose bytes match but whose git state moved has its
    /// commit attribution refreshed in place
    /// ([`db::file::reattribute`]), also without a parse.
    /// After indexing, the worktree's `commit_id` is bumped to HEAD.
    ///
    /// Records are stamped with the path's last commit whether or not
    /// the file is clean (the on-disk blob matches the index entry) —
    /// `record.commit_id = NULL` is reserved for client edits that
    /// haven't been committed. Cleanliness decides the *file* row's
    /// `commit_id` instead: NULL there marks a file whose content is
    /// not in git, which is what makes [`Self::commit_repository`]
    /// stage it. A path that appears in no commit at all resolves to
    /// NULL either way, so its rows read as in-flight until the first
    /// commit carries them.
    ///
    /// In-flight client edits (`record.commit_id IS NULL`) are
    /// **preserved**, not overwritten, wherever the file disagrees with
    /// them; each divergence is reported in [`SyncOutcome::conflicts`],
    /// logged, and materialized as a conflict row holding the file's
    /// value (see [`crate::ConflictState`] and
    /// [`upsert_file_and_records_inner`]).
    ///
    /// `options` can hand the working tree the last word over those
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
    ///
    /// # Errors
    ///
    /// Returns [`crate::Error::Yaml`] / [`crate::Error::Json`] when a
    /// tracked file fails to parse, or any underlying git / database
    /// error.
    pub async fn update_from_working_dir(&self, options: ScanOptions) -> Result<SyncOutcome> {
        Ok(self.scan(options).await?.0)
    }

    /// [`Self::update_from_working_dir`], also returning the `HEAD` it took
    /// in.
    async fn scan(&self, options: ScanOptions) -> Result<(SyncOutcome, Option<gix::ObjectId>)> {
        // HEAD is read once, so the commit stamped below is the one the
        // scan took in, and a rebuild recorded, even if it moves meanwhile.
        let meta = git::worktree_meta(&self.repo()?)?;
        let stats = self.scan_files(&options, &meta).await?;
        *self.walked_head() = meta.head_oid;
        if let Some(oid) = meta.head_oid {
            db::worktree::update_commit(self.db(), self.worktree_id(), Some(&oid.to_string()))
                .await?;
        }

        // Auto-pick a default file_path for new records on the first
        // run. No-op when an operator has already pinned a value.
        db::worktree::auto_pick_default_file(self.db(), self.worktree_id()).await?;

        Ok((stats, meta.head_oid))
    }

    /// Refuse a checkout on another branch than the handle's, and rebuild
    /// after a rewrite: a rebase, reset or force-push leaves the chain
    /// holding a commit HEAD `head` doesn't descend from (§4.8). Where the
    /// repository doesn't have that commit at all, refuse a rebuild that
    /// would lose committed records, unless `options` allow it or recover
    /// them to a branch, which it returns.
    async fn follow_head(
        &self,
        branch: &str,
        head: Option<&str>,
        options: &ScanOptions,
    ) -> Result<Option<crate::model::Exported>> {
        let worktree = db::worktree::get(self.db(), self.worktree_id()).await?;
        // a handle opened detached (CI, a tag, a pinned commit) follows
        // whatever is checked out; the rebuild covers a rewrite
        if worktree.branch != git::DETACHED {
            if branch == git::DETACHED {
                return Err(Error::Detached {
                    branch: worktree.branch,
                });
            }
            if branch != worktree.branch {
                return Err(Error::BranchChanged {
                    expected: worktree.branch,
                    found: branch.to_string(),
                });
            }
        }
        let (Some(recorded), Some(n)) = (worktree.commit_id.as_deref(), head) else {
            return Ok(None);
        };
        let repo = self.repo()?;
        if recorded == n || git::is_ancestor(&repo, recorded, n)? {
            return Ok(None);
        }
        drop(repo);
        self.rebuild_onto(&worktree, recorded, n, options, &mut |_| Ok(()))
            .await
    }

    /// Rebuild after a rewrite, from `recorded`, the commit `worktree`'s
    /// chain holds, onto HEAD `n`, which doesn't descend from it.
    async fn rebuild_onto(
        &self,
        worktree: &crate::model::Worktree,
        recorded: &str,
        n: &str,
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
            n,
            files: &files,
            keep: (missing && !options.rebuild_missing).then_some(recorded),
            recover: recovery.as_ref().map(|p| crate::fork::Recover {
                origin: &worktree.origin,
                branch: &p.branch,
                from: &worktree.branch,
                base: &p.base,
                files: &p.files,
            }),
        };
        let rebuilt = crate::fork::rebuild(self.db(), self.repo()?, self.formats(), &rb).await;
        match (rebuilt, recovery) {
            (Ok(Some(b)), Some(p)) => {
                after(crate::export::ExportStep::Moved)?;
                let exported = self.commit_export(&p.branch, b, oid(&p.base)?, after);
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
        recorded: &str,
        n: &str,
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
            .head_files(&repo, Some(&base), &Default::default(), false)?
            .files;
        Ok(Some(Prepared {
            branch: format!("git-sync/recovered-{}", &recorded[..recorded.len().min(12)]),
            base,
            files,
        }))
    }

    /// `e`, with where to read the records a [`Error::CommitMissing`]
    /// would lose: what was written after the nearest commit HEAD `n` has
    /// that this database made.
    async fn missing_since(&self, e: Error, n: &str) -> Error {
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

    /// What a scan's first pass makes of one tracked file: nothing, a
    /// file gone from the working tree, or a candidate -- parsed when its
    /// bytes or HEAD's copy changed since the last take-in.
    fn classify<'a>(
        &'a self,
        pass: &FirstPass<'_>,
        tf: &'a git::TrackedFile,
        stats: &mut SyncOutcome,
    ) -> Classified<'a> {
        let Some(syntax) = Syntax::for_extension(&extract_ext(&tf.rel_path)) else {
            return Classified::Skip;
        };
        // Most markdown in a repository is prose. Settle that from
        // the first line rather than reading and hashing every
        // README to find out.
        if syntax == Syntax::Markdown
            && matches!(
                unfurl_merge::markdown::find_literate_directive(&tf.abs_path),
                Ok(None)
            )
        {
            return Classified::Skip;
        }
        let bytes = match std::fs::read(&tf.abs_path) {
            Ok(b) => b,
            // Still in the index, gone from the working tree: a
            // plain `rm`. Every other read failure leaves the file
            // alone rather than reading as a deletion.
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Classified::Vanished,
            Err(_) => return Classified::Skip,
        };
        let blob = match git::blob_oid_for_bytes(pass.repo, &bytes) {
            Ok(blob) => blob,
            Err(error) => {
                tracing::warn!(
                    file = tf.rel_path.as_str(),
                    error = error.to_string().as_str(),
                    "file could not be hashed"
                );
                stats.unparsed.push(crate::model::FileFailure {
                    file_path: tf.rel_path.clone(),
                    error,
                });
                return Classified::Skip;
            }
        };
        // Clean: what HEAD has, so nothing for a commit to carry.
        let clean = pass.head_blobs.get(&tf.rel_path) == Some(&blob);
        let disk_blob = blob.to_string();

        let db_file = pass.known_files.get(&tf.rel_path);
        if db_file.is_none()
            && !pass.options.force
            && self.unrecognized().contains(&tf.rel_path, blob)
        {
            return Classified::Skip;
        }
        let db_commit = db_file.and_then(|f| f.commit_id.clone());
        let head_changed = pass.head_changed.contains(&tf.rel_path);
        let parsed_doc = if !pass.options.force
            && !head_changed
            && db_file.is_some_and(|f| f.source_oid.as_deref() == Some(disk_blob.as_str()))
        {
            None
        } else {
            match self.parse_and_detect(&tf.rel_path, syntax, &bytes, stats) {
                Ok(Some(doc)) => {
                    self.unrecognized().disk.remove(&tf.rel_path);
                    Some(doc)
                }
                Ok(None) => {
                    self.unrecognized().disk.insert(tf.rel_path.clone(), blob);
                    return Classified::Skip;
                }
                Err(error) => {
                    tracing::warn!(
                        file = tf.rel_path.as_str(),
                        syntax = ?syntax,
                        error = error.to_string().as_str(),
                        "file could not be parsed"
                    );
                    stats.unparsed.push(crate::model::FileFailure {
                        file_path: tf.rel_path.clone(),
                        error,
                    });
                    return Classified::Skip;
                }
            }
        };
        // A walk at the same HEAD finds a clean file's last commit where
        // the last one did, so only the rest need one.
        let needs_history = parsed_doc.is_some()
            || head_changed
            || !(pass.head_unmoved && clean && db_commit.is_some());
        Classified::Candidate(Candidate {
            tf,
            clean,
            disk_blob,
            parsed_doc,
            db_commit,
            needs_history,
        })
    }

    /// Take HEAD, as `meta` read it, and the working tree into the
    /// database (§4.3).
    ///
    /// The committed side first: every file whose blob at HEAD isn't the
    /// one the committed segments hold is parsed from HEAD, and the head
    /// segment brought up to it. Then the draft side: every tracked file
    /// whose bytes differ from what was last taken in, or which HEAD
    /// just changed, is parsed from disk and reconciled against the
    /// committed chain; in-flight client edits are preserved and
    /// divergences reported in [`SyncOutcome::conflicts`]. A file whose
    /// bytes and git state both match its last take-in is skipped whole.
    async fn scan_files(
        &self,
        options: &ScanOptions,
        meta: &git::WorktreeMeta,
    ) -> Result<SyncOutcome> {
        let repo = self.repo()?;
        let head = meta.head_oid.map(|o| o.to_string());
        let recovered = self
            .follow_head(&meta.branch, head.as_deref(), options)
            .await?;
        let tracked = git::tracked_files(&repo)?;
        let known_files: std::collections::HashMap<String, crate::model::File> =
            db::file::list(self.db(), self.worktree_id())
                .await?
                .into_iter()
                .map(|f| (f.path.clone(), f))
                .collect();
        let mut stats = SyncOutcome {
            recovered,
            ..Default::default()
        };

        // The committed side: HEAD's blobs against the committed segments'.
        let HeadSide {
            blobs: head_blobs,
            files: head_files,
            parsed: head_parsed,
        } = self.head_files(&repo, head.as_deref(), &known_files, options.force)?;
        stats.files_parsed += head_parsed;
        let head_changed: BTreeSet<String> =
            head_files.iter().map(|f| f.rel_path.clone()).collect();

        let mut candidates: Vec<Candidate<'_>> = Vec::new();
        let mut walk_paths: Vec<String> = Vec::new();
        let mut indexed: std::collections::HashSet<&str> =
            std::collections::HashSet::with_capacity(tracked.len());
        let mut vanished: std::collections::HashSet<String> = std::collections::HashSet::new();
        // `pass` borrows the repository, which isn't `Sync`, so it's
        // scoped to end before the next await
        {
            let pass = FirstPass {
                repo: &repo,
                options,
                head_unmoved: meta.head_oid.is_some() && *self.walked_head() == meta.head_oid,
                head_blobs: &head_blobs,
                head_changed: &head_changed,
                known_files: &known_files,
            };

            for tf in &tracked {
                stats.files_seen += 1;
                indexed.insert(tf.rel_path.as_str());
                match self.classify(&pass, tf, &mut stats) {
                    Classified::Skip => {}
                    Classified::Vanished => {
                        vanished.insert(tf.rel_path.clone());
                    }
                    Classified::Candidate(c) => {
                        if c.needs_history {
                            walk_paths.push(tf.rel_path.clone());
                        }
                        candidates.push(c);
                    }
                }
            }
        }

        {
            let mut unrecognized = self.unrecognized();
            unrecognized
                .disk
                .retain(|path, _| indexed.contains(path.as_str()));
            unrecognized
                .head
                .retain(|path, _| head_blobs.contains_key(path));
        }
        // Files the database knows and the working tree no longer has,
        // either because the index dropped them or because the file
        // itself is gone. Their records are taken in as deleted.
        let mut orphans: Vec<String> = known_files
            .iter()
            .filter(|(path, f)| {
                !f.deleted && (!indexed.contains(path.as_str()) || vanished.contains(*path))
            })
            .map(|(path, _)| path.clone())
            .collect();
        orphans.sort();

        // A rename is an orphan and a brand-new path holding the exact
        // bytes the orphan's records were parsed from. Exact-content
        // only: a move that also edited the file, or an ambiguity in
        // either direction, falls back to delete-and-add -- noisier, but
        // never a guess.
        let mut renames: Vec<(String, String)> = Vec::new();
        if !orphans.is_empty() {
            let arrivals: Vec<(&str, &str)> = candidates
                .iter()
                .filter(|c| !known_files.contains_key(&c.tf.rel_path))
                .map(|c| (c.tf.rel_path.as_str(), c.disk_blob.as_str()))
                .collect();
            renames = crate::scan::match_renames(&orphans, &known_files, &arrivals);
            for (from, to) in &renames {
                orphans.retain(|p| p != from);
                // The records the destination holds are the ones being
                // moved: no reparse.
                if let Some(c) = candidates.iter_mut().find(|c| &c.tf.rel_path == to) {
                    c.parsed_doc = None;
                }
                stats.files_renamed.push((from.clone(), to.clone()));
            }
        }
        let empty = serde_json::Value::Object(serde_json::Map::new());
        let no_validation = crate::Validation::default();

        // Second pass: resolve the commit that last touched every
        // candidate path via a single backwards walk from HEAD.
        let last_commits = git::last_commits_for_paths(&repo, meta.head_oid, &walk_paths)?;
        let mut resolves: std::collections::HashMap<String, Option<i64>> =
            std::collections::HashMap::new();
        let mut docs: Vec<(Candidate<'_>, ParsedDoc<'_>, Option<i64>)> = Vec::new();
        for mut c in candidates {
            let mut parsed_doc = c.parsed_doc.take();
            let record_commit_id = c.last_commit(&last_commits);
            let file_commit_id: Option<&str> = if c.clean { record_commit_id } else { None };
            let resolves_version = record_commit_id.and_then(|commit| {
                *resolves.entry(commit.to_string()).or_insert_with(|| {
                    git::commit_message(&repo, commit)
                        .as_deref()
                        .and_then(resolves_version_from_message)
                })
            });
            if parsed_doc.is_none() && resolves_version.is_some() {
                // "Keep the file as it is and drop the database's edit"
                // is a resolution that changes no bytes, so the
                // unchanged-file skip would swallow it.
                if let (Ok(bytes), Some(syntax)) = (
                    std::fs::read(&c.tf.abs_path),
                    Syntax::for_extension(&extract_ext(&c.tf.rel_path)),
                ) {
                    if let Ok(doc) =
                        self.parse_and_detect(&c.tf.rel_path, syntax, &bytes, &mut stats)
                    {
                        parsed_doc = doc;
                    }
                }
            }
            let Some(doc) = parsed_doc else {
                // The database already holds these bytes, so only the
                // file's commit attribution can be out of date.
                if file_commit_id == c.db_commit.as_deref() {
                    stats.files_unchanged += 1;
                } else {
                    self.set_file_commit(&c.tf.rel_path, file_commit_id).await?;
                    stats.files_updated += 1;
                }
                continue;
            };
            stats.files_updated += 1;
            docs.push((c, doc, resolves_version));
        }

        let mut disk_files: Vec<DiskFile<'_>> = Vec::new();
        for (c, doc, resolves_version) in &docs {
            let record_commit_id = c.last_commit(&last_commits);
            disk_files.push(DiskFile {
                file: ScannedFile {
                    rel_path: &c.tf.rel_path,
                    record_commit_id,
                    file_commit_id: if c.clean { record_commit_id } else { None },
                    source_oid: &c.disk_blob,
                    value: &doc.value,
                    format: doc.format,
                    force: options.force,
                    resolves_version: *resolves_version,
                    validation: &doc.validation,
                },
                gone: false,
            });
        }
        for path in &orphans {
            let row = &known_files[path];
            let Some(format) = self.formats().by_name(&row.format) else {
                // A file whose format the registry no longer knows keeps
                // its records and gets only the removal mark.
                db::file::set_deleted(self.db(), self.worktree_id(), path, true).await?;
                continue;
            };
            tracing::info!(file = path.as_str(), "file is gone from the working tree");
            stats.files_deleted += 1;
            stats.files_updated += 1;
            disk_files.push(DiskFile {
                file: ScannedFile {
                    rel_path: path,
                    record_commit_id: row.commit_id.as_deref(),
                    file_commit_id: row.commit_id.as_deref(),
                    source_oid: row.source_oid.as_deref().unwrap_or_default(),
                    value: &empty,
                    format,
                    force: false,
                    resolves_version: None,
                    validation: &no_validation,
                },
                gone: true,
            });
        }

        on_pool!(self.db(), pool => crate::scan::scan_in_pool(
            self,
            pool,
            head.as_deref(),
            &head_files,
            &renames,
            &disk_files,
            &mut stats,
        ).await)?;
        Ok(stats)
    }

    /// HEAD's blobs, and the files whose blob isn't the one the
    /// committed segments hold, parsed from `head` -- except a file known
    /// to be in no format at that blob, unless `force`.
    pub(crate) fn head_files(
        &self,
        repo: &gix::Repository,
        head: Option<&str>,
        known_files: &std::collections::HashMap<String, crate::model::File>,
        force: bool,
    ) -> Result<HeadSide> {
        let head_blobs: std::collections::HashMap<String, gix::ObjectId> = match head {
            Some(h) => git::tree_blobs(repo, h)?,
            None => Default::default(),
        };
        let mut head_files: Vec<HeadFile> = Vec::new();
        let mut parsed = 0;
        let head_paths: BTreeSet<&String> = head_blobs
            .keys()
            .chain(
                known_files
                    .values()
                    .filter(|f| f.committed_oid.is_some())
                    .map(|f| &f.path),
            )
            .collect();
        for path in head_paths {
            let Some(syntax) = Syntax::for_extension(&extract_ext(path)) else {
                continue;
            };
            let blob = head_blobs.get(path).copied();
            let now = blob.map(|o| o.to_string());
            let known = known_files.get(path);
            if now == known.and_then(|f| f.committed_oid.clone()) {
                continue;
            }
            let skip = |blob| known.is_none() && !force && self.unrecognized().contains(path, blob);
            if blob.is_some_and(skip) {
                continue;
            }
            let bytes = match &now {
                Some(oid) => Some(git::read_blob(repo, oid)?),
                None => None,
            };
            // HEAD's content is reported where the working tree's is; a
            // commit that no format claims or that doesn't parse holds no
            // records the committed segments can take.
            let mut ignored = SyncOutcome::default();
            let detected = bytes
                .as_ref()
                .map(|bytes| self.parse_and_detect(path, syntax, bytes, &mut ignored));
            parsed += ignored.files_parsed;
            // only a document no format claims; one that doesn't parse is
            // the working tree's to report
            if let (Some(Ok(None)), None, Some(blob)) = (&detected, known, blob) {
                self.unrecognized().head.insert(path.clone(), blob);
            }
            let doc = detected.and_then(|d| d.ok().flatten());
            let format = match (&doc, known) {
                (Some(doc), _) => doc.format.name().to_string(),
                (None, Some(f)) => f.format.clone(),
                (None, None) => continue,
            };
            head_files.push(HeadFile::new(path.clone(), now, doc.as_ref(), &format));
        }
        Ok(HeadSide {
            blobs: head_blobs,
            files: head_files,
            parsed,
        })
    }

    /// Point a file row at the commit carrying its bytes, `None` while
    /// they're uncommitted.
    async fn set_file_commit(&self, file_path: &str, commit: Option<&str>) -> Result<()> {
        let w = self.worktree_id();
        on_pool!(self.db(), pool => {
            let mut tx = pool.begin().await?;
            if let Some(f) = crate::db::store::Store::files(&mut tx, w, Some(file_path)).await?.pop() {
                crate::db::store::Store::upsert_file(&mut tx, w, &crate::db::tables::FileRow {
                    path: file_path,
                    format: &f.format,
                    commit_id: Some(commit),
                    source_oid: Some(f.source_oid.as_deref()),
                    committed_oid: None,
                }).await?;
            }
            tx.commit().await?;
            Ok::<(), Error>(())
        })
    }

    /// Parse `bytes` and classify the document via the registry.
    /// `Ok(None)` when no format claims it — the file is not one of
    /// ours and the scan moves on.
    pub(crate) fn parse_and_detect(
        &self,
        rel_path: &str,
        syntax: Syntax,
        bytes: &[u8],
        stats: &mut SyncOutcome,
    ) -> Result<Option<ParsedDoc<'_>>> {
        stats.files_parsed += 1;
        parse_and_detect(self.formats(), rel_path, syntax, bytes, stats)
    }

    fn unrecognized(&self) -> std::sync::MutexGuard<'_, Unrecognized> {
        self.inner
            .unrecognized
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    fn walked_head(&self) -> std::sync::MutexGuard<'_, Option<gix::ObjectId>> {
        self.inner
            .walked_head
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// Search records by optional `file_path` / `path` / `key` filters.
    ///
    /// All `Some(...)` filters are AND'd together; `None` matches any
    /// value. With `alias = true` and a `key` filter, a record also
    /// matches when one of its aliases, which
    /// [`crate::DataFormat::find_alias`] gives it, has that key. Without a `key` filter, `alias` is a
    /// no-op. Tombstoned records are hidden.
    ///
    /// `type_names`, when set and non-empty, restricts results to
    /// records whose JSON payload declares one of the given names as a
    /// key of its `type` object (the cloudmap `typeRef` shape); with
    /// `subtypes`, also every subtype of those names per the `extends`
    /// lists of the `/types` section.
    ///
    /// Results are ordered by `(path, key)`, compared byte-wise, for
    /// stable output.
    ///
    /// `after`, when set, is an *exclusive* lower bound on that
    /// ordering: only records ordered strictly after the given
    /// `(path, key)` are returned. `limit` caps how many come back.
    /// Together they page a scan without an offset, so concurrent
    /// writes can't make a page skip or repeat a record — and because
    /// the bound is a value rather than a row reference, deleting the
    /// record `after` names doesn't strand the walk. Note that
    /// `(path, key)` is only unique within one file: a query spanning
    /// several (`file_path = None`) can hold two records with the same
    /// pair, and a page boundary between them keeps just the later one.
    pub async fn find_records(&self, query: &RecordQuery) -> Result<Vec<Record>> {
        let mut rows = db::record::find(self.db(), self.worktree_id(), query).await?;
        // A page that stopped exactly on `limit` may have cut a
        // `(path, key)` group in half; ask for the remainder. The
        // follow-up returns nothing when the boundary was already clean,
        // so this costs one bounded query on a full page and nothing on
        // a short one.
        if query.whole_groups && query.limit.is_some_and(|n| rows.len() as i64 == n) {
            if let Some(last) = rows.last().cloned() {
                let rest = db::record::find(
                    self.db(),
                    self.worktree_id(),
                    &RecordQuery {
                        path: Some(last.path.clone()),
                        key: Some(last.key.clone()),
                        // Exact key: an alias OR-clause would widen this
                        // past the group being completed.
                        alias: false,
                        after: Some(crate::model::Cursor {
                            path: last.path.clone(),
                            key: last.key.clone(),
                            file_path: Some(last.file_path.clone()),
                            worktree_id: Some(last.worktree_id),
                        }),
                        limit: None,
                        whole_groups: false,
                        ..query.clone()
                    },
                )
                .await?;
                rows.extend(rest);
            }
        }
        Ok(rows)
    }

    /// Facet aggregation over the records matching `query`: distinct
    /// records counted per value at `spec.group`, plus a breakdown per
    /// facet column -- see [`crate::FacetSpec`].
    ///
    /// Shares [`Self::find_records`]'s filters (`path`, `type_names`,
    /// `json_queries`, …); `after` / `limit` are ignored, an aggregation
    /// having nothing to page. Values come back as extracted -- key
    /// rendering, canonicalization and merging of spelling variants
    /// (sqlite groups object values by their stored key order) are the
    /// caller's business.
    pub async fn facet_records(
        &self,
        query: &RecordQuery,
        spec: &crate::model::FacetSpec,
    ) -> Result<crate::model::FacetRows> {
        db::record::facet(self.db(), self.worktree_id(), query, spec).await
    }

    /// Like [`Self::find_records`], but also walks
    /// [`crate::DataFormat::follow`] outgoing edges from each match.
    ///
    /// Performs a breadth-first traversal starting from the initial
    /// match set, following `(path, key)` references returned by each
    /// record's [`crate::DataFormat`]. Returns at most `follow` newly
    /// discovered records, alias-resolved (so versioned URLs hit
    /// their canonical record). The starting set is never re-emitted
    /// in the followed set, and `(path, key)` duplicates are
    /// suppressed.
    ///
    /// Returns `(initial, followed)` where `initial` is what
    /// [`Self::find_records`] would have returned for the same
    /// filters, and `followed` is the breadth-first frontier capped
    /// at `follow` entries. `follow == 0` returns an empty followed
    /// set.
    ///
    /// `type_names` filters the **initial** set only (see
    /// [`Self::find_records`]); the follow walk traverses edges from
    /// those matches without re-applying the type filter, so the
    /// followed set stays a complete neighborhood of the starting
    /// records.
    pub async fn find_records_follow(
        &self,
        query: &RecordQuery,
        follow: u32,
        exclude: Vec<i64>,
    ) -> Result<(Vec<Record>, Vec<Record>)> {
        let initial = self.find_records(query).await?;
        let followed = self
            .follow_records(
                &initial,
                follow,
                exclude,
                query.since_version,
                query.worktrees.as_ref(),
            )
            .await?;
        Ok((initial, followed))
    }

    /// The follow walk of [`Self::find_records_follow`], over records the
    /// caller already holds.
    ///
    /// Returns at most `follow` newly discovered records, alias-resolved,
    /// never re-emitting anything in `initial` or named by `exclude`.
    pub async fn follow_records(
        &self,
        initial: &[Record],
        follow: u32,
        exclude: Vec<i64>,
        since_version: Option<i64>,
        worktrees: Option<&crate::model::WorktreeFilter>,
    ) -> Result<Vec<Record>> {
        // Soft cap on the size of a single batched `key IN (...)`
        // query. Each follow batch binds `2 * keys` parameters (the
        // alias OR-clause re-binds the same set), so 100 keys → 200
        // bindings — newer SQLite has a 32766 cap (we require ≥ 3.45 for JSONB anyway).
        const MAX_BATCH_KEYS: usize = 100;
        // Hard cap on `exclude.len() + |visited_ids|`. Each id binds one parameter
        const MAX_EXCLUDE_IDS: usize = 10000;

        if exclude.len() > MAX_EXCLUDE_IDS {
            return Err(Error::Other(format!(
                "follow_records: exclude list too large \
                 ({} > {MAX_EXCLUDE_IDS}); shrink the caller's cache \
                 or split the walk",
                exclude.len()
            )));
        }
        if follow == 0 || initial.is_empty() {
            return Ok(Vec::new());
        }

        // Memoize file_path → format-name so we don't query `file` once
        // per visited record.
        let mut format_cache: std::collections::HashMap<(i64, String), Option<String>> =
            std::collections::HashMap::new();

        // (path, key) tracking dedupes the followed Vec; id tracking
        // becomes the SQL `id NOT IN (...)` predicate so the database
        // skips records the walker has already emitted (or that the
        // caller pre-excluded).
        let mut visited: BTreeSet<(String, String)> = initial
            .iter()
            .map(|r| (r.path.clone(), r.key.clone()))
            .collect();
        let mut visited_ids: BTreeSet<i64> = exclude.into_iter().collect();
        for r in initial {
            visited_ids.insert(r.id);
        }
        let mut queue: std::collections::VecDeque<Record> = initial.iter().cloned().collect();
        let mut followed: Vec<Record> = Vec::new();
        let mut batch_keys: Vec<String> = Vec::new();

        // Outer loop: accumulate follow keys from the queue into
        // `batch_keys` and flush in one query whenever the buffer
        // crosses MAX_BATCH_KEYS (or the queue runs dry). Compared
        // to the original per-key query, this collapses many records'
        // `key = ?` lookups into a single `key IN (...)` query; the
        // `id NOT IN (...)` predicate keeps already-visited records
        // out of every result set.
        loop {
            while let Some(rec) = queue.pop_front() {
                // the record's own worktree: the walk may span several
                let file = (rec.worktree_id, rec.file_path.clone());
                let format_name = match format_cache.get(&file) {
                    Some(v) => v.clone(),
                    None => {
                        let f = db::file::get(self.db(), rec.worktree_id, &rec.file_path).await?;
                        let name = f.map(|f| f.format);
                        format_cache.insert(file, name.clone());
                        name
                    }
                };
                let Some(name) = format_name else {
                    continue;
                };
                let Some(fmt) = self.formats().by_name(&name) else {
                    continue;
                };
                for key in fmt.follow(&rec) {
                    batch_keys.push(key);
                }
                if batch_keys.len() >= MAX_BATCH_KEYS {
                    break;
                }
            }
            if batch_keys.is_empty() {
                break;
            }

            // visited_ids grows as the walk discovers records, so
            // re-check the cap against the live set before each
            // flush. Unlike the entry-time check (which is the
            // caller's mistake), exceeding here is just the walk
            // outgrowing the SQL parameter budget — stop following
            // and return what we've collected so far.
            if visited_ids.len() > MAX_EXCLUDE_IDS * 2 {
                break;
            }

            let key_refs: Vec<&str> = batch_keys.iter().map(|s| s.as_str()).collect();
            let exclude_ids: Vec<i64> = visited_ids.iter().copied().collect();
            let hits = db::record::find_many(
                self.db(),
                self.worktree_id(),
                worktrees,
                &key_refs,
                true,
                &exclude_ids,
                since_version,
            )
            .await?;
            batch_keys.clear();

            for r in hits {
                let pair = (r.path.clone(), r.key.clone());
                if !visited.insert(pair) {
                    continue;
                }
                visited_ids.insert(r.id);
                queue.push_back(r.clone());
                followed.push(r);
                if (followed.len() as u32) >= follow {
                    return Ok(followed);
                }
            }
        }
        Ok(followed)
    }

    /// Returns the record at `(file_path, path, key)` within this
    /// worktree, or `None` if absent or tombstoned.
    pub async fn get_record(
        &self,
        file_path: &str,
        path: &str,
        key: &str,
    ) -> Result<Option<Record>> {
        db::record::get(self.db(), self.worktree_id(), file_path, path, key).await
    }

    /// Returns the record with the given `key_id`, as this worktree sees it.
    ///
    /// Unlike [`Self::get_record`], this does **not** hide tombstoned
    /// rows — useful for tests and for inspecting the in-flight delete
    /// state.
    pub async fn get_record_by_id(&self, id: i64) -> Result<Option<Record>> {
        db::record::get_by_id(self.db(), self.worktree_id(), id).await
    }

    /// Returns the [`crate::model::File`] row for `file_path` within
    /// this worktree, or `None` if no row exists.
    pub async fn get_file(&self, file_path: &str) -> Result<Option<crate::model::File>> {
        db::file::get(self.db(), self.worktree_id(), file_path).await
    }

    /// Every worktree in the database `filter` matches.
    pub async fn worktrees(
        &self,
        filter: &crate::model::WorktreeFilter,
    ) -> Result<Vec<crate::model::Worktree>> {
        self.db().worktrees(filter).await
    }

    /// Returns the [`crate::model::Worktree`] row this `SyncedRepo` is
    /// bound to.
    pub async fn get_worktree(&self) -> Result<crate::model::Worktree> {
        db::worktree::get(self.db(), self.worktree_id()).await
    }

    /// Override (or clear) `worktree.default_file_path`.
    ///
    /// Pass `Some(path)` to pin a value, `None` to clear it. The
    /// auto-pick in [`Self::update_from_working_dir`] only runs when
    /// the column is `NULL`, so pinning a value protects it from
    /// later re-syncs.
    pub async fn set_default_file_path(&self, value: Option<&str>) -> Result<()> {
        db::worktree::set_default_file(self.db(), self.worktree_id(), value).await
    }

    /// List changes within this worktree.
    ///
    /// `since == Some(v)` returns every record whose
    /// [`Record::version`] is strictly greater than `v` — both
    /// committed and in-flight, including tombstones — in version
    /// order. Pass the largest version your caller has previously
    /// observed to receive only what has changed since.
    ///
    /// `since == None` returns only the in-flight records
    /// (`commit_id IS NULL`) — i.e. exactly what
    /// [`Self::commit_repository`] would write next, again in version
    /// order. Equivalent to "give me the pending work-list."
    ///
    /// Tombstones (`deleted == true`) are returned in both modes so
    /// callers can tell apart "still here" from "in-flight delete."
    ///
    /// `include_conflicts` adds the file's side of contested records
    /// (see [`Self::list_conflicts`]). They draw versions like any other
    /// row, so a poller catching up from a watermark learns of a
    /// conflict as soon as it is materialized — but only when it asks,
    /// since a caller tracking record state would otherwise see a
    /// contested record twice and have no reason to expect it.
    ///
    /// A `since` below the worktree's `reset_version` fails with
    /// [`Error::Reset`]: a rebuild dropped records from the view without
    /// tombstones, so the caller re-reads it whole and resumes from
    /// [`Self::watermark`].
    pub async fn list_changes(
        &self,
        since: Option<i64>,
        include_conflicts: bool,
    ) -> Result<Vec<Record>> {
        db::record::list_changes(self.db(), self.worktree_id(), since, include_conflicts).await
    }

    /// Delete this worktree from the database: its records, draft and
    /// files, and the segments only it used (§4.13). The checkout on disk
    /// is left alone, and opening it again starts afresh. A family's root
    /// can't be deleted while other worktrees belong to it:
    /// [`Error::FamilyInUse`].
    ///
    /// Drop this handle's other clones first: they still name the deleted
    /// worktree, so every call through one fails.
    pub async fn delete_worktree(self) -> Result<()> {
        crate::fork::delete(self.db(), self.worktree_id(), self.family_id()).await
    }

    /// The highest version written so far to the worktrees `worktrees`
    /// selects, or to this one: a `since_version` for the next read that
    /// misses nothing written after this call. Read it before the read it
    /// covers. Fails with [`Error::CursorAcrossFamilies`] when the
    /// worktrees span version families.
    pub async fn watermark(&self, worktrees: Option<&crate::model::WorktreeFilter>) -> Result<i64> {
        db::record::watermark(self.db(), self.worktree_id(), worktrees).await
    }

    /// Insert a new record at `(file_path, path, key)`.
    ///
    /// If a tombstoned row already exists at that location, it is
    /// resurrected with the new value. Live (non-tombstoned) rows
    /// cause [`crate::Error::AlreadyExists`].
    ///
    /// `expected_commit` is the optimistic-concurrency token. When
    /// the record row is absent, the check is performed against the
    /// containing file's `commit_id`. See [`CommitRef`] for the
    /// semantics. The conflict check + INSERT + alias refresh run in
    /// a single sqlx transaction; on mismatch the transaction rolls
    /// back and [`crate::Error::Conflict`] is returned.
    ///
    /// Returns the new record's [`WriteOutcome`] (primary key + the
    /// monotonic version stamped on this write).
    ///
    /// `file_path == None` resolves to the worktree's
    /// `default_file_path` (set on the first
    /// [`Self::update_from_working_dir`] run); errors with
    /// [`crate::Error::NotFound`] when the default is unset.
    ///
    /// `resolve` settles any conflict on the record: the client has
    /// seen the file's side (via [`Self::list_conflicts`]) and is
    /// choosing against it, so the conflict row goes with this write.
    /// Passing `false` writes and leaves the conflict standing, which
    /// is what an ordinary client that never looked should do -- a
    /// write cannot discard the file's value by accident. Harmless on a
    /// record with no conflict.
    pub async fn create_record(
        &self,
        file_path: Option<&str>,
        path: &str,
        key: &str,
        json: serde_json::Value,
        expected_commit: Option<CommitRef>,
        resolve: bool,
    ) -> Result<WriteOutcome> {
        // Dispatch on the concrete pool type; the body is shared via the
        // generic `crud_create_in_pool` (same pattern as `apply_batch`).
        on_pool!(self.db(), pool => crud_create_in_pool(
                    self,
                    pool,
                    WriteTarget {
                        file_path,
                        path,
                        key,
                    },
                    json,
                    expected_commit,
                    resolve,
                )
                .await)
    }

    /// Replace an existing record's JSON.
    ///
    /// Fails with [`crate::Error::NotFound`] if the row is absent or
    /// tombstoned. `expected_commit` is the optimistic-concurrency
    /// token; see [`CommitRef`]. Sets the row's `commit_id` back to
    /// `NULL` (in-flight). Returns the row's [`WriteOutcome`]
    /// (primary key + the new monotonic version).
    ///
    /// `file_path == None` resolves to the existing record's own
    /// `file_path`. Errors with [`crate::Error::NotFound`] when no
    /// record matches `(path, key)`.
    ///
    /// `resolve` settles any conflict on the record: the client has
    /// seen the file's side (via [`Self::list_conflicts`]) and is
    /// choosing against it, so the conflict row goes with this write.
    /// Passing `false` writes and leaves the conflict standing, which
    /// is what an ordinary client that never looked should do -- a
    /// write cannot discard the file's value by accident. Harmless on a
    /// record with no conflict.
    pub async fn update_record(
        &self,
        file_path: Option<&str>,
        path: &str,
        key: &str,
        json: serde_json::Value,
        expected_commit: Option<CommitRef>,
        resolve: bool,
    ) -> Result<WriteOutcome> {
        on_pool!(self.db(), pool => crud_update_in_pool(
                    self,
                    pool,
                    WriteTarget {
                        file_path,
                        path,
                        key,
                    },
                    json,
                    expected_commit,
                    resolve,
                )
                .await)
    }

    /// Insert-or-replace a record.
    ///
    /// Behaves like [`Self::create_record`] when the row is absent,
    /// and like [`Self::update_record`] when it's present (live or
    /// tombstoned). `expected_commit` is checked the same way as in
    /// the other CRUD methods.
    ///
    /// `file_path == None` resolves to the existing record's
    /// `file_path` when one matches `(path, key)`, falling back to
    /// the worktree's `default_file_path` for new records. Errors
    /// when the record is new and no default is set.
    ///
    /// `resolve` settles any conflict on the record: the client has
    /// seen the file's side (via [`Self::list_conflicts`]) and is
    /// choosing against it, so the conflict row goes with this write.
    /// Passing `false` writes and leaves the conflict standing, which
    /// is what an ordinary client that never looked should do -- a
    /// write cannot discard the file's value by accident. Harmless on a
    /// record with no conflict.
    pub async fn upsert_record(
        &self,
        file_path: Option<&str>,
        path: &str,
        key: &str,
        json: serde_json::Value,
        expected_commit: Option<CommitRef>,
        resolve: bool,
    ) -> Result<WriteOutcome> {
        on_pool!(self.db(), pool => crud_upsert_in_pool(
                    self,
                    pool,
                    WriteTarget {
                        file_path,
                        path,
                        key,
                    },
                    json,
                    expected_commit,
                    resolve,
                )
                .await)
    }

    /// Tombstone the record at `(file_path, path, key)` and clear its
    /// aliases.
    ///
    /// The row stays in the database (marked `deleted = TRUE`,
    /// `commit_id = NULL`) until the next [`Self::commit_repository`]
    /// purges it. Reads via [`Self::get_record`] /
    /// [`Self::find_records`] hide tombstones.
    ///
    /// Fails with [`crate::Error::NotFound`] if the row is absent or
    /// already tombstoned.
    ///
    /// `file_path == None` resolves to the existing record's own
    /// `file_path`.
    ///
    /// `resolve` settles any conflict on the record: the client has
    /// seen the file's side (via [`Self::list_conflicts`]) and is
    /// choosing against it, so the conflict row goes with this write.
    /// Passing `false` writes and leaves the conflict standing, which
    /// is what an ordinary client that never looked should do -- a
    /// write cannot discard the file's value by accident. Harmless on a
    /// record with no conflict.
    pub async fn delete_record(
        &self,
        file_path: Option<&str>,
        path: &str,
        key: &str,
        expected_commit: Option<CommitRef>,
        resolve: bool,
    ) -> Result<WriteOutcome> {
        on_pool!(self.db(), pool => crud_delete_in_pool(
                    self,
                    pool,
                    WriteTarget {
                        file_path,
                        path,
                        key,
                    },
                    expected_commit,
                    resolve,
                )
                .await)
    }

    /// Remove a whole file from the worktree.
    ///
    /// Tombstones the file row and every live record in it, in one
    /// transaction. Nothing touches the disk yet: the next
    /// [`Self::save_changes`] removes the file, and the next
    /// [`Self::commit_repository`] stages that removal and drops the
    /// rows. Until then [`Self::list_changes`] shows the tombstones,
    /// which is what a delete of this size ought to look like -- it is
    /// the record deletions that the commit actually carries.
    ///
    /// Writing a record back into the file un-deletes it: a file with a
    /// record in it plainly exists, so the two states cannot both hold
    /// and the write is the more recent statement of intent.
    ///
    /// `expected_commit` is the optimistic-concurrency token, checked
    /// against the file rather than any one record:
    /// [`CommitRef::Commit`] must match the file row's `commit_id`, and
    /// [`CommitRef::Pending`] requires that nothing in the file has been
    /// written since that version. Deleting a file is a claim about all
    /// of it, so a per-record check would let a concurrent write to a
    /// record the caller never saw slip through.
    ///
    /// # Errors
    ///
    /// [`crate::Error::NotFound`] when the worktree has no such file,
    /// [`crate::Error::Conflict`] on a failed token check.
    pub async fn delete_file(
        &self,
        file_path: &str,
        expected_commit: Option<CommitRef>,
    ) -> Result<Vec<WriteOutcome>> {
        on_pool!(self.db(), pool => delete_file_in_pool(self, pool, file_path, expected_commit).await)
    }

    /// Apply a batch of [`BatchOp`]s under a single SQL transaction.
    ///
    /// In **atomic** mode (`atomic == true`), the first per-record
    /// [`Error::Conflict`] / [`Error::NotFound`] aborts the batch: the
    /// surrounding transaction is rolled back, the offending op is
    /// returned in [`BatchOutcome::failed`], and
    /// [`BatchOutcome::applied`] is empty.
    ///
    /// In **non-atomic** mode (`atomic == false`), per-record
    /// [`Error::Conflict`] / [`Error::NotFound`] are caught: the op
    /// is recorded in [`BatchOutcome::failed`] and the loop continues.
    /// All non-failing ops commit together at the end of the batch.
    ///
    /// Other (non-CRUD) errors — I/O, malformed JSON, SQL backend
    /// failures — always abort and propagate as `Err`.
    ///
    /// `meta` opts the batch into the `txn` audit table: a row naming
    /// the author, the message, and the version range this batch
    /// stamped is inserted in the same transaction, so it lands only if
    /// the batch does. A batch that applied nothing (empty, or fully
    /// rolled back) records no row. [`Self::commit_repository`] later
    /// reports these rows in the body of the git commit message. Pass
    /// `None` to write without an audit trail.
    pub async fn apply_batch(
        &self,
        ops: Vec<BatchOp>,
        atomic: bool,
        meta: Option<TxnMeta>,
    ) -> Result<BatchOutcome> {
        on_pool!(self.db(), pool => apply_batch_inner(self, pool, ops, atomic, false, meta).await)
    }

    /// [`Self::apply_batch`], with each written record checked by its
    /// format's [`crate::DataFormat::validate_record`] before it's stored.
    ///
    /// An invalid record fails the whole batch with [`Error::Invalid`] and
    /// rolls it back, atomic or not: it's a fault in the request, not in
    /// one record's race with another writer.
    pub async fn apply_batch_checked(
        &self,
        ops: Vec<BatchOp>,
        atomic: bool,
        meta: Option<TxnMeta>,
    ) -> Result<BatchOutcome> {
        on_pool!(self.db(), pool => apply_batch_inner(self, pool, ops, atomic, true, meta).await)
    }

    /// Every `txn` audit row of this worktree, oldest version range
    /// first — both outstanding batches and ones already carried into a
    /// commit (distinguished by [`Txn::commit_id`]).
    pub async fn list_transactions(&self) -> Result<Vec<Txn>> {
        db::commit::list_all(self.db(), self.worktree_id()).await
    }

    /// Persist every in-flight record edit to disk.
    ///
    /// For each dirty file — one with a record whose `commit_id IS
    /// NULL`, or whose own content is not yet in git — calls
    /// [`Self::write_file`]. Files whose bytes turn out to match what
    /// is already on disk are skipped, which is also what happens for a
    /// hand-edited file the scan took in: nothing is pending for it, so
    /// there is nothing to write and it merely awaits commit.
    ///
    /// A failure on one file does not stop the others, and does not
    /// discard what already succeeded: each is reported in
    /// [`SyncOutcome::failed`] while the rest carry on. Failing fast
    /// would leave earlier files rewritten on disk with nothing in the
    /// return value saying so — the error would name one file and the
    /// caller would have no way to learn about the others.
    ///
    /// **Warning**: writing a stale file — one edited on disk since
    /// its last take-in — merges the pending records the edit did not
    /// touch and leaves the rest alone, recording each collision as a
    /// conflict row and reporting it in [`SyncOutcome::conflicts`]. The
    /// records themselves are **not** updated and `source_oid` keeps
    /// naming the old bytes, so the file's rows go on serving pre-edit
    /// values until the next [`Self::update_from_working_dir`] or
    /// [`Self::commit_repository`] (which scans first) takes the file
    /// in. A save is not how the database learns what a hand edit
    /// said.
    ///
    /// # Errors
    ///
    /// Only for failures that prevent the attempt entirely, such as the
    /// database being unreachable. A per-file failure is data, not an
    /// error.
    pub async fn save_changes(&self) -> Result<SyncOutcome> {
        let dirty: Vec<String> =
            db::record::list_dirty_files(self.db(), self.worktree_id()).await?;
        let mut outcome = SyncOutcome::default();
        for fp in dirty {
            match self.write_file(&fp).await {
                Ok(res) => {
                    outcome.files_deleted += usize::from(res.deleted && res.written.is_some());
                    outcome.written.extend(res.written);
                    outcome.conflicts.extend(res.conflicts);
                }
                Err(error) => outcome.failed.push(crate::model::FileFailure {
                    file_path: fp,
                    error,
                }),
            }
        }
        Ok(outcome)
    }

    /// Apply pending record changes for `file_path` to the on-disk
    /// file in place.
    ///
    /// Reads the file, parses it (YAML or JSON, by extension), then
    /// applies each in-flight (`commit_id IS NULL`) record:
    ///
    /// - Non-tombstone rows are written at `obj[trim(path)][key]`,
    ///   creating the section object if missing.
    /// - Tombstones (`deleted = TRUE`) remove `obj[trim(path)][key]`.
    /// - A section that becomes empty is removed entirely.
    ///
    /// Untouched keys keep their original position and value, so the
    /// rewrite is "minimal": only the diff is reflected. If the file
    /// doesn't exist on disk, a fresh document is synthesised from
    /// the non-tombstone records (tombstones are no-ops in that case).
    ///
    /// Returns [`WriteFileOutcome::written`]` = Some(path)` when the
    /// on-disk bytes changed, `None` when the freshly-rendered output
    /// matches what was already on disk.
    ///
    /// The render reads the *live* on-disk document, so a file that was
    /// hand-edited since its last take-in still comes out merged: the
    /// disk's changes to other records survive. Where both sides touched
    /// the same record, neither is overwritten — the record is skipped,
    /// the file keeps its value, and the divergence is materialized as a
    /// conflict row (see [`crate::ConflictState`]) and reported in
    /// [`WriteFileOutcome::conflicts`] until
    /// [`Self::resolve_conflict`] settles it.
    ///
    /// Writing a stale file deliberately leaves `source_oid` naming the
    /// old bytes: the file now holds a hand edit to records this
    /// database never took in, so the next
    /// [`Self::update_from_working_dir`] (or [`Self::commit_repository`],
    /// which scans first) has to see the mismatch and take the merged
    /// file in. Until then the file's rows serve pre-edit values for
    /// those records.
    ///
    /// # Errors
    ///
    /// [`crate::Error::Yaml`] / [`crate::Error::Json`] on parse / emit
    /// failure; [`crate::Error::Io`] for filesystem failures.
    pub async fn write_file(&self, file_path: &str) -> Result<WriteFileOutcome> {
        self.write_file_racing(file_path, async || Ok(())).await
    }

    /// [`Self::write_file`], running `before_persist` between each render
    /// and its write: where another writer would change what it read.
    async fn write_file_racing(
        &self,
        file_path: &str,
        mut before_persist: impl AsyncFnMut() -> Result<()>,
    ) -> Result<WriteFileOutcome> {
        for _ in 0..WRITE_ATTEMPTS {
            let render = match self.render_file(file_path).await? {
                Render::Done(outcome) => return Ok(outcome),
                render => render,
            };
            before_persist().await?;
            let persisted = match render {
                Render::Write(rendered) => self.persist_render(file_path, rendered).await?,
                Render::Remove(removal) => self.persist_removal(file_path, removal).await?,
                Render::Record(bookkeeping) => {
                    self.persist_bookkeeping(file_path, bookkeeping).await?
                }
                Render::Done(outcome) => Some(outcome),
            };
            if let Some(outcome) = persisted {
                return Ok(outcome);
            }
        }
        Err(Error::FileChanged {
            file_path: file_path.to_string(),
        })
    }

    /// [`Self::write_file`] up to the rename: what it would write, or what
    /// it did when there was nothing to.
    async fn render_file(&self, file_path: &str) -> Result<Render> {
        // Read before the disk is, so a write landing in between is caught
        // by `write_seq` rather than paired with the newer bytes.
        let db::record::RenderInputs {
            file: file_row,
            write_seq,
            live,
            // Divergences already on record: the file's side of these
            // stands whether or not the file has moved since.
            conflicts: conflict_rows,
            pending,
            bases,
            base_values,
        } = db::record::render_inputs(self.db(), self.worktree_id(), file_path).await?;
        let abs_path = self.inner.repo_path.join(file_path);

        // A deleted file comes off the disk when nothing is left in it, and
        // the deletion is retracted when something is: a record written
        // back, or one contested. Decided before the pending-is-empty exit
        // below, because a deletion whose tombstones have already been
        // purged leaves nothing pending and would otherwise never reach
        // the disk.
        let deleted = file_row.as_ref().is_some_and(|f| f.deleted);
        if deleted && !live && conflict_rows.is_empty() {
            return Ok(Render::Remove(Removal {
                abs_path,
                write_seq,
            }));
        }
        let retract = deleted;

        if pending.is_empty() {
            return Ok(Render::record(Bookkeeping {
                write_seq,
                retract,
                commit_id: None,
                ops: Vec::new(),
                outcome: WriteFileOutcome::default(),
            }));
        }

        let format = pending
            .iter()
            .find_map(|rec| self.formats().for_path(&rec.path));

        let syntax = Syntax::for_extension(&extract_ext(file_path))
            .ok_or_else(|| Error::Other(format!("{file_path}: unsupported file extension")))?;

        // A stale source means the disk holds an edit this database
        // never took in — the apply below must check each pending
        // record against its base for collisions with that edit.
        let stale = self.source_stale(file_row.as_ref(), &abs_path)?;
        let check = stale.then_some(ConflictCheck { base_values });

        // One read answers both questions the write has about the file:
        // the bytes a splice keeps, and whether there is a document here
        // at all. Asking the filesystem twice can answer them
        // differently -- a file that appears between a stat and a read
        // would have the format header written over its contents.
        let source = match std::fs::read_to_string(&abs_path) {
            Ok(text) => Some(text),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
            Err(e) => return Err(Error::Io(e)),
        };
        // A literate document is human-authored: there is no prose to
        // invent, and the front matter naming its format has to come
        // from somewhere. Refuse rather than write records into a file
        // the next scan would not recognise.
        if syntax == Syntax::Markdown && source.is_none() {
            return Err(Error::Other(format!(
                "{file_path}: cannot create a literate markdown document; \
                 add it with `literate-yaml` front matter first"
            )));
        }
        let mut root = match source.as_deref() {
            Some(text) => syntax.into_value(file_path, text.as_bytes())?,
            None => new_root(format),
        };
        let Applying {
            touched,
            conflicts,
            ops,
            applied,
        } = apply_pending_records(
            &mut root,
            file_path,
            pending,
            format,
            &bases,
            &conflict_rows,
            check.as_ref(),
        );
        let commit_id = file_row.as_ref().and_then(|f| f.commit_id.clone());
        if touched.is_empty() {
            // Conflict bookkeeping lands even when nothing is written: the
            // divergences it records are why there is nothing to write.
            return Ok(Render::record(Bookkeeping {
                write_seq,
                retract,
                commit_id,
                ops,
                outcome: WriteFileOutcome {
                    written: None,
                    deleted: false,
                    conflicts,
                },
            }));
        }
        let render = |source: Option<&str>, root: &mut serde_json::Value| {
            render_bytes(file_path, syntax, source, root, format, &applied, &touched)
        };
        let bytes = render(source.as_deref(), &mut root)?;
        let bytes = self.unless_head_has(file_path, syntax, bytes, render)?;
        Ok(Render::Write(Rendered {
            abs_path,
            bytes,
            stale,
            write_seq,
            format: format.map(|f| f.name().to_string()),
            retract,
            commit_id,
            ops,
            conflicts,
        }))
    }

    /// `pending`, edits of `file_path`, applied to `source`, its bytes at
    /// an export's base (`None`: it isn't there): every one, none checked
    /// against a base or held back by a conflict. `None` when they change
    /// nothing in it.
    pub(crate) fn render_onto(
        &self,
        file_path: &str,
        source: Option<&[u8]>,
        pending: Vec<Record>,
    ) -> Result<Option<Vec<u8>>> {
        let syntax = Syntax::for_extension(&extract_ext(file_path))
            .ok_or_else(|| Error::Other(format!("{file_path}: unsupported file extension")))?;
        let format = pending
            .iter()
            .find_map(|rec| self.formats().for_path(&rec.path));
        let text = source
            .map(|b| {
                std::str::from_utf8(b)
                    .map_err(|e| Error::Other(format!("{file_path}: not UTF-8: {e}")))
            })
            .transpose()?;
        if syntax == Syntax::Markdown && text.is_none() {
            return Err(Error::Other(format!(
                "{file_path}: cannot create a literate markdown document; \
                 add it with `literate-yaml` front matter first"
            )));
        }
        let mut root = match text {
            Some(t) => syntax.into_value(file_path, t.as_bytes())?,
            None => new_root(format),
        };
        let no_rows = std::collections::HashMap::new();
        let Applying {
            touched, applied, ..
        } = apply_pending_records(
            &mut root,
            file_path,
            pending,
            format,
            &std::collections::HashMap::new(),
            &no_rows,
            None,
        );
        if touched.is_empty() {
            return Ok(None);
        }
        render_bytes(
            file_path, syntax, text, &mut root, format, &applied, &touched,
        )
        .map(Some)
    }

    /// `bytes`, or HEAD's copy of `file_path` where `render`ing HEAD's own
    /// document gives `bytes`: the file on disk then differs from HEAD in
    /// nothing its records don't carry, so a file whose records are back
    /// where HEAD has them isn't committed for its layout alone, and
    /// nothing only on disk, a comment or prose, is lost.
    fn unless_head_has(
        &self,
        file_path: &str,
        syntax: Syntax,
        bytes: Vec<u8>,
        render: impl Fn(Option<&str>, &mut serde_json::Value) -> Result<Vec<u8>>,
    ) -> Result<Vec<u8>> {
        let repo = self.repo()?;
        let Some(head) = git::worktree_meta(&repo)?.head_oid else {
            return Ok(bytes);
        };
        let Some(committed) = git::read_blob_at_commit(&repo, &head.to_string(), file_path)? else {
            return Ok(bytes);
        };
        let (Ok(text), Ok(mut root)) = (
            std::str::from_utf8(&committed),
            syntax.into_value(file_path, &committed),
        ) else {
            return Ok(bytes);
        };
        // an empty section holds no record, and a save drops the one it
        // empties
        if let Some(sections) = root.as_object_mut() {
            sections.retain(|_, v| v.as_object().is_none_or(|o| !o.is_empty()));
        }
        let same = committed != bytes && render(Some(text), &mut root).is_ok_and(|b| b == bytes);
        Ok(if same { committed } else { bytes })
    }

    /// Write `rendered` to `file_path`, unless another writer has since
    /// changed what its render decides: then `None`, and nothing is
    /// written.
    ///
    /// `source_oid` is stamped only when the database still describes what
    /// the file held. A stale write deliberately leaves it naming the old
    /// bytes: the render just merged over an edit this database never took
    /// in, so the next scan has to see the mismatch and take the merged
    /// file in. Stamping the rendered oid would hide the hand edit from
    /// every future scan.
    async fn persist_render(
        &self,
        file_path: &str,
        rendered: Rendered,
    ) -> Result<Option<WriteFileOutcome>> {
        let Rendered {
            abs_path,
            bytes,
            stale,
            write_seq,
            format,
            retract,
            commit_id,
            ops,
            conflicts,
        } = rendered;
        let tmp = stage_write(&abs_path, &bytes)?;
        // a stale file keeps naming the old bytes, for the next scan to
        // take the merge in
        let oid = match stale {
            true => None,
            false => Some(git::blob_oid_for_bytes(&self.repo()?, &bytes)?.to_string()),
        };
        let write = RenderWrite {
            file_path,
            expected: write_seq,
            format: format.as_deref(),
            source_oid: oid.as_deref(),
            retract,
            commit_id: commit_id.as_deref(),
            ops: &ops,
        };
        let persist = || {
            tmp.persist(&abs_path)
                .map(|_| ())
                .map_err(|e| Error::Io(e.error))
        };
        let won =
            on_pool!(self.db(), pool => commit_render_in_pool(self, pool, &write, persist).await)?;
        Ok(won.then_some(WriteFileOutcome {
            written: Some(abs_path),
            deleted: false,
            conflicts,
        }))
    }

    /// Take `file_path` off the disk, unless another writer has since
    /// changed what its render decides -- written a record back, say: then
    /// `None`, and nothing is removed.
    async fn persist_removal(
        &self,
        file_path: &str,
        removal: Removal,
    ) -> Result<Option<WriteFileOutcome>> {
        let Removal {
            abs_path,
            write_seq,
        } = removal;
        let mut removed = false;
        // `remove_file` on an absent file is the already-done case, not a
        // failure.
        let unlink = || match std::fs::remove_file(&abs_path) {
            Ok(()) => {
                removed = true;
                Ok(())
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(Error::Io(e)),
        };
        let write = RenderWrite {
            file_path,
            expected: write_seq,
            format: None,
            source_oid: None,
            retract: false,
            commit_id: None,
            ops: &[],
        };
        let won =
            on_pool!(self.db(), pool => commit_render_in_pool(self, pool, &write, unlink).await)?;
        Ok(won.then(|| WriteFileOutcome {
            written: removed.then(|| abs_path.clone()),
            deleted: true,
            conflicts: Vec::new(),
        }))
    }

    /// Record `bookkeeping`, unless another writer has since changed what
    /// its render decides: then `None`, and nothing is recorded.
    async fn persist_bookkeeping(
        &self,
        file_path: &str,
        bookkeeping: Bookkeeping,
    ) -> Result<Option<WriteFileOutcome>> {
        let Bookkeeping {
            write_seq,
            retract,
            commit_id,
            ops,
            outcome,
        } = bookkeeping;
        let write = RenderWrite {
            file_path,
            expected: write_seq,
            format: None,
            source_oid: None,
            retract,
            commit_id: commit_id.as_deref(),
            ops: &ops,
        };
        let nothing = || Ok(());
        let won =
            on_pool!(self.db(), pool => commit_render_in_pool(self, pool, &write, nothing).await)?;
        Ok(won.then_some(outcome))
    }

    /// Whether `abs` no longer hashes to the `source_oid` recorded on
    /// `file_row` — i.e. the disk holds an edit the database never took
    /// in. `false` when the row is absent or has no `source_oid`
    /// (registered by a record write, never scanned — nothing was
    /// parsed, so there is nothing to contradict) and when the file is
    /// absent (a document about to be synthesised).
    fn source_stale(&self, file_row: Option<&crate::model::File>, abs: &Path) -> Result<bool> {
        let Some(expected) = file_row.and_then(|f| f.source_oid.as_deref()) else {
            return Ok(false);
        };
        let bytes = match std::fs::read(abs) {
            Ok(b) => b,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(false),
            Err(e) => return Err(Error::Io(e)),
        };
        Ok(git::blob_oid_for_bytes(&self.repo()?, &bytes)? != expected)
    }

    /// The file's side of every record this worktree is in conflict
    /// over, optionally narrowed to one file.
    ///
    /// Each returned [`Record`] carries the *file's* value with
    /// [`Record::conflict`] set; the database's own row for the same
    /// `(file_path, path, key)` is what [`Self::get_record`] returns.
    /// A conflict row with [`Record::deleted`] set means the file no
    /// longer has the record at all, its `json` being the value the file
    /// dropped.
    pub async fn list_conflicts(&self, file_path: Option<&str>) -> Result<Vec<Record>> {
        db::record::list_conflicts(self.db(), self.worktree_id(), file_path).await
    }

    /// Settle a conflicted record, ending the divergence.
    ///
    /// A plain CRUD write to a conflicted key is *not* a resolution: it
    /// rewrites the database's row and leaves the conflict standing, so
    /// a client that never looked at [`Self::list_conflicts`] cannot
    /// discard the file's value by accident. Saying which side wins is
    /// this call, and [`Resolution`] is the affirmation.
    ///
    /// [`Resolution::Theirs`] / [`Resolution::Merged`] /
    /// [`Resolution::Delete`] rewrite the record and drop the conflict
    /// row outright — the value is being restated, so if the file has
    /// moved again in the meantime the next scan simply finds a new
    /// divergence. [`Resolution::Ours`] restates nothing, so it only
    /// marks the conflict [`ConflictState::Resolved`] and leaves the
    /// file check to the next write.
    ///
    /// Returns the [`WriteOutcome`] of the row that changed: the record
    /// for every variant but [`Resolution::Ours`], which changes the
    /// conflict row.
    ///
    /// # Errors
    ///
    /// [`crate::Error::NotFound`] when there is no conflict at that key,
    /// or no in-flight record left for it to be about;
    /// [`crate::Error::Conflict`] when `expected_commit` does not match
    /// the record.
    pub async fn resolve_conflict(
        &self,
        file_path: &str,
        path: &str,
        key: &str,
        resolution: Resolution,
        expected_commit: Option<CommitRef>,
    ) -> Result<WriteOutcome> {
        on_pool!(self.db(), pool => resolve_conflict_in_pool(
                    self,
                    pool,
                    file_path,
                    path,
                    key,
                    resolution,
                    expected_commit,
                )
                .await)
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
        let commit = match self.commit_scanned(message, head).await {
            Err(Error::HeadMoved { .. }) if exported.is_some() => {
                let (_, head) = self.scan(ScanOptions::default()).await?;
                self.commit_scanned(message, head).await?
            }
            made => made?,
        };
        Ok(Committed { commit, exported })
    }

    /// The rest of [`Self::commit_repository`], after a scan that took in
    /// `scanned_head`. The commit is made onto it, so [`Error::HeadMoved`]
    /// if `HEAD` has moved since.
    async fn commit_scanned(
        &self,
        message: &str,
        scanned_head: Option<gix::ObjectId>,
    ) -> Result<Option<String>> {
        // Snapshot the dirty file list *before* save_changes (which sets
        // no commit_id changes) so we know what to stage even when bytes
        // didn't actually change on disk.
        let dirty: Vec<String> =
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
        let head = scanned_head.map(|o| o.to_string());
        let to_stage: Vec<String> = dirty
            .iter()
            .filter(|rel| self.differs_from_head(&repo, head.as_deref(), rel))
            .cloned()
            .collect();
        // File rows the database owes a removal for and no longer has a
        // file behind. Drawn from `dirty` rather than `to_stage`: a
        // deletion git has already recorded stages nothing, and its row
        // would otherwise stay tombstoned with no commit ever coming to
        // purge it.
        let tombstoned: std::collections::HashSet<String> =
            db::file::list(self.db(), self.worktree_id())
                .await?
                .into_iter()
                .filter(|f| f.deleted)
                .map(|f| f.path)
                .collect();
        let removed: Vec<String> = dirty
            .iter()
            .filter(|rel| tombstoned.contains(*rel) && !self.inner.repo_path.join(rel).exists())
            .cloned()
            .collect();

        let oid_str = match (to_stage.is_empty(), head.as_deref()) {
            (false, _) => {
                git::commit_paths_onto(&repo, &to_stage, &message, scanned_head)?.to_string()
            }
            // Nothing to record, but rows are still in flight: their
            // content is what HEAD already holds, so attribute them to
            // the commit that does carry it rather than manufacture an
            // empty one. (A row whose content is *not* in HEAD is a
            // conflicted one, which `the fold` skips.)
            (true, Some(head)) => head.to_string(),
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
        let head_side = self.head_files(&repo, Some(&oid_str), &known_files, false)?;
        let carried = crate::scan::Carried {
            commit: &oid_str,
            files: &files,
            removed: &removed,
            watermark,
            head_files: &head_side.files,
            named: &named,
        };
        on_pool!(self.db(), pool => crate::scan::commit_in_pool(self, pool, &carried).await)?;

        Ok((!to_stage.is_empty()).then_some(oid_str))
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
    fn differs_from_head(&self, repo: &gix::Repository, head: Option<&str>, rel: &str) -> bool {
        let Some(head) = head else {
            return true;
        };
        let disk = match std::fs::read(self.inner.repo_path.join(rel)) {
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

/// Parse `bytes` as `syntax` and classify the document with `formats`:
/// `None` when no format claims it or validation rejects it outright.
pub(crate) fn parse_and_detect<'f>(
    formats: &'f FormatRegistry,
    rel_path: &str,
    syntax: Syntax,
    bytes: &[u8],
    stats: &mut SyncOutcome,
) -> Result<Option<ParsedDoc<'f>>> {
    let parsed = syntax.parse(rel_path, bytes)?;
    if parsed.extended {
        // A rewrite emits strict JSON, so this file will lose its
        // comments and be reflowed the first time a record in it
        // changes.
        stats.files_needing_json5 += 1;
        tracing::warn!(
            file = rel_path,
            "file needs json5 syntax; a rewrite will emit strict json and drop comments"
        );
    }
    let literate = parsed.literate;
    let value = crate::document::fold_chunks(parsed.chunks);
    // A literate document names its format in front matter, because
    // its YAML is spread across fenced blocks and carries no header
    // for `detect` to inspect. `generic` is the exception: it says
    // the merged document does carry one after all, so classify it
    // the way a plain YAML or JSON file is.
    let format = match literate.as_deref() {
        Some(GENERIC_LITERATE_FORMAT) | None => formats.detect(&value),
        Some(name) => formats.detect_literate(name),
    };
    let Some(format) = format else {
        return Ok(None);
    };
    // After `fold_chunks`, so a literate document is validated as the
    // document it merges to rather than per fenced block.
    let validation = format.validate_document(&value);
    log_validation(rel_path, format.name(), &validation);
    let validation = std::sync::Arc::new(validation);
    if !validation.is_empty() {
        stats.invalid.push(crate::ValidationFailure {
            file_path: rel_path.to_string(),
            format: format.name().to_string(),
            validation: std::sync::Arc::clone(&validation),
        });
    }
    if validation.is_fatal() {
        // Unreadable as this format, so it is skipped exactly as an
        // unparseable file is: its rows go stale rather than being
        // cleared, because a document we cannot interpret is no
        // evidence that its records are gone.
        return Ok(None);
    }
    Ok(Some(ParsedDoc {
        format,
        value,
        validation,
    }))
}

/// `root` rendered as `file_path`'s bytes. Markdown is rendered against
/// the document's own blocks rather than from `root`, so it needs the
/// `source` and the records actually `applied` -- neither of which a
/// value-to-bytes function can take.
fn render_bytes(
    file_path: &str,
    syntax: Syntax,
    source: Option<&str>,
    root: &mut serde_json::Value,
    format: Option<&dyn crate::DataFormat>,
    applied: &[unfurl_merge::markdown::Applied],
    touched: &[String],
) -> Result<Vec<u8>> {
    match source.filter(|_| syntax == Syntax::Markdown) {
        Some(src) => unfurl_merge::markdown::render(
            file_path,
            src,
            root,
            applied,
            format.map(|f| f.path_prefixes()).unwrap_or_default(),
        )
        .map_err(Error::from),
        None => syntax.render_document(source, root, format, touched, file_path),
    }
}

/// How many times [`SyncedRepo::write_file`] renders a file that another
/// writer keeps writing first, before giving up.
pub(crate) const WRITE_ATTEMPTS: usize = 3;

/// What [`SyncedRepo::render_file`] made of a file.
enum Render {
    /// Nothing to record.
    Done(WriteFileOutcome),
    /// Nothing for the disk, but conflicts to record or a deletion to
    /// retract.
    Record(Bookkeeping),
    /// Bytes to write.
    Write(Rendered),
    /// A deleted file with nothing left in it, to take off the disk.
    Remove(Removal),
}

impl Render {
    /// `bookkeeping` to record, or `Done` when it holds nothing.
    fn record(bookkeeping: Bookkeeping) -> Self {
        match bookkeeping.retract || !bookkeeping.ops.is_empty() {
            true => Render::Record(bookkeeping),
            false => Render::Done(bookkeeping.outcome),
        }
    }
}

/// What a render with nothing to write records, and the `write_seq` it was
/// decided at.
struct Bookkeeping {
    write_seq: Option<i64>,
    retract: bool,
    commit_id: Option<String>,
    ops: Vec<ConflictOp>,
    outcome: WriteFileOutcome,
}

/// A removal waiting to be made, and the `write_seq` it was decided at.
struct Removal {
    abs_path: PathBuf,
    write_seq: Option<i64>,
}

/// A render waiting to be written, and what it was rendered over.
struct Rendered {
    abs_path: PathBuf,
    bytes: Vec<u8>,
    stale: bool,
    /// The file row's `write_seq` the render read; `None` when there was
    /// no row.
    write_seq: Option<i64>,
    /// The name of the format the file's records belong to, to register
    /// the file as when there was no row.
    format: Option<String>,
    /// The file was deleted but something is still in it: the write
    /// clears the deletion.
    retract: bool,
    commit_id: Option<String>,
    ops: Vec<ConflictOp>,
    conflicts: Vec<crate::model::RecordConflict>,
}

/// What [`commit_render_in_pool`] records.
struct RenderWrite<'a> {
    file_path: &'a str,
    /// `write_seq` as the render found it; `None` when there was no row.
    expected: Option<i64>,
    /// What to register the file as when there was no row.
    format: Option<&'a str>,
    /// The new `source_oid`, or `None` to keep it.
    source_oid: Option<&'a str>,
    /// Clear the file's deletion.
    retract: bool,
    commit_id: Option<&'a str>,
    ops: &'a [ConflictOp],
}

/// Record a render, its conflict bookkeeping, and `persist`, the rename
/// that puts it on disk (or the unlink that takes it off), in one
/// transaction -- unless the file row's `write_seq` has moved since the
/// render read it: then `false`, and none of it is done.
///
/// A render that found no row registers the file and claims from 0: a
/// racing render that registered it first has already moved it past 0. A
/// file whose records no format claims can't be registered (its format
/// column has nothing to hold), so its write isn't checked; a record
/// write refuses such a section in the first place.
///
/// `persist` should be the rename only. The bytes are written and flushed
/// beforehand, so the transaction spans a constant-time operation rather
/// than an I/O proportional to the document -- which matters because
/// holding it open is holding a row lock, and on SQLite that is the
/// single writer.
///
/// One window survives and cannot be closed: a crash between the rename
/// and the commit leaves the file ahead of the database. That direction
/// is the recoverable one -- the bytes already contain the pending
/// records, so re-syncing takes them back in.
async fn commit_render_in_pool<DB: crate::db::store::Store>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    write: &RenderWrite<'_>,
    persist: impl FnOnce() -> Result<()>,
) -> Result<bool> {
    let mut tx = pool.begin().await?;
    let w = sync.worktree_id();
    let expected = match (write.expected, write.format) {
        (Some(seq), _) => Some(seq),
        (None, Some(format)) => {
            DB::ensure_file(&mut tx, w, write.file_path, format).await?;
            Some(0)
        }
        (None, None) => None,
    };
    if let Some(expected) = expected {
        if !DB::claim_write(&mut tx, w, write.file_path, expected, write.source_oid).await? {
            return Ok(false);
        }
    }
    if write.retract {
        DB::set_file_deleted(&mut tx, w, write.file_path, false).await?;
    }
    if !write.ops.is_empty() {
        apply_conflict_ops_in_tx(&mut tx, sync, write.file_path, write.commit_id, write.ops)
            .await?;
    }
    persist()?;
    tx.commit().await?;
    Ok(true)
}

/// A recovery branch claimed at `base`, the commit the records it takes
/// were made on, with every file of its tree.
struct Prepared {
    branch: String,
    base: String,
    files: Vec<crate::scan::HeadFile>,
}

fn oid(hex: &str) -> Result<gix::ObjectId> {
    gix::ObjectId::from_hex(hex.as_bytes()).map_err(|e| Error::Git(e.to_string()))
}

#[cfg(test)]
mod race_tests {
    //! Writers racing each other: one renders, scans or commits, another
    //! writes, then the first finishes. Each test runs on SQLite --
    //! file-backed, so in WAL mode with several connections, as deployed --
    //! and, with `UNFURL_TEST_PG_URL` set and the `postgres` feature, on
    //! Postgres.
    //!
    //! The snapshot tests only tell a snapshot from separate reads on
    //! Postgres: SQLite in WAL mode reads one snapshot whether asked or not,
    //! so there they pass either way.
    use super::*;
    use crate::db::store::Store;
    use crate::model::Resolution;
    use crate::{DbConfig, FormatRegistry};
    use serde_json::json;

    const FILE: &str = "cloudmap.yaml";
    const DASHBOARD: &str = "git://unfurl.cloud/feb20a/dashboard.git";

    /// Run `test` on a scanned repository holding the cloudmap fixture,
    /// once per backend.
    async fn each_backend(test: impl AsyncFn(&SyncedRepo, &Path)) {
        let tmp = tempfile::tempdir().expect("tempdir");
        let url = format!(
            "sqlite://{}?mode=rwc",
            tmp.path().join("db.sqlite").display()
        );
        run(DbConfig::Sqlite { url }, &test).await;
        #[cfg(feature = "postgres")]
        if let Ok(base) = std::env::var("UNFURL_TEST_PG_URL") {
            let schema = format!("unfurl_test_{}", uuid::Uuid::new_v4().simple());
            let admin = sqlx::PgPool::connect(&base).await.expect("connect");
            sqlx::query(&format!("CREATE SCHEMA \"{schema}\""))
                .execute(&admin)
                .await
                .expect("create schema");
            let sep = if base.contains('?') { '&' } else { '?' };
            let url = format!("{base}{sep}options=-c%20search_path%3D{schema}");
            run(DbConfig::Postgres { url }, &test).await;
            sqlx::query(&format!("DROP SCHEMA \"{schema}\" CASCADE"))
                .execute(&admin)
                .await
                .expect("drop schema");
        }
    }

    async fn run(db: DbConfig, test: &impl AsyncFn(&SyncedRepo, &Path)) {
        let repo = tempfile::tempdir().expect("tempdir");
        let fixture = std::fs::read(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/expected_cloudmap.yaml"),
        )
        .expect("fixture");
        git::init_with_files(repo.path(), &[(FILE.to_string(), fixture)], "initial").expect("init");
        let sync = SyncedRepo::open(repo.path(), db, FormatRegistry::with_builtins())
            .await
            .expect("open");
        sync.update_from_working_dir(ScanOptions::default())
            .await
            .expect("scan");
        test(&sync, repo.path()).await;
    }

    async fn upsert(sync: &SyncedRepo, key: &str) {
        sync.upsert_record(
            Some(FILE),
            "/repositories",
            key,
            json!({"name": key}),
            None,
            false,
        )
        .await
        .expect("upsert");
    }

    /// Change the file as a person or another tool would.
    fn hand_edit(dir: &Path, from: &str, to: &str) {
        let path = dir.join(FILE);
        let text = std::fs::read_to_string(&path).expect("read");
        assert!(text.contains(from), "{from:?} isn't in the file");
        std::fs::write(&path, text.replace(from, to)).expect("write");
    }

    fn on_disk(dir: &Path) -> String {
        std::fs::read_to_string(dir.join(FILE)).expect("read")
    }

    async fn render(sync: &SyncedRepo) -> Rendered {
        render_of(sync, FILE).await
    }

    async fn render_of(sync: &SyncedRepo, file_path: &str) -> Rendered {
        match sync.render_file(file_path).await.expect("render") {
            Render::Write(rendered) => rendered,
            Render::Done(outcome) => panic!("nothing to write: {outcome:?}"),
            Render::Remove(_) => panic!("a removal, not a write"),
            Render::Record(_) => panic!("bookkeeping, not a write"),
        }
    }

    async fn render_bookkeeping(sync: &SyncedRepo, file_path: &str) -> Bookkeeping {
        match sync.render_file(file_path).await.expect("render") {
            Render::Record(bookkeeping) => bookkeeping,
            Render::Done(outcome) => panic!("nothing to record: {outcome:?}"),
            Render::Write(_) => panic!("a write, not bookkeeping"),
            Render::Remove(_) => panic!("a removal, not bookkeeping"),
        }
    }

    async fn render_removal(sync: &SyncedRepo) -> Removal {
        match sync.render_file(FILE).await.expect("render") {
            Render::Remove(removal) => removal,
            Render::Done(outcome) => panic!("nothing to remove: {outcome:?}"),
            Render::Write(_) => panic!("a write, not a removal"),
            Render::Record(_) => panic!("bookkeeping, not a removal"),
        }
    }

    async fn file_row(sync: &SyncedRepo, file_path: &str) -> Option<crate::model::File> {
        db::file::get(sync.db(), sync.worktree_id(), file_path)
            .await
            .expect("file row")
    }

    /// Persisting `rendered` writes nothing: another writer changed what it
    /// read.
    async fn assert_lost(sync: &SyncedRepo, rendered: Rendered) {
        let written = sync.persist_render(FILE, rendered).await.expect("persist");
        assert!(written.is_none(), "the out-of-date render was written");
    }

    async fn dashboard_name(sync: &SyncedRepo) -> serde_json::Value {
        let record = sync
            .get_record(FILE, "/repositories", DASHBOARD)
            .await
            .expect("get");
        record.expect("dashboard").json["name"].clone()
    }

    /// The other writer's render, written first, isn't written over.
    #[tokio::test]
    async fn a_render_loses_to_a_write_since() {
        each_backend(async |sync, dir| {
            upsert(sync, "a").await;
            let first = render(sync).await;
            assert!(!first.stale);
            upsert(sync, "b").await;
            sync.write_file(FILE).await.expect("the other writer");

            assert_lost(sync, first).await;
            let text = on_disk(dir);
            assert!(
                text.contains("name: a") && text.contains("name: b"),
                "{text}"
            );
            // the other writer wrote every pending edit, so a retry renders
            // again and finds nothing left to write
            let again = sync.write_file(FILE).await.expect("write");
            assert!(again.written.is_none(), "{again:?}");
        })
        .await;
    }

    /// A write over a hand edit keeps `source_oid` naming the bytes before
    /// it, so `write_seq` is what tells a second such writer it lost. The hand edit is still taken in by the next scan.
    #[tokio::test]
    async fn a_render_loses_to_a_write_since_over_a_hand_edit() {
        each_backend(async |sync, dir| {
            hand_edit(dir, "name: dashboard", "name: dashboard-by-hand");
            upsert(sync, "a").await;
            let first = render(sync).await;
            assert!(first.stale, "the hand edit makes the file stale");
            upsert(sync, "b").await;
            sync.write_file(FILE).await.expect("the other writer");

            assert_lost(sync, first).await;
            let text = on_disk(dir);
            assert!(text.contains("dashboard-by-hand"), "{text}");
            assert!(
                text.contains("name: a") && text.contains("name: b"),
                "{text}"
            );

            assert_eq!(dashboard_name(sync).await, "dashboard");
            sync.update_from_working_dir(ScanOptions::default())
                .await
                .expect("scan");
            assert_eq!(dashboard_name(sync).await, "dashboard-by-hand");
        })
        .await;
    }

    /// A scan taking in a hand edit changes what a render decides, so a
    /// render from before it isn't written over the hand edit.
    #[tokio::test]
    async fn a_render_loses_to_a_scan_since() {
        each_backend(async |sync, dir| {
            upsert(sync, "a").await;
            let first = render(sync).await;
            hand_edit(dir, "name: dashboard", "name: dashboard-by-hand");
            sync.update_from_working_dir(ScanOptions::default())
                .await
                .expect("scan");

            assert_lost(sync, first).await;
            assert!(on_disk(dir).contains("dashboard-by-hand"));
            sync.write_file(FILE).await.expect("write");
            let text = on_disk(dir);
            assert!(
                text.contains("dashboard-by-hand") && text.contains("name: a"),
                "{text}"
            );
        })
        .await;
    }

    /// A render that read a conflict before it was resolved doesn't write
    /// it back.
    #[tokio::test]
    async fn a_render_loses_to_a_resolution_since() {
        each_backend(async |sync, dir| {
            sync.update_record(
                Some(FILE),
                "/repositories",
                DASHBOARD,
                json!({"name": "ours"}),
                None,
                false,
            )
            .await
            .expect("update");
            hand_edit(dir, "name: dashboard", "name: theirs");
            let scan = sync
                .update_from_working_dir(ScanOptions::default())
                .await
                .expect("scan");
            assert_eq!(scan.conflicts.len(), 1, "{scan:?}");
            upsert(sync, "s").await;
            let first = render(sync).await;
            sync.resolve_conflict(FILE, "/repositories", DASHBOARD, Resolution::Theirs, None)
                .await
                .expect("resolve");

            assert_lost(sync, first).await;
            let conflicts = sync.list_conflicts(None).await.expect("conflicts");
            assert!(
                conflicts.is_empty(),
                "reopened by the out-of-date render: {conflicts:?}"
            );
            assert_eq!(dashboard_name(sync).await, "theirs");
            sync.write_file(FILE).await.expect("write");
            let conflicts = sync.list_conflicts(None).await.expect("conflicts");
            assert!(conflicts.is_empty(), "{conflicts:?}");
            assert_eq!(dashboard_name(sync).await, "theirs");
            let text = on_disk(dir);
            assert!(
                text.contains("name: theirs") && text.contains("name: s"),
                "{text}"
            );
        })
        .await;
    }

    /// A render with nothing to write but a conflict to open doesn't
    /// reopen it after a scan took the conflict in and it was resolved.
    #[tokio::test]
    async fn conflict_bookkeeping_loses_to_a_resolution_since() {
        each_backend(async |sync, dir| {
            sync.update_record(
                Some(FILE),
                "/repositories",
                DASHBOARD,
                json!({"name": "ours"}),
                None,
                false,
            )
            .await
            .expect("update");
            // a hand edit not yet taken in: the render finds the divergence
            hand_edit(dir, "name: dashboard", "name: theirs");
            let first = render_bookkeeping(sync, FILE).await;
            assert!(!first.ops.is_empty());
            sync.update_from_working_dir(ScanOptions::default())
                .await
                .expect("scan");
            sync.resolve_conflict(FILE, "/repositories", DASHBOARD, Resolution::Theirs, None)
                .await
                .expect("resolve");

            let recorded = sync
                .persist_bookkeeping(FILE, first)
                .await
                .expect("persist");
            assert!(recorded.is_none(), "the out-of-date render was recorded");
            let conflicts = sync.list_conflicts(None).await.expect("conflicts");
            assert!(
                conflicts.is_empty(),
                "reopened by the out-of-date render: {conflicts:?}"
            );
        })
        .await;
    }

    /// A retraction decided before the file was deleted again doesn't undo
    /// the newer deletion.
    #[tokio::test]
    async fn a_retraction_loses_to_a_deletion_since() {
        each_backend(async |sync, dir| {
            let one = "one.yaml";
            let doc = "apiVersion: unfurl/v1.0.0\nkind: CloudMap\n\
                       repositories:\n  git://example.com/r.git:\n    name: r\n";
            std::fs::write(dir.join(one), doc).expect("write");
            let repo = sync.repo().expect("repo");
            git::commit_paths(&repo, &[one.into()], "add").expect("commit");
            sync.update_from_working_dir(ScanOptions::default())
                .await
                .expect("scan");
            sync.delete_file(one, None).await.expect("delete");
            // an edit to the deleted record contests the deletion
            std::fs::write(dir.join(one), doc.replace("name: r", "name: edited")).expect("edit");
            sync.update_from_working_dir(ScanOptions::default())
                .await
                .expect("scan");
            let first = render_bookkeeping(sync, one).await;
            assert!(first.retract);
            sync.delete_file(one, None).await.expect("delete again");

            let recorded = sync.persist_bookkeeping(one, first).await.expect("persist");
            assert!(
                recorded.is_none(),
                "the out-of-date retraction was recorded"
            );
            let row = file_row(sync, one).await.expect("row");
            assert!(row.deleted, "the newer deletion was undone");
        })
        .await;
    }

    /// A render from before the file was deleted doesn't bring it back.
    #[tokio::test]
    async fn a_render_loses_to_a_deletion_since() {
        each_backend(async |sync, dir| {
            upsert(sync, "a").await;
            let first = render(sync).await;
            sync.delete_file(FILE, None).await.expect("delete");

            assert_lost(sync, first).await;
            let outcome = sync.write_file(FILE).await.expect("write");
            assert!(outcome.deleted, "{outcome:?}");
            assert!(!dir.join(FILE).exists());
        })
        .await;
    }

    /// A removal decided before a record was written back into the file
    /// doesn't delete the file that write put there.
    #[tokio::test]
    async fn a_removal_loses_to_a_write_back_since() {
        each_backend(async |sync, dir| {
            sync.delete_file(FILE, None).await.expect("delete");
            let first = render_removal(sync).await;
            upsert(sync, "a").await;
            sync.write_file(FILE).await.expect("the other writer");

            let removed = sync.persist_removal(FILE, first).await.expect("persist");
            assert!(removed.is_none(), "the out-of-date removal was made");
            assert!(on_disk(dir).contains("name: a"));
            assert!(!file_row(sync, FILE).await.expect("row").deleted);
        })
        .await;
    }

    /// A deletion with a record written back is retracted by the write
    /// that puts the record on disk, in one render: the retraction doesn't
    /// make its own write lose.
    #[tokio::test]
    async fn a_retraction_is_one_write() {
        each_backend(async |sync, dir| {
            sync.delete_file(FILE, None).await.expect("delete");
            upsert(sync, "a").await;
            let mut renders = 0;
            let outcome = sync
                .write_file_racing(FILE, async || {
                    renders += 1;
                    Ok(())
                })
                .await
                .expect("write");
            assert_eq!(renders, 1);
            assert!(outcome.written.is_some() && !outcome.deleted, "{outcome:?}");
            assert!(on_disk(dir).contains("name: a"));
            assert!(!file_row(sync, FILE).await.expect("row").deleted);
        })
        .await;
    }

    /// A file a record was created in has no row until it is first
    /// written, and that write registers it: a second render of it, from
    /// before, loses rather than going unchecked.
    #[tokio::test]
    async fn a_render_of_a_new_file_loses_to_its_first_write() {
        each_backend(async |sync, dir| {
            const NEW: &str = "new.yaml";
            let json = json!({"name": "a"});
            sync.create_record(Some(NEW), "/repositories", "a", json, None, false)
                .await
                .expect("create");
            assert!(file_row(sync, NEW).await.is_none(), "no row before a write");
            let first = render_of(sync, NEW).await;
            assert_eq!(first.write_seq, None);
            sync.write_file(NEW).await.expect("the other writer");
            let row = file_row(sync, NEW).await.expect("registered by the write");
            assert!(row.source_oid.is_some(), "{row:?}");

            let written = sync.persist_render(NEW, first).await.expect("persist");
            assert!(written.is_none(), "the out-of-date render was written");
            let text = std::fs::read_to_string(dir.join(NEW)).expect("read");
            assert!(text.contains("name: a"), "{text}");
        })
        .await;
    }

    /// `claim_write` claims only from the `write_seq` it's given, and keeps
    /// `source_oid` when given none: a stale write's.
    #[tokio::test]
    async fn claim_write_checks_write_seq_and_can_keep_source_oid() {
        each_backend(async |sync, _dir| {
            let w = sync.worktree_id();
            let before = file_row(sync, FILE).await.expect("row");
            let seq = async || {
                let inputs = db::record::render_inputs(sync.db(), w, FILE).await;
                inputs.expect("inputs").write_seq
            };
            let at = seq().await.expect("seq");
            on_pool!(sync.db(), pool => {
                let mut tx = pool.begin().await.expect("begin");
                let stale = Store::claim_write(&mut tx, w, FILE, at + 1, Some("x")).await;
                assert!(!stale.expect("claim"), "claimed from a write_seq it isn't at");
                let kept = Store::claim_write(&mut tx, w, FILE, at, None).await;
                assert!(kept.expect("claim"));
                tx.commit().await.expect("commit");
            });
            let after = file_row(sync, FILE).await.expect("row");
            assert_eq!(after.source_oid, before.source_oid);
            assert_eq!(seq().await, Some(at + 1));
        })
        .await;
    }

    /// Whether anything is left in a deleted file is read in the render's
    /// snapshot with the rest.
    #[tokio::test]
    async fn a_render_reads_whats_left_of_a_deletion_in_its_snapshot() {
        each_backend(async |sync, _dir| {
            let w = sync.worktree_id();
            sync.delete_file(FILE, None).await.expect("delete");
            let inputs = db::record::render_inputs_racing(sync.db(), w, FILE, async || {
                upsert(sync, "a").await;
                Ok(())
            })
            .await
            .expect("inputs");
            assert!(!inputs.live, "the snapshot saw a record written back later");
            let after = db::record::render_inputs(sync.db(), w, FILE)
                .await
                .expect("inputs");
            assert!(after.live, "the write-back did land");
        })
        .await;
    }

    /// The dashboard's pending edit `ours` against `theirs` in the file.
    async fn conflicted(sync: &SyncedRepo, dir: &Path) {
        sync.update_record(
            Some(FILE),
            "/repositories",
            DASHBOARD,
            json!({"name": "ours"}),
            None,
            false,
        )
        .await
        .expect("update");
        hand_edit(dir, "name: dashboard", "name: theirs");
        sync.update_from_working_dir(ScanOptions::default())
            .await
            .expect("scan");
        assert_eq!(sync.list_conflicts(None).await.expect("conflicts").len(), 1);
    }

    /// The dashboard's name in `FILE` at `commit`.
    fn name_at(sync: &SyncedRepo, commit: &str) -> serde_json::Value {
        let bytes = git::read_blob_at_commit(&sync.repo().expect("repo"), commit, FILE)
            .expect("read")
            .expect("there");
        let doc: serde_json::Value = serde_saphyr::from_slice(&bytes).expect("yaml");
        doc["repositories"][DASHBOARD]["name"].clone()
    }

    /// A write to a conflicted edit after the export read it: the export
    /// moves nothing, and tries again with the new edit.
    #[tokio::test]
    async fn an_export_a_write_raced_tries_again() {
        each_backend(async |sync, dir| {
            conflicted(sync, dir).await;
            let mut tries = 0;
            let exported = sync
                .export_racing(
                    "exported",
                    async || {
                        tries += 1;
                        if tries == 1 {
                            let ours = json!({"name": "ours-again"});
                            sync.update_record(
                                Some(FILE),
                                "/repositories",
                                DASHBOARD,
                                ours,
                                None,
                                false,
                            )
                            .await?;
                        }
                        Ok(())
                    },
                    |_| Ok(()),
                )
                .await
                .expect("export")
                .expect("exported");
            assert_eq!(tries, 2);
            assert_eq!(name_at(sync, &exported.commit), "ours-again");
            assert!(sync
                .list_conflicts(None)
                .await
                .expect("conflicts")
                .is_empty());
        })
        .await;
    }

    /// An export that fails after any step from the move on loses
    /// nothing, and calling it again finishes it.
    #[tokio::test]
    async fn a_failed_export_is_finished_by_the_next() {
        use crate::export::ExportStep;
        for at in [ExportStep::Moved, ExportStep::Written, ExportStep::Folded] {
            each_backend(async |sync, dir| {
                conflicted(sync, dir).await;
                let base = git::worktree_meta(&sync.repo().expect("repo"))
                    .expect("meta")
                    .head_oid
                    .expect("head");
                let err = sync
                    .export_failing("exported", at)
                    .await
                    .expect_err("stopped");
                assert!(
                    matches!(&err, Error::Other(m) if m.starts_with("stopped")),
                    "{err:?}"
                );
                assert!(sync
                    .list_conflicts(None)
                    .await
                    .expect("conflicts")
                    .is_empty());
                let repo = sync.repo().expect("repo");
                let tip = git::branch_tip(&repo, "exported").expect("tip");
                assert_eq!(tip, Some(base), "{at:?}: the ref stays at the base");
                let folded = branch_commit(sync, "exported").await;

                let exported = sync
                    .export_conflicts("exported")
                    .await
                    .expect("finish")
                    .expect("finished");
                assert_ne!(exported.commit, base.to_string());
                if at == ExportStep::Folded {
                    assert_eq!(Some(&exported.commit), folded.as_ref(), "the commit folded");
                }
                let tip = git::branch_tip(&repo, "exported").expect("tip");
                assert_eq!(tip.map(|t| t.to_string()), Some(exported.commit.clone()));
                assert_eq!(name_at(sync, &exported.commit), "ours");
                let again = sync.export_conflicts("exported").await;
                assert!(
                    matches!(again, Err(Error::BranchExists { .. })),
                    "{again:?}"
                );
            })
            .await;
        }
    }

    /// The commit the database has branch `branch`'s worktree at.
    async fn branch_commit(sync: &SyncedRepo, branch: &str) -> Option<String> {
        let filter = crate::model::WorktreeFilter {
            origin: None,
            branch: Some(branch.into()),
        };
        let rows = sync.worktrees(&filter).await.expect("worktrees");
        rows[0].commit_id.clone()
    }

    /// An export that failed after its fold, then a write: the next call
    /// moves the ref to the commit folded.
    #[tokio::test]
    async fn a_failed_export_is_finished_after_a_write() {
        use crate::export::ExportStep;
        each_backend(async |sync, dir| {
            conflicted(sync, dir).await;
            sync.export_failing("exported", ExportStep::Folded)
                .await
                .expect_err("stopped");
            let folded = branch_commit(sync, "exported").await;
            let write = crate::model::BatchOp::Upsert {
                file_path: Some(FILE.into()),
                path: "/repositories".into(),
                key: "another".into(),
                json: json!({"name": "another"}),
                expected: None,
                resolve: false,
            };
            sync.apply_batch(vec![write], true, None)
                .await
                .expect("a write");

            let exported = sync
                .export_conflicts("exported")
                .await
                .expect("finish")
                .expect("finished");
            assert_eq!(Some(exported.commit), folded);
        })
        .await;
    }

    /// Exports a failure left unfinished, after any step, are finished by
    /// `finish_exports`, with no branch named; a finished one's mark left
    /// behind is cleared.
    #[tokio::test]
    async fn unfinished_exports_are_finished_at_startup() {
        use crate::export::ExportStep;
        for at in [ExportStep::Moved, ExportStep::Written, ExportStep::Folded] {
            each_backend(async |sync, dir| {
                conflicted(sync, dir).await;
                sync.export_failing("exported", at)
                    .await
                    .expect_err("stopped");
                let done = sync.finish_exports().await.expect("finished");
                assert_eq!(done.len(), 1, "{at:?}");
                assert_eq!(name_at(sync, &done[0].commit), "ours");
                let repo = sync.repo().expect("repo");
                let tip = git::branch_tip(&repo, "exported").expect("tip");
                assert_eq!(tip.map(|t| t.to_string()), Some(done[0].commit.clone()));
                assert!(sync.finish_exports().await.expect("none").is_empty());

                // a mark a crash after the ref moved left behind
                let n = done[0].worktree_id;
                let branch = sync.get_worktree().await.expect("worktree").branch;
                on_pool!(sync.db(), pool => {
                    let mut tx = pool.begin().await.expect("tx");
                    Store::set_exporting_from(&mut tx, n, &branch)
                        .await
                        .expect("mark");
                    tx.commit().await.expect("commit");
                });
                assert!(sync.finish_exports().await.expect("cleared").is_empty());
                let row = sync.worktrees(&Default::default()).await.expect("rows");
                let marked = row.iter().find(|w| w.id == n).expect("the branch");
                assert_eq!(marked.exporting_from, None, "the mark is cleared");
            })
            .await;
        }
    }

    /// A recovery that fails after its rebuild committed leaves the
    /// records in its branch's draft, an unfinished export
    /// `finish_exports` completes.
    #[tokio::test]
    async fn a_failed_recovery_is_finished_at_startup() {
        each_backend(async |sync, dir| {
            let git = |args: &[&str]| -> String {
                let out = std::process::Command::new("git")
                    .args(["-c", "user.name=t", "-c", "user.email=t@t"])
                    .args(args)
                    .current_dir(dir)
                    .output()
                    .expect("git");
                assert!(out.status.success(), "git {args:?}: {out:?}");
                String::from_utf8_lossy(&out.stdout).trim().to_string()
            };
            let base = git(&["rev-parse", "HEAD"]);
            sync.update_record(
                Some(FILE),
                "/repositories",
                DASHBOARD,
                json!({"name": "lost"}),
                None,
                false,
            )
            .await
            .expect("write");
            let gone = sync
                .commit_repository("lost", Default::default())
                .await
                .expect("commit")
                .commit
                .expect("a commit");
            git(&["reset", "-q", "--hard", &base]);
            git(&["reflog", "expire", "--expire=now", "--all"]);
            git(&["gc", "-q", "--prune=now"]);

            let worktree = sync.get_worktree().await.expect("worktree");
            let options = ScanOptions {
                recover_missing: true,
                ..Default::default()
            };
            let mut fail = |_| Err(Error::Other("stopped".into()));
            let err = sync
                .rebuild_onto(&worktree, &gone, &base, &options, &mut fail)
                .await
                .expect_err("stopped");
            assert!(matches!(&err, Error::Other(m) if m == "stopped"), "{err:?}");
            assert_eq!(
                sync.get_worktree().await.expect("worktree").commit_id,
                Some(base.clone()),
                "the rebuild committed"
            );

            let done = sync.finish_exports().await.expect("finished");
            assert_eq!(done.len(), 1);
            assert_eq!(
                done[0].branch,
                format!("git-sync/recovered-{}", &gone[..12])
            );
            assert_eq!(name_at(sync, &done[0].commit), "lost");
        })
        .await;
    }

    /// A failed export whose ref someone else committed to, before or
    /// after its fold: the next call leaves the branch alone rather than
    /// take their commit for its own.
    #[tokio::test]
    async fn a_failed_export_is_not_finished_on_another_commit() {
        use crate::export::ExportStep;
        for at in [ExportStep::Moved, ExportStep::Folded] {
            each_backend(async |sync, dir| {
                conflicted(sync, dir).await;
                sync.export_failing("exported", at)
                    .await
                    .expect_err("stopped");
                let repo = sync.repo().expect("repo");
                let base = git::branch_tip(&repo, "exported")
                    .expect("tip")
                    .expect("the ref");
                let theirs = vec![("other.txt".to_string(), Some(b"theirs".to_vec()))];
                let c = git::commit_files_onto(&repo, base, &theirs, "theirs").expect("commit");
                git::move_branch(&repo, "exported", base, c).expect("move");

                let again = sync.export_conflicts("exported").await;
                assert!(
                    matches!(again, Err(Error::BranchExists { .. })),
                    "{at:?}: {again:?}"
                );
                assert_eq!(git::branch_tip(&repo, "exported").expect("tip"), Some(c));
            })
            .await;
        }
    }

    /// A finished export with an edit in its branch's draft since, as a
    /// checkout of the branch would make: exporting to it again is
    /// refused, with nothing committed or moved.
    #[tokio::test]
    async fn a_finished_export_is_not_resumed() {
        each_backend(async |sync, dir| {
            conflicted(sync, dir).await;
            let exported = sync
                .export_conflicts("exported")
                .await
                .expect("export")
                .expect("exported");
            let branch = sync.sibling(exported.worktree_id);
            let edit = crate::model::BatchOp::Upsert {
                file_path: Some(FILE.into()),
                path: "/repositories".into(),
                key: "on-the-branch".into(),
                json: json!({"name": "on the branch"}),
                expected: None,
                resolve: false,
            };
            branch
                .apply_batch(vec![edit], true, None)
                .await
                .expect("an edit on the branch");

            let again = sync.export_conflicts("exported").await;
            assert!(
                matches!(again, Err(Error::BranchExists { .. })),
                "{again:?}"
            );
            let repo = sync.repo().expect("repo");
            let tip = git::branch_tip(&repo, "exported").expect("tip");
            assert_eq!(tip.map(|t| t.to_string()), Some(exported.commit));
            let pending = branch.list_changes(None, false).await.expect("changes");
            assert!(pending.iter().any(|r| r.key == "on-the-branch"));
        })
        .await;
    }

    /// `HEAD` moving between an export and its commit: the commit is
    /// retried, and the export reported.
    #[tokio::test]
    async fn a_commit_after_an_export_retries_a_head_move() {
        each_backend(async |sync, dir| {
            conflicted(sync, dir).await;
            let options = CommitOptions {
                conflicts_to_branch: Some("exported".into()),
            };
            let mut moved = false;
            let committed = sync
                .commit_repository_racing("commit", options, async || {
                    if !moved {
                        moved = true;
                        std::fs::write(dir.join("other.txt"), "outside\n").expect("write");
                        git::commit_paths(&sync.repo()?, &["other.txt".into()], "outside")?;
                    }
                    Ok(())
                })
                .await
                .expect("commit");
            assert!(moved);
            let exported = committed.exported.expect("exported");
            assert_eq!(name_at(sync, &exported.commit), "ours");
            let commit = committed.commit.expect("committed");
            assert_eq!(name_at(sync, &commit), "theirs");
        })
        .await;
    }

    /// Make a render lose without touching the file, as a writer of
    /// something else it depends on -- a resolution, a deletion -- does.
    async fn change_what_renders_read(sync: &SyncedRepo) -> Result<()> {
        let w = sync.worktree_id();
        on_pool!(sync.db(), pool => {
            let mut tx = pool.begin().await?;
            Store::bump_write_seq(&mut tx, w, FILE).await?;
            tx.commit().await?;
            Ok(())
        })
    }

    /// Having lost once, `write_file` renders again and writes that.
    #[tokio::test]
    async fn a_write_that_lost_renders_again() {
        each_backend(async |sync, dir| {
            upsert(sync, "a").await;
            let mut renders = 0;
            let outcome = sync
                .write_file_racing(FILE, async || {
                    renders += 1;
                    match renders {
                        1 => change_what_renders_read(sync).await,
                        _ => Ok(()),
                    }
                })
                .await
                .expect("the second render wins");
            assert_eq!(renders, 2);
            assert!(outcome.written.is_some(), "{outcome:?}");
            assert!(on_disk(dir).contains("name: a"));
        })
        .await;
    }

    /// Losing every time, `write_file` gives up with
    /// [`Error::FileChanged`] and writes nothing; the edit stays pending
    /// for the next write.
    #[tokio::test]
    async fn a_write_that_always_loses_gives_up() {
        each_backend(async |sync, dir| {
            upsert(sync, "a").await;
            let mut renders = 0;
            let err = sync
                .write_file_racing(FILE, async || {
                    renders += 1;
                    change_what_renders_read(sync).await
                })
                .await
                .expect_err("never wins");
            assert!(
                matches!(&err, Error::FileChanged { file_path } if file_path == FILE),
                "{err:?}"
            );
            assert_eq!(renders, WRITE_ATTEMPTS);
            assert!(!on_disk(dir).contains("name: a"));

            sync.write_file(FILE).await.expect("write");
            assert!(on_disk(dir).contains("name: a"));
        })
        .await;
    }

    /// A render's reads see one moment: a write committed after its
    /// snapshot began isn't in it.
    #[tokio::test]
    async fn a_snapshot_does_not_see_a_later_write() {
        each_backend(async |sync, _dir| {
            let w = sync.worktree_id();
            on_pool!(sync.db(), pool => {
                let mut tx = pool.begin().await.expect("begin");
                Store::begin_snapshot(&mut tx).await.expect("snapshot");
                let before = Store::write_seq(&mut tx, w, FILE).await.expect("read");
                assert!(before.is_some());
                let mut other = pool.begin().await.expect("begin");
                Store::bump_write_seq(&mut other, w, FILE).await.expect("bump");
                other.commit().await.expect("commit");
                let after = Store::write_seq(&mut tx, w, FILE).await.expect("read");
                assert_eq!(before, after, "the snapshot saw a later write");
            })
        })
        .await;
    }

    /// A render reads the database from one snapshot: a write committed
    /// partway through its reads -- here, after the first -- is in none of
    /// them.
    #[tokio::test]
    async fn a_render_reads_one_snapshot() {
        each_backend(async |sync, _dir| {
            let w = sync.worktree_id();
            upsert(sync, "a").await;
            let before = db::record::render_inputs(sync.db(), w, FILE)
                .await
                .expect("inputs");
            let inputs = db::record::render_inputs_racing(sync.db(), w, FILE, async || {
                upsert(sync, "b").await;
                sync.write_file(FILE).await.map(|_| ())
            })
            .await
            .expect("inputs");
            assert_eq!(inputs.write_seq, before.write_seq);
            let keys: Vec<&str> = inputs.pending.iter().map(|r| r.key.as_str()).collect();
            assert_eq!(keys, ["a"]);
            let source_oid =
                |i: &db::record::RenderInputs| i.file.as_ref().map(|f| f.source_oid.clone());
            assert_eq!(source_oid(&inputs), source_oid(&before));

            let after = db::record::render_inputs(sync.db(), w, FILE)
                .await
                .expect("inputs");
            assert_ne!(after.write_seq, before.write_seq, "the write did land");
        })
        .await;
    }

    /// An outside commit landing between a commit's scan and the commit
    /// itself fails the commit rather than being built on unseen, and a
    /// retry commits the same edit onto it.
    #[tokio::test]
    async fn a_commit_fails_when_head_moves_after_its_scan() {
        each_backend(async |sync, dir| {
            upsert(sync, "a").await;
            let (_, scanned) = sync.scan(ScanOptions::default()).await.expect("scan");
            std::fs::write(dir.join("other.txt"), "outside\n").expect("write");
            let repo = sync.repo().expect("repo");
            let outside =
                git::commit_paths(&repo, &["other.txt".into()], "out").expect("outside commit");

            let err = sync
                .commit_scanned("msg", scanned)
                .await
                .expect_err("HEAD moved after the scan");
            assert!(
                matches!(&err, Error::HeadMoved { expected, found }
                    if *expected == scanned.map(|o| o.to_string())
                        && *found == Some(outside.to_string())),
                "{err:?}"
            );
            assert_eq!(repo.head_id().expect("head").detach(), outside);

            let oid = sync
                .commit_repository("retry", Default::default())
                .await
                .expect("retry")
                .commit
                .expect("a commit");
            let commit = repo
                .find_commit(gix::ObjectId::from_hex(oid.as_bytes()).expect("oid"))
                .expect("commit");
            assert_eq!(
                commit.parent_ids().next().map(|p| p.detach()),
                Some(outside)
            );
            let committed = git::read_blob_at_commit(&repo, &oid, FILE)
                .expect("read")
                .expect("in the commit");
            assert!(String::from_utf8_lossy(&committed).contains("name: a"));
        })
        .await;
    }
}

#[cfg(test)]
mod scan_cache_tests {
    //! What a scan skips because nothing changed: files known to be in no
    //! format, and history for files whose last commit can't have moved.
    use super::*;
    use crate::{DbConfig, FormatRegistry};

    const DEPLOY: &str = "deploy.yaml";
    const MANIFEST: &str = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm\n";

    /// A repository with the cloudmap fixture, `deploy.yaml` (in no format)
    /// and `extra`, scanned once.
    async fn scanned(extra: &[(&str, &str)]) -> (SyncedRepo, tempfile::TempDir, SyncOutcome) {
        let tmp = tempfile::tempdir().expect("tempdir");
        let fixture = std::fs::read(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/expected_cloudmap.yaml"),
        )
        .expect("fixture");
        let mut files = vec![
            ("cloudmap.yaml".to_string(), fixture),
            (DEPLOY.to_string(), MANIFEST.as_bytes().to_vec()),
        ];
        files.extend(
            extra
                .iter()
                .map(|(p, t)| (p.to_string(), t.as_bytes().to_vec())),
        );
        git::init_with_files(tmp.path(), &files, "initial").expect("init");
        let db = DbConfig::Sqlite {
            url: "sqlite::memory:".into(),
        };
        let sync = SyncedRepo::open(tmp.path(), db, FormatRegistry::with_builtins())
            .await
            .expect("open");
        let first = scan(&sync, false).await;
        (sync, tmp, first)
    }

    async fn scan(sync: &SyncedRepo, force: bool) -> SyncOutcome {
        sync.update_from_working_dir(ScanOptions {
            force,
            ..Default::default()
        })
        .await
        .expect("scan")
    }

    async fn commit_of(sync: &SyncedRepo, path: &str) -> Option<String> {
        let row = sync.get_file(path).await.expect("get").expect("row");
        row.commit_id
    }

    #[tokio::test]
    async fn a_document_in_no_format_is_parsed_once() {
        let (sync, _tmp, first) = scanned(&[]).await;
        assert!(first.files_parsed >= 2, "{first:?}");
        assert_eq!(scan(&sync, false).await.files_parsed, 0);
        assert!(sync.get_file(DEPLOY).await.expect("get").is_none());
    }

    #[tokio::test]
    async fn an_edited_document_in_no_format_is_parsed_again() {
        let (sync, tmp, _) = scanned(&[]).await;
        std::fs::write(tmp.path().join(DEPLOY), format!("{MANIFEST}data: {{}}\n")).expect("edit");
        assert_eq!(scan(&sync, false).await.files_parsed, 1);
        assert_eq!(scan(&sync, false).await.files_parsed, 0);
    }

    #[tokio::test]
    async fn a_document_that_becomes_a_cloudmap_is_taken_in() {
        let (sync, tmp, _) = scanned(&[]).await;
        let cloudmap = "apiVersion: unfurl/v1.0.0\nkind: CloudMap\n\
                        repositories:\n  git://example.com/r.git:\n    name: r\n";
        std::fs::write(tmp.path().join(DEPLOY), cloudmap).expect("edit");
        scan(&sync, false).await;
        let record = sync
            .get_record(DEPLOY, "/repositories", "git://example.com/r.git")
            .await
            .expect("get");
        assert!(record.is_some(), "the new cloudmap wasn't taken in");
    }

    #[tokio::test]
    async fn a_forced_scan_parses_every_document() {
        let (sync, _tmp, first) = scanned(&[]).await;
        assert_eq!(scan(&sync, true).await.files_parsed, first.files_parsed);
    }

    #[tokio::test]
    async fn a_document_that_doesnt_parse_is_reported_every_scan() {
        let (sync, _tmp, first) = scanned(&[("broken.json", "{ not json")]).await;
        let broken = |outcome: &SyncOutcome| {
            outcome
                .unparsed
                .iter()
                .any(|f| f.file_path == "broken.json")
        };
        assert!(broken(&first), "{first:?}");
        assert!(broken(&scan(&sync, false).await));
    }

    /// Commits that change a file and change it back leave it clean, with
    /// the blob the database holds, but with a later last commit.
    #[tokio::test]
    async fn a_file_changed_and_changed_back_moves_to_the_later_commit() {
        let (sync, tmp, _) = scanned(&[]).await;
        let repo = sync.repo().expect("repo");
        let path = tmp.path().join("cloudmap.yaml");
        let text = std::fs::read_to_string(&path).expect("read");
        std::fs::write(&path, text.replace("name: dashboard", "name: renamed")).expect("edit");
        git::commit_paths(&repo, &["cloudmap.yaml".into()], "change").expect("commit");
        std::fs::write(&path, &text).expect("restore");
        let back = git::commit_paths(&repo, &["cloudmap.yaml".into()], "back").expect("commit");
        scan(&sync, false).await;
        assert_eq!(
            commit_of(&sync, "cloudmap.yaml").await,
            Some(back.to_string())
        );
    }

    #[tokio::test]
    async fn a_files_commit_moves_only_with_commits_that_touch_it() {
        let (sync, tmp, _) = scanned(&[]).await;
        let repo = sync.repo().expect("repo");
        let initial = commit_of(&sync, "cloudmap.yaml").await;
        assert!(initial.is_some());
        for i in 0..3 {
            std::fs::write(tmp.path().join("other.txt"), format!("{i}\n")).expect("write");
            git::commit_paths(&repo, &["other.txt".into()], "other").expect("commit");
            scan(&sync, false).await;
            assert_eq!(commit_of(&sync, "cloudmap.yaml").await, initial);
        }

        let path = tmp.path().join("cloudmap.yaml");
        let text = std::fs::read_to_string(&path).expect("read");
        std::fs::write(&path, text.replace("name: dashboard", "name: renamed")).expect("edit");
        let touched = git::commit_paths(&repo, &["cloudmap.yaml".into()], "touch").expect("commit");
        scan(&sync, false).await;
        assert_eq!(
            commit_of(&sync, "cloudmap.yaml").await,
            Some(touched.to_string())
        );
    }
}

#[cfg(test)]
mod scan_bench {
    //! How long a scan of an unchanged repository takes. Run by hand:
    //!
    //! ```text
    //! cargo test --no-default-features --lib scan_bench -- --ignored --nocapture
    //! ```
    use super::*;
    use crate::{DbConfig, FormatRegistry};

    fn size(var: &str, default: usize) -> usize {
        std::env::var(var)
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(default)
    }

    /// A Kubernetes-like manifest of about 30 KB: valid YAML no format
    /// claims.
    fn manifest(n: usize) -> Vec<u8> {
        let mut text =
            format!("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm-{n}\ndata:\n");
        for i in 0..600 {
            text.push_str(&format!(
                "  key-{i}: value {n} {i} lorem ipsum dolor sit amet\n"
            ));
        }
        text.into_bytes()
    }

    #[tokio::test]
    #[ignore = "a timing, not a test"]
    async fn scan_of_an_unchanged_repository() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let dir = tmp.path().join("repo");
        let fixture = std::fs::read(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/expected_cloudmap.yaml"),
        )
        .expect("fixture");
        let mut files = vec![("cloudmap.yaml".to_string(), fixture)];
        files.extend(
            (0..size("BENCH_UNRELATED", 400)).map(|n| (format!("k8s/cm-{n}.yaml"), manifest(n))),
        );
        git::init_with_files(&dir, &files, "initial").expect("init");
        let repo = git::open_repo(&dir).expect("repo");
        for i in 0..size("BENCH_COMMITS", 300) {
            std::fs::write(dir.join("other.txt"), format!("{i}\n")).expect("write");
            git::commit_paths(&repo, &["other.txt".into()], "later").expect("commit");
        }
        let url = format!(
            "sqlite://{}?mode=rwc",
            tmp.path().join("db.sqlite").display()
        );
        let sync = SyncedRepo::open(
            &dir,
            DbConfig::Sqlite { url },
            FormatRegistry::with_builtins(),
        )
        .await
        .expect("open");
        let paths: Vec<_> = (0..size("BENCH_UNRELATED", 400))
            .map(|n| dir.join(format!("k8s/cm-{n}.yaml")))
            .collect();
        let start = std::time::Instant::now();
        let all: Vec<Vec<u8>> = paths
            .iter()
            .map(|p| std::fs::read(p).expect("read"))
            .collect();
        println!("read only: {:?}", start.elapsed());
        let start = std::time::Instant::now();
        for bytes in &all {
            git::blob_oid_for_bytes(&repo, bytes).expect("hash");
        }
        println!("hash only: {:?}", start.elapsed());
        let first = std::time::Instant::now();
        sync.update_from_working_dir(ScanOptions::default())
            .await
            .expect("first scan");
        println!("first scan: {:?}", first.elapsed());
        for n in 1..=3 {
            let start = std::time::Instant::now();
            let outcome = sync
                .update_from_working_dir(ScanOptions::default())
                .await
                .expect("scan");
            println!("unchanged scan {n}: {:?} ({outcome:?})", start.elapsed());
        }
    }
}
