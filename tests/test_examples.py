import unittest
import six
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
        assert len(job3.workDone) == 3, job3.jsonSummary()

    def test_ansible(self):
        """
        Run ansible command on a (mock) remote instance.
        """
        path = __file__ + "/../examples/ansible-manifest.yaml"
        manifest = YamlManifest(path=path)
        runner = Runner(manifest)

        output = six.StringIO()  # so we don't save the file
        job = runner.run(
            JobOptions(
                workflow="run",
                host="www.example.com",
                instance="www.example.com",
                cmdline=["echo", "foo"],
                out=output,
            )
        )
        assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
        assert list(job.workDone.values())[0].result.result["stdout"] == "foo"
