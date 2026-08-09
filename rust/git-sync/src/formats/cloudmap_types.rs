// AUTO-GENERATED from unfurl/cloudmap/cloudmap-schema.json by typify.
// Do not edit by hand — change the JSON Schema and rebuild.
//
// Used by `crate::formats::cloudmap::CloudMapFormat` so a schema
// change that renames or removes a field surfaces as a compile
// error here rather than silently producing empty follow-edges.
#![allow(unused_imports, dead_code, clippy::all)]
#[doc = r" Error types."]
pub mod error {
    #[doc = r" Error from a `TryFrom` or `FromStr` implementation."]
    pub struct ConversionError(::std::borrow::Cow<'static, str>);
    impl ::std::error::Error for ConversionError {}
    impl ::std::fmt::Display for ConversionError {
        fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> Result<(), ::std::fmt::Error> {
            ::std::fmt::Display::fmt(&self.0, f)
        }
    }
    impl ::std::fmt::Debug for ConversionError {
        fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> Result<(), ::std::fmt::Error> {
            ::std::fmt::Debug::fmt(&self.0, f)
        }
    }
    impl From<&'static str> for ConversionError {
        fn from(value: &'static str) -> Self {
            Self(value.into())
        }
    }
    impl From<String> for ConversionError {
        fn from(value: String) -> Self {
            Self(value.into())
        }
    }
}
#[doc = "`Artifact`"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"title\": \"Artifact\","]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"allOf\": ["]
#[doc = "    {"]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"properties\": {"]
#[doc = "        \"digest\": {"]
#[doc = "          \"description\": \"Cryptographic digest of the artifact.\","]
#[doc = "          \"type\": \"string\""]
#[doc = "        },"]
#[doc = "        \"discovery\": {"]
#[doc = "          \"$ref\": \"#/definitions/discovery\""]
#[doc = "        },"]
#[doc = "        \"immutable\": {"]
#[doc = "          \"description\": \"Indicates whether the artifact identifier refers to an artifact that will not change.\","]
#[doc = "          \"type\": \"boolean\""]
#[doc = "        },"]
#[doc = "        \"instantiated_by\": {"]
#[doc = "          \"description\": \"URLs referencing instantiations that created or validated this artifact.\","]
#[doc = "          \"$ref\": \"#/definitions/typedURLs\""]
#[doc = "        },"]
#[doc = "        \"metadata\": {"]
#[doc = "          \"description\": \"Human-readable metadata about the artifact.\","]
#[doc = "          \"allOf\": ["]
#[doc = "            {"]
#[doc = "              \"$ref\": \"#/definitions/metadata\""]
#[doc = "            },"]
#[doc = "            {"]
#[doc = "              \"type\": \"object\","]
#[doc = "              \"properties\": {"]
#[doc = "                \"platforms\": {"]
#[doc = "                  \"description\": \"List of platforms this artifact supports.\","]
#[doc = "                  \"type\": \"array\","]
#[doc = "                  \"items\": {"]
#[doc = "                    \"type\": \"object\","]
#[doc = "                    \"properties\": {"]
#[doc = "                      \"architecture\": {"]
#[doc = "                        \"description\": \"CPU architecture (e.g., amd64, arm64).\","]
#[doc = "                        \"type\": \"string\""]
#[doc = "                      },"]
#[doc = "                      \"os\": {"]
#[doc = "                        \"description\": \"Operating system (e.g., linux, windows).\","]
#[doc = "                        \"type\": \"string\""]
#[doc = "                      }"]
#[doc = "                    }"]
#[doc = "                  }"]
#[doc = "                }"]
#[doc = "              }"]
#[doc = "            }"]
#[doc = "          ]"]
#[doc = "        },"]
#[doc = "        \"release_schedule\": {"]
#[doc = "          \"$ref\": \"#/definitions/release_schedule\""]
#[doc = "        },"]
#[doc = "        \"status\": {"]
#[doc = "          \"$ref\": \"#/definitions/lifecycle_status\""]
#[doc = "        },"]
#[doc = "        \"tags\": {"]
#[doc = "          \"description\": \"List of available tags for this artifact (e.g., container image tags).\","]
#[doc = "          \"type\": \"array\","]
#[doc = "          \"items\": {"]
#[doc = "            \"type\": \"string\""]
#[doc = "          }"]
#[doc = "        },"]
#[doc = "        \"type\": {"]
#[doc = "          \"description\": \"Type identifier from types/artifacts with optional version constraints.\","]
#[doc = "          \"$ref\": \"#/definitions/typeRef\""]
#[doc = "        },"]
#[doc = "        \"versions\": {"]
#[doc = "          \"description\": \"Artifacts that are variants of this artifact (for example, releases or snapshots). Each artifact inherits the metadata of this one unless overridden in its declaration. Identifiers should share the base ID as this package. If versions share the same digest, the artifact identifier refers to the same physical artifact, such as a tagged container image.\","]
#[doc = "          \"type\": \"object\","]
#[doc = "          \"additionalProperties\": {"]
#[doc = "            \"$ref\": \"#/definitions/artifact\""]
#[doc = "          },"]
#[doc = "          \"propertyNames\": {"]
#[doc = "            \"description\": \"A PURL identifier or another URL if the artifact is of a type not supported by PURL.\","]
#[doc = "            \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "          }"]
#[doc = "        }"]
#[doc = "      },"]
#[doc = "      \"additionalProperties\": true"]
#[doc = "    },"]
#[doc = "    {"]
#[doc = "      \"$ref\": \"#/definitions/relationships\""]
#[doc = "    }"]
#[doc = "  ],"]
#[doc = "  \"$$target\": \"#/definitions/artifact\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct Artifact {
    #[doc = "Map of URLs of interesting artifacts that this record embeds or incorporates."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub contains: ::std::option::Option<TypedUrLs>,
    #[doc = "Build-time or run-time, dependencies the user may provide or configure. if url, it could be service use needs an account on or a default. Software, services, or environment context that the instantiation may depend on. Keys are labels or URLs, values are type constraints of components or capabilities. Non-exhaustive: for example, the artifact type may imply additional requirements or some dependencies might be optional."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub dependencies: ::std::option::Option<TypedUrLs>,
    #[doc = "Cryptographic digest of the artifact."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub digest: ::std::option::Option<::std::string::String>,
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub discovery: ::std::option::Option<Discovery>,
    #[doc = "Indicates whether the artifact identifier refers to an artifact that will not change."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub immutable: ::std::option::Option<bool>,
    #[doc = "URLs referencing instantiations that created or validated this artifact."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub instantiated_by: ::std::option::Option<TypedUrLs>,
    #[doc = "Map of URLs (or labels) of entities (e.g., software package, service image or template, capabilities pipeline, build tools) that this artifact instantiates with optional type constraints."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub instantiates: ::std::option::Option<TypedUrLs>,
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub metadata: ::std::option::Option<ArtifactMetadata>,
    #[doc = "(Build-time or run-time) Map of URLs of interesting artifacts, repositories or services that this artifact may reference when executed or instantiated."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub references: ::std::option::Option<TypedUrLs>,
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub release_schedule: ::std::option::Option<ReleaseSchedule>,
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub status: ::std::option::Option<LifecycleStatus>,
    #[doc = "List of available tags for this artifact (e.g., container image tags)."]
    #[serde(default, skip_serializing_if = "::std::vec::Vec::is_empty")]
    pub tags: ::std::vec::Vec<::std::string::String>,
    #[doc = "Type identifier from types/artifacts with optional version constraints."]
    #[serde(
        rename = "type",
        default,
        skip_serializing_if = "::std::option::Option::is_none"
    )]
    pub type_: ::std::option::Option<TypeRef>,
    #[doc = "Artifacts that are variants of this artifact (for example, releases or snapshots). Each artifact inherits the metadata of this one unless overridden in its declaration. Identifiers should share the base ID as this package. If versions share the same digest, the artifact identifier refers to the same physical artifact, such as a tagged container image."]
    #[serde(
        default,
        skip_serializing_if = ":: std :: collections :: HashMap::is_empty"
    )]
    pub versions: ::std::collections::HashMap<ArtifactVersionsKey, Artifact>,
}
impl ::std::default::Default for Artifact {
    fn default() -> Self {
        Self {
            contains: Default::default(),
            dependencies: Default::default(),
            digest: Default::default(),
            discovery: Default::default(),
            immutable: Default::default(),
            instantiated_by: Default::default(),
            instantiates: Default::default(),
            metadata: Default::default(),
            references: Default::default(),
            release_schedule: Default::default(),
            status: Default::default(),
            tags: Default::default(),
            type_: Default::default(),
            versions: Default::default(),
        }
    }
}
#[doc = "Human-readable metadata about the artifact."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"Human-readable metadata about the artifact.\","]
#[doc = "  \"allOf\": ["]
#[doc = "    {"]
#[doc = "      \"$ref\": \"#/definitions/metadata\""]
#[doc = "    },"]
#[doc = "    {"]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"properties\": {"]
#[doc = "        \"platforms\": {"]
#[doc = "          \"description\": \"List of platforms this artifact supports.\","]
#[doc = "          \"type\": \"array\","]
#[doc = "          \"items\": {"]
#[doc = "            \"type\": \"object\","]
#[doc = "            \"properties\": {"]
#[doc = "              \"architecture\": {"]
#[doc = "                \"description\": \"CPU architecture (e.g., amd64, arm64).\","]
#[doc = "                \"type\": \"string\""]
#[doc = "              },"]
#[doc = "              \"os\": {"]
#[doc = "                \"description\": \"Operating system (e.g., linux, windows).\","]
#[doc = "                \"type\": \"string\""]
#[doc = "              }"]
#[doc = "            }"]
#[doc = "          }"]
#[doc = "        }"]
#[doc = "      }"]
#[doc = "    }"]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct ArtifactMetadata {
    #[doc = "Date and time on which the resource was created, conforming to RFC 3339."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub created: ::std::option::Option<::chrono::DateTime<::chrono::offset::Utc>>,
    #[doc = "Human-readable description."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub description: ::std::option::Option<::std::string::String>,
    #[doc = "Link to issue, PR/MR, or discussion about this definition."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub discussion_url: ::std::option::Option<::std::string::String>,
    #[doc = "URL to get documentation."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub documentation_url: ::std::option::Option<::std::string::String>,
    #[doc = "URL to the entity this is a fork of."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub fork_of: ::std::option::Option<::std::string::String>,
    #[doc = "URL to find more information."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub homepage_url: ::std::option::Option<::std::string::String>,
    #[doc = "List of platforms this artifact supports."]
    #[serde(default, skip_serializing_if = "::std::vec::Vec::is_empty")]
    pub platforms: ::std::vec::Vec<ArtifactMetadataPlatformsItem>,
    #[doc = "Informal pointer to source ref (branch or tag name)."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub source_ref: ::std::option::Option<::std::string::String>,
    #[doc = "Informal pointer to source code revision. Use when deployment information is not available."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub source_revision: ::std::option::Option<::std::string::String>,
    #[doc = "Informal pointer to source code. Use when deployment information is not available."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub source_url: ::std::option::Option<::std::string::String>,
    #[doc = "License(s) as an SPDX License Expression."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub spdx_licenses: ::std::option::Option<::std::string::String>,
    #[doc = "Icon or thumbnail URL."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub thumbnail_url: ::std::option::Option<::std::string::String>,
    #[doc = "Human-readable title."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub title: ::std::option::Option<::std::string::String>,
    #[doc = "List of topic or categories associated with the resource."]
    #[serde(default, skip_serializing_if = "::std::vec::Vec::is_empty")]
    pub topics: ::std::vec::Vec<::std::string::String>,
    #[doc = "Name of the distributing entity, organization, or individual."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub vendor: ::std::option::Option<::std::string::String>,
    #[doc = "Version. The version may match a label or tag in the source code repository or may be Semantic Versioning-compatible."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub version: ::std::option::Option<ArtifactMetadataVersion>,
}
impl ::std::default::Default for ArtifactMetadata {
    fn default() -> Self {
        Self {
            created: Default::default(),
            description: Default::default(),
            discussion_url: Default::default(),
            documentation_url: Default::default(),
            fork_of: Default::default(),
            homepage_url: Default::default(),
            platforms: Default::default(),
            source_ref: Default::default(),
            source_revision: Default::default(),
            source_url: Default::default(),
            spdx_licenses: Default::default(),
            thumbnail_url: Default::default(),
            title: Default::default(),
            topics: Default::default(),
            vendor: Default::default(),
            version: Default::default(),
        }
    }
}
#[doc = "`ArtifactMetadataPlatformsItem`"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"properties\": {"]
#[doc = "    \"architecture\": {"]
#[doc = "      \"description\": \"CPU architecture (e.g., amd64, arm64).\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"os\": {"]
#[doc = "      \"description\": \"Operating system (e.g., linux, windows).\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    }"]
#[doc = "  }"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct ArtifactMetadataPlatformsItem {
    #[doc = "CPU architecture (e.g., amd64, arm64)."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub architecture: ::std::option::Option<::std::string::String>,
    #[doc = "Operating system (e.g., linux, windows)."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub os: ::std::option::Option<::std::string::String>,
}
impl ::std::default::Default for ArtifactMetadataPlatformsItem {
    fn default() -> Self {
        Self {
            architecture: Default::default(),
            os: Default::default(),
        }
    }
}
#[doc = "Version. The version may match a label or tag in the source code repository or may be Semantic Versioning-compatible."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"Version. The version may match a label or tag in the source code repository or may be Semantic Versioning-compatible.\","]
#[doc = "  \"anyOf\": ["]
#[doc = "    {"]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    {"]
#[doc = "      \"type\": \"number\""]
#[doc = "    }"]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
#[serde(untagged)]
pub enum ArtifactMetadataVersion {
    String(::std::string::String),
    Number(f64),
}
impl ::std::fmt::Display for ArtifactMetadataVersion {
    fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
        match self {
            Self::String(x) => x.fmt(f),
            Self::Number(x) => x.fmt(f),
        }
    }
}
impl ::std::convert::From<f64> for ArtifactMetadataVersion {
    fn from(value: f64) -> Self {
        Self::Number(value)
    }
}
#[doc = "A PURL identifier or another URL if the artifact is of a type not supported by PURL."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"A PURL identifier or another URL if the artifact is of a type not supported by PURL.\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Serialize, Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
#[serde(transparent)]
pub struct ArtifactVersionsKey(::std::string::String);
impl ::std::ops::Deref for ArtifactVersionsKey {
    type Target = ::std::string::String;
    fn deref(&self) -> &::std::string::String {
        &self.0
    }
}
impl ::std::convert::From<ArtifactVersionsKey> for ::std::string::String {
    fn from(value: ArtifactVersionsKey) -> Self {
        value.0
    }
}
impl ::std::str::FromStr for ArtifactVersionsKey {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        static PATTERN: ::std::sync::LazyLock<::regress::Regex> =
            ::std::sync::LazyLock::new(|| ::regress::Regex::new("^[^\\s]*$").unwrap());
        if PATTERN.find(value).is_none() {
            return Err("doesn't match pattern \"^[^\\s]*$\"".into());
        }
        Ok(Self(value.to_string()))
    }
}
impl ::std::convert::TryFrom<&str> for ArtifactVersionsKey {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for ArtifactVersionsKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for ArtifactVersionsKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl<'de> ::serde::Deserialize<'de> for ArtifactVersionsKey {
    fn deserialize<D>(deserializer: D) -> ::std::result::Result<Self, D::Error>
    where
        D: ::serde::Deserializer<'de>,
    {
        ::std::string::String::deserialize(deserializer)?
            .parse()
            .map_err(|e: self::error::ConversionError| {
                <D::Error as ::serde::de::Error>::custom(e.to_string())
            })
    }
}
#[doc = "`CloudMapSchema`"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"title\": \"CloudMap Schema\","]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"required\": ["]
#[doc = "    \"apiVersion\","]
#[doc = "    \"kind\""]
#[doc = "  ],"]
#[doc = "  \"properties\": {"]
#[doc = "    \"apiVersion\": {"]
#[doc = "      \"type\": \"string\","]
#[doc = "      \"enum\": ["]
#[doc = "        \"unfurl/v1alpha1\","]
#[doc = "        \"unfurl/v1.0.0\""]
#[doc = "      ]"]
#[doc = "    },"]
#[doc = "    \"artifacts\": {"]
#[doc = "      \"description\": \"Tangible object that instantiates services or other artifacts. Artifact ID is either a package URL (see <https://github.com/package-url/purl-spec>) or repository URL with path.\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"additionalProperties\": {"]
#[doc = "        \"$ref\": \"#/definitions/artifact\""]
#[doc = "      },"]
#[doc = "      \"propertyNames\": {"]
#[doc = "        \"description\": \"A PURL identifier or another URL if the artifact is of a type not supported by PURL.\","]
#[doc = "        \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "      }"]
#[doc = "    },"]
#[doc = "    \"components\": {"]
#[doc = "      \"description\": \"Components that are produced or consumed by artifacts and services. Components describe relationships (references, instantiates, dependencies) and are identified by URL or label.\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"additionalProperties\": {"]
#[doc = "        \"$ref\": \"#/definitions/component\""]
#[doc = "      },"]
#[doc = "      \"propertyNames\": {"]
#[doc = "        \"description\": \"URL or label of the component.\","]
#[doc = "        \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "      }"]
#[doc = "    },"]
#[doc = "    \"instantiations\": {"]
#[doc = "      \"description\": \"Build and deployment information for artifacts and services. Keys are URLs.\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"additionalProperties\": {"]
#[doc = "        \"$ref\": \"#/definitions/instantiation\""]
#[doc = "      },"]
#[doc = "      \"propertyNames\": {"]
#[doc = "        \"description\": \"URL of the instantiation.\","]
#[doc = "        \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "      }"]
#[doc = "    },"]
#[doc = "    \"kind\": {"]
#[doc = "      \"const\": \"CloudMap\""]
#[doc = "    },"]
#[doc = "    \"metadata\": {"]
#[doc = "      \"description\": \"Human-readable metadata about this CloudMap.\","]
#[doc = "      \"$ref\": \"#/definitions/metadata\""]
#[doc = "    },"]
#[doc = "    \"repositories\": {"]
#[doc = "      \"description\": \"Git repositories. Keys are URLs that start with git://\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"additionalProperties\": {"]
#[doc = "        \"$ref\": \"#/definitions/repository\""]
#[doc = "      },"]
#[doc = "      \"propertyNames\": {"]
#[doc = "        \"description\": \"URL of the repository using the git:// URL scheme.\","]
#[doc = "        \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "      }"]
#[doc = "    },"]
#[doc = "    \"services\": {"]
#[doc = "      \"description\": \"Instances of services.\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"additionalProperties\": {"]
#[doc = "        \"$ref\": \"#/definitions/service\""]
#[doc = "      },"]
#[doc = "      \"propertyNames\": {"]
#[doc = "        \"description\": \"URL of the service.\","]
#[doc = "        \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "      }"]
#[doc = "    },"]
#[doc = "    \"types\": {"]
#[doc = "      \"description\": \"Type definitions for artifacts, services, software, and capabilities.\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"additionalProperties\": {"]
#[doc = "        \"$ref\": \"#/definitions/type\""]
#[doc = "      },"]
#[doc = "      \"propertyNames\": {"]
#[doc = "        \"description\": \"Fully-qualified type name with namespace (e.g., Compute@unfurl.cloud/onecommons/std).\","]
#[doc = "        \"pattern\": \"^[^\\\\s]+$\""]
#[doc = "      }"]
#[doc = "    }"]
#[doc = "  },"]
#[doc = "  \"additionalProperties\": true"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct CloudMapSchema {
    #[serde(rename = "apiVersion")]
    pub api_version: CloudMapSchemaApiVersion,
    #[doc = "Tangible object that instantiates services or other artifacts. Artifact ID is either a package URL (see <https://github.com/package-url/purl-spec>) or repository URL with path."]
    #[serde(
        default,
        skip_serializing_if = ":: std :: collections :: HashMap::is_empty"
    )]
    pub artifacts: ::std::collections::HashMap<CloudMapSchemaArtifactsKey, Artifact>,
    #[doc = "Components that are produced or consumed by artifacts and services. Components describe relationships (references, instantiates, dependencies) and are identified by URL or label."]
    #[serde(
        default,
        skip_serializing_if = ":: std :: collections :: HashMap::is_empty"
    )]
    pub components: ::std::collections::HashMap<CloudMapSchemaComponentsKey, Component>,
    #[doc = "Build and deployment information for artifacts and services. Keys are URLs."]
    #[serde(
        default,
        skip_serializing_if = ":: std :: collections :: HashMap::is_empty"
    )]
    pub instantiations: ::std::collections::HashMap<CloudMapSchemaInstantiationsKey, Instantiation>,
    pub kind: ::serde_json::Value,
    #[doc = "Human-readable metadata about this CloudMap."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub metadata: ::std::option::Option<Metadata>,
    #[doc = "Git repositories. Keys are URLs that start with git://"]
    #[serde(
        default,
        skip_serializing_if = ":: std :: collections :: HashMap::is_empty"
    )]
    pub repositories: ::std::collections::HashMap<CloudMapSchemaRepositoriesKey, Repository>,
    #[doc = "Instances of services."]
    #[serde(
        default,
        skip_serializing_if = ":: std :: collections :: HashMap::is_empty"
    )]
    pub services: ::std::collections::HashMap<CloudMapSchemaServicesKey, Service>,
    #[doc = "Type definitions for artifacts, services, software, and capabilities."]
    #[serde(
        default,
        skip_serializing_if = ":: std :: collections :: HashMap::is_empty"
    )]
    pub types: ::std::collections::HashMap<CloudMapSchemaTypesKey, Type>,
}
#[doc = "`CloudMapSchemaApiVersion`"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"enum\": ["]
#[doc = "    \"unfurl/v1alpha1\","]
#[doc = "    \"unfurl/v1.0.0\""]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(
    :: serde :: Deserialize,
    :: serde :: Serialize,
    Clone,
    Copy,
    Debug,
    Eq,
    Hash,
    Ord,
    PartialEq,
    PartialOrd,
)]
pub enum CloudMapSchemaApiVersion {
    #[serde(rename = "unfurl/v1alpha1")]
    UnfurlV1alpha1,
    #[serde(rename = "unfurl/v1.0.0")]
    UnfurlV100,
}
impl ::std::fmt::Display for CloudMapSchemaApiVersion {
    fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
        match *self {
            Self::UnfurlV1alpha1 => f.write_str("unfurl/v1alpha1"),
            Self::UnfurlV100 => f.write_str("unfurl/v1.0.0"),
        }
    }
}
impl ::std::str::FromStr for CloudMapSchemaApiVersion {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        match value {
            "unfurl/v1alpha1" => Ok(Self::UnfurlV1alpha1),
            "unfurl/v1.0.0" => Ok(Self::UnfurlV100),
            _ => Err("invalid value".into()),
        }
    }
}
impl ::std::convert::TryFrom<&str> for CloudMapSchemaApiVersion {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for CloudMapSchemaApiVersion {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for CloudMapSchemaApiVersion {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
#[doc = "A PURL identifier or another URL if the artifact is of a type not supported by PURL."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"A PURL identifier or another URL if the artifact is of a type not supported by PURL.\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Serialize, Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
#[serde(transparent)]
pub struct CloudMapSchemaArtifactsKey(::std::string::String);
impl ::std::ops::Deref for CloudMapSchemaArtifactsKey {
    type Target = ::std::string::String;
    fn deref(&self) -> &::std::string::String {
        &self.0
    }
}
impl ::std::convert::From<CloudMapSchemaArtifactsKey> for ::std::string::String {
    fn from(value: CloudMapSchemaArtifactsKey) -> Self {
        value.0
    }
}
impl ::std::str::FromStr for CloudMapSchemaArtifactsKey {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        static PATTERN: ::std::sync::LazyLock<::regress::Regex> =
            ::std::sync::LazyLock::new(|| ::regress::Regex::new("^[^\\s]*$").unwrap());
        if PATTERN.find(value).is_none() {
            return Err("doesn't match pattern \"^[^\\s]*$\"".into());
        }
        Ok(Self(value.to_string()))
    }
}
impl ::std::convert::TryFrom<&str> for CloudMapSchemaArtifactsKey {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for CloudMapSchemaArtifactsKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for CloudMapSchemaArtifactsKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl<'de> ::serde::Deserialize<'de> for CloudMapSchemaArtifactsKey {
    fn deserialize<D>(deserializer: D) -> ::std::result::Result<Self, D::Error>
    where
        D: ::serde::Deserializer<'de>,
    {
        ::std::string::String::deserialize(deserializer)?
            .parse()
            .map_err(|e: self::error::ConversionError| {
                <D::Error as ::serde::de::Error>::custom(e.to_string())
            })
    }
}
#[doc = "URL or label of the component."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"URL or label of the component.\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Serialize, Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
#[serde(transparent)]
pub struct CloudMapSchemaComponentsKey(::std::string::String);
impl ::std::ops::Deref for CloudMapSchemaComponentsKey {
    type Target = ::std::string::String;
    fn deref(&self) -> &::std::string::String {
        &self.0
    }
}
impl ::std::convert::From<CloudMapSchemaComponentsKey> for ::std::string::String {
    fn from(value: CloudMapSchemaComponentsKey) -> Self {
        value.0
    }
}
impl ::std::str::FromStr for CloudMapSchemaComponentsKey {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        static PATTERN: ::std::sync::LazyLock<::regress::Regex> =
            ::std::sync::LazyLock::new(|| ::regress::Regex::new("^[^\\s]*$").unwrap());
        if PATTERN.find(value).is_none() {
            return Err("doesn't match pattern \"^[^\\s]*$\"".into());
        }
        Ok(Self(value.to_string()))
    }
}
impl ::std::convert::TryFrom<&str> for CloudMapSchemaComponentsKey {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for CloudMapSchemaComponentsKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for CloudMapSchemaComponentsKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl<'de> ::serde::Deserialize<'de> for CloudMapSchemaComponentsKey {
    fn deserialize<D>(deserializer: D) -> ::std::result::Result<Self, D::Error>
    where
        D: ::serde::Deserializer<'de>,
    {
        ::std::string::String::deserialize(deserializer)?
            .parse()
            .map_err(|e: self::error::ConversionError| {
                <D::Error as ::serde::de::Error>::custom(e.to_string())
            })
    }
}
#[doc = "URL of the instantiation."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"URL of the instantiation.\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Serialize, Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
#[serde(transparent)]
pub struct CloudMapSchemaInstantiationsKey(::std::string::String);
impl ::std::ops::Deref for CloudMapSchemaInstantiationsKey {
    type Target = ::std::string::String;
    fn deref(&self) -> &::std::string::String {
        &self.0
    }
}
impl ::std::convert::From<CloudMapSchemaInstantiationsKey> for ::std::string::String {
    fn from(value: CloudMapSchemaInstantiationsKey) -> Self {
        value.0
    }
}
impl ::std::str::FromStr for CloudMapSchemaInstantiationsKey {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        static PATTERN: ::std::sync::LazyLock<::regress::Regex> =
            ::std::sync::LazyLock::new(|| ::regress::Regex::new("^[^\\s]*$").unwrap());
        if PATTERN.find(value).is_none() {
            return Err("doesn't match pattern \"^[^\\s]*$\"".into());
        }
        Ok(Self(value.to_string()))
    }
}
impl ::std::convert::TryFrom<&str> for CloudMapSchemaInstantiationsKey {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for CloudMapSchemaInstantiationsKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for CloudMapSchemaInstantiationsKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl<'de> ::serde::Deserialize<'de> for CloudMapSchemaInstantiationsKey {
    fn deserialize<D>(deserializer: D) -> ::std::result::Result<Self, D::Error>
    where
        D: ::serde::Deserializer<'de>,
    {
        ::std::string::String::deserialize(deserializer)?
            .parse()
            .map_err(|e: self::error::ConversionError| {
                <D::Error as ::serde::de::Error>::custom(e.to_string())
            })
    }
}
#[doc = "URL of the repository using the git:// URL scheme."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"URL of the repository using the git:// URL scheme.\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Serialize, Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
#[serde(transparent)]
pub struct CloudMapSchemaRepositoriesKey(::std::string::String);
impl ::std::ops::Deref for CloudMapSchemaRepositoriesKey {
    type Target = ::std::string::String;
    fn deref(&self) -> &::std::string::String {
        &self.0
    }
}
impl ::std::convert::From<CloudMapSchemaRepositoriesKey> for ::std::string::String {
    fn from(value: CloudMapSchemaRepositoriesKey) -> Self {
        value.0
    }
}
impl ::std::str::FromStr for CloudMapSchemaRepositoriesKey {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        static PATTERN: ::std::sync::LazyLock<::regress::Regex> =
            ::std::sync::LazyLock::new(|| ::regress::Regex::new("^[^\\s]*$").unwrap());
        if PATTERN.find(value).is_none() {
            return Err("doesn't match pattern \"^[^\\s]*$\"".into());
        }
        Ok(Self(value.to_string()))
    }
}
impl ::std::convert::TryFrom<&str> for CloudMapSchemaRepositoriesKey {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for CloudMapSchemaRepositoriesKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for CloudMapSchemaRepositoriesKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl<'de> ::serde::Deserialize<'de> for CloudMapSchemaRepositoriesKey {
    fn deserialize<D>(deserializer: D) -> ::std::result::Result<Self, D::Error>
    where
        D: ::serde::Deserializer<'de>,
    {
        ::std::string::String::deserialize(deserializer)?
            .parse()
            .map_err(|e: self::error::ConversionError| {
                <D::Error as ::serde::de::Error>::custom(e.to_string())
            })
    }
}
#[doc = "URL of the service."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"URL of the service.\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Serialize, Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
#[serde(transparent)]
pub struct CloudMapSchemaServicesKey(::std::string::String);
impl ::std::ops::Deref for CloudMapSchemaServicesKey {
    type Target = ::std::string::String;
    fn deref(&self) -> &::std::string::String {
        &self.0
    }
}
impl ::std::convert::From<CloudMapSchemaServicesKey> for ::std::string::String {
    fn from(value: CloudMapSchemaServicesKey) -> Self {
        value.0
    }
}
impl ::std::str::FromStr for CloudMapSchemaServicesKey {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        static PATTERN: ::std::sync::LazyLock<::regress::Regex> =
            ::std::sync::LazyLock::new(|| ::regress::Regex::new("^[^\\s]*$").unwrap());
        if PATTERN.find(value).is_none() {
            return Err("doesn't match pattern \"^[^\\s]*$\"".into());
        }
        Ok(Self(value.to_string()))
    }
}
impl ::std::convert::TryFrom<&str> for CloudMapSchemaServicesKey {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for CloudMapSchemaServicesKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for CloudMapSchemaServicesKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl<'de> ::serde::Deserialize<'de> for CloudMapSchemaServicesKey {
    fn deserialize<D>(deserializer: D) -> ::std::result::Result<Self, D::Error>
    where
        D: ::serde::Deserializer<'de>,
    {
        ::std::string::String::deserialize(deserializer)?
            .parse()
            .map_err(|e: self::error::ConversionError| {
                <D::Error as ::serde::de::Error>::custom(e.to_string())
            })
    }
}
#[doc = "Fully-qualified type name with namespace (e.g., Compute@unfurl.cloud/onecommons/std)."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"Fully-qualified type name with namespace (e.g., Compute@unfurl.cloud/onecommons/std).\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"pattern\": \"^[^\\\\s]+$\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Serialize, Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
#[serde(transparent)]
pub struct CloudMapSchemaTypesKey(::std::string::String);
impl ::std::ops::Deref for CloudMapSchemaTypesKey {
    type Target = ::std::string::String;
    fn deref(&self) -> &::std::string::String {
        &self.0
    }
}
impl ::std::convert::From<CloudMapSchemaTypesKey> for ::std::string::String {
    fn from(value: CloudMapSchemaTypesKey) -> Self {
        value.0
    }
}
impl ::std::str::FromStr for CloudMapSchemaTypesKey {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        static PATTERN: ::std::sync::LazyLock<::regress::Regex> =
            ::std::sync::LazyLock::new(|| ::regress::Regex::new("^[^\\s]+$").unwrap());
        if PATTERN.find(value).is_none() {
            return Err("doesn't match pattern \"^[^\\s]+$\"".into());
        }
        Ok(Self(value.to_string()))
    }
}
impl ::std::convert::TryFrom<&str> for CloudMapSchemaTypesKey {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for CloudMapSchemaTypesKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for CloudMapSchemaTypesKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl<'de> ::serde::Deserialize<'de> for CloudMapSchemaTypesKey {
    fn deserialize<D>(deserializer: D) -> ::std::result::Result<Self, D::Error>
    where
        D: ::serde::Deserializer<'de>,
    {
        ::std::string::String::deserialize(deserializer)?
            .parse()
            .map_err(|e: self::error::ConversionError| {
                <D::Error as ::serde::de::Error>::custom(e.to_string())
            })
    }
}
#[doc = "`Component`"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"title\": \"Component\","]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"allOf\": ["]
#[doc = "    {"]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"properties\": {"]
#[doc = "        \"metadata\": {"]
#[doc = "          \"description\": \"Human-readable metadata about the service.\","]
#[doc = "          \"$ref\": \"#/definitions/metadata\""]
#[doc = "        },"]
#[doc = "        \"source\": {"]
#[doc = "          \"description\": \"Repository or artifact URL.\","]
#[doc = "          \"type\": \"string\""]
#[doc = "        },"]
#[doc = "        \"status\": {"]
#[doc = "          \"description\": \"Lifecycle status of the component.\","]
#[doc = "          \"$ref\": \"#/definitions/lifecycle_status\""]
#[doc = "        },"]
#[doc = "        \"type\": {"]
#[doc = "          \"description\": \"Type identifier from types/components with optional version constraints.\","]
#[doc = "          \"$ref\": \"#/definitions/typeRef\""]
#[doc = "        },"]
#[doc = "        \"versions\": {"]
#[doc = "          \"description\": \"Components that are variants of this component (for example, different versions or configurations). Each component inherits the metadata of this one unless overridden in its declaration.\","]
#[doc = "          \"type\": \"object\","]
#[doc = "          \"additionalProperties\": {"]
#[doc = "            \"$ref\": \"#/definitions/component\""]
#[doc = "          },"]
#[doc = "          \"propertyNames\": {"]
#[doc = "            \"description\": \"URL of the component variant.\","]
#[doc = "            \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "          }"]
#[doc = "        }"]
#[doc = "      },"]
#[doc = "      \"additionalProperties\": true"]
#[doc = "    },"]
#[doc = "    {"]
#[doc = "      \"$ref\": \"#/definitions/relationships\""]
#[doc = "    }"]
#[doc = "  ],"]
#[doc = "  \"$$target\": \"#/definitions/component\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct Component {
    #[doc = "Map of URLs of interesting artifacts that this record embeds or incorporates."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub contains: ::std::option::Option<TypedUrLs>,
    #[doc = "Build-time or run-time, dependencies the user may provide or configure. if url, it could be service use needs an account on or a default. Software, services, or environment context that the instantiation may depend on. Keys are labels or URLs, values are type constraints of components or capabilities. Non-exhaustive: for example, the artifact type may imply additional requirements or some dependencies might be optional."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub dependencies: ::std::option::Option<TypedUrLs>,
    #[doc = "Map of URLs (or labels) of entities (e.g., software package, service image or template, capabilities pipeline, build tools) that this artifact instantiates with optional type constraints."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub instantiates: ::std::option::Option<TypedUrLs>,
    #[doc = "Human-readable metadata about the service."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub metadata: ::std::option::Option<Metadata>,
    #[doc = "(Build-time or run-time) Map of URLs of interesting artifacts, repositories or services that this artifact may reference when executed or instantiated."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub references: ::std::option::Option<TypedUrLs>,
    #[doc = "Repository or artifact URL."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub source: ::std::option::Option<::std::string::String>,
    #[doc = "Lifecycle status of the component."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub status: ::std::option::Option<LifecycleStatus>,
    #[doc = "Type identifier from types/components with optional version constraints."]
    #[serde(
        rename = "type",
        default,
        skip_serializing_if = "::std::option::Option::is_none"
    )]
    pub type_: ::std::option::Option<TypeRef>,
    #[doc = "Components that are variants of this component (for example, different versions or configurations). Each component inherits the metadata of this one unless overridden in its declaration."]
    #[serde(
        default,
        skip_serializing_if = ":: std :: collections :: HashMap::is_empty"
    )]
    pub versions: ::std::collections::HashMap<ComponentVersionsKey, Component>,
}
impl ::std::default::Default for Component {
    fn default() -> Self {
        Self {
            contains: Default::default(),
            dependencies: Default::default(),
            instantiates: Default::default(),
            metadata: Default::default(),
            references: Default::default(),
            source: Default::default(),
            status: Default::default(),
            type_: Default::default(),
            versions: Default::default(),
        }
    }
}
#[doc = "URL of the component variant."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"URL of the component variant.\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Serialize, Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
#[serde(transparent)]
pub struct ComponentVersionsKey(::std::string::String);
impl ::std::ops::Deref for ComponentVersionsKey {
    type Target = ::std::string::String;
    fn deref(&self) -> &::std::string::String {
        &self.0
    }
}
impl ::std::convert::From<ComponentVersionsKey> for ::std::string::String {
    fn from(value: ComponentVersionsKey) -> Self {
        value.0
    }
}
impl ::std::str::FromStr for ComponentVersionsKey {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        static PATTERN: ::std::sync::LazyLock<::regress::Regex> =
            ::std::sync::LazyLock::new(|| ::regress::Regex::new("^[^\\s]*$").unwrap());
        if PATTERN.find(value).is_none() {
            return Err("doesn't match pattern \"^[^\\s]*$\"".into());
        }
        Ok(Self(value.to_string()))
    }
}
impl ::std::convert::TryFrom<&str> for ComponentVersionsKey {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for ComponentVersionsKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for ComponentVersionsKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl<'de> ::serde::Deserialize<'de> for ComponentVersionsKey {
    fn deserialize<D>(deserializer: D) -> ::std::result::Result<Self, D::Error>
    where
        D: ::serde::Deserializer<'de>,
    {
        ::std::string::String::deserialize(deserializer)?
            .parse()
            .map_err(|e: self::error::ConversionError| {
                <D::Error as ::serde::de::Error>::custom(e.to_string())
            })
    }
}
#[doc = "Metadata discovery information."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"title\": \"Discovery\","]
#[doc = "  \"description\": \"Metadata discovery information.\","]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"properties\": {"]
#[doc = "    \"last_checked\": {"]
#[doc = "      \"description\": \"Date and time of the last metadata check, conforming to RFC 3339.\","]
#[doc = "      \"type\": \"string\","]
#[doc = "      \"format\": \"date-time\""]
#[doc = "    },"]
#[doc = "    \"sources\": {"]
#[doc = "      \"description\": \"List of URLs that were used for metadata discovery, such as API URLs or PR URLs for manual edits.\","]
#[doc = "      \"type\": \"array\","]
#[doc = "      \"items\": {"]
#[doc = "        \"type\": \"string\""]
#[doc = "      }"]
#[doc = "    }"]
#[doc = "  },"]
#[doc = "  \"$$target\": \"#/definitions/discovery\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct Discovery {
    #[doc = "Date and time of the last metadata check, conforming to RFC 3339."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub last_checked: ::std::option::Option<::chrono::DateTime<::chrono::offset::Utc>>,
    #[doc = "List of URLs that were used for metadata discovery, such as API URLs or PR URLs for manual edits."]
    #[serde(default, skip_serializing_if = "::std::vec::Vec::is_empty")]
    pub sources: ::std::vec::Vec<::std::string::String>,
}
impl ::std::default::Default for Discovery {
    fn default() -> Self {
        Self {
            last_checked: Default::default(),
            sources: Default::default(),
        }
    }
}
#[doc = "**(Deprecated)** Inline artifact"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"**(Deprecated)** Inline artifact\","]
#[doc = "  \"deprecated\": true,"]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"additionalProperties\": {"]
#[doc = "    \"type\": \"object\","]
#[doc = "    \"required\": ["]
#[doc = "      \"artifact_type\""]
#[doc = "    ],"]
#[doc = "    \"properties\": {"]
#[doc = "      \"artifact_type\": {"]
#[doc = "        \"type\": \"string\""]
#[doc = "      },"]
#[doc = "      \"artifacts\": {"]
#[doc = "        \"type\": \"array\","]
#[doc = "        \"items\": {"]
#[doc = "          \"type\": \"string\""]
#[doc = "        }"]
#[doc = "      },"]
#[doc = "      \"dependencies\": {"]
#[doc = "        \"type\": \"array\","]
#[doc = "        \"items\": {"]
#[doc = "          \"type\": \"string\""]
#[doc = "        }"]
#[doc = "      },"]
#[doc = "      \"description\": {"]
#[doc = "        \"type\": \"string\""]
#[doc = "      },"]
#[doc = "      \"name\": {"]
#[doc = "        \"type\": \"string\""]
#[doc = "      },"]
#[doc = "      \"schema\": {"]
#[doc = "        \"type\": \"string\""]
#[doc = "      },"]
#[doc = "      \"type\": {"]
#[doc = "        \"anyOf\": ["]
#[doc = "          {"]
#[doc = "            \"type\": \"string\""]
#[doc = "          },"]
#[doc = "          {"]
#[doc = "            \"type\": \"object\","]
#[doc = "            \"properties\": {"]
#[doc = "              \"extends\": {"]
#[doc = "                \"type\": \"array\","]
#[doc = "                \"items\": {"]
#[doc = "                  \"type\": \"string\""]
#[doc = "                }"]
#[doc = "              },"]
#[doc = "              \"name\": {"]
#[doc = "                \"type\": \"string\""]
#[doc = "              },"]
#[doc = "              \"title\": {"]
#[doc = "                \"type\": \"string\""]
#[doc = "              }"]
#[doc = "            }"]
#[doc = "          }"]
#[doc = "        ]"]
#[doc = "      },"]
#[doc = "      \"version\": {"]
#[doc = "        \"anyOf\": ["]
#[doc = "          {"]
#[doc = "            \"type\": \"string\""]
#[doc = "          },"]
#[doc = "          {"]
#[doc = "            \"type\": \"number\""]
#[doc = "          }"]
#[doc = "        ]"]
#[doc = "      }"]
#[doc = "    }"]
#[doc = "  },"]
#[doc = "  \"$$target\": \"#/definitions/inlineArtifact\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
#[serde(transparent)]
pub struct InlineArtifact(
    pub ::std::collections::HashMap<::std::string::String, InlineArtifactValue>,
);
impl ::std::ops::Deref for InlineArtifact {
    type Target = ::std::collections::HashMap<::std::string::String, InlineArtifactValue>;
    fn deref(&self) -> &::std::collections::HashMap<::std::string::String, InlineArtifactValue> {
        &self.0
    }
}
impl ::std::convert::From<InlineArtifact>
    for ::std::collections::HashMap<::std::string::String, InlineArtifactValue>
{
    fn from(value: InlineArtifact) -> Self {
        value.0
    }
}
impl ::std::convert::From<::std::collections::HashMap<::std::string::String, InlineArtifactValue>>
    for InlineArtifact
{
    fn from(
        value: ::std::collections::HashMap<::std::string::String, InlineArtifactValue>,
    ) -> Self {
        Self(value)
    }
}
#[doc = "`InlineArtifactValue`"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"required\": ["]
#[doc = "    \"artifact_type\""]
#[doc = "  ],"]
#[doc = "  \"properties\": {"]
#[doc = "    \"artifact_type\": {"]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"artifacts\": {"]
#[doc = "      \"type\": \"array\","]
#[doc = "      \"items\": {"]
#[doc = "        \"type\": \"string\""]
#[doc = "      }"]
#[doc = "    },"]
#[doc = "    \"dependencies\": {"]
#[doc = "      \"type\": \"array\","]
#[doc = "      \"items\": {"]
#[doc = "        \"type\": \"string\""]
#[doc = "      }"]
#[doc = "    },"]
#[doc = "    \"description\": {"]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"name\": {"]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"schema\": {"]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"type\": {"]
#[doc = "      \"anyOf\": ["]
#[doc = "        {"]
#[doc = "          \"type\": \"string\""]
#[doc = "        },"]
#[doc = "        {"]
#[doc = "          \"type\": \"object\","]
#[doc = "          \"properties\": {"]
#[doc = "            \"extends\": {"]
#[doc = "              \"type\": \"array\","]
#[doc = "              \"items\": {"]
#[doc = "                \"type\": \"string\""]
#[doc = "              }"]
#[doc = "            },"]
#[doc = "            \"name\": {"]
#[doc = "              \"type\": \"string\""]
#[doc = "            },"]
#[doc = "            \"title\": {"]
#[doc = "              \"type\": \"string\""]
#[doc = "            }"]
#[doc = "          }"]
#[doc = "        }"]
#[doc = "      ]"]
#[doc = "    },"]
#[doc = "    \"version\": {"]
#[doc = "      \"anyOf\": ["]
#[doc = "        {"]
#[doc = "          \"type\": \"string\""]
#[doc = "        },"]
#[doc = "        {"]
#[doc = "          \"type\": \"number\""]
#[doc = "        }"]
#[doc = "      ]"]
#[doc = "    }"]
#[doc = "  }"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct InlineArtifactValue {
    pub artifact_type: ::std::string::String,
    #[serde(default, skip_serializing_if = "::std::vec::Vec::is_empty")]
    pub artifacts: ::std::vec::Vec<::std::string::String>,
    #[serde(default, skip_serializing_if = "::std::vec::Vec::is_empty")]
    pub dependencies: ::std::vec::Vec<::std::string::String>,
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub description: ::std::option::Option<::std::string::String>,
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub name: ::std::option::Option<::std::string::String>,
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub schema: ::std::option::Option<::std::string::String>,
    #[serde(
        rename = "type",
        default,
        skip_serializing_if = "::std::option::Option::is_none"
    )]
    pub type_: ::std::option::Option<InlineArtifactValueType>,
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub version: ::std::option::Option<InlineArtifactValueVersion>,
}
#[doc = "`InlineArtifactValueType`"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"anyOf\": ["]
#[doc = "    {"]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    {"]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"properties\": {"]
#[doc = "        \"extends\": {"]
#[doc = "          \"type\": \"array\","]
#[doc = "          \"items\": {"]
#[doc = "            \"type\": \"string\""]
#[doc = "          }"]
#[doc = "        },"]
#[doc = "        \"name\": {"]
#[doc = "          \"type\": \"string\""]
#[doc = "        },"]
#[doc = "        \"title\": {"]
#[doc = "          \"type\": \"string\""]
#[doc = "        }"]
#[doc = "      }"]
#[doc = "    }"]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
#[serde(untagged)]
pub enum InlineArtifactValueType {
    String(::std::string::String),
    Object {
        #[serde(default, skip_serializing_if = "::std::vec::Vec::is_empty")]
        extends: ::std::vec::Vec<::std::string::String>,
        #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
        name: ::std::option::Option<::std::string::String>,
        #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
        title: ::std::option::Option<::std::string::String>,
    },
}
#[doc = "`InlineArtifactValueVersion`"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"anyOf\": ["]
#[doc = "    {"]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    {"]
#[doc = "      \"type\": \"number\""]
#[doc = "    }"]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
#[serde(untagged)]
pub enum InlineArtifactValueVersion {
    String(::std::string::String),
    Number(f64),
}
impl ::std::fmt::Display for InlineArtifactValueVersion {
    fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
        match self {
            Self::String(x) => x.fmt(f),
            Self::Number(x) => x.fmt(f),
        }
    }
}
impl ::std::convert::From<f64> for InlineArtifactValueVersion {
    fn from(value: f64) -> Self {
        Self::Number(value)
    }
}
#[doc = "`Instantiation`"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"title\": \"Instantiation\","]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"properties\": {"]
#[doc = "    \"digest\": {"]
#[doc = "      \"description\": \"Cryptographic digest of the document referenced by the instantiation URL.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"discovery\": {"]
#[doc = "      \"$ref\": \"#/definitions/discovery\""]
#[doc = "    },"]
#[doc = "    \"inputs\": {"]
#[doc = "      \"description\": \"The artifact, instantiation, service, or repository URLs that were consumed or referenced as part of the instantiation process.\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"$ref\": \"#/definitions/typedURLs\""]
#[doc = "    },"]
#[doc = "    \"instantiated\": {"]
#[doc = "      \"description\": \"The artifacts or services created or updated by this instantiation with optional capability.\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"$ref\": \"#/definitions/typedURLs\""]
#[doc = "    },"]
#[doc = "    \"metadata\": {"]
#[doc = "      \"description\": \"Additional information about the instantiation, expected contents depends on the instantiation artifact type.\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"$ref\": \"#/definitions/metadata\""]
#[doc = "    },"]
#[doc = "    \"revision\": {"]
#[doc = "      \"description\": \"If instantiation URL references a repository, source control revision of that repository.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"source\": {"]
#[doc = "      \"description\": \"Repository or artifact URL.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"source_ref\": {"]
#[doc = "      \"description\": \"If source URL references a repository, the branch or tag name.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"source_revision\": {"]
#[doc = "      \"description\": \"If source URL references a repository, the source control revision of that repository.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"status\": {"]
#[doc = "      \"description\": \"Lifecycle status of the instantiation\","]
#[doc = "      \"$ref\": \"#/definitions/lifecycle_status\""]
#[doc = "    },"]
#[doc = "    \"type\": {"]
#[doc = "      \"description\": \"Type of the instantiation.\","]
#[doc = "      \"$ref\": \"#/definitions/typeRef\""]
#[doc = "    },"]
#[doc = "    \"versions\": {"]
#[doc = "      \"description\": \"Instantiations that are variants of this instantiation (for example, different deployments or environments). Each instantiation inherits the metadata of this one unless overridden in its declaration.\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"additionalProperties\": {"]
#[doc = "        \"$ref\": \"#/definitions/instantiation\""]
#[doc = "      },"]
#[doc = "      \"propertyNames\": {"]
#[doc = "        \"description\": \"URL of the instantiation variant.\","]
#[doc = "        \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "      }"]
#[doc = "    }"]
#[doc = "  },"]
#[doc = "  \"additionalProperties\": true,"]
#[doc = "  \"$$target\": \"#/definitions/instantiation\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct Instantiation {
    #[doc = "Cryptographic digest of the document referenced by the instantiation URL."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub digest: ::std::option::Option<::std::string::String>,
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub discovery: ::std::option::Option<Discovery>,
    #[doc = "The artifact, instantiation, service, or repository URLs that were consumed or referenced as part of the instantiation process."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub inputs: ::std::option::Option<TypedUrLs>,
    #[doc = "The artifacts or services created or updated by this instantiation with optional capability."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub instantiated: ::std::option::Option<TypedUrLs>,
    #[doc = "Additional information about the instantiation, expected contents depends on the instantiation artifact type."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub metadata: ::std::option::Option<Metadata>,
    #[doc = "If instantiation URL references a repository, source control revision of that repository."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub revision: ::std::option::Option<::std::string::String>,
    #[doc = "Repository or artifact URL."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub source: ::std::option::Option<::std::string::String>,
    #[doc = "If source URL references a repository, the branch or tag name."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub source_ref: ::std::option::Option<::std::string::String>,
    #[doc = "If source URL references a repository, the source control revision of that repository."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub source_revision: ::std::option::Option<::std::string::String>,
    #[doc = "Lifecycle status of the instantiation"]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub status: ::std::option::Option<LifecycleStatus>,
    #[doc = "Type of the instantiation."]
    #[serde(
        rename = "type",
        default,
        skip_serializing_if = "::std::option::Option::is_none"
    )]
    pub type_: ::std::option::Option<TypeRef>,
    #[doc = "Instantiations that are variants of this instantiation (for example, different deployments or environments). Each instantiation inherits the metadata of this one unless overridden in its declaration."]
    #[serde(
        default,
        skip_serializing_if = ":: std :: collections :: HashMap::is_empty"
    )]
    pub versions: ::std::collections::HashMap<InstantiationVersionsKey, Instantiation>,
}
impl ::std::default::Default for Instantiation {
    fn default() -> Self {
        Self {
            digest: Default::default(),
            discovery: Default::default(),
            inputs: Default::default(),
            instantiated: Default::default(),
            metadata: Default::default(),
            revision: Default::default(),
            source: Default::default(),
            source_ref: Default::default(),
            source_revision: Default::default(),
            status: Default::default(),
            type_: Default::default(),
            versions: Default::default(),
        }
    }
}
#[doc = "URL of the instantiation variant."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"URL of the instantiation variant.\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Serialize, Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
#[serde(transparent)]
pub struct InstantiationVersionsKey(::std::string::String);
impl ::std::ops::Deref for InstantiationVersionsKey {
    type Target = ::std::string::String;
    fn deref(&self) -> &::std::string::String {
        &self.0
    }
}
impl ::std::convert::From<InstantiationVersionsKey> for ::std::string::String {
    fn from(value: InstantiationVersionsKey) -> Self {
        value.0
    }
}
impl ::std::str::FromStr for InstantiationVersionsKey {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        static PATTERN: ::std::sync::LazyLock<::regress::Regex> =
            ::std::sync::LazyLock::new(|| ::regress::Regex::new("^[^\\s]*$").unwrap());
        if PATTERN.find(value).is_none() {
            return Err("doesn't match pattern \"^[^\\s]*$\"".into());
        }
        Ok(Self(value.to_string()))
    }
}
impl ::std::convert::TryFrom<&str> for InstantiationVersionsKey {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for InstantiationVersionsKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for InstantiationVersionsKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl<'de> ::serde::Deserialize<'de> for InstantiationVersionsKey {
    fn deserialize<D>(deserializer: D) -> ::std::result::Result<Self, D::Error>
    where
        D: ::serde::Deserializer<'de>,
    {
        ::std::string::String::deserialize(deserializer)?
            .parse()
            .map_err(|e: self::error::ConversionError| {
                <D::Error as ::serde::de::Error>::custom(e.to_string())
            })
    }
}
#[doc = "`LifecycleStatus`"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"title\": \"Lifecycle status\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"enum\": ["]
#[doc = "    \"wishlist\","]
#[doc = "    \"model\","]
#[doc = "    \"planned\","]
#[doc = "    \"development\","]
#[doc = "    \"alpha\","]
#[doc = "    \"beta\","]
#[doc = "    \"production\","]
#[doc = "    \"maintenance\","]
#[doc = "    \"unmaintained\","]
#[doc = "    \"deprecated\","]
#[doc = "    \"removed\""]
#[doc = "  ],"]
#[doc = "  \"$$target\": \"#/definitions/lifecycle_status\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(
    :: serde :: Deserialize,
    :: serde :: Serialize,
    Clone,
    Copy,
    Debug,
    Eq,
    Hash,
    Ord,
    PartialEq,
    PartialOrd,
)]
pub enum LifecycleStatus {
    #[serde(rename = "wishlist")]
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
impl ::std::fmt::Display for LifecycleStatus {
    fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
        match *self {
            Self::Wishlist => f.write_str("wishlist"),
            Self::Model => f.write_str("model"),
            Self::Planned => f.write_str("planned"),
            Self::Development => f.write_str("development"),
            Self::Alpha => f.write_str("alpha"),
            Self::Beta => f.write_str("beta"),
            Self::Production => f.write_str("production"),
            Self::Maintenance => f.write_str("maintenance"),
            Self::Unmaintained => f.write_str("unmaintained"),
            Self::Deprecated => f.write_str("deprecated"),
            Self::Removed => f.write_str("removed"),
        }
    }
}
impl ::std::str::FromStr for LifecycleStatus {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        match value {
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
            _ => Err("invalid value".into()),
        }
    }
}
impl ::std::convert::TryFrom<&str> for LifecycleStatus {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for LifecycleStatus {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for LifecycleStatus {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
#[doc = "Common metadata fields shared across artifacts, services, instantiations, and repositories."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"title\": \"Common Metadata\","]
#[doc = "  \"description\": \"Common metadata fields shared across artifacts, services, instantiations, and repositories.\","]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"properties\": {"]
#[doc = "    \"created\": {"]
#[doc = "      \"description\": \"Date and time on which the resource was created, conforming to RFC 3339.\","]
#[doc = "      \"type\": \"string\","]
#[doc = "      \"format\": \"date-time\""]
#[doc = "    },"]
#[doc = "    \"description\": {"]
#[doc = "      \"description\": \"Human-readable description.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"discussion_url\": {"]
#[doc = "      \"description\": \"Link to issue, PR/MR, or discussion about this definition.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"documentation_url\": {"]
#[doc = "      \"description\": \"URL to get documentation.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"fork_of\": {"]
#[doc = "      \"description\": \"URL to the entity this is a fork of.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"homepage_url\": {"]
#[doc = "      \"description\": \"URL to find more information.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"source_ref\": {"]
#[doc = "      \"description\": \"Informal pointer to source ref (branch or tag name).\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"source_revision\": {"]
#[doc = "      \"description\": \"Informal pointer to source code revision. Use when deployment information is not available.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"source_url\": {"]
#[doc = "      \"description\": \"Informal pointer to source code. Use when deployment information is not available.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"spdx_licenses\": {"]
#[doc = "      \"description\": \"License(s) as an SPDX License Expression.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"thumbnail_url\": {"]
#[doc = "      \"description\": \"Icon or thumbnail URL.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"title\": {"]
#[doc = "      \"description\": \"Human-readable title.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"topics\": {"]
#[doc = "      \"description\": \"List of topic or categories associated with the resource.\","]
#[doc = "      \"type\": \"array\","]
#[doc = "      \"items\": {"]
#[doc = "        \"type\": \"string\""]
#[doc = "      }"]
#[doc = "    },"]
#[doc = "    \"vendor\": {"]
#[doc = "      \"description\": \"Name of the distributing entity, organization, or individual.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"version\": {"]
#[doc = "      \"description\": \"Version. The version may match a label or tag in the source code repository or may be Semantic Versioning-compatible.\","]
#[doc = "      \"anyOf\": ["]
#[doc = "        {"]
#[doc = "          \"type\": \"string\""]
#[doc = "        },"]
#[doc = "        {"]
#[doc = "          \"type\": \"number\""]
#[doc = "        }"]
#[doc = "      ]"]
#[doc = "    }"]
#[doc = "  },"]
#[doc = "  \"$$target\": \"#/definitions/metadata\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct Metadata {
    #[doc = "Date and time on which the resource was created, conforming to RFC 3339."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub created: ::std::option::Option<::chrono::DateTime<::chrono::offset::Utc>>,
    #[doc = "Human-readable description."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub description: ::std::option::Option<::std::string::String>,
    #[doc = "Link to issue, PR/MR, or discussion about this definition."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub discussion_url: ::std::option::Option<::std::string::String>,
    #[doc = "URL to get documentation."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub documentation_url: ::std::option::Option<::std::string::String>,
    #[doc = "URL to the entity this is a fork of."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub fork_of: ::std::option::Option<::std::string::String>,
    #[doc = "URL to find more information."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub homepage_url: ::std::option::Option<::std::string::String>,
    #[doc = "Informal pointer to source ref (branch or tag name)."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub source_ref: ::std::option::Option<::std::string::String>,
    #[doc = "Informal pointer to source code revision. Use when deployment information is not available."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub source_revision: ::std::option::Option<::std::string::String>,
    #[doc = "Informal pointer to source code. Use when deployment information is not available."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub source_url: ::std::option::Option<::std::string::String>,
    #[doc = "License(s) as an SPDX License Expression."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub spdx_licenses: ::std::option::Option<::std::string::String>,
    #[doc = "Icon or thumbnail URL."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub thumbnail_url: ::std::option::Option<::std::string::String>,
    #[doc = "Human-readable title."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub title: ::std::option::Option<::std::string::String>,
    #[doc = "List of topic or categories associated with the resource."]
    #[serde(default, skip_serializing_if = "::std::vec::Vec::is_empty")]
    pub topics: ::std::vec::Vec<::std::string::String>,
    #[doc = "Name of the distributing entity, organization, or individual."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub vendor: ::std::option::Option<::std::string::String>,
    #[doc = "Version. The version may match a label or tag in the source code repository or may be Semantic Versioning-compatible."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub version: ::std::option::Option<MetadataVersion>,
}
impl ::std::default::Default for Metadata {
    fn default() -> Self {
        Self {
            created: Default::default(),
            description: Default::default(),
            discussion_url: Default::default(),
            documentation_url: Default::default(),
            fork_of: Default::default(),
            homepage_url: Default::default(),
            source_ref: Default::default(),
            source_revision: Default::default(),
            source_url: Default::default(),
            spdx_licenses: Default::default(),
            thumbnail_url: Default::default(),
            title: Default::default(),
            topics: Default::default(),
            vendor: Default::default(),
            version: Default::default(),
        }
    }
}
#[doc = "Version. The version may match a label or tag in the source code repository or may be Semantic Versioning-compatible."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"Version. The version may match a label or tag in the source code repository or may be Semantic Versioning-compatible.\","]
#[doc = "  \"anyOf\": ["]
#[doc = "    {"]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    {"]
#[doc = "      \"type\": \"number\""]
#[doc = "    }"]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
#[serde(untagged)]
pub enum MetadataVersion {
    String(::std::string::String),
    Number(f64),
}
impl ::std::fmt::Display for MetadataVersion {
    fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
        match self {
            Self::String(x) => x.fmt(f),
            Self::Number(x) => x.fmt(f),
        }
    }
}
impl ::std::convert::From<f64> for MetadataVersion {
    fn from(value: f64) -> Self {
        Self::Number(value)
    }
}
#[doc = "Common relationships used by artifacts and components to describe how they relate to other records and types. Each field is a typedURLs map whose key is a URL or label and whose value is an optional type reference."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"title\": \"Relationships\","]
#[doc = "  \"description\": \"Common relationships used by artifacts and components to describe how they relate to other records and types. Each field is a typedURLs map whose key is a URL or label and whose value is an optional type reference.\","]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"properties\": {"]
#[doc = "    \"contains\": {"]
#[doc = "      \"description\": \"Map of URLs of interesting artifacts that this record embeds or incorporates.\","]
#[doc = "      \"$ref\": \"#/definitions/typedURLs\""]
#[doc = "    },"]
#[doc = "    \"dependencies\": {"]
#[doc = "      \"description\": \"Build-time or run-time, dependencies the user may provide or configure. if url, it could be service use needs an account on or a default. Software, services, or environment context that the instantiation may depend on. Keys are labels or URLs, values are type constraints of components or capabilities. Non-exhaustive: for example, the artifact type may imply additional requirements or some dependencies might be optional.\","]
#[doc = "      \"$ref\": \"#/definitions/typedURLs\""]
#[doc = "    },"]
#[doc = "    \"instantiates\": {"]
#[doc = "      \"description\": \"Map of URLs (or labels) of entities (e.g., software package, service image or template, capabilities pipeline, build tools) that this artifact instantiates with optional type constraints.\","]
#[doc = "      \"$ref\": \"#/definitions/typedURLs\""]
#[doc = "    },"]
#[doc = "    \"references\": {"]
#[doc = "      \"description\": \"(Build-time or run-time) Map of URLs of interesting artifacts, repositories or services that this artifact may reference when executed or instantiated.\","]
#[doc = "      \"$ref\": \"#/definitions/typedURLs\""]
#[doc = "    }"]
#[doc = "  },"]
#[doc = "  \"$$target\": \"#/definitions/relationships\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct Relationships {
    #[doc = "Map of URLs of interesting artifacts that this record embeds or incorporates."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub contains: ::std::option::Option<TypedUrLs>,
    #[doc = "Build-time or run-time, dependencies the user may provide or configure. if url, it could be service use needs an account on or a default. Software, services, or environment context that the instantiation may depend on. Keys are labels or URLs, values are type constraints of components or capabilities. Non-exhaustive: for example, the artifact type may imply additional requirements or some dependencies might be optional."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub dependencies: ::std::option::Option<TypedUrLs>,
    #[doc = "Map of URLs (or labels) of entities (e.g., software package, service image or template, capabilities pipeline, build tools) that this artifact instantiates with optional type constraints."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub instantiates: ::std::option::Option<TypedUrLs>,
    #[doc = "(Build-time or run-time) Map of URLs of interesting artifacts, repositories or services that this artifact may reference when executed or instantiated."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub references: ::std::option::Option<TypedUrLs>,
}
impl ::std::default::Default for Relationships {
    fn default() -> Self {
        Self {
            contains: Default::default(),
            dependencies: Default::default(),
            instantiates: Default::default(),
            references: Default::default(),
        }
    }
}
#[doc = "Scheduled Release for an artifact or service."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"title\": \"Release Schedule\","]
#[doc = "  \"description\": \"Scheduled Release for an artifact or service.\","]
#[doc = "  \"type\": \"array\","]
#[doc = "  \"items\": {"]
#[doc = "    \"type\": \"object\","]
#[doc = "    \"properties\": {"]
#[doc = "      \"effective_date\": {"]
#[doc = "        \"description\": \"The date and time the release will happen (RFC 3339 format).\","]
#[doc = "        \"type\": \"string\","]
#[doc = "        \"format\": \"date-time\""]
#[doc = "      },"]
#[doc = "      \"status\": {"]
#[doc = "        \"description\": \"The upcoming lifecycle status.\","]
#[doc = "        \"type\": \"string\","]
#[doc = "        \"$ref\": \"#/definitions/lifecycle_status\""]
#[doc = "      },"]
#[doc = "      \"url\": {"]
#[doc = "        \"description\": \"The updated resource URL for this upcoming release.\","]
#[doc = "        \"type\": \"string\""]
#[doc = "      },"]
#[doc = "      \"version\": {"]
#[doc = "        \"description\": \"Version of the upcoming release.\","]
#[doc = "        \"anyOf\": ["]
#[doc = "          {"]
#[doc = "            \"type\": \"string\""]
#[doc = "          },"]
#[doc = "          {"]
#[doc = "            \"type\": \"number\""]
#[doc = "          }"]
#[doc = "        ]"]
#[doc = "      }"]
#[doc = "    }"]
#[doc = "  },"]
#[doc = "  \"$$target\": \"#/definitions/release_schedule\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
#[serde(transparent)]
pub struct ReleaseSchedule(pub ::std::vec::Vec<ReleaseScheduleItem>);
impl ::std::ops::Deref for ReleaseSchedule {
    type Target = ::std::vec::Vec<ReleaseScheduleItem>;
    fn deref(&self) -> &::std::vec::Vec<ReleaseScheduleItem> {
        &self.0
    }
}
impl ::std::convert::From<ReleaseSchedule> for ::std::vec::Vec<ReleaseScheduleItem> {
    fn from(value: ReleaseSchedule) -> Self {
        value.0
    }
}
impl ::std::convert::From<::std::vec::Vec<ReleaseScheduleItem>> for ReleaseSchedule {
    fn from(value: ::std::vec::Vec<ReleaseScheduleItem>) -> Self {
        Self(value)
    }
}
#[doc = "`ReleaseScheduleItem`"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"properties\": {"]
#[doc = "    \"effective_date\": {"]
#[doc = "      \"description\": \"The date and time the release will happen (RFC 3339 format).\","]
#[doc = "      \"type\": \"string\","]
#[doc = "      \"format\": \"date-time\""]
#[doc = "    },"]
#[doc = "    \"status\": {"]
#[doc = "      \"description\": \"The upcoming lifecycle status.\","]
#[doc = "      \"type\": \"string\","]
#[doc = "      \"$ref\": \"#/definitions/lifecycle_status\""]
#[doc = "    },"]
#[doc = "    \"url\": {"]
#[doc = "      \"description\": \"The updated resource URL for this upcoming release.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"version\": {"]
#[doc = "      \"description\": \"Version of the upcoming release.\","]
#[doc = "      \"anyOf\": ["]
#[doc = "        {"]
#[doc = "          \"type\": \"string\""]
#[doc = "        },"]
#[doc = "        {"]
#[doc = "          \"type\": \"number\""]
#[doc = "        }"]
#[doc = "      ]"]
#[doc = "    }"]
#[doc = "  }"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct ReleaseScheduleItem {
    #[doc = "The date and time the release will happen (RFC 3339 format)."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub effective_date: ::std::option::Option<::chrono::DateTime<::chrono::offset::Utc>>,
    #[doc = "The upcoming lifecycle status."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub status: ::std::option::Option<LifecycleStatus>,
    #[doc = "The updated resource URL for this upcoming release."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub url: ::std::option::Option<::std::string::String>,
    #[doc = "Version of the upcoming release."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub version: ::std::option::Option<ReleaseScheduleItemVersion>,
}
impl ::std::default::Default for ReleaseScheduleItem {
    fn default() -> Self {
        Self {
            effective_date: Default::default(),
            status: Default::default(),
            url: Default::default(),
            version: Default::default(),
        }
    }
}
#[doc = "Version of the upcoming release."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"Version of the upcoming release.\","]
#[doc = "  \"anyOf\": ["]
#[doc = "    {"]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    {"]
#[doc = "      \"type\": \"number\""]
#[doc = "    }"]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
#[serde(untagged)]
pub enum ReleaseScheduleItemVersion {
    String(::std::string::String),
    Number(f64),
}
impl ::std::fmt::Display for ReleaseScheduleItemVersion {
    fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
        match self {
            Self::String(x) => x.fmt(f),
            Self::Number(x) => x.fmt(f),
        }
    }
}
impl ::std::convert::From<f64> for ReleaseScheduleItemVersion {
    fn from(value: f64) -> Self {
        Self::Number(value)
    }
}
#[doc = "`Repository`"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"properties\": {"]
#[doc = "    \"branches\": {"]
#[doc = "      \"description\": \"Map of branch names to their commit SHA hashes.\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"additionalProperties\": {"]
#[doc = "        \"description\": \"Commit SHA hash for the branch.\","]
#[doc = "        \"type\": \"string\""]
#[doc = "      }"]
#[doc = "    },"]
#[doc = "    \"contains\": {"]
#[doc = "      \"description\": \"Map of files or directories (as relative URLs) in the repository that are useful for characterizing the repository and integrating it with the other resources in the cloud map, with optional type constraints.\","]
#[doc = "      \"$ref\": \"#/definitions/typedURLs\""]
#[doc = "    },"]
#[doc = "    \"default_branch\": {"]
#[doc = "      \"description\": \"Default branch name (e.g., main, master).\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"fork_of\": {"]
#[doc = "      \"description\": \"URL of the repository that this repository was forked from.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"initial_revision\": {"]
#[doc = "      \"description\": \"Initial commit of the default branch.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"internal_id\": {"]
#[doc = "      \"description\": \"Internal identifier from the repository host (e.g., GitHub repository ID).\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"metadata\": {"]
#[doc = "      \"description\": \"Metadata about the repository that isn't stored in the git repository itself but might be provided by the host (e.g., metadata found on the repository's GitHub or GitLab project page).\","]
#[doc = "      \"allOf\": ["]
#[doc = "        {"]
#[doc = "          \"$ref\": \"#/definitions/metadata\""]
#[doc = "        },"]
#[doc = "        {"]
#[doc = "          \"type\": \"object\","]
#[doc = "          \"properties\": {"]
#[doc = "            \"issues_url\": {"]
#[doc = "              \"description\": \"URL to the issue tracker for the repository.\","]
#[doc = "              \"type\": \"string\""]
#[doc = "            },"]
#[doc = "            \"lastupdate_digest\": {"]
#[doc = "              \"description\": \"Digest hash of the metadata at the time of the last update.\","]
#[doc = "              \"type\": \"string\""]
#[doc = "            },"]
#[doc = "            \"lastupdate_time\": {"]
#[doc = "              \"description\": \"Timestamp of the last metadata update.\","]
#[doc = "              \"type\": \"string\""]
#[doc = "            },"]
#[doc = "            \"license_url\": {"]
#[doc = "              \"description\": \"URL to the license file or license information.\","]
#[doc = "              \"type\": \"string\""]
#[doc = "            },"]
#[doc = "            \"project_status\": {"]
#[doc = "              \"description\": \"Project status as defined by https://www.repostatus.org\","]
#[doc = "              \"type\": \"string\","]
#[doc = "              \"enum\": ["]
#[doc = "                \"concept\","]
#[doc = "                \"WIP\","]
#[doc = "                \"suspended\","]
#[doc = "                \"abandoned\","]
#[doc = "                \"active\","]
#[doc = "                \"inactive\","]
#[doc = "                \"unsupported\","]
#[doc = "                \"moved\""]
#[doc = "              ]"]
#[doc = "            }"]
#[doc = "          }"]
#[doc = "        }"]
#[doc = "      ]"]
#[doc = "    },"]
#[doc = "    \"mirror_of\": {"]
#[doc = "      \"description\": \"URL of the repository that this repository is a mirror of.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"name\": {"]
#[doc = "      \"description\": \"Repository name.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"path\": {"]
#[doc = "      \"description\": \"Project path relative to base location of git repositories on the host.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"private\": {"]
#[doc = "      \"description\": \"True if the repository is not publicly accessible.\","]
#[doc = "      \"type\": \"boolean\""]
#[doc = "    },"]
#[doc = "    \"project_url\": {"]
#[doc = "      \"description\": \"URL to the repository's project page on the host (e.g., <https://github.com/user/repo>).\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"protocols\": {"]
#[doc = "      \"description\": \"List of protocols available to clone the repository (e.g., https, ssh).\","]
#[doc = "      \"type\": \"array\","]
#[doc = "      \"items\": {"]
#[doc = "        \"type\": \"string\""]
#[doc = "      }"]
#[doc = "    },"]
#[doc = "    \"service\": {"]
#[doc = "      \"description\": \"URL of the service hosting this repository.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"tags\": {"]
#[doc = "      \"description\": \"Map of tag names to their commit SHA hashes.\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"additionalProperties\": {"]
#[doc = "        \"description\": \"Commit SHA hash for the tag.\","]
#[doc = "        \"type\": \"string\""]
#[doc = "      }"]
#[doc = "    }"]
#[doc = "  },"]
#[doc = "  \"additionalProperties\": true,"]
#[doc = "  \"$$target\": \"#/definitions/repository\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct Repository {
    #[doc = "Map of branch names to their commit SHA hashes."]
    #[serde(
        default,
        skip_serializing_if = ":: std :: collections :: HashMap::is_empty"
    )]
    pub branches: ::std::collections::HashMap<::std::string::String, ::std::string::String>,
    #[doc = "Map of files or directories (as relative URLs) in the repository that are useful for characterizing the repository and integrating it with the other resources in the cloud map, with optional type constraints."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub contains: ::std::option::Option<TypedUrLs>,
    #[doc = "Default branch name (e.g., main, master)."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub default_branch: ::std::option::Option<::std::string::String>,
    #[doc = "URL of the repository that this repository was forked from."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub fork_of: ::std::option::Option<::std::string::String>,
    #[doc = "Initial commit of the default branch."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub initial_revision: ::std::option::Option<::std::string::String>,
    #[doc = "Internal identifier from the repository host (e.g., GitHub repository ID)."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub internal_id: ::std::option::Option<::std::string::String>,
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub metadata: ::std::option::Option<RepositoryMetadata>,
    #[doc = "URL of the repository that this repository is a mirror of."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub mirror_of: ::std::option::Option<::std::string::String>,
    #[doc = "Repository name."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub name: ::std::option::Option<::std::string::String>,
    #[doc = "Project path relative to base location of git repositories on the host."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub path: ::std::option::Option<::std::string::String>,
    #[doc = "True if the repository is not publicly accessible."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub private: ::std::option::Option<bool>,
    #[doc = "URL to the repository's project page on the host (e.g., <https://github.com/user/repo>)."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub project_url: ::std::option::Option<::std::string::String>,
    #[doc = "List of protocols available to clone the repository (e.g., https, ssh)."]
    #[serde(default, skip_serializing_if = "::std::vec::Vec::is_empty")]
    pub protocols: ::std::vec::Vec<::std::string::String>,
    #[doc = "URL of the service hosting this repository."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub service: ::std::option::Option<::std::string::String>,
    #[doc = "Map of tag names to their commit SHA hashes."]
    #[serde(
        default,
        skip_serializing_if = ":: std :: collections :: HashMap::is_empty"
    )]
    pub tags: ::std::collections::HashMap<::std::string::String, ::std::string::String>,
}
impl ::std::default::Default for Repository {
    fn default() -> Self {
        Self {
            branches: Default::default(),
            contains: Default::default(),
            default_branch: Default::default(),
            fork_of: Default::default(),
            initial_revision: Default::default(),
            internal_id: Default::default(),
            metadata: Default::default(),
            mirror_of: Default::default(),
            name: Default::default(),
            path: Default::default(),
            private: Default::default(),
            project_url: Default::default(),
            protocols: Default::default(),
            service: Default::default(),
            tags: Default::default(),
        }
    }
}
#[doc = "Metadata about the repository that isn't stored in the git repository itself but might be provided by the host (e.g., metadata found on the repository's GitHub or GitLab project page)."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"Metadata about the repository that isn't stored in the git repository itself but might be provided by the host (e.g., metadata found on the repository's GitHub or GitLab project page).\","]
#[doc = "  \"allOf\": ["]
#[doc = "    {"]
#[doc = "      \"$ref\": \"#/definitions/metadata\""]
#[doc = "    },"]
#[doc = "    {"]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"properties\": {"]
#[doc = "        \"issues_url\": {"]
#[doc = "          \"description\": \"URL to the issue tracker for the repository.\","]
#[doc = "          \"type\": \"string\""]
#[doc = "        },"]
#[doc = "        \"lastupdate_digest\": {"]
#[doc = "          \"description\": \"Digest hash of the metadata at the time of the last update.\","]
#[doc = "          \"type\": \"string\""]
#[doc = "        },"]
#[doc = "        \"lastupdate_time\": {"]
#[doc = "          \"description\": \"Timestamp of the last metadata update.\","]
#[doc = "          \"type\": \"string\""]
#[doc = "        },"]
#[doc = "        \"license_url\": {"]
#[doc = "          \"description\": \"URL to the license file or license information.\","]
#[doc = "          \"type\": \"string\""]
#[doc = "        },"]
#[doc = "        \"project_status\": {"]
#[doc = "          \"description\": \"Project status as defined by https://www.repostatus.org\","]
#[doc = "          \"type\": \"string\","]
#[doc = "          \"enum\": ["]
#[doc = "            \"concept\","]
#[doc = "            \"WIP\","]
#[doc = "            \"suspended\","]
#[doc = "            \"abandoned\","]
#[doc = "            \"active\","]
#[doc = "            \"inactive\","]
#[doc = "            \"unsupported\","]
#[doc = "            \"moved\""]
#[doc = "          ]"]
#[doc = "        }"]
#[doc = "      }"]
#[doc = "    }"]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct RepositoryMetadata {
    #[doc = "Date and time on which the resource was created, conforming to RFC 3339."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub created: ::std::option::Option<::chrono::DateTime<::chrono::offset::Utc>>,
    #[doc = "Human-readable description."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub description: ::std::option::Option<::std::string::String>,
    #[doc = "Link to issue, PR/MR, or discussion about this definition."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub discussion_url: ::std::option::Option<::std::string::String>,
    #[doc = "URL to get documentation."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub documentation_url: ::std::option::Option<::std::string::String>,
    #[doc = "URL to the entity this is a fork of."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub fork_of: ::std::option::Option<::std::string::String>,
    #[doc = "URL to find more information."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub homepage_url: ::std::option::Option<::std::string::String>,
    #[doc = "URL to the issue tracker for the repository."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub issues_url: ::std::option::Option<::std::string::String>,
    #[doc = "Digest hash of the metadata at the time of the last update."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub lastupdate_digest: ::std::option::Option<::std::string::String>,
    #[doc = "Timestamp of the last metadata update."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub lastupdate_time: ::std::option::Option<::std::string::String>,
    #[doc = "URL to the license file or license information."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub license_url: ::std::option::Option<::std::string::String>,
    #[doc = "Project status as defined by https://www.repostatus.org"]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub project_status: ::std::option::Option<RepositoryMetadataProjectStatus>,
    #[doc = "Informal pointer to source ref (branch or tag name)."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub source_ref: ::std::option::Option<::std::string::String>,
    #[doc = "Informal pointer to source code revision. Use when deployment information is not available."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub source_revision: ::std::option::Option<::std::string::String>,
    #[doc = "Informal pointer to source code. Use when deployment information is not available."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub source_url: ::std::option::Option<::std::string::String>,
    #[doc = "License(s) as an SPDX License Expression."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub spdx_licenses: ::std::option::Option<::std::string::String>,
    #[doc = "Icon or thumbnail URL."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub thumbnail_url: ::std::option::Option<::std::string::String>,
    #[doc = "Human-readable title."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub title: ::std::option::Option<::std::string::String>,
    #[doc = "List of topic or categories associated with the resource."]
    #[serde(default, skip_serializing_if = "::std::vec::Vec::is_empty")]
    pub topics: ::std::vec::Vec<::std::string::String>,
    #[doc = "Name of the distributing entity, organization, or individual."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub vendor: ::std::option::Option<::std::string::String>,
    #[doc = "Version. The version may match a label or tag in the source code repository or may be Semantic Versioning-compatible."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub version: ::std::option::Option<RepositoryMetadataVersion>,
}
impl ::std::default::Default for RepositoryMetadata {
    fn default() -> Self {
        Self {
            created: Default::default(),
            description: Default::default(),
            discussion_url: Default::default(),
            documentation_url: Default::default(),
            fork_of: Default::default(),
            homepage_url: Default::default(),
            issues_url: Default::default(),
            lastupdate_digest: Default::default(),
            lastupdate_time: Default::default(),
            license_url: Default::default(),
            project_status: Default::default(),
            source_ref: Default::default(),
            source_revision: Default::default(),
            source_url: Default::default(),
            spdx_licenses: Default::default(),
            thumbnail_url: Default::default(),
            title: Default::default(),
            topics: Default::default(),
            vendor: Default::default(),
            version: Default::default(),
        }
    }
}
#[doc = "Project status as defined by https://www.repostatus.org"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"Project status as defined by https://www.repostatus.org\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"enum\": ["]
#[doc = "    \"concept\","]
#[doc = "    \"WIP\","]
#[doc = "    \"suspended\","]
#[doc = "    \"abandoned\","]
#[doc = "    \"active\","]
#[doc = "    \"inactive\","]
#[doc = "    \"unsupported\","]
#[doc = "    \"moved\""]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(
    :: serde :: Deserialize,
    :: serde :: Serialize,
    Clone,
    Copy,
    Debug,
    Eq,
    Hash,
    Ord,
    PartialEq,
    PartialOrd,
)]
pub enum RepositoryMetadataProjectStatus {
    #[serde(rename = "concept")]
    Concept,
    #[serde(rename = "WIP")]
    Wip,
    #[serde(rename = "suspended")]
    Suspended,
    #[serde(rename = "abandoned")]
    Abandoned,
    #[serde(rename = "active")]
    Active,
    #[serde(rename = "inactive")]
    Inactive,
    #[serde(rename = "unsupported")]
    Unsupported,
    #[serde(rename = "moved")]
    Moved,
}
impl ::std::fmt::Display for RepositoryMetadataProjectStatus {
    fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
        match *self {
            Self::Concept => f.write_str("concept"),
            Self::Wip => f.write_str("WIP"),
            Self::Suspended => f.write_str("suspended"),
            Self::Abandoned => f.write_str("abandoned"),
            Self::Active => f.write_str("active"),
            Self::Inactive => f.write_str("inactive"),
            Self::Unsupported => f.write_str("unsupported"),
            Self::Moved => f.write_str("moved"),
        }
    }
}
impl ::std::str::FromStr for RepositoryMetadataProjectStatus {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        match value {
            "concept" => Ok(Self::Concept),
            "WIP" => Ok(Self::Wip),
            "suspended" => Ok(Self::Suspended),
            "abandoned" => Ok(Self::Abandoned),
            "active" => Ok(Self::Active),
            "inactive" => Ok(Self::Inactive),
            "unsupported" => Ok(Self::Unsupported),
            "moved" => Ok(Self::Moved),
            _ => Err("invalid value".into()),
        }
    }
}
impl ::std::convert::TryFrom<&str> for RepositoryMetadataProjectStatus {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for RepositoryMetadataProjectStatus {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for RepositoryMetadataProjectStatus {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
#[doc = "Version. The version may match a label or tag in the source code repository or may be Semantic Versioning-compatible."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"Version. The version may match a label or tag in the source code repository or may be Semantic Versioning-compatible.\","]
#[doc = "  \"anyOf\": ["]
#[doc = "    {"]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    {"]
#[doc = "      \"type\": \"number\""]
#[doc = "    }"]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
#[serde(untagged)]
pub enum RepositoryMetadataVersion {
    String(::std::string::String),
    Number(f64),
}
impl ::std::fmt::Display for RepositoryMetadataVersion {
    fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
        match self {
            Self::String(x) => x.fmt(f),
            Self::Number(x) => x.fmt(f),
        }
    }
}
impl ::std::convert::From<f64> for RepositoryMetadataVersion {
    fn from(value: f64) -> Self {
        Self::Number(value)
    }
}
#[doc = "`Service`"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"title\": \"Service\","]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"properties\": {"]
#[doc = "    \"access\": {"]
#[doc = "      \"description\": \"Access to the service (who can resolve the URL).\","]
#[doc = "      \"type\": \"string\","]
#[doc = "      \"enum\": ["]
#[doc = "        \"public\","]
#[doc = "        \"private\","]
#[doc = "        \"none\""]
#[doc = "      ]"]
#[doc = "    },"]
#[doc = "    \"connections\": {"]
#[doc = "      \"description\": \"Services this service connects to during operation.\","]
#[doc = "      \"$ref\": \"#/definitions/typedURLs\""]
#[doc = "    },"]
#[doc = "    \"discovery\": {"]
#[doc = "      \"$ref\": \"#/definitions/discovery\""]
#[doc = "    },"]
#[doc = "    \"endpoints\": {"]
#[doc = "      \"description\": \"Service endpoints.\","]
#[doc = "      \"$ref\": \"#/definitions/typedURLs\""]
#[doc = "    },"]
#[doc = "    \"instantiated_by\": {"]
#[doc = "      \"description\": \"URLs referencing instantiations that created or validated this service.\","]
#[doc = "      \"$ref\": \"#/definitions/typedURLs\""]
#[doc = "    },"]
#[doc = "    \"metadata\": {"]
#[doc = "      \"description\": \"Human-readable metadata about the service.\","]
#[doc = "      \"$ref\": \"#/definitions/metadata\""]
#[doc = "    },"]
#[doc = "    \"policies\": {"]
#[doc = "      \"description\": \"Service policies and legal information.\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"properties\": {"]
#[doc = "        \"privacy_policy\": {"]
#[doc = "          \"description\": \"URL to the privacy policy.\","]
#[doc = "          \"type\": \"string\""]
#[doc = "        },"]
#[doc = "        \"spdx_licenses\": {"]
#[doc = "          \"description\": \"License(s) under which the service is distributed as an SPDX License Expression.\","]
#[doc = "          \"type\": \"string\""]
#[doc = "        },"]
#[doc = "        \"terms_of_service\": {"]
#[doc = "          \"description\": \"URL to the terms of service.\","]
#[doc = "          \"type\": \"string\""]
#[doc = "        }"]
#[doc = "      }"]
#[doc = "    },"]
#[doc = "    \"release_schedule\": {"]
#[doc = "      \"$ref\": \"#/definitions/release_schedule\""]
#[doc = "    },"]
#[doc = "    \"status\": {"]
#[doc = "      \"description\": \"Lifecycle status of the service.\","]
#[doc = "      \"$ref\": \"#/definitions/lifecycle_status\""]
#[doc = "    },"]
#[doc = "    \"type\": {"]
#[doc = "      \"description\": \"Type identifier from types/services with optional version constraints.\","]
#[doc = "      \"$ref\": \"#/definitions/typeRef\""]
#[doc = "    },"]
#[doc = "    \"versions\": {"]
#[doc = "      \"description\": \"Services that are variants of this service (for example, different versions or environments). Each service inherits the metadata of this one unless overridden in its declaration. Identifiers should share the base URL as this service.\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"additionalProperties\": {"]
#[doc = "        \"$ref\": \"#/definitions/service\""]
#[doc = "      },"]
#[doc = "      \"propertyNames\": {"]
#[doc = "        \"description\": \"URL of the service variant.\","]
#[doc = "        \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "      }"]
#[doc = "    }"]
#[doc = "  },"]
#[doc = "  \"additionalProperties\": true,"]
#[doc = "  \"$$target\": \"#/definitions/service\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct Service {
    #[doc = "Access to the service (who can resolve the URL)."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub access: ::std::option::Option<ServiceAccess>,
    #[doc = "Services this service connects to during operation."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub connections: ::std::option::Option<TypedUrLs>,
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub discovery: ::std::option::Option<Discovery>,
    #[doc = "Service endpoints."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub endpoints: ::std::option::Option<TypedUrLs>,
    #[doc = "URLs referencing instantiations that created or validated this service."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub instantiated_by: ::std::option::Option<TypedUrLs>,
    #[doc = "Human-readable metadata about the service."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub metadata: ::std::option::Option<Metadata>,
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub policies: ::std::option::Option<ServicePolicies>,
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub release_schedule: ::std::option::Option<ReleaseSchedule>,
    #[doc = "Lifecycle status of the service."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub status: ::std::option::Option<LifecycleStatus>,
    #[doc = "Type identifier from types/services with optional version constraints."]
    #[serde(
        rename = "type",
        default,
        skip_serializing_if = "::std::option::Option::is_none"
    )]
    pub type_: ::std::option::Option<TypeRef>,
    #[doc = "Services that are variants of this service (for example, different versions or environments). Each service inherits the metadata of this one unless overridden in its declaration. Identifiers should share the base URL as this service."]
    #[serde(
        default,
        skip_serializing_if = ":: std :: collections :: HashMap::is_empty"
    )]
    pub versions: ::std::collections::HashMap<ServiceVersionsKey, Service>,
}
impl ::std::default::Default for Service {
    fn default() -> Self {
        Self {
            access: Default::default(),
            connections: Default::default(),
            discovery: Default::default(),
            endpoints: Default::default(),
            instantiated_by: Default::default(),
            metadata: Default::default(),
            policies: Default::default(),
            release_schedule: Default::default(),
            status: Default::default(),
            type_: Default::default(),
            versions: Default::default(),
        }
    }
}
#[doc = "Access to the service (who can resolve the URL)."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"Access to the service (who can resolve the URL).\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"enum\": ["]
#[doc = "    \"public\","]
#[doc = "    \"private\","]
#[doc = "    \"none\""]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(
    :: serde :: Deserialize,
    :: serde :: Serialize,
    Clone,
    Copy,
    Debug,
    Eq,
    Hash,
    Ord,
    PartialEq,
    PartialOrd,
)]
pub enum ServiceAccess {
    #[serde(rename = "public")]
    Public,
    #[serde(rename = "private")]
    Private,
    #[serde(rename = "none")]
    None,
}
impl ::std::fmt::Display for ServiceAccess {
    fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
        match *self {
            Self::Public => f.write_str("public"),
            Self::Private => f.write_str("private"),
            Self::None => f.write_str("none"),
        }
    }
}
impl ::std::str::FromStr for ServiceAccess {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        match value {
            "public" => Ok(Self::Public),
            "private" => Ok(Self::Private),
            "none" => Ok(Self::None),
            _ => Err("invalid value".into()),
        }
    }
}
impl ::std::convert::TryFrom<&str> for ServiceAccess {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for ServiceAccess {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for ServiceAccess {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
#[doc = "Service policies and legal information."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"Service policies and legal information.\","]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"properties\": {"]
#[doc = "    \"privacy_policy\": {"]
#[doc = "      \"description\": \"URL to the privacy policy.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"spdx_licenses\": {"]
#[doc = "      \"description\": \"License(s) under which the service is distributed as an SPDX License Expression.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"terms_of_service\": {"]
#[doc = "      \"description\": \"URL to the terms of service.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    }"]
#[doc = "  }"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct ServicePolicies {
    #[doc = "URL to the privacy policy."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub privacy_policy: ::std::option::Option<::std::string::String>,
    #[doc = "License(s) under which the service is distributed as an SPDX License Expression."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub spdx_licenses: ::std::option::Option<::std::string::String>,
    #[doc = "URL to the terms of service."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub terms_of_service: ::std::option::Option<::std::string::String>,
}
impl ::std::default::Default for ServicePolicies {
    fn default() -> Self {
        Self {
            privacy_policy: Default::default(),
            spdx_licenses: Default::default(),
            terms_of_service: Default::default(),
        }
    }
}
#[doc = "URL of the service variant."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"URL of the service variant.\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"pattern\": \"^[^\\\\s]*$\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Serialize, Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
#[serde(transparent)]
pub struct ServiceVersionsKey(::std::string::String);
impl ::std::ops::Deref for ServiceVersionsKey {
    type Target = ::std::string::String;
    fn deref(&self) -> &::std::string::String {
        &self.0
    }
}
impl ::std::convert::From<ServiceVersionsKey> for ::std::string::String {
    fn from(value: ServiceVersionsKey) -> Self {
        value.0
    }
}
impl ::std::str::FromStr for ServiceVersionsKey {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        static PATTERN: ::std::sync::LazyLock<::regress::Regex> =
            ::std::sync::LazyLock::new(|| ::regress::Regex::new("^[^\\s]*$").unwrap());
        if PATTERN.find(value).is_none() {
            return Err("doesn't match pattern \"^[^\\s]*$\"".into());
        }
        Ok(Self(value.to_string()))
    }
}
impl ::std::convert::TryFrom<&str> for ServiceVersionsKey {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for ServiceVersionsKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for ServiceVersionsKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl<'de> ::serde::Deserialize<'de> for ServiceVersionsKey {
    fn deserialize<D>(deserializer: D) -> ::std::result::Result<Self, D::Error>
    where
        D: ::serde::Deserializer<'de>,
    {
        ::std::string::String::deserialize(deserializer)?
            .parse()
            .map_err(|e: self::error::ConversionError| {
                <D::Error as ::serde::de::Error>::custom(e.to_string())
            })
    }
}
#[doc = "`Type`"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"properties\": {"]
#[doc = "    \"extends\": {"]
#[doc = "      \"description\": \"List of fully-qualified type names that this type extends.\","]
#[doc = "      \"type\": \"array\","]
#[doc = "      \"items\": {"]
#[doc = "        \"type\": \"string\""]
#[doc = "      }"]
#[doc = "    },"]
#[doc = "    \"kind\": {"]
#[doc = "      \"description\": \"The kind of the type. One of: Component, Artifact, or Capability.\","]
#[doc = "      \"type\": \"string\","]
#[doc = "      \"enum\": ["]
#[doc = "        \"Component\","]
#[doc = "        \"Artifact\","]
#[doc = "        \"Capability\""]
#[doc = "      ]"]
#[doc = "    },"]
#[doc = "    \"metadata\": {"]
#[doc = "      \"description\": \"Additional metadata about the type.\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"$ref\": \"#/definitions/metadata\""]
#[doc = "    },"]
#[doc = "    \"model\": {"]
#[doc = "      \"description\": \"URL of artifact or service to use a model for instances of this type.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"properties\": {"]
#[doc = "      \"description\": \"JSON Schema describing the properties of instances of this type.\","]
#[doc = "      \"type\": \"object\""]
#[doc = "    },"]
#[doc = "    \"source\": {"]
#[doc = "      \"description\": \"Artifact containing type definition. Include if it cannot be derived from the type name.\","]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    \"status\": {"]
#[doc = "      \"description\": \"Maturity level of the type definition.\","]
#[doc = "      \"type\": \"string\","]
#[doc = "      \"enum\": ["]
#[doc = "        \"draft\","]
#[doc = "        \"experimental\","]
#[doc = "        \"stable\","]
#[doc = "        \"deprecated\","]
#[doc = "        \"removed\""]
#[doc = "      ]"]
#[doc = "    }"]
#[doc = "  },"]
#[doc = "  \"additionalProperties\": true,"]
#[doc = "  \"$$target\": \"#/definitions/type\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct Type {
    #[doc = "List of fully-qualified type names that this type extends."]
    #[serde(default, skip_serializing_if = "::std::vec::Vec::is_empty")]
    pub extends: ::std::vec::Vec<::std::string::String>,
    #[doc = "The kind of the type. One of: Component, Artifact, or Capability."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub kind: ::std::option::Option<TypeKind>,
    #[doc = "Additional metadata about the type."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub metadata: ::std::option::Option<Metadata>,
    #[doc = "URL of artifact or service to use a model for instances of this type."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub model: ::std::option::Option<::std::string::String>,
    #[doc = "JSON Schema describing the properties of instances of this type."]
    #[serde(default, skip_serializing_if = "::serde_json::Map::is_empty")]
    pub properties: ::serde_json::Map<::std::string::String, ::serde_json::Value>,
    #[doc = "Artifact containing type definition. Include if it cannot be derived from the type name."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub source: ::std::option::Option<::std::string::String>,
    #[doc = "Maturity level of the type definition."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub status: ::std::option::Option<TypeStatus>,
}
impl ::std::default::Default for Type {
    fn default() -> Self {
        Self {
            extends: Default::default(),
            kind: Default::default(),
            metadata: Default::default(),
            model: Default::default(),
            properties: Default::default(),
            source: Default::default(),
            status: Default::default(),
        }
    }
}
#[doc = "The kind of the type. One of: Component, Artifact, or Capability."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"The kind of the type. One of: Component, Artifact, or Capability.\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"enum\": ["]
#[doc = "    \"Component\","]
#[doc = "    \"Artifact\","]
#[doc = "    \"Capability\""]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(
    :: serde :: Deserialize,
    :: serde :: Serialize,
    Clone,
    Copy,
    Debug,
    Eq,
    Hash,
    Ord,
    PartialEq,
    PartialOrd,
)]
pub enum TypeKind {
    Component,
    Artifact,
    Capability,
}
impl ::std::fmt::Display for TypeKind {
    fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
        match *self {
            Self::Component => f.write_str("Component"),
            Self::Artifact => f.write_str("Artifact"),
            Self::Capability => f.write_str("Capability"),
        }
    }
}
impl ::std::str::FromStr for TypeKind {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        match value {
            "Component" => Ok(Self::Component),
            "Artifact" => Ok(Self::Artifact),
            "Capability" => Ok(Self::Capability),
            _ => Err("invalid value".into()),
        }
    }
}
impl ::std::convert::TryFrom<&str> for TypeKind {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for TypeKind {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for TypeKind {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
#[doc = "Type references with optional constraints. Keys are type names, values are either null or objects with constraint properties such as version."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"title\": \"Type Ref\","]
#[doc = "  \"description\": \"Type references with optional constraints. Keys are type names, values are either null or objects with constraint properties such as version.\","]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"additionalProperties\": {"]
#[doc = "    \"oneOf\": ["]
#[doc = "      {"]
#[doc = "        \"description\": \"No constraints on the type.\","]
#[doc = "        \"type\": \"null\""]
#[doc = "      },"]
#[doc = "      {"]
#[doc = "        \"description\": \"Constraints on the type.\","]
#[doc = "        \"type\": \"object\","]
#[doc = "        \"allOf\": ["]
#[doc = "          {"]
#[doc = "            \"$ref\": \"#/definitions/relationships\""]
#[doc = "          },"]
#[doc = "          {"]
#[doc = "            \"properties\": {"]
#[doc = "              \"metadata\": {"]
#[doc = "                \"description\": \"Additional metadata about the type reference.\","]
#[doc = "                \"type\": \"object\""]
#[doc = "              },"]
#[doc = "              \"model\": {"]
#[doc = "                \"description\": \"URL of artifact or service model that constrains this type reference.\","]
#[doc = "                \"type\": \"string\""]
#[doc = "              },"]
#[doc = "              \"properties\": {"]
#[doc = "                \"description\": \"Constraints on instance properties that are associated with the type. Actual interpretation of properties is entirely up to the type definition.\","]
#[doc = "                \"type\": \"object\""]
#[doc = "              },"]
#[doc = "              \"status\": {"]
#[doc = "                \"description\": \"Status of the instance or capability.\","]
#[doc = "                \"type\": \"string\","]
#[doc = "                \"enum\": ["]
#[doc = "                  \"unknown\","]
#[doc = "                  \"absent\","]
#[doc = "                  \"present\","]
#[doc = "                  \"failed\","]
#[doc = "                  \"validated\""]
#[doc = "                ]"]
#[doc = "              },"]
#[doc = "              \"version\": {"]
#[doc = "                \"description\": \"Version constraint for the type.\","]
#[doc = "                \"anyOf\": ["]
#[doc = "                  {"]
#[doc = "                    \"type\": \"string\""]
#[doc = "                  },"]
#[doc = "                  {"]
#[doc = "                    \"type\": \"number\""]
#[doc = "                  }"]
#[doc = "                ]"]
#[doc = "              }"]
#[doc = "            }"]
#[doc = "          }"]
#[doc = "        ]"]
#[doc = "      }"]
#[doc = "    ]"]
#[doc = "  },"]
#[doc = "  \"propertyNames\": {"]
#[doc = "    \"description\": \"Type name (e.g., software.Nginx, capabilities.GitOps). Not a URL (no scheme prefix) or a URI template (doesn't start with an expression).\","]
#[doc = "    \"pattern\": \"^(?![A-Za-z][A-Za-z0-9+.-]*:)(?!\\\\{)[^\\\\s]+$\""]
#[doc = "  },"]
#[doc = "  \"$$target\": \"#/definitions/typeRef\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
#[serde(transparent)]
pub struct TypeRef(
    pub ::std::collections::HashMap<TypeRefKey, ::std::option::Option<TypeRefValue>>,
);
impl ::std::ops::Deref for TypeRef {
    type Target = ::std::collections::HashMap<TypeRefKey, ::std::option::Option<TypeRefValue>>;
    fn deref(
        &self,
    ) -> &::std::collections::HashMap<TypeRefKey, ::std::option::Option<TypeRefValue>> {
        &self.0
    }
}
impl ::std::convert::From<TypeRef>
    for ::std::collections::HashMap<TypeRefKey, ::std::option::Option<TypeRefValue>>
{
    fn from(value: TypeRef) -> Self {
        value.0
    }
}
impl
    ::std::convert::From<
        ::std::collections::HashMap<TypeRefKey, ::std::option::Option<TypeRefValue>>,
    > for TypeRef
{
    fn from(
        value: ::std::collections::HashMap<TypeRefKey, ::std::option::Option<TypeRefValue>>,
    ) -> Self {
        Self(value)
    }
}
#[doc = "Type name (e.g., software.Nginx, capabilities.GitOps). Not a URL (no scheme prefix) or a URI template (doesn't start with an expression)."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"Type name (e.g., software.Nginx, capabilities.GitOps). Not a URL (no scheme prefix) or a URI template (doesn't start with an expression).\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"pattern\": \"^(?![A-Za-z][A-Za-z0-9+.-]*:)(?!\\\\{)[^\\\\s]+$\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Serialize, Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
#[serde(transparent)]
pub struct TypeRefKey(::std::string::String);
impl ::std::ops::Deref for TypeRefKey {
    type Target = ::std::string::String;
    fn deref(&self) -> &::std::string::String {
        &self.0
    }
}
impl ::std::convert::From<TypeRefKey> for ::std::string::String {
    fn from(value: TypeRefKey) -> Self {
        value.0
    }
}
impl ::std::str::FromStr for TypeRefKey {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        static PATTERN: ::std::sync::LazyLock<::regress::Regex> =
            ::std::sync::LazyLock::new(|| {
                ::regress::Regex::new("^(?![A-Za-z][A-Za-z0-9+.-]*:)(?!\\{)[^\\s]+$").unwrap()
            });
        if PATTERN.find(value).is_none() {
            return Err(
                "doesn't match pattern \"^(?![A-Za-z][A-Za-z0-9+.-]*:)(?!\\{)[^\\s]+$\"".into(),
            );
        }
        Ok(Self(value.to_string()))
    }
}
impl ::std::convert::TryFrom<&str> for TypeRefKey {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for TypeRefKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for TypeRefKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl<'de> ::serde::Deserialize<'de> for TypeRefKey {
    fn deserialize<D>(deserializer: D) -> ::std::result::Result<Self, D::Error>
    where
        D: ::serde::Deserializer<'de>,
    {
        ::std::string::String::deserialize(deserializer)?
            .parse()
            .map_err(|e: self::error::ConversionError| {
                <D::Error as ::serde::de::Error>::custom(e.to_string())
            })
    }
}
#[doc = "Constraints on the type."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"Constraints on the type.\","]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"allOf\": ["]
#[doc = "    {"]
#[doc = "      \"$ref\": \"#/definitions/relationships\""]
#[doc = "    },"]
#[doc = "    {"]
#[doc = "      \"properties\": {"]
#[doc = "        \"metadata\": {"]
#[doc = "          \"description\": \"Additional metadata about the type reference.\","]
#[doc = "          \"type\": \"object\""]
#[doc = "        },"]
#[doc = "        \"model\": {"]
#[doc = "          \"description\": \"URL of artifact or service model that constrains this type reference.\","]
#[doc = "          \"type\": \"string\""]
#[doc = "        },"]
#[doc = "        \"properties\": {"]
#[doc = "          \"description\": \"Constraints on instance properties that are associated with the type. Actual interpretation of properties is entirely up to the type definition.\","]
#[doc = "          \"type\": \"object\""]
#[doc = "        },"]
#[doc = "        \"status\": {"]
#[doc = "          \"description\": \"Status of the instance or capability.\","]
#[doc = "          \"type\": \"string\","]
#[doc = "          \"enum\": ["]
#[doc = "            \"unknown\","]
#[doc = "            \"absent\","]
#[doc = "            \"present\","]
#[doc = "            \"failed\","]
#[doc = "            \"validated\""]
#[doc = "          ]"]
#[doc = "        },"]
#[doc = "        \"version\": {"]
#[doc = "          \"description\": \"Version constraint for the type.\","]
#[doc = "          \"anyOf\": ["]
#[doc = "            {"]
#[doc = "              \"type\": \"string\""]
#[doc = "            },"]
#[doc = "            {"]
#[doc = "              \"type\": \"number\""]
#[doc = "            }"]
#[doc = "          ]"]
#[doc = "        }"]
#[doc = "      }"]
#[doc = "    }"]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
pub struct TypeRefValue {
    #[doc = "Map of URLs of interesting artifacts that this record embeds or incorporates."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub contains: ::std::option::Option<TypedUrLs>,
    #[doc = "Build-time or run-time, dependencies the user may provide or configure. if url, it could be service use needs an account on or a default. Software, services, or environment context that the instantiation may depend on. Keys are labels or URLs, values are type constraints of components or capabilities. Non-exhaustive: for example, the artifact type may imply additional requirements or some dependencies might be optional."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub dependencies: ::std::option::Option<TypedUrLs>,
    #[doc = "Map of URLs (or labels) of entities (e.g., software package, service image or template, capabilities pipeline, build tools) that this artifact instantiates with optional type constraints."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub instantiates: ::std::option::Option<TypedUrLs>,
    #[doc = "Additional metadata about the type reference."]
    #[serde(default, skip_serializing_if = "::serde_json::Map::is_empty")]
    pub metadata: ::serde_json::Map<::std::string::String, ::serde_json::Value>,
    #[doc = "URL of artifact or service model that constrains this type reference."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub model: ::std::option::Option<::std::string::String>,
    #[doc = "Constraints on instance properties that are associated with the type. Actual interpretation of properties is entirely up to the type definition."]
    #[serde(default, skip_serializing_if = "::serde_json::Map::is_empty")]
    pub properties: ::serde_json::Map<::std::string::String, ::serde_json::Value>,
    #[doc = "(Build-time or run-time) Map of URLs of interesting artifacts, repositories or services that this artifact may reference when executed or instantiated."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub references: ::std::option::Option<TypedUrLs>,
    #[doc = "Status of the instance or capability."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub status: ::std::option::Option<TypeRefValueStatus>,
    #[doc = "Version constraint for the type."]
    #[serde(default, skip_serializing_if = "::std::option::Option::is_none")]
    pub version: ::std::option::Option<TypeRefValueVersion>,
}
impl ::std::default::Default for TypeRefValue {
    fn default() -> Self {
        Self {
            contains: Default::default(),
            dependencies: Default::default(),
            instantiates: Default::default(),
            metadata: Default::default(),
            model: Default::default(),
            properties: Default::default(),
            references: Default::default(),
            status: Default::default(),
            version: Default::default(),
        }
    }
}
#[doc = "Status of the instance or capability."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"Status of the instance or capability.\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"enum\": ["]
#[doc = "    \"unknown\","]
#[doc = "    \"absent\","]
#[doc = "    \"present\","]
#[doc = "    \"failed\","]
#[doc = "    \"validated\""]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(
    :: serde :: Deserialize,
    :: serde :: Serialize,
    Clone,
    Copy,
    Debug,
    Eq,
    Hash,
    Ord,
    PartialEq,
    PartialOrd,
)]
pub enum TypeRefValueStatus {
    #[serde(rename = "unknown")]
    Unknown,
    #[serde(rename = "absent")]
    Absent,
    #[serde(rename = "present")]
    Present,
    #[serde(rename = "failed")]
    Failed,
    #[serde(rename = "validated")]
    Validated,
}
impl ::std::fmt::Display for TypeRefValueStatus {
    fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
        match *self {
            Self::Unknown => f.write_str("unknown"),
            Self::Absent => f.write_str("absent"),
            Self::Present => f.write_str("present"),
            Self::Failed => f.write_str("failed"),
            Self::Validated => f.write_str("validated"),
        }
    }
}
impl ::std::str::FromStr for TypeRefValueStatus {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        match value {
            "unknown" => Ok(Self::Unknown),
            "absent" => Ok(Self::Absent),
            "present" => Ok(Self::Present),
            "failed" => Ok(Self::Failed),
            "validated" => Ok(Self::Validated),
            _ => Err("invalid value".into()),
        }
    }
}
impl ::std::convert::TryFrom<&str> for TypeRefValueStatus {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for TypeRefValueStatus {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for TypeRefValueStatus {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
#[doc = "Version constraint for the type."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"Version constraint for the type.\","]
#[doc = "  \"anyOf\": ["]
#[doc = "    {"]
#[doc = "      \"type\": \"string\""]
#[doc = "    },"]
#[doc = "    {"]
#[doc = "      \"type\": \"number\""]
#[doc = "    }"]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
#[serde(untagged)]
pub enum TypeRefValueVersion {
    String(::std::string::String),
    Number(f64),
}
impl ::std::fmt::Display for TypeRefValueVersion {
    fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
        match self {
            Self::String(x) => x.fmt(f),
            Self::Number(x) => x.fmt(f),
        }
    }
}
impl ::std::convert::From<f64> for TypeRefValueVersion {
    fn from(value: f64) -> Self {
        Self::Number(value)
    }
}
#[doc = "Maturity level of the type definition."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"Maturity level of the type definition.\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"enum\": ["]
#[doc = "    \"draft\","]
#[doc = "    \"experimental\","]
#[doc = "    \"stable\","]
#[doc = "    \"deprecated\","]
#[doc = "    \"removed\""]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(
    :: serde :: Deserialize,
    :: serde :: Serialize,
    Clone,
    Copy,
    Debug,
    Eq,
    Hash,
    Ord,
    PartialEq,
    PartialOrd,
)]
pub enum TypeStatus {
    #[serde(rename = "draft")]
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
impl ::std::fmt::Display for TypeStatus {
    fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
        match *self {
            Self::Draft => f.write_str("draft"),
            Self::Experimental => f.write_str("experimental"),
            Self::Stable => f.write_str("stable"),
            Self::Deprecated => f.write_str("deprecated"),
            Self::Removed => f.write_str("removed"),
        }
    }
}
impl ::std::str::FromStr for TypeStatus {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        match value {
            "draft" => Ok(Self::Draft),
            "experimental" => Ok(Self::Experimental),
            "stable" => Ok(Self::Stable),
            "deprecated" => Ok(Self::Deprecated),
            "removed" => Ok(Self::Removed),
            _ => Err("invalid value".into()),
        }
    }
}
impl ::std::convert::TryFrom<&str> for TypeStatus {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for TypeStatus {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for TypeStatus {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
#[doc = "Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or \"metadata\". Alternatively, keys can be labels and its value a nested typed URL map."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"Map of URLs with optional type references. Keys are URLs, values are type references with optional constraints or \\\"metadata\\\". Alternatively, keys can be labels and its value a nested typed URL map.\","]
#[doc = "  \"type\": \"object\","]
#[doc = "  \"additionalProperties\": {"]
#[doc = "    \"oneOf\": ["]
#[doc = "      {"]
#[doc = "        \"type\": \"null\""]
#[doc = "      },"]
#[doc = "      {"]
#[doc = "        \"type\": \"object\","]
#[doc = "        \"$ref\": \"#/definitions/typeRef\""]
#[doc = "      },"]
#[doc = "      {"]
#[doc = "        \"description\": \"Nested map for a label key: URLs to type references.\","]
#[doc = "        \"type\": \"object\","]
#[doc = "        \"additionalProperties\": {"]
#[doc = "          \"oneOf\": ["]
#[doc = "            {"]
#[doc = "              \"type\": \"null\""]
#[doc = "            },"]
#[doc = "            {"]
#[doc = "              \"type\": \"object\","]
#[doc = "              \"$ref\": \"#/definitions/typeRef\""]
#[doc = "            }"]
#[doc = "          ]"]
#[doc = "        },"]
#[doc = "        \"propertyNames\": {"]
#[doc = "          \"description\": \"URL (starts with a scheme) or a URI template expression that can expand into one (starts with \\\"{\\\"); i.e. anything that isn't a type name.\","]
#[doc = "          \"pattern\": \"^(?!(?![A-Za-z][A-Za-z0-9+.-]*:)(?!\\\\{)[^\\\\s]+$)[^\\\\s]+$\""]
#[doc = "        }"]
#[doc = "      }"]
#[doc = "    ]"]
#[doc = "  },"]
#[doc = "  \"propertyNames\": {"]
#[doc = "    \"description\": \"URL or label\","]
#[doc = "    \"pattern\": \"^[^\\\\s]+$\""]
#[doc = "  },"]
#[doc = "  \"$$target\": \"#/definitions/typedURLs\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
#[serde(transparent)]
pub struct TypedUrLs(pub ::std::collections::HashMap<TypedUrLsKey, TypedUrLsValue>);
impl ::std::ops::Deref for TypedUrLs {
    type Target = ::std::collections::HashMap<TypedUrLsKey, TypedUrLsValue>;
    fn deref(&self) -> &::std::collections::HashMap<TypedUrLsKey, TypedUrLsValue> {
        &self.0
    }
}
impl ::std::convert::From<TypedUrLs> for ::std::collections::HashMap<TypedUrLsKey, TypedUrLsValue> {
    fn from(value: TypedUrLs) -> Self {
        value.0
    }
}
impl ::std::convert::From<::std::collections::HashMap<TypedUrLsKey, TypedUrLsValue>> for TypedUrLs {
    fn from(value: ::std::collections::HashMap<TypedUrLsKey, TypedUrLsValue>) -> Self {
        Self(value)
    }
}
#[doc = "URL or label"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"URL or label\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"pattern\": \"^[^\\\\s]+$\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Serialize, Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
#[serde(transparent)]
pub struct TypedUrLsKey(::std::string::String);
impl ::std::ops::Deref for TypedUrLsKey {
    type Target = ::std::string::String;
    fn deref(&self) -> &::std::string::String {
        &self.0
    }
}
impl ::std::convert::From<TypedUrLsKey> for ::std::string::String {
    fn from(value: TypedUrLsKey) -> Self {
        value.0
    }
}
impl ::std::str::FromStr for TypedUrLsKey {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        static PATTERN: ::std::sync::LazyLock<::regress::Regex> =
            ::std::sync::LazyLock::new(|| ::regress::Regex::new("^[^\\s]+$").unwrap());
        if PATTERN.find(value).is_none() {
            return Err("doesn't match pattern \"^[^\\s]+$\"".into());
        }
        Ok(Self(value.to_string()))
    }
}
impl ::std::convert::TryFrom<&str> for TypedUrLsKey {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for TypedUrLsKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for TypedUrLsKey {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl<'de> ::serde::Deserialize<'de> for TypedUrLsKey {
    fn deserialize<D>(deserializer: D) -> ::std::result::Result<Self, D::Error>
    where
        D: ::serde::Deserializer<'de>,
    {
        ::std::string::String::deserialize(deserializer)?
            .parse()
            .map_err(|e: self::error::ConversionError| {
                <D::Error as ::serde::de::Error>::custom(e.to_string())
            })
    }
}
#[doc = "`TypedUrLsValue`"]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"oneOf\": ["]
#[doc = "    {"]
#[doc = "      \"type\": \"null\""]
#[doc = "    },"]
#[doc = "    {"]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"$ref\": \"#/definitions/typeRef\""]
#[doc = "    },"]
#[doc = "    {"]
#[doc = "      \"description\": \"Nested map for a label key: URLs to type references.\","]
#[doc = "      \"type\": \"object\","]
#[doc = "      \"additionalProperties\": {"]
#[doc = "        \"oneOf\": ["]
#[doc = "          {"]
#[doc = "            \"type\": \"null\""]
#[doc = "          },"]
#[doc = "          {"]
#[doc = "            \"type\": \"object\","]
#[doc = "            \"$ref\": \"#/definitions/typeRef\""]
#[doc = "          }"]
#[doc = "        ]"]
#[doc = "      },"]
#[doc = "      \"propertyNames\": {"]
#[doc = "        \"description\": \"URL (starts with a scheme) or a URI template expression that can expand into one (starts with \\\"{\\\"); i.e. anything that isn't a type name.\","]
#[doc = "        \"pattern\": \"^(?!(?![A-Za-z][A-Za-z0-9+.-]*:)(?!\\\\{)[^\\\\s]+$)[^\\\\s]+$\""]
#[doc = "      }"]
#[doc = "    }"]
#[doc = "  ]"]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Deserialize, :: serde :: Serialize, Clone, Debug)]
#[serde(untagged)]
pub enum TypedUrLsValue {
    Variant0,
    Variant1(TypeRef),
    Variant2(
        ::std::collections::HashMap<TypedUrLsValueVariant2Key, ::std::option::Option<TypeRef>>,
    ),
}
impl ::std::convert::From<TypeRef> for TypedUrLsValue {
    fn from(value: TypeRef) -> Self {
        Self::Variant1(value)
    }
}
impl
    ::std::convert::From<
        ::std::collections::HashMap<TypedUrLsValueVariant2Key, ::std::option::Option<TypeRef>>,
    > for TypedUrLsValue
{
    fn from(
        value: ::std::collections::HashMap<
            TypedUrLsValueVariant2Key,
            ::std::option::Option<TypeRef>,
        >,
    ) -> Self {
        Self::Variant2(value)
    }
}
#[doc = "URL (starts with a scheme) or a URI template expression that can expand into one (starts with \"{\"); i.e. anything that isn't a type name."]
#[doc = r""]
#[doc = r" <details><summary>JSON schema</summary>"]
#[doc = r""]
#[doc = r" ```json"]
#[doc = "{"]
#[doc = "  \"description\": \"URL (starts with a scheme) or a URI template expression that can expand into one (starts with \\\"{\\\"); i.e. anything that isn't a type name.\","]
#[doc = "  \"type\": \"string\","]
#[doc = "  \"pattern\": \"^(?!(?![A-Za-z][A-Za-z0-9+.-]*:)(?!\\\\{)[^\\\\s]+$)[^\\\\s]+$\""]
#[doc = "}"]
#[doc = r" ```"]
#[doc = r" </details>"]
#[derive(:: serde :: Serialize, Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
#[serde(transparent)]
pub struct TypedUrLsValueVariant2Key(::std::string::String);
impl ::std::ops::Deref for TypedUrLsValueVariant2Key {
    type Target = ::std::string::String;
    fn deref(&self) -> &::std::string::String {
        &self.0
    }
}
impl ::std::convert::From<TypedUrLsValueVariant2Key> for ::std::string::String {
    fn from(value: TypedUrLsValueVariant2Key) -> Self {
        value.0
    }
}
impl ::std::str::FromStr for TypedUrLsValueVariant2Key {
    type Err = self::error::ConversionError;
    fn from_str(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        static PATTERN: ::std::sync::LazyLock<::regress::Regex> =
            ::std::sync::LazyLock::new(|| {
                ::regress::Regex::new("^(?!(?![A-Za-z][A-Za-z0-9+.-]*:)(?!\\{)[^\\s]+$)[^\\s]+$")
                    .unwrap()
            });
        if PATTERN.find(value).is_none() {
            return Err ("doesn't match pattern \"^(?!(?![A-Za-z][A-Za-z0-9+.-]*:)(?!\\{)[^\\s]+$)[^\\s]+$\"" . into ()) ;
        }
        Ok(Self(value.to_string()))
    }
}
impl ::std::convert::TryFrom<&str> for TypedUrLsValueVariant2Key {
    type Error = self::error::ConversionError;
    fn try_from(value: &str) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<&::std::string::String> for TypedUrLsValueVariant2Key {
    type Error = self::error::ConversionError;
    fn try_from(
        value: &::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl ::std::convert::TryFrom<::std::string::String> for TypedUrLsValueVariant2Key {
    type Error = self::error::ConversionError;
    fn try_from(
        value: ::std::string::String,
    ) -> ::std::result::Result<Self, self::error::ConversionError> {
        value.parse()
    }
}
impl<'de> ::serde::Deserialize<'de> for TypedUrLsValueVariant2Key {
    fn deserialize<D>(deserializer: D) -> ::std::result::Result<Self, D::Error>
    where
        D: ::serde::Deserializer<'de>,
    {
        ::std::string::String::deserialize(deserializer)?
            .parse()
            .map_err(|e: self::error::ConversionError| {
                <D::Error as ::serde::de::Error>::custom(e.to_string())
            })
    }
}
