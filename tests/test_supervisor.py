import os
import signal
import time
import unittest
import json
import shutil

from click.testing import CliRunner
from six.moves import urllib

import unfurl.configurators  # python2.7 workaround
import unfurl.configurators.shell  # python2.7 workaround
import unfurl.configurators.supervisor  # python2.7 workaround
from unfurl.job import JobOptions, Runner
from unfurl.yamlmanifest import YamlManifest


class SupervisorTest(unittest.TestCase):
    def test_supervisor(self):
        cliRunner = CliRunner()
        with cliRunner.isolated_filesystem():
            srcpath = os.path.join(
                os.path.dirname(__file__), "examples", "supervisor-ensemble.yaml"
            )
            path = shutil.copy(srcpath, ".")
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
                print(json.dumps(summary, indent=2))
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
