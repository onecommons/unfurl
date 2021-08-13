import shutil
from dataclasses import dataclass
from typing import Optional, Iterable

from click.testing import CliRunner

from unfurl.job import Runner, JobOptions
from unfurl.manifest import Manifest
from unfurl.support import Status
from unfurl.yamlmanifest import YamlManifest


@dataclass
class Step:
    workflow: str
    target_status: Status


DEFAULT_STEPS = (
    Step("check", Status.absent),
    Step("deploy", Status.ok),
    Step("check", Status.ok),
    Step("deploy", Status.ok),
    Step("undeploy", Status.absent),
    Step("check", Status.absent),
)


def lifecycle(manifest: Manifest, steps: Optional[Iterable[Step]] = DEFAULT_STEPS):
    runner = Runner(manifest)
    for i, step in enumerate(steps, start=1):
        step_str = f"#{i} - {step.workflow}"
        job = runner.run(JobOptions(workflow=step.workflow))
        assert job.status == Status.ok, step_str
        # instance_count = (
        #     len(job.rootResource.descendents) - 3
        # )  # minus root, input, output
        # assert job.task_count == instance_count, step_str
        for task in job.workDone.values():
            assert task.target.status == step.target_status, step_str
        yield job


def isolated_lifecycle(path: str, steps: Optional[Iterable[Step]] = DEFAULT_STEPS):
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem():
        path = shutil.copy(path, ".")
        manifest = YamlManifest(path=path)
        yield from lifecycle(manifest, steps)
