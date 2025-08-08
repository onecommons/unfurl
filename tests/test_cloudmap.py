import os
import traceback
from click.testing import CliRunner
import pytest
from unfurl.__main__ import cli
import git
from unfurl.util import change_cwd, API_VERSION
from unfurl.repo import sanitize_url
from tests.utils import init_project, run_cmd

UNFURL_TEST_CLOUDMAP_URL = os.getenv("UNFURL_TEST_CLOUDMAP_URL")
if not UNFURL_TEST_CLOUDMAP_URL:
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
            args=["init", "--mono", "project"],
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
      ensemble/ensemble.yaml#spec/service_template:
        artifact_type: artifact.tosca.UnfurlEnsemble""".rstrip()

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
    assert "nothing to commit for: synced testProvider" in caplog.text


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
        "nothing to commit for: Update hosts/testProvider with latest from testProvider/feb20a",
        "syncing to feb20a",
        f"skipping push: no change detected on branch testProvider/main for {sanitize_url(UNFURL_TEST_CLOUDMAP_URL)}/feb20a/dashboard.git",
        "nothing to commit for: synced testProvider",
    ]:
        assert msg in caplog.text
