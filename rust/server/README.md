# Unfurl Server

A Rust HTTP api server that acts as caching proxy to the `unfurl server` Python backend and as a front-end to a cloudmap repository using the git-sync crate. 

## Build prerequisites

OpenAPI type generation is driven by the [`oas3-gen`](https://crates.io/crates/oas3-gen)
CLI tool. `build.rs` shells out to it when available, post-processes
the output, and writes the result directly to `src/unfurl_types.rs`,
which is committed to git. **Builds without `oas3-gen` installed use
the committed file and succeed without it.**

To regenerate after OpenAPI spec changes, install `oas3-gen` and rebuild:

```bash
cargo install oas3-gen
cargo build -p unfurl-server
```

The binary lands in `~/.cargo/bin`, which must be on `PATH`.

## Generated types

`build.rs` runs:

```
oas3-gen generate --input ../../unfurl/server/openapi.json
    --output $OUT_DIR/oas3out --all-schemas server-mod
```

Post-processes the output and writes it to `src/unfurl_types.rs`,
committed to git as a normal Rust module.
Commit `src/unfurl_types.rs` whenever the OpenAPI spec changes.

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
| Proxy timeout (seconds, `0` = none) | `--proxy-timeout-secs` | `UNFURL_PROXY_TIMEOUT_SECS` | `120` |
| Max request body bytes | `--max-body-bytes` | `UNFURL_MAX_BODY_BYTES` | `10485760` (10 MiB) |
| Shared internal-auth secret | `--secret` | `UNFURL_SECRET` | (empty) |
| Package digest for ETags | `--package-digest` | `UNFURL_PACKAGE_DIGEST` | (empty) |
| Log file (else stderr) | — | `UNFURL_LOGFILE` | (unset) |
| Log filter | — | `RUST_LOG` | `info` |

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
| Op timeout (seconds, `0` = none) | `--redis-timeout-secs` | `UNFURL_REDIS_TIMEOUT_SECS` | `5` |
| Patch batch window (seconds, `0` = no batching) | `--batch-window-secs` | `UNFURL_BATCH_WINDOW_SECS` | `5` |

If both `--redis-url` and `--redis-host` are unset, caching and the
write queue are disabled and every request is proxied synchronously
to Python.

**Cloudmap fast path** (optional — when both are set, `GET / POST
/cloudmap` are served locally via the `unfurl-git-sync` crate;
otherwise `/cloudmap` is proxied to Python):

| Setting | CLI flag | Env var |
|---|---|---|
| Path to a checked-out cloudmap repo | `--cloudmap-repo` | `UNFURL_CLOUDMAP_REPO` |
| Index DB URL (`sqlite::memory:`, `sqlite:///path/to.db`, `postgres://...`) | `--cloudmap-db-url` | `UNFURL_CLOUDMAP_DB_URL` |

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

## Regenerating after schema changes

When `unfurl/server/serve.py`, `unfurl/server/schemas.py`, or `unfurl/cloudmap-schema.json` change, regenerate the OpenAPI spec on the Python side:

```bash
OPENAPI_VERSION=3.0.3 FLASK_APP=unfurl.server.serve UNFURL_HOME="" \
    .tox/py314/bin/flask spec --output unfurl/server/openapi.json --format json
```

`build.rs` declares `cargo:rerun-if-changed` on the spec, so the next
`cargo build` (with `oas3-gen` installed) regenerates and updates
`src/unfurl_types.rs` automatically. Commit that file alongside the spec change.
