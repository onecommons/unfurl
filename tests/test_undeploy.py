import unittest
import json
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions, Status
from unfurl.configurators import TemplateConfigurator
from .utils import lifecycle, isolated_lifecycle, Step
from pathlib import Path

manifestContent = """\
  apiVersion: unfurl/v1alpha1
  kind: Ensemble
  spec:
    service_template:
      node_types:
        test.nodes.simple:
          interfaces:
           defaults:
              implementation: unfurl.configurators.TemplateConfigurator
           Install:
              check:
           Standard:
              configure:
              delete:

      topology_template:
        node_templates:
          discovered:
            directives:
              - dependent
            type: test.nodes.simple

          external:
            directives:
              - select
            type: test.nodes.simple

          missing:
            type: test.nodes.simple

          preexisting:
            type: test.nodes.simple
            directives:
              - discover
            interfaces:
             Install:
              operations:
                discover:
                  implementation: unfurl.configurators.TemplateConfigurator
                  inputs:
                    done:
                      # check only sets creator = False if found and created wasn't set before
                      status: ok

          installerNode:
            type: test.nodes.simple
            interfaces:
             Standard:
              operations:
                configure:
                  implementation: unfurl.configurators.TemplateConfigurator
                  inputs:
                    resultTemplate:
                      - name: managed
                        template: discovered
                        # status is set, so create and delete operations won't be invoked
                        readyState: ok
                      - name: unmanaged
                        template: discovered
                        # status is not set, so create and delete operations will be invoked
  """

manifest2Content = """\
  apiVersion: unfurl/v1alpha1
  kind: Ensemble
  spec:
    service_template:
      node_types:
        test.nodes.simple:
          interfaces:
           defaults:
              implementation: unfurl.configurators.TemplateConfigurator
           Standard:
              create:
              start:
              stop:
              delete:

      topology_template:
        node_templates:
          no_op:
            type: tosca.nodes.Root

          simple:
            type: test.nodes.simple
  """


class UndeployTest(unittest.TestCase):
    def setUp(self):
        self.maxDiff = None

    def test_check(self):
        manifest = YamlManifest(manifestContent)
        runner = Runner(manifest)
        job = runner.run(JobOptions(startTime=1, check=True))  # deploy
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        summary = job.json_summary()
        # print(json.dumps(summary, indent=2))
        # print(job.out.getvalue())
        self.assertEqual(
            {
                "id": "A01110000000",
                "status": "ok",
                "total": 8,
                "ok": 7,
                "error": 0,
                "unknown": 0,
                "skipped": 1,
                "changed": 6,
            },
            summary["job"],
        )
        targets = [t["target"] for t in summary["tasks"] if t["status"]]
        self.assertNotIn(
            "discovered",
            targets,
            "template with dependent directive should not create an instance",
        )
        self.assertNotIn("managed", targets, "managed should not create deploy tasks")
        self.assertIn("unmanaged", targets, "unmanaged should create deploy tasks")
        self.assertEqual(
            job.rootResource.find_resource("managed").created, "::installerNode"
        )
        self.assertEqual(
            job.rootResource.find_resource("unmanaged").created, "A01110000008"
        )
        self.assertIs(job.rootResource.find_resource("preexisting").created, None)
        self.assertNotIn(
            "external", targets, "missing external instances should not be created"
        )

        # print(job.out.getvalue())

        manifest2 = YamlManifest(job.out.getvalue())
        # don't delete installerNode
        instances = [n for n in job.rootResource.all if n != "installerNode"]
        job = Runner(manifest2).run(
            JobOptions(workflow="undeploy", instances=instances, startTime=2)
        )
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        summary = job.json_summary()
        # print(json.dumps(summary, indent=2))
        self.assertEqual(
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
            summary["job"],
        )
        targets = [t["target"] for t in summary["tasks"]]
        self.assertNotIn(
            "preexisting", targets, "discovered instances should not be deleted"
        )
        self.assertIn("unmanaged", targets, "unmanaged should be deleted")
        self.assertNotIn("managed", targets, "managed should not be deleted")
        # print(job.out.getvalue())

        # now undeploy installerNode
        manifest3 = YamlManifest(job.out.getvalue())
        job = Runner(manifest3).run(JobOptions(workflow="undeploy", startTime=3))
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        summary = job.json_summary()
        # print(json.dumps(summary, indent=2))
        # installerNode gets deleted, orphaning "managed"
        self.assertEqual(
            {
                "id": "A01130000000",
                "status": "ok",
                "total": 1,
                "ok": 1,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 1,
            },
            summary["job"],
        )
        targets = [t["target"] for t in summary["tasks"]]
        self.assertNotIn(
            "preexisting", targets, "discovered instances should not be deleted"
        )
        self.assertIn("installerNode", targets, "installerNode should be deleted")

        # check: instance should still be absent
        manifest5 = YamlManifest(job.out.getvalue())
        job = Runner(manifest5).run(JobOptions(workflow="check", startTime=4))
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        summary2 = job.json_summary()
        # print(summary2)
        self.assertEqual(
            {
                "id": "A01140000000",
                "status": "ok",
                "total": 5,
                "ok": 5,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 0,
            },
            summary2["job"],
        )
        # XXX more tests:
        # check / discover only sets creator = False if instance is found and created wasn't set before
        # config sets creator = True only if created wasn't set before
        # undeploy doesn't delete with directive = select
        # undeploy only deletes if status == ok, degraded, error (aka present)

    def test_stop(self):
        manifest = YamlManifest(manifest2Content)
        runner = Runner(manifest)
        # deploy
        job = runner.run(JobOptions(startTime=1))
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        summary = job.json_summary()
        # print(json.dumps(summary, indent=2))
        # print(job.out.getvalue())
        self.assertEqual(
            {
                "id": "A01110000000",
                "status": "ok",
                "total": 2,
                "ok": 2,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 1,
            },
            summary["job"],
        )

        # stop
        manifest2 = YamlManifest(job.out.getvalue())
        job = Runner(manifest2).run(JobOptions(workflow="stop", startTime=2))
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        summary = job.json_summary()
        # print(json.dumps(summary, indent=2))
        # print(job.out.getvalue())
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
                        "target": "simple",
                        "operation": "stop",
                        "template": "simple",
                        "type": "test.nodes.simple",
                        "targetStatus": "pending",
                        "targetState": "stopped",
                        "changed": True,
                        "configurator": "unfurl.configurators.TemplateConfigurator",
                        "priority": "required",
                        "reason": "stop",
                    }
                ],
            },
            summary,
        )

        # start again
        manifest3 = YamlManifest(job.out.getvalue())
        job = Runner(manifest3).run(JobOptions(startTime=3))
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        summary = job.json_summary()
        # print(json.dumps(summary, indent=2))
        # print(job.out.getvalue())
        self.assertEqual(
            {
                "id": "A01130000000",
                "status": "ok",
                "total": 1,
                "ok": 1,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 1,
            },
            summary["job"],
        )

        # undeploy: should stop and delete
        manifest4 = YamlManifest(job.out.getvalue())
        job = Runner(manifest4).run(JobOptions(workflow="undeploy", startTime=4))
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        summary = job.json_summary()
        # print(json.dumps(summary, indent=2))
        # print(job.out.getvalue())
        self.assertEqual(
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
            summary["job"],
        )
        self.assertEqual(
            [
                {
                    "status": "ok",
                    "target": "simple",
                    "operation": "stop",
                    "template": "simple",
                    "type": "test.nodes.simple",
                    "targetStatus": "ok",
                    "targetState": "stopped",
                    "changed": False,
                    "configurator": "unfurl.configurators.TemplateConfigurator",
                    "priority": "required",
                    "reason": "undeploy",
                },
                {
                    "status": "ok",
                    "target": "simple",
                    "operation": "delete",
                    "template": "simple",
                    "type": "test.nodes.simple",
                    "targetStatus": "absent",
                    "targetState": "deleted",
                    "changed": True,
                    "configurator": "unfurl.configurators.TemplateConfigurator",
                    "priority": "required",
                    "reason": "undeploy",
                },
            ],
            summary["tasks"],
        )
        # print(job._json_plan_summary(pretty=True, include_rendered=False))
        self.assertEqual(
            job._json_plan_summary(include_rendered=False),
            [
                {
                    "instance": "simple",
                    "status": "Status.absent",
                    "state": "NodeState.deleted",
                    "managed": "A01110000002",
                    "plan": [
                        {
                            "workflow": "undeploy",
                            "sequence": [
                                {"operation": "stop", "reason": "undeploy"},
                                {"operation": "delete", "reason": "undeploy"},
                            ],
                        }
                    ],
                }
            ],
        )

    # XXX fix and test Install.revert:
    # def test_revert(self): pass


manifestNoOpContent = """\
  apiVersion: unfurl/v1alpha1
  kind: Ensemble
  spec:
    service_template:
      topology_template:
        node_templates:
          no_op:
            type: tosca.nodes.Root
  """

def test_noop():
    STEPS = (
        Step("check", Status.absent),
        Step("deploy", Status.ok, changed=0),
        Step("check", Status.ok, changed=0),
        Step("deploy", Status.ok, changed=0),
        Step("undeploy", Status.absent, changed=0),
        Step("check", Status.absent, changed=0),
    )
    manifest = YamlManifest(manifestNoOpContent)
    list(lifecycle(manifest, STEPS))

def test_protected():
    STEPS = (
        Step("deploy", Status.ok, changed=1),
        Step("undeploy", Status.ok, changed=0), # doesn't delete
        Step("check", Status.ok, changed=0),
    )
    path = Path(__file__).parent / "examples" / "protected-ensemble.yaml"
    jobs = isolated_lifecycle(str(path), STEPS)
    list(jobs)

manifest_delete_failed_node = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    node_types:
      my_host:
        derived_from: tosca:Root
        capabilities:
          host:
            type: tosca.capabilities.Compute

    topology_template:
      node_templates:
        my_host:
          type: my_host
          interfaces:
            Standard:
              operations:
                create:
                  implementation: exit 1
                  inputs:
                    done:
                      modified: true
                delete:
                  implementation: exit 0
"""

def test_delete_failed_node():
    STEPS = (
        Step("deploy", Status.error, changed=1),
        Step("undeploy", Status.absent, changed=1),
    )
    manifest = YamlManifest(manifest_delete_failed_node)
    list(lifecycle(manifest, STEPS))


manifest_dependent = """\
  apiVersion: unfurl/v1alpha1
  kind: Ensemble
  spec:
    service_template:
      node_types:
        test.nodes.simple:
          interfaces:
           defaults:
              implementation: Template
           Install:
              check:
           Standard:
              configure:
              delete:

      topology_template:
        node_templates:
          host:
            type: test.nodes.simple

          deletable:
            type: test.nodes.simple

          dependent:
            directives:
              - protected
            type: test.nodes.simple
            requirements:
              - host: host
"""

def test_delete_dependent_node():
    STEPS = (
        Step("deploy", Status.ok, changed=3),
        Step("undeploy", Status.absent, changed=1),
    )
    manifest = YamlManifest(manifest_dependent)
    list(lifecycle(manifest, STEPS))
