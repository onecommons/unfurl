# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""GitLab repository host support for cloud maps.

Implements :py:class:`GitlabManager`, the :py:class:`~unfurl.cloudmap.RepositoryHost`
that syncs a cloud map with a GitLab instance via the ``python-gitlab`` API.
"""

from __future__ import annotations

import collections
from itertools import islice
import os
import re
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple, cast
from urllib.parse import urlparse

import gitlab
from gitlab.base import RESTObject
from gitlab.v4.objects import Group, Project

from ..logs import getLogger
from ..server.gui_variables.ufcloud_secrets import yield_ci_variables, set_ci_variables
from ..tosca_plugins.cloudmap_defs import (
    CommonMetadata,
    EntitySchema,
    HostConfig,
    Instantiation,
    PipelineArtifact,
    PipelineRunProperties,
    PipelineVariable,
    Repository,
    RepositoryMetadata,
    TypeRefConstraint,
    TypeRefs,
    get_repository_url,
)
from ..yamlloader import urlopen
from .host import RepositoryHost, map_ci_status

if TYPE_CHECKING:
    from . import Directory

logger = getLogger("unfurl")


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
        self,
        url: str,
        directory: Directory,
        download: bool,
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
        self,
        dest_proj: Project,
        directory: Directory,
        download: bool,
    ) -> Repository:
        r = self.gitlab_project_to_repository(dest_proj)
        previous = directory.context.get_repository(r)
        directory.context.add_record(r)
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
            r for r in directory.context.find_repositories() if self.has_repository(r)
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
                avatar = urlopen(repo_info.metadata.thumbnail_url).read()
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
            branches=self._branches(project),
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

    def _branches(self, project: Project) -> Dict[str, str]:
        """The project's branches, capped at ``MAX_GIT_REFS``.

        The cap takes whatever the API lists first -- alphabetical order for
        GitLab -- so on a project with enough branches it can drop the default
        one, which is the branch consumers actually need (`get_default_branch`,
        and the sync's `find_mismatched_repo` comparison). Fetch that one
        explicitly when the cap misses it.
        """
        branches = {
            b.name: b.commit["id"]
            for b in islice(
                project.branches.list(per_page=100, iterator=True),
                self.MAX_GIT_REFS,
            )
        }
        default = project.default_branch
        if default and default not in branches:
            branches[default] = project.branches.get(default).commit["id"]
        return branches

    def get_pipeline_runs(
        self,
        repo_info: "Repository",
        ref: str = "",
        commit: str = "",
        limit: int = 0,
        status: Optional[List[str]] = None,
        workflow_file: str = "",
        trigger: Optional[List[str]] = None,
        directory: Optional["Directory"] = None,
    ) -> Iterable[Instantiation]:
        limit = limit or self.DEFAULT_PIPELINE_LIMIT
        project_path = self.extract_project_path(repo_info.url)
        project = self.gitlab.projects.get(project_path)

        # GitLab projects have a single CI config file (defaults to
        # `.gitlab-ci.yml`); used both for the workflow_file filter and as the
        # `source` matched against PipelineRunAnalyzer subclasses.
        ci_path = getattr(project, "ci_config_path", "") or ".gitlab-ci.yml"
        if workflow_file and ci_path != workflow_file:
            # The requested workflow_file doesn't match; no matching runs.
            return

        kwargs: Dict[str, Any] = {}
        if ref:
            kwargs["ref"] = ref
        if commit:
            kwargs["sha"] = commit
        # GitLab's pipelines `status` filter takes a single value; let the API
        # filter when exactly one status is requested.
        if status and len(status) == 1:
            kwargs["status"] = status[0]
        # GitLab's `source` filter (pipeline trigger) takes a single value.
        if trigger and len(trigger) == 1:
            kwargs["source"] = trigger[0]

        pipelines = project.pipelines.list(
            order_by="id", sort="desc", iterator=True, **kwargs
        )

        count = 0
        for pipeline in pipelines:
            if limit and count >= limit:
                break
            # Client-side filter for the multi-status case (a no-op when the
            # API already filtered). The list item carries `.status`, so we
            # skip the extra full fetch for non-matching pipelines.
            if status and getattr(pipeline, "status", None) not in status:
                continue
            # Client-side trigger filter for the multi-value case. List items
            # carry `.source`, so we can avoid the full fetch here too.
            if (
                trigger
                and len(trigger) > 1
                and getattr(pipeline, "source", None) not in trigger
            ):
                continue
            full_pipeline = project.pipelines.get(pipeline.id)

            properties = _gitlab_pipeline_properties(
                project, full_pipeline, self.save_internal
            )

            # For merge-request pipelines the ref is
            # `refs/merge-requests/<iid>/head`; construct the MR URL from
            # the extracted iid and use it as the discussion link.
            discussion_url = ""
            mr_match = re.match(
                r"refs/merge-requests/(\d+)/head", full_pipeline.ref or ""
            )
            if mr_match:
                discussion_url = (
                    f"{project.web_url}/-/merge_requests/{mr_match.group(1)}"
                )
            instantiation = Instantiation(
                url=full_pipeline.web_url,
                type=TypeRefs({
                    EntitySchema.GitLabPipelineRun: TypeRefConstraint(
                        properties=cast(Dict[str, Any], properties),
                        status=map_ci_status(full_pipeline.status),
                    )
                }),
                source=repo_info.artifact_url(".gitlab-ci.yml"),
                source_ref=full_pipeline.ref,
                source_revision=full_pipeline.sha,
                revision=full_pipeline.sha,
                metadata=CommonMetadata(
                    title=f"Pipeline #{full_pipeline.id}",
                    description=f"{properties['trigger']}: {properties['status']}",
                    # `created` records when the run finished (RFC 3339).
                    created=properties.get("finished_at", ""),
                    discussion_url=discussion_url,
                ),
            )
            if directory is not None:
                self._run_pipeline_analyzer(
                    directory, repo_info, instantiation, full_pipeline, ci_path
                )
            count += 1
            yield instantiation


def _gitlab_pipeline_properties(
    project: Any, full_pipeline: Any, save_variables: bool = False
) -> PipelineRunProperties:
    """Extract properties from a GitLab pipeline for the CIRun type constraint.

    Pipeline variables are the externally-supplied inputs to a pipeline
    (manual-run form, trigger tokens, scheduled-pipeline variables, API
    ``variables``). Their values come back in cleartext and may contain
    secrets, and the cloudmap is committed to git, so they're only
    collected when ``save_variables`` is set (driven by the host's
    ``save_internal`` option).
    """
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

    # Fetch pipeline variables (GitLab only). These can hold secrets, so
    # only collect them when the host opts in via `save_internal`.
    if save_variables:
        try:
            raw_variables = full_pipeline.variables.list(get_all=True)
            variables = [
                PipelineVariable(key=v.key, value=v.value) for v in raw_variables
            ]
        except Exception:
            pass

    # GitLab pipeline `user` is a raw dict (name/username/...).
    user = getattr(full_pipeline, "user", None)
    actor = user.get("username", "") if isinstance(user, dict) else ""

    # pipeline variables — the variables passed when that specific pipeline was created (manual "Run pipeline"
    # form inputs, trigger-token variables, scheduled-pipeline variables, API variables).
    return PipelineRunProperties(
        id=full_pipeline.id,
        run_number=getattr(full_pipeline, "iid", 0) or 0,
        status=getattr(full_pipeline, "status", "") or "",
        log_url=full_pipeline.web_url,
        trigger=getattr(full_pipeline, "source", "") or "",
        actor=actor,
        artifacts=artifacts,
        artifacts_expire_at=artifacts_expire_at,
        variables=variables,
        # GitLab returns RFC 3339 strings for timestamps already.
        created_at=getattr(full_pipeline, "created_at", "") or "",
        started_at=getattr(full_pipeline, "started_at", "") or "",
        finished_at=getattr(full_pipeline, "finished_at", "") or "",
        committed_at=getattr(full_pipeline, "committed_at", "") or "",
    )
