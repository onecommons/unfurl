# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""GitHub repository host support for cloud maps.

Implements :py:class:`GithubManager`, the :py:class:`~unfurl.cloudmap.RepositoryHost`
that syncs a cloud map with GitHub (or GitHub Enterprise) via ``PyGithub``.
``PyGithub`` is an optional dependency: when it isn't installed
``GithubManager`` is ``None`` and callers are expected to check for it.
"""

from __future__ import annotations

from itertools import islice
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Union, cast
from urllib.parse import urlparse

from ..logs import getLogger
from ..tosca_plugins.cloudmap_defs import (
    CommonMetadata,
    EntitySchema,
    HostConfig,
    Instantiation,
    PipelineArtifact,
    PipelineRunProperties,
    Repository,
    RepositoryMetadata,
    TypeRefConstraint,
    TypeRefs,
    get_repository_url,
)
from .host import RepositoryHost, map_ci_status

if TYPE_CHECKING:
    from . import Directory

logger = getLogger("unfurl")


def _github_run_properties(run: "WorkflowRun") -> PipelineRunProperties:
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

    actor = run.actor.login if getattr(run, "actor", None) else ""
    # The workflow-run payload embeds head_commit with a top-level
    # `timestamp`; PyGithub doesn't surface it as a typed attribute, so
    # read it from the already-loaded raw data (no extra request).
    committed_at = ""
    try:
        head_commit = run.raw_data.get("head_commit") or {}
        committed_at = head_commit.get("timestamp", "") or ""
    except Exception:
        pass

    pp = PipelineRunProperties(
        id=run.id,
        run_number=getattr(run, "run_number", 0) or 0,
        # `conclusion` is the outcome once completed (success/failure/...);
        # fall back to `status` (queued/in_progress) while still running.
        status=getattr(run, "conclusion", "") or getattr(run, "status", "") or "",
        log_url=run.logs_url,
        trigger=getattr(run, "event", "") or "",
        actor=actor,
        artifacts=artifacts,
        artifacts_expire_at=artifacts_expire_at,
    )
    # PyGithub returns datetimes; emit RFC 3339 strings, skipping None.
    if committed_at:
        pp["committed_at"] = committed_at
    if run.created_at:
        pp["created_at"] = run.created_at.isoformat()
    if run.run_started_at:
        pp["started_at"] = run.run_started_at.isoformat()
    if run.updated_at:
        pp["finished_at"] = run.updated_at.isoformat()
    return pp


# PyGithub is optional - only needed for GitHub integration
try:
    import github
    from github import Github, GithubException, Auth
    from github.Repository import Repository as GithubRepository
    from github.Organization import Organization as GithubOrganization
    from github.NamedUser import NamedUser
    from github.AuthenticatedUser import AuthenticatedUser
    from github.WorkflowRun import WorkflowRun
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
            status: Optional[List[str]] = None,
            workflow_file: str = "",
            trigger: Optional[List[str]] = None,
            context: Optional["Directory"] = None,
        ) -> Iterable[Instantiation]:
            limit = limit or self.DEFAULT_PIPELINE_LIMIT
            project_path = self.extract_project_path(repo_info.url)
            gh_repo = self.github.get_repo(project_path)

            kwargs: Dict[str, Any] = {}
            if ref:
                kwargs["branch"] = ref  # tags too
            if commit:
                kwargs["head_sha"] = commit
            # GitHub's `status` filter takes a single value matched against the
            # run's status OR conclusion; let the API filter when one is given.
            if status and len(status) == 1:
                kwargs["status"] = status[0]
            # GitHub's `event` filter (pipeline trigger) takes a single value.
            if trigger and len(trigger) == 1:
                kwargs["event"] = trigger[0]

            runs = gh_repo.get_workflow_runs(**kwargs)

            status_set = set(status) if status else None
            trigger_set = set(trigger) if trigger else None
            count = 0
            for run in runs:
                if limit and count >= limit:
                    break

                # Client-side filter: skip runs from a different workflow file.
                if workflow_file and run.path != workflow_file:
                    continue

                # Client-side trigger filter for the multi-value case (a no-op
                # when the API already filtered with a single event).
                if trigger_set is not None and run.event not in trigger_set:
                    continue

                # Client-side filter for the multi-status case. GitHub matches a
                # status against either the run status or its conclusion, so do
                # the same here (a no-op when the API already filtered).
                if status_set is not None and not (
                    status_set & {run.status, run.conclusion}
                ):
                    continue

                properties = _github_run_properties(run)

                # The run payload embeds associated pull requests (only
                # for same-repo PRs); use the first as the discussion link.
                discussion_url = ""
                prs = getattr(run, "pull_requests", None) or []
                if prs:
                    base = run.html_url.split("/actions/")[0]
                    discussion_url = f"{base}/pull/{prs[0].number}"

                instantiation = Instantiation(
                    url=run.html_url,
                    type=TypeRefs({
                        EntitySchema.GitHubRun: TypeRefConstraint(
                            properties=cast(Dict[str, Any], properties),
                            status=map_ci_status(run.conclusion or run.status),
                        )
                    }),
                    source=repo_info.artifact_url(
                        run.path
                    ),  # e.g. ".github/workflows/ci.yml"
                    source_ref=run.head_branch,
                    source_revision=run.head_sha,
                    revision=run.head_sha,
                    metadata=CommonMetadata(
                        title=run.name or run.display_title,
                        description=f"{run.event}: {run.conclusion or run.status}",
                        # `created` records when the run finished (RFC 3339).
                        created=properties.get("finished_at", ""),
                        discussion_url=discussion_url,
                    ),
                )
                if context is not None:
                    self._run_pipeline_analyzer(
                        context, repo_info, instantiation, run, run.path
                    )
                count += 1
                yield instantiation
