"""
  UI for local Unfurl project

  "project_path" is used by serve for export and patch

  Returns:
      _type_: _description_

  Yields:
      _type_: _description_
"""

import os
from typing import Any, Iterator, List, Literal, Optional, Union
from typing_extensions import TypedDict, Required

from ..logs import is_sensitive, getLogger

from ..repo import Repo, GitRepo

from .serve import app
from ..localenv import LocalEnv
from .gui_variables import set_variables, yield_variables
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

UFGUI_DIR = os.getenv("UFGUI_DIR", local_dir)
WEBPACK_ORIGIN = os.getenv("WEBPACK_ORIGIN")  # implied dev mode
DIST = os.path.join(UFGUI_DIR, "dist")
PUBLIC = os.path.join(UFGUI_DIR, "public")
UNFURL_SERVE_PATH = os.getenv("UNFURL_SERVE_PATH", "")

env = Environment(loader=FileSystemLoader(os.path.join(local_dir, "templates")))
blueprint_template = env.get_template("project.j2.html")
dashboard_template = env.get_template("dashboard.j2.html")


def get_project_readme(repo: GitRepo) -> str:
    for filename in ["README", "README.md", "README.txt"]:
        path = os.path.join(repo.working_dir, filename)
        if os.path.exists(path):
            with open(path, "r") as file:
                return file.read()
    return ""


def get_head_contents(f) -> str:
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


def serve_document(path, localenv):
    localrepo = localenv.project.project_repoview.repo

    localrepo_is_dashboard = bool(localenv.manifestPath)

    home_project = get_project_path(localrepo) if localrepo_is_dashboard else None

    if localrepo_is_dashboard and localrepo.remote and localrepo.remote.url:
        parsed = urlparse(localrepo.remote.url)
        [user, _, *_] = re.split(r"[@:]", parsed.netloc)
        origin = f"{parsed.scheme}://{parsed.hostname}"
    else:
        parsed = None
        user = ""
        origin = None

    server_fragment = re.split(r"/?(deployment-drafts|-)(?=/)", path)
    repo = _get_repo(server_fragment[0].lstrip("/"), localenv)

    if not repo:
        return "Not found", 404
    format = "environments"
    # assume serving dashboard
    if repo.repo != localrepo.repo or not localrepo_is_dashboard:
        format = "blueprint"

    project_path = get_project_path(repo)
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


def get_project_path(repo):
    server_url = app.config["UNFURL_CLOUD_SERVER"]
    server_host = urlparse(server_url).hostname
    cloud_remote = repo.find_remote(host=server_host)
    if cloud_remote:
        project_path = Repo.get_path_for_git_repo(cloud_remote.url, False)
    else:
        project_path = "local:" + repo.project_path()
    return project_path


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


import git


def _get_repo(project_path, localenv: LocalEnv, branch=None) -> Optional[GitRepo]:
    if not project_path or project_path == "local:":
        return localenv.project.project_repoview.repo if localenv.project else None

    local_projects = app.config["UNFURL_LOCAL_PROJECTS"]
    if project_path[-1] != "/":
        project_path += "/"
    if project_path in local_projects:
        working_dir = local_projects[project_path]
        return GitRepo(git.Repo(working_dir))

    if project_path.startswith("local:"):
        # it's not a cloud server project
        logger.error(f"Can't find project {project_path} in {list(local_projects)}")
        return None

    project_path = project_path.rstrip("/")
    repo: Optional[GitRepo] = None

    if not branch:
        branch = serve.get_default_branch(project_path, branch, {"format": "blueprint"})

    # XXX no need to do export!
    def do_export():
        req = Request(
            {"QUERY_STRING": f"?format=blueprint&auth_project={project_path}"}
        )
        serve._export(
            req, requested_format="blueprint", deployment_path="", include_all=False
        )

    def search_repo_paths():
        repo = serve._get_project_repo(project_path, branch, {})
        if not repo:
            repo = serve._get_project_repo(os.path.basename(project_path), branch, {})
        return repo

    localrepo = localenv.project and localenv.project.project_repoview.repo
    if localrepo and (project_path == localrepo.project_path()):
        repo = localrepo
    else:
        repo = search_repo_paths()
        if not repo:
            do_export()
            repo = search_repo_paths()

    return repo


def create_gui_routes(localenv: LocalEnv):
    app.config["UNFURL_GUI_MODE"] = localenv
    localrepo = (
        localenv.project
        and localenv.project.project_repoview
        and localenv.project.project_repoview.repo
    )
    assert localrepo

    def get_repo(project_path, branch=None):
        return _get_repo(project_path, localenv, branch)

    @app.route("/<path:project_path>/-/variables", methods=["GET"])
    def get_variables(project_path):
        repo = get_repo(project_path)
        if not repo or repo.repo != localrepo.repo:
            return "Not found", 404
        return {"variables": list(yield_variables(localenv))}

    @app.route("/<path:project_path>/-/variables", methods=["PATCH"])
    def patch_variables(project_path):
        nonlocal localenv
        repo = get_repo(project_path)
        if not repo or repo.repo != localrepo.repo:
            return "Not found", 404

        body = request.json
        if isinstance(body, dict) and "variables_attributes" in body:
            localenv = set_variables(localenv, body["variables_attributes"])
            return {"variables": list(yield_variables(localenv))}
        else:
            return "Bad Request", 400

    # XXX remove
    @app.route("/api/v4/unfurl_access_token")
    def unfurl_access_token():
        return {"token": ""}

    @app.route("/api/v4/projects/<path:project_path>/repository/branches")
    def branches(project_path):
        repo = get_repo(project_path)
        if not repo:
            return "Not found", 404
        return jsonify(
            # TODO
            [{"name": repo.active_branch, "commit": {"id": repo.revision}}]
        )

    # XXX delete
    @app.route("/api/v4/projects/")
    def empty_project():
        return "Not found", 404

    # XXX delete
    @app.route("/api/v4/projects/<path:project_path>")
    def project(project_path):
        return {"name": os.path.basename(project_path)}

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
            return serve_document(path, localenv)

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
