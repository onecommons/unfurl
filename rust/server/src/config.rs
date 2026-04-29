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

    /// Redis connection URL. When absent, falls back to CACHE_REDIS_HOST/PORT/PASSWORD/DB.
    #[arg(long, env = "CACHE_REDIS_URL")]
    pub redis_url: Option<String>,

    /// Redis host (used when CACHE_REDIS_URL is not set).
    #[arg(long, env = "CACHE_REDIS_HOST")]
    pub redis_host: Option<String>,

    /// Redis port (used when CACHE_REDIS_URL is not set).
    #[arg(long, env = "CACHE_REDIS_PORT", default_value_t = 6379)]
    pub redis_port: u16,

    /// Redis password (used when CACHE_REDIS_URL is not set).
    #[arg(long, env = "CACHE_REDIS_PASSWORD")]
    pub redis_password: Option<String>,

    /// Redis database number (used when CACHE_REDIS_URL is not set).
    #[arg(long, env = "CACHE_REDIS_DB", default_value_t = 0)]
    pub redis_db: u16,

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

    /// Maximum request body size in bytes for proxied and write requests.
    /// Default: 10 MiB (10485760 bytes).
    #[arg(long, env = "UNFURL_MAX_BODY_BYTES", default_value_t = 10 * 1024 * 1024)]
    pub max_body_bytes: usize,

    /// Batch window in seconds for consolidating PATCH requests to the same
    /// project.  Items enqueued within this window are merged before being
    /// forwarded to the backend.  0 disables batching (each item is forwarded
    /// individually).  Default: 5 seconds.
    #[arg(long, env = "UNFURL_BATCH_WINDOW_SECS", default_value_t = 5)]
    pub batch_window_secs: u64,

    /// Path to a working directory of a cloudmap git repo.
    /// When set together with `cloudmap_db_url`, GET /cloudmap is served
    /// using the `unfurl-git-sync` crate; otherwise it is proxied to the Python
    /// backend.
    #[arg(long, env = "UNFURL_CLOUDMAP_REPO")]
    pub cloudmap_repo: Option<String>,

    /// SQL database URL backing the cloudmap index. Accepts
    /// `sqlite::memory:`, `sqlite:///path/to.db`, or a Postgres URL
    /// (`postgres://...`). Required for the local cloudmap handler.
    #[arg(long, env = "UNFURL_CLOUDMAP_DB_URL")]
    pub cloudmap_db_url: Option<String>,
}

impl Config {
    /// Resolved backend URL (falls back to `http://{host}:{port+1}`).
    pub fn backend_url(&self) -> String {
        self.backend_url
            .clone()
            .unwrap_or_else(|| format!("http://{}:{}", self.host, self.port + 1))
    }

    /// Resolved Redis URL: prefers `CACHE_REDIS_URL`, otherwise builds from
    /// `CACHE_REDIS_HOST`/`PORT`/`PASSWORD`/`DB`.  Returns `None` when neither
    /// `redis_url` nor `redis_host` is set.
    pub fn effective_redis_url(&self) -> Option<String> {
        if let Some(url) = &self.redis_url {
            return Some(url.clone());
        }
        let host = self.redis_host.as_deref()?;
        let userinfo = match &self.redis_password {
            Some(pw) => format!(":{}@", pw),
            None => String::new(),
        };
        Some(format!(
            "redis://{}{}:{}/{}",
            userinfo, host, self.redis_port, self.redis_db
        ))
    }

    /// Redis key prefix for the write queue (legacy, still used as fallback).
    pub fn queue_key(&self) -> String {
        format!("{}patch_queue", self.cache_key_prefix)
    }

    /// Redis key for the per-project batch list.
    pub fn batch_list_key(&self, project_id: &str) -> String {
        format!("{}batch:{}", self.cache_key_prefix, project_id)
    }

    /// Redis sorted set tracking projects whose batch window has expired.
    pub fn batch_ready_set_key(&self) -> String {
        format!("{}batch_ready", self.cache_key_prefix)
    }

    /// Redis key prefix for distributed batch processing locks.
    pub fn batch_lock_prefix(&self) -> String {
        format!("{}batch_lock:", self.cache_key_prefix)
    }

    /// Return the effective Redis URL with any password redacted for logging.
    pub fn redacted_redis_url(&self) -> Option<String> {
        self.effective_redis_url().map(|url| redact_url(&url))
    }
}

/// Replace the password portion of a Redis URL with `***`.
/// Handles both `redis://:password@host` and `redis://user:password@host`.
fn redact_url(url: &str) -> String {
    // Unix socket URLs (redis+unix:// or unix://) and plain redis:// URLs
    // share the same `:password@` pattern in the authority section.
    if let Some(at) = url.find('@') {
        if let Some(colon) = url[..at].rfind(':') {
            return format!("{}:***{}", &url[..colon], &url[at..]);
        }
    }
    url.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Helper: build a Config with only the redis fields set, everything else defaulted.
    fn config_with_redis(
        url: Option<&str>,
        host: Option<&str>,
        port: u16,
        password: Option<&str>,
        db: u16,
    ) -> Config {
        Config {
            host: "127.0.0.1".into(),
            port: 8080,
            backend_url: None,
            redis_url: url.map(Into::into),
            redis_host: host.map(Into::into),
            redis_port: port,
            redis_password: password.map(Into::into),
            redis_db: db,
            cache_key_prefix: "ufsv::".into(),
            secret: String::new(),
            proxy_timeout_secs: 120,
            redis_timeout_secs: 5,
            package_digest: String::new(),
            max_body_bytes: 10 * 1024 * 1024,
            batch_window_secs: 5,
            cloudmap_repo: None,
            cloudmap_db_url: None,
        }
    }

    #[test]
    fn effective_redis_url_prefers_url() {
        let cfg = config_with_redis(
            Some("redis://myhost:1234/5"),
            Some("ignored"),
            9999,
            Some("ignored"),
            9,
        );
        assert_eq!(cfg.effective_redis_url().unwrap(), "redis://myhost:1234/5");
    }

    #[test]
    fn effective_redis_url_from_host_port_db() {
        let cfg = config_with_redis(None, Some("redis.example.com"), 6380, None, 3);
        assert_eq!(
            cfg.effective_redis_url().unwrap(),
            "redis://redis.example.com:6380/3"
        );
    }

    #[test]
    fn effective_redis_url_with_password() {
        let cfg = config_with_redis(None, Some("localhost"), 6379, Some("s3cret"), 0);
        assert_eq!(
            cfg.effective_redis_url().unwrap(),
            "redis://:s3cret@localhost:6379/0"
        );
    }

    #[test]
    fn effective_redis_url_defaults() {
        let cfg = config_with_redis(None, Some("localhost"), 6379, None, 0);
        assert_eq!(
            cfg.effective_redis_url().unwrap(),
            "redis://localhost:6379/0"
        );
    }

    #[test]
    fn effective_redis_url_none_when_no_config() {
        let cfg = config_with_redis(None, None, 6379, None, 0);
        assert!(cfg.effective_redis_url().is_none());
    }

    #[test]
    fn redact_url_hides_password() {
        assert_eq!(
            super::redact_url("redis://:s3cret@localhost:6379/0"),
            "redis://:***@localhost:6379/0"
        );
        assert_eq!(
            super::redact_url("redis://user:pass@host:6379/0"),
            "redis://user:***@host:6379/0"
        );
    }

    #[test]
    fn redact_url_noop_without_password() {
        assert_eq!(
            super::redact_url("redis://localhost:6379/0"),
            "redis://localhost:6379/0"
        );
        assert_eq!(
            super::redact_url("unix:///var/run/redis.sock?db=2"),
            "unix:///var/run/redis.sock?db=2"
        );
    }
}
