import os
from typing import Any, Iterator, List, Literal, Optional, Union
from typing_extensions import TypedDict, Required

from ..logs import is_sensitive, getLogger

from ..repo import GitRepo

from ..localenv import LocalEnv
from .serve import app
from . import serve
from flask import request, Response, Request, jsonify, send_file
from jinja2 import Environment, FileSystemLoader
import requests
import re
from glob import glob
from rich import inspect
from urllib.parse import urlparse
from ruamel.yaml.comments import CommentedMap


logger = getLogger("unfurl.gui")

local_dir = os.path.dirname(os.path.abspath(__file__))

with app.app_context():
    UFGUI_DIR = os.getenv("UFGUI_DIR", local_dir)
    WEBPACK_ORIGIN = os.getenv("WEBPACK_ORIGIN")  # implied dev mode
    DIST = os.path.join(UFGUI_DIR, "dist")
    PUBLIC = os.path.join(UFGUI_DIR, "public")

    # getting an error when trying to access this variable
    """
    Exiting with error: Working outside of application context.

    This typically means that you attempted to use functionality that needed
    to interface with the current application object in some way. To solve
    this, set up an application context with app.app_context().  See the
    documentation for more information.
    """
    UNFURL_SERVE_PATH = ""  # os.getenv("UNFURL_SERVE_PATH", "")
    user = os.getenv("USER", "unfurl-user")
    password = None

env = Environment(loader=FileSystemLoader(os.path.join(local_dir, "templates")))
origin = None
blueprint_template = env.get_template("project.j2.html")
dashboard_template = env.get_template("dashboard.j2.html")

# the overrides is a hackish way to load all env vars for all environments
localenv = LocalEnv(UNFURL_SERVE_PATH, overrides={"ENVIRONMENT": "*"})
assert localenv.instance_repoview and localenv.instance_repoview.repo
localrepo = localenv.instance_repoview.repo

localrepo_is_dashboard = (
    len(glob(os.path.join(UNFURL_SERVE_PATH, "unfurl.y*ml"))) > 0
    and len(glob(os.path.join("ensemble-template.y*ml"))) == 0
)

home_project = localrepo.project_path() if localrepo_is_dashboard else None

if localrepo_is_dashboard and localrepo.remote and localrepo.remote.url:
    parsed = urlparse(localrepo.remote.url)
    [user, password, *rest] = re.split(r"[@:]", parsed.netloc)

    origin = f"{parsed.scheme}://{parsed.hostname}"


def get_project_readme(repo):
    for filename in ["README", "README.md", "README.txt"]:
        path = os.path.join(repo.working_dir, filename)
        if os.path.exists(path):
            with open(path, "r") as file:
                return file.read()


def get_head_contents(f):
    with open(f, "r") as file:
        contents = file.read()
        match = re.search(r"<head.*?>(.*?)</head>", contents, re.DOTALL)
        if match:
            return match.group(1)
        else:
            return ""


if WEBPACK_ORIGIN:
    project_head = f"""
    <head>
      {get_head_contents(os.path.join(PUBLIC, "index.html"))}

      <script defer src="/js/chunk-vendors.js"></script>
      <script defer src="/js/chunk-common.js"></script>
      <script defer src="/js/project.js"></script>
    </head>
    """

    dashboard_head = f"""
    <head>
      {get_head_contents(os.path.join(PUBLIC, "index.html"))}

      <script defer src="/js/chunk-vendors.js"></script>
      <script defer src="/js/chunk-common.js"></script>
      <script defer src="/js/dashboard.js"></script>
    </head>

    """
else:
    project_head = (
        f"<head>{get_head_contents(os.path.join(DIST, 'project.html'))}</head>"
    )
    dashboard_head = (
        f"<head>{get_head_contents(os.path.join(DIST, 'dashboard.html'))}</head>"
    )


def serve_document(path):
    server_fragment = re.split(r"/?(deployment-drafts|-)(?=/)", path)[0]
    repo = get_repo(server_fragment)

    if not repo:
        return "Not found", 404
    format = "environments"
    if glob(os.path.join(repo.working_dir, "ensemble-template.y*ml")):
        format = "blueprint"

    project_path = repo.project_path()
    project_name = os.path.basename(project_path)

    if format == "blueprint":
        template = blueprint_template
    else:
        template = dashboard_template

    return template.render(
        name=project_name,
        readme=get_project_readme(repo),
        user=user,
        origin=origin,
        head=(project_head if format == "blueprint" else dashboard_head),
        project_path=project_path,
        namespace=os.path.dirname(project_path),
        home_project=home_project,
        working_dir_project=home_project if localrepo_is_dashboard else project_path,
    )


def proxy_webpack(url):
    res = requests.request(  # ref. https://stackoverflow.com/a/36601467/248616
        method=request.method,
        url=url,
        headers={
            k: v for k, v in request.headers if k.lower() != "host"
        },  # exclude 'host' header
        data=request.get_data(),
        cookies=request.cookies,
        allow_redirects=False,
    )

    # exclude some keys in :res response
    excluded_headers = [
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
    ]  # NOTE we here exclude all "hop-by-hop headers" defined by RFC 2616 section 13.5.1 ref. https://www.rfc-editor.org/rfc/rfc2616#section-13.5.1
    headers = [
        (k, v) for k, v in res.raw.headers.items() if k.lower() not in excluded_headers
    ]

    return Response(res.content, res.status_code, headers)


def get_repo(project_path, branch=None) -> Optional[GitRepo]:
    project_path = project_path.rstrip("/")
    repo: Optional[GitRepo] = None

    if not branch:
        branch = serve.get_default_branch(project_path, branch, {"format": "blueprint"})

    def do_export():
        req = Request({
            'QUERY_STRING': f"?format=blueprint&auth_project={project_path}"
        })
        serve._export(
            req,
            requested_format="blueprint",
            deployment_path="",
            include_all=False
        )

    def search_repo_paths():
        repo = serve._get_project_repo(project_path, branch, {})
        if not repo:
            repo = serve._get_project_repo(os.path.basename(project_path), branch, {})
        return repo

    if localrepo and (project_path == localrepo.project_path()):
        repo = localrepo
    else:
        repo = search_repo_paths()
        if not repo:
            do_export()
            repo = search_repo_paths()

    return repo


class EnvVar(TypedDict, total=False):
    # see https://docs.gitlab.com/ee/api/project_level_variables.html
    id: Union[int, str]  # ID or URL-encoded path of the project
    key: Required[str]
    masked: Required[bool]
    environment_scope: Required[str]
    value: Any
    secret_value: Any  # ??? not sent by api
    _destroy: bool
    variable_type: Required[Union[Literal["env_var"], Literal["file"]]]
    raw: Literal[False]  # if true value isn't expanded
    protected: Literal[False]


def _set_variables(env_vars: List[EnvVar]):
    project = localenv.project or localenv.homeProject
    assert project
    secret_config_key, secret_config = project.localConfig.find_secret_include()
    secret_environments = (
        secret_config.setdefault("environments", CommentedMap())
        if secret_config
        else None
    )
    config: dict = project.localConfig.config.config
    assert config
    env = config.setdefault("environments", CommentedMap())
    modified_secrets = False
    modified_config = False
    for envvar in env_vars:
        environment_scope = envvar["environment_scope"]
        env_name = "defaults" if environment_scope == "*" else environment_scope
        key = envvar["key"]
        value = envvar.get("secret_value", envvar.get("value"))
        if envvar["variable_type"] == "file":
            value = {"eval": dict(tempfile=value)}
        if envvar.get("_destroy"):
            if env_name in env and key in env[env_name]:
                modified_config = True
                del env[env_name][key]
            secret_env = (
                secret_environments.get(env_name) if secret_environments else None
            )
            if secret_env and key in secret_env.get("variables", {}):
                modified_secrets = True
                del secret_env["variables"][key]
        else:
            if envvar["masked"]:
                env.pop(key, None)  # in case this flag changed
                if secret_environments is not None:
                    secret_env = secret_environments.setdefault(
                        env_name, CommentedMap()
                    )
                    modified_secrets = True
                    secret_env.setdefault("variables", {})[key] = {"eval": dict(sensitive=value)}  # mark sensitive
            else:
                # in case this flag changed
                secret_env = (
                    secret_environments.get(env_name) if secret_environments else None
                )
                if secret_env and key in secret_env.get("variables", {}):
                    modified_secrets = True
                    del secret_env["variables"][key]
                modified_config = True
                env.setdefault(env_name, CommentedMap())[key] = value
    if modified_secrets:
        project.localConfig.config.save_include(secret_config_key)
    if modified_config:
        project.localConfig.config.save()
    if modified_secrets or modified_config:
        globals()["localenv"] = LocalEnv(UNFURL_SERVE_PATH, overrides={"ENVIRONMENT": "*"})
    return {"variables": list(_yield_variables())}


def _yield_variables() -> Iterator[EnvVar]:
    project = localenv.project or localenv.homeProject
    assert project
    for env_name, context in project.contexts.items():
        env_vars = context and context.get("variables")
        if not env_vars:
            continue
        if env_name == "defaults":
            scope = "*"
        else:
            scope = env_name
        for name, value in env_vars.items():
            masked = is_sensitive(value)
            variable_type: Union[Literal["env_var"], Literal["file"]] = "env_var"
            if isinstance(value, dict) and "eval" in value:
                eval_ref = value["eval"]
                if "sensitive" in eval_ref:
                    masked = True
                    value = eval_ref["sensitive"]
                if "tempfile" in eval_ref:
                    variable_type = "file"
                    value = eval_ref["tempfile"]
            yield EnvVar(
                id=scope + ":" + name,
                key=name,
                masked=masked,
                variable_type=variable_type,
                value=value,
                environment_scope=scope,
                protected=False,
                raw=False,
            )


def create_gui_routes():
    app.config["UNFURL_GUI_MODE"] = True
    @app.route("/<path:project_path>/-/variables", methods=["GET"])
    def get_variables(project_path):
        if get_repo(project_path) != localrepo:
            return "Not found", 404
        return {"variables": list(_yield_variables())}

    @app.route("/<path:project_path>/-/variables", methods=["PATCH"])
    def set_variables(project_path):
        if get_repo(project_path) != localrepo:
            return "Not found", 404

        body = request.json
        if isinstance(body, dict) and "variables_attributes" in body:
            return _set_variables(body["variables_attributes"])
        else:
            return "Bad Request", 400

    @app.route("/api/v4/unfurl_access_token")
    def unfurl_access_token():
        return {"token": password}

    @app.route("/api/v4/projects/<path:project_path>/repository/branches")
    def branches(project_path):
        repo = get_repo(project_path)
        if not repo:
            return "Not found", 404

        return jsonify(
            # TODO
            [{"name": repo.active_branch, "commit": {"id": repo.revision}}]
        )

    @app.route("/api/v4/projects/<path:project_path>")
    def project(project_path):
        repo = get_repo(project_path)
        if not repo:
            return {}
        return {"name": os.path.basename(repo.project_path())}

    @app.route("/<path:project_path>/-/raw/<branch>/<path:file>")
    def local_file(project_path, branch, file):
        repo = get_repo(project_path)
        if repo:
            full_path = os.path.join(repo.working_dir, file)
            if os.path.exists(full_path):
                return send_file(full_path)

        return "Not found", 404

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve_webpack(path):
        if "accept" in request.headers and "text/html" in request.headers["accept"]:
            return serve_document(path)

        if WEBPACK_ORIGIN:
            url = f"{WEBPACK_ORIGIN}/{path}"
        else:
            url = path

        qs = request.query_string.decode("utf-8")
        if qs != "":
            url += "?" + qs

        if request.headers["sec-fetch-dest"] == "iframe":
            return "Not found", 404

        if WEBPACK_ORIGIN:
            return proxy_webpack(url)
        else:
            return send_file(os.path.join(DIST, path))
