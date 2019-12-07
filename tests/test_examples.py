import unittest
import six
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions, Status
from unfurl.configurator import Configurator


class HelmConfigurator(Configurator):
    def run(self, task):
        assert task.inputs["chart"] == "gitlab/gitlab"
        assert task.inputs["flags"] == [{"repo": "https://charts.gitlab.io/"}]
        subtaskRequest = task.createSubTask("instantiate")
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
        yield task.createResult(True, True, Status.ok)


class RunTest(unittest.TestCase):
    def test_manifest(self):
        manifest = YamlManifest(path=__file__ + "/../examples/helm-manifest.yaml")
        runner = Runner(manifest)
        self.assertEqual(runner.lastChangeId, 0, "expected new manifest")
        output = six.StringIO()
        job = runner.run(JobOptions(add=True, out=output, startTime="test"))
        assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()

        # manifest shouldn't have changed
        manifest2 = YamlManifest(output.getvalue())
        output2 = six.StringIO()
        job2 = Runner(manifest2).run(
            JobOptions(add=True, out=output2, startTime="test")
        )
        # print('2', output2.getvalue())
        assert not job2.unexpectedAbort, job2.unexpectedAbort.getStackTrace()

        # should not find any tasks to run
        assert len(job2.workDone) == 0, job2.workDone
        self.maxDiff = None
        self.assertEqual(output.getvalue(), output2.getvalue())
