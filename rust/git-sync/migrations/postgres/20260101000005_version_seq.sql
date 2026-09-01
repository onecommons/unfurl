-- Move the version counter off `worktree` and onto the family it belongs
-- to: an upstream together with its forks and per-user drafts.
--
-- Those share responses -- a read can merge a draft's edits over the
-- upstream they came from -- and a version is both an
-- optimistic-concurrency token and a `list_changes` cursor. Drawn from
-- independent counters, the same number would mean two different rows
-- and `version > cursor` would compare across unrelated sequences, so
-- one counter has to cover everything that can appear in one response.
--
-- Not global, though: two unrelated cloudmaps never appear together, so
-- sharing a counter between them would buy nothing and cost a shared
-- lock.
CREATE TABLE version_seq (
    -- The family's root worktree -- the upstream everything else forked
    -- from. Its own row is its own family.
    worktree_id  BIGINT PRIMARY KEY REFERENCES worktree(id) ON DELETE CASCADE,
    next_version BIGINT NOT NULL DEFAULT 1
);

-- NULL means "its own family"; see `COALESCE(family_id, id)` in the
-- queries. Left nullable so adding the column needs no sentinel value
-- that would momentarily violate the reference. `db::worktree::upsert`
-- seeds both this and the `version_seq` row when it creates a worktree.
ALTER TABLE worktree ADD COLUMN family_id BIGINT REFERENCES version_seq(worktree_id);
