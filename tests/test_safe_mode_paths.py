"""Safe mode must not let a checkout reach outside the project.

A repository can carry a symlink pointing outside itself; cloning or
pulling it would otherwise make an arbitrary file readable through the
checked-out tree.
"""

import os
import subprocess

import pytest

# unfurl puts the vendored toscaparser on sys.path, so it must be imported first
from unfurl.repo import Repo
from unfurl.yamlloader import ImportResolver
from toscaparser.common.exception import ExceptionCollector


def _repo_with_symlink(path, target="/etc/passwd"):
    """A repo holding a symlink out of the tree, plus a regular file."""
    path.mkdir(parents=True, exist_ok=True)

    def run(*a):
        subprocess.run(a, cwd=str(path), check=True, capture_output=True)

    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "test@example.com")
    run("git", "config", "user.name", "Test")
    (path / "real.txt").write_text("hello\n")
    os.symlink(target, str(path / "escape.txt"))
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "initial")
    return str(path)


def test_clone_without_symlinks_checks_out_a_regular_file(tmp_path):
    src = _repo_with_symlink(tmp_path / "src")
    dest = str(tmp_path / "clone")

    repo = Repo.create_working_dir(src, dest, symlinks=False)

    escape = os.path.join(dest, "escape.txt")
    assert not os.path.islink(escape), "symlink should not have been created"
    # core.symlinks=false checks the link out as a file holding its target
    # path, so the target's *contents* are never exposed.
    assert open(escape).read() == "/etc/passwd"
    assert open(os.path.join(dest, "real.txt")).read() == "hello\n"
    # persisted, so a later checkout here can't reintroduce one: the clone
    # environment governs only the clone's own checkout
    assert repo.repo.git.config("--local", "--get", "core.symlinks") == "false"


def test_clone_with_symlinks_is_the_default(tmp_path):
    """The before-state: without the flag the escape is real.

    Keeps the test above honest -- if cloning never produced a symlink
    here, it would pass for the wrong reason.
    """
    src = _repo_with_symlink(tmp_path / "src")
    dest = str(tmp_path / "clone")

    Repo.create_working_dir(src, dest)

    escape = os.path.join(dest, "escape.txt")
    assert os.path.islink(escape)
    assert os.readlink(escape) == "/etc/passwd"


def test_pull_in_safe_mode_disables_symlinks(tmp_path, monkeypatch):
    """`pull()` persists core.symlinks=false, covering later checkouts too."""
    import tosca.loader
    from unfurl.server.serve import pull

    src_path = tmp_path / "src"
    src = _repo_with_symlink(src_path)
    dest = str(tmp_path / "clone")
    # cloned *before* safe mode, the way a long-lived server cache would be
    repo = Repo.create_working_dir(src, dest)
    assert os.path.islink(os.path.join(dest, "escape.txt"))

    # a second symlink lands upstream after the clone
    os.symlink("/etc/hosts", str(src_path / "escape2.txt"))
    for a in (
        ("git", "add", "-A"),
        ("git", "commit", "-q", "-m", "second"),
    ):
        subprocess.run(a, cwd=src, check=True, capture_output=True)

    monkeypatch.setattr(tosca.loader, "FORCE_SAFE_MODE", "1")
    pull(repo, "main")

    assert (
        repo.repo.git.config("--get", "core.symlinks") == "false"
    ), "pull should have persisted the setting"
    escape2 = os.path.join(dest, "escape2.txt")
    assert os.path.exists(escape2), "the pull should have brought the new file"
    assert not os.path.islink(escape2)
    assert open(escape2).read() == "/etc/hosts"
