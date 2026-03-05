# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Pydantic models used as APIFlask input/output schemas for the unfurl server API.

These are used with @app.input() and @app.output() decorators to provide
request validation and OpenAPI spec generation.
"""

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import Literal

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


class TypesQuery(ExportBaseQuery):
    """Query parameters for /types."""

    file: str = Field(
        default="dummy-ensemble.yaml", description="Filename used as template context"
    )
    cloudmap: Optional[str] = Field(
        default=None,
        description="CloudMap project ID to merge types from, e.g. 'onecommons/cloudmap'",
    )


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

    auth_project: Optional[str] = Field(
        default=None,
        description="Project ID whose cache entries and cloned files will be removed",
    )


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
