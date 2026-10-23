//! A read across worktrees by git's rule (branch-segments.md §4.1): each
//! covered worktree's own view, less a live version unchanged since its
//! merge base with another the read covers, which has changed it since.

use std::collections::{BTreeSet, HashMap, HashSet};

use crate::db::store::{Records, Store};
use crate::db::tables::{AcrossRow, AcrossRows, At, Coverage, CoveredWorktree, SegmentGraph};
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

/// Past how many records [`Covered::first_batch`] reads every row in the
/// covered segments rather than those at the records' places.
const EVERY_ROW_PAST: usize = 300;

/// Past how many covered rows a read across holds at once, it walks them
/// in place order instead.
const ROWS_HELD: usize = 50_000;

#[cfg(feature = "fault-injection")]
thread_local! {
    static EVERY_ROW: std::cell::Cell<usize> = const { std::cell::Cell::new(EVERY_ROW_PAST) };
    static HELD: std::cell::Cell<usize> = const { std::cell::Cell::new(ROWS_HELD) };
}

#[cfg(feature = "fault-injection")]
fn every_row_past() -> usize {
    EVERY_ROW.get()
}

#[cfg(not(feature = "fault-injection"))]
fn every_row_past() -> usize {
    EVERY_ROW_PAST
}

#[cfg(feature = "fault-injection")]
fn rows_held() -> usize {
    HELD.get()
}

#[cfg(not(feature = "fault-injection"))]
fn rows_held() -> usize {
    ROWS_HELD
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

/// Run `f` with every read across worktrees on this thread reading every
/// row in the covered segments, holding at most `n` at once, as one past
/// 50,000 rows does.
#[cfg(feature = "fault-injection")]
pub fn walking_rows<T>(n: usize, f: impl FnOnce() -> T) -> T {
    let before = (EVERY_ROW.replace(0), HELD.replace(n));
    let out = f();
    EVERY_ROW.set(before.0);
    HELD.set(before.1);
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
            parent: segments.into_iter().collect(),
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
        let Coverage {
            chains: rows,
            graph,
        } = DB::covered(tx, id, origin, branch, ids).await?;
        let mut worktrees: Vec<CoveredWorktree> = Vec::new();
        let mut chains: Vec<(usize, i64)> = Vec::new();
        // a row per segment of each worktree's chain, by worktree
        for row in rows {
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
        let mut graph = Graph::new(graph);
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

    /// The next batch of the rows the rule compares for `b`, or `None`
    /// past the last.
    async fn next_batch<DB: Store>(
        &self,
        tx: &mut sqlx::Transaction<'_, DB>,
        b: &mut Batches<'_>,
    ) -> Result<Option<Batch>> {
        loop {
            match std::mem::replace(&mut b.next, Next::Done) {
                Next::Done => return Ok(None),
                Next::Start => {
                    if let Some(batch) = self.first_batch(tx, b).await? {
                        return Ok(Some(batch));
                    }
                }
                Next::Walk { after, moved } => return self.walk_batch(tx, b, after, moved).await,
            }
        }
    }

    /// The rows in [`Self::segments`] at every place `b`'s records have a
    /// row there, with the entries on them; past [`every_row_past`]
    /// records, every row there. `None`, with `b` set to walk them, past
    /// [`rows_held`] of those.
    async fn first_batch<DB: Store>(
        &self,
        tx: &mut sqlx::Transaction<'_, DB>,
        b: &mut Batches<'_>,
    ) -> Result<Option<Batch>> {
        let segments = self.segments();
        // past a few hundred, reading every row is cheaper than their places
        if b.count <= every_row_past() {
            let rows = DB::across_rows(tx, &segments, b.records).await?;
            return Ok(Some(Batch::all(with_entries(rows))));
        }
        let held = rows_held();
        let rows = DB::across_rows_in(tx, &segments, held + 1).await?;
        if rows.len() <= held {
            // those in other segments neither view nor base holds
            let entries = DB::entries_in_segments(tx, &segments).await?;
            return Ok(Some(Batch::all(with_entries(AcrossRows { rows, entries }))));
        }
        let moved = DB::moved_records(tx, &segments)
            .await?
            .into_iter()
            .collect();
        b.next = Next::Walk { after: None, moved };
        Ok(None)
    }

    /// The rows in [`Self::segments`] at the places after `after`, up to
    /// [`rows_held`] of them but whole places, and the rows at the other
    /// places of the `moved` records among them.
    async fn walk_batch<DB: Store>(
        &self,
        tx: &mut sqlx::Transaction<'_, DB>,
        b: &mut Batches<'_>,
        after: Option<(String, String, String)>,
        moved: HashSet<i64>,
    ) -> Result<Option<Batch>> {
        let segments = self.segments();
        let from = after.as_ref().map(|(file_path, path, key)| At {
            file_path,
            path,
            key,
        });
        let (mut found, last) = whole_places::<DB>(tx, &segments, from).await?;
        let Some(end) = found.rows.last().map(place) else {
            return Ok(None);
        };
        let decided = found.rows.len();
        let mut ids: Vec<i64> = found
            .rows
            .iter()
            .map(|r| r.key_id)
            .filter(|k| moved.contains(k))
            .collect();
        ids.sort_unstable();
        ids.dedup();
        if !ids.is_empty() {
            let more = DB::across_rows(tx, &segments, Records::Ids(&ids)).await?;
            let have: HashSet<i64> = found.rows.iter().map(|r| r.id).collect();
            found
                .entries
                .extend(more.entries.into_iter().filter(|(r, _)| !have.contains(r)));
            found
                .rows
                .extend(more.rows.into_iter().filter(|r| !have.contains(&r.id)));
        }
        if !last {
            b.next = Next::Walk {
                after: Some(end),
                moved,
            };
        }
        Ok(Some(Batch {
            rows: with_entries(found),
            decided,
        }))
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
        let index: HashMap<i64, usize> = self
            .worktrees
            .iter()
            .enumerate()
            .map(|(i, w)| (w.id, i))
            .collect();
        let mut dropped = vec![false; records.len()];
        let mut batches = Batches::new(Records::Ids(&ids), ids.len());
        while let Some(batch) = self.next_batch(tx, &mut batches).await? {
            let resolved = Resolve::new(self, &batch.rows, batch.decided).resolve();
            for (rec, gone) in records.iter().zip(dropped.iter_mut()) {
                if rec.deleted || rec.conflict.is_some() {
                    continue;
                }
                let Some(&w) = index.get(&rec.worktree_id) else {
                    continue;
                };
                let p = (rec.file_path.as_str(), rec.path.as_str(), rec.key.as_str());
                if let Some(d) = resolved.dropped_at(w, p) {
                    *gone = d;
                }
            }
        }
        Ok(records
            .into_iter()
            .zip(dropped)
            .filter(|(_, gone)| !gone)
            .map(|(rec, _)| rec)
            .collect())
    }

    /// Those of the rows `candidates` some covered view shows and the rule
    /// keeps.
    pub(crate) async fn kept<DB: Store>(
        &self,
        tx: &mut sqlx::Transaction<'_, DB>,
        mut candidates: Vec<i64>,
    ) -> Result<Vec<i64>> {
        if candidates.is_empty() {
            return Ok(Vec::new());
        }
        candidates.sort_unstable();
        let mut out = Vec::new();
        let mut batches = Batches::new(Records::OfRows(&candidates), candidates.len());
        while let Some(batch) = self.next_batch(tx, &mut batches).await? {
            let resolved = Resolve::new(self, &batch.rows, batch.decided).resolve();
            out.extend(
                batch
                    .rows
                    .rows
                    .iter()
                    .enumerate()
                    .filter(|&(i, r)| {
                        resolved.decides(i)
                            && resolved.kept(i)
                            && candidates.binary_search(&r.id).is_ok()
                    })
                    .map(|(_, r)| r.id),
            );
        }
        Ok(out)
    }
}

/// Up to [`rows_held`] rows in `segments` at the places after `after`,
/// ending on a whole place, and whether they're the last.
async fn whole_places<DB: Store>(
    tx: &mut sqlx::Transaction<'_, DB>,
    segments: &[i64],
    after: Option<At<'_>>,
) -> Result<(AcrossRows, bool)> {
    let mut limit = rows_held().max(1);
    loop {
        let mut found = DB::rows_after(tx, segments, after, limit).await?;
        let Some(last) = found.rows.last().map(place) else {
            return Ok((found, true));
        };
        if found.rows.len() < limit {
            return Ok((found, true));
        }
        // the last place may go on past the limit: leave it to the next
        match found.rows.iter().rposition(|r| place(r) != last) {
            Some(i) => {
                let gone: HashSet<i64> = found.rows.drain(i + 1..).map(|r| r.id).collect();
                found.entries.retain(|(r, _)| !gone.contains(r));
                return Ok((found, false));
            }
            // one place fills them
            None => limit *= 2,
        }
    }
}

/// A row's place.
fn place(r: &AcrossRow) -> (String, String, String) {
    (r.file_path.clone(), r.path.clone(), r.key.clone())
}

/// Rows to resolve: the first `decided` of them those whose places the
/// resolution decides, the rest moved records' rows elsewhere.
struct Batch {
    rows: Rows,
    decided: usize,
}

impl Batch {
    fn all(rows: Rows) -> Self {
        let decided = rows.rows.len();
        Batch { rows, decided }
    }
}

/// Where [`Covered::next_batch`] reads on from.
enum Next {
    Start,
    /// Walking the covered rows in place order, after `after`; `moved` are
    /// the records with rows at more than one place.
    Walk {
        after: Option<(String, String, String)>,
        moved: HashSet<i64>,
    },
    Done,
}

/// The batches of the rows the rule compares for `count` `records`.
struct Batches<'r> {
    records: Records<'r>,
    count: usize,
    next: Next,
}

impl<'r> Batches<'r> {
    fn new(records: Records<'r>, count: usize) -> Self {
        Batches {
            records,
            count,
            next: Next::Start,
        }
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

/// A batch's rows, and the entries on them by row.
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
    /// The places it decides: those below this, the batch's own.
    decided: usize,
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

    /// Whether it decides row `i`'s place.
    fn decides(&self, i: usize) -> bool {
        self.resolve.place_of[i] < self.resolve.decided
    }

    /// Whether view `w` leaves out the live row it shows at `p`, if it
    /// decides `p`.
    fn dropped_at(&self, w: usize, p: Place<'_>) -> Option<bool> {
        let q = *self.resolve.places.get(&p)?;
        (q < self.resolve.decided).then(|| {
            self.resolve
                .shows(w, q)
                .is_some_and(|i| has(self.dropped.get(i), w))
        })
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
    /// The resolution of `rows`, deciding the places of the first
    /// `decided` of them.
    fn new(covered: &'a Covered, rows: &'a Rows, decided: usize) -> Self {
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
        // places are numbered as their first rows come
        let decided = place_of[..decided].iter().max().map_or(0, |&q| q + 1);
        Resolve {
            covered,
            rows,
            at,
            place_of,
            places,
            by_record,
            shown,
            based,
            decided,
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
        for (q, here) in self.at.iter().enumerate().take(self.decided) {
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
