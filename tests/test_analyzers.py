import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from unfurl.cloudmap import (
    CloudMap,
    GithubManager,
    GitlabManager,
    RepositoryHost,
    RepositoryAnalyzer,
)
from unfurl.localenv import LocalEnv
from unfurl.util import API_VERSION
from unfurl.analyzers import (
    GitHubWorkflowNotable,
    GitLabPipelineNotable,
    Notables,
)
from unfurl.tosca_plugins.cloudmap_defs import (
    CommonMetadata,
    EntitySchema,
    Instantiation,
    TypeRefs,
    Repository,
)
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


class TestCINotables:
    """Test CI notable detection."""

    def test_gitlab_pipeline_notable_match(self, tmp_path):
        """Create a temp repo with .gitlab-ci.yml, verify GitLabPipelineNotable is found."""
        (tmp_path / ".gitlab-ci.yml").write_text("stages:\n  - build\n")
        analyzer = RepositoryAnalyzer(list(Notables))
        notables = analyzer.analyze_local(str(tmp_path), str(tmp_path))
        ci_notables = [n for n in notables if isinstance(n, GitLabPipelineNotable)]
        assert len(ci_notables) == 1
        assert ci_notables[0].artifact_type == EntitySchema.GitLabPipeline

    def test_github_workflow_notable_match(self, tmp_path):
        """Create a temp repo with .github/workflows/ci.yml, verify GitHubWorkflowNotable is found."""
        workflows_dir = tmp_path / ".github" / "workflows"
        workflows_dir.mkdir(parents=True)
        (workflows_dir / "ci.yml").write_text("name: CI\non: push\n")
        analyzer = RepositoryAnalyzer(list(Notables))
        notables = analyzer.analyze_local(str(tmp_path), str(tmp_path))
        gh_notables = [n for n in notables if isinstance(n, GitHubWorkflowNotable)]
        assert len(gh_notables) == 1
        assert gh_notables[0].artifact_type == EntitySchema.GitHubWorkflow

    def test_github_workflow_notable_no_workflows_dir(self, tmp_path):
        """If .github exists but no workflows/ subdir, analyze returns None."""
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "CODEOWNERS").write_text("* @owner\n")
        analyzer = RepositoryAnalyzer(list(Notables))
        notables = analyzer.analyze_local(str(tmp_path), str(tmp_path))
        gh_notables = [n for n in notables if isinstance(n, GitHubWorkflowNotable)]
        # The notable is created but analyze() should return None
        assert len(gh_notables) == 1
        repo_info = Repository(url="git://example.com/repo.git", path="repo", name="repo")
        artifact = gh_notables[0].analyze(MagicMock(), repo_info, str(tmp_path))
        assert artifact is None

    def test_no_ci_notable(self, tmp_path):
        """Repo without CI files returns no CI notables."""
        (tmp_path / "README.md").write_text("# Hello\n")
        analyzer = RepositoryAnalyzer(list(Notables))
        notables = analyzer.analyze_local(str(tmp_path), str(tmp_path))
        ci_notables = [
            n
            for n in notables
            if isinstance(n, (GitLabPipelineNotable, GitHubWorkflowNotable))
        ]
        assert len(ci_notables) == 0

    def test_github_workflow_notable_path(self, tmp_path):
        """Test that GitHubWorkflowNotable.path returns .github/workflows."""
        notable = GitHubWorkflowNotable(".", "")
        assert notable.path == ".github/workflows"

    def test_github_workflow_notable_path_nested(self):
        """Test path for a nested folder."""
        notable = GitHubWorkflowNotable("subdir", "")
        assert notable.path == os.path.join("subdir", ".github", "workflows")

    def test_github_workflow_notable_analyze(self, tmp_path):
        """Test that analyze returns an artifact with the correct type and URL."""
        workflows_dir = tmp_path / ".github" / "workflows"
        workflows_dir.mkdir(parents=True)
        (workflows_dir / "ci.yml").write_text("name: CI\n")
        repo_info = Repository(
            url="git://github.com/owner/repo.git", path="owner/repo", name="repo"
        )
        notable = GitHubWorkflowNotable(".", "")
        artifact = notable.analyze(MagicMock(), repo_info, str(tmp_path))
        assert artifact is not None
        assert EntitySchema.GitHubWorkflow in artifact.type.types
        assert ".github/workflows" in artifact.url


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

        mock_pipeline = MagicMock()
        mock_pipeline.id = 42
        mock_project = MagicMock()
        mock_project.pipelines.list.return_value = [mock_pipeline]

        full_pipeline = MagicMock()
        full_pipeline.id = 42
        full_pipeline.web_url = "https://gitlab.example.com/project/-/pipelines/42"
        full_pipeline.sha = "abc123"
        full_pipeline.status = "success"

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
        assert EntitySchema.CIPipelineRun in inst.type.types
        assert inst.source_revision == "abc123"
        assert inst.status == "verified"
        assert inst.metadata.title == "Pipeline #42"

        # Verify properties in type constraint
        constraint = inst.type.types[EntitySchema.CIPipelineRun]
        assert constraint is not None
        props = constraint["properties"]
        assert props["id"] == 42
        assert props["log_url"] == "https://gitlab.example.com/project/-/pipelines/42"
        assert len(props["artifacts"]) == 1
        assert props["artifacts"][0]["name"] == "build/build.zip"
        assert props["artifacts"][0]["size"] == 1024
        assert props["artifacts_expire_at"] == "2026-04-01T00:00:00Z"
        assert len(props["variables"]) == 1
        assert props["variables"][0] == {"key": "CI_DEBUG", "value": "true"}

    @pytest.mark.skipif(GithubManager is None, reason="PyGithub not installed")
    def test_github_get_pipeline_runs(self):
        manager = GithubManager.__new__(GithubManager)
        manager.github = MagicMock()
        manager.hostname = "github.com"
        manager.canonical_url = ""

        mock_run = MagicMock()
        mock_run.html_url = "https://github.com/owner/repo/actions/runs/123"
        mock_run.id = 123
        mock_run.head_sha = "def456"
        mock_run.head_branch = "main"
        mock_run.conclusion = "success"
        mock_run.status = "completed"
        mock_run.name = "CI"
        mock_run.display_title = "CI"
        mock_run.event = "push"
        mock_run.path = ".github/workflows/ci.yml"
        mock_run.logs_url = "https://api.github.com/repos/owner/repo/actions/runs/123/logs"

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
        assert EntitySchema.CIPipelineRun in inst.type.types
        assert inst.source_revision == "def456"
        assert inst.status == "verified"
        assert inst.metadata.title == "CI"
        assert ".github/workflows/ci.yml" in inst.source

        # Verify properties in type constraint
        constraint = inst.type.types[EntitySchema.CIPipelineRun]
        assert constraint is not None
        props = constraint["properties"]
        assert props["id"] == 123
        assert props["log_url"] == "https://api.github.com/repos/owner/repo/actions/runs/123/logs"
        assert len(props["artifacts"]) == 1
        assert props["artifacts"][0]["name"] == "dist"
        assert props["artifacts"][0]["size"] == 2048
        assert props["artifacts"][0]["expires_at"] == "2026-04-15T00:00:00Z"
        assert props["artifacts_expire_at"] == "2026-04-15T00:00:00Z"
        # GitHub does not include variables
        assert "variables" not in props

    def test_gitlab_get_pipeline_runs_with_limit(self):
        manager = GitlabManager.__new__(GitlabManager)
        manager.gitlab = MagicMock()
        manager.hostname = "gitlab.example.com"
        manager.canonical_url = ""

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
            fp.web_url = f"https://gitlab.example.com/project/-/pipelines/{pid}"
            fp.sha = f"sha{pid}"
            fp.status = "success"
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


class TestAddRecordPipelineRuns:
    """Test that add_record fetches pipeline runs when URL has ref or commit."""

    def test_add_record_with_ref_fetches_pipelines(self, tmp_path):
        """add_record with a #ref fragment should call get_pipeline_runs and store results."""
        unfurl_yaml = tmp_path / "unfurl.yaml"
        unfurl_yaml.write_text(f"apiVersion: {API_VERSION}\nkind: Project\n")
        cloudmap_file = tmp_path / "cloudmap.yaml"
        local_env = LocalEnv(str(unfurl_yaml), can_be_empty=True)

        mock_instantiation = Instantiation(
            url="https://example.com/pipelines/1",
            type=TypeRefs({EntitySchema.CIPipelineRun: None}),
            source="git://example.com/owner/repo.git",
            source_revision="abc123",
            revision="abc123",
            status="verified",
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
            result = cm.add_record(
                "https://example.com/owner/repo.git#main", "no"
            )

        assert isinstance(result, Repository)
        db = cm.directory.db
        assert "https://example.com/pipelines/1" in db.instantiations
        inst = db.instantiations["https://example.com/pipelines/1"]
        assert inst.status == "verified"
        assert EntitySchema.CIPipelineRun in inst.type.types
        mock_host.get_pipeline_runs.assert_called_once_with(
            result, ref="main", commit=""
        )

    def test_add_record_with_commit_fetches_pipelines(self, tmp_path):
        """add_record with a #~commit fragment should pass commit to get_pipeline_runs."""
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
            cm.add_record(
                "https://example.com/owner/repo.git#~deadbeef", "no"
            )

        mock_host.get_pipeline_runs.assert_called_once()
        call_kwargs = mock_host.get_pipeline_runs.call_args
        assert call_kwargs[1]["ref"] == ""
        assert call_kwargs[1]["commit"] == "deadbeef"

    def test_add_record_ref_resolves_branch_to_commit(self, tmp_path):
        """add_record with a #ref resolves the ref to a commit SHA from branches."""
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
            cm.add_record("https://example.com/owner/repo.git#main", "no")

        mock_host.get_pipeline_runs.assert_called_once()
        call_args = mock_host.get_pipeline_runs.call_args
        assert call_args[1]["ref"] == "main"
        assert call_args[1]["commit"] == "aaa111"

    def test_add_record_ref_resolves_tag_to_commit(self, tmp_path):
        """add_record with a #ref resolves the ref to a commit SHA from tags."""
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
            cm.add_record("https://example.com/owner/repo.git#v1.2.0", "no")

        mock_host.get_pipeline_runs.assert_called_once()
        call_args = mock_host.get_pipeline_runs.call_args
        assert call_args[1]["ref"] == "v1.2.0"
        assert call_args[1]["commit"] == "ccc333"

    def test_add_record_without_ref_skips_pipelines(self, tmp_path):
        """add_record without ref or commit should NOT call get_pipeline_runs."""
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
            cm.add_record("https://example.com/owner/repo.git", "no")

        mock_host.get_pipeline_runs.assert_not_called()

    def test_add_record_pipeline_error_is_caught(self, tmp_path):
        """add_record should catch and log errors from get_pipeline_runs."""
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
            result = cm.add_record(
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
        assert EntitySchema.CIPipelineRun in inst.type.types
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
        assert EntitySchema.CIPipelineRun in inst.type.types
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
      :meth:`CloudMap.add_record`.
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
        from unfurl.analyzers import (
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
        """``add_record()`` dispatches non-OCI PURLs to the generic analyzer,
        which produces an artifact tagged ``GenericPackage`` and populates
        ``title``/``version`` from the parsed PURL."""
        cm = self._make_cloudmap(tmp_path)
        url = "pkg:npm/lodash@4.17.21"

        artifact = cm.add_record(url)

        assert artifact is not None
        assert artifact.url == url
        assert EntitySchema.GenericPackage in artifact.type.types
        assert artifact.metadata.title == "lodash"
        assert artifact.metadata.version == "4.17.21"
        # Persisted in the cloudmap and idempotent on repeat calls.
        assert cm.directory.db.get_artifact(url) is artifact
        assert cm.add_record(url) is artifact

    def test_generic_pkg_analyzer_omits_empty_metadata(self, tmp_path):
        """When the PURL has no name/version components, ``title`` and
        ``version`` are left at their dataclass defaults rather than set to
        empty strings explicitly — keeping the YAML round-trip minimal."""
        cm = self._make_cloudmap(tmp_path)
        # pkg:<type>/ with no name and no @version
        url = "pkg:foo/"

        artifact = cm.add_record(url)

        assert artifact is not None
        assert artifact.url == url
        # Both fields default to "" when not parsed from the URL.
        assert artifact.metadata.title == ""
        assert artifact.metadata.version == ""

    def test_custom_analyzer_dispatch(self, tmp_path):
        """A user-registered ``URLAnalyzer`` whose ``url_schemes`` prefix
        is more specific than the built-ins is selected by
        ``match_url_analyzer`` and invoked by ``add_record``."""
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
        from unfurl.analyzers import GenericPkgArtifactAnalyzer

        assert list(cm.match_url_analyzer(url)) == [
            CustomTestAnalyzer,
            GenericPkgArtifactAnalyzer,
        ]

        artifact = cm.add_record(url)
        assert artifact is not None
        assert artifact.url == url
        assert cm.directory.db.get_artifact(url) is artifact

    def test_init_from_url_decline_falls_through_to_next_analyzer(self, tmp_path):
        """When the most-specific analyzer declines via ``init_from_url``
        returning ``None``, ``add_record()`` walks the longest-prefix-first
        chain and falls back to the next matching analyzer (here, the
        built-in generic ``pkg:`` handler)."""
        from unfurl.tosca_plugins.cloudmap_defs import URLAnalyzer, Artifact
        from unfurl.analyzers import GenericPkgArtifactAnalyzer

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

        record = cm.add_record(url)
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
from unfurl.tosca_plugins.cloudmap_defs import RepositoryNotable, Repository, Artifact

class CustomTestNotable(RepositoryNotable):
    files = ["custom-test.yaml"]
    folders = []

    def analyze(self, directory, repo_info, root_path):
        directory.logger.info(f"CustomTestNotable analyzing {self.file}")
        return None
""",
            ["notables/custom.py#CustomTestNotable"],
            1,
            "Loaded custom CustomTestNotable",
        ),
        (
            "invalid_path",
            None,  # No file created
            ["notables/nonexistent.py#MissingClass"],
            0,
            "Failed to load custom Analyzer",
        ),
        (
            "not_notable_subclass",
            """
class NotANotable:
    def __init__(self):
        pass
""",
            ["notables/notnotable.py#NotANotable"],
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
from unfurl.tosca_plugins.cloudmap_defs import RepositoryNotable

class UnsafeTestNotable(RepositoryNotable):
    files = ["unsafe-test.yaml"]

    def analyze(self, directory, repo_info, root_path):
        return directory._local__env
""",
            ["notables/unsafe.py#UnsafeTestNotable"],
            0,
            "Failed to load custom Analyzer",
        ),
    ],
)
def test_custom_analyzers(
    tmp_path,
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
        notables_dir = project_path / "notables"
        notables_dir.mkdir()
        class_file = analyzer_config[0].split("#")[0].split("/")[-1]
        custom_py = notables_dir / class_file
        custom_py.write_text(custom_class_code)

    unfurl_yaml = project_path / "unfurl.yaml"
    analyzers = "\n".join(f"        - {repr(path)}" for path in analyzer_config)
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

    os.chdir(project_path)
    local_env = LocalEnv(
        str(project_path),
        can_be_empty=True,
        overrides={"safe_mode": True},
    )

    cloudmap = CloudMap.from_name(
        local_env,
        "cloudmap",
        None,  # clone_root
        "",  # host_name
        False,  # skip_analysis
        False,  # commit
    )

    assert len(cloudmap.custom_analyzers) == expected_count
    assert expected_log in caplog.text

    if expected_count > 0:
        assert cloudmap.custom_analyzers[0].__name__ == "CustomTestNotable"
        assert "custom-test.yaml" in cloudmap.directory.analyzer.files
        assert (
            cloudmap.directory.analyzer.files["custom-test.yaml"].__name__
            == "CustomTestNotable"
        )
