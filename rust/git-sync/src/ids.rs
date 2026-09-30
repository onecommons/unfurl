// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Records at past commits, and the identity commit rollups give them
//! (docs/branch-segments.md §3.5, §4.7, §4.9).

use std::collections::{BTreeMap, BTreeSet, HashMap};

use crate::document::{extract_ext, Syntax};
use crate::error::Result;
use crate::format::FormatRegistry;
use crate::git;
use crate::model::{CommitRollup, SyncOutcome};
use crate::rollup::parse_commit_rollup;
use crate::scan::HeadFile;
use crate::sync::parse_and_detect;

/// A file's records: `(path, key)` → value.
pub(crate) type Records = BTreeMap<(String, String), serde_json::Value>;

/// A blob as a scan takes it in: its records, and what it skips.
struct Parsed {
    records: Records,
    /// A scan skips the file whole: unparseable, or unreadable as its
    /// format.
    whole: bool,
    /// Records and sections validation rejected.
    rejected: BTreeSet<(String, String)>,
    skip_paths: BTreeSet<String>,
}

/// A repository's records at past commits, and the `key_id`s the rollups
/// this database wrote give them. Each commit's tree and message, and
/// each file blob, is read once.
pub(crate) struct History<'s> {
    formats: &'s FormatRegistry,
    repo: gix::Repository,
    database: String,
    trees: HashMap<String, HashMap<String, gix::ObjectId>>,
    blobs: HashMap<gix::ObjectId, Parsed>,
    rollups: HashMap<String, Option<CommitRollup>>,
}

impl<'s> History<'s> {
    pub(crate) fn new(
        formats: &'s FormatRegistry,
        repo: gix::Repository,
        database: String,
    ) -> Self {
        History {
            formats,
            repo,
            database,
            trees: HashMap::new(),
            blobs: HashMap::new(),
            rollups: HashMap::new(),
        }
    }

    /// The id of the record at `file`, `path`, `key` at `commit`: named by
    /// the rollup of the commit that set its value, found walking back
    /// through parents with the same value, first parent first, to the
    /// first such commit this database made.
    pub(crate) fn id(
        &mut self,
        commit: &str,
        file: &str,
        path: &str,
        key: &str,
    ) -> Result<Option<i64>> {
        let v = self.value(commit, file, path, key)?;
        let mut seen = BTreeSet::from([commit.to_string()]);
        let mut todo = vec![commit.to_string()];
        while let Some(at) = todo.pop() {
            let mut same = Vec::new();
            for p in git::commit_parents(&self.repo, &at)? {
                if self.value(&p, file, path, key)? == v {
                    same.push(p);
                }
            }
            if same.is_empty() {
                if let Some(id) = self.named(&at, file, path, key)? {
                    return Ok(Some(id));
                }
            }
            todo.extend(same.into_iter().rev().filter(|p| seen.insert(p.clone())));
        }
        Ok(None)
    }

    /// The id `commit`'s rollup names for the record, when the rollup is
    /// this database's.
    fn named(&mut self, commit: &str, file: &str, path: &str, key: &str) -> Result<Option<i64>> {
        if !self.rollups.contains_key(commit) {
            // a damaged rollup names nothing, like a commit without one
            let rollup = git::commit_message(&self.repo, commit)
                .and_then(|m| parse_commit_rollup(&m).ok().flatten())
                .filter(|r| r.database.as_deref() == Some(self.database.as_str()));
            self.rollups.insert(commit.to_string(), rollup);
        }
        let Some(rollup) = &self.rollups[commit] else {
            return Ok(None);
        };
        Ok(rollup
            .txns
            .iter()
            .flat_map(|t| &t.records)
            .chain(&rollup.records)
            .find(|r| r.file_path.as_deref() == Some(file) && r.path == path && r.key == key)
            .and_then(|r| r.key_id))
    }

    /// Whether the record exists at every commit on `to`'s first-parent
    /// history back to `from`, both included; `false` if `from` isn't on it.
    pub(crate) fn continuous(
        &mut self,
        from: &str,
        to: &str,
        file: &str,
        path: &str,
        key: &str,
    ) -> Result<bool> {
        let mut at = to.to_string();
        loop {
            if self.value(&at, file, path, key)?.is_none() {
                return Ok(false);
            }
            if at == from {
                return Ok(true);
            }
            match git::commit_parents(&self.repo, &at)?.into_iter().next() {
                Some(p) => at = p,
                None => return Ok(false),
            }
        }
    }

    pub(crate) fn repo(&self) -> &gix::Repository {
        &self.repo
    }

    /// `file`'s records at `commit`: `(path, key)` → value.
    pub(crate) fn records(&mut self, commit: &str, file: &str) -> Result<Records> {
        Ok(match self.load(commit, file)? {
            Some(blob) => self.blobs[&blob].records.clone(),
            None => Records::new(),
        })
    }

    /// The record's value at `commit`, `None` where it has none.
    pub(crate) fn value(
        &mut self,
        commit: &str,
        file: &str,
        path: &str,
        key: &str,
    ) -> Result<Option<serde_json::Value>> {
        Ok(match self.load(commit, file)? {
            Some(blob) => self.blobs[&blob]
                .records
                .get(&(path.to_string(), key.to_string()))
                .cloned(),
            None => None,
        })
    }

    /// `file`'s blob at `commit`, parsed into [`Self::blobs`]; `None` where
    /// the commit has no such file.
    fn load(&mut self, commit: &str, file: &str) -> Result<Option<gix::ObjectId>> {
        if !self.trees.contains_key(commit) {
            let tree = git::tree_blobs(&self.repo, commit)?;
            self.trees.insert(commit.to_string(), tree);
        }
        let Some(&blob) = self.trees[commit].get(file) else {
            return Ok(None);
        };
        if !self.blobs.contains_key(&blob) {
            let records = self.parse(file, blob)?;
            self.blobs.insert(blob, records);
        }
        Ok(Some(blob))
    }

    /// Whether a scan of `file` at `commit` leaves the record at
    /// `(path, key)` as it is: validation rejected it, its section, or
    /// the file.
    pub(crate) fn skips(
        &mut self,
        commit: &str,
        file: &str,
        path: &str,
        key: &str,
    ) -> Result<bool> {
        Ok(match self.load(commit, file)? {
            Some(blob) => {
                let p = &self.blobs[&blob];
                p.whole
                    || p.skip_paths.contains(path)
                    || p.rejected.contains(&(path.to_string(), key.to_string()))
            }
            None => false,
        })
    }

    fn parse(&self, file: &str, blob: gix::ObjectId) -> Result<Parsed> {
        let none = |whole| Parsed {
            records: Records::new(),
            whole,
            rejected: BTreeSet::new(),
            skip_paths: BTreeSet::new(),
        };
        let Some(syntax) = Syntax::for_extension(&extract_ext(file)) else {
            return Ok(none(false));
        };
        let bytes = git::read_blob(&self.repo, &blob.to_string())?;
        let mut ignored = SyncOutcome::default();
        // unparseable, or unreadable as its format: a scan leaves its rows
        let Ok(Some(doc)) = parse_and_detect(self.formats, file, syntax, &bytes, &mut ignored)
        else {
            return Ok(none(true));
        };
        let head = HeadFile::new(file.to_string(), None, Some(&doc), "");
        Ok(Parsed {
            records: head
                .records
                .into_iter()
                .map(|(path, key, value)| ((path, key), value))
                .collect(),
            whole: false,
            rejected: head.rejected,
            skip_paths: head.skip_paths,
        })
    }
}
