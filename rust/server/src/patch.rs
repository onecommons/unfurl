// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Apply a list of GraphQL-style patch entries to a typename-keyed JSON map.
//!
//! Ported from `_do_patch` in `unfurl/server/serve.py`.
//!
//! `target` is a two-level dict: `{ __typename: { name: GraphqlObject } }`.
//! Each patch entry is itself a JSON object carrying control fields:
//!
//! - `__typename` (required): bucket within `target` to mutate.
//! - `name` (required for inserts; defaults to `__deleted` for deletes):
//!   key under which the entry is stored or removed.
//! - `__deleted`: when present, the entry is a delete instruction.
//!   `name == "*"` removes the entire `typename` bucket; otherwise the
//!   single named entry is removed.
//!
//! Malformed entries (missing `name`/`__typename`, or `name == "*"` without
//! `__deleted`) are logged and skipped.
//!
//! This is used to apply cached "patch" arrays to a target document for the
//! `/create_ensemble`, `/update_ensemble`, and `/create_provider` endpoints.

use serde_json::{Map, Value};
use tracing::warn;

/// Apply `patches` to `target` in place. `target` must be a JSON object
/// (the outer typename-keyed map); non-object values are ignored. Each entry
/// in `patches` should also be an object — non-objects are skipped.
pub fn apply_patch(patches: &[Value], target: &mut Value) {
    let Some(target_map) = target.as_object_mut() else {
        return;
    };
    for patch in patches {
        let Some(patch_obj) = patch.as_object() else {
            continue;
        };
        apply_one(patch_obj, target_map);
    }
}

fn apply_one(patch: &Map<String, Value>, target: &mut Map<String, Value>) {
    let typename = patch.get("__typename").and_then(Value::as_str);
    let deleted = patch.get("__deleted");

    // `name` defaults to the `__deleted` value when this is a delete entry,
    // matching the Python `patch_inner.get("name", deleted)` fallback.
    let name = patch
        .get("name")
        .and_then(Value::as_str)
        .or_else(|| deleted.and_then(Value::as_str));

    let (Some(typename), Some(name)) = (typename, name) else {
        warn!("skipping malformed patch {patch:?}");
        return;
    };

    if deleted.is_some() {
        if name == "*" {
            target.remove(typename);
            return;
        }
        let bucket = target
            .entry(typename.to_string())
            .or_insert_with(|| Value::Object(Map::new()));
        let Some(bucket_map) = bucket.as_object_mut() else {
            return;
        };
        if bucket_map.remove(name).is_none() {
            warn!("skipping delete: {name} is missing from {typename}");
        }
        return;
    }

    if name == "*" {
        warn!("error: name is '*' not allowed without '__deleted', skipping {patch:?}");
        return;
    }

    let bucket = target
        .entry(typename.to_string())
        .or_insert_with(|| Value::Object(Map::new()));
    if let Some(bucket_map) = bucket.as_object_mut() {
        bucket_map.insert(name.to_string(), Value::Object(patch.clone()));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn apply(patches: Value, target: Value) -> Value {
        let patches = patches.as_array().unwrap().clone();
        let mut target = target;
        apply_patch(&patches, &mut target);
        target
    }

    #[test]
    fn insert_into_typename_bucket() {
        let result = apply(
            json!([{"__typename": "ResourceTemplate", "name": "db", "type": "Database"}]),
            json!({}),
        );
        assert_eq!(
            result,
            json!({
                "ResourceTemplate": {
                    "db": {"__typename": "ResourceTemplate", "name": "db", "type": "Database"}
                }
            })
        );
    }

    #[test]
    fn insert_into_existing_bucket() {
        let result = apply(
            json!([{"__typename": "T", "name": "b", "v": 2}]),
            json!({"T": {"a": {"name": "a", "v": 1}}}),
        );
        let bucket = result["T"].as_object().unwrap();
        assert_eq!(bucket.len(), 2);
        assert_eq!(bucket["a"], json!({"name": "a", "v": 1}));
        assert_eq!(bucket["b"], json!({"__typename": "T", "name": "b", "v": 2}));
    }

    #[test]
    fn delete_named_entry() {
        let result = apply(
            json!([{"__typename": "T", "__deleted": "a", "name": "a"}]),
            json!({"T": {"a": {"v": 1}, "b": {"v": 2}}}),
        );
        assert_eq!(result, json!({"T": {"b": {"v": 2}}}));
    }

    #[test]
    fn delete_uses_deleted_field_when_name_absent() {
        let result = apply(
            json!([{"__typename": "T", "__deleted": "a"}]),
            json!({"T": {"a": {"v": 1}, "b": {"v": 2}}}),
        );
        assert_eq!(result, json!({"T": {"b": {"v": 2}}}));
    }

    #[test]
    fn delete_wildcard_clears_typename_bucket() {
        let result = apply(
            json!([{"__typename": "T", "__deleted": "*"}]),
            json!({"T": {"a": {"v": 1}}, "U": {"x": 1}}),
        );
        assert_eq!(result, json!({"U": {"x": 1}}));
    }

    #[test]
    fn delete_missing_name_is_a_noop() {
        let result = apply(
            json!([{"__typename": "T", "__deleted": "ghost"}]),
            json!({"T": {"a": {"v": 1}}}),
        );
        assert_eq!(result, json!({"T": {"a": {"v": 1}}}));
    }

    #[test]
    fn insert_with_name_wildcard_is_skipped() {
        // name == "*" is only valid in delete entries.
        let result = apply(
            json!([{"__typename": "T", "name": "*", "v": 1}]),
            json!({"T": {"a": {"v": 1}}}),
        );
        assert_eq!(result, json!({"T": {"a": {"v": 1}}}));
    }

    #[test]
    fn malformed_entry_missing_typename_is_skipped() {
        let result = apply(
            json!([{"name": "a", "v": 1}]),
            json!({"T": {"a": {"v": 1}}}),
        );
        assert_eq!(result, json!({"T": {"a": {"v": 1}}}));
    }

    #[test]
    fn malformed_entry_missing_name_is_skipped() {
        let result = apply(
            json!([{"__typename": "T", "v": 1}]),
            json!({"T": {"a": {"v": 1}}}),
        );
        assert_eq!(result, json!({"T": {"a": {"v": 1}}}));
    }

    #[test]
    fn multiple_patches_applied_in_order() {
        let result = apply(
            json!([
                {"__typename": "T", "name": "a", "v": 1},
                {"__typename": "T", "name": "b", "v": 2},
                {"__typename": "T", "__deleted": "a"},
                {"__typename": "U", "name": "x", "v": 99}
            ]),
            json!({}),
        );
        assert_eq!(
            result,
            json!({
                "T": {"b": {"__typename": "T", "name": "b", "v": 2}},
                "U": {"x": {"__typename": "U", "name": "x", "v": 99}}
            })
        );
    }

    #[test]
    fn non_object_target_is_ignored() {
        let mut target = json!([1, 2, 3]);
        apply_patch(&[json!({"__typename": "T", "name": "a"})], &mut target);
        assert_eq!(target, json!([1, 2, 3]));
    }

    #[test]
    fn non_object_patch_entry_is_skipped() {
        let result = apply(
            json!([null, "string", 42, {"__typename": "T", "name": "a"}]),
            json!({}),
        );
        assert_eq!(
            result,
            json!({"T": {"a": {"__typename": "T", "name": "a"}}})
        );
    }
}
