import os
import traceback
from click.testing import CliRunner
import pytest
from unfurl.__main__ import cli
import git
from unfurl.oci import EntitySchema
from unfurl.util import change_cwd, API_VERSION
from unfurl.repo import sanitize_url
from tests.utils import init_project, run_cmd, run_job_cmd
from unittest.mock import Mock, patch
from unfurl.cloudmap import (
    GitlabManager,
    GithubManager,
    Repository,
    RepositoryMetadata,
    Directory,
    CloudMapDB,
    TypeRefs,
)

UNFURL_TEST_CLOUDMAP_URL = os.getenv("UNFURL_TEST_CLOUDMAP_URL")

# Mark to skip integration tests if UNFURL_TEST_CLOUDMAP_URL not set
skip_integration = pytest.mark.skipif(
    not UNFURL_TEST_CLOUDMAP_URL,
    reason="need UNFURL_TEST_CLOUDMAP_URL set to run integration test",
)

# XXX more tests:
# add readonly public test of --import (doesn't need UNFURL_TEST_CLOUDMAP_URL)
# add local test: unfurl cloudmap --sync local --clone-root local-repos
# add commit in local repo and add a project to upstream cloudmap
# verify that sync updates testProvider properly (and delete the created project)

unfurl_yaml = """
apiVersion: unfurl/v1alpha1
kind: Project
environments:
  defaults:
    repositories:
      cloudmap:
        url: file:../cloudmap
    cloudmaps:
      repositories:
        cloudmap:
          # url: file:../cloudmap # configurator needs it to be a regular repository
          clone_root: ../repos
      hosts:
        testProvider:
          type: gitlab
          url:
            get_env: UNFURL_TEST_CLOUDMAP_URL
          canonical_url: https://unfurl.cloud
"""

ensemble_yaml = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    node_types:
      CloudMapExporter:
        derived_from: tosca.nodes.Root
        interfaces:
          Standard:
            operations:
              configure:
                implementation: CloudMap
                inputs:
                  host:
                    url:
                      get_env: UNFURL_TEST_CLOUDMAP_URL
                  cloudmap: cloudmap
                  namespace: feb20a

    topology_template:
      node_templates:
        cloudmap_exporter:
          type: CloudMapExporter
"""

SAVE_TMP = os.getenv("UNFURL_TEST_TMPDIR")

@pytest.fixture(scope="module")
def runner():
    runner = CliRunner()
    with runner.isolated_filesystem(SAVE_TMP) as test_dir:
        if SAVE_TMP:
            print("saving to", test_dir)
        os.system("git init cloudmap")
        with change_cwd("cloudmap"):
            with open("README", "w") as foo:
                foo.write("empty")
            os.system("git add README")
            os.system("git commit -m'initial commit'")
            # switch branches so we can push to main later
            os.system("git checkout -b ignore")

        os.makedirs("repos")
        init_project(
            runner,
            args=["init", "--mono", "--var", "vaultid", "", "project"],
            env=dict(UNFURL_HOME=""),
        )
        with change_cwd("project"):
            # Create a mock deployment
            with open("unfurl.yaml", "w") as f:
                f.write(unfurl_yaml)

            yield runner

expected_cloudmap = f"""apiVersion: {API_VERSION}
kind: CloudMap
repositories:
  unfurl.cloud/feb20a/dashboard:
    git: unfurl.cloud/feb20a/dashboard.git
    path: feb20a/dashboard
    name: dashboard
    protocols:
    - https
    - ssh
    project_url: https://unfurl.cloud/feb20a/dashboard
    metadata:
      issues_url: https://unfurl.cloud/feb20a/dashboard/-/issues
      homepage_url: https://unfurl.cloud/feb20a/dashboard
    private: true
    default_branch: main
    branches:
      main: f2440a4f6cf20bf0c14d0d256d28b796aeacff0b
    notable:
      ensemble/ensemble.yaml:
        type: {EntitySchema.Ensemble}
        artifact: https://unfurl.cloud/feb20a/dashboard.git#:ensemble/ensemble.yaml
artifacts:
  https://unfurl.cloud/feb20a/dashboard.git#:ensemble/ensemble.yaml:
    type: {EntitySchema.Ensemble}""".rstrip()

@skip_integration
def test_create(runner, caplog):
    run_cmd(
        runner,
        ["--home", ""]
        + "cloudmap --sync testProvider --namespace feb20a".split(),
    )
    with change_cwd("cloudmap"):
        with open("cloudmap.yaml") as f:
            cloudmap = f.read().rstrip()
            # print("cloudmap\n", cloudmap)
            assert cloudmap == expected_cloudmap
        assert not os.system("git push origin main")

    assert "importing group feb20a" in caplog.text
    assert "importing group feb20a/feb20b" in caplog.text
    assert "syncing to feb20a" in caplog.text
    assert (
        "committed: Update hosts/testProvider with latest from testProvider/feb20a"
        in caplog.text
    )
    assert 'nothing to commit for "synced to testProvider"' in caplog.text


@skip_integration
def test_sync(runner, caplog):
    # run again, should be a no op
    run_cmd(
        runner,
        ["--home", ""]
        + "cloudmap --sync testProvider --namespace feb20a".split(),
    )
    assert UNFURL_TEST_CLOUDMAP_URL
    for msg in [
        "found git repo unfurl.cloud/feb20a/dashboard.git",
        'nothing to commit for "Update hosts/testProvider with latest from testProvider/feb20a"',
        "syncing to feb20a",
        f"skipping push: no change detected on branch testProvider/main for {sanitize_url(UNFURL_TEST_CLOUDMAP_URL)}/feb20a/dashboard.git",
        'nothing to commit for "synced to testProvider"',
    ]:
        assert msg in caplog.text

@skip_integration
def test_configurator(runner, caplog):
    """Test using CloudMapConfigurator from an ensemble"""
    assert UNFURL_TEST_CLOUDMAP_URL
    # run configurator (exports to cloud)
    with open("ensemble.yaml", "w") as f:
        f.write(ensemble_yaml)

    result, job, summary = run_job_cmd(
        runner,
        ["--home", "", "deploy", "ensemble.yaml"],
    )
    expected = {
        "job": {
            "id": "A01110000000",
            "status": "ok",
            "total": 1,
            "ok": 1,
            "error": 0,
            "unknown": 0,
            "skipped": 0,
            "changed": 1,
        },
        "outputs": {},
        "tasks": [
            {
                "status": "ok",
                "target": "cloudmap_exporter",
                "operation": "configure",
                "template": "cloudmap_exporter",
                "type": "CloudMapExporter",
                "targetStatus": "ok",
                "targetState": "configured",
                "changed": True,
                "configurator": "unfurl.cloudmap.CloudMapConfigurator",
                "priority": "required",
                "reason": "add",
            }
        ],
    }
    assert summary == expected
    result, job, summary = run_job_cmd(
        runner,
        ["--home", "", "deploy", "ensemble.yaml"],
    )
    # no change
    expected["job"]["id"] = "A01110GC0000"
    expected["job"]["changed"] = 0
    expected["job"]["ok"] = 0
    expected["job"]["skipped"] = 1
    expected["tasks"][0]["reason"] = "reconfigure"
    expected["tasks"][0]["status"] = None
    expected["tasks"][0]["changed"] = False
    assert summary == expected


expected_types_cloudmap = f"""apiVersion: {API_VERSION}
kind: CloudMap
repositories:
  unfurl.cloud/onecommons/blueprints/cronicle:
    git: unfurl.cloud/onecommons/blueprints/cronicle.git
    path: onecommons/blueprints/cronicle
    name: Cronicle
    protocols:
    - https
    - ssh
    internal_id: '504'
    project_url: https://unfurl.cloud/onecommons/blueprints/cronicle
    metadata:
      description: A simple, distributed task scheduler and runner with a web based
        UI.
      issues_url: https://unfurl.cloud/onecommons/blueprints/cronicle/-/issues
      homepage_url: https://unfurl.cloud/onecommons/blueprints/cronicle
      avatar_url: https://unfurl.cloud/onecommons/blueprints/cronicle/-/avatar
    default_branch: main
    branches:
      main: c927e49f0fa1bc6c957cc16ca9d554b46d1abe73
    tags:
      v1.0.0: c927e49f0fa1bc6c957cc16ca9d554b46d1abe73
      v0.1.0: 2f9288e491d47ab0d976c135a5a17475bc9c746a
    notable:
      ensemble-template.yaml#spec/service_template:
        type: {EntitySchema.CloudBlueprint}
        artifact: https://unfurl.cloud/onecommons/blueprints/cronicle.git#:ensemble-template.yaml%23spec/service_template
artifacts:
  https://unfurl.cloud/onecommons/blueprints/cronicle.git#:ensemble-template.yaml%23spec/service_template:
    type: {EntitySchema.CloudBlueprint}
    notable:
    - pkg:oci/cronicle?repository_url=docker.io/soulteary:
    instantiates:
      CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle:
    metadata:
      description: A simple, distributed task scheduler and runner with a web based
        UI.
      title: Cronicle
      version: '0.1'
      thumbnail_url: https://unfurl.cloud/onecommons/blueprints/cronicle/-/avatar
types:
  CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle:
    name: CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle
    kind: Service
    title: CronicleApp
    extends:
    - CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle
    - unfurl.nodes.WebApp@unfurl.cloud/onecommons/std:generic_types
    - WebApp@unfurl.cloud/onecommons/std:generic_types
    - _ContainerAppBase@unfurl.cloud/onecommons/std:generic_types
    - App@unfurl.cloud/onecommons/std:generic_types
    - tosca.nodes.Root
    - tosca.capabilities.Node
    - tosca.capabilities.Root
"""


class TestGithubManager:
    """Unit tests for GithubManager using mock GitHub API objects."""

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_init_github_com(self, mock_auth, mock_github_class):
        """Test GithubManager initialization with github.com."""
        config = {
            "type": "github",
            "url": "https://github.com",
            "password": "test_token_123",
        }

        manager = GithubManager("test_github", config)

        assert manager.name == "test_github"
        assert manager.hostname == "github.com"
        assert manager.token == "test_token_123"
        assert manager.base_url == "https://github.com"
        mock_auth.Token.assert_called_once_with("test_token_123")
        mock_github_class.assert_called_once()

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_init_github_enterprise(self, mock_auth, mock_github_class):
        """Test GithubManager initialization with GitHub Enterprise."""
        config = {
            "type": "github",
            "url": "https://github.company.com",
            "password": "enterprise_token",
        }

        manager = GithubManager("test_enterprise", config)

        assert manager.hostname == "github.company.com"
        assert manager.base_url == "https://github.company.com"
        # Verify Enterprise API endpoint was used
        call_kwargs = mock_github_class.call_args[1]
        assert call_kwargs["base_url"] == "https://github.company.com/api/v3"

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_has_repository_github_url(self, mock_auth, mock_github_class):
        """Test has_repository identifies GitHub repos correctly."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}
        manager = GithubManager("test", config)

        repo = Repository(
            name="test-repo",
            git="github.com/user/test-repo.git",
            path="user/test-repo",
            initial_revision="",
            protocols=["https"],
            default_branch="main",
        )

        assert manager.has_repository(repo)

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_has_repository_non_github_url(self, mock_auth, mock_github_class):
        """Test has_repository rejects non-GitHub repos."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}
        manager = GithubManager("test", config)

        repo = Repository(
            name="test-repo",
            git="gitlab.com/user/test-repo.git",
            path="user/test-repo",
            initial_revision="",
            protocols=["https"],
            default_branch="main",
        )

        assert not manager.has_repository(repo)

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_github_repository_to_repository(self, mock_auth, mock_github_class):
        """Test conversion from PyGithub Repository to cloudmap Repository."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}
        manager = GithubManager("test", config)

        # Mock PyGithub Repository object
        mock_repo = Mock()
        mock_repo.name = "test-repo"
        mock_repo.full_name = "testuser/test-repo"
        mock_repo.description = "Test repository"
        mock_repo.private = False
        mock_repo.clone_url = "https://github.com/testuser/test-repo.git"
        mock_repo.ssh_url = "git@github.com:testuser/test-repo.git"
        mock_repo.html_url = "https://github.com/testuser/test-repo"
        mock_repo.default_branch = "main"
        mock_repo.homepage = "https://example.com"
        mock_repo.get_topics.return_value = ["python", "testing"]
        mock_repo.id = 12345

        # Mock license
        mock_license = Mock()
        mock_license.spdx_id = "MIT"
        mock_repo.license = mock_license

        # Mock owner
        mock_owner = Mock()
        mock_owner.login = "testuser"
        mock_repo.owner = mock_owner

        # Mock branches
        mock_branch_main = Mock()
        mock_branch_main.name = "main"
        mock_branch_main.commit = Mock()
        mock_branch_main.commit.sha = "abc123"

        mock_branch_dev = Mock()
        mock_branch_dev.name = "develop"
        mock_branch_dev.commit = Mock()
        mock_branch_dev.commit.sha = "def456"

        mock_repo.get_branches.return_value = [mock_branch_main, mock_branch_dev]

        # Mock tags
        mock_tag_v1 = Mock()
        mock_tag_v1.name = "v1.0.0"
        mock_tag_v1.commit = Mock()
        mock_tag_v1.commit.sha = "tag123"

        mock_tag_v2 = Mock()
        mock_tag_v2.name = "v2.0.0"
        mock_tag_v2.commit = Mock()
        mock_tag_v2.commit.sha = "tag456"

        mock_repo.get_tags.return_value = [mock_tag_v1, mock_tag_v2]

        # Convert to cloudmap Repository
        result = manager.github_repository_to_repository(mock_repo)

        assert result.name == "test-repo"
        assert result.git == "github.com/testuser/test-repo.git"
        assert result.path == "testuser/test-repo"
        assert result.private is False
        assert result.default_branch == "main"
        assert result.metadata.description == "Test repository"
        assert result.metadata.topics == ["python", "testing"]
        assert result.metadata.spdx_licenses == "MIT"
        assert result.metadata.homepage_url == "https://example.com"
        # Verify branches and tags
        assert result.branches == {"main": "abc123", "develop": "def456"}
        assert result.tags == {"v1.0.0": "tag123", "v2.0.0": "tag456"}

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_get_owner_user(self, mock_auth, mock_github_class):
        """Test get_owner returns authenticated user."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}

        # Mock authenticated user
        mock_user = Mock()
        mock_user.login = "testuser"
        mock_github_instance = mock_github_class.return_value
        mock_github_instance.get_user.return_value = mock_user

        manager = GithubManager("test", config, namespace="")

        result = manager.get_owner("")

        assert result == mock_user
        mock_github_instance.get_user.assert_called_once()

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_get_owner_organization(self, mock_auth, mock_github_class):
        """Test get_owner returns organization."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}

        # Mock organization
        mock_org = Mock()
        mock_org.login = "testorg"
        mock_github_instance = mock_github_class.return_value
        mock_github_instance.get_organization.return_value = mock_org

        manager = GithubManager("test", config, namespace="testorg")

        result = manager.get_owner("testorg")

        assert result == mock_org
        mock_github_instance.get_organization.assert_called_once_with("testorg")

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_get_owner_org_not_found(self, mock_auth, mock_github_class):
        """Test get_owner raises error when organization not found."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}

        # Mock GithubException for 404
        from github import GithubException

        mock_exception = GithubException(404, {"message": "Not found"}, None)
        mock_github_instance = mock_github_class.return_value
        mock_github_instance.get_organization.side_effect = mock_exception
        mock_github_instance.get_user.side_effect = mock_exception

        manager = GithubManager("test", config)
        assert manager.get_owner("nonexistent") is None

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_from_host_user_repos(self, mock_auth, mock_github_class):
        """Test fetching repositories from authenticated user."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}

        # Mock user and repos
        mock_user = Mock()
        mock_user.login = "testuser"

        mock_repo = Mock()
        mock_repo.name = "test-repo"
        mock_repo.full_name = "testuser/test-repo"
        mock_repo.description = "Test"
        mock_repo.private = False
        mock_repo.clone_url = "https://github.com/testuser/test-repo.git"
        mock_repo.ssh_url = "git@github.com:testuser/test-repo.git"
        mock_repo.html_url = "https://github.com/testuser/test-repo"
        mock_repo.default_branch = "main"
        mock_repo.homepage = None
        mock_repo.get_topics.return_value = []
        mock_repo.license = None
        mock_repo.owner = mock_user
        mock_repo.id = 123

        # Mock branches
        mock_branch = Mock()
        mock_branch.name = "main"
        mock_branch.commit = Mock()
        mock_branch.commit.sha = "abc123def456"
        mock_repo.get_branches.return_value = [mock_branch]

        # Mock tags
        mock_tag = Mock()
        mock_tag.name = "v1.0.0"
        mock_tag.commit = Mock()
        mock_tag.commit.sha = "tag123abc456"
        mock_repo.get_tags.return_value = [mock_tag]

        mock_user.get_repos.return_value = [mock_repo]

        mock_github_instance = mock_github_class.return_value
        mock_github_instance.get_user.return_value = mock_user

        manager = GithubManager("test", config, namespace="")

        # Mock directory
        mock_directory = Mock(spec=Directory)
        mock_directory.db = Mock()
        mock_directory.db.repositories = {}
        mock_directory.repos_root = "/mock/repos"

        # Mock repo structure to avoid fetch/analyze errors
        mock_git_repo = Mock()
        mock_git_repo.repo.references = []  # Empty list to avoid iteration error
        mock_git_repo.working_dir = "/mock/repos/testuser/test-repo"

        # Mock the remote to return the correct URL
        mock_remote = Mock()
        mock_remote.url = "https://github.com/testuser/test-repo.git"
        mock_git_repo.repo.remote.return_value = mock_remote

        # Mock find_repo and clone_repo to prevent actual git operations
        mock_directory.find_repo.return_value = None
        mock_directory.clone_repo.return_value = mock_git_repo
        mock_directory.maybe_analyze.return_value = None

        manager.from_host(mock_directory)

        # Verify repository was added
        assert len(mock_directory.db.repositories) == 1
        # Repository key is the git URL without protocol
        assert "github.com/testuser/test-repo" in mock_directory.db.repositories
        # Verify branches and tags were captured
        repo = mock_directory.db.repositories["github.com/testuser/test-repo"]
        assert repo.branches == {"main": "abc123def456"}
        assert repo.tags == {"v1.0.0": "tag123abc456"}

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_from_host_organization_repos(self, mock_auth, mock_github_class):
        """Test fetching repositories from organization."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}

        # Mock organization and repos
        mock_org = Mock()
        mock_org.login = "testorg"

        mock_repo = Mock()
        mock_repo.name = "org-repo"
        mock_repo.full_name = "testorg/org-repo"
        mock_repo.description = "Org test"
        mock_repo.private = True
        mock_repo.clone_url = "https://github.com/testorg/org-repo.git"
        mock_repo.ssh_url = "git@github.com:testorg/org-repo.git"
        mock_repo.html_url = "https://github.com/testorg/org-repo"
        mock_repo.default_branch = "main"
        mock_repo.homepage = None
        mock_repo.get_topics.return_value = ["org"]
        mock_repo.license = None
        mock_repo.owner = mock_org
        mock_repo.id = 456

        # Mock multiple branches
        mock_branch_main = Mock()
        mock_branch_main.name = "main"
        mock_branch_main.commit = Mock()
        mock_branch_main.commit.sha = "123abc456def"

        mock_branch_dev = Mock()
        mock_branch_dev.name = "develop"
        mock_branch_dev.commit = Mock()
        mock_branch_dev.commit.sha = "789ghi012jkl"

        mock_repo.get_branches.return_value = [mock_branch_main, mock_branch_dev]

        # Mock multiple tags
        mock_tag_v1 = Mock()
        mock_tag_v1.name = "v1.0.0"
        mock_tag_v1.commit = Mock()
        mock_tag_v1.commit.sha = "aaa111bbb222"

        mock_tag_v2 = Mock()
        mock_tag_v2.name = "v2.0.0"
        mock_tag_v2.commit = Mock()
        mock_tag_v2.commit.sha = "ccc333ddd444"

        mock_repo.get_tags.return_value = [mock_tag_v1, mock_tag_v2]

        mock_org.get_repos.return_value = [mock_repo]

        mock_github_instance = mock_github_class.return_value
        mock_github_instance.get_organization.return_value = mock_org

        manager = GithubManager("test", config, namespace="testorg")

        # Mock directory
        mock_directory = Mock(spec=Directory)
        mock_directory.db = Mock()
        mock_directory.db.repositories = {}
        mock_directory.repos_root = "/mock/repos"

        # Mock repo structure to avoid fetch/analyze errors
        mock_git_repo = Mock()
        mock_git_repo.repo.references = []  # Empty list to avoid iteration error
        mock_git_repo.working_dir = "/mock/repos/testorg/org-repo"

        # Mock the remote to return the correct URL
        mock_remote = Mock()
        mock_remote.url = "https://github.com/testorg/org-repo.git"
        mock_git_repo.repo.remote.return_value = mock_remote

        # Mock find_repo and clone_repo to prevent actual git operations
        mock_directory.find_repo.return_value = None
        mock_directory.clone_repo.return_value = mock_git_repo
        mock_directory.maybe_analyze.return_value = None

        manager.from_host(mock_directory)

        # Verify repository was added
        assert len(mock_directory.db.repositories) == 1
        # Repository key is the git URL without protocol
        assert "github.com/testorg/org-repo" in mock_directory.db.repositories
        # Verify branches and tags were captured
        repo = mock_directory.db.repositories["github.com/testorg/org-repo"]
        assert repo.branches == {"main": "123abc456def", "develop": "789ghi012jkl"}
        assert repo.tags == {"v1.0.0": "aaa111bbb222", "v2.0.0": "ccc333ddd444"}

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_create_project_organization(self, mock_auth, mock_github_class):
        """Test creating a repository in an organization."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}
        manager = GithubManager("test", config)
        manager.dryrun = False

        # Mock organization
        mock_org = Mock()
        mock_org.login = "testorg"

        # Mock created repo with all required attributes
        mock_created_repo = Mock()
        mock_created_repo.full_name = "testorg/new-repo"
        mock_created_repo.description = "New repository"
        mock_created_repo.private = True
        mock_created_repo.get_topics.return_value = []  # Return list for get_topics
        mock_org.create_repo.return_value = mock_created_repo

        # Mock repo to create
        repo_info = Repository(
            name="new-repo",
            git="github.com/testorg/new-repo.git",
            path="testorg/new-repo",
            initial_revision="",
            protocols=["https"],
            default_branch="main",
            metadata=RepositoryMetadata(
                description="New repository",
                topics=["new"],
            ),
            private=True,
        )

        result = manager.create_project(repo_info, mock_org)

        assert result == mock_created_repo
        mock_org.create_repo.assert_called_once_with(
            name="new-repo",
            description="New repository",
            private=True,
            auto_init=False,
        )

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_create_project_user(self, mock_auth, mock_github_class):
        """Test creating a repository for authenticated user."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}

        # Mock authenticated user
        mock_user = Mock()
        mock_user.login = "testuser"

        # Mock created repo with all required attributes
        mock_created_repo = Mock()
        mock_created_repo.full_name = "testuser/user-repo"
        mock_created_repo.description = "User repository"
        mock_created_repo.private = False
        mock_created_repo.get_topics.return_value = []  # Return list for get_topics
        mock_user.create_repo.return_value = mock_created_repo

        manager = GithubManager("test", config)
        manager.dryrun = False

        # Mock repo to create
        repo_info = Repository(
            name="user-repo",
            git="github.com/testuser/user-repo.git",
            path="testuser/user-repo",
            initial_revision="",
            protocols=["https"],
            default_branch="main",
            metadata=RepositoryMetadata(
                description="User repository",
                topics=[],
            ),
            private=False,
        )

        result = manager.create_project(repo_info, mock_user)

        assert result == mock_created_repo
        mock_user.create_repo.assert_called_once_with(
            name="user-repo",
            description="User repository",
            private=False,
            auto_init=False,
        )

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_update_project_metadata(self, mock_auth, mock_github_class):
        """Test updating repository metadata."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}
        manager = GithubManager("test", config)
        manager.dryrun = False

        # Mock existing repo
        mock_repo = Mock()
        mock_repo.full_name = "testuser/test-repo"
        mock_repo.description = "Old description"
        mock_repo.private = True
        mock_repo.get_topics.return_value = ["old"]

        # New metadata
        repo_info = Repository(
            name="test-repo",
            git="github.com/testuser/test-repo.git",
            path="testuser/test-repo",
            initial_revision="",
            protocols=["https"],
            default_branch="main",
            metadata=RepositoryMetadata(
                description="New description",
                topics=["new", "updated"],
            ),
            private=False,
        )

        result = manager.update_project_metadata(repo_info, mock_repo)

        assert result is True
        mock_repo.edit.assert_called()  # Called for description and visibility
        mock_repo.replace_topics.assert_called_once_with(["new", "updated"])

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_git_url_with_auth(self, mock_auth, mock_github_class):
        """Test generating authenticated git URL."""
        config = {
            "type": "github",
            "url": "https://github.com",
            "password": "secret_token",
        }
        manager = GithubManager("test", config)

        mock_repo = Mock()
        mock_repo.clone_url = "https://github.com/user/repo.git"

        result = manager.git_url_with_auth(mock_repo)

        assert result == "https://secret_token@github.com/user/repo.git"

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_canonize_with_canonical_url(self, mock_auth, mock_github_class):
        """Test URL canonization with canonical URL set."""
        config = {
            "type": "github",
            "url": "https://github.com",
            "password": "token",
            "canonical_url": "https://canonical.example.com",
        }
        manager = GithubManager("test", config)

        url = "https://github.com/user/repo.git"
        result = manager.canonize(url)

        assert result == "https://canonical.example.com/user/repo.git"

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_canonize_without_canonical_url(self, mock_auth, mock_github_class):
        """Test URL canonization without canonical URL."""
        config = {"type": "github", "url": "https://github.com", "password": "token"}
        manager = GithubManager("test", config)

        url = "https://github.com/user/repo.git"
        result = manager.canonize(url)

        assert result == url

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_url_credentials_priority(self, mock_auth, mock_github_class):
        """Test that URL credentials take priority over config credentials."""
        config = {
            "type": "github",
            "url": "https://urluser:urltoken@github.com",
            "user": "configuser",
            "password": "configtoken",
        }
        manager = GithubManager("test", config)

        assert manager.user == "urluser"
        assert manager.token == "urltoken"

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_config_credentials_fallback(self, mock_auth, mock_github_class):
        """Test that config credentials are used when URL has none."""
        config = {
            "type": "github",
            "url": "https://github.com",
            "user": "configuser",
            "password": "configtoken",
        }
        manager = GithubManager("test", config)

        assert manager.user == "configuser"
        assert manager.token == "configtoken"

    @patch("unfurl.cloudmap.Github")
    @patch("unfurl.cloudmap.Auth")
    def test_url_user_only_priority(self, mock_auth, mock_github_class):
        """Test that URL username takes priority even without URL password."""
        config = {
            "type": "github",
            "url": "https://urluser@github.com",
            "user": "configuser",
            "password": "configtoken",
        }
        manager = GithubManager("test", config)

        assert manager.user == "urluser"
        assert manager.token == "configtoken"  # Falls back to config password


class TestGitlabManager:
    """Unit tests for GitlabManager using mock GitLab API objects."""

    @patch("unfurl.cloudmap.gitlab.Gitlab")
    def test_url_credentials_priority(self, mock_gitlab_class):
        """Test that URL credentials take priority over config credentials."""
        config = {
            "type": "gitlab",
            "url": "https://urluser:urltoken@gitlab.example.com/namespace",
            "user": "configuser",
            "password": "configtoken",
        }

        # Mock the Gitlab instance to avoid auth attempt
        mock_gitlab_instance = Mock()
        mock_gitlab_class.return_value = mock_gitlab_instance

        manager = GitlabManager("test", config)

        assert manager.user == "urluser"
        assert manager.token == "urltoken"

    @patch("unfurl.cloudmap.gitlab.Gitlab")
    def test_config_credentials_fallback(self, mock_gitlab_class):
        """Test that config credentials are used when URL has none."""
        config = {
            "type": "gitlab",
            "url": "https://gitlab.example.com/namespace",
            "user": "configuser",
            "password": "configtoken",
        }

        # Mock the Gitlab instance to avoid auth attempt
        mock_gitlab_instance = Mock()
        mock_gitlab_class.return_value = mock_gitlab_instance

        manager = GitlabManager("test", config)

        assert manager.user == "configuser"
        assert manager.token == "configtoken"

    @patch("unfurl.cloudmap.gitlab.Gitlab")
    def test_url_user_only_priority(self, mock_gitlab_class):
        """Test that URL username takes priority even without URL password."""
        config = {
            "type": "gitlab",
            "url": "https://urluser@gitlab.example.com/namespace",
            "user": "configuser",
            "password": "configtoken",
        }

        # Mock the Gitlab instance to avoid auth attempt
        mock_gitlab_instance = Mock()
        mock_gitlab_class.return_value = mock_gitlab_instance

        manager = GitlabManager("test", config)

        assert manager.user == "urluser"
        assert manager.token == "configtoken"  # Falls back to config password

    @patch("unfurl.cloudmap.gitlab.Gitlab")
    def test_no_credentials(self, mock_gitlab_class):
        """Test manager initialization with no credentials at all."""
        config = {
            "type": "gitlab",
            "url": "https://gitlab.example.com/namespace",
        }

        # Mock the Gitlab instance to avoid auth attempt
        mock_gitlab_instance = Mock()
        mock_gitlab_class.return_value = mock_gitlab_instance

        manager = GitlabManager("test", config)

        assert manager.user is None
        assert manager.token is None


def test_cloudmap_schema_with_artifacts_and_services():
    """Test CloudMapDB validates artifacts and services sections with new schema."""
    import tempfile

    cloudmap_yaml = f"""apiVersion: {API_VERSION}
kind: CloudMap
repositories:
  github.com/onecommons/unfurl:
    git: github.com/onecommons/unfurl.git
    path: onecommons/unfurl
    name: unfurl
    protocols:
    - https
    - ssh
    default_branch: main
    branches:
      main: f5da8de13ae2dcce293508c4ccac9b373e66dd49
    tags:
      v1.1.0: abc123def456
artifacts:
  pkg:oci:docker.io/library/nginx:
    type: cloudmap.artifacts.oci.Image
    notable:
    - pkg:oci:ghcr.io/library/alpine
    instantiates:
      software.WebServer:
        version: "1.25"
      software.HTTPServer: null
    requires:
      software.Linux:
        version: ">=5.0"
    source:
      location: https://github.com/onecommons/unfurl#:.
      revision: f5da8de13ae2dcce293508c4ccac9b373e66dd49
      provenance: https://ghcr.io/v2/actions/actions-runner/manifests/sha256:6ab8b6170ff81ad2288567b1a2c7446fbd15bc458fd899d94a5626d77e8c90dd
      reproducible: false
    digest: sha256:abc123
    immutable: false
    metadata:
      title: Nginx Web Server
      description: High-performance HTTP server and reverse proxy
      created: "2023-09-24T15:30:00Z"
      platforms:
      - architecture: amd64
        os: linux
      - architecture: arm64
        os: linux
      spdx_licenses: BSD-2-Clause
      vendor: Nginx Inc.
      version: 1.25.3
      homepage_url: https://nginx.org
      documentation_url: https://nginx.org/en/docs/
      thumbnail_url: https://nginx.org/images/nginx-logo.png
    discovery:
      last_checked: "2023-09-24T15:30:00Z"
      sources:
      - https://ghcr.io/v2/nginx/manifests/latest
      - https://github.com/nginx/nginx/releases
    releases:
      pkg:oci:docker.io/library/nginx@sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49:
        digest: sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49
        immutable: true
      pkg:oci:docker.io/library/nginx:latest:
        digest: sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49
        immutable: false
        metadata:
          version: latest
services:
  https://unfurl.cloud:
    type:
      WebApp@unfurl.cloud/onecommons/std:generic_types:
        version: "1.0"
    capabilities:
      capabilities.GitOps:
      capabilities.CICD:
        version: ">=2.0"
    endpoints:
    - url: https://unfurl.cloud/api/v1
      type: API
    dependencies:
    - https://github.com
    metadata:
      title: Unfurl Cloud
      description: Open-source platform for collaboratively developing cloud applications
      vendor: OneCommons
      version: 1.0.0
      documentation_url: https://docs.unfurl.cloud
      thumbnail_url: https://unfurl.cloud/unfurl-logo.svg
      source_url: https://github.com/onecommons/unfurl-cloud
    notable:
    - pkg:oci:ghcr.io/onecommons/unfurl
    policies:
      spdx_licenses: MIT
      terms_of_service: https://unfurl.cloud/terms
      privacy_policy: https://unfurl.cloud/privacy
    deployment:
      location: unfurl.cloud/onecommons/unfurl_cloud_prod#v1:prod
      type: cloudmap.artifacts.unfurl.Ensemble
      revision: f5da8de13ae2dcce293508c4ccac9b373e66dd49
    discovery:
      last_checked: "2023-09-24T15:30:00Z"
      sources:
      - https://unfurl.cloud/api/v1/metadata
types:
  Zulip@unfurl.cloud/onecommons/blueprints/zulip:
    kind: Service
    title: Zulip
    source: unfurl.cloud/onecommons/blueprints/zulip#:types/app.yaml
    extends:
    - unfurl.nodes.WebApp@unfurl.cloud/onecommons/std:generic_types
    - WebApp@unfurl.cloud/onecommons/std:generic_types
  software.Nginx@unfurl.cloud/onecommons/std:
    kind: Software
    title: Nginx Web Server
    extends:
    - software.WebServer@unfurl.cloud/onecommons/std:generic_types
"""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(cloudmap_yaml)
        temp_path = f.name

    try:
        # This should validate the schema successfully
        db = CloudMapDB(temp_path)

        # Verify repositories loaded correctly
        assert len(db.repositories) == 1
        assert "github.com/onecommons/unfurl" in db.repositories

        # Verify artifacts loaded correctly
        assert "artifacts" in db.db
        assert "pkg:oci:docker.io/library/nginx" in db.artifacts
        artifact = db.artifacts["pkg:oci:docker.io/library/nginx"]
        assert artifact.type == "cloudmap.artifacts.oci.Image"
        assert artifact.source.revision == "f5da8de13ae2dcce293508c4ccac9b373e66dd49"
        assert artifact.source.reproducible is False
        assert artifact.immutable is False
        assert len(artifact.notable) == 1

        # Verify instantiates uses typeRef structure
        instantiates = artifact.instantiates
        assert isinstance(instantiates, TypeRefs)
        assert "software.WebServer" in instantiates.types
        assert instantiates.types["software.WebServer"]["version"] == "1.25"
        assert "software.HTTPServer" in instantiates.types
        assert instantiates.types["software.HTTPServer"] is None

        # Verify requires uses typeRef structure
        requires = artifact.requires
        assert isinstance(requires, TypeRefs)
        assert "software.Linux" in requires.types
        assert requires.types["software.Linux"]["version"] == ">=5.0"

        assert artifact.metadata.title == "Nginx Web Server"
        assert len(artifact.metadata.platforms) == 2
        assert artifact.discovery.last_checked == "2023-09-24T15:30:00Z"
        assert len(artifact.discovery.sources) == 2

        # Verify releases loaded correctly
        releases = artifact.releases
        assert len(releases) == 2
        assert (
            "pkg:oci:docker.io/library/nginx@sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49"
            in releases
        )
        assert "pkg:oci:docker.io/library/nginx:latest" in releases

        # Verify release by digest
        release_by_digest = releases[
            "pkg:oci:docker.io/library/nginx@sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49"
        ]
        assert (
            release_by_digest.digest
            == "sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49"
        )
        assert release_by_digest.immutable is True

        # Verify release by tag
        release_latest = releases["pkg:oci:docker.io/library/nginx:latest"]
        assert (
            release_latest.digest == "sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49"
        )
        assert release_latest.immutable is False
        assert release_latest.metadata.version == "latest"

        # Verify services loaded correctly
        assert "services" in db.db
        assert "https://unfurl.cloud" in db.services
        service = db.services["https://unfurl.cloud"]

        # Verify type uses typeRef structure
        service_type = service.type
        assert isinstance(service_type, TypeRefs)
        assert "WebApp@unfurl.cloud/onecommons/std:generic_types" in service_type.types
        assert (
            service_type.types["WebApp@unfurl.cloud/onecommons/std:generic_types"][
                "version"
            ]
            == "1.0"
        )

        # Verify capabilities uses typeRef structure
        capabilities = service.capabilities
        assert isinstance(capabilities, TypeRefs)
        assert "capabilities.GitOps" in capabilities.types
        assert capabilities.types["capabilities.GitOps"] is None
        assert "capabilities.CICD" in capabilities.types
        assert capabilities.types["capabilities.CICD"]["version"] == ">=2.0"

        assert len(service.endpoints) == 1
        assert len(service.dependencies) == 1
        assert service.metadata.title == "Unfurl Cloud"
        assert service.policies.spdx_licenses == "MIT"
        assert service.deployment.type == "cloudmap.artifacts.unfurl.Ensemble"
        assert service.discovery.last_checked == "2023-09-24T15:30:00Z"
        assert len(service.discovery.sources) == 1

        # Verify types loaded correctly
        assert "types" in db.db
        assert len(db.types) == 2

        # Verify Zulip service type
        assert "Zulip@unfurl.cloud/onecommons/blueprints/zulip" in db.types
        zulip_type = db.types["Zulip@unfurl.cloud/onecommons/blueprints/zulip"]
        assert zulip_type.kind == "Service"
        assert zulip_type.title == "Zulip"
        assert (
            zulip_type.source
            == "unfurl.cloud/onecommons/blueprints/zulip#:types/app.yaml"
        )

        # Verify extends is an array of strings
        extends = zulip_type.extends
        assert isinstance(extends, list)
        assert len(extends) == 2
        assert (
            "unfurl.nodes.WebApp@unfurl.cloud/onecommons/std:generic_types" in extends
        )
        assert "WebApp@unfurl.cloud/onecommons/std:generic_types" in extends

        # Verify Nginx software type
        assert "software.Nginx@unfurl.cloud/onecommons/std" in db.types
        nginx_type = db.types["software.Nginx@unfurl.cloud/onecommons/std"]
        assert nginx_type.kind == "Software"
        assert nginx_type.title == "Nginx Web Server"
        assert not nginx_type.source  # Optional field not provided

        # Verify extends for Nginx is an array
        nginx_extends = nginx_type.extends
        assert isinstance(nginx_extends, list)
        assert len(nginx_extends) == 1
        assert (
            "software.WebServer@unfurl.cloud/onecommons/std:generic_types"
            in nginx_extends
        )

    finally:
        # Clean up temp file
        os.unlink(temp_path)


def test_get_cloudmap_types():
    """Test get_cloudmap_types with mocked load_yaml."""
    from unfurl.server.cache import get_cloudmap_types, CLOUDMAP_BRANCH
    import yaml

    # Parse the expected_types_cloudmap YAML string
    cloudmap_doc = yaml.safe_load(expected_types_cloudmap)

    # Mock load_yaml to return the parsed cloudmap
    with patch("unfurl.server.cache.load_yaml") as mock_load_yaml:
        mock_load_yaml.return_value = (None, cloudmap_doc)

        # Create a mock CacheEntry (we just need something to pass in)
        mock_cache_entry = Mock()

        # Call the function
        err, types = get_cloudmap_types("test_project", mock_cache_entry)

        # Verify load_yaml was called correctly
        mock_load_yaml.assert_called_once_with(
            "test_project",
            CLOUDMAP_BRANCH,
            "cloudmap.yaml",
            mock_cache_entry
        )

        # Verify no error
        assert err is None

        # Verify we got the expected type
        assert "CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle" in types

        # Verify the type has the expected properties
        cronicle_type = types["CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle"]
        assert cronicle_type["name"] == "CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle"
        assert cronicle_type["title"] == "CronicleApp"
        assert cronicle_type["__typename"] == "ResourceType"

        # Verify extends are fully qualified
        assert len(cronicle_type["extends"]) > 0

        # Verify implementations and directives are set
        assert "connect" in cronicle_type["implementations"]
        assert "create" in cronicle_type["implementations"]
        assert "substitute" in cronicle_type["directives"]

        # Verify metadata fields
        assert "description" in cronicle_type
        assert cronicle_type["description"] == "A simple, distributed task scheduler and runner with a web based UI."

        # Verify icon/thumbnail is set
        assert "icon" in cronicle_type
        assert cronicle_type["icon"] == "https://unfurl.cloud/onecommons/blueprints/cronicle/-/avatar"
