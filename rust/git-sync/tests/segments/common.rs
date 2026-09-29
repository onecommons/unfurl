// Shared by the segment tests: the model's ids, keys and files, and
// git as the model has it.

type Key = u8;
type Ver = u64;
type RowId = u64;
type SegId = usize;
type Wt = usize;
type CommitId = usize;
/// Another draft's entry, carried to a new row: the content of the row it
/// was on, the draft's segment, and the `key_id` of the edit that made it.
type Entry = (Option<Ver>, SegId, Ver);

/// Where a scanned row's `key_id` can come from, besides the record at
/// its key or a new one.
#[derive(Default)]
struct KeyIds {
    /// The rollup of the commit that set its value.
    named: Option<Ver>,
    /// A record that left the other file in this scan, and its entries.
    moved: Option<(Ver, Vec<Entry>)>,
    /// Ids other keys of the new tree have: no fallback takes one.
    elsewhere: BTreeSet<Ver>,
}

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

/// Git: each commit's whole tree and parents, first parent first, and for
/// a commit git-sync made, the `key_id` of each record its rollup lists.
#[derive(Default)]
struct Git {
    commits: Vec<BTreeMap<Key, Ver>>,
    parents: Vec<Vec<CommitId>>,
    rollups: Vec<Option<BTreeMap<Key, Ver>>>,
    /// A rebase's replayed commits, each with the commit it copies: its
    /// message, and so its rollup.
    replayed_from: BTreeMap<CommitId, CommitId>,
}

impl Git {
    /// A commit made outside git-sync: no rollup.
    fn commit(&mut self, tree: BTreeMap<Key, Ver>, parents: Vec<CommitId>) -> CommitId {
        self.commits.push(tree);
        self.parents.push(parents);
        self.rollups.push(None);
        self.commits.len() - 1
    }

    fn commit_with_rollup(
        &mut self,
        tree: BTreeMap<Key, Ver>,
        ids: BTreeMap<Key, Ver>,
        parents: Vec<CommitId>,
    ) -> CommitId {
        self.commits.push(tree);
        self.parents.push(parents);
        self.rollups.push(Some(ids));
        self.commits.len() - 1
    }
}

/// The `key_id` `key` had at `c`, from the rollup of a commit that set
/// its value there: walking back through parents with the same value, the
/// first parent before the others, to the first such commit git-sync made.
/// `None` when it made none of them.
fn rollup_id(git: &Git, key: Key, c: CommitId) -> Option<Ver> {
    let v = git.commits[c].get(&key);
    let mut seen = BTreeSet::from([c]);
    let mut todo = vec![c];
    while let Some(at) = todo.pop() {
        let same: Vec<CommitId> = git.parents[at]
            .iter()
            .copied()
            .filter(|&p| git.commits[p].get(&key) == v)
            .collect();
        if same.is_empty() {
            if let Some(id) = git.rollups[at].as_ref().and_then(|r| r.get(&key)) {
                return Some(*id);
            }
        }
        todo.extend(same.into_iter().rev().filter(|&p| seen.insert(p)));
    }
    None
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

/// A new record's id when its version is already another record's id, as
/// a merge that brings an old version back can make it. The model's ids
/// are versions; a real row's id is fresh by construction, so this is the
/// harness's, not the design's.
fn fresh_id(ver: Ver, key: Key) -> Ver {
    2 * PRIVATE_VERSIONS + ver * KEYS as Ver + key as Ver
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
    /// Resolving a conflict for the file's side: this key, and no other.
    Only(Key),
}
