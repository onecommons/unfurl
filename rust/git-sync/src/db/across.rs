//! A read across worktrees by git's rule (branch-segments.md §4.1): each
//! covered worktree's own view, less a live version unchanged since its
//! merge base with another the read covers, which has changed it since.

use std::collections::{BTreeSet, HashMap, HashSet};

use crate::db::store::{Records, Store};
use crate::db::tables::{AcrossRow, AcrossRows, CoveredWorktree, SegmentGraph};
use crate::error::Result;
use crate::model::Record;

/// Sets of covered worktrees, by their index in [`Covered`], `words` bit
/// words each, one after another.
struct Masks {
    words: usize,
    bits: Vec<u64>,
}

impl Masks {
    fn new(words: usize, count: usize) -> Self {
        Masks {
            words,
            bits: vec![0; words * count],
        }
    }

    fn get(&self, i: usize) -> &[u64] {
        &self.bits[i * self.words..(i + 1) * self.words]
    }

    fn get_mut(&mut self, i: usize) -> &mut [u64] {
        &mut self.bits[i * self.words..(i + 1) * self.words]
    }
}

fn has(mask: &[u64], w: usize) -> bool {
    mask[w / 64] & (1 << (w % 64)) != 0
}

fn set(mask: &mut [u64], w: usize) {
    mask[w / 64] |= 1 << (w % 64);
}

fn ones(mask: &[u64]) -> impl Iterator<Item = usize> + '_ {
    mask.iter().enumerate().flat_map(|(i, &word)| {
        (0..64)
            .filter(move |b| word & (1 << b) != 0)
            .map(move |b| i * 64 + b)
    })
}

/// Past how many records [`Covered::fetch`] reads every row in the covered
/// segments rather than those at the records' places.
const EVERY_ROW_PAST: usize = 300;

#[cfg(feature = "fault-injection")]
thread_local! {
    static EVERY_ROW: std::cell::Cell<usize> = const { std::cell::Cell::new(EVERY_ROW_PAST) };
}

#[cfg(feature = "fault-injection")]
fn every_row_past() -> usize {
    EVERY_ROW.get()
}

#[cfg(not(feature = "fault-injection"))]
fn every_row_past() -> usize {
    EVERY_ROW_PAST
}

/// Run `f` with every read across worktrees on this thread reading every
/// row in the covered segments, as one past 300 records does.
#[cfg(feature = "fault-injection")]
pub fn reading_every_row<T>(f: impl FnOnce() -> T) -> T {
    let before = EVERY_ROW.replace(0);
    let out = f();
    EVERY_ROW.set(before);
    out
}

/// A family's committed segments: their parents and the merges in them.
struct Graph {
    parent: HashMap<i64, i64>,
    merged: HashMap<i64, Vec<i64>>,
    history: HashMap<i64, HashSet<i64>>,
}

impl Graph {
    fn new(SegmentGraph { segments, merges }: SegmentGraph) -> Self {
        let mut merged: HashMap<i64, Vec<i64>> = HashMap::new();
        for (from, to) in merges {
            merged.entry(from).or_default().push(to);
        }
        Graph {
            parent: segments
                .into_iter()
                .filter_map(|(s, p)| p.map(|p| (s, p)))
                .collect(),
            merged,
            history: HashMap::new(),
        }
    }

    /// The segments `s`'s history holds, through parents and merges, with
    /// `s`.
    fn history(&mut self, s: i64) -> &HashSet<i64> {
        if !self.history.contains_key(&s) {
            let mut out = HashSet::from([s]);
            let mut todo = vec![s];
            while let Some(x) = todo.pop() {
                let below = self
                    .parent
                    .get(&x)
                    .into_iter()
                    .chain(self.merged.get(&x).into_iter().flatten());
                for &y in below {
                    if out.insert(y) {
                        todo.push(y);
                    }
                }
            }
            self.history.insert(s, out);
        }
        &self.history[&s]
    }

    /// The latest segments the histories of `a` and `b` share.
    fn merge_bases(&mut self, a: i64, b: i64) -> Vec<i64> {
        let ha = self.history(a).clone();
        let common: Vec<i64> = self
            .history(b)
            .iter()
            .copied()
            .filter(|s| ha.contains(s))
            .collect();
        let mut below: HashSet<i64> = HashSet::new();
        for &x in &common {
            // a history holds every history below it
            if below.contains(&x) {
                continue;
            }
            below.extend(self.history(x).iter().copied().filter(|&y| y != x));
        }
        common.into_iter().filter(|s| !below.contains(s)).collect()
    }

    /// The committed state at segment `s`: it and its parents.
    fn view(&self, s: i64) -> HashSet<i64> {
        let mut out = HashSet::from([s]);
        let mut at = s;
        while let Some(&p) = self.parent.get(&at) {
            out.insert(p);
            at = p;
        }
        out
    }
}

/// Each two covered worktrees' merge bases, by number.
struct Bases {
    /// Worktrees `w` and `u`'s, at `w * n + u`, as an index into `classes`.
    pair: Vec<Option<usize>>,
    /// Each set of merge bases some two worktrees have.
    classes: Vec<Vec<usize>>,
    /// Each merge base's state: its segment and its parents.
    views: Vec<HashSet<i64>>,
    /// The words of a set of merge bases.
    words: usize,
    /// The merge bases whose states hold each segment.
    holding: HashMap<i64, Vec<u64>>,
}

impl Bases {
    fn new(graph: &mut Graph, worktrees: &[CoveredWorktree]) -> Self {
        let n = worktrees.len();
        let mut pair = vec![None; n * n];
        let mut classes: Vec<Vec<usize>> = Vec::new();
        let mut class_of: HashMap<Vec<usize>, usize> = HashMap::new();
        let mut base_of: HashMap<i64, usize> = HashMap::new();
        let mut views: Vec<HashSet<i64>> = Vec::new();
        for (i, w) in worktrees.iter().enumerate() {
            for (j, u) in worktrees.iter().enumerate().skip(i + 1) {
                if w.family != u.family {
                    continue;
                }
                let mut found: Vec<usize> = Vec::new();
                for s in graph.merge_bases(w.head, u.head) {
                    let b = *base_of.entry(s).or_insert_with(|| {
                        views.push(graph.view(s));
                        views.len() - 1
                    });
                    found.push(b);
                }
                if found.is_empty() {
                    continue;
                }
                found.sort_unstable();
                let c = *class_of.entry(found).or_insert_with_key(|f| {
                    classes.push(f.clone());
                    classes.len() - 1
                });
                (pair[i * n + j], pair[j * n + i]) = (Some(c), Some(c));
            }
        }
        let words = views.len().div_ceil(64).max(1);
        let mut holding: HashMap<i64, Vec<u64>> = HashMap::new();
        for (b, view) in views.iter().enumerate() {
            for &s in view {
                set(holding.entry(s).or_insert_with(|| vec![0; words]), b);
            }
        }
        Bases {
            pair,
            classes,
            views,
            words,
            holding,
        }
    }
}

/// The worktrees a read covers, the views holding each segment, and each
/// two's merge bases.
pub(crate) struct Covered {
    worktrees: Vec<CoveredWorktree>,
    words: usize,
    holds: HashMap<i64, Vec<u64>>,
    bases: Bases,
}

impl Covered {
    /// The worktrees `DB::covered` finds for these, and their merge bases.
    pub(crate) async fn load<DB: Store>(
        tx: &mut sqlx::Transaction<'_, DB>,
        id: Option<i64>,
        origin: Option<&str>,
        branch: Option<&str>,
        ids: &str,
    ) -> Result<Self> {
        let mut worktrees: Vec<CoveredWorktree> = Vec::new();
        let mut chains: Vec<(usize, i64)> = Vec::new();
        // a row per segment of each worktree's chain, by worktree
        for row in DB::covered(tx, id, origin, branch, ids).await? {
            if worktrees.last().is_none_or(|w| w.id != row.worktree.id) {
                worktrees.push(row.worktree);
            }
            if let Some(s) = row.segment_id {
                chains.push((worktrees.len() - 1, s));
            }
        }
        let words = worktrees.len().div_ceil(64).max(1);
        let drafts = worktrees.iter().enumerate().map(|(i, w)| (i, w.draft));
        let mut holds: HashMap<i64, Vec<u64>> = HashMap::new();
        for (i, s) in chains.into_iter().chain(drafts) {
            set(holds.entry(s).or_insert_with(|| vec![0; words]), i);
        }
        let families: BTreeSet<i64> = worktrees.iter().map(|w| w.family).collect();
        let families: Vec<i64> = families.into_iter().collect();
        let mut graph = Graph::new(DB::segment_graph(tx, &families).await?);
        let bases = Bases::new(&mut graph, &worktrees);
        Ok(Covered {
            worktrees,
            words,
            holds,
            bases,
        })
    }

    /// Whether the read covers more than one worktree.
    pub(crate) fn several(&self) -> bool {
        self.worktrees.len() > 1
    }

    /// The covered views' segments.
    pub(crate) fn view_segments(&self) -> Vec<i64> {
        let all: BTreeSet<i64> = self.holds.keys().copied().collect();
        all.into_iter().collect()
    }

    /// The segments the rule reads rows from: the covered views' and the
    /// merge bases'.
    fn segments(&self) -> Vec<i64> {
        let all: BTreeSet<i64> = self
            .holds
            .keys()
            .copied()
            .chain(self.bases.views.iter().flatten().copied())
            .collect();
        all.into_iter().collect()
    }

    /// The rows the rule compares for `records`: those in
    /// [`Self::segments`] at every place the records have a row there,
    /// with the entries on them; past `count` of them, every row there.
    async fn fetch<DB: Store>(
        &self,
        tx: &mut sqlx::Transaction<'_, DB>,
        records: Records<'_>,
        count: usize,
    ) -> Result<Rows> {
        // past a few hundred, reading every row is cheaper than their places
        if count > every_row_past() {
            return self.fetch_every(tx).await;
        }
        Ok(with_entries(
            DB::across_rows(tx, &self.segments(), records).await?,
        ))
    }

    /// Every row in [`Self::segments`], with the entries in them.
    async fn fetch_every<DB: Store>(&self, tx: &mut sqlx::Transaction<'_, DB>) -> Result<Rows> {
        let segments = self.segments();
        let rows = DB::across_rows_in(tx, &segments).await?;
        // those in other segments neither view nor base holds
        let entries = DB::entries_in_segments(tx, &segments).await?;
        Ok(with_entries(AcrossRows { rows, entries }))
    }

    /// `records` less the copies the rule leaves out: a record is the row
    /// its worktree's view shows at its place.
    pub(crate) async fn drop_copies<DB: Store>(
        &self,
        tx: &mut sqlx::Transaction<'_, DB>,
        records: Vec<Record>,
    ) -> Result<Vec<Record>> {
        let mut ids: Vec<i64> = records.iter().map(|r| r.id).collect();
        ids.sort_unstable();
        ids.dedup();
        let rows = self.fetch(tx, Records::Ids(&ids), ids.len()).await?;
        let resolved = Resolve::new(self, &rows).resolve();
        let index: HashMap<i64, usize> = self
            .worktrees
            .iter()
            .enumerate()
            .map(|(i, w)| (w.id, i))
            .collect();
        Ok(records
            .into_iter()
            .filter(|rec| {
                if rec.deleted || rec.conflict.is_some() {
                    return true;
                }
                let Some(&w) = index.get(&rec.worktree_id) else {
                    return true;
                };
                let p = (rec.file_path.as_str(), rec.path.as_str(), rec.key.as_str());
                !resolved.dropped_at(w, p)
            })
            .collect())
    }

    /// Those of the rows `candidates` some covered view shows and the rule
    /// keeps.
    pub(crate) async fn kept<DB: Store>(
        &self,
        tx: &mut sqlx::Transaction<'_, DB>,
        candidates: Vec<i64>,
    ) -> Result<Vec<i64>> {
        if candidates.is_empty() {
            return Ok(Vec::new());
        }
        let rows = self
            .fetch(tx, Records::OfRows(&candidates), candidates.len())
            .await?;
        let resolved = Resolve::new(self, &rows).resolve();
        let by_id: HashMap<i64, usize> = rows
            .rows
            .iter()
            .enumerate()
            .map(|(i, r)| (r.id, i))
            .collect();
        Ok(candidates
            .into_iter()
            .filter(|id| by_id.get(id).is_some_and(|&i| resolved.kept(i)))
            .collect())
    }
}

/// `rows` with their entries by row.
fn with_entries(AcrossRows { rows, entries }: AcrossRows) -> Rows {
    let mut by_row: HashMap<i64, Vec<i64>> = HashMap::new();
    for (row, seg) in entries {
        by_row.entry(row).or_default().push(seg);
    }
    Rows {
        rows,
        entries: by_row,
    }
}

/// What [`Covered::fetch`] read: rows, and the entries on them by row.
pub(crate) struct Rows {
    rows: Vec<AcrossRow>,
    entries: HashMap<i64, Vec<i64>>,
}

type Place<'a> = (&'a str, &'a str, &'a str);

/// One resolution's indexes: rows by place and by record, and the views
/// and the merge bases' states showing each row.
struct Resolve<'a> {
    covered: &'a Covered,
    rows: &'a Rows,
    /// Each place's rows.
    at: Vec<Vec<usize>>,
    /// Each row's place, an index into `at`.
    place_of: Vec<usize>,
    places: HashMap<Place<'a>, usize>,
    by_record: HashMap<i64, Vec<usize>>,
    shown: Masks,
    based: Masks,
}

/// A resolution: the views showing each row, and those leaving it out.
struct Resolved<'a> {
    resolve: Resolve<'a>,
    dropped: Masks,
}

impl Resolved<'_> {
    /// Whether some view shows row `i` and keeps it.
    fn kept(&self, i: usize) -> bool {
        let (shown, dropped) = (self.resolve.shown.get(i), self.dropped.get(i));
        shown.iter().zip(dropped).any(|(s, d)| s & !d != 0)
    }

    /// Whether view `w` leaves out the live row it shows at `p`.
    fn dropped_at(&self, w: usize, p: Place<'_>) -> bool {
        self.resolve
            .places
            .get(&p)
            .and_then(|&q| self.resolve.shows(w, q))
            .is_some_and(|i| has(self.dropped.get(i), w))
    }
}

/// Set `mask` to what `holding` holds `segment` for, less what it holds
/// each of `entries`' segments for.
fn hold(mask: &mut [u64], holding: &HashMap<i64, Vec<u64>>, segment: i64, entries: &[i64]) {
    let Some(holds) = holding.get(&segment) else {
        return;
    };
    mask.copy_from_slice(holds);
    for s in entries {
        if let Some(gone) = holding.get(s) {
            mask.iter_mut().zip(gone).for_each(|(m, g)| *m &= !g);
        }
    }
}

/// One live row at place `q` while it's resolved: what each view shows
/// there, and whether each class of merge bases has its content there.
struct AtRow<'s> {
    q: usize,
    shows: &'s [Option<usize>],
    here: Vec<Option<bool>>,
    /// Whether its record has a row at another place.
    elsewhere: bool,
}

impl<'a> Resolve<'a> {
    fn new(covered: &'a Covered, rows: &'a Rows) -> Self {
        let count = rows.rows.len();
        let mut at: Vec<Vec<usize>> = Vec::new();
        let mut places: HashMap<Place<'a>, usize> = HashMap::new();
        let mut place_of = Vec::with_capacity(count);
        let mut by_record: HashMap<i64, Vec<usize>> = HashMap::new();
        let mut shown = Masks::new(covered.words, count);
        let mut based = Masks::new(covered.bases.words, count);
        for (i, r) in rows.rows.iter().enumerate() {
            let q = *places
                .entry((&r.file_path, &r.path, &r.key))
                .or_insert_with(|| {
                    at.push(Vec::new());
                    at.len() - 1
                });
            at[q].push(i);
            place_of.push(q);
            by_record.entry(r.key_id).or_default().push(i);
            let entries = rows.entries.get(&r.id).map_or(&[][..], Vec::as_slice);
            hold(shown.get_mut(i), &covered.holds, r.segment_id, entries);
            hold(
                based.get_mut(i),
                &covered.bases.holding,
                r.segment_id,
                entries,
            );
        }
        Resolve {
            covered,
            rows,
            at,
            place_of,
            places,
            by_record,
            shown,
            based,
        }
    }

    fn row(&self, i: usize) -> &'a AcrossRow {
        &self.rows.rows[i]
    }

    /// Whether rows `a` and `b` hold the same content.
    fn same(&self, a: usize, b: usize) -> bool {
        let (x, y) = (self.row(a).content_hash, self.row(b).content_hash);
        debug_assert!(x.is_some() && y.is_some(), "rows {a} and {b} have no hash");
        a == b || (x.is_some() && x == y)
    }

    /// The live row view `u` shows at place `q`.
    fn shows(&self, u: usize, q: usize) -> Option<usize> {
        self.at[q]
            .iter()
            .copied()
            .find(|&i| !self.row(i).deleted && has(self.shown.get(i), u))
    }

    /// The live row the state at merge base `b` has at place `q`.
    fn base_at(&self, b: usize, q: usize) -> Option<usize> {
        self.at[q]
            .iter()
            .copied()
            .find(|&i| !self.row(i).deleted && has(self.based.get(i), b))
    }

    /// Each live row's views that leave it out.
    fn resolve(self) -> Resolved<'a> {
        let n = self.covered.worktrees.len();
        let mut dropped = Masks::new(self.covered.words, self.rows.rows.len());
        for (q, here) in self.at.iter().enumerate() {
            let live: Vec<usize> = here
                .iter()
                .copied()
                .filter(|&i| !self.row(i).deleted)
                .collect();
            // every view shows the one row: nothing to leave out
            if live
                .iter()
                .any(|&i| (0..n).all(|w| has(self.shown.get(i), w)))
            {
                continue;
            }
            let shows: Vec<Option<usize>> = (0..n)
                .map(|u| live.iter().copied().find(|&i| has(self.shown.get(i), u)))
                .collect();
            for &i in &live {
                let mut row = AtRow {
                    q,
                    shows: &shows,
                    here: vec![None; self.covered.bases.classes.len()],
                    elsewhere: self.by_record[&self.row(i).key_id]
                        .iter()
                        .any(|&o| self.place_of[o] != q),
                };
                for w in ones(self.shown.get(i)) {
                    if self.unchanged_copy(i, w, &mut row) {
                        set(dropped.get_mut(i), w);
                    }
                }
            }
        }
        Resolved {
            resolve: self,
            dropped,
        }
    }

    /// Whether row `i`, which view `w` shows at `row.q`, is unchanged since
    /// `w`'s merge base with another covered worktree which has changed it
    /// since.
    fn unchanged_copy(&self, i: usize, w: usize, row: &mut AtRow<'_>) -> bool {
        let n = row.shows.len();
        (0..n).any(|u| {
            let Some(c) = self.covered.bases.pair[w * n + u] else {
                return false;
            };
            if row.shows[u].is_some_and(|x| self.same(x, i)) {
                return false;
            }
            // the bases had it here, and the other has changed it here
            let here = *row.here[c].get_or_insert_with(|| {
                self.covered.bases.classes[c]
                    .iter()
                    .all(|&b| self.base_at(b, row.q).is_some_and(|x| self.same(x, i)))
            });
            here || (row.elsewhere && self.moved(i, u, c, row.q))
        })
    }

    /// Whether row `i` moved to place `q` since every merge base of class
    /// `c`, and `u` has modified it where the base had it, a live value: a
    /// rename and a deletion keep both.
    fn moved(&self, i: usize, u: usize, c: usize, q: usize) -> bool {
        let others: Vec<usize> = self.by_record[&self.row(i).key_id]
            .iter()
            .map(|&o| self.place_of[o])
            .filter(|&r| r != q)
            .collect();
        self.covered.bases.classes[c].iter().all(|&b| {
            !self.base_at(b, q).is_some_and(|x| self.same(x, i))
                && others.iter().any(|&r| {
                    self.base_at(b, r).is_some_and(|x| self.same(x, i))
                        && self.shows(u, r).is_some_and(|y| !self.same(y, i))
                })
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_mask_holds_worktrees_past_the_first_word() {
        let mut masks = Masks::new(2, 3);
        for w in [0, 63, 64, 127] {
            set(masks.get_mut(1), w);
        }
        assert_eq!(ones(masks.get(1)).collect::<Vec<_>>(), [0, 63, 64, 127]);
        assert!(has(masks.get(1), 64) && !has(masks.get(1), 65));
        assert_eq!(ones(masks.get(0)).chain(ones(masks.get(2))).count(), 0);
    }
}
