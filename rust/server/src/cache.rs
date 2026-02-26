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
/// Returns `Some(json_value)` on a valid cache hit, `None` otherwise.
/// `timeout_secs` is the deadline for the Redis GET; 0 means no timeout.
pub async fn try_cache(
    conn: &mut redis::aio::MultiplexedConnection,
    key: &str,
    latest_commit: Option<&str>,
    timeout_secs: u64,
) -> Option<JsonValue> {
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
        return None;
    }
    deserialize_cache_value(&raw, latest_commit)
}

/// Deserialize the pickled CacheValue tuple and validate it.
///
/// The tuple has 5 fields:
///   0 - value (the JSON-serializable response dict)
///   1 - last_commit (str)
///   2 - latest_commit (str)  -- compared with the request param
///   3 - deps (dict)          -- must be empty for a cache hit
///   4 - last_commit_date (int)
fn deserialize_cache_value(raw: &[u8], latest_commit: Option<&str>) -> Option<JsonValue> {
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
            return None;
        }
    }

    // Field 3: deps -- must be empty (or None) for us to serve from cache.
    if !deps_empty(&items[3]) {
        return None;
    }

    // Field 0: the actual response value. Convert pickle -> JSON.
    pickle_to_json(&items[0])
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
