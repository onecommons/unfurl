# Implementing segments

The plan for building [the segment design](branch-segments.md) into
git-sync, for review before any code. It covers what's reused, the API,
every behaviour change, and the phases with their gates. The design
itself isn't restated.

## 1. What shapes the plan

- **Nothing is released,** so there's no migration
  ([§7](branch-segments.md#7-starting-from-scratch)). The new schema
  replaces the migrations, and databases are rebuilt by scanning.
- **The server is the only Rust consumer,** through the public API; it
  has no SQL of its own. Python reaches it over HTTP. The wire contract
  carries row ids: `unfurl.server.id` is `Record::id`, `exclude` takes
  row ids, and Python's proxy stores both
  (`server/src/cloudmap.rs:1363-1405`, `:756`;
  `unfurl/cloudmap/proxy.py:88`).
- **One handle, one worktree.** `SyncedRepoInner.worktree_id` is fixed at
  `open`. There's no fork, delete, user branch or layered view today.
  Nothing checks ancestry: a scan re-parses whatever bytes differ.
- **Git is real in tests.** `git.rs` is free functions over
  `gix::Repository`, with no seam. Tests build temporary repos with gix
  and the git CLI (`tests/common/mod.rs`).
- **The backend matrix exists:** `crud_test!` runs each test on SQLite,
  and on Postgres when `UNFURL_TEST_PG_URL` is set. CI sets it.
- **The types cache is already gone** (`3e7329ab`); §6.11 is done.

## 2. Testing: SQL against the in-memory implementation

The model test checks the in-memory `Segments` against a reference with
a few allowed over-reports. The SQL implementation is checked against
the in-memory one, step by step, for **exact** agreement. So the
allowances stay a question about the design, and SQL needs no internals
exposed: no `recreated` flag, no entries.

**What's compared,** after every step, and nothing else:
- each worktree's view: key → (content, `key_id`);
- its committed chain, the same way;
- its conflicts: key → (theirs, resolved);
- each layered stack's rows, from phase 3;
- the tree of every commit the implementation makes.

**`key_id`s are compared through a bijection** kept across the run, not
by value. The in-memory ids are versions; SQL's come from a sequence.

**Ties are part of the contract.** The in-memory code breaks ties by key
order: split tiers, the ids a split keeps first, and the scan's order
(rollup, continuing, pairing, fresh). SQL breaks them by
`(file_path, path, key)` in `COLLATE "C"`. The harness maps model key
*k* to file `f{k / 3}.yaml`, path `/r`, key `k{k % 3}`, so the two
orders are the same. A move pairs *k* with *k* ± 3, the same
`(path, key)` in the other file, as §3.5 pairs them.

**Content is `{"v": N}`.** Every write draws a fresh version, so equal
content means equal version; a version a merge brings back is identical
JSON on both sides.

**A test format.** The scan runs `formats.detect` and validation. The
harness registers a minimal `DataFormat`: a `kind` header, one map
section `r`, no validation, no aliases.

**Real git.** The harness builds External, Move, Merge (regular,
octopus, rebase keeping messages, squash, unrelated) and Rebuild commits
in a temporary repo, mirroring the model's `Git`, with the git CLI as
the tests do now. Forks get their own working directory
(`git worktree add`); user branches have none.

**The committed chain** is read by the harness with SQL over
`worktree_segment`, leaving out the draft. Views, conflicts and stacks
go through the public API.

**Layout.** The model test is split into files under
`tests/segments/`: `common.rs`, `imp.rs`, `model.rs`, `world.rs`,
`histories.rs`, and `git_mirror.rs` with the test format. The entry
points `segments_model.rs` and `segments_sql.rs` `include!` them, so
they share one scope as before, and each defines its own `run`, which
the scripted histories call.

**Cost.** A step is real git plus SQL, milliseconds rather than
microseconds. `cargo test` runs the scripted histories on both backends
through the `crud_test!` matrix. Random runs are `#[ignore]`, sized by
`PROPTEST_CASES`, and run by hand before merging a phase.

## 3. API

### Unchanged in signature, changed in meaning (phase 1)

- **`Record::id` is the `key_id`,** stable across edits and shared by
  every view. `WriteOutcome::id` likewise. `get_record_by_id` resolves a
  `key_id` within the handle's view. `find_records_follow`'s `exclude`
  takes `key_id`s.
- **`Record::worktree_id`** stays: a cross-worktree read returns one row
  per worktree that shows the record (C.7).
- **`Pending(v)`** still means "the record's version is at most `v`". The
  guard moves from the update's `WHERE` to the delete of the replaced
  draft row (C.9).
- **`Commit(oid)`** compares the record's own `commit_id`, which is now
  per record ([§6.2](branch-segments.md#6-behaviour-changes)).

### Changed signatures (phase 1)

```rust
pub enum Changes { Records(Vec<Record>), Reset }   // §4.10
pub async fn list_changes(&self, since: Option<i64>, include_conflicts: bool)
    -> Result<Changes>;
```

`TxnRecord` gains `key_id` and `file_path`, which the rollup lines now
carry (§5).

### Forks and deletion (phase 2)

```rust
// unchanged signature: a worktree whose HEAD a family's chain holds is
// placed in that family (§4.5, §4.6) rather than scanned from nothing
pub async fn open(working_dir, DbConfig, FormatRegistry) -> Result<SyncedRepo>;
pub async fn delete_worktree(self) -> Result<()>;   // then compaction, C.14
```

`open` finds the family:
- by origin, for another branch of the same repository;
- by the `Git-Sync-Family` trailer on a git-sync commit in HEAD's
  history, for a fork of another origin.

That replaces `test_crud.rs`'s raw `UPDATE worktree SET family_id`.

### User branches and layered views (phase 3)

```rust
impl SyncedRepo {
    /// A branch off this worktree's head, with no checkout (§4.12).
    pub async fn create_user_branch(&self, name: &str) -> Result<i64>;
    pub async fn user_branches(&self) -> Result<Vec<Worktree>>;
    /// Main's view with these user branches over it, the last on top.
    pub fn layers(&self, stack: &[i64]) -> Layers;
}

impl Layers {
    // reads over the stack: C.2's merge by key_id; `layer` and `copies`
    // on each row
    pub async fn find_records(&self, q: &RecordQuery) -> Result<Vec<LayeredRecord>>;
    pub async fn facet_records(&self, q: &RecordQuery, f: &FacetSpec) -> Result<FacetRows>;
    pub async fn list_changes(&self, since: Option<i64>) -> Result<Changes>;
    // writes go into the top branch's draft, made through the stack (C.9)
    pub async fn apply_batch(&self, ops: Vec<BatchOp>, atomic: bool, meta: Option<TxnMeta>)
        -> Result<BatchOutcome>;
    pub async fn list_conflicts(&self) -> Result<Vec<Record>>;
    pub async fn resolve_conflict(&self, fp: &str, path: &str, key: &str, r: Resolution)
        -> Result<WriteOutcome>;
    /// Rebase the top branch onto main's head (C.15).
    pub async fn publish(&self) -> Result<Vec<RecordConflict>>;
}
pub struct LayeredRecord { pub record: Record, pub layer: i64, pub copies: i64 }
```

`Layers` shares the handle's `Db` and repository. It's a value, not a
connection: a stack is named per request, as the server will name it.

### A branch switch under an open handle

Today it silently writes branch B's content into A's worktree. With
ancestry checked, a HEAD on another branch would rebuild A's chain as
B. **Proposed:** the scan refuses with `Error::BranchChanged`, and the
caller reopens.

## 4. What's reused, replaced and added

Every new statement uses the generic `*_in_pool` style, not the
duplicated-SQL style. (Phase 1 replaced `Dialect` with the `Segments`
trait; see §7.1.)

| Kept | Where |
|---|---|
| `Db`, `DbConfig`, `connect`, the version check | `db/mod.rs` |
| The `Dialect` trait and `*_in_pool` dispatch | `db/tx.rs`, `crud.rs` |
| JSON and type filters, facet builders, `facet_values`, `TYPE_CLOSURE_CTE` | `db/record.rs`; their `FROM`/`WHERE` changes to the view |
| `enforce_conflict`, `occ_binds`, `race_expected` | `crud.rs`, `db/tx.rs` |
| `classify_conflict`, `read_base_docs`, `base_value` | `conflict.rs`, `scan.rs`; also for an edit's base when a merge brings a version back (§4.3) |
| The per-file scan pass, parse, validation, the unchanged-blob skip | `sync.rs`, `scan.rs` |
| Aliases: rows are immutable, so an alias row is written with each row | `alias`, `replace_aliases` |
| `txn` and its listing | `db/commit.rs` |
| The rollup builder, now with ids | `rollup.rs` |
| `parse_commit_rollup`, which gets its first caller: the scan's id lookup | `rollup.rs` |
| `git.rs`'s helpers, `commit_paths` | `git.rs` |

| Replaced | By |
|---|---|
| `WORKTREE_CLAUSE_*`, `WorktreeScope` | the view CTE (C.1, C.2, C.7) |
| Every `Dialect` constant (all keyed on `worktree_id`) | Appendix C's statements |
| `roll_forward`, `HAS_CONFLICT_SIBLING` in `worktree_id` form | the fold (C.12) |
| `commit_id IS NULL` as "pending" | a row in the draft segment, taken in from the file or not (§4.3) |
| `delete_missing`'s hard delete | tombstones or head-row deletes (§4.3) |
| `list_changes`, `list_by_version_range`, `list_dirty_files`, `load_pending`, `pending_bases` | the same over the view or the draft |
| `file::rename` + `scan::match_renames` | move pairing by `key_id`: a renamed file's records pair like any move |
| `file::reattribute` | nothing: attribution is per record |
| `auto_pick_default_file` | the same over the view |

| Added | |
|---|---|
| Segments, worktree chains, placement, forks, splits | §4.5–4.7, C.13 |
| An ancestry check, parents, merge bases | `git.rs`; §4.3, §4.8 |
| Rebuild after a rewrite | §4.8 |
| The rollup id lookup through parents | §3.5, §4.9 |
| Worktree deletion and compaction | §4.13, C.14 |
| User branches, layered reads and writes, publish | §4.12, C.2, C.9, C.15 |

## 5. Behaviour changes

From [§6](branch-segments.md#6-behaviour-changes), still to do: 1–10.
Found while planning:

- **Row ids leave the wire.** `unfurl.server.id` and `exclude` become
  `key_id`s. The server's `cloudmap.rs` and Python's
  `unfurl/cloudmap/proxy.py` change with it.
- **Rollup lines carry `key_id` and `file_path`.** §3.5 gives the id
  only. With moves, two files can hold the same `(path, key)`, so the
  id lookup needs the file too. The server's `test_cloudmap.rs:1582`
  parses rollups.
- **Staged, uncommitted edits become draft rows.** Today a file is clean
  when its blob matches the index, so a staged edit counts as committed
  and is never staged by `commit_repository`. The design compares with
  HEAD's blob.
- **A renamed file's records are new rows.** Pairing keeps their
  `key_id`s, as today, but they draw new versions, so `list_changes`
  reports them. Today a rename keeps the versions too.
- **Deleting a worktree** is new; today only cascades would.
- **A branch switch under an open handle is refused** (§3).
- **The paging cursor never holds a row id.** It's already
  `(path, key, file_path, worktree_id)`; Appendix C's C.4 and C.7 bind a
  row id, which an edit invalidates, and are corrected. In a layered
  read, `worktree_id` carries the layer, since copies share
  `(path, key, file_path)`.

## 6. Guards

Kept: those §6 lists, mapped to their new places.

Added:
- **The fold carries only what the save rendered.** A CRUD write between
  `save_changes` and the fold is carried today into a commit whose blob
  doesn't hold it, and C.12 would do the same. `commit_repository`
  reads the family's `next_version` for the rollup trailer after
  saving; it moves before `save_changes`, and the fold carries only
  draft rows below it. Later rows stay in the draft for the next commit.
- **The scan checks ancestry** before treating HEAD as a fast-forward
  (§4.3, §4.8).

## 7. Phases

Each phase starts by writing its missing SQL into Appendix C, checked
against the bench database, so the spec exists before the code. Each
ends with the differential harness green on the operations it enables,
on both backends, and the existing `crud_test!` suites green with the
phase's behaviour changes applied.

0. **Harness.** Split `segments_model.rs` into the shared module; add
   the test format and the real-git side; run it against the in-memory
   implementation only. No production code changes.
1. **One worktree.** Schema; view reads (C.1, C.3–C.7); writes (C.8,
   C.9); scan with pairing and rollup ids (C.10, C.11); the fold (C.12);
   the rollup with ids; `Record::id` as `key_id`, through the server and
   Python. Operations: Write, Commit, External, DiskEdit, Resolve, Move.
2. **Forks.** Placement, forks, splits, rebuild, deletion, compaction,
   ancestry. Appendix C adds split and rebuild. Operations: Fork,
   Rebuild, Delete.
3. **User branches.** `Layers`, publish. Appendix C adds publish's full
   classification and the re-link's tag for committed rows. Operations:
   NewUser, UserWrite, Publish.
4. **Merges.** The rollup id lookup through parents; entries for a
   version a merge brings back, with the base read from git. Operation:
   Merge.

The server's HTTP surface for user branches follows phase 3, separately.

### 7.1 Phase 1: done

The crate's suites, the server's `test_cloudmap` and 800 random
one-worktree histories agree with the in-memory implementation on
SQLite, and 300 on Postgres (`SEGMENTS_SQL_PG=1`).

How it differs from the plan:

- **SQL is written once, in SQLite syntax.** `db::seg::pg()` rewrites
  it for Postgres (`?N`, `jsonb(?N)`, `json(col)`). The `Segments`
  trait holds every segment statement, implemented for both backends by
  one macro; `on_pool!` runs a generic body on either pool. `Dialect`
  and `db/tx.rs` are gone.
- **A scan is one transaction:** HEAD's side, then the disk's, then
  renames, which pair on the draft side too.
- **An edit's base is stored, not read from git.** `record.base_json`
  holds the content `base_commit_id` names; the three-way check uses it,
  and the fold clears it. Reading it back from git by place failed once
  the record had moved.
- **Deferred:** rollup lines with `key_id`s go to phase 4, because the
  rollup lists only a txn batch's records, so it can't answer the id
  lookup for the rest. `list_changes`' Reset outcome goes to phase 2.

Id rules the harness found, applied alike in the model, the in-memory
implementation and SQL (branch-segments.md §4.3):

- A scan looks up a key's id in this order: rollup, move, the chain's
  record at the key, the draft's, new.
- A value taken in from the file keeps the draft's record, and a
  withdrawn edit's record continues, unless git holds that record at
  another key.
- A renewed record whose version equals its id gets a fresh id.

Model changes to match production: an empty commit folds onto HEAD; a
file-side resolution touches its key only (`FileWins::Only`); a forced
scan applies over every file; a disk edit that leaves the file
unchanged, and an external change that changes nothing, are skipped.

Behaviour changes, beyond §5's:

- **Resolving on the file's side withdraws the edit.** The file's value
  comes in as committed, not as a pending edit.
- **A forced scan leaves a taken-in tombstone** where the file lacks a
  record the database had.
- **A no-op save doesn't rewrite the file.**
- **Conflict rows are stamped by the fold** with the rest of the draft.
- **A record's `unfurl.server.commit` is the last commit that changed
  it,** no longer restamped with every commit to its file. `GET
  /cloudmap` reports the head it read at as `commit`, which the Python
  proxy uses for `latest_commit` instead of the records' commits.
- **`unfurl.server.id` and `exclude` are `key_id`s.** They're still
  integers, so the server and Python needed no change.
- **SQLite `record.id` is `AUTOINCREMENT`,** since a new record's
  `key_id` is its first row's id and must never be reused.
- **The server's tests run on Postgres** when `UNFURL_TEST_PG_URL` is
  set, each in a schema of its own.

Not done yet: the ancestry check and `Error::BranchChanged`, which go
with phase 2's rebuild.

## 8. For review

1. The differential harness, rather than checking SQL against the
   reference directly.
2. `Layers` as a value naming the stack per call, rather than a handle
   per user branch.
3. Refusing a branch switch under an open handle.
4. Finding a fork's family by origin, then by the `Git-Sync-Family`
   trailer.
5. The fold's version watermark.
6. Rollup lines carrying `file_path` as well as `key_id`.
