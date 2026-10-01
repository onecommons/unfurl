// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! The tables' row types, and how a row is read back.
//!
//! [`crate::db::store::Store`] reads and writes these inside a
//! transaction; the table modules beside it turn them into the
//! [`crate::model`] types the API returns.

use crate::error::{Error, Result};
use crate::model::ConflictState;

/// One version of one record (§3.4).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct RecordRow {
    pub(crate) id: i64,
    pub(crate) key_id: i64,
    pub(crate) segment_id: i64,
    pub(crate) file_path: String,
    pub(crate) path: String,
    pub(crate) key: String,
    pub(crate) commit_id: Option<String>,
    pub(crate) json: serde_json::Value,
    pub(crate) deleted: bool,
    pub(crate) version: i64,
    pub(crate) base_commit_id: Option<String>,
    /// The content of the version `base_commit_id` names.
    pub(crate) base_json: Option<serde_json::Value>,
    pub(crate) settled: Vec<i64>,
    pub(crate) conflict: Option<ConflictState>,
}

impl RecordRow {
    pub(crate) fn at(&self) -> At<'_> {
        At {
            file_path: &self.file_path,
            path: &self.path,
            key: &self.key,
        }
    }

    /// A client's edit, rather than a value taken in from the file.
    pub(crate) fn is_edit(&self) -> bool {
        self.commit_id.is_none()
    }
}

/// A record's place: file, section and key.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) struct At<'a> {
    pub(crate) file_path: &'a str,
    pub(crate) path: &'a str,
    pub(crate) key: &'a str,
}

/// An owned [`At`].
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) struct Place {
    pub(crate) file_path: String,
    pub(crate) path: String,
    pub(crate) key: String,
}

impl Place {
    pub(crate) fn new(file_path: &str, path: &str, key: &str) -> Self {
        Place {
            file_path: file_path.to_string(),
            path: path.to_string(),
            key: key.to_string(),
        }
    }

    pub(crate) fn as_at(&self) -> At<'_> {
        At {
            file_path: &self.file_path,
            path: &self.path,
            key: &self.key,
        }
    }
}

impl From<&RecordRow> for Place {
    fn from(r: &RecordRow) -> Self {
        Place::new(&r.file_path, &r.path, &r.key)
    }
}

/// A row to insert. `key_id: None` starts a new record, whose id is the
/// row's own.
pub(crate) struct NewRecordRow<'a> {
    pub(crate) at: At<'a>,
    pub(crate) key_id: Option<i64>,
    pub(crate) commit_id: Option<&'a str>,
    pub(crate) json: &'a serde_json::Value,
    pub(crate) deleted: bool,
    pub(crate) version: i64,
    pub(crate) base_commit_id: Option<&'a str>,
    pub(crate) base_json: Option<&'a serde_json::Value>,
    pub(crate) settled: &'a [i64],
    pub(crate) conflict: Option<ConflictState>,
}

impl<'a> NewRecordRow<'a> {
    /// A row with none of a draft edit's fields: no base, nothing
    /// settled, no conflict.
    pub(crate) fn new(
        at: At<'a>,
        key_id: Option<i64>,
        commit_id: Option<&'a str>,
        json: &'a serde_json::Value,
        deleted: bool,
        version: i64,
    ) -> Self {
        NewRecordRow {
            at,
            key_id,
            commit_id,
            json,
            deleted,
            version,
            base_commit_id: None,
            base_json: None,
            settled: &[],
            conflict: None,
        }
    }
}

impl<'a> From<&'a RecordRow> for NewRecordRow<'a> {
    fn from(r: &'a RecordRow) -> Self {
        NewRecordRow {
            at: r.at(),
            key_id: Some(r.key_id),
            commit_id: r.commit_id.as_deref(),
            json: &r.json,
            deleted: r.deleted,
            version: r.version,
            base_commit_id: r.base_commit_id.as_deref(),
            base_json: r.base_json.as_ref(),
            settled: &r.settled,
            conflict: r.conflict,
        }
    }
}

/// A `superseded` row, which the design calls an entry (§3.3), with the
/// place of the record row it names: the row it's on, and the edit that
/// made it.
#[derive(Debug, Clone)]
pub(crate) struct Superseded {
    pub(crate) row: i64,
    pub(crate) file_path: String,
    pub(crate) path: String,
    pub(crate) key: String,
    /// The row's own record, and whether it's a tombstone.
    pub(crate) row_key_id: i64,
    pub(crate) deleted: bool,
    pub(crate) key_id: i64,
}

impl Superseded {
    pub(crate) fn at(&self) -> At<'_> {
        At {
            file_path: &self.file_path,
            path: &self.path,
            key: &self.key,
        }
    }
}

pub(crate) const ROW_COLUMNS: &str = "r.id, r.key_id, r.segment_id, r.file_path, r.path, r.key, \
     r.commit_id, json(r.json) AS json, r.deleted, r.version, r.base_commit_id, \
     json(r.settled) AS settled, r.conflict, json(r.base_json) AS base_json";

/// A `record` row as [`ROW_COLUMNS`] selects it, JSON still as text.
#[derive(sqlx::FromRow)]
pub(crate) struct RecordColumns {
    id: i64,
    key_id: i64,
    segment_id: i64,
    file_path: String,
    path: String,
    key: String,
    commit_id: Option<String>,
    json: String,
    deleted: bool,
    version: i64,
    base_commit_id: Option<String>,
    settled: Option<String>,
    conflict: Option<String>,
    base_json: Option<String>,
}

impl TryFrom<RecordColumns> for RecordRow {
    type Error = Error;

    fn try_from(c: RecordColumns) -> Result<Self> {
        let json_err = |source| Error::Json {
            path: c.path.clone(),
            source,
        };
        Ok(RecordRow {
            json: serde_json::from_str(&c.json).map_err(json_err)?,
            settled: match c.settled.as_deref() {
                Some(s) => serde_json::from_str(s).map_err(json_err)?,
                None => Vec::new(),
            },
            base_json: c
                .base_json
                .as_deref()
                .map(serde_json::from_str)
                .transpose()
                .map_err(json_err)?,
            conflict: ConflictState::from_column(c.conflict.as_deref()),
            id: c.id,
            key_id: c.key_id,
            segment_id: c.segment_id,
            file_path: c.file_path,
            path: c.path,
            key: c.key,
            commit_id: c.commit_id,
            deleted: c.deleted,
            version: c.version,
            base_commit_id: c.base_commit_id,
        })
    }
}

impl ConflictState {
    /// The `record.conflict` column value.
    pub(crate) fn to_column(self) -> &'static str {
        match self {
            Self::Conflict => "conflict",
            Self::Resolved => "resolved",
        }
    }

    /// Read the column back. An unrecognized non-NULL value reads as
    /// [`Self::Conflict`]: whatever wrote it meant "the two sides
    /// disagree", and the unresolved reading is the safe one.
    pub(crate) fn from_column(value: Option<&str>) -> Option<Self> {
        match value {
            None => None,
            Some("resolved") => Some(Self::Resolved),
            Some(_) => Some(Self::Conflict),
        }
    }
}

/// A file row to record. `Some` on an optional field sets it; the
/// working-tree side (`commit_id`, `source_oid`) and the committed side
/// (`committed_oid`) are set by different passes of a scan.
pub(crate) struct FileRow<'a> {
    pub(crate) path: &'a str,
    pub(crate) format: &'a str,
    pub(crate) commit_id: Option<Option<&'a str>>,
    pub(crate) source_oid: Option<Option<&'a str>>,
    pub(crate) committed_oid: Option<Option<&'a str>>,
}

/// A worktree's open head segment and its draft.
#[derive(Debug, Clone, Copy)]
pub(crate) struct WorktreeSegs {
    pub(crate) head: i64,
    pub(crate) draft: i64,
}

/// A segment, and the commit whose state it ends at.
#[derive(Debug, Clone, sqlx::FromRow)]
pub(crate) struct SegmentEnd {
    pub(crate) id: i64,
    /// `None` for a new worktree's head, until its first scan.
    pub(crate) head_commit: Option<String>,
}
