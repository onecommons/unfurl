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
import os
from pathlib import Path
import time
import traceback
from typing import (
    Dict,
    List,
    NamedTuple,
    Optional,
    Tuple,
    Any,
    Union,
    TYPE_CHECKING,
    cast,
    Callable,
)
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit
from base64 import b64decode

from flask import Flask, Request, current_app, jsonify, request
import flask.json
from flask.typing import ResponseValue
from flask_caching import Cache
from flask_cors import CORS

import git
from git.objects import Commit
from .manifest import relabel_dict
from .packages import Package, get_package_from_url, is_semver

from .projectpaths import rmtree
from .localenv import LocalEnv, Project
from .repo import (
    GitRepo,
    Repo,
    RepoView,
    add_user_to_url,
    normalize_git_url_hard,
    sanitize_url,
    split_git_url,
    get_remote_tags,
)
from .util import UnfurlError, get_package_digest, unique_name
from .logs import getLogger, add_log_file
from .yamlmanifest import YamlManifest
from .yamlloader import ImportResolver_Context, SimpleCacheResolver
from . import __version__, DefaultNames
from . import to_json
from . import init
from .cloudmap import Repository, RepositoryDict


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
app.config["UNFURL_CLOUD_SERVER"] = os.getenv("UNFURL_CLOUD_SERVER")
app.config["UNFURL_SECRET"] = os.getenv("UNFURL_SERVE_SECRET")
app.config["CACHE_DEFAULT_PULL_TIMEOUT"] = int(
    os.environ.get("CACHE_DEFAULT_PULL_TIMEOUT") or 120
)
app.config["CACHE_DEFAULT_REMOTE_TAGS_TIMEOUT"] = int(
    os.environ.get("CACHE_DEFAULT_REMOTE_TAGS_TIMEOUT") or 300
)
cors = app.config["UNFURL_SERVE_CORS"] = os.getenv("UNFURL_SERVE_CORS")
if cors:
    CORS(app, origins=cors.split())
os.environ["GIT_TERMINAL_PROMPT"] = "0"

git_user_name = os.environ.get("UNFURL_SET_GIT_USER")
if git_user_name:
    git_user_full_name = f"{git_user_name} unfurl-server-{__version__(True)}-{get_package_digest()}"
    os.environ["GIT_AUTHOR_NAME"] = git_user_full_name
    os.environ["GIT_COMMITTER_NAME"] = git_user_full_name
    os.environ["EMAIL"] = f"{git_user_name}-unfurl-server+noreply@unfurl.cloud"

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


def set_current_ensemble_git_url():
    project_or_ensemble_path = os.getenv("UNFURL_SERVE_PATH")
    if not project_or_ensemble_path:
        return
    try:
        local_env = LocalEnv(project_or_ensemble_path, can_be_empty=True)
        if (
            local_env.project
            and local_env.project.project_repoview
            and local_env.project.project_repoview.repo
        ):
            app.config[
                "UNFURL_CURRENT_WORKING_DIR"
            ] = local_env.project.project_repoview.repo.working_dir
            app.config["UNFURL_CURRENT_GIT_URL"] = normalize_git_url_hard(
                local_env.project.project_repoview.url
            )
    except Exception:
        logger.info(
            'no project found at "%s", no local project set', project_or_ensemble_path
        )


set_current_ensemble_git_url()


def get_project_id(request):
    project_id = request.args.get("auth_project")
    if project_id:
        return project_id
    return ""


def _get_project_repo_dir(project_id: str, branch: str, args: Optional[dict]) -> str:
    clone_root = current_app.config.get("UNFURL_CLONE_ROOT", ".")
    if not project_id:
        return clone_root
    base = "public"
    if args:
        if (
            "username" in args
            or "visibility" in args
            and args["visibility"] != "public"
        ):
            base = "private"
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


def _clone_repo(project_id: str, branch: str, args: dict) -> GitRepo:
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
        return Repo.create_working_dir(git_url, repo_path, branch, depth=1)
    finally:
        os.unlink(clone_lock_path)


_cache_inflight_sleep_duration = 0.2
_cache_inflight_timeout = float(
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
    file_path: str  # relative to project root
    key: str
    stale_pull_age: int = 0
    do_clone: bool = False
    latest_commit: str = ""  # HEAD of this branch
    last_commit: str = ""  # last commit for file_path
    latest_package_url: str = ""  # set when dependency uses the latest package revision

    def to_entry(self) -> "CacheEntry":
        return CacheEntry(
            self.project_id,
            self.branch,
            self.file_path,
            self.key,
            stale_pull_age=self.stale_pull_age,
            do_clone=self.do_clone,
        )

    def cache_key(self) -> str:
        return f"{self.project_id}:{self.branch or ''}:{self.file_path}:{self.key}"

    def out_of_date(self, args: Optional[dict]) -> bool:
        if self.latest_package_url:
            return self._tag_changed(args)
        cache_entry = self.to_entry()
        # get dep's repo, pulls if last pull greater than stale_pull_age
        repo = cache_entry.pull(cache, self.stale_pull_age)
        if repo.revision != self.latest_commit:
            # the repository has changed, check to see if files this cache entry uses has changed
            # note: we don't need the cache value to be present in the cache since we have the commit info already
            last_commit_for_entry = cache_entry._set_commit_info()
            if last_commit_for_entry != self.last_commit:
                # there's a newer version of files used by the cache entry
                logger.debug(f"dependency {self.cache_key()} changed")
                return True
        return False  # we're up-to-date!

    def _tag_changed(self, args: Optional[dict]) -> bool:
        package = get_package_from_url(self.latest_package_url)
        assert package

        def get_remote_tags(url, pattern):
            return get_remote_tags_cached(url, pattern, args)

        package.set_version_from_repo(get_remote_tags)
        if package.revision_tag and self.branch != package.revision_tag:
            logger.debug(
                f"newer tag {package.revision_tag} found for {self.cache_key()} (was {self.branch})"
            )
            return True
        return False


CacheItemDependencies = Dict[str, CacheItemDependency]
# cache value, last_commit (on the file_path), latest_commit (seen in branch), map of deps this value depends on


class CacheValue(NamedTuple):
    value: Any
    last_commit: str
    latest_commit: str
    deps: CacheItemDependencies


CacheWorkCallable = Callable[
    ["CacheEntry", Optional[str]], Tuple[Optional[Any], Any, bool]
]


def pull(repo: GitRepo, branch: str) -> str:
    action = "pulled"
    firstCommit = next(repo.repo.iter_commits("HEAD", max_parents=0))
    try:
        # use shallow_since so we don't remove commits we already fetched
        repo.pull(
            revision=branch,
            with_exceptions=True,
            shallow_since=str(firstCommit.committed_date),
        )
    except git.exc.GitCommandError as e:  # type: ignore
        if "You are not currently on a branch." in e.stderr:
            # we cloned a tag, not a branch, set action so we remember this
            action = "detached"
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
    commitinfo: Union[bool, Optional["Commit"]] = None
    hit: Optional[bool] = None
    directives: Optional[CacheDirective] = None

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

    def pull(self, cache: Cache, stale_ok_age: int = 0) -> GitRepo:
        branch = self.branch or DEFAULT_BRANCH
        repo_key = "pull:" + _get_project_repo_dir(self.project_id, branch, self.args)
        # treat repo_key as a mutex to serialize write operations on the repo
        val = cache.get(repo_key)
        if val:
            logger.debug(f"pull cache hit found for {repo_key}: {val}")
            last_check, action = cast(Tuple[float, str], val)
            if action == "detached":
                # we checked out a tag not a branch, no pull is needed
                return self.checked_repo
            if stale_ok_age and last_check - time.time() <= stale_ok_age:
                # last_check was recent enough, no need to pull if the local clone still exists
                if not self.repo:
                    self._set_project_repo()
                if self.repo:
                    return self.repo

            if action == "in_flight":
                logger.debug(f"pull inflight for {repo_key}")
                start_time = time.time()
                while time.time() - start_time < _cache_inflight_timeout:
                    time.sleep(_cache_inflight_sleep_duration)
                    val = cache.get(repo_key)
                    if not val:
                        break  # cache was cleared?
                    last_check, action = cast(Tuple[float, str], val)
                    if action != "in_flight":  # finished, assume repo is up-to-date
                        return self.checked_repo

        cache.set(repo_key, (time.time(), "in_flight"))
        try:
            if not self.repo:
                self._set_project_repo()
            repo = self.repo
            if not repo:
                if self.do_clone:
                    logger.info(f"cloning repo for {repo_key}")
                    repo = _clone_repo(self.project_id, branch, self.args or {})
                    action = "cloned"
                else:
                    raise UnfurlError(f"missing repo at {repo_key}")
            else:
                logger.info(f"pulling repo for {repo_key}")
                action = pull(repo, branch)
            cache.set(repo_key, (time.time(), action))
            return repo
        except Exception:
            logger.info(f"pull failed for {repo_key}")
            cache.set(repo_key, (time.time(), "failed"))
            raise

    def _set_commit_info(self) -> str:
        repo = self.checked_repo
        paths = []
        if self.file_path:  # if no file_path, just get the latest commit
            paths.append(self.file_path)
        # note: self.file_path can be a directory
        commits = list(repo.repo.iter_commits(self.branch, paths, max_count=1))
        if commits:
            self.commitinfo = commits[0]
            new_commit = self.commitinfo.hexsha
        else:
            # file doesn't exist
            new_commit = ""  # not found
            self.commitinfo = False  # treat as cache miss
        return new_commit

    def set_cache(self, cache: Cache, directives: CacheDirective, value: Any) -> str:
        self.directives = directives
        latest_commit = directives.latest_commit
        if not directives.cache:
            return latest_commit or ""
        full_key = self.cache_key()
        logger.info("setting cache with %s", full_key)
        if self.commitinfo is None:
            last_commit = self._set_commit_info()
        else:
            last_commit = self.commitinfo.hexsha if self.commitinfo else ""  # type: ignore
        if not directives.store:
            value = "not_stored"  # XXX
        cache.set(
            full_key,
            CacheValue(value, last_commit, latest_commit or last_commit, self._deps),
            timeout=directives.timeout,
        )
        return last_commit

    def _pull_if_missing_commit(self, commit: str) -> None:
        try:
            self.checked_repo.repo.commit(commit)
        except Exception:
            # commit not in repo, repo probably is out of date
            self.pull(cache)  # raises if pull fails

    def is_commit_older_than(self, older: str, newer: str) -> bool:
        if older == newer:
            return False
        self._pull_if_missing_commit(newer)
        # if "older..newer" is true iter_commits (git rev-list) will list
        # newer commits up to and including "newer", newest first
        # otherwise the list will be empty
        if list(self.checked_repo.repo.iter_commits(f"{older}..{newer}", max_count=1)):
            return True
        return False

    def at_latest(self, older: str, newer: Optional[str]) -> bool:
        if newer:
            # return true if the client supplied an older commit than the one the cache last saw
            return not self.is_commit_older_than(older, newer)
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
        value = cast(CacheValue, cache.get(full_key))
        if value is None:
            logger.info("cache miss for %s", full_key)
            self.hit = False
            return None, None  # cache miss

        response, last_commit, cached_latest_commit, self._deps = value
        if latest_commit == cached_latest_commit:
            # this is the latest
            logger.info("cache hit for %s with %s", full_key, latest_commit)
            self.hit = True
            return value, None
        else:
            # cache might be out of date, let's check by getting the commit info for the file path
            try:
                at_latest = self.at_latest(cached_latest_commit, latest_commit)
            except Exception:
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
                logger.info("cache hit for %s with %s", full_key, latest_commit)
                self.hit = True
                return value, None

            # the latest_commit is newer than the cached_latest_commit, check if the file has changed
            new_commit = self._set_commit_info()
            if new_commit == last_commit:
                # the file hasn't changed, let's update the cache with latest_commit so we don't have to do this check again
                value = CacheValue(
                    response,
                    last_commit,
                    latest_commit or cached_latest_commit,
                    self._deps,
                )
                cache.set(
                    full_key,
                    value,
                )
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
        inflight = cast(Tuple[str, float], cache.get(self._inflight_key()))
        if inflight:
            inflight_commit, start_time = inflight
            # if inflight_commit is older than latest_commit, do the work anyway otherwise wait for the inflight value
            if not latest_commit or not self.is_commit_older_than(
                inflight_commit, latest_commit
            ):
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

        cache.set(self._inflight_key(), (latest_commit, time.time()))
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
            logger.error("unexpected error doing work for cache", exc_info=True)
            self.directives = CacheDirective(latest_commit=latest_commit, store=False)
            return exc, None, self.directives
        if not self.repo or self.strict:
            # self.strict might re-clone the repo
            self._set_project_repo()
        assert self.repo
        latest = self.repo.revision
        if latest:  # if revision is valid
            latest_commit = latest
        self.directives = CacheDirective(store=cacheable, latest_commit=latest_commit)
        if not err and self.root_entry and cache_dependency:
            if self.commitinfo is None:
                last_commit = self._set_commit_info()
            else:
                last_commit = self.commitinfo.hexsha if self.commitinfo else ""  # type: ignore
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
        else:  # cache miss
            try:
                if not self.repo:
                    self._set_project_repo()
                if self.repo:
                    # if we have a local copy of the repo
                    # make sure we pulled latest_commit before doing the work
                    if not latest_commit:
                        self.pull(cache, self.stale_pull_age)
                    else:
                        self._pull_if_missing_commit(latest_commit)
                elif self.do_clone:
                    self.repo = self.pull(cache)  # this will clone the repo
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
        dep.latest_commit = latest_commit
        dep.last_commit = last_commit
        self._deps[dep.cache_key()] = dep
        logger.debug("added dep %s on %s", self._deps, self.cache_key())

    def make_cache_dep(
        self: "CacheEntry", stale_pull_age: int, package: Optional[Package]
    ) -> CacheItemDependency:
        dep = CacheItemDependency(
            self.project_id,
            self.branch,
            self.file_path,
            self.key,
            stale_pull_age,
            self.do_clone,
            "",
            "",
        )
        if package and package.discovered_revision:
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
    return urljoin("https://unfurl.cloud/", project_id + ".git")


def get_project_url(project_id: str, username=None, password=None) -> str:
    base_url = current_app.config.get("UNFURL_CLOUD_SERVER")
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
        repo = _clone_repo(project_id, branch, args)
        path = Path(repo.working_dir)
        if (path / DefaultNames.LocalConfigTemplate).is_file() and not (
            path / "local" / DefaultNames.LocalConfig
        ).is_file():
            # create local/unfurl.yaml in the new project
            new_project = Project(repo.working_dir)
            created_local = init._create_local_config(new_project, logger, {})
            if not created_local:
                logger.error(
                    f"creating local/unfurl.yaml in {new_project.projectRoot} failed"
                )
        logger.info("clone success: %s to %s", repo.safe_url, repo.working_dir)
    return repo


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
    err, val = _do_export(
        cache_entry.project_id,
        cache_entry.key,
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


# /export?format=environments&include_deployments=true&latest_commit=foo&project_id=bar&branch=main
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
    branch = request.args.get("branch", DEFAULT_BRANCH)
    args = dict(request.args)
    if request.headers.get("X-Git-Credentials"):
        args["username"], args["password"] = (
            b64decode(request.headers["X-Git-Credentials"]).decode().split(":", 1)
        )
    args["include_all"] = get_canonical_url(project_id) if include_all else ""
    repo = _get_project_repo(project_id, branch, args)
    cache_entry = CacheEntry(
        project_id, branch, file_path, requested_format, repo, args=args
    )
    err, json_summary = cache_entry.get_or_set(
        cache,
        _export_cache_work,
        latest_commit,
    )
    if not err:
        hit = cache_entry.hit and not post_work
        if request.args.get("include_all_deployments"):
            deployments = []
            for manifest_path in json_summary["DeploymentPath"]:
                dcache_entry = CacheEntry(
                    project_id, branch, manifest_path, "deployment", repo, args=args
                )
                derr, djson = dcache_entry.get_or_set(
                    cache, _export_cache_work, latest_commit
                )
                if derr:
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
        if hit:
            etag = request.headers.get("If-None-Match")
            if latest_commit and _make_etag(latest_commit) == etag:
                return "Not Modified", 304
        elif post_work:
            post_work(cache_entry, json_summary)

        response = json_response(
            json_summary, request.args.get("pretty"), sort_keys=False
        )
        if latest_commit:
            response.headers["Etag"] = _make_etag(latest_commit)
        return response
    else:
        if isinstance(err, Exception):
            return create_error_response(
                "INTERNAL_ERROR", "An internal error occurred", err
            )
        else:
            return err


def _get_cloudmap_types(project_id, root_cache_entry):
    err, doc = load_yaml(project_id, "main", "cloudmap.yaml", root_cache_entry)
    if doc is None:
        return err, {}
    repositories_dict = cast(Dict[str, dict], doc.get("repositories") or {})
    types = {}
    for r_dict in repositories_dict.values():
        r = Repository(**r_dict)
        if not r.notable:
            continue
        for file_path, notable in r.notable.items():
            if notable.get("artifact_type") == "artifact.tosca.ServiceTemplate":
                typeinfo = notable.get("type")
                if typeinfo:
                    if "_sourceinfo" not in typeinfo:
                        typeinfo["_sourceinfo"] = dict(file=file_path, url=r.git_url())
                    typeinfo["_sourceinfo"]["incomplete"] = True
                    if not typeinfo.get("description") and notable.get("description"):
                        typeinfo["description"] = notable["description"]
                    # XXX hack, always set for root type:
                    typeinfo["implementations"] = ["connect", "create"]
                    typeinfo["directives"] = ["substitution"]
                    types[typeinfo["name"]] = typeinfo
    return err, types


@app.route("/types")
def get_types():
    # request.args.getlist("implementation_requirements")
    # request.args.getlist("extends")
    # request.args.getlist("implements")
    _add_types = None
    filename = request.args.get("file", "dummy-ensemble.yaml")
    if request.args.get("cloudmap"):  # e.g. "onecommons/cloudmap"

        def _add_types(cache_entry, db):
            err, types = _get_cloudmap_types(request.args.get("cloudmap"), cache_entry)
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
        project_id, branch, path, requested_format, args=request.args
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
    found = False
    for visibility in ["public", "private"]:
        project_dir = _get_project_repo_dir(project_id, "", dict(visibility=visibility))
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


def _make_readonly_localenv(clone_location, parent_localenv=None):
    try:
        # we don't want to decrypt secrets because the export is cached and shared
        overrides = dict(
            UNFURL_SKIP_VAULT_DECRYPT=True,
            # XXX enable skipping when deps support private repositories
            UNFURL_SKIP_UPSTREAM_CHECK=False,
            apply_url_credentials=True,
        )
        local_env = LocalEnv(
            clone_location,
            current_app.config["UNFURL_OPTIONS"].get("home"),
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
                create_error_response("INTERNAL_ERROR", "Could not find repository"),
                None,
                False,
            )
        clone_location = os.path.join(clone_location, deployment_path)
        err, local_env = _make_readonly_localenv(clone_location)
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
    if check_lastcommit and latest_commit and repo.revision != latest_commit:
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
) -> Tuple[Optional[Any], Optional[str]]:
    assert cache_entry.branch
    err, parent_localenv, localenv_cache_entry = _localenv_from_cache(
        cache, project_id, cache_entry.branch, deployment_path, latest_commit, args
    )
    if err:
        return err, None
    assert parent_localenv
    if parent_localenv.project:
        repo = parent_localenv.project.project_repoview.repo
    else:
        repo = parent_localenv.instanceRepo
    assert repo
    clone_location = os.path.join(repo.working_dir, deployment_path)
    err, local_env = _make_readonly_localenv(clone_location, parent_localenv)
    if err:
        return (
            create_error_response("INTERNAL_ERROR", "An internal error occurred", err),
            None,
        )
    assert local_env
    if args.get("environment"):
        local_env.manifest_context_name = args["environment"]
    if cache_entry:
        local_env.make_resolver = ServerCacheResolver.make_factory(cache_entry)
    exporter = getattr(to_json, "to_" + requested_format)
    json_summary = exporter(local_env, args.get("include_all"))

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


def _update_imports(current: List[dict], new: List[dict]) -> List[dict]:
    current.extend(new)
    return current


def _apply_imports(
    template: dict,
    patch: List[dict],
    repo_url: str,
    root_file_path: str,
    repositories=None,
) -> None:
    # use _sourceinfo to patch imports and repositories
    # imports:
    #   - file, repository, prefix
    # repositories:
    #     repo_name: url
    imports = []
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
        if repository:
            if repository != "unfurl" and root:
                for name, tpl in repositories.items():
                    if tpl["url"] == root:
                        repository = name
                        break
                else:
                    # don't use an existing name because the urls won't match
                    repository = unique_name(repository, repositories)
                    logger.debug("adding repository '%s': %s", repository, root)
                    patch_repositories[repository] = repositories[repository] = dict(
                        url=root
                    )
            _import["repository"] = repository
        else:
            if root and normalize_git_url_hard(root) != normalize_git_url_hard(
                repo_url
            ):
                # if root is an url then this was imported by file inside a repository
                for name, tpl in repositories.items():
                    if tpl["url"] == root:
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
                _import["repository"] = repository
            else:
                if file == root_file_path:
                    # type defined in the root template, no need to import
                    continue
        imports.append(_import)
    _add_imports(imports, template, repositories)


def _add_imports(imports: List[dict], template: dict, repositories: dict):
    for i in imports:
        logger.trace("checking for import %s", i)
        for existing in template.setdefault("imports", []):
            # add imports if missing
            if i["file"] == existing["file"]:
                if i.get("namespace_prefix") == existing.get("namespace_prefix"):
                    existing_repository = existing.get("repository")
                    if "repository" in i:
                        if "repository" in existing:
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
) -> List[dict]:
    deployment_blueprint = patch["name"]
    doc = manifest.manifest.config
    deployment_blueprints = doc.setdefault("spec", {}).setdefault(
        "deployment_blueprints", {}
    )
    imports: List[dict] = []
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
                for name, val in prop.items():
                    tpl = old_node_templates.get(name, {})
                    _update_imports(imports, _patch_node_template(val, tpl))
                    new_node_templates[name] = tpl
                current["resource_templates"] = new_node_templates
    return imports


def _make_requirement(dependency) -> dict:
    req = dict(node=dependency.get("match"))
    if "constraint" in dependency and "visibility" in dependency["constraint"]:
        req["metadata"] = dict(visibility=dependency["constraint"]["visibility"])
    return req


def _patch_node_template(patch: dict, tpl: dict) -> List[dict]:
    imports: List[dict] = []
    for key, value in patch.items():
        if key in ["type", "directives", "imported", "metadata"]:
            tpl[key] = value
        elif key == "_sourceinfo":
            imports.append(value)
        elif key == "title":
            if value != patch["name"]:
                tpl.setdefault("metadata", {})["title"] = value
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
                imports: List[dict] = []
                environment = environments.setdefault(name, {})
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
                                imports, _patch_node_template(node_patch, tpl)
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
    localEnv = LocalEnv(readonly_localEnv.project.projectRoot, can_be_empty=True)
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
            commit_msg = body.get("commit_msg", "Update environment")
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
    imports: List[dict] = []
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
                _update_imports(imports, _patch_node_template(patch_inner, doc))
    assert manifest.manifest and manifest.manifest.config and manifest.repo
    _apply_imports(
        manifest.manifest.config["spec"]["service_template"],
        imports,
        manifest.repo.url,
        # template path relative to the repository root
        manifest.get_tosca_file_path(),
    )


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
    if create:
        blueprint_url = body.get("blueprint_url", parent_localenv.project.projectRoot)
        logger.info(
            "creating deployment at %s for %s",
            clone_location,
            sanitize_url(blueprint_url, True),
        )
        msg = init.clone(
            blueprint_url,
            clone_location,
            existing=True,
            mono=True,
            skeleton="dashboard",
            use_environment=environment,
            use_deployment_blueprint=deployment_blueprint,
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
    )
    # don't validate in case we are still an incomplete draft
    local_env = LocalEnv(clone_location, overrides=overrides)
    local_env.make_resolver = ServerCacheResolver.make_factory(
        None, dict(username=username, password=password)
    )
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
        commit_msg = body.get("commit_msg", "Update deployment")
        # XXX catch exception from commit and run git restore to rollback working dir
        committed = manifest.commit(commit_msg, True)
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
    current_working_dir = current_app.config.get("UNFURL_CURRENT_WORKING_DIR") or "."
    if not project_path or project_path == ".":
        clone_location = current_working_dir
    else:
        current_git_url = current_app.config.get("UNFURL_CURRENT_GIT_URL")
        if (
            current_git_url
            and normalize_git_url_hard(get_project_url(project_path)) == current_git_url
        ):
            # developer mode: use the project we are serving from if the project_path matches
            logger.debug("exporting from local repo %s", current_git_url)
            clone_location = current_working_dir
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
    elif code == "INTERNAL_ERROR":
        http_code = 500
    elif code == "CONFLICT":
        http_code = 409
    response = {"code": code, "message": message}
    if err:
        response["details"] = "".join(
            traceback.TracebackException.from_exception(err).format()
        )
    return jsonify(response), http_code


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
    app.config["UNFURL_CLOUD_SERVER"] = cloud_server or os.getenv("UNFURL_CLOUD_SERVER")
    if os.getenv("UNFURL_SERVE_PATH") != project_path:
        # this happens in the unit tests
        os.environ["UNFURL_SERVE_PATH"] = project_path
        set_current_ensemble_git_url()

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
    # gunicorn"  , "-b", "0.0.0.0:5000", "unfurl.server:app"
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
        private = not repo_view.url.startswith(base_url) or repo_view.repo

        # if the repo is private, use the base implementation
        # otherwise use the server cache to resolve the url to a local repo clone and load the file from it
        # and track its cache entry as a dependency on the root cache entry

        def _work(
            cache_entry: CacheEntry, latest_commit: Optional[str]
        ) -> Tuple[Any, Any, bool]:
            path = os.path.join(cache_entry.checked_repo.working_dir, file_name)
            doc, cacheable = self._really_load_yaml(path, True, fragment)
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

            project_id = urlparse(repo_view.url).path.strip("/")
            if project_id.endswith(".git"):
                project_id = project_id[:-4]
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

            latest_commit = (
                repo_view.package.lock_to_commit if repo_view.package else None
            )
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
            doc, cacheable = super().load_yaml(url, fragment, ctx)
            # XXX support private cache deps (need to save last_commit, provide repo_view.working_dir)
        elif err:
            raise err

        return doc, cacheable
