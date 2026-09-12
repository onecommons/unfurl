"""How `unfurl serve --gui` resolves the projects it is asked for.

Two things it used to get wrong, both about where a repository is allowed
to live:

* a project with no ensembles could not resolve a project path at all,
  because `_get_repo` routed every clone through
  `Manifest.find_or_clone_from_url`, which needs a manifest to build its
  import resolver. Package rules are the only thing that path was wanted
  for here, so the replacement applies them off the environment context
  and these tests pin that they still are;
* a `localRepositories` entry pointing outside the served project was
  rejected rather than used. The project doing the serving is trusted and
  its entries apply; a project it serves is not, and gets `safe_mode`,
  where the same entry is ignored.
"""

import os
import subprocess
import urllib.parse
from typing import Optional

import pytest
import requests
from click.testing import CliRunner

from unfurl.localenv import LocalEnv
from git import Repo
from unfurl.repo import GitRepo
from unfurl.server import gui
from unfurl.server import serve
from unfurl.server.serve import app
from unfurl.yamlloader import yaml
from tests.utils import init_project

# the server fixtures live with the other server tests
from tests.test_server import HOST, _start_gui_server, _terminate_process


def _make_git_repo(path) -> str:
    """A real repo with one commit, usable as a clone source."""
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(a, cwd=str(path), check=True, capture_output=True)
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "test@example.com")
    run("git", "config", "user.name", "Test")
    (path / "README.md").write_text("source\n")
    run("git", "add", "README.md")
    run("git", "commit", "-q", "-m", "initial")
    return str(path)


def _localenv_without_ensemble(tmp_path, package_rules: Optional[str] = None):
    project = tmp_path / "project"
    project.mkdir()
    unfurl_yaml = project / "unfurl.yaml"
    variables = ""
    if package_rules:
        variables = (
            "\nenvironments:\n"
            "  defaults:\n"
            "    variables:\n"
            f"      UNFURL_PACKAGE_RULES: {package_rules!r}\n"
        )
    unfurl_yaml.write_text("apiVersion: unfurl/v1alpha1\nkind: Project\n" + variables)
    local_env = LocalEnv(str(unfurl_yaml), can_be_empty=True)
    assert not local_env.manifestPath, "fixture must have no ensemble"
    return local_env


def _server_context():
    """The minimum app config `get_remote_refs_cached` reads.

    `configure_app` sets these in a real server; a bare app context has
    neither, and the tag lookup raises KeyError before reaching git.
    """
    ctx = app.app_context()
    ctx.push()
    app.config.setdefault("UNFURL_LOCAL_PROJECTS", {})
    app.config["UNFURL_CLOUD_SERVER"] = "https://unfurl.cloud"
    app.config["CACHE_DEFAULT_REMOTE_TAGS_TIMEOUT"] = 300
    return ctx


def test_package_rule_rewrites_url_without_an_ensemble(tmp_path):
    """A rule pointing a package id at a local repo must be honoured.

    Asserting on the resolved working_dir rather than on "something was
    cloned": without the rule the url would stay `example.com/pkg` and
    resolve somewhere else entirely.
    """
    source = _make_git_repo(tmp_path / "source")
    local_env = _localenv_without_ensemble(
        tmp_path, package_rules=f"example.com/pkg file://{source}"
    )

    ctx = _server_context()
    try:
        repo = gui._find_or_clone_no_ensemble("https://example.com/pkg", local_env)
    finally:
        ctx.pop()

    assert repo is not None, "the package rule should have resolved to the local repo"
    assert os.path.realpath(repo.working_dir).startswith(
        os.path.realpath(str(tmp_path))
    ), repo.working_dir
    assert os.path.isfile(os.path.join(repo.working_dir, "README.md"))


def test_already_cloned_repo_is_found_without_network(tmp_path):
    """The find-first branch: an url already in workingDirs needs no clone."""
    source = _make_git_repo(tmp_path / "source")
    local_env = _localenv_without_ensemble(tmp_path)
    url = f"file://{source}"
    # register it the way a previous clone would have
    local_env.find_or_create_working_dir(url)

    ctx = _server_context()
    try:
        repo = gui._find_or_clone_no_ensemble(url, local_env)
    finally:
        ctx.pop()
    assert repo is not None
    assert os.path.isfile(os.path.join(repo.working_dir, "README.md"))


def test_to_environments_without_an_ensemble(tmp_path):
    """`to_environments` must work on a project that has no ensemble.

    It synthesizes a `YamlManifest` for that case, and a synthetic manifest
    has no `repo` -- so nothing in the shared export path may depend on
    one. This is the same project shape the gui serves above.
    """
    from unfurl.to_json import to_environments

    # An environment must be declared: to_environments loops over
    # project.contexts, so without one the synthetic-manifest branch is
    # never reached and this would pass vacuously.
    project = tmp_path / "project"
    project.mkdir()
    (project / "unfurl.yaml").write_text(
        "apiVersion: unfurl/v1alpha1\n"
        "kind: Project\n"
        "environments:\n"
        "  staging:\n"
        "    variables:\n"
        "      EXAMPLE: value\n"
    )
    local_env = LocalEnv(str(project / "unfurl.yaml"), can_be_empty=True)
    assert not local_env.manifestPath, "fixture must have no ensemble"

    db = to_environments(local_env)
    env = db["DeploymentEnvironment"]["staging"]
    # to_environments catches per-environment failures and stores
    # {"error": ...} under the same key, so presence proves nothing --
    # the absence of that key is what says the export actually ran.
    assert "error" not in env, env.get("details")
    assert env["name"] == "staging"
    assert "connections" in env and "instances" in env


def _add_local_repository(project_root: str, path: str, url: str) -> None:
    """Set a `localRepositories` entry in a project's unfurl.yaml.

    Loaded and re-dumped rather than appended: `unfurl init` leaves the file
    ending inside a commented-out `environments:` block with no trailing
    newline, so appended top-level keys get absorbed into it.
    """
    unfurl_yaml = os.path.join(project_root, "unfurl.yaml")
    with open(unfurl_yaml) as f:
        config = yaml.load(f)
    config.setdefault("localRepositories", {})[path] = {"url": url}
    with open(unfurl_yaml, "w") as f:
        yaml.dump(config, f)


def _read_log(path: str) -> str:
    with open(path, "r", errors="replace") as f:
        return f.read()


def test_gui_local_repository_outside_project():
    """`unfurl serve --gui` resolves a localRepositories entry outside the project.

    The scenario: a project served with --gui declares

        localRepositories:
          /path/to/onecommons/std:
            url: https://unfurl.cloud/onecommons/std

    and the browser asks for that project. This used to fail with
    "<path> not allowed outside of project" -- the path confinement applied
    to the project being served rather than to the one doing the serving.
    Now the entry is honoured and the local checkout is used instead of
    cloning.

    The other half is the contrast: an *upstream* project is untrusted, so
    `_make_readonly_localenv` gives it `safe_mode` and the same yaml in its
    own unfurl.yaml is ignored with a warning. The gui's own project is not
    loaded that way, so its entry still applies.

    `remote:<git-url>` is used as the project path because in gui mode it
    bypasses treating the id as a cloud-server project, which keeps this
    test off the network.
    """
    runner = CliRunner()
    p = None
    with runner.isolated_filesystem():
        try:
            # The repository the gui project points at. It is an unfurl
            # project itself, and carries the same kind of entry -- pointing
            # further out -- so the safe_mode branch has something to ignore.
            init_project(
                runner, args=["init", "--empty", "upstream"], env=dict(UNFURL_HOME="")
            )
            upstream_root = os.path.abspath("upstream")
            outside_upstream = os.path.abspath("elsewhere")
            os.makedirs(outside_upstream)
            _add_local_repository(
                upstream_root, outside_upstream, "https://example.com/elsewhere.git"
            )
            upstream_repo = GitRepo(Repo(upstream_root))
            upstream_repo.add_all(upstream_root)
            upstream_repo.commit("add localRepositories")

            # The project served with --gui, pointing at the one above.
            init_project(
                runner, args=["init", "--empty", "guiproject"], env=dict(UNFURL_HOME="")
            )
            gui_root = os.path.abspath("guiproject")
            _add_local_repository(gui_root, upstream_root, f"file://{upstream_root}")
            gui_repo = GitRepo(Repo(gui_root))
            gui_repo.add_all(gui_root)
            gui_repo.commit("add localRepositories")

            p, port = _start_gui_server(gui_root, name="localrepos")

            project_path = f"remote:file://{upstream_root}"
            url = (
                f"http://{HOST}:{port}/api/v4/projects/"
                f"{urllib.parse.quote(project_path, safe='/')}"
                "/repository/branches"
            )
            res = requests.get(url)
            assert res.status_code == 200, f"{res.status_code}: {res.text}"
            branches = res.json()
            assert branches, res.text
            assert branches[0]["commit"]["id"] == upstream_repo.revision

            log = _read_log(p._py_log_file)
            # the bug this fixes
            assert "not allowed outside of project" not in log, log[-4000:]
            # it used the checkout rather than cloning it
            assert not os.path.isdir(os.path.join(gui_root, "upstream")), (
                "should have resolved the local repository, not cloned"
            )
            # ...and the gui project's own entry was honoured, which is only
            # meaningful because the same yaml is ignored below.
            assert "Ignoring localRepositories entry" not in log, log[-4000:]

            # Now export the upstream project. `_make_readonly_localenv` loads
            # it with safe_mode, so *its* localRepositories entry -- pointing
            # at `elsewhere`, outside itself -- must be dropped with a warning.
            res = requests.get(
                f"http://{HOST}:{port}/export",
                params={"auth_project": project_path, "format": "blueprint"},
            )
            assert res.status_code == 200, f"{res.status_code}: {res.text}"
            log = _read_log(p._py_log_file)
            assert "Ignoring localRepositories entry outside of project" in log, log[
                -4000:
            ]
            assert outside_upstream in log, log[-4000:]
        finally:
            if p:
                _terminate_process(p)
