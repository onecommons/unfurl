# Securing git and GitLab credentials

Status: proposed.

The servers hold three kinds of credential on a user's behalf: their GitLab
access token, a project's token for its CI variables, and the server's own
API secret. Today those are written to Redis, to checkouts' `.git/config`
and, in one debug path, to a commit message, and one user's can end up used
for another's write.

The plan has two scopes:

- **The baseline (§2.1–2.4)** keeps credentials from being stored anywhere
  but one place: encrypted, in the cloudmap database, as a **grant**. Queued work and
  checkouts hold grant IDs and plain URLs; the queue worker turns a grant
  back into the credential when it hands a request to Python. Python still
  receives credentials in memory, as today.
- **Hardening (§2.5), optional and later**, also keeps credentials out of
  the Python process's memory: a local proxy in the Rust server adds them to
  git and GitLab API requests, and the two servers run as separate users.

## 1. Threat model

### Assets

| Asset | What it opens | Where it comes from |
|---|---|---|
| A user's GitLab access token | Every project the user can read or write, by git and the API, until it expires (typically weeks to a year) | `X-Git-Credentials` header; `username`/`private_token`/`password` in a body or query; the userinfo of `blueprint_url` |
| A project token (`UNFURL_PROJECT_TOKEN`) | That project's CI/CD variables, where users keep deployment secrets | The `private_token=` parameter of `cloud_vars_url` |
| The server's API secret (`UNFURL_SERVE_SECRET`) | Every endpoint of the unfurl server | `Authorization: Bearer` header, or `?secret=` |
| CI variable values | Users' deployment secrets (cloud keys, passwords) | Fetched from GitLab through `cloud_vars_url` |
| Private repository and cloudmap content | The users' private data | Clones under `private/`, the cloudmap database, the response cache |

### Trust boundaries and assumptions

- **Every external request passes through the Tyk API gateway,** which
  checks the user's token against the request's `auth_project`: a POST
  needs write access to the project, a GET of a private project read
  access. Nothing else can reach the unfurl server's ports. So every request
  the servers see comes from a user with the access it needs, and the
  servers' own secret check (`UNFURL_SERVE_SECRET`) is defence in depth, against something
  reaching them without passing through Tyk.
- **Redis** is password-protected and reachable only by privileged processes.
  It is the same server as the GitLab instance's (not necessarily the same
  db), so it is trusted to the level GitLab trusts it, and no further.
- **GitLab's rule for credentials** is the bar
  (`doc/development/secure_coding_guidelines/_index.md`, "At rest"):
  credentials, which include tokens and session cookies, are stored as salted
  hashes where they only need comparing, and **encrypted at rest** where they
  must be used again; they are **never logged**. GitLab follows it by keeping
  credentials out of Redis: the credentials an async job needs (import and
  mirror credentials, `attr_encrypted ... key: :db_key_base` in
  `ProjectImportData` and `RemoteMirror`) are encrypted in Postgres, and the
  job's arguments are IDs (`RepositoryUpdateRemoteMirrorWorker#perform(remote_mirror_id, ...)`).
  Redis and Valkey have no data-encryption primitives (TLS in transit only;
  RDB and AOF files are plain), so encryption is the client's job.
- **Private data in Redis** (cache entries, queued patch bodies) is treated
  as GitLab treats it, stored as it is. GitLab's guidelines cover
  credentials, not data in general; this is a judgment, not a documented rule.
- **Postgres** (the cloudmap database) is shared by every replica and
  backed up with the rest of the application's state.
- **The Python server** runs the most code that untrusted content reaches:
  TOSCA blueprints (in safe mode), templates, YAML, clones of user
  repositories. It is the process most likely to be compromised. The
  baseline still gives it credentials in memory; hardening (§2.5) doesn't.
- **The Rust server** is smaller and parses only HTTP and JSON. It holds the
  key that decrypts grants. Its compromise is out of scope (as GitLab's
  application server holding `db_key_base` is for GitLab).
- **TLS** protects every connection that leaves the host.
- **Logs** pass through `unfurl.logs.SensitiveFilter`, which removes
  credentials from URLs (including `private_token=`-style parameters) and
  redacts values marked sensitive.

### Threats

| # | Threat | Today | Baseline | Hardening |
|---|---|---|---|---|
| T1 | Credentials at rest in Redis: memory, RDB/AOF files, replicas, backups | With a grant store, queued items name a grant in place of `X-Git-Credentials`; without one, they keep the header. Bodies carry `private_token`, `blueprint_url`'s token and `cloud_vars_url`'s, and a batch is replayed with its first item's credentials | Queued items hold grant IDs and plain bodies, no credentials | — |
| T2 | Credentials at rest on disk | The hosted server's blueprint and dependency clones keep plain URLs (`transient_url_credentials`), but its project clones embed credentials on purpose (`_clone_repo`, `set_url_credentials`), and its other clones' fetches take the token from them; older clones keep the tokens they were made with | Remote URLs are plain; credentials reach git per command | — |
| T3 | One user's credentials used for another's action | A batch is forwarded with its *first* item's headers (`queue::consolidate`), so a later item is committed and pushed with the first writer's token; a `private/` clone holds whichever user's token cloned it, and pulls with it for everyone. Tyk has checked that every writer may write and every reader may read, so no one gains access they lack; what's left is that GitLab records the first writer as the pusher, and enforces protected branches by the first writer's role | Batches are split by grant, so each user's writes are pushed with their own token | — |
| T4 | Requests that reach the server without passing through Tyk (defence in depth) | Python checks the secret on every request it receives (`before_request` hook, `serve.py`); the Rust server checks it only on a write it queues, whose replay authenticates with the server's own secret. With Python bound to localhost (the server images), what the Rust server answers by itself skips the check: cache hits served from Redis, and the cloudmap endpoints served from the git-sync database | The Rust server checks the secret on every request, as Python does (CORS preflights exempt) | — |
| T5 | A compromised Python process | It sees every user's token in every request, and every token stored in the checkouts' `.git/config` | It sees the tokens of the requests and batches it handles, in memory; none are stored for it to find | It sees grant IDs only: usable only through the local proxy, within their scope, until they expire |
| T6 | Ambient credentials sent somewhere unintended | Startup clones (`clone.rs`) inherit the server's whole git environment and config (only `GIT_TERMINAL_PROMPT=0` is set); Python's clones follow `.gitmodules` URLs, which a repository's content controls (`recurse_submodules=True`) | Git runs with no ambient credentials and `GIT_ALLOW_PROTOCOL=https` | The proxy adds credentials only for a grant's own host and project |

Out of scope: a compromised Rust server process (or, with hardening, the
`unfurl-git` user), root on the host, GitLab itself, and an attacker who can
read Redis *and* the grant key file.

Accepted: a grant ID in a queued item is a bearer capability for the
worker. Anyone who can write to Redis can queue an item naming another
user's grant, and the worker replays it as that user. That is the exposure
of the tokens queued items hold today, and Redis is trusted to that level.

Note that no design hides CI variable *values* from Python: it decrypts and
re-encrypts vault content with them and deploys with them. Hardening hides
the project *token*, which keeps opening all of a project's variables.

## 2. Design

### 2.1 Grants

A grant is a credential the Rust server holds for a user, for one
repository:

```text
grant: id (random, 128-bit) · user · origin · scopes · token (encrypted)
       · key_id · token_digest · created_at · last_used_at · expires_at
grant_key: key_id · check (an HMAC of a fixed string under the key)
```

- **Created by the Rust server** from the credentials on a request it
  queues: the `X-Git-Credentials` header; `username`/`private_token`/`password`
  in the body; the userinfo of `blueprint_url`; the `private_token`
  parameter of `cloud_vars_url` (a project token). `user` is the
  `X-Unfurl-Username` the gateway sets (empty without one), and `origin` the repository as
  `worktree.origin` spells it (`unfurl.cloud/org/project`): the request's
  `auth_project` on `UNFURL_CLOUD_SERVER`, or `blueprint_url` for its own
  credentials. So the grant for a worktree's remote is an exact match.
- **Not checked with GitLab.** The gateway has checked the caller, and
  GitLab enforces the token's scopes, expiry and revocation whenever it is
  used, so a grant can do no more than its token. A revoked token fails the
  job that uses it (§2.4). So `scopes` is left empty, for hardening to
  fill (§2.5).
- **Expires** `UNFURL_GRANT_TTL_DAYS` (default 30) after the last request
  that brought its token.
- **Stored in the cloudmap database** (Postgres, or SQLite for a server
  built without Postgres), with the token encrypted with AES-256-GCM: a
  random nonce per token, stored with the ciphertext, and the grant ID as
  associated data, so a ciphertext can't be moved to another row. The key
  is 32 random bytes, raw or base64-encoded (a Kubernetes secret mounted as
  a file holds the decoded value), in the file `UNFURL_GRANT_KEY_FILE`, the
  counterpart of GitLab's `db_key_base`. A grant is found by
  `token_digest`, an HMAC of the token under the same key, so, as GitLab's
  rule asks of compare-only values, a dump of the table can't be checked
  against guesses. The database rather than Redis
  because grants must outlive a Redis flush or eviction (GitLab's cache
  instance evicts by LRU), and because this is where GitLab keeps the same
  kind of credential.
- **git-sync stores grants** (its migrations and `Db` methods), with their
  tokens as opaque bytes: it never holds the key. The server encrypts and
  decrypts, and git-sync's own pulls and pushes take credentials from their
  caller, per call, as `GIT_CONFIG_*` entries (§2.3), as git keeps a
  remote while a credential helper keeps its secret.
- **Cached per process**, with a bounded size, by `(token_digest, origin)`
  → `(grant id, expires_at)`, so the hot path, a request bringing a token
  already seen, is a map lookup. The database is written for a new token,
  and to extend `expires_at` at most once an hour.
- **Expired grants are deleted** periodically
  (`DELETE ... WHERE expires_at < now`), and every read, the cache's
  included, checks `expires_at`.
- **The key is checked at startup** against its `grant_key` row, written
  by the first server to use it, so every replica of a database must have
  the same key, and one with the wrong key is found when it starts, not
  when a replay fails. Each database should have its own key.
- **Keys rotate without rewriting the table**: each row names its key, so
  a new key can become current while an old one still decrypts its rows
  until they expire, and then be removed. A leaked key's rows can instead
  be deleted, and are recreated by their users' next requests. One key is
  supported until rotation is needed.
- **Without a key, with the wrong one, or without a cloudmap database**, a
  server with Redis queues credentials as before and warns once, loudly,
  at startup. A server without Redis queues nothing, so needs neither.

### 2.2 The queue

- **Authenticate every request**: the Rust server checks the secret as
  Python does (Bearer header or `?secret=`, CORS preflights exempt) before it
  answers, proxies or queues anything, so a cache hit or a cloudmap request it
  serves by itself is checked too.
- **Store a grant ID for `X-Git-Credentials`**, so the stored headers are
  `X-Unfurl-User` and `X-Forwarded-For`, and the item names its grant.
- **Strip credentials from the stored body**: `username`, `private_token`
  and `password`; the userinfo of `blueprint_url`; the `private_token`
  parameter of `cloud_vars_url`. Each becomes a grant (§2.1), and the stored
  item names its grant ID instead. A queued item holds no token or secret.
- **Partition batches by credential** as well as project and branch, so
  each user's writes reach Python, and are pushed, with their own
  credentials: by grant, or, queueing credentials without grants (§2.1), by
  a hash of them, never the token itself.
- **Items queued before grants**, which name none, replay as they were
  stored.
- **Restore on replay**: the worker loads and decrypts each batch's grants
  and puts the credentials back into the request it forwards to Python over
  localhost: `X-Git-Credentials`, and `blueprint_url` and `cloud_vars_url`
  as the client sent them. Python is unchanged.
- Bodies otherwise stay as they are (§1, "Private data in Redis").

Synchronous requests, which are never stored, are forwarded as today.

### 2.3 Checkouts without credentials

- Clones are made with plain URLs. Credentials reach git per command, in
  its environment: `GIT_CONFIG_*` entries holding a
  `url.<credentialed>.insteadOf=<url>` rewrite, which git applies when it
  connects and doesn't store, so `remote.origin.url` holds no token. Fetches
  and pulls on existing clones (`find_or_create_working_dir`) get the
  request's credentials the same way.
- `apply_url_credentials`, which reused a token by reading it from another
  clone's URL, takes the request's credentials from the `LocalEnv`'s
  overrides instead, set by the server from the request.
- The server's project clones (`_clone_repo`) stop embedding credentials,
  and their pulls use the request's credentials, or, for a pull with no
  request behind it, a grant (§2.4).
- Existing clones' remote URLs are cleaned once, when opened
  (`git remote set-url origin <url without credentials>`).
- Startup clones run git with an emptied environment, no global or system
  config, and `GIT_ALLOW_PROTOCOL=https` (T6).

### 2.4 Work after the request

- **A push of queued writes** uses the grant of the user who wrote them
  (§2.2).
- **A pull with no request behind it** (the startup clone, a scheduled
  pull) uses the most recently used grant for that project. That
  data is then served to each requester only after their own check, so it
  grants no one access they lack. A project with no unexpired grant waits for
  one; a service token for such projects is optional and out of this plan.
- **An expired or revoked grant** fails its job visibly; the user's next
  request makes a new one.

### 2.5 Hardening: keep credentials out of Python's memory (optional)

The baseline leaves credentials in Python's memory while it handles a
request or batch. To take them out of it:

**The Rust server rewrites every request, not only queued ones.** It
creates grants for the credentials on any request (§2.1), removes them, and
gives Python an `X-Git-Grant: <id>` header, which Python reads where it reads
`X-Git-Credentials` today (`_get_body` in `endpoints.py`; `serve.py`'s
`args`), as `username=grant`, `password=<id>`, and `cloud_vars_url` rewritten
to the proxy. The queue worker no longer restores credentials on replay.

**A local credential proxy**, a second listener in `unfurl-server`, on
`127.0.0.1` only (or a Unix socket), never on the public port: grant IDs are
bearer capabilities, so the proxy must be unreachable from outside the
container.

| Route | Forwarded to | Needs |
|---|---|---|
| `GET /git/<host>/<project>.git/info/refs?service=git-upload-pack` | the same path on `<host>` | `git:read` |
| `POST /git/<host>/<project>.git/git-upload-pack` | the same | `git:read` |
| `GET .../info/refs?service=git-receive-pack`, `POST .../git-receive-pack` | the same | `git:write` |
| `GET /api/<host>/projects/<id>/variables` | `/api/v4/projects/<id>/variables` | `api:variables:read` |

- The grant ID arrives as the basic-auth password (git) or a `grant=`
  parameter (the variables URL). The proxy loads and decrypts the grant,
  checks host, project, scope and expiry, and forwards with the real token
  in `Authorization` (git) or `PRIVATE-TOKEN` (API). Anything else is
  refused: a grant opens these routes for its own project and nothing more of
  GitLab.
- Grants' `scopes` are filled with those in the table above: `git:read` and `git:write`
  from the token's own (`GET /api/v4/personal_access_tokens/self`:
  `read_repository`, `write_repository`), and `api:variables:read` only for
  `cloud_vars_url`'s project token. Without the proxy, GitLab's own check of
  the token is enough (§2.1).
- Bodies stream both ways, with no size limit and long timeouts. Pack data
  can be hundreds of megabytes; the public router's `max_body_bytes` and
  timeouts don't apply here.
- The token never leaves the Rust process. The proxy logs grant IDs, never
  tokens.
- Python's git reaches it through one `insteadOf` rule per GitLab host, set
  in Python's environment
  (`GIT_CONFIG_KEY_n=url.http://127.0.0.1:<port>/git/<host>/.insteadOf`,
  `GIT_CONFIG_VALUE_n=https://<host>/`), so `get_project_url`,
  `add_transient_credentials` and the rest work unchanged, with a grant ID
  where a token was.
- Git LFS isn't covered. If the repositories use it, its batch API needs
  routes of its own.

**Two users in the container**, so Python can't read what the Rust server
holds:

| User | Runs | Can read |
|---|---|---|
| `unfurl` | gunicorn and the git it starts | checkouts, grant IDs |
| `unfurl-git` | `unfurl-server`: proxy, grant store, queue worker, git-sync | the grant key file (mode `0400`), its own memory |

- The entrypoint starts each as its user (`gosu`), under `tini -g`.
- The checkouts are shared: one group, group-writable files (umask `002`,
  setgid directories), and `safe.directory` for both users, which git
  otherwise refuses for a repository another user owns.
- Linux keeps `unfurl` out of `unfurl-git`'s memory, its `/proc/<pid>/environ`
  and its files.

Requests reaching Python directly (`unfurl serve` without Redis, or
`UNFURL_RUST_SERVER=0`) keep today's behaviour, since no grant header
arrives.

## 3. Rollout

Each phase stands on its own.

**Phase 0: checkouts without credentials (§2.3)** for the hosted server's
project clones, existing clones' remote URLs, and startup clones'
environment.

**Phase 1: grants, and a queue that stores none (§2.1, 2.2, 2.4).** In
order, each standing on its own:
1. The grant table and its queries (git-sync) and the server's grant
   store: key, encryption, create, look up, expire.
2. Batches partitioned by credential.
3. Credentials in the body, `blueprint_url` and `cloud_vars_url` become
   grants too.
4. Work after the request uses grants: the startup clone (`clone.rs`), and
   git-sync's pulls and pushes when they land; a grant whose token git
   refuses is deleted.
5. The Rust server checks the secret on every request (T4).

This completes the baseline. Until a deployment has a key, its queue
stores credentials as before (§2.1).

**Phase 2 (optional): hardening.** The Rust server rewriting every request;
the local proxy; Python reading `X-Git-Grant` and the `insteadOf` rules; the
image running Python and the Rust server as `unfurl` and `unfurl-git`, with
the key readable only by the latter (§2.5).

## 4. Behaviour changes

- A clone made for one request is pulled for a later one with that later
  request's credentials, not with the token of whoever cloned it (Phase 0).
- Each user's queued writes are forwarded, and pushed, as that user;
  concurrent writers to one project and branch make more, smaller batches.
- A request without a valid API secret is refused by the Rust server, cache
  hits and cloudmap requests included, and a queued write is refused when
  queued, not when forwarded.
- Deployments with Redis need a grant key (a mounted secret) and a
  cloudmap database for Phase 1; without them, the server warns at startup
  and queues credentials as before.
- With a grant store, a queued write whose credentials can't be stored
  (the database is down) is refused with a 503, not queued with them; and a
  batch whose grant has expired, or that a replica without the key drains,
  fails as the write would, with a 401, rather than reaching Python without
  credentials.
- A grant expires `UNFURL_GRANT_TTL_DAYS` after its token was last seen, so
  background work for a project nobody has used for that long waits.
- With hardening, Python never receives a user's token, a project token or
  the API secret, only grant IDs and the proxy's URLs; deployments also need
  the `unfurl-git` user.

## 5. Tests

Each guards an invariant, so each is checked by breaking what it guards
(AGENTS.md):

- no token in any Redis queue item, header, endpoint or body;
- a replayed batch reaches Python with its own grant's credentials, and two
  users' writes in one batch window are forwarded separately;
- with a secret set, the Rust server refuses a request without it on
  a cache hit, a cloudmap read and write, and a queued write;
- no token in any clone's `.git/config` after a credentialed clone, and a
  later clone on the same host still authenticates;
- the stored token is ciphertext, decrypts only with the key, and only in
  its own row;
- without a key, queued writes still replay with their credentials;
- with hardening: the proxy refuses another project, another host,
  `receive-pack` for a `git:read` grant, any route outside the allowlist, an
  expired grant, and any connection not from localhost.

## 6. Open questions

1. Whether a user's newer token replaces their older grants for the same
   project.
2. Projects with no recent grant: wait, or a service token (§2.4).
3. Where the grant key comes from in each deployment (a Kubernetes or Docker
   secret).
4. For hardening: whether the repositories use Git LFS (§2.5).
