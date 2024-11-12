# Copyright (c) 2024 OneCommons Co
# SPDX-License-Identifier: MIT
import glob
import os
from typing import Any, Iterator, List, Literal, Optional, Union
import shutil
import tarfile
import urllib.request
import urllib.parse

from ..packages import is_semver_compatible_with

from ..to_json import get_project_path

from ..logs import getLogger

from ..repo import GitRepo

from .serve import app, get_project_url
from ..localenv import LocalEnv
from ..util import UnfurlError, unique_name
from .gui_variables import set_variables, yield_variables

from flask import request, Response, jsonify, send_from_directory, make_response
from jinja2 import Environment, FileSystemLoader
import requests
import re
from urllib.parse import urlparse
from urllib.error import HTTPError
import git

TAG = "v0.1.0-alpha.2"
RELEASE_URL = f"https://github.com/onecommons/unfurl-gui/releases/download/{TAG}/unfurl-gui-dist.tar.gz"
DIST_DIR = ".cache/unfurl_gui"
TAG_FILE = "RELEASE.txt"

__doc__ = f"""
Running ``unfurl serve --gui /path/to/your/project`` will start Unfurl's built-in web server (at http://127.0.0.1:8081 by default, see :cli:`unfurl serve<unfurl-serve>` for more options).

When the server starts it checks for the web application's files at
``{DIST_DIR}`` in your unfurl home project if there is one set or in the current project.

If that directory is missing or the web application version there isn't compatible with the version required by your unfurl (currently "{TAG}"), the web application is downloaded from ``{RELEASE_URL}`` to ``{DIST_DIR}``.

You can set an alternative download URL with the ``UNFURL_GUI_DIST_URL`` environment variable or set it to "skip" to skip downloading a release. If a version tag is embedded in that URL then the local download needs to exactly match that version otherwise a semantic version compatibility check is made.
You can also set an alternative download location with the ``UNFURL_GUI_DIST_DIR`` environment variable.

For development, you can instead set the ``UNFURL_GUI_DIR`` environment variable to point 
to your local clone of the `unfurl-gui <https://github.com/onecommons/unfurl-gui>`_ repository.
You'll need to either build the release distribution with  ``yarn build`` or run ``yarn serve`` there and set the ``UNFURL_GUI_WEBPACK_ORIGIN`` environment variable to its URL.
"""

logger = getLogger("unfurl.gui")

release_url_pattern = r".+/(v[0-9A-Za-z.\-]+)/.*tar.gz"

_local_dir = os.path.dirname(os.path.abspath(__file__))
_template_env = Environment(
    loader=FileSystemLoader(os.path.join(_local_dir, "templates"))
)
blueprint_template = _template_env.get_template("project.j2.html")
dashboard_template = _template_env.get_template("dashboard.j2.html")


def get_project_readme(repo: GitRepo) -> str:
    for path in glob.glob(os.path.join(repo.working_dir, "[Rr][Ee][Aa][Dd][Mm][Ee].*")):
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


def get_head_for_webpack(index_path: str) -> str:
    return f"""
        <head>
          {get_head_contents(index_path)}

          <script defer src="/js/chunk-vendors.js"></script>
          <script defer src="/js/chunk-common.js"></script>
          <script defer src="/js/project.js"></script>
        </head>
        """


def serve_document(
    path, localenv: LocalEnv, webpack_origin: str, public_files_dir: str
):
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
        return send_from_directory(public_files_dir, "404.html")
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
        html_src_file = "project.html"
    else:
        template = dashboard_template
        html_src_file = "dashboard.html"
    if webpack_origin:
        head = get_head_for_webpack(os.path.join(public_files_dir, "index.html"))
    else:
        head = f"<head>{get_head_contents(os.path.join(public_files_dir, html_src_file))}</head>"

    return template.render(
        name=project_name,
        readme=get_project_readme(repo),
        user=user,
        origin=origin,
        head=head,
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
    excluded_headers = (
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "trailers",
        "upgrade",
    )  # NOTE we here exclude all hop-by-hop response headers defined by RFC 2616 section 13.5.1 ref. https://www.rfc-editor.org/rfc/rfc2616#section-13.5.1
    headers = [
        (k, v) for k, v in res.raw.headers.items() if k.lower() not in excluded_headers
    ]
    return Response(res.content, res.status_code, headers)


def _get_repo(project_path: str, localenv: LocalEnv, branch=None) -> Optional[GitRepo]:
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
        repo_info = localenv.find_path_in_repos(project_path[len("local:") :])
        if repo_info[0]:
            return repo_info[0]
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
        logger.warning("could not find or clone %s", url, exc_info=True)
        repo_view = None

    if not repo_view or not repo_view.repo:
        logger.warning("could not find or clone %s", url)
    return repo_view and repo_view.repo or None


def fetch_release(dist_dir, release_url, release_tag, exact_match):
    tag_file = os.path.join(dist_dir, TAG_FILE)
    msg = ""
    if release_tag:
        if os.path.exists(tag_file):
            logger.debug(f"Checking {tag_file} for '{release_tag}'")
            with open(tag_file, "r") as f:
                found_tag = f.read().strip()
            if found_tag == release_tag or (
                not exact_match and is_semver_compatible_with(release_tag, found_tag)
            ):
                logger.info(f"Found local unfurl_gui release {found_tag}")
                return
            else:
                msg = f"'{found_tag}' is not compatible with '{release_tag}'"
        else:
            msg = "unfurl_gui distribution not found"
        if msg:
            logger.info(msg)

    if os.path.exists(dist_dir):
        parent, dirname = os.path.split(dist_dir)
        new_name = unique_name(dirname, os.listdir(parent))
        new_path = os.path.join(parent, new_name)
        logger.info("Moving existing dist directory to %s", new_path)
        os.rename(dist_dir, new_path)

    tar_path = os.path.join(dist_dir, "unfurl-gui-dist.tar.gz")
    logger.debug(f"Downloading {release_url} to {tar_path}")
    os.makedirs(dist_dir, exist_ok=True)
    try:
        urllib.request.urlretrieve(release_url, tar_path)
    except HTTPError as e:
        # e.g. HTTP Error 404: Not Found
        logger.error(f"Unable to download {release_url}: {e}")
    else:
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(path=dist_dir)

        logger.info(f"Extracted unfurl_gui release {release_url} to {dist_dir}")
        os.remove(tar_path)


def create_routes(localenv: LocalEnv):
    app.config["UNFURL_GUI_MODE"] = localenv
    localrepo = (
        localenv.project
        and localenv.project.project_repoview
        and localenv.project.project_repoview.repo
    )
    assert localrepo

    development_mode = os.getenv("UNFURL_GUI_DIR") or os.getenv(
        "UNFURL_GUI_WEBPACK_ORIGIN"
    )
    if development_mode:
        ufgui_dir = os.getenv("UNFURL_GUI_DIR", ".")
        logger.info(
            "Development mode detected, not downloading compiled assets, using %s instead.",
            ufgui_dir,
        )
        # (development only) webpack serve origin - `yarn serve` in unfurl_gui would use http://localhost:8080 by default
        webpack_origin = os.getenv("UNFURL_GUI_WEBPACK_ORIGIN", "")
        dist_dir = os.path.join(ufgui_dir, "dist")
        if webpack_origin:
            public_files_dir = os.path.join(ufgui_dir, "public")
        else:
            public_files_dir = dist_dir
    else:
        webpack_origin = ""
        download_dir = os.getenv("UNFURL_GUI_DIST_DIR", DIST_DIR)
        if not os.path.isabs(download_dir):
            home_project = localenv.homeProject or localenv.project
            assert home_project
            download_dir = os.path.join(home_project.projectRoot, download_dir)
        dist_dir = os.path.join(download_dir, "dist")
        public_files_dir = dist_dir
        release_url = os.getenv("UNFURL_GUI_DIST_URL")
        tag = TAG

        if release_url == "skip":
            logger.info("Skipping download check for unfurl_gui release.")
        else:
            exact = False
            if release_url:
                tag_match = re.match(release_url_pattern, release_url)
                if tag_match:
                    exact = True
                    tag = tag_match.group(1)
            else:
                release_url = RELEASE_URL
            # XXX search for latest compatible release with https://api.github.com/repos/onecommons/unfurl-gui/releases tag_name assets[0][browser_download_url]
            fetch_release(download_dir, release_url, tag, exact)

    def get_repo(project_path: str, branch=None):
        return _get_repo(project_path, localenv, branch)

    def notfound_response(projectPath):
        # 404 page is not currently a template, but could become one
        return send_from_directory(public_files_dir, "404.html")

    @app.route("/<path:project_path>/-/variables", methods=["GET"])
    def get_variables(project_path):
        repo = get_repo(project_path)
        if not repo or repo.repo != localrepo.repo:
            return notfound_response(project_path)
        return {"variables": list(yield_variables(localenv))}

    @app.route("/<path:project_path>/-/variables", methods=["PATCH"])
    def patch_variables(project_path):
        repo = get_repo(project_path)
        if not repo or repo.repo != localrepo.repo:
            return notfound_response(project_path)

        body = request.json
        if isinstance(body, dict) and "variables_attributes" in body:
            set_variables(localenv, body["variables_attributes"])
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
            [{"name": repo.active_branch, "commit": {"id": repo.revision}}]
        )

    @app.route("/api/v4/<api>")
    def unsupported_api(api):
        return "Bad Request", 400

    @app.route("/<path:project_path>/-/raw/<branch>/<path:file>")
    def local_file(project_path, branch, file):
        repo = get_repo(project_path, branch)
        if repo:
            full_path = os.path.join(repo.working_dir, file)
            if os.path.exists(full_path):
                return send_from_directory(repo.working_dir, file)
        return notfound_response(project_path)

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve_path(path):
        if "accept" in request.headers and "text/html" in request.headers["accept"]:
            return serve_document(path, localenv, webpack_origin, public_files_dir)

        if request.headers.get("sec-fetch-dest") == "iframe":
            return "Bad Request", 400

        if webpack_origin:
            url = urllib.parse.urljoin(webpack_origin, path)
            qs = request.query_string.decode("utf-8")
            if qs != "":
                url += "?" + qs
            return proxy_webpack(url)
        else:
            assert path and path[0] != "/"
            local_path = os.path.join(dist_dir, path)
            if os.path.isfile(local_path):
                response = make_response(send_from_directory(dist_dir, path))
                if not development_mode:
                    response.headers["Cache-Control"] = (
                        "public, max-age=31536000"  # 1 year
                    )
                return response
            return serve_document(path, localenv, webpack_origin, public_files_dir)
