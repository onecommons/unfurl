# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
"""
A cloud map is document containing metadata on collections of repositories include the artifacts and blueprints they contain.
You can use a cloud map to discover blueprints to deploy.

You can use a cloud map to manage servers that host git repositories and synchronize mirrors of git repositories.
Three types of repository hosts are currently supported: local, gitlab, and unfurl.cloud
For example, the following commands can be used push projects on a staging instance to production:

```bash
# sync latest in staging with the cloudmap
unfurl cloudmap --sync staging --namespace onecommons/blueprints

# now sync the cloudmap with production
unfurl cloudmap --sync production --namespace onecommons/blueprints

# only push the main branch to the public repo
git --push origin main
```
"""

import collections
from dataclasses import dataclass, field, asdict
from io import StringIO
from operator import attrgetter
from pathlib import Path
import tempfile
import os
import os.path
from typing import (
    Any,
    Iterator,
    Optional,
    List,
    Dict,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    cast,
)
import logging
from urllib.parse import urljoin, urlparse
import git
import git.cmd
from git.objects import IndexObject
import gitlab
from gitlab.v4.objects import Project, Group, ProjectTag, ProjectBranch

from .tosca import NodeSpec, ToscaSpec

from .support import ContainerImage

from .to_json import node_type_to_graphql
from .repo import (
    GitRepo,
    is_git_worktree,
    normalize_git_url,
    sanitize_url,
    split_git_url,
)
from .util import UnfurlError
from .localenv import LocalEnv
from .yamlloader import YamlConfig, urlopen as _urlopen
from .logs import getLogger
from . import DefaultNames

logger = getLogger("unfurl")


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
    Metadata about the repository that isn't stored in the git repository itsself but might be provided by the host
    e.g. metadata that found on the repository's GitHub or GitLab project page.
    """

    description: str = ""
    topics: List[str] = field(default_factory=list)
    spdx_license_id: str = ""
    license_url: str = ""
    issues_url: str = ""
    homepage_url: str = ""
    avatar_url: str = ""
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

    def update_branch(self, repo: GitRepo, branch: str = "main"):
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
    repo: GitRepo, remote_name: str, dest_branch: str, merge=False, force=False
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
    Schema = "unfurl.cloud/onecommons/unfurl-types"
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

    def __init__(self, root_path: str, folder: str, file: str):
        self.root_path = root_path
        self.folder = folder
        self.file = file
        self.fragment = ""
        self.metadata: Dict[str, Any] = {}
        self.artifacts: List[ArtifactDict] = []

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
    def init(cls: Type[T], root_path: str, folder: str, file: str) -> Optional[T]:
        return cls(root_path, folder, file)

    def asdict(self) -> Dict[str, Any]:
        metadata = dict(artifact_type=self.artifact_type)
        metadata.update(filter_dict(self.metadata))
        return metadata


class ContainerBuilderNotable(Notable):
    files = ("Containerfile", "Dockerfile")
    artifact_type = EntitySchema.ContainerFile

    def __init__(self, root_path: str, folder: str, file: str):
        super().__init__(root_path, folder, file)


class UnfurlNotable(Notable):
    files = [
        DefaultNames.LocalConfig,
        DefaultNames.EnsembleTemplate,
        DefaultNames.Ensemble,
        "dummy-ensemble.yaml",  # DefaultNames.ServiceTemplate,  # XXX fix unfurl-types hack
    ]
    folders = [DefaultNames.ProjectDirectory, DefaultNames.EnsembleDirectory]

    def __init__(self, root_path: str, folder: str, file: str):
        super().__init__(root_path, folder, file)
        # XXX set readonly=True after adding representers for AnsibleUnicode etc.
        localenv = LocalEnv(
            self.full_path,
            can_be_empty=True,
        )
        if localenv.manifestPath:
            self.artifact_type = self._get_artifacttype(localenv.manifestPath)
            manifest = localenv.get_manifest()
            rel_path = str(
                Path(localenv.manifestPath).relative_to(Path(self.root_path))
            )
            self.folder, self.file = os.path.split(rel_path)
            assert manifest.tosca
            self.fragment = manifest.tosca.fragment
            spec = manifest.tosca
            assert spec
            assert spec.template and spec.template.tpl
            metadata = spec.template.tpl.get("metadata") or spec.template.tpl or {}
            self.metadata.update(
                dict(
                    name=metadata.get("template_name"),
                    version=metadata.get("template_version"),
                    description=spec.template.description,
                )
            )
            node = self._get_root_node(spec)
            schema_repo = manifest.repositories.get("types")
            if schema_repo:
                self.metadata["schema"] = schema_repo.url.strip(":")
            if node:
                assert node.toscaEntityTemplate.type_definition
                type_dict = node_type_to_graphql(
                    node.topology, node.toscaEntityTemplate.type_definition, {}, True
                )
                self.metadata.update(
                    dict(
                        type=filter_dict(type_dict),
                        dependencies=list(self.find_dependencies(node).values()),
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
    def init(cls, root_path: str, folder: str, file: str) -> Optional["UnfurlNotable"]:
        try:
            return UnfurlNotable(root_path, folder, file)
        except UnfurlError:
            return None

    def find_dependencies(self, node: NodeSpec):
        return {
            name: req.get("node")
            for name, req in node.toscaEntityTemplate.missing_requirements.items()
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
        node = topology.substitution_mappings and topology.substitution_mappings.node
        if node:
            assert spec.topology
            return spec.topology.get_node_template(node)
        return None


class Analyzer:
    max_analyze_depth = 4

    def __init__(self, notables: List[Type[Notable]]):
        self.files: Dict[str, Type[Notable]] = {}
        self.folders: Dict[str, Type[Notable]] = {}
        for n in notables:
            self.add_notable_class(n)

    def add_notable_class(self, cls: Type[Notable]):
        for file in cls.files:
            self.files[file] = cls
        for folder in cls.folders:
            self.folders[folder] = cls

    def analyze_local(self, rootDir) -> List[Notable]:
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
                        notable = notable_cls.init(rootDir, rel_root, "")
                if notable:
                    dirs.remove(folder)  # don't visit folder
            for filename in files:
                notable_cls = self.files.get(filename)
                if notable_cls and notable_cls not in notables_found:
                    notable = notable_cls.init(rootDir, rel_root, filename)
            if notable:
                notables.append(notable)
                assert notable_cls
                notables_found.append(notable_cls)
        return notables

    def analyze_repo_tree(
        self,
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
                        notable = notable_cls.init(root_path, cast(str, item.path), "")
                else:
                    descend.append(item)
            elif item.type == "blob":
                notable_cls = self.files.get(filename)
                if notable_cls and notable_cls not in notables_found:
                    notable = notable_cls.init(root_path, dirname, filename)
            if notable:
                notables.append(notable)
                assert notable_cls
                notables_found.append(notable_cls)
        return descend


class _LocalGitRepos:
    def __init__(self, local_repo_root: str = "") -> None:
        self._set_repos(os.path.expanduser(local_repo_root))

    def _add_repo(self, working_dir: str) -> Optional[GitRepo]:
        gitrepo = git.Repo(working_dir)
        repo = GitRepo(gitrepo)
        if not repo.remote:
            logger.debug(f"skipping git repo in {working_dir}: no remote set")
        else:
            for remote in gitrepo.remotes:
                if not remote.url:
                    continue
                url = get_remote_git_url(remote.url)
                self.remotes.setdefault(url, []).append(remote)
                logger.debug(f"found git repo {url} in {working_dir}")
            self.repos[working_dir] = repo
        return None

    def _set_repos(self, root: str) -> None:
        # note: there can be a many to many relationship between upstream and local repos
        # url => remotes
        self.remotes: Dict[str, List[git.Remote]] = {}
        # working_dir => repo
        self.repos: Dict[str, GitRepo] = {}
        logger.debug(f"looking for repos in {root}")
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


class Directory(_LocalGitRepos):
    """
    Loads and saves a yaml file
    """

    DEFAULT_NAME = "cloudmap.yml"

    def __init__(
        self, path=".", local_repo_root: str = "", skip_analysis=False
    ) -> None:
        self.do_analysis = not skip_analysis
        self._load(path)
        self.tmp_dir: Optional[tempfile.TemporaryDirectory] = None
        super().__init__(local_repo_root)
        self.analyzer = Analyzer([UnfurlNotable, ContainerBuilderNotable])

    def _load(self, path: str):
        if os.path.isdir(path):
            path = os.path.join(path, self.DEFAULT_NAME)
        default_db = dict(apiVersion="unfurl/v1alpha1", kind="CloudMap")
        self.config = YamlConfig(
            default_db,
            path,
            # validate,
            # os.path.join(_basepath, "cloudmap-schema.json"),
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

    def find_local_repos_for_host(
        self, host: "RepositoryHost"
    ) -> Iterator[Tuple[git.Remote, GitRepo, Repository]]:
        """for each repo that matches host.host and host.namespace, yield matching remote and Repository"""
        for url, remotes in self.remotes.items():
            repo_info = self.repositories.get(url)
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
            logger.debug(f"setting {self.tmp_dir.name} as repo_root")

    def cleanup_local(self):
        if self.tmp_dir:
            self.tmp_dir.cleanup()

    def save(self):
        # maintain order of repositories so git merge is effective
        # we want to support mirrors
        self.db["schema"] = EntitySchema.Schema
        self.db["repositories"] = {
            k: self.repositories[k].asdict() for k in sorted(self.repositories)
        }
        self.db.pop("artifacts", None)
        if self.artifacts:
            self.db["artifacts"] = {
                a: self.artifacts[a] for a in sorted(self.artifacts)
            }
        self.config.save()

    def clone_repo(self, repo_info: Repository, url: str) -> GitRepo:
        # XXX handle conflict when same path, different host
        download_path = str(Path(self.repos_root) / repo_info.path)
        repo = git.Repo.clone_from(url or repo_info.git_url(), download_path)
        return GitRepo(repo)

    def analyze_repo(self, repo: GitRepo) -> List[Notable]:
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
                repo.working_dir, children, notables
            ):
                if tree.type == "tree" and tree not in seen:
                    items.append(tree)
                    seen.add(tree)
        return notables

    def maybe_analyze(
        self, repo_info: Repository, repo: GitRepo, previous_notables: dict
    ) -> Optional[List[Notable]]:
        if self.do_analysis:
            return self.analyze(repo_info, repo)
        else:  # preserve previous analysis
            repo_info.notable = previous_notables
        return None

    def analyze(self, repo_info: Repository, repo: GitRepo) -> List[Notable]:
        notables = self.analyze_repo(repo)
        repo_info.add_notables(notables)
        for n in notables:
            for a in n.artifacts:
                self.artifacts.update(a)
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

    def has_repository(self, repo_info: Repository) -> bool:
        return False

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

    def fetch_repo(
        self, push_url: str, dest: Repository, local: "Directory"
    ) -> GitRepo:
        # add remote for target repo
        repo = local.find_repo(push_url, self.name)
        if not repo:
            repo = local.find_repo(dest.git, self.name)
        if not repo:
            repo = local.clone_repo(dest, push_url)
        remote_name = self.name or "origin"
        try:
            dest_remote = repo.repo.remote(remote_name)
        except ValueError:
            dest_remote = git.Remote.create(repo.repo, remote_name, push_url)
        else:
            if normalize_git_url(dest_remote.url) != normalize_git_url(dest.git_url()):
                logger.warning(
                    f"{dest_remote.url} doesn't match {dest.git_url()} for remote '{remote_name}' in {repo.working_dir}"
                )
                # XXX should we set the url?
        if self.canonical_url and push_url != dest.git_url():
            # add a remote so we can match this repository with mirror hosts
            canonical_remote_name = "canonical"
            try:
                canonical_remote = repo.repo.remote(canonical_remote_name)
                if canonical_remote.url != dest.git_url():
                    logger.warning(
                        f"{canonical_remote.url} doesn't match {dest.git_url()} for remote {canonical_remote_name} in {repo.working_dir}"
                    )
            except ValueError:
                git.Remote.create(repo.repo, canonical_remote_name, dest.git_url())
        dest_remote.fetch()  # XXX use the revision in dest
        return repo


class LocalRepositoryHost(RepositoryHost, _LocalGitRepos):
    """
    Locally manage git repositories from any origin using the git protocol.
    """
    def __init__(self, local_repo_root: str = "", namespace: str = "") -> None:
        _LocalGitRepos.__init__(self, local_repo_root)
        self.path = namespace

    def has_repository(self, repo_info: Repository) -> bool:
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
                assert repo.repo.working_dir
                if not os.getenv("UNFURL_SKIP_UPSTREAM_CHECK"):
                    repo.pull(remote.name)
                repository = self.git_to_repository(remote, path)
                repository.initial_revision = repo.get_initial_revision()
                previous = directory.repositories.get(repository.key)
                if previous:
                    # don't replace metadata from remote host
                    if not previous.initial_revision:
                        previous.initial_revision = repository.initial_revision
                    repository = previous
                else:
                    directory.repositories[repository.key] = repository
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


class GitlabManager(RepositoryHost):
    def __init__(self, name: str, config: Dict[str, str], namespace: str = ""):
        self.name = name
        self.visibility = config.get("visibility", "any")
        url = config["url"]
        parts = urlparse(url)
        user, sep, host = parts.netloc.rpartition("@")
        if sep and ":" in user:
            user, colon, token = user.partition(":")
        self.user = config.get("user") or user
        self.token = config.get("password") or token
        self.path = namespace or parts.path.strip("/")
        self.canonical_url = config.get("canonical_url") or ""
        if self.canonical_url:
            self.hostname = urlparse(self.canonical_url).netloc
        else:
            self.hostname = host
        url = parts._replace(netloc=host, path="").geturl()
        self.gitlab = gitlab.Gitlab(url, private_token=self.token)
        logger.info(f"connecting to {self.gitlab.url} namespace {self.path}")
        self.gitlab.auth()

    def has_repository(self, repo_info: Repository) -> bool:
        if repo_info.git.startswith(self.hostname) and repo_info.match_path(self.path):
            return True
        return False

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
        logger.info(f"importing group {group.full_path}")
        self._import_projects_from_host(projects, directory)
        for subgroup in group.subgroups.list(iterator=True):
            self._import_group_from_host(self.gitlab.groups.get(subgroup.id), directory)

    def _import_projects_from_host(self, projects, directory: Directory):
        # XXX delete removed projects
        repositories = directory.repositories
        for p in projects:
            dest_proj: Project = self.gitlab.projects.get(p.id)
            if self.visibility == "public" and dest_proj.visibility != "public":
                continue
            try:
                dest_proj.default_branch
            except AttributeError:
                # project without repositories will throw error, skip those
                logger.warning(
                    f"skipping project {dest_proj.web_url}, it doesn't have a git repository"
                )
                continue
            r = self.gitlab_project_to_repository(dest_proj)

            previous = repositories.get(r.key)
            repositories[r.key] = r
            if directory.repos_root:
                # add remote branches to local repository
                # XXX pull mirror = True and merge all branches not just main?
                remote_url = self.git_url_with_auth(dest_proj)
                repo = self.fetch_repo(remote_url, r, directory)
                directory.maybe_analyze(r, repo, previous.notable if previous else {})

    def _get_projects_from_group(self, group, projects):
        for p in group.projects.list(iterator=True):
            projects[p.path_with_namespace][0] = p  # type: ignore
        for subgroup in group.subgroups.list(iterator=True):
            self._get_projects_from_group(self.gitlab.groups.get(subgroup.id), projects)

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
            r for r in directory.repositories.values() if self.has_repository(r)
        ]

        dest_path = self.path
        logger.info(f"syncing to {dest_path}")
        if not repositories:
            logger.info("no matches")
            return False

        dest_group = self.ensure_group(dest_path)
        if not dest_group:
            logger.info("%s doesn't exist", dest_path)
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
                if dest:
                    # if both exist, update any changed metadata
                    if self.dryrun:
                        logger.info(
                            "dry run: skipping creating updating project %s", name
                        )
                    else:
                        self.update_project_metadata(repo_info, dest)
                    do_merge = not force and merge
                else:
                    if self.dryrun:
                        logger.info("dry run: skipping creating project %s", name)
                        continue
                    # create the project
                    dest = self.create_project(repo_info, dest_group)
                    do_merge = False
                assert dest
                if directory.repos_root:
                    remote_url = self.git_url_with_auth(dest)
                    repo = directory.find_repo(dest.http_url_to_repo, self.name)
                    if not repo:
                        repo = directory.find_repo(repo_info.git, self.name)
                    if repo:
                        # there's a local mirror that might have changed
                        try:
                            repo = self.fetch_repo(remote_url, repo_info, directory)
                            # clone source repo and push to dest
                            dest_branch = (
                                f"{self.name}/{repo_info.get_default_branch()}"
                            )
                            commit = repo.repo.head.commit
                            branch_exists = dest_branch in repo.repo.references
                            if (
                                not branch_exists
                                or commit != repo.repo.references[dest_branch].commit
                            ):
                                # now update project repository
                                logger.info(
                                    f"{force and '(force) ' or ' '}{do_merge and 'merging' or 'pushing'} local repository to {repo.safe_url}"
                                )
                                if self.dryrun:
                                    summary = cast(str, commit.summary)
                                    logger.info(
                                        f"dry run: would have pushed commit {commit.hexsha[:6]} {commit.committed_datetime} {summary}"
                                    )
                                else:
                                    force_merge_local_and_push_to_remote(
                                        repo,
                                        self.name,
                                        dest_branch,
                                        merge=branch_exists and do_merge,
                                        force=force,
                                    )
                                if do_merge:
                                    # we might have create a merge commit, update the directory
                                    repo_info.update_branch(repo, "main")
                            else:
                                logger.debug(
                                    f"skipping push: no change detected on branch {dest_branch} for {repo.safe_url}"
                                )
                        except Exception:
                            logger.error(
                                f"Unexpected error updating upstream git for {repo_info.git}",
                                exc_info=True,
                            )
            # delete any extra projects
            # XXX: enable when ready
            if not repo_info and dest:
                logger.info(f"would delete {dest_path}")
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
        logger.info(f"ensuring group {path} on {gitlab.url}")

        # see if group exists first
        group = self._get_group(path)
        if self.dryrun:
            return group

        # create if missing
        if group is None:
            parent = None
            path_so_far = []

            for name in path.split("/"):
                path_so_far.append(name)
                group = self._get_group("/".join(path_so_far))

                # create group if missing
                if group is None:
                    full_path = "/".join(path_so_far)
                    logger.info(f"creating group {full_path}")
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
        logger.info(f"creating project {repo_info.path}")

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
            avatar = _urlopen(repo_info.metadata.avatar_url).read()
            new_project.avatar = avatar
            new_project.save()

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
        # XXX dest_proj.visibility = upstream_proj.visibility
        if not changed:
            return False
        try:
            dest_proj.save()
        except Exception:
            logger.error("failed to save", dest_proj.path, exc_info=True)
            return False
        return True

    def canonize(self, url: str) -> str:
        if self.canonical_url:
            parts = urlparse(url)
            return urljoin(self.canonical_url, parts.path)
        else:
            return url

    def gitlab_project_to_repository(self, project: Project) -> Repository:
        kw = {}
        # XXX
        # if project.license:
        #    kw["license"] = project.license.key in spdx_ids # or nickname or name
        # if project.avatar_url:
        #     kw["avatar_url"] = project.avatar_url
        if project.issues_enabled:
            kw["issues_url"] = self.canonize(project.web_url + "/-/issues")

        # https://docs.gitlab.com/ee/api/projects.html#get-single-project
        metadata = RepositoryMetadata(
            description=project.description,
            topics=project.topics,
            homepage_url=self.canonize(project.web_url),
            **kw,
        )
        metadata.set_lastupdate()
        # strip out url scheme
        parts = urlparse(self.canonize(project.http_url_to_repo))
        git_url = parts.netloc + parts.path
        protocols = [parts.scheme]
        if project.ssh_url_to_repo:
            protocols.append("ssh")
        repository = Repository(
            initial_revision="",  # XXX
            git=git_url,
            name=project.name,
            protocols=protocols,
            path=project.path_with_namespace,
            default_branch=project.default_branch,
            # internal_id=str(project.get_id()),
            project_url=self.canonize(project.web_url),
            metadata=metadata,
            private=project.visibility != "public",
            branches={
                b.name: b.commit["id"] for b in project.branches.list(iterator=True)
            },
            tags={t.name: t.target for t in project.tags.list(iterator=True)},
        )
        return repository


class CloudMap:
    """
    Manages a cloudmap repository with a cloudmap.yaml file.
    Sync operations create a separate branch for each repository host to reflect its remote state.
    """

    def __init__(
        self,
        repo: GitRepo,
        host_branch: str,
        localrepo_root: str = "",
        path: str = "",
        skip_analysis: bool = False,
    ):
        self.repo = repo
        self.branch = host_branch
        self.directory = Directory(
            str(Path(repo.working_dir) / (path or "cloudmap.yaml")),
            localrepo_root,
            skip_analysis,
        )

    @classmethod
    def from_name(
        cls,
        local_env: "LocalEnv",
        name: str,
        clone_root: str,
        host_name: str,
        namespace: str,
        skip_analysis: bool,
    ) -> "CloudMap":
        environment = local_env.get_context().get("cloudmaps", {})
        # for now name is just the name of repository
        repository = environment.get("repositories", {}).get(name)
        if repository:
            cloudmap_url = repository["url"]
            localrepo_root = repository.get("clone_root", clone_root)
        else:
            # assume name is an url or local path
            cloudmap_url = name
            localrepo_root = clone_root
        url, path, revision = split_git_url(cloudmap_url)
        url = normalize_git_url(url)

        # what if branch only exists locally?
        if not host_name:
            branch = revision or "main"
            branch_exists = True
        else:
            branch = f"hosts/{host_name}"
            local_repo = local_env.find_git_repo(url, branch)
            if local_repo and branch in local_repo.repo.branches:  # type: ignore
                branch_exists = True
            else:
                branch_exists = bool(git.cmd.Git().ls_remote(url, branch, heads=True))
        if branch_exists:  # branch exists
            # clone or checkout branch
            repo, _, _ = local_env.find_or_create_working_dir(url, branch)
        else:
            # clone or checkout main and create branch
            repo, _, _ = local_env.find_or_create_working_dir(
                url, "main", checkout_args=dict(b=branch)
            )
        if not repo:
            raise UnfurlError(f"couldn't clone {url}")
        return CloudMap(repo, branch, localrepo_root, path, skip_analysis)

    def get_host(
        self,
        local_env: "LocalEnv",
        name: str,
        namespace: str,
        visibility: Optional[str] = None,
    ) -> RepositoryHost:
        environment = local_env.get_context().get("cloudmaps", {})
        host_config: Optional[dict] = environment.get("hosts", {}).get(name)
        clone_root = self.directory.repos_root
        if host_config is None:
            if name == "local":
                host_config = dict(type="local", clone_root=clone_root)
            elif ":" in name:
                # assume it's an url pointing to gitlab or unfurl cloud instance
                host_config = dict(type="gitlab", url=name)
            else:
                raise UnfurlError(f"no repository host named {name} found")
        if host_config["type"] == "local":
            return LocalRepositoryHost(host_config.get("clone_root", clone_root), namespace)

        assert host_config["type"] in ["gitlab", "unfurl.cloud"]
        config = local_env.map_value(host_config, environment.get("variables"))
        if visibility:
            config["visibility"] = visibility
        return GitlabManager(name, config, namespace)

    def from_host(self, host: RepositoryHost) -> bool:
        host.from_host(self.directory)
        changed = self.save(
            f"Update {self.branch} with latest from {'/'.join([host.name, host.path])}"
        )
        return changed

    def sync(self, host: RepositoryHost, force=False) -> None:
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
        return self.to_host(host, changed, force)

    def to_host(self, host: RepositoryHost, merge_host: bool, force=False) -> None:
        if self.branch != "main":
            self.repo.checkout("main")
            self.directory.reload()  # map may have changed, reload the directory
            # make sure local repos matches the cloudmap
            mismatched = self.directory.find_mismatched_repo(host)
            if mismatched:
                raise UnfurlError(
                    f"Aborting sync, cloudmap is out of sync with {mismatched.working_dir}"
                )
            # merge the host branch into main
            # there will be a merge conflict if a repository branch or tags was changed in both branches
            # if so, manually merge the changes in the local repo (they will be on the remote branch), sync them with the cloudmap, then re-run
            if merge_host:
                self.repo.repo.git.merge(
                    self.branch, m=f"merge changes from syncing {self.branch}"
                )
                self.directory.reload()  # map changed, reload the directory

            # for each repository merge the host's default branch (it was already fetched during from_host())
            # into the local repo's default branch
            # since the cloudmap merge was successful this will just be a fast-forward merge
            self.directory.merge_from_host(host)

        # deploy main to the host
        host.to_host(self.directory, True, force=force)

        # if force we might have created merge commits, update the cloudmap with those
        self.save(f"synced {host.name}")
        if self.branch != "main":
            # set host branch head to match to main because we are even now
            self.repo.repo.git.branch(self.branch, f=True)

    def save(self, msg: str) -> bool:
        self.directory.save()
        if self.repo.is_dirty(True, self.directory.config.path):
            assert self.directory.config.path
            self.repo.commit_files([self.directory.config.path], msg)
            self.repo.repo.index.commit(msg)
            logger.debug(f"committed: {msg}")
            return True
        else:
            logger.debug(f"nothing to commit for: {msg}")
            return False
