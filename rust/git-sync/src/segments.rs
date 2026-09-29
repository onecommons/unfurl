// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! The segment design's operations on one worktree's draft
//! (docs/branch-segments.md §4.2, §4.3, §4.11), over
//! [`crate::db::store::Store`].
//!
//! These follow the in-memory implementation the model test checks
//! (`tests/segments/imp.rs`), which `tests/segments_sql.rs` holds them
//! to: a view, conflicts and ids that agree with it exactly.

use std::collections::BTreeSet;

use crate::db::store::{At, Filter, NewRow, Row, Scope, Store};
use crate::error::Result;
use crate::model::ConflictState;

/// What a write puts at its key: a value, or a tombstone.
#[derive(Debug, Clone, Copy)]
pub(crate) enum Value<'a> {
    Live(&'a serde_json::Value),
    Deleted,
}

/// How a draft row came to be.
#[derive(Debug, Clone, Copy)]
pub(crate) enum Origin<'a> {
    /// A client's edit: pending, with a base.
    Edit,
    /// Taken in from the working tree, keeping the file's last commit.
    File(Option<&'a str>),
}

/// The row a write made, and its record.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Written {
    pub(crate) id: i64,
    pub(crate) key_id: i64,
}

/// Write a new version into worktree `w`'s draft, made through its own
/// view (§4.2, C.9). `record` is the `key_id` it takes, `None` for a new
/// record.
pub(crate) async fn write<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    at: At<'_>,
    value: Value<'_>,
    origin: Origin<'_>,
    record: Option<i64>,
    version: i64,
) -> Result<Written> {
    let d = DB::segs(tx, w).await?.draft;
    let edit = matches!(origin, Origin::Edit);
    let draft = DB::rows_in(tx, d, None, false).await?;
    // at most one edit per record: one at another key is replaced by
    // this one, which keeps its base
    let other = record.and_then(|id| {
        draft
            .iter()
            .find(|y| y.key_id == id && y.at() != at && edit)
            .cloned()
    });
    let live_anywhere = match record {
        Some(id) => live_anywhere(tx, w, &draft, id).await?,
        None => false,
    };
    if let (Some(y), Some(id)) = (&other, record) {
        replace_other(tx, w, d, y, id).await?;
    }
    let prior = draft.iter().find(|y| y.at() == at).cloned();
    let (base, base_json) = if edit && live_anywhere {
        edit_base(tx, w, record, prior.as_ref(), other.as_ref()).await?
    } else {
        (None, None)
    };
    let shown = DB::visible(tx, w, Scope::Own, Filter::At(at)).await?;
    let mut settled: BTreeSet<i64> = prior
        .as_ref()
        .map(|p| p.settled.iter().copied().collect())
        .unwrap_or_default();
    settled.extend(
        shown
            .iter()
            .filter(|r| r.segment_id != d && !r.deleted && Some(r.key_id) != record)
            .map(|r| r.key_id),
    );
    let mut seen = seen_rows(tx, w, at, &shown, record).await?;
    // a tombstone holds the value it removes
    let gone;
    let (json, deleted) = match value {
        Value::Live(v) => (v, false),
        Value::Deleted => {
            gone = shown
                .iter()
                .find(|r| !r.deleted)
                .map(|r| r.json.clone())
                .or_else(|| prior.as_ref().map(|p| p.json.clone()))
                .unwrap_or(serde_json::Value::Null);
            (&gone, true)
        }
    };
    if let Some(p) = &prior {
        seen.retain(|r| r.id != p.id);
        DB::delete_row(tx, p.id).await?;
        if let Some(id) = record {
            retag(tx, d, p.key_id, id).await?;
        }
    }
    let settled: Vec<i64> = if edit {
        settled.into_iter().collect()
    } else {
        Vec::new()
    };
    let (id, key_id) = DB::insert(
        tx,
        d,
        NewRow {
            at,
            key_id: record,
            commit_id: match origin {
                Origin::Edit => None,
                Origin::File(c) => c,
            },
            json,
            deleted,
            version,
            base_commit_id: base.as_deref(),
            base_json: base_json.as_ref(),
            settled: &settled,
            conflict: None,
        },
    )
    .await?;
    hide(tx, d, at, key_id, &seen).await?;
    Ok(Written { id, key_id })
}

/// Whether `w`'s view shows record `id` live anywhere, a live row in
/// `draft` included, the writer's own too. A write of a record it doesn't
/// is a re-create, with no base.
async fn live_anywhere<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    draft: &[Row],
    id: i64,
) -> Result<bool> {
    Ok(draft.iter().any(|y| y.key_id == id && !y.deleted)
        || DB::visible(tx, w, Scope::Chain, Filter::KeyId(id))
            .await?
            .iter()
            .any(|r| !r.deleted))
}

/// Remove draft `d`'s edit `y` of record `id`, which a write of it at
/// another key replaces.
async fn replace_other<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    d: i64,
    y: &Row,
    id: i64,
) -> Result<()> {
    drop_conflict(tx, d, y.at()).await?;
    DB::delete_row(tx, y.id).await?;
    // its entries stay with this edit, but its own tree's row of
    // another record shows again at the key it leaves
    let chain: BTreeSet<i64> = DB::visible(tx, w, Scope::Chain, Filter::All)
        .await?
        .iter()
        .map(|r| r.segment_id)
        .collect();
    for e in DB::entries_in(tx, d).await? {
        if e.key_id == id && e.at() == y.at() && e.row_key_id != id {
            let seg = row_segment(tx, w, e.row).await?;
            if seg.is_some_and(|s| chain.contains(&s)) {
                DB::untag(tx, e.row, d, id).await?;
            }
        }
    }
    Ok(())
}

/// The committed version an edit of `record` was made over, its commit and
/// content: the base of the edit it replaces at its key, `prior`, or at
/// another, `other`, else the record's committed version in the view, at
/// whatever key.
async fn edit_base<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    record: Option<i64>,
    prior: Option<&Row>,
    other: Option<&Row>,
) -> Result<(Option<String>, Option<serde_json::Value>)> {
    if let Some(y) = prior.or(other) {
        return Ok((y.base_commit_id.clone(), y.base_json.clone()));
    }
    Ok(match record {
        Some(id) => DB::visible(tx, w, Scope::Chain, Filter::KeyId(id))
            .await?
            .into_iter()
            .find(|r| !r.deleted)
            .map_or((None, None), |r| (r.commit_id, Some(r.json))),
        None => (None, None),
    })
}

/// The rows a write at `at` was made over: those `shown` there, and the
/// same record's rows elsewhere with content seen here, a move's copy of
/// what the edit was made over (§3.5).
async fn seen_rows<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    at: At<'_>,
    shown: &[Row],
    record: Option<i64>,
) -> Result<Vec<Row>> {
    let mut seen = shown.to_vec();
    let Some(id) = record else {
        return Ok(seen);
    };
    let contents: Vec<&serde_json::Value> = shown
        .iter()
        .filter(|r| !r.deleted && r.key_id == id)
        .map(|r| &r.json)
        .collect();
    if !contents.is_empty() {
        for r in DB::visible(tx, w, Scope::Own, Filter::KeyId(id)).await? {
            if r.at() != at && !r.deleted && contents.contains(&&r.json) {
                seen.push(r);
            }
        }
    }
    Ok(seen)
}

/// Draft `d`'s new row of record `key_id` at `at` hides the rows it was
/// made over, `seen`, and those the draft already hides there, but a live
/// row of a record it edits elsewhere, which the merge hid from this write.
async fn hide<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    d: i64,
    at: At<'_>,
    key_id: i64,
    seen: &[Row],
) -> Result<()> {
    let elsewhere: BTreeSet<i64> = DB::rows_in(tx, d, None, false)
        .await?
        .iter()
        .filter(|r| r.at() != at)
        .map(|r| r.key_id)
        .collect();
    let hidden: Vec<i64> = DB::entries_in(tx, d)
        .await?
        .into_iter()
        .filter(|e| e.at() == at && (e.deleted || !elsewhere.contains(&e.row_key_id)))
        .map(|e| e.row)
        .collect();
    for r in seen.iter().map(|r| r.id).chain(hidden) {
        DB::entry(tx, r, d, key_id).await?;
    }
    Ok(())
}

/// The segment row `row` is in, if `w`'s view holds it.
async fn row_segment<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    row: i64,
) -> Result<Option<i64>> {
    Ok(DB::visible(tx, w, Scope::Own, Filter::All)
        .await?
        .into_iter()
        .find(|r| r.id == row)
        .map(|r| r.segment_id))
}

/// Draft `d`'s entries made by edit `old` become `new`'s, but at a key
/// another row of `old` holds.
pub(crate) async fn retag<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    d: i64,
    old: i64,
    new: i64,
) -> Result<()> {
    if old == new {
        return Ok(());
    }
    let others: Vec<(String, String, String)> = DB::rows_in(tx, d, None, false)
        .await?
        .into_iter()
        .filter(|y| y.key_id == old)
        .map(|y| (y.file_path, y.path, y.key))
        .collect();
    for e in DB::entries_in(tx, d).await? {
        let place = (e.file_path.clone(), e.path.clone(), e.key.clone());
        if e.key_id == old && !others.contains(&place) {
            DB::untag(tx, e.row, d, old).await?;
            DB::entry(tx, e.row, d, new).await?;
        }
    }
    Ok(())
}

/// Take the row at `at` out of draft `d`, with the entries its edit made,
/// unless another of the draft's rows is the same record's.
pub(crate) async fn remove_draft_row<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    d: i64,
    at: At<'_>,
) -> Result<()> {
    let draft = DB::rows_in(tx, d, Some(at.file_path), false).await?;
    let Some(x) = draft.iter().find(|y| y.at() == at) else {
        return Ok(());
    };
    let id = x.key_id;
    DB::delete_row(tx, x.id).await?;
    let same: Vec<(String, String, String)> = DB::rows_in(tx, d, None, false)
        .await?
        .into_iter()
        .filter(|y| y.key_id == id)
        .map(|y| (y.file_path, y.path, y.key))
        .collect();
    for e in DB::entries_in(tx, d).await? {
        let place = (e.file_path.clone(), e.path.clone(), e.key.clone());
        if e.key_id == id && !same.contains(&place) {
            DB::untag(tx, e.row, d, id).await?;
        }
    }
    Ok(())
}

/// Draft `d`'s conflict row at `at`.
pub(crate) async fn conflict_row<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    d: i64,
    at: At<'_>,
) -> Result<Option<Row>> {
    Ok(DB::rows_in(tx, d, Some(at.file_path), true)
        .await?
        .into_iter()
        .find(|r| r.at() == at))
}

/// Record the file's side at `at`: `theirs`, or a tombstone holding the
/// value the file dropped.
pub(crate) async fn set_conflict<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    d: i64,
    at: At<'_>,
    theirs: Value<'_>,
    dropped: &serde_json::Value,
    commit_id: Option<&str>,
    version: i64,
) -> Result<()> {
    drop_conflict(tx, d, at).await?;
    let record = DB::rows_in(tx, d, Some(at.file_path), false)
        .await?
        .into_iter()
        .find(|r| r.at() == at)
        .map(|r| r.key_id);
    let (json, deleted) = match theirs {
        Value::Live(v) => (v, false),
        Value::Deleted => (dropped, true),
    };
    DB::insert(
        tx,
        d,
        NewRow {
            at,
            key_id: record,
            commit_id,
            json,
            deleted,
            version,
            base_commit_id: None,
            base_json: None,
            settled: &[],
            conflict: Some(ConflictState::Conflict),
        },
    )
    .await?;
    Ok(())
}

pub(crate) async fn drop_conflict<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    d: i64,
    at: At<'_>,
) -> Result<()> {
    if let Some(c) = conflict_row(tx, d, at).await? {
        DB::delete_row(tx, c.id).await?;
    }
    Ok(())
}

/// Where a record's id comes from when the committed side adds a row at
/// its key (§4.3): the commit's rollup, a record that moved here in this
/// scan, and the ids other keys hold, which it may not take.
pub(crate) struct KeyIds {
    pub(crate) named: Option<i64>,
    pub(crate) moved: Option<i64>,
    pub(crate) elsewhere: BTreeSet<i64>,
}

/// One record whose committed value changed: bring worktree `w`'s head
/// up to it (§4.3, C.10). `change: None` means the commit deleted it.
/// Returns the row it added, if any.
pub(crate) async fn scan_key<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    at: At<'_>,
    change: Option<&serde_json::Value>,
    commit: &str,
    version: i64,
    ids: KeyIds,
) -> Result<Option<Written>> {
    let segs = DB::segs(tx, w).await?;
    let h = segs.head;
    let drafted = DB::rows_in(tx, segs.draft, Some(at.file_path), false)
        .await?
        .into_iter()
        .find(|r| r.at() == at)
        .map(|r| r.key_id);
    let chain = DB::visible(tx, w, Scope::Chain, Filter::At(at)).await?;
    let shown = chain.first().cloned();
    let live = shown.as_ref().filter(|r| !r.deleted);
    let head_row = DB::rows_in(tx, h, Some(at.file_path), false)
        .await?
        .into_iter()
        .find(|r| r.at() == at);
    // the rows below the head the key shows once its own row goes
    let below: Vec<Row> = match &head_row {
        Some(_) => Vec::new(),
        None => chain
            .iter()
            .filter(|r| r.segment_id != h)
            .cloned()
            .collect(),
    };
    let pinned = head_row.is_some() && DB::held_elsewhere(tx, w, at).await?;
    if let Some(r) = &head_row {
        DB::delete_row(tx, r.id).await?;
    }
    let (json, deleted, key_id) = match change {
        Some(v) => {
            let key_id = ids
                .named
                .or(live
                    .map(|r| r.key_id)
                    .filter(|id| !ids.elsewhere.contains(id)))
                .or(ids.moved.filter(|id| !ids.elsewhere.contains(id)))
                // else the record the draft holds at the key
                .or(drafted.filter(|id| !ids.elsewhere.contains(id)));
            (v.clone(), false, key_id)
        }
        // already absent in the chain: the head hides the row below
        None if head_row.is_none() && live.is_none() => return Ok(None),
        None if pinned || below.iter().any(|r| !r.deleted) => {
            let gone = live
                .map(|r| r.json.clone())
                .unwrap_or(serde_json::Value::Null);
            (gone, true, shown.as_ref().map(|r| r.key_id))
        }
        None => return Ok(None),
    };
    let (id, key_id) = DB::insert(
        tx,
        h,
        NewRow {
            at,
            key_id,
            commit_id: Some(commit),
            json: &json,
            deleted,
            version,
            base_commit_id: None,
            base_json: None,
            settled: &[],
            conflict: None,
        },
    )
    .await?;
    for r in below {
        DB::entry(tx, r.id, h, key_id).await?;
    }
    Ok(Some(Written { id, key_id }))
}

/// Each pending edit follows its record, with its conflict, to the place
/// `w`'s committed chain now has it (§3.5). Returns the files edits left.
pub(crate) async fn follow_records<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
) -> Result<BTreeSet<String>> {
    let d = DB::segs(tx, w).await?.draft;
    let mut vacated = BTreeSet::new();
    for x in DB::rows_in(tx, d, None, false).await? {
        if !x.is_edit() {
            continue;
        }
        let here = DB::visible(tx, w, Scope::Chain, Filter::At(x.at()))
            .await?
            .into_iter()
            .find(|r| !r.deleted);
        // not when the record at its key is its own
        if here.is_some_and(|r| r.key_id == x.key_id) {
            continue;
        }
        let to = DB::visible(tx, w, Scope::Chain, Filter::KeyId(x.key_id))
            .await?
            .into_iter()
            .find(|r| !r.deleted && r.at() != x.at());
        let Some(to) = to else {
            continue;
        };
        let taken = DB::rows_in(tx, d, Some(&to.file_path), false)
            .await?
            .iter()
            .any(|y| y.at() == to.at());
        // another edit holds the key it would follow to: it stays, as a
        // new record, so no two files share the id
        if taken {
            renew(tx, d, &x).await?;
            continue;
        }
        relocate(tx, d, &x, to.at()).await?;
        vacated.insert(x.file_path.clone());
    }
    Ok(vacated)
}

/// Move edit `x` to `to`, with its conflict row; its entries on other
/// records at the place it leaves go.
async fn relocate<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    d: i64,
    x: &Row,
    to: At<'_>,
) -> Result<()> {
    if let Some(c) = conflict_row(tx, d, x.at()).await? {
        DB::relocate(tx, c.id, to).await?;
    }
    DB::relocate(tx, x.id, to).await?;
    for e in DB::entries_in(tx, d).await? {
        if e.key_id == x.key_id && e.at() == x.at() && e.row_key_id != x.key_id {
            DB::untag(tx, e.row, d, x.key_id).await?;
        }
    }
    Ok(())
}

/// Draft row `x` becomes a new record: an edit that can't follow its
/// record, or a value taken in from the file where git has its record at
/// another place. Its entries at its place are the new record's; on the
/// old record's rows elsewhere they go. Returns its `key_id`.
pub(crate) async fn renew<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    d: i64,
    x: &Row,
) -> Result<i64> {
    let old = x.key_id;
    // A key_id is its first row's id, so a new record needs a new row.
    DB::delete_row(tx, x.id).await?;
    let (_, fresh) = DB::insert(
        tx,
        d,
        NewRow {
            at: x.at(),
            key_id: None,
            commit_id: x.commit_id.as_deref(),
            json: &x.json,
            deleted: x.deleted,
            version: x.version,
            base_commit_id: x.base_commit_id.as_deref(),
            base_json: x.base_json.as_ref(),
            settled: &x.settled,
            conflict: None,
        },
    )
    .await?;
    let others: Vec<(String, String, String)> = DB::rows_in(tx, d, None, false)
        .await?
        .into_iter()
        .filter(|y| y.key_id == old)
        .map(|y| (y.file_path, y.path, y.key))
        .collect();
    for e in DB::entries_in(tx, d).await? {
        if e.key_id != old {
            continue;
        }
        let place = (e.file_path.clone(), e.path.clone(), e.key.clone());
        if !others.contains(&place) {
            DB::untag(tx, e.row, d, old).await?;
        }
        if e.at() == x.at() {
            DB::entry(tx, e.row, d, fresh).await?;
        }
    }
    Ok(fresh)
}

/// C.11: worktree `w`'s draft supersedes what its committed chain now
/// shows at each place it holds, and an edited record's copy elsewhere
/// with content it already superseded.
pub(crate) async fn relink<DB: Store>(tx: &mut sqlx::Transaction<'_, DB>, w: i64) -> Result<()> {
    let d = DB::segs(tx, w).await?.draft;
    let draft = DB::rows_in(tx, d, None, false).await?;
    let conflicts = DB::rows_in(tx, d, None, true).await?;
    for y in draft.iter().chain(&conflicts) {
        let tag = draft
            .iter()
            .find(|r| r.at() == y.at())
            .map_or(y.key_id, |r| r.key_id);
        for c in DB::visible(tx, w, Scope::Chain, Filter::At(y.at())).await? {
            DB::entry(tx, c.id, d, tag).await?;
        }
    }
    let edited: BTreeSet<i64> = draft
        .iter()
        .filter(|r| r.is_edit())
        .map(|r| r.key_id)
        .collect();
    let entries = DB::entries_in(tx, d).await?;
    let mut seen: Vec<(i64, serde_json::Value)> = Vec::new();
    for e in &entries {
        if !e.deleted && edited.contains(&e.row_key_id) {
            let row = DB::visible(tx, w, Scope::Chain, Filter::KeyId(e.row_key_id))
                .await?
                .into_iter()
                .chain(DB::rows_in(tx, d, None, false).await?)
                .find(|r| r.id == e.row);
            if let Some(r) = row {
                seen.push((r.key_id, r.json));
            }
        }
    }
    for c in DB::visible(tx, w, Scope::Chain, Filter::All).await? {
        if !c.deleted
            && seen
                .iter()
                .any(|(id, json)| *id == c.key_id && *json == c.json)
        {
            DB::entry(tx, c.id, d, c.key_id).await?;
        }
    }
    Ok(())
}

/// When the working tree gets the last word over a pending edit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum FileWins {
    Never,
    /// `force`: over every edit.
    Always,
    /// A `Git-Sync-Resolves-Version: N` trailer: over an edit at version
    /// N or below that diverges and has no standing resolution.
    Diverged(i64),
}

/// Draft `d`'s rows a commit of `files` carries: those written before
/// `watermark`, at no conflict row's key.
async fn carried<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    d: i64,
    files: &BTreeSet<String>,
    watermark: i64,
) -> Result<Vec<Row>> {
    let conflicts = DB::rows_in(tx, d, None, true).await?;
    Ok(DB::rows_in(tx, d, None, false)
        .await?
        .into_iter()
        .filter(|x| files.contains(&x.file_path) && x.version < watermark)
        .filter(|x| !conflicts.iter().any(|c| c.at() == x.at()))
        .collect())
}

/// The rows whose value a commit of worktree `w`'s `files` changes: what
/// its rollup names (§3.5), a deletion under the id of the record it
/// deletes. Those the fold carries, and conflict rows,
/// the file's value, which the commit carries at a key the fold leaves:
/// each with the id [`scan_key`] gives its head row, the chain's live
/// record at the key, else the draft's.
pub(crate) async fn changed<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    files: &BTreeSet<String>,
    watermark: i64,
) -> Result<Vec<Row>> {
    let d = DB::segs(tx, w).await?.draft;
    let chain = DB::visible(tx, w, Scope::Chain, Filter::All).await?;
    let live = |x: &Row| chain.iter().find(|r| r.at() == x.at() && !r.deleted);
    let changes = |x: &Row| match live(x) {
        Some(c) => x.deleted || c.json != x.json,
        None => !x.deleted,
    };
    let draft = DB::rows_in(tx, d, None, false).await?;
    let mut rows: Vec<Row> = Vec::new();
    for mut x in carried(tx, d, files, watermark).await? {
        if !changes(&x) {
            continue;
        }
        // a deletion deletes the record the chain shows at the key
        if let Some(shown) = live(&x).filter(|_| x.deleted) {
            x.key_id = shown.key_id;
        }
        rows.push(x);
    }
    for mut c in DB::rows_in(tx, d, None, true).await? {
        if !files.contains(&c.file_path) || !changes(&c) {
            continue;
        }
        let drafted = draft.iter().find(|y| y.at() == c.at());
        if let Some(id) = live(&c).or(drafted).map(|r| r.key_id) {
            c.key_id = id;
            rows.push(c);
        }
    }
    Ok(rows)
}

/// C.12: fold worktree `w`'s draft rows of `files` into its head, as
/// commit `commit` carries them: those with no conflict row, written
/// before `watermark`. A row keeps its id, `key_id`, version and content.
pub(crate) async fn fold<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    files: &BTreeSet<String>,
    commit: &str,
    watermark: i64,
) -> Result<()> {
    let segs = DB::segs(tx, w).await?;
    let (d, h) = (segs.draft, segs.head);
    let conflicts = DB::rows_in(tx, d, None, true).await?;
    // a conflict row is the file's value, which is what the commit
    // carries; it stays in the draft until resolved
    for c in conflicts.iter().filter(|c| files.contains(&c.file_path)) {
        DB::stamp(tx, c.id, d, commit).await?;
    }
    for x in &carried(tx, d, files, watermark).await? {
        if let Some(hr) = DB::rows_in(tx, h, Some(&x.file_path), false)
            .await?
            .into_iter()
            .find(|r| r.at() == x.at())
        {
            DB::delete_row(tx, hr.id).await?;
        }
        // the draft's entries at its place, and the edit's own at any
        // other, become the head's; never on the head's own rows
        for e in DB::entries_in(tx, d).await? {
            if e.at() != x.at() && e.key_id != x.key_id {
                continue;
            }
            DB::untag(tx, e.row, d, e.key_id).await?;
            let in_head = DB::rows_in(tx, h, Some(&e.file_path), false)
                .await?
                .iter()
                .any(|r| r.id == e.row);
            if !in_head {
                DB::entry(tx, e.row, h, e.key_id).await?;
            }
        }
        DB::stamp(tx, x.id, h, commit).await?;
        // a tombstone that hides nothing below the head goes, unless
        // another worktree's draft holds its key
        if x.deleted {
            let hides = DB::entries_in(tx, h)
                .await?
                .iter()
                .any(|e| e.at() == x.at());
            if !hides && !DB::held_elsewhere(tx, w, x.at()).await? {
                DB::delete_row(tx, x.id).await?;
            }
        }
    }
    Ok(())
}
