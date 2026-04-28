CREATE TABLE worktree (
    id        INTEGER PRIMARY KEY,
    origin    TEXT    NOT NULL,
    branch    TEXT    NOT NULL,
    commit_id TEXT,
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
    FOREIGN KEY (worktree_id, file_path)
        REFERENCES file (worktree_id, path) ON DELETE CASCADE
);

CREATE TABLE alias (
    record_id INTEGER NOT NULL REFERENCES record(id) ON DELETE CASCADE,
    path      TEXT    NOT NULL,
    key       TEXT    NOT NULL,
    PRIMARY KEY (record_id, path, key)
);
