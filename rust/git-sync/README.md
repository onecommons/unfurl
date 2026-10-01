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
