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
    /// A rewrite onto another worktree's line (a rebase onto its later
    /// commits): `.0`'s HEAD moves to a new commit on top of a segment of
    /// `.1`'s chain, `.2` from its root, head included.
    Rebase(u8, u8, u8, Key, bool),
    /// A commit made outside the database moving a record to the other
    /// file, edited too with `.2`, then scanned.
    Move(u8, Key, bool),
    /// Export a worktree's unresolved conflicts to a new branch (C.20).
    Export(u8),
    /// An export that fails after one of its steps, `.0 % 3`, from the
    /// worktree `.0 / 3` picks: its branch holds the edits uncommitted
    /// until the next export from that worktree finishes it.
    FailedExport(u8),
    /// A merge made outside the database into a worktree's head of other
    /// worktrees' commits (each a worktree and how far back), checked out
    /// and scanned. `.3` picks each conflicting key's side, and `.4` are
    /// changes the merge commit itself makes.
    Merge(u8, Vec<(u8, u8)>, MergeKind, u8, Vec<(Key, bool)>),
}

#[derive(Clone, Copy, Debug)]
enum MergeKind {
    /// One commit with a parent per source, an octopus with several, or a
    /// fast-forward when the head is an ancestor of the one source.
    Regular,
    /// The source's commits replayed on the head, keeping their messages
    /// and so their rollups.
    Rebase,
    /// One commit with the head as its only parent: no rollup.
    Squash,
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
        2 => (any::<u8>(), key.clone(), any::<bool>()).prop_map(|(w, k, e)| Op::Move(w, k, e)),
        2 => merge_op(key),
    ]
}

fn merge_op(key: std::ops::Range<Key>) -> impl Strategy<Value = Op> {
    let kind = prop_oneof![
        3 => Just(MergeKind::Regular),
        1 => Just(MergeKind::Rebase),
        1 => Just(MergeKind::Squash),
    ];
    (
        any::<u8>(),
        prop::collection::vec((any::<u8>(), 0u8..3), 1..3),
        kind,
        any::<u8>(),
        prop::collection::vec((key, prop::bool::weighted(0.2)), 0..2),
    )
        .prop_map(|(w, s, kind, picks, extra)| Op::Merge(w, s, kind, picks, extra))
}

/// [`op`] with rewrites onto other worktrees' lines, kept out of it so
/// the seeds saved under it replay the same histories. Weighted toward
/// what a rebase needs: forks, and commits on both lines.
fn rebase_op() -> impl Strategy<Value = Op> {
    prop_oneof![
        3 => op(),
        3 => (any::<u8>(), 0..KEYS, prop::bool::weighted(0.2)).prop_map(|(w, k, d)| Op::Write(w, k, d)),
        3 => any::<u8>().prop_map(Op::Commit),
        1 => (any::<u8>(), 0u8..4).prop_map(|(w, b)| Op::Fork(w, b)),
        2 => (any::<u8>(), any::<u8>(), any::<u8>(), 0..KEYS, prop::bool::weighted(0.2))
            .prop_map(|(w, u, b, k, d)| Op::Rebase(w, u, b, k, d)),
    ]
}

/// [`op`] with exports, kept out of it so the seeds saved under it replay
/// the same histories. Weighted toward what an export needs: edits, and
/// hand edits and outside commits that conflict with them, on bases of
/// different ages.
fn export_op() -> impl Strategy<Value = Op> {
    let key = 0..KEYS;
    prop_oneof![
        3 => op(),
        4 => (any::<u8>(), key.clone(), prop::bool::weighted(0.2)).prop_map(|(w, k, d)| Op::Write(w, k, d)),
        2 => any::<u8>().prop_map(Op::Commit),
        3 => (any::<u8>(), key.clone(), prop::bool::weighted(0.2)).prop_map(|(w, k, d)| Op::DiskEdit(w, k, d, FileWins::Never)),
        2 => (any::<u8>(), prop::collection::vec((key, prop::bool::weighted(0.2)), 1..3)).prop_map(|(w, c)| Op::External(w, c)),
        3 => any::<u8>().prop_map(Op::Export),
    ]
}

/// Exports, some failing partway and finished by a later one.
fn failed_export_op() -> impl Strategy<Value = Op> {
    prop_oneof![
        4 => export_op(),
        2 => any::<u8>().prop_map(Op::FailedExport),
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
        3 => (any::<u8>(), key.clone(), any::<bool>()).prop_map(|(w, k, e)| Op::Move(w, k, e)),
        2 => merge_op(key),
    ]
}

struct World {
    imp: Segments,
    model: Model,
    git: Git,
    next_ver: Ver,
    /// Each commit's records' `key_id`s, as the model has them.
    commit_ids: Vec<BTreeMap<Key, Ver>>,
    /// A worktree's own ids at a commit it reached, where they can differ
    /// from the recorded ones: a fast-forward to another's commit, say.
    own_ids: BTreeMap<(Wt, CommitId), BTreeMap<Key, Ver>>,
    /// For each commit and key, the commit whose rollup names the record
    /// there: the one that set its value, as the harness made it, not the
    /// implementation's walk.
    origins: Vec<BTreeMap<Key, CommitId>>,
    /// Each worktree's unfinished export, by worktree.
    stuck: BTreeMap<Wt, Unfinished>,
}

/// An export that failed after a step: its edits in the branch's draft,
/// or folded into a commit its ref isn't at yet.
enum Unfinished {
    Moved(Stuck),
    Folded,
}

/// An export stopped after its move: branch `f`, forked at `base`, holds
/// the edits in its draft, and committed holds `tree` with `ids`.
struct Stuck {
    f: Wt,
    base: CommitId,
    tree: BTreeMap<Key, Ver>,
    ids: BTreeMap<Key, Ver>,
}

impl World {
    fn new() -> Self {
        let mut git = Git::default();
        let mut imp = Segments::default();
        let tree: BTreeMap<Key, Ver> = (0..KEYS).map(|k| (k, k as Ver + 1)).collect();
        let c0 = git.commit(tree.clone(), Vec::new());
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
            own_ids: BTreeMap::new(),
            origins: Vec::new(),
            stuck: BTreeMap::new(),
        }
    }

    /// Record the model's ids for the commit just made.
    /// `w`'s ids at commit `c`: its own where it reached `c`, else those
    /// recorded for `c`.
    fn ids_at(&self, w: Wt, c: CommitId) -> BTreeMap<Key, Ver> {
        self.own_ids
            .get(&(w, c))
            .cloned()
            .unwrap_or_else(|| self.commit_ids[c].clone())
    }

    fn committed(&mut self, w: Wt) {
        let c = self.commit_ids.len();
        self.own_ids.insert((w, c), self.model.wts[w].ids.clone());
        self.commit_ids.push(self.model.wts[w].ids.clone());
        assert_eq!(self.commit_ids.len(), self.git.commits.len());
    }

    /// A commit made outside the database with these changes, checked out
    /// in `w` and scanned. A record that left one file as it arrived in the
    /// other moved, keeping its `key_id`.
    fn external(&mut self, w: Wt, changes: BTreeMap<Key, Option<Ver>>) {
        let before = self.model.wts[w].committed.clone();
        // a scan takes in only what the commit changed
        let changes: BTreeMap<Key, Option<Ver>> = changes
            .into_iter()
            .filter(|(k, v)| before.get(k) != v.as_ref())
            .collect();
        let m = &mut self.model.wts[w];
        let old_ids = m.ids.clone();
        // no two keys share an id: those the unchanged keys keep, and those
        // given earlier in the change, are passed over
        let mut taken: BTreeSet<Ver> = old_ids
            .iter()
            .filter(|(k, _)| !changes.contains_key(k))
            .map(|(_, &id)| id)
            .collect();
        for (&k, &change) in &changes {
            if let Some(v) = change {
                let from = other_file(k);
                let continuing = before.contains_key(&k).then(|| old_ids[&k]);
                let moved = (!before.contains_key(&k)
                    && before.contains_key(&from)
                    && changes.get(&from) == Some(&None))
                .then(|| old_ids[&from]);
                // else the record the draft holds at the key
                let drafted = m.draft.get(&k).map(|d| d.id);
                let id = continuing
                    .filter(|id| !taken.contains(id))
                    .or(moved.filter(|id| !taken.contains(id)))
                    .or(drafted.filter(|id| !taken.contains(id)))
                    .unwrap_or(if taken.contains(&v) {
                        fresh_id(v, k)
                    } else {
                        v
                    });
                taken.insert(id);
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
        let held: BTreeSet<Key> = m.draft.keys().copied().collect();
        m.follow_records();
        // and the files edits that followed their records left
        let files: BTreeSet<Key> = changes
            .keys()
            .copied()
            .chain(held.into_iter().filter(|k| !m.draft.contains_key(k)))
            .map(file_of)
            .collect();
        for &f in &files {
            m.reconcile(f, &mut self.next_ver, FileWins::Never);
        }
        let head = *self.imp.wts[w].history.last().unwrap();
        let c = self.git.commit(m.committed.clone(), vec![head]);
        let disk = m.disk.clone();
        let changes: Vec<(Key, Option<Ver>)> = changes.into_iter().collect();
        self.imp.scan(w, &changes, c, &disk, &self.git);
        self.committed(w);
        if w == MAIN {
            self.main_moved(&before, &BTreeMap::new());
        }
    }

    fn ancestors(&self, c: CommitId) -> BTreeSet<CommitId> {
        let mut out = BTreeSet::from([c]);
        let mut todo = vec![c];
        while let Some(x) = todo.pop() {
            for &p in &self.git.parents[x] {
                if out.insert(p) {
                    todo.push(p);
                }
            }
        }
        out
    }

    /// The common ancestor of `a` and `b` made last; none for unrelated
    /// histories, which merge against an empty tree.
    fn merge_base(&self, a: CommitId, b: CommitId) -> Option<CommitId> {
        let (x, y) = (self.ancestors(a), self.ancestors(b));
        x.intersection(&y).max().copied()
    }

    /// Replay the source's first-parent commits since its merge base with
    /// `head` on top of it, each keeping its message and so its rollup.
    fn rebase_commits(&mut self, head: CommitId, src: CommitId) -> Vec<(CommitId, CommitId)> {
        let below = self
            .merge_base(head, src)
            .map_or_else(BTreeSet::new, |b| self.ancestors(b));
        let mut replay = Vec::new();
        let mut at = src;
        while !below.contains(&at) {
            replay.push(at);
            match self.git.parents[at].first() {
                Some(&p) => at = p,
                None => break,
            }
        }
        replay.reverse();
        let mut tree = self.git.commits[head].clone();
        let mut prev = head;
        let mut new = Vec::new();
        for ci in replay {
            let parent = self.git.parents[ci]
                .first()
                .map(|&p| self.git.commits[p].clone())
                .unwrap_or_default();
            for k in 0..KEYS {
                let v = self.git.commits[ci].get(&k).copied();
                if v != parent.get(&k).copied() {
                    match v {
                        Some(v) => tree.insert(k, v),
                        None => tree.remove(&k),
                    };
                }
            }
            prev = match self.git.rollups[ci].clone() {
                Some(ids) => self.git.commit_with_rollup(tree.clone(), ids, vec![prev]),
                None => self.git.commit(tree.clone(), vec![prev]),
            };
            self.git.replayed_from.insert(prev, ci);
            new.push((prev, ci));
        }
        new
    }

    /// A merge commit of `srcs` into `head`: three-way per key against each
    /// source's merge base, `picks` choosing the side where both changed a
    /// key, then `extra`, the merge commit's own changes.
    fn merge_commit(
        &mut self,
        head: CommitId,
        srcs: &[CommitId],
        squash: bool,
        picks: u8,
        extra: &[(Key, bool)],
    ) -> CommitId {
        let mut tree = self.git.commits[head].clone();
        // where each value comes from: the side the merge took it from
        let this = self.git.commits.len();
        let mut origin = self.origins[head].clone();
        for (i, &src) in srcs.iter().enumerate() {
            let base = self
                .merge_base(head, src)
                .map_or_else(BTreeMap::new, |b| self.git.commits[b].clone());
            let theirs = self.git.commits[src].clone();
            for k in 0..KEYS {
                let (b, o, t) = (base.get(&k), tree.get(&k), theirs.get(&k));
                let take = if t == b || o == t {
                    false
                } else if o == b {
                    true
                } else {
                    picks >> ((i * KEYS as usize + k as usize) % 8) & 1 == 1
                };
                if take {
                    match t {
                        Some(&v) => {
                            tree.insert(k, v);
                            origin.insert(k, self.origins[src][&k]);
                        }
                        None => {
                            tree.remove(&k);
                            origin.remove(&k);
                        }
                    };
                }
            }
        }
        for &(k, deleted) in extra {
            if deleted {
                tree.remove(&k);
                origin.remove(&k);
            } else {
                let v = self.ver();
                tree.insert(k, v);
                origin.insert(k, this);
            }
        }
        if squash {
            // one parent, no rollup: the values it brings are its own
            return self.git.commit(tree, vec![head]);
        }
        let parents = std::iter::once(head).chain(srcs.iter().copied()).collect();
        let c = self.git.commit(tree, parents);
        assert_eq!(self.origins.len(), c);
        self.origins.push(origin);
        c
    }

    /// A merge made outside the database into `w`'s head, checked out and
    /// scanned.
    fn merge(
        &mut self,
        w: Wt,
        srcs: Vec<CommitId>,
        kind: MergeKind,
        picks: u8,
        extra: &[(Key, bool)],
    ) {
        let head = *self.imp.wts[w].history.last().unwrap();
        let mine = self.ancestors(head);
        // each source once: git has no duplicate parents
        let mut seen = BTreeSet::new();
        let srcs: Vec<CommitId> = srcs
            .into_iter()
            .filter(|c| !mine.contains(c) && seen.insert(*c))
            .collect();
        if srcs.is_empty() {
            return;
        }
        // a replayed commit's records are its source commit's
        let mut replayed: BTreeMap<CommitId, CommitId> = BTreeMap::new();
        let new: Vec<CommitId> = match kind {
            MergeKind::Rebase => {
                let pairs = self.rebase_commits(head, srcs[0]);
                replayed.extend(pairs.iter().copied());
                pairs.into_iter().map(|(c, _)| c).collect()
            }
            MergeKind::Regular
                if srcs.len() == 1
                    && extra.is_empty()
                    && self.ancestors(srcs[0]).contains(&head) =>
            {
                // a fast-forward: no commit of its own
                vec![srcs[0]]
            }
            MergeKind::Regular => vec![self.merge_commit(head, &srcs, false, picks, extra)],
            MergeKind::Squash => vec![self.merge_commit(head, &srcs, true, picks, extra)],
        };
        let Some(&top) = new.last() else { return };
        self.fill_origins();
        let old_ids = self.model.wts[w].ids.clone();
        // the commits before the top, for later lookups through them
        let made: Vec<CommitId> = new
            .iter()
            .copied()
            .filter(|&c| c >= self.commit_ids.len() && c != top)
            .collect();
        for c in made {
            let ids = self.merged_ids(c, head, &old_ids, &replayed);
            let ids = self.rollup_named(c, ids, &replayed);
            self.commit_ids.push(ids);
        }
        let before = self.model.wts[w].committed.clone();
        let tree = self.git.commits[top].clone();
        let mut ids = self.merged_ids(top, head, &old_ids, &replayed);
        let changes: BTreeMap<Key, Option<Ver>> = (0..KEYS)
            .filter(|k| before.get(k) != tree.get(k))
            .map(|k| (k, tree.get(&k).copied()))
            .collect();
        let m = &mut self.model.wts[w];
        for (&k, &change) in &changes {
            match change {
                Some(v) => m.disk.insert(k, v),
                None => m.disk.remove(&k),
            };
        }
        let disk = m.disk.clone();
        let scanned: Vec<(Key, Option<Ver>)> = changes.iter().map(|(&k, &c)| (k, c)).collect();
        self.imp.scan(w, &scanned, top, &disk, &self.git);
        // No rollup names these: the scan takes them from the rows it has,
        // which can miss a record's identity (§4.9). Adopt its choice.
        let chain = self.imp.chain_set(w);
        for &k in tree.keys() {
            if let std::collections::btree_map::Entry::Vacant(slot) = ids.entry(k) {
                if let Some(id) = self.imp.live_id(&chain, k) {
                    slot.insert(id);
                }
            }
        }
        if top >= self.commit_ids.len() {
            let named = self.rollup_named(top, ids.clone(), &replayed);
            self.commit_ids.push(named);
        }
        self.own_ids.insert((w, top), ids.clone());
        let m = &mut self.model.wts[w];
        m.committed = tree;
        m.ids = ids;
        let held: BTreeSet<Key> = m.draft.keys().copied().collect();
        m.follow_records();
        // and the files edits that followed their records left
        let files: BTreeSet<Key> = changes
            .keys()
            .copied()
            .chain(held.into_iter().filter(|k| !m.draft.contains_key(k)))
            .map(file_of)
            .collect();
        for &f in &files {
            m.reconcile(f, &mut self.next_ver, FileWins::Never);
        }
        if w == MAIN {
            self.main_moved(&before, &BTreeMap::new());
        }
    }

    /// A replayed commit's rollup names the source's records, whatever the
    /// rebasing worktree had to give them: those are its recorded ids.
    fn rollup_named(
        &self,
        c: CommitId,
        mut ids: BTreeMap<Key, Ver>,
        replayed: &BTreeMap<CommitId, CommitId>,
    ) -> BTreeMap<Key, Ver> {
        if let (Some(&src), Some(listed)) = (replayed.get(&c), &self.git.rollups[c]) {
            for k in listed.keys() {
                if let Some(&id) = self.commit_ids[src].get(k) {
                    ids.insert(*k, id);
                }
            }
        }
        ids
    }

    /// The reference's `key_id`s for commit `c`, scanned from `from`: the
    /// ids `from` keeps at keys whose value didn't change, `old_ids` being
    /// the merging worktree's, and at a changed key the id of the record
    /// in the commit that set its value, when git-sync made it and no kept
    /// key has it. Other keys are left out: nothing names their record. A
    /// commit `replayed` maps is a rebase's copy of another, whose ids are
    /// the source's.
    fn merged_ids(
        &self,
        c: CommitId,
        from: CommitId,
        old_ids: &BTreeMap<Key, Ver>,
        replayed: &BTreeMap<CommitId, CommitId>,
    ) -> BTreeMap<Key, Ver> {
        let (tree, old) = (&self.git.commits[c], &self.git.commits[from]);
        let mut ids: BTreeMap<Key, Ver> = tree
            .keys()
            .filter(|&k| old.get(k) == tree.get(k))
            .filter_map(|k| old_ids.get(k).map(|&id| (*k, id)))
            .collect();
        let mut taken: BTreeSet<Ver> = ids.values().copied().collect();
        for &k in tree.keys().filter(|&k| old.get(k) != tree.get(k)) {
            let setter = self.origins[c][&k];
            if self.git.rollups[setter].is_none() {
                continue;
            }
            let setter = replayed.get(&setter).copied().unwrap_or(setter);
            if let Some(&id) = self.commit_ids.get(setter).and_then(|m| m.get(&k)) {
                if taken.insert(id) {
                    ids.insert(k, id);
                }
            }
        }
        ids
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
            .filter(|&w| {
                let m = &self.model.wts[w];
                m.alive && m.user == user && !m.exported
            })
            .collect();
        (!alive.is_empty()).then(|| alive[n as usize % alive.len()])
    }

    /// A rewrite: `w`'s HEAD moves to a new commit on top of `base`, a
    /// worktree's chain up to an index, or none (a new root), with `k`
    /// changed; then scanned.
    fn rebuild(&mut self, w: Wt, base: Option<(Wt, usize)>, k: Key, deleted: bool) {
        // onto a line that already holds w's HEAD it's a fast-forward and a
        // commit, not a rewrite: a base ending at HEAD, below an empty head,
        // say
        if let Some((u, i)) = base {
            let seg = self.imp.wts[u].chain[i].0;
            let bc = self.imp.segs[seg].head_commit.unwrap();
            let head = *self.imp.wts[w].history.last().unwrap();
            let upto = self.imp.wts[u].history.iter().position(|&x| x == bc).unwrap();
            if self.imp.wts[u].history[..=upto].contains(&head) {
                return;
            }
        }
        let base_commit = base.map(|(u, i)| {
            let seg = self.imp.wts[u].chain[i].0;
            self.imp.segs[seg].head_commit.unwrap()
        });
        let mut tree =
            base_commit.map_or_else(BTreeMap::new, |c| self.git.commits[c].clone());
        let mut base_ids = base
            .zip(base_commit)
            .map_or_else(BTreeMap::new, |((u, _), c)| self.ids_at(u, c));
        // Where no rollup names a record's id at the base, the rows
        // there decide it, which can lose one (§4.8): adopt them.
        if let (Some((u, i)), Some(bc)) = (base, base_commit) {
            let base_view: BTreeSet<SegId> = self.imp.wts[u].chain[..=i]
                .iter()
                .map(|&(s, _)| s)
                .collect();
            for (&k, id) in base_ids.iter_mut() {
                let named = self.named_id(k, bc);
                let lost = named.is_some_and(|n| {
                    n != *id
                        || (0..KEYS)
                            .any(|j| j != k && self.imp.live_id(&base_view, j) == Some(n))
                });
                if named.is_none() || lost {
                    if let Some(got) = self.imp.live_id(&base_view, k) {
                        allowed(7, usize::from(got != *id));
                        *id = got;
                    }
                }
            }
        }
        if deleted {
            tree.remove(&k);
        } else {
            let v = self.ver();
            tree.insert(k, v);
        }
        let c = self
            .git
            .commit(tree.clone(), base_commit.into_iter().collect());
        self.imp.rebuild(w, base, &tree, c);
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
                    // else the record the draft holds at the key
                    .or(m.draft.get(&k).map(|d| d.id).filter(|id| !taken.contains(id)))
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

    fn apply(&mut self, op: &Op) {
        self.fill_origins();
        self.apply_op(op);
        self.fill_origins();
    }

    /// Origins for commits made since, with one parent or none: a value
    /// the parent had comes from its origin, else from the commit. A merge
    /// commit's are set when the harness makes it.
    fn fill_origins(&mut self) {
        for c in self.origins.len()..self.git.commits.len() {
            let tree = &self.git.commits[c];
            let parent = self.git.parents[c].first().copied();
            let origin = tree
                .iter()
                .map(|(&k, v)| {
                    let from = parent
                        .filter(|&p| self.git.commits[p].get(&k) == Some(v))
                        .map_or(c, |p| self.origins[p][&k]);
                    (k, from)
                })
                .collect();
            self.origins.push(origin);
        }
    }

    /// The id the rollup of the commit that set `k`'s value at `c` names.
    fn named_id(&self, k: Key, c: CommitId) -> Option<Ver> {
        let origin = *self.origins[c].get(&k)?;
        self.git.rollups[origin].as_ref()?.get(&k).copied()
    }

    /// The model's ids for `f`, just forked from `w` at `w`'s
    /// `history[pos]`.
    fn forked_ids(&mut self, w: Wt, pos: usize, f: Wt) -> BTreeMap<Key, Ver> {
        let len = self.imp.wts[w].history.len();
        let c = self.imp.wts[w].history[pos];
        // at the head, the worktree's own ids: another worktree may
        // have had to give the same commit's records others
        let ids = if pos == len - 1 {
            self.model.wts[w].ids.clone()
        } else {
            let mut ids = self.ids_at(w, c);
            // Where no rollup names a record's id at `c`, the split
            // falls back on the ids it has, which can miss a record
            // deleted and re-created after `c` outside git-sync.
            // So does a key whose rollup names an id the worktree
            // didn't give it at `c`, or another key took first (§4.7).
            let chain = self.imp.chain_set(f);
            for (k, id) in ids.iter_mut() {
                let named = self.named_id(*k, c);
                let lost = named.is_some_and(|n| {
                    n != *id
                        || (0..KEYS).any(|j| j != *k && self.imp.live_id(&chain, j) == Some(n))
                });
                if named.is_none() || lost {
                    if let Some(got) = self.imp.live_id(&chain, *k) {
                        allowed(6, usize::from(got != *id));
                        *id = got;
                    }
                }
            }
            // the worktree forked from shares the split segment
            self.own_ids.insert((w, c), ids.clone());
            ids
        };
        self.own_ids.insert((f, c), ids.clone());
        ids
    }

    /// C.20: `w`'s edits under an unresolved conflict go to a new branch
    /// forked at their oldest base and committed there; `w` takes in the
    /// file's values, as resolving each for the file would. With `stop`,
    /// it fails after that step: the move (0) or writing the commit (1),
    /// leaving the edits in the branch's draft, or the fold (2), leaving
    /// only its ref to move; the last two only where it makes a commit.
    /// The next export from `w` finishes it, and does nothing else.
    fn export(&mut self, n: u8, stop: Option<u8>) {
        let w = self.pick(n, false).unwrap();
        match self.stuck.remove(&w) {
            Some(Unfinished::Moved(stuck)) => return self.finish_export(stuck),
            Some(Unfinished::Folded) => return,
            None => {}
        }
        let Some(stuck) = self.move_export(w) else {
            return;
        };
        let commits = stuck.tree != self.git.commits[stuck.base];
        match (stop, commits) {
            (Some(0), _) | (Some(1), true) => {
                self.stuck.insert(w, Unfinished::Moved(stuck));
            }
            (Some(2), true) => {
                self.finish_export(stuck);
                self.stuck.insert(w, Unfinished::Folded);
            }
            _ => self.finish_export(stuck),
        }
    }

    /// The export's move: a branch at the edits' base whose draft holds
    /// them, and `w` resolving each for the file.
    fn move_export(&mut self, w: Wt) -> Option<Stuck> {
        let Some((f, pos, rows)) = self.imp.export_fork(w, &self.git) else {
            assert!(
                self.model.wts[w].conflicts.values().all(|c| c.resolved),
                "worktree {w} exported nothing, with unresolved conflicts"
            );
            return None;
        };
        assert_eq!(f, self.model.wts.len(), "the branch's index");
        let at_base = self.forked_ids(w, pos, f);
        let base = self.imp.wts[w].history[pos];
        let moved: BTreeSet<Key> = rows.iter().map(|r| self.imp.rows[r].key).collect();
        let disk = self.model.wts[w].disk.clone();
        self.imp.export_move(w, f, &rows, &disk);
        let m = &self.model.wts[w];
        let exported: BTreeMap<Key, ODraft> = m
            .conflicts
            .iter()
            .filter(|(_, c)| !c.resolved)
            .map(|(&k, _)| (k, m.draft[&k].clone()))
            .collect();
        assert_eq!(
            moved,
            exported.keys().copied().collect(),
            "worktree {w}'s exported keys"
        );
        let (mut tree, mut ids) = (self.git.commits[base].clone(), at_base.clone());
        let mut draft = BTreeMap::new();
        for (&k, d) in &exported {
            // a record the base has at another key: the edit is a new one
            let id = match at_base.iter().any(|(&j, &id)| j != k && id == d.id) {
                false => d.id,
                true if d.ver == d.id => fresh_id(d.ver, k),
                true => d.ver,
            };
            if d.deleted {
                tree.remove(&k);
                ids.remove(&k);
            } else {
                tree.insert(k, d.ver);
                ids.insert(k, id);
            }
            let edit = ODraft {
                id,
                based_on: BTreeSet::new(),
                ..d.clone()
            };
            draft.insert(k, edit);
        }
        for &k in exported.keys() {
            let m = &mut self.model.wts[w];
            m.conflicts.remove(&k);
            m.reconcile(file_of(k), &mut self.next_ver, FileWins::Only(k));
        }
        let mut branch = OWt::new(self.git.commits[base].clone(), at_base, false);
        branch.draft = draft;
        branch.exported = true;
        self.model.wts.push(branch);
        Some(Stuck { f, base, tree, ids })
    }

    /// The export's commit onto its base, and its fold.
    fn finish_export(&mut self, stuck: Stuck) {
        let Stuck { f, tree, ids, .. } = stuck;
        let made = self.git.commits.len();
        let commit = self.imp.export_commit(f, &mut self.git);
        assert_eq!(self.git.commits[commit], tree, "the exported branch's tree");
        let mut branch = OWt::new(tree, ids, false);
        branch.exported = true;
        self.model.wts[f] = branch;
        if self.git.commits.len() > made {
            self.committed(f);
        }
    }

    fn apply_op(&mut self, op: &Op) {
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
            Op::Merge(n, ref sources, kind, picks, ref extra) => {
                let w = self.pick(n, false).unwrap();
                let srcs: Vec<CommitId> = sources
                    .iter()
                    .filter_map(|&(s, back)| {
                        let s = self.pick(s, false).filter(|&s| s != w)?;
                        let h = &self.imp.wts[s].history;
                        Some(h[h.len() - 1 - (back as usize).min(h.len() - 1)])
                    })
                    .collect();
                self.merge(w, srcs, kind, picks, extra);
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
                let before = m.disk.get(&k).copied();
                if deleted {
                    m.disk.remove(&k);
                } else {
                    m.disk.insert(k, ver);
                }
                // `force` is over every file, not the one edited; otherwise
                // a scan skips a file whose content is what it last took in
                let files = match wins {
                    FileWins::Always => (0..KEYS / KEYS_PER_FILE).collect(),
                    _ if before == m.disk.get(&k).copied() => Vec::new(),
                    _ => vec![file_of(k)],
                };
                for f in files {
                    let m = &mut self.model.wts[w];
                    m.reconcile(f, &mut self.next_ver, wins);
                    let disk = m.disk.clone();
                    self.imp.reconcile(w, f, &disk, wins);
                }
            }
            Op::Fork(n, back) => {
                let w = self.pick(n, false).unwrap();
                let len = self.imp.wts[w].history.len();
                let pos = len - 1 - (back as usize).min(len - 1);
                let c = self.imp.wts[w].history[pos];
                let f = self.imp.fork(w, pos, &self.git);
                let ids = self.forked_ids(w, pos, f);
                self.model
                    .wts
                    .push(OWt::new(self.git.commits[c].clone(), ids, false));
            }
            Op::Export(n) => self.export(n, None),
            Op::FailedExport(n) => self.export(n / 3, Some(n % 3)),
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
                // A record an edit settled only in a re-created row, which
                // the edit's draft superseded: its entry on the row it saw
                // went with that row (§4.7), so the model can't tell. Adopt
                // it as settled.
                let d = self.imp.wts[u].draft;
                let main_chain = &self.imp.chain_set(MAIN);
                let adopted: Vec<(Key, Ver)> = self
                    .imp
                    .rows_in(d)
                    .into_iter()
                    .flat_map(|x| {
                        let key = self.imp.rows[&x].key;
                        let imp = &self.imp;
                        imp.rows[&x]
                            .settled
                            .iter()
                            .copied()
                            .filter(move |&r| {
                                imp.visible(main_chain, Some(key)).iter().any(|y| {
                                    let row = &imp.rows[y];
                                    row.recreated && row.id == r && imp.sup.contains(&(*y, d))
                                })
                            })
                            .map(move |r| (key, r))
                            .collect::<Vec<_>>()
                    })
                    .collect();
                allowed(5, adopted.len());
                for (k, r) in adopted {
                    if let Some(e) = self.model.wts[u].draft.get_mut(&k) {
                        e.settled.insert(r);
                    }
                }
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
                    if user {
                        m.draft.remove(&k);
                    } else {
                        m.reconcile(file_of(k), &mut self.next_ver, FileWins::Only(k));
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
                self.rebuild(w, base_idx.map(|i| (w, i)), k, deleted);
            }
            Op::Rebase(n, other, back, k, deleted) => {
                let w = self.pick(n, false).unwrap();
                let others: Vec<Wt> = (0..self.model.wts.len())
                    .filter(|&u| {
                        let m = &self.model.wts[u];
                        u != w && m.alive && !m.user && !m.exported
                    })
                    .collect();
                if others.is_empty() {
                    return;
                }
                let u = others[other as usize % others.len()];
                let i = back as usize % self.imp.wts[u].chain.len();
                self.rebuild(w, Some((u, i)), k, deleted);
            }
            Op::Delete(n) => {
                // an exported branch has no handle to delete it through
                let candidates: Vec<Wt> = (1..self.model.wts.len())
                    .filter(|&w| self.model.wts[w].alive && !self.model.wts[w].exported)
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
                let which = [over_report, seen_copy, deletion_over_report];
                if let Some(i) = which.iter().position(|&b| b) {
                    allowed(i, 1);
                }
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
                allowed(3, expected.difference(&layered).count() - lost.len());
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
                    allowed(4, 1);
                    assert!(
                        (recreated && edited_over) || other_file_copy,
                        "step {step} ({op:?}): main + {stack:?} shows ({k}, {v}), which no edit in the stack was made over\n  got: {layered:?}\n want: {expected:?}"
                    );
                }
            }
        }
    }
}

/// Where the checker allowed a difference from the reference, or the
/// reference adopted the implementation's choice. With
/// `SEGMENTS_ALLOWANCES` naming a file, `run` writes how often each
/// happened there every 10000 cases, so runs of one seed can be compared.
/// Per thread, so a scripted test counts only its own.
const ALLOWANCES: [&str; 8] = [
    "over-report",
    "seen-copy",
    "deletion-over-report",
    "lost-copy",
    "extra-copy",
    "settled-adopted",
    "fork-adopted",
    "rebuild-adopted",
];
thread_local! {
    static ALLOWED: [Cell<u64>; ALLOWANCES.len()] = const { [const { Cell::new(0) }; ALLOWANCES.len()] };
    static CASES: Cell<u64> = const { Cell::new(0) };
}

fn allowed(allowance: usize, n: usize) {
    ALLOWED.with(|a| a[allowance].set(a[allowance].get() + n as u64));
}

/// Count a finished case; with `SEGMENTS_ALLOWANCES` set, write the
/// counts every 10000.
fn case_done() {
    let cases = CASES.with(|n| {
        n.set(n.get() + 1);
        n.get()
    });
    if let Some(path) = std::env::var_os("SEGMENTS_ALLOWANCES").filter(|_| cases.is_multiple_of(10000)) {
        let counts: Vec<String> = ALLOWED.with(|a| {
            ALLOWANCES
                .iter()
                .zip(a)
                .map(|(name, n)| format!("{name} {}\n", n.get()))
                .collect()
        });
        std::fs::write(path, format!("cases {cases}\n{}", counts.concat())).unwrap();
    }
}

/// Like `run`, but nothing may be allowed: for a rule whose absence shows
/// only as an allowed over-report or copy.
fn run_exact(ops: &[Op]) {
    let total = || ALLOWED.with(|a| a.iter().map(Cell::get).sum::<u64>());
    let before = total();
    run(ops);
    assert_eq!(total(), before, "the checker allowed a difference");
}
