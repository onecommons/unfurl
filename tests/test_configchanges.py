import unittest
import os
import json
import traceback
from click.testing import CliRunner
from unfurl.__main__ import cli, _latestJobs
from unfurl.configurator import Configurator


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
            properties:
              outputVar: unset
            interfaces:
             Standard:
              operations:
                create:
                  implementation: Template
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
  changes: [] # add so changes are saved here
"""


class SpecChangeConfigurator(Configurator):
    def run(self, task):
        assert self.can_run(task)
        # access the property through task.vars to test that accesses are also tracked that way
        prop = task.vars["SELF"]["testProperty"]
        yield task.done(True, result=prop)


spec = """\
tosca_definitions_version: tosca_simple_unfurl_1_0_0
node_types:
  test.nodes.TestPropertyChange:
    derived_from: tosca.nodes.SoftwareComponent
    properties:
      testProperty:
        type: string
        default: %s
    interfaces:
      Standard:
        operations:
          configure:
            implementation:
              className: SpecChange
"""

specChangeManifest = """\
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    imports:
      - file: ../spec.yaml
    topology_template:
      node_templates:
        node1:
          type: test.nodes.TestPropertyChange
changes: [] # add so changes are saved here
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
            changes = {"::node1": {"outputVar": "1"}}
            assert _latestJobs
            job = _latestJobs[-1]
            self.assertEqual(
                changes, job.manifest.manifest.config["changes"][2]["changes"]
            )

            assert _latestJobs
            job = _latestJobs[-1]
            summary = job.json_summary()
            # print("deployed")
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
            # print("result.output1", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)

            # Nothing changed so no jobs should run
            assert _latestJobs
            job = _latestJobs[-1]
            summary = job.json_summary()
            # print("no change")
            # print(json.dumps(summary, indent=2))
            # print(job.out.getvalue())
            # with open("ensemble/jobs.tsv") as f:
            #     print(f.read())
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
            # print("result.output2", result.exit_code, result.output)

            assert _latestJobs
            job = _latestJobs[-1]
            summary = job.json_summary()
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
            changes2 = {"::node1": {"outputVar": "2"}}
            self.assertEqual(
                changes2, job.manifest.manifest.config["changes"][-1]["changes"]
            )

    def test_spec_change(self):
        """
        Test changing a node template causes it to be rerun.
        """
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["--home", "./unfurl_home", "init", "--mono"])
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )

            with open("ensemble/ensemble.yaml", "w") as f:
                f.write(specChangeManifest)
            with open("spec.yaml", "w") as f:
                f.write(spec % "A")
            args = [
                #  "-vvv",
                "--home",
                "./unfurl_home",
                "deploy",
                "--starttime=1",
                "--jobexitcode",
                "degraded",
            ]
            result = runner.invoke(cli, args)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)
            # print("result.output 1", result.exit_code, result.output)
            changes = {"::node1": {"outputVar": "1"}}
            assert _latestJobs
            job = _latestJobs[-1]
            changes = job.manifest.manifest.config["changes"][0]
            assert changes["result"] == "A"
            assert changes["digestKeys"] == "::node1::testProperty"
            assert changes["digestValue"] == "6dcd4ce23d88e2ee9568ba546c007c63d9131c1b"

            assert _latestJobs
            job = _latestJobs[-1]
            summary = job.json_summary()
            # print("deployed")
            # print(json.dumps(summary, indent=2))
            # print(job.out.getvalue())
            self.assertEqual(
                {
                    "id": "A01100000000",
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

            # update the property in the spec
            # this should trigger the configuration operation to be rerun
            with open("spec.yaml", "w") as f:
                f.write(spec % "B")

            result = runner.invoke(cli, args)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)
            # print("result.output 2", result.exit_code, result.output)

            assert _latestJobs
            job = _latestJobs[-1]
            summary = job.json_summary()
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
                    "changed": 0,
                },
                summary["job"],
            )
            self.assertEqual("reconfigure", summary["tasks"][0]["reason"])
            changes = job.manifest.manifest.config["changes"][1]
            assert changes["result"] == "B"
            assert changes["digestKeys"] == "::node1::testProperty"
            assert changes["digestValue"] == "ae4f281df5a5d0ff3cad6371f76d5c29b6d953ec"
