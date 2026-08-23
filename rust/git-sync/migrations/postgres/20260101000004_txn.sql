-- Audit trail of batch writes: one row per `apply_batch` call that was
-- given a `TxnMeta`. Reported in the body of the git commit message
-- that later carries those writes.
CREATE TABLE txn (
    id            BIGSERIAL PRIMARY KEY,
    worktree_id   BIGINT NOT NULL REFERENCES worktree(id) ON DELETE CASCADE,
    -- Inclusive `record.version` range stamped by the batch. Ranges from
    -- concurrent batches never interleave: the first `next_version` draw
    -- holds the worktree row lock until the transaction commits.
    first_version BIGINT NOT NULL,
    last_version  BIGINT NOT NULL,
    -- Free-form author string, e.g. `Name <email>`; NULL when unknown.
    author        TEXT,
    -- The caller's description of the batch; NULL when not supplied.
    message       TEXT,
    -- RFC 3339 timestamp with the local offset, e.g.
    -- `2026-07-23T13:10:21-07:00`.
    created_at    TEXT   NOT NULL,
    -- NULL = outstanding (not yet committed to git); stamped by
    -- `db::commit::roll_forward` alongside the record rows.
    commit_id     TEXT
);

CREATE INDEX idx_txn_outstanding ON txn (worktree_id) WHERE commit_id IS NULL;
