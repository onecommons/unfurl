import os
import traceback
from click.testing import CliRunner
import pytest
from unfurl.__main__ import cli
import git
from unfurl.util import change_cwd
from tests.utils import init_project, run_cmd

if not os.getenv("UNFURL_TEST_CLOUDMAP_URL"):
    pytest.skip(
        reason="need UNFURL_TEST_CLOUDMAP_URL set to run test",
        allow_module_level=True,
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
    cloudmaps:
      repositories:
        cloudmap:
          url: file:../cloudmap
          clone_root: ../repos
      hosts:
        testProvider:
          type: gitlab
          url:
            get_env: UNFURL_TEST_CLOUDMAP_URL
          canonical_url: https://unfurl.cloud
"""


@pytest.fixture(scope="module")
def runner():
    runner = CliRunner()
    with runner.isolated_filesystem():
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
            args=["init", "--mono", "project"],
            env=dict(UNFURL_HOME=""),
        )
        with change_cwd("project"):
            # Create a mock deployment
            with open("unfurl.yaml", "w") as f:
                f.write(unfurl_yaml)

            yield runner


expected_cloudmap = """apiVersion: unfurl/v1alpha1
kind: CloudMap
schema: unfurl.cloud/onecommons/unfurl-types
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
      main: 1dd9512edccaec2049a001fedf47195ab6ea298f
    notable:
      ensemble/ensemble.yaml#spec/service_template:
        artifact_type: artifact.tosca.UnfurlEnsemble
        name: Generic cloud provider implementations
        version: 2.0.0
        schema: https://unfurl.cloud/onecommons/unfurl-types.git#v"""


def test_create(runner, caplog):
    run_cmd(
        runner,
        ["--home", ""]
        + "cloudmap --sync testProvider --namespace feb20a".split(),
    )
    with change_cwd("cloudmap"):
        with open("cloudmap.yaml") as f:
            cloudmap = f.read()
            # print("cloudmap", cloudmap)
            assert cloudmap.startswith(expected_cloudmap), cloudmap
        assert not os.system("git push origin main")

    assert "importing group feb20a" in caplog.text
    assert "importing group feb20a/feb20b" in caplog.text
    assert "syncing to feb20a" in caplog.text
    assert (
        "committed: Update hosts/testProvider with latest from testProvider/feb20a"
        in caplog.text
    )
    assert "nothing to commit for: synced testProvider" in caplog.text


def test_sync(runner, caplog):
    # run again, should be a no op
    run_cmd(
        runner,
        ["--home", ""]
        + "cloudmap --sync testProvider --namespace feb20a".split(),
    )
    for msg in [
        "found git repo unfurl.cloud/feb20a/dashboard.git",
        "nothing to commit for: Update hosts/testProvider with latest from testProvider/feb20a",
        "syncing to feb20a",
        "skipping push: no change detected on branch testProvider/main for https://XXXXX:XXXXX@staging.unfurl.cloud/feb20a/dashboard.git",
        "nothing to commit for: synced testProvider",
    ]:
        assert msg in caplog.text
