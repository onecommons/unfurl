import unittest
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions, Status
from unfurl.configurator import Configurator
from unfurl.merge import lookup_path
import datetime


class TestConfigurator(Configurator):
    def run(self, task):
        assert self.can_run(task)
        attributes = task.target.attributes
        attributes["copyOfMeetsTheRequirement"] = attributes["meetsTheRequirement"]
        params = attributes
        if params.get("addresources"):
            shouldYield = params.get("yieldresources")
            resourceSpec = {"name": params["resourceName"], "template": "test1"}
            if shouldYield:
                resourceSpec["dependent"] = True

            updateSpec = dict(name=".self", status=dict(attributes={"newAttribute": 1}))
            task.logger.info("updateResources: %s", [resourceSpec, updateSpec])
            jobrequest, errors = task.update_resources([resourceSpec, updateSpec])
            if shouldYield:
                job = yield jobrequest
                assert job, jobrequest

        yield task.done(True)


manifest = """
apiVersion: unfurl/v1alpha1
kind: Manifest
spec:
 instances:
    test1:
        attributes:
          meetsTheRequirement: "copy"
        interfaces:
          Standard:
            operations:
              configure:
                implementation:
                  className: Test
                  majorVersion: 0
                  preConditions:
                    properties:
                      meetsTheRequirement:
                        type: string
                    required: ['meetsTheRequirement']
    test2:
        attributes:
          meetsTheRequirement: false
        +/spec/instances/test1:
    test3:
        +/spec/instances/test1:
        properties:
          resourceName: added1
          addresources: true
    test4:
        +/spec/instances/test3:
        properties:
          resourceName: added2
          yieldresources: true
"""


class ConfiguratorTest(unittest.TestCase):
    def verifyRoundtrip(self, original, jobOptions):
        if jobOptions.startTime:
            jobOptions.startTime += 1
        job = Runner(YamlManifest(original)).run(jobOptions)
        # should not need to run any tasks
        assert len(job.workDone) == 0, job.workDone

        self.maxDiff = None
        self.assertEqual(original, job.out.getvalue())

    def test_neededTasks(self):
        """
        test that runner figures out the proper tasks to run
        """
        runner = Runner(YamlManifest(manifest))
        test1 = runner.manifest.get_root_resource().find_resource("test1")
        assert test1
        # missing = runner.manifest.spec[0].findInvalidPreconditions(test1)
        # assert not missing, missing

        run1 = runner.run(JobOptions(instance="test1"))
        assert not run1.unexpectedAbort, run1.unexpectedAbort.get_stack_trace()
        assert len(run1.workDone) == 1, run1.summary()

    def test_preConditions(self):
        """test that the configuration only runs if the resource meets the requirements"""
        runner = Runner(YamlManifest(manifest))
        test1 = runner.manifest.get_root_resource().find_resource("test1")
        assert test1

        self.assertEqual(test1.attributes["meetsTheRequirement"], "copy")
        #     notYetProvided = runner.manifest.spec[0].findMissingProvided(test1)
        #     self.assertEqual(str(notYetProvided[1]),
        # """[<ValidationError: "'copyOfMeetsTheRequirement' is a required property">]""")

        jobOptions1 = JobOptions(resource="test1", startTime=1)
        run1 = runner.run(jobOptions1)
        # print(run1.out.getvalue())
        assert not run1.unexpectedAbort, run1.unexpectedAbort.get_stack_trace()
        self.assertEqual(test1.attributes["copyOfMeetsTheRequirement"], "copy")

        # provided = runner.manifest.spec[0].findMissingProvided(test1)
        # assert not provided, provided

        # check that the modifications were recorded
        self.assertEqual(
            runner.manifest.manifest.config["changes"][0]["changes"],
            {"::test1": {"copyOfMeetsTheRequirement": "copy"}},
        )

        test2 = runner.manifest.get_root_resource().find_resource("test2")
        assert test2
        requiredAttribute = test2.attributes["meetsTheRequirement"]
        assert requiredAttribute is False, requiredAttribute
        # missing = runner.manifest.specs[0].findMissingRequirements(test2)
        # self.assertEqual(str(missing[1]), '''[<ValidationError: "False is not of type 'string'">]''')

        self.verifyRoundtrip(run1.out.getvalue(), jobOptions1)

        jobOptions2 = JobOptions(resource="test2", startTime=2)
        run2 = runner.run(jobOptions2)
        assert not run2.unexpectedAbort, run2.unexpectedAbort.get_stack_trace()
        assert run2.status == Status.error, run2.status
        # XXX better error reporting
        # self.assertEqual(str(run2.problems), "can't run required configuration: resource test2 doesn't meet requirement")

        # print(run2.out.getvalue())
        # don't re-run the failed configurations so nothing will have changed
        jobOptions2.repair = "none"
        jobOptions2.skip_new = True
        self.verifyRoundtrip(run2.out.getvalue(), jobOptions2)

    def test_addingResources(self):
        runner = Runner(YamlManifest(manifest))
        jobOptions = JobOptions(resource="test3", startTime=1)
        run = runner.run(jobOptions)
        assert not run.unexpectedAbort, run.unexpectedAbort.get_stack_trace()
        # self.assertEqual(list(run.workDone.keys()), [('test3', 'test'), ('added1', 'config1')])

        # print('config', run.out.getvalue())

        changes = runner.manifest.manifest.config["changes"][0]["changes"]
        added = {".added": {"name": "added1", "template": "test1"}}
        self.assertEqual(changes["::added1"], added)

        # verify modified
        self.assertEqual(changes["::test3"], {"copyOfMeetsTheRequirement": "copy"})

        # print('test3', run.out.getvalue())
        jobOptions.repair = "none"
        self.verifyRoundtrip(run.out.getvalue(), jobOptions)

        jobOptions = JobOptions(resource="test4", startTime=2)
        run = runner.run(jobOptions)
        assert not run.unexpectedAbort, run.unexpectedAbort.get_stack_trace()
        self.assertEqual(
            [(t.name, t.target.name) for t in run.workDone.values()],
            [
                ("for add: Standard.configure", "test4"),
                ("for add: Standard.configure", "added2"),
            ],
        )
        # print('test4', run.out.getvalue())

        # XXX
        # verify dependencies added
        # dependencies = lookupPath(
        #     runner.manifest.manifest.config,
        #     "root.instances.test4.status.configurations.test.dependencies".split("."),
        # )
        # self.assertEqual(dependencies, [{"ref": "::added2"}])

        jobOptions.repair = "none"
        self.verifyRoundtrip(run.out.getvalue(), jobOptions)

    def test_TemplateConfigurator(self):
        manifest = """\
  apiVersion: unfurl/v1alpha1
  kind: Manifest
  spec:
    service_template:
      topology_template:
        node_templates:
          testNode:
            type: tosca.nodes.Root
            interfaces:
             Standard:
              operations:
                configure:
                  implementation:
                    className: unfurl.configurators.TemplateConfigurator
                  inputs:
                    done:
                      result:
                        outputVar: true
                    resultTemplate: |
                      - name: .self
                        attributes:
                          outputVar: "{{ outputVar }}"
  """
        runner = Runner(YamlManifest(manifest))
        job = runner.run()
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        self.assertEqual(
            job.stats(),
            {"total": 1, "ok": 1, "error": 0, "unknown": 0, "skipped": 0, "changed": 1},
        )
        assert job.rootResource.find_resource("testNode").attributes["outputVar"]


def test_result_template_errors(caplog):
    manifest = """\
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    topology_template:
      node_templates:
        testNode:
          type: tosca.nodes.Root
          interfaces:
           Standard:
            operations:
              configure:
                implementation:
                  className: unfurl.configurators.TemplateConfigurator
                inputs:
                  resultTemplate: |
                    - name: .self
                      attributes:
                        outputVar: "{{ SELF.missing }}"
"""
    runner = Runner(YamlManifest(manifest))
    job = runner.run()
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    for record in caplog.records:
        if record.levelname == "WARNING":
            assert (
                record.getMessage()
                == 'error processing resultTemplate for testNode: <<Error rendering template: missing attribute or key: "missing">>'
            )
            break
    else:
        assert False, "log message not found"

    # def test_shouldRun(self):
    #   pass
    #   #assert should_run
    #   # run()
    #   #assert not should_run
    #
    # def test_update(self):
    #   #test version changed
    #   pass
    #    #
    # def test_revert(self):
    #   # assert changes are removed
    #   pass
