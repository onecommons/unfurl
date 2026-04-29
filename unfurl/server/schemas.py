# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Pydantic models used as APIFlask input/output schemas for the unfurl server API.

These are used with @app.input() and @app.output() decorators to provide
request validation and OpenAPI spec generation.
"""

import json
import os
from functools import lru_cache
from typing import Any, Dict, List, Optional, Union
from typing_extensions import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from ..graphql import (
    ApplicationBlueprint as ApplicationBlueprintType,
    Deployment as DeploymentType,
    DeploymentEnvironment as DeploymentEnvironmentType,
    DeploymentPath as DeploymentPathType,
    DeploymentTemplate as DeploymentTemplateType,
    ResourceTemplate as ResourceTemplateType,
    ResourceType as ResourceTypeType,
)


# ---------------------------------------------------------------------------
# CloudMap graph TypedDicts (also used by unfurl.reporting)
# ---------------------------------------------------------------------------


class TypeRefJson(TypedDict, total=False):
    """A type reference in the graph, used in relationship lists."""

    type: str
    constraints: Dict[str, Any]
    label: str


class RecordRef(TypedDict, total=False):
    """A reference to a record in the graph, used in relationship lists."""

    url: str
    kind: str
    missing: bool
    type_refs: List[TypeRefJson]


RelEntry = Union[RecordRef, TypeRefJson, str]


class GraphNodeJson(TypedDict, total=False):
    """A node in the JSON graph representation."""

    kind: str
    url: str
    rels: Dict[str, List[RelEntry]]


class GraphJson(TypedDict, total=False):
    """Top-level JSON graph structure.

    In section mode (full graph), ``sections`` maps section names to dicts of
    url → GraphNodeJson.  In single-record mode, ``roots`` lists the queried
    record refs and all encountered records are stored in ``sections``.
    """

    sections: Dict[str, Dict[str, GraphNodeJson]]
    roots: List[RecordRef]
    error: str


class CloudMapResponse(BaseModel):
    """Pydantic wrapper for :class:`GraphJson`, used by ``@app.output``."""

    model_config = ConfigDict(extra="allow")

    sections: Optional[Dict[str, Dict[str, GraphNodeJson]]] = Field(
        default=None, description="Map of section name → {url → GraphNodeJson}"
    )
    roots: Optional[List[RecordRef]] = Field(
        default=None, description="List of root record references (single-record mode)"
    )
    error: Optional[str] = Field(
        default=None, description="Error message when the record is not found"
    )

    @model_serializer(mode="wrap")
    def _exclude_none(self, handler):
        return {k: v for k, v in handler(self).items() if v is not None}


# ---------------------------------------------------------------------------
# Query parameter schemas
# ---------------------------------------------------------------------------


class ProjectQuery(BaseModel):
    """Common auth query parameter shared by all endpoints."""

    model_config = ConfigDict(
        extra="allow"
    )  # pass unknown query params through to request.args

    auth_project: Optional[str] = Field(
        default=None, description="Project ID for authorization and cache key scoping"
    )


class ExportBaseQuery(ProjectQuery):
    """Shared query parameters for /export and /types."""

    latest_commit: Optional[str] = Field(
        default=None, description="Commit hash used to validate the cache entry"
    )
    branch: Optional[str] = Field(default=None, description="Git branch name")
    pretty: bool = Field(default=False, description="Pretty-print the JSON response")
    username: Optional[str] = Field(
        default=None,
        description="Git username (alternative to X-Git-Credentials header)",
    )
    visibility: Optional[Literal["public", "private"]] = Field(
        default=None, description="Repository visibility"
    )
    queueid: Optional[str] = Field(
        default=None,
        description="If set, the Rust proxy will enqueue the request for async processing via Redis instead of proxying synchronously",
    )


class ExportQuery(ExportBaseQuery):
    """Query parameters for /export."""

    format: Literal["deployment", "blueprint", "environments"] = Field(
        default="deployment", description="Export format"
    )
    deployment_path: Optional[str] = Field(
        default=None, description="Path to the deployment within the project"
    )
    environment: Optional[str] = Field(
        default=None, description="Environment name (used with 'environments' format)"
    )
    include_all_deployments: bool = Field(
        default=False,
        description="Include all deployment exports embedded in the response",
    )
    stale: Optional[Literal["ok"]] = Field(
        default=None,
        description="Return any cache hit without checking if it's out of date.",
    )


class TypesQuery(ExportBaseQuery):
    """Query parameters for /types."""

    file: str = Field(
        default="dummy-ensemble.yaml", description="Filename used as template context"
    )
    cloudmap: Optional[str] = Field(
        default=None,
        description="CloudMap project ID to merge types from, e.g. 'onecommons/cloudmap'",
    )


CloudMapKind = Literal[
    "repositories", "artifacts", "services", "instantiations", "types"
]


class CloudMapQuery(ProjectQuery):
    """Query parameters for /cloudmap (graph) endpoint."""

    url: Optional[str] = Field(
        default=None,
        description="Optional artifact or instantiation URL to filter the graph to",
    )


class CloudMapDocQuery(ProjectQuery):
    """Query parameters for /cloudmap (raw document) endpoint."""

    kind: Optional[CloudMapKind] = Field(
        default=None,
        description=(
            "Top-level CloudMap section to return; if omitted the full "
            "document is returned."
        ),
    )
    key: Optional[str] = Field(
        default=None,
        description=(
            "Record key (URL) within the selected ``kind`` section; "
            "ignored when ``kind`` is omitted."
        ),
    )
    follow: int = Field(
        default=0,
        description=(
            "If > 0 and ``key`` is supplied, walk the CloudMap graph "
            "starting at ``key`` and return the discovered records in "
            "the second element of the response pair. Otherwise the "
            "second element is an empty dict."
        ),
    )


# ---------------------------------------------------------------------------
# CloudMap document response — references docs/cloudmap-schema.json
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_cloudmap_schema() -> Dict[str, Any]:
    """Return the canonical cloudmap JSON schema as a dict."""
    here = os.path.dirname(os.path.abspath(__file__))
    schema_path = os.path.join(here, "..", "..", "docs", "cloudmap-schema.json")
    with open(schema_path, "r") as f:
        return json.load(f)


class CloudMapDocument(BaseModel):
    """Placeholder Pydantic model for the CloudMap document response.

    The model intentionally has no fields — APIFlask emits an empty
    stub for it, which is then replaced wholesale by
    :func:`hoist_cloudmap_definitions` (registered as a
    ``@app.spec_processor``) with the contents of
    ``docs/cloudmap-schema.json``.  Doing the substitution at spec-build
    time avoids Pydantic v2's internal ``$defs`` ref-counting machinery.
    """

    model_config = ConfigDict(extra="allow")


class CloudMapDocumentPair(BaseModel):
    """Placeholder for the ``/cloudmap`` response: a 2-element array of
    CloudMap documents.

    APIFlask emits an empty stub which :func:`hoist_cloudmap_definitions`
    replaces with a fixed-length array schema referencing
    :class:`CloudMapDocument`.
    """

    model_config = ConfigDict(extra="allow")


def _rewrite_refs_to_components(node: Any, prefix: str = "cloudmap_") -> Any:
    """Return a deep copy of ``node`` with ``#/definitions/<X>``
    rewritten to ``#/components/schemas/{prefix}<X>``."""
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str) and v.startswith("#/definitions/"):
                out[k] = "#/components/schemas/" + prefix + v.split("/", 2)[2]
            else:
                out[k] = _rewrite_refs_to_components(v, prefix)
        return out
    if isinstance(node, list):
        return [_rewrite_refs_to_components(v, prefix) for v in node]
    return node


def hoist_cloudmap_definitions(spec: Dict[str, Any]) -> Dict[str, Any]:
    """APIFlask ``spec_processor``: replace the placeholder
    ``CloudMapDocument`` schema with the canonical CloudMap schema and
    hoist its ``definitions`` into ``components.schemas`` under a
    ``cloudmap_`` prefix.

    Internal ``$ref`` arrows of the form ``#/definitions/<name>`` are
    rewritten to ``#/components/schemas/cloudmap_<name>`` so they
    resolve in the OpenAPI spec.
    """
    components = spec.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    has_doc = "CloudMapDocument" in schemas
    has_pair = "CloudMapDocumentPair" in schemas
    if not (has_doc or has_pair):
        return spec
    canonical = _load_cloudmap_schema()
    defs = canonical.get("definitions", {})
    for name, definition in defs.items():
        schemas["cloudmap_" + name] = _rewrite_refs_to_components(definition)
    schemas["CloudMapDocument"] = {
        "title": canonical.get("title", "CloudMap"),
        "type": canonical.get("type", "object"),
        "properties": _rewrite_refs_to_components(canonical.get("properties", {})),
        "required": canonical.get("required", []),
    }
    if has_pair:
        schemas["CloudMapDocumentPair"] = {
            "title": "CloudMap document pair",
            "description": (
                "Two-element array: the queried (and optionally filtered) "
                "CloudMap document followed by a CloudMap document of "
                "records discovered by following the graph from the "
                "filter key (empty dict when ``follow`` is 0 or no key "
                "was supplied)."
            ),
            "type": "array",
            "items": {"$ref": "#/components/schemas/CloudMapDocument"},
            "minItems": 2,
            "maxItems": 2,
        }
    return spec


class PopulateCacheQuery(ProjectQuery):
    """Query parameters for /populate_cache."""

    branch: str = Field(
        default="main",
        description="Branch name; also accepts 'refs/heads/…' and 'refs/tags/…' prefixes",
    )
    path: str = Field(description="File path relative to the project root")
    latest_commit: str = Field(description="Latest commit hash for this file")
    removed: Optional[str] = Field(
        default=None,
        description="If truthy (not '0' or 'false'), delete the cache entry instead of populating it",
    )
    visibility: Optional[Literal["public", "private"]] = Field(
        default=None,
        description="Repository visibility; private repositories are not cloned automatically",
    )


class EmptyCacheQuery(ProjectQuery):
    """Query parameters for /empty_cache."""

    auth_project: str = Field(  # type: ignore[assignment]  # overrides Optional[str] in parent
        description="Must equal the UNFURL_SERVER_ADMIN_PROJECT environment variable"
    )
    cache_prefix: Optional[str] = Field(
        default=None,
        description="Cache key prefix to clear; defaults to the server-configured prefix",
    )


class ClearProjectQuery(ProjectQuery):
    """Query parameters for /clear_project_file_cache."""


# ---------------------------------------------------------------------------
# Request body schemas
# ---------------------------------------------------------------------------


class PatchEnvironmentBody(BaseModel):
    """JSON body for /delete_deployment, /update_environment, and /delete_environment."""

    model_config = ConfigDict(extra="allow")  # allow extra fields added by _get_body

    patch: List[Dict[str, Any]] = Field(
        description="List of patch operations describing the changes to apply"
    )
    branch: str = Field(default="main", description="Target branch")
    latest_commit: Optional[str] = Field(
        default=None,
        description="Latest known commit hash for optimistic concurrency checks",
    )
    username: Optional[str] = Field(
        default=None, description="Git username for pushing the commit"
    )
    private_token: Optional[str] = Field(
        default=None, description="Git personal access token or password"
    )
    commit_msg: Optional[str] = Field(default=None, description="Git commit message")
    queueid: Optional[str] = Field(
        default=None,
        description="If set, the Rust proxy will enqueue the request for async processing via Redis instead of proxying synchronously",
    )


class PatchEnsembleBody(PatchEnvironmentBody):
    """JSON body for /create_ensemble, /update_ensemble, and /create_provider.

    Extends PatchEnvironmentBody with ensemble-specific fields.
    """

    environment: Optional[str] = Field(
        default=None, description="Deployment environment name"
    )
    deployment_path: Optional[str] = Field(
        default=None, description="Path for the deployment within the project"
    )
    cloud_vars_url: Optional[str] = Field(
        default=None,
        description="URL for cloud variables used for vault secret encryption",
    )
    deployment_blueprint: Optional[str] = Field(
        default=None,
        description="Name of the deployment blueprint to use when creating an ensemble",
    )
    blueprint_url: Optional[str] = Field(
        default=None,
        description="Remote blueprint URL to clone when creating an ensemble",
    )


class BatchPatchBody(BaseModel):
    """JSON body for /batch_patch -- used by the Rust proxy to forward
    a batch of write requests that share the same branch and latest_commit.

    The ``requests`` list preserves the original submission order so the
    Python backend can apply each operation sequentially before pushing once.
    """

    model_config = ConfigDict(extra="allow")

    latest_commit: Optional[str] = Field(
        default=None,
        description="Latest known commit hash for optimistic concurrency checks",
    )
    branch: str = Field(default="main", description="Target branch")
    requests: List[Dict[str, Any]] = Field(
        description="Ordered list of original requests, each with 'endpoint' and the original body fields"
    )
    queueid: Optional[int] = Field(
        default=None,
        description="Current queueid from the Rust proxy; Python updates the Redis queue key with '{new_commit},{queueid}' after committing",
    )


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """Error response returned by all endpoints on failure."""

    code: str = Field(
        description="Error code (e.g. BAD_REQUEST, UNAUTHORIZED, INTERNAL_ERROR)"
    )
    message: str = Field(description="Human-readable error message")
    details: Optional[str] = Field(
        default=None,
        description="Full exception traceback, included when an unexpected error occurs",
    )


class PatchResponse(BaseModel):
    """Response from all write endpoints after a successful commit."""

    commit: Optional[str] = Field(
        default=None,
        description="Commit hash after applying the patch, or null if no changes were committed",
    )


class ExportResponse(BaseModel):
    """GraphQL-style JSON database returned by /export and /types.

    Each top-level key maps to a dict of named GraphQL objects of that type,
    reflecting the GraphQL schema defined in unfurl/graphql.py.
    """

    model_config = ConfigDict(
        extra="allow"
    )  # allow format-specific keys not listed here

    ResourceType: Optional[Dict[str, ResourceTypeType]] = Field(
        default=None,
        description="Map of type name → ResourceType object (TOSCA node type)",
    )
    ResourceTemplate: Optional[Dict[str, ResourceTemplateType]] = Field(
        default=None,
        description="Map of template name → ResourceTemplate object (TOSCA node template)",
    )
    DeploymentTemplate: Optional[Dict[str, DeploymentTemplateType]] = Field(
        default=None, description="Map of blueprint name → DeploymentTemplate object"
    )
    ApplicationBlueprint: Optional[Dict[str, ApplicationBlueprintType]] = Field(
        default=None, description="Map of blueprint name → ApplicationBlueprint object"
    )
    DeploymentEnvironment: Optional[DeploymentEnvironmentType] = Field(
        default=None,
        description="DeploymentEnvironment object (deployment and environments formats)",
    )
    DeploymentPath: Optional[Dict[str, DeploymentPathType]] = Field(
        default=None,
        description="Map of path → DeploymentPath object (registered ensemble paths)",
    )
    Deployment: Optional[Dict[str, DeploymentType]] = Field(
        default=None,
        description="Map of deployment name → Deployment object (deployment format only)",
    )
    deployments: Optional[List[Union[DeploymentType, Dict[str, Any]]]] = Field(
        default=None,
        description="Embedded deployment exports (present when include_all_deployments=true)",
    )


# ---------------------------------------------------------------------------
# Reusable response-code sets for @app.doc(responses=...)
# ---------------------------------------------------------------------------

EXPORT_RESPONSES: Dict[int, str] = {
    304: "Not Modified (ETag matched)",
    401: "Unauthorized",
    500: "Internal error",
}

PATCH_RESPONSES: Dict[int, str] = {
    401: "Unauthorized",
    409: "Conflict (repository at wrong revision)",
    500: "Internal error",
}
