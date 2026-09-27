# Unfurl Server

A Rust HTTP api server that acts as caching proxy to the `unfurl server` Python backend and as a front-end to a cloudmap repository using the git-sync crate.

## Running the server

Running `unfurl serve` will automatically start both the Python server and this server if the `unfurl-server` binary is found. To run just this server, run `unfurl-server` directly. See the configuration section below for details on how to configure the server and its connection to the Python backend, Redis cache, and cloudmap repository.

By default the server binds `127.0.0.1:8080` and proxies everything
to `http://127.0.0.1:8081` (port + 1). Without Redis or a cloudmap
repository configured, every request is forwarded straight to the Python
backend.

### Configuration

Every option can be set with a CLI flag or an environment variable.
Pass `--help` for the canonical list. The most-used knobs:

| Setting | CLI flag | Env var | Default |
|---|---|---|---|
| Bind host | `--host` | `UNFURL_HOST` | `127.0.0.1` |
| Bind port | `--port` | `UNFURL_PORT` | `8080` |
| Python backend URL | `--backend-url` | `UNFURL_BACKEND_URL` | `http://{host}:{port+1}` |
| Proxy timeout (integer seconds, `0` = none) | `--proxy-timeout-secs` | `UNFURL_PROXY_TIMEOUT_SECS` | `120` |
| Max request body bytes | `--max-body-bytes` | `UNFURL_MAX_BODY_BYTES` | `10485760` (10 MiB) |
| Shared internal-auth secret | `--secret` | `UNFURL_SECRET` | (empty) |
| Package digest for ETags | `--package-digest` | `UNFURL_PACKAGE_DIGEST` | (empty) |
| Allowed CORS origins | `--cors-origins` | `UNFURL_SERVE_CORS` | (unset — no CORS layer) |
| Log file (else stderr) | — | `UNFURL_LOGFILE` | (unset) |
| Log style (`text`, `json`) | `--log-style` | `UNFURL_LOG_STYLE` | `text` on a terminal, else `json` |
| Log filter | — | `RUST_LOG` | `info` |
| Write-queue key lifetime | `--queue-key-ttl-secs` | `UNFURL_QUEUE_KEY_TTL_SECS` | `86400` |
| How long `/events` is held open | `--events-budget-secs` | `UNFURL_EVENTS_BUDGET_SECS` | `120` |
| Branch-watch poll interval | `--branch-poll-interval-ms` | `UNFURL_BRANCH_POLL_INTERVAL_MS` | `1000` |

`--cors-origins` takes origins separated by whitespace or commas, or
`*` for any origin; an origin that isn't a valid header value is a
startup error. Credentials are not allowed, matching flask-cors's
default on the python side.

When `unfurl serve` spawns this process it exports the origins it
resolved for its own flask-cors setup — including the
`UNFURL_CLOUD_SERVER`-derived default — so both servers answer
preflights for the same set. Preflights matter here because routes such
as `/export` are registered `GET`-only: without the layer a browser's
`OPTIONS` gets a 405 from the method router.

**Cloudmap fast path** (optional — when both are set, `GET / POST
/cloudmap` are served locally via the `unfurl-git-sync` crate;
otherwise `/cloudmap` is proxied to Python):

| Setting | CLI flag | Env var | Default |
|---|---|---|---|
| Path to a checked-out cloudmap repo | `--cloudmap-repo` | `UNFURL_CLOUDMAP_REPO` | (unset) |
| Index DB URL (`sqlite::memory:`, `sqlite:///path/to.db`, `postgres://...`) | `--cloudmap-db-url` | `UNFURL_CLOUDMAP_DB_URL` | (unset) |
| Working tree wins over in-flight edits on the startup scan | `--cloudmap-force` | `UNFURL_CLOUDMAP_FORCE` | `false` |
| Serve the index as it stands, without scanning at startup | `--cloudmap-skip-scan` | `UNFURL_CLOUDMAP_SKIP_SCAN` | `false` |
| Smallest refusal that aborts the startup scan (`report`, `file`, `record`) | `--scan-abort-level` | `UNFURL_SCAN_ABORT_LEVEL` | `report` |
| Cloud server whose projects `auth_project` names | `--cloud-server` | `UNFURL_CLOUD_SERVER` | `https://unfurl.cloud` |

The database can index several cloudmaps, one worktree per repository
and branch; only the checked-out one can be written to. A read naming
neither `auth_project` nor `branch` is answered from the checked-out
worktree. Otherwise it is answered from the worktree whose origin is
`{cloud server}/{auth_project}` (the checked-out worktree's origin
without `auth_project`) on `branch`, `main` without one: with no records
when that origin has no such branch, and by Python when the database has
no worktree for the origin at all.

The startup scan validates every record against the cloudmap schema,
reports what it refused, and summarises it in one line.
`--scan-abort-level` decides whether that also stops the server coming
up: `report` (the default) serves anyway, `file` aborts on a file skipped
whole, `record` aborts on even one refused record. Validation always runs -- the levels
choose what aborts, not whether records are checked -- because refusing a
record is what keeps an invalid value from overwriting the row already
indexed. `AGENTS.md` has the grades.

`--cloudmap-skip-scan` serves the index as it stands. Nothing else
triggers a scan, so the index is then only as fresh as whoever last wrote
it, and `--cloudmap-force` and `--scan-abort-level` have nothing to act
on — each of those is warned about at startup, as is an in-memory index,
which the skipped scan leaves empty.

**Redis** (optional — required for `GET /export`/`/types` caching
and for the write-queue fast path on the patch endpoints):

| Setting | CLI flag | Env var | Default |
|---|---|---|---|
| Full URL (preferred) | `--redis-url` | `CACHE_REDIS_URL` | (unset) |
| Host (fallback) | `--redis-host` | `CACHE_REDIS_HOST` | (unset) |
| Port | `--redis-port` | `CACHE_REDIS_PORT` | `6379` |
| Password | `--redis-password` | `CACHE_REDIS_PASSWORD` | (unset) |
| DB number | `--redis-db` | `CACHE_REDIS_DB` | `0` |
| Cache key prefix | `--cache-key-prefix` | `CACHE_KEY_PREFIX` | `ufsv::` |
| Op timeout (integer seconds, `0` = none) | `--redis-timeout-secs` | `UNFURL_REDIS_TIMEOUT_SECS` | `5` |
| Patch batch window (fractional seconds, `0` = no batching) | `--batch-window-secs` | `UNFURL_BATCH_WINDOW_SECS` | `3.0` |
| Worker poll interval (fractional seconds) | `--worker-poll-interval-secs` | `UNFURL_WORKER_POLL_INTERVAL_SECS` | `0.1` |

If both `--redis-url` and `--redis-host` are unset, caching and the
write queue are disabled and every request is proxied synchronously
to Python.

> **Note on `*_secs` settings:** `--batch-window-secs` and
> `--worker-poll-interval-secs` accept fractional values (e.g.
> `0.5`).  `--proxy-timeout-secs` and `--redis-timeout-secs` are
> whole-second integers only.

### Logging

`--log-style json` (or `UNFURL_LOG_STYLE=json` — not
`UNFURL_LOG_FORMAT`, which is python's `logging.Formatter` string) puts
every field through a serializer; `text` is the
readable form. Both emit exactly one event per line — a file name with a
space in it, or a parser diagnostic spanning several lines, is quoted and
escaped rather than running into the next field:

```
WARN file could not be parsed file="my broken file.yaml" syntax=Yaml
  error="yaml error in my broken file.yaml: …unclosed bracket '['\n --> <input>:1:1\n…"
```

Values that can hold arbitrary text — `file`, `path`, `key`, `error` — are
always quoted. Ones that cannot, like `syntax` and the counts, are bare.
So `grep 'file="x y.yaml"'` matches a whole field rather than a prefix.

Each message is a fixed string, chosen so a log can be filtered by what
the scan *did* rather than by parsing fields out of a shared line. The
startup scan of a cloudmap repo emits:

| Level | Message | What it means | Fields |
|---|---|---|---|
| WARN | `file could not be parsed` | Not valid YAML/JSON; the file is skipped and its existing records kept — broken, not emptied | `file`, `syntax`, `error` |
| WARN | `document is unreadable as this format; skipping the file` | Schema violation the whole document turns on, e.g. an unknown `apiVersion`; skipped the same way, nothing deleted | `file`, `format`, `error` |
| WARN | `section refused; the records it holds are left as they are` | A section is not a mapping, so nothing under it is enumerable; its rows are neither replaced nor pruned | `file`, `format`, `path`, `error` |
| WARN | `record refused; the row already indexed is left as it is` | One record does not fit its schema; the row already there survives rather than being overwritten | `file`, `format`, `path`, `key`, `error` |
| INFO | `schema warning; indexed anyway` | A complaint that does not stop the record being indexed | `file`, `format`, `path`, `key`, `error` |
| WARN | `document does not conform to its schema` | One summary per file, with counts rather than contents — the line to grep first | `file`, `format`, `fatal`, `refused_sections`, `refused_records`, `warnings` |
| WARN | `file needs json5 syntax; a rewrite will emit strict json and drop comments` | Read fine; the first write to it normalises the file | `file` |
| INFO | `file is gone from the working tree` | A tracked file was removed | `file` |
| INFO | `cloudmap startup scan complete` | The aggregate, emitted once | `files_seen`, `records_upserted`, `records_deleted`, `unparsed`, `invalid_files`, `skipped_files`, `refused` |
| ERROR | `cloudmap does not conform to its schema; refusing to start` | `--scan-abort-level` was set above what the scan found | `abort_level`, `skipped_files`, `refused` |

Everything the scan acted on is WARN, including a skipped file: a document
skipped for a bad header and one skipped for bad syntax have the same
consequence, so they log at the same level. Only the advisory grade is
INFO. A scan reports at most ten findings per grade per file and leaves
the rest to the summary's counts, so one badly broken document cannot bury
everything else.

Divergence between a file and an in-flight edit logs separately —
`file diverges from a pending edit; keeping both sides` and
`record deleted from file under a pending edit; keeping both sides`, with
their `Git-Sync-Resolves-Version` counterparts at INFO — carrying the same
`file`, `path` and `key` fields, so a refusal and a conflict on one record
line up.

Scan events come from the `unfurl_git_sync::sync` and
`unfurl_git_sync::scan` targets, so `RUST_LOG=unfurl_git_sync=warn` keeps
them without the proxy's request logging.

### Examples

Local dev — Python on 8081, Rust proxy on 8080, Redis on its
default socket, cloudmap fast path served from a sibling repo:

```bash
CACHE_REDIS_URL=redis://localhost:6379/0 \
UNFURL_CLOUDMAP_REPO=$HOME/_dev/cloudmap \
UNFURL_CLOUDMAP_DB_URL=sqlite::memory: \
RUST_LOG=info \
unfurl-server
```

Pure passthrough (no Redis, no cloudmap) — no cache, but enables asynchronous updates:

```bash
unfurl-server --backend-url http://127.0.0.1:5000 --port 8080
```

Production-ish — bind all interfaces, write logs to a file, longer
batch window for write coalescing:

```bash
UNFURL_HOST=0.0.0.0 \
UNFURL_LOGFILE=/var/log/unfurl-server.log \
CACHE_REDIS_URL=redis://redis.internal:6379/0 \
UNFURL_BATCH_WINDOW_SECS=10 \
unfurl-server
```

## Development

`src/unfurl_types.rs` is generated from the OpenAPI spec, but it is
**committed to git**: building this crate needs nothing beyond a Rust
toolchain. Only changing the spec needs the generator below.

### Regenerating the types

Type generation is driven by the
[`oas3-gen`](https://crates.io/crates/oas3-gen) CLI. `build.rs` shells
out to it when it is installed, post-processes the output, and writes
`src/unfurl_types.rs` directly; when it isn't installed the committed
file is used as-is and the build succeeds without it.

When `unfurl/server/serve.py`, `unfurl/server/schemas.py`, or
`unfurl/cloudmap/cloudmap-schema.json` change, regenerate the spec on the
Python side first:

```bash
OPENAPI_VERSION=3.0.3 FLASK_APP=unfurl.server.serve UNFURL_HOME="" \
    .tox/py314/bin/flask spec --output unfurl/server/openapi.json --format json
```

Then install the pinned generator and rebuild. `build.rs` declares
`cargo:rerun-if-changed` on the spec, so the rebuild is what regenerates:

```bash
cargo install oas3-gen --version "$(cat rust/server/.oas3-gen-version)" --locked
cargo build -p unfurl-server
```

Commit `src/unfurl_types.rs` alongside the spec change. The generator
binary lands in `~/.cargo/bin`, which must be on `PATH`.

`build.rs` invokes it as:

```
oas3-gen generate --input ../../unfurl/server/openapi.json
    --output $OUT_DIR/oas3out --all-schemas server-mod
```

The pinned version lives in `rust/server/.oas3-gen-version` and is the
single source of truth, also read by `.github/workflows/on_push.yml` and
`docker/Dockerfile.server`. `build.rs` enforces the pin at compile time:
a mismatched generator fails the build with the exact reinstall command.
(With no `oas3-gen` installed at all the check is skipped.)

### Bumping `oas3-gen`

1. Edit `rust/server/.oas3-gen-version` and replace the version with the
   new `X.Y.Z`.
2. Reinstall the pinned binary:
   ```bash
   cargo install oas3-gen --version X.Y.Z --locked --force
   ```
3. Rebuild — `build.rs` regenerates `src/unfurl_types.rs` against the
   new generator:
   ```bash
   cargo build -p unfurl-server
   ```
4. Inspect the diff in `src/unfurl_types.rs`. If a generator release
   introduces new external crate references (e.g. 0.26+ switched maps
   to `indexmap::IndexMap`), add the corresponding dependency to
   `rust/server/Cargo.toml`. Adjust any handler that names a renamed
   type.
5. Commit `.oas3-gen-version` and `src/unfurl_types.rs` together.

The write-queue protocol these endpoints implement — the subscription
endpoints, the SSE frame kinds and the Redis key shapes — is documented
in `AGENTS.md` next to this file.
