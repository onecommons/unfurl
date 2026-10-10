# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""
HTTP endpoints for unfurl server.
"""

import gc
import json
import os
import re
from urllib.parse import urlparse
from base64 import b64decode
from typing import Any, Dict, List, Optional, Tuple, Union, cast

from flask import Response, current_app, g, jsonify, make_response, request
from flask.typing import ResponseReturnValue

from toscaparser.elements.entity_type import Namespace
from .. import init
from ..graphql import (
    GraphqlObject,
    GraphqlObjectsByName,
    ImportDef,
    get_local_type,
)
from ..localenv import LocalEnv
from ..logs import getLogger
from ..manifest import relabel_dict
from ..projectpaths import Folders
from ..repo import (
    GitRepo,
    Repo,
    add_user_to_url,
    normalize_git_url_hard,
    sanitize_url,
)
from ..util import assert_not_none, unique_name
from ..yamlmanifest import YamlManifest

from .schemas import (
    BatchPatchBody,
    EventsQuery,
    PATCH_RESPONSES,
    PatchEnsembleBody,
    PatchEnvironmentBody,
    PatchResponse,
    ProjectAuthQuery,
    QueueStateQuery,
    QueueStateResult,
    QueuedWriteEvent,
)

# Imported from .serve at the bottom of this file; serve.py imports
# this module last so all of these names are bound by the time we
# resolve them.
from .serve import (
    CacheEntry,
    UNFURL_SERVER_DEBUG_PATCH,
    _get_filepath,
    _get_project_repo,
    localenv_from_cache_checked,
    app,
    create_error_response,
    ensure_local_config,
    get_cache,
    get_project_id_or_abort,
    queue_key_ttl,
    set_branch_head,
    refresh_current_localenv,
)

logger = getLogger("unfurl.server")


_EVENT_STREAM_RESPONSE: Dict[
    Union[int, str], Dict[str, Union[str, Dict[str, Dict[str, Any]]]]
] = {
    200: {
        # Plain prose, no backticks or braces: this becomes a doc comment
        # in the generated unfurl_types.rs, and rustdoc compiles anything
        # it reads as a code block.
        "description": (
            "Stream of data frames, one per watched write as it settles, "
            "ending with a terminal frame whose status is done. "
            "Declared without a schema deliberately: OpenAPI 3.0 has no "
            "way to say a stream of these, since a response schema "
            "describes the whole body, and oas3-gen generates an "
            "EventStream wrapper for a typed text/event-stream that does "
            "not compile. The frame shape is QueuedWriteEvent in "
            "schemas.py. OpenAPI 3.2 added event streaming and would let "
            "this be declared properly."
        ),
        "content": {"text/event-stream": {}},
    }
}


@app.get("/events")
@app.doc(
    summary="Stream a queued write's outcome",
    description=(
        "One ``data:`` frame per watched write as it settles, ending with "
        "``{\"status\": \"done\"}``. The client must close the stream on "
        "that frame: `EventSource` reopens one that merely ends.\n\n"
        "The body is a stream of ``QueuedWriteEvent``, which OpenAPI 3.0 "
        "cannot express -- the schema below describes one frame, not the "
        "body.\n\n"
        "Served by the rust proxy when a write queue is configured. This "
        "backend has no queue, so nothing is ever pending and it sends "
        "``done`` at once."
    ),
    tags=["Project"],
    responses=_EVENT_STREAM_RESPONSE,
)
@app.input(EventsQuery, location="query", arg_name="query")
def get_events(query: EventsQuery) -> ResponseReturnValue:
    # Terminal frame only: with no queue there is nothing to wait on, and
    # a client that stayed open would wait out its own timeout instead.
    return Response(
        f"data: {json.dumps({'status': 'done'})}\n\n",
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/queue_state")
@app.doc(
    summary="Write queue state for a branch",
    description=(
        "Report each base commit with writes queued against it, so a "
        "client can tell whether the queueid it holds is still current "
        "without paying for an export.\n\n"
        "Served by the rust proxy when a write queue is configured. This "
        "backend has no queue, so it answers with no commits -- which is "
        "the truthful answer here, not a stub: without a queue no write "
        "is ever pending."
    ),
    tags=["Project"],
)
@app.input(QueueStateQuery, location="query", arg_name="query")
@app.output(QueueStateResult, description="Queued writes by base commit")
def get_queue_state(query: QueueStateQuery) -> ResponseReturnValue:
    return {"branch": query.branch, "commits": {}}


# ---------------------------------------------------------------------------
# Patch endpoints
# ---------------------------------------------------------------------------


def _get_author(request) -> Optional[str]:
    """The git author for commits made while handling ``request``.

    Clients that authenticate the end user themselves can identify them with the
    ``X-Unfurl-User`` header (``"Name <email>"``, a bare name, or a bare email address)
    so the commit is attributed to them instead of to the server's git identity.
    In a batch, it's the author of the request being applied, which the Rust
    server recorded from that request's header when it queued it.
    """
    if "batch_author" in g:
        return g.batch_author
    return request.headers.get("X-Unfurl-User")


def _get_body(request) -> dict:
    body = request.json
    if request.headers.get("X-Git-Credentials"):
        body["username"], body["private_token"] = (
            b64decode(request.headers["X-Git-Credentials"]).decode().split(":", 1)
        )
    return body


def _branch_from_body(body: dict) -> Tuple[Optional[Response], str]:
    """The branch a write applies to, or an error response if it named none.

    A write has to say where it goes. Defaulting to `main` meant a request that
    left `branch` out -- or sent it empty -- committed to whatever `main` is,
    which needn't be the branch the client read: `GET /export` resolves an
    unnamed branch from the remote's advertised default and reports it back, so
    the two can legitimately differ. Rejecting is safe for real clients: the GUI
    already refuses to build a write without a branch, and the Rust proxy
    rejects one before queueing it.
    """
    branch = (body.get("branch") or "").strip()
    if not branch:
        return create_error_response(
            "BAD_REQUEST", "'branch' is required and cannot be empty"
        ), ""
    return None, branch


def _is_error_response(result: ResponseReturnValue) -> bool:
    """Whether a `_patch_*` helper reported failure rather than a patch result.

    Both outcomes are Flask responses, so the type alone doesn't say: a tuple is
    always an error, and a `Response` is one when its status says so. Testing
    only for a tuple let `create_provider` and `batch_patch` continue after a
    failed patch and report success to the client.
    """
    if isinstance(result, tuple):
        return True
    return isinstance(result, Response) and result.status_code >= 400


@app.post("/delete_deployment")
@app.doc(
    summary="Delete a deployment",
    tags=["Project"],
    responses=PATCH_RESPONSES,
)
@app.input(ProjectAuthQuery, location="query", arg_name="query")
@app.input(PatchEnvironmentBody, location="json", arg_name="body_schema")
@app.output(PatchResponse)
def delete_deployment(
    query: ProjectAuthQuery, body_schema: PatchEnvironmentBody
) -> ResponseReturnValue:
    body = _get_body(request)
    return _patch_environment(
        body, get_project_id_or_abort(request), delete_deployment=True
    )


@app.post("/update_environment")
@app.doc(
    summary="Update a deployment environment",
    tags=["Project"],
    responses=PATCH_RESPONSES,
)
@app.input(ProjectAuthQuery, location="query", arg_name="query")
@app.input(PatchEnvironmentBody, location="json", arg_name="body_schema")
@app.output(PatchResponse)
def update_environment(
    query: ProjectAuthQuery, body_schema: PatchEnvironmentBody
) -> ResponseReturnValue:
    body = _get_body(request)
    return _patch_environment(body, get_project_id_or_abort(request))


@app.post("/delete_environment")
@app.doc(
    summary="Delete a deployment environment",
    tags=["Project"],
    responses=PATCH_RESPONSES,
)
@app.input(ProjectAuthQuery, location="query", arg_name="query")
@app.input(PatchEnvironmentBody, location="json", arg_name="body_schema")
@app.output(PatchResponse)
def delete_environment(
    query: ProjectAuthQuery, body_schema: PatchEnvironmentBody
) -> ResponseReturnValue:
    body = _get_body(request)
    return _patch_environment(body, get_project_id_or_abort(request))


@app.post("/create_provider")
@app.doc(
    summary="Create a cloud provider and its associated ensemble",
    tags=["Project"],
    responses=PATCH_RESPONSES,
)
@app.input(ProjectAuthQuery, location="query", arg_name="query")
@app.input(PatchEnsembleBody, location="json", arg_name="body_schema")
@app.output(PatchResponse)
def create_provider(
    query: ProjectAuthQuery, body_schema: PatchEnsembleBody
) -> ResponseReturnValue:
    body = _get_body(request)
    project_id = get_project_id_or_abort(request)
    latest_commit = body.get("latest_commit") or ""
    branch_err, branch = _branch_from_body(body)
    if branch_err:
        return branch_err
    err, readonly_localEnv = localenv_from_cache_checked(
        assert_not_none(get_cache()),
        project_id,
        branch,
        "",
        latest_commit,
        body,
    )
    if err:
        return err
    # Reuse the readonly localenv across both helpers by passing it as
    # `batched=`.  That also suppresses each helper's per-commit push;
    # we issue a single push at the end so the client's resulting
    # commit is visible upstream.
    env_result = _patch_environment(body, project_id, readonly_localEnv)
    if _is_error_response(env_result):
        return env_result
    ensemble_result = _patch_ensemble(body, True, project_id, readonly_localEnv)
    if _is_error_response(ensemble_result):
        return ensemble_result
    assert readonly_localEnv and readonly_localEnv.project
    repo = readonly_localEnv.project.project_repoview.gitrepo
    assert repo
    if not app.config.get("UNFURL_GUI_MODE"):
        username = cast(str, body.get("username"))
        password = cast(str, body.get("private_token", body.get("password")))
        push_err = _push_changes(
            repo, username, password, latest_commit, project_id, branch
        )
        if push_err:
            return push_err
    return ensemble_result


def _update_imports(current: List[ImportDef], new: List[ImportDef]) -> List[ImportDef]:
    current.extend(new)
    return current


def _apply_imports(
    template: dict,
    patch: List[ImportDef],
    repo_url: str,
    root_file_path: str,
    skip_prefixes: List[str],
    repositories: Optional[Dict[str, Any]] = None,
) -> None:
    # use _sourceinfo to patch imports and repositories
    # imports:
    #   - file, repository, prefix
    # repositories:
    #     repo_name: url
    imports: List[dict] = []
    if not repositories:
        repositories = template.get("repositories") or {}
    for source_info in patch:
        patch_repositories = template.setdefault("repositories", {})
        repository = source_info.get("repository")
        root = source_info.get("url")
        prefix = source_info.get("prefix")
        file = source_info["file"]
        _import = dict(file=file)
        if prefix:
            _import["namespace_prefix"] = prefix
        norm_root = normalize_git_url_hard(root) if root else ""
        if repository:
            if repository != "unfurl" and root:
                for name, tpl in repositories.items():
                    # sometime the client sends reposities with the package id as its name and no "url" key
                    url = tpl.get("url") or name
                    if url and normalize_git_url_hard(url) == norm_root:
                        repository = name
                        break
                else:
                    # don't use an existing name because the urls won't match
                    repository = unique_name(repository, repositories)
                    logger.debug("adding repository '%s': %s", repository, root)
                    patch_repositories[repository] = repositories[repository] = dict(
                        url=root
                    )
            if repository:
                _import["repository"] = repository
        else:
            if root and norm_root != normalize_git_url_hard(repo_url):
                # sometime the client sends reposities with the package id as its name and no "url" key
                for name, tpl in repositories.items():
                    url = tpl.get("url") or name
                    if url and normalize_git_url_hard(url) == norm_root:
                        repository = name
                        break
                else:
                    # no repository declared
                    repository = Repo.get_path_for_git_repo(root, name_only=True)
                    repository = unique_name(repository, repositories)
                    logger.debug(
                        "adding generated repository '%s': %s", repository, root
                    )
                    patch_repositories[repository] = repositories[repository] = dict(
                        url=root
                    )
                if repository:
                    _import["repository"] = repository
            else:
                if file == root_file_path:
                    # type defined in the root template, no need to import
                    continue
        imports.append(_import)
    _add_imports(imports, template, repositories, skip_prefixes)


def _add_imports(
    imports: List[dict], template: dict, repositories: dict, skip_prefixes: List[str]
):
    for i in imports:
        logger.trace("checking for import %s", i)
        for existing in template.setdefault("imports", []):
            # add imports if missing
            if i["file"] == existing["file"]:
                if i.get("namespace_prefix") in skip_prefixes:
                    continue  # don't match environment imports
                if i.get("namespace_prefix") == existing.get("namespace_prefix"):
                    existing_repository = existing.get("repository")
                    if "repository" in i:
                        if "repository" in existing:
                            if i["repository"] == "unfurl":
                                break
                            if (
                                repositories[i["repository"]]["url"]
                                == repositories[existing["repository"]]["url"]
                            ):
                                break
                    elif not existing_repository:
                        break  # match
        else:
            logger.debug("added import %s", i)
            template["imports"].append(i)


def _patch_deployment_blueprint(
    patch: dict, manifest: "YamlManifest", deleted: bool
) -> List[ImportDef]:
    deployment_blueprint = patch["name"]
    doc = manifest.manifest.config
    assert doc
    deployment_blueprints = doc.setdefault("spec", {}).setdefault(
        "deployment_blueprints", {}
    )
    imports: List[ImportDef] = []
    current = deployment_blueprints.setdefault(deployment_blueprint, {})
    if deleted:
        del deployment_blueprints[deployment_blueprint]
    else:
        keys = [
            "title",
            "cloud",
            "description",
            "primary",
            "source",
            "branch",
        ]
        for key, prop in patch.items():
            if key in keys:
                current[key] = prop
            elif key == "ResourceTemplate":
                # assume the patch has the complete set and replace the current set
                old_node_templates = current.get("resource_templates", {})
                new_node_templates = {}
                assert manifest.tosca and manifest.tosca.topology
                namespace = manifest.tosca.topology.topology_template.custom_defs
                for name, val in prop.items():
                    tpl = old_node_templates.get(name, {})
                    _update_imports(imports, _patch_node_template(val, tpl, namespace))
                    new_node_templates[name] = tpl
                current["resource_templates"] = new_node_templates
    return imports


def _make_requirement(dependency) -> dict:
    req = dict(node=dependency.get("match"))
    if "constraint" in dependency and "visibility" in dependency["constraint"]:
        req["metadata"] = dict(visibility=dependency["constraint"]["visibility"])
    return req


def _patch_node_template(
    patch: dict, tpl: dict, namespace: Optional[Namespace], prefix=""
) -> List[ImportDef]:
    imports: List[ImportDef] = []
    title = None
    for key, value in patch.items():
        if key == "type":
            # type's value will be a global name
            src_import_def = cast(Optional[ImportDef], patch.get("_sourceinfo"))
            if src_import_def and prefix:
                src_import_def["prefix"] = prefix
            local, import_def = get_local_type(namespace, value, src_import_def)
            if import_def:
                imports.append(import_def)
            tpl[key] = local
        elif key in ["directives", "imported"]:
            tpl[key] = value
        elif key == "title":
            if value != patch["name"]:
                title = value
        elif key == "metadata":
            tpl.setdefault("metadata", {}).update(value)
        elif key == "properties":
            props = tpl.setdefault("properties", {})
            assert isinstance(props, dict), f"bad props {props} in {tpl}"
            assert isinstance(value, list), (
                f"bad patch value {value} for {key} in {patch}"
            )
            for prop in value:
                assert isinstance(prop, dict), (
                    f"bad {prop} in {value} for {key} in {patch}"
                )
                if prop["value"] == {"__deleted": True}:
                    props.pop(prop["name"], None)
                else:
                    props[prop["name"]] = prop["value"]
        elif key == "dependencies":
            requirements = [
                {dependency["name"]: _make_requirement(dependency)}
                for dependency in value
                if "match" in dependency
            ]
            if requirements or "requirements" in tpl:
                tpl["requirements"] = requirements
    if title:  # give "title" priority over "metadata/title"
        tpl.setdefault("metadata", {})["title"] = title
    return imports


# XXX
# @app.route("/delete_ensemble", methods=["POST"])
# def delete_ensemble():
#     body = request.json
#     deployment_path = body.get("deployment_path")
#     invalidate_cache(body, "environments")
#     update_deployment(deployment_path)
#     repo.delete_dir(deployment_path)
#     localConfig.config.save()
#     commit_msg = body.get("commit_msg", "Update environment")
#     _commit_and_push(repo, localConfig.config.path, commit_msg)
#     return "OK"


@app.post("/update_ensemble")
@app.doc(
    summary="Update an existing ensemble",
    tags=["Project"],
    responses=PATCH_RESPONSES,
)
@app.input(ProjectAuthQuery, location="query", arg_name="query")
@app.input(PatchEnsembleBody, location="json", arg_name="body_schema")
@app.output(PatchResponse)
def update_ensemble(
    query: ProjectAuthQuery, body_schema: PatchEnsembleBody
) -> ResponseReturnValue:
    body = _get_body(request)
    return _patch_ensemble(body, False, get_project_id_or_abort(request))


@app.post("/create_ensemble")
@app.doc(
    summary="Create a new ensemble",
    tags=["Project"],
    responses=PATCH_RESPONSES,
)
@app.input(ProjectAuthQuery, location="query", arg_name="query")
@app.input(PatchEnsembleBody, location="json", arg_name="body_schema")
@app.output(PatchResponse)
def create_ensemble(
    query: ProjectAuthQuery, body_schema: PatchEnsembleBody
) -> ResponseReturnValue:
    body = _get_body(request)
    return _patch_ensemble(body, True, get_project_id_or_abort(request))


def _update_queue_key(
    project_id: str, branch: str, latest_commit: str, new_commit: str, queueid: int
) -> None:
    """Update the Redis queue key after batch_patch commits.

    Sets ``{CACHE_KEY_PREFIX}queue:{project_id}:{branch}:{latest_commit}`` to
    ``"{new_commit},{queueid}"`` so subsequent ``inc_queueid`` calls
    (in the rust proxy) redirect clients to the new commit.

    A batch that commits nothing writes ``latest_commit`` as the new
    commit. Don't skip that write: the proxy reads the self-reference as
    "batch finished, HEAD unchanged" (INC_QUEUEID_SCRIPT restarts the
    queue, readers proceed at ``latest_commit``), whereas leaving the
    plain integer behind strands both at a queueid no one can advance.

    Writes via the raw redis client rather than ``cache.set`` so the
    value lands as a plain UTF-8 string. Flask-Caching would pickle it,
    which the rust proxy's ``check_export_queue`` / ``inc_queueid`` Lua
    can't decode as a string.
    """
    cache = get_cache()
    assert cache
    prefix = app.config.get("CACHE_KEY_PREFIX", "")
    # The branch is in the key because a batch is partitioned by
    # (latest_commit, branch): two branches off one commit each commit
    # their own, and sharing a key would have the second overwrite the
    # first. Must match `Config::queue_entry_key` in the rust proxy.
    queue_key = f"{prefix}queue:{project_id}:{branch}:{latest_commit}"
    value = f"{new_commit},{queueid}"
    backend = getattr(cache, "cache", None)
    redis_client = backend and getattr(backend, "_write_client", None)
    if redis_client is None:
        cache.set(queue_key, value)
        return
    # Expire it, or a project accumulates one key per commit ever written
    # against it.
    ttl = queue_key_ttl()
    try:
        if ttl > 0:
            redis_client.set(queue_key, value, ex=ttl)
        else:
            redis_client.set(queue_key, value)
        logger.debug("updated queue key %s = %s (ttl=%s)", queue_key, value, ttl)
    except Exception as exc:
        logger.error("failed to update queue key %s: %s", queue_key, exc)
    # The branch's head is the only key a client with nothing queued can
    # watch: the per-commit key above is addressable only by someone who
    # already knows the base commit a write was made against.
    set_branch_head(project_id, branch, new_commit)


@app.post("/batch_patch")
@app.doc(
    summary="Apply a batch of write requests",
    description=(
        "Used by the Rust proxy to forward a batch of write requests that "
        "share the same branch and latest_commit.  Each request in the "
        "``requests`` list is applied in order; a single push is performed "
        "at the end."
    ),
    tags=["Project"],
    responses=PATCH_RESPONSES,
)
@app.input(ProjectAuthQuery, location="query", arg_name="query")
@app.input(BatchPatchBody, location="json", arg_name="body_schema")
@app.output(PatchResponse)
def batch_patch(
    query: ProjectAuthQuery, body_schema: "BatchPatchBody"
) -> ResponseReturnValue:
    body = _get_body(request)
    project_id = get_project_id_or_abort(request)
    batch_requests = body.get("requests", [])
    latest_commit = body.get("latest_commit") or ""
    branch_err, branch = _branch_from_body(body)
    if branch_err:
        return _mark_rolled_back(branch_err)
    # Check every request before applying any: each carries the body it was
    # queued with, and a batch that failed part way through would leave the
    # earlier requests committed with no way for the client to learn which.
    for req in batch_requests:
        branch_err, _ = _branch_from_body(req)
        if branch_err:
            return _mark_rolled_back(branch_err)
    logger.info(
        "batch_patch: project=%s branch=%s requests=%d",
        project_id,
        branch,
        len(batch_requests),
    )
    err, readonly_localEnv = localenv_from_cache_checked(
        assert_not_none(get_cache()),
        project_id,
        branch,
        "",
        latest_commit,
        body,
    )
    if err:
        return _mark_rolled_back(err)
    assert readonly_localEnv and readonly_localEnv.project
    # Get the repo via the same parent-aware fresh LocalEnv that
    # `_patch_environment` uses internally, so projects with a separate
    # ensemble subrepo report the *parent* project's HEAD here (which is
    # where individual patches actually committed) rather than the
    # cached LocalEnv's ensemble-subrepo HEAD.
    #
    # Acquired before anything is applied because the rollback below
    # needs it, and `repo.revision` needs to be read before the first
    # patch commits over it.
    home_dir = app.config.get("UNFURL_CURRENT_WORKING_DIR") or current_app.config[
        "UNFURL_OPTIONS"
    ].get("home")
    parent_localEnv = LocalEnv(
        readonly_localEnv.project.projectRoot,
        home_dir,
        parent=readonly_localEnv,
        can_be_empty=True,
        overrides=dict(safe_mode=True),
    )
    assert parent_localEnv.project
    repo = parent_localEnv.project.project_repoview.gitrepo
    assert repo
    start_revision = repo.revision
    # Read before anything is applied: uncommitted work already here isn't
    # this batch's to discard. A patch that finds the repo dirty writes to
    # disk without committing (the `was_dirty` branch in
    # `_patch_environment`) and `/export` serves that state, so a rollback
    # would destroy what the user can already see.
    started_dirty = repo.is_dirty()

    try:
        result = _apply_batch_requests(
            body, batch_requests, project_id, readonly_localEnv, repo, latest_commit
        )
    except Exception as exc:
        _rollback_batch(repo, start_revision, started_dirty)
        logger.error("batch_patch failed", exc_info=True)
        return _mark_rolled_back(
            create_error_response("INTERNAL_ERROR", "Could not apply batch", exc),
            started_dirty,
        )
    if _is_error_response(result):
        _rollback_batch(repo, start_revision, started_dirty)
        return _mark_rolled_back(result, started_dirty)
    return result


def _rollback_batch(
    repo: GitRepo, start_revision: str, started_dirty: bool = False
) -> None:
    """Discard everything a failed batch committed locally.

    The requests in a batch commit as they are applied, so an error part
    way through leaves the earlier ones committed but unpushed. They sit
    in the persistent working copy under `repos/private/...` and the next
    batch that pushes successfully carries them along -- landing writes
    the client was told were discarded.

    `start_revision` is the observed HEAD rather than the request's
    `latest_commit`: the two differ for a project whose ensemble lives in
    a subrepo, and HEAD is the one the patches committed against.

    Nothing is rolled back in gui mode, nor when the working
    copy was already dirty on entry, where a reset would take uncommitted
    work this batch never made.
    """
    if not _rolls_back_a_failed_batch(started_dirty):
        logger.warning(
            "not rolling back batch, left at %s: %s",
            repo.revision,
            "gui mode"
            if app.config.get("UNFURL_GUI_MODE")
            else f"the working copy at {repo.working_dir} was already dirty",
        )
        return
    _discard_local_commits(repo, start_revision, "batch")


def _discard_local_commits(repo: GitRepo, start_revision: str, what: str) -> bool:
    """Reset `repo` working directory back to `start_revision`, both dirty tracked and untracked files."""
    if not start_revision:
        # Nothing to reset to, and `HEAD~1` would be a guess at how many
        # commits were made; unborn HEAD has no parent at all.
        logger.error("cannot roll back %s: no starting revision", what)
        return False
    if not repo.reset(f"--hard {start_revision}"):
        logger.error("failed to roll back %s to %s", what, start_revision)
        return False
    # `reset --hard` restores tracked files but leaves new ones behind, and
    # `_patch_ensemble` commits with add_all, so a later write would sweep in
    # whatever a half-applied `create_ensemble` wrote. `-x` because the
    # ignore rules cover state a patch writes (`local`, `jobs`, `tmp`) that
    # should go back with everything else. `-d` and not `-ff`: git refuses to
    # descend into an untracked directory that is its own repository, so
    # cloned dependencies under `tosca_repositories` and an ensemble subrepo
    # survive either way.
    status, _out, err = repo.run_cmd(["clean", "-fdx"])
    if status:
        logger.error(
            "rolled back %s to %s but could not remove new files: %s",
            what,
            start_revision,
            err,
        )
        return False
    logger.info("rolled back %s to %s", what, start_revision)
    return True


def _rolls_back_a_failed_batch(started_dirty: bool = False) -> bool:
    """Whether a failed batch is undone. False in gui mode or on a dirty repo.

    The one predicate behind both the rollback and the `rolled_back` flag
    that reports it, so the two can't disagree.
    """
    return not app.config.get("UNFURL_GUI_MODE") and not started_dirty


def _mark_rolled_back(
    result: ResponseReturnValue, started_dirty: bool = False
) -> ResponseReturnValue:
    """Record on an error response whether the batch left anything applied.

    `/batch_patch` applies every request or none, so the queue worker can
    retry a transient failure without compounding it. It reads this rather
    than inferring retry-safety from the status code.

    Gui mode keeps what a failed batch committed, and so does a repo that
    was already dirty, so this is false in both -- including for an error
    raised before anything was applied, where nothing needed undoing. That direction is the safe one to be wrong in:
    a worker that believes a batch was left half-applied declines to retry
    it, where the reverse would replay writes that already landed. Gui mode
    has no queue worker to read it at all.
    """
    if isinstance(result, Response) and result.is_json:
        body = result.get_json(silent=True)
        if isinstance(body, dict):
            body["rolled_back"] = _rolls_back_a_failed_batch(started_dirty)
            result.set_data(json.dumps(body))
    return result


def _annotate_failed_request(
    result: ResponseReturnValue,
    endpoint: str,
    index: int,
    total: int,
    applied: List[Dict[str, object]],
) -> ResponseReturnValue:
    """Name the request a batch failed on, which ran before it, and how
    many never ran.

    The error `_apply_batch_requests` returns is the failing request's own
    and says nothing about its position, so a client saw one message with
    no sign the batch held other writes.

    `applied` is what makes the un-rolled-back case reportable. Where
    `_rolls_back_a_failed_batch` is false -- gui mode, or a repo already
    dirty on entry -- the requests before the failure stay committed, so
    the batch was not "discarded" and `rolled_back` alone says only that
    something may have survived, not which.
    """
    if isinstance(result, Response) and result.is_json:
        body = result.get_json(silent=True)
        if isinstance(body, dict):
            body["failed_request"] = {
                "endpoint": endpoint,
                "index": index,
                "count": total,
                "skipped": total - index - 1,
            }
            body["applied"] = applied
            result.set_data(json.dumps(body))
    return result


def _apply_one_batch_request(
    req_body: dict,
    project_id: str,
    readonly_localEnv: LocalEnv,
    endpoint: str,
    create: bool,
) -> ResponseReturnValue:
    """Apply one request of a batch, or return its error response.

    `create_provider` patches an environment and an ensemble, so both
    branches can run for one request.
    """
    result: ResponseReturnValue = {}
    if endpoint in (
        "create_provider",
        "update_environment",
        "delete_environment",
        "delete_deployment",
    ):
        result = _patch_environment(req_body, project_id, batched=readonly_localEnv)
        if _is_error_response(result):
            return result
    if create or endpoint == "update_ensemble":
        result = _patch_ensemble(req_body, create, project_id, batched=readonly_localEnv)
    return result


def _apply_batch_requests(
    body: dict,
    batch_requests: list,
    project_id: str,
    readonly_localEnv: LocalEnv,
    repo: GitRepo,
    latest_commit: str,
) -> ResponseReturnValue:
    """Apply every request in the batch, then push once.

    Any error return (or exception) leaves the caller to roll back, so
    this doesn't have to unwind what it already committed.
    """
    last_body = body  # track last body for credentials
    latest_commits = set()
    # Requests that committed before any failure. Appended after both
    # patch calls below, because `create_provider` runs an environment
    # patch *and* an ensemble one and is one request either way.
    applied: List[Dict[str, object]] = []
    for index, req in enumerate(batch_requests):
        endpoint = req.get("endpoint", "")
        latest_commits.add(req.get("latest_commit", ""))
        # The request body is the req dict itself (endpoint + original body fields).
        # Credentials reach us once, on the outer request's X-Git-Credentials
        # header; the queued sub-requests were serialized before that header
        # existed and never pass through _get_body, so carry them in or every
        # batched patch fails its own credential check (and the push after it).
        req_body = req
        for cred in ("username", "private_token", "password"):
            if cred in body and cred not in req_body:
                req_body[cred] = body[cred]
        last_body = req_body
        # A batch holds several users' writes, and its own header names only
        # the first; requests queued before the Rust server recorded each
        # one's author have none, and keep that header.
        if "author" in req_body:
            g.batch_author = req_body.pop("author")
        else:
            g.pop("batch_author", None)
        create = endpoint in ("create_ensemble", "create_provider")
        # Caught per request so an exception says which one, like a returned
        # error does. Without this the two carried disjoint halves of the
        # story: an annotated error and no traceback, or a traceback and no
        # idea which request raised or what had already committed.
        # Re-raised as an error response, which the caller rolls back
        # exactly as it does the returned kind.
        try:
            result = _apply_one_batch_request(
                req_body, project_id, readonly_localEnv, endpoint, create
            )
        except Exception as exc:
            logger.error("batch request %s raised", endpoint, exc_info=True)
            return _annotate_failed_request(
                create_error_response("INTERNAL_ERROR", "Could not apply batch", exc),
                endpoint,
                index,
                len(batch_requests),
                applied,
            )
        if _is_error_response(result):
            return _annotate_failed_request(
                result, endpoint, index, len(batch_requests), applied
            )
        applied.append({"endpoint": endpoint, "index": index})
    username = last_body.get("username")
    password = last_body.get("private_token", last_body.get("password"))
    if not app.config.get("UNFURL_GUI_MODE"):
        # the caller's rollback covers a failed push too
        err = _push_changes(
            repo,
            username,
            password,
            latest_commit,
            project_id,
            body["branch"],
            rollback=False,
        )
        if err:
            return err
    # Update the Redis queue key so subsequent inc_queueid calls
    # redirect clients to the new commit.
    batch_queueid = body.get("queueid")
    if batch_queueid is not None and repo:
        new_commit = repo.revision
        for lc in latest_commits:
            if lc:  # skip empty latest_commit values
                _update_queue_key(
                    project_id, body["branch"], lc, new_commit, batch_queueid
                )
    return _patch_response(repo)


def update_deployment(project, key, patch_inner, save, deleted=False):
    localConfig = project.localConfig
    deployment_path = os.path.join(project.projectRoot, key, "ensemble.yaml")
    tpl = project.find_ensemble_by_path(deployment_path)
    if deleted:
        if tpl:
            localConfig.ensembles.remove(tpl)
    else:
        if not tpl:
            tpl = dict(file=deployment_path)
            localConfig.ensembles.append(tpl)
        for key in patch_inner:
            if key not in ["name", "__deleted", "__typename"]:
                tpl[key] = patch_inner[key]
    localConfig.config.config["ensembles"] = localConfig.ensembles
    if save:
        localConfig.config.save()


def _patch_response(repo: Optional[GitRepo]) -> Response:
    return jsonify(dict(commit=repo and repo.revision or None))


def _apply_environment_patch(patch: list, local_env: LocalEnv) -> Optional[Response]:
    project = local_env.project
    assert project
    localConfig = project.localConfig
    for patch_inner in patch:
        assert isinstance(patch_inner, dict)
        typename = patch_inner.get("__typename")
        deleted = patch_inner.get("__deleted") or False
        assert isinstance(deleted, bool)
        if typename == "DeploymentEnvironment":
            environments = localConfig.config.config.setdefault("environments", {})
            if environments is None:
                environments = localConfig.config.config["environments"] = {}
            name = patch_inner["name"]
            if deleted:
                if name in environments:
                    del environments[name]
            else:
                imports: List[ImportDef] = []
                if name not in environments:
                    # can't commit to reserved folder names
                    invalid = Folders.has_excluded_path(name)
                    if invalid:
                        return create_error_response(
                            "BAD_REQUEST",
                            f'Cannot create environment with reserved name: "{invalid}"',
                        )
                environment = environments.setdefault(name, {})
                prefix = re.sub(r"\W", "_", name)
                for key in patch_inner:
                    if key == "instances" or key == "connections":
                        target = environment.get(key) or {}
                        new_target = {}
                        for node_name, node_patch in patch_inner[key].items():
                            tpl = target.setdefault(node_name, {})
                            if not isinstance(tpl, dict):
                                # connections keys can be a string or null
                                tpl = {}
                            _update_imports(
                                imports,
                                _patch_node_template(node_patch, tpl, None, prefix),
                            )
                            new_target[node_name] = tpl
                        environment[key] = new_target  # replace
                assert project.project_repoview.repo
                # imports defined here can be included by multiple deployments so we can't specify its root file path
                context = project.get_context(name)
                repositories = relabel_dict(context, local_env, "repositories").copy()
                _apply_imports(
                    environment,
                    imports,
                    project.project_repoview.repo.url,
                    "",
                    [],
                    repositories,
                )
        elif typename == "DeploymentPath":
            update_deployment(project, patch_inner["name"], patch_inner, False, deleted)
    return None


def _patch_environment(
    body: dict,
    project_id: str,
    batched: Optional[LocalEnv] = None,
    delete_deployment=False,
) -> ResponseReturnValue:
    patch = body.get("patch")
    assert isinstance(patch, list)
    latest_commit = body.get("latest_commit") or ""
    branch_err, branch = _branch_from_body(body)
    if branch_err:
        return branch_err
    if batched:
        readonly_localEnv: Optional[LocalEnv] = batched
    else:
        err, readonly_localEnv = localenv_from_cache_checked(
            assert_not_none(get_cache()),
            project_id,
            branch,
            "",
            latest_commit,
            body,
        )
        if err:
            return err
    assert readonly_localEnv and readonly_localEnv.project
    # if UNFURL_CURRENT_WORKING_DIR is set, use it as the home project so we don't clone remote projects that are local
    home_dir = app.config.get("UNFURL_CURRENT_WORKING_DIR") or current_app.config[
        "UNFURL_OPTIONS"
    ].get("home")
    # we need to set the parent here or else projects with separate ensemble repos select the ensemble project
    localEnv = LocalEnv(
        readonly_localEnv.project.projectRoot,
        home_dir,
        parent=readonly_localEnv,
        can_be_empty=True,
        overrides=dict(safe_mode=True),
    )
    assert localEnv.project
    repo = localEnv.project.project_repoview.gitrepo
    assert repo
    username = cast(str, body.get("username"))
    password = cast(str, body.get("private_token", body.get("password")))
    if (
        not password
        and repo.url.startswith("http")
        and not app.config.get("UNFURL_GUI_MODE")
    ):
        return create_error_response("UNAUTHORIZED", "Missing credentials")
    was_dirty = repo.is_dirty()
    starting_revision = repo.revision
    localConfig = localEnv.project.localConfig
    localConfig.reload_if_changed(readonly=False)
    err = _apply_environment_patch(patch, localEnv)
    # XXX if batched is None: else batch invalidations
    invalidate_cache(body, "environments", project_id)
    # localenv will have state unfurl.yaml
    invalidate_cache(
        {"branch": body.get("branch"), "deployment_path": "unfurl.yaml"},
        "localenv",
        project_id,
    )
    if err:
        return err
    localConfig.config.save()
    if not was_dirty:
        if repo.is_dirty():
            commit_msg = _get_commit_msg(body, "Update environment")
            err = _commit_and_push(
                repo,
                cast(str, localConfig.config.path),
                commit_msg,
                username,
                password,
                starting_revision,
                project_id,
                branch,
                batched=bool(batched),
                author=_get_author(request),
            )
            if err:
                return err  # err will be an error response
    else:
        logger.warning(
            "local repository at %s was dirty, not committing or pushing",
            localEnv.project.projectRoot,
        )

    # Standalone gui mode answers /export from the LocalEnv in
    # app.config["UNFURL_GUI_MODE"], built when the server started -- the
    # environment we just wrote stays invisible to it until it is rebuilt, so
    # the page that saved it renders none of what it saved. Same refresh
    # _patch_ensemble does when it registers a new ensemble.
    if app.config.get("UNFURL_GUI_MODE"):
        refresh_current_localenv()
    return _patch_response(repo)


def invalidate_cache(body: dict, format: str, project_id: str) -> bool:
    if project_id and project_id != ".":
        branch = body.get("branch")
        file_path = _get_filepath(format, body.get("deployment_path") or "")
        entry = CacheEntry(project_id, branch, file_path, format)
        success = entry.delete_cache(assert_not_none(get_cache()))
        logger.debug(f"invalidate cache: delete {entry.cache_key()}: {success}")
        was_inflight = entry._cancel_inflight(assert_not_none(get_cache()))
        logger.debug(
            f"invalidate cache: cancel inflight {entry.cache_key()}: {was_inflight}"
        )
        return success
    return False


def _apply_ensemble_patch(patch: list, manifest: YamlManifest):
    imports: List[ImportDef] = []
    for patch_inner in patch:
        assert isinstance(patch_inner, dict)
        typename = patch_inner.get("__typename")
        deleted = patch_inner.get("__deleted") or False
        assert isinstance(deleted, bool)
        if typename == "DeploymentTemplate":
            _update_imports(
                imports, _patch_deployment_blueprint(patch_inner, manifest, deleted)
            )
        elif typename == "ResourceTemplate":
            # notes: only update or delete node_templates declared directly in the manifest
            doc = manifest.manifest.config
            for key in [
                "spec",
                "service_template",
                "topology_template",
                "node_templates",
                patch_inner["name"],
            ]:
                if deleted:
                    if key not in doc:
                        break
                    elif key == patch_inner["name"]:
                        del doc[key]
                    else:
                        doc = doc[key]
                else:
                    if not doc.get(key):
                        doc[key] = doc = {}
                    else:
                        doc = doc[key]
            if not deleted:
                assert manifest.tosca and manifest.tosca.topology
                namespace = manifest.tosca.topology.topology_template.custom_defs
                _update_imports(
                    imports, _patch_node_template(patch_inner, doc, namespace)
                )
    assert manifest.manifest and manifest.manifest.config and manifest.repo
    skip_prefixes = ["defaults"]
    if manifest.localEnv and manifest.localEnv.manifest_environment_name:
        skip_prefixes.append(manifest.localEnv.manifest_environment_name)
    _apply_imports(
        manifest.manifest.config["spec"]["service_template"],
        imports,
        manifest.repo.url,
        # template path relative to the repository root
        manifest.get_tosca_file_path(),
        skip_prefixes,
    )


def _get_commit_msg(body, default_msg):
    msg = body.get("commit_msg", default_msg)
    if UNFURL_SERVER_DEBUG_PATCH:
        body.pop("username", None)
        body.pop("private_token", None)
        body.pop("password", None)
        body.pop("cloud_vars_url", None)
        msg += "\n" + json.dumps(body, indent=2)
    return msg


_CLOUD_VARS_PATH = re.compile(r"/api/v4/projects/[^/]+/variables/?\Z")


def _rejected_cloud_vars_url(url: str) -> str:
    """Why this server may not fetch `url`, or "" if it may.

    The server fetches this itself (see `Project._load_cloud_vars`) and the
    client puts a private token in the query string -- see
    `unfurl_cloud_vars_url()` in unfurl-gui -- so an unchecked value would
    let a caller point the server at any host and hand that host the token.

    Accept only the project-variables endpoint on the configured
    UNFURL_CLOUD_SERVER.
    """
    base = current_app.config.get("UNFURL_CLOUD_SERVER") or ""
    base_parts = urlparse(base)
    parts = urlparse(url)
    if not base_parts.hostname:
        # a local path, as the unit tests configure: no origin to match
        reason = f"UNFURL_CLOUD_SERVER is not a url: {base!r}"
    elif (parts.hostname, parts.port) != (base_parts.hostname, base_parts.port):
        reason = f"not on {base_parts.hostname}"
    elif not _CLOUD_VARS_PATH.fullmatch(parts.path):
        reason = "not the project-variables endpoint"
    else:
        return ""
    logger.warning("rejecting cloud_vars_url %s: %s", reason, sanitize_url(url))
    return reason


def _patch_ensemble(
    body: dict,
    create: bool,
    project_id: str,
    batched: Optional[LocalEnv] = None,
) -> ResponseReturnValue:
    from .cache import ServerCacheResolver

    patch = body.get("patch")
    assert isinstance(patch, list)
    environment = body.get("environment") or ""  # cloud_vars_url need the ""!
    deployment_path = body.get("deployment_path") or ""
    if create:
        # can't commit to reserved folder names
        invalid = Folders.has_excluded_path(deployment_path)
        if invalid:
            return create_error_response(
                "BAD_REQUEST",
                f'Cannot create deployment with reserved name: "{invalid}"',
            )
    cloud_vars_url = body.get("cloud_vars_url") or ""
    if cloud_vars_url:
        # checked here rather than where it is used, so a request carrying
        # one we won't fetch is refused before any repository is cloned
        rejected = _rejected_cloud_vars_url(cloud_vars_url)
        if rejected:
            return create_error_response(
                "BAD_REQUEST", f"invalid cloud_vars_url: {rejected}"
            )
    branch_err, branch = _branch_from_body(body)
    if branch_err:
        return branch_err
    existing_repo = _get_project_repo(project_id, branch, body)

    username = body.get("username")
    password = body.get("private_token", body.get("password"))
    # XXX push_url isn't used... is this needed?? and doesn't make sense in local mode
    push_url = existing_repo.url if existing_repo else app.config["UNFURL_CLOUD_SERVER"]
    if (
        push_url
        and not password
        and push_url.startswith("http")
        and not app.config.get("UNFURL_GUI_MODE")
    ):
        return create_error_response("UNAUTHORIZED", "Missing credentials")

    latest_commit = body.get("latest_commit") or ""
    if batched:
        parent_localenv: Optional[LocalEnv] = batched
    else:
        err, parent_localenv = localenv_from_cache_checked(
            assert_not_none(get_cache()),
            project_id,
            branch,
            "",
            latest_commit,
            body,
        )
        if err:
            if isinstance(existing_repo, GitRepo):
                existing_repo.repo.__del__()
                gc.collect()
            return err
    assert (
        parent_localenv
        and parent_localenv.project
        and parent_localenv.project.project_repoview.repo
    )
    clone_location = os.path.join(
        parent_localenv.project.project_repoview.repo.working_dir, deployment_path
    )

    # XXX if batched is None: else batch invalidations
    invalidate_cache(body, "deployment", project_id)
    if create:
        # Creating a deployment also changes the project's
        # DeploymentPath list (in unfurl.yaml's environments view), so the
        # cached `environments` export — which the dashboard relies on to
        # discover deployments — is also stale.
        invalidate_cache({"branch": body.get("branch")}, "environments", project_id)
        # The cached LocalEnv holds a Project whose localConfig.ensembles list is now stale too, so
        # invalidate the cached localenv too
        invalidate_cache(
            {"branch": body.get("branch"), "deployment_path": "unfurl.yaml"},
            "localenv",
            project_id,
        )
    if existing_repo:
        was_dirty = existing_repo.is_dirty()
        if isinstance(existing_repo, GitRepo):
            existing_repo.repo.__del__()
        existing_repo = None
        gc.collect()
    else:
        was_dirty = False
    starting_revision = parent_localenv.project.project_repoview.repo.revision

    current_working_dir: str = app.config.get(
        "UNFURL_CURRENT_WORKING_DIR",
        parent_localenv.project.project_repoview.repo.working_dir,
    )
    if current_working_dir == parent_localenv.project.project_repoview.repo.working_dir:
        # don't set as home if its current project
        current_working_dir = current_app.config["UNFURL_OPTIONS"].get("home")

    make_resolver = ServerCacheResolver.make_factory(
        None, dict(username=username, password=password)
    )
    parent_localenv.make_resolver = make_resolver
    gui_mode = bool(app.config.get("UNFURL_GUI_MODE"))
    if create:
        _create_ensemble(
            environment,
            deployment_path,
            parent_localenv,
            clone_location,
            was_dirty,
            body.get("deployment_blueprint"),
            current_working_dir,
            gui_mode,
            body.get("blueprint_url"),
            _get_author(request),
        )
    # set the UNFURL_CLOUD_VARS_URL because we may need to encrypt with vault secret when we commit changes.
    # set apply_url_credentials=True so that we reuse the credentials when cloning other repositories on this server
    overrides = dict(
        ENVIRONMENT=environment,
        apply_url_credentials=True,
        # we need to decrypt/encrypt yaml but we can skip secret files (expensive)
        skip_secret_files=True,
        safe_mode=True,
    )
    if cloud_vars_url:  # validated above
        overrides["UNFURL_CLOUD_VARS_URL"] = cloud_vars_url
    if gui_mode:
        overrides["UNFURL_SKIP_UPSTREAM_CHECK"] = True
        overrides["use_local_cache"] = True
    else:
        # the hosted server's clones keep no credentials (see LocalEnv)
        overrides["transient_url_credentials"] = True
    ensure_local_config(parent_localenv.project.projectRoot)
    local_env = LocalEnv(
        clone_location,
        current_working_dir,
        parent=parent_localenv,
        overrides=overrides,
    )
    local_env.make_resolver = make_resolver
    # don't validate in case we are still an incomplete draft
    manifest = local_env.get_manifest(skip_validation=True, safe_mode=True)
    # logger.info("vault secrets %s", manifest.manifest.vault.secrets)
    _apply_ensemble_patch(patch, manifest)
    manifest.manifest.save()
    if was_dirty:
        logger.warning(
            "local repository at %s was dirty, not committing or pushing",
            clone_location,
        )
    else:
        commit_msg = _get_commit_msg(body, "Update deployment")
        # XXX catch exception from commit and run git restore to rollback working dir
        committed = manifest.commit(
            commit_msg, True, ensemble_only=True, author=_get_author(request)
        )
        if committed or create:
            logger.info(f"committed to {committed} repositories")
            # In standalone gui mode, app.config["UNFURL_GUI_MODE"] holds the
            # LocalEnv that /export reads to enumerate DeploymentPath. A new
            # ensemble just registered in unfurl.yaml isn't in that view
            # until we rebuild it, so the dashboard's include_all_deployments
            # wouldn't iterate the new deployment.
            if create and app.config.get("UNFURL_GUI_MODE"):
                refresh_current_localenv()
            if (
                isinstance(manifest.repo, GitRepo)
                and not app.config.get("UNFURL_GUI_MODE")
                and not batched
            ):
                err = _push_changes(
                    manifest.repo,
                    username,
                    password,
                    starting_revision,
                    project_id,
                    branch,
                )
                if err:
                    return err
        else:
            logger.info("no changes where made, nothing committed")
    # XXX we don't support separate ensemble repositories on the client so we cheat and return the project repo's latest commit
    # instead of this correct response: return _patch_response(manifest.repo)
    assert local_env.project
    repo = local_env.project.project_repoview.gitrepo
    assert repo
    return _patch_response(repo)


def _create_ensemble(
    environment: str,
    deployment_path: str,
    parent_localenv: LocalEnv,
    clone_location: str,
    was_dirty: bool,
    deployment_blueprint: Optional[str],
    current_working_dir: str,
    gui_mode: bool,
    blueprint_url: Optional[str],
    author: Optional[str] = None,
):
    assert parent_localenv.project
    # if current_working_dir is set, use it as the home project so clone uses the local repository if available
    mono = (
        parent_localenv.instance_repoview
        and parent_localenv.instance_repoview.repo
        and parent_localenv.instance_repoview.repo.working_dir
        == (
            parent_localenv.project.project_repoview.repo
            and parent_localenv.project.project_repoview.repo.working_dir
        )
    )
    skeleton = None if gui_mode else "dashboard"
    if blueprint_url:
        logger.info(
            "creating deployment at %s for %s",
            clone_location,
            sanitize_url(blueprint_url, True),
        )
        msg = init.clone(
            blueprint_url,
            clone_location,
            existing=True,
            mono=mono,
            render=was_dirty,  # don't commit if dirty
            skeleton=skeleton,
            use_environment=environment,
            use_deployment_blueprint=deployment_blueprint,
            home=current_working_dir,
            parent_localenv=parent_localenv,
            author=author,
        )
    else:
        logger.info("creating new deployment at %s", clone_location)
        # this will clone the default ensemble if it exists or use ensemble-template
        parent_localenv.project.projectRoot
        msg = init.clone(
            parent_localenv.project.projectRoot,
            parent_localenv.project.projectRoot,
            deployment_path,
            want_init=True,
            existing=True,
            mono=mono,
            render=was_dirty,  # don't commit if dirty
            skeleton=skeleton,
            use_environment=environment,
            use_deployment_blueprint=deployment_blueprint,
            home=current_working_dir,
            parent_localenv=parent_localenv,
            author=author,
        )
    logger.info(msg)


def _push_changes(
    repo: GitRepo,
    username: Optional[str],
    password: Optional[str],
    starting_revision: str,
    project_id: str,
    branch: str,
    rollback: bool = True,
):
    """Push, discarding the unpushed commit on failure.

    `rollback=False` is for callers that roll back themselves, so a batch
    doesn't get reset twice against two different revisions.
    """
    if password:
        assert username is not None
        url = add_user_to_url(repo.url, username, password)
    else:
        url = None
    try:
        repo.push(url)
        logger.info("pushed")
    except Exception as err:
        if rollback:
            # Discard the commit we could not push -- mainly for security,
            # since a push rejected for authorization would otherwise leave
            # the caller's commit in the server's working copy for a later
            # write to carry along.
            _discard_local_commits(repo, starting_revision, "push")
        logger.error("push failed", exc_info=True)
        return create_error_response("INTERNAL_ERROR", "Could not push repository", err)
    # Reached only on a successful push, which is the point the commit
    # becomes what a reader would fetch.
    set_branch_head(project_id, branch, repo.revision)
    return None


# no longer used
def _do_patch(patch: List[GraphqlObject], target: Dict[str, GraphqlObjectsByName]):
    """Apply a list of GraphQL-style patch entries to ``target`` in place.
    ``target`` is a dict of dicts of GraphQL objects keyed by name, keyed by __typename.

    If the patch entry has a ``__deleted`` field, the entry is removed from the target,
    otherwise the entry replaces the entry in the target.
    If ``__deleted`` == "*", delete all the records with the given __typename.
    """
    for patch_inner in patch:
        typename = patch_inner.get("__typename")
        deleted = patch_inner.get("__deleted")
        name = patch_inner.get("name", deleted)
        if not name or not typename:
            logger.warning(f"skipping malformed patch {patch_inner}")
            continue
        target_inner = target.setdefault(typename, {})
        if deleted:
            if name == "*":
                del target[typename]
            else:
                if name in target_inner:
                    del target_inner[name]
                else:
                    logger.warning(
                        f"skipping delete: {name} is missing from {typename}"
                    )
            continue
        if name == "*":
            logger.warning(
                f"error: name = '*' not allowed without '__deleted' present, skipping {patch_inner}"
            )
        else:
            target_inner[name] = patch_inner


# no longer used
# def _patch_json(body: dict) -> str:
#     patch = body["patch"]
#     assert isinstance(patch, list)
#     path = body["path"]  # File path
#     clone_location, repo = _patch_request(body, body.get("project_id") or "")
#     if repo is None:
#         return create_error_response("INTERNAL_ERROR", "Could not find repository")
#     assert clone_location is not None
#     full_path = os.path.join(clone_location, path)
#     if os.path.exists(full_path):
#         with open(full_path) as read_file:
#             target = json.load(read_file)
#     else:
#         target = {}

#     _do_patch(patch, target)

#     with open(full_path, "w") as write_file:
#         json.dump(target, write_file, indent=2)

#     commit_msg = body.get("commit_msg", "Update deployment")
#     _commit_and_push(repo, full_path, commit_msg)
#     return "OK"


def _commit_and_push(
    repo: GitRepo,
    full_path: str,
    commit_msg: str,
    username: str,
    password: str,
    starting_revision: str,
    project_id: str,
    branch: str,
    batched: bool = False,
    author: Optional[str] = None,
):
    repo.add_all(full_path)
    # XXX catch exception and run git restore to rollback working dir
    repo.commit_files([full_path], commit_msg, author)
    logger.info("committed %s: %s (author: %s)", full_path, commit_msg, author or "-")
    if batched:
        return None  # the batch pushes once at the end, and records the head there
    if app.config.get("UNFURL_GUI_MODE"):
        # Nothing is pushed, so the local repository is what readers read.
        set_branch_head(project_id, branch, repo.revision)
        return None
    if password:
        url = add_user_to_url(repo.url, username, password)
    else:
        url = None
    try:
        repo.push(url)
        logger.info("pushed")
    except Exception as err:
        # Discard the commit we could not push -- mainly for security,
        # since a push rejected for authorization would otherwise leave
        # the caller's commit in the server's working copy for a later
        # write to carry along.
        _discard_local_commits(repo, starting_revision, "push")
        logger.error("push failed", exc_info=True)
        return create_error_response("INTERNAL_ERROR", "Could not push repository", err)
    set_branch_head(project_id, branch, repo.revision)
    return None
