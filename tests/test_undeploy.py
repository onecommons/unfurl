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
                        readyState:
                          local: ok
                      - name: unmanaged
                        template: discovered
                        # status is not set, so create and delete operations will be invoked
  """


class UndeployTest(unittest.TestCase):
    # assert addResources() sets created = target if includes status
    # undeploy doesn't create delete task if created != target
    # check only sets creator = False if found and created wasn't set before

    # config sets creator = True if created wasn't set before

    # undeploy doesn't delete unless created == True or created == self.key

    # undeploy doesn't delete with directive = discover
    # undeploy doesn't delete with directive = select
    # undeploy only deletes if status == ok, degraded, error (aka present)

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
        # print(job.out.getvalue())

        manifest2 = YamlManifest(job.out.getvalue())
        job = Runner(manifest2).run(JobOptions(workflow="undeploy", startTime=2))
        assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
        summary = job.jsonSummary()
        # print(json.dumps(summary, indent=2))
        targets = [t["target"] for t in summary["tasks"]]
        self.assertNotIn(
            "discovered",
            targets,
            "template with dependent directive should not create an instance",
        )
        self.assertNotIn(
            "preexisting", targets, "discovered instances should not be deleted"
        )
        self.assertIn("unmanaged", targets, "unmanaged should be deleted")
