-- Whether a server cloning the database's worktrees clones this one's
-- branch at startup. Not one git-sync made without a checkout -- a
-- recovery's, an export's, a fork of commits a push diverted -- whose
-- branch may be only in this repository or only on the remote, nor one
-- a request brought up, which a request brings up again.
ALTER TABLE worktree ADD COLUMN clone_on_start BOOLEAN NOT NULL DEFAULT TRUE;
UPDATE worktree SET clone_on_start = FALSE WHERE branch LIKE 'git-sync/recovered-%';

-- Whether the worktree's checkout holds commits its remote doesn't have
-- yet, which a server pushes at startup: a push it hadn't made before it
-- stopped.
ALTER TABLE worktree ADD COLUMN push_pending BOOLEAN NOT NULL DEFAULT FALSE;
