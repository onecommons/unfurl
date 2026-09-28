-- queries.sql's single-view queries in today's form, against the
-- flat_record table baseline.sql builds: the worktree clause from
-- db/record.rs, a type list and rollup pairs expanded by the server (the
-- types cache) and bound as constants. Numbering matches queries.sql.

\set ON_ERROR_STOP on
\pset pager off

SELECT 1 AS main, (SELECT max(version) - 1000 FROM record) AS cursor \gset
\set probe_key 'k0000123'
\set tag_filter '{"tags": ["tag7"]}'

-- what the types cache would hand the query
SELECT '{' || string_agg(key, ',') || '}' AS subtypes
FROM flat_record
WHERE worktree_id = 1 AND path = '/types' AND json -> 'extends' ? 'T2'
\gset
SELECT '{' || string_agg(t.key, ',') || '}' AS decls,
       '{' || string_agg(e.anc, ',') || '}' AS ancs
FROM flat_record t
CROSS JOIN LATERAL jsonb_array_elements_text(t.json -> 'extends') e(anc)
WHERE t.worktree_id = 1 AND t.path = '/types'
\gset

SHOW jit;

\echo
\echo ==== 1. get one record
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
SELECT r.id, r.json, r.version, r.deleted
FROM flat_record r
WHERE r.worktree_id IN (SELECT w.id FROM worktree w WHERE w.id = :main)
  AND r.conflict IS NULL
  AND r.file_path = 'cloudmap.yaml' AND r.path = '/artifacts' AND r.key = :'probe_key';

\echo
\echo ==== 2. find with a JSON filter, one page
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
SELECT r.id, r.file_path, r.path, r.key, r.json, r.version
FROM flat_record r
WHERE r.worktree_id IN (SELECT w.id FROM worktree w WHERE w.id = :main)
  AND r.conflict IS NULL AND NOT r.deleted
  AND r.json @> :'tag_filter'::jsonb
ORDER BY r.path COLLATE "C", r.key COLLATE "C", r.file_path COLLATE "C",
         r.worktree_id, r.id
LIMIT 50;

\echo
\echo ==== 3. find with ?type=T2 and its subtypes, expanded by the server
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
SELECT r.id, r.path, r.key, r.json
FROM flat_record r
WHERE r.worktree_id IN (SELECT w.id FROM worktree w WHERE w.id = :main)
  AND r.conflict IS NULL AND NOT r.deleted
  AND r.json -> 'type' ?| CAST(:'subtypes' AS text[])
ORDER BY r.path COLLATE "C", r.key COLLATE "C", r.file_path COLLATE "C",
         r.worktree_id, r.id
LIMIT 50;

\echo
\echo ==== 4. facet total
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
SELECT COUNT(*)
FROM flat_record r
WHERE r.worktree_id IN (SELECT w.id FROM worktree w WHERE w.id = :main)
  AND r.conflict IS NULL AND NOT r.deleted
  AND r.path <> '/types';

\echo
\echo ==== 5. facet group counts by type, subtypes rolled up
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
SELECT COALESCE(to_jsonb(mg.anc), jg.val) AS g0, COUNT(DISTINCT r.id) AS n
FROM flat_record r
CROSS JOIN LATERAL facet_values(r.json #> '{type}') jg(val)
LEFT JOIN unnest(CAST(:'decls' AS text[]), CAST(:'ancs' AS text[])) mg(decl, anc)
       ON to_jsonb(mg.decl) = jg.val
WHERE r.worktree_id IN (SELECT w.id FROM worktree w WHERE w.id = :main)
  AND r.conflict IS NULL AND NOT r.deleted
  AND r.path <> '/types'
GROUP BY g0;

\echo
\echo ==== 6. facet column: type (rolled up) x metadata.tier x tags
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
SELECT COALESCE(to_jsonb(m0.anc), jg.val) AS g0, j1.val AS v1, j2.val AS v2,
       COUNT(DISTINCT r.id) AS n
FROM flat_record r
CROSS JOIN LATERAL facet_values(r.json #> '{type}') jg(val)
LEFT JOIN unnest(CAST(:'decls' AS text[]), CAST(:'ancs' AS text[])) m0(decl, anc)
       ON to_jsonb(m0.decl) = jg.val
CROSS JOIN LATERAL facet_values(r.json #> '{metadata,tier}') j1(val)
CROSS JOIN LATERAL facet_values(r.json #> '{tags}') j2(val)
WHERE r.worktree_id IN (SELECT w.id FROM worktree w WHERE w.id = :main)
  AND r.conflict IS NULL AND NOT r.deleted
  AND r.path <> '/types'
GROUP BY g0, v1, v2;

\echo
\echo ==== 7. list_changes since a cursor
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
SELECT r.id, r.path, r.key, r.json, r.deleted, r.version
FROM flat_record r
WHERE r.worktree_id = :main AND r.version > :cursor AND r.conflict IS NULL
ORDER BY r.version;

\echo
\echo ==== 10. find across worktrees: every branch named main (main + forks)
EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
SELECT r.worktree_id, r.id, r.path, r.key, r.json
FROM flat_record r
WHERE r.worktree_id IN (SELECT w.id FROM worktree w WHERE w.branch = 'main')
  AND r.conflict IS NULL AND NOT r.deleted
  AND r.json @> :'tag_filter'::jsonb
ORDER BY r.path COLLATE "C", r.key COLLATE "C", r.file_path COLLATE "C",
         r.worktree_id, r.id
LIMIT 50;
