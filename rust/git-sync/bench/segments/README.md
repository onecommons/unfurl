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
| `../../src/db/bench.rs` | The store's own reads for queries 1–3, 5–7 and 10, on Postgres or the SQLite copy, each run six times warm, then once more (on Postgres, under `auto_explain`). |
| `to_sqlite.py` | Copies the generated database into a SQLite file built from `migrations/sqlite`, for the store's SQLite path. |
| `run.sh` | `run.sh generate\|queries\|all\|sqlite [var=value …]`. `generate` applies `migrations/postgres` and `generate.sql`, then makes the SQLite copy; `sqlite` makes the copy again on its own. Run it with no mode for the details. |

## Running it

It runs against the `unfurl-pg-jit` container (Postgres 17, JIT on) by
default. `BENCH_PG_URL` and `BENCH_DB` override the server and database.

```bash
docker run -d --rm --name unfurl-pg-jit -e POSTGRES_PASSWORD=pgjit \
    -p 55432:5432 postgres:17                  # if it isn't running

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

```bash
cargo test --features postgres --lib bench_segments_pg -- --ignored --nocapture
cargo test --lib bench_segments_sqlite -- --ignored --nocapture
```

At the defaults, generation takes about 20 seconds, `baseline.sql` about
30, and `check.sql` a few seconds.

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
used JIT.

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
