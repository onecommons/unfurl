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

use std::collections::BTreeSet;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::db::RecordId;
use crate::db::{self, Db, DbConfig};
use crate::error::{Error, Result};
use crate::format::FormatRegistry;
use crate::git;
use crate::model::{
    Applied, BatchOp, BatchOutcome, Failed, Record, RecordQuery, Txn, TxnMeta, UpdateStats,
    WriteOutcome,
};

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

        // Inspect the repo once at open time to get origin/branch.
        let repo = git::open_repo(&repo_path)?;
        let meta = git::worktree_meta(&repo)?;
        // Drop `repo` here; we re-open per call to keep `Send` guarantees
        // out of long-lived state.
        drop(repo);

        let worktree_id = db::worktree::upsert(&db, &meta.origin, &meta.branch).await?;

        Ok(Self {
            inner: Arc::new(SyncedRepoInner {
                db,
                repo_path,
                formats,
                worktree_id,
            }),
        })
    }

    fn repo(&self) -> Result<gix::Repository> {
        git::open_repo(&self.inner.repo_path)
    }

    fn worktree_id(&self) -> i64 {
        self.inner.worktree_id
    }

    fn db(&self) -> &Db {
        &self.inner.db
    }

    fn formats(&self) -> &FormatRegistry {
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
    /// Two-pass implementation: the first pass parses each tracked
    /// `.yaml` / `.yml` / `.json` file and classifies it via the
    /// registry's [`FormatRegistry::detect`]; the second pass resolves
    /// the last-commit oid per "clean" file (where the on-disk blob
    /// matches the index entry) by walking ancestors of HEAD once via
    /// [`crate::git::last_commits_for_paths`]. Dirty files get
    /// `commit_id = None`. After indexing, the worktree's
    /// `commit_id` is bumped to HEAD.
    ///
    /// # Errors
    ///
    /// Returns [`crate::Error::Yaml`] / [`crate::Error::Json`] when a
    /// tracked file fails to parse, or any underlying git / database
    /// error.
    pub async fn update_from_working_dir(&self) -> Result<UpdateStats> {
        let repo = self.repo()?;
        let meta = git::worktree_meta(&repo)?;
        let head_oid_str = meta.head_oid.map(|oid| oid.to_string());
        let tracked = git::tracked_files(&repo)?;

        // First pass: parse + classify each tracked file. We collect
        // the work to do AND the set of "clean" files (disk blob ==
        // index blob) so we can resolve their last-commit oids in a
        // single backwards walk afterwards.
        struct Pending<'a> {
            tf: &'a git::TrackedFile,
            format_name: String,
            value: serde_json::Value,
            clean: bool,
        }

        let mut stats = UpdateStats::default();
        let mut pending: Vec<Pending<'_>> = Vec::new();
        let mut clean_paths: Vec<String> = Vec::new();

        for tf in &tracked {
            stats.files_seen += 1;

            let ext = match tf.rel_path.rsplit_once('.') {
                Some((_, ext)) => ext.to_ascii_lowercase(),
                None => continue,
            };
            if !matches!(ext.as_str(), "yaml" | "yml" | "json") {
                continue;
            }

            let bytes = match std::fs::read(&tf.abs_path) {
                Ok(b) => b,
                Err(_) => continue,
            };
            let text = match std::str::from_utf8(&bytes) {
                Ok(s) => s,
                Err(_) => continue,
            };

            let value: serde_json::Value = if ext == "json" {
                serde_json::from_str(text).map_err(|e| Error::Json {
                    path: tf.rel_path.clone(),
                    source: e,
                })?
            } else {
                serde_saphyr::from_str(text).map_err(|e| Error::Yaml {
                    path: tf.rel_path.clone(),
                    message: e.to_string(),
                })?
            };

            let Some(format) = self.formats().detect(&value) else {
                continue;
            };
            let format_name = format.name().to_string();

            let disk_blob = git::blob_oid_for_disk_file(&repo, &tf.abs_path)?;
            let clean = disk_blob == tf.head_blob_oid;
            if clean {
                clean_paths.push(tf.rel_path.clone());
            }
            pending.push(Pending {
                tf,
                format_name,
                value,
                clean,
            });
        }

        // Second pass: resolve the actual last-commit oid for every
        // clean file via a single backwards walk from HEAD. Dirty files
        // get NULL.
        let last_commits = git::last_commits_for_paths(&repo, &clean_paths)?;

        for p in pending {
            let last_commit_id: Option<&str> = if p.clean {
                last_commits.get(&p.tf.rel_path).map(|s| s.as_str())
            } else {
                None
            };
            // It's possible (though unusual) for a clean file to not
            // appear in any commit on this branch — e.g. it was added
            // to the index but never committed. Treat as NULL.
            let format = match self.formats().by_name(&p.format_name) {
                Some(f) => f,
                None => continue,
            };

            stats.files_updated += 1;
            self.upsert_file_and_records(
                &p.tf.rel_path,
                last_commit_id,
                &p.value,
                format,
                &mut stats,
            )
            .await?;
        }

        // Update worktree.commit_id to HEAD.
        if let Some(oid) = head_oid_str {
            db::worktree::update_commit(self.db(), self.worktree_id(), Some(&oid)).await?;
        }

        // Auto-pick a default file_path for new records on the first
        // run. No-op when an operator has already pinned a value.
        db::worktree::auto_pick_default_file(self.db(), self.worktree_id()).await?;

        Ok(stats)
    }

    /// Sync one parsed file into the DB: upsert the file row, upsert
    /// every record found in it, and delete records that vanished —
    /// all inside a single SQL transaction (see
    /// [`upsert_file_and_records_inner`]).
    async fn upsert_file_and_records(
        &self,
        rel_path: &str,
        last_commit_id: Option<&str>,
        value: &serde_json::Value,
        format: &dyn crate::format::DataFormat,
        stats: &mut UpdateStats,
    ) -> Result<()> {
        match self.db() {
            Db::Sqlite(pool) => {
                upsert_file_and_records_inner(
                    self,
                    pool,
                    rel_path,
                    last_commit_id,
                    value,
                    format,
                    stats,
                )
                .await
            }
            #[cfg(feature = "postgres")]
            Db::Postgres(pool) => {
                upsert_file_and_records_inner(
                    self,
                    pool,
                    rel_path,
                    last_commit_id,
                    value,
                    format,
                    stats,
                )
                .await
            }
        }
    }

    /// Search records by optional `file_path` / `path` / `key` filters.
    ///
    /// All `Some(...)` filters are AND'd together; `None` matches any
    /// value. With `alias = true` and a `key` filter, a record also
    /// matches when one of its [`crate::Alias`] rows has that key
    /// (joined on `record_id`). Without a `key` filter, `alias` is a
    /// no-op. Tombstoned records are hidden.
    ///
    /// `type_names`, when set and non-empty, restricts results to
    /// records whose JSON payload declares one of the given names as a
    /// key of its `type` object (the cloudmap `typeRef` shape).
    /// Matching is by exact name — expand subtypes first (e.g. via the
    /// `extends` closure of the `/types` section) to match a type
    /// hierarchy.
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
        db::record::find(self.db(), self.worktree_id(), query).await
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

    /// Change-detection probe for a section: `(COUNT(*),
    /// MAX(version))` over every row (tombstones included) whose
    /// `record.path` equals `path`.
    ///
    /// The pair moves whenever the section's contents change and only
    /// then — suitable as a cache key for derived data (e.g. the
    /// `extends` closure of the `/types` section).
    pub async fn section_stat(&self, path: &str) -> Result<(i64, Option<i64>)> {
        db::record::section_stat(self.db(), self.worktree_id(), path).await
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
            .follow_records(&initial, follow, exclude, query.since_version)
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
        let mut format_cache: std::collections::HashMap<String, Option<String>> =
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
                let format_name = match format_cache.get(&rec.file_path) {
                    Some(v) => v.clone(),
                    None => {
                        let f =
                            db::file::get(self.db(), self.worktree_id(), &rec.file_path).await?;
                        let name = f.map(|f| f.format);
                        format_cache.insert(rec.file_path.clone(), name.clone());
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

    /// Returns the record with the given primary key.
    ///
    /// Unlike [`Self::get_record`], this does **not** hide tombstoned
    /// rows — useful for tests and for inspecting the in-flight delete
    /// state.
    pub async fn get_record_by_id(&self, id: i64) -> Result<Option<Record>> {
        db::record::get_by_id(self.db(), id).await
    }

    /// Returns the [`crate::model::File`] row for `file_path` within
    /// this worktree, or `None` if no row exists.
    pub async fn get_file(&self, file_path: &str) -> Result<Option<crate::model::File>> {
        db::file::get(self.db(), self.worktree_id(), file_path).await
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
    pub async fn list_changes(&self, since: Option<i64>) -> Result<Vec<Record>> {
        db::record::list_changes(self.db(), self.worktree_id(), since).await
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
    pub async fn create_record(
        &self,
        file_path: Option<&str>,
        path: &str,
        key: &str,
        json: serde_json::Value,
        expected_commit: Option<CommitRef>,
    ) -> Result<WriteOutcome> {
        // Dispatch on the concrete pool type; the body is shared via the
        // generic `crud_create_in_pool` (same pattern as `apply_batch`).
        match self.db() {
            Db::Sqlite(pool) => {
                crud_create_in_pool(self, pool, file_path, path, key, json, expected_commit).await
            }
            #[cfg(feature = "postgres")]
            Db::Postgres(pool) => {
                crud_create_in_pool(self, pool, file_path, path, key, json, expected_commit).await
            }
        }
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
    pub async fn update_record(
        &self,
        file_path: Option<&str>,
        path: &str,
        key: &str,
        json: serde_json::Value,
        expected_commit: Option<CommitRef>,
    ) -> Result<WriteOutcome> {
        match self.db() {
            Db::Sqlite(pool) => {
                crud_update_in_pool(self, pool, file_path, path, key, json, expected_commit).await
            }
            #[cfg(feature = "postgres")]
            Db::Postgres(pool) => {
                crud_update_in_pool(self, pool, file_path, path, key, json, expected_commit).await
            }
        }
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
    pub async fn upsert_record(
        &self,
        file_path: Option<&str>,
        path: &str,
        key: &str,
        json: serde_json::Value,
        expected_commit: Option<CommitRef>,
    ) -> Result<WriteOutcome> {
        match self.db() {
            Db::Sqlite(pool) => {
                crud_upsert_in_pool(self, pool, file_path, path, key, json, expected_commit).await
            }
            #[cfg(feature = "postgres")]
            Db::Postgres(pool) => {
                crud_upsert_in_pool(self, pool, file_path, path, key, json, expected_commit).await
            }
        }
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
    pub async fn delete_record(
        &self,
        file_path: Option<&str>,
        path: &str,
        key: &str,
        expected_commit: Option<CommitRef>,
    ) -> Result<WriteOutcome> {
        match self.db() {
            Db::Sqlite(pool) => {
                crud_delete_in_pool(self, pool, file_path, path, key, expected_commit).await
            }
            #[cfg(feature = "postgres")]
            Db::Postgres(pool) => {
                crud_delete_in_pool(self, pool, file_path, path, key, expected_commit).await
            }
        }
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
        match self.db() {
            Db::Sqlite(pool) => apply_batch_inner(self, pool, ops, atomic, meta).await,
            #[cfg(feature = "postgres")]
            Db::Postgres(pool) => apply_batch_inner(self, pool, ops, atomic, meta).await,
        }
    }

    /// Every `txn` audit row of this worktree, oldest version range
    /// first — both outstanding batches and ones already carried into a
    /// commit (distinguished by [`Txn::commit_id`]).
    pub async fn list_transactions(&self) -> Result<Vec<Txn>> {
        db::commit::list_all(self.db(), self.worktree_id()).await
    }

    /// Persist every in-flight record edit to disk.
    ///
    /// For each file with at least one record whose `commit_id IS
    /// NULL`, calls [`Self::write_file`]. Returns the paths that were
    /// actually rewritten — files whose bytes turned out to match
    /// what's already on disk are skipped.
    pub async fn save_changes(&self) -> Result<Vec<PathBuf>> {
        let dirty: Vec<String> =
            db::record::list_dirty_files(self.db(), self.worktree_id()).await?;
        let mut written = Vec::new();
        for fp in dirty {
            if let Some(p) = self.write_file(&fp).await? {
                written.push(p);
            }
        }
        Ok(written)
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
    /// Returns `Some(path)` when the on-disk bytes changed, `None`
    /// when the freshly-rendered output matches what was already on
    /// disk.
    ///
    /// # Errors
    ///
    /// [`crate::Error::Yaml`] / [`crate::Error::Json`] on parse / emit
    /// failure; [`crate::Error::Io`] for filesystem failures.
    pub async fn write_file(&self, file_path: &str) -> Result<Option<PathBuf>> {
        let pending = db::record::load_pending(self.db(), self.worktree_id(), file_path).await?;
        if pending.is_empty() {
            return Ok(None);
        }

        let format = pending
            .iter()
            .find_map(|rec| self.formats().for_path(&rec.path));

        let abs = self.inner.repo_path.join(file_path);
        let ext = extract_ext(file_path);

        let existed = abs.exists();
        let mut root = load_root(&abs, file_path, &ext)?;
        if !existed {
            // Brand-new document
            if let (Some(fmt), Some(root_obj)) = (format, root.as_object_mut()) {
                if let Some(header) = fmt.new_document().as_object() {
                    for (k, v) in header {
                        root_obj.insert(k.clone(), v.clone());
                    }
                }
            }
        }
        let touched = apply_pending_records(&mut root, pending, format);
        apply_format_ordering(&mut root, format, &touched);
        let bytes = serialize_root(&root, file_path, &ext)?;

        if let Ok(existing) = std::fs::read(&abs) {
            if existing == bytes {
                return Ok(None);
            }
        }

        atomic_write(&abs, &bytes)?;
        Ok(Some(abs))
    }

    /// Persist all pending edits and create a git commit.
    ///
    /// Equivalent to [`Self::save_changes`], followed by a gix commit
    /// of the dirty paths under `message`, followed by rolling the
    /// new commit oid into every affected `record` / `file` /
    /// `worktree` row in a single transaction. Tombstones for the
    /// committed paths are purged at the same time.
    ///
    /// When outstanding `txn` audit rows exist (batches applied with a
    /// [`TxnMeta`] since the last commit), a "Rollup of N git-sync
    /// transactions" section listing them is appended to `message` —
    /// one commit carries many batches, so this is where their
    /// individual authors and messages survive. Those rows are stamped
    /// with the new oid alongside the records.
    ///
    /// Returns the new commit oid as a hex string, or `None` when
    /// there was nothing to commit (no in-flight records).
    ///
    /// # Errors
    ///
    /// Surfaces [`crate::Error::Git`] if gix can't construct the
    /// commit, [`crate::Error::Io`] for filesystem trouble during
    /// `save_changes`, and any underlying database error.
    pub async fn commit_repository(&self, message: &str) -> Result<Option<String>> {
        // Snapshot the dirty file list *before* save_changes (which sets
        // no commit_id changes) so we know what to stage even when bytes
        // didn't actually change on disk.
        let dirty: Vec<String> =
            db::record::list_dirty_files(self.db(), self.worktree_id()).await?;
        if dirty.is_empty() {
            return Ok(None);
        }
        let _written = self.save_changes().await?;

        // Read the audit rows before roll_forward stamps them: this
        // commit is the one that carries their writes.
        let txns = db::commit::list_outstanding(self.db(), self.worktree_id()).await?;
        let message = build_commit_message(message, &txns);

        // Create the commit using gix.
        let repo = self.repo()?;
        let oid = git::commit_paths(&repo, &dirty, &message)?;
        let oid_str = oid.to_string();

        // Roll the commit id into the dirty rows in a single transaction.
        db::commit::roll_forward(self.db(), self.worktree_id(), &dirty, &oid_str).await?;

        Ok(Some(oid_str))
    }
}

/// `subject` with a rollup section describing `txns` appended, or
/// `subject` unchanged when there are none.
///
/// A single git commit carries every batch applied since the last one,
/// so the per-batch author and message would otherwise be lost. The
/// section renders as:
///
/// ```text
/// Rollup of 2 git-sync transactions:
///
///  - 45-66 on 1 Thu Jul 23 13:10:21 2026 -0700 Author Name <author@emails.org>:
///      A commit message for the first batch
///  - 67 on 1 Thu Jul 23 13:11:02 2026 -0700:
///      Another commit message
/// ```
///
/// — where `45-66` is the [`Txn`] version range (collapsed to one
/// number when the batch stamped a single version) and `1` is the
/// worktree id. The author is omitted when unknown, as are the indented
/// message lines when the batch carried no message.
fn build_commit_message(subject: &str, txns: &[Txn]) -> String {
    if txns.is_empty() {
        return subject.to_string();
    }
    let plural = if txns.len() == 1 { "" } else { "s" };
    let mut out = format!(
        "{subject}\n\nRollup of {} git-sync transaction{plural}:\n\n",
        txns.len()
    );
    for txn in txns {
        let range = if txn.first_version == txn.last_version {
            txn.first_version.to_string()
        } else {
            format!("{}-{}", txn.first_version, txn.last_version)
        };
        // Never fail a commit over a timestamp: an unparseable stamp is
        // reported as stored.
        let date = chrono::DateTime::parse_from_rfc3339(&txn.created_at)
            .map(|dt| dt.format("%a %b %e %H:%M:%S %Y %z").to_string())
            .unwrap_or_else(|_| txn.created_at.clone());
        let author = match &txn.author {
            Some(a) => format!(" {a}"),
            None => String::new(),
        };
        out.push_str(&format!(
            " - {range} on {} {date}{author}:\n",
            txn.worktree_id
        ));
        if let Some(message) = &txn.message {
            for line in message.lines() {
                out.push_str(&format!("     {line}\n"));
            }
        }
    }
    out
}

/// Lower-cased extension of `file_path`, or empty when there isn't one.
fn extract_ext(file_path: &str) -> String {
    file_path
        .rsplit_once('.')
        .map(|(_, e)| e.to_ascii_lowercase())
        .unwrap_or_default()
}

/// Load the on-disk document at `abs` as a `serde_json::Value` and
/// guarantee it's an object. A missing file yields an empty object;
/// a non-object root is replaced with one.
fn load_root(abs: &Path, file_path: &str, ext: &str) -> Result<serde_json::Value> {
    let mut root: serde_json::Value = match std::fs::read(abs) {
        Ok(bytes) => parse_bytes_for_extension(file_path, ext, &bytes)?,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            serde_json::Value::Object(serde_json::Map::new())
        }
        Err(e) => return Err(Error::Io(e)),
    };
    if !root.is_object() {
        root = serde_json::Value::Object(serde_json::Map::new());
    }
    Ok(root)
}

/// Apply `format`'s per-section ordering policy to the sections this batch
/// wrote into. No-op when the records matched no registered format, or when
/// the format opts out of sorting for a section.
fn apply_format_ordering(
    root: &mut serde_json::Value,
    format: Option<&dyn crate::DataFormat>,
    touched_sections: &[String],
) {
    let (Some(fmt), Some(root_obj)) = (format, root.as_object_mut()) else {
        return;
    };
    for section_name in touched_sections {
        if !matches!(fmt.get_order(section_name), crate::Order::Sort) {
            continue;
        }
        if let Some(section_obj) = root_obj
            .get_mut(section_name.as_str())
            .and_then(|v| v.as_object_mut())
        {
            section_obj.sort_keys();
        }
    }
}

/// Apply every pending record to `root` in order. Returns the list of
/// top-level section names this batch touched (insertion-order, no
/// duplicates) so callers can re-sort just those sections.
fn apply_pending_records(
    root: &mut serde_json::Value,
    pending: Vec<Record>,
    format: Option<&dyn crate::DataFormat>,
) -> Vec<String> {
    let mut touched: Vec<String> = Vec::new();
    for rec in pending {
        let section_name = rec.path.trim_start_matches('/').to_string();
        // v1 supports single-segment parents only.
        if section_name.is_empty() {
            continue;
        }
        let root_obj = root.as_object_mut().expect("root is object");
        if rec.deleted {
            apply_delete(root_obj, &section_name, &rec.key);
        } else {
            apply_insert(root_obj, &section_name, rec.key, rec.json, format);
        }
        if !touched.contains(&section_name) {
            touched.push(section_name);
        }
    }
    touched
}

/// Remove `key` from `root_obj[section_name]`. Drops the section
/// entirely when it becomes empty. Uses `shift_remove` (not
/// `remove`/`swap_remove`) so the order of the surviving entries is
/// preserved — critical for the "minimally-edited" output the tests
/// assert against.
fn apply_delete(
    root_obj: &mut serde_json::Map<String, serde_json::Value>,
    section_name: &str,
    key: &str,
) {
    if let Some(section) = root_obj
        .get_mut(section_name)
        .and_then(|v| v.as_object_mut())
    {
        section.shift_remove(key);
        if section.is_empty() {
            root_obj.shift_remove(section_name);
        }
    }
}

/// Insert or replace `root_obj[section_name][key] = json`, creating
/// the section if it's missing and replacing any non-object value.
fn apply_insert(
    root_obj: &mut serde_json::Map<String, serde_json::Value>,
    section_name: &str,
    key: String,
    json: serde_json::Value,
    format: Option<&dyn crate::DataFormat>,
) {
    let section = root_obj
        .entry(section_name.to_string())
        .or_insert_with(|| serde_json::Value::Object(serde_json::Map::new()));
    if !section.is_object() {
        *section = serde_json::Value::Object(serde_json::Map::new());
    }
    let section = section.as_object_mut().expect("section is object");
    let json = match section.get(&key) {
        Some(previous) => reorder_like(previous, json),
        // Nothing on disk to copy an order from, so fall back to the
        // format's canonical one.
        None => match format {
            Some(fmt) => order_fields(json, fmt.field_order(section_name)),
            None => json,
        },
    };
    section.insert(key, json);
}

/// Emit `json`'s top-level keys in `order`, appending any it doesn't
/// name in the order they arrived. A non-object, or an empty `order`,
/// passes through untouched.
fn order_fields(json: serde_json::Value, order: &[&str]) -> serde_json::Value {
    let serde_json::Value::Object(mut object) = json else {
        return json;
    };
    if order.is_empty() {
        return serde_json::Value::Object(object);
    }
    let mut out = serde_json::Map::with_capacity(object.len());
    for key in order {
        if let Some(value) = object.shift_remove(*key) {
            out.insert((*key).to_string(), value);
        }
    }
    out.extend(object);
    serde_json::Value::Object(out)
}

/// Re-key `next` to follow `previous`'s key order, recursively.
///
/// The database is an index of the file, not its author. A record read
/// back out carries whatever order the backend stored it in — the
/// writing client's on SQLite, and on Postgres `JSONB`'s normalised
/// order (keys sorted by length, then bytewise). Neither is the order
/// the file was written in, so writing a record back verbatim would
/// rewrite its whole block instead of the field that changed.
///
/// Mirroring the on-disk block keeps the diff down to the actual edit,
/// and does it at every depth: nested objects, maps keyed by data
/// rather than by schema, and objects nested inside arrays all keep the
/// order the file already had. Keys `previous` doesn't have are
/// appended in the order they arrived, so nothing is dropped and
/// additions still show up in the diff.
///
/// A record with no counterpart on disk has nothing to mirror and is
/// written in the order it arrived; see [`crate::DataFormat`] for the
/// canonical field order applied to those.
fn reorder_like(previous: &serde_json::Value, next: serde_json::Value) -> serde_json::Value {
    use serde_json::Value;
    match (previous, next) {
        (Value::Object(previous), Value::Object(mut next)) => {
            let mut out = serde_json::Map::with_capacity(next.len());
            for (key, previous_value) in previous {
                if let Some(value) = next.shift_remove(key) {
                    out.insert(key.clone(), reorder_like(previous_value, value));
                }
            }
            // Whatever is left is new to the file; keep it in arrival order.
            out.extend(next);
            Value::Object(out)
        }
        // Arrays keep their element order (both backends preserve it),
        // but objects *inside* them are subject to the same rewrite, so
        // pair them up positionally.
        (Value::Array(previous), Value::Array(next)) => Value::Array(
            next.into_iter()
                .enumerate()
                .map(|(i, value)| match previous.get(i) {
                    Some(previous_value) => reorder_like(previous_value, value),
                    None => value,
                })
                .collect(),
        ),
        (_, next) => next,
    }
}

/// Serialize `root` as YAML or JSON depending on `ext`.
fn serialize_root(root: &serde_json::Value, file_path: &str, ext: &str) -> Result<Vec<u8>> {
    match ext {
        "yaml" | "yml" => {
            let s = serde_saphyr::to_string(root).map_err(|e| Error::Yaml {
                path: file_path.to_string(),
                message: e.to_string(),
            })?;
            Ok(crate::util::elide_explicit_nulls(&s).into_bytes())
        }
        "json" => serde_json::to_vec_pretty(root).map_err(|e| Error::Json {
            path: file_path.to_string(),
            source: e,
        }),
        other => Err(Error::Other(format!("unsupported extension: {other}"))),
    }
}

/// Atomic-replace `abs` with `bytes` via a tempfile in the same
/// directory. Creates the parent directory if needed.
fn atomic_write(abs: &Path, bytes: &[u8]) -> Result<()> {
    let dir = abs.parent().unwrap_or_else(|| Path::new("."));
    std::fs::create_dir_all(dir)?;
    let mut tmp = tempfile::NamedTempFile::new_in(dir)?;
    tmp.write_all(bytes)?;
    tmp.flush()?;
    tmp.persist(abs).map_err(|e| Error::Io(e.error))?;
    Ok(())
}

/// Parse `bytes` as YAML or JSON depending on the file extension and
/// convert the result into a `serde_json::Value`. The crate enables
/// `serde_json/preserve_order`, so the resulting value's object keys
/// retain on-disk ordering.
fn parse_bytes_for_extension(
    file_path: &str,
    ext: &str,
    bytes: &[u8],
) -> Result<serde_json::Value> {
    match ext {
        "yaml" | "yml" => {
            let text = std::str::from_utf8(bytes)
                .map_err(|e| Error::Other(format!("{file_path}: file is not valid utf-8: {e}")))?;
            serde_saphyr::from_str(text).map_err(|e| Error::Yaml {
                path: file_path.to_string(),
                message: e.to_string(),
            })
        }
        "json" => serde_json::from_slice(bytes).map_err(|e| Error::Json {
            path: file_path.to_string(),
            source: e,
        }),
        other => Err(Error::Other(format!("unsupported extension: {other}"))),
    }
}

// ---------------------------------------------------------------------------
// CRUD with optimistic concurrency check.
// ---------------------------------------------------------------------------

/// Extract the (expected_version, expected_commit) bind pair from a
/// [`CommitRef`] for the SQL-level OCC predicate baked into
/// [`db::tx::update_record`] / [`db::tx::upsert_record`] /
/// [`db::tx::delete_record`].
///
/// `enforce_conflict` is still called separately as an early-bailout
/// (avoids acquiring write locks for clearly-stale clients) — these
/// binds are the second-line race guard against another tx writing
/// between our lookup and our write.
fn occ_binds(expected: Option<&CommitRef>) -> (Option<i64>, Option<&str>) {
    match expected {
        Some(CommitRef::Pending(v)) => (Some(*v), None),
        Some(CommitRef::Commit(c)) => (None, Some(c.as_str())),
        None => (None, None),
    }
}

fn enforce_conflict(
    file_path: &str,
    path: &str,
    expected: &CommitRef,
    existing_record_commit: Option<&String>,
    existing_record_version: Option<i64>,
    record_present: bool,
) -> Result<()> {
    match expected {
        CommitRef::Pending(expected_version) => {
            // The record must exist and its `version` must be <=
            // `expected_version`. Versions are monotonic per record
            // (writes always bump), so `record.version > expected`
            // means someone rewrote the row after the client's last
            // observation — that's the conflict signal. `<=` covers
            // both the unchanged case (`==`) and the case where the
            // client is holding a queueid from a *later* batch in
            // which this particular row wasn't touched.
            //
            // The `commit_id` doesn't matter — a `Pending(v)` token
            // remains valid after `commit_repository` rolls forward
            // (commit attribution doesn't bump version).
            if record_present && existing_record_version.is_some_and(|v| v <= *expected_version) {
                Ok(())
            } else {
                Err(Error::Conflict {
                    file_path: file_path.to_string(),
                    path: path.to_string(),
                    expected: expected.clone(),
                    actual: existing_record_commit.cloned(),
                })
            }
        }
        CommitRef::Commit(expected_oid) => {
            // Oid token requires an existing record at the given key,
            // and its commit_id must match.
            match existing_record_commit {
                Some(actual) if actual == expected_oid => Ok(()),
                actual => Err(Error::Conflict {
                    file_path: file_path.to_string(),
                    path: path.to_string(),
                    expected: expected.clone(),
                    actual: actual.cloned(),
                }),
            }
        }
    }
}

// Each CRUD primitive performs its conflict check + mutation + alias
// refresh in a single sqlx transaction. On any error (including a
// `Conflict`) the transaction is dropped without commit, so SQLite /
// Postgres roll it back atomically.

// Note: See the comment under "Generic transaction helpers." in tx.rs if you are wondering why we need all these `where` clauses on these generic functions.

async fn crud_create_in_pool<DB>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    file_path: Option<&str>,
    path: &str,
    key: &str,
    json: serde_json::Value,
    expected_commit: Option<CommitRef>,
) -> Result<WriteOutcome>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<i64>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    db::tx::LookupRow: for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (String,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    let json_text = serde_json::to_string(&json).map_err(|e| Error::Json {
        path: path.to_string(),
        source: e,
    })?;
    let mut tx = pool.begin().await?;
    let lookup = db::tx::lookup_commits(&mut tx, sync.worktree_id(), file_path, path, key).await?;
    // Resolve the effective file_path: caller-supplied, then existing
    // record's file, then worktree default. NotFound when none of those
    // yield a value (e.g. brand-new key and no `default_file_path` set).
    let resolved_fp: String = file_path
        .map(str::to_string)
        .or_else(|| lookup.record_file_path.clone())
        .or_else(|| lookup.default_file_path.clone())
        .ok_or_else(|| Error::NotFound {
            file_path: String::new(),
            path: path.to_string(),
        })?;
    let file_path: &str = &resolved_fp;
    // Live row → conflict. Tombstones are treated as absent so
    // `create_record` resurrects them.
    if lookup.record_id.is_some() {
        return Err(Error::AlreadyExists {
            file_path: file_path.to_string(),
            path: path.to_string(),
        });
    }
    if let Some(exp) = expected_commit.as_ref() {
        enforce_conflict(
            file_path,
            path,
            exp,
            lookup.record_commit.as_ref(),
            lookup.record_version,
            false,
        )?;
    }
    let version = db::tx::next_version(&mut tx, sync.worktree_id()).await?;
    let (exp_v, exp_c) = occ_binds(expected_commit.as_ref());
    let id = db::tx::create_record(
        &mut tx,
        RecordId {
            worktree_id: sync.worktree_id(),
            file_path,
            path,
            key,
        },
        &json_text,
        version,
        exp_v,
        exp_c,
    )
    .await?;
    let format_owner = db::tx::file_format(&mut tx, sync.worktree_id(), file_path).await?;
    db::tx::replace_aliases(
        &mut tx,
        id,
        &compute_aliases(
            sync,
            format_owner.as_deref(),
            id,
            file_path,
            path,
            key,
            &json,
        ),
    )
    .await?;
    tx.commit().await?;
    Ok(WriteOutcome { id, version })
}

async fn crud_update_in_pool<DB>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    file_path: Option<&str>,
    path: &str,
    key: &str,
    json: serde_json::Value,
    expected_commit: Option<CommitRef>,
) -> Result<WriteOutcome>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<i64>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    db::tx::LookupRow: for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (String,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    let json_text = serde_json::to_string(&json).map_err(|e| Error::Json {
        path: path.to_string(),
        source: e,
    })?;
    let mut tx = pool.begin().await?;
    let lookup = db::tx::lookup_commits(&mut tx, sync.worktree_id(), file_path, path, key).await?;
    // Resolve the effective file_path: caller-supplied, then existing
    // record's file. update/delete don't fall back to the worktree
    // default — they require an existing record, and the
    // `record_id.ok_or(NotFound)` below catches the absent case.
    let resolved_fp: String = file_path
        .map(str::to_string)
        .or_else(|| lookup.record_file_path.clone())
        .unwrap_or_default();
    let file_path: &str = &resolved_fp;
    // Tombstone or absent → NotFound.
    let id = lookup.record_id.ok_or_else(|| Error::NotFound {
        file_path: file_path.to_string(),
        path: path.to_string(),
    })?;
    if let Some(exp) = expected_commit.as_ref() {
        enforce_conflict(
            file_path,
            path,
            exp,
            lookup.record_commit.as_ref(),
            lookup.record_version,
            true,
        )?;
    }
    let version = db::tx::next_version(&mut tx, sync.worktree_id()).await?;
    let (exp_v, exp_c) = occ_binds(expected_commit.as_ref());
    db::tx::update_record(&mut tx, id, &json_text, version, exp_v, exp_c).await?;
    let format_owner = db::tx::file_format(&mut tx, sync.worktree_id(), file_path).await?;
    db::tx::replace_aliases(
        &mut tx,
        id,
        &compute_aliases(
            sync,
            format_owner.as_deref(),
            id,
            file_path,
            path,
            key,
            &json,
        ),
    )
    .await?;
    tx.commit().await?;
    Ok(WriteOutcome { id, version })
}

async fn crud_upsert_in_pool<DB>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    file_path: Option<&str>,
    path: &str,
    key: &str,
    json: serde_json::Value,
    expected_commit: Option<CommitRef>,
) -> Result<WriteOutcome>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<i64>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    db::tx::LookupRow: for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (String,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    let json_text = serde_json::to_string(&json).map_err(|e| Error::Json {
        path: path.to_string(),
        source: e,
    })?;
    let mut tx = pool.begin().await?;
    let lookup = db::tx::lookup_commits(&mut tx, sync.worktree_id(), file_path, path, key).await?;
    // Resolve the effective file_path: caller-supplied, then existing
    // record's file, then worktree default. NotFound when none of those
    // yield a value (e.g. brand-new key and no `default_file_path` set).
    let resolved_fp: String = file_path
        .map(str::to_string)
        .or_else(|| lookup.record_file_path.clone())
        .or_else(|| lookup.default_file_path.clone())
        .ok_or_else(|| Error::NotFound {
            file_path: String::new(),
            path: path.to_string(),
        })?;
    let file_path: &str = &resolved_fp;
    if let Some(exp) = expected_commit.as_ref() {
        enforce_conflict(
            file_path,
            path,
            exp,
            lookup.record_commit.as_ref(),
            lookup.record_version,
            lookup.record_id.is_some(),
        )?;
    }
    let format_owner = ensure_file_registered(sync, &mut tx, file_path, path).await?;
    let version = db::tx::next_version(&mut tx, sync.worktree_id()).await?;
    let (exp_v, exp_c) = occ_binds(expected_commit.as_ref());
    let id = db::tx::upsert_record(
        &mut tx,
        RecordId {
            worktree_id: sync.worktree_id(),
            file_path,
            path,
            key,
        },
        &json_text,
        version,
        exp_v,
        exp_c,
    )
    .await?;
    db::tx::replace_aliases(
        &mut tx,
        id,
        &compute_aliases(
            sync,
            format_owner.as_deref(),
            id,
            file_path,
            path,
            key,
            &json,
        ),
    )
    .await?;
    tx.commit().await?;
    Ok(WriteOutcome { id, version })
}

async fn crud_delete_in_pool<DB>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    file_path: Option<&str>,
    path: &str,
    key: &str,
    expected_commit: Option<CommitRef>,
) -> Result<WriteOutcome>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<i64>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    db::tx::LookupRow: for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    let mut tx = pool.begin().await?;
    let lookup = db::tx::lookup_commits(&mut tx, sync.worktree_id(), file_path, path, key).await?;
    // Resolve the effective file_path: caller-supplied, then existing
    // record's file. update/delete don't fall back to the worktree
    // default — they require an existing record, and the
    // `record_id.ok_or(NotFound)` below catches the absent case.
    let resolved_fp: String = file_path
        .map(str::to_string)
        .or_else(|| lookup.record_file_path.clone())
        .unwrap_or_default();
    let file_path: &str = &resolved_fp;
    // Tombstone or absent → NotFound. We never hard-delete here;
    // `commit_repository` is the only path that purges tombstones once
    // they've been written to disk.
    let id = lookup.record_id.ok_or_else(|| Error::NotFound {
        file_path: file_path.to_string(),
        path: path.to_string(),
    })?;
    if let Some(exp) = expected_commit.as_ref() {
        enforce_conflict(
            file_path,
            path,
            exp,
            lookup.record_commit.as_ref(),
            lookup.record_version,
            true,
        )?;
    }
    let version = db::tx::next_version(&mut tx, sync.worktree_id()).await?;
    let (exp_v, exp_c) = occ_binds(expected_commit.as_ref());
    db::tx::delete_record(&mut tx, id, version, exp_v, exp_c).await?;
    tx.commit().await?;
    Ok(WriteOutcome { id, version })
}

/// Apply a single [`BatchOp`] in the caller-owned transaction,
/// returning the new `(id, version)` on success.
///
/// Layered over [`db::tx::lookup_commits`] / [`enforce_conflict`] /
/// [`db::tx::next_version`] / [`db::tx::upsert_record`] /
/// [`db::tx::file_format`] / [`db::tx::replace_aliases`] /
/// [`db::tx::delete_record`].
async fn apply_one_in_tx<DB>(
    sync: &SyncedRepo,
    tx: &mut sqlx::Transaction<'_, DB>,
    op: BatchOp,
) -> Result<WriteOutcome>
where
    DB: db::tx::Dialect,
    // bind-value bounds (every db::tx::* helper calls `.bind(...)`)
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<i64>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    // arguments + executor (every helper runs a query through `&mut **tx`)
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    // row-decoding bounds: the three concrete row shapes used by
    // lookup_commits / next_version / file_format.
    db::tx::LookupRow: for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (String,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    match op {
        BatchOp::Upsert {
            file_path,
            path: op_path,
            key: op_key,
            json,
            expected,
        } => {
            let json_text = serde_json::to_string(&json).map_err(|e| Error::Json {
                path: op_path.clone(),
                source: e,
            })?;
            let lookup = db::tx::lookup_commits(
                tx,
                sync.worktree_id(),
                file_path.as_deref(),
                &op_path,
                &op_key,
            )
            .await?;
            // Resolve the effective file_path: caller-supplied, then
            // existing record's file, then worktree default. NotFound
            // when none of those yield a value (e.g. brand-new key
            // and no `default_file_path` set).
            let resolved_fp: String = file_path
                .as_deref()
                .map(str::to_string)
                .or_else(|| lookup.record_file_path.clone())
                .or_else(|| lookup.default_file_path.clone())
                .ok_or_else(|| Error::NotFound {
                    file_path: String::new(),
                    path: op_path.clone(),
                })?;
            if let Some(exp) = expected.as_ref() {
                enforce_conflict(
                    &resolved_fp,
                    &op_path,
                    exp,
                    lookup.record_commit.as_ref(),
                    lookup.record_version,
                    lookup.record_id.is_some(),
                )?;
            }
            let format_owner = ensure_file_registered(sync, tx, &resolved_fp, &op_path).await?;
            let version = db::tx::next_version(tx, sync.worktree_id()).await?;
            let (exp_v, exp_c) = occ_binds(expected.as_ref());
            let id = db::tx::upsert_record(
                tx,
                RecordId {
                    worktree_id: sync.worktree_id(),
                    file_path: &resolved_fp,
                    path: &op_path,
                    key: &op_key,
                },
                &json_text,
                version,
                exp_v,
                exp_c,
            )
            .await?;
            db::tx::replace_aliases(
                tx,
                id,
                &compute_aliases(
                    sync,
                    format_owner.as_deref(),
                    id,
                    &resolved_fp,
                    &op_path,
                    &op_key,
                    &json,
                ),
            )
            .await?;
            Ok(WriteOutcome { id, version })
        }
        BatchOp::Delete {
            file_path,
            path: op_path,
            key: op_key,
            expected,
        } => {
            let lookup = db::tx::lookup_commits(
                tx,
                sync.worktree_id(),
                file_path.as_deref(),
                &op_path,
                &op_key,
            )
            .await?;
            // Resolve the effective file_path: caller-supplied, then
            // existing record's file. update/delete don't fall back
            // to the worktree default — they require an existing
            // record, and the `record_id.ok_or(NotFound)` below
            // catches the absent case.
            let resolved_fp: String = file_path
                .as_deref()
                .map(str::to_string)
                .or_else(|| lookup.record_file_path.clone())
                .unwrap_or_default();
            let id = lookup.record_id.ok_or_else(|| Error::NotFound {
                file_path: resolved_fp.clone(),
                path: op_path.clone(),
            })?;
            if let Some(exp) = expected.as_ref() {
                enforce_conflict(
                    &resolved_fp,
                    &op_path,
                    exp,
                    lookup.record_commit.as_ref(),
                    lookup.record_version,
                    true,
                )?;
            }
            let version = db::tx::next_version(tx, sync.worktree_id()).await?;
            let (exp_v, exp_c) = occ_binds(expected.as_ref());
            db::tx::delete_record(tx, id, version, exp_v, exp_c).await?;
            Ok(WriteOutcome { id, version })
        }
    }
}

/// Generic batch driver: opens one transaction on `pool`, walks
/// `ops`, accumulates [`Applied`] / [`Failed`] entries, and either
/// commits (success / non-atomic with failures) or rolls back (atomic
/// + first failure).
//
async fn apply_batch_inner<DB>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    ops: Vec<BatchOp>,
    atomic: bool,
    meta: Option<TxnMeta>,
) -> Result<BatchOutcome>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<i64>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> Option<&'q str>: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    db::tx::LookupRow: for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (i64,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
    (String,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    let mut outcome = BatchOutcome::default();
    let mut tx = pool.begin().await?;
    for (index, op) in ops.into_iter().enumerate() {
        let path = op.path().to_string();
        let key = op.key().to_string();
        match apply_one_in_tx(sync, &mut tx, op).await {
            Ok(write) => {
                let v = write.version;
                if outcome.last_version.map_or(true, |cur| v > cur) {
                    outcome.last_version = Some(v);
                }
                outcome.applied.push(Applied {
                    index,
                    path,
                    key,
                    outcome: write,
                });
            }
            Err(err @ (Error::Conflict { .. } | Error::NotFound { .. })) => {
                outcome.failed.push(Failed {
                    index,
                    path,
                    key,
                    error: err,
                });
                if atomic {
                    // Drop the tx without commit → rollback; clear any
                    // "applied" entries we'd optimistically pushed
                    // (they didn't really commit).
                    drop(tx);
                    outcome.applied.clear();
                    outcome.last_version = None;
                    return Ok(outcome);
                }
                // Non-atomic: the application-level conflict didn't
                // poison the SQL tx, so continue.
            }
            Err(other) => {
                return Err(other);
            }
        }
    }
    // Audit row last, inside the same transaction: it lands only if the
    // writes it describes do. A batch that applied nothing has no
    // version range to record, so it gets no row.
    if let (Some(meta), Some(first), Some(last)) = (
        meta,
        outcome.applied.first().map(|a| a.outcome.version),
        outcome.last_version,
    ) {
        let created_at = chrono::Local::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, false);
        db::tx::insert_txn(
            &mut tx,
            sync.worktree_id(),
            first,
            last,
            meta.author.as_deref(),
            meta.message.as_deref(),
            &created_at,
        )
        .await?;
    }
    tx.commit().await?;
    Ok(outcome)
}

/// Return the format owning `file_path`, registering the file first if the
/// worktree hasn't scanned it.
///
/// A write may name a `file_path` that doesn't exist yet — creating a second
/// cloudmap, say. Records carry a foreign key to the `file` table, so the row
/// has to exist before the insert; the file itself is synthesised on the next
/// [`SyncedRepo::write_file`], which already handles a missing file on disk.
/// The format is taken from whichever registered [`crate::DataFormat`] claims
/// the record's section.
async fn ensure_file_registered<DB>(
    sync: &SyncedRepo,
    tx: &mut sqlx::Transaction<'_, DB>,
    file_path: &str,
    record_path: &str,
) -> Result<Option<String>>
where
    DB: db::tx::Dialect,
    for<'q> i64: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> &'q str: sqlx::Encode<'q, DB> + sqlx::Type<DB>,
    for<'q> <DB as sqlx::Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as sqlx::Database>::Connection: sqlx::Executor<'c, Database = DB>,
    (String,): for<'r> sqlx::FromRow<'r, <DB as sqlx::Database>::Row> + Send + Unpin,
{
    if let Some(existing) = db::tx::file_format(tx, sync.worktree_id(), file_path).await? {
        return Ok(Some(existing));
    }
    // No format claims this section, so there's nothing to record the file as
    // — `file.format` is NOT NULL. Callers writing a section no registered
    // format knows about get a clear error instead of an FK violation.
    let format = sync
        .formats()
        .for_path(record_path)
        .ok_or_else(|| Error::UnknownFormat(record_path.to_string()))?
        .name()
        .to_string();
    db::tx::ensure_file(tx, sync.worktree_id(), file_path, &format).await?;
    Ok(Some(format))
}

/// Transactional body of [`SyncedRepo::upsert_file_and_records`]: one
/// SQL transaction per file covering the file-row upsert, every record
/// upsert (each stamped with a version drawn *inside* the tx), and the
/// trailing delete of records that vanished from the file.
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
async fn upsert_file_and_records_inner<DB>(
    sync: &SyncedRepo,
    pool: &sqlx::Pool<DB>,
    rel_path: &str,
    last_commit_id: Option<&str>,
    value: &serde_json::Value,
    format: &dyn crate::format::DataFormat,
    stats: &mut UpdateStats,
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
{
    let mut tx = pool.begin().await?;

    // Upsert file row.
    db::tx::upsert_file(
        &mut tx,
        sync.worktree_id(),
        rel_path,
        format.name(),
        last_commit_id,
    )
    .await?;

    // Collect new (path, key) set so we can delete records that disappeared.
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

    for (path, key, child) in to_upsert {
        let json_text = serde_json::to_string(&child).map_err(|e| Error::Json {
            path: path.clone(),
            source: e,
        })?;
        let version = db::tx::next_version(&mut tx, sync.worktree_id()).await?;
        let id = db::tx::sync_upsert_record(
            &mut tx,
            RecordId {
                worktree_id: sync.worktree_id(),
                file_path: rel_path,
                path: &path,
                key: &key,
            },
            &json_text,
            last_commit_id,
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
            commit_id: last_commit_id.map(|s| s.to_string()),
            json: child,
            deleted: false,
            version,
        };
        db::tx::replace_aliases(&mut tx, id, &format.find_alias(&record)).await?;
    }

    // Delete records that used to be in the file but are gone now.
    let removed = db::tx::delete_missing(&mut tx, sync.worktree_id(), rel_path, &new_keys).await?;
    stats.records_deleted += removed;

    tx.commit().await?;
    Ok(())
}

fn compute_aliases(
    sync: &SyncedRepo,
    format_owner: Option<&str>,
    record_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
    json: &serde_json::Value,
) -> Vec<(String, String)> {
    let Some(name) = format_owner else {
        return Vec::new();
    };
    let Some(fmt) = sync.formats().by_name(name) else {
        return Vec::new();
    };
    let record = Record {
        id: record_id,
        worktree_id: sync.worktree_id(),
        file_path: file_path.to_string(),
        path: path.to_string(),
        key: key.to_string(),
        commit_id: None,
        json: json.clone(),
        deleted: false,
        // version isn't read by `DataFormat::find_alias` impls, so a
        // placeholder is fine — this struct is only consulted for
        // alias derivation.
        version: 0,
    };
    fmt.find_alias(&record)
}

#[cfg(test)]
mod tests {
    use super::reorder_like;
    use serde_json::json;

    /// Top-level keys of a JSON object, in order.
    fn keys(value: &serde_json::Value) -> Vec<&str> {
        value
            .as_object()
            .expect("object")
            .keys()
            .map(String::as_str)
            .collect()
    }

    #[test]
    fn reorder_like_mirrors_the_previous_key_order() {
        let previous = json!({"path": "p", "name": "n", "metadata": {"description": "d"}});
        let next = json!({"metadata": {"description": "d2"}, "name": "n2", "path": "p2"});
        let out = reorder_like(&previous, next);
        assert_eq!(keys(&out), ["path", "name", "metadata"]);
        assert_eq!(out["name"], "n2", "values come from `next`");
    }

    #[test]
    fn reorder_like_appends_keys_the_file_lacks() {
        let previous = json!({"path": "p", "name": "n"});
        let next = json!({"tags": {}, "name": "n", "branches": {}, "path": "p"});
        let out = reorder_like(&previous, next);
        assert_eq!(
            keys(&out),
            ["path", "name", "tags", "branches"],
            "known keys first, new ones in arrival order"
        );
    }

    #[test]
    fn reorder_like_recurses_into_nested_and_data_keyed_maps() {
        // `metadata` is schema-shaped, `contains` is keyed by data --
        // mirroring the file covers both without knowing the difference.
        let previous = json!({
            "metadata": {"description": "d", "homepage_url": "h", "issues_url": "i"},
            "contains": {".gitlab-ci.yml": null, "unfurl.yaml": null},
        });
        let next = json!({
            "contains": {"unfurl.yaml": null, ".gitlab-ci.yml": null},
            "metadata": {"issues_url": "i", "description": "d", "homepage_url": "h2"},
        });
        let out = reorder_like(&previous, next);
        assert_eq!(keys(&out), ["metadata", "contains"]);
        assert_eq!(
            keys(&out["metadata"]),
            ["description", "homepage_url", "issues_url"]
        );
        assert_eq!(keys(&out["contains"]), [".gitlab-ci.yml", "unfurl.yaml"]);
    }

    #[test]
    fn reorder_like_pairs_array_elements_positionally() {
        let previous = json!({"release_schedule": [{"version": "1", "date": "d"}]});
        let next = json!({"release_schedule": [{"date": "d2", "version": "2"}, {"b": 1, "a": 2}]});
        let out = reorder_like(&previous, next);
        let items = out["release_schedule"].as_array().expect("array");
        assert_eq!(keys(&items[0]), ["version", "date"], "paired with previous");
        assert_eq!(keys(&items[1]), ["b", "a"], "no counterpart, left alone");
    }

    #[test]
    fn reorder_like_leaves_mismatched_shapes_alone() {
        let previous = json!({"a": {"x": 1}});
        let next = json!({"a": [1, 2]});
        assert_eq!(reorder_like(&previous, next.clone()), next);
        assert_eq!(reorder_like(&json!("scalar"), next.clone()), next);
    }
}
