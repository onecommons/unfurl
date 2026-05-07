# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""
HTTP endpoints that mutate a project repository: the patch APIs
(``/create_ensemble``, ``/update_ensemble``, ``/create_provider``,
``/delete_deployment``, ``/update_environment``,
``/delete_environment``, ``/batch_patch``) and the cloudmap APIs
(``GET /cloudmap``, ``POST /cloudmap``, ``GET /graph``).

All endpoints register on the shared :data:`unfurl.server.serve.app`
APIFlask instance — ``serve.py`` imports this module at the bottom so
the ``@app.<verb>(...)`` decorators run after the rest of the server
is wired up.
"""

import gc
import json
import os
import re
from base64 import b64decode
from typing import Any, Dict, List, Optional, Set, Tuple, cast

from flask import Response, current_app, jsonify, make_response, request
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
    CloudMapDocQuery,
    CloudMapDocumentPair,
    CloudMapQuery,
    CloudMapResponse,
    PatchEnsembleBody,
    PatchEnvironmentBody,
    PatchResponse,
    PATCH_RESPONSES,
    PostCloudmapRequest,
)

# Imported from .serve at the bottom of this file; serve.py imports
# this module last so all of these names are bound by the time we
# resolve them.
from .serve import (
    CacheEntry,
    DEFAULT_BRANCH,
    UNFURL_SERVER_DEBUG_PATCH,
    _get_filepath,
    _get_project_repo,
    _localenv_from_cache_checked,
    _update_queue_key,
    app,
    create_error_response,
    ensure_local_config,
    get_cache,
    get_project_id,
)

logger = getLogger("unfurl.server")


# ---------------------------------------------------------------------------
# CloudMap endpoints
# ---------------------------------------------------------------------------


@app.get("/cloudmap")
@app.doc(
    summary="CloudMap document",
    description=(
        "Return a pair ``[document, follow]`` of CloudMap documents. "
        "``document`` is the raw CloudMap (or a subset filtered by "
        "``kind`` / ``key``). ``follow`` contains records reachable "
        "from ``key`` when ``follow`` > 0, otherwise it is ``{}``."
    ),
    tags=["Export"],
)
@app.input(CloudMapDocQuery, location="query", arg_name="query")
@app.output(
    CloudMapDocumentPair,
    description="Pair: [filtered CloudMap document, followed records]",
)
def get_cloudmap(query: CloudMapDocQuery) -> ResponseReturnValue:
    from .cache import load_cloudmap_local

    project_id = query.auth_project or ""
    kind = query.kind
    key = query.key
    follow = query.follow
    need_db = follow > 0 and bool(key)
    err, doc, db = load_cloudmap_local(
        project_id,
        latest_commit=query.latest_commit,
        create_db=need_db,
    )
    if doc is None:
        if isinstance(err, Response):
            return err
        return make_response(jsonify(error=str(err)), 500)

    # `exclude` is a CSV of record primary-key ids the caller already
    # holds (matches the rust handler's contract). Non-numeric tokens
    # are silently dropped — same as the rust side.
    exclude_param = query.exclude or ""
    exclude_ids: Set[int] = set()
    for tok in exclude_param.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            exclude_ids.add(int(tok))
        except ValueError:
            pass

    if not kind:
        primary = doc
    else:
        section = doc.get(kind, {})
        if key is None:
            primary = {kind: section}
        elif not isinstance(section, dict) or key not in section:
            return make_response(
                jsonify(error=f"key {key!r} not found in {kind!r}"), 404
            )
        else:
            primary = {kind: {key: section[key]}}

    followed: Dict[str, Any] = {}
    if db is not None:
        from ..reporting import CollectVisitor, walk_cloudmap_graph

        visitor = CollectVisitor(key, follow, exclude=exclude_ids)
        walk_cloudmap_graph(db, visitor, key or "")
        followed = visitor.result

    return [primary, followed]


_CLOUDMAP_SECTIONS: Tuple[str, ...] = (
    "repositories",
    "artifacts",
    "services",
    "instantiations",
    "types",
)
_CLOUDMAP_ENVELOPE_KEYS: Tuple[str, ...] = (
    "latest_commit",
    "cloudmap_path",
    "username",
    "private_token",
    "password",
    "commit_msg",
    "atomic",
)


@app.post("/cloudmap")
@app.doc(
    summary="Modify the CloudMap document",
    description=(
        "Apply a batch of add / update / delete operations to "
        "``cloudmap.yaml``. Top-level keys split between an envelope "
        "(``latest_commit`` / ``cloudmap_path`` / ``username`` / "
        "``private_token`` / ``commit_msg``) and the cloudmap "
        "sections (``repositories``, ``artifacts``, ``services``, "
        "``instantiations``, ``types``).\n\n"
        "Each section maps record keys to a JSON object that "
        "schema-validates as the corresponding cloudmap entity. To "
        "delete a record, send the object with "
        "``unfurl.server.deleted: true``.\n\n"
        "The body is validated against "
        "``docs/cloudmap-schema.json`` (a 422 is returned on schema "
        "violation). On success the file is committed locally (no "
        "push) and the new commit oid is returned."
    ),
    tags=["Export"],
)
@app.input(PostCloudmapRequest, location="json", arg_name="body")
@app.output(
    PatchResponse,
    description="commit and list of applied changes (mirrors the rust handler's per-record response)",
)
def post_cloudmap(body: PostCloudmapRequest) -> ResponseReturnValue:
    """
    Unlike the Rust server, the atomic flag is ignored, posts are always atomic.
    Also per-record optimistic concurrency (via ``unfurl.server.{commit,version}`` keys) is not supported by this handler,
    so the ``latest_commit`` check is the only concurrency control in place.
    """
    from .cache import CLOUDMAP_BRANCH, load_cloudmap_local

    raw = _get_body(request)
    cloudmap_path = raw.get("cloudmap_path") or "cloudmap.yaml"
    latest_commit = raw.get("latest_commit")
    username = raw.get("username")
    password = raw.get("private_token", raw.get("password"))

    # Split the body: envelope keys vs cloudmap sections.
    body_sections: Dict[str, Dict[str, Any]] = {}
    for section, entries in raw.items():
        if section in _CLOUDMAP_ENVELOPE_KEYS:
            continue
        if section not in _CLOUDMAP_SECTIONS:
            return make_response(
                jsonify(error=f"unknown section {section!r}"), 400
            )
        if not isinstance(entries, dict):
            return make_response(
                jsonify(error=f"section {section!r} must be a JSON object"),
                400,
            )
        body_sections[section] = entries

    project_id = get_project_id(request)
    err, doc, _ = load_cloudmap_local(
        project_id,
        branch=CLOUDMAP_BRANCH,
        file_name=cloudmap_path,
        latest_commit=latest_commit,
        create_db=False,
    )
    if doc is None:
        if isinstance(err, Response):
            return err
        return make_response(jsonify(error=str(err)), 500)
    if not isinstance(doc, dict):
        return make_response(
            jsonify(error=f"{cloudmap_path} is not a YAML mapping"), 500
        )

    # Resolve the on-disk path and the GitRepo for `_commit_and_push`.
    cache_entry = CacheEntry(
        project_id, CLOUDMAP_BRANCH, cloudmap_path, "load_yaml", do_clone=True
    )
    cache_entry._set_project_repo()
    repo = cache_entry.checked_repo
    if not isinstance(repo, GitRepo):
        return make_response(
            jsonify(error="cloudmap repository not available"), 500
        )
    full_path = os.path.join(repo.working_dir, cloudmap_path)
    starting_revision = repo.revision
    if starting_revision and latest_commit and starting_revision != latest_commit:
        return make_response(
            jsonify(
                error=(
                    f"cloudmap has changed since latest_commit {latest_commit}, "
                    f"current revision is {starting_revision}"
                )
            ),
            409,
        )

    # Apply the body to `doc`. A record with `unfurl.server.deleted:
    # true` is removed; any other object replaces (or inserts). Track
    # which records actually changed so the response can list them in
    # ``applied`` (mirrors the rust handler's per-record response).
    # See the docstring above for the OCC / `atomic` story.
    applied: List[Dict[str, Any]] = []
    for section, entries in body_sections.items():
        section_doc: Dict[str, Any] = doc.setdefault(section, {})
        for key, value in entries.items():
            if not isinstance(value, dict):
                return make_response(
                    jsonify(
                        error=f"{section}.{key}: value must be a JSON object",
                    ),
                    400,
                )
            payload = dict(value)
            if payload.pop("unfurl.server.deleted", False):
                if section_doc.pop(key, None) is not None:
                    applied.append({"section": section, "key": key, "version": 0})
            else:
                # Strip OCC keys so they don't leak into the persisted YAML.
                payload.pop("unfurl.server.commit", None)
                payload.pop("unfurl.server.version", None)
                if section_doc.get(key) != payload:
                    section_doc[key] = payload
                    applied.append({"section": section, "key": key, "version": 0})

    if not applied:
        return {"commit": latest_commit, "applied": []}

    # Write back to disk using the project's ruamel-based loader so
    # we preserve quoting / commenting style that toscaparser's
    # `load_yaml` already round-trips for the on-disk fixture.
    from ..yamlloader import yaml as _yaml

    try:
        with open(full_path, "w") as f:
            _yaml.dump(doc, f)
    except OSError as e:
        return make_response(
            jsonify(error=f"could not write {cloudmap_path}: {e}"), 500
        )

    # Commit locally (no push). `changed` guarantees the working tree
    # is dirty, so no `is_dirty()` check is required here.
    commit_msg = raw.get("commit_msg") or f"Update {cloudmap_path}"
    commit_err = _commit_and_push(
        repo,
        full_path,
        commit_msg,
        cast(str, username or ""),
        cast(str, password or ""),
        starting_revision,
        batched=True,
    )
    if commit_err:
        return commit_err
    new_commit = repo.revision

    return {"commit": new_commit, "applied": applied}


@app.get("/graph")
@app.doc(
    summary="CloudMap graph",
    description="Return the CloudMap dependency graph as JSON, optionally filtered to a single URL.",
    tags=["Export"],
)
@app.input(CloudMapQuery, location="query", arg_name="query")
@app.output(CloudMapResponse, description="CloudMap dependency graph as JSON")
def get_cloudmap_graph(query: CloudMapQuery) -> ResponseReturnValue:
    from .cache import get_cloudmap_view
    from ..reporting import cloudmap_graph_json

    project_id = get_project_id(request)
    err, db = get_cloudmap_view(project_id)
    if db is None:
        if isinstance(err, Response):
            return err
        return make_response(jsonify(error=str(err)), 500)
    url = request.args.get("url") or ""
    result = cloudmap_graph_json(db, url)
    if "error" in result:
        return make_response(jsonify(result), 404)
    return result


# ---------------------------------------------------------------------------
# Patch endpoints
# ---------------------------------------------------------------------------


def _get_body(request) -> dict:
    body = request.json
    if request.headers.get("X-Git-Credentials"):
        body["username"], body["private_token"] = (
            b64decode(request.headers["X-Git-Credentials"]).decode().split(":", 1)
        )
    return body


@app.post("/delete_deployment")
@app.doc(
    summary="Delete a deployment",
    tags=["Project"],
    responses=PATCH_RESPONSES,
)
@app.input(PatchEnvironmentBody, location="json", arg_name="body_schema")
@app.output(PatchResponse)
def delete_deployment(body_schema: PatchEnvironmentBody) -> ResponseReturnValue:
    body = _get_body(request)
    return _patch_environment(body, get_project_id(request))


@app.post("/update_environment")
@app.doc(
    summary="Update a deployment environment",
    tags=["Project"],
    responses=PATCH_RESPONSES,
)
@app.input(PatchEnvironmentBody, location="json", arg_name="body_schema")
@app.output(PatchResponse)
def update_environment(body_schema: PatchEnvironmentBody) -> ResponseReturnValue:
    body = _get_body(request)
    return _patch_environment(body, get_project_id(request))


@app.post("/delete_environment")
@app.doc(
    summary="Delete a deployment environment",
    tags=["Project"],
    responses=PATCH_RESPONSES,
)
@app.input(PatchEnvironmentBody, location="json", arg_name="body_schema")
@app.output(PatchResponse)
def delete_environment(body_schema: PatchEnvironmentBody) -> ResponseReturnValue:
    body = _get_body(request)
    return _patch_environment(body, get_project_id(request))


@app.post("/create_provider")
@app.doc(
    summary="Create a cloud provider and its associated ensemble",
    tags=["Project"],
    responses=PATCH_RESPONSES,
)
@app.input(PatchEnsembleBody, location="json", arg_name="body_schema")
@app.output(PatchResponse)
def create_provider(body_schema: PatchEnsembleBody) -> ResponseReturnValue:
    body = _get_body(request)
    project_id = get_project_id(request)
    _patch_environment(body, project_id)
    return _patch_ensemble(body, True, project_id, False)


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
                    if normalize_git_url_hard(tpl["url"]) == norm_root:
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
                # if root is an url then this was imported by file inside a repository
                for name, tpl in repositories.items():
                    if normalize_git_url_hard(tpl["url"]) == norm_root:
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
            assert isinstance(
                value, list
            ), f"bad patch value {value} for {key} in {patch}"
            for prop in value:
                assert isinstance(
                    prop, dict
                ), f"bad {prop} in {value} for {key} in {patch}"
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
@app.input(PatchEnsembleBody, location="json", arg_name="body_schema")
@app.output(PatchResponse)
def update_ensemble(body_schema: PatchEnsembleBody) -> ResponseReturnValue:
    body = _get_body(request)
    return _patch_ensemble(body, False, get_project_id(request))


@app.post("/create_ensemble")
@app.doc(
    summary="Create a new ensemble",
    tags=["Project"],
    responses=PATCH_RESPONSES,
)
@app.input(PatchEnsembleBody, location="json", arg_name="body_schema")
@app.output(PatchResponse)
def create_ensemble(body_schema: PatchEnsembleBody) -> ResponseReturnValue:
    body = _get_body(request)
    return _patch_ensemble(body, True, get_project_id(request))


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
@app.input(BatchPatchBody, location="json", arg_name="body_schema")
@app.output(PatchResponse)
def batch_patch(body_schema: "BatchPatchBody") -> ResponseReturnValue:
    body = _get_body(request)
    project_id = get_project_id(request)
    batch_requests = body.get("requests", [])
    latest_commit = body.get("latest_commit") or ""
    branch = body.get("branch", DEFAULT_BRANCH)
    logger.info(
        "batch_patch: project=%s branch=%s requests=%d",
        project_id,
        branch,
        len(batch_requests),
    )
    err, readonly_localEnv = _localenv_from_cache_checked(
        assert_not_none(get_cache()),
        project_id,
        branch,
        "",
        latest_commit,
        body,
        False,
    )
    if err:
        return err
    assert readonly_localEnv and readonly_localEnv.project
    last_body = body  # track last body for credentials
    for req in batch_requests:
        endpoint = req.get("endpoint", "")
        # The request body is the req dict itself (endpoint + original body fields).
        req_body = req
        last_body = req_body
        create = endpoint in ("create_ensemble", "create_provider")
        if endpoint in (
            "create_provider",
            "update_environment",
            "delete_environment",
            "delete_deployment",
        ):
            result = _patch_environment(req_body, project_id, batched=readonly_localEnv)
            if isinstance(result, tuple):
                return result  # error response
        if create or endpoint == "update_ensemble":
            result = _patch_ensemble(
                req_body,
                create,
                project_id,
                check_lastcommit=False,
                batched=readonly_localEnv,
            )
            if isinstance(result, tuple):
                return result  # error response
    repo = readonly_localEnv.project.project_repoview.gitrepo
    assert repo
    username = last_body.get("username")
    password = last_body.get("private_token", last_body.get("password"))
    if not app.config.get("UNFURL_GUI_MODE"):
        err = _push_changes(repo, username, password, latest_commit)
        if err:
            return err
    # Update the Redis queue key so subsequent inc_queueid calls
    # redirect clients to the new commit.
    batch_queueid = body.get("queueid")
    if batch_queueid is not None and repo:
        new_commit = repo.revision
        _update_queue_key(project_id, latest_commit, new_commit, batch_queueid)
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
    body: dict, project_id: str, batched: Optional[LocalEnv] = None
) -> ResponseReturnValue:
    patch = body.get("patch")
    assert isinstance(patch, list)
    latest_commit = body.get("latest_commit") or ""
    branch = body.get("branch", DEFAULT_BRANCH)
    if batched:
        readonly_localEnv: Optional[LocalEnv] = batched
    else:
        err, readonly_localEnv = _localenv_from_cache_checked(
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
    if batched is None:  # XXX
        invalidate_cache(body, "environments", project_id)
    # if UNFURL_CURRENT_WORKING_DIR is set, use it as the home project so we don't clone remote projects that are local
    home_dir = app.config.get("UNFURL_CURRENT_WORKING_DIR") or current_app.config[
        "UNFURL_OPTIONS"
    ].get("home")
    localEnv = LocalEnv(
        readonly_localEnv.project.projectRoot, home_dir, can_be_empty=True
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
    err = _apply_environment_patch(patch, localEnv)
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
                bool(batched),
            )
            if err:
                return err  # err will be an error response
    else:
        logger.warning(
            "local repository at %s was dirty, not committing or pushing",
            localEnv.project.projectRoot,
        )
    return _patch_response(repo)


# def queue_request(environ):
#   url = f"{environ['wsgi.url_scheme']}://{environ['SERVER_NAME']}:{environ['SERVER_PORT']}/"


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


def _patch_ensemble(
    body: dict,
    create: bool,
    project_id: str,
    check_lastcommit: bool = True,
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
    branch = body.get("branch", DEFAULT_BRANCH)
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
        err, parent_localenv = _localenv_from_cache_checked(
            assert_not_none(get_cache()),
            project_id,
            branch,
            "",
            latest_commit,
            body,
            check_lastcommit,
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

    if batched is None:  # XXX
        invalidate_cache(body, "deployment", project_id)
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
        )
    cloud_vars_url = body.get("cloud_vars_url") or ""
    # set the UNFURL_CLOUD_VARS_URL because we may need to encrypt with vault secret when we commit changes.
    # set apply_url_credentials=True so that we reuse the credentials when cloning other repositories on this server
    overrides = dict(
        ENVIRONMENT=environment,
        apply_url_credentials=True,
        # we need to decrypt/encrypt yaml but we can skip secret files (expensive)
        skip_secret_files=True,
    )
    if cloud_vars_url:
        overrides["UNFURL_CLOUD_VARS_URL"] = cloud_vars_url
    if gui_mode:
        overrides["UNFURL_SKIP_UPSTREAM_CHECK"] = True
        overrides["use_local_cache"] = True
    ensure_local_config(parent_localenv.project.projectRoot)
    local_env = LocalEnv(clone_location, current_working_dir, overrides=overrides)
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
        committed = manifest.commit(commit_msg, True, ensemble_only=True)
        if committed or create:
            logger.info(f"committed to {committed} repositories")
            if manifest.repo and not app.config.get("UNFURL_GUI_MODE") and not batched:
                err = _push_changes(
                    manifest.repo, username, password, starting_revision
                )
                if err:
                    return err
        else:
            logger.info("no changes where made, nothing committed")
    return _patch_response(manifest.repo)


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
):
    assert parent_localenv.project
    # if current_working_dir is set, use it as the home project so clone uses the local repository if available
    mono = parent_localenv.instance_repoview is parent_localenv.project.project_repoview
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
        )
    logger.info(msg)


def _push_changes(
    repo: GitRepo,
    username: Optional[str],
    password: Optional[str],
    starting_revision: str,
):
    if password:
        assert username is not None
        url = add_user_to_url(repo.url, username, password)
    else:
        url = None
    try:
        repo.push(url)
        logger.info("pushed")
    except Exception as err:
        # discard the last commit that we couldn't push
        # this is mainly for security if we couldn't push because the user wasn't authorized
        # XXX starting_revision wrong if not a mono repo
        repo.reset(f"--hard {starting_revision or 'HEAD~1'}")
        logger.error("push failed", exc_info=True)
        return create_error_response("INTERNAL_ERROR", "Could not push repository", err)
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
    batched: bool = False,
):
    repo.add_all(full_path)
    # XXX catch exception and run git restore to rollback working dir
    repo.commit_files([full_path], commit_msg)
    logger.info("committed %s: %s", full_path, commit_msg)
    if app.config.get("UNFURL_GUI_MODE") or batched:
        return None  # don't push
    if password:
        url = add_user_to_url(repo.url, username, password)
    else:
        url = None
    try:
        repo.push(url)
        logger.info("pushed")
    except Exception as err:
        # discard the last commit that we couldn't push
        # this is mainly for security if we couldn't push because the user wasn't authorized
        repo.reset(f"--hard {starting_revision or 'HEAD~1'}")
        logger.error("push failed", exc_info=True)
        return create_error_response("INTERNAL_ERROR", "Could not push repository", err)
    return None
