import shutil
from typing import Optional, Iterable

from click.testing import CliRunner

from unfurl.job import Runner, JobOptions
from unfurl.manifest import Manifest
from unfurl.yamlmanifest import YamlManifest

DEFAULT_STEPS = (
    "check",
    "deploy",
    "check",
    "deploy",
    "undeploy",
    "check",
)


def lifecycle(manifest: Manifest, steps: Optional[Iterable] = DEFAULT_STEPS):
    runner = Runner(manifest)
    for step in steps:
        yield runner.run(JobOptions(workflow=step))


def isolated_lifecycle(path: str, steps: Optional[Iterable] = None):
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem():
        path = shutil.copy(path, ".")
        manifest = YamlManifest(path=path)
        yield from lifecycle(manifest, steps)
