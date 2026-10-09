# Sharing records between branches, forks and layers

Status: draft proposal, 2026-09-27. Nothing here is implemented.

## Summary

git-sync keeps a complete copy of a worktree's records for every
`(origin, branch)` it tracks. Branches, forks and users' branches of one
cloudmap share most of their history, so almost all of those copies would
be identical: a branch created a minute ago duplicates every record, and
scanning it re-parses every file.

This design stores each version of a record once, visible to every
worktree whose history contains it:

- **Segments.** A segment holds the record versions by which the tree at
  one commit differs from the state of its parent segment. Segments form
  a tree that mirrors where tracked worktrees fork. A worktree reads the
  chain from the root to its own **head segment**, plus a **draft
  segment** holding its uncommitted changes.
- **Supersession.** When a version of a record is written into a
  segment, the version it replaces is recorded as superseded in that
  segment. A row is visible to a read when its segment is in the read's
  view and none of the segments that supersede it are. Reads stay one
  statement of equality joins and an anti-join, so the JSON indexes still
  drive searches and the planner's estimates stay sound.
- **Rows never change.** Every edit writes a new row. That's what lets a
  change in one branch show up in reads that combine it with another.
- **Cheap branches.** Creating a branch or fork adds one row per segment
  in its chain and one per file, and none per record.
- **Layered reads.** A read can combine branches: main plus a user's
  branch that isn't in git yet, or a public branch plus private record
  data. Each upper branch contributes its own changes on top of the one
  below. A record more than one of them holds comes back once per branch,
  and the client or server decides from the content whether to merge the
  copies or treat them as a conflict. Publishing a user's branch to git as
  a real branch for a pull request copies no rows.
- **Rewrites rebuild from the tree.** A worktree whose new HEAD doesn't
  descend from its old one is rebuilt from the tree, reusing whatever
  segments still apply. History is never replayed.

None of this is new; [Appendix A](#appendix-a-prior-art) compares it with
Decibel, Oracle Workspace Manager, Neon, OrpheusDB, Dolt and others. The
closest precedent is Decibel's hybrid storage, which also freezes a
branch's head into a shared segment when another branch forks from it.
The difference is how liveness is recorded. Decibel marks every record
live in the parent as live in the new branch. This design records only
where a row is *hidden*, which branch creation never has to touch.

## 1. The current model and the problem

The tables today (the new DDL is in [Appendix B](#appendix-b-draft-schema)):

| Table | Holds |
|---|---|
| `worktree` | One tracked `(origin, branch)`: its HEAD commit, default file, and `family_id`, the worktree whose `version_seq` row it draws versions from. Nothing creates forks or drafts yet, so every worktree is its own family. |
| `file` | One row per file per worktree: format, last-touching commit, `source_oid` (the blob its records were parsed from), and `deleted` (a removal the database owes the disk). |
| `record` | One row per record per worktree, plus at most one conflict row. Foreign key `(worktree_id, file_path)` to `file`. |
| `alias`, `txn`, `version_seq` | Alternative addresses per record row; the audit trail of batch writes; each family's version counter. |

A `record` row is the database's view of one record in one worktree:

- **Committed or in flight.** `commit_id` is set on a committed row and
  NULL on one in flight. An edit through the CRUD API overwrites the
  committed row in place and remembers its base in `base_commit_id`
  (`UPDATE_RECORD` in `db/tx.rs`). A row taken in from a file that
  differs from HEAD carries the file's last-touching commit
  (`sync.rs`), so it isn't a pending edit.
- **Conflict rows.** When the file and the database disagree about an
  in-flight record, a second row with `conflict` set holds the file's
  value, until someone resolves it.
- **`version`.** Drawn from the family's counter on every content
  change. It is both the optimistic-concurrency token
  (`CommitRef::Pending`) and the `list_changes` cursor, and a commit
  doesn't change it.
- **`id`.** Reaches clients as `unfurl.server.id`.

**Scan** (`update_from_working_dir`) compares each tracked file's blob with
`file.source_oid`, parses what changed and upserts the records. In-flight
rows get a three-way classification against the file at their base commit
(`read_base_docs`), and commit attribution is refreshed (`reattribute`).
The scan never looks at history, which is why a rebased or force-pushed
branch rescans correctly today.

**Commit** (`commit_repository`) scans, renders in-flight rows into the
files, commits them with gix, and then runs `roll_forward`. That stamps
the committed files' rows with the new commit, purges their tombstones and
stamps the outstanding `txn` rows.

**The problem.** Every worktree holds a complete copy of its records:
- A second branch of the same cloudmap duplicates every record and parses
  every file.
- Forks and users' branches multiply that.
- Cross-worktree search, which `WorktreeFilter` already supports, returns
  one copy per worktree.

Measured on the dev server's cloudmap: 138 records, with JSON averaging
1,457 bytes of a 1,658-byte row. Nearly all of a copy's cost is record
content, so moving narrower rows around doesn't help much. The versions
themselves have to be shared.

## 2. Goals and non-goals

Goals:

1. A version of a record is stored once, however many branches and forks
   can see it.
2. Creating a branch or fork copies no records.
3. **Layered reads.** A read can combine branches: main plus a user's
   branch that isn't in git yet, or a public branch plus private record
   data.
   - Each upper branch contributes its own changes on top of the one
     below.
   - A record held by more than one of them, matched by `(path, key)`,
     comes back once per branch. The client or server merges the copies
     into one record, or treats them as conflicting, depending on their
     content.
   - A user's branch can live a long time, and can later be published to
     git as a real branch for a pull request.
   - Stacks can be any depth, and can include independent branches that
     work like separate data sources.
4. Reads stay one SQL statement built from equality joins. The JSON GIN
   indexes still drive `find`, and the facet queries gain no inequality
   joins. An inequality join is what set off Postgres JIT on the facet
   queries before.
5. The same schema and statements work on SQLite and Postgres.
6. Git stays the source of truth. Any worktree's committed state can be
   rebuilt from its tree, whatever happened to its history.
7. Today's semantics survive:
   - OCC tokens;
   - `list_changes`;
   - conflict detection and resolution;
   - `txn` rollups. A **rollup** is the section of the message of a
     commit git-sync makes that lists the batches of writes the commit
     carries: each batch's version range, branch, time, author and
     message, and each record it changed (version, `M` or `D`, path and
     key). `parse_commit_rollup` reads it back, so the commit message is
     the only durable record of who wrote what
     ([§4.4](#44-commit)). A commit made outside git-sync has no rollup;
   - version families;
   - `unfurl.server.id` stable across edits and commits.

Non-goals:

- **Mirroring git's commit graph.** Segments exist where tracked
  worktrees diverge, not at every commit, branch point or merge.
- **Reading arbitrary historical commits.** Only tracked heads and their
  drafts are readable.
- **Sharing between unrelated repositories.**

## 3. Model

### 3.1 Segments are states, not commit ranges

**A segment's rows are the difference between the tree at its head
commit and the state of its parent segment.** That is the whole
definition. It doesn't depend on which commits lie between the two, so
any placement of a segment is correct, and placement only decides how
much is shared. That makes forks, splits, merges and rebases correct by
construction: each of them computes a segment's rows as a diff of trees.

A segment is one of three kinds:

| Kind | Belongs to | Holds | Changes |
|---|---|---|---|
| **head** | exactly one worktree | the committed state up to the worktree's HEAD | new rows as the branch moves forward |
| **internal** | every worktree whose chain passes through it | the committed state up to a fork point | frozen; only a split or compaction rewrites it |
| **draft** | exactly one worktree | its uncommitted changes: CRUD edits, and files that differ from HEAD | every uncommitted write |

A root segment has no parent. In phase 1 each segment has at most one
parent, so segments form a tree and merges are handled as content
([§4.9](#49-merges)).

### 3.2 Views

A read sees a **view**, which is a set of segments:

- **W's view, `S(W)`,** is W's *committed chain* plus W's own draft. The
  chain is W's head and every ancestor of the head.
- **A layered view** stacks worktrees: a base W0, then W1 … Wn. It's
  W0's whole view plus each upper worktree's **own part**: its draft and
  the segments of its chain that it didn't inherit when it was forked
  ([§4.12](#412-layered-reads-user-branches-and-private-overlays)).

Committed chains are stored in `worktree_segment`. An `inherited` flag
marks the part of each chain that a worktree was forked with. Drafts
aren't stored there, because each read names its worktrees. Segments
exist only where tracked worktrees diverge, so a chain is tens of rows. A
read turns its view into a small CTE
([C.1](#c1-the-view-and-the-visibility-test)).

- **I1.** A committed chain is closed under parents: every ancestor of a
  segment in the chain is in the chain.

### 3.3 Visibility through supersession

When a row for key *k* is written into segment *c*, the row for *k* that
was visible below *c* gets an entry `superseded(record_id, c)`. What
"below *c*" means depends on the kind of *c*:

| Segment *c* | Below *c* |
|---|---|
| a head | W's committed chain without the head |
| W's draft | W's committed chain, plus, for a write made through a layered view, whatever that view shows ([§4.2](#42-uncommitted-writes)) |
| a split prefix | its parent chain |

A row *r* with `conflict IS NULL` is **visible** in a view when its
segment is in the view and no `superseded(r, c)` has *c* in the view.

- **I2.** Take any row *h* in segment *c*, and any row *r* for the same
  key that is visible directly below *c*. Then `superseded(r, c)` is
  recorded.
- **I3.** In a worktree's own view, each key has at most one visible row.
  This follows from I1 and I2.

Why I3 follows: W's view is a chain, its committed chain and then its
draft. Take two rows for *k* in the view, *r1* in *s1* and *r2* in *s2*,
with *s1* below *s2*. When *r2* was written, the row visible below *s2*
was either *r1*, or a row in some segment between the two that was itself
written over *r1*, and so on down the chain. Every segment on that path
is in the view by I1, so *r1* is superseded in the view.

**A layered view can show a record more than once.** That happens when a
lower worktree changed the record after an upper one forked and the upper
one had changed it too, or when independent branches both hold it. The
read returns every copy, and the client or server decides whether they
merge or conflict ([§4.12](#412-layered-reads-user-branches-and-private-overlays)).

This is Postgres MVCC with one change. There, a tuple's own `xmin` and
`xmax` decide its visibility against a snapshot. Here the snapshot is the
view, and `superseded` is a set-valued `xmax`, because a row can be
replaced in several branches independently. Oracle Workspace Manager's
`NEXTVER` column plays a similar role ([Appendix A](#appendix-a-prior-art)).

**A replacement with the same content takes over entries.** When a row
replaces the row a view showed for its key, in a write, a scan, a fold or
a rebuild, and the two hold the same content, the new row gets the other
worktrees' entries on the old one. An edit made over that content was
made over the new row too. It takes them only from the row shown just
before: an older row with the same content predates changes the edit may
have seen since. It matters most for tombstones, which a later delete
or a rebuild re-creates while the record stays absent.

**A deletion leaves a tombstone while another worktree's draft holds the
key.** A scan, a fold and a rebuild would otherwise just drop the row,
when nothing below it needs hiding. Then a layered worktree that edited
the record couldn't tell a deletion made after its edit, which is a
conflict, from one it saw and edited over, which isn't
([§4.12](#412-layered-reads-user-branches-and-private-overlays)): with
the tombstone, the second supersedes it and the first doesn't.

**An entry names the edit that made it.** `superseded(r, c, key_id)`
records which record's version in *c* supersedes *r*. Visibility ignores
it: a row is hidden in a view when any entry's segment is in the view.
It says which entries belong to which edit, since a draft holds several
and moves take them to other keys ([§3.5](#35-record-identity)):
- **An edit's entries go with it.**
  - When it follows its record to another file, they stay.
  - When it's withdrawn, they go, except at a key another draft row of
    the same record holds.
  - When it can't follow and becomes a new record, its old record's
    entries go. So do other drafts' entries on it made by edits of the
    old record, once it has moved from where they saw it.
  - When an edit of the same record at another key replaces it, they
    pass to that edit, except on its own tree's rows of other records
    at the key it leaves. Those show again.
  - When a draft row changes record, as a row taken in from the file
    does ([§4.3](#43-scan-moving-forward)), they're retagged, except at
    a key another row of the old record holds.
- **A write joins the entries at its key.** It adds its `key_id` to the
  draft's existing entries there, since what it wrote over relies on
  them. The exception is a live row of a record the draft edits at
  another key: the merge hid that from the write
  ([§4.12](#412-layered-reads-user-branches-and-private-overlays)).
- **Publishing drops entries on main's rows** at keys where the draft
  holds no edit. After publishing, main's rows are the user's own tree.
- **A fold moves them** to the head with the edit's row, whatever key
  they're at.
- **A move carries** only the entries whose `key_id` is the moved
  record's: another draft that settled that record at the old key while
  editing a different one keeps showing its own edit.

Without it, each of those operations has to guess from the key which
entries were the edit's, and the model test found a lost record for
every guess.

**Stale entries are harmless.** An entry only hides a row from the views
that contain the entry's segment. A draft appears only in views that
include its worktree, and a segment no worktree lists is in nobody's view.
So nothing ever has to remove an entry for correctness; compaction removes
them to save space.

### 3.4 Rows are immutable versions

A row's content never changes. An edit, a delete, or a scan that finds a
committed record changed writes a new row. The row it replaces goes one
of two ways:
- **In the same segment,** it's deleted.
- **In a segment below,** it's superseded.

A commit or a split may move a row to another segment, but never changes
its content.

**Why.** An entry supersedes one exact version. Suppose a worktree layered
over main has superseded one of main's rows, and main then replaces that
row. Deleting the old row removes the entry with it, through
`ON DELETE CASCADE`. So the layered view shows main's new version next to
the upper worktree's, and the client or server sees both. An update in
place would keep the entry and silently hide main's change.

**Cost.** Little: Postgres writes a new tuple for an update anyway. Row
ids now change on every edit, and `key_id` ([§3.5](#35-record-identity))
is the identity clients see.

### 3.5 Record identity

Today `unfurl.server.id` is the row id, and it stays stable because edits
and commits update the row in place. Rows are now immutable, so their ids
change on every edit. So every row carries a **`key_id`**: the id of the
record's first version, inherited by every later version in any segment.
`unfurl.server.id` reports the `key_id`.
- **`get_by_id` and `find_records_follow`'s `exclude` lists** resolve a
  `key_id` within a view.
- **Renames** keep the identity too. A file rename becomes rows at the
  new path that inherit the old rows' `key_id`s, plus tombstones at the
  old path.
- **Moves.** A record that leaves one file as the same `(path, key)`
  arrives in another, in one scan, moved: the new row keeps its
  `key_id`. Otherwise an added record gets a new one. Two files holding
  the same `(path, key)` at once hold two records, which never share a
  `key_id`. A scan pairs them with a hash join over the keys it removed
  and added, so the cost is negligible.
  - **The moved row takes over entries** on the row it replaces, when
    the content is the same, but only from drafts holding an edit of
    that record. A draft editing another record at the old key doesn't
    follow it.
  - **A pending edit follows its record** to the new file, with its
    conflict row, after a scan or a rebuild. At publishing it stays put
    when the record now at its key is one it settled: another record's
    row its writes superseded there, recorded on the edit. Otherwise
    leaving it would hide a record it never saw.
- **The rollup names every record whose value the commit changes,**
  with its `key_id` and file, after the quoted key
  (`* 22 M "/repositories" "git://…" 1041 "cloudmap.yaml"`), where the
  grammar already allowed commentary, so older parsers are unaffected.
  A batch's records stay under its entry; the rest (a hand edit taken in,
  a single-record write, the file's side of a conflict) go in a block of
  their own with its own count trailer. A scan reads a new row's id from
  there, through a merge's parents ([§4.9](#49-merges)), and a split or
  rebuild reads the id a record had at an older commit
  ([§4.7](#47-splitting-a-segment)). A commit made outside git-sync has
  no rollup; then the id comes from the rows the database has, which can
  miss a record deleted and re-created after that commit.
  - **Ids are one database's.** A `key_id` is a row id, so a rollup names
    the database it came from (`Git-Sync-Database`, generated when the
    database is created), and a reader takes its ids only when that's
    its own. Another database hosting the same repository, or a fork
    served from one, would otherwise hand it ids that collide with its
    own. A rollup from elsewhere counts as no rollup.

### 3.6 Files

`file` stays per worktree. There are few files, a fork copies them, and a
rebuild recomputes them from the tree. Two things change:

- **A new `committed_oid` column:** the blob at W's head commit whose
  records the committed segments hold. `source_oid` keeps its meaning,
  the blob W's view of the file was parsed from. The two differ when the
  file is dirty on disk.
- **`record`'s foreign key to `file` is dropped.** A record in a shared
  segment belongs to many worktrees' files. It's replaced by an
  invariant:
  - **I4.** W's `file` table has a row for every file that a record
    visible in W's own view belongs to.

## 4. Operations

### 4.1 Reads

A read names one worktree, or a stack of them for a layered view. It
builds the view as a CTE, then does three things:
1. Joins the view.
2. Anti-joins `superseded` against the view.
3. Filters out conflict rows, and tombstones unless the caller asks for
   them, as today.

A layered read also reports, for each row, which worktree it came from,
and counts the copies of each record it returns, matched by
`(path, key)`.
Cross-worktree reads build one view per worktree a `WorktreeFilter`
matches, and return one row per (worktree, visible record), as they do
today. See [Appendix C](#appendix-c-example-queries).

**Counting.** Aggregations, such as the facet endpoint's totals and group
counts ([C.5](#c5-facets)), count distinct visible rows:
- **In one worktree,** that's one row per record, as today.
- **Across worktrees,** a row shared by several of them counts once. That
  is a behaviour change ([§6](#6-behaviour-changes)).
- **In a layered view,** records are counted by `(path, key)`. A record
  held by two layers counts once in each group its copies land in, the
  way an array-valued field does.

**`since_version`,** the filter on `find` and facets, means what a
`list_changes` cursor means ([§4.10](#410-list_changes)): a newer
`version`, or a segment that joined the chain later.

**Types are resolved in the query.** Two things depend on the `/types`
section's `extends` lists:
- **`?type=T`,** which matches T and every subtype;
- **the facet rollup,** which counts a subtype under its ancestors.
  It's unrelated to the commit rollup of goal 7.

Both come from the `/types` rows visible in the read's own view
([C.5](#c5-facets)). `RecordQuery` carries the type names as requested:
- **The rollup pairs** are computed inside the facet statement.
- **The `type` filter's list** is expanded by a small first query over
  the same view, then bound as a constant. The planner can't size a list
  computed inside the statement: it estimated 29 matching rows against
  6,636 in the benchmark, and rescanned the JSON index once per segment.
  The two statements run in one `REPEATABLE READ` transaction, so they
  share a snapshot.

That has several effects:
- **Nothing is cached,** and no key has to name a view, so user views
  cost nothing extra.
- **Never stale.** The expansion reads the same snapshot as the query,
  where today's cache can be a write behind.
- **Copies merge.** Copies of a type, in a layered view or in two files,
  all contribute their edges, as the cache's builder does today for
  types in two files.
- **Facets and filters agree.** A `type` facet bucket for T counts
  exactly what `?type=T` matches, because both use the same CTEs.

`extends` lists are flattened in practice: a type lists its whole
ancestor chain, itself first. For a producer that lists only direct
parents, a `WITH RECURSIVE` walk covers it, and finishes after one step
on flattened lists.

This removes the server's types cache (`CloudMapState::types_cache` and
`rollup_pairs_from`), and `section_stat`, which only that cache used.

**Status.** Done on today's schema: `RecordQuery::subtypes` expands
the names in a first query, and the facet rollup builds its pairs in
the statement (`db/record.rs`). The two statements there don't share a
transaction yet, so the expansion can miss a type written between
them.

### 4.2 Uncommitted writes

A write targets one worktree's draft. It's made through a view: the
worktree's own, or a layered view with that worktree on top. For a
create, update or delete, whether single or in a batch:

1. **Draw the version first.** `next_version` takes the family's
   `version_seq` row lock, which serialises this write with scans and
   folds in the family. Doing the lookup after that means nothing can
   move the visible rows between the lookup and the write.
2. **Look up the rows visible for the key,** both in the view the write
   is made through and in the target's own view, and run today's OCC
   checks against them. `enforce_conflict` is unchanged; in a layered
   view it checks every visible row.
3. **Delete the draft's current version,** if it has one. The OCC guard
   moves here: the delete matches only while that row's version is still
   at most the expected one. A draft holds at most one edit per record,
   so an edit of the same `key_id` at another key goes too. The new row
   keeps its base and its entries ([§3.3](#33-visibility-through-supersession)).
4. **Write the new version into the draft** ([§3.4](#34-rows-are-immutable-versions)):
   a tombstone, for a delete. It takes the `key_id` of the record it
   replaces. Its `base_commit_id` says the record was live in the view
   when the edit was written, and which committed version it was:
   - **No base when the view shows the record live nowhere,** at any
     key, copies merged by `key_id`: the write re-creates it. A draft's
     live row counts as live, the writer's own included, since an edit
     that shows the record never saw it deleted. A tombstone doesn't.
   - **Otherwise the base of the draft row it replaces,** or of the edit
     of the same record it replaces at another key (step 3).
   - **Otherwise the `commit_id` of the record's committed row** in the
     view, wherever that is: under an uncommitted edit of it, such as
     main's, or at another key main moved it to. A record only a draft
     has, never committed, has none.
   - It's the view's record, not the target's own view's, because a
     user's own view can still hold a record main has since deleted. A
     base from there would make an edit made after seeing the deletion
     look, at publishing, like a conflict with it.
   - **The base's content goes with it,** as `base_json`. The three-way
     check compares the file against that, wherever the record has
     moved since: git has no `key_id`s, so a base read back from git by
     place can find another record.
5. **Record `superseded(row, draft)`** for every other row found in step
   2. Through a layered view, that includes rows of the worktrees below,
   so a write settles a record the view was showing more than once in the
   same file. The new row takes over other worktrees' entries on any of
   them with the same content, and on the row it replaced
   ([§3.3](#33-visibility-through-supersession)).
6. **Supersede the record's other copies** the views show with content
   this write was made over: rows of the same `key_id` in another file,
   such as the old copy a user's own view still has of a record main
   moved. Only the record being edited: a row of another record the
   view shows at the key is settled, and recorded on the edit as such
   ([§3.5](#35-record-identity)).

Deleting a whole file writes a tombstone for each of its visible records
and marks the file row `deleted`, as today. Aliases are written for the
new row.

**Uncommitted writes only ever go into drafts.** The committed segments
must equal git exactly, because other worktrees may read them
([§4.3](#43-scan-moving-forward)).

### 4.3 Scan: moving forward

When the new HEAD descends from W's recorded head commit, the scan is a
fast-forward. That includes pulls that bring in merges.

The scan maintains two things: **W's head segment equals HEAD's blobs**,
and **W's draft holds whatever differs between disk and HEAD**. Today a
dirty file's rows are the database's only copy of it. Under sharing, a
branch forked at W's head commit must see HEAD's content, not W's disk.

For each tracked file:

- **Committed side.** Runs when HEAD's blob differs from `committed_oid`.
  Parse HEAD's blob. For each record that differs from W's committed
  chain:
  - if the head already has a row for that key, replace it: delete it
    and insert the new version ([§3.4](#34-rows-are-immutable-versions));
  - otherwise insert one, and record `superseded` on the row visible
    below the head (example [C.10](#c10-scan-bring-the-head-up-to-head)).

  For each record gone from the file: if something below the head is
  still visible for that key, write a tombstone into the head; otherwise
  delete the head's own row. If another worktree's draft holds the key,
  write a tombstone instead of deleting it
  ([§3.3](#33-visibility-through-supersession)).
- **Draft side.** Runs when the disk blob differs from `source_oid`, and
  follows today's in-flight handling. Parse the disk content. Where it
  differs from the committed chain, write rows into W's draft, taken in
  from the file with `base_commit_id` NULL. Where a pending edit already
  exists, today's three-way classification decides whether the edit is
  kept, a conflict row is written, or the file wins (`force`, or a
  `Git-Sync-Resolves-Version` trailer).
  - **A row taken in from the file isn't a pending edit.** It keeps the
    file's last-touching commit in `commit_id`, as today, which keeps it
    out of `LIST_PENDING_RECORDS` (`commit_id IS NULL`). Otherwise a
    second hand edit of the same record would classify as a conflict with
    the first: ours the old disk value, theirs the new one, no base.
  - **Where the disk returns to HEAD,** the draft's row for the key is
    deleted, together with the draft's entries for it, so the committed
    row shows again.
- **Parsing cost.** A clean file (disk equals HEAD) is parsed at most
  once. A dirty file is parsed twice, once for HEAD's blob and once for
  the disk content. Today it's parsed once.

**After the files, re-link W's draft.** Its entries on any head row the
scan replaced were deleted along with that row. So any row newly visible
in the committed chain, for a key the draft holds, gets
`superseded(row, draft)` and today's three-way classification
([C.11](#c11-re-link-a-worktrees-draft)).

**Worktrees layered over W need nothing.** A replaced row took their
entries with it. So wherever one of them had changed the same record, a
layered view now shows both versions.

**Record identity.** A new head row's `key_id` is, first, the id the
rollup of the commit that set its value names, through every parent
([§4.9](#49-merges)); else a record that moved from the other file in
this scan; else the record at its key; else the record the draft holds
at its key; else a new one. No two keys share one: a rollup's id another
key keeps is passed over, and so is any id a key named earlier in the
scan holds.
- **A value taken in from the file** is the record git has at its key,
  else the record the draft holds there unless git has it at another
  key, else a new one.
- **A file's value that replaces a withdrawn edit,** by `force`, a
  trailer or a resolution for the file's side, continues the edit's
  record, on the same condition.

**Edits that follow their records leave files behind.** When a pending
edit moves to its record's new key ([§3.5](#35-record-identity)), the
file it left is reconciled too, though its blob didn't change: the key
may need the file's own value back.

**A version a merge brings back** is a row with old content. In main,
which users' drafts layer over, it takes other drafts' entries from an
earlier row with that content at its key, made by an edit at that key
or of its record, and from an edit whose base is that version. Where
those rows are gone, a layered user sees it as a copy, the re-created
row over-report ([§10](#10-verification-plan)).
- **An edit's base is found by content:** its `base_json` against the
  row's. `base_commit_id` names a commit, and the row a merge brings
  back has a new one.

**Commit attribution.** New head rows take the file's last-touching
commit, as today. Rows that didn't change keep theirs
([§6](#6-behaviour-changes)). `file.commit_id` is refreshed as today.

If HEAD does not descend from the recorded commit, or the recorded commit
no longer exists, the scan rebuilds instead ([§4.8](#48-rebuilding-after-a-rewrite)).

### 4.4 Commit

The outer flow of `commit_repository` stays as it is:
1. Scan.
2. Render W's view into the dirty files.
3. Build the rollup message.
4. Make the gix commit.

**The rollup names what the fold will carry,** read from W's draft
before the fold, with the fold's own selection: draft rows of the
committed files below the version watermark, at no conflicted key, whose
value differs from the committed chain's. A conflicted key's conflict row
is the file's value, which the commit carries too; it's named with the
id its head row will take, the chain's live record at the key, else the
draft's. A batch's records are those whose version is in its range. With
family-wide versions and shared segments, a bare version range could
include rows another worktree drew, so nothing reads one. The head rows
the commit then brings in take the ids its rollup names first, as any
scan does ([§4.3](#43-scan-moving-forward)): otherwise a file's side of
a conflict, carried to a key of another file where the same record
left, would pair as a move and take that record's id.

Then the **fold** replaces `roll_forward` ([C.12](#c12-commit-fold-the-draft-into-the-head)),
keeping its order and its guards:

1. **Carried rows.** These are draft rows of the committed files that
   have no conflict sibling: today's `HAS_CONFLICT_SIBLING` test. For
   each one, delete the head's own row for that key if it has one, then
   move the draft row into the head:
   - `segment_id` becomes the head;
   - `commit_id` becomes the new commit;
   - `base_commit_id` becomes NULL.

   The rows move rather than copy, so their `id`, `key_id`, `version` and
   content are preserved. A `Pending(v)` token a client holds stays valid
   across the commit, as today.
2. **Entries.** `superseded(r, draft)` entries for the moved keys, and
   a moved edit's own entries at any other key, become
   `superseded(r, head)`.
3. **Tombstones.** A moved tombstone that hides nothing below the head is
   deleted, unless another worktree's draft holds the key
   ([§3.3](#33-visibility-through-supersession)). One that does hide
   something stays, as a committed tombstone (a behaviour change: today
   every tombstone is purged). A moved row takes over the entries on
   the committed row it replaces, in the head or below, when the content
   is the same.
4. **Conflict rows.** The commit carries the file's value, which is the
   conflict row's value, so the head gets a committed row with that
   value (or a committed tombstone, for the tombstone-shaped kind), which
   supersedes what's below it and is re-linked. The conflict row itself
   stays in the draft, stamped with the commit, as today, because a
   divergence outlives the commit and only resolution ends it. The
   pending row it shadows stays in the draft too.
   - The head's row is new, so a layered worktree that edited over the
     file's value while it was in the draft sees it again as a copy, and
     at publishing as a conflict. That over-reports; it never hides one.
5. **Bookkeeping.** Update `file.committed_oid` and `file.commit_id` and
   `worktree.commit_id`, and stamp the `txn` rows.

It all happens in one transaction, as today.

Worktrees layered over W need nothing from the fold. It moves rows
without changing their ids or content, so their entries stay valid.

### 4.5 Creating a branch or fork

To create worktree C at commit *c*:

1. **Place *c*** ([§4.6](#46-placement)) to find the base segment.
2. **If the base is W's open head, ending exactly at *c*, close it.** It
   becomes internal, and W gets a new empty head with the old one as its
   parent. This is Decibel's branch operation. An empty head doesn't need
   to close: C branches from its parent, and W keeps it. So repeated forks
   from the same commit don't lengthen W's chain. Empty means no rows and
   no `superseded` entries: a head whose rows a scan deleted can still
   hide rows below it.
3. **Give C an empty head and an empty draft.** C's head's parent is the
   base. C's `worktree_segment` rows are the base's chain, marked
   `inherited`, plus its own head. Its `file` rows are copied from W when
   *c* is W's head commit, and computed from the tree at *c* otherwise.
4. **Fill C's head when *c* isn't the base's head commit** (a merge-base
   placement). The head holds the difference between the tree at *c* and
   the base's state, parsing only the files that differ between the two
   commits.
5. **A fork of another origin joins W's family.** It shares W's rows, so
   their versions must come from one counter.

**Cost:** one row per segment in the chain, plus one per file, plus the
differences in case 4. See [C.13](#c13-fork-at-ws-head).

C doesn't see W's draft, because a branch starts from a commit, not from
uncommitted edits.

**A branch that isn't in git yet is created the same way.** It has no
checkout until it's published, so its head stays empty and its draft
holds its changes ([§4.12](#412-layered-reads-user-branches-and-private-overlays)).
For any other branch, the caller creates the checkout on disk, as today,
with a clone or `git worktree add`.

### 4.6 Placement

Given a commit *c*, either a new branch's head or a rebuilt worktree's new
HEAD, the base segment is found as follows:

1. **If *c* is some segment's head commit,** use that segment, closing it
   if it's an open head ([§4.5](#45-creating-a-branch-or-fork)).
2. **If *c* is an ancestor of some tracked worktree's head,** walk that
   worktree's chain down from its head. The segment that contains *c* is
   the one where *c* is an ancestor of its head commit and not of its
   parent's. Split it at *c* ([§4.7](#47-splitting-a-segment)).

   If several worktrees qualify, prefer one whose first-parent history
   contains *c*. That way a commit on a merged-in feature branch splits
   the feature's segment rather than main's. Splitting main's would still
   be correct, because segments are states, but it would share less.
3. **Otherwise,** compute the merge-base of *c* with each tracked
   worktree's head, take the deepest, and place that commit (cases 1
   and 2). The new head then holds the difference between the tree at *c*
   and that state.

**Cost:** one gix merge-base per tracked worktree, done outside the
database transaction.

### 4.7 Splitting a segment

Splitting S (head commit *h*, parent P) at commit *c* works as follows:

- **Segments.** S keeps its id and becomes the prefix, ending at *c*. A
  new segment S2, ending at *h*, becomes S's only child, and S's former
  children move under S2.
- **Chains.** Every worktree with S in its chain gets S2 too, with S's
  `added_version` and `inherited` flag, because nothing it sees changes.
  If S was an open head, S2 becomes its owner's head and S becomes
  internal.
- **Rows.** Only keys in files that differ between *c* and *h* can
  differ, so take a git tree diff from *c* to *h* and parse those files
  at *c*. Add the keys of S's rows written after *c*, by `commit_id`,
  even where git's value is the same at both: a tombstone kept for
  another draft's key ([§3.3](#33-visibility-through-supersession))
  changes nothing in git, but it records a deletion made after *c*, and
  left in S it would read as older than the edits it's newer than. For
  each such key *k*, compare three values: *v_c* at *c*, *v_h* (S's row,
  if it has one), and *v_p*, the value visible below S in the database.
  Where a scan at *c* would have skipped *k* (validation rejected it,
  its section or its file), *v_c* is the row that scan kept: S's own if
  written by *c*, else *v_p*. With S holding no row and *v_c* = *v_p*,
  S2 just takes S's entries for *k*:
  - **When *v_c* = *v_h*** and S's row, if any, predates *c*, nothing
    moves. A row written after *c* is handled as below.
  - **When *v_c* ≠ *v_h* and S has a row, or S's row was written after
    *c*,** move that row to S2, keeping
    its `id`, `key_id`, `version` and content. Then:
    - if *v_c* ≠ *v_p*, insert a new row with *v_c* into S (a tombstone
      if *k* is absent at *c*) and record `superseded(new, S2)`;
    - otherwise *k* didn't change before *c*, so rewrite the entries
      `superseded(x, S)` for *k*'s rows below S to `superseded(x, S2)`.
  - **When *v_c* ≠ *v_p* but S has no row,** *k* changed before *c* and
    changed back after it, or was added before *c* and deleted after it.
    Insert *v_c* into S, superseding what's below, and a row in S2
    restoring git's value at *h* that supersedes S's new row: a copy of
    the row below when that's its value, or else a new row, a tombstone
    if *k* is absent at *h*.
    - Not *v_p*: S can hide the row below with an entry alone, and then
      its value at *h* isn't *v_p*. That happens after a fold deletes a
      tombstone the child had superseded, leaving the child's entry on
      the row beneath it.
  - **The restored row stands for an older version** than anything a
    segment outside S and its ancestors holds for *k*. So every such
    segment supersedes it, drafts and layered worktrees included.
    Otherwise a draft that edited the key, whether over the row below or
    over its absence, would see the restored row appear beside its
    edit. A restored deletion that isn't the row below is no exception:
    a draft that held the key when S deleted it would have kept a
    tombstone ([§3.3](#33-visibility-through-supersession)), so drafts
    holding it now edited after.
- **New rows inherit identity.** No two keys of the tree at *c* share a
  `key_id`: rows the split keeps hold theirs, and each id is given once.
  The rows it re-creates take ids by the strongest evidence first,
  across all of them before the next kind:
  1. the id the rollup of the commit that set the value names, through
     every parent ([§3.5](#35-record-identity));
  2. S's row in the other file written after *c* with the value at *c*:
     the record moved;
  3. S's row at the key, if the key held a record at every commit since
     *c*;
  4. the row below, if it's live, the key held a record at every commit
     from its commit to *c*, and its content isn't at another key at *c*,
     where the record moved;
  5. else a new id.

  Where one kind names the same id for two keys, the first key takes it.
  Rollups name what the committing worktree gave a record, which this one
  may not have: after a merge brought a record's old content to a key
  another of its records held, the scan gave it a new id. And a moved
  row's id is only as good as the scan that wrote it. The split can't
  tell these apart from the rows it has, the same loss as a commit made
  outside git-sync ([§4.9](#49-merges)).
  Their `commit_id` is the file's last-touching commit at *c*.
- **Drafts are unaffected.** Draft entries reference row ids, not segment
  ids, so every draft's entries survive the split, and every existing
  view is unchanged.
- **Re-created rows lose their entries.** The split rebuilds *v_c* from
  git as a new row. An entry on the original row, which may since have
  been replaced, can't reach it. So a layered worktree that edited over
  that content can see it again as a copy, if main's history is later
  rewritten back to it. Such copies over-report a conflict; they never
  hide one. The same holds for any re-created row: a file row taken in
  again after its edit was withdrawn, or a version a merge brings back.
  Rows don't record what was seen on them once they're replaced, so a
  user who edited over that content, and whose entry went with the row it
  was on, sees it again. Writing over it, their edit settles its record,
  and publishing then leaves the edit where it is though its own record
  has moved. Recording seen content per draft would close this, at the
  cost of a second supersession mechanism, by content, beside the one by
  row; the case is too rare to pay for it.
- ***v_p* comes from the database, not from git.** That's what lets
  compaction delete rows without breaking a later split ([§4.13](#413-compaction-and-garbage-collection)).
  The split already parses those files at *h* for the diff, so the
  restored row reads its value from there.

**Cost:** one parse per file changed between *c* and *h*, and writes only
for keys whose value differs. A cloudmap is essentially one file, so
that's one parse.

### 4.8 Rebuilding after a rewrite

A rebuild runs when HEAD doesn't descend from W's recorded head commit,
or that commit is missing: after a rebase, reset, squash or force-push.
It's also how a user's branch is rebased before it's published
([§4.12](#412-layered-reads-user-branches-and-private-overlays)).

**A missing recorded commit is refused when the rebuild loses records.**
The repository lacking it at all usually means a new clone of a remote
that never got git-sync's commits, not a rewrite. Rebuilding then
replaces records those commits hold with HEAD's, and compaction deletes
them. So the rebuild runs in its transaction, compares W's committed
view before and after, and when a record committed in a commit the
repository doesn't have is gone or changed, rolls back and fails with
`CommitMissing`. A record upstream changed, which the remote has
moved on since, isn't lost. A rebuild that loses nothing, as when
the commits reached the remote as other commits, goes ahead, and
`ScanOptions::rebuild_missing` lets the caller accept the loss.

The error carries a `since_version` whose read includes the records it
would lose: one below the `Git-Sync-Next-Version` of the nearest commit
in HEAD's history this database made. It isn't exact: it also returns
pending edits and anything else written since.

A deletion the missing commits made isn't counted: a record they
deleted that HEAD still has comes back, and the comparison sees only a
record HEAD has that the view didn't, which a new upstream record
looks like too.

1. **Place the new HEAD *n*** ([§4.6](#46-placement)). After
   `git rebase main`, the base is typically main's head.
2. **Build a new head H′** whose parent is the base. It holds the
   difference between the tree at *n* and the base's state, parsing only
   the files that differ.
   - A record W showed that's absent at *n* gets a tombstone in H′ while
     another worktree's draft holds the key, even when the base has
     nothing to hide ([§3.3](#33-visibility-through-supersession)).
   - A new row with the same content as the row W showed takes over its
     entries. Not from the base's rows: those are older, and an edit
     made since may have seen a change in between.
   - A new row keeps the `key_id` its commit's rollup names. Without one,
     the base's record at that key, else W's, provided the base doesn't
     already use that id elsewhere in the new tree. Pending edits then
     follow their records ([§3.5](#35-record-identity)).
3. **Replace W's committed chain** with the base's chain (marked
   `inherited`) plus H′. The draft stays. Bump `worktree.reset_version`
   ([§4.10](#410-list_changes)).
4. **Recompute the file rows'** `committed_oid` from the tree at *n*.
5. **Re-derive W's draft entries, then classify.** Drop the draft's
   entries on the committed chain's rows at keys where the draft holds
   no row. Made on W's old chain, or carried onto another line's row when
   a record moved there (§3.5), they'd hide rows W's working tree still
   has. Then re-link ([C.11](#c11-re-link-a-worktrees-draft)), which
   derives the ones the draft still needs, a client edit's hidden copy of
   a moved record included. A draft row whose draft
   already supersedes the row now visible for its key needs no check.
   Rows never change (§3.4), so that entry proves the edit was made on
   top of this exact version. Only the other rows get today's three-way
   classification, with the same caveat as today. A base commit that's
   gone, after a fresh clone of a force-pushed remote, reads as diverged,
   so conflicts are over-reported, never lost.
6. **Leave W's old exclusive segments** to compaction; they're no longer
   in any chain.

History is never replayed, so every kind of rewrite takes this same path.
It's also an optimisation available on any HEAD move: if the new HEAD is
another tracked worktree's head commit, a rebuild shares everything,
where a fast-forward would copy the difference.

**Worktrees layered over W keep their own parts.** That's what the
`inherited` flag is for: a layer's contribution doesn't depend on W's old
history. So W's rewrite shows through as conflicts only on the records
the layer changed.

### 4.9 Merges

**Phase 1 handles merges as content.** A merge commit that arrives by
fast-forward goes through [§4.3](#43-scan-moving-forward): the head gets a
row for every record where the merged tree differs from W's committed
chain. That's correct for any history shape. A merge segment would add
sharing, not correctness.

**Ids through merges.** Merges are made outside the database, as regular,
octopus or rebase merges of a published branch, and reach W by a
fast-forward scan. A record keeps its `key_id` through them by the rollup
of a commit that set its value, when it's this database's
([§3.5](#35-record-identity)), found by walking back through parents
with the same value, the first parent before the others, to the first
such commit git-sync made: the merged branch's rollups are reachable
through its parent. A squash or an outside commit that set the same value
doesn't end the walk while another parent reaches a rollup. A rebase merge copies
each commit's message, and so its rollup. Where no rollup names the
record the id comes from the rows, which can miss its identity:
- a squash merge, which keeps no rollups;
- a value set in the merge commit itself, or by a commit made outside
  git-sync;
- a rollup's id another key of the tree already holds, which the scan
  passes over.

**Merges bring old versions back.** A value a merge restores may be one
a user's layered edit was made over, whose row a rewrite has since
collected. The draft's entry went with that row, so the user sees the
version again beside their edit: the re-created-row over-report of
[§4.7](#47-splitting-a-segment).

The cost is duplication. The merged-in branch's changes are stored twice
while both branches are live. Once the merged branch is deleted, its
segments are collected and one copy remains. A fork that keeps pulling
from upstream accumulates copies of upstream's changes. Rebasing the fork
onto upstream restores the sharing ([§4.8](#48-rebuilding-after-a-rewrite)).

**Phase 2 adds merge segments,** only if measurement shows that
duplication matters:
- **Parents.** A segment can have several parents, so `segment_parent`
  replaces `parent_id`, and a committed chain becomes a DAG closed under
  parents.
- **Rows.** A merge segment M holds a row for *k* exactly when either:
  - more than one row for *k* is visible across its parents: value
    against value, value against tombstone, or identical edits made on
    both sides; or
  - the merge commit's value differs from the one visible row.

  That row supersedes every one of them. The first parent's `key_id` wins.
- **Correctness.** I3's proof carries over, because every path between
  two versions passes through segments in the view.
- **`list_changes`.** `added_version` covers the rows that become visible
  when the other parent's segments join the chain.

### 4.10 `list_changes`

A view's changes since a cursor are its visible rows whose `version` is
past the cursor, or whose segment joined the chain after the cursor
(`worktree_segment.added_version`; a draft's rows count as always
present). A layered view's changes are the union of its worktrees'
parts. The `since_version` filter on `find` and facets follows the same
rule.
- **I5.** Any operation that makes an older row visible again must either
  write a new row version or bump `worktree.reset_version`.
- **Reset.** A cursor below `reset_version` gets a new `Reset` outcome,
  and the client re-reads. A layered view resets when any of its
  worktrees does.

What each operation does under I5:

| Operation | Under I5 |
|---|---|
| Rebuild, including rebasing a user's branch | Bumps `reset_version`. A segment left the chain, so records only it added vanish without a tombstone. |
| Discarding an edit in a worktree's own draft | Writes the committed value into the draft as a new version instead, or bumps. Overwriting from the file (`force`, the trailer) already writes a new version, as today. |
| Discarding an edit in a layered worktree | Deletes the draft row and the draft's entries for the key, and bumps. Writing main's value back instead would later show as a second copy whenever main changed the record again. |
| Fork, split, compaction | Nothing: every existing view keeps its visible set. |
| Phase-2 merges | Covered by `added_version`. |

Replacing a row is never a problem: the replacement is a new row with a
new version.

**Over a layered view, `list_changes` returns every visible copy of
each record it reports,** matched by `(path, key)`. Otherwise, when main
replaces a row that a user's branch had superseded, only main's new row
would be past the cursor. A client keeping the latest version per key
would then overwrite the user's version, and a copy count taken over the
returned rows would be wrong ([C.6](#c6-list_changes)). The same goes for
`since = None`, which returns the drafts of every worktree in the view,
main's included.

Committed tombstones now persist while they hide something below them.
So a committed delete still reaches a client that polls late; today's
purge can hide it.

`since = None`, meaning everything in flight, returns the rows of the
view's drafts.

### 4.11 Conflicts

There are now two kinds:

- **A worktree's own conflicts** are between its file and its database
  view. They mean what they mean today, and they're held in conflict
  rows in its draft, one per `(draft, file_path, path, key)` as before.
  Conflict rows are never visible and never supersede anything. For a
  clean file, the file's value is now also the committed row.
  Collapsing conflict rows into state on the draft row is a possible
  later simplification ([§9](#9-open-questions)). Dirty files still need
  somewhere to hold the disk value, and that's the conflict row.
- **Copies in a layered view** aren't conflicts as far as git-sync is
  concerned ([§4.12](#412-layered-reads-user-branches-and-private-overlays)).
  The read returns every copy, and the client or server merges them or
  treats them as conflicting, from their content. A write through the
  view settles them. When the upper worktree is rebased or published,
  copies it still holds in the same file as main become conflicts of the
  first kind.

### 4.12 Layered reads: user branches and private overlays

A layered view stacks worktrees. There are two uses so far:

- **User branches.** A `POST /cloudmap` from a user without write
  permission goes to that user's branch of main, created on their first
  write.
  - **What it is:** an ordinary branch that isn't in git yet. It's a fork
    of main ([§4.5](#45-creating-a-branch-or-fork)) with no checkout and
    no commits of its own, and its draft holds the user's edits.
  - **Reads:** a `GET /cloudmap` from that user reads main with their
    branch layered on top. That's main's latest state, including main's
    own uncommitted edits, with theirs over it.
  - **Lifetime:** it may live for a long time, and may be published to
    git as a real branch for a pull request.
- **Private overlays.** Private record data is kept in a branch layered
  over the public main branch, for the readers allowed to see it.

Which worktrees a read layers, and who may write where, is the server's
decision.

**The view.** A layered view over W0, W1 … Wn is W0's whole view plus
each upper worktree's own part: its draft and the chain segments it
didn't inherit ([C.2](#c2-a-layered-view)). A user branch that hasn't
committed anything has an empty head, so its own part is in effect its
draft. Visibility is the same test as for any view, local to each row.
A branch forked from a user branch contributes only what it changed on
top of that; to see both, layer both.

**Stacks and independent branches.** A stack can be any depth. It can
also hold **independent branches**: branches that weren't forked from
anything below them and work like separate data sources.
- **Their own part is everything.** An independent branch inherits
  nothing, so its whole chain counts as its own part.
- **Shared records come back as copies.** A record it shares with a
  lower layer, matched by `(path, key)` even in a differently named file,
  comes back once per layer.
- **It joins the family of the worktrees it's layered with,** as a fork
  does. That keeps the rule the `version_seq` migration states: one
  counter covers everything that can appear in one response. So a stack
  has one `list_changes` cursor and one set of `Pending(v)` tokens, as
  today, and the branch's writers share the family's lock.
  - Only a source layered with two unrelated families would need a
    cursor per family, and nothing needs that yet.

**Copies, merged or conflicting.** An upper worktree's rows supersede the
rows it saw when it wrote them. If a lower worktree changes the same
record afterwards, its new version is a new row ([§3.4](#34-rows-are-immutable-versions)),
which nothing in the view supersedes. So the record comes back twice,
once per worktree.

Records are matched by `(path, key)`, so the same record kept in
differently named files counts too ([C.2](#c2-a-layered-view)).

git-sync doesn't decide what the copies mean. The client or server
merges them into one record, or treats them as conflicting, depending on
their content. Nothing has to detect them in the meantime:
- there's no re-linking when main moves;
- there are no conflict rows to write;
- there are no versions to compare.

**Copies of one record in different files merge by `key_id`.** When a
layered view shows rows of the same `key_id` in different files, the upper
layer's wins, and the rest are left out. That happens when an edit's
record moved in a lower worktree, or when a rewrite brought back the
record's original row after the edit was made. What a user writes
through the view is made over what this shows. Merging can defer showing
a lower worktree's change to the record, but never loses it: publishing
classifies the edit against main's current row ([step 2](#412-layered-reads-user-branches-and-private-overlays)).

**Writes settle them.** A write made through a layered view supersedes
every row the view shows at the same `(file_path, path, key)`, as well as
the row the target's own view shows ([§4.2](#42-uncommitted-writes)).
- Editing a record the view shows twice leaves one copy.
- Editing a record that main changed after the user's branch point lands
  on top of main's newer version, and also leaves one copy.
- Copies in other files are left for the client or server to merge.

**A second branch point, per record.** That last case is the one that
would otherwise need a second draft branch, because the user's edit is
based on main's state after their branch point. Here the later branch
point is recorded per record, as an entry that supersedes main's newer
row. The entry has no effect in the branch's own view, whose chain
doesn't include main's newer segment, until the branch is rebased onto
main. At that point it applies. So merging the two branch points when
committing to git is just the rebase:

**Publishing a user branch.**
1. **Rebase it onto main's head** ([§4.8](#48-rebuilding-after-a-rewrite),
   [C.15](#c15-publish-a-user-branch)). Its committed chain becomes
   main's current chain, marked `inherited`, plus an empty head. That's
   cheap, because it has no commits of its own. Each edit follows its
   record, by `key_id`, to the file main has it in, with its conflict
   row, unless it settled the record now at its key
   ([§3.5](#35-record-identity)). All edits move before any is
   classified, since moving or renewing one changes the entries the
   others are classified by.
2. **Re-link its draft** against the new chain, and classify only the
   rows whose draft doesn't already supersede the row now visible
   ([§4.8](#48-rebuilding-after-a-rewrite)). Main's row is theirs.
   - **A row the draft supersedes needs no check,** and a standing
     conflict whose file side isn't main's value any more is dropped: the
     edit is on top of main's value.
   - A record edited on top of main's newer version is already
     superseded, with no conflict. That includes an edit on top of one of
     main's uncommitted edits which main has since committed, because
     the commit moved that exact row into main's head.
   - A record that both changed, and that the user hasn't resolved,
     becomes an ordinary conflict of the first kind, to resolve before
     committing.
   - **A record main deleted conflicts with a live edit of it that has a
     base.** A conflict is an edit of a version main deleted, by someone
     who never saw it deleted. An edit written while the record was live
     nowhere has no base (§4.2 step 4), so a base means the deletion came
     after, whatever key it happened at and whatever tombstone it left.
     Main's side of the conflict is its value at the edit's key, if any.
     The record is the one the edit had before step 1: an edit renewed
     there is a new record, and the one it was made over is still live.
   - **A renewed edit adds a record,** so where main has no live value at
     its key it conflicts with nothing, whatever absence the user saw
     there.
3. **Commit it.** Create the git branch at main's head, check it out, and
   commit the draft (`commit_repository`). The pull request is then main
   plus the user's changes.

No rows are copied.

**Long-lived branches.**
- **Storage:** a user branch costs only the records it changed.
- **Main's chain grows:** it gains one segment per distinct fork point
  while such branches live, although forks at the same commit share one
  ([§4.5](#45-creating-a-branch-or-fork)).
- **Keeping it short:** rebasing idle branches onto main's head lets old
  fork points fold away ([§9](#9-open-questions)).
- **Nothing pinned:** a layered branch pins nothing in main, because its
  entries reference rows that main shows anyway.

### 4.13 Compaction and garbage collection

In the tree model, only two events leave anything to collect:

- **Deleting a worktree.** Its draft and head, and any internal segment
  no other worktree lists, lose their last reference and are deleted.
  Rows, aliases and entries cascade.
- **A rebuild,** including a user branch's rebase. The worktree's old
  exclusive segments go the same way.

Either can leave an internal segment with exactly one child. That's the
branch-deletion case, and such a pair is **folded**, unless it would
cross a fork boundary: a worktree whose chain has the parent inherited
and the child as its own part. Folding that would move inherited rows
into the worktree's own part, and a layered view would show them as its
changes. Nor is it folded while the child is an open head: the head's
writes replace its rows in place, so a later change to a key would erase
the parent's row outright rather than hide it, and with it the record
id a split at the parent's head commit recovers. The pair folds once the
head closes. The fold goes like this:
1. Keep the id of whichever side has fewer rows to move.
2. Delete the parent's rows that the child overrides. The chains that
   contain the parent all contain the child, so those rows are hidden
   everywhere.
3. Move the other rows.
4. Repoint entries that named the dropped segment to the survivor.

Compaction runs as part of those operations or in a deferred pass.
Correctness never depends on it, because stale entries are harmless.

**A family root can't be deleted while other worktrees belong to its
family.** The foreign keys from `segment.family_id` and
`worktree.family_id` to `version_seq` refuse it, and the operation
reports that. Moving the family to a surviving member would also change
the origin its rollup trailers name (`Git-Sync-Family`). Readers
rebuilding the version sequence from older commits match on that origin,
so this is left as an open question rather than done implicitly
([§9](#9-open-questions)).

**Dead rows are kept until their segment is folded.** A dead row is one
superseded in every view that contains its segment, such as a record both
branches below a fork point rewrote. A fork placed exactly at that
segment's head commit would see the row again. Folding removes that
boundary, and a fork there afterwards splits the folded segment, reading
*v_p* from the database ([§4.7](#47-splitting-a-segment)). A layered
worktree never makes a row dead: the rows it supersedes stay visible in
the lower worktree's own view.

### 4.14 Concurrency

Fork, split, fold, rebuild (including publishing a user branch) and
compaction are **structural operations**. They change segments that
other worktrees may share, and they assume the open head is exclusive.
- **They take the family lock first.** Each runs in one transaction that
  starts by taking the family's `version_seq` row lock: `SELECT … FOR
  UPDATE` on Postgres, and `BEGIN IMMEDIATE` on SQLite, which serialises
  writers anyway.
- **Writes already take that lock.** Every record write, including a
  write to a user's branch, takes it when it draws a version. So within
  a family, structural operations and writes serialise. Reads never
  lock.
- **The gix work stays outside the transaction,** as the scan's does
  today: tree diffs, merge-bases and parsing.
- **Writers re-read `head_segment_id` after taking the lock.** A fork can
  close W's head between a scan's parse and its write. A scan holding a
  head id from before the lock would then write committed rows into a
  segment that has just become internal and shared. `SyncedRepo` can go
  on caching `worktree_id` and `family_id` at open, because neither
  changes, but not the head or draft ids.

Forks and users' branches join their upstream's family, so they share
its lock ([§6](#6-behaviour-changes)).

### 4.15 Remotes: pulling, pushing and conflicts

Proposed; none of it is implemented. git-sync doesn't fetch, pull or
push today. It reacts to whatever it finds at `HEAD` and in the working
tree, so a pull or push is someone else's job, and the next scan takes
in its result ([§4.3](#43-scan-moving-forward),
[§4.8](#48-rebuilding-after-a-rewrite)).

The aim is to pull and push without ever leaving conflict markers in a
working tree. A divergence between our changes and the remote's is
handled where it is cheapest:
- **In the database,** as record conflicts, where both sides and the
  base are already known ([§4.11](#411-conflicts)).
- **In git,** when someone would rather resolve it there: the conflicting
  edits go to a branch of their own, to merge like any other.

Two building blocks come first.

**A. Branches without a checkout.** Today every tracked branch is a
`SyncedRepo` on a checkout. A branch here is a worktree row with no
checkout, whose state is its ref's commit.
- **Advancing it** uses only the committed side of the scan. When the
  ref's tip descends from `worktree.commit_id`, `head_files` and
  `advance_head` read the changed blobs from the commits, not from a
  disk. When it doesn't, it rebuilds ([§4.8](#48-rebuilding-after-a-rewrite)).
- **It has no draft writes.** Edits to it are made in git, by whoever
  resolves its conflicts.
- **It is deleted when its ref is.** A sweep over the family's
  checkout-less branches deletes each whose ref is gone
  ([C.19](#c19-delete-a-worktree)), on the main worktree's scan or
  periodically. Compaction then collects what only it used.
- **Phase 3's user branches are the starting point,** not the whole
  of it ([§4.12](#412-layered-reads-user-branches-and-private-overlays)).
  They're checkout-less worktree rows too, but with an empty head and
  only a draft, and no ref until they're published, which checks them
  out ([C.15](#c15-publish-a-user-branch)). A branch here has commits,
  follows a ref, is committed to without a checkout, and goes when its
  ref does.

**B. Moving draft rows into another worktree's head.** Nothing in
phases 1–3 does this: rows only ever move within one worktree. Here a
worktree's edits go to a branch forked for them: a copy of each into the
branch's draft, with its `key_id`, `version`, base and content, which the commit fold
([§4.4](#44-commit), [C.12](#c12-commit-fold-the-draft-into-the-head))
then carries into the branch's head, as any fold does.
- **Entries:** none are copied. Re-linking the branch's draft
  ([C.11](#c11-re-link-a-worktrees-draft)) makes it supersede what the
  branch shows at each place it holds. The source's entries at other
  places hide the record where its history moved it from, which the
  branch's needn't have done.
- **What the source does:** it resolves each conflict for the file, which
  withdraws its edit and takes the file's value in.

#### Exporting conflicts to a branch

`export_conflicts(name)` hands a worktree's record conflicts to git
([C.20](#c20-export-a-worktrees-conflicts-to-a-branch)).
1. **Pick the base:** the latest commit on `HEAD`'s first-parent line
   that every conflicted edit's `base_commit_id` descends from or is. With
   no bases (records both sides created) that's `HEAD`; where no commit of
   the line qualifies (a base a rewrite dropped), it's the line's first
   commit. A three-way merge from an older base can add conflicts, never
   hide one.
2. **Fork** a checkout-less worktree at the base
   ([§4.5](#45-creating-a-branch-or-fork), splitting the segment if the
   base falls inside one, [§4.7](#47-splitting-a-segment)), and move the
   conflicted edits into its draft. A ref or worktree of that name that
   already exists is an error; no other name is chosen. Edits that don't
   conflict stay in the source's draft, to be saved and committed there
   as usual.
3. **Render in memory.** For each file the branch's draft has edits in,
   apply them to the file's blob at the base: `apply_pending_records`
   with no conflict check, on bytes from git rather than the disk.
4. **Commit** that onto the base, with a rollup naming the moved
   batches, so who wrote what survives, and fold it into the branch's
   head (B). Nothing is checked out.

The source worktree resolves each exported conflict for the file, as
`resolve_conflict(Theirs)` does, so it's conflict-free for those records
and its view shows the file's value. On the branch, an edit whose record
the base has at another key (main moved it since) becomes a new record:
the branch's tree holds both, and merging back conflicts where they
meet. Resolving is ordinary git: merge the branch into
the source branch, fixing any textual conflicts, and the source's next
scan takes the merge in as content. It holds no pending edits for those
records any more, so nothing diverges and no `Git-Sync-Resolves-Version`
trailer is needed. The records keep their ids through the merge by the
branch's rollup, once the rollup lookup walks merges made outside the
database (phase 4); until then a record can get a new id there.

Behaviour changes:
- **The edits leave the source's view.** Tokens clients hold for them
  stop matching, and `list_changes` no longer lists them as pending. The
  export reports the branch they went to.
- **Git can report more than the database did.** It merges by line, so
  two neighbouring records can conflict textually though neither does as
  records. That over-reports; it never loses an edit.

**From `commit_repository`.** A commit can export the conflicts it finds,
as an option: `commit_repository(message, CommitOptions {
conflicts_to_branch: Some(name) })`, a change to its signature. The
export runs after the commit's scan and before its save:
1. **Scan,** as every commit does. This is what finds a hand edit's or an
   outside commit's divergences.
2. **Export** the worktree's conflicts, as above, if there are any. The
   draft then holds only edits that don't conflict.
3. **Save and commit** as usual. Neither sees the exported edits: the
   fold's selection has no rows for them, and the rollup doesn't name
   them.

It returns the new commit, if any, and the branch, if one was made.
- **A batch can be split between the two commits,** when some of its
  records conflicted and others didn't. Each commit's rollup names its
  share of the batch's records, under the batch's author and message. A
  `txn` row is stamped with one commit, so the export copies the batch's
  row to the branch's worktree, with the same author, message and
  version range, and each copy is stamped by its own worktree's commit. A
  batch whose records all went is moved rather than copied: left behind
  unstamped, it would be outstanding in the source for good, and named,
  empty, in every later rollup there.
- **`HeadMoved` is retried inside `commit_repository`,** after the
  export: it scans and commits again, without exporting again, and
  returns the first attempt's `Exported`. Retrying from outside would
  lose it, since the retry's scan finds nothing left to export. The
  server's own retry then has nothing to do for this call.
- **Without the option, nothing changes:** the conflicts stay in the
  worktree, and the commit carries the file's values as now
  ([§4.11](#411-conflicts)).

#### Pulling

1. **Fetch.** Nothing local changes.
2. **If `HEAD` can fast-forward to upstream,** move it and scan. Pending
   edits that the new commits diverge from become record conflicts, as
   for any commit made outside git-sync.
3. **If the histories have diverged,** rebase in the database rather
   than merge in git:
   1. **Unfold** the local commits, from the merge base to `HEAD`, into
      draft rows based at the merge base: the fold in reverse. Each batch
      keeps its `txn` row, unstamped, so its author and message survive
      the re-commit. A commit without a rollup is unfolded from its tree
      diff, with ids found as a scan finds them.
   2. **Save** any other pending edits first, so the working tree holds
      nothing the reset would lose.
   3. **Reset** the branch and working tree to upstream. The next scan
      rebuilds ([§4.8](#48-rebuilding-after-a-rewrite)), keeping the
      draft and classifying each edit against upstream's value: one
      upstream didn't change stays pending; one it changed becomes a
      record conflict.
   4. **Commit** what doesn't conflict onto upstream
      (`commit_repository`).
   5. **Leave the conflicts** in the database to resolve, or export them
      (above).

The result is linear history with no merge commits and no conflict
markers, and only records that truly conflict are held back. History is
rewritten, which is safe only because the commits rewritten were never
pushed.

**What isn't a record doesn't survive the unfold.** The reset drops
anything in the local commits that has no row to go into: files no
format handles, comments, ordering and layout in a document outside its
record values, and prose around a literate cloudmap's blocks. So before
unfolding, check whether any local commit since the merge base changes
something the unfold can't represent, and either:
- **divert** (the push fallback below): push local `HEAD` to a new ref and
  reset, which loses nothing; or
- **merge the rest** with gix's in-memory three-way tree merge (merge
  base, local, upstream) for everything but the records, rebase the
  records in the database as above, and commit the merged tree onto
  upstream. A text conflict there needs the diversion, or an export.

Diverting is the simpler first version.

#### Pushing

1. **Push** the branch.
2. **On a non-fast-forward rejection,** pull (above), then push again,
   up to a few times while upstream keeps moving. Records that conflict
   are exported to a branch, which is pushed under a new name.
3. **The fallback is to divert the whole push.** When the rebase can't
   run, or the retries run out, push local `HEAD` to a new remote ref
   instead. A new ref can't be rejected as a non-fast-forward. In the
   database the new branch takes over the local commits: it's a
   checkout-less fork at local `HEAD`, sharing its segments, and the
   local branch is reset to upstream and rebuilt. Pending edits stay in
   its draft, classified against upstream as above.

A rejection for any other reason (permissions, a protected branch, a
server-side hook, a name already taken) fails the push and changes
nothing locally.

**What it needs:**
- **The `git` command line** for what gix (0.89) doesn't do:
  - **push;**
  - **updating the working tree and index** for the fast-forward and the
    reset: `git merge --ff-only` and `git reset --hard`. gix's only
    checkout (`gix_worktree_state::checkout`, what a clone uses) writes
    every index entry, removes nothing the new tree dropped, and
    overwrites local changes unchecked.

  Credentials come from wherever the server's existing pushes get
  theirs. Nor does git-sync rebase, which the unfold makes unnecessary.
- **gix's `merge` feature** for the tree merge, and
  `blocking-network-client` for fetch, unless the `git` command line
  fetches too. git-sync is on gix 0.89, which has both; neither is
  enabled yet.
- **Exclusive use of the checkout** for a reset. Pulling and pushing hold
  the family lock only for their structural steps
  ([§4.14](#414-concurrency)); the network and gix work happens outside
  it, as the scan's does. A write that lands meanwhile is caught by
  `write_seq` or `HeadMoved`, as now, but the reset itself has to be
  kept from running under a render.

## 5. Performance

**Reads.**
- **The visibility check is equality joins:** `record` joined with the
  view, a CTE of tens of rows, and an anti-join of `superseded` probed on
  its primary key. Postgres estimates that from column statistics.
  That's the difference from resolving the newest version at read time,
  which needs an inequality `NOT EXISTS … depth >` or a `DISTINCT ON`
  sort, both of which the planner estimates badly.
- **`find` with a JSON filter** starts from a bitmap scan of the GIN
  index. Each candidate then costs one membership lookup and one
  anti-join probe.
- **Facets** make one pass over the view's visible rows, with a hash
  anti-join and the lateral `facet_values`. A facet column adds one
  lateral per member path, plus the type-rollup pairs when grouping by
  `type`. There's no inequality join to misestimate.
- **A `type` filter or a type rollup** adds one scan of the view's
  `/types` rows, 32 in the dev cloudmap. It's evaluated once per
  statement.
- **Point gets** use the `(file_path, path, key)` index, filtered to the
  view.
- **A layered view** adds each upper worktree's own part to the CTE,
  usually one or two segments. Flagging conflicts is a window count over
  the rows the read returns.
- **SQLite** runs the same statements. It has no JSON index today, so
  filters scan the view's rows, as they do now.
- **Cross-worktree search** produces one row per (matched worktree,
  visible record).

**Writes.**
| Operation | Cost |
|---|---|
| An edit | One insert. Plus one delete, if the draft already held a version. Plus one small supersession insert per row the view showed |
| A change to main while branches are layered over it | Nothing extra |
| Fork, including a user branch | Tens of membership rows, plus the file rows |
| Scan | Rows only for records that differ; unchanged blobs are skipped, as now |
| Commit | Moves rows (updates `segment_id`); about as many writes as `roll_forward` today |
| Publishing a user branch | Its chain's membership rows, plus a re-link of its draft |
| Split | Rows only for keys that changed between the split point and the segment's head |

**Storage.** There's one row per distinct version, plus a `superseded`
entry per replacement, instead of a full row per record per worktree. An
entry is about 90 bytes including its two index entries. The dev
cloudmap's record rows average 1,658 bytes. A user branch costs the
records it changed.

**Growth.** Rows grow with changes, not with worktrees or users. Chains
grow by one segment per live fork point, and fold back when branches are
deleted or rebased.

**Why a side table rather than an array.** OrpheusDB found appending to a
per-record version array orders of magnitude slower than inserting one
row per version ([Appendix A](#orpheusdb)). A `superseded bigint[]` on
`record` would make the predicate local to the row, but every
supersession would rewrite a ~1.6 KB row, and branches superseding the
same row would contend for its lock. SQLite would also need a second
dialect ([§9](#9-open-questions)).

**Measured.** `bench/segments/` generates a synthetic family, runs
Appendix C's queries under `EXPLAIN ANALYZE`, and runs today's layout
for comparison; its README has the results. In brief:
- no plan uses JIT;
- aggregates are the same or faster, and `list_changes` is 8× faster;
- storage is a sixth of today's, with user branches included;
- finds cost somewhat more.

It also found two rules for writing the queries:
- in a cross-worktree anti-join, the worktree match goes in `WHERE`,
  not in the join's `ON`;
- finds select ids and sort keys first, and fetch JSON only for the
  page.

## 6. Behaviour changes

1. **Record ids change on every edit,** because rows are never changed in
   place ([§3.4](#34-rows-are-immutable-versions)). `unfurl.server.id`
   reports `key_id`, so clients see no change. `get_by_id` and `exclude`
   lists resolve within a view.
2. **`commit_id` becomes per record.** A row keeps the commit that wrote
   its version, and a later commit touching the same file no longer
   re-attributes unchanged records. `CommitRef::Commit` tokens therefore
   stop being invalidated by commits that changed other records in the
   file. `file.commit_id` keeps today's meaning.
3. **Committed tombstones persist** while they hide a row below them.
   Today they're purged on commit.
4. **`list_changes` gains a `Reset` outcome.**
5. **Dirty files are parsed twice per scan:** HEAD's blob and the disk
   content.
6. **The `record` → `file` foreign key is gone.** I4 replaces it.
7. **Forks join their upstream's family** and share its `version_seq`
   lock. A busy fork and its upstream serialise their writes, where
   separate families wouldn't.
8. **The rollup reads only the committing worktree's own segments.**
9. **Writes draw their version before looking up the visible rows.**
   That holds the family lock slightly longer: one indexed lookup.
10. **Facet counts across worktrees count a shared row once.** Today each
    worktree's copy counts, so a record that main and a fork both hold,
    unchanged, counts twice.
11. **The server's types cache goes,** and `section_stat` with it.
    Subtype expansion and the facet rollup are computed in the query, so
    they're never a write behind.

Guards kept, each mapped above:
- OCC: `enforce_conflict`, and the SQL-level version guard, which moves
  from an update's `WHERE` to the delete of the replaced row.
- The three-way conflict classification and `read_base_docs`, for a
  worktree's own conflicts.
- `HAS_CONFLICT_SIBLING` in the fold.
- The tombstone purge, now limited to tombstones that hide nothing.
- The `force` option and the `Git-Sync-Resolves-Version` trailer.
- The unchanged-blob skip.
- The fold's single transaction.

## 7. Starting from scratch

Nothing has been released, so there's no migration. The schema replaces
today's, and databases are rebuilt by scanning their repositories.

## 8. Alternatives considered

| Alternative | Why not |
|---|---|
| Keep the copies (status quo) | Storage and scan cost grow with branches × records; cross-worktree search duplicates every record. |
| Share by blob: records keyed by file blob, plus a per-worktree overlay (Iceberg and Snowflake at file level) | A cloudmap is essentially one file, so any change on a branch gives it a new blob and a full copy. |
| Share by commit: a snapshot per commit | Only identical commits share, and every commit rewrites everything. |
| Resolve the newest version at read time (segments ordered by depth, newest wins) | Every read, facet and search needs an inequality anti-join or a `DISTINCT ON` sort. Estimates are poor, which is the JIT problem again, and cost grows with versions. |
| A per-branch visibility table: tuple-first liveness, or OrpheusDB-style membership | Reads stay plain, but each branch copies a membership row per record. OrpheusDB avoids per-row cost with arrays, but it versions whole datasets per commit, not live per-record edits. |
| Deduplicate the JSON by content hash, keeping per-branch rows | Removes most bytes (JSON is 88% of a row), but still one row per record per branch, and a full insert per fork. |
| Liveness bitmaps per segment (Decibel's hybrid) | Branch creation must mark every live record for the new branch. Supersession stores the complement, which branch creation doesn't touch. |
| Content-addressed trees (Dolt, Noms, lakeFS) | O(1) branches and cheap diffs, but they need their own index structures. They don't fit JSON queries served by Postgres and SQLite indexes. |
| User drafts as a special kind of segment that follows main: re-linked on every change to main, with conflicts found by comparing versions and written as conflict rows | Every change to main has to find the drafts holding its keys, and write their conflicts eagerly. Layered reads give the same result lazily: the conflict is two rows in one view. |
| A second user branch for edits made on top of main's newer versions | Works, but it takes a branch per branch point. Recording the later branch point per record gives the same result, and the rebase at publishing applies it. |
| Rows updated in place | Cheaper by one index entry, but an entry would then keep hiding a row after its content changed, so a layered view would miss the conflict. |

## 9. Open questions

Decided since the first draft:
- **A layered view's base includes its own draft,** so a user sees main's
  latest state, including edits not yet committed.
- **Stacks are arbitrary.** They can be any depth, and can include
  independent branches that work like separate data sources
  ([§4.12](#412-layered-reads-user-branches-and-private-overlays)).
- **An independent branch joins the family of the worktrees it's layered
  with,** so a stack keeps one version counter, one cursor and one set of
  tokens, as today.
- **Records are matched by `(path, key)` across a stack.** The read
  returns every copy, and the client or server merges them or treats them
  as conflicting, depending on their content.

Still open:

1. **Phase-2 merge segments ([§4.9](#49-merges)).** This depends on how
   often long-lived branches merge into each other while both stay live.
2. **A `superseded bigint[]` column on Postgres** would make the
   predicate local to the row. It's rejected for now: row rewrites, lock
   contention, and a second dialect. Revisit it if the anti-join shows up
   in `EXPLAIN`.
3. **Collapsing conflict rows into draft-row state,** for a worktree's
   own conflicts. The committed row is the file's value for clean files.
   Dirty files still need a place for the disk value.
4. **Keeping main's chain short.** Long-lived user branches pin their
   fork points in main's chain. Rebasing branches that have no commits of
   their own onto main's head, automatically, would let those fork points
   fold away. That turns duplicates into conflicts of the first kind
   early ([§4.11](#411-conflicts)).
5. **Reverting edits in a layered branch** bumps its `reset_version`
   ([§4.10](#410-list_changes)). A cheaper revert marker may be worth it
   if reverts turn out to be common.
6. **Per-record commit attribution.** Is it acceptable to every client
   that uses `CommitRef::Commit` tokens?
7. **When to run compaction:** inline with the operations that leave
   garbage, or in a background pass.
8. **Segmenting `file`** as well, if repositories come to hold many
    files.
9. **Placement preference beyond first-parent history,** when several
    tracked chains contain a commit.
10. **Deleting a family root that still has members.** Refuse it, as the
    schema does now, or move the family to a surviving member, accepting
    that the `Git-Sync-Family` trailer's origin changes for later
    commits. Giving families an identity of their own, independent of any
    worktree, would avoid the choice.
11. **Remotes ([§4.15](#415-remotes-pulling-pushing-and-conflicts)):**
    - Who triggers a pull: the server on a schedule, before each commit,
      or a client request?
    - Exported and diverted branch names: generated, or the caller's?
    - Should a pull, or a server-made commit, export its conflicts by
      default, or leave them in the database until someone asks?
    - How many push retries before diverting?
    - Unfolding a commit without a rollup finds ids as a scan does, so
      a record can come back with a new id. Is that acceptable, or does
      such a commit force the fallback?
    - Local commits that change more than records: divert them, or
      merge the rest with gix's tree merge (§4.15, Pulling)?

## 10. Verification plan

- **A model test,** in `tests/segments_model.rs`. A reference model computes each view's
  expected records directly, independent of segments:
  - a worktree's from its HEAD tree plus its draft;
  - a layered view's from the base's records plus each upper worktree's
    changes since its fork point, with a copy per worktree wherever more
    than one holds a record, matched by `(path, key)`.

  It's checked after every step of scripted histories:
  - forks off heads and off older commits (splits);
  - edits and commits;
  - fast-forwards that include merges;
  - rebases, resets and force-pushes;
  - worktree deletion and compaction;
  - user branches that live while main moves, commits and is rebased,
    are edited on top of main's newer versions, and are then published;
  - stacks of three or more worktrees, including an independent branch
    that keeps a shared record in a differently named file.

  Add property-based random sequences over the same operations.

  **Status.** Written against an in-memory implementation of the design.
  It runs 2,000 random histories of up to 60 steps per `cargo test`.
  With merges in the model, 2,000,000 have passed, plus 16,000,000
  shorter ones weighted toward publishing after moves and rewrites. Besides views it checks each
  worktree's committed segments against git, its conflicts against the
  reference's, and the tree a commit renders.
  - **What it covers:** merges arriving by fast-forward, stacks of user
    branches, hand edits of the working tree scanned per file, the
    three-way classification, `force` and the resolves-version trailer,
    conflicts carried through commits, both resolutions, and publishing's
    classification.
  - **Moves** between files: pairing in scans, ids through rollups and
    splits, edits following their records after scans, rebuilds and
    publishing, entries tagged by the edit that made them, copies merged
    by `key_id`, and conflicts with a record main deleted, wherever it
    did.
  - **Merges** made outside the database and scanned: regular and
    octopus merges, rebase merges that keep their commits' rollups,
    squash merges, fast-forwards, merges of unrelated histories, and
    changes made in the merge commit itself. Git in the model has parent
    lists, and a record's id is read from the rollup of the commit that
    set its value, through every parent. Where no rollup names a record,
    or names one the worktree didn't give it, the model takes the
    implementation's id, the documented loss of §4.7 and §4.9; wherever a
    rollup does name it, the id must match.
  - **Not modelled:** independent branches.
  - **Resolving for the file's side** is modelled as withdrawing the
    edit. Today it rewrites the edit to the file's value, the same in
    effect, but the model's versions stand for content, so it can't give
    a new row an old version.
  - **Over-reports allowed:** an extra copy, or an extra conflict at
    publishing, only on a row re-created with content a layered edit was
    made over. That's a split's restored row, fold step 4's row, the row
    a resolution for the file's side or `force` writes, a moved row, an
    edit moved to follow its record, a file row re-created after its edit
    was withdrawn, a row a rebuild brings back into view, or a version a
    merge brings back into main. The reverse
    too: a missing conflict, only where such a row showed as a copy in a
    stack and the user edited over it. The user saw main's value; the
    model, going by versions, thinks it was hidden. And an extra conflict
    with a deletion, only where the user's edit was written over such a
    row: the entry that hid it went with the row it was on, so the record
    looked live and the edit kept its base. And, for the same reason, an
    edit that settled a record whose only row at its key is such a row
    stays at that key at publishing though its record moved.
    - A re-created file row takes over entries from an earlier row at its
      key with the same content, made by an edit at that key or of its
      record. That covers the common case; the over-report is left for
      when the earlier row was replaced in the draft first, since rows
      don't record what was seen on them after they're gone.

  **It found these in the design,** all fixed above:
  - the split's restored rows, restoring git's value rather than the
    row below, and which rows a split moves ([§4.7](#47-splitting-a-segment));
  - folds across a fork boundary ([§4.13](#413-compaction-and-garbage-collection));
  - rows taken in from a dirty file counting as pending edits
    ([§4.3](#43-scan-moving-forward));
  - a layered edit's base coming from the target's own view
    ([§4.2](#42-uncommitted-writes));
  - deletions that leave no tombstone, and replacements that drop
    entries, under a layered edit
    ([§3.3](#33-visibility-through-supersession));
  - conflicts left standing at publishing after main went back to the
    edit's base ([§4.12](#412-layered-reads-user-branches-and-private-overlays));
  - with moves: entries that didn't say which edit made them, so every
    move, withdrawal and fold had to guess
    ([§3.3](#33-visibility-through-supersession)); an edit left behind
    by its record, and one that couldn't follow sharing an id with it
    ([§3.5](#35-record-identity)); two edits of one record in a draft, and
    a write missing an absence the merge by `key_id` hid
    ([§4.2](#42-uncommitted-writes)); and fallback ids a split or
    rebuild gave to two records at once
    ([§4.7](#47-splitting-a-segment), [§4.8](#48-rebuilding-after-a-rewrite));
    and deletions missed at publishing, where the record left the edit's
    key before main deleted it
    ([§4.12](#412-layered-reads-user-branches-and-private-overlays)).
    Deciding that from rows took special cases without end; the edit's
    base, taken per record, settles it
    ([§4.2](#42-uncommitted-writes));
  - a fork at a head whose only content is entries dropping them
    ([§4.5](#45-creating-a-branch-or-fork));
  - a renewed edit keeping other drafts' entries about its old record
    ([§3.3](#33-visibility-through-supersession));
  - with merges: a scan that reads no rollups, so a merged record lost its
    id ([§4.3](#43-scan-moving-forward)); a split whose fallbacks ran key by
    key, so a weak claim on one key took the id a rollup gave another, and
    whose row below could be a record deleted before the split point
    ([§4.7](#47-splitting-a-segment)); an edit that followed its record out
    of a file leaving that file unreconciled, which dropped a hand edit at
    the next commit ([§4.3](#43-scan-moving-forward)); and old versions a
    merge brings back showing beside the edits made over them
    ([§4.9](#49-merges)).

  The same harness should later drive the SQL implementation.
- **Mutation checks,** following AGENTS.md's "verify a guard test by
  breaking the code". Each of these mutations must fail the model test:
  - update a row in place instead of replacing it (layered conflicts
    must then go missing);
  - drop the draft re-link after a scan;
  - skip one supersession insert;
  - skip superseding the lower worktrees' rows on a layered write;
  - skip the split's entry rewrite;
  - fold a tombstone that still hides a row;
  - include an upper worktree's inherited segments in a layered view;
  - make conflict rows visible;
  - apply an edit a conflict holds back, in the fold;
  - skip fold step 4, or the re-link after the fold;
  - keep the draft's entries when a row taken in from the file goes;
  - treat rows taken in from the file as pending edits;
  - ignore a resolution the file hasn't moved under;
  - let `force` keep pending edits, or the trailer override a
    resolution;
  - drop the tombstone for a key another draft holds;
  - skip carrying entries to a replacement with the same content;
  - classify every row at publishing, or keep stale conflicts there;
  - restore the row below in a split, rather than git's value;
  - leave rows written after the split point in the prefix;
  - carry entries in the fold only from the head's own row;
  - take a layered edit's base from the target's own view;
  - for moves: skip pairing in the scan, or carry every entry to the
    moved row; ignore rollups, or moved rows, in a split; reuse an id
    the tree has elsewhere, in a split or a rebuild; leave edits behind
    their records, ignore at publishing what an edit settled, or let an
    edit that can't follow keep the id; skip relocation at publishing, or keep
    every entry on a relocated edit; show every copy of a record rather
    than merging by `key_id`; skip superseding a record's copies
    elsewhere; allow two edits of a record in a draft, drop a replaced
    edit's entries, or keep hiding its own tree's row; skip joining the
    entries at a write's key, or join a row the merge hid; retag another
    row's entries, or skip retagging a replaced row's; keep entries on
    main's rows at keys a published draft doesn't hold;
  - for deletions: skip the deletion conflict at publishing, never reset
    a re-create's base, conflict over an absence with a renewed edit, or
    keep other drafts' entries on it;
  - in a split's ids: let the row below ignore other keys' claims, or
    reuse the row of a record that moved, or a new id another key has;
  - fork past a head that holds only entries;
  - with merges: look up a rollup through only the first parent holding
    the value; skip taking other drafts' entries onto a version a merge
    brings back; skip superseding a record's copies in other files.

  A mutation that survives is either a gap in the tests or a rule the
  design doesn't need. To tell them apart, the random tests can run with
  fixed seeds, and count how often the checker allows an over-report or
  a copy, or adopts an id the implementation chose. Removing a needed
  rule changes those counts even where every check passes:
  - **Kept, with a scripted test pinned:** superseding a record's copies
    in other files (without it, allowed over-reports rose from 3,019 to
    3,035 in 2 million cases); taking entries onto a version a merge
    brings back (allowed copies rose from 27,122 to 214,544).
  - **Removed, the counts unchanged:** a write superseding the
    tombstone the merge by `key_id` leaves out, and main's tombstone
    another draft's entry hides; and an edit staying put at a scan when
    it settled the record at its key. Publishing still needs that last
    rule: without it the scripted tests fail.
  - **Kept, undecided:** a split's row below passing over a record that
    moved. Without it, 2 of 2 million cases give a split a different id
    where nothing checks which is right.
- **`EXPLAIN ANALYZE` on the `unfurl-pg-jit` container.** Use a synthetic
  cloudmap of, say, 20k records, 20 worktrees and a few dozen segments,
  with a few hundred user branches. Run:
  - a get;
  - a `find` with a JSON filter;
  - a `find` with `?type=T`, whose expansion should run once per
    statement, with the GIN index on `json -> 'type'` still driving the
    scan;
  - a facet column with two member paths and the type-rollup join;
  - `list_changes`;
  - a layered read;
  - a cross-worktree `find`.

  Expect a bitmap scan on the GIN index and hash or nested-loop
  joins, with no nested loop over the anti-join and no JIT.

---

## Appendix A. Prior art

The approaches fall into five families.

| Family | Systems | New branch | Read on a branch | Their answer to the weak spot |
|---|---|---|---|---|
| Delta chains: a branch stores its changes; reads walk the ancestry | Decibel version-first, Oracle Workspace Manager, Neon, TerminusDB | O(1) | Resolve the newest version across the ancestry | Compaction: Neon's image layers, TerminusDB's delta rollups, Oracle's `CompressWorkspace` |
| Liveness per branch: shared versions plus per-branch membership | Decibel tuple-first and hybrid, OrpheusDB | Copy the membership | A plain filter or join | Compact membership: bitmaps, or one array per version |
| Content-addressed trees | Git, Dolt/Noms, lakeFS | O(1): a root hash | Read the branch's own tree | Chunking gives record-level sharing; indexes must be trees too |
| File or partition snapshots | Iceberg, Delta Lake, Snowflake clones | Copy a file list | Scan the listed files | Only works when changes touch few files |
| Two-level overlay | CMS workspaces (Drupal Workspaces) | O(1) | Overlay, else live | Built for draft → publish, not arbitrary branching |

This design is a delta chain whose ancestry walk has been precomputed
into supersession entries at write time. Or, equivalently, it's
Decibel's hybrid scheme with the liveness bitmap replaced by its
complement. Layered views generalise the two-level overlay to a stack of
branches.

### Decibel

*Decibel: The Relational Dataset Branching System*, Maddox et al.,
PVLDB 9(9), 2016 ([paper](http://www.vldb.org/pvldb/vol9/p624-maddox.pdf)).
It evaluates three storage schemes inside one relational engine:

- **Version-first.** Each branch's modifications live in a separate
  segment file. A child points at its branch point in the ancestor's
  file, and a scan walks the lineage. "The scanner cannot blindly emit
  records from ancestor segment files, as records that are modified in a
  child branch will result in two copies of the tuple … Decibel uses an
  in-memory set to track emitted tuples." That's read-time resolution,
  the thing supersession avoids.
- **Tuple-first.** Every tuple ever seen is stored in one heap, with a
  bitmap of the branches it's live in. A branch copies its parent's
  bitmap.
- **Hybrid.** Segmented files as in version-first, a local bitmap per
  segment, and a global branch-to-segment bitmap. On branching:

  > "The branch operation creates two new head segments that point to
  > the prior parent head segment: one for the parent and one for the
  > new child branch. The old head of the parent becomes an internal
  > segment that contains records in both branches (note that its
  > bitmap is expanded)."

  That's [§4.5](#45-creating-a-branch-or-fork)'s fork. But:

  > "As in tuple-first, the creation of a new branch requires that all
  > records live in the direct ancestor branch be marked as live in a
  > new bitmap column for the branch being created."

  Supersession stores where a row is hidden instead of where it's live,
  and branch creation never changes that.

The paper reports that "our proposed hybrid scheme outperforms the
tuple-first and version-first schemes on our benchmark". Merges there
record parent precedence, or do a three-way diff against the lowest
common ancestor.

### OrpheusDB

*OrpheusDB: Bolt-on Versioning for Relational Databases*, Huang et al.,
PVLDB 10(10), 2017 ([paper](http://www.vldb.org/pvldb/vol10/p1130-huang.pdf)).
It adds versioning to an unmodified Postgres, the same position as
git-sync.

It compares several data models:
- **Per-record version arrays:** `vlist` on each record, either in the
  data table or in a separate versioning table.
- **Split-by-rlist:** a data table of immutable record versions, plus a
  versioning table of `(vid, rlist)`, where `rlist` is an array of record
  ids.

It chose split-by-rlist:
- **Commit** "only need[s] to add one tuple to the versioning table".
- **Checkout** joins `unnest(rlist)` back to the data.
- **The per-record arrays** were "multiple orders of magnitude" slower to
  commit, because every record's array is appended to.

Its LyreSplit partitioning bounds checkout cost. OrpheusDB versions whole
datasets at commit (checkout, edit, commit), not individual live edits,
so its membership arrays would be rewritten on every per-record write
here. Its data table of immutable record versions is the same choice as
[§3.4](#34-rows-are-immutable-versions).

### Oracle Workspace Manager

Rows of a version-enabled table carry multiple versions in the same
table, and workspaces form a hierarchy with merge and refresh
([docs](https://docs.oracle.com/en/database/oracle/oracle-database/18/adwsm/introduction-to-workspace-manager.html)).
- **Version-enabling a table** renames it to `<name>_LT` and creates a
  view with `INSTEAD OF` triggers in its place. It adds `VERSION`,
  `NEXTVER`, `DELSTATUS` and `LTLOCK` columns
  ([ORACLE-BASE](https://oracle-base.com/articles/9i/workspace-management-9i)).
  `NEXTVER` records a row's next version, the nearest thing to
  `superseded` in a product.
- **`CompressWorkspace`** deletes savepoints and intermediate versions,
  to reduce storage and improve performance.

### Neon

Postgres storage with copy-on-write branches, called timelines
([storage docs](https://github.com/neondatabase/neon/blob/main/docs/pageserver-storage.md),
[read path](https://neon.com/blog/get-page-at-lsn)).
- **Branches:** a timeline records its ancestor and the LSN it branched
  at, and a read that doesn't find a page on the timeline falls back to
  the ancestor.
- **Layers:** delta layers record changes. Image layers are snapshots
  written in the background, which shorten the chain of deltas a read has
  to replay and let old deltas be collected.

That's a delta chain whose compaction is the image layer.

### TerminusDB

A stack of immutable delta layers, each recording one commit's additions
and removals. **Delta rollups** flatten the stack without changing
history, triggered so that only a logarithmic number of layers
accumulates
([immutability](https://terminusdb.org/docs/immutability-explanation/),
[rollups](https://terminusdb.com/blog/delta-rollups/)).

### Dolt, Noms and lakeFS

- **Dolt** stores tables, schemas and indexes as prolly trees. These are
  content-addressed B-trees whose identical subtrees are stored once, so
  branches share structure and diffs are cheap
  ([prolly trees](https://docs.dolthub.com/architecture/storage-engine/prolly-tree),
  [structural sharing](https://www.dolthub.com/blog/2024-04-12-study-in-structural-sharing/)).
  Its secondary indexes are prolly trees too, which is what it took to
  make indexed queries work over shared structure.
- **lakeFS** (Graveler) encodes each commit as a two-level Merkle tree:
  content-addressed SSTable ranges under a metarange, with branches as
  pointers to commits
  ([versioning internals](https://docs.lakefs.io/v1.66/understand/how/versioning-internals/)).

### Iceberg and Snowflake

- **Iceberg** branches and tags are named references to snapshots. A
  snapshot is a manifest list over immutable data files, which branches
  share ([branching](https://iceberg.apache.org/docs/latest/branching/)).
- **Snowflake's zero-copy clones** copy metadata that points at the same
  micro-partitions, writing new partitions only when either side changes
  ([overview](https://atrium.ai/resources/snowflake-zero-copy-cloning/)).

Both share at file or partition granularity, which is option A's
weakness for a one-file cloudmap.

### Drupal Workspaces

A `workspace_association` index tracks the latest revision of each
entity changed in a workspace, and reads inside the workspace swap those
revisions in over the live site
([WorkspaceAssociation](https://api.drupal.org/api/drupal/core!modules!workspaces!src!WorkspaceAssociation.php/class/WorkspaceAssociation/9)).
That's the two-level overlay; layered views are the same idea over a
stack of branches.

### Sapling's segmented changelog

Sapling uses the same word for something else: segments of the *commit
graph*, which speed up graph queries such as common-ancestor, and let
`log` and `blame` bisect history in O(log n)
([axes of scale](https://sapling-scm.com/docs/scale/axes/),
[announcement](https://engineering.fb.com/2022/11/15/open-source/sapling-source-control-scalable/)).
git-sync doesn't need its own graph index. Placement asks gix for
merge-bases, once per tracked worktree.

### Lessons carried over

1. **Every delta-chain system needs compaction** to bound its reads.
   Here reads don't walk chains, so compaction only bounds storage and
   chain length ([§4.13](#413-compaction-and-garbage-collection)).
2. **Liveness per branch costs at branch creation.** Decibel and
   OrpheusDB both pay it. Recording supersession instead avoids it.
3. **Content-addressed trees need their own indexes.** That rules them
   out for queries that must run through Postgres and SQLite indexes.
4. **Decibel's measurements favour the hybrid.** Segments are for
   locality and cheap branching; per-row metadata is for multi-branch
   operations. This design keeps that split and inverts the metadata.

## Appendix B. Draft schema

This is Postgres. The SQLite differences follow the DDL.
Tables not shown (`alias`, `txn`, and the `facet_values` function) are
unchanged.

```sql
CREATE TABLE worktree (
    id                BIGSERIAL PRIMARY KEY,
    origin            TEXT      NOT NULL,
    branch            TEXT      NOT NULL,
    commit_id         TEXT,
    default_file_path TEXT,
    family_id         BIGINT,
    -- the worktree's open head segment and its draft
    head_segment_id   BIGINT,
    draft_segment_id  BIGINT,
    -- a list_changes cursor below this must re-read (§4.10)
    reset_version     BIGINT    NOT NULL DEFAULT 0,
    UNIQUE (origin, branch)
);

CREATE TABLE version_seq (
    worktree_id  BIGINT PRIMARY KEY REFERENCES worktree(id) ON DELETE CASCADE,
    next_version BIGINT NOT NULL DEFAULT 1
);
ALTER TABLE worktree ADD FOREIGN KEY (family_id) REFERENCES version_seq(worktree_id);

CREATE TABLE segment (
    id          BIGSERIAL PRIMARY KEY,
    -- every worktree that shares a segment draws versions from one counter
    family_id   BIGINT NOT NULL REFERENCES version_seq(worktree_id),
    kind        TEXT   NOT NULL CHECK (kind IN ('head', 'internal', 'draft')),
    -- phase 1: a tree. Phase 2 (merge segments) replaces this with segment_parent.
    parent_id   BIGINT REFERENCES segment(id),
    -- the commit whose tree this segment completes; NULL for a draft
    head_commit TEXT,
    -- the worktree a head or draft belongs to; NULL once internal
    owner_id    BIGINT REFERENCES worktree(id) ON DELETE CASCADE,
    CHECK ((kind = 'internal') = (owner_id IS NULL)),
    CHECK (kind <> 'draft' OR (parent_id IS NULL AND head_commit IS NULL))
);
CREATE INDEX idx_segment_parent ON segment(parent_id);
ALTER TABLE worktree ADD FOREIGN KEY (head_segment_id)  REFERENCES segment(id);
ALTER TABLE worktree ADD FOREIGN KEY (draft_segment_id) REFERENCES segment(id);

-- a worktree's committed chain: its head and every ancestor of it (I1).
-- Drafts aren't listed: each read names its worktrees.
CREATE TABLE worktree_segment (
    worktree_id   BIGINT  NOT NULL REFERENCES worktree(id) ON DELETE CASCADE,
    segment_id    BIGINT  NOT NULL REFERENCES segment(id)  ON DELETE CASCADE,
    -- the version at which the segment joined the chain (§4.10)
    added_version BIGINT  NOT NULL DEFAULT 0,
    -- part of the chain the worktree was forked with; a layered view takes
    -- only the rest from an upper worktree (§4.12)
    inherited     BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (worktree_id, segment_id)
);
CREATE INDEX idx_worktree_segment_segment ON worktree_segment(segment_id, worktree_id);

CREATE TABLE file (
    worktree_id   BIGINT  NOT NULL REFERENCES worktree(id) ON DELETE CASCADE,
    path          TEXT    NOT NULL,
    format        TEXT    NOT NULL,
    commit_id     TEXT,
    -- the blob this worktree's view of the file was parsed from
    source_oid    TEXT,
    -- the blob at the head commit whose records the committed segments hold
    committed_oid TEXT,
    deleted       BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (worktree_id, path)
);
CREATE INDEX idx_file_format ON file(format);

-- one immutable version of one record (§3.4)
CREATE TABLE record (
    id             BIGSERIAL PRIMARY KEY,
    -- identity shared by every version of the record; `unfurl.server.id`
    key_id         BIGINT  NOT NULL,
    -- no foreign key to file (I4)
    segment_id     BIGINT  NOT NULL REFERENCES segment(id) ON DELETE CASCADE,
    file_path      TEXT    NOT NULL,
    path           TEXT    NOT NULL,
    key            TEXT    NOT NULL,
    -- NULL for a client's edit, which is what makes it pending; a row
    -- taken in from a dirty file keeps the file's last-touching commit
    commit_id      TEXT,
    json           JSONB   NOT NULL,
    deleted        BOOLEAN NOT NULL DEFAULT FALSE,
    version        BIGINT  NOT NULL DEFAULT 0,
    -- in a draft: the commit of the committed version this edit started from
    base_commit_id TEXT,
    -- and that version's content, which the three-way check compares the
    -- file against (§4.2)
    base_json      JSONB,
    -- in a draft: the key_ids of other records an edit settled at its key (§3.5)
    settled        JSONB,
    -- conflict rows live only in drafts (enforced by the writers)
    conflict       TEXT CHECK (conflict IS NULL OR conflict IN ('conflict', 'resolved'))
);
CREATE UNIQUE INDEX uq_record_path     ON record(segment_id, file_path, path, key)
    WHERE conflict IS NULL;
CREATE UNIQUE INDEX uq_record_conflict ON record(segment_id, file_path, path, key)
    WHERE conflict IS NOT NULL;
-- the versions of one record, across segments
CREATE INDEX idx_record_key      ON record(file_path, path, key);
-- lookups that don't name the file (was idx_record_worktree_path)
CREATE INDEX idx_record_path_key ON record(path, key);
CREATE INDEX idx_record_key_id   ON record(key_id);
CREATE INDEX idx_record_version  ON record(segment_id, version);
CREATE INDEX idx_record_type_gin ON record USING GIN ((json -> 'type'));
CREATE INDEX idx_record_json_gin ON record USING GIN (json jsonb_path_ops);

-- superseded(r, c): segment c holds a newer version of r's record (I2)
CREATE TABLE superseded (
    record_id  BIGINT NOT NULL REFERENCES record(id)  ON DELETE CASCADE,
    segment_id BIGINT NOT NULL REFERENCES segment(id) ON DELETE CASCADE,
    -- the record whose version in segment_id supersedes record_id: which
    -- of a draft's edits made the entry (§3.3)
    key_id     BIGINT NOT NULL,
    PRIMARY KEY (record_id, segment_id, key_id)
);
CREATE INDEX idx_superseded_segment ON superseded(segment_id);

-- whose key_ids a rollup names (§3.5): created with the database
CREATE TABLE database_identity (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    uuid TEXT    NOT NULL
);
INSERT INTO database_identity VALUES (1, replace(gen_random_uuid()::text, '-', ''));
```

What SQLite does differently:

- **Types.** `INTEGER PRIMARY KEY` replaces `BIGSERIAL`, with
  `AUTOINCREMENT` on `record`: a new record's `key_id` is its first
  row's id, so a deleted row's id must never come back. `INTEGER`
  replaces `BIGINT` and `BOOLEAN` (0 or 1). `BLOB`, through `jsonb()`,
  replaces `JSONB`, as today.
- **Foreign keys** are declared inline. SQLite can't `ALTER TABLE ADD
  FOREIGN KEY`, but it accepts forward references, so `worktree` and
  `segment` can name each other.
- **No GIN indexes.** The partial unique indexes are the same.
- **The database's uuid** is `lower(hex(randomblob(16)))`.
- **Setting `key_id` on a new record** takes two steps: an insert, then
  `UPDATE record SET key_id = id WHERE id = last_insert_rowid()`.

## Appendix C. Example queries

These are Postgres. Placeholders are named for readability, where the
code would use `$n` or `?n`:

| Placeholder | Stands for |
|---|---|
| `:w` | the worktree |
| `:d` | its draft |
| `:h` | its head |
| `:w0`, `:upper` | a layered view's base worktree, and an array of the worktrees above it |

The SQLite statements have the same shape, with a few substitutions:
- `json_extract` instead of `@>`;
- `IN (…)` lists instead of `= ANY(:array)`;
- in C.5, `json_each` instead of `facet_values`, as `facet_sqlite` does
  today, and a `MATERIALIZED` CTE instead of `unnest`;
- `COUNT(DISTINCT json_array(r.path, r.key))`, since SQLite's
  `COUNT(DISTINCT …)` takes one argument;
- `json_each` for jsonb's `?` and `?|`, as SQLite's type filter does
  today.

### C.1 The view and the visibility test

Every read of one worktree starts from its view, a CTE of tens of rows:

```sql
WITH v AS (
    -- the committed chain
    SELECT segment_id, added_version FROM worktree_segment WHERE worktree_id = :w
    UNION ALL
    -- the worktree's draft
    SELECT draft_segment_id, 0 FROM worktree WHERE id = :w
)
SELECT …
FROM record r
JOIN v ON v.segment_id = r.segment_id
WHERE r.conflict IS NULL
  -- not replaced within the view
  AND NOT EXISTS (SELECT 1 FROM superseded x
                  JOIN v vx ON vx.segment_id = x.segment_id
                  WHERE x.record_id = r.id)
```

`include_conflicts` drops the `r.conflict IS NULL` test. Conflict rows
live only in drafts and are never superseded, so the view's own conflict
rows come through. C.3, C.4 and C.6 write this pattern as `<C.1>`; C.5
wraps it in a CTE.

### C.2 A layered view

This is the same test over the base's whole view plus each upper
worktree's own part, with copies of a record in different files merged
by `key_id` ([§4.12](#412-layered-reads-user-branches-and-private-overlays)).
`layer` says which worktree a row came from, and `copies` counts the
versions the view shows of the same record, matched by `(path, key)`.

```sql
WITH v AS (
    -- the base's whole view
    SELECT segment_id, added_version, CAST(:w0 AS bigint) AS layer,
           FALSE AS upper_draft
    FROM worktree_segment WHERE worktree_id = :w0
    UNION
    SELECT draft_segment_id, 0, id, FALSE FROM worktree WHERE id = :w0
    UNION
    -- each upper worktree's own part: the chain it didn't inherit, and its draft
    SELECT segment_id, added_version, worktree_id, FALSE
    FROM worktree_segment
    WHERE worktree_id = ANY(:upper) AND NOT inherited
    UNION
    SELECT draft_segment_id, 0, id, TRUE FROM worktree WHERE id = ANY(:upper)
),
-- the rows the merge by key_id leaves out: lower rows of a record an
-- upper draft edits at another key. A few rows, so computed once
hidden AS MATERIALIZED (
    SELECT r.id
    FROM record e
    JOIN v ve ON ve.segment_id = e.segment_id AND ve.upper_draft
    JOIN record r ON r.key_id = e.key_id
    JOIN v ON v.segment_id = r.segment_id AND NOT v.upper_draft
    WHERE e.conflict IS NULL
      AND (e.file_path, e.path, e.key) <> (r.file_path, r.path, r.key)
),
counted AS (                     -- the window sorts ids and keys only
    SELECT v.layer, r.id, count(*) OVER (PARTITION BY r.path, r.key) AS copies
    FROM record r
    JOIN v ON v.segment_id = r.segment_id
    WHERE r.conflict IS NULL
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN v vx ON vx.segment_id = x.segment_id
                      WHERE x.record_id = r.id)
      -- copies merged by key_id
      AND NOT EXISTS (SELECT 1 FROM hidden h WHERE h.id = r.id)
)
SELECT c.layer, r.key_id AS id, r.file_path, r.path, r.key, r.json,
       r.version, r.deleted, c.copies
FROM counted c
JOIN record r ON r.id = c.id
```

C.3–C.6 work the same over this view, the merge included; C.6's
`visible` below abbreviates it. The window count covers the rows the
read returns, so a filtered `find` counts only the copies that match the
filter. It runs over ids and keys, with the JSON joined on after: sorted
with the rows' JSON, a section of a few thousand records spills to disk.

### C.3 Get one record

```sql
<C.1>
  AND r.file_path = :file AND r.path = :path AND r.key = :key;
-- at most one row in a worktree's own view (I3); deleted = TRUE is a
-- tombstone: NotFound, as today
```

### C.4 Find with a JSON filter, one page

The page is picked by ids and sort keys, and its JSON fetched after.
The filter's candidates include other segments' versions, and carrying
their JSON through the anti-join costs more than looking the page's
rows up again ([bench](../bench/segments/README.md)).

```sql
WITH page AS (
    <C.1, selecting r.id, r.path, r.key, r.file_path>
      AND NOT r.deleted
      AND r.json @> :containment::jsonb
      AND (r.path COLLATE "C", r.key COLLATE "C", r.file_path COLLATE "C", r.id)
          > (:after_path, :after_key, :after_file, :after_row)
    ORDER BY r.path COLLATE "C", r.key COLLATE "C", r.file_path COLLATE "C", r.id
    LIMIT :limit
)
SELECT r.key_id AS id, r.file_path, r.path, r.key, r.commit_id,
       r.json, r.version, r.deleted
FROM page p
JOIN record r ON r.id = p.id
ORDER BY p.path COLLATE "C", p.key COLLATE "C", p.file_path COLLATE "C", p.id;
```

### C.5 Facets

`/cloudmap/facets` issues these as separate statements, as
`db::record::facet_pg` does today:
- the total;
- the group counts;
- one aggregation per facet column.

They share the view, the filters and the `/types` CTEs. `vis` is the
view's visible rows after the filters.

The count is over `r.id`: that's one row per record in a worktree's own
view, and a shared row counted once across worktrees (§4.1). Over a
layered view it's `DISTINCT (r.path, r.key)`.

```sql
WITH v AS (…),                   -- C.1, C.2, or C.7's view for a cross-worktree scope
types AS (                       -- the view's /types rows (§4.1)
    SELECT t.key, t.json
    FROM record t
    JOIN v ON v.segment_id = t.segment_id
    WHERE t.path = '/types' AND t.conflict IS NULL AND NOT t.deleted
      AND (CAST(:file_path AS text) IS NULL OR t.file_path = :file_path)
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN v vx ON vx.segment_id = x.segment_id
                      WHERE x.record_id = t.id)
),
pairs AS (                       -- the facet rollup: each type under each
    SELECT key AS decl,          -- ancestor, itself included, since extends
           jsonb_array_elements_text(json -> 'extends') AS anc   -- lists it first
    FROM types
),
vis AS (
    SELECT r.*
    FROM record r
    JOIN v ON v.segment_id = r.segment_id
    WHERE NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN v vx ON vx.segment_id = x.segment_id
                      WHERE x.record_id = r.id)
      -- the filters shared with find; each one only when the query asks for it
      AND r.conflict IS NULL                       -- unless include_conflicts
      AND NOT r.deleted                            -- unless include_deleted
      AND r.file_path = :file_path
      AND r.path = :path
      AND (r.key = :key
           OR EXISTS (SELECT 1 FROM alias a         -- key, when aliases are on
                      WHERE a.record_id = r.id AND a.key = :key))
      AND r.json -> 'type' ?| :subtypes::text[]    -- GIN on json -> 'type';
                                                   -- :subtypes from the query below
      AND r.json @> :containment::jsonb            -- an exact match: a GIN
      AND r.json #> :tokens::text[] = :value::jsonb --   pre-filter, then equality
      AND r.json @? :jsonpath::jsonpath            -- a JSON path, GIN
      AND (r.version > :since OR v.added_version > :since)  -- since_version (§4.10)
)
```

`types` scans the record table on its own, rather than filtering a CTE of
every visible row. Postgres materializes a CTE that's referenced twice,
and a materialized CTE of the whole view would have no index for `vis`'s
filters.

`:subtypes`, for a `type` filter on facets or `find`, comes from a first
query over the same view, in the same transaction:

```sql
WITH v AS (…), types AS (…)      -- as above
SELECT array_agg(DISTINCT name)
FROM (SELECT unnest(CAST(:type_names AS text[])) AS name
      UNION
      SELECT key FROM types
      WHERE json -> 'extends' ?| CAST(:type_names AS text[])) n;
```

The total:

```sql
SELECT COUNT(DISTINCT r.id) FROM vis r;   -- layered: COUNT(DISTINCT (r.path, r.key))
```

The group counts. The `pairs` join applies only when grouping by `type`
with subtypes rolled up; without it, `g0` is just `jg.val`.

```sql
SELECT COALESCE(to_jsonb(mg.anc), jg.val) AS g0,
       COUNT(DISTINCT r.id) AS n
FROM vis r
CROSS JOIN LATERAL facet_values(r.json #> :group_path::text[]) jg(val)
LEFT JOIN pairs mg ON to_jsonb(mg.decl) = jg.val
GROUP BY g0;
```

A facet column with two member paths, the first rolled up and the second
not. Each member adds a lateral; the cells are the cross product.

```sql
SELECT jg.val AS g0,
       COALESCE(to_jsonb(m0.anc), j0.val) AS v0,
       j1.val AS v1,
       COUNT(DISTINCT r.id) AS n
FROM vis r
CROSS JOIN LATERAL facet_values(r.json #> :group_path::text[]) jg(val)
CROSS JOIN LATERAL facet_values(r.json #> :member0_path::text[]) j0(val)
LEFT JOIN pairs m0 ON to_jsonb(m0.decl) = j0.val
CROSS JOIN LATERAL facet_values(r.json #> :member1_path::text[]) j1(val)
GROUP BY g0, v0, v1;
```

For a producer whose `extends` lists only direct parents, the expansion and
`pairs` become a `WITH RECURSIVE` walk over `types`. On flattened lists
it finishes after one step.

In a cross-worktree scope, the anti-joins also match `vx.worktree_id` to
`v.worktree_id`, as in C.7.

### C.6 `list_changes`

```sql
-- first: the reset_version of the view's worktrees;
-- a :cursor below any of them answers Reset instead (§4.10)
<C.1>
  AND (r.version > :cursor OR v.added_version > :cursor)
ORDER BY r.version;
-- tombstones included, as today
```

Over a layered view (C.2), the changed rows only pick the records. The
answer is every row the view shows for those records, matched by
`(path, key)`, with `copies` counted over all of them:

```sql
WITH v AS (…),                       -- as in C.2
visible AS (
    SELECT r.*, v.layer, v.added_version
    FROM record r
    JOIN v ON v.segment_id = r.segment_id
    WHERE r.conflict IS NULL
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN v vx ON vx.segment_id = x.segment_id
                      WHERE x.record_id = r.id)
      AND …                          -- C.2's merge by key_id
),
counted AS (
    SELECT visible.*,
           count(*) OVER (PARTITION BY path, key) AS copies
    FROM visible
)
SELECT * FROM counted c
WHERE EXISTS (SELECT 1 FROM visible ch
              WHERE ch.path = c.path AND ch.key = c.key
                AND (ch.version > :cursor OR ch.added_version > :cursor))
ORDER BY c.version;
```

### C.7 Find across worktrees

```sql
WITH v AS (
    SELECT ws.worktree_id, ws.segment_id
    FROM worktree_segment ws
    JOIN worktree w ON w.id = ws.worktree_id
    WHERE (CAST(:origin AS text) IS NULL OR w.origin = :origin)
      AND (CAST(:branch AS text) IS NULL OR w.branch = :branch)
    UNION ALL
    SELECT w.id, w.draft_segment_id
    FROM worktree w
    WHERE (CAST(:origin AS text) IS NULL OR w.origin = :origin)
      AND (CAST(:branch AS text) IS NULL OR w.branch = :branch)
),
page AS (                            -- ids and sort keys only, as in C.4
    SELECT v.worktree_id, r.id, r.path, r.key, r.file_path
    FROM v
    JOIN record r ON r.segment_id = v.segment_id
    WHERE r.conflict IS NULL
      AND NOT r.deleted
      AND r.json @> :filter::jsonb
      -- the worktree match goes in WHERE, not in the join's ON: Postgres then
      -- turns the NOT EXISTS into a hash anti-join instead of a subplan per row
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN v vx ON vx.segment_id = x.segment_id
                      WHERE x.record_id = r.id AND vx.worktree_id = v.worktree_id)
      AND (r.path COLLATE "C", r.key COLLATE "C", r.file_path COLLATE "C",
           v.worktree_id, r.id)
          > (:after_path, :after_key, :after_file, :after_worktree, :after_row)
    ORDER BY r.path COLLATE "C", r.key COLLATE "C", r.file_path COLLATE "C",
             v.worktree_id, r.id
    LIMIT :limit
)
SELECT p.worktree_id, r.key_id AS id, r.file_path, r.path, r.key,
       r.json, r.version
FROM page p
JOIN record r ON r.id = p.id
ORDER BY p.path COLLATE "C", p.key COLLATE "C", p.file_path COLLATE "C",
         p.worktree_id, p.id;
```

### C.8 Create a record

```sql
-- :v was drawn from next_version(:family) earlier in this transaction.
-- If C.3 finds any visible version, even a tombstone, this is C.9 instead.
WITH n AS (SELECT nextval('record_id_seq') AS id)
INSERT INTO record (id, key_id, segment_id, file_path, path, key, json, version)
SELECT n.id, n.id, :d, :file, :path, :key, :json::jsonb, :v
FROM n
RETURNING id;
```

### C.9 Write a new version: update or delete

`:visible` holds the ids of the rows visible for the key in the view the
write is made through, copies merged by `key_id` (C.2), and in `:w`'s own
view (C.3 over each), other than the draft's own version `:old`.
`enforce_conflict` runs against all of them. `:old` is the draft's row at
the key, and `:other` its edit of the same record at another key.

```sql
-- 1. :v drawn first (takes the family lock).

-- 2. what the write supersedes, as the record's edit: the rows in :visible;
--    main's rows at the key with content one of them has, which a draft
--    below may hide; and the record's rows at other keys with content seen
--    here, a move's copy of what the edit was made over (§3.5)
CREATE TEMP TABLE seen ON COMMIT DROP AS
SELECT unnest(CAST(:visible AS bigint[]) || CAST(:old AS bigint)) AS id;

INSERT INTO seen
SELECT r.id
FROM record r
JOIN (<C.1 over main>) m ON m.id = r.id
WHERE (r.file_path, r.path, r.key) = (:file, :path, :key) AND NOT r.deleted
  AND r.json IN (SELECT s.json FROM record s JOIN seen USING (id)
                 WHERE NOT s.deleted);

INSERT INTO seen
SELECT r.id
FROM record r
JOIN (<C.2 or C.1 over the view, and over :w's own>) m ON m.id = r.id
WHERE r.key_id = :key_id AND NOT r.deleted
  AND (r.file_path, r.path, r.key) <> (:file, :path, :key)
  AND r.json IN (SELECT s.json FROM record s JOIN seen USING (id)
                 WHERE s.key_id = :key_id AND NOT s.deleted);

-- 3. other drafts' entries on those rows, :old's included, pass to the new
--    row where the content is the same (§3.3)
CREATE TEMP TABLE carry ON COMMIT DROP AS
SELECT x.segment_id, x.key_id
FROM seen
JOIN record o ON o.id = seen.id
JOIN superseded x ON x.record_id = o.id
JOIN segment s ON s.id = x.segment_id AND s.kind = 'draft' AND s.owner_id <> :w
WHERE o.deleted = :deleted AND (:deleted OR o.json = :json::jsonb);

-- 4. a draft holds one edit per record: one at another key goes, and the
--    new row keeps its base. Its entries stay with the record, except on
--    its own tree's rows of other records at the key it leaves
DELETE FROM record WHERE id = :other RETURNING base_commit_id, base_json;  -- :other_base
DELETE FROM superseded x USING record r
WHERE x.segment_id = :d AND x.key_id = :key_id AND r.id = x.record_id
  AND (r.file_path, r.path, r.key) = (:other_file, :other_path, :other_key)
  AND r.key_id <> :key_id
  AND r.segment_id IN (SELECT segment_id FROM worktree_segment
                       WHERE worktree_id = :w);

-- 5. the draft's current version goes, if it's still what the writer saw
DELETE FROM record
WHERE id = :old
  AND (CAST(:expected_version AS bigint) IS NULL OR version <= :expected_version)
RETURNING id, key_id, base_commit_id, base_json, settled;  -- no row, while :old was expected: Conflict

-- 6. the new version; deleted = TRUE for a delete. :base_commit_id is:
--    - NULL when the view shows the record live nowhere, at any key: a draft's
--      live row counts, the writer's own included, a tombstone doesn't;
--    - else :old's base, else :other_base;
--    - else the commit_id of the record's committed row in the view, at any key.
--    :base_json is the content of the same version.
--    :settled is :old's, plus the key_ids of other records' live rows in
--    :visible outside the draft.
INSERT INTO record (key_id, segment_id, file_path, path, key, json, deleted,
                    version, base_commit_id, base_json, settled)
VALUES (:key_id, :d, :file, :path, :key, :json::jsonb, :deleted,
        :v, :base_commit_id, :base_json::jsonb, :settled::jsonb)
RETURNING id;                                   -- :n

INSERT INTO superseded (record_id, segment_id, key_id)
SELECT id, :d, :key_id FROM seen WHERE id <> :old
ON CONFLICT DO NOTHING;

INSERT INTO superseded (record_id, segment_id, key_id)
SELECT :n, segment_id, key_id FROM carry
ON CONFLICT DO NOTHING;

-- 7. the write joins the draft's entries at its key, except on a live row
--    of a record the draft edits at another key, which the merge hid from it
INSERT INTO superseded (record_id, segment_id, key_id)
SELECT x.record_id, :d, :key_id
FROM superseded x
JOIN record r ON r.id = x.record_id
WHERE x.segment_id = :d
  AND (r.file_path, r.path, r.key) = (:file, :path, :key)
  AND (r.deleted
       OR NOT EXISTS (SELECT 1 FROM record e
                      WHERE e.segment_id = :d AND e.conflict IS NULL
                        AND e.key_id = r.key_id
                        AND (e.file_path, e.path, e.key)
                            <> (r.file_path, r.path, r.key)))
ON CONFLICT DO NOTHING;
```

Between steps 5 and 6, when `:old` was another record's row, as a row
taken in from the file can be, its entries are retagged to `:key_id`, except at a key another
row of the old record holds ([§3.3](#33-visibility-through-supersession)).

### C.10 Scan: bring the head up to HEAD

This is for one record that differs from W's committed chain. `:key_id`
is, in order ([§4.3](#43-scan-moving-forward)):
1. the id the rollup of the commit that set the value names;
2. the id of the record that moved here from the other file in this scan;
3. the id of the record the chain shows at the key;
4. a new one, as in C.8.

An id another key keeps, or took earlier in the scan, is passed over.

```sql
-- other drafts' entries on the head's current version, and on the row
-- below if the head held none, pass to the new row where the content is
-- the same (§3.3); :below is the row visible below the head
CREATE TEMP TABLE carry ON COMMIT DROP AS
SELECT x.segment_id, x.key_id
FROM superseded x
JOIN record o ON o.id = x.record_id
JOIN segment s ON s.id = x.segment_id AND s.kind = 'draft' AND s.owner_id <> :w
WHERE o.id IN (:head_row, :below)
  AND NOT o.deleted AND o.json = :json::jsonb;

-- the head's current version, if it has one, is replaced (§3.4); any entries
-- on it, W's draft's or a layered worktree's, go with it
DELETE FROM record WHERE id = :head_row;

INSERT INTO record (key_id, segment_id, file_path, path, key, commit_id,
                    json, version)
VALUES (:key_id, :h, :file, :path, :key, :commit, :json::jsonb, :v)
RETURNING id;                                   -- :n

INSERT INTO superseded (record_id, segment_id, key_id)
SELECT :n, segment_id, key_id FROM carry
ON CONFLICT DO NOTHING;

-- if the head held no version: the row visible below the head (W's
-- committed chain, without the head) is now superseded by it
INSERT INTO superseded (record_id, segment_id, key_id)
SELECT r.id, :h, :key_id
FROM record r
JOIN worktree_segment ws
  ON ws.segment_id = r.segment_id AND ws.worktree_id = :w
WHERE r.file_path = :file AND r.path = :path AND r.key = :key
  AND r.conflict IS NULL
  AND r.segment_id <> :h
  AND NOT EXISTS (SELECT 1 FROM superseded x
                  JOIN worktree_segment wx
                    ON wx.segment_id = x.segment_id AND wx.worktree_id = :w
                  WHERE x.record_id = r.id AND x.segment_id <> :h)
ON CONFLICT DO NOTHING;

-- then C.11, since W's draft may have lost an entry
```

A record gone from the file follows the same steps, with a tombstone
for the new row, which keeps the record's `key_id`. A row that moved
from the other file (2.) is a new row for the record too, so it also
takes the entries of the row it moved from with the same content. In
main, a version a merge brings back also takes other drafts' entries
from earlier rows with that content, and from edits whose base, read
from git, has it, as §4.3 describes.

### C.11 Re-link a worktree's draft

This runs after a scan, a rebuild or a split changes W's committed chain.
A rebuild first drops the entries the draft has no row to justify
([§4.8](#48-rebuilding-after-a-rewrite), step 5):

```sql
DELETE FROM superseded WHERE segment_id = :d AND record_id IN (
  SELECT r.id FROM record r
  JOIN worktree_segment ws ON ws.segment_id = r.segment_id
  WHERE ws.worktree_id = :w
    AND NOT EXISTS (SELECT 1 FROM record x WHERE x.segment_id = :d
                    AND x.file_path = r.file_path AND x.path = r.path
                    AND x.key = r.key));
```

```sql
-- the committed chain's row at each key the draft holds, tagged with the
-- draft row's record
INSERT INTO superseded (record_id, segment_id, key_id)
SELECT c.id, :d, d.key_id
FROM record d
JOIN record c
  ON c.file_path = d.file_path AND c.path = d.path AND c.key = d.key
JOIN worktree_segment ws
  ON ws.segment_id = c.segment_id AND ws.worktree_id = :w
WHERE d.segment_id = :d AND d.conflict IS NULL
  AND c.conflict IS NULL
  -- c is visible in the committed chain
  AND NOT EXISTS (SELECT 1 FROM superseded x
                  JOIN worktree_segment wx
                    ON wx.segment_id = x.segment_id AND wx.worktree_id = :w
                  WHERE x.record_id = c.id)
ON CONFLICT DO NOTHING;

-- and an edited record's copy in another file, with content the draft
-- already superseded
INSERT INTO superseded (record_id, segment_id, key_id)
SELECT c.id, :d, c.key_id
FROM record c
JOIN worktree_segment ws
  ON ws.segment_id = c.segment_id AND ws.worktree_id = :w
WHERE c.conflict IS NULL AND NOT c.deleted
  AND NOT EXISTS (SELECT 1 FROM superseded x
                  JOIN worktree_segment wx
                    ON wx.segment_id = x.segment_id AND wx.worktree_id = :w
                  WHERE x.record_id = c.id)
  AND EXISTS (SELECT 1 FROM record e
              JOIN superseded x ON x.segment_id = :d
              JOIN record o ON o.id = x.record_id
              WHERE e.segment_id = :d AND e.conflict IS NULL
                AND e.commit_id IS NULL             -- an edit, not taken in
                AND e.key_id = c.key_id
                AND o.key_id = c.key_id AND NOT o.deleted AND o.json = c.json)
ON CONFLICT DO NOTHING;
```

### C.12 Commit: fold the draft into the head

These statements run in the fold's transaction, after the gix commit
`:commit` has carried the files `:files`. `ON COMMIT DROP` is
Postgres-only; SQLite drops the temporary table explicitly at the end.

```sql
-- the rows the commit carries: draft rows of those files with no conflict
-- sibling (today's HAS_CONFLICT_SIBLING test)
CREATE TEMP TABLE carried ON COMMIT DROP AS
SELECT d.id, d.key_id, d.file_path, d.path, d.key, d.json, d.deleted
FROM record d
WHERE d.segment_id = :d AND d.conflict IS NULL
  AND d.file_path = ANY(:files)
  AND NOT EXISTS (SELECT 1 FROM record c
                  WHERE c.segment_id = :d AND c.conflict IS NOT NULL
                    AND c.file_path = d.file_path
                    AND c.path = d.path AND c.key = d.key);

-- 1. other drafts' entries on the committed row each carried row replaces,
--    in the head or below, pass to it when the content is the same
INSERT INTO superseded (record_id, segment_id, key_id)
SELECT k.id, x.segment_id, x.key_id
FROM carried k
JOIN record c
  ON c.file_path = k.file_path AND c.path = k.path AND c.key = k.key
JOIN worktree_segment ws
  ON ws.segment_id = c.segment_id AND ws.worktree_id = :w
JOIN superseded x ON x.record_id = c.id
JOIN segment s ON s.id = x.segment_id AND s.kind = 'draft' AND s.owner_id <> :w
WHERE c.conflict IS NULL
  AND c.deleted = k.deleted AND (k.deleted OR c.json = k.json)
  AND NOT EXISTS (SELECT 1 FROM superseded y
                  JOIN worktree_segment wy
                    ON wy.segment_id = y.segment_id AND wy.worktree_id = :w
                  WHERE y.record_id = c.id)
ON CONFLICT DO NOTHING;

-- 2. the head's own older versions of those records go
DELETE FROM record h USING carried k
WHERE h.segment_id = :h AND h.conflict IS NULL
  AND h.file_path = k.file_path AND h.path = k.path AND h.key = k.key;

-- 3. the draft's entries at those keys, and a carried edit's own entries at
--    any other key, become the head's; not on the head's own rows, since a
--    segment never supersedes its own row
CREATE TEMP TABLE moved ON COMMIT DROP AS
SELECT x.record_id, x.key_id, r.segment_id = :h AS own
FROM superseded x
JOIN record r ON r.id = x.record_id
WHERE x.segment_id = :d
  AND (x.key_id IN (SELECT key_id FROM carried)
       OR EXISTS (SELECT 1 FROM carried k
                  WHERE k.file_path = r.file_path
                    AND k.path = r.path AND k.key = r.key));

INSERT INTO superseded (record_id, segment_id, key_id)
SELECT record_id, :h, key_id FROM moved WHERE NOT own
ON CONFLICT DO NOTHING;

DELETE FROM superseded x USING moved m
WHERE x.segment_id = :d AND x.record_id = m.record_id AND x.key_id = m.key_id;

-- 4. the rows move: id, key_id, version and content are kept
UPDATE record
SET segment_id = :h, commit_id = :commit, base_commit_id = NULL, base_json = NULL,
    settled = NULL
WHERE id IN (SELECT id FROM carried);

-- 5. tombstones that hide nothing below the head are purged, unless another
--    worktree's draft holds the key (§3.3)
DELETE FROM record t USING carried k
WHERE t.id = k.id AND k.deleted
  AND NOT EXISTS (SELECT 1 FROM superseded x
                  JOIN record r ON r.id = x.record_id
                  WHERE x.segment_id = :h
                    AND r.file_path = k.file_path
                    AND r.path = k.path AND r.key = k.key)
  AND NOT EXISTS (SELECT 1 FROM record o
                  JOIN segment s ON s.id = o.segment_id
                  WHERE s.kind = 'draft' AND s.owner_id <> :w
                    AND o.conflict IS NULL
                    AND o.file_path = k.file_path
                    AND o.path = k.path AND o.key = k.key);

-- 6. conflict rows: the head gets the committed value (as in C.10, then
--    C.11); file rows, worktree.commit_id and txn are stamped as today
```

### C.13 Fork at W's head

This is how a user's branch is created too. It assumes W's head `:h` isn't
empty. An empty head stays open instead
([§4.5](#45-creating-a-branch-or-fork)): skip closing it and W's new
head, and give `:nw`'s head `:h`'s parent. The internal-only copy below
already leaves `:h` out.

```sql
-- empty: no rows and no entries, since a head whose rows a scan deleted
-- can still hide rows below it
SELECT NOT EXISTS (SELECT 1 FROM record WHERE segment_id = :h)
   AND NOT EXISTS (SELECT 1 FROM superseded WHERE segment_id = :h) AS empty;

-- in one transaction that holds the family's version_seq lock (§4.14);
-- the new worktree :nw is already inserted, with family_id = :family and no
-- version_seq row of its own

-- W's head becomes internal; W and :nw each get a head below it
UPDATE segment SET kind = 'internal', owner_id = NULL WHERE id = :h;

INSERT INTO segment (family_id, kind, parent_id, head_commit, owner_id)
VALUES (:family, 'head', :h, :commit, :w)
RETURNING id;                                   -- :h_w

INSERT INTO segment (family_id, kind, parent_id, head_commit, owner_id)
VALUES (:family, 'head', :h, :commit, :nw)
RETURNING id;                                   -- :h_nw

INSERT INTO segment (family_id, kind, owner_id)
VALUES (:family, 'draft', :nw)
RETURNING id;                                   -- :d_nw

UPDATE worktree SET head_segment_id = :h_w WHERE id = :w;
UPDATE worktree SET head_segment_id = :h_nw, draft_segment_id = :d_nw,
                    commit_id = :commit
WHERE id = :nw;

INSERT INTO worktree_segment (worktree_id, segment_id, added_version, inherited)
VALUES (:w, :h_w, :v, FALSE);

-- :nw's chain: W's internal segments, inherited, plus its own head. The
-- kind filter keeps out W's new head, which stays W's alone.
INSERT INTO worktree_segment (worktree_id, segment_id, added_version, inherited)
SELECT :nw, ws.segment_id, 0, TRUE
FROM worktree_segment ws
JOIN segment s ON s.id = ws.segment_id
WHERE ws.worktree_id = :w AND s.kind = 'internal';

INSERT INTO worktree_segment (worktree_id, segment_id, added_version, inherited)
VALUES (:nw, :h_nw, 0, FALSE);

-- committed files only: a pending deletion or a file only W's draft
-- registered isn't part of the commit. (Not what was built: the fork
-- creates no file rows, and its first scan creates them. C.20 relies on
-- that, and has C.10 create them.)
INSERT INTO file (worktree_id, path, format, commit_id, source_oid, committed_oid)
SELECT :nw, path, format, commit_id, committed_oid, committed_oid
FROM file
WHERE worktree_id = :w AND committed_oid IS NOT NULL;
```

### C.14 Compaction

```sql
-- committed segments in no chain; their descendants are in no chain either
-- (I1), so they go in one statement. Drafts go with their worktree.
DELETE FROM segment s
WHERE s.kind <> 'draft'
  AND NOT EXISTS (SELECT 1 FROM worktree_segment ws WHERE ws.segment_id = s.id);

-- fold internal segment :p into its only child :c (keeping :c's id);
-- never while :c is an open head (§4.13)
DELETE FROM record r
WHERE r.segment_id = :p
  AND EXISTS (SELECT 1 FROM superseded x
              WHERE x.record_id = r.id AND x.segment_id = :c);

UPDATE record SET segment_id = :c WHERE segment_id = :p;

INSERT INTO superseded (record_id, segment_id, key_id)
SELECT record_id, :c, key_id FROM superseded WHERE segment_id = :p
ON CONFLICT DO NOTHING;

DELETE FROM superseded WHERE segment_id = :p;

UPDATE segment SET parent_id = (SELECT parent_id FROM segment WHERE id = :p)
WHERE id = :c;

DELETE FROM worktree_segment WHERE segment_id = :p;
DELETE FROM segment WHERE id = :p;
```

### C.15 Publish a user branch

This rebases the user's branch `:b` onto main `:m`. The branch has no
commits of its own, so its empty head `:hb` just moves.

```sql
-- in one transaction holding the family lock. If main's head :mh isn't
-- empty, it's closed first, as in C.13, and :base is :mh. If it's empty
-- it stays open and main's alone, and :base is its parent.

-- the branch's chain becomes main's internal segments, inherited, plus its
-- head; the kind filter keeps out main's open head
DELETE FROM worktree_segment WHERE worktree_id = :b;

INSERT INTO worktree_segment (worktree_id, segment_id, added_version, inherited)
SELECT :b, ws.segment_id, :v, TRUE
FROM worktree_segment ws
JOIN segment s ON s.id = ws.segment_id
WHERE ws.worktree_id = :m AND s.kind = 'internal';

UPDATE segment SET parent_id = :base, head_commit = :main_commit WHERE id = :hb;

INSERT INTO worktree_segment (worktree_id, segment_id, added_version, inherited)
VALUES (:b, :hb, :v, FALSE);

UPDATE worktree SET commit_id = :main_commit, reset_version = :v WHERE id = :b;
```

Then the draft's edits are classified against main's rows. `chain` is
the rows visible in `:b`'s new chain, main's tree; each statement below
starts with it:

```sql
WITH chain AS (
    SELECT r.*
    FROM record r
    JOIN worktree_segment ws ON ws.segment_id = r.segment_id AND ws.worktree_id = :b
    WHERE r.conflict IS NULL
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN worktree_segment wx
                        ON wx.segment_id = x.segment_id AND wx.worktree_id = :b
                      WHERE x.record_id = r.id)
)
-- 1. edits whose record main moved: each follows it to its new key, with
--    its entries whose key_id is the record's. Not when the record at its
--    own key is one it settled. Where another of the draft's edits holds
--    the new key, it stays, as a new record (a new key_id)
SELECT e.id, m.file_path, m.path, m.key
FROM record e
JOIN chain m ON m.key_id = e.key_id AND NOT m.deleted
WHERE e.segment_id = :d AND e.conflict IS NULL
  AND (m.file_path, m.path, m.key) <> (e.file_path, e.path, e.key)
  AND NOT EXISTS (SELECT 1 FROM chain h
                  WHERE (h.file_path, h.path, h.key) = (e.file_path, e.path, e.key)
                    AND NOT h.deleted
                    AND e.settled @> to_jsonb(h.key_id));
```

2. **Deletions.** A live edit with a base whose record `chain` shows live
   nowhere was made over a version main has since deleted. It conflicts,
   with main's value at its key as theirs:

   ```sql
   SELECT e.id
   FROM record e
   WHERE e.segment_id = :d AND e.conflict IS NULL AND NOT e.deleted
     AND e.base_commit_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM chain m
                     WHERE m.key_id = e.key_id AND NOT m.deleted);
   ```
3. **Renewed edits** (step 1's new records) over no live value in
   `chain` add a record: no conflict.
4. **The rest** skip classification where the draft already supersedes
   `chain`'s row at the key, or another row of the record with the same
   content. Those that still conflict become conflict rows.
5. **Main's rows are the branch's own tree now.** Where the draft holds
   no edit, its entries on them go:

   ```sql
   DELETE FROM superseded x USING chain m
   WHERE x.segment_id = :d AND x.record_id = m.id
     AND NOT EXISTS (SELECT 1 FROM record e
                     WHERE e.segment_id = :d AND e.conflict IS NULL
                       AND (e.file_path, e.path, e.key)
                           = (m.file_path, m.path, m.key));
   ```
6. Then C.11 re-links the draft. `:b` gets a checkout and a
   `commit_repository`, and its old chain segments are left to C.14.

### C.16 Placement

The git work is outside the transaction ([§4.6](#46-placement)); these
read what it needs, and what it finds.

```sql
-- the family a new worktree joins: another branch of its origin, else the
-- family its history's Git-Sync-Family trailer names (the root's origin)
SELECT family_id FROM worktree WHERE origin = :origin LIMIT 1;
SELECT family_id FROM worktree WHERE origin = :family_origin AND id = family_id;

-- case 1: a segment ending at :commit; an internal one needs no closing
SELECT id, kind, owner_id
FROM segment
WHERE family_id = :family AND head_commit = :commit AND kind <> 'draft'
ORDER BY kind = 'internal' DESC
LIMIT 1;

-- case 2: the tracked heads to test ancestry against, then
SELECT w.id, s.id AS head, s.head_commit
FROM worktree w
JOIN segment s ON s.id = w.head_segment_id
WHERE w.family_id = :family;

-- one worktree's chain from its head down, to find the segment
-- whose head commit :commit is an ancestor of and its parent's isn't
WITH RECURSIVE down(id, parent_id, head_commit, depth) AS (
    SELECT id, parent_id, head_commit, 0 FROM segment WHERE id = :head
    UNION ALL
    SELECT s.id, s.parent_id, s.head_commit, d.depth + 1
    FROM segment s JOIN down d ON s.id = d.parent_id
)
SELECT id, head_commit FROM down ORDER BY depth;
```

### C.17 Split S at *c*

In one transaction holding the family lock (§4.14). S is `:s`; the
per-key cases of [§4.7](#47-splitting-a-segment) are decided in Rust from
the tree diff between *c* and S's head commit, and each runs the
statements named for it.

```sql
-- S2 takes over S's end: its head commit, and its role if S was a head
INSERT INTO segment (family_id, kind, parent_id, head_commit, owner_id)
SELECT family_id, kind, id, head_commit, owner_id FROM segment WHERE id = :s
RETURNING id;                                   -- :s2

UPDATE segment SET parent_id = :s2 WHERE parent_id = :s AND id <> :s2;
UPDATE worktree SET head_segment_id = :s2 WHERE head_segment_id = :s;
UPDATE segment SET kind = 'internal', owner_id = NULL, head_commit = :commit
WHERE id = :s;

-- every chain holding S holds S2, with S's flags
INSERT INTO worktree_segment (worktree_id, segment_id, added_version, inherited)
SELECT worktree_id, :s2, added_version, inherited
FROM worktree_segment WHERE segment_id = :s;

-- a key whose value changed after c, where S has a row (:row): it moves
UPDATE record SET segment_id = :s2 WHERE id = :row;

-- and if the value at c isn't the one below S, S gets it (C.10's insert,
-- into :s, with the entries on the rows below), which S2's row hides
INSERT INTO superseded (record_id, segment_id, key_id)
VALUES (:new, :s2, :key_id)
ON CONFLICT DO NOTHING;

-- otherwise the key didn't change before c, so S's entries on the rows
-- below are S2's now
UPDATE superseded SET segment_id = :s2
WHERE segment_id = :s
  AND record_id IN (SELECT id FROM record
                    WHERE file_path = :file AND path = :path AND key = :key)
  AND NOT EXISTS (SELECT 1 FROM superseded y
                  WHERE y.record_id = superseded.record_id
                    AND y.segment_id = :s2 AND y.key_id = superseded.key_id);
DELETE FROM superseded
WHERE segment_id = :s
  AND record_id IN (SELECT id FROM record
                    WHERE file_path = :file AND path = :path AND key = :key);

-- a key that changed before c and back after it, where S has no row: S
-- gets the value at c (:new, as above) and S2 a row restoring the value at
-- h (:restored) over it; every segment outside S and its ancestors that
-- holds the key, or an entry for it, hides the restored row
INSERT INTO superseded (record_id, segment_id, key_id)
VALUES (:new, :s2, :key_id)
ON CONFLICT DO NOTHING;

WITH RECURSIVE up(id) AS (
    SELECT :s::bigint
    UNION ALL
    SELECT s.parent_id FROM segment s JOIN up ON s.id = up.id
    WHERE s.parent_id IS NOT NULL
)
INSERT INTO superseded (record_id, segment_id, key_id)
SELECT DISTINCT :restored, held.segment_id, held.key_id
FROM (
    SELECT segment_id, key_id FROM record
    WHERE file_path = :file AND path = :path AND key = :key
    UNION
    SELECT x.segment_id, x.key_id FROM superseded x
    JOIN record r ON r.id = x.record_id
    WHERE r.file_path = :file AND r.path = :path AND r.key = :key
) held
WHERE held.segment_id <> :s2
  AND held.segment_id NOT IN (SELECT id FROM up)
ON CONFLICT DO NOTHING;
```

The new rows take ids by §4.7's order, the rollup's first; their
`commit_id` is the file's last-touching commit at *c*.

### C.18 Rebuild W onto a base

In one transaction holding the family lock. The base segment `:base`
comes from placement (C.16), the differences between the tree at the
new HEAD and the base's state from the gix work outside it.

```sql
-- H' above the base; W's old head is left to compaction
UPDATE segment SET kind = 'internal', owner_id = NULL WHERE id = :h;

INSERT INTO segment (family_id, kind, parent_id, head_commit, owner_id)
VALUES (:family, 'head', :base, :commit, :w)
RETURNING id;                                   -- :h2

-- W's chain: the base's ancestry, inherited, and H'
DELETE FROM worktree_segment WHERE worktree_id = :w;

WITH RECURSIVE up(id) AS (
    SELECT :base::bigint
    UNION ALL
    SELECT s.parent_id FROM segment s JOIN up ON s.id = up.id
    WHERE s.parent_id IS NOT NULL
)
INSERT INTO worktree_segment (worktree_id, segment_id, added_version, inherited)
SELECT :w, id, 0, TRUE FROM up;

INSERT INTO worktree_segment (worktree_id, segment_id, added_version, inherited)
VALUES (:w, :h2, 0, FALSE);

-- a segment left the chain, so rows only it added vanish untombstoned (I5)
UPDATE worktree
SET head_segment_id = :h2, commit_id = :commit,
    reset_version = (SELECT next_version FROM version_seq
                     WHERE worktree_id = :family)
WHERE id = :w;

-- then, per key where the tree at the new HEAD differs from the base's
-- state, C.10 into :h2; the file rows' committed_oid from that tree; and
-- C.11 to re-link the draft
UPDATE file SET committed_oid = :blob
WHERE worktree_id = :w AND path = :file;
```

### C.19 Delete a worktree

In one transaction holding the family lock. A family root with other
members is refused ([§4.13](#413-compaction-and-garbage-collection)):
`version_seq` cascades from the worktree, and the members' segments and
worktree rows reference it.

```sql
-- refused when this counts anything
SELECT count(*) FROM worktree WHERE family_id = :w AND id <> :w;

-- W's head and draft go with it (segment.owner_id cascades), and with them
-- their rows, entries and aliases; a root alone takes its family's
-- remaining segments first, since they reference its version_seq row,
-- after letting go of its own head and draft
UPDATE worktree SET head_segment_id = NULL, draft_segment_id = NULL
WHERE id = :w AND family_id = :w;

DELETE FROM segment
WHERE family_id = :w
  AND NOT EXISTS (SELECT 1 FROM worktree o
                  WHERE o.family_id = :w AND o.id <> :w);

DELETE FROM worktree WHERE id = :w;

-- then C.14, for internal segments no chain holds any more
```

### C.20 Export a worktree's conflicts to a branch

[§4.15](#415-remotes-pulling-pushing-and-conflicts); `src/export.rs`. W's
draft is `:d`. In order:

1. **Pick the base** `:base`, outside any transaction: the latest commit
   on HEAD's first-parent line that each conflicted edit's
   `base_commit_id` descends from or is, or that line's first commit when
   there's none (a base a rewrite dropped from history has no ancestor to
   merge from).
2. **Render** the edits onto `:base`'s blobs, to fail here, with nothing
   changed, on a file that can't be rendered there (a literate document
   the base doesn't have, say). Step 4 renders again, from `:n`'s draft.
3. **Claim the name:** create the ref `:branch` at `:base`, which must not
   exist, nor a worktree `(:origin, :branch)`. If the transaction below
   fails, the ref is deleted again while it still names `:base`.
4. **Move the edits,** in one transaction holding the family lock
   (§4.14), below. It also gives `:n` W's file rows, with their blobs at
   `:base`: a fork has none, and `:n` has no checkout to scan.
5. **Commit:** render `:n`'s draft onto `:base`'s blobs, with the rollup
   of `:n`'s batches, as the commit `:commit` whose parent is `:base`.
   Nothing names it yet. When the edits change nothing there, `:commit`
   is `:base`.
6. **Fold** `:n`'s draft into its head at `:commit` (C.12), and bring the
   head up to its tree (C.10). With `:n`'s file rows at the base, only
   the files the export changed are parsed.
7. **Move the ref** from `:base` to `:commit`, and clear
   `worktree.exporting_from`.

The commit is written after the move because its rollup names ids that
only the move settles (step 2 below renews some). Between the two, the
edits are in `:n`'s draft, where nothing else writes. The fold comes
before the ref moves, so that once it's done the database names the
commit and only the ref is left. A failure anywhere loses nothing: an
export of the same `:branch` from W finds `:n` marked as W's unfinished
export, with either an uncommitted draft and the ref at `:n`'s commit
(steps 5 to 7 to go) or an empty draft and the ref at that commit's
parent (step 7 to go). The mark is what keeps a live worktree's
pending edits, or a finished export's, from passing for an unfinished
one.

```sql
-- the edits that go: W's pending edits at a key with an unresolved
-- conflict row. A 'resolved' conflict is the client's call already, and
-- stays.
CREATE TEMP TABLE exported ON COMMIT DROP AS
SELECT e.id, e.key_id, e.file_path, e.path, e.key, e.version
FROM record e
WHERE e.segment_id = :d AND e.conflict IS NULL AND e.commit_id IS NULL
  AND EXISTS (SELECT 1 FROM record c
              WHERE c.segment_id = :d AND c.conflict = 'conflict'
                AND c.file_path = e.file_path
                AND c.path = e.path AND c.key = e.key);

-- guard: these are the edits the base was picked for. A write replaces a
-- draft row with a new one, so a set of ids that differs means a write
-- landed since: roll back, delete the ref, and start again.
SELECT id FROM exported ORDER BY id;

-- the new worktree :n, forked at :base: C.16 places it, C.17 splits the
-- segment it falls inside, and C.13 creates :n with head :hn and draft :dn

-- 1. a copy of each goes to :dn as it is: key_id, version, base, settled
--    and content
INSERT INTO record (key_id, segment_id, file_path, path, key, commit_id, json,
                    deleted, version, base_commit_id, base_json, settled)
SELECT key_id, :dn, file_path, path, key, NULL, json,
       deleted, version, base_commit_id, base_json, settled
FROM record WHERE id IN (SELECT id FROM exported);

-- 2. a copy whose record :n's chain shows live at another place is a new
--    record there, as an edit that can't follow its record is renewed
--    (§3.5): the base has the record elsewhere, and two places can't share
--    an id

-- 3. W resolves each exported conflict for the file, as
--    resolve_conflict(Theirs) does: its edit is withdrawn with its
--    entries, the file's value is taken in, with the key_id a scan gives
--    it, and the file's write_seq moves, so a render W made before this
--    loses

-- 4. batches: copied to :n where some of their edits stay in W's draft,
--    moved where none do. One left behind with nothing in it would be
--    outstanding in W for good.
INSERT INTO txn (worktree_id, first_version, last_version, author, message, created_at)
SELECT :n, t.first_version, t.last_version, t.author, t.message, t.created_at
FROM txn t
WHERE t.worktree_id = :w AND t.commit_id IS NULL
  AND EXISTS (SELECT 1 FROM exported k
              WHERE k.version BETWEEN t.first_version AND t.last_version)
  AND EXISTS (SELECT 1 FROM record r
              WHERE r.segment_id = :d AND r.conflict IS NULL AND r.commit_id IS NULL
                AND r.version BETWEEN t.first_version AND t.last_version);

UPDATE txn SET worktree_id = :n
WHERE worktree_id = :w AND commit_id IS NULL
  AND EXISTS (SELECT 1 FROM exported k
              WHERE k.version BETWEEN txn.first_version AND txn.last_version)
  AND NOT EXISTS (SELECT 1 FROM record r
                  WHERE r.segment_id = :d AND r.conflict IS NULL AND r.commit_id IS NULL
                    AND r.version BETWEEN txn.first_version AND txn.last_version);

-- 5. C.11 re-links :dn against :n's chain: it supersedes what :n shows at
--    each place it holds. W's entries aren't copied: those at other places
--    hide the record where W's history moved it from, which :n's needn't
--    have done.

-- 6. :n is W's unfinished export until step 7
UPDATE worktree SET exporting_from = :w_branch WHERE id = :n;
```
