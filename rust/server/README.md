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
| Grant key file (32 bytes, or their base64) | `--grant-key-file` | `UNFURL_GRANT_KEY_FILE` | (unset — queued writes store credentials) |
| Inherited descriptor to read the grant key from, then close; excludes the file | `--grant-key-fd` | `UNFURL_GRANT_KEY_FD` | (unset) |
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
| With a DB URL and no repo: clone (or fast-forward) a checkout of every worktree the DB tracks under this directory, in python's `public/<project>/<branch>` layout, and serve them all; a request naming a project or branch with none brings one up, with the request's own credentials if it has them, which are then kept as a grant for its later pulls, and one that can't be is left to python; a missing last commit's records are saved to a branch. With Redis, git work in a checkout holds a lock shared with python, keyed by its path, so this must resolve to the same absolute path as python's `UNFURL_CLONE_ROOT` | `--clone-root` | `UNFURL_CLONE_ROOT` | (unset; `unfurl serve` passes its own) |
| Working tree wins over in-flight edits on the startup scan, and a missing last commit is rebuilt from HEAD instead of refusing to start | `--cloudmap-force` | `UNFURL_CLOUDMAP_FORCE` | `false` |
| A missing last commit's records are saved to a branch, `git-sync/recovered-<commit>`, to merge, instead of refusing to start | `--cloudmap-recover` | `UNFURL_CLOUDMAP_RECOVER` | `false` |
| Trust the own git config of the checkouts under `--clone-root`, which python writes too. Without it, git-sync ignores it but for remote urls, the server's git commands override hooks, fsmonitor, credential helpers and proxies, and a checkout whose config sets filter or diff drivers, includes, extra headers, url rewrites or `core.worktree` isn't pulled or served (`docs/credentials.md` §2.6). A configured `--cloudmap-repo`, and serving a local path, are trusted anyway | `--trust-checkouts` | `UNFURL_TRUST_CHECKOUTS` | `false` |
| How many seconds since python last pulled a project's clone a cached `/export` or `/types` made at the clone's HEAD may be served for a request naming no `latest_commit` (python's setting, read the same way; `stale=ok` or `stale=never` on the request override it). 0 serves none, -1 any; needs `--clone-root`. Also how old a cloudmap checkout's last pull may be before a request pulls it: see **Freshness** below | `--pull-timeout` | `CACHE_DEFAULT_PULL_TIMEOUT` | `120` |
| With a clone root: a cloudmap repository a request naming no project reads, as `<project>[#branch]` (its default branch without one). It's cloned at startup like the worktrees the database tracks, or pulled if its checkout is there; such a read spans their worktrees, so it returns no `commit` or `version` and refuses `since_version`. Without one, python's default, the public cloudmap on `main`; if none is served, a read naming no project goes to python | `--default-cloudmap` (repeatable) | `UNFURL_DEFAULT_CLOUDMAPS` (comma-separated) | `onecommons/cloudmap#main` |
| The fewest seconds between two pushes of a cloudmap checkout's commits: see **Pushing** below | `--push-interval` | `UNFURL_PUSH_INTERVAL` | `60` |
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

**Freshness.** Outside dev mode, a request that a served cloudmap
checkout answers can pull it first, as python pulls its own clones and
sharing its record of each pull, `(time, outcome)` at
`<CACHE_KEY_PREFIX><project>:pull:<checkout directory>` in its cache (in
this process alone without Redis):

| Request | Last pull | Pull |
|---|---|---|
| any | `detached` or `proxied` | none |
| no `latest_commit` | within `--pull-timeout` seconds | none |
| no `latest_commit` | older, or none recorded | in the background; the request is served at once |
| a `latest_commit` the checkout has (a `-dirty` suffix ignored) | any | none |
| a `latest_commit` it lacks | any | first, then the request is served |

The checkouts pulled are the one a write goes to, the one a read of a
project reads, and for a read naming no project the default cloudmaps,
in the background only. One just brought up for a request isn't pulled
again. `stale=ok` and `stale=never` don't apply, as on python's cloudmap
reads. A checkout pulls once at a time, with the request's credentials,
else for a checkout under `private/` its users' most recent grant, else
none, holding its git lock -- which is how python waits on it -- and if
its HEAD moved it is rescanned and its unfinished exports finished; a
scan over `--scan-abort-level` is logged, and the checkout still served.
How each pull ends:

| What happens | Done | Recorded |
|---|---|---|
| It fast-forwards to its upstream | | `pulled` |
| Its remote can't be reached, or refuses its credentials | served as it is, and pulled again after the timeout | `failed` |
| It can't fast-forward, holding nothing its remote lacks (its upstream was rewritten) | reset to its upstream | `pulled` |
| It can't fast-forward, holding work its remote lacks: uncommitted changes, commits on no remote-tracking branch before the fetch, or records the database hasn't committed | served as it is until the work is pushed | `diverged` |
| It's broken: no longer a git checkout, or one git can't read | under the clone root, moved aside to `<directory>.broken-<unix time>` -- logged as an error -- and cloned again; the configured `--cloudmap-repo` isn't | `cloned` |
| Its own config sets what runs a command (a filter, diff or merge driver, a gpg program) or redirects it, or, under the clone root, its remote names another project than its own, for fetching or pushing | refused: neither pulled nor moved aside, as its credentials are for its own project | `failed` |
| On no branch with an upstream | none | `detached` |

A grant git refuses is forgotten, so the next pull tries the one before
it. The startup clone or pull of each checkout brings it up the same
way, but for one whose remote can't be reached, which is served as it is.
Python's own pulls differ in one way: one that fails for any reason
deletes the clone to clone it again, unless it holds unpushed work.

**Pushing.** Outside dev mode, a cloudmap write that commits
(`commit: true`) answers at once, with the local commit; the commit is
pushed in the background, with the request's credentials, or else its
project's most recent grant. A checkout is pushed at most once per
`--push-interval` seconds: a commit sooner after its last push waits,
and goes with those made meanwhile. A push carries the checkout's branch
and the `git-sync/` branches made in it, such as an export's, that the
remote lacks. Commits a pull finds the checkout holding that the remote
lacks -- python's, or those of a push that failed -- are pushed first,
with a grant only: never the credentials of the request that happened to
find them. A push holds the checkout's git lock, and a commit holds the
lock the checkout's pulls and pushes do, so none runs git in it
meanwhile. Only a checkout under the clone root is pushed, never the
configured `--cloudmap-repo`; and only from its own branch, to its own
project: one on another branch, or whose remote names another project,
isn't. A push that fails is tried again on its own the interval after (a
second at the least): with the same credentials if the remote couldn't
be reached, else a grant.

| The push | Done | `/events` reports |
|---|---|---|
| is taken | the branch head recorded, as python records it | `pushed` |
| finds the remote's branch moved on, and it merges cleanly | merged in memory (`git merge-tree`), the merge committed and checked out, and pushed | `pushed` |
| finds it moved on and conflicting, or is rejected (a protected branch, a hook) | the commits pushed to a new branch, `git-sync/diverted-<commit>`, for a developer to merge, and the checkout reset to its remote and rescanned | `diverted` |
| is rejected on that new branch too | the commits kept on a local branch, `git-sync/refused-<commit>`, never pushed, and the checkout reset to its remote and rescanned | `refused` |
| has its credentials refused, or can't reach the remote | left for the next push, with a grant; no grant is forgotten, as one may still read | nothing yet |
| can't be reset, its working tree holding changes, or is on another branch | left as it is, nothing pushed or kept, and tried again each interval, with a warning | nothing yet |

Since the gateway lets only a project's developers write, a refusal of
credentials means they were revoked within its TTL: another developer's
grant may push the write. Records the database has staged survive a
reset: the rescan keeps them, to commit onto the remote's branch. Merging
needs git 2.38 or later (`merge-tree --write-tree`); with an older one,
every push the remote moved on from is diverted, with a warning. A diverted commit's records leave the branch's view until
its branch is merged; the merge comes in with the next pull. What came of
a commit's push is kept in Redis, `<prefix>push:<project>:<branch>:<commit>`,
and a `/events` branch watch on that commit reports it once the push is
tried, rather than `moved`. This is the "Pushing" of git-sync's design
(§4.15), but for its in-database rebase: a clean merge in git stands in
for it, and what doesn't merge is diverted. It's done here, not in
git-sync, since gix can't push and the server holds the credentials.

**Creating the Postgres database:** `unfurl-server create-database`
creates the role and database `UNFURL_CLOUDMAP_DB_URL` names and exits,
0 on success. Connected as the admin role to the same host and port, it
creates the role with `LOGIN` and the app password, or sets the password
of a role that exists, then creates the database owned by it unless it
exists; a database owned by another role is left alone, with a warning.
It creates no tables: the server does when it connects. The password is
sent as a SCRAM-SHA-256 verifier, never in plaintext. A URL with a
password, or whose user is the admin role, is refused, as is an existing
role that is a superuser or can create roles or databases, replicate, or
bypass row-level security. Pass the passwords
in the environment: flags show in `ps`. The work is
`unfurl_git_sync::db::provision`'s.

| Setting | CLI flag | Env var |
|---|---|---|
| The app's URL, without a password: its user is the role, its path the database | `--cloudmap-db-url` (before the subcommand) | `UNFURL_CLOUDMAP_DB_URL` |
| The role's password, which the server also reads to connect | `--app-password` | `PGPASSWORD` |
| Admin role | `--admin-user` | `UNFURL_DB_ADMIN_USER` |
| Admin role's password | `--admin-password` | `UNFURL_DB_ADMIN_PASSWORD` |
| Database to connect to as the admin, for services that restrict `postgres` | `--admin-database` | `UNFURL_DB_ADMIN_DATABASE` (default `postgres`) |

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
