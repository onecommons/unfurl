// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `GitSync` — the public type tying the pool, gix repo, and
//! [`FormatRegistry`] together.

use std::collections::BTreeSet;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::db::{Db, DbConfig};
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

        let worktree_id = upsert_worktree(&db, &meta.origin, &meta.branch).await?;

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
            update_worktree_commit(self.db(), self.worktree_id(), Some(&oid)).await?;
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
        upsert_file(
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
            let id = upsert_record_row(
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
            replace_aliases(self.db(), id, &format.find_alias(&record)).await?;
        }

        // Delete records that used to be in the file but are gone now.
        let removed =
            delete_missing_records(self.db(), self.worktree_id(), rel_path, &new_keys).await?;
        stats.records_deleted += removed;

        Ok(())
    }

    /// Search records by `(path, optional key)` targets within this
    /// worktree's records of the given format.
    ///
    /// - empty `targets` → all records of that format.
    /// - `(path, None)` → all records under `path`.
    /// - `(path, Some(key))` → that exact record (or via alias).
    pub async fn find_records(
        &self,
        targets: &[(String, Option<String>)],
        format: &str,
        follow: bool,
    ) -> Result<Vec<Record>> {
        let mut out = find_records_inner(self.db(), self.worktree_id(), targets, format).await?;

        if follow {
            let Some(fmt) = self.formats().by_name(format) else {
                return Err(Error::UnknownFormat(format.to_string()));
            };
            let mut visited: BTreeSet<(String, String)> = out
                .iter()
                .map(|r| (r.path.clone(), r.key.clone()))
                .collect();
            let mut queue: Vec<Record> = out.clone();
            while let Some(rec) = queue.pop() {
                let next = fmt.follow(&rec);
                let new_targets: Vec<(String, Option<String>)> = next
                    .into_iter()
                    .filter(|p| !visited.contains(p))
                    .map(|(p, k)| (p, Some(k)))
                    .collect();
                if new_targets.is_empty() {
                    continue;
                }
                let more =
                    find_records_inner(self.db(), self.worktree_id(), &new_targets, format).await?;
                for r in more {
                    let id = (r.path.clone(), r.key.clone());
                    if visited.insert(id) {
                        queue.push(r.clone());
                        out.push(r);
                    }
                }
            }
        }
        Ok(out)
    }

    /// Read a single record by `(file_path, path, key)` within this
    /// worktree.
    pub async fn get_record(
        &self,
        file_path: &str,
        path: &str,
        key: &str,
    ) -> Result<Option<Record>> {
        get_record_inner(self.db(), self.worktree_id(), file_path, path, key).await
    }

    /// Read a single record by primary key.
    pub async fn get_record_by_id(&self, id: i64) -> Result<Option<Record>> {
        get_record_by_id_inner(self.db(), id).await
    }

    /// Read the `file` row for a given working-tree path. Returns
    /// `None` if no row exists for this worktree.
    pub async fn get_file(&self, file_path: &str) -> Result<Option<crate::model::File>> {
        get_file_inner(self.db(), self.worktree_id(), file_path).await
    }

    /// Read the `worktree` row this `GitSync` is bound to.
    pub async fn get_worktree(&self) -> Result<crate::model::Worktree> {
        get_worktree_inner(self.db(), self.worktree_id()).await
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
        let dirty: Vec<String> = list_dirty_files(self.db(), self.worktree_id()).await?;
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
        let pending = load_pending_records(self.db(), self.worktree_id(), file_path).await?;
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
        let dirty: Vec<String> = list_dirty_files(self.db(), self.worktree_id()).await?;
        if dirty.is_empty() {
            return Ok(None);
        }
        let _written = self.save_changes().await?;

        // Create the commit using gix.
        let repo = self.repo()?;
        let oid = git::commit_paths(&repo, &dirty, message)?;
        let oid_str = oid.to_string();

        // Roll the commit id into the dirty rows in a single transaction.
        roll_commit_forward(self.db(), self.worktree_id(), &dirty, &oid_str).await?;

        Ok(Some(oid_str))
    }
}

// ---------------------------------------------------------------------------
// Free helpers — split out so the impl block stays readable.
// ---------------------------------------------------------------------------

async fn upsert_worktree(db: &Db, origin: &str, branch: &str) -> Result<i64> {
    match db {
        Db::Sqlite(pool) => {
            let row: Option<(i64,)> =
                sqlx::query_as("SELECT id FROM worktree WHERE origin = ?1 AND branch = ?2")
                    .bind(origin)
                    .bind(branch)
                    .fetch_optional(pool)
                    .await?;
            if let Some((id,)) = row {
                return Ok(id);
            }
            let row: (i64,) = sqlx::query_as(
                "INSERT INTO worktree (origin, branch) VALUES (?1, ?2) RETURNING id",
            )
            .bind(origin)
            .bind(branch)
            .fetch_one(pool)
            .await?;
            Ok(row.0)
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let row: Option<(i64,)> =
                sqlx::query_as("SELECT id FROM worktree WHERE origin = $1 AND branch = $2")
                    .bind(origin)
                    .bind(branch)
                    .fetch_optional(pool)
                    .await?;
            if let Some((id,)) = row {
                return Ok(id);
            }
            let row: (i64,) = sqlx::query_as(
                "INSERT INTO worktree (origin, branch) VALUES ($1, $2) RETURNING id",
            )
            .bind(origin)
            .bind(branch)
            .fetch_one(pool)
            .await?;
            Ok(row.0)
        }
    }
}

async fn update_worktree_commit(db: &Db, worktree_id: i64, commit: Option<&str>) -> Result<()> {
    match db {
        Db::Sqlite(pool) => {
            sqlx::query("UPDATE worktree SET commit_id = ?1 WHERE id = ?2")
                .bind(commit)
                .bind(worktree_id)
                .execute(pool)
                .await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            sqlx::query("UPDATE worktree SET commit_id = $1 WHERE id = $2")
                .bind(commit)
                .bind(worktree_id)
                .execute(pool)
                .await?;
        }
    }
    Ok(())
}

async fn upsert_file(
    db: &Db,
    worktree_id: i64,
    path: &str,
    format: &str,
    commit_id: Option<&str>,
) -> Result<()> {
    match db {
        Db::Sqlite(pool) => {
            sqlx::query(
                "INSERT INTO file (worktree_id, path, format, commit_id) \
                 VALUES (?1, ?2, ?3, ?4) \
                 ON CONFLICT(worktree_id, path) DO UPDATE SET \
                   format = excluded.format, \
                   commit_id = excluded.commit_id",
            )
            .bind(worktree_id)
            .bind(path)
            .bind(format)
            .bind(commit_id)
            .execute(pool)
            .await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            sqlx::query(
                "INSERT INTO file (worktree_id, path, format, commit_id) \
                 VALUES ($1, $2, $3, $4) \
                 ON CONFLICT(worktree_id, path) DO UPDATE SET \
                   format = EXCLUDED.format, \
                   commit_id = EXCLUDED.commit_id",
            )
            .bind(worktree_id)
            .bind(path)
            .bind(format)
            .bind(commit_id)
            .execute(pool)
            .await?;
        }
    }
    Ok(())
}

async fn upsert_record_row(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
    json: &serde_json::Value,
    commit_id: Option<&str>,
) -> Result<i64> {
    let json_text = serde_json::to_string(json).map_err(|e| Error::Json {
        path: path.to_string(),
        source: e,
    })?;
    // Re-syncing from disk: any tombstone for this (path, key) must be
    // cleared since the value is reappearing in the source of truth.
    match db {
        Db::Sqlite(pool) => {
            let row: (i64,) = sqlx::query_as(
                "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, deleted) \
                 VALUES (?1, ?2, ?3, ?4, jsonb(?5), ?6, 0) \
                 ON CONFLICT(worktree_id, file_path, path, key) DO UPDATE SET \
                   json = excluded.json, \
                   commit_id = excluded.commit_id, \
                   deleted = 0 \
                 RETURNING id",
            )
            .bind(worktree_id)
            .bind(file_path)
            .bind(path)
            .bind(key)
            .bind(&json_text)
            .bind(commit_id)
            .fetch_one(pool)
            .await?;
            Ok(row.0)
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let row: (i64,) = sqlx::query_as(
                "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, deleted) \
                 VALUES ($1, $2, $3, $4, $5::jsonb, $6, FALSE) \
                 ON CONFLICT(worktree_id, file_path, path, key) DO UPDATE SET \
                   json = EXCLUDED.json, \
                   commit_id = EXCLUDED.commit_id, \
                   deleted = FALSE \
                 RETURNING id",
            )
            .bind(worktree_id)
            .bind(file_path)
            .bind(path)
            .bind(key)
            .bind(&json_text)
            .bind(commit_id)
            .fetch_one(pool)
            .await?;
            Ok(row.0)
        }
    }
}

async fn replace_aliases(db: &Db, record_id: i64, aliases: &[(String, String)]) -> Result<()> {
    match db {
        Db::Sqlite(pool) => {
            let mut tx = pool.begin().await?;
            sqlx::query("DELETE FROM alias WHERE record_id = ?1")
                .bind(record_id)
                .execute(&mut *tx)
                .await?;
            for (p, k) in aliases {
                sqlx::query(
                    "INSERT OR IGNORE INTO alias (record_id, path, key) VALUES (?1, ?2, ?3)",
                )
                .bind(record_id)
                .bind(p)
                .bind(k)
                .execute(&mut *tx)
                .await?;
            }
            tx.commit().await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let mut tx = pool.begin().await?;
            sqlx::query("DELETE FROM alias WHERE record_id = $1")
                .bind(record_id)
                .execute(&mut *tx)
                .await?;
            for (p, k) in aliases {
                sqlx::query(
                    "INSERT INTO alias (record_id, path, key) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                )
                .bind(record_id)
                .bind(p)
                .bind(k)
                .execute(&mut *tx)
                .await?;
            }
            tx.commit().await?;
        }
    }
    Ok(())
}

async fn delete_missing_records(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
    keep: &BTreeSet<(String, String)>,
) -> Result<usize> {
    // Find all current (path, key) pairs for the file then delete those
    // not in the keep set. Per-row deletes keep the SQL simple — file
    // record counts are small.
    match db {
        Db::Sqlite(pool) => {
            let rows: Vec<(String, String)> = sqlx::query_as(
                "SELECT path, key FROM record WHERE worktree_id = ?1 AND file_path = ?2",
            )
            .bind(worktree_id)
            .bind(file_path)
            .fetch_all(pool)
            .await?;
            let mut removed = 0usize;
            for (p, k) in rows {
                if keep.contains(&(p.clone(), k.clone())) {
                    continue;
                }
                let res = sqlx::query(
                    "DELETE FROM record WHERE worktree_id = ?1 AND file_path = ?2 \
                     AND path = ?3 AND key = ?4",
                )
                .bind(worktree_id)
                .bind(file_path)
                .bind(&p)
                .bind(&k)
                .execute(pool)
                .await?;
                removed += res.rows_affected() as usize;
            }
            Ok(removed)
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let rows: Vec<(String, String)> = sqlx::query_as(
                "SELECT path, key FROM record WHERE worktree_id = $1 AND file_path = $2",
            )
            .bind(worktree_id)
            .bind(file_path)
            .fetch_all(pool)
            .await?;
            let mut removed = 0usize;
            for (p, k) in rows {
                if keep.contains(&(p.clone(), k.clone())) {
                    continue;
                }
                let res = sqlx::query(
                    "DELETE FROM record WHERE worktree_id = $1 AND file_path = $2 \
                     AND path = $3 AND key = $4",
                )
                .bind(worktree_id)
                .bind(file_path)
                .bind(&p)
                .bind(&k)
                .execute(pool)
                .await?;
                removed += res.rows_affected() as usize;
            }
            Ok(removed)
        }
    }
}

async fn list_dirty_files(db: &Db, worktree_id: i64) -> Result<Vec<String>> {
    // A file is dirty when it has at least one record row with
    // commit_id IS NULL — either an in-flight update / upsert (json
    // pending) or an in-flight delete (tombstone). Both cases need
    // `save_changes` to rewrite the file on disk.
    match db {
        Db::Sqlite(pool) => {
            let rows: Vec<(String,)> = sqlx::query_as(
                "SELECT DISTINCT file_path FROM record \
                 WHERE worktree_id = ?1 AND commit_id IS NULL",
            )
            .bind(worktree_id)
            .fetch_all(pool)
            .await?;
            Ok(rows.into_iter().map(|(p,)| p).collect())
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let rows: Vec<(String,)> = sqlx::query_as(
                "SELECT DISTINCT file_path FROM record \
                 WHERE worktree_id = $1 AND commit_id IS NULL",
            )
            .bind(worktree_id)
            .fetch_all(pool)
            .await?;
            Ok(rows.into_iter().map(|(p,)| p).collect())
        }
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

/// Load all in-flight (commit_id IS NULL) record changes for `file_path`,
/// including tombstones (`deleted = TRUE`). Used by `write_file` to
/// apply only the diff against the on-disk document.
async fn load_pending_records(db: &Db, worktree_id: i64, file_path: &str) -> Result<Vec<Record>> {
    let rows = match db {
        Db::Sqlite(pool) => {
            sqlx::query_as::<_, (i64, String, String, Option<String>, String, i64)>(
                "SELECT id, path, key, commit_id, json(json), deleted FROM record \
                 WHERE worktree_id = ?1 AND file_path = ?2 AND commit_id IS NULL \
                 ORDER BY id",
            )
            .bind(worktree_id)
            .bind(file_path)
            .fetch_all(pool)
            .await?
            .into_iter()
            .map(|(id, p, k, c, t, d)| (id, p, k, c, t, d != 0))
            .collect::<Vec<_>>()
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let pg_rows: Vec<(i64, String, String, Option<String>, serde_json::Value, bool)> =
                sqlx::query_as(
                    "SELECT id, path, key, commit_id, json::jsonb, deleted FROM record \
                     WHERE worktree_id = $1 AND file_path = $2 AND commit_id IS NULL \
                     ORDER BY id",
                )
                .bind(worktree_id)
                .bind(file_path)
                .fetch_all(pool)
                .await?;
            pg_rows
                .into_iter()
                .map(|(id, p, k, c, v, d)| (id, p, k, c, v.to_string(), d))
                .collect()
        }
    };

    let mut out = Vec::with_capacity(rows.len());
    for (id, path, key, commit_id, json_text, deleted) in rows {
        let json: serde_json::Value =
            serde_json::from_str(&json_text).map_err(|e| Error::Json {
                path: path.clone(),
                source: e,
            })?;
        out.push(Record {
            id,
            worktree_id,
            file_path: file_path.to_string(),
            path,
            key,
            commit_id,
            json,
            deleted,
        });
    }
    Ok(out)
}

async fn find_records_inner(
    db: &Db,
    worktree_id: i64,
    targets: &[(String, Option<String>)],
    format: &str,
) -> Result<Vec<Record>> {
    // Split targets into "path-only" (any key under that parent) and
    // "exact (path, key)" buckets so we can build the WHERE clause
    // simply in either dialect.
    let mut path_only: Vec<String> = Vec::new();
    let mut path_key: Vec<(String, String)> = Vec::new();
    for (p, k) in targets {
        match k {
            Some(k) => path_key.push((p.clone(), k.clone())),
            None => path_only.push(p.clone()),
        }
    }

    match db {
        Db::Sqlite(pool) => {
            let mut sql = String::from(
                "SELECT r.id, r.file_path, r.path, r.key, r.commit_id, json(r.json) FROM record r \
                 JOIN file f ON f.worktree_id = r.worktree_id AND f.path = r.file_path \
                 WHERE r.worktree_id = ?1 AND f.format = ?2 AND r.deleted = 0",
            );
            if !targets.is_empty() {
                // Bind layout: ?1 = worktree_id, ?2 = format, then
                // path_only paths, then path_key flattened (path, key)*,
                // and then again for the alias subquery.
                sql.push_str(" AND (");
                let mut idx: usize = 3;
                let mut clauses: Vec<String> = Vec::new();
                if !path_only.is_empty() {
                    let placeholders: Vec<String> = (0..path_only.len())
                        .map(|i| format!("?{}", idx + i))
                        .collect();
                    clauses.push(format!("r.path IN ({})", placeholders.join(",")));
                    idx += path_only.len();
                }
                if !path_key.is_empty() {
                    let pairs: Vec<String> = (0..path_key.len())
                        .map(|i| {
                            let p = idx + i * 2;
                            let k = idx + i * 2 + 1;
                            format!("(r.path = ?{p} AND r.key = ?{k})")
                        })
                        .collect();
                    clauses.push(format!("({})", pairs.join(" OR ")));
                    idx += path_key.len() * 2;
                }
                if !path_key.is_empty() {
                    let pairs: Vec<String> = (0..path_key.len())
                        .map(|i| {
                            let p = idx + i * 2;
                            let k = idx + i * 2 + 1;
                            format!("(a.path = ?{p} AND a.key = ?{k})")
                        })
                        .collect();
                    clauses.push(format!(
                        "EXISTS (SELECT 1 FROM alias a WHERE a.record_id = r.id AND ({}))",
                        pairs.join(" OR ")
                    ));
                }
                sql.push_str(&clauses.join(" OR "));
                sql.push(')');
            }
            sql.push_str(" ORDER BY r.path, r.key");

            let mut q =
                sqlx::query_as::<_, (i64, String, String, String, Option<String>, String)>(&sql)
                    .bind(worktree_id)
                    .bind(format);
            for p in &path_only {
                q = q.bind(p);
            }
            for (p, k) in &path_key {
                q = q.bind(p).bind(k);
            }
            // alias clause repeats path_key.
            for (p, k) in &path_key {
                q = q.bind(p).bind(k);
            }
            let rows = q.fetch_all(pool).await?;

            let mut out = Vec::with_capacity(rows.len());
            for (id, file_path, path, key, commit_id, json_text) in rows {
                let json: serde_json::Value =
                    serde_json::from_str(&json_text).map_err(|e| Error::Json {
                        path: path.clone(),
                        source: e,
                    })?;
                out.push(Record {
                    id,
                    worktree_id,
                    file_path,
                    path,
                    key,
                    commit_id,
                    json,
                    deleted: false,
                });
            }
            Ok(out)
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            if targets.is_empty() {
                let rows: Vec<(
                    i64,
                    String,
                    String,
                    String,
                    Option<String>,
                    serde_json::Value,
                )> = sqlx::query_as(
                    "SELECT r.id, r.file_path, r.path, r.key, r.commit_id, r.json \
                         FROM record r \
                         JOIN file f ON f.worktree_id = r.worktree_id AND f.path = r.file_path \
                         WHERE r.worktree_id = $1 AND f.format = $2 AND r.deleted = FALSE \
                         ORDER BY r.path, r.key",
                )
                .bind(worktree_id)
                .bind(format)
                .fetch_all(pool)
                .await?;
                return Ok(rows
                    .into_iter()
                    .map(|(id, fp, p, k, cid, json)| Record {
                        id,
                        worktree_id,
                        file_path: fp,
                        path: p,
                        key: k,
                        commit_id: cid,
                        json,
                        deleted: false,
                    })
                    .collect());
            }

            // Build the SQL with $-placeholders.
            let mut sql = String::from(
                "SELECT r.id, r.file_path, r.path, r.key, r.commit_id, r.json \
                 FROM record r \
                 JOIN file f ON f.worktree_id = r.worktree_id AND f.path = r.file_path \
                 WHERE r.worktree_id = $1 AND f.format = $2 AND r.deleted = FALSE AND (",
            );
            let mut idx: usize = 3;
            let mut clauses: Vec<String> = Vec::new();
            if !path_only.is_empty() {
                let placeholders: Vec<String> = (0..path_only.len())
                    .map(|i| format!("${}", idx + i))
                    .collect();
                clauses.push(format!("r.path IN ({})", placeholders.join(",")));
                idx += path_only.len();
            }
            if !path_key.is_empty() {
                let pairs: Vec<String> = (0..path_key.len())
                    .map(|i| {
                        let p = idx + i * 2;
                        let k = idx + i * 2 + 1;
                        format!("(r.path = ${p} AND r.key = ${k})")
                    })
                    .collect();
                clauses.push(format!("({})", pairs.join(" OR ")));
                idx += path_key.len() * 2;
            }
            if !path_key.is_empty() {
                let pairs: Vec<String> = (0..path_key.len())
                    .map(|i| {
                        let p = idx + i * 2;
                        let k = idx + i * 2 + 1;
                        format!("(a.path = ${p} AND a.key = ${k})")
                    })
                    .collect();
                clauses.push(format!(
                    "EXISTS (SELECT 1 FROM alias a WHERE a.record_id = r.id AND ({}))",
                    pairs.join(" OR ")
                ));
            }
            sql.push_str(&clauses.join(" OR "));
            sql.push_str(") ORDER BY r.path, r.key");

            let mut q = sqlx::query_as::<
                _,
                (
                    i64,
                    String,
                    String,
                    String,
                    Option<String>,
                    serde_json::Value,
                ),
            >(&sql)
            .bind(worktree_id)
            .bind(format);
            for p in &path_only {
                q = q.bind(p);
            }
            for (p, k) in &path_key {
                q = q.bind(p).bind(k);
            }
            for (p, k) in &path_key {
                q = q.bind(p).bind(k);
            }
            let rows = q.fetch_all(pool).await?;
            Ok(rows
                .into_iter()
                .map(|(id, fp, p, k, cid, json)| Record {
                    id,
                    worktree_id,
                    file_path: fp,
                    path: p,
                    key: k,
                    commit_id: cid,
                    json,
                    deleted: false,
                })
                .collect())
        }
    }
}

async fn get_record_inner(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
) -> Result<Option<Record>> {
    match db {
        Db::Sqlite(pool) => {
            let row: Option<(i64, Option<String>, String)> = sqlx::query_as(
                "SELECT id, commit_id, json(json) FROM record \
                 WHERE worktree_id = ?1 AND file_path = ?2 AND path = ?3 AND key = ?4 \
                   AND deleted = 0",
            )
            .bind(worktree_id)
            .bind(file_path)
            .bind(path)
            .bind(key)
            .fetch_optional(pool)
            .await?;
            row_to_record(row, worktree_id, file_path, path, key)
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let row: Option<(i64, Option<String>, serde_json::Value)> = sqlx::query_as(
                "SELECT id, commit_id, json FROM record \
                 WHERE worktree_id = $1 AND file_path = $2 AND path = $3 AND key = $4 \
                   AND deleted = FALSE",
            )
            .bind(worktree_id)
            .bind(file_path)
            .bind(path)
            .bind(key)
            .fetch_optional(pool)
            .await?;
            match row {
                Some((id, commit_id, json)) => Ok(Some(Record {
                    id,
                    worktree_id,
                    file_path: file_path.to_string(),
                    path: path.to_string(),
                    key: key.to_string(),
                    commit_id,
                    json,
                    deleted: false,
                })),
                None => Ok(None),
            }
        }
    }
}

fn row_to_record(
    row: Option<(i64, Option<String>, String)>,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
) -> Result<Option<Record>> {
    match row {
        Some((id, commit_id, json_text)) => {
            let json: serde_json::Value =
                serde_json::from_str(&json_text).map_err(|e| Error::Json {
                    path: path.to_string(),
                    source: e,
                })?;
            Ok(Some(Record {
                id,
                worktree_id,
                file_path: file_path.to_string(),
                path: path.to_string(),
                key: key.to_string(),
                commit_id,
                json,
                deleted: false,
            }))
        }
        None => Ok(None),
    }
}

/// `(id, worktree_id, file_path, path, key, commit_id, json_text,
/// deleted)` — the row shape for the SQLite `get_record_by_id_inner`
/// query. Aliased to keep clippy's `type_complexity` lint happy.
type FullRecordRowSqlite = (
    i64,
    i64,
    String,
    String,
    String,
    Option<String>,
    String,
    i64,
);

#[cfg(feature = "postgres")]
type FullRecordRowPg = (
    i64,
    i64,
    String,
    String,
    String,
    Option<String>,
    serde_json::Value,
    bool,
);

/// Row shape for `get_file_inner` on SQLite.
type FileRowSqlite = (i64, String, String, Option<String>);

/// Row shape for `get_file_inner` on Postgres.
#[cfg(feature = "postgres")]
type FileRowPg = (i64, String, String, Option<String>);

async fn get_record_by_id_inner(db: &Db, id: i64) -> Result<Option<Record>> {
    match db {
        Db::Sqlite(pool) => {
            let row: Option<FullRecordRowSqlite> = sqlx::query_as(
                "SELECT id, worktree_id, file_path, path, key, commit_id, json(json), deleted \
                     FROM record WHERE id = ?1",
            )
            .bind(id)
            .fetch_optional(pool)
            .await?;
            match row {
                Some((id, wt, fp, p, k, c, t, d)) => {
                    let json: serde_json::Value =
                        serde_json::from_str(&t).map_err(|e| Error::Json {
                            path: p.clone(),
                            source: e,
                        })?;
                    Ok(Some(Record {
                        id,
                        worktree_id: wt,
                        file_path: fp,
                        path: p,
                        key: k,
                        commit_id: c,
                        json,
                        deleted: d != 0,
                    }))
                }
                None => Ok(None),
            }
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let row: Option<FullRecordRowPg> = sqlx::query_as(
                "SELECT id, worktree_id, file_path, path, key, commit_id, json, deleted \
                     FROM record WHERE id = $1",
            )
            .bind(id)
            .fetch_optional(pool)
            .await?;
            match row {
                Some((id, wt, fp, p, k, c, json, d)) => Ok(Some(Record {
                    id,
                    worktree_id: wt,
                    file_path: fp,
                    path: p,
                    key: k,
                    commit_id: c,
                    json,
                    deleted: d,
                })),
                None => Ok(None),
            }
        }
    }
}

async fn get_file_inner(
    db: &Db,
    worktree_id: i64,
    file_path: &str,
) -> Result<Option<crate::model::File>> {
    match db {
        Db::Sqlite(pool) => {
            let row: Option<FileRowSqlite> = sqlx::query_as(
                "SELECT worktree_id, path, format, commit_id \
                 FROM file WHERE worktree_id = ?1 AND path = ?2",
            )
            .bind(worktree_id)
            .bind(file_path)
            .fetch_optional(pool)
            .await?;
            Ok(row.map(|(wt, path, format, commit_id)| crate::model::File {
                worktree_id: wt,
                path,
                format,
                commit_id,
            }))
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let row: Option<FileRowPg> = sqlx::query_as(
                "SELECT worktree_id, path, format, commit_id \
                 FROM file WHERE worktree_id = $1 AND path = $2",
            )
            .bind(worktree_id)
            .bind(file_path)
            .fetch_optional(pool)
            .await?;
            Ok(row.map(|(wt, path, format, commit_id)| crate::model::File {
                worktree_id: wt,
                path,
                format,
                commit_id,
            }))
        }
    }
}

async fn get_worktree_inner(db: &Db, worktree_id: i64) -> Result<crate::model::Worktree> {
    let row: (i64, String, String, Option<String>) = match db {
        Db::Sqlite(pool) => {
            sqlx::query_as("SELECT id, origin, branch, commit_id FROM worktree WHERE id = ?1")
                .bind(worktree_id)
                .fetch_one(pool)
                .await?
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            sqlx::query_as("SELECT id, origin, branch, commit_id FROM worktree WHERE id = $1")
                .bind(worktree_id)
                .fetch_one(pool)
                .await?
        }
    };
    Ok(crate::model::Worktree {
        id: row.0,
        origin: row.1,
        branch: row.2,
        commit_id: row.3,
    })
}

async fn roll_commit_forward(
    db: &Db,
    worktree_id: i64,
    files: &[String],
    new_commit: &str,
) -> Result<()> {
    if files.is_empty() {
        return Ok(());
    }
    // Order of operations (see plan):
    //   1. roll commit forward on live, in-flight rows;
    //   2. purge tombstones (their on-disk effect is already in the
    //      commit);
    //   3. roll commit forward on file rows;
    //   4. roll commit forward on the worktree row.
    match db {
        Db::Sqlite(pool) => {
            let mut tx = pool.begin().await?;
            sqlx::query(
                "UPDATE record SET commit_id = ?1 \
                 WHERE worktree_id = ?2 AND commit_id IS NULL AND deleted = 0",
            )
            .bind(new_commit)
            .bind(worktree_id)
            .execute(&mut *tx)
            .await?;
            sqlx::query("DELETE FROM record WHERE worktree_id = ?1 AND deleted = 1")
                .bind(worktree_id)
                .execute(&mut *tx)
                .await?;
            let placeholders: Vec<String> =
                (0..files.len()).map(|i| format!("?{}", i + 3)).collect();
            let sql = format!(
                "UPDATE file SET commit_id = ?1 WHERE worktree_id = ?2 AND path IN ({})",
                placeholders.join(",")
            );
            let mut q = sqlx::query(&sql).bind(new_commit).bind(worktree_id);
            for f in files {
                q = q.bind(f);
            }
            q.execute(&mut *tx).await?;
            sqlx::query("UPDATE worktree SET commit_id = ?1 WHERE id = ?2")
                .bind(new_commit)
                .bind(worktree_id)
                .execute(&mut *tx)
                .await?;
            tx.commit().await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let mut tx = pool.begin().await?;
            sqlx::query(
                "UPDATE record SET commit_id = $1 \
                 WHERE worktree_id = $2 AND commit_id IS NULL AND deleted = FALSE",
            )
            .bind(new_commit)
            .bind(worktree_id)
            .execute(&mut *tx)
            .await?;
            sqlx::query("DELETE FROM record WHERE worktree_id = $1 AND deleted = TRUE")
                .bind(worktree_id)
                .execute(&mut *tx)
                .await?;
            sqlx::query("UPDATE file SET commit_id = $1 WHERE worktree_id = $2 AND path = ANY($3)")
                .bind(new_commit)
                .bind(worktree_id)
                .bind(files)
                .execute(&mut *tx)
                .await?;
            sqlx::query("UPDATE worktree SET commit_id = $1 WHERE id = $2")
                .bind(new_commit)
                .bind(worktree_id)
                .execute(&mut *tx)
                .await?;
            tx.commit().await?;
        }
    }
    Ok(())
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
            tx_ensure_file_row_sqlite(&mut tx, sync.worktree_id(), file_path).await?;
            let lookup =
                tx_lookup_commits_sqlite(&mut tx, sync.worktree_id(), file_path, path, key).await?;
            // Live row → conflict. Tombstones are treated as absent so
            // the caller's `create_record` can resurrect them.
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
            // Either INSERT (no row) or UPDATE-and-clear (resurrect a
            // tombstone). The unique index `(worktree_id, file_path,
            // path, key)` ensures both branches converge on the same
            // row id.
            let row: (i64,) = sqlx::query_as(
                "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, deleted) \
                 VALUES (?1, ?2, ?3, ?4, jsonb(?5), NULL, 0) \
                 ON CONFLICT(worktree_id, file_path, path, key) DO UPDATE SET \
                   json = excluded.json, commit_id = NULL, deleted = 0 \
                 RETURNING id",
            )
            .bind(sync.worktree_id())
            .bind(file_path)
            .bind(path)
            .bind(key)
            .bind(&json_text)
            .fetch_one(&mut *tx)
            .await?;
            id = row.0;
            format_owner = tx_file_format_sqlite(&mut tx, sync.worktree_id(), file_path).await?;
            tx_replace_aliases_sqlite(
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
            tx_ensure_file_row_pg(&mut tx, sync.worktree_id(), file_path).await?;
            let lookup =
                tx_lookup_commits_pg(&mut tx, sync.worktree_id(), file_path, path, key).await?;
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
            let row: (i64,) = sqlx::query_as(
                "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, deleted) \
                 VALUES ($1, $2, $3, $4, $5::jsonb, NULL, FALSE) \
                 ON CONFLICT(worktree_id, file_path, path, key) DO UPDATE SET \
                   json = EXCLUDED.json, commit_id = NULL, deleted = FALSE \
                 RETURNING id",
            )
            .bind(sync.worktree_id())
            .bind(file_path)
            .bind(path)
            .bind(key)
            .bind(&json_text)
            .fetch_one(&mut *tx)
            .await?;
            id = row.0;
            format_owner = tx_file_format_pg(&mut tx, sync.worktree_id(), file_path).await?;
            tx_replace_aliases_pg(
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
                tx_lookup_commits_sqlite(&mut tx, sync.worktree_id(), file_path, path, key).await?;
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
            sqlx::query(
                "UPDATE record SET json = jsonb(?1), commit_id = NULL, deleted = 0 \
                 WHERE id = ?2",
            )
            .bind(&json_text)
            .bind(id)
            .execute(&mut *tx)
            .await?;
            let format_owner =
                tx_file_format_sqlite(&mut tx, sync.worktree_id(), file_path).await?;
            tx_replace_aliases_sqlite(
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
                tx_lookup_commits_pg(&mut tx, sync.worktree_id(), file_path, path, key).await?;
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
            sqlx::query(
                "UPDATE record SET json = $1::jsonb, commit_id = NULL, deleted = FALSE \
                 WHERE id = $2",
            )
            .bind(&json_text)
            .bind(id)
            .execute(&mut *tx)
            .await?;
            let format_owner = tx_file_format_pg(&mut tx, sync.worktree_id(), file_path).await?;
            tx_replace_aliases_pg(
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
            tx_ensure_file_row_sqlite(&mut tx, sync.worktree_id(), file_path).await?;
            let lookup =
                tx_lookup_commits_sqlite(&mut tx, sync.worktree_id(), file_path, path, key).await?;
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
            let row: (i64,) = sqlx::query_as(
                "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, deleted) \
                 VALUES (?1, ?2, ?3, ?4, jsonb(?5), NULL, 0) \
                 ON CONFLICT(worktree_id, file_path, path, key) DO UPDATE SET \
                   json = excluded.json, commit_id = NULL, deleted = 0 \
                 RETURNING id",
            )
            .bind(sync.worktree_id())
            .bind(file_path)
            .bind(path)
            .bind(key)
            .bind(&json_text)
            .fetch_one(&mut *tx)
            .await?;
            id = row.0;
            let format_owner =
                tx_file_format_sqlite(&mut tx, sync.worktree_id(), file_path).await?;
            tx_replace_aliases_sqlite(
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
            tx_ensure_file_row_pg(&mut tx, sync.worktree_id(), file_path).await?;
            let lookup =
                tx_lookup_commits_pg(&mut tx, sync.worktree_id(), file_path, path, key).await?;
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
            let row: (i64,) = sqlx::query_as(
                "INSERT INTO record (worktree_id, file_path, path, key, json, commit_id, deleted) \
                 VALUES ($1, $2, $3, $4, $5::jsonb, NULL, FALSE) \
                 ON CONFLICT(worktree_id, file_path, path, key) DO UPDATE SET \
                   json = EXCLUDED.json, commit_id = NULL, deleted = FALSE \
                 RETURNING id",
            )
            .bind(sync.worktree_id())
            .bind(file_path)
            .bind(path)
            .bind(key)
            .bind(&json_text)
            .fetch_one(&mut *tx)
            .await?;
            id = row.0;
            let format_owner = tx_file_format_pg(&mut tx, sync.worktree_id(), file_path).await?;
            tx_replace_aliases_pg(
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
                tx_lookup_commits_sqlite(&mut tx, sync.worktree_id(), file_path, path, key).await?;
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
            sqlx::query("UPDATE record SET deleted = 1, commit_id = NULL WHERE id = ?1")
                .bind(id)
                .execute(&mut *tx)
                .await?;
            // Aliases are no longer reachable through the live row.
            sqlx::query("DELETE FROM alias WHERE record_id = ?1")
                .bind(id)
                .execute(&mut *tx)
                .await?;
            tx.commit().await?;
        }
        #[cfg(feature = "postgres")]
        Db::Postgres(pool) => {
            let mut tx = pool.begin().await?;
            let lookup =
                tx_lookup_commits_pg(&mut tx, sync.worktree_id(), file_path, path, key).await?;
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
            sqlx::query("UPDATE record SET deleted = TRUE, commit_id = NULL WHERE id = $1")
                .bind(id)
                .execute(&mut *tx)
                .await?;
            sqlx::query("DELETE FROM alias WHERE record_id = $1")
                .bind(id)
                .execute(&mut *tx)
                .await?;
            tx.commit().await?;
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Transaction-scoped helpers used by the CRUD primitives.
// ---------------------------------------------------------------------------

async fn tx_ensure_file_row_sqlite(
    tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    worktree_id: i64,
    file_path: &str,
) -> Result<()> {
    sqlx::query(
        "INSERT INTO file (worktree_id, path, format, commit_id) \
         VALUES (?1, ?2, 'unknown', NULL) \
         ON CONFLICT(worktree_id, path) DO NOTHING",
    )
    .bind(worktree_id)
    .bind(file_path)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

#[cfg(feature = "postgres")]
async fn tx_ensure_file_row_pg(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    worktree_id: i64,
    file_path: &str,
) -> Result<()> {
    sqlx::query(
        "INSERT INTO file (worktree_id, path, format, commit_id) \
         VALUES ($1, $2, 'unknown', NULL) \
         ON CONFLICT(worktree_id, path) DO NOTHING",
    )
    .bind(worktree_id)
    .bind(file_path)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

/// Lookup result from [`tx_lookup_commits_sqlite`] /
/// [`tx_lookup_commits_pg`]. Tombstones surface as if absent: the
/// `record_id` and `record_commit` fields are `None`, and the conflict
/// checker falls back to `file_commit`.
struct RecordLookup {
    /// Live record id; `None` when the row is absent or a tombstone.
    record_id: Option<i64>,
    /// Live row's `commit_id`; `None` when absent or a tombstone.
    record_commit: Option<String>,
    /// File row's `commit_id` (used as a fallback in the conflict
    /// check when `record_id.is_none()`).
    file_commit: Option<String>,
}

/// `(record.id, record.commit_id, record.deleted, file.commit_id)` — the
/// row shape of [`tx_lookup_commits_sqlite`]. Aliased to keep clippy's
/// `type_complexity` lint happy.
type LookupRowSqlite = (Option<i64>, Option<String>, Option<i64>, Option<String>);

#[cfg(feature = "postgres")]
type LookupRowPg = (Option<i64>, Option<String>, Option<bool>, Option<String>);

async fn tx_lookup_commits_sqlite(
    tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
) -> Result<RecordLookup> {
    let row: Option<LookupRowSqlite> = sqlx::query_as(
        "SELECT r.id, r.commit_id, r.deleted, f.commit_id FROM file f \
         LEFT JOIN record r ON r.worktree_id = f.worktree_id \
                           AND r.file_path = f.path \
                           AND r.path = ?3 \
                           AND r.key = ?4 \
         WHERE f.worktree_id = ?1 AND f.path = ?2",
    )
    .bind(worktree_id)
    .bind(file_path)
    .bind(path)
    .bind(key)
    .fetch_optional(&mut **tx)
    .await?;
    let (raw_id, rec_commit, deleted, file_commit) = row.unwrap_or((None, None, None, None));
    let is_tombstone = matches!(deleted, Some(d) if d != 0);
    let record_id = match (raw_id, is_tombstone) {
        (Some(id), false) => Some(id),
        _ => None,
    };
    let record_commit = if record_id.is_some() {
        rec_commit
    } else {
        None
    };
    Ok(RecordLookup {
        record_id,
        record_commit,
        file_commit,
    })
}

#[cfg(feature = "postgres")]
async fn tx_lookup_commits_pg(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    worktree_id: i64,
    file_path: &str,
    path: &str,
    key: &str,
) -> Result<RecordLookup> {
    let row: Option<LookupRowPg> = sqlx::query_as(
        "SELECT r.id, r.commit_id, r.deleted, f.commit_id FROM file f \
         LEFT JOIN record r ON r.worktree_id = f.worktree_id \
                           AND r.file_path = f.path \
                           AND r.path = $3 \
                           AND r.key = $4 \
         WHERE f.worktree_id = $1 AND f.path = $2",
    )
    .bind(worktree_id)
    .bind(file_path)
    .bind(path)
    .bind(key)
    .fetch_optional(&mut **tx)
    .await?;
    let (raw_id, rec_commit, deleted, file_commit) = row.unwrap_or((None, None, None, None));
    let is_tombstone = matches!(deleted, Some(true));
    let record_id = match (raw_id, is_tombstone) {
        (Some(id), false) => Some(id),
        _ => None,
    };
    let record_commit = if record_id.is_some() {
        rec_commit
    } else {
        None
    };
    Ok(RecordLookup {
        record_id,
        record_commit,
        file_commit,
    })
}

async fn tx_file_format_sqlite(
    tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    worktree_id: i64,
    file_path: &str,
) -> Result<Option<String>> {
    let row: Option<(String,)> =
        sqlx::query_as("SELECT format FROM file WHERE worktree_id = ?1 AND path = ?2")
            .bind(worktree_id)
            .bind(file_path)
            .fetch_optional(&mut **tx)
            .await?;
    Ok(row.map(|(f,)| f))
}

#[cfg(feature = "postgres")]
async fn tx_file_format_pg(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    worktree_id: i64,
    file_path: &str,
) -> Result<Option<String>> {
    let row: Option<(String,)> =
        sqlx::query_as("SELECT format FROM file WHERE worktree_id = $1 AND path = $2")
            .bind(worktree_id)
            .bind(file_path)
            .fetch_optional(&mut **tx)
            .await?;
    Ok(row.map(|(f,)| f))
}

async fn tx_replace_aliases_sqlite(
    tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    record_id: i64,
    aliases: &[(String, String)],
) -> Result<()> {
    sqlx::query("DELETE FROM alias WHERE record_id = ?1")
        .bind(record_id)
        .execute(&mut **tx)
        .await?;
    for (p, k) in aliases {
        sqlx::query("INSERT OR IGNORE INTO alias (record_id, path, key) VALUES (?1, ?2, ?3)")
            .bind(record_id)
            .bind(p)
            .bind(k)
            .execute(&mut **tx)
            .await?;
    }
    Ok(())
}

#[cfg(feature = "postgres")]
async fn tx_replace_aliases_pg(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    record_id: i64,
    aliases: &[(String, String)],
) -> Result<()> {
    sqlx::query("DELETE FROM alias WHERE record_id = $1")
        .bind(record_id)
        .execute(&mut **tx)
        .await?;
    for (p, k) in aliases {
        sqlx::query(
            "INSERT INTO alias (record_id, path, key) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
        )
        .bind(record_id)
        .bind(p)
        .bind(k)
        .execute(&mut **tx)
        .await?;
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
