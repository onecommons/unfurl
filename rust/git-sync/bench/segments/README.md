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

## Results, 2026-09-27

At the defaults: 20,000 records plus 61 types, main's chain of 30
segments at 1% churn each, 20 forks, 300 user branches, and 10% of user
edits in conflict with main. Postgres 17.11 on arm64, with warm caches.
Each figure is the range over the last two of three runs.

| Size | Segments | Today |
|---|---|---|
| Record storage | 112 MB for everything, 300 user branches included | 730 MB for main and 20 forks; user branches can't be represented |

| Query (ms) | Segments | Today |
|---|---|---|
| 1. get one record | 0.14 | 0.05–0.07 |
| 2. find, JSON filter, one page | 7–14 | 9–10 |
| 3. find, `?type=T2` and subtypes | 110–135 | 65–79 |
| 4. facet total | 27–38 | 76–81 |
| 5. facet by type, rolled up | 340–353 | 403–465 |
| 6. facet column, 3 dimensions | 1,642–1,651 | 1,724–1,766 |
| 7. `list_changes` | 11 | 83–85 |
| 8. layered read, one section | 28–31 | — |
| 9. layered `list_changes` | 117–128 | — |
| 10. find across main and forks | 50–53 | 16.5 |
| 10b. the same, JSON fetched only for the page | 22–23 | — |
| 11. layered facet by type | 158–169 | — |

No plan in either layout used JIT.

## Findings

- **Aggregates are the same or faster.** Facets make one pass over a
  view, and the visibility anti-join is a small part of it. In query 6 it
  takes 27 ms of the 1.65 s; the rest is the facet itself, 175k cells
  sorted for `COUNT(DISTINCT)`, in either layout.
- **`list_changes` is 8× faster,** thanks to the `(segment_id, version)`
  index. Today's schema has no version index.
- **Point reads and single-view finds cost a little more.** The JSON
  index covers every branch's versions, so a filter's candidates include
  versions from other segments, each fetched before the view filters it
  out.
- **In a cross-worktree anti-join, the worktree match must go in
  `WHERE`.** In the join's `ON`, Postgres runs the `NOT EXISTS` as a
  subplan per (worktree, candidate) pair; in `WHERE`, it becomes a hash
  anti-join. The design's C.7 now uses the `WHERE` form. That took query
  10 from 86 ms to 50 ms.
- **Fetching JSON only for the page** takes it to 22 ms, against 16.5 ms
  today. The anti-join hashes about 10k candidates, and carrying 1.4 KB of
  JSON through that costs more than looking the page's rows up again.
  Finds should select ids and sort keys first.
- **The type filter is still about 1.7× slower.** Its expanded type list
  comes from an InitPlan, so the planner can't see its size. It estimated
  29 matching rows against 6,636, and rescanned the GIN index once per
  segment. Things to try:
  - expand the list in a separate first query, and bind it as a constant,
    as today;
  - fetch JSON late here too.
