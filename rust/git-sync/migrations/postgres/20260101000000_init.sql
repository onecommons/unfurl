-- The segment schema (docs/branch-segments.md, Appendix B). Rows are
-- immutable versions shared between worktrees; a worktree's view is its
-- chain of committed segments plus its draft, and `superseded` says
-- which rows a segment hides.

CREATE TABLE worktree (
    id                BIGSERIAL PRIMARY KEY,
    origin            TEXT    NOT NULL,
    branch            TEXT    NOT NULL,
    commit_id         TEXT,
    -- Working-tree-relative path of the file new records go to when
    -- the caller passes `file_path = None` to a CRUD call. Set on
    -- the first `update_from_working_dir` run; never overwritten
    -- afterwards (operators can pin it manually).
    default_file_path TEXT,
    -- For a branch C.20 is exporting conflicts to, until its ref is at
    -- its commit: the branch of the worktree exporting them, in the same
    -- origin.
    exporting_from    TEXT,
    -- The family's root worktree, whose `version_seq` row this one draws
    -- versions from.
    family_id         BIGINT,
    -- The worktree's open head segment and its draft.
    head_segment_id   BIGINT,
    draft_segment_id  BIGINT,
    -- A `list_changes` cursor below this must re-read (§4.10).
    reset_version     BIGINT  NOT NULL DEFAULT 0,
    UNIQUE (origin, branch)
);

-- One version counter per family: an upstream together with its forks
-- and user branches, which share rows. A version is both an
-- optimistic-concurrency token and a `list_changes` cursor, so one
-- counter has to cover everything that can appear in one response.
CREATE TABLE version_seq (
    worktree_id  BIGINT PRIMARY KEY REFERENCES worktree(id) ON DELETE CASCADE,
    next_version BIGINT  NOT NULL DEFAULT 1
);

CREATE TABLE segment (
    id          BIGSERIAL PRIMARY KEY,
    family_id   BIGINT  NOT NULL REFERENCES version_seq(worktree_id),
    kind        TEXT    NOT NULL CHECK (kind IN ('head', 'internal', 'draft')),
    parent_id   BIGINT  REFERENCES segment(id),
    -- The commit whose tree this segment completes; NULL for a draft.
    head_commit TEXT,
    -- The worktree a head or draft belongs to; NULL once internal.
    owner_id    BIGINT  REFERENCES worktree(id) ON DELETE CASCADE,
    CHECK ((kind = 'internal') = (owner_id IS NULL)),
    CHECK (kind <> 'draft' OR (parent_id IS NULL AND head_commit IS NULL))
);

ALTER TABLE worktree ADD FOREIGN KEY (family_id) REFERENCES version_seq(worktree_id);
ALTER TABLE worktree ADD FOREIGN KEY (head_segment_id) REFERENCES segment(id);
ALTER TABLE worktree ADD FOREIGN KEY (draft_segment_id) REFERENCES segment(id);

-- A worktree's committed chain: its head and every ancestor of it.
-- Drafts aren't listed: each read names its worktrees.
CREATE TABLE worktree_segment (
    worktree_id   BIGINT  NOT NULL REFERENCES worktree(id) ON DELETE CASCADE,
    segment_id    BIGINT  NOT NULL REFERENCES segment(id)  ON DELETE CASCADE,
    -- The version at which the segment joined the chain (§4.10).
    added_version BIGINT  NOT NULL DEFAULT 0,
    -- Part of the chain the worktree was forked with; a layered view
    -- takes only the rest from an upper worktree (§4.12).
    inherited     BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (worktree_id, segment_id)
);

CREATE TABLE file (
    worktree_id   BIGINT  NOT NULL REFERENCES worktree(id) ON DELETE CASCADE,
    path          TEXT    NOT NULL,
    format        TEXT    NOT NULL,
    commit_id     TEXT,
    -- Blob OID of the exact bytes the worktree's view of this file was
    -- parsed from: what a scan compares the disk against. NULL for a
    -- file registered by a record write rather than a scan.
    source_oid    TEXT,
    -- Blob OID at the head commit whose records the committed segments
    -- hold: what a scan compares HEAD against.
    committed_oid TEXT,
    -- The database owes the worktree a removal of this file: every
    -- record in it is tombstoned and the next save deletes it from
    -- disk, the next commit stages that and purges this row.
    deleted       BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (worktree_id, path)
);

-- One immutable version of one record (§3.4). No foreign key to `file`
-- (I4): a row is shared by every worktree whose view holds it.
CREATE TABLE record (
    id             BIGSERIAL PRIMARY KEY,
    -- Identity shared by every version of the record; what
    -- `unfurl.server.id` reports. A new record's is its first row's id.
    key_id         BIGINT  NOT NULL,
    segment_id     BIGINT  NOT NULL REFERENCES segment(id) ON DELETE CASCADE,
    file_path      TEXT    NOT NULL,
    path           TEXT    NOT NULL,
    key            TEXT    NOT NULL,
    -- NULL for a client's edit, which is what makes it pending; a row
    -- taken in from a dirty file keeps the file's last-touching commit.
    commit_id      TEXT,
    json           JSONB   NOT NULL,
    deleted        BOOLEAN NOT NULL DEFAULT FALSE,
    -- Drawn from the family's `version_seq`. The optimistic-concurrency
    -- token (`CommitRef::Pending`) and the `list_changes` cursor.
    version        BIGINT  NOT NULL DEFAULT 0,
    -- In a draft: the commit of the committed version this edit started
    -- from, which is read from git when its content is needed (§4.3).
    -- NULL when the view showed the record live nowhere: the edit
    -- re-creates it.
    base_commit_id TEXT,
    -- In a draft: the content of that committed version, which the
    -- three-way check compares the file against. NULL wherever the base is.
    base_json      JSONB  ,
    -- In a draft: the key_ids of other records an edit settled at its
    -- key (§3.5), as a JSON array.
    settled        JSONB,
    -- The file's side of a record the two sides disagree about, in a
    -- draft only, beside the draft's own row:
    --
    --   'conflict' -- the file's value, as of the scan or write that
    --                 found the divergence. Unresolved.
    --   'resolved' -- the client has declared the database's row the
    --                 winner. The snapshot stays so the next write can
    --                 check the file hasn't moved again since.
    conflict       TEXT CHECK (conflict IS NULL OR conflict IN ('conflict', 'resolved'))
);

-- superseded(r, c): segment c holds a newer version of r's record (I2).
CREATE TABLE superseded (
    record_id  BIGINT  NOT NULL REFERENCES record(id)  ON DELETE CASCADE,
    segment_id BIGINT  NOT NULL REFERENCES segment(id) ON DELETE CASCADE,
    -- The record whose version in segment_id supersedes record_id: which
    -- of a draft's edits made the entry (§3.3).
    key_id     BIGINT  NOT NULL,
    PRIMARY KEY (record_id, segment_id, key_id)
);

CREATE TABLE alias (
    record_id BIGINT  NOT NULL REFERENCES record(id) ON DELETE CASCADE,
    path      TEXT    NOT NULL,
    key       TEXT    NOT NULL,
    PRIMARY KEY (record_id, path, key)
);
