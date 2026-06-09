import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from unfurl.configurator import Status, Cancel
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


def test_input():
    configurator = ShellConfigurator()

    result = configurator.run_process(cmd="read in; echo $in", shell=True, input="hello world")
    assert result.stdout.strip() == "hello world"

    result2 = configurator.run_process(cmd="python -c 'import sys; print(sys.stdin.read())'", input="hello world")
    assert result2.stdout.strip() == "hello world"

    result3 = configurator.run_process(
        cmd="python -c 'import sys; print(sys.stdin.read())'",
        input="hello world",
        echo=False,
    )
    assert result3.stdout.strip() == "hello world"


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

        job = runner.run(JobOptions(instance="test_node", dryrun=False, skip_save=True))

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

        job = runner.run(JobOptions(instance="test_node", dryrun=True, skip_save=True))

        assert job.status == Status.ok
        task = list(job.workDone.values())[0]
        cmd = task.result.result["cmd"].strip()
        assert cmd == "echo hello world --use-dry-run"

    def test_error_if_dry_run_not_defined_for_task(self, caplog):
        ensemble = ENSEMBLE_DRY_RUN.format(
            command="command: echo hello world", dryrun=""
        )
        runner = Runner(YamlManifest(ensemble))

        job = runner.run(JobOptions(instance="test_node", dryrun=True, skip_save=True))
        assert not job.workDone
        assert "Skipping task: it doesn't support dry run" in caplog.text

def test_lifecycle():
    path = Path(__file__).parent / "examples" / "shell-ensemble.yaml"
    # undeploy isn't implemented, skip those
    jobs = isolated_lifecycle(str(path), DEFAULT_STEPS[:4])
    for job in jobs:
        assert job.status == Status.ok, job.workflow

@pytest.mark.parametrize(
    "input, output,status",
    [
        ["'\"S\"'", "S", Status.error],
        [1, 1, Status.ok],
    ],
)
def test_outputs(input, output, status):
    runner = Runner(YamlManifest(ENSEMBLE_OUTPUTS % input))

    job = runner.run(JobOptions(skip_save=True))
    # print(job.summary())

    assert job.status == status
    task = list(job.workDone.values())[0]
    assert task.outputs == {"a_output": output}


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
                        echo '{"a_output": {{inputs.for_output }} }'
                      properties:
                        outputsTemplate: "{{ stdout | from_json }}"
                  inputs:
                      for_output: %s
                  outputs:
                    a_output:
                      type: integer
                      description: Test output parameter
"""

ENSEMBLE_BACKGROUND = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
configurations:
  create:
    implementation:
      className: unfurl.configurators.shell.ShellConfigurator
    inputs:
      command: echo hello
      background: true
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

ENSEMBLE_BACKGROUND_SLOW = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
configurations:
  create:
    implementation:
      className: unfurl.configurators.shell.ShellConfigurator
    inputs:
      command: sleep 2 && echo done
      background: true
      initial_sleep: 0.1
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

ENSEMBLE_BACKGROUND_TIMEOUT = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
configurations:
  create:
    implementation:
      className: unfurl.configurators.shell.ShellConfigurator
      timeout: 1
    inputs:
      command: sleep 30
      background: true
      initial_sleep: 0.1
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


_BG_OK_TASK = {
    "status": "ok",
    "target": "test_node",
    "operation": "create",
    "template": "test_node",
    "type": "tosca.nodes.Root",
    "targetStatus": "ok",
    "targetState": "created",
    "changed": True,
    "configurator": "unfurl.configurators.shell.ShellConfigurator",
    "priority": "required",
    "reason": "add",
}

_BG_ERROR_TASK = {
    **_BG_OK_TASK,
    "status": "error",
    "targetStatus": "unknown",
    "targetState": "creating",
}


def test_background_quick_command():
    """Quick command completes during initial_sleep, no resume needed."""
    runner = Runner(YamlManifest(ENSEMBLE_BACKGROUND))
    job = runner.run(JobOptions(instance="test_node", startTime=1, skip_save=True))
    summary = {
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
        "tasks": [_BG_OK_TASK],
    }
    assert job.json_summary() == summary

    # run in foreground to verify same result
    ensemble = ENSEMBLE_BACKGROUND.replace("      background: true\n", "")
    runner = Runner(YamlManifest(ensemble))
    job = runner.run(JobOptions(instance="test_node", startTime=1, skip_save=True))
    assert job.json_summary() == summary


def test_background_slow_command(caplog):
    """Slow command triggers resume cycles, eventually completes without re-rendering."""
    runner = Runner(YamlManifest(ENSEMBLE_BACKGROUND_SLOW))
    with caplog.at_level("DEBUG"):
        job = runner.run(JobOptions(instance="test_node", startTime=1, skip_save=True))
    assert job.json_summary() == {
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
        "tasks": [_BG_OK_TASK],
    }
    # Verify the task was only rendered once (not re-rendered on each resume cycle)
    render_count = sum(
        1
        for r in caplog.records
        if "rendering" in r.message and "test_node" in r.message
    )
    assert render_count == 1, f"Expected 1 render, got {render_count}"


def test_background_cancel_on_job_timeout(caplog):
    """Job timeout sends Cancel to background shell, result is collected."""
    runner = Runner(YamlManifest(ENSEMBLE_BACKGROUND_SLOW))
    with caplog.at_level("DEBUG"):
        job = runner.run(JobOptions(instance="test_node", startTime=1, skip_save=True, timeout=1))
    assert job.json_summary() == {
        "job": {
            "id": "A01110000000",
            "status": "error",
            "total": 1,
            "ok": 0,
            "error": 1,
            "unknown": 0,
            "skipped": 0,
            "changed": 1,
        },
        "outputs": {},
        "tasks": [_BG_ERROR_TASK],
    }
    assert any("shell task run failure" in r.message for r in caplog.records)
    assert any("Background shell cancelled" in r.message for r in caplog.records)
    result = list(job.workDone.values())[0].result.result
    assert result["returncode"] is not None
    assert "stderr" in result
    assert "stdout" in result


def test_background_cancel_on_task_timeout():
    """Task timeout terminates background shell, result variables are captured."""
    runner = Runner(YamlManifest(ENSEMBLE_BACKGROUND_TIMEOUT))
    job = runner.run(JobOptions(instance="test_node", startTime=1, skip_save=True))
    assert job.json_summary() == {
        "job": {
            "id": "A01110000000",
            "status": "error",
            "total": 1,
            "ok": 0,
            "error": 1,
            "unknown": 0,
            "skipped": 0,
            "changed": 1,
        },
        "outputs": {},
        "tasks": [_BG_ERROR_TASK],
    }
    result = list(job.workDone.values())[0].result.result
    assert result["returncode"] is not None
    assert "stderr" in result
    assert "stdout" in result
    assert result["timeout"] == 1



ENSEMBLE_BACKGROUND_RESULT_VARS = """
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
                        echo '{{"a_output": 42}}'
                      properties:
                        outputsTemplate: "{{{{ stdout | from_json }}}}"
                        resultTemplate: |
                          {{% if returncode == 0 and stdout | from_json %}}
                          readyState:
                            local: ok
                          {{% endif %}}
                  inputs:
                    background: true
                    {extra_inputs}
                  outputs:
                    a_output:
                      type: integer
                      description: Test output parameter
"""


@pytest.mark.parametrize("initial_sleep", [0, 0.5], ids=["resume-loop", "initial-sleep"])
def test_background_result_variables(initial_sleep):
    """Background shell sets the same result variables (returncode, stdout, etc.) as blocking shell."""
    extra = f"initial_sleep: {initial_sleep}" if initial_sleep else ""
    ensemble = ENSEMBLE_BACKGROUND_RESULT_VARS.format(extra_inputs=extra)
    runner = Runner(YamlManifest(ensemble))
    job = runner.run(JobOptions(startTime=1, skip_save=True))
    summary = job.json_summary()
    assert summary == {
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
                **_BG_OK_TASK,
            }
        ],
    }
    task = list(job.workDone.values())[0]
    assert task.outputs == {"a_output": 42}
    result = task.result.result
    assert result["returncode"] == 0
    assert "a_output" in result["stdout"]
    assert result["error"] is None
    assert result["timeout"] is None


ENSEMBLE_BACKGROUND_ECHO = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
configurations:
  create:
    implementation:
      className: unfurl.configurators.shell.ShellConfigurator
    inputs:
      command: 'echo background-stdout-mark; echo background-stderr-mark 1>&2'
      background: true
      initial_sleep: 0.2
      {extra_inputs}
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


def test_background_echo_streams_to_stdout(capsys):
    """With echo=true (the default for verbose>=0), background stdout/stderr
    is streamed live to sys.stdout/sys.stderr via _BackgroundReader.pump()."""
    ensemble = ENSEMBLE_BACKGROUND_ECHO.format(extra_inputs="")
    runner = Runner(YamlManifest(ensemble))
    job = runner.run(JobOptions(instance="test_node", startTime=1, skip_save=True))
    assert job.json_summary()["job"]["ok"] == 1
    captured = capsys.readouterr()
    assert "background-stdout-mark" in captured.out
    assert "background-stderr-mark" in captured.err
    # And the result should still contain the full output
    result = list(job.workDone.values())[0].result.result
    assert "background-stdout-mark" in result["stdout"]
    assert "background-stderr-mark" in result["stderr"]


def test_background_echo_disabled(capsys):
    """echo=false suppresses live streaming; output reaches the final result
    only (via proc.communicate())."""
    ensemble = ENSEMBLE_BACKGROUND_ECHO.format(extra_inputs="echo: false")
    runner = Runner(YamlManifest(ensemble))
    job = runner.run(JobOptions(instance="test_node", startTime=1, skip_save=True))
    assert job.json_summary()["job"]["ok"] == 1
    captured = capsys.readouterr()
    assert "background-stdout-mark" not in captured.out
    assert "background-stderr-mark" not in captured.err
    result = list(job.workDone.values())[0].result.result
    assert "background-stdout-mark" in result["stdout"]
    assert "background-stderr-mark" in result["stderr"]


def test_background_echo_progressive(capsys):
    """Output that arrives during the poll loop (not just at exit) is still
    streamed live — verifies pump() runs across multiple poll iterations."""
    # Three echos separated by sleeps so output trickles across poll cycles.
    ensemble = ENSEMBLE_BACKGROUND_ECHO.replace(
        "echo background-stdout-mark; echo background-stderr-mark 1>&2",
        "echo line1; sleep 0.3; echo line2; sleep 0.3; echo line3",
    ).format(extra_inputs="")
    runner = Runner(YamlManifest(ensemble))
    job = runner.run(JobOptions(instance="test_node", startTime=1, skip_save=True))
    assert job.json_summary()["job"]["ok"] == 1
    captured = capsys.readouterr()
    for marker in ("line1", "line2", "line3"):
        assert marker in captured.out, f"{marker!r} missing from streamed stdout"
    result = list(job.workDone.values())[0].result.result
    assert "line1" in result["stdout"]
    assert "line3" in result["stdout"]
