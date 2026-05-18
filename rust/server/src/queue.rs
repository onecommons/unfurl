// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Redis-backed write queue with atomic batch consolidation.
//!
//! POST write operations (create_*, update_*, delete_*) are enqueued
//! to per-project Redis lists.  A background worker drains items whose
//! batch window has expired, partitions them by `(project_id, branch)`,
//! and forwards each partition to the Python backend's `/batch_patch`
//! endpoint.  The batch's top-level `latest_commit` and `queueid` come
//! from the last queued request in the partition.

use once_cell::sync::Lazy;
use redis::AsyncCommands;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;
use std::collections::HashMap;

use crate::config::Config;

static ENQUEUE: Lazy<redis::Script> = Lazy::new(|| redis::Script::new(ENQUEUE_SCRIPT));
static INC_QUEUEID: Lazy<redis::Script> = Lazy::new(|| redis::Script::new(INC_QUEUEID_SCRIPT));

/// Payload stored in the Redis per-project batch list.
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct QueueItem {
    /// Full path and query string, e.g. `/create_provider?auth_project=foo`.
    pub endpoint: String,
    pub body: JsonValue,
    pub headers: HashMap<String, String>,
}

// ---------------------------------------------------------------------------
// Lua Scripts
// ---------------------------------------------------------------------------

/// Lua script for atomic enqueue.
///
/// KEYS[1] = per-project batch list key
/// KEYS[2] = batch ready sorted set key
/// ARGV[1] = serialised QueueItem JSON
/// ARGV[2] = batch window in seconds
///
/// Logic:
/// 1. RPUSH the item onto the per-project list.
/// 2. If this is the first item (list length == 1), record the deadline in the
///    ready set as `now + batch_window` so the worker knows when to drain.
const ENQUEUE_SCRIPT: &str = r#"
local list_key = KEYS[1]
local ready_key = KEYS[2]
local payload  = ARGV[1]
local window   = tonumber(ARGV[2])

redis.call('RPUSH', list_key, payload)
local len = redis.call('LLEN', list_key)
if len == 1 then
    local t = redis.call('TIME')
    local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
    local deadline = now + window
    redis.call('ZADD', ready_key, 'NX', deadline, list_key)
end
return len
"#;

/// Lua script for atomic queueid validation and increment.
///
/// KEYS[1] = queue key: "queue:{project_id}:{latest_commit}"
/// ARGV[1] = client's queueid (integer as string)
/// ARGV[2] = "queue:{project_id}:" prefix for checking newer commits
///
/// The queue key value is either:
///   - A plain integer queueid (e.g. "1")
///   - "{new_commit},{last_queueid}" after batch_patch commits
///
/// Returns one of:
///   - "error" on conflict (stale queueid or newer patch in flight)
///   - "{new_queueid}" when incrementing on current commit
///   - "{new_commit},{new_queueid}" when a new commit was produced
const INC_QUEUEID_SCRIPT: &str = r#"
local queue_key = KEYS[1]
local queueid = tonumber(ARGV[1])
local prefix = ARGV[2]

local current = redis.call('GET', queue_key)
if not current then
    if queueid > 0 then
        -- previous patch in flight should have created the key
        return "error"
    end
    -- first patch: create and set to 1
    redis.call('SET', queue_key, '1')
    return "1"
end

-- parse current value: either "queueid" or "new_commit,last_queueid"
local new_commit = nil
local last_queueid = nil
local comma = string.find(current, ',')
if comma then
    new_commit = string.sub(current, 1, comma - 1)
    last_queueid = tonumber(string.sub(current, comma + 1))
else
    last_queueid = tonumber(current)
end

if last_queueid > queueid then
    -- there was a newer patch
    return "error"
end

if new_commit then
    -- batch_patch produced a new commit; check if there's already a newer queue
    local newer_key = prefix .. new_commit
    local newer = redis.call('GET', newer_key)
    if newer then
        -- there's already a newer patch in flight
        return "error"
    end
    -- start a new queue on the new commit
    redis.call('SET', newer_key, '1')
    return new_commit .. ',1'
end

-- increment queueid on current commit
local new_queueid = redis.call('INCR', queue_key)
return tostring(new_queueid)
"#;

/// Maximum number of items to drain in a single LPOP call.
/// This is a safety cap; in practice batch lists are much shorter.
const DRAIN_BATCH_SIZE: usize = 1000;

// ---------------------------------------------------------------------------
// Enqueue
// ---------------------------------------------------------------------------

/// Result of `inc_queueid`: either a new queueid (with an optional new
/// commit hash) or a conflict error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum QueueIdResult {
    /// The queueid was incremented on the current commit.
    Ok { new_queueid: i64 },
    /// A new commit was produced by batch_patch; the client should use the
    /// new commit hash going forward.
    NewCommit {
        new_commit: String,
        new_queueid: i64,
    },
    /// Conflict: stale queueid or newer patch already in flight.
    Conflict,
}

/// Atomically validate and increment the queueid for a project+commit.
///
/// The queue key `queue:{project_id}:{latest_commit}` tracks the sequence
/// of patches against a given commit.  When `batch_patch` commits, the
/// Python server replaces the value with `"{new_commit},{queueid}"`.
/// (see _update_queue_key() in unfurl/server/endpoints.py)
pub async fn inc_queueid(
    conn: &mut redis::aio::MultiplexedConnection,
    config: &Config,
    project_id: &str,
    latest_commit: &str,
    queueid: i64,
) -> Result<QueueIdResult, redis::RedisError> {
    let queue_key = config.queue_entry_key(project_id, latest_commit);
    let prefix = config.queue_entry_prefix(project_id);

    let result: String = INC_QUEUEID
        .key(&queue_key)
        .arg(queueid)
        .arg(&prefix)
        .invoke_async(conn)
        .await?;

    if result == "error" {
        return Ok(QueueIdResult::Conflict);
    }

    if let Some((commit, qid_str)) = result.split_once(',') {
        let new_queueid = qid_str.parse::<i64>().unwrap_or(0);
        Ok(QueueIdResult::NewCommit {
            new_commit: commit.to_string(),
            new_queueid,
        })
    } else {
        let new_queueid = result.parse::<i64>().unwrap_or(0);
        Ok(QueueIdResult::Ok { new_queueid })
    }
}

/// Result of [`check_export_queue`]: either redirect the client to a
/// committed new commit, or tell them to retry because their queued
/// write hasn't been committed yet.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExportQueueCheck {
    /// The queue key records a new commit produced by `batch_patch`,
    /// and the recorded `last_queueid` is at least as high as the
    /// client's `queueid`. The caller should treat this commit as the
    /// effective `latest_commit`.
    UseNewCommit(String),
    /// The client's queued write hasn't been committed yet (either no
    /// commit recorded against `latest_commit`, or the recorded
    /// `last_queueid` is below the client's `queueid`). Caller should
    /// reply 503 with a Retry-After hint.
    Retry,
}

/// Resolve whether an `/export` (or similar) request with a `queueid`
/// can proceed.
///
/// Reads the queue key `queue:{project_id}:{latest_commit}` and
/// interprets its value the same way `INC_QUEUEID_SCRIPT` does:
///   * Missing key → [`ExportQueueCheck::Retry`]
///   * `"N"` (no commit produced yet) → [`ExportQueueCheck::Retry`]
///   * `"{new_commit},{N}"` where `N >= request_queueid` →
///     [`ExportQueueCheck::UseNewCommit`]
///   * `"{new_commit},{N}"` where `N < request_queueid` →
///     [`ExportQueueCheck::Retry`] (more queued writes still pending)
pub async fn check_export_queue(
    conn: &mut redis::aio::MultiplexedConnection,
    config: &Config,
    project_id: &str,
    latest_commit: &str,
    request_queueid: i64,
) -> Result<ExportQueueCheck, redis::RedisError> {
    let queue_key = config.queue_entry_key(project_id, latest_commit);
    let val: Option<String> = conn.get(&queue_key).await?;
    let Some(val) = val else {
        return Ok(ExportQueueCheck::Retry);
    };
    let Some((new_commit, qid_str)) = val.split_once(',') else {
        return Ok(ExportQueueCheck::Retry);
    };
    let last_queueid: i64 = qid_str.parse().unwrap_or(0);
    if last_queueid < request_queueid {
        return Ok(ExportQueueCheck::Retry);
    }
    Ok(ExportQueueCheck::UseNewCommit(new_commit.to_string()))
}

/// Report whether the queue for `(project_id, branch)` is busy: either
/// the per-project batch list contains an item targeting this branch,
/// or `run_worker` currently holds the project's batch lock (which it
/// keeps from before its `LPOP` drain through the last `/batch_patch`
/// forward to Python — so this catches the window where items have
/// been popped from the list but Python hasn't yet committed them).
///
/// Caveat: the lock has a TTL of `proxy_timeout_secs`; a Python commit
/// that exceeds that window can let this check fall through to
/// "not busy" while a batch is still being applied. That's a
/// pre-existing assumption of the batch lock, not introduced here.
pub async fn has_pending_writes(
    conn: &mut redis::aio::MultiplexedConnection,
    config: &Config,
    project_id: &str,
    branch: &str,
) -> Result<bool, redis::RedisError> {
    let list_key = config.batch_list_key(project_id);
    let lock_key = format!("{}{}", config.batch_lock_prefix(), list_key);

    let locked: Option<String> = conn.get(&lock_key).await?;
    if locked.is_some() {
        return Ok(true);
    }

    let items: Vec<String> = redis::cmd("LRANGE")
        .arg(&list_key)
        .arg(0)
        .arg(-1)
        .query_async(conn)
        .await?;
    for raw in items {
        if let Ok(item) = serde_json::from_str::<QueueItem>(&raw) {
            let item_branch = item
                .body
                .get("branch")
                .and_then(|v| v.as_str())
                .unwrap_or("main");
            if item_branch == branch {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

/// Force the batch worker to drain a project's pending queue on its
/// next poll, by lowering its `batch_ready` deadline to "now".
///
/// Uses `ZADD <ready_key> XX <now> <list_key>`: the `XX` flag means
/// "only update if the member already exists", so this is a no-op
/// when no work is queued (avoids creating a phantom entry that the
/// worker would otherwise pick up and try to drain).
///
/// Used by the `/export` and `/types` wait-and-proxy path: when a
/// client's request can only be satisfied after the in-flight batch
/// commits, we'd rather collapse the remainder of the batch window
/// than make the client poll on a 503.  The worker still owns the
/// commit itself; this just expedites when it runs.
pub async fn kick_worker(
    conn: &mut redis::aio::MultiplexedConnection,
    config: &Config,
    project_id: &str,
) -> Result<(), redis::RedisError> {
    let list_key = config.batch_list_key(project_id);
    let ready_key = config.batch_ready_set_key();
    let now = get_redis_time(conn).await.ok_or_else(|| {
        redis::RedisError::from((redis::ErrorKind::ResponseError, "Redis TIME failed"))
    })?;
    let _: redis::Value = redis::cmd("ZADD")
        .arg(&ready_key)
        .arg("XX")
        .arg(now)
        .arg(&list_key)
        .query_async(conn)
        .await?;
    Ok(())
}

/// Push a write request onto the per-project batch list and register the
/// batch deadline in the ready set (atomically via Lua).
pub async fn enqueue(
    conn: &mut redis::aio::MultiplexedConnection,
    config: &Config,
    project_id: &str,
    item: &QueueItem,
) -> Result<(), redis::RedisError> {
    let list_key = config.batch_list_key(project_id);
    let ready_key = config.batch_ready_set_key();
    let payload = serde_json::to_string(item).unwrap_or_default();

    ENQUEUE
        .key(list_key)
        .key(ready_key)
        .arg(payload)
        .arg(config.batch_window_secs)
        .invoke_async::<i64>(conn)
        .await?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Partitioning
// ---------------------------------------------------------------------------

/// Grouping key: requests sharing the same `(project_id, branch)` are
/// sent together in one `/batch_patch` call so the Python backend can
/// apply them in order and push once.  Requests against different
/// `latest_commit` values for the same branch are merged into a single
/// batch; the last queued request's `latest_commit` and `queueid` are
/// used as the batch's top-level values.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct PartitionKey {
    project_id: String,
    branch: String,
}

/// A single request entry inside a batch, preserving the original endpoint
/// and body so the Python backend can dispatch each one in order.
#[derive(Debug, Serialize)]
struct BatchRequest {
    /// Endpoint path without leading `/` (e.g. `"create_provider"`).
    endpoint: String,
    /// The original request body.
    #[serde(flatten)]
    body: JsonValue,
}

/// A partitioned batch ready to be posted to `/batch_patch`.
#[derive(Debug, Serialize)]
struct PartitionedBatch {
    /// The last queued request's `latest_commit` — Python applies the
    /// batch against this revision and updates the queue key from it.
    latest_commit: String,
    branch: String,
    /// The individual requests in submission order.
    requests: Vec<BatchRequest>,
    /// The last queued request's `queueid` — Python uses this to
    /// update the queue key after committing.
    #[serde(skip_serializing_if = "Option::is_none")]
    queueid: Option<i64>,
}

/// Extract the endpoint name from a full path+query string.
/// `/create_provider?auth_project=foo` → `create_provider`.
fn endpoint_name(endpoint: &str) -> &str {
    let path = endpoint.split('?').next().unwrap_or(endpoint);
    path.strip_prefix('/').unwrap_or(path)
}

/// Parse a `queueid` field from a request body.
fn extract_queueid(body: &JsonValue) -> Option<i64> {
    body.get("queueid").and_then(|v| v.as_i64())
}

/// Partition a list of queue items by `(project_id, branch)`. Items
/// within each partition preserve their original order. The batch's
/// `latest_commit` and `queueid` are taken from the last queued request
/// in the partition.
fn consolidate(
    project_id: &str,
    items: Vec<QueueItem>,
) -> Vec<(PartitionedBatch, HashMap<String, String>)> {
    // Preserve insertion order with a Vec of (key, batch, headers).
    let mut groups: Vec<(PartitionKey, PartitionedBatch, HashMap<String, String>)> = Vec::new();

    for item in items {
        let body = &item.body;
        let key = PartitionKey {
            project_id: project_id.to_string(),
            branch: body
                .get("branch")
                .and_then(|v| v.as_str())
                .unwrap_or("main")
                .to_string(),
        };

        let latest_commit = body
            .get("latest_commit")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let queueid = extract_queueid(body);

        let req = BatchRequest {
            endpoint: endpoint_name(&item.endpoint).to_string(),
            body: body.clone(),
        };

        if let Some(idx) = groups.iter().position(|(k, _, _)| k == &key) {
            let batch = &mut groups[idx].1;
            batch.requests.push(req);
            batch.latest_commit = latest_commit;
            batch.queueid = queueid;
        } else {
            let batch = PartitionedBatch {
                latest_commit,
                branch: key.branch.clone(),
                requests: vec![req],
                queueid,
            };
            groups.push((key, batch, item.headers));
        }
    }

    groups.into_iter().map(|(_, b, h)| (b, h)).collect()
}

// ---------------------------------------------------------------------------
// Worker
// ---------------------------------------------------------------------------

/// Background worker: polls the ready set for projects whose batch window
/// has expired, drains their items atomically, consolidates, and forwards
/// merged batches to the Python backend.
pub async fn run_worker(
    mut conn: redis::aio::MultiplexedConnection,
    config: Config,
    backend_url: String,
    client: Client,
) {
    let ready_key = config.batch_ready_set_key();
    let lock_prefix = config.batch_lock_prefix();
    let poll_interval = std::time::Duration::from_secs_f64(config.worker_poll_interval_secs);

    tracing::debug!(
        "batch worker started (window={}s, poll={}ms, ready_set={})",
        config.batch_window_secs,
        poll_interval.as_millis(),
        ready_key,
    );

    loop {
        // Poll for projects whose deadline has passed.
        // ZRANGEBYSCORE ready_key -inf <now>
        let now: f64 = match get_redis_time(&mut conn).await {
            Some(t) => t,
            None => {
                tokio::time::sleep(poll_interval).await;
                continue;
            }
        };

        let ready_keys: Vec<String> = match redis::cmd("ZRANGE")
            .arg(&ready_key)
            .arg("-inf")
            .arg(now)
            .arg("BYSCORE")
            .query_async(&mut conn)
            .await
        {
            Ok(keys) => keys,
            Err(e) => {
                tracing::error!(
                    "ZRANGE BYSCORE error: {}, retrying in {}ms",
                    e,
                    poll_interval.as_millis()
                );
                tokio::time::sleep(poll_interval).await;
                continue;
            }
        };

        if ready_keys.is_empty() {
            // Nothing ready yet; sleep briefly and poll again.
            tokio::time::sleep(poll_interval).await;
            continue;
        }

        for list_key in &ready_keys {
            // Try to acquire a distributed lock so only one worker processes
            // this project's batch.
            let lock_key = format!("{}{}", lock_prefix, list_key);
            let acquired: bool = matches!(
                redis::cmd("SET")
                    .arg(&lock_key)
                    .arg("1")
                    .arg("NX")
                    .arg("EX")
                    .arg(config.proxy_timeout_secs)
                    .query_async(&mut conn)
                    .await,
                Ok(Some(redis::Value::Okay)) | Ok(Some(_))
            );
            if !acquired {
                continue; // another worker is handling this batch
            }

            // Remove from ready set first to prevent duplicate processing.
            let _: Result<i64, _> = conn.zrem(&ready_key, list_key).await;

            // Drain all items from the list using LPOP with a count argument
            // (available since Redis 6.2).  This pops up to DRAIN_BATCH_SIZE
            // items atomically in a single round-trip.
            let raw_items: Vec<String> = match redis::cmd("LPOP")
                .arg(list_key)
                .arg(DRAIN_BATCH_SIZE)
                .query_async(&mut conn)
                .await
            {
                Ok(items) => items,
                Err(e) => {
                    tracing::error!("LPOP drain error for {}: {}", list_key, e);
                    let _: Result<(), _> = conn.del(&lock_key).await;
                    continue;
                }
            };

            if raw_items.is_empty() {
                let _: Result<(), _> = conn.del(&lock_key).await;
                continue;
            }

            tracing::debug!("draining {} items from batch {}", raw_items.len(), list_key);

            let items: Vec<QueueItem> = raw_items
                .iter()
                .filter_map(|s| match serde_json::from_str(s) {
                    Ok(item) => Some(item),
                    Err(e) => {
                        tracing::error!("failed to parse queue item: {} -- {}", e, s);
                        None
                    }
                })
                .collect();

            // Extract project_id from the list_key (format: prefix + "batch:" + project_id).
            let project_id = list_key
                .rsplit_once("batch:")
                .map(|(_, pid)| pid)
                .unwrap_or("");

            let batches = consolidate(project_id, items);

            for (batch, headers) in batches {
                let url = format!(
                    "{}/batch_patch?auth_project={}",
                    backend_url,
                    urlencoding::encode(project_id)
                );
                tracing::debug!(
                    "forwarding batch to {} (requests={})",
                    url,
                    batch.requests.len()
                );

                let batch_json = match serde_json::to_value(&batch) {
                    Ok(v) => v,
                    Err(e) => {
                        tracing::error!("failed to serialize merged batch: {}", e);
                        continue;
                    }
                };

                let mut builder = client.post(&url);
                for (k, v) in &headers {
                    builder = builder.header(k.as_str(), v.as_str());
                }
                builder = builder.json(&batch_json);

                match builder.send().await {
                    Ok(resp) => {
                        let status = resp.status();
                        if !status.is_success() {
                            let body = resp.text().await.unwrap_or_default();
                            tracing::error!(
                                "backend returned {} for batch_patch (project={}): {}",
                                status,
                                project_id,
                                body
                            );
                        }
                    }
                    Err(e) => {
                        tracing::error!(
                            "failed to forward batch_patch to backend (project={}): {}",
                            project_id,
                            e
                        );
                    }
                }
            }

            // Release the lock.
            let _: Result<(), _> = conn.del(&lock_key).await;
        }
    }
}

/// Get the current time from Redis (consistent across instances).
pub(crate) async fn get_redis_time(conn: &mut redis::aio::MultiplexedConnection) -> Option<f64> {
    let result: Result<(i64, i64), _> = redis::cmd("TIME").query_async(conn).await;
    match result {
        Ok((secs, micros)) => Some(secs as f64 + micros as f64 / 1_000_000.0),
        Err(e) => {
            tracing::error!("Redis TIME error: {}", e);
            None
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_consolidate_single_item() {
        let items = vec![QueueItem {
            endpoint: "/create_provider?auth_project=proj1".into(),
            body: json!({
                "patch": [{"__typename": "ResourceTemplate", "name": "t1"}],
                "commit_msg": "add provider",
                "branch": "main",
                "latest_commit": "abc123",
                "queueid": 1,
                "deployment_path": "deploy1",
                "environment": "prod",
            }),
            headers: HashMap::new(),
        }];

        let batches = consolidate("proj1", items);
        assert_eq!(batches.len(), 1);
        let (batch, _) = &batches[0];
        assert_eq!(batch.requests.len(), 1);
        assert_eq!(batch.requests[0].endpoint, "create_provider");
        assert_eq!(batch.branch, "main");
        assert_eq!(batch.latest_commit, "abc123");
        assert_eq!(batch.queueid, Some(1));
    }

    #[test]
    fn test_consolidate_groups_same_branch_commit() {
        let items = vec![
            QueueItem {
                endpoint: "/create_provider?auth_project=proj1".into(),
                body: json!({
                    "patch": [{"__typename": "A"}],
                    "commit_msg": "msg1",
                    "branch": "main",
                    "latest_commit": "abc",
                    "queueid": 1,
                    "deployment_path": "d1",
                }),
                headers: HashMap::new(),
            },
            QueueItem {
                endpoint: "/delete_deployment?auth_project=proj1".into(),
                body: json!({
                    "patch": [{"__typename": "B"}],
                    "commit_msg": "msg2",
                    "branch": "main",
                    "latest_commit": "abc",
                    "queueid": 2,
                }),
                headers: HashMap::new(),
            },
        ];

        let batches = consolidate("proj1", items);
        assert_eq!(batches.len(), 1);
        let (batch, _) = &batches[0];
        assert_eq!(batch.requests.len(), 2);
        // Order preserved.
        assert_eq!(batch.requests[0].endpoint, "create_provider");
        assert_eq!(batch.requests[1].endpoint, "delete_deployment");
        // Last request's latest_commit and queueid surface at the top level.
        assert_eq!(batch.latest_commit, "abc");
        assert_eq!(batch.queueid, Some(2));
    }

    #[test]
    fn test_consolidate_groups_different_commits_same_branch() {
        // Same branch, different latest_commit values now end up in the
        // same partition; the last queued request's commit/queueid wins.
        let items = vec![
            QueueItem {
                endpoint: "/update_ensemble?auth_project=proj1".into(),
                body: json!({
                    "patch": [{"a": 1}],
                    "branch": "main",
                    "latest_commit": "abc",
                    "queueid": 1,
                }),
                headers: HashMap::new(),
            },
            QueueItem {
                endpoint: "/update_ensemble?auth_project=proj1".into(),
                body: json!({
                    "patch": [{"b": 2}],
                    "branch": "main",
                    "latest_commit": "def",
                    "queueid": 1,
                }),
                headers: HashMap::new(),
            },
        ];

        let batches = consolidate("proj1", items);
        assert_eq!(batches.len(), 1);
        let (batch, _) = &batches[0];
        assert_eq!(batch.requests.len(), 2);
        assert_eq!(batch.latest_commit, "def");
        assert_eq!(batch.queueid, Some(1));
    }

    #[test]
    fn test_consolidate_different_branches() {
        let items = vec![
            QueueItem {
                endpoint: "/update_ensemble?auth_project=proj1".into(),
                body: json!({
                    "patch": [{"a": 1}],
                    "branch": "main",
                    "latest_commit": "abc",
                    "queueid": 1,
                }),
                headers: HashMap::new(),
            },
            QueueItem {
                endpoint: "/update_ensemble?auth_project=proj1".into(),
                body: json!({
                    "patch": [{"b": 2}],
                    "branch": "dev",
                    "latest_commit": "abc",
                    "queueid": 1,
                }),
                headers: HashMap::new(),
            },
        ];

        let batches = consolidate("proj1", items);
        assert_eq!(batches.len(), 2);
    }

    #[test]
    fn test_consolidate_preserves_order() {
        let items = vec![
            QueueItem {
                endpoint: "/create_provider?auth_project=proj1".into(),
                body: json!({
                    "patch": [{"__typename": "A"}],
                    "branch": "main",
                    "latest_commit": "abc",
                    "queueid": 1,
                    "deployment_path": "environments/gcp/primary_provider",
                }),
                headers: HashMap::new(),
            },
            QueueItem {
                endpoint: "/update_environment?auth_project=proj1".into(),
                body: json!({
                    "patch": [{"__typename": "B"}],
                    "branch": "main",
                    "latest_commit": "abc",
                    "queueid": 2,
                }),
                headers: HashMap::new(),
            },
            QueueItem {
                endpoint: "/delete_deployment?auth_project=proj1".into(),
                body: json!({
                    "patch": [{"__typename": "C"}],
                    "branch": "main",
                    "latest_commit": "abc",
                    "queueid": 3,
                }),
                headers: HashMap::new(),
            },
        ];

        let batches = consolidate("proj1", items);
        assert_eq!(batches.len(), 1);
        let (batch, _) = &batches[0];
        assert_eq!(batch.requests.len(), 3);
        assert_eq!(batch.requests[0].endpoint, "create_provider");
        assert_eq!(batch.requests[1].endpoint, "update_environment");
        assert_eq!(batch.requests[2].endpoint, "delete_deployment");
        assert_eq!(batch.queueid, Some(3));
    }

    #[test]
    fn test_endpoint_name() {
        assert_eq!(endpoint_name("/create_ensemble"), "create_ensemble");
        assert_eq!(
            endpoint_name("/create_provider?auth_project=x"),
            "create_provider"
        );
        assert_eq!(endpoint_name("/delete_deployment?a=b"), "delete_deployment");
        assert_eq!(endpoint_name("/update_ensemble"), "update_ensemble");
    }
}
