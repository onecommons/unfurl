import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from unfurl.configurator import Status
from unfurl.configurators.shell import ShellConfigurator, subprocess
from unfurl.job import JobOptions, Runner
from unfurl.yamlmanifest import YamlManifest

from .utils import isolated_lifecycle, DEFAULT_STEPS


class TestShellConfigurator:
    def setup_method(self):
        path = Path(__file__).parent / "examples" / "shell-ensemble.yaml"
        with open(path) as f:
            self.ensemble = f.read()

    def test_shell(self):
        """
        test that runner figures out the proper tasks to run
        """
        runner = Runner(YamlManifest(self.ensemble))

        job = runner.run(JobOptions(instance="test1"))

        assert len(job.workDone) == 1, job.workDone
        node = runner.manifest.get_root_resource().find_resource("test1")
        assert node.attributes["stdout"] == "helloworld"
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()

    def test_timeout(self):
        configurator = ShellConfigurator()

        err = configurator.run_process(cmd="sleep 5", timeout=1)

        assert isinstance(err, subprocess.TimeoutExpired)

    @pytest.mark.skipif(sys.version_info < (3, 7, 5), reason="requires Python 3.7.5")
    def test_timeout_with_ensemble(self):
        runner = Runner(YamlManifest(ENSEMBLE_TIMEOUT))
        start_time = datetime.now()

        job = runner.run(JobOptions(instance="test_node"))

        delta = datetime.now() - start_time
        assert job.status == Status.error
        assert delta < timedelta(seconds=2), delta - timedelta(seconds=2)


class TestDryRun:
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
        ensemble = ENSEMBLE_DRY_RUN.format(command=command, dryrun=dryrun)
        runner = Runner(YamlManifest(ensemble))

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
        ensemble = ENSEMBLE_DRY_RUN.format(command=command, dryrun=dryrun)
        runner = Runner(YamlManifest(ensemble))

        job = runner.run(JobOptions(instance="test_node", dryrun=True))

        assert job.status == Status.ok
        task = list(job.workDone.values())[0]
        cmd = task.result.result["cmd"].strip()
        assert cmd == "echo hello world --use-dry-run"

    def test_error_if_dry_run_not_defined_for_task(self):
        ensemble = ENSEMBLE_DRY_RUN.format(
            command="command: echo hello world", dryrun=""
        )
        runner = Runner(YamlManifest(ensemble))

        job = runner.run(JobOptions(instance="test_node", dryrun=True))

        task = list(job.workDone.values())[0]
        assert job.status == Status.error
        assert task.result.result == "could not run: dry run not supported"


def test_lifecycle():
    path = Path(__file__).parent / "examples" / "shell-ensemble.yaml"
    # undeploy isn't implemented, skip those
    jobs = isolated_lifecycle(str(path), DEFAULT_STEPS[:4])
    for job in jobs:
        assert job.status == Status.ok, job.workflow

def test_outputs():
    runner = Runner(YamlManifest(ENSEMBLE_OUTPUTS))

    job = runner.run(JobOptions(skip_save=True))

    assert job.status == Status.ok
    task = list(job.workDone.values())[0]
    assert task.outputs == {"a_output": 1}


ENSEMBLE_TIMEOUT = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
configurations:
  create:
    implementation:
      className: unfurl.configurators.shell.ShellConfigurator
      timeout: 1
    inputs:
      command: sleep 15
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

ENSEMBLE_DRY_RUN = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
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

ENSEMBLE_OUTPUTS = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    topology_template:
      node_templates:
        test_node:
          type: tosca.nodes.Root
          interfaces:
            Standard:
              operations:
                create:
                  implementation:
                    primary:
                      type: unfurl.artifacts.ShellExecutable
                      file: dummy.sh
                      contents: |
                        echo '{"a_output": {{inputs.for_output}} }'
                      properties:
                        outputsTemplate: "{{ stdout | from_json }}"
                  inputs:
                      for_output: 1
"""

