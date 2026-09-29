CREATE INDEX idx_segment_parent           ON segment(parent_id);
CREATE INDEX idx_worktree_segment_segment ON worktree_segment(segment_id, worktree_id);
CREATE INDEX idx_file_format              ON file(format);
CREATE INDEX idx_alias_path               ON alias(path, key);
CREATE INDEX idx_superseded_segment       ON superseded(segment_id);

-- One row of each kind per key in a segment. Both are `ON CONFLICT`
-- targets, so the `WHERE` has to be repeated where they're named.
CREATE UNIQUE INDEX uq_record_path     ON record(segment_id, file_path, path, key)
    WHERE conflict IS NULL;
CREATE UNIQUE INDEX uq_record_conflict ON record(segment_id, file_path, path, key)
    WHERE conflict IS NOT NULL;
-- The versions of one record, across segments.
CREATE INDEX idx_record_key     ON record(file_path, path, key);
-- Lookups that don't name the file.
CREATE INDEX idx_record_path_key ON record(path, key);
CREATE INDEX idx_record_key_id  ON record(key_id);
CREATE INDEX idx_record_version ON record(segment_id, version);
