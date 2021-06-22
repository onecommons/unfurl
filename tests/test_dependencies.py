import unittest
import os
import json
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions
from unfurl.configurators import TemplateConfigurator

manifestContent = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    types:
      nodes.Test:
        derived_from: tosca.nodes.Root
        attributes:
          attr:
            type: string
        interfaces:
         Standard:
          operations:
            configure:
              implementation: Template
              inputs:
                done:
                  status: ok
    topology_template:
      node_templates:
        nodeA:
          type: tosca:Root
          interfaces:
           Standard:
            operations:
              configure:
                implementation: Template
                inputs:
                  run: "{{ NODES.nodeC.prop }}"
                  done:
                    status: ok

        nodeB:
          type: tosca:Root
          interfaces:
           Standard:
            operations:
              configure:
                implementation: Template
                inputs:
                  run: "{{ NODES.nodeC.attr }}"
                  done:
                    status: ok

        nodeC:
          type: nodes.Test
          properties:
            prop: static
        # attributes: # XXX this is currently ignored!
        #   attr: live
"""


class DependencyTest(unittest.TestCase):
    def test_dependencies(self):
        """
        Don't run a task if it depends on a live attribute on an non-operational instance.

        C is deployed after A and B and
          A depends on a C property (which are static)
          B depends on a C attribute (which are live)

        So A should run and B shouldn't run
        """
        self.maxDiff = None
        manifest = YamlManifest(manifestContent)
        runner = Runner(manifest)
        job = runner.run(JobOptions(startTime=1))  # deploy
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        summary = job.json_summary()
        # print("deployed")
        # print(json.dumps(summary, indent=2))
        # print(job.out.getvalue())

        # XXX dependencies detected during render should be saved
        # dependencies = [dict(ref="::nodeC::attr", required=True)]
        # self.assertEqual(
        #     dependencies,
        #     job.runner.manifest.manifest.config["changes"][1]["dependencies"],
        # )

        self.assertEqual(
            summary,
            {
                "job": {
                    "id": "A01110000000",
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
                        "target": "nodeA",
                        "operation": "configure",
                        "template": "nodeA",
                        "type": "tosca.nodes.Root",
                        "targetStatus": "ok",
                        "targetState": "configured",
                        "changed": True,
                        "configurator": "unfurl.configurators.TemplateConfigurator",
                        "priority": "required",
                        "reason": "add",
                    },
                    {
                        "status": "ok",
                        "target": "nodeC",
                        "operation": "configure",
                        "template": "nodeC",
                        "type": "nodes.Test",
                        "targetStatus": "ok",
                        "targetState": "configured",
                        "changed": True,
                        "configurator": "unfurl.configurators.TemplateConfigurator",
                        "priority": "required",
                        "reason": "add",
                    },
                    {
                        "status": "ok",
                        "target": "nodeB",
                        "operation": "configure",
                        "template": "nodeB",
                        "type": "tosca.nodes.Root",
                        "targetStatus": "ok",
                        "targetState": "configured",
                        "changed": True,
                        "configurator": "unfurl.configurators.TemplateConfigurator",
                        "priority": "required",
                        "reason": "add",
                    },
                ],
            },
        )

        # Deploy again: B's task should run now since C should have been deployed
        manifest2 = YamlManifest(job.out.getvalue())
        job = Runner(manifest2).run(JobOptions(startTime=2))
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        summary = job.json_summary()
        # changes = job.runner.manifest.manifest.config["changes"]
        # XXX test that attr: "live" is in changes
        # print(job.out.getvalue())
        self.assertEqual(
            summary,
            {
                "job": {
                    "id": "A01120000000",
                    "status": "ok",
                    "total": 0,
                    "ok": 0,
                    "error": 0,
                    "unknown": 0,
                    "skipped": 0,
                    "changed": 0,
                },
                "outputs": {},
                "tasks": [],
            },
        )
