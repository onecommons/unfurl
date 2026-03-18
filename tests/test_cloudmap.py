import os
import traceback
from click.testing import CliRunner
import pytest
from unfurl.__main__ import cli
import git
from unfurl.oci import EntitySchema, Instantiation, join_resource_url
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
    Service,
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
  git://unfurl.cloud/feb20a/dashboard.git:
    path: feb20a/dashboard
    name: dashboard
    protocols:
    - https
    - ssh
    project_url: https://unfurl.cloud/feb20a/dashboard
    metadata:
      homepage_url: https://unfurl.cloud/feb20a/dashboard
      issues_url: https://unfurl.cloud/feb20a/dashboard/-/issues
    private: true
    default_branch: main
    branches:
      main: 4551885dfab39991cfdb958cb79fcb6aa282481d
    notable:
      ensemble/ensemble.yaml:
        type:
          {EntitySchema.Ensemble}:
        artifact: git://unfurl.cloud/feb20a/dashboard.git#:ensemble/ensemble.yaml
      environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml:
        type:
          cloudmap.artifacts.unfurl.Ensemble:
        artifact: git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml
artifacts:
  git://unfurl.cloud/feb20a/dashboard.git#:ensemble/ensemble.yaml:
    type:
      cloudmap.artifacts.unfurl.Ensemble:
    digest: git:tree:5fe07694589fe54e2fb60f250e793db684bbeb95
  git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml:
    type:
      cloudmap.artifacts.unfurl.Ensemble:
    notable:
      pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest:
    instantiates:
      Odoo@unfurl.cloud/onecommons/blueprints/odoo:
    dependencies:
      aws:
        unfurl.relationships.ConnectsTo.AWSAccount:
      gcp:
        unfurl.relationships.ConnectsTo.GoogleCloudProject:
      odoo-aws-1:
        unfurl.relationships.ConnectsTo.AWSAccount:
    digest: git:blob:8e784df418a595b84a916be749024ec967ef1a60
    metadata:
      title: Odoo
      version: 0.1
  pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest:
    type:
      cloudmap.artifacts.oci.Image:
    metadata:
      description: Bitnami Secure Image for odoo
    discovery:
      sources:
      - https://hub.docker.com/v2/repositories/bitnami/odoo/
services:
  https://example.com/oodo:
    type:
      Odoo@unfurl.cloud/onecommons/blueprints/odoo:
    instantiated_by:
    - git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml
instantiations:
  git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml:
    type:
      cloudmap.artifacts.unfurl.Ensemble:
    revision: 4551885dfab39991cfdb958cb79fcb6aa282481d
    source: git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml
    source_revision: 2e57b3251bd9f8e292385b9f31774f6408abc4d7
    instantiated:
      https://example.com/oodo:
    inputs:
      pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest:
types:
  Odoo@unfurl.cloud/onecommons/blueprints/odoo:
    name: Odoo@unfurl.cloud/onecommons/blueprints/odoo
    kind: Component
    extends:
    - Odoo@unfurl.cloud/onecommons/blueprints/odoo
    - unfurl.nodes.SoftwareService@unfurl.cloud/onecommons/std:generic_types
    - SoftwareService@unfurl.cloud/onecommons/std:generic_types
    - SoftwareComponent@unfurl.cloud/onecommons/std:generic_types
    - tosca.nodes.Root
    - tosca.capabilities.Node
    - tosca.capabilities.Root
    metadata:
      title: Odoo
  unfurl.relationships.ConnectsTo.AWSAccount:
    name: unfurl.relationships.ConnectsTo.AWSAccount
    kind: Component
    extends:
    - unfurl.relationships.ConnectsTo.AWSAccount
    - unfurl.relationships.ConnectsTo.CloudAccount
    - unfurl.relationships.ConnectsTo.ComputeMachines
    - tosca.relationships.ConnectsTo
    - tosca.relationships.Root
    - unfurl.relationships.ConnectsTo.ObjectStorage
    metadata:
      title: AWSAccount
  unfurl.relationships.ConnectsTo.GoogleCloudProject:
    name: unfurl.relationships.ConnectsTo.GoogleCloudProject
    kind: Component
    extends:
    - unfurl.relationships.ConnectsTo.GoogleCloudProject
    - unfurl.relationships.ConnectsTo.CloudAccount
    - unfurl.relationships.ConnectsTo.ComputeMachines
    - tosca.relationships.ConnectsTo
    - tosca.relationships.Root
    metadata:
      title: GoogleCloudProject"""

@skip_integration
@pytest.mark.parametrize("commit", ["--commit", ""])
def test_create(runner: CliRunner, caplog, commit: str):
    run_cmd(
        runner,
        ["--home", ""]
        + f"cloudmap {commit} --sync testProvider --namespace feb20a".split(),
        print_result=True,
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
    if commit:
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
        + "cloudmap --sync testProvider --commit --namespace feb20a".split(),
    )
    assert UNFURL_TEST_CLOUDMAP_URL
    for msg in [
        "found git repo git://unfurl.cloud/feb20a/dashboard.git",
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
  git://unfurl.cloud/onecommons/blueprints/cronicle.git:
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
        artifact: git://unfurl.cloud/onecommons/blueprints/cronicle.git#:ensemble-template.yaml%23spec/service_template
artifacts:
  git://unfurl.cloud/onecommons/blueprints/cronicle.git#:ensemble-template.yaml%23spec/service_template:
    type: {EntitySchema.CloudBlueprint}
    notable:
      pkg:oci/cronicle?repository_url=docker.io/soulteary:
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
    kind: Component
    metadata:
      title: CronicleApp
      discussion_url: https://unfurl.cloud/onecommons/blueprints/cronicle/-/issues/1
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
            url="git://github.com/user/test-repo.git",
            path="user/test-repo",
            initial_revision="",
            protocols=["https"],
            default_branch="main",
        )

        assert manager.has_repository(repo)

        repo2 = Repository(
            name="test-repo",
            url="git://gitlab.com/user/test-repo.git",
            path="user/test-repo",
            initial_revision="",
            protocols=["https"],
            default_branch="main",
        )

        assert not manager.has_repository(repo2)

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
        assert result.url == "git://github.com/testuser/test-repo.git"
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
    def test_github_repository_to_repository_conversion(
        self, mock_auth, mock_github_class
    ):
        """Test converting PyGithub Repository to cloudmap Repository with all features."""
        config = {
            "type": "github",
            "url": "https://github.com",
            "password": "token",
            "save_internal": True,
        }
        manager = GithubManager("test", config)

        # Mock PyGithub Repository with all features
        mock_owner = Mock()
        mock_owner.login = "testuser"

        mock_license = Mock()
        mock_license.spdx_id = "MIT"

        mock_repo = Mock()
        mock_repo.name = "test-repo"
        mock_repo.full_name = "testuser/test-repo"
        mock_repo.description = "Test repository with all features"
        mock_repo.private = True
        mock_repo.clone_url = "https://github.com/testuser/test-repo.git"
        mock_repo.ssh_url = "git@github.com:testuser/test-repo.git"
        mock_repo.html_url = "https://github.com/testuser/test-repo"
        mock_repo.default_branch = "develop"
        mock_repo.homepage = "https://example.com"
        mock_repo.get_topics.return_value = ["python", "testing"]
        mock_repo.license = mock_license
        mock_repo.owner = mock_owner
        mock_repo.id = 12345

        # Mock multiple branches
        mock_branch_main = Mock()
        mock_branch_main.name = "main"
        mock_branch_main.commit = Mock()
        mock_branch_main.commit.sha = "abc123def456"

        mock_branch_dev = Mock()
        mock_branch_dev.name = "develop"
        mock_branch_dev.commit = Mock()
        mock_branch_dev.commit.sha = "789ghi012jkl"

        mock_repo.get_branches.return_value = [mock_branch_main, mock_branch_dev]

        # Mock multiple tags
        mock_tag_v1 = Mock()
        mock_tag_v1.name = "v1.0.0"
        mock_tag_v1.commit = Mock()
        mock_tag_v1.commit.sha = "tag111aaa222"

        mock_tag_v2 = Mock()
        mock_tag_v2.name = "v2.0.0"
        mock_tag_v2.commit = Mock()
        mock_tag_v2.commit.sha = "tag333bbb444"

        mock_repo.get_tags.return_value = [mock_tag_v1, mock_tag_v2]

        # Convert to cloudmap Repository
        result = manager.github_repository_to_repository(mock_repo)

        # Verify basic properties
        assert result.name == "test-repo"
        assert result.url == "git://github.com/testuser/test-repo.git"
        assert result.path == "testuser/test-repo"
        assert result.private is True
        assert result.default_branch == "develop"
        assert result.metadata.description == "Test repository with all features"
        assert result.metadata.topics == ["python", "testing"]
        assert result.metadata.homepage_url == "https://example.com"
        assert result.metadata.spdx_licenses == "MIT"

        # Verify internal_id is saved when save_internal=True
        assert result.internal_id == "12345"

        # Verify branches were correctly extracted
        assert result.branches == {"main": "abc123def456", "develop": "789ghi012jkl"}

        # Verify tags were correctly extracted
        assert result.tags == {"v1.0.0": "tag111aaa222", "v2.0.0": "tag333bbb444"}

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
            url="git://github.com/testorg/new-repo.git",
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
            url="git://github.com/testuser/user-repo.git",
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
            url="git://github.com/testuser/test-repo.git",
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


def test_join_resource_url_merges_query_params_with_join_precedence():
    merged = join_resource_url(
        "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&tag=latest&source=base",
        "?tag=1.0&new=value",
    )
    assert (
        merged
        == "pkg:oci/odoo?repository_url=docker.io/bitnami/odoo&source=base&tag=1.0&new=value"
    )


def test_join_resource_url_replaces_repeated_base_key_with_join_values():
    merged = join_resource_url(
        "pkg:oci/name?tag=base1&tag=base2&keep=yes", "?tag=a&tag=b"
    )
    assert merged == "pkg:oci/name?keep=yes&tag=a&tag=b"


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
instantiations:
  "2023-09-24T15:30:00Z":
    type:
      cloudmap.artifacts.IntotoAttestation: null
    source: https://github.com/onecommons/unfurl#:.
    source_revision: f5da8de13ae2dcce293508c4ccac9b373e66dd49
    status: observed
  "2023-09-24T15:31:00Z":
    type:
      cloudmap.artifacts.unfurl.Ensemble: null
    source: git://unfurl.cloud/onecommons/unfurl_cloud_prod.git#:v1:prod
    revision: f5da8de13ae2dcce293508c4ccac9b373e66dd49
artifacts:
  pkg:oci:docker.io/library/nginx:
    type:
      cloudmap.artifacts.oci.Image:
    notable:
      pkg:oci:ghcr.io/library/alpine:
    instantiates:
      software.WebServer:
        version: "1.25"
      software.HTTPServer:
    dependencies:
      "os":
        software.Linux:
          version: ">=5.0"
    instantiated_by:
    - "#/instantiations/2023-09-24T15:30:00Z"
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
    versions:
      "@sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49":
        digest: sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49
        immutable: true
      ?tag=latest:
        digest: sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49
        immutable: false
        metadata:
          version: latest
services:
  https://unfurl.cloud:
    type:
      WebApp@unfurl.cloud/onecommons/std:generic_types:
        version: "1.0"
      capabilities.GitOps:
      capabilities.CICD:
        version: ">=2.0"
    endpoints:
      https://unfurl.cloud/api/v4:
        GitLabAPI:
          version: "4"
    connections:
      https://github.com:
    metadata:
      title: Unfurl Cloud
      description: Open-source platform for collaboratively developing cloud applications
      vendor: OneCommons
      version: 1.0.0
      documentation_url: https://docs.unfurl.cloud
      thumbnail_url: https://unfurl.cloud/unfurl-logo.svg
      source_url: https://github.com/onecommons/unfurl-cloud
    policies:
      spdx_licenses: MIT
      terms_of_service: https://unfurl.cloud/terms
      privacy_policy: https://unfurl.cloud/privacy
    instantiated_by:
    - "#/instantiations/2023-09-24T15:31:00Z"
    discovery:
      last_checked: "2023-09-24T15:30:00Z"
      sources:
      - https://unfurl.cloud/api/v1/metadata
types:
  Zulip@unfurl.cloud/onecommons/blueprints/zulip:
    kind: Component
    source: git://unfurl.cloud/onecommons/blueprints/zulip.git#:types/app.yaml
    metadata:
      title: Zulip
    extends:
    - unfurl.nodes.WebApp@unfurl.cloud/onecommons/std:generic_types
    - WebApp@unfurl.cloud/onecommons/std:generic_types
  software.Nginx@unfurl.cloud/onecommons/std:
    kind: Component
    metadata:
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
        assert "git://github.com/onecommons/unfurl.git" in db.repositories

        # Verify artifacts loaded correctly
        assert "artifacts" in db.db
        assert "pkg:oci:docker.io/library/nginx" in db.artifacts
        artifact = db.get_artifact("pkg:oci:docker.io/library/nginx")
        assert artifact
        assert isinstance(artifact.type, TypeRefs)
        assert "cloudmap.artifacts.oci.Image" in artifact.type.types
        assert artifact.immutable is False
        assert len(artifact.notable) == 1
        assert len(artifact.instantiated_by) == 1

        # Verify instantiations loaded correctly
        assert "instantiations" in db.db
        assert len(db.instantiations) == 2

        # Get the build instantiation (ignore exact timestamp key)
        build_instantiation = None
        for key, inst in db.instantiations.items():
            if "cloudmap.artifacts.IntotoAttestation" in inst.type.types:
                build_instantiation = inst
                assert inst.source == "https://github.com/onecommons/unfurl#:."
                assert (
                    inst.source_revision == "f5da8de13ae2dcce293508c4ccac9b373e66dd49"
                )
                assert inst.status == "observed"
                break
        assert build_instantiation is not None, "Build instantiation not found"

        # Verify instantiates uses typeRef structure
        instantiates = artifact.instantiates
        assert isinstance(instantiates, TypeRefs)
        assert "software.WebServer" in instantiates.types
        assert instantiates.types["software.WebServer"]["version"] == "1.25"
        assert "software.HTTPServer" in instantiates.types
        assert instantiates.types["software.HTTPServer"] is None

        # Verify dependencies uses typeRef structure
        assert artifact.dependencies == {
            "os": TypeRefs({"software.Linux": {"version": ">=5.0"}})
        }

        assert artifact.metadata.title == "Nginx Web Server"
        assert len(artifact.metadata.platforms) == 2
        assert artifact.discovery.last_checked == "2023-09-24T15:30:00Z"
        assert len(artifact.discovery.sources) == 2

        # Verify versions loaded correctly
        versions = artifact.versions
        assert len(versions) == 2
        version_by_digest = db.get_artifact(
            "pkg:oci:docker.io/library/nginx@sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49"
        )
        assert version_by_digest, list(db.artifacts)
        assert "@sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49" in versions
        # assert "pkg:oci:docker.io/library/nginx:latest" in versions

        # Verify version by digest
        assert (
            version_by_digest.digest
            == "sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49"
        )
        assert version_by_digest.immutable is True

        # Verify version by tag
        version_latest = db.get_artifact("pkg:oci:docker.io/library/nginx?tag=latest")
        assert version_latest, list(db.artifacts)
        assert (
            version_latest.digest == "sha256:f5da8de13ae2dcce293508c4ccac9b373e66dd49"
        )
        assert version_latest.immutable is False
        assert version_latest.metadata.version == "latest"

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

        # Verify capabilities uses typeRef structure (capabilities are in service.type)
        assert "capabilities.GitOps" in service_type.types
        assert service_type.types["capabilities.GitOps"] is None
        assert "capabilities.CICD" in service_type.types
        assert service_type.types["capabilities.CICD"]["version"] == ">=2.0"

        assert len(service.endpoints) == 1
        assert len(service.connections) == 1
        assert service.metadata.title == "Unfurl Cloud"
        assert service.policies.spdx_licenses == "MIT"
        assert len(service.instantiated_by) == 1
        assert service.discovery.last_checked == "2023-09-24T15:30:00Z"
        assert len(service.discovery.sources) == 1

        # Get the deployment instantiation (ignore exact timestamp key)
        deployment_instantiation = None
        for key, inst in db.instantiations.items():
            if "cloudmap.artifacts.unfurl.Ensemble" in inst.type.types:
                deployment_instantiation = inst
                assert (
                    inst.source
                    == "git://unfurl.cloud/onecommons/unfurl_cloud_prod.git#:v1:prod"
                )
                assert inst.revision == "f5da8de13ae2dcce293508c4ccac9b373e66dd49"
                break
        assert deployment_instantiation is not None, (
            "Deployment instantiation not found"
        )

        # Verify types loaded correctly
        assert "types" in db.db
        assert len(db.types) == 2

        # Verify Zulip service type
        assert "Zulip@unfurl.cloud/onecommons/blueprints/zulip" in db.types
        zulip_type = db.types["Zulip@unfurl.cloud/onecommons/blueprints/zulip"]
        assert zulip_type.kind == "Component"
        assert zulip_type.metadata.title == "Zulip"
        assert (
            zulip_type.source
            == "git://unfurl.cloud/onecommons/blueprints/zulip.git#:types/app.yaml"
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
        assert nginx_type.kind == "Component"
        assert nginx_type.metadata.title == "Nginx Web Server"
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


def test_get_cloudmap_types(mocker):
    """Test get_cloudmap_types with mocked load_yaml."""
    from unfurl.server.cache import get_cloudmap_types, CLOUDMAP_BRANCH
    import yaml

    # Parse the expected_types_cloudmap YAML string
    cloudmap_doc = yaml.safe_load(expected_types_cloudmap)

    # Variable to capture the CloudMapDB instance
    captured_db = None

    # Create a wrapper for CloudMapDB to capture the instance
    original_cloudmapdb = CloudMapDB

    def cloudmapdb_wrapper(*args, **kwargs):
        nonlocal captured_db
        captured_db = original_cloudmapdb(*args, **kwargs)
        return captured_db

    # Mock load_yaml to return the parsed cloudmap
    with patch("unfurl.server.cache.load_yaml") as mock_load_yaml:
        mock_load_yaml.return_value = (None, cloudmap_doc)

        # Spy on CloudMapDB to capture the instance
        with patch("unfurl.server.cache.CloudMapDB", side_effect=cloudmapdb_wrapper):
            # Create a mock CacheEntry (we just need something to pass in)
            mock_cache_entry = Mock()

            # Call the function
            err, types = get_cloudmap_types("test_project", mock_cache_entry)

            # Verify load_yaml was called correctly
            mock_load_yaml.assert_called_once_with(
                "test_project", CLOUDMAP_BRANCH, "cloudmap.yaml", mock_cache_entry
            )

            # Verify no error
            assert err is None

            # Verify the db was captured
            assert isinstance(captured_db, CloudMapDB)
            assert captured_db.get_repository(
                "git://unfurl.cloud/onecommons/blueprints/cronicle.git#:ensemble-template.yaml%23spec/service_template"
            )

            # Verify we got the expected type
            assert "CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle" in types

            # Verify the type has the expected properties
            cronicle_type = types[
                "CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle"
            ]
            assert cronicle_type == {
                "__typename": "ResourceType",
                "name": "CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle",
                "requirements": [],
                "extends": [
                    "CronicleApp@unfurl.cloud/onecommons/blueprints/cronicle",
                    "unfurl.nodes.WebApp@unfurl.cloud/onecommons/std:generic_types",
                    "WebApp@unfurl.cloud/onecommons/std:generic_types",
                    "_ContainerAppBase@unfurl.cloud/onecommons/std:generic_types",
                    "App@unfurl.cloud/onecommons/std:generic_types",
                    "tosca.nodes.Root",
                    "tosca.capabilities.Node",
                    "tosca.capabilities.Root",
                ],
                "title": "CronicleApp",
                "_sourceinfo": {
                    "file": "ensemble-template.yaml#spec/service_template",
                    "url": "https://unfurl.cloud/onecommons/blueprints/cronicle.git",
                    "incomplete": True,
                },
                "inputsSchema": {},
                "description": "A simple, distributed task scheduler and runner with a web based UI.",
                "implementations": ["connect", "create"],
                "directives": ["substitute"],
                "icon": "https://unfurl.cloud/onecommons/blueprints/cronicle/-/avatar",
            }

@pytest.mark.parametrize("test_case,custom_class_code,analyzer_config,expected_count,expected_log", [
    (
        "valid_custom_analyzer",
        """
from unfurl.cloudmap import Notable, Directory, Repository, Artifact

class CustomTestNotable(Notable):
    files = ["custom-test.yaml"]
    folders = []

    def analyze(self, directory, repo_info, root_path):
        directory.logger.info(f"CustomTestNotable analyzing {self.file}")
        return None
""",
        ["notables/custom.py#CustomTestNotable"],
        1,
        "Loaded custom Notable analyzer"
    ),
    (
        "invalid_path",
        None,  # No file created
        ["notables/nonexistent.py#MissingClass"],
        0,
        "Failed to load custom Notable analyzer"
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
        "not a subclass of Notable"
    ),
])
def test_custom_analyzers(tmp_path, caplog, test_case, custom_class_code, analyzer_config, expected_count, expected_log):
    """Test loading custom Notable analyzer classes from cloudmaps config"""
    from unfurl.cloudmap import CloudMap, Notable
    from unfurl.localenv import LocalEnv

    # Create a temporary cloudmap repository
    cloudmap_repo_path = tmp_path / "cloudmap"
    cloudmap_repo_path.mkdir()

    # Initialize git repo
    import git
    repo = git.Repo.init(cloudmap_repo_path)

    # Create cloudmap.yaml
    cloudmap_yaml = cloudmap_repo_path / "cloudmap.yaml"
    cloudmap_yaml.write_text(f"""apiVersion: {API_VERSION}
kind: CloudMap
repositories: {{}}
""")

    files_to_commit = ["cloudmap.yaml"]

    # Commit the files
    repo.index.add(files_to_commit)
    repo.index.commit("Initial commit")

    # Create unfurl project with custom analyzer config
    project_path = tmp_path / "project"
    project_path.mkdir()

    # Create custom class file if code is provided
    if custom_class_code:
        notables_dir = project_path / "notables"
        notables_dir.mkdir()

        # Extract filename from analyzer_config
        class_file = analyzer_config[0].split("#")[0].split("/")[-1]
        custom_py = notables_dir / class_file
        custom_py.write_text(custom_class_code)

    unfurl_yaml = project_path / "unfurl.yaml"
    unfurl_yaml.write_text(f"""apiVersion: unfurl/v1alpha1
kind: Project
environments:
  defaults:
    cloudmaps:
      analyzers:
        {chr(10).join(f"        - {repr(path)}" for path in analyzer_config)}
      repositories:
        cloudmap:
          url: {cloudmap_repo_path}
""")

    # Load the LocalEnv with skip_default_ensemble to avoid needing an ensemble
    os.chdir(project_path)
    local_env = LocalEnv(str(project_path), overrides={"skip_default_ensemble": True})

    # Create CloudMap instance - this should load (or fail to load) the custom analyzer
    cloudmap = CloudMap.from_name(
        local_env,
        "cloudmap",
        None,  # clone_root
        "",  # host_name
        False,  # skip_analysis
        False,  # commit
    )

    # Verify expected number of custom analyzers
    assert len(cloudmap.custom_analyzers) == expected_count

    # Verify expected log message
    assert expected_log in caplog.text

    # Additional validation for successful load
    if expected_count > 0:
        assert cloudmap.custom_analyzers[0].__name__ == "CustomTestNotable"
        assert "custom-test.yaml" in cloudmap.directory.analyzer.files
        assert cloudmap.directory.analyzer.files["custom-test.yaml"].__name__ == "CustomTestNotable"


def test_instantiation_versions():
    """Test that Instantiation versions property works correctly with type inheritance and serialization."""
    import json

    # Create an Instantiation with versions as dicts
    inst_data = {
        "url": "test-inst",
        "type": {"software.Nginx": None},
        "versions": {
            "v1": {"digest": "sha256:abc123", "status": "verified"},
            "v2": {
                "digest": "sha256:def456",
                "status": "verified",
                "type": {"software.Nginx": {"version": "1.25"}},
            },
        },
    }

    inst = Instantiation(**inst_data)

    # Verify versions were converted to Instantiation instances
    assert isinstance(inst.versions["v1"], Instantiation)
    assert isinstance(inst.versions["v2"], Instantiation)

    # Verify type inheritance from parent
    assert inst.versions["v1"].type.types == {"software.Nginx": None}
    # v2 specifies its own type, should not inherit
    assert inst.versions["v2"].type.types == {"software.Nginx": {"version": "1.25"}}

    # Verify other properties
    assert inst.versions["v1"].digest == "sha256:abc123"
    assert inst.versions["v1"].status == "verified"
    assert inst.versions["v2"].digest == "sha256:def456"

    assert inst.versions["v2"]._parent == inst

    # Test serialization
    result = inst.asdict()
    assert "versions" in result
    assert "v1" in result["versions"]
    assert "v2" in result["versions"]
    assert result["versions"]["v1"]["digest"] == "sha256:abc123"
    assert result["versions"]["v1"]["status"] == "verified"
    assert result["versions"]["v2"]["digest"] == "sha256:def456"

    # Verify the result is JSON serializable
    json_str = json.dumps(result)
    assert json_str  # Should not raise an exception

    # Test round-trip: deserialize and recreate
    recreated = Instantiation(url="test-inst", **result)
    assert isinstance(recreated.versions["v1"], Instantiation)
    assert recreated.versions["v1"].digest == "sha256:abc123"


def test_release_schedule():
    """Test that Service release_schedule property works correctly with serialization."""
    import json

    # Create a Service with release_schedule (formerly migrations)
    service_data = {
        "url": "https://example.com/api",
        "type": {"service.API": None},
        "status": "production",
        "release_schedule": [
            {
                "url": "https://new-example.com/api",
                "status": "production",
                "effective_date": "2026-03-01T00:00:00Z",
            },
            {
                "url": "https://example.com/api/v2",
                "status": "beta",
                "effective_date": "2026-02-15T00:00:00Z",
            },
        ],
    }

    service = Service(**service_data)

    # Verify release_schedule field
    assert len(service.release_schedule) == 2
    assert service.release_schedule[0].url == "https://new-example.com/api"
    assert service.release_schedule[0].status == "production"
    assert service.release_schedule[0].effective_date == "2026-03-01T00:00:00Z"
    assert service.release_schedule[1].url == "https://example.com/api/v2"
    assert service.release_schedule[1].status == "beta"
    assert service.release_schedule[1].effective_date == "2026-02-15T00:00:00Z"

    # Test serialization
    result = service.asdict()
    assert "release_schedule" in result
    assert len(result["release_schedule"]) == 2
    assert result["release_schedule"][0]["url"] == "https://new-example.com/api"
    assert result["release_schedule"][0]["status"] == "production"
    assert result["release_schedule"][1]["url"] == "https://example.com/api/v2"

    # Verify it's JSON serializable
    json_str = json.dumps(result)
    assert json_str  # Should not raise an exception

    # Test round-trip: deserialize and recreate
    recreated = Service(url="https://example.com/api", **result)
    assert len(recreated.release_schedule) == 2
    assert recreated.release_schedule[0].url == "https://new-example.com/api"
