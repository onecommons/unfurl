import getpass
import os
from pathlib import Path

from unfurl import __version__
from unfurl.__main__ import DockerCmd


class TestDockerCmd:
    def test_parse_image(self):
        assert DockerCmd.parse_image("docker", "0.2.1") == "onecommons/unfurl:0.2.1"
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
            == "onecommons/unfurl:0.2.1"
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
        monkeypatch.setattr(os, "getuid", lambda: 1000)
        monkeypatch.setattr(os, "getgid", lambda: 1000)
        monkeypatch.setattr(getpass, "getuser", lambda: "joe")
        monkeypatch.setattr(Path, "home", lambda: "/home/joe")
        monkeypatch.setattr(Path, "cwd", lambda: "/home/joe/project")

        cmd = DockerCmd("docker --privileged", {"ANSWER": 42}).build()

        assert (
            " ".join(cmd)
            == "docker run --rm -w /data -u 1000:1000 -e HOME=/home/joe -e USER=joe -e ANSWER=42 "
            "-v /home/joe/project:/data -v /home/joe:/home/joe "
            "-v /var/run/docker.sock:/var/run/docker.sock "
            f"--privileged onecommons/unfurl:latest unfurl --no-runtime "
            f"--version-check {__version__(True)}"
        )
