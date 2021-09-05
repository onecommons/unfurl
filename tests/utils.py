import shutil
import traceback
from dataclasses import dataclass
from typing import Optional, Iterable

from click.testing import CliRunner
from unfurl.__main__ import cli, _latestJobs
from unfurl.job import Runner, JobOptions, Job
from unfurl.manifest import Manifest
from unfurl.support import Status
from unfurl.yamlmanifest import YamlManifest


@dataclass
class Step:
    workflow: str
    target_status: Status
    changed: Optional[int] = None
    total: Optional[int] = None
    step: int = 0

    @property
    def name(self):
        return f"#{self.step} - {self.workflow}"


DEFAULT_STEPS = (
    Step("check", Status.absent),
    Step("deploy", Status.ok, changed=-1),  # check that some changes were made
    Step("check", Status.ok, changed=0),  # check that no changes were made
    # XXX add total=0 to check that no tasks ran (after reconfigure is smarter)
    Step("deploy", Status.ok),
    Step("undeploy", Status.absent),
    Step("check", Status.absent),
)


def _check_job(job, i, step):
    step.step = i
    step_str = step.name
    assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
    assert job.status == Status.ok, f"{step_str} is {job.status.name}"
    summary = job.json_summary()
    print(step_str)
    print(job.json_summary(True))

    if step.total is not None:
        assert summary["job"]["total"] == step.total, f"{step_str} unexpected {summary}"

    if step.changed is not None:
        if step.changed == -1:
            assert summary["job"]["changed"], f"{step_str} unexpected {summary}"
        else:
            assert (
                summary["job"]["changed"] == step.changed
            ), f"{step_str} unexpected {summary}"

    for task in job.workDone.values():
        assert (
            task.target.status == step.target_status
        ), f"Step: {step_str}, status: {task.target.status.name} should be {step.target_status.name} for {task.target.name}"
    job.step = step
    return job


def lifecycle(
    manifest: Manifest, steps: Optional[Iterable[Step]] = DEFAULT_STEPS
) -> Iterable[Job]:
    runner = Runner(manifest)
    for i, step in enumerate(steps, start=1):
        print(f"starting step #{i} - {step.workflow}")
        job = runner.run(JobOptions(workflow=step.workflow, starttime=i))
        yield _check_job(job, i, step)


def _home(env):
    if env and "UNFURL_HOME" in env:
        return env
    else:
        if env is None:
            env = {}
        env["UNFURL_HOME"] = "./unfurl_home"
        return env


def init_project(cli_runner, path, env=None):
    result = cli_runner.invoke(
        cli,
        [
            "init",
            "--mono",
        ],
        env=_home(env),
    )
    # uncomment this to see output:
    # print("result.output", result.exit_code, result.output)
    assert not result.exception, "\n".join(traceback.format_exception(*result.exc_info))
    assert result.exit_code == 0, result

    return shutil.copy(path, "ensemble/ensemble.yaml")


def isolated_lifecycle(
    path: str, steps: Optional[Iterable[Step]] = DEFAULT_STEPS, env=None
) -> Iterable[Job]:
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem():
        init_project(cli_runner, path, env)
        for i, step in enumerate(steps, start=1):
            print(f"starting step #{i} - {step.workflow}")
            args = [
                #  "-vvv",
                step.workflow,
                "--starttime",
                i,
            ]
            result = cli_runner.invoke(cli, args, env=_home(env))
            print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            assert result.exit_code == 0, result
            assert _latestJobs
            yield _check_job(_latestJobs[-1], i, step)
