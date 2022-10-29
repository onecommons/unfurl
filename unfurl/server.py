import json
import logging
import os
from urllib.parse import unquote

import click
import uvicorn
from flask import Flask, current_app, jsonify, request
from flask_caching import Cache

from git import Repo
from unfurl.localenv import LocalEnv
from unfurl.repo import GitRepo
from unfurl.util import UnfurlError

logger = logging.getLogger("unfurl")

flask_config = {
    # Use in-memory caching, see https://flask-caching.readthedocs.io/en/latest/#built-in-cache-backends for more options
    "CACHE_TYPE": "simple",
}
app = Flask(__name__)
app.config.from_mapping(flask_config)
cache = Cache(app)


@app.before_request
def hook():
    """
    Run before every request. If the secret is specified, check all requests for the secret.
    Secret can be in the secret query parameter (localhost:8080/health?secret=<secret>) or as an
    Authorization bearer token (Authorization=Bearer <secret>).
    """
    secret = current_app.config["UNFURL_SECRET"]
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


@app.route("/export")
@cache.cached(
    query_string=True
)  # Ensure that the request cached includes the query string (the response differs between different formats)
def export():
    requested_format = request.args.get("format", "deployment")
    if requested_format not in ["blueprint", "environments", "deployment"]:
        return create_error_response(
            "BAD_REQUEST",
            "Query parameter 'format' must be one of 'blueprint', 'environments' or 'deployment'",
        )

    # Default to exporting the ensemble provided to the server on startup
    path = current_app.config["UNFURL_ENSEMBLE_PATH"]

    clone_root = current_app.config["UNFURL_CLONE_ROOT"]

    # If asking for external repository
    if request.args.get("url") is not None:
        git_url = unquote(request.args.get("url"))  # Unescape the URL
        repo = LocalEnv(
            current_app.config["UNFURL_ENSEMBLE_PATH"], can_be_empty=True
        ).find_git_repo(git_url)

        # Repo doesn't exists, clone it
        if not repo:
            from . import init

            result = init.clone(
                git_url,
                clone_root + Repo.get_path_for_git_repo(git_url) + "/",
                empty=True,
                var=(
                    [
                        "UNFURL_CLOUD_VARS_URL",
                        unquote(request.args.get("cloud_vars_url")),
                    ],
                ),
            )
            logging.info(result)

            repo = LocalEnv(
                clone_root + "/" + Repo.get_path_for_git_repo(git_url),
                can_be_empty=True,
            ).find_git_repo(git_url)

            if repo is None:
                return create_error_response(
                    "INTERNAL_ERROR", "Could not find repository"
                )

        deployment_path = request.args.get("deployment_path")
        if deployment_path:
            path = os.path.join(repo.working_dir, deployment_path)
        else:
            path = repo.working_dir

    deployment_enviroment = request.args.get("environment")
    if deployment_enviroment is None:
        deployment_enviroment = current_app.config["UNFURL_OPTIONS"].get(
            "use_environment"
        )

    from . import to_json

    try:
        local_env = LocalEnv(
            path,
            current_app.config["UNFURL_OPTIONS"].get("home"),
            override_context=deployment_enviroment,
        )
    except UnfurlError as e:
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


@app.route("/update_deployment", methods=["POST"])
def update_deployment():
    body = request.json
    # Repository URL
    project_path = body.get("projectPath")
    # File path
    path = body.get("path")
    # Patch
    patch = body.get("patch")

    commit_msg = body.get("commit_msg", "Update deployment")

    # Project is external
    if project_path.startswith("http") or project_path.startswith("git"):
        repo = LocalEnv(
            current_app.config["UNFURL_ENSEMBLE_PATH"], can_be_empty=True
        ).find_git_repo(project_path)

        # Repo doesn't exists, clone it
        if repo is None:
            clone_root = current_app.config["UNFURL_CLONE_ROOT"]
            git_url = project_path
            from . import init

            clone_location = (
                clone_root + "/" + Repo.get_path_for_git_repo(git_url)
            ).lstrip("./")

            result = init.clone(
                git_url,
                clone_location + "/",
            )
            logging.info(result)

            repo = LocalEnv(
                clone_location,
                can_be_empty=True,
            ).find_git_repo(git_url)

            if repo is None:
                return create_error_response(
                    "INTERNAL_ERROR", "Could not find repository"
                )

    else:
        clone_location = project_path
        repo = Repo.init(clone_location)
        if repo is None:
            return create_error_response("INTERNAL_ERROR", "Could not find repository")
        repo = GitRepo(repo)

    target = json.loads(repo.show(path, "HEAD"))

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
            else:
                del target[typename][deleted]
            continue
        target_inner[patch_inner["name"]] = patch_inner

    with open(f"{clone_location}/{path}", "w") as f:
        f.write(json.dumps(target, indent=2))

    repo.add_all(clone_location)
    repo.commit_files([f"{clone_location}/{path}"], commit_msg)

    return "OK"


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
    uvicorn.run(app, host=host, port=port, interface="wsgi", log_level="info")

    # app.run(host=host, port=port)
