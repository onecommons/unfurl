"""``unfurl clone cloudmap:instantiation:<url>`` clones the ensemble if its
repository is recorded as public, otherwise reconstructs it from the
instantiation record."""

from pathlib import Path

import git
import pytest
from click.testing import CliRunner

from unfurl.__main__ import cli

from unfurl.cloudmap.db import CloudMapDB
from unfurl.localenv import LocalEnv
from unfurl.tosca_plugins.cloudmap_defs import Instantiation, Repository, section_of
from unfurl.util import API_VERSION, change_cwd
from unfurl.yamlloader import yaml
from tests.utils import init_project, run_cmd

ENSEMBLE_TEMPLATE = """\
apiVersion: unfurl/v1.0.0
spec:
  service_template:
    topology_template:
      node_templates:
        from_blueprint:
          type: tosca.nodes.Root
"""

UNFURL_YAML = """\
apiVersion: unfurl/v1.0.0
kind: Project
environments:
  defaults:
    cloudmaps:
      repositories:
        cloudmap:
          url: {cloudmap_path}
"""


def _git_repo(path: Path, files: dict) -> None:
    repo = git.Repo.init(path, initial_branch="main")
    for name, contents in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(contents)
    repo.index.add(list(files))
    repo.index.commit("initial commit")


def _git_url(path: Path) -> str:
    return "git://" + str(path)


@pytest.fixture
def workspace(tmp_path: Path):
    runner = CliRunner()
    with change_cwd(str(tmp_path)):
        init_project(
            runner,
            args=["init", "--mono", "--empty", "--var", "vaultid", "", "project"],
            env=dict(UNFURL_HOME=""),
        )
    project = tmp_path / "project"
    # local repositories are used in place, and have to be inside the project
    blueprint = project / "blueprint"
    _git_repo(blueprint, {"ensemble-template.yaml": ENSEMBLE_TEMPLATE})
    cloudmap_path = project / "cloudmap.yaml"
    (project / "unfurl.yaml").write_text(
        UNFURL_YAML.format(cloudmap_path=cloudmap_path)
    )
    return runner, tmp_path, blueprint, project, cloudmap_path


def _write_cloudmap(path: Path, *records) -> None:
    doc: dict = {"apiVersion": API_VERSION, "kind": "CloudMap"}
    for record in records:
        doc.setdefault(section_of(record), {})[record.key] = record.asdict()
    with open(path, "w") as f:
        yaml.dump(doc, f)


def _clone(runner: CliRunner, cwd: Path, source: str) -> Path:
    """Clone ``source`` into the project and return the new ensemble's path."""
    project = cwd / "project"
    before = set(project.glob("ensemble*/ensemble.yaml"))
    with change_cwd(str(project)):
        run_cmd(runner, ["--home", "", "clone", source, "."])
    found = set(project.glob("ensemble*/ensemble.yaml")) - before
    assert len(found) == 1, found
    return found.pop()


def test_reconstruct_ensemble_from_instantiation(workspace):
    runner, tmp_path, blueprint, project, cloudmap_path = workspace
    ensemble_url = _git_url(project / "deployments") + "#:ensemble/ensemble.yaml"
    _write_cloudmap(
        cloudmap_path,
        Repository(
            url=_git_url(project / "deployments"), path="deployments", private=True
        ),
        Repository(url=_git_url(blueprint), path="blueprint", protocols=["file"]),
        Instantiation(
            url=ensemble_url,
            source=_git_url(blueprint) + "#:ensemble-template.yaml",
            source_ref="main",
        ),
    )
    ensemble_path = _clone(runner, tmp_path, "cloudmap:instantiation:" + ensemble_url)
    with open(ensemble_path) as f:
        doc = yaml.load(f)
    assert doc["+include-blueprint"] == {
        "file": "ensemble-template.yaml",
        "repository": "spec",
    }
    assert doc["spec"]["service_template"]["repositories"]["spec"] == {
        "url": "file://" + str(blueprint),
        "revision": "main",
    }
    manifest = LocalEnv(str(ensemble_path)).get_manifest(skip_validation=True)
    assert "from_blueprint" in manifest.tosca.template.topology_template.node_templates


def test_clone_public_instantiation(workspace):
    runner, tmp_path, blueprint, project, cloudmap_path = workspace
    # outside the project, like a remote repository
    deployments = tmp_path / "deployments"
    ensemble = f"""\
apiVersion: unfurl/v1.0.0
kind: Ensemble
+include-blueprint:
  file: ensemble-template.yaml
  repository: spec
spec:
  service_template:
    description: cloned from the public repository
    repositories:
      spec:
        url: file://{blueprint}
"""
    _git_repo(
        deployments,
        {
            "unfurl.yaml": "apiVersion: unfurl/v1.0.0\nkind: Project\n",
            "ensemble/ensemble.yaml": ensemble,
        },
    )
    ensemble_url = _git_url(deployments) + "#:ensemble/ensemble.yaml"
    _write_cloudmap(
        cloudmap_path,
        Repository(
            url=_git_url(deployments),
            path="deployments",
            protocols=["file"],
            private=False,
        ),
        Instantiation(
            url=ensemble_url, source=_git_url(blueprint) + "#:ensemble-template.yaml"
        ),
    )
    ensemble_path = _clone(runner, tmp_path, "cloudmap:instantiation:" + ensemble_url)

    # forked from the ensemble in the deployments repository, not reconstructed
    manifest = LocalEnv(str(ensemble_path)).get_manifest(skip_validation=True)
    assert manifest.tosca.template.description == "cloned from the public repository"
    assert "from_blueprint" in manifest.tosca.template.topology_template.node_templates


def test_unknown_instantiation_is_an_error(workspace):
    runner, tmp_path, blueprint, project, cloudmap_path = workspace
    _write_cloudmap(cloudmap_path)
    source = "cloudmap:instantiation:git:///nowhere#:e.yaml"
    with change_cwd(str(project)):
        result = runner.invoke(cli, ["--home", "", "clone", source, "."])
    assert result.exit_code != 0
    assert f'Could not find "{source}"' in str(result.exception)


def test_resolve_cloudmap_url():
    std = Repository(
        url="git://unfurl.cloud/onecommons/std.git",
        path="onecommons/std",
        protocols=["https"],
    )
    # not recorded as private is public
    public = Repository(url="git://example.com/public.git", path="public")
    private = Repository(
        url="git://example.com/private.git", path="private", private=True
    )
    public_inst = Instantiation(url="git://example.com/public.git#:ensemble.yaml")
    private_inst = Instantiation(url="git://example.com/private.git#:ensemble.yaml")
    db = CloudMapDB("", contents={}, validate=False)
    for record in (std, public, private, public_inst, private_inst):
        db.add_record(record)

    # the package id form
    assert (
        db.resolve_cloudmap_url("cloudmap:unfurl.cloud/onecommons/std.git#v1:path")
        == "https://unfurl.cloud/onecommons/std.git#v1:path"
    )
    assert (
        db.resolve_cloudmap_url("cloudmap:repository:git://unfurl.cloud/onecommons/std.git")
        == "https://unfurl.cloud/onecommons/std.git"
    )
    assert (
        db.resolve_cloudmap_url("cloudmap:artifact:[git://example.com/other.git#:a.yaml]")
        == "https://example.com/other.git#:a.yaml"
    )
    assert (
        db.resolve_cloudmap_url("cloudmap:instantiation:" + public_inst.url)
        == "https://example.com/public.git#:ensemble.yaml"
    )
    # private, or not in the cloudmap
    assert db.resolve_cloudmap_url("cloudmap:instantiation:" + private_inst.url) is None
    assert db.resolve_cloudmap_url("cloudmap:instantiation:git://x.com/y.git#:e.yaml") is None
    # a record type with no git url
    assert db.resolve_cloudmap_url("cloudmap:type:Foo@example.com") is None


def test_reconstruct_into_new_project(tmp_path: Path):
    runner = CliRunner()
    home = tmp_path / "home"
    with change_cwd(str(tmp_path)):
        run_cmd(runner, ["--home", str(home), "--no-runtime", "home", "--init"])
    cloudmap_path = home / "cloudmap.yaml"
    (home / "unfurl.yaml").write_text(UNFURL_YAML.format(cloudmap_path=cloudmap_path))
    # local repositories are used in place: the home project is allowed too
    blueprint = home / "blueprint"
    _git_repo(blueprint, {"ensemble-template.yaml": ENSEMBLE_TEMPLATE})
    ensemble_url = _git_url(tmp_path / "deployments") + "#:ensemble/ensemble.yaml"
    _write_cloudmap(
        cloudmap_path,
        Repository(url=_git_url(blueprint), path="blueprint", protocols=["file"]),
        Instantiation(
            url=ensemble_url, source=_git_url(blueprint) + "#:ensemble-template.yaml"
        ),
    )
    with change_cwd(str(tmp_path)):
        run_cmd(
            runner,
            ["--home", str(home), "clone", "cloudmap:instantiation:" + ensemble_url, "new"],
        )
    ensemble_path = tmp_path / "new" / "ensemble" / "ensemble.yaml"
    with open(ensemble_path) as f:
        doc = yaml.load(f)
    assert doc["+include-blueprint"]["repository"] == "spec"
    assert LocalEnv(str(ensemble_path)).project.projectRoot == str(tmp_path / "new")
