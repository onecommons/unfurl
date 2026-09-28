# Segment design: query-plan benchmark

Synthetic data and the query set for [the segment design](../../docs/branch-segments.md),
with a baseline in today's layout to compare against. This isn't wired
into any test suite: it's for checking plans and timings by hand, before
and after a change.

## Files

| File | What it does |
|---|---|
| `schema.sql` | The design's draft schema (Appendix B) and `facet_values`. |
| `generate.sql` | One family: main's chain of segments with churn, forks, user branches layered over main, and drafts. Parameters are psql variables; see the top of the file. Sampling is hash-based, so the same parameters always build the same database. |
| `check.sql` | Checks invariant I3 on a sample of views, and counts the copies that layered views show. |
| `queries.sql` | Appendix C's queries under `EXPLAIN (ANALYZE, BUFFERS, SETTINGS)`. |
| `baseline.sql` | Today's layout, a full copy per worktree, built from the generated data for main and the forks. |
| `baseline_queries.sql` | The single-view queries in today's form, against that copy. |
| `run.sh` | `run.sh [generate\|queries\|all] [var=value …]` |

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

At the defaults, generation takes about 20 seconds, `baseline.sql` about
30, and `check.sql` a few seconds.

## Results, 2026-09-28

At the defaults: 20,000 records plus 61 types, main's chain of 30
segments at 1% churn each, 20 forks, 300 user branches, and 10% of user
edits in conflict with main. Postgres 17.11 on arm64, with warm caches.
Each figure is the range over the last two of three runs of one layout,
since alternating the layouts evicts each other's pages.

| Size | Segments | Today |
|---|---|---|
| Record storage | 112 MB for everything, 300 user branches included | 730 MB for main and 20 forks; user branches can't be represented |

| Query (ms) | Segments | Today |
|---|---|---|
| 1. get one record | 0.08–0.14 | 0.04–0.05 |
| 2. find, JSON filter, one page | 3.6–3.9 | 5.4–5.7 |
| 3. find, `?type=T2` and subtypes, list expanded in the statement | 48–49 | 54–55 |
| 3b. the same, list expanded by a first query, JSON fetched only for the page | 22–23, plus 2 to expand | — |
| 4. facet total | 19–22 | 73–74 |
| 5. facet by type, rolled up | 389–392 | 457–464 |
| 6. facet column, 3 dimensions | 1,954–1,964 | 2,081–2,084 |
| 7. `list_changes` | 12 | 80–98 |
| 8. layered read, one section | 20–23 | — |
| 9. layered `list_changes` | 140–142 | — |
| 10. find across main and forks | 46–51 | 16–17 |
| 10b. the same, JSON fetched only for the page | 20–22 | — |
| 11. layered facet by type | 190–213 | — |

No plan in either layout used JIT.

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
