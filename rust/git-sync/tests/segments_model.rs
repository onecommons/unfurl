//! Model test for the segment design (docs/branch-segments.md).
//!
//! [`Segments`] is an in-memory implementation of the design's storage:
//! segments, immutable row versions and supersession entries, with the
//! operations of §4 — writes, scans, the commit fold, forks and splits,
//! publishing a user branch, deletion and compaction.
//!
//! [`Model`] is a reference that knows nothing about segments: each
//! worktree's committed tree, its working tree, its draft, its conflicts,
//! and the versions each draft edit was made over. After every step of a
//! random history, every worktree's own view, its committed segments and
//! its conflicts must agree with it exactly. A user branch's layered view
//! must never lose a row the model shows, and may only add a copy of a
//! version the user's edit was made over: copies are over-reported, never
//! lost.
//!
//! Versions are shared ids: the harness hands both sides the same id for
//! each edit, so views compare as sets of `(key, version)`, and a version
//! stands for its content. Tombstones aren't compared, only what they
//! hide. Keys are grouped into files, which is what a scan's draft side
//! works on.

use std::collections::{BTreeMap, BTreeSet};

use proptest::prelude::*;

type Key = u8;
type Ver = u64;
type RowId = u64;
type SegId = usize;
type Wt = usize;
type CommitId = usize;

const MAIN: Wt = 0;
const KEYS: Key = 6;
const KEYS_PER_FILE: Key = 3;
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

fn file_of(key: Key) -> Key {
    key / KEYS_PER_FILE
}

fn keys_of(file: Key) -> std::ops::Range<Key> {
    file * KEYS_PER_FILE..(file + 1) * KEYS_PER_FILE
}

/// What a client's edit knows beyond its value.
#[derive(Clone, Copy, Debug, PartialEq)]
struct Edit {
    /// The committed value it was made over (`base_commit_id`); `None`
    /// for a create, or an edit of an uncommitted row.
    base: Option<Ver>,
    /// For a delete, the value it removes (a tombstone's json).
    gone: Option<Ver>,
}

/// Today's three-way decision (`conflict::classify_conflict`), over
/// versions: does the file's value `theirs` diverge from a client's edit?
/// Shared by both sides: what's under test is what each hands it.
fn diverges(ver: Ver, deleted: bool, edit: Edit, theirs: Option<Ver>) -> bool {
    let ours = if deleted { edit.gone } else { Some(ver) };
    match theirs {
        Some(t) if Some(t) == ours => false,
        Some(_) if deleted => true,
        Some(t) => edit.base != Some(t),
        None => !deleted && edit.base.is_some(),
    }
}

/// Where a scan lets the file's value win over a pending edit.
#[derive(Clone, Copy, Debug, PartialEq)]
enum FileWins {
    Never,
    /// `ScanOptions::force`: everywhere, standing resolutions included.
    Always,
    /// A `Git-Sync-Resolves-Version` trailer: where the edit diverges and
    /// no resolution stands.
    Diverged,
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
    /// A draft row a client wrote. A draft row without one was taken in
    /// from the working tree, and isn't a pending edit.
    edit: Option<Edit>,
    /// A conflict row, the file's side, and whether it's resolved. Never
    /// visible and never superseding.
    conflict: Option<bool>,
    /// The commit that wrote this version (`commit_id`); `None` in a draft.
    commit: Option<CommitId>,
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
                edit: None,
                conflict: None,
                commit: None,
            },
        );
        id
    }

    fn stamp(&mut self, row: RowId, commit: CommitId) {
        self.rows.get_mut(&row).unwrap().commit = Some(commit);
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

    /// A segment's rows, conflict rows excluded.
    fn rows_in(&self, seg: SegId) -> Vec<RowId> {
        self.rows
            .iter()
            .filter(|(_, r)| r.seg == seg && r.conflict.is_none())
            .map(|(id, _)| *id)
            .collect()
    }

    fn row_in(&self, seg: SegId, key: Key) -> Option<RowId> {
        self.rows
            .iter()
            .find(|(_, r)| r.seg == seg && r.key == key && r.conflict.is_none())
            .map(|(id, _)| *id)
    }

    fn conflict_row(&self, seg: SegId, key: Key) -> Option<RowId> {
        self.rows
            .iter()
            .find(|(_, r)| r.seg == seg && r.key == key && r.conflict.is_some())
            .map(|(id, _)| *id)
    }

    /// `w`'s conflicts: key → (the file's value, resolved).
    fn conflicts(&self, w: Wt) -> BTreeMap<Key, (Option<Ver>, bool)> {
        let d = self.wts[w].draft;
        self.rows
            .values()
            .filter(|r| r.seg == d)
            .filter_map(|r| {
                let theirs = (!r.deleted).then_some(r.ver);
                r.conflict.map(|resolved| (r.key, (theirs, resolved)))
            })
            .collect()
    }

    fn set_conflict(&mut self, w: Wt, key: Key, theirs: Option<Ver>) {
        self.drop_conflict(w, key);
        let d = self.wts[w].draft;
        let id = self.insert_value(d, key, theirs);
        self.rows.get_mut(&id).unwrap().conflict = Some(false);
    }

    fn drop_conflict(&mut self, w: Wt, key: Key) {
        if let Some(c) = self.conflict_row(self.wts[w].draft, key) {
            self.delete_row(c);
        }
    }

    /// A resolved conflict row still describing the file: the resolution
    /// stands.
    fn resolution_stands(&self, w: Wt, key: Key, theirs: Option<Ver>) -> bool {
        self.conflict_row(self.wts[w].draft, key).is_some_and(|c| {
            let r = &self.rows[&c];
            r.conflict == Some(true) && (!r.deleted).then_some(r.ver) == theirs
        })
    }

    /// Record, refresh or drop the conflict between `w`'s pending edit
    /// `x` and the file's value.
    fn settle(&mut self, w: Wt, x: RowId, theirs: Option<Ver>) {
        let r = &self.rows[&x];
        let key = r.key;
        if self.resolution_stands(w, key, theirs) {
            return;
        }
        if diverges(r.ver, r.deleted, r.edit.unwrap(), theirs) {
            self.set_conflict(w, key, theirs);
        } else {
            self.drop_conflict(w, key);
        }
    }

    /// Whether another worktree's draft holds `key`. While one does, a
    /// record `w` deletes leaves a tombstone, which that worktree's later
    /// edits supersede: that's how a publish tells an edit made before the
    /// deletion from one made after it.
    fn held_elsewhere(&self, key: Key, w: Wt) -> bool {
        self.rows.values().any(|r| {
            r.key == key
                && r.conflict.is_none()
                && self.segs[r.seg].kind == Kind::Draft
                && self.segs[r.seg].owner != Some(w)
        })
    }

    /// The entries other worktrees' drafts hold on `rows`, with the
    /// content (`None`: a tombstone) of the row each is on.
    fn draft_entries(&self, rows: &[RowId], w: Wt) -> Vec<(Option<Ver>, SegId)> {
        rows.iter()
            .flat_map(|&r| {
                let content = (!self.rows[&r].deleted).then_some(self.rows[&r].ver);
                self.sup
                    .range((r, 0)..(r + 1, 0))
                    .filter(|&&(_, s)| {
                        self.segs[s].kind == Kind::Draft && self.segs[s].owner != Some(w)
                    })
                    .map(move |&(_, s)| (content, s))
            })
            .collect()
    }

    /// A row replacing others with the same content takes over their
    /// entries: an edit made over that content was made over it too. Only
    /// from the rows the view showed just before: an older row with the
    /// same content predates changes the edit may have seen since.
    fn carry(&mut self, entries: Vec<(Option<Ver>, SegId)>, to: RowId) {
        let content = (!self.rows[&to].deleted).then_some(self.rows[&to].ver);
        for (c, s) in entries {
            if c == content {
                self.sup.insert((to, s));
            }
        }
    }

    /// The live version `view` shows for `key`.
    fn live(&self, view: &BTreeSet<SegId>, key: Key) -> Option<Ver> {
        self.visible(view, Some(key))
            .into_iter()
            .map(|r| &self.rows[&r])
            .find(|r| !r.deleted)
            .map(|r| r.ver)
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
                r.conflict.is_none()
                    && view.contains(&r.seg)
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

    /// §4.2: a write into `w`'s draft, made through `via`: a client's edit,
    /// or with `edit` false, a value taken in from the working tree.
    fn write(
        &mut self,
        w: Wt,
        via: &BTreeSet<SegId>,
        key: Key,
        ver: Ver,
        deleted: bool,
        edit: bool,
    ) {
        let d = self.wts[w].draft;
        let own = self.own_view(w);
        let prior = self.row_in(d, key);
        // the committed value the edit is made over, as `via` shows it: an
        // uncommitted row there (a draft's) gives it none
        let committed = || {
            let shown = self.visible(via, Some(key));
            shown
                .iter()
                .all(|r| self.segs[self.rows[r].seg].kind != Kind::Draft)
                .then(|| self.live(via, key))
                .flatten()
        };
        let meta = edit.then(|| Edit {
            base: match prior {
                Some(p) => self.rows[&p].edit.and_then(|e| e.base),
                None => committed(),
            },
            gone: if deleted { self.live(&own, key) } else { None },
        });
        let mut seen: BTreeSet<RowId> = self.visible(via, Some(key)).into_iter().collect();
        seen.extend(self.visible(&own, Some(key)));
        let entries = self.draft_entries(&seen.iter().copied().collect::<Vec<_>>(), w);
        if let Some(old) = prior {
            seen.remove(&old);
            self.delete_row(old);
        }
        let id = self.insert(d, key, ver, deleted);
        self.rows.get_mut(&id).unwrap().edit = meta;
        self.carry(entries, id);
        let seen: Vec<RowId> = seen.into_iter().collect();
        for r in seen {
            self.sup.insert((r, d));
        }
    }

    /// Take `key` out of `w`'s draft, and the draft's entries for it.
    fn remove_draft_row(&mut self, w: Wt, key: Key) {
        let d = self.wts[w].draft;
        if let Some(x) = self.row_in(d, key) {
            self.delete_row(x);
        }
        let stale: Vec<(RowId, SegId)> = self
            .sup
            .iter()
            .filter(|&&(r, s)| s == d && self.rows[&r].key == key)
            .copied()
            .collect();
        for e in stale {
            self.sup.remove(&e);
        }
    }

    /// §4.3's draft side, for one file of `w`'s working tree.
    fn reconcile(&mut self, w: Wt, file: Key, disk: &BTreeMap<Key, Ver>, wins: FileWins) {
        let d = self.wts[w].draft;
        for key in keys_of(file) {
            let theirs = disk.get(&key).copied();
            let mut x = self.row_in(d, key);
            let mut forced = false;
            if let Some(p) = x.filter(|x| self.rows[x].edit.is_some()) {
                let r = &self.rows[&p];
                let file_wins = match wins {
                    FileWins::Never => false,
                    FileWins::Always => true,
                    FileWins::Diverged => {
                        !self.resolution_stands(w, key, theirs)
                            && diverges(r.ver, r.deleted, r.edit.unwrap(), theirs)
                    }
                };
                if !file_wins {
                    self.settle(w, p, theirs);
                    continue;
                }
                self.remove_draft_row(w, key);
                x = None;
                forced = true;
            }
            self.drop_conflict(w, key);
            if theirs == self.live(&self.chain_set(w), key) {
                if x.is_some() {
                    self.remove_draft_row(w, key);
                }
            } else if x.map(|x| (!self.rows[&x].deleted).then_some(self.rows[&x].ver))
                != Some(theirs)
            {
                let own = self.own_view(w);
                let ver = theirs.unwrap_or_else(|| self.private_ver());
                self.write(w, &own, key, ver, theirs.is_none(), false);
                // over a withdrawn edit: the file's value again, in a new row
                if forced {
                    let r = self.row_in(d, key).unwrap();
                    self.recreated(r);
                }
            }
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
    /// these changes (`None`: deleted), such as a merge, which the working
    /// tree `disk` holds too.
    fn scan(
        &mut self,
        w: Wt,
        changes: &[(Key, Option<Ver>)],
        commit: CommitId,
        disk: &BTreeMap<Key, Ver>,
    ) {
        for &(key, change) in changes {
            self.scan_key(w, key, change, commit);
        }
        let files: BTreeSet<Key> = changes.iter().map(|&(k, _)| file_of(k)).collect();
        for f in files {
            self.reconcile(w, f, disk, FileWins::Never);
        }
        let h = self.wts[w].head;
        self.segs[h].head_commit = Some(commit);
        self.wts[w].history.push(commit);
        self.relink_draft(w);
    }

    fn scan_key(&mut self, w: Wt, key: Key, change: Option<Ver>, commit: CommitId) {
        let h = self.wts[w].head;
        let mut below = self.chain_set(w);
        below.remove(&h);
        let vis_below = self.visible(&below, Some(key));
        let head_row = self.row_in(h, key);
        let pinned = head_row.is_some() && self.held_elsewhere(key, w);
        let entries = self.draft_entries(head_row.as_slice(), w);
        if let Some(hr) = head_row {
            self.delete_row(hr);
        }
        let new_row = match change {
            Some(v) => Some(self.insert(h, key, v, false)),
            None if pinned || vis_below.iter().any(|r| !self.rows[r].deleted) => {
                let t = self.private_ver();
                Some(self.insert(h, key, t, true))
            }
            None => None,
        };
        if let Some(n) = new_row {
            self.stamp(n, commit);
            self.carry(entries, n);
            if head_row.is_none() {
                let entries = self.draft_entries(&vis_below, w);
                self.carry(entries, n);
                for r in vis_below {
                    self.sup.insert((r, h));
                }
            }
        }
    }

    /// §4.4: render `w`'s view into its working tree `disk`, commit the
    /// result, and fold. Returns the tree, or `None` when there's nothing
    /// to commit.
    fn commit(
        &mut self,
        w: Wt,
        disk: &BTreeMap<Key, Ver>,
        git: &mut Git,
    ) -> Option<BTreeMap<Key, Ver>> {
        let (d, h) = (self.wts[w].draft, self.wts[w].head);
        let mut tree = disk.clone();
        let mut carried = Vec::new();
        for x in self.rows_in(d) {
            let r = self.rows[&x].clone();
            let theirs = disk.get(&r.key).copied();
            let apply = match (r.edit, self.conflict_row(d, r.key)) {
                // taken in from the working tree: already there
                (None, _) => {
                    carried.push(x);
                    continue;
                }
                (Some(_), None) => true,
                (Some(_), Some(c)) if self.rows[&c].conflict == Some(true) => {
                    if self.resolution_stands(w, r.key, theirs) {
                        self.delete_row(c);
                        true
                    } else {
                        // the file moved under the resolution
                        self.set_conflict(w, r.key, theirs);
                        false
                    }
                }
                (Some(_), Some(_)) => false,
            };
            if apply {
                if r.deleted {
                    tree.remove(&r.key);
                } else {
                    tree.insert(r.key, r.ver);
                }
                carried.push(x);
            }
        }
        if tree == git.commits[*self.wts[w].history.last().unwrap()] {
            return None;
        }
        let commit = git.commit(tree.clone());
        for x in carried {
            let k = self.rows[&x].key;
            // the committed row it replaces, in the head or below
            let shown = self.visible(&self.chain_set(w), Some(k));
            let entries = self.draft_entries(&shown, w);
            if let Some(hr) = self.row_in(h, k) {
                self.delete_row(hr);
            }
            self.carry(entries, x);
            self.move_entries(d, h, k);
            let row = self.rows.get_mut(&x).unwrap();
            row.seg = h;
            row.edit = None;
            row.commit = Some(commit);
            let hides = self
                .sup
                .iter()
                .any(|&(r, s)| s == h && self.rows[&r].key == k);
            if self.rows[&x].deleted && !hides && !self.held_elsewhere(k, w) {
                self.delete_row(x);
            }
        }
        // Step 4: under an edit held back by a conflict, the head takes
        // the file's value.
        for x in self.rows_in(d) {
            let k = self.rows[&x].key;
            let file = tree.get(&k).copied();
            if self.live(&self.chain_set(w), k) != file {
                self.scan_key(w, k, file, commit);
                if let Some(r) = self.row_in(h, k) {
                    self.recreated(r);
                }
            }
        }
        self.segs[h].head_commit = Some(commit);
        self.wts[w].history.push(commit);
        self.relink_draft(w);
        Some(tree)
    }

    /// Settle a conflict for the file's side: the edit is withdrawn, and
    /// what the working tree holds (`None` for a user branch) stands.
    fn resolve_theirs(&mut self, w: Wt, key: Key, disk: Option<&BTreeMap<Key, Ver>>) {
        self.drop_conflict(w, key);
        self.remove_draft_row(w, key);
        if let Some(disk) = disk {
            self.reconcile(w, file_of(key), disk, FileWins::Never);
            // the file's value again, in a new row
            if let Some(r) = self.row_in(self.wts[w].draft, key) {
                self.recreated(r);
            }
        }
    }

    fn resolve_ours(&mut self, w: Wt, key: Key) {
        let c = self.conflict_row(self.wts[w].draft, key).unwrap();
        self.rows.get_mut(&c).unwrap().conflict = Some(true);
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
        // keys that differ, and rows written after `c` whatever their value:
        // a tombstone kept for another draft's key changes nothing in git
        let keys: BTreeSet<Key> = tree_c
            .keys()
            .chain(tree_h.keys())
            .copied()
            .filter(|k| tree_c.get(k) != tree_h.get(k))
            .chain(
                self.rows_in(s)
                    .into_iter()
                    .map(|r| &self.rows[&r])
                    .filter(|r| r.commit > Some(c))
                    .map(|r| r.key),
            )
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
                        self.stamp(new, c);
                        self.recreated(new);
                        self.sup.insert((new, s2));
                    } else {
                        self.move_entries(s, s2, k);
                    }
                }
                None => {
                    // changed before c and back after it
                    let new = self.insert_value(s, k, v_c);
                    self.stamp(new, c);
                    self.recreated(new);
                    for &x in &vis_below {
                        self.sup.insert((x, s));
                    }
                    // `s` may hide the row below by an entry alone (a fold
                    // took the tombstone), so restore git's value at its end
                    let v_h = tree_h.get(&k).copied();
                    let copy = vis_below
                        .first()
                        .copied()
                        .filter(|b| (!self.rows[b].deleted).then_some(self.rows[b].ver) == v_h);
                    let r2 = match copy {
                        Some(b) => {
                            let (ver, deleted) = (self.rows[&b].ver, self.rows[&b].deleted);
                            self.insert(s2, k, ver, deleted)
                        }
                        None => self.insert_value(s2, k, v_h),
                    };
                    self.stamp(r2, h_commit);
                    self.recreated(r2);
                    // The restored row stands for a version older than
                    // anything a segment outside `s` and its ancestors
                    // holds for the key, so each of those supersedes it. A
                    // restored deletion is no newer than a draft holding the
                    // key: one held then would have kept a tombstone.
                    let newer: BTreeSet<SegId> = self
                        .rows
                        .values()
                        .filter(|r| {
                            r.conflict.is_none()
                                && r.key == k
                                && r.seg != s
                                && r.seg != s2
                                && !below.contains(&r.seg)
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
        // §4.12: classify only the edits whose draft doesn't already
        // supersede what the new chain shows; main's value is theirs
        let (d, view) = (self.wts[u].draft, self.chain_set(u));
        for x in self.rows_in(d) {
            let k = self.rows[&x].key;
            let unseen: Vec<RowId> = self
                .visible(&view, Some(k))
                .into_iter()
                .filter(|&r| !self.sup.contains(&(r, d)))
                .collect();
            if unseen.is_empty() {
                // the edit is on top of main's value: a conflict with any
                // other is over
                let main = self.live(&view, k);
                if let Some(c) = self.conflict_row(d, k) {
                    if (!self.rows[&c].deleted).then_some(self.rows[&c].ver) != main {
                        self.delete_row(c);
                    }
                }
                continue;
            }
            let theirs = unseen
                .iter()
                .map(|r| &self.rows[r])
                .find(|r| !r.deleted)
                .map(|r| r.ver);
            self.settle(u, x, theirs);
        }
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
        let old_view = self.chain_set(w);
        let hn = self.new_seg(Kind::Head, base, Some(w), Some(commit));
        for k in 0..KEYS {
            let vis = self.visible(&view, Some(k));
            let live = vis
                .iter()
                .find(|r| !self.rows[r].deleted)
                .map(|r| self.rows[r].ver);
            let want = tree.get(&k).copied();
            let pinned =
                || !self.visible(&old_view, Some(k)).is_empty() && self.held_elsewhere(k, w);
            if want == live && (want.is_some() || !pinned()) {
                continue;
            }
            let n = self.insert_value(hn, k, want);
            self.stamp(n, commit);
            let old = self.visible(&old_view, Some(k));
            let entries = self.draft_entries(&old, w);
            self.carry(entries, n);
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
        let rows: Vec<RowId> = self
            .rows
            .iter()
            .filter(|(_, r)| r.seg == s)
            .map(|(id, _)| *id)
            .collect();
        for r in rows {
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

#[derive(Clone, Copy, Debug)]
enum Origin {
    /// Taken in from the working tree.
    File,
    Edit(Edit),
}

#[derive(Clone, Debug)]
struct ODraft {
    ver: Ver,
    deleted: bool,
    /// The versions this edit was made over, and the ids of main's
    /// absences ([`Model::absent`]) it was made over.
    based_on: BTreeSet<Ver>,
    origin: Origin,
    /// For a tombstone main writes over an absence, that absence's ids
    /// ([`Model::absent`]): committed, it continues it.
    continues: BTreeSet<Ver>,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct OConflict {
    theirs: Option<Ver>,
    resolved: bool,
}

#[derive(Clone, Debug)]
struct OWt {
    committed: BTreeMap<Key, Ver>,
    /// The working tree; a user branch has none, and this is unused.
    disk: BTreeMap<Key, Ver>,
    draft: BTreeMap<Key, ODraft>,
    conflicts: BTreeMap<Key, OConflict>,
    /// A user branch's view of main's absences ([`Model::absent`]), as of
    /// its fork or last publish: its own view shows those tombstones.
    absent: BTreeMap<Key, BTreeSet<Ver>>,
    user: bool,
    alive: bool,
}

impl OWt {
    fn new(committed: BTreeMap<Key, Ver>, user: bool) -> Self {
        OWt {
            disk: committed.clone(),
            committed,
            draft: BTreeMap::new(),
            conflicts: BTreeMap::new(),
            absent: BTreeMap::new(),
            user,
            alive: true,
        }
    }

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

    /// A client's edit of `k`, made over `shown`, the committed value the
    /// view it's made through shows; `based_on` matters only for a user
    /// branch.
    fn edit(
        &mut self,
        k: Key,
        ver: Ver,
        deleted: bool,
        shown: Option<Ver>,
        based_on: BTreeSet<Ver>,
    ) {
        let base = match self.draft.get(&k) {
            Some(ODraft {
                origin: Origin::Edit(e),
                ..
            }) => e.base,
            Some(_) => None,
            None => shown,
        };
        let gone = if deleted {
            self.view().get(&k).copied()
        } else {
            None
        };
        self.draft.insert(
            k,
            ODraft {
                ver,
                deleted,
                based_on,
                origin: Origin::Edit(Edit { base, gone }),
                continues: BTreeSet::new(),
            },
        );
    }

    /// The conflict between the edit of `k` and the file's value.
    fn settle(&mut self, k: Key, theirs: Option<Ver>) {
        if self
            .conflicts
            .get(&k)
            .is_some_and(|c| c.resolved && c.theirs == theirs)
        {
            return;
        }
        let d = &self.draft[&k];
        let Origin::Edit(e) = d.origin else {
            unreachable!("only edits conflict")
        };
        if diverges(d.ver, d.deleted, e, theirs) {
            self.conflicts.insert(
                k,
                OConflict {
                    theirs,
                    resolved: false,
                },
            );
        } else {
            self.conflicts.remove(&k);
        }
    }

    /// A scan's draft side for one file. A tombstone taken in from the
    /// file gets a fresh id from `next`.
    fn reconcile(&mut self, file: Key, next: &mut Ver, wins: FileWins) {
        for k in keys_of(file) {
            let theirs = self.disk.get(&k).copied();
            if let Some(d) = self.draft.get(&k).filter(|d| d.origin.is_edit()) {
                let Origin::Edit(e) = d.origin else {
                    unreachable!("filtered to edits")
                };
                let stands = self
                    .conflicts
                    .get(&k)
                    .is_some_and(|c| c.resolved && c.theirs == theirs);
                let file_wins = match wins {
                    FileWins::Never => false,
                    FileWins::Always => true,
                    FileWins::Diverged => !stands && diverges(d.ver, d.deleted, e, theirs),
                };
                if !file_wins {
                    self.settle(k, theirs);
                    continue;
                }
                self.draft.remove(&k);
            }
            self.conflicts.remove(&k);
            let holds = self.draft.get(&k).is_some_and(|d| {
                if d.deleted {
                    theirs.is_none()
                } else {
                    theirs == Some(d.ver)
                }
            });
            if theirs == self.committed.get(&k).copied() {
                self.draft.remove(&k);
            } else if !holds {
                let ver = theirs.unwrap_or_else(|| {
                    *next += 1;
                    *next
                });
                self.draft.insert(
                    k,
                    ODraft {
                        ver,
                        deleted: theirs.is_none(),
                        based_on: BTreeSet::new(),
                        origin: Origin::File,
                        continues: BTreeSet::new(),
                    },
                );
            }
        }
    }

    /// Render the draft into the working tree and commit it: the tree, or
    /// `None` when that changes nothing.
    fn commit(&mut self) -> Option<BTreeMap<Key, Ver>> {
        let mut tree = self.disk.clone();
        let mut carried = Vec::new();
        for (&k, d) in &self.draft {
            let theirs = self.disk.get(&k).copied();
            let apply = match (d.origin, self.conflicts.get(&k)) {
                (Origin::File, _) => {
                    carried.push(k);
                    continue;
                }
                (Origin::Edit(_), None) => true,
                (Origin::Edit(_), Some(c)) if c.resolved => c.theirs == theirs,
                (Origin::Edit(_), Some(_)) => false,
            };
            if apply {
                if d.deleted {
                    tree.remove(&k);
                } else {
                    tree.insert(k, d.ver);
                }
                carried.push(k);
            }
        }
        for (&k, c) in self.conflicts.iter_mut() {
            if c.resolved {
                let theirs = self.disk.get(&k).copied();
                if c.theirs != theirs {
                    *c = OConflict {
                        theirs,
                        resolved: false,
                    };
                }
            }
        }
        for k in &carried {
            if self.draft[k].origin.is_edit() {
                self.conflicts.remove(k);
            }
        }
        if tree == self.committed {
            return None;
        }
        for k in carried {
            self.draft.remove(&k);
        }
        self.committed = tree.clone();
        self.disk = tree.clone();
        Some(tree)
    }
}

impl Origin {
    fn is_edit(&self) -> bool {
        matches!(self, Origin::Edit(_))
    }
}

struct Model {
    wts: Vec<OWt>,
    /// For each key main's committed tree lacks, ids for that absence: the
    /// version of each tombstone main committed for it, or a fresh id when
    /// a scan or rebuild removed it. They last while the key stays absent.
    absent: BTreeMap<Key, BTreeSet<Ver>>,
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
    /// A commit made outside the database and checked out, then scanned:
    /// one or more changes, as a merge brings.
    External(u8, Vec<(Key, bool)>),
    /// A hand edit of a worktree's working tree, then scanned, letting the
    /// file win where `.3` says.
    DiskEdit(u8, Key, bool, FileWins),
    /// Fork main or a fork, `.1` commits back from its head.
    Fork(u8, u8),
    NewUser,
    /// A write by a user through main, the other users' branches listed
    /// (a stack), and their own on top.
    UserWrite(u8, Vec<u8>, Key, bool),
    Publish(u8),
    /// Resolve one of a worktree's conflicts (a user's, with `.1`), for
    /// the database's side (`.3`) or the file's.
    Resolve(u8, bool, u8, bool),
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
        3 => (any::<u8>(), key.clone(), prop::bool::weighted(0.2), prop_oneof![6 => Just(FileWins::Never), 1 => Just(FileWins::Always), 1 => Just(FileWins::Diverged)]).prop_map(|(w, k, d, f)| Op::DiskEdit(w, k, d, f)),
        1 => (any::<u8>(), 0u8..4).prop_map(|(w, b)| Op::Fork(w, b)),
        1 => Just(Op::NewUser),
        4 => (any::<u8>(), prop::collection::vec(any::<u8>(), 0..3), key.clone(), prop::bool::weighted(0.2)).prop_map(|(u, b, k, d)| Op::UserWrite(u, b, k, d)),
        1 => any::<u8>().prop_map(Op::Publish),
        2 => (any::<u8>(), any::<bool>(), any::<u8>(), any::<bool>()).prop_map(|(w, u, i, o)| Op::Resolve(w, u, i, o)),
        1 => any::<u8>().prop_map(Op::Delete),
        1 => (any::<u8>(), any::<u8>(), key, prop::bool::weighted(0.2)).prop_map(|(w, b, k, d)| Op::Rebuild(w, b, k, d)),
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
            let r = imp.insert(root, k, v, false);
            imp.stamp(r, c0);
        }
        imp.add_worktree(root, vec![(root, false)], vec![c0]);
        World {
            imp,
            model: Model {
                wts: vec![OWt::new(tree, false)],
                absent: BTreeMap::new(),
            },
            git,
            next_ver: KEYS as Ver + 1,
        }
    }

    fn ver(&mut self) -> Ver {
        self.next_ver += 1;
        self.next_ver
    }

    /// Main's committed tree moved from `before`; `tombstones` are the
    /// ids of the draft tombstones its commit carried.
    fn main_moved(
        &mut self,
        before: &BTreeMap<Key, Ver>,
        tombstones: &BTreeMap<Key, BTreeSet<Ver>>,
    ) {
        for k in 0..KEYS {
            if self.model.wts[MAIN].committed.contains_key(&k) {
                self.model.absent.remove(&k);
                continue;
            }
            let newly = before.contains_key(&k) || !self.model.absent.contains_key(&k);
            let new_ids = match tombstones.get(&k) {
                Some(t) => t.clone(),
                None if newly => BTreeSet::from([self.ver()]),
                None => continue,
            };
            let ids = self.model.absent.entry(k).or_default();
            if newly {
                ids.clear();
            }
            ids.extend(new_ids);
        }
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
                self.imp.write(w, &own, k, ver, deleted, true);
                // a tombstone over main's absence continues it
                let continues = match (w, deleted, self.model.wts[w].shown(k)) {
                    (MAIN, true, None) => self.model.absent[&k].clone(),
                    (MAIN, true, Some((t, true))) => {
                        let mut ids = self.model.wts[w].draft[&k].continues.clone();
                        ids.insert(t);
                        ids
                    }
                    _ => BTreeSet::new(),
                };
                let m = &mut self.model.wts[w];
                let shown = m.committed.get(&k).copied();
                m.edit(k, ver, deleted, shown, BTreeSet::new());
                m.draft.get_mut(&k).unwrap().continues = continues;
            }
            Op::Commit(n) => {
                let w = self.pick(n, false).unwrap();
                let m = &self.model.wts[w];
                let (disk, before) = (m.disk.clone(), m.committed.clone());
                let tombstones: BTreeMap<Key, BTreeSet<Ver>> = m
                    .draft
                    .iter()
                    .filter(|(_, d)| d.deleted)
                    .map(|(&k, d)| {
                        let mut ids = d.continues.clone();
                        ids.insert(d.ver);
                        (k, ids)
                    })
                    .collect();
                let got = self.imp.commit(w, &disk, &mut self.git);
                let want = self.model.wts[w].commit();
                assert_eq!(got, want, "worktree {w}'s commit");
                if w == MAIN && want.is_some() {
                    // only those the commit carried; a conflict holds some back
                    let draft = &self.model.wts[w].draft;
                    let carried: BTreeMap<Key, BTreeSet<Ver>> = tombstones
                        .into_iter()
                        .filter(|(k, _)| !draft.contains_key(k))
                        .collect();
                    self.main_moved(&before, &carried);
                }
            }
            Op::External(n, ref changes) => {
                let w = self.pick(n, false).unwrap();
                let before = self.model.wts[w].committed.clone();
                let changes: BTreeMap<Key, Option<Ver>> = changes
                    .iter()
                    .map(|&(k, deleted)| (k, (!deleted).then(|| self.ver())))
                    .collect();
                let m = &mut self.model.wts[w];
                for (&k, &change) in &changes {
                    match change {
                        Some(v) => {
                            m.committed.insert(k, v);
                            m.disk.insert(k, v);
                        }
                        None => {
                            m.committed.remove(&k);
                            m.disk.remove(&k);
                        }
                    };
                }
                let files: BTreeSet<Key> = changes.keys().map(|&k| file_of(k)).collect();
                for &f in &files {
                    m.reconcile(f, &mut self.next_ver, FileWins::Never);
                }
                let c = self.git.commit(m.committed.clone());
                let disk = m.disk.clone();
                let changes: Vec<(Key, Option<Ver>)> = changes.into_iter().collect();
                self.imp.scan(w, &changes, c, &disk);
                if w == MAIN {
                    self.main_moved(&before, &BTreeMap::new());
                }
            }
            Op::DiskEdit(n, k, deleted, wins) => {
                let w = self.pick(n, false).unwrap();
                let ver = self.ver();
                let m = &mut self.model.wts[w];
                if deleted {
                    m.disk.remove(&k);
                } else {
                    m.disk.insert(k, ver);
                }
                m.reconcile(file_of(k), &mut self.next_ver, wins);
                let disk = m.disk.clone();
                self.imp.reconcile(w, file_of(k), &disk, wins);
            }
            Op::Fork(n, back) => {
                let w = self.pick(n, false).unwrap();
                let len = self.imp.wts[w].history.len();
                let pos = len - 1 - (back as usize).min(len - 1);
                let c = self.imp.wts[w].history[pos];
                self.imp.fork(w, pos, &self.git);
                self.model
                    .wts
                    .push(OWt::new(self.git.commits[c].clone(), false));
            }
            Op::NewUser => {
                let pos = self.imp.wts[MAIN].history.len() - 1;
                self.imp.fork(MAIN, pos, &self.git);
                let committed = self.model.wts[MAIN].committed.clone();
                let mut user = OWt::new(committed, true);
                user.absent = self.model.absent.clone();
                self.model.wts.push(user);
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
                self.imp.write(u, &via, k, ver, deleted, true);
                let old = self.model.wts[u].draft.get(&k).map(|d| d.ver);
                let rows = self.model.stack_rows(&stack, k);
                let main = &self.model.wts[MAIN];
                // only main's committed row shown: another layer's is a draft's
                let shown = match rows.as_slice() {
                    [(v, false)]
                        if !main.draft.contains_key(&k) && main.committed.get(&k) == Some(v) =>
                    {
                        Some(*v)
                    }
                    _ => None,
                };
                let mut seen: BTreeSet<Ver> = rows
                    .into_iter()
                    .map(|(v, _)| v)
                    .filter(|&v| Some(v) != old)
                    .collect();
                // main's absence, unless an edit lower in the stack hides it
                if self.model.wts[MAIN].shown(k).is_none() {
                    let ids = &self.model.absent[&k];
                    let hidden = stack.iter().filter(|&&v| v != u).any(|&v| {
                        self.model.wts[v]
                            .draft
                            .get(&k)
                            .is_some_and(|d| !d.based_on.is_disjoint(ids))
                    });
                    if !hidden {
                        seen.extend(ids);
                    }
                }
                let user = &mut self.model.wts[u];
                match user.draft.get(&k) {
                    Some(old) => seen.extend(&old.based_on),
                    // what its own view shows, a tombstone included
                    None => match user.committed.get(&k) {
                        Some(&v) => {
                            seen.insert(v);
                        }
                        None => seen.extend(user.absent.get(&k).into_iter().flatten()),
                    },
                }
                user.edit(k, ver, deleted, shown, seen);
            }
            Op::Publish(n) => {
                let Some(u) = self.pick(n, true) else { return };
                self.imp.publish(u);
                let committed = self.model.wts[MAIN].committed.clone();
                let states: BTreeMap<Key, BTreeSet<Ver>> = (0..KEYS)
                    .map(|k| {
                        let ids = match committed.get(&k) {
                            Some(&v) => BTreeSet::from([v]),
                            None => self.model.absent[&k].clone(),
                        };
                        (k, ids)
                    })
                    .collect();
                let user = &mut self.model.wts[u];
                let keys: Vec<Key> = user.draft.keys().copied().collect();
                for k in keys {
                    let main = committed.get(&k).copied();
                    if user.draft[&k].based_on.is_disjoint(&states[&k]) {
                        user.settle(k, main);
                    } else if user.conflicts.get(&k).is_some_and(|c| c.theirs != main) {
                        // on top of main's value: a conflict with any other is over
                        user.conflicts.remove(&k);
                    }
                }
                user.committed = committed;
                user.absent = self.model.absent.clone();
                for (k, d) in user.draft.iter_mut() {
                    d.based_on.extend(&states[k]);
                }
            }
            Op::Resolve(n, user, i, ours) => {
                let Some(w) = self.pick(n, user) else { return };
                let m = &mut self.model.wts[w];
                let keys: Vec<Key> = m.conflicts.keys().copied().collect();
                if keys.is_empty() {
                    return;
                }
                let k = keys[i as usize % keys.len()];
                if ours {
                    m.conflicts.get_mut(&k).unwrap().resolved = true;
                    self.imp.resolve_ours(w, k);
                } else {
                    // Today's rewrites the edit to the file's value; with
                    // versions standing for content, that's modelled as
                    // withdrawing the edit, to the same effect.
                    m.conflicts.remove(&k);
                    m.draft.remove(&k);
                    if !user {
                        m.reconcile(file_of(k), &mut self.next_ver, FileWins::Never);
                    }
                    let disk = (!user).then(|| m.disk.clone());
                    self.imp.resolve_theirs(w, k, disk.as_ref());
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
                // the checkout carries the working tree's own changes over
                let m = &mut self.model.wts[w];
                for k in 0..KEYS {
                    if m.disk.get(&k) == m.committed.get(&k) {
                        match tree.get(&k) {
                            Some(&v) => m.disk.insert(k, v),
                            None => m.disk.remove(&k),
                        };
                    }
                }
                let before = std::mem::replace(&mut m.committed, tree);
                let disk = m.disk.clone();
                for f in 0..KEYS / KEYS_PER_FILE {
                    m.reconcile(f, &mut self.next_ver, FileWins::Never);
                    self.imp.reconcile(w, f, &disk, FileWins::Never);
                }
                if w == MAIN {
                    self.main_moved(&before, &BTreeMap::new());
                }
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
            let live = |rows: Vec<RowId>| -> BTreeMap<Key, Ver> {
                rows.iter()
                    .map(|r| &self.imp.rows[r])
                    .filter(|r| !r.deleted)
                    .map(|r| (r.key, r.ver))
                    .collect()
            };
            assert_eq!(
                live(rows),
                m.view(),
                "step {step} ({op:?}): worktree {w}'s view"
            );
            // the committed segments equal git exactly
            let chain = self.imp.visible(&self.imp.chain_set(w), None);
            assert_eq!(
                live(chain),
                m.committed,
                "step {step} ({op:?}): worktree {w}'s committed chain"
            );
            let want: BTreeMap<Key, (Option<Ver>, bool)> = m
                .conflicts
                .iter()
                .map(|(&k, c)| (k, (c.theirs, c.resolved)))
                .collect();
            // An extra conflict is an over-report, allowed only on a user's
            // edit, against main's committed value in a row re-created with
            // content the edit was made over, which its entry can't reach.
            let got = self.imp.conflicts(w);
            let chain_rows = self.imp.visible(&self.imp.chain_set(w), None);
            for k in got.keys().chain(want.keys()) {
                if got.get(k) == want.get(k) {
                    continue;
                }
                // Missing, only where main's value is in a re-created row
                // that showed as a copy in a stack and this user edited
                // over: they saw it, which the model can't tell.
                let seen_copy = m.user
                    && !got.contains_key(k)
                    && want[k].0.is_some_and(|t| {
                        chain_rows.iter().any(|id| {
                            let r = &self.imp.rows[id];
                            r.key == *k
                                && r.ver == t
                                && r.recreated
                                && self.imp.sup.contains(&(*id, self.imp.wts[w].draft))
                        })
                    });
                let over_report = m.user
                    && !want.contains_key(k)
                    && got[k].0.is_some_and(|t| {
                        m.draft.get(k).is_some_and(|d| d.based_on.contains(&t))
                            && chain_rows.iter().any(|r| {
                                let r = &self.imp.rows[r];
                                r.key == *k && r.ver == t && r.recreated
                            })
                    });
                assert!(
                    over_report || seen_copy,
                    "step {step} ({op:?}): worktree {w}'s conflicts\n  got: {got:?}\n want: {want:?}"
                );
            }
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
                // A row re-created with content a lower edit was made over
                // shows as a copy in that stack; an edit made over the copy
                // hides it, which the model, going by versions, can't tell.
                let main_view = self.imp.own_view(MAIN);
                let lost: Vec<&(Key, Ver)> = expected
                    .difference(&layered)
                    .filter(|&&(k, v)| {
                        !self.imp.visible(&main_view, Some(k)).iter().any(|r| {
                            self.imp.rows[r].ver == v
                                && self.imp.rows[r].recreated
                                && stack
                                    .iter()
                                    .any(|&u| self.imp.sup.contains(&(*r, self.imp.wts[u].draft)))
                        })
                    })
                    .collect();
                assert!(
                    lost.is_empty(),
                    "step {step} ({op:?}): main + {stack:?} lost rows {lost:?}\n  got: {layered:?}\n want: {expected:?}"
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

/// A fold took the tombstone a segment's own deletion superseded, so the
/// segment hides the row below by an entry alone: a split must restore
/// git's value at its end, not that row.
#[test]
fn split_restores_git_value_not_the_row_below() {
    run(&[
        Op::Write(0, 0, false),
        Op::NewUser,
        Op::Fork(0, 0),
        Op::Delete(1),
        Op::Commit(0),
        Op::External(0, vec![(3, true)]),
        Op::NewUser,
        Op::External(0, vec![(0, false)]),
        Op::Write(0, 0, false),
        Op::Commit(0),
        Op::External(0, vec![(3, false)]),
        Op::External(0, vec![(3, true)]),
        Op::Delete(11),
        Op::Fork(0, 1),
        Op::Write(0, 0, false),
    ]);
}

/// Main deletes a record a user edited, leaving a tombstone that changes
/// nothing in git. Splits must move it past the split point, and not let
/// the user's draft supersede its restoration, or the conflict is lost.
#[test]
fn split_keeps_a_later_deletion_newer_than_a_layered_edit() {
    run(&[
        Op::NewUser,
        Op::NewUser,
        Op::Fork(0, 0),
        Op::Rebuild(20, 253, 0, false),
        Op::Delete(0),
        Op::External(6, vec![(3, false)]),
        Op::UserWrite(0, vec![], 3, false),
        Op::Delete(253),
        Op::External(0, vec![(3, true)]),
        Op::Fork(0, 2),
        Op::Fork(14, 1),
        Op::Publish(0),
    ]);
}

/// Main's committed deletion replaces a tombstone below its head that the
/// user saw: the fold must carry the user's entry across, or publishing
/// reads the unchanged absence as new.
#[test]
fn fold_carries_entries_from_the_row_below() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 0, false),
        Op::Rebuild(0, 31, 1, false),
        Op::Write(0, 0, false),
        Op::Publish(0),
        Op::UserWrite(0, vec![], 0, true),
        Op::Write(0, 0, true),
        Op::DiskEdit(0, 1, false, FileWins::Never),
        Op::Commit(0),
        Op::Write(0, 0, false),
        Op::Publish(0),
    ]);
}

/// A split re-creates main's value in a new row, which a stack shows as a
/// copy beside a lower user's edit; the upper user edits over it, so
/// publishing finds no conflict with it.
#[test]
fn stack_copy_edited_over_is_no_conflict() {
    run(&[
        Op::NewUser,
        Op::DiskEdit(0, 4, false, FileWins::Never),
        Op::UserWrite(0, vec![], 4, false),
        Op::Commit(0),
        Op::External(0, vec![(0, false)]),
        Op::Write(0, 4, false),
        Op::Fork(0, 0),
        Op::Delete(31),
        Op::Commit(0),
        Op::Fork(0, 1),
        Op::NewUser,
        Op::Rebuild(14, 33, 0, false),
        Op::UserWrite(5, vec![104], 4, false),
        Op::Publish(47),
    ]);
}

/// A rebuild deletes a record a user edited: it leaves a tombstone, which
/// the user's later edit supersedes, so publishing sees that edit was
/// made after the deletion.
#[test]
fn rebuild_leaves_a_tombstone_for_a_held_key() {
    run(&[
        Op::NewUser,
        Op::Write(0, 3, false),
        Op::UserWrite(0, vec![], 2, false),
        Op::Fork(0, 0),
        Op::Rebuild(30, 125, 3, false),
        Op::UserWrite(0, vec![], 2, false),
        Op::Write(0, 0, false),
        Op::Publish(0),
    ]);
}

/// Main deletes twice over its own draft tombstone, which a user saw: the
/// second takes over the user's entry from the one it replaces.
#[test]
fn write_carries_entries_from_the_replaced_draft_row() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 4, false),
        Op::Write(0, 4, true),
        Op::UserWrite(0, vec![], 4, false),
        Op::Write(0, 4, true),
        Op::Commit(0),
        Op::Publish(0),
    ]);
}

/// A user edits a record main has deleted, which the user's own view still
/// holds: the edit's base comes from the layered view, so it has none.
#[test]
fn layered_edit_takes_its_base_from_the_view_written_through() {
    run(&[
        Op::NewUser,
        Op::Rebuild(0, 91, 0, false),
        Op::UserWrite(0, vec![], 0, false),
        Op::UserWrite(0, vec![], 4, false),
        Op::External(0, vec![(4, false)]),
        Op::Write(0, 4, true),
        Op::Commit(0),
        Op::Publish(0),
    ]);
}

/// A user's edit, conflicting with main's value at one publish, is made
/// again over main's newer value: that conflict is over at the next.
#[test]
fn publish_drops_a_conflict_the_edit_moved_past() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 1, false),
        Op::DiskEdit(0, 1, false, FileWins::Never),
        Op::Fork(0, 0),
        Op::Commit(6),
        Op::Publish(0),
        Op::Write(188, 1, false),
        Op::Fork(0, 0),
        Op::Commit(3),
        Op::UserWrite(0, vec![], 1, false),
        Op::Publish(0),
    ]);
}

/// Main's draft tombstone, written over an absence a user saw, continues
/// that absence once committed, though git had the record in between.
#[test]
fn committed_tombstone_continues_the_absence_it_was_written_over() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 3, false),
        Op::Rebuild(0, 0, 3, true),
        Op::UserWrite(0, vec![], 3, false),
        Op::Write(0, 3, true),
        Op::External(0, vec![(3, false)]),
        Op::DiskEdit(0, 3, true, FileWins::Never),
        Op::Write(0, 0, false),
        Op::Commit(0),
        Op::Publish(0),
    ]);
}
