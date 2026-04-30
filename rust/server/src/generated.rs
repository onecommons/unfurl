// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Auto-generated request/response types from the unfurl OpenAPI
//! spec. Generated at build time by `oas3-gen` via `build.rs`.
//!
// The whole module is auto-generated, so we silence the
// usual lints here.
// We allow deprecated here because its tripped because the auto-generated
// `CloudmapRepositoryNotable::CloudmapInlineArtifact` type is marked as deprecated in the cloudmap json schema.
#![allow(unused_imports, dead_code, deprecated, clippy::all)]

include!(concat!(env!("OUT_DIR"), "/unfurl_types.rs"));
