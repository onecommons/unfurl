import pytest
import json
import os
import traceback
from click.testing import CliRunner
from .utils import init_project, run_job_cmd
from string import Template

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
                post_configure_target:
                  implementation: echo "attach target {{TARGET.volume_id}} to {{SOURCE.public_address}} at {{ SELF.location }}"
                # post_configure_source:
                #   implementation: echo "attach source {{SOURCE.public_address}} to {{TARGET.volume_id}} at {{ SELF.location }}"
                  inputs:
                    resultTemplate:
                      readyState: $local_storage_status
                # remove_target: echo "detach from target {{TARGET.name}}"
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
                      readyState: $compute_status
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
                      readyState: ok
                      attributes:
                        volume_id: DX34B
                        public_address:
                          eval: .sources::local_storage::public_address
                delete: echo "delete my_block_storage"
"""


@pytest.mark.parametrize(
    ["local_storage_status", "compute_status", "total", "expected_errors"],
    [
        # compute explicitly set to ok
        ("", "ok", 3, 0),
        # attaching failed
        ("error", "", 3, 1),
        # compute failed, so attachment doesn't run
        ("", "error", 3, 3),
    ],
)
def test_plan(local_storage_status, compute_status, total, expected_errors):
    runner = CliRunner()
    with runner.isolated_filesystem():
        init_project(
            runner,
            args=["init", "--mono"],
        )
        with open("ensemble/ensemble.yaml", "w") as f:
            f.write(
                Template(ENSEMBLE_WITH_RELATIONSHIPS).substitute(
                    local_storage_status=local_storage_status,
                    compute_status=compute_status,
                )
            )
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
        # print(job.manifest.status_summary()
        assert job.rootResource.find_instance("my_server").required
        relinstance = job.rootResource.find_instance("my_server").get_requirements(
            "local_storage"
        )[0]
        assert relinstance.required
        assert not relinstance.is_computed()

        planoutput = result.output.strip()
        assert planoutput
        # print(planoutput)
        plan = json.loads(planoutput)

        assert plan[0]["instance"] == "my_block_storage"
        assert plan[0]["plan"][0]["sequence"][0]["operation"] == "create"
        relation = plan[0]["plan"][0]["sequence"][1]
        assert relation["instance"] == "local_storage"
        # # relation is a nested task group:
        assert relation["plan"][0]["operation"] == "post_configure_target"

        assert plan[1]["instance"] == "my_server"
        assert plan[1]["plan"][0]["sequence"][0]["operation"] == "create"
        # relation = plan[1]["plan"][0]["sequence"][1]
        # assert relation["instance"] == "local_storage"
        # # relation is a nested task group:
        # assert relation["plan"][0]["sequence"][0]["operation"] == "post_configure_source"

        result, job, summary = run_job_cmd(runner, ["deploy"])
        # print(job.json_summary(True))
        expected = [
            {
                "status": "ok",
                "target": "my_server",
                "operation": "create",
                "template": "my_server",
                "type": "tosca.nodes.Compute",
                "targetStatus": compute_status or "ok",
                "targetState": "created",
                "changed": True,
                "configurator": "unfurl.configurators.shell.ShellConfigurator",
                "priority": "required",
                "reason": "add",
            },
            {
                "status": compute_status or "ok",
                "target": "my_block_storage",
                "operation": "create",
                "template": "my_block_storage",
                "type": "Volume",
                "targetStatus": "pending" if compute_status == "error" else  "ok",
                "targetState": "creating" if compute_status == "error" else "created",
                "changed": False if compute_status == "error" else True,
                "configurator": "unfurl.configurators.shell.ShellConfigurator",
                "priority": "required",
                "reason": "add",
            },
        ]
        if total > 2:
            expected += [
                {
                    "status": compute_status or "ok",
                    "target": "local_storage",
                    "operation": "post_configure_target",
                    "template": "local_storage",
                    "type": "VolumeAttach",
                    "targetStatus": "pending" if compute_status == "error" else (local_storage_status or "ok"),
                    "targetState": None if compute_status == "error" else "configured",
                    "changed": False if compute_status == "error" else True,
                    "configurator": "unfurl.configurators.shell.ShellConfigurator",
                    "priority": "required",
                    "reason": "add",
                }
            ]
        assert summary == {
            "job": {
                "id": "A01110000000",
                "status": compute_status or "ok",
                "total": total,
                "ok": total - expected_errors,
                "error": expected_errors,
                "unknown": 0,
                "skipped": 0,
                "changed": 1 if compute_status == "error" else total,
            },
            "outputs": {},
            "tasks": expected,
        }
        # print("deploy", job.manifest.status_summary())

        if compute_status != "ok":
            return  # only test undeploy once

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
                "changed": 3,
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
                    "reason": "undeploy",
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
                    "reason": "undeploy",
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
                    "reason": "undeploy",
                },
            ],
        }
