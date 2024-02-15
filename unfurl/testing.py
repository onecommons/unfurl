# Copyright (c) 2024 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Utility functions for unit tests.
"""
from dataclasses import dataclass
from typing import Any, List, Optional, Iterable, Tuple
import shutil
import traceback
import time
import os
import os.path
from typing import (
    Optional,
    Type,
    TypeVar,
    Tuple,
)

from click.testing import CliRunner, Result
from .__main__ import cli, _latestJobs
from .job import Runner, JobOptions, Job
from .manifest import Manifest
from .yamlmanifest import YamlManifest
from .support import Status, Priority
import tosca
from tosca.python2yaml import PythonToYaml
from .dsl import proxy_instance


@dataclass
class Step:
    workflow: str
    target_status: Status
    changed: Optional[int] = None
    total: Optional[int] = None
    step: int = 0
    ignore_target_status: tuple = ()

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
    expected_job_status = (
        Status.error if step.target_status == Status.error else Status.ok
    )
    assert job.status == expected_job_status, f"{step_str} is {job.status.name}"
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
            if task.target.name not in step.ignore_target_status:
                assert (
                    task.target.status == step.target_status
                ), f"Step: {step_str}, status: {task.target.status.name} should be {step.target_status.name} for {task.target.name}"
    job.step = step
    return job


def lifecycle(
    manifest: Manifest, steps: Iterable[Step] = DEFAULT_STEPS
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
    steps: Iterable[Step] = DEFAULT_STEPS,
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
            args: List[str] = [
                #  "-vvv",
                step.workflow,
                "--starttime",
                str(i),
            ]
            result = cli_runner.invoke(cli, args, env=_home(env))
            print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(  
                traceback.format_exception(*result.exc_info)  # type: ignore
            )
            assert result.exit_code == 0, result
            assert _latestJobs
            yield _check_job(_latestJobs[-1], i, step)
            if sleep is not None and step.changed != 0:
                time.sleep(sleep)


def run_cmd(runner: CliRunner, args, print_result=False, env=None) -> Result:
    result = runner.invoke(cli, args, env=_home(env))
    if print_result:
        print("result.output", result.exit_code, result.output)
    assert not result.exception, "\n".join(traceback.format_exception(*result.exc_info))  # type: ignore
    assert result.exit_code == 0, result
    return result


def run_job_cmd(
    runner: CliRunner,
    args=(
        "-vvv",
        "deploy",
    ),
    starttime=1,
    print_result=False,
    env=None,
) -> Tuple[Result, Job, dict]:
    _args = list(args)
    if starttime:
        _args.append(f"--starttime={starttime}")
    result = run_cmd(runner, _args, print_result, env)
    assert _latestJobs
    job = _latestJobs[-1]
    summary = job.json_summary()
    return result, job, summary


_N = TypeVar("_N", bound=tosca.Namespace)


def runtime_test(namespace: Type[_N]) -> _N:
    return create_runner(namespace)[0]


def create_runner(namespace: Type[_N]) -> Tuple[_N, "Runner"]:
    from .job import Runner

    converter = PythonToYaml(namespace.get_defs())
    doc = converter.module2yaml(True)
    # pprint.pprint(doc)
    config = dict(
        apiVersion="unfurl/v1alpha1", kind="Ensemble", spec=dict(service_template=doc)
    )
    manifest = YamlManifest(config)
    assert manifest.rootResource
    # a plan is needed to create the instances
    runner = Runner(manifest)
    job = runner.static_plan()
    assert manifest.rootResource.attributeManager
    # make sure we share the change_count
    ctx = manifest.rootResource.attributeManager._get_context(manifest.rootResource)
    clone = namespace()
    node_templates = {
        t._name: (python_name, t)
        for python_name, t in namespace.get_defs().items()
        if isinstance(t, tosca.NodeType)
    }
    count = 0
    for r in manifest.rootResource.get_self_and_descendants():
        if r.name in node_templates:
            python_name, t = node_templates[r.name]
            proxy = proxy_instance(r, t.__class__, ctx)
            assert proxy._obj is t  # make sure it found this template
            setattr(clone, python_name, proxy)
            count += 1
    assert count == len(node_templates), f"{count}, {len(node_templates)}"
    assert tosca.global_state.mode == "runtime"
    tosca.global_state.context = ctx
    return clone, runner
