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
                post_configure_source: echo "attach {{TARGET.size}} to {{SOURCE.name}} at {{ SELF.location }}"
                remove_target: echo "detach  {{TARGET.size}} to {{SOURCE.name}} at {{ SELF.location }}"

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
                create: echo "create my_server"
                delete: echo "delete my_server"

        my_block_storage:
          type: tosca.nodes.Storage.BlockStorage
          properties:
            name: blocky
            size: 10 GB
          interfaces:
            Standard:
              operations:
                create: echo "create my_block_storage"
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

        result, job, summary = run_job_cmd(runner, ["undeploy"], 2)
        # print(job.json_summary(True))
        # print("teardown", job.manifest.status_summary())
