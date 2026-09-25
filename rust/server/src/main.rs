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

/// Logs what the startup scan found, and exits when the operator asked
/// for a clean index rather than a reported one.
///
/// The per-file and per-record detail is already logged by the scan; this
/// is the one aggregate line, and the only place the counts are compared
/// against [`ScanAbortLevel`].
fn report_scan(scan: &unfurl_git_sync::SyncOutcome, level: ScanAbortLevel) {
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
    if level.aborts(fatal, refused) {
        tracing::error!(
            abort_level = %level,
            skipped_files = fatal,
            refused = refused,
            "cloudmap does not conform to its schema; refusing to start"
        );
        std::process::exit(1);
    }
}

#[tokio::main]
async fn main() {
    // Initialise tracing.  If UNFURL_LOGFILE is set, write directly to that
    // file (line-buffered) so readers can see output immediately.  Otherwise
    // write to stderr so Python can redirect it via subprocess.Popen(stderr=…).
    // Parsed before the subscriber exists so `--log-style` reaches it:
    // reading the env var alone left the flag inert. Clap reports its own
    // argument errors, so nothing is lost by logging slightly later.
    let config = Config::parse();
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

    tracing::info!(
        "unfurl-server starting on {}:{} -> backend {} (RUST_LOG={:?}, cache_prefix={:?})",
        config.host,
        config.port,
        config.backend_url(),
        std::env::var("RUST_LOG").unwrap_or_default(),
        config.cache_key_prefix,
    );

    // Optional Redis connection for cache lookups.
    //
    // IMPORTANT: The queue worker MUST use a *separate* redis::Client / connection.
    // Using a clone() of MultiplexedConnection shares the same underlying socket,
    // and BLPOP 0 (infinite timeout) on that socket would block every subsequent
    // GET/SET command, causing all cache lookups to hang indefinitely.
    let redacted_url = config.redacted_redis_url();
    let redis_client_opt: Option<redis::Client> = match config.effective_redis_url() {
        Some(url) => match redis::Client::open(url.as_str()) {
            Ok(c) => {
                tracing::info!("using Redis: {}", redacted_url.as_deref().unwrap_or(""));
                Some(c)
            }
            Err(e) => {
                tracing::error!(
                    "invalid Redis URL {}: {}",
                    redacted_url.as_deref().unwrap_or(""),
                    e
                );
                std::process::exit(1);
            }
        },
        None => {
            tracing::info!("no Redis config set, caching disabled");
            None
        }
    };

    let redis_conn = match redis_client_opt.as_ref() {
        Some(client) => match client.get_multiplexed_async_connection().await {
            Ok(conn) => {
                tracing::info!("connected to Redis (cache)");
                Some(conn)
            }
            Err(e) => {
                tracing::error!("Redis connection failed: {}", e);
                std::process::exit(1);
            }
        },
        None => None,
    };

    // Spawn batch worker with its own *separate* connection so that its
    // polling loop does not block the shared cache connection.
    if redis_conn.is_some() {
        if let Some(ref client) = redis_client_opt {
            match client.get_multiplexed_async_connection().await {
                Ok(worker_conn) => {
                    let worker_config = config.clone();
                    let backend = config.backend_url();
                    let http_client = reqwest::Client::new();
                    tokio::spawn(async move {
                        queue::run_worker(worker_conn, worker_config, backend, http_client).await;
                    });
                    tracing::debug!("batch worker started");
                }
                Err(e) => {
                    tracing::error!("Redis worker connection failed: {}", e);
                    std::process::exit(1);
                }
            }
        }
    }

    // Build the HTTP client used for proxying.  Apply a request timeout when
    // configured so the server doesn't block indefinitely waiting for a slow
    // Python backend.
    let http_client = {
        let mut builder = reqwest::Client::builder();
        if config.proxy_timeout_secs > 0 {
            builder = builder.timeout(std::time::Duration::from_secs(config.proxy_timeout_secs));
        }
        builder.build().expect("failed to build HTTP client")
    };

    // Optional cloudmap fast-path: when both cloudmap_repo and
    // cloudmap_db_url are set, open a SyncedRepo and serve GET /cloudmap
    // locally; otherwise it falls through to the proxy.
    let cloudmap_state = match (
        config.cloudmap_repo.as_deref(),
        config.cloudmap_db_url.as_deref(),
    ) {
        (Some(repo), Some(db_url)) => {
            tracing::info!("opening cloudmap repo at {} (db={})", repo, db_url);
            let scan = if config.cloudmap_skip_scan {
                warn_about_skipped_scan(&config, db_url);
                None
            } else {
                Some(unfurl_git_sync::ScanOptions {
                    force: config.cloudmap_force,
                })
            };
            match cloudmap::CloudMapState::open(repo, db_url, scan).await {
                Ok((cm, outcome)) => {
                    if let Some(outcome) = outcome {
                        report_scan(&outcome, config.scan_abort_level);
                    }
                    Some(cm)
                }
                Err(e) => {
                    tracing::error!(
                        error = e.to_string().as_str(),
                        "failed to open cloudmap repo"
                    );
                    std::process::exit(1);
                }
            }
        }
        // Exactly one of the pair given: almost certainly a typo or a
        // half-finished deployment, so say which is missing rather than
        // claiming neither was set.
        (Some(_), None) => {
            tracing::warn!(
                "cloudmap_repo is set but cloudmap_db_url is not; \
                 GET /cloudmap will be proxied"
            );
            None
        }
        (None, Some(_)) => {
            tracing::warn!(
                "cloudmap_db_url is set but cloudmap_repo is not; \
                 GET /cloudmap will be proxied"
            );
            None
        }
        _ => None,
    };

    let state = AppState {
        config: Arc::new(config.clone()),
        client: http_client,
        redis: redis_conn,
        cloudmap: cloudmap_state,
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

    let app = unfurl_server::build_router(state, cors);

    // Resolve `host:port` to every address the OS hands back via
    // getaddrinfo and bind a listener on each one we can.  This is what
    // makes `UNFURL_HOST=localhost` listen on both 127.0.0.1 *and*
    // [::1]: `TcpListener::bind(hostname)` otherwise tries each address
    // in order and stops after the first success — on macOS that's
    // typically the IPv6 entry, leaving IPv4 clients unreachable.
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
