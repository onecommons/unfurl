from dataclasses import dataclass
from functools import partial
import json
import os
import time
from typing import Dict, List, Optional, Tuple, Any, Union, TYPE_CHECKING, cast, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit
from base64 import b64decode

import click
import uvicorn
from flask import Flask, current_app, jsonify, request
from flask_caching import Cache

import git
from .localenv import LocalEnv
from .repo import GitRepo
from .util import UnfurlError, get_package_digest
from .logs import getLogger, add_log_file
from .yamlmanifest import YamlManifest
from . import to_json
from . import init
from . import __version__
if TYPE_CHECKING:
    from git.objects import Commit

__logfile = os.getenv("UNFURL_LOGFILE")
if __logfile:
    add_log_file(__logfile)
logger = getLogger("unfurl.server")

# note: export FLASK_ENV=development to see error stacks
# see https://flask-caching.readthedocs.io/en/latest/#built-in-cache-backends for more options
flask_config: Dict[str, Any] = {
    "CACHE_TYPE": os.environ.get('CACHE_TYPE', "simple"),
    "CACHE_KEY_PREFIX": os.environ.get("CACHE_KEY_PREFIX", "ufsv::"),
}
# default: never cache entries never expire
flask_config["CACHE_DEFAULT_TIMEOUT"] = int(os.environ.get("CACHE_DEFAULT_TIMEOUT") or 0)
if flask_config["CACHE_TYPE"] == "RedisCache":
    if "CACHE_REDIS_URL" in os.environ:
        flask_config["CACHE_REDIS_URL"] = os.environ['CACHE_REDIS_URL']
    else:
        flask_config["CACHE_REDIS_HOST"] = os.environ['CACHE_REDIS_HOST']
        flask_config["CACHE_REDIS_PORT"] = int(os.environ.get('CACHE_REDIS_PORT') or 6379)
        flask_config["CACHE_REDIS_DB"] = int(os.environ.get('CACHE_REDIS_DB') or 0)

app = Flask(__name__)
app.config.from_mapping(flask_config)
cache = Cache(app)
app.config["UNFURL_OPTIONS"] = {}
app.config["UNFURL_CLONE_ROOT"] = os.getenv("UNFURL_CLONE_ROOT") or "."
app.config["UNFURL_CLOUD_SERVER"] = os.getenv("UNFURL_CLOUD_SERVER")
app.config["UNFURL_SECRET"] = os.getenv("UNFURL_SERVE_SECRET")


def get_project_id(request):
    project_id = request.args.get("auth_project")
    if project_id:
        return project_id
    return ""


def _get_project_repo_dir(project_id: str) -> str:
    clone_root = current_app.config.get("UNFURL_CLONE_ROOT", ".")
    return os.path.join(clone_root, project_id)


def _get_project_repo(project_id: str) -> Optional[GitRepo]:
    path = _get_project_repo_dir(project_id)
    if os.path.isdir(path):
        return GitRepo(git.Repo(path))
    return None


# cache value, last_commit (on the file_path), latest_commit (seen in branch)
CacheValueType = Tuple[Any, str, str]
_cache_inflight_sleep_duration = 0.2
_cache_inflight_timeout = float(os.getenv("UNFURL_SERVE_CACHE_TIMEOUT") or 120)  # should match request timeout

# XXX support dependencies on multiple repositories (e.g. unfurl-types):
# in get_and_set(), have work() return (and set_cache receives) a list of (project_id, branch, latest_commit) instead of `latest_commit`
# get_cache would have to check all of them (and unless all latest_commits are passed to get_cache it will have check every time)
# (and the request would still need to send the root latest_commit)
# to mitigate cache misses on multi-use/monolithic repos use branches to partition uses? but that would require a lot of branch management for interdependent code.

# more sophisticated option: allow tag refs as the last_commit, so cache.get() has a semantic versioned tags as latest_commits
# if the primary latest_commit isn't out of date, we assume the tags are accurate
# use git describe to get the latest tag on the dependent repo's branch
# and then check if the latest tag is compatible using semantic versioning to decide if the cached version is out of date

@dataclass
class CacheEntry:
    project_id: str
    branch: Optional[str]
    file_path: str  # relative to project root
    key: str
    repo: Optional[GitRepo] = None
    commitinfo: Optional["Commit"] = None
    hit: Optional[bool] = None

    def _set_project_repo(self) -> Optional[GitRepo]:
        self.repo = _get_project_repo(self.project_id)
        return self.repo

    def cache_key(self) -> str:
        return f"{self.project_id}:{self.branch or ''}:{self.file_path}:{self.key}"

    def _inflight_key(self) -> str:
        return "_inflight::" + self.cache_key()

    def delete_cache(self, cache) -> bool:
        full_key = self.cache_key()
        logger.info("deleting from cache: %s", full_key)
        return cache.delete(full_key)

    def set_cache(self, cache, latest_commit: str, value: Any) -> str:
        full_key = self.cache_key()
        logger.info("setting cache with %s", full_key)
        last_commit = self.commitinfo and self.commitinfo.hexsha
        if not last_commit:
            if not self.repo:
                self._set_project_repo()
            assert self.repo
            commits = list(self.repo.repo.iter_commits(self.branch, [self.file_path], max_count=1))
            if not commits:  # missing file
                self.commitinfo = False
                last_commit = ""
            else:
                self.commitinfo = commits[0]
                last_commit = self.commitinfo.hexsha
        assert isinstance(last_commit, str)
        cache.set(full_key, (value, last_commit, latest_commit))
        return last_commit

    def _pull_if_missing_commit(self, commit: str):
        if not self.repo:
            self._set_project_repo()
        if not self.repo:
            return  # we don't have a local copy of the repo
        try:
            self.repo.repo.commit(commit)
        except ValueError:
            # newer not in repo, repo probably is out of date
            self.repo.pull()

    def is_commit_older_than(self, older, newer):
        if older == newer:
            return False
        self._pull_if_missing_commit(newer)
        assert self.repo
        # if "older..newer" is true iter_commits (git rev-list) will list
        # newer commits up to and including "newer", newest first
        # otherwise the list will be empty
        if list(self.repo.repo.iter_commits(f"{older}..{newer}", max_count=1)):
            return True
        return False

    def get_cache(self, cache, latest_commit: str) -> Tuple[Any, Union[bool, "Commit"]]:
        """Look up a cached value and then check if it out of date by checking if the file path in the key was modified after the given commit
        (also store the last_commit so we don't have to do that check everytime)
        we assume latest_commit is the last commit the client has seen but it might be older than the local copy
        """
        full_key = self.cache_key()
        value = cast(CacheValueType, cache.get(full_key))
        if not value:
            logger.info("cache miss for %s", full_key)
            self.hit = False
            return None, False  # cache miss
        response, last_commit, cached_latest_commit = value
        if not latest_commit or latest_commit == cached_latest_commit:
            # this is the latest (or we aren't checking)
            logger.info("cache hit for %s with %s", full_key, latest_commit)
            self.hit = True
            return response, True
        else:
            # cache might be out of date, let's check by getting the commit info for the file path
            try:
                cached_is_older = self.is_commit_older_than(cached_latest_commit, latest_commit)
            except Exception:
                logger.info("cache hit for %s, but error with client's commit %s", full_key, latest_commit)
                # got an error resolving latest_commit, just return the cached value
                self.hit = True
                return response, True
            if not cached_is_older:
                # the client has an older commit than the cache had, so treat as a cache hit
                logger.info("cache hit for %s with %s", full_key, latest_commit)
                self.hit = True
                return response, True

            assert self.repo
            # the latest_commit is newer than the cached_latest_commit, check if the file has changed
            commits = list(self.repo.repo.iter_commits(self.branch, [self.file_path], max_count=1))
            if commits:
                self.commitinfo = commits[0]
                new_commit = self.commitinfo.hexsha
            else:
                # file doesn't exist
                new_commit = ""  # not found
                self.commitinfo = False  # treat as cache miss
            if new_commit == last_commit:
                # the file hasn't changed, let's update the cache with latest_commit so we don't have to do this check again
                cache.set(full_key, (response, last_commit, latest_commit))
                logger.info("cache hit for %s, updated %s", full_key, latest_commit)
                self.hit = True
                return response, True
            else:
                # stale -- up to the caller to do something about it, e.g. update or delete the key
                logger.info("stale cache hit for %s with %s", full_key, latest_commit)
                return response, self.commitinfo

    def _set_inflight(self, cache, latest_commit) -> Tuple[Any, Union[bool, "Commit"]]:
        inflight = cache.get(self._inflight_key())
        if inflight:
            inflight_commit, start_time = inflight
            # if inflight_commit is older than latest_commit, do the work anyway otherwise wait for the inflight value
            if not self.is_commit_older_than(inflight_commit, latest_commit):
                # keep checking inflight key until it is deleted
                # or if been inflight longer than timeout, assume its work aborted and stop waiting
                while time.time() - start_time < _cache_inflight_timeout:
                    time.sleep(_cache_inflight_sleep_duration)
                    if not cache.get(self._inflight_key()):
                        # no longer in flight
                        value, commitinfo = self.get_cache(cache, inflight_commit)
                        if commitinfo:  # hit, use this instead of doing our work
                            return value, commitinfo
                        break  # missing, so inflight work must have failed, continue with our work

        cache.set(self._inflight_key(), (latest_commit, time.time()))
        return None, False

    def _cancel_inflight(self, cache):
        return cache.delete(self._inflight_key())

    def get_or_set(self, cache, work: Callable, latest_commit: str) -> Tuple[Optional[Any], Any]:
        value, commitinfo = self.get_cache(cache, latest_commit)
        if commitinfo:
            if commitinfo is True:
                return None, value
            # otherwise in cache but stale, fall thru to redo work
            # XXX? check date to see if its recent enough to serve anyway
            # if commitinfo.committed_date - time.time() < stale_ok_age:
            #      return value
            self.hit = False
        else:  # cache miss
            self._pull_if_missing_commit(latest_commit)

        value, found_inflight = self._set_inflight(cache, latest_commit)
        if found_inflight:
            # there was already work inflight and use that instead
            return None, value

        try:
            err, value = work(self, latest_commit)
        except Exception as exc:
            logger.error("unexpected error doing work for cache", exc_info=True)
            err, value = exc, None

        found_inflight = self._cancel_inflight(cache)
        # if we not found_inflight that means invalidate_cache deleted it so we should cache the result 
        if found_inflight and not err:
            self.set_cache(cache, latest_commit, value)
        return err, value


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


@app.route("/version")
def version():
    return f"{__version__(True)} ({get_package_digest()})"


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


def _stage(project_id: str, args: dict) -> Optional[str]:
    # Default to exporting the ensemble provided to the server on startup
    repo = None
    repo_path = _get_project_repo_dir(project_id)
    repo = _get_project_repo(project_id)
    # repo doesn't exists, clone it
    if repo:
        logger.info(f"found repo at {repo.working_dir}")
    else:
        os.makedirs(os.path.dirname(repo_path), exist_ok=True)
        git_url = get_project_url(project_id, args.get("username"), args.get("password"))
        result = init.clone(
            git_url,
            repo_path,
        )
        logger.info(f"cloned {git_url}: {result} in pid {os.getpid()}")
        repo = _get_project_repo(project_id)
        if repo:
            logger.info("clone success: %s to %s", git_url, repo.working_dir)
    return repo and repo.working_dir or None


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


def _cache_work(args: dict, cache_entry: CacheEntry, latest_commit: str) -> Any:
    return _do_export(cache_entry.project_id, cache_entry.key, cache_entry.file_path, cache_entry, latest_commit, args)


def _make_etag(latest_commit: str):
    return f'W/"{latest_commit}"'


# /export?format=environments&include_deployments=true&latest_commit=foo&project_id=bar&branch=main
@app.route("/export")
def export():
    requested_format = request.args.get("format", "deployment")
    if requested_format not in ["blueprint", "environments", "deployment"]:
        return create_error_response(
            "BAD_REQUEST",
            "Query parameter 'format' must be one of 'blueprint', 'environments' or 'deployment'",
        )

    latest_commit = request.args.get("latest_commit")
    project_id = get_project_id(request)
    deployment_path = request.args.get("deployment_path") or ""
    cache_entry = None
    file_path = _get_filepath(requested_format, deployment_path)
    branch = request.args.get("branch")
    if request.headers.get("X-Git-Credentials"):
        args = dict(request.args)
        args["username"], args["password"] = b64decode(request.headers["X-Git-Credentials"]).decode().split(":", 1)
    else:
        args = request.args
    if latest_commit is not None:
        cache_entry = CacheEntry(project_id, branch, file_path, requested_format)
        err, json_summary = cache_entry.get_or_set(cache, partial(_cache_work, args), latest_commit)
    else:
        err, json_summary = _do_export(project_id, requested_format, file_path, None, None, args)
    if not err:
        hit = cache_entry and cache_entry.hit
        if request.args.get("include_all_deployments"):
            deployments = []
            for manifest_path in json_summary["DeploymentPath"]:
                dcache_entry = CacheEntry(project_id, branch, manifest_path, "deployment")
                workfn = partial(_cache_work, request.args)
                derr, djson = dcache_entry.get_or_set(cache, workfn, latest_commit)
                if not derr:
                    deployments.append(djson)
                    hit = hit and dcache_entry.hit
            json_summary["deployments"] = deployments
        if hit:
            etag = request.headers.get("If-None-Match")
            if latest_commit and _make_etag(latest_commit) == etag:
                return "Not Modified", 304
        response = jsonify(json_summary)
        if latest_commit:
            response.headers["Etag"] = _make_etag(latest_commit)
        return response
    else:
        if isinstance(err, Exception):
            return create_error_response("INTERNAL_ERROR", "An internal error occurred")
        else:
            return err


@app.route("/populate_cache")
def populate_cache():
    project_id = get_project_id(request)
    branch = request.args.get("branch")
    path = request.args["path"]
    latest_commit = request.args["latest_commit"]
    requested_format = format_from_path(path)
    removed = request.args.get("removed")
    cache_entry = CacheEntry(project_id, branch, path, requested_format)
    if removed:
        cache_entry.delete_cache(cache)
        return "OK"
    project_dir = _get_project_repo_dir(project_id)
    if not os.path.isdir(project_dir):
        # don't try to clone private repository
        if request.args.get("visibility") != "public":
            logger.info("skipping populate cache for private repository %s", project_id)
            return "OK"
    err, json_summary = cache_entry.get_or_set(cache, partial(_cache_work, request.args), latest_commit)
    if err:
        return err
    else:
        return "OK"


def _make_readonly_localenv(clone_location, parent_localenv=None):
    try:
        # we don't want to decrypt secrets because the export is cached and shared
        overrides = dict(UNFURL_SKIP_VAULT_DECRYPT=True, UNFURL_SKIP_UPSTREAM_CHECK=True)
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


def _do_export(project_id: str, requested_format: str, deployment_path: str,
               cache_entry: CacheEntry, latest_commit: str, args: dict) -> Tuple[Optional[Any], Optional[str]]:
    if project_id:
        if cache_entry and cache_entry.commitinfo:
            repo = GitRepo(cache_entry.commitinfo.repo)
            clone_location: Optional[str] = os.path.join(repo.working_dir, deployment_path)
        else:
            clone_location = _fetch_localenv_location(project_id, deployment_path, args)
            if clone_location is None:
                return create_error_response(
                    "INTERNAL_ERROR", "Could not find repository"
                ), None
    else:  # use the current ensemble
        clone_location = current_app.config.get("UNFURL_ENSEMBLE_PATH") or "."

    # load the ensemble
    if cache_entry:
        # don't pass latest_commit because localenv doesn't care
        err, parent_localenv = CacheEntry(project_id, cache_entry.branch, "unfurl.yaml", "localenv"
                                          ).get_or_set(
            cache, lambda *args: _make_readonly_localenv(clone_location), "")
    else:
        err, parent_localenv = None, None
    if not err:
        err, local_env = _make_readonly_localenv(clone_location, parent_localenv)
        if args.get("environment"):
            local_env.manifest_context_name = args["environment"]

    if err:
        return create_error_response("INTERNAL_ERROR", "An internal error occurred"), None

    exporter = getattr(to_json, "to_" + requested_format)
    json_summary = exporter(local_env)
    return None, json_summary


def _get_body(request):
    body = request.json
    if request.headers.get("X-Git-Credentials"):
        body["username"], body["password"] = b64decode(request.headers["X-Git-Credentials"]).decode().split(":", 1)
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
    return _patch_ensemble(body, True, project_id)


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


def _make_requirement(dependency) -> dict:
    req = dict(node=dependency.get("match"))
    if "constraint" in dependency and "visibility" in dependency["constraint"]:
        req["metadata"] = dict(visibility=dependency["constraint"]["visibility"])
    return req


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
            requirements = [{dependency["name"]: _make_requirement(dependency)}
                            for dependency in value if "match" in dependency]
            if requirements or "requirements" in tpl:
                tpl["requirements"] = requirements


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


def _patch_response(repo: GitRepo):
    return jsonify(dict(commit=repo.revision))


def _patch_environment(body: dict, project_id: str) -> str:
    patch = body.get("patch")
    assert isinstance(patch, list)
    clone_location = _fetch_localenv_location(project_id, "", body)
    if clone_location is None:
        # XXX create a new ensemble if patch is for a new deployment
        return create_error_response("INTERNAL_ERROR", "Could not find repository")
    invalidate_cache(body, "environments", project_id)
    localEnv = LocalEnv(clone_location, can_be_empty=True)
    assert localEnv.project
    repo = localEnv.project.project_repoview.repo
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
            update_deployment(localEnv.project, patch_inner["name"], patch_inner, False, deleted)
    localConfig.config.save()
    commit_msg = body.get("commit_msg", "Update environment")
    err = _commit_and_push(repo, localConfig.config.path, commit_msg)
    if err:
        return err  # err will be an error response
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
        logger.debug(f"invalidate cache: cancel inflight {entry.cache_key()}: {was_inflight}")
        return success
    return False


def _patch_ensemble(body: dict, create: bool, project_id: str) -> str:
    patch = body.get("patch")
    assert isinstance(patch, list)
    environment = body.get("environment") or ""  # cloud_vars_url need the ""!
    deployment_path = body.get("deployment_path") or ""
    clone_location = _fetch_localenv_location(project_id, deployment_path, body)
    if clone_location is None:
        # XXX create a new ensemble if patch is for a new deployment
        return create_error_response("INTERNAL_ERROR", "Could not find repository")
    assert clone_location
    invalidate_cache(body, "deployment", project_id)
    deployment_blueprint = body.get("deployment_blueprint")
    if create:
        blueprint_url = body["blueprint_url"]
        logger.info("creating deployment at %s for %s", clone_location, blueprint_url)
        msg = init.clone(blueprint_url, clone_location, existing=True, mono=True, skeleton="dashboard",
                         use_environment=environment, use_deployment_blueprint=deployment_blueprint)
        logger.info(msg)
    # elif clone:
    #     logger.info("creating deployment at %s for %s", clone_location, blueprint_url)
    #     msg = init.clone(clone_location, clone_location, existing=True, mono=True, skeleton="dashboard",
    #                      use_environment=environment, use_deployment_blueprint=deployment_blueprint)
    #     logger.info(msg)

    # don't validate in case we are still an incomplete draft
    cloud_vars_url = body.get("cloud_vars_url") or ""
    overrides = dict(ENVIRONMENT=environment, UNFURL_CLOUD_VARS_URL=cloud_vars_url)
    manifest = LocalEnv(clone_location, overrides=overrides).get_manifest(skip_validation=True)
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
                        doc = doc[key]
                else:
                    doc = doc.setdefault(key, {})
            if not deleted:
                _patch_node_template(patch_inner, doc)

    manifest.manifest.save()
    manifest.add_all()
    commit_msg = body.get("commit_msg", "Update deployment")
    # XXX catch exception from commit and run git restore to rollback working dir
    committed = manifest.commit(commit_msg, False)
    logger.info(f"committed to {committed} repositories")
    if manifest.repo.repo.remotes:
        try:
            manifest.repo.repo.remotes.origin.push()
            logger.info("pushed")
        except Exception:
            # discard the last commit that we couldn't push
            # this is mainly for security if we couldn't push because the user wasn't authorized
            manifest.repo.reset()
            logger.error("push failed", exc_info=True)
            return create_error_response("INTERNAL_ERROR", "Could not push repository")
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


def _commit_and_push(repo, full_path, commit_msg):
    repo.add_all(full_path)
    # XXX catch exception and run git restore to rollback working dir
    repo.commit_files([full_path], commit_msg)
    logger.info("committed %s: %s", full_path, commit_msg)
    if repo.repo.remotes:
        try:
            repo.repo.remotes.origin.push()
            logger.info("pushed")
        except Exception:
            # discard the last commit that we couldn't push
            # this is mainly for security if we couldn't push because the user wasn't authorized
            repo.repo.reset()
            logger.error("push failed", exc_info=True)
            return create_error_response("INTERNAL_ERROR", "Could not push repository")
    return None


def _fetch_localenv_location(project_path: str, deployment_path: str, args: dict) -> Optional[str]:
    # Repository URL
    # Project is external
    if not project_path or project_path == ".":
        # get the local ensemble
        clone_location = current_app.config.get("UNFURL_ENSEMBLE_PATH") or "."
    else:
        clone_location = _stage(project_path, args)
        if not clone_location:
            return clone_location
    # XXX: deployment_path must be in the project repo, split repos are not supported
    # we want the caching and staging infrastructure to only know about git, not unfurl projects
    # so we can't reference a file path outside of the git repository
    return os.path.join(clone_location, deployment_path)


def create_error_response(code, message):
    http_code = 400  # Default to BAD_REQUEST
    if code == "BAD_REQUEST":
        http_code = 400
    elif code == "UNAUTHORIZED":
        http_code = 401
    elif code == "INTERNAL_ERROR":
        http_code = 500
    return jsonify({"code": code, "message": message}), http_code


# UNFURL_SKIP_UPSTREAM_CHECK=1 UNFURL_HOME="" gunicorn -w 4 unfurl.server:app
def serve(
    host: str,
    port: int,
    secret: str,
    clone_root: str,
    project_or_ensemble_path: click.Path,
    options: dict,
    cloud_server=None
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
    app.config["UNFURL_CLOUD_SERVER"] = cloud_server or os.getenv("UNFURL_CLOUD_SERVER")

    # Start one WSGI server
    uvicorn.run(app, host=host, port=port, interface="wsgi", log_level=logger.getEffectiveLevel())

    # app.run(host=host, port=port)
    # gunicorn"  , "-b", "0.0.0.0:5000", "unfurl.server:app"
    # from gunicorn.app.wsgiapp import WSGIApplication
    # WSGIApplication().run()
