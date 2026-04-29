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

use crate::db::{self, Db, DbConfig};
use crate::error::{Error, Result};
use crate::format::FormatRegistry;
use crate::git;
use crate::model::{Record, UpdateStats};

/// Optimistic-concurrency token used by mutating CRUD calls.
///
/// Pass `Some(token)` as the `expected_commit` argument to
/// [`SyncedRepo::create_record`] / [`SyncedRepo::update_record`] /
/// [`SyncedRepo::upsert_record`] / [`SyncedRepo::delete_record`] to assert
/// what the caller believes the row's current `commit_id` is. Mismatch
/// returns [`crate::Error::Conflict`] and rolls back the transaction.
/// Pass `None` to skip the check entirely.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CommitRef {
    /// Caller expects the row's `commit_id` to be `NULL` — i.e. a
    /// pending in-flight edit, not yet committed.
    Pending,
    /// Caller expects the row's `commit_id` to equal this hex commit
    /// oid string (40 hex chars for SHA-1).
    Oid(String),
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
                &p.format_name,
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

        Ok(stats)
    }

    async fn upsert_file_and_records(
        &self,
        rel_path: &str,
        format_name: &str,
        last_commit_id: Option<&str>,
        value: &serde_json::Value,
        format: &dyn crate::format::DataFormat,
        stats: &mut UpdateStats,
    ) -> Result<()> {
        // Upsert file row.
        db::file::upsert(
            self.db(),
            self.worktree_id(),
            rel_path,
            format_name,
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
            let id = db::record::upsert(
                self.db(),
                self.worktree_id(),
                rel_path,
                &path,
                &key,
                &child,
                last_commit_id,
            )
            .await?;
            stats.records_upserted += 1;

            // Aliases.
            let record = Record {
                id,
                worktree_id: self.worktree_id(),
                file_path: rel_path.to_string(),
                path: path.clone(),
                key: key.clone(),
                commit_id: last_commit_id.map(|s| s.to_string()),
                json: child,
                deleted: false,
            };
            db::alias::replace(self.db(), id, &format.find_alias(&record)).await?;
        }

        // Delete records that used to be in the file but are gone now.
        let removed =
            db::record::delete_missing(self.db(), self.worktree_id(), rel_path, &new_keys).await?;
        stats.records_deleted += removed;

        Ok(())
    }

    /// Search records by optional `file_path` / `path` / `key` filters.
    ///
    /// All `Some(...)` filters are AND'd together; `None` matches any
    /// value. With `alias = true` and a `key` filter, a record also
    /// matches when one of its [`crate::Alias`] rows has that key
    /// (joined on `record_id`). Without a `key` filter, `alias` is a
    /// no-op. Tombstoned records are hidden.
    ///
    /// Results are ordered by `(path, key)` for stable output.
    pub async fn find_records(
        &self,
        file_path: Option<String>,
        path: Option<String>,
        key: Option<String>,
        alias: bool,
    ) -> Result<Vec<Record>> {
        db::record::find(
            self.db(),
            self.worktree_id(),
            file_path.as_deref(),
            path.as_deref(),
            key.as_deref(),
            alias,
        )
        .await
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
    pub async fn find_records_follow(
        &self,
        file_path: Option<String>,
        path: Option<String>,
        key: Option<String>,
        alias: bool,
        follow: u32,
    ) -> Result<(Vec<Record>, Vec<Record>)> {
        let initial = self.find_records(file_path, path, key, alias).await?;
        if follow == 0 || initial.is_empty() {
            return Ok((initial, Vec::new()));
        }

        // Memoize file_path → format-name so we don't query `file` once
        // per visited record.
        let mut format_cache: std::collections::HashMap<String, Option<String>> =
            std::collections::HashMap::new();

        let mut visited: BTreeSet<(String, String)> = initial
            .iter()
            .map(|r| (r.path.clone(), r.key.clone()))
            .collect();
        let mut queue: std::collections::VecDeque<Record> = initial.iter().cloned().collect();
        let mut followed: Vec<Record> = Vec::new();

        while let Some(rec) = queue.pop_front() {
            if followed.len() as u32 >= follow {
                break;
            }
            let format_name = match format_cache.get(&rec.file_path) {
                Some(v) => v.clone(),
                None => {
                    let f = db::file::get(self.db(), self.worktree_id(), &rec.file_path).await?;
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
                // Each follow target is a key matched against
                // `record.key` and `alias.key` across every record in
                // the worktree (no path filter). One URL may resolve
                // to multiple records — they're all added.
                let hits = db::record::find(
                    self.db(),
                    self.worktree_id(),
                    None,
                    None,
                    Some(key.as_str()),
                    true,
                )
                .await?;
                for r in hits {
                    let id = (r.path.clone(), r.key.clone());
                    if visited.insert(id) {
                        queue.push_back(r.clone());
                        followed.push(r);
                        if followed.len() as u32 >= follow {
                            break;
                        }
                    }
                }
                if followed.len() as u32 >= follow {
                    break;
                }
            }
        }
        Ok((initial, followed))
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
    /// Returns the new record's primary key.
    pub async fn create_record(
        &self,
        file_path: &str,
        path: &str,
        key: &str,
        json: serde_json::Value,
        expected_commit: Option<CommitRef>,
    ) -> Result<i64> {
        crud_create(self, file_path, path, key, json, expected_commit).await
    }

    /// Replace an existing record's JSON.
    ///
    /// Fails with [`crate::Error::NotFound`] if the row is absent or
    /// tombstoned. `expected_commit` is the optimistic-concurrency
    /// token; see [`CommitRef`]. Sets the row's `commit_id` back to
    /// `NULL` (in-flight). Returns the row's primary key.
    pub async fn update_record(
        &self,
        file_path: &str,
        path: &str,
        key: &str,
        json: serde_json::Value,
        expected_commit: Option<CommitRef>,
    ) -> Result<i64> {
        crud_update(self, file_path, path, key, json, expected_commit).await
    }

    /// Insert-or-replace a record.
    ///
    /// Behaves like [`Self::create_record`] when the row is absent,
    /// and like [`Self::update_record`] when it's present (live or
    /// tombstoned). `expected_commit` is checked the same way as in
    /// the other CRUD methods.
    pub async fn upsert_record(
        &self,
        file_path: &str,
        path: &str,
        key: &str,
        json: serde_json::Value,
        expected_commit: Option<CommitRef>,
    ) -> Result<i64> {
        crud_upsert(self, file_path, path, key, json, expected_commit).await
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
    pub async fn delete_record(
        &self,
        file_path: &str,
        path: &str,
        key: &str,
        expected_commit: Option<CommitRef>,
    ) -> Result<()> {
        crud_delete(self, file_path, path, key, expected_commit).await
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

        let abs = self.inner.repo_path.join(file_path);
        let ext = file_path
            .rsplit_once('.')
            .map(|(_, e)| e.to_ascii_lowercase())
            .unwrap_or_default();

        // Parse the existing on-disk file (if any) into a serde_json
        // value. When the file is missing, start from an empty object.
        let mut root: serde_json::Value = match std::fs::read(&abs) {
            Ok(bytes) => parse_bytes_for_extension(file_path, &ext, &bytes)?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                serde_json::Value::Object(serde_json::Map::new())
            }
            Err(e) => return Err(Error::Io(e)),
        };

        // The document must be an object; if it isn't, replace it.
        if !root.is_object() {
            root = serde_json::Value::Object(serde_json::Map::new());
        }

        for rec in pending {
            let section_name = rec.path.trim_start_matches('/');
            if section_name.is_empty() {
                // v1 supports single-segment parents only.
                continue;
            }
            let root_obj = root.as_object_mut().expect("root is object");
            if rec.deleted {
                // Use `shift_remove` (not `remove`/`swap_remove`) so we
                // preserve the order of the surviving entries —
                // critical for the "minimally-edited" output the tests
                // assert against.
                if let Some(section) = root_obj
                    .get_mut(section_name)
                    .and_then(|v| v.as_object_mut())
                {
                    section.shift_remove(&rec.key);
                    if section.is_empty() {
                        root_obj.shift_remove(section_name);
                    }
                }
            } else {
                let section = root_obj
                    .entry(section_name.to_string())
                    .or_insert_with(|| serde_json::Value::Object(serde_json::Map::new()));
                if !section.is_object() {
                    *section = serde_json::Value::Object(serde_json::Map::new());
                }
                let section_obj = section.as_object_mut().expect("section is object");
                section_obj.insert(rec.key, rec.json);
            }
        }

        // Serialize per extension.
        let bytes: Vec<u8> = match ext.as_str() {
            "yaml" | "yml" => {
                let s = serde_saphyr::to_string(&root).map_err(|e| Error::Yaml {
                    path: file_path.to_string(),
                    message: e.to_string(),
                })?;
                crate::util::elide_explicit_nulls(&s).into_bytes()
            }
            "json" => serde_json::to_vec_pretty(&root).map_err(|e| Error::Json {
                path: file_path.to_string(),
                source: e,
            })?,
            other => return Err(Error::Other(format!("unsupported extension: {other}"))),
        };

        // Skip writes if the bytes are unchanged.
        if let Ok(existing) = std::fs::read(&abs) {
            if existing == bytes {
                return Ok(None);
            }
        }

        // Atomic replace via tempfile in the same directory.
        let dir = abs.parent().unwrap_or_else(|| Path::new("."));
        std::fs::create_dir_all(dir)?;
        let mut tmp = tempfile::NamedTempFile::new_in(dir)?;
        tmp.write_all(&bytes)?;
        tmp.flush()?;
        tmp.persist(&abs).map_err(|e| Error::Io(e.error))?;
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

        // Create the commit using gix.
        let repo = self.repo()?;
        let oid = git::commit_paths(&repo, &dirty, message)?;
        let oid_str = oid.to_string();

        // Roll the commit id into the dirty rows in a single transaction.
        db::commit::roll_forward(self.db(), self.worktree_id(), &dirty, &oid_str).await?;

        Ok(Some(oid_str))
    }
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

fn enforce_conflict(
    file_path: &str,
    path: &str,
    expected: &CommitRef,
    existing_record_commit: Option<&String>,
    file_commit: Option<&String>,
    record_present: bool,
) -> Result<()> {
    match expected {
        CommitRef::Pending => {
            if record_present && existing_record_commit.is_none() {
                Ok(())
            } else {
                Err(Error::Conflict {
                    file_path: file_path.to_string(),
                    path: path.to_string(),
                    expected: expected.clone(),
                    actual: existing_record_commit.or(file_commit).cloned(),
                })
            }
        }
        CommitRef::Oid(expected_oid) => {
            let target = if record_present {
                existing_record_commit
            } else {
                file_commit
            };
            match target {
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

async fn crud_create(
    sync: &SyncedRepo,
    file_path: &str,
    path: &str,
    key: &str,
    json: serde_json::Value,
    expected_commit: Option<CommitRef>,
) -> Result<i64> {
    let json_text = serde_json::to_string(&json).map_err(|e| Error::Json {
        path: path.to_string(),
        source: e,
    })?;
    let format_owner: Option<String>;
    let id: i64;
    match sync.db() {
        Db::Sqlite(pool) => {
            let mut tx = pool.begin().await?;
            db::tx::ensure_file_row(&mut tx, sync.worktree_id(), file_path).await?;
            let lookup =
                db::tx::lookup_commits(&mut tx, sync.worktree_id(), file_path, path, key).await?;
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
                    lookup.file_commit.as_ref(),
                    false,
                )?;
            }
            id = db::tx::create_record(
                &mut tx,
                sync.worktree_id(),
                file_path,
                path,
                key,
                &json_text,
            )
            .await?;
            format_owner = db::tx::file_format(&mut tx, sync.worktree_id(), file_path).await?;
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
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let mut tx = pool.begin().await?;
            db::tx::ensure_file_row(&mut tx, sync.worktree_id(), file_path).await?;
            let lookup =
                db::tx::lookup_commits(&mut tx, sync.worktree_id(), file_path, path, key).await?;
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
                    lookup.file_commit.as_ref(),
                    false,
                )?;
            }
            id = db::tx::create_record(
                &mut tx,
                sync.worktree_id(),
                file_path,
                path,
                key,
                &json_text,
            )
            .await?;
            format_owner = db::tx::file_format(&mut tx, sync.worktree_id(), file_path).await?;
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
        }
    }
    Ok(id)
}

async fn crud_update(
    sync: &SyncedRepo,
    file_path: &str,
    path: &str,
    key: &str,
    json: serde_json::Value,
    expected_commit: Option<CommitRef>,
) -> Result<i64> {
    let json_text = serde_json::to_string(&json).map_err(|e| Error::Json {
        path: path.to_string(),
        source: e,
    })?;
    let id: i64;
    match sync.db() {
        Db::Sqlite(pool) => {
            let mut tx = pool.begin().await?;
            let lookup =
                db::tx::lookup_commits(&mut tx, sync.worktree_id(), file_path, path, key).await?;
            // Tombstone or absent → NotFound.
            id = lookup.record_id.ok_or_else(|| Error::NotFound {
                file_path: file_path.to_string(),
                path: path.to_string(),
            })?;
            if let Some(exp) = expected_commit.as_ref() {
                enforce_conflict(
                    file_path,
                    path,
                    exp,
                    lookup.record_commit.as_ref(),
                    lookup.file_commit.as_ref(),
                    true,
                )?;
            }
            db::tx::update_record(&mut tx, id, &json_text).await?;
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
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let mut tx = pool.begin().await?;
            let lookup =
                db::tx::lookup_commits(&mut tx, sync.worktree_id(), file_path, path, key).await?;
            id = lookup.record_id.ok_or_else(|| Error::NotFound {
                file_path: file_path.to_string(),
                path: path.to_string(),
            })?;
            if let Some(exp) = expected_commit.as_ref() {
                enforce_conflict(
                    file_path,
                    path,
                    exp,
                    lookup.record_commit.as_ref(),
                    lookup.file_commit.as_ref(),
                    true,
                )?;
            }
            db::tx::update_record(&mut tx, id, &json_text).await?;
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
        }
    }
    Ok(id)
}

async fn crud_upsert(
    sync: &SyncedRepo,
    file_path: &str,
    path: &str,
    key: &str,
    json: serde_json::Value,
    expected_commit: Option<CommitRef>,
) -> Result<i64> {
    let json_text = serde_json::to_string(&json).map_err(|e| Error::Json {
        path: path.to_string(),
        source: e,
    })?;
    let id: i64;
    match sync.db() {
        Db::Sqlite(pool) => {
            let mut tx = pool.begin().await?;
            db::tx::ensure_file_row(&mut tx, sync.worktree_id(), file_path).await?;
            let lookup =
                db::tx::lookup_commits(&mut tx, sync.worktree_id(), file_path, path, key).await?;
            if let Some(exp) = expected_commit.as_ref() {
                enforce_conflict(
                    file_path,
                    path,
                    exp,
                    lookup.record_commit.as_ref(),
                    lookup.file_commit.as_ref(),
                    lookup.record_id.is_some(),
                )?;
            }
            id = db::tx::upsert_record(
                &mut tx,
                sync.worktree_id(),
                file_path,
                path,
                key,
                &json_text,
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
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let mut tx = pool.begin().await?;
            db::tx::ensure_file_row(&mut tx, sync.worktree_id(), file_path).await?;
            let lookup =
                db::tx::lookup_commits(&mut tx, sync.worktree_id(), file_path, path, key).await?;
            if let Some(exp) = expected_commit.as_ref() {
                enforce_conflict(
                    file_path,
                    path,
                    exp,
                    lookup.record_commit.as_ref(),
                    lookup.file_commit.as_ref(),
                    lookup.record_id.is_some(),
                )?;
            }
            id = db::tx::upsert_record(
                &mut tx,
                sync.worktree_id(),
                file_path,
                path,
                key,
                &json_text,
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
        }
    }
    Ok(id)
}

async fn crud_delete(
    sync: &SyncedRepo,
    file_path: &str,
    path: &str,
    key: &str,
    expected_commit: Option<CommitRef>,
) -> Result<()> {
    match sync.db() {
        Db::Sqlite(pool) => {
            let mut tx = pool.begin().await?;
            let lookup =
                db::tx::lookup_commits(&mut tx, sync.worktree_id(), file_path, path, key).await?;
            // Tombstone or absent → NotFound. We never hard-delete here;
            // `commit_repository` is the only path that purges
            // tombstones once they've been written to disk.
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
                    lookup.file_commit.as_ref(),
                    true,
                )?;
            }
            db::tx::delete_record(&mut tx, id).await?;
            tx.commit().await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let mut tx = pool.begin().await?;
            let lookup =
                db::tx::lookup_commits(&mut tx, sync.worktree_id(), file_path, path, key).await?;
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
                    lookup.file_commit.as_ref(),
                    true,
                )?;
            }
            db::tx::delete_record(&mut tx, id).await?;
            tx.commit().await?;
        }
    }
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
    };
    fmt.find_alias(&record)
}
