-- Today's layout, for comparison: every worktree holds a full copy of its
-- records (migrations/postgres), built from a database generate.sql made.
-- Main and the forks get copies; user branches don't exist today.
--
--   psql "$URL/$DB" -f baseline.sql && psql "$URL/$DB" -f baseline_queries.sql

\set ON_ERROR_STOP on

DROP TABLE IF EXISTS flat_record;
CREATE TABLE flat_record (
    id             BIGSERIAL PRIMARY KEY,
    worktree_id    BIGINT  NOT NULL,
    file_path      TEXT    NOT NULL,
    path           TEXT    NOT NULL,
    key            TEXT    NOT NULL,
    commit_id      TEXT,
    json           JSONB   NOT NULL,
    deleted        BOOLEAN NOT NULL DEFAULT FALSE,
    version        BIGINT  NOT NULL DEFAULT 0,
    base_commit_id TEXT,
    conflict       TEXT
);

-- each worktree's visible rows, as its own copy
WITH views AS (
    SELECT worktree_id AS w, segment_id FROM worktree_segment
    WHERE worktree_id IN (SELECT id FROM worktree WHERE branch = 'main')
    UNION ALL
    SELECT id, draft_segment_id FROM worktree WHERE branch = 'main'
)
INSERT INTO flat_record (worktree_id, file_path, path, key, commit_id, json,
                         deleted, version)
SELECT vw.w, r.file_path, r.path, r.key, r.commit_id, r.json, r.deleted, r.version
FROM views vw
JOIN record r ON r.segment_id = vw.segment_id
WHERE r.conflict IS NULL
  AND NOT EXISTS (SELECT 1 FROM superseded x
                  JOIN views vx ON vx.segment_id = x.segment_id AND vx.w = vw.w
                  WHERE x.record_id = r.id);

-- today's indexes
CREATE INDEX flat_idx_record_worktree_path ON flat_record(worktree_id, path, key);
CREATE INDEX flat_idx_record_file          ON flat_record(worktree_id, file_path);
CREATE UNIQUE INDEX flat_uq_record_path    ON flat_record(worktree_id, file_path, path, key)
    WHERE conflict IS NULL;
CREATE INDEX flat_idx_record_type_gin      ON flat_record USING GIN ((json -> 'type'));
CREATE INDEX flat_idx_record_json_gin      ON flat_record USING GIN (json jsonb_path_ops);

ANALYZE flat_record;

SELECT count(*) AS flat_rows,
       pg_size_pretty(pg_total_relation_size('flat_record')) AS flat_size,
       pg_size_pretty(pg_total_relation_size('record')
                      + pg_total_relation_size('superseded')) AS segment_size
FROM flat_record;
