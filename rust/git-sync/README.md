# unfurl-git-sync

Sync JSON/YAML data tracked in a git repository into SQLite (default) or
Postgres via gitoxide and sqlx.

## Features

- `default = []` — SQLite is always compiled in.
- `sqlite` — (no-op; SQLite is always available).
- `postgres` — opt in to a Postgres backend.

## Status

v1: cloudmap support, SQLite + Postgres backends, optimistic-concurrency
CRUD, semantic-only YAML round-trip.

## Notes

- SQLite ≥ 3.45 is required for JSONB (`jsonb()`/`json()` builtins);
  startup verifies the bundled version.

## Tests

```bash
cargo test -p unfurl-git-sync --no-default-features # SQLite
UNFURL_TEST_PG_URL="postgres://localhost/unfurl_test" \
  cargo test -p unfurl-git-sync --no-default-features --features postgres
```
