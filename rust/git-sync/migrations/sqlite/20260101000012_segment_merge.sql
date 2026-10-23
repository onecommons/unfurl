-- A merge in a segment's commits, or a fast-forward to its head: segment
-- `segment_id` holds commit `merge_commit`, which brought in the line of
-- segment `merged_segment_id`, ending at the commit merged. Two
-- worktrees' merge bases are the latest segments their histories share,
-- following parents and these.
CREATE TABLE segment_merge (
    segment_id        INTEGER NOT NULL REFERENCES segment(id) ON DELETE CASCADE,
    merge_commit      TEXT    NOT NULL,
    merged_segment_id INTEGER NOT NULL REFERENCES segment(id) ON DELETE CASCADE,
    PRIMARY KEY (segment_id, merge_commit, merged_segment_id)
);
CREATE INDEX idx_segment_merge_merged ON segment_merge (merged_segment_id);
