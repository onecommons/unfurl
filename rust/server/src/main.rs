// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! unfurl-server: Rust HTTP proxy for the unfurl Python backend.
//!
//! - Caches GET /export and GET /types via Redis
//! - Enqueues POST write operations to a Redis list
//! - Transparently proxies everything else to Python

mod cache;
mod client;
mod config;
mod proxy;
mod queue;
mod routes;

use axum::{
    routing::{get, post},
    Router,
};
use clap::Parser;
use config::Config;
use std::sync::Arc;
use tokio::net::TcpListener;
use tower_http::trace::TraceLayer;
use tracing_subscriber::EnvFilter;

/// Shared application state available to all handlers.
#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub client: reqwest::Client,
    pub redis: Option<redis::aio::MultiplexedConnection>,
}

#[tokio::main]
async fn main() {
    // Initialise tracing.  Write to stderr so that Python can redirect it to a
    // log file via subprocess.Popen(stderr=...) without mixing with app output.
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    let config = Config::parse();
    tracing::info!(
        "unfurl-server starting on {}:{} -> backend {}",
        config.host,
        config.port,
        config.backend_url()
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

    // Spawn queue worker with its own *separate* connection so that BLPOP 0
    // does not block the shared cache connection.
    if redis_conn.is_some() {
        if let Some(ref client) = redis_client_opt {
            match client.get_multiplexed_async_connection().await {
                Ok(worker_conn) => {
                    let queue_key = config.queue_key();
                    let backend = config.backend_url();
                    let http_client = reqwest::Client::new();
                    tokio::spawn(async move {
                        queue::run_worker(worker_conn, queue_key, backend, http_client).await;
                    });
                    tracing::info!("queue worker started");
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

    let state = AppState {
        config: Arc::new(config.clone()),
        client: http_client,
        redis: redis_conn,
    };

    // Build router.
    let app = Router::new()
        // Cache-aware read endpoints.
        .route("/export", get(routes::handle_export))
        .route("/types", get(routes::handle_types))
        // Write endpoints queued to Redis.
        .route("/create_ensemble", post(routes::handle_write))
        .route("/update_ensemble", post(routes::handle_write))
        .route("/create_provider", post(routes::handle_write))
        .route("/delete_deployment", post(routes::handle_write))
        .route("/update_environment", post(routes::handle_write))
        .route("/delete_environment", post(routes::handle_write))
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
