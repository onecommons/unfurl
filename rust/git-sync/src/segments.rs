// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! The segment design's operations on one worktree's draft
//! (docs/branch-segments.md §4.2, §4.3, §4.11), over
//! [`crate::db::store::Store`].
//!
//! These follow the in-memory implementation the model test checks
//! (`tests/segments/imp.rs`), which `tests/segments_sql.rs` holds them
//! to: a view, conflicts and ids that agree with it exactly.

use std::collections::{BTreeMap, BTreeSet};

use crate::commit::CommitId;
use crate::db::store::{Filter, Scope, Store};
use crate::db::tables::{At, NewRecordRow, Place, RecordRow, Superseded};
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
pub(crate) enum Origin {
    /// A client's edit: pending, with a base.
    Edit,
    /// Taken in from the working tree, keeping the file's last commit.
    File(Option<CommitId>),
}

/// The row a write made, and its record.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Written {
    pub(crate) id: i64,
    pub(crate) key_id: i64,
}

/// [`Draft::write`] into worktree `w`'s draft, the part of it the write
/// can touch loaded for it.
pub(crate) async fn write<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    at: At<'_>,
    value: Value<'_>,
    origin: Origin,
    record: Option<i64>,
    version: i64,
) -> Result<Written> {
    let mut draft = Draft::around(tx, w, at, record).await?;
    let written = draft.write(tx, at, value, origin, record, version).await?;
    #[cfg(debug_assertions)]
    draft.assert_current(tx).await?;
    Ok(written)
}

/// A worktree's draft, read once in a transaction and kept current by the
/// writes made through it. Drawing a write's version takes the family's
/// counter lock, so no other writer changes it meanwhile; whatever else
/// changes it in the transaction must load it again after.
pub(crate) struct Draft {
    pub(crate) worktree_id: i64,
    pub(crate) segment: i64,
    /// Its rows, conflict rows apart, in place order.
    rows: Vec<RecordRow>,
    /// Its conflict rows, read when first needed.
    conflicts: Option<Vec<RecordRow>>,
    /// The rows it hides, each with the edit that hid it.
    entries: Vec<Superseded>,
    held: Held,
}

/// What a [`Draft`] holds of its segment.
enum Held {
    All,
    /// What one write at `place` can touch ([`Store::rows_around`]): the
    /// rows at `places`, where `place` and every row of `records` are, and
    /// the entries `records` made or at `place`. Its lookups stay inside
    /// them, which debug builds check; `places` comes from the rows
    /// loaded, which debug builds check against the database.
    Around {
        place: Place,
        records: BTreeSet<i64>,
        places: BTreeSet<Place>,
    },
}

impl Held {
    fn record(&self, id: i64) -> bool {
        match self {
            Held::All => true,
            Held::Around { records, .. } => records.contains(&id),
        }
    }

    fn place(&self, at: At<'_>) -> bool {
        match self {
            Held::All => true,
            Held::Around { places, .. } => places.iter().any(|p| p.as_at() == at),
        }
    }
}

/// `a` and `b` alike, their content as values: Postgres's jsonb gives back
/// a float that's an integer as one (1e16 as 10000000000000000).
#[cfg(debug_assertions)]
fn same_row(a: &RecordRow, b: &RecordRow) -> bool {
    let bare = |r: &RecordRow| RecordRow {
        json: serde_json::Value::Null,
        base_json: None,
        ..r.clone()
    };
    let base = match (&a.base_json, &b.base_json) {
        (Some(x), Some(y)) => same_value(x, y),
        (x, y) => x.is_none() && y.is_none(),
    };
    bare(a) == bare(b) && same_value(&a.json, &b.json) && base
}

#[cfg(debug_assertions)]
fn same_value(a: &serde_json::Value, b: &serde_json::Value) -> bool {
    use serde_json::Value;
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => x == y || x.as_f64() == y.as_f64(),
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(a, b)| same_value(a, b))
        }
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len()
                && x.iter()
                    .all(|(k, v)| y.get(k).is_some_and(|w| same_value(v, w)))
        }
        _ => a == b,
    }
}

/// An entry on `row`, made by edit `key_id`.
fn entry_on(row: &RecordRow, key_id: i64) -> Superseded {
    Superseded {
        row: row.id,
        file_path: row.file_path.clone(),
        path: row.path.clone(),
        key: row.key.clone(),
        row_key_id: row.key_id,
        deleted: row.deleted,
        key_id,
    }
}

impl Draft {
    /// Worktree `worktree_id`'s draft.
    pub(crate) async fn load<DB: Store>(
        tx: &mut sqlx::Transaction<'_, DB>,
        worktree_id: i64,
    ) -> Result<Self> {
        let segment = DB::segs(tx, worktree_id).await?.draft;
        Ok(Draft {
            worktree_id,
            segment,
            rows: DB::rows_in(tx, segment, None, false).await?,
            conflicts: None,
            entries: DB::entries_in(tx, segment).await?,
            held: Held::All,
        })
    }

    /// The part of worktree `worktree_id`'s draft a write at `at` of
    /// `record` can touch, for that one write.
    pub(crate) async fn around<DB: Store>(
        tx: &mut sqlx::Transaction<'_, DB>,
        worktree_id: i64,
        at: At<'_>,
        record: Option<i64>,
    ) -> Result<Self> {
        let segment = DB::segs(tx, worktree_id).await?.draft;
        let (conflicts, rows): (Vec<RecordRow>, Vec<RecordRow>) =
            DB::rows_around(tx, segment, at, record)
                .await?
                .into_iter()
                .partition(|r| r.conflict.is_some());
        let entries = DB::entries_around(tx, segment, at, record).await?;
        // the records the store's query takes: `record`, those at `at`, and
        // those an entry at `at` hides
        let loaded = || rows.iter().chain(&conflicts);
        let mut records: BTreeSet<i64> = record.into_iter().collect();
        records.extend(loaded().filter(|r| r.at() == at).map(|r| r.key_id));
        records.extend(
            entries
                .iter()
                .filter(|e| e.at() == at)
                .map(|e| e.row_key_id),
        );
        let place = Place::new(at.file_path, at.path, at.key);
        let places: BTreeSet<Place> = loaded().map(Place::from).chain([place.clone()]).collect();
        let draft = Draft {
            worktree_id,
            segment,
            rows,
            conflicts: Some(conflicts),
            entries,
            held: Held::Around {
                place,
                records,
                places,
            },
        };
        #[cfg(debug_assertions)]
        draft.assert_loaded(tx, at, record).await?;
        Ok(draft)
    }

    /// Panic where [`Self::around`]'s records aren't those the database
    /// gives, or it holds other than what they say it must.
    #[cfg(debug_assertions)]
    async fn assert_loaded<DB: Store>(
        &self,
        tx: &mut sqlx::Transaction<'_, DB>,
        at: At<'_>,
        record: Option<i64>,
    ) -> Result<()> {
        let fresh = Draft::load(tx, self.worktree_id).await?;
        let conflicts = DB::rows_in(tx, self.segment, None, true).await?;
        let mut want: BTreeSet<i64> = record.into_iter().collect();
        let here = fresh.rows.iter().chain(&conflicts).filter(|r| r.at() == at);
        want.extend(here.map(|r| r.key_id));
        want.extend(
            fresh
                .entries
                .iter()
                .filter(|e| e.at() == at)
                .map(|e| e.row_key_id),
        );
        if let Held::Around { records, .. } = &self.held {
            assert_eq!(records, &want, "the records a write at {at:?} can touch");
        }
        self.assert_current(tx).await
    }

    /// Panic where it no longer matches the database. Debug builds check it
    /// after the writes made through it.
    #[cfg(debug_assertions)]
    pub(crate) async fn assert_current<DB: Store>(
        &self,
        tx: &mut sqlx::Transaction<'_, DB>,
    ) -> Result<()> {
        let mut fresh = Draft::load(tx, self.worktree_id).await?;
        let mut conflicts = DB::rows_in(tx, self.segment, None, true).await?;
        // what a draft around one write must hold, from the database rather
        // than from what it loaded: every row of its records and at their
        // places, and the entries they made or at the write's place
        if let Held::Around { place, records, .. } = &self.held {
            let mut places: BTreeSet<Place> = fresh
                .rows
                .iter()
                .chain(&conflicts)
                .filter(|r| records.contains(&r.key_id))
                .map(Place::from)
                .collect();
            places.insert(place.clone());
            let held =
                |r: &RecordRow| records.contains(&r.key_id) || places.contains(&Place::from(r));
            fresh.rows.retain(held);
            conflicts.retain(held);
            fresh
                .entries
                .retain(|e| records.contains(&e.key_id) || place.as_at() == e.at());
        }
        let same = self.rows.len() == fresh.rows.len()
            && self
                .rows
                .iter()
                .zip(&fresh.rows)
                .all(|(a, b)| same_row(a, b));
        assert!(
            same,
            "the draft's cached rows: {:?} != {:?}",
            self.rows, fresh.rows
        );
        let held = |entries: &[Superseded]| -> BTreeSet<(i64, i64)> {
            entries.iter().map(|e| (e.row, e.key_id)).collect()
        };
        assert_eq!(
            held(&self.entries),
            held(&fresh.entries),
            "the draft's cached entries"
        );
        if let Some(cached) = &self.conflicts {
            let ids = |rows: &[RecordRow]| -> BTreeSet<i64> { rows.iter().map(|r| r.id).collect() };
            assert_eq!(
                ids(cached),
                ids(&conflicts),
                "the draft's cached conflict rows"
            );
        }
        Ok(())
    }

    /// Its row at `at`, conflict rows apart.
    pub(crate) fn row_at(&self, at: At<'_>) -> Option<&RecordRow> {
        debug_assert!(self.held.place(at), "{at:?} is outside the draft loaded");
        self.rows.iter().find(|r| r.at() == at)
    }

    /// Its rows of record `id`, conflict rows apart.
    fn rows_of(&self, id: i64) -> impl Iterator<Item = &RecordRow> {
        debug_assert!(
            self.held.record(id),
            "record {id} is outside the draft loaded"
        );
        self.rows.iter().filter(move |r| r.key_id == id)
    }

    async fn insert<DB: Store>(
        &mut self,
        tx: &mut sqlx::Transaction<'_, DB>,
        row: NewRecordRow<'_>,
    ) -> Result<(i64, i64)> {
        let mut new = RecordRow {
            id: 0,
            key_id: 0,
            segment_id: self.segment,
            file_path: row.at.file_path.to_string(),
            path: row.at.path.to_string(),
            key: row.at.key.to_string(),
            commit_id: row.commit_id,
            json: row.json.clone(),
            deleted: row.deleted,
            version: row.version,
            base_commit_id: row.base_commit_id,
            base_json: row.base_json.cloned(),
            settled: row.settled.to_vec(),
            conflict: row.conflict,
        };
        let (id, key_id) = DB::insert(tx, self.segment, row).await?;
        (new.id, new.key_id) = (id, key_id);
        if let Held::Around {
            records, places, ..
        } = &mut self.held
        {
            records.insert(key_id);
            places.insert(Place::from(&new));
        }
        match (new.conflict, &mut self.conflicts) {
            (None, _) => {
                self.rows.push(new);
                self.rows
                    .sort_by(|a, b| (a.at(), a.id).cmp(&(b.at(), b.id)));
            }
            (Some(_), Some(conflicts)) => conflicts.push(new),
            // read with the rest when first needed
            (Some(_), None) => {}
        }
        Ok((id, key_id))
    }

    /// Delete row `id`, with the entries on it.
    async fn delete_row<DB: Store>(
        &mut self,
        tx: &mut sqlx::Transaction<'_, DB>,
        id: i64,
    ) -> Result<()> {
        DB::delete_row(tx, id).await?;
        self.rows.retain(|r| r.id != id);
        if let Some(conflicts) = &mut self.conflicts {
            conflicts.retain(|r| r.id != id);
        }
        self.entries.retain(|e| e.row != id);
        Ok(())
    }

    async fn entry<DB: Store>(
        &mut self,
        tx: &mut sqlx::Transaction<'_, DB>,
        entry: Superseded,
    ) -> Result<()> {
        DB::entry(tx, entry.row, self.segment, entry.key_id).await?;
        let held = |e: &Superseded| e.row == entry.row && e.key_id == entry.key_id;
        if !self.entries.iter().any(held) {
            self.entries.push(entry);
        }
        Ok(())
    }

    async fn untag<DB: Store>(
        &mut self,
        tx: &mut sqlx::Transaction<'_, DB>,
        row: i64,
        key_id: i64,
    ) -> Result<()> {
        DB::untag(tx, row, self.segment, key_id).await?;
        self.entries
            .retain(|e| !(e.row == row && e.key_id == key_id));
        Ok(())
    }

    /// Drop its conflict row at `at`.
    pub(crate) async fn drop_conflict<DB: Store>(
        &mut self,
        tx: &mut sqlx::Transaction<'_, DB>,
        at: At<'_>,
    ) -> Result<()> {
        debug_assert!(self.held.place(at), "{at:?} is outside the draft loaded");
        if self.conflicts.is_none() {
            self.conflicts = Some(DB::rows_in(tx, self.segment, None, true).await?);
        }
        let conflicts = self.conflicts.as_deref().unwrap_or_default();
        if let Some(id) = conflicts.iter().find(|r| r.at() == at).map(|r| r.id) {
            self.delete_row(tx, id).await?;
        }
        Ok(())
    }

    /// Its entries made by edit `old` become `new`'s, but at a key another
    /// row of `old` holds.
    async fn retag<DB: Store>(
        &mut self,
        tx: &mut sqlx::Transaction<'_, DB>,
        old: i64,
        new: i64,
    ) -> Result<()> {
        if old == new {
            return Ok(());
        }
        let others: Vec<At<'_>> = self.rows_of(old).map(RecordRow::at).collect();
        let moving: Vec<Superseded> = self
            .entries
            .iter()
            .filter(|e| e.key_id == old && !others.contains(&e.at()))
            .cloned()
            .collect();
        for e in moving {
            self.untag(tx, e.row, old).await?;
            self.entry(tx, Superseded { key_id: new, ..e }).await?;
        }
        Ok(())
    }

    /// The committed version an edit of `record` is made over, its commit
    /// and content: `earlier`'s base, an edit of it at this key or another,
    /// else its committed version at whatever key. None for a re-create,
    /// which the view shows live nowhere.
    async fn edit_base<DB: Store>(
        &self,
        tx: &mut sqlx::Transaction<'_, DB>,
        record: Option<i64>,
        earlier: Option<&RecordRow>,
    ) -> Result<(Option<CommitId>, Option<serde_json::Value>)> {
        let Some(id) = record else {
            return Ok((None, None));
        };
        if self.rows_of(id).any(|y| !y.deleted) {
            return Ok(earlier.map_or((None, None), |y| (y.base_commit_id, y.base_json.clone())));
        }
        let committed = DB::visible(tx, self.worktree_id, Scope::Chain, Filter::KeyId(id)).await?;
        let Some(live) = committed.into_iter().find(|r| !r.deleted) else {
            return Ok((None, None));
        };
        Ok(match earlier {
            Some(y) => (y.base_commit_id, y.base_json.clone()),
            None => (live.commit_id, Some(live.json)),
        })
    }

    /// Write a new version into the draft, made through its worktree's own
    /// view (§4.2, C.9). `record` is the `key_id` it takes, `None` for a
    /// new record.
    pub(crate) async fn write<DB: Store>(
        &mut self,
        tx: &mut sqlx::Transaction<'_, DB>,
        at: At<'_>,
        value: Value<'_>,
        origin: Origin,
        record: Option<i64>,
        version: i64,
    ) -> Result<Written> {
        let (w, d) = (self.worktree_id, self.segment);
        let edit = matches!(origin, Origin::Edit);
        // at most one edit per record: one at another key is replaced by
        // this one, which keeps its base
        let other = record.and_then(|id| self.rows_of(id).find(|y| y.at() != at && edit).cloned());
        let prior = self.row_at(at).cloned();
        // before `other` goes: it may be the record's live row
        let (base, base_json) = match edit {
            true => {
                self.edit_base(tx, record, prior.as_ref().or(other.as_ref()))
                    .await?
            }
            false => (None, None),
        };
        if let (Some(y), Some(id)) = (&other, record) {
            replace_other(tx, self, y, id).await?;
        }
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
        // read before the prior row goes: its entries go with it
        let entries = other_entries(tx, &seen.iter().collect::<Vec<_>>(), w).await?;
        if let Some(p) = &prior {
            seen.retain(|r| r.id != p.id);
            self.delete_row(tx, p.id).await?;
        }
        let settled: Vec<i64> = if edit {
            settled.into_iter().collect()
        } else {
            Vec::new()
        };
        let (id, key_id) = self
            .insert(
                tx,
                NewRecordRow {
                    at,
                    key_id: record,
                    commit_id: match origin {
                        Origin::Edit => None,
                        Origin::File(c) => c,
                    },
                    json,
                    deleted,
                    version,
                    base_commit_id: base,
                    base_json: base_json.as_ref(),
                    settled: &settled,
                    conflict: None,
                },
            )
            .await?;
        // the edit it replaces made entries this one inherits, a new
        // record's too, whose id is known only now
        if let Some(p) = &prior {
            self.retag(tx, p.key_id, key_id).await?;
        }
        hide(tx, self, at, key_id, &seen).await?;
        take_over(tx, &entries, id, (!deleted).then_some(json)).await?;
        Ok(Written { id, key_id })
    }
}

/// Remove draft `d`'s edit `y` of record `id`, which a write of it at
/// another key replaces.
async fn replace_other<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    draft: &mut Draft,
    y: &RecordRow,
    id: i64,
) -> Result<()> {
    let w = draft.worktree_id;
    debug_assert!(
        draft.held.record(id),
        "record {id} is outside the draft loaded"
    );
    draft.drop_conflict(tx, y.at()).await?;
    draft.delete_row(tx, y.id).await?;
    // its entries stay with this edit, but its own tree's row of
    // another record shows again at the key it leaves
    let chain: BTreeSet<i64> = DB::visible(tx, w, Scope::Chain, Filter::All)
        .await?
        .iter()
        .map(|r| r.segment_id)
        .collect();
    let left: Vec<i64> = draft
        .entries
        .iter()
        .filter(|e| e.key_id == id && e.at() == y.at() && e.row_key_id != id)
        .map(|e| e.row)
        .collect();
    for row in left {
        let seg = row_segment(tx, w, row).await?;
        if seg.is_some_and(|s| chain.contains(&s)) {
            draft.untag(tx, row, id).await?;
        }
    }
    Ok(())
}

/// The rows a write at `at` was made over: those `shown` there, and the
/// same record's rows elsewhere with content seen here, a move's copy of
/// what the edit was made over (§3.5).
async fn seen_rows<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    at: At<'_>,
    shown: &[RecordRow],
    record: Option<i64>,
) -> Result<Vec<RecordRow>> {
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
    draft: &mut Draft,
    at: At<'_>,
    key_id: i64,
    seen: &[RecordRow],
) -> Result<()> {
    debug_assert!(draft.held.place(at), "{at:?} is outside the draft loaded");
    let elsewhere: BTreeSet<i64> = draft
        .rows
        .iter()
        .filter(|r| r.at() != at)
        .map(|r| r.key_id)
        .collect();
    let here = draft.entries.iter().filter(|e| e.at() == at);
    debug_assert!(
        here.clone().all(|e| draft.held.record(e.row_key_id)),
        "a record hidden at {at:?} is outside the draft loaded"
    );
    let hidden: Vec<Superseded> = here
        .filter(|e| e.deleted || !elsewhere.contains(&e.row_key_id))
        .cloned()
        .collect();
    for r in seen {
        draft.entry(tx, entry_on(r, key_id)).await?;
    }
    for e in hidden {
        draft.entry(tx, Superseded { key_id, ..e }).await?;
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

/// [`Draft::retag`] in worktree `w`'s draft, loaded for it.
pub(crate) async fn retag<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    old: i64,
    new: i64,
) -> Result<()> {
    let mut draft = Draft::load(tx, w).await?;
    draft.retag(tx, old, new).await?;
    #[cfg(debug_assertions)]
    draft.assert_current(tx).await?;
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
) -> Result<Option<RecordRow>> {
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
    commit_id: Option<CommitId>,
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
        NewRecordRow {
            conflict: Some(ConflictState::Conflict),
            ..NewRecordRow::new(at, record, commit_id, json, deleted, version)
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
/// scan with the entries it takes, and the ids other keys hold, which it
/// may not take.
pub(crate) struct KeyIds {
    pub(crate) named: Option<i64>,
    pub(crate) moved: Option<(i64, Vec<OtherEntry>)>,
    pub(crate) elsewhere: BTreeSet<i64>,
}

/// Another worktree's draft entry on a row: the draft, its tag, and the
/// content of the row, `None` for a tombstone.
pub(crate) struct OtherEntry {
    content: Option<serde_json::Value>,
    draft: i64,
    tag: i64,
}

impl OtherEntry {
    pub(crate) fn tag(&self) -> i64 {
        self.tag
    }
}

/// The entries other worktrees' drafts than `w`'s hold on `rows`.
pub(crate) async fn other_entries<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    rows: &[&RecordRow],
    w: i64,
) -> Result<Vec<OtherEntry>> {
    let mut out = Vec::new();
    for r in rows {
        for (draft, tag) in DB::draft_entries_on(tx, r.id, w).await? {
            let content = (!r.deleted).then(|| r.json.clone());
            out.push(OtherEntry {
                content,
                draft,
                tag,
            });
        }
    }
    Ok(out)
}

/// §3.3: row `to` replaces rows with these entries, and takes over those
/// made over the content it holds, `None` for a tombstone.
pub(crate) async fn take_over<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    entries: &[OtherEntry],
    to: i64,
    content: Option<&serde_json::Value>,
) -> Result<()> {
    for e in entries.iter().filter(|e| e.content.as_ref() == content) {
        DB::entry(tx, to, e.draft, e.tag).await?;
    }
    Ok(())
}

/// §4.9: main's new row `to`, record `id` at `at` with content `json`, is
/// a version a merge may bring back: other drafts' entries on its earlier
/// rows here, made by an edit at this place or of the record, pass to it,
/// and a draft's edit here or of the record made over `json` hides it,
/// though the row it saw may be gone.
async fn brought_back<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    family: i64,
    w: i64,
    at: At<'_>,
    to: i64,
    id: i64,
    json: &serde_json::Value,
) -> Result<()> {
    let rows = DB::rows_at_or_of(tx, family, at, id).await?;
    let earlier: Vec<&RecordRow> = rows
        .iter()
        .map(|(r, _)| r)
        .filter(|r| r.at() == at && r.id != to && !r.deleted && &r.json == json)
        .collect();
    let mut entries = Vec::new();
    for e in other_entries(tx, &earlier, w).await? {
        let edited_here = e.tag == id
            || DB::rows_in(tx, e.draft, Some(at.file_path), false)
                .await?
                .iter()
                .any(|y| y.key_id == e.tag && y.at() == at);
        if edited_here {
            entries.push(e);
        }
    }
    take_over(tx, &entries, to, Some(json)).await?;
    let made_over = rows.iter().filter(|(r, draft_of)| {
        draft_of.is_some_and(|d| d != w) && r.base_json.as_ref() == Some(json)
    });
    for (r, _) in made_over {
        DB::entry(tx, to, r.segment_id, r.key_id).await?;
    }
    Ok(())
}

/// One record whose committed value changed: bring worktree `w`'s head
/// up to it (§4.3, C.10). `change: None` means the commit deleted it.
/// Returns the row it added, if any.
pub(crate) async fn scan_key<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    w: i64,
    at: At<'_>,
    change: Option<&serde_json::Value>,
    commit: CommitId,
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
    let below: Vec<RecordRow> = match &head_row {
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
                .or(ids
                    .moved
                    .as_ref()
                    .map(|(id, _)| *id)
                    .filter(|id| !ids.elsewhere.contains(id)))
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
        NewRecordRow::new(at, key_id, Some(commit), &json, deleted, version),
    )
    .await?;
    for r in below {
        DB::entry(tx, r.id, h, key_id).await?;
    }
    let content = (!deleted).then_some(&json);
    if !deleted && segs.family == w {
        brought_back(tx, segs.family, w, at, id, key_id, &json).await?;
    }
    // only the row that keeps the moved record's id is it (§3.5)
    if let Some((moved_id, moved)) = &ids.moved {
        if key_id == *moved_id {
            take_over(tx, moved, id, content).await?;
        }
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
    x: &RecordRow,
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
    x: &RecordRow,
) -> Result<i64> {
    let old = x.key_id;
    // A key_id is its first row's id, so a new record needs a new row.
    DB::delete_row(tx, x.id).await?;
    let (_, fresh) = DB::insert(
        tx,
        d,
        NewRecordRow {
            key_id: None,
            conflict: None,
            ..NewRecordRow::from(x)
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
) -> Result<Vec<RecordRow>> {
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
) -> Result<Vec<RecordRow>> {
    let d = DB::segs(tx, w).await?.draft;
    let chain = DB::visible(tx, w, Scope::Chain, Filter::All).await?;
    let mut live_at: BTreeMap<At<'_>, &RecordRow> = BTreeMap::new();
    for r in chain.iter().filter(|r| !r.deleted) {
        live_at.entry(r.at()).or_insert(r);
    }
    let live = |x: &RecordRow| live_at.get(&x.at()).copied();
    let changes = |x: &RecordRow| match live(x) {
        Some(c) => x.deleted || c.json != x.json,
        None => !x.deleted,
    };
    let draft = DB::rows_in(tx, d, None, false).await?;
    let mut drafted_at: BTreeMap<At<'_>, &RecordRow> = BTreeMap::new();
    for y in &draft {
        drafted_at.entry(y.at()).or_insert(y);
    }
    let mut rows: Vec<RecordRow> = Vec::new();
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
        let drafted = drafted_at.get(&c.at()).copied();
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
    commit: CommitId,
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
        // unchanged: the record the chain shows already holds it, so it
        // isn't folded into the head as a copy, which an edit made later
        // elsewhere, hiding the row it shares, would leave showing
        let shown = DB::visible(tx, w, Scope::Chain, Filter::At(x.at())).await?;
        let unchanged = shown
            .iter()
            .find(|c| !c.deleted)
            .is_some_and(|c| !x.deleted && c.json == x.json && c.key_id == x.key_id);
        if unchanged {
            remove_draft_row(tx, d, x.at()).await?;
            continue;
        }
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
