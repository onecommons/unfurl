-- Sanity checks on a generated database: invariant I3 (a worktree's own
-- view shows each record at most once) and what layered views show.
-- Checked on a sample (main, three forks, five user branches): every view
-- at once is millions of rows.

\set ON_ERROR_STOP on

CREATE TEMP TABLE sample AS
SELECT 1 AS id
UNION ALL (SELECT id FROM worktree WHERE branch = 'main' AND id > 1 ORDER BY id LIMIT 3)
UNION ALL (SELECT id FROM worktree WHERE branch LIKE 'drafts/%' ORDER BY id LIMIT 5);

-- I3: must return no rows
WITH views AS (
    SELECT worktree_id AS w, segment_id FROM worktree_segment
    WHERE worktree_id IN (SELECT id FROM sample)
    UNION ALL
    SELECT id, draft_segment_id FROM worktree WHERE id IN (SELECT id FROM sample)
),
visible AS (
    SELECT vw.w, r.file_path, r.path, r.key
    FROM views vw
    JOIN record r ON r.segment_id = vw.segment_id
    WHERE r.conflict IS NULL
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN views vx ON vx.segment_id = x.segment_id AND vx.w = vw.w
                      WHERE x.record_id = r.id)
)
SELECT w, file_path, path, key, count(*) AS rows
FROM visible
GROUP BY w, file_path, path, key
HAVING count(*) > 1
LIMIT 10;

-- every worktree sees every record (tombstones included), and main's
-- view matches the record count
WITH views AS (
    SELECT worktree_id AS w, segment_id FROM worktree_segment
    WHERE worktree_id IN (SELECT id FROM sample)
    UNION ALL
    SELECT id, draft_segment_id FROM worktree WHERE id IN (SELECT id FROM sample)
)
SELECT min(n) AS fewest_visible, max(n) AS most_visible
FROM (
    SELECT vw.w, count(*) AS n
    FROM views vw
    JOIN record r ON r.segment_id = vw.segment_id
    WHERE r.conflict IS NULL
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN views vx ON vx.segment_id = x.segment_id AND vx.w = vw.w
                      WHERE x.record_id = r.id)
    GROUP BY vw.w
) per_worktree;

-- layered views (main + each user branch): records shown twice
WITH users AS (SELECT id FROM worktree WHERE branch LIKE 'drafts/%'
               AND id IN (SELECT id FROM sample)),
v AS (
    SELECT u.id AS u, ws.segment_id FROM users u
    JOIN worktree_segment ws ON ws.worktree_id = 1
    UNION ALL
    SELECT u.id, (SELECT draft_segment_id FROM worktree WHERE id = 1) FROM users u
    UNION ALL
    SELECT u.id, ws.segment_id FROM users u
    JOIN worktree_segment ws ON ws.worktree_id = u.id AND NOT ws.inherited
    UNION ALL
    SELECT u.id, w.draft_segment_id FROM users u JOIN worktree w ON w.id = u.id
)
SELECT count(DISTINCT u) AS user_views, count(*) AS duplicated_records
FROM (
    SELECT v.u, r.path, r.key
    FROM v JOIN record r ON r.segment_id = v.segment_id
    WHERE r.conflict IS NULL
      AND NOT EXISTS (SELECT 1 FROM superseded x
                      JOIN v vx ON vx.segment_id = x.segment_id AND vx.u = v.u
                      WHERE x.record_id = r.id)
    GROUP BY v.u, r.path, r.key
    HAVING count(*) > 1
) dup;
