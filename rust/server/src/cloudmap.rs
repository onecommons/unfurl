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
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::Arc;
use tokio::sync::Mutex;
use unfurl_git_sync::{
    canonical_facet_key, canonical_json_text, BatchOp, CommitRef, DbConfig, FacetPath, FacetSpec,
    FormatRegistry, JsonQuery, Record, RecordQuery, SyncedRepo,
};

use crate::proxy;
use axum::extract::FromRequest;

use crate::routes::{ValidatedJson, ValidatedQuery, ValidatedRepeatedQuery};
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

/// Mint the paging cursor naming `(path, key)` as the last record of a page.
///
/// Just `<section>/<key>`. Section names are a fixed set and contain no
/// "/", so the first one separates the two halves however many the key
/// itself has; nothing needs escaping because the whole token is
/// URL-encoded as a query parameter anyway. The python handler's
/// `_encode_page_token` produces the identical string, so a client may
/// page across the two implementations — which it does whenever this
/// handler proxies a request to python.
pub fn encode_page_token(path: &str, key: &str) -> String {
    format!("{}/{}", path.trim_start_matches('/'), key)
}

/// Inverse of [`encode_page_token`], returning `(path, key)`.
///
/// Checking the section matters: an unrecognised one would otherwise
/// silently resume from the wrong place in the ordering rather than
/// report the bad cursor. Rejects only malformed tokens, never merely
/// stale ones — the cursor is a value rather than a row reference, so the
/// record it names may have been deleted since it was issued.
fn decode_page_token(token: &str) -> Result<(String, String), LocalError> {
    let bad = || {
        LocalError::BadRequest(format!(
            "page_token {token:?} is not a valid cursor: expected <section>/<key>"
        ))
    };
    let (section, key) = token.split_once('/').ok_or_else(bad)?;
    if key.is_empty() {
        return Err(bad());
    }
    let path = path_for_kind(section).ok_or_else(bad)?;
    Ok((path.to_string(), key.to_string()))
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
    /// The adjacency flattened into `(declared, ancestor)` pairs for
    /// the facets rollup, self-pairs included — see
    /// [`rollup_pairs_from`]. Computed once per rebuild.
    rollup_pairs: Vec<(String, String)>,
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

    /// The repository this state reads from.
    ///
    /// Exposed so callers that already hold a [`CloudMapState`] can drive
    /// the store directly rather than opening a second handle to the same
    /// database.
    pub fn synced(&self) -> &SyncedRepo {
        &self.inner
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

    /// Run `read` against the up-to-date types cache for `file_path`.
    ///
    /// Each call re-probes the `/types` section's `(COUNT(*),
    /// MAX(version))` stat ([`SyncedRepo::section_stat`]) and rebuilds
    /// only when the pair moved, so writes to other sections leave the
    /// cache warm. Shared by the `type` query filter
    /// ([`Self::subtype_names`]) and the facets rollup
    /// ([`Self::rollup_pairs`]), which therefore can't drift.
    async fn with_types_cache<T>(
        &self,
        file_path: Option<&str>,
        read: impl FnOnce(&TypesCache) -> T,
    ) -> Result<T, unfurl_git_sync::Error> {
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
                .find_records(&RecordQuery {
                    file_path: cache_key.clone(),
                    path: Some("/types".into()),
                    ..Default::default()
                })
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
            let rollup_pairs = rollup_pairs_from(&children);
            guard.insert(
                cache_key.clone(),
                TypesCache {
                    stat,
                    children,
                    rollup_pairs,
                },
            );
        }
        let cache = guard.get(&cache_key).expect("types cache just populated");
        Ok(read(cache))
    }

    /// Expand `type_name` to itself plus every subtype — every type
    /// record whose `extends` list (transitively) contains it.
    /// `type_name` need not have a type record — the result then is
    /// just the name itself.
    async fn subtype_names(
        &self,
        type_name: &str,
        file_path: Option<&str>,
    ) -> Result<Vec<String>, unfurl_git_sync::Error> {
        self.with_types_cache(file_path, |cache| {
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
            out
        })
        .await
    }

    /// The `(declared, ancestor)` rollup pairs for the facets
    /// endpoint's `subtypes` mode, from the same cached adjacency the
    /// `type` filter expands through — which is what keeps the
    /// invariant that a `type` facet bucket for `T` counts exactly the
    /// records `?type=T` matches.
    async fn rollup_pairs(
        &self,
        file_path: Option<&str>,
    ) -> Result<Vec<(String, String)>, unfurl_git_sync::Error> {
        self.with_types_cache(file_path, |cache| cache.rollup_pairs.clone())
            .await
    }
}

/// Flatten a reverse-`extends` adjacency into the `(member, bucket)`
/// pairs [`unfurl_git_sync::FacetSpec::rollup_pairs`] expects: for
/// every ancestor `P`, each transitive descendant `D` yields `(D, P)`,
/// plus the self-pair `(D, D)` for every `D` that gained an ancestor —
/// the SQL rollup join *replaces* a value with its buckets, so without
/// the self-pair a subtype would stop counting as itself. A type with
/// no pairs at all needs none: the join's COALESCE falls back to the
/// value itself.
fn rollup_pairs_from(children: &HashMap<String, Vec<String>>) -> Vec<(String, String)> {
    let mut pairs: BTreeSet<(String, String)> = BTreeSet::new();
    for parent in children.keys() {
        let mut seen: HashSet<&str> = HashSet::from([parent.as_str()]);
        let mut queue: Vec<&str> = vec![parent.as_str()];
        while let Some(name) = queue.pop() {
            if name != parent.as_str() {
                pairs.insert((name.to_string(), parent.clone()));
            }
            if let Some(kids) = children.get(name) {
                for kid in kids {
                    if seen.insert(kid.as_str()) {
                        queue.push(kid.as_str());
                    }
                }
            }
        }
    }
    let members: Vec<String> = pairs.iter().map(|(d, _)| d.clone()).collect();
    for member in members {
        pairs.insert((member.clone(), member));
    }
    pairs.into_iter().collect()
}

/// Axum handler for `GET /cloudmap`.
///
/// Local fast-path when [`AppState::cloudmap`] is set; otherwise
/// proxies to the Python backend.
pub async fn handle_cloudmap(
    State(state): State<AppState>,
    ValidatedRepeatedQuery(params): ValidatedRepeatedQuery<unfurl_types::GetCloudmapRequestQuery>,
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
    if let Err(answered) = serve_or_proxy(&state, &cm, params.auth_project.as_deref(), req).await {
        return answered;
    }

    match build_response(&cm, &params).await {
        Ok(body) => Json(body).into_response(),
        Err(err) => local_error_response(err),
    }
}

/// The project gate shared by the cloudmap read handlers: a request
/// naming a different project must not be answered from the one
/// repository this handler serves; hand it to python, which routes per
/// project. A request naming none keeps the previous behaviour (reads
/// resolve to the configured repo, as they always have). `Ok(())`
/// means serve locally; `Err` carries the already-built response
/// (proxied or errored).
async fn serve_or_proxy(
    state: &AppState,
    cm: &CloudMapState,
    auth_project: Option<&str>,
    req: Request<axum::body::Body>,
) -> Result<(), Response> {
    match cm
        .project_check(auth_project, state.config.dev_mode())
        .await
    {
        Ok(ProjectCheck::Serve) | Ok(ProjectCheck::Missing) => Ok(()),
        Ok(ProjectCheck::OtherProject) => {
            tracing::debug!(
                "auth_project {:?} is not the configured cloudmap repo; proxying",
                auth_project
            );
            Err(proxy::forward(
                &state.client,
                &state.config.backend_url(),
                req,
                state.config.max_body_bytes,
            )
            .await)
        }
        Err(e) => {
            tracing::error!("cloudmap worktree lookup failed: {}", e);
            Err((
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": format!("worktree: {e}")})),
            )
                .into_response())
        }
    }
}

/// Axum handler for `GET /cloudmap/facets`.
///
/// Local fast-path when [`AppState::cloudmap`] is set; otherwise
/// proxies to the Python backend. Extracted through
/// [`ValidatedRepeatedQuery`] because `facet=` repeats.
pub async fn handle_cloudmap_facets(
    State(state): State<AppState>,
    ValidatedRepeatedQuery(params): ValidatedRepeatedQuery<
        unfurl_types::GetCloudmapFacetsRequestQuery,
    >,
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
    if let Err(answered) = serve_or_proxy(&state, &cm, params.auth_project.as_deref(), req).await {
        return answered;
    }

    match build_facets_response(&cm, &params).await {
        Ok(body) => Json(body).into_response(),
        Err(err) => local_error_response(err),
    }
}

/// Map a [`LocalError`] to the HTTP response both cloudmap handlers
/// answer with — one mapping so the two endpoints can't drift.
fn local_error_response(err: LocalError) -> Response {
    match err {
        LocalError::NotFound(msg) => {
            (StatusCode::NOT_FOUND, Json(json!({"error": msg}))).into_response()
        }
        LocalError::BadRequest(msg) => {
            (StatusCode::BAD_REQUEST, Json(json!({"error": msg}))).into_response()
        }
        LocalError::Unprocessable(msg) => (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"error": msg})),
        )
            .into_response(),
        LocalError::Internal(msg) => {
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
    /// A malformed request parameter — 400, the same status the python
    /// handler answers for an unparseable `query`.
    BadRequest(String),
    /// A parameter that violates its declared schema constraint — 422,
    /// matching what APIFlask returns on the python side. The generated
    /// query struct carries no range validators, so bounds like
    /// `limit >= 1` are checked here instead.
    Unprocessable(String),
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
///
/// The response is an object: the document under `result`, plus
/// `followed` and `next_page_token` only when the request asked for what
/// they carry.
/// The record selection shared by `GET /cloudmap` and `GET
/// /cloudmap/facets`, resolved once from the request's query
/// parameters: `cloudmap_path` scoping, `kind` mapped to its record
/// path (404 on an unknown one), `type` expanded through the subtype
/// closure, and `filter` parsed into a [`JsonQuery`]. One resolver for
/// both endpoints, so a change to the selection parameters — say, a
/// repeatable `filter` — lands in both at once.
struct Selection {
    file_path: Option<String>,
    path: Option<&'static str>,
    type_names: Option<Vec<String>>,
    json_filters: Vec<JsonQuery>,
}

impl Selection {
    async fn resolve(
        cm: &CloudMapState,
        kind: Option<&str>,
        type_param: Option<&str>,
        filters: &[String],
        cloudmap_path: Option<&str>,
    ) -> Result<Self, LocalError> {
        // Scope the read to one cloudmap file when the request names one;
        // `None` matches records from every file in the worktree, which is
        // what a request without `cloudmap_path` has always done.
        let file_path = cloudmap_path.map(str::trim).filter(|p| !p.is_empty());

        // Resolve `kind` to its JSON-pointer path up-front so an unknown
        // kind 404s before we issue any DB query.
        let path: Option<&'static str> = match kind {
            Some(k) => Some(
                path_for_kind(k)
                    .ok_or_else(|| LocalError::NotFound(format!("section {k:?} not found")))?,
            ),
            None => None,
        };

        // Expand the optional `type` filter to the requested name plus
        // all of its (transitive) subtypes per the `/types` section, then
        // push the name set down into the SQL record match.
        let type_names: Option<Vec<String>> = match type_param {
            Some(t) if !t.is_empty() => Some(
                cm.subtype_names(t, file_path)
                    .await
                    .map_err(|e| LocalError::Internal(format!("subtype_names: {e}")))?,
            ),
            _ => None,
        };

        // `filter` narrows on record contents: `<json pointer>=<value>`,
        // pushed into the SQL WHERE clause so the database does the
        // filtering. Repeatable — every occurrence must match (the
        // clauses AND together); blank occurrences are dropped.
        let json_filters: Vec<JsonQuery> = filters
            .iter()
            .filter(|f| !f.trim().is_empty())
            .map(|f| parse_json_filter(f))
            .collect::<Result<_, _>>()?;

        Ok(Self {
            file_path: file_path.map(str::to_string),
            path,
            type_names,
            json_filters,
        })
    }

    /// The base [`RecordQuery`] for this selection; callers add their
    /// own key / paging / versioning fields.
    fn into_query(self) -> RecordQuery {
        RecordQuery {
            file_path: self.file_path,
            path: self.path.map(str::to_string),
            type_names: self.type_names,
            json_queries: self.json_filters,
            ..Default::default()
        }
    }
}

async fn build_response(
    cm: &CloudMapState,
    params: &unfurl_types::GetCloudmapRequestQuery,
) -> Result<Value, LocalError> {
    let synced = cm.inner.as_ref();
    let kind = params.kind.as_deref();
    let key = params.key.as_deref();
    let selection = Selection::resolve(
        cm,
        kind,
        params.r#type.as_deref(),
        params.filter.as_deref().unwrap_or_default(),
        params.cloudmap_path.as_deref(),
    )
    .await?;

    // A walk starts from every record the query selected, so it needs no
    // key: `find_records_follow` seeds its queue from the whole initial
    // set. With a key that set is one record; with `kind` / `type` /
    // `filter` it is their matches, and the walk returns the
    // neighbourhood of all of them.
    let follow = params.follow.unwrap_or(0).max(0) as u32;
    // alias=true when filtering by key (so versioned URLs hit their
    // canonical record); alias=false for the full document.
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

    let type_filtered = selection.type_names.is_some();
    let content_filtered = !selection.json_filters.is_empty();

    // `select` reduces every returned record to the requested properties.
    // Parsed once here; empty / all-blank values mean "no projection".
    let select_paths: Option<Vec<SelectPath>> = params
        .select
        .as_deref()
        .map(parse_select)
        .filter(|p| !p.is_empty());

    // A client catching up from a watermark needs to learn what was
    // deleted; one reading a section live does not.
    let mut query = selection.into_query();
    query.since_version = params.since_version;
    query.include_deleted = params.since_version.is_some();

    if let Some(limit) = params.limit {
        if limit < 1 {
            return Err(LocalError::Unprocessable(format!(
                "limit must be at least 1, got {limit}"
            )));
        }
        if key.is_some() {
            return Err(LocalError::BadRequest(
                "limit cannot be combined with key".to_string(),
            ));
        }
        query.after = params
            .page_token
            .as_deref()
            .filter(|t| !t.is_empty())
            .map(decode_page_token)
            .transpose()?;
        // Fetch one extra record: its presence is what says another
        // page exists. Testing `len == limit` instead would end a
        // section whose size is a multiple of `limit` on a token
        // pointing at an empty page.
        query.limit = Some(limit + 1);
        return paged_response(
            synced,
            &query,
            follow,
            exclude_ids,
            select_paths.as_deref(),
            limit,
        )
        .await;
    }

    query.key = key.map(str::to_string);
    query.alias = alias;
    let (initial, followed_records) = synced
        .find_records_follow(&query, follow, exclude_ids)
        .await
        .map_err(|e| LocalError::Internal(format!("find_records_follow: {e}")))?;

    if let (Some(kind_str), Some(key_str)) = (kind, key) {
        if initial.is_empty() {
            let hint = match (type_filtered, content_filtered) {
                (true, true) => " with matching type matching the filter",
                (true, false) => " with matching type",
                (false, true) => " matching the filter",
                (false, false) => "",
            };
            return Err(LocalError::NotFound(format!(
                "key {key_str:?} not found in {kind_str:?}{hint}"
            )));
        }
    }

    let mut body = Map::new();
    body.insert(
        "result".to_string(),
        records_to_document(initial, select_paths.as_deref()),
    );
    // Omitted unless the request asked to walk, so a client can tell
    // "you didn't ask" from "there is none".
    if follow > 0 {
        body.insert(
            "followed".to_string(),
            records_to_document(followed_records, select_paths.as_deref()),
        );
    }
    Ok(Value::Object(body))
}

/// One page of a `limit` request: the ordered scan cut at `limit`.
async fn paged_response(
    synced: &SyncedRepo,
    query: &RecordQuery,
    follow: u32,
    exclude_ids: Vec<i64>,
    select: Option<&[SelectPath]>,
    limit: i64,
) -> Result<Value, LocalError> {
    let mut rows = synced
        .find_records(query)
        .await
        .map_err(|e| LocalError::Internal(format!("find_records: {e}")))?;

    let more = rows.len() as i64 > limit;
    // Cut the page *before* walking, or the probe record -- which belongs to
    // the next page -- would contribute its neighbours to this one.
    rows.truncate(limit as usize);
    let next = if more {
        rows.last().map(|r| encode_page_token(&r.path, &r.key))
    } else {
        None
    };

    let followed = if follow > 0 {
        synced
            .follow_records(&rows, follow, exclude_ids, query.since_version)
            .await
            .map_err(|e| LocalError::Internal(format!("follow_records: {e}")))?
    } else {
        Vec::new()
    };

    let mut body = Map::new();
    body.insert("result".to_string(), records_to_document(rows, select));
    // Each page carries its own neighbourhood, capped at `follow` -- the same
    // per-response bound `limit` puts on the page itself. A record reachable
    // from two pages is reported on both unless the caller excludes it.
    if follow > 0 {
        body.insert(
            "followed".to_string(),
            records_to_document(followed, select),
        );
    }
    // Absent on the last page — that is what ends a client's walk.
    if let Some(token) = next {
        body.insert("next_page_token".to_string(), Value::String(token));
    }
    Ok(Value::Object(body))
}

/// Parse a JSON Pointer (RFC 6901, leading "/" optional) into
/// unescaped reference tokens — the same rule as `_pointer_tokens` in
/// `unfurl/server/endpoints.py`, so `group_by` and `facet` paths mean
/// the same thing on both servers.
fn pointer_tokens(path: &str) -> Result<Vec<String>, LocalError> {
    let trimmed = path.trim();
    let body = trimmed.strip_prefix('/').unwrap_or(trimmed);
    let tokens: Vec<String> = body
        .split('/')
        .map(|t| t.replace("~1", "/").replace("~0", "~"))
        .collect();
    if tokens.iter().any(|t| t.is_empty()) {
        return Err(LocalError::BadRequest(format!(
            "path {path:?} needs non-empty segments"
        )));
    }
    Ok(tokens)
}

/// Render reference tokens back to normalized JSON Pointer text,
/// re-escaping per RFC 6901 — the inverse of [`pointer_tokens`].
fn pointer_text(tokens: &[String]) -> String {
    let escaped: Vec<String> = tokens
        .iter()
        .map(|t| t.replace('~', "~0").replace('/', "~1"))
        .collect();
    format!("/{}", escaped.join("/"))
}

/// Build the `{meta, total, groups}` body for `GET /cloudmap/facets`.
///
/// Record selection is [`Selection::resolve`], shared with `GET
/// /cloudmap`. The aggregation itself runs in the database
/// ([`SyncedRepo::facet_records`]); this function renders the raw rows
/// into response keys — [`canonical_facet_key`] for simple values, the
/// canonical JSON array of member values for composite cells — and
/// **merges** buckets whose keys collapse to the same canonical
/// spelling, summing their counts. The merge is what absorbs sqlite
/// grouping object values by their stored key order; the sum
/// over-counts only when one record spells the same object two ways
/// within one array, which we accept.
async fn build_facets_response(
    cm: &CloudMapState,
    params: &unfurl_types::GetCloudmapFacetsRequestQuery,
) -> Result<Value, LocalError> {
    let selection = Selection::resolve(
        cm,
        params.kind.as_deref(),
        params.r#type.as_deref(),
        params.filter.as_deref().unwrap_or_default(),
        params.cloudmap_path.as_deref(),
    )
    .await?;

    let group_tokens = pointer_tokens(&params.group_by)?;
    // Each `facet=` occurrence is one column; a comma within one
    // composes a tuple column from several member paths. Blank
    // members and blank occurrences are dropped, like `select` does.
    let mut column_tokens: Vec<Vec<Vec<String>>> = Vec::new();
    for raw in params.facet.as_deref().unwrap_or_default() {
        let members: Vec<Vec<String>> = raw
            .split(',')
            .map(str::trim)
            .filter(|p| !p.is_empty())
            .map(pointer_tokens)
            .collect::<Result<_, _>>()?;
        if !members.is_empty() {
            column_tokens.push(members);
        }
    }

    // The subtypes rollup applies to every column whose path is
    // exactly `type` — the group or any facet member — and is inert
    // elsewhere (it defaults on, so a request without a type column
    // must not error).
    let subtypes = params.subtypes.unwrap_or(true);
    let is_type_path = |tokens: &[String]| tokens.len() == 1 && tokens[0] == "type";
    let group_rollup = subtypes && is_type_path(&group_tokens);
    let member_rollup: Vec<Vec<bool>> = column_tokens
        .iter()
        .map(|members| {
            members
                .iter()
                .map(|tokens| subtypes && is_type_path(tokens))
                .collect()
        })
        .collect();
    let rollup_applied = group_rollup || member_rollup.iter().any(|flags| flags.contains(&true));
    let rollup_pairs = if rollup_applied {
        cm.rollup_pairs(selection.file_path.as_deref())
            .await
            .map_err(|e| LocalError::Internal(format!("rollup_pairs: {e}")))?
    } else {
        Vec::new()
    };

    let bad_path = |e: unfurl_git_sync::Error| LocalError::BadRequest(e.to_string());
    let spec = FacetSpec {
        group: FacetPath::new(group_tokens.clone(), group_rollup).map_err(bad_path)?,
        columns: column_tokens
            .iter()
            .zip(&member_rollup)
            .map(|(members, flags)| {
                members
                    .iter()
                    .zip(flags)
                    .map(|(tokens, rollup)| FacetPath::new(tokens.clone(), *rollup))
                    .collect::<Result<Vec<_>, _>>()
            })
            .collect::<Result<Vec<_>, _>>()
            .map_err(bad_path)?,
        rollup_pairs,
    };
    let rows = cm
        .inner
        .facet_records(&selection.into_query(), &spec)
        .await
        .map_err(|e| LocalError::Internal(format!("facet_records: {e}")))?;

    // Canonicalize, merge, nest. BTreeMaps double as the sorted-key
    // emission the parity tests byte-compare.
    let column_count = column_tokens.len();
    let mut groups: BTreeMap<String, (i64, Vec<BTreeMap<String, i64>>)> = BTreeMap::new();
    for (value, count) in &rows.groups {
        let entry = groups
            .entry(canonical_facet_key(value))
            .or_insert_with(|| (0, vec![BTreeMap::new(); column_count]));
        entry.0 += count;
    }
    for (index, column) in rows.columns.iter().enumerate() {
        for row in column {
            let cell_key = if row.members.len() == 1 {
                canonical_facet_key(&row.members[0])
            } else {
                canonical_json_text(&Value::Array(row.members.clone()))
            };
            let entry = groups
                .entry(canonical_facet_key(&row.group))
                .or_insert_with(|| (0, vec![BTreeMap::new(); column_count]));
            *entry.1[index].entry(cell_key).or_insert(0) += row.count;
        }
    }

    let mut groups_map = Map::new();
    for (group_key, (count, columns)) in groups {
        let mut entry = Map::new();
        entry.insert("count".to_string(), json!(count));
        if column_count > 0 {
            entry.insert(
                "facets".to_string(),
                Value::Array(
                    columns
                        .into_iter()
                        .map(|cells| {
                            Value::Object(cells.into_iter().map(|(k, n)| (k, json!(n))).collect())
                        })
                        .collect(),
                ),
            );
        }
        groups_map.insert(group_key, Value::Object(entry));
    }
    Ok(json!({
        "meta": {
            "group_by": pointer_text(&group_tokens),
            "facets": column_tokens
                .iter()
                .map(|members| {
                    Value::Array(
                        members
                            .iter()
                            .map(|tokens| Value::String(pointer_text(tokens)))
                            .collect(),
                    )
                })
                .collect::<Vec<_>>(),
            "subtypes": rollup_applied,
        },
        "total": rows.total,
        "groups": Value::Object(groups_map),
    }))
}

/// Parse the `filter` param: `<json pointer>=<value>`.
///
/// Ports `_parse_json_filter` in `unfurl/server/endpoints.py`. The path is a
/// JSON Pointer (RFC 6901, leading "/" optional); the value is read as JSON --
/// `true`, `false`, `null` and numbers keep their type, anything else is a
/// string, and a double-quoted value is always a string.
fn parse_json_filter(expr: &str) -> Result<JsonQuery, LocalError> {
    // No "=" at all is an existence test: the path has to resolve, whatever
    // it holds.
    let (path, raw) = match expr.split_once('=') {
        Some((path, raw)) => (path, Some(raw)),
        None => (expr, None),
    };
    let path = path.trim();
    // `^=` is prefix matching; the "^" sits at the end of the path half once
    // the query has been split on "=".
    let (path, prefix) = match path.strip_suffix('^') {
        Some(head) => (head, true),
        None => (path, false),
    };
    let path = path.strip_prefix('/').unwrap_or(path);
    let tokens: Vec<String> = path
        .split('/')
        .map(|t| t.replace("~1", "/").replace("~0", "~"))
        .collect();
    let Some(raw) = raw else {
        return JsonQuery::exists(tokens)
            .map_err(|e| LocalError::BadRequest(format!("filter {expr:?}: {e}")));
    };
    let built = if prefix {
        // a prefix is textual, so the value is never parsed as JSON -- only
        // unquoted, so a prefix that looks like a number can be written either
        // way
        let raw = raw
            .strip_prefix('"')
            .and_then(|r| r.strip_suffix('"'))
            .unwrap_or(raw);
        JsonQuery::starts_with(tokens, raw.to_string())
    } else {
        JsonQuery::new(tokens, parse_json_literal(raw)?)
    };
    built.map_err(|e| LocalError::BadRequest(format!("filter {expr:?}: {e}")))
}

/// Read a `filter` value as JSON, defaulting to a string.
///
/// An array literal is parsed as JSON (it means an exact match); a malformed
/// one is an error rather than a string, which would silently never match. An
/// object literal is rejected -- [`JsonQuery::new`] says why. A value that
/// would otherwise be read as a number, a keyword or an array can be forced to
/// a string by quoting it.
fn parse_json_literal(raw: &str) -> Result<Value, LocalError> {
    if raw.len() > 1 && raw.starts_with('"') && raw.ends_with('"') {
        return Ok(Value::String(raw[1..raw.len() - 1].to_string()));
    }
    if raw.starts_with('{') {
        // rejected here rather than by `JsonQuery::new` so the message is the
        // same whether or not the object parses as JSON
        return Err(LocalError::BadRequest(
            "filter value can't be an object: address a member by putting its key \
             in the path"
                .to_string(),
        ));
    }
    if raw.starts_with('[') {
        return serde_json::from_str(raw).map_err(|e| {
            LocalError::BadRequest(format!("filter value {raw:?} is not valid JSON: {e}"))
        });
    }
    Ok(match raw {
        "true" => Value::Bool(true),
        "false" => Value::Bool(false),
        "null" => Value::Null,
        _ => raw
            .parse::<i64>()
            .map(Value::from)
            .or_else(|_| raw.parse::<f64>().map(Value::from))
            .unwrap_or_else(|_| Value::String(raw.to_string())),
    })
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
        let enriched = annotate_record(r.json, r.id, r.version, r.commit_id, r.deleted);
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
fn annotate_record(
    json: Value,
    id: i64,
    version: i64,
    commit_id: Option<String>,
    deleted: bool,
) -> Value {
    let Value::Object(mut map) = json else {
        return json;
    };
    map.insert("unfurl.server.id".to_string(), Value::from(id));
    map.insert("unfurl.server.version".to_string(), Value::from(version));
    map.insert(
        "unfurl.server.commit".to_string(),
        commit_id.map(Value::from).unwrap_or(Value::Null),
    );
    // Same key a client sends to *request* a delete, so the flag means
    // "this record is gone" in both directions. Only ever set on an
    // incremental read, which is the one that asked for tombstones.
    if deleted {
        map.insert("unfurl.server.deleted".to_string(), Value::Bool(true));
    }
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
