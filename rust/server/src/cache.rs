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
    tracing::debug!("try_cache: key={} latest_commit={:?}", key, latest_commit);
    if latest_commit.is_none() {
        tracing::debug!("cache ignored - no latest_commit");
        // TODO check pull cache if stale pulls are ok (CACHE_DEFAULT_PULL_TIMEOUT is set)
        return None;
    }
    // Use Option<Vec<u8>> so that a missing key (nil reply) decodes to None
    // instead of an Err, avoiding a silent cache-miss when the key is absent.
    let get_fut = conn.get::<_, Option<Vec<u8>>>(key);
    let raw_opt: Option<Vec<u8>> = if timeout_secs > 0 {
        tokio::time::timeout(std::time::Duration::from_secs(timeout_secs), get_fut)
            .await
            .unwrap_or_else(|_| {
                tracing::warn!("Redis GET timed out for key: {}", key);
                Ok(None)
            })
            .ok()?
    } else {
        get_fut.await.ok()?
    };
    let raw = match raw_opt {
        None => {
            tracing::info!("cache miss (no entry): {}", key);
            return None;
        }
        Some(b) if b.is_empty() => {
            tracing::info!("cache miss (no entry): {}", key);
            return None;
        }
        Some(b) => b,
    };
    // cachelib's RedisSerializer prepends b"!" before the pickle payload.
    let payload = if raw.first() == Some(&b'!') {
        &raw[1..]
    } else {
        &raw[..]
    };
    let (json_val, etag, has_deps) =
        deserialize_cache_value(payload, latest_commit, key, package_digest)?;
    if has_deps {
        tracing::debug!("cache ignored - deps not empty: {}", key);
        return None;
    }
    Some((json_val, etag))
}

/// Deserialize the pickled CacheValue tuple and validate it.
///
/// The tuple has 5 fields:
///   0 - value (the JSON-serializable response dict)
///   1 - last_commit (str)
///   2 - latest_commit (str)  -- compared with the request param
///   3 - deps (dict)          -- returned as `has_deps` flag for caller to check
///   4 - last_commit_date (int)
///
/// Returns `(json_value, etag, has_deps)` or `None` on parse/validation failure.
pub(crate) fn deserialize_cache_value(
    raw: &[u8],
    latest_commit: Option<&str>,
    key: &str,
    package_digest: &str,
) -> Option<(JsonValue, String, bool)> {
    // keep_restore_state: for NEWOBJ (used by Python NamedTuples) pushes the
    // args tuple onto the stack instead of an empty dict, letting us read the
    // 5-element CacheValue tuple without needing to call the Python class.
    // replace_unresolved_globals: replace unknown class refs with None instead
    // of erroring, needed for nested classes like CacheItemDependency in deps.
    let opts = DeOptions::new()
        .keep_restore_state()
        .replace_unresolved_globals();
    let pickle_val: serde_pickle::Value = match serde_pickle::from_slice(raw, opts) {
        Ok(v) => v,
        Err(e) => {
            tracing::warn!("cache pickle parse error for {}: {:?}", key, e);
            return None;
        }
    };

    let items = match &pickle_val {
        serde_pickle::Value::Tuple(v) => v.as_slice(),
        serde_pickle::Value::List(v) => v.as_slice(),
        other => {
            tracing::warn!(
                "cache ignored - unexpected pickle type for {}: {:?}",
                key,
                std::mem::discriminant(other)
            );
            return None;
        }
    };

    if items.len() < 5 {
        tracing::warn!(
            "cache ignored - pickle tuple too short ({} fields) for {}",
            items.len(),
            key
        );
        return None;
    }

    // Field 2: latest_commit -- must match the request's latest_commit param.
    let cached_commit = match pickle_string(&items[2]) {
        Some(s) => s,
        None => {
            tracing::warn!(
                "cache ignored - latest_commit field is not a string (type: {:?}) for {}",
                std::mem::discriminant(&items[2]),
                key
            );
            return None;
        }
    };
    if let Some(req_commit) = latest_commit {
        if !req_commit.is_empty() && cached_commit != req_commit {
            tracing::debug!(
                "cache ignored - commit mismatch: {} (request={}, cached={})",
                key,
                req_commit,
                cached_commit
            );
            return None;
        }
    }

    // Field 3: deps
    let has_deps = !deps_empty(&items[3]);

    // Field 1: last_commit -- used to compute the ETag.
    let last_commit = pickle_string(&items[1]).unwrap_or_default();
    let etag = compute_etag(&last_commit, package_digest);

    // Field 0: the actual response value. Convert pickle -> JSON.
    tracing::info!("cache hit: {}", key);
    let json_val = pickle_to_json(&items[0])?;
    Some((json_val, etag, has_deps))
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
        let hex = if hex.len() > 40 {
            &hex[hex.len() - 40..]
        } else {
            hex
        };
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
        serde_pickle::Value::F64(f) => serde_json::Number::from_f64(*f).map(JsonValue::Number),
        serde_pickle::Value::String(s) => Some(JsonValue::String(s.clone())),
        serde_pickle::Value::Bytes(b) => String::from_utf8(b.clone()).ok().map(JsonValue::String),
        serde_pickle::Value::List(items) | serde_pickle::Value::Tuple(items) => {
            let arr: Option<Vec<JsonValue>> = items.iter().map(pickle_to_json).collect();
            arr.map(JsonValue::Array)
        }
        serde_pickle::Value::Dict(entries) => {
            let mut map = serde_json::Map::new();
            for (k, v) in entries {
                let key = match k {
                    HashableValue::String(s) => s.clone(),
                    HashableValue::Bytes(b) => String::from_utf8(b.clone()).ok()?,
                    _ => return None,
                };
                map.insert(key, pickle_to_json(v)?);
            }
            Some(JsonValue::Object(map))
        }
        serde_pickle::Value::Set(items) | serde_pickle::Value::FrozenSet(items) => {
            let arr: Option<Vec<JsonValue>> = items.iter().map(hashable_to_json).collect();
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
            let arr: Option<Vec<JsonValue>> = items.iter().map(hashable_to_json).collect();
            arr.map(JsonValue::Array)
        }
        HashableValue::FrozenSet(items) => {
            let arr: Option<Vec<JsonValue>> = items.iter().map(hashable_to_json).collect();
            arr.map(JsonValue::Array)
        }
        HashableValue::Int(_) => Some(JsonValue::String(format!("{:?}", val))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // these are CacheValue objects from unfurl/server/serve.py created by tests/test_server.py::test_server_export_remote()
    // from these keys in redis:
    // "onecommons/project-templates/dashboard:main:ensemble/ensemble.yaml:deployment"
    // "onecommons/project-templates/application-blueprint:main:ensemble-template.yaml:blueprint"
    const DEPLOYMENT_PKL: &[u8] = include_bytes!("../tests/fixtures/deployment.pkl");
    const BLUEPRINT_PKL: &[u8] = include_bytes!("../tests/fixtures/blueprint.pkl");

    fn strip_cachelib(raw: &[u8]) -> &[u8] {
        if raw.first() == Some(&b'!') {
            &raw[1..]
        } else {
            raw
        }
    }

    #[test]
    fn deserialize_deployment_pickle() {
        let payload = strip_cachelib(DEPLOYMENT_PKL);
        let (json_val, etag, has_deps) =
            deserialize_cache_value(payload, None, "deployment_test_key", "")
                .expect("deployment.pkl should deserialize");

        // last_commit = fabf43928c19ca898369630cfd64a0926bc5de97, package_digest = ""
        assert_eq!(etag, "W/\"0xfabf43928c19ca898369630cfd64a0926bc5de97\"");
        assert!(!has_deps, "deployment.pkl should have empty deps");

        let obj = json_val.as_object().expect("value should be a JSON object");
        assert!(obj.contains_key("ResourceType"), "missing ResourceType key");
        assert!(obj.contains_key("Deployment"), "missing Deployment key");
    }

    #[test]
    fn deserialize_blueprint_pickle_has_deps() {
        let payload = strip_cachelib(BLUEPRINT_PKL);
        let (json_val, etag, has_deps) =
            deserialize_cache_value(payload, None, "blueprint_test_key", "")
                .expect("blueprint.pkl should deserialize");

        assert_eq!(etag, "W/\"0xacd052bc27c682ff655338fe04f9de116d98b177\"");
        assert!(has_deps, "blueprint.pkl should have non-empty deps");

        let obj = json_val.as_object().expect("value should be a JSON object");
        assert!(obj.contains_key("ResourceType"), "missing ResourceType key");

        // Verify serde_pickle can fully read the deps Dict[str, CacheItemDependency].
        // CacheItemDependency dataclass instances deserialize as plain Dicts.
        let opts = DeOptions::new()
            .keep_restore_state()
            .replace_unresolved_globals();
        let pickle_val: serde_pickle::Value =
            serde_pickle::from_slice(payload, opts).expect("re-parse for deps inspection");
        let items = match &pickle_val {
            serde_pickle::Value::Tuple(v) => v.as_slice(),
            serde_pickle::Value::List(v) => v.as_slice(),
            _ => panic!("expected tuple/list at top level"),
        };
        let deps = match &items[3] {
            serde_pickle::Value::Dict(d) => d,
            other => panic!(
                "deps should be a Dict, got: {:?}",
                std::mem::discriminant(other)
            ),
        };
        assert_eq!(
            deps.len(),
            1,
            "blueprint.pkl should have exactly 1 dep entry"
        );

        // Verify the dep key and that CacheItemDependency fields are accessible.
        let dep_key = HashableValue::String("onecommons/std:v1.1.0".into());
        let dep_val = deps
            .get(&dep_key)
            .expect("missing dep key onecommons/std:v1.1.0");

        let dep_fields = match dep_val {
            serde_pickle::Value::Dict(d) => d,
            _ => panic!("dep value should be a Dict (deserialized CacheItemDependency)"),
        };
        // Helper to look up a field by name in the dep dict.
        let get_field = |name: &str| -> &serde_pickle::Value {
            dep_fields
                .get(&HashableValue::String(name.into()))
                .unwrap_or_else(|| panic!("missing CacheItemDependency field: {}", name))
        };

        // Verify all CacheItemDependency field values.
        assert_eq!(
            get_field("project_id"),
            &serde_pickle::Value::String("onecommons/std".into())
        );
        assert_eq!(
            get_field("branch"),
            &serde_pickle::Value::String("v1.1.0".into())
        );
        assert_eq!(
            get_field("key"),
            &serde_pickle::Value::String("load_yaml".into())
        );
        assert_eq!(get_field("stale_pull_age"), &serde_pickle::Value::I64(120));
        assert_eq!(get_field("do_clone"), &serde_pickle::Value::Bool(true));
        assert_eq!(
            get_field("latest_commit"),
            &serde_pickle::Value::String("62ce5b304f12c1a810930d849f17b460cd2999f0".into())
        );
        assert_eq!(
            get_field("file_paths"),
            &serde_pickle::Value::List(vec![
                serde_pickle::Value::String("deployment-blueprints.yaml".into()),
                serde_pickle::Value::String("generic_types.yaml".into()),
            ])
        );
        assert_eq!(
            get_field("last_commits"),
            &serde_pickle::Value::List(vec![serde_pickle::Value::String(
                "62ce5b304f12c1a810930d849f17b460cd2999f0".into()
            ),])
        );
        assert_eq!(
            get_field("latest_package_url"),
            &serde_pickle::Value::String("https://unfurl.cloud/onecommons/std".into())
        );
    }

    /// Integration test: connect to Redis at UNFURL_TEST_REDIS_URL, write a key,
    /// read it back, and verify the value matches.  Skipped when the env var is absent.
    ///
    /// Run with:
    ///   UNFURL_TEST_REDIS_URL="unix:///path/to/redis.socket?db=2" \
    ///   cargo test -p unfurl-server -- --nocapture redis_roundtrip
    #[tokio::test]
    async fn redis_roundtrip() {
        let url = match std::env::var("UNFURL_TEST_REDIS_URL") {
            Ok(u) if !u.is_empty() => u,
            _ => {
                println!("UNFURL_TEST_REDIS_URL not set, skipping");
                return;
            }
        };

        let client = redis::Client::open(url.as_str()).expect("failed to open Redis client");
        let mut conn = client
            .get_multiplexed_async_connection()
            .await
            .expect("failed to connect to Redis");

        let test_key = "unfurl_server_rust_roundtrip_test";
        let test_val = "hello_from_rust_db_check";

        // Write via the configured URL (e.g. ?db=2)
        let _: () = redis::AsyncCommands::set(&mut conn, test_key, test_val)
            .await
            .expect("SET failed");

        // Read back on the same connection
        let got: String = redis::AsyncCommands::get(&mut conn, test_key)
            .await
            .expect("GET failed");

        assert_eq!(got, test_val, "roundtrip value mismatch");
        println!("Redis roundtrip OK: {} = {:?}", test_key, got);

        // When the URL selects a non-zero database, verify the key is NOT
        // visible in db=0 (guards against the redis crate silently ignoring
        // the db selector).
        //
        // Unix socket URLs use `?db=N`; TCP URLs use the path segment `/N`.
        // We only run this cross-check when we can tell a non-zero db is in use.
        let uses_nonzero_db = if url.contains("?db=") {
            // unix socket: ?db=N where N != 0
            url.split("?db=")
                .nth(1)
                .and_then(|s| s.split('&').next())
                .is_some_and(|n| n != "0")
        } else {
            // TCP: redis://host:port/N where N != 0
            url.trim_end_matches('/')
                .rsplit('/')
                .next()
                .is_some_and(|n| n.parse::<u16>().is_ok_and(|d| d != 0))
        };

        if uses_nonzero_db {
            // Build a db=0 URL to cross-check isolation.
            let db0_url = if url.contains("?db=") {
                // Unix socket: strip ?db=N, add ?db=0
                format!("{}?db=0", url.split('?').next().unwrap_or(&url))
            } else {
                // TCP: replace trailing /N with /0
                let base = url.rsplit_once('/').map_or(url.as_str(), |(b, _)| b);
                format!("{}/0", base)
            };
            let client0 =
                redis::Client::open(db0_url.as_str()).expect("failed to open db=0 client");
            let mut conn0 = client0
                .get_multiplexed_async_connection()
                .await
                .expect("failed to connect to db=0");
            let in_db0: Option<String> = redis::AsyncCommands::get(&mut conn0, test_key)
                .await
                .unwrap_or(None);
            println!(
                "Key in db=0: {:?} (should be None if db selector is honoured)",
                in_db0
            );
            assert!(
                in_db0.is_none(),
                "Key visible in db=0 — the redis crate is ignoring the db selector! \
                 url={:?}  db0_url={:?}",
                url,
                db0_url,
            );
        } else {
            println!("Skipping db isolation check (URL uses db=0)");
        }

        // Clean up (on the original connection / db)
        let _: () = redis::AsyncCommands::del(&mut conn, test_key)
            .await
            .expect("DEL failed");
    }

    /// Read any export-style key from Redis and attempt deserialization, printing
    /// diagnostics.  Useful for debugging cache miss issues.
    #[tokio::test]
    async fn redis_read_export_key() {
        let url = match std::env::var("UNFURL_TEST_REDIS_URL") {
            Ok(u) if !u.is_empty() => u,
            _ => {
                println!("UNFURL_TEST_REDIS_URL not set, skipping");
                return;
            }
        };

        let client = redis::Client::open(url.as_str()).expect("failed to open Redis client");
        let mut conn = client
            .get_multiplexed_async_connection()
            .await
            .expect("failed to connect to Redis");

        // Look for any key ending in ":deployment"
        let keys: Vec<String> = redis::cmd("KEYS")
            .arg("*:deployment")
            .query_async(&mut conn)
            .await
            .unwrap_or_default();

        if keys.is_empty() {
            println!("No *:deployment keys found in Redis — nothing to inspect");
            return;
        }

        for key in &keys {
            println!("\n--- Key: {} ---", key);
            let raw: Vec<u8> = redis::AsyncCommands::get(&mut conn, key)
                .await
                .unwrap_or_default();
            println!("  raw bytes len: {}", raw.len());
            if raw.is_empty() {
                println!("  (empty)");
                continue;
            }

            // cachelib prepends b"!" before the pickle payload — strip it.
            let payload = if raw.first() == Some(&b'!') {
                &raw[1..]
            } else {
                &raw[..]
            };
            println!(
                "  first byte: 0x{:02x} ({})",
                raw[0],
                if raw[0] == b'!' {
                    "cachelib prefix stripped"
                } else {
                    "no prefix"
                }
            );

            // Try to deserialize as pickle
            let opts = serde_pickle::DeOptions::default();
            match serde_pickle::from_slice::<serde_pickle::Value>(payload, opts) {
                Err(e) => println!("  pickle parse error: {:?}", e),
                Ok(val) => {
                    let items = match &val {
                        serde_pickle::Value::Tuple(v) => v.clone(),
                        serde_pickle::Value::List(v) => v.clone(),
                        other => {
                            println!("  unexpected top-level type: {:?}", other);
                            continue;
                        }
                    };
                    println!("  tuple length: {}", items.len());
                    for (i, item) in items.iter().enumerate() {
                        let short = format!("{:?}", item);
                        let short = if short.len() > 120 {
                            &short[..120]
                        } else {
                            &short
                        };
                        println!("  field[{}]: {}", i, short);
                    }

                    // Try a full deserialization with latest_commit=None
                    match super::deserialize_cache_value(payload, None, key, "") {
                        Some((json_val, etag, has_deps)) => {
                            let preview = serde_json::to_string(&json_val).unwrap_or_default();
                            let preview = if preview.len() > 200 {
                                &preview[..200]
                            } else {
                                &preview
                            };
                            println!(
                                "  => deserialized OK, etag={:?}, has_deps={}, json preview: {}",
                                etag, has_deps, preview
                            );
                        }
                        None => println!("  => deserialize_cache_value returned None"),
                    }
                }
            }
        }
    }
}
