import unittest
import os
from unfurl.job import Runner, JobOptions
from unfurl.util import sensitive_str
from unfurl.support import Status
from unfurl.localenv import LocalEnv
from click.testing import CliRunner


ensembleConfig = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    topology_template:
      node_templates:
        example:
          type: tosca.nodes.Root
          interfaces:
            defaults:
              implementation:
                className: unfurl.configurators.terraform.TerraformConfigurator
              inputs:
                tfvars:
                  tag: test
                main:
                  provider:
                    aws:
                      version: "~> 3.2"
                      endpoints:
                        ec2: http://localhost:5000
                        sts: http://localhost:5000
                  output:
                     availability_zone:
                        value: "${aws_instance.example.availability_zone}"
                        sensitive: true
                  resource:
                    aws_instance:
                      example:
                        ami: "ami-2757f631"
                        instance_type: "t2.micro"
                        tags:
                          Name: "${var.tag}"
                  variable:
                    tag:
                      type: string
            Standard:
              operations:
                delete:
                create:
                  inputs:
                    resultTemplate:
                        attributes:
                          id: "{{ resources[0].instances[0].attributes.id }}"
                          availability_zone: "{{ outputs.availability_zone.value }}"
                          tags: "{{ resources[0].instances[0].attributes.tags }}"
            Install:
              operations:
                check:
"""

projectConfig = """
apiVersion: unfurl/v1alpha1
kind: Project
contexts:
  defaults:
    environment:
      AWS_DEFAULT_REGION: us-east-1
    secrets:
      attributes:
        vault_default_password: testing
"""


class TerraformTest(unittest.TestCase):
    def setUp(self):
        import threading
        from moto.server import main

        t = threading.Thread(name="moto_thread", target=lambda: main([]))
        t.daemon = True
        # UI lives at http://localhost:5000/moto-api
        t.start()

    def tearDown(self):
        pass  # XXX how to shut down the moto server?

    def test_terraform(self):
        """
    test that runner figures out the proper tasks to run
    """
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("unfurl.yaml", "w") as f:
                f.write(projectConfig)

            with open("ensemble.yaml", "w") as f:
                f.write(ensembleConfig)

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
                            "type": "tosca.nodes.Root",
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
                            "type": "tosca.nodes.Root",
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
                            "type": "tosca.nodes.Root",
                            "targetStatus": "absent",
                            "changed": True,
                            "configurator": "unfurl.configurators.terraform.TerraformConfigurator",
                            "priority": "required",
                            "reason": "undeploy",
                        }
                    ],
                },
            )
