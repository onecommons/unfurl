-- The draft schema from docs/branch-segments.md, Appendix B, plus the
-- facet_values function from migrations/postgres. Keep it in step with
-- the design doc until the schema moves into real migrations.

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

-- The facet values of one extracted path, per the extraction rule in
-- db::record::facet_lateral_pg: an array's elements, an object's keys, or a
-- scalar itself; a missing path (SQL NULL) yields nothing.
--
-- A function so the planner can be told how many rows to expect: it guesses
-- 100 for each built-in set-returning function, and a facet query crosses one
-- expansion per column, so three columns over a few records were estimated in
-- the tens of millions -- enough to trigger JIT compilation, which then took
-- far longer than the query. STRICT keeps it from being inlined, which would
-- put that guess back.
CREATE FUNCTION facet_values(v jsonb) RETURNS SETOF jsonb
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE ROWS 2 AS $$
    SELECT e FROM jsonb_array_elements(
        CASE WHEN jsonb_typeof(v) = 'array' THEN v ELSE '[]'::jsonb END) e
    UNION ALL
    SELECT to_jsonb(k) FROM jsonb_object_keys(
        CASE WHEN jsonb_typeof(v) = 'object' THEN v ELSE '{}'::jsonb END) k
    UNION ALL
    SELECT v WHERE jsonb_typeof(v) NOT IN ('array', 'object')
$$;
