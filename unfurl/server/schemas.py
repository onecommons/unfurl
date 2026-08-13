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

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from ..graphql import (
    ApplicationBlueprint as ApplicationBlueprintType,
    Deployment as DeploymentType,
    DeploymentEnvironment as DeploymentEnvironmentType,
    DeploymentPath as DeploymentPathType,
    DeploymentTemplate as DeploymentTemplateType,
    ResourceTemplate as ResourceTemplateType,
    ResourceType as ResourceTypeType,
)
from ..util import find_schema_errors


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


class ProjectAuthQuery(BaseModel):
    """Common auth query parameter shared by all endpoints."""

    model_config = ConfigDict(
        extra="allow"
    )  # pass unknown query params through to request.args

    auth_project: Optional[str] = Field(
        default=None, description="Project ID for authorization and cache key scoping"
    )


class ProjectQuery(ProjectAuthQuery):
    latest_commit: Optional[str] = Field(
        default=None, description="Commit hash used to validate the cache entry"
    )
    branch: Optional[str] = Field(default=None, description="Git branch name")
    queueid: Optional[int] = Field(
        default=None,
        description="Setting this enables asynchronous writes",
    )


class ExportBaseQuery(ProjectQuery):
    """Shared query parameters for /export and /types."""

    pretty: bool = Field(default=False, description="Pretty-print the JSON response")
    username: Optional[str] = Field(
        default=None,
        description="Git username (alternative to X-Git-Credentials header)",
    )
    visibility: Optional[Literal["public", "private"]] = Field(
        default=None, description="Repository visibility"
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
    include_all_deployments: Optional[Literal["true", "1", ""]] = Field(
        default="",
        description="Include all deployment exports embedded in the response",
    )
    stale: Optional[Literal["ok", "never"]] = Field(
        default=None,
        description="Return any cache hit without checking if it's out of date.",
    )


class TypesQuery(ExportBaseQuery):
    """Query parameters for /types."""

    file: Optional[str] = Field(
        default="", description="Filename used as template context"
    )
    cloudmap: Optional[str] = Field(
        default=None,
        description="CloudMap project ID to merge types from, e.g. 'onecommons/cloudmap'",
    )


CloudMapKind = Literal[
    "repositories", "artifacts", "services", "instantiations", "components", "types"
]


class CloudMapBaseQuery(ProjectQuery):
    """Query parameters shared by the cloudmap read endpoints.

    ``cloudmap_path`` mirrors the POST ``/cloudmap`` body field of the same name, so a
    cloudmap kept somewhere other than ``cloudmap.yaml`` can be read back through the
    same path it was written to.
    """

    cloudmap_path: Optional[str] = Field(
        default=None,
        description=(
            "Path of the cloudmap file inside the repo; defaults to "
            "``cloudmap.yaml``."
        ),
    )


class CloudMapQuery(CloudMapBaseQuery):
    """Query parameters for /graph endpoint."""

    url: Optional[str] = Field(
        default=None,
        description="Optional artifact or instantiation URL to filter the graph to",
    )


class CloudMapDocQuery(CloudMapBaseQuery):
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
    since_version: Optional[int] = Field(
        default=None,
        description=(
            "When set, return only records whose "
            "``unfurl.server.version`` is greater than this value. "
            "Requires the rust git-sync backend; ignored by the "
            "Python YAML fallback."
        ),
    )
    exclude: Optional[str] = Field(
        default=None,
        description=(
            "Comma-separated list of record primary-key ids "
            "(``unfurl.server.id`` values) to exclude from the "
            "response. Used by clients with a warm cache to avoid "
            "re-receiving records they already hold during a "
            "``follow`` walk. Requires the rust git-sync backend; "
            "ignored by the Python YAML fallback."
        ),
    )
    type: Optional[str] = Field(
        default=None,
        description=(
            "Fully-qualified type name; return only records whose "
            "``type`` declares this type or a type that (transitively) "
            "``extends`` it, per the ``types`` section of the CloudMap."
        ),
    )
    filter: Optional[str] = Field(
        default=None,
        description=(
            "Filter on the contents of each record: a JSON Pointer path "
            "(RFC 6901) with an optional operator and value.\n"
            "\n"
            # fenced as `text` so that rustdoc doesn't take the block for a
            # Rust doctest when this lands in the generated unfurl_types
            "```text\n"
            "/metadata/topics=library                       equals, or array-contains\n"
            '/metadata/topics=["documentation","library"]   exact array match\n'
            "/metadata/homepage_url^=https://unfurl.cloud/  prefix (strings only)\n"
            "/metadata/discovery                            the path exists\n"
            "```\n"
            "\n"
            "``=`` matches when the value at the path equals the value or "
            "is an array containing it; an array literal is an exact match "
            "instead -- same elements, same order -- and an object literal "
            "is rejected. ``^=`` needs a string at the path, or a string "
            "element of an array there, that starts with the value; a "
            "number never matches a prefix. A path with no operator at all "
            "matches when the path resolves, counting a ``null`` or an "
            "empty array or object as present.\n"
            "\n"
            "Values are read as JSON: ``true``, ``false``, ``null`` and "
            "numbers keep their type, an array has to be valid JSON "
            '(``["a","b"]``, not ``[a,b]``), and anything else is a '
            'string. Wrap a value in double quotes to force a string '
            '(``="42"``). Wildcards in the path aren\'t supported yet. '
            "Combines with ``kind``, ``key`` and ``type``: a record has to "
            "match all of them."
        ),
    )
    select: Optional[str] = Field(
        default=None,
        description=(
            "Comma-separated list of JSON Pointer paths (RFC 6901, "
            "e.g. ``/type,/metadata/title``); when set, each record in "
            "the response (both elements of the pair) is reduced to "
            "only the selected properties, keeping their nested "
            "structure. Paths without a leading ``/`` get one "
            "prepended. The special entry ``$key`` adds the record's "
            "key to the reduced record under ``\"$key\"``. Paths that "
            "don't resolve are omitted."
        ),
    )
    limit: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Return at most this many records, and change the response "
            "from the bare ``[document, follow]`` pair to an envelope:\n"
            "\n"
            # fenced as `text` so that rustdoc doesn't take the block for a
            # Rust doctest when this lands in the generated unfurl_types
            "```text\n"
            '{"records": [document, follow], "next_page_token": "..."}\n'
            "```\n"
            "\n"
            "``next_page_token`` is absent on the last page; pass it back as "
            "``page_token`` to get the next one. Records are ordered by "
            "section then key, so a walk is stable across writes. Cannot be "
            "combined with ``key`` (which selects a single record); "
            "``follow`` and ``exclude`` have no effect on a paged request, "
            "whose ``follow`` half is always empty. Combines with ``kind``, "
            "``type``, ``filter`` and ``select``, which all apply before the "
            "page is cut."
        ),
    )
    page_token: Optional[str] = Field(
        default=None,
        description=(
            "Opaque cursor from a previous paged response's "
            "``next_page_token``: resume after the record it names. Only "
            "meaningful together with ``limit``. A token stays valid when "
            "the record it names is deleted."
        ),
    )


# ---------------------------------------------------------------------------
# CloudMap document response — references docs/cloudmap-schema.json
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_cloudmap_schema() -> Dict[str, Any]:
    """Return the canonical cloudmap JSON schema as a dict."""
    here = os.path.dirname(os.path.abspath(__file__))
    schema_path = os.path.join(here, "..", "cloudmap", "cloudmap-schema.json")
    with open(schema_path, "r") as f:
        return json.load(f)


_CLOUDMAP_REQUEST_ENVELOPE_KEYS = frozenset([
    "latest_commit",
    "cloudmap_path",
    "commit",
    "username",
    "private_token",
    "password",
    "commit_msg",
    "atomic",
])


class CloudMapDocument(BaseModel):
    """Pydantic stub for a CloudMap document, used as the **response
    element** for GET ``/cloudmap`` and as the **request body** for
    POST ``/cloudmap``.

    For POST, deletes are signalled by an ``unfurl.server.deleted:
    true`` flag on the record (handled by the endpoint after schema
    validation passes). Record values must always be objects.
    """

    # The model has no declared fields — APIFlask emits an empty stub
    # in the OpenAPI spec, which is then replaced wholesale by
    # :func:`hoist_cloudmap_definitions` (registered as a
    # ``@app.spec_processor``) with the contents of
    # ``unfurl/cloudmap/cloudmap-schema.json``. Doing the substitution at spec-build
    # time avoids Pydantic v2's internal ``$defs`` ref-counting
    # machinery.

    # Runtime validation is wired through ``__validate_cloudmap_schema``
    # below: every request body that arrives via ``@app.input()`` is
    # checked against the canonical ``unfurl/cloudmap/cloudmap-schema.json`` (after
    # stripping known envelope keys like ``latest_commit`` /
    # ``cloudmap_path``). Violations surface as a Pydantic
    # ``ValidationError``, which APIFlask returns as a 422.

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def __validate_cloudmap_schema(self):
        # Build the dict to validate by stripping envelope keys (they
        # belong to the request, not the cloudmap document).
        payload: Dict[str, Any] = {}
        # `model_dump` includes extras when extra="allow".
        for k, v in self.model_dump().items():
            if k in _CLOUDMAP_REQUEST_ENVELOPE_KEYS:
                continue
            payload[k] = v
        # An empty payload (envelope only) is trivially valid.
        if not payload:
            return self
        # apiVersion + kind are required by the schema. POST callers
        # commonly omit them — supply defaults so they don't have to.
        payload.setdefault("apiVersion", "unfurl/v1.0.0")
        payload.setdefault("kind", "CloudMap")
        schema = _load_cloudmap_schema()
        err = find_schema_errors(payload, schema)
        if err is not None:
            message, _details = err
            raise ValueError(f"cloudmap schema violation: {message}")
        return self


class PostCloudmapRequest(BaseModel):
    """Request body for ``POST /cloudmap``.

    A CloudMap document portion plus request-only envelope/control fields
    (``atomic`` / ``latest_commit`` / ``cloudmap_path`` / ``username``
    / ``private_token`` / ``commit_msg``).

    Declared as a flat object — the cloudmap section maps and the
    envelope keys live side-by-side at the top level. The endpoint
    splits them apart by name; the JSON-Schema validation only runs on
    the cloudmap-document subset.
    """

    # --- cloudmap document portion ---
    # Declared as `Dict[str, Any]` so the OpenAPI schema is permissive;
    # the canonical per-record validation runs in the model_validator
    # below against `cloudmap-schema.json`.
    apiVersion: Optional[str] = Field(default=None)
    kind: Optional[str] = Field(default=None)
    metadata: Optional[Dict[str, Any]] = Field(default=None)
    repositories: Optional[Dict[str, Any]] = Field(default=None)
    artifacts: Optional[Dict[str, Any]] = Field(default=None)
    components: Optional[Dict[str, Any]] = Field(default=None)
    services: Optional[Dict[str, Any]] = Field(default=None)
    instantiations: Optional[Dict[str, Any]] = Field(default=None)
    types: Optional[Dict[str, Any]] = Field(default=None)

    # --- envelope / control ---
    atomic: Optional[bool] = Field(
        default=None,
        description=(
            "When ``true`` (default), the batch is all-or-nothing: any "
            "per-record OCC failure rolls everything back. When "
            "``false``, per-record failures are skipped and the rest "
            "of the batch commits; the 409 body lists ``applied`` and "
            "``failed`` arrays. Honoured by the rust local handler "
            "only — the Python YAML fallback is implicitly atomic."
        ),
    )
    latest_commit: Optional[str] = Field(
        default=None,
        description="Last commit oid the client observed. Forwarded to git-level OCC checks.",
    )
    cloudmap_path: Optional[str] = Field(
        default=None,
        description="Path of the cloudmap file inside the repo.",
    )
    commit: Optional[bool] = Field(
        default=None,
        description=(
            "Whether to commit the write to git. "
            "If Commit = true is sent with a body that carries no records at all the "
            "handler then commits whatever is already pending."
        ),
    )
    username: Optional[str] = Field(
        default=None,
        description="Git credential username; can also be sent via the ``X-Git-Credentials`` header.",
    )
    private_token: Optional[str] = Field(
        default=None,
        description="Git credential token; can also be sent via the ``X-Git-Credentials`` header.",
    )
    commit_msg: Optional[str] = Field(
        default=None,
        description="Commit message for the local commit; falls back to a generated default.",
    )

    # `extra="allow"` is required because per-record payloads inside
    # the section maps may carry the OCC marker keys
    # (``unfurl.server.{commit,version,id,deleted}``); those land in
    # the section dicts (typed as `Dict[str, Any]`) so the
    # `extra="allow"` here is for forward-compat with new envelope
    # keys, not for the per-record markers.
    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def __validate_cloudmap_schema(self):
        # Build the cloudmap-document subset (excluding envelope keys)
        # and run it through the canonical JSON-Schema validator.
        # Skip when no document fields are present (envelope-only POSTs
        # are accepted as no-ops).
        payload: Dict[str, Any] = {}
        for k, v in self.model_dump(exclude_none=True).items():
            if k in _CLOUDMAP_REQUEST_ENVELOPE_KEYS:
                continue
            if k == "atomic":
                continue
            payload[k] = v
        if not payload:
            return self
        payload.setdefault("apiVersion", "unfurl/v1.0.0")
        payload.setdefault("kind", "CloudMap")
        schema = _load_cloudmap_schema()
        err = find_schema_errors(payload, schema)
        if err is not None:
            message, _details = err
            raise ValueError(f"cloudmap schema violation: {message}")
        return self


class CloudMapResult(BaseModel):
    """Placeholder for the ``GET /cloudmap`` response object.

    APIFlask emits an empty stub which :func:`hoist_cloudmap_definitions`
    replaces with an object schema referencing :class:`CloudMapDocument`.
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
    has_result = "CloudMapResult" in schemas
    has_post = "PostCloudmapRequest" in schemas
    if not (has_doc or has_result or has_post):
        return spec
    canonical = _load_cloudmap_schema()
    defs = canonical.get("definitions", {})
    for name, definition in defs.items():
        schemas["cloudmap_" + name] = _rewrite_refs_to_components(definition)
    canonical_props = _rewrite_refs_to_components(canonical.get("properties", {}))
    # ``CloudMapResult`` $refs ``CloudMapDocument`` so the latter has to
    # exist whenever the result does, even if no request body references
    # it directly (APIFlask only emits stubs for types listed on
    # @app.input / @app.output).
    if has_doc or has_result:
        schemas["CloudMapDocument"] = {
            "title": canonical.get("title", "CloudMap"),
            "type": canonical.get("type", "object"),
            "properties": canonical_props,
            "required": canonical.get("required", []),
        }
    if has_result:
        schemas["CloudMapResult"] = {
            "title": "CloudMap query result",
            "description": (
                "The queried (and optionally filtered) CloudMap document "
                "under ``result``. ``followed`` and ``next_page_token`` "
                "appear only when the request asked for what they carry, "
                "so their absence is meaningful rather than empty."
            ),
            "type": "object",
            "properties": {
                "result": {"$ref": "#/components/schemas/CloudMapDocument"},
                "followed": {
                    "allOf": [{"$ref": "#/components/schemas/CloudMapDocument"}],
                    "description": (
                        "Records discovered by walking the graph from "
                        "``key``. Present only when the request asked to "
                        "``follow`` from a ``key``."
                    ),
                },
                "next_page_token": {
                    "type": "string",
                    "description": (
                        "Cursor to pass as ``page_token`` for the next "
                        "page. Present only on a ``limit`` request that "
                        "has one -- its absence ends the walk."
                    ),
                },
            },
            "required": ["result"],
        }
    if has_post:
        # Replace the loose `Dict[str, Any]` shape Pydantic emits for
        # the cloudmap section fields with the typed
        # ``additionalProperties: {$ref: cloudmap_<section>}`` shape
        # the canonical schema declares. This restores per-record
        # serde-level type checking on the rust path (without it,
        # malformed records like ``{"protocols": "not-an-array"}``
        # would slip through to the handler instead of being rejected
        # at the JSON-extractor layer with 422).
        post_props = schemas["PostCloudmapRequest"].setdefault("properties", {})
        for section in (
            "repositories",
            "artifacts",
            "components",
            "services",
            "instantiations",
            "types",
        ):
            if section in canonical_props and section in post_props:
                post_props[section] = canonical_props[section]
    return spec


class PopulateCacheQuery(ProjectQuery):
    """Query parameters for /populate_cache."""

    path: str = Field(description="File path relative to the project root")
    removed: Optional[str] = Field(
        default=None,
        description="If truthy (not '0' or 'false'), delete the cache entry instead of populating it",
    )
    visibility: Optional[Literal["public", "private"]] = Field(
        default=None,
        description="Repository visibility; private repositories are not cloned automatically",
    )


class EmptyCacheQuery(ProjectAuthQuery):
    """Query parameters for /empty_cache."""

    auth_project: str = Field(  # type: ignore[assignment]  # overrides Optional[str] in parent
        description="Must equal the UNFURL_SERVER_ADMIN_PROJECT environment variable"
    )
    cache_prefix: Optional[str] = Field(
        default=None,
        description="Cache key prefix to clear; defaults to the server-configured prefix",
    )


class ClearProjectQuery(ProjectAuthQuery):
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
    queueid: Optional[int] = Field(
        default=None,
        description="Setting this enables asynchronous writes",
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
        description="Ordered list of original requests, each with 'endpoint' key and the original body fields"
    )
    queueid: Optional[int] = Field(
        default=None,
        description="Internal version counter, external clients should omit this field",
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


class AppliedRecord(BaseModel):
    """One record successfully applied during a CloudMap batch write.

    Returned in :attr:`PatchResponse.applied`.
    """

    section: str = Field(description="CloudMap section, e.g. ``artifacts``.")
    key: str = Field(description="Record key within the section.")
    version: int = Field(
        description="``unfurl.server.version`` stamped on the row by this write."
    )


# TODO: wire up a typed ConflictBody for the 409 response shape and use
# this model inside it. Today the rust handler at
# `rust/server/src/cloudmap.rs::From<WriteError> for ApiError` emits the
# 409 body as a freeform `serde_json::Value` and the proxy parses it as
# a raw dict — neither side goes through pydantic, so there's nowhere to
# attach this model. Defining ConflictBody requires:
#   * a pydantic model with `{section, key, actual, applied, failed}`
#     registered as the 409 response on POST /cloudmap,
#   * the rust handler emitting through the regenerated typed
#     `unfurl_types::ConflictBody` instead of `json!(...)`,
#   * the proxy's `_post` deserializing into the typed shape.
# Until that's done this model is the *accurate description* of the
# wire format produced by the rust handler — kept here as documentation.
#
# class FailedRecord(BaseModel):
#     """One record that did *not* apply during a CloudMap batch write.
#
#     Returned in the 409 conflict body's ``failed`` array (non-atomic
#     mode only). The atomic-mode 409 body uses the singleton ``section``
#     / ``key`` / ``actual`` fields and leaves ``failed`` empty.
#     """
#
#     section: str = Field(description="CloudMap section, e.g. ``artifacts``.")
#     key: str = Field(description="Record key within the section.")
#     actual: Optional[str] = Field(
#         default=None,
#         description="The row's current ``commit_id`` at the time of the conflict.",
#     )
#     error: Optional[str] = Field(
#         default=None,
#         description="Short error label, e.g. ``conflict`` or ``not_found``.",
#     )


class PatchResponse(BaseModel):
    """Response from write endpoints.

    Either ``commit`` (a git commit hash) or ``queueid`` (a monotonic
    version counter) used as a optimistic-concurrency
    token the client should echo back on its next write.
    """

    commit: Optional[str] = Field(
        default=None,
        description=(
            "The repository's commit hash after the request was handled: the "
            "new commit when one was made, otherwise the unchanged HEAD (which "
            "the client can echo back as ``latest_commit``). Null only when the "
            "repository has no commits at all."
        ),
    )
    queueid: Optional[int] = Field(
        default=None,
        description=("Monotonic version assigned to this uncommitted write operation."),
    )
    applied: List[AppliedRecord] = Field(
        default_factory=list,
        description=(
            "Per-record results for batch CloudMap writes. Empty for "
            "single-record endpoints. In non-atomic mode, contains the "
            "records that committed even though the batch reported a "
            "conflict."
        ),
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
    latest_commit: Optional[str] = Field(
        default=None,
        description="Latest commit hash observed by the export; clients can use this for cache validation",
    )
    queueid: Optional[int] = Field(
        default=None,
        description=("Monotonic version assigned to this uncommitted write operation."),
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
