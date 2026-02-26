// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Redis cache look-up for `/export` and `/types` endpoints.
//!
//! The Python server (flask-caching with `RedisCache`) stores pickled
//! `CacheValue` NamedTuples.  We deserialise just enough to decide
//! whether the cached entry is still valid and, if so, return the JSON
//! payload without hitting the Python backend.

use redis::AsyncCommands;
use serde_json::Value as JsonValue;
use serde_pickle::{DeOptions, HashableValue};

/// Try to fetch and validate a cached response from Redis.
///
/// Returns `Some((json_value, etag))` on a valid cache hit, `None` otherwise.
/// The `etag` replicates Python's `CacheValue.make_etag()` so callers can
/// honour `If-None-Match` / return `Etag` headers without touching Python.
/// `timeout_secs` is the deadline for the Redis GET; 0 means no timeout.
pub async fn try_cache(
    conn: &mut redis::aio::MultiplexedConnection,
    key: &str,
    latest_commit: Option<&str>,
    timeout_secs: u64,
    package_digest: &str,
) -> Option<(JsonValue, String)> {
    let get_fut = conn.get::<_, Vec<u8>>(key);
    let raw: Vec<u8> = if timeout_secs > 0 {
        tokio::time::timeout(std::time::Duration::from_secs(timeout_secs), get_fut)
            .await
            .unwrap_or_else(|_| {
                tracing::warn!("Redis GET timed out for key: {}", key);
                Ok(Vec::new())
            })
            .ok()?
    } else {
        get_fut.await.ok()?
    };
    if raw.is_empty() {
        tracing::debug!("cache miss (no entry): {}", key);
        return None;
    }
    deserialize_cache_value(&raw, latest_commit, key, package_digest)
}

/// Deserialize the pickled CacheValue tuple and validate it.
///
/// The tuple has 5 fields:
///   0 - value (the JSON-serializable response dict)
///   1 - last_commit (str)
///   2 - latest_commit (str)  -- compared with the request param
///   3 - deps (dict)          -- must be empty for a cache hit
///   4 - last_commit_date (int)
fn deserialize_cache_value(
    raw: &[u8],
    latest_commit: Option<&str>,
    key: &str,
    package_digest: &str,
) -> Option<(JsonValue, String)> {
    let opts = DeOptions::default();
    let pickle_val: serde_pickle::Value = serde_pickle::from_slice(raw, opts).ok()?;

    let items = match &pickle_val {
        serde_pickle::Value::Tuple(v) => v.as_slice(),
        serde_pickle::Value::List(v) => v.as_slice(),
        _ => return None,
    };

    if items.len() < 5 {
        return None;
    }

    // Field 2: latest_commit -- must match the request's latest_commit param.
    let cached_commit = pickle_string(&items[2])?;
    if let Some(req_commit) = latest_commit {
        if !req_commit.is_empty() && cached_commit != req_commit {
            tracing::debug!(
                "cache ignored - commit mismatch: {} (request={}, cached={})",
                key, req_commit, cached_commit
            );
            return None;
        }
    }

    // Field 3: deps -- must be empty (or None) for us to serve from cache.
    if !deps_empty(&items[3]) {
        tracing::debug!("cache ignored - deps not empty: {}", key);
        return None;
    }

    // Field 1: last_commit -- used to compute the ETag.
    let last_commit = pickle_string(&items[1]).unwrap_or_default();
    let etag = compute_etag(&last_commit, package_digest);

    // Field 0: the actual response value. Convert pickle -> JSON.
    tracing::debug!("cache hit: {}", key);
    let json_val = pickle_to_json(&items[0])?;
    Some((json_val, etag))
}

/// Compute the ETag for a cached response, replicating Python's `CacheValue.make_etag()`.
///
/// Python: `etag = int(last_commit or "0", 16) ^ int(package_digest or "0", 16)`
///         `return f'W/"{hex(etag)}"'`
///
/// Both inputs are hex strings (git short/full hashes); the XOR is performed
/// as big-endian 160-bit integers (20 bytes), matching Python's arbitrary-precision int.
fn compute_etag(last_commit: &str, package_digest: &str) -> String {
    fn parse_hex_be(hex: &str) -> [u8; 20] {
        let mut bytes = [0u8; 20];
        let hex = hex.trim_start_matches("0x");
        // Left-pad to even length so chunks(2) works cleanly.
        let padded;
        let hex = if hex.len() % 2 != 0 {
            padded = format!("0{}", hex);
            padded.as_str()
        } else {
            hex
        };
        // Take at most the last 40 hex chars (20 bytes).
        let hex = if hex.len() > 40 { &hex[hex.len() - 40..] } else { hex };
        let start = 20 - hex.len() / 2;
        for (i, chunk) in hex.as_bytes().chunks(2).enumerate() {
            if let Ok(s) = std::str::from_utf8(chunk) {
                if let Ok(b) = u8::from_str_radix(s, 16) {
                    bytes[start + i] = b;
                }
            }
        }
        bytes
    }

    let commit_bytes = parse_hex_be(last_commit);
    let digest_bytes = parse_hex_be(package_digest);
    let xor: [u8; 20] = std::array::from_fn(|i| commit_bytes[i] ^ digest_bytes[i]);

    // Replicate Python's `hex(n)`: lowercase, "0x" prefix, no leading zeros.
    let hex_str: String = xor.iter().map(|b| format!("{:02x}", b)).collect();
    let trimmed = hex_str.trim_start_matches('0');
    let hex_val = if trimmed.is_empty() { "0" } else { trimmed };
    format!("W/\"0x{}\"", hex_val)
}

/// Extract a string from a pickle value.
fn pickle_string(val: &serde_pickle::Value) -> Option<String> {
    match val {
        serde_pickle::Value::String(s) => Some(s.clone()),
        serde_pickle::Value::Bytes(b) => String::from_utf8(b.clone()).ok(),
        _ => None,
    }
}

/// Check whether the deps field is empty (dict with no keys, None, or Bool(false)).
fn deps_empty(val: &serde_pickle::Value) -> bool {
    match val {
        serde_pickle::Value::None => true,
        serde_pickle::Value::Bool(false) => true,
        serde_pickle::Value::Dict(d) => d.is_empty(),
        _ => false,
    }
}

/// Best-effort conversion from a serde_pickle::Value to serde_json::Value.
fn pickle_to_json(val: &serde_pickle::Value) -> Option<JsonValue> {
    match val {
        serde_pickle::Value::None => Some(JsonValue::Null),
        serde_pickle::Value::Bool(b) => Some(JsonValue::Bool(*b)),
        serde_pickle::Value::I64(n) => Some(JsonValue::Number((*n).into())),
        serde_pickle::Value::F64(f) => {
            serde_json::Number::from_f64(*f).map(JsonValue::Number)
        }
        serde_pickle::Value::String(s) => Some(JsonValue::String(s.clone())),
        serde_pickle::Value::Bytes(b) => {
            String::from_utf8(b.clone()).ok().map(JsonValue::String)
        }
        serde_pickle::Value::List(items) | serde_pickle::Value::Tuple(items) => {
            let arr: Option<Vec<JsonValue>> = items.iter().map(pickle_to_json).collect();
            arr.map(JsonValue::Array)
        }
        serde_pickle::Value::Dict(entries) => {
            let mut map = serde_json::Map::new();
            for (k, v) in entries {
                let key = match k {
                    HashableValue::String(s) => s.clone(),
                    HashableValue::Bytes(b) => {
                        String::from_utf8(b.clone()).ok()?
                    }
                    _ => return None,
                };
                map.insert(key, pickle_to_json(v)?);
            }
            Some(JsonValue::Object(map))
        }
        serde_pickle::Value::Set(items) | serde_pickle::Value::FrozenSet(items) => {
            let arr: Option<Vec<JsonValue>> =
                items.iter().map(hashable_to_json).collect();
            arr.map(JsonValue::Array)
        }
        serde_pickle::Value::Int(_) => {
            // Big integer -- try to render as string
            Some(JsonValue::String(format!("{:?}", val)))
        }
    }
}

/// Convert a `HashableValue` (used as dict keys and set items) to JSON.
fn hashable_to_json(val: &HashableValue) -> Option<JsonValue> {
    match val {
        HashableValue::None => Some(JsonValue::Null),
        HashableValue::Bool(b) => Some(JsonValue::Bool(*b)),
        HashableValue::I64(n) => Some(JsonValue::Number((*n).into())),
        HashableValue::F64(f) => serde_json::Number::from_f64(*f).map(JsonValue::Number),
        HashableValue::String(s) => Some(JsonValue::String(s.clone())),
        HashableValue::Bytes(b) => String::from_utf8(b.clone()).ok().map(JsonValue::String),
        HashableValue::Tuple(items) => {
            let arr: Option<Vec<JsonValue>> =
                items.iter().map(hashable_to_json).collect();
            arr.map(JsonValue::Array)
        }
        HashableValue::FrozenSet(items) => {
            let arr: Option<Vec<JsonValue>> =
                items.iter().map(hashable_to_json).collect();
            arr.map(JsonValue::Array)
        }
        HashableValue::Int(_) => Some(JsonValue::String(format!("{:?}", val))),
    }
}
