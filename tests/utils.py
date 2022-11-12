import shutil
import traceback
import os
import os.path
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Iterable
import time
import unittest
import urllib.request

from click.testing import CliRunner
from unfurl.__main__ import cli, _latestJobs
from unfurl.job import Runner, JobOptions, Job
from unfurl.manifest import Manifest
from unfurl.support import Status, Priority


class MotoTest(unittest.TestCase):
    PROJECT_CONFIG = """\
      apiVersion: unfurl/v1alpha1
      kind: Project
      environments:
        defaults:
          connections:
            # declare the primary_provider as a connection to an Amazon Web Services account:
            primary_provider:
              type: unfurl.relationships.ConnectsTo.AWSAccount
              properties:
                  AWS_DEFAULT_REGION: us-east-1
                  endpoints:
                      ec2: http://localhost:5001
                      sts: http://localhost:5001
"""

    def setUp(self):
        from multiprocessing import Process

        from moto.server import main

        self.p = Process(target=main, args=(["-p5001"],))
        self.p.start()

        for n in range(5):
            time.sleep(0.2)
            try:
                url = "http://localhost:5001/moto-api"  # UI lives here
                urllib.request.urlopen(url)
            except Exception as e:  # URLError
                print(e)
            else:
                return True
        return False

    def tearDown(self):
        try:
            self.p.terminate()
        except:
            pass


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
    Step("deploy", Status.ok, changed=0),
    Step("undeploy", Status.absent, changed=-1),
    Step("check", Status.absent, changed=0),
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
        if task.status is not None and task.priority > Priority.ignore:
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


def init_project(cli_runner, path=None, env=None, args=None):
    args = args or [
        "init",
        "--mono",
    ]
    result = cli_runner.invoke(
        cli,
        args,
        env=_home(env),
    )
    # uncomment this to see output:
    # print("result.output", result.exit_code, result.output)
    assert not result.exception, "\n".join(traceback.format_exception(*result.exc_info))
    assert result.exit_code == 0, result

    if path and os.path.isfile(path):
        return shutil.copy(path, "ensemble/ensemble.yaml")
    return path


def isolated_lifecycle(
    path: str,
    steps: Optional[Iterable[Step]] = DEFAULT_STEPS,
    env=None,
    init_args=None,
    tmp_dir=None,
    sleep=None,
) -> Iterable[Job]:
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem(
        tmp_dir or os.getenv("UNFURL_TEST_TMPDIR")
    ) as tmp_path:
        print(f"using {tmp_path}")
        path = init_project(cli_runner, path, env, init_args)
        if path and os.path.isdir(path):
            os.chdir(path)
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
            if sleep is not None and step.changed != 0:
                time.sleep(sleep)


def print_config(dir, homedir=None):
    if homedir:
        print("!home")
        with open(Path(homedir) / "unfurl.yaml") as f:
            print(f.read())

        print("!home/local")
        with open(Path(homedir) / "local" / "unfurl.yaml") as f:
            print(f.read())

    print(f"!{dir}!")
    with open(Path(dir) / "unfurl.yaml") as f:
        print(f.read())
    print(f"!{dir}/local!")
    with open(Path(dir) / "local" / "unfurl.yaml") as f:
        print(f.read())


def run_cmd(runner, args, print_result=False, env=None):
    result = runner.invoke(cli, args, env=_home(env))
    if print_result:
        print("result.output", result.exit_code, result.output)
    assert not result.exception, "\n".join(traceback.format_exception(*result.exc_info))
    assert result.exit_code == 0, result
    return result


def run_job_cmd(runner, args=("deploy",), starttime=1, print_result=False, env=None):
    _args = list(args)
    if starttime:
        _args.append(f"--starttime={starttime}")
    result = run_cmd(runner, _args, print_result, env)
    assert _latestJobs
    job = _latestJobs[-1]
    summary = job.json_summary()
    return result, job, summary
