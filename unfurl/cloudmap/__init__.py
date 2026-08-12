# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
"""
A cloud map is document containing metadata on collections of repositories including the artifacts and blueprints they contain.

You can use a cloud map to manage servers that host git repositories and synchronize mirrors of git repositories.
Three types of repository hosts are currently supported: local, gitlab, and unfurl.cloud.

You can synchronize multiple instances of the same repository host by setting the "canonical_url" key in the repository host configuration.

For example, given this configuration snippet:

```yaml
environments:
  defaults:
    cloudmaps:
      repositories:
        cloudmap:
          url: file:../cloudmap
          clone_root: ../repos
      hosts:
        staging:
            type: unfurl.cloud
            url: https://staging.unfurl.cloud/onecommons/
            canonical_url: https://unfurl.cloud
        production:
            type: unfurl.cloud
            url: https://unfurl.cloud/onecommons/
            visibility: public
```

then projects found on staging.unfurl.cloud will be saved in the cloudmap as belonging to unfurl.cloud.
So following commands will push all the projects in "onecommons/blueprints" namespace from the staging instance to the production instance at unfurl.cloud:

```bash
# first sync latest on staging with the cloudmap
unfurl cloudmap --sync staging --namespace onecommons/blueprints

# now sync the cloudmap with production
unfurl cloudmap --sync production --namespace onecommons/blueprints
```

These commands commit any changes to cloudmap.yaml to its local clone of the cloudmap git repository.
It will also create branches for each repository to host record their last known state and the
"main" branch serves as the "source of truth" for the cloud map.

Currently you need manually push updates to cloudmap to the upstream cloudmap repository, for example:

`cd cloudmap; git --push origin main`
"""

from dataclasses import dataclass, field, asdict, InitVar
from operator import attrgetter
from pathlib import Path
import tempfile
import os
import os.path
from typing import (
    Iterator,
    Optional,
    List,
    Dict,
    Sequence,
    Set,
    Tuple,
    Type,
    cast,
)
from typing_extensions import Required, Literal, Protocol
from urllib.parse import ParseResult, urlparse, quote
import git
import git.cmd
from git.objects import IndexObject

from ..projectpaths import _getdir
from ..tosca_plugins.cloudmap_defs import (
    HostConfig,
    CloudMapInputs,
    CloudMapRecord,
    LocalHostConfig,
    Analyzer,
    URLAnalyzer,
    PipelineRunAnalyzer,
    Artifact,
    EntitySchema,
    RepositoryAnalyzer,
    Repository,
    Service,
    TypedUrls,
    TypeRefs,
    get_repository_url,
    build_oci_purl,
    AnalyzerContext,
)
from ..support import ContainerImageParts
from ..configurator import Configurator, TaskView
from ..util import load_class_from_file, split_url_fragment

from ..repo import (
    GitRepo,
    Repo,
    normalize_git_url,
    normalize_git_url_hard,
    sanitize_url,
    split_git_url,
    split_git_url_with_commit,
)
from ..util import UnfurlError
from ..localenv import LocalEnv
from ..logs import getLogger
from .db import CloudMapDB
from .db import CloudMapStore
from .provenance import ProvenanceTrackingContext
from .github import GithubManager
from .gitlab import GitlabManager
from .host import LocalRepositoryHost, RepositoryHost, _LocalGitRepos

logger = getLogger("unfurl")

DEFAULT_CLOUDMAP_REPO = "https://github.com/onecommons/cloudmap.git"


class AnalyzerRegistry:
    max_analyze_depth = 4

    def __init__(self, notables: List[Type[RepositoryAnalyzer]], logger=logger):
        self.logger = logger
        self.files: Dict[str, Type[RepositoryAnalyzer]] = {}
        self.folders: Dict[str, Type[RepositoryAnalyzer]] = {}
        # Generic catch-all RepositoryAnalyzer subclasses (those that declare
        # neither files nor folders). Each entry is consulted via its
        # ``init()`` factory whenever no name-specific class matched a path,
        # in registration order; the first ``init()`` returning a non-None
        # instance wins.
        self.generic: List[Type[RepositoryAnalyzer]] = []
        for n in notables:
            self.add_notable_class(n)

    def add_notable_class(self, cls: Type[RepositoryAnalyzer]):
        for file in cls.files:
            self.files[file] = cls
        for folder in cls.folders:
            self.folders[folder] = cls
        if not cls.files and not cls.folders:
            # No specific match keys → register as a generic fallback.
            self.generic.append(cls)

    def _match_generic(
        self, dirname: str, filename: str, digest: str = ""
    ) -> Tuple[Optional[RepositoryAnalyzer], Optional[Type[RepositoryAnalyzer]]]:
        """Try every generic RepositoryAnalyzer; return the first instance
        that ``init()`` produces, along with its class."""
        for cls in self.generic:
            instance = cls.init(dirname, filename, digest)
            if instance is not None:
                return instance, cls
        return None, None

    def analyze_local(self, root_dir: str, start_path: str) -> List[RepositoryAnalyzer]:
        notables: List[RepositoryAnalyzer] = []
        for root, dirs, files in os.walk(start_path):
            notable = None
            notable_cls = None
            notables_found: List[Type[RepositoryAnalyzer]] = []
            rel_root = str(Path(root).relative_to(Path(root_dir)))
            for folder in dirs:
                # Pass the matched directory's own path as `folder` (file="") so
                # that `notable.path` is the directory path, consistent with
                # analyze_repo_tree() which inits folder matches with item.path.
                folder_path = (
                    folder if rel_root == "." else os.path.join(rel_root, folder)
                )
                notable_cls = self.folders.get(folder)
                if notable_cls:
                    if notable_cls not in notables_found:
                        notable = notable_cls.init(folder_path, "")
                if not notable:
                    # Fall back to generic RepositoryAnalyzers (no files/folders
                    # declared). The first init() to accept the folder wins.
                    notable, notable_cls = self._match_generic(folder_path, "")
                if notable:
                    dirs.remove(folder)  # don't visit folder
            for filename in files:
                file_cls = self.files.get(filename)
                if file_cls and file_cls not in notables_found:
                    notable_cls = file_cls
                    notable = notable_cls.init(rel_root, filename)
                if not notable:
                    notable, notable_cls = self._match_generic(rel_root, filename)
            if notable:
                notables.append(notable)
                assert notable_cls
                notables_found.append(notable_cls)
        return notables

    def analyze_path(
        self, file_path: str, root_dir: str = ""
    ) -> List[RepositoryAnalyzer]:
        """Analyze a single file or directory path for notables.

        If the path points to a directory under root_dir, delegates to analyze_local().
        Otherwise, matches the filename against registered Notable classes.

        Args:
            file_path: Relative path to analyze (e.g. "path/to/Dockerfile").
            root_dir: Root directory of the repository on disk (for directory analysis).

        Returns:
            List of RepositoryAnalyzer instances found (empty if no match).
        """
        # If root_dir is available and the path is a directory, walk it
        if root_dir:
            full_path = os.path.join(root_dir, file_path)
            if os.path.isdir(full_path):
                return self.analyze_local(root_dir, full_path)

        # Try to match a single file or folder name
        dirname, filename = os.path.split(file_path)
        notable_cls = self.files.get(filename) or self.folders.get(filename)
        if notable_cls:
            notable_inst = notable_cls.init(dirname, filename)
            if notable_inst:
                return [notable_inst]
        # Fall back to generic RepositoryAnalyzers.
        generic_inst, _ = self._match_generic(dirname, filename)
        if generic_inst is not None:
            return [generic_inst]
        return []

    def analyze_repo_tree(
        self,
        root_path: str,
        children: List[IndexObject],
        notables: List[RepositoryAnalyzer],
        depth=-1,
    ):
        descend: List[IndexObject] = []
        if depth > self.max_analyze_depth:
            return descend
        notables_found: List[Type[RepositoryAnalyzer]] = []
        for item in children:
            notable = None
            notable_cls = None
            dirname, filename = os.path.split(item.path)
            if item.type == "tree":
                notable_cls = self.folders.get(filename)
                digest = f"git:tree:{item.hexsha}"
                if notable_cls:
                    # XXX if notable_cls not in notables_found:
                    notable = notable_cls.init(cast(str, item.path), "", digest)
                if not notable:
                    notable, notable_cls = self._match_generic(
                        cast(str, item.path), "", digest
                    )
                if not notable:
                    descend.append(item)
            elif item.type == "blob":
                notable_cls = self.files.get(filename)
                digest = f"git:blob:{item.hexsha}"
                if notable_cls:  # XXX and notable_cls not in notables_found:
                    notable = notable_cls.init(dirname, filename, digest)
                if not notable:
                    notable, notable_cls = self._match_generic(
                        dirname, filename, digest
                    )
            if notable:
                notables.append(notable)
                assert notable_cls
                notables_found.append(notable_cls)
        return descend


Analyze_Options = Literal["yes", "no", "save-only", "default"]

class Directory(_LocalGitRepos):
    """Drives analysis: clones repositories, runs the analyzers matching their
    files, and hands the records they produce to :py:attr:`db`.

    The records themselves live in the ``db``, which is what analyzers are
    given -- this class is the local git working area around it.
    """

    DEFAULT_NAME = "cloudmap.yml"

    def __init__(
        self,
        cloudmap: "CloudMap",
        db: CloudMapStore,
        local_repo_root: str = "",
        skip_analysis=False,
    ) -> None:
        # `store` is where records live: a local document, or a
        # `CloudMapProxy` standing in for one when the cloudmap is served by an
        # upstream server, so nothing here has to know the difference.
        self.store = db
        # `context` is what analyzers -- and the repository host code that runs
        # them -- read and write through. Always the tracking context, never
        # the store: it attributes records to the url being analyzed, and it
        # exposes only `AnalyzerContext`, so an analyzer can neither persist
        # the cloudmap nor reach the `CloudMap` that owns it.
        self.context = ProvenanceTrackingContext(db)
        self.context.logger = cloudmap.logger
        self.context.do_analysis = not skip_analysis
        self.tmp_dir: Optional[tempfile.TemporaryDirectory] = None
        self.cloudmap = cloudmap
        _LocalGitRepos.__init__(self, local_repo_root, cloudmap.logger)

        # Start with default Notable classes and add custom analyzers from cloudmap.
        # Only RepositoryAnalyzer subclasses go through RepositoryAnalyzer;
        # URLAnalyzer subclasses live on cloudmap.url_analyzers instead.
        from .analyzers import Analyzers

        notable_classes: List[Type[RepositoryAnalyzer]] = list(Analyzers)
        # note: these will override built-in analyzers if they register the same files and folders types
        notable_classes.extend(
            cls
            for cls in cloudmap.custom_analyzers
            if issubclass(cls, RepositoryAnalyzer)
        )

        self.analyzer = AnalyzerRegistry(notable_classes, self.logger)

    def _set_repos(self, root: str) -> None:
        super()._set_repos(root)
        if self.cloudmap.local_env:
            for repo in self.cloudmap.local_env._get_repos():
                if isinstance(repo, GitRepo):
                    self._add_repo(repo)

    @property
    def do_analysis(self) -> bool:
        """Whether cross-referenced urls are analyzed rather than recorded as
        stubs. Lives on the ``db`` because that's what analyzers read it from."""
        return self.context.do_analysis

    @do_analysis.setter
    def do_analysis(self, value: bool) -> None:
        self.context.do_analysis = value

    def find_local_repos_for_host(
        self, host: "RepositoryHost"
    ) -> Iterator[Tuple[git.Remote, GitRepo, Repository]]:
        """for each repo that matches host.host and host.namespace, yield matching remote and Repository"""
        for url, remotes in self.remotes.items():
            repo_info = self.context.get_repository(url)
            if repo_info and host.has_repository(repo_info):
                remote = self._choose_remote(remotes, host.name)
                working_dir = cast(str, remote.repo.working_tree_dir).rstrip("/")
                yield remote, self.repos[working_dir], repo_info

    def find_mismatched_repo(self, host: "RepositoryHost") -> Optional[GitRepo]:
        for remote, repo, repo_info in self.find_local_repos_for_host(host):
            if repo.revision != repo_info.branches["main"]:
                return repo
        return None

    def merge_from_host(self, host: "RepositoryHost") -> None:
        """For each local repo that has a remote that matches the repository host, pull the default branch."""
        for remote, repo, repo_info in self.find_local_repos_for_host(host):
            default_branch = repo_info.get_default_branch()
            host_branch = f"{remote.name}/{default_branch}"
            if host_branch in remote.repo.git.branch(r=True).split():
                remote.repo.git.checkout(default_branch, with_exceptions=True)
                # should be a ff merge
                remote.repo.git.merge(host_branch, ff_only=True, with_exceptions=True)

    def ensure_local(self):
        if not self.repos_root:
            self.tmp_dir = tempfile.TemporaryDirectory(prefix="oc-repo-update-")
            self.repos_root = self.tmp_dir.name
            self.logger.debug(f"setting {self.tmp_dir.name} as repo_root")

    def cleanup_local(self):
        if self.tmp_dir:
            self.tmp_dir.cleanup()

    def clone_repo(self, repo_info: Repository, url: str) -> GitRepo:
        # XXX handle conflict when same path, different host
        assert self.repos_root
        download_path = str(Path(self.repos_root) / repo_info.path)
        self.logger.verbose(f"cloning {sanitize_url(url)} to {download_path}")
        repo = git.Repo.clone_from(url or repo_info.git_url(), download_path)
        gitrepo = GitRepo(repo)
        self._add_repo(gitrepo)
        return gitrepo

    def analyze_repo(
        self, repo_info: Repository, repo: GitRepo
    ) -> List[RepositoryAnalyzer]:
        notables: List[RepositoryAnalyzer] = []

        root = repo.repo.head.commit.tree
        items = [root]
        seen = set()
        while items:
            item = items.pop(0)
            # sort so blobs are before trees
            children = sorted(
                root._get_intermediate_items(item), key=attrgetter("type")
            )
            # analyze return trees to descend into
            # XXX track and pass depth argument
            for tree in self.analyzer.analyze_repo_tree(
                repo.working_dir, children, notables
            ):
                if tree.type == "tree" and tree not in seen:
                    items.append(tree)
                    seen.add(tree)
        return notables

    def maybe_analyze(
        self,
        repo_info: Repository,
        repo: GitRepo,
        previous_contains: TypedUrls,
    ) -> Optional[List[RepositoryAnalyzer]]:
        if self.do_analysis:
            try:
                return self.analyze(repo_info, repo)
            except Exception:
                self.context._mark_failed()
                # restore previous
                repo_info.contains = previous_contains
                self.logger.error(
                    "Unexpected error analyzing %s.", repo_info.url, exc_info=True
                )
        # no analysis happened, preserve previous analysis
        repo_info.contains = previous_contains
        return None

    def analyze(self, repo_info: Repository, repo: GitRepo) -> List[RepositoryAnalyzer]:
        """Run the analyzers matching this repository's files.

        Records are attributed to the repository and to the file that produced
        them, whether this runs as part of analyzing a url or as part of a
        repository host sync -- the same files produce the same records either
        way, so they get the same provenance.
        """
        self.logger.verbose("analyzing %s", repo_info.url)
        analyze_queue = self.analyze_repo(repo_info, repo)
        with self.context._tracking_provenance(repo_info.url):
            return self._analyze_notables(repo_info, repo, analyze_queue)

    def _analyze_notables(
        self,
        repo_info: Repository,
        repo: GitRepo,
        analyze_queue: List[RepositoryAnalyzer],
    ) -> List[RepositoryAnalyzer]:
        notables: List[RepositoryAnalyzer] = []
        context = self.context
        for n in analyze_queue:
            try:
                # so a record is discovered from the file that produced it as
                # well as from the repository the file is in
                with context._tracking_provenance(repo_info.artifact_url(n.path)):
                    artifact = n.analyze(context, repo_info, repo.working_dir)
                if artifact:
                    # XXX what to do if self.context.get_artifact(artifact.url)?
                    # (currently we want to give this priority for the git digest)
                    context.add_record(artifact)
                    url = artifact.metadata.source_url
                    if (
                        url
                        and CloudMap._is_git_url(url)
                        and not self.context.get_repository(url)
                    ):
                        self.cloudmap.analyze_url(url, "no")
            except Exception:
                context._mark_failed()
                self.logger.error(
                    "Unexpected error analyzing notable %s in %s.",
                    n.path,
                    repo_info.url,
                    exc_info=True,
                )
            else:
                notables.append(n)
        repo_info.add_notables(notables)
        return notables


class CloudMap:
    """
    Manages a cloudmap repository with a cloudmap.yaml file.
    Sync operations create a separate branch for each repository host to reflect its remote state.
    """

    def __init__(
        self,
        repo: Optional[GitRepo],
        host_branch: str,
        source_branch: str = "main",
        localrepo_root: str = "",
        path: str = "",
        skip_analysis: bool = False,
        commit: bool = False,
        logger=logger,
        local_env: Optional["LocalEnv"] = None,
        db: Optional[CloudMapStore] = None,
    ):
        """Initialize a CloudMap bound to a local cloudmap git checkout.

        Args:
            repo: Local git repository that contains the cloudmap file.
            host_branch:  Working cloudmap branch (default to ``hosts/{host_name}``)
            source_branch: Source-of-truth cloudmap branch used for exporting to a host.
            localrepo_root: Root directory for local repository clones used by sync.
            path: Path to the cloudmap file, if relative, relative to the ``repo`` root or project root if ``repo`` is None.
            skip_analysis: Skip repository content analysis when True.
            commit: Whether to commit changes to the cloudmap repository after syncing.
            logger: Logger used for cloudmap operations.
            local_env: Optional local environment used for context and config.
            db: The record store this cloudmap reads and writes. Defaults to
                the local document at ``path``; :py:meth:`_get_server` passes a
                :py:class:`~unfurl.cloudmap.proxy.CloudMapProxy`.
        """
        self.logger = logger
        self.host_branch = host_branch or source_branch
        self.source_branch = source_branch
        self.local_env = local_env
        project_path = (
            local_env and local_env.project and local_env.project.projectRoot
        ) or "."
        if os.path.isabs(path):
            filepath = path
        elif repo:
            filepath = str(Path(repo.working_dir) / (path or "cloudmap.yaml"))
        else:
            filepath = str(Path(project_path) / (path or "cloudmap.yaml"))
        # Load custom analyzers after repo is available
        self.custom_analyzers = self._load_custom_analyzers(
            local_env, project_path, logger
        )
        self.repo = repo
        self.commit = commit

        # URL-based analyzer registry, keyed by URL prefix. Seeded with the
        # built-in OCI/PURL handlers from .analyzers; any custom
        # URLAnalyzer subclasses loaded from cloudmaps.analyzers config
        # are registered as well. Overlapping prefixes are resolved by
        # longest-prefix-wins in match_url_analyzer().
        self.url_analyzers: Dict[str, Type[URLAnalyzer]] = {}
        from .analyzers import URLAnalyzers

        for url_cls in cast("Sequence[Type[URLAnalyzer]]", URLAnalyzers):
            self.register_url_analyzer(url_cls)
        for cls in self.custom_analyzers:
            if issubclass(cls, URLAnalyzer):
                self.register_url_analyzer(cls)

        # built here rather than by the caller: the store keeps a reference to
        # the cloudmap that owns it, which doesn't exist until now
        if db is None:
            db = CloudMapDB(filepath)
        db.set_cloudmap(self)
        self.directory = Directory(self, db, localrepo_root, skip_analysis)

    def register_url_analyzer(self, cls: Type[URLAnalyzer]) -> None:
        """Register a :class:`URLAnalyzer` subclass for each of its ``url_schemes``."""
        for scheme in cls.url_schemes:
            self.url_analyzers[scheme] = cls

    def _collect_origin_urls(self, repository: "Repository") -> Set[str]:
        """Return the normalized set of URLs ``repository`` derives from.

        Starts with the repository's own ``url`` and transitively follows its
        ``fork_of`` and ``mirror_of`` links: for each followed url that has a
        :class:`Repository` record in the cloudmap, its own origins are
        followed too. A ``visited`` set guards against cycles."""
        urls: Set[str] = set()

        def walk(repo: "Repository") -> None:
            for url in (repo.url, repo.fork_of, repo.mirror_of):
                if not url:
                    continue
                norm = normalize_git_url_hard(url)
                if norm in urls:
                    continue
                urls.add(norm)
                # follow the chain when a record exists for the origin url
                origin = self.directory.context.get_repository(url)
                if origin is not None and origin is not repo:
                    walk(origin)

        walk(repository)
        return urls

    def _source_typerefs(
        self, repository: "Repository", source: str
    ) -> Optional[TypeRefs]:
        """Resolve the type references for the pipeline's ``source`` file.

        Looks it up in the repository's ``contains`` map first (keyed by
        relative path), falling back to the type of the source's
        :class:`Artifact` record. Returns ``None`` when neither is found."""
        if ("", source) in repository.contains:
            return repository.contains[("", source)]
        artifact = self.directory.context.get_artifact(repository.artifact_url(source))
        if artifact is not None:
            return artifact.type
        return None

    def _typerefs_match(
        self, wanted: Sequence[TypeRefs], source_types: Optional[TypeRefs]
    ) -> bool:
        """True if any :class:`TypeRefs` in ``wanted`` has all of its type
        names present in ``source_types``.

        The set of available names includes not just the source's own type
        names but also every type they (transitively) extend, resolved via the
        cloudmap's :class:`CloudType` records, so an analyzer keyed on a base
        type also matches a source declaring one of its subtypes."""
        if source_types is None:
            return False
        available: Set[str] = set()
        pending = list(source_types.names())
        while pending:
            name = pending.pop()
            if name in available:
                continue
            available.add(name)
            record = self.directory.context.get_type(name)
            if record is not None:
                pending.extend(record.extends)
        return any(set(tr.names()) <= available for tr in wanted)

    def find_pipeline_analyzers(
        self, repository: "Repository", source: str
    ) -> Optional[Type[PipelineRunAnalyzer]]:
        """Return the first custom :class:`PipelineRunAnalyzer` matching the
        pipeline's ``repository`` and ``source``.

        An analyzer matches when each of its non-empty ``repositories``,
        ``sources``, and ``source_types`` class attributes matches (an empty
        attribute is a wildcard):

        - ``repositories``: any url matches the repository's own ``url`` or a
          repository it transitively derives from (``fork_of`` / ``mirror_of``,
          see :meth:`_collect_origin_urls`), so an analyzer keyed on an upstream
          repository also matches its forks and mirrors.
        - ``sources``: any path equals ``source``.
        - ``source_types``: any entry's type names are all present in the
          source file's type references (see :meth:`_source_typerefs`)."""
        candidates = self._collect_origin_urls(repository)
        source_types: Optional[TypeRefs] = None
        source_types_resolved = False
        for cls in self.custom_analyzers:
            if not (isinstance(cls, type) and issubclass(cls, PipelineRunAnalyzer)):
                continue
            if cls.repositories and not any(
                normalize_git_url_hard(r) in candidates for r in cls.repositories
            ):
                continue
            if cls.sources and source not in cls.sources:
                continue
            if cls.source_types:
                if not source_types_resolved:
                    source_types = self._source_typerefs(repository, source)
                    source_types_resolved = True
                if not self._typerefs_match(cls.source_types, source_types):
                    continue
            return cls
        return None

    def match_url_analyzer(self, url: str) -> Iterator[Type[URLAnalyzer]]:
        """Yield every registered analyzer whose URL prefix matches ``url``,
        ordered longest-prefix-first.

        Multiple analyzers can match (e.g. both ``"pkg:oci"`` and ``"pkg:"``
        match ``"pkg:oci/..."``); :meth:`analyze_url` walks them in order and
        falls back to the next when one declines via ``init_from_url``
        returning ``None``.
        """
        matches = [
            (prefix, cls)
            for prefix, cls in self.url_analyzers.items()
            if url.startswith(prefix)
        ]
        matches.sort(key=lambda item: len(item[0]), reverse=True)
        for _prefix, cls in matches:
            yield cls

    @staticmethod
    def _checkout_cloudmap(
        local_env: "LocalEnv",
        url: str,
        revision: str,
        host_branch: str,
        logger=logger,
    ) -> Tuple[GitRepo, str]:
        """Clone or checkout the cloudmap repository locally.

        If host_branch is provided, checkout that branch. If the host branch does
        not exist yet on the remote, it is created from ``revision``.

        Returns:
            Tuple[GitRepo, str]: The checked out local repository and the host branch name, if set.
        """
        # XXX what if branch only exists locally?
        if not host_branch or host_branch == revision:
            branch = ""
            branch_exists = True
        else:
            branch = host_branch
            local_repo = local_env.find_repo(url, branch)
            if (
                local_repo
                and isinstance(local_repo, GitRepo)
                and branch in local_repo.repo.branches
            ):
                branch_exists = True
            else:
                try:
                    branch_exists = bool(
                        git.cmd.Git().ls_remote(url, branch, heads=True)
                    )
                except Exception:
                    raise UnfurlError(
                        f'Error trying to access cloudmap git repository at "{url}"',
                        saveStack=True,
                    )
            logger.verbose(
                f"Using {'existing' if branch_exists else 'new'} branch {branch} for cloudmap."
            )

        if branch_exists:  # branch exists
            # clone or checkout branch
            repo, _, _ = local_env.find_or_create_working_dir(url, branch or revision)
        else:
            # clone or checkout main and create branch
            repo, _, _ = local_env.find_or_create_working_dir(
                url, revision, checkout_args=dict(b=branch)
            )

        if not isinstance(repo, GitRepo):
            # XXX add find_or_create_working_dir variant that always returns GitRepo
            raise UnfurlError(f"couldn't clone {url}")

        return repo, branch

    @classmethod
    def _get_server(
        cls,
        local_env: "LocalEnv",
        name: str,
        clone_root: Optional[str] = None,
        host_branch: str = "",
        skip_analysis: bool = False,
        commit: bool = False,
        logger=logger,
    ) -> Optional["CloudMap"]:
        """The cloudmap served by the upstream server configured for ``name``.

        Its records are accessed through a :py:class:`~unfurl.cloudmap.proxy.CloudMapProxy`
        standing in for a local :py:class:`~unfurl.cloudmap.db.CloudMapDB`:
        nothing is cloned and records are POSTed rather than committed.

        Returns None when no server is configured for ``name``.
        """
        env_context = local_env.get_context()
        environment = env_context.get("cloudmaps", {})
        server = environment.get("servers", {}).get(name)
        url = ""
        if server:
            server = local_env.map_value(server, env_context.get("variables"))
            url = server.get("url")
        else:
            server = {}
            if name != "cloudmap":
                parts = urlparse(name)
                if (
                    parts.scheme
                    and not cls._is_git_url(name)
                    and parts.hostname not in ("github.com", "gitlab.com")
                ):
                    url = name
        if not url:
            return None
        from .proxy import CloudMapProxy

        return CloudMap(
            None,  # nothing to clone: the cloudmap is on the server
            host_branch,
            localrepo_root=clone_root or "",
            skip_analysis=skip_analysis,
            commit=commit,
            logger=logger,
            local_env=local_env,
            db=CloudMapProxy(
                url,
                username=server.get("username"),
                private_token=server.get("password"),
                timeout=server.get("timeout"),
                logger=logger,
            ),
        )

    @classmethod
    def from_name(
        cls,
        local_env: "LocalEnv",
        name: str,
        clone_root: Optional[str],
        host_branch: str,
        skip_analysis: bool,
        commit: bool,
        logger=logger,
        use_server: bool = False,
    ) -> "CloudMap":
        """Open the cloudmap named ``name``.

        ``use_server`` opts in to an upstream cloudmap server when one is
        configured for ``name``: records are POSTed there and nothing is
        cloned. It is off by default because syncing with a repository host
        needs a local clone -- :py:meth:`to_host` merges the host branch into
        the source branch and commits, which only a checkout can do.
        """
        if use_server:
            cloudmap = cls._get_server(
                local_env, name, clone_root, host_branch, skip_analysis, commit, logger
            )
            if cloudmap is not None:
                return cloudmap
        url, path, revision, repository = cls.get_config(local_env, name)
        if not url:
            # create or use cloudmap file in the project repo
            assert local_env.project
            repo = local_env.project.project_repoview.repo
            if repo:
                assert isinstance(repo, GitRepo)
                revision = host_branch = repo.active_branch
            else:
                host_branch = ""
            path = os.path.relpath(os.path.abspath(path), local_env.project.projectRoot)
        else:
            repo, host_branch = cls._checkout_cloudmap(
                local_env, url, revision, host_branch, logger
            )
        if clone_root is None:
            local_repo_root = repository.get("clone_root") or ""
        else:
            local_repo_root = clone_root

        return CloudMap(
            cast(Optional[GitRepo], repo),
            host_branch,
            revision,
            local_repo_root,
            path,
            skip_analysis,
            commit,
            logger,
            local_env,
        )

    @staticmethod
    def _load_custom_analyzers(
        local_env: Optional["LocalEnv"],
        base_dir: str,
        logger,
    ) -> List[Type[Analyzer]]:
        """
        Load custom Notable analyzer classes from cloudmaps config.

        Args:
            local_env: LocalEnv instance to get cloudmaps config from
            base_dir: Base directory for resolving relative paths
            logger: Logger instance for debug/warning/error messages

        Returns:
            List of loaded Notable analyzer classes
        """
        custom_analyzers: List[Type[Analyzer]] = []
        if not local_env:
            return custom_analyzers

        cloudmaps_config = local_env.get_context().get("cloudmaps", {})
        analyzer_entries = cloudmaps_config.get("analyzers", [])
        for entry in analyzer_entries:
            if not isinstance(entry, dict):
                logger.warning(
                    f"Analyzer config entry must be a mapping with a 'path' key, got: {entry!r}"
                )
                continue
            analyzer_path = entry.get("path")
            if not analyzer_path:
                logger.warning(f"Analyzer config entry missing 'path' key: {entry!r}")
                continue
            try:
                analyzer_class = load_class_from_file(
                    analyzer_path,
                    base_dir,
                    "Analyzer class",
                )
                if analyzer_class and issubclass(analyzer_class, Analyzer):
                    # Both RepositoryAnalyzer and URLAnalyzer subclasses are
                    # accepted; CloudMap dispatches them to the right registry.
                    custom_analyzers.append(analyzer_class)
                    logger.debug(
                        f"Loaded custom {analyzer_class.__name__} from {analyzer_path}"
                    )
                else:
                    logger.warning(
                        f"Class loaded from {analyzer_path} is not a subclass of Analyzer"
                    )
            except Exception as e:
                logger.error(
                    f"Failed to load custom Analyzer from {analyzer_path}: {e}"
                )

        return custom_analyzers

    @classmethod
    def get_config(cls, local_env: "LocalEnv", name: str) -> Tuple[str, str, str, dict]:
        """name is a cloudmap url or a named cloudmap repository."""
        environment = local_env.get_context().get("cloudmaps", {})
        # for now name is just the name of repository
        repository = environment.get("repositories", {}).get(name)
        if not repository or "url" not in repository:
            repositories, _ = local_env.get_repositories_and_package_specs()
            env_repository = repositories.get(name)
            if repository and env_repository:
                repository.update(env_repository)
            else:
                repository = env_repository
        if repository:
            cloudmap_url = repository["url"]
        else:
            if name == "cloudmap":
                cloudmap_url = DEFAULT_CLOUDMAP_REPO
            else:
                # assume name is an url or local path
                cloudmap_url = name
            repository = {}
        if not urlparse(cloudmap_url).scheme:
            # url is a local path return as path
            # so we create a new cloudmap file
            return "", cloudmap_url, repository.get("revision", "main"), repository
        else:
            url, path, revision = split_git_url(cloudmap_url)
            return (
                normalize_git_url(url),
                path,
                revision or repository.get("revision", "main"),
                repository,
            )

    @staticmethod
    def _find_host_config(
        hosts: Dict[str, HostConfig],
        host_name: str,
        path: str = "",
    ) -> Tuple[str, Optional[HostConfig]]:
        """Find the best matching host config for a given hostname.

        When multiple host configs share the same hostname, the one whose URL
        path is the longest prefix of *path* wins.  This lets narrower configs
        (e.g. ``https://gitlab.com/myorg``) take priority over broader ones
        (e.g. ``https://gitlab.com``).
        """
        best_name = ""
        best_config: Optional[HostConfig] = None
        best_match_len = -1
        for name, host_config in hosts.items():
            if "url" not in host_config:
                continue
            host_parsed = urlparse(host_config["url"])
            if host_parsed.hostname != host_name:
                continue
            host_path = host_parsed.path.strip("/")
            if not host_path:
                # Host with no path matches anything but with lowest priority
                if best_match_len < 0:
                    best_name, best_config, best_match_len = name, host_config, 0
            elif path.startswith(host_path) or path.startswith(host_path + "/"):
                if len(host_path) > best_match_len:
                    best_name, best_config, best_match_len = (
                        name,
                        host_config,
                        len(host_path),
                    )
            elif best_match_len < 0:
                # No path match yet; keep as fallback over nothing
                best_name, best_config, best_match_len = name, host_config, 0
        return best_name, best_config

    @classmethod
    def get_host(
        cls,
        local_env: "LocalEnv",
        name: str,
        namespace: str,
        repos_root: str,
        visibility: Optional[str] = None,
        repo_filter: str = "",
    ) -> RepositoryHost:
        """
        Find a repository host in the cloudmap config with a hostname matching the given URL
        and return a RepositoryHost instance with repo_filter set to the URL.

        Args:
            local_env: The local environment containing cloudmap configuration
            name: Name of the host config or url with optional credentials
            namespace: Optional namespace to filter repositories
            repos_root: Optional root directory for local repositories
            visibility: Optional visibility filter for repositories
            repo_filter: Optional repository identifier to match and use as repo_filter
        Returns:
            RepositoryHost instance
        """
        environment = local_env.get_context().get("cloudmaps", {})
        hosts = environment.get("hosts", {})
        host_config: Optional[HostConfig] = hosts.get(name)
        if host_config is None:
            if name == "local":
                host_config = LocalHostConfig(
                    type="local", clone_root=repos_root, url=""
                )
            elif ":" in name:
                # name is an url
                # try to find a matching host config for the url, otherwise create new host config on the fly
                url = name
                parts = urlparse(url)
                path = parts.path.strip("/")
                hostname = parts.hostname
                if parts.scheme == "file":
                    host_config = LocalHostConfig(
                        type="local", clone_root=repos_root or parts.path, url=""
                    )
                    name = ""
                else:
                    if not hostname:
                        raise UnfurlError(f"invalid url for host: {url}")
                    hosts = local_env.map_value(
                        hosts, local_env.get_context().get("variables")
                    )
                    name, host_config = cls._find_host_config(hosts, hostname, path)
                if host_config is None:
                    assert hostname
                    if hostname.endswith("github.com"):
                        host_type = "github"
                    elif hostname.endswith("gitlab.com"):
                        host_type = "gitlab"
                    elif hostname.endswith("unfurl.cloud"):
                        host_type = "unfurl.cloud"
                    else:
                        host_type = "local"
                    if parts.scheme in ("git", "ssh"):
                        url = url.replace(parts.scheme, "https", 1)
                    host_config = HostConfig(type=host_type, url=url)  # type: ignore
                    # set name to empty so we don't create a branch like "hosts/github.com"
                    name = ""
                if not repo_filter and "/" in path:
                    # find host config that matches this hostname and set this url as its repo_filter
                    repo_filter = url
                elif not namespace and path:
                    namespace = path
            else:
                raise UnfurlError(f"no repository host named {name} found")
        else:
            host_config = local_env.map_value(
                host_config, local_env.get_context().get("variables")
            )
        assert host_config
        if visibility:
            host_config["visibility"] = visibility
        return cls.make_host(host_config, name, namespace, repo_filter, repos_root)

    @classmethod
    def make_host(
        cls,
        host_config: HostConfig,
        name: str = "",
        namespace: str = "",
        repo_filter: str = "",
        clone_root: str = "",
        logger=logger,
    ) -> RepositoryHost:
        logger.info(f'Using repository host: "{name}"')
        if host_config["type"] == "local":
            clone_root = (
                clone_root or cast(LocalHostConfig, host_config).get("clone_root") or ""
            )
            return LocalRepositoryHost(
                name,
                clone_root,
                namespace,
                repo_filter,
                logger,
            )
        elif host_config["type"] == "github":
            if not name:
                name = urlparse(host_config["url"]).hostname or ""
                assert name
            if GithubManager is None:
                raise ImportError(
                    "PyGithub is required for GitHub integration. "
                    "Install it with: pip install PyGithub"
                )
            return GithubManager(name, host_config, namespace, repo_filter, logger)

        assert host_config["type"] in ["gitlab", "unfurl.cloud"]
        if not name:
            name = urlparse(host_config["url"]).hostname or ""
            assert name
        return GitlabManager(name, host_config, namespace, repo_filter, logger)

    @staticmethod
    def _is_git_url(url: str) -> bool:
        """Return True if the URL looks like a git repository URL."""
        parts = urlparse(url)
        scheme = parts.scheme
        if scheme == "git" or scheme.startswith("git+"):
            return True
        if parts.path.endswith(".git"):
            return True
        if parts.hostname == "github.com" and parts.path.strip("/").count("/") == 1:
            return True
        if parts.hostname in ("github.com", "unfurl.cloud") and "/-/" not in parts.path:
            return True
        return False

    def analyze_url(
        self,
        url: str,
        analyze: Analyze_Options = "default",
        replace: bool = False,
    ) -> Optional[CloudMapRecord]:
        """Analyze a URL and add the resulting record to the cloudmap.

        Determines the record type based on the URL scheme and structure:

        - Git URLs (git:, git+https:, .git suffix, local paths) → Repository
        - Package URLs (pkg:) → Artifact
        - Everything else → try URL analyzers, default to Service if no takers.

        Records added are attributed to ``url`` in their
        ``metadata.discovery.sources``, so a later run can tell what this URL
        contributed.

        Args:
            url: The URL to add. Can be a git URL, pkg: PURL, or a service URL.
            analyze: Whether to analyze the repository ("yes", "no", "save-only", "default") (default: "default").
            replace: Also remove records that were previously discovered from
                ``url`` but that it no longer produces -- see
                :py:meth:`~unfurl.cloudmap.provenance.ProvenanceTrackingContext.replace_from_source`.
                Implies ``analyze="yes"``.

        Returns:
            If analyze != "yes" return None if the URL is already in the database,
            otherwise, return the Repository, Artifact, or Service that was added (or None if there was an error).
        """
        url, from_local_path = self._normalize_analyzed_url(url)
        if replace:
            # Several paths below short-circuit when a record already exists
            # (the dedupe check here, and the one in `_add_repository_record`).
            # Those returns produce nothing, which would make every previously
            # discovered record look orphaned, so re-analysis has to be forced.
            analyze = "yes"
        source = self._canonical_source(url)
        tracked = self.directory.context
        with tracked._tracking_provenance(source) as provenance:
            record = self._analyze_url(tracked, url, analyze, from_local_path)
        if replace:
            tracked.replace_from_source(source, provenance)
        return record

    @classmethod
    def _canonical_source(cls, url: str) -> str:
        """The form of ``url`` recorded as a discovery source.

        Records are keyed by a repository's canonical ``git://`` URL, so the
        URL they were discovered from is canonicalized to match.
        """
        if not cls._is_git_url(url):
            return url
        base, fragment = split_url_fragment(url)
        return get_repository_url(base) + (f"#{fragment}" if fragment else "")

    def _normalize_analyzed_url(self, url: str) -> Tuple[str, bool]:
        """Resolve ``url`` to the canonical form recorded as a discovery source.

        A path inside a git repository becomes that repository's ``git://`` URL
        and a bare name with no scheme is treated as a container image and
        becomes a ``pkg:oci`` PURL. Anything with a scheme is returned
        unchanged -- records are built from the URL as written, which is where
        a repository's protocols are inferred from (``git+https`` implies
        https). See :py:meth:`_canonical_source` for the form recorded as
        provenance.

        Returns:
            ``(url, from_local_path)``, where ``from_local_path`` marks a path
            that resolved to a git repository -- known to be a repository, so
            the caller can skip the URL-analyzer dispatch as before.
        """
        if urlparse(url).scheme:
            return url, False
        # No scheme - see if it's a local path inside a git repository
        repo = os.path.exists(url) and Repo.find_containing_git_repo(url)
        if repo:
            self.directory._add_repo(repo)
            # don't include "." as a path to examine
            return repo.get_url_with_path(os.path.abspath(url)).rstrip("#:."), True
        self.logger.info(
            "URL %s has no scheme and is not a local path; treating as container image name",
            url,
        )
        return build_oci_purl(ContainerImageParts.split(url)), False

    def _analyze_url(
        self,
        context: ProvenanceTrackingContext,
        url: str,
        analyze: Analyze_Options,
        from_local_path: bool = False,
    ) -> Optional[CloudMapRecord]:
        """Dispatch an already-normalized URL to the record type it names.

        Split out of :py:meth:`analyze_url` so provenance tracking can wrap the
        whole dispatch, including the records analyzers add as side effects.
        """
        parts = urlparse(url)

        if from_local_path:
            # a local path is known to be a repository, so skip the URL analyzers
            return self._add_repository_record(context, url, analyze)

        if analyze != "yes":
            # unless analyze is "yes", don't add a new record if the URL already exists in the database
            existing = (
                context.get_artifact(url)
                or context.get_service(url)
                or context.get_instantiation(url)
            )
            if existing is not None:
                return None

        # Dispatch URL-based records to a registered URLAnalyzer (covers
        # pkg:oci, pkg:docker, any other pkg:* and custom URL schemes).
        # match_url_analyzer yields every matching analyzer in
        # longest-prefix-first order; we try each until one accepts
        # (init_from_url returning a non-None instance).
        for analyzer_cls in self.match_url_analyzer(url):
            analyzer = analyzer_cls.init_from_url(url, parts)
            if analyzer is None:
                continue
            record = analyzer.analyze_url(context)
            if record is not None:
                context.add_record(record)
                return record

        if self._is_git_url(url):
            return self._add_repository_record(context, url, analyze)

        # Everything else → Service record
        service = Service(url=url)
        context.add_record(service)
        return service

    def _add_repository_record(
        self,
        context: ProvenanceTrackingContext,
        url: str,
        analyze: Analyze_Options,
    ) -> Optional[Repository]:
        host: Optional[RepositoryHost] = None
        repo_url, file_path, revision, commit = split_git_url_with_commit(url)
        canonical_url = get_repository_url(repo_url)
        current_do_analysis = self.directory.do_analysis
        if file_path and analyze == "default":
            # default to analyzing the file if a file path is specified
            analyze = "yes"
            # don't analyze the whole repo if a file path is specified, just analyze the file
            self.directory.do_analysis = False
        repo_info = context.get_repository(canonical_url)
        if analyze != "yes" and repo_info is not None:
            # if not analyzing, return None if the repository already exists
            return None

        download = False
        if analyze == "yes" or analyze == "save-only":
            if not self.directory.repos_root:
                # no download location, so only analyze if the repository is already cloned locally
                if self.directory.find_repo(repo_url, ""):
                    download = True
                else:
                    download = False
                    self.logger.warning(
                        "Cannot analyze %s because the repository is not cloned locally.",
                        repo_url,
                    )

            else:
                download = True
        # re-import the repository if need to analyze it
        if repo_info is None or download:
            # Try to import via a matching repository host
            if self.local_env:
                host = CloudMap.get_host(
                    self.local_env,
                    repo_url,
                    namespace="",
                    repos_root=self.directory.repos_root,
                    repo_filter=canonical_url,
                )
                try:
                    repo_info = host.import_project_url(
                        repo_url,
                        self.directory,
                        download=download,
                    )
                except Exception as e:
                    self.logger.error(
                        "Failed to import project URL %s",
                        sanitize_url(repo_url),
                        exc_info=True,
                    )
                    return None

            if repo_info is None:
                # Fallback: build a minimal Repository from URL components
                parsed = urlparse(canonical_url)
                url_parts = urlparse(url)
                path = parsed.path.lstrip("/")
                if path.endswith(".git"):
                    path = path[:-4]
                name = path.rpartition("/")[2]
                # Infer protocol from the original URL scheme
                protocols: List[str] = []
                scheme = url_parts.scheme
                if "+" in scheme:
                    # e.g. git+https → https, git+ssh → ssh
                    protocols.append(scheme.split("+", 1)[1])
                elif scheme and scheme != "git":
                    protocols.append(scheme)
                repo_info = Repository(
                    url=canonical_url,
                    path=path,
                    name=name,
                    protocols=protocols,
                )

            # A repository host adds the record itself while importing, so
            # re-add it here to attribute it to the url being analyzed the same
            # way the fallback above is. Re-adding is a no-op apart from the
            # provenance: `add_record` replaces by key and the stamp is
            # idempotent.
            context.add_record(repo_info)

        if file_path and analyze == "yes":
            local_repo = self.directory.find_repo(repo_info.url, "")
            if not local_repo:
                self.logger.warning(
                    "Cannot analyze %s because the repository is not cloned locally.",
                    url,
                )
                return repo_info
            root_path = local_repo.working_dir
            notables = (
                root_path
                and self.directory.analyzer.analyze_path(file_path, root_path)
                or []
            )
            if notables:
                for n in notables:
                    try:
                        with context._tracking_provenance(
                            repo_info.artifact_url(n.path)
                        ):
                            artifact = n.analyze(context, repo_info, root_path)
                        # keep an existing record (it has the git digest);
                        # looking it up marks it as still in use
                        if artifact and not context.get_artifact(artifact.url):
                            context.add_record(artifact)
                    except Exception:
                        context._mark_failed()
                        self.logger.error(
                            "Unexpected error analyzing %s.",
                            repo_info.url,
                            exc_info=True,
                        )
                    repo_info.contains[("", n.path)] = (
                        TypeRefs({n.artifact_type: None}) if n.artifact_type else None
                    )
            else:
                artifact_url = repo_info.artifact_url(file_path)
                if not context.get_artifact(artifact_url):
                    artifact = Artifact(
                        url=artifact_url,
                        type=TypeRefs({EntitySchema.GenericFile: None}),
                    )
                    context.add_record(artifact)
                if ("", file_path) not in repo_info.contains:
                    repo_info.contains[("", file_path)] = None

        # Fetch pipeline runs if a ref or commit was specified in the URL
        if repo_info and (revision or commit):
            # Resolve ref to commit SHA from the repository's branches/tags
            if revision and not commit:
                commit = repo_info.branches.get(
                    revision, repo_info.tags.get(revision, "")
                )
            if not host and self.local_env:
                host = CloudMap.get_host(
                    self.local_env,
                    repo_url,
                    namespace="",
                    repos_root=self.directory.repos_root,
                    repo_filter=canonical_url,
                )
            if host:
                try:
                    for instantiation in host.get_pipeline_runs(
                        repo_info,
                        ref=revision,
                        commit=commit,
                        directory=self.directory,
                        workflow_file=file_path,
                    ):
                        context.add_record(instantiation)
                except Exception:
                    context._mark_failed()
                    self.logger.error(
                        "Failed to fetch pipeline runs for %s",
                        sanitize_url(repo_url),
                        exc_info=True,
                    )

        # restore original analysis setting
        self.directory.do_analysis = current_do_analysis
        return repo_info

    def from_host(self, host: RepositoryHost) -> bool:
        count = host.from_host(self.directory)
        if not count and host.repo_filter:
            self.logger.info(
                f"No repositories matched filter '{host.repo_filter}' on host {host.name}"
            )
        changed = self.save(
            f"Update {self.host_branch} with latest from {'/'.join([host.name, host.path]).rstrip('/')}"
        )
        return changed

    def sync(self, host: RepositoryHost, force=False) -> bool:
        """
        Synchronize the cloudmap with the given the repository host.

        First, update a branch named "hosts/{host.name}" with the latest from the repository host.
        Then merge the host branch into the default branch (e.g., "main").
        If a conflict is detected, abort with a merge error in the cloudmap repository.
        For example, if a repository branch or tags was changed in both branches there will be a merge conflict.
        If so, manually merge the changes in the local repo (they will be on the remote branch), sync them with the cloudmap, then re-run this command.

        Finally, update the repository host with any changes it needs to match the cloudmap.
        New project will be create or with existing project metadata updated and changes in local repository clones will be pushed to the host.

        The user is responsible for pushing changes to cloudmap repository back upstream.
        """
        changed = self.from_host(host)
        return self.to_host(host, changed, force, True)

    def to_host(
        self, host: RepositoryHost, merge_host: bool, force: bool, sync: bool
    ) -> bool:
        op_name = "sync" if sync else "export"
        db = self.directory.store
        # syncing merges branches and commits, so it needs a local clone --
        # a cloudmap served by an upstream server has no file on disk
        assert isinstance(db, CloudMapDB), (
            f"cannot {op_name} a cloudmap served by an upstream server"
        )
        if self.host_branch != self.source_branch:
            if self.repo:
                # CloudMap.from_name() switches to host_branch but we want to sync the source branch (e.g. main)
                self.repo.checkout(self.source_branch)
            db.reload()  # map may have changed, reload the directory
            # make sure local repos matches the cloudmap
            mismatched = self.directory.find_mismatched_repo(host)
            if mismatched:
                raise UnfurlError(
                    f"Aborting {op_name}, cloudmap is out of sync with {mismatched.working_dir}"
                )
            # merge the host branch into main
            # there will be a merge conflict if a repository branch or tags was changed in both branches
            # if so, manually merge the changes in the local repo (they will be on the remote branch), sync them with the cloudmap, then re-run
            if merge_host and self.repo and self.commit:
                self.repo.repo.git.merge(
                    self.host_branch, m=f"merge changes from syncing {self.host_branch}"
                )
                db.reload()  # map changed, reload the directory

            # for each repository merge the host's default branch (it was already fetched during from_host())
            # into the local repo's default branch
            # since the cloudmap merge was successful this will just be a fast-forward merge
            self.directory.merge_from_host(host)

        # deploy source_branch (e.g. main) to the host
        host.to_host(self.directory, True, force=force)

        # cloudmap might have changed
        changed = self.save(f"{op_name}ed to {host.name}")
        if self.repo and self.commit and self.host_branch != self.source_branch:
            # set host branch head to match to the source_branch (e.g. main) because we just synced main to the host
            self.repo.repo.git.branch(self.host_branch, f=True)
        return changed

    def save(self, msg: str) -> bool:
        db = self.directory.store
        if not isinstance(db, CloudMapDB):
            # a server-backed cloudmap: POST the buffered records; the server
            # owns the repository and makes the commit
            db.save(msg)
            return True
        changed = db.save()
        if not db.config.path:
            return changed
        if not self.repo or not self.commit:
            self.logger.info("saved cloudmap to %s: %s", db.config.path, msg)
            return changed
        path = db.config.path
        assert path
        self.repo.repo.index.add([path])
        if self.repo.is_dirty(False, path):
            self.repo.commit_files([path], msg)
            self.repo.repo.index.commit(msg)
            self.logger.verbose(f"committed: {msg}")
            return True
        else:
            self.logger.verbose(f'nothing to commit for "{msg}"')
            return False


class CloudMapConfigurator(Configurator):
    """
    Exports matching repositories in a cloudmap to the given host.
    You need to configure a cloudmap in your environment
    """

    def can_dry_run(self, task: TaskView) -> bool:
        return True

    def check_digest(self, task: "TaskView", changeset) -> bool:
        # set this now we see can compare with previous version
        task.inputs["cloudmap_path"] = _getdir(
            task.inputs.context, task.inputs.get("cloudmap", "cloudmap")
        )
        return super().check_digest(task, changeset)

    def render(self, task: TaskView) -> Tuple[CloudMap, RepositoryHost]:
        # set this so we can track changes to it
        localEnv = task._manifest.localEnv
        assert localEnv
        inputs = cast(CloudMapInputs, task.inputs)
        namespace = inputs.get("namespace") or ""
        host = CloudMap.make_host(
            HostConfig(type="unfurl.cloud", **inputs["host"]),
            "",  # if empty, get the name from hostname
            namespace,
            inputs.get("repository") or "",
            logger=task.logger,
        )
        host.dryrun = bool(task.dry_run)
        cloudmap_name = task.inputs.get("cloudmap", "cloudmap")
        cloud_map = CloudMap.from_name(
            localEnv,
            cloudmap_name,
            inputs.get("clone_root") or "",
            # set to switch branches, e.g. to f"hosts/{host.name}"
            str(inputs.get("host_branch", "")),
            bool(inputs.get("skip_analysis")),
            bool(inputs.get("commit")),
            task.logger,
        )
        # set this so we can track changes to the repo
        task.inputs["cloudmap_path"] = _getdir(task.inputs.context, cloudmap_name)
        return cloud_map, host

    def run(
        self,
        task: TaskView,
    ):
        cloud_map, host = cast(Tuple[CloudMap, RepositoryHost], task.rendered)
        changed = cloud_map.to_host(host, False, bool(task.inputs.get("force")), False)
        return task.done(True, changed)
