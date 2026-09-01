CREATE INDEX idx_record_worktree_path ON record(worktree_id, path, key);
CREATE INDEX idx_record_file          ON record(worktree_id, file_path);
CREATE INDEX idx_alias_path           ON alias(path, key);
CREATE INDEX idx_file_format          ON file(format);

-- One database-side row and at most one file-side row per record. Both
-- are `ON CONFLICT` targets, so the `WHERE` has to be repeated at every
-- upsert naming them (see `db::tx::Dialect`).
CREATE UNIQUE INDEX uq_record_path     ON record(worktree_id, file_path, path, key)
    WHERE conflict IS NULL;
CREATE UNIQUE INDEX uq_record_conflict ON record(worktree_id, file_path, path, key)
    WHERE conflict IS NOT NULL;
