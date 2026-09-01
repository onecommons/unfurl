// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Plain-old-data structs returned from the database.
//!
//! Each struct mirrors one row of its corresponding SQL table, plus
//! [`WorkingDir`] (a derived view over the gix repo) and
//! [`SyncOutcome`] (a return value).

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
    /// Stable identity of the repository, as
    /// [`crate::git::normalize_git_url_hard`] renders it — scheme,
    /// credentials, `.git` suffix and fragment removed, host lower-cased,
    /// e.g. `unfurl.cloud/onecommons/cloudmap`. Falls back to the
    /// working-tree path when no remote is configured, and to an empty
    /// string when there is neither (a bare repo without a remote).
    ///
    /// Normalized so that the same repository cloned over https by one
    /// user and ssh by another is one worktree rather than two, which
    /// matters because everything else — records, versions, the audit
    /// trail — hangs off this row. The same function backs python's
    /// repository identity, so the two agree.
    ///
    /// Note it is *one* remote's URL, not a chosen one:
    /// `gix::Repository::remote_names` returns a sorted set, so a
    /// repository with several remotes yields whichever name sorts first
    /// — not necessarily `origin`. A caller needing a specific identity
    /// has to supply it rather than let it be derived.
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
    /// Blob OID of the exact bytes this file's records were parsed from,
    /// or `None` for a file registered by a record write rather than a
    /// scan.
    ///
    /// Compared against the file's current contents before a write, to
    /// catch an edit the database never took in. `commit_id` cannot
    /// serve: it names the commit that last touched the path, so an
    /// uncommitted edit leaves it unchanged.
    pub source_oid: Option<String>,
    /// The database owes the worktree a removal of this file.
    ///
    /// Set by [`crate::SyncedRepo::delete_file`], which tombstones every
    /// record in the file alongside it. The next
    /// [`crate::SyncedRepo::save_changes`] removes the file from disk
    /// and the next [`crate::SyncedRepo::commit_repository`] stages that
    /// and drops this row. A tombstone rather than a hard delete
    /// because `record`'s foreign key onto `file` cascades: dropping the
    /// row would destroy the very tombstones that are the deletion.
    pub deleted: bool,
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
    /// Exclusive lower bound on the result order: the paging cursor.
    /// Being a value rather than a row reference, it stays usable after
    /// the record it names is deleted.
    pub after: Option<Cursor>,
    /// Cap on how many records come back.
    pub limit: Option<i64>,
    /// Never end a page part-way through a `(path, key)`: when `limit`
    /// would cut between two records sharing one, carry on to the end of
    /// that group even though the page then exceeds `limit`.
    ///
    /// Needed by any caller that collapses or merges records sharing a
    /// `(path, key)` — the cloudmap document model does, since a response
    /// is `{section: {key: value}}`. Such a caller has to see a whole
    /// group at once to decide what it becomes, and a group split across
    /// two pages is decided twice, from half the information each time.
    ///
    /// It also lets the cursor stay coarse. Resuming after `(path, key)`
    /// is only correct if the page ended on a group boundary; otherwise
    /// the remainder of a split group is never asked for again, and
    /// vanishes with no error.
    pub whole_groups: bool,
    /// Also return tombstones — records deleted since they were written.
    ///
    /// Off by default, because a live view of a section has no use for
    /// them. Pair it with [`Self::since_version`]: a caller catching up
    /// from a watermark needs to learn that a record *went away*, which
    /// is otherwise unobservable — its row simply stops being returned.
    /// Check [`Record::deleted`] to tell a tombstone from a live record.
    pub include_deleted: bool,
    /// Also return the file's side of conflicted records — see
    /// [`ConflictState`].
    ///
    /// Off by default, so an ordinary read sees one row per record: the
    /// database's own. Turn it on to see both sides at once, and read
    /// [`Record::conflict`] to tell them apart.
    pub include_conflicts: bool,
}

/// Where a page of [`crate::SyncedRepo::find_records`] resumes.
///
/// Results are ordered by `(path, key, file_path, worktree_id)` — a
/// total order, because that is the `record` table's unique index
/// rearranged. Keyset paging needs one: with a partial order, ties break
/// however the query planner feels and two runs can disagree about which
/// row a cursor sits after.
///
/// The cursor may be *coarser* than that order, and the right coarseness
/// depends on what the caller treats as one record:
///
/// - `path` and `key` alone resume after every row sharing them. This
///   suits the cloudmap document model, where a response is
///   `{section: {key: value}}` and two records sharing a key cannot both
///   be represented — so a finer cursor would resume mid-group and hand
///   back a record the client already has.
/// - Adding `file_path` distinguishes the two records two files may
///   legitimately hold at the same `(path, key)`.
/// - Adding `worktree_id` as well distinguishes one logical record seen
///   in two worktrees — an upstream and a per-user draft, say — which a
///   caller merging them wants to see both halves of.
///
/// Components apply as a prefix: `worktree_id` is ignored without
/// `file_path`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Cursor {
    /// Parent JSON-pointer of the last record on the previous page.
    pub path: String,
    /// Its record key.
    pub key: String,
    /// Its file, to resume within a `(path, key)` shared by two files.
    pub file_path: Option<String>,
    /// Its worktree, to resume within a `(path, key, file_path)` present
    /// in more than one.
    pub worktree_id: Option<i64>,
}

impl Cursor {
    /// Resume after `(path, key)` regardless of file or worktree.
    pub fn new(path: impl Into<String>, key: impl Into<String>) -> Self {
        Self {
            path: path.into(),
            key: key.into(),
            file_path: None,
            worktree_id: None,
        }
    }

    /// The ordering columns this cursor constrains, as a prefix of the
    /// `ORDER BY`. Shared by both dialects so the comparison and the
    /// bind list cannot disagree about how many values there are.
    pub(crate) fn columns(&self) -> &'static [&'static str] {
        match (&self.file_path, self.worktree_id) {
            (Some(_), Some(_)) => &["r.path", "r.key", "r.file_path", "r.worktree_id"],
            (Some(_), None) => &["r.path", "r.key", "r.file_path"],
            _ => &["r.path", "r.key"],
        }
    }
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
    /// Monotonic version, drawn from the counter shared by this
    /// worktree's family — the upstream it was forked from, together
    /// with that upstream's other forks and drafts. Family-wide rather
    /// than per-worktree because a read can merge a draft's edits over
    /// the upstream they came from, and this doubles as both an
    /// optimistic-concurrency token and a cursor: from independent
    /// counters, one number would name two different rows.
    ///
    /// Bumped on every CRUD write and
    /// preserved across commit roll-forward, so it doubles as both the
    /// optimistic-concurrency token (see
    /// [`crate::CommitRef::Pending`]) and a cursor for
    /// [`crate::SyncedRepo::list_changes`].
    pub version: i64,
    /// `None` on the database's own row — the one the CRUD API reads and
    /// writes. `Some(..)` marks the *file's* side of a record the two
    /// disagree about; see [`ConflictState`].
    ///
    /// Only returned by queries that opt in
    /// ([`RecordQuery::include_conflicts`],
    /// [`crate::SyncedRepo::list_conflicts`]).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub conflict: Option<ConflictState>,
}

/// What a conflict row holds — the file's view of a record the database
/// disagrees with, kept alongside the database's own row so that neither
/// side is overwritten before someone resolves.
///
/// Materialized by a scan or a write that finds the file diverging from
/// an in-flight edit, and carried in the `record.conflict` column. Until
/// it is resolved (see [`crate::SyncedRepo::resolve_conflict`]) the API
/// keeps serving the database's row while git and the working tree keep
/// serving this one — deliberately, so a divergence is never decided by
/// whichever side happened to be written last.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ConflictState {
    /// Unresolved: the file's value, as of the scan or write that last
    /// saw the divergence. A write leaves the record alone while this
    /// stands, and a commit stamps this row rather than its sibling —
    /// this is what the commit actually carries.
    Conflict,
    /// The client has declared the database's row the winner. The file's
    /// value is kept as the snapshot the resolution was made against:
    /// the next write applies the record only if the file still holds
    /// it, and flips back to [`Self::Conflict`] if it has moved again.
    Resolved,
}

impl ConflictState {
    /// The `record.conflict` column value.
    pub(crate) fn as_str(self) -> &'static str {
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

/// How [`crate::SyncedRepo::resolve_conflict`] settles a conflicted
/// record.
///
/// Every variant clears the divergence; they differ in what the record
/// becomes. `Ours` is the only one that defers — it records the decision
/// and leaves the file check to the next write, because the record's
/// value is not being restated and so nothing has re-validated it
/// against the file. The rest name a value (or its absence) explicitly,
/// which the next scan re-checks against the file anyway.
#[derive(Debug, Clone, PartialEq)]
pub enum Resolution {
    /// Keep the database's row as it stands. Marks the conflict
    /// [`ConflictState::Resolved`]; the next write applies the record
    /// if the file still holds the snapshotted value, and re-opens the
    /// conflict if it has moved again.
    Ours,
    /// Take the file's value — the record is rewritten to it (or
    /// tombstoned, when the file no longer has the record).
    Theirs,
    /// Take a hand-merged value. The usual outcome of a real three-way
    /// resolution, where neither side is wholly right.
    Merged(serde_json::Value),
    /// Drop the record from both sides: the row becomes a tombstone and
    /// the next write removes the key from the file.
    Delete,
}

/// Options for a working-tree scan — see
/// [`crate::SyncedRepo::update_from_working_dir_with`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ScanOptions {
    /// Let the file win over every in-flight edit: pending rows are
    /// overwritten from disk and conflict rows are dropped.
    ///
    /// The blanket form of the per-record `Git-Sync-Resolves-Version`
    /// trailer, for an operator who has decided the working tree is
    /// authoritative. It also bypasses the unchanged-file skip — a
    /// pending row diverges from the file whatever the file's bytes
    /// have done since the last take-in, so skipping on unchanged bytes
    /// would make this a no-op on exactly the files it is meant to
    /// resolve.
    pub force: bool,
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
    /// With `branch` on each entry it says which worktree made these
    /// writes — not decoration: merging a branch brings its rollup
    /// commits into another branch's history, so a reader walking a log
    /// sees rollups it did not make.
    ///
    /// It is *not* sufficient to decide whether the ranges are usable.
    /// Versions are drawn per family — an upstream together with its
    /// forks and drafts — so a fork's history contains upstream rollups
    /// whose origin differs but whose ranges are the same sequence and
    /// are perfectly usable, while a merge from an unrelated repository
    /// carries ranges that are not. Nothing in the message distinguishes
    /// those two cases today; a rebuild that cares needs the family
    /// recorded as well.
    ///
    /// It is [`Worktree::origin`], so it arrives already normalized by
    /// [`crate::git::normalize_git_url_hard`] and an equality test is
    /// sound — the same repository spelled `ssh://` in one commit and
    /// `https://` in another still compares equal.
    pub origin: Option<String>,
    /// [`Worktree::origin`] of the family's root — the upstream this
    /// worktree and its sibling forks and drafts all draw versions from
    /// — read from the `Git-Sync-Family` trailer. `None` when that
    /// trailer is absent.
    ///
    /// This, not [`Self::origin`], is what says whether the ranges in a
    /// rollup belong to the sequence a reader is reconstructing. A
    /// fork's history contains upstream rollups written under a
    /// different origin but drawn from the same counter, and a merge
    /// from an unrelated repository carries rollups drawn from a
    /// different one; only the family tells them apart. It is the root's
    /// origin rather than its row id because a row id means nothing
    /// outside the database that assigned it.
    pub family: Option<String>,
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
    /// op that was allocated a version and then failed. A batch reserves
    /// its whole range up front, so *any* failed op leaves a gap, not
    /// only one rejected after a version was drawn for it. (Only a
    /// non-atomic batch can do that -- an atomic one rolls back whole
    /// and records no row at all.)
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

/// Outcome of a sync in either direction — one shape for
/// [`crate::SyncedRepo::update_from_working_dir`] (which fills the
/// scan counters) and [`crate::SyncedRepo::save_changes`] (which fills
/// [`Self::written`] / [`Self::failed`]), so a library client reads
/// [`Self::conflicts`] the same way whichever function found them.
///
/// A pure scan leaves the write lists empty; a pure save leaves the
/// scan counters zero. `files_updated` ≤ `files_seen`;
/// `records_upserted` and `records_deleted` are totals across the
/// whole pass.
#[derive(Debug, Default)]
pub struct SyncOutcome {
    /// Tracked files visited by the sync pass.
    pub files_seen: usize,
    /// Files whose database state was refreshed — records re-extracted
    /// from changed bytes, or commit attribution updated for unchanged
    /// bytes whose git state moved.
    pub files_updated: usize,
    /// Files skipped whole: same bytes and same git state as the last
    /// take-in, so there was nothing to re-extract. A skipped file is
    /// not re-classified against the format registry.
    pub files_unchanged: usize,
    /// Total records inserted or refreshed in this pass. A record in a
    /// changed file whose value and commit attribution both already
    /// match the database is not rewritten and not counted — its
    /// `version` stays put, so `Pending` OCC tokens and `list_changes`
    /// cursors see only records that actually changed.
    pub records_upserted: usize,
    /// Records hard-deleted because they disappeared from disk.
    pub records_deleted: usize,
    /// In-flight client edits the scan left untouched rather than
    /// overwriting from disk — includes every row in [`Self::conflicts`]
    /// plus the quiet cases (unsaved creates, edits matching the file).
    /// Rows in files skipped as unchanged are not counted; see
    /// [`Self::files_unchanged`].
    pub records_preserved: usize,
    /// Rows the sync found the file disagreeing with a pending edit on.
    /// Each one also exists as a conflict row in the database (see
    /// [`ConflictState`]), so this list is a report of state that
    /// persists rather than the only record of it. Settle them with
    /// [`crate::SyncedRepo::resolve_conflict`]; until then a write
    /// leaves the file's value in place and the record unsaved.
    pub conflicts: Vec<RecordConflict>,
    /// Files that parsed only as JSON5 — a comment, a trailing comma, an
    /// unquoted key — rather than as strict JSON. Counts only files
    /// actually parsed this pass, not skipped ones.
    ///
    /// They are read fine. It is the *write* side that makes this worth
    /// reporting: a rewrite emits strict JSON, so the first change to a
    /// record in such a file normalizes it and drops its comments.
    pub files_needing_json5: usize,
    /// Files changed on disk by a save. Absent means failed, or
    /// unchanged — the rendered bytes matched what was already there.
    pub written: Vec<std::path::PathBuf>,
    /// How many of [`Self::written`] were removals rather than
    /// rewrites. See [`crate::SyncedRepo::delete_file`].
    pub files_deleted: usize,
    /// Files a save could not write, each with the reason. Per file
    /// because one failure says nothing about the rest, and reading
    /// this is the only way to learn which files a partly-successful
    /// save left modified on disk.
    pub failed: Vec<SaveFailure>,
}

impl SyncOutcome {
    /// The first write failure, if any. For a caller that wants to
    /// stop on the first problem while still seeing what was written.
    pub fn first_error(&self) -> Option<&crate::Error> {
        self.failed.first().map(|f| &f.error)
    }
}

/// One record where the file disagrees with an in-flight client edit.
///
/// Both sides are preserved: the database keeps serving its own row and
/// the file keeps serving its value, with the latter materialized as a
/// conflict row (see [`ConflictState`]) that this report mirrors. Until
/// [`crate::SyncedRepo::resolve_conflict`] settles it, a write skips the
/// record rather than overwriting either side.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RecordConflict {
    /// Working-tree-relative path of the file.
    pub file_path: String,
    /// Parent JSON-pointer the record sits under.
    pub path: String,
    /// The record's key within that section.
    pub key: String,
    /// Which pair of changes collided.
    pub kind: RecordConflictKind,
    /// Commit the pending edit is based on — the merge base for
    /// resolving against git history. `None` for [`RecordConflictKind::AddAdd`].
    pub base_commit_id: Option<String>,
    /// The file's version of the record — `None` when the file side
    /// deleted it ([`RecordConflictKind::ModifyDelete`]). Carried here
    /// because a subsequent write replaces it with the pending edit:
    /// for an uncommitted disk change this report is then the only
    /// place the value survives, and rewriting the record to it (or
    /// deleting the record) is how a caller resolves in the file's
    /// favor.
    pub theirs: Option<serde_json::Value>,
}

/// The colliding pair behind a [`RecordConflict`], named
/// ours-then-theirs: the pending edit first, the disk-side change
/// second.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum RecordConflictKind {
    /// Pending edit vs. a different value in the file.
    ModifyModify,
    /// Pending create vs. the same key added to the file independently.
    AddAdd,
    /// Pending delete vs. an edit in the file — the record being
    /// deleted is not the one the client saw.
    DeleteModify,
    /// Pending edit vs. the key removed from the file.
    ModifyDelete,
}

/// Result of one [`crate::SyncedRepo::write_file`] call.
#[derive(Debug, Default)]
pub struct WriteFileOutcome {
    /// Path changed on disk, or `None` when there was nothing to write,
    /// the rendered bytes matched what was already there, or a file
    /// this call would have removed was already gone.
    pub written: Option<std::path::PathBuf>,
    /// The change was a removal: the database had this file deleted and
    /// nothing live or contested was left in it. See
    /// [`crate::SyncedRepo::delete_file`].
    pub deleted: bool,
    /// Records the write left alone because the two sides disagree:
    /// divergences already on record as conflict rows, plus any the
    /// write itself found by applying over a stale on-disk document.
    /// `theirs` holds the file's value, which is what stays in the
    /// file. Settle one with
    /// [`crate::SyncedRepo::resolve_conflict`].
    pub conflicts: Vec<RecordConflict>,
}

/// One file [`crate::SyncedRepo::save_changes`] could not write.
#[derive(Debug)]
pub struct SaveFailure {
    /// Working-tree-relative path of the file.
    pub file_path: String,
    /// Why it could not be written.
    pub error: crate::Error,
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
