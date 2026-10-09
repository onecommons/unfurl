-- Users' credentials for a repository, for work done after the request
-- that brought them (rust/server/docs/credentials.md §2.1). Tokens are
-- encrypted by the caller: git-sync stores them and never holds the key.
CREATE TABLE credential_grant (
    id            TEXT    PRIMARY KEY,
    username      TEXT    NOT NULL,
    -- the repository, as worktree.origin spells it
    origin        TEXT    NOT NULL,
    -- empty until a grant's scopes are needed (§2.5)
    scopes        TEXT    NOT NULL DEFAULT '',
    -- the token, encrypted with the key named by key_id
    token         BYTEA   NOT NULL,
    key_id        TEXT    NOT NULL,
    -- a digest of the token keyed by the caller, to find its grant
    token_digest  TEXT    NOT NULL,
    -- seconds since the Unix epoch
    created_at    BIGINT  NOT NULL,
    last_used_at  BIGINT  NOT NULL,
    expires_at    BIGINT  NOT NULL,
    UNIQUE (token_digest, origin)
);
CREATE INDEX credential_grant_by_origin ON credential_grant (origin, last_used_at);
CREATE INDEX credential_grant_by_expiry ON credential_grant (expires_at);

-- The id of the key grants are encrypted with, so a caller started with
-- a different key finds out when it starts. One row: of two callers
-- starting at once with different keys, the second sees the first's.
CREATE TABLE credential_grant_key (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    key_id        TEXT    NOT NULL
);
