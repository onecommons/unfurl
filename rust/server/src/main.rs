// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! unfurl-server: Rust HTTP proxy for the unfurl Python backend.
//!
//! - Caches GET /export and GET /types via Redis
//! - Enqueues POST write operations to a Redis list
//! - Transparently proxies everything else to Python

use clap::Parser;
use std::io::IsTerminal;
use std::sync::Arc;
use tokio::net::TcpListener;
use tracing_subscriber::EnvFilter;
use unfurl_server::config::{Config, LogStyle, ScanAbortLevel};
use unfurl_server::gitlock::GitLocks;
use unfurl_server::grants::GrantStore;
use unfurl_server::{cloudmap, queue, AppState};

/// Points out the options that a skipped scan makes inert, and the
/// database it leaves with nothing in it.
///
/// Each of these is a combination that looks configured but does nothing,
/// which is worth a line at startup rather than a puzzled operator later.
fn warn_about_skipped_scan(config: &Config, db_url: &str) {
    tracing::info!("cloudmap_skip_scan set; serving the index as it stands");
    if config.cloudmap_force {
        tracing::warn!("cloudmap_force does nothing without a scan to resolve against");
    }
    if config.scan_abort_level != ScanAbortLevel::Report {
        tracing::warn!(
            abort_level = %config.scan_abort_level,
            "scan_abort_level does nothing without a scan to abort"
        );
    }
    // An in-memory index starts empty and the scan was the only thing that
    // would have filled it, so every /cloudmap read would answer nothing.
    if db_url.contains(":memory:") {
        tracing::warn!(
            db = db_url,
            "cloudmap_skip_scan with an in-memory index leaves it empty"
        );
    }
}

/// Logs what the startup scan found, and returns whether the operator
/// asked, through `level`, for a clean index rather than a reported one.
///
/// The per-file and per-record detail is already logged by the scan; this
/// is the one aggregate line, and the only place the counts are compared
/// against [`ScanAbortLevel`].
fn report_scan(scan: &unfurl_git_sync::SyncOutcome, level: ScanAbortLevel) -> bool {
    if let Some(e) = &scan.recovered {
        tracing::warn!(
            branch = e.branch.as_str(),
            commit = e.commit.as_str(),
            records = e.records.len(),
            "the repository lacks commits the database made: their records are saved to a branch, to merge"
        );
    }
    let fatal: usize = scan.invalid.iter().map(|f| f.validation.fatal.len()).sum();
    let refused: usize = scan.invalid.iter().map(|f| f.validation.errors.len()).sum();
    tracing::info!(
        files_seen = scan.files_seen,
        records_upserted = scan.records_upserted,
        records_deleted = scan.records_deleted,
        unparsed = scan.unparsed.len(),
        invalid_files = scan.invalid.len(),
        skipped_files = fatal,
        refused = refused,
        "cloudmap startup scan complete"
    );
    let aborts = level.aborts(fatal, refused);
    if aborts {
        tracing::error!(
            abort_level = %level,
            skipped_files = fatal,
            refused = refused,
            "cloudmap does not conform to its schema; refusing to serve it"
        );
    }
    aborts
}

/// Initialises tracing.  If UNFURL_LOGFILE is set, write directly to that
/// file (line-buffered) so readers can see output immediately.  Otherwise
/// write to stderr so Python can redirect it via subprocess.Popen(stderr=…).
fn init_tracing(config: &Config) {
    let env_filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let log_file = std::env::var("UNFURL_LOGFILE").ok();
    let style = LogStyle::resolve(
        config.log_style,
        log_file.is_some(),
        std::io::stderr().is_terminal(),
    );
    if let Some(log_path) = &log_file {
        let file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(log_path)
            .unwrap_or_else(|e| panic!("cannot open UNFURL_LOGFILE {log_path:?}: {e}"));
        let writer = std::sync::Mutex::new(std::io::LineWriter::new(file));
        match style {
            LogStyle::Json => tracing_subscriber::fmt()
                .json()
                .with_writer(writer)
                .with_env_filter(env_filter)
                .init(),
            LogStyle::Text => tracing_subscriber::fmt()
                .with_writer(writer)
                .with_env_filter(env_filter)
                .with_ansi(false)
                .init(),
        }
    } else {
        match style {
            LogStyle::Json => tracing_subscriber::fmt()
                .json()
                .with_writer(std::io::stderr)
                .with_env_filter(env_filter)
                .init(),
            LogStyle::Text => tracing_subscriber::fmt()
                .with_writer(std::io::stderr)
                .with_env_filter(env_filter)
                .init(),
        }
    }
}

/// Connects to Redis for cache lookups, if configured, and spawns the batch
/// worker on a connection of its own, restoring credentials from `grants`.
///
/// The queue worker MUST use a *separate* connection: a clone() of
/// MultiplexedConnection shares the same underlying socket, and BLPOP 0
/// (infinite timeout) on that socket would block every subsequent GET/SET
/// command, causing all cache lookups to hang indefinitely.
async fn connect_redis(
    config: &Config,
    grants: Option<Arc<GrantStore>>,
) -> Option<redis::aio::MultiplexedConnection> {
    let redacted_url = config.redacted_redis_url();
    let redacted_url = redacted_url.as_deref().unwrap_or("");
    let Some(url) = config.effective_redis_url() else {
        tracing::info!("no Redis config set, caching disabled");
        return None;
    };
    let client = redis::Client::open(url.as_str()).unwrap_or_else(|e| {
        tracing::error!("invalid Redis URL {}: {}", redacted_url, e);
        std::process::exit(1);
    });
    tracing::info!("using Redis: {}", redacted_url);

    let conn = client
        .get_multiplexed_async_connection()
        .await
        .unwrap_or_else(|e| {
            tracing::error!("Redis connection failed: {}", e);
            std::process::exit(1);
        });
    tracing::info!("connected to Redis (cache)");

    let worker_conn = client
        .get_multiplexed_async_connection()
        .await
        .unwrap_or_else(|e| {
            tracing::error!("Redis worker connection failed: {}", e);
            std::process::exit(1);
        });
    let worker_config = config.clone();
    let backend = config.backend_url();
    let http_client = reqwest::Client::new();
    tokio::spawn(async move {
        queue::run_worker(worker_conn, worker_config, backend, http_client, grants).await;
    });
    tracing::debug!("batch worker started");
    Some(conn)
}

/// Builds the HTTP client used for proxying.  Apply a request timeout when
/// configured so the server doesn't block indefinitely waiting for a slow
/// Python backend.
fn build_http_client(config: &Config) -> reqwest::Client {
    let mut builder = reqwest::Client::builder();
    if config.proxy_timeout_secs > 0 {
        builder = builder.timeout(std::time::Duration::from_secs(config.proxy_timeout_secs));
    }
    builder.build().expect("failed to build HTTP client")
}

/// Optional cloudmap fast-path: when both cloudmap_repo and
/// cloudmap_db_url are set, open a SyncedRepo and serve GET /cloudmap
/// locally; otherwise it falls through to the proxy.
async fn open_cloudmap(
    config: &Config,
    locks: Option<GitLocks>,
) -> Option<cloudmap::CloudMapState> {
    let (repo, db_url) = match (
        config.cloudmap_repo.as_deref(),
        config.cloudmap_db_url.as_deref(),
    ) {
        (Some(repo), Some(db_url)) => (repo, db_url),
        // Exactly one of the pair given: almost certainly a typo or a
        // half-finished deployment, so say which is missing rather than
        // claiming neither was set.
        (Some(_), None) => {
            tracing::warn!(
                "cloudmap_repo is set but cloudmap_db_url is not; \
                 GET /cloudmap will be proxied"
            );
            return None;
        }
        (None, Some(db_url)) if config.clone_root.is_some() => {
            return open_clones(config, db_url, locks).await;
        }
        (None, Some(_)) => {
            tracing::warn!(
                "cloudmap_db_url is set but cloudmap_repo is not; \
                 GET /cloudmap will be proxied"
            );
            return None;
        }
        (None, None) => return None,
    };
    tracing::info!("opening cloudmap repo at {} (db={})", repo, db_url);
    let scan = scan_options(config, db_url, config.cloudmap_recover);
    let accept = |o: &unfurl_git_sync::SyncOutcome| !report_scan(o, config.scan_abort_level);
    match cloudmap::CloudMapState::open_locked(repo, db_url, scan, accept, locks.as_ref()).await {
        Ok(Some(cm)) => Some(cm),
        Ok(None) => std::process::exit(1),
        Err(e) => {
            tracing::error!(
                error = format!(
                    "{e}{}",
                    open_hint(&*e, repo, config, config.cloudmap_recover)
                )
                .as_str(),
                "failed to open cloudmap repo"
            );
            std::process::exit(1);
        }
    }
}

/// Clone mode: a checkout of each worktree the database at `db_url`
/// tracks, under `clone_root`, each scanned and served. A worktree whose
/// checkout can't be made or opened is logged and left to python. Records
/// a missing commit made are saved to a branch: a new clone is where
/// that happens.
async fn open_clones(
    config: &Config,
    db_url: &str,
    locks: Option<GitLocks>,
) -> Option<cloudmap::CloudMapState> {
    let root = std::path::Path::new(config.clone_root.as_deref()?);
    let db = or_exit(
        cloudmap::db_config(db_url),
        "failed to open the cloudmap database",
    );
    tracing::info!(
        "cloning the cloudmap's worktrees under {} (db={})",
        root.display(),
        db_url
    );
    let scan = scan_options(config, db_url, true);
    let checkouts =
        unfurl_server::clone::prepare(&db, root, &config.cloud_server, locks.as_ref()).await;
    let served = unfurl_server::clone::serve(
        checkouts,
        db_url,
        scan,
        |outcome| !report_scan(outcome, config.scan_abort_level),
        |e, path| open_hint(e, path, config, true),
        locks,
    )
    .await;
    or_exit(served, "failed to serve the cloudmap checkouts")
}

/// `result`'s value, or, logging `what` failed, the process's end.
fn or_exit<T, E: std::fmt::Display>(result: Result<T, E>, what: &str) -> T {
    result.unwrap_or_else(|e| {
        tracing::error!(error = e.to_string().as_str(), "{what}");
        std::process::exit(1);
    })
}

/// The startup scan's options, `None` to skip it.
fn scan_options(
    config: &Config,
    db_url: &str,
    recover: bool,
) -> Option<unfurl_git_sync::ScanOptions> {
    if config.cloudmap_skip_scan {
        warn_about_skipped_scan(config, db_url);
        return None;
    }
    Some(unfurl_git_sync::ScanOptions {
        force: config.cloudmap_force,
        rebuild_missing: config.cloudmap_force,
        recover_missing: recover,
    })
}

/// What to do about `e`, failing to open the checkout at `repo`, with
/// recovery on or not.
fn open_hint(
    e: &(dyn std::error::Error + 'static),
    repo: &str,
    config: &Config,
    recovering: bool,
) -> String {
    let Some(unfurl_git_sync::Error::CommitMissing { since, .. }) =
        e.downcast_ref::<unfurl_git_sync::Error>()
    else {
        return String::new();
    };
    let list = since.map(|v| {
        format!(
            "; started with --cloudmap-skip-scan, {} includes them",
            written_since_read(repo, &config.cloud_server, v)
        )
    });
    let recover = match recovering {
        // it ran, and found no commit to save them at
        true => "",
        false => " with --cloudmap-recover so they are saved to a branch, or",
    };
    format!(
        "{}; start{recover} with --cloudmap-force to rebuild from HEAD without them",
        list.unwrap_or_default()
    )
}

/// The `GET /cloudmap` that reads what the checkout at `repo` wrote after
/// version `since`: named by its project on `cloud_server` and its branch,
/// where the checkout says what those are.
fn written_since_read(repo: &str, cloud_server: &str, since: i64) -> String {
    let mut query = Vec::new();
    let meta = unfurl_git_sync::git::open_repo(std::path::Path::new(repo))
        .and_then(|r| unfurl_git_sync::git::worktree_meta(&r));
    if let Ok(meta) = meta {
        let host = unfurl_git_sync::git::normalize_git_url_hard(cloud_server);
        if let Some(project) = meta.origin.strip_prefix(&format!("{host}/")) {
            query.push(format!("auth_project={project}"));
        }
        query.push(format!("branch={}", meta.branch));
    }
    query.push(format!("since_version={since}"));
    format!("GET /cloudmap?{}", query.join("&"))
}

/// Resolves `host:port` to every address the OS hands back via getaddrinfo
/// and serves `app` on each one that binds.
///
/// This is what makes `UNFURL_HOST=localhost` listen on both 127.0.0.1 *and*
/// [::1]: `TcpListener::bind(hostname)` otherwise tries each address in order
/// and stops after the first success — on macOS that's typically the IPv6
/// entry, leaving IPv4 clients unreachable.
async fn serve(app: axum::Router, config: &Config) {
    let addr = format!("{}:{}", config.host, config.port);
    let resolved: Vec<std::net::SocketAddr> = match tokio::net::lookup_host(&addr).await {
        Ok(iter) => iter.collect(),
        Err(e) => panic!("failed to resolve {addr}: {e}"),
    };
    if resolved.is_empty() {
        panic!("no addresses resolved for {addr}");
    }

    let mut tasks = Vec::with_capacity(resolved.len());
    for sock_addr in resolved {
        let listener = match TcpListener::bind(sock_addr).await {
            Ok(l) => l,
            Err(e) => {
                // Tolerate a single-family failure (e.g. another process
                // already owns the v4 socket) as long as some other
                // address binds successfully.
                tracing::warn!("failed to bind {}: {}", sock_addr, e);
                continue;
            }
        };
        tracing::info!("listening on {}", sock_addr);
        let app_clone = app.clone();
        tasks.push(tokio::spawn(async move {
            axum::serve(listener, app_clone)
                .with_graceful_shutdown(shutdown_signal())
                .await
                .expect("server error");
        }));
    }
    if tasks.is_empty() {
        panic!("failed to bind any resolved address for {addr}");
    }
    for task in tasks {
        task.await.expect("server task panicked");
    }
}

/// The store queued writes keep credentials in, with its expired grants
/// deleted hourly; or, with a loud warning, none.
async fn open_grants(config: &Config) -> Option<Arc<GrantStore>> {
    let store = match GrantStore::from_config(config).await {
        Ok(store) => Arc::new(store),
        Err(reason) => {
            tracing::warn!(
                reason = reason.as_str(),
                "NO GRANT STORE: queued writes will store users' git credentials in Redis"
            );
            return None;
        }
    };
    tracing::info!("grant store opened");
    let sweeper = store.clone();
    tokio::spawn(async move {
        let mut hourly = tokio::time::interval(std::time::Duration::from_secs(3600));
        loop {
            hourly.tick().await;
            if let Err(e) = sweeper.sweep().await {
                tracing::warn!(error = %e, "failed to delete expired grants");
            }
        }
    });
    Some(store)
}

#[tokio::main]
async fn main() {
    // Parsed before the subscriber exists so `--log-style` reaches it:
    // reading the env var alone left the flag inert. Clap reports its own
    // argument errors, so nothing is lost by logging slightly later.
    let config = Config::parse();
    init_tracing(&config);

    tracing::info!(
        "unfurl-server starting on {}:{} -> backend {} (RUST_LOG={:?}, cache_prefix={:?})",
        config.host,
        config.port,
        config.backend_url(),
        std::env::var("RUST_LOG").unwrap_or_default(),
        config.cache_key_prefix,
    );

    // without Redis nothing is queued, so nothing needs a grant
    let grants = match config.effective_redis_url() {
        Some(_) => open_grants(&config).await,
        None => None,
    };
    let redis = connect_redis(&config, grants.clone()).await;
    // python works in the same clones; without Redis, it shares none
    let locks = redis
        .clone()
        .map(|conn| GitLocks::new(conn, &config.cache_key_prefix));
    let state = AppState {
        config: Arc::new(config.clone()),
        redis,
        client: build_http_client(&config),
        cloudmap: open_cloudmap(&config, locks).await,
        grants,
    };

    let cors = match config.cors_layer() {
        Ok(layer) => {
            if layer.is_some() {
                tracing::info!(
                    "CORS enabled for origins: {}",
                    config.cors_origins.as_deref().unwrap_or("")
                );
            }
            layer
        }
        Err(e) => {
            tracing::error!("{}", e);
            std::process::exit(1);
        }
    };

    serve(unfurl_server::build_router(state, cors), &config).await;
}

/// Resolves when the process receives SIGINT (Ctrl-C) or SIGTERM
async fn shutdown_signal() {
    use tokio::signal;
    let ctrl_c = async {
        signal::ctrl_c()
            .await
            .expect("failed to install SIGINT handler");
    };
    #[cfg(unix)]
    let terminate = async {
        signal::unix::signal(signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();
    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
}

#[cfg(test)]
mod tests {
    use super::written_since_read;

    /// A checkout of `origin`, or with no remote.
    fn checkout(origin: Option<&str>) -> tempfile::TempDir {
        let dir = tempfile::tempdir().expect("tempdir");
        unfurl_git_sync::git::init_with_files(
            dir.path(),
            &[("a.yaml".into(), b"a: 1\n".to_vec())],
            "init",
        )
        .expect("init");
        if let Some(origin) = origin {
            let status = std::process::Command::new("git")
                .args(["remote", "add", "origin", origin])
                .current_dir(dir.path())
                .status()
                .expect("git");
            assert!(status.success());
        }
        dir
    }

    #[test]
    fn the_read_names_the_checkouts_project_and_branch() {
        let dir = checkout(Some("https://unfurl.cloud/org/proj.git"));
        let read = written_since_read(dir.path().to_str().unwrap(), "https://unfurl.cloud", 7);
        assert!(
            read.starts_with("GET /cloudmap?auth_project=org/proj&branch="),
            "{read}"
        );
        assert!(read.ends_with("&since_version=7"), "{read}");
    }

    #[test]
    fn a_checkout_elsewhere_is_named_by_its_branch_alone() {
        let dir = checkout(None);
        let read = written_since_read(dir.path().to_str().unwrap(), "https://unfurl.cloud", 7);
        assert!(read.starts_with("GET /cloudmap?branch="), "{read}");
        assert!(!read.contains("auth_project"), "{read}");
    }
}
