# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
"""
API server for the unfurl front-end app that provides JSON representations of ensembles and TOSCA service templates
and a patch api for updating them.

The server manage local clones of remote git repositories and uses a in-memory or redis cache for efficient access.
"""

# Security assumptions:
# The server can read and write to private git repositories using credentials passed in HTTP requests from different users.
# So it is important that a http request can't be manipulated into accessing a cloned local git repository the initiator doesn't have access to.
# To wit, the following rules apply:
# * Export requests evaluate expressions in safe mode and limit file system access to the current project or a referenced repository (enforced by ``ImportResolver._has_path_escaped()``)
# * Patch requests never expressions and commits are pushed upstream only using transient credentials supplied with the request.
# * Request results maybe be cached whether private or public and retrieved without authorization because the cache key is always derived from the ``auth_project`` url parameter
# and assumption is that an upstream api proxy has already authorized the requestor access to that project.
# * But processing a request (loading the project) it may access content in other repositories and it is not assumed that the requestor has access to those repositories.
# (Just because a user can read a file with a reference to a repository doesn't imply they have access to the referenced repository.)
# When accessing referenced repositories or packages only files in public repositories are cached and the repositories are cloned into the shared "public" directory.
# But if access fails when attempting to clone the repository, the repository is accessed the using the standard project loader, which makes clone local to the project using the project repository's credentials (if on the same host) (see ``apply_url_credentials``), and the loaded files are not cached.

from dataclasses import dataclass, field
import gc
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import traceback
import signal
from typing import (
    Dict,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    Any,
    Union,
    TYPE_CHECKING,
    cast,
    Callable,
)
from typing_extensions import Literal
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit
from base64 import b64decode

from apiflask import APIFlask
from flask import Request, Response, current_app, jsonify, make_response, request
import flask.json
from flask.typing import ResponseReturnValue
from flask_caching import Cache
from flask_cors import CORS

import git
from git.objects import Commit

from ..graphql import (
    GraphqlObject,
    GraphqlObjectsByName,
    ImportDef,
    get_local_type,
    project_id_from_urlresult,
)
from .schemas import (
    BatchPatchBody,
    ClearProjectQuery,
    CloudMapDocQuery,
    CloudMapDocument,
    CloudMapDocumentPair,
    CloudMapQuery,
    CloudMapResponse,
    EmptyCacheQuery,
    ExportQuery,
    ExportResponse,
    PatchEnvironmentBody,
    PatchEnsembleBody,
    PatchResponse,
    PopulateCacheQuery,
    TypesQuery,
    EXPORT_RESPONSES,
    PATCH_RESPONSES,
    hoist_cloudmap_definitions,
)
from ..manifest import relabel_dict
from ..packages import Package, get_package_from_url, ProxiedRepo

from ..projectpaths import rmtree, Folders
from ..localenv import LocalEnv, Project
from ..repo import (
    GitRepo,
    Repo,
    RepoView,
    add_user_to_url,
    normalize_git_url,
    normalize_git_url_hard,
    sanitize_url,
)
from ..util import (
    UnfurlError,
    get_package_digest,
    is_relative_to,
    unique_name,
    assert_not_none,
)
from ..logs import Levels, get_console_log_level, getLogger, add_log_file
from ..yamlmanifest import YamlManifest
from .. import __version__, semver_prerelease, DefaultNames, DEFAULT_CLOUD_SERVER
from .. import to_json
from .. import init
from toscaparser.common.exception import FatalToscaImportError
from toscaparser.elements.entity_type import Namespace
import tosca

if TYPE_CHECKING:
    from cachelib.redis import RedisCache

__logfile = os.getenv("UNFURL_LOGFILE")
if __logfile:
    add_log_file(__logfile)
logger = getLogger("unfurl.server")


app = APIFlask(__name__, title="Unfurl Server API", version=__version__())


@app.spec_processor
def _hoist_cloudmap_defs(spec):
    """Lift CloudMap schema definitions into components.schemas so that
    the canonical cloudmap-schema.json definitions appear as named
    OpenAPI components and ``$ref`` arrows resolve.
    """
    return hoist_cloudmap_definitions(spec)


def configure_app(app: APIFlask = app) -> Cache:
    """
    Configure the Flask app and cache based on environment variables.
     - CACHE_TYPE: the type of cache to use (e.g. "simple", "redis")
     - CACHE_KEY_PREFIX: a prefix to add to all cache keys (default: "ufsv::")
     - CACHE_DEFAULT_TIMEOUT: default cache timeout in seconds (default: 0, which means never expire)
     - CACHE_REDIS_URL or CACHE_REDIS_HOST, CACHE_REDIS_PORT, etc. for RedisCache configuration
     - UNFURL_CLONE_ROOT: root directory for cloning git repositories (default: current directory)
     - UNFURL_CLOUD_SERVER: URL of the unfurl cloud server (default: https://unfurl.cloud)
     - UNFURL_SERVE_SECRET: optional secret for authenticating requests
     - UNFURL_SERVE_CORS: optional comma-separated list of allowed CORS origins (default: origin of UNFURL_CLOUD_SERVER)
     - CACHE_DEFAULT_PULL_TIMEOUT: default timeout in seconds for pulling git repositories when validating cache entries, -1: never pull, 0: always pull (default: 120)
     - CACHE_DEFAULT_REMOTE_TAGS_TIMEOUT: default timeout in seconds for fetching remote tags when validating package dependencies in cache entries (default: 300)
     - CACHE_CONTROL_SERVE_STALE: if set to a positive integer, allows serving stale cache entries while asynchronously refreshing them in the background if they are older than this many seconds (default: 0, which means don't serve stale entries)
    """
    # note: export FLASK_ENV=development to see error stacks
    # see https://flask-caching.readthedocs.io/en/latest/#built-in-cache-backends for more options
    if "CACHE_TYPE" in os.environ:
        cache_type = os.environ["CACHE_TYPE"]
    elif "CACHE_REDIS_URL" in os.environ or os.environ.get("CACHE_REDIS_HOST"):
        cache_type = "RedisCache"
    else:
        cache_type = "simple"
    flask_config: Dict[str, Any] = {
        "CACHE_TYPE": cache_type,
        "CACHE_KEY_PREFIX": os.environ.get("CACHE_KEY_PREFIX", "ufsv::"),
    }
    # default: never cache entries never expire
    flask_config["CACHE_DEFAULT_TIMEOUT"] = int(
        os.environ.get("CACHE_DEFAULT_TIMEOUT") or 0
    )
    if flask_config["CACHE_TYPE"] == "RedisCache":
        if "CACHE_REDIS_PASSWORD" in os.environ:
            flask_config["CACHE_REDIS_PASSWORD"] = os.environ["CACHE_REDIS_PASSWORD"]
        if "CACHE_REDIS_URL" in os.environ:
            flask_config["CACHE_REDIS_URL"] = os.environ["CACHE_REDIS_URL"]
        elif "CACHE_REDIS_HOST" in os.environ:
            flask_config["CACHE_REDIS_HOST"] = os.environ["CACHE_REDIS_HOST"]
            flask_config["CACHE_REDIS_PORT"] = int(
                os.environ.get("CACHE_REDIS_PORT") or 6379
            )
            flask_config["CACHE_REDIS_DB"] = int(os.environ.get("CACHE_REDIS_DB") or 0)
        else:
            raise UnfurlError(
                "CACHE_REDIS_URL or CACHE_REDIS_HOST environment variable must be set for RedisCache"
            )
    app.config.from_mapping(flask_config)
    cache = Cache(app)
    logger.verbose("created cache %s", flask_config["CACHE_TYPE"])
    app.config["UNFURL_OPTIONS"] = {}
    app.config["UNFURL_CLONE_ROOT"] = os.getenv("UNFURL_CLONE_ROOT") or "."
    app.config["UNFURL_CLOUD_SERVER"] = (
        os.getenv("UNFURL_CLOUD_SERVER") or DEFAULT_CLOUD_SERVER
    )
    app.config["UNFURL_SECRET"] = os.getenv("UNFURL_SERVE_SECRET")
    app.config["UNFURL_LOCAL_CLOUDMAP_URL"] = os.getenv("UNFURL_LOCAL_CLOUDMAP_URL")
    app.config["CACHE_DEFAULT_PULL_TIMEOUT"] = int(
        os.environ.get("CACHE_DEFAULT_PULL_TIMEOUT") or 120
    )
    app.config["CACHE_DEFAULT_REMOTE_TAGS_TIMEOUT"] = int(
        os.environ.get("CACHE_DEFAULT_REMOTE_TAGS_TIMEOUT") or 300
    )
    app.config["CACHE_CONTROL_SERVE_STALE"] = int(
        os.environ.get("CACHE_CONTROL_SERVE_STALE") or 0  # 2592000 (1 month)
    )
    global _cache_inflight_timeout
    _cache_inflight_timeout = int(os.getenv("UNFURL_SERVE_CACHE_TIMEOUT") or 120)
    cors = app.config["UNFURL_SERVE_CORS"] = os.getenv("UNFURL_SERVE_CORS")
    if not cors:
        ucs_parts = urlparse(app.config["UNFURL_CLOUD_SERVER"])
        cors = f"{ucs_parts.scheme}://{ucs_parts.netloc}"
    if cors:
        CORS(app, origins=cors.split())
    os.environ["GIT_TERMINAL_PROMPT"] = "0"

    git_user_name = os.environ.get("UNFURL_SET_GIT_USER")
    if git_user_name:
        git_user_full_name = f"{git_user_name} unfurl-server-{semver_prerelease()}+{get_package_digest()}"
        os.environ["GIT_AUTHOR_NAME"] = git_user_full_name
        os.environ["GIT_COMMITTER_NAME"] = git_user_full_name
        os.environ["EMAIL"] = f"{git_user_name}-unfurl-server+noreply@unfurl.cloud"

    if os.environ.get("CACHE_CLEAR_ON_START"):
        prefix = os.environ.get("CACHE_CLEAR_ON_START")
        # if set, use the given prefix, otherwise the current prefix
        if prefix in ["1", "true"]:
            prefix = app.config["CACHE_KEY_PREFIX"]
        clear_all(cache, prefix)

    return cache


UNFURL_SERVER_DEBUG_PATCH = os.environ.get("UNFURL_TEST_SERVER_DEBUG_PATCH")
DEFAULT_BRANCH = "main"


def clear_cache(cache: Cache, starts_with: str) -> Optional[List[Any]]:
    backend = cache.cache
    backend.ignore_errors = True
    redis = getattr(backend, "_read_client", None)
    if redis:
        prefix = backend.key_prefix  # type: ignore
        keys = [
            k.decode()[len(prefix) :] for k in redis.keys(prefix + starts_with + "*")
        ]
    else:
        simple = getattr(backend, "_cache", None)
        if simple is not None:
            keys = [key for key in simple if key.startswith(starts_with)]
        else:
            logger.error(
                f"clearing cache prefix '{starts_with}': couldn't find cache {type(backend)}"
            )
            return None
    logger.info(f"clearing cache {starts_with}, found keys: {repr(keys)}, {len(keys)}")
    return cache.delete_many(*keys)  # type: ignore


def clear_all(cache, prefix) -> None:
    backend = cache.cache
    redis = cast(Optional["RedisCache"], getattr(backend, "_write_client", None))
    if redis:
        keys = redis.keys(pattern=prefix + "*")  # type: ignore
        logger.info(f"clearing cache with prefix {prefix}, found {len(keys)} keys")
        if keys:
            redis.delete(*keys)
    else:
        clear_cache(cache, "")


def _set_local_projects(
    repo_views: Iterable[RepoView], local_projects: Dict[str, str], clone_root, gui
):
    server_url = app.config["UNFURL_CLOUD_SERVER"]
    server_host = urlparse(server_url).hostname
    for repo_view in repo_views:
        if not repo_view.repo:
            continue
        remote_url = repo_view.repo.find_remote_url(host=server_host)
        if remote_url:
            parts = urlparse(normalize_git_url(remote_url))
            project_id = project_id_from_urlresult(parts)
            if project_id in local_projects:
                # unless the existing one is inside the clone_root
                if not is_relative_to(local_projects[project_id], clone_root):
                    continue  #  don't replace an existing local project
            logger.debug(
                "found local project at %s for %s",
                repo_view.repo.working_dir,
                project_id,
            )
            local_projects[project_id] = repo_view.repo.working_dir
        elif gui:
            # only include non unfurl cloud repos when in gui mode
            local_projects[to_json.get_local_project_path(repo_view.repo)] = (
                repo_view.repo.working_dir
            )


def set_local_projects(local_env: LocalEnv, clone_root: str, gui: bool):
    clone_root = os.path.abspath(clone_root)
    local_projects: Dict[str, str] = {}
    project = local_env.project or local_env.homeProject
    while project:
        _set_local_projects(
            project.workingDirs.values(), local_projects, clone_root, gui
        )
        project = project.parentProject
    app.config["UNFURL_LOCAL_PROJECTS"] = local_projects


def set_current_ensemble_git_url(gui: bool = False) -> Optional[LocalEnv]:
    project_or_ensemble_path = os.getenv("UNFURL_SERVE_PATH")
    if not project_or_ensemble_path:
        return None
    try:
        # the ENVIRONMENT=* in overrides is a hackish way to load all env vars for all environments
        if gui:
            overrides = dict(
                ENVIRONMENT="*",
                UNFURL_SKIP_UPSTREAM_CHECK=True,
                UNFURL_SKIP_VAULT_DECRYPT=True,
                apply_url_credentials=True,
            )
        else:
            overrides = {}
        local_env = LocalEnv(
            project_or_ensemble_path,
            overrides=overrides,
            can_be_empty=True,
            readonly=not gui,
        )
        if not local_env.manifestPath and local_env.project:
            # found project without an ensemble, try to validate the ensemble-template.yaml
            template = os.path.join(
                local_env.project.projectRoot, DefaultNames.EnsembleTemplate
            )
            if os.path.isfile(template):
                overrides["format"] = "blueprint"
                local_env = LocalEnv(template, overrides=overrides, readonly=not gui)
                logger.info('Using ensemble template found at "%s"', template)
            else:
                logger.info(
                    'Can not find an ensemble or ensemble template in project at "%s"',
                    template,
                )
                return None
    except Exception:
        logger.info(
            'No project found at "%s", no local project set', project_or_ensemble_path
        )
        return None
    if (
        local_env.project
        and local_env.project.project_repoview
        and local_env.project.project_repoview.repo
    ):
        app.config["UNFURL_CURRENT_WORKING_DIR"] = (
            local_env.project.project_repoview.repo.working_dir
        )
        server_url = app.config["UNFURL_CLOUD_SERVER"]
        server_host = urlparse(server_url).hostname
        if not gui and not server_host:
            return None  # no remote is ok in local mode
        if server_host:
            remote_url = local_env.project.project_repoview.repo.find_remote_url(
                host=server_host
            )
            if not remote_url:
                remote_url = local_env.project.project_repoview.url
            app.config["UNFURL_CURRENT_GIT_URL"] = normalize_git_url(remote_url)
        else:
            app.config["UNFURL_CURRENT_GIT_URL"] = normalize_git_url(
                local_env.project.project_repoview.repo.url
            )
        return local_env
    return None


_cache: Optional[Cache] = None


def get_cache() -> Optional[Cache]:
    return _cache


# SERVER_SOFTWARE will be set if this process is invoked by a front-end http server like apache or gunicorn
if os.getenv("SERVER_SOFTWARE"):
    _cache = configure_app()
    set_current_ensemble_git_url()


def get_project_id(request) -> str:
    project_id = request.args.get("auth_project")
    if project_id:
        return project_id
    return ""


def get_current_project_id() -> str:
    current_git_url = app.config.get("UNFURL_CURRENT_GIT_URL")
    if not current_git_url:
        return ""
    server_url = app.config["UNFURL_CLOUD_SERVER"]
    server_host = urlparse(server_url).hostname
    parts = urlparse(current_git_url)
    if parts.hostname != server_host:
        return ""
    return project_id_from_urlresult(parts)


def local_developer_mode() -> bool:
    return bool(app.config.get("UNFURL_CURRENT_GIT_URL"))


def _get_local_project_dir(project_id) -> str:
    local_projects = app.config.get("UNFURL_LOCAL_PROJECTS")
    if local_projects:
        return local_projects.get(project_id, "")
    return ""


def _get_project_repo_dir(project_id: str, branch: str, args: Optional[dict]) -> str:
    # NB: in gui mode, reserves a working_dir in UNFURL_LOCAL_PROJECTS if the project_id is missing
    if not project_id:
        return app.config.get("UNFURL_CURRENT_WORKING_DIR", ".")
    local_dir = _get_local_project_dir(project_id)
    if local_dir:
        return local_dir
    local_env = cast(Optional[LocalEnv], app.config.get("UNFURL_GUI_MODE"))
    if local_env:
        assert local_env.project
        local_dir = local_env.project._create_path_for_git_repo(
            get_project_url(project_id)
        ).rstrip("/")
        cast(dict, app.config.get("UNFURL_LOCAL_PROJECTS"))[project_id] = local_dir
        return local_dir
    return _get_managed_project_repo_dir(project_id, branch, args)


def _get_managed_project_repo_dir(
    project_id: str, branch: str, args: Optional[dict]
) -> str:
    base = "public"
    if args:
        if (
            "username" in args
            or "visibility" in args
            and args["visibility"] != "public"
        ):
            base = "private"
    clone_root = current_app.config.get("UNFURL_CLONE_ROOT", ".")
    return os.path.join(clone_root, base, project_id, branch).rstrip("/")


def _get_project_repo(
    project_id: str, branch: str, args: Optional[dict]
) -> Optional[Repo]:
    path = _get_project_repo_dir(project_id, branch, args)
    if os.path.isdir(path):
        if os.path.isfile(path + ".lock"):
            logger.warning("can't get repo: %s found", path + ".lock")
            return None  # in the middle of cloning this repo
        repo = Repo.make_repo(path)
        if not repo:
            return None
        if args:
            # make sure we are using the latest credentials:
            username, password = (
                args.get("username"),
                args.get("private_token", args.get("password")),
            )
            if username and password:
                repo.set_url_credentials(username, password, True)  # type: ignore
        return repo
    else:
        logger.warning("repo not found: %s", path)
    return None


def _clone_repo(
    project_id: str, branch: str, shallow_since: Optional[int], args: dict
) -> GitRepo:
    repo_path = _get_project_repo_dir(project_id, branch, args)
    os.makedirs(os.path.dirname(repo_path), exist_ok=True)
    username, password = (
        args.get("username"),
        args.get("private_token", args.get("password")),
    )
    git_url = get_project_url(project_id, username, password)
    clone_lock_path = repo_path + ".lock"
    try:
        with open(clone_lock_path, "xb", buffering=0) as lockfile:
            lockfile.write(bytes(str(os.getpid()), "ascii"))  # type: ignore
        return Repo.create_working_dir(
            git_url, repo_path, branch, shallow_since=shallow_since
        )
    finally:
        if os.path.exists(clone_lock_path):
            os.unlink(clone_lock_path)


_cache_inflight_sleep_duration = 0.2
# should match request timeout
_cache_inflight_timeout = 120


@dataclass
class CacheDirective:
    cache: bool = True  # save cache entry
    store: bool = True  # store value in cache
    check_file: bool = True  # check if the file in the key has changed
    # cache timeout or default (default default: never expires)
    timeout: Optional[int] = None
    latest_commit: Optional[str] = None


@dataclass
class CacheItemDependency:
    """CacheItemDependencies are used to track the dependencies referenced when generating a cached value.
    They are saved alongside the cached value and used to validate a retrieved cached value
    by checking if any of the dependencies are out of date (see ``CacheEntry._validate()``).

    A CacheItemDependency can represent a package pinned to a major version, in which case it checks there's a newer compatible version.
    Or it can represent a file or directory in a branch on a repository, in which case it checks if it has a new revision.
    """

    # XXX if the request could pass latest_commit arguments for dependent repositories we could skip having to pull from them, just like we do with the root cache check.

    project_id: str
    branch: Optional[str]
    file_paths: List[str]  # relative to project root
    key: str
    stale_pull_age: int = 0
    do_clone: bool = False
    latest_commit: str = ""  # HEAD of this branch
    last_commits: Set[str] = field(default_factory=set)  # last commit for file_path
    latest_package_url: str = ""  # set when dependency uses the latest package revision

    def to_entry(self) -> "CacheEntry":
        return CacheEntry(
            self.project_id,
            self.branch,
            "",
            self.key,
            stale_pull_age=self.stale_pull_age,
            do_clone=self.do_clone,
        )

    def dep_key(self) -> str:
        return f"{self.project_id}:{self.branch or ''}"

    def out_of_date(self, args: Optional[dict]) -> bool:
        if self.latest_package_url:
            package = get_package_from_url(self.latest_package_url)
            assert package
            set_version_from_remote_tags(package, args)
            if package.revision_tag and self.branch != package.revision_tag:
                logger.debug(
                    f"newer tag {package.revision_tag} found for {package} (was {self.branch})"
                )
                return True
            else:
                return False

        cache_entry = self.to_entry()
        # get dep's repo, pulls if last pull greater than stale_pull_age
        repo = cache_entry.pull(assert_not_none(get_cache()), self.stale_pull_age)
        if repo.revision != self.latest_commit:
            # the repository has changed, check to see if files this cache entry uses has changed
            # note: we don't need the cache value to be present in the cache since we have the commit info already
            cache_entry._set_commit_info(self.file_paths)
            if cache_entry.last_commit not in self.last_commits:
                # there's a newer version of files used by the cache entry
                logger.debug(f"dependency {self.dep_key()} changed")
                return True
        return False  # we're up-to-date!


def set_version_from_remote_tags(package: Package, args: Optional[dict]):
    from .cache import get_remote_tags_cached

    def get_remote_tags(url, pattern):
        return get_remote_tags_cached(url, pattern, args)

    package.set_version_from_repo(get_remote_tags)


CacheItemDependencies = Dict[str, CacheItemDependency]
# cache value, last_commit (on the file_path), latest_commit (seen in branch), map of deps this value depends on


def _to_plain_types(obj: Any) -> Any:
    """Recursively convert dict/str/list subclasses (e.g. AnsibleMapping,
    AnsibleUnicode, GraphqlDB) to plain Python types so the pickled
    representation uses only standard opcodes that Rust's serde_pickle can handle."""
    if isinstance(obj, dict):
        # dict subclass (AnsibleMapping, GraphqlDB, etc.)
        return {_to_plain_types(k): _to_plain_types(v) for k, v in obj.items()}
    if isinstance(obj, str):
        # str subclass (AnsibleUnicode)
        return str(obj)
    if isinstance(obj, list):
        return [_to_plain_types(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_to_plain_types(v) for v in obj)
    # pass through
    return obj


class CacheValue(NamedTuple):
    value: Any
    last_commit: str
    latest_commit: str
    deps: CacheItemDependencies
    last_commit_date: int
    # set if front end patches a cached value in-place:
    queueid: int = 0  # > 1 when the value hasn't been committed yet

    def make_etag(self) -> str:
        etag = int(self.last_commit or "0", 16) ^ int(get_package_digest() or "0", 16)
        for dep in self.deps.values():
            for last_commit in dep.last_commits:
                if last_commit:
                    etag ^= int(last_commit, 16)
        return _make_etag(hex(etag))


# Error returned by CacheEntry.get_or_set
# None on success, an Exception, or a Response with an error status code.
CacheError = Union[None, Exception, Response]

CacheWorkCallable = Callable[
    ["CacheEntry", Optional[str]], Tuple[CacheError, Any, bool]
]


PullCacheEntry = Tuple[float, str]


class InflightCacheValue(NamedTuple):
    inflight_commit: Optional[str]
    time: float


def _get_committed_date(commit: Commit) -> int:
    try:
        return commit.committed_date
    except ValueError:
        commit.repo.git.clear_cache()
        return commit.committed_date


def pull(repo: GitRepo, branch: str, shallow_since=None) -> str:
    action = "pulled"
    firstCommit = next(repo.repo.iter_commits("HEAD", max_parents=0))
    # set shallow_since so we don't remove commits we already fetched
    committed_date = _get_committed_date(firstCommit)
    if shallow_since:
        shallow_since = str(min(shallow_since, committed_date))
    else:
        shallow_since = str(committed_date)
    try:
        repo.pull(
            revision=branch,
            with_exceptions=True,
            shallow_since=shallow_since,
        )
    except git.exc.GitCommandError as e:  # type: ignore
        if (
            "You are not currently on a branch." in e.stderr
            or "bad revision" in e.stderr
        ):
            # its a local development repo or we cloned a tag, not a branch, set action so we remember this
            action = "detached"
            logger.verbose("Found detached repository at %s", repo.working_dir)
        else:
            raise
    return action


@dataclass
class CacheEntry:
    project_id: str
    branch: Optional[str]
    file_path: str  # relative to project root
    key: str
    repo: Optional[Repo] = None
    strict: bool = False
    args: Optional[dict] = None
    stale_pull_age: int = 0
    do_clone: bool = True
    _deps: CacheItemDependencies = field(default_factory=dict)
    root_entry: Optional["CacheEntry"] = None
    # following are set by get_cache() or set_cache():
    # commitinfo: Union[Literal[False], "Commit", None] = None
    last_commit: Optional[str] = None
    last_commit_date: int = 0
    hit: Optional[bool] = None
    directives: Optional[CacheDirective] = None
    value: Optional[CacheValue] = None
    pull_state: Optional[str] = None
    owns_repo: bool = False
    cache: Optional[Cache] = None

    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        state.pop("cache", None)
        return state

    def _set_project_repo(self) -> Optional[Repo]:
        self.repo = _get_project_repo(
            self.project_id,
            self.branch or DEFAULT_BRANCH,
            self.args,
        )
        self.owns_repo = True
        return self.repo

    def cache_key(self) -> str:
        return f"{self.project_id}:{self.branch or ''}:{self.file_path}:{self.key}"

    def _inflight_key(self) -> str:
        return "_inflight::" + self.cache_key()

    def delete_cache(self, cache) -> bool:
        full_key = self.cache_key()
        logger.info("deleting from cache: %s", full_key)
        return cache.delete(full_key)

    @property
    def checked_repo(self) -> Repo:
        if not self.repo:
            self._set_project_repo()
        assert self.repo, self.project_id
        return self.repo

    def pull(self, cache: Cache, stale_ok_age: int = 0, shallow_since=None) -> Repo:
        if local_developer_mode():
            if not self.repo:
                self._set_project_repo()
            if self.repo:
                try:
                    if self.repo.is_dirty():
                        # don't pull if working dir is dirty
                        return self.repo
                except Exception:
                    logger.error(
                        "dirty check failed for repository %s",
                        self.repo.working_dir,
                        exc_info=True,
                    )
                    if isinstance(self.repo, GitRepo):
                        self.repo.repo.__del__()
                        self.repo = None
                        gc.collect()

        branch = self.branch or DEFAULT_BRANCH
        repo_key = (
            self.project_id
            + ":pull:"
            + _get_project_repo_dir(self.project_id, branch, self.args)
        )
        # treat repo_key as a mutex to serialize write operations on the repo
        val = cache.get(repo_key)
        if val:
            logger.debug(f"pull cache hit found for {repo_key}: {val}")
            last_check, action = cast(PullCacheEntry, val)
            self.pull_state = action
            if action == "detached" or action == "proxied":
                # using a local development repo that's on a different branch or
                # we checked out a tag not a branch, no pull is needed
                return self.checked_repo

            if action == "in_flight":
                logger.debug(f"pull inflight for {repo_key}")
                start_time = time.time()
                while time.time() - start_time < _cache_inflight_timeout:
                    time.sleep(_cache_inflight_sleep_duration)
                    val = cache.get(repo_key)
                    if not val:
                        break  # cache was cleared?
                    last_check, action = cast(PullCacheEntry, val)
                    if action != "in_flight":  # finished, assume repo is up-to-date
                        self.pull_state = action
                        return self.checked_repo

            if stale_ok_age and (
                stale_ok_age == -1 or time.time() - last_check <= stale_ok_age
            ):
                # last_check was recent enough, no need to pull if the local clone still exists
                if not self.repo:
                    self._set_project_repo()
                if self.repo:
                    logger.trace(f"recent pull for {action} {repo_key}")
                    return self.repo

        cache.set(repo_key, (time.time(), "in_flight"), _cache_inflight_timeout)
        try:
            if not self.repo:
                self._set_project_repo()
            repo = self.repo
            if repo:
                if not isinstance(repo, GitRepo):
                    action = "proxied"
                elif repo.remote:
                    logger.info(f"pulling repo for {repo_key}")
                    try:
                        action = pull(repo, branch, shallow_since)
                    except Exception:
                        logger.info(
                            f"pull failed for {repo_key}, clearing project",
                            exc_info=True,
                        )
                        _clear_project(self.project_id)
                        if not local_developer_mode():
                            # we don't delete the repo in local developer mode
                            repo = None
                        else:
                            action = "detached"
                else:
                    action = "detached"
            if not repo:
                if self.do_clone:
                    logger.info(f"cloning repo for {repo_key}")
                    repo = _clone_repo(
                        self.project_id, branch, shallow_since, self.args or {}
                    )
                    action = "cloned"
                else:
                    raise UnfurlError(f"missing repo at {repo_key}")
            self.pull_state = action
            cache.set(repo_key, (time.time(), action))
            return repo
        except Exception:
            logger.info(f"pull failed for {repo_key}")
            cache.set(repo_key, (time.time(), "failed"))
            raise

    def _set_commit_info(self, paths: Optional[List[str]] = None) -> None:
        if paths is None:
            paths = []
            if self.file_path:  # if no file_path, just get the latest commit
                paths.append(self.file_path)
        repo = self.checked_repo
        if isinstance(repo, GitRepo):
            # note: self.file_path can be a directory
            commits = list(
                repo.repo.iter_commits(self.branch or "HEAD", paths, max_count=1)
            )
            if commits:
                commitinfo = commits[0]
                self.last_commit = commitinfo.hexsha
                self.last_commit_date = _get_committed_date(commitinfo)
        else:
            assert isinstance(repo, ProxiedRepo)
            files = repo.files
            if files and set(files).intersection(paths):
                self.last_commit_date = int(repo.revision_time)
                self.last_commit = repo.revision
                commits = paths  # type: ignore
            else:
                commits = []
        if not commits:
            # file doesn't exist
            # treat as cache miss
            self.last_commit = ""  # not found
            self.last_commit_date = 0

    def set_cache(self, cache: Cache, directives: CacheDirective, value: Any) -> str:
        self.directives = directives
        latest_commit = directives.latest_commit
        if not directives.cache:
            return latest_commit or ""
        full_key = self.cache_key()
        try:
            if self.last_commit is None:
                self._set_commit_info()
        except git.exc.GitCommandError as e:  # type: ignore
            # this can happen if the repository is detached or on a different branch (in developer mode)
            # e.g.   cmdline: git rev-list --max-count=1 main -- ensemble.yaml
            #        stderr: 'fatal: bad revision 'main''
            logger.debug(
                "set_cache for %s couldn't get commit info",
                full_key,
                exc_info=True,
            )
            return latest_commit or ""
        if not directives.store:
            value = "not_stored"  # XXX
        self.value = CacheValue(
            value,
            self.last_commit or "",
            latest_commit or self.last_commit or "",
            self._deps,
            self.last_commit_date,
        )
        logger.info(
            "setting cache with %s with %s deps %s",
            full_key,
            self.last_commit,
            [dep.project_id for dep in self.value.deps.values()],
        )
        cache.set(
            full_key,
            self.value,
            timeout=directives.timeout,
        )
        return self.last_commit or ""

    def _pull_if_missing_commit(
        self, commit: str, commit_date: int
    ) -> Tuple[bool, Repo]:
        try:
            repo = self.checked_repo
            if not isinstance(repo, GitRepo):
                return False, repo
            repo.repo.commit(commit)
            return False, repo
        except Exception:
            # commit not in repo, repo probably is out of date
            return True, self.pull(
                assert_not_none(get_cache()), shallow_since=commit_date
            )  # raises if pull fails

    def is_commit_older_than(self, older: str, newer: str, commit_date: int) -> bool:
        if older == newer:
            return False
        # our shallow clones might not have the commit, fetch now if needed
        pulled, self.repo = self._pull_if_missing_commit(older, commit_date)
        if not pulled:
            pulled, self.repo = self._pull_if_missing_commit(newer, commit_date)
        # if "older..newer" is true iter_commits (git rev-list) will list
        # newer commits up to and including "newer", newest first
        # otherwise the list will be empty
        repo = self.repo
        if isinstance(repo, GitRepo) and list(
            repo.repo.iter_commits(f"{older}..{newer}", max_count=1)
        ):
            return True
        return False

    def at_latest(self, older: str, newer: Optional[str], commit_date: int) -> bool:
        if newer:
            # return true if the client supplied an older commit than the one the cache last saw
            return not self.is_commit_older_than(older, newer, commit_date)
        else:
            repo = self.pull(assert_not_none(get_cache()), self.stale_pull_age)
            return older == repo.revision

    def get_cache(
        self, cache: Cache, latest_commit: Optional[str]
    ) -> Tuple[Optional[CacheValue], Optional[bool]]:
        """Look up a cached value and then check if it out of date by checking if the file path in the key was modified after the given commit
        (also store the last_commit so we don't have to do that check everytime)
        we assume latest_commit is the last commit the client has seen but it might be older than the local copy
        """
        full_key = self.cache_key()
        if hasattr(cache, "cache") and hasattr(cache.cache, "key_prefix"):
            prefixed_key = cache.cache.key_prefix + full_key
        else:
            prefixed_key = full_key

        # note: if CacheValue's definition changes then cache.get() will return None because it catches PickleError exceptions
        value = cast(Optional[CacheValue], cache.get(full_key))
        self.value = value
        if value is None:
            logger.info("cache miss for %s", prefixed_key)
            self.hit = False
            return None, None  # cache miss

        (
            response,
            last_commit,
            cached_latest_commit,
            self._deps,
            cached_last_commit_date,
            queueid,
        ) = value
        if latest_commit == cached_latest_commit:
            # this is the latest
            logger.info("cache hit for %s with %s", prefixed_key, latest_commit)
            self.hit = True
            return value, None
        else:
            if self.stale_pull_age == -1:
                # if stale_pull_age is -1, we never want to pull, so just treat as stale cache hit
                logger.info(
                    "cache hit for %s (stale_pull_age is -1)",
                    prefixed_key,
                )
                return value, True
            # cache might be out of date, let's check by getting the commit info for the file path
            try:
                at_latest = self.at_latest(
                    cached_latest_commit, latest_commit, cached_last_commit_date
                )
            except Exception:
                # if the cached commit was wrong we would have already cleared the project in the at_latest() call
                # so if we get an exception here is because the client sent an invalid commit
                if self.strict:
                    logger.warning(
                        "pull failed for %s, reverting local repo",
                        self.project_id,
                        exc_info=True,
                    )
                    # delete the local repository
                    _clear_project(self.project_id)
                    return None, None  # treat as cache miss
                else:
                    logger.info(
                        "cache hit for %s, but error with client's commit %s",
                        prefixed_key,
                        latest_commit,
                        exc_info=True,
                    )
                    # got an error resolving latest_commit, just return the cached value
                    self.hit = True
                    return value, None
            if at_latest:
                # repo was up-to-date, so treat as a cache hit
                logger.info(
                    "cache hit for %s with %s",
                    prefixed_key,
                    latest_commit or cached_latest_commit,
                )
                self.hit = True
                return value, None
            if self.directives and not self.directives.check_file:
                logger.info(
                    "cache miss for %s (stale but check_file disabled) with %s",
                    prefixed_key,
                    latest_commit or cached_latest_commit,
                )
                return None, None  # treat as cache miss
            # the latest_commit is newer than the cached_latest_commit, check if the file has changed
            self._set_commit_info()
            if self.last_commit == last_commit:
                # the file hasn't changed, let's update the cache with latest_commit so we don't have to do this check again
                value = CacheValue(
                    response,
                    self.last_commit or "",
                    latest_commit or cached_latest_commit,
                    self._deps,
                    self.last_commit_date,
                )
                cache.set(
                    full_key,
                    value,
                )
                self.value = value
                logger.info("cache hit for %s, updated %s", prefixed_key, latest_commit)
                self.hit = True
                return value, None
            else:
                # stale -- up to the caller to do something about it, e.g. update or delete the key
                logger.info(
                    "stale cache hit for %s with %s", prefixed_key, latest_commit
                )
                return value, bool(self.last_commit)

    def _set_inflight(
        self, cache: Cache, latest_commit: Optional[str]
    ) -> Tuple[Any, Union[bool, "Commit"]]:
        inflight = cast(InflightCacheValue, cache.get(self._inflight_key()))
        if inflight:
            inflight_commit, start_time = inflight
            # XXX if inflight_commit is older than latest_commit, do the work anyway (but don't pull if commit is missing)
            # wait for the inflight value
            # keep checking inflight key until it is deleted
            # or if been inflight longer than timeout, assume its work aborted and stop waiting
            while time.time() - start_time < _cache_inflight_timeout:
                time.sleep(_cache_inflight_sleep_duration)
                if not cache.get(self._inflight_key()):
                    # no longer in flight
                    cache_value, stale = self.get_cache(cache, inflight_commit)
                    if cache_value:  # hit, use this instead of doing our work
                        return cache_value.value, True
                    break  # missing, so inflight work must have failed, continue with our work
        cache.set(
            self._inflight_key(),
            InflightCacheValue(latest_commit, time.time()),
            _cache_inflight_timeout,
        )
        return None, False

    def _cancel_inflight(self, cache: Cache):
        return cache.delete(self._inflight_key())

    def _do_work(
        self,
        work: CacheWorkCallable,
        latest_commit: Optional[str],
        cache_dependency: Optional[CacheItemDependency] = None,
    ) -> Tuple[CacheError, Any, CacheDirective]:
        try:
            self._deps = {}
            # NB: work shouldn't modify the working directory
            err, value, cacheable = work(self, latest_commit)
        except Exception as exc:
            loc = f" ({self.repo.working_dir})" if self.repo else ""
            logger.error(
                "unexpected error doing work for cache: %s%s",
                self.cache_key(),
                loc,
                exc_info=True,
            )
            err = exc
            value = None
        if err:
            self.directives = CacheDirective(latest_commit=latest_commit, store=False)
            return err, value, self.directives
        if not self.repo or self.strict:
            # if self.strict then this might re-clone the repo
            self._set_project_repo()
        assert self.repo
        latest = self.repo.revision
        if latest:  # if revision is valid
            latest_commit = latest
        self.directives = CacheDirective(store=cacheable, latest_commit=latest_commit)
        if not err and self.root_entry and cache_dependency:
            if self.last_commit is None:
                self._set_commit_info()
            self.root_entry.add_cache_dep(
                cache_dependency, latest_commit or "", self.last_commit or ""
            )
        return err, value, self.directives

    def _validate(
        self,
        value: Any,
        cache: Cache,
        latest_commit: Optional[str],
        validate: Optional[Callable],
    ) -> bool:
        if value == "not_stored":
            return False

        if local_developer_mode():
            if not self.repo:
                self._set_project_repo()
            if self.repo:
                if self.repo.is_dirty(
                    False, os.path.join(self.repo.working_dir, self.file_path)
                ):
                    return False
            else:
                return False

        logger.debug("checking deps %s on %s", list(self._deps), self.cache_key())

        for dep in self._deps.values():
            if dep.out_of_date(self.args):
                # need to regenerate the value
                return False

        return not validate or validate(value, self, cache, latest_commit)

    def get_or_set(
        self,
        cache: Cache,
        work: CacheWorkCallable,
        latest_commit: Optional[str],
        validate: Optional[Callable] = None,
        cache_dependency: Optional[CacheItemDependency] = None,
    ) -> Tuple[CacheError, Any]:
        try:
            if latest_commit is None and not self.stale_pull_age:
                # don't use the cache
                return self._do_work(work, latest_commit)[0:2]

            cache_value, stale = self.get_cache(cache, latest_commit)
            if cache_value:  # cache hit
                if not stale:
                    if self._validate(
                        cache_value.value,
                        cache,
                        cache_value.latest_commit,
                        validate,
                    ):
                        if self.root_entry and cache_dependency:
                            self.root_entry.add_cache_dep(
                                cache_dependency,
                                cache_value.latest_commit,
                                cache_value.last_commit,
                            )
                        return None, cache_value.value
                    logger.debug(f"validation failed for {self.cache_key()}")
                elif self.stale_pull_age == -1:
                    # if stale_pull_age is -1, we never want to pull, so just return the stale value
                    return None, cache_value.value
                # otherwise in cache but stale or invalid, fall thru to redo work
                # XXX? check date to see if its recent enough to serve anyway
                # if _get_committed_date(stale) - time.time() < stale_ok_age:
                #      return value
                self.hit = False
            commit_date = cache_value.last_commit_date if cache_value else 0
            if not cache_value or not self.hit:  # cache miss
                try:
                    if not self.repo:
                        self._set_project_repo()
                    if self.repo:
                        # if we have a local copy of the repo
                        # make sure we pulled latest_commit before doing the work
                        if not latest_commit:
                            self.repo = self.pull(cache, self.stale_pull_age)
                        else:
                            pulled, self.repo = self._pull_if_missing_commit(
                                latest_commit, commit_date
                            )
                    elif self.do_clone:  # this will clone the repo
                        self.repo = self.pull(cache, shallow_since=commit_date)
                except Exception as pull_err:
                    logger.warning(
                        f"exception while pulling {self.project_id}", exc_info=True
                    )
                    return pull_err, None
            assert self.repo or not self.do_clone, self

            value, found_inflight = self._set_inflight(cache, latest_commit)
            if found_inflight:
                # there was already work inflight and use that instead
                return None, value

            err, value, directives = self._do_work(
                work, latest_commit, cache_dependency
            )
            cancel_succeeded = self._cancel_inflight(cache)
            # skip caching work if cancel inflight failed -- that means invalidate_cache deleted it
            if cancel_succeeded and not err:
                self.set_cache(cache, directives, value)
            return err, value
        finally:
            if self.owns_repo and self.repo:
                self._cleanup()

    def _cleanup(self) -> None:
        if isinstance(self.repo, GitRepo):
            self.repo.repo.__del__()
        self.repo = None
        gc.collect()

    def add_cache_dep(
        self, dep: CacheItemDependency, latest_commit: str, last_commit: str
    ) -> None:
        existing = self._deps.get(dep.dep_key())
        if existing:
            existing.file_paths.extend(dep.file_paths)
            existing.last_commits.add(last_commit)
            existing.latest_commit = latest_commit
        else:
            dep.latest_commit = latest_commit
            dep.last_commits.add(last_commit)
            self._deps[dep.dep_key()] = dep
        logger.debug("added dep %s on %s", self._deps, self.cache_key())

    def make_cache_dep(
        self: "CacheEntry", stale_pull_age: int, package: Optional[Package]
    ) -> CacheItemDependency:
        dep = CacheItemDependency(
            self.project_id,
            self.branch,
            [self.file_path],
            self.key,
            stale_pull_age,
            self.do_clone,
        )
        if package and package.discovered:
            # if set then we want to see if the dependency changed by looking for newer tags
            # (instead of pulling from the branch)
            dep.latest_package_url = str(package.url)
        return dep


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
        except (
            IndexError
        ):  # Quick sanity check to make sure the header is formatted correctly
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


@app.get("/health")
@app.doc(summary="Health check", tags=["Status"])
def health() -> str:
    return "OK"


@app.get("/version")
@app.doc(summary="Server version", tags=["Status"])
def version() -> str:
    return f"{semver_prerelease()}+{get_package_digest() or '00000000'}"


def get_canonical_url(project_id: str) -> str:
    return urljoin(DEFAULT_CLOUD_SERVER, project_id.rstrip("/") + ".git")


def get_project_url(project_id: str, username=None, password=None, branch=None) -> str:
    assert not project_id.startswith("local:"), project_id
    base_url = cast(str, current_app.config["UNFURL_CLOUD_SERVER"])
    assert base_url
    if username:
        url_parts = urlsplit(base_url)
        if password:
            netloc = f"{username}:{password}@{url_parts.netloc}"
        else:
            netloc = f"{username}@{url_parts.netloc}"
        base_url = urlunsplit(url_parts._replace(netloc=netloc))
    url = urljoin(base_url, project_id.rstrip("/") + ".git")
    if branch and branch != "(MISSING)":
        url += "#" + branch
    return url


def _stage(project_id: str, branch: str, args: dict, pull: bool) -> Optional[Repo]:
    """
    Clones or pulls the latest from the given project repository and returns the repository's working directory
    or None if clone failed.
    """
    repo = None
    repo = _get_project_repo(project_id, branch, args)
    if repo:
        logger.info(f"found repo at {repo.working_dir}")
        if pull and isinstance(repo, GitRepo) and not repo.is_dirty():
            repo.pull(with_exceptions=True)
    else:
        # repo doesn't exists, clone it
        try:
            repo = _clone_repo(project_id, branch, None, args)
        except UnfurlError:
            return None
        working_dir = repo.working_dir
        ensure_local_config(working_dir)
        logger.info("clone success: %s to %s", repo.safe_url, repo.working_dir)
    return repo


def ensure_local_config(working_dir: str) -> None:
    path = Path(working_dir)
    if (path / DefaultNames.LocalConfigTemplate).is_file() and not (
        path / "local" / DefaultNames.LocalConfig
    ).is_file():
        # create local/unfurl.yaml in the new project
        new_project = Project(str(path / DefaultNames.LocalConfig))
        created_local = init._create_local_config(new_project, logger, {})
        if not created_local:
            logger.error(
                f"creating local/unfurl.yaml in {new_project.projectRoot} failed"
            )


def _get_filepath(format: str, deployment_path: str) -> str:
    if deployment_path:
        if not deployment_path.endswith(".yaml"):
            return os.path.join(deployment_path, "ensemble.yaml")
        return deployment_path
    elif format == "blueprint":
        return "ensemble-template.yaml"
    elif format == "environments":
        return "unfurl.yaml"
    else:
        return "ensemble/ensemble.yaml"


def format_from_path(path: str) -> str:
    if path.endswith("ensemble-template.yaml"):
        return "blueprint"
    elif path.endswith("unfurl.yaml"):
        return "environments"
    else:
        return "deployment"


def _export_cache_work(
    cache_entry: CacheEntry, latest_commit: Optional[str]
) -> Tuple[CacheError, Any, bool]:
    format, sep, extra = cache_entry.key.partition("+")
    err, val = _do_export(
        cache_entry.project_id,
        format,
        cache_entry.file_path,
        cache_entry,
        latest_commit,
        cache_entry.args or {},
    )
    return err, _to_plain_types(val), True


def _make_etag(latest_commit: str) -> str:
    return f'W/"{latest_commit}"'


def json_response(obj: Any, pretty: Optional[str], **dump_args: Any) -> Response:
    if pretty:
        dump_args.setdefault("indent", 2)
    else:
        dump_args.setdefault("separators", (",", ":"))

    dumps = current_app.json.dumps
    return current_app.response_class(
        f"{dumps(obj, **dump_args)}\n", mimetype="application/json"
    )


# /export?format=environments&include_all_deployments=true&latest_commit=foo&project_id=bar&branch=main
@app.get("/export")
@app.doc(
    summary="Export ensemble as JSON",
    description="Export an ensemble or service template in a JSON format suitable for the frontend. "
    "Supports 'deployment', 'blueprint', and 'environments' formats.",
    tags=["Export"],
    responses=EXPORT_RESPONSES,
)
@app.input(ExportQuery, location="query", arg_name="query")
@app.output(ExportResponse, description="GraphQL-style JSON database of TOSCA objects")
def export(query: ExportQuery) -> ResponseReturnValue:
    requested_format = request.args.get("format", "deployment")
    if requested_format not in ["blueprint", "environments", "deployment"]:
        return create_error_response(
            "BAD_REQUEST",
            "Query parameter 'format' must be one of 'blueprint', 'environments' or 'deployment'",
        )
    deployment_path = request.args.get("deployment_path") or ""
    return _export(request, requested_format, deployment_path, False)


def get_default_branch(
    project_id: str,
    branch: Optional[str] = "(MISSING)",
    args: Optional[Dict[str, Any]] = None,
) -> str:
    project_url = get_project_url(project_id)
    package = get_package_from_url(project_url)
    if package:
        package.missing = branch == "(MISSING)"
        set_version_from_remote_tags(package, args)
        branch = package.revision_tag or DEFAULT_BRANCH
    else:
        logger.debug(
            f"{project_url} is not a package url, skipping retrieving remote version tags."
        )
        branch = DEFAULT_BRANCH

    return branch


def _export(
    request: Request,
    requested_format: str,
    deployment_path: str,
    include_all: bool,
    post_work: Optional[Callable[[CacheEntry, Any], None]] = None,
) -> ResponseReturnValue:
    latest_commit = request.args.get("latest_commit")
    if latest_commit == "undefined":
        latest_commit = None
    project_id = get_project_id(request)
    if (
        not deployment_path
        and include_all
        and project_id in ("onecommons/std", "onecommons/unfurl-types")
    ):
        file_path = "dummy-ensemble.yaml"
    else:
        file_path = _get_filepath(requested_format, deployment_path)
    branch = request.args.get("branch")
    if branch == "HEAD":
        branch = ""
    args: Dict[str, Any] = dict(request.args)
    if request.headers.get("X-Git-Credentials"):
        args["username"], args["password"] = (
            b64decode(request.headers["X-Git-Credentials"]).decode().split(":", 1)
        )
    if project_id and not project_id.startswith("local:"):
        args["root_url"] = get_canonical_url(project_id)
        if not branch or branch == "(MISSING)":
            branch = get_default_branch(project_id, branch, args)
    elif not branch:
        branch = ""
    args["include_all"] = include_all
    if include_all:
        extra = "+types"
    else:
        if args.get("environment") and requested_format == "environments":
            extra = "+" + args["environment"]
        else:
            extra = ""
    repo = _get_project_repo(project_id, branch, args)
    stale_pull_age = (
        -1
        if request.args.get("stale") == "ok"
        else app.config["CACHE_DEFAULT_PULL_TIMEOUT"]
    )
    cache_entry = CacheEntry(
        project_id,
        branch,
        file_path,
        requested_format + extra,
        repo,
        args=args,
        stale_pull_age=stale_pull_age,
    )
    try:
        if requested_format == "blueprint":
            # blueprint exports can depend on more than just the file in the key
            cache_entry.directives = CacheDirective(check_file=False)
        err, json_summary = cache_entry.get_or_set(
            assert_not_none(get_cache()),
            _export_cache_work,
            latest_commit,
        )
        if not err:
            hit = cache_entry.hit and not post_work
            derrors = False
            if request.args.get("include_all_deployments"):
                deployments = []
                for manifest_path in json_summary["DeploymentPath"]:
                    dcache_entry = CacheEntry(
                        project_id,
                        branch,
                        manifest_path,
                        "deployment",
                        repo,
                        args=args,
                        stale_pull_age=stale_pull_age,
                        # don't need to set root_entry since deployments depend on the same commit
                    )
                    derr, djson = dcache_entry.get_or_set(
                        assert_not_none(get_cache()), _export_cache_work, latest_commit
                    )
                    if derr:
                        derrors = True
                        error_dict = dict(
                            deployment=manifest_path, error="Internal Error"
                        )
                        if isinstance(derr, Exception):
                            error_dict["details"] = "".join(
                                traceback.TracebackException.from_exception(
                                    derr
                                ).format()
                            )
                        deployments.append(error_dict)
                    else:
                        deployments.append(djson)
                    hit = hit and dcache_entry.hit
                json_summary["deployments"] = deployments
            if not derrors and (hit or (cache_entry.value and not post_work)):
                etag = request.headers.get("If-None-Match")
                if etag and cache_entry.value and cache_entry.value.make_etag() == etag:
                    return Response("Not Modified", status=304)
            elif post_work:
                post_work(cache_entry, json_summary)

            if cache_entry.value and cache_entry.value.latest_commit:
                json_summary["latest_commit"] = cache_entry.value.latest_commit
            response = json_response(
                json_summary, request.args.get("pretty"), sort_keys=False
            )
            if not derrors:
                # don't set caching if there were errors
                if cache_entry.value:
                    response.headers["Etag"] = cache_entry.value.make_etag()
                if latest_commit or stale_pull_age == -1:
                    max_age = 86400  # one day
                else:
                    max_age = stale_pull_age
                serve_stale = app.config["CACHE_CONTROL_SERVE_STALE"]
                if serve_stale:
                    response.headers["Cache-Control"] = (
                        f"max-age={max_age}, stale-while-revalidate={serve_stale}"
                    )
            return response
        else:
            if isinstance(err, FatalToscaImportError):
                return create_error_response(
                    "BAD_REPOSITORY",
                    "Aborting loading the {requested_format} because an import failed.",
                    err,
                )
            elif isinstance(err, Exception):
                return create_error_response(
                    "INTERNAL_ERROR", "An internal error occurred", err
                )
            else:
                return err
    finally:
        if isinstance(repo, GitRepo):
            repo.repo.__del__()
            gc.collect()


@app.get("/types")
@app.doc(
    summary="Export TOSCA types",
    description="Export all available TOSCA resource types, optionally augmented from a CloudMap project.",
    tags=["Export"],
    responses=EXPORT_RESPONSES,
)
@app.input(TypesQuery, location="query", arg_name="query")
@app.output(ExportResponse, description="GraphQL-style JSON database of TOSCA types")
def get_types(query: TypesQuery) -> ResponseReturnValue:
    # request.args.getlist("implementation_requirements")
    # request.args.getlist("extends")
    # request.args.getlist("implements")
    _add_types = None
    filename = request.args.get("file")
    cloudmap_project_id = request.args.get("cloudmap")
    if cloudmap_project_id:  # e.g. "onecommons/cloudmap"
        from .cache import get_cloudmap_types

        def _add_types(cache_entry: CacheEntry, db: Any):
            err, types = get_cloudmap_types(cloudmap_project_id, cache_entry)
            if err:
                return err
            db["ResourceType"].update(types)

    return _export(request, "blueprint", filename or "", True, _add_types)


@app.post("/populate_cache")
@app.doc(summary="Populate export cache for a project file", tags=["Cache"])
@app.input(PopulateCacheQuery, location="query", arg_name="query")
def populate_cache(query: PopulateCacheQuery) -> ResponseReturnValue:
    project_id = get_project_id(request)
    branch = request.args.get("branch", DEFAULT_BRANCH)
    for prefix in ["refs/heads/", "refs/tags/"]:
        if branch.startswith(prefix):
            branch = branch[len(prefix) :]
            break
    path = request.args["path"]
    latest_commit = request.args["latest_commit"]
    requested_format = format_from_path(path)
    removed = request.args.get("removed")
    cache_entry = CacheEntry(
        project_id, branch, path, requested_format, args=dict(request.args)
    )
    visibility = request.args.get("visibility")
    logger.debug(
        "populate cache with %s at %s, (removed: %s visibility: %s)",
        cache_entry.cache_key(),
        latest_commit,
        removed,
        visibility,
    )
    cache = assert_not_none(get_cache())
    if removed and removed not in ["0", "false"]:
        cache_entry.delete_cache(cache)
        cache_entry._cancel_inflight(cache)
        return "OK"
    project_dir = _get_project_repo_dir(project_id, branch, dict(visibility=visibility))
    if not os.path.isdir(project_dir):
        # don't try to clone private repository
        if visibility != "public":
            logger.info("skipping populate cache for private repository %s", project_id)
            return "OK"
    err, json_summary = cache_entry.get_or_set(cache, _export_cache_work, latest_commit)
    if err:
        if isinstance(err, Exception):
            return create_error_response(
                "INTERNAL_ERROR", "An internal error occurred", err
            )
        else:
            return err
    else:
        return "OK"


@app.post("/empty_cache")
@app.doc(summary="Clear all cache entries (admin only)", tags=["Cache"])
@app.input(EmptyCacheQuery, location="query", arg_name="query")
def empty_cache(query: EmptyCacheQuery) -> ResponseReturnValue:
    project_id = get_project_id(request)
    # only members of this project (with write permission) has permission for this
    admin_project = os.environ.get("UNFURL_SERVER_ADMIN_PROJECT")
    if not project_id or project_id != admin_project:
        return create_error_response("UNAUTHORIZED", "Unauthorized project")
    prefix = request.args.get("cache_prefix", app.config["CACHE_KEY_PREFIX"])
    cache = assert_not_none(get_cache())
    clear_all(cache, prefix)
    return "OK"


@app.post("/clear_project_file_cache")
@app.doc(summary="Clear cache and cloned files for a project", tags=["Cache"])
@app.input(ClearProjectQuery, location="query", arg_name="query")
def clear_project(query: ClearProjectQuery) -> ResponseReturnValue:
    project_id = get_project_id(request)
    return _clear_project(project_id)


def _clear_project(project_id: str) -> ResponseReturnValue:
    if not local_developer_mode() and project_id:
        found = False
        # only delete repos we cloned
        for visibility in ["public", "private"]:
            project_dir = _get_managed_project_repo_dir(
                project_id, "", dict(visibility=visibility)
            )
            if os.path.isdir(project_dir):
                found = True
                logger.info("clear_project: removing %s", project_dir)
                rmtree(project_dir, logger)
        if not found:
            logger.info("clear_project: %s not found", project_id)
    cache = assert_not_none(get_cache())
    cleared = clear_cache(cache, project_id + ":")
    if cleared is None:
        return create_error_response("INTERNAL_ERROR", "An internal error occurred")
    clear_cache(cache, "_inflight::" + project_id + ":")
    if app.config.get("UNFURL_GUI_MODE"):
        # In standalone gui mode /export uses the LocalEnv held in
        # app.config["UNFURL_GUI_MODE"] directly; reload it from disk so the
        # next request picks up newly-added environments or other on-disk
        # changes that the project_id-scoped invalidation above wouldn't
        # notice.
        refreshed = set_current_ensemble_git_url(gui=True)
        if refreshed:
            app.config["UNFURL_GUI_MODE"] = refreshed
    return f"{len(cleared)}"


def _make_readonly_localenv(
    clone_root: str,
    deployment_path: str,
    parent_localenv=None,
    requested_format: Optional[str] = None,
):
    gui_local_env = app.config.get("UNFURL_GUI_MODE")
    try:
        # we don't want to decrypt secrets because the export is cached and shared
        overrides: Dict[str, Any] = dict(
            UNFURL_SKIP_VAULT_DECRYPT=True,
            # XXX enable skipping when deps support private repositories
            UNFURL_SKIP_UPSTREAM_CHECK=bool(gui_local_env),
            apply_url_credentials=True,
        )
        overrides["UNFURL_SEARCH_ROOT"] = clone_root
        if requested_format:
            overrides["format"] = requested_format
        clone_location = os.path.join(clone_root, deployment_path)
        # if UNFURL_CURRENT_WORKING_DIR is set, use it as the home project so we don't clone remote projects that are local
        if app.config.get("UNFURL_CURRENT_WORKING_DIR", clone_root) != clone_root:
            home_dir = app.config.get("UNFURL_CURRENT_WORKING_DIR")
        else:  # when invoked from the command line UNFURL_OPTIONS are set to the cli options
            home_dir = current_app.config["UNFURL_OPTIONS"].get("home")
        local_env = LocalEnv(
            clone_location,
            home_dir,
            can_be_empty=True,
            parent=parent_localenv or gui_local_env,
            readonly=True,
            overrides=overrides,
        )
        # In standalone gui mode the parent LocalEnv caches YamlManifest
        # objects by path in `_manifests`. When the on-disk manifest is
        # modified out-of-band (e.g. by a `unfurl deploy` CLI run), the
        # cached YamlManifest still has the pre-change state —-
        # so evict the cached manifest so the next get_manifest()
        # call re-reads from disk.
        if gui_local_env and local_env.manifestPath:
            local_env._manifests.pop(local_env.manifestPath, None)
    except UnfurlError as e:
        logger.error("error loading project at %s", clone_location, exc_info=True)
        return e, None
    return None, local_env


def _validate_localenv(
    localEnv, entry: CacheEntry, cache: Cache, latest_commit: Optional[str]
) -> bool:
    return bool(
        localEnv and localEnv.project and os.path.isdir(localEnv.project.projectRoot)
    )


def _localenv_from_cache(
    cache,
    project_id: str,
    branch: str,
    deployment_path: str,
    latest_commit: Optional[str],
    args: dict,
) -> Tuple[CacheError, Optional[LocalEnv], Optional[CacheEntry]]:
    if not project_id and (gui_local_env := app.config.get("UNFURL_GUI_MODE")):
        return None, gui_local_env, None

    # we want to make cloning a repo cache work to prevent concurrent cloning
    def _cache_localenv_work(
        cache_entry: CacheEntry, latest_commit: Optional[str]
    ) -> Tuple[CacheError, Any, bool]:
        # don't try to pull -- cache will have already pulled if latest_commit wasn't in the repo
        clone_location = _fetch_working_dir(cache_entry.project_id, branch, args, False)
        if clone_location is None:
            return (
                create_error_response("BAD_REPOSITORY", "Could not find repository"),
                None,
                False,
            )
        err, local_env = _make_readonly_localenv(clone_location, deployment_path)
        return err, local_env, True

    tosca.reset_safe_mode()
    cache_entry = CacheEntry(
        project_id,
        branch,
        # localenv will use the default location if no deployment_path
        deployment_path
        or os.path.join(DefaultNames.EnsembleDirectory, DefaultNames.Ensemble),
        "localenv",
        args=args,
        stale_pull_age=app.config["CACHE_DEFAULT_PULL_TIMEOUT"],
    )
    err, value = cache_entry.get_or_set(
        cache, _cache_localenv_work, latest_commit, _validate_localenv
    )
    return err, value, cache_entry


def localenv_from_cache_checked(
    cache,
    project_id: str,
    branch: str,
    deployment_path: str,
    latest_commit: str,
    args: dict,
    check_lastcommit: bool = True,
) -> Tuple[Optional[Response], Optional[LocalEnv]]:
    """Like `_localenv_from_cache` but coerces any cache-layer exception
    into an `INTERNAL_ERROR` `Response` so the caller can `return err`
    directly into Flask without further type wrangling."""
    err, readonly_localEnv, _ = _localenv_from_cache(
        cache, project_id, branch, deployment_path, latest_commit, args
    )
    if err:
        if isinstance(err, Exception):
            err = create_error_response(
                "INTERNAL_ERROR", "An internal error occurred", err
            )
        return err, readonly_localEnv
    assert readonly_localEnv
    assert readonly_localEnv.project
    repo = readonly_localEnv.project.project_repoview.repo
    assert repo
    if check_lastcommit and latest_commit and repo.revision != latest_commit:
        logger.warning(
            f"Conflict in {project_id}: {latest_commit} != {repo.revision} ({repo.url})"
        )
        err = create_error_response("CONFLICT", "Repository at wrong revision")
        return err, readonly_localEnv
    return None, readonly_localEnv


def _do_export(
    project_id: str,
    requested_format: str,
    deployment_path: str,
    cache_entry: CacheEntry,
    latest_commit: Optional[str],
    args: dict,
) -> Tuple[CacheError, Optional[Any]]:
    # assert cache_entry.branch
    parent_localenv = args.get("parent_localenv")
    if not parent_localenv:
        err, parent_localenv, _ = _localenv_from_cache(
            assert_not_none(get_cache()),
            project_id,
            cache_entry.branch or "",
            deployment_path,
            latest_commit,
            args,
        )
        if err:
            return err, None
    assert parent_localenv
    args["parent_localenv"] = parent_localenv  # share localenv in the request
    if parent_localenv.project:
        repo: Optional[RepoView] = parent_localenv.project.project_repoview
    else:
        repo = parent_localenv.instance_repoview
    assert repo and repo.repo, (
        parent_localenv.project,
        parent_localenv.project and parent_localenv.project.project_repoview,
        parent_localenv.instance_repoview,
    )
    err, local_env = _make_readonly_localenv(
        repo.repo.working_dir, deployment_path, parent_localenv, requested_format
    )
    if err:
        return (
            create_error_response("INTERNAL_ERROR", "An internal error occurred", err),
            None,
        )
    assert local_env
    if args.get("environment"):
        local_env.manifest_environment_name = args["environment"]
    elif primary_provider := args.get("implementation_requirements"):
        if primary_provider not in ("null", "undefined") and local_env.project:
            local_env.project.contexts["_export_types_placeholder"] = dict(
                connections=dict(primary_provider=dict(type=primary_provider))
            )
            local_env.manifest_environment_name = "_export_types_placeholder"
    if cache_entry:
        from .cache import ServerCacheResolver

        local_env.make_resolver = ServerCacheResolver.make_factory(cache_entry)
    gui = bool(app.config.get("UNFURL_GUI_MODE"))
    if requested_format == "environments":
        json_summary = to_json.to_environments(
            local_env,
            args.get("root_url"),
            args.get("environment"),
            include_default=gui,
        )
    elif requested_format == "blueprint":
        json_summary = to_json.to_blueprint(
            local_env,
            args.get("root_url"),
            args.get("include_all", False),
            nested=args.get("include_all", False),
        )
    elif requested_format == "deployment":
        server_host = (
            urlparse(app.config["UNFURL_CLOUD_SERVER"]).hostname if gui else None
        )
        json_summary = to_json.to_deployment(
            local_env, args.get("root_url"), server_host=server_host
        )
    else:
        assert False, requested_format
    return None, json_summary


def _fetch_working_dir(
    project_path: str, branch: str, args: dict, pull: bool
) -> Optional[str]:
    # if successful, returns the repository's working directory or None if clone failed
    if not project_path or project_path == ".":
        clone_location = current_app.config.get("UNFURL_CURRENT_WORKING_DIR") or "."
    else:
        local_dir = _get_local_project_dir(project_path)
        if local_dir:
            # developer mode: use the project we are serving from if the project_path matches
            logger.debug("exporting from local repo %s", project_path)
            clone_location = local_dir
        else:
            # otherwise clone the project if necessary
            # root of repo not necessarily unfurl project
            repo = _stage(project_path, branch, args, pull)
            if repo:
                clone_location = repo.working_dir
                if isinstance(repo, GitRepo):
                    repo.repo.__del__()
                    gc.collect()
            else:
                clone_location = None
        if not clone_location:
            return clone_location
    # XXX local: deployment_path must be in the project repo, split repos are not supported
    # we want the caching and staging infrastructure to only know about git, not unfurl projects
    # so we can't reference a file path outside of the git repository
    return clone_location


def create_error_response(
    code: str, message: str, err: Optional[Exception] = None
) -> Response:
    http_code = 400  # Default to BAD_REQUEST
    if code == "BAD_REQUEST":
        http_code = 400
    elif code == "UNAUTHORIZED":
        http_code = 401
    elif code == "FORBIDDEN":
        http_code = 403
    elif code == "NOT_FOUND":
        http_code = 404
    elif code in ["INTERNAL_ERROR", "BAD_REPOSITORY"]:
        http_code = 500
    elif code == "CONFLICT":
        http_code = 409
    response = {"code": code, "message": message}
    if err:
        response["details"] = "".join(
            traceback.TracebackException.from_exception(err).format()
        )
    return make_response(jsonify(response), http_code)


def enter_safe_mode():
    import tosca.loader

    tosca.loader.FORCE_SAFE_MODE = os.getenv("UNFURL_TEST_SAFE_LOADER") or "1"


# SERVER_SOFTWARE will be set if this process is invoked by a front-end http server like apache or gunicorn
if os.getenv("SERVER_SOFTWARE"):
    enter_safe_mode()


# Register the /cloudmap and patch endpoints (decorators on `app` run
# at import time). Must come after every name `endpoints` imports
# from this module is defined — `app`, `CacheEntry`,
# `create_error_response`, `localenv_from_cache_checked`, etc.
from . import endpoints  # noqa: E402, F401


def _backend_port(main_port: int) -> int:
    """Python backend port: UNFURL_BACKEND_PORT env var, or main_port + 1."""
    return int(os.environ.get("UNFURL_BACKEND_PORT") or (main_port + 1))


def _find_rust_server_bin() -> Optional[str]:
    """Search for the unfurl-server binary.

    Search order:

    1. ``UNFURL_RUST_SERVER_BIN`` env var (explicit override).
    2. Cargo build output (development: ``rust/target/{debug,release}/unfurl-server``).
       Only resolves in editable installs whose `parent_dir` is the
       repo root; intentionally checked before PATH so a freshly-built
       binary always wins over a stale copy that `setuptools-rust`
       previously dropped into the venv's ``bin/``.
    3. ``PATH`` via ``shutil.which``.
    4. Alongside the installed unfurl package (distribution installs).
    """
    # 1. Explicit override (used by tests and bespoke deployments).
    override = os.environ.get("UNFURL_RUST_SERVER_BIN")
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override

    # serve.py lives at {root}/unfurl/server/serve.py
    # two dirnames up → repo root (editable install) or site-packages parent
    server_dir = os.path.dirname(os.path.abspath(__file__))  # .../unfurl/server
    pkg_dir = os.path.dirname(server_dir)  # .../unfurl (the package)
    parent_dir = os.path.dirname(pkg_dir)  # repo root or site-packages

    # 2. Cargo build output (development: rust/target/{debug,release}/unfurl-server).
    # Prefer debug over release so that `cargo build` (without --release) is picked
    # up during development.  This is checked *before* PATH so a recent `cargo build`
    # supersedes whatever `setuptools-rust` last copied into the venv's `bin/`, which
    # is otherwise a frequent source of stale-binary surprises in dev.
    for build_type in ("debug", "release"):
        candidate = os.path.join(
            parent_dir, "rust", "target", build_type, "unfurl-server"
        )
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    # 3. PATH
    found = shutil.which("unfurl-server")
    if found:
        return found

    # 4. Alongside the package (wheel installs place the binary next to the package dir)
    candidate = os.path.join(parent_dir, "unfurl-server")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate

    return None


def _start_proxy_server(host: str, port: int) -> Optional[subprocess.Popen[bytes]]:
    """Start the unfurl-server binary if available and UNFURL_RUST_SERVER != '0'."""
    if os.environ.get("UNFURL_RUST_SERVER") == "0":
        return None

    bin_path = _find_rust_server_bin()
    if not bin_path:
        if os.environ.get("UNFURL_RUST_SERVER"):
            logger.warning("UNFURL_RUST_SERVER set but unfurl-server binary not found")
        return None

    backend_port = _backend_port(port)
    env = os.environ.copy()
    env["UNFURL_HOST"] = host
    env["UNFURL_PORT"] = str(port)
    env["UNFURL_BACKEND_URL"] = f"http://{host}:{backend_port}"
    env.setdefault("UNFURL_PACKAGE_DIGEST", get_package_digest())
    # Map UNFURL_LOGGING to RUST_LOG so Rust tracing picks up the same level.
    # At debug/trace, scope the verbose level to our crate and keep the
    # chatty dependencies (reqwest, hyper, tower_http, h2, want, mio) at
    # info/warn so the log isn't dominated by per-request connection +
    # framing chatter.
    if "RUST_LOG" not in env:
        level = get_console_log_level()
        noisy_quiet = (
            "reqwest=info,tower_http=info,hyper=warn,h2=warn,want=warn,mio=warn"
        )
        if level == Levels.TRACE:
            env["RUST_LOG"] = f"trace"
        elif level < Levels.INFO:
            env["RUST_LOG"] = f"info,unfurl_server=debug,{noisy_quiet}"
        elif level >= Levels.ERROR:
            env["RUST_LOG"] = "error"
        elif level == Levels.WARNING:
            env["RUST_LOG"] = "warn"
        else:
            env["RUST_LOG"] = "info"
    logger.info(
        "Starting unfurl-server on http://%s:%d (backend port: %d) with %s RUST_LOG=%s",
        host,
        port,
        backend_port,
        get_console_log_level(),
        env["RUST_LOG"],
    )
    log_file_path = env.get("UNFURL_LOGFILE")
    stderr_target = None
    log_fh = None
    if log_file_path:
        logger.debug("Redirecting unfurl-server stderr to %s", log_file_path)
        log_fh = open(log_file_path, "ab")
        stderr_target = log_fh
    else:
        logger.debug("UNFURL_LOGFILE not set, unfurl-server logs go to stderr")
    logger.debug(
        "stderr_target=%r, log_fh=%r, log_file_path=%r",
        stderr_target,
        log_fh,
        log_file_path,
    )
    logger.debug("unfurl-server binary: %s", bin_path)
    proc = subprocess.Popen([bin_path], env=env, stderr=stderr_target)
    if log_fh:
        time.sleep(0.5)
        log_fh.flush()
        if log_file_path:
            logger.debug(
                "Log file size after Popen: %d (pid=%d)",
                os.path.getsize(log_file_path),
                proc.pid,
            )
    # Attach the file handle so the caller can close it if needed.
    proc._log_fh = log_fh  # type: ignore[attr-defined]

    # Ensure the Rust subprocess is reaped when this process receives SIGTERM
    # (e.g. when the test runner calls p.terminate()).  Without this handler
    # Python exits immediately on SIGTERM without running the finally block,
    # leaving the Rust process as an orphan that holds onto the port.
    _prev_sigterm = signal.getsignal(signal.SIGTERM)

    def _sigterm_handler(signum: int, frame: object) -> None:
        proc.terminate()  # type: ignore[union-attr]
        try:
            proc.wait(timeout=5)  # type: ignore[union-attr]
        except subprocess.TimeoutExpired:
            proc.kill()  # type: ignore[union-attr]
        signal.signal(signal.SIGTERM, _prev_sigterm)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)
    return proc


# UNFURL_HOME="" gunicorn --log-level debug -w 4 unfurl.server:app
def serve(
    host: str,
    port: int,
    secret: str,
    clone_root: str,
    project_path,
    options: dict,
    cloud_server=None,
    gui: bool = False,
):
    """Start a simple HTTP server which will expose part of the CLI's API.

    Args:
        host (str): Which host to bind to (0.0.0.0 will allow external connections)
        port (int): Port to listen to (defaults to 8080)
        secret (str): The secret to use to authenticate requests
        clone_root (str): The root directory to clone all repositories into
        project_or_ensemble_path (str): The path of the ensemble or project to base requests on
        options (dict): Additional options to pass to the server (as passed to the unfurl CLI)
    """
    global _cache
    _cache = configure_app()
    app.config["UNFURL_SECRET"] = secret
    app.config["UNFURL_OPTIONS"] = options
    app.config["UNFURL_CLONE_ROOT"] = clone_root
    if os.getenv("UNFURL_SERVE_PATH") != project_path:
        # this happens in the unit tests
        os.environ["UNFURL_SERVE_PATH"] = project_path
    if cloud_server:
        if cloud_server[0] != "/":  # unit tests use local file paths
            server_host = urlparse(cloud_server).hostname
            if not server_host:
                logger.info(
                    'Exiting, cloud server URL "%s" is not a valid absolute URL',
                    cloud_server,
                )
                return
        app.config["UNFURL_CLOUD_SERVER"] = cloud_server
    local_env = set_current_ensemble_git_url(gui)
    if local_env:
        set_local_projects(local_env, clone_root, gui)

    if gui:
        if not local_env:
            logger.error("Unable to run local ui, could not find a valid project.")
            return
        from . import gui as unfurl_gui

        unfurl_gui.create_routes(local_env)
    else:
        current_project_id = get_current_project_id()
        if current_project_id:
            set_local_server_url = f"{urljoin(app.config['UNFURL_CLOUD_SERVER'], current_project_id)}?unfurl-server=http://{host}:{port}"
            logger.info(
                f"***Visit [bold]{set_local_server_url}[/bold] to view this local project. ***",
                extra=dict(rich=dict(markup=True)),
            )
        elif app.config.get("UNFURL_CURRENT_GIT_URL"):
            logger.warning(
                f"Serving from a local project that isn't hosted on {app.config['UNFURL_CLOUD_SERVER']}, no connection URL available."
            )

    enter_safe_mode()
    if gui:
        logger.info(
            "Serving ui for project at %s", app.config.get("UNFURL_CURRENT_GIT_URL")
        )

    import waitress
    from .translogger import make_filter  # type: ignore

    wlogger = logging.getLogger("waitress.queue")
    wlogger.setLevel(Levels.ERROR)  # suppress queue warning spam

    # Enable per-request profiling by setting UNFURL_PROFILE_REQUEST=<dir>.
    # Each request produces a .prof file named after the path + timestamp.
    if profile_dir := os.environ.get("UNFURL_PROFILE_REQUEST"):
        from werkzeug.middleware.profiler import ProfilerMiddleware

        os.makedirs(profile_dir, exist_ok=True)
        app.wsgi_app = ProfilerMiddleware(  # type: ignore[method-assign]
            app.wsgi_app,
            profile_dir=profile_dir,
            filename_format="{method}.{path}.{time:.0f}.{elapsed:.0f}ms.prof",
        )
        logger.info("Per-request profiling enabled; output to %s", profile_dir)
    # Optionally start the Rust proxy server in front of waitress.
    rust_proc = None
    try:
        if app.config["CACHE_TYPE"] == "RedisCache":
            rust_proc = _start_proxy_server(host, port)

        # When the Rust proxy is running with a cloudmap-sync DB, that
        # process owns the authoritative cloudmap and Python read paths
        # (``get_cloudmap_view`` in cache.py) should hit it over HTTP
        # instead of loading ``cloudmap.yaml`` from a local clone.
        if (
            rust_proc
            and os.environ.get("UNFURL_CLOUDMAP_REPO")
            and os.environ.get("UNFURL_CLOUDMAP_DB_URL")
            and not app.config.get("UNFURL_LOCAL_CLOUDMAP_URL")
        ):
            proxy_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
            app.config["UNFURL_LOCAL_CLOUDMAP_URL"] = f"http://{proxy_host}:{port}"

        # Start single-threaded WSGI server
        waitress.serve(
            make_filter(
                app,
                logger_name="http",
                logging_level=Levels.VERBOSE,
            ),
            host=host,
            port=_backend_port(port) if rust_proc else port,
            threads=1,
            ident="unfurl",
        )
    finally:
        if rust_proc:
            rust_proc.terminate()
            rust_proc.wait()

    # gunicorn  , "-b", "0.0.0.0:5000", "unfurl.server:app"
    # from gunicorn.app.wsgiapp import WSGIApplication
    # WSGIApplication().run()
