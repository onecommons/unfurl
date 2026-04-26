# Copyright (c) 2025 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Safe-importable cloudmap data type definitions.

Contains the dataclasses, TypedDicts, protocols, and helper functions used by
the cloudmap subsystem. This module is safe to import from sandboxed Notable
subclasses.
"""

from __future__ import annotations
from dataclasses import dataclass, asdict, field, InitVar
from functools import total_ordering
import os
import os.path
from datetime import datetime, timezone
from operator import attrgetter
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Type,
    TypeVar,
    Union,
    cast,
)
from typing_extensions import Literal, Protocol, Required, TypedDict, Unpack, Self
from urllib.parse import ParseResult, quote, urlparse, urlunparse, parse_qsl, urlencode

from unfurl.repo import normalize_git_url
from unfurl.tosca_plugins.functions import ContainerImageParts

if TYPE_CHECKING:
    from unfurl.localenv import LocalEnv
    from unfurl.support import ContainerImage
    from unfurl.logs import UnfurlLogger


def validate_url(url: str, field_name: str = "URL") -> str:
    """
    Validate that the given field is a valid URL.

    Args:
        url: The URL string to validate
        field_name: Name of the field (for error messages)

    Returns:
        The validated URL

    Raises:
        ValueError: If the URL is not valid
    """
    # we want to support relative URLs too (like path or #fragment) so just check for spaces
    if any(c.isspace() for c in url):
        raise ValueError(f"{field_name} is not a valid URL: {url!r}")
    return url


def join_resource_url(base_url: str, join_url: str) -> str:
    assert join_url
    base = urlparse(base_url)
    if not base.scheme:
        return join_url  # if base_url is not an absolute URL, just return the join_url
    join = urlparse(join_url)
    if join.scheme or (not join.fragment and not join.query and "@" not in join.path):
        # just return join url if it is an absolute URL or a bare name without a purl version
        return join_url
    replace = {}
    if join.fragment:
        replace["fragment"] = join.fragment
    if join.query:
        if base.query and "=" in join.query:
            base_params = parse_qsl(base.query, keep_blank_values=True)
            join_params = parse_qsl(join.query, keep_blank_values=True)
            join_keys = {key for key, _value in join_params}
            merged_params = [item for item in base_params if item[0] not in join_keys]
            merged_params.extend(join_params)
            replace["query"] = urlencode(merged_params, safe=":/@")
        else:
            replace["query"] = join.query
    if join.path:
        if join.path.startswith("@"):
            # if join path starts with @, treat it as a purl version and append to base path with @
            replace["path"] = base.path.partition("@")[0] + join.path
        else:
            replace["path"] = join.path
    return urlunparse(base._replace(**replace))


class EntitySchema:
    """built-in artifact entity types"""

    # https://github.com/package-url/purl-spec
    # mime type https://www.iana.org/assignments/media-types/media-types.xhtml
    Schema = "unfurl.cloud/onecommons/std"
    GenericFile = "tosca.artifacts.File"
    GenericPackage = "cloudmap.artifacts.GenericPackage"
    """Catch-all artifact type for generic ``pkg:`` PURLs (npm, pypi, maven, etc.)."""
    ContainerFile = "cloudmap.artifacts.Containerfile"
    CloudBlueprint = "cloudmap.artifacts.tosca.ServiceTemplate"
    CloudMap = "cloudmap.artifacts.CloudMap"
    TOSCASchema = "cloudmap.artifacts.tosca.TypeLibrary"
    Ensemble = "cloudmap.artifacts.unfurl.Ensemble"
    UnfurlProject = "cloudmap.artifacts.unfurl.Project"
    Package = "cloudmap.artifacts.unfurl.Package"
    "Repository with semver, roughly like a language-agnostic go module"
    OCIImage = "cloudmap.artifacts.oci.Image"
    PullRequest = "cloudmap.artifacts.PullRequest"
    CommitMessage = "cloudmap.artifacts.CommitMessage"
    InTotoAttestation = "cloudmap.artifacts.InTotoAttestation"
    "application/vnd.in-toto+json"
    SpDxDoc = "cloudmap.artifacts.SpdxDocument"
    CycloneDxBom = "cloudmap.artifacts.CycloneDxBom"
    SlsaProvenance02 = "cloudmap.artifacts.SlsaProvenance02"
    SlsaProvenance1 = "cloudmap.artifacts.SlsaProvenance1"
    BuildkitProvenance = "cloudmap.artifacts.BuildkitProvenance"
    "see https://github.com/moby/buildkit/blob/master/docs/attestations/slsa-definitions.md"
    Empty = "cloudmap.artifacts.Empty"
    "Null artifact used to represent empty documents or placeholders"
    Group = "cloudmap.artifacts.Group"
    "Generic grouping of artifacts (use notable to declare members)"
    AbstractBlueprint = "cloudmap.artifacts.AbstractBlueprint"
    "Artifact definition that does not correspond to a concrete artifact"
    GitHubWorkflow = "cloudmap.artifacts.ci.GitHubWorkflow"
    GitLabPipeline = "cloudmap.artifacts.ci.GitLabPipeline"
    CIPipelineRun = "cloudmap.artifacts.ci.PipelineRun"


ArtifactMappings = {
    "https://in-toto.io/Statement/v0.1": EntitySchema.InTotoAttestation,
    "https://spdx.dev/Document": EntitySchema.SpDxDoc,
    "https://slsa.dev/provenance/v0.2": EntitySchema.SlsaProvenance02,
    "https://slsa.dev/provenance/v1": EntitySchema.SlsaProvenance1,
    "https://mobyproject.org/buildkit@v1": EntitySchema.BuildkitProvenance,
    "https://github.com/moby/buildkit/blob/master/docs/attestations/slsa-definitions.md": EntitySchema.BuildkitProvenance,
    "https://cyclonedx.org/bom/v1.4": EntitySchema.CycloneDxBom,
}


@dataclass
class CommonMetadata:
    """Common metadata fields shared across artifacts, services, and repositories."""

    title: str = ""
    """Human-readable title."""
    description: str = ""
    """Human-readable description."""
    topics: List[str] = field(default_factory=list)
    """List of topic or categories associated with the resource."""
    vendor: str = ""
    """Name of the distributing entity, organization, or individual."""
    version: str = ""
    """Version. The version may match a label or tag in the source code repository or may be Semantic Versioning-compatible."""
    fork_of: str = ""
    """URL to the entity this is a fork of."""
    documentation_url: str = ""
    """URL to get documentation."""
    homepage_url: str = ""
    """URL to find more information."""
    thumbnail_url: str = ""
    """Icon or thumbnail URL."""
    discussion_url: str = ""
    """Link to issue, PR/MR, or discussion about this definition."""
    spdx_licenses: str = ""
    """License(s) as an SPDX License Expression."""
    created: str = ""
    """Date and time on which the resource was created, conforming to RFC 3339."""
    source_url: str = ""
    """Informal pointer to source code"""
    source_ref: str = ""
    """Informal pointer to source ref (branch or tag name)"""
    source_revision: str = ""
    """Informal pointer to source code revision"""

    def asdict(self) -> Dict[str, Any]:
        # exclude empty values
        return {k: v for k, v in asdict(self).items() if v}

    def __post_init__(self):
        if self.fork_of:
            self.fork_of = validate_url(
                self.fork_of, f"{self.__class__.__name__}.fork_of"
            )
        if self.homepage_url:
            self.homepage_url = validate_url(
                self.homepage_url, f"{self.__class__.__name__}.homepage_url"
            )
        if self.documentation_url:
            self.documentation_url = validate_url(
                self.documentation_url, f"{self.__class__.__name__}.documentation_url"
            )
        if self.discussion_url:
            self.discussion_url = validate_url(
                self.discussion_url, f"{self.__class__.__name__}.discussion_url"
            )
        if self.thumbnail_url:
            self.thumbnail_url = validate_url(
                self.thumbnail_url, f"{self.__class__.__name__}.thumbnail_url"
            )
        if self.source_url:
            self.source_url = validate_url(
                self.source_url, f"{self.__class__.__name__}.source_url"
            )


@dataclass
class ArtifactMetadata(CommonMetadata):
    """
    Metadata about the repository that isn't stored in the git repository itself but might be provided by the host
    e.g. metadata that found on the repository's GitHub or GitLab project page.
    """

    platforms: Optional[List[Dict[str, str]]] = None

    def extract_urls_from_labels(self, labels: Dict[str, Any]) -> None:
        """
        Extract URLs and metadata from OCI labels/annotations and set fields on this instance.
        https://specs.opencontainers.org/image-spec/annotations/
        """

        def _set_if_present(field_name: str, label_key: str) -> None:
            value = labels.get(label_key)
            if isinstance(value, str) and value.strip():
                cleaned = value.strip()
                if field_name in ("source_url", "homepage_url", "documentation_url"):
                    try:
                        cleaned = validate_url(
                            cleaned, f"ArtifactMetadata.{field_name}"
                        )
                        setattr(self, field_name, cleaned)
                    except ValueError:
                        pass  # skip invalid URL
                else:
                    setattr(self, field_name, cleaned)

        # OCI standard annotations
        _set_if_present("source_url", "org.label-schema.vcs-url")
        _set_if_present("source_url", "org.opencontainers.image.source")
        _set_if_present("documentation_url", "org.opencontainers.image.documentation")
        _set_if_present("spdx_licenses", "org.opencontainers.image.licenses")
        _set_if_present("version", "org.label-schema.version")
        _set_if_present("version", "org.opencontainers.image.version")
        _set_if_present("source_revision", "org.opencontainers.image.revision")
        _set_if_present("title", "org.label-schema.name")
        _set_if_present("title", "org.opencontainers.image.title")
        _set_if_present("description", "org.label-schema.description")
        _set_if_present("vendor", "org.label-schema.vendor")
        _set_if_present("description", "org.opencontainers.image.description")
        _set_if_present("vendor", "org.opencontainers.image.vendor")
        _set_if_present("homepage_url", "org.label-schema.url")
        _set_if_present("homepage_url", "org.opencontainers.image.url")
        _set_if_present("created", "org.opencontainers.image.created")

    def __post_init__(self):
        super().__post_init__()


TypeRefStatus = Literal["unknown", "absent", "present", "failed", "validated"]


class PipelineArtifact(TypedDict):
    """An artifact produced by a CI pipeline job or workflow run."""

    name: str
    url: str
    size: int
    expires_at: str


class PipelineVariable(TypedDict):
    """A CI pipeline variable (GitLab only)."""

    key: str
    value: str


class PipelineRunProperties(TypedDict, total=False):
    """Properties for a CIPipelineRun type constraint."""

    id: int
    log_url: str
    artifacts: List[PipelineArtifact]
    artifacts_expire_at: str
    variables: List[PipelineVariable]


class TypeRefConstraint(TypedDict, total=False):
    status: TypeRefStatus
    version: Union[int, float, str]
    properties: Dict[str, Any]
    metadata: Dict[str, Any]
    model: str


TypeRefJson = Dict[
    str,
    Optional[TypeRefConstraint],
]


@total_ordering
class TypeRefs:
    """
    Type references with optional constraints.

    Represents a mapping of type names to optional version constraints.
    Example: {"software.Nginx": {"version": "1.25"}, "software.Linux": None}
    """

    def __init__(self, types: Optional[TypeRefJson] = None):
        """Initialize TypeRefs from a dict, a single type-name string, or empty.

        Accepts the YAML shorthand ``type: <type-name>`` (parsed as a plain
        string) and normalises it into the canonical ``{<type-name>: None}``
        dict so callers can rely on ``self.types`` always being a dict.
        """
        if types is None:
            self.types: TypeRefJson = {}
        elif isinstance(types, str):
            self.types = {types: None}
        else:
            self.types = types

    def asdict(self) -> TypeRefJson:
        """Return JSON representation of typeRef."""
        return {k: filter_dict(self.types[k]) or None for k in sorted(self.types)}

    def names(self) -> List[str]:
        """Return list of type names."""
        return list(self.types)

    def add(
        self, type_name: Optional[str], **kw: Unpack[TypeRefConstraint]
    ) -> "TypeRefs":
        """Add a type reference with optional constraints."""
        if not type_name:
            return self
        self.types[type_name] = filter_dict(kw) or None
        return self

    def __bool__(self) -> bool:
        """Return True if there are any type references."""
        return bool(self.types)

    def __len__(self) -> int:
        """Return number of type references."""
        return len(self.types)

    def __repr__(self) -> str:
        return f"TypeRefs({self.types!r})"

    def __eq__(self, other: object) -> bool:
        """Return True if there are any type references."""
        if not isinstance(other, TypeRefs):
            return NotImplemented
        return self.types == other.types

    def __ne__(self, other: object) -> bool:
        """Return True if there are any type references."""
        if not isinstance(other, TypeRefs):
            return NotImplemented
        return self.types != other.types

    def __lt__(self, other):
        """Compare based on the sorted items of the types dict."""
        if not isinstance(other, TypeRefs):
            return NotImplemented
        return sorted(self.types.items()) < sorted(other.types.items())

    def __hash__(self) -> int:
        """Hash based on the sorted items of the types dict."""
        return hash(tuple(sorted(self.types.items())))

    def __cmp__(self, other: object) -> int:
        """Compare based on the sorted items of the types dict."""
        if not isinstance(other, TypeRefs):
            return NotImplemented
        return (sorted(self.types.items()) > sorted(other.types.items())) - (
            sorted(self.types.items()) < sorted(other.types.items())
        )

    @staticmethod
    def urls_asdict(typed_urls: "TypedUrls") -> Dict[str, Optional[TypeRefJson]]:
        """Convert TypedUrls to a dict with TypeRefJson values."""
        result: Dict[str, Optional[TypeRefJson]] = {}
        for url in sorted(typed_urls):
            type_refs = typed_urls[url]
            if isinstance(type_refs, TypeRefs):
                result[url] = type_refs.asdict() or None
            else:
                result[url] = type_refs
        return result

    @staticmethod
    def urls_fromdict(
        type_urls_dict: Union["TypedUrls", Dict[str, Optional[TypeRefJson]]],
    ) -> "TypedUrls":
        """Convert instantiated dict values to Optional[TypeRefs]"""
        type_urls: TypedUrls = {}
        for k, v in type_urls_dict.items():
            if isinstance(v, TypeRefs):
                type_urls[k] = v
            else:
                type_urls[k] = TypeRefs(types=v)
        return type_urls


TypedUrls = Dict[str, Optional[TypeRefs]]


class VersionedRecord:
    url: str
    versions: Dict[str, Self]
    type: TypeRefs

    def __init__(self, **kwargs):
        pass

    def _load_versions(self) -> Dict[str, Self]:
        new_versions: Dict[str, Self] = {}
        cls = type(self)
        for version_key, version_val in self.versions.items():
            if isinstance(version_val, cls):
                new_versions[version_key] = version_val
            elif isinstance(version_val, dict):
                version_dict = cast(Dict[str, Any], version_val)
                # Inherit type from parent if not specified in version
                if "type" not in version_dict:
                    version_dict = dict(version_dict, type=self.type)
                new_versions[version_key] = cls(
                    url=join_resource_url(self.url, version_key),
                    _parent=self,
                    **version_dict,
                )

        return new_versions


@dataclass
class Instantiation(VersionedRecord):
    """
    Build and deployment information for artifacts and services.

    Stored in CloudMapDB.instantiations with URL keys and referenced by
    Artifact.instantiated_by and Service.instantiated_by.
    """

    url: str = ""
    """URL of the instantiation (auto-generated as timestamp fragment if not provided)"""
    type: TypeRefs = field(default_factory=TypeRefs)
    """Type of the instantiation."""
    digest: str = ""
    """Cryptographic digest of document reference by the instantiation URL."""
    revision: str = ""
    """If instantiation URL references a repository, source control revision of that repository."""
    source: str = ""
    """Repository or artifact URL."""
    source_ref: str = ""
    """If source URL references a repository, the branch or tag name."""
    source_revision: str = ""
    """If source URL references a repository, the source control revision of that repository."""
    instantiated: TypedUrls = field(default_factory=dict)
    """The artifacts or services created or updated by this instantiation with optional capability."""
    inputs: TypedUrls = field(default_factory=dict)
    """The artifact, service, or repository URLs that were consumed or referenced as part of the instantiation process."""
    metadata: CommonMetadata = field(default_factory=CommonMetadata)
    """Additional metadata about the instantiation."""
    discovery: Optional["Discovery"] = None
    """Metadata discovery information"""
    status: Optional[
        Literal[
            "draft",
            "model",
            "planned",
            "WIP",
            "observed",
            "verifiable",
            "verified",
            "reproducible",
            "reproduced",
        ]
    ] = None
    """Status of the instantiation's reproducibility and verification."""
    versions: Dict[str, "Instantiation"] = field(default_factory=dict)
    """Instantiations that are variants of this instantiation (for example, different deployments or environments)"""
    _parent: InitVar[Optional["Instantiation"]] = None

    def __post_init__(self, _parent: Optional["Instantiation"] = None):
        if not self.url:  # Auto-generate id as url fragment if not set
            self.url = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if self.source:
            self.source = validate_url(self.source, "Instantiation.source")
        if not isinstance(self.type, TypeRefs):
            self.type = TypeRefs(types=self.type)
        if not isinstance(self.metadata, CommonMetadata):
            self.metadata = CommonMetadata(**(self.metadata or {}))
        if not isinstance(self.discovery, Discovery):
            self.discovery = Discovery(**(self.discovery or {}))
        self.instantiated = TypeRefs.urls_fromdict(self.instantiated)
        self.inputs = TypeRefs.urls_fromdict(self.inputs)
        # Convert versions dict entries to Instantiation instances if they're still dicts
        if self.versions:
            self.versions = self._load_versions()
        self._parent = _parent  # type: ignore  # (don't mark as field to exclude from asdict)

    def asdict(self) -> Dict[str, Any]:
        # exclude url and empty values
        result = {}
        for k, v in asdict(self).items():
            if k == "url":
                continue  # url is the key, not saved in value
            if k == "type" and v:
                v = v.asdict() if isinstance(v, TypeRefs) else v
            elif k == "metadata":
                v = filter_dict(v)
            elif k == "discovery" and v:
                v = filter_dict(v)
            elif k == "inputs":
                v = TypeRefs.urls_asdict(v)
            elif k == "instantiated":
                v = TypeRefs.urls_asdict(v)
            elif k == "versions" and self.versions:
                # Convert nested Instantiation instances to dicts by calling asdict on actual instances
                v = {url: inst.asdict() for url, inst in self.versions.items()}
            # exclude empty values and values inherited from parent
            if v and (
                not self._parent or v != getattr(self._parent, k)  # type: ignore
            ):
                result[k] = v
        return result


@dataclass
class Discovery:
    """Metadata discovery information."""

    last_checked: str = ""
    """Date and time of the last metadata check"""
    sources: List[str] = field(default_factory=list)
    """List of URLs that were used for metadata discovery"""

    def __post_init__(self):
        if self.sources:
            self.sources = [
                validate_url(url, "Discovery.sources") for url in self.sources
            ]

    def asdict(self) -> Dict[str, Any]:
        # exclude empty values
        return {k: v for k, v in asdict(self).items() if v}


_M = TypeVar("_M", bound=Mapping[str, Any])


# use TypeVar to handle TypedDicts
def filter_dict(d: Optional[_M]) -> Optional[_M]:
    """Exclude empty values from a dictionary."""
    if d is None:
        return None
    return cast(_M, {k: v for k, v in d.items() if v})


LifecycleStatus = Literal[
    "wishlist",
    "model",
    "planned",
    "development",
    "alpha",
    "beta",
    "production",
    "maintenance",
    "unmaintained",
    "deprecated",
    "removed",
]


@dataclass
class ScheduledRelease:
    """Scheduled Release for an artifact or service."""

    url: str = ""
    """The URL for this upcoming release"""
    version: Union[str, int, float] = ""
    """Version of the upcoming release"""
    status: Optional[LifecycleStatus] = None
    """The upcoming lifecycle status"""
    effective_date: str = ""
    """The date and time the release will happen (RFC 3339 format)."""

    def __post_init__(self):
        if self.url:
            self.url = validate_url(self.url, "ScheduledRelease.url")


def build_oci_purl(ref: ContainerImageParts) -> str:
    """
    Build a Package URL for an OCI artifact.

    pkg:oci/<name>@<version>?<qualifiers>#<subpath>

    e.g. pkg:oci/static@sha256%3A244fd47e07d10?repository_url=gcr.io/distroless/static&arch=amd64&tag=latest

    Version is the digest and can be omitted.
    The image repository (or namespace) is included in repository_url, not the name.
    """
    purl = f"pkg:oci/{quote(ref.name)}"
    if ref.digest:
        purl += f"@{quote(ref.digest)}"
    purl += "?repository_url=" + quote(ref.registry or "docker.io")
    repository = ref.repository
    if repository:
        purl += f"/{quote(repository)}"
    if ref.tag:
        purl += f"&tag={quote(ref.tag)}"
    return purl


@dataclass
class Artifact(VersionedRecord):
    url: str
    type: TypeRefs = field(default_factory=TypeRefs)
    """Type identifier from types/artifacts with optional version constraints"""
    notable: TypedUrls = field(default_factory=dict)
    """"Map of URLs of interesting artifacts that this artifact embeds or incorporates."""
    references: TypedUrls = field(default_factory=dict)
    """Map of URLs of interesting artifacts, repositories or services that this artifact may reference when executed or instantiated."""
    instantiates: TypeRefs = field(default_factory=TypeRefs)
    """Types that this artifact instantiates with optional version constraints"""
    dependencies: TypedUrls = field(default_factory=dict)
    """Software, services, or environment context that the instantiation may depend on. Keys are labels or artifact URLs, values are type constraints of components or capabilities. Non-exhaustive: for example, the type may imply additional requirements or some dependencies might be optional."""
    instantiated_by: TypedUrls = field(default_factory=dict)
    """URLs referencing entries in instantiations with optional type constraints."""
    digest: str = ""
    """Cryptographic digest of the artifact"""
    immutable: bool = False
    """Whether the artifact identifier refers to an artifact that will not change over time"""
    status: Optional[LifecycleStatus] = None
    """Lifecycle status of the artifact"""
    release_schedule: List[ScheduledRelease] = field(default_factory=list)
    """Release schedule information for this artifact"""
    metadata: ArtifactMetadata = field(default_factory=ArtifactMetadata)
    """Human-readable metadata"""
    discovery: Optional["Discovery"] = None
    """Metadata discovery information"""
    tags: Optional[List[str]] = None
    """List of available tags for this artifact (e.g., container image tags)"""
    versions: Dict[str, "Artifact"] = field(default_factory=dict)
    """Artifacts that are variants of this artifact (for example, releases or snapshots)"""
    _parent: InitVar[Optional["Artifact"]] = None

    def __post_init__(self, _parent: Optional["Artifact"] = None):
        # Validate pkg URL
        if self.url:
            parts = urlparse(self.url)  # just to parse and validate
            if not parts.scheme:  # migrate old cloudmap format
                self.url = build_oci_purl(ContainerImageParts.split(self.url))
            elif parts.scheme not in ["pkg", "git"]:
                raise ValueError(f"Artifact.url must be a pkg URL: {self.url!r}")

        if not isinstance(self.metadata, ArtifactMetadata):
            self.metadata = ArtifactMetadata(**(self.metadata or {}))
        if not isinstance(self.discovery, Discovery):
            self.discovery = Discovery(**(self.discovery or {}))
        if not isinstance(self.type, TypeRefs):
            self.type = TypeRefs(types=self.type)
        if not isinstance(self.instantiates, TypeRefs):
            self.instantiates = TypeRefs(types=self.instantiates)
        self.dependencies = TypeRefs.urls_fromdict(self.dependencies)
        self.notable = TypeRefs.urls_fromdict(self.notable)
        self.references = TypeRefs.urls_fromdict(self.references)
        if isinstance(self.instantiated_by, list):
            self.instantiated_by = {url: None for url in self.instantiated_by}
        self.instantiated_by = TypeRefs.urls_fromdict(self.instantiated_by)
        self.release_schedule = [
            ScheduledRelease(**item) if isinstance(item, dict) else item
            for item in self.release_schedule
        ]
        # Convert versions dict entries to Artifact instances if they're still dicts
        if self.versions:
            self.versions = self._load_versions()
        self._parent = _parent  # type: ignore  # (don't mark as field to exclude from asdict)

    def asdict(self) -> Dict[str, Any]:
        # exclude empty values
        result = {}
        for k, v in asdict(self).items():
            if k == "url":
                continue  # skip url, save as the key instead
            if k == "metadata":
                v = filter_dict(v)
            elif k == "discovery" and v:
                v = filter_dict(v)
            elif k == "notable":
                v = TypeRefs.urls_asdict(v)
            elif k == "references":
                v = TypeRefs.urls_asdict(v)
            elif k == "type" and v:
                v = v.asdict() if isinstance(v, TypeRefs) else v
            elif k == "instantiates" and v:
                v = v.asdict() if isinstance(v, TypeRefs) else v
            elif k == "dependencies":
                v = TypeRefs.urls_asdict(v)
            elif k == "instantiated_by":
                v = TypeRefs.urls_asdict(v)
            elif k == "release_schedule" and v:
                v = [filter_dict(item) for item in v]
            elif k == "versions" and v:
                # Convert nested Artifact instances to dicts
                v = {
                    url: (rel.asdict() if isinstance(rel, Artifact) else rel)
                    for url, rel in v.items()
                }
            # exclude empty values and values inherited from parent
            if v and (
                not self._parent or v != getattr(self._parent, k)  # type: ignore
            ):
                result[k] = v
        return result


def get_repository_url(url: str) -> str:
    """Return the git:// URL for the repository without user or fragment"""
    parts = urlparse(normalize_git_url(url))
    user, sep, host = parts.netloc.rpartition("@")
    netloc = host
    if "+" in parts.scheme:
        # remove @revision from VCS location URLs like "git+https" (see https://github.com/spdx/spdx-spec/blob/cfa1b9d08903/chapters/3-package-information.md#3.7)
        return "git://" + netloc + parts.path.partition("@")[0]
    return "git://" + netloc + parts.path


def _match_namespace(path: str, namespace: str) -> bool:
    if not namespace or path == namespace:
        return True
    if not path:
        return False
    # don't match on partial segments
    return path.startswith(os.path.join(namespace, ""))


# Data classes
@dataclass
class Namespace:
    name: str
    path: str
    url: str
    internal_id: Optional[str] = None
    description: str = ""
    thumbnail_url: str = ""
    public: Optional[bool] = None
    shared: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.url:
            self.url = validate_url(self.url, "Namespace.url")
        if self.thumbnail_url:
            self.thumbnail_url = validate_url(
                self.thumbnail_url, "Namespace.thumbnail_url"
            )


ProjectStatus = Literal[
    "concept",
    "WIP",
    "suspended",
    "abandoned",
    "active",
    "inactive",
    "unsupported",
    "moved",
]


@dataclass
class RepositoryMetadata(CommonMetadata):
    """
    Metadata about the repository that isn't stored in the git repository itself but might be provided by the host
    e.g. metadata that found on the repository's GitHub or GitLab project page.
    """

    license_url: str = ""
    issues_url: str = ""
    ci_variables: Optional[dict] = None
    lastupdate_time: Optional[str] = None
    lastupdate_digest: Optional[str] = None
    project_status: Optional[ProjectStatus] = None

    def __post_init__(self):
        super().__post_init__()
        if self.license_url:
            self.license_url = validate_url(
                self.license_url, "RepositoryMetadata.license_url"
            )
        if self.issues_url:
            self.issues_url = validate_url(
                self.issues_url, "RepositoryMetadata.issues_url"
            )

    def asdict(self):
        # exclude empty values
        return {k: v for k, v in asdict(self).items() if v}

    # def get_digest(self) -> str:
    #     keys = sorted([k in asdict(self) if not k.startswith("lastupdate")]
    #     prefix = "".join([k[0] for k in keys])

    def set_lastupdate(self) -> None:
        pass


class NotableDict(TypedDict, total=False):
    type: TypeRefJson
    artifact: str


@dataclass
class Repository:
    url: str
    """URL of the repository using the git:// URL scheme"""
    path: str
    """Project path relative to base location of git repositories on the host"""
    initial_revision: str = ""
    "Initial commit of the default branch."
    name: str = ""
    service: Optional[str] = None
    "URL of the service hosting this repository."
    protocols: List[str] = field(default_factory=list)
    internal_id: Optional[str] = None
    "Internal identifier from the repository host (e.g., GitHub repository ID)."
    project_url: str = ""
    metadata: RepositoryMetadata = field(default_factory=RepositoryMetadata)
    mirror_of: Optional[str] = None
    "URL of the original repository if this is a mirror"
    fork_of: Optional[str] = None
    "URL of the original repository if this is a fork"
    private: Optional[bool] = None
    "True if the repository not publicly accessible."
    default_branch: str = ""
    'The default branch of the repository (e.g. "main").'
    branches: Dict[str, str] = field(default_factory=dict)
    tags: Dict[str, str] = field(default_factory=dict)
    notable: Dict[str, NotableDict] = field(default_factory=dict)
    """Map of paths to files and directories in the repository that are useful for characterizing the repository and integrating it with the other resources in the cloud map"""

    def __post_init__(self):
        if self.url:
            # url is stored as git:// URL
            self.url = get_repository_url(self.url)
        if self.project_url:
            self.project_url = validate_url(self.project_url, "Repository.project_url")
        if self.mirror_of:
            self.mirror_of = validate_url(self.mirror_of, "Repository.mirror_of")
        if self.fork_of:
            self.fork_of = validate_url(self.fork_of, "Repository.fork_of")
        if not isinstance(self.metadata, RepositoryMetadata):
            md = self.metadata
            if isinstance(md, dict) and "avatar_url" in md:
                # migrate deprecated key
                md["thumbnail_url"] = md.pop("avatar_url")
            self.metadata = RepositoryMetadata(**(md or {}))

    def get_current_commit(self) -> str:
        """Return the current commit for the default branch."""
        branch_name = self.default_branch or "main"
        return self.branches.get(branch_name, "")

    def get_metadata(self, directory: "RepositoryDict") -> dict:
        if self.mirror_of and self.mirror_of in directory:
            # merge mirror_of metadata
            return dict(
                directory[self.mirror_of].get_metadata(directory),
                **self.metadata.asdict(),
            )
        return self.metadata.asdict()

    def asdict(self) -> Dict[str, Any]:
        # exclude empty values and skip url, (save as the key instead)
        return {
            k: (filter_dict(v) if k == "metadata" else v)
            for k, v in asdict(self).items()
            if v and k != "url"
        }

    def git_url(self, preference=()) -> str:
        "URL to clone the repository using preferred protocol or the first available protocol"
        preference = preference or self.protocols  # match first protocol if not set
        url = self.url[len("git://") :]
        for scheme in preference:
            if scheme in self.protocols:
                if scheme == "ssh":
                    return "git@" + url.replace("/", ":", 1)
                else:
                    return scheme + "://" + url
        return ""

    def artifact_url(self, file_path: str) -> str:
        "URL to reference a file in the repository as an artifact"
        return f"{self.url}#:{quote(file_path)}"

    def match_path(self, path: str) -> bool:
        return _match_namespace(self.path, path)

    @property
    def package_id(self):
        "URL as a package id"
        url = self.url[len("git://") :]
        if url.endswith(".git"):
            return url[:-4]
        return url

    # match url and path?
    # def get_namespace(self, directory) -> Optional[Namespace]:
    #     path.split("/")

    def update_branch(self, repo: "Any", branch: str = ""):
        if not branch:
            branch = self.get_default_branch()
        self.branches[branch] = repo.revision

    def add_notables(self, notables: List["RepositoryNotable"]) -> None:
        notables.sort(key=attrgetter("path"))
        self.notable = {n.path: n.asdict() for n in notables}

    def get_default_branch(self):
        return self.default_branch or "main"


ArtifactDict = Dict[str, Artifact]


@dataclass
class ServiceMetadata(CommonMetadata):
    """Human-readable metadata about a service."""


@dataclass
class ServicePolicies:
    """Service policies and legal information."""

    spdx_licenses: str = ""
    terms_of_service: str = ""
    privacy_policy: str = ""

    def __post_init__(self):
        if self.terms_of_service:
            self.terms_of_service = validate_url(
                self.terms_of_service, "ServicePolicies.terms_of_service"
            )
        if self.privacy_policy:
            self.privacy_policy = validate_url(
                self.privacy_policy, "ServicePolicies.privacy_policy"
            )

    def asdict(self) -> Dict[str, Any]:
        # exclude empty values
        return {k: v for k, v in asdict(self).items() if v}


@dataclass
class Service(VersionedRecord):
    """A service instance."""

    url: str
    """URL of the service"""
    type: TypeRefs = field(default_factory=TypeRefs)
    """Type identifiers from types with optional version constraints"""
    access: Optional[Literal["public", "private", "none", ""]] = ""
    "Access to the service (who can resolve the URL)."
    endpoints: TypedUrls = field(default_factory=dict)
    """Service endpoints"""
    connections: TypedUrls = field(default_factory=dict)
    "Services this service connects to during operation."
    status: Optional[LifecycleStatus] = None
    """Lifecycle status of the service"""
    metadata: ServiceMetadata = field(default_factory=ServiceMetadata)
    policies: ServicePolicies = field(default_factory=ServicePolicies)
    instantiated_by: TypedUrls = field(default_factory=dict)
    """URLs referencing entries in instantiations with optional type constraints."""
    discovery: Optional[Discovery] = None
    """Metadata discovery information (last_checked, sources)"""
    release_schedule: List[ScheduledRelease] = field(default_factory=list)
    """Release schedule information for this service"""
    versions: Dict[str, "Service"] = field(default_factory=dict)
    """Services that are variants of this service (for example, different versions or environments)"""
    _parent: InitVar[Optional["Service"]] = None

    def __post_init__(self, _parent: Optional["Service"] = None):
        if self.url:
            self.url = validate_url(self.url, "Service.url")

        if not isinstance(self.metadata, ServiceMetadata):
            self.metadata = ServiceMetadata(**(self.metadata or {}))
        if not isinstance(self.policies, ServicePolicies):
            self.policies = ServicePolicies(**(self.policies or {}))
        if not isinstance(self.discovery, Discovery):
            self.discovery = Discovery(**(self.discovery or {}))
        if not isinstance(self.type, TypeRefs):
            self.type = TypeRefs(types=self.type)
        self.release_schedule = [
            ScheduledRelease(**item) if isinstance(item, dict) else item
            for item in self.release_schedule
        ]
        self.endpoints = TypeRefs.urls_fromdict(self.endpoints)
        self.connections = TypeRefs.urls_fromdict(self.connections)
        if isinstance(self.instantiated_by, list):
            self.instantiated_by = {url: None for url in self.instantiated_by}
        self.instantiated_by = TypeRefs.urls_fromdict(self.instantiated_by)
        # Convert versions dict entries to Service instances if they're still dicts
        if self.versions:
            self.versions = self._load_versions()
        self._parent = _parent  # type: ignore # (don't mark as field to exclude from asdict)

    def asdict(self) -> Dict[str, Any]:
        # exclude empty values
        result = {}
        for k, v in asdict(self).items():
            if k == "url":
                continue  # skip url, save as the key instead
            if k == "metadata":
                v = filter_dict(v)
            elif k == "policies":
                v = filter_dict(v)
            elif k == "discovery" and v:
                v = filter_dict(v)
            elif k == "type" and v:
                v = v.asdict() if isinstance(v, TypeRefs) else v
            elif k == "endpoints":
                v = TypeRefs.urls_asdict(v)
            elif k == "connections":
                v = TypeRefs.urls_asdict(v)
            elif k == "instantiated_by":
                v = TypeRefs.urls_asdict(v)
            elif k == "release_schedule" and v:
                v = [filter_dict(item) for item in v]
            elif k == "versions" and v:
                # Convert nested Service instances to dicts
                v = {
                    url: (svc.asdict() if isinstance(svc, Service) else svc)
                    for url, svc in v.items()
                }

            # exclude empty values and values inherited from parent
            if v and (
                not self._parent or v != getattr(self._parent, k)  # type: ignore
            ):
                result[k] = v
        return result


@dataclass
class CloudType:
    """A type definition for artifacts, services, software, or capabilities."""

    name: str
    """Fully-qualified type name with namespace"""
    kind: Literal["Component", "Artifact", "Capability"]
    source: str = ""
    """Artifact containing type definition"""
    extends: List[str] = field(default_factory=list)
    """List of fully-qualified type names that this type extends"""
    implementations: List[str] = field(default_factory=list)
    """Non-exhaustive list URLs to repositories or artifacts that implement this type."""
    status: Optional[
        Literal["draft", "experimental", "stable", "deprecated", "removed"]
    ] = None
    """Maturity level of the type definition"""
    model: str = ""
    """URL of artifact or service to use as a model for instances of this type"""
    metadata: CommonMetadata = field(default_factory=CommonMetadata)
    """Additional metadata about the type"""

    def __post_init__(self):
        if self.source:
            self.source = validate_url(self.source, "CloudType.source")
        if self.model:
            self.model = validate_url(self.model, "CloudType.model")
        if not isinstance(self.metadata, CommonMetadata):
            self.metadata = CommonMetadata(**(self.metadata or {}))

    def asdict(self) -> Dict[str, Any]:
        result = {}
        for k, v in asdict(self).items():
            if k == "metadata":
                v = filter_dict(v)
            if v:  # exclude empty values
                result[k] = v
        return result


ServiceDict = Dict[str, Service]
CloudTypeDict = Dict[str, CloudType]
RepositoryDict = Dict[str, Repository]


T = TypeVar("T", bound="RepositoryNotable")


class CloudMapView(Protocol):
    # --- Add records ---

    def add_artifact(self, artifact: "Artifact") -> str: ...

    def add_service(self, service: "Service") -> str: ...

    def add_instantiation(self, instantiation: "Instantiation") -> str: ...

    def add_image_artifact(self, image: "ContainerImage") -> "Artifact": ...

    def add_type(self, cloud_type: "CloudType") -> str: ...

    def add_repository(self, repository: Repository) -> str: ...

    # --- Look up existing records ---

    def get_artifact(self, url: str) -> Optional["Artifact"]: ...

    def get_service(self, url: str) -> Optional["Service"]: ...

    def get_instantiation(self, url: str) -> Optional["Instantiation"]: ...

    def get_type(self, name: str) -> Optional["CloudType"]: ...

    def get_repository(self, r: Union[str, Repository]) -> Optional[Repository]: ...

    # --- Iterate / search records ---

    def find_artifacts(self, type: str = "") -> Iterable["Artifact"]:
        """An empty filter returns all artifacts."""
        ...

    def find_services(self, type: str = "") -> Iterable["Service"]:
        """An empty filter returns all services."""
        ...

    def find_instantiations(self, type: str = "") -> Iterable["Instantiation"]:
        """An empty filter returns all instantiations."""
        ...

    def find_types(self) -> Iterable["CloudType"]: ...

    def find_repositories(self) -> Iterable["Repository"]: ...


class AnalyzerContext(CloudMapView, Protocol):
    """Abstract interface to a cloudmap.

    Exposes the subset of :class:`Directory` / :class:`CloudMapDB` functionality that
    custom Notable subclasses (possibly loaded in safe mode) need to contribute
    records to the cloudmap. Attributes with a leading underscore are inaccessible
    from sandboxed code and are intended for built-in Notable classes only.
    """

    logger: "UnfurlLogger"
    """Logger for emitting diagnostic messages during analysis."""

    do_analysis: bool
    """True if cross-referenced URLs should be recursively analyzed (vs. recorded
    as stubs)."""

    @property
    def _local__env(self) -> Optional["LocalEnv"]:
        """Parent environment for loading nested unfurl projects.

        Names with _<name>__<suffix> can not be accessed from sandboxed Notables,
        so this is only accessible to built-in Notable classes and not custom Notables loaded in safe mode.
        """
        ...

    def add_record(
        self,
        url: str,
        analyze: Literal["yes", "no", "save-only", "default"] = "default",
    ) -> Optional[Union["Repository", "Artifact", "Service", "Instantiation"]]:
        """Record (and optionally recursively analyze) a URL: git repo, pkg: PURL,
        or service URL. Returns the record that was added or already existed."""
        ...


class Analyzer:
    """Common base for cloudmap analyzers.

    Both file/folder-based :class:`RepositoryNotable` and URL-based
    :class:`URLAnalyzer` subclasses inherit from this so they share the
    ``artifact_type`` class attribute (the EntitySchema string assigned to
    artifacts produced by the analyzer).
    """

    artifact_type: str = EntitySchema.GenericFile


class RepositoryNotable(Analyzer):
    """
    Base class for plugins that discover notable files or directories in a repository
    -- for example, a Dockerfile, Helm chart, or TOSCA service template.

    Subclasses declare which filenames or directory names they match via the
    ``files`` and ``folders`` class attributes. The ``RepositoryAnalyzer`` walks
    a repository tree, instantiates the appropriate Notable subclass for each
    match, and calls ``analyze()`` to produce an ``Artifact`` for the cloud map.

    Attributes:
        files: Filenames that this Notable class matches (e.g. ``["Dockerfile"]``).
        folders: Directory names that this Notable class matches (e.g. ``["charts"]``).
        artifact_type: The artifact type to assign to matched artifacts (inherited from :class:`Analyzer`).
    """

    files: Sequence[str] = ()
    folders: Sequence[str] = ()

    def __init__(
        self,
        folder: str,
        file: str,
        digest: str = "",
    ):
        self.folder = "" if folder == "." else folder
        self.file = file
        self.digest = digest
        self.fragment = ""
        self.artifact_id: Optional[str] = (
            None  # Artifact ID when added to directory.db.artifacts
        )

    def __repr__(self):
        return f"{self.__class__.__name__}(folder={self.folder!r}, file={self.file!r}, digest={self.digest!r})"

    def analyze(
        self, directory: AnalyzerContext, repo_info: Repository, root_path: str
    ) -> Optional[Artifact]:
        """Analyze the matched file and return an Artifact for the cloud map.

        Subclasses can override this to extract additional metadata (e.g. parsing
        a Dockerfile to find base images or a Helm Chart.yaml for chart metadata).

        Args:
            directory: The :class:`CloudMapView` performing the analysis.
            repo_info: Repository metadata for constructing artifact URLs.
            root_path: Filesystem path to the repository root (for reading file contents).

        Returns:
            An Artifact instance, or None if analysis determines the file is not relevant.
        """
        directory.logger.debug("analyzing %s with %s", self.file, self)
        # Create artifact url from repository URL + file path
        artifact_url = repo_info.artifact_url(self.path)
        return Artifact(url=artifact_url, type=TypeRefs({self.artifact_type: None}))

    @property
    def path(self) -> str:
        """The relative path within the repository, including any URL fragment."""
        if self.file:
            path = os.path.join(self.folder, self.file)
            if self.fragment:
                return path + "#" + self.fragment
            return path
        else:
            return self.folder

    @classmethod
    def _exist_in_folder(cls, folder: str, notables: List["RepositoryNotable"]):
        """Check whether a Notable of this class already exists for the given folder."""
        for n in notables:
            if cls is n.__class__ and n.folder == folder:
                return True
        return False

    @classmethod
    def init(
        cls: Type[T],
        folder: str,
        file: str,
        digest: str = "",
    ) -> Optional[T]:
        """Factory method for creating a Notable instance.

        Subclasses can override this to conditionally reject a match
        (by returning None) or to customize initialization.
        """
        return cls(folder, file, digest)

    def asdict(self) -> NotableDict:
        """Serialize this Notable to a dict for inclusion in Repository.notable."""
        metadata = NotableDict(type={self.artifact_type: None})
        if self.artifact_id:
            metadata["artifact"] = self.artifact_id
        return metadata


class URLAnalyzer(Analyzer):
    """Base class for analyzers that produce records from a URL.

    Whereas :class:`RepositoryNotable` analyzes files/directories inside a git
    repository, ``URLAnalyzer`` subclasses handle URLs directly — for
    example PURL-based references like ``pkg:oci/...``, ``pkg:npm/...``, or
    custom schemes contributed by plugins.

    Subclasses declare which URL-prefix(es) they handle via the
    ``url_schemes`` class attribute (longest-prefix wins, so ``"pkg:oci"``
    beats ``"pkg:"`` for an OCI image). They override two methods:

    - :py:meth:`init_from_url` — factory that parses the URL and returns a
      configured instance, or ``None`` to decline (e.g. malformed input).
    - :py:meth:`analyze_url` — produces an :class:`Artifact` (and any related
      :class:`Instantiation` records via the passed-in :class:`CloudMapView`).

    Custom subclasses can be loaded via the ``cloudmaps.analyzers`` config in
    the same way as :class:`RepositoryNotable` subclasses.
    """

    url_schemes: Sequence[str] = ()

    @classmethod
    def init_from_url(cls, url: str, parsed: ParseResult) -> Optional[Self]:
        """Construct an analyzer instance for ``url`` or return ``None`` to decline.

        Override in subclasses. The default implementation returns ``None``,
        meaning the analyzer cannot handle any URL.
        """
        return None

    def analyze_url(self, directory: "CloudMapView") -> Optional["VersionedRecord"]:
        """Produce an Artifact for the URL this analyzer was constructed with.

        Subclasses can also write related records (Instantiation, Service,
        etc.) directly via ``directory.add_*`` methods. The returned
        :class:`Artifact` is added to the cloudmap by the caller; return
        ``None`` if no artifact should be recorded.
        """
        return None


class HostConfig(TypedDict, total=False):
    type: Literal["local", "gitlab", "unfurl.cloud", "github"]
    # not used by local:
    url: Required[str]
    user: str
    password: str
    visibility: str  # "public", "private", "any"
    save_internal: bool  # save repository host internals
    canonical_url: str
    # omitted or null means default "hosts/{host.name}", set to "" to disable switching branches
    host_branch: Optional[str]


class LocalHostConfig(HostConfig):
    # use when type = local (LocalRepositoryHost)
    clone_root: str  # directory containing the repositories


class CloudMapInputs(TypedDict, total=False):
    host: Required[HostConfig]
    cloudmap: str  # name of cloudmap in the environment
    namespace: str  # filter by namespace
    repository: str  # if set just export this repository (identified by url)
    clone_root: str
    skip_analysis: bool
    force: bool
    host_branch: Optional[str]


CloudMapRecord = Union[
    "Repository", "Artifact", "Instantiation", "Service", "CloudType"
]

__all__ = [
    "VersionedRecord",
    # Dataclasses
    "Namespace",
    "RepositoryMetadata",
    "Repository",
    "ServiceMetadata",
    "ServicePolicies",
    "Service",
    "CloudType",
    "CommonMetadata",
    "ArtifactMetadata",
    "Instantiation",
    "Discovery",
    "ScheduledRelease",
    "Artifact",
    # Supporting classes
    "TypeRefs",
    "EntitySchema",
    "ArtifactMappings",
    "CloudMapRecord",
    # TypedDicts
    "NotableDict",
    "TypeRefConstraint",
    "PipelineArtifact",
    "PipelineVariable",
    "PipelineRunProperties",
    "HostConfig",
    "LocalHostConfig",
    "CloudMapInputs",
    # Type aliases
    "TypeRefStatus",
    "TypeRefJson",
    "TypedUrls",
    "LifecycleStatus",
    "ArtifactDict",
    "ServiceDict",
    "CloudTypeDict",
    "RepositoryDict",
    # Helpers
    "validate_url",
    "filter_dict",
    "join_resource_url",
    "build_oci_purl",
    "get_repository_url",
    # Analyzer base classes & context
    "CloudMapView",
    "Analyzer",
    "RepositoryNotable",
    "URLAnalyzer",
    "AnalyzerContext",
]
