-- The design's queries (docs/branch-segments.md, Appendix C) under
-- EXPLAIN ANALYZE, against a database built by generate.sql. What to look
-- for: a bitmap scan on the GIN indexes where a JSON filter applies, hash
-- or nested-loop joins on the view, no nested loop over the anti-join, and
-- no "JIT:" section.

\set ON_ERROR_STOP on
\pset pager off

-- worktrees and values to query with: main, one user branch, one fork
SELECT 1 AS main,
       (SELECT min(id) FROM worktree WHERE branch LIKE 'drafts/%') AS user_wt,
       (SELECT min(id) FROM worktree WHERE branch = 'main' AND id > 1) AS fork_wt,
       (SELECT max(version) - 1000 FROM record) AS cursor
\gset
\set probe_key 'k0000121'
-- in /components, so a miss in /artifacts
\set miss_key 'k0000123'
\set tag_filter '{"tags": ["tag7"]}'
\set type_names '{T2}'

SHOW jit;
SHOW jit_above_cost;

\echo
\echo ==== 1. get one record, main view
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
WITH v AS (
    SELECT segment_id, added_version FROM worktree_segment WHERE worktree_id = :main
    UNION ALL
    SELECT draft_segment_id, 0 FROM worktree WHERE id = :main
)
SELECT r.key_id, r.json, r.version, r.deleted
FROM record r
JOIN v ON v.segment_id = r.segment_id
WHERE r.conflict IS NULL
  AND NOT EXISTS (SELECT 1 FROM superseded x
                  JOIN v vx ON vx.segment_id = x.segment_id
                  WHERE x.record_id = r.id)
  AND r.file_path = 'cloudmap.yaml' AND r.path = '/artifacts' AND r.key = :'probe_key';

\echo
\echo ==== 1b. get a key the section lacks, main view
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
WITH v AS (
    SELECT segment_id, added_version FROM worktree_segment WHERE worktree_id = :main
    UNION ALL
    SELECT draft_segment_id, 0 FROM worktree WHERE id = :main
)
SELECT r.key_id, r.json, r.version, r.deleted
FROM record r
JOIN v ON v.segment_id = r.segment_id
WHERE r.conflict IS NULL
  AND NOT EXISTS (SELECT 1 FROM superseded x
                  JOIN v vx ON vx.segment_id = x.segment_id
                  WHERE x.record_id = r.id)
  AND r.file_path = 'cloudmap.yaml' AND r.path = '/artifacts' AND r.key = :'miss_key';

\echo
\echo ==== 2. find with a JSON filter, one page, main view
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
WITH v AS (
    SELECT segment_id, added_version FROM worktree_segment WHERE worktree_id = :main
    UNION ALL
    SELECT draft_segment_id, 0 FROM worktree WHERE id = :main
)
SELECT r.key_id, r.file_path, r.path, r.key, r.json, r.version
FROM record r
JOIN v ON v.segment_id = r.segment_id
WHERE r.conflict IS NULL
  AND NOT EXISTS (SELECT 1 FROM superseded x
                  JOIN v vx ON vx.segment_id = x.segment_id
                  WHERE x.record_id = r.id)
  AND NOT r.deleted
  AND r.json @> :'tag_filter'::jsonb
ORDER BY r.path COLLATE "C", r.key COLLATE "C", r.file_path COLLATE "C", r.id
LIMIT 50;

\echo
\echo ==== 3. find with ?type=T2 and its subtypes, main view
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
WITH v AS (
    SELECT segment_id, added_version FROM worktree_segment WHERE worktree_id = :main
    UNION ALL
    SELECT draft_segment_id, 0 FROM worktree WHERE id = :main
),
types AS (
    SELECT t.key, t.json
    FROM record t
    JOIN v ON v.segment_id = t.segment_id
    WHERE t.path = '/types' AND t.conflict IS NULL AND NOT t.deleted
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN v vx ON vx.segment_id = x.segment_id
                      WHERE x.record_id = t.id)
),
wanted AS (
    SELECT array_agg(DISTINCT name) AS names
    FROM (SELECT unnest(CAST(:'type_names' AS text[])) AS name
          UNION
          SELECT key FROM types
          WHERE json -> 'extends' ?| CAST(:'type_names' AS text[])) n
)
SELECT r.key_id, r.path, r.key, r.json
FROM record r
JOIN v ON v.segment_id = r.segment_id
WHERE r.conflict IS NULL
  AND NOT EXISTS (SELECT 1 FROM superseded x
                  JOIN v vx ON vx.segment_id = x.segment_id
                  WHERE x.record_id = r.id)
  AND NOT r.deleted
  AND r.json -> 'type' ?| (SELECT names FROM wanted)
ORDER BY r.path COLLATE "C", r.key COLLATE "C", r.file_path COLLATE "C", r.id
LIMIT 50;

\echo
\echo ==== 3b. find with ?type=T2, the list expanded by a first query and bound as a constant, JSON fetched only for the page
\echo expansion:
\timing on
WITH v AS (
    SELECT segment_id FROM worktree_segment WHERE worktree_id = :main
    UNION ALL
    SELECT draft_segment_id FROM worktree WHERE id = :main
)
SELECT '{' || string_agg(DISTINCT name, ',') || '}' AS subtypes
FROM (SELECT unnest(CAST(:'type_names' AS text[])) AS name
      UNION
      SELECT t.key
      FROM record t
      JOIN v ON v.segment_id = t.segment_id
      WHERE t.path = '/types' AND t.conflict IS NULL AND NOT t.deleted
        AND t.json -> 'extends' ?| CAST(:'type_names' AS text[])
        AND NOT EXISTS (SELECT 1 FROM superseded x
                        JOIN v vx ON vx.segment_id = x.segment_id
                        WHERE x.record_id = t.id)) n
\gset
\timing off
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
WITH v AS (
    SELECT segment_id FROM worktree_segment WHERE worktree_id = :main
    UNION ALL
    SELECT draft_segment_id FROM worktree WHERE id = :main
),
page AS (
    SELECT r.id, r.path, r.key, r.file_path
    FROM record r
    JOIN v ON v.segment_id = r.segment_id
    WHERE r.conflict IS NULL
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN v vx ON vx.segment_id = x.segment_id
                      WHERE x.record_id = r.id)
      AND NOT r.deleted
      AND r.json -> 'type' ?| CAST(:'subtypes' AS text[])
    ORDER BY r.path COLLATE "C", r.key COLLATE "C", r.file_path COLLATE "C", r.id
    LIMIT 50
)
SELECT r.key_id, r.path, r.key, r.json
FROM page p
JOIN record r ON r.id = p.id
ORDER BY p.path COLLATE "C", p.key COLLATE "C", p.file_path COLLATE "C", p.id;

\echo
\echo ==== 4. facet total, main view
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
WITH v AS (
    SELECT segment_id, added_version FROM worktree_segment WHERE worktree_id = :main
    UNION ALL
    SELECT draft_segment_id, 0 FROM worktree WHERE id = :main
)
SELECT COUNT(DISTINCT r.id)
FROM record r
JOIN v ON v.segment_id = r.segment_id
WHERE r.conflict IS NULL
  AND NOT EXISTS (SELECT 1 FROM superseded x
                  JOIN v vx ON vx.segment_id = x.segment_id
                  WHERE x.record_id = r.id)
  AND NOT r.deleted
  AND r.path <> '/types';

\echo
\echo ==== 5. facet group counts by type, subtypes rolled up, main view
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
WITH v AS (
    SELECT segment_id, added_version FROM worktree_segment WHERE worktree_id = :main
    UNION ALL
    SELECT draft_segment_id, 0 FROM worktree WHERE id = :main
),
types AS (
    SELECT t.key, t.json
    FROM record t
    JOIN v ON v.segment_id = t.segment_id
    WHERE t.path = '/types' AND t.conflict IS NULL AND NOT t.deleted
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN v vx ON vx.segment_id = x.segment_id
                      WHERE x.record_id = t.id)
),
pairs AS (
    SELECT key AS decl, jsonb_array_elements_text(json -> 'extends') AS anc
    FROM types
),
vis AS (
    SELECT r.*
    FROM record r
    JOIN v ON v.segment_id = r.segment_id
    WHERE r.conflict IS NULL
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN v vx ON vx.segment_id = x.segment_id
                      WHERE x.record_id = r.id)
      AND NOT r.deleted
      AND r.path <> '/types'
)
SELECT COALESCE(to_jsonb(mg.anc), jg.val) AS g0, COUNT(DISTINCT r.id) AS n
FROM vis r
CROSS JOIN LATERAL facet_values(r.json #> '{type}') jg(val)
LEFT JOIN pairs mg ON to_jsonb(mg.decl) = jg.val
GROUP BY g0;

\echo
\echo ==== 6. facet column: type (rolled up) x metadata.tier x tags, main view
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
WITH v AS (
    SELECT segment_id, added_version FROM worktree_segment WHERE worktree_id = :main
    UNION ALL
    SELECT draft_segment_id, 0 FROM worktree WHERE id = :main
),
types AS (
    SELECT t.key, t.json
    FROM record t
    JOIN v ON v.segment_id = t.segment_id
    WHERE t.path = '/types' AND t.conflict IS NULL AND NOT t.deleted
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN v vx ON vx.segment_id = x.segment_id
                      WHERE x.record_id = t.id)
),
pairs AS (
    SELECT key AS decl, jsonb_array_elements_text(json -> 'extends') AS anc
    FROM types
),
vis AS (
    SELECT r.*
    FROM record r
    JOIN v ON v.segment_id = r.segment_id
    WHERE r.conflict IS NULL
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN v vx ON vx.segment_id = x.segment_id
                      WHERE x.record_id = r.id)
      AND NOT r.deleted
      AND r.path <> '/types'
)
SELECT COALESCE(to_jsonb(m0.anc), jg.val) AS g0, j1.val AS v1, j2.val AS v2,
       COUNT(DISTINCT r.id) AS n
FROM vis r
CROSS JOIN LATERAL facet_values(r.json #> '{type}') jg(val)
LEFT JOIN pairs m0 ON to_jsonb(m0.decl) = jg.val
CROSS JOIN LATERAL facet_values(r.json #> '{metadata,tier}') j1(val)
CROSS JOIN LATERAL facet_values(r.json #> '{tags}') j2(val)
GROUP BY g0, v1, v2;

\echo
\echo ==== 7. list_changes since a cursor, main view
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
WITH v AS (
    SELECT segment_id, added_version FROM worktree_segment WHERE worktree_id = :main
    UNION ALL
    SELECT draft_segment_id, 0 FROM worktree WHERE id = :main
)
SELECT r.key_id, r.path, r.key, r.json, r.deleted, r.version
FROM record r
JOIN v ON v.segment_id = r.segment_id
WHERE r.conflict IS NULL
  AND NOT EXISTS (SELECT 1 FROM superseded x
                  JOIN v vx ON vx.segment_id = x.segment_id
                  WHERE x.record_id = r.id)
  AND (r.version > :cursor OR v.added_version > :cursor)
ORDER BY r.version;

\echo
\echo ==== 8. layered read: main + a user branch, one section, with copy counts
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
WITH v AS (
    SELECT segment_id, added_version, CAST(:main AS bigint) AS layer,
           FALSE AS upper_draft
    FROM worktree_segment WHERE worktree_id = :main
    UNION
    SELECT draft_segment_id, 0, id, FALSE FROM worktree WHERE id = :main
    UNION
    SELECT segment_id, added_version, worktree_id, FALSE
    FROM worktree_segment WHERE worktree_id = :user_wt AND NOT inherited
    UNION
    SELECT draft_segment_id, 0, id, TRUE FROM worktree WHERE id = :user_wt
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
-- the window sorts ids and keys; JSON joins on after, so the sort
-- stays in memory
counted AS (
    SELECT v.layer, r.id, count(*) OVER (PARTITION BY r.path, r.key) AS copies
    FROM record r
    JOIN v ON v.segment_id = r.segment_id
    WHERE r.conflict IS NULL
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN v vx ON vx.segment_id = x.segment_id
                      WHERE x.record_id = r.id)
      -- copies merged by key_id
      AND NOT EXISTS (SELECT 1 FROM hidden h WHERE h.id = r.id)
      AND NOT r.deleted
      AND r.path = '/services'
)
SELECT c.layer, r.key_id, r.path, r.key, r.json, c.copies
FROM counted c
JOIN record r ON r.id = c.id;

\echo
\echo ==== 9. layered list_changes: every copy of each changed record
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
WITH v AS (
    SELECT segment_id, added_version, CAST(:main AS bigint) AS layer,
           FALSE AS upper_draft
    FROM worktree_segment WHERE worktree_id = :main
    UNION
    SELECT draft_segment_id, 0, id, FALSE FROM worktree WHERE id = :main
    UNION
    SELECT segment_id, added_version, worktree_id, FALSE
    FROM worktree_segment WHERE worktree_id = :user_wt AND NOT inherited
    UNION
    SELECT draft_segment_id, 0, id, TRUE FROM worktree WHERE id = :user_wt
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
visible AS (
    SELECT r.*, v.layer, v.added_version
    FROM record r
    JOIN v ON v.segment_id = r.segment_id
    WHERE r.conflict IS NULL
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN v vx ON vx.segment_id = x.segment_id
                      WHERE x.record_id = r.id)
      -- copies merged by key_id
      AND NOT EXISTS (SELECT 1 FROM hidden h WHERE h.id = r.id)
),
counted AS (
    SELECT visible.*,
           count(*) OVER (PARTITION BY path, key) AS copies
    FROM visible
)
SELECT c.layer, c.key_id, c.path, c.key, c.deleted, c.version, c.copies
FROM counted c
WHERE EXISTS (SELECT 1 FROM visible ch
              WHERE ch.path = c.path AND ch.key = c.key
                AND (ch.version > :cursor OR ch.added_version > :cursor))
ORDER BY c.version;

\echo
\echo ==== 10. find across worktrees: every branch named main (main + forks)
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
WITH v AS (
    SELECT ws.worktree_id, ws.segment_id
    FROM worktree_segment ws
    JOIN worktree w ON w.id = ws.worktree_id
    WHERE w.branch = 'main'
    UNION ALL
    SELECT w.id, w.draft_segment_id
    FROM worktree w
    WHERE w.branch = 'main'
)
SELECT v.worktree_id, r.key_id, r.path, r.key, r.json
FROM v
JOIN record r ON r.segment_id = v.segment_id
WHERE r.conflict IS NULL
  AND NOT r.deleted
  AND r.json @> :'tag_filter'::jsonb
  -- the worktree match goes in WHERE, not in the join's ON: Postgres then
  -- turns the NOT EXISTS into a hash anti-join instead of a subplan per row
  AND NOT EXISTS (SELECT 1 FROM superseded x
                  JOIN v vx ON vx.segment_id = x.segment_id
                  WHERE x.record_id = r.id AND vx.worktree_id = v.worktree_id)
ORDER BY r.path COLLATE "C", r.key COLLATE "C", r.file_path COLLATE "C",
         v.worktree_id, r.id
LIMIT 50;

\echo
\echo ==== 11. layered facet by type, counting records by (path, key)
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
WITH v AS (
    SELECT segment_id, added_version, FALSE AS upper_draft
    FROM worktree_segment WHERE worktree_id = :main
    UNION
    SELECT draft_segment_id, 0, FALSE FROM worktree WHERE id = :main
    UNION
    SELECT segment_id, added_version, FALSE
    FROM worktree_segment WHERE worktree_id = :user_wt AND NOT inherited
    UNION
    SELECT draft_segment_id, 0, TRUE FROM worktree WHERE id = :user_wt
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
vis AS (
    SELECT r.*
    FROM record r
    JOIN v ON v.segment_id = r.segment_id
    WHERE r.conflict IS NULL
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN v vx ON vx.segment_id = x.segment_id
                      WHERE x.record_id = r.id)
      -- copies merged by key_id
      AND NOT EXISTS (SELECT 1 FROM hidden h WHERE h.id = r.id)
      AND NOT r.deleted
      AND r.path <> '/types'
)
SELECT jg.val AS g0, COUNT(DISTINCT (r.path, r.key)) AS n
FROM vis r
CROSS JOIN LATERAL facet_values(r.json #> '{type}') jg(val)
GROUP BY g0;

\echo
\echo ==== 10b. find across worktrees, JSON fetched only for the page
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
WITH v AS (
    SELECT ws.worktree_id, ws.segment_id
    FROM worktree_segment ws
    JOIN worktree w ON w.id = ws.worktree_id
    WHERE w.branch = 'main'
    UNION ALL
    SELECT w.id, w.draft_segment_id
    FROM worktree w
    WHERE w.branch = 'main'
),
page AS (
    SELECT v.worktree_id, r.id, r.path, r.key, r.file_path
    FROM v
    JOIN record r ON r.segment_id = v.segment_id
    WHERE r.conflict IS NULL
      AND NOT r.deleted
      AND r.json @> :'tag_filter'::jsonb
      -- the worktree match goes in WHERE, not in the join's ON: Postgres then
      -- turns the NOT EXISTS into a hash anti-join instead of a subplan per row
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN v vx ON vx.segment_id = x.segment_id
                      WHERE x.record_id = r.id AND vx.worktree_id = v.worktree_id)
    ORDER BY r.path COLLATE "C", r.key COLLATE "C", r.file_path COLLATE "C",
             v.worktree_id, r.id
    LIMIT 50
)
SELECT p.worktree_id, r.key_id, r.path, r.key, r.json
FROM page p
JOIN record r ON r.id = p.id
ORDER BY p.path COLLATE "C", p.key COLLATE "C", p.file_path COLLATE "C",
         p.worktree_id, p.id;
