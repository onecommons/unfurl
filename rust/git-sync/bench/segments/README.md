# Segment design: query-plan benchmark

Synthetic data and the query set for [the segment design](../../docs/branch-segments.md),
with a baseline in today's layout to compare against. This isn't wired
into any test suite: it's for checking plans and timings by hand, before
and after a change.

`queries.sql` holds the design's candidate SQL, written by hand, and
production's statements differ from it. To see what the store actually
runs, use `src/db/bench.rs`, which calls the store's reads against the
same database.

## Files

| File | What it does |
|---|---|
| `generate.sql` | One family: main's chain of segments with churn, forks, user branches layered over main, and drafts. Parameters are psql variables; see the top of the file. Sampling is hash-based, so the same parameters always build the same database. |
| `check.sql` | Checks invariant I3 on a sample of views, and counts the copies that layered views show. |
| `queries.sql` | Appendix C's queries under `EXPLAIN (ANALYZE, BUFFERS, SETTINGS)`: design candidates, not the SQL the store emits. 8, 9 and 11 are layered reads, which production doesn't have until phase 3. |
| `baseline.sql` | Today's layout, a full copy per worktree, built from the generated data for main and the forks. |
| `baseline_queries.sql` | The single-view queries in today's form, against that copy. |
| `../../src/db/bench.rs` | The store's own reads for queries 1–3, 5–8 and 10–13, on Postgres or the SQLite copy, each run six times warm, then once more (on Postgres, under `auto_explain`); then its writes, with the statements each took. |
| `to_sqlite.py` | Copies the generated database into a SQLite file built from `migrations/sqlite`, for the store's SQLite path. |
| `run.sh` | `run.sh generate\|queries\|all\|sqlite [var=value …]`. `generate` applies `migrations/postgres` and `generate.sql`, then makes the SQLite copy; `sqlite` makes the copy again on its own. Run it with no mode for the details. |

## Running it

It runs against the `unfurl-pg-jit` container (Postgres 17, JIT on) by
default. `BENCH_PG_URL` and `BENCH_DB` override the server and database.

```bash
docker run -d --name unfurl-pg-jit --restart unless-stopped \
    -v unfurl-pg-jit:/var/lib/postgresql/data -e POSTGRES_PASSWORD=pgjit \
    -p 55432:5432 postgres:17                  # or `docker start unfurl-pg-jit`

./run.sh generate                              # defaults: 20k records, 20 forks, 300 users
./run.sh generate records=50000 users=1000     # or scale it
psql "$URL/unfurl_segments_bench" -f check.sql
./run.sh queries

psql "$URL/unfurl_segments_bench" -f baseline.sql           # today's layout
psql "$URL/unfurl_segments_bench" -f baseline_queries.sql
```

The store's reads, against the database `generate` built and against the
SQLite copy it made. The copy goes to `$BENCH_SQLITE`, or
`unfurl_segments_bench.db` in the temp directory, where `bench.rs` looks
by default; it needs Python's `sqlite3` at 3.45 or later, for `jsonb()`.
On macOS, asdf's Python links the system's SQLite 3.43; run `run.sh` with
Homebrew's first on the `PATH`.

The writes run after the reads, as they change the database: records of
the bench's own, made, updated and deleted through `apply_batch_inner`,
the single-record writes and a scan's on the first fork, and last main's
scan of a merge that changes 100 of its records. Their tombstones stay
in the fork's draft, and the next run's writes replace them, so
regenerate before comparing runs. Time writes in release builds: a debug build checks
each single write's partial draft against reads of the whole one.

```bash
cargo test --release --features postgres --lib bench_segments_pg -- --ignored --nocapture
cargo test --release --lib bench_segments_sqlite -- --ignored --nocapture
```

The nightly workflow (`.github/workflows/nightly.yml`) times HEAD
against a baseline commit, `refs/bench/git-sync`, on the same machine in
the same job: GitHub's runners differ by CPU, and a time stored from one
doesn't hold on another. It builds both, then runs each three times for
each of two families, the default ("20k") and "500" below, alternating
which side goes first, each on a fresh database from its own side's
migrations and `generate.sql`, with `$BENCH_JSON` naming a file for the
rows and `$BENCH_FAMILY` the family. `compare.py` then fails a row where
its fastest time at HEAD is more than its limit times the base's (1.5 by
default, in `limits.json`) and at least `floor_ms` (0.1) more, below
which a fast read's ratio is the timer's noise, or where it's gone from
HEAD; and all a family's rows where its database differs between the
two. It shows each row's statements too, but they fail nothing: a row
costs what its time says. A read's time is the fastest of six
calls, a write's that of its n together. It reports every row, with the
runner's CPU. To accept a change that moves a number, move the ref:
`git push --force origin <commit>:refs/bench/git-sync`. It's neither a
branch nor a tag, so moving it runs no workflow, and it keeps its commit
when a branch's history is rewritten.

At the defaults, generation takes about 20 seconds, `baseline.sql` about
30, and `check.sql` a few seconds. Since 2026-10-21 generation ends with
`VACUUM ANALYZE`, where it ran `ANALYZE`: autovacuum could otherwise
reach the tables partway through a run and change their plans, such as
a scan answered from an index alone. Earlier sections' numbers come from
databases it may or may not have reached.

## Results, 2026-10-01

At the defaults: 20,000 records plus 61 types, main's chain of 30
segments at 1% churn each, 20 forks, 300 user branches, and 10% of user
edits in conflict with main. Postgres 17 on arm64, with warm caches, all
in one session. "The store" is `bench.rs`: `auto_explain`'s time for the
call's slowest statement. "Design" is `queries.sql` and "plain CRUD" is
`baseline_queries.sql`, today's layout of one row per record per
worktree, each the range over three runs.

| Size | Segments | Plain CRUD |
|---|---|---|
| Record storage | 110 MB for everything, 300 user branches included | 730 MB for main and 20 forks; user branches can't be represented |

| Query (ms) | The store | Design | Plain CRUD |
|---|---|---|---|
| 1. get one record | 0.06 | 0.13–0.22 | 0.07–0.12 |
| 1b. miss one record | 0.01 | 0.03–0.04 | — |
| 2. find, JSON filter, one page | 2.8 | 3.0–3.5 | 4.2–6.2 |
| 3. find, `?type=T2` and subtypes, list expanded by a first query, JSON fetched only for the page | 23 | 20.4–20.8 | 49–78¹ |
| 5. facet by type, rolled up | 241 | 338–355 | 400–495 |
| 6. facet column, 3 dimensions | 540 | 1,715–1,779 | 1,808–2,182 |
| 7. `list_changes` | 10 | 9.8–11.4 | 72–79² |
| 10. find across main and forks, JSON fetched only for the page | 18–22³ | 17.9–18.5 | 15¹ |

¹ Plain CRUD fetches JSON for every candidate, not just the page.

² The plain table has no version index; a real one would have.

³ The call's wall time: `auto_explain`'s per-node timing inflates the
one explained run to 23 ms.

A facet call also runs the total (24 ms) and the group pass (query 5)
before its columns, so query 6's whole call takes 750–910 ms. No plan
used JIT; the reads across worktrees of 2026-10-15 did.

Where the store's finds lost to the design's SQL, two causes account
for most of it. Measured by running the store's statements in psql with
one change each, in one session:

| ms | As emitted | `r.json`, not `r.json::text` |
|---|---|---|
| 3, custom plan | 63 | 40 |
| 3, generic plan | 96 | 75 |
| 10, custom plan | 77 | 51 |
| 10, generic plan | 108 | 77 |

- **The generic plan couldn't see bound lists and scopes.** Once sqlx's
  cached statement had run five times, it got a generic plan. With the
  type list bound as `$n::text[]`, that plan estimated one matching row
  per segment and probed the GIN index once per segment, 31 times on
  main. The `($n IS NULL OR …)` scope tests hid the worktree count the
  same way, so query 10 expected 17 view segments and got 403.
  Emitting only the scope clauses that are set didn't help: a generic
  plan still estimates `branch = $3` from the average branch. The store
  now runs its built finds and facets as unnamed statements, planned
  with their values on each call, for 0.3–0.5 ms of planning.
- **`r.json::text` is computed for every candidate, not just the page.**
  Postgres evaluates the select list below a top-N sort, so query 10
  rendered about 8,400 rows of JSON as text to return 50, and carried
  each candidate's JSON through the anti-join and the sort. Postgres
  finds now select the page by id and sort keys, as 3b and 10b do, then
  join the page's rows: query 3 went from 63 ms to 23, and query 10 from
  78 to about 20. SQLite only computes the select list for the rows the
  limit keeps, so the same change made its finds slower, and its arm
  doesn't have it.
- **`list_changes` read the whole view** and filtered by version in Rust,
  about 630 ms a call. It now filters in SQL, as query 7 does.
- **Facets sorted every record-cell, and extracted members once per
  rollup bucket.** Query 6 makes 175,046 record-cells from 19,896
  records (×4.4 for the type rollup, ×2 for tags). `COUNT(DISTINCT
  r.id)` sorted them all, spilling 11 MB to disk, about 900 ms; and the
  planner joined the rollup pairs before extracting tier and tags, so it
  ran `facet_values` 87,523 times per member instead of 19,896. Facets
  now extract each record's values behind an `OFFSET 0` fence, join the
  rollup after, and count with `DISTINCT` then `COUNT(*)`, which hashes:
  query 6 went from 1,801 ms to 540, and query 5 from 388 to 241. The
  design's SQL and the plain table have the same shape, and would gain
  the same.
- Neither `@?` against `@>`, nor the inline view against a CTE, made a
  difference beyond noise.

## SQLite, 2026-10-01

The store's reads against `run.sh sqlite`'s copy, after each of the day's
changes. Warm medians in ms, one session; SQLite plans every statement
with its values, so the custom-plan change doesn't apply.

| Query (ms) | Start of day | + `list_changes` in SQL | + page-first finds | + facet rewrite |
|---|---|---|---|---|
| 1. get one record | 0.4 | 0.3 | 0.4 | 0.3 |
| 1b. miss one record | 0.3 | 0.2 | 0.2 | 0.2 |
| 2. find, JSON filter, one page | 65 | 65 | 67 | 68 |
| 3. find, `?type=T2` and subtypes | 6 | 6 | 10 | 10 |
| 5. facet by type, rolled up | 1,000 | 930 | 940 | 410 |
| 6. facet column, 3 dimensions | 2,330 | 2,300 | 2,330 | 1,400 |
| 7. `list_changes` | 605 | 5 | 5 | 5 |
| 10. find across main and forks | 60 | 62 | 64 | 65 |

Page-first was then taken out of the SQLite arm. Query 2 scans every
candidate's JSON with `json_each`: SQLite has no GIN index.

## Findings

- **Aggregates are the same or faster.** Facets make one pass over a
  view, and the visibility anti-join is a small part of it. In query 6 it
  takes 27 ms of the 1.65 s; the rest is the facet itself, 175k cells
  sorted for `COUNT(DISTINCT)`, in either layout.
- **`list_changes` is 8× faster,** thanks to the `(segment_id, version)`
  index. Today's schema has no version index.
- **Merging copies by `key_id` is free when its rows are computed
  first.** The rows it leaves out, lower rows of a record an upper draft
  edits at another key, are found from the draft's few edits through
  the `key_id` index, in a `MATERIALIZED` CTE, and excluded by a hash
  anti-join on the row id. Checking each visible row against the edits
  instead cost a third more on query 8 and 11. Without `MATERIALIZED`,
  Postgres inlines the CTE and runs it once per row, which doubles them.
- **A layered read's copy count sorts ids, not JSON.** Query 8 returns
  a whole section, about 4,000 rows; with their JSON the window's sort
  spilled 5.6 MB to disk. Counting over ids and keys, then joining the
  JSON on, keeps it in memory and takes the query from 29 ms to 21.
- **The machine drifts between sessions.** Queries 5 and 6 are 10–20%
  slower than in the previous run in both layouts, with the query and
  schema unchanged, and a database on the previous schema times the same
  side by side. Compare layouts within one session.
- **Point reads cost a little more,** a tenth of a millisecond: the key
  index covers every segment's versions of the record, and each is
  checked against the view. A single-view find with a JSON filter is now
  faster than today, though its candidates include other segments'
  versions too.
- **In a cross-worktree anti-join, the worktree match must go in
  `WHERE`.** In the join's `ON`, Postgres runs the `NOT EXISTS` as a
  subplan per (worktree, candidate) pair; in `WHERE`, it becomes a hash
  anti-join. The design's C.7 now uses the `WHERE` form. That took query
  10 from 86 ms to 50 ms.
- **Fetching JSON only for the page** takes it to 20–22 ms, against
  16–17 ms today. The anti-join hashes about 10k candidates, and carrying 1.4 KB of
  JSON through that costs more than looking the page's rows up again.
  Finds should select ids and sort keys first.
- **The type filter's list is expanded by a first query.** Computed inside
  the statement (query 3), the list comes from an InitPlan whose size the
  planner can't see. It estimated 29 matching rows against 6,636, and
  rescanned the GIN index once per segment. Expanded by a small first
  query over the same view (about 2 ms) and bound as a constant, with JSON
  fetched only for the page (query 3b), the whole thing takes about
  25 ms, against 54–55 ms today. And that's with no cache.

## Facets across worktrees, 2026-10-15

Facets now count a key once however many files, worktrees or
repositories hold a copy of it, and reads 11 and 12 run reads 5 and 6
across the 21 worktrees on a branch named `main` (main and the forks).
The database is the defaults': 36,189 record rows for 20,061 keys,
16,918 superseded entries, 671 segments, 127 MB on Postgres and 81 MB as
SQLite. Times here are a whole call's warm median in ms, not one
statement's, so they don't compare with the tables above.

| Postgres | Per row | Per key | + visible rows once | + temporary table, integer key | + `work_mem` | + text grouping, JIT off |
|---|---|---|---|---|---|---|
| 5. facet by type | 245 | 255 | 261 | 231 | 263 | 236 |
| 6. facet column | — | 816 | 894 | 1,561 | 755 | 666–765¹ |
| 11. 5 across every `main` | 3,100 | 3,047 | 645 | 457 | 521 | 425 |
| 12. 6 across every `main` | 26,000 | 28,180 | 3,655 | 2,779 | 2,666 | 1,202 |

| SQLite | Per row | Per key | + visible rows once | + temporary table, integer key |
|---|---|---|---|---|
| 5. facet by type | 393 | 1,220 | 611 | 490 |
| 6. facet column | — | 2,660 | 1,817 | 1,395 |
| 11. 5 across every `main` | 4,700 | 7,578 | 4,060 | 2,378 |
| 12. 6 across every `main` | 16,000 | 22,269 | 7,667 | 4,588 |

¹ The last two runs, one with JIT and one without: noise, not JIT.

- **Across worktrees, a row was extracted once per worktree that sees
  it.** Most of main's segments are in all 21 views, so read 11 extracted
  493,627 (row, worktree) pairs from 36,066 rows. Finding the distinct
  visible ids first and extracting from those took read 12 from 28 s to
  3.7 s.
- **Each count repeated the visibility check.** A facet call runs the
  total, the groups and one pass per column, each with its own anti-join.
  The visible ids now go into a temporary table once per call, which
  every count reads; it's dropped with the call's transaction.
- **Counting per key cost SQLite more than Postgres**: two text columns
  to compare where an id was one integer. The temporary table numbers
  each `(path, key)` with `DENSE_RANK()`, so the counts compare an
  integer. A schema column would do no better for facets, and nothing
  else groups by it.
- **An accurate estimate made Postgres sort on disk.** The visibility
  join used to underestimate the cells, about 19,000 against 175,046, so
  the planner hashed their `DISTINCT`. Reading the temporary table, it
  estimated them right, and at that size a hash didn't fit 4 MB of
  `work_mem`, so it sorted the jsonb values on disk instead: read 6 went
  from 0.9 s to 1.6 s. `ANALYZE` on the temporary table changed nothing.
  The call sets `work_mem` to 32 MB for its transaction, which hashes.
- **Sorting jsonb is slow; sorting its text under `"C"` isn't.** Read 12
  overestimates its cells, 3.1 million against 258,496, so it sorts them
  in memory either way, and jsonb comparison took 1.2 s of it. Values now
  group on jsonb's text, which orders an object's keys but keeps a
  number's digits: `1.0` and `1.00` are two values, as on SQLite.
- **The overestimate turned on JIT, which didn't pay.** It compiled for
  about 0.4 s on read 12 and gained nothing on any read, so the facet
  call turns it off: 1.76 s to 1.2 s. At this size there aren't enough
  rows for compiled expressions to win the compile back.
- **What's left is the rollup join's overestimate**, and 21 views' worth
  of anti-join: read 11's visibility check alone is about 250 ms.

## Small databases, 2026-10-16

Two smaller families, each in a database of its own; `bench.rs` reads the
same `BENCH_DB` and `BENCH_SQLITE`:

```bash
BENCH_DB=unfurl_segments_small500 BENCH_SQLITE=$TMPDIR/small500.db ./run.sh generate \
    records=500 types=20 main_segments=10 forks=3 fork_edits=10 \
    fork_draft_edits=2 users=10 user_edits=5 main_draft_edits=5
BENCH_DB=unfurl_segments_small2k BENCH_SQLITE=$TMPDIR/small2k.db ./run.sh generate \
    records=2000 types=30 main_segments=15 forks=5 fork_edits=40 \
    fork_draft_edits=3 users=30 user_edits=10 main_draft_edits=10
```

"500" is 660 record rows for 521 keys, 145 superseded entries, 37
segments and 4 worktrees on `main`: 2.7 MB on Postgres, 1.6 MB as SQLite.
"2k" is 2,850 rows for 2,031 keys, 842 superseded entries, 86 segments
and 6 worktrees on `main`: 10 MB and 6.5 MB. Times are a whole call's
warm median in ms, as in the section above.

| Query (ms) | 500, Postgres | 500, SQLite | 2k, Postgres | 2k, SQLite |
|---|---|---|---|---|
| 1. get one record | 2.5 | 0.2 | 1.7 | 0.2 |
| 1b. miss one record | 0.9 | 0.2 | 1.5 | 0.2 |
| 2. find, JSON filter, one page | 1.7 | 1.0 | 3.8 | 6.9 |
| 3. find, `?type=T2` and subtypes | 6.2 | 2.1 | 6.9 | 3.3 |
| 5. facet by type | 9.9 | 11.8 | 27 | 40 |
| 6. facet column | 34 | 36 | 81 | 118 |
| 7. `list_changes`¹ | 21 | 16 | 18 | 14 |
| 10. find across every `main` | 4.5 | 1.8 | 4.1 | 5.3 |
| 11. 5 across every `main` | 12 | 26 | 32 | 109 |
| 12. 6 across every `main` | 29 | 61 | 102 | 242 |

¹ Every change: with fewer than 1,000 versions, the cursor is 0.

- **A Postgres point read is mostly round trips.** Read 1's statement
  takes 0.08 ms of the call's 2.5. SQLite's are in process.
- **SQLite's facets fall behind from a few thousand records,** and
  across worktrees most: 2.4–3.4× Postgres on reads 11 and 12 at 2k.
- **Postgres's facet columns vary by a third between runs** at these
  sizes: read 6 took 30–42 ms at 500 and 68–106 ms at 2k.
- **The temporary table costs about a millisecond at 500 records, and
  pays from 2k.** Making it takes 1.2 ms more than its query as a plain
  `SELECT`, and the `ROLLBACK` that drops it 0.8 ms more than a plain
  one; each pass reading it saves a visibility check, 0.9 ms at 500.
  The whole call in psql, against each pass computing the rows in a
  `MATERIALIZED` CTE:

| Whole call (ms) | 500 | 2k | 20k |
|---|---|---|---|
| 5, temporary table | 9.1 | 25.0 | 226 |
| 5, CTE in each pass | 8.1 | 28.5 | 268 |
| 6, temporary table | 21.0 | 59.4 | 595 |
| 6, CTE in each pass | 20.6 | 66.1 | 696 |

## Facet values inline, 2026-10-16

Postgres facets expanded each path with `facet_values`, a SQL function
kept from being inlined (`STRICT`) so the planner took its `ROWS 2`.
Each call cost about 3 µs of executor setup: on read 12, its 88k calls
took 275 ms of the column pass's 780, where reading the paths' JSON took
100. The expansion is now inlined in the statement, each path read once
per record behind an `OFFSET 0`, and the function is gone. The store's
facets before and after, interleaved in one session, two runs each:

| Postgres (ms) | 500 before | after | 2k before | after | 20k before | after |
|---|---|---|---|---|---|---|
| 5. facet by type | 10.2 | 11.8 | 27.1 | 22.9 | 246 | 187 |
| 6. facet column | 31.9 | 36.0 | 85.3 | 70.4 | 634 | 586 |
| 11. 5 across every `main` | 11.9 | 13.1 | 33.6 | 29.3 | 438 | 340 |
| 12. 6 across every `main` | 30.1 | 25.0 | 98.3 | 84.1 | 1,201 | 660 |

At 500 the differences are within the noise.

- **Read 6 gains least.** Inlined, each expansion is estimated at the
  100 rows of a built-in set-returning function, so its cells come to
  192 billion against 175,046, and the planner sorts their `DISTINCT`
  instead of hashing it: the sort gives back most of what extraction
  saved (405 ms to 145). `enable_sort = off` didn't change the whole
  call beyond noise. The estimate is harmless only because the call
  turns JIT off.
- **Counting by signature traded one facet for another.** Keys whose
  values put them in the same cells count the same: 20,055 keys made
  2,100 such sets, and counting keys per set, then expanding only the
  sets through the rollup, took read 6 to 346 ms. But a member unique to
  each record (`metadata.homepage_url`) leaves a set per key, and it was
  130–180 ms slower than without. SQLite went the same both ways: read 6
  from 622 ms to 404 with `tier`, and from 748 to 904 with
  `homepage_url`. Facet paths are the caller's, so it wasn't taken.
- **Extracting into a second temporary table,** for its accurate row
  count, planned badly at small sizes: 183 ms against 20 at 500 records.

## SQLite's type closure, 2026-10-16

SQLite's `json_each` is built in, and extraction was 30–80 ms of its
column pass. Most of the rest was the type closure: SQLite inlines a CTE
it isn't told to materialize, so the closure's recursive step reran
`edges`, visibility check and all, for each row it added. That was 443 ms
per pass on read 12, built twice per call, for 61 `/types` records. The
facets now materialize `edges`; `?type=` with subtypes doesn't, since
its recursion reads only the edges it reaches, and materializing took
read 3 from 6.5 ms to 21.7. Interleaved, two runs each:

| SQLite (ms) | 500 before | after | 2k before | after | 20k before | after |
|---|---|---|---|---|---|---|
| 3. find, `?type=T2` and subtypes | 2.3 | 2.4 | 3.2 | 3.2 | 6.5 | 6.5 |
| 5. facet by type | 11.7 | 7.8 | 40.9 | 30.2 | 426 | 368 |
| 6. facet column | 35.3 | 26.7 | 119 | 98.2 | 1,418 | 1,317 |
| 11. 5 across every `main` | 25.1 | 10.8 | 109 | 54.0 | 2,388 | 1,426 |
| 12. 6 across every `main` | 62.0 | 31.8 | 247 | 136 | 4,582 | 2,556 |

What's left on SQLite is the rollup join and its `DISTINCT`, 300–500 ms
of read 6 and 12's column pass at 20k.

## Writes, 2026-10-21

Each write read its worktree's whole draft three times, five when it
replaced a draft row, so its cost grew with the draft: the fork's draft
holds 1,110 of the bench's rows by the updates. Now a transaction reads
the draft once and keeps it as its writes change it (`segments::Draft`).
Per write, on GDK's Postgres 17 over its socket and the SQLite copy, at
the defaults:

| Write | n | statements before | after | SQLite ms before | after | Postgres ms before | after |
|---|---|---|---|---|---|---|---|
| batch create | 10 | 12.1 | 6.4 | 2.1 | 0.82 | 2.6 | 1.65 |
| batch create | 100 | 12.0 | 6.0 | 3.5 | 0.47 | 3.4 | 0.38 |
| batch create | 1,000 | 12.0 | 6.0 | 22.0 | 0.76 | 21.2 | 0.72 |
| batch update | 10 | 14.1 | 8.4 | 42.0 | 2.57 | 37.8 | 2.99 |
| batch update | 100 | 14.0 | 8.0 | 39.2 | 1.40 | 38.1 | 1.38 |
| batch update | 1,000 | 14.0 | 8.0 | 40.5 | 1.29 | 37.7 | 1.24 |
| single update | 10 | 15.0 | 12.0 | 40.5 | 13.8 | 38.0 | 13.6 |
| single delete | 10 | 14.0 | 11.0 | 38.4 | 13.5 | 37.8 | 13.7 |
| batch delete | 100 | 13.0 | 7.0 | 39.1 | 1.36 | 37.1 | 1.22 |
| batch delete | 1,000 | 13.0 | 7.0 | 39.5 | 1.24 | 37.6 | 1.20 |

Postgres runs one statement more per transaction. "Before" is the code
after the change's first step, which split `write_in_tx`, measured with
the bench's writes added to it; it already read a record's committed
rows once rather than twice, one statement fewer per first update. Before that, a temporary
test of `apply_batch` over a 2,000-record file on SQLite took 20.5 ms a
create in a batch of 1,000 and 61 ms an update over a 1,220-row draft;
after, 0.52 and 1.19.

- **A batch's writes are flat in the draft's size** now: about 1 ms
  each on both backends. The time went to fetching and parsing the
  draft's rows, which is why Postgres and SQLite matched to within 10%.
- **A single write still reads the whole draft once,** so it costs
  13.6 ms at this draft's size and grows with it. It needs only its row
  at the key, its record's rows, the entries at the key, and whether the
  records those hide have a row elsewhere.
- **What's left per write** is the committed side, read per op: the live
  record at the key, the record's committed rows, other drafts' entries
  on them, the file's format. The committed chain doesn't change during
  a batch, so a batch could read those once for all its writes.

## Single writes, 2026-10-21

A single write, and each key a scan takes in, still read their
worktree's whole draft: 1,110 rows on the first fork by then. Now they
read only what a write at one key can touch: the rows at the places the
key's records have rows, and the entries those records made or at the
key (`Draft::around`). Per write, in release builds, at the defaults;
"before" is the change before it with the scan's row added to the bench.
The section above timed debug builds, which check each write's draft
against a full read: a single update on SQLite took 13.8 ms there, 5.94
here.

| Write | n | statements | SQLite ms before | after | Postgres ms before | after |
|---|---|---|---|---|---|---|
| single update | 10 | 12.0 | 5.94 | 1.08 | 2.25 | 0.85 |
| single delete | 10 | 11.0 | 5.95 | 1.09 | 2.11 | 0.42 |
| scan take-in | 100 | 6.0 | 5.38 | 0.30 | 1.92 | 0.21 |
| batch update | 1,000 | 8.0 | 0.31 | 0.33 | 0.33 | 0.36 |

Postgres runs one statement more per transaction. The ten-write rows
total 4 to 11 ms, so they vary by a third between runs.

- **A single write now costs what a batched one does,** and a scan's
  take-in what a batched create does (0.19 ms on SQLite). Neither grows
  with the draft any more, only with the rows at its key's places.
- **An index on `superseded(segment_id, key_id)`,** for the entries a
  write's records made, didn't pay: with it, Postgres's batched updates
  and deletes of 1,000 took 0.39 to 0.45 ms a write over three runs, and
  0.36 in the one without, and single writes were no faster.

## Reads across worktrees by git's rule, 2026-10-23

A read across worktrees now returns each worktree's own view, less a
version unchanged since its merge base with another the read covers,
which has changed it since (branch-segments.md §4.1). SQL reads the
views, then in one statement the rows at every place the page's records
have a row, with the entries on them, and Rust leaves out the copies
(C.7). Read 8 is read 2's next page and read 13 is read 10's. At the
defaults, in release builds, on the `unfurl-pg-jit` container and the
SQLite copy, the fastest of six warm calls over two runs, in ms, as
`compare.py` takes them. "Before" is the nightly's baseline (09e8cd3a),
which left a row any covered worktree's entry superseded out of every
view, run alternately with it; it has no reads 8 and 13:

| Read | Postgres before | now | statements | SQLite before | now | statements |
|---|---|---|---|---|---|---|
| 8. find, JSON filter, the next page | | 5.8 | 1 | | 1.6 | 1 |
| 10. find across every `main` | 10.0 | 19.4 | 1 → 4 | 47.8 | 38.2 | 1 → 4 |
| 11. 5 across every `main` | 217 | 235 | 5 → 13 | 174 | 277 | 3 → 11 |
| 12. 6 across every `main` | 433 | 506 | 6 → 14 | 555 | 656 | 4 → 12 |
| 13. 10's next page | | 48.6 | 6 | | 3.5 | 6 |

Reads 1–7 didn't move beyond noise. On the 500-record family, the
nightly's limit of 1.5 times fails Postgres's reads 10, 11 and 12 (5.6,
16.9 and 27.0 ms: 2.8, 1.7 and 1.5 times) and SQLite's read 11 (4.8 ms,
1.6 times). Figures in the notes below not said to be the container's
were measured earlier that day on GDK's Postgres 17 and its SQLite
copy, 37,498 rows.

- **A page reads twice its limit,** so the copies it leaves out seldom
  cost another round, and one left short reads on from where it stopped.
  Read 13 reads on once, so it runs two pages.
- **On Postgres, a page with a cursor reads the entries from a
  materialized CTE** of each view's hidden rows. Matched per view in a
  subquery, as SQLite's still are, the anti-join planned as a nested
  loop over the views per row: read 13's page statement took 372 ms in
  psql. On SQLite the CTE cost 80 ms a page.
- **A page's rows to compare are one statement,** with each row's
  entries aggregated beside it and the records' ids bound as one
  parameter. `+r.segment_id` keeps SQLite on the place index: probing
  each segment at each place took 3 to 4 times as long.
- **Ids are bound as an array on Postgres,** which its planner sees as
  a constant. Bound as text and cast, `= ANY` checked each row against
  every id in turn: 13.7 ms of the 500-record family's read 11 on the
  container.
- **Past 300 records, a read fetches every row in the covered segments,
  and the entries in them,** which at the defaults is every facet
  across: 31,498 rows.
- **Content compares as the hash each row is written with**
  (`content_hash`), so no JSON is read. Fetching and canonicalising the
  JSON of the rows compared, 17,316 of the 31,498 here, took 141 ms of
  read 11 on SQLite and 40 on Postgres. `bench.rs` fills in the hashes
  of the rows `generate.sql` made before it times anything.
- **The resolver numbers places and merge bases,** with each row's
  views and merge bases as bit sets, and takes 9 ms of read 11. Looking
  each place up by its strings took 146 ms on Postgres, 207 on SQLite.
- **What read 11 spends beyond the old rule** is reading the rows
  twice: as candidates, then to resolve. On SQLite that's 71 and 94 ms,
  and its counts take 103. Postgres reads them twice too, 19 and 38 ms
  on the container under `auto_explain`, but its old facet cost more,
  so its ratio is lower.
- **Read 10 on Postgres spends its time in the page's anti-join,** over
  each entry and view in the covered views: on the container, 73,864
  pairs, 12 ms, where the old rule matched on the row alone. The
  resolver's three statements take under half a millisecond.
- **Fetching JSON only for the rows kept wouldn't pay:** a page that
  returned none at all saved at most 1.8 ms of read 10 on the container,
  about what the statement fetching the kept rows' would cost.
