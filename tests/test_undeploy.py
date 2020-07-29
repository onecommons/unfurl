import unittest
import json
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions, Status
from unfurl.configurators import TemplateConfigurator

manifestContent = """\
  apiVersion: unfurl/v1alpha1
  kind: Ensemble
  spec:
    service_template:
      node_types:
        test.nodes.simple:
          interfaces:
           Install:
              check:
                implementation: unfurl.configurators.TemplateConfigurator
           Standard:
              configure:
                implementation: unfurl.configurators.TemplateConfigurator
              delete:
                implementation: unfurl.configurators.TemplateConfigurator

      topology_template:
        node_templates:
          discovered:
            directives:
              - dependent
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
                    result:
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


class UndeployTest(unittest.TestCase):
    def test_check(self):
        manifest = YamlManifest(manifestContent)
        runner = Runner(manifest)
        job = runner.run(JobOptions(startTime=1))  # deploy
        assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
        summary = job.jsonSummary()
        # print(json.dumps(summary, indent=2))
        self.assertEqual(
            {
                "id": "A01110000000",
                "status": "ok",
                "total": 5,
                "ok": 5,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 4,
            },
            summary["job"],
        )
        targets = [t["target"] for t in summary["tasks"]]
        self.assertNotIn(
            "discovered",
            targets,
            "template with dependent directive should not create an instance",
        )
        self.assertNotIn("managed", targets, "managed should not create deploy tasks")
        self.assertIn("unmanaged", targets, "unmanaged should create deploy tasks")
        self.assertEqual(
            job.rootResource.findResource("managed").created, "::installerNode"
        )
        self.assertIs(job.rootResource.findResource("unmanaged").created, True)
        self.assertIs(job.rootResource.findResource("preexisting").created, False)
        # print(job.out.getvalue())

        manifest2 = YamlManifest(job.out.getvalue())
        # don't delete installerNode
        instances = [n for n in job.rootResource.all if n != "installerNode"]
        job = Runner(manifest2).run(
            JobOptions(workflow="undeploy", instances=instances, startTime=2)
        )
        assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
        summary = job.jsonSummary()
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
        assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
        summary = job.jsonSummary()
        # print(json.dumps(summary, indent=2))
        self.assertEqual(
            {
                "id": "A01130000000",
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
        self.assertIn("installerNode", targets, "installerNode should be deleted")
        self.assertIn("managed", targets, "managed should be deleted now")

        # XXX more tests:
        # check / discover only sets creator = False if instance is found and created wasn't set before
        # config sets creator = True only if created wasn't set before
        # undeploy doesn't delete with directive = select
        # undeploy only deletes if status == ok, degraded, error (aka present)

    # XXX fix and test Install.revert:
    # def test_revert(self): pass
