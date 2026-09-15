// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Library re-exports for integration tests.

pub mod cache;
pub mod cloudmap;
pub mod config;
pub mod patch;
pub mod proxy;
pub mod queue;
pub mod routes;
pub mod unfurl_types;

use axum::http::{header, HeaderValue};
use axum::{
    routing::{get, post},
    Router,
};
use std::sync::Arc;
use tower_http::cors::CorsLayer;
use tower_http::set_header::SetResponseHeaderLayer;
use tower_http::trace::TraceLayer;

/// Shared application state available to all handlers.
#[derive(Clone)]
pub struct AppState {
    pub config: Arc<config::Config>,
    pub client: reqwest::Client,
    pub redis: Option<redis::aio::MultiplexedConnection>,
    pub cloudmap: Option<cloudmap::CloudMapState>,
}

/// Value of the `Server` response header on responses this proxy produces
/// itself -- a cache hit, a queue 409/503, a local cloudmap read.
///
/// Set `if_not_present`, so a response proxied from python keeps python's
/// (`unfurl`, from waitress's `ident`). The header therefore says which
/// server produced the body; it does not say whether this proxy was in
/// front, which is what `Via` is for.
///
/// No version: RFC 9110 asks origin servers not to put needlessly
/// fine-grained detail here, and the crate version has been 0.1.0 since
/// the first commit, so it identifies nothing while still narrowing a
/// fingerprint.
const SERVER_IDENT: &str = "unfurl-server";

/// Build the application router.
///
/// `cors` is applied inside the trace layer so preflight requests, which
/// [`CorsLayer`] answers itself without reaching the router, are still
/// logged. It must be a `.layer` and not a `.route_layer`: the latter
/// skips the fallback and unmatched methods, which is exactly where the
/// preflight for a `get`-only route such as `/export` would land.
pub fn build_router(state: AppState, cors: Option<CorsLayer>) -> Router {
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
        // Facet counts over the same records; the handler proxies
        // itself when no cloudmap repo is configured.
        .route("/cloudmap/facets", get(cloudmap::handle_cloudmap_facets))
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
        .fallback(routes::handle_fallback);
    // `.layer()` added last is outermost, so trace wraps cors and logs the
    // preflights that cors answers without reaching the router.
    let app = match cors {
        Some(layer) => app.layer(layer),
        None => app,
    };
    app.layer(SetResponseHeaderLayer::if_not_present(
        header::SERVER,
        HeaderValue::from_static(SERVER_IDENT),
    ))
    .layer(TraceLayer::new_for_http())
    .with_state(state)
}
