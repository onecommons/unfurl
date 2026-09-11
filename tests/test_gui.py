"""The gui server must work in an unfurl project that has no ensembles.

`_get_repo` used to route every clone through
`Manifest.find_or_clone_from_url`, which needs a manifest to build its
import resolver -- so a project without one could not resolve a project
path at all. The replacement resolves package rules off the environment
context directly; these tests pin that the rules are still applied,
since that was the only reason the manifest path was used here.
"""

import os
import subprocess
from typing import Optional

import pytest

from unfurl.localenv import LocalEnv
from unfurl.server import gui
from unfurl.server.serve import app


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
