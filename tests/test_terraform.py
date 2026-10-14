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
import unfurl.configurators.terraform as terraform_module
from unfurl.configurators.terraform import (
    TerraformConfigurator,
    _check_tf_cli,
    _default_plugin_cache_dir,
    _locked_providers,
    _needs_init,
    _needs_migration,
    _schema_cache_dir,
    _schema_cache_file,
    _tf_cli_version,
    mark_sensitive,
)
from unfurl.util import UnfurlTaskError
from unfurl.yamlmanifest import YamlManifest
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


_TF114_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "terraform", "tf114")
_MIGRATION_ENSEMBLE = os.path.join(
    os.path.dirname(__file__), "examples", "terraform-migration-ensemble.yaml"
)
_MIGRATION_WARNING = "This run migrates migrate-node from Terraform to OpenTofu"
_REMOTE_UNDO = "Unfurl doesn't hold this state, so it can't be reverted with git"

# prints the version line of the tool it stands in for, and succeeds at every other command
_CLI_STUB = """#!/bin/sh
if [ "$1" = version ]; then
  echo "%s"
  echo "on linux_amd64"
fi
"""


def _write_cli_stub(directory, name, version_line):
    directory.mkdir(exist_ok=True)
    stub = directory / name
    stub.write_text(_CLI_STUB % version_line)
    stub.chmod(0o755)
    return stub


@pytest.fixture
def fresh_cli_state(monkeypatch):
    monkeypatch.setattr(terraform_module, "_tf_cli_versions", {})
    monkeypatch.setattr(terraform_module, "_tf_cli_warned", set())
    monkeypatch.setattr(terraform_module, "_migration_warned", set())


class _CliTask:
    def __init__(self, path):
        self.environ = {"PATH": str(path)}
        self.logger = logging.getLogger("unfurl.test_terraform")
        self._errors = []


def _check(tmp_path, name, version_line):
    _write_cli_stub(tmp_path / "bin", name, version_line)
    return _check_tf_cli(_CliTask(tmp_path / "bin"), name)


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_check_tf_cli_rejects_old_tofu(tmp_path, fresh_cli_state):
    with pytest.raises(UnfurlTaskError, match="OpenTofu 1.5.0 .* is too old"):
        _check(tmp_path, "tofu", "OpenTofu v1.5.0")


@pytest.mark.parametrize(
    "version,warns,lock_clause",
    [
        ("1.6.0", True, True),
        ("1.9.1", True, True),
        ("1.10.0", True, False),
        ("1.11.9", True, False),
        ("1.12.0", False, False),
        ("1.13.1", False, False),
    ],
)
def test_check_tf_cli_warns_before_1_12(
    tmp_path, fresh_cli_state, caplog, version, warns, lock_clause
):
    with caplog.at_level(logging.WARNING):
        cli = _check(tmp_path, "tofu", f"OpenTofu v{version}")
    assert cli == ("tofu", tuple(int(n) for n in version.split(".")))
    warnings = _warnings(caplog)
    assert len(warnings) == int(warns), warnings
    if warns:
        assert "is older than 1.12" in warnings[0]
        assert ("carry locked hashicorp/* provider versions" in warnings[0]) == (
            lock_clause
        )


def test_check_tf_cli_warns_once_per_path(tmp_path, fresh_cli_state, caplog):
    with caplog.at_level(logging.WARNING):
        _check(tmp_path, "tofu", "OpenTofu v1.6.0")
        _check(tmp_path, "tofu", "OpenTofu v1.6.0")
    assert len(_warnings(caplog)) == 1


def test_check_tf_cli_terraform_is_unsupported(tmp_path, fresh_cli_state, caplog):
    with caplog.at_level(logging.WARNING):
        cli = _check(tmp_path, "terraform", "Terraform v1.1.4")
    assert cli == ("terraform", (1, 1, 4))
    (warning,) = _warnings(caplog)
    assert "Running HashiCorp Terraform 1.1.4" in warning
    assert "Unfurl supports only OpenTofu" in warning


def test_check_tf_cli_rejects_old_terraform(tmp_path, fresh_cli_state):
    with pytest.raises(UnfurlTaskError, match="Terraform 0.15.5 .* is too old"):
        _check(tmp_path, "terraform", "Terraform v0.15.5")


def test_check_tf_cli_warns_on_unknown_output(tmp_path, fresh_cli_state, caplog):
    with caplog.at_level(logging.WARNING):
        assert _check(tmp_path, "wrapper", "wrapper 2.0") is None
    (warning,) = _warnings(caplog)
    assert "Could not determine the version" in warning


def test_check_tf_cli_uses_task_path(tmp_path, fresh_cli_state, monkeypatch):
    _write_cli_stub(tmp_path / "bin", "tofu", "OpenTofu v1.13.1")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    # not on the task's PATH, so not found, even though it's on os.environ's
    assert _check_tf_cli(_CliTask(tmp_path / "empty"), "tofu") is None


def test_tf_cli_version_probes_once_per_path(tmp_path, fresh_cli_state):
    stub = _write_cli_stub(tmp_path / "bin", "tofu", "OpenTofu v1.13.1")
    assert _tf_cli_version(str(stub), {}) == ("tofu", (1, 13, 1))
    stub.write_text(_CLI_STUB % "OpenTofu v1.6.0")
    assert _tf_cli_version(str(stub), {}) == ("tofu", (1, 13, 1))


def test_needs_init_matches_both_hints():
    assert _needs_init('Error: Module not installed\n\nRun "tofu init" to install.')
    assert _needs_init('Please run "terraform init".')
    assert not _needs_init("Error: Invalid reference")


@pytest.fixture
def rc_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _rc(path, cache_dir=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'plugin_cache_dir = "{cache_dir}"\n' if cache_dir else "\n")


_TOFU_1_6 = ("tofu", (1, 6, 3))
_TOFU_1_7 = ("tofu", (1, 7, 0))
_TERRAFORM = ("terraform", (1, 5, 7))


def test_plugin_cache_dir_prefers_tofurc(rc_home):
    _rc(rc_home / ".tofurc", "/tofurc-cache")
    _rc(rc_home / ".terraformrc", "/terraformrc-cache")
    assert _default_plugin_cache_dir({}, _TOFU_1_7) == "/tofurc-cache"
    # terraform doesn't read .tofurc
    assert _default_plugin_cache_dir({}, _TERRAFORM) == "/terraformrc-cache"


def test_plugin_cache_dir_falls_back_to_terraformrc(rc_home):
    _rc(rc_home / ".terraformrc", "/terraformrc-cache")
    assert _default_plugin_cache_dir({}, _TOFU_1_7) == "/terraformrc-cache"


def test_plugin_cache_dir_reads_xdg_tofurc_from_1_7(rc_home, tmp_path):
    xdg = tmp_path / "xdg"
    _rc(xdg / "opentofu" / "tofurc", "/xdg-cache")
    env = {"XDG_CONFIG_HOME": str(xdg)}
    assert _default_plugin_cache_dir(env, _TOFU_1_7) == "/xdg-cache"
    default = str(rc_home / ".terraform.d" / "plugin-cache")
    assert _default_plugin_cache_dir(env, _TOFU_1_6) == default
    assert _default_plugin_cache_dir(env, _TERRAFORM) == default
    # a home config file takes precedence
    _rc(rc_home / ".terraformrc", "/terraformrc-cache")
    assert _default_plugin_cache_dir(env, _TOFU_1_7) == "/terraformrc-cache"


def test_plugin_cache_dir_honors_tf_cli_config_file(rc_home, tmp_path):
    _rc(rc_home / ".tofurc", "/tofurc-cache")
    _rc(tmp_path / "custom.tfrc", "/custom-cache")
    env = {"TF_CLI_CONFIG_FILE": str(tmp_path / "custom.tfrc")}
    assert _default_plugin_cache_dir(env, _TOFU_1_7) == "/custom-cache"


def test_plugin_cache_dir_default_when_config_has_none(rc_home):
    # the first config file found is the one used, even without plugin_cache_dir
    _rc(rc_home / ".tofurc")
    _rc(rc_home / ".terraformrc", "/terraformrc-cache")
    assert _default_plugin_cache_dir({}, _TOFU_1_7) == str(
        rc_home / ".terraform.d" / "plugin-cache"
    )


def test_plugin_cache_dir_ignores_xdg_default_location(rc_home):
    # tofu only looks in $XDG_CONFIG_HOME when it's set, not in ~/.config
    _rc(rc_home / ".config" / "opentofu" / "tofurc", "/xdg-cache")
    assert _default_plugin_cache_dir({}, _TOFU_1_7) == str(
        rc_home / ".terraform.d" / "plugin-cache"
    )


def test_plugin_cache_dir_reads_config_dir(rc_home):
    _rc(rc_home / ".terraform.d" / "b.tfrc", "/b-cache")
    _rc(rc_home / ".terraform.d" / "a.tfrc")
    (rc_home / ".terraform.d" / "c.txt").write_text('plugin_cache_dir = "/c-cache"\n')
    for cli in (_TOFU_1_7, _TERRAFORM):
        assert _default_plugin_cache_dir({}, cli) == "/b-cache"
    (rc_home / ".terraform.d" / "a.tfrc.json").write_text(
        json.dumps({"plugin_cache_dir": "/json-cache"})
    )
    assert _default_plugin_cache_dir({}, _TOFU_1_7) == "/json-cache"
    # the main config file takes precedence
    _rc(rc_home / ".tofurc", "/tofurc-cache")
    assert _default_plugin_cache_dir({}, _TOFU_1_7) == "/tofurc-cache"
    # but isn't read with an explicit config file
    env = {"TF_CLI_CONFIG_FILE": str(rc_home / "missing.tfrc")}
    assert _default_plugin_cache_dir(env, _TOFU_1_7) == str(
        rc_home / ".terraform.d" / "plugin-cache"
    )


def test_plugin_cache_dir_reads_xdg_config_dir(rc_home, tmp_path):
    xdg = tmp_path / "xdg"
    _rc(xdg / "opentofu" / "cache.tfrc", "/xdg-dir-cache")
    env = {"XDG_CONFIG_HOME": str(xdg)}
    assert _default_plugin_cache_dir(env, _TOFU_1_7) == "/xdg-dir-cache"
    # ~/.terraform.d takes precedence when it exists
    (rc_home / ".terraform.d").mkdir()
    assert _default_plugin_cache_dir(env, _TOFU_1_7) == str(
        rc_home / ".terraform.d" / "plugin-cache"
    )


def test_plugin_cache_dir_empty_opts_out(rc_home):
    _rc(rc_home / ".tofurc")
    (rc_home / ".tofurc").write_text('plugin_cache_dir = ""\n')
    assert _default_plugin_cache_dir({}, _TOFU_1_7) is None


def test_plugin_cache_dir_uses_env_home(rc_home, tmp_path):
    other = tmp_path / "other"
    _rc(other / ".tofurc", "/other-cache")
    assert _default_plugin_cache_dir({"HOME": str(other)}, _TOFU_1_7) == "/other-cache"


def _migration_template():
    os.environ["terraform_dir"] = os.path.join(
        os.path.dirname(__file__), "fixtures", "terraform"
    )
    with open(_MIGRATION_ENSEMBLE) as f:
        manifest = YamlManifest(f.read())
    return manifest.rootResource, manifest.tosca.get_template("migrate-node")


def _planned_artifacts(kw):
    _, template = _migration_template()
    kw = TerraformConfigurator.set_config_spec_args(kw, template)
    return [artifact.name for artifact in kw.get("dependencies", [])]


def test_plan_finds_tofu_on_path(tmp_path, monkeypatch):
    _write_cli_stub(tmp_path / "bin", "tofu", "OpenTofu v1.13.1")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert _planned_artifacts({"inputs": {}}) == []


def test_plan_installs_opentofu_without_tofu(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert _planned_artifacts({"inputs": {}}) == ["configurator-artifacts--opentofu"]


def test_plan_installs_nothing_for_command_input(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert _planned_artifacts({"inputs": {"command": "terraform"}}) == []


_TF_NULL_STATE = {
    "resources": [{"provider": 'provider["registry.terraform.io/hashicorp/null"]'}]
}
_TOFU_NULL_STATE = {
    "resources": [{"provider": 'provider["registry.opentofu.org/hashicorp/null"]'}]
}


def test_needs_migration(tmp_path):
    tf_lock = tmp_path / "tf.lock.hcl"
    _write_lock_file(tf_lock, {_NULL: "3.3.2"})
    tofu_lock = tmp_path / "tofu.lock.hcl"
    _write_lock_file(tofu_lock, {"registry.opentofu.org/hashicorp/null": "3.3.2"})
    missing = str(tmp_path / "missing.hcl")
    assert _needs_migration(_TF_NULL_STATE, missing)
    # the state is authoritative over the lock
    assert not _needs_migration(_TOFU_NULL_STATE, str(tf_lock))
    # without resources in the state, the lock is checked
    assert _needs_migration(None, str(tf_lock))
    assert _needs_migration({"resources": []}, str(tf_lock))
    assert not _needs_migration(None, str(tofu_lock))
    assert not _needs_migration(None, missing)


def _setup_tf114(state=True, lock=True):
    """Place the state and lock terraform 1.1.4 wrote for migrate-node where the configurator finds them."""
    if state:
        os.makedirs("active/secrets/migrate-node")
        shutil.copy(
            os.path.join(_TF114_DIR, "terraform.tfstate.yaml"),
            "active/secrets/migrate-node/",
        )
    if lock:
        os.makedirs("active/artifacts/migrate-node")
        shutil.copy(
            os.path.join(_TF114_DIR, ".terraform.lock.hcl"),
            "active/artifacts/migrate-node/",
        )


def _write_migration_ensemble(**inputs):
    os.environ["terraform_dir"] = os.path.join(
        os.path.dirname(__file__), "fixtures", "terraform"
    )
    yaml = YAML()
    with open(_MIGRATION_ENSEMBLE) as f:
        data = yaml.load(f)
    node = data["spec"]["service_template"]["topology_template"]["node_templates"]
    node["migrate-node"]["interfaces"]["defaults"]["inputs"].update(inputs)
    with open("ensemble.yaml", "w") as f:
        yaml.dump(data, f)


def _migration_warnings(caplog):
    return [w for w in _warnings(caplog) if _MIGRATION_WARNING in w]


def _run_migration_job(caplog, startTime=1, **options):
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        job = Runner(LocalEnv().get_manifest()).run(
            JobOptions(startTime=startTime, **options)
        )
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    return job, _migration_warnings(caplog)


@pytest.fixture
def stub_tofu(tmp_path, monkeypatch, fresh_cli_state):
    _write_cli_stub(tmp_path / "bin", "tofu", "OpenTofu v1.13.1")
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])


@pytest.mark.parametrize(
    "inputs,state,undo",
    [
        ({}, True, "git revert"),
        # no resources in the state to check, so the lock is checked
        ({}, False, "git revert"),
        ({"stateLocation": "remote"}, False, _REMOTE_UNDO),
    ],
)
def test_migration_warning(stub_tofu, caplog, inputs, state, undo):
    with CliRunner().isolated_filesystem():
        _setup_tf114(state=state)
        _write_migration_ensemble(**inputs)
        # check then configure: two tasks on the instance, one warning
        job, warnings = _run_migration_job(caplog, check=True, force=True)
        assert len(job.workDone) == 2, job.summary()
        assert len(warnings) == 1, warnings
        assert undo in warnings[0]
        # each job warns while the deployment hasn't migrated
        job, warnings = _run_migration_job(caplog, 2, dryrun=True, force=True)
        assert len(job.workDone) == 1, job.summary()
        assert len(warnings) == 1, warnings


def test_no_migration_warning_after_migration(stub_tofu, caplog):
    with CliRunner().isolated_filesystem():
        _setup_tf114(lock=False)
        state = "active/secrets/migrate-node/terraform.tfstate.yaml"
        with open(state) as f:
            contents = f.read()
        with open(state, "w") as f:
            f.write(contents.replace("registry.terraform.io", "registry.opentofu.org"))
        _write_migration_ensemble()
        job, warnings = _run_migration_job(caplog, dryrun=True)
        assert len(job.workDone) == 1, job.summary()
        assert not warnings


def test_no_migration_warning_for_terraform(tmp_path, monkeypatch, fresh_cli_state, caplog):
    terraform = _write_cli_stub(tmp_path / "bin", "terraform", "Terraform v1.5.7")
    with CliRunner().isolated_filesystem():
        _setup_tf114()
        _write_migration_ensemble(command=str(terraform))
        job, warnings = _run_migration_job(caplog, dryrun=True)
        assert len(job.workDone) == 1, job.summary()
        assert not warnings


# like tofu init, rewrites the lock file in the working directory
_INIT_STUB = """#!/bin/sh
case "$1" in
  version) echo "OpenTofu v1.13.1";;
  init) sed 's/registry.terraform.io/registry.opentofu.org/' .terraform.lock.hcl > lock.tmp && mv lock.tmp .terraform.lock.hcl;;
esac
"""


@pytest.mark.parametrize("dryrun", [True, False])
def test_dry_run_keeps_saved_lock(tmp_path, monkeypatch, fresh_cli_state, caplog, dryrun):
    stub = tmp_path / "bin" / "tofu"
    stub.parent.mkdir()
    stub.write_text(_INIT_STUB)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(stub.parent) + os.pathsep + os.environ["PATH"])
    with CliRunner().isolated_filesystem():
        _setup_tf114()
        _write_migration_ensemble()
        job, _ = _run_migration_job(caplog, dryrun=dryrun)
        assert len(job.workDone) == 1, job.summary()
        locked = _locked_providers("active/artifacts/migrate-node/.terraform.lock.hcl")
        migrated = "registry.opentofu.org/hashicorp/null" in locked
        assert migrated == (not dryrun), locked


def _tf114_resource_id():
    with open(os.path.join(_TF114_DIR, "terraform.tfstate.yaml")) as f:
        state = YAML().load(f)
    return state["resources"][0]["instances"][0]["attributes"]["id"]


@unittest.skipIf(
    "terraform" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set"
)
@unittest.skipUnless(shutil.which("tofu"), "tofu not installed")
class TerraformMigrationTest(unittest.TestCase):
    """Migrate the deployment Terraform 1.1.4 made of tests/fixtures/terraform/tf114 with the tofu on PATH."""

    @pytest.fixture(autouse=True)
    def _caplog(self, caplog, fresh_cli_state):
        self.caplog = caplog

    def _saved_state(self):
        with open("active/secrets/migrate-node/terraform.tfstate.yaml") as f:
            return YAML().load(f)

    def test_dry_run_warns_without_migrating(self):
        with CliRunner().isolated_filesystem():
            _setup_tf114()
            _write_migration_ensemble()
            job, warnings = _run_migration_job(self.caplog, dryrun=True)
            assert job.status == Status.ok, job.summary()
            assert len(warnings) == 1, warnings
            state = self._saved_state()
            assert "registry.terraform.io/" in state["resources"][0]["provider"]
            assert state["terraform_version"] == "1.1.4"

    def test_deploy_migrates(self):
        with CliRunner().isolated_filesystem():
            _setup_tf114()
            _write_migration_ensemble()
            # check then configure: two tasks on the instance, one warning
            job, warnings = _run_migration_job(self.caplog, check=True, force=True)
            assert job.status == Status.ok, job.summary()
            assert len(job.workDone) == 2, job.summary()
            assert len(warnings) == 1, warnings

            state = self._saved_state()
            resource = state["resources"][0]
            assert resource["provider"] == (
                'provider["registry.opentofu.org/hashicorp/null"]'
            )
            # the resource terraform created, not a new one
            assert resource["instances"][0]["attributes"]["id"] == _tf114_resource_id()
            tofu = terraform_module._tf_cli_version(
                os.path.realpath(shutil.which("tofu")), os.environ
            )
            assert tofu and tofu[0] == "tofu"
            assert state["terraform_version"] == ".".join(str(n) for n in tofu[1])
            if tofu[1] >= terraform_module.TOFU_LOCK_SHIM_VERSION:
                # the version terraform locked, and nothing left that needs registry.terraform.io
                locked = _locked_providers(
                    "active/artifacts/migrate-node/.terraform.lock.hcl"
                )
                assert locked == {
                    "registry.opentofu.org/hashicorp/null": _locked_providers(
                        os.path.join(_TF114_DIR, ".terraform.lock.hcl")
                    )[_NULL]
                }, locked

            # migrated, so the next run doesn't warn
            job, warnings = _run_migration_job(self.caplog, 2, force=True)
            assert job.status == Status.ok, job.summary()
            assert len(job.workDone) == 1, job.summary()
            assert not warnings

    def test_remote_state_warning(self):
        with CliRunner().isolated_filesystem():
            _setup_tf114(state=False)
            _write_migration_ensemble(stateLocation="remote")
            job, warnings = _run_migration_job(self.caplog, dryrun=True)
            assert len(warnings) == 1, warnings
            assert _REMOTE_UNDO in warnings[0]


def _regenerate_tf114(terraform: str) -> None:
    """Deploy the migration ensemble with ``terraform`` (1.1.4) and save its state and lock as the tf114 fixtures."""
    with CliRunner().isolated_filesystem():
        _write_migration_ensemble(command=terraform)
        job = Runner(LocalEnv().get_manifest()).run(JobOptions(startTime=1))
        assert job.status == Status.ok, job.summary()
        shutil.copy("active/secrets/migrate-node/terraform.tfstate.yaml", _TF114_DIR)
        shutil.copy("active/artifacts/migrate-node/.terraform.lock.hcl", _TF114_DIR)


if __name__ == "__main__":
    # Regenerate tests/fixtures/terraform/tf114 with the given terraform 1.1.4 binary, e.g. offline:
    # TF_CLI_CONFIG_FILE=<a filesystem_mirror config> python -m tests.test_terraform <path to terraform>
    import sys

    _regenerate_tf114(sys.argv[1])
