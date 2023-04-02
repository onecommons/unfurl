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
        reason="need UNFURL_TEST_CLOUDMAP_GITLAB_TOKEN set to run test",
        allow_module_level=True,
    )

# XXX more tests:
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
repositories:
- git: unfurl.cloud/feb20a/dashboard.git
  path: feb20a/dashboard
  name: dashboard
  protocols:
  - https
  - ssh
  internal_id: '973'
  project_url: https://unfurl.cloud/feb20a/dashboard
  metadata:
    issues_url: https://unfurl.cloud/feb20a/dashboard/-/issues
    homepage_url: https://unfurl.cloud/feb20a/dashboard
  private: true
  default_branch: main
  branches:
    main:"""


def test_create(runner, caplog):
    run_cmd(
        runner,
        ["--home", ""]
        + "cloudmap --sync testProvider --namespace feb20a".split(),
    )
    with change_cwd("cloudmap"):
        with open("cloudmap.yaml") as f:
            cloudmap = f.read()
            assert cloudmap.startswith(expected_cloudmap), cloudmap
        assert not os.system("git push origin main")

    assert "syncing to feb20a" in caplog.text
    assert (
        "committed: Update hosts/testProvider/feb20a with latest from testProvider/feb20a"
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
        "nothing to commit for: Update hosts/testProvider/feb20a with latest from testProvider/feb20a",
        "syncing to feb20a",
        "pushed to https://XXXXX:XXXXX@app.dev.unfurl.cloud/feb20a/dashboard.git: [up to date]",
        "nothing to commit for: synced testProvider",
    ]:
        assert msg in caplog.text
