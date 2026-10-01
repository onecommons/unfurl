-- Synthetic data for the segment design (docs/branch-segments.md): one
-- family with a main branch, forks, user branches layered over main, and
-- drafts. Run after migrations/postgres, into an empty database:
--
--   psql "$URL" -v records=20000 -v users=300 -f generate.sql
--
-- Every parameter below has a default. Sampling hashes the record and
-- worktree numbers instead of calling random(), so the same parameters
-- always build the same database.

\set ON_ERROR_STOP on
\if :{?records}          \else \set records 20000          \endif
\if :{?types}            \else \set types 60               \endif
\if :{?main_segments}    \else \set main_segments 30       \endif
-- percent of main's records changed in each segment after the root
\if :{?churn_pct}        \else \set churn_pct 1            \endif
-- percent of those changes that are deletes
\if :{?delete_pct}       \else \set delete_pct 2           \endif
\if :{?forks}            \else \set forks 20               \endif
\if :{?fork_edits}       \else \set fork_edits 200         \endif
\if :{?fork_draft_edits} \else \set fork_draft_edits 5     \endif
\if :{?users}            \else \set users 300              \endif
\if :{?user_edits}       \else \set user_edits 20          \endif
-- percent of user edits made before main's later change to the same
-- record, so a layered read shows both copies
\if :{?conflict_pct}     \else \set conflict_pct 10        \endif
\if :{?main_draft_edits} \else \set main_draft_edits 50    \endif

\echo generating: records=:records types=:types main_segments=:main_segments churn_pct=:churn_pct forks=:forks users=:users

BEGIN;

-- A deterministic number in [0, 100) from any text.
CREATE FUNCTION pg_temp.pct(t text) RETURNS int
LANGUAGE sql IMMUTABLE AS $$ SELECT abs(hashtext(t)) % 100 $$;

-- One record's JSON, about 1.4 KB like the dev cloudmap's: a `type`
-- typeRef map, facetable tags and metadata, and a description.
CREATE FUNCTION pg_temp.rec_json(i int, rev text, ntypes int) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT jsonb_build_object(
        'name', 'record ' || i,
        'type', jsonb_build_object('T' || (1 + i % ntypes), NULL),
        'tags', jsonb_build_array('tag' || (i % 50), 'tag' || (i % 7 + 50)),
        'metadata', jsonb_build_object(
            'owner', 'team' || (i % 20),
            'tier', i % 3,
            'homepage_url', 'https://unfurl.cloud/org' || (i % 100) || '/p' || i),
        'revision', rev,
        'description', (SELECT string_agg(md5(i || ':' || rev || ':' || n), '')
                        FROM generate_series(1, 36) n))
$$;

CREATE TEMP SEQUENCE ver;

-- records: path by bucket, a /types row per type
CREATE TEMP TABLE keys AS
SELECT i,
       CASE WHEN i % 5 < 2 THEN '/artifacts'
            WHEN i % 5 = 2 THEN '/services'
            WHEN i % 5 = 3 THEN '/components'
            ELSE '/instantiations' END AS path,
       'k' || lpad(i::text, 7, '0') AS key
FROM generate_series(1, :records) i;

-- worktrees: 1 is main, then forks, then user branches
INSERT INTO worktree (id, origin, branch)
VALUES (1, 'unfurl.cloud/org/cloudmap', 'main');
INSERT INTO version_seq (worktree_id) VALUES (1);
UPDATE worktree SET family_id = 1 WHERE id = 1;

INSERT INTO worktree (id, origin, branch, family_id)
SELECT 1 + f, 'unfurl.cloud/fork' || f || '/cloudmap', 'main', 1
FROM generate_series(1, :forks) f;

INSERT INTO worktree (id, origin, branch, family_id)
SELECT 1 + :forks + u, 'unfurl.cloud/org/cloudmap', 'drafts/user' || u, 1
FROM generate_series(1, :users) u;

-- main's chain: segment 1 is the root, :main_segments is main's head
INSERT INTO segment (id, family_id, kind, parent_id, head_commit, owner_id)
SELECT g, 1, CASE WHEN g = :main_segments THEN 'head' ELSE 'internal' END,
       NULLIF(g - 1, 0), md5('main' || g),
       CASE WHEN g = :main_segments THEN 1 END
FROM generate_series(1, :main_segments) g;

-- every other worktree forks at a point inside main's chain
CREATE TEMP TABLE branch AS
SELECT w.id AS w,
       (w.id <= 1 + :forks) AS is_fork,
       1 + abs(hashtext('fork' || w.id)) % (:main_segments - 1) AS fork_seg
FROM worktree w WHERE w.id > 1;

-- segment ids: main's chain, then main's draft, then each branch's head
-- and draft
INSERT INTO segment (id, family_id, kind, owner_id)
VALUES (:main_segments + 1, 1, 'draft', 1);

ALTER TABLE branch ADD COLUMN head_seg bigint, ADD COLUMN draft_seg bigint;
UPDATE branch SET head_seg  = :main_segments + 2 * (w - 1),
                  draft_seg = :main_segments + 2 * (w - 1) + 1;

INSERT INTO segment (id, family_id, kind, parent_id, head_commit, owner_id)
SELECT head_seg, 1, 'head', fork_seg,
       CASE WHEN is_fork THEN md5('fork' || w) ELSE md5('main' || fork_seg) END, w
FROM branch;
INSERT INTO segment (id, family_id, kind, owner_id)
SELECT draft_seg, 1, 'draft', w FROM branch;
\o /dev/null
SELECT setval(pg_get_serial_sequence('segment', 'id'), (SELECT max(id) FROM segment));
\o

UPDATE worktree SET head_segment_id = :main_segments, draft_segment_id = :main_segments + 1
WHERE id = 1;
UPDATE worktree w SET head_segment_id = b.head_seg, draft_segment_id = b.draft_seg,
                      commit_id = (SELECT head_commit FROM segment WHERE id = b.head_seg)
FROM branch b WHERE w.id = b.w;
UPDATE worktree SET commit_id = md5('main' || :main_segments) WHERE id = 1;

-- committed chains
INSERT INTO worktree_segment (worktree_id, segment_id, inherited)
SELECT 1, g, FALSE FROM generate_series(1, :main_segments) g;
INSERT INTO worktree_segment (worktree_id, segment_id, inherited)
SELECT b.w, g, TRUE FROM branch b, generate_series(1, :main_segments) g
WHERE g <= b.fork_seg;
INSERT INTO worktree_segment (worktree_id, segment_id, inherited)
SELECT w, head_seg, FALSE FROM branch;

INSERT INTO file (worktree_id, path, format, commit_id, source_oid, committed_oid)
SELECT id, 'cloudmap.yaml', 'cloudmap', commit_id, md5('blob' || id), md5('blob' || id)
FROM worktree;

-- the root: every record, and the /types section with flattened extends
INSERT INTO record (key_id, segment_id, file_path, path, key, commit_id, json, version)
SELECT 0, 1, 'cloudmap.yaml', k.path, k.key, md5('main1'),
       pg_temp.rec_json(k.i, 'r0', :types), nextval('ver')
FROM keys k;

INSERT INTO record (key_id, segment_id, file_path, path, key, commit_id, json, version)
SELECT 0, 1, 'cloudmap.yaml', '/types', 'T' || t, md5('main1'),
       jsonb_build_object(
           'name', 'T' || t,
           -- a tree: T(n)'s parent is T(n / 3); extends lists itself first,
           -- then every ancestor up to T0
           'extends', (WITH RECURSIVE up(n) AS (
                           SELECT t UNION ALL SELECT n / 3 FROM up WHERE n > 0)
                       SELECT jsonb_agg('T' || n) FROM up)),
       nextval('ver')
FROM generate_series(0, :types) t;

UPDATE record SET key_id = id WHERE key_id = 0;
CREATE TEMP TABLE root_ids AS
SELECT path, key, id FROM record WHERE segment_id = 1;
CREATE UNIQUE INDEX ON root_ids (path, key);

-- main's later segments: :churn_pct of the records each, some deleted
INSERT INTO record (key_id, segment_id, file_path, path, key, commit_id, json,
                    deleted, version)
SELECT ri.id, g, 'cloudmap.yaml', k.path, k.key, md5('main' || g),
       pg_temp.rec_json(k.i, 'm' || g, :types),
       pg_temp.pct('del' || k.i || ':' || g) < :delete_pct, nextval('ver')
FROM generate_series(2, :main_segments) g
JOIN keys k ON pg_temp.pct('churn' || k.i || ':' || g) < :churn_pct
JOIN root_ids ri ON ri.path = k.path AND ri.key = k.key
ORDER BY g, k.i;

-- fork heads and drafts
INSERT INTO record (key_id, segment_id, file_path, path, key, commit_id, json, version)
SELECT ri.id, b.head_seg, 'cloudmap.yaml', k.path, k.key, md5('fork' || b.w),
       pg_temp.rec_json(k.i, 'f' || b.w, :types), nextval('ver')
FROM branch b
CROSS JOIN LATERAL (SELECT * FROM keys
                    ORDER BY md5('fork' || b.w || ':' || keys.i) LIMIT :fork_edits) k
JOIN root_ids ri ON ri.path = k.path AND ri.key = k.key
WHERE b.is_fork;

INSERT INTO record (key_id, segment_id, file_path, path, key, json, version)
SELECT ri.id, b.draft_seg, 'cloudmap.yaml', k.path, k.key,
       pg_temp.rec_json(k.i, 'fd' || b.w, :types), nextval('ver')
FROM branch b
CROSS JOIN LATERAL (SELECT * FROM keys
                    ORDER BY md5('forkdraft' || b.w || ':' || keys.i)
                    LIMIT :fork_draft_edits) k
JOIN root_ids ri ON ri.path = k.path AND ri.key = k.key
WHERE b.is_fork;

-- user branches: empty heads, edits in their drafts
INSERT INTO record (key_id, segment_id, file_path, path, key, json, version)
SELECT ri.id, b.draft_seg, 'cloudmap.yaml', k.path, k.key,
       pg_temp.rec_json(k.i, 'u' || b.w, :types), nextval('ver')
FROM branch b
CROSS JOIN LATERAL (SELECT * FROM keys
                    ORDER BY md5('user' || b.w || ':' || keys.i) LIMIT :user_edits) k
JOIN root_ids ri ON ri.path = k.path AND ri.key = k.key
WHERE NOT b.is_fork;

-- main's own draft
INSERT INTO record (key_id, segment_id, file_path, path, key, json, version)
SELECT ri.id, :main_segments + 1, 'cloudmap.yaml', k.path, k.key,
       pg_temp.rec_json(k.i, 'md', :types), nextval('ver')
FROM (SELECT * FROM keys ORDER BY md5('maindraft:' || i) LIMIT :main_draft_edits) k
JOIN root_ids ri ON ri.path = k.path AND ri.key = k.key;

-- supersession (I2). Ancestry of every committed segment, itself at
-- distance 0.
CREATE TEMP TABLE anc AS
WITH RECURSIVE a(seg, anc, dist) AS (
    SELECT id, id, 0 FROM segment WHERE kind <> 'draft'
    UNION ALL
    SELECT a.seg, s.parent_id, a.dist + 1
    FROM a JOIN segment s ON s.id = a.anc
    WHERE s.parent_id IS NOT NULL
)
SELECT * FROM a;
CREATE INDEX ON anc (seg);

-- a committed row supersedes the nearest version below its segment
INSERT INTO superseded (record_id, segment_id, key_id)
SELECT DISTINCT ON (h.id) p.id, h.segment_id, h.key_id
FROM record h
JOIN segment hs ON hs.id = h.segment_id AND hs.kind <> 'draft'
JOIN anc a ON a.seg = h.segment_id AND a.dist > 0
JOIN record p ON p.segment_id = a.anc AND p.file_path = h.file_path
             AND p.path = h.path AND p.key = h.key
ORDER BY h.id, a.dist;

-- a draft row supersedes the version its worktree's committed chain shows
INSERT INTO superseded (record_id, segment_id, key_id)
SELECT DISTINCT ON (h.id) p.id, h.segment_id, h.key_id
FROM record h
JOIN segment ds ON ds.id = h.segment_id AND ds.kind = 'draft'
JOIN worktree w ON w.id = ds.owner_id
JOIN anc a ON a.seg = w.head_segment_id
JOIN record p ON p.segment_id = a.anc AND p.file_path = h.file_path
             AND p.path = h.path AND p.key = h.key
ORDER BY h.id, a.dist
ON CONFLICT DO NOTHING;

-- a user branch's edit, written through the layered view, also supersedes
-- what main showed: main's draft row, else main's current committed row.
-- :conflict_pct of edits skip this, as if made before main's change, so a
-- layered read shows both copies wherever main has since changed the record.
INSERT INTO superseded (record_id, segment_id, key_id)
SELECT DISTINCT ON (h.id) p.id, h.segment_id, h.key_id
FROM record h
JOIN branch b ON b.draft_seg = h.segment_id AND NOT b.is_fork
JOIN LATERAL (
    SELECT md.id, -1 AS dist FROM record md
    WHERE md.segment_id = :main_segments + 1
      AND md.file_path = h.file_path AND md.path = h.path AND md.key = h.key
    UNION ALL
    SELECT mc.id, a.dist FROM anc a
    JOIN record mc ON mc.segment_id = a.anc AND mc.file_path = h.file_path
                  AND mc.path = h.path AND mc.key = h.key
    WHERE a.seg = :main_segments
) p ON TRUE
WHERE pg_temp.pct('conflict' || h.id) >= :conflict_pct
ORDER BY h.id, p.dist
ON CONFLICT DO NOTHING;

UPDATE version_seq SET next_version = (SELECT max(version) + 1 FROM record);

COMMIT;

ANALYZE;

SELECT (SELECT count(*) FROM worktree) AS worktrees,
       (SELECT count(*) FROM segment) AS segments,
       (SELECT count(*) FROM record) AS records,
       (SELECT count(*) FROM superseded) AS superseded,
       pg_size_pretty(pg_total_relation_size('record')) AS record_size,
       pg_size_pretty(pg_total_relation_size('superseded')) AS superseded_size;
