import pytest
import json
import os
from click.testing import CliRunner
from .utils import init_project, run_job_cmd
from pathlib import Path

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
    runner = CliRunner()
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
        print(result.output)
        # print(job.manifest.status_summary())
        plan = json.loads(result.output.strip())
        # print(plan)
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
