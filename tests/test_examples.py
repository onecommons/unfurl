import sys
import unittest
import io
import os
import os.path
import shutil
import json
from click.testing import CliRunner
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions
from unfurl.configurator import Configurator
from unfurl.configurators import TemplateConfigurator
from unfurl.util import make_temp_dir
from unfurl.support import Status
from .utils import isolated_lifecycle, DEFAULT_STEPS, Step


class HelmConfigurator(Configurator):
    def run(self, task):
        assert task.inputs["chart"] == "gitlab/gitlab"
        assert task.inputs["flags"] == {"repo": "https://charts.gitlab.io/"}
        subtaskRequest = task.create_sub_task("Install.subtaskOperation")
        assert subtaskRequest
        assert (
            subtaskRequest.configSpec
            and subtaskRequest.configSpec.className
            == "tests.test_examples.DummyShellConfigurator"
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
    def setUp(self):
        os.environ["UNFURL_WORKDIR"] = make_temp_dir(delete=True)
        self.maxDiff = None

    def tearDown(self):
        del os.environ["UNFURL_WORKDIR"]

    @unittest.skipIf("k8s" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
    def test_manifest(self):
        path = __file__ + "/../examples/mock-helm-ensemble.yaml"
        manifest = YamlManifest(path=path)
        runner = Runner(manifest)

        assert not manifest.lastJob, "expected new manifest"
        output = io.StringIO()  # so we don't save the file
        job = runner.run(JobOptions(add=True, out=output, startTime=1))
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
        # print(manifest.statusSummary())
        # check that the set-state step ran, it sets "created" here:
        assert job.rootResource.find_instance("gitlab-release").created
        summary = job.json_summary()
        tasks = summary.pop("tasks")
        summary.pop("external_jobs")
        self.assertEqual(summary,
            {
                "job": {
                    "id": "A01110000000",
                    "status": "ok",
                    "total": 10,
                    "ok": 5,
                    "error": 0,
                    "unknown": 0,
                    "skipped": 5,
                    "changed": 5,
                },
                "outputs": {},
            })
        self.assertEqual([t for t in tasks if t["status"]],
        [
                    {
                        "status": "ok",
                        "target": "stagingCluster",
                        "operation": "discover",
                        "template": "stagingCluster",
                        "type": "unfurl.nodes.K8sCluster",
                        "targetStatus": "ok",
                        "targetState": None,
                        "changed": True,
                        "configurator": "unfurl.configurators.k8s.ClusterConfigurator",
                        "priority": "required",
                        "reason": "discover",
                    },
                    {
                        "status": "ok",
                        "target": "defaultNamespace",
                        "operation": "discover",
                        "template": "defaultNamespace",
                        "type": "unfurl.nodes.K8sNamespace",
                        "targetStatus": "ok",
                        "targetState": None,
                        "changed": True,
                        "configurator": "unfurl.configurators.k8s.ResourceConfigurator",
                        "priority": "required",
                        "reason": "discover",
                    },
                    {
                        "status": "ok",
                        "target": "gitlab-release",
                        "operation": "execute",
                        "template": "gitlab-release",
                        "type": "unfurl.nodes.HelmRelease",
                        "targetStatus": "pending",
                        "targetState": None,
                        "changed": True,
                        "configurator": "tests.test_examples.HelmConfigurator",
                        "priority": "required",
                        "reason": "step:helm",
                    },
                    {
                        "status": "ok",
                        "target": "gitlab-release",
                        "operation": "subtaskOperation",
                        "template": "gitlab-release",
                        "type": "unfurl.nodes.HelmRelease",
                        "targetStatus": "pending",
                        "targetState": None,
                        "changed": True,
                        "configurator": "tests.test_examples.DummyShellConfigurator",
                        "priority": "required",
                        "reason": "subtask: for step:helm: unfurl.interfaces.install.Helm.execute",
                    },
                    {
                        "status": "ok",
                        "target": "gitlab-release",
                        "operation": "discover",
                        "template": "gitlab-release",
                        "type": "unfurl.nodes.HelmRelease",
                        "targetStatus": "ok",
                        "targetState": None,
                        "changed": True,
                        "configurator": "unfurl.configurators.shell.ShellConfigurator",
                        "priority": "required",
                        "reason": "step:helm",
                    },
                ])
        # manifest shouldn't have changed
        # print("1", output.getvalue())
        baseDir = __file__ + "/../examples/"
        manifest2 = YamlManifest(output.getvalue(), path=baseDir)
        # print(manifest2.statusSummary())
        output2 = io.StringIO()
        job2 = Runner(manifest2).run(JobOptions(add=True, out=output2, startTime=2))
        # print("2", output2.getvalue())
        # print("2", job2.json_summary(True))
        # print(job2._json_plan_summary(True))
        assert not job2.unexpectedAbort, job2.unexpectedAbort.get_stack_trace()
        # should not have found any tasks to run:
        assert len(job2.workDone) == 0, job2.workDone
        # self.assertEqual(output.getvalue(), output2.getvalue())

        output3 = io.StringIO()
        manifest3 = YamlManifest(output2.getvalue(), path=baseDir)
        job3 = Runner(manifest3).run(
            JobOptions(workflow="undeploy", out=output3, startTime=2)
        )
        # print(output3.getvalue())
        # only the chart delete gitlab-release should have ran as it owns the resources it created
        # print(job3.json_summary(True))
        assert len(job3.workDone) == 1, job3.json_summary()
        tasks = list(job3.workDone.values())
        assert tasks[0].target.status.name == "absent", tasks[0].target.status

    @unittest.skipIf("k8s" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
    def test_discover(self):
        path = __file__ + "/../examples/mock-helm-ensemble.yaml"
        manifest = YamlManifest(path=path)
        runner = Runner(manifest)
        assert not manifest.lastJob, "expected new manifest"
        output = io.StringIO()  # so we don't save the file
        job = runner.run(JobOptions(workflow="discover", out=output, startTime=1))
        # print(job.summary())
        # print("discovered", runner.manifest.tosca.discovered)
        # print("discovered manifest", output.getvalue())
        assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()

        baseDir = __file__ + "/../examples/"
        manifest2 = YamlManifest(output.getvalue(), path=baseDir)
        manifest2.manifest.path = os.path.abspath(
            path
        )  # set the basedir which sets the current working dir
        # manifest2.statusSummary()
        output2 = io.StringIO()
        job2 = Runner(manifest2).run(
            JobOptions(workflow="discover", out=output2, startTime=2)
        )
        # print("2", output2.getvalue())
        # print('job2', job2.summary())
        assert not job2.unexpectedAbort, job2.unexpectedAbort.get_stack_trace()
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
        runner = CliRunner()
        with runner.isolated_filesystem() as tempDir:
            srcpath = os.path.join(
                os.path.dirname(__file__), "examples", "ansible-ensemble.yaml"
            )
            path = shutil.copy(srcpath, ".")
            manifest = YamlManifest(path=path)
            runner = Runner(manifest)

            output = io.StringIO()  # so we don't save the file
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
            assert not job.unexpectedAbort, job.unexpectedAbort.get_stack_trace()
            # verify we wrote out the correct ansible inventory file
            try:
                from pathlib import Path

                p = Path(os.environ["UNFURL_WORKDIR"])

                files = list(p.glob("**/*inventory.yml"))
                self.assertEqual(len(files), 1, files)
                inventory = files[-1]
                expectedInventory = """all:
  hosts:
    www.example.com:
      ansible_port: 22
      ansible_connection: local
      ansible_user: ubuntu
      ansible_pipelining: false
      ansible_private_key_file: ~/.ssh/example-key.pem
      ansible_python_interpreter: %s
  vars: {}
  children:
    example_group:
      hosts: {}
      vars:
        var_for_ansible_playbook: test
      children: {}
""" % sys.executable
                with inventory.open() as f:
                    inventory_contents = f.read()
                    assert inventory_contents == expectedInventory
            except ImportError:
                pass  # skip on 2.7
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
                path = __file__ + "/../examples/ansible-ensemble.yaml"
                manifest = YamlManifest(path=path)
                runner = Runner(manifest)

                output = io.StringIO()  # so we don't save the file
                job = runner.run(
                    JobOptions(
                        workflow="deploy",
                        # this instance doesn't exist so warning is output
                        instance="test_remote",
                        out=output,
                    )
                )
                output = io.StringIO()  # so we don't save the file
        finally:
            os.environ["UNFURL_TMPDIR"] = oldTmpDir
        tasks = list(job.workDone.values())
        self.assertEqual(len(tasks), 1)
        assert "stdout" in tasks[0].result.result, tasks[0].result.result
        self.assertEqual(tasks[0].result.result["stdout"], "bar")
        self.assertEqual(
            job.json_summary()["tasks"],
            [
                {
                    "status": "ok",
                    "target": "test_remote",
                    "operation": "configure",
                    "template": "test_remote",
                    "targetState": "configured",
                    "type": "tosca.nodes.Root",
                    "targetStatus": "ok",
                    "changed": True,
                    "configurator": "unfurl.configurators.ansible.AnsibleConfigurator",
                    "priority": "required",
                    "reason": "add",
                }
            ],
        )

# XXX
@unittest.skipIf(os.getenv("CI"), reason="container started exiting with -1 on Git Actions for unknown reason")
@unittest.skipIf("docker" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
def test_unfurl_site_examples():
    steps = list(DEFAULT_STEPS[1:])  # XXX fix docker check and enable the first step
    list(
        isolated_lifecycle(
            "unfurl_site",
            steps=steps,
            init_args="clone https://github.com/onecommons/unfurl_site.git".split(),
        )
    )
