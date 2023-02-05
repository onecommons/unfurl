import pytest
import json
import os
import traceback
from click.testing import CliRunner
from unfurl.__main__ import cli, _latestJobs
from unfurl.localenv import LocalEnv
from unfurl.job import start_job
from unfurl.yamlmanifest import YamlManifest
from .utils import init_project, run_job_cmd
from pathlib import Path
from unfurl.support import Status
from unfurl.configurator import Configurator

ENSEMBLE_WITH_RELATIONSHIPS = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    relationship_types:
      VolumeAttach:
          derived_from: tosca.relationships.AttachesTo
          properties:
            location:
              default: /my_mount_point
          interfaces:
            Configure:
              operations:
                # post_configure_target: echo "attach target {{TARGET.volume_id}} to {{SOURCE.public_address}} at {{ SELF.location }}"
                post_configure_source: echo "attach source {{SOURCE.public_address}} to {{TARGET.volume_id}} at {{ SELF.location }}"
                remove_target: echo "detach from target {{TARGET.name}}"
                remove_source: echo "detach from source {{TARGET.name}}"

    node_types:
      Volume:
        derived_from: tosca.nodes.Storage.BlockStorage
        attributes:
          public_address:
            type: string
            default:
              eval: .sources::local_storage::public_address

    topology_template:
      node_templates:
        my_server:
          type: tosca.nodes.Compute
          properties:
            name: compute
          requirements:
            - local_storage:
                node: my_block_storage
                relationship:
                  type: VolumeAttach
          interfaces:
            Standard:
              operations:
                create:
                  implementation: echo "create my_server"
                  inputs:
                    resultTemplate:
                      readyState: ok
                      attributes:
                        public_address:  10.10.10.1
                delete: echo "delete my_server"

        my_block_storage:
          type: Volume
          properties:
            name: blocky
            size: 10 GB
          interfaces:
            Standard:
              operations:
                create:
                  implementation: echo "create my_block_storage at {{ SELF.public_address }}"
                  inputs:
                    resultTemplate:
                      attributes:
                        volume_id: DX34B
                        public_address:
                          eval: .sources::local_storage::public_address
                delete: echo "delete my_block_storage"
"""


def test_plan():
    runner = CliRunner()
    with runner.isolated_filesystem():
        init_project(
            runner,
            args=["init", "--mono"],
        )
        with open("ensemble/ensemble.yaml", "w") as f:
            f.write(ENSEMBLE_WITH_RELATIONSHIPS)
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
        # print(planoutput)
        plan = json.loads(planoutput)
    
        assert plan[0]["instance"] == "my_block_storage"
        assert plan[0]["plan"][0]["sequence"][0]["operation"] == "create"

        assert plan[1]["instance"] == "my_server"
        assert plan[1]["plan"][0]["sequence"][0]["operation"] == "create"
        relation = plan[1]["plan"][0]["sequence"][1]
        # print(relation)
        assert relation["instance"] == "local_storage"
        assert relation["plan"][0]["operation"] == "post_configure_source"

        result, job, summary = run_job_cmd(runner, ["deploy"])
        # print(job.json_summary(True))
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
        # print("deploy", job.manifest.status_summary())

        result, job, summary = run_job_cmd(runner, ["undeploy"], 2)
        # print(job.json_summary(True))
        # print("teardown", job.manifest.status_summary())
        assert summary == {
            "job": {
              "id": "A01120000000",
              "status": "ok",
              "total": 3,
              "ok": 3,
              "error": 0,
              "unknown": 0,
              "skipped": 0,
              "changed": 3
            },
            "outputs": {},
            "tasks": [
              {
                "status": "ok",
                "target": "local_storage",
                "operation": "remove_source",
                "template": "local_storage",
                "type": "VolumeAttach",
                "targetStatus": "absent",
                "targetState": "configured",
                "changed": True,
                "configurator": "unfurl.configurators.shell.ShellConfigurator",
                "priority": "required",
                "reason": "undeploy"
              },
              {
                "status": "ok",
                "target": "my_server",
                "operation": "delete",
                "template": "my_server",
                "type": "tosca.nodes.Compute",
                "targetStatus": "absent",
                "targetState": "deleted",
                "changed": True,
                "configurator": "unfurl.configurators.shell.ShellConfigurator",
                "priority": "required",
                "reason": "undeploy"
              },
              {
                "status": "ok",
                "target": "my_block_storage",
                "operation": "delete",
                "template": "my_block_storage",
                "type": "Volume",
                "targetStatus": "absent",
                "targetState": "deleted",
                "changed": True,
                "configurator": "unfurl.configurators.shell.ShellConfigurator",
                "priority": "required",
                "reason": "undeploy"
              }
            ]
          }