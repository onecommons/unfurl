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
from urllib.parse import urljoin, urlparse
import os

from ..cloudmap import Repository, RepositoryDict
from ..logs import getLogger
from ..graphql import ImportDef, ResourceTypesByName

from toscaparser.elements.entity_type import Namespace

from flask import current_app
from .serve import (
    CacheEntry,
    Cache,
    _get_local_project_dir,
    _get_project_repo,
    cache,
    DEFAULT_BRANCH,
    project_id_from_urlresult,
)
from ..repo import (
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
from ..yamlloader import ImportResolver_Context, SimpleCacheResolver
from ..packages import is_semver

logger = getLogger("unfurl.server")


def load_yaml(
    project_id, branch, file_name, root_entry=None, latest_commit=None
) -> Tuple[Optional[Any], Any]:
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
        stale_pull_age=current_app.config["CACHE_DEFAULT_PULL_TIMEOUT"],
        do_clone=True,
        root_entry=root_entry,
    )
    # this will add this cache_dep to the root_cache_request's value
    # XXX create package from url, branch and latest_commit to decide if a cache_dep is need
    dep = cache_entry.make_cache_dep(cache_entry.stale_pull_age, None)
    return cache_entry.get_or_set(cache, _work, latest_commit, cache_dependency=dep)


def get_cloudmap_types(project_id: str, root_cache_entry: CacheEntry):
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
        stale_pull_age=current_app.config["CACHE_DEFAULT_PULL_TIMEOUT"],
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
        timeout = current_app.config["CACHE_DEFAULT_REMOTE_TAGS_TIMEOUT"]
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
        return current_app.config["CACHE_TYPE"] != "simple"

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
