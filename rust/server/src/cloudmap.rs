// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Local handler for `GET /cloudmap` backed by the `unfurl-git-sync`
//! crate.
//!
//! Wires up a [`unfurl_git_sync::SyncedRepo`] at startup when both
//! `cloudmap_repo` and `cloudmap_db_url` are configured. The handler
//! reads the cloudmap from the working tree, applies optional `kind` /
//! `key` filters, and (when `follow > 0`) walks
//! [`unfurl_git_sync::SyncedRepo::find_records_follow`] to populate the
//! second element of the response pair.
//!
//! When the repo is not configured, the handler proxies the request to
//! the Python backend.

use axum::{
    extract::State,
    http::{Request, StatusCode},
    response::{IntoResponse, Json, Response},
};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc;
use tokio::sync::Mutex;
use unfurl_git_sync::{BatchOp, CommitRef, DbConfig, FormatRegistry, Record, SyncedRepo};

use crate::proxy;
use axum::extract::FromRequest;

use crate::routes::{ValidatedJson, ValidatedQuery};
use crate::unfurl_types;
use crate::AppState;

/// Maps cloudmap section name (the URL query value) to its
/// JSON-pointer path stored in the `record.path` column.
const KIND_TO_PATH: &[(&str, &str)] = &[
    ("repositories", "/repositories"),
    ("artifacts", "/artifacts"),
    ("components", "/components"),
    ("services", "/services"),
    ("instantiations", "/instantiations"),
    ("types", "/types"),
];

/// Whether `project_id` (an `auth_project` query value) names the repository
/// `origin` belongs to.
///
/// `worktree.origin` is the remote URL with the scheme stripped, e.g.
/// `unfurl.cloud/onecommons/cloudmap.git`, so a project id like
/// `onecommons/cloudmap` is a trailing path segment of it, with `.git`
/// optional on either side.
///
/// A worktree with no configured remote records its filesystem path as the
/// origin instead, which matches no project id. Serving such a checkout is what
/// `--dev-mode` is for; it bypasses this check entirely.
fn origin_matches(origin: &str, project_id: &str) -> bool {
    fn strip(s: &str) -> &str {
        let s = s.trim_matches('/');
        s.strip_suffix(".git").unwrap_or(s)
    }
    let origin = strip(origin);
    let project = strip(project_id);
    if project.is_empty() {
        return false;
    }
    origin == project || origin.ends_with(&format!("/{project}"))
}

/// Reverse lookup: `record.path` → cloudmap section name.
fn section_for_path(path: &str) -> Option<&'static str> {
    for (section, p) in KIND_TO_PATH {
        if *p == path {
            return Some(section);
        }
    }
    None
}

fn path_for_kind(kind: &str) -> Option<&'static str> {
    for (section, p) in KIND_TO_PATH {
        if *section == kind {
            return Some(p);
        }
    }
    None
}

/// Long-lived cloudmap handle stored in [`crate::AppState`].
#[derive(Clone)]
pub struct CloudMapState {
    inner: Arc<SyncedRepo>,
    /// Lazily-built reverse `extends` adjacency of the `/types`
    /// section, used by the `type` query filter, keyed by the
    /// request's `cloudmap_path` (`None` = every file). See
    /// [`CloudMapState::subtype_names`].
    types_cache: Arc<Mutex<HashMap<Option<String>, TypesCache>>>,
}

/// Cached reverse `extends` adjacency of the `/types` section.
struct TypesCache {
    /// `(COUNT(*), MAX(version))` of the `/types` section at build
    /// time ([`SyncedRepo::section_stat`]). The cache is stale exactly
    /// when a fresh probe returns a different pair.
    stat: (i64, Option<i64>),
    /// Parent type name → type names that directly declare it in
    /// their `extends` list.
    children: HashMap<String, Vec<String>>,
}

impl CloudMapState {
    /// Open the working dir, run an initial scan, and return a handle
    /// suitable for stashing in [`crate::AppState`].
    ///
    /// Both `repo_path` and `db_url` must be set. The DB URL is parsed
    /// to pick the right backend: anything starting with `postgres://`
    /// or `postgresql://` is Postgres (only when the
    /// `unfurl-git-sync/postgres` feature is enabled at build time);
    /// everything else is treated as SQLite.
    pub async fn open(
        repo_path: &str,
        db_url: &str,
    ) -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        let db_cfg = if db_url.starts_with("postgres://") || db_url.starts_with("postgresql://") {
            #[cfg(feature = "postgres")]
            {
                DbConfig::Postgres { url: db_url.into() }
            }
            #[cfg(not(feature = "postgres"))]
            {
                return Err(format!(
                    "cloudmap_db_url is Postgres ({db_url}) but unfurl-server was built \
                     without the `postgres` feature"
                )
                .into());
            }
        } else {
            DbConfig::Sqlite { url: db_url.into() }
        };

        let registry = FormatRegistry::with_builtins();
        let synced = SyncedRepo::open(repo_path, db_cfg, registry).await?;
        synced.update_from_working_dir().await?;
        Ok(Self {
            inner: Arc::new(synced),
            types_cache: Arc::new(Mutex::new(HashMap::new())),
        })
    }

    /// Wrap an already-opened [`SyncedRepo`] in a [`CloudMapState`].
    ///
    /// Useful in tests that want to control the working dir, database
    /// URL, and format registry directly rather than going through
    /// [`CloudMapState::open`].
    pub fn from_synced(synced: SyncedRepo) -> Self {
        Self {
            inner: Arc::new(synced),
            types_cache: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Flush every in-flight record to its file and commit the result,
    /// returning the new commit oid — or `None` when nothing was staged.
    ///
    /// Driven by `commit: true` on `POST /cloudmap`; without it the handler
    /// leaves records staged and the commit is somebody else's job.
    pub async fn commit(&self, message: &str) -> Result<Option<String>, unfurl_git_sync::Error> {
        self.inner.commit_repository(message).await
    }

    /// Decide whether a request's `auth_project` may be served from the one
    /// repository this handler is configured with.
    ///
    /// The fast-path serves a single `cloudmap_repo`, so a request for a
    /// different project must not be answered from it, and a request naming no
    /// project has nothing to attribute the write to.
    ///
    /// `dev_mode` (see [`crate::config::Config::dev_mode`]) skips both checks:
    /// the server was started on one checkout and its clients aren't expected
    /// to name it. That's a deliberate switch rather than something inferred
    /// from the origin, because a local checkout can have a remote too.
    pub async fn project_check(
        &self,
        project_id: Option<&str>,
        dev_mode: bool,
    ) -> Result<ProjectCheck, unfurl_git_sync::Error> {
        if dev_mode {
            return Ok(ProjectCheck::Serve);
        }
        let origin = self.inner.get_worktree().await?.origin;
        Ok(match project_id.filter(|p| !p.is_empty()) {
            Some(p) if origin_matches(&origin, p) => ProjectCheck::Serve,
            Some(_) => ProjectCheck::OtherProject,
            None => ProjectCheck::Missing,
        })
    }

    /// The repository's HEAD commit as the recorded in the database.
    ///
    /// Read from the `worktree` row rather than from git. It can be stale if
    /// something outside this process commits, which the next scan corrects.
    pub async fn head_commit(&self) -> Result<Option<String>, unfurl_git_sync::Error> {
        Ok(self.inner.get_worktree().await?.commit_id)
    }

    /// Expand `type_name` to itself plus every subtype — every type
    /// record whose `extends` list (transitively) contains it.
    ///
    /// The reverse-extends adjacency is built from the `/types`
    /// section and cached; each call re-probes the section's
    /// `(COUNT(*), MAX(version))` stat ([`SyncedRepo::section_stat`])
    /// and rebuilds only when the pair moved, so writes to other
    /// sections leave the cache warm. `type_name` need not have a
    /// type record — the result then is just the name itself.
    async fn subtype_names(
        &self,
        type_name: &str,
        file_path: Option<&str>,
    ) -> Result<Vec<String>, unfurl_git_sync::Error> {
        let stat = self.inner.section_stat("/types").await?;
        let cache_key = file_path.map(str::to_string);
        let mut guard = self.types_cache.lock().await;
        // (`Option::is_none_or` reads better but is stable only since
        // Rust 1.82; MSRV is 1.70.)
        if !matches!(guard.get(&cache_key), Some(c) if c.stat == stat) {
            // Two separate queries (probe, then fetch) — a write
            // landing in between can pair a fresh section with a
            // stale stat or vice versa; either way the next request's
            // probe mismatches and rebuilds, so the cache is at most
            // one write behind, never stuck.
            let records = self
                .inner
                .find_records(
                    cache_key.clone(),
                    Some("/types".into()),
                    None,
                    false,
                    None,
                    None,
                )
                .await?;
            let mut children: HashMap<String, Vec<String>> = HashMap::new();
            for r in &records {
                let Some(extends) = r.json.get("extends").and_then(Value::as_array) else {
                    continue;
                };
                for parent in extends.iter().filter_map(Value::as_str) {
                    // Type records commonly list themselves first in
                    // `extends`; skip the self-edge.
                    if parent != r.key {
                        children
                            .entry(parent.to_string())
                            .or_default()
                            .push(r.key.clone());
                    }
                }
            }
            guard.insert(cache_key.clone(), TypesCache { stat, children });
        }
        let cache = guard.get(&cache_key).expect("types cache just populated");

        // BFS over the reverse edges, starting at (and including) the
        // requested name. `extends` lists are often pre-flattened
        // (full ancestor closure) — the walk handles both that and
        // direct-parents-only producers.
        let mut out: Vec<String> = Vec::new();
        let mut seen: HashSet<&str> = HashSet::from([type_name]);
        let mut queue: Vec<&str> = vec![type_name];
        while let Some(name) = queue.pop() {
            out.push(name.to_string());
            if let Some(kids) = cache.children.get(name) {
                for kid in kids {
                    if seen.insert(kid.as_str()) {
                        queue.push(kid.as_str());
                    }
                }
            }
        }
        Ok(out)
    }
}

/// Axum handler for `GET /cloudmap`.
///
/// Local fast-path when [`AppState::cloudmap`] is set; otherwise
/// proxies to the Python backend.
pub async fn handle_cloudmap(
    State(state): State<AppState>,
    ValidatedQuery(params): ValidatedQuery<unfurl_types::GetCloudmapRequestQuery>,
    req: Request<axum::body::Body>,
) -> Response {
    let Some(cm) = state.cloudmap.clone() else {
        return proxy::forward(
            &state.client,
            &state.config.backend_url(),
            req,
            state.config.max_body_bytes,
        )
        .await;
    };
    // A request naming a different project must not be answered from the one
    // repository this handler serves; hand it to python, which routes per
    // project. A request naming none keeps the previous behaviour.
    {
        match cm
            .project_check(params.auth_project.as_deref(), state.config.dev_mode())
            .await
        {
            // Reads without an `auth_project` keep working: they resolve to the
            // configured repo, as they always have.
            Ok(ProjectCheck::Serve) | Ok(ProjectCheck::Missing) => {}
            Ok(ProjectCheck::OtherProject) => {
                tracing::debug!(
                    "auth_project {:?} is not the configured cloudmap repo; proxying",
                    params.auth_project
                );
                return proxy::forward(
                    &state.client,
                    &state.config.backend_url(),
                    req,
                    state.config.max_body_bytes,
                )
                .await;
            }
            Err(e) => {
                tracing::error!("cloudmap worktree lookup failed: {}", e);
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(json!({"error": format!("worktree: {e}")})),
                )
                    .into_response();
            }
        }
    }
    drop(req);

    match build_response(&cm, &params).await {
        Ok(body) => Json(body).into_response(),
        Err(LocalError::NotFound(msg)) => {
            (StatusCode::NOT_FOUND, Json(json!({"error": msg}))).into_response()
        }
        Err(LocalError::Internal(msg)) => {
            tracing::error!("cloudmap handler error: {}", msg);
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": msg})),
            )
                .into_response()
        }
    }
}

enum LocalError {
    NotFound(String),
    Internal(String),
}

/// Outcome of [`CloudMapState::project_check`].
#[derive(Debug, PartialEq, Eq)]
pub enum ProjectCheck {
    /// The request may be served from the configured repository.
    Serve,
    /// The request named a different project — python routes per project, so
    /// it should be proxied there.
    OtherProject,
    /// The request named no project and the configured repository has a
    /// remote, so there is nothing to attribute the request to.
    Missing,
}

/// Build the `[primary, followed]` pair returned by `GET /cloudmap`.
///
/// Each element is a CloudMap-shaped object — declared as
/// [`unfurl_types::CloudMapDocumentPair`] in the OpenAPI spec. We don't
/// strict-deserialize through [`unfurl_types::CloudMapDocument`] on emit
/// because the typed struct has required fields (`apiVersion`,
/// `kind`) that get filled with defaults; Python's `get_cloudmap`
/// returns a bare `{}` for an empty `followed` and we keep wire
/// parity. So we emit each element as a [`Value::Object`] containing
/// only the section maps that actually have records.
async fn build_response(
    cm: &CloudMapState,
    params: &unfurl_types::GetCloudmapRequestQuery,
) -> Result<Vec<Value>, LocalError> {
    let synced = cm.inner.as_ref();
    let kind = params.kind.as_deref();
    let key = params.key.as_deref();
    // Scope the read to one cloudmap file when the request names one; `None` matches
    // records from every file in the worktree, which is what a request without
    // `cloudmap_path` has always done.
    let file_path = params
        .cloudmap_path
        .as_deref()
        .map(str::trim)
        .filter(|p| !p.is_empty());

    // Resolve `kind` to its JSON-pointer path up-front so an unknown
    // kind 404s before we issue any DB query.
    let path: Option<&'static str> = match kind {
        Some(k) => Some(
            path_for_kind(k)
                .ok_or_else(|| LocalError::NotFound(format!("section {k:?} not found")))?,
        ),
        None => None,
    };

    // Walking only makes sense when we have a starting key.
    // alias=true when filtering by key (so versioned URLs hit their
    // canonical record); alias=false for the full document.
    let follow = if key.is_some() {
        params.follow.unwrap_or(0).max(0) as u32
    } else {
        0
    };
    let alias = key.is_some();

    // Single DB query for both halves of the response. With follow=0
    // `find_records_follow` returns the initial set and an empty
    // followed Vec — same shape as a plain `find_records` call.
    // `since_version` is pushed into the SQL `WHERE` clause by
    // `db::record::find`, so both the initial set and any follow-walk
    // edge lookups are filtered at the database.
    // `exclude` arrives as a comma-separated list of record primary-key
    // ids; non-numeric tokens are silently dropped. Empty / `None`
    // string means "no exclusion" — matches the schema description.
    let exclude_ids: Vec<i64> = params
        .exclude
        .as_deref()
        .map(|s| {
            s.split(',')
                .filter_map(|tok| tok.trim().parse::<i64>().ok())
                .collect()
        })
        .unwrap_or_default();

    // Expand the optional `type` filter to the requested name plus
    // all of its (transitive) subtypes per the `/types` section, then
    // push the name set down into the SQL record match. Applies to
    // the initial set only — the follow walk stays unfiltered.
    let type_names: Option<Vec<String>> = match params.r#type.as_deref() {
        Some(t) if !t.is_empty() => Some(
            cm.subtype_names(t, file_path)
                .await
                .map_err(|e| LocalError::Internal(format!("subtype_names: {e}")))?,
        ),
        _ => None,
    };
    let type_filtered = type_names.is_some();

    let (initial, followed_records) = synced
        .find_records_follow(
            file_path.map(str::to_string),
            path.map(|s| s.to_string()),
            key.map(|s| s.to_string()),
            alias,
            follow,
            params.since_version,
            exclude_ids,
            type_names,
        )
        .await
        .map_err(|e| LocalError::Internal(format!("find_records_follow: {e}")))?;

    if let (Some(kind_str), Some(key_str)) = (kind, key) {
        if initial.is_empty() {
            let hint = if type_filtered {
                " with matching type"
            } else {
                ""
            };
            return Err(LocalError::NotFound(format!(
                "key {key_str:?} not found in {kind_str:?}{hint}"
            )));
        }
    }

    // `select` reduces every returned record (both halves of the
    // pair) to the requested properties. Parsed once here; empty /
    // all-blank values mean "no projection".
    let select_paths: Option<Vec<SelectPath>> = params
        .select
        .as_deref()
        .map(parse_select)
        .filter(|p| !p.is_empty());

    let primary = records_to_document(initial, select_paths.as_deref());
    let followed = records_to_document(followed_records, select_paths.as_deref());
    Ok(vec![primary, followed])
}

/// A parsed `select` entry: the special `$key` item or a JSON
/// Pointer's unescaped reference tokens (paired with the raw pointer
/// string for [`Value::pointer`] resolution).
enum SelectPath {
    /// The literal `$key` item — adds the record's key under `"$key"`.
    Key,
    /// A JSON Pointer: `(raw, unescaped reference tokens)`.
    Pointer(String, Vec<String>),
}

/// Parse the `select` query param: comma-separated JSON Pointers
/// (RFC 6901). Items are trimmed and empties dropped; a missing
/// leading `/` is prepended; `$key` maps to [`SelectPath::Key`].
/// Pointers that have a strict prefix also in the list are dropped —
/// the ancestor pointer already selects the whole subtree.
fn parse_select(raw: &str) -> Vec<SelectPath> {
    let mut pointers: Vec<(String, Vec<String>)> = Vec::new();
    let mut keys = 0usize;
    for part in raw.split(',') {
        let part = part.trim();
        if part.is_empty() {
            continue;
        }
        if part == "$key" {
            keys += 1;
            continue;
        }
        let ptr = if part.starts_with('/') {
            part.to_string()
        } else {
            format!("/{part}")
        };
        let tokens: Vec<String> = ptr
            .split('/')
            .skip(1)
            .map(|t| t.replace("~1", "/").replace("~0", "~"))
            .collect();
        pointers.push((ptr, tokens));
    }
    let mut out: Vec<SelectPath> = Vec::new();
    if keys > 0 {
        out.push(SelectPath::Key);
    }
    for (i, (ptr, tokens)) in pointers.iter().enumerate() {
        let covered = pointers.iter().enumerate().any(|(j, (_, other))| {
            i != j && other.len() < tokens.len() && &tokens[..other.len()] == other.as_slice()
        });
        if !covered {
            out.push(SelectPath::Pointer(ptr.clone(), tokens.clone()));
        }
    }
    out
}

/// Reduce `record` to the properties named by `select`, keeping their
/// nested structure (array indices become object keys). Unresolvable
/// paths are omitted; [`SelectPath::Key`] adds the record's key.
fn project_record(record: &Value, key: &str, select: &[SelectPath]) -> Value {
    let mut out = Map::new();
    for path in select {
        match path {
            SelectPath::Key => {
                out.insert("$key".to_string(), Value::from(key));
            }
            SelectPath::Pointer(raw, tokens) => {
                let Some(value) = record.pointer(raw) else {
                    continue;
                };
                set_nested(&mut out, tokens, value.clone());
            }
        }
    }
    Value::Object(out)
}

/// Insert `value` at the nested location named by `tokens`, creating
/// intermediate objects as needed. Prefix-dedup in [`parse_select`]
/// guarantees no intermediate node is a non-object leaf.
fn set_nested(out: &mut Map<String, Value>, tokens: &[String], value: Value) {
    match tokens {
        [] => {}
        [leaf] => {
            out.insert(leaf.clone(), value);
        }
        [first, rest @ ..] => {
            let entry = out
                .entry(first.clone())
                .or_insert_with(|| Value::Object(Map::new()));
            if let Value::Object(map) = entry {
                set_nested(map, rest, value);
            }
        }
    }
}

/// Group records by section (`record.path`) and emit a CloudMap-shaped
/// object: `{ section: { key: json } }`. An empty input produces `{}`.
///
/// Each record's JSON object is enriched with three extra keys so
/// clients can identify the row and echo back a
/// [`unfurl_git_sync::CommitRef`] for optimistic-concurrency on
/// subsequent writes:
///
/// - `"unfurl.server.id"` — the row's primary-key id (always
///   present); useful as a stable identifier across renames and as
///   an entry in the `exclude` list passed to a follow-up
///   `find_records_follow`.
/// - `"unfurl.server.version"` — the row's [`Record::version`]
///   (always present); use as `CommitRef::Pending(v)`.
/// - `"unfurl.server.commit"` — the row's `commit_id`, or `null`
///   when the record is in-flight; use as `CommitRef::Commit(o)` when
///   non-null.
///
/// Records whose JSON payload isn't an object are left as-is — the
/// cloudmap format only emits map-valued records, so this is a
/// defensive fallthrough.
///
/// When `select` is set, each record is then reduced via
/// [`project_record`]. Projection runs **after** annotation, so the
/// `unfurl.server.*` keys are selectable (and dropped otherwise).
fn records_to_document(records: Vec<Record>, select: Option<&[SelectPath]>) -> Value {
    let mut sections: BTreeMap<&'static str, Map<String, Value>> = BTreeMap::new();
    for r in records {
        let Some(section) = section_for_path(&r.path) else {
            continue;
        };
        let enriched = annotate_record(r.json, r.id, r.version, r.commit_id);
        let entry = match select {
            Some(paths) => project_record(&enriched, &r.key, paths),
            None => enriched,
        };
        sections.entry(section).or_default().insert(r.key, entry);
    }
    let mut out = Map::new();
    for (section, entries) in sections {
        out.insert(section.to_string(), Value::Object(entries));
    }
    Value::Object(out)
}

/// Splice the row's identity + OCC tokens onto its JSON payload.
/// No-op when the payload isn't a JSON object.
fn annotate_record(json: Value, id: i64, version: i64, commit_id: Option<String>) -> Value {
    let Value::Object(mut map) = json else {
        return json;
    };
    map.insert("unfurl.server.id".to_string(), Value::from(id));
    map.insert("unfurl.server.version".to_string(), Value::from(version));
    map.insert(
        "unfurl.server.commit".to_string(),
        commit_id.map(Value::from).unwrap_or(Value::Null),
    );
    Value::Object(map)
}

// ---------------------------------------------------------------------------
// POST /cloudmap — write handler.
// ---------------------------------------------------------------------------

/// Pop the OCC tokens out of a record's JSON object and convert them
/// into a [`CommitRef`].
///
/// - `unfurl.server.commit` (string) → [`CommitRef::Commit`].
/// - `unfurl.server.version` (i64) → [`CommitRef::Pending`].
/// - When both are present, `Commit` wins (it's the stricter token).
/// - All three identity keys (including `unfurl.server.id`) are
///   popped regardless so none leak into the payload that gets
///   persisted to disk.
fn pop_commit_ref(map: &mut Map<String, Value>) -> Option<CommitRef> {
    let oid = map
        .remove("unfurl.server.commit")
        .and_then(|v| v.as_str().map(str::to_string));
    let version = map.remove("unfurl.server.version").and_then(|v| v.as_i64());
    // `unfurl.server.id` is server-assigned identity; the row already
    // owns its id, so the client's echo is dropped without affecting
    // the OCC decision.
    map.remove("unfurl.server.id");
    if let Some(o) = oid {
        return Some(CommitRef::Commit(o));
    }
    version.map(CommitRef::Pending)
}

/// Local axum handler for `POST /cloudmap`.
///
/// Body is the typed [`unfurl_types::CloudMapDocument`]; the
/// [`ValidatedJson`] extractor turns a shape mismatch into a **422
/// Unprocessable Entity** (matching APIFlask on the Python backend)
/// instead of axum's default 400. Each
/// section maps record keys (URLs) to objects that schema-validate
/// as the corresponding cloudmap entity. Two extension keys on the
/// record drive special behaviour:
///
/// - `unfurl.server.{version,commit}` — optional OCC token gating
///   the write.
/// - `unfurl.server.deleted: true` — delete the record (OCC tokens
///   still honoured).
///
/// Unknown top-level sections produce a **400 Bad Request** with an
/// `error: "unknown section <name>"` body, matching the Python
/// handler's behaviour. Serde alone would silently drop them (the
/// generated type uses `#[serde(flatten)] additional_properties` for
/// forward-compat with new envelope keys); the handler explicitly
/// inspects that bag and rejects truly unknown keys before applying.
/// Record fields not in the schema are still silently dropped.
///
/// Errors fail fast: the first record whose OCC token mismatches
/// returns 409 and the remainder are skipped. Previously-applied
/// edits stay in-flight (queryable via
/// [`unfurl_git_sync::SyncedRepo::list_changes`]); no `save_changes`
/// / `commit_repository` is invoked — the caller drives commit
/// separately.
///
/// This handler is registered only when `state.cloudmap` is `Some`;
/// see [`post_cloudmap_proxy`] for the unconfigured fall-through.
pub async fn post_cloudmap_local(
    State(state): State<AppState>,
    ValidatedQuery(params): ValidatedQuery<unfurl_types::PostCloudmapRequestParamsQuery>,
    req: Request<axum::body::Body>,
) -> Response {
    let cm = state
        .cloudmap
        .as_ref()
        .expect("post_cloudmap_local registered without CloudMapState");

    // Writes must name the project they target, the same as the python handler
    // (`get_project_id_or_abort`) -- this handler serves one repository and
    // would otherwise apply an unattributed write to it.
    match cm
        .project_check(params.auth_project.as_deref(), state.config.dev_mode())
        .await
    {
        Ok(ProjectCheck::Serve) => {}
        Ok(ProjectCheck::Missing) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({
                    "code": "BAD_REQUEST",
                    "message": "Missing required query parameter 'auth_project'",
                })),
            )
                .into_response();
        }
        Ok(ProjectCheck::OtherProject) => {
            tracing::debug!(
                "auth_project {:?} is not the configured cloudmap repo; proxying",
                params.auth_project
            );
            return proxy::forward(
                &state.client,
                &state.config.backend_url(),
                req,
                state.config.max_body_bytes,
            )
            .await;
        }
        Err(e) => {
            tracing::error!("cloudmap worktree lookup failed: {}", e);
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": format!("worktree: {e}")})),
            )
                .into_response();
        }
    }

    // Only now consume the body: proxying above needs the request intact.
    let body =
        match ValidatedJson::<unfurl_types::PostCloudmapRequest>::from_request(req, &state).await {
            Ok(ValidatedJson(body)) => body,
            Err(rejection) => return rejection,
        };
    match post_cloudmap_apply(cm, body).await {
        Ok(response) => response.into_response(),
        Err(err) => err.into_response(),
    }
}

/// The local write itself, split out so [`post_cloudmap_local`] can own the
/// auth_project checks and the raw request needed to proxy past them.
async fn post_cloudmap_apply(
    cm: &CloudMapState,
    body: unfurl_types::PostCloudmapRequest,
) -> Result<Json<unfurl_types::PatchResponse>, ApiError> {
    // Reject unknown top-level keys (sections or envelope) the same
    // way the Python handler does — they end up in the typed
    // request's `additional_properties` bag because oas3-gen reflects
    // the Pydantic `extra="allow"` config as a flatten'd HashMap, and
    // serde otherwise drops them silently.
    if let Some(unknown) = body.additional_properties.keys().next() {
        return Err(WriteError::BadRequest(format!("unknown section {unknown:?}")).into());
    }
    // By default the Rust handler only stages records to the SyncedRepo's
    // database (in-flight, `commit_id IS NULL`) and leaves the commit to the
    // caller, so the reported `commit` is just the unchanged HEAD. `commit:
    // true` asks this handler to drive it instead: the staged records are
    // flushed to disk and committed before responding. Either way we return
    // the largest `version` the CRUD
    // calls stamped during this batch as `queueid`: the client can echo it back
    // as `unfurl.server.version` on the next request to gate the
    // optimistic-concurrency check. Versions are monotonic per worktree, so the
    // last write's version is also the largest.
    let atomic = body.atomic.unwrap_or(true);
    let commit_requested = body.commit.unwrap_or(false);
    // Not scoped to `cloudmap_path`: `commit_repository` commits every file with
    // staged records, which may be more than the one this request named.
    let commit_msg = body
        .commit_msg
        .clone()
        .unwrap_or_else(|| "Update cloudmap".to_string());

    // Repository-level optimistic concurrency, mirroring the python handler:
    // refuse the write if the repository has moved since the commit the client
    // last saw. Skipped when either side has nothing to compare, same as there.
    //
    // This is complementary to the per-record `unfurl.server.{version,commit}`
    // check, not a substitute: another client's *staged* writes don't move
    // HEAD, so they slip past this one and are caught per record instead.
    let head = cm
        .head_commit()
        .await
        .map_err(|e| WriteError::Internal(format!("head_commit: {e}")))?;
    if let (Some(expected), Some(actual)) = (body.latest_commit.as_deref(), head.as_deref()) {
        if expected != actual {
            return Err(ApiError {
                status: StatusCode::CONFLICT,
                body: json!({
                    "error": format!(
                        "cloudmap has changed since latest_commit {expected}, \
                         current revision is {actual}"
                    )
                }),
            });
        }
    }

    let result = apply_writes(cm, body, atomic).await?;
    // A body carrying no records is legitimate here — it means "commit whatever
    // is already staged". `commit_repository` is itself a no-op returning None
    // when nothing is dirty, so there's no separate emptiness check to make.
    let committed = if commit_requested {
        cm.commit(&commit_msg)
            .await
            .map_err(|e| WriteError::Internal(format!("commit_repository: {e}")))?
    } else {
        None
    };
    // `commit` reports where the repository is now, matching the python
    // handlers: the commit just made, or the unchanged HEAD when this request
    // only staged records (the client's OCC token for staged writes is
    // `queueid`, not this).
    // `commit_repository` hands back the oid it just wrote; without a commit
    // HEAD is unchanged, so the value read for the check above still stands.
    let commit = committed.or(head);
    Ok(Json(unfurl_types::PatchResponse {
        commit,
        queueid: result.last_version,
        applied: Some(result.applied),
    }))
}

/// Proxy fallthrough for `POST /cloudmap` when `state.cloudmap` is
/// `None`. Forwards the request verbatim to the Python backend.
///
/// Registered in place of [`post_cloudmap_local`] at server startup
/// when no cloudmap repo is configured; see `main.rs` for the
/// branching.
pub async fn post_cloudmap_proxy(
    State(state): State<AppState>,
    req: Request<axum::body::Body>,
) -> Response {
    proxy::forward(
        &state.client,
        &state.config.backend_url(),
        req,
        state.config.max_body_bytes,
    )
    .await
}

/// Errors returned by [`post_cloudmap_local`]. `IntoResponse` maps
/// each variant to the appropriate HTTP status with a JSON body.
pub struct ApiError {
    status: StatusCode,
    body: Value,
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.status, Json(self.body)).into_response()
    }
}

impl From<WriteError> for ApiError {
    fn from(err: WriteError) -> Self {
        match err {
            WriteError::BadRequest(msg) => Self {
                status: StatusCode::BAD_REQUEST,
                body: json!({"error": msg}),
            },
            WriteError::Conflict {
                section,
                key,
                actual,
                applied,
                failed,
            } => Self {
                status: StatusCode::CONFLICT,
                body: json!({
                    "error": "conflict",
                    "section": section,
                    "key": key,
                    "actual": actual,
                    "applied": applied
                        .iter()
                        .map(|a| json!({
                            "section": a.section,
                            "key": a.key,
                            "version": a.version,
                        }))
                        .collect::<Vec<_>>(),
                    "failed": failed
                        .iter()
                        .map(|f| json!({
                            "section": f.section,
                            "key": f.key,
                            "actual": f.actual,
                            "error": f.error,
                        }))
                        .collect::<Vec<_>>(),
                }),
            },
            WriteError::Internal(msg) => {
                tracing::error!("post_cloudmap error: {}", msg);
                Self {
                    status: StatusCode::INTERNAL_SERVER_ERROR,
                    body: json!({"error": msg}),
                }
            }
        }
    }
}

enum WriteError {
    BadRequest(String),
    Conflict {
        /// First conflicting record's section (back-compat with the
        /// pre-batch response shape).
        section: String,
        /// First conflicting record's key.
        key: String,
        /// First conflicting record's server-side ``commit_id``.
        actual: Option<String>,
        /// All records that committed before the failure (atomic mode
        /// always rolls back, so this is empty there). Non-atomic
        /// mode populates this with everything that landed.
        applied: Vec<unfurl_types::PatchResponseAppliedRecord>,
        /// All records that did not commit. In atomic mode this is the
        /// single record that triggered the rollback; in non-atomic
        /// mode this is every record skipped.
        failed: Vec<FailedJson>,
    },
    Internal(String),
}

/// Per-record failure detail surfaced in the 409 response body.
#[derive(Clone, Debug)]
struct FailedJson {
    section: String,
    key: String,
    actual: Option<String>,
    error: Option<String>,
}

/// Result of applying a CloudMapDocument body via [`apply_writes`].
struct WriteOutcome {
    last_version: Option<i64>,
    applied: Vec<unfurl_types::PatchResponseAppliedRecord>,
}

/// Convert each section of the request body into [`BatchOp`]s, dispatch
/// to [`SyncedRepo::apply_batch`], then map the outcome → either a
/// success [`WriteOutcome`] or a [`WriteError::Conflict`] carrying the
/// per-record applied/failed lists.
async fn apply_writes(
    cm: &CloudMapState,
    body: unfurl_types::PostCloudmapRequest,
    atomic: bool,
) -> Result<WriteOutcome, WriteError> {
    let synced = cm.inner.as_ref();

    // A request naming a `cloudmap_path` writes to that file; without one, git-sync
    // resolves the file per record (see `build_batch_op`). Read it before the
    // `collect!` macro below moves the section fields out of `body`.
    let file_path = body
        .cloudmap_path
        .as_deref()
        .map(str::trim)
        .filter(|p| !p.is_empty())
        .map(str::to_string);
    let file_path = file_path.as_deref();

    // Build (BatchOp, section_name) pairs in a fixed section order so
    // the batch is deterministic across requests.
    let mut ops: Vec<BatchOp> = Vec::new();
    // Parallel vector mapping each op's index → its section name. The
    // batch outcome reports `path` (JSON-pointer) and `key`; we look
    // up the section here for the response, since "/repositories" →
    // "repositories" is well-defined but spelled twice in the body
    // would be confusing if derived in two places.
    let mut sections: Vec<&'static str> = Vec::new();

    macro_rules! collect {
        ($field:expr, $section:literal, $path:literal) => {
            if let Some(records) = $field {
                for (key, record) in records {
                    let value = serde_json::to_value(&record).map_err(|e| {
                        WriteError::Internal(format!(
                            "section {:?} key {:?}: serialize record: {e}",
                            $section, key
                        ))
                    })?;
                    let op = build_batch_op($section, $path, key, value, file_path)?;
                    ops.push(op);
                    sections.push($section);
                }
            }
        };
    }

    collect!(body.repositories, "repositories", "/repositories");
    collect!(body.artifacts, "artifacts", "/artifacts");
    collect!(body.components, "components", "/components");
    collect!(body.services, "services", "/services");
    collect!(body.instantiations, "instantiations", "/instantiations");
    collect!(body.types, "types", "/types");

    if ops.is_empty() {
        return Ok(WriteOutcome {
            last_version: None,
            applied: Vec::new(),
        });
    }

    let outcome = synced
        .apply_batch(ops, atomic)
        .await
        .map_err(|e| WriteError::Internal(format!("apply_batch: {e}")))?;

    let applied: Vec<unfurl_types::PatchResponseAppliedRecord> = outcome
        .applied
        .iter()
        .map(|a| {
            let section = sections.get(a.index).copied().unwrap_or("");
            unfurl_types::PatchResponseAppliedRecord {
                section: section.to_string(),
                key: a.key.clone(),
                version: a.outcome.version,
            }
        })
        .collect();

    if !outcome.failed.is_empty() {
        // Take the first failure as the canonical conflict identity
        // (matches the existing CloudMapProxyConflict's section/key
        // contract) but include the full `failed` list for callers
        // that want to handle non-atomic mode.
        let primary = &outcome.failed[0];
        let primary_section = sections
            .get(primary.index)
            .copied()
            .unwrap_or("")
            .to_string();
        let mut failed_records: Vec<FailedJson> = Vec::with_capacity(outcome.failed.len());
        let mut primary_actual: Option<String> = None;
        for f in &outcome.failed {
            let section = sections.get(f.index).copied().unwrap_or("").to_string();
            let (kind, actual) = classify_failure(&f.error);
            if std::ptr::eq(f, primary) {
                primary_actual.clone_from(&actual);
            }
            failed_records.push(FailedJson {
                section,
                key: f.key.clone(),
                actual,
                error: kind,
            });
        }
        return Err(WriteError::Conflict {
            section: primary_section,
            key: primary.key.clone(),
            actual: primary_actual,
            applied,
            failed: failed_records,
        });
    }

    Ok(WriteOutcome {
        last_version: outcome.last_version,
        applied,
    })
}

/// Convert a single ``(section, path, key, value)`` triple into a
/// [`BatchOp`]. The OCC marker keys (``unfurl.server.{commit,version,
/// id}``) and the ``unfurl.server.deleted`` flag are popped here so
/// the JSON that goes into git-sync is the bare record payload.
fn build_batch_op(
    section: &str,
    path: &'static str,
    key: String,
    value: Value,
    file_path: Option<&str>,
) -> Result<BatchOp, WriteError> {
    // `None` leaves git-sync to resolve the file: the existing record's file, then
    // (for upserts) the worktree's `default_file_path`. That's what a request without
    // `cloudmap_path` should keep doing.
    let file_path = file_path.map(str::to_string);
    match value {
        Value::Object(mut map) => {
            let commit_ref = pop_commit_ref(&mut map);
            let is_delete = matches!(map.remove("unfurl.server.deleted"), Some(Value::Bool(true)));
            if is_delete {
                Ok(BatchOp::Delete {
                    file_path,
                    path: path.to_string(),
                    key,
                    expected: commit_ref,
                })
            } else {
                Ok(BatchOp::Upsert {
                    file_path,
                    path: path.to_string(),
                    key,
                    json: Value::Object(map),
                    expected: commit_ref,
                })
            }
        }
        other => Err(WriteError::BadRequest(format!(
            "section {section:?} key {key:?}: value must be a JSON object, got {other:?}"
        ))),
    }
}

fn classify_failure(err: &unfurl_git_sync::Error) -> (Option<String>, Option<String>) {
    use unfurl_git_sync::Error as E;
    match err {
        E::Conflict { actual, .. } => (Some("conflict".to_string()), actual.clone()),
        E::NotFound { .. } => (Some("not_found".to_string()), None),
        _ => (Some("error".to_string()), None),
    }
}

#[cfg(test)]
mod tests {
    use super::origin_matches;

    #[test]
    fn origin_matches_project_path_suffix() {
        let origin = "unfurl.cloud/onecommons/cloudmap.git";
        assert!(origin_matches(origin, "onecommons/cloudmap"));
        // `.git` optional on either side, leading/trailing slashes ignored.
        assert!(origin_matches(origin, "onecommons/cloudmap.git"));
        assert!(origin_matches(origin, "/onecommons/cloudmap/"));
        assert!(origin_matches(
            "unfurl.cloud/onecommons/cloudmap",
            "onecommons/cloudmap"
        ));
    }

    #[test]
    fn origin_rejects_other_projects() {
        let origin = "unfurl.cloud/onecommons/cloudmap.git";
        assert!(!origin_matches(origin, "someone/else"));
        assert!(!origin_matches(origin, ""));
        // A suffix that isn't on a path boundary must not match.
        assert!(!origin_matches(origin, "commons/cloudmap"));
        // ... nor a prefix of the project id.
        assert!(!origin_matches(origin, "onecommons"));
    }

    #[test]
    fn origin_without_remote_matches_nothing() {
        // A worktree with no remote records its filesystem path, which is not
        // a project id. Serving such a checkout is what `--dev-mode` is for —
        // this function deliberately doesn't treat it as a wildcard, so a
        // strict deployment can't be talked into answering from it.
        assert!(!origin_matches("/tmp/some/checkout", "onecommons/cloudmap"));
        assert!(!origin_matches("/tmp/some/checkout", "anything/at-all"));
    }
}
