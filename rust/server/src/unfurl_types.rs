#![allow(unused_imports, dead_code, deprecated, clippy::all)]
use axum::response::IntoResponse;
use serde::{Deserialize, Serialize};
use validator::Validate;
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
#[serde(untagged)]
pub enum ArrayKind {
    #[default]
    ExportResponseDeployment(ExportResponseDeployment),
    Object(std::collections::HashMap<String, serde_json::Value>),
}
/// JSON body for /batch_patch -- used by the Rust proxy to forward
/// a batch of write requests that share the same branch and latest_commit.
///
/// The ``requests`` list preserves the original submission order so the
/// Python backend can apply each operation sequentially before pushing once.
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
#[serde(default)]
pub struct BatchPatchBody {
    /// Target branch
    #[default(Some("main".to_string()))]
    pub branch: Option<String>,
    /// Latest known commit hash for optimistic concurrency checks
    pub latest_commit: Option<String>,
    /// Internal version counter, external clients should omit this field
    pub queueid: Option<i64>,
    /// Ordered list of original requests, each with 'endpoint' key and the original body fields
    pub requests: Vec<std::collections::HashMap<String, serde_json::Value>>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
#[serde(default)]
pub struct CloudMapDocument {
    #[serde(rename = "apiVersion")]
    pub api_version: CloudMapDocumentApiVersion,
    /// Tangible object that instantiates services or other artifacts. Artifact ID is either a package URL (see <https://github.com/package-url/purl-spec>) or repository URL with path.
    pub artifacts: Option<std::collections::HashMap<String, Box<CloudmapArtifact>>>,
    /// Components that are produced or consumed by artifacts and services. Components describe relationships (references, instantiates, dependencies) and are identified by URL or label.
    pub components: Option<std::collections::HashMap<String, Box<CloudmapComponent>>>,
    /// Build and deployment information for artifacts and services. Keys are URLs.
    pub instantiations: Option<std::collections::HashMap<String, Box<CloudmapInstantiation>>>,
    #[default("CloudMap".to_string())]
    pub kind: String,
    /// Common metadata fields shared across artifacts, services, instantiations, and repositories.
    pub metadata: Option<CloudmapMetadata>,
    /// Git repositories. Keys are URLs that start with git://
    pub repositories: Option<std::collections::HashMap<String, CloudmapRepository>>,
    /// Instances of services.
    pub services: Option<std::collections::HashMap<String, Box<CloudmapService>>>,
    /// Type definitions for artifacts, services, software, and capabilities.
    pub types: Option<std::collections::HashMap<String, CloudmapType>>,
}
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize, oas3_gen_support::Default)]
pub enum CloudMapDocumentApiVersion {
    #[serde(rename = "unfurl/v1alpha1")]
    #[default]
    UnfurlV1alpha1,
    #[serde(rename = "unfurl/v1.0.0")]
    UnfurlV100,
}
impl core::fmt::Display for CloudMapDocumentApiVersion {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::UnfurlV1alpha1 => write!(f, "unfurl/v1alpha1"),
            Self::UnfurlV100 => write!(f, "unfurl/v1.0.0"),
        }
    }
}
impl core::str::FromStr for CloudMapDocumentApiVersion {
    type Err = String;
    fn from_str(s: &str) -> core::result::Result<Self, Self::Err> {
        match s {
            "unfurl/v1alpha1" => Ok(Self::UnfurlV1alpha1),
            "unfurl/v1.0.0" => Ok(Self::UnfurlV100),
            _ => Err(format!(
                "unknown variant '{}', expected one of: {}",
                s, "unfurl/v1alpha1, unfurl/v1.0.0"
            )),
        }
    }
}
/// Pydantic wrapper for :class:`GraphJson`, used by ``@app.output``.
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudMapResponse {
    /// Error message when the record is not found
    pub error: Option<String>,
    /// List of root record references (single-record mode)
    pub roots: Option<Vec<CloudMapResponseRecordRef>>,
    /// Map of section name → {url → GraphNodeJson}
    pub sections: Option<
        std::collections::HashMap<
            String,
            std::collections::HashMap<String, CloudMapResponseGraphNodeJson>,
        >,
    >,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
/// A node in the JSON graph representation.
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudMapResponseGraphNodeJson {
    pub kind: Option<String>,
    pub rels: Option<std::collections::HashMap<String, Vec<serde_json::Value>>>,
    pub url: Option<String>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
/// A reference to a record in the graph, used in relationship lists.
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudMapResponseRecordRef {
    pub kind: Option<String>,
    pub missing: Option<bool>,
    pub type_refs: Option<Vec<CloudMapResponseTypeRefJson>>,
    pub url: Option<String>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
/// A type reference in the graph, used in relationship lists.
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudMapResponseTypeRefJson {
    pub constraints: Option<std::collections::HashMap<String, serde_json::Value>>,
    pub label: Option<String>,
    #[serde(rename = "type")]
    pub r#type: Option<String>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
/// The queried (and optionally filtered) CloudMap document under ``result``. ``followed`` and ``next_page_token`` appear only when the request asked for what they carry, so their absence is meaningful rather than empty.
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudMapResult {
    /// Records discovered by walking the graph from ``key``. Present only when the request asked to ``follow`` from a ``key``.
    pub followed: Option<CloudMapDocument>,
    /// Cursor to pass as ``page_token`` for the next page. Present only on a ``limit`` request that has one -- its absence ends the walk.
    pub next_page_token: Option<String>,
    pub result: CloudMapDocument,
}
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudmapArtifact {
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub contains: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub dependencies: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Cryptographic digest of the artifact.
    pub digest: Option<String>,
    /// Indicates whether the artifact identifier refers to an artifact that will not change.
    pub immutable: Option<bool>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub instantiated_by: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub instantiates: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Human-readable metadata about the artifact.
    pub metadata: Option<serde_json::Value>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub references: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Scheduled Release for an artifact or service.
    pub release_schedule: Option<Vec<serde_json::Value>>,
    pub status: Option<CloudmapLifecycleStatus>,
    /// List of available tags for this artifact (e.g., container image tags).
    pub tags: Option<Vec<String>>,
    /// Type references with optional constraints. Keys are type names, values are either null or objects with constraint properties such as version.
    #[serde(rename = "type")]
    pub r#type: Option<std::collections::HashMap<String, Option<serde_json::Value>>>,
    /// Artifacts that are variants of this artifact (for example, releases or snapshots). Each artifact inherits the metadata of this one unless overridden in its declaration. Identifiers should share the base ID as this package. If versions share the same digest, the artifact identifier refers to the same physical artifact, such as a tagged container image.
    pub versions: Option<std::collections::HashMap<String, Box<CloudmapArtifact>>>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudmapComponent {
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub contains: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub dependencies: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub instantiated_by: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub instantiates: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Common metadata fields shared across artifacts, services, instantiations, and repositories.
    pub metadata: Option<CloudmapMetadata>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub references: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    pub status: Option<CloudmapLifecycleStatus>,
    /// Type references with optional constraints. Keys are type names, values are either null or objects with constraint properties such as version.
    #[serde(rename = "type")]
    pub r#type: Option<std::collections::HashMap<String, Option<serde_json::Value>>>,
    /// Components that are variants of this component (for example, different versions or configurations). Each component inherits the metadata of this one unless overridden in its declaration.
    pub versions: Option<std::collections::HashMap<String, Box<CloudmapComponent>>>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
/// Metadata discovery information.
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudmapDiscovery {
    /// Date and time of the last metadata check, conforming to RFC 3339.
    pub last_checked: Option<chrono::DateTime<chrono::Utc>>,
    /// List of URLs that were used for metadata discovery, such as API URLs or PR URLs for manual edits.
    pub sources: Option<Vec<String>>,
}
/// **(Deprecated)** Inline artifact
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudmapInlineArtifact {
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudmapInstantiation {
    /// Cryptographic digest of the document referenced by the instantiation URL.
    pub digest: Option<String>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub inputs: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub instantiated: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Common metadata fields shared across artifacts, services, instantiations, and repositories.
    pub metadata: Option<CloudmapMetadata>,
    /// If instantiation URL references a repository, source control revision of that repository.
    pub revision: Option<String>,
    /// Repository or artifact URL.
    pub source: Option<String>,
    /// If source URL references a repository, the branch or tag name.
    pub source_ref: Option<String>,
    /// If source URL references a repository, the source control revision of that repository.
    pub source_revision: Option<String>,
    pub status: Option<CloudmapLifecycleStatus>,
    /// Type references with optional constraints. Keys are type names, values are either null or objects with constraint properties such as version.
    #[serde(rename = "type")]
    pub r#type: Option<std::collections::HashMap<String, Option<serde_json::Value>>>,
    /// Instantiations that are variants of this instantiation (for example, different deployments or environments). Each instantiation inherits the metadata of this one unless overridden in its declaration.
    pub versions: Option<std::collections::HashMap<String, Box<CloudmapInstantiation>>>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize, oas3_gen_support::Default)]
pub enum CloudmapLifecycleStatus {
    #[serde(rename = "wishlist")]
    #[default]
    Wishlist,
    #[serde(rename = "model")]
    Model,
    #[serde(rename = "planned")]
    Planned,
    #[serde(rename = "development")]
    Development,
    #[serde(rename = "alpha")]
    Alpha,
    #[serde(rename = "beta")]
    Beta,
    #[serde(rename = "production")]
    Production,
    #[serde(rename = "maintenance")]
    Maintenance,
    #[serde(rename = "unmaintained")]
    Unmaintained,
    #[serde(rename = "deprecated")]
    Deprecated,
    #[serde(rename = "removed")]
    Removed,
}
impl core::fmt::Display for CloudmapLifecycleStatus {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::Wishlist => write!(f, "wishlist"),
            Self::Model => write!(f, "model"),
            Self::Planned => write!(f, "planned"),
            Self::Development => write!(f, "development"),
            Self::Alpha => write!(f, "alpha"),
            Self::Beta => write!(f, "beta"),
            Self::Production => write!(f, "production"),
            Self::Maintenance => write!(f, "maintenance"),
            Self::Unmaintained => write!(f, "unmaintained"),
            Self::Deprecated => write!(f, "deprecated"),
            Self::Removed => write!(f, "removed"),
        }
    }
}
impl core::str::FromStr for CloudmapLifecycleStatus {
    type Err = String;
    fn from_str(s: &str) -> core::result::Result<Self, Self::Err> {
        match s {
            "wishlist" => Ok(Self::Wishlist),
            "model" => Ok(Self::Model),
            "planned" => Ok(Self::Planned),
            "development" => Ok(Self::Development),
            "alpha" => Ok(Self::Alpha),
            "beta" => Ok(Self::Beta),
            "production" => Ok(Self::Production),
            "maintenance" => Ok(Self::Maintenance),
            "unmaintained" => Ok(Self::Unmaintained),
            "deprecated" => Ok(Self::Deprecated),
            "removed" => Ok(Self::Removed),
            _ => {
                Err(
                    format!(
                        "unknown variant '{}', expected one of: {}", s,
                        "wishlist, model, planned, development, alpha, beta, production, maintenance, unmaintained, deprecated, removed"
                    ),
                )
            }
        }
    }
}
/// Common metadata fields shared across artifacts, services, instantiations, and repositories.
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudmapMetadata {
    /// Date and time on which the resource was created, conforming to RFC 3339.
    pub created: Option<chrono::DateTime<chrono::Utc>>,
    /// Human-readable description.
    pub description: Option<String>,
    /// Metadata discovery information.
    pub discovery: Option<CloudmapDiscovery>,
    /// Link to issue, PR/MR, or discussion about this definition.
    pub discussion_url: Option<String>,
    /// URL to get documentation.
    pub documentation_url: Option<String>,
    /// URL to the entity this is a fork of.
    pub fork_of: Option<String>,
    /// URL to find more information.
    pub homepage_url: Option<String>,
    /// Informal pointer to source ref (branch or tag name).
    pub source_ref: Option<String>,
    /// Informal pointer to source code revision. Use when deployment information is not available.
    pub source_revision: Option<String>,
    /// Informal pointer to source code. Use when deployment information is not available.
    pub source_url: Option<String>,
    /// License(s) as an SPDX License Expression.
    pub spdx_licenses: Option<String>,
    /// Icon or thumbnail URL.
    pub thumbnail_url: Option<String>,
    /// Human-readable title.
    pub title: Option<String>,
    /// List of topic or categories associated with the resource.
    pub topics: Option<Vec<String>>,
    /// Name of the distributing entity, organization, or individual.
    pub vendor: Option<String>,
    /// Version. The version may match a label or tag in the source code repository or may be Semantic Versioning-compatible.
    pub version: Option<CloudmapMetadataVersion>,
}
/// Version. The version may match a label or tag in the source code repository or may be Semantic Versioning-compatible.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
#[serde(untagged)]
pub enum CloudmapMetadataVersion {
    #[default]
    String(String),
    Number(f64),
}
/// Common relationships used by artifacts and components to describe how they relate to other records and types. Each field is a typedURLs map whose key is a URL or label and whose value is an optional type reference.
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudmapRelationships {
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub contains: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub dependencies: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub instantiated_by: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub instantiates: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub references: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
}
/// Scheduled Release for an artifact or service.
pub type CloudmapReleaseSchedule = Vec<CloudmapReleaseScheduleCloudmapReleaseSchedule>;
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudmapReleaseScheduleCloudmapReleaseSchedule {
    /// The date and time the release will happen (RFC 3339 format).
    pub effective_date: Option<chrono::DateTime<chrono::Utc>>,
    pub status: Option<CloudmapLifecycleStatus>,
    /// The updated resource URL for this upcoming release.
    pub url: Option<String>,
    /// Version of the upcoming release.
    pub version: Option<CloudmapReleaseScheduleCloudmapReleaseScheduleVersion>,
}
/// Version of the upcoming release.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
#[serde(untagged)]
pub enum CloudmapReleaseScheduleCloudmapReleaseScheduleVersion {
    #[default]
    String(String),
    Number(f64),
}
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudmapRepository {
    /// Map of branch names to their commit SHA hashes.
    pub branches: Option<std::collections::HashMap<String, String>>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub contains: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Default branch name (e.g., main, master).
    pub default_branch: Option<String>,
    /// URL of the repository that this repository was forked from.
    pub fork_of: Option<String>,
    /// Initial commit of the default branch.
    pub initial_revision: Option<String>,
    /// Internal identifier from the repository host (e.g., GitHub repository ID).
    pub internal_id: Option<String>,
    /// Metadata about the repository that isn't stored in the git repository itself but might be provided by the host (e.g., metadata found on the repository's GitHub or GitLab project page).
    pub metadata: Option<serde_json::Value>,
    /// URL of the repository that this repository is a mirror of.
    pub mirror_of: Option<String>,
    /// Repository name.
    pub name: Option<String>,
    /// Project path relative to base location of git repositories on the host.
    pub path: Option<String>,
    /// True if the repository is not publicly accessible.
    pub private: Option<bool>,
    /// URL to the repository's project page on the host (e.g., <https://github.com/user/repo>).
    pub project_url: Option<String>,
    /// List of protocols available to clone the repository (e.g., https, ssh).
    pub protocols: Option<Vec<String>>,
    /// URL of the service hosting this repository.
    pub service: Option<String>,
    /// Map of tag names to their commit SHA hashes.
    pub tags: Option<std::collections::HashMap<String, String>>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudmapService {
    /// Access to the service (who can resolve the URL).
    pub access: Option<CloudmapServiceAccess>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub connections: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub endpoints: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
    pub instantiated_by: Option<std::collections::HashMap<String, Box<CloudmapTypeRef>>>,
    /// Common metadata fields shared across artifacts, services, instantiations, and repositories.
    pub metadata: Option<CloudmapMetadata>,
    /// Service policies and legal information.
    pub policies: Option<CloudmapServicePolicies>,
    /// Scheduled Release for an artifact or service.
    pub release_schedule: Option<Vec<serde_json::Value>>,
    pub status: Option<CloudmapLifecycleStatus>,
    /// Type references with optional constraints. Keys are type names, values are either null or objects with constraint properties such as version.
    #[serde(rename = "type")]
    pub r#type: Option<std::collections::HashMap<String, Option<serde_json::Value>>>,
    /// Services that are variants of this service (for example, different versions or environments). Each service inherits the metadata of this one unless overridden in its declaration. Identifiers should share the base URL as this service.
    pub versions: Option<std::collections::HashMap<String, Box<CloudmapService>>>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
/// Access to the service (who can resolve the URL).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize, oas3_gen_support::Default)]
pub enum CloudmapServiceAccess {
    #[serde(rename = "public")]
    #[default]
    Public,
    #[serde(rename = "private")]
    Private,
    #[serde(rename = "none")]
    None,
}
impl core::fmt::Display for CloudmapServiceAccess {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::Public => write!(f, "public"),
            Self::Private => write!(f, "private"),
            Self::None => write!(f, "none"),
        }
    }
}
impl core::str::FromStr for CloudmapServiceAccess {
    type Err = String;
    fn from_str(s: &str) -> core::result::Result<Self, Self::Err> {
        match s {
            "public" => Ok(Self::Public),
            "private" => Ok(Self::Private),
            "none" => Ok(Self::None),
            _ => Err(format!(
                "unknown variant '{}', expected one of: {}",
                s, "public, private, none"
            )),
        }
    }
}
/// Service policies and legal information.
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudmapServicePolicies {
    /// URL to the privacy policy.
    pub privacy_policy: Option<String>,
    /// License(s) under which the service is distributed as an SPDX License Expression.
    pub spdx_licenses: Option<String>,
    /// URL to the terms of service.
    pub terms_of_service: Option<String>,
}
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudmapType {
    /// List of fully-qualified type names that this type extends.
    pub extends: Option<Vec<String>>,
    /// The kind of the type. One of: Component, Artifact, or Capability.
    pub kind: Option<CloudmapTypeKind>,
    /// Common metadata fields shared across artifacts, services, instantiations, and repositories.
    pub metadata: Option<CloudmapMetadata>,
    /// URL of artifact or service to use a model for instances of this type.
    pub model: Option<String>,
    /// Fully-qualified name of the type. Repeats the key this type is stored under.
    pub name: Option<String>,
    /// JSON Schema describing the properties of instances of this type.
    pub properties: Option<serde_json::Value>,
    /// Artifact containing type definition. Include if it cannot be derived from the type name.
    pub source: Option<String>,
    /// Maturity level of the type definition.
    pub status: Option<CloudmapTypeStatus>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
/// The kind of the type. One of: Component, Artifact, or Capability.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize, oas3_gen_support::Default)]
pub enum CloudmapTypeKind {
    #[default]
    Component,
    Artifact,
    Capability,
}
impl core::fmt::Display for CloudmapTypeKind {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::Component => write!(f, "Component"),
            Self::Artifact => write!(f, "Artifact"),
            Self::Capability => write!(f, "Capability"),
        }
    }
}
impl core::str::FromStr for CloudmapTypeKind {
    type Err = String;
    fn from_str(s: &str) -> core::result::Result<Self, Self::Err> {
        match s {
            "Component" => Ok(Self::Component),
            "Artifact" => Ok(Self::Artifact),
            "Capability" => Ok(Self::Capability),
            _ => Err(format!(
                "unknown variant '{}', expected one of: {}",
                s, "Component, Artifact, Capability"
            )),
        }
    }
}
/// Type references with optional constraints. Keys are type names, values are either null or objects with constraint properties such as version.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudmapTypeRef {
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, Option<serde_json::Value>>,
}
/// Maturity level of the type definition.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize, oas3_gen_support::Default)]
pub enum CloudmapTypeStatus {
    #[serde(rename = "draft")]
    #[default]
    Draft,
    #[serde(rename = "experimental")]
    Experimental,
    #[serde(rename = "stable")]
    Stable,
    #[serde(rename = "deprecated")]
    Deprecated,
    #[serde(rename = "removed")]
    Removed,
}
impl core::fmt::Display for CloudmapTypeStatus {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::Draft => write!(f, "draft"),
            Self::Experimental => write!(f, "experimental"),
            Self::Stable => write!(f, "stable"),
            Self::Deprecated => write!(f, "deprecated"),
            Self::Removed => write!(f, "removed"),
        }
    }
}
impl core::str::FromStr for CloudmapTypeStatus {
    type Err = String;
    fn from_str(s: &str) -> core::result::Result<Self, Self::Err> {
        match s {
            "draft" => Ok(Self::Draft),
            "experimental" => Ok(Self::Experimental),
            "stable" => Ok(Self::Stable),
            "deprecated" => Ok(Self::Deprecated),
            "removed" => Ok(Self::Removed),
            _ => Err(format!(
                "unknown variant '{}', expected one of: {}",
                s, "draft, experimental, stable, deprecated, removed"
            )),
        }
    }
}
/// Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or "metadata". Alternatively, keys can be labels and its value a nested typed URL map.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct CloudmapTypedUrLs {
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, Box<CloudmapTypeRef>>,
}
/// GraphQL-style JSON database returned by /export and /types.
///
/// Each top-level key maps to a dict of named GraphQL objects of that type,
/// reflecting the GraphQL schema defined in unfurl/graphql.py.
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct ExportResponse {
    /// Map of blueprint name → ApplicationBlueprint object
    #[serde(rename = "ApplicationBlueprint")]
    pub application_blueprint:
        Option<std::collections::HashMap<String, ExportResponseApplicationBlueprint>>,
    /// Map of deployment name → Deployment object (deployment format only)
    #[serde(rename = "Deployment")]
    pub deployment: Option<std::collections::HashMap<String, ExportResponseDeployment>>,
    /// DeploymentEnvironment object (deployment and environments formats)
    #[serde(rename = "DeploymentEnvironment")]
    pub deployment_environment: Option<ExportResponseDeploymentEnvironment>,
    /// Map of path → DeploymentPath object (registered ensemble paths)
    #[serde(rename = "DeploymentPath")]
    pub deployment_path: Option<std::collections::HashMap<String, ExportResponseDeploymentPath>>,
    /// Map of blueprint name → DeploymentTemplate object
    #[serde(rename = "DeploymentTemplate")]
    pub deployment_template:
        Option<std::collections::HashMap<String, ExportResponseDeploymentTemplate>>,
    /// Map of template name → ResourceTemplate object (TOSCA node template)
    #[serde(rename = "ResourceTemplate")]
    pub resource_template:
        Option<std::collections::HashMap<String, ExportResponseResourceTemplate>>,
    /// Map of type name → ResourceType object (TOSCA node type)
    #[serde(rename = "ResourceType")]
    pub resource_type: Option<std::collections::HashMap<String, ExportResponseResourceType>>,
    /// Embedded deployment exports (present when include_all_deployments=true)
    pub deployments: Option<ExportResponseDeployments>,
    /// Latest commit hash observed by the export; clients can use this for cache validation
    pub latest_commit: Option<String>,
    /// Monotonic version assigned to this uncommitted write operation.
    pub queueid: Option<i64>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
#[serde_with::skip_serializing_none]
#[derive(
    Debug, Clone, PartialEq, Serialize, Deserialize, validator::Validate, oas3_gen_support::Default,
)]
pub struct ExportResponseApplicationBlueprint {
    #[serde(rename = "__typename")]
    #[validate(length(min = 1u64))]
    pub typename: String,
    #[serde(rename = "blueprintPath")]
    pub blueprint_path: Option<String>,
    #[serde(rename = "deploymentTemplates")]
    pub deployment_templates: Option<Vec<String>>,
    pub description: Option<String>,
    pub image: Option<String>,
    #[serde(rename = "livePreview")]
    pub live_preview: Option<String>,
    pub metadata: Option<std::collections::HashMap<String, serde_json::Value>>,
    #[validate(length(min = 1u64))]
    pub name: String,
    pub primary: Option<String>,
    #[serde(rename = "primaryDeploymentBlueprint")]
    pub primary_deployment_blueprint: Option<String>,
    #[serde(rename = "projectIcon")]
    pub project_icon: Option<String>,
    #[serde(rename = "projectPath")]
    pub project_path: Option<String>,
    #[serde(rename = "sourceCodeUrl")]
    pub source_code_url: Option<String>,
    pub title: Option<String>,
    pub visibility: Option<String>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct ExportResponseDeployment {
    #[serde(rename = "__typename")]
    pub typename: String,
    #[serde(rename = "deployTime")]
    pub deploy_time: Option<String>,
    #[serde(rename = "deploymentTemplate")]
    pub deployment_template: Option<String>,
    pub description: Option<String>,
    pub metadata: Option<std::collections::HashMap<String, serde_json::Value>>,
    pub name: String,
    pub packages: Option<std::collections::HashMap<String, serde_json::Value>>,
    pub primary: Option<String>,
    pub resources: Option<Vec<String>>,
    pub status: Option<ExportResponseStatus>,
    pub summary: Option<String>,
    pub title: Option<String>,
    pub url: Option<String>,
    pub visibility: Option<String>,
    pub workflow: Option<String>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct ExportResponseDeploymentEnvironment {
    pub connections: Option<std::collections::HashMap<String, ExportResponseResourceTemplate>>,
    pub instances: std::collections::HashMap<String, ExportResponseResourceTemplate>,
    pub name: Option<String>,
    pub primary_provider: Option<ExportResponseResourceTemplate>,
    pub repositories: Option<std::collections::HashMap<String, serde_json::Value>>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
#[serde_with::skip_serializing_none]
#[derive(
    Debug, Clone, PartialEq, Serialize, Deserialize, validator::Validate, oas3_gen_support::Default,
)]
pub struct ExportResponseDeploymentPath {
    #[serde(rename = "__typename")]
    #[validate(length(min = 1u64))]
    pub typename: String,
    pub description: Option<String>,
    #[validate(length(min = 1u64))]
    pub environment: String,
    pub incremental_deploy: bool,
    pub metadata: Option<std::collections::HashMap<String, serde_json::Value>>,
    #[validate(length(min = 1u64))]
    pub name: String,
    pub pipelines: Vec<std::collections::HashMap<String, serde_json::Value>>,
    pub project_id: Option<String>,
    pub title: Option<String>,
    pub visibility: Option<String>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
#[serde_with::skip_serializing_none]
#[derive(
    Debug, Clone, PartialEq, Serialize, Deserialize, validator::Validate, oas3_gen_support::Default,
)]
pub struct ExportResponseDeploymentTemplate {
    #[serde(rename = "ResourceTemplate")]
    pub resource_template:
        Option<std::collections::HashMap<String, ExportResponseResourceTemplate>>,
    #[serde(rename = "__typename")]
    #[validate(length(min = 1u64))]
    pub typename: String,
    pub blueprint: Option<String>,
    pub branch: Option<String>,
    pub cloud: Option<String>,
    #[serde(rename = "commitTime")]
    pub commit_time: Option<String>,
    pub description: Option<String>,
    #[serde(rename = "environmentVariableNames")]
    pub environment_variable_names: Option<Vec<String>>,
    pub metadata: Option<std::collections::HashMap<String, serde_json::Value>>,
    #[validate(length(min = 1u64))]
    pub name: String,
    pub primary: Option<String>,
    #[serde(rename = "resourceTemplates")]
    pub resource_templates: Option<Vec<String>>,
    pub slug: Option<String>,
    pub source: Option<String>,
    pub title: Option<String>,
    pub visibility: Option<String>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
/// Embedded deployment exports (present when include_all_deployments=true)
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
#[serde(untagged)]
pub enum ExportResponseDeployments {
    #[default]
    Array(Vec<ArrayKind>),
}
#[serde_with::skip_serializing_none]
#[derive(
    Debug, Clone, PartialEq, Serialize, Deserialize, validator::Validate, oas3_gen_support::Default,
)]
pub struct ExportResponseImportDef {
    #[validate(length(min = 1u64))]
    pub file: String,
    pub incomplete: Option<bool>,
    pub prefix: Option<String>,
    pub repository: Option<String>,
    pub url: Option<String>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct ExportResponseRequirement {
    #[serde(rename = "__typename")]
    pub typename: String,
    pub constraint: Box<ExportResponseRequirementConstraint>,
    pub description: Option<String>,
    #[serde(rename = "match")]
    pub r#match: Option<String>,
    pub metadata: Option<std::collections::HashMap<String, serde_json::Value>>,
    pub name: String,
    pub target: Option<String>,
    pub title: Option<String>,
    pub visibility: Option<String>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
#[serde_with::skip_serializing_none]
#[derive(
    Debug, Clone, PartialEq, Serialize, Deserialize, validator::Validate, oas3_gen_support::Default,
)]
pub struct ExportResponseRequirementConstraint {
    #[serde(rename = "__typename")]
    #[validate(length(min = 1u64))]
    pub typename: String,
    #[validate(length(min = 1u64))]
    pub badge: String,
    pub description: Option<String>,
    #[validate(length(min = 1u64))]
    pub icon: String,
    #[serde(rename = "inputsSchema")]
    pub inputs_schema: std::collections::HashMap<String, serde_json::Value>,
    #[serde(rename = "match")]
    pub r#match: Option<String>,
    pub max: i64,
    pub metadata: Option<std::collections::HashMap<String, serde_json::Value>>,
    pub min: i64,
    #[validate(length(min = 1u64))]
    pub name: String,
    pub node_filter: Option<std::collections::HashMap<String, serde_json::Value>>,
    #[serde(rename = "requirementsFilter")]
    #[validate(nested)]
    pub requirements_filter: Option<Vec<ExportResponseRequirementConstraint>>,
    #[serde(rename = "resourceType")]
    #[validate(length(min = 1u64))]
    pub resource_type: String,
    pub title: Option<String>,
    pub visibility: Option<String>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct ExportResponseResourceTemplate {
    #[serde(rename = "__typename")]
    pub typename: String,
    pub dependencies: Option<Vec<ExportResponseRequirement>>,
    pub description: Option<String>,
    pub directives: Option<Vec<String>>,
    pub imported: Option<String>,
    pub metadata: Option<std::collections::HashMap<String, serde_json::Value>>,
    pub name: String,
    pub properties: Option<Vec<std::collections::HashMap<String, serde_json::Value>>>,
    pub title: Option<String>,
    #[serde(rename = "type")]
    pub r#type: Option<String>,
    pub visibility: Option<String>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
#[serde_with::skip_serializing_none]
#[derive(
    Debug, Clone, PartialEq, Serialize, Deserialize, validator::Validate, oas3_gen_support::Default,
)]
pub struct ExportResponseResourceType {
    #[serde(rename = "__typename")]
    #[validate(length(min = 1u64))]
    pub typename: String,
    #[serde(rename = "_sourceinfo")]
    #[validate(nested)]
    pub sourceinfo: Option<ExportResponseImportDef>,
    pub badge: Option<String>,
    #[serde(rename = "computedPropertiesSchema")]
    pub computed_properties_schema: Option<std::collections::HashMap<String, serde_json::Value>>,
    pub description: Option<String>,
    pub details_url: Option<String>,
    pub directives: Option<Vec<String>>,
    pub extends: Vec<String>,
    pub icon: Option<String>,
    pub implementation_requirements: Option<Vec<String>>,
    pub implementations: Option<Vec<String>>,
    #[serde(rename = "inputsSchema")]
    pub inputs_schema: std::collections::HashMap<String, serde_json::Value>,
    pub metadata: Option<std::collections::HashMap<String, serde_json::Value>>,
    #[validate(length(min = 1u64))]
    pub name: String,
    #[serde(rename = "outputsSchema")]
    pub outputs_schema: Option<std::collections::HashMap<String, serde_json::Value>>,
    #[validate(nested)]
    pub requirements: Vec<ExportResponseRequirementConstraint>,
    pub title: Option<String>,
    pub visibility: Option<String>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize, oas3_gen_support::Default)]
pub enum ExportResponseStatus {
    #[serde(rename = "0")]
    #[default]
    Value0,
    #[serde(rename = "1")]
    Value1,
    #[serde(rename = "2")]
    Value2,
    #[serde(rename = "3")]
    Value3,
    #[serde(rename = "4")]
    Value4,
    #[serde(rename = "5")]
    Value5,
}
impl core::fmt::Display for ExportResponseStatus {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::Value0 => write!(f, "0"),
            Self::Value1 => write!(f, "1"),
            Self::Value2 => write!(f, "2"),
            Self::Value3 => write!(f, "3"),
            Self::Value4 => write!(f, "4"),
            Self::Value5 => write!(f, "5"),
        }
    }
}
impl core::str::FromStr for ExportResponseStatus {
    type Err = String;
    fn from_str(s: &str) -> core::result::Result<Self, Self::Err> {
        match s {
            "0" => Ok(Self::Value0),
            "1" => Ok(Self::Value1),
            "2" => Ok(Self::Value2),
            "3" => Ok(Self::Value3),
            "4" => Ok(Self::Value4),
            "5" => Ok(Self::Value5),
            _ => Err(format!(
                "unknown variant '{}', expected one of: {}",
                s, "0, 1, 2, 3, 4, 5"
            )),
        }
    }
}
/// Response body for ``GET /cloudmap/facets``.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct FacetsResult {
    /// One entry per distinct group value: array elements, object keys, or the scalar itself at the ``group_by`` path. Keys are strings; a non-string value appears as its canonical JSON text (minified, object keys sorted), so structured keys parse back to JSON. Empty when no selected record has the path.
    pub groups: std::collections::HashMap<String, FacetsResultFacetGroup>,
    /// Names the dimensions of a ``/cloudmap/facets`` result, echoing
    /// the request parameters that produced it in normalized form.
    pub meta: FacetsResultFacetsMeta,
    /// Distinct records matched by the selection parameters, whether or not they produced a group value. The denominator for the counts in ``groups``, which may overlap and need not sum to it.
    pub total: i64,
}
/// One group in a ``/cloudmap/facets`` response.
#[serde_with::skip_serializing_none]
#[derive(
    Debug, Clone, PartialEq, Serialize, Deserialize, validator::Validate, oas3_gen_support::Default,
)]
pub struct FacetsResultFacetGroup {
    /// Number of distinct selected records whose ``group_by`` path yielded this group's key. Independent of any facet columns.
    #[validate(range(min = 0i64))]
    pub count: i64,
    /// Omitted when the request had no ``facet`` parameters. Aligned by index with ``meta.facets``: the i-th map is the i-th column's breakdown for this group, mapping facet value to distinct-record count -- present for every requested column, even when empty. Composite-column keys are the canonical JSON array of the member values in path order. Counts need not sum to ``count``: a record carrying several values is counted under each, and a record with none contributes to ``count`` only.
    pub facets: Option<Vec<std::collections::HashMap<String, i64>>>,
}
/// Names the dimensions of a ``/cloudmap/facets`` result, echoing
/// the request parameters that produced it in normalized form.
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
#[serde(default)]
pub struct FacetsResultFacetsMeta {
    /// One entry per facet column, in request order: the normalized member paths of that column. A simple facet is a one-element entry; a comma-composed facet lists each member. Empty when the request did not facet.
    pub facets: Option<Vec<Vec<String>>>,
    /// Normalized JSON Pointer of the grouping path -- what the keys of ``groups`` are values of.
    pub group_by: String,
    /// True when subtype-closure rollup was actually applied: a ``type`` column was present and ``subtypes`` was not disabled. Group and facet buckets then overlap up the type hierarchy instead of partitioning the records.
    #[default(Some(false))]
    pub subtypes: Option<bool>,
}
/// Count the selected records grouped by the value at ``group_by``, with an optional per-group breakdown for each ``facet`` column. Record selection (``kind`` / ``type`` / ``filter``) works exactly as on ``GET /cloudmap``; the response carries counts only, no records.
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct GetCloudmapFacetsRequest {
    #[validate(nested)]
    pub query: GetCloudmapFacetsRequestQuery,
}
impl GetCloudmapFacetsRequest {}
#[derive(Debug, Clone, PartialEq, Deserialize, validator::Validate, oas3_gen_support::Default)]
pub struct GetCloudmapFacetsRequestQuery {
    /// Project ID for authorization and cache key scoping
    pub auth_project: Option<String>,
    /// Commit hash used to validate the cache entry
    pub latest_commit: Option<String>,
    /// Git branch name
    pub branch: Option<String>,
    /// Path of the cloudmap file inside the repo; defaults to ``cloudmap.yaml``.
    pub cloudmap_path: Option<String>,
    /// Top-level CloudMap section to select records from; if omitted every section is considered.
    pub kind: Option<String>,
    /// Fully-qualified type name; select only records whose ``type`` declares this type or a type that (transitively) ``extends`` it, per the ``types`` section of the CloudMap.
    #[serde(rename = "type")]
    pub r#type: Option<String>,
    /// Filter on the contents of each record: a JSON Pointer path (RFC 6901) with an optional operator and value. Repeatable: (they AND together),
    ///
    /// ```text
    /// /metadata/topics=library                       equals, or array-contains
    /// /metadata/topics=["documentation","library"]   exact array match
    /// /metadata/homepage_url^=https://unfurl.cloud/  prefix (strings only)
    /// /metadata/discovery                            the path exists
    /// ```
    ///
    /// ``=`` matches when the value at the path equals the value or is an array containing it; an array literal is an exact match instead -- same elements, same order -- and an object literal is rejected. ``^=`` needs a string at the path, or a string element of an array there, that starts with the value; a number never matches a prefix. A path with no operator at all matches when the path resolves, counting a ``null`` or an empty array or object as present.
    ///
    /// Values are read as JSON: ``true``, ``false``, ``null`` and numbers keep their type, an array has to be valid JSON (``["a","b"]``, not ``[a,b]``), and anything else is a string. Wrap a value in double quotes to force a string (``="42"``). Wildcards in the path aren't supported yet. Combines with the other selection parameters: a record has to match all of them.
    pub filter: Option<Vec<String>>,
    /// JSON Pointer path (RFC 6901) to group the selected records by; a path without a leading ``/`` gets one prepended. The value found at the path becomes the group: each element of an array, each key of an object, or the scalar itself. A record without the path lands in no group (but still counts toward ``total``).
    #[validate(length(min = 1u64))]
    pub group_by: String,
    /// Repeatable: each occurrence adds one facet column, a value-to-count breakdown within every group. A comma-separated list of paths in a single occurrence composes one column keyed by the tuple of values, counting their per-record combinations -- pairing is per record, not per array element, so to correlate fields of the same array element, facet on their parent (the whole element becomes the value) rather than on the fields separately. A record missing any of a column's paths is absent from that column.
    pub facet: Option<Vec<String>>,
    /// When true (the default), a column whose path is exactly ``type`` also counts each record under every ancestor of its declared types, per the ``types`` section's ``extends`` graph -- so a base type's bucket includes its subtypes' records, mirroring what the ``type`` selection parameter would match. Buckets then overlap and do not sum to the record count. Pass false to count exact declared names only. Has no effect on other paths.
    #[default(Some(true))]
    pub subtypes: Option<bool>,
}
/// Response types for GetCloudmapFacetsResponse
#[derive(Debug, Clone)]
pub enum GetCloudmapFacetsResponse {
    ///200: Group and facet counts for the selected records
    Ok(FacetsResult),
    ///422: Validation error
    UnprocessableEntity(ValidationError),
    ///default: Unknown response
    Unknown,
}
impl IntoResponse for GetCloudmapFacetsResponse {
    fn into_response(self) -> axum::response::Response {
        match self {
            Self::Ok(data) => (http::StatusCode::OK, axum::Json(data)).into_response(),
            Self::UnprocessableEntity(data) => {
                (http::StatusCode::UNPROCESSABLE_ENTITY, axum::Json(data)).into_response()
            }
            Self::Unknown => http::StatusCode::OK.into_response(),
        }
    }
}
/// Return the CloudMap document under ``result`` — the raw CloudMap, or the subset selected by ``kind`` / ``key`` / ``type`` / ``filter``.
///
/// Two further keys appear only when the request asked for what they carry, so a client can tell 'you didn't ask' from 'there is none': ``followed`` holds the records reached by walking the graph, and is present only when ``follow`` > 0 with a ``key``; ``next_page_token`` is the cursor for the next page, and is present only on a ``limit`` request that has one.
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct GetCloudmapRequest {
    pub query: GetCloudmapRequestQuery,
}
impl GetCloudmapRequest {}
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
pub struct GetCloudmapRequestQuery {
    /// Project ID for authorization and cache key scoping
    pub auth_project: Option<String>,
    /// Commit hash used to validate the cache entry
    pub latest_commit: Option<String>,
    /// Git branch name
    pub branch: Option<String>,
    /// Path of the cloudmap file inside the repo; defaults to ``cloudmap.yaml``.
    pub cloudmap_path: Option<String>,
    /// Top-level CloudMap section to select records from; if omitted every section is considered.
    pub kind: Option<String>,
    /// Fully-qualified type name; select only records whose ``type`` declares this type or a type that (transitively) ``extends`` it, per the ``types`` section of the CloudMap.
    #[serde(rename = "type")]
    pub r#type: Option<String>,
    /// Filter on the contents of each record: a JSON Pointer path (RFC 6901) with an optional operator and value. Repeatable: (they AND together),
    ///
    /// ```text
    /// /metadata/topics=library                       equals, or array-contains
    /// /metadata/topics=["documentation","library"]   exact array match
    /// /metadata/homepage_url^=https://unfurl.cloud/  prefix (strings only)
    /// /metadata/discovery                            the path exists
    /// ```
    ///
    /// ``=`` matches when the value at the path equals the value or is an array containing it; an array literal is an exact match instead -- same elements, same order -- and an object literal is rejected. ``^=`` needs a string at the path, or a string element of an array there, that starts with the value; a number never matches a prefix. A path with no operator at all matches when the path resolves, counting a ``null`` or an empty array or object as present.
    ///
    /// Values are read as JSON: ``true``, ``false``, ``null`` and numbers keep their type, an array has to be valid JSON (``["a","b"]``, not ``[a,b]``), and anything else is a string. Wrap a value in double quotes to force a string (``="42"``). Wildcards in the path aren't supported yet. Combines with the other selection parameters: a record has to match all of them.
    pub filter: Option<Vec<String>>,
    /// Record key (URL) within the selected ``kind`` section; ignored when ``kind`` is omitted.
    pub key: Option<String>,
    /// If > 0, walk the CloudMap graph outward from every record the query selected and return what it reaches under ``followed``. Records already in the result are never repeated under ``followed``, and the value caps how many are returned. Follow doesn't know about paging; a record reachable from two pages appears on both unless ``exclude`` rules it out.
    #[default(Some(0i64))]
    pub follow: Option<i64>,
    /// When set, return only records whose ``unfurl.server.version`` is greater than this value, including records deleted since then -- those come back carrying ``unfurl.server.deleted: true`` so a client catching up can drop them, which it could not otherwise learn (a deleted record simply stops being returned). Requires the rust git-sync backend; ignored by the Python YAML fallback, which reports neither versions nor deletions.
    pub since_version: Option<i64>,
    /// Comma-separated list of record primary-key ids (``unfurl.server.id`` values) to exclude from the response. Used by clients with a warm cache to avoid re-receiving records they already hold during a ``follow`` walk. Requires the rust git-sync backend; ignored by the Python YAML fallback.
    pub exclude: Option<String>,
    /// Comma-separated list of JSON Pointer paths (RFC 6901, e.g. ``/type,/metadata/title``); when set, each record in the response (both elements of the pair) is reduced to only the selected properties, keeping their nested structure. Paths without a leading ``/`` get one prepended. The special entry ``$key`` adds the record's key to the reduced record under ``"$key"``. Paths that don't resolve are omitted.
    pub select: Option<String>,
    /// Return at most this many records, delivered under ``result`` with a ``next_page_token`` key alongside when more remain. ``next_page_token`` is absent on the last page; pass it back as ``page_token`` to get the next one. Records are ordered by section then key, so a walk is stable across writes. Cannot be combined with ``key`` (which selects a single record); ``follow`` and ``exclude`` have no effect on a paged request, whose ``follow`` half is always empty. Combines with ``kind``, ``type``, ``filter`` and ``select``, which all apply before the page is cut.
    pub limit: Option<i64>,
    /// Opaque cursor from a previous paged response's ``next_page_token``: resume after the record it names. Only meaningful together with ``limit``. A token stays valid when the record it names is deleted.
    pub page_token: Option<String>,
}
/// Response types for GetCloudmapResponse
#[derive(Debug, Clone)]
pub enum GetCloudmapResponse {
    ///200: The filtered CloudMap document, plus follow/paging keys when requested
    Ok(CloudMapResult),
    ///422: Validation error
    UnprocessableEntity(ValidationError),
    ///default: Unknown response
    Unknown,
}
impl IntoResponse for GetCloudmapResponse {
    fn into_response(self) -> axum::response::Response {
        match self {
            Self::Ok(data) => (http::StatusCode::OK, axum::Json(data)).into_response(),
            Self::UnprocessableEntity(data) => {
                (http::StatusCode::UNPROCESSABLE_ENTITY, axum::Json(data)).into_response()
            }
            Self::Unknown => http::StatusCode::OK.into_response(),
        }
    }
}
/// Export an ensemble or service template in a JSON format suitable for the frontend. Supports 'deployment', 'blueprint', and 'environments' formats.
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct GetExportRequest {
    pub query: GetExportRequestQuery,
}
impl GetExportRequest {}
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
pub struct GetExportRequestQuery {
    /// Project ID for authorization and cache key scoping
    pub auth_project: Option<String>,
    /// Commit hash used to validate the cache entry
    pub latest_commit: Option<String>,
    /// Git branch name
    pub branch: Option<String>,
    /// Setting this enables asynchronous writes
    pub queueid: Option<i64>,
    /// Pretty-print the JSON response
    #[default(Some(false))]
    pub pretty: Option<bool>,
    /// Git username (alternative to X-Git-Credentials header)
    pub username: Option<String>,
    /// Repository visibility
    pub visibility: Option<String>,
    /// Export format
    #[default(Some(Default::default()))]
    pub format: Option<GetExportRequestQueryFormat>,
    /// Path to the deployment within the project
    pub deployment_path: Option<String>,
    /// Environment name (used with 'environments' format)
    pub environment: Option<String>,
    /// Include all deployment exports embedded in the response
    #[default(Some(String::new()))]
    pub include_all_deployments: Option<String>,
    /// Return any cache hit without checking if it's out of date.
    pub stale: Option<String>,
}
/// Export format
#[derive(Debug, Clone, PartialEq, Eq, Hash, Deserialize, Serialize, oas3_gen_support::Default)]
pub enum GetExportRequestQueryFormat {
    #[serde(rename = "deployment")]
    #[default]
    Deployment,
    #[serde(rename = "blueprint")]
    Blueprint,
    #[serde(rename = "environments")]
    Environments,
}
impl core::fmt::Display for GetExportRequestQueryFormat {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::Deployment => write!(f, "deployment"),
            Self::Blueprint => write!(f, "blueprint"),
            Self::Environments => write!(f, "environments"),
        }
    }
}
impl core::str::FromStr for GetExportRequestQueryFormat {
    type Err = String;
    fn from_str(s: &str) -> core::result::Result<Self, Self::Err> {
        match s {
            "deployment" => Ok(Self::Deployment),
            "blueprint" => Ok(Self::Blueprint),
            "environments" => Ok(Self::Environments),
            _ => Err(format!(
                "unknown variant '{}', expected one of: {}",
                s, "deployment, blueprint, environments"
            )),
        }
    }
}
/// Return the CloudMap dependency graph as JSON, optionally filtered to a single URL.
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct GetGraphRequest {
    pub query: GetGraphRequestQuery,
}
impl GetGraphRequest {}
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
pub struct GetGraphRequestQuery {
    /// Project ID for authorization and cache key scoping
    pub auth_project: Option<String>,
    /// Commit hash used to validate the cache entry
    pub latest_commit: Option<String>,
    /// Git branch name
    pub branch: Option<String>,
    /// Path of the cloudmap file inside the repo; defaults to ``cloudmap.yaml``.
    pub cloudmap_path: Option<String>,
    /// Optional artifact or instantiation URL to filter the graph to
    pub url: Option<String>,
}
/// Response types for GetGraphResponse
#[derive(Debug, Clone)]
pub enum GetGraphResponse {
    ///200: CloudMap dependency graph as JSON
    Ok(CloudMapResponse),
    ///422: Validation error
    UnprocessableEntity(ValidationError),
    ///default: Unknown response
    Unknown,
}
impl IntoResponse for GetGraphResponse {
    fn into_response(self) -> axum::response::Response {
        match self {
            Self::Ok(data) => (http::StatusCode::OK, axum::Json(data)).into_response(),
            Self::UnprocessableEntity(data) => {
                (http::StatusCode::UNPROCESSABLE_ENTITY, axum::Json(data)).into_response()
            }
            Self::Unknown => http::StatusCode::OK.into_response(),
        }
    }
}
/// Health check
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct GetHealthRequest {}
impl GetHealthRequest {}
/// Response types for GetHealthResponse
#[derive(Debug, Clone)]
pub enum GetHealthResponse {
    ///200: Successful response
    Ok,
    ///default: Unknown response
    Unknown,
}
impl IntoResponse for GetHealthResponse {
    fn into_response(self) -> axum::response::Response {
        match self {
            Self::Ok => http::StatusCode::OK.into_response(),
            Self::Unknown => http::StatusCode::OK.into_response(),
        }
    }
}
/// Export all available TOSCA resource types, optionally augmented from a CloudMap project.
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct GetTypesRequest {
    pub query: GetTypesRequestQuery,
}
impl GetTypesRequest {}
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
pub struct GetTypesRequestQuery {
    /// Project ID for authorization and cache key scoping
    pub auth_project: Option<String>,
    /// Commit hash used to validate the cache entry
    pub latest_commit: Option<String>,
    /// Git branch name
    pub branch: Option<String>,
    /// Setting this enables asynchronous writes
    pub queueid: Option<i64>,
    /// Pretty-print the JSON response
    #[default(Some(false))]
    pub pretty: Option<bool>,
    /// Git username (alternative to X-Git-Credentials header)
    pub username: Option<String>,
    /// Repository visibility
    pub visibility: Option<String>,
    /// Filename used as template context
    #[default(Some(String::new()))]
    pub file: Option<String>,
    /// CloudMap project ID to merge types from, e.g. 'onecommons/cloudmap'
    pub cloudmap: Option<String>,
}
/// Response types for GetTypesResponse
#[derive(Debug, Clone)]
pub enum GetTypesResponse {
    ///200: GraphQL-style JSON database of TOSCA types
    Ok(ExportResponse),
    ///304: Not Modified (ETag matched)
    NotModified,
    ///401: Unauthorized
    Unauthorized(HTTPError),
    ///422: Validation error
    UnprocessableEntity(ValidationError),
    ///500: Internal error
    InternalServerError(HTTPError),
    ///default: Unknown response
    Unknown,
}
impl IntoResponse for GetTypesResponse {
    fn into_response(self) -> axum::response::Response {
        match self {
            Self::Ok(data) => (http::StatusCode::OK, axum::Json(data)).into_response(),
            Self::NotModified => http::StatusCode::NOT_MODIFIED.into_response(),
            Self::Unauthorized(data) => {
                (http::StatusCode::UNAUTHORIZED, axum::Json(data)).into_response()
            }
            Self::UnprocessableEntity(data) => {
                (http::StatusCode::UNPROCESSABLE_ENTITY, axum::Json(data)).into_response()
            }
            Self::InternalServerError(data) => {
                (http::StatusCode::INTERNAL_SERVER_ERROR, axum::Json(data)).into_response()
            }
            Self::Unknown => http::StatusCode::OK.into_response(),
        }
    }
}
/// Server version
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct GetVersionRequest {}
impl GetVersionRequest {}
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct HTTPError {
    pub detail: Option<serde_json::Value>,
    pub message: Option<String>,
}
/// JSON body for /create_ensemble, /update_ensemble, and /create_provider.
///
/// Extends PatchEnvironmentBody with ensemble-specific fields.
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
#[serde(default)]
pub struct PatchEnsembleBody {
    /// Remote blueprint URL to clone when creating an ensemble
    pub blueprint_url: Option<String>,
    /// Target branch
    #[default(Some("main".to_string()))]
    pub branch: Option<String>,
    /// URL for cloud variables used for vault secret encryption
    pub cloud_vars_url: Option<String>,
    /// Git commit message
    pub commit_msg: Option<String>,
    /// Name of the deployment blueprint to use when creating an ensemble
    pub deployment_blueprint: Option<String>,
    /// Path for the deployment within the project
    pub deployment_path: Option<String>,
    /// Deployment environment name
    pub environment: Option<String>,
    /// Latest known commit hash for optimistic concurrency checks
    pub latest_commit: Option<String>,
    /// List of patch operations describing the changes to apply
    pub patch: Vec<std::collections::HashMap<String, serde_json::Value>>,
    /// Git personal access token or password
    pub private_token: Option<String>,
    /// Setting this enables asynchronous writes
    pub queueid: Option<i64>,
    /// Git username for pushing the commit
    pub username: Option<String>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
/// JSON body for /delete_deployment, /update_environment, and /delete_environment.
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
#[serde(default)]
pub struct PatchEnvironmentBody {
    /// Target branch
    #[default(Some("main".to_string()))]
    pub branch: Option<String>,
    /// Git commit message
    pub commit_msg: Option<String>,
    /// Latest known commit hash for optimistic concurrency checks
    pub latest_commit: Option<String>,
    /// List of patch operations describing the changes to apply
    pub patch: Vec<std::collections::HashMap<String, serde_json::Value>>,
    /// Git personal access token or password
    pub private_token: Option<String>,
    /// Setting this enables asynchronous writes
    pub queueid: Option<i64>,
    /// Git username for pushing the commit
    pub username: Option<String>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
/// Response from write endpoints.
///
/// Either ``commit`` (a git commit hash) or ``queueid`` (a monotonic
/// version counter) used as a optimistic-concurrency
/// token the client should echo back on its next write.
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct PatchResponse {
    /// Per-record results for batch CloudMap writes. Empty for single-record endpoints. In non-atomic mode, contains the records that committed even though the batch reported a conflict.
    pub applied: Option<Vec<PatchResponseAppliedRecord>>,
    /// The repository's commit hash after the request was handled: the new commit when one was made, otherwise the unchanged HEAD (which the client can echo back as ``latest_commit``). Null only when the repository has no commits at all.
    pub commit: Option<String>,
    /// Monotonic version assigned to this uncommitted write operation.
    pub queueid: Option<i64>,
}
/// One record successfully applied during a CloudMap batch write.
///
/// Returned in :attr:`PatchResponse.applied`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct PatchResponseAppliedRecord {
    /// Record key within the section.
    pub key: String,
    /// CloudMap section, e.g. ``artifacts``.
    pub section: String,
    /// ``unfurl.server.version`` stamped on the row by this write.
    pub version: i64,
}
/// Used by the Rust proxy to forward a batch of write requests that share the same branch and latest_commit.  Each request in the ``requests`` list is applied in order; a single push is performed at the end.
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct PostBatchPatchRequest {
    pub query: PostBatchPatchRequestQuery,
    pub body: Option<BatchPatchBody>,
}
impl PostBatchPatchRequest {}
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
pub struct PostBatchPatchRequestQuery {
    /// Project ID for authorization and cache key scoping
    pub auth_project: Option<String>,
}
/// Response types for PostBatchPatchResponse
#[derive(Debug, Clone)]
pub enum PostBatchPatchResponse {
    ///200: Successful response
    Ok(PatchResponse),
    ///401: Unauthorized
    Unauthorized(HTTPError),
    ///409: Conflict (repository at wrong revision)
    Conflict(HTTPError),
    ///422: Validation error
    UnprocessableEntity(ValidationError),
    ///500: Internal error
    InternalServerError(HTTPError),
    ///default: Unknown response
    Unknown,
}
impl IntoResponse for PostBatchPatchResponse {
    fn into_response(self) -> axum::response::Response {
        match self {
            Self::Ok(data) => (http::StatusCode::OK, axum::Json(data)).into_response(),
            Self::Unauthorized(data) => {
                (http::StatusCode::UNAUTHORIZED, axum::Json(data)).into_response()
            }
            Self::Conflict(data) => (http::StatusCode::CONFLICT, axum::Json(data)).into_response(),
            Self::UnprocessableEntity(data) => {
                (http::StatusCode::UNPROCESSABLE_ENTITY, axum::Json(data)).into_response()
            }
            Self::InternalServerError(data) => {
                (http::StatusCode::INTERNAL_SERVER_ERROR, axum::Json(data)).into_response()
            }
            Self::Unknown => http::StatusCode::OK.into_response(),
        }
    }
}
/// Clear cache and cloned files for a project
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct PostClearProjectFileCacheRequest {
    pub query: PostClearProjectFileCacheRequestQuery,
}
impl PostClearProjectFileCacheRequest {}
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
pub struct PostClearProjectFileCacheRequestQuery {
    /// Project ID for authorization and cache key scoping
    pub auth_project: Option<String>,
}
/// Request body for ``POST /cloudmap``.
///
/// A CloudMap document portion plus request-only envelope/control fields
/// (``atomic`` / ``latest_commit`` / ``cloudmap_path`` / ``username``
/// / ``private_token`` / ``commit_msg``).
///
/// Declared as a flat object — the cloudmap section maps and the
/// envelope keys live side-by-side at the top level. The endpoint
/// splits them apart by name; the JSON-Schema validation only runs on
/// the cloudmap-document subset.
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
pub struct PostCloudmapRequest {
    #[serde(rename = "apiVersion")]
    pub api_version: Option<String>,
    /// Tangible object that instantiates services or other artifacts. Artifact ID is either a package URL (see <https://github.com/package-url/purl-spec>) or repository URL with path.
    pub artifacts: Option<std::collections::HashMap<String, Box<CloudmapArtifact>>>,
    /// When ``true`` (default), the batch is all-or-nothing: any per-record OCC failure rolls everything back. When ``false``, per-record failures are skipped and the rest of the batch commits; the 409 body lists ``applied`` and ``failed`` arrays. Honoured by the rust local handler only — the Python YAML fallback is implicitly atomic.
    pub atomic: Option<bool>,
    /// Branch to write to; defaults to ``main``.
    pub branch: Option<String>,
    /// Path of the cloudmap file inside the repo.
    pub cloudmap_path: Option<String>,
    /// Whether to commit the write to git. If Commit = true is sent with a body that carries no records at all the handler then commits whatever is already pending.
    pub commit: Option<bool>,
    /// Commit message for the local commit; falls back to a generated default.
    pub commit_msg: Option<String>,
    /// Components that are produced or consumed by artifacts and services. Components describe relationships (references, instantiates, dependencies) and are identified by URL or label.
    pub components: Option<std::collections::HashMap<String, Box<CloudmapComponent>>>,
    /// Build and deployment information for artifacts and services. Keys are URLs.
    pub instantiations: Option<std::collections::HashMap<String, Box<CloudmapInstantiation>>>,
    pub kind: Option<String>,
    /// Last commit oid the client observed. Forwarded to git-level OCC checks.
    pub latest_commit: Option<String>,
    pub metadata: Option<std::collections::HashMap<String, serde_json::Value>>,
    /// Git credential token; can also be sent via the ``X-Git-Credentials`` header.
    pub private_token: Option<String>,
    /// Git repositories. Keys are URLs that start with git://
    pub repositories: Option<std::collections::HashMap<String, CloudmapRepository>>,
    /// Instances of services.
    pub services: Option<std::collections::HashMap<String, Box<CloudmapService>>>,
    /// Type definitions for artifacts, services, software, and capabilities.
    pub types: Option<std::collections::HashMap<String, CloudmapType>>,
    /// Git credential username; can also be sent via the ``X-Git-Credentials`` header.
    pub username: Option<String>,
    /// Additional properties not defined in the schema.
    #[serde(flatten)]
    pub additional_properties: std::collections::HashMap<String, serde_json::Value>,
}
/// Apply a batch of add / update / delete operations to ``cloudmap.yaml``. Top-level keys split between an envelope (``latest_commit`` / ``cloudmap_path`` / ``username`` / ``private_token`` / ``commit_msg``) and the cloudmap sections (``repositories``, ``artifacts``, ``services``, ``instantiations``, ``types``).
///
/// Each section maps record keys to a JSON object that schema-validates as the corresponding cloudmap entity. To delete a record, send the object with ``unfurl.server.deleted: true``.
///
/// The body is validated against ``docs/cloudmap-schema.json`` (a 422 is returned on schema violation). On success the file is committed locally (no push) and the new commit oid is returned.
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct PostCloudmapRequestParams {
    pub query: PostCloudmapRequestParamsQuery,
    pub body: Option<PostCloudmapRequest>,
}
impl PostCloudmapRequestParams {}
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
pub struct PostCloudmapRequestParamsQuery {
    /// Project ID for authorization and cache key scoping
    pub auth_project: Option<String>,
}
/// Response types for PostCloudmapResponse
#[derive(Debug, Clone)]
pub enum PostCloudmapResponse {
    ///200: commit and list of applied changes (mirrors the rust handler's per-record response)
    Ok(PatchResponse),
    ///422: Validation error
    UnprocessableEntity(ValidationError),
    ///default: Unknown response
    Unknown,
}
impl IntoResponse for PostCloudmapResponse {
    fn into_response(self) -> axum::response::Response {
        match self {
            Self::Ok(data) => (http::StatusCode::OK, axum::Json(data)).into_response(),
            Self::UnprocessableEntity(data) => {
                (http::StatusCode::UNPROCESSABLE_ENTITY, axum::Json(data)).into_response()
            }
            Self::Unknown => http::StatusCode::OK.into_response(),
        }
    }
}
/// Create a new ensemble
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct PostCreateEnsembleRequest {
    pub query: PostCreateEnsembleRequestQuery,
    pub body: Option<PatchEnsembleBody>,
}
impl PostCreateEnsembleRequest {}
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
pub struct PostCreateEnsembleRequestQuery {
    /// Project ID for authorization and cache key scoping
    pub auth_project: Option<String>,
}
/// Create a cloud provider and its associated ensemble
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct PostCreateProviderRequest {
    pub query: PostCreateProviderRequestQuery,
    pub body: Option<PatchEnsembleBody>,
}
impl PostCreateProviderRequest {}
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
pub struct PostCreateProviderRequestQuery {
    /// Project ID for authorization and cache key scoping
    pub auth_project: Option<String>,
}
/// Delete a deployment
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct PostDeleteDeploymentRequest {
    pub query: PostDeleteDeploymentRequestQuery,
    pub body: Option<PatchEnvironmentBody>,
}
impl PostDeleteDeploymentRequest {}
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
pub struct PostDeleteDeploymentRequestQuery {
    /// Project ID for authorization and cache key scoping
    pub auth_project: Option<String>,
}
/// Delete a deployment environment
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct PostDeleteEnvironmentRequest {
    pub query: PostDeleteEnvironmentRequestQuery,
    pub body: Option<PatchEnvironmentBody>,
}
impl PostDeleteEnvironmentRequest {}
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
pub struct PostDeleteEnvironmentRequestQuery {
    /// Project ID for authorization and cache key scoping
    pub auth_project: Option<String>,
}
/// Clear all cache entries (admin only)
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct PostEmptyCacheRequest {
    #[validate(nested)]
    pub query: PostEmptyCacheRequestQuery,
}
impl PostEmptyCacheRequest {}
#[derive(Debug, Clone, PartialEq, Deserialize, validator::Validate, oas3_gen_support::Default)]
pub struct PostEmptyCacheRequestQuery {
    /// Must equal the UNFURL_SERVER_ADMIN_PROJECT environment variable
    #[validate(length(min = 1u64))]
    pub auth_project: String,
    /// Cache key prefix to clear; defaults to the server-configured prefix
    pub cache_prefix: Option<String>,
}
/// Response types for PostEmptyCacheResponse
#[derive(Debug, Clone)]
pub enum PostEmptyCacheResponse {
    ///200: Successful response
    Ok,
    ///422: Validation error
    UnprocessableEntity(ValidationError),
    ///default: Unknown response
    Unknown,
}
impl IntoResponse for PostEmptyCacheResponse {
    fn into_response(self) -> axum::response::Response {
        match self {
            Self::Ok => http::StatusCode::OK.into_response(),
            Self::UnprocessableEntity(data) => {
                (http::StatusCode::UNPROCESSABLE_ENTITY, axum::Json(data)).into_response()
            }
            Self::Unknown => http::StatusCode::OK.into_response(),
        }
    }
}
/// Populate export cache for a project file
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct PostPopulateCacheRequest {
    #[validate(nested)]
    pub query: PostPopulateCacheRequestQuery,
}
impl PostPopulateCacheRequest {}
#[derive(Debug, Clone, PartialEq, Deserialize, validator::Validate, oas3_gen_support::Default)]
pub struct PostPopulateCacheRequestQuery {
    /// Project ID for authorization and cache key scoping
    pub auth_project: Option<String>,
    /// Commit hash used to validate the cache entry
    pub latest_commit: Option<String>,
    /// Git branch name
    pub branch: Option<String>,
    /// File path relative to the project root
    #[validate(length(min = 1u64))]
    pub path: String,
    /// If truthy (not '0' or 'false'), delete the cache entry instead of populating it
    pub removed: Option<String>,
    /// Repository visibility; private repositories are not cloned automatically
    pub visibility: Option<String>,
}
/// Update an existing ensemble
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct PostUpdateEnsembleRequest {
    pub query: PostUpdateEnsembleRequestQuery,
    pub body: Option<PatchEnsembleBody>,
}
impl PostUpdateEnsembleRequest {}
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
pub struct PostUpdateEnsembleRequestQuery {
    /// Project ID for authorization and cache key scoping
    pub auth_project: Option<String>,
}
/// Update a deployment environment
#[derive(Debug, Clone, validator::Validate, oas3_gen_support::Default)]
pub struct PostUpdateEnvironmentRequest {
    pub query: PostUpdateEnvironmentRequestQuery,
    pub body: Option<PatchEnvironmentBody>,
}
impl PostUpdateEnvironmentRequest {}
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize, oas3_gen_support::Default)]
pub struct PostUpdateEnvironmentRequestQuery {
    /// Project ID for authorization and cache key scoping
    pub auth_project: Option<String>,
}
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct ValidationError {
    pub detail: Option<ValidationErrorDetail>,
    pub message: Option<String>,
}
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct ValidationErrorDetail {
    #[serde(rename = "<location>")]
    pub location: Option<ValidationErrorDetaillocation>,
}
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, oas3_gen_support::Default)]
pub struct ValidationErrorDetaillocation {
    #[serde(rename = "<field_name>")]
    pub field_name: Option<Vec<String>>,
}
