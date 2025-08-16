import pytest
import json
import os
import traceback
import itertools
import pprint
from click.testing import CliRunner
from unfurl.eval import RefContext
from unfurl.projectpaths import _get_base_dir
from unfurl.job import Job
from string import Template
from unfurl.testing import init_project, run_job_cmd, run_cmd

SAVE_TMP = os.getenv("UNFURL_TEST_TMPDIR")

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
def test_plan(local_storage_status, compute_status, total, expected_errors, mocker):
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
        my_block_storage = job.rootResource.find_instance("my_block_storage")
        assert my_block_storage
        assert (
            my_block_storage.query(
                "{{ '.sources::local_storage' | eval(wantList=true) | count }}"
            )
            == 1
        )
        assert (
            my_block_storage.query(
                "{{ '.sources::local_storage[name=compute]' | eval(wantList=true) | count }}"
            )
            == 1
        )
        assert (
            my_block_storage.query(
                "{{ '.sources::local_storage[name=wrongname]' | eval(wantList=true) | count }}"
            )
            == 0
        )
        assert (
            my_block_storage.query(
                "{{ '.sources::local_storage[nonexistent_property]' | eval(wantList=true) | count }}"
            )
            == 0
        )

        relinstance = job.rootResource.find_instance("my_server").get_requirements(
            "local_storage"
        )[0]
        assert relinstance.required
        assert not relinstance.is_computed()

        render_dir = _get_base_dir(RefContext(relinstance), "tasks")
        assert render_dir.endswith("tasks/my_server~r~local_storage")

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

        spy = mocker.spy(Job, "_reorder_requests")
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
                "targetStatus": "pending" if compute_status == "error" else "ok",
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
                    "targetStatus": "pending"
                    if compute_status == "error"
                    else (local_storage_status or "ok"),
                    "targetState": None if compute_status == "error" else "configured",
                    "changed": False if compute_status == "error" else True,
                    "configurator": "unfurl.configurators.shell.ShellConfigurator",
                    "priority": "required",
                    "reason": "add",
                }
            ]
        blocked = 2 if compute_status == "error" else 0
        expected_summary = {
            "job": {
                "id": "A01110000000",
                "status": compute_status or "ok",
                "total": total,
                "ok": total - expected_errors,
                "error": expected_errors - blocked,
                "unknown": 0,
                "skipped": 0,
                "changed": 1 if compute_status == "error" else total,
            },
            "outputs": {},
            "tasks": expected,
        }
        if blocked:
            expected_summary["job"]["blocked"] = blocked

        # assert plan didn't reorder
        assert spy.call_count
        assert spy.call_args_list[0].args == spy.spy_return_list[0]
        assert spy.call_args_list[-1].args == spy.spy_return_list[-1]
        assert summary == expected_summary, summary
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


# all|mixed success? changed? check vs deploy
# commits / check caplog messages?
# check final disk layout

commit_manifest = """
apiVersion: unfurl/v1.0.0
kind: Ensemble
spec:
  service_template:
    node_types:
      Test:
        derived_from: tosca:Root
        interfaces:
          Standard:
            operations:
              configure:
                implementation: echo "test"
                inputs:
                  dryrun: true
                  done:
                    success: "{{ '.name' | eval  not in '::root::inputs::failed' | eval }}"
                    modified: "{{  '.name' | eval in '::root::inputs::changed' | eval }}"
                    # success: "{{ '.name' | eval not in TOPOLOGY.inputs.failed' | eval }}"
                    # modified: "{{ '.name'  | eval in TOPOLOGY.inputs.changed | eval }}"


    topology_template:
      inputs:
        failed:
          type: string
          default: none
        changed:
          type: string
          default: none
      node_templates:
        test1:
          type: Test
        test2:
          type: Test
"""


@pytest.fixture()
def runner(request):
    runner = CliRunner()
    with runner.isolated_filesystem(SAVE_TMP) as test_dir:
        if SAVE_TMP:
            print("running in", test_dir)
        init_project(
            runner,
            env=dict(UNFURL_HOME=""),
        )
        with open("ensemble-template.yaml", "w") as f:
            f.write(commit_manifest)
        run_cmd(runner, ["commit", "--no-edit"])
        runner.job_count = 0
        yield runner


def _deploy(cli_runner, command, expected_summary=None, check_files=None):
    args = f"-vvv {command}"
    result, job, summary = run_job_cmd(
        cli_runner, args, print_result=True, starttime=cli_runner.job_count
    )
    # os.system("git diff")
    # print(job.json_summary()["job"])
    files = dict((item[0], item[1:]) for item in os.walk("ensemble"))
    # pprint.pprint(files)
    if check_files:
        for expected_dir, expected in check_files.items():
            assert expected_dir in files
            assert files[expected_dir] == expected
    if expected_summary:
        summary = job.json_summary()["job"]
        jobid = summary.pop("id")
        expected_summary.pop("id", None)
        assert expected_summary == summary, jobid


def test_committing(runner):
    # changes updated when changed
    command = "deploy --commit"
    for args, summary, files in [
        # both failed, no changes
        (
            "--var input_failed 'test1 test2'",
            {
                "id": "A01110000000",
                "status": "error",
                "total": 2,
                "ok": 0,
                "error": 2,
                "unknown": 0,
                "skipped": 0,
                "changed": 0,
            },
            {
                "ensemble": (["planned", "jobs"], ["ensemble.yaml"]),
                "ensemble/jobs": (
                    [],
                    [
                        "job2020-01-01-01-00-00-A0111000.log",
                        "job2020-01-01-01-00-00-A0111000.yaml",
                    ],
                ),
            },
        ),
        # one failed with change, the other repaired
        (
            "--var input_failed test1 --var input_changed test1",
            {
                "id": "A01120000000",
                "status": "error",
                "total": 2,
                "ok": 1,
                "error": 1,
                "unknown": 0,
                "skipped": 0,
                "changed": 2,
            },
            {},
        ),
        # one failed, both changed
        (
            "--var input_failed test1 --var input_changed 'test1 test2'",
            {
                "id": "A01130000000",
                "status": "error",
                "total": 2,
                "ok": 1,
                "error": 1,
                "unknown": 0,
                "skipped": 0,
                "changed": 2,
            },
            {},
        ),
        # repair and reconfigure
        (
            "",
            {
                "id": "A01140000000",
                "status": "ok",
                "total": 2,
                "ok": 2,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 1,
            },
            {},
        ),
        # nothing to do
        (
            "",
            {
                "id": "A01150000000",
                "status": "ok",
                "total": 2,
                "ok": 0,
                "error": 0,
                "unknown": 0,
                "skipped": 2,
                "changed": 0,
            },
            {},
        ),
        # one failed and reconfigure
        (
            "--var input_failed test1",
            {
                "id": "A01160000000",
                "status": "error",
                "total": 2,
                "ok": 1,
                "error": 1,
                "unknown": 0,
                "skipped": 0,
                "changed": 0,
            },
            {
                "ensemble/changes": (
                    [],
                    [
                        "job2020-01-01-04-00-00-A0114000.yaml",
                        "job2020-01-01-06-00-00-A0116000.yaml",
                        "job2020-01-01-02-00-00-A0112000.yaml",
                        "job2020-01-01-03-00-00-A0113000.yaml",
                    ],
                ),
                "ensemble/jobs": (
                    [],
                    [
                        "job2020-01-01-04-00-00-A0114000.yaml",
                        "job2020-01-01-01-00-00-A0111000.log",
                        "job2020-01-01-06-00-00-A0116000.yaml",
                        "job2020-01-01-06-00-00-A0116000.log",
                        "job2020-01-01-02-00-00-A0112000.yaml",
                        "job2020-01-01-04-00-00-A0114000.log",
                        "job2020-01-01-02-00-00-A0112000.log",
                        "job2020-01-01-03-00-00-A0113000.yaml",
                        "job2020-01-01-01-00-00-A0111000.yaml",
                        "job2020-01-01-03-00-00-A0113000.log",
                        "job2020-01-01-05-00-00-A0115000.log",
                    ],
                ),
            },
        ),
    ]:  # Updating status for job A01110000000
        runner.job_count += 1
        _deploy(runner, command + " " + args, summary)


def test_planning(runner):
    plan_files = {
        "ensemble": (["planned"], ["ensemble.yaml"]),
    }
    for command, summary, files in [
        # both failed, no changes
        ("plan --var input_failed 'test1 test2'", {}, plan_files),
        (
            "deploy --commit --dryrun",
            {
                "id": "A01120000000",
                "status": "ok",
                "total": 2,
                "ok": 2,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 2,
            },
            plan_files,
        ),
        (
            "deploy --commit --dryrun --var input_failed 'test1 test2'",
            {
                "id": "A01130000000",
                "status": "error",
                "total": 2,
                "ok": 0,
                "error": 2,
                "unknown": 0,
                "skipped": 0,
                "changed": 0,
            },
            {
                "ensemble": (["planned"], ["ensemble.yaml"]),
                "ensemble/planned": (
                    ["tasks", "previous"],
                    [
                        "job2020-01-01-02-00-00-A0112000.ensemble.yaml",
                        "job2020-01-01-01-00-00-A0111000.log",
                        "job2020-01-01-02-00-00-A0112000.yaml",
                        "job2020-01-01-03-00-00-A0113000.ensemble.yaml",
                        "job2020-01-01-02-00-00-A0112000.log",
                        "job2020-01-01-03-00-00-A0113000.yaml",
                        "job2020-01-01-03-00-00-A0113000.log",
                    ],
                ),
                "ensemble/planned/tasks": (["test1", "test2"], []),
                "ensemble/planned/tasks/test1": (["configure"], []),
                "ensemble/planned/tasks/test1/configure": ([], ["rendered.sh"]),
                "ensemble/planned/tasks/test2": (["configure"], []),
                "ensemble/planned/tasks/test2/configure": ([], ["rendered.sh"]),
            },
        ),
    ]:
        runner.job_count += 1
        _deploy(runner, command, summary, files)
