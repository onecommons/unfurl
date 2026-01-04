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
from dataclasses import dataclass, field, asdict
from operator import attrgetter
from pathlib import Path
import tempfile
import os
import os.path
from typing import (
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
from typing_extensions import Required, Literal
from urllib.parse import urljoin, urlparse
import git
import git.cmd
from git.objects import IndexObject
import gitlab
from gitlab.v4.objects import Project, Group, ProjectTag, ProjectBranch

from toscaparser.elements.nodetype import NodeType
from toscaparser.nodetemplate import NodeTemplate
from .server.gui_variables.ufcloud_secrets import yield_ci_variables, set_ci_variables
from .projectpaths import FilePath, _getdir
from .graphql import ResourceTypesByName
from .spec import NodeSpec, ToscaSpec

from .support import ContainerImage
from .configurator import Configurator, Result, TaskView


from .to_json import node_type_to_graphql
from .repo import (
    GitRepo,
    is_git_worktree,
    normalize_git_url,
    sanitize_url,
    split_git_url,
)
from .util import API_VERSION, UnfurlError, assert_not_none, unique_name
from .localenv import LocalEnv
from .yamlloader import YamlConfig, urlopen as _urlopen
from .logs import getLogger
from . import DefaultNames

logger = getLogger("unfurl")

DEFAULT_CLOUDMAP_REPO = "https://github.com/onecommons/cloudmap.git"

_basepath = os.path.abspath(os.path.dirname(__file__))


# Data classes
@dataclass
class Namespace:
    name: str
    path: str
    url: str
    internal_id: Optional[str] = None
    description: str = ""
    avatar_url: str = ""
    public: Optional[bool] = None
    shared: List[str] = field(default_factory=list)


@dataclass
class RepositoryMetadata:
    """
    Metadata about the repository that isn't stored in the git repository itself but might be provided by the host
    e.g. metadata that found on the repository's GitHub or GitLab project page.
    """

    description: str = ""
    topics: List[str] = field(default_factory=list)
    spdx_license_id: str = ""
    license_url: str = ""
    issues_url: str = ""
    homepage_url: str = ""
    avatar_url: str = ""
    ci_variables: Optional[dict] = None
    lastupdate_time: Optional[str] = None
    lastupdate_digest: Optional[str] = None

    def asdict(self):
        # exclude empty values
        return {k: v for k, v in asdict(self).items() if v}

    # def get_digest(self) -> str:
    #     keys = sorted([k in asdict(self) if not k.startswith("lastupdate")]
    #     prefix = "".join([k[0] for k in keys])

    def set_lastupdate(self) -> None:
        pass


def filter_dict(d: dict) -> dict:
    # exclude empty values
    return {k: v for k, v in d.items() if v}


def match_namespace(path: str, namespace: str) -> bool:
    if not namespace or path == namespace:
        return True
    if not path:
        return False
    # don't match on partial segments
    return path.startswith(os.path.join(namespace, ""))


@dataclass
class Repository:
    git: str  # hostname and path without protocols, unique key for this record
    path: str  # project path relative to base location of git repositories on the host
    initial_revision: str = ""
    name: str = ""
    protocols: List[str] = field(default_factory=list)
    internal_id: Optional[str] = None
    project_url: str = ""
    metadata: RepositoryMetadata = field(default_factory=RepositoryMetadata)
    mirror_of: Optional[str] = None
    fork_of: Optional[str] = None
    private: Optional[bool] = None
    default_branch: str = ""
    branches: Dict[str, str] = field(default_factory=dict)
    tags: Dict[str, str] = field(default_factory=dict)
    notable: Dict[str, dict] = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.metadata, dict):
            self.metadata = RepositoryMetadata(**self.metadata)

    def get_metadata(self, directory: "RepositoryDict") -> dict:
        if self.mirror_of and self.mirror_of in directory:
            # merge mirror_of metadata
            return dict(
                directory[self.mirror_of].get_metadata(directory),
                **self.metadata.asdict(),
            )
        return self.metadata.asdict()

    def asdict(self) -> Dict[str, Any]:
        # exclude empty values
        return {
            k: (filter_dict(v) if k == "metadata" else v)
            for k, v in asdict(self).items()
            if v
        }

    def git_url(self, preference=()) -> str:
        preference = preference or self.protocols  # match first protocol if not set
        for scheme in preference:
            if scheme in self.protocols:
                if scheme == "ssh":
                    return "git@" + self.git.replace("/", ":", 1)
                else:
                    return scheme + "://" + self.git
        return ""

    def match_path(self, path: str) -> bool:
        return match_namespace(self.path, path)

    @property
    def key(self):
        if self.git.endswith(".git"):
            return self.git[:-4]
        return self.git

    # match url and path?
    # def get_namespace(self, directory) -> Optional[Namespace]:
    #     path.split("/")

    def update_branch(self, repo: GitRepo, branch: str = ""):
        if not branch:
            branch = self.get_default_branch()
        self.branches[branch] = repo.revision

    def add_notables(self, notables: List["Notable"]) -> None:
        notables.sort(key=attrgetter("path"))
        self.notable = {n.path: n.asdict() for n in notables}

    def get_default_branch(self):
        return self.default_branch or "main"


ArtifactDict = Dict[str, dict]


RepositoryDict = Dict[str, Repository]


def find_git_repos(rootDir, gitDir=".git") -> Iterator[str]:
    for root, dirs, files in os.walk(rootDir):
        if gitDir in dirs and is_git_worktree(root, gitDir):
            del dirs[:]  # don't visit sub directories
            yield os.path.abspath(root)


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


T = TypeVar("T", bound="Notable")


class EntitySchema:
    Schema = "unfurl.cloud/onecommons/std"
    GenericFile = "artifact.File"
    ContainerFile = "artifact.Containerfile"
    CloudBlueprint = "artifact.tosca.ServiceTemplate"
    TOSCASchema = "artifact.tosca.TypeLibrary"
    Ensemble = "artifact.tosca.UnfurlEnsemble"
    UnfurlProject = "artifact.tosca.UnfurlProject"
    OCIImage = "artifacts.OCIImage"


class Notable:
    files: Sequence[str] = ()
    folders: Sequence[str] = ()
    artifact_type = EntitySchema.GenericFile

    def __init__(
        self,
        repo_info: Repository,
        root_path: str,
        folder: str,
        file: str,
        logger=logger,
    ):
        self.logger = logger
        self.root_path = root_path
        self.folder = folder
        self.file = file
        self.fragment = ""
        self.metadata: Dict[str, Any] = {}
        self.artifacts: List[ArtifactDict] = []
        self.logger.verbose("analyzing %s with %s", file, self)

    @property
    def path(self) -> str:
        if self.file:
            path = os.path.join(self.folder, self.file)
            if self.fragment:
                return path + "#" + self.fragment
            return path
        else:
            return self.folder

    @property
    def full_path(self) -> str:
        return os.path.join(self.root_path, self.folder, self.file)

    @classmethod
    def _exist_in_folder(cls, folder: str, notables: List["Notable"]):
        for n in notables:
            if cls is n.__class__ and n.folder == folder:
                return True
        return False

    @classmethod
    def init(
        cls: Type[T],
        repo_info: Repository,
        root_path: str,
        folder: str,
        file: str,
        logger=None,
    ) -> Optional[T]:
        return cls(repo_info, root_path, folder, file, logger)

    def asdict(self) -> Dict[str, Any]:
        metadata = dict(artifact_type=self.artifact_type)
        metadata.update(filter_dict(self.metadata))
        return metadata


class ContainerBuilderNotable(Notable):
    files = ("Containerfile", "Dockerfile")
    artifact_type = EntitySchema.ContainerFile

    def __init__(
        self,
        repo_info: Repository,
        root_path: str,
        folder: str,
        file: str,
        logger=logger,
    ) -> None:
        super().__init__(repo_info, root_path, folder, file, logger)


class UnfurlNotable(Notable):
    files = [
        DefaultNames.LocalConfig,
        DefaultNames.EnsembleTemplate,
        DefaultNames.Ensemble,
        "dummy-ensemble.yaml",  # DefaultNames.ServiceTemplate,  # XXX fix unfurl-types hack
    ]
    folders = [DefaultNames.ProjectDirectory, DefaultNames.EnsembleDirectory]

    def __init__(
        self,
        repo_info: Repository,
        root_path: str,
        folder: str,
        file: str,
        logger=logger,
    ) -> None:
        super().__init__(repo_info, root_path, folder, file, logger)
        # XXX set readonly=True after adding representers for AnsibleUnicode etc.
        localenv = LocalEnv(
            self.full_path,
            can_be_empty=True,
        )
        if localenv.manifestPath:
            self.artifact_type = self._get_artifacttype(localenv.manifestPath)
            manifest = localenv.get_manifest(skip_validation=True, safe_mode=True)
            rel_path = str(
                Path(localenv.manifestPath).relative_to(Path(self.root_path))
            )
            self.folder, self.file = os.path.split(rel_path)
            spec = assert_not_none(manifest.tosca)
            self.fragment = spec.fragment
            metadata = cast(dict, spec.template.tpl).get("metadata") or {}
            self.metadata.update(
                filter_dict(
                    dict(
                        name=metadata.get("template_name"),
                        version=metadata.get("template_version"),
                        description=spec.template.description,
                    )
                )
            )
            node = self._get_root_node(spec)
            schema_repo = manifest.repositories.get("types")
            if schema_repo:
                self.metadata["schema"] = schema_repo.url.strip(":")
            if node:
                types = ResourceTypesByName(
                    repo_info.key, spec.template.topology_template.custom_defs
                )
                type_dict = cast(
                    dict,
                    node_type_to_graphql(
                        node.topology,
                        assert_not_none(node.toscaEntityTemplate.type_definition),
                        types,
                        True,
                    ),
                )
                if type_dict:
                    type_dict.pop("__typename", None)
                    self.metadata.update(
                        dict(
                            type=filter_dict(type_dict),
                            dependencies=list(
                                self.find_dependencies(node, types).values()
                            ),
                        )
                    )
                image_name = self.find_image_dependency(node)
                if image_name:
                    self.artifacts.append(
                        {image_name: dict(type=EntitySchema.OCIImage)}
                    )
                    self.metadata["artifacts"] = [image_name]
        else:
            self.artifact_type = EntitySchema.UnfurlProject

    @classmethod
    def init(
        cls,
        repo_info: Repository,
        root_path: str,
        folder: str,
        file: str,
        logger=logger,
    ) -> Optional["UnfurlNotable"]:
        try:
            return UnfurlNotable(repo_info, root_path, folder, file, logger)
        except UnfurlError:
            logger.info("analysis failed for %s", file, exc_info=True)
            return None

    def find_dependencies(self, node: NodeSpec, types: ResourceTypesByName):
        return {
            name: types.expand_typename(req.get("node"))
            for name, req in cast(
                NodeTemplate, node.toscaEntityTemplate
            ).missing_requirements.items()
        }

    def find_image_dependency(self, node: NodeSpec):
        container_service = node.get_relationship("container")
        if container_service and container_service.target:
            image = container_service.target.properties.get("container", {}).get(
                "image"
            )
            if image:
                name, tag, digest, hostname = ContainerImage.split(image)
                return os.path.join(hostname or "docker.io", name or "")
        return None

    def _get_artifacttype(self, path: str) -> str:
        if path.endswith(DefaultNames.EnsembleTemplate):
            return EntitySchema.CloudBlueprint
        elif path.endswith("dummy-ensemble.yaml"):
            return EntitySchema.TOSCASchema
        else:
            return EntitySchema.Ensemble

    def _get_root_node(self, spec: ToscaSpec) -> Optional[NodeSpec]:
        topology = spec.template.topology_template
        assert topology
        node = topology.substitution_mappings and topology.substitution_mappings.node
        if node:
            assert spec.topology
            return spec.topology.get_node_template(node)
        return None


class Analyzer:
    max_analyze_depth = 4

    def __init__(self, notables: List[Type[Notable]], logger=logger):
        self.logger = logger
        self.files: Dict[str, Type[Notable]] = {}
        self.folders: Dict[str, Type[Notable]] = {}
        for n in notables:
            self.add_notable_class(n)

    def add_notable_class(self, cls: Type[Notable]):
        for file in cls.files:
            self.files[file] = cls
        for folder in cls.folders:
            self.folders[folder] = cls

    def analyze_local(self, repo_info: Repository, rootDir) -> List[Notable]:
        # currently unused
        notables: List[Notable] = []
        for root, dirs, files in os.walk(rootDir):
            notable = None
            notable_cls = None
            notables_found: List[Type[Notable]] = []
            rel_root = str(Path(root).relative_to(Path(rootDir)))
            for folder in dirs:
                notable_cls = self.folders.get(folder)
                if notable_cls:
                    if notable_cls not in notables_found:
                        notable = notable_cls.init(
                            repo_info, rootDir, rel_root, "", self.logger
                        )
                if notable:
                    dirs.remove(folder)  # don't visit folder
            for filename in files:
                notable_cls = self.files.get(filename)
                if notable_cls and notable_cls not in notables_found:
                    notable = notable_cls.init(
                        repo_info, rootDir, rel_root, filename, self.logger
                    )
            if notable:
                notables.append(notable)
                assert notable_cls
                notables_found.append(notable_cls)
        return notables

    def analyze_repo_tree(
        self,
        repo_info: Repository,
        root_path: str,
        children: List[IndexObject],
        notables: List[Notable],
        depth=-1,
    ):
        descend: List[IndexObject] = []
        if depth > self.max_analyze_depth:
            return descend
        notables_found: List[Type[Notable]] = []
        for item in children:
            notable = None
            notable_cls = None
            dirname, filename = os.path.split(item.path)
            if item.type == "tree":
                notable_cls = self.folders.get(filename)
                if notable_cls:
                    if notable_cls not in notables_found:
                        notable = notable_cls.init(
                            repo_info, root_path, cast(str, item.path), "", self.logger
                        )
                else:
                    descend.append(item)
            elif item.type == "blob":
                notable_cls = self.files.get(filename)
                if notable_cls and notable_cls not in notables_found:
                    notable = notable_cls.init(
                        repo_info, root_path, dirname, filename, self.logger
                    )
            if notable:
                notables.append(notable)
                assert notable_cls
                notables_found.append(notable_cls)
        return descend


class _LocalGitRepos:
    def __init__(self, local_repo_root: str = "", logger=logger) -> None:
        self.logger = logger
        self._set_repos(os.path.expanduser(local_repo_root))

    def _add_repo(self, working_dir: str) -> Optional[GitRepo]:
        gitrepo = git.Repo(working_dir)
        repo = GitRepo(gitrepo)
        if not repo.remote:
            self.logger.debug(f"skipping git repo in {working_dir}: no remote set")
        else:
            for remote in gitrepo.remotes:
                if not remote.url:
                    continue
                url = get_remote_git_url(remote.url)
                self.remotes.setdefault(url, []).append(remote)
                self.logger.trace(f"found git repo {url} in {working_dir}")
            self.repos[working_dir] = repo
        return None

    def _set_repos(self, root: str) -> None:
        # note: there can be a many to many relationship between upstream and local repos
        # url => remotes
        self.remotes: Dict[str, List[git.Remote]] = {}
        # working_dir => repo
        self.repos: Dict[str, GitRepo] = {}
        self.logger.debug(f"looking for repos in {root}")
        if root:
            for working_dir in find_git_repos(root):
                self._add_repo(working_dir)
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
        remotes = self.remotes.get(get_remote_git_url(url))
        if remotes:
            # find best candidate
            remote = self._choose_remote(remotes, hint)
            return self.repos[cast(str, remote.repo.working_tree_dir)]
        return None


class CloudMapDB:
    """
    Loads the cloudmap yaml file
    """

    DEFAULT_NAME = "cloudmap.yml"

    def __init__(self, path=".") -> None:
        self._load(path)

    def _load(self, path: str, contents=None):
        if os.path.isdir(path):
            path = os.path.join(path, self.DEFAULT_NAME)
        default_db = dict(apiVersion=API_VERSION, kind="CloudMap", repositories={})
        self.config = YamlConfig(
            contents or default_db,
            path,
            # validate,
            schema=os.path.join(_basepath, "cloudmap-schema.json"),
        )
        db = self.config.config
        assert isinstance(db, dict)
        repositories = cast(Dict, db.get("repositories") or {})
        self.repositories: RepositoryDict = {
            url: Repository(**r) for url, r in repositories.items()
        }
        self.artifacts: ArtifactDict = db.get("artifacts") or {}
        self.db = cast(Dict[str, Any], db)

    def reload(self):
        assert self.config.path
        self._load(self.config.path)

    def save(self):
        # maintain order of repositories so git merge is effective
        # we want to support mirrors
        self.db["repositories"] = {
            k: self.repositories[k].asdict() for k in sorted(self.repositories)
        }
        self.db.pop("artifacts", None)
        if self.artifacts:
            self.db["artifacts"] = {
                a: self.artifacts[a] for a in sorted(self.artifacts)
            }
        self.config.save()


class Directory(_LocalGitRepos):
    """
    Loads and saves a yaml file
    """

    DEFAULT_NAME = "cloudmap.yml"

    def __init__(
        self,
        path=".",
        local_repo_root: str = "",
        skip_analysis=False,
        logger=None,
    ) -> None:
        self.db = CloudMapDB(path)
        self.tmp_dir: Optional[tempfile.TemporaryDirectory] = None
        _LocalGitRepos.__init__(self, local_repo_root, logger)
        self.do_analysis = not skip_analysis
        self.analyzer = Analyzer([UnfurlNotable, ContainerBuilderNotable], logger)

    def find_local_repos_for_host(
        self, host: "RepositoryHost"
    ) -> Iterator[Tuple[git.Remote, GitRepo, Repository]]:
        """for each repo that matches host.host and host.namespace, yield matching remote and Repository"""
        for url, remotes in self.remotes.items():
            repo_info = self.db.repositories.get(url)
            if repo_info and host.has_repository(repo_info):
                remote = self._choose_remote(remotes, host.name)
                working_dir = cast(str, remote.repo.working_tree_dir)
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
        download_path = str(Path(self.repos_root) / repo_info.path)
        repo = git.Repo.clone_from(url or repo_info.git_url(), download_path)
        return GitRepo(repo)

    def analyze_repo(self, repo_info: Repository, repo: GitRepo) -> List[Notable]:
        notables: List[Notable] = []

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
                repo_info, repo.working_dir, children, notables
            ):
                if tree.type == "tree" and tree not in seen:
                    items.append(tree)
                    seen.add(tree)
        return notables

    def maybe_analyze(
        self, repo_info: Repository, repo: GitRepo, previous_notables: dict
    ) -> Optional[List[Notable]]:
        if self.do_analysis:
            try:
                return self.analyze(repo_info, repo)
            except Exception:
                # restore previous
                repo_info.notable = previous_notables
                self.logger.error(
                    "Unexpected error analyzing %s.", repo_info.key, exc_info=True
                )
        # no analysis happened, preserve previous analysis
        repo_info.notable = previous_notables
        return None

    def analyze(self, repo_info: Repository, repo: GitRepo) -> List[Notable]:
        notables = self.analyze_repo(repo_info, repo)
        self.logger.verbose("analyzing %s", repo_info.key)
        repo_info.add_notables(notables)
        for n in notables:
            for a in n.artifacts:
                self.db.artifacts.update(a)
        return notables


def get_remote_git_url(url: str) -> str:
    """Return the location of the git server without the scheme or user"""
    parts = urlparse(url)
    if not parts.netloc and not parts.scheme and "@" in url:
        # scp style used by git: user@server:project.git
        parts = urlparse("ssh://" + url.replace(":", "/", 1))
    if parts.netloc:
        user, sep, host = parts.netloc.rpartition("@")
        return host + parts.path
    return url


class RepositoryHost:
    name: str = ""
    path: str = ""
    canonical_url: str = ""
    dryrun: bool = False
    repo_filter: str = ""
    hostname: str = ""

    def __init__(self) -> None:
        self.logger = logger

    def from_host(self, directory: Directory):
        """
        Update the directory with latest from this host.
        If the directory has local repositories associated with it, update those repositories too.
        """

    def to_host(self, directory: Directory, merge: bool, force: bool) -> bool:
        """
        Update this repository host with to match.
        If merge is True and there a local repositories associate with the directory,
        merge and push any changes in the local repository.

        Returns True has a change was made to the repository host.
        """
        return False

    def match_repo_filter(self, git_url) -> bool:
        if self.repo_filter:
            if git_url.endswith(".git"):
                git_url = git_url[:-4]
            if self.repo_filter[0] == "!":
                return git_url != self.repo_filter[1:]
            return git_url == self.repo_filter
        return True

    def has_repository(self, repo_info: Repository) -> bool:
        """Check if repository belongs to this host."""
        if self.repo_filter:
            return self.match_repo_filter(repo_info.key)
        if repo_info.git.startswith(self.hostname) and repo_info.match_path(self.path):
            return True
        return False

    def fetch_repo(
        self, push_url: str, dest: Repository, local: "Directory"
    ) -> Tuple[GitRepo, bool]:
        # return the repo and if it need to be cloned
        # add remote for target repo
        repo = local.find_repo(push_url, self.name)
        if not repo:
            repo = local.find_repo(dest.git, self.name)
        missing = not repo
        if not repo:
            repo = local.clone_repo(dest, push_url)
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
                    f"{dest_remote.url} doesn't match {dest.git_url()} for remote '{remote_name}' in {repo.working_dir}"
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
        """Convert URL to canonical form if canonical_url is set."""
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
            remote_branch = f"{self.name or 'origin'}/{r.default_branch}"
            if remote_branch in repo.repo.references:
                # reset local main to remote's main
                repo.repo.git.checkout(r.default_branch, remote_branch, B=True)
        except Exception:
            self.logger.error("Error retrieving content for %s", r.key, exc_info=True)
        else:
            directory.maybe_analyze(r, repo, previous.notable if previous else {})

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
        logger=None,
    ) -> None:
        RepositoryHost.__init__(self)
        _LocalGitRepos.__init__(self, local_repo_root, logger)
        self.name = name
        self.path = namespace
        self.repo_filter = repo_filter

    def has_repository(self, repo_info: Repository) -> bool:
        if self.repo_filter:
            return self.match_repo_filter(repo_info.key)
        return repo_info.git in self.remotes

    def include_local_repo(self, repo: GitRepo) -> bool:
        return bool(repo.remote)

    def from_host(self, directory: Directory):
        """Pull latest and update the cloudmap to match the local repositories."""
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
                if self.repo_filter and not self.match_repo_filter(
                    get_remote_git_url(remote.url)
                ):
                    continue
                assert repo.repo.working_dir
                if not os.getenv("UNFURL_SKIP_UPSTREAM_CHECK"):
                    repo.pull(remote.name)
                repository = self.git_to_repository(remote, path)
                repository.initial_revision = repo.get_initial_revision()
                previous = directory.db.repositories.get(repository.key)
                if previous:
                    # don't replace metadata from remote host
                    if not previous.initial_revision:
                        previous.initial_revision = repository.initial_revision
                    repository = previous
                else:
                    directory.db.repositories[repository.key] = repository
                directory.maybe_analyze(
                    repository, repo, previous.notable if previous else {}
                )

    def to_host(self, directory: Directory, merge: bool, force: bool) -> bool:
        """Push cloudmap revisions to origin."""
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
            git=get_remote_git_url(url),
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


class HostConfig(TypedDict, total=False):
    type: Union[
        Literal["local"], Literal["gitlab"], Literal["unfurl.cloud"], Literal["github"]
    ]
    # not used by local:
    url: Required[str]
    user: str
    password: str
    visibility: str  # "public", "private", "any"
    save_internal: bool  # save repository host internals
    canonical_url: str


class LocalHostConfig(HostConfig):
    # use when type = local (LocalRepositoryHost)
    clone_root: str  # directory containing the repositories


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
        RepositoryHost.__init__(self)
        self.logger = logger
        self.name = name
        self.visibility = config.get("visibility", "any")
        self.repo_filter = repo_filter
        url = config["url"]
        parts = urlparse(url)
        user, sep, host = parts.netloc.rpartition("@")
        if sep and ":" in user:
            user, colon, token = user.partition(":")
        else:
            token = None
        self.user = config.get("user") or user
        self.token = config.get("password") or token
        self.path = namespace or parts.path.strip("/")
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

    def from_host(self, directory: Directory):
        """
        Update the directory with projects on this gitlab instance.
        If the directory has local repositories associated with it, update those repositories too.
        """
        # Gather repo info
        if self.path:
            group = self._get_group(self.path)
            if not group:
                raise Exception(f"Group {self.path} not found")
            self._import_group_from_host(group, directory)
        else:
            projects = self.gitlab.projects.list(iterator=True)
            self._import_projects_from_host(projects, directory)

    def _import_group_from_host(self, group: Group, directory: Directory):
        # XXX add/update namespace in cloudmap
        projects = group.projects.list(iterator=True)
        self.logger.info(f"importing group {group.full_path}")
        self._import_projects_from_host(projects, directory)
        for subgroup in group.subgroups.list(iterator=True):
            self._import_group_from_host(self.gitlab.groups.get(subgroup.id), directory)

    def _import_projects_from_host(
        self, projects: Iterable[RESTObject], directory: Directory
    ):
        # XXX delete removed projects
        for p in projects:
            if self.repo_filter:
                git_url, scheme = self._git_url(cast(Project, p))
                if not self.match_repo_filter(git_url):  # git_url is r.key
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

    def _import_project(
        self, dest_proj: Project, directory: Directory, download: bool
    ) -> None:
        r = self.gitlab_project_to_repository(dest_proj)
        repositories = directory.db.repositories
        previous = repositories.get(r.key)
        repositories[r.key] = r
        if download and directory.repos_root:
            # add remote branches to local repository
            # XXX pull mirror = True and merge all branches not just main?
            remote_url = self.git_url_with_auth(dest_proj)
            self._fetch_and_analyze_repo(r, directory, previous, remote_url)

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
            if not repo:
                repo = directory.find_repo(repo_info.git, self.name)
            if repo:
                # there's a local mirror that might have changed
                try:
                    # get the latest from the remote
                    repo, cloned = self.fetch_repo(remote_url, repo_info, directory)
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
                        f"Unexpected error updating upstream git for {repo_info.git}",
                        exc_info=True,
                    )

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

        if repo_info.metadata.avatar_url:
            try:
                # XXX if we have credentials for this host, add them so we can we try to access non-public avatars
                # if self.visibility != "public":
                #   gl = get_gl_for_host(repo_info.metadata.avatar_url)
                #   if gl:
                #     response = gl.session.get(avatar_url)
                #     avatar = response.content
                avatar = _urlopen(repo_info.metadata.avatar_url).read()
            except Exception:
                self.logger.error(
                    f"Error retrieving avatar at %s",
                    repo_info.metadata.avatar_url,
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

    def _git_url(self, project: Project):
        # strip out url scheme
        parts = urlparse(self.canonize(project.http_url_to_repo))
        git_url = parts.netloc + parts.path
        return git_url, parts.scheme

    def gitlab_project_to_repository(self, project: Project) -> Repository:
        self.logger.verbose("getting %s", project.http_url_to_repo)
        kw = {}
        # XXX
        # if project.license:
        #    kw["license"] = project.license.key in spdx_ids # or nickname or name
        if self.save_internal and project.avatar_url:
            # these urls point to the instance's uploaded files and aren't portable
            kw["avatar_url"] = project.avatar_url
        if project.issues_enabled:
            kw["issues_url"] = self.canonize(project.web_url + "/-/issues")

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
        git_url, scheme = self._git_url(project)
        protocols = [scheme]
        if project.ssh_url_to_repo:
            protocols.append("ssh")
        repository = Repository(
            initial_revision="",  # XXX
            git=git_url,
            name=project.name,
            protocols=protocols,
            path=project.path_with_namespace,
            default_branch=project.default_branch,
            project_url=self.canonize(project.web_url),
            metadata=metadata,
            private=self._get_project_visibility(project) != "public",
            branches={
                b.name: b.commit["id"] for b in project.branches.list(iterator=True)
            },
            tags={t.name: t.commit["id"] for t in project.tags.list(iterator=True)},
        )
        if self.save_internal:
            repository.internal_id = str(project.get_id())
        return repository


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
            self.logger = logger
            self.name = name
            self.path = namespace
            self.repo_filter = repo_filter
            self.visibility = config.get("visibility", "any")
            self.save_internal = config.get("save_internal", False)
            self.canonical_url = config.get("canonical_url", "")

            # Parse URL to extract hostname and base_url for GitHub Enterprise support
            url = config.get("url", "https://github.com")
            self.user = config.get("user", "")
            self.token = config.get("password", "")

            # Parse URL - supports both github.com and GitHub Enterprise
            if url:
                parsed_url = urlparse(url)
                # Extract token from URL if embedded (https://user:token@github.com)
                if parsed_url.username and not self.user:
                    self.user = parsed_url.username
                if parsed_url.password and not self.token:
                    self.token = parsed_url.password

                # Extract hostname
                self.hostname = parsed_url.hostname or "github.com"

                # Construct base URL for API
                if self.hostname == "github.com":
                    self.base_url = "https://github.com"
                else:
                    # GitHub Enterprise requires /api/v3 path
                    self.base_url = f"{parsed_url.scheme}://{self.hostname}"
            else:
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

        def _git_url(self, repo: "GithubRepository") -> Tuple[str, str]:
            """Extract and normalize git URL from GitHub repository, returning (git_url, scheme)."""
            # strip out url scheme
            parts = urlparse(self.canonize(repo.clone_url))
            git_url = parts.netloc + parts.path
            return git_url, parts.scheme

        def _get_organization(self, path: str) -> Optional[GithubOrganization]:
            """Get organization without creating it (helper for checking existence)."""
            try:
                return self.github.get_organization(path)
            except GithubException:
                return None

        def github_repository_to_repository(
            self, repo: "GithubRepository"
        ) -> Repository:
            """Convert PyGithub Repository object to cloudmap Repository dataclass."""
            # Extract git URL and scheme
            git_url, scheme = self._git_url(repo)

            # Build protocols list
            protocols = [scheme]
            if repo.ssh_url:
                protocols.append("ssh")

            # Extract metadata
            kw: Dict[str, Any] = {}
            if repo.homepage:
                kw["homepage_url"] = repo.homepage

            metadata = RepositoryMetadata(
                description=repo.description or "",
                topics=repo.get_topics(),
                spdx_license_id=repo.license.spdx_id if repo.license else "",
                **kw,
            )

            # Build Repository object
            repository = Repository(
                initial_revision="",  # XXX
                git=git_url,
                name=repo.name,
                protocols=protocols,
                path=f"{repo.owner.login}/{repo.name}",
                default_branch=repo.default_branch or "main",
                project_url=self.canonize(repo.html_url),
                metadata=metadata,
                private=repo.private,
                branches={b.name: b.commit.sha for b in repo.get_branches()},
                tags={t.name: t.commit.sha for t in repo.get_tags()},
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
                    return self.github.get_user(path)
                self.logger.error(f"Error fetching GitHub org '{path}'", exc_info=True)
                return None

        def _import_repositories_from_host(
            self, repos: Iterable[GithubRepository], directory: Directory
        ) -> None:
            """Import multiple repositories from GitHub into directory."""
            for repo in repos:
                # Filter by repo_filter if set
                if self.repo_filter:
                    git_url, scheme = self._git_url(repo)
                    if not self.match_repo_filter(git_url):
                        self.logger.trace(
                            "skipping %s, doesn't match %s", git_url, self.repo_filter
                        )
                        continue

                # Filter by visibility if needed
                if self.visibility == "public" and repo.private:
                    continue
                self._import_repository(repo, directory, True)

        def _import_repository(
            self, repo: GithubRepository, directory: Directory, download: bool
        ) -> None:
            # Convert and add to directory
            repositories = directory.db.repositories
            repo_info = self.github_repository_to_repository(repo)
            previous = repositories.get(repo_info.key)
            repositories[repo_info.key] = repo_info
            if download and directory.repos_root:
                # add remote branches to local repository
                # XXX pull mirror = True and merge all branches not just main?
                remote_url = self.git_url_with_auth(repo)
                self._fetch_and_analyze_repo(repo_info, directory, previous, remote_url)
            self.logger.debug(f"Imported GitHub repo: {repo.full_name}")

        def from_host(self, directory: Directory) -> None:
            """Fetch repositories from GitHub and sync to cloudmap."""
            group = self.get_owner(self.path)
            if group:
                repos = group.get_repos()
                self._import_repositories_from_host(repos, directory)

        def create_project(
            self,
            repo_info: Repository,
            dest_group: Union[GithubOrganization, AuthenticatedUser],
        ) -> GithubRepository:
            """Create new GitHub repository."""
            if self.dryrun:
                self.logger.info(f"[DRYRUN] Would create GitHub repo: {repo_info.name}")
                return None  # type: ignore

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
            if self.dryrun:
                self.logger.info(
                    f"[DRYRUN] Would update metadata for GitHub repo: {dest.full_name}"
                )
                return False

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
            """Push/sync repositories to GitHub."""
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
                    existing_repo = dest_group.get_repo(repo_info.name)
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
                        self.create_project(repo_info, owner)
                        changed = True
                    else:
                        self.logger.error(
                            f"Error checking GitHub repo {repo_info.name}: {e}"
                        )
                        raise
                else:
                    # Update existing repo
                    if self.update_project_metadata(repo_info, existing_repo):
                        changed = True

            return changed


class CloudMap:
    """
    Manages a cloudmap repository with a cloudmap.yaml file.
    Sync operations create a separate branch for each repository host to reflect its remote state.
    """

    def __init__(
        self,
        repo: GitRepo,
        host_branch: str,
        source_branch: str = "main",
        localrepo_root: str = "",
        path: str = "",
        skip_analysis: bool = False,
        logger=logger,
    ):
        self.logger = logger
        self.repo = repo
        self.host_branch = host_branch
        self.source_branch = source_branch
        self.directory = Directory(
            str(Path(repo.working_dir) / (path or "cloudmap.yaml")),
            localrepo_root,
            skip_analysis,
            logger,
        )

    @classmethod
    def get_db(
        cls,
        local_env: "LocalEnv",
        name: str = "cloudmap",
    ) -> "CloudMapDB":
        url, path, revision, repository = cls.get_config(local_env, name)
        repo, _, _ = local_env.find_or_create_working_dir(url, revision)
        if not repo:
            raise UnfurlError(f"couldn't clone {url}")
        filepath = str(Path(repo.working_dir) / (path or "cloudmap.yaml"))
        return CloudMapDB(filepath)

    @classmethod
    def from_name(
        cls,
        local_env: "LocalEnv",
        name: str,
        clone_root: str,
        host_name: str,
        namespace: str,
        skip_analysis: bool,
        logger=logger,
    ) -> "CloudMap":
        url, path, revision, repository = cls.get_config(local_env, name)
        local_repo_root = clone_root or repository.get("clone_root") or ""

        # what if branch only exists locally?
        if not host_name:
            branch = revision
            branch_exists = True
        else:
            branch = f"hosts/{host_name}"
            local_repo = local_env.find_git_repo(url, branch)
            if local_repo and branch in local_repo.repo.branches:  # type: ignore  # Unsupported right operand type for in
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
        if branch_exists:  # branch exists
            # clone or checkout branch
            repo, _, _ = local_env.find_or_create_working_dir(url, branch)
        else:
            # clone or checkout main and create branch
            repo, _, _ = local_env.find_or_create_working_dir(
                url, revision, checkout_args=dict(b=branch)
            )
        if not isinstance(repo, GitRepo):
            # XXX add find_or_create_working_dir variant that always returns GitRepo
            raise UnfurlError(f"couldn't clone {url}")
        return CloudMap(
            repo, branch, revision, local_repo_root, path, skip_analysis, logger
        )

    @classmethod
    def get_config(cls, local_env: "LocalEnv", name: str) -> Tuple[str, str, str, dict]:
        # name is a cloudmap url or a named cloudmap repository
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
        url, path, revision = split_git_url(cloudmap_url)
        return (
            normalize_git_url(url),
            path,
            revision or repository.get("revision", "main"),
            repository,
        )

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
        environment = local_env.get_context().get("cloudmaps", {})
        hosts = environment.get("hosts", {})
        host_config: Optional[HostConfig] = hosts.get(name)
        if host_config is None:
            if name == "local":
                host_config = LocalHostConfig(
                    type="local", clone_root=repos_root, url=""
                )
            elif ":" in name:
                # assume it's an url pointing to gitlab or unfurl cloud instance
                hostname = urlparse(name).hostname
                if not hostname:
                    raise UnfurlError(f"invalid url for host: {name}")
                host_config = HostConfig(type="gitlab", url=name)
                # name has to be useable as part of a branch name so just use the hostname
                name = unique_name(hostname, list(hosts))
            else:
                raise UnfurlError(f"no repository host named {name} found")
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

    def from_host(self, host: RepositoryHost) -> bool:
        host.from_host(self.directory)
        changed = self.save(
            f"Update {self.host_branch} with latest from {'/'.join([host.name, host.path])}"
        )
        return changed

    def sync(self, host: RepositoryHost, force=False) -> bool:
        """
        Synchronize the cloudmap with the given the repository host.

        First, update a branch named "hosts/{host_name}/{namespace}" with the latest from the repository host.
        Then merge the host branch into "main".
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
            if merge_host:
                self.repo.repo.git.merge(
                    self.host_branch, m=f"merge changes from syncing {self.host_branch}"
                )
                self.directory.db.reload()  # map changed, reload the directory

            # for each repository merge the host's default branch (it was already fetched during from_host())
            # into the local repo's default branch
            # since the cloudmap merge was successful this will just be a fast-forward merge
            self.directory.merge_from_host(host)

        # deploy main to the host
        host.to_host(self.directory, True, force=force)

        # cloudmap might have changed
        changed = self.save(f"{op_name}ed to {host.name}")
        if self.host_branch != self.source_branch:
            # set host branch head to match to main because we are even now
            self.repo.repo.git.branch(self.host_branch, f=True)
        return changed

    def save(self, msg: str) -> bool:
        self.directory.db.save()
        if self.repo.is_dirty(True, self.directory.db.config.path):
            assert self.directory.db.config.path
            self.repo.commit_files([self.directory.db.config.path], msg)
            self.repo.repo.index.commit(msg)
            self.logger.debug(f"committed: {msg}")
            return True
        else:
            self.logger.debug(f"nothing to commit for: {msg}")
            return False


class CloudMapInputs(TypedDict, total=False):
    host: Required[HostConfig]
    cloudmap: str  # name of cloudmap in the environment
    namespace: str
    repository: str
    clone_root: str
    skip_analysis: bool
    force: bool


class CloudMapConfigurator(Configurator):
    """
    Exports the cloudmap to the given host.
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

    def render(self, task: TaskView):
        # set this so we can track changes to it
        localEnv = task._manifest.localEnv
        assert localEnv
        inputs = cast(CloudMapInputs, task.inputs)
        namespace = inputs.get("namespace") or ""
        host = CloudMap.make_host(
            HostConfig(type="unfurl.cloud", **inputs["host"]),
            "",
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
            host.name,
            namespace,
            bool(inputs.get("skip_analysis")),
            task.logger,
        )
        # set this so we can track changes to the repo
        task.inputs["cloudmap_path"] = _getdir(task.inputs.context, cloudmap_name)
        return cloud_map, host

    def run(
        self,
        task: TaskView,
    ):
        cloud_map, host = task.rendered
        changed = cloud_map.to_host(host, False, bool(task.inputs.get("force")), False)
        return task.done(True, changed)
