//! Model test for the segment design (docs/branch-segments.md).
//!
//! [`Segments`] is an in-memory implementation of the design's storage:
//! segments, immutable row versions and supersession entries, with the
//! operations of §4 — writes, scans, the commit fold, forks and splits,
//! publishing a user branch, deletion and compaction.
//!
//! [`Model`] is a reference that knows nothing about segments: each
//! worktree's committed tree, its draft, and the versions each draft edit
//! was made over. After every step of a random history, every worktree's
//! own view must agree with it exactly. A user branch's layered view must
//! never lose a row the model shows, and may only add a copy of a version
//! the user's edit was made over: conflicts are over-reported, never lost.
//!
//! Versions are shared ids: the harness hands both sides the same id for
//! each edit, so views compare as sets of `(key, version)`. Tombstones
//! aren't compared, only what they hide.

use std::collections::{BTreeMap, BTreeSet};

use proptest::prelude::*;

type Key = u8;
type Ver = u64;
type RowId = u64;
type SegId = usize;
type Wt = usize;
type CommitId = usize;

const MAIN: Wt = 0;
const KEYS: Key = 4;
/// Versions for tombstones the implementation writes on its own (scans,
/// splits) — never compared, kept out of the shared range.
const PRIVATE_VERSIONS: Ver = 1 << 40;

/// Git: each commit's whole tree.
#[derive(Default)]
struct Git {
    commits: Vec<BTreeMap<Key, Ver>>,
}

impl Git {
    fn commit(&mut self, tree: BTreeMap<Key, Ver>) -> CommitId {
        self.commits.push(tree);
        self.commits.len() - 1
    }
}

// ---------------------------------------------------------------------------
// The implementation
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Kind {
    Head,
    Internal,
    Draft,
}

#[derive(Clone, Debug)]
struct Seg {
    kind: Kind,
    parent: Option<SegId>,
    owner: Option<Wt>,
    head_commit: Option<CommitId>,
    alive: bool,
}

#[derive(Clone, Debug)]
struct Row {
    seg: SegId,
    key: Key,
    ver: Ver,
    deleted: bool,
    /// Made by a split from git's content, rather than by the write that
    /// created the version.
    recreated: bool,
}

#[derive(Clone, Debug)]
struct SWt {
    head: SegId,
    draft: SegId,
    /// Root first; `true` marks a segment inherited when forked.
    chain: Vec<(SegId, bool)>,
    history: Vec<CommitId>,
    alive: bool,
}

#[derive(Default)]
struct Segments {
    segs: Vec<Seg>,
    rows: BTreeMap<RowId, Row>,
    /// `(row, segment)`: the segment holds a newer version of the row's record.
    sup: BTreeSet<(RowId, SegId)>,
    wts: Vec<SWt>,
    next_row: RowId,
    next_private: Ver,
}

impl Segments {
    fn new_seg(
        &mut self,
        kind: Kind,
        parent: Option<SegId>,
        owner: Option<Wt>,
        head_commit: Option<CommitId>,
    ) -> SegId {
        self.segs.push(Seg {
            kind,
            parent,
            owner,
            head_commit,
            alive: true,
        });
        self.segs.len() - 1
    }

    fn insert(&mut self, seg: SegId, key: Key, ver: Ver, deleted: bool) -> RowId {
        let id = self.next_row;
        self.next_row += 1;
        self.rows.insert(
            id,
            Row {
                seg,
                key,
                ver,
                deleted,
                recreated: false,
            },
        );
        id
    }

    fn private_ver(&mut self) -> Ver {
        self.next_private += 1;
        PRIVATE_VERSIONS + self.next_private
    }

    /// Delete a row; entries on it go too (`ON DELETE CASCADE`).
    fn delete_row(&mut self, id: RowId) {
        self.rows.remove(&id);
        self.sup.retain(|&(r, _)| r != id);
    }

    fn rows_in(&self, seg: SegId) -> Vec<RowId> {
        self.rows
            .iter()
            .filter(|(_, r)| r.seg == seg)
            .map(|(id, _)| *id)
            .collect()
    }

    fn row_in(&self, seg: SegId, key: Key) -> Option<RowId> {
        self.rows
            .iter()
            .find(|(_, r)| r.seg == seg && r.key == key)
            .map(|(id, _)| *id)
    }

    fn chain_set(&self, w: Wt) -> BTreeSet<SegId> {
        self.wts[w].chain.iter().map(|&(s, _)| s).collect()
    }

    fn own_view(&self, w: Wt) -> BTreeSet<SegId> {
        let mut v = self.chain_set(w);
        v.insert(self.wts[w].draft);
        v
    }

    /// The base's whole view plus each upper worktree's own part.
    fn layered(&self, base: Wt, uppers: &[Wt]) -> BTreeSet<SegId> {
        let mut v = self.own_view(base);
        for &upper in uppers {
            v.extend(
                self.wts[upper]
                    .chain
                    .iter()
                    .filter(|(_, inherited)| !inherited)
                    .map(|&(s, _)| s),
            );
            v.insert(self.wts[upper].draft);
        }
        v
    }

    fn visible(&self, view: &BTreeSet<SegId>, key: Option<Key>) -> Vec<RowId> {
        self.rows
            .iter()
            .filter(|(id, r)| {
                view.contains(&r.seg)
                    && key.is_none_or(|k| r.key == k)
                    && !self
                        .sup
                        .range((**id, 0)..(**id + 1, 0))
                        .any(|&(_, s)| view.contains(&s))
            })
            .map(|(id, _)| *id)
            .collect()
    }

    fn ancestors(&self, seg: SegId) -> BTreeSet<SegId> {
        let mut out = BTreeSet::new();
        let mut p = self.segs[seg].parent;
        while let Some(s) = p {
            out.insert(s);
            p = self.segs[s].parent;
        }
        out
    }

    fn move_entries(&mut self, from: SegId, to: SegId, key: Key) {
        let moved: Vec<RowId> = self
            .sup
            .iter()
            .filter(|&&(r, s)| s == from && self.rows[&r].key == key)
            .map(|&(r, _)| r)
            .collect();
        for r in moved {
            self.sup.remove(&(r, from));
            self.sup.insert((r, to));
        }
    }

    /// §4.2: a write into `w`'s draft, made through `via`.
    fn write(&mut self, w: Wt, via: &BTreeSet<SegId>, key: Key, ver: Ver, deleted: bool) {
        let d = self.wts[w].draft;
        let own = self.own_view(w);
        let mut seen: BTreeSet<RowId> = self.visible(via, Some(key)).into_iter().collect();
        seen.extend(self.visible(&own, Some(key)));
        if let Some(old) = self.row_in(d, key) {
            seen.remove(&old);
            self.delete_row(old);
        }
        self.insert(d, key, ver, deleted);
        for r in seen {
            self.sup.insert((r, d));
        }
    }

    /// C.11: the draft supersedes what `w`'s committed chain now shows.
    fn relink_draft(&mut self, w: Wt) {
        let d = self.wts[w].draft;
        let chain = self.chain_set(w);
        let keys: BTreeSet<Key> = self
            .rows_in(d)
            .into_iter()
            .map(|r| self.rows[&r].key)
            .collect();
        for k in keys {
            for c in self.visible(&chain, Some(k)) {
                self.sup.insert((c, d));
            }
        }
    }

    /// §4.3: a fast-forward scan brings `w`'s head up to a commit with
    /// these changes (`None`: deleted), such as a merge.
    fn scan(&mut self, w: Wt, changes: &[(Key, Option<Ver>)], commit: CommitId) {
        for &(key, change) in changes {
            self.scan_key(w, key, change);
        }
        let h = self.wts[w].head;
        self.segs[h].head_commit = Some(commit);
        self.wts[w].history.push(commit);
        self.relink_draft(w);
    }

    fn scan_key(&mut self, w: Wt, key: Key, change: Option<Ver>) {
        let h = self.wts[w].head;
        let mut below = self.chain_set(w);
        below.remove(&h);
        let vis_below = self.visible(&below, Some(key));
        let head_row = self.row_in(h, key);
        if let Some(hr) = head_row {
            self.delete_row(hr);
        }
        let new_row = match change {
            Some(v) => Some(self.insert(h, key, v, false)),
            None if vis_below.iter().any(|r| !self.rows[r].deleted) => {
                let t = self.private_ver();
                Some(self.insert(h, key, t, true))
            }
            None => None,
        };
        if new_row.is_some() && head_row.is_none() {
            for r in vis_below {
                self.sup.insert((r, h));
            }
        }
    }

    /// §4.4: the fold.
    fn commit(&mut self, w: Wt, commit: CommitId) {
        let (d, h) = (self.wts[w].draft, self.wts[w].head);
        for x in self.rows_in(d) {
            let k = self.rows[&x].key;
            if let Some(hr) = self.row_in(h, k) {
                self.delete_row(hr);
            }
            self.move_entries(d, h, k);
            self.rows.get_mut(&x).unwrap().seg = h;
            let hides = self
                .sup
                .iter()
                .any(|&(r, s)| s == h && self.rows[&r].key == k);
            if self.rows[&x].deleted && !hides {
                self.delete_row(x);
            }
        }
        self.segs[h].head_commit = Some(commit);
        self.wts[w].history.push(commit);
    }

    fn add_worktree(
        &mut self,
        head: SegId,
        chain: Vec<(SegId, bool)>,
        history: Vec<CommitId>,
    ) -> Wt {
        let w = self.wts.len();
        let draft = self.new_seg(Kind::Draft, None, Some(w), None);
        self.segs[head].owner = Some(w);
        self.wts.push(SWt {
            head,
            draft,
            chain,
            history,
            alive: true,
        });
        w
    }

    /// Close `w`'s head if it has rows; either way, return the base a new
    /// branch at `w`'s head starts from, and the chain it inherits.
    fn base_at_head(&mut self, w: Wt) -> (Option<SegId>, Vec<(SegId, bool)>) {
        let h = self.wts[w].head;
        let commit = *self.wts[w].history.last().unwrap();
        if self.rows_in(h).is_empty() {
            let chain = self.wts[w]
                .chain
                .iter()
                .filter(|&&(s, _)| s != h)
                .map(|&(s, _)| (s, true))
                .collect();
            (self.segs[h].parent, chain)
        } else {
            let chain = self.wts[w].chain.iter().map(|&(s, _)| (s, true)).collect();
            self.segs[h].kind = Kind::Internal;
            self.segs[h].owner = None;
            let hw = self.new_seg(Kind::Head, Some(h), Some(w), Some(commit));
            self.wts[w].chain.push((hw, false));
            self.wts[w].head = hw;
            (Some(h), chain)
        }
    }

    /// §4.5 and §4.6: fork `w` at `history[pos]`, splitting a segment if the
    /// commit falls inside one.
    fn fork(&mut self, w: Wt, pos: usize, git: &Git) -> Wt {
        let history = self.wts[w].history.clone();
        let c = history[pos];
        if pos == history.len() - 1 {
            let (base, mut chain) = self.base_at_head(w);
            let hc = self.new_seg(Kind::Head, base, None, Some(c));
            chain.push((hc, false));
            return self.add_worktree(hc, chain, history);
        }
        let pos_of = |commit: CommitId| history.iter().position(|&x| x == commit).unwrap();
        let s = self.wts[w]
            .chain
            .iter()
            .map(|&(s, _)| s)
            .find(|&s| pos_of(self.segs[s].head_commit.unwrap()) >= pos)
            .unwrap();
        if self.segs[s].head_commit != Some(c) {
            self.split(s, c, git);
        }
        let mut chain: Vec<(SegId, bool)> = Vec::new();
        for &(seg, _) in &self.wts[w].chain {
            chain.push((seg, true));
            if seg == s {
                break;
            }
        }
        let hc = self.new_seg(Kind::Head, Some(s), None, Some(c));
        chain.push((hc, false));
        self.add_worktree(hc, chain, history[..=pos].to_vec())
    }

    /// §4.7: `s` keeps its id and ends at `c`; a new `s2` holds the rest.
    fn split(&mut self, s: SegId, c: CommitId, git: &Git) {
        let h_commit = self.segs[s].head_commit.unwrap();
        let (kind, owner) = (self.segs[s].kind, self.segs[s].owner);
        let s2 = self.new_seg(kind, Some(s), owner, Some(h_commit));
        if kind == Kind::Head {
            self.segs[s].kind = Kind::Internal;
            self.segs[s].owner = None;
            self.wts[owner.unwrap()].head = s2;
        }
        self.segs[s].head_commit = Some(c);
        for i in 0..self.segs.len() {
            if i != s2 && self.segs[i].alive && self.segs[i].parent == Some(s) {
                self.segs[i].parent = Some(s2);
            }
        }
        for wt in &mut self.wts {
            if let Some(i) = wt.chain.iter().position(|&(seg, _)| seg == s) {
                let inherited = wt.chain[i].1;
                wt.chain.insert(i + 1, (s2, inherited));
            }
        }
        let below = self.ancestors(s);
        let (tree_c, tree_h) = (&git.commits[c], &git.commits[h_commit]);
        let keys: BTreeSet<Key> = tree_c
            .keys()
            .chain(tree_h.keys())
            .copied()
            .filter(|k| tree_c.get(k) != tree_h.get(k))
            .collect();
        for k in keys {
            let v_c = tree_c.get(&k).copied();
            let vis_below = self.visible(&below, Some(k));
            let below_live = vis_below
                .iter()
                .find(|r| !self.rows[r].deleted)
                .map(|r| self.rows[r].ver);
            match self.row_in(s, k) {
                Some(sr) => {
                    self.rows.get_mut(&sr).unwrap().seg = s2;
                    if v_c != below_live {
                        let new = self.insert_value(s, k, v_c);
                        self.recreated(new);
                        self.sup.insert((new, s2));
                    } else {
                        self.move_entries(s, s2, k);
                    }
                }
                None => {
                    // changed before c and back after it
                    let new = self.insert_value(s, k, v_c);
                    self.recreated(new);
                    for &x in &vis_below {
                        self.sup.insert((x, s));
                    }
                    let r2 = match vis_below.first() {
                        Some(&b) => {
                            let (ver, deleted) = (self.rows[&b].ver, self.rows[&b].deleted);
                            self.insert(s2, k, ver, deleted)
                        }
                        None => self.insert_value(s2, k, None),
                    };
                    self.recreated(r2);
                    // The restored row stands for a version older than
                    // anything a segment outside `s` and its ancestors
                    // holds for the key, so each of those supersedes it.
                    let newer: BTreeSet<SegId> = self
                        .rows
                        .values()
                        .filter(|r| {
                            r.key == k && r.seg != s && r.seg != s2 && !below.contains(&r.seg)
                        })
                        .map(|r| r.seg)
                        .collect();
                    for seg in newer {
                        self.sup.insert((r2, seg));
                    }
                    self.sup.insert((new, s2));
                }
            }
        }
    }

    fn recreated(&mut self, row: RowId) {
        self.rows.get_mut(&row).unwrap().recreated = true;
    }

    fn insert_value(&mut self, seg: SegId, key: Key, value: Option<Ver>) -> RowId {
        match value {
            Some(v) => self.insert(seg, key, v, false),
            None => {
                let t = self.private_ver();
                self.insert(seg, key, t, true)
            }
        }
    }

    /// C.15: rebase user branch `u` onto main's head.
    fn publish(&mut self, u: Wt) {
        let (base, _) = self.base_at_head(MAIN);
        let mut chain: Vec<(SegId, bool)> = self.wts[MAIN]
            .chain
            .iter()
            .filter(|&&(s, _)| self.segs[s].kind == Kind::Internal)
            .map(|&(s, _)| (s, true))
            .collect();
        let hu = self.wts[u].head;
        self.segs[hu].parent = base;
        self.segs[hu].head_commit = self.wts[MAIN].history.last().copied();
        chain.push((hu, false));
        self.wts[u].chain = chain;
        self.wts[u].history = self.wts[MAIN].history.clone();
        self.relink_draft(u);
    }

    /// §4.8: `w`'s HEAD moved to `commit`, which doesn't descend from its
    /// old head: rebuild on the chain up to `chain[base_idx]`.
    fn rebuild(
        &mut self,
        w: Wt,
        base_idx: Option<usize>,
        tree: &BTreeMap<Key, Ver>,
        commit: CommitId,
    ) {
        let base_chain: Vec<(SegId, bool)> = match base_idx {
            Some(i) => self.wts[w].chain[..=i].to_vec(),
            None => Vec::new(),
        };
        let base = base_chain.last().map(|&(s, _)| s);
        let view: BTreeSet<SegId> = base_chain.iter().map(|&(s, _)| s).collect();
        let hn = self.new_seg(Kind::Head, base, Some(w), Some(commit));
        for k in 0..KEYS {
            let vis = self.visible(&view, Some(k));
            let live = vis
                .iter()
                .find(|r| !self.rows[r].deleted)
                .map(|r| self.rows[r].ver);
            let want = tree.get(&k).copied();
            if want == live {
                continue;
            }
            self.insert_value(hn, k, want);
            for x in vis {
                self.sup.insert((x, hn));
            }
        }
        let old_head = self.wts[w].head;
        let keep = base.map_or(0, |b| {
            let c = self.segs[b].head_commit.unwrap();
            self.wts[w].history.iter().position(|&x| x == c).unwrap() + 1
        });
        let mut chain = base_chain;
        chain.push((hn, false));
        let wt = &mut self.wts[w];
        wt.chain = chain;
        wt.head = hn;
        wt.history.truncate(keep);
        wt.history.push(commit);
        self.segs[old_head].kind = Kind::Internal;
        self.segs[old_head].owner = None;
        self.relink_draft(w);
        self.compact();
    }

    fn drop_seg(&mut self, s: SegId) {
        self.segs[s].alive = false;
        for r in self.rows_in(s) {
            self.delete_row(r);
        }
        self.sup.retain(|&(_, seg)| seg != s);
        for wt in &mut self.wts {
            wt.chain.retain(|&(seg, _)| seg != s);
        }
    }

    /// §4.13
    fn delete_worktree(&mut self, w: Wt) {
        self.wts[w].alive = false;
        let (h, d) = (self.wts[w].head, self.wts[w].draft);
        self.drop_seg(d);
        self.drop_seg(h);
        self.compact();
    }

    fn compact(&mut self) {
        loop {
            let referenced: BTreeSet<SegId> = self
                .wts
                .iter()
                .filter(|w| w.alive)
                .flat_map(|w| w.chain.iter().map(|&(s, _)| s))
                .collect();
            let dead: Vec<SegId> = (0..self.segs.len())
                .filter(|&s| {
                    self.segs[s].alive
                        && self.segs[s].kind != Kind::Draft
                        && !referenced.contains(&s)
                })
                .collect();
            if !dead.is_empty() {
                for s in dead {
                    self.drop_seg(s);
                }
                continue;
            }
            let fold = (0..self.segs.len()).find_map(|p| {
                if !self.segs[p].alive || self.segs[p].kind != Kind::Internal {
                    return None;
                }
                let children: Vec<SegId> = (0..self.segs.len())
                    .filter(|&c| self.segs[c].alive && self.segs[c].parent == Some(p))
                    .collect();
                let [c] = children[..] else { return None };
                // never fold across a fork boundary: a worktree's inherited
                // and own segments stay separate
                let crosses = self.wts.iter().filter(|w| w.alive).any(|w| {
                    let flag = |seg| w.chain.iter().find(|&&(s, _)| s == seg).map(|&(_, i)| i);
                    flag(p).is_some() && flag(c).is_some() && flag(p) != flag(c)
                });
                (!crosses).then_some((p, c))
            });
            let Some((p, c)) = fold else { break };
            for r in self.rows_in(p) {
                if self.sup.contains(&(r, c)) {
                    self.delete_row(r);
                }
            }
            for r in self.rows_in(p) {
                self.rows.get_mut(&r).unwrap().seg = c;
            }
            let moved: Vec<RowId> = self
                .sup
                .iter()
                .filter(|&&(_, s)| s == p)
                .map(|&(r, _)| r)
                .collect();
            for r in moved {
                self.sup.remove(&(r, p));
                self.sup.insert((r, c));
            }
            self.segs[c].parent = self.segs[p].parent;
            self.segs[p].alive = false;
            for wt in &mut self.wts {
                wt.chain.retain(|&(seg, _)| seg != p);
            }
        }
    }
}

// ---------------------------------------------------------------------------
// The reference model
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
struct ODraft {
    ver: Ver,
    deleted: bool,
    /// The versions this edit was made over.
    based_on: BTreeSet<Ver>,
}

#[derive(Clone, Debug)]
struct OWt {
    committed: BTreeMap<Key, Ver>,
    draft: BTreeMap<Key, ODraft>,
    user: bool,
    alive: bool,
}

impl OWt {
    fn view(&self) -> BTreeMap<Key, Ver> {
        let mut v = self.committed.clone();
        for (k, d) in &self.draft {
            if d.deleted {
                v.remove(k);
            } else {
                v.insert(*k, d.ver);
            }
        }
        v
    }

    /// What this worktree's view shows for `k`, tombstones included.
    fn shown(&self, k: Key) -> Option<(Ver, bool)> {
        self.draft
            .get(&k)
            .map(|d| (d.ver, d.deleted))
            .or_else(|| self.committed.get(&k).map(|&v| (v, false)))
    }
}

struct Model {
    wts: Vec<OWt>,
}

impl Model {
    /// Main with user branches' edits stacked on top: main's version and
    /// each edit show, except a version some edit in the stack was made
    /// over. Tombstones included.
    fn stack_rows(&self, uppers: &[Wt], k: Key) -> Vec<(Ver, bool)> {
        let drafts: Vec<&ODraft> = uppers
            .iter()
            .filter_map(|&u| self.wts[u].draft.get(&k))
            .collect();
        let hidden: BTreeSet<Ver> = drafts
            .iter()
            .flat_map(|d| d.based_on.iter().copied())
            .collect();
        self.wts[MAIN]
            .shown(k)
            .into_iter()
            .chain(drafts.iter().map(|d| (d.ver, d.deleted)))
            .filter(|(v, _)| !hidden.contains(v))
            .collect()
    }

    fn layered(&self, uppers: &[Wt]) -> BTreeSet<(Key, Ver)> {
        (0..KEYS)
            .flat_map(|k| {
                self.stack_rows(uppers, k)
                    .into_iter()
                    .filter(|&(_, deleted)| !deleted)
                    .map(move |(v, _)| (k, v))
            })
            .collect()
    }
}

// ---------------------------------------------------------------------------
// The harness
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
enum Op {
    /// A write through a worktree's own view (main or a fork).
    Write(u8, Key, bool),
    Commit(u8),
    /// A commit made outside the database, then scanned: one or more
    /// changes, as a merge brings.
    External(u8, Vec<(Key, bool)>),
    /// Fork main or a fork, `.1` commits back from its head.
    Fork(u8, u8),
    NewUser,
    /// A write by a user through main, the other users' branches listed
    /// (a stack), and their own on top.
    UserWrite(u8, Vec<u8>, Key, bool),
    Publish(u8),
    Delete(u8),
    /// A rewrite (rebase, reset, force-push): a worktree's HEAD moves to a
    /// new commit on top of an older segment, with one key changed.
    Rebuild(u8, u8, Key, bool),
}

fn op() -> impl Strategy<Value = Op> {
    let key = 0..KEYS;
    prop_oneof![
        4 => (any::<u8>(), key.clone(), prop::bool::weighted(0.2)).prop_map(|(w, k, d)| Op::Write(w, k, d)),
        2 => any::<u8>().prop_map(Op::Commit),
        2 => (any::<u8>(), prop::collection::vec((key.clone(), prop::bool::weighted(0.2)), 1..4)).prop_map(|(w, c)| Op::External(w, c)),
        1 => (any::<u8>(), 0u8..4).prop_map(|(w, b)| Op::Fork(w, b)),
        1 => Just(Op::NewUser),
        4 => (any::<u8>(), prop::collection::vec(any::<u8>(), 0..3), key, prop::bool::weighted(0.2)).prop_map(|(u, b, k, d)| Op::UserWrite(u, b, k, d)),
        1 => any::<u8>().prop_map(Op::Publish),
        1 => any::<u8>().prop_map(Op::Delete),
        1 => (any::<u8>(), any::<u8>(), 0..KEYS, prop::bool::weighted(0.2)).prop_map(|(w, b, k, d)| Op::Rebuild(w, b, k, d)),
    ]
}

struct World {
    imp: Segments,
    model: Model,
    git: Git,
    next_ver: Ver,
}

impl World {
    fn new() -> Self {
        let mut git = Git::default();
        let mut imp = Segments::default();
        let tree: BTreeMap<Key, Ver> = (0..KEYS).map(|k| (k, k as Ver + 1)).collect();
        let c0 = git.commit(tree.clone());
        let root = imp.new_seg(Kind::Head, None, None, Some(c0));
        for (&k, &v) in &tree {
            imp.insert(root, k, v, false);
        }
        imp.add_worktree(root, vec![(root, false)], vec![c0]);
        World {
            imp,
            model: Model {
                wts: vec![OWt {
                    committed: tree,
                    draft: BTreeMap::new(),
                    user: false,
                    alive: true,
                }],
            },
            git,
            next_ver: KEYS as Ver + 1,
        }
    }

    fn ver(&mut self) -> Ver {
        self.next_ver += 1;
        self.next_ver
    }

    fn pick(&self, n: u8, user: bool) -> Option<Wt> {
        let alive: Vec<Wt> = (0..self.model.wts.len())
            .filter(|&w| self.model.wts[w].alive && self.model.wts[w].user == user)
            .collect();
        (!alive.is_empty()).then(|| alive[n as usize % alive.len()])
    }

    fn apply(&mut self, op: &Op) {
        match *op {
            Op::Write(n, k, deleted) => {
                let w = self.pick(n, false).unwrap();
                let ver = self.ver();
                let own = self.imp.own_view(w);
                self.imp.write(w, &own, k, ver, deleted);
                self.model.wts[w].draft.insert(
                    k,
                    ODraft {
                        ver,
                        deleted,
                        based_on: BTreeSet::new(),
                    },
                );
            }
            Op::Commit(n) => {
                let w = self.pick(n, false).unwrap();
                if self.model.wts[w].draft.is_empty() {
                    return;
                }
                let m = &mut self.model.wts[w];
                for (k, d) in std::mem::take(&mut m.draft) {
                    if d.deleted {
                        m.committed.remove(&k);
                    } else {
                        m.committed.insert(k, d.ver);
                    }
                }
                let c = self.git.commit(m.committed.clone());
                self.imp.commit(w, c);
            }
            Op::External(n, ref changes) => {
                let w = self.pick(n, false).unwrap();
                let changes: BTreeMap<Key, Option<Ver>> = changes
                    .iter()
                    .map(|&(k, deleted)| (k, (!deleted).then(|| self.ver())))
                    .collect();
                let m = &mut self.model.wts[w];
                for (&k, &change) in &changes {
                    match change {
                        Some(v) => m.committed.insert(k, v),
                        None => m.committed.remove(&k),
                    };
                }
                let c = self.git.commit(m.committed.clone());
                let changes: Vec<(Key, Option<Ver>)> = changes.into_iter().collect();
                self.imp.scan(w, &changes, c);
            }
            Op::Fork(n, back) => {
                let w = self.pick(n, false).unwrap();
                let len = self.imp.wts[w].history.len();
                let pos = len - 1 - (back as usize).min(len - 1);
                let c = self.imp.wts[w].history[pos];
                self.imp.fork(w, pos, &self.git);
                self.model.wts.push(OWt {
                    committed: self.git.commits[c].clone(),
                    draft: BTreeMap::new(),
                    user: false,
                    alive: true,
                });
            }
            Op::NewUser => {
                let pos = self.imp.wts[MAIN].history.len() - 1;
                self.imp.fork(MAIN, pos, &self.git);
                let committed = self.model.wts[MAIN].committed.clone();
                self.model.wts.push(OWt {
                    committed,
                    draft: BTreeMap::new(),
                    user: true,
                    alive: true,
                });
            }
            Op::UserWrite(n, ref below, k, deleted) => {
                let Some(u) = self.pick(n, true) else { return };
                let mut stack: Vec<Wt> = Vec::new();
                for &b in below {
                    let v = self.pick(b, true).unwrap();
                    if v != u && !stack.contains(&v) {
                        stack.push(v);
                    }
                }
                stack.push(u);
                let ver = self.ver();
                let via = self.imp.layered(MAIN, &stack);
                self.imp.write(u, &via, k, ver, deleted);
                let old = self.model.wts[u].draft.get(&k).map(|d| d.ver);
                let mut seen: BTreeSet<Ver> = self
                    .model
                    .stack_rows(&stack, k)
                    .into_iter()
                    .map(|(v, _)| v)
                    .filter(|&v| Some(v) != old)
                    .collect();
                let user = &mut self.model.wts[u];
                match user.draft.get(&k) {
                    Some(old) => seen.extend(&old.based_on),
                    None => seen.extend(user.committed.get(&k)),
                }
                user.draft.insert(
                    k,
                    ODraft {
                        ver,
                        deleted,
                        based_on: seen,
                    },
                );
            }
            Op::Publish(n) => {
                let Some(u) = self.pick(n, true) else { return };
                self.imp.publish(u);
                let committed = self.model.wts[MAIN].committed.clone();
                let user = &mut self.model.wts[u];
                user.committed = committed;
                for (k, d) in user.draft.iter_mut() {
                    if let Some(&v) = user.committed.get(k) {
                        d.based_on.insert(v);
                    }
                }
            }
            Op::Rebuild(n, back, k, deleted) => {
                let w = self.pick(n, false).unwrap();
                let chain_len = self.imp.wts[w].chain.len();
                // a segment below the head, or none (a new root)
                let base_idx = (chain_len > 1)
                    .then(|| (back as usize) % chain_len)
                    .filter(|&i| i < chain_len - 1);
                let mut tree = match base_idx {
                    Some(i) => {
                        let seg = self.imp.wts[w].chain[i].0;
                        self.git.commits[self.imp.segs[seg].head_commit.unwrap()].clone()
                    }
                    None => BTreeMap::new(),
                };
                if deleted {
                    tree.remove(&k);
                } else {
                    let v = self.ver();
                    tree.insert(k, v);
                }
                let c = self.git.commit(tree.clone());
                self.imp.rebuild(w, base_idx, &tree, c);
                self.model.wts[w].committed = tree;
            }
            Op::Delete(n) => {
                let candidates: Vec<Wt> = (1..self.model.wts.len())
                    .filter(|&w| self.model.wts[w].alive)
                    .collect();
                if candidates.is_empty() {
                    return;
                }
                let w = candidates[n as usize % candidates.len()];
                self.imp.delete_worktree(w);
                self.model.wts[w].alive = false;
            }
        }
    }

    fn check(&self, step: usize, op: &Op) {
        for w in 0..self.model.wts.len() {
            let m = &self.model.wts[w];
            if !m.alive {
                continue;
            }
            let rows = self.imp.visible(&self.imp.own_view(w), None);
            let mut keys = BTreeSet::new();
            for r in &rows {
                assert!(
                    keys.insert(self.imp.rows[r].key),
                    "step {step} ({op:?}): I3 broken in worktree {w}'s view, key {}",
                    self.imp.rows[r].key
                );
            }
            let live: BTreeMap<Key, Ver> = rows
                .iter()
                .map(|r| &self.imp.rows[r])
                .filter(|r| !r.deleted)
                .map(|r| (r.key, r.ver))
                .collect();
            assert_eq!(live, m.view(), "step {step} ({op:?}): worktree {w}'s view");
            if !m.user {
                continue;
            }
            // this user's branch over main, alone and under each other user's
            let others: Vec<Wt> = (0..self.model.wts.len())
                .filter(|&v| v != w && self.model.wts[v].alive && self.model.wts[v].user)
                .collect();
            let stacks = std::iter::once(vec![w]).chain(others.iter().map(|&v| vec![v, w]));
            for stack in stacks {
                let rows: Vec<&Row> = self
                    .imp
                    .visible(&self.imp.layered(MAIN, &stack), None)
                    .iter()
                    .map(|r| &self.imp.rows[r])
                    .filter(|r| !r.deleted)
                    .collect();
                let layered: BTreeSet<(Key, Ver)> = rows.iter().map(|r| (r.key, r.ver)).collect();
                // Never lost: every row the model shows is there. An extra
                // copy is an over-report, allowed only where main shows a
                // version the user's edit was made over in a row a split
                // re-created from git, which the user's entry can't reach.
                let expected = self.model.layered(&stack);
                assert!(
                    expected.is_subset(&layered),
                    "step {step} ({op:?}): main + {stack:?} lost rows\n  got: {layered:?}\n want: {expected:?}"
                );
                for &(k, v) in layered.difference(&expected) {
                    let recreated = rows.iter().any(|r| r.key == k && r.ver == v && r.recreated);
                    let edited_over = stack.iter().any(|&u| {
                        self.model.wts[u]
                            .draft
                            .get(&k)
                            .is_some_and(|d| d.based_on.contains(&v))
                    });
                    assert!(
                        recreated && edited_over,
                        "step {step} ({op:?}): main + {stack:?} shows ({k}, {v}), which no edit in the stack was made over\n  got: {layered:?}\n want: {expected:?}"
                    );
                }
            }
        }
    }
}

fn run(ops: &[Op]) {
    let mut world = World::new();
    world.check(0, &Op::NewUser);
    for (i, op) in ops.iter().enumerate() {
        world.apply(op);
        world.check(i + 1, op);
    }
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(2000))]

    #[test]
    fn segments_agree_with_the_model(ops in prop::collection::vec(op(), 1..60)) {
        run(&ops);
    }
}

/// A key changed before a split point and back after it, with a user branch
/// that edited over the original version: the split's restored row has to
/// stay hidden from that user.
#[test]
fn split_restores_a_row_with_its_entries() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 1, false),
        Op::External(0, vec![(1, false)]),
        Op::External(0, vec![(1, true)]),
        Op::External(0, vec![(1, false)]),
        Op::Fork(0, 2),
    ]);
}
