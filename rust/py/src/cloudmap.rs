// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `GitSyncDB`: a cloudmap file kept in a git-sync database, for
//! `unfurl/cloudmap/gitsync_db.py`. Records cross as JSON strings, so the
//! cloudmap schema stays on the Python side.

use std::future::Future;
use std::sync::{Arc, OnceLock};

use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use unfurl_git_sync::{
    git, CommitOptions, DbConfig, Error, FormatRegistry, OpenOptions, RecordQuery, ScanOptions,
    SyncOutcome, SyncedDir,
};

create_exception!(
    _rust,
    GitSyncError,
    PyException,
    "A git-sync operation failed."
);
create_exception!(
    _rust,
    GitSyncConflict,
    GitSyncError,
    "A write conflicts with a change to the file."
);
create_exception!(
    _rust,
    CommitMissing,
    GitSyncError,
    "The commit the database last saw isn't in the repository: scan with rebuild_missing."
);

/// One runtime for the process, shared by every handle.
fn runtime() -> &'static tokio::runtime::Runtime {
    static RUNTIME: OnceLock<tokio::runtime::Runtime> = OnceLock::new();
    RUNTIME.get_or_init(|| {
        tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("a tokio runtime")
    })
}

/// Run `future` to completion without holding the GIL.
fn run<T: Send>(
    py: Python<'_>,
    future: impl Future<Output = unfurl_git_sync::Result<T>> + Send,
) -> PyResult<T> {
    py.allow_threads(|| runtime().block_on(future))
        .map_err(to_py)
}

fn to_py(error: Error) -> PyErr {
    let message = error.to_string();
    match error {
        Error::Conflict { .. } => GitSyncConflict::new_err(message),
        Error::CommitMissing { .. } => CommitMissing::new_err(message),
        _ => GitSyncError::new_err(message),
    }
}

/// An outcome as a dict: the counts, the files written, and what didn't go
/// through, as `(file, section, key)` conflicts and `(file, message)`
/// failures.
fn outcome_dict<'py>(py: Python<'py>, outcome: &SyncOutcome) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("files_seen", outcome.files_seen)?;
    dict.set_item("files_parsed", outcome.files_parsed)?;
    dict.set_item("records_upserted", outcome.records_upserted)?;
    dict.set_item("records_deleted", outcome.records_deleted)?;
    let written: Vec<String> = outcome
        .written
        .iter()
        .map(|p| p.to_string_lossy().into_owned())
        .collect();
    dict.set_item("written", written)?;
    let conflicts: Vec<(&str, &str, &str)> = outcome
        .conflicts
        .iter()
        .map(|c| (c.file_path.as_str(), c.path.as_str(), c.key.as_str()))
        .collect();
    dict.set_item("conflicts", conflicts)?;
    let failures = |failed: &[unfurl_git_sync::FileFailure]| -> Vec<(String, String)> {
        failed
            .iter()
            .map(|f| (f.file_path.clone(), f.error.to_string()))
            .collect()
    };
    dict.set_item("failed", failures(&outcome.failed))?;
    dict.set_item("unparsed", failures(&outcome.unparsed))?;
    let invalid: Vec<&str> = outcome
        .invalid
        .iter()
        .map(|v| v.file_path.as_str())
        .collect();
    dict.set_item("invalid", invalid)?;
    Ok(dict)
}

/// One cloudmap file in a git working tree, read and written through a
/// git-sync database.
#[pyclass(frozen)]
pub struct GitSyncDB {
    sync: Arc<SyncedDir>,
    file_path: String,
}

#[pymethods]
impl GitSyncDB {
    /// Open the cloudmap at `file_path`, relative to `repo_path`, a git
    /// working tree's root or a directory in no repository, with the SQLite
    /// database `db_url` names (an sqlx URL, its path percent-encoded, such
    /// as `sqlite:///abs/path.sqlite?mode=rwc` to create it if it doesn't
    /// exist). The handle reads and writes only that file, committed or not.
    #[staticmethod]
    fn open(
        py: Python<'_>,
        repo_path: String,
        db_url: String,
        file_path: String,
    ) -> PyResult<Self> {
        if !db_url.starts_with("sqlite:") {
            return Err(PyValueError::new_err(format!(
                "not a SQLite database URL: {db_url}"
            )));
        }
        let db = DbConfig::Sqlite { url: db_url };
        let pathspec = format!(":(literal){file_path}");
        let options = OpenOptions::default()
            .pathspecs([pathspec.as_str()])
            .untracked(true);
        let sync = run(py, async move {
            SyncedDir::open(
                repo_path,
                db,
                FormatRegistry::with_builtins(),
                git::Trust::Full,
                options,
            )
            .await
        })?;
        Ok(Self {
            sync: Arc::new(sync),
            file_path,
        })
    }

    /// Take in the file and the repository's history since the last scan.
    #[pyo3(signature = (rebuild_missing=false))]
    fn scan<'py>(&self, py: Python<'py>, rebuild_missing: bool) -> PyResult<Bound<'py, PyDict>> {
        let sync = self.sync.clone();
        let options = ScanOptions::default().rebuild_missing(rebuild_missing);
        let outcome = run(
            py,
            async move { sync.update_from_working_dir(options).await },
        )?;
        outcome_dict(py, &outcome)
    }

    /// The records under `section` (a JSON pointer such as
    /// `/repositories`), as `(section, key, json)`: all of them, the one at
    /// `key` (or one of its aliases, with `alias`), or those of one of
    /// `type_names` (or their subtypes, with `subtypes`).
    #[pyo3(signature = (section, key=None, type_names=None, subtypes=true, alias=true))]
    fn find(
        &self,
        py: Python<'_>,
        section: &str,
        key: Option<&str>,
        type_names: Option<Vec<String>>,
        subtypes: bool,
        alias: bool,
    ) -> PyResult<Vec<(String, String, String)>> {
        let mut query = RecordQuery::default()
            .file_path(self.file_path.as_str())
            .path(section)
            .subtypes(subtypes)
            .alias(alias);
        if let Some(key) = key {
            query = query.key(key);
        }
        if let Some(names) = type_names {
            query = query.type_names(names);
        }
        let sync = self.sync.clone();
        let records = run(py, async move { sync.find_records(&query).await })?;
        Ok(records
            .into_iter()
            .map(|r| (r.path, r.key, r.json.to_string()))
            .collect())
    }

    /// Write the record at `key` under `section`, from the JSON `record`,
    /// pending until [`Self::save`].
    fn upsert(&self, py: Python<'_>, section: String, key: String, record: &str) -> PyResult<()> {
        let json: serde_json::Value =
            serde_json::from_str(record).map_err(|e| PyValueError::new_err(e.to_string()))?;
        let (sync, file_path) = (self.sync.clone(), self.file_path.clone());
        run(py, async move {
            sync.upsert_record(Some(&file_path), &section, &key, json, None, false)
                .await
        })?;
        Ok(())
    }

    /// Delete the record at `key` under `section`, pending until
    /// [`Self::save`].
    fn delete(&self, py: Python<'_>, section: String, key: String) -> PyResult<()> {
        let (sync, file_path) = (self.sync.clone(), self.file_path.clone());
        run(py, async move {
            sync.delete_record(Some(&file_path), &section, &key, None, false)
                .await
        })?;
        Ok(())
    }

    /// Write the pending changes into the file.
    fn save<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let sync = self.sync.clone();
        let outcome = run(py, async move { sync.save_changes().await })?;
        outcome_dict(py, &outcome)
    }

    /// Commit the saved changes with `message`; the new commit, or `None`
    /// when HEAD already holds them (they're attributed to it) or there is
    /// no repository.
    fn commit(&self, py: Python<'_>, message: String) -> PyResult<Option<String>> {
        let Some(repo) = self.sync.repository() else {
            return Ok(None);
        };
        let committed = run(py, async move {
            repo.commit_repository(&message, CommitOptions::default())
                .await
        })?;
        Ok(committed.commit.map(|c| c.to_string()))
    }
}

/// Adds `GitSyncDB` and its exceptions to the module.
pub(crate) fn add(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add_class::<GitSyncDB>()?;
    m.add("GitSyncError", py.get_type::<GitSyncError>())?;
    m.add("GitSyncConflict", py.get_type::<GitSyncConflict>())?;
    m.add("CommitMissing", py.get_type::<CommitMissing>())?;
    Ok(())
}
