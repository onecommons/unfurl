import os
import shutil
import signal
import time
import unittest
import urllib.request
from pathlib import Path

from click.testing import CliRunner

from unfurl.job import JobOptions, Runner
from unfurl.support import Status
from unfurl.yamlmanifest import YamlManifest

from .utils import isolated_lifecycle


class SupervisorTest(unittest.TestCase):
    def test_supervisor(self):
        cli_runner = CliRunner()
        with cli_runner.isolated_filesystem():
            src_path = Path(__file__).parent / "examples" / "supervisor-ensemble.yaml"
            path = shutil.copy(src_path, ".")
            runner = Runner(YamlManifest(path=path))
            try:
                job = runner.run(JobOptions(startTime=1, check=True))  # deploy
                assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
                summary = job.json_summary()
                # print(json.dumps(summary, indent=2))
                self.assertEqual(
                    {
                        "id": "A01110000000",
                        "status": "ok",
                        "total": 5,
                        "ok": 5,
                        "error": 0,
                        "unknown": 0,
                        "skipped": 0,
                        "changed": 4,
                    },
                    summary["job"],
                )

                time.sleep(0.25)
                f = urllib.request.urlopen("http://127.0.0.1:8012/")
                expected = b"Directory listing for /"
                self.assertIn(expected, f.read())

                runner = Runner(YamlManifest(path=path))
                job = runner.run(JobOptions(workflow="undeploy", startTime=2))
                assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
                summary = job.json_summary()
                # print(json.dumps(summary, indent=2))
                self.assertEqual(
                    {
                        "id": "A01120000000",
                        "status": "ok",
                        "total": 3,
                        "ok": 3,
                        "error": 0,
                        "unknown": 0,
                        "skipped": 0,
                        "changed": 3,
                    },
                    summary["job"],
                )
            finally:
                # NOTE: to manually kill: pkill -lf supervisord
                if os.path.exists("supervisord/local/supervisord.pid"):
                    with open("supervisord/local/supervisord.pid") as f:
                        pid = int(f.read())
                        print("killing", pid)
                        os.kill(pid, signal.SIGINT)


class TestSupervisorLifecycle:
    def test_lifecycle(self):
        src_path = str(Path(__file__).parent / "examples" / "supervisor-ensemble.yaml")
        jobs = isolated_lifecycle(src_path)
        try:
            for job in jobs:
                # print("JOB", job.workflow, job.json_summary())
                assert job.status == Status.ok, job.workflow
        finally:
            # NOTE: to manually kill: pkill -lf supervisord
            if os.path.exists("supervisord/local/supervisord.pid"):
                with open("supervisord/local/supervisord.pid") as f:
                    pid = int(f.read())
                    print("killing", pid)
                    os.kill(pid, signal.SIGINT)
