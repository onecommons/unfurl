// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! CLI arguments and configuration from environment variables.

use clap::Parser;

/// Rust HTTP proxy server for unfurl.
///
/// Sits in front of the Python (waitress) backend, adding Redis cache
/// look-ups for GET /export and GET /types, and enqueuing POST write
/// operations to a Redis list.
#[derive(Parser, Debug, Clone)]
#[command(version, about)]
pub struct Config {
    /// Host address to bind to.
    #[arg(long, env = "UNFURL_HOST", default_value = "127.0.0.1")]
    pub host: String,

    /// Port to listen on.
    #[arg(long, env = "UNFURL_PORT", default_value_t = 8080)]
    pub port: u16,

    /// URL of the Python backend (waitress).
    #[arg(long, env = "UNFURL_BACKEND_URL")]
    pub backend_url: Option<String>,

    /// Redis connection URL. When absent, caching and queuing are disabled.
    #[arg(long, env = "CACHE_REDIS_URL")]
    pub redis_url: Option<String>,

    /// Key prefix used by flask-caching's RedisCache backend.
    #[arg(long, env = "CACHE_KEY_PREFIX", default_value = "ufsv::")]
    pub cache_key_prefix: String,

    /// Shared secret for authenticating internal requests.
    #[arg(long, env = "UNFURL_SECRET", default_value = "")]
    pub secret: String,

    /// Timeout in seconds for proxied HTTP requests to the Python backend.
    /// 0 means no timeout. Default: 120 seconds.
    #[arg(long, env = "UNFURL_PROXY_TIMEOUT_SECS", default_value_t = 120)]
    pub proxy_timeout_secs: u64,

    /// Timeout in seconds for Redis operations (GET, SET, RPUSH, etc.).
    /// 0 means no timeout. Default: 5 seconds.
    #[arg(long, env = "UNFURL_REDIS_TIMEOUT_SECS", default_value_t = 5)]
    pub redis_timeout_secs: u64,

    /// Short git hash of the unfurl package (matches Python's get_package_digest()).
    /// Used together with the cached `last_commit` to compute ETags.
    #[arg(long, env = "UNFURL_PACKAGE_DIGEST", default_value = "")]
    pub package_digest: String,
}

impl Config {
    /// Resolved backend URL (falls back to `http://{host}:{port+1}`).
    pub fn backend_url(&self) -> String {
        self.backend_url.clone().unwrap_or_else(|| {
            format!("http://{}:{}", self.host, self.port + 1)
        })
    }

    /// Redis key prefix for the write queue.
    pub fn queue_key(&self) -> String {
        format!("{}patch_queue", self.cache_key_prefix)
    }
}
