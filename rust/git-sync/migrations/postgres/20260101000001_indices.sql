CREATE INDEX idx_record_worktree_path ON record(worktree_id, path, key);
CREATE INDEX idx_record_file          ON record(worktree_id, file_path);
CREATE INDEX idx_alias_path           ON alias(path, key);
CREATE INDEX idx_file_format          ON file(format);
CREATE UNIQUE INDEX uq_record_path    ON record(worktree_id, file_path, path, key);
