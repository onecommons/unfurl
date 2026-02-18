import unittest
import traceback
import os
from click.testing import CliRunner
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions, Status
from unfurl.configurator import Configurator
from unfurl.__main__ import cli
from unfurl.job import start_job


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
            jobrequest, errors = task.update_instances([resourceSpec, updateSpec])
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
        readyState: pending
        template:
          type: unfurl.nodes.Default
          interfaces:
            Standard:
              operations:
                configure:
                  implementation:
                    className: Test
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
        template:
          properties:
            resourceName: added1
            addresources: true
    test4:
        +/spec/instances/test3:
        template:
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

        jobOptions1 = JobOptions(instance="test1", startTime=1)
        run1 = runner.run(jobOptions1)
        # print(run1.out.getvalue())
        assert not run1.unexpectedAbort, run1.unexpectedAbort.get_stack_trace()
        self.assertEqual(test1.attributes["copyOfMeetsTheRequirement"], "copy")

        # provided = runner.manifest.spec[0].findMissingProvided(test1)
        # assert not provided, provided

        # check that the modifications were recorded
        self.assertEqual(
            runner.manifest.manifest.config["changes"][0]["changes"],
            {":::test1": {"copyOfMeetsTheRequirement": "copy"}},
        )

        test2 = runner.manifest.get_root_resource().find_resource("test2")
        assert test2
        requiredAttribute = test2.attributes["meetsTheRequirement"]
        assert requiredAttribute is False, requiredAttribute
        # missing = runner.manifest.specs[0].findMissingRequirements(test2)
        # self.assertEqual(str(missing[1]), '''[<ValidationError: "False is not of type 'string'">]''')

        self.verifyRoundtrip(run1.out.getvalue(), jobOptions1)

        jobOptions2 = JobOptions(instance="test2", startTime=2)
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
        jobOptions = JobOptions(instance="test3", startTime=1)
        run = runner.run(jobOptions)
        assert not run.unexpectedAbort, run.unexpectedAbort.get_stack_trace()
        # self.assertEqual(list(run.workDone.keys()), [('test3', 'test'), ('added1', 'config1')])

        # print('config', run.out.getvalue())

        changes = runner.manifest.manifest.config["changes"][0]["changes"]
        added = {".added": {"name": "added1", "template": "test1"}}
        assert changes[":::added1"] == added

        # verify modified
        assert changes[":::test3"] == {"copyOfMeetsTheRequirement": "copy"}

        # print('test3', run.out.getvalue())
        jobOptions.repair = "none"
        self.verifyRoundtrip(run.out.getvalue(), jobOptions)

        jobOptions = JobOptions(instance="test4", startTime=2)
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
    assert job.get_end_time() > job.get_start_time()
    for record in caplog.records:
        if record.levelname == "WARNING":
            assert (
                record.getMessage()
                == 'Task configure for testNode (reason: add): error processing resultTemplate: <<Error rendering template: missing attribute or key: "missing">>'
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


def test_discover_artifact():
    manifest = """\
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    node_types:
      BuildArtifact:
        artifacts:
          test:  # this type will be used if not set inline in the template
            type: tosca.artifacts.File
        interfaces:
           Standard:
            operations:
              configure:
                implementation:
                  className: unfurl.configurators.TemplateConfigurator
                inputs:
                  resultTemplate: |
                    - name: .self
                      artifacts:
                        test:
                          # error if template isn't declared inline
                          template:  
                            file: artifact.txt

    topology_template:
      node_templates:
        testNode:
          type: BuildArtifact
"""
    runner = Runner(YamlManifest(manifest))
    job = runner.run()
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    artifact = (
        job.manifest.get_root_resource().find_resource("testNode").artifacts["test"]
    )
    assert artifact.file == "artifact.txt"
    assert artifact.type == "tosca.artifacts.File"

    saved = job.out.getvalue()
    # print(saved)
    loaded = YamlManifest(saved)
    artifact = loaded.get_root_resource().find_resource("testNode").artifacts["test"]
    assert artifact.file == "artifact.txt"
    assert artifact.type == "tosca.artifacts.File"
    # out = loaded.manifest.save()
    # print(out.getvalue())

    # no tasks will run since nothing has changed but make sure the artifact is still there
    job = Runner(loaded).run()
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    saved = job.out.getvalue()
    # print(saved)
    loaded = YamlManifest(saved)
    artifact = loaded.get_root_resource().find_resource("testNode").artifacts["test"]
    assert artifact.file == "artifact.txt"
    assert artifact.type == "tosca.artifacts.File"

    # force task to run and replace the artifact
    job = Runner(loaded).run(jobOptions=JobOptions(force=True))
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    saved = job.out.getvalue()
    # print(saved)
    loaded = YamlManifest(saved)
    artifact = loaded.get_root_resource().find_resource("testNode").artifacts["test"]
    assert artifact.file == "artifact.txt"
    assert artifact.type == "tosca.artifacts.File"


envRestoreManifest = """\
  apiVersion: unfurl/v1alpha1
  kind: Ensemble
  +include:
    file: ensemble-template.yaml
    repository: spec
  environment:
    variables:
      TEST_MANIFEST_VAR: from_manifest
      RESET_CREDENTIALS: cred.json
      ACCESS_TOKEN: fake_token
  changes: []
  spec:
    service_template:
      topology_template:
        node_templates:
          testNode:
            type: tosca.nodes.Root
            interfaces:
              Standard:
                operations:
                  create:
                    implementation:
                      className: Template
                    inputs:
                      done:
                        result: {}
                      resultTemplate:
                        eval:
                          to_env:
                            TEST_PERSISTENT_VAR: persisted_value
                            ACCESS_TOKEN: null
                          update_os_environ: true
  """


def test_environ_restored_after_job():
    """
    Test that os.environ is restored after a job runs, except for
    changes made with update_os_environ=True.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            [
                "--home",
                "./unfurl_home",
                "init",
                "--mono",
            ],
        )
        assert not result.exception, "\n".join(
            traceback.format_exception(*result.exc_info)
        )

        with open("ensemble/ensemble.yaml", "w") as f:
            f.write(envRestoreManifest)

        # Set up initial os.environ state
        os.environ["TEST_PRE_EXISTING"] = "should_survive"
        os.environ["ACCESS_TOKEN"] = "original_token"
        # Set a pre-existing value that the manifest will override during the job
        os.environ["RESET_CREDENTIALS"] = "original_creds.json"
        original_creds = os.environ["RESET_CREDENTIALS"]

        # These should NOT be in os.environ before the job
        assert "TEST_MANIFEST_VAR" not in os.environ
        assert "TEST_PERSISTENT_VAR" not in os.environ

        try:
            job, rendered, proceed = start_job(_opts={"startTime": 1})
            job.run(rendered)

            summary = job.json_summary()
            assert summary["job"]["status"] == "ok", summary

            # Env vars set only by the manifest should NOT persist after the job
            assert os.environ.get("TEST_MANIFEST_VAR") is None, (
                "Manifest env var should not persist in os.environ after job"
            )

            # Manifest should not permanently overwrite pre-existing values
            assert os.environ.get("RESET_CREDENTIALS") == original_creds, (
                f"Manifest should not overwrite pre-existing RESET_CREDENTIALS: "
                f"expected {original_creds!r}, got {os.environ.get('RESET_CREDENTIALS')!r}"
            )

            # Env vars set with update_os_environ=True SHOULD persist
            assert os.environ.get("TEST_PERSISTENT_VAR") == "persisted_value", (
                "update_os_environ=True changes should persist after job"
            )

            # update_os_environ deleted ACCESS_TOKEN
            assert "ACCESS_TOKEN" not in os.environ, (
                "update_os_environ=True deletion should persist after job"
            )

            # Pre-existing env vars should be unchanged
            assert os.environ.get("TEST_PRE_EXISTING") == "should_survive", (
                "Pre-existing env vars should survive the job"
            )
        finally:
            # Clean up
            os.environ.pop("TEST_PRE_EXISTING", None)
            os.environ.pop("TEST_PERSISTENT_VAR", None)
            os.environ.pop("RESET_CREDENTIALS", None)
            os.environ.pop("ACCESS_TOKEN", None)
