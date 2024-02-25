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
import json
import os
from pathlib import Path
import re
import time
import traceback
from typing import (
    Dict,
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

from flask import Flask, Request, current_app, jsonify, request
import flask.json
from flask.typing import ResponseValue
from flask_caching import Cache
from flask_cors import CORS

import git
from git.objects import Commit

from .graphql import ImportDef, ResourceTypesByName, get_local_type
from .manifest import relabel_dict
from .packages import Package, get_package_from_url, is_semver

from .projectpaths import rmtree
from .localenv import LocalEnv, Project
from .repo import (
    GitRepo,
    Repo,
    RepoView,
    add_user_to_url,
    normalize_git_url,
    normalize_git_url_hard,
    sanitize_url,
    split_git_url,
    get_remote_tags,
)
from .util import UnfurlError, get_package_digest, is_relative_to, unique_name
from .logs import getLogger, add_log_file
from .yamlmanifest import YamlManifest
from .yamlloader import ImportResolver_Context, SimpleCacheResolver
from . import __version__, DefaultNames, DEFAULT_CLOUD_SERVER
from . import to_json
from . import init
from .cloudmap import Repository, RepositoryDict
from toscaparser.common.exception import FatalToscaImportError
from toscaparser.elements.entity_type import Namespace


__logfile = os.getenv("UNFURL_LOGFILE")
if __logfile:
    add_log_file(__logfile)
logger = getLogger("unfurl.server")

# note: export FLASK_ENV=development to see error stacks
# see https://flask-caching.readthedocs.io/en/latest/#built-in-cache-backends for more options
flask_config: Dict[str, Any] = {
    "CACHE_TYPE": os.environ.get("CACHE_TYPE", "simple"),
    "CACHE_KEY_PREFIX": os.environ.get("CACHE_KEY_PREFIX", "ufsv::"),
}
# default: never cache entries never expire
flask_config["CACHE_DEFAULT_TIMEOUT"] = int(
    os.environ.get("CACHE_DEFAULT_TIMEOUT") or 0
)
if flask_config["CACHE_TYPE"] == "RedisCache":
    if "CACHE_REDIS_URL" in os.environ:
        flask_config["CACHE_REDIS_URL"] = os.environ["CACHE_REDIS_URL"]
    else:
        flask_config["CACHE_REDIS_HOST"] = os.environ["CACHE_REDIS_HOST"]
        flask_config["CACHE_REDIS_PORT"] = int(
            os.environ.get("CACHE_REDIS_PORT") or 6379
        )
        flask_config["CACHE_REDIS_DB"] = int(os.environ.get("CACHE_REDIS_DB") or 0)
app = Flask(__name__)
app.config.from_mapping(flask_config)
cache = Cache(app)
logger.info("created cache %s", flask_config["CACHE_TYPE"])
app.config["UNFURL_OPTIONS"] = {}
app.config["UNFURL_CLONE_ROOT"] = os.getenv("UNFURL_CLONE_ROOT") or "."
app.config["UNFURL_CLOUD_SERVER"] = (
    os.getenv("UNFURL_CLOUD_SERVER") or DEFAULT_CLOUD_SERVER
)
app.config["UNFURL_SECRET"] = os.getenv("UNFURL_SERVE_SECRET")
app.config["CACHE_DEFAULT_PULL_TIMEOUT"] = int(
    os.environ.get("CACHE_DEFAULT_PULL_TIMEOUT") or 120
)
app.config["CACHE_DEFAULT_REMOTE_TAGS_TIMEOUT"] = int(
    os.environ.get("CACHE_DEFAULT_REMOTE_TAGS_TIMEOUT") or 300
)
app.config["CACHE_CONTROL_SERVE_STALE"] = int(
    os.environ.get("CACHE_CONTROL_SERVE_STALE") or 0  # 2592000 (1 month)
)
cors = app.config["UNFURL_SERVE_CORS"] = os.getenv("UNFURL_SERVE_CORS")
if not cors:
    ucs_parts = urlparse(app.config["UNFURL_CLOUD_SERVER"])
    cors = f"{ucs_parts.scheme}://{ucs_parts.netloc}"
if cors:
    CORS(app, origins=cors.split())
os.environ["GIT_TERMINAL_PROMPT"] = "0"

git_user_name = os.environ.get("UNFURL_SET_GIT_USER")
if git_user_name:
    git_user_full_name = (
        f"{git_user_name} unfurl-server-{__version__(True)}-{get_package_digest()}"
    )
    os.environ["GIT_AUTHOR_NAME"] = git_user_full_name
    os.environ["GIT_COMMITTER_NAME"] = git_user_full_name
    os.environ["EMAIL"] = f"{git_user_name}-unfurl-server+noreply@unfurl.cloud"

UNFURL_SERVER_DEBUG_PATCH = os.environ.get("UNFURL_TEST_SERVER_DEBUG_PATCH")


def clear_cache(cache: Cache, starts_with: str) -> Optional[List[Any]]:
    backend = cache.cache
    backend.ignore_errors = True
    redis = getattr(backend, "_read_client", None)
    if redis:
        keys = [k.decode()[len(backend.key_prefix) :] for k in redis.keys(backend.key_prefix + starts_with + "*")]  # type: ignore
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


def clear_all(cache, prefix):
    backend = cache.cache
    redis = getattr(backend, "_write_client", None)
    if redis:
        keys = redis.keys(pattern=prefix + "*")
        logger.info(f"clearing cache with prefix {prefix}, found {len(keys)} keys")
        if keys:
            redis.delete(*keys)
    else:
        clear_cache(cache, "")


if os.environ.get("CACHE_CLEAR_ON_START"):
    prefix = os.environ.get("CACHE_CLEAR_ON_START")
    # if set, use the given prefix, otherwise the current prefix
    if prefix in ["1", "true"]:
        prefix = flask_config["CACHE_KEY_PREFIX"]
    clear_all(cache, prefix)

DEFAULT_BRANCH = "main"


def _set_local_projects(repo_views, local_projects, clone_root):
    server_url = app.config["UNFURL_CLOUD_SERVER"]
    server_host = urlparse(server_url).hostname
    for repo_view in repo_views:
        remote = repo_view.repo.find_remote(host=server_host)
        if remote:
            parts = urlparse(remote.url)
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


def set_local_projects(local_env, clone_root):
    clone_root = os.path.abspath(clone_root)
    local_projects: Dict[str, str] = {}
    if local_env.project:
        _set_local_projects(
            local_env.project.workingDirs.values(), local_projects, clone_root
        )
    if local_env.homeProject:
        _set_local_projects(
            local_env.homeProject.workingDirs.values(), local_projects, clone_root
        )
    app.config["UNFURL_LOCAL_PROJECTS"] = local_projects


def set_current_ensemble_git_url():
    project_or_ensemble_path = os.getenv("UNFURL_SERVE_PATH")
    if not project_or_ensemble_path:
        return None
    try:
        local_env = LocalEnv(project_or_ensemble_path, can_be_empty=True, readonly=True)
    except Exception:
        logger.info(
            'no project found at "%s", no local project set', project_or_ensemble_path
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
        if not server_host:
            return None
        remote = local_env.project.project_repoview.repo.find_remote(host=server_host)
        if remote:
            remote_url = remote.url
        else:
            remote_url = local_env.project.project_repoview.url
        app.config["UNFURL_CURRENT_GIT_URL"] = normalize_git_url(remote_url)
        return local_env
    return None


set_current_ensemble_git_url()


def get_project_id(request):
    project_id = request.args.get("auth_project")
    if project_id:
        return project_id
    return ""


def project_id_from_urlresult(urlparse_result) -> str:
    project_id = urlparse_result.path.strip("/")
    if project_id.endswith(".git"):
        project_id = project_id[:-4]
    return project_id


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
    if not project_id:
        return app.config.get("UNFURL_CURRENT_WORKING_DIR", ".")
    local_dir = _get_local_project_dir(project_id)
    if local_dir:
        return local_dir
    base = "public"
    if args:
        if (
            "username" in args
            or "visibility" in args
            and args["visibility"] != "public"
        ):
            base = "private"
    clone_root = current_app.config.get("UNFURL_CLONE_ROOT", ".")
    return os.path.join(clone_root, base, project_id, branch)


def _get_project_repo(
    project_id: str, branch: str, args: Optional[dict]
) -> Optional[GitRepo]:
    path = _get_project_repo_dir(project_id, branch, args)
    if os.path.isdir(os.path.join(path, ".git")):
        if os.path.isfile(path + ".lock"):
            return None  # in the middle of cloning this repo
        repo = GitRepo(git.Repo(path))
        if args:
            # make sure we are using the latest credentials:
            username, password = args.get("username"), args.get(
                "private_token", args.get("password")
            )
            if username and password:
                repo.set_url_credentials(username, password, True)
        return repo
    return None


def _clone_repo(
    project_id: str, branch: str, shallow_since: Optional[int], args: dict
) -> GitRepo:
    repo_path = _get_project_repo_dir(project_id, branch, args)
    os.makedirs(os.path.dirname(repo_path), exist_ok=True)
    username, password = args.get("username"), args.get(
        "private_token", args.get("password")
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
        os.unlink(clone_lock_path)


_cache_inflight_sleep_duration = 0.2
_cache_inflight_timeout = int(
    os.getenv("UNFURL_SERVE_CACHE_TIMEOUT") or 120
)  # should match request timeout


@dataclass
class CacheDirective:
    cache: bool = True  # save cache entry
    store: bool = True  # store value in cache
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
        repo = cache_entry.pull(cache, self.stale_pull_age)
        if repo.revision != self.latest_commit:
            # the repository has changed, check to see if files this cache entry uses has changed
            # note: we don't need the cache value to be present in the cache since we have the commit info already
            last_commit_for_entry, _ = cache_entry._set_commit_info(self.file_paths)
            if last_commit_for_entry not in self.last_commits:
                # there's a newer version of files used by the cache entry
                logger.debug(f"dependency {self.dep_key()} changed")
                return True
        return False  # we're up-to-date!


def set_version_from_remote_tags(package: Package, args: Optional[dict]):
    def get_remote_tags(url, pattern):
        return get_remote_tags_cached(url, pattern, args)

    package.set_version_from_repo(get_remote_tags)


CacheItemDependencies = Dict[str, CacheItemDependency]
# cache value, last_commit (on the file_path), latest_commit (seen in branch), map of deps this value depends on


class CacheValue(NamedTuple):
    value: Any
    last_commit: str
    latest_commit: str
    deps: CacheItemDependencies
    last_commit_date: int

    def make_etag(self) -> str:
        etag = int(self.last_commit, 16) ^ int(get_package_digest(True), 16)
        for dep in self.deps.values():
            for last_commit in dep.last_commits:
                if last_commit:
                    etag ^= int(last_commit, 16)
        return _make_etag(hex(etag))


CacheWorkCallable = Callable[
    ["CacheEntry", Optional[str]], Tuple[Optional[Any], Any, bool]
]

PullCacheEntry = Tuple[float, str]


class InflightCacheValue(NamedTuple):
    inflight_commit: Optional[str]
    time: float


def pull(repo: GitRepo, branch: str, shallow_since=None) -> str:
    action = "pulled"
    try:
        firstCommit = next(repo.repo.iter_commits("HEAD", max_parents=0))
        # set shallow_since so we don't remove commits we already fetched
        if shallow_since:
            shallow_since = min(shallow_since, firstCommit.committed_date)
        else:
            shallow_since = firstCommit.committed_date
        repo.pull(
            revision=branch,
            with_exceptions=True,
            shallow_since=str(shallow_since),
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
    repo: Optional[GitRepo] = None
    strict: bool = False
    args: Optional[dict] = None
    stale_pull_age: int = 0
    do_clone: bool = True
    _deps: CacheItemDependencies = field(default_factory=dict)
    root_entry: Optional["CacheEntry"] = None
    # following are set by get_cache() or set_cache():
    commitinfo: Union[Literal[False], Optional["Commit"]] = None
    hit: Optional[bool] = None
    directives: Optional[CacheDirective] = None
    value: Optional[CacheValue] = None
    pull_state: Optional[str] = None

    def _set_project_repo(self) -> Optional[GitRepo]:
        self.repo = _get_project_repo(
            self.project_id,
            self.branch or DEFAULT_BRANCH,
            self.args,
        )
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
    def checked_repo(self) -> GitRepo:
        if not self.repo:
            self._set_project_repo()
        assert self.repo
        return self.repo

    def pull(self, cache: Cache, stale_ok_age: int = 0, shallow_since=None) -> GitRepo:
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
                    self.repo = None

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
            if action == "detached":
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

            if stale_ok_age and time.time() - last_check <= stale_ok_age:
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
                logger.info(f"pulling repo for {repo_key}")
                try:
                    action = pull(repo, branch, shallow_since)
                except Exception:
                    logger.info(
                        f"pull failed for {repo_key}, clearing project", exc_info=True
                    )
                    _clear_project(self.project_id)
                    if not local_developer_mode():
                        repo = None  # we don't delete the repo in local developer mode
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

    def _set_commit_info(self, paths=None) -> Tuple[str, int]:
        repo = self.checked_repo
        if paths is None:
            paths = []
            if self.file_path:  # if no file_path, just get the latest commit
                paths.append(self.file_path)
        # note: self.file_path can be a directory
        commits = list(repo.repo.iter_commits(self.branch, paths, max_count=1))
        if commits:
            self.commitinfo = commits[0]
            new_commit = self.commitinfo.hexsha
            new_commit_date = self.commitinfo.committed_date
        else:
            # file doesn't exist
            new_commit = ""  # not found
            new_commit_date = 0
            self.commitinfo = False  # treat as cache miss
        return new_commit, new_commit_date

    def set_cache(self, cache: Cache, directives: CacheDirective, value: Any) -> str:
        self.directives = directives
        latest_commit = directives.latest_commit
        if not directives.cache:
            return latest_commit or ""
        full_key = self.cache_key()
        try:
            if self.commitinfo is None:
                last_commit, last_commit_date = self._set_commit_info()
            elif self.commitinfo:
                last_commit = self.commitinfo.hexsha
                last_commit_date = self.commitinfo.committed_date
            else:
                last_commit = ""
                last_commit_date = 0
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
            last_commit,
            latest_commit or last_commit,
            self._deps,
            last_commit_date,
        )
        logger.info(
            "setting cache with %s with %s deps %s",
            full_key,
            last_commit,
            [dep.project_id for dep in self.value.deps.values()],
        )
        cache.set(
            full_key,
            self.value,
            timeout=directives.timeout,
        )
        return last_commit

    def _pull_if_missing_commit(
        self, commit: str, commit_date: int
    ) -> Tuple[bool, GitRepo]:
        try:
            repo = self.checked_repo
            repo.repo.commit(commit)
            return False, repo
        except Exception:
            # commit not in repo, repo probably is out of date
            return True, self.pull(
                cache, shallow_since=commit_date
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
        if list(self.repo.repo.iter_commits(f"{older}..{newer}", max_count=1)):
            return True
        return False

    def at_latest(self, older: str, newer: Optional[str], commit_date: int) -> bool:
        if newer:
            # return true if the client supplied an older commit than the one the cache last saw
            return not self.is_commit_older_than(older, newer, commit_date)
        else:
            repo = self.pull(cache, self.stale_pull_age)
            return older == repo.revision

    def get_cache(
        self, cache: Cache, latest_commit: Optional[str]
    ) -> Tuple[Optional[CacheValue], Union[bool, Optional["Commit"]]]:
        """Look up a cached value and then check if it out of date by checking if the file path in the key was modified after the given commit
        (also store the last_commit so we don't have to do that check everytime)
        we assume latest_commit is the last commit the client has seen but it might be older than the local copy
        """
        full_key = self.cache_key()
        # note: if CacheValue's definition changes then cache.get() will return None because it catches PickleError exceptions
        value = cast(Optional[CacheValue], cache.get(full_key))
        self.value = value
        if value is None:
            logger.info("cache miss for %s", full_key)
            self.hit = False
            return None, None  # cache miss

        (
            response,
            last_commit,
            cached_latest_commit,
            self._deps,
            cached_last_commit_date,
        ) = value
        if latest_commit == cached_latest_commit:
            # this is the latest
            logger.info("cache hit for %s with %s", full_key, latest_commit)
            self.hit = True
            return value, None
        else:
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
                        full_key,
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
                    full_key,
                    latest_commit or cached_latest_commit,
                )
                self.hit = True
                return value, None

            # the latest_commit is newer than the cached_latest_commit, check if the file has changed
            new_commit, new_commit_date = self._set_commit_info()
            if new_commit == last_commit:
                # the file hasn't changed, let's update the cache with latest_commit so we don't have to do this check again
                value = CacheValue(
                    response,
                    last_commit,
                    latest_commit or cached_latest_commit,
                    self._deps,
                    new_commit_date,
                )
                cache.set(
                    full_key,
                    value,
                )
                self.value = value
                logger.info("cache hit for %s, updated %s", full_key, latest_commit)
                self.hit = True
                return value, None
            else:
                # stale -- up to the caller to do something about it, e.g. update or delete the key
                logger.info("stale cache hit for %s with %s", full_key, latest_commit)
                return value, self.commitinfo

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
    ) -> Tuple[Optional[Any], Any, CacheDirective]:
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
            if self.commitinfo is None:
                last_commit, _ = self._set_commit_info()
            elif self.commitinfo:
                last_commit = self.commitinfo.hexsha
            else:
                last_commit = ""
            self.root_entry.add_cache_dep(
                cache_dependency, latest_commit or "", last_commit
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
            if self.checked_repo.is_dirty(False, self.file_path):
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
    ) -> Tuple[Optional[Any], Any]:
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
            # otherwise in cache but stale or invalid, fall thru to redo work
            # XXX? check date to see if its recent enough to serve anyway
            # if stale.committed_date - time.time() < stale_ok_age:
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

        err, value, directives = self._do_work(work, latest_commit, cache_dependency)
        cancel_succeeded = self._cancel_inflight(cache)
        # skip caching work if cancel inflight failed -- that means invalidate_cache deleted it
        if cancel_succeeded and not err:
            self.set_cache(cache, directives, value)
        return err, value

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
            dep.latest_package_url = package.url
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


@app.route("/health")
def health():
    return "OK"


@app.route("/version")
def version():
    return f"{__version__(True)} ({get_package_digest()})"


def get_canonical_url(project_id: str) -> str:
    return urljoin(DEFAULT_CLOUD_SERVER, project_id + ".git")


def get_project_url(project_id: str, username=None, password=None) -> str:
    base_url = current_app.config["UNFURL_CLOUD_SERVER"]
    assert base_url
    if username:
        url_parts = urlsplit(base_url)
        if password:
            netloc = f"{username}:{password}@{url_parts.netloc}"
        else:
            netloc = f"{username}@{url_parts.netloc}"
        base_url = urlunsplit(url_parts._replace(netloc=netloc))
    return urljoin(base_url, project_id + ".git")


def _stage(project_id: str, branch: str, args: dict, pull: bool) -> Optional[GitRepo]:
    """
    Clones or pulls the latest from the given project repository and returns the repository's working directory
    or None if clone failed.
    """
    repo = None
    repo = _get_project_repo(project_id, branch, args)
    if repo:
        logger.info(f"found repo at {repo.working_dir}")
        if pull and not repo.is_dirty():
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


def ensure_local_config(working_dir):
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


def _get_filepath(format, deployment_path):
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


def format_from_path(path):
    if path.endswith("ensemble-template.yaml"):
        return "blueprint"
    elif path.endswith("unfurl.yaml"):
        return "environments"
    else:
        return "deployment"


def _export_cache_work(
    cache_entry: CacheEntry, latest_commit: Optional[str]
) -> Tuple[Any, Any, bool]:
    format, sep, extra = cache_entry.key.partition("+")
    err, val = _do_export(
        cache_entry.project_id,
        format,
        cache_entry.file_path,
        cache_entry,
        latest_commit,
        cache_entry.args or {},
    )
    return err, val, True


def _make_etag(latest_commit: str):
    return f'W/"{latest_commit}"'


def json_response(obj, pretty, **dump_args):
    if pretty:
        dump_args.setdefault("indent", 2)
    else:
        dump_args.setdefault("separators", (",", ":"))

    dumps = flask.json.dumps
    mimetype = current_app.config["JSONIFY_MIMETYPE"]
    # XXX in flask 2.2+:
    # dumps = current_app.json.dumps
    # mimetype= current_app.json.mimetype
    return current_app.response_class(f"{dumps(obj, **dump_args)}\n", mimetype=mimetype)


# /export?format=environments&include_all_deployments=true&latest_commit=foo&project_id=bar&branch=main
@app.route("/export")
def export():
    requested_format = request.args.get("format", "deployment")
    if requested_format not in ["blueprint", "environments", "deployment"]:
        return create_error_response(
            "BAD_REQUEST",
            "Query parameter 'format' must be one of 'blueprint', 'environments' or 'deployment'",
        )
    deployment_path = request.args.get("deployment_path") or ""
    return _export(request, requested_format, deployment_path, False)


def _export(
    request: Request,
    requested_format: str,
    deployment_path: str,
    include_all: bool,
    post_work=None,
):
    latest_commit = request.args.get("latest_commit")
    project_id = get_project_id(request)
    file_path = _get_filepath(requested_format, deployment_path)
    branch = request.args.get("branch")
    args: Dict[str, Any] = dict(request.args)
    if request.headers.get("X-Git-Credentials"):
        args["username"], args["password"] = (
            b64decode(request.headers["X-Git-Credentials"]).decode().split(":", 1)
        )
    args["root_url"] = get_canonical_url(project_id)
    args["include_all"] = include_all
    if include_all:
        extra = "+types"
    else:
        if args.get("environment") and requested_format == "environments":
            extra = "+" + args["environment"]
        else:
            extra = ""
    if not branch or branch == "(MISSING)":
        package = get_package_from_url(get_project_url(project_id))
        if package:
            package.missing = branch == "(MISSING)"
            set_version_from_remote_tags(package, args)
            branch = package.revision_tag or DEFAULT_BRANCH
        else:
            logger.debug(
                f"{get_project_url(project_id)} is not a package url, skipping retrieving remote version tags."
            )
            branch = DEFAULT_BRANCH
    repo = _get_project_repo(project_id, branch, args)
    stale_pull_age = app.config["CACHE_DEFAULT_PULL_TIMEOUT"]
    cache_entry = CacheEntry(
        project_id,
        branch,
        file_path,
        requested_format + extra,
        repo,
        args=args,
        stale_pull_age=stale_pull_age,
    )
    err, json_summary = cache_entry.get_or_set(
        cache,
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
                    cache, _export_cache_work, latest_commit
                )
                if derr:
                    derrors = True
                    error_dict = dict(deployment=manifest_path, error="Internal Error")
                    if isinstance(derr, Exception):
                        error_dict["details"] = "".join(
                            traceback.TracebackException.from_exception(derr).format()
                        )
                    deployments.append(error_dict)
                else:
                    deployments.append(djson)
                hit = hit and dcache_entry.hit
            json_summary["deployments"] = deployments
        if not derrors and (hit or (cache_entry.value and not post_work)):
            etag = request.headers.get("If-None-Match")
            if etag and cache_entry.value and cache_entry.value.make_etag() == etag:
                return "Not Modified", 304
        elif post_work:
            post_work(cache_entry, json_summary)

        response = json_response(
            json_summary, request.args.get("pretty"), sort_keys=False
        )
        if not derrors:
            # don't set caching if there were errors
            if cache_entry.value:
                response.headers["Etag"] = cache_entry.value.make_etag()
            if latest_commit:
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


def _get_cloudmap_types(project_id: str, root_cache_entry: CacheEntry):
    err, doc = load_yaml(project_id, "main", "cloudmap.yaml", root_cache_entry)
    if doc is None:
        return err, {}
    repositories_dict = cast(Dict[str, dict], doc.get("repositories") or {})
    types: Dict[str, dict] = {}
    for r_dict in repositories_dict.values():
        r = Repository(**r_dict)
        if not r.notable:
            continue
        for file_path, notable in r.notable.items():
            if notable.get("artifact_type") == "artifact.tosca.ServiceTemplate":
                typeinfo = notable.get("type")
                if typeinfo:
                    name = typeinfo["name"]
                    if "_sourceinfo" not in typeinfo:
                        typeinfo["_sourceinfo"] = ImportDef(
                            file=file_path, url=r.git_url()
                        )
                    if "@" not in name:
                        schema = cast(str, notable.get("schema", r.git_url()))
                        local_types = ResourceTypesByName(schema, Namespace({}, ""))
                        typeinfo["name"] = name = local_types.expand_typename(name)
                        # make sure "extends" are fully qualified
                        extends = typeinfo.get("extends")
                        if extends:
                            typeinfo["extends"] = [
                                local_types.expand_typename(extend)
                                for extend in extends
                            ]
                    typeinfo["_sourceinfo"]["incomplete"] = True
                    if not typeinfo.get("description") and notable.get("description"):
                        typeinfo["description"] = notable["description"]
                    # XXX hack, always set for root type:
                    typeinfo["implementations"] = ["connect", "create"]
                    typeinfo["directives"] = ["substitute"]
                    if r.metadata.avatar_url:
                        typeinfo["icon"] = r.metadata.avatar_url
                    dependencies = notable.get("dependencies")
                    if dependencies:
                        typeinfo.setdefault("metadata", {})["components"] = dependencies
                    types[name] = typeinfo

    return err, types


@app.route("/types")
def get_types():
    # request.args.getlist("implementation_requirements")
    # request.args.getlist("extends")
    # request.args.getlist("implements")
    _add_types = None
    filename = request.args.get("file", "dummy-ensemble.yaml")
    cloudmap_project_id = request.args.get("cloudmap")
    if cloudmap_project_id:  # e.g. "onecommons/cloudmap"

        def _add_types(cache_entry, db):
            err, types = _get_cloudmap_types(cloudmap_project_id, cache_entry)
            if err:
                return err
            db["ResourceType"].update(types)

    return _export(request, "blueprint", filename, True, _add_types)


@app.route("/populate_cache", methods=["POST"])
def populate_cache():
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


@app.route("/clear_project_file_cache", methods=["POST"])
def clear_project():
    project_id = get_project_id(request)
    return _clear_project(project_id)


def _clear_project(project_id):
    if not local_developer_mode():
        found = False
        for visibility in ["public", "private"]:
            project_dir = _get_project_repo_dir(
                project_id, "", dict(visibility=visibility)
            )
            if os.path.isdir(project_dir):
                found = True
                logger.info("clear_project: removing %s", project_dir)
                rmtree(project_dir, logger)
        if not found:
            logger.info("clear_project: %s not found", project_id)
    cleared = clear_cache(cache, project_id + ":")
    if cleared is None:
        return create_error_response("INTERNAL_ERROR", "An internal error occurred")
    clear_cache(cache, "_inflight::" + project_id + ":")
    return f"{len(cleared)}"


def _make_readonly_localenv(
    clone_root: str, deployment_path: str, parent_localenv=None
):
    try:
        # we don't want to decrypt secrets because the export is cached and shared
        overrides: Dict[str, Any] = dict(
            UNFURL_SKIP_VAULT_DECRYPT=True,
            # XXX enable skipping when deps support private repositories
            UNFURL_SKIP_UPSTREAM_CHECK=False,
            apply_url_credentials=True,
        )
        overrides["UNFURL_SEARCH_ROOT"] = clone_root
        clone_location = os.path.join(clone_root, deployment_path)
        # if UNFURL_CURRENT_WORKING_DIR is set, use it as the home project so we don't clone remote projects that are local
        if app.config.get("UNFURL_CURRENT_WORKING_DIR") != clone_root:
            home_dir = app.config.get("UNFURL_CURRENT_WORKING_DIR")
        else:
            home_dir = current_app.config["UNFURL_OPTIONS"].get("home")
        local_env = LocalEnv(
            clone_location,
            home_dir,
            can_be_empty=True,
            parent=parent_localenv,
            readonly=True,
            overrides=overrides,
        )
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
) -> Tuple[Any, Optional[LocalEnv], CacheEntry]:
    # we want to make cloning a repo cache work to prevent concurrent cloning
    def _cache_localenv_work(
        cache_entry: CacheEntry, latest_commit: Optional[str]
    ) -> Tuple[Any, Any, bool]:
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

    repo = _get_project_repo(project_id, branch, args)
    cache_entry = CacheEntry(
        project_id,
        branch,
        # localenv will use the default location if no deployment_path
        deployment_path
        or os.path.join(DefaultNames.EnsembleDirectory, DefaultNames.Ensemble),
        "localenv",
        repo,
        args=args,
        stale_pull_age=app.config["CACHE_DEFAULT_PULL_TIMEOUT"],
    )
    err, value = cache_entry.get_or_set(
        cache, _cache_localenv_work, latest_commit, _validate_localenv
    )
    return err, value, cache_entry


def _localenv_from_cache_checked(
    cache,
    project_id: str,
    branch: str,
    deployment_path: str,
    latest_commit: str,
    args: dict,
    check_lastcommit: bool = True,
) -> Tuple[Any, Optional[LocalEnv]]:
    err, readonly_localEnv, _ = _localenv_from_cache(
        cache, project_id, branch, deployment_path, latest_commit, args
    )
    if err:
        return err, readonly_localEnv
    assert readonly_localEnv
    assert readonly_localEnv.project
    repo = readonly_localEnv.project.project_repoview.repo
    assert repo
    if (
        not _get_local_project_dir(project_id)
        and check_lastcommit
        and latest_commit
        and repo.revision != latest_commit
    ):
        logger.warning(f"Conflict in {project_id}: {latest_commit} != {repo.revision}")
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
) -> Tuple[Optional[Any], Optional[Any]]:
    assert cache_entry.branch
    parent_localenv = args.get("parent_localenv")
    if not parent_localenv:
        err, parent_localenv, localenv_cache_entry = _localenv_from_cache(
            cache, project_id, cache_entry.branch, deployment_path, latest_commit, args
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
        repo.repo.working_dir, deployment_path, parent_localenv
    )
    if err:
        return (
            create_error_response("INTERNAL_ERROR", "An internal error occurred", err),
            None,
        )
    assert local_env
    if args.get("environment"):
        local_env.manifest_context_name = args["environment"]
    elif args.get("implementation_requirements"):
        primary_provider = args["implementation_requirements"]
        if local_env.project:
            local_env.project.contexts["_export_types_placeholder"] = dict(
                connections=dict(primary_provider=dict(type=primary_provider))
            )
            local_env.manifest_context_name = "_export_types_placeholder"
    if cache_entry:
        local_env.make_resolver = ServerCacheResolver.make_factory(cache_entry)
    if requested_format == "environments":
        json_summary = to_json.to_environments(
            local_env, args.get("root_url"), args.get("environment")
        )
    elif requested_format == "blueprint":
        json_summary = to_json.to_blueprint(
            local_env,
            args.get("root_url"),
            args.get("include_all", False),
            nested=args.get("include_all", False),
        )
    elif requested_format == "deployment":
        json_summary = to_json.to_deployment(local_env, args.get("root_url"))
    else:
        assert False, requested_format
    return None, json_summary


def _get_body(request):
    body = request.json
    if request.headers.get("X-Git-Credentials"):
        body["username"], body["private_token"] = (
            b64decode(request.headers["X-Git-Credentials"]).decode().split(":", 1)
        )
    return body


@app.route("/delete_deployment", methods=["POST"])
def delete_deployment():
    body = _get_body(request)
    return _patch_environment(body, get_project_id(request))


@app.route("/update_environment", methods=["POST"])
def update_environment():
    body = _get_body(request)
    return _patch_environment(body, get_project_id(request))


@app.route("/delete_environment", methods=["POST"])
def delete_environment():
    body = _get_body(request)
    return _patch_environment(body, get_project_id(request))


@app.route("/create_provider", methods=["POST"])
def create_provider():
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
                    repository = unique_name(repository, repositories)  # type: ignore
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
                    repository = unique_name(repository, repositories)  # type: ignore
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
        keys = ["title", "cloud", "description", "primary", "source", "projectPath"]
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


@app.route("/update_ensemble", methods=["POST"])
def update_ensemble():
    body = _get_body(request)
    return _patch_ensemble(body, False, get_project_id(request))


@app.route("/create_ensemble", methods=["POST"])
def create_ensemble():
    body = _get_body(request)
    return _patch_ensemble(body, True, get_project_id(request))


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


def _patch_response(repo: Optional[GitRepo]):
    return jsonify(dict(commit=repo and repo.revision or None))


def _apply_environment_patch(patch: list, local_env: LocalEnv):
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
            name = patch_inner["name"]
            if deleted:
                if name in environments:
                    del environments[name]
            else:
                imports: List[ImportDef] = []
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


def _patch_environment(body: dict, project_id: str):
    patch = body.get("patch")
    assert isinstance(patch, list)
    latest_commit = body.get("latest_commit") or ""
    branch = body.get("branch", DEFAULT_BRANCH)
    err, readonly_localEnv = _localenv_from_cache_checked(
        cache, project_id, branch, "", latest_commit, body
    )
    if err:
        return err
    assert readonly_localEnv and readonly_localEnv.project
    invalidate_cache(body, "environments", project_id)
    # if UNFURL_CURRENT_WORKING_DIR is set, use it as the home project so we don't clone remote projects that are local
    home_dir = app.config.get("UNFURL_CURRENT_WORKING_DIR") or current_app.config[
        "UNFURL_OPTIONS"
    ].get("home")
    localEnv = LocalEnv(
        readonly_localEnv.project.projectRoot, home_dir, can_be_empty=True
    )
    assert localEnv.project
    repo = localEnv.project.project_repoview.repo
    assert repo
    username = cast(str, body.get("username"))
    password = cast(str, body.get("private_token", body.get("password")))
    if not password and repo.url.startswith("http"):
        return create_error_response("UNAUTHORIZED", "Missing credentials")
    was_dirty = repo.is_dirty()
    starting_revision = (
        localEnv.project.project_repoview.repo
        and localEnv.project.project_repoview.repo.revision
        or ""
    )
    localConfig = localEnv.project.localConfig
    _apply_environment_patch(patch, localEnv)
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
        file_path = _get_filepath(format, body.get("deployment_path"))
        entry = CacheEntry(project_id, branch, file_path, format)
        success = entry.delete_cache(cache)
        logger.debug(f"invalidate cache: delete {entry.cache_key()}: {success}")
        was_inflight = entry._cancel_inflight(cache)
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
    if manifest.localEnv and manifest.localEnv.manifest_context_name:
        skip_prefixes.append(manifest.localEnv.manifest_context_name)
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
    body: dict, create: bool, project_id: str, check_lastcommit=True
) -> str:
    patch = body.get("patch")
    assert isinstance(patch, list)
    environment = body.get("environment") or ""  # cloud_vars_url need the ""!
    deployment_path = body.get("deployment_path") or ""
    branch = body.get("branch", DEFAULT_BRANCH)
    existing_repo = _get_project_repo(project_id, branch, body)

    username = body.get("username")
    password = body.get("private_token", body.get("password"))
    push_url = existing_repo.url if existing_repo else app.config["UNFURL_CLOUD_SERVER"]
    if push_url and not password and push_url.startswith("http"):
        return create_error_response("UNAUTHORIZED", "Missing credentials")

    latest_commit = body.get("latest_commit") or ""
    err, parent_localenv = _localenv_from_cache_checked(
        cache, project_id, branch, "", latest_commit, body, check_lastcommit
    )
    if err:
        return err
    assert (
        parent_localenv
        and parent_localenv.project
        and parent_localenv.project.project_repoview.repo
    )
    clone_location = os.path.join(
        parent_localenv.project.project_repoview.repo.working_dir, deployment_path
    )

    invalidate_cache(body, "deployment", project_id)
    was_dirty = existing_repo and existing_repo.is_dirty()
    starting_revision = parent_localenv.project.project_repoview.repo.revision
    deployment_blueprint = body.get("deployment_blueprint")
    current_working_dir = app.config.get("UNFURL_CURRENT_WORKING_DIR")
    if current_working_dir == parent_localenv.project.project_repoview.repo.working_dir:
        # don't set home if its current project
        current_working_dir = None
    make_resolver = ServerCacheResolver.make_factory(
        None, dict(username=username, password=password)
    )
    parent_localenv.make_resolver = make_resolver
    if create:
        blueprint_url = body.get("blueprint_url", parent_localenv.project.projectRoot)
        logger.info(
            "creating deployment at %s for %s",
            clone_location,
            sanitize_url(blueprint_url, True),
        )
        # if UNFURL_CURRENT_WORKING_DIR is set, use it as the home project so clone uses the local repository if available
        msg = init.clone(
            blueprint_url,
            clone_location,
            existing=True,
            mono=True,
            skeleton="dashboard",
            use_environment=environment,
            use_deployment_blueprint=deployment_blueprint,
            home=current_working_dir,
            parent_localenv=parent_localenv,
        )
        logger.info(msg)
    # elif clone:
    #     logger.info("creating deployment at %s for %s", clone_location, sanitize_url(blueprint_url, True))
    #     msg = init.clone(clone_location, clone_location, existing=True, mono=True, skeleton="dashboard",
    #                      use_environment=environment, use_deployment_blueprint=deployment_blueprint)
    #     logger.info(msg)

    cloud_vars_url = body.get("cloud_vars_url") or ""
    # set the UNFURL_CLOUD_VARS_URL because we may need to encrypt with vault secret when we commit changes.
    # set apply_url_credentials=True so that we reuse the credentials when cloning other repositories on this server
    overrides = dict(
        ENVIRONMENT=environment,
        UNFURL_CLOUD_VARS_URL=cloud_vars_url,
        apply_url_credentials=True,
        # we need to decrypt/encrypt yaml but we can skip secret files (expensive)
        skip_secret_files=True,
    )
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
        committed = manifest.commit(commit_msg, True, True)
        if committed or create:
            logger.info(f"committed to {committed} repositories")
            if manifest.repo:
                try:
                    if password:
                        url = add_user_to_url(manifest.repo.url, username, password)
                    else:
                        url = None
                    manifest.repo.push(url)
                    logger.info("pushed")
                except Exception as err:
                    # discard the last commit that we couldn't push
                    # this is mainly for security if we couldn't push because the user wasn't authorized
                    manifest.repo.reset(f"--hard {starting_revision or 'HEAD~1'}")
                    logger.error("push failed", exc_info=True)
                    return create_error_response(
                        "INTERNAL_ERROR", "Could not push repository", err
                    )
        else:
            logger.info(f"no changes where made, nothing commited")
    return _patch_response(manifest.repo)


# no longer used
# def _do_patch(patch: List[dict], target: dict):
#     for patch_inner in patch:
#         typename = patch_inner.get("__typename")
#         deleted = patch_inner.get("__deleted")
#         target_inner = target
#         if typename != "*":
#             if not target_inner.get(typename):
#                 target_inner[typename] = {}
#             target_inner = target_inner[typename]
#         if deleted:
#             name = patch_inner.get("name", deleted)
#             if deleted == "*":
#                 if typename == "*":
#                     target = {}
#                 else:
#                     del target[typename]
#             elif name in target[typename]:
#                 del target[typename][name]
#             else:
#                 logger.warning(f"skipping delete: {deleted} is missing from {typename}")
#             continue
#         target_inner[patch_inner["name"]] = patch_inner

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
):
    repo.add_all(full_path)
    # XXX catch exception and run git restore to rollback working dir
    repo.commit_files([full_path], commit_msg)
    logger.info("committed %s: %s", full_path, commit_msg)
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
            clone_location = repo.working_dir if repo else None
        if not clone_location:
            return clone_location
    # XXX: deployment_path must be in the project repo, split repos are not supported
    # we want the caching and staging infrastructure to only know about git, not unfurl projects
    # so we can't reference a file path outside of the git repository
    return clone_location


def create_error_response(code: str, message: str, err: Optional[Exception] = None):
    http_code = 400  # Default to BAD_REQUEST
    if code == "BAD_REQUEST":
        http_code = 400
    elif code == "UNAUTHORIZED":
        http_code = 401
    elif code in ["INTERNAL_ERROR", "BAD_REPOSITORY"]:
        http_code = 500
    elif code == "CONFLICT":
        http_code = 409
    response = {"code": code, "message": message}
    if err:
        response["details"] = "".join(
            traceback.TracebackException.from_exception(err).format()
        )
    return jsonify(response), http_code


def enter_safe_mode():
    import tosca.loader

    tosca.loader.FORCE_SAFE_MODE = os.getenv("UNFURL_TEST_SAFE_LOADER") or "1"


if os.getenv("SERVER_SOFTWARE"):
    enter_safe_mode()


# UNFURL_HOME="" gunicorn --log-level debug -w 4 unfurl.server:app
def serve(
    host: str,
    port: int,
    secret: str,
    clone_root: str,
    project_path,
    options: dict,
    cloud_server=None,
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
    local_env = set_current_ensemble_git_url()
    if local_env:
        set_local_projects(local_env, clone_root)

    current_project_id = get_current_project_id()
    if current_project_id:
        set_local_server_url = f'{urljoin(app.config["UNFURL_CLOUD_SERVER"], current_project_id)}?unfurl-server=http://{host}:{port}'
        logger.info(
            f"***Visit [bold]{set_local_server_url}[/bold] to view this local project. ***",
            extra=dict(rich=dict(markup=True)),
        )
    elif app.config.get("UNFURL_CURRENT_GIT_URL"):
        logger.warning(
            f'Serving from a local project that isn\'t hosted on {app.config["UNFURL_CLOUD_SERVER"]}, no connection URL available.'
        )

    enter_safe_mode()

    # Start one WSGI server
    import uvicorn

    uvicorn.run(
        app,
        host=host,
        port=port,
        interface="wsgi",
        log_level=logger.getEffectiveLevel(),
    )

    # app.run(host=host, port=port)
    # gunicorn  , "-b", "0.0.0.0:5000", "unfurl.server:app"
    # from gunicorn.app.wsgiapp import WSGIApplication
    # WSGIApplication().run()


def load_yaml(project_id, branch, file_name, root_entry=None, latest_commit=None):
    from toscaparser.utils.yamlparser import load_yaml

    def _work(
        cache_entry: CacheEntry, latest_commit: Optional[str]
    ) -> Tuple[Any, Any, bool]:
        path = os.path.join(cache_entry.checked_repo.working_dir, cache_entry.file_path)
        doc = load_yaml(path)
        return None, doc, True

    cache_entry = CacheEntry(
        project_id,
        branch,
        file_name,
        "load_yaml",
        stale_pull_age=app.config["CACHE_DEFAULT_PULL_TIMEOUT"],
        do_clone=True,
        root_entry=root_entry,
    )
    # this will add this cache_dep to the root_cache_request's value
    # XXX create package from url, branch and latest_commit to decide if a cache_dep is need
    dep = cache_entry.make_cache_dep(cache_entry.stale_pull_age, None)
    return cache_entry.get_or_set(cache, _work, latest_commit, cache_dependency=dep)


def get_working_dir(project_id, branch, file_name, root_entry=None, latest_commit=None):
    def _work(
        cache_entry: CacheEntry, latest_commit: Optional[str]
    ) -> Tuple[Any, Any, bool]:
        path = os.path.join(cache_entry.checked_repo.working_dir, cache_entry.file_path)
        return None, path, True

    def _validate(
        working_dir, entry: CacheEntry, cache: Cache, latest_commit: Optional[str]
    ) -> bool:
        return os.path.isdir(working_dir)

    cache_entry = CacheEntry(
        project_id,
        branch,
        file_name,
        "working_dir",
        stale_pull_age=app.config["CACHE_DEFAULT_PULL_TIMEOUT"],
        do_clone=True,
        root_entry=root_entry,
    )
    return cache_entry.get_or_set(cache, _work, latest_commit, _validate)


def get_remote_tags_cached(url, pattern, args) -> List[str]:
    key = normalize_git_url_hard(url)
    tags = cast(Optional[List[str]], cache.get("tags:" + key + ":" + pattern))
    if tags is not None:
        return tags
    else:
        base_url = current_app.config["UNFURL_CLOUD_SERVER"] and normalize_git_url_hard(
            current_app.config["UNFURL_CLOUD_SERVER"]
        )
        if args and base_url and key.startswith(base_url):
            # repository on this server, apply credentials if present
            username, password = args.get("username"), args.get(
                "private_token", args.get("password")
            )
            if username and password:
                url = add_user_to_url(url, username, password)
        tags = get_remote_tags(url, pattern)
        timeout = app.config["CACHE_DEFAULT_REMOTE_TAGS_TIMEOUT"]
        cache.set("tags:" + key + ":" + pattern, tags, timeout)
        return tags


class ServerCacheResolver(SimpleCacheResolver):
    safe_mode: bool = True
    root_cache_request: Optional[CacheEntry] = None
    args: Optional[dict] = None

    @classmethod
    def make_factory(
        cls,
        root_cache_request: Optional[CacheEntry],
        credentials: Optional[dict] = None,
    ):
        def ctor(*args, **kw):
            assert root_cache_request or credentials
            resolver = cls(*args, **kw)
            resolver.root_cache_request = root_cache_request
            resolver.args = (
                credentials
                if credentials is not None
                else root_cache_request and root_cache_request.args
            )
            return resolver

        return ctor

    def get_remote_tags(self, url, pattern="*") -> List[str]:
        return get_remote_tags_cached(url, pattern, self.args)

    @property
    def use_local_cache(self) -> bool:
        return flask_config["CACHE_TYPE"] != "simple"

    def find_repository_path(
        self,
        name: str,
        tpl: Optional[Dict[str, Any]] = None,
        base_path: Optional[str] = None,
    ) -> Optional[str]:
        repo_view = self._match_repoview(name, tpl)
        if not repo_view:
            return None
        base_url = current_app.config["UNFURL_CLOUD_SERVER"]
        private = (
            not base_url or not repo_view.url.startswith(base_url) or repo_view.repo
        )
        if private:
            logger.trace(
                f"find_repository_path on server falling back to private for {repo_view.url} ({repo_view.repo})"
            )
            return self._get_link_to_repo(repo_view, base_path)
        else:
            project_id = project_id_from_urlresult(urlparse(repo_view.url))
            branch = self._branch_from_repo(repo_view)
            err, working_dir = get_working_dir(
                project_id, branch, "", root_entry=None, latest_commit=None
            )
            if err:
                return None
            return working_dir

    def _really_resolve_to_local_path(
        self,
        repo_view: RepoView,
        base: str,
        file_name: str,
    ) -> str:
        # this is called by ImportResolver.resolve_to_local_path()
        # we only want to expose a real local path during deploy time, not when generating cacheable representations
        # so in the context of the server just return a git url
        # (the only time the server will call this is when resolving expressions to a local file path (e.g. abspath, get_dir))
        path = repo_view.as_git_url(sanitize=True)
        if file_name:
            return os.path.join(path, file_name)
        else:
            return path

    def load_yaml(
        self,
        url: str,
        fragment: Optional[str],
        ctx: ImportResolver_Context,
    ) -> Tuple[Any, bool]:
        isFile, repo_view, base, file_name = ctx
        base_url = current_app.config["UNFURL_CLOUD_SERVER"]
        # if not base_url, than we're running locally, fall back to the base implementation
        if isFile or not base_url:
            # url is a file path relative to the current project, just use the ensemble's in-memory cache
            return super().load_yaml(url, fragment, ctx)
        assert repo_view  # urls must have a repo_view

        # private if the repo isn't on the server or the project has a local copy of the repository
        # XXX handle remote repositories
        private = not repo_view.url.startswith(base_url)
        project_id = project_id_from_urlresult(urlparse(repo_view.url))
        if private:
            logger.trace(
                f"load yaml {file_name} for {url} isn't on {base_url}, skipping cache."
            )
        elif repo_view.repo or _get_local_project_dir(project_id):
            private = True
            logger.trace(f"load yaml {file_name} for {url}: local repository found.")
        if private:
            return super().load_yaml(url, fragment, ctx)

        # if the repo is private, use the base implementation
        # otherwise use the server cache to resolve the url to a local repo clone and load the file from it
        # and track its cache entry as a dependency on the root cache entry

        def _work(
            cache_entry: CacheEntry, latest_commit: Optional[str]
        ) -> Tuple[Any, Any, bool]:
            path = os.path.join(cache_entry.checked_repo.working_dir, file_name)
            doc, cacheable = self._really_load_yaml(
                path, True, fragment, repo_view, cache_entry.checked_repo.working_dir
            )
            # we only care about deps when the revision is mutable (not a version tag)
            # # version specified or explicit -> not a dependency
            # # no revision specified -> use key for latest remote tags cache of repo
            # # branch or tag that isn't a semver -> dep, save commit hash as latest_commit
            assert repo_view
            # return the value and whether it is cacheable
            return None, doc, cacheable and not private

        err = None
        if not private:
            # assert repo_view.package # local repositories
            # if the revision doesn't look like a version_tag treat as branch
            branch = self._branch_from_repo(repo_view)
            cache_entry = CacheEntry(
                project_id,
                branch,
                os.path.join(repo_view.path, file_name),
                "load_yaml" + (fragment or ""),
                stale_pull_age=app.config["CACHE_DEFAULT_PULL_TIMEOUT"],
                do_clone=True,
                root_entry=self.root_cache_request,
            )
            is_cache_dep = not repo_view.package or repo_view.package.is_mutable_ref()
            if self.use_local_cache:
                doc = self.get_cache(cache_entry.cache_key())  # check local cache
                if doc is not None:
                    return doc, True
            # XXX lock_to_commit not implemented, currently used to indicate the lock have a tag
            # latest_commit = (
            #     repo_view.package.lock_to_commit if repo_view.package else None
            # )
            latest_commit = None
            dep = None
            if is_cache_dep:
                # this will add this cache_dep to the root_cache_request's value
                dep = cache_entry.make_cache_dep(
                    cache_entry.stale_pull_age,
                    repo_view.package if repo_view.package else None,
                )
            err, doc = cache_entry.get_or_set(
                cache, _work, latest_commit, cache_dependency=dep
            )
            if err:
                if not _get_project_repo(project_id, branch, None):
                    # couldn't clone the repo
                    private = True
                    # XXX not working (not set yet?):
                    # if credentials were added, to a private so we can check if clone locally with the credentials works
                    # private = repo_view.has_credentials()
            else:
                # cache_entry.directives isn't set on cache hit so the value must have been cacheable if None
                cacheable = not cache_entry.directives or cache_entry.directives.store
                if cacheable and self.use_local_cache:
                    self.set_cache(cache_entry.cache_key(), doc)

        if private:
            logger.trace(
                f"load yaml {file_name} for {url}: server falling back to private with {repo_view.repo} {err}"
            )
            doc, cacheable = super().load_yaml(url, fragment, ctx)
            # XXX support private cache deps (need to save last_commit, provide repo_view.working_dir)
        elif err:
            raise err

        return doc, cacheable

    def _branch_from_repo(self, repo_view):
        if repo_view.package and not repo_view.package.has_semver(True):
            branch = repo_view.package.revision_tag or DEFAULT_BRANCH
        elif repo_view.revision:
            branch = repo_view.revision
        else:
            url, gitpath, revision = split_git_url(repo_view.url)
            if revision and not is_semver(revision, True):
                branch = revision
            else:
                branch = DEFAULT_BRANCH
        return branch
