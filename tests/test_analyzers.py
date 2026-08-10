import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from unfurl.cloudmap import (
    CloudMap,
    GithubManager,
    GitlabManager,
    RepositoryHost,
    AnalyzerRegistry,
)
from unfurl.cloudmap.provenance import (
    ProvenanceTrackingContext,
    discovery_sources,
    record_discovery_source,
)
from unfurl.localenv import LocalEnv
from unfurl.util import API_VERSION
from unfurl.cloudmap.analyzers import (
    GitHubWorkflowAnalyzer,
    GitLabPipelineAnalyzer,
    UnfurlAnalyzer,
    Analyzers,
)
from unfurl.tosca_plugins.cloudmap_defs import (
    Artifact,
    ArtifactMetadata,
    CloudType,
    CommonMetadata,
    Discovery,
    EntitySchema,
    Instantiation,
    PipelineRunAnalyzer,
    Repository,
    RepositoryAnalyzer,
    Service,
    TypeRefs,
    URLAnalyzer,
)
from tosca import global_state
UNFURL_TEST_UNFURL_GUI_TOKEN_URL = os.getenv("UNFURL_TEST_UNFURL_GUI_TOKEN_URL")
UNFURL_TEST_GITHUB_KEY = os.getenv("UNFURL_TEST_GITHUB_KEY")

skip_gitlab_integration = pytest.mark.skipif(
    not UNFURL_TEST_UNFURL_GUI_TOKEN_URL,
    reason="need UNFURL_TEST_UNFURL_GUI_TOKEN_URL set for GitLab integration tests",
)

skip_github_integration = pytest.mark.skipif(
    not UNFURL_TEST_GITHUB_KEY and not os.getenv("CI"),
    reason="need UNFURL_TEST_GITHUB_KEY set for GitHub integration tests",
)


class TestCIAnalyzers:
    """Test CI notable detection."""

    def test_gitlab_pipeline_notable_match(self, tmp_path):
        """Create a temp repo with .gitlab-ci.yml, verify GitLabPipelineAnalyzer is found."""
        (tmp_path / ".gitlab-ci.yml").write_text("stages:\n  - build\n")
        analyzer = AnalyzerRegistry(list(Analyzers))
        analyzers = analyzer.analyze_local(str(tmp_path), str(tmp_path))
        ci_analyzers = [n for n in analyzers if isinstance(n, GitLabPipelineAnalyzer)]
        assert len(ci_analyzers) == 1
        assert ci_analyzers[0].artifact_type == EntitySchema.GitLabPipeline

    def test_github_workflow_notable_match(self, tmp_path):
        """Create a temp repo with .github/workflows/ci.yml, verify GitHubWorkflowAnalyzer is found."""
        workflows_dir = tmp_path / ".github" / "workflows"
        workflows_dir.mkdir(parents=True)
        (workflows_dir / "ci.yml").write_text("name: CI\non: push\n")
        analyzer = AnalyzerRegistry(list(Analyzers))
        analyzers = analyzer.analyze_local(str(tmp_path), str(tmp_path))
        gh_analyzers = [n for n in analyzers if isinstance(n, GitHubWorkflowAnalyzer)]
        assert len(gh_analyzers) == 1
        assert gh_analyzers[0].artifact_type == EntitySchema.GitHubWorkflow

    def test_github_workflow_notable_no_workflows_dir(self, tmp_path):
        """If .github exists but no workflows/ subdir, analyze adds nothing."""
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "CODEOWNERS").write_text("* @owner\n")
        analyzer = AnalyzerRegistry(list(Analyzers))
        analyzers = analyzer.analyze_local(str(tmp_path), str(tmp_path))
        gh_analyzers = [n for n in analyzers if isinstance(n, GitHubWorkflowAnalyzer)]
        # The notable is created but analyze() should return None and record
        # no `contains` entries (no directory-level entry).
        assert len(gh_analyzers) == 1
        repo_info = Repository(url="git://example.com/repo.git", path="repo", name="repo")
        artifact = gh_analyzers[0].analyze(MagicMock(), repo_info, str(tmp_path))
        assert artifact is None
        assert gh_analyzers[0].contains == {}

    def test_no_ci_notable(self, tmp_path):
        """Repo without CI files returns no CI analyzers."""
        (tmp_path / "README.md").write_text("# Hello\n")
        analyzer = AnalyzerRegistry(list(Analyzers))
        analyzers = analyzer.analyze_local(str(tmp_path), str(tmp_path))
        ci_analyzers = [
            n
            for n in analyzers
            if isinstance(n, (GitLabPipelineAnalyzer, GitHubWorkflowAnalyzer))
        ]
        assert len(ci_analyzers) == 0

    def test_github_workflow_notable_analyze(self, tmp_path):
        """analyze() emits a separate artifact + `contains` entry per workflow file."""
        workflows_dir = tmp_path / ".github" / "workflows"
        workflows_dir.mkdir(parents=True)
        (workflows_dir / "ci.yml").write_text("name: CI\n")
        (workflows_dir / "release.yaml").write_text("name: Release\n")
        # non-yaml files and subdirectories are ignored
        (workflows_dir / "README.md").write_text("# workflows\n")
        (workflows_dir / "scripts").mkdir()
        repo_info = Repository(
            url="git://github.com/owner/repo.git", path="owner/repo", name="repo"
        )
        directory = MagicMock()
        # both walkers construct folder matches with the matched dir's path
        notable = GitHubWorkflowAnalyzer(".github", "")
        # artifacts are added via directory.add_record(), so analyze() returns None
        artifact = notable.analyze(directory, repo_info, str(tmp_path))
        assert artifact is None

        # one `contains` entry per workflow file, each typed as a GitHubWorkflow
        assert set(notable.contains) == {
            ".github/workflows/ci.yml",
            ".github/workflows/release.yaml",
        }
        for type_refs in notable.contains.values():
            assert EntitySchema.GitHubWorkflow in type_refs.types

        # a separate artifact was recorded for each workflow file
        recorded = [c.args[0] for c in directory.add_record.call_args_list]
        recorded_urls = {a.url for a in recorded}
        assert len(recorded) == 2
        assert any(u.endswith(".github/workflows/ci.yml") for u in recorded_urls)
        assert any(u.endswith(".github/workflows/release.yaml") for u in recorded_urls)
        for a in recorded:
            assert EntitySchema.GitHubWorkflow in a.type.types

    def test_github_workflow_analyze_nested_folder(self, tmp_path):
        """A nested match: folder is the matched .github dir's full path."""
        workflows_dir = tmp_path / "subdir" / ".github" / "workflows"
        workflows_dir.mkdir(parents=True)
        (workflows_dir / "ci.yml").write_text("name: CI\n")
        repo_info = Repository(
            url="git://github.com/owner/repo.git", path="owner/repo", name="repo"
        )
        # folder == "subdir/.github" mimics the walker's init(<matched dir path>, ...)
        notable = GitHubWorkflowAnalyzer(os.path.join("subdir", ".github"), "")
        notable.analyze(MagicMock(), repo_info, str(tmp_path))
        assert set(notable.contains) == {"subdir/.github/workflows/ci.yml"}

    def test_github_workflow_multiple_entries_in_contains(self, tmp_path):
        """add_notables records every per-file entry an analyzer contributes."""
        workflows_dir = tmp_path / ".github" / "workflows"
        workflows_dir.mkdir(parents=True)
        (workflows_dir / "ci.yml").write_text("name: CI\n")
        (workflows_dir / "release.yaml").write_text("name: Release\n")
        repo_info = Repository(
            url="git://github.com/owner/repo.git", path="owner/repo", name="repo"
        )
        notable = GitHubWorkflowAnalyzer(".github", "")
        notable.analyze(MagicMock(), repo_info, str(tmp_path))
        repo_info.add_notables([notable])
        assert set(repo_info.contains) == {
            ("", ".github/workflows/ci.yml"),
            ("", ".github/workflows/release.yaml"),
        }


class TestEnsembleInstantiation:
    """UnfurlAnalyzer._create_ensemble_instantiation_and_service() pulls the
    ensemble's last-job status/summary/time into the Instantiation."""

    def _make_manifest(self, last_job):
        manifest = MagicMock()
        manifest.repositories.get.return_value = None  # no "spec" repo
        manifest.lastJob = last_job
        manifest.uri = "https://example.com/ensemble"
        return manifest

    def _run(self, last_job):
        analyzer = UnfurlAnalyzer(".", "ensemble.yaml")
        manifest = self._make_manifest(last_job)
        repo_info = MagicMock()
        repo_info.get_current_commit.return_value = "abc123"
        directory = MagicMock()
        directory.do_analysis = False
        directory.get_artifact.return_value = None
        artifact = MagicMock()
        artifact.url = "git://example.com/repo.git#:ensemble.yaml"
        artifact.references = {}
        with patch(
            "unfurl.cloudmap.analyzers.get_deployment_url", return_value=None
        ):
            analyzer._create_ensemble_instantiation_and_service(
                manifest, repo_info, directory, "test.Type", artifact
            )
        # the Instantiation is the record added that isn't the Service
        added = [c.args[0] for c in directory.add_record.call_args_list]
        insts = [r for r in added if isinstance(r, Instantiation)]
        assert len(insts) == 1
        return insts[0]

    @staticmethod
    def _status(inst):
        # deployment status lives in the Ensemble type-ref constraint
        constraint = inst.type.types[EntitySchema.Ensemble]
        return constraint.get("status") if constraint else None

    def test_lastjob_sets_status_and_metadata(self):
        last_job = {
            "changeId": 1,
            "startTime": "2026-04-01-12-00-00-000000",
            "endTime": "2026-04-01-12-30-00-000000",
            "workflow": "deploy",
            "summary": "2 instances deployed",
            "readyState": {"effective": "ok", "local": "ok"},
        }
        inst = self._run(last_job)
        assert self._status(inst) == "present"
        assert inst.metadata.created == "2026-04-01 12:30:00+00:00"
        assert inst.metadata.description == "deploy: 2 instances deployed"

    def test_lastjob_error_status_is_failed(self):
        last_job = {
            "changeId": 2,
            "startTime": "2026-04-01-12-00-00-000000",
            "endTime": "2026-04-01-12-30-00-000000",
            "workflow": "deploy",
            "summary": "1 failed",
            "readyState": {"effective": "error", "local": "error"},
        }
        inst = self._run(last_job)
        assert self._status(inst) == "failed"

    def test_no_lastjob_leaves_status_unset(self):
        inst = self._run(None)
        assert self._status(inst) is None
        assert inst.metadata.created == ""

    def test_malformed_lastjob_does_not_raise(self):
        # missing startTime/endTime, bad status name, bad time format — all
        # tolerated by _add_lastjob without raising.
        last_job = {
            "workflow": "deploy",
            "summary": "partial",
            "readyState": {"effective": "bogus-status"},
            "endTime": "not-a-timestamp",
        }
        inst = self._run(last_job)
        # status couldn't be mapped, time couldn't be parsed -> left unset,
        # but the readable fields still come through.
        assert self._status(inst) is None
        assert inst.metadata.created == ""
        assert inst.metadata.description == "deploy: partial"


class TestGenericRepositoryAnalyzerFallback:
    """Generic ``RepositoryAnalyzer`` subclasses (no ``files``/``folders``
    declared) are consulted as fallbacks: for every path the walker visits
    where no name-specific class matched, ``AnalyzerRegistry`` calls
    ``init()`` on each generic class in registration order and uses the
    first instance returned.
    """

    @staticmethod
    def _make_generic_class():
        """A generic Analyzer that accepts ``*.toml`` files and reports them."""
        from unfurl.tosca_plugins.cloudmap_defs import RepositoryAnalyzer

        class TomlAnalyzer(RepositoryAnalyzer):
            files = ()  # generic — no name-keyed registration
            folders = ()
            artifact_type = EntitySchema.GenericFile
            init_calls: list = []

            @classmethod
            def init(cls, folder, file, digest=""):
                cls.init_calls.append((folder, file, digest))
                # Accept .toml files only; decline everything else.
                if file.endswith(".toml"):
                    return cls(folder, file, digest)
                return None

        return TomlAnalyzer

    def test_register_classifies_as_generic(self):
        """Classes with empty ``files``/``folders`` go onto
        ``AnalyzerRegistry.generic`` instead of the file/folder maps."""
        TomlAnalyzer = self._make_generic_class()
        analyzer = AnalyzerRegistry([TomlAnalyzer])
        assert analyzer.generic == [TomlAnalyzer]
        assert analyzer.files == {}
        assert analyzer.folders == {}

    def test_analyze_local_falls_back_to_generic(self, tmp_path):
        """``analyze_local`` consults the generic class for unmatched files
        and uses whatever ``init()`` returns."""
        TomlAnalyzer = self._make_generic_class()
        (tmp_path / "pyproject.toml").write_text("[tool.x]\n")
        (tmp_path / "README.md").write_text("# hi\n")

        analyzer = AnalyzerRegistry(list(Analyzers) + [TomlAnalyzer])
        analyzers = analyzer.analyze_local(str(tmp_path), str(tmp_path))

        # The generic class accepted pyproject.toml and produced an instance.
        toml_hits = [n for n in analyzers if isinstance(n, TomlAnalyzer)]
        assert len(toml_hits) == 1
        assert toml_hits[0].file == "pyproject.toml"

        # init() was consulted for the unmatched file.
        seen_files = {filename for (_dir, filename, _digest) in TomlAnalyzer.init_calls}
        assert "pyproject.toml" in seen_files

    def test_analyze_path_falls_back_to_generic(self, tmp_path):
        """``analyze_path`` for a single file with no name match consults
        the generic chain."""
        TomlAnalyzer = self._make_generic_class()
        (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")

        analyzer = AnalyzerRegistry(list(Analyzers) + [TomlAnalyzer])
        result = analyzer.analyze_path("Cargo.toml", str(tmp_path))

        assert len(result) == 1
        assert isinstance(result[0], TomlAnalyzer)
        assert result[0].file == "Cargo.toml"

    def test_specific_match_wins_over_generic(self, tmp_path):
        """Name-specific classes are tried first; the generic fallback only
        runs if nothing claimed the file."""
        from unfurl.tosca_plugins.cloudmap_defs import RepositoryAnalyzer

        class NoOpGeneric(RepositoryAnalyzer):
            files = ()
            folders = ()
            init_calls: list = []

            @classmethod
            def init(cls, folder, file, digest=""):
                cls.init_calls.append(file)
                return cls(folder, file, digest)

        # .gitlab-ci.yml is claimed by the built-in GitLabPipelineAnalyzer.
        (tmp_path / ".gitlab-ci.yml").write_text("stages: []\n")

        analyzer = AnalyzerRegistry(list(Analyzers) + [NoOpGeneric])
        analyzers = analyzer.analyze_local(str(tmp_path), str(tmp_path))

        # The generic was never consulted for the matched file.
        assert ".gitlab-ci.yml" not in NoOpGeneric.init_calls
        # And exactly one notable was produced — the specific GitLab one.
        gitlab_hits = [n for n in analyzers if isinstance(n, GitLabPipelineAnalyzer)]
        assert len(gitlab_hits) == 1


class TestPipelineRunsMocked:
    """Mock-based tests for get_pipeline_runs."""

    def test_base_class_returns_empty(self):
        host = RepositoryHost.__new__(RepositoryHost)
        repo_info = Repository(url="git://example.com/repo.git", path="repo", name="repo")
        result = list(host.get_pipeline_runs(repo_info))
        assert result == []

    def test_gitlab_get_pipeline_runs(self):
        manager = GitlabManager.__new__(GitlabManager)
        manager.gitlab = MagicMock()
        manager.hostname = "gitlab.example.com"
        manager.canonical_url = ""
        # Pipeline variables are only collected with save_internal set.
        manager.save_internal = True

        mock_pipeline = MagicMock()
        mock_pipeline.id = 42
        mock_project = MagicMock()
        mock_project.pipelines.list.return_value = [mock_pipeline]

        full_pipeline = MagicMock()
        full_pipeline.id = 42
        full_pipeline.iid = 7
        full_pipeline.web_url = "https://gitlab.example.com/project/-/pipelines/42"
        full_pipeline.sha = "abc123"
        full_pipeline.status = "success"
        full_pipeline.source = "merge_request_event"
        full_pipeline.user = {"username": "octocat", "name": "The Octocat"}
        # Merge-request pipeline ref → discussion_url is derived from the iid.
        full_pipeline.ref = "refs/merge-requests/7/head"
        # GitLab returns RFC 3339 strings for timestamps.
        full_pipeline.created_at = "2026-04-01T01:00:00Z"
        full_pipeline.started_at = "2026-04-01T01:30:00Z"
        full_pipeline.finished_at = "2026-04-01T02:03:04Z"
        full_pipeline.committed_at = "2026-04-01T00:55:00Z"

        # Mock job with artifacts
        mock_job = MagicMock()
        mock_job.name = "build"
        mock_job.id = 100
        mock_job.artifacts = [{"filename": "build.zip", "size": 1024}]
        mock_job.artifacts_expire_at = "2026-04-01T00:00:00Z"
        full_pipeline.jobs.list.return_value = [mock_job]

        # Mock pipeline variables (GitLab only)
        mock_var = MagicMock()
        mock_var.key = "CI_DEBUG"
        mock_var.value = "true"
        full_pipeline.variables.list.return_value = [mock_var]

        mock_project.pipelines.get.return_value = full_pipeline
        mock_project.web_url = "https://gitlab.example.com/owner/repo"

        manager.gitlab.projects.get.return_value = mock_project

        repo_info = Repository(
            url="git://gitlab.example.com/owner/repo.git",
            path="owner/repo",
            name="repo",
        )

        results = list(manager.get_pipeline_runs(repo_info, ref="main"))
        assert len(results) == 1
        inst = results[0]
        assert inst.url == "https://gitlab.example.com/project/-/pipelines/42"
        assert EntitySchema.GitLabPipelineRun in inst.type.types
        assert inst.source_revision == "abc123"
        assert inst.metadata.title == "Pipeline #42"
        # The finished time is saved in the instantiation metadata.
        assert inst.metadata.created == "2026-04-01T02:03:04Z"
        # The merge-request URL (from the ref iid) is the discussion link.
        assert (
            inst.metadata.discussion_url
            == "https://gitlab.example.com/owner/repo/-/merge_requests/7"
        )

        # Verify properties in type constraint
        constraint = inst.type.types[EntitySchema.GitLabPipelineRun]
        assert constraint is not None
        # the CI status is mapped onto the type-ref constraint
        assert constraint["status"] == "present"
        props = constraint["properties"]
        assert props["id"] == 42
        assert props["run_number"] == 7
        assert props["status"] == "success"
        assert props["trigger"] == "merge_request_event"
        assert props["actor"] == "octocat"
        assert props["created_at"] == "2026-04-01T01:00:00Z"
        assert props["started_at"] == "2026-04-01T01:30:00Z"
        assert props["committed_at"] == "2026-04-01T00:55:00Z"
        assert props["log_url"] == "https://gitlab.example.com/project/-/pipelines/42"
        assert len(props["artifacts"]) == 1
        assert props["artifacts"][0]["name"] == "build/build.zip"
        assert props["artifacts"][0]["size"] == 1024
        assert props["artifacts_expire_at"] == "2026-04-01T00:00:00Z"
        assert len(props["variables"]) == 1
        assert props["variables"][0] == {"key": "CI_DEBUG", "value": "true"}
        assert props["finished_at"] == "2026-04-01T02:03:04Z"

        # With save_internal off, pipeline variables (which may hold
        # secrets) are not collected.
        manager.save_internal = False
        inst_no_vars = list(manager.get_pipeline_runs(repo_info, ref="main"))[0]
        props_no_vars = inst_no_vars.type.types[EntitySchema.GitLabPipelineRun][
            "properties"
        ]
        assert props_no_vars["variables"] == []

    @pytest.mark.skipif(GithubManager is None, reason="PyGithub not installed")
    def test_github_get_pipeline_runs(self):
        manager = GithubManager.__new__(GithubManager)
        manager.github = MagicMock()
        manager.hostname = "github.com"
        manager.canonical_url = ""

        mock_run = MagicMock()
        mock_run.html_url = "https://github.com/owner/repo/actions/runs/123"
        mock_run.id = 123
        mock_run.run_number = 5
        mock_run.head_sha = "def456"
        mock_run.head_branch = "main"
        mock_run.conclusion = "success"
        mock_run.status = "completed"
        mock_run.name = "CI"
        mock_run.display_title = "CI"
        mock_run.event = "pull_request"
        mock_run.path = ".github/workflows/ci.yml"
        mock_run.logs_url = "https://api.github.com/repos/owner/repo/actions/runs/123/logs"
        mock_run.actor = MagicMock(login="octocat")
        # PyGithub returns datetime objects for run timestamps.
        mock_run.created_at = datetime(2026, 4, 1, 1, 0, 0, tzinfo=timezone.utc)
        mock_run.run_started_at = datetime(2026, 4, 1, 1, 30, 0, tzinfo=timezone.utc)
        mock_run.updated_at = datetime(2026, 4, 1, 2, 3, 4, tzinfo=timezone.utc)
        # head_commit timestamp lives in the run's raw payload, not a typed attr.
        mock_run.raw_data = {"head_commit": {"timestamp": "2026-04-01T00:55:00Z"}}
        # An associated PR → discussion_url.
        mock_run.pull_requests = [MagicMock(number=42)]

        # Mock artifacts
        mock_artifact = MagicMock()
        mock_artifact.name = "dist"
        mock_artifact.archive_download_url = "https://api.github.com/repos/owner/repo/actions/artifacts/456/zip"
        mock_artifact.size_in_bytes = 2048
        mock_artifact.expires_at = "2026-04-15T00:00:00Z"
        mock_run.get_artifacts.return_value = [mock_artifact]

        mock_gh_repo = MagicMock()
        mock_gh_repo.get_workflow_runs.return_value = [mock_run]
        manager.github.get_repo.return_value = mock_gh_repo

        repo_info = Repository(
            url="git://github.com/owner/repo.git",
            path="owner/repo",
            name="repo",
        )

        results = list(manager.get_pipeline_runs(repo_info, ref="main"))
        assert len(results) == 1
        inst = results[0]
        assert inst.url == "https://github.com/owner/repo/actions/runs/123"
        assert EntitySchema.GitHubRun in inst.type.types
        assert inst.source_revision == "def456"
        assert inst.metadata.title == "CI"
        assert ".github/workflows/ci.yml" in inst.source
        # The finished time (datetime → RFC 3339 string) is saved in metadata.
        assert inst.metadata.created == "2026-04-01T02:03:04+00:00"
        # The associated PR URL (from the embedded pull_requests) is the link.
        assert inst.metadata.discussion_url == "https://github.com/owner/repo/pull/42"

        # Verify properties in type constraint
        constraint = inst.type.types[EntitySchema.GitHubRun]
        assert constraint is not None
        # the CI status is mapped onto the type-ref constraint
        assert constraint["status"] == "present"
        props = constraint["properties"]
        assert props["id"] == 123
        assert props["run_number"] == 5
        # `conclusion` ("success") wins over `status` ("completed")
        assert props["status"] == "success"
        assert props["trigger"] == "pull_request"
        assert props["actor"] == "octocat"
        assert props["created_at"] == "2026-04-01T01:00:00+00:00"
        assert props["started_at"] == "2026-04-01T01:30:00+00:00"
        assert props["committed_at"] == "2026-04-01T00:55:00Z"
        assert props["log_url"] == "https://api.github.com/repos/owner/repo/actions/runs/123/logs"
        assert len(props["artifacts"]) == 1
        assert props["artifacts"][0]["name"] == "dist"
        assert props["artifacts"][0]["size"] == 2048
        assert props["artifacts"][0]["expires_at"] == "2026-04-15T00:00:00Z"
        assert props["artifacts_expire_at"] == "2026-04-15T00:00:00Z"
        assert props["finished_at"] == "2026-04-01T02:03:04+00:00"
        # GitHub does not include variables
        assert "variables" not in props

    def test_gitlab_get_pipeline_runs_with_limit(self):
        manager = GitlabManager.__new__(GitlabManager)
        manager.gitlab = MagicMock()
        manager.hostname = "gitlab.example.com"
        manager.canonical_url = ""
        manager.save_internal = False

        pipelines = []
        for i in range(5):
            p = MagicMock()
            p.id = i
            pipelines.append(p)

        mock_project = MagicMock()
        mock_project.pipelines.list.return_value = pipelines

        def make_full_pipeline(pid):
            fp = MagicMock()
            fp.id = pid
            fp.iid = pid
            fp.web_url = f"https://gitlab.example.com/project/-/pipelines/{pid}"
            fp.sha = f"sha{pid}"
            fp.status = "success"
            fp.source = "push"
            fp.user = {"username": "octocat"}
            fp.ref = "main"
            fp.created_at = ""
            fp.started_at = ""
            fp.finished_at = f"2026-04-0{pid + 1}T00:00:00Z"
            fp.committed_at = ""
            fp.jobs.list.return_value = []
            fp.variables.list.return_value = []
            return fp

        mock_project.pipelines.get.side_effect = make_full_pipeline
        mock_project.web_url = "https://gitlab.example.com/owner/repo"
        manager.gitlab.projects.get.return_value = mock_project

        repo_info = Repository(
            url="git://gitlab.example.com/owner/repo.git",
            path="owner/repo",
            name="repo",
        )

        results = list(manager.get_pipeline_runs(repo_info, limit=2))
        assert len(results) == 2

    def test_gitlab_get_pipeline_runs_status_filter(self):
        manager = GitlabManager.__new__(GitlabManager)
        manager.gitlab = MagicMock()
        manager.hostname = "gitlab.example.com"
        manager.canonical_url = ""
        manager.save_internal = False

        statuses = ["success", "failed", "running"]
        list_items = []
        for i, st in enumerate(statuses):
            p = MagicMock()
            p.id = i
            p.status = st  # the list item carries the status
            list_items.append(p)

        mock_project = MagicMock()
        # the mocked API returns everything; filtering is what we're testing
        mock_project.pipelines.list.return_value = list_items

        def make_full_pipeline(pid):
            fp = MagicMock()
            fp.id = pid
            fp.iid = pid
            fp.web_url = f"https://gitlab.example.com/project/-/pipelines/{pid}"
            fp.sha = f"sha{pid}"
            fp.status = statuses[pid]
            fp.source = "push"
            fp.user = {"username": "octocat"}
            fp.ref = "main"
            fp.created_at = fp.started_at = fp.finished_at = fp.committed_at = ""
            fp.jobs.list.return_value = []
            fp.variables.list.return_value = []
            return fp

        mock_project.pipelines.get.side_effect = make_full_pipeline
        mock_project.web_url = "https://gitlab.example.com/owner/repo"
        manager.gitlab.projects.get.return_value = mock_project
        repo_info = Repository(
            url="git://gitlab.example.com/owner/repo.git",
            path="owner/repo",
            name="repo",
        )

        # single status -> forwarded to the API and applied client-side
        results = list(manager.get_pipeline_runs(repo_info, status=["failed"]))
        assert mock_project.pipelines.list.call_args.kwargs.get("status") == "failed"
        assert len(results) == 1
        props = results[0].type.types[EntitySchema.GitLabPipelineRun]["properties"]
        assert props["status"] == "failed"

        # multiple statuses -> no API status kwarg, client-side filter applies
        mock_project.pipelines.list.reset_mock()
        results = list(
            manager.get_pipeline_runs(repo_info, status=["success", "running"])
        )
        assert "status" not in mock_project.pipelines.list.call_args.kwargs
        got = {
            r.type.types[EntitySchema.GitLabPipelineRun]["properties"]["status"]
            for r in results
        }
        assert got == {"success", "running"}

    @pytest.mark.skipif(GithubManager is None, reason="PyGithub not installed")
    def test_github_get_pipeline_runs_status_filter(self):
        manager = GithubManager.__new__(GithubManager)
        manager.github = MagicMock()
        manager.hostname = "github.com"
        manager.canonical_url = ""

        def make_run(rid, status, conclusion):
            run = MagicMock()
            run.html_url = f"https://github.com/owner/repo/actions/runs/{rid}"
            run.id = rid
            run.run_number = rid
            run.head_sha = f"sha{rid}"
            run.head_branch = "main"
            run.status = status
            run.conclusion = conclusion
            run.name = "CI"
            run.display_title = "CI"
            run.event = "push"
            run.path = ".github/workflows/ci.yml"
            run.logs_url = f"https://api.github.com/repos/owner/repo/actions/runs/{rid}/logs"
            run.actor = MagicMock(login="octocat")
            run.created_at = None
            run.run_started_at = None
            run.updated_at = None
            run.raw_data = {}
            run.pull_requests = []
            run.get_artifacts.return_value = []
            return run

        all_runs = [
            make_run(1, "completed", "success"),
            make_run(2, "completed", "failure"),
            make_run(3, "in_progress", None),
        ]
        mock_gh_repo = MagicMock()
        mock_gh_repo.get_workflow_runs.return_value = all_runs
        manager.github.get_repo.return_value = mock_gh_repo
        repo_info = Repository(
            url="git://github.com/owner/repo.git", path="owner/repo", name="repo"
        )

        # single status (a conclusion) -> forwarded to the API and filtered
        results = list(manager.get_pipeline_runs(repo_info, status=["failure"]))
        assert mock_gh_repo.get_workflow_runs.call_args.kwargs.get("status") == "failure"
        got = {
            r.type.types[EntitySchema.GitHubRun]["properties"]["status"]
            for r in results
        }
        assert got == {"failure"}

        # multiple statuses -> no API status kwarg; matches run status OR conclusion
        mock_gh_repo.get_workflow_runs.reset_mock()
        results = list(
            manager.get_pipeline_runs(repo_info, status=["success", "in_progress"])
        )
        assert "status" not in mock_gh_repo.get_workflow_runs.call_args.kwargs
        got = {
            r.type.types[EntitySchema.GitHubRun]["properties"]["status"]
            for r in results
        }
        # run 1 matches by conclusion "success"; run 3 matches by status "in_progress"
        assert got == {"success", "in_progress"}


class TestPipelineRunAnalyzer:
    """Test PipelineRunAnalyzer matching and invocation from get_pipeline_runs."""

    @staticmethod
    def _make_analyzer():
        """A PipelineRunAnalyzer matching owner/repo's foo.yaml workflow that
        records each invocation and marks the instantiation."""
        calls: list = []

        class FooPipelineAnalyzer(PipelineRunAnalyzer):
            repositories = ("git://github.com/owner/repo.git",)
            sources = (".github/workflows/foo.yaml",)

            def analyze_pipeline_run(
                self, context, repo_info, instantiation, obj, root_path
            ):
                calls.append((obj, root_path, instantiation))
                instantiation.metadata.notes = "seen"

        return FooPipelineAnalyzer, calls

    def test_find_pipeline_analyzers_matching(self):
        cls, _calls = self._make_analyzer()
        cm = CloudMap.__new__(CloudMap)
        cm.custom_analyzers = [cls]
        # no Repository records in the cloudmap -> origin chain isn't extended
        cm.directory = MagicMock()
        cm.directory.get_repository.return_value = None

        def repo(url, **kw):
            return Repository(url=url, path="owner/repo", name="repo", **kw)

        # exact source + normalized repo url (note: missing .git is normalized)
        assert (
            cm.find_pipeline_analyzers(
                repo("git://github.com/owner/repo"), ".github/workflows/foo.yaml"
            )
            is cls
        )
        # non-matching source -> None
        assert (
            cm.find_pipeline_analyzers(
                repo("git://github.com/owner/repo.git"), ".github/workflows/bar.yaml"
            )
            is None
        )
        # non-matching repository -> None
        assert (
            cm.find_pipeline_analyzers(
                repo("git://github.com/other/repo.git"), ".github/workflows/foo.yaml"
            )
            is None
        )
        # a fork whose fork_of points at the analyzer's repository -> matches
        assert (
            cm.find_pipeline_analyzers(
                repo(
                    "git://github.com/fork/repo.git",
                    fork_of="git://github.com/owner/repo.git",
                ),
                ".github/workflows/foo.yaml",
            )
            is cls
        )
        # a mirror whose mirror_of points at the analyzer's repository -> matches
        assert (
            cm.find_pipeline_analyzers(
                repo(
                    "git://github.com/mirror/repo.git",
                    mirror_of="git://github.com/owner/repo.git",
                ),
                ".github/workflows/foo.yaml",
            )
            is cls
        )

    def test_find_pipeline_analyzers_follows_chain(self):
        """fork_of/mirror_of are followed transitively through Repository
        records that exist in the cloudmap."""
        cls, _calls = self._make_analyzer()  # keyed on owner/repo
        cm = CloudMap.__new__(CloudMap)
        cm.custom_analyzers = [cls]

        # leaf forks mid; mid (a cloudmap record) in turn forks owner/repo
        # (the analyzer's repository). Only mid has a Repository record.
        mid = Repository(
            url="git://github.com/mid/repo.git",
            path="mid/repo",
            name="repo",
            fork_of="git://github.com/owner/repo.git",
        )
        records = {"git://github.com/mid/repo.git": mid}
        cm.directory = MagicMock()
        cm.directory.get_repository.side_effect = lambda url: records.get(url)

        leaf = Repository(
            url="git://github.com/leaf/repo.git",
            path="leaf/repo",
            name="repo",
            fork_of="git://github.com/mid/repo.git",
        )
        # leaf -> mid (record) -> owner/repo matches the analyzer
        assert cm.find_pipeline_analyzers(leaf, ".github/workflows/foo.yaml") is cls

        # a leaf whose chain never reaches the analyzer's repository -> None
        records["git://github.com/mid/repo.git"] = Repository(
            url="git://github.com/mid/repo.git",
            path="mid/repo",
            name="repo",
            fork_of="git://github.com/elsewhere/repo.git",
        )
        assert cm.find_pipeline_analyzers(leaf, ".github/workflows/foo.yaml") is None

    def test_find_pipeline_analyzers_chain_cycle(self):
        """A fork_of/mirror_of cycle through records terminates."""
        cls, _calls = self._make_analyzer()
        cm = CloudMap.__new__(CloudMap)
        cm.custom_analyzers = [cls]

        a = Repository(
            url="git://github.com/a/repo.git",
            path="a/repo",
            name="repo",
            fork_of="git://github.com/b/repo.git",
        )
        b = Repository(
            url="git://github.com/b/repo.git",
            path="b/repo",
            name="repo",
            fork_of="git://github.com/a/repo.git",  # back-edge -> cycle
        )
        records = {
            "git://github.com/a/repo.git": a,
            "git://github.com/b/repo.git": b,
        }
        cm.directory = MagicMock()
        cm.directory.get_repository.side_effect = lambda url: records.get(url)

        # neither a nor b derive from owner/repo -> None, and no infinite loop
        assert cm.find_pipeline_analyzers(a, ".github/workflows/foo.yaml") is None

    def test_find_pipeline_analyzers_source_types(self):
        """source_types matches against the source file's type refs found in
        the repository's `contains` map or on its Artifact record."""

        class TypedAnalyzer(PipelineRunAnalyzer):
            # wildcard repositories/sources; only the source type gates
            source_types = (
                TypeRefs({EntitySchema.GitHubWorkflow: None}),
            )

        cm = CloudMap.__new__(CloudMap)
        cm.custom_analyzers = [TypedAnalyzer]
        cm.directory = MagicMock()
        cm.directory.get_repository.return_value = None
        cm.directory.get_artifact.return_value = None
        cm.directory.get_type.return_value = None

        src = ".github/workflows/foo.yaml"

        # 1. type found in `contains` (with an extra type present too) -> match
        repo = Repository(
            url="git://github.com/owner/repo.git",
            path="owner/repo",
            name="repo",
            contains={
                src: TypeRefs(
                    {EntitySchema.GitHubWorkflow: None, EntitySchema.GenericFile: None}
                )
            },
        )
        assert cm.find_pipeline_analyzers(repo, src) is TypedAnalyzer

        # 2. `contains` has the source but not the wanted type -> None
        repo_other = Repository(
            url="git://github.com/owner/repo.git",
            path="owner/repo",
            name="repo",
            contains={src: TypeRefs({EntitySchema.GenericFile: None})},
        )
        assert cm.find_pipeline_analyzers(repo_other, src) is None

        # 3. source absent from `contains` -> fall back to the Artifact record
        repo_bare = Repository(
            url="git://github.com/owner/repo.git", path="owner/repo", name="repo"
        )
        cm.directory.get_artifact.return_value = Artifact(
            url=repo_bare.artifact_url(src),
            type=TypeRefs({EntitySchema.GitHubWorkflow: None}),
        )
        assert cm.find_pipeline_analyzers(repo_bare, src) is TypedAnalyzer

        # 4. no `contains` entry and no Artifact record -> None
        cm.directory.get_artifact.return_value = None
        assert cm.find_pipeline_analyzers(repo_bare, src) is None

    def test_find_pipeline_analyzers_source_types_all_required(self):
        """All type names in a source_types entry must be present in the
        source's type refs."""

        class TwoTypeAnalyzer(PipelineRunAnalyzer):
            source_types = (
                TypeRefs(
                    {EntitySchema.GitHubWorkflow: None, EntitySchema.GenericFile: None}
                ),
            )

        cm = CloudMap.__new__(CloudMap)
        cm.custom_analyzers = [TwoTypeAnalyzer]
        cm.directory = MagicMock()
        cm.directory.get_repository.return_value = None
        cm.directory.get_artifact.return_value = None
        cm.directory.get_type.return_value = None

        src = ".github/workflows/foo.yaml"
        # only one of the two wanted types present -> not a match
        repo = Repository(
            url="git://github.com/owner/repo.git",
            path="owner/repo",
            name="repo",
            contains={src: TypeRefs({EntitySchema.GitHubWorkflow: None})},
        )
        assert cm.find_pipeline_analyzers(repo, src) is None

    def test_find_pipeline_analyzers_source_types_extends(self):
        """source_types matches a base type when the source declares a subtype
        whose CloudType record (transitively) extends it."""

        base_type = "custom.BaseWorkflow"
        mid_type = "custom.MidWorkflow"
        sub_type = "custom.SubWorkflow"

        class BaseAnalyzer(PipelineRunAnalyzer):
            source_types = (TypeRefs({base_type: None}),)

        cm = CloudMap.__new__(CloudMap)
        cm.custom_analyzers = [BaseAnalyzer]
        cm.directory = MagicMock()
        cm.directory.get_repository.return_value = None
        cm.directory.get_artifact.return_value = None

        # sub_type -> mid_type -> base_type (transitive extends chain)
        type_records = {
            sub_type: CloudType(name=sub_type, kind="Artifact", extends=[mid_type]),
            mid_type: CloudType(name=mid_type, kind="Artifact", extends=[base_type]),
        }
        cm.directory.get_type.side_effect = lambda name: type_records.get(name)

        src = ".github/workflows/foo.yaml"
        repo = Repository(
            url="git://github.com/owner/repo.git",
            path="owner/repo",
            name="repo",
            contains={src: TypeRefs({sub_type: None})},
        )
        # the source only declares sub_type, but it transitively extends base_type
        assert cm.find_pipeline_analyzers(repo, src) is BaseAnalyzer

        # an unrelated base type with no record and not in the chain -> None
        class UnrelatedAnalyzer(PipelineRunAnalyzer):
            source_types = (TypeRefs({"custom.Unrelated": None}),)

        cm.custom_analyzers = [UnrelatedAnalyzer]
        assert cm.find_pipeline_analyzers(repo, src) is None

    @staticmethod
    def _make_manager_and_run():
        manager = GithubManager.__new__(GithubManager)
        manager.github = MagicMock()
        manager.hostname = "github.com"
        manager.canonical_url = ""

        run = MagicMock()
        run.html_url = "https://github.com/owner/repo/actions/runs/9"
        run.id = 9
        run.run_number = 1
        run.head_sha = "sha9"
        run.head_branch = "main"
        run.status = "completed"
        run.conclusion = "success"
        run.name = "Foo"
        run.display_title = "Foo"
        run.event = "push"
        run.path = ".github/workflows/foo.yaml"
        run.logs_url = "https://api.github.com/repos/owner/repo/actions/runs/9/logs"
        run.actor = MagicMock(login="octocat")
        run.created_at = None
        run.run_started_at = None
        run.updated_at = None
        run.raw_data = {}
        run.pull_requests = []
        run.get_artifacts.return_value = []

        mock_gh_repo = MagicMock()
        mock_gh_repo.get_workflow_runs.return_value = [run]
        manager.github.get_repo.return_value = mock_gh_repo
        return manager, run

    def test_get_pipeline_runs_invokes_analyzer(self):
        cls, calls = self._make_analyzer()
        manager, run = self._make_manager_and_run()

        cm = CloudMap.__new__(CloudMap)
        cm.custom_analyzers = [cls]
        cm.directory = MagicMock()
        cm.directory.get_repository.return_value = None

        context = MagicMock()
        context.cloudmap = cm
        context._local__env = None  # not safe mode
        context.find_repo.return_value = None  # not cloned -> root_path ""

        repo_info = Repository(
            url="git://github.com/owner/repo.git", path="owner/repo", name="repo"
        )
        results = list(
            manager.get_pipeline_runs(repo_info, ref="main", context=context)
        )
        assert len(results) == 1
        # analyzer ran with the raw run object and empty root_path
        assert len(calls) == 1
        obj, root_path, inst = calls[0]
        assert obj is run
        assert root_path == ""
        assert inst is results[0]
        # the instantiation was mutated in place
        assert results[0].metadata.notes == "seen"

    def test_get_pipeline_runs_safe_mode_withholds_obj(self):
        cls, calls = self._make_analyzer()
        manager, run = self._make_manager_and_run()

        cm = CloudMap.__new__(CloudMap)
        cm.custom_analyzers = [cls]
        cm.directory = MagicMock()
        cm.directory.get_repository.return_value = None

        context = MagicMock()
        context.cloudmap = cm
        context.find_repo.return_value = None

        repo_info = Repository(
            url="git://github.com/owner/repo.git", path="owner/repo", name="repo"
        )
        # safe mode -> raw API object must be withheld from sandboxed analyzers
        saved = global_state.safe_mode
        global_state.safe_mode = True
        try:
            list(manager.get_pipeline_runs(repo_info, ref="main", context=context))
        finally:
            global_state.safe_mode = saved
        assert len(calls) == 1
        obj, _root_path, _inst = calls[0]
        assert obj is None


class TestAnalyzeUrlPipelineRuns:
    """Test that analyze_url fetches pipeline runs when URL has ref or commit."""

    def test_analyze_url_with_ref_fetches_pipelines(self, tmp_path):
        """analyze_url with a #ref fragment should call get_pipeline_runs and store results."""
        unfurl_yaml = tmp_path / "unfurl.yaml"
        unfurl_yaml.write_text(f"apiVersion: {API_VERSION}\nkind: Project\n")
        cloudmap_file = tmp_path / "cloudmap.yaml"
        local_env = LocalEnv(str(unfurl_yaml), can_be_empty=True)

        mock_instantiation = Instantiation(
            url="https://example.com/pipelines/1",
            type=TypeRefs({EntitySchema.CIRun: None}),
            source="git://example.com/owner/repo.git",
            source_revision="abc123",
            revision="abc123",
            metadata=CommonMetadata(title="Pipeline #1", description="success"),
        )

        mock_host = MagicMock(spec=RepositoryHost)
        mock_host.import_project_url.return_value = Repository(
            url="git://example.com/owner/repo.git",
            path="owner/repo",
            name="repo",
        )
        mock_host.get_pipeline_runs.return_value = [mock_instantiation]

        with patch.object(CloudMap, "get_host", return_value=mock_host):
            cm = CloudMap(
                repo=None,
                host_branch="main",
                path=str(cloudmap_file),
                local_env=local_env,
                localrepo_root=str(tmp_path),
            )
            result = cm.analyze_url(
                "https://example.com/owner/repo.git#main", "no"
            )

        assert isinstance(result, Repository)
        db = cm.directory.db
        assert "https://example.com/pipelines/1" in db.instantiations
        inst = db.instantiations["https://example.com/pipelines/1"]
        assert EntitySchema.CIRun in inst.type.types
        mock_host.get_pipeline_runs.assert_called_once_with(
            result, ref="main", commit="", context=cm.directory, workflow_file=""
        )

    def test_analyze_url_with_commit_fetches_pipelines(self, tmp_path):
        """analyze_url with a #~commit fragment should pass commit to get_pipeline_runs."""
        unfurl_yaml = tmp_path / "unfurl.yaml"
        unfurl_yaml.write_text(f"apiVersion: {API_VERSION}\nkind: Project\n")
        cloudmap_file = tmp_path / "cloudmap.yaml"
        local_env = LocalEnv(str(unfurl_yaml), can_be_empty=True)

        mock_host = MagicMock(spec=RepositoryHost)
        mock_host.import_project_url.return_value = Repository(
            url="git://example.com/owner/repo.git",
            path="owner/repo",
            name="repo",
        )
        mock_host.get_pipeline_runs.return_value = []

        with patch.object(CloudMap, "get_host", return_value=mock_host):
            cm = CloudMap(
                repo=None,
                host_branch="main",
                path=str(cloudmap_file),
                local_env=local_env,
                localrepo_root=str(tmp_path),
            )
            cm.analyze_url(
                "https://example.com/owner/repo.git#~deadbeef", "no"
            )

        mock_host.get_pipeline_runs.assert_called_once()
        call_kwargs = mock_host.get_pipeline_runs.call_args
        assert call_kwargs[1]["ref"] == ""
        assert call_kwargs[1]["commit"] == "deadbeef"

    def test_analyze_url_ref_resolves_branch_to_commit(self, tmp_path):
        """analyze_url with a #ref resolves the ref to a commit SHA from branches."""
        unfurl_yaml = tmp_path / "unfurl.yaml"
        unfurl_yaml.write_text(f"apiVersion: {API_VERSION}\nkind: Project\n")
        cloudmap_file = tmp_path / "cloudmap.yaml"
        local_env = LocalEnv(str(unfurl_yaml), can_be_empty=True)

        mock_host = MagicMock(spec=RepositoryHost)
        mock_host.import_project_url.return_value = Repository(
            url="git://example.com/owner/repo.git",
            path="owner/repo",
            name="repo",
            branches={"main": "aaa111", "develop": "bbb222"},
        )
        mock_host.get_pipeline_runs.return_value = []

        with patch.object(CloudMap, "get_host", return_value=mock_host):
            cm = CloudMap(
                repo=None,
                host_branch="main",
                path=str(cloudmap_file),
                local_env=local_env,
                localrepo_root=str(tmp_path),
            )
            cm.analyze_url("https://example.com/owner/repo.git#main", "no")

        mock_host.get_pipeline_runs.assert_called_once()
        call_args = mock_host.get_pipeline_runs.call_args
        assert call_args[1]["ref"] == "main"
        assert call_args[1]["commit"] == "aaa111"

    def test_analyze_url_ref_resolves_tag_to_commit(self, tmp_path):
        """analyze_url with a #ref resolves the ref to a commit SHA from tags."""
        unfurl_yaml = tmp_path / "unfurl.yaml"
        unfurl_yaml.write_text(f"apiVersion: {API_VERSION}\nkind: Project\n")
        cloudmap_file = tmp_path / "cloudmap.yaml"
        local_env = LocalEnv(str(unfurl_yaml), can_be_empty=True)

        mock_host = MagicMock(spec=RepositoryHost)
        mock_host.import_project_url.return_value = Repository(
            url="git://example.com/owner/repo.git",
            path="owner/repo",
            name="repo",
            branches={"main": "aaa111"},
            tags={"v1.2.0": "ccc333"},
        )
        mock_host.get_pipeline_runs.return_value = []

        with patch.object(CloudMap, "get_host", return_value=mock_host):
            cm = CloudMap(
                repo=None,
                host_branch="main",
                path=str(cloudmap_file),
                local_env=local_env,
                localrepo_root=str(tmp_path),
            )
            cm.analyze_url("https://example.com/owner/repo.git#v1.2.0", "no")

        mock_host.get_pipeline_runs.assert_called_once()
        call_args = mock_host.get_pipeline_runs.call_args
        assert call_args[1]["ref"] == "v1.2.0"
        assert call_args[1]["commit"] == "ccc333"

    def test_analyze_url_without_ref_skips_pipelines(self, tmp_path):
        """analyze_url without ref or commit should NOT call get_pipeline_runs."""
        unfurl_yaml = tmp_path / "unfurl.yaml"
        unfurl_yaml.write_text(f"apiVersion: {API_VERSION}\nkind: Project\n")
        cloudmap_file = tmp_path / "cloudmap.yaml"
        local_env = LocalEnv(str(unfurl_yaml), can_be_empty=True)

        mock_host = MagicMock(spec=RepositoryHost)
        mock_host.import_project_url.return_value = Repository(
            url="git://example.com/owner/repo.git",
            path="owner/repo",
            name="repo",
        )

        with patch.object(CloudMap, "get_host", return_value=mock_host):
            cm = CloudMap(
                repo=None,
                host_branch="main",
                path=str(cloudmap_file),
                local_env=local_env,
                localrepo_root=str(tmp_path),
            )
            cm.analyze_url("https://example.com/owner/repo.git", "no")

        mock_host.get_pipeline_runs.assert_not_called()

    def test_analyze_url_pipeline_error_is_caught(self, tmp_path):
        """analyze_url should catch and log errors from get_pipeline_runs."""
        unfurl_yaml = tmp_path / "unfurl.yaml"
        unfurl_yaml.write_text(f"apiVersion: {API_VERSION}\nkind: Project\n")
        cloudmap_file = tmp_path / "cloudmap.yaml"
        local_env = LocalEnv(str(unfurl_yaml), can_be_empty=True)

        mock_host = MagicMock(spec=RepositoryHost)
        mock_host.import_project_url.return_value = Repository(
            url="git://example.com/owner/repo.git",
            path="owner/repo",
            name="repo",
        )
        mock_host.get_pipeline_runs.side_effect = Exception("API error")

        with patch.object(CloudMap, "get_host", return_value=mock_host):
            cm = CloudMap(
                repo=None,
                host_branch="main",
                path=str(cloudmap_file),
                local_env=local_env,
                localrepo_root=str(tmp_path),
            )
            # Should not raise
            result = cm.analyze_url(
                "https://example.com/owner/repo.git#main", "no"
            )

        assert isinstance(result, Repository)
        # No instantiations added due to error
        assert len(cm.directory.db.instantiations) == 0


# Integration tests


@pytest.fixture(scope="module")
def gitlab_manager():
    """Create a GitlabManager pointing at unfurl.cloud."""
    if not UNFURL_TEST_UNFURL_GUI_TOKEN_URL:
        pytest.skip("need UNFURL_TEST_UNFURL_GUI_TOKEN_URL")
    config = {"url": UNFURL_TEST_UNFURL_GUI_TOKEN_URL, "type": "gitlab"}
    return GitlabManager("test", config)


@pytest.fixture(scope="module")
def gitlab_repo_info(gitlab_manager):
    """Import onecommons/unfurl-gui from unfurl.cloud."""
    project = gitlab_manager.gitlab.projects.get("onecommons/unfurl-gui")
    return gitlab_manager.gitlab_project_to_repository(project)


@pytest.fixture(scope="module")
def github_manager():
    """Create a GithubManager with UNFURL_TEST_GITHUB_KEY."""
    if not UNFURL_TEST_GITHUB_KEY:
        pytest.skip("need UNFURL_TEST_GITHUB_KEY")
    config = {
        "url": "https://github.com",
        "password": UNFURL_TEST_GITHUB_KEY,
        "type": "github",
    }
    return GithubManager("test", config)


@pytest.fixture(scope="module")
def github_repo_info(github_manager):
    """Import onecommons/unfurl from github.com."""
    repo = github_manager.github.get_repo("onecommons/unfurl")
    return github_manager.github_repository_to_repository(repo)


@skip_gitlab_integration
class TestGitLabPipelineRunsIntegration:
    def test_gitlab_pipeline_runs_by_ref(self, gitlab_manager, gitlab_repo_info):
        runs = list(gitlab_manager.get_pipeline_runs(gitlab_repo_info, ref="main", limit=5))
        assert len(runs) >= 1
        inst = runs[0]
        assert EntitySchema.GitLabPipelineRun in inst.type.types
        assert inst.source_revision
        assert inst.url

    def test_gitlab_pipeline_runs_by_commit(self, gitlab_manager, gitlab_repo_info):
        branches = gitlab_repo_info.branches
        assert"main" in branches
        sha = branches["main"]
        runs = list(
            gitlab_manager.get_pipeline_runs(gitlab_repo_info, commit=sha, limit=5)
        )
        # May have runs or not, but should not error
        for inst in runs:
            assert inst.source_revision == sha

    def test_gitlab_pipeline_runs_limit(self, gitlab_manager, gitlab_repo_info):
        runs = list(gitlab_manager.get_pipeline_runs(gitlab_repo_info, ref="main", limit=2))
        assert len(runs) <= 2


@skip_github_integration
class TestGitHubWorkflowRunsIntegration:
    def test_github_workflow_runs_by_ref(self, github_manager, github_repo_info):
        runs = list(github_manager.get_pipeline_runs(github_repo_info, ref="main", limit=5))
        assert len(runs) >= 1
        inst = runs[0]
        assert EntitySchema.GitHubRun in inst.type.types
        assert inst.source_revision
        assert inst.url

    def test_github_workflow_runs_by_commit(self, github_manager, github_repo_info):
        branches = github_repo_info.branches
        assert "main" in branches
        sha = branches["main"]
        runs = list(
            github_manager.get_pipeline_runs(github_repo_info, commit=sha, limit=5)
        )
        for inst in runs:
            assert inst.source_revision == sha

    def test_github_workflow_runs_limit(self, github_manager, github_repo_info):
        runs = list(github_manager.get_pipeline_runs(github_repo_info, ref="main", limit=2))
        assert len(runs) <= 2


class TestArtifactAnalyzerRegistry:
    """Tests for the URL-based ``URLAnalyzer`` dispatch on :class:`CloudMap`.

    Covers:
    - Built-in ``OCIArtifactAnalyzer`` and ``GenericPkgArtifactAnalyzer`` are
      registered via ``URLAnalyzers`` and selected by longest-prefix match.
    - A custom ``URLAnalyzer`` subclass registered via
      :meth:`CloudMap.register_url_analyzer` is dispatched by
      :meth:`CloudMap.analyze_url`.
    - When ``init_from_url`` returns ``None`` the dispatcher records nothing
      and returns ``None``.
    """

    @staticmethod
    def _make_cloudmap(tmp_path):
        unfurl_yaml = tmp_path / "unfurl.yaml"
        unfurl_yaml.write_text(f"apiVersion: {API_VERSION}\nkind: Project\n")
        cloudmap_file = tmp_path / "cloudmap.yaml"
        local_env = LocalEnv(str(unfurl_yaml), can_be_empty=True)
        return CloudMap(
            repo=None,
            host_branch="main",
            path=str(cloudmap_file),
            local_env=local_env,
            localrepo_root=str(tmp_path),
        )

    def test_match_url_longest_prefix_wins(self, tmp_path):
        """``match_url_analyzer`` yields every matching analyzer in
        longest-prefix-first order. ``pkg:oci`` beats the generic ``pkg:``
        fallback for OCI URLs; ``pkg:npm/...`` only matches the generic one;
        non-pkg URLs match nothing.
        """
        from unfurl.cloudmap.analyzers import (
            OCIArtifactAnalyzer,
            GenericPkgArtifactAnalyzer,
        )

        cm = self._make_cloudmap(tmp_path)

        oci = list(cm.match_url_analyzer("pkg:oci/library/nginx@latest"))
        assert oci == [OCIArtifactAnalyzer, GenericPkgArtifactAnalyzer]

        docker = list(cm.match_url_analyzer("pkg:docker/library/nginx@1"))
        assert docker == [OCIArtifactAnalyzer, GenericPkgArtifactAnalyzer]

        npm = list(cm.match_url_analyzer("pkg:npm/lodash@4.17.21"))
        assert npm == [GenericPkgArtifactAnalyzer]

        assert list(cm.match_url_analyzer("https://example.com/foo")) == []

    def test_generic_pkg_analyzer_creates_artifact(self, tmp_path):
        """``analyze_url()`` dispatches non-OCI PURLs to the generic analyzer,
        which produces an artifact tagged ``GenericPackage`` and populates
        ``title``/``version`` from the parsed PURL."""
        cm = self._make_cloudmap(tmp_path)
        url = "pkg:npm/lodash@4.17.21"

        artifact = cm.analyze_url(url)

        assert artifact is not None
        assert artifact.url == url
        assert EntitySchema.GenericPackage in artifact.type.types
        assert artifact.metadata.title == "lodash"
        assert artifact.metadata.version == "4.17.21"
        # Persisted in the cloudmap and idempotent on repeat calls.
        assert cm.directory.db.get_artifact(url) is artifact
        assert cm.analyze_url(url, "yes") == artifact

    def test_generic_pkg_analyzer_omits_empty_metadata(self, tmp_path):
        """When the PURL has no name/version components, ``title`` and
        ``version`` are left at their dataclass defaults rather than set to
        empty strings explicitly — keeping the YAML round-trip minimal."""
        cm = self._make_cloudmap(tmp_path)
        # pkg:<type>/ with no name and no @version
        url = "pkg:foo/"

        artifact = cm.analyze_url(url)

        assert artifact is not None
        assert artifact.url == url
        # Both fields default to "" when not parsed from the URL.
        assert artifact.metadata.title == ""
        assert artifact.metadata.version == ""

    def test_custom_analyzer_dispatch(self, tmp_path):
        """A user-registered ``URLAnalyzer`` whose ``url_schemes`` prefix
        is more specific than the built-ins is selected by
        ``match_url_analyzer`` and invoked by ``analyze_url``."""
        from unfurl.tosca_plugins.cloudmap_defs import (
            URLAnalyzer,
            Artifact,
            EntitySchema,
            TypeRefs,
        )

        class CustomTestAnalyzer(URLAnalyzer):
            # Longer than the built-in "pkg:" so it wins for "pkg:test/..."
            # but still satisfies Artifact.url's pkg/git scheme requirement.
            url_schemes = ("pkg:test",)
            artifact_type = EntitySchema.GenericFile

            def __init__(self, url):
                self.url = url

            @classmethod
            def init_from_url(cls, url, parsed):
                return cls(url)

            def analyze_url(self, directory):
                return Artifact(
                    url=self.url,
                    type=TypeRefs({EntitySchema.GenericFile: None}),
                )

        cm = self._make_cloudmap(tmp_path)
        cm.register_url_analyzer(CustomTestAnalyzer)

        url = "pkg:test/widget@1"
        # CustomTestAnalyzer is yielded first (longer prefix), the generic
        # pkg: fallback after it.
        from unfurl.cloudmap.analyzers import GenericPkgArtifactAnalyzer

        assert list(cm.match_url_analyzer(url)) == [
            CustomTestAnalyzer,
            GenericPkgArtifactAnalyzer,
        ]

        artifact = cm.analyze_url(url)
        assert artifact is not None
        assert artifact.url == url
        assert cm.directory.db.get_artifact(url) is artifact

    def test_init_from_url_decline_falls_through_to_next_analyzer(self, tmp_path):
        """When the most-specific analyzer declines via ``init_from_url``
        returning ``None``, ``analyze_url()`` walks the longest-prefix-first
        chain and falls back to the next matching analyzer (here, the
        built-in generic ``pkg:`` handler)."""
        from unfurl.tosca_plugins.cloudmap_defs import URLAnalyzer, Artifact
        from unfurl.cloudmap.analyzers import GenericPkgArtifactAnalyzer

        decline_calls: list = []

        class DecliningAnalyzer(URLAnalyzer):
            # More specific than "pkg:" so it's tried first.
            url_schemes = ("pkg:decline",)

            @classmethod
            def init_from_url(cls, url, parsed):
                decline_calls.append(url)
                return None

        cm = self._make_cloudmap(tmp_path)
        cm.register_url_analyzer(DecliningAnalyzer)

        url = "pkg:decline/widget@1"
        assert list(cm.match_url_analyzer(url)) == [
            DecliningAnalyzer,
            GenericPkgArtifactAnalyzer,
        ]

        record = cm.analyze_url(url)
        # DecliningAnalyzer.init_from_url was consulted...
        assert decline_calls == [url]
        # ...and the generic fallback created an Artifact.
        assert isinstance(record, Artifact)
        assert record.url == url
        assert cm.directory.db.get_artifact(url) is record


# ---------------------------------------------------------------------------
# Custom-analyzer loading from cloudmap config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "test_case,custom_class_code,analyzer_config,expected_count,expected_log",
    [
        (
            "valid_custom_analyzer",
            """
from unfurl.tosca_plugins.cloudmap_defs import RepositoryAnalyzer, Repository, Artifact

class CustomTestAnalyzer(RepositoryAnalyzer):
    files = ["custom-test.yaml"]
    folders = []

    def analyze(self, directory, repo_info, root_path):
        directory.logger.info(f"CustomTestAnalyzer analyzing {self.file}")
        return None
""",
            ["analyzers/custom.py#CustomTestAnalyzer"],
            1,
            "Loaded custom CustomTestAnalyzer",
        ),
        (
            "invalid_path",
            None,  # No file created
            ["analyzers/nonexistent.py#MissingClass"],
            0,
            "Failed to load custom Analyzer",
        ),
        (
            "not_notable_subclass",
            """
class NotAAnalyzer:
    def __init__(self):
        pass
""",
            ["analyzers/notnotable.py#NotAAnalyzer"],
            0,
            "not a subclass of Analyzer",
        ),
        (
            # CloudMapView attributes like _local__env are named to match the
            # safe-mode policy (`name[0] == '_' and '__' in name`), so
            # RestrictedPython rejects the attribute access at compile time and
            # the module fails to load.
            "unsafe_underscore_access",
            """
from unfurl.tosca_plugins.cloudmap_defs import RepositoryAnalyzer

class UnsafeTestAnalyzer(RepositoryAnalyzer):
    files = ["unsafe-test.yaml"]

    def analyze(self, directory, repo_info, root_path):
        return directory._local__env
""",
            ["analyzers/unsafe.py#UnsafeTestAnalyzer"],
            0,
            "Failed to load custom Analyzer",
        ),
    ],
)
def test_custom_analyzers(
    tmp_path,
    monkeypatch,
    caplog,
    test_case,
    custom_class_code,
    analyzer_config,
    expected_count,
    expected_log,
):
    """Test loading custom Analyzer classes from cloudmaps config."""
    cloudmap_repo_path = tmp_path / "cloudmap"
    cloudmap_repo_path.mkdir()

    import git

    repo = git.Repo.init(cloudmap_repo_path)

    cloudmap_yaml = cloudmap_repo_path / "cloudmap.yaml"
    cloudmap_yaml.write_text(
        f"""apiVersion: {API_VERSION}
kind: CloudMap
repositories: {{}}
"""
    )

    repo.index.add(["cloudmap.yaml"])
    repo.index.commit("Initial commit")

    project_path = tmp_path / "project"
    project_path.mkdir()

    if custom_class_code:
        analyzers_dir = project_path / "analyzers"
        analyzers_dir.mkdir()
        class_file = analyzer_config[0].split("#")[0].split("/")[-1]
        custom_py = analyzers_dir / class_file
        custom_py.write_text(custom_class_code)

    unfurl_yaml = project_path / "unfurl.yaml"
    analyzers = "\n".join(f"        - path: {repr(path)}" for path in analyzer_config)
    unfurl_yaml.write_text(
        f"""apiVersion: {API_VERSION}
kind: Project
environments:
  defaults:
    cloudmaps:
      analyzers:
        {analyzers}
      repositories:
        cloudmap:
          url: {cloudmap_repo_path}
"""
    )

    monkeypatch.chdir(project_path)
    local_env = LocalEnv(
        str(project_path),
        can_be_empty=True,
    )

    # Analyzers are loaded in safe mode; `load_class_from_file` reads the
    # safe-mode state from the `global_state.safe_mode` global.
    saved = global_state.safe_mode
    global_state.safe_mode = True
    try:
        cloudmap = CloudMap.from_name(
            local_env,
            "cloudmap",
            None,  # clone_root
            "",  # host_name
            False,  # skip_analysis
            False,  # commit
        )
    finally:
        global_state.safe_mode = saved

    assert len(cloudmap.custom_analyzers) == expected_count
    assert expected_log in caplog.text

    if expected_count > 0:
        assert cloudmap.custom_analyzers[0].__name__ == "CustomTestAnalyzer"
        assert "custom-test.yaml" in cloudmap.directory.analyzer.files
        assert (
            cloudmap.directory.analyzer.files["custom-test.yaml"].__name__
            == "CustomTestAnalyzer"
        )


@skip_github_integration
def test_analyze_url(tmp_path):
    """Test CloudMap.analyze_url() correctly identifies and creates Repository, Artifact, and Service records."""

    unfurl_yaml = tmp_path / "unfurl.yaml"
    unfurl_yaml.write_text(f"""apiVersion: {API_VERSION}
kind: Project
""")

    # Create a minimal cloudmap YAML file in a temp directory
    cloudmap_file = tmp_path / "cloudmap.yaml"

    local_env = LocalEnv(str(unfurl_yaml), can_be_empty=True)
    cm = CloudMap(
        repo=None,
        host_branch="main",
        path=str(cloudmap_file),
        local_env=local_env,
        localrepo_root=str(tmp_path),
    )
    db = cm.directory.db

    # Case 1: plain git URL without file path → creates a Repository
    repo_url = "git://gitrepos.org/someorg/somerepo.git"
    repo = cm.analyze_url(repo_url, "no")
    assert isinstance(repo, Repository)
    expected_repo = Repository(
        url=repo_url,
        path="someorg/somerepo",
        name="somerepo",
    )
    assert repo == expected_repo
    assert db.repositories[repo_url] is repo
    # Calling again should return None when the Repository already exists and analyze == "no"
    repo2 = cm.analyze_url(repo_url, "no")
    assert repo2 is None

    # Case 2: git URL with a file path fragment → creates Repository + Artifact
    requested_url = "https://github.com/nginxinc/docker-nginx.git#:modules/Dockerfile"
    result = cm.analyze_url(requested_url)
    # Repository.contains is keyed by (label, url); url is the repo-relative path
    assert result.contains == {
        ("", "modules/Dockerfile"): TypeRefs(
            {"cloudmap.artifacts.Containerfile": None}
        )
    }
    artifact_url = f"{result.url}#:modules/Dockerfile"
    assert artifact_url in db.artifacts, list(db.artifacts)
    artifact = db.artifacts[artifact_url]
    expected_artifact = Artifact(
        url=artifact_url,
        type=TypeRefs({EntitySchema.ContainerFile: None}),
        # The analyzed url is recorded because it isn't this record's own key:
        # github redirects nginxinc/docker-nginx to nginx/docker-nginx, so the
        # record is keyed under the new name. Recorded in canonical `git://`
        # form, so the spelling used to request it doesn't matter.
        metadata=ArtifactMetadata(
            discovery=Discovery(
                sources=["git://github.com/nginxinc/docker-nginx.git#:modules/Dockerfile"]
            )
        ),
    )
    assert artifact == expected_artifact

    # Case 3: pkg:oci PURL with pinned tag → creates Artifact + Instantiation via OCI
    pkg_url = "pkg:oci/nginx?repository_url=docker.io/library/nginx&tag=1.27.4"
    oci_artifact = cm.analyze_url(pkg_url, "yes")
    instantiation_url = "https://registry-1.docker.io/v2/library/nginx/blobs/sha256:96536756f4a7391a16ef8abf336c7f7ac73cc94fb2b77ab406add4a8bcaa3635"
    expected_oci = Artifact(
        url=pkg_url,
        type=TypeRefs({"cloudmap.artifacts.oci.Image": None}),
        instantiated_by={instantiation_url: None},
        digest="sha256:09369da6b10306312cd908661320086bf87fbae1b6b0c49a1f50ba531fef2eab",
        metadata=ArtifactMetadata(
            description="Official build of Nginx.",
            homepage_url="https://hub.docker.com/_/nginx",
            source_revision="cffeb933620093bc0c08c0b28c3d5cbaec79d729",
            source_url="https://github.com/nginxinc/docker-nginx.git#cffeb933620093bc0c08c0b28c3d5cbaec79d729:mainline/debian",
            version="1.27.4",
            created="2025-02-05T21:27:16Z",
            platforms=[
                {"architecture": "amd64", "os": "linux"},
                {"architecture": "arm", "os": "linux"},
                {"architecture": "arm", "os": "linux"},
                {"architecture": "arm64", "os": "linux"},
                {"architecture": "386", "os": "linux"},
                {"architecture": "mips64le", "os": "linux"},
                {"architecture": "ppc64le", "os": "linux"},
                {"architecture": "s390x", "os": "linux"},
            ],
            discovery=Discovery(
                sources=[
                    "https://registry-1.docker.io/v2/library/nginx/manifests/sha256:09369da6b10306312cd908661320086bf87fbae1b6b0c49a1f50ba531fef2eab",
                    "https://hub.docker.com/v2/repositories/library/nginx/",
                ],
            ),
        ),
    )
    assert oci_artifact == expected_oci
    assert db.artifacts[pkg_url] is oci_artifact

    # Verify the Instantiation record was also created
    assert instantiation_url in db.instantiations
    expected_instantiation = Instantiation(
        url=instantiation_url,
        type=TypeRefs({
            "cloudmap.artifacts.InTotoAttestation": None,
            "cloudmap.artifacts.SpdxDocument": None,
        }),
        instantiated={
            "pkg:oci/nginx?repository_url=docker.io/library/nginx&tag=1.27.4": None
        },
        digest="sha256:96536756f4a7391a16ef8abf336c7f7ac73cc94fb2b77ab406add4a8bcaa3635",
        source_revision="cffeb933620093bc0c08c0b28c3d5cbaec79d729",
        source="https://github.com/nginxinc/docker-nginx.git#cffeb933620093bc0c08c0b28c3d5cbaec79d729:mainline/debian",
    )
    assert db.instantiations[instantiation_url] == expected_instantiation

    # Case 4: regular HTTPS URL → creates a Service
    svc_url = "https://example.com/myservice"
    service = cm.analyze_url(svc_url, "no")
    expected_service = Service(url=svc_url)
    assert service == expected_service
    assert db.services[svc_url] is service
    # Calling again is idempotent
    service2 = cm.analyze_url(svc_url, "yes")
    assert service2 == service

    # Case 5: missing git repository → should return None and not create a record
    assert cm.analyze_url("git://github.com/onecommons/does-not-exist", "no") is None

    # Case 6: git+https scheme URL → treated as git repository
    gitplus_repo = cm.analyze_url("git+https://rando.com/org/repo.git", "no")
    expected_gitplus = Repository(
        url="git://rando.com/org/repo.git",
        path="org/repo",
        name="repo",
        protocols=["https"],
    )
    assert gitplus_repo == expected_gitplus


def test_analyze_url_generic_purl(tmp_path):
    """Test CloudMap.analyze_url() with generic (non-OCI/Docker) PURLs."""

    cloudmap_file = tmp_path / "cloudmap.yaml"

    cm = CloudMap(
        repo=None, host_branch="main", path=str(cloudmap_file), skip_analysis=True
    )
    db = cm.directory.db

    # Simple PURL with name and version
    npm_url = "pkg:npm/express@4.18.2"
    npm_art = cm.analyze_url(npm_url, "no")
    expected_npm = Artifact(
        url=npm_url,
        type=TypeRefs({EntitySchema.GenericPackage: None}),
        metadata=ArtifactMetadata(title="express", version="4.18.2"),
    )
    assert npm_art == expected_npm
    assert db.artifacts[npm_url] is npm_art

    # PURL with namespace
    maven_url = "pkg:maven/org.apache.xmlgraphics/batik-anim@1.9.1"
    maven_art = cm.analyze_url(maven_url, "no")
    expected_maven = Artifact(
        url=maven_url,
        type=TypeRefs({EntitySchema.GenericPackage: None}),
        metadata=ArtifactMetadata(title="batik-anim", version="1.9.1"),
    )
    assert maven_art == expected_maven

    # PURL without version
    pypi_url = "pkg:pypi/requests"
    pypi_art = cm.analyze_url(pypi_url, "no")
    expected_pypi = Artifact(
        url=pypi_url,
        type=TypeRefs({EntitySchema.GenericPackage: None}),
        metadata=ArtifactMetadata(title="requests"),
    )
    assert pypi_art == expected_pypi

    # # Idempotent
    # pypi_art2 = cm.analyze_url(pypi_url, "no")
    # assert pypi_art2 is pypi_art


class _StubURLAnalyzer(URLAnalyzer):
    """Produces whatever the test set in ``emits`` for the url being analyzed.

    Lets a test change what a source produces between runs, which is what
    ``analyze_url(..., replace=True)`` is meant to notice.
    """

    url_schemes = ("stub:",)
    emits: dict = {}

    def __init__(self, url: str):
        self.url = url

    @classmethod
    def init_from_url(cls, url, parsed):
        return cls(url) if url in cls.emits else None

    def analyze_url(self, directory):
        primary = None
        for record in self.emits[self.url]:
            if isinstance(record, Artifact) and primary is None:
                primary = record  # returned for the caller to add
            else:
                directory.add_record(record)
        return primary


def _stub_cloudmap(tmp_path, emits):
    _StubURLAnalyzer.emits = emits
    cm = CloudMap(
        repo=None,
        host_branch="main",
        path=str(tmp_path / "cloudmap.yaml"),
        skip_analysis=True,
    )
    cm.register_url_analyzer(_StubURLAnalyzer)
    return cm


def _sources(record):
    return discovery_sources(record)


def test_analyze_url_records_discovery_source(tmp_path):
    """Every record an analysis produces is attributed to the analyzed url."""
    cm = _stub_cloudmap(
        tmp_path,
        {
            "stub:one": [
                Artifact(url="pkg:generic/main"),
                CloudType(name="some.Type", kind="Component"),
                Service(url="https://svc.example.com"),
            ]
        },
    )
    cm.analyze_url("stub:one")
    db = cm.directory.db

    for record in (
        db.artifacts["pkg:generic/main"],
        db.types["some.Type"],
        db.services["https://svc.example.com"],
    ):
        assert _sources(record) == ["stub:one"], record


def test_analyze_url_replace_collects_orphans(tmp_path):
    """A record the source stops producing is removed; the rest survive."""
    kept = Artifact(url="pkg:generic/kept")
    orphan = Artifact(url="pkg:generic/orphan")
    cm = _stub_cloudmap(tmp_path, {"stub:one": [kept, orphan]})
    cm.analyze_url("stub:one")
    db = cm.directory.db
    assert "pkg:generic/orphan" in db.artifacts

    # the source no longer produces the second artifact
    _StubURLAnalyzer.emits = {"stub:one": [Artifact(url="pkg:generic/kept")]}
    cm.analyze_url("stub:one", replace=True)

    assert "pkg:generic/kept" in db.artifacts
    assert "pkg:generic/orphan" not in db.artifacts, "orphan should be collected"


def test_analyze_url_replace_keeps_records_with_other_sources(tmp_path):
    """A record is only deleted once every source has stopped producing it."""
    shared = CloudType(name="shared.Type", kind="Component")
    cm = _stub_cloudmap(
        tmp_path,
        {
            "stub:one": [Artifact(url="pkg:generic/a"), shared],
            "stub:two": [Artifact(url="pkg:generic/b"), shared],
        },
    )
    cm.analyze_url("stub:one")
    cm.analyze_url("stub:two")
    db = cm.directory.db
    assert _sources(db.types["shared.Type"]) == ["stub:one", "stub:two"]

    # stub:one stops producing the shared type; stub:two still does
    _StubURLAnalyzer.emits["stub:one"] = [Artifact(url="pkg:generic/a")]
    cm.analyze_url("stub:one", replace=True)

    assert "shared.Type" in db.types, "still produced by stub:two"
    assert _sources(db.types["shared.Type"]) == ["stub:two"]


def test_analyze_url_replace_does_not_oscillate(tmp_path):
    """Re-analyzing an unchanged source must not delete anything.

    Analyzers skip re-adding records that already exist, so a record can be
    still-in-use yet never passed to ``add_record``. If that isn't marked, the
    sweep deletes it and the next run recreates it -- alternating forever, so
    this runs three times rather than two.
    """
    from unfurl.cloudmap.analyzers import create_cloud_type_from_type_info

    class _GuardedAnalyzer(_StubURLAnalyzer):
        """Adds a CloudType through the same guard the real analyzers use.

        `create_cloud_type_from_type_info` returns None when the type is
        already in the cloudmap, so the second run never calls `add_record`
        for it -- which is exactly the case the sweep must not misread.
        """

        url_schemes = ("stub:",)

        def analyze_url(self, directory):
            cloud_type = create_cloud_type_from_type_info(
                {"name": "dep.Type", "title": "Dep"}, directory
            )
            if cloud_type:
                directory.add_record(cloud_type)
            return Artifact(url="pkg:generic/a")

    cm = _stub_cloudmap(tmp_path, {"stub:one": []})
    cm.register_url_analyzer(_GuardedAnalyzer)
    db = cm.directory.db
    for run in range(3):
        cm.analyze_url("stub:one", replace=True)
        assert "dep.Type" in db.types, f"deleted on run {run + 1}"
        assert "pkg:generic/a" in db.artifacts, f"deleted on run {run + 1}"


def test_analyze_url_replace_skips_sweep_when_analysis_fails(tmp_path):
    """A failed analysis mustn't be read as 'the source produces nothing'."""

    class _Boom(_StubURLAnalyzer):
        url_schemes = ("stub:",)

        def analyze_url(self, directory):
            directory._mark_failed()
            return None

    cm = _stub_cloudmap(tmp_path, {"stub:one": [Artifact(url="pkg:generic/a")]})
    cm.analyze_url("stub:one")
    db = cm.directory.db
    assert "pkg:generic/a" in db.artifacts

    cm.register_url_analyzer(_Boom)
    cm.analyze_url("stub:one", replace=True)
    assert "pkg:generic/a" in db.artifacts, "must not sweep after a failed run"


def test_analyze_url_replace_matches_repository_file_sources(tmp_path):
    """A repository url collects the records of its own files.

    Records produced by analyzing files inside a repository are attributed to
    ``<repo url>#:<path>``, not the bare repository url, so replacing a
    repository has to match that prefix -- otherwise deleting a file from the
    repository would never collect the records it had produced.
    """
    cm = _stub_cloudmap(tmp_path, {})
    db = cm.directory.db
    repo_url = "git://example.com/org/repo.git"

    # stand in for a previous run that analyzed two of the repository's files
    for path, url in (
        ("kept.yaml", "pkg:generic/from-kept"),
        ("removed.yaml", "pkg:generic/from-removed"),
    ):
        artifact = Artifact(url=url)
        record_discovery_source(artifact, f"{repo_url}#:{path}")
        db.add_record(artifact)
    unrelated = Artifact(url="pkg:generic/unrelated")
    record_discovery_source(unrelated, "git://example.com/org/other.git#:f.yaml")
    db.add_record(unrelated)

    ctx = ProvenanceTrackingContext(cm.directory)
    assert ctx.matches_source(f"{repo_url}#:kept.yaml", repo_url)
    assert ctx.matches_source(repo_url, repo_url)
    assert not ctx.matches_source("git://example.com/org/other.git#:f.yaml", repo_url)

    found = ctx.find_by_source(repo_url)
    assert {key for _kind, key in found} == {
        "pkg:generic/from-kept",
        "pkg:generic/from-removed",
    }, "the repository's own file artifacts, and nothing else"


def test_analyzer_failure_marks_the_run_incomplete(tmp_path):
    """A swallowed analyzer error has to reach the run's error count.

    `Directory.analyze` logs and continues when an analyzer raises, so without
    this the run looks like "produced nothing" and `--replace` would delete
    every record attributed to the url.
    """

    class _Boom(RepositoryAnalyzer):
        files = ("boom.yaml",)

        def analyze(self, directory, repo_info, root_path):
            raise RuntimeError("boom")

    cm = _stub_cloudmap(tmp_path, {})
    repo_info = Repository(url="git://example.com/org/repo.git", path="org/repo")
    mock_repo = MagicMock()
    mock_repo.working_dir = str(tmp_path)

    context = ProvenanceTrackingContext(cm.directory)
    with patch.object(
        type(cm.directory), "analyze_repo", return_value=[_Boom("", "boom.yaml")]
    ):
        with context._tracking_provenance("git://example.com/org/repo.git") as provenance:
            # the tracking context is what `import_project_url` passes down
            cm.directory.analyze(repo_info, mock_repo, context)

    assert provenance.errors == 1, "the swallowed exception must be counted"


def test_analyze_url_replace_forces_reanalysis(tmp_path):
    """`replace` overrides analyze="no", which would otherwise short-circuit.

    The dedupe check returns early for a url that's already recorded; that
    would produce nothing and make every record attributed to it look orphaned.
    """
    # a PURL is recorded under the url itself, so the dedupe check sees it
    url = "pkg:npm/express@4.18.2"
    cm = _stub_cloudmap(tmp_path, {})
    db = cm.directory.db
    cm.analyze_url(url, "no")
    assert url in db.artifacts

    # analyze="no" returns None here because the record already exists
    assert cm.analyze_url(url, "no") is None
    assert cm.analyze_url(url, "no", replace=True) is not None, "forced re-analysis"
    assert url in db.artifacts, "re-analysis re-emitted it, so it wasn't swept"


def test_host_synced_records_are_not_attributed_to_a_url(tmp_path):
    """Repository host sync writes records directly, so they carry no source.

    Their provenance is the host, not a url that was analyzed, and attributing
    them would make them candidates for a sweep they never belonged to.
    """
    cm = _stub_cloudmap(tmp_path, {})
    repo = Repository(url="git://example.com/org/repo.git", path="org/repo")
    with ProvenanceTrackingContext(cm.directory)._tracking_provenance("stub:one"):
        cm.directory.db.add_record(repo)  # what the host managers call
    assert _sources(repo) == []


def test_analyze_url_records_a_canonical_source(tmp_path):
    """The same repository spelled two ways is one discovery source.

    Records are keyed by the canonical `git://` url, so the source has to be
    canonicalized too -- otherwise `--replace git://...` wouldn't find records
    added by `--add https://...`, and the sweep would silently collect nothing.
    """
    cm = _stub_cloudmap(tmp_path, {})
    https_url = "https://gitrepos.org/someorg/somerepo.git#:f.yaml"
    git_url = "git://gitrepos.org/someorg/somerepo.git#:f.yaml"
    assert cm._normalize_analyzed_url(https_url)[0] == git_url
    assert cm._normalize_analyzed_url(git_url)[0] == git_url

    # a non-git url must not be rewritten
    for url in ("pkg:npm/express@4.18.2", "https://example.com/service"):
        assert cm._normalize_analyzed_url(url)[0] == url
