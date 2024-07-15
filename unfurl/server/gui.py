"""
UI for local Unfurl project

"project_path" is used by serve for export and patch
"""

import os
from typing import Any, Iterator, List, Literal, Optional, Union
import shutil
import tarfile
import urllib.request

from ..to_json import get_project_path

from ..logs import getLogger

from ..repo import GitRepo

from .serve import app, get_project_url
from ..localenv import LocalEnv
from ..util import UnfurlError
from .gui_variables import set_variables, yield_variables

from flask import request, Response, jsonify, send_file, make_response
from jinja2 import Environment, FileSystemLoader
import requests
import re
from urllib.parse import urlparse
import git

logger = getLogger("unfurl.gui")

TAG = "v0.1.0.alpha.1"
RELEASE = os.getenv(
    "UNFURL_GUI_DIST",
    f"https://github.com/onecommons/unfurl-gui/releases/download/{TAG}/unfurl-gui-dist.tar.gz",
)

local_dir = os.path.dirname(os.path.abspath(__file__))

# (development only) should be set to the unfurl_gui directory
UFGUI_DIR = os.getenv("UNFURL_GUI_DIR", local_dir)
# (development only) webpack serve origin - `yarn serve` in unfurl_gui would use http://localhost:8080 by default
WEBPACK_ORIGIN = os.getenv("UNFURL_GUI_WEBPACK_ORIGIN")
DIST = os.path.join(UFGUI_DIR, "dist")
PUBLIC = os.path.join(UFGUI_DIR, "public")
UNFURL_SERVE_PATH = os.getenv("UNFURL_SERVE_PATH", "")
IMPLIED_DEVELOPMENT_MODE = (
    "UNFURL_GUI_DIR" in os.environ or "UNFURL_GUI_WEBPACK_ORIGIN" in os.environ
)

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


def get_project_head():
    if WEBPACK_ORIGIN:
        project_head = f"""
        <head>
          {get_head_contents(os.path.join(PUBLIC, "index.html"))}

          <script defer src="/js/chunk-vendors.js"></script>
          <script defer src="/js/chunk-common.js"></script>
          <script defer src="/js/project.js"></script>
        </head>
        """
    else:
        project_head = (
            f"<head>{get_head_contents(os.path.join(DIST, 'project.html'))}</head>"
        )
    return project_head


def get_dashboard_head():
    if WEBPACK_ORIGIN:
        dashboard_head = f"""
        <head>
          {get_head_contents(os.path.join(PUBLIC, "index.html"))}

          <script defer src="/js/chunk-vendors.js"></script>
          <script defer src="/js/chunk-common.js"></script>
          <script defer src="/js/dashboard.js"></script>
        </head>

        """
    else:
        dashboard_head = (
            f"<head>{get_head_contents(os.path.join(DIST, 'dashboard.html'))}</head>"
        )
    return dashboard_head


def notfound_response(projectPath):
    # 404 page is not currently a template, but could become one
    location = PUBLIC if WEBPACK_ORIGIN else DIST
    return send_file(os.path.join(location, "404.html"))


def serve_document(path, localenv: LocalEnv):
    assert localenv.project
    localrepo = localenv.project.project_repoview.repo
    assert localrepo

    localrepo_is_dashboard = bool(localenv.manifestPath)

    home_project = _get_project_path(localrepo) if localrepo_is_dashboard else None

    if localrepo_is_dashboard and localrepo.remote and localrepo.remote.url:
        parsed = urlparse(localrepo.remote.url)
        [user, _, *_] = re.split(r"[@:]", parsed.netloc)
        origin = f"{parsed.scheme}://{parsed.hostname}"
    else:
        parsed = None
        user = ""
        origin = None

    server_fragment = re.split(r"/?(deployment-drafts|-)(?=/)", path)
    projectPath = server_fragment[0].lstrip("/")
    repo = _get_repo(projectPath, localenv)

    if not repo:
        return notfound_response(projectPath)
    format = "environments"
    # assume serving dashboard unless an /-/overview url
    if (
        "-/overview" in path
        or repo.repo != localrepo.repo
        or not localrepo_is_dashboard
    ):
        format = "blueprint"

    project_path = _get_project_path(repo)
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
        head=(get_project_head() if format == "blueprint" else get_dashboard_head()),
        project_path=project_path,
        namespace=os.path.dirname(project_path),
        home_project=home_project,
        working_dir_project=home_project if localrepo_is_dashboard else project_path,
    )


def _get_project_path(repo: GitRepo):
    return get_project_path(repo, urlparse(app.config["UNFURL_CLOUD_SERVER"]).hostname)


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
    assert localenv.project
    localrepo = localenv.project.project_repoview.repo
    if localrepo and (project_path == localrepo.project_path()):
        return localrepo

    # not found, so clone repo using import loader machinery
    # (to apply package rules and deduce branch from lock section or remote tags)
    if project_path.startswith("remote:"):
        url = project_path[len("remote:") :]
    else:
        url = get_project_url(project_path, branch=branch)
    # XXX this will always use the default deployment
    # this might be a problem we weren't explicitly passed the branch/revision used by a different deployment
    try:
        repo_view = localenv.get_manifest().find_or_clone_from_url(url)
    except UnfurlError:  # we probably want to treat clone errors as not found
        repo_view = None

    if not repo_view or not repo_view.repo:
        logger.warning("could not find or clone %s", url)
    return repo_view and repo_view.repo or None


def fetch(download_dir):
    TAG_FILE = os.path.join(download_dir, "unfurl_gui", "current_tag.txt")
    dist_dir = os.path.join(download_dir, "unfurl_gui", "dist")

    logger.debug(f"Checking assets for '{TAG}'")
    if os.path.exists(TAG_FILE):
        with open(TAG_FILE, "r") as f:
            current_tag = f.read().strip()
        if current_tag == TAG and os.path.exists(dist_dir):
            logger.debug(f"'{TAG}' is up-to-date")
            return
        else:
            logger.debug(f"'{current_tag}' does not match the needed tag '{TAG}'")

    if os.path.exists(dist_dir):
        shutil.rmtree(dist_dir)
        logger.debug("Removed existing dist directory")

    logger.debug(f"Downloading {RELEASE}")
    os.makedirs(dist_dir, exist_ok=True)
    tar_path = os.path.join(dist_dir, "unfurl-gui-dist.tar.gz")
    urllib.request.urlretrieve(RELEASE, tar_path)

    with tarfile.open(tar_path, "r:gz") as tar:
        logger.debug(f"Extracting {RELEASE} to {dist_dir}")
        tar.extractall(path=os.path.dirname(dist_dir))

    os.remove(tar_path)
    logger.debug("Removed tarball file")

    with open(TAG_FILE, "w") as f:
        f.write(TAG)
    logger.debug(f"Updated tag file to '{TAG}'")


def create_routes(localenv: LocalEnv):
    app.config["UNFURL_GUI_MODE"] = localenv
    localrepo = (
        localenv.project
        and localenv.project.project_repoview
        and localenv.project.project_repoview.repo
    )
    assert localrepo

    if IMPLIED_DEVELOPMENT_MODE:
        logger.debug("Development mode detected, not downloading compiled assets.")
    else:
        home_project = localenv.homeProject or localenv.project
        assert home_project
        download_dir = os.path.join(home_project.projectRoot, ".cache")
        global DIST
        DIST = os.path.join(download_dir, "unfurl_gui", "dist")
        global PUBLIC
        PUBLIC = os.path.join(download_dir, "unfurl_gui", "public")
        fetch(download_dir)

    def get_repo(project_path, branch=None):
        return _get_repo(project_path, localenv, branch)

    @app.route("/<path:project_path>/-/variables", methods=["GET"])
    def get_variables(project_path):
        repo = get_repo(project_path)
        if not repo or repo.repo != localrepo.repo:
            return notfound_response(project_path)
        return {"variables": list(yield_variables(localenv))}

    @app.route("/<path:project_path>/-/variables", methods=["PATCH"])
    def patch_variables(project_path):
        nonlocal localenv
        repo = get_repo(project_path)
        if not repo or repo.repo != localrepo.repo:
            return notfound_response(project_path)

        body = request.json
        if isinstance(body, dict) and "variables_attributes" in body:
            localenv = set_variables(localenv, body["variables_attributes"])
            return {"variables": list(yield_variables(localenv))}
        else:
            return "Bad Request", 400

    @app.route("/api/v4/projects/<path:project_path>/repository/branches")
    def branches(project_path):
        repo = get_repo(project_path)
        if not repo:
            return notfound_response(project_path)
        return jsonify(
            # TODO
            [{"name": "HEAD", "commit": {"id": repo.revision}}]
        )

    @app.route("/<path:project_path>/-/raw/<branch>/<path:file>")
    def local_file(project_path, branch, file):
        repo = get_repo(project_path, branch)
        if repo:
            full_path = os.path.join(repo.working_dir, file)
            if os.path.exists(full_path):
                return send_file(full_path)
        return notfound_response(project_path)

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
            return "Bad Request", 400

        if WEBPACK_ORIGIN:
            return proxy_webpack(url)
        else:
            response = make_response(send_file(os.path.join(DIST, path)))
            if not IMPLIED_DEVELOPMENT_MODE:
                response.headers["Cache-Control"] = "public, max-age=31536000"  # 1 year
            return response
