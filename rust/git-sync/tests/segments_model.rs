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
/// Another draft's entry, carried to a new row: the content of the row it
/// was on, the draft's segment, and the `key_id` of the edit that made it.
type Entry = (Option<Ver>, SegId, Ver);

/// A version written, or a tombstone.
#[derive(Clone, Copy)]
struct Value {
    ver: Ver,
    deleted: bool,
}

const MAIN: Wt = 0;
/// The tag on an entry a committed segment holds: no edit made it.
const CHAIN_TAG: Ver = 0;
const KEYS: Key = 6;
const KEYS_PER_FILE: Key = 3;
/// Versions for tombstones the implementation writes on its own (scans,
/// splits) — never compared, kept out of the shared range.
const PRIVATE_VERSIONS: Ver = 1 << 40;

/// Git: each commit's whole tree, and for a commit git-sync made, the
/// `key_id` of each record its rollup lists.
#[derive(Default)]
struct Git {
    commits: Vec<BTreeMap<Key, Ver>>,
    rollups: Vec<Option<BTreeMap<Key, Ver>>>,
}

impl Git {
    /// A commit made outside git-sync: no rollup.
    fn commit(&mut self, tree: BTreeMap<Key, Ver>) -> CommitId {
        self.commits.push(tree);
        self.rollups.push(None);
        self.commits.len() - 1
    }

    fn commit_with_rollup(
        &mut self,
        tree: BTreeMap<Key, Ver>,
        ids: BTreeMap<Key, Ver>,
    ) -> CommitId {
        self.commits.push(tree);
        self.rollups.push(Some(ids));
        self.commits.len() - 1
    }
}

/// The `key_id` `key` had at `c`, from the rollup of the commit in
/// `history` that last changed it, when git-sync made that commit.
fn rollup_id(git: &Git, history: &[CommitId], key: Key, c: CommitId) -> Option<Ver> {
    let pos = history.iter().position(|&x| x == c)?;
    let changed = |i: usize| {
        let parent = i
            .checked_sub(1)
            .and_then(|p| git.commits[history[p]].get(&key));
        git.commits[history[i]].get(&key) != parent
    };
    let named = |i: usize| git.rollups[history[i]].as_ref()?.get(&key).copied();
    // The commit that last set it. Not the next change's: a resolved edit
    // committed over another record names its own.
    (0..=pos).rev().find(|&i| changed(i)).and_then(named)
}

/// Whether `key` holds a record in every commit of `history` from `c` to
/// `h`: if not, the record at `h` needn't be the one at `c`.
fn continuous(git: &Git, history: &[CommitId], key: Key, c: CommitId, h: CommitId) -> bool {
    let (Some(a), Some(b)) = (
        history.iter().position(|&x| x == c),
        history.iter().position(|&x| x == h),
    ) else {
        return false;
    };
    history[a..=b]
        .iter()
        .all(|&x| git.commits[x].contains_key(&key))
}

/// The same record name in the other file.
fn other_file(key: Key) -> Key {
    (key + KEYS_PER_FILE) % KEYS
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
    /// The record's identity, `unfurl.server.id`: its first version's.
    id: Ver,
    /// For an edit, the other records whose rows at its key its writes
    /// superseded: it settled them, so it stays at the key they hold.
    settled: BTreeSet<Ver>,
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
    /// For each entry, the `key_id`s of the edits that made it: which of a
    /// draft's edits it belongs to (§3.3).
    tags: BTreeMap<(RowId, SegId), BTreeSet<Ver>>,
    wts: Vec<SWt>,
    next_row: RowId,
    next_private: Ver,
    /// Keys an edit left to follow its record, per worktree: the file's
    /// value taken in there next is a new row for old content.
    vacated: BTreeSet<(Wt, Key)>,
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
                id: ver,
                settled: BTreeSet::new(),
            },
        );
        id
    }

    fn set_id(&mut self, row: RowId, id: Ver) {
        self.rows.get_mut(&row).unwrap().id = id;
    }

    /// The `key_id` of the live row `view` shows for `key`.
    fn live_id(&self, view: &BTreeSet<SegId>, key: Key) -> Option<Ver> {
        self.visible(view, Some(key))
            .into_iter()
            .map(|r| &self.rows[&r])
            .find(|r| !r.deleted)
            .map(|r| r.id)
    }

    /// The `key_id` of the row `view` shows for `key`, live or not.
    fn shown_id(&self, view: &BTreeSet<SegId>, key: Key) -> Option<Ver> {
        self.live_id(view, key).or_else(|| {
            self.visible(view, Some(key))
                .first()
                .map(|r| self.rows[r].id)
        })
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
        self.tags.retain(|&(r, _), _| r != id);
    }

    /// Record `r` superseded by segment `s`, made by an edit of `key_id`.
    fn entry(&mut self, r: RowId, s: SegId, key_id: Ver) {
        self.sup.insert((r, s));
        self.tags.entry((r, s)).or_default().insert(key_id);
    }

    /// Drop the entry `(r, s)` altogether.
    fn unentry(&mut self, r: RowId, s: SegId) {
        self.sup.remove(&(r, s));
        self.tags.remove(&(r, s));
    }

    /// Drop edit `key_id`'s part in entry `(r, s)`; it goes when no edit's
    /// is left.
    fn untag(&mut self, r: RowId, s: SegId, key_id: Ver) {
        if let Some(t) = self.tags.get_mut(&(r, s)) {
            t.remove(&key_id);
            if t.is_empty() {
                self.unentry(r, s);
            }
        }
    }

    /// Move entry `(r, from)` to `(r, to)` with its tags.
    fn move_entry(&mut self, r: RowId, from: SegId, to: SegId) {
        let tags = self.tags.remove(&(r, from)).unwrap_or_default();
        self.sup.remove(&(r, from));
        // a segment never supersedes its own row
        if self.rows[&r].seg == to {
            return;
        }
        self.sup.insert((r, to));
        self.tags.entry((r, to)).or_default().extend(tags);
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
        if let Some(p) = self.row_in(d, key) {
            let record = self.rows[&p].id;
            self.set_id(id, record);
        }
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
    /// content (`None`: a tombstone) of the row each is on, one per edit
    /// that made it.
    fn draft_entries(&self, rows: &[RowId], w: Wt) -> Vec<Entry> {
        rows.iter()
            .flat_map(|&r| {
                let content = (!self.rows[&r].deleted).then_some(self.rows[&r].ver);
                self.sup
                    .range((r, 0)..(r + 1, 0))
                    .filter(|&&(_, s)| {
                        self.segs[s].kind == Kind::Draft && self.segs[s].owner != Some(w)
                    })
                    .flat_map(move |&(_, s)| {
                        self.tags[&(r, s)].iter().map(move |&k| (content, s, k))
                    })
            })
            .collect()
    }

    /// A row replacing others with the same content takes over their
    /// entries: an edit made over that content was made over it too. Only
    /// from the rows the view showed just before: an older row with the
    /// same content predates changes the edit may have seen since.
    fn carry(&mut self, entries: Vec<Entry>, to: RowId) {
        let content = (!self.rows[&to].deleted).then_some(self.rows[&to].ver);
        for (c, s, k) in entries {
            if c == content {
                self.entry(to, s, k);
            }
        }
    }

    /// What a layered view shows, with copies of one record in different
    /// files merged: an upper draft's edit of a record leaves out the
    /// lower rows of that record at other keys (§4.12).
    fn visible_merged(&self, view: &BTreeSet<SegId>, key: Option<Key>) -> Vec<RowId> {
        let main_draft = self.wts[MAIN].draft;
        let upper = |s: SegId| self.segs[s].kind == Kind::Draft && s != main_draft;
        let edits: Vec<(Key, Ver)> = self
            .rows
            .values()
            .filter(|r| r.conflict.is_none() && view.contains(&r.seg) && upper(r.seg))
            .map(|r| (r.key, r.id))
            .collect();
        self.visible(view, key)
            .into_iter()
            .filter(|r| {
                let r = &self.rows[r];
                upper(r.seg) || !edits.iter().any(|&(k, id)| id == r.id && k != r.key)
            })
            .collect()
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
            self.move_entry(r, from, to);
        }
    }

    /// §4.2: a write into `w`'s draft, made through `via`: a client's edit,
    /// or with `edit` false, a value taken in from the working tree.
    fn write(
        &mut self,
        w: Wt,
        via: &BTreeSet<SegId>,
        key: Key,
        value: Value,
        edit: bool,
        record: Ver,
    ) {
        let Value { ver, deleted } = value;
        let d = self.wts[w].draft;
        let own = self.own_view(w);
        // at most one edit per record: one of it at another key is replaced
        // by this one, which keeps its base
        let other = self
            .rows_in(d)
            .into_iter()
            .find(|&y| self.rows[&y].id == record && self.rows[&y].key != key && edit);
        let other_base = other.and_then(|y| self.rows[&y].edit).map(|e| e.base);
        // a re-create, with no base: the view shows the record live nowhere,
        // copies merged by key_id, a draft's live row included, the writer's
        // own too
        let mut below = via.clone();
        below.remove(&d);
        let live_anywhere = self
            .rows_in(d)
            .iter()
            .any(|y| self.rows[y].id == record && !self.rows[y].deleted)
            || self
                .visible_merged(&below, None)
                .iter()
                .any(|r| self.rows[r].id == record && !self.rows[r].deleted);
        if let Some(y) = other {
            let k2 = self.rows[&y].key;
            self.drop_conflict(w, k2);
            // its entries stay with this edit, but its own tree's row of
            // another record shows again at the key it leaves
            self.delete_row(y);
            let chain = self.chain_set(w);
            self.untag_edit(d, record, |r| {
                r.key == k2 && chain.contains(&r.seg) && r.id != record
            });
        }
        let prior = self.row_in(d, key);
        // the record's committed version in `via`, at whatever key, under
        // whatever uncommitted edit of it
        let committed = || {
            let chain: BTreeSet<SegId> = via
                .iter()
                .copied()
                .filter(|&s| self.segs[s].kind != Kind::Draft)
                .collect();
            self.visible(&chain, None)
                .into_iter()
                .map(|r| &self.rows[&r])
                .find(|r| r.id == record && !r.deleted)
                .map(|r| r.ver)
        };
        let meta = edit.then(|| Edit {
            base: match (live_anywhere, prior, other_base) {
                (false, _, _) => None,
                (true, Some(p), _) => self.rows[&p].edit.and_then(|e| e.base),
                (true, None, Some(b)) => b,
                (true, None, None) => committed(),
            },
            gone: if deleted { self.live(&own, key) } else { None },
        });
        let mut seen: BTreeSet<RowId> = self.visible_merged(via, Some(key)).into_iter().collect();
        seen.extend(self.visible(&own, Some(key)));
        // an absence shows whatever copy the merge leaves out
        seen.extend(
            self.visible(via, Some(key))
                .into_iter()
                .filter(|r| self.rows[r].deleted),
        );
        // and whatever another draft's entries hide, while no draft below
        // has a row at the key: main's absence is what shows
        let lower_row = self
            .visible(via, Some(key))
            .iter()
            .any(|r| self.rows[r].seg != d && self.segs[self.rows[r].seg].kind == Kind::Draft);
        if !lower_row {
            seen.extend(
                self.visible(&self.own_view(MAIN), Some(key))
                    .into_iter()
                    .filter(|r| self.rows[r].deleted),
            );
        }
        let mut settled = prior.map_or_else(BTreeSet::new, |p| self.rows[&p].settled.clone());
        settled.extend(
            seen.iter()
                .map(|r| &self.rows[r])
                .filter(|r| r.seg != d && !r.deleted && r.id != record)
                .map(|r| r.id),
        );
        // and the same record's rows elsewhere with content seen here: a
        // move's copy of what the edit was made over (§3.5)
        let contents: BTreeSet<(Ver, Ver)> = seen
            .iter()
            .map(|r| &self.rows[r])
            .filter(|r| !r.deleted && r.id == record)
            .map(|r| (r.id, r.ver))
            .collect();
        let mut elsewhere = self.visible_merged(via, None);
        elsewhere.extend(self.visible(&own, None));
        seen.extend(elsewhere.into_iter().filter(|r| {
            let r = &self.rows[r];
            r.key != key && !r.deleted && contents.contains(&(r.id, r.ver))
        }));
        let entries = self.draft_entries(&seen.iter().copied().collect::<Vec<_>>(), w);
        if let Some(old) = prior {
            seen.remove(&old);
            let old_id = self.rows[&old].id;
            self.delete_row(old);
            self.retag(d, old_id, record);
        }
        let id = self.insert(d, key, ver, deleted);
        self.rows.get_mut(&id).unwrap().edit = meta;
        self.rows.get_mut(&id).unwrap().settled = settled;
        self.set_id(id, record);
        self.carry(entries, id);
        // rows here the draft already hides are this edit's to hide too,
        // but a live row of a record it edits elsewhere, which the merge
        // hid from this write
        let elsewhere: BTreeSet<Ver> = self
            .rows_in(d)
            .into_iter()
            .map(|r| &self.rows[&r])
            .filter(|r| r.key != key)
            .map(|r| r.id)
            .collect();
        let hidden: Vec<RowId> = self
            .tags
            .keys()
            .filter(|&&(r, s)| {
                let r = &self.rows[&r];
                s == d && r.key == key && (r.deleted || !elsewhere.contains(&r.id))
            })
            .map(|&(r, _)| r)
            .collect();
        for r in seen.into_iter().chain(hidden) {
            self.entry(r, d, record);
        }
    }

    /// Edit row `x` can't follow its record: it becomes a new record. Its
    /// entries at its key are the new record's; on the old record's rows
    /// elsewhere they go.
    fn renew_id(&mut self, x: RowId) {
        let (d, key, old) = (self.rows[&x].seg, self.rows[&x].key, self.rows[&x].id);
        let fresh = self.rows[&x].ver;
        self.set_id(x, fresh);
        // other drafts' entries on it an edit of the old record made, once
        // it moved from where they saw it: it's another record now
        let moved = self.rows[&x].recreated;
        let on_x: Vec<SegId> = self
            .sup
            .range((x, 0)..(x + 1, 0))
            .map(|&(_, s)| s)
            .filter(|&s| s != d && moved)
            .collect();
        for s in on_x {
            self.untag(x, s, old);
        }
        let mine: Vec<RowId> = self
            .tags
            .iter()
            .filter(|&(&(_, s), t)| s == d && t.contains(&old))
            .map(|(&(r, _), _)| r)
            .collect();
        for r in mine {
            let rkey = self.rows[&r].key;
            // another edit of the old record there still needs it
            let other = self
                .rows_in(d)
                .into_iter()
                .any(|y| self.rows[&y].key == rkey && self.rows[&y].id == old);
            if !other {
                self.untag(r, d, old);
            }
            if rkey == key {
                self.entry(r, d, fresh);
            }
        }
    }

    /// Move edit row `x` to `to`, with its conflict row and its entries.
    /// Other drafts' entries on it follow only if an edit of its record
    /// made them; its own on other records at the key it leaves go.
    fn relocate(&mut self, x: RowId, to: Key) {
        let (d, from, id) = (self.rows[&x].seg, self.rows[&x].key, self.rows[&x].id);
        if let Some(c) = self.conflict_row(d, from) {
            self.rows.get_mut(&c).unwrap().key = to;
        }
        self.rows.get_mut(&x).unwrap().key = to;
        self.recreated(x);
        let on_x: Vec<SegId> = self
            .sup
            .range((x, 0)..(x + 1, 0))
            .map(|&(_, s)| s)
            .collect();
        for s in on_x {
            let keep = self.tags[&(x, s)].contains(&id);
            if !keep {
                self.unentry(x, s);
            }
        }
        self.untag_edit(d, id, |r| r.key == from && r.id != id);
    }

    /// A pending edit follows its record, with its conflict, to the key
    /// `w`'s committed chain now has it at.
    fn follow_records(&mut self, w: Wt) {
        let (d, chain) = (self.wts[w].draft, self.chain_set(w));
        for x in self.rows_in(d) {
            let (id, from) = (self.rows[&x].id, self.rows[&x].key);
            // not when the record at its key is its own, or one it settled
            let here = self.live_id(&chain, from);
            if self.rows[&x].edit.is_none()
                || here == Some(id)
                || here.is_some_and(|r| self.rows[&x].settled.contains(&r))
            {
                continue;
            }
            let to = (0..KEYS).find(|&k| k != from && self.live_id(&chain, k) == Some(id));
            // another edit holds the key it would follow to: it stays, as a
            // new record, so no two files share the id
            if to.is_some_and(|to| self.row_in(d, to).is_some()) {
                self.renew_id(x);
                continue;
            }
            if let Some(to) = to {
                self.relocate(x, to);
                self.vacated.insert((w, from));
            }
        }
    }

    /// Take `key` out of `w`'s draft, with the entries its edit made
    /// (unless another of the draft's rows is the same record's).
    fn remove_draft_row(&mut self, w: Wt, key: Key) {
        let d = self.wts[w].draft;
        let Some(x) = self.row_in(d, key) else {
            return;
        };
        let id = self.rows[&x].id;
        self.delete_row(x);
        // but where a row of the same id holds the key: another edit
        // hiding the same rows holds its own tags there
        let same: BTreeSet<Key> = self
            .rows_in(d)
            .into_iter()
            .map(|r| &self.rows[&r])
            .filter(|r| r.id == id)
            .map(|r| r.key)
            .collect();
        self.untag_edit(d, id, |r| !same.contains(&r.key));
    }

    /// Draft `d`'s entries made by edit `old` become `new`'s, but at a key
    /// another row of `old` holds.
    fn retag(&mut self, d: SegId, old: Ver, new: Ver) {
        if old == new {
            return;
        }
        let others: BTreeSet<Key> = self
            .rows_in(d)
            .into_iter()
            .map(|y| &self.rows[&y])
            .filter(|y| y.id == old)
            .map(|y| y.key)
            .collect();
        let mine: Vec<RowId> = self
            .tags
            .iter()
            .filter(|&(&(r, s), t)| {
                s == d && t.contains(&old) && !others.contains(&self.rows[&r].key)
            })
            .map(|(&(r, _), _)| r)
            .collect();
        for r in mine {
            self.untag(r, d, old);
            self.entry(r, d, new);
        }
    }

    /// Drop edit `id`'s part in draft `d`'s entries on rows `which` picks.
    fn untag_edit(&mut self, d: SegId, id: Ver, which: impl Fn(&Row) -> bool) {
        let mine: Vec<RowId> = self
            .tags
            .iter()
            .filter(|&(&(r, s), t)| s == d && t.contains(&id) && which(&self.rows[&r]))
            .map(|(&(r, _), _)| r)
            .collect();
        for r in mine {
            self.untag(r, d, id);
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
            // a value taken in from the file is the record git has at its
            // key, or a new one
            if let Some(x) = x {
                let r = self.live_id(&self.chain_set(w), key);
                let (old, new) = (self.rows[&x].id, r.unwrap_or(self.rows[&x].ver));
                self.set_id(x, new);
                self.retag(d, old, new);
            }
            if theirs == self.live(&self.chain_set(w), key) {
                if x.is_some() {
                    self.remove_draft_row(w, key);
                }
            } else if x.map(|x| (!self.rows[&x].deleted).then_some(self.rows[&x].ver))
                != Some(theirs)
            {
                let own = self.own_view(w);
                let ver = theirs.unwrap_or_else(|| self.private_ver());
                let record = self.live_id(&self.chain_set(w), key).unwrap_or(ver);
                self.write(
                    w,
                    &own,
                    key,
                    Value {
                        ver,
                        deleted: theirs.is_none(),
                    },
                    false,
                    record,
                );
                // over a withdrawn edit: the file's value again, in a new row
                if forced || self.vacated.remove(&(w, key)) {
                    let r = self.row_in(d, key).unwrap();
                    self.recreated(r);
                    // drafts that superseded this value here saw it: an edit
                    // at this key, or of the record
                    if let Some(v) = theirs {
                        let earlier: Vec<RowId> = self
                            .rows
                            .iter()
                            .filter(|&(&x, row)| {
                                x != r && row.key == key && !row.deleted && row.ver == v
                            })
                            .map(|(&x, _)| x)
                            .collect();
                        let entries: Vec<Entry> =
                            self.draft_entries(&earlier, w)
                                .into_iter()
                                .filter(|&(_, seg, tag)| {
                                    tag == record
                                        || self.rows_in(seg).iter().any(|y| {
                                            self.rows[y].id == tag && self.rows[y].key == key
                                        })
                                })
                                .collect();
                        self.carry(entries, r);
                    }
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
            let tag = self.row_in(d, k).map_or(CHAIN_TAG, |x| self.rows[&x].id);
            for c in self.visible(&chain, Some(k)) {
                self.entry(c, d, tag);
            }
        }
        // an edited record's copy in another file, with content the draft
        // already superseded
        let edited: BTreeSet<Ver> = self
            .rows_in(d)
            .into_iter()
            .filter(|x| self.rows[x].edit.is_some())
            .map(|x| self.rows[&x].id)
            .collect();
        let seen: BTreeSet<(Ver, Ver)> = self
            .sup
            .iter()
            .filter(|&&(_, s)| s == d)
            .map(|&(r, _)| &self.rows[&r])
            .filter(|r| !r.deleted && edited.contains(&r.id))
            .map(|r| (r.id, r.ver))
            .collect();
        let copies: Vec<RowId> = self
            .visible(&chain, None)
            .into_iter()
            .filter(|r| {
                let r = &self.rows[r];
                !r.deleted && seen.contains(&(r.id, r.ver))
            })
            .collect();
        for c in copies {
            let tag = self.rows[&c].id;
            self.entry(c, d, tag);
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
        // A record that left one file and arrived in the other in this scan
        // moved: it keeps its `key_id`, and with the same content its
        // entries (§3.5).
        let chain = self.chain_set(w);
        let mut moved: BTreeMap<Key, (Ver, Vec<Entry>)> = BTreeMap::new();
        for &(key, change) in changes {
            let from = other_file(key);
            if change.is_some()
                && self.live_id(&chain, key).is_none()
                && changes.contains(&(from, None))
            {
                let left: Vec<RowId> = self
                    .visible(&chain, Some(from))
                    .into_iter()
                    .filter(|r| !self.rows[r].deleted)
                    .collect();
                if let Some(&r) = left.first() {
                    // only those an edit of this record made: one editing
                    // another record at that key doesn't follow it
                    let id = self.rows[&r].id;
                    let entries = self
                        .draft_entries(&left, w)
                        .into_iter()
                        .filter(|&(_, _, k)| k == id)
                        .collect();
                    moved.insert(key, (id, entries));
                }
            }
        }
        for &(key, change) in changes {
            self.scan_key(w, key, change, commit, moved.remove(&key));
        }
        self.follow_records(w);
        let files: BTreeSet<Key> = changes.iter().map(|&(k, _)| file_of(k)).collect();
        for f in files {
            self.reconcile(w, f, disk, FileWins::Never);
        }
        let h = self.wts[w].head;
        self.segs[h].head_commit = Some(commit);
        self.wts[w].history.push(commit);
        self.relink_draft(w);
    }

    fn scan_key(
        &mut self,
        w: Wt,
        key: Key,
        change: Option<Ver>,
        commit: CommitId,
        moved: Option<(Ver, Vec<Entry>)>,
    ) {
        let h = self.wts[w].head;
        let mut below = self.chain_set(w);
        below.remove(&h);
        let vis_below = self.visible(&below, Some(key));
        let head_row = self.row_in(h, key);
        // an edit or a deletion continues the record shown; otherwise a
        // move carries its id over, or it's a new record
        let record = self.shown_id(&self.chain_set(w), key);
        let live_record = self.live_id(&self.chain_set(w), key);
        let pinned = head_row.is_some() && self.held_elsewhere(key, w);
        let entries = self.draft_entries(head_row.as_slice(), w);
        if let Some(hr) = head_row {
            self.delete_row(hr);
        }
        let new_row = match change {
            Some(v) => Some(self.insert(h, key, v, false)),
            // already absent in the chain: the head hides the row below
            None if head_row.is_none() && live_record.is_none() => None,
            None if pinned || vis_below.iter().any(|r| !self.rows[r].deleted) => {
                let t = self.private_ver();
                Some(self.insert(h, key, t, true))
            }
            None => None,
        };
        if let Some(n) = new_row {
            self.stamp(n, commit);
            let id = match change {
                Some(v) => live_record
                    .or(moved.as_ref().map(|(id, _)| *id))
                    .unwrap_or(v),
                None => record.unwrap(),
            };
            self.set_id(n, id);
            if let Some((_, entries)) = moved {
                // a new row for the record, like a split's
                self.recreated(n);
                self.carry(entries, n);
            }
            self.carry(entries, n);
            if head_row.is_none() {
                let entries = self.draft_entries(&vis_below, w);
                self.carry(entries, n);
                for r in vis_below {
                    self.entry(r, h, CHAIN_TAG);
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
        let parent = &git.commits[*self.wts[w].history.last().unwrap()];
        if tree == *parent {
            return None;
        }
        let changed: Vec<Key> = (0..KEYS).filter(|k| tree.get(k) != parent.get(k)).collect();
        let before = self.chain_set(w);
        let deleted_ids: BTreeMap<Key, Ver> = changed
            .iter()
            .filter_map(|&k| self.shown_id(&before, k).map(|id| (k, id)))
            .collect();
        let commit = git.commit_with_rollup(tree.clone(), BTreeMap::new());
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
            // and the edit's own entries at any other key
            let record = self.rows[&x].id;
            let followed: Vec<RowId> = self
                .tags
                .iter()
                .filter(|&(&(r, s), t)| s == d && t.contains(&record) && self.rows[&r].key != k)
                .map(|(&(r, _), _)| r)
                .collect();
            for r in followed {
                // a segment never supersedes its own row
                if self.rows[&r].seg != h {
                    self.move_entry(r, d, h);
                } else {
                    self.unentry(r, d);
                }
            }
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
                self.scan_key(w, k, file, commit, None);
                if let Some(r) = self.row_in(h, k) {
                    self.recreated(r);
                }
            }
        }
        // the rollup lists each record the commit changed, with its id
        let chain = self.chain_set(w);
        git.rollups[commit] = Some(
            changed
                .iter()
                .filter_map(|&k| match tree.contains_key(&k) {
                    true => self.live_id(&chain, k).map(|id| (k, id)),
                    false => deleted_ids.get(&k).map(|&id| (k, id)),
                })
                .collect(),
        );
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
        // none where the model allowed a missing one (an over-report there)
        if let Some(c) = self.conflict_row(self.wts[w].draft, key) {
            self.rows.get_mut(&c).unwrap().conflict = Some(true);
        }
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
        // empty: no rows, and no entries hiding rows below
        if self.rows_in(h).is_empty() && !self.sup.iter().any(|&(_, s)| s == h) {
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
            self.split(s, c, git, &history);
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
    fn split(&mut self, s: SegId, c: CommitId, git: &Git, history: &[CommitId]) {
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
        // S's rows written after `c`: one in the other file with a key's
        // value at `c` is where that record moved
        let late: Vec<(Key, Ver, Ver)> = self
            .rows_in(s)
            .into_iter()
            .map(|r| &self.rows[&r])
            .filter(|r| r.commit > Some(c) && !r.deleted)
            .map(|r| (r.key, r.ver, r.id))
            .collect();
        let moved_id = |k: Key, v: Option<Ver>| {
            late.iter()
                .find(|&&(key, ver, _)| key == other_file(k) && Some(ver) == v)
                .map(|&(_, _, id)| id)
        };
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
        // the ids the tree at `c` keeps elsewhere: a fallback can't reuse one
        let mut at_c = below.clone();
        at_c.insert(s);
        let mut taken: BTreeSet<Ver> = tree_c
            .keys()
            .filter(|j| !keys.contains(j))
            .filter_map(|&j| self.live_id(&at_c, j))
            .collect();
        // and a changed key whose value at `c` the row below already holds
        for (&j, &v) in tree_c.iter().filter(|(j, _)| keys.contains(j)) {
            let below_row = self
                .visible(&below, Some(j))
                .into_iter()
                .find(|r| !self.rows[r].deleted);
            if let Some(r) = below_row.filter(|r| self.rows[r].ver == v) {
                taken.insert(self.rows[&r].id);
            }
        }
        // the ids keys claim by rollup, move or continuing record: the row
        // below is a weaker claim, and loses to them
        let claims: BTreeMap<Key, Ver> = keys
            .iter()
            .filter_map(|&k| {
                let v_c = tree_c.get(&k).copied();
                let same = self.row_in(s, k).and_then(|sr| {
                    continuous(git, history, k, c, h_commit).then_some(self.rows[&sr].id)
                });
                rollup_id(git, history, k, c)
                    .or(moved_id(k, v_c))
                    .or(same)
                    .map(|id| (k, id))
            })
            .collect();
        let claimed = |k: Key, id: &Ver| claims.iter().any(|(&j, i)| j != k && i == id);
        // rows below that are a record the tree at `c` has at another key:
        // it moved there
        let moved_on: BTreeSet<RowId> = keys
            .iter()
            .filter_map(|&k| {
                self.visible(&below, Some(k)).first().copied().filter(|b| {
                    let r = &self.rows[b];
                    !r.deleted && tree_c.iter().any(|(&j, &v)| j != k && v == r.ver)
                })
            })
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
                        let below_id = vis_below
                            .first()
                            .filter(|b| !moved_on.contains(b))
                            .map(|b| self.rows[b].id);
                        let same =
                            continuous(git, history, k, c, h_commit).then_some(self.rows[&sr].id);
                        let free = |id: &Ver| !taken.contains(id);
                        let fresh = Some(self.rows[&new].id).filter(free);
                        let id = rollup_id(git, history, k, c)
                            .or(moved_id(k, v_c))
                            .or(same.filter(free))
                            .or(below_id.filter(|id| free(id) && !claimed(k, id)))
                            .or(fresh)
                            .unwrap_or_else(|| self.private_ver());
                        taken.insert(id);
                        self.set_id(new, id);
                        self.stamp(new, c);
                        self.recreated(new);
                        self.entry(new, s2, CHAIN_TAG);
                    } else {
                        self.move_entries(s, s2, k);
                    }
                }
                None => {
                    // changed before c and back after it
                    let below_id = vis_below
                        .first()
                        .filter(|b| !moved_on.contains(b))
                        .map(|b| self.rows[b].id);
                    let new = self.insert_value(s, k, v_c);
                    let fresh = Some(self.rows[&new].id).filter(|id| !taken.contains(id));
                    let id = rollup_id(git, history, k, c)
                        .or(moved_id(k, v_c))
                        .or(below_id.filter(|id| !taken.contains(id) && !claimed(k, id)))
                        .or(fresh)
                        .unwrap_or_else(|| self.private_ver());
                    taken.insert(id);
                    self.set_id(new, id);
                    self.stamp(new, c);
                    self.recreated(new);
                    for &x in &vis_below {
                        self.entry(x, s, CHAIN_TAG);
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
                            let r = self.insert(s2, k, ver, deleted);
                            self.set_id(r, self.rows[&b].id);
                            r
                        }
                        None => {
                            let r = self.insert_value(s2, k, v_h);
                            let id = rollup_id(git, history, k, h_commit)
                                .or(below_id)
                                .unwrap_or(self.rows[&r].id);
                            self.set_id(r, id);
                            r
                        }
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
                        // a draft's entry is its edit's at that key
                        let tag = self.row_in(seg, k).map_or(CHAIN_TAG, |x| self.rows[&x].id);
                        self.entry(r2, seg, tag);
                    }
                    self.entry(new, s2, CHAIN_TAG);
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
        let records: BTreeMap<RowId, Ver> = self
            .rows_in(d)
            .into_iter()
            .map(|x| (x, self.rows[&x].id))
            .collect();
        for x in self.rows_in(d) {
            // main's row for the record, live wherever it is, else what's
            // at the edit's key; the edit and its conflict move to its file
            let id = self.rows[&x].id;
            let at = (0..KEYS).find(|&k| self.live_id(&view, k) == Some(id));
            // not when the record at its key is one it settled
            let vacated = !matches!(
                self.live_id(&view, self.rows[&x].key),
                Some(r) if self.rows[&x].settled.contains(&r)
            );
            if let Some(to) = at.filter(|&to| vacated && to != self.rows[&x].key) {
                if self.row_in(d, to).is_none() {
                    self.relocate(x, to);
                } else {
                    // another edit holds that key: this one stays, a new record
                    self.renew_id(x);
                }
            }
        }
        // then classify, once no edit's entries can still move
        for x in self.rows_in(d) {
            let k = self.rows[&x].key;
            // seen: superseded by the draft, or the draft superseded another
            // row of the record with the same content
            let edited: BTreeSet<Ver> = self
                .rows_in(d)
                .into_iter()
                .map(|r| self.rows[&r].id)
                .collect();
            let seen_content: BTreeSet<(Ver, Ver)> = self
                .sup
                .iter()
                .filter(|&&(_, s)| s == d)
                .map(|&(r, _)| &self.rows[&r])
                .filter(|r| !r.deleted && edited.contains(&r.id))
                .map(|r| (r.id, r.ver))
                .collect();
            let rid = records[&x];
            let gone = !(0..KEYS).any(|j| self.live_id(&view, j) == Some(rid));
            let unseen: Vec<RowId> = self
                .visible(&view, Some(k))
                .into_iter()
                .filter(|&r| {
                    let row = &self.rows[&r];
                    !self.sup.contains(&(r, d))
                        && (row.deleted || !seen_content.contains(&(row.id, row.ver)))
                })
                .collect();
            // made over a version of a record main has since deleted: a
            // base means the record was live when it was written
            let deleted = gone
                && !self.rows[&x].deleted
                && self.rows[&x].edit.is_some_and(|e| e.base.is_some());
            if deleted {
                self.settle(u, x, self.live(&view, k));
                continue;
            }
            // renewed, it adds a record: over no live value, no conflict
            if rid != self.rows[&x].id && self.live(&view, k).is_none() {
                if let Some(c) = self.conflict_row(d, k) {
                    self.delete_row(c);
                }
                continue;
            }
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
        // main's rows are its own tree's now: where it holds no edit, it shows them
        let held: BTreeSet<Key> = self
            .rows_in(d)
            .into_iter()
            .map(|r| self.rows[&r].key)
            .collect();
        for r in self.visible(&view, None) {
            if !held.contains(&self.rows[&r].key) {
                self.unentry(r, d);
            }
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
            // no rollup: the record the base shows continues, else the one
            // W showed, else it's new
            // the base's ids stay unique in the new tree
            let taken: BTreeSet<Ver> = (0..KEYS)
                .filter(|j| tree.contains_key(j))
                .filter_map(|j| self.live_id(&view, j))
                .collect();
            let id = match want {
                Some(v) => self
                    .live_id(&view, k)
                    .or(self.live_id(&old_view, k).filter(|id| !taken.contains(id)))
                    .unwrap_or(v),
                None => self
                    .shown_id(&old_view, k)
                    .or(self.shown_id(&view, k))
                    .unwrap(),
            };
            self.set_id(n, id);
            self.stamp(n, commit);
            let old = self.visible(&old_view, Some(k));
            let entries = self.draft_entries(&old, w);
            self.carry(entries, n);
            for x in vis {
                self.entry(x, hn, CHAIN_TAG);
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
        // rows the rewrite brings back into view: old content in rows no
        // edit since could have reached
        let before: BTreeSet<RowId> = self.visible(&old_view, None).into_iter().collect();
        let back: Vec<RowId> = self
            .visible(&self.chain_set(w), None)
            .into_iter()
            .filter(|r| !before.contains(r) && self.rows[r].seg != hn)
            .collect();
        for r in back {
            self.recreated(r);
        }
        self.follow_records(w);
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
        self.tags.retain(|&(_, seg), _| seg != s);
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
                self.move_entry(r, p, c);
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
    /// The record's `key_id`.
    id: Ver,
    /// Where it saw each version it was made over: the rows its entries
    /// are on, which stay put when a publish moves the edit.
    seen_at: BTreeSet<(Key, Ver)>,
    /// The other records it settled at its key.
    settled: BTreeSet<Ver>,
    /// For a tombstone, the tombstones it replaced in the draft: the same
    /// content, so an edit made over one was made over it.
    same_as: BTreeSet<Ver>,
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct OConflict {
    theirs: Option<Ver>,
    resolved: bool,
}

#[derive(Clone, Debug)]
struct OWt {
    committed: BTreeMap<Key, Ver>,
    /// The `key_id` of each committed record.
    ids: BTreeMap<Key, Ver>,
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
    fn new(committed: BTreeMap<Key, Ver>, ids: BTreeMap<Key, Ver>, user: bool) -> Self {
        OWt {
            disk: committed.clone(),
            committed,
            ids,
            draft: BTreeMap::new(),
            conflicts: BTreeMap::new(),
            absent: BTreeMap::new(),
            user,
            alive: true,
        }
    }

    /// Committed keys an edit hides elsewhere: the same record, with a
    /// version the edit was made over (a move's other copy).
    fn moved_away(&self) -> BTreeSet<Key> {
        self.draft
            .iter()
            .flat_map(|(&k, d)| {
                self.committed.iter().filter(move |&(&j, &v)| {
                    j != k
                        && self.ids.get(&j) == Some(&d.id)
                        && (self.user || d.based_on.contains(&v))
                })
            })
            .map(|(&j, _)| j)
            .collect()
    }

    fn view(&self) -> BTreeMap<Key, Ver> {
        let mut v = self.committed.clone();
        for j in self.moved_away() {
            v.remove(&j);
        }
        for (k, d) in &self.draft {
            if d.deleted {
                v.remove(k);
            } else {
                v.insert(*k, d.ver);
            }
        }
        v
    }

    /// The `key_id` of each record the view shows live.
    fn view_ids(&self) -> BTreeMap<Key, Ver> {
        let mut v = self.ids.clone();
        v.retain(|k, _| self.committed.contains_key(k));
        for j in self.moved_away() {
            v.remove(&j);
        }
        for (k, d) in &self.draft {
            if d.deleted {
                v.remove(k);
            } else {
                v.insert(*k, d.id);
            }
        }
        v
    }

    /// The `key_id` of what the view shows for `k`.
    fn shown_id(&self, k: Key) -> Option<Ver> {
        self.draft
            .get(&k)
            .map(|d| d.id)
            .or_else(|| self.ids.get(&k).copied())
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
        value: Value,
        shown: Option<Ver>,
        based_on: BTreeSet<Ver>,
        id: Ver,
        mut settled: BTreeSet<Ver>,
    ) {
        let Value { ver, deleted } = value;
        // at most one edit per record: one of it at another key is replaced
        let other = self
            .draft
            .iter()
            .find(|(&k2, d)| k2 != k && d.id == id && d.origin.is_edit())
            .map(|(&k2, _)| k2);
        let mut inherited = None;
        if let Some(k2) = other {
            let mut d = self.draft.remove(&k2).unwrap();
            self.conflicts.remove(&k2);
            // its own tree's row of another record there shows again
            let own = self
                .committed
                .get(&k2)
                .filter(|_| self.ids.get(&k2) != Some(&id));
            d.seen_at.retain(|&(sk, v)| sk != k2 || Some(&v) != own);
            inherited = Some(d);
        }
        let replaced = self.draft.get(&k).cloned();
        if let Some(d) = self.draft.get(&k) {
            settled.extend(&d.settled);
        }
        settled.remove(&id);
        let base = match self.draft.get(&k) {
            Some(ODraft {
                origin: Origin::Edit(e),
                ..
            }) => e.base,
            Some(_) => None,
            None => match &inherited {
                Some(ODraft {
                    origin: Origin::Edit(e),
                    ..
                }) => e.base,
                _ => shown,
            },
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
                id,
                seen_at: BTreeSet::new(),
                settled,
                same_as: BTreeSet::new(),
            },
        );
        if let Some(old) = replaced.filter(|o| o.deleted && deleted) {
            let d = self.draft.get_mut(&k).unwrap();
            d.same_as.extend(old.same_as);
            d.same_as.insert(old.ver);
        }
        if let Some(old) = inherited {
            let d = self.draft.get_mut(&k).unwrap();
            d.based_on.extend(old.based_on);
            d.seen_at.extend(old.seen_at);
        }
    }

    /// A pending edit follows its record, with its conflict, to the key the
    /// committed tree now has it at.
    fn follow_records(&mut self) {
        for from in self.draft.keys().copied().collect::<Vec<_>>() {
            let d = &self.draft[&from];
            // not when the record at its key is its own, or one it settled
            let here = self.committed.contains_key(&from).then(|| self.ids[&from]);
            if !d.origin.is_edit()
                || here == Some(d.id)
                || here.is_some_and(|r| d.settled.contains(&r))
            {
                continue;
            }
            let to = self
                .ids
                .iter()
                .find(|&(&k, &id)| k != from && id == d.id)
                .map(|(&k, _)| k);
            if to.is_some_and(|to| self.draft.contains_key(&to)) {
                let d = self.draft.get_mut(&from).unwrap();
                d.id = d.ver;
                continue;
            }
            if let Some(to) = to {
                let d = self.draft.remove(&from).unwrap();
                self.draft.insert(to, d);
                if let Some(c) = self.conflicts.remove(&from) {
                    self.conflicts.insert(to, c);
                }
            }
        }
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
            // a value taken in from the file is the record git has at its
            // key, or a new one
            if let Some(d) = self.draft.get_mut(&k) {
                d.id = self.ids.get(&k).copied().unwrap_or(d.ver);
            }
            if theirs == self.committed.get(&k).copied() {
                self.draft.remove(&k);
            } else if !holds {
                let ver = theirs.unwrap_or_else(|| {
                    *next += 1;
                    *next
                });
                let id = self.ids.get(&k).copied().unwrap_or(ver);
                self.draft.insert(
                    k,
                    ODraft {
                        ver,
                        deleted: theirs.is_none(),
                        based_on: BTreeSet::new(),
                        origin: Origin::File,
                        continues: BTreeSet::new(),
                        id,
                        seen_at: BTreeSet::new(),
                        settled: BTreeSet::new(),
                        same_as: BTreeSet::new(),
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
        // a carried edit keeps its record's id; a value the file brings in
        // continues the committed record, or is a new one
        self.ids = tree
            .iter()
            .map(|(&k, &v)| {
                let id = match self.draft.get(&k) {
                    Some(d) if carried.contains(&k) => d.id,
                    _ => self.ids.get(&k).copied().unwrap_or(v),
                };
                (k, id)
            })
            .collect();
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
    /// `writer`'s edit of its record at another key merges nothing away:
    /// the write replaces it.
    fn stack_rows(&self, uppers: &[Wt], k: Key, writer: Option<(Wt, Ver)>) -> Vec<(Ver, bool)> {
        let drafts: Vec<&ODraft> = uppers
            .iter()
            .filter_map(|&u| self.wts[u].draft.get(&k))
            .collect();
        // edits in the stack superseded the rows they saw at these keys
        let seen: BTreeSet<(Key, Ver)> = uppers
            .iter()
            .flat_map(|&u| self.wts[u].draft.values())
            .flat_map(|d| d.seen_at.iter().copied())
            .collect();
        // main's draft row is one row too, wherever a move took it
        let main_draft = self.wts[MAIN].draft.contains_key(&k);
        // copies of one record in different files merge: an edit in the
        // stack of main's record at another key leaves main's row out
        let merged = |id: Option<Ver>| {
            uppers
                .iter()
                .flat_map(|&u| self.wts[u].draft.iter().map(move |e| (u, e)))
                .any(|(u, (&dk, d))| dk != k && Some(d.id) == id && writer != Some((u, d.id)))
        };
        // a draft row: by an edit that saw it at its key, or an edit of its
        // record made over its version, wherever it has moved since
        let hidden_draft = |id: Ver, v: Ver| {
            seen.contains(&(k, v))
                || uppers
                    .iter()
                    .flat_map(|&u| &self.wts[u].draft)
                    .any(|(_, e)| {
                        e.id == id && e.ver != v && e.seen_at.iter().any(|&(_, sv)| sv == v)
                    })
        };
        let main_id = self.wts[MAIN].shown_id(k);
        let main_row = self.wts[MAIN].shown(k).filter(|&(v, dead)| {
            // a tombstone is no copy to merge
            if merged(main_id) && !dead {
                return false;
            }
            if main_draft {
                let d = &self.wts[MAIN].draft[&k];
                // a tombstone continuing an absence an edit in the stack was
                // made over took over that edit's entry
                let continued = d.deleted
                    && uppers.iter().any(|&u| {
                        self.wts[u]
                            .draft
                            .get(&k)
                            .is_some_and(|e| !e.based_on.is_disjoint(&d.continues))
                    });
                return !continued
                    && !hidden_draft(d.id, v)
                    && !d.same_as.iter().any(|&a| hidden_draft(d.id, a));
            }
            !seen.contains(&(k, v))
        });
        main_row
            .into_iter()
            .chain(
                // a draft's version is one row's, wherever a publish moved it
                drafts
                    .iter()
                    .filter(|d| !hidden_draft(d.id, d.ver))
                    .map(|d| (d.ver, d.deleted)),
            )
            .collect()
    }

    fn layered(&self, uppers: &[Wt]) -> BTreeSet<(Key, Ver)> {
        (0..KEYS)
            .flat_map(|k| {
                self.stack_rows(uppers, k, None)
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
    /// Commit a worktree's draft.
    Commit(u8),
    /// A commit made outside the database and checked out, then scanned:
    /// one or more changes, as a merge brings.
    External(u8, Vec<(Key, bool)>),
    /// A hand edit of a worktree's working tree, then scanned, letting the
    /// file win where `.3` says.
    DiskEdit(u8, Key, bool, FileWins),
    /// Fork main or a fork, `.1` commits back from its head.
    Fork(u8, u8),
    /// A user branch off main's head.
    NewUser,
    /// A write by a user through main, the other users' branches listed
    /// (a stack), and their own on top.
    UserWrite(u8, Vec<u8>, Key, bool),
    /// Move a user's draft onto main's head.
    Publish(u8),
    /// Resolve one of a worktree's conflicts (a user's, with `.1`), for
    /// the database's side (`.3`) or the file's.
    Resolve(u8, bool, u8, bool),
    /// Delete a worktree other than main's.
    Delete(u8),
    /// A rewrite (rebase, reset, force-push): a worktree's HEAD moves to a
    /// new commit on top of an older segment, with one key changed.
    Rebuild(u8, u8, Key, bool),
    /// A commit made outside the database moving a record to the other
    /// file, edited too with `.2`, then scanned.
    Move(u8, Key, bool),
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
        1 => (any::<u8>(), any::<u8>(), key.clone(), prop::bool::weighted(0.2)).prop_map(|(w, b, k, d)| Op::Rebuild(w, b, k, d)),
        2 => (any::<u8>(), key, any::<bool>()).prop_map(|(w, k, e)| Op::Move(w, k, e)),
    ]
}

/// Weighted toward publishing after moves, rewrites and main's changes,
/// where short histories find the most.
fn publish_op() -> impl Strategy<Value = Op> {
    let key = 0..KEYS;
    prop_oneof![
        2 => (any::<u8>(), key.clone(), prop::bool::weighted(0.2)).prop_map(|(w, k, d)| Op::Write(w, k, d)),
        1 => any::<u8>().prop_map(Op::Commit),
        2 => (any::<u8>(), prop::collection::vec((key.clone(), prop::bool::weighted(0.3)), 1..3)).prop_map(|(w, c)| Op::External(w, c)),
        1 => (any::<u8>(), key.clone(), prop::bool::weighted(0.2)).prop_map(|(w, k, d)| Op::DiskEdit(w, k, d, FileWins::Never)),
        1 => (any::<u8>(), 0u8..4).prop_map(|(w, b)| Op::Fork(w, b)),
        2 => Just(Op::NewUser),
        6 => (any::<u8>(), prop::collection::vec(any::<u8>(), 0..2), key.clone(), prop::bool::weighted(0.2)).prop_map(|(u, b, k, d)| Op::UserWrite(u, b, k, d)),
        3 => any::<u8>().prop_map(Op::Publish),
        1 => (any::<u8>(), any::<bool>(), any::<u8>(), any::<bool>()).prop_map(|(w, u, i, o)| Op::Resolve(w, u, i, o)),
        3 => (any::<u8>(), any::<u8>(), key.clone(), prop::bool::weighted(0.3)).prop_map(|(w, b, k, d)| Op::Rebuild(w, b, k, d)),
        3 => (any::<u8>(), key, any::<bool>()).prop_map(|(w, k, e)| Op::Move(w, k, e)),
    ]
}

struct World {
    imp: Segments,
    model: Model,
    git: Git,
    next_ver: Ver,
    /// Each commit's records' `key_id`s, as the model has them.
    commit_ids: Vec<BTreeMap<Key, Ver>>,
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
        let ids = tree.clone();
        World {
            imp,
            model: Model {
                wts: vec![OWt::new(tree, ids.clone(), false)],
                absent: BTreeMap::new(),
            },
            git,
            next_ver: KEYS as Ver + 1,
            commit_ids: vec![ids],
        }
    }

    /// Record the model's ids for the commit just made.
    fn committed(&mut self, w: Wt) {
        self.commit_ids.push(self.model.wts[w].ids.clone());
        assert_eq!(self.commit_ids.len(), self.git.commits.len());
    }

    /// A commit made outside the database with these changes, checked out
    /// in `w` and scanned. A record that left one file as it arrived in the
    /// other moved, keeping its `key_id`.
    fn external(&mut self, w: Wt, changes: BTreeMap<Key, Option<Ver>>) {
        let before = self.model.wts[w].committed.clone();
        let m = &mut self.model.wts[w];
        let old_ids = m.ids.clone();
        for (&k, &change) in &changes {
            if let Some(v) = change {
                let from = other_file(k);
                let id = if before.contains_key(&k) {
                    old_ids[&k]
                } else if before.contains_key(&from) && changes.get(&from) == Some(&None) {
                    old_ids[&from]
                } else {
                    v
                };
                m.ids.insert(k, id);
            }
        }
        for (&k, &change) in &changes {
            match change {
                Some(v) => {
                    m.committed.insert(k, v);
                    m.disk.insert(k, v);
                }
                None => {
                    m.committed.remove(&k);
                    m.disk.remove(&k);
                    m.ids.remove(&k);
                }
            };
        }
        m.follow_records();
        let files: BTreeSet<Key> = changes.keys().map(|&k| file_of(k)).collect();
        for &f in &files {
            m.reconcile(f, &mut self.next_ver, FileWins::Never);
        }
        let c = self.git.commit(m.committed.clone());
        let disk = m.disk.clone();
        let changes: Vec<(Key, Option<Ver>)> = changes.into_iter().collect();
        self.imp.scan(w, &changes, c, &disk);
        self.committed(w);
        if w == MAIN {
            self.main_moved(&before, &BTreeMap::new());
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
                // deleting a record the view doesn't show is NotFound
                if deleted && !self.model.wts[w].view().contains_key(&k) {
                    return;
                }
                let ver = self.ver();
                let own = self.imp.own_view(w);
                // the record it replaces, else a new one
                let m = &self.model.wts[w];
                let record = m
                    .draft
                    .get(&k)
                    .map(|d| d.id)
                    .or(m.ids.get(&k).copied())
                    .unwrap_or(ver);
                self.imp
                    .write(w, &own, k, Value { ver, deleted }, true, record);
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
                // a re-create, with no base: live nowhere in its view
                let live_anywhere = m.draft.values().any(|d| d.id == record && !d.deleted)
                    || m.ids.values().any(|&i| i == record);
                let shown = m.committed.get(&k).copied();
                let settled = if m.draft.contains_key(&k) {
                    BTreeSet::new()
                } else {
                    m.ids.get(&k).copied().into_iter().collect()
                };
                m.edit(
                    k,
                    Value { ver, deleted },
                    shown,
                    BTreeSet::new(),
                    record,
                    settled,
                );
                let d = m.draft.get_mut(&k).unwrap();
                d.continues = continues;
                if !live_anywhere {
                    if let Origin::Edit(e) = &mut d.origin {
                        e.base = None;
                    }
                }
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
                if want.is_some() {
                    self.committed(w);
                }
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
                let changes: BTreeMap<Key, Option<Ver>> = changes
                    .iter()
                    .map(|&(k, deleted)| (k, (!deleted).then(|| self.ver())))
                    .collect();
                self.external(w, changes);
            }
            Op::Move(n, k, edited) => {
                let w = self.pick(n, false).unwrap();
                let to = other_file(k);
                let m = &self.model.wts[w];
                let (Some(&v), false) = (m.committed.get(&k), m.committed.contains_key(&to)) else {
                    return;
                };
                let v = if edited { self.ver() } else { v };
                self.external(w, BTreeMap::from([(k, None), (to, Some(v))]));
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
                let history = self.imp.wts[w].history.clone();
                let f = self.imp.fork(w, pos, &self.git);
                let mut ids = self.commit_ids[c].clone();
                // Where no rollup names a record's id at `c`, the split falls
                // back on the ids it has, which can miss a record deleted and
                // re-created after `c` outside git-sync.
                let chain = self.imp.chain_set(f);
                for (k, id) in ids.iter_mut() {
                    if rollup_id(&self.git, &history, *k, c).is_none() {
                        if let Some(got) = self.imp.live_id(&chain, *k) {
                            *id = got;
                        }
                    }
                }
                self.commit_ids[c] = ids.clone();
                self.model
                    .wts
                    .push(OWt::new(self.git.commits[c].clone(), ids, false));
            }
            Op::NewUser => {
                let pos = self.imp.wts[MAIN].history.len() - 1;
                self.imp.fork(MAIN, pos, &self.git);
                let main = &self.model.wts[MAIN];
                let (committed, ids) = (main.committed.clone(), main.ids.clone());
                let mut user = OWt::new(committed, ids, true);
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
                let old = self.model.wts[u].draft.get(&k).map(|d| d.ver);
                let rows = self.model.stack_rows(&stack, k, None);
                // deleting a record the view doesn't show is NotFound
                if deleted && !rows.iter().any(|&(_, dead)| !dead) {
                    return;
                }
                // the record it replaces: its own edit's, else the live row
                // the stack shows, main's first, else its own view's
                let shows = |v: Ver| rows.contains(&(v, false));
                let record = {
                    let model = &self.model;
                    let main = &model.wts[MAIN];
                    model.wts[u]
                        .draft
                        .get(&k)
                        .map(|d| d.id)
                        .or(main
                            .shown(k)
                            .filter(|&(v, dead)| !dead && shows(v))
                            .and_then(|_| main.shown_id(k)))
                        .or(stack.iter().find_map(|&v| {
                            model.wts[v]
                                .draft
                                .get(&k)
                                .filter(|d| !d.deleted && shows(d.ver))
                                .map(|d| d.id)
                        }))
                        .or(model.wts[u].ids.get(&k).copied())
                        .unwrap_or(ver)
                };
                let rows = self.model.stack_rows(&stack, k, Some((u, record)));
                let shows = |v: Ver| rows.contains(&(v, false));
                // the other records the stack or its own view shows at `k`
                let settled: BTreeSet<Ver> = {
                    let model = &self.model;
                    let main = &model.wts[MAIN];
                    let mut ids: BTreeSet<Ver> = main
                        .shown(k)
                        .filter(|&(v, dead)| !dead && shows(v))
                        .and_then(|_| main.shown_id(k))
                        .into_iter()
                        .collect();
                    ids.extend(stack.iter().filter(|&&v| v != u).filter_map(|&v| {
                        model.wts[v]
                            .draft
                            .get(&k)
                            .filter(|d| !d.deleted && shows(d.ver))
                            .map(|d| d.id)
                    }));
                    let own = &model.wts[u];
                    if !own.draft.contains_key(&k) && own.committed.contains_key(&k) {
                        ids.extend(own.ids.get(&k));
                    }
                    ids
                };
                self.imp
                    .write(u, &via, k, Value { ver, deleted }, true, record);
                // the base: a re-create has none, when the stack shows the
                // record live nowhere, a draft's live row included, the user's
                // own too; else main's committed version of it
                let lower: Vec<Wt> = stack.iter().copied().filter(|&v| v != u).collect();
                let own_has = self.model.wts[u]
                    .draft
                    .values()
                    .any(|d| d.id == record && !d.deleted);
                let live_anywhere = own_has
                    || (0..KEYS).any(|j| {
                        let rows_j = self.model.stack_rows(&lower, j, None);
                        let main = &self.model.wts[MAIN];
                        let main_live = main.shown(j).is_some_and(|(v, dead)| {
                            !dead
                                && main.shown_id(j) == Some(record)
                                && rows_j.contains(&(v, false))
                        });
                        main_live
                            || lower.iter().any(|&v| {
                                self.model.wts[v].draft.get(&j).is_some_and(|d| {
                                    d.id == record && !d.deleted && rows_j.contains(&(d.ver, false))
                                })
                            })
                    });
                let main = &self.model.wts[MAIN];
                let shown = main
                    .ids
                    .iter()
                    .find(|&(_, &i)| i == record)
                    .and_then(|(j, _)| main.committed.get(j).copied());
                let rows_at_k: Vec<Ver> = rows.iter().map(|&(v, _)| v).collect();
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
                let mut seen_at: BTreeSet<(Key, Ver)> = rows_at_k
                    .iter()
                    .filter(|&&v| Some(v) != old)
                    .map(|&v| (k, v))
                    .collect();
                match user.draft.get(&k) {
                    Some(old) => seen_at.extend(&old.seen_at),
                    None => seen_at.extend(user.committed.get(&k).map(|&v| (k, v))),
                }
                user.edit(k, Value { ver, deleted }, shown, seen, record, settled);
                let d = user.draft.get_mut(&k).unwrap();
                d.seen_at.extend(seen_at);
                // live nowhere: the write re-creates the record
                if !live_anywhere {
                    if let Origin::Edit(e) = &mut d.origin {
                        e.base = None;
                    }
                }
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
                let main_ids = self.model.wts[MAIN].ids.clone();
                // each record's versions the model knows: edits', and main's
                let versions: BTreeSet<(Ver, Ver)> = self
                    .model
                    .wts
                    .iter()
                    .flat_map(|m| m.draft.values().map(|d| (d.id, d.ver)))
                    .chain(committed.iter().map(|(k, &v)| (main_ids[k], v)))
                    .collect();
                let user = &mut self.model.wts[u];
                // each edit's record before it follows it or is renewed
                let orig: BTreeMap<Ver, Ver> = user.draft.values().map(|d| (d.ver, d.id)).collect();
                // an edit follows its record to the file main has it in
                for k in user.draft.keys().copied().collect::<Vec<_>>() {
                    let id = user.draft[&k].id;
                    let to = (0..KEYS)
                        .find(|t| committed.contains_key(t) && main_ids.get(t) == Some(&id));
                    let vacated = !committed
                        .contains_key(&k)
                        .then(|| main_ids[&k])
                        .is_some_and(|r| user.draft[&k].settled.contains(&r));
                    if to.is_some_and(|to| vacated && to != k && user.draft.contains_key(&to)) {
                        // another edit holds that key: this one stays, a new
                        // record, and what it saw of the old one elsewhere goes
                        let d = user.draft.get_mut(&k).unwrap();
                        d.id = d.ver;
                        d.seen_at.retain(|&(sk, _)| sk == k);
                    } else if let Some(to) = to.filter(|&to| vacated && to != k) {
                        let mut d = user.draft.remove(&k).unwrap();
                        // what it saw at the key it leaves no longer hides
                        // anything, but versions of its own record
                        let id = d.id;
                        d.seen_at
                            .retain(|&(sk, v)| sk != k || versions.contains(&(id, v)));
                        user.draft.insert(to, d);
                        if let Some(c) = user.conflicts.remove(&k) {
                            user.conflicts.insert(to, c);
                        }
                    }
                }
                // Entries belong to the draft, not one edit: main's value is
                // seen if any edit saw it here, or where a move brought it from.
                let seen_at: BTreeSet<(Key, Ver)> = user
                    .draft
                    .values()
                    .flat_map(|d| d.seen_at.iter().copied())
                    .collect();
                let seen_ids: BTreeSet<Ver> = user
                    .draft
                    .values()
                    .flat_map(|d| d.based_on.iter().copied())
                    .collect();
                let drafts: Vec<ODraft> = user.draft.values().cloned().collect();
                let seen = |k: Key| match committed.get(&k) {
                    Some(&v) => {
                        seen_at.contains(&(k, v))
                            || drafts
                                .iter()
                                .any(|d| main_ids.get(&k) == Some(&d.id) && d.based_on.contains(&v))
                    }
                    None => !seen_ids.is_disjoint(&states[&k]),
                };
                let keys: Vec<Key> = user.draft.keys().copied().collect();
                for k in keys {
                    let main = committed.get(&k).copied();
                    // a live edit made over a version of a record main has
                    // since deleted
                    let d = &user.draft[&k];
                    let id = orig[&d.ver];
                    let deleted = !d.deleted
                        && matches!(d.origin, Origin::Edit(Edit { base: Some(_), .. }))
                        && !main_ids.values().any(|&i| i == id);
                    if deleted {
                        user.settle(k, main);
                        continue;
                    }
                    // renewed, it adds a record: over an absence, whatever
                    // absence it was, that's no conflict
                    if id != d.id && main.is_none() {
                        user.conflicts.remove(&k);
                        continue;
                    }
                    if !seen(k) {
                        user.settle(k, main);
                    } else if user.conflicts.get(&k).is_some_and(|c| c.theirs != main) {
                        // on top of main's value: a conflict with any other is over
                        user.conflicts.remove(&k);
                    }
                }
                // main's versions are its own tree's now: where it holds no
                // edit, it shows them
                let held: BTreeSet<Key> = user.draft.keys().copied().collect();
                for d in user.draft.values_mut() {
                    d.seen_at
                        .retain(|(sk, v)| held.contains(sk) || committed.get(sk) != Some(v));
                }
                user.committed = committed;
                user.absent = self.model.absent.clone();
                user.ids = main_ids;
                for (k, d) in user.draft.iter_mut() {
                    d.based_on.extend(&states[k]);
                    d.seen_at.extend(user.committed.get(k).map(|&v| (*k, v)));
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
                let base_commit = base_idx.map(|i| {
                    let seg = self.imp.wts[w].chain[i].0;
                    self.imp.segs[seg].head_commit.unwrap()
                });
                let mut tree =
                    base_commit.map_or_else(BTreeMap::new, |c| self.git.commits[c].clone());
                let base_ids =
                    base_commit.map_or_else(BTreeMap::new, |c| self.commit_ids[c].clone());
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
                // no rollup: the base's record continues, else the one this
                // worktree had, else it's new
                let taken: BTreeSet<Ver> = base_ids
                    .iter()
                    .filter(|(k, _)| tree.contains_key(k))
                    .map(|(_, &id)| id)
                    .collect();
                m.ids = tree
                    .iter()
                    .map(|(&k, &v)| {
                        let id = base_ids
                            .get(&k)
                            .or(m.ids.get(&k).filter(|id| !taken.contains(id)))
                            .copied()
                            .unwrap_or(v);
                        (k, id)
                    })
                    .collect();
                let before = std::mem::replace(&mut m.committed, tree);
                m.follow_records();
                let disk = m.disk.clone();
                for f in 0..KEYS / KEYS_PER_FILE {
                    m.reconcile(f, &mut self.next_ver, FileWins::Never);
                    self.imp.reconcile(w, f, &disk, FileWins::Never);
                }
                self.committed(w);
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
            // a user branch's own view merges copies of a record too
            let own = self.imp.own_view(w);
            let rows = if m.user {
                self.imp.visible_merged(&own, None)
            } else {
                self.imp.visible(&own, None)
            };
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
            let ids = |rows: &[RowId]| -> BTreeMap<Key, Ver> {
                rows.iter()
                    .map(|r| &self.imp.rows[r])
                    .filter(|r| !r.deleted)
                    .map(|r| (r.key, r.id))
                    .collect()
            };
            assert_eq!(
                ids(&rows),
                m.view_ids(),
                "step {step} ({op:?}): worktree {w}'s key_ids"
            );
            assert_eq!(
                live(rows),
                m.view(),
                "step {step} ({op:?}): worktree {w}'s view"
            );
            // the committed segments equal git exactly
            let chain = self.imp.visible(&self.imp.chain_set(w), None);
            assert_eq!(
                ids(&chain),
                m.ids,
                "step {step} ({op:?}): worktree {w}'s committed key_ids"
            );
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
                        m.draft.values().any(|d| d.based_on.contains(&t))
                            && chain_rows.iter().any(|r| {
                                let r = &self.imp.rows[r];
                                r.key == *k && r.ver == t && r.recreated
                            })
                    });
                // An extra conflict with a deletion, only where the edit was
                // written over a re-created row at its key: an entry that
                // would have hid it was lost with the row it was on, so the
                // record showed live.
                let draft = self.imp.wts[w].draft;
                let deletion_over_report = m.user
                    && !want.contains_key(k)
                    && got[k].0.is_none()
                    && self.imp.rows.iter().any(|(id, r)| {
                        r.recreated
                            && r.key == *k
                            && !r.deleted
                            && self.imp.sup.contains(&(*id, draft))
                    });
                assert!(
                    over_report || seen_copy || deletion_over_report,
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
                    .visible_merged(&self.imp.layered(MAIN, &stack), None)
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
                    // at this key, or at one a move brought it from
                    let edited_over = stack.iter().any(|&u| {
                        self.model.wts[u]
                            .draft
                            .values()
                            .any(|d| d.based_on.contains(&v))
                    });
                    // the same record as an edit in another file, made over
                    // this version: a copy the client merges by key_id
                    let other_file_copy = rows.iter().any(|r| {
                        r.key == k
                            && r.ver == v
                            && stack.iter().any(|&u| {
                                self.model.wts[u].draft.iter().any(|(&dk, d)| {
                                    dk != k && d.id == r.id && d.based_on.contains(&v)
                                })
                            })
                    });
                    assert!(
                        (recreated && edited_over) || other_file_copy,
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

    #[test]
    fn publishing_agrees_with_the_model(ops in prop::collection::vec(publish_op(), 1..16)) {
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

/// A record a user edited moves to the other file unchanged: the moved row
/// takes over the user's entry, so the user's layered view shows only
/// their edit. Checked on the implementation directly, since the model
/// allows a moved row to show as an extra copy.
#[test]
fn move_carries_entries_to_the_moved_row() {
    let mut world = World::new();
    for op in [
        Op::External(0, vec![(other_file(1), true)]),
        Op::NewUser,
        Op::UserWrite(0, vec![], 1, false),
        Op::Move(0, 1, false),
    ] {
        world.apply(&op);
    }
    assert!(world.model.wts[MAIN].committed.contains_key(&other_file(1)));
    let rows: BTreeSet<(Key, Ver)> = world
        .imp
        .visible(&world.imp.layered(MAIN, &[1]), None)
        .iter()
        .map(|r| &world.imp.rows[r])
        .filter(|r| !r.deleted)
        .map(|r| (r.key, r.ver))
        .collect();
    let edit = world.model.wts[1].draft[&1].ver;
    assert!(rows.contains(&(1, edit)), "{rows:?}");
    assert!(
        !rows.iter().any(|&(k, _)| k == other_file(1)),
        "the moved row shows beside the edit: {rows:?}"
    );
}

/// A record moves to the other file, then a fork at the commit before the
/// move and a rebuild: the id pairing gave the moved row carries through both.
#[test]
fn scan_pairing_keeps_a_moved_records_id() {
    run(&[
        Op::Rebuild(0, 0, 5, false),
        Op::Move(0, 5, false),
        Op::Fork(0, 1),
        Op::Rebuild(0, 8, 0, false),
    ]);
}

/// Main's edit, held back from a commit by a hand deletion, is resolved for
/// the file. Users writing and publishing after see main's value, not a
/// conflict: fold step 4 and the re-link after it keep the draft in step.
#[test]
fn fold_step_4_rewrites_a_resolved_file_row() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 2, false),
        Op::Write(0, 2, false),
        Op::DiskEdit(0, 2, true, FileWins::Never),
        Op::Commit(0),
        Op::NewUser,
        Op::NewUser,
        Op::UserWrite(1, vec![], 2, false),
        Op::Resolve(0, false, 0, false),
        Op::UserWrite(21, vec![7], 2, false),
        Op::Publish(33),
    ]);
}

/// Main moves a record a user edits into a key the user also edits. Main's
/// copy stays merged away while the user edits that record elsewhere, and
/// shows once publishing makes the edit a new record.
#[test]
fn merged_copy_shows_once_its_edit_is_a_new_record() {
    run(&[
        Op::NewUser,
        Op::Fork(0, 0),
        Op::Fork(0, 0),
        Op::External(180, vec![(3, true)]),
        Op::Fork(0, 0),
        Op::UserWrite(0, vec![], 3, false),
        Op::UserWrite(0, vec![], 0, false),
        Op::Move(84, 0, false),
        Op::Write(4, 3, false),
        Op::UserWrite(0, vec![], 3, false),
        Op::Publish(0),
    ]);
}

/// Main's edit follows its record to the other file, then the file wins over
/// it. Every entry the edit made goes with it, so after a rebuild the
/// record's old row shows again.
#[test]
fn withdrawn_edit_takes_its_entries_with_it() {
    run(&[
        Op::NewUser,
        Op::External(0, vec![(1, true)]),
        Op::Write(0, 4, false),
        Op::Move(0, 4, true),
        Op::Write(0, 4, false),
        Op::DiskEdit(0, 0, false, FileWins::Always),
        Op::DiskEdit(0, 3, false, FileWins::Always),
        Op::Rebuild(0, 68, 0, false),
    ]);
}

/// A user's edit follows its record at publish, and a new edit of another
/// record takes the key it left. Resolving the first for the file leaves
/// the old record's tombstone hidden by the second's tag.
#[test]
fn write_tags_entries_already_at_its_key() {
    run(&[
        Op::DiskEdit(0, 4, true, FileWins::Never),
        Op::Commit(0),
        Op::Fork(0, 0),
        Op::NewUser,
        Op::Fork(0, 0),
        Op::Move(120, 1, false),
        Op::External(21, vec![(4, false)]),
        Op::UserWrite(0, vec![], 1, false),
        Op::Publish(0),
        Op::UserWrite(0, vec![], 1, false),
        Op::Resolve(0, true, 0, false),
    ]);
}

/// A user's edit at a key doesn't claim main's row there of a record the
/// user edits elsewhere: the merge hid it. When publishing makes that edit
/// a new record, the row shows.
#[test]
fn write_leaves_a_merged_row_it_never_saw() {
    run(&[
        Op::NewUser,
        Op::External(0, vec![(2, true)]),
        Op::Move(0, 5, false),
        Op::DiskEdit(0, 2, false, FileWins::Never),
        Op::UserWrite(0, vec![], 2, false),
        Op::DiskEdit(0, 0, false, FileWins::Never),
        Op::UserWrite(0, vec![], 5, false),
        Op::UserWrite(0, vec![], 2, false),
        Op::Publish(0),
    ]);
}

/// A user moves their edit of a record to where main deleted it, while the
/// merge by key_id hides main's tombstone. They saw their own edit, not the
/// deletion, so publishing conflicts.
#[test]
fn own_edit_misses_a_deletion_the_merge_hides() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 5, false),
        Op::UserWrite(0, vec![], 2, false),
        Op::External(0, vec![(2, true)]),
        Op::Move(0, 5, false),
        Op::Rebuild(0, 49, 0, false),
        Op::DiskEdit(0, 3, false, FileWins::Never),
        Op::UserWrite(0, vec![], 2, false),
        Op::Publish(0),
    ]);
}

/// Two draft rows share a record's id. Re-identifying one leaves the entries
/// at the other's key, so a rebuild can't show a row beside it.
#[test]
fn retag_leaves_another_rows_entries() {
    run(&[
        Op::External(0, vec![(5, true)]),
        Op::Rebuild(0, 0, 2, false),
        Op::Fork(0, 0),
        Op::Move(71, 2, false),
        Op::DiskEdit(43, 5, true, FileWins::Never),
        Op::DiskEdit(89, 2, false, FileWins::Never),
        Op::Rebuild(1, 188, 0, false),
    ]);
}

/// A write replacing a draft row with a new record moves the old row's
/// entries to it, so none stay behind once the row goes.
#[test]
fn write_retags_the_row_it_replaces() {
    run(&[
        Op::Fork(0, 0),
        Op::NewUser,
        Op::Delete(2),
        Op::External(0, vec![(0, false)]),
        Op::Write(0, 3, false),
        Op::NewUser,
        Op::DiskEdit(0, 2, false, FileWins::Never),
        Op::UserWrite(0, vec![], 0, false),
        Op::Rebuild(0, 33, 2, true),
        Op::NewUser,
        Op::DiskEdit(0, 2, false, FileWins::Never),
        Op::External(0, vec![(2, false)]),
        Op::NewUser,
        Op::Delete(2),
        Op::Rebuild(0, 69, 3, false),
    ]);
}

/// A user's edit of a record moves to another key: their own tree's row of
/// another record at the key it left shows again, in their view and in a
/// stack.
#[test]
fn replaced_edit_shows_its_own_trees_row_again() {
    run(&[
        Op::Write(0, 3, true),
        Op::Commit(0),
        Op::Move(0, 0, false),
        Op::Fork(0, 1),
        Op::Write(0, 1, false),
        Op::NewUser,
        Op::UserWrite(0, vec![], 3, false),
        Op::Rebuild(30, 24, 3, false),
        Op::UserWrite(0, vec![], 3, false),
        Op::Publish(0),
        Op::UserWrite(0, vec![], 0, false),
    ]);
}

/// A user's edit moves away from a key after seeing main's row there. Once
/// published, main's rows are the user's own tree, and the key shows it.
#[test]
fn publish_shows_main_rows_where_the_draft_holds_no_edit() {
    run(&[
        Op::Write(0, 3, false),
        Op::NewUser,
        Op::External(0, vec![(2, true)]),
        Op::UserWrite(0, vec![], 5, false),
        Op::Write(0, 3, false),
        Op::External(0, vec![(2, false), (5, true)]),
        Op::NewUser,
        Op::External(0, vec![(5, false)]),
        Op::Write(0, 0, false),
        Op::NewUser,
        Op::UserWrite(31, vec![], 2, false),
        Op::UserWrite(180, vec![], 5, false),
        Op::UserWrite(63, vec![76], 2, false),
        Op::Publish(45),
    ]);
}

/// A user's edit written over another user's edit moves to another key. The
/// draft keeps hiding what it saw at the key it left.
#[test]
fn replaced_edit_keeps_hiding_what_it_saw() {
    run(&[
        Op::NewUser,
        Op::Write(0, 1, true),
        Op::Commit(0),
        Op::UserWrite(0, vec![], 1, false),
        Op::NewUser,
        Op::Move(0, 4, false),
        Op::UserWrite(19, vec![48], 1, false),
        Op::UserWrite(7, vec![], 4, false),
    ]);
}

/// A fork at an older commit splits a segment: entries on rows the new
/// segment keeps move with them.
#[test]
fn split_moves_entries_on_rows_after_the_split_point() {
    run(&[
        Op::Fork(0, 0),
        Op::External(103, vec![(0, false)]),
        Op::External(7, vec![(1, false)]),
        Op::Fork(19, 1),
    ]);
}

/// A conflict resolved for the database's side stays resolved when a later
/// scan finds the file unchanged under it.
#[test]
fn scan_keeps_a_resolution_the_file_hasnt_moved_under() {
    run(&[
        Op::Fork(0, 0),
        Op::NewUser,
        Op::Write(14, 2, false),
        Op::Fork(0, 0),
        Op::NewUser,
        Op::External(129, vec![(2, false)]),
        Op::Delete(20),
        Op::Resolve(72, false, 0, true),
        Op::External(50, vec![(0, false)]),
    ]);
}

/// A hand edit scanned with the file winning where it diverged doesn't undo
/// a resolution the file hasn't moved under.
#[test]
fn diverged_file_leaves_a_standing_resolution() {
    run(&[
        Op::Write(0, 2, false),
        Op::DiskEdit(0, 2, false, FileWins::Never),
        Op::Resolve(0, false, 0, true),
        Op::DiskEdit(0, 0, false, FileWins::Diverged),
    ]);
}

/// A split restoring a row after a move puts back the entries newer
/// segments had on it, so no second row shows at its key.
#[test]
fn split_restores_the_entries_on_a_row_it_brings_back() {
    run(&[
        Op::Rebuild(0, 0, 2, false),
        Op::Move(0, 2, false),
        Op::Write(0, 2, false),
        Op::Fork(0, 1),
    ]);
}

/// A move carries entries only an edit of the moved record made: a user's
/// edit of another record at the old key doesn't follow it.
#[test]
fn move_carries_only_its_records_entries() {
    run(&[
        Op::DiskEdit(0, 2, true, FileWins::Never),
        Op::NewUser,
        Op::UserWrite(0, vec![], 5, false),
        Op::Commit(0),
        Op::UserWrite(0, vec![], 2, false),
        Op::Move(0, 5, true),
        Op::Publish(0),
        Op::Move(0, 2, false),
        Op::DiskEdit(0, 0, false, FileWins::Never),
        Op::Publish(0),
        Op::Resolve(0, true, 0, false),
    ]);
}

/// A user's edit, moved to follow its record, is written again through
/// another user's edit of it, after rebuilds deleted the record. They saw
/// the other user's edit, not the deletion, so publishing conflicts.
#[test]
fn edit_through_another_users_edit_misses_mains_deletion() {
    run(&[
        Op::Rebuild(0, 0, 5, false),
        Op::NewUser,
        Op::Move(0, 5, false),
        Op::NewUser,
        Op::UserWrite(19, vec![], 2, false),
        Op::Rebuild(0, 2, 5, false),
        Op::Rebuild(0, 0, 0, false),
        Op::UserWrite(0, vec![], 5, false),
        Op::UserWrite(11, vec![20], 5, false),
        Op::Publish(47),
    ]);
}

/// An edit whose record moves to a key another edit holds stays put as a
/// new record, so no two files share the id.
#[test]
fn edit_that_cant_follow_becomes_a_new_record() {
    run(&[
        Op::DiskEdit(0, 1, false, FileWins::Never),
        Op::Rebuild(0, 0, 0, false),
        Op::Commit(0),
        Op::Write(0, 4, false),
        Op::Write(0, 1, false),
        Op::Move(0, 1, false),
    ]);
}

/// An edit moved to its record's new key at publish keeps only the entries
/// an edit of that record made on it.
#[test]
fn relocated_edit_keeps_only_its_records_entries() {
    run(&[
        Op::Rebuild(0, 0, 0, false),
        Op::Write(0, 1, false),
        Op::NewUser,
        Op::Commit(0),
        Op::UserWrite(0, vec![], 1, false),
        Op::Move(0, 1, false),
        Op::Publish(0),
        Op::Move(0, 4, false),
        Op::NewUser,
        Op::Write(0, 4, false),
        Op::UserWrite(215, vec![188], 4, false),
        Op::Publish(32),
    ]);
}

/// A user's edit returns to the key main moved its record from, then a
/// rebuild drops the record. The user saw the key empty, but only because
/// of the move, so the edit conflicts with the deletion.
#[test]
fn edit_conflicts_with_a_deletion_it_saw_only_as_a_move() {
    run(&[
        Op::NewUser,
        Op::Rebuild(0, 125, 3, false),
        Op::Move(0, 3, false),
        Op::UserWrite(0, vec![], 0, false),
        Op::UserWrite(0, vec![], 3, false),
        Op::Rebuild(0, 0, 1, false),
        Op::Publish(0),
    ]);
}

/// Main moves a record a user then edits at the old key, and a rebuild
/// drops it. The user saw the record live at its new key, never deleted, so
/// publishing conflicts.
#[test]
fn edit_at_a_moves_old_key_misses_a_rebuilds_deletion() {
    run(&[
        Op::NewUser,
        Op::External(0, vec![(4, true)]),
        Op::UserWrite(0, vec![], 1, false),
        Op::Publish(0),
        Op::Move(0, 1, false),
        Op::UserWrite(0, vec![], 1, false),
        Op::Rebuild(0, 47, 0, false),
        Op::Publish(0),
    ]);
}

/// A user's deletion agrees with main's at publishing, and they then write
/// the record again. Their tree has held the deletion since, so it's no
/// conflict.
#[test]
fn rewrite_after_publishing_over_a_deletion_saw_it() {
    run(&[
        Op::NewUser,
        Op::Rebuild(0, 254, 0, false),
        Op::UserWrite(0, vec![], 2, true),
        Op::NewUser,
        Op::Rebuild(0, 7, 0, false),
        Op::Write(0, 0, false),
        Op::Fork(0, 0),
        Op::Rebuild(8, 3, 0, false),
        Op::Write(20, 2, false),
        Op::UserWrite(0, vec![], 0, false),
        Op::Publish(44),
        Op::Delete(220),
        Op::Write(0, 0, false),
        Op::UserWrite(0, vec![], 2, false),
        Op::Publish(0),
    ]);
}

/// A user edits a record that main's draft then deletes and main commits.
/// Writing again, the user sees their own edit, not the deletion, so
/// publishing conflicts.
#[test]
fn own_edit_hides_mains_pending_deletion() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 0, false),
        Op::Rebuild(0, 73, 1, false),
        Op::Write(0, 0, true),
        Op::UserWrite(0, vec![], 0, false),
        Op::Write(0, 1, false),
        Op::Commit(0),
        Op::Publish(0),
    ]);
}

/// A user moves their edit of a record to a key where main, after a
/// rebuild, has the record nowhere. Their own edit still showed it live,
/// so the moved edit keeps its base and conflicts with the deletion.
#[test]
fn moved_edit_keeps_its_base_after_a_deletion() {
    run(&[
        Op::External(0, vec![(2, true)]),
        Op::NewUser,
        Op::Write(0, 0, false),
        Op::Move(0, 5, false),
        Op::UserWrite(0, vec![], 2, false),
        Op::Rebuild(0, 29, 0, false),
        Op::UserWrite(0, vec![], 5, false),
        Op::Publish(0),
    ]);
}

/// A user edits a record, then writes it again while rewrites and moves
/// around it end with main deleting it. Their own edit is what they saw, so
/// publishing conflicts.
#[test]
fn rebuilds_deletion_under_an_own_edit_is_unseen() {
    run(&[
        Op::Write(0, 0, false),
        Op::Write(0, 0, false),
        Op::NewUser,
        Op::UserWrite(0, vec![], 3, false),
        Op::Rebuild(0, 43, 0, false),
        Op::DiskEdit(0, 0, false, FileWins::Never),
        Op::NewUser,
        Op::Write(0, 0, false),
        Op::Move(0, 0, false),
        Op::Move(0, 3, false),
        Op::UserWrite(6, vec![], 3, false),
        Op::Rebuild(0, 0, 0, true),
        Op::Publish(62),
    ]);
}

/// Main's pending edit of another record covers the key a move took a
/// user's record to. The user writes over their own edit, and a rebuild
/// then deletes the record, which they never saw deleted: a conflict.
#[test]
fn deletion_after_a_rewrite_of_an_own_edit_is_unseen() {
    run(&[
        Op::Write(0, 2, false),
        Op::Rebuild(0, 0, 5, false),
        Op::NewUser,
        Op::UserWrite(0, vec![], 5, false),
        Op::Move(0, 5, false),
        Op::UserWrite(0, vec![], 5, false),
        Op::Rebuild(0, 73, 0, false),
        Op::Publish(0),
    ]);
}

/// A user edits over main's deletion of a record. Main then creates another
/// record at that key and deletes it too: that second absence the user
/// never saw, so publishing finds a conflict.
#[test]
fn another_records_deletion_at_the_key_is_unseen() {
    run(&[
        Op::NewUser,
        Op::UserWrite(220, vec![], 1, false),
        Op::Rebuild(209, 255, 2, true),
        Op::Move(95, 2, true),
        Op::UserWrite(96, vec![], 1, false),
        Op::NewUser,
        Op::Rebuild(243, 39, 1, false),
        Op::Rebuild(55, 196, 4, false),
        Op::Rebuild(52, 47, 5, true),
        Op::Publish(140),
    ]);
}

/// A user edits a record, and main's draft and then a rebuild delete it.
/// Writing again, the user sees their own edit, not the deletion, so
/// publishing conflicts.
#[test]
fn rewriting_an_own_edit_misses_a_rebuilds_deletion() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 1, false),
        Op::Write(0, 1, true),
        Op::Rebuild(0, 187, 0, false),
        Op::UserWrite(0, vec![], 1, false),
        Op::Publish(0),
    ]);
}

/// Main moves a record, a user edits it at its old key over the move's
/// tombstone, and main then deletes it at its new key. The user saw only
/// the move, so publishing finds the deletion unseen.
#[test]
fn deletion_at_the_records_new_key_is_unseen() {
    run(&[
        Op::NewUser,
        Op::DiskEdit(0, 1, true, FileWins::Never),
        Op::Fork(0, 0),
        Op::Commit(104),
        Op::External(8, vec![(1, false), (4, true)]),
        Op::UserWrite(0, vec![], 1, false),
        Op::UserWrite(0, vec![], 4, false),
        Op::External(74, vec![(1, true)]),
        Op::Publish(0),
    ]);
}

/// Main's pending edit of a record covers a rebuild's deletion of it. A
/// user writing over that edit twice never saw the deletion.
#[test]
fn deletion_under_mains_pending_edit_is_unseen() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 4, false),
        Op::Write(0, 4, false),
        Op::Rebuild(0, 5, 0, false),
        Op::UserWrite(0, vec![], 4, false),
        Op::UserWrite(0, vec![], 4, false),
        Op::Write(0, 0, false),
        Op::Publish(0),
    ]);
}

/// Main moves a record a user edits, then a rebuild deletes it and puts a
/// new record at the user's key. The conflict is with that new value.
#[test]
fn deletion_elsewhere_under_a_new_value_conflicts_with_the_value() {
    run(&[
        Op::Fork(0, 0),
        Op::NewUser,
        Op::UserWrite(0, vec![], 2, false),
        Op::Rebuild(48, 5, 2, false),
        Op::DiskEdit(5, 5, false, FileWins::Never),
        Op::Move(20, 2, false),
        Op::Rebuild(20, 0, 2, false),
        Op::Publish(0),
    ]);
}

/// A user writes over main's deletion of a record, which main's rewrites
/// then bring back and delete again. The user saw only the first deletion.
#[test]
fn deletion_after_the_record_came_back_is_unseen() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 5, false),
        Op::External(0, vec![(5, true)]),
        Op::UserWrite(0, vec![], 5, false),
        Op::Rebuild(0, 72, 0, false),
        Op::Rebuild(0, 73, 0, false),
        Op::Publish(0),
    ]);
}

/// Main moves a record back from the key a user edits it at, and then
/// deletes it there without a new tombstone. The user saw it live at the
/// other key, so the deletion is unseen.
#[test]
fn deletion_where_the_record_was_live_when_written_is_unseen() {
    run(&[
        Op::Write(0, 0, true),
        Op::Commit(0),
        Op::Fork(0, 0),
        Op::Move(84, 3, false),
        Op::NewUser,
        Op::UserWrite(0, vec![], 0, false),
        Op::Move(20, 0, false),
        Op::Fork(0, 1),
        Op::UserWrite(0, vec![], 0, false),
        Op::Fork(1, 0),
        Op::Fork(1, 0),
        Op::External(15, vec![(3, true)]),
        Op::Publish(0),
    ]);
}

/// A user writes over another user's deletion of a record that main's
/// rebuild also deleted. What they saw was the other user's tombstone, not
/// main's deletion.
#[test]
fn another_users_tombstone_hides_mains_deletion() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 3, false),
        Op::Rebuild(0, 61, 0, false),
        Op::NewUser,
        Op::Write(0, 0, false),
        Op::Write(0, 0, false),
        Op::UserWrite(91, vec![], 3, true),
        Op::Write(0, 0, false),
        Op::UserWrite(36, vec![43], 3, false),
        Op::Publish(44),
    ]);
}

/// Main rewrites its pending deletion of a record that one user edited
/// over. The new tombstone takes over that user's entry, so a second user
/// writing through the first never saw the deletion.
#[test]
fn rewritten_tombstone_stays_hidden_under_an_edit() {
    run(&[
        Op::NewUser,
        Op::NewUser,
        Op::Fork(0, 0),
        Op::NewUser,
        Op::UserWrite(55, vec![], 0, false),
        Op::DiskEdit(8, 0, true, FileWins::Never),
        Op::UserWrite(150, vec![], 0, false),
        Op::Write(188, 0, true),
        Op::Delete(122),
        Op::UserWrite(55, vec![234], 0, false),
        Op::NewUser,
        Op::UserWrite(0, vec![], 0, false),
        Op::Commit(0),
        Op::Publish(37),
    ]);
}

/// Main's pending tombstone continues a rebuild's absence that one user
/// edited over, taking over their entry. A second user writing through
/// the first never saw it.
#[test]
fn continuing_tombstone_stays_hidden_under_an_edit() {
    run(&[
        Op::NewUser,
        Op::NewUser,
        Op::Fork(0, 0),
        Op::NewUser,
        Op::UserWrite(55, vec![], 0, false),
        Op::Rebuild(2, 55, 1, false),
        Op::UserWrite(150, vec![], 0, false),
        Op::Write(188, 0, true),
        Op::Delete(122),
        Op::UserWrite(55, vec![234], 0, false),
        Op::DiskEdit(0, 1, false, FileWins::Never),
        Op::NewUser,
        Op::UserWrite(0, vec![], 0, false),
        Op::Commit(0),
        Op::Publish(37),
    ]);
}

/// A fork at an older commit splits a segment where one key's value at
/// that commit is the row below, and another's is restored. The restored
/// row can't take the id the row below keeps.
#[test]
fn split_skips_an_id_the_row_below_keeps() {
    run(&[
        Op::Rebuild(0, 0, 3, false),
        Op::Fork(0, 0),
        Op::Move(32, 3, false),
        Op::NewUser,
        Op::Rebuild(62, 190, 3, false),
        Op::External(6, vec![(3, true)]),
        Op::Delete(6),
        Op::Move(0, 0, false),
        Op::External(0, vec![(0, false)]),
        Op::Write(0, 3, false),
        Op::Fork(0, 3),
        Op::Rebuild(224, 190, 1, false),
    ]);
}

/// A user's edit of a record main then deleted is rebased at a publish onto
/// main's rebuilt tree, which has another record at the key. Main then
/// deletes that record, and the commit folds its own old tombstone over the
/// deletion. The user never saw that deletion.
#[test]
fn deletion_after_an_earlier_publish_is_unseen() {
    run(&[
        Op::NewUser,
        Op::External(0, vec![(0, true)]),
        Op::External(0, vec![(0, false)]),
        Op::UserWrite(0, vec![], 0, false),
        Op::Write(0, 0, true),
        Op::Rebuild(0, 12, 1, false),
        Op::Write(0, 1, false),
        Op::Publish(0),
        Op::External(0, vec![(0, true)]),
        Op::Commit(0),
        Op::Publish(0),
    ]);
}

/// A user edits a record main then deletes, and writes again at a key where
/// main deleted another record it had moved there. Their own edits are what
/// they saw, so both deletions conflict.
#[test]
fn own_edit_under_a_merged_tombstone_misses_the_deletion() {
    run(&[
        Op::NewUser,
        Op::Fork(0, 0),
        Op::UserWrite(0, vec![], 4, false),
        Op::NewUser,
        Op::Delete(142),
        Op::External(0, vec![(4, true)]),
        Op::Move(0, 1, false),
        Op::UserWrite(0, vec![], 1, false),
        Op::Write(0, 4, true),
        Op::UserWrite(14, vec![], 4, false),
        Op::Fork(0, 0),
        Op::Delete(164),
        Op::Commit(0),
        Op::Publish(56),
    ]);
}

/// A move carries a user's entry onto main's tombstone at the record's new
/// key, where another user writes through the first over their own edit.
/// They saw their edit, not main's deletion, so publishing conflicts.
#[test]
fn own_edit_misses_a_deletion_another_drafts_entry_hides() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 5, false),
        Op::Write(0, 2, true),
        Op::External(0, vec![(5, true)]),
        Op::NewUser,
        Op::NewUser,
        Op::UserWrite(232, vec![], 2, false),
        Op::Move(0, 2, false),
        Op::Commit(0),
        Op::UserWrite(66, vec![25], 5, false),
        Op::Publish(39),
    ]);
}

/// A user edits a record, main moves it and a rebuild deletes it, and the
/// user writes again while main's pending row covers the key. Their own
/// edit still showed the record, so the edit keeps its base and conflicts.
#[test]
fn write_over_mains_pending_row_keeps_the_edits_base() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 2, false),
        Op::External(0, vec![(5, true)]),
        Op::Move(0, 2, false),
        Op::UserWrite(0, vec![], 2, false),
        Op::Rebuild(0, 181, 0, false),
        Op::DiskEdit(0, 2, false, FileWins::Never),
        Op::UserWrite(0, vec![], 2, false),
        Op::Publish(0),
    ]);
}

/// A user writes through another user's deletion of a record that main
/// has moved. Merged by key_id, the stack shows the record deleted, so the
/// write re-creates it, and main deleting its copy later is no conflict.
#[test]
fn write_through_a_lower_deletion_re_creates_the_record() {
    run(&[
        Op::NewUser,
        Op::NewUser,
        Op::UserWrite(73, vec![], 0, true),
        Op::DiskEdit(0, 3, true, FileWins::Never),
        Op::Write(0, 0, false),
        Op::Commit(0),
        Op::Move(0, 0, false),
        Op::UserWrite(20, vec![67], 0, false),
        Op::External(0, vec![(3, true)]),
        Op::Publish(68),
    ]);
}

/// A user's edit that another user's edit superseded follows its record to
/// another file, then can't follow it again at publishing and becomes a
/// new record. The other user's entry was about the old record, so the
/// stack shows the new one.
#[test]
fn renewed_edit_drops_entries_on_the_old_record() {
    run(&[
        Op::NewUser,
        Op::NewUser,
        Op::Write(0, 4, false),
        Op::UserWrite(110, vec![], 4, false),
        Op::External(0, vec![(1, true)]),
        Op::UserWrite(13, vec![152], 4, false),
        Op::DiskEdit(0, 1, false, FileWins::Never),
        Op::Move(0, 4, false),
        Op::Publish(104),
        Op::UserWrite(86, vec![], 4, false),
        Op::Rebuild(0, 177, 0, false),
        Op::Publish(44),
    ]);
}

/// A scan deletes main's head row in place and compaction drops the
/// tombstone below, leaving the head with no rows but the entry hiding the
/// old record. A fork at the head has to keep it.
#[test]
fn fork_keeps_a_head_with_only_entries() {
    run(&[
        Op::NewUser,
        Op::NewUser,
        Op::Rebuild(0, 104, 4, true),
        Op::NewUser,
        Op::External(0, vec![(4, false)]),
        Op::External(0, vec![(4, true)]),
        Op::Delete(29),
        Op::Fork(0, 0),
    ]);
}

/// A user's edit that can't follow its record at publishing becomes a new
/// record at a key main has nothing at. Whatever absence the user saw
/// there, an added record conflicts with none.
#[test]
fn renewed_edit_over_an_absence_is_no_conflict() {
    run(&[
        Op::Rebuild(0, 0, 0, false),
        Op::Write(0, 5, false),
        Op::External(0, vec![(2, false)]),
        Op::Move(0, 2, false),
        Op::NewUser,
        Op::UserWrite(0, vec![], 2, false),
        Op::Write(0, 5, true),
        Op::Fork(0, 1),
        Op::Rebuild(20, 129, 0, false),
        Op::UserWrite(0, vec![], 5, false),
        Op::Publish(0),
    ]);
}

/// A split restores a record at a key whose row below is another record
/// that moved to a key the split also restores. The row below's id
/// belongs to that key, not this one.
#[test]
fn split_leaves_the_row_belows_id_to_the_record_that_moved() {
    run(&[
        Op::Rebuild(0, 0, 2, false),
        Op::Rebuild(0, 0, 1, false),
        Op::NewUser,
        Op::Move(0, 1, false),
        Op::External(0, vec![(1, false)]),
        Op::External(0, vec![(1, true), (4, false)]),
        Op::Fork(0, 1),
        Op::Fork(1, 2),
        Op::Write(229, 4, false),
        Op::Rebuild(136, 15, 2, false),
    ]);
}

/// Main moves a record and deletes it with the other key's new record. A
/// split restoring both can't give the new record the moved one's id.
#[test]
fn split_gives_a_moved_records_old_row_id_to_the_record() {
    run(&[
        Op::Rebuild(0, 0, 2, false),
        Op::Rebuild(0, 0, 1, false),
        Op::NewUser,
        Op::Move(0, 1, false),
        Op::External(0, vec![(1, false)]),
        Op::External(0, vec![(1, true), (4, true)]),
        Op::Fork(0, 1),
        Op::Fork(1, 2),
        Op::Write(229, 4, false),
        Op::Rebuild(136, 15, 2, false),
    ]);
}

/// A user's deletion held back by the fold while another user's worktree is
/// deleted around it. Folding away the tombstone that still hides a row
/// would lose that user's conflict at publishing.
#[test]
fn fold_keeps_a_tombstone_that_still_hides_a_row() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 4, false),
        Op::Rebuild(0, 55, 0, false),
        Op::Publish(0),
        Op::NewUser,
        Op::Fork(0, 0),
        Op::Delete(8),
        Op::UserWrite(92, vec![], 4, true),
        Op::NewUser,
        Op::External(0, vec![(4, false)]),
        Op::Delete(16),
        Op::DiskEdit(0, 4, true, FileWins::Never),
        Op::Delete(13),
        Op::Commit(0),
        Op::Publish(0),
    ]);
}

/// A user writes through another user's draft, whose entry hides main's
/// tombstone at the key. With no edit of the other user there, what they
/// saw was main's absence, and the write supersedes that tombstone.
#[test]
fn write_supersedes_mains_tombstone_another_draft_hides() {
    run(&[
        Op::NewUser,
        Op::DiskEdit(0, 1, false, FileWins::Never),
        Op::UserWrite(0, vec![], 1, true),
        Op::NewUser,
        Op::Write(0, 1, false),
        Op::DiskEdit(0, 0, false, FileWins::Always),
        Op::UserWrite(1, vec![20], 1, false),
        Op::Write(0, 0, false),
        Op::Rebuild(0, 43, 0, false),
        Op::Publish(47),
    ]);
}
