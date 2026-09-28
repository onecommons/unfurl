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
        let mut settled = prior.map_or_else(BTreeSet::new, |p| self.rows[&p].settled.clone());
        settled.extend(
            seen.iter()
                .map(|r| &self.rows[r])
                .filter(|r| r.seg != d && !r.deleted && r.id != record)
                .map(|r| r.id),
        );
        // and main's rows here with content seen here, which a draft below
        // may hide: another copy of what the edit was made over
        let here: BTreeSet<Ver> = seen
            .iter()
            .map(|r| &self.rows[r])
            .filter(|r| !r.deleted)
            .map(|r| r.ver)
            .collect();
        let copies: Vec<RowId> = self
            .visible(&self.own_view(MAIN), Some(key))
            .into_iter()
            .filter(|r| !self.rows[r].deleted && here.contains(&self.rows[r].ver))
            .collect();
        seen.extend(copies);
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
            // not when the record at its key is its own
            if self.rows[&x].edit.is_none() || self.live_id(&chain, from) == Some(id) {
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
        git: &Git,
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
        // Each added row's id, first from the rollup of the commit that set
        // its value, through every parent (§4.9), unless another key of the
        // new tree keeps that id
        let kept: BTreeSet<Ver> = (0..KEYS)
            .filter(|k| !changes.iter().any(|&(c, _)| c == *k))
            .filter_map(|k| self.live_id(&chain, k))
            .collect();
        let mut taken = kept.clone();
        let mut named: BTreeMap<Key, Ver> = BTreeMap::new();
        for &(key, change) in changes {
            if let Some(id) = change
                .and_then(|_| rollup_id(git, key, commit))
                .filter(|id| !taken.contains(id))
            {
                taken.insert(id);
                named.insert(key, id);
            }
        }
        // values some other commit already had at their keys: a merge
        // brings them back into main, re-creating rows users may have
        // edited over (§4.9)
        let history: BTreeSet<(Key, Ver)> = (0..git.commits.len())
            .filter(|&x| x != commit)
            .flat_map(|x| git.commits[x].iter().map(|(&k, &v)| (k, v)))
            .collect();
        // and the ids keys scanned before this one got
        let mut given: BTreeSet<Ver> = BTreeSet::new();
        for &(key, change) in changes {
            let ids = KeyIds {
                named: named.get(&key).copied(),
                moved: moved.remove(&key),
                elsewhere: named
                    .iter()
                    .filter(|&(&k, _)| k != key)
                    .map(|(_, &id)| id)
                    .chain(kept.iter().copied())
                    .chain(given.iter().copied())
                    .collect(),
            };
            self.scan_key(w, key, change, commit, ids);
            given.extend(self.live_id(&self.chain_set(w), key));
            if w == MAIN && change.is_some_and(|v| history.contains(&(key, v))) {
                if let Some(r) = self.row_in(self.wts[w].head, key) {
                    self.recreated(r);
                }
            }
        }
        let before = self.vacated.clone();
        self.follow_records(w);
        // and the files edits that followed their records here left
        let files: BTreeSet<Key> = changes
            .iter()
            .map(|&(k, _)| k)
            .chain(
                self.vacated
                    .difference(&before)
                    .filter(|&&(v, _)| v == w)
                    .map(|&(_, k)| k),
            )
            .map(file_of)
            .collect();
        for f in files {
            self.reconcile(w, f, disk, FileWins::Never);
        }
        let h = self.wts[w].head;
        self.segs[h].head_commit = Some(commit);
        self.wts[w].history.push(commit);
        self.relink_draft(w);
    }

    fn scan_key(&mut self, w: Wt, key: Key, change: Option<Ver>, commit: CommitId, ids: KeyIds) {
        let KeyIds {
            named,
            moved,
            elsewhere,
        } = ids;
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
                Some(v) => named
                    .or(live_record.filter(|id| !elsewhere.contains(id)))
                    .or(moved
                        .as_ref()
                        .map(|(id, _)| *id)
                        .filter(|id| !elsewhere.contains(id)))
                    .unwrap_or(if elsewhere.contains(&v) {
                        fresh_id(v, key)
                    } else {
                        v
                    }),
                None => record.unwrap(),
            };
            self.set_id(n, id);
            if let Some((_, entries)) = moved {
                // a new row for the record, like a split's
                self.recreated(n);
                self.carry(entries, n);
            }
            self.carry(entries, n);
            // a version a merge brings back into main, which users' drafts
            // layer over: drafts that superseded it at this key saw this
            // content, and elsewhere, edits of the record
            if change.is_some() && w == MAIN {
                let earlier = |here: bool| -> Vec<RowId> {
                    self.rows
                        .iter()
                        .filter(|&(&r, row)| {
                            r != n
                                && !row.deleted
                                && Some(row.ver) == change
                                && (row.key == key) == here
                                && (here || row.id == id)
                        })
                        .map(|(&r, _)| r)
                        .collect()
                };
                let (here, there) = (earlier(true), earlier(false));
                // here, those an edit at this key made, or one of the record
                let mut entries: Vec<Entry> = self
                    .draft_entries(&here, w)
                    .into_iter()
                    .filter(|&(_, seg, tag)| {
                        tag == id
                            || self
                                .rows_in(seg)
                                .iter()
                                .any(|y| self.rows[y].id == tag && self.rows[y].key == key)
                    })
                    .collect();
                entries.extend(
                    self.draft_entries(&there, w)
                        .into_iter()
                        .filter(|&(_, _, tag)| tag == id),
                );
                self.carry(entries, n);
                // and an edit made over this version, here or of the
                // record, though the row it saw is gone
                let own = self.wts[w].draft;
                let made_over: Vec<(SegId, Ver)> = self
                    .rows
                    .values()
                    .filter(|y| {
                        y.seg != own
                            && self.segs[y.seg].kind == Kind::Draft
                            && y.edit.is_some_and(|e| e.base == change)
                            && (y.key == key || y.id == id)
                    })
                    .map(|y| (y.seg, y.id))
                    .collect();
                for (seg, tag) in made_over {
                    self.entry(n, seg, tag);
                }
            }
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
        let head_commit = *self.wts[w].history.last().unwrap();
        let commit = git.commit_with_rollup(tree.clone(), BTreeMap::new(), vec![head_commit]);
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
                self.scan_key(w, k, file, commit, KeyIds::default());
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
        // and a changed key whose value at `c` the row below already holds,
        // unless another key has that id: then the row below is another
        // copy of the record, and the key is re-created with its own
        let mut collide: BTreeSet<Key> = BTreeSet::new();
        for (&j, &v) in tree_c.iter().filter(|(j, _)| keys.contains(j)) {
            let below_row = self
                .visible(&below, Some(j))
                .into_iter()
                .find(|r| !self.rows[r].deleted);
            if let Some(r) = below_row.filter(|r| self.rows[r].ver == v) {
                if !taken.insert(self.rows[&r].id) {
                    collide.insert(j);
                }
            }
        }
        // §4.7: the re-created keys' ids, strongest evidence first across
        // all of them, each id once: rollup, move, continuing record, row
        // below; a new id after. Ties in a tier go by key.
        let below_commit = self.segs[s].parent.and_then(|p| self.segs[p].head_commit);
        let recreate: Vec<Key> = keys
            .iter()
            .copied()
            .filter(|&k| {
                let below_live = self
                    .visible(&below, Some(k))
                    .into_iter()
                    .find(|r| !self.rows[r].deleted)
                    .map(|r| self.rows[&r].ver);
                self.row_in(s, k).is_none()
                    || tree_c.get(&k).copied() != below_live
                    || collide.contains(&k)
            })
            .collect();
        let tier = |t: u8, k: Key| -> Option<Ver> {
            let v_c = tree_c.get(&k).copied();
            match t {
                1 => rollup_id(git, k, c),
                2 => moved_id(k, v_c),
                3 => self.row_in(s, k).and_then(|sr| {
                    continuous(git, history, k, c, h_commit).then_some(self.rows[&sr].id)
                }),
                // the record at `c` only if the key held a record at every
                // commit from the row below's to `c`, and it didn't move to
                // another key at `c`
                _ => self
                    .visible(&below, Some(k))
                    .first()
                    .map(|b| &self.rows[b])
                    .filter(|b| {
                        !b.deleted
                            && !tree_c.iter().any(|(&j, &v)| j != k && v == b.ver)
                            && below_commit.is_none_or(|bc| continuous(git, history, k, bc, c))
                    })
                    .map(|b| b.id),
            }
        };
        let mut recovered: BTreeMap<Key, Ver> = BTreeMap::new();
        for t in 1..=4 {
            for &k in &recreate {
                if recovered.contains_key(&k) {
                    continue;
                }
                if let Some(id) = tier(t, k).filter(|id| !taken.contains(id)) {
                    taken.insert(id);
                    recovered.insert(k, id);
                }
            }
        }
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
                    if v_c != below_live || collide.contains(&k) {
                        let new = self.insert_value(s, k, v_c);
                        let fresh = Some(self.rows[&new].id).filter(|id| !taken.contains(id));
                        let id = recovered
                            .get(&k)
                            .copied()
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
                    let new = self.insert_value(s, k, v_c);
                    let fresh = Some(self.rows[&new].id).filter(|id| !taken.contains(id));
                    let id = recovered
                        .get(&k)
                        .copied()
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
                            let below_id = vis_below.first().map(|b| self.rows[b].id);
                            let id = rollup_id(git, k, h_commit)
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
