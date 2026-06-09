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

import collections
from itertools import islice
from dataclasses import dataclass, field, asdict, InitVar
from operator import attrgetter
from pathlib import Path
import re
import tempfile
import os
import os.path
from typing import (
    TYPE_CHECKING,
    Any,
    Iterable,
    Iterator,
    Optional,
    List,
    Dict,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    TypedDict,
    cast,
    Union,
)
from gitlab.base import RESTObject
from typing_extensions import Required, Literal, Protocol
from urllib.parse import ParseResult, urljoin, urlparse, quote
import git
import git.cmd
from git.objects import IndexObject
import gitlab
from gitlab.v4.objects import Project, Group, ProjectTag, ProjectBranch
from ..server.gui_variables.ufcloud_secrets import yield_ci_variables, set_ci_variables
from ..projectpaths import _getdir
from ..tosca_plugins.cloudmap_defs import (
    HostConfig,
    CloudMapInputs,
    CloudMapRecord,
    LocalHostConfig,
    Analyzer,
    ArtifactDict,
    URLAnalyzer,
    Artifact,
    CloudType,
    CloudTypeDict,
    CommonMetadata,
    EntitySchema,
    Instantiation,
    RepositoryNotable,
    NotableDict,
    PipelineArtifact,
    PipelineRunProperties,
    PipelineVariable,
    Repository,
    RepositoryDict,
    RepositoryMetadata,
    Service,
    ServiceDict,
    TypeRefConstraint,
    TypeRefs,
    get_repository_url,
    AnalyzerContext,
    CloudMapView,
)
from ..support import ContainerImage
from ..configurator import Configurator, TaskView
from ..util import load_class_from_file
from ..oci import create_oci_artifact

from ..repo import (
    GitRepo,
    Repo,
    is_git_worktree,
    normalize_git_url,
    sanitize_url,
    split_git_url,
    split_git_url_with_commit,
)
from ..util import API_VERSION, UnfurlError
from ..localenv import LocalEnv
from ..yamlloader import YamlConfig, urlopen as _urlopen
from ..logs import getLogger, UnfurlLogger
from unfurl import repo

logger = getLogger("unfurl")

DEFAULT_CLOUDMAP_REPO = "https://github.com/onecommons/cloudmap.git"

_basepath = os.path.abspath(os.path.dirname(__file__))


def find_git_repos(rootDir: str, gitDir=".git") -> Iterator[str]:
    for root, dirs, files in os.walk(rootDir):
        if gitDir in dirs and is_git_worktree(root, gitDir):
            del dirs[:]  # don't visit sub directories
            yield os.path.abspath(root)


def match_namespace(path: str, namespace: str) -> bool:
    if not namespace or path == namespace:
        return True
    if not path:
        return False
    # don't match on partial segments
    return path.startswith(os.path.join(namespace, ""))


def force_merge_local_and_push_to_remote(
    repo: GitRepo,
    remote_name: str,
    dest_branch: str,
    merge=False,
    force=False,
    logger=logger,
) -> None:
    """Make the remote repo match the local repo using force push or merge "ours" strategy."""
    if merge:
        assert not force  # why do you want to do both?
        # merge the remote branch into the current local HEAD
        # Use "ours" merge strategy so the resulting tree of the merge is always that of the current branch head, effectively
        # ignoring all changes from all other branches.
        # This creates a merge commit equivalent to a force push without rewriting history
        repo.repo.git.merge(
            dest_branch, s="ours", m=f"set {dest_branch} to {repo.repo.active_branch}"
        )

    # push local HEAD to remote "main" branch
    # skip the pipeline because that might cause additional commits
    remote = repo.repo.remotes[remote_name]
    pushinfolist = remote.push(o="ci.skip", follow_tags=True, force=force)
    pushinfo = pushinfolist[0]
    if pushinfolist.error:
        logger.error(
            f"pushed to {sanitize_url(remote.url, True)} failed: {pushinfo.summary}"
        )
    else:
        logger.info(f"pushed to {sanitize_url(remote.url, True)}: {pushinfo.summary}")


class RepositoryAnalyzer:
    max_analyze_depth = 4

    def __init__(self, notables: List[Type[RepositoryNotable]], logger=logger):
        self.logger = logger
        self.files: Dict[str, Type[RepositoryNotable]] = {}
        self.folders: Dict[str, Type[RepositoryNotable]] = {}
        # Generic catch-all RepositoryNotable subclasses (those that declare
        # neither files nor folders). Each entry is consulted via its
        # ``init()`` factory whenever no name-specific class matched a path,
        # in registration order; the first ``init()`` returning a non-None
        # instance wins.
        self.generic: List[Type[RepositoryNotable]] = []
        for n in notables:
            self.add_notable_class(n)

    def add_notable_class(self, cls: Type[RepositoryNotable]):
        for file in cls.files:
            self.files[file] = cls
        for folder in cls.folders:
            self.folders[folder] = cls
        if not cls.files and not cls.folders:
            # No specific match keys → register as a generic fallback.
            self.generic.append(cls)

    def _match_generic(
        self, dirname: str, filename: str, digest: str = ""
    ) -> Tuple[Optional[RepositoryNotable], Optional[Type[RepositoryNotable]]]:
        """Try every generic RepositoryNotable; return the first instance
        that ``init()`` produces, along with its class."""
        for cls in self.generic:
            instance = cls.init(dirname, filename, digest)
            if instance is not None:
                return instance, cls
        return None, None

    def analyze_local(self, root_dir: str, start_path: str) -> List[RepositoryNotable]:
        notables: List[RepositoryNotable] = []
        for root, dirs, files in os.walk(start_path):
            notable = None
            notable_cls = None
            notables_found: List[Type[RepositoryNotable]] = []
            rel_root = str(Path(root).relative_to(Path(root_dir)))
            for folder in dirs:
                notable_cls = self.folders.get(folder)
                if notable_cls:
                    if notable_cls not in notables_found:
                        notable = notable_cls.init(rel_root, "")
                if not notable:
                    # Fall back to generic RepositoryNotables (no files/folders
                    # declared). The first init() to accept the folder wins.
                    notable, notable_cls = self._match_generic(rel_root, "")
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
    ) -> List[RepositoryNotable]:
        """Analyze a single file or directory path for notables.

        If the path points to a directory under root_dir, delegates to analyze_local().
        Otherwise, matches the filename against registered Notable classes.

        Args:
            file_path: Relative path to analyze (e.g. "path/to/Dockerfile").
            root_dir: Root directory of the repository on disk (for directory analysis).

        Returns:
            List of Notable instances found (empty if no match).
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
        # Fall back to generic RepositoryNotables.
        generic_inst, _ = self._match_generic(dirname, filename)
        if generic_inst is not None:
            return [generic_inst]
        return []

    def analyze_repo_tree(
        self,
        root_path: str,
        children: List[IndexObject],
        notables: List[RepositoryNotable],
        depth=-1,
    ):
        descend: List[IndexObject] = []
        if depth > self.max_analyze_depth:
            return descend
        notables_found: List[Type[RepositoryNotable]] = []
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


class _LocalGitRepos:
    def __init__(
        self, local_repo_root: str = "", _logger: Optional[UnfurlLogger] = None
    ) -> None:
        self.logger = _logger or logger
        # Repository url => remotes (populated lazily; access via the ``remotes`` property).
        self._remotes: Dict[str, List[git.Remote]] = {}
        # working_dir => repo (populated lazily; access via the ``repos`` property).
        self._repos: Dict[str, GitRepo] = {}
        self.repos_root: str = os.path.expanduser(local_repo_root)
        self._repos_loaded = False

    def _ensure_loaded(self) -> None:
        if self._repos_loaded:
            return
        # set first so re-entrant ``self.repos`` accesses inside ``_set_repos``
        # (e.g. via subclass overrides) don't recurse back into the loader.
        self._repos_loaded = True
        self._set_repos(self.repos_root)

    @property
    def repos(self) -> Dict[str, GitRepo]:
        self._ensure_loaded()
        return self._repos

    @property
    def remotes(self) -> Dict[str, List[git.Remote]]:
        self._ensure_loaded()
        return self._remotes

    def _add_repo(self, repo: GitRepo) -> Optional[GitRepo]:
        working_dir = repo.working_dir
        if not repo.remote:
            self.logger.debug(f"skipping git repo in {working_dir}: no remote set")
        elif working_dir not in self._repos:
            gitrepo: git.Repo = repo.repo
            for remote in gitrepo.remotes:
                if not remote.url:
                    continue
                url = get_repository_url(remote.url)
                remotes = self._remotes.setdefault(url, [])
                if remote not in remotes:
                    remotes.append(remote)
                self.logger.trace(f"found git repo {url} in {working_dir}")
            self._repos[working_dir.rstrip("/")] = repo
        return None

    def _set_repos(self, root: str) -> None:
        # note: there can be a many to many relationship between upstream and local repos
        if root:
            self.logger.debug(f"looking for repos in {root}")
            for working_dir in find_git_repos(root):
                gitrepo = git.Repo(working_dir)
                repo = GitRepo(gitrepo)
                self._add_repo(repo)
        self.repos_root = root

    @staticmethod
    def _choose_remote(remotes: List[git.Remote], hint: str) -> git.Remote:
        host = origin = canonical = None
        for remote in remotes:
            if remote.name == hint:
                host = remote
            elif remote.name == "origin":
                origin = remote
            elif remote.name == "canonical":
                canonical = remote
        # find best candidate
        return host or origin or canonical or remotes[0]

    def find_repo(self, url: str, hint: str) -> Optional[GitRepo]:
        remotes = self.remotes.get(get_repository_url(url))
        if remotes:
            # find best candidate
            remote = self._choose_remote(remotes, hint)
            return self.repos[cast(str, remote.repo.working_tree_dir).rstrip("/")]
        return None


class CloudMapDB(CloudMapView):
    """
    Loads the cloudmap yaml file
    """

    DEFAULT_NAME = "cloudmap.yml"

    def __init__(self, path=".", contents=None, validate: bool = True) -> None:
        self._load(path, contents, validate)

    def get_repository(self, r: Union[str, Repository]) -> Optional[Repository]:
        """Get a repository by its key (URL)."""
        if isinstance(r, Repository):
            url = r.url
        else:
            if r.startswith("#/repositories/"):
                # support repository references by url fragment
                url = r[len("#/repositories/") :]
            else:
                url = get_repository_url(r)  # its a str
        found = self.repositories.get(url)
        if not found and not url.endswith(".git"):
            # package ids don't have .git suffix, try adding it
            return self.repositories.get(url + ".git")
        return found

    def get_artifact(self, url: str) -> Optional[Artifact]:
        if url.startswith("#/artifacts/"):
            # support artifact references by url fragment
            url = url[len("#/artifacts/") :]
        return self.artifacts.get(url)

    def get_service(self, url: str) -> Optional[Service]:
        if url.startswith("#/services/"):
            # support service references by url fragment
            url = url[len("#/services/") :]
        return self.services.get(url)

    def get_instantiation(self, url: str) -> Optional[Instantiation]:
        if url.startswith("#/instantiations/"):
            # support instantiation references by url fragment
            url = url[len("#/instantiations/") :]
        return self.instantiations.get(url)

    def get_type(self, name: str) -> Optional[CloudType]:
        return self.types.get(name)

    def add_record(self, record: CloudMapRecord) -> None:
        if isinstance(record, Repository):
            self.repositories[record.url] = record
        elif isinstance(record, Artifact):
            self.artifacts[record.url] = record
            for art in record.versions.values():
                self.artifacts[art.url] = art
        elif isinstance(record, Service):
            self.services[record.url] = record
            for svc in record.versions.values():
                self.services[svc.url] = svc
        elif isinstance(record, Instantiation):
            # Instantiation.__post_init__ auto-generates `url` when not
            # set, so reading record.url is safe here.
            self.instantiations[record.url] = record
            for inst in record.versions.values():
                self.instantiations[inst.url] = inst
        elif isinstance(record, CloudType):
            self.types[record.name] = record
        else:
            raise TypeError(
                f"unsupported cloudmap record type: {type(record).__name__}"
            )

    def delete_record(self, record: CloudMapRecord) -> None:
        """Remove ``record`` (and any version variants) from the
        appropriate section. Missing keys are silently ignored.
        Inverse of :meth:`add_record`.
        """
        if isinstance(record, Repository):
            self.repositories.pop(record.url, None)
        elif isinstance(record, Artifact):
            self.artifacts.pop(record.url, None)
            for art in record.versions.values():
                self.artifacts.pop(art.url, None)
        elif isinstance(record, Service):
            self.services.pop(record.url, None)
            for svc in record.versions.values():
                self.services.pop(svc.url, None)
        elif isinstance(record, Instantiation):
            self.instantiations.pop(record.url, None)
            for inst in record.versions.values():
                self.instantiations.pop(inst.url, None)
        elif isinstance(record, CloudType):
            self.types.pop(record.name, None)
        else:
            raise TypeError(
                f"unsupported cloudmap record type: {type(record).__name__}"
            )

    def add_image_artifact(self, image: "ContainerImage") -> Artifact:
        """
        Add an OCI artifact to the cloudmap from a container image.

        Creates the artifact, extracts build instantiation, and adds them to the database.

        Args:
            image: ContainerImage to analyze

        Returns:
            Artifact object added to self.artifacts
        """
        artifact, instantiation, artifact_fetch = create_oci_artifact(image)

        # Add instantiation and update artifact.instantiated_by
        if instantiation:
            self.add_record(instantiation)
        # Add artifact to database
        self.add_record(artifact)

        return artifact

    def _load(self, path: str, contents=None, validate: bool = True) -> None:
        if os.path.isdir(path):
            path = os.path.join(path, self.DEFAULT_NAME)
        default_db = dict(apiVersion=API_VERSION, kind="CloudMap", repositories={})
        self.config = YamlConfig(
            contents or default_db,
            path,
            validate,
            schema=os.path.join(_basepath, "cloudmap-schema.json"),
        )
        # Cloudmap URLs can be very long; prevent ruamel.yaml from using
        # explicit key syntax ("? key\n:") by raising the simple key limit.
        self.config.yaml.emitter.MAX_SIMPLE_KEY_LENGTH = 1024
        db = self.config.config
        assert isinstance(db, dict)
        self.metadata = cast(Dict[str, Any], db.get("metadata") or {})

        # Load types first (may be augmented during notable migration)
        types = cast(Dict, db.get("types") or {})
        self.types: CloudTypeDict = {
            name: (
                t
                if isinstance(t, CloudType)
                else CloudType(name=t.pop("name", name), **t)
            )
            for name, t in types.items()
        }

        # Load artifacts (may be augmented during notable migration)
        artifacts = cast(Dict, db.get("artifacts") or {})
        self.artifacts: ArtifactDict = self._load_resource(artifacts, Artifact)

        # Load repositories and migrate old notable format
        repositories = cast(Dict, db.get("repositories") or {})
        self.repositories: RepositoryDict = {}
        for url, r in repositories.items():
            repo = Repository(url=r.pop("git", url), **r)
            # Backwards compatibility: migrate old notable dictionary format to new format
            if isinstance(repo.notable, dict):
                from .analyzers import migrate_old_notable_format

                migrate_old_notable_format(self, repo)
            self.add_record(repo)

        # Load services
        services = cast(Dict, db.get("services") or {})
        self.services: ServiceDict = self._load_resource(services, Service)

        # Load instantiations
        instantiations = cast(Dict, db.get("instantiations") or {})
        self.instantiations: Dict[str, Instantiation] = self._load_resource(
            instantiations, Instantiation
        )

        self.db = cast(Dict[str, Any], db)

    def _load_resource(
        self,
        resources: Dict[str, Any],
        cls: Union[Type[Artifact], Type[Service], Type[Instantiation]],
    ) -> Dict[str, Any]:
        resource_dict = {
            url: (a if isinstance(a, cls) else cls(url=a.pop("url", url), **a))
            for url, a in resources.items()
        }
        # add versions to top level for easy lookup by url#version
        versions = {}
        for url, resource in resource_dict.items():
            for version_key, obj in resource.versions.items():
                versions[obj.url] = obj  # obj.url has been merged with its parent url
        resource_dict.update(versions)
        return resource_dict

    def reload(self):
        assert self.config.path
        self._load(self.config.path)

    def save(self) -> bool:
        # maintain order of repositories so git merge is effective
        # we want to support mirrors
        changed = False
        old_metadata = self.db.pop("metadata", None)
        if self.metadata:
            new_metadata = {k: self.metadata[k] for k in sorted(self.metadata)}
            if new_metadata != old_metadata:
                changed = True
            self.db["metadata"] = new_metadata
        elif old_metadata:
            changed = True

        old_repositories = self.db.pop("repositories", None)
        new_repositories = {
            k: self.repositories[k].asdict() for k in sorted(self.repositories)
        }
        if new_repositories != old_repositories:
            changed = True
        self.db["repositories"] = new_repositories

        old_artifacts = self.db.pop("artifacts", None)
        if self.artifacts:
            new_artifacts = {
                url: val.asdict()
                for url, val in sorted(self.artifacts.items())
                if not val._parent  # type: ignore[attr-defined]
            }
            if new_artifacts != old_artifacts:
                changed = True
            self.db["artifacts"] = new_artifacts
        elif old_artifacts:
            changed = True

        old_services = self.db.pop("services", None)
        if self.services:
            new_services = {
                url: val.asdict()
                for url, val in sorted(self.services.items())
                if not val._parent  # type: ignore[attr-defined]
            }
            if new_services != old_services:
                changed = True
            self.db["services"] = new_services
        elif old_services:
            changed = True

        old_instantiations = self.db.pop("instantiations", None)
        if self.instantiations:
            new_instantiations = {
                key: val.asdict()
                for key, val in sorted(self.instantiations.items())
                if not val._parent  # type: ignore[attr-defined]
            }
            if new_instantiations != old_instantiations:
                changed = True
            self.db["instantiations"] = new_instantiations
        elif old_instantiations:
            changed = True

        old_types = self.db.pop("types", None)
        if self.types:
            new_types = {
                name: (
                    self.types[name].asdict()
                    if isinstance(self.types[name], CloudType)
                    else self.types[name]
                )
                for name in sorted(self.types)
            }
            if new_types != old_types:
                changed = True
            self.db["types"] = new_types
        elif old_types:
            changed = True

        self.config.save()
        return changed

    def find_artifacts(self, type: str = "") -> Iterable[Artifact]:
        """An empty filter returns all artifacts."""
        if not type:
            return self.artifacts.values()
        return (a for a in self.artifacts.values() if type in a.type.types)

    def find_services(self, type: str = "") -> Iterable[Service]:
        """An empty filter returns all services."""
        if not type:
            return self.services.values()
        return (s for s in self.services.values() if type in s.type.types)

    def find_instantiations(self, type: str = "") -> Iterable[Instantiation]:
        """An empty filter returns all instantiations."""
        if not type:
            return self.instantiations.values()
        return (i for i in self.instantiations.values() if type in i.type.types)

    def find_types(self) -> Iterable[CloudType]:
        return self.types.values()

    def find_repositories(self) -> Iterable[Repository]:
        return self.repositories.values()


class Directory(_LocalGitRepos, AnalyzerContext):
    DEFAULT_NAME = "cloudmap.yml"

    def __init__(
        self,
        cloudmap: "CloudMap",
        path=".",
        local_repo_root: str = "",
        skip_analysis=False,
    ) -> None:
        self.db = CloudMapDB(path)
        self.tmp_dir: Optional[tempfile.TemporaryDirectory] = None
        self.cloudmap = cloudmap
        _LocalGitRepos.__init__(self, local_repo_root, cloudmap.logger)
        self.do_analysis = not skip_analysis

        # Start with default Notable classes and add custom analyzers from cloudmap.
        # Only RepositoryNotable subclasses go through RepositoryAnalyzer;
        # URLAnalyzer subclasses live on cloudmap.url_analyzers instead.
        from .analyzers import Notables

        notable_classes: List[Type[RepositoryNotable]] = list(Notables)
        # note: these will override built-in analyzers if they register the same files and folders types
        notable_classes.extend(
            cls
            for cls in cloudmap.custom_analyzers
            if issubclass(cls, RepositoryNotable)
        )

        self.analyzer = RepositoryAnalyzer(notable_classes, self.logger)

    def _set_repos(self, root: str) -> None:
        super()._set_repos(root)
        if self.cloudmap.local_env:
            for repo in self.cloudmap.local_env._get_repos():
                if isinstance(repo, GitRepo):
                    self._add_repo(repo)

    # --- CloudMapView implementation ---

    @property
    def _local__env(self) -> Optional["LocalEnv"]:
        return self.cloudmap.local_env

    def analyze_url(
        self,
        url: str,
        analyze: Literal["yes", "no", "save-only", "default"] = "default",
    ) -> Optional[Union[Repository, Artifact, Service, Instantiation]]:
        return self.cloudmap.analyze_url(url, analyze)

    def add_record(self, record: CloudMapRecord) -> None:
        self.db.add_record(record)

    def delete_record(self, record: CloudMapRecord) -> None:
        self.db.delete_record(record)

    def get_artifact(self, url: str) -> Optional[Artifact]:
        return self.db.get_artifact(url)

    def find_artifacts(self, type: str = "") -> Iterable[Artifact]:
        return self.db.find_artifacts(type)

    def find_services(self, type: str = "") -> Iterable[Service]:
        return self.db.find_services(type)

    def find_instantiations(self, type: str = "") -> Iterable[Instantiation]:
        return self.db.find_instantiations(type)

    def find_types(self) -> Iterable[CloudType]:
        return self.db.find_types()

    def find_repositories(self) -> Iterable[Repository]:
        return self.db.find_repositories()

    def get_service(self, url: str) -> Optional[Service]:
        return self.db.get_service(url)

    def get_instantiation(self, url: str) -> Optional[Instantiation]:
        return self.db.get_instantiation(url)

    def add_image_artifact(self, image: "ContainerImage") -> Artifact:
        return self.db.add_image_artifact(image)

    def get_type(self, name: str) -> Optional[CloudType]:
        return self.db.get_type(name)

    def get_repository(self, r: Union[str, Repository]) -> Optional[Repository]:
        return self.db.get_repository(r)

    def find_local_repos_for_host(
        self, host: "RepositoryHost"
    ) -> Iterator[Tuple[git.Remote, GitRepo, Repository]]:
        """for each repo that matches host.host and host.namespace, yield matching remote and Repository"""
        for url, remotes in self.remotes.items():
            repo_info = self.db.get_repository(url)
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
    ) -> List[RepositoryNotable]:
        notables: List[RepositoryNotable] = []

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
        previous_notables: Dict[str, NotableDict],
    ) -> Optional[List[RepositoryNotable]]:
        if self.do_analysis:
            try:
                return self.analyze(repo_info, repo)
            except Exception:
                # restore previous
                repo_info.notable = previous_notables
                self.logger.error(
                    "Unexpected error analyzing %s.", repo_info.url, exc_info=True
                )
        # no analysis happened, preserve previous analysis
        repo_info.notable = previous_notables
        return None

    def analyze(self, repo_info: Repository, repo: GitRepo) -> List[RepositoryNotable]:
        self.logger.verbose("analyzing %s", repo_info.url)
        notables = self.analyze_repo(repo_info, repo)
        for n in notables:
            artifact = n.analyze(self, repo_info, repo.working_dir)
            if artifact:
                # XXX what to do if self.db.get_artifact(artifact.url)?
                # (currently we want to give this priority for the git digest)
                self.db.add_record(artifact)
                url = artifact.metadata.source_url
                if (
                    url
                    and CloudMap._is_git_url(url)
                    and not self.db.get_repository(url)
                ):
                    self.cloudmap.analyze_url(url, "no")
        repo_info.add_notables(notables)
        return notables


class RepositoryHost:
    name: str = ""
    path: str = ""
    canonical_url: str = ""
    dryrun: bool = False
    repo_filter: str = ""
    hostname: str = ""

    MAX_GIT_REFS = 100
    DEFAULT_PIPELINE_LIMIT = 50

    def __init__(
        self,
        name: str,
        namespace: str,
        repo_filter: str,
        logger: UnfurlLogger,
        host_branch: Optional[str] = None,
    ) -> None:
        self.name = name
        self.path = namespace
        self.repo_filter = get_repository_url(repo_filter) if repo_filter else ""
        self.logger = logger
        self.host_branch = f"hosts/{name}" if host_branch is None else host_branch

    def from_host(self, directory: Directory) -> int:
        """
        Update the directory with latest from this host.
        If the directory has local repositories associated with it, update those repositories too.
        """
        return 0

    def to_host(self, directory: Directory, merge: bool, force: bool) -> bool:
        """
        Update or create repositories on this repository host.
        If merge is True and there a local repositories associate with the directory,
        merge and push any changes in the local repository.

        Returns True has a change was made to the repository host.
        """
        return False

    def import_project_url(
        self, url: str, directory: Directory, download: bool
    ) -> Optional[Repository]:
        """Import a project from the given URL into the directory."""
        return None

    def extract_project_path(self, url: str) -> str:
        if ":" in url:
            parts = urlparse(url)
            path = parts.path.lstrip("/")
        elif url.startswith(self.hostname):
            path = url[len(self.hostname) :].lstrip("/")
        else:
            path = url.lstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return path

    def match_repo_filter(self, repo_key: str) -> bool:  # XXX
        """repo_key is the location of the git server without the scheme or user"""
        if self.repo_filter:
            repo_key = get_repository_url(repo_key)
            if self.repo_filter[0] == "!":
                return repo_key != self.repo_filter[1:]
            return repo_key == self.repo_filter
        return True

    def has_repository(self, repo_info: Repository) -> bool:
        """Check if repository belongs to this host."""
        if self.repo_filter:
            return self.match_repo_filter(repo_info.url)
        if repo_info.url.startswith("git://" + self.hostname) and repo_info.match_path(
            self.path
        ):
            return True
        return False

    def fetch_repo(
        self, push_url: str, dest: Repository, local: "Directory"
    ) -> Tuple[Optional[GitRepo], bool]:
        # return the repo and if it need to be cloned
        # add remote for target repo
        repo = local.find_repo(push_url, self.name)
        if not repo:
            repo = local.find_repo(dest.url, self.name)
        missing = not repo
        if not repo:
            if local.repos_root:
                repo = local.clone_repo(dest, push_url)
            else:
                return None, False
        remote_name = self.name or "origin"
        try:
            dest_remote = repo.repo.remote(remote_name)
        except ValueError:
            dest_remote = git.Remote.create(repo.repo, remote_name, push_url)
        else:
            if normalize_git_url(dest_remote.url, hard=3) != normalize_git_url(
                dest.git_url(), hard=3
            ):
                self.logger.warning(
                    f"{normalize_git_url(dest_remote.url, hard=3)} doesn't match {normalize_git_url(dest.git_url(), hard=3)} for remote '{remote_name}' in {repo.working_dir}"
                )
                # XXX should we set the url?
        if self.canonical_url and push_url != dest.git_url():
            # add a remote so we can match this repository with mirror hosts
            canonical_remote_name = "canonical"
            try:
                canonical_remote = repo.repo.remote(canonical_remote_name)
                if canonical_remote.url != dest.git_url():
                    self.logger.warning(
                        f"{canonical_remote.url} doesn't match {dest.git_url()} for remote {canonical_remote_name} in {repo.working_dir}"
                    )
            except ValueError:
                git.Remote.create(repo.repo, canonical_remote_name, dest.git_url())
        if not missing:  # if it wasn't just cloned
            dest_remote.fetch()
        return repo, missing

    def canonize(self, url: str) -> str:
        """Convert URL to use canonical url if canonical_url is set."""
        if self.canonical_url:
            parts = urlparse(url)
            return urljoin(self.canonical_url, parts.path)
        else:
            return url

    def _fetch_and_analyze_repo(
        self,
        r: Repository,
        directory: Directory,
        previous: Optional[Repository],
        remote_url: str,
    ) -> None:
        try:
            repo, cloned = self.fetch_repo(remote_url, r, directory)
            assert repo
            remote_branch = f"{self.name or 'origin'}/{r.default_branch}"
            if remote_branch in repo.repo.references and not repo.is_dirty():
                # reset local main to remote's main
                repo.repo.git.checkout(r.default_branch, remote_branch, B=True)
        except Exception:
            self.logger.error("Error retrieving content for %s", r.url, exc_info=True)
        else:
            directory.maybe_analyze(r, repo, previous.notable if previous else {})

    def _push_to_host(
        self,
        repo: Optional[GitRepo],
        repo_info: Repository,
        directory: Directory,
        remote_url: str,
        do_merge: bool,
        force: bool,
    ) -> None:
        if not repo:
            repo = directory.find_repo(repo_info.url, self.name)
        if repo:
            # there's a local mirror that might have changed
            try:
                # get the latest from the remote
                repo, cloned = self.fetch_repo(remote_url, repo_info, directory)
                assert repo
                dest_branch = f"{self.name}/{repo_info.get_default_branch()}"
                if not repo.active_branch:
                    repo.checkout(repo_info.get_default_branch())
                commit = repo.repo.head.commit
                branch_exists = dest_branch in repo.repo.references
                if (
                    not branch_exists
                    or commit != repo.repo.references[dest_branch].commit
                ):
                    # now update project repository
                    self.logger.info(
                        f"{force and '(force) ' or ' '}{do_merge and 'merging' or 'pushing'} local repository to {repo.safe_url}"
                    )
                    if self.dryrun:
                        summary = cast(str, commit.summary)
                        self.logger.info(
                            f"dry run: would have pushed commit {commit.hexsha[:6]} {commit.committed_datetime} {summary}"
                        )
                    else:
                        # maybe merge and (maybe) force push the current branch into dest_branch
                        force_merge_local_and_push_to_remote(
                            repo,
                            self.name,
                            dest_branch,
                            merge=branch_exists and do_merge,
                            force=force,
                            logger=self.logger,
                        )
                    if do_merge:
                        # we might have create a merge commit, update the directory
                        repo_info.update_branch(repo)
                else:
                    self.logger.debug(
                        f"skipping push: no change detected on branch {dest_branch} for {repo.safe_url}"
                    )
            except Exception:
                self.logger.error(
                    f"Unexpected error updating upstream git for {repo_info.url}",
                    exc_info=True,
                )

    def get_pipeline_runs(
        self,
        repo_info: "Repository",
        ref: str = "",
        commit: str = "",
        limit: int = 0,
    ) -> Iterable[Instantiation]:
        f"""Return pipeline/workflow run Instantiations for the given repository.

        Results are ordered newest-first (by creation time descending).

        Args:
            repo_info: The Repository record
            ref: Git ref (branch or tag name) to filter runs
            commit: Git commit SHA to filter runs
            limit: Max number of runs to return (default: {self.DEFAULT_PIPELINE_LIMIT})
        """
        return []


class LocalRepositoryHost(RepositoryHost, _LocalGitRepos):
    """
    Locally manage git repositories from any origin using the git protocol.
    """

    def __init__(
        self,
        name: str,
        local_repo_root: str = "",
        namespace: str = "",
        repo_filter: str = "",
        logger=logger,
        host_branch: Optional[str] = None,
    ) -> None:
        super().__init__(name, namespace, repo_filter, logger, host_branch)
        _LocalGitRepos.__init__(self, local_repo_root, logger)

    def has_repository(self, repo_info: Repository) -> bool:
        if self.repo_filter:
            return self.match_repo_filter(repo_info.url)
        return repo_info.url in self.remotes

    def include_local_repo(self, repo: GitRepo) -> bool:
        return bool(repo.remote)

    def from_host(self, directory: Directory) -> int:
        """Pull latest and update the cloudmap to match the local repositories."""
        count = 0
        for repo in self.repos.values():
            if self.include_local_repo(repo):
                path = str(
                    Path(repo.working_dir).relative_to(
                        Path(os.path.abspath(self.repos_root))
                    )
                )
                if not match_namespace(path, self.path):
                    continue
                # prefer "canonical" remote
                remote = _LocalGitRepos._choose_remote(repo.repo.remotes, "canonical")
                if self.repo_filter and not self.match_repo_filter(remote.url):
                    continue
                assert repo.repo.working_dir
                if not os.getenv("UNFURL_SKIP_UPSTREAM_CHECK"):
                    repo.pull(remote.name)
                repository = self.git_to_repository(remote, path)
                repository.initial_revision = repo.get_initial_revision()
                previous = directory.db.get_repository(repository)
                if previous:
                    # don't replace metadata from remote host
                    if not previous.initial_revision:
                        previous.initial_revision = repository.initial_revision
                    repository = previous
                else:
                    directory.db.add_record(repository)
                directory.maybe_analyze(
                    repository, repo, previous.notable if previous else {}
                )
                count += 1
        return count

    def to_host(self, directory: Directory, merge: bool, force: bool) -> bool:
        """Push local changes to matching repositories to origin."""
        matched = False
        for repo in self.repos.values():
            if self.include_local_repo(repo):
                # if we're looking at different local clones than the cloudmap's
                if self.repos_root != directory.repos_root:
                    cloudmap_local_repo = directory.find_repo(repo.url, self.name)
                    if cloudmap_local_repo:
                        repo.pull(cloudmap_local_repo.working_dir)
                repo.push()
                matched = True
        return matched

    @staticmethod
    def git_to_repository(remote: git.Remote, path: str) -> "Repository":
        url = remote.url
        record = Repository(
            url=url,
            path=path,
            protocols=[urlparse(url).scheme or "ssh"],
        )
        # to get default branch:
        # first line of git ls-remote --symref url
        # ref: refs/heads/main	HEAD

        # add origin's refs as branches
        # refs iterates over .git/refs/remotes/origin/*
        remote_refs = sorted(remote.refs, key=lambda r: r.name)
        # (git fetch --tags to get the latest tag refs)
        # XXX include branches for all remotes that point to the same url
        record.branches = {
            ref.remote_head: ref.commit.hexsha
            for ref in remote_refs
            if ref.remote_head != "HEAD"
        }
        # XXX add other remotes as mirrors?
        return record


def _clean_ci_var(envvar):
    envvar = envvar.copy()
    envvar.pop("project_id")
    envvar.pop("id")
    envvar.pop("secret_value")
    return envvar


class GitlabManager(RepositoryHost):
    def __init__(
        self,
        name: str,
        config: HostConfig,
        namespace: str = "",
        repo_filter: str = "",
        logger=logger,
    ) -> None:
        url = config["url"]
        parts = urlparse(url)
        user, sep, host = parts.netloc.rpartition("@")
        if sep and ":" in user:
            user, colon, token = user.partition(":")
        else:
            token = None
        # Prioritize URL credentials over config credentials
        self.user = user or config.get("user")
        self.token = token or config.get("password")
        # namespace can be provided in the URL path or as a parameter
        namespace = namespace or parts.path.strip("/")
        super().__init__(
            name, namespace, repo_filter, logger, config.get("host_branch")
        )
        self.visibility = config.get("visibility", "any")
        self.save_internal = bool(config.get("save_internal"))
        self.canonical_url = config.get("canonical_url") or ""
        if self.canonical_url:
            self.hostname = urlparse(self.canonical_url).netloc
        else:
            self.hostname = host
        url = parts._replace(netloc=host, path="").geturl()
        self.gitlab = gitlab.Gitlab(url, private_token=self.token)
        self.logger.info(f"connecting to {self.gitlab.url} namespace {self.path}")
        if self.token:
            self.logger.debug(
                "authenticating with user %s token=%s", self.user, self.token
            )
            self.gitlab.auth()

    def _get_project_visibility(self, project: Project):
        if self.token:
            return project.visibility
        else:
            # public api calls will not have this attribute but we can assume it is public in that case
            return "public"

    def from_host(self, directory: Directory) -> int:
        """
        Update the directory with projects on this gitlab instance.
        If the directory has local repositories associated with it, update those repositories too.
        """
        if self.repo_filter and self.repo_filter[0] != "!":
            self.import_project_url(self.repo_filter, directory, download=True)
            return 1
        if self.path:
            group = self._get_group(self.path)
            if not group:
                raise Exception(f"Group {self.path} not found")
            return self._import_group_from_host(group, directory)
        else:
            projects = self.gitlab.projects.list(iterator=True)
            return self._import_projects_from_host(projects, directory)

    def _import_group_from_host(self, group: Group, directory: Directory) -> int:
        # XXX add/update namespace in cloudmap
        projects = group.projects.list(iterator=True)
        self.logger.info(f"importing group {group.full_path}")
        count = self._import_projects_from_host(projects, directory)
        for subgroup in group.subgroups.list(iterator=True):
            count += self._import_group_from_host(
                self.gitlab.groups.get(subgroup.id), directory
            )
        return count

    def import_project_url(
        self, url: str, directory: Directory, download: bool
    ) -> Repository:
        project = self.gitlab.projects.get(self.extract_project_path(url))
        return self._import_project(project, directory, download)

    def _import_projects_from_host(
        self, projects: Iterable[RESTObject], directory: Directory
    ) -> int:
        # XXX delete removed projects
        count = 0
        for p in projects:
            if self.repo_filter:
                git_url = self.canonize(cast(Project, p).http_url_to_repo)
                if not self.match_repo_filter(git_url):
                    self.logger.trace(
                        "skipping %s, doesn't match %s", git_url, self.repo_filter
                    )
                    continue
            dest_proj: Project = self.gitlab.projects.get(p.id)
            if (
                self.visibility == "public"
                and self._get_project_visibility(dest_proj) != "public"
            ):
                continue
            try:
                dest_proj.default_branch
            except AttributeError:
                # project without repositories will throw error, skip those
                self.logger.warning(
                    f"skipping project {dest_proj.web_url}, it doesn't have a git repository"
                )
                continue
            self._import_project(dest_proj, directory, True)
            count += 1
        return count

    def _import_project(
        self, dest_proj: Project, directory: Directory, download: bool
    ) -> Repository:
        r = self.gitlab_project_to_repository(dest_proj)
        previous = directory.db.get_repository(r)
        directory.db.add_record(r)
        if download:
            # add remote branches to local repository
            # XXX pull mirror = True and merge all branches not just main?
            remote_url = self.git_url_with_auth(dest_proj)
            self._fetch_and_analyze_repo(r, directory, previous, remote_url)
        return r

    def _get_projects_from_group(self, group, projects):
        for p in group.projects.list(iterator=True):
            projects[p.path_with_namespace][0] = p  # type: ignore
        for subgroup in group.subgroups.list(iterator=True):
            self._get_projects_from_group(self.gitlab.groups.get(subgroup.id), projects)

    def _sync_project_to_host(
        self,
        dest: Optional[Project],
        repo_info: Repository,
        dest_group: Group,
        directory: Directory,
        merge: bool,
        force: bool,
    ) -> None:
        try:
            name = repo_info.name
            if dest:
                # if both exist, update any changed metadata
                if self.dryrun:
                    self.logger.info(
                        "dry run: skipping creating updating project %s", name
                    )
                else:
                    self.update_project_metadata(repo_info, dest)
                do_merge = not force and merge
            else:
                if self.dryrun:
                    self.logger.info("dry run: skipping creating project %s", name)
                    return
                # create the project
                dest = self.create_project(repo_info, dest_group)
                do_merge = False
        except Exception:
            self.logger.error(
                "Unexpected error updating project metadata for %s",
                name,
                exc_info=True,
            )
            return
        assert dest
        if directory.repos_root:
            remote_url = self.git_url_with_auth(dest)
            repo = directory.find_repo(dest.http_url_to_repo, self.name)
            self._push_to_host(repo, repo_info, directory, remote_url, do_merge, force)

    def to_host(self, directory: Directory, merge: bool, force: bool) -> bool:
        """
        Create or update projects in a gitlab instance.
        If the target project has changed, update the records.

        If merge is True and there a local repositories associate with the directory,
        merge and push any changes in the local repository.

        Returns True has a change was made to the repository host.
        """
        # filter repositories to only ones that match the path
        repositories = [
            r for r in directory.db.repositories.values() if self.has_repository(r)
        ]

        dest_path = self.path
        op_name = "sync" if merge else "export"
        self.logger.info(f"{op_name}ing to {dest_path or self.hostname}")
        if not repositories:
            self.logger.info("no matching repositories to " + op_name)
            return False

        dest_group = self.ensure_group(dest_path)
        if not dest_group:
            self.logger.info("%s doesn't exist", dest_path)
            return False
        # XXX look up Namespace and sync it?

        projects = cast(
            Dict[str, Tuple[Optional[Project], Optional[Repository]]],
            collections.defaultdict(lambda: [None, None]),
        )
        self._get_projects_from_group(dest_group, projects)
        for r in repositories:
            projects[r.path][1] = r  # type: ignore

        for name in projects:
            dest, repo_info = projects[name]
            if repo_info:
                self._sync_project_to_host(
                    dest, repo_info, dest_group, directory, merge, force
                )

            # delete any extra projects
            # XXX: enable when ready
            if not repo_info and dest:
                self.logger.info(f"would delete {dest_path}")
                # full_proj = staging_gitlab.projects.get(staging.id)
                # full_proj.delete()
        return True

    def git_url_with_auth(self, project: Project) -> str:
        scheme, sep, url = project.http_url_to_repo.rpartition("://")
        return f"{scheme}://{self.user}:{self.token}@{url}"

    # only fetch group, don't create it
    def _get_group(self, path: str) -> Optional[Group]:
        try:
            return cast(Group, self.gitlab.groups.get(path))
        except Exception:
            return None

    def ensure_group(self, path: str) -> Optional[Group]:
        """Get or create the given group in the Gitlab instance"""
        gitlab = self.gitlab
        assert path
        self.logger.info(f"ensuring group {path} on {gitlab.url}")

        # see if group exists first
        group = self._get_group(path)
        if self.dryrun:
            return group

        # create if missing
        if group is None:
            parent: Optional[Group] = None
            path_so_far = []

            for name in path.split("/"):
                path_so_far.append(name)
                group = self._get_group("/".join(path_so_far))

                # create group if missing
                if group is None:
                    full_path = "/".join(path_so_far)
                    self.logger.info(f"creating group {full_path}")
                    params = {"name": name, "path": name, "visibility": "public"}
                    if parent:
                        params["parent_id"] = parent.id

                    self.gitlab.groups.create(params)
                    # make sure group is populated
                    group = self.gitlab.groups.get(full_path)

                parent = group
        assert group
        return group

    def create_project(self, repo_info: Repository, dest_group: Group) -> Project:
        self.logger.info(f"creating project {repo_info.path}")

        namespace, project_path = os.path.split(repo_info.path)
        if namespace != self.path:
            dest_group = self.ensure_group(namespace)  # type: ignore
            assert dest_group
        proj_data = {
            "name": repo_info.name,
            "path": project_path,
            "namespace_id": dest_group.id,
            "description": repo_info.metadata.description,
            "topics": repo_info.metadata.topics,
            "default_branch": repo_info.default_branch,
            "visibility": "private" if repo_info.private else "public",
        }

        new_project = cast(Project, self.gitlab.projects.create(proj_data))

        if repo_info.metadata.thumbnail_url:
            try:
                # XXX if we have credentials for this host, add them so we can we try to access non-public avatars
                # if self.visibility != "public":
                #   gl = get_gl_for_host(repo_info.metadata.thumbnail_url)
                #   if gl:
                #     response = gl.session.get(thumbnail_url)
                #     avatar = response.content
                avatar = _urlopen(repo_info.metadata.thumbnail_url).read()
            except Exception:
                self.logger.error(
                    f"Error retrieving avatar at %s",
                    repo_info.metadata.thumbnail_url,
                    exc_info=True,
                )
            else:
                new_project.avatar = avatar
                new_project.save()

        if repo_info.metadata.ci_variables and self.save_internal:
            set_ci_variables(new_project, repo_info.metadata.ci_variables.values())
        return new_project

    def update_project_metadata(
        self, repo_info: Repository, dest: gitlab.base.RESTObject
    ) -> bool:
        changed = False
        dest_proj: Project = self.gitlab.projects.get(dest.id)
        if dest_proj.description != repo_info.metadata.description:
            dest_proj.description = repo_info.metadata.description
            changed = True
        if dest_proj.topics != repo_info.metadata.topics:
            dest_proj.topics = repo_info.metadata.topics
            changed = True
        visibility = "private" if repo_info.private else "public"
        if dest_proj.visibility != visibility:
            dest_proj.visibility = visibility
            changed = True
        if changed:
            try:
                dest_proj.save()
            except Exception:
                self.logger.error("failed to save", dest_proj.path, exc_info=True)
                changed = False
        if repo_info.metadata.ci_variables and self.save_internal:
            # update or add ci vars recorded in the cloudmap
            local_vars = repo_info.metadata.ci_variables.copy()
            for envvar in yield_ci_variables(dest_proj):
                local_var = local_vars.pop(envvar["key"], None)
                # XXX if not local_var:  project.variables.delete(envvar)
                if local_var and _clean_ci_var(envvar) != local_var:
                    envvar.update(local_var)
                    local_vars[envvar["key"]] = envvar  # add it back
            if local_vars:
                changed = True
                set_ci_variables(dest_proj, local_vars.values())
        return changed

    def gitlab_project_to_repository(self, project: Project) -> Repository:
        self.logger.verbose("getting %s", project.http_url_to_repo)
        kw = {}
        # XXX
        # if project.license:
        #    kw["license"] = project.license.key in spdx_ids # or nickname or name
        if self.save_internal and project.avatar_url:
            # these urls point to the instance's uploaded files and aren't portable
            kw["thumbnail_url"] = project.avatar_url
        if getattr(project, "issues_enabled", False):
            kw["issues_url"] = self.canonize(project.web_url + "/-/issues")
        forked_from = getattr(project, "forked_from_project", None)

        # https://docs.gitlab.com/ee/api/projects.html#get-single-project
        metadata = RepositoryMetadata(
            description=project.description,
            topics=project.topics,
            homepage_url=self.canonize(project.web_url),
            **kw,
        )
        if self.visibility != "public" and self.save_internal:
            metadata.ci_variables = {
                envvar["key"]: _clean_ci_var(envvar)
                for envvar in yield_ci_variables(project)
            }
        metadata.set_lastupdate()
        git_url = project.http_url_to_repo
        parts = urlparse(git_url)
        protocols = [parts.scheme]
        if project.ssh_url_to_repo:
            protocols.append("ssh")
        repository = Repository(
            initial_revision="",  # XXX
            url=self.canonize(git_url),
            name=project.name,
            protocols=protocols,
            path=project.path_with_namespace,
            default_branch=project.default_branch,
            project_url=self.canonize(project.web_url),
            metadata=metadata,
            fork_of=get_repository_url(self.canonize(forked_from["http_url_to_repo"]))
            if forked_from
            else None,
            private=self._get_project_visibility(project) != "public",
            branches={
                b.name: b.commit["id"]
                for b in islice(
                    project.branches.list(per_page=100, iterator=True),
                    self.MAX_GIT_REFS,
                )
            },
            tags={
                t.name: t.commit["id"]
                for t in islice(
                    project.tags.list(per_page=100, iterator=True), self.MAX_GIT_REFS
                )
            },
        )
        if self.save_internal:
            repository.internal_id = str(project.get_id())
        return repository

    def get_pipeline_runs(
        self,
        repo_info: "Repository",
        ref: str = "",
        commit: str = "",
        limit: int = 0,
    ) -> Iterable[Instantiation]:
        limit = limit or self.DEFAULT_PIPELINE_LIMIT
        project_path = self.extract_project_path(repo_info.url)
        project = self.gitlab.projects.get(project_path)

        kwargs: Dict[str, Any] = {}
        if ref:
            kwargs["ref"] = ref
        if commit:
            kwargs["sha"] = commit

        pipelines = project.pipelines.list(
            order_by="id", sort="desc", iterator=True, **kwargs
        )

        count = 0
        for pipeline in pipelines:
            if limit and count >= limit:
                break
            full_pipeline = project.pipelines.get(pipeline.id)

            properties = _gitlab_pipeline_properties(project, full_pipeline)

            instantiation = Instantiation(
                url=full_pipeline.web_url,
                type=TypeRefs(
                    {
                        EntitySchema.CIRun: TypeRefConstraint(
                            properties=cast(Dict[str, Any], properties)
                        )
                    }
                ),
                source=repo_info.artifact_url(".gitlab-ci.yml"),
                source_ref=full_pipeline.ref,
                source_revision=full_pipeline.sha,
                revision=full_pipeline.sha,
                status=_map_ci_status(full_pipeline.status),
                metadata=CommonMetadata(
                    title=f"Pipeline #{full_pipeline.id}",
                    description=full_pipeline.status,
                ),
            )
            count += 1
            yield instantiation


def _gitlab_pipeline_properties(
    project: Any, full_pipeline: Any
) -> PipelineRunProperties:
    """Extract properties from a GitLab pipeline for the CIRun type constraint."""
    artifacts: List[PipelineArtifact] = []
    artifacts_expire_at = ""
    variables: List[PipelineVariable] = []

    # Fetch job-level artifacts
    try:
        jobs = full_pipeline.jobs.list(get_all=True)
        for job in jobs:
            job_artifacts = getattr(job, "artifacts", None)
            if job_artifacts:
                expire_at = str(getattr(job, "artifacts_expire_at", "") or "")
                for art in job_artifacts:
                    artifacts.append(
                        PipelineArtifact(
                            name=f"{job.name}/{art.get('filename', '')}",
                            url=f"{project.web_url}/-/jobs/{job.id}/artifacts/download",
                            size=art.get("size", 0),
                            expires_at=expire_at,
                        )
                    )
                if not artifacts_expire_at and expire_at:
                    artifacts_expire_at = expire_at
    except Exception:
        pass

    # Fetch pipeline variables (GitLab only)
    try:
        raw_variables = full_pipeline.variables.list(get_all=True)
        variables = [PipelineVariable(key=v.key, value=v.value) for v in raw_variables]
    except Exception:
        pass

    return PipelineRunProperties(
        id=full_pipeline.id,
        log_url=full_pipeline.web_url,
        artifacts=artifacts,
        artifacts_expire_at=artifacts_expire_at,
        variables=variables,
    )


def _github_run_properties(run: Any) -> PipelineRunProperties:
    """Extract properties from a GitHub workflow run for the CIRun type constraint."""
    artifacts: List[PipelineArtifact] = []
    artifacts_expire_at = ""

    # Fetch artifacts
    try:
        for art in run.get_artifacts():
            expire_str = str(art.expires_at) if art.expires_at else ""
            artifacts.append(
                PipelineArtifact(
                    name=art.name,
                    url=art.archive_download_url,
                    size=art.size_in_bytes,
                    expires_at=expire_str,
                )
            )
            if not artifacts_expire_at and expire_str:
                artifacts_expire_at = expire_str
    except Exception:
        pass

    return PipelineRunProperties(
        id=run.id,
        log_url=run.logs_url,
        artifacts=artifacts,
        artifacts_expire_at=artifacts_expire_at,
    )


InstantiationStatus = Literal[
    "draft",
    "model",
    "planned",
    "WIP",
    "observed",
    "verifiable",
    "verified",
    "reproducible",
    "reproduced",
]

_CI_STATUS_MAP: Dict[
    str,
    InstantiationStatus,
] = {
    "success": "verified",
    "failed": "observed",
    "running": "WIP",
    "pending": "planned",
    "canceled": "observed",
    "cancelled": "observed",
    "skipped": "observed",
}


def _map_ci_status(
    status: str,
) -> Optional[InstantiationStatus]:
    """Map GitHub/GitLab CI status to Instantiation status."""
    return _CI_STATUS_MAP.get(status)


# PyGithub is optional - only needed for GitHub integration
try:
    import github
    from github import Github, GithubException, Auth
    from github.Repository import Repository as GithubRepository
    from github.Organization import Organization as GithubOrganization
    from github.NamedUser import NamedUser
    from github.AuthenticatedUser import AuthenticatedUser
except ImportError:
    github = None  # type:ignore[assignment]
    GithubManager = None  # type:ignore[no-redef]
else:

    class GithubManager(RepositoryHost):  # type:ignore[no-redef]
        """GitHub repository host manager using PyGithub API."""

        def __init__(
            self,
            name: str,
            config: HostConfig,
            namespace: str = "",
            repo_filter: str = "",
            logger=logger,
        ) -> None:
            super().__init__(
                name, namespace, repo_filter, logger, config.get("host_branch")
            )
            self.visibility = config.get("visibility", "any")
            self.save_internal = config.get("save_internal", False)
            self.canonical_url = config.get("canonical_url", "")

            # Parse URL to extract hostname and base_url for GitHub Enterprise support
            url = config.get("url", "https://github.com")

            # Parse URL - supports both github.com and GitHub Enterprise
            # Prioritize URL credentials over config credentials
            if url:
                parsed_url = urlparse(url)
                # Extract token from URL if embedded (https://user:token@github.com)
                self.user = parsed_url.username or config.get("user", "")
                self.token = parsed_url.password or config.get("password", "")

                # Extract hostname
                self.hostname = parsed_url.hostname or "github.com"

                # Construct base URL for API
                if self.hostname == "github.com":
                    self.base_url = "https://github.com"
                else:
                    # GitHub Enterprise requires /api/v3 path
                    self.base_url = f"{parsed_url.scheme}://{self.hostname}"
            else:
                self.user = config.get("user", "")
                self.token = config.get("password", "")
                self.hostname = "github.com"
                self.base_url = "https://github.com"

            # Initialize PyGithub client
            if self.token:
                auth = Auth.Token(self.token)
                if self.hostname == "github.com":
                    self.github: Github = Github(auth=auth)
                else:
                    # GitHub Enterprise requires base_url with /api/v3
                    self.github = Github(base_url=f"{self.base_url}/api/v3", auth=auth)
            else:
                # No auth - can only access public repos
                if self.hostname == "github.com":
                    self.github = Github()
                else:
                    self.github = Github(base_url=f"{self.base_url}/api/v3")

        def _get_organization(self, path: str) -> Optional[GithubOrganization]:
            """Get organization without creating it (helper for checking existence)."""
            try:
                return self.github.get_organization(path)
            except GithubException:
                return None

        def import_project_url(
            self, url: str, directory: Directory, download: bool
        ) -> Repository:
            repo = self.github.get_repo(self.extract_project_path(url))
            return self._import_repository(repo, directory, download)

        def github_repository_to_repository(
            self, repo: "GithubRepository"
        ) -> Repository:
            """Convert PyGithub Repository object to cloudmap Repository dataclass."""
            # Extract git URL and scheme
            git_url = self.canonize(repo.clone_url)

            # Build protocols list
            protocols = ["https"]
            if repo.ssh_url:
                protocols.append("ssh")

            # Extract metadata
            kw: Dict[str, Any] = {}
            if repo.homepage:
                kw["homepage_url"] = repo.homepage
            # note: skipping thumbnail_url for GitHub, projects don't have thumbnails (only owners)
            if repo.has_issues:
                kw["issues_url"] = self.canonize(repo.html_url + "/issues")

            metadata = RepositoryMetadata(
                description=repo.description or "",
                topics=repo.get_topics(),
                spdx_licenses=repo.license.spdx_id if repo.license else "",
                **kw,
            )
            metadata.set_lastupdate()

            # Build Repository object
            repository = Repository(
                initial_revision="",  # XXX
                url=git_url,
                name=repo.name,
                protocols=protocols,
                path=f"{repo.owner.login}/{repo.name}",
                default_branch=repo.default_branch or "main",
                project_url=self.canonize(repo.html_url),
                metadata=metadata,
                fork_of=get_repository_url(self.canonize(repo.parent.clone_url))
                if repo.fork and repo.parent
                else None,
                private=repo.private,
                branches={
                    b.name: b.commit.sha
                    for b in islice(repo.get_branches(), self.MAX_GIT_REFS)
                },
                tags={
                    t.name: t.commit.sha
                    for t in islice(repo.get_tags(), self.MAX_GIT_REFS)
                },
            )

            if self.save_internal:
                repository.internal_id = str(repo.id)

            return repository

        def git_url_with_auth(self, repo: GithubRepository) -> str:
            """Generate authenticated git URL for PyGithub Repository."""
            clone_url = repo.clone_url
            if self.token:
                # Inject token into URL: https://{token}@github.com/org/repo.git
                parsed = urlparse(clone_url)
                return f"{parsed.scheme}://{self.token}@{parsed.hostname}{parsed.path}"
            return clone_url

        def get_owner(
            self, path: str
        ) -> Optional[Union[GithubOrganization, AuthenticatedUser, NamedUser]]:
            """Get organization or user. Cannot create organizations via GitHub API."""
            if not path:
                # Return authenticated user
                return self.github.get_user()
            # Try to get organization or named user
            try:
                return self.github.get_organization(path)
            except GithubException as e:
                if e.status == 404:
                    try:
                        return self.github.get_user(path)
                    except GithubException as e:
                        if e.status == 404:
                            self.logger.error(f"GitHub org or user '{path}' not found")
                        else:
                            self.logger.error(
                                f"Error fetching Github user '{path}'", exc_info=True
                            )
                        return None
                self.logger.error(f"Error fetching GitHub org '{path}'", exc_info=True)
                return None

        def _import_repositories_from_host(
            self, repos: Iterable[GithubRepository], directory: Directory
        ) -> int:
            """Import multiple repositories from GitHub into directory."""
            count = 0
            for repo in repos:
                # Filter by repo_filter if set
                if self.repo_filter:
                    git_url = self.canonize(repo.clone_url)
                    if not self.match_repo_filter(git_url):
                        self.logger.trace(
                            "skipping %s, doesn't match %s", git_url, self.repo_filter
                        )
                        continue

                # Filter by visibility if needed
                if self.visibility == "public" and repo.private:
                    continue
                self._import_repository(repo, directory, True)
                count += 1
            return count

        def _import_repository(
            self, repo: GithubRepository, directory: Directory, download: bool
        ) -> Repository:
            # Convert and add to directory
            repo_info = self.github_repository_to_repository(repo)
            previous = directory.db.get_repository(repo_info)
            directory.db.add_record(repo_info)
            if download:
                # add remote branches to local repository
                # XXX pull mirror = True and merge all branches not just main?
                remote_url = self.git_url_with_auth(repo)
                self._fetch_and_analyze_repo(repo_info, directory, previous, remote_url)
            self.logger.debug(f"Imported GitHub repo: {repo.full_name}")
            return repo_info

        def from_host(self, directory: Directory) -> int:
            """Fetch repositories from GitHub and sync to cloudmap."""
            if self.repo_filter and self.repo_filter[0] != "!":
                self.import_project_url(self.repo_filter, directory, download=True)
                return 1
            group = self.get_owner(self.path)
            if group:
                repos = group.get_repos()
                return self._import_repositories_from_host(repos, directory)
            return 0

        def create_project(
            self,
            repo_info: Repository,
            dest_group: Union[GithubOrganization, AuthenticatedUser],
        ) -> GithubRepository:
            """Create new GitHub repository."""
            name = repo_info.name
            description = repo_info.metadata.description or ""
            private = repo_info.private if repo_info.private is not None else True

            # Create repo in organization or user
            repo = dest_group.create_repo(
                name=name,
                description=description,
                private=private,
                auto_init=False,
            )
            self.logger.info(f"Created GitHub repo: {dest_group.login}/{name}")

            # Update metadata (topics, etc.)
            self.update_project_metadata(repo_info, repo)

            return repo

        def update_project_metadata(
            self, repo_info: Repository, dest: GithubRepository
        ) -> bool:
            """Update GitHub repository metadata (description, topics, visibility)."""
            changed = False

            # Update description
            if (
                repo_info.metadata.description
                and repo_info.metadata.description != dest.description
            ):
                dest.edit(description=repo_info.metadata.description)
                self.logger.debug(f"Updated description for {dest.full_name}")
                changed = True

            # Update topics
            if repo_info.metadata.topics:
                current_topics = dest.get_topics()
                if set(repo_info.metadata.topics) != set(current_topics):
                    dest.replace_topics(repo_info.metadata.topics)
                    self.logger.debug(f"Updated topics for {dest.full_name}")
                    changed = True

            # Update visibility
            if repo_info.private is not None:
                if repo_info.private != dest.private:
                    dest.edit(private=repo_info.private)
                    self.logger.debug(f"Updated visibility for {dest.full_name}")
                    changed = True

            return changed

        def to_host(self, directory: Directory, merge: bool, force: bool) -> bool:
            """
            Create or update projects on GitHub.
            If the target project has changed, update the records.

            If merge is True and there a local repositories associate with the directory,
            merge and push any changes in the local repository.

            Returns True has a change was made to the repository host.
            """

            # Filter repos that belong to this host
            matching_repos = [
                r for r in directory.db.repositories.values() if self.has_repository(r)
            ]
            if not matching_repos:
                self.logger.info("No matching repositories to sync to GitHub")
                return False

            # check if organization/user exists
            dest_group = self.get_owner(self.path)
            if not dest_group:
                self.logger.error(f"Failed to get GitHub destination: {self.path}")
                return False

            changed = False
            for repo_info in matching_repos:
                # Check if repo already exists on GitHub
                try:
                    repo = dest_group.get_repo(repo_info.name)
                    do_merge = not force and merge
                except GithubException as e:
                    if e.status == 404:
                        # Repo doesn't exist, create it
                        if isinstance(dest_group, NamedUser):
                            auth_user = cast(AuthenticatedUser, self.github.get_user())
                            if auth_user.login == dest_group.login:
                                owner: Union[AuthenticatedUser, GithubOrganization] = (
                                    auth_user
                                )
                            else:
                                self.logger.error(
                                    f"Cannot create repository under user {dest_group.login} - authenticated as {auth_user.login}"
                                )
                                continue
                        else:
                            owner = dest_group
                        if self.dryrun:
                            self.logger.info(
                                "dry run: skipping creating project %s", repo_info.name
                            )
                            continue
                        repo = self.create_project(repo_info, owner)
                        do_merge = False
                        changed = True
                    else:
                        self.logger.error(
                            f"Error checking GitHub repo {repo_info.name}: {e}"
                        )
                        raise
                else:
                    # Update existing repo
                    if self.dryrun:
                        self.logger.info(
                            "dry run: skipping creating updating project %s",
                            repo_info.name,
                        )
                        continue
                    elif self.update_project_metadata(repo_info, repo):
                        changed = True
                if directory.repos_root:
                    remote_url = self.git_url_with_auth(repo)
                    git_repo = directory.find_repo(repo.clone_url, self.name)
                    self._push_to_host(
                        git_repo, repo_info, directory, remote_url, do_merge, force
                    )

            return changed

        def get_pipeline_runs(
            self,
            repo_info: "Repository",
            ref: str = "",
            commit: str = "",
            limit: int = 0,
        ) -> Iterable[Instantiation]:
            limit = limit or self.DEFAULT_PIPELINE_LIMIT
            project_path = self.extract_project_path(repo_info.url)
            gh_repo = self.github.get_repo(project_path)

            kwargs: Dict[str, Any] = {}
            if ref:
                kwargs["branch"] = ref  # tags too
            if commit:
                kwargs["head_sha"] = commit

            runs = gh_repo.get_workflow_runs(**kwargs)

            count = 0
            for run in runs:
                if limit and count >= limit:
                    break

                properties = _github_run_properties(run)

                instantiation = Instantiation(
                    url=run.html_url,
                    type=TypeRefs(
                        {
                            EntitySchema.CIRun: TypeRefConstraint(
                                properties=cast(Dict[str, Any], properties)
                            )
                        }
                    ),
                    source=repo_info.artifact_url(
                        run.path
                    ),  # e.g. ".github/workflows/ci.yml"
                    source_ref=run.head_branch,
                    source_revision=run.head_sha,
                    revision=run.head_sha,
                    status=_map_ci_status(run.conclusion or run.status),
                    metadata=CommonMetadata(
                        title=run.name or run.display_title,
                        description=f"{run.event}: {run.conclusion or run.status}",
                    ),
                )
                count += 1
                yield instantiation


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

        self._visited: set[str] = set()
        self.directory = Directory(
            self,
            filepath,
            localrepo_root,
            skip_analysis,
        )

    def register_url_analyzer(self, cls: Type[URLAnalyzer]) -> None:
        """Register a :class:`URLAnalyzer` subclass for each of its ``url_schemes``."""
        for scheme in cls.url_schemes:
            self.url_analyzers[scheme] = cls

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

    @classmethod
    def get_context(
        cls,
        local_env: "LocalEnv",
        name: str = "cloudmap",
    ) -> "CloudMapView":
        # XXX change this to AnalyzerContext and use this for the --add cloudmap cli option
        env_context = local_env.get_context()
        environment = env_context.get("cloudmaps", {})
        # check "servers" first (what to return if server??)
        server = environment.get("servers", {}).get(name)
        url = ""
        if server:
            server = local_env.map_value(server, env_context.get("variables"))
            url = server.get("url")
        else:
            server = {}
            if name == "cloudmap":
                url = ""  # XXX when live: DEFAULT_CLOUDMAP_URL
            else:
                parts = urlparse(name)
                if (
                    parts.scheme
                    and not cls._is_git_url(name)
                    and parts.hostname not in ("github.com", "gitlab.com")
                ):
                    url = name
        if url:
            from .proxy import CloudMapProxy

            return CloudMapProxy(
                url,
                username=server.get("username"),
                private_token=server.get("password"),
                timeout=server.get("timeout"),
                logger=logger,
            )
        # no server, use repository instead
        url, path, revision, repository = cls.get_config(local_env, name)
        repo, _, _ = local_env.find_or_create_working_dir(url, revision)
        if not repo:
            raise UnfurlError(f"couldn't clone {url}")
        filepath = str(Path(repo.working_dir) / (path or "cloudmap.yaml"))
        return CloudMap(
            repo if isinstance(repo, GitRepo) else None,
            "",
            revision,
            "",
            filepath,  # use absolute path in case repo is not a git repo
            local_env=local_env,
        ).directory

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
    def from_name(
        cls,
        local_env: "LocalEnv",
        name: str,
        clone_root: Optional[str],
        host_branch: str,
        skip_analysis: bool,
        commit: bool,
        logger=logger,
    ) -> "CloudMap":
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
                    local_env.overrides.get("safe_mode"),
                )
                if analyzer_class and issubclass(analyzer_class, Analyzer):
                    # Both RepositoryNotable and URLAnalyzer subclasses are
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
        return False

    def analyze_url(
        self,
        url: str,
        analyze: Literal["yes", "no", "save-only", "default"] = "default",
    ) -> Optional[Union[Repository, Artifact, Service, Instantiation]]:
        """Analyze a URL and add the resulting record to the cloudmap.

        Determines the record type based on the URL scheme and structure:

        - Git URLs (git:, git+https:, .git suffix, local paths) → Repository
        - Package URLs (pkg:) → Artifact
        - Everything else → Service

        Args:
            url: The URL to add. Can be a git URL, pkg: PURL, or a service URL.
            analyze: Whether to analyze the repository ("yes", "no", "save-only", "default") (default: "default").

        Returns:
            The Repository, Artifact, or Service that was added (or already existed).
        """
        db = self.directory.db
        if url in self._visited:
            # Already processed this URL, return existing record if any
            return (
                db.get_artifact(url) or db.services.get(url) or db.get_repository(url)
            )
        self._visited.add(url)
        parts = urlparse(url)

        if not parts.scheme:
            # No scheme - see if it's a local path inside a git repository
            repo = Repo.find_containing_git_repo(url)
            if repo:
                # don't include "." as a path to examine
                self.directory._add_repo(repo)
                url = repo.get_url_with_path(os.path.abspath(url)).rstrip("#:.")
                return self._add_repository_record(url, analyze)

        # Short-circuit: a record already added under this URL is treated as
        # cached (matches the previous _add_pkg_record behaviour for the
        # generic-PURL branch). URLAnalyzers may produce Artifact / Service /
        # Instantiation records, so check all three slots.
        existing = (
            db.get_artifact(url) or db.get_service(url) or db.get_instantiation(url)
        )
        if existing is not None:
            return existing

        # Dispatch URL-based records to a registered URLAnalyzer (covers
        # pkg:oci, pkg:docker, any other pkg:* and custom URL schemes).
        # match_url_analyzer yields every matching analyzer in
        # longest-prefix-first order; we try each until one accepts
        # (init_from_url returning a non-None instance).
        for analyzer_cls in self.match_url_analyzer(url):
            analyzer = analyzer_cls.init_from_url(url, parts)
            if analyzer is None:
                continue
            record = analyzer.analyze_url(self.directory)
            if record is not None:
                # Route the returned VersionedRecord to the right collection
                # by type. Repository isn't a VersionedRecord so it's not
                # handled here — URLAnalyzers that need to record a
                # Repository should call directory.add_record directly.
                if isinstance(record, (Artifact, Service, Instantiation)):
                    db.add_record(record)
                else:
                    self.logger.warning(
                        "URLAnalyzer %s returned unsupported record type %s for %s",
                        analyzer_cls.__name__,
                        type(record).__name__,
                        url,
                    )
            return cast(Optional[Union[Artifact, Service, Instantiation]], record)

        if self._is_git_url(url):
            return self._add_repository_record(url, analyze)

        # Everything else → Service record
        service = Service(url=url)
        db.add_record(service)
        return service

    def _add_repository_record(
        self,
        url: str,
        analyze: Literal["yes", "no", "save-only", "default"],
    ) -> Optional[Repository]:
        db = self.directory.db
        host: Optional[RepositoryHost] = None
        repo_url, file_path, revision, commit = split_git_url_with_commit(url)
        canonical_url = get_repository_url(repo_url)
        self._visited.add(repo_url)
        self._visited.add(canonical_url)

        current_do_analysis = self.directory.do_analysis
        if file_path:
            if analyze == "default":
                # default to analyzing the file if a file path is specified
                analyze = "yes"
                # don't analyze the whole repo if a file path is specified, just analyze the file
                self.directory.do_analysis = False
        repo_info = db.get_repository(canonical_url)
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
                db.add_record(repo_info)

        if file_path:
            if analyze == "yes":
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
                            artifact = n.analyze(self.directory, repo_info, root_path)
                            if artifact and not db.get_artifact(artifact.url):
                                db.add_record(artifact)
                        except Exception:
                            self.logger.error(
                                "Unexpected error analyzing %s.",
                                repo_info.url,
                                exc_info=True,
                            )
                        repo_info.notable[n.path] = n.asdict()
                else:
                    artifact_url = repo_info.artifact_url(file_path)
                    if not db.get_artifact(artifact_url):
                        artifact = Artifact(
                            url=artifact_url,
                            type=TypeRefs({EntitySchema.GenericFile: None}),
                        )
                        db.add_record(artifact)
                    if file_path not in repo_info.notable:
                        repo_info.notable[file_path] = {}

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
                        repo_info, ref=revision, commit=commit
                    ):
                        db.add_record(instantiation)
                except Exception:
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
        if self.host_branch != self.source_branch:
            if self.repo:
                # CloudMap.from_name() switches to host_branch but we want to sync the source branch (e.g. main)
                self.repo.checkout(self.source_branch)
            self.directory.db.reload()  # map may have changed, reload the directory
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
                self.directory.db.reload()  # map changed, reload the directory

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
        changed = self.directory.db.save()
        if not self.directory.db.config.path:
            return changed
        if not self.repo or not self.commit:
            self.logger.info(
                "saved cloudmap to %s: %s", self.directory.db.config.path, msg
            )
            return changed
        self.repo.repo.index.add(self.directory.db.config.path)
        if self.repo.is_dirty(False, self.directory.db.config.path):
            assert self.directory.db.config.path
            self.repo.commit_files([self.directory.db.config.path], msg)
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
