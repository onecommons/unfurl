// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Library re-exports for integration tests.

pub mod cache;
pub mod cloudmap;
pub mod config;
pub mod generated;
pub mod patch;
pub mod proxy;
pub mod queue;
pub mod routes;

use std::sync::Arc;

/// Shared application state available to all handlers.
#[derive(Clone)]
pub struct AppState {
    pub config: Arc<config::Config>,
    pub client: reqwest::Client,
    pub redis: Option<redis::aio::MultiplexedConnection>,
    pub cloudmap: Option<cloudmap::CloudMapState>,
}
