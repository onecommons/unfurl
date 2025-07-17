import pytest
import json
import os
import traceback
from click.testing import CliRunner
from unfurl.__main__ import cli, _latestJobs
from unfurl.localenv import LocalEnv
from unfurl.job import start_job
from .utils import init_project, run_job_cmd
from pathlib import Path
from unfurl.support import Status
from unfurl.configurator import Configurator

manifest = """\
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    imports:
      - repository: unfurl
        file: tosca_plugins/googlecloud.yaml

    topology_template:
      node_templates:
        should_skip:
          directives:
          - default
          description: this default template isn't referenced so shouldn't be part of the plan
          type: tosca:Root
          interfaces:
            Standard:
              configure: echo "should have been skipped"

        gcp-org:
          type: unfurl.nodes.GoogleCloudOrganization
          properties:
            display_name: myorg.com
            id: 9999999999
            billing_account: BBBBBBB

        dev-folder:
          type: unfurl.nodes.GoogleCloudFolder
          properties:
            display_name: dev
            id: 888888888
          requirements:
          - host:
              node: gcp-org

        dev_gcp_project:
          type: unfurl.nodes.GoogleCloudProject
          properties:
            name: dev1
            activate_apis: [dns.googleapis.com]
          requirements:
          - host:
              node: dev-folder

        root_gcp_project:
          type: unfurl.nodes.GoogleCloudProject
          properties:
            name: prod
          requirements:
          - host:
              node: gcp-org

"""


# @pytest.mark.skip
def test_plan():
    runner = CliRunner(mix_stderr=False)
    with runner.isolated_filesystem():
        init_project(
            runner,
            args=["init", "--mono"],
        )
        with open("ensemble/ensemble.yaml", "w") as f:
            f.write(manifest)
        # suppress logging
        try:
            old_env_level = os.environ.get("UNFURL_LOGGING")
            result, job, summary = run_job_cmd(
                runner,
                ["--quiet", "plan", "--output=json"],
                env={"UNFURL_LOGGING": "critical"},
            )
        finally:
            if old_env_level:
                os.environ["UNFURL_LOGGING"] = old_env_level
        # print(job.manifest.status_summary())
        planoutput = result.output.strip()
        assert planoutput
        # print("planoutput", planoutput)
        plan: list = json.loads(planoutput)
        if "job_request" in plan[0]:  # external artifact setup
            plan.pop(0)
        assert plan[0]["instance"] == "dev_gcp_project"
        folder = plan[0]["plan"][0]["sequence"][0]["rendered"]["tasks"]
        with open(Path(folder) / "main.unfurl.tmp.tf") as tf:
            main_tf = tf.read().strip()
            # print(main_tf)
            assert 'billing_account   = "BBBBBBB"' in main_tf
            assert 'activate_apis = ["dns.googleapis.com"]' in main_tf
            assert "dev1" in main_tf
            assert 'org_id = ""' in main_tf and "9999999999" not in main_tf
            assert 'folder_id = "888888888"' in main_tf

        assert plan[1]["instance"] == "root_gcp_project"
        folder = plan[1]["plan"][0]["sequence"][0]["rendered"]["tasks"]
        with open(Path(folder) / "main.unfurl.tmp.tf") as tf:
            main_tf = tf.read().strip()
            # print(main_tf)
            assert 'billing_account   = "BBBBBBB"' in main_tf
            assert 'activate_apis = ["dns.googleapis.com"]' not in main_tf
            assert "prod" in main_tf
            assert 'org_id = "9999999999"' in main_tf
            assert "folder_id" not in main_tf


gcpTestManifest = """\
  apiVersion: unfurl/v1alpha1
  kind: Ensemble
  +include:
    file: ensemble-template.yaml
    repository: spec
  environment:
    variables:
      GOOGLE_APPLICATION_CREDENTIALS: bad.json
  changes: [] # set this so we save changes here instead of the job changelog files
  spec:
    service_template:
      topology_template:
        node_templates:
          testNode:
            type: tosca.nodes.Root
            interfaces:
             Standard:
              operations:
                create:
                  implementation:
                    className: unfurl.configurators.TemplateConfigurator
                  inputs:
                    resultTemplate:
                      readyState: ok
  """

# test bad connection aborts job
# test with good connection
# create and destroy unfurl_service_account, verify upgraded GOOGLE_APPLICATION_CREDENTIALS

class TestEnvironConfigurator(Configurator):
    def run(self, task):
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"], os.environ
        assert "GOOGLE_OAUTH_ACCESS_TOKEN" not in os.environ, os.environ
        yield task.done(True, Status.ok)

gcpTestUpgradeConnectionManifest = """\
  apiVersion: unfurl/v1alpha1
  kind: Ensemble
  +include:
    file: ensemble-template.yaml
    repository: spec
  environment:
    variables:
      GOOGLE_OAUTH_ACCESS_TOKEN: fake
      GOOGLE_APPLICATION_CREDENTIALS: null
  changes: [] # set this so we save changes here instead of the job changelog files
  spec:
    service_template:
      topology_template:
        node_templates:
          testNode:
            type: tosca.nodes.Root
            interfaces:
              Standard:
                operations:
                  create:
                    implementation:
                      primary: TestEnviron
          unfurl_service_account:
            type: tosca:Root
            directives:
              - dependent # don't create instance
            interfaces:
              Standard:
                requirements:
                  - unfurl.relationships.ConnectsTo.GoogleCloudProject
                configure:
                  implementation: Template
                  inputs:
                    done:
                      result:
                        outputs:  # this should be become a quoted_dict so "token" isn't evaluated
                          app_credentials: '{"token":"XXXX"}'
                    resultTemplate:
                      eval:
                        if: "{{ outputs.app_credentials }}"
                        then:
                          - eval:
                              to_env:
                                GOOGLE_APPLICATION_CREDENTIALS:
                                  eval:
                                    tempfile:
                                      "{{ outputs.app_credentials }}"
                                    suffix: .json
                                GOOGLE_OAUTH_ACCESS_TOKEN: null
                              update_os_environ: true
  """


def test_validate_connection():
    """
    test that we can connect to AWS account
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        # override home so to avoid interferring with other tests
        result = runner.invoke(
            cli,
            [
                "--home",
                "./unfurl_home",
                "init",
                "--mono",
                "--skeleton=gcp",
            ],
        )
        # uncomment this to see output:
        # print("result.output", result.exit_code, result.output)
        assert not result.exception, "\n".join(
            traceback.format_exception(*result.exc_info)
        )

        with open("ensemble/ensemble.yaml", "w") as f:
            f.write(gcpTestManifest)

        # gcpTestManifest has an invalid GOOGLE_APPLICATION_CREDENTIALS so job should abort
        # without running testNode
        job, rendered, proceed = start_job(_opts={"startTime": 1})
        job.run(rendered)
        summary = job.json_summary()
        assert summary == {
  "job": {
    "id": "A01110000000",
    "status": "error",
    "total": 1,
    "ok": 0,
    "error": 1,
    "unknown": 0,
    "skipped": 0,
    "changed": 1
  },
  "outputs": {},
  "tasks": [
    {
      "status": "ok",
      "target": "primary_provider",
      "operation": "check",
      "template": "primary_provider",
      "type": "unfurl.relationships.ConnectsTo.GoogleCloudProject",
      "targetStatus": "error",
      "targetState": "error",
      "changed": True,
      "configurator": "unfurl.configurators.gcp.CheckGoogleCloudConnectionConfigurator",
      "priority": "critical",
      "reason": "check"
    }
  ]
}


def test_upgrade_connection():
    """
    test that we can connect to AWS account
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        # override home so to avoid interferring with other tests
        result = runner.invoke(
            cli,
            [
                "--home",
                "./unfurl_home",
                "init",
                "--mono",
                "--skeleton=gcp",
            ],
        )
        # uncomment this to see output:
        # print("result.output", result.exit_code, result.output)
        assert not result.exception, "\n".join(
            traceback.format_exception(*result.exc_info)
        )

        with open("ensemble/ensemble.yaml", "w") as f:
            f.write(gcpTestUpgradeConnectionManifest)

        # gcpTestUpgradeConnectionManifest only has GOOGLE_OAUTH_ACCESS_TOKEN
        # so try to upgrade by creating unfurl_service_account
        job, rendered, proceed = start_job(_opts={"startTime": 1})
        job.run(rendered)
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
        assert open(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]).read() == """{
  "token": "XXXX"
}"""
        assert "GOOGLE_OAUTH_ACCESS_TOKEN" not in os.environ, os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"]

        summary = job.json_summary()
        assert summary["job"] == {
          "id": "A01110000000",
          "status": "ok",
          "total": 3,
          "ok": 3,
          "error": 0,
          "unknown": 0,
          "skipped": 0,
          "changed": 3
        }


gcpEnvVarTypes = """\
  apiVersion: unfurl/v1alpha1
  kind: Ensemble
  +include:
    file: ensemble-template.yaml
    repository: spec
  changes: [] # set this so we save changes here instead of the job changelog files
  spec:
    service_template:
      topology_template:
        node_templates:
          testNode:
            type: tosca.nodes.Root
            interfaces:
              Standard:
                operations:
                  create:
                    implementation:
                      className: Template
                    inputs:
                      run:
                        eval:
                          get_ensemble_metadata:
                      resultTemplate:
                        readyState: ok
"""


def test_gcp_configurator():
    """
    test the unfurl.datatypes.EnvVar sets os.environ when default is get_env
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        # override home so to avoid interferring with other tests
        result = runner.invoke(
            cli,
            [
                "--home",
                "./unfurl_home",
                "init",
                "--mono",
                "--skeleton=gcp",
                "--use-environment=testing"
            ],
        )
        # uncomment this to see output:
        # print("result.output", result.exit_code, result.output)
        assert not result.exception, "\n".join(
            traceback.format_exception(*result.exc_info)
        )

        with open("ensemble/ensemble.yaml", "w") as f:
            f.write(gcpEnvVarTypes)

        # if both are present the gcp configurator will delete GOOGLE_OAUTH_ACCESS_TOKEN
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "cred.json"
        os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = "delete me"

        # set this so get_ensemble_metadata()["unfurlproject"] looks realistic:
        os.system("git remote add origin https://unfurl.cloud/my/dashboard.git")
        job, rendered, proceed = start_job(_opts={"startTime": 1})
        job.run(rendered)
        assert "GOOGLE_OAUTH_ACCESS_TOKEN" not in os.environ

        summary = job.json_summary()
        assert summary["job"] == {
          "id": "A01110000000",
          "status": "ok",
          "total": 2,
          "ok": 2,
          "error": 0,
          "unknown": 0,
          "skipped": 0,
          "changed": 2
        }

        # check that get_ensemble_metadata() evaluated correctly:
        result = list(job.workDone.values())[-1].result.result
        assert result.pop("revision")
        assert result == dict(deployment="ensemble", job="A01110000000",
                              environment="testing", unfurlproject='my/dashboard')
