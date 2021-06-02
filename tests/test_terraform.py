import os
import unittest
import time
import json
from six.moves import urllib

from click.testing import CliRunner

from unfurl.job import JobOptions, Runner
from unfurl.localenv import LocalEnv
from unfurl.support import Status
from unfurl.util import sensitive_str

# python2.7 workarounds:
import unfurl.configurators
import unfurl.configurators.terraform
import unfurl.yamlmanifest


@unittest.skipIf(
    "terraform" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set"
)
class TerraformTest(unittest.TestCase):
    def setUp(self):
        self.maxDiff = None

    def test_terraform(self):
        cliRunner = CliRunner()
        with cliRunner.isolated_filesystem():  # temp_dir="/tmp"
            path = os.path.join(os.path.dirname(__file__), "examples")
            with open(os.path.join(path, "terraform-simple-ensemble.yaml")) as f:
                manifestContent = f.read()
            with open("ensemble.yaml", "w") as f:
                f.write(manifestContent)
            manifest = LocalEnv().getManifest()
            runner = Runner(manifest)
            job = runner.run(JobOptions(startTime=1))  # deploy
            assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
            example = job.rootResource.findResource("example")
            self.assertEqual(example.attributes["tag"], "Hello, test!")
            summary = job.jsonSummary()
            # print(job.summary())
            # print(job._planSummary())
            # print(json.dumps(summary, indent=2))
            self.assertEqual(
                {
                    "job": {
                        "id": "A01110000000",
                        "status": "ok",
                        "total": 2,
                        "ok": 2,
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
                            "operation": "check",
                            "template": "example",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "ok",  # nothing to modify... but still needs to run!
                            "targetState": "created",
                            "changed": False,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "check",
                        },
                        {
                            "status": "ok",
                            "target": "example",
                            "operation": "create",
                            "template": "example",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "ok",
                            "targetState": "created",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "add",
                        },
                    ],
                },
                summary,
            )

            runner2 = Runner(LocalEnv().getManifest())
            job = runner2.run(JobOptions(workflow="undeploy", startTime=2))
            assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
            # print(job.summary())
            # print(job._planSummary())
            summary = job.jsonSummary()
            # print(json.dumps(summary, indent=2))
            self.assertEqual(
                {
                    "job": {
                        "id": "A01120000000",
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
                summary,
            )

            runner2 = Runner(LocalEnv().getManifest())
            job = runner2.run(JobOptions(workflow="check", startTime=2))
            assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
            summary = job.jsonSummary()
            # print(json.dumps(summary, indent=2))
            self.assertEqual(
                {
                    "job": {
                        "id": "A01120GC0000",
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
                            "targetStatus": "absent",
                            "targetState": "deleted",
                            "changed": False,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "check",
                        }
                    ],
                },
                summary,
            )
            # {'job': {'id': 'A01120GC0000', 'status': 'ok', 'total': 1, 'ok': 1, 'error': 0, 'unknown': 0, 'skipped': 0, 'changed': 0}, 'outputs': {}, 'tasks': [{'status': 'ok', 'target': 'example', 'operation': 'check', 'template': 'example', 'type': 'unfurl.nodes.Installer.Terraform', 'targetStatus': 'ok', 'changed': False, 'configurator': 'unfurl.configurators.terraform.TerraformConfigurator', 'priority': 'required', 'reason': 'check'}]}
            self.assertEqual(
                job._jsonPlanSummary(),
                [
                    {
                        "name": "example",
                        "status": "Status.absent",
                        "state": "NodeState.deleted",
                        "managed": "A01110000002",
                        "plan": [{"operation": "check", "reason": "check"}],
                    }
                ],
            )


@unittest.skipIf(
    "terraform" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set"
)
@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
class TerraformMotoTest(unittest.TestCase):
    def setUp(self):
        import threading

        from moto.server import main

        t = threading.Thread(name="moto_thread", target=lambda: main([]))
        t.daemon = True
        t.start()

        time.sleep(0.25)
        url = "http://localhost:5000/moto-api"  # UI lives here
        f = urllib.request.urlopen(url)

        path = os.path.join(os.path.dirname(__file__), "examples")
        with open(os.path.join(path, "terraform-ensemble.yaml")) as f:
            self.ensemble_config = f.read()
        with open(os.path.join(path, "terraform-project-config.yaml")) as f:
            self.project_config = f.read()
        self.maxDiff = None

    def tearDown(self):
        pass  # XXX how to shut down the moto server?

    def test_terraform(self):
        """
        test that runner figures out the proper tasks to run
        """
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("unfurl.yaml", "w") as f:
                f.write(self.project_config)

            with open("ensemble.yaml", "w") as f:
                f.write(self.ensemble_config)

            manifest = LocalEnv().getManifest()
            assert manifest.manifest.vault and manifest.manifest.vault.secrets
            assert not manifest.lastJob

            job = Runner(manifest).run(JobOptions(startTime=1, verbose=-1))
            # print(job.out.getvalue())
            # print(job.jsonSummary(True))
            assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
            assert job.status == Status.ok, job.summary()
            example = job.rootResource.findResource("example")
            self.assertEqual(example.attributes["tags"], {"Name": "test"})
            self.assertEqual(example.attributes["availability_zone"], "us-east-1a")
            self.assertEqual(
                type(example.attributes["availability_zone"]), sensitive_str
            )
            # print("deploy")
            # print(job.jsonSummary(True))
            self.assertEqual(
                job.jsonSummary(),
                {
                    "job": {
                        "id": "A01110000000",
                        "status": "ok",
                        "total": 2,
                        "ok": 2,
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
                            "targetState": "created",
                            "operation": "check",
                            "template": "example",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "ok",
                            "changed": False,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "check",
                        },
                        {
                            "status": "ok",
                            "target": "example",
                            "operation": "create",
                            "template": "example",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "ok",
                            "targetState": "created",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "add",
                        },
                    ],
                },
            )

            # reload and check
            manifest2 = LocalEnv().getManifest()
            assert manifest2.lastJob
            manifest2.rootResource.findResource("example")
            self.assertEqual(
                type(example.attributes["availability_zone"]), sensitive_str
            )
            job = Runner(manifest2).run(
                JobOptions(workflow="check", verbose=-1, startTime=2)
            )
            assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
            # print("check")
            # print(job.jsonSummary(True))
            self.assertEqual(
                job.jsonSummary(),
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
                            "targetStatus": "started",
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
            manifest3 = LocalEnv().getManifest()
            assert manifest3.lastJob
            example = manifest3.rootResource.findResource("example")
            assert example
            self.assertEqual(
                type(example.attributes["availability_zone"]), sensitive_str
            )
            job = Runner(manifest3).run(
                JobOptions(workflow="undeploy", verbose=2, startTime=3)
            )
            assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
            assert job.status == Status.ok, job.summary()
            self.assertEqual(
                job.jsonSummary(),
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
