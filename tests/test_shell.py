import unittest

import pytest

from unfurl.configurator import Status
from unfurl.job import JobOptions, Runner
from unfurl.yamlmanifest import YamlManifest

manifest = """
apiVersion: unfurl/v1alpha1
kind: Manifest
configurations:
  create:
    implementation:
      className: unfurl.configurators.shell.ShellConfigurator
      environment:
        FOO: "{{inputs.foo}}"
    inputs:
       # test that self-references works in jinja2 templates
       command: "echo ${{inputs.envvar}}"
       timeout: 9999
       foo:     helloworld
       envvar:  FOO
       resultTemplate: |
         - name: SELF
           attributes:
             stdout: "{{ stdout | trim }}"
spec:
  service_template:
    topology_template:
      node_templates:
        test1:
          type: tosca.nodes.Root
          interfaces:
            Standard:
              +/configurations:
"""


class ShellConfiguratorTest(unittest.TestCase):
    def test_shell(self):
        """
        test that runner figures out the proper tasks to run
        """
        runner = Runner(YamlManifest(manifest))

        run1 = runner.run(JobOptions(instance="test1"))
        assert len(run1.workDone) == 1, run1.workDone
        self.assertEqual(
            runner.manifest.getRootResource()
            .findResource("test1")
            .attributes["stdout"],
            "helloworld",
        )
        assert not run1.unexpectedAbort, run1.unexpectedAbort.getStackTrace()


class TestDryRun:
    MANIFEST = """
apiVersion: unfurl/v1alpha1
kind: Manifest
configurations:
  create:
    implementation:
      className: unfurl.configurators.shell.ShellConfigurator
    inputs:
      {command}
      {dryrun}
spec:
  service_template:
    topology_template:
      node_templates:
        test_node:
          type: tosca.nodes.Root
          interfaces:
            Standard:
              +/configurations:
"""

    @pytest.mark.parametrize(
        "command,dryrun",
        [
            ["command: 'echo hello world'", ""],
            [
                "command: 'echo hello world'",
                "dryrun: '--use-dry-run'",
            ],
            [
                "command: 'echo hello world %dryrun%'",
                "dryrun: '--use-dry-run'",
            ],
            [
                "command:\n" "        - echo\n" "        - hello world",
                "dryrun: '--use-dry-run'",
            ],
            [
                "command:\n"
                "        - echo\n"
                "        - hello world\n"
                "        - '%dryrun%'",
                "dryrun: '--use-dry-run'",
            ],
        ],
    )
    def test_run_without_dry_run(self, command, dryrun):
        manifest = self.MANIFEST.format(command=command, dryrun=dryrun)
        runner = Runner(YamlManifest(manifest))

        job = runner.run(JobOptions(instance="test_node", dryrun=False))

        assert job.status == Status.ok
        task = list(job.workDone.values())[0]
        cmd = task.result.result["cmd"].strip()
        assert cmd == "echo hello world"

    @pytest.mark.parametrize(
        "command,dryrun",
        [
            [
                "command: 'echo hello world'",
                "dryrun: '--use-dry-run'",
            ],
            [
                "command: 'echo hello world %dryrun%'",
                "dryrun: '--use-dry-run'",
            ],
            [
                "command:\n" "        - echo\n" "        - hello world",
                "dryrun: '--use-dry-run'",
            ],
            [
                "command:\n"
                "        - echo\n"
                "        - hello world\n"
                "        - '%dryrun%'",
                "dryrun: '--use-dry-run'",
            ],
        ],
    )
    def test_run_with_dry_run(self, command, dryrun):
        manifest = self.MANIFEST.format(command=command, dryrun=dryrun)
        runner = Runner(YamlManifest(manifest))

        job = runner.run(JobOptions(instance="test_node", dryrun=True))

        assert job.status == Status.ok
        task = list(job.workDone.values())[0]
        cmd = task.result.result["cmd"].strip()
        assert cmd == "echo hello world --use-dry-run"

    def test_error_if_dry_run_not_defined_for_task(self):
        manifest = self.MANIFEST.format(command="command: echo hello world", dryrun="")
        runner = Runner(YamlManifest(manifest))

        job = runner.run(JobOptions(instance="test_node", dryrun=True))

        task = list(job.workDone.values())[0]
        assert job.status == Status.error
        assert task.result.result == "could not run: dry run not supported"
