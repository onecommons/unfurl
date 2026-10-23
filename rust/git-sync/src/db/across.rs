//! A read across worktrees by git's rule (branch-segments.md §4.1): each
//! covered worktree's own view, less a live version unchanged since its
//! merge base with another the read covers, which has changed it since.

use std::borrow::Cow;
use std::collections::{BTreeSet, HashMap, HashSet};

use crate::db::store::Store;
use crate::db::tables::{AcrossRow, CoveredWorktree, SegmentGraph};
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

/// Past how many places [`Covered::fetch`] reads every row in the covered
/// segments rather than those at each place.
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
/// row in the covered segments, as one past 300 places does.
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

/// The worktrees a read covers, the views holding each segment, and each
/// two's merge bases as the views of the segments ending at them.
pub(crate) struct Covered {
    worktrees: Vec<CoveredWorktree>,
    words: usize,
    holds: HashMap<i64, Vec<u64>>,
    bases: HashMap<(usize, usize), Vec<i64>>,
    base_views: HashMap<i64, HashSet<i64>>,
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
        let worktrees = DB::covered(tx, id, origin, branch, ids).await?;
        let words = worktrees.len().div_ceil(64).max(1);
        let index: HashMap<i64, usize> = worktrees
            .iter()
            .enumerate()
            .map(|(i, w)| (w.id, i))
            .collect();
        let ids: Vec<i64> = worktrees.iter().map(|w| w.id).collect();
        let chains: Vec<(usize, i64)> = DB::chains(tx, &ids)
            .await?
            .into_iter()
            .map(|(w, s)| (index[&w], s))
            .collect();
        let drafts = worktrees.iter().enumerate().map(|(i, w)| (i, w.draft));
        let mut holds: HashMap<i64, Vec<u64>> = HashMap::new();
        for (i, s) in chains.into_iter().chain(drafts) {
            set(holds.entry(s).or_insert_with(|| vec![0; words]), i);
        }
        let families: BTreeSet<i64> = worktrees.iter().map(|w| w.family).collect();
        let families: Vec<i64> = families.into_iter().collect();
        let mut graph = Graph::new(DB::segment_graph(tx, &families).await?);
        let mut bases = HashMap::new();
        let mut base_views = HashMap::new();
        for (i, w) in worktrees.iter().enumerate() {
            for (j, u) in worktrees.iter().enumerate().skip(i + 1) {
                if w.family != u.family {
                    continue;
                }
                let found = graph.merge_bases(w.head, u.head);
                for &s in &found {
                    base_views.entry(s).or_insert_with(|| graph.view(s));
                }
                if !found.is_empty() {
                    bases.insert((j, i), found.clone());
                    bases.insert((i, j), found);
                }
            }
        }
        Ok(Covered {
            worktrees,
            words,
            holds,
            bases,
            base_views,
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
            .chain(self.base_views.values().flatten().copied())
            .collect();
        all.into_iter().collect()
    }

    /// The rows the rule compares for these places and records: those at
    /// them in [`Self::segments`], and those at the places the records'
    /// other rows are, with the entries on them.
    pub(crate) async fn fetch<DB: Store>(
        &self,
        tx: &mut sqlx::Transaction<'_, DB>,
        places: Vec<(String, String, String)>,
        key_ids: &[i64],
    ) -> Result<Rows> {
        let segments = self.segments();
        // past a few hundred places, reading every row is cheaper
        if places.len() > every_row_past() {
            let rows = DB::across_rows_in(tx, &segments).await?;
            return with_entries(tx, rows).await;
        }
        let mut rows = DB::across_rows(tx, &segments, &places, key_ids).await?;
        let known: HashSet<(String, String, String)> = places.into_iter().collect();
        let more: BTreeSet<(String, String, String)> = rows
            .iter()
            .map(place)
            .filter(|p| !known.contains(p))
            .collect();
        if !more.is_empty() {
            let more: Vec<(String, String, String)> = more.into_iter().collect();
            rows.extend(DB::across_rows(tx, &segments, &more, &[]).await?);
            rows.sort_unstable_by_key(|r| r.id);
            rows.dedup_by_key(|r| r.id);
        }
        with_entries(tx, rows).await
    }

    /// `records` less the copies the rule leaves out, `rows` being what
    /// [`Self::fetch`] read for them: a record is the row its worktree's
    /// view shows at its place.
    pub(crate) fn drop_copies(&self, rows: &Rows, records: Vec<Record>) -> Vec<Record> {
        let resolved = Resolve::new(self, rows).resolve();
        let index: HashMap<i64, usize> = self
            .worktrees
            .iter()
            .enumerate()
            .map(|(i, w)| (w.id, i))
            .collect();
        records
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
            .collect()
    }

    /// The ids of `candidates` some covered view shows and keeps.
    pub(crate) fn kept(&self, rows: &Rows, candidates: &[i64]) -> Vec<i64> {
        let resolved = Resolve::new(self, rows).resolve();
        let by_id: HashMap<i64, usize> = rows
            .rows
            .iter()
            .enumerate()
            .map(|(i, r)| (r.id, i))
            .collect();
        candidates
            .iter()
            .copied()
            .filter(|id| by_id.get(id).is_some_and(|&i| resolved.kept(i)))
            .collect()
    }
}

/// `rows` with the entries on them.
async fn with_entries<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    rows: Vec<AcrossRow>,
) -> Result<Rows> {
    let ids: Vec<i64> = rows.iter().map(|r| r.id).collect();
    let mut entries: HashMap<i64, Vec<i64>> = HashMap::new();
    for (row, seg) in DB::entries_on_rows(tx, &ids).await? {
        entries.entry(row).or_default().push(seg);
    }
    Ok(Rows {
        rows,
        entries,
        canonical: DB::POSTGRES,
    })
}

/// What [`Covered::fetch`] read: rows, the entries on them by row, and
/// whether their content compares as it is.
pub(crate) struct Rows {
    rows: Vec<AcrossRow>,
    entries: HashMap<i64, Vec<i64>>,
    canonical: bool,
}

fn place(r: &AcrossRow) -> (String, String, String) {
    (r.file_path.clone(), r.path.clone(), r.key.clone())
}

type Place<'a> = (&'a str, &'a str, &'a str);

/// One resolution's indexes: rows by place and by record, the views
/// showing each, and its content as it compares.
struct Resolve<'a> {
    covered: &'a Covered,
    rows: &'a Rows,
    at: HashMap<Place<'a>, Vec<usize>>,
    by_record: HashMap<i64, Vec<usize>>,
    shown: Masks,
    content: Vec<Cow<'a, str>>,
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
            .shows(w, p)
            .is_some_and(|i| has(self.dropped.get(i), w))
    }
}

impl<'a> Resolve<'a> {
    fn new(covered: &'a Covered, rows: &'a Rows) -> Self {
        let mut at: HashMap<Place<'a>, Vec<usize>> = HashMap::new();
        let mut by_record: HashMap<i64, Vec<usize>> = HashMap::new();
        let mut shown = Masks::new(covered.words, rows.rows.len());
        for (i, r) in rows.rows.iter().enumerate() {
            at.entry((&r.file_path, &r.path, &r.key))
                .or_default()
                .push(i);
            by_record.entry(r.key_id).or_default().push(i);
            let Some(holds) = covered.holds.get(&r.segment_id) else {
                continue;
            };
            let mask = shown.get_mut(i);
            mask.copy_from_slice(holds);
            for s in rows.entries.get(&r.id).into_iter().flatten() {
                if let Some(gone) = covered.holds.get(s) {
                    mask.iter_mut().zip(gone).for_each(|(m, g)| *m &= !g);
                }
            }
        }
        let content = rows
            .rows
            .iter()
            .map(|r| match rows.canonical {
                true => Cow::Borrowed(r.content.as_str()),
                false => serde_json::from_str(&r.content)
                    .map(|v| Cow::Owned(crate::model::canonical_json_text(&v)))
                    .unwrap_or(Cow::Borrowed(r.content.as_str())),
            })
            .collect();
        Resolve {
            covered,
            rows,
            at,
            by_record,
            shown,
            content,
        }
    }

    fn row(&self, i: usize) -> &'a AcrossRow {
        &self.rows.rows[i]
    }

    fn same(&self, a: usize, b: usize) -> bool {
        self.content[a] == self.content[b]
    }

    /// The live row view `u` shows at `p`.
    fn shows(&self, u: usize, p: Place<'_>) -> Option<usize> {
        self.at
            .get(&p)?
            .iter()
            .copied()
            .find(|&i| !self.row(i).deleted && has(self.shown.get(i), u))
    }

    /// The live row the state at base segment `b` has at `p`.
    fn base_at(&self, b: i64, p: Place<'_>) -> Option<usize> {
        let view = &self.covered.base_views[&b];
        self.at.get(&p)?.iter().copied().find(|&i| {
            let r = self.row(i);
            !r.deleted
                && view.contains(&r.segment_id)
                && !self
                    .rows
                    .entries
                    .get(&r.id)
                    .is_some_and(|e| e.iter().any(|s| view.contains(s)))
        })
    }

    /// Each live row's views that leave it out.
    fn resolve(self) -> Resolved<'a> {
        let n = self.covered.worktrees.len();
        let mut dropped = Masks::new(self.covered.words, self.rows.rows.len());
        for (&p, here) in &self.at {
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
                for w in ones(self.shown.get(i)) {
                    if self.unchanged_copy(i, w, p, &shows) {
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

    /// Whether row `i`, which view `w` shows at `p`, where each view shows
    /// `shows`, is unchanged since `w`'s merge base with another covered
    /// worktree which has changed it since.
    fn unchanged_copy(&self, i: usize, w: usize, p: Place<'_>, shows: &[Option<usize>]) -> bool {
        (0..shows.len()).any(|u| {
            let Some(bases) = self.covered.bases.get(&(w, u)) else {
                return false;
            };
            if shows[u].is_some_and(|x| self.same(x, i)) {
                return false;
            }
            // the base had it here, and the other has changed it here
            let here = bases
                .iter()
                .all(|&b| self.base_at(b, p).is_some_and(|x| self.same(x, i)));
            here || self.moved(i, u, bases, p)
        })
    }

    /// Whether row `i` moved to `p` since every one of `bases`, and `u`
    /// has modified it where the base had it, a live value: a rename and
    /// a deletion keep both.
    fn moved(&self, i: usize, u: usize, bases: &[i64], p: Place<'_>) -> bool {
        let others: Vec<Place<'a>> = self.by_record[&self.row(i).key_id]
            .iter()
            .map(|&o| {
                let r = self.row(o);
                (r.file_path.as_str(), r.path.as_str(), r.key.as_str())
            })
            .filter(|&q| q != p)
            .collect();
        bases.iter().all(|&b| {
            !self.base_at(b, p).is_some_and(|x| self.same(x, i))
                && others.iter().any(|&q| {
                    self.base_at(b, q).is_some_and(|x| self.same(x, i))
                        && self.shows(u, q).is_some_and(|y| !self.same(y, i))
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
