import os
import unittest

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
        import threading

        from moto.server import main

        t = threading.Thread(name="moto_thread", target=lambda: main([]))
        t.daemon = True
        # UI lives at http://localhost:5000/moto-api
        t.start()

        path = os.path.join(os.path.dirname(__file__), "examples")
        with open(os.path.join(path, "terraform-ensemble.yaml")) as f:
            self.ensemble_config = f.read()
        with open(os.path.join(path, "terraform-project-config.yaml")) as f:
            self.project_config = f.read()

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
            self.assertEqual(
                job.jsonSummary(),
                {
                    "job": {
                        "id": "A01110000000",
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
                            "operation": "create",
                            "template": "example",
                            "type": "unfurl.nodes.Installer.Terraform",
                            "targetStatus": "ok",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "add",
                        }
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
            assert job.status == Status.ok, job.summary()
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
                            "changed": False,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "check",
                        }
                    ],
                },
            )

            # reload and undeploy:
            manifest3 = LocalEnv().getManifest()
            assert manifest3.lastJob
            manifest3.rootResource.findResource("example")
            self.assertEqual(
                type(example.attributes["availability_zone"]), sensitive_str
            )
            job = Runner(manifest2).run(
                JobOptions(workflow="undeploy", verbose=-1, startTime=3)
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
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "undeploy",
                        }
                    ],
                },
            )
