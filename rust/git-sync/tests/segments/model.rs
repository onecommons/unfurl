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
            // not when the record at its key is its own
            let here = self.committed.contains_key(&from).then(|| self.ids[&from]);
            if !d.origin.is_edit() || here == Some(d.id) {
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
