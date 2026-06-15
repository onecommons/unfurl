# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
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
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import os

from ..cloudmap import CloudMapDB, EntitySchema
from ..tosca_plugins.cloudmap_defs import CloudMapView
from ..logs import getLogger
from ..graphql import ImportDef, ResourceType, ResourceTypesByName

from toscaparser.elements.entity_type import Namespace

from flask import current_app, has_app_context, request
import requests
from .serve import (
    CacheEntry,
    CacheError,
    Cache,
    _get_local_project_dir,
    _get_project_repo,
    get_cache,
    DEFAULT_BRANCH,
    project_id_from_urlresult,
)

from ..repo import (
    RepoView,
    add_user_to_url,
    normalize_git_url_hard,
    split_git_url,
    get_remote_tags,
)
from ..yamlloader import (
    ImportResolver_Context,
    SimpleCacheResolver,
    get_tags_from_proxy,
)
from ..packages import is_semver

logger = getLogger("unfurl.server")
CLOUDMAP_BRANCH = "main"


def load_yaml_from_cache(
    project_id: str,
    branch: str,
    file_name: str,
    root_entry: Optional["CacheEntry"] = None,
    latest_commit: Optional[str] = None,
) -> Tuple[CacheError, Any]:
    from toscaparser.utils.yamlparser import load_yaml

    def _work(
        cache_entry: CacheEntry, latest_commit: Optional[str]
    ) -> Tuple[CacheError, Any, bool]:
        path = os.path.join(cache_entry.checked_repo.working_dir, cache_entry.file_path)
        doc = load_yaml(path)
        return None, doc, True

    cache_entry = CacheEntry(
        project_id,
        branch,
        file_name,
        "load_yaml",
        stale_pull_age=current_app.config["CACHE_DEFAULT_PULL_TIMEOUT"],
        do_clone=True,
        root_entry=root_entry,
    )
    # this will add this cache_dep to the root_cache_request's value
    # XXX create package from url, branch and latest_commit to decide if a cache_dep is need
    dep = cache_entry.make_cache_dep(cache_entry.stale_pull_age, None)
    cache = get_cache()
    if cache:
        return cache_entry.get_or_set(cache, _work, latest_commit, cache_dependency=dep)
    else:
        return _work(cache_entry, latest_commit)[:2]


def load_cloudmap_local(
    project_id: str,
    branch: str = CLOUDMAP_BRANCH,
    file_name: str = "cloudmap.yaml",
    root_entry: Optional["CacheEntry"] = None,
    latest_commit: Optional[str] = None,
    validate: bool = False,
    create_db: bool = True,
) -> Tuple[CacheError, Optional[Dict[str, Any]], Optional[CloudMapDB]]:
    """Load ``file_name`` from cache and (optionally) wrap it in a
    :class:`CloudMapDB`.

    Returns ``(err, doc, db)``. When the YAML couldn't be loaded, ``doc``
    and ``db`` are both ``None``. When ``create_db`` is ``False`` the
    document is loaded but ``db`` is left as ``None``; callers that only
    need the raw doc can pass ``create_db=False`` to skip the
    ``CloudMapDB`` construction.
    """
    err, doc = load_yaml_from_cache(
        project_id, branch, file_name, root_entry, latest_commit
    )
    if doc is None or not create_db:
        return err, doc, None
    return err, doc, CloudMapDB("", doc, validate)


def get_cloudmap_view(
    project_id: str,
    branch: str = CLOUDMAP_BRANCH,
    file_name: str = "cloudmap.yaml",
    root_entry: Optional["CacheEntry"] = None,
    latest_commit: Optional[str] = None,
    validate: bool = False,
) -> Tuple[CacheError, Optional[CloudMapView]]:
    """Load ``file_name`` from cache and wrap it in a :class:`CloudMapDB`.

    When the app is configured with ``UNFURL_LOCAL_CLOUDMAP_URL`` (the local
    Rust proxy server has a cloudmap-sync DB attached) read access is
    routed through a :class:`CloudMapProxy` against that URL instead of
    a local YAML clone — the Rust process owns the authoritative
    cloudmap.

    Returns ``(err, db)``. When the YAML couldn't be loaded, ``db``
    is ``None``.
    """
    # called by /get_types and /graph because they have python only logic.
    syncing_url = (
        current_app.config.get("UNFURL_LOCAL_CLOUDMAP_URL")
        if has_app_context()
        else None
    )
    if syncing_url:
        from ..cloudmap.proxy import CloudMapProxy

        # Forward auth_project and latest_commit so the rust handler
        # can scope/pin its read against the same project + commit the
        # caller is asking about. CloudMapProxy preserves query params
        # from base_url on every request.
        extra: List[Tuple[str, str]] = []
        if project_id:
            extra.append(("auth_project", project_id))
        if latest_commit:
            extra.append(("latest_commit", latest_commit))
        if extra:
            parsed = urlparse(syncing_url)
            existing = parse_qsl(parsed.query, keep_blank_values=True)
            new_query = urlencode(existing + extra)
            syncing_url = urlunparse(parsed._replace(query=new_query))
        # Forward auth headers from the inbound request onto the
        # session so they're attached to every request the proxy makes.
        session = requests.Session()
        for header in ("X-Git-Credentials", "Authorization", "WWW-Authenticate"):
            value = request.headers.get(header)
            if value:
                session.headers[header] = value
        logger.verbose(
            "routing CloudMapView through CloudMapProxy at %s (forwarded headers: %s)",
            syncing_url,
            sorted(session.headers.keys()),
        )
        return None, CloudMapProxy(syncing_url, session=session, logger=logger)

    err, doc = load_yaml_from_cache(
        project_id, branch, file_name, root_entry, latest_commit
    )
    if doc is None:
        return err, None
    return err, CloudMapDB("", doc, validate)


def get_cloudmap_types(
    project_id: str, root_cache_entry: CacheEntry, validate: bool = False
) -> Tuple[CacheError, Dict[str, ResourceType]]:
    err, db = get_cloudmap_view(
        project_id, root_entry=root_cache_entry, validate=validate
    )
    if db is None:
        return err, {}
    return err, _get_cloudmap_types(db)


def _get_cloudmap_types(
    db: CloudMapView,
) -> Dict[str, ResourceType]:
    types: Dict[str, ResourceType] = {}
    for artifact in db.find_artifacts(EntitySchema.CloudBlueprint):
        repo = db.get_repository(artifact.url)
        if repo:
            git_url = repo.git_url()
        else:
            git_url = artifact.url.replace("git://", "https://", 1)
        # Iterate through each type that this artifact instantiates
        for type_refs in artifact.instantiates.values():
            if not type_refs:
                continue
            for type_name in type_refs.types:
                cloud_type = db.get_type(type_name)
                if cloud_type:
                    name = cloud_type.name
                    local_types = ResourceTypesByName(git_url, Namespace({}, ""))
                    if "@" not in name:
                        name = local_types.expand_typename(name)
                    # make sure "extends" are fully qualified
                    if cloud_type.extends:
                        extends = [
                            local_types.expand_typename(extend)
                            for extend in cloud_type.extends
                        ]
                    else:
                        extends = []
                    file = split_git_url(artifact.url)[1]
                    resource_type = ResourceType(
                        __typename="ResourceType",
                        name=name,
                        requirements=[],
                        extends=extends,
                        title=cloud_type.metadata.title
                        or name.split(".")[-1],  # short, readable name
                        _sourceinfo=ImportDef(file=file, url=git_url, incomplete=True),
                        inputsSchema={},
                    )
                    if artifact.metadata.description:
                        resource_type["description"] = artifact.metadata.description
                    # XXX hack, always set for root type:
                    resource_type["implementations"] = ["connect", "create"]
                    resource_type["directives"] = ["substitute"]
                    thumbnail = artifact.metadata.thumbnail_url
                    if thumbnail:
                        resource_type["icon"] = thumbnail
                    dependencies = artifact.dependencies
                    if dependencies:
                        components = []
                        for typeref in dependencies.values():
                            if typeref:
                                components.extend(typeref.names())
                        resource_type.setdefault("metadata", {})["components"] = (
                            components
                        )
                    types[name] = resource_type
    return types


def get_working_dir(project_id, branch, file_name, root_entry=None, latest_commit=None):
    def _work(
        cache_entry: CacheEntry, latest_commit: Optional[str]
    ) -> Tuple[CacheError, Any, bool]:
        path = os.path.join(cache_entry.checked_repo.working_dir, cache_entry.file_path)
        return None, path, True

    def _validate(
        working_dir, entry: CacheEntry, cache: Cache, latest_commit: Optional[str]
    ) -> bool:
        return os.path.isdir(working_dir)

    cache = get_cache()
    assert cache
    cache_entry = CacheEntry(
        project_id,
        branch,
        file_name,
        "working_dir",
        stale_pull_age=current_app.config["CACHE_DEFAULT_PULL_TIMEOUT"],
        do_clone=True,
        root_entry=root_entry,
    )
    return cache_entry.get_or_set(cache, _work, latest_commit, _validate)


def get_remote_tags_cached(url, pattern, args) -> List[str]:
    key = normalize_git_url_hard(url)
    tags = None
    cache = get_cache()
    if cache is not None:
        tags = cast(Optional[List[str]], cache.get("tags:" + key + ":" + pattern))
    if tags is not None:
        return tags
    else:
        private = False
        base_url = current_app.config["UNFURL_CLOUD_SERVER"] and normalize_git_url_hard(
            current_app.config["UNFURL_CLOUD_SERVER"]
        )
        if args and base_url and key.startswith(base_url):
            # repository on this server, apply credentials if present
            username, password = (
                args.get("username"),
                args.get("private_token", args.get("password")),
            )
            if username and password:
                private = True
                url = add_user_to_url(url, username, password)
        if not private:
            tags = get_tags_from_proxy(url, pattern)
        if tags is None:
            tags = get_remote_tags(url, pattern)
        timeout = current_app.config["CACHE_DEFAULT_REMOTE_TAGS_TIMEOUT"]
        if cache:
            cache.set("tags:" + key + ":" + pattern, tags, timeout)
        return tags


class ServerCacheResolver(SimpleCacheResolver):
    _safe_mode: bool = True
    root_cache_request: Optional[CacheEntry] = None
    args: Optional[dict] = None

    @classmethod
    def make_factory(
        cls,
        root_cache_request: Optional[CacheEntry],
        credentials: Optional[dict] = None,
    ):
        gui_mode = bool(current_app.config.get("UNFURL_GUI_MODE"))

        def ctor(*args, **kw):
            resolver = cls(*args, **kw)
            resolver.root_cache_request = root_cache_request
            resolver._safe_mode = not gui_mode
            if credentials:
                resolver.args = credentials
            else:
                assert root_cache_request
                resolver.args = root_cache_request.args
            return resolver

        return ctor

    def get_remote_tags(self, url, pattern="*") -> Optional[List[str]]:
        if self.local_env and self.local_env.overrides.get(
            "UNFURL_SKIP_UPSTREAM_CHECK"
        ):
            local_projects = current_app.config.get("UNFURL_LOCAL_PROJECTS")
            if local_projects:
                try:
                    project_id = project_id_from_urlresult(urlparse(url))
                except Exception:
                    project_id = None
                if project_id and local_projects.get(project_id):
                    return None
            if self.local_env.find_repo(url):
                return None
        return get_remote_tags_cached(url, pattern, self.args)

    @property
    def use_local_cache(self) -> bool:
        return current_app.config["CACHE_TYPE"] != "simple"

    def find_repository_path(
        self,
        name: str,
        tpl: Optional[Dict[str, Any]] = None,
        base_path: Optional[str] = None,
    ) -> Optional[str]:
        """Return the tosca_repository path for the given repository name, or None if not found."""
        repo_view = self._match_repoview(name, tpl)
        if not repo_view:
            return self._check_existing_tosca_repository_path(name, base_path, tpl)
        base_url = current_app.config["UNFURL_CLOUD_SERVER"]
        private = not base_url or (
            repo_view and not repo_view.url.startswith(base_url) or repo_view.repo
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
            # XXX should we create a tosca_repository link to this path?
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
        if current_app.config.get("UNFURL_GUI_MODE"):
            private = True
        else:
            private = not repo_view.url.startswith(base_url)
            project_id = project_id_from_urlresult(urlparse(repo_view.url))
            if private:
                logger.trace(
                    f"load yaml {file_name} for {url} isn't on {base_url}, skipping cache."
                )
            elif repo_view.repo or _get_local_project_dir(project_id):
                private = True
                logger.trace(
                    f"load yaml {file_name} for {url}: local repository found."
                )
        if private:
            return super().load_yaml(url, fragment, ctx)

        # if the repo is private, use the base implementation
        # otherwise use the server cache to resolve the url to a local repo clone and load the file from it
        # and track its cache entry as a dependency on the root cache entry

        def _work(
            cache_entry: CacheEntry, latest_commit: Optional[str]
        ) -> Tuple[CacheError, Any, bool]:
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
                stale_pull_age=current_app.config["CACHE_DEFAULT_PULL_TIMEOUT"],
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
            cache = get_cache()
            assert cache
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
        elif isinstance(err, Exception):
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
