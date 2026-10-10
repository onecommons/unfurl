-- One row per repository, by its origin as worktree.origin spells it, for
-- what belongs to the repository rather than to one of its branches.
CREATE TABLE repository (
    origin        TEXT    PRIMARY KEY,
    -- Whether it's public: 'public', 'private', or NULL until something
    -- finds out. Records from a private repository aren't served to
    -- requests that haven't been checked for access to it.
    visibility    TEXT    CHECK (visibility IN ('public', 'private'))
);
