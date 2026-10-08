# unfurl-git-sync

Sync JSON/YAML files tracked in a git repository into SQLite (default) or
Postgres via gitoxide and sqlx.

## Features

- **Scan:** indexes a git working tree's YAML, JSON, JSON5/JSONC and
  Markdown (with embedded YAML) files into SQL, re-parsing only files
  whose bytes changed. A file that fails to parse is reported, not
  fatal.
- **Record oriented:** documents are split into records by
  application-level schemas.
- **Pluggable formats:** a `DataFormat` trait describes an application
  schema; the cloudmap format ships built in.
- **Query:** find records by path and json filter, facets, and follow
  aliases and cross-references between them.
- **CRUD:** create, update, upsert and delete records, singly or in
  batches, with git commits as optimistic-concurrency tokens.
- **Write back:** saves edits to the files in place, keeping untouched
  keys' order, comments and prose, and commits them with a message that
  records who made each change and why.
- **Conflicts:** when the file and an unsaved edit both change a record,
  neither is overwritten; the file's value is kept as a conflict to
  resolve.
- **Worktrees and branches:** each git worktree keeps its own view,
  sharing unchanged history with the others in its family (branches
  and forks) through segments ([design](docs/branch-segments.md)).
  A rewritten HEAD (rebase, reset) is rebuilt from the new history; a
  worktree whose branch changed is refused. Record ids stay stable
  across commits and forks. Deleting a worktree compacts its family's
  shared history.
- **Change feed:** a version watermark lets clients read only what
  changed since their last read, and tells them when to re-read.

## Conflicts

A conflict is a record that changed both in the database, as an edit not
yet saved to its file, and in the file. Neither value is overwritten:
the edit stays pending, and the file's value is kept beside it as a
conflict row.

How each operation treats one:

- **Scan** (`update_from_working_dir`): takes in the file's changes.
  Where a pending edit and the file disagree about a record, it records
  the file's value as a conflict instead of taking it in, and reports it
  in `SyncOutcome::conflicts`. A commit made outside git-sync that
  changes such a record is found the same way.
- **Reads:** return the pending edit. `list_conflicts`, or a query with
  `include_conflicts`, also returns the file's value, marked as a
  conflict.
- **Save** (`write_file`, `save_changes`): writes every other pending
  edit, but leaves a conflicted record's value in the file as it is. The
  conflict is reported in the outcome; it isn't an error.
- **Commit** (`commit_repository(message, CommitOptions)`): scans first, so a hand edit is taken
  in (and may become a conflict) before anything is written. The commit
  carries what is on disk, so the file's value: the conflict row is
  stamped with the commit, and the pending edit stays pending. When the
  file already matches `HEAD`, nothing is committed and it returns
  `None`; a standing conflict never produces empty commits.
- **A plain write** to a conflicted record changes the pending edit and
  leaves the conflict standing.

A conflict lasts until `resolve_conflict` settles it:

- `Resolution::Ours` keeps the pending edit. The next save writes it to
  the file, unless the file has changed again since, which reopens the
  conflict.
- `Resolution::Theirs` takes the file's value and drops the pending edit.
- `Resolution::Merged` takes a value supplied with the resolution.
- `Resolution::Delete` deletes the record; the next save removes it
  from the file.

To resolve them in git instead, `export_conflicts(branch)` moves the
edits under unresolved conflicts to a new branch, with no checkout,
forked where they were made and committed there; the worktree resolves
each for the file. Merging that branch back conflicts in git where the
records did. `commit_repository` does the same with
`CommitOptions::conflicts_to_branch`, exporting what its scan finds
before it commits.

Two ways to let the file win without resolving each record:

- When the commit that last changed a file has a
  `Git-Sync-Resolves-Version: N` trailer, the next scan lets the file
  win over its diverging edits made at version `N` or earlier, unless
  they were already resolved.
- `ScanOptions::force` makes the file win over every pending edit, and
  drops every conflict.

When git itself moves:

- **New commits on top of `HEAD`** (a pull, a commit made outside
  git-sync) are taken in by the next scan, and conflict with pending
  edits as a hand edit would.
- **A rewritten `HEAD`** (rebase, reset): the next scan first rebuilds
  the database's committed view from the new history. Pending edits are
  kept. One to a record the rewrite left alone stays pending as it was;
  one to a record whose value the rewrite changed becomes a conflict
  with the new value, as a hand edit would. When the edit's base commit
  can't be found any more, it counts as changed, so a rewrite can report
  a conflict that isn't one but never drops an edit.
- **`HEAD` moving during a commit** fails it with `Error::HeadMoved`,
  and nothing is committed. A retry scans the new `HEAD` first; the
  server retries once on its own.

The internals are in the design doc: [the commit
fold](docs/branch-segments.md#44-commit) and [conflict
rows](docs/branch-segments.md#411-conflicts).

## Cargo features

- `default = []` — SQLite is always compiled in.
- `sqlite` — (no-op; SQLite is always available).
- `postgres` — opt in to a Postgres backend.

## Status

Cloudmap support, SQLite and Postgres backends, and per-worktree views
with shared segments. User branches (layers, publishing) are next.

## Notes

- SQLite ≥ 3.45 is required for JSONB (`jsonb()`/`json()` builtins);
  startup verifies the bundled version.

## Tests

```bash
cargo test -p unfurl-git-sync # SQLite only
UNFURL_TEST_PG_URL="postgres://localhost/unfurl_test" \
  cargo test -p unfurl-git-sync --features postgres
```
