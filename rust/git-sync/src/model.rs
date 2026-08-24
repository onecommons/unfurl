// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Plain-old-data structs returned from the database.
//!
//! Each struct mirrors one row of its corresponding SQL table, plus
//! [`WorkingDir`] (a derived view over the gix repo) and
//! [`UpdateStats`] (a return value).

use crate::error::{Error, Result};
use serde::{Deserialize, Serialize};

/// One row of the `worktree` table — a `(origin, branch)` pair the
/// crate has indexed.
///
/// `commit_id` is the most recent HEAD oid observed on the branch; it
/// advances whenever [`crate::SyncedRepo::update_from_working_dir`] or
/// [`crate::SyncedRepo::commit_repository`] runs.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Worktree {
    /// Auto-assigned primary key.
    pub id: i64,
    /// Remote URL with scheme stripped (or the working-tree path when
    /// no remote is configured).
    pub origin: String,
    /// Short branch name, e.g. `main`.
    pub branch: String,
    /// Last HEAD oid the crate has observed for this `(origin, branch)`.
    pub commit_id: Option<String>,
    /// Working-tree-relative path of the file new records go to when a
    /// CRUD call passes `file_path = None`. Set on the first
    /// [`crate::SyncedRepo::update_from_working_dir`] run; never
    /// overwritten afterwards (operators can pin it manually).
    pub default_file_path: Option<String>,
}

/// One row of the `file` table — a tracked file within a worktree.
///
/// `format` is the [`crate::DataFormat::name`] that classified the
/// file's contents on the most recent
/// [`crate::SyncedRepo::update_from_working_dir`]; `commit_id` is the
/// last-known commit that touched this path.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct File {
    /// Foreign key into [`Worktree`].
    pub worktree_id: i64,
    /// Working-tree-relative path of the file.
    pub path: String,
    /// Name of the [`crate::DataFormat`] that classified this file.
    pub format: String,
    /// Last commit oid known to have touched this path.
    pub commit_id: Option<String>,
}

/// One row of the `record` table — a single extracted JSON value.
///
/// A search over a record's JSON contents: the value at `tokens` has to match
/// `value`.
///
/// A scalar `value` matches when the value at the path *is* it or is an array
/// containing it, so an array and a scalar are searched the same way and no
/// knowledge of the record's shape is needed. An array `value` instead means
/// exact equality -- same elements, same order.
///
/// To match a member of an object, put the member's key in the path. Object
/// literals are rejected: postgres compares them structurally while sqlite
/// compares the rendered text, so the two backends would disagree on key
/// order. Wildcards in the path aren't supported.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QueryOp {
    /// The value at the path is the query value (or contains it, for an
    /// array; or equals it exactly, for an array query value).
    Equals,
    /// The value at the path is a string starting with the query value -- or
    /// an array with an element that does. Non-strings never match.
    StartsWith,
    /// The path resolves at all. A `null` or an empty array/object counts as
    /// existing; only a missing path doesn't. The query value is unused.
    Exists,
}

/// The filters of a record search, as one value.
///
/// Every field is optional and `AND`ed with the rest, so
/// [`Default::default()`] searches everything and a caller names only what
/// it wants to narrow:
///
/// ```
/// use unfurl_git_sync::RecordQuery;
/// let q = RecordQuery {
///     path: Some("/artifacts".into()),
///     limit: Some(50),
///     ..Default::default()
/// };
/// ```
///
/// Grouping them keeps [`crate::SyncedRepo::find_records`] and the SQL
/// builders behind it to a readable argument list, and means adding a
/// filter doesn't ripple through every call site.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct RecordQuery {
    /// Restrict to one cloudmap file; `None` spans every file in the worktree.
    pub file_path: Option<String>,
    /// Restrict to one section, e.g. `/artifacts`.
    pub path: Option<String>,
    /// Restrict to one record key.
    pub key: Option<String>,
    /// Also match a record one of whose [`Alias`] rows carries `key`.
    /// A no-op without `key`.
    pub alias: bool,
    /// Only records whose `version` is greater than this.
    pub since_version: Option<i64>,
    /// Only records declaring one of these names as a key of their `type`
    /// object. Matching is by exact name — expand subtypes first.
    pub type_names: Option<Vec<String>>,
    /// Only records whose JSON satisfies *every* one of these
    /// predicates (ANDed; empty means no content filter). Each
    /// predicate is applied independently — two filters may be
    /// satisfied by different elements of the same array.
    pub json_queries: Vec<JsonQuery>,
    /// Exclusive `(path, key)` lower bound on the byte-wise result order:
    /// the paging cursor. Being a value rather than a row reference, it
    /// stays usable after the record it names is deleted.
    pub after: Option<(String, String)>,
    /// Cap on how many records come back.
    pub limit: Option<i64>,
    /// Also return tombstones — records deleted since they were written.
    ///
    /// Off by default, because a live view of a section has no use for
    /// them. Pair it with [`Self::since_version`]: a caller catching up
    /// from a watermark needs to learn that a record *went away*, which
    /// is otherwise unobservable — its row simply stops being returned.
    /// Check [`Record::deleted`] to tell a tombstone from a live record.
    pub include_deleted: bool,
}

impl RecordQuery {
    /// Whether the alias OR-clause applies: it is a no-op without a key.
    pub(crate) fn alias_active(&self) -> bool {
        self.alias && self.key.is_some()
    }

    /// The type filter to apply, if any.
    ///
    /// An empty name list matches nothing under `?|` / `IN ()`, so it is
    /// reported as "no filter" instead — callers building a list from a
    /// subtype expansion don't have to special-case an empty result.
    pub(crate) fn effective_type_names(&self) -> Option<&[String]> {
        self.type_names.as_deref().filter(|t| !t.is_empty())
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct JsonQuery {
    /// Unescaped JSON-Pointer reference tokens, e.g.
    /// `["metadata", "discovery", "sources"]`.
    pub tokens: Vec<String>,
    /// The value to compare against.
    pub value: serde_json::Value,
    /// How to compare it.
    pub op: QueryOp,
}

/// Reject reference tokens the SQL path syntax can't express: empties,
/// and tokens containing a quote or backslash, which `$."…"` paths have
/// no escape for. Shared by [`JsonQuery::new`] and [`FacetPath::new`].
fn validate_path_tokens(tokens: &[String]) -> Result<()> {
    if tokens.is_empty() || tokens.iter().any(|t| t.is_empty()) {
        return Err(Error::Other(
            "json query needs a path of non-empty segments".to_string(),
        ));
    }
    if let Some(bad) = tokens.iter().find(|t| t.contains('"') || t.contains('\\')) {
        return Err(Error::Other(format!(
            "json query path segment {bad:?} can't contain a quote or backslash"
        )));
    }
    Ok(())
}

/// Reference tokens as SQL/JSON path syntax:
/// `$."metadata"."discovery"."sources"`. Every token is quoted so that
/// tokens containing "." or " " resolve; see [`validate_path_tokens`].
fn sql_path_from(tokens: &[String]) -> String {
    let mut path = String::from("$");
    for token in tokens {
        path.push_str(&format!(".\"{token}\""));
    }
    path
}

impl JsonQuery {
    /// The path as SQL/JSON path syntax: `$."metadata"."discovery"."sources"`.
    ///
    /// Every token is quoted so that tokens containing "." or " " resolve, and
    /// tokens are rejected up front (see [`JsonQuery::new`]) if they contain a
    /// quote or a backslash, which the path syntax has no escape for.
    pub fn sql_path(&self) -> String {
        sql_path_from(&self.tokens)
    }

    /// The postgres SQL/JSON path for this query, with the value written into
    /// it: `$."metadata"."version"[*] ? (@ == "1.0")`.
    ///
    /// `@?` takes no variables, so the value has to be part of the path. It is
    /// serialized as JSON, which is also jsonpath literal syntax, so strings
    /// arrive quoted and escaped and `true` / `null` / numbers stay bare.
    /// `[*]` is what makes an array and a scalar behave the same: in `lax`
    /// mode (the default) it unwraps an array and wraps a scalar.
    pub fn jsonpath(&self) -> String {
        let literal = serde_json::to_string(&self.value).unwrap_or_else(|_| "null".to_string());
        match self.op {
            // A bare path is an existence test. Postgres can't serve it from a
            // GIN index (the extractor has no value to look up), so this plans
            // as a sequential scan -- see the 20260101000003 migration.
            QueryOp::Exists => self.sql_path(),
            QueryOp::Equals => format!("{}[*] ? (@ == {literal})", self.sql_path()),
            // `starts with` only applies to strings, so a number or a boolean
            // at the path never matches -- which is what the sqlite side gets
            // from its `jq.type = 'text'` guard.
            QueryOp::StartsWith => {
                format!("{}[*] ? (@ starts with {literal})", self.sql_path())
            }
        }
    }

    /// The sqlite `LIKE` pattern for a [`QueryOp::StartsWith`] query, with the
    /// pattern metacharacters in the prefix escaped (the clause pairs this
    /// with `ESCAPE '\\'`). Unlike jsonpath's `starts with`, `LIKE` would
    /// otherwise read a "%" or "_" in the prefix as a wildcard.
    pub fn like_pattern(&self) -> String {
        let prefix = self.value.as_str().unwrap_or_default();
        let escaped = prefix
            .replace('\\', "\\\\")
            .replace('%', "\\%")
            .replace('_', "\\_");
        format!("{escaped}%")
    }

    /// Whether this query is an exact match on an array rather than the
    /// "equals or contains" test a scalar gets.
    pub fn is_exact(&self) -> bool {
        self.op == QueryOp::Equals && self.value.is_array()
    }

    /// The value nested under the path, e.g. `{"metadata":{"topics":["a"]}}`.
    ///
    /// Used as postgres' `@>` pre-filter for an exact-array query: containment
    /// is a superset of equality, so it never drops a match, and unlike the
    /// equality test itself it can be served by the GIN index.
    pub fn containment(&self) -> serde_json::Value {
        let mut nested = self.value.clone();
        for token in self.tokens.iter().rev() {
            nested = serde_json::json!({ token.as_str(): nested });
        }
        nested
    }

    /// Build a query, rejecting tokens the path syntax can't express.
    pub fn new(tokens: Vec<String>, value: serde_json::Value) -> Result<Self> {
        validate_path_tokens(&tokens)?;
        if value.is_object() {
            return Err(Error::Other(
                "json query value can't be an object: address a member by putting \
                 its key in the path"
                    .to_string(),
            ));
        }
        Ok(Self {
            tokens,
            value,
            op: QueryOp::Equals,
        })
    }

    /// An existence query: the path resolves, whatever it holds.
    pub fn exists(tokens: Vec<String>) -> Result<Self> {
        let mut query = Self::new(tokens, serde_json::Value::Null)?;
        query.op = QueryOp::Exists;
        Ok(query)
    }

    /// A prefix query: the value at the path is a string starting with
    /// `prefix`, or an array with an element that does.
    pub fn starts_with(tokens: Vec<String>, prefix: String) -> Result<Self> {
        let mut query = Self::new(tokens, serde_json::Value::String(prefix))?;
        query.op = QueryOp::StartsWith;
        Ok(query)
    }
}

/// One extraction path of a facet aggregation (see
/// [`crate::SyncedRepo::facet_records`]).
///
/// The value at the path is unwrapped one level, the same way the
/// `json_query` filter reads a path: an array contributes each element,
/// an object contributes each *key*, a scalar contributes itself, and a
/// record without the path contributes nothing.
#[derive(Debug, Clone, PartialEq)]
pub struct FacetPath {
    /// Unescaped JSON-Pointer reference tokens, e.g. `["metadata", "topics"]`.
    pub tokens: Vec<String>,
    /// Remap each extracted value through [`FacetSpec::rollup_pairs`]:
    /// a value with pairs counts under every bucket its pairs name
    /// (include a self-pair to keep it counting as itself); a value
    /// with no pairs counts as itself.
    pub rollup: bool,
}

impl FacetPath {
    /// Build a path, rejecting tokens the SQL path syntax can't express
    /// (the same rule as [`JsonQuery::new`]).
    pub fn new(tokens: Vec<String>, rollup: bool) -> Result<Self> {
        validate_path_tokens(&tokens)?;
        Ok(Self { tokens, rollup })
    }

    /// The path as SQL/JSON path syntax (see [`JsonQuery::sql_path`]).
    pub(crate) fn sql_path(&self) -> String {
        sql_path_from(&self.tokens)
    }
}

/// The dimensions of a facet aggregation: one grouping path, any number
/// of facet columns -- each one or more member paths, a multi-member
/// column counting the per-record combinations of its members' values
/// -- and the rollup mapping applied where a path opts in.
#[derive(Debug, Clone, PartialEq)]
pub struct FacetSpec {
    /// The path records are grouped by.
    pub group: FacetPath,
    /// The facet columns, each a list of member paths.
    pub columns: Vec<Vec<FacetPath>>,
    /// `(member, bucket)` pairs consulted by paths with
    /// [`FacetPath::rollup`] set: an extracted value equal to `member`
    /// counts under `bucket` instead of itself, once per pair naming
    /// it. Callers wanting a member to also count as itself must
    /// include the self-pair. The caller supplies the pairs as data --
    /// e.g. a type-inheritance closure -- so the aggregation itself
    /// stays format-agnostic.
    pub rollup_pairs: Vec<(String, String)>,
}

/// Render a JSON value as canonical text: minified, object keys sorted
/// byte-wise at every depth. Two spellings of the same value -- e.g.
/// `{"os":…,"architecture":…}` and its reversal -- render identically,
/// which is what lets facet callers merge sqlite's stored-key-order
/// buckets and what makes response keys parse back to the same JSON on
/// every backend. (This crate's `serde_json` has `preserve_order` on,
/// so plain `to_string` would keep whatever order the value carried.)
pub fn canonical_json_text(value: &serde_json::Value) -> String {
    fn write(value: &serde_json::Value, out: &mut String) {
        match value {
            serde_json::Value::Object(map) => {
                let mut keys: Vec<&String> = map.keys().collect();
                keys.sort();
                out.push('{');
                for (i, key) in keys.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    out.push_str(&serde_json::Value::String((*key).clone()).to_string());
                    out.push(':');
                    write(&map[key.as_str()], out);
                }
                out.push('}');
            }
            serde_json::Value::Array(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write(item, out);
                }
                out.push(']');
            }
            scalar => out.push_str(&scalar.to_string()),
        }
    }
    let mut out = String::new();
    write(value, &mut out);
    out
}

/// Render one facet value as a response key: strings stay bare, any
/// other value becomes its [`canonical_json_text`] -- so structured
/// keys parse back to JSON and every server implementation produces
/// the same spelling.
pub fn canonical_facet_key(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::String(s) => s.clone(),
        other => canonical_json_text(other),
    }
}

/// One row of a facet column: a (group value, member values)
/// combination and its distinct-record count.
#[derive(Debug, Clone, PartialEq)]
pub struct FacetColumnRow {
    /// The (possibly rolled-up) group value this combination fell under.
    pub group: serde_json::Value,
    /// One extracted value per member path, in column order.
    pub members: Vec<serde_json::Value>,
    /// Distinct records contributing this combination.
    pub count: i64,
}

/// The raw rows of a facet aggregation, values as extracted -- no
/// canonicalization or key rendering; that is the caller's business.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct FacetRows {
    /// Records matching the query's filters, whether or not they
    /// produced a group value.
    pub total: i64,
    /// Distinct-record count per group value.
    pub groups: Vec<(serde_json::Value, i64)>,
    /// Per facet column, in [`FacetSpec::columns`] order.
    pub columns: Vec<Vec<FacetColumnRow>>,
}

/// Records sit at `obj[path][key]` inside their owning file, where
/// `path` is the JSON-pointer to the parent map (e.g. `/repositories`)
/// and `key` is the literal map key. `commit_id == None` indicates an
/// in-flight edit that hasn't been committed yet.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Record {
    /// Auto-assigned primary key.
    pub id: i64,
    /// Foreign key into [`Worktree`].
    pub worktree_id: i64,
    /// Working-tree-relative path of the file this record came from.
    pub file_path: String,
    /// JSON-pointer to the parent map this record lives under (e.g.
    /// `/repositories`).
    pub path: String,
    /// Unescaped key under [`Record::path`]; stored verbatim — no
    /// JSON-pointer escaping.
    pub key: String,
    /// Last commit oid that committed this record's value, or `None`
    /// when the record is in-flight (uncommitted).
    pub commit_id: Option<String>,
    /// The record's JSON payload.
    pub json: serde_json::Value,
    /// Tombstone flag.
    ///
    /// A row with `deleted == true` AND `commit_id == None` is an
    /// in-flight delete waiting for the next
    /// [`crate::SyncedRepo::commit_repository`] to purge it.
    /// [`crate::SyncedRepo::get_record`] and
    /// [`crate::SyncedRepo::find_records`] hide tombstones; only
    /// [`crate::SyncedRepo::get_record_by_id`] returns them.
    pub deleted: bool,
    /// Monotonic per-worktree version. Bumped on every CRUD write and
    /// preserved across commit roll-forward, so it doubles as both the
    /// optimistic-concurrency token (see
    /// [`crate::CommitRef::Pending`]) and a cursor for
    /// [`crate::SyncedRepo::list_changes`].
    pub version: i64,
}

/// Attribution for a batch write: who asked for it and why.
///
/// Passed to [`crate::SyncedRepo::apply_batch`] to opt that batch into
/// the `txn` audit table. Both fields are optional — a caller that knows
/// neither can still pass `TxnMeta::default()` to get a row recording
/// just the version range and timestamp.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct TxnMeta {
    /// Free-form author string, e.g. `Name <email>`. Stored verbatim.
    pub author: Option<String>,
    /// The caller's description of the batch. Reproduced in the body of
    /// the git commit message that carries these writes.
    pub message: Option<String>,
}

/// One row of the `txn` table — the audit trail of a batch write.
///
/// Written by [`crate::SyncedRepo::apply_batch`] when given a
/// [`TxnMeta`], read back by [`crate::SyncedRepo::list_transactions`],
/// and reported in the commit-message body by
/// [`crate::SyncedRepo::commit_repository`].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Txn {
    /// Auto-assigned primary key.
    pub id: i64,
    /// Foreign key into [`Worktree`].
    pub worktree_id: i64,
    /// Lowest [`Record::version`] the batch stamped.
    pub first_version: i64,
    /// Highest [`Record::version`] the batch stamped. Equal to
    /// `first_version` for a one-op batch.
    pub last_version: i64,
    /// Author as supplied in [`TxnMeta::author`].
    pub author: Option<String>,
    /// Message as supplied in [`TxnMeta::message`].
    pub message: Option<String>,
    /// RFC 3339 timestamp with the local offset, taken when the batch
    /// was applied.
    pub created_at: String,
    /// Commit oid that carried this batch's writes to git, or `None`
    /// while the batch is still outstanding.
    pub commit_id: Option<String>,
}

/// One record still carrying a version a batch drew.
///
/// Worked out at commit time by matching `record.version` against the
/// batch's range, so it lists what the batch contributed to *this
/// commit* -- a write that a later batch in the same commit overwrote
/// is not here, and is reported as a shortfall instead (see
/// [`RollupTxn::unaccounted`]).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TxnRecord {
    /// Parent JSON-pointer, e.g. `/repositories`.
    pub path: String,
    /// Record key within that section.
    pub key: String,
    /// The version the batch stamped on the row.
    pub version: i64,
    /// Whether the row is a tombstone -- the op was a delete.
    pub deleted: bool,
}

/// A commit message's rollup -- built by
/// [`crate::SyncedRepo::commit_repository`] on the way out and returned
/// by [`crate::parse_commit_rollup`] on the way back in.
///
/// The commit message itself is the machine-readable form; there is no
/// separate encoded copy. See
/// [`crate::SyncedRepo::commit_repository`] for the grammar.
#[derive(Debug, Clone, PartialEq)]
pub struct CommitRollup {
    /// [`Worktree::origin`] of the worktree that made these writes,
    /// from the `Git-Sync-Origin` trailer. `None` when that trailer is
    /// absent.
    ///
    /// With `branch` on each entry it identifies the version namespace
    /// the ranges belong to — not decoration: one database can host
    /// several worktrees, each with its own counter, and merging a
    /// branch brings its rollup commits into another branch's history,
    /// so a rebuild has to filter rather than assume every rollup it
    /// walks past is its own.
    pub origin: Option<String>,
    /// The `Git-Sync-Next-Version` trailer: the worktree's version
    /// counter as of this commit. Present on every git-sync commit,
    /// which is what makes the message recognisable as parseable.
    pub next_version: i64,
    /// The batches this commit carries, oldest version range first.
    /// Empty when the commit carried no batch writes.
    pub txns: Vec<RollupTxn>,
}

/// One batch within a [`CommitRollup`] — a [`Txn`] minus the columns
/// that are local to the database that wrote it (`id`, `worktree_id`)
/// or already implied by the commit carrying it (`commit_id`).
#[derive(Debug, Clone, PartialEq)]
pub struct RollupTxn {
    /// Lowest version the batch drew.
    pub first_version: i64,
    /// Highest version the batch drew. This is the *draw* range, so it
    /// can exceed what [`RollupTxn::records`] accounts for: an op that
    /// failed its optimistic-concurrency check after drawing leaves a
    /// number belonging to no record.
    pub last_version: i64,
    /// [`Worktree::branch`] the writes were made on.
    pub branch: String,
    /// RFC 3339 timestamp, verbatim from [`Txn::created_at`].
    pub created_at: String,
    /// Author as supplied in [`TxnMeta::author`].
    pub author: Option<String>,
    /// Message as supplied in [`TxnMeta::message`].
    pub message: Option<String>,
    /// The records still carrying a version from this batch, in version
    /// order.
    pub records: Vec<TxnRecord>,
}

impl RollupTxn {
    /// Versions this batch drew that no record in [`Self::records`]
    /// accounts for.
    ///
    /// Two things land here and they cannot be told apart after the
    /// fact: a write a later batch in the same commit overwrote, and an
    /// op that drew a version and then failed its optimistic-concurrency
    /// check. (Only a non-atomic batch can do the latter -- an atomic
    /// one rolls back whole and records no row at all.)
    ///
    /// It is reported rather than ignored because the alternative is a
    /// lie: an upsert replaces the entire record, so a client that read
    /// an earlier value and sent it back carries those edits into the
    /// surviving one. Listing only the last writer would credit them
    /// with work that is partly someone else's. This says "something of
    /// this batch is not visible here" without pretending to know what.
    pub fn unaccounted(&self) -> i64 {
        (self.last_version - self.first_version + 1) - self.records.len() as i64
    }
}

/// One row of the `alias` table — an alternate `(path, key)` lookup
/// pointing at a record.
///
/// Aliases let callers find a record by a synonym (e.g. a versioned
/// URL) via [`crate::SyncedRepo::find_records`] with `alias = true`.
/// They are populated by [`crate::DataFormat::find_alias`].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Alias {
    /// Foreign key into [`Record`].
    pub record_id: i64,
    /// Parent JSON-pointer of the alias.
    pub path: String,
    /// Unescaped alias key.
    pub key: String,
}

/// Snapshot of the gix working tree this [`crate::SyncedRepo`] is bound to.
///
/// Returned by [`crate::SyncedRepo::get_working_dir`]. `head_commit` is
/// `None` for an empty / unborn repository.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkingDir {
    /// Absolute filesystem path to the working directory.
    pub repo_path: std::path::PathBuf,
    /// Branch name (e.g. `main`), or `HEAD` for a detached HEAD.
    pub branch: String,
    /// Current HEAD commit oid as a hex string. `None` for an unborn
    /// or empty repository.
    pub head_commit: Option<String>,
}

/// Result of a CRUD write
/// ([`crate::SyncedRepo::create_record`] /
/// [`crate::SyncedRepo::update_record`] /
/// [`crate::SyncedRepo::upsert_record`] /
/// [`crate::SyncedRepo::delete_record`]).
///
/// `version` is the worktree-scoped monotonic counter stamped on this
/// write — pass it back as a [`crate::CommitRef::Pending`] token on the
/// next request to scope the optimistic-concurrency check.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct WriteOutcome {
    /// Primary key of the affected `record` row. For `delete_record`,
    /// this is the id of the row that was tombstoned.
    pub id: i64,
    /// Worktree-scoped monotonic version stamped on this write.
    pub version: i64,
}

/// Counters returned by [`crate::SyncedRepo::update_from_working_dir`].
///
/// `files_updated` ≤ `files_seen`; `records_upserted` and
/// `records_deleted` are totals across the whole sync pass.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct UpdateStats {
    /// Tracked files visited by the sync pass.
    pub files_seen: usize,
    /// Files whose records were re-extracted into the database.
    pub files_updated: usize,
    /// Total records inserted or refreshed in this pass.
    pub records_upserted: usize,
    /// Records hard-deleted because they disappeared from disk.
    pub records_deleted: usize,
}

/// One operation in a batch passed to
/// [`crate::SyncedRepo::apply_batch`].
#[derive(Debug, Clone)]
pub enum BatchOp {
    /// Insert-or-update — same semantics as
    /// [`crate::SyncedRepo::upsert_record`].
    Upsert {
        /// Effective file path; `None` falls back to the existing
        /// record's file then the worktree's `default_file_path`.
        file_path: Option<String>,
        /// Parent JSON-pointer the record sits under.
        path: String,
        /// Record key.
        key: String,
        /// Record payload.
        json: serde_json::Value,
        /// Optional OCC token gating the write.
        expected: Option<crate::CommitRef>,
    },
    /// Tombstone — same semantics as
    /// [`crate::SyncedRepo::delete_record`].
    Delete {
        /// Effective file path; `None` falls back to the existing
        /// record's file (deletes have no default-path fallback).
        file_path: Option<String>,
        /// Parent JSON-pointer.
        path: String,
        /// Record key.
        key: String,
        /// Optional OCC token gating the delete.
        expected: Option<crate::CommitRef>,
    },
}

impl BatchOp {
    /// Parent JSON-pointer this op targets.
    pub fn path(&self) -> &str {
        match self {
            BatchOp::Upsert { path, .. } | BatchOp::Delete { path, .. } => path,
        }
    }
    /// Record key this op targets.
    pub fn key(&self) -> &str {
        match self {
            BatchOp::Upsert { key, .. } | BatchOp::Delete { key, .. } => key,
        }
    }
}

/// A single [`BatchOp`] that landed successfully.
#[derive(Debug, Clone)]
pub struct Applied {
    /// Index of the op in the original batch.
    pub index: usize,
    /// Op's parent JSON-pointer.
    pub path: String,
    /// Op's record key.
    pub key: String,
    /// `(id, version)` stamped on the row.
    pub outcome: WriteOutcome,
    /// Whether the op was a [`BatchOp::Delete`]. A delete tombstones the
    /// row rather than removing it, so the two outcomes are otherwise
    /// indistinguishable to a caller reading this list.
    pub deleted: bool,
}

/// A single [`BatchOp`] that did not land.
///
/// In atomic mode, a populated `failed` always means the whole batch
/// was rolled back (so [`BatchOutcome::applied`] is empty). In
/// non-atomic mode, ``failed`` and ``applied`` may both be non-empty:
/// the failed records were skipped, the others committed.
#[derive(Debug)]
pub struct Failed {
    /// Index of the op in the original batch.
    pub index: usize,
    /// Op's parent JSON-pointer.
    pub path: String,
    /// Op's record key.
    pub key: String,
    /// The error raised when applying this op.
    pub error: crate::Error,
}

/// Result of [`crate::SyncedRepo::apply_batch`].
#[derive(Debug, Default)]
pub struct BatchOutcome {
    /// Records successfully applied (committed to the database).
    pub applied: Vec<Applied>,
    /// Records that were skipped.
    pub failed: Vec<Failed>,
    /// Largest `version` stamped during this batch, or ``None`` when
    /// nothing was applied.
    pub last_version: Option<i64>,
}
