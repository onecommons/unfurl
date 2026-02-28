// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Redis-backed write queue and background worker.
//!
//! POST write operations (create_*, update_*, delete_*) are enqueued
//! to a Redis list and processed asynchronously by a background task
//! that forwards them to the Python backend.

use redis::AsyncCommands;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;
use std::collections::HashMap;

/// Payload stored in the Redis queue list.
#[derive(Debug, Serialize, Deserialize)]
pub struct QueueItem {
    pub endpoint: String,
    pub body: JsonValue,
    pub headers: HashMap<String, String>,
}

/// Push a write request onto the Redis queue and return immediately.
pub async fn enqueue(
    conn: &mut redis::aio::MultiplexedConnection,
    queue_key: &str,
    item: &QueueItem,
) -> Result<(), redis::RedisError> {
    let payload = serde_json::to_string(item).unwrap_or_default();
    conn.rpush(queue_key, payload).await
}

/// Background worker: loops forever, popping items from the queue and
/// forwarding them to the Python backend.
///
/// Errors on individual items are logged but do not stop the worker.
pub async fn run_worker(
    mut conn: redis::aio::MultiplexedConnection,
    queue_key: String,
    backend_url: String,
    client: Client,
) {
    tracing::info!("queue worker started, listening on key: {}", queue_key);
    loop {
        // BLPOP with 0 timeout blocks indefinitely until an item appears.
        let result: Result<Option<(String, String)>, _> = redis::cmd("BLPOP")
            .arg(&queue_key)
            .arg(0)
            .query_async(&mut conn)
            .await;

        let payload = match result {
            Ok(Some((_key, val))) => val,
            Ok(None) => continue,
            Err(e) => {
                tracing::error!("BLPOP error: {}, retrying in 1s", e);
                tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                continue;
            }
        };

        let item: QueueItem = match serde_json::from_str(&payload) {
            Ok(i) => i,
            Err(e) => {
                tracing::error!("failed to parse queue item: {} -- payload: {}", e, payload);
                continue;
            }
        };

        let url = format!("{}{}", backend_url, item.endpoint);
        tracing::info!("processing queued request: {} {}", item.endpoint, url);

        let mut builder = client.post(&url);
        for (k, v) in &item.headers {
            builder = builder.header(k.as_str(), v.as_str());
        }
        builder = builder.json(&item.body);

        match builder.send().await {
            Ok(resp) => {
                let status = resp.status();
                if !status.is_success() {
                    let body = resp.text().await.unwrap_or_default();
                    tracing::error!(
                        "backend returned {} for {}: {}",
                        status,
                        item.endpoint,
                        body
                    );
                }
            }
            Err(e) => {
                tracing::error!("failed to forward {} to backend: {}", item.endpoint, e);
            }
        }
    }
}
