import json
import logging
import os
import time
from typing import List, Optional, Tuple
from urllib.parse import unquote

import click
import uvicorn
from flask import Flask, current_app, jsonify, request
from flask_caching import Cache

from git import Repo
from .localenv import LocalEnv
from .repo import GitRepo
from .util import UnfurlError, get_random_password
from .logs import getLogger, add_log_file
from .yamlmanifest import YamlManifest
from . import to_json
from . import init

if os.getenv("UNFURL_LOGFILE"):
    add_log_file(os.getenv("UNFURL_LOGFILE"))
logger = getLogger("unfurl.server")

# note: export FLASK_ENV=development to see error stacks
flask_config = {
    # Use in-memory caching, see https://flask-caching.readthedocs.io/en/latest/#built-in-cache-backends for more options
    "CACHE_TYPE": "simple",
}
app = Flask(__name__)
# app.config.from_mapping(flask_config)
# cache = Cache(app)
app.config["UNFURL_OPTIONS"] = {}

@app.before_request
def hook():
    """
    Run before every request. If the secret is specified, check all requests for the secret.
    Secret can be in the secret query parameter (localhost:8080/health?secret=<secret>) or as an
    Authorization bearer token (Authorization=Bearer <secret>).
    """
    secret = current_app.config.get("UNFURL_SECRET")
    if secret is None:  # No secret specified, no authentication required
        return

    qs_secret = request.args.get("secret")  # Get secret from query string
    header_secret = request.headers.get(
        "Authorization"
    )  # Get secret from Authorization header
    if header_secret is not None:
        try:
            # Remove "Bearer " from header
            header_secret = header_secret.split(" ")[1]
        except IndexError:  # Quick sanity check to make sure the header is formatted correctly
            return create_error_response(
                "BAD_REQUEST",
                "The Authorization header must be in the format 'Bearer <secret>'",
            )

    if secret not in [
        qs_secret,
        header_secret,
    ]:  # No valid secret found in headers or qs
        return create_error_response(
            "UNAUTHORIZED",
            "Please pass the secret as a query parameter or as an Authorization bearer token",
        )


@app.route("/health")
def health():
    return "OK"


def _stage(git_url: str, cloud_vars_url: str, deployment_path: str) -> Tuple[Optional[str], Optional[GitRepo]]:
    # Default to exporting the ensemble provided to the server on startup
    repo = None
    path = current_app.config.get("UNFURL_ENSEMBLE_PATH", ".")
    if path and path != ".":
        # if the user set an UNFURL_ENSEMBLE_PATH try to use it
        try:
            repo = LocalEnv(
                path, can_be_empty=True
            ).find_git_repo(git_url)
            if repo:
                logging.info("found existing repo as %s", repo.working_dir)
        except UnfurlError:
            logging.debug("failed to find git repo %s in ensemble path %s", git_url, path, exc_info=True)
            repo = None

    # Repo doesn't exists, clone it
    clone_root = current_app.config.get("UNFURL_CLONE_ROOT", ".")
    if not repo:
        # clone_dest_path = GitRepo.get_path_for_git_repo(git_url)
        # XXX hack!!!
        clone_dest_path = f"dashboard.{time.time()}{get_random_password(3, '', '')}"
        ensemble_path = os.path.join(clone_root, clone_dest_path)
        result = init.clone(
            git_url,
            ensemble_path + "/",
            var=(
                [
                    "UNFURL_CLOUD_VARS_URL",
                    cloud_vars_url,
                ],
            ),
        )
        logging.info(f"cloned: {result} in pid {os.getpid()}")

        repo = LocalEnv(ensemble_path, can_be_empty=True).find_git_repo(git_url)
        if not repo:
            return None, None

    if deployment_path:
        path = os.path.join(repo.working_dir, deployment_path)
    else:
        path = repo.working_dir
    logging.info("staging path set to %s", path)
    return path, repo


@app.route("/export")
# @cache.cached(
#     query_string=True
# )  # Ensure that the request cached includes the query string (the response differs between different formats)
def export():
    requested_format = request.args.get("format", "deployment")
    if requested_format not in ["blueprint", "environments", "deployment"]:
        return create_error_response(
            "BAD_REQUEST",
            "Query parameter 'format' must be one of 'blueprint', 'environments' or 'deployment'",
        )

    # If asking for external repository
    if request.args.get("url") is not None:
        git_url = unquote(request.args.get("url"))  # Unescape the URL
        cloud_vars_url = request.args.get("cloud_vars_url") or ""
        if cloud_vars_url:
            cloud_vars_url = unquote(cloud_vars_url)
        logger.warning("cloud_vars_url %s", cloud_vars_url)
        deployment_path = request.args.get("deployment_path") or ""
        path, repo = _stage(git_url, cloud_vars_url, deployment_path)
        if path is None:
            return create_error_response(
                "INTERNAL_ERROR", "Could not find repository"
            )
    else:
        path = current_app.config["UNFURL_ENSEMBLE_PATH"]

    deployment_enviroment = request.args.get("environment")
    if deployment_enviroment is None:
        deployment_enviroment = current_app.config["UNFURL_OPTIONS"].get(
            "use_environment"
        )

    try:
        local_env = LocalEnv(
            path,
            current_app.config["UNFURL_OPTIONS"].get("home"),
            override_context=deployment_enviroment or "",  # cloud_vars_url need the ""!
        )
        local_env.overrides["UNFURL_SKIP_VAULT_DECRYPT"] = True
    except UnfurlError as e:
        logger.error("error loading project at %s", path, exc_info=True)
        error_message = str(e)
        # Sort of a hack to get the specific error since it only raises an "UnfurlError"
        # Will break if the error message changes or if the Exception class changes
        if "No environment named" in error_message:
            return create_error_response(
                "BAD_REQUEST",
                f"No environment named '{deployment_enviroment}' found in the repository",
            )
        else:
            return create_error_response("INTERNAL_ERROR", "An internal error occurred")

    exporter = getattr(to_json, "to_" + requested_format)
    json_summary = exporter(local_env)

    return jsonify(json_summary)


@app.route("/delete_deployment", methods=["POST"])
def delete_deployment():
    body = request.json
    return _patch_json(body)


@app.route("/update_environment", methods=["POST"])
def update_environment():
    body = request.json
    return _patch_environment(body)


@app.route("/delete_environment", methods=["POST"])
def delete_environment():
    body = request.json
    return _patch_environment(body)


@app.route("/update_deployment", methods=["POST"])
def update_deployment():
    body = request.json
    return _patch_json(body)


def _patch_deployment_blueprint(patch: dict, manifest: "YamlManifest", deleted: bool) -> None:
    deployment_blueprint = patch["name"]
    doc = manifest.manifest.config
    deployment_blueprints = (
        doc.setdefault("spec", {}).setdefault("deployment_blueprints", {})
    )
    current = deployment_blueprints.setdefault(deployment_blueprint, {})
    if deleted:
        del deployment_blueprints[deployment_blueprint]
    else:
        keys = ["title", "cloud", "description", "primary", "source", "projectPath"]
        for key, prop in patch.items():
            if key in keys:
                current[key] = prop
            elif key == "ResourceTemplate":
                # XXX do we care about these?
                node_templates = current.setdefault("resource_templates", {})
                for name, val in prop.items():
                    tpl = node_templates.setdefault(name, {})
                    _patch_node_template(val, tpl)


def _patch_node_template(patch: dict, tpl: dict) -> None:
    for key, value in patch.items():
        if key == "type":
            tpl[key] = value
        elif key == "title":
            if value != patch["name"]:
                tpl.setdefault("metadata", {})["title"] = value
        elif key == "properties":
            props = tpl.setdefault("properties", {})
            assert isinstance(value, list)
            for prop in value:
                props[prop["name"]] = prop["value"]
        elif key == "dependencies":
            requirements = [{dependency["name"]: dependency["match"]}
                            for dependency in value if "match" in dependency]
            if requirements or "requirements" in tpl:
                tpl["requirements"] = requirements


@app.route("/delete_ensemble", methods=["POST"])
def delete_ensemble():
    body = request.json
    return _patch_ensemble(body, False)


@app.route("/update_ensemble", methods=["POST"])
def update_ensemble():
    body = request.json
    return _patch_ensemble(body, False)


@app.route("/create_ensemble", methods=["POST"])
def create_ensemble():
    body = request.json
    return _patch_ensemble(body, True)


def _patch_environment(body: dict) -> str:
    patch = body.get("patch")
    assert isinstance(patch, list)
    clone_location, repo = _patch_request(body)
    if repo is None:
        # XXX create a new ensemble if patch is for a new deployment
        return create_error_response("INTERNAL_ERROR", "Could not find repository")
    localEnv = LocalEnv(clone_location)
    for patch_inner in patch:
        assert isinstance(patch_inner, dict)
        typename = patch_inner.get("__typename")
        deleted = patch_inner.get("__deleted") or False
        assert isinstance(deleted, bool)
        assert localEnv.project
        localConfig = localEnv.project.localConfig
        if typename == "DeploymentEnvironment":
            environments = localConfig.config.config.setdefault("environments", {})
            name = patch_inner["name"]
            if deleted:
                if name in environments:
                    del environments[name]
            else:
                environment = environments.setdefault(name, {})
                for key in patch_inner:
                    if key == "instances" or key == "connections":
                        target = environment.get(key) or {}
                        for node_name, node_patch in patch_inner[key].items():
                            tpl = target.setdefault(node_name, {})
                            if not isinstance(tpl, dict):
                                # connections keys can be a string or null
                                tpl = {}
                            _patch_node_template(node_patch, tpl)
                        environment[key] = target  # replace
        elif typename == "DeploymentPath":
            path = patch_inner["name"]
            tpl = localEnv.project.find_ensemble_by_path(path)
            if deleted:
                if tpl:
                    localConfig.ensembles.remove(tpl)
            else:
                if not tpl:
                    deployment_path = patch_inner["name"]
                    if deployment_path[-1] != "/":
                        deployment_path += "/"
                    tpl = dict(file=deployment_path)
                    localConfig.ensembles.append(tpl)
                for key in patch_inner:
                    if key not in ["name", "__deleted", "__typename"]:
                        tpl[key] = patch_inner[key]
            localConfig.config.config["ensembles"] = localConfig.ensembles
    localConfig.config.save()
    commit_msg = body.get("commit_msg", "Update environment")
    _commit_and_push(repo, localConfig.config.path, commit_msg)
    return "OK"


def _patch_ensemble(body: dict, create: bool) -> str:
    patch = body.get("patch")
    assert isinstance(patch, list)
    environment = body.get("environment") or ""  # cloud_vars_url need the ""!
    clone_location, repo = _patch_request(body)
    if repo is None:
        # XXX create a new ensemble if patch is for a new deployment
        return create_error_response("INTERNAL_ERROR", "Could not find repository")
    if create:
        deployment_blueprint = body.get("deployment_blueprint")
        blueprint_url = body["blueprint_url"]
        logger.info("creating deployment at %s for %s", clone_location, blueprint_url)
        msg = init.clone(blueprint_url, clone_location, existing=True, mono=True, skeleton="dashboard",
                         environment=environment, use_deployment_blueprint=deployment_blueprint)
        logger.info(msg)
    manifest = LocalEnv(clone_location, override_context=environment).get_manifest()
    # logger.info("vault secrets %s", manifest.manifest.vault.secrets)
    for patch_inner in patch:
        assert isinstance(patch_inner, dict)
        typename = patch_inner.get("__typename")
        deleted = patch_inner.get("__deleted") or False
        assert isinstance(deleted, bool)
        if typename == "DeploymentTemplate":
            _patch_deployment_blueprint(patch_inner, manifest, deleted)
        elif typename == "ResourceTemplate":
            # notes: only update or delete node_templates declared directly in the manifest
            doc = manifest.manifest.config
            for key in ["spec", "service_template", "topology_template", "node_templates", patch_inner["name"]]:
                if deleted:
                    if key not in doc:
                        break
                    elif key == patch_inner["name"]:
                        del doc[key]
                else:
                    doc = doc.setdefault(key, {})
            if not deleted:
                _patch_node_template(patch_inner, doc)

    manifest.manifest.save()
    manifest.add_all()
    commit_msg = body.get("commit_msg", "Update deployment")
    committed = manifest.commit(commit_msg, False)
    logger.info(f"committed to {committed} repositories")
    if repo.repo.remotes:
        repo.repo.remotes.origin.push()
        logger.info("pushed")
    return "OK"


def _do_patch(patch: List[dict], target: dict):
    for patch_inner in patch:
        typename = patch_inner.get("__typename")
        deleted = patch_inner.get("__deleted")
        target_inner = target
        if typename != "*":
            if not target_inner.get(typename):
                target_inner[typename] = {}
            target_inner = target_inner[typename]
        if deleted:
            if deleted == "*":
                if typename == "*":
                    target = {}
                else:
                    del target[typename]
            elif deleted in target[typename]:
                del target[typename][deleted]
            else:
                logger.warning(f"skipping delete: {deleted} is missing from {typename}")
            continue
        target_inner[patch_inner["name"]] = patch_inner


def _patch_json(body: dict) -> str:
    patch = body["patch"]
    assert isinstance(patch, list)
    path = body["path"]  # File path
    clone_location, repo = _patch_request(body)
    if repo is None:
        return create_error_response("INTERNAL_ERROR", "Could not find repository")
    assert clone_location is not None
    full_path = os.path.join(clone_location, path)
    if os.path.exists(full_path):
        with open(full_path) as read_file:
            target = json.load(read_file)
    else:
        target = {}

    _do_patch(patch, target)

    with open(full_path, "w") as write_file:
        json.dump(target, write_file, indent=2)

    commit_msg = body.get("commit_msg", "Update deployment")
    _commit_and_push(repo, full_path, commit_msg)
    return "OK"


def _commit_and_push(repo, full_path, commit_msg):
    repo.add_all(full_path)
    repo.commit_files([full_path], commit_msg)
    logger.info("committed %s: %s", full_path, commit_msg)
    if repo.repo.remotes:
        repo.repo.remotes.origin.push()
        logger.info("pushed")


def _patch_request(body: dict) -> Tuple[Optional[str], Optional[GitRepo]]:
    # Repository URL
    project_path = body["projectPath"]

    # Project is external
    if project_path.startswith("http") or project_path.startswith("git"):
        cloud_vars_url = body.get("cloud_vars_url") or ""
        deployment_path = body.get("deployment_path") or ""
        clone_location, repo = _stage(project_path, cloud_vars_url, deployment_path)
        if repo is None:
            return None, None
    else:
        clone_location = project_path
        repo = Repo.init(clone_location)
        if repo is None:
            return None, None
        repo = GitRepo(repo)

    return clone_location, repo


def create_error_response(code, message):
    http_code = 400  # Default to BAD_REQUEST
    if code == "BAD_REQUEST":
        http_code = 400
    elif code == "UNAUTHORIZED":
        http_code = 401
    elif code == "INTERNAL_ERROR":
        http_code = 500
    return jsonify({"code": code, "message": message}), http_code


def serve(
    host: str,
    port: int,
    secret: str,
    clone_root: str,
    project_or_ensemble_path: click.Path,
    options: dict,
):
    """Start a simple HTTP server which will expose part of the CLI's API.

    Args:
        host (str): Which host to bind to (0.0.0.0 will allow external connections)
        port (int): Port to listen to (defaults to 8080)
        secret (str): The secret to use to authenticate requests
        clone_root (str): The root directory to clone all repositories into
        project_or_ensemble_path (click.Path): The path of the ensemble or project to base requests on
        options (dict): Additional options to pass to the server (as passed to the unfurl CLI)
    """
    app.config["UNFURL_SECRET"] = secret
    app.config["UNFURL_OPTIONS"] = options
    app.config["UNFURL_CLONE_ROOT"] = clone_root
    app.config["UNFURL_ENSEMBLE_PATH"] = project_or_ensemble_path

    # Start one WSGI server
    uvicorn.run(app, host=host, port=port, interface="wsgi", log_level=logger.getEffectiveLevel())

    # app.run(host=host, port=port)
    # gunicorn"  , "-b", "0.0.0.0:5000", "unfurl.server:app"
    # from gunicorn.app.wsgiapp import WSGIApplication
    # WSGIApplication().run()