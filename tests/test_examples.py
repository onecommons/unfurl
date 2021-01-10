import unittest
import six
import os
import os.path
import json
from click.testing import CliRunner
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions, Status
from unfurl.configurator import Configurator
from unfurl.configurators import TemplateConfigurator

# python 2.7 needs these:
from unfurl.configurators.shell import ShellConfigurator
from unfurl.configurators.ansible import AnsibleConfigurator
from unfurl.configurators.k8s import ClusterConfigurator


class HelmConfigurator(Configurator):
    def run(self, task):
        assert task.inputs["chart"] == "gitlab/gitlab"
        assert task.inputs["flags"] == {"repo": "https://charts.gitlab.io/"}
        subtaskRequest = task.createSubTask("Install.subtaskOperation")
        assert subtaskRequest
        assert (
            subtaskRequest.configSpec
            and subtaskRequest.configSpec.className == "DummyShell"
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


class DummyShellConfigurator(TemplateConfigurator):
    def run(self, task):
        yield task.done(True, True)


class RunTest(unittest.TestCase):
    @unittest.skipIf("k8s" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
    def test_manifest(self):
        path = __file__ + "/../examples/helm-manifest.yaml"
        manifest = YamlManifest(path=path)
        runner = Runner(manifest)

        assert not manifest.lastJob, "expected new manifest"
        output = six.StringIO()  # so we don't save the file
        job = runner.run(JobOptions(add=True, out=output, startTime="test"))
        assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()

        # manifest shouldn't have changed
        # print("1", output.getvalue())
        manifest2 = YamlManifest(output.getvalue())
        # manifest2.statusSummary()
        output2 = six.StringIO()
        job2 = Runner(manifest2).run(JobOptions(add=True, out=output2, startTime=1))
        # print("2", output2.getvalue())
        # print(job2.summary())
        assert not job2.unexpectedAbort, job2.unexpectedAbort.getStackTrace()
        # should not have found any tasks to run:
        assert len(job2.workDone) == 0, job2.workDone
        self.maxDiff = None
        # self.assertEqual(output.getvalue(), output2.getvalue())

        output3 = six.StringIO()
        manifest3 = YamlManifest(output2.getvalue())
        job3 = Runner(manifest3).run(
            JobOptions(workflow="undeploy", out=output3, startTime=2)
        )
        # print(output3.getvalue())
        # only the chart delete task should have ran as it owns the resources it created
        # print(job3.jsonSummary())
        assert len(job3.workDone) == 1, job3.jsonSummary()
        tasks = list(job3.workDone.values())
        assert tasks[0].target.status.name == "absent", tasks[0].target.status

    @unittest.skipIf("k8s" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
    def test_discover(self):
        path = __file__ + "/../examples/helm-manifest.yaml"
        manifest = YamlManifest(path=path)
        runner = Runner(manifest)
        assert not manifest.lastJob, "expected new manifest"
        output = six.StringIO()  # so we don't save the file
        job = runner.run(JobOptions(workflow="discover", out=output, startTime=1))
        # print(job.summary())
        # print("discovered", runner.manifest.tosca.discovered)
        # print("discovered manifest", output.getvalue())
        assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()

        manifest2 = YamlManifest(output.getvalue())
        manifest2.manifest.path = os.path.abspath(
            path
        )  # set the basedir which sets the current working dir
        # manifest2.statusSummary()
        output2 = six.StringIO()
        job2 = Runner(manifest2).run(
            JobOptions(workflow="discover", out=output2, startTime=2)
        )
        # print("2", output2.getvalue())
        # print('job2', job2.summary())
        assert not job2.unexpectedAbort, job2.unexpectedAbort.getStackTrace()
        # print("job", json.dumps(job2.jsonSummary(), indent=2))
        # should not have found any tasks to run:
        assert len(job2.workDone) == 8, list(job2.workDone)

        # XXX this diff works if change log is ignored
        # self.maxDiff = None
        # self.assertEqual(output.getvalue(), output2.getvalue())

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
                # verify we wrote out the correct ansible inventory file
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
  children:
    example_group:
      hosts: {}
      vars:
        var_for_ansible_playbook: test
      children: {}
"""
                    with inventory.open() as f:
                        self.assertEqual(f.read(), expectedInventory)
                except ImportError:
                    pass  # skip on 2.7
        finally:
            os.environ["UNFURL_TMPDIR"] = oldTmpDir
        tasks = list(job.workDone.values())
        self.assertEqual(len(tasks), 1)
        assert "stdout" in tasks[0].result.result, tasks[0].result.result
        self.assertEqual(tasks[0].result.result["stdout"], "foo")

    def test_remote(self):
        """
        test that ansible is invoked when operation_host is remote
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
                        workflow="deploy",
                        # this instance doesn't exist so warning is output
                        instance="test_remote",
                        out=output,
                    )
                )
                output = six.StringIO()  # so we don't save the file
        finally:
            os.environ["UNFURL_TMPDIR"] = oldTmpDir
        tasks = list(job.workDone.values())
        self.assertEqual(len(tasks), 1)
        assert "stdout" in tasks[0].result.result, tasks[0].result.result
        self.assertEqual(tasks[0].result.result["stdout"], "bar")
        self.assertEqual(
            job.jsonSummary()["tasks"],
            [
                {
                    "status": "ok",
                    "target": "test_remote",
                    "operation": "configure",
                    "template": "test_remote",
                    "type": "tosca.nodes.Root",
                    "targetStatus": "ok",
                    "changed": True,
                    "configurator": "unfurl.configurators.ansible.AnsibleConfigurator",
                    "priority": "required",
                    "reason": "add",
                }
            ],
        )
