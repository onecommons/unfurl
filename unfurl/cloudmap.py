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
unfurl cloudmap --sync staging --namespace onecommons/blueprint --namespace onecommons/blueprints

# now sync the cloudmap with production
unfurl cloudmap --sync production --namespace onecommons/blueprints

# only push the main branch to the public repo
git --push origin main
```
"""

import collections
from dataclasses import dataclass, field, asdict
from io import StringIO
from pathlib import Path
import tempfile
import os
import os.path
from typing import Any, Iterator, Optional, List, Dict, Tuple, cast
import logging
from urllib.parse import urljoin, urlparse
import git
import git.cmd
import gitlab
from gitlab.v4.objects import Project, Group, ProjectTag, ProjectBranch
from ruamel.yaml import YAML

from .repo import GitRepo, is_git_worktree, normalize_git_url, split_git_url

from .util import UnfurlError

from .localenv import LocalEnv

from .yamlloader import YamlConfig, urlopen as _urlopen

from .logs import getLogger

logger = getLogger("unfurl")

yaml = YAML()


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
        if not path or path == self.path:
            return True
        if not self.path:
            return False
        # don't match on partial segments
        return self.path.startswith(os.path.join(path, ""))

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


RepositoryDict = Dict[str, Repository]


def find_git_repos(rootDir, gitDir=".git") -> Iterator[str]:
    for root, dirs, files in os.walk(rootDir):
        if gitDir in dirs and is_git_worktree(root, gitDir):
            del dirs[:]  # don't visit sub directories
            yield os.path.abspath(root)


def force_merge_local_and_push_to_remote(
    repo: GitRepo, remote_name: str, dest_branch: str, merge=False, force=False
):
    """Update existing project repo with changes from upstream and"""
    if merge:
        # merge the remote branch into the current local HEAD
        # Use "ours" merge strategy so the resulting tree of the merge is always that of the current branch head, effectively
        # ignoring all changes from all other branches.
        # This creates a merge commit equivalent to a force push without rewriting history
        repo.repo.git.merge(dest_branch, s="ours")

    # push local HEAD to remote "main" branch
    # skip the pipeline because that might cause additional commits
    pushinfo = repo.repo.remotes[remote_name].push(
        o="ci.skip", follow_tags=True, force=force
    )[0]
    logger.info(f"pushed to {repo.safe_url}: {pushinfo.summary}")


class _LocalGitRepos:
    def __init__(self, local_repo_root: str = "") -> None:
        self._set_repos(os.path.expanduser(local_repo_root))

    @staticmethod
    def _add_repo(working_dir: str, repos: Dict[str, GitRepo]):
        repo = GitRepo(git.Repo(working_dir))
        if not repo.remote:
            logger.debug(f"skipping git repo in {working_dir}: no remote set")
        else:
            # XXX get all remotes
            if "canonical" in repo.repo.remotes:
                remote_url = repo.repo.remotes["canonical"].url
            else:
                remote_url = repo.remote.url
            url = get_remote_git_url(remote_url)
            if url:
                logger.debug(f"found git repo {url} in {working_dir}")
                repos[url] = repo
                return repo
            else:
                logger.debug(f"skipping git repo in {working_dir}: no remote set")
        return None

    def _set_repos(self, root: str):
        repos: Dict[str, GitRepo] = {}
        logger.debug(f"looking for repos in {root}")
        if root:
            for working_dir in find_git_repos(root):
                self._add_repo(working_dir, repos)
        self.repos = repos
        self.repos_root = root


class Directory(_LocalGitRepos):
    """
    Loads and saves a yaml file
    """

    DEFAULT_NAME = "cloudmap.yml"

    def __init__(self, path=".", local_repo_root: str = "") -> None:
        self._load(path)
        self.tmp_dir: Optional[tempfile.TemporaryDirectory] = None
        super().__init__(local_repo_root)

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
        repositories = cast(Dict, db and db.get("repositories") or {})
        self.repositories: RepositoryDict = {
            url: Repository(**r) for url, r in repositories.items()
        }
        self.db = cast(Dict[str, Any], db)

    def reload(self):
        assert self.config.path
        self._load(self.config.path)

    def find_local_repos_for_host(self, host: "RepositoryHost"):
        """for each repo that matches host.host and host.namespace, check that HEAD matches the repository entry"""
        for url, repo in self.repos.items():
            repo_info = self.repositories.get(url)
            if repo_info and host.has_repository(repo_info):
                yield repo, repo_info

    def find_mismatched_repo(self, host: "RepositoryHost") -> Optional[GitRepo]:
        for repo, repo_info in self.find_local_repos_for_host(host):
            if repo.revision != repo_info.branches["main"]:
                return repo
        return None

    def merge(self, host: "RepositoryHost"):
        """For each git repo that has a branch for the repository host, merge it into main"""
        host_branch = f"{host.name}/main"
        for repo in self.repos.values():
            if host_branch in repo.repo.branches:  # type: ignore
                repo.repo.git.merge(host_branch)

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
        self.db["repositories"] = {
            k: self.repositories[k].asdict() for k in sorted(self.repositories)
        }
        self.config.save()

    def find_repo(self, url: str) -> Optional[GitRepo]:
        return self.repos.get(get_remote_git_url(url))

    def clone_repo(self, repo_info: Repository, url: str) -> GitRepo:
        download_path = str(Path(self.repos_root) / repo_info.path)
        repo = git.Repo.clone_from(url or repo_info.git_url(), download_path)
        return GitRepo(repo)


def get_remote_git_url(url):
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
        repo = local.find_repo(push_url)
        if not repo:
            repo = local.find_repo(dest.git)
        if not repo:
            repo = local.clone_repo(dest, push_url)
        try:
            dest_remote = repo.repo.remote(self.name or "origin")
        except ValueError:
            dest_remote = git.Remote.create(repo.repo, self.name or "origin", push_url)
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

    def has_repository(self, repo_info: Repository) -> bool:
        return repo_info.git in self.repos

    def include_local_repo(self, repo: GitRepo) -> bool:
        return bool(repo.remote)  # XXX check host and namespace filters too

    def from_host(self, directory: Directory):
        """Pull latest and update the cloudmap to match the local repositories."""
        for url, repo in self.repos.items():
            if self.include_local_repo(repo):
                repo.pull()  # XXX make optional?
                assert repo.repo.working_dir
                path = str(
                    Path(repo.repo.working_dir).relative_to(
                        Path(os.path.abspath(self.repos_root))
                    )
                )
                repository = self.git_to_repository(repo, path)
                directory.repositories[repository.key] = repository

    def to_host(self, directory: Directory, merge: bool, force: bool) -> bool:
        """Push cloudmap revisions to origin."""
        matched = False
        for url, repo in self.repos.items():
            if self.include_local_repo(repo):
                if directory.repos_root and self.repos_root != directory.repos_root:
                    cloudmap_local_repo = directory.repos.get(url)
                    if cloudmap_local_repo:
                        repo.pull(cloudmap_local_repo.working_dir)
                repo.push()
                matched = True
        return matched

    @staticmethod
    def git_to_repository(repo: GitRepo, path: str) -> "Repository":
        assert repo.remote  # origin
        url = repo.remote.url
        record = Repository(
            git=get_remote_git_url(url),
            path=path,
            protocols=[urlparse(url).scheme or "ssh"],
            initial_revision=repo.get_initial_revision(),
        )
        # to get default branch:
        # first line of git ls-remote --symref url
        # ref: refs/heads/main	HEAD

        # add origin's refs as branches
        # refs iterates over .git/refs/remotes/origin/*
        remote_refs = sorted(repo.repo.remote().refs, key=lambda r: r.name)
        # (git fetch --tags to get the latest tag refs)
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
            r = self.gitlab_project_to_repository(dest_proj)
            repositories[r.key] = r
            if directory.repos_root:
                # add remote branches to local repository
                # XXX pull mirror = True and merge all branches not just main?
                remote_url = self.git_url_with_auth(dest_proj)
                self.fetch_repo(remote_url, r, directory)

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
        # XXX look up Namespace and sync it?

        projects = cast(
            Dict[str, Tuple[Optional[Project], Optional[Repository]]],
            collections.defaultdict(lambda: [None, None]),
        )
        for p in dest_group.projects.list(iterator=True):
            projects[p.path_with_namespace][0] = p  # type: ignore
        for r in repositories:
            projects[r.path][1] = r  # type: ignore

        for name in projects:
            dest, repo_info = projects[name]
            if repo_info:
                if dest:
                    # if both exist, update any changed metadata
                    self.update_project_metadata(repo_info, dest)
                    do_merge = merge
                else:
                    # create the project
                    dest = self.create_project(repo_info, dest_group)
                    do_merge = False
                assert dest
                if directory.repos_root:
                    remote_url = self.git_url_with_auth(dest)
                    repo = directory.find_repo(dest.http_url_to_repo)
                    if not repo:
                        repo = directory.find_repo(repo_info.git)
                    if repo:
                        # now update project repository
                        logger.info(
                            f"{force and '(force) ' or ' '}{do_merge and 'merging' or 'pushing'} local repository to {repo.safe_url}"
                        )
                        # there's a local mirror that might have changed
                        try:
                            repo = self.fetch_repo(remote_url, repo_info, directory)
                            # clone source repo and push to dest
                            dest_branch = f"{self.name}/main"
                            force_merge_local_and_push_to_remote(
                                repo,
                                self.name,
                                dest_branch,
                                merge=do_merge,
                                force=force,
                            )
                            if do_merge:
                                # we might have create a merge commit, update the directory
                                repo_info.update_branch(repo, "main")
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
        return f"https://{self.user}:{self.token}@{project.http_url_to_repo.lstrip('https://')}"

    # only fetch group, don't create it
    def _get_group(self, path: str) -> Optional[Group]:
        try:
            return cast(Group, self.gitlab.groups.get(path))
        except Exception:
            return None

    def ensure_group(self, path: str) -> Group:
        """Get or create the given group in the Gitlab instance"""
        gitlab = self.gitlab
        logger.info(f"ensuring group {path} on {gitlab.url}")

        # see if group exists first
        group = self._get_group(path)

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
        logger.info(f"  creating {repo_info.path}")

        project_path, namespace = os.path.split(repo_info.path)
        if namespace != self.path:
            dest_group = self.ensure_group(namespace)
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
        if project.avatar_url:
            kw["avatar_url"] = self.canonize(project.avatar_url)
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
            internal_id=str(project.get_id()),
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
        provider_branch: str,
        localrepo_root: str = "",
        path: str = "",
    ):
        self.repo = repo
        self.branch = provider_branch
        self.directory = Directory(
            str(Path(repo.working_dir) / (path or "cloudmap.yaml")), localrepo_root
        )

    @classmethod
    def from_name(
        cls,
        local_env: "LocalEnv",
        name: str,
        clone_root: str,
        provider_name: str,
        namespace: str,
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
        if not provider_name:
            branch = revision or "main"
            branch_exists = True
        else:
            branch = f"hosts/{provider_name}/{namespace}".rstrip("/")
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
        return CloudMap(repo, branch, localrepo_root, path)

    def get_host(
        self,
        local_env: "LocalEnv",
        name: str,
        namespace: str,
        visibility: Optional[str] = None,
    ) -> RepositoryHost:
        environment = local_env.get_context().get("cloudmaps", {})
        provider_config: Optional[dict] = environment.get("hosts", {}).get(name)
        clone_root = self.directory.repos_root
        if provider_config is None:
            if name == "local":
                provider_config = dict(type="local", clone_root=clone_root)
            elif ":" in name:
                # assume it's an url pointing to gitlab or unfurl cloud instance
                provider_config = dict(type="gitlab", url=name)
            else:
                raise UnfurlError(f"no repository host named {name} found")
        if provider_config["type"] == "local":
            return LocalRepositoryHost(provider_config.get("clone_root", clone_root))

        assert provider_config["type"] in ["gitlab", "unfurl.cloud"]
        config = local_env.map_value(provider_config, environment.get("variables"))
        if visibility:
            config["visibility"] = visibility
        return GitlabManager(name, config, namespace)

    def sync(self, host: RepositoryHost, force=False) -> None:
        """
        Synchronize the cloudmap with the given the repository host.

        First, update a branch named "hosts/{host_name}/{namespace}" with the latest from the repository host.
        Then merge the provider branch into "main".
        If a conflict is detected, abort with a merge error in the cloudmap repository.
        For example, if a repository branch or tags was changed in both branches there will be a merge conflict.
        If so, manually merge the changes in the local repo (they will be on the remote branch), sync them with the cloudmap, then re-run this command.

        Finally, update the repository host with any changes it needs to match the cloudmap.
        New project will be create or with existing project metadata updated and changes in local repository clones will be pushed to the host.

        The user is responsible for pushing changes to cloudmap repository back upstream.
        """
        host.from_host(self.directory)
        changed = self.save(
            f"Update {self.branch} with latest from {'/'.join([host.name, host.path])}"
        )

        if self.branch != "main":
            self.repo.checkout("main")
            self.directory.reload()  # map may have changed, reload the directory
            # make sure local repos matches the cloudmap
            mismatched = self.directory.find_mismatched_repo(host)
            if mismatched:
                raise UnfurlError(
                    f"Aborting sync, cloudmap is out of sync with {mismatched.working_dir}"
                )
            # merge the provider branch into main
            # there will be a merge conflict if a repository branch or tags was changed in both branches
            # if so, manually merge the changes in the local repo (they will be on the remote branch), sync them with the cloudmap, then re-run
            if changed:
                self.repo.repo.git.merge(self.branch)  # --commit --no-edit
            self.directory.reload()  # map changed, reload the directory

            # for each repository merge the provider's branch
            # (which was fetched during from_provider()) into main
            # since the cloudmap merge was successful this will just be a fast-forward merge
            self.directory.merge(host)

        # deploy main to the provider
        host.to_host(self.directory, True, force=force)

        # if force we might have created merge commits, update the cloudmap with those
        self.save(f"synced {host.name}")
        if self.branch != "main":
            # set provider branch match to main because we are even now
            self.repo.repo.git.branch(self.branch, f=True)

    def save(self, msg: str) -> bool:
        self.directory.save()
        if self.repo.is_dirty(True):
            self.repo.add_all(self.repo.working_dir)
            self.repo.repo.index.commit(msg)
            logger.debug(f"committed: {msg}")
            return True
        else:
            logger.debug(f"nothing to commit for: {msg}")
            return False
