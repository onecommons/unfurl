CREATE TABLE worktree (
    id                BIGSERIAL PRIMARY KEY,
    origin            TEXT      NOT NULL,
    branch            TEXT      NOT NULL,
    commit_id         TEXT,
    -- Working-tree-relative path of the file new records go to when
    -- the caller passes `file_path = None` to a CRUD call. Set on
    -- the first `update_from_working_dir` run; never overwritten
    -- afterwards (operators can pin it manually).
    default_file_path TEXT,
    UNIQUE (origin, branch)
);

CREATE TABLE file (
    worktree_id BIGINT NOT NULL REFERENCES worktree(id) ON DELETE CASCADE,
    path        TEXT   NOT NULL,
    format      TEXT   NOT NULL,
    commit_id   TEXT,
    -- Blob OID of the exact bytes this file's records were parsed from.
    --
    -- Lets a write tell whether the database's picture of a file is
    -- still current. `commit_id` cannot: it names the commit that last
    -- touched the path, so it is unchanged by an uncommitted edit and
    -- shared by files that differ.
    --
    -- NULL for a file registered by a record write rather than a scan
    -- -- nothing has been parsed from it, so there is nothing to
    -- compare.
    source_oid  TEXT,
    PRIMARY KEY (worktree_id, path)
);

CREATE TABLE record (
    id          BIGSERIAL PRIMARY KEY,
    worktree_id BIGINT  NOT NULL,
    file_path   TEXT    NOT NULL,
    path        TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    commit_id   TEXT,
    json        JSONB   NOT NULL,
    deleted     BOOLEAN NOT NULL DEFAULT FALSE,
    -- Per-row monotonic stamp (drawn from `version_seq.next_version`).
    -- Doubles as the optimistic-concurrency token (`CommitRef::Pending`)
    -- and the cursor for `SyncedRepo::list_changes`. Preserved across
    -- commit roll-forward.
    version     BIGINT  NOT NULL DEFAULT 0,
    -- The `commit_id` the row had when a client first edited it -- the
    -- commit this pending edit is based on.
    --
    -- NULL on a non-pending row, and NULL on a pending row that was
    -- never in the file (a create). Cleared when a commit rolls forward
    -- and when a scan takes the row in fresh. Distinguishes an unsaved
    -- create from a pending edit whose record was deleted from the file,
    -- and names the merge base for resolving a diverged record against
    -- git history.
    base_commit_id TEXT,
    -- Which view of the record this row holds. NULL is the database's
    -- own -- what the CRUD API reads and writes. A non-NULL row is the
    -- *file's* view of a record the two sides disagree about, kept
    -- alongside the NULL one so neither side is overwritten before
    -- someone resolves:
    --
    --   'conflict' -- the file's value, as of the scan or write that
    --                 found the divergence. Unresolved.
    --   'resolved' -- the client has declared the database's row the
    --                 winner. The snapshot stays so the next write can
    --                 check the file hasn't moved again since.
    --
    -- The two partial unique indexes in `indices.sql` allow at most one
    -- row of each kind per (worktree, file, path, key). TEXT rather than
    -- a postgres enum so one column type serves both backends -- the
    -- CHECK gives the validation an enum would, on sqlite too.
    conflict    TEXT CHECK (conflict IS NULL OR conflict IN ('conflict', 'resolved')),
    FOREIGN KEY (worktree_id, file_path)
        REFERENCES file (worktree_id, path) ON DELETE CASCADE
);

CREATE TABLE alias (
    record_id BIGINT NOT NULL REFERENCES record(id) ON DELETE CASCADE,
    path      TEXT   NOT NULL,
    key       TEXT   NOT NULL,
    PRIMARY KEY (record_id, path, key)
);
