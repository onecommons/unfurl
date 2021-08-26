import shutil
from dataclasses import dataclass
from typing import Optional, Iterable

from click.testing import CliRunner

from unfurl.job import Runner, JobOptions, Job
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


def lifecycle(
    manifest: Manifest, steps: Optional[Iterable[Step]] = DEFAULT_STEPS
) -> Iterable[Job]:
    runner = Runner(manifest)
    has_default_steps = steps == DEFAULT_STEPS
    for i, step in enumerate(steps, start=1):
        step_str = f"#{i} - {step.workflow}"
        job = runner.run(JobOptions(workflow=step.workflow))
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        assert job.status == Status.ok, f"{step_str} is {job.status.name}"
        # XXX if has_default_steps and i == 3 check that no changes were made
        # XXX if has_default_steps and i == 4 check that no tasks ran
        for task in job.workDone.values():
            assert (
                task.target.status == step.target_status
            ), f"Step: {step_str}, status: {task.target.status.name} should be {step.target_status.name} for {task.target.name}"
        yield job


def isolated_lifecycle(
    path: str, steps: Optional[Iterable[Step]] = DEFAULT_STEPS
) -> Iterable[Job]:
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem():
        path = shutil.copy(path, ".")
        # XXX this should save and reload for each step using the cli
        manifest = YamlManifest(path=path)
        yield from lifecycle(manifest, steps)
