import unittest
import six
import os
from click.testing import CliRunner
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions, Status
from unfurl.configurator import Configurator


class HelmConfigurator(Configurator):
    def run(self, task):
        assert task.inputs["chart"] == "gitlab/gitlab"
        assert task.inputs["flags"] == {"repo": "https://charts.gitlab.io/"}
        subtaskRequest = task.createSubTask("Install.subtaskOperation")
        assert subtaskRequest
        assert (
            subtaskRequest.configSpec
            and subtaskRequest.configSpec.className == "DummyShellConfigurator"
        ), subtaskRequest.configSpec.className
        subtask = yield subtaskRequest
        assert subtask.inputs["helmcmd"] == "install"
        assert subtask.inputs["chart"] == "gitlab/gitlab"
        assert subtask.status == subtask.status.ok, subtask.status.name
        assert subtask.result.modified, subtask.result

        # subtaskRequest2 = task.createSubTask("discover")
        # subtask2 = yield subtaskRequest2
        # assert subtask2.status == Status.ok, subtask2.status.name
        yield subtask.result


class DummyShellConfigurator(Configurator):
    def run(self, task):
        yield task.done(True, Status.ok)


class RunTest(unittest.TestCase):
    def test_manifest(self):
        path = __file__ + "/../examples/helm-manifest.yaml"
        manifest = YamlManifest(path=path)
        runner = Runner(manifest)
        self.assertEqual(runner.lastChangeId, 0, "expected new manifest")
        output = six.StringIO()  # so we don't save the file
        job = runner.run(JobOptions(add=True, out=output, startTime="test"))
        assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()

        # manifest shouldn't have changed
        # print("1", output.getvalue())
        manifest2 = YamlManifest(output.getvalue())
        # manifest2.statusSummary()
        output2 = six.StringIO()
        job2 = Runner(manifest2).run(
            JobOptions(add=True, out=output2, startTime="test")
        )
        # print("2", output2.getvalue())
        # print(job2.summary())
        assert not job2.unexpectedAbort, job2.unexpectedAbort.getStackTrace()
        # should not have found any tasks to run:
        assert len(job2.workDone) == 0, job2.workDone
        self.maxDiff = None
        self.assertEqual(output.getvalue(), output2.getvalue())

        output3 = six.StringIO()
        manifest3 = YamlManifest(output2.getvalue())
        job3 = Runner(manifest3).run(
            JobOptions(workflow="undeploy", out=output3, startTime="test")
        )
        # two delete tasks should have ran
        assert len(job3.workDone) == 2, job3.jsonSummary()

    def test_ansible(self):
        """
        Run ansible command on a (mock) remote instance.
        """
        try:
            oldTmpDir = os.environ["UNFURL_TMPDIR"]
            runner = CliRunner()
            with runner.isolated_filesystem() as tempDir:
                os.environ["UNFURL_TMPDIR"] = tempDir
                path = __file__ + "/../examples/ansible-manifest.yaml"
                manifest = YamlManifest(path=path)
                runner = Runner(manifest)

                output = six.StringIO()  # so we don't save the file
                job = runner.run(
                    JobOptions(
                        workflow="run",
                        host="www.example.com",
                        # this instance doesn't exist so warning is output
                        instance="www.example.com",
                        cmdline=["echo", "foo"],
                        out=output,
                    )
                )
                assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
                try:
                    from pathlib import Path

                    p = Path(os.environ["UNFURL_TMPDIR"])
                    files = list(p.glob("**/*-inventory.yaml"))
                    self.assertEqual(len(files), 1, files)
                    inventory = files[-1]
                    expectedInventory = """all:
  hosts:
    www.example.com:
      ansible_port: 22
      ansible_connection: local
      ansible_user: ubuntu
      ansible_pipelining: yes
      ansible_private_key_file: ~/.ssh/example-key.pem
  vars: {}
  children: {}
"""
                    with inventory.open() as f:
                        self.assertEqual(f.read(), expectedInventory)
                except ImportError:
                    pass
        finally:
            os.environ["UNFURL_TMPDIR"] = oldTmpDir
        tasks = list(job.workDone.values())
        self.assertEqual(len(tasks), 1)
        assert "stdout" in tasks[0].result.result, tasks[0].result.result
        self.assertEqual(tasks[0].result.result["stdout"], "foo")
