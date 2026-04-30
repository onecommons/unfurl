// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! unfurl-server: Rust HTTP proxy for the unfurl Python backend.
//!
//! - Caches GET /export and GET /types via Redis
//! - Enqueues POST write operations to a Redis list
//! - Transparently proxies everything else to Python

use axum::{
    routing::{get, post},
    Router,
};
use clap::Parser;
use std::sync::Arc;
use tokio::net::TcpListener;
use tower_http::trace::TraceLayer;
use tracing_subscriber::EnvFilter;
use unfurl_server::config::Config;
use unfurl_server::{cloudmap, queue, routes, AppState};

#[tokio::main]
async fn main() {
    // Initialise tracing.  If UNFURL_LOGFILE is set, write directly to that
    // file (line-buffered) so readers can see output immediately.  Otherwise
    // write to stderr so Python can redirect it via subprocess.Popen(stderr=…).
    let env_filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    if let Ok(log_path) = std::env::var("UNFURL_LOGFILE") {
        let file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .unwrap_or_else(|e| panic!("cannot open UNFURL_LOGFILE {log_path:?}: {e}"));
        tracing_subscriber::fmt()
            .with_writer(std::sync::Mutex::new(std::io::LineWriter::new(file)))
            .with_env_filter(env_filter)
            .with_ansi(false)
            .init();
    } else {
        tracing_subscriber::fmt()
            .with_writer(std::io::stderr)
            .with_env_filter(env_filter)
            .init();
    };

    let config = Config::parse();
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
            match cloudmap::CloudMapState::open(repo, db_url).await {
                Ok(cm) => Some(cm),
                Err(e) => {
                    tracing::error!("failed to open cloudmap repo: {}", e);
                    std::process::exit(1);
                }
            }
        }
        (Some(_), None) | (None, Some(_)) => {
            tracing::info!(
                "cloudmap_repo and cloudmap_db_url not set; \
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

    // Build router.
    // POST /cloudmap is registered as the typed local handler when a
    // cloudmap repo is configured, otherwise the proxy fallthrough.
    // Splitting at startup lets the local handler use a clean
    // `Json<unfurl_types::CloudMapDocument>` extractor without losing
    // the proxy path.
    let cloudmap_route = if state.cloudmap.is_some() {
        get(cloudmap::handle_cloudmap).post(cloudmap::post_cloudmap_local)
    } else {
        get(cloudmap::handle_cloudmap).post(cloudmap::post_cloudmap_proxy)
    };

    let app = Router::new()
        // Cache-aware read endpoints.
        .route("/export", get(routes::handle_export))
        .route("/types", get(routes::handle_types))
        .route("/cloudmap", cloudmap_route)
        // Write endpoints queued to Redis. Follow the openapi spec: Two typed wrappers around
        // `handle_write` validate the request body against its
        // OpenAPI request schema and declare `Json<PatchResponse>`
        // Three endpoints share `PatchEnsembleBody`,
        // the other three share `PatchEnvironmentBody`.
        .route("/create_ensemble", post(routes::handle_patch_ensemble))
        .route("/update_ensemble", post(routes::handle_patch_ensemble))
        .route("/create_provider", post(routes::handle_patch_ensemble))
        .route("/delete_deployment", post(routes::handle_patch_environment))
        .route(
            "/update_environment",
            post(routes::handle_patch_environment),
        )
        .route(
            "/delete_environment",
            post(routes::handle_patch_environment),
        )
        // Everything else proxied transparently.
        .fallback(routes::handle_fallback)
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    let addr = format!("{}:{}", config.host, config.port);
    let listener = TcpListener::bind(&addr)
        .await
        .expect("failed to bind address");
    tracing::info!("listening on {}", addr);
    axum::serve(listener, app).await.expect("server error");
}
