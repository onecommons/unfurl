import json
import logging
import os
import shutil
import time
import unittest
from io import StringIO
from types import SimpleNamespace

import pytest

from click.testing import CliRunner
from ruamel.yaml import YAML

from unfurl.job import JobOptions, Runner
from unfurl.localenv import LocalEnv
from unfurl.support import Status
from unfurl.configurators.terraform import (
    TerraformConfigurator,
    _locked_providers,
    _schema_cache_dir,
    _schema_cache_file,
    mark_sensitive,
)
from unfurl.util import sensitive, sensitive_str, wrap_sensitive_value
from unfurl.yamlloader import make_vault_lib, make_yaml

from .utils import lifecycle, MotoTest


def _inject_background_input(node):
    """Walk a parsed TOSCA ensemble and add ``background: true`` to every
    interface ``inputs:`` mapping so the TerraformConfigurator runs in
    background mode. Concurrent tasks sharing the same workfolder are
    serialized via the Job's workfolder-claim mechanism (see
    Job.claim_workfolder / TaskView.claim_work_folder)."""
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if k == "inputs" and isinstance(v, dict):
                v["background"] = True
            else:
                _inject_background_input(v)
    elif isinstance(node, list):
        for item in node:
            _inject_background_input(item)


@unittest.skipIf(
    "terraform" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set"
)
class TerraformTest(unittest.TestCase):
    def setUp(self):
        self.maxDiff = None

    def setup_filesystem(self, background=False):
        terraform_dir = os.environ["terraform_dir"] = os.path.join(
            os.path.dirname(__file__), "fixtures", "terraform"
        )

        path = os.path.join(os.path.dirname(__file__), "examples")
        src = os.path.join(path, "terraform-simple-ensemble.yaml")
        if background:
            yaml = YAML()
            with open(src) as f:
                data = yaml.load(f)
            _inject_background_input(data)
            with open("ensemble.yaml", "w") as f:
                yaml.dump(data, f)
        else:
            shutil.copy(src, "ensemble.yaml")

        # copy the terraform lock file so the configurator avoids calling terraform init
        # if .tox/.terraform already has the providers
        os.makedirs("tasks/terraform-node")
        lock_file = terraform_dir + "/.terraform.lock.hcl"
        if not os.getenv("CI") and os.path.exists(lock_file):
            shutil.copy(lock_file, "tasks/terraform-node/")
            os.makedirs("tasks/terraform-node-json")
            shutil.copy(lock_file, "tasks/terraform-node-json/")

    def test_terraform(self):
        self._run_test_terraform(background=False)

    def test_terraform_background(self):
        self._run_test_terraform(background=True)

    def _run_test_terraform(self, background):
        cli_runner = CliRunner()
        with cli_runner.isolated_filesystem():
            self.setup_filesystem(background=background)
            manifest = LocalEnv().get_manifest()
            runner = Runner(manifest)
            job = runner.run(JobOptions(startTime=1, check=True))  # deploy
            assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
            example = job.rootResource.find_resource("example")
            self.assertEqual(example.attributes["tag"], "Hello, test!")
            rtt = job.rootResource.find_resource("result-template-test")
            self.assertEqual(rtt.attributes["test"], "outputting result template test!")

            summary = job.json_summary()
            task_outputs = set(
                [
                    tuple(task.result.outputs.values())[0]
                    for task in job.workDone.values()
                    if task.result.outputs
                ]
            )
            assert task_outputs == {
                "outputting result template test!",
                "outputting test3!",
                "outputting test2!",
                "Hello, test!",
            }
            terraform_node = job.rootResource.find_resource("terraform-node")
            self.assertEqual(
                terraform_node.attributes["test_output"], "outputting test2!"
            )

            # print(job.summary())
            # print(job._planSummary())
            # print(json.dumps(summary, indent=2))
            self.assertEqual(
                {
                    "job": {
                        "id": "A01110000000",
                        "status": "ok",
                        "total": 8,
                        "ok": 8,
                        "error": 0,
                        "unknown": 0,
                        "skipped": 0,
                        "changed": 8,
                    },
                    "outputs": {},
                    "tasks": [
                        {
                            "status": "ok",
                            "target": "result-template-test",
                            "operation": "check",
                            "template": "result-template-test",
                            "type": "TerraformWithResultTemplate",
                            "targetStatus": "absent",
                            "targetState": "initial",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "check",
                        },
                        {
                            "status": "ok",
                            "target": "result-template-test",
                            "operation": "configure",
                            "template": "result-template-test",
                            "type": "TerraformWithResultTemplate",
                            "targetStatus": "ok",
                            "targetState": "configured",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "add",
                        },
                        {
                            "status": "ok",
                            "target": "terraform-node",
                            "operation": "check",
                            "template": "terraform-node",
                            "type": "GenericTerraformManagedResource",
                            "targetStatus": "absent",
                            "targetState": "initial",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "check",
                        },
                        {
                            "status": "ok",
                            "target": "terraform-node",
                            "operation": "configure",
                            "template": "terraform-node",
                            "type": "GenericTerraformManagedResource",
                            "targetStatus": "ok",
                            "targetState": "configured",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "add",
                        },
                        {
                            "status": "ok",
                            "target": "terraform-node-json",
                            "operation": "check",
                            "template": "terraform-node-json",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "absent",
                            "targetState": "initial",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "check",
                        },
                        {
                            "status": "ok",
                            "target": "terraform-node-json",
                            "operation": "configure",
                            "template": "terraform-node-json",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "ok",
                            "targetState": "configured",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "add",
                        },
                        {
                            "status": "ok",
                            "target": "example",
                            "operation": "check",
                            "template": "example",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "absent",  # nothing to modify... but still needs to run!
                            "targetState": "initial",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "check",
                        },
                        {
                            "status": "ok",
                            "target": "example",
                            "operation": "configure",
                            "template": "example",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "ok",
                            "targetState": "configured",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "add",
                        },
                    ],
                },
                summary,
            )

            runner2 = Runner(LocalEnv().get_manifest())
            job = runner2.run(JobOptions(workflow="undeploy", startTime=2))
            assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
            # print(job.summary())
            summary = job.json_summary()
            # print(json.dumps(summary, indent=2))
            self.assertEqual(
                {
                    "job": {
                        "id": "A01120000000",
                        "status": "ok",
                        "total": 4,
                        "ok": 4,
                        "error": 0,
                        "unknown": 0,
                        "skipped": 0,
                        "changed": 4,
                    },
                    "outputs": {},
                    "tasks": [
                        {
                            "status": "ok",
                            "target": "result-template-test",
                            "operation": "delete",
                            "template": "result-template-test",
                            "type": "TerraformWithResultTemplate",
                            "targetStatus": "absent",
                            "targetState": "deleted",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "undeploy",
                        },
                        {
                            "status": "ok",
                            "target": "terraform-node",
                            "operation": "delete",
                            "template": "terraform-node",
                            "type": "GenericTerraformManagedResource",
                            "targetStatus": "absent",
                            "targetState": "deleted",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "undeploy",
                        },
                        {
                            "status": "ok",
                            "target": "terraform-node-json",
                            "operation": "delete",
                            "template": "terraform-node-json",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "absent",
                            "targetState": "deleted",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "undeploy",
                        },
                        {
                            "status": "ok",
                            "target": "example",
                            "operation": "delete",
                            "template": "example",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "absent",
                            "targetState": "deleted",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "undeploy",
                        },
                    ],
                },
                summary,
            )

            runner2 = Runner(LocalEnv().get_manifest())
            job = runner2.run(JobOptions(workflow="check", startTime=2))
            assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
            summary = job.json_summary()
            # print(json.dumps(summary, indent=2))
            # print(job._json_plan_summary(True))
            self.assertEqual(
                {
                    "job": {
                        "id": "A01120GC0000",
                        "status": "ok",
                        "total": 4,
                        "ok": 4,
                        "error": 0,
                        "unknown": 0,
                        "skipped": 0,
                        "changed": 0,
                    },
                    "outputs": {},
                    "tasks": [
                        {
                            "status": "ok",
                            "target": "result-template-test",
                            "operation": "check",
                            "template": "result-template-test",
                            "type": "TerraformWithResultTemplate",
                            "targetStatus": "absent",
                            "targetState": "deleted",
                            "changed": False,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "check",
                        },
                        {
                            "status": "ok",
                            "target": "terraform-node",
                            "operation": "check",
                            "template": "terraform-node",
                            "type": "GenericTerraformManagedResource",
                            "targetStatus": "absent",
                            "targetState": "deleted",
                            "changed": False,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "check",
                        },
                        {
                            "status": "ok",
                            "target": "terraform-node-json",
                            "operation": "check",
                            "template": "terraform-node-json",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "absent",
                            "targetState": "deleted",
                            "changed": False,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "check",
                        },
                        {
                            "status": "ok",
                            "target": "example",
                            "operation": "check",
                            "template": "example",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "absent",
                            "targetState": "deleted",
                            "changed": False,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "check",
                        },
                    ],
                },
                summary,
            )
            plan_summary = job._json_plan_summary(include_rendered=False)
            # Background mode runs extra suspend/resume cycles (each commits
            # changes and advances the change counter), so the `managed`
            # change IDs aren't stable across modes. Compare the rest.
            for entry in plan_summary:
                entry.pop("managed", None)
            self.assertEqual(
                plan_summary,
                [
                    {
                        "instance": "result-template-test",
                        "plan": [{"operation": "check", "reason": "check"}],
                        "state": "NodeState.deleted",
                        "status": "Status.absent",
                    },
                    {
                        "instance": "terraform-node",
                        "status": "Status.absent",
                        "state": "NodeState.deleted",
                        "plan": [{"operation": "check", "reason": "check"}],
                    },
                    {
                        "instance": "terraform-node-json",
                        "status": "Status.absent",
                        "state": "NodeState.deleted",
                        "plan": [{"operation": "check", "reason": "check"}],
                    },
                    {
                        "instance": "example",
                        "status": "Status.absent",
                        "state": "NodeState.deleted",
                        "plan": [{"operation": "check", "reason": "check"}],
                    },
                ],
            )

    @unittest.skipIf(
        "slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set"
    )
    def test_lifecycle(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            self.setup_filesystem()
            for job in lifecycle(manifest=LocalEnv().get_manifest()):
                assert job.status == Status.ok


@unittest.skipIf(
    "terraform" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set"
)
@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
class TerraformMotoTest(MotoTest):
    def setUp(self):
        assert super().setUp(), "moto server didn't start"

        path = os.path.join(os.path.dirname(__file__), "examples")
        with open(os.path.join(path, "terraform-ensemble.yaml")) as f:
            self.ensemble_config = f.read()
        with open(os.path.join(path, "terraform-project-config.yaml")) as f:
            self.project_config = f.read()
        self.maxDiff = None

    def setup_filesystem(self):
        terraform_dir = os.environ["terraform_dir"] = os.path.join(
            os.path.dirname(__file__), "fixtures", "terraform"
        )

        # copy the terraform lock file so the configurator avoids calling terraform init
        # if .tox/.terraform already has the providers
        lock_file = terraform_dir + "/aws-terraform.lock.hcl"
        if not os.getenv("CI") and os.path.exists(lock_file):
            os.makedirs("tasks/example")
            shutil.copy(
                terraform_dir + "/aws-terraform.lock.hcl",
                "tasks/example/.terraform.lock.hcl",
            )

    def test_terraform(self):
        """
        test that runner figures out the proper tasks to run
        """
        runner = CliRunner()
        with runner.isolated_filesystem():
            self.setup_filesystem()
            with open("unfurl.yaml", "w") as f:
                f.write(self.project_config)

            with open("ensemble.yaml", "w") as f:
                f.write(self.ensemble_config)

            manifest = LocalEnv().get_manifest()
            assert manifest.manifest.vault and manifest.manifest.vault.secrets
            assert not manifest.lastJob

            job = Runner(manifest).run(JobOptions(startTime=1, check=True, verbose=-1))
            # print(job.out.getvalue())
            # print(job.jsonSummary(True))
            assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
            assert job.status == Status.ok, job.summary()
            example = job.rootResource.find_resource("example")
            self.assertEqual(example.attributes["availability_zone"], "us-east-1a")
            self.assertEqual(
                type(example.attributes["availability_zone"]), sensitive_str
            )
            self.assertEqual(example.attributes["tags"], {"Name": "test"})
            # print("deploy")
            # print(job.jsonSummary(True))
            self.assertEqual(
                job.json_summary(),
                {
                    "job": {
                        "id": "A01110000000",
                        "status": "ok",
                        "total": 2,
                        "ok": 2,
                        "error": 0,
                        "unknown": 0,
                        "skipped": 0,
                        "changed": 2,
                    },
                    "outputs": {},
                    "tasks": [
                        {
                            "status": "ok",
                            "target": "example",
                            "targetState": "initial",
                            "operation": "check",
                            "template": "example",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "absent",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "check",
                        },
                        {
                            "status": "ok",
                            "target": "example",
                            "operation": "configure",
                            "template": "example",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "ok",
                            "targetState": "configured",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "add",
                        },
                    ],
                },
            )

            # reload and check
            manifest2 = LocalEnv().get_manifest()
            assert manifest2.lastJob
            manifest2.rootResource.find_resource("example")
            self.assertEqual(
                type(example.attributes["availability_zone"]), sensitive_str
            )
            job = Runner(manifest2).run(
                JobOptions(workflow="check", verbose=-1, startTime=2)
            )
            assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
            # print("check")
            # print(job.jsonSummary(True))
            self.assertEqual(
                job.json_summary(),
                {
                    "job": {
                        "id": "A01120000000",
                        "status": "ok",
                        "total": 1,
                        "ok": 1,
                        "error": 0,
                        "unknown": 0,
                        "skipped": 0,
                        "changed": 0,
                    },
                    "outputs": {},
                    "tasks": [
                        {
                            "status": "ok",
                            "target": "example",
                            "operation": "check",
                            "template": "example",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "ok",
                            "targetState": "configured",
                            "changed": False,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "check",
                        }
                    ],
                },
            )
            assert job.status == Status.ok, job.summary()

            # reload and undeploy:
            manifest3 = LocalEnv().get_manifest()
            assert manifest3.lastJob
            example = manifest3.rootResource.find_resource("example")
            assert example
            self.assertEqual(
                type(example.attributes["availability_zone"]), sensitive_str
            )
            job = Runner(manifest3).run(
                JobOptions(workflow="undeploy", verbose=2, startTime=3)
            )
            assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
            assert job.status == Status.ok, job.summary()
            self.assertEqual(
                job.json_summary(),
                {
                    "job": {
                        "id": "A01130000000",
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
                            "target": "example",
                            "operation": "delete",
                            "template": "example",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "absent",
                            "targetState": "deleted",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "undeploy",
                        }
                    ],
                },
            )


class _SensitiveTask:
    sensitive = staticmethod(wrap_sensitive_value)
    logger = logging.getLogger("unfurl.test_terraform")

    def __init__(self, vault_password="vaultpass"):
        self._manifest = SimpleNamespace(yaml=make_yaml(make_vault_lib(vault_password)))


def _dump_state(task, state):
    out = StringIO()
    task._manifest.yaml.dump(state, out)
    return out.getvalue()


def _access_key_schema():
    # aws_iam_access_key's schema, trimmed, plus a nested block
    return {
        "resource_schemas": {
            "aws_iam_access_key": {
                "version": 0,
                "block": {
                    "attributes": {
                        "id": {"type": "string", "computed": True},
                        "user": {"type": "string", "required": True},
                        "secret": {
                            "type": "string",
                            "computed": True,
                            "sensitive": True,
                        },
                    },
                    "block_types": {
                        "rotation": {
                            "nesting_mode": "list",
                            "block": {
                                "attributes": {
                                    "token": {"type": "string", "sensitive": True},
                                    "days": {"type": "number"},
                                }
                            },
                        }
                    },
                },
            }
        }
    }


def _access_key_state(provider):
    return {
        "version": 4,
        "outputs": {},
        "resources": [
            {
                "mode": "managed",
                "type": "aws_iam_access_key",
                "name": "key",
                "provider": provider,
                "instances": [
                    {
                        "schema_version": 0,
                        "attributes": {
                            "id": "AKIAEXAMPLE",
                            "user": "deployer",
                            "secret": "s3cr3t",
                            "rotation": [{"token": "t0ken", "days": 30}],
                        },
                        "sensitive_attributes": [],
                    }
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    "provider,schema_key",
    [
        (
            'provider["registry.terraform.io/hashicorp/aws"]',
            "registry.terraform.io/hashicorp/aws",
        ),
        (
            'provider["registry.terraform.io/hashicorp/aws"].west',
            "registry.terraform.io/hashicorp/aws",
        ),
        (
            'module.main.provider["registry.terraform.io/hashicorp/aws"]',
            "registry.terraform.io/hashicorp/aws",
        ),
        (
            'provider["registry.opentofu.org/hashicorp/aws"]',
            "registry.opentofu.org/hashicorp/aws",
        ),
        # schema cached by terraform, state written by tofu
        (
            'provider["registry.opentofu.org/hashicorp/aws"].west',
            "registry.terraform.io/hashicorp/aws",
        ),
    ],
)
def test_mark_sensitive_uses_provider_schema(provider, schema_key):
    state = mark_sensitive(
        {schema_key: _access_key_schema()},
        _access_key_state(provider),
        _SensitiveTask(),
        TerraformConfigurator.sensitive_names,
    )
    attributes = state["resources"][0]["instances"][0]["attributes"]
    assert isinstance(attributes["secret"], sensitive)
    assert attributes["secret"] == "s3cr3t"
    assert isinstance(attributes["rotation"][0]["token"], sensitive)
    for name in ("id", "user"):
        assert not isinstance(attributes[name], sensitive)
    assert not isinstance(attributes["rotation"][0]["days"], sensitive)


def test_mark_sensitive_saves_encrypted_state():
    task = _SensitiveTask()
    state = mark_sensitive(
        {"registry.terraform.io/hashicorp/aws": _access_key_schema()},
        _access_key_state('provider["registry.terraform.io/hashicorp/aws"]'),
        task,
    )
    saved = _dump_state(task, state)
    assert "s3cr3t" not in saved
    assert "!vault" in saved


def test_mark_sensitive_skips_resources_without_vault():
    task = _SensitiveTask(vault_password="")
    state = _access_key_state('provider["registry.terraform.io/hashicorp/aws"]')
    state["outputs"] = {"secret": {"value": "out-s3cr3t", "sensitive": True}}
    state = mark_sensitive(
        {"registry.terraform.io/hashicorp/aws": _access_key_schema()},
        state,
        task,
        TerraformConfigurator.sensitive_names,
    )
    attributes = state["resources"][0]["instances"][0]["attributes"]
    assert not isinstance(attributes["secret"], sensitive)
    assert not isinstance(attributes["rotation"][0]["token"], sensitive)
    # outputs are still marked, as before
    assert isinstance(state["outputs"]["secret"]["value"], sensitive)
    saved = _dump_state(task, state["resources"])
    assert "s3cr3t" in saved
    assert "t0ken" in saved
    assert sensitive_str.redacted_str not in saved


def test_mark_sensitive_ignores_other_registries():
    state = mark_sensitive(
        {"registry.terraform.io/hashicorp/aws": _access_key_schema()},
        _access_key_state('provider["example.com/hashicorp/aws"]'),
        _SensitiveTask(),
    )
    attributes = state["resources"][0]["instances"][0]["attributes"]
    assert not isinstance(attributes["secret"], sensitive)



_NULL = "registry.terraform.io/hashicorp/null"
_AWS = "registry.terraform.io/hashicorp/aws"


class _SchemaTask:
    def __init__(self, stateLocation="artifacts"):
        self.inputs = {"stateLocation": stateLocation}
        self.logger = logging.getLogger("unfurl.test_terraform")


class _SchemaFolder:
    def __init__(self, cwd):
        self.cwd = str(cwd)

    def get_current_path(self, name, mkdir=True):
        return os.path.join(self.cwd, name)


class _StubSchemaConfigurator(TerraformConfigurator):
    def __init__(self, fetched):
        self.fetched = fetched
        self.fetches = 0

    def _run_init(self, task, terraform, folder, env):
        return True

    def _fetch_provider_schemas(self, task, terraform, folder, env):
        self.fetches += 1
        return self.fetched


def _write_lock_file(path, locked):
    # the format terraform writes
    path.write_text(
        "# This file is maintained automatically by \"terraform init\".\n\n"
        + "\n".join(
            f'provider "{address}" {{\n'
            f'  version     = "{version}"\n'
            f'  constraints = ">= 1.0.0"\n'
            "  hashes = [\n"
            '    "h1:abc=",\n'
            "  ]\n"
            "}\n"
            for address, version in locked.items()
        )
    )


def _cache_dir(tmp_path):
    return _schema_cache_dir(str(tmp_path / "cache"), ["terraform"])


def _ensure_schema(tmp_path, fetched, locked, stateLocation="artifacts"):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    _write_lock_file(work / ".terraform.lock.hcl", locked)
    configurator = _StubSchemaConfigurator(fetched)
    schemas = configurator._ensure_provider_schema(
        _SchemaTask(stateLocation),
        ["terraform"],
        _SchemaFolder(work),
        {},
        cache_dir=_cache_dir(tmp_path),
    )
    return configurator, schemas


def _cache_path(tmp_path, address, version):
    return _schema_cache_file(_cache_dir(tmp_path), address, version)


def _cached(tmp_path, address, version):
    with open(_cache_path(tmp_path, address, version)) as f:
        return json.load(f)


def _write_cache(tmp_path, address, version, contents):
    path = _cache_path(tmp_path, address, version)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(contents)


def test_schema_cache_names_per_command_and_version():
    assert _schema_cache_dir("/c", ["terraform"]) == "/c/providers-schema-terraform"
    assert _schema_cache_dir("/c", ["/usr/local/bin/tofu", "-chdir=x"]) == (
        "/c/providers-schema-tofu"
    )
    assert _schema_cache_file("/c/d", _AWS, "5.1.0") == (
        "/c/d/registry.terraform.io_hashicorp_aws@5.1.0.json"
    )


def test_locked_providers_reads_versions(tmp_path):
    lock_file = tmp_path / ".terraform.lock.hcl"
    _write_lock_file(lock_file, {_NULL: "3.3.2", _AWS: "5.1.0"})
    assert _locked_providers(str(lock_file)) == {_NULL: "3.3.2", _AWS: "5.1.0"}
    assert _locked_providers(str(tmp_path / "missing.hcl")) == {}


def test_schema_cache_skips_empty_schema(tmp_path):
    configurator, schemas = _ensure_schema(tmp_path, {}, {_NULL: "3.3.2"})
    assert schemas == {}
    assert configurator.fetches == 1
    assert not os.path.exists(_cache_path(tmp_path, _NULL, "3.3.2"))


def test_schema_cache_refetches_empty_cache(tmp_path):
    _write_cache(tmp_path, _NULL, "3.3.2", "{}")
    configurator, schemas = _ensure_schema(
        tmp_path, {_NULL: {"v": 1}}, {_NULL: "3.3.2"}
    )
    assert configurator.fetches == 1
    assert schemas == {_NULL: {"v": 1}}
    assert _cached(tmp_path, _NULL, "3.3.2") == {"v": 1}


def test_schema_cache_fetches_missing_provider(tmp_path):
    _write_cache(tmp_path, _NULL, "3.3.2", json.dumps({"v": 1}))
    configurator, schemas = _ensure_schema(
        tmp_path, {_NULL: {"v": 1}, _AWS: {"v": 2}}, {_NULL: "3.3.2", _AWS: "5.1.0"}
    )
    assert configurator.fetches == 1
    assert schemas == {_NULL: {"v": 1}, _AWS: {"v": 2}}
    assert _cached(tmp_path, _AWS, "5.1.0") == {"v": 2}

    # now that every locked provider is cached, it isn't fetched again
    configurator, schemas = _ensure_schema(
        tmp_path, {}, {_NULL: "3.3.2", _AWS: "5.1.0"}
    )
    assert configurator.fetches == 0
    assert schemas == {_NULL: {"v": 1}, _AWS: {"v": 2}}


def test_schema_cache_keeps_versions_apart(tmp_path):
    _write_cache(tmp_path, _AWS, "4.67.0", json.dumps({"v": 4}))
    # a missing version is fetched even though the provider is cached
    configurator, schemas = _ensure_schema(tmp_path, {_AWS: {"v": 5}}, {_AWS: "5.1.0"})
    assert configurator.fetches == 1
    assert schemas == {_AWS: {"v": 5}}
    assert _cached(tmp_path, _AWS, "4.67.0") == {"v": 4}
    assert _cached(tmp_path, _AWS, "5.1.0") == {"v": 5}

    # each deployment gets the schema of the version it locked
    for version, expected in [("4.67.0", {"v": 4}), ("5.1.0", {"v": 5})]:
        configurator, schemas = _ensure_schema(tmp_path, {}, {_AWS: version})
        assert configurator.fetches == 0
        assert schemas == {_AWS: expected}


def test_schema_cache_ignores_corrupt_file(tmp_path):
    _write_cache(tmp_path, _NULL, "3.3.2", '{"resource_sch')
    configurator, schemas = _ensure_schema(
        tmp_path, {_NULL: {"v": 1}}, {_NULL: "3.3.2"}
    )
    assert configurator.fetches == 1
    assert _cached(tmp_path, _NULL, "3.3.2") == {"v": 1}


@pytest.mark.parametrize("stateLocation", ["secrets", "remote"])
def test_schema_cache_unused_without_cleartext_state(tmp_path, stateLocation):
    _write_cache(tmp_path, _NULL, "3.3.2", json.dumps({"v": 1}))
    configurator, schemas = _ensure_schema(
        tmp_path, {_AWS: {"v": 2}}, {_NULL: "3.3.2", _AWS: "5.1.0"}, stateLocation
    )
    assert configurator.fetches == 0
    assert schemas == {}
    assert not os.path.exists(_cache_path(tmp_path, _AWS, "5.1.0"))
