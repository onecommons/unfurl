import unittest
import os
import json
import logging
import six
from unfurl.localenv import LocalEnv
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions
from unfurl.configurators import TemplateConfigurator
from unfurl.projectpaths import FilePath

_manifestTemplate = """
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
                run:
                  somevalue: true
                done:
                  status: %s

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

manifestContent = _manifestTemplate % "ok"
manifestErrorContent = _manifestTemplate % "error"


def test_digests(caplog):
    path = __file__ + "/../examples/digest-ensemble.yaml"
    manifest = LocalEnv(path).get_manifest()
    runner = Runner(manifest)
    output = six.StringIO()  # so we don't save the file
    job = runner.run(JobOptions(startTime=1, out=output))  # deploy
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    # print(job.out.getvalue())
    digest = job.manifest.manifest.config["changes"][0]["digestValue"]
    assert digest == "51a474d43031bde30ec0832e11ca03cf70e5a36f"

    filepath = FilePath(__file__ + "/../fixtures/helmrepo")
    digestContents = filepath.__digestable__(dict(manifest=manifest))
    assert digestContents == "git:800472c7b1b2ea128464b9144c1440ca7289a5fa"

    filepath = FilePath(__file__ + "/../..")  # root of repo
    digestContents = filepath.__digestable__(dict(manifest=manifest))
    assert digestContents.startswith("git:"), digestContents

    with caplog.at_level(logging.DEBUG):
        manifest2 = YamlManifest(
            job.out.getvalue(), path=os.path.dirname(path), localEnv=manifest.localEnv
        )
        output2 = six.StringIO()  # so we don't save the file
        job2 = Runner(manifest2).run(JobOptions(startTime=2, out=output2))
        assert not job2.unexpectedAbort, job2.unexpectedAbort.get_stack_trace()
        # print(job2.out.getvalue())
        summary = job2.json_summary()
        # print(json.dumps(summary, indent=2))
        assert summary == {
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
        }
        logMsg = "skipping task configure for instance nodeA with state NodeState.configured and status Status.ok: no change detected"
        assert logMsg in caplog.text


class DependencyTest(unittest.TestCase):
    def test_bad_dependency(self):
        """
        Don't run a task if it depends on a live attribute on an non-operational instance.

        C is deployed after A and B and
          A depends on a C property (which are static)
          B depends on a C attribute (which are live)

        So A should run and B shouldn't run
        """
        self.maxDiff = None
        manifest = YamlManifest(manifestErrorContent)
        runner = Runner(manifest)
        job = runner.run(JobOptions(startTime=1))  # deploy
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        summary = job.json_summary()
        # print("deployed")
        # print(json.dumps(summary, indent=2))
        # print(job.out.getvalue())

        dependencies = [dict(ref="::nodeC::attr", expected=None, required=True)]
        self.assertEqual(
            dependencies,
            job.manifest.manifest.config["changes"][2]["dependencies"],
        )

        self.assertEqual(
            summary,
            {
                "job": {
                    "id": "A01110000000",
                    "status": "error",
                    "total": 3,
                    "ok": 2,
                    "error": 1,
                    "unknown": 0,
                    "skipped": 0,
                    "changed": 2,
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
                        "targetStatus": "error",
                        "targetState": "configured",
                        "changed": True,
                        "configurator": "unfurl.configurators.TemplateConfigurator",
                        "priority": "required",
                        "reason": "add",
                    },
                    {
                        "status": "error",
                        "target": "nodeB",
                        "operation": "configure",
                        "template": "nodeB",
                        "type": "tosca.nodes.Root",
                        "targetStatus": "pending",
                        "targetState": "configuring",
                        "changed": False,
                        "configurator": "unfurl.configurators.TemplateConfigurator",
                        "priority": "required",
                        "reason": "add",
                    },
                ],
            },
        )

    def test_dependencies(self):
        """
        Don't run a task if it depends on a live attribute on an non-operational instance.

        C is deployed after A and B and
          A depends on a C property (which are static)
          B depends on a C attribute (which are live)

        So B should run after C
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

        # dependencies detected during render should be saved
        dependencies = [dict(ref="::nodeC::attr", required=True, expected=None)]
        self.assertEqual(
            dependencies,
            job.manifest.manifest.config["changes"][2]["dependencies"],
        )

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
        # changes = job.manifest.manifest.config["changes"]
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
