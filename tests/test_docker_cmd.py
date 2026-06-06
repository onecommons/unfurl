import getpass
import os
import traceback
import unittest
from pathlib import Path

import pytest
from click.testing import CliRunner

from unfurl import __version__, version_tuple
from unfurl.__main__ import _args, cli, DockerCmd


def _clear_ci_env(monkeypatch):
    """Remove CI markers so DockerCmd's auto-detection sees a clean env.

    Tests pin ci_mode behaviour explicitly (via `--ci` / `--no-ci` / set
    CI=true via monkeypatch) and shouldn't accidentally flip based on the
    host's actual CI env (running pytest under GitHub Actions, etc.).
    """
    for v in ("CI",) + DockerCmd._CI_VENDOR_ENV:
        monkeypatch.delenv(v, raising=False)


class TestDockerCmd:
    def test_parse_image(self):
        assert (
            DockerCmd.parse_image("docker", "0.2.1")
            == "ghcr.io/onecommons/unfurl:0.2.1"
        )
        assert (
            DockerCmd.parse_image("docker:unfurl_local", "0.2.1")
            == "unfurl_local:0.2.1"
        )
        assert (
            DockerCmd.parse_image("docker:onecommons/unfurl:0.2.0", "0.2.1")
            == "onecommons/unfurl:0.2.0"
        )
        assert (
            DockerCmd.parse_image("docker --privileged", "0.2.1")
            == "ghcr.io/onecommons/unfurl:0.2.1"
        )

    def test_parse_docker_arrgs(self):
        assert DockerCmd.parse_docker_args("docker") == []
        assert DockerCmd.parse_docker_args("docker:unfurl_local") == []
        assert DockerCmd.parse_docker_args("docker:onecommons/unfurl:0.2.0") == []
        assert DockerCmd.parse_docker_args("docker --privileged") == ["--privileged"]
        assert DockerCmd.parse_docker_args("docker --privileged -e A=B") == [
            "--privileged",
            "-e",
            "A=B",
        ]

    def test_build(self, monkeypatch):
        _clear_ci_env(monkeypatch)
        monkeypatch.setattr(os, "getuid", lambda: 1000)
        monkeypatch.setattr(os, "getgid", lambda: 1000)
        monkeypatch.setattr(getpass, "getuser", lambda: "joe")
        monkeypatch.setattr(Path, "home", lambda: "/home/joe")
        monkeypatch.setattr(Path, "cwd", lambda: "/home/joe/project")

        cmd = DockerCmd("docker --privileged", {"ANSWER": 42}).build()
        tag = "latest" if len(version_tuple()) > 3 else __version__()
        assert (
            " ".join(cmd)
            == "docker run --rm -w /data -u 1000:1000 -e HOME=/home/joe -e USER=joe -e ANSWER=42 "
            "-v /home/joe/project:/data -v /home/joe:/home/joe "
            "-v /var/run/docker.sock:/var/run/docker.sock "
            f"--privileged ghcr.io/onecommons/unfurl:{tag} unfurl --no-runtime"
        )

    def test_build_ci_mode(self, monkeypatch):
        """`--ci` swaps to host-path mounts + injected identity env vars.

        The container layout pivots: cwd mounted at the same path inside (so
        host-resolved paths stay valid), HOME=/tmp and USER=unfurl injected
        so `--user`'s passwd-less uid doesn't crash ansible/getpass, git
        identity + init.defaultBranch=main passed via env vars (no readable
        ~/.gitconfig inside), no home dir mount, explicit
        `--entrypoint unfurl`. Designed for CI runners — see DockerCmd's
        docstring for the full survival-kit rationale.
        """
        _clear_ci_env(monkeypatch)
        monkeypatch.setattr(os, "getuid", lambda: 1000)
        monkeypatch.setattr(os, "getgid", lambda: 1000)
        monkeypatch.setattr(Path, "cwd", lambda: "/work/project")

        cmd = DockerCmd("docker:my/image:tag --ci", {"FOO": "bar"}).build()
        assert (
            " ".join(cmd)
            == "docker run --rm -v /work/project:/work/project -w /work/project "
            "-u 1000:1000 "
            "-e HOME=/tmp -e USER=unfurl "
            "-e GIT_AUTHOR_NAME=unfurl -e GIT_AUTHOR_EMAIL=unfurl@example.com "
            "-e GIT_COMMITTER_NAME=unfurl -e GIT_COMMITTER_EMAIL=unfurl@example.com "
            "-e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=init.defaultBranch "
            "-e GIT_CONFIG_VALUE_0=main "
            "-e FOO=bar "
            "-v /var/run/docker.sock:/var/run/docker.sock "
            "--entrypoint unfurl my/image:tag --no-runtime"
        )
        # --ci is consumed (not forwarded as a `docker run` flag).
        assert "--ci" not in cmd

    def test_ci_auto_detected_from_ci_env(self, monkeypatch):
        """`CI=true` in the env flips to ci_mode without an explicit `--ci`."""
        _clear_ci_env(monkeypatch)
        monkeypatch.setenv("CI", "true")
        assert DockerCmd("docker:my/image:tag", {}).ci_mode is True

    def test_ci_auto_detected_from_vendor_env(self, monkeypatch):
        """A vendor-specific marker (e.g. GITHUB_ACTIONS) is enough."""
        _clear_ci_env(monkeypatch)
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        assert DockerCmd("docker:my/image:tag", {}).ci_mode is True

    def test_no_ci_overrides_env(self, monkeypatch):
        """`--no-ci` wins even when CI markers are present in the env."""
        _clear_ci_env(monkeypatch)
        monkeypatch.setenv("CI", "true")
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        cmd = DockerCmd("docker:my/image:tag --no-ci", {})
        assert cmd.ci_mode is False
        # `--no-ci` is consumed, not forwarded to `docker run`.
        assert "--no-ci" not in cmd.docker_args

    def test_explicit_ci_beats_no_env(self, monkeypatch):
        """`--ci` wins even when no CI markers are set."""
        _clear_ci_env(monkeypatch)
        assert DockerCmd("docker:my/image:tag --ci", {}).ci_mode is True

    def test_clean_env_keeps_default_mode(self, monkeypatch):
        """Without any CI markers or flags, default (developer) mode wins."""
        _clear_ci_env(monkeypatch)
        # `CI=false` shouldn't activate (we only treat truthy values as on).
        monkeypatch.setenv("CI", "false")
        assert DockerCmd("docker:my/image:tag", {}).ci_mode is False


@unittest.skipIf(
    "slow" in os.getenv("UNFURL_TEST_SKIP", "")
    or "docker" in os.getenv("UNFURL_TEST_SKIP", ""),
    "UNFURL_TEST_SKIP set",
)
class TestDockerRuntime(unittest.TestCase):
    """End-to-end: `--runtime=docker:...` dispatches to a real docker run.

    Requires a reachable docker daemon. Skipped via UNFURL_TEST_SKIP when
    one isn't available.
    """

    # Locally the happy path is ~17s; the 90s timeout tolerates a cold
    # image pull on a fresh runner without exceeding pytest-timeout's
    # default. `reruns=2` rides over transient registry/daemon flakiness
    # without paying the worst case three times in a row.
    @pytest.mark.timeout(90)
    @pytest.mark.flaky(reruns=2, reruns_delay=5)
    def test_docker_runtime(self):
        ensemble = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
configurations:
  create:
    implementation:
      className: unfurl.configurators.shell.ShellConfigurator
    inputs:
      command: "echo hello world"
spec:
  service_template:
    topology_template:
      node_templates:
        test1:
          type: tosca.nodes.Root
          interfaces:
            Standard:
              +/configurations:
"""
        runner = CliRunner()
        # ghcr.io is the default registry (multi-arch builds land there per
        # dev_images.yml), but the current `:latest` predates the arm64 work
        # so we still need `--platform=linux/amd64` to run on arm64 hosts.
        # Drop the platform pin once a multi-arch release has been published
        # at ghcr.io/onecommons/unfurl:latest.
        runtime = "docker:ghcr.io/onecommons/unfurl:latest --platform=linux/amd64"
        cli_args = [
            "--runtime=" + runtime,
            "--no-version-check",
            "deploy",
            # CliRunner.invoke has no tty, so the unfurl running inside the
            # container would block on `yesno("proceed with job?")` and crash
            # on /dev/tty. --approve short-circuits that prompt.
            "--approve",
            "ensemble.yaml",
        ]
        _args[:] = cli_args

        with runner.isolated_filesystem():
            with open("ensemble.yaml", "w") as f:
                f.write(ensemble)
            try:
                if os.environ.get("UNFURL_NORUNTIME"):
                    del os.environ["UNFURL_NORUNTIME"]
                result = runner.invoke(cli, cli_args)
            finally:
                os.environ["UNFURL_NORUNTIME"] = "1"

        assert not result.exception, "\n".join(
            traceback.format_exception(*result.exc_info)
        )
        assert result.exit_code == 0, result.stderr
        self.assertIn("running remote with _args", result.output)
