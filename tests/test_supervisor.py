import os
import shutil
import signal
import time
import unittest
import urllib.request
from pathlib import Path
import json

from click.testing import CliRunner

from unfurl.job import JobOptions, Runner
from unfurl.support import Status
from unfurl.yamlmanifest import YamlManifest

from .utils import isolated_lifecycle

SAVE_TMP = os.getenv("UNFURL_TEST_TMPDIR")


class SupervisorTest(unittest.TestCase):
    @staticmethod
    def _kill_supervisord(base_dir: str = "."):
        """Kill supervisord if running, using pid file or subprocess scan."""
        pid_path = os.path.join(base_dir, "supervisord/local/supervisord.pid")
        if os.path.exists(pid_path):
            try:
                with open(pid_path) as f:
                    pid = int(f.read().strip())
                print("killing supervisord pid", pid)
                os.kill(pid, signal.SIGTERM)
                # Wait for it to actually exit
                for _ in range(20):
                    try:
                        os.kill(pid, 0)  # check if still alive
                        time.sleep(0.1)
                    except OSError:
                        break
                else:
                    os.kill(pid, signal.SIGKILL)
            except (OSError, ValueError) as e:
                print(f"cleanup: ignoring error killing supervisord: {e}")

    def test_supervisor(self):
        cli_runner = CliRunner()
        with cli_runner.isolated_filesystem(SAVE_TMP) as dir:
            if SAVE_TMP:
                print("running in", dir)
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
                        "changed": 4,  # create operation doesn't set modified
                    },
                    summary["job"],
                )

                retries = 0
                f = None
                while retries < 5:
                  try:
                      time.sleep(0.20)
                      f = urllib.request.urlopen("http://127.0.0.1:8012/", timeout=.20)
                  except urllib.error.URLError:
                      retries += 1
                  else:
                      break

                assert f, retries
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
                self._kill_supervisord()


class TestSupervisorLifecycle:
    def test_lifecycle(self):
        src_path = str(Path(__file__).parent / "examples" / "supervisor-ensemble.yaml")
        jobs = isolated_lifecycle(src_path)
        try:
            for job in jobs:
                assert job.status == Status.ok, job.workflow
        finally:
            SupervisorTest._kill_supervisord()
