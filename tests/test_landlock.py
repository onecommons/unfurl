"""Tests for docker/aux/run-landlocked, run in the server image's base image."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import pytest

IMAGE = "python:3.13-slim"
SCRIPT = Path(__file__).parent.parent / "docker" / "aux" / "run-landlocked"
CLONE_ROOT = "/clones"


def _docker_available() -> bool:
    if "docker" in os.getenv("UNFURL_TEST_SKIP", "") or not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "version"], capture_output=True).returncode == 0


pytestmark = [
    pytest.mark.skipif(not _docker_available(), reason="docker isn't available"),
    pytest.mark.timeout(120),
]


def run(
    script: str,
    env: Optional[Dict[str, str]] = None,
    mounts: Optional[List[str]] = None,
) -> "subprocess.CompletedProcess[str]":
    """Run ``script`` with sh in the image, as root, with run-landlocked on PATH."""
    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{SCRIPT}:/usr/local/bin/run-landlocked:ro",
        "-e",
        f"UNFURL_CLONE_ROOT={CLONE_ROOT}",
    ]
    for name, value in (env or {}).items():
        cmd += ["-e", f"{name}={value}"]
    for mount in mounts or []:
        cmd += ["-v", mount]
    cmd += [IMAGE, "sh", "-c", f"mkdir -p {CLONE_ROOT} /unlisted; {script}"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=110)


@pytest.fixture(scope="module")
def landlock() -> None:
    probe = run(
        "setpriv --landlock-access fs --landlock-rule path-beneath:execute,read-file:/usr -- true"
    )
    if probe.returncode:
        pytest.skip(f"no Landlock in the docker kernel: {probe.stderr}")


def test_writes(landlock: None) -> None:
    result = run(
        "run-landlocked sh -c '"
        f"echo x > {CLONE_ROOT}/f && echo clone-root-ok; "
        "echo x > /tmp/f && echo tmp-ok; "
        "echo x > /etc/f || echo etc-denied; "
        'echo x > "$HOME/f" || echo home-denied; '
        "echo x > /unlisted/f || echo unlisted-denied'"
    )
    assert result.stdout.split() == [
        "clone-root-ok",
        "tmp-ok",
        "etc-denied",
        "home-denied",
        "unlisted-denied",
    ], result.stderr


def test_extra_writable_paths(landlock: None) -> None:
    result = run(
        "mkdir -p /extra1 /extra2; run-landlocked sh -c "
        "'echo x > /extra1/f && echo x > /extra2/f && echo extra-ok'",
        env={"UNFURL_LANDLOCK_RW": "/extra1::/extra2:/missing"},
    )
    assert result.stdout.split() == ["extra-ok"], result.stderr


def test_reads(landlock: None) -> None:
    result = run(
        "mkdir -p /run/secrets && echo s > /run/secrets/key && "
        "run-landlocked sh -c '"
        "cat /run/secrets/key || echo secret-denied; "
        "cat /etc/hostname > /dev/null && echo hostname-ok'"
    )
    assert result.stdout.split() == ["secret-denied", "hostname-ok"], result.stderr


def test_warns_of_readable_key(landlock: None) -> None:
    result = run(
        "run-landlocked true",
        env={"UNFURL_GRANT_KEY_FILE": "/etc/unfurl/grant-key/key"},
    )
    assert result.returncode == 0
    assert result.stderr.splitlines() == [
        "run-landlocked: warning: UNFURL_GRANT_KEY_FILE is readable under /etc"
    ]
    result = run(
        "run-landlocked true",
        env={"UNFURL_GRANT_KEY_FILE": "/run/secrets/grant-key"},
    )
    assert result.returncode == 0
    assert result.stderr == ""


def test_symlink_in_clone_root(landlock: None) -> None:
    result = run(
        f"run-landlocked sh -c 'mkdir {CLONE_ROOT}/a && "
        f"ln -s ../a {CLONE_ROOT}/link && echo symlink-ok'"
    )
    assert result.stdout.split() == ["symlink-ok"], result.stderr


def test_child_inherits(landlock: None) -> None:
    result = run(
        "run-landlocked python -c 'import subprocess; "
        'subprocess.run(["sh", "-c", "echo x > /etc/f || echo child-denied"], '
        "stderr=subprocess.DEVNULL)'"
    )
    assert result.stdout.split() == ["child-denied"], result.stderr


def test_fallback_without_landlock(tmp_path: Path) -> None:
    fake = tmp_path / "setpriv"
    fake.write_text("#!/bin/sh\necho 'setpriv: landlock unsupported' >&2\nexit 1\n")
    fake.chmod(0o755)
    result = run(
        "PATH=/fake:$PATH run-landlocked sh -c 'echo x > /etc/f && echo unrestricted'",
        mounts=[f"{tmp_path}:/fake:ro"],
    )
    assert result.stdout.split() == ["unrestricted"]
    warnings = [line for line in result.stderr.splitlines() if "warning" in line]
    assert warnings == [
        "run-landlocked: warning: running without Landlock: setpriv: landlock unsupported"
    ]


def test_without_truncate(landlock: None, tmp_path: Path) -> None:
    fake = tmp_path / "setpriv"
    fake.write_text(
        '#!/bin/sh\ncase "$*" in *truncate*) exit 1 ;; esac\nexec /usr/bin/setpriv "$@"\n'
    )
    fake.chmod(0o755)
    result = run(
        "PATH=/fake:$PATH run-landlocked sh -c '"
        f"echo x > {CLONE_ROOT}/f && echo clone-root-ok; "
        "echo x > /etc/f || echo etc-denied'",
        mounts=[f"{tmp_path}:/fake:ro"],
    )
    assert result.stdout.split() == ["clone-root-ok", "etc-denied"]
    assert "warning" not in result.stderr


def test_disabled() -> None:
    result = run(
        "run-landlocked sh -c 'echo x > /etc/f && echo unrestricted'",
        env={"UNFURL_LANDLOCK": "0"},
    )
    assert result.stdout.split() == ["unrestricted"]
    assert result.stderr == ""
