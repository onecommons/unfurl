# Copyright (c) 2024 OneCommons Co
# SPDX-License-Identifier: MIT
import glob
import os
from typing import Any, Dict, Iterator, List, Literal, Optional, Tuple, Union
import shutil
import tarfile
import urllib.request
import urllib.parse
import datetime
import time

from ..packages import is_semver_compatible_with, resolve_package

from ..to_json import get_project_path

from ..logs import getLogger

from ..repo import GitRepo, RepoView, normalize_git_url, Repo, split_git_url

from .serve import app, get_project_url
from ..localenv import LocalEnv
from ..util import UnfurlError, unique_name
from .gui_variables import set_variables, yield_variables

from flask import request, Response, jsonify, send_from_directory, make_response
from jinja2 import Environment, FileSystemLoader
from markupsafe import escape
import requests
import re
from urllib.parse import urlparse
from urllib.error import HTTPError
import git

TAG = "v0.1.0-alpha.2"
RELEASE_URL = f"https://github.com/onecommons/unfurl-gui/releases/download/{TAG}/unfurl-gui-dist.tar.gz"
DIST_DIR = ".cache/unfurl_gui"
TAG_FILE = "dist/RELEASE.txt"
# URL prefixes that only ever contain build output, never project paths
# Build output, never project paths. The directories webpack emits are fixed by
# unfurl-gui's vue.config.js; the rest is whatever public/ ships, enumerated at
# startup so a new directory there (oc/, and whatever comes next) is covered
# without being listed here -- a hand-written list silently turned /oc/assets
# font requests into git clone attempts.
#
# This is a prefix test rather than a file-existence test because in webpack dev
# mode there is no populated dist/ to stat; those requests are proxied instead.
_WEBPACK_OUTPUT_DIRS = ("js/", "css/", "img/", "media/", "fonts/", "fixtures/")


def _static_prefixes(public_files_dir: str) -> Tuple[str, ...]:
    shipped = set()
    if os.path.isdir(public_files_dir):
        shipped = {
            entry + "/"
            for entry in os.listdir(public_files_dir)
            if os.path.isdir(os.path.join(public_files_dir, entry))
        }
    return tuple(sorted(shipped.union(_WEBPACK_OUTPUT_DIRS)))


# A project path has to look like one before it is worth a network round trip.
# These are GitLab's own rules (lib/gitlab/path_regex.rb): a segment starts with
# an alphanumeric, underscore or dot, may contain hyphens, ends alphanumeric,
# underscore or hyphen, and may not end in .git or .atom. A project always lives
# in a namespace, so there are at least two segments.
_PATH_SEGMENT_RE = re.compile(
    r"[a-zA-Z0-9_.][a-zA-Z0-9_\-.]{0,253}[a-zA-Z0-9_\-]|[a-zA-Z0-9_]"
)

# TOP_LEVEL_ROUTES in that same file -- the ones a stray request is most likely
# to ask for. A namespace can never be called these, so they cannot be projects.
_RESERVED_FIRST_SEGMENT = frozenset((
    "-",
    "__",
    "admin",
    "api",
    "assets",
    "dashboard",
    "explore",
    "groups",
    "health_check",
    "help",
    "import",
    "login",
    "oauth",
    "profile",
    "projects",
    "public",
    "s",
    "search",
    "snippets",
    "uploads",
    "users",
    "v2",
))


# https://developer.mozilla.org/docs/Web/HTTP/Reference/Headers/Sec-Fetch-Dest
_SUBRESOURCE_FETCH_DESTS = frozenset((
    "audio",
    "embed",
    "font",
    "image",
    "manifest",
    "object",
    "paintworklet",
    "report",
    "script",
    "style",
    "track",
    "video",
    "worker",
    "xslt",
))


def looks_like_project_path(path: str) -> bool:
    """Cheap structural check, so junk never reaches a clone attempt."""
    segments = path.strip("/").split("/")
    if len(segments) < 2 or segments[0] in _RESERVED_FIRST_SEGMENT:
        return False
    if path.startswith("remote:"):
        return True
    return all(
        _PATH_SEGMENT_RE.fullmatch(segment) and not segment.endswith((".git", ".atom"))
        for segment in segments
    )


# Remember paths that did not resolve, so a scanner -- or a mistyped URL someone
# reloads -- pays the clone attempt once rather than on every request.
#
# The entry can be long lived because it never hides a project that exists: a
# cached miss falls back to _get_local_repo, which still finds anything cloned
# since. All it delays is re-attempting a clone for a path that really was not
# there, so the cost of being wrong is one stale 404 rather than a broken page.
MISSING_PROJECT_TTL = 3600.0  # an hour
_missing_projects: Dict[str, float] = {}


def _is_known_missing(project_path: str) -> bool:
    expires = _missing_projects.get(project_path)
    if expires is None:
        return False
    if expires <= time.monotonic():
        del _missing_projects[project_path]
        return False
    return True


def _remember_missing(project_path: str) -> None:
    _missing_projects[project_path] = time.monotonic() + MISSING_PROJECT_TTL


CLOUD_PAGE = "public_cloud.html"
# Where the cloud map page fetches its data from. Relative to this server, which
# in gui mode is the origin the page itself was served from.
CLOUDMAP_URL = (
    "/types?auth_project=onecommons%2Fstd"
    "&cloudmap=onecommons/cloudmap&file=dummy-ensemble.yaml"
)

__doc__ = f"""
Running ``unfurl serve --gui /path/to/your/project`` will start Unfurl's built-in web server (at http://127.0.0.1:8081 by default, see :cli:`unfurl serve<unfurl-serve>` for more options).

When the server starts it checks for the web application's files at
``{DIST_DIR}`` in your unfurl home project if there is one set or in the current project.

If that directory is missing or the web application version there isn't compatible with the version required by your local unfurl (currently "{TAG}"), the web application is downloaded from ``{RELEASE_URL}`` to ``{DIST_DIR}``.

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
cloud_template = _template_env.get_template("cloud.j2.html")


def get_project_readme(repo: Repo) -> str:
    for path in glob.glob(os.path.join(repo.working_dir, "[Rr][Ee][Aa][Dd][Mm][Ee].*")):
        with open(path, "r") as file:
            return file.read()
    return ""


def _inner_html(tag: str, contents: str) -> str:
    match = re.search(rf"<{tag}.*?>(.*?)</{tag}>", contents, re.DOTALL)
    return match.group(1) if match else ""


def get_head_contents(f) -> str:
    with open(f, "r") as file:
        return _inner_html("head", file.read())


def get_head_for_webpack(index_path: str) -> str:
    return f"""
        <head>
          {get_head_contents(index_path)}

          <script defer src="/js/chunk-vendors.js"></script>
          <script defer src="/js/chunk-common.js"></script>
          <script defer src="/js/project.js"></script>
        </head>
        """


def render_cloud_page(html: str) -> str:
    """Point the cloud map page at this server's ``/types`` endpoint.

    ``div#chart``'s ``data-cloudmap`` attribute is the hook gitlab fills in
    server-side when it renders ``public_cloud/index.html.haml``; here the page
    is a static build artifact so we do the same substitution on its markup.
    It has to happen server-side: the page bundle reads the attribute while it
    initializes, before any script we could add to the document would run.
    """
    cloudmap_url = os.getenv("UNFURL_GUI_CLOUDMAP_URL", CLOUDMAP_URL)
    marker = '<div id="chart">'
    if marker not in html:
        logger.warning(
            "Could not find '%s' in %s, the cloud map will fall back to its "
            "built-in URL and most likely fail to load.",
            marker,
            CLOUD_PAGE,
        )
        return html
    return html.replace(
        marker, f'<div id="chart" data-cloudmap="{escape(cloudmap_url)}">', 1
    )


def notfound_page(public_files_dir: str) -> Response:
    """The web application's 404 page, served with a 404 status.

    send_from_directory() would send it as a 200, leaving the browser -- and
    anything scripted against these routes -- with no signal that the request
    failed: the page says 404 but the response claims success.
    """
    response = make_response(send_from_directory(public_files_dir, "404.html"))
    response.status_code = 404
    return response


def _is_dashboard(localenv: LocalEnv) -> bool:
    return bool(
        localenv.manifestPath and localenv.overrides.get("format") != "blueprint"
    )


def serve_project_page(
    path, localenv: LocalEnv, webpack_origin: str, public_files_dir: str
):
    assert localenv.project
    localrepo = localenv.project.project_repoview.repo
    assert localrepo

    localrepo_is_dashboard = _is_dashboard(localenv)

    home_project = _get_project_path(localrepo) if localrepo_is_dashboard else None

    parsed = None
    user = ""
    origin = ""
    if localrepo_is_dashboard and localrepo.url:
        parsed = urlparse(normalize_git_url(localrepo.url))
        if parsed.netloc:  # skip file: urls (no hostname => no usable origin)
            user = parsed.username or ""
            origin = f"{parsed.scheme}://{parsed.hostname}"

    server_fragment = re.split(r"/?(deployment-drafts|-)(?=/)", path)
    projectPath = server_fragment[0].lstrip("/")
    # _get_repo clones when it can't find the project locally, so guard it:
    # anything that isn't shaped like a project path, or that we already failed
    # to find recently, is answered without touching the network.
    if len(server_fragment) == 1 and (
        not looks_like_project_path(projectPath) or _is_known_missing(projectPath)
    ):
        repo = _get_local_repo(projectPath, localenv)
    else:
        repo = _get_repo(projectPath, localenv)
        if repo is None:
            _remember_missing(projectPath)

    if not repo:
        return notfound_page(public_files_dir)
    # assume serving dashboard unless an /-/overview url
    if (
        "-/overview" in path
        or repo.working_dir != localrepo.working_dir
        or not localrepo_is_dashboard
    ):
        template = blueprint_template
        html_src_file = "project.html"
    else:
        template = dashboard_template
        html_src_file = "dashboard.html"

    project_path = _get_project_path(repo)
    project_name = os.path.basename(project_path)

    if webpack_origin:
        head = get_head_for_webpack(os.path.join(public_files_dir, "index.html"))
    else:
        head = f"<head>{get_head_contents(os.path.join(public_files_dir, html_src_file))}</head>"

    return template.render(
        nav="dashboard",
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


def _get_project_path(repo: Repo):
    return get_project_path(repo, urlparse(app.config["UNFURL_CLOUD_SERVER"]).hostname)


def proxy_request(url: str) -> Response:
    logger.trace(f"Proxying request to webpack dev server: {url}")
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


def _get_local_repo(
    project_path: str, localenv: LocalEnv, branch=None
) -> Optional[Repo]:
    if not project_path or project_path == "local:":
        return localenv.project.project_repoview.repo if localenv.project else None

    local_projects = app.config["UNFURL_LOCAL_PROJECTS"]
    # try with and without trailing slash since project_path might have one but local_projects keys don't
    lookup_keys = (project_path.rstrip("/"), project_path)
    for key in lookup_keys:
        if key in local_projects:
            working_dir = local_projects[key]
            if localenv.project:
                repo_view = localenv.project.workingDirs.get(working_dir)
                if repo_view and repo_view.repo:
                    return repo_view.repo
            return Repo.make_repo(working_dir)
    if "/" not in project_path and ":" not in project_path:
        return None
    if project_path[-1] != "/":
        project_path += "/"

    if project_path.startswith("local:"):
        # it's not a cloud server project
        repo_info = localenv.find_path_in_repos(project_path[len("local:") :])
        if repo_info.repo_view and repo_info.repo_view.repo:
            return repo_info.repo_view.repo
        logger.error(f"Can't find project {project_path} in {list(local_projects)}")
        return None

    project_path = project_path.rstrip("/")
    assert localenv.project
    localrepo = localenv.project.project_repoview.repo
    if localrepo and (project_path == localrepo.project_path()):
        return localrepo
    return None


def _find_or_clone_no_ensemble(url: str, localenv: LocalEnv) -> Optional[Repo]:
    """Find or clone ``url`` for a project that has no ensemble manifests.

    :py:meth:`Manifest.find_or_clone_from_url` builds its import resolver from
    a manifest, so a project with no ensemble can't go through it -- but
    package rules still have to apply, or a rewritten package id resolves to
    the wrong remote. Resolve them against the environment context instead,
    which is the only thing ``find_or_clone_from_url`` was wanted for here.
    """
    repo = localenv.find_repo(split_git_url(url)[0])
    if repo:
        return repo

    from .cache import get_remote_refs_cached

    def get_remote_tags(tag_url: str, pattern: str) -> List[str]:
        # No credentials, matching the manifest path: this localenv has no
        # `make_resolver`, so `find_or_clone_from_url` resolves through a
        # SimpleCacheResolver, which takes credentials from the local repo's
        # own remote url rather than from the request.
        return get_remote_refs_cached(tag_url, pattern, None).tags

    repo_view = RepoView(dict(name="", url=url), None)
    _, package_specs = localenv.get_repositories_and_package_specs()
    # A fresh `packages` dict: this resolves one url, where an import loader
    # shares one across a whole load so repeated references reuse a package.
    resolve_package(repo_view, {}, package_specs, get_remote_tags)
    # resolve_package() rewrote repo_view's url and revision to the package's
    # if it matched a rule, so read them back off the view.
    repo, _, _ = localenv.find_or_create_working_dir(
        split_git_url(repo_view.url)[0],
        repo_view.revision_tag or None,
        package=repo_view.package or None,
    )
    return repo


def _get_repo(project_path: str, localenv: LocalEnv, branch=None) -> Optional[Repo]:
    repo = _get_local_repo(project_path, localenv, branch)
    if repo:
        return repo

    # not found, so clone repo using import loader machinery
    # (to apply package rules and deduce branch from lock section or remote tags)
    if project_path.startswith("remote:"):
        # TBD: set in to_json.get_blueprint_from_topology()
        url = project_path[len("remote:") :]
    else:
        url = get_project_url(project_path, branch=branch)
    # XXX this will always use the default deployment
    # this might be a problem we weren't explicitly passed the branch/revision used by a different deployment
    try:
        if not localenv.manifestPath:
            repo = _find_or_clone_no_ensemble(url, localenv)
        else:
            repo_view = localenv.get_manifest(
                skip_validation=True
            ).find_or_clone_from_url(url)
            repo = repo_view.repo if repo_view else None
    except UnfurlError:  # we probably want to treat clone errors as not found
        logger.warning("could not find or clone %s", url, exc_info=True)
        repo = None

    if not repo:
        logger.warning("could not find or clone %s", url)
    else:
        app.config["UNFURL_LOCAL_PROJECTS"][project_path] = repo.working_dir
    return repo


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
            msg = f"unfurl_gui distribution not found in {tag_file}"
        if msg:
            logger.info(msg)

    if os.path.exists(dist_dir):
        if not os.path.isdir(dist_dir):
            raise UnfurlError(
                f"{dist_dir} is not an unfurl_gui download directory "
                "Set UNFURL_GUI_DIST_DIR to an empty or managed path, or set "
                "UNFURL_GUI_DIR to a local unfurl-gui clone to serve it directly."
            )

        if any(os.scandir(dist_dir)):  # not an empty directory
            if os.getenv("UNFURL_GUI_DIST_DIR"):
                raise UnfurlError(
                    f"{dist_dir} is not an empty unfurl_gui download directory "
                    "Set UNFURL_GUI_DIST_DIR to an empty path, or set "
                    "UNFURL_GUI_DIR to a local unfurl-gui clone to serve it directly."
                )

            # This is our managed cache dir, backup the existing non-empty dist directory
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
    # don't try to pull if stale
    app.config["CACHE_DEFAULT_PULL_TIMEOUT"] = -1
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
        ufgui_dir = os.getenv("UNFURL_GUI_DIR")
        if not ufgui_dir:
            raise UnfurlError(
                "UNFURL_GUI_DIR must be set if UNFURL_GUI_WEBPACK_ORIGIN is set."
            )
        else:
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

    # after both branches, so it sees the directory this mode actually serves
    static_prefixes = _static_prefixes(public_files_dir)
    logger.debug("serving these paths as static files: %s", static_prefixes)

    def get_repo(project_path: str, branch=None) -> Optional[Repo]:
        return _get_repo(project_path, localenv, branch)

    # Paths every crawler and browser asks for and this server does not serve.
    # Answering them here keeps them out of serve_path, where each one would
    # otherwise be treated as a project path.
    @app.route("/.well-known/<path:path>")
    @app.route("/robots.txt")
    @app.route("/sitemap.xml")
    @app.route("/sitemap.xml.gz")
    @app.route("/favicon.ico")
    @app.route("/favicon.png")
    @app.route("/apple-touch-icon.png")
    @app.route("/apple-touch-icon-precomposed.png")
    @app.route("/browserconfig.xml")
    @app.route("/manifest.json")
    def notfound_response(path=None):
        # 404 page is not currently a template, but could become one
        return notfound_page(public_files_dir)

    @app.route(
        "/api/v4/projects//-/variables",
        defaults={"project_path": ""},
        merge_slashes=False,
        methods=["GET"],
    )
    @app.route("/<path:project_path>/-/variables", methods=["GET"])
    def get_variables(project_path):
        repo = get_repo(project_path)
        if not repo or repo.working_dir != localrepo.working_dir:
            return notfound_response(project_path)
        return {"variables": list(yield_variables(localenv))}

    @app.route(
        "/api/v4/projects//-/variables",
        defaults={"project_path": ""},
        merge_slashes=False,
        methods=["PATCH"],
    )
    @app.route("/<path:project_path>/-/variables", methods=["PATCH"])
    def patch_variables(project_path):
        repo = get_repo(project_path)
        if not repo or repo.working_dir != localrepo.working_dir:
            return notfound_response(project_path)

        body = request.json
        if isinstance(body, dict) and "variables_attributes" in body:
            set_variables(localenv, body["variables_attributes"])
            return {"variables": list(yield_variables(localenv))}
        else:
            return "Bad Request", 400

    @app.route(
        "/api/v4/projects//repository/branches",
        defaults={"project_path": ""},
        merge_slashes=False,
    )
    @app.route("/api/v4/projects/<path:project_path>/repository/branches")
    def branches(project_path):
        repo = get_repo(project_path)
        if not repo:
            return notfound_response(project_path)
        if isinstance(repo, GitRepo):
            branch = repo.active_branch
        else:
            branch = "main"  # XXX
        # ISO 8601 e.g. "2012-06-28T03:44:20-07:00"
        committed_date = datetime.datetime.fromtimestamp(
            repo.revision_time, tz=datetime.timezone.utc
        ).isoformat()
        commit_id = repo.revision
        if commit_id:
            if repo.is_dirty():
                commit_id += "-dirty"
            elif (
                project := localenv.project
            ) and repo is project.project_repoview.repo:
                # Pick up ensemble entries create after startup, e.g. added by /create_ensemble
                project.localConfig.reload_if_changed()
                for tpl in project.localConfig.ensembles:
                    if not tpl.get("file"):
                        continue
                    working_dir = os.path.join(
                        project.projectRoot, os.path.dirname(tpl["file"])
                    )
                    repo_view = project.workingDirs.get(working_dir)
                    if repo_view:
                        if (
                            repo_view is not project.project_repoview
                            and repo_view.is_dirty()
                        ):
                            commit_id += "-dirty"
                            break
                    elif os.path.isdir(os.path.join(working_dir, ".git")):
                        # workingDirs is the startup snapshot, so deployments
                        # created since don't have a RepoView. Check the
                        # filesystem directly.
                        if git.Repo(working_dir).is_dirty():
                            commit_id += "-dirty"
                            break
        logger.debug("/branches %s -> %s", project_path, commit_id)
        entry: Dict[str, Any] = {
            "name": branch,
            "commit": {
                "id": commit_id,
                "committed_date": committed_date,
                "created_at": committed_date,
            },
        }
        # only stated when known to be the default: `default_branch` is ""
        # for a clone that records no origin/HEAD (see GitRepo.default_branch)
        if branch and isinstance(repo, GitRepo) and repo.default_branch == branch:
            entry["default"] = True
        return jsonify([entry])

    @app.route("/api/v4/<api>")
    def unsupported_api(api):
        return "Bad Request", 400

    @app.route(
        "//-/raw/<branch>/<path:file>",
        defaults={"project_path": ""},
        merge_slashes=False,
        methods=["GET"],
    )
    @app.route("/<path:project_path>/-/raw/<branch>/<path:file>")
    def local_file(project_path, branch, file):
        repo = get_repo(project_path, branch)
        if repo:
            full_path = os.path.join(repo.working_dir, file)
            if os.path.exists(full_path):
                return send_from_directory(repo.working_dir, file)
        return notfound_response(project_path)

    @app.route("/cloud", strict_slashes=False)
    def public_cloud():
        if webpack_origin:
            response = proxy_request(urllib.parse.urljoin(webpack_origin, CLOUD_PAGE))
            if response.status_code != 200:
                return response  # an error page is not markup to lift from
            html = response.get_data(as_text=True)
        else:
            html_path = os.path.join(public_files_dir, CLOUD_PAGE)
            if not os.path.isfile(html_path):
                logger.error(
                    "%s not found, this unfurl-gui distribution predates the cloud map page.",
                    html_path,
                )
                return notfound_response("cloud")
            with open(html_path) as f:
                html = f.read()

        # The built page is a whole document. Re-serving it as-is left /cloud
        # with no header and so no way back to the dashboard, so lift its head
        # and body into the skeleton instead; the markup stays unfurl-gui's.
        html = render_cloud_page(html)
        localrepo = localenv.project.project_repoview.repo if localenv.project else None
        home_project = (
            _get_project_path(localrepo)
            if localrepo and _is_dashboard(localenv)
            else None
        )
        return cloud_template.render(
            nav="cloud",
            name="Cloud",
            head=f"<head>{_inner_html('head', html)}</head>",
            body=_inner_html("body", html),
            home_project=home_project,
            working_dir_project=home_project or "",
            user="",
            origin="",
        )

    def _serve_static_file(path):
        if webpack_origin:
            url = urllib.parse.urljoin(webpack_origin, path)
            qs = request.query_string.decode("utf-8")
            if qs != "":
                url += "?" + qs
            return proxy_request(url)

        assert path and path[0] != "/"
        local_path = os.path.join(dist_dir, path)
        if os.path.isfile(local_path):
            response = make_response(send_from_directory(dist_dir, path))
            # Content-hashed webpack assets are immutable — safe to
            # cache forever even in development mode. Otherwise a
            # reload downloads ~80 chunks.
            #
            # Webpack outputs hashes in two formats:
            #   - `.<hex8+>.<ext>`: JS/CSS/sourcemap chunks, e.g.
            #     `3124.39cada19.js`, `app.1f2e3d4c.css`
            #   - `<hash>.<ext>`: font/image assets where the whole
            #     basename is the hash, e.g.
            #     `o-0NIpQlx3QUlC5A4PNjXhFVZNyB.woff2` (base64-ish,
            #     20+ chars).
            basename = os.path.basename(path)
            has_content_hash = bool(
                re.search(r"\.[0-9a-f]{8,}\.[^.]+$", basename)
                or re.match(r"^[A-Za-z0-9_-]{20,}\.[^.]+$", basename)
            )
            if not development_mode or has_content_hash:
                response.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"  # 1 year
                )
            return response
        else:
            logger.debug("no static file at %s", local_path)
            return "Not Found", 404

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve_path(path):
        # Refuse to be framed by another site. A same-origin frame is not a
        # framing attempt -- Cypress runs the app in one, and checking dest
        # alone answered every one of its page loads with a 400.
        if request.headers.get("sec-fetch-dest") == "iframe" and request.headers.get(
            "sec-fetch-site"
        ) not in ("same-origin", "same-site", "none"):
            return "Bad Request", 400

        is_static = path.startswith(static_prefixes)
        # A browser navigates to a project page; it never fetches one as a font,
        # stylesheet or script. Sec-Fetch-Dest says which.
        fetch_dest = request.headers.get("Sec-Fetch-Dest")
        is_subresource = bool(fetch_dest) and fetch_dest in _SUBRESOURCE_FETCH_DESTS
        if is_static or is_subresource:
            return _serve_static_file(path)

        return serve_project_page(path, localenv, webpack_origin, public_files_dir)
