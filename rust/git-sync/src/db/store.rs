// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Row-level operations on segments (docs/branch-segments.md §3, §4).
//!
//! [`Store`] is implemented once, by [`store_impl`], for each
//! backend, so its bodies are concrete and need none of the bounds a
//! function generic over [`sqlx::Database`] carries. Code built on it is
//! generic over `DB: Store` alone.
//!
//! Statements are written once, in SQLite's syntax, and
//! [`crate::db::sql!`] spells them for Postgres at compile time: `?N`
//! placeholders, `jsonb(?N)` for JSON going in, and `json(col)` for JSON
//! coming out. Everything else is written to
//! mean the same on both: a bind that can be NULL is cast, booleans are
//! bound rather than written as `0`/`1`.

use std::future::Future;

use crate::commit::CommitId;
use crate::db;
use crate::db::tables::{
    AcrossRow, AcrossRows, At, Coverage, CoveredChain, CoveredRow, CoveredWorktree, DraftedColumns,
    FileRow, NewRecordRow, RecordColumns, RecordRow, SegmentEnd, SegmentGraph, Superseded,
    WithEntries, WorktreeSegs, ROW_COLUMNS,
};
use crate::error::{Error, Result};
use crate::model::ConflictState;

/// Which segments a read looks through: a worktree's, or a segment's.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Scope {
    /// A worktree's committed chain and its draft.
    Own,
    /// A worktree's committed chain alone.
    Chain,
    /// The state a segment ends at: it and its ancestors.
    State,
}

/// Which rows of a view a read returns.
#[derive(Debug, Clone, Copy)]
pub(crate) enum Filter<'a> {
    At(At<'a>),
    /// A section and key, in any file.
    PathKey(&'a str, &'a str),
    File(&'a str),
    KeyId(i64),
    /// Rows changed since a cursor: written after it, or in a segment that
    /// joined the view after it (§4.10).
    Since(i64),
    All,
}

/// The view of worktree `?1`, as the CTE `v(segment_id, added_version)`. A
/// draft's rows, and a segment's ancestors, count as always present.
fn view_cte(scope: Scope) -> &'static str {
    match scope {
        Scope::Own => {
            "WITH v AS (SELECT segment_id, added_version FROM worktree_segment WHERE worktree_id = ?1 \
             UNION ALL SELECT draft_segment_id, 0 FROM worktree WHERE id = ?1) "
        }
        Scope::Chain => {
            "WITH v AS (SELECT segment_id, added_version FROM worktree_segment WHERE worktree_id = ?1) "
        }
        Scope::State => {
            "WITH RECURSIVE v(segment_id, added_version) AS (SELECT CAST(?1 AS BIGINT), CAST(0 AS BIGINT) \
             UNION ALL SELECT s.parent_id, CAST(0 AS BIGINT) FROM segment s JOIN v ON s.id = v.segment_id \
             WHERE s.parent_id IS NOT NULL) "
        }
    }
}

/// The records a write at `?2`..`?4` of `?5` in draft `?1` can touch:
/// `?5`, the draft's at the place, and those an entry there hides.
const AROUND: &str = "records(key_id) AS ( \
    SELECT key_id FROM record WHERE segment_id = ?1 AND file_path = ?2 AND path = ?3 AND key = ?4 \
    UNION SELECT h.key_id FROM record h JOIN superseded x ON x.record_id = h.id \
      WHERE h.file_path = ?2 AND h.path = ?3 AND h.key = ?4 AND x.segment_id = ?1 \
    UNION SELECT CAST(?5 AS BIGINT) WHERE CAST(?5 AS BIGINT) IS NOT NULL)";

fn sort_rows(rows: &mut [RecordRow]) {
    rows.sort_by(|a, b| (a.at(), a.id).cmp(&(b.at(), b.id)));
}

/// Which records [`Store::across_rows`] reads around.
#[derive(Debug, Clone, Copy)]
pub(crate) enum Records<'a> {
    /// These records.
    Ids(&'a [i64]),
    /// The records of these rows.
    OfRows(&'a [i64]),
}

/// The condition that a value is one of the ids `store_impl!`'s `ids`
/// binds as the parameter `?1`.
fn one_of<DB: Store>() -> &'static str {
    match DB::POSTGRES {
        true => "= ANY($1)",
        false => "IN (SELECT value FROM json_each(?1))",
    }
}

/// The store's row-level statements, implemented for each backend. Their futures are
/// `Send`, as the server's handlers need.
pub(crate) trait Store: sqlx::Database + Sized {
    /// Whether this is Postgres, whose statements [`crate::db::sql!`]
    /// spells.
    const POSTGRES: bool;

    fn segs(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
    ) -> impl Future<Output = Result<WorktreeSegs>> + Send;

    /// The rows `worktree_id`'s view shows: visible in `scope`, not
    /// conflict rows, sorted by place. With [`Scope::State`],
    /// `worktree_id` is a segment's.
    fn visible(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        scope: Scope,
        filter: Filter<'_>,
    ) -> impl Future<Output = Result<Vec<RecordRow>>> + Send;

    /// Segment `seg`'s own rows: its conflict rows with `conflicts`,
    /// otherwise the rest.
    fn rows_in(
        tx: &mut sqlx::Transaction<'_, Self>,
        seg: i64,
        file_path: Option<&str>,
        conflicts: bool,
    ) -> impl Future<Output = Result<Vec<RecordRow>>> + Send;

    /// Insert a row; returns its id and `key_id`.
    fn insert(
        tx: &mut sqlx::Transaction<'_, Self>,
        seg: i64,
        row: NewRecordRow<'_>,
    ) -> impl Future<Output = Result<(i64, i64)>> + Send;

    /// Delete a row, with the entries on it and its aliases.
    fn delete_row(
        tx: &mut sqlx::Transaction<'_, Self>,
        id: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// `superseded(row, seg)`, made by edit `key_id`.
    fn entry(
        tx: &mut sqlx::Transaction<'_, Self>,
        row: i64,
        seg: i64,
        key_id: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Every entry `seg` holds: (row, row's place, key_id).
    fn entries_in(
        tx: &mut sqlx::Transaction<'_, Self>,
        seg: i64,
    ) -> impl Future<Output = Result<Vec<Superseded>>> + Send;

    /// Draft `seg`'s rows, conflict rows included, a write at `at` of
    /// `record` can touch: those at `at` and at every place a row of
    /// `record`, of a record with a row at `at`, or of one an entry at `at`
    /// hides, is.
    fn rows_around(
        tx: &mut sqlx::Transaction<'_, Self>,
        seg: i64,
        at: At<'_>,
        record: Option<i64>,
    ) -> impl Future<Output = Result<Vec<RecordRow>>> + Send;

    /// The entries `seg` holds that the same write can touch: those its
    /// records' edits made, and those on a row at `at`.
    fn entries_around(
        tx: &mut sqlx::Transaction<'_, Self>,
        seg: i64,
        at: At<'_>,
        record: Option<i64>,
    ) -> impl Future<Output = Result<Vec<Superseded>>> + Send;

    /// Drop one entry.
    fn untag(
        tx: &mut sqlx::Transaction<'_, Self>,
        row: i64,
        seg: i64,
        key_id: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Move a row into `seg`, stamped with the commit that wrote it, and
    /// clear what only a draft row carries.
    fn stamp(
        tx: &mut sqlx::Transaction<'_, Self>,
        row: i64,
        seg: i64,
        commit_id: CommitId,
    ) -> impl Future<Output = Result<()>> + Send;

    fn set_conflict_state(
        tx: &mut sqlx::Transaction<'_, Self>,
        row: i64,
        state: ConflictState,
        version: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Draw `count` consecutive versions from the family's counter.
    fn next_version(
        tx: &mut sqlx::Transaction<'_, Self>,
        family_id: i64,
        count: i64,
    ) -> impl Future<Output = Result<i64>> + Send;

    /// Give a draft row another record's identity.
    fn set_key_id(
        tx: &mut sqlx::Transaction<'_, Self>,
        row: i64,
        key_id: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Move a draft row to another place, keeping its id and its entries.
    fn relocate(
        tx: &mut sqlx::Transaction<'_, Self>,
        row: i64,
        to: At<'_>,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Whether a draft other than `worktree_id`'s holds a row at `at`.
    fn held_elsewhere(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        at: At<'_>,
    ) -> impl Future<Output = Result<bool>> + Send;

    /// Whether segment `seg` holds an entry on a row at `at`.
    fn hides_at(
        tx: &mut sqlx::Transaction<'_, Self>,
        seg: i64,
        at: At<'_>,
    ) -> impl Future<Output = Result<bool>> + Send;

    /// Point `worktree_id`'s head at `commit`: its segment's and its own.
    fn set_head_commit(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        commit: CommitId,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Record what the committed segments and the working tree hold of a
    /// file: its format, its last commit (NULL while dirty), and the blobs
    /// last taken in from each side.
    fn upsert_file(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        file: &FileRow<'_>,
    ) -> impl Future<Output = Result<()>> + Send;

    /// `worktree_id`'s file rows, or the one at `path`.
    fn files(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        path: Option<&str>,
    ) -> impl Future<Output = Result<Vec<crate::model::File>>> + Send;

    /// Make the transaction one read-only snapshot. Postgres reads each
    /// statement afresh otherwise; SQLite in WAL mode already reads one.
    fn begin_snapshot(
        tx: &mut sqlx::Transaction<'_, Self>,
    ) -> impl Future<Output = Result<()>> + Send;

    /// A file row's `write_seq`: bumped by everything that changes what a
    /// render of the file decides -- a write or removal, a scan taking the
    /// file in, a deletion, a conflict's resolution. A record write doesn't
    /// bump it: a render that misses a new record is only incomplete, as
    /// the record stays pending until a later write puts it on disk.
    fn write_seq(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        path: &str,
    ) -> impl Future<Output = Result<Option<i64>>> + Send;

    /// Bump a file row's `write_seq`.
    fn bump_write_seq(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        path: &str,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Bump a file row's `write_seq` if it is still `expected`, setting
    /// `source_oid` too unless it is `None`; whether it was.
    fn claim_write(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        path: &str,
        expected: i64,
        source_oid: Option<&str>,
    ) -> impl Future<Output = Result<bool>> + Send;

    /// Create the worktree `(origin, branch)`: its own family, with an
    /// empty head, which is its whole chain, and an empty draft.
    fn create_worktree(
        tx: &mut sqlx::Transaction<'_, Self>,
        origin: &str,
        branch: &str,
    ) -> impl Future<Output = Result<i64>> + Send;

    /// Take family `family`'s lock (§4.14): writes draw versions under it,
    /// so a structural change doesn't interleave with one.
    fn lock_family(
        tx: &mut sqlx::Transaction<'_, Self>,
        family: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// The family of a worktree with origin `origin`, if there is one.
    fn family_of_origin(
        tx: &mut sqlx::Transaction<'_, Self>,
        origin: &str,
    ) -> impl Future<Output = Result<Option<i64>>> + Send;

    /// A segment of `family` a chain holds whose state is `commit`'s
    /// (C.16), an internal one first, since it needs no closing.
    fn segment_ending_at(
        tx: &mut sqlx::Transaction<'_, Self>,
        family: i64,
        commit: CommitId,
    ) -> impl Future<Output = Result<Option<i64>>> + Send;

    /// Segment `base` made one to build on: an open head closes
    /// ([`Store::close_head`]), unless it's empty and its parent ends at
    /// its commit too, when the parent is the base instead.
    fn prepare_base(
        tx: &mut sqlx::Transaction<'_, Self>,
        family: i64,
        base: i64,
    ) -> impl Future<Output = Result<Option<i64>>> + Send;

    /// Close `head` if it's an open head: internal now, its owner getting
    /// a new empty head above it at its commit.
    fn close_head(
        tx: &mut sqlx::Transaction<'_, Self>,
        family: i64,
        head: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Record that segment `segment`'s commit `merge` brought in the line
    /// of segment `merged`, which ends at the commit merged.
    fn add_merge(
        tx: &mut sqlx::Transaction<'_, Self>,
        segment: i64,
        merge: CommitId,
        merged: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// The merge commits segment `segment` records.
    fn merges_in(
        tx: &mut sqlx::Transaction<'_, Self>,
        segment: i64,
    ) -> impl Future<Output = Result<Vec<CommitId>>> + Send;

    /// After `s` splits into `s2` above it: `s`'s merges at `later` move to
    /// `s2`, and the merges of `s`'s line point to `s2`.
    fn split_merges(
        tx: &mut sqlx::Transaction<'_, Self>,
        s: i64,
        s2: i64,
        later: &[CommitId],
    ) -> impl Future<Output = Result<()>> + Send;

    /// C.13: create worktree `(origin, branch)` at `commit`, in `family`,
    /// on segment `base`, whose state is `commit`'s, prepared by
    /// [`Store::prepare_base`].
    fn fork_at(
        tx: &mut sqlx::Transaction<'_, Self>,
        family: i64,
        base: i64,
        origin: &str,
        branch: &str,
        commit: CommitId,
    ) -> impl Future<Output = Result<i64>> + Send;

    /// The worktrees a read covers: `id`, else every one of `origin` and
    /// `branch`, either `None` for any, and `ids`, a
    /// [`crate::model::WorktreeFilter::ids_sql`] clause; with their
    /// families' segment graph.
    fn covered(
        tx: &mut sqlx::Transaction<'_, Self>,
        id: Option<i64>,
        origin: Option<&str>,
        branch: Option<&str>,
        ids: &str,
    ) -> impl Future<Output = Result<Coverage>> + Send;

    /// The rows in `segments` at the places where `records` have a row in
    /// them, but conflict rows, and the entries on them as `(row, segment)`.
    fn across_rows(
        tx: &mut sqlx::Transaction<'_, Self>,
        segments: &[i64],
        records: Records<'_>,
    ) -> impl Future<Output = Result<AcrossRows>> + Send;

    /// Every row in `segments` but conflict rows, as [`Store::across_rows`]
    /// reads them.
    fn across_rows_in(
        tx: &mut sqlx::Transaction<'_, Self>,
        segments: &[i64],
    ) -> impl Future<Output = Result<Vec<AcrossRow>>> + Send;

    /// Run `sql`, a statement whose `{ids}` is replaced by the condition
    /// that a row's `id` is one of `ids`, bound as one parameter.
    fn exec_ids(
        tx: &mut sqlx::Transaction<'_, Self>,
        sql: &str,
        ids: &[i64],
    ) -> impl Future<Output = Result<()>> + Send;

    /// The entries in `segments`, as `(row, segment)`.
    fn entries_in_segments(
        tx: &mut sqlx::Transaction<'_, Self>,
        segments: &[i64],
    ) -> impl Future<Output = Result<Vec<(i64, i64)>>> + Send;

    /// The ids of the live `/types` rows in `segments` and `file_path`, or
    /// every file.
    fn types_rows(
        tx: &mut sqlx::Transaction<'_, Self>,
        segments: &[i64],
        file_path: Option<&str>,
    ) -> impl Future<Output = Result<Vec<i64>>> + Send;

    /// Each worktree of `family` with its head commit, by id.
    fn family_lines(
        tx: &mut sqlx::Transaction<'_, Self>,
        family: i64,
    ) -> impl Future<Output = Result<Vec<(i64, Option<CommitId>)>>> + Send;

    /// Each worktree of `family`'s head segment.
    fn family_heads(
        tx: &mut sqlx::Transaction<'_, Self>,
        family: i64,
    ) -> impl Future<Output = Result<Vec<SegmentEnd>>> + Send;

    /// Segment `top` and its ancestors, from `top` down.
    fn segment_chain(
        tx: &mut sqlx::Transaction<'_, Self>,
        top: i64,
    ) -> impl Future<Output = Result<Vec<SegmentEnd>>> + Send;

    /// C.17's structure: segment `s` ends at `commit` now, and a new
    /// segment above it takes its former end, role, children and place in
    /// every chain. Returns the new segment.
    fn split_segment(
        tx: &mut sqlx::Transaction<'_, Self>,
        s: i64,
        commit: CommitId,
    ) -> impl Future<Output = Result<i64>> + Send;

    /// Move row `row` into segment `seg`, as it is.
    fn move_row(
        tx: &mut sqlx::Transaction<'_, Self>,
        row: i64,
        seg: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Segment `from`'s entries on rows at `at` become `to`'s.
    fn move_entries(
        tx: &mut sqlx::Transaction<'_, Self>,
        from: i64,
        to: i64,
        at: At<'_>,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Every row at `at` in `family`'s segments but conflict rows, in any
    /// segment.
    fn rows_at(
        tx: &mut sqlx::Transaction<'_, Self>,
        family: i64,
        at: At<'_>,
    ) -> impl Future<Output = Result<Vec<RecordRow>>> + Send;

    /// Every row at `at`, or of record `key_id` anywhere, in `family`'s
    /// segments but conflict rows, each with its draft's worktree where
    /// it's in a draft.
    fn rows_at_or_of(
        tx: &mut sqlx::Transaction<'_, Self>,
        family: i64,
        at: At<'_>,
        key_id: i64,
    ) -> impl Future<Output = Result<Vec<(RecordRow, Option<i64>)>>> + Send;

    /// A new open head for worktree `owner`, above `parent`, at `commit`.
    fn new_head(
        tx: &mut sqlx::Transaction<'_, Self>,
        family: i64,
        parent: Option<i64>,
        commit: CommitId,
        owner: i64,
    ) -> impl Future<Output = Result<i64>> + Send;

    /// The entries other worktrees' drafts than `w`'s hold on row `row`:
    /// (draft, key_id).
    fn draft_entries_on(
        tx: &mut sqlx::Transaction<'_, Self>,
        row: i64,
        w: i64,
    ) -> impl Future<Output = Result<Vec<(i64, i64)>>> + Send;

    /// C.19: delete worktree `w`, with its head, draft, chain and files;
    /// a family root others belong to is refused with
    /// [`Error::FamilyInUse`].
    fn delete_worktree(
        tx: &mut sqlx::Transaction<'_, Self>,
        w: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// C.14: delete `family`'s committed segments no chain's history holds,
    /// merged lines included, and fold each internal segment left with one
    /// internal child into it, unless that crosses a fork boundary
    /// (§4.13) or a merge brought it in.
    fn compact(
        tx: &mut sqlx::Transaction<'_, Self>,
        family: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// The first internal segment of `family`, by id, with exactly one
    /// child that's internal too, where no worktree holds the two with
    /// different `inherited` flags and no merge brought it in:
    /// `(parent, child)`.
    fn fold_pair(
        tx: &mut sqlx::Transaction<'_, Self>,
        family: i64,
    ) -> impl Future<Output = Result<Option<(i64, i64)>>> + Send;

    /// C.14's fold of internal segment `p` into its only child `c`, which
    /// keeps its id.
    fn fold(
        tx: &mut sqlx::Transaction<'_, Self>,
        p: i64,
        c: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// §4.8 step 5: draft `d`'s entries on worktree `w`'s chain rows at
    /// keys the draft holds no row at.
    fn drop_stale_entries(
        tx: &mut sqlx::Transaction<'_, Self>,
        w: i64,
        d: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// C.18's structure: worktree `w`'s chain is `base`'s ancestry and new
    /// head `head` now, its old head closes, its commit is `commit`, and
    /// cursors from before it reset (§4.10).
    fn rechain(
        tx: &mut sqlx::Transaction<'_, Self>,
        family: i64,
        w: i64,
        base: Option<i64>,
        head: i64,
        commit: CommitId,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Stamp the worktree's outstanding `txn` rows with the commit that
    /// carries their writes.
    fn stamp_txns(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        commit: CommitId,
    ) -> impl Future<Output = Result<()>> + Send;

    /// The worktree's `txn` rows `commit` carried are outstanding again.
    fn unstamp_txns(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        commit: CommitId,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Draft edit `id` is based at `base_commit`, on `base_json`, or is a
    /// create where that's `None`; a tombstone's json is `base_json` too.
    fn rebase_edit(
        tx: &mut sqlx::Transaction<'_, Self>,
        id: i64,
        base_commit: Option<CommitId>,
        base_json: Option<&serde_json::Value>,
    ) -> impl Future<Output = Result<()>> + Send;

    /// The worktree's outstanding `txn` rows, oldest range first.
    fn outstanding_txns(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
    ) -> impl Future<Output = Result<Vec<crate::model::Txn>>> + Send;

    /// Worktree `worktree_id` is a branch the worktree on branch `from`
    /// is exporting its conflicts to.
    fn set_exporting_from(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        from: &str,
    ) -> impl Future<Output = Result<()>> + Send;

    /// Worktree `worktree_id` isn't cloned at startup: it has no checkout.
    fn no_clone_on_start(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    /// `txn` row `id` belongs to `worktree_id` now.
    fn move_txn(
        tx: &mut sqlx::Transaction<'_, Self>,
        id: i64,
        worktree_id: i64,
    ) -> impl Future<Output = Result<()>> + Send;

    fn delete_file(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        path: &str,
    ) -> impl Future<Output = Result<()>> + Send;

    /// The commit the worktree's head is at.
    fn head_commit(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
    ) -> impl Future<Output = Result<Option<CommitId>>> + Send;

    /// The file new records go to when a write names none.
    fn default_file_path(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
    ) -> impl Future<Output = Result<Option<String>>> + Send;

    /// The format `worktree_id` has `file_path` in, if it has the file.
    fn file_format(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        file_path: &str,
    ) -> impl Future<Output = Result<Option<String>>> + Send;

    /// Register a file the worktree hasn't scanned; an existing row is
    /// left as it is.
    fn ensure_file(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        file_path: &str,
        format: &str,
    ) -> impl Future<Output = Result<()>> + Send;

    /// A file row's commit and whether the database owes its removal.
    fn file_state(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        file_path: &str,
    ) -> impl Future<Output = Result<Option<(Option<CommitId>, bool)>>> + Send;

    fn set_file_deleted(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        file_path: &str,
        deleted: bool,
    ) -> impl Future<Output = Result<()>> + Send;

    /// A row's aliases, replacing any it had.
    fn replace_aliases(
        tx: &mut sqlx::Transaction<'_, Self>,
        row: i64,
        aliases: &[(String, String)],
    ) -> impl Future<Output = Result<()>> + Send;

    /// The audit row of one batch write.
    fn insert_txn(
        tx: &mut sqlx::Transaction<'_, Self>,
        worktree_id: i64,
        versions: (i64, i64),
        author: Option<&str>,
        message: Option<&str>,
        created_at: &str,
    ) -> impl Future<Output = Result<()>> + Send;
}

macro_rules! store_impl {
    ($db:ty, $postgres:expr, $ids:expr) => {
        impl Store for $db {
            const POSTGRES: bool = $postgres;

            async fn segs(tx: &mut sqlx::Transaction<'_, Self>, worktree_id: i64) -> Result<WorktreeSegs> {
                let sql = sql!(
                    tx,
                    "SELECT head_segment_id, draft_segment_id, family_id FROM worktree WHERE id = ?1",
                );
                let (head, draft, family): (Option<i64>, Option<i64>, Option<i64>) =
                    sqlx::query_as(&sql).bind(worktree_id).fetch_one(&mut **tx).await?;
                match (head, draft) {
                    (Some(head), Some(draft)) => Ok(WorktreeSegs {
                        head,
                        draft,
                        family: family.unwrap_or(worktree_id),
                    }),
                    _ => Err(Error::Other(format!(
                        "worktree {worktree_id} has no segments"
                    ))),
                }
            }

            async fn visible(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                scope: Scope,
                filter: Filter<'_>,
            ) -> Result<Vec<RecordRow>> {
                let cond = match filter {
                    Filter::At(_) => "AND r.file_path = ?2 AND r.path = ?3 AND r.key = ?4",
                    Filter::PathKey(..) => "AND r.path = ?2 AND r.key = ?3",
                    Filter::File(_) => "AND r.file_path = ?2",
                    Filter::KeyId(_) => "AND r.key_id = ?2",
                    Filter::Since(_) => "AND (r.version > ?2 OR v.added_version > ?2)",
                    Filter::All => "",
                };
                let text = format!(
                    "{}SELECT {ROW_COLUMNS} FROM record r JOIN v ON v.segment_id = r.segment_id \
                     WHERE r.conflict IS NULL \
                       AND NOT EXISTS (SELECT 1 FROM superseded x \
                                       JOIN v vx ON vx.segment_id = x.segment_id \
                                       WHERE x.record_id = r.id) {cond}",
                    view_cte(scope)
                );
                let sql = db::spell(&*tx, text);
                let q = sqlx::query_as::<_, RecordColumns>(&sql).bind(worktree_id);
                let q = match filter {
                    Filter::At(at) => q.bind(at.file_path).bind(at.path).bind(at.key),
                    Filter::PathKey(path, key) => q.bind(path).bind(key),
                    Filter::File(f) => q.bind(f),
                    Filter::KeyId(id) | Filter::Since(id) => q.bind(id),
                    Filter::All => q,
                };
                let mut rows = q
                    .fetch_all(&mut **tx)
                    .await?
                    .into_iter()
                    .map(RecordRow::try_from)
                    .collect::<Result<Vec<_>>>()?;
                sort_rows(&mut rows);
                Ok(rows)
            }

            async fn rows_in(
                tx: &mut sqlx::Transaction<'_, Self>,
                seg: i64,
                file_path: Option<&str>,
                conflicts: bool,
            ) -> Result<Vec<RecordRow>> {
                let text = format!(
                    "SELECT {ROW_COLUMNS} FROM record r WHERE r.segment_id = ?1 \
                       AND (CAST(?2 AS TEXT) IS NULL OR r.file_path = ?2) \
                       AND (r.conflict IS NOT NULL) = ?3"
                );
                let sql = db::spell(&*tx, text);
                let mut rows = sqlx::query_as::<_, RecordColumns>(&sql)
                    .bind(seg)
                    .bind(file_path)
                    .bind(conflicts)
                    .fetch_all(&mut **tx)
                    .await?
                    .into_iter()
                    .map(RecordRow::try_from)
                    .collect::<Result<Vec<_>>>()?;
                sort_rows(&mut rows);
                Ok(rows)
            }

            async fn insert(
                tx: &mut sqlx::Transaction<'_, Self>,
                seg: i64,
                row: NewRecordRow<'_>,
            ) -> Result<(i64, i64)> {
                let json = serde_json::to_string(row.json).map_err(|e| Error::Json {
                    path: row.at.path.to_string(),
                    source: e,
                })?;
                let settled = (!row.settled.is_empty())
                    .then(|| serde_json::to_string(row.settled).expect("ids serialize"));
                let base_json = row
                    .base_json
                    .map(|b| serde_json::to_string(b).expect("a value serializes"));
                let sql = sql!(tx, "INSERT INTO record (key_id, segment_id, file_path, path, key, commit_id, \
                         json, deleted, version, base_commit_id, settled, conflict, base_json, \
                         content_hash) \
                     VALUES (COALESCE(CAST(?1 AS BIGINT), 0), ?2, ?3, ?4, ?5, CAST(?6 AS TEXT), \
                         jsonb(?7), ?8, ?9, CAST(?10 AS TEXT), jsonb(?11), CAST(?12 AS TEXT), \
                         jsonb(?13), ?14) \
                     RETURNING id",);
                let (id,): (i64,) = sqlx::query_as(&sql)
                    .bind(row.key_id)
                    .bind(seg)
                    .bind(row.at.file_path)
                    .bind(row.at.path)
                    .bind(row.at.key)
                    .bind(row.commit_id)
                    .bind(json)
                    .bind(row.deleted)
                    .bind(row.version)
                    .bind(row.base_commit_id)
                    .bind(settled)
                    .bind(row.conflict.map(|c| c.to_column()))
                    .bind(base_json)
                    .bind(db::content_hash(row.json))
                    .fetch_one(&mut **tx)
                    .await?;
                let key_id = match row.key_id {
                    Some(k) => k,
                    None => {
                        let sql = sql!(tx, "UPDATE record SET key_id = id WHERE id = ?1");
                        sqlx::query(&sql).bind(id).execute(&mut **tx).await?;
                        id
                    }
                };
                Ok((id, key_id))
            }

            async fn delete_row(tx: &mut sqlx::Transaction<'_, Self>, id: i64) -> Result<()> {
                let sql = sql!(tx, "DELETE FROM record WHERE id = ?1");
                sqlx::query(&sql).bind(id).execute(&mut **tx).await?;
                Ok(())
            }

            async fn entry(
                tx: &mut sqlx::Transaction<'_, Self>,
                row: i64,
                seg: i64,
                key_id: i64,
            ) -> Result<()> {
                let sql = sql!(tx, "INSERT INTO superseded (record_id, segment_id, key_id) VALUES (?1, ?2, ?3) \
                     ON CONFLICT DO NOTHING",);
                sqlx::query(&sql)
                    .bind(row)
                    .bind(seg)
                    .bind(key_id)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn rows_around(
                tx: &mut sqlx::Transaction<'_, Self>,
                seg: i64,
                at: At<'_>,
                record: Option<i64>,
            ) -> Result<Vec<RecordRow>> {
                let text = format!(
                    "WITH {AROUND}, \
                     places(file_path, path, key) AS ( \
                       SELECT r.file_path, r.path, r.key FROM record r \
                       JOIN records k ON k.key_id = r.key_id WHERE r.segment_id = ?1 \
                       UNION SELECT CAST(?2 AS TEXT), CAST(?3 AS TEXT), CAST(?4 AS TEXT)) \
                     SELECT {ROW_COLUMNS} FROM places p JOIN record r \
                       ON r.file_path = p.file_path AND r.path = p.path AND r.key = p.key \
                     WHERE r.segment_id = ?1"
                );
                let sql = db::spell(&*tx, text);
                let mut rows = sqlx::query_as::<_, RecordColumns>(&sql)
                    .bind(seg)
                    .bind(at.file_path)
                    .bind(at.path)
                    .bind(at.key)
                    .bind(record)
                    .fetch_all(&mut **tx)
                    .await?
                    .into_iter()
                    .map(RecordRow::try_from)
                    .collect::<Result<Vec<_>>>()?;
                sort_rows(&mut rows);
                Ok(rows)
            }

            async fn entries_around(
                tx: &mut sqlx::Transaction<'_, Self>,
                seg: i64,
                at: At<'_>,
                record: Option<i64>,
            ) -> Result<Vec<Superseded>> {
                let text = format!(
                    "WITH {AROUND} \
                     SELECT x.record_id, r.file_path, r.path, r.key, r.key_id, r.deleted, x.key_id \
                     FROM superseded x JOIN records k ON k.key_id = x.key_id \
                     JOIN record r ON r.id = x.record_id WHERE x.segment_id = ?1 \
                     UNION \
                     SELECT x.record_id, r.file_path, r.path, r.key, r.key_id, r.deleted, x.key_id \
                     FROM record r JOIN superseded x ON x.record_id = r.id AND x.segment_id = ?1 \
                     WHERE r.file_path = ?2 AND r.path = ?3 AND r.key = ?4 \
                     ORDER BY 1, 7"
                );
                let sql = db::spell(&*tx, text);
                let rows: Vec<(i64, String, String, String, i64, bool, i64)> =
                    sqlx::query_as(&sql)
                        .bind(seg)
                        .bind(at.file_path)
                        .bind(at.path)
                        .bind(at.key)
                        .bind(record)
                        .fetch_all(&mut **tx)
                        .await?;
                Ok(rows
                    .into_iter()
                    .map(|(row, file_path, path, key, row_key_id, deleted, key_id)| Superseded {
                        row,
                        file_path,
                        path,
                        key,
                        row_key_id,
                        deleted,
                        key_id,
                    })
                    .collect())
            }

            async fn entries_in(
                tx: &mut sqlx::Transaction<'_, Self>,
                seg: i64,
            ) -> Result<Vec<Superseded>> {
                let sql = sql!(tx, "SELECT x.record_id, r.file_path, r.path, r.key, r.key_id, r.deleted, x.key_id \
                     FROM superseded x JOIN record r ON r.id = x.record_id \
                     WHERE x.segment_id = ?1 ORDER BY x.record_id, x.key_id",);
                let rows: Vec<(i64, String, String, String, i64, bool, i64)> =
                    sqlx::query_as(&sql).bind(seg).fetch_all(&mut **tx).await?;
                Ok(rows
                    .into_iter()
                    .map(|(row, file_path, path, key, row_key_id, deleted, key_id)| Superseded {
                        row,
                        file_path,
                        path,
                        key,
                        row_key_id,
                        deleted,
                        key_id,
                    })
                    .collect())
            }

            async fn untag(
                tx: &mut sqlx::Transaction<'_, Self>,
                row: i64,
                seg: i64,
                key_id: i64,
            ) -> Result<()> {
                let sql = sql!(tx, "DELETE FROM superseded WHERE record_id = ?1 AND segment_id = ?2 AND key_id = ?3",);
                sqlx::query(&sql)
                    .bind(row)
                    .bind(seg)
                    .bind(key_id)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn stamp(
                tx: &mut sqlx::Transaction<'_, Self>,
                row: i64,
                seg: i64,
                commit_id: CommitId,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE record SET segment_id = ?2, commit_id = ?3, base_commit_id = NULL, \
                         base_json = NULL, settled = NULL WHERE id = ?1",);
                sqlx::query(&sql)
                    .bind(row)
                    .bind(seg)
                    .bind(commit_id)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn set_conflict_state(
                tx: &mut sqlx::Transaction<'_, Self>,
                row: i64,
                state: ConflictState,
                version: i64,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE record SET conflict = ?2, version = ?3 WHERE id = ?1");
                sqlx::query(&sql)
                    .bind(row)
                    .bind(state.to_column())
                    .bind(version)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn next_version(
                tx: &mut sqlx::Transaction<'_, Self>,
                family_id: i64,
                count: i64,
            ) -> Result<i64> {
                let sql = sql!(tx, "UPDATE version_seq SET next_version = next_version + ?2 \
                     WHERE worktree_id = ?1 RETURNING next_version - ?2",);
                let (first,): (i64,) = sqlx::query_as(&sql)
                    .bind(family_id)
                    .bind(count)
                    .fetch_one(&mut **tx)
                    .await?;
                Ok(first)
            }

            async fn set_key_id(
                tx: &mut sqlx::Transaction<'_, Self>,
                row: i64,
                key_id: i64,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE record SET key_id = ?2 WHERE id = ?1");
                sqlx::query(&sql)
                    .bind(row)
                    .bind(key_id)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn relocate(
                tx: &mut sqlx::Transaction<'_, Self>,
                row: i64,
                to: At<'_>,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE record SET file_path = ?2, path = ?3, key = ?4 WHERE id = ?1",);
                sqlx::query(&sql)
                    .bind(row)
                    .bind(to.file_path)
                    .bind(to.path)
                    .bind(to.key)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn held_elsewhere(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                at: At<'_>,
            ) -> Result<bool> {
                let sql = sql!(tx, "SELECT EXISTS (SELECT 1 FROM record r JOIN segment s ON s.id = r.segment_id \
                     WHERE s.kind = 'draft' AND s.owner_id <> ?1 AND r.conflict IS NULL \
                       AND r.file_path = ?2 AND r.path = ?3 AND r.key = ?4)",);
                let (held,): (bool,) = sqlx::query_as(&sql)
                    .bind(worktree_id)
                    .bind(at.file_path)
                    .bind(at.path)
                    .bind(at.key)
                    .fetch_one(&mut **tx)
                    .await?;
                Ok(held)
            }

            async fn hides_at(
                tx: &mut sqlx::Transaction<'_, Self>,
                seg: i64,
                at: At<'_>,
            ) -> Result<bool> {
                let sql = sql!(tx, "SELECT EXISTS (SELECT 1 FROM record r \
                     JOIN superseded x ON x.record_id = r.id AND x.segment_id = ?1 \
                     WHERE r.file_path = ?2 AND r.path = ?3 AND r.key = ?4)",);
                let (hides,): (bool,) = sqlx::query_as(&sql)
                    .bind(seg)
                    .bind(at.file_path)
                    .bind(at.path)
                    .bind(at.key)
                    .fetch_one(&mut **tx)
                    .await?;
                Ok(hides)
            }

            async fn set_head_commit(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                commit: CommitId,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE segment SET head_commit = ?2 \
                     WHERE id = (SELECT head_segment_id FROM worktree WHERE id = ?1)",);
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(commit)
                    .execute(&mut **tx)
                    .await?;
                let sql = sql!(tx, "UPDATE worktree SET commit_id = ?2 WHERE id = ?1");
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(commit)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn upsert_file(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                file: &FileRow<'_>,
            ) -> Result<()> {
                let sql = sql!(tx, "INSERT INTO file (worktree_id, path, format, commit_id, source_oid, committed_oid) \
                     VALUES (?1, ?2, ?3, CAST(?4 AS TEXT), CAST(?5 AS TEXT), CAST(?6 AS TEXT)) \
                     ON CONFLICT (worktree_id, path) DO UPDATE SET \
                       format = excluded.format, \
                       commit_id = CASE WHEN ?7 THEN excluded.commit_id ELSE file.commit_id END, \
                       source_oid = CASE WHEN ?7 THEN excluded.source_oid ELSE file.source_oid END, \
                       committed_oid = CASE WHEN ?8 THEN excluded.committed_oid \
                                            ELSE file.committed_oid END, \
                       write_seq = file.write_seq + 1",);
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(file.path)
                    .bind(file.format)
                    .bind(file.commit_id.flatten())
                    .bind(file.source_oid.flatten())
                    .bind(file.committed_oid.flatten())
                    .bind(file.source_oid.is_some())
                    .bind(file.committed_oid.is_some())
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn files(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                path: Option<&str>,
            ) -> Result<Vec<crate::model::File>> {
                let sql = sql!(tx, "SELECT worktree_id, path, format, commit_id, source_oid, committed_oid, deleted \
                     FROM file WHERE worktree_id = ?1 AND (CAST(?2 AS TEXT) IS NULL OR path = ?2) \
                     ORDER BY path",);
                let rows: Vec<(i64, String, String, Option<CommitId>, Option<String>, Option<String>, bool)> =
                    sqlx::query_as(&sql)
                        .bind(worktree_id)
                        .bind(path)
                        .fetch_all(&mut **tx)
                        .await?;
                Ok(rows
                    .into_iter()
                    .map(
                        |(worktree_id, path, format, commit_id, source_oid, committed_oid, deleted)| {
                            crate::model::File {
                                worktree_id,
                                path,
                                format,
                                commit_id,
                                source_oid,
                                committed_oid,
                                deleted,
                            }
                        },
                    )
                    .collect())
            }

            async fn begin_snapshot(tx: &mut sqlx::Transaction<'_, Self>) -> Result<()> {
                if Self::POSTGRES {
                    sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                        .execute(&mut **tx)
                        .await?;
                }
                Ok(())
            }

            async fn write_seq(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                path: &str,
            ) -> Result<Option<i64>> {
                let sql = sql!(tx, "SELECT write_seq FROM file WHERE worktree_id = ?1 AND path = ?2");
                Ok(sqlx::query_scalar(&sql)
                    .bind(worktree_id)
                    .bind(path)
                    .fetch_optional(&mut **tx)
                    .await?)
            }

            async fn bump_write_seq(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                path: &str,
            ) -> Result<()> {
                let sql = sql!(
                    tx,
                    "UPDATE file SET write_seq = write_seq + 1 WHERE worktree_id = ?1 AND path = ?2",
                );
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(path)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn claim_write(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                path: &str,
                expected: i64,
                source_oid: Option<&str>,
            ) -> Result<bool> {
                let sql = sql!(
                    tx,
                    "UPDATE file SET write_seq = write_seq + 1, \
                     source_oid = COALESCE(CAST(?4 AS TEXT), source_oid) \
                     WHERE worktree_id = ?1 AND path = ?2 AND write_seq = ?3",
                );
                let done = sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(path)
                    .bind(expected)
                    .bind(source_oid)
                    .execute(&mut **tx)
                    .await?;
                Ok(done.rows_affected() == 1)
            }

            async fn create_worktree(
                tx: &mut sqlx::Transaction<'_, Self>,
                origin: &str,
                branch: &str,
            ) -> Result<i64> {
                let sql = sql!(tx, "INSERT INTO worktree (origin, branch) VALUES (?1, ?2) RETURNING id",);
                let (w,): (i64,) = sqlx::query_as(&sql)
                    .bind(origin)
                    .bind(branch)
                    .fetch_one(&mut **tx)
                    .await?;
                for sql in [
                    sql!(tx, "INSERT INTO version_seq (worktree_id) VALUES (?1)"),
                    sql!(tx, "UPDATE worktree SET family_id = ?1 WHERE id = ?1"),
                ] {
                    sqlx::query(sql).bind(w).execute(&mut **tx).await?;
                }
                let sql = sql!(tx, "INSERT INTO segment (family_id, kind, owner_id) VALUES (?1, ?2, ?1) RETURNING id",);
                let mut ids = Vec::new();
                for kind in ["head", "draft"] {
                    let (id,): (i64,) = sqlx::query_as(&sql)
                        .bind(w)
                        .bind(kind)
                        .fetch_one(&mut **tx)
                        .await?;
                    ids.push(id);
                }
                let sql = sql!(tx, "UPDATE worktree SET head_segment_id = ?2, draft_segment_id = ?3 WHERE id = ?1",);
                sqlx::query(&sql)
                    .bind(w)
                    .bind(ids[0])
                    .bind(ids[1])
                    .execute(&mut **tx)
                    .await?;
                let sql = sql!(tx, "INSERT INTO worktree_segment (worktree_id, segment_id) VALUES (?1, ?2)",);
                sqlx::query(&sql).bind(w).bind(ids[0]).execute(&mut **tx).await?;
                Ok(w)
            }

            async fn lock_family(tx: &mut sqlx::Transaction<'_, Self>, family: i64) -> Result<()> {
                sqlx::query(sql!(
                    tx,
                    "UPDATE version_seq SET next_version = next_version WHERE worktree_id = ?1"
                ))
                .bind(family)
                .execute(&mut **tx)
                .await?;
                Ok(())
            }

            async fn family_of_origin(
                tx: &mut sqlx::Transaction<'_, Self>,
                origin: &str,
            ) -> Result<Option<i64>> {
                let row: Option<(i64,)> = sqlx::query_as(sql!(
                    tx,
                    "SELECT COALESCE(family_id, id) FROM worktree WHERE origin = ?1 ORDER BY id LIMIT 1"
                ))
                .bind(origin)
                .fetch_optional(&mut **tx)
                .await?;
                Ok(row.map(|(f,)| f))
            }

            async fn segment_ending_at(
                tx: &mut sqlx::Transaction<'_, Self>,
                family: i64,
                commit: CommitId,
            ) -> Result<Option<i64>> {
                let row: Option<(i64,)> = sqlx::query_as(sql!(
                    tx,
                    "SELECT id FROM segment \
                     WHERE family_id = ?1 AND head_commit = ?2 AND kind <> 'draft' \
                       AND EXISTS (SELECT 1 FROM worktree_segment ws WHERE ws.segment_id = segment.id) \
                     ORDER BY CASE WHEN kind = 'internal' THEN 0 ELSE 1 END, id LIMIT 1"
                ))
                .bind(family)
                .bind(commit)
                .fetch_optional(&mut **tx)
                .await?;
                Ok(row.map(|(s,)| s))
            }

            async fn prepare_base(
                tx: &mut sqlx::Transaction<'_, Self>,
                family: i64,
                base: i64,
            ) -> Result<Option<i64>> {
                let (kind, parent, owner): (String, Option<i64>, Option<i64>) = sqlx::query_as(sql!(
                    tx,
                    "SELECT kind, parent_id, owner_id FROM segment WHERE id = ?1"
                ))
                .bind(base)
                .fetch_one(&mut **tx)
                .await?;
                if kind != "head" || owner.is_none() {
                    return Ok(Some(base));
                }
                // empty: no rows, and no entries hiding rows below; and the
                // parent ends where it does, or the fork's merge base would
                // end no segment
                let (empty,): (bool,) = sqlx::query_as(sql!(
                    tx,
                    "SELECT NOT EXISTS (SELECT 1 FROM record WHERE segment_id = ?1) \
                        AND NOT EXISTS (SELECT 1 FROM superseded WHERE segment_id = ?1) \
                        AND EXISTS (SELECT 1 FROM segment h JOIN segment p ON p.id = h.parent_id \
                                    WHERE h.id = ?1 AND p.head_commit = h.head_commit)"
                ))
                .bind(base)
                .fetch_one(&mut **tx)
                .await?;
                if empty {
                    return Ok(parent);
                }
                Self::close_head(tx, family, base).await?;
                Ok(Some(base))
            }

            async fn close_head(
                tx: &mut sqlx::Transaction<'_, Self>,
                family: i64,
                head: i64,
            ) -> Result<()> {
                let (kind, owner): (String, Option<i64>) =
                    sqlx::query_as(sql!(tx, "SELECT kind, owner_id FROM segment WHERE id = ?1"))
                        .bind(head)
                        .fetch_one(&mut **tx)
                        .await?;
                let (Some(owner), "head") = (owner, kind.as_str()) else {
                    return Ok(());
                };
                let (v,): (i64,) = sqlx::query_as(sql!(
                    tx,
                    "SELECT next_version FROM version_seq WHERE worktree_id = ?1"
                ))
                .bind(family)
                .fetch_one(&mut **tx)
                .await?;
                sqlx::query(sql!(
                    tx,
                    "UPDATE segment SET kind = 'internal', owner_id = NULL WHERE id = ?1"
                ))
                .bind(head)
                .execute(&mut **tx)
                .await?;
                let (above,): (i64,) = sqlx::query_as(sql!(
                    tx,
                    "INSERT INTO segment (family_id, kind, parent_id, head_commit, owner_id) \
                     SELECT family_id, 'head', id, head_commit, ?2 FROM segment WHERE id = ?1 \
                     RETURNING id"
                ))
                .bind(head)
                .bind(owner)
                .fetch_one(&mut **tx)
                .await?;
                sqlx::query(sql!(tx, "UPDATE worktree SET head_segment_id = ?2 WHERE id = ?1"))
                    .bind(owner)
                    .bind(above)
                    .execute(&mut **tx)
                    .await?;
                sqlx::query(sql!(
                    tx,
                    "INSERT INTO worktree_segment \
                     (worktree_id, segment_id, added_version, inherited) \
                     VALUES (?1, ?2, ?3, ?4)"
                ))
                .bind(owner)
                .bind(above)
                .bind(v)
                .bind(false)
                .execute(&mut **tx)
                .await?;
                Ok(())
            }

            async fn add_merge(
                tx: &mut sqlx::Transaction<'_, Self>,
                segment: i64,
                merge: CommitId,
                merged: i64,
            ) -> Result<()> {
                sqlx::query(sql!(
                    tx,
                    "INSERT INTO segment_merge (segment_id, merge_commit, merged_segment_id) \
                     VALUES (?1, ?2, ?3) ON CONFLICT DO NOTHING"
                ))
                .bind(segment)
                .bind(merge)
                .bind(merged)
                .execute(&mut **tx)
                .await?;
                Ok(())
            }

            async fn merges_in(
                tx: &mut sqlx::Transaction<'_, Self>,
                segment: i64,
            ) -> Result<Vec<CommitId>> {
                let rows: Vec<(CommitId,)> = sqlx::query_as(sql!(
                    tx,
                    "SELECT DISTINCT merge_commit FROM segment_merge WHERE segment_id = ?1"
                ))
                .bind(segment)
                .fetch_all(&mut **tx)
                .await?;
                Ok(rows.into_iter().map(|(c,)| c).collect())
            }

            async fn split_merges(
                tx: &mut sqlx::Transaction<'_, Self>,
                s: i64,
                s2: i64,
                later: &[CommitId],
            ) -> Result<()> {
                for &m in later {
                    sqlx::query(sql!(
                        tx,
                        "UPDATE segment_merge SET segment_id = ?2 WHERE segment_id = ?1 AND merge_commit = ?3"
                    ))
                    .bind(s)
                    .bind(s2)
                    .bind(m)
                    .execute(&mut **tx)
                    .await?;
                }
                sqlx::query(sql!(
                    tx,
                    "UPDATE segment_merge SET merged_segment_id = ?2 WHERE merged_segment_id = ?1"
                ))
                .bind(s)
                .bind(s2)
                .execute(&mut **tx)
                .await?;
                Ok(())
            }

            async fn fork_at(
                tx: &mut sqlx::Transaction<'_, Self>,
                family: i64,
                base: i64,
                origin: &str,
                branch: &str,
                commit: CommitId,
            ) -> Result<i64> {
                let base = Self::prepare_base(tx, family, base).await?;
                let (w,): (i64,) = sqlx::query_as(sql!(
                    tx,
                    "INSERT INTO worktree (origin, branch, family_id, commit_id) \
                     VALUES (?1, ?2, ?3, ?4) RETURNING id"
                ))
                .bind(origin)
                .bind(branch)
                .bind(family)
                .bind(commit)
                .fetch_one(&mut **tx)
                .await?;
                let (head,): (i64,) = sqlx::query_as(sql!(
                    tx,
                    "INSERT INTO segment (family_id, kind, parent_id, head_commit, owner_id) \
                     VALUES (?1, 'head', ?2, ?3, ?4) RETURNING id"
                ))
                .bind(family)
                .bind(base)
                .bind(commit)
                .bind(w)
                .fetch_one(&mut **tx)
                .await?;
                let (draft,): (i64,) = sqlx::query_as(sql!(
                    tx,
                    "INSERT INTO segment (family_id, kind, owner_id) VALUES (?1, 'draft', ?2) RETURNING id"
                ))
                .bind(family)
                .bind(w)
                .fetch_one(&mut **tx)
                .await?;
                sqlx::query(sql!(
                    tx,
                    "UPDATE worktree SET head_segment_id = ?2, draft_segment_id = ?3 WHERE id = ?1"
                ))
                .bind(w)
                .bind(head)
                .bind(draft)
                .execute(&mut **tx)
                .await?;
                // the base and its ancestors, inherited, then its own head
                if let Some(base) = base {
                    sqlx::query(sql!(
                        tx,
                        "WITH RECURSIVE up(id) AS ( \
                             SELECT CAST(?2 AS BIGINT) \
                             UNION ALL \
                             SELECT s.parent_id FROM segment s JOIN up ON s.id = up.id \
                             WHERE s.parent_id IS NOT NULL) \
                         INSERT INTO worktree_segment (worktree_id, segment_id, added_version, inherited) \
                         SELECT ?1, id, 0, ?3 FROM up"
                    ))
                    .bind(w)
                    .bind(base)
                    .bind(true)
                    .execute(&mut **tx)
                    .await?;
                }
                sqlx::query(sql!(
                    tx,
                    "INSERT INTO worktree_segment (worktree_id, segment_id, added_version, inherited) \
                     VALUES (?1, ?2, 0, ?3)"
                ))
                .bind(w)
                .bind(head)
                .bind(false)
                .execute(&mut **tx)
                .await?;
                Ok(w)
            }

            async fn covered(
                tx: &mut sqlx::Transaction<'_, Self>,
                id: Option<i64>,
                origin: Option<&str>,
                branch: Option<&str>,
                ids: &str,
            ) -> Result<Coverage> {
                let covers = format!(
                    "(CAST(?1 AS BIGINT) IS NULL OR w.id = ?1) \
                     AND (CAST(?2 AS TEXT) IS NULL OR w.origin = ?2) \
                     AND (CAST(?3 AS TEXT) IS NULL OR w.branch = ?3){ids} \
                     AND w.head_segment_id IS NOT NULL AND w.draft_segment_id IS NOT NULL"
                );
                let families = format!("SELECT COALESCE(w.family_id, w.id) FROM worktree w WHERE {covers}");
                // 0: a worktree, its head, draft and family, and a segment
                // of its chain; 1: a segment and its parent; 2: a merge's
                // segment and the one it merged
                let sql = format!(
                    "SELECT CAST(0 AS BIGINT), w.id, w.head_segment_id, w.draft_segment_id, \
                     COALESCE(w.family_id, w.id), ws.segment_id FROM worktree w \
                     LEFT JOIN worktree_segment ws ON ws.worktree_id = w.id WHERE {covers} \
                     UNION ALL SELECT CAST(1 AS BIGINT), s.id, s.parent_id, CAST(0 AS BIGINT), \
                     CAST(0 AS BIGINT), CAST(NULL AS BIGINT) FROM segment s \
                     WHERE s.kind <> 'draft' AND s.parent_id IS NOT NULL \
                       AND s.family_id IN ({families}) \
                     UNION ALL SELECT CAST(2 AS BIGINT), m.segment_id, m.merged_segment_id, \
                     CAST(0 AS BIGINT), CAST(0 AS BIGINT), CAST(NULL AS BIGINT) \
                     FROM segment_merge m JOIN segment s ON s.id = m.segment_id \
                     WHERE s.family_id IN ({families}) \
                     ORDER BY 1, 2"
                );
                // the ids list is inlined: a statement per call
                let rows: Vec<CoveredRow> = sqlx::query_as(&db::spell(&*tx, sql))
                    .persistent(false)
                    .bind(id)
                    .bind(origin)
                    .bind(branch)
                    .fetch_all(&mut **tx)
                    .await?;
                let mut out = Coverage {
                    chains: Vec::new(),
                    graph: SegmentGraph {
                        segments: Vec::new(),
                        merges: Vec::new(),
                    },
                };
                for (kind, a, b, draft, family, segment_id) in rows {
                    match kind {
                        0 => out.chains.push(CoveredChain {
                            worktree: CoveredWorktree {
                                id: a,
                                head: b,
                                draft,
                                family,
                            },
                            segment_id,
                        }),
                        1 => out.graph.segments.push((a, b)),
                        _ => out.graph.merges.push((a, b)),
                    }
                }
                Ok(out)
            }

            async fn across_rows(
                tx: &mut sqlx::Transaction<'_, Self>,
                segments: &[i64],
                records: Records<'_>,
            ) -> Result<AcrossRows> {
                let (ids, of_rows) = match records {
                    Records::Ids(ids) => (ids, false),
                    Records::OfRows(ids) => (ids, true),
                };
                if segments.is_empty() || ids.is_empty() {
                    return Ok(AcrossRows {
                        rows: Vec::new(),
                        entries: Vec::new(),
                    });
                }
                let within = one_of::<Self>();
                let records = match of_rows {
                    false => format!("o.key_id {within}"),
                    true => format!("o.key_id IN (SELECT c.key_id FROM record c WHERE c.id {within})"),
                };
                let segments = db::id_list(segments);
                // `+` keeps SQLite from probing every segment at each place
                let sql = format!(
                    "SELECT r.id, r.segment_id, r.key_id, r.file_path, r.path, r.key, r.deleted, \
                     r.content_hash, (SELECT string_agg(CAST(e.segment_id AS TEXT), ',') \
                     FROM superseded e WHERE e.record_id = r.id) AS entries \
                     FROM record r WHERE r.conflict IS NULL AND +r.segment_id IN ({segments}) \
                       AND (r.file_path, r.path, r.key) IN (SELECT o.file_path, o.path, o.key \
                       FROM record o WHERE o.conflict IS NULL AND o.segment_id IN ({segments}) \
                       AND {records})"
                );
                let found: Vec<WithEntries> = sqlx::query_as(&sql)
                    .persistent(false)
                    .bind(($ids)(ids))
                    .fetch_all(&mut **tx)
                    .await?;
                let mut rows = Vec::with_capacity(found.len());
                let mut entries = Vec::new();
                for WithEntries { row, entries: on } in found {
                    for s in on.iter().flat_map(|on| on.split(',')) {
                        let s = s.parse().map_err(|e| sqlx::Error::Decode(Box::new(e)))?;
                        entries.push((row.id, s));
                    }
                    rows.push(row);
                }
                Ok(AcrossRows { rows, entries })
            }

            async fn across_rows_in(
                tx: &mut sqlx::Transaction<'_, Self>,
                segments: &[i64],
            ) -> Result<Vec<AcrossRow>> {
                if segments.is_empty() {
                    return Ok(Vec::new());
                }
                let sql = format!(
                    "SELECT r.id, r.segment_id, r.key_id, r.file_path, r.path, r.key, r.deleted, \
                     r.content_hash FROM record r \
                     WHERE r.conflict IS NULL AND r.segment_id IN ({}) ORDER BY r.id",
                    db::id_list(segments)
                );
                Ok(sqlx::query_as(&sql).persistent(false).fetch_all(&mut **tx).await?)
            }

            async fn exec_ids(
                tx: &mut sqlx::Transaction<'_, Self>,
                sql: &str,
                ids: &[i64],
            ) -> Result<()> {
                let within = one_of::<Self>();
                sqlx::query(&sql.replace("{ids}", &format!("id {within}")))
                    .persistent(false)
                    .bind(($ids)(ids))
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn entries_in_segments(
                tx: &mut sqlx::Transaction<'_, Self>,
                segments: &[i64],
            ) -> Result<Vec<(i64, i64)>> {
                if segments.is_empty() {
                    return Ok(Vec::new());
                }
                let sql = format!(
                    "SELECT record_id, segment_id FROM superseded WHERE segment_id IN ({})",
                    db::id_list(segments)
                );
                Ok(sqlx::query_as(&sql).persistent(false).fetch_all(&mut **tx).await?)
            }

            async fn types_rows(
                tx: &mut sqlx::Transaction<'_, Self>,
                segments: &[i64],
                file_path: Option<&str>,
            ) -> Result<Vec<i64>> {
                if segments.is_empty() {
                    return Ok(Vec::new());
                }
                let sql = format!(
                    "SELECT r.id FROM record r \
                     WHERE r.segment_id IN ({}) AND r.path = '/types' AND NOT r.deleted \
                       AND r.conflict IS NULL AND (CAST(?1 AS TEXT) IS NULL OR r.file_path = ?1)",
                    db::id_list(segments)
                );
                Ok(sqlx::query_scalar(&db::spell(&*tx, sql))
                    .persistent(false)
                    .bind(file_path)
                    .fetch_all(&mut **tx)
                    .await?)
            }

            async fn family_lines(
                tx: &mut sqlx::Transaction<'_, Self>,
                family: i64,
            ) -> Result<Vec<(i64, Option<CommitId>)>> {
                Ok(sqlx::query_as(sql!(
                    tx,
                    "SELECT w.id, s.head_commit FROM worktree w \
                     JOIN segment s ON s.id = w.head_segment_id \
                     WHERE w.family_id = ?1 ORDER BY w.id"
                ))
                .bind(family)
                .fetch_all(&mut **tx)
                .await?)
            }

            async fn family_heads(
                tx: &mut sqlx::Transaction<'_, Self>,
                family: i64,
            ) -> Result<Vec<SegmentEnd>> {
                Ok(sqlx::query_as(sql!(
                    tx,
                    "SELECT s.id, s.head_commit FROM worktree w \
                     JOIN segment s ON s.id = w.head_segment_id \
                     WHERE w.family_id = ?1 ORDER BY w.id"
                ))
                .bind(family)
                .fetch_all(&mut **tx)
                .await?)
            }

            async fn segment_chain(
                tx: &mut sqlx::Transaction<'_, Self>,
                top: i64,
            ) -> Result<Vec<SegmentEnd>> {
                Ok(sqlx::query_as(sql!(
                    tx,
                    "WITH RECURSIVE down(id, parent_id, head_commit, depth) AS ( \
                         SELECT id, parent_id, head_commit, 0 FROM segment WHERE id = ?1 \
                         UNION ALL \
                         SELECT s.id, s.parent_id, s.head_commit, d.depth + 1 \
                         FROM segment s JOIN down d ON s.id = d.parent_id) \
                     SELECT id, head_commit FROM down ORDER BY depth"
                ))
                .bind(top)
                .fetch_all(&mut **tx)
                .await?)
            }

            async fn split_segment(
                tx: &mut sqlx::Transaction<'_, Self>,
                s: i64,
                commit: CommitId,
            ) -> Result<i64> {
                let (s2,): (i64,) = sqlx::query_as(sql!(
                    tx,
                    "INSERT INTO segment (family_id, kind, parent_id, head_commit, owner_id) \
                     SELECT family_id, kind, id, head_commit, owner_id FROM segment WHERE id = ?1 \
                     RETURNING id"
                ))
                .bind(s)
                .fetch_one(&mut **tx)
                .await?;
                for sql in [
                    sql!(tx, "UPDATE segment SET parent_id = ?2 WHERE parent_id = ?1 AND id <> ?2"),
                    sql!(tx, "UPDATE worktree SET head_segment_id = ?2 WHERE head_segment_id = ?1"),
                    sql!(
                        tx,
                        "INSERT INTO worktree_segment (worktree_id, segment_id, added_version, inherited) \
                         SELECT worktree_id, ?2, added_version, inherited \
                         FROM worktree_segment WHERE segment_id = ?1"
                    ),
                ] {
                    sqlx::query(sql).bind(s).bind(s2).execute(&mut **tx).await?;
                }
                sqlx::query(sql!(
                    tx,
                    "UPDATE segment SET kind = 'internal', owner_id = NULL, head_commit = ?2 WHERE id = ?1"
                ))
                .bind(s)
                .bind(commit)
                .execute(&mut **tx)
                .await?;
                Ok(s2)
            }

            async fn move_row(tx: &mut sqlx::Transaction<'_, Self>, row: i64, seg: i64) -> Result<()> {
                sqlx::query(sql!(tx, "UPDATE record SET segment_id = ?2 WHERE id = ?1"))
                    .bind(row)
                    .bind(seg)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn move_entries(
                tx: &mut sqlx::Transaction<'_, Self>,
                from: i64,
                to: i64,
                at: At<'_>,
            ) -> Result<()> {
                sqlx::query(sql!(
                    tx,
                    "UPDATE superseded SET segment_id = ?2 \
                     WHERE segment_id = ?1 \
                       AND record_id IN (SELECT id FROM record \
                                         WHERE file_path = ?3 AND path = ?4 AND key = ?5) \
                       AND NOT EXISTS (SELECT 1 FROM superseded y \
                                       WHERE y.record_id = superseded.record_id \
                                         AND y.segment_id = ?2 AND y.key_id = superseded.key_id)"
                ))
                .bind(from)
                .bind(to)
                .bind(at.file_path)
                .bind(at.path)
                .bind(at.key)
                .execute(&mut **tx)
                .await?;
                // what was already `to`'s too
                sqlx::query(sql!(
                    tx,
                    "DELETE FROM superseded \
                     WHERE segment_id = ?1 \
                       AND record_id IN (SELECT id FROM record \
                                         WHERE file_path = ?2 AND path = ?3 AND key = ?4)"
                ))
                .bind(from)
                .bind(at.file_path)
                .bind(at.path)
                .bind(at.key)
                .execute(&mut **tx)
                .await?;
                Ok(())
            }

            async fn rows_at(
                tx: &mut sqlx::Transaction<'_, Self>,
                family: i64,
                at: At<'_>,
            ) -> Result<Vec<RecordRow>> {
                let text = format!(
                    "SELECT {ROW_COLUMNS} FROM record r JOIN segment s ON s.id = r.segment_id \
                     WHERE s.family_id = ?1 AND r.file_path = ?2 AND r.path = ?3 AND r.key = ?4 \
                       AND r.conflict IS NULL"
                );
                let sql = db::spell(&*tx, text);
                let rows = sqlx::query_as::<_, RecordColumns>(&sql)
                    .bind(family)
                    .bind(at.file_path)
                    .bind(at.path)
                    .bind(at.key)
                    .fetch_all(&mut **tx)
                    .await?;
                rows.into_iter().map(RecordRow::try_from).collect()
            }

            async fn rows_at_or_of(
                tx: &mut sqlx::Transaction<'_, Self>,
                family: i64,
                at: At<'_>,
                key_id: i64,
            ) -> Result<Vec<(RecordRow, Option<i64>)>> {
                let text = format!(
                    "SELECT {ROW_COLUMNS}, CASE WHEN s.kind = 'draft' THEN s.owner_id END AS draft_of \
                     FROM record r JOIN segment s ON s.id = r.segment_id \
                     WHERE s.family_id = ?1 AND r.file_path = ?2 AND r.path = ?3 AND r.key = ?4 \
                       AND r.conflict IS NULL \
                     UNION ALL \
                     SELECT {ROW_COLUMNS}, CASE WHEN s.kind = 'draft' THEN s.owner_id END \
                     FROM record r JOIN segment s ON s.id = r.segment_id \
                     WHERE s.family_id = ?1 AND r.key_id = ?5 AND r.conflict IS NULL \
                       AND NOT (r.file_path = ?2 AND r.path = ?3 AND r.key = ?4)"
                );
                let sql = db::spell(&*tx, text);
                let rows = sqlx::query_as::<_, DraftedColumns>(&sql)
                    .bind(family)
                    .bind(at.file_path)
                    .bind(at.path)
                    .bind(at.key)
                    .bind(key_id)
                    .fetch_all(&mut **tx)
                    .await?;
                rows.into_iter()
                    .map(|c| Ok((RecordRow::try_from(c.row)?, c.draft_of)))
                    .collect()
            }

            async fn new_head(
                tx: &mut sqlx::Transaction<'_, Self>,
                family: i64,
                parent: Option<i64>,
                commit: CommitId,
                owner: i64,
            ) -> Result<i64> {
                let (id,): (i64,) = sqlx::query_as(sql!(
                    tx,
                    "INSERT INTO segment (family_id, kind, parent_id, head_commit, owner_id) \
                     VALUES (?1, 'head', ?2, ?3, ?4) RETURNING id"
                ))
                .bind(family)
                .bind(parent)
                .bind(commit)
                .bind(owner)
                .fetch_one(&mut **tx)
                .await?;
                Ok(id)
            }

            async fn draft_entries_on(
                tx: &mut sqlx::Transaction<'_, Self>,
                row: i64,
                w: i64,
            ) -> Result<Vec<(i64, i64)>> {
                Ok(sqlx::query_as(sql!(
                    tx,
                    "SELECT x.segment_id, x.key_id FROM superseded x \
                     JOIN segment s ON s.id = x.segment_id \
                     WHERE x.record_id = ?1 AND s.kind = 'draft' AND s.owner_id <> ?2"
                ))
                .bind(row)
                .bind(w)
                .fetch_all(&mut **tx)
                .await?)
            }

            async fn delete_worktree(
                tx: &mut sqlx::Transaction<'_, Self>,
                w: i64,
            ) -> Result<()> {
                let members: i64 = sqlx::query_scalar(sql!(
                    tx,
                    "SELECT count(*) FROM worktree WHERE family_id = ?1 AND id <> ?1"
                ))
                .bind(w)
                .fetch_one(&mut **tx)
                .await?;
                if members > 0 {
                    return Err(Error::FamilyInUse { members });
                }
                // a root alone: its family's segments reference its
                // version_seq row, which goes with it; its own row points at
                // its head and draft
                sqlx::query(sql!(
                    tx,
                    "UPDATE worktree SET head_segment_id = NULL, draft_segment_id = NULL \
                     WHERE id = ?1 AND family_id = ?1"
                ))
                .bind(w)
                .execute(&mut **tx)
                .await?;
                sqlx::query(sql!(tx, "DELETE FROM segment WHERE family_id = ?1"))
                    .bind(w)
                    .execute(&mut **tx)
                    .await?;
                sqlx::query(sql!(tx, "DELETE FROM worktree WHERE id = ?1"))
                    .bind(w)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn compact(
                tx: &mut sqlx::Transaction<'_, Self>,
                family: i64,
            ) -> Result<()> {
                loop {
                    // a segment no chain's history holds, through parents and
                    // merges, has no descendant one holds (I1), so one
                    // statement takes them all
                    let dead = sqlx::query(sql!(
                        tx,
                        "DELETE FROM segment WHERE family_id = ?1 AND kind <> 'draft' \
                         AND id NOT IN ( \
                             WITH RECURSIVE held(id) AS ( \
                                 SELECT ws.segment_id FROM worktree_segment ws \
                                 JOIN segment s ON s.id = ws.segment_id WHERE s.family_id = ?1 \
                                 UNION \
                                 SELECT e.below FROM held h JOIN ( \
                                     SELECT id AS above, parent_id AS below FROM segment \
                                     WHERE parent_id IS NOT NULL \
                                     UNION ALL \
                                     SELECT segment_id, merged_segment_id FROM segment_merge \
                                 ) e ON e.above = h.id) \
                             SELECT id FROM held)"
                    ))
                    .bind(family)
                    .execute(&mut **tx)
                    .await?
                    .rows_affected();
                    if dead > 0 {
                        continue;
                    }
                    let Some((p, c)) = Self::fold_pair(tx, family).await? else {
                        return Ok(());
                    };
                    Self::fold(tx, p, c).await?;
                }
            }

            async fn fold_pair(
                tx: &mut sqlx::Transaction<'_, Self>,
                family: i64,
            ) -> Result<Option<(i64, i64)>> {
                let pairs: Vec<(i64, i64, String)> = sqlx::query_as(sql!(
                    tx,
                    "SELECT p.id, MIN(c.id), MIN(c.kind) FROM segment p \
                     JOIN segment c ON c.parent_id = p.id \
                     WHERE p.family_id = ?1 AND p.kind = 'internal' \
                       AND NOT EXISTS (SELECT 1 FROM segment_merge m WHERE m.merged_segment_id = p.id) \
                     GROUP BY p.id HAVING COUNT(*) = 1 ORDER BY p.id"
                ))
                .bind(family)
                .fetch_all(&mut **tx)
                .await?;
                for (p, c, kind) in pairs {
                    // never into an open head, whose writes replace its rows
                    if kind != "internal" {
                        continue;
                    }
                    // nor across a fork boundary (§4.13)
                    let crosses: bool = sqlx::query_scalar(sql!(
                        tx,
                        "SELECT EXISTS (SELECT 1 FROM worktree_segment a \
                         JOIN worktree_segment b ON b.worktree_id = a.worktree_id \
                         WHERE a.segment_id = ?1 AND b.segment_id = ?2 \
                           AND a.inherited <> b.inherited)"
                    ))
                    .bind(p)
                    .bind(c)
                    .fetch_one(&mut **tx)
                    .await?;
                    if !crosses {
                        return Ok(Some((p, c)));
                    }
                }
                Ok(None)
            }

            async fn fold(
                tx: &mut sqlx::Transaction<'_, Self>,
                p: i64,
                c: i64,
            ) -> Result<()> {
                const STEPS: [&str; 9] = [
                    // the parent's rows the child overrides are hidden in
                    // every chain that holds the parent
                    "DELETE FROM record WHERE segment_id = ?1 AND EXISTS \
                     (SELECT 1 FROM superseded x WHERE x.record_id = record.id \
                      AND x.segment_id = ?2)",
                    "UPDATE record SET segment_id = ?2 WHERE segment_id = ?1",
                    "INSERT INTO superseded (record_id, segment_id, key_id) \
                     SELECT record_id, ?2, key_id FROM superseded WHERE segment_id = ?1 \
                     ON CONFLICT DO NOTHING",
                    "DELETE FROM superseded WHERE segment_id = ?1",
                    // the merges in its commits are the child's now
                    "INSERT INTO segment_merge (segment_id, merge_commit, merged_segment_id) \
                     SELECT ?2, merge_commit, merged_segment_id FROM segment_merge WHERE segment_id = ?1 \
                     ON CONFLICT DO NOTHING",
                    "DELETE FROM segment_merge WHERE segment_id = ?1",
                    "UPDATE segment SET parent_id = \
                     (SELECT parent_id FROM segment WHERE id = ?1) WHERE id = ?2",
                    "DELETE FROM worktree_segment WHERE segment_id = ?1",
                    "DELETE FROM segment WHERE id = ?1",
                ];
                for step in STEPS {
                    sqlx::query(&db::spell(&*tx, step.to_string()))
                        .bind(p)
                        .bind(c)
                        .execute(&mut **tx)
                        .await?;
                }
                Ok(())
            }

            async fn drop_stale_entries(
                tx: &mut sqlx::Transaction<'_, Self>,
                w: i64,
                d: i64,
            ) -> Result<()> {
                sqlx::query(sql!(
                    tx,
                    "DELETE FROM superseded WHERE segment_id = ?2 AND record_id IN ( \
                         SELECT r.id FROM record r \
                         JOIN worktree_segment ws ON ws.segment_id = r.segment_id \
                         WHERE ws.worktree_id = ?1 AND NOT EXISTS ( \
                             SELECT 1 FROM record x WHERE x.segment_id = ?2 \
                             AND x.file_path = r.file_path AND x.path = r.path AND x.key = r.key))"
                ))
                .bind(w)
                .bind(d)
                .execute(&mut **tx)
                .await?;
                Ok(())
            }

            async fn rechain(
                tx: &mut sqlx::Transaction<'_, Self>,
                family: i64,
                w: i64,
                base: Option<i64>,
                head: i64,
                commit: CommitId,
            ) -> Result<()> {
                let (old, v): (i64, i64) = sqlx::query_as(sql!(
                    tx,
                    "SELECT w.head_segment_id, q.next_version FROM worktree w \
                     JOIN version_seq q ON q.worktree_id = ?2 WHERE w.id = ?1"
                ))
                .bind(w)
                .bind(family)
                .fetch_one(&mut **tx)
                .await?;
                // drawn, so a watermark read after the rebuild is at least
                // the reset_version and isn't itself stale
                sqlx::query(sql!(
                    tx,
                    "UPDATE version_seq SET next_version = next_version + 1 WHERE worktree_id = ?1"
                ))
                .bind(family)
                .execute(&mut **tx)
                .await?;
                sqlx::query(sql!(
                    tx,
                    "WITH RECURSIVE up(id) AS ( \
                         SELECT CAST(?2 AS BIGINT) WHERE ?2 IS NOT NULL \
                         UNION ALL \
                         SELECT s.parent_id FROM segment s JOIN up ON s.id = up.id \
                         WHERE s.parent_id IS NOT NULL) \
                     DELETE FROM worktree_segment \
                     WHERE worktree_id = ?1 AND segment_id NOT IN (SELECT id FROM up)"
                ))
                .bind(w)
                .bind(base)
                .execute(&mut **tx)
                .await?;
                // the base's ancestry w doesn't hold, from another line of
                // history, joins now
                sqlx::query(sql!(
                    tx,
                    "WITH RECURSIVE up(id) AS ( \
                         SELECT CAST(?2 AS BIGINT) WHERE ?2 IS NOT NULL \
                         UNION ALL \
                         SELECT s.parent_id FROM segment s JOIN up ON s.id = up.id \
                         WHERE s.parent_id IS NOT NULL) \
                     INSERT INTO worktree_segment (worktree_id, segment_id, added_version, inherited) \
                     SELECT ?1, id, ?3, ?4 FROM up WHERE id NOT IN \
                         (SELECT segment_id FROM worktree_segment WHERE worktree_id = ?1)"
                ))
                .bind(w)
                .bind(base)
                .bind(v)
                .bind(true)
                .execute(&mut **tx)
                .await?;
                sqlx::query(sql!(
                    tx,
                    "INSERT INTO worktree_segment (worktree_id, segment_id, added_version, inherited) \
                     VALUES (?1, ?2, ?3, ?4)"
                ))
                .bind(w)
                .bind(head)
                .bind(v)
                .bind(false)
                .execute(&mut **tx)
                .await?;
                sqlx::query(sql!(
                    tx,
                    "UPDATE segment SET kind = 'internal', owner_id = NULL WHERE id = ?1"
                ))
                .bind(old)
                .execute(&mut **tx)
                .await?;
                sqlx::query(sql!(
                    tx,
                    "UPDATE worktree SET head_segment_id = ?2, commit_id = ?3, reset_version = ?4 \
                     WHERE id = ?1"
                ))
                .bind(w)
                .bind(head)
                .bind(commit)
                .bind(v)
                .execute(&mut **tx)
                .await?;
                Ok(())
            }

            async fn stamp_txns(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                commit: CommitId,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE txn SET commit_id = ?2 WHERE worktree_id = ?1 AND commit_id IS NULL",);
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(commit)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn unstamp_txns(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                commit: CommitId,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE txn SET commit_id = NULL WHERE worktree_id = ?1 AND commit_id = ?2",);
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(commit)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn rebase_edit(
                tx: &mut sqlx::Transaction<'_, Self>,
                id: i64,
                base_commit: Option<CommitId>,
                base_json: Option<&serde_json::Value>,
            ) -> Result<()> {
                let base_json = base_json.map(|b| serde_json::to_string(b).expect("a value serializes"));
                let sql = sql!(tx, "UPDATE record SET base_commit_id = CAST(?2 AS TEXT), base_json = jsonb(?3), \
                     json = CASE WHEN deleted AND ?3 IS NOT NULL THEN jsonb(?3) ELSE json END \
                     WHERE id = ?1",);
                sqlx::query(&sql)
                    .bind(id)
                    .bind(base_commit)
                    .bind(base_json)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn outstanding_txns(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
            ) -> Result<Vec<crate::model::Txn>> {
                let sql = sql!(tx, "SELECT id, worktree_id, first_version, last_version, author, message, \
                         created_at, commit_id FROM txn WHERE worktree_id = ?1 AND commit_id IS NULL \
                     ORDER BY first_version, id",);
                Ok(sqlx::query_as(&sql).bind(worktree_id).fetch_all(&mut **tx).await?)
            }

            async fn set_exporting_from(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                from: &str,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE worktree SET exporting_from = ?2 WHERE id = ?1");
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(from)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn no_clone_on_start(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE worktree SET clone_on_start = FALSE WHERE id = ?1");
                sqlx::query(&sql).bind(worktree_id).execute(&mut **tx).await?;
                Ok(())
            }

            async fn move_txn(
                tx: &mut sqlx::Transaction<'_, Self>,
                id: i64,
                worktree_id: i64,
            ) -> Result<()> {
                let sql = sql!(tx, "UPDATE txn SET worktree_id = ?2 WHERE id = ?1");
                sqlx::query(&sql)
                    .bind(id)
                    .bind(worktree_id)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn delete_file(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                path: &str,
            ) -> Result<()> {
                let sql = sql!(tx, "DELETE FROM file WHERE worktree_id = ?1 AND path = ?2");
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(path)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn head_commit(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
            ) -> Result<Option<CommitId>> {
                let sql = sql!(tx, "SELECT commit_id FROM worktree WHERE id = ?1");
                let (commit,): (Option<CommitId>,) = sqlx::query_as(&sql)
                    .bind(worktree_id)
                    .fetch_one(&mut **tx)
                    .await?;
                Ok(commit)
            }

            async fn default_file_path(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
            ) -> Result<Option<String>> {
                let sql = sql!(tx, "SELECT default_file_path FROM worktree WHERE id = ?1");
                let (path,): (Option<String>,) = sqlx::query_as(&sql)
                    .bind(worktree_id)
                    .fetch_one(&mut **tx)
                    .await?;
                Ok(path)
            }

            async fn file_format(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                file_path: &str,
            ) -> Result<Option<String>> {
                let sql = sql!(tx, "SELECT format FROM file WHERE worktree_id = ?1 AND path = ?2");
                let row: Option<(String,)> = sqlx::query_as(&sql)
                    .bind(worktree_id)
                    .bind(file_path)
                    .fetch_optional(&mut **tx)
                    .await?;
                Ok(row.map(|(f,)| f))
            }

            async fn ensure_file(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                file_path: &str,
                format: &str,
            ) -> Result<()> {
                let sql = sql!(tx, "INSERT INTO file (worktree_id, path, format) VALUES (?1, ?2, ?3) \
                     ON CONFLICT DO NOTHING",);
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(file_path)
                    .bind(format)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn file_state(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                file_path: &str,
            ) -> Result<Option<(Option<CommitId>, bool)>> {
                let sql = sql!(tx, "SELECT commit_id, deleted FROM file WHERE worktree_id = ?1 AND path = ?2",);
                Ok(sqlx::query_as(&sql)
                    .bind(worktree_id)
                    .bind(file_path)
                    .fetch_optional(&mut **tx)
                    .await?)
            }

            async fn set_file_deleted(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                file_path: &str,
                deleted: bool,
            ) -> Result<()> {
                let sql =
                    sql!(tx, "UPDATE file SET deleted = ?3, write_seq = write_seq + 1 \
                              WHERE worktree_id = ?1 AND path = ?2");
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(file_path)
                    .bind(deleted)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }

            async fn replace_aliases(
                tx: &mut sqlx::Transaction<'_, Self>,
                row: i64,
                aliases: &[(String, String)],
            ) -> Result<()> {
                let sql = sql!(tx, "DELETE FROM alias WHERE record_id = ?1");
                sqlx::query(&sql).bind(row).execute(&mut **tx).await?;
                let sql = sql!(tx, "INSERT INTO alias (record_id, path, key) VALUES (?1, ?2, ?3) \
                     ON CONFLICT DO NOTHING",);
                for (path, key) in aliases {
                    sqlx::query(&sql)
                        .bind(row)
                        .bind(path)
                        .bind(key)
                        .execute(&mut **tx)
                        .await?;
                }
                Ok(())
            }

            async fn insert_txn(
                tx: &mut sqlx::Transaction<'_, Self>,
                worktree_id: i64,
                versions: (i64, i64),
                author: Option<&str>,
                message: Option<&str>,
                created_at: &str,
            ) -> Result<()> {
                let sql = sql!(tx, "INSERT INTO txn (worktree_id, first_version, last_version, author, message, \
                         created_at) \
                     VALUES (?1, ?2, ?3, CAST(?4 AS TEXT), CAST(?5 AS TEXT), ?6)",);
                sqlx::query(&sql)
                    .bind(worktree_id)
                    .bind(versions.0)
                    .bind(versions.1)
                    .bind(author)
                    .bind(message)
                    .bind(created_at)
                    .execute(&mut **tx)
                    .await?;
                Ok(())
            }
        }
    };
}

// ids bound as one parameter: a JSON array on SQLite, an array on Postgres,
// which its planner sees as a constant
store_impl!(sqlx::Sqlite, false, |ids: &[i64]| format!(
    "[{}]",
    db::id_list(ids)
));
#[cfg(feature = "postgres")]
store_impl!(sqlx::Postgres, true, <[i64]>::to_vec);
