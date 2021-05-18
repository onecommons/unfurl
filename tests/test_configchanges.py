import unittest
import os
import traceback
import json
from click.testing import CliRunner
from unfurl.__main__ import cli, _latestJobs
from unfurl.localenv import LocalEnv

version1 = """
  apiVersion: unfurl/v1alpha1
  kind: Ensemble
  spec:
    service_template:
      types:
        nodes.Test:
          derived_from: tosca.nodes.Root
          interfaces:
           Standard:
            operations:
              create:
                implementation: Template
                inputs:
                  done:
                    status: ok
      topology_template:
        node_templates:
          node2: {type: nodes.Test}
          node1:
            type: tosca.nodes.Root
            interfaces:
             Standard:
              operations:
                create:
                  implementation: Template
                  inputs:
                    done:
                      status: ok
                configure:
                  implementation: Template
                  inputs:
                    input1:
                      get_env: envvar1
                    done:
                      status: ok
                      result:
                        outputVar: "{{ inputs.input1 }}"
                    resultTemplate: |
                      - name: .self
                        attributes:
                          outputVar: "{{ outputVar }}"
"""


class ConfigChangeTest(unittest.TestCase):
    def test_config_change(self):
        """
        Test changing a configuration causes it to be rerun.
        """
        runner = CliRunner()
        with runner.isolated_filesystem():
            # override home so to avoid interferring with other tests
            result = runner.invoke(cli, ["--home", "./unfurl_home", "init", "--mono"])

            # uncomment this to see output:
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            assert os.path.isdir("./unfurl_home"), "home project not created"
            assert os.path.isfile(
                "./unfurl_home/unfurl.yaml"
            ), "home unfurl.yaml not created"

            with open("ensemble/ensemble.yaml", "w") as f:
                f.write(version1)
            args = [
                #  "-vvv",
                "--home",
                "./unfurl_home",
                "deploy",
                "--starttime=1",
                "--dirty=ok",
                "--commit",
                "--jobexitcode",
                "degraded",
            ]
            envVars = dict(envvar1="1")
            result = runner.invoke(cli, args, env=envVars)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)
            # print("result.output", result.exit_code, result.output)

            assert _latestJobs
            job = _latestJobs[-1]
            summary = job.jsonSummary()
            # print(json.dumps(summary, indent=2))
            # print(job.out.getvalue())
            self.assertEqual(
                {
                    "id": "A01100000000",
                    "status": "ok",
                    "total": 3,
                    "ok": 3,
                    "error": 0,
                    "unknown": 0,
                    "skipped": 0,
                    "changed": 2,
                },
                summary["job"],
            )

            result = runner.invoke(cli, args, env=envVars)
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)

            # Nothing changed so no jobs should run
            assert _latestJobs
            job = _latestJobs[-1]
            summary = job.jsonSummary()
            # print("no change")
            # print(json.dumps(summary, indent=2))
            # print(job.out.getvalue())
            self.assertEqual(
                {
                    "id": "A01100GC0000",
                    "status": "ok",
                    "total": 0,
                    "ok": 0,
                    "error": 0,
                    "unknown": 0,
                    "skipped": 0,
                    "changed": 0,
                },
                summary["job"],
            )

            # change the environment variable that an input depends on
            # this should trigger the configuration operation to be rerun

            envVars["envvar1"] = "2"
            result = runner.invoke(cli, args, env=envVars)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)
            # print("result.output", result.exit_code, result.output)

            assert _latestJobs
            job = _latestJobs[-1]
            summary = job.jsonSummary()
            # print("reconfigure")
            # print(json.dumps(summary, indent=2))
            # print(job.out.getvalue())
            self.assertEqual(
                {
                    "id": "A01100GC0000",
                    "status": "ok",
                    "total": 1,
                    "ok": 1,
                    "error": 0,
                    "unknown": 0,
                    "skipped": 0,
                    "changed": 1,
                },
                summary["job"],
            )
            self.assertEqual("reconfigure", summary["tasks"][0]["reason"])
