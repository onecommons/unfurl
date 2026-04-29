CREATE TABLE worktree (
    id                INTEGER PRIMARY KEY,
    origin            TEXT    NOT NULL,
    branch            TEXT    NOT NULL,
    commit_id         TEXT,
    -- Per-worktree monotonic version counter. Bumped on every CRUD
    -- write; the previous value is stamped on `record.version`.
    next_version      INTEGER NOT NULL DEFAULT 1,
    -- Working-tree-relative path of the file new records go to when
    -- the caller passes `file_path = None` to a CRUD call. Set on
    -- the first `update_from_working_dir` run; never overwritten
    -- afterwards (operators can pin it manually).
    default_file_path TEXT,
    UNIQUE (origin, branch)
);

CREATE TABLE file (
    worktree_id INTEGER NOT NULL REFERENCES worktree(id) ON DELETE CASCADE,
    path        TEXT    NOT NULL,
    format      TEXT    NOT NULL,
    commit_id   TEXT,
    PRIMARY KEY (worktree_id, path)
);

CREATE TABLE record (
    id          INTEGER PRIMARY KEY,
    worktree_id INTEGER NOT NULL,
    file_path   TEXT    NOT NULL,
    path        TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    commit_id   TEXT,
    json        BLOB    NOT NULL,
    deleted     INTEGER NOT NULL DEFAULT 0,
    -- Per-row monotonic stamp (drawn from `worktree.next_version`).
    -- Doubles as the optimistic-concurrency token (`CommitRef::Pending`)
    -- and the cursor for `SyncedRepo::list_changes`. Preserved across
    -- commit roll-forward.
    version     INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (worktree_id, file_path)
        REFERENCES file (worktree_id, path) ON DELETE CASCADE
);

CREATE TABLE alias (
    record_id INTEGER NOT NULL REFERENCES record(id) ON DELETE CASCADE,
    path      TEXT    NOT NULL,
    key       TEXT    NOT NULL,
    PRIMARY KEY (record_id, path, key)
);
