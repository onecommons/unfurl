# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""Repository hosts: the interface a cloud map syncs against.

:py:class:`RepositoryHost` is the base every host implementation subclasses --
:py:class:`LocalRepositoryHost` here, and :py:class:`~unfurl.cloudmap.gitlab.GitlabManager`
/ :py:class:`~unfurl.cloudmap.github.GithubManager` in their own modules.
"""

from __future__ import annotations

import os
import os.path
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
    cast,
)
from urllib.parse import urljoin, urlparse

import git

from tosca import safe_mode

from ..logs import getLogger, UnfurlLogger
from ..repo import GitRepo, is_git_worktree, normalize_git_url, sanitize_url
from ..tosca_plugins.cloudmap_defs import (
    Instantiation,
    Repository,
    TypeRefStatus,
    get_repository_url,
)

if TYPE_CHECKING:
    from . import Directory

logger = getLogger("unfurl")


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
            directory.maybe_analyze(r, repo, previous.contains if previous else {})

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
        status: Optional[List[str]] = None,
        workflow_file: str = "",
        trigger: Optional[List[str]] = None,
        context: Optional["Directory"] = None,
    ) -> Iterable[Instantiation]:
        f"""Return pipeline/workflow run Instantiations for the given repository.

        Results are ordered newest-first (by creation time descending).

        Args:
            repo_info: The Repository record
            ref: Git ref (branch or tag name) to filter runs
            commit: Git commit SHA to filter runs
            limit: Max number of runs to return (default: {self.DEFAULT_PIPELINE_LIMIT})
            status: If given, only return runs whose status is in this list
                (GitLab pipeline ``status``; GitHub run ``status`` or
                ``conclusion``). The platform API is used to filter when a
                single status is requested, otherwise filtering is client-side.
            workflow_file: If given, only return runs for this workflow file
                path. For GitLab, matched against the project's CI config path
                (client-side; returns nothing if it doesn't match). For GitHub,
                matched against each run's ``path`` field (client-side filter).
            trigger: If given, only return runs whose trigger matches
                (GitLab pipeline ``source`` / GitHub run ``event``). The
                platform API is used to filter when a single value is given,
                otherwise filtering is client-side.
            context: If given, each built Instantiation is passed to a matching
                :class:`PipelineRunAnalyzer` (looked up via
                ``context.cloudmap.find_pipeline_analyzers``) for enrichment.
        """
        return []

    def _run_pipeline_analyzer(
        self,
        context: "Directory",
        repo_info: "Repository",
        instantiation: Instantiation,
        obj: Any,
        source: str,
    ) -> None:
        """Invoke a matching :class:`PipelineRunAnalyzer` to enrich ``instantiation``.

        Looks up a custom analyzer by ``repo_info.url`` and ``source``. The raw
        platform object ``obj`` is withheld (set to ``None``) when running in
        safe mode, since sandboxed analyzers must not access the API client.
        """
        analyzer_cls = context.cloudmap.find_pipeline_analyzers(repo_info, source)
        if analyzer_cls is None:
            return
        if safe_mode():
            obj = None  # withhold raw API object from sandboxed analyzers
        repo = context.find_repo(repo_info.url, "")
        root_path = repo.working_dir if repo else ""
        try:
            analyzer_cls().analyze_pipeline_run(
                context, repo_info, instantiation, obj, root_path
            )
        except Exception:
            self.logger.error(
                "Pipeline run analyzer failed for %s", repo_info.url, exc_info=True
            )


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
                    repository, repo, previous.contains if previous else {}
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


_CI_STATUS_MAP: Dict[
    str,
    TypeRefStatus,
] = {
    "success": "present",
    "failed": "failed",
    "running": "unknown",
    "pending": "absent",
    "canceled": "failed",
    "cancelled": "failed",
    "skipped": "absent",
}


def map_ci_status(
    status: str,
) -> Optional[TypeRefStatus]:
    """Map GitHub/GitLab CI status to Instantiation status."""
    return _CI_STATUS_MAP.get(status)
