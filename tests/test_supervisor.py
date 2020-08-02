import unittest
import os
import json
import time
from six.moves import urllib
from click.testing import CliRunner

import unfurl.configurators  # python2.7 workaround
import unfurl.configurators.shell  # python2.7 workaround
import unfurl.configurators.supervisor  # python2.7 workaround
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions

manifest = """\
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    imports:
      - repository: unfurl
        file: configurators/supervisor-template.yaml

    topology_template:
      node_templates:
        # localhost:
        #   directives:
        #   - select
        #   type: tosca.nodes.Compute

        supervisord:
          type: unfurl.nodes.Supervisor
          properties:
            homeDir: . # unix socket paths can't be > 100 characters
          # requirements:
          # - host: localhost

        unfurl_run_runner:
          type: unfurl.nodes.ProcessController.Supervisor
          properties:
            name: test
            program:
              command: python3 -m http.server -b 127.0.0.1 8012
              redirect_stderr: true
              stdout_logfile: '%(here)s/test.log'
          requirements:
          - host: supervisord
"""


class SupervisorTest(unittest.TestCase):
    def test_supervisor(self):
        cliRunner = CliRunner()
        with cliRunner.isolated_filesystem():
            runner = Runner(YamlManifest(manifest))

            job = runner.run(JobOptions(startTime=1))  # deploy
            assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
            summary = job.jsonSummary()
            self.assertEqual(
                {
                    "id": "A01110000000",
                    "status": "ok",
                    "total": 4,
                    "ok": 4,
                    "error": 0,
                    "unknown": 0,
                    "skipped": 0,
                    "changed": 3,
                },
                summary["job"],
            )

            # print(json.dumps(summary, indent=2))
            time.sleep(0.25)
            f = urllib.request.urlopen("http://127.0.0.1:8012/")
            expected = b"Directory listing for /"
            self.assertIn(expected, f.read())

            runner = Runner(YamlManifest(job.out.getvalue()))
            job = runner.run(JobOptions(workflow="undeploy", startTime=2))
            assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
            summary = job.jsonSummary()
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
