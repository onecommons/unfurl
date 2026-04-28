// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `GitSync` — the public type tying the pool, gix repo, and
//! [`FormatRegistry`] together.

use std::collections::BTreeSet;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::db::{self, Db, DbConfig};
use crate::error::{Error, Result};
use crate::format::FormatRegistry;
use crate::git;
use crate::model::{Record, UpdateStats};

/// Optimistic-concurrency token passed to mutating CRUD calls.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CommitRef {
    /// Caller expects the row's `commit_id` to be NULL (a pending edit).
    Pending,
    /// Caller expects the row's `commit_id` to equal this hex commit
    /// oid string.
    Oid(String),
}

/// Sync handle bundling a sqlx pool, a gix repo, and a format registry.
///
/// Cheaply cloneable (`Arc`-wrapped state) so callers can hand it to
/// background tasks without juggling lifetimes.
#[derive(Clone)]
pub struct GitSync {
    inner: Arc<GitSyncInner>,
}

struct GitSyncInner {
    db: Db,
    repo_path: PathBuf,
    formats: FormatRegistry,
    worktree_id: i64,
}

impl GitSync {
    /// Open a working tree, ensure the schema is up to date, and ensure
    /// a `worktree` row exists for `(origin, branch)`.
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
            inner: Arc::new(GitSyncInner {
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

    /// Snapshot the working tree: its filesystem path, current branch
    /// name, and HEAD commit oid (as a hex string). Used by tests to
    /// assert conflict-token semantics and by callers that need to
    /// stash a reference to the current commit.
    pub async fn get_working_dir(&self) -> Result<crate::model::WorkingDir> {
        let repo = self.repo()?;
        let meta = git::worktree_meta(&repo)?;
        Ok(crate::model::WorkingDir {
            repo_path: self.inner.repo_path.clone(),
            branch: meta.branch,
            head_commit: meta.head_oid.map(|o| o.to_string()),
        })
    }

    /// Walk the working tree, parse new/changed files, upsert records.
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

    /// Search records by optional filters. All `Some(...)` filters AND
    /// together. With `alias = true` and a `key` filter, a record also
    /// matches when one of its alias rows has that key (joined on
    /// `record_id`); without a `key` filter, `alias` is a no-op.
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

    /// Same filters as [`Self::find_records`]; additionally walks
    /// [`crate::DataFormat::follow`] outgoing edges from each match
    /// breadth-first, returning at most `follow` newly-discovered
    /// records. Returns `(initial, followed)` where `initial` is what
    /// `find_records` would have returned and `followed` is the new
    /// set, capped at `follow` entries (alias-resolved).
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
            for (p, k) in fmt.follow(&rec) {
                if visited.contains(&(p.clone(), k.clone())) {
                    continue;
                }
                // Each follow target is an exact (path, key) lookup,
                // alias-aware so versioned URLs resolve to their
                // canonical record.
                let hits = db::record::find(
                    self.db(),
                    self.worktree_id(),
                    None,
                    Some(p.as_str()),
                    Some(k.as_str()),
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

    /// Read a single record by `(file_path, path, key)` within this
    /// worktree.
    pub async fn get_record(
        &self,
        file_path: &str,
        path: &str,
        key: &str,
    ) -> Result<Option<Record>> {
        db::record::get(self.db(), self.worktree_id(), file_path, path, key).await
    }

    /// Read a single record by primary key.
    pub async fn get_record_by_id(&self, id: i64) -> Result<Option<Record>> {
        db::record::get_by_id(self.db(), id).await
    }

    /// Read the `file` row for a given working-tree path. Returns
    /// `None` if no row exists for this worktree.
    pub async fn get_file(&self, file_path: &str) -> Result<Option<crate::model::File>> {
        db::file::get(self.db(), self.worktree_id(), file_path).await
    }

    /// Read the `worktree` row this `GitSync` is bound to.
    pub async fn get_worktree(&self) -> Result<crate::model::Worktree> {
        db::worktree::get(self.db(), self.worktree_id()).await
    }

    /// Create a new record. Fails with `AlreadyExists` if
    /// `(file_path, path, key)` is already present.
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

    /// Update an existing record's JSON. Fails with `NotFound` if no row
    /// matches.
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

    /// Insert-or-update.
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

    /// Delete the record (and its aliases). Fails with `NotFound` if
    /// missing.
    pub async fn delete_record(
        &self,
        file_path: &str,
        path: &str,
        key: &str,
        expected_commit: Option<CommitRef>,
    ) -> Result<()> {
        crud_delete(self, file_path, path, key, expected_commit).await
    }

    /// Re-serialize each file with at least one record whose
    /// `commit_id IS NULL` and write it back to disk. Returns the paths
    /// that were actually rewritten (skipped when output bytes match
    /// what's on disk).
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

    /// Lower-level: apply pending record changes for `file_path` to the
    /// existing on-disk file (parsed as YAML or JSON), preserving the
    /// document's original key ordering, formatting, and any keys we
    /// don't track. Returns `Some(path)` when on-disk bytes changed,
    /// `None` otherwise.
    ///
    /// Pending changes are records whose `commit_id IS NULL`:
    /// - non-tombstone rows are written at `obj[trim(path)][key]`,
    ///   creating the section object if missing;
    /// - tombstones (`deleted = TRUE`) remove `obj[trim(path)][key]`;
    /// - if the resulting section becomes empty it is removed.
    ///
    /// If the file does not exist on disk, a fresh document is
    /// synthesised from the non-tombstone records (tombstones in this
    /// case are no-ops).
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

    /// Run `save_changes`, stage the dirty paths, create a commit on
    /// HEAD, and roll the new oid into the records / files in one
    /// transaction. Returns the new commit oid as a hex string, or
    /// `None` if nothing was committed.
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
    sync: &GitSync,
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
    sync: &GitSync,
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
    sync: &GitSync,
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
    sync: &GitSync,
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
    sync: &GitSync,
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
