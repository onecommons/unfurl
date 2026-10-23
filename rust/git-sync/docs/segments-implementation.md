# Implementing segments

The plan for building [the segment design](branch-segments.md) into
git-sync, for review before any code. It covers what's reused, the API,
every behaviour change, and the phases with their gates. The design
itself isn't restated.

## 1. What shapes the plan

- **The segments schema replaced the migrations once**
  ([§7](branch-segments.md#7-starting-from-scratch)), and databases were
  rebuilt by scanning. Each change since adds its own migration, as
  `20260101000012_segment_merge.sql` does for merges.
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
a few allowed over-reports, its reads across worktrees against git's
rule (`check_across`), and each two worktrees' merge bases, the
segments', against git's. The SQL implementation is checked against
the in-memory one, step by step, for **exact** agreement, but for one
over-report in reads across where SQL falls short of the design
(`renamed-copy`, §7.7). So the other allowances stay a question about
the design, and SQL needs no internals exposed: no `recreated` flag, no
entries.

**What's compared,** after every step, and nothing else:
- each worktree's view: key → (content, `key_id`);
- its committed chain, the same way;
- its conflicts: key → (theirs, resolved);
- each layered stack's rows, from phase 3;
- a read across the open worktrees: (worktree, key, content), against
  the in-memory implementation's `read_across`; the same read two
  records a page, as a client reads it, against itself unpaged; and a
  facet's total over it;
- each two open worktrees' merge bases, through the segments' parents
  and merges, as commits;
- the tree of every commit the implementation makes.

`SEGMENTS_VISIBLE_ONLY` leaves out what no read sees, chain length, draft
entries and merge bases, so a random search shrinks onto a difference a
user could see.

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
Error::Reset { since: i64, reset_version: i64 }   // §4.10
pub async fn watermark(&self, worktrees: Option<&WorktreeFilter>) -> Result<i64>;
```

`list_changes`, `find_records` and facets fail with `Error::Reset` given
a `since_version` below the view's `reset_version`. `list_changes` has
no caller outside the tests, so an error keeps one mechanism for every
cursor read rather than a `Changes` enum for one of them.

`TxnRecord` gains `key_id` and `file_path`, which the rollup lines now
carry, and `CommitRollup` gains `database` and `records`, the records no
batch made (§5).

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

Two entry points, each replacing what happens today:
- **`open` of a worktree the database doesn't have** finds its family,
  places HEAD (C.16), splitting a segment if HEAD falls inside one
  (C.17), and forks there (C.13), where today it starts a family of its
  own with an empty chain and scans everything. Opening the same origin
  and branch again finds the existing row, as today.
- **A scan whose HEAD doesn't descend from the recorded commit**
  rebuilds (C.18) where today it applies the rewritten history as
  changes on the old chain. A HEAD on another branch than the handle
  was opened for is refused with `Error::BranchChanged`; reopening
  forks or finds that branch's own worktree.

```rust
Error::BranchChanged { expected: String, found: String }
Error::FamilyInUse { members: i64 }   // deleting a family root others belong to
```

- **Structural operations take the family lock first** (§4.14):
  `Store::lock_family`, an `UPDATE` of the family's `version_seq` row,
  which takes Postgres's row lock and SQLite's write lock as a write's
  version draw does. That covers fork, split, rebuild, deletion,
  compaction, and the scan, which reads the chain before it draws a
  version. **A fork's git work runs under the lock** (placement's
  ancestry walks, and the split's parses at *c* and *h*): keeping it
  outside would mean checking afterwards that the segment hadn't moved,
  and a fork is rare and a cloudmap one file.
- **`git.rs` gains** `is_ancestor`, `merge_base`, `changed_paths` and
  `first_parent_contains`. A tracked head whose commit this repository
  doesn't have is skipped by placement, never an error.
- **The rollup id reader comes forward from phase 4:** a split, a
  rebuild and a scan give a new row the id the rollup of the commit that
  set its value names, walked back through parents (§3.5, §4.9), as the
  model does. Only rollups whose `Git-Sync-Database` is this database's
  count. A scan walks merges made outside the database since phase 4.
- **A GET with a `since_version` below the view's `reset_version`**
  answers `409` with the code `RESET`, and the client re-reads. Every
  GET carries `version`, the watermark read before its records, which is
  the cursor a client resumes from: the highest record version it saw
  can be below `reset_version` for good, since a rebuild mostly keeps old
  rows. A rebuild draws the version it stores as `reset_version`, so a
  watermark read after it is never stale. The Python proxy's `refresh()`
  resumes from `version`, and on `RESET` replaces its cache with a whole
  re-read, keeping staged writes.

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
    pub async fn list_changes(&self, since: Option<i64>) -> Result<Vec<LayeredRecord>>;
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

**`create_user_branch` places the branch on this worktree's own line**,
where a segment of its chain ends at the head commit, an internal one
first, not wherever C.16 finds one: after main fast-forwards to a fork's
commit, the fork's closed head ends there too, and a user branch built
on it has its edits made over the fork's rows, not main's. The
in-memory implementation's `branch_off` does this.

### Remotes (phase 5, proposed)

[§4.15](branch-segments.md#415-remotes-pulling-pushing-and-conflicts) of
the design; nothing here is decided.

```rust
impl SyncedRepo {
    /// Move this worktree's conflicted edits to a new branch at their
    /// oldest base, committed without a checkout. `None` when there are
    /// no conflicts.
    pub async fn export_conflicts(&self, branch: &str) -> Result<Option<Exported>>;
    /// Changed signature: exports the conflicts its scan finds first when
    /// `conflicts_to_branch` is set. `CommitOptions::default()` commits as
    /// today.
    pub async fn commit_repository(&self, message: &str, options: CommitOptions)
        -> Result<Committed>;
    /// Built: a worktree for `branch`, with no checkout, forked from this
    /// one at the commit its head is at, not cloned at startup.
    pub async fn fork_branch(&self, branch: &str, pushed: &str) -> Result<i64>;
    /// Built: rebase the local commits onto `upstream` in the database
    /// (C.21); the caller then resets the checkout to `upstream`, and scans
    /// and commits. Fetching, pushing and the reset stay in the server.
    pub async fn rebase_onto(&self, upstream: &str) -> Result<Rebased>;
    /// Built: merge `upstream` into HEAD record by record where git can't
    /// (C.22), given git's merged tree and the files it couldn't merge;
    /// the caller fast-forwards to the merge, pushes and scans.
    pub async fn merge_upstream(&self, upstream: CommitId, merged: gix::ObjectId,
        conflicted: &[String]) -> Result<MergedUpstream>;
}
pub struct CommitOptions { pub conflicts_to_branch: Option<String> }
/// What one call committed: to the current branch, and to the exported
/// branch, either of which can be absent.
pub struct Committed { pub commit: Option<String>, pub exported: Option<Exported> }
/// The branch the conflicted edits went to, its commit, and the records
/// moved there.
pub struct Exported { pub branch: String, pub commit: String, pub records: Vec<TxnRecord> }
pub enum Rebased { Unfolded { base: String, edits: usize }, FastForward, Ahead, Refused(String) }
pub enum MergedUpstream { Merged { commit: CommitId, held: Vec<HeldRecord> }, FastForward, Ahead, Refused(String) }
```

A checkout-less branch is a `worktree` row like any other, marked not to
be cloned at startup: it gets no `SyncedRepo` of its own until something
checks the branch out and takes the row over.

**Clone mode (the server, built).** A server given a database and a clone
root but no checkout (`--clone-root`, which `unfurl serve` sets) makes
the checkouts from the database: for each worktree marked to be cloned
at startup (`clone_on_start`) whose origin is a project on the cloud
server, a clone of its branch where python puts one,
`public/<project>/<branch>` anonymously or, for a private project, with
its users' most recent grant under `private/`, or a fast-forward of the
one there, under python's lock file. One that can't be fast-forwarded is
reset to its remote if it holds no work the remote lacks, and otherwise
served as it is; a broken one is moved aside and cloned again. A worktree that can't
be cloned (an exported branch the remote lacks, a private project no
grant opens and no checkout of, a remote that doesn't answer in five
minutes) is logged and left to python. A request naming a project and
branch with no checkout brings one up the same way, with the request's
own credentials, under `private/`, if it has them -- kept as a grant
for its later pulls -- and its project and
branch never a path out of the clone root; one that can't be brought up
is left to python. The gateway has checked each request's access to its
project either way. A served checkout is pulled when a request finds it
stale by python's pull record, or lacking the request's `latest_commit`,
and rescanned if its HEAD moved (**Freshness** in the server's README). Each checkout
is opened and scanned with `recover_missing`, since a new clone is where
commits the database made go missing, and writes are routed to the
checkout of their project and branch (when they name none, the
repository's default branch, which opening the checkout records in
`repository`, or `main` until it has). The repositories `--default-cloudmap` names are
cloned too, and a read naming no project reads their worktrees, by id;
without any, it goes to python. Reads
go through any of them, as they read the database. Python's server
pulls, writes and clears in the same clones, so git work in one -- the
startup pull and scan, finishing exports, a write's commit -- holds its
git lock, the Redis key python holds around its own
(`_git_lock::<real path>`), renewed while it's held; a write waits 30
seconds for it, then gets 503.

### A branch switch under an open handle

Today it silently writes branch B's content into A's worktree. With
ancestry checked, a HEAD on another branch would rebuild A's chain as
B. **Decided:** the scan refuses with `Error::BranchChanged`, and the
caller reopens.

## 4. What's reused, replaced and added

Every new statement uses the generic `*_in_pool` style, not the
duplicated-SQL style. (Phase 1 replaced `Dialect` with the `Store`
trait; see §7.1.)

| Kept | Where |
|---|---|
| `Db`, `DbConfig`, `connect`, the version check | `db/mod.rs` |
| The `Dialect` trait and `*_in_pool` dispatch | `db/tx.rs`, `crud.rs` |
| JSON and type filters, facet builders, `TYPE_CLOSURE_CTE` | `db/record.rs`; their `FROM`/`WHERE` changes to the view |
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
| Merges between segments, and merge bases from them | §4.3, §4.9, C.10, `segment_merge` |
| Reads across worktrees by git's rule, the copies left out in Rust | §4.1, C.7 |
| User branches, layered reads and writes, publish | §4.12, C.2, C.9, C.15 |

## 5. Behaviour changes

From [§6](branch-segments.md#6-behaviour-changes), still to do: 1–10.
Found while planning:

- **Row ids leave the wire.** `unfurl.server.id` and `exclude` become
  `key_id`s. The server's `cloudmap.rs` and Python's
  `unfurl/cloudmap/proxy.py` change with it.
- **Rollup lines carry `key_id` and `file_path`.** §3.5 gave the id
  only. With moves, two files can hold the same `(path, key)`, so the
  id lookup needs the file too. The server's `test_cloudmap.rs:1582`
  parses rollups.
- **The rollup names every record the commit changes,** not only a
  batch's: the rest go in a records block with a `Git-Sync-Record-Count`
  trailer. Today single-record writes, batches without `TxnMeta`, hand
  edits and resolutions are named nowhere, so the id lookup couldn't
  find them. A batch write that leaves the committed value as it was
  now counts in the shortfall line.
- **`Git-Sync-Database`.** A `key_id` is one database's row id, so the
  rollup names the database, from a `database_identity` row created
  with it, and a reader takes only its own database's ids.
- **Staged, uncommitted edits become draft rows.** Today a file is clean
  when its blob matches the index, so a staged edit counts as committed
  and is never staged by `commit_repository`. The design compares with
  HEAD's blob.
- **A renamed file's records are new rows.** Pairing keeps their
  `key_id`s, as today, but they draw new versions, so `list_changes`
  reports them. Today a rename keeps the versions too.
- **Deleting a worktree** is new; today only cascades would.
- **A branch switch under an open handle is refused** (§3).
- **Branches of one origin share rows, record ids and one version
  counter,** and a fork whose history names a family joins it. Today
  each branch is a family of its own, with its own ids.
- **A rewritten HEAD rebuilds,** and a cursor from before it gets
  `Error::Reset`, or `409 RESET` from a GET with `since_version`.
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
   ancestry, and the rollup id lookup through parents for this
   database's rollups. Appendix C adds placement, split, rebuild and
   deletion (C.16–C.19). Operations: Fork, Rebuild, Delete.
3. **User branches.** `Layers`, publish. Appendix C adds publish's full
   classification and the re-link's tag for committed rows. Operations:
   NewUser, UserWrite, Publish. Until then the SQL harness runs a
   history with one of them in memory only, counted as skipped; once
   `sql_supports` gains them, the scripted ones (`histories.rs`, such as
   `a_publish_hides_main_s_unchanged_save_of_the_value_it_rebased_over`)
   and the `publishing` search run against SQL unchanged. They check
   what C.15's publish leaves only through a layered read, so
   `SqlWorld::check` must compare each layered stack's rows (§2) in the
   same change.
4. **Merges.** The id lookup through merges made outside the
   database (built, §7.3); entries for a version a merge brings back,
   with the base read from git, which are users' and wait on phase 3.
   Operation: Merge.

5. **Remotes** (proposed, [§4.15](branch-segments.md#415-remotes-pulling-pushing-and-conflicts)),
   in this order, each usable on its own:
   1. **Export:** a checkout-less branch forked at the edits' base, the
      cross-worktree move and the fold without a checkout,
      `export_conflicts`, and `commit_repository`'s new `CommitOptions`,
      with a batch's `txn` row split across the two commits.
   2. **Branches without a checkout, kept as rows (built).** Not advanced
      without a checkout: `worktree.clone_on_start`, false for an export's,
      a recovery's and a `fork_branch`'s row, and for one the server
      brought up on demand, keeps a server cloning the worktrees at
      startup from cloning them; a request naming such a branch brings up
      a checkout, which takes the row over. The server forks the commits
      a push diverts this way, so their records stay readable by id. A
      sweep of worktrees whose remote branch is gone, for every branch,
      is left for later.
   3. **Pull:** fetch and fast-forward, then the unfold and the rebase in
      the database. The first half is built in the server (`clone.rs`,
      "Freshness" in its README), with the git command line; the unfold
      and rebase are built in git-sync (`rebase_onto`, §7.5), and the
      server doesn't use them yet.
   4. **Push:** push support (the `git` CLI or git2; gix can't push),
      the retry, and diverting to a new ref. Built in the server instead
      (`push.rs`, "Pushing" in its README): what the remote moved
      on from is merged in git when it merges cleanly, and diverted when
      it doesn't, pending the rebase.

   **Transport and integration.** Fetching, pushing and changing the
   working tree stay in the server, which holds the credentials and the
   distrust policy for the git command line. git-sync's
   `rebase_onto(upstream)` changes the database first; the server then
   resets the checkout and scans and commits, as for any outside change.
   The database going first is what lets a failure between the two heal,
   with no mark to finish from, and it needs no trait for the server to
   implement.

   **Order from here:** phase 4's id lookup is built, so a merged
   diverted or exported branch keeps its records' ids, and step 3's
   unfold and rebase, and the merge record by record (C.22), are built
   in git-sync, and the server's push merges with it, exporting the
   records it held. Then the sweep.

   Appendix C adds the cross-worktree fold and the unfold. Operations:
   Export, Pull, Push, Divert.

   It depends only on phase 2. Phase 3's publish could use step 1's fold
   without a checkout, and its user branches are step 2's rows. Phase 4 is
   what keeps a record's id when an exported branch is merged back in
   git.

The server's HTTP surface for user branches follows phase 3, separately.

### 7.1 Phase 1: done

The crate's suites, the server's `test_cloudmap` and 800 random
one-worktree histories agree with the in-memory implementation on
SQLite, and 300 on Postgres (`SEGMENTS_SQL_PG=1`).

How it differs from the plan:

- **SQL is written once, in SQLite syntax.** `db::sql!` spells it for
  Postgres at compile time (`?N`, `jsonb(?N)`, `json(col)`), and
  `db::pg()` at runtime for SQL built then. The `Store` trait holds
  every segment statement, implemented for both backends by one macro;
  `on_pool!` runs a single body on either pool. `Dialect` and
  `db/tx.rs` are gone.
- **A scan is one transaction:** HEAD's side, then the disk's, then
  renames, which pair on the draft side too.
- **An edit's base is stored, not read from git.** `record.base_json`
  holds the content `base_commit_id` names; the three-way check uses it,
  and the fold clears it. Reading it back from git by place failed once
  the record had moved.
- **Rollups name ids,** for every record a commit changes, under the
  database's identity (§5), which the harness checks against the
  model's on every commit. The commit's own head rows read them first,
  as the model does (`file_deletion_under_an_edit_keeps_the_id`).
  Reading an earlier commit's, through a merge's parents, came with
  phase 4. The Reset outcome goes to phase 2.

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
- **A no-op save doesn't rewrite the file.** And a save that renders to
  what HEAD's own document renders to writes HEAD's bytes, so JSON's
  re-emitted formatting doesn't make a commit on its own
  (`deleting_a_hand_edits_record_commits_nothing`). A comment or prose
  only on disk makes the two renders differ, so it's kept
  (`a_save_back_to_head_keeps_an_uncommitted_comment`).
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

### 7.2 Phase 2: done

Fork on open, placement, split, rebuild, deletion and compaction (C.13,
C.14, C.16–C.19), with the rollup id lookup through parents. The crate's
suites, the server's, and `rust/dev/verify.sh`'s random histories (100
of each kind per run, on SQLite and Postgres) agree with the in-memory
implementation. The harness runs Fork, Rebuild, Rebase and Delete, and
checks each worktree's chain length as well as its view, which is what
makes compaction visible to it.

How it differs from the plan:

- **A rebuild can land on another worktree's line,** as `git rebase
  main` does after main moved on. It prepares the base as a fork does
  (`Store::prepare_base`, shared with C.13): another worktree's open
  head closes, or an empty one steps to its parent where that ends at
  the same commit. The rebuilt chain
  takes the base's ancestry it didn't hold. The harness's `Op::Rebase`
  covers it, with a strategy of its own so seeds saved under `op()` still
  replay the same histories.
- **Reset is an error, not a `Changes` enum.** `list_changes`,
  `find_records` and facets fail with `Error::Reset` below the view's
  `reset_version`, and `watermark()` gives the cursor to resume from,
  which `GET /cloudmap` returns as `version`. A rebuild draws the version
  it stores, so a watermark read after it is never stale. A cursor over
  worktrees of more than one family is refused
  (`Error::CursorAcrossFamilies`).
- **A rebuild re-derives the draft's entries** before re-linking
  (§4.8 step 5), and the scan after it parses every file again, so a
  `git reset --mixed` that kept the working tree shows what it holds.
- **Compaction runs inline,** in the deletion's or rebuild's
  transaction, as the model runs it, and **never folds into an open
  head** (§4.13): the head's writes replace its rows in place, which
  would erase the parent's, and with them the id a split recovers.
- **A split keeps what a scan skipped:** a record validation rejected
  at the fork commit takes the row the scan kept, and a place whose
  value there is what's below only moves its entries (§4.7).
- **C.19 lets go of a root's head and draft** before deleting its
  family's segments, which the worktree row references.
- **A handle opened on a detached HEAD keeps scanning;** one opened on
  a branch refuses a detached HEAD (`Error::Detached`) or another branch
  (`Error::BranchChanged`).

Behaviour changes, beyond §5's:

- **`GET /cloudmap` returns `version`**, and a stale `since_version`
  answers `409 RESET`; the Python proxy resumes from `version` and
  re-reads on `RESET`, keeping staged writes.
- **The scan's head update and the commit take the family lock** before
  reading the head.
- **A new origin's family trailer is looked for along first parents
  only,** at most 1000 commits back, and only in this database's
  rollups.

Open:

- **A rollup could name an id its head row doesn't get,** if two places
  were ever given one id. Not reproduced; `advance_head` debug-asserts
  it.
- **A skipped record's row a head replaced in place is lost to a
  split.** In a worktree with no fork yet, whose one head spans all its
  commits, a fork at a commit where a record was rejected can't recover
  the row the scan kept then once the head has changed it since.
### 7.3 Phase 4: ids through merges, built

The harness runs Merge against SQL: the 38 seeded histories that make
one, and the random runs of every operation. The strategies whose seeds
predate merges keep the filter they were saved under
(`ran_before_merges`), so the seeds replay the same histories.

- **A scan of a commit made outside the database takes the ids rollups
  name** for the places it gives a value, walked back through parents
  to the commit that set each value (`History::id`, §4.9). An id
  another place holds is passed over; the commit's own rollup, on a
  commit through the database, is still asserted.
- **Only the family's own rollups count,** by `Git-Sync-Family`, in the
  split's lookup too: `key_id`s are the database's, so another family's
  merged in would take over records of its own.
- **A new family's first scan takes no ids from rollups.** It has made
  no commits, so no rollup names it. With no rows, every record is a
  change, and the walk would read the tree of every commit back to the
  one that set each value, and parse each version of the file once.
- **The reference never reuses an id.** A new record whose version was
  once some row's id gets a fresh one; it checked only the tree before.
  The first runs against SQL found it, in six of the seeded histories.
  Two of them depend on the walk itself:
  `merge_finds_the_rollup_past_a_squash` and
  `split_moves_late_rows_after_merges`.

Entries for a version a merge or revert brings back into main: the new
row takes over other drafts' entries on an earlier row with its content
at its place, made by an edit there or of its record, so a fork's edit
made over that version still hides it in a read across both. A draft's
edit at the place or of the record made over that version hides it too,
though the row the edit saw is gone, or at the record's old place after
a move. Not built: the in-memory implementation also passes on entries
on the record's rows at another place, which, with those edits' own
entries made, no history found yet needs.

Open:

- **The walk runs in the scan's transaction, under the family lock.**
  It costs what the rows lag behind HEAD, as a split's does, except
  for a value that came back from long ago: a reset to an old commit,
  a file that newly parses, a long-lived branch merged in. Bounding it
  would make the id depend on how far back it was set.

### 7.4 Phase 5, step 1: export (plan)

Built: `src/export.rs`, with `Op::Export` in the harness on both sides.
How it differs from this plan:

- **Two transactions, not one.** The move (fork, copies, W's
  resolutions, batches, re-link) is one; the fold is another, through a
  handle on the branch (`SyncedRepo::sibling`, sharing this one's
  database, formats and repository) rather than a `Target` struct. The
  commit is written between them, from the branch's draft, because its
  rollup names ids only the move settles.
- **The ref is claimed at the base** before the move, and moved to the
  commit after it; the guard compares the edits against those the base
  was picked for, not those rendered.
- **Rendered twice:** once before anything changes, so a file that can't
  be rendered at the base fails cleanly, and again from the branch's
  draft for the commit.
- **Resumable:** a failure after the move leaves the edits in the
  branch's draft, and exporting to the same branch finishes it. An error
  from `commit_repository` after its export leaves them on the branch
  its options name; calling it again finishes an unfinished export.
  The draft is folded into the commit before the ref moves, so the
  database already names the commit when only the ref is left to move.
  Only the exporting worktree's own unfinished export is resumed:
  `worktree.exporting_from` names its branch from the move transaction
  until the ref is at the commit, so no other worktree's state, and no
  finished export's, passes for one. The branch and its commit are only
  in the local repository until pushed: a new clone has neither.
- **`finish_exports`** completes every unfinished export from a
  worktree, as exporting to each branch again would. The server calls it
  once at startup, before anything else writes; two replicas starting
  together could both finish the same one, which a claim on the row
  would prevent.
- **Recovery reuses the export's second half.** `recover_missing` forks
  `git-sync/recovered-<commit>` at the commit the missing commits were
  made on, copies their rows into its draft, and commits it as an export
  does (§4.8), so a failure after the rebuild is an unfinished export.
- **The branch gets the source's file rows** at the base, so its commit
  parses only the files the export changed.
- **No entries are copied:** re-linking the branch's draft derives them.
- **The harness changed C.20 three ways:** the base rule (the latest
  commit on HEAD's first-parent line every base descends from), a new
  record where the base has the edit's record at another place, and W's
  side being `resolve_conflict(Theirs)`'s code.

The rest of this section is the plan as written. The SQL is [C.20](branch-segments.md#c20-export-a-worktrees-conflicts-to-a-branch);
the decisions are §3's and [§4.15](branch-segments.md#415-remotes-pulling-pushing-and-conflicts)'s.
Advancing and sweeping checkout-less branches are deferred (step 2).

**Order of work.** Each item is reviewable on its own, and the suites
stay green after each.

1. **Harness first.** `Op::Export(w)` in `tests/segments`:
   - **Model** (`imp.rs`, `world.rs`): a new worktree forked at the
     oldest base of W's unresolved conflicts, whose head holds the base's
     tree with W's conflicted edits applied; W's draft and conflict rows
     at those keys go. The real-git side (`GitMirror`) makes the commit,
     so `check_rollup` checks its rollup.
   - **SQL side** (`sql_world.rs`): calls `export_conflicts` on W's
     handle. The new worktree has no handle, so its view is read across
     worktrees (C.7). `sql_supports` gains `Export`.
   - Seeded histories for the edge cases below, then random ones.
2. **Git without a checkout** (`git.rs`):
   - `commit_files_onto(repo, parent, files: &[(String, Option<Vec<u8>>)],
     message) -> ObjectId`: `build_tree_with_updates` over blobs written
     from bytes, committed with no ref updated. `commit_paths_onto`
     becomes a wrapper that reads the paths from disk.
   - `create_ref(repo, name, oid)` with `PreviousValue::MustNotExist`,
     and `delete_ref_if(repo, name, oid)` for the rollback.
   - `export_base(repo, head, &[oid])`: the latest commit on `head`'s
     first-parent line that each oid descends from or is, else the
     line's first commit.
3. **Rendering from git's bytes** (`sync.rs`): split `render_file`'s
   apply and emit (from `apply_pending_records` through `render`) into a
   function of `(file_path, source: Option<&str>, pending, format,
   bases, conflict_rows, check)` returning the bytes. `render_file`
   keeps its reads and its races; the export calls it with the base's
   blob as the source, only the conflicted edits as pending, no conflict
   rows, so none is skipped, and `check: None`. An edit made on a later
   base than the one chosen has a `base_json` that isn't the chosen
   blob's, and a check would read that as a divergence: onto the base,
   every edit applies unconditionally.
4. **Folding without a handle** (`scan.rs`): `commit_in_pool`,
   `advance_head` and the file-row updates take a small `Target { w,
   family }` (plus the formats) instead of `&SyncedRepo`. The commit path
   passes its own; the export passes the new worktree's.
5. **Forking inside a transaction** (`fork.rs`): `fork_into` takes the
   caller's transaction, so the fork, the move and the fold commit
   together, and a failure leaves no worktree behind.
6. **The move** (`db/store.rs`): C.20's statements 1-6 as `Store`
   methods, written once in SQLite syntax as the others are, plus
   `conflicted_edits(tx, d)` for the guard and the render.
7. **The rollup** (`sync.rs`): the part of `commit_scanned` that builds
   the rollup from the outstanding batches and their records becomes a
   function of a worktree, a record set and a watermark, so the export
   builds the branch commit's message the same way. A batch whose
   records all moved is moved (C.20 step 6), so it drops out of W's next
   rollup.
8. **`export_conflicts(branch) -> Result<Option<Exported>>`:**
   1. Read W's unresolved conflicts and their edits in one snapshot. None:
      `Ok(None)`.
   2. Pick the base (C.20: the latest commit on `HEAD`'s first-parent
      line every edit's base descends from or is), render each file,
      build the rollup, write the commit, and create the ref. An existing
      ref is `Error::BranchExists`.
   3. In one transaction: lock the family, fork, check the guard, C.20,
      C.11, C.12 and C.10 for the new worktree.
   4. When the guard fails, roll back, delete the ref, and render again,
      up to `WRITE_ATTEMPTS` times, then `Error::FileChanged`. Any other
      failure deletes the ref and returns the error.
9. **`commit_repository(message, CommitOptions) -> Result<Committed>`:**
   the export runs between the scan and `commit_scanned`, when
   `conflicts_to_branch` is set. The 37 call sites change mechanically;
   the server passes `CommitOptions::default()`. `HeadMoved` is retried
   inside `commit_repository`, once, after the export: scan and
   `commit_scanned` again without exporting, and return the first
   attempt's `Exported` with the retry's commit. A retry from outside
   would lose it, since its scan finds nothing left to export. The
   server's `retry_if_head_moved` stays for the other writes.
10. **Docs:** the README's conflicts section gets the option.

**Tests** (`crud_test!`, both backends), each with a mutation that
undoes what it guards:
- W's conflicted edits move; W has no conflicts and shows the file's
  values; its other pending edits are committed on W as usual.
- A standalone export leaves W showing the file's value at once, with no
  scan or commit after it, for a dirty file (step 4's resolution) and
  a clean one (the chain's row).
- `HeadMoved` after a successful export: `commit_repository` retries,
  commits, and still returns the `Exported`.
- The branch commit holds the base's file with the edits applied, and
  its rollup names the moved batches.
- `git merge` of the branch into W conflicts textually exactly where the
  records did; after resolving it, W's scan takes the merge in with no
  record conflicts.
- A batch split between the two commits appears in both rollups; one
  wholly exported appears only in the branch's.
- Bases on different lines use their merge base; edits with no base use
  `HEAD`; a deletion against a modification exports as a tombstone.
- A write that replaces an exported edit between the render and the
  transaction makes the export render again (a hook as
  `render_inputs_racing`), and W's render from before the export loses.
- `BranchExists` leaves the database and the refs unchanged.
- No conflicts: `None`, and no ref.

**Gates:** the CRAP check on what changed (aim CC 25 or less), CI's
clippy (`--all-features --all-targets -D warnings`), the crate's suites
and the harness on both backends, and the server's `test_cloudmap`.

**Risks:**
- **C.20 step 2** drops entries on rows above the base and relies on
  C.11 to derive the branch's. The harness is the check.
- **`fork_into` in the caller's transaction** changes when its lock is
  taken; the scan and the commit take the family lock in the same order.
- **A branch with no file rows until C.10.** The fold (C.12) runs before
  the head update creates them, so it must not need them. C.13 in the
  design copies them, but the fork as built doesn't, and C.20 follows
  what was built.
- **C.20 step 4 is `resolve_conflict(Theirs)`'s code,** run per key in
  the export's transaction, so W's side is exactly a resolution's; the
  harness checks it against the model's.

### 7.5 Phase 5, step 3: the rebase in the database, built in git-sync

`rebase_onto(upstream)` is C.21. The harness runs it as `Integrate`:
an outside commit off a commit below W's head, the rebase onto it, the
reset, and the scan, on its own strategy so the seeds saved under the
others replay as before. Where the model refuses, it still makes the
upstream commit and SQL must refuse it too. 600 random histories and 15
seeded ones agree with the reference on SQLite. `test_rebase` checks
the result and the refusals a history can't easily make, on both
backends: a dirty tree, data outside the records, an added file, a mode
change, an unparseable file, a moved record, conflicts, and an
unscanned HEAD.

How it differs from the plan:
- **Values, not bytes,** decide whether the local commits changed only
  records. Rendering every edit onto the merge base at once orders new
  records differently from the commits that made them one at a time, so
  bytes would refuse most rebases. Comments, layout and key order the
  local commits changed aren't kept.
- **A tombstone deletes what the rebuilt chain shows.** Upstream can
  bring a record back with an id a split gave it (§4.7); a tombstone
  with the rollup's id would delete a record that isn't there.
- **The row beats the rollup** for an id: an outside commit after the
  last rollup can have re-created the record.
- **A create has no base,** or the three-way check reads upstream's
  lacking it as an edit of a record the file dropped.
- **The reference never reuses an id in a split** either: its fresh id
  for a re-created row had come back for the same version at the same
  key.

Behaviour changes, for the server's use of it:
- **Comments, layout and key order** the local commits changed are
  dropped: only their records are carried onto upstream.
- **Clients re-read:** the rebuild resets W's cursors, as any rebuild
  does, where a clean merge in git was a fast-forward scan.
- **The caller holds the checkout** from `rebase_onto` through the reset,
  the scan and the commit, and resets only while HEAD is still the commit
  `Unfolded` names: a scan in between undoes the rebase, leaving no-op
  edits the next commit folds, and a commit in between wasn't unfolded.
  `refs/git-sync/rebased/<branch>` keeps the local commits reachable.
- **A worktree with conflicts isn't rebased:** they're resolved, or
  exported, first.

Not built:
- **With user branches open.** The harness skips `Integrate` while one
  exists: a user's edit over a version the unfold copies needs the
  entries a merge's old versions do (phase 4's remainder, after phase
  3).
- **A test of the refusal for a record validation rejects.** The
  random histories cover an edit pending at another place.
- **Restamping on a heal.** A rebase that heals leaves its batches
  outstanding, so the next commit's rollup names them again.
- **The server's use of it,** next.

### 7.6 Phase 5, step 3: the merge record by record, built in git-sync

`merge_upstream` is C.22, the server's way to take in an upstream that
moved on: where C.21 rewrites the local commits, this merges, so no
commit is rewritten, no batch is unstamped, and clients' cursors
survive. Files it builds from records lose comments and layout the
local commits changed there, as C.21's do.

When the server pushes a checkout whose remote has moved on, it calls
`merge_upstream`, where it used to merge in git only and divert what
git couldn't merge. If the merge held records, the server then scans,
so they're in conflict, and exports them with `export_held` (which
exports only the conflicts at the records given) to
`git-sync/conflicts-<merge>`. One push carries the merge and that
branch. `/events` reports `conflicted`, naming the branch, for each
pushed commit that wrote a held record; the server finds the branch by
the merge's name, not from the push that made it, so a merge pushed
again after an error, or diverted, reports it as well. `held` lists
only the records the merge inserted a held edit for: a client's staged
write at such a record stays in conflict on the branch, unexported, as
no uncommitted write is. Other conflicts aren't
exported: on the server most come from a pull landing under writes no
client has committed yet, and exporting them would push those writes.
`rebase_onto` stays, unused by the server.

The harness runs `merge_upstream` as `MergeUpstream` (the model's
`merge` with upstream's side picked for a key both changed, after
holding ours), on its own strategy; 300 random
histories and 6 seeded ones agree with the reference, with the mirror
running `git merge-tree` for SQL. `test_merge` checks git's textual
conflict between different records, a record both changed, fields git
merges, a duplicate key git made in YAML and in JSON, a merged tree
missing a change of ours, a conflict elsewhere left alone, and each
refusal, on both backends.

What the harness found:
- **A held deletion names its record as the database can:** the row or
  tombstone the chain holds at its place, else this family's rollup
  line, else a new id. With no rebuild to name it, a purged tombstone's
  record gets a new id, and upstream's version of it takes that id.
- **The id lookup keeps tombstones,** which C.21 didn't need: a
  deletion an outside commit made has no rollup line.

Untested: the server's second `export_held`, which finishes an export
that failed after its records moved, has no server test, since nothing
there can make the first one fail; git-sync's
`a_failed_export_of_held_records_finishes` covers the resume it relies
on.

Open: C.21 moves an already-pending edit's base before its reset, so a
rebase stopped there leaves that edit in conflict with the worktree's
own commit; the merge leaves such an edit as it is instead.

Not built: a diverged freshness pull,
which keeps serving the checkout until the next push merges; and a
field-by-field merge of a record both sides changed where git couldn't.

### 7.7 Phase 3, step 1: copies, built in the server

A read across several worktrees is each one's view side by side, as
before. The server reports what it holds more than once: a key in two
files, or in two worktrees or repositories, has one record in `result`
-- the first live one in sort order -- and every live copy under
`conflicts`, grouped by `origin`, `branch` and `file`, `kind: copies`,
on every read; it used to keep the last row of each key and drop the
rest unseen. A tombstone, in an incremental read, is a deletion, shown
where nothing live is. A write naming no file, of a key live in more
than one, fails (`Error::Ambiguous`, a 409) rather than taking the
first. Pages already end on a `(path, key)` (`whole_groups`).

**Which copies are left out** follows git: a version one worktree shows
is left out where it's unchanged since its merge base with another the
read covers, which has changed it since (branch-segments.md §4.1). The
merge bases come from the segments' parents and the merges' edges
(§4.9), which a scan records for each commit it brings in of another
worktree's line, made to end a segment there; compaction keeps what live
histories hold through them. Each worktree's entries apply in its own
view only, so a draft's entry on another worktree's row no longer hides
it there.

**Status.** Both implementations read across this way: `find_records`,
`find_records_follow` and `facet_records` in SQL, with the copies left
out in Rust (C.7). The model test checks the in-memory implementation's
reads across against git's rule at every step, and its merge bases
against git's; the SQL harness checks SQL's against the in-memory
implementation's. Its limits:
- **SQL finds a moved version by its `key_id`,** where the design and
  the in-memory implementation match content. Where the merge base holds
  the version at its old place under another id, as a copy can after a
  fast-forward or a fork (§3.5), SQL doesn't see the move, and a read
  across shows a version git's rule leaves out: the SQL harness's
  `renamed-copy` allowance.
- **Content compares by a 64-bit hash** of its canonical JSON, which
  `Store::insert` writes with each row (`record.content_hash`), so a
  collision would read a changed version as unchanged. A row written
  outside the store has none, and compares as changed: the bench fills
  in its generated rows' before timing.
- **Past 300 records, a read fetches every row in the covered segments**
  into memory, without its content: on the bench, every facet across,
  31,498 rows. Facets across take 1.6 and 1.2 times the old rule's time
  on SQLite, and on the bench's 500-record family, 1.7 and 1.5 on
  Postgres (bench/segments/README.md).
- **The server's conflicts report across worktrees** reads every row
  across them, unpaged, and resolves them all before keeping its few
  conflict rows. Not yet measured.
- **A facet across worktrees counts no conflict rows,** with
  `include_conflicts` or without: the rule fetches none to resolve. A
  find across keeps them. Nothing asks for both today.

Next: what drafts keep on other worktrees' rows (`other_entries`,
`draft_entries_on`, `brought_back`'s hand-on and `take_over`'s
other-drafts part) no longer matters to reads across, but a user
branch's layered view (§4.12) still reads its draft's entries on main's
rows, and a rebuild (§4.8, step 5) a fork draft's handed-on entries. So
those helpers can stop only for drafts never layered, which needs a
marker the database can see, with mutation runs against the model's
layered reads, `check_across` and the rebuild's conflict check.

The SQL harness has no user branches, so a draft entry on a row outside
its own view, as §4.12's second branch point makes, is checked only once
phase 3 adds them.

## 8. For review

1. The differential harness, rather than checking SQL against the
   reference directly.
2. `Layers` as a value naming the stack per call, rather than a handle
   per user branch.
3. Refusing a branch switch under an open handle.
4. Finding a fork's family by origin, then by the `Git-Sync-Family`
   trailer.
5. The fold's version watermark.
6. Rollup lines carrying `file_path` as well as `key_id`, a records
   block for changes no batch made, and `Git-Sync-Database` scoping the
   ids to the database that wrote them.
