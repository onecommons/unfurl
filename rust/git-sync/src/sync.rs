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
    apply_conflict_ops_in_pool, apply_pending_records, resolve_conflict_in_pool, Applying,
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
    BatchOp, BatchOutcome, CommitRollup, Record, RecordQuery, Resolution, RollupTxn, ScanOptions,
    SyncOutcome, Txn, TxnMeta, WriteFileOutcome, WriteOutcome,
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
    formats: FormatRegistry,
    worktree_id: i64,
    /// Root of this worktree's version family — see
    /// [`db::worktree::family_id`]. Resolved once at open because every
    /// write draws from it.
    family_id: i64,
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
struct HeadSide {
    /// Every blob in HEAD's tree, by path.
    blobs: std::collections::HashMap<String, gix::ObjectId>,
    /// The files whose blob isn't the one the committed segments hold.
    files: Vec<HeadFile>,
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
                formats,
                worktree_id,
                family_id,
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
    /// holding a commit HEAD `head` doesn't descend from (§4.8).
    async fn follow_head(&self, branch: &str, head: Option<&str>) -> Result<()> {
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
            return Ok(());
        };
        let repo = self.repo()?;
        if recorded == n || git::is_ancestor(&repo, recorded, n)? {
            return Ok(());
        }
        let files = self.head_files(&repo, Some(n), &Default::default())?.files;
        crate::fork::rebuild(
            self.db(),
            repo,
            self.formats(),
            self.worktree_id(),
            self.family_id(),
            n,
            &files,
        )
        .await
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
        self.follow_head(&meta.branch, head.as_deref()).await?;
        let tracked = git::tracked_files(&repo)?;
        let known_files: std::collections::HashMap<String, crate::model::File> =
            db::file::list(self.db(), self.worktree_id())
                .await?
                .into_iter()
                .map(|f| (f.path.clone(), f))
                .collect();
        let mut stats = SyncOutcome::default();

        // The committed side: HEAD's blobs against the committed segments'.
        let HeadSide {
            blobs: head_blobs,
            files: head_files,
        } = self.head_files(&repo, head.as_deref(), &known_files)?;
        let head_changed: BTreeSet<String> =
            head_files.iter().map(|f| f.rel_path.clone()).collect();

        /// What pass 1 learned about one tracked file.
        struct Candidate<'a> {
            tf: &'a git::TrackedFile,
            clean: bool,
            disk_blob: String,
            /// The parsed document, present when the bytes differ from
            /// what the database last took in, or HEAD changed the file.
            parsed_doc: Option<ParsedDoc<'a>>,
            db_commit: Option<String>,
        }

        let mut candidates: Vec<Candidate<'_>> = Vec::new();
        let mut walk_paths: Vec<String> = Vec::new();
        let mut indexed: std::collections::HashSet<&str> =
            std::collections::HashSet::with_capacity(tracked.len());
        let mut vanished: std::collections::HashSet<String> = std::collections::HashSet::new();

        for tf in &tracked {
            stats.files_seen += 1;
            indexed.insert(tf.rel_path.as_str());

            let Some(syntax) = Syntax::for_extension(&extract_ext(&tf.rel_path)) else {
                continue;
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
                continue;
            }
            let bytes = match std::fs::read(&tf.abs_path) {
                Ok(b) => b,
                // Still in the index, gone from the working tree: a
                // plain `rm`. Every other read failure leaves the file
                // alone rather than reading as a deletion.
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    vanished.insert(tf.rel_path.clone());
                    continue;
                }
                Err(_) => continue,
            };
            let disk_blob = git::blob_oid_for_bytes(&repo, &bytes);
            // Clean: what HEAD has, so nothing for a commit to carry.
            let clean = head_blobs.get(&tf.rel_path) == Some(&disk_blob);
            let disk_blob = disk_blob.to_string();

            let db_file = known_files.get(&tf.rel_path);
            let db_commit = db_file.and_then(|f| f.commit_id.clone());
            let parsed_doc = if !options.force
                && !head_changed.contains(&tf.rel_path)
                && db_file.is_some_and(|f| f.source_oid.as_deref() == Some(disk_blob.as_str()))
            {
                None
            } else {
                match self.parse_and_detect(&tf.rel_path, syntax, &bytes, &mut stats) {
                    Ok(Some(doc)) => Some(doc),
                    Ok(None) => continue,
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
                        continue;
                    }
                }
            };
            walk_paths.push(tf.rel_path.clone());
            candidates.push(Candidate {
                tf,
                clean,
                disk_blob,
                parsed_doc,
                db_commit,
            });
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
        let last_commits = git::last_commits_for_paths(&repo, &walk_paths)?;
        let mut resolves: std::collections::HashMap<String, Option<i64>> =
            std::collections::HashMap::new();
        let mut docs: Vec<(Candidate<'_>, ParsedDoc<'_>, Option<i64>)> = Vec::new();
        for c in candidates {
            let record_commit_id: Option<&str> =
                last_commits.get(&c.tf.rel_path).map(String::as_str);
            let file_commit_id: Option<&str> = if c.clean { record_commit_id } else { None };
            let resolves_version = record_commit_id.and_then(|commit| {
                *resolves.entry(commit.to_string()).or_insert_with(|| {
                    git::commit_message(&repo, commit)
                        .as_deref()
                        .and_then(resolves_version_from_message)
                })
            });
            let mut parsed_doc = c.parsed_doc;
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
            docs.push((
                Candidate {
                    parsed_doc: None,
                    ..c
                },
                doc,
                resolves_version,
            ));
        }

        let mut disk_files: Vec<DiskFile<'_>> = Vec::new();
        for (c, doc, resolves_version) in &docs {
            let record_commit_id = last_commits.get(&c.tf.rel_path).map(String::as_str);
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
    /// committed segments hold, parsed from `head`.
    fn head_files(
        &self,
        repo: &gix::Repository,
        head: Option<&str>,
        known_files: &std::collections::HashMap<String, crate::model::File>,
    ) -> Result<HeadSide> {
        let head_blobs: std::collections::HashMap<String, gix::ObjectId> = match head {
            Some(h) => git::tree_blobs(repo, h)?,
            None => Default::default(),
        };
        let mut head_files: Vec<HeadFile> = Vec::new();
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
            let now = head_blobs.get(path).map(|o| o.to_string());
            let known = known_files.get(path);
            if now == known.and_then(|f| f.committed_oid.clone()) {
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
            let doc = match &bytes {
                Some(bytes) => self
                    .parse_and_detect(path, syntax, bytes, &mut ignored)
                    .ok()
                    .flatten(),
                None => None,
            };
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
        parse_and_detect(self.formats(), rel_path, syntax, bytes, stats)
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
        db::worktree::matching(self.db(), filter).await
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
        let file_row = db::file::get(self.db(), self.worktree_id(), file_path).await?;
        let abs_path = self.inner.repo_path.join(file_path);
        // Divergences already on record: the file's side of these stands
        // whether or not the file has moved since, so they are loaded
        // unconditionally.
        let conflict_rows: std::collections::HashMap<(String, String), Record> =
            db::record::list_conflicts(self.db(), self.worktree_id(), Some(file_path))
                .await?
                .into_iter()
                .map(|r| ((r.path.clone(), r.key.clone()), r))
                .collect();

        // Checked before the pending-is-empty exit below, because a
        // deletion whose tombstones have already been purged leaves
        // nothing pending and would otherwise never reach the disk.
        if let Some(outcome) = self
            .remove_deleted_file(file_path, &abs_path, file_row.as_ref(), &conflict_rows)
            .await?
        {
            return Ok(outcome);
        }

        let pending = db::record::load_pending(self.db(), self.worktree_id(), file_path).await?;
        if pending.is_empty() {
            return Ok(WriteFileOutcome::default());
        }

        let format = pending
            .iter()
            .find_map(|rec| self.formats().for_path(&rec.path));

        let syntax = Syntax::for_extension(&extract_ext(file_path))
            .ok_or_else(|| Error::Other(format!("{file_path}: unsupported file extension")))?;
        let bases = db::record::pending_bases(self.db(), self.worktree_id(), file_path).await?;

        // A stale source means the disk holds an edit this database
        // never took in — the apply below must check each pending
        // record against its base for collisions with that edit.
        let stale = self.source_stale(file_row.as_ref(), &abs_path)?;
        let check = match stale {
            true => Some(ConflictCheck {
                base_values: db::record::pending_base_values(
                    self.db(),
                    self.worktree_id(),
                    file_path,
                )
                .await?,
            }),
            false => None,
        };

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
        // Conflict bookkeeping lands even when nothing is written: the
        // divergences it records are why there is nothing to write.
        self.apply_conflict_ops(
            file_path,
            file_row.and_then(|f| f.commit_id).as_deref(),
            &ops,
        )
        .await?;
        if touched.is_empty() {
            return Ok(WriteFileOutcome {
                written: None,
                deleted: false,
                conflicts,
            });
        }
        // Markdown is rendered against the document's own blocks rather
        // than from `root`, so it needs the source and the list of
        // records actually applied -- neither of which a
        // value-to-bytes function can take.
        let render = |source: Option<&str>, root: &mut serde_json::Value| match source
            .filter(|_| syntax == Syntax::Markdown)
        {
            Some(src) => unfurl_merge::markdown::render(
                file_path,
                src,
                root,
                &applied,
                format.map(|f| f.path_prefixes()).unwrap_or_default(),
            )
            .map_err(Error::from),
            None => syntax.render_document(source, root, format, &touched, file_path),
        };
        let bytes = render(source.as_deref(), &mut root)?;
        let bytes = self.unless_head_has(file_path, syntax, bytes, render)?;
        self.persist_render(file_path, &abs_path, &bytes, stale)
            .await?;
        Ok(WriteFileOutcome {
            written: Some(abs_path),
            deleted: false,
            conflicts,
        })
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

    /// Take a file the database has deleted off the disk, or retract the
    /// deletion when something is still in it.
    ///
    /// `Some` means the file is gone and [`Self::write_file`] is done;
    /// `None` means there was no deletion to act on, or one that no
    /// longer holds because a record was written back or is contested —
    /// a file with content plainly exists.
    async fn remove_deleted_file(
        &self,
        file_path: &str,
        abs: &Path,
        file_row: Option<&crate::model::File>,
        conflict_rows: &std::collections::HashMap<(String, String), Record>,
    ) -> Result<Option<WriteFileOutcome>> {
        if !file_row.is_some_and(|f| f.deleted) {
            return Ok(None);
        }
        let live = db::record::find(
            self.db(),
            self.worktree_id(),
            &RecordQuery {
                file_path: Some(file_path.to_string()),
                limit: Some(1),
                ..Default::default()
            },
        )
        .await?;
        if !live.is_empty() || !conflict_rows.is_empty() {
            db::file::set_deleted(self.db(), self.worktree_id(), file_path, false).await?;
            return Ok(None);
        }
        // `remove_file` on an absent file is the already-done case, not
        // a failure.
        let removed = match std::fs::remove_file(abs) {
            Ok(()) => true,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => false,
            Err(e) => return Err(Error::Io(e)),
        };
        Ok(Some(WriteFileOutcome {
            written: removed.then(|| abs.to_path_buf()),
            deleted: true,
            conflicts: Vec::new(),
        }))
    }

    /// Put the rendered bytes on disk, stamping `source_oid` only when
    /// the database still describes what the file held.
    ///
    /// A stale write deliberately leaves `source_oid` naming the old
    /// bytes: the render just merged over an edit this database never
    /// took in, so the next scan has to see the mismatch and take the
    /// merged file in. Stamping the rendered oid would hide the hand
    /// edit from every future scan.
    async fn persist_render(
        &self,
        file_path: &str,
        abs: &Path,
        bytes: &[u8],
        stale: bool,
    ) -> Result<()> {
        let tmp = stage_write(abs, bytes)?;
        if stale {
            return tmp.persist(abs).map(|_| ()).map_err(|e| Error::Io(e.error));
        }
        // Write and flush outside the transaction; only the rename goes
        // inside it, so the two records of "the file now holds these
        // bytes" -- the file itself and `source_oid` -- commit together.
        let oid = git::blob_oid_for_bytes(&self.repo()?, bytes).to_string();
        db::file::commit_write(self.db(), self.worktree_id(), file_path, &oid, || {
            tmp.persist(abs).map(|_| ()).map_err(|e| Error::Io(e.error))
        })
        .await
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
        Ok(git::blob_oid_for_bytes(&self.repo()?, &bytes).to_string() != expected)
    }

    /// Persist the conflict bookkeeping one [`Self::write_file`] worked
    /// out, in a single transaction.
    ///
    /// Separate from the render because the render is a pure function
    /// over the document: it decides *what* the divergences are, this
    /// writes them down. Only the conflict rows are touched — a write
    /// never rewrites a record, which is the whole point of skipping a
    /// conflicted one.
    async fn apply_conflict_ops(
        &self,
        file_path: &str,
        commit_id: Option<&str>,
        ops: &[ConflictOp],
    ) -> Result<()> {
        if ops.is_empty() {
            return Ok(());
        }
        on_pool!(self.db(), pool => apply_conflict_ops_in_pool(self, pool, file_path, commit_id, ops).await)
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
    /// When outstanding `txn` audit rows exist (batches applied with a
    /// [`TxnMeta`] since the last commit), a "Rollup of N git-sync
    /// transactions" section listing them is appended to `message` —
    /// one commit carries many batches, so this is where their
    /// individual authors and messages survive. Those rows are stamped
    /// with the new oid alongside the records.
    ///
    /// Returns the new commit oid as a hex string, or `None` when no
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
    pub async fn commit_repository(&self, message: &str) -> Result<Option<String>> {
        // Take any outside edits in before saving: the writes below
        // then render over a current picture, and the fold can't
        // stamp a stale row with a commit that doesn't carry its json.
        let (_, head) = self.scan(ScanOptions::default()).await?;
        self.commit_scanned(message, head).await
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

        // Read the audit rows before the fold stamps them: this
        // commit is the one that carries their writes. Each batch's
        // records are resolved here too — also before the fold,
        // which purges the tombstones a delete leaves behind.
        let files: BTreeSet<String> = dirty.iter().cloned().collect();
        let txns = db::commit::list_outstanding(self.db(), self.worktree_id()).await?;
        let worktree = db::worktree::get(self.db(), self.worktree_id()).await?;
        let mut records =
            db::record::committed_records(self.db(), self.worktree_id(), &files, watermark).await?;
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
        let message = build_commit_message(message, &rollup);
        // the commit's head rows take the ids its rollup names
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
        let head_side = self.head_files(&repo, Some(&oid_str), &known_files)?;
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

#[cfg(test)]
mod head_race_tests {
    use super::*;
    use crate::{DbConfig, FormatRegistry};

    const CLOUDMAP: &str = "apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories: {}\n";

    async fn open(dir: &Path) -> SyncedRepo {
        let files = [("cloudmap.yaml".to_string(), CLOUDMAP.as_bytes().to_vec())];
        git::init_with_files(dir, &files, "initial").expect("init");
        let db = DbConfig::Sqlite {
            url: "sqlite::memory:".into(),
        };
        SyncedRepo::open(dir, db, FormatRegistry::with_builtins())
            .await
            .expect("open")
    }

    /// An outside commit landing between a commit's scan and the commit
    /// itself fails the commit rather than being built on unseen, and a
    /// retry commits the same edit onto it.
    #[tokio::test]
    async fn a_commit_fails_when_head_moves_after_its_scan() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let sync = open(tmp.path()).await;
        sync.update_from_working_dir(ScanOptions::default())
            .await
            .expect("scan");
        let key = "git://example.com/a.git";
        sync.upsert_record(
            Some("cloudmap.yaml"),
            "/repositories",
            key,
            serde_json::json!({"name": "a"}),
            None,
            false,
        )
        .await
        .expect("upsert");

        let (_, scanned) = sync.scan(ScanOptions::default()).await.expect("scan");
        std::fs::write(tmp.path().join("other.txt"), "outside\n").expect("write");
        let outside = git::commit_paths(&sync.repo().expect("repo"), &["other.txt".into()], "out")
            .expect("outside commit");

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
        let repo = sync.repo().expect("repo");
        assert_eq!(repo.head_id().expect("head").detach(), outside);

        let oid = sync
            .commit_repository("retry")
            .await
            .expect("retry")
            .expect("a commit");
        let commit = repo
            .find_commit(gix::ObjectId::from_hex(oid.as_bytes()).expect("oid"))
            .expect("commit");
        assert_eq!(
            commit.parent_ids().next().map(|p| p.detach()),
            Some(outside)
        );
        let committed = git::read_blob_at_commit(&repo, &oid, "cloudmap.yaml")
            .expect("read")
            .expect("cloudmap.yaml in the commit");
        assert!(String::from_utf8_lossy(&committed).contains(key));
    }
}
