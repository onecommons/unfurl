# Copyright (c) 2025 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Safe-importable cloudmap data type definitions.

Contains the dataclasses, TypedDicts, protocols, and helper functions used by
the cloudmap subsystem. This module is safe to import from sandboxed Analyzer
subclasses.
"""

from __future__ import annotations
from dataclasses import dataclass, asdict, field, InitVar
from functools import total_ordering
import os
import os.path
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from operator import attrgetter
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Tuple,
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
from typing_extensions import Literal, Required, TypedDict, Unpack, Self
from urllib.parse import ParseResult, quote, urlparse, urlunparse, parse_qsl, urlencode

from unfurl.repo import normalize_git_url, split_git_url, git_url_join
from unfurl.util import URI_TEMPLATE_EXPRESSION, has_uri_template, split_url_fragment
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


# the "?" that starts a url's query string (see _URL_FRAGMENT in unfurl/util.py)
_URL_QUERY = re.compile(r"(?<!\{)\?")
# an expression at the end of a url part, e.g. the query of "https://x/app{?tag}"
_TRAILING_URI_TEMPLATE = re.compile(r"\{([+#./;?&])?[^{}]*\}$")
# a url scheme; matched without urlparse so an expression can't be mistaken for one
_URL_SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*(?=:)")
# expression operators that set the same part of a url as the key of this dict
_SAME_URL_PART = {"?": ("?", "&"), "&": ("?", "&"), "#": ("#",)}
# characters urlencode() shouldn't escape when merging query strings
_QUERY_SAFE = ":/@{},*"


def url_scheme(url: str) -> str:
    """Return the scheme of ``url``, or "" if it doesn't have one.

    Unlike :func:`urllib.parse.urlparse` this never raises and a leading URI
    template expression can't be mistaken for a scheme.
    """
    match = _URL_SCHEME.match(url)
    return match.group(0).lower() if match else ""


def _split_url_parts(url: str) -> Tuple[str, str, str]:
    """Split ``url`` into the part before its query, its query and its fragment.

    A "?" or "#" that follows a "{" is the operator of a URI template expression
    (``{?var}``, ``{#var}``), not a delimiter, so :func:`urllib.parse.urlparse`
    can't be used here.
    """
    head, fragment = split_url_fragment(url)
    parts = _URL_QUERY.split(head, 1)
    if len(parts) > 1:
        return parts[0], parts[1], fragment
    return head, "", fragment


def _strip_trailing_template(part: str, operator: str) -> str:
    """Remove a trailing expression from ``part`` if it sets the same url part."""
    trailing = _TRAILING_URI_TEMPLATE.search(part)
    if trailing and (trailing.group(1) or "") in _SAME_URL_PART[operator]:
        return part[: trailing.start()]
    return part


def _join_template_url(base_url: str, join_url: str, expression: "re.Match") -> str:
    """Merge a version key that starts with a URI template expression into ``base_url``.

    The expression's operator says which part of the base url the key sets:
    "?" and "&" the query, "#" the fragment, and "/", ";" and "." the path.

    Args:
        base_url: The url of the record the version is a variant of.
        join_url: The version key, e.g. ``{?tag}``.
        expression: The leading expression of ``join_url``, already matched.

    Returns:
        The version's url, or ``join_url`` unchanged if the expression's
        operator doesn't name a part of the url.
    """
    operator = expression.group(1) or ""
    if operator in ("", "+"):
        # "+" expands to reserved characters too, so the key can be a whole url,
        # and the default operator expands to a single percent-encoded value:
        # neither says what part of the url the key is, so use it as-is
        return join_url
    prefix, query, fragment = _split_url_parts(base_url)
    if operator == "#":
        fragment = ""
    elif operator in ("?", "&") and query and not has_uri_template(query):
        # drop the parameters the expression sets, the way literal keys merge
        varnames = {
            name.partition(":")[0].rstrip("*")
            for name in expression.group(2).split(",")
        }
        params = [
            param
            for param in parse_qsl(query, keep_blank_values=True)
            if param[0] not in varnames
        ]
        query = urlencode(params, safe=_QUERY_SAFE)
    if operator in _SAME_URL_PART:
        # a url has one query and one fragment, so an expression already setting
        # that part is replaced instead of appended to
        if query and operator != "#":
            query = _strip_trailing_template(query, operator)
        else:
            prefix = _strip_trailing_template(prefix, operator)
    if operator in ("?", "&"):
        # a query string starts with "?" and continues with "&"
        wanted = "&" if query else "?"
        if operator != wanted:
            join_url = "{" + wanted + join_url[2:]
    parts = [prefix]
    if operator in ("/", ";", "."):
        parts.append(join_url)  # a path segment, parameter or label
    if query:
        parts.append("?" + query)
    if operator in ("?", "&"):
        parts.append(join_url)
    if fragment:
        parts.append("#" + fragment)
    if operator == "#":
        parts.append(join_url)
    return "".join(parts)


def join_resource_url(base_url: str, join_url: str) -> str:
    assert join_url
    if not url_scheme(base_url):
        return join_url  # if base_url is not an absolute URL, just return the join_url
    expression = URI_TEMPLATE_EXPRESSION.match(
        join_url
    )  # starts with a {var} expression
    if expression and expression.group(1):
        # the key is a URI template, its operator says what part of the url it is.
        # (the default operator is percent-encoded so it expands to a single
        # value, like a bare version key: fall through and treat it as one.)
        return _join_template_url(base_url, join_url, expression)
    base = urlparse(base_url)
    join = urlparse(join_url)
    if base.scheme == "git" and not join.scheme:
        base_url, file_path, revision = split_git_url(base_url)
        if join.fragment:
            return base_url + "#" + join.fragment
        # assume URL is a git ref
        if not base_url.endswith(".git"):
            base_url += ".git"
        return git_url_join(base_url, file_path, join_url)
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
            replace["query"] = urlencode(merged_params, safe=_QUERY_SAFE)
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
    GitHubWorkflow = "cloudmap.artifacts.GitHubWorkflow"
    GitLabPipeline = "cloudmap.artifacts.GitLabPipeline"
    CIRun = "cloudmap.artifacts.CIRun"
    GitLabPipelineRun = "cloudmap.artifacts.GitLabPipelineRun"
    "A record of an individual GitLab CI pipeline execution (subtype of CIRun)."
    GitHubRun = "cloudmap.artifacts.GitHubRun"
    "A record of an individual GitHub Actions workflow run (subtype of CIRun)."


ArtifactMappings = {
    "https://in-toto.io/Statement/v0.1": EntitySchema.InTotoAttestation,
    "https://in-toto.io/Statement/v1": EntitySchema.InTotoAttestation,
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
    """Properties for a CIRun type constraint."""

    id: Required[int]
    run_number: int
    """Human-friendly sequential run number (GitLab pipeline ``iid`` / GitHub ``run_number``)."""
    status: Required[str]
    """Raw outcome of the run (GitLab pipeline ``status`` e.g. ``success``/``failed``;
    GitHub run ``conclusion`` if completed, else its ``status``)."""
    log_url: str
    trigger: Required[str]
    """What triggered the run (GitLab pipeline ``source`` / GitHub run ``event``)."""
    actor: str
    """Username of whoever triggered the run (GitLab pipeline ``user`` / GitHub run ``actor``)."""
    artifacts: List[PipelineArtifact]
    artifacts_expire_at: str
    variables: List[PipelineVariable]
    created_at: str
    """Date and time the run was created/queued, conforming to RFC 3339."""
    started_at: str
    """Date and time the run started executing, conforming to RFC 3339."""
    finished_at: str
    """Date and time the pipeline run finished, conforming to RFC 3339."""
    committed_at: str
    """Date and time of the run's head commit (GitLab ``committed_at`` / GitHub ``head_commit.timestamp``), conforming to RFC 3339."""


class TypeRefConstraint(TypedDict, total=False):
    status: Optional[TypeRefStatus]
    version: Union[int, float, str]
    properties: Dict[str, Any]
    metadata: Dict[str, Any]
    model: str


TypeRefJson = Dict[
    str,
    Optional[TypeRefConstraint],
]

_LABEL_RE = re.compile(r"^[\w.-]+$")


def is_label(key: str) -> bool:
    """Return True if ``key`` looks like a label rather than a URL.

    Labels are restricted to word characters, ``-`` and ``.`` (the ``[\\w.-]``
    character class); anything containing other characters (e.g. ``:`` or ``/``
    as found in URLs and file paths) is not a label."""
    return bool(_LABEL_RE.match(key))


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
        self.metadata: Dict[str, Any] = {}
        if types is None:
            self.types: TypeRefJson = {}
        elif isinstance(types, str):
            self.types = {types: None}
        else:
            assert isinstance(types, Mapping), (
                "TypeRefs must be initialized with a dict or a string"
            )
            self.types = dict(types)  # copy
            if "metadata" in self.types:
                self.metadata = cast(dict, self.types.pop("metadata") or {})

    def asdict(self) -> TypeRefJson:
        """Return JSON representation of typeRef."""
        types = {k: filter_dict(self.types[k]) or None for k in sorted(self.types)}
        if self.metadata:
            types["metadata"] = cast(TypeRefConstraint, self.metadata)
        return types

    def aslist(self) -> List[Tuple[str, Optional[Any]]]:
        """Return list of (type name, constraints) pairs."""
        return [(n, dict(c) if c else None) for n, c in self.types.items()]

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
    def urls_asdict(typed_urls: "TypedUrls") -> Dict[str, Any]:
        """Serialize a :data:`TypedUrls` map back to its JSON representation.

        Keys are ``(label, url)`` tuples. Entries are grouped by label:

        - ``("", url)`` becomes ``{url: typeRefs}`` (the plain form).
        - ``(label, "")`` becomes ``{label: typeRefs}``.
        - ``(label, url)`` entries sharing a label are nested under it:
          ``{label: {url: typeRefs, ...}}``.
        """
        result: Dict[str, Any] = {}
        for label, url in sorted(typed_urls):
            type_refs = typed_urls[(label, url)]
            if isinstance(type_refs, TypeRefs):
                value: Optional[TypeRefJson] = type_refs.asdict() or None
            else:
                value = type_refs
            if not label:
                result[url] = value
            elif not url:
                result[label] = value
            else:
                nested = result.setdefault(label, {})
                nested[url] = value
        return result

    @staticmethod
    def urls_fromdict(
        type_urls_dict: Union["TypedUrls", Dict[str, Any]],
        keys_are_urls: bool = False,
    ) -> "TypedUrls":
        """Normalize a JSON typedURLs map (or an already-parsed :data:`TypedUrls`)
        into ``{(label, url): Optional[TypeRefs]}``.

        Supports two JSON forms:

        - ``url: typeRefs | null`` -> ``("", url)`` (the key is not a label).
        - ``label: {url: typeRefs | null}`` -> ``(label, url)`` for each nested
          url. If the value of a label is instead a plain typeRefs map (its keys
          are type names, not urls) the entry is stored as ``(label, "")``.

        When ``keys_are_urls`` is True assume the key is a url and always use the first form.
        """
        type_urls: TypedUrls = {}
        for k, v in type_urls_dict.items():
            if isinstance(k, tuple):
                # already a (label, url) key
                type_urls[k] = v if isinstance(v, TypeRefs) else TypeRefs(types=v)
            elif keys_are_urls or not is_label(k):
                # the key is a url or file path, no label
                type_urls[("", k)] = v if isinstance(v, TypeRefs) else TypeRefs(types=v)
            else:
                if isinstance(v, Mapping) and _keys_are_urls(v):
                    # label: {url: typeRefs} nested form
                    for url, tr in v.items():
                        type_urls[(k, url)] = (
                            tr if isinstance(tr, TypeRefs) else TypeRefs(types=tr)
                        )
                else:
                    # label whose value is a plain typeRefs map (or null)
                    type_urls[(k, "")] = (
                        v if isinstance(v, TypeRefs) else TypeRefs(types=v)
                    )
        return type_urls


# A URL is a string that starts with a valid URI scheme followed by ``:``
# (RFC 3986 § 3.1: ALPHA *( ALPHA / DIGIT / "+" / "-" / "." )). This mirrors
# the ``propertyNames`` patterns used to discriminate url keys from type-name
# keys in ``unfurl/cloudmap/cloudmap-schema.json`` and ``is_url`` in
# ``rust/git-sync/src/formats/cloudmap.rs``.
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _is_url(key: str) -> bool:
    """Return True if ``key`` is a URL (starts with a URI scheme).
    Simple test to distinguish global type names from URLs."""
    return bool(_URL_SCHEME_RE.match(key))


def _keys_are_urls(value: "Mapping[str, Any]") -> bool:
    """True if ``value`` is a non-empty map whose keys are all urls."""
    return bool(value) and all(_is_url(key) for key in value)


TypedUrls = Dict[Tuple[str, str], Optional[TypeRefs]]


class CloudMapRecord:
    @property
    def key(self) -> str:
        """The unique key for this record."""
        raise NotImplementedError("Subclasses must implement the key property")

    def asdict(self) -> Dict[str, Any]:
        return {}


class VersionedRecord(CloudMapRecord):
    url: str
    versions: Dict[str, Self]
    type: TypeRefs

    @property
    def key(self) -> str:
        return self.url

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

    Stored in CloudMapDB.instantiations with URL keys and referenced by the
    ``instantiated_by`` of an artifact, a component or a service.
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
    status: Optional[LifecycleStatus] = None
    """Lifecycle status of the instantiation"""
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
    contains: TypedUrls = field(default_factory=dict)
    """"Map of URLs of interesting artifacts that this artifact embeds or incorporates."""
    references: TypedUrls = field(default_factory=dict)
    """Map of URLs of interesting artifacts, repositories or services that this artifact may reference when executed or instantiated."""
    instantiates: TypedUrls = field(default_factory=dict)
    """Map of URLs (or labels) of entities this artifact instantiates with optional type constraints."""
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
            scheme = url_scheme(self.url)
            if not scheme:
                # a URI template can expand into the scheme (e.g. "{+urlvar}"),
                # so leave it alone -- converting it would escape the expression
                if not has_uri_template(self.url):
                    # migrate old cloudmap format
                    self.url = build_oci_purl(ContainerImageParts.split(self.url))
            elif scheme not in ["pkg", "git"]:
                raise ValueError(f"Artifact.url must be a pkg URL: {self.url!r}")

        if not isinstance(self.metadata, ArtifactMetadata):
            self.metadata = ArtifactMetadata(**(self.metadata or {}))
        if not isinstance(self.discovery, Discovery):
            self.discovery = Discovery(**(self.discovery or {}))
        if not isinstance(self.type, TypeRefs):
            self.type = TypeRefs(types=self.type)
        self.instantiates = TypeRefs.urls_fromdict(self.instantiates)
        self.dependencies = TypeRefs.urls_fromdict(self.dependencies)
        self.contains = TypeRefs.urls_fromdict(self.contains)
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

    def get_repository_url(self) -> Optional[str]:
        """If the artifact references a repository, return the git:// URL for that repository."""
        if self.url.startswith("git://"):
            # a "#" inside a URI template expression isn't the fragment
            url = split_url_fragment(self.url)[0]
            if not url.endswith(".git") and not _TRAILING_URI_TEMPLATE.search(url):
                # (an expression can expand into the ".git" suffix)
                url += ".git"
            return url
        return None

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
            elif k == "contains":
                v = TypeRefs.urls_asdict(v)
            elif k == "references":
                v = TypeRefs.urls_asdict(v)
            elif k == "type" and v:
                v = v.asdict() if isinstance(v, TypeRefs) else v
            elif k == "instantiates":
                v = TypeRefs.urls_asdict(v)
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


@dataclass
class Component(VersionedRecord):
    """
    A component that describes relationships (references, instantiates,
    dependencies, instantiated_by) and is identified by URL or label.
    """

    url: str
    type: TypeRefs = field(default_factory=TypeRefs)
    """Type identifier from types/components with optional version constraints"""
    contains: TypedUrls = field(default_factory=dict)
    """Map of URLs of interesting artifacts that this component embeds or incorporates."""
    references: TypedUrls = field(default_factory=dict)
    """Map of URLs of interesting artifacts, repositories or services that this component may reference when executed or instantiated."""
    instantiates: TypedUrls = field(default_factory=dict)
    """Map of URLs (or labels) of entities this component instantiates with optional type constraints."""
    dependencies: TypedUrls = field(default_factory=dict)
    """Software, services, or environment context that this component may depend on. Keys are labels or URLs, values are type constraints of components or capabilities."""
    instantiated_by: TypedUrls = field(default_factory=dict)
    """URLs referencing entries in instantiations with optional type constraints."""
    metadata: CommonMetadata = field(default_factory=CommonMetadata)
    """Human-readable metadata"""
    status: Optional[LifecycleStatus] = None
    """Lifecycle status of the component."""
    versions: Dict[str, "Component"] = field(default_factory=dict)
    """Components that are variants of this component"""
    _parent: InitVar[Optional["Component"]] = None

    def __post_init__(self, _parent: Optional["Component"] = None):
        if self.url:
            self.url = validate_url(self.url, "Component.url")
        if not isinstance(self.metadata, CommonMetadata):
            self.metadata = CommonMetadata(**(self.metadata or {}))
        if not isinstance(self.type, TypeRefs):
            self.type = TypeRefs(types=self.type)
        self.contains = TypeRefs.urls_fromdict(self.contains)
        self.references = TypeRefs.urls_fromdict(self.references)
        self.instantiates = TypeRefs.urls_fromdict(self.instantiates)
        self.dependencies = TypeRefs.urls_fromdict(self.dependencies)
        if isinstance(self.instantiated_by, list):
            self.instantiated_by = {url: None for url in self.instantiated_by}
        self.instantiated_by = TypeRefs.urls_fromdict(self.instantiated_by)
        if self.versions:
            self.versions = self._load_versions()
        self._parent = _parent  # type: ignore  # (don't mark as field to exclude from asdict)

    def asdict(self) -> Dict[str, Any]:
        result = {}
        for k, v in asdict(self).items():
            if k == "url":
                continue  # skip url, save as the key instead
            if k == "metadata":
                v = filter_dict(v)
            elif k == "type" and v:
                v = v.asdict() if isinstance(v, TypeRefs) else v
            elif k == "contains":
                v = TypeRefs.urls_asdict(v)
            elif k == "references":
                v = TypeRefs.urls_asdict(v)
            elif k == "instantiates":
                v = TypeRefs.urls_asdict(v)
            elif k == "dependencies":
                v = TypeRefs.urls_asdict(v)
            elif k == "instantiated_by":
                v = TypeRefs.urls_asdict(v)
            elif k == "versions" and v:
                v = {
                    url: (c.asdict() if isinstance(c, Component) else c)
                    for url, c in v.items()
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


@dataclass
class Repository(CloudMapRecord):
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
    contains: TypedUrls = field(default_factory=dict)
    """Map of artifact URLs (for files or directories in the repository) that are useful for characterizing the repository and integrating it with the other resources in the cloud map, with optional type constraints."""

    @property
    def key(self) -> str:
        return self.url

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
        # contains keys are repo-relative file paths (url-parts), not labels
        self.contains = TypeRefs.urls_fromdict(self.contains, keys_are_urls=True)

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
        result: Dict[str, Any] = {}
        for k, v in asdict(self).items():
            if k == "url":
                continue
            if k == "metadata":
                v = filter_dict(v)
            elif k == "contains":
                v = TypeRefs.urls_asdict(v)
            if v:
                result[k] = v
        return result

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

    def add_notables(self, notables: List["RepositoryAnalyzer"]) -> None:
        notables.sort(key=attrgetter("path"))
        # analyzers contribute entries keyed by repo-relative file path
        contains: Dict[str, Optional[TypeRefs]] = {}
        for n in notables:
            if n.contains is not None:
                # analyzer mapped to multiple entries (e.g. one per workflow file)
                contains.update(n.contains)
            else:
                type_refs = (
                    TypeRefs({n.artifact_type: None}) if n.artifact_type else None
                )
                contains[n.path] = type_refs
        # keep entries ordered by path even when an analyzer contributed several,
        # normalizing the file-path keys into ("", url) form
        self.contains = TypeRefs.urls_fromdict(
            {k: contains[k] for k in sorted(contains)}, keys_are_urls=True
        )

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
class CloudType(CloudMapRecord):
    """A type definition for artifacts, services, software, or capabilities."""

    name: str
    """Fully-qualified type name with namespace"""
    kind: Literal["Component", "Artifact", "Capability"]
    source: str = ""
    """Artifact containing type definition"""
    extends: List[str] = field(default_factory=list)
    """List of fully-qualified type names that this type extends"""
    status: Optional[
        Literal["draft", "experimental", "stable", "deprecated", "removed"]
    ] = None
    """Maturity level of the type definition"""
    model: str = ""
    """URL of artifact or service to use as a model for instances of this type"""
    metadata: CommonMetadata = field(default_factory=CommonMetadata)
    """Additional metadata about the type"""
    properties: Optional[Dict[str, Any]] = None
    """JSON Schema describing the properties of instances of this type."""

    @property
    def key(self) -> str:
        return self.name

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
ComponentDict = Dict[str, Component]
CloudTypeDict = Dict[str, CloudType]
RepositoryDict = Dict[str, Repository]


T = TypeVar("T", bound="RepositoryAnalyzer")


class CloudMapView(ABC):
    """Abstract base class for cloudmap views."""

    # --- Look up existing records ---

    @abstractmethod
    def get_artifact(self, url: str) -> Optional["Artifact"]: ...

    @abstractmethod
    def get_service(self, url: str) -> Optional["Service"]: ...

    @abstractmethod
    def get_component(self, url: str) -> Optional["Component"]: ...

    @abstractmethod
    def get_instantiation(self, url: str) -> Optional["Instantiation"]: ...

    @abstractmethod
    def get_type(self, name: str) -> Optional["CloudType"]: ...

    @abstractmethod
    def get_repository(self, r: Union[str, Repository]) -> Optional[Repository]: ...

    # --- Iterate / search records ---

    @abstractmethod
    def find_artifacts(self, type: str = "") -> Iterable["Artifact"]:
        """An empty filter returns all artifacts."""

    @abstractmethod
    def find_services(self, type: str = "") -> Iterable["Service"]:
        """An empty filter returns all services."""

    @abstractmethod
    def find_components(self, type: str = "") -> Iterable["Component"]:
        """An empty filter returns all components."""

    @abstractmethod
    def find_instantiations(self, type: str = "") -> Iterable["Instantiation"]:
        """An empty filter returns all instantiations."""

    @abstractmethod
    def find_types(self) -> Iterable["CloudType"]: ...

    @abstractmethod
    def find_repositories(self) -> Iterable["Repository"]: ...

    def resolve_cloudmap_url(self, cloudmap_url: str) -> Optional[str]:
        "Convert 'cloudmap:<package_id>' pseudo-URLs to resolvable (e.g. https://) git URL."
        # call split_git_url to parse the #fragment
        repo_url, filePath, revision = split_git_url(cloudmap_url)
        found_prefix = ""
        for prefix in ("cloudmap:", "repository:", "artifact:", "instantiation:"):
            if repo_url.startswith(prefix):
                found_prefix = prefix
                repo_url = repo_url[len(prefix) :]
        repo_record = self.get_repository(repo_url)
        if repo_record:
            repo_url = repo_record.git_url()
        else:
            # XXX if found_prefix = artifact or instantiation, get source from record
            repo_url = repo_url.replace("git://", "https://")
        return git_url_join(repo_url, filePath, revision)


class AnalyzerContext(CloudMapView):
    """Abstract base class for the cloudmap context analyzers see.

    Exposes the subset of :class:`Directory` / :class:`CloudMapDB` functionality that
    custom Analyzer subclasses (possibly loaded in safe mode) need to contribute
    records to the cloudmap. Attributes with a leading underscore are inaccessible
    from sandboxed code and are intended for built-in Analyzer classes only.
    """

    logger: "UnfurlLogger"
    """Logger for emitting diagnostic messages during analysis."""

    do_analysis: bool
    """True if cross-referenced URLs should be recursively analyzed (vs. recorded
    as stubs)."""

    @property
    @abstractmethod
    def _local__env(self) -> Optional["LocalEnv"]:
        """Parent environment for loading nested unfurl projects.

        Names with _<name>__<suffix> can not be accessed from sandboxed Notables,
        so this is only accessible to built-in Analyzer classes and not custom Notables loaded in safe mode.
        """

    # --- Add records ---

    @abstractmethod
    def add_record(self, record: "CloudMapRecord") -> None:
        """Add or replace a record in the cloudmap.
        For :class:`VersionedRecord` (``Artifact`` / ``Service``
        / ``Instantiation``), each entry of ``record.versions`` are
        also added under its own ``obj.url``
        """

    @abstractmethod
    def delete_record(self, record: "CloudMapRecord") -> None:
        """Remove a record from the cloudmap.

        Inverse of :meth:`add_record`: dispatches by concrete type
        and removes the entry from the matching per-section storage.
        For :class:`VersionedRecord` subclasses each entry of
        ``record.versions`` is also removed under its own
        ``obj.url`` so a parent delete cleans up its variants too.
        Implementations should treat missing keys as a no-op rather
        than raising.
        """

    @abstractmethod
    def analyze_url(
        self,
        url: str,
        analyze: Literal["yes", "no", "save-only", "default"] = "default",
    ) -> Optional["CloudMapRecord"]:
        """Analyze a URL (git repo, pkg: PURL, or service URL) and add the
        resulting record to the cloudmap. Returns the record that was
        added or already existed."""

    @abstractmethod
    def add_image_artifact(self, image: "ContainerImage") -> "Artifact": ...


class Analyzer:
    """Common base for cloudmap analyzers."""


class RepositoryAnalyzer(Analyzer):
    """
    Base class for plugins that discover notable files or directories in a repository
    -- for example, a Dockerfile, Helm chart, or TOSCA service template.

    Subclasses declare which filenames or directory names they match via the
    ``files`` and ``folders`` class attributes. The ``RepositoryAnalyzer`` walks
    a repository tree, instantiates the appropriate Analyzer subclass for each
    match, and calls ``analyze()`` to produce an ``Artifact`` for the cloud map.

    Attributes:
        files: Filenames that this Analyzer class matches (e.g. ``["Dockerfile"]``).
        folders: Directory names that this Analyzer class matches (e.g. ``["charts"]``).
        artifact_type: The artifact type to assign to matched artifacts (inherited from :class:`Analyzer`).
    """

    artifact_type: str = EntitySchema.GenericFile
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
        # keyed by repo-relative file path; normalized to (label, url) form
        # when merged into a Repository by Repository.add_notables()
        self.contains: Optional[Dict[str, Optional[TypeRefs]]] = None

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
    def _exist_in_folder(cls, folder: str, notables: List["RepositoryAnalyzer"]):
        """Check whether a Analyzer of this class already exists for the given folder."""
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
        """Factory method for creating a Analyzer instance.

        Subclasses can override this to conditionally reject a match
        (by returning None) or to customize initialization.
        """
        return cls(folder, file, digest)


class URLAnalyzer(Analyzer):
    """Base class for analyzers that produce records from a URL.

    Whereas :class:`RepositoryAnalyzer` analyzes files/directories inside a git
    repository, ``URLAnalyzer`` subclasses handle URLs directly — for
    example PURL-based references like ``pkg:oci/...``, ``pkg:npm/...``, or
    custom schemes contributed by plugins.

    Subclasses declare which URL-prefix(es) they handle via the
    ``url_schemes`` class attribute (longest-prefix wins, so ``"pkg:oci"``
    beats ``"pkg:"`` for an OCI image). They override two methods:

    - :py:meth:`init_from_url` — factory that parses the URL and returns a
      configured instance, or ``None`` to decline (e.g. malformed input).
    - :py:meth:`analyze_url` — produces an :class:`Artifact` (and any related
      :class:`Instantiation` records via the passed-in :class:`AnalyzerContext`).

    Custom subclasses can be loaded via the ``cloudmaps.analyzers`` config in
    the same way as :class:`RepositoryAnalyzer` subclasses.
    """

    url_schemes: Sequence[str] = ()

    @classmethod
    def init_from_url(cls, url: str, parsed: ParseResult) -> Optional[Self]:
        """Construct an analyzer instance for ``url`` or return ``None`` to decline.

        Override in subclasses. The default implementation returns ``None``,
        meaning the analyzer cannot handle any URL.
        """
        return None

    def analyze_url(self, directory: "AnalyzerContext") -> Optional["VersionedRecord"]:
        """Produce an Artifact for the URL this analyzer was constructed with.

        Subclasses can also write related records (Instantiation, Service,
        etc.) directly via ``directory.add_*`` methods. The returned
        :class:`Artifact` is added to the cloudmap by the caller; return
        ``None`` if no artifact should be recorded.
        """
        return None


class PipelineRunAnalyzer(Analyzer):
    """Analyzer for CI pipelines / workflow runs.

    Subclasses are matched by the ``repositories``, ``sources``, and
    ``source_types`` class attributes; an empty attribute acts as a wildcard
    (matches anything). They override :py:meth:`analyze_pipeline_run`.

    Class attributes:
        repositories: Repository urls to match. Matches if any url matches the
            pipeline's repository (or a repository it derives from via
            ``fork_of`` / ``mirror_of``). Empty matches any repository.
        sources: Relative workflow file paths to match, e.g.
            ``".github/workflows/foo.yaml"``. Matches if any path equals the
            pipeline's source. Empty matches any source.
        source_types: :class:`TypeRefs` to match against the source file's type
            references (found in the repository's ``contains`` map or, failing
            that, on the source's :class:`Artifact` record). Matches if any
            entry's type names are all present in the source's type references.
            Empty matches any source type.
    """

    repositories: Sequence[str] = ()
    sources: Sequence[str] = ()
    source_types: Sequence[TypeRefs] = ()

    def analyze_pipeline_run(
        self,
        context: "AnalyzerContext",
        repo_info: "Repository",
        instantiation: "Instantiation",
        obj: Any,
        root_path: str,
    ) -> None:
        """Enrich ``instantiation`` in place.

        Args:
            context: The cloudmap analyzer context (for adding related records).
            repo_info: The Repository the pipeline run belongs to.
            instantiation: The Instantiation record for the pipeline/run.
            obj: The platform pipeline / workflow-run object. Set to ``None``
                when running in safe mode (sandboxed analyzers don't get the
                raw API object).
            root_path: The repository's local working directory, or ``""`` if
                the repository is not cloned locally.
        """


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
    "Component",
    # Supporting classes
    "TypeRefs",
    "EntitySchema",
    "ArtifactMappings",
    "CloudMapRecord",
    # TypedDicts
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
    "ComponentDict",
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
    "RepositoryAnalyzer",
    "URLAnalyzer",
    "AnalyzerContext",
]
