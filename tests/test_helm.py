import os
import os.path
import sys
import threading
import unittest
from functools import partial
import shutil
import urllib.request
from click.testing import CliRunner

from unfurl.job import JobOptions, Runner
from unfurl.yamlmanifest import YamlManifest
from unfurl.localenv import LocalEnv
from .utils import init_project

# http://localhost:8000/fixtures/helmrepo
@unittest.skipIf("helm" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
class HelmTest(unittest.TestCase):
    def setUp(self):
        self.maxDiff = None
        path = os.path.join(
            os.path.dirname(__file__), "examples", "helm-simple-ensemble.yaml"
        )
        with open(path) as f:
            self.manifest = f.read()

        server_address = ("", 8010)
        directory = os.path.dirname(__file__)
        try:
            from http.server import HTTPServer, SimpleHTTPRequestHandler

            handler = partial(SimpleHTTPRequestHandler, directory=directory)
            self.httpd = HTTPServer(server_address, handler)
        except:  # address might still be in use
            self.httpd = None
            return

        t = threading.Thread(name="http_thread", target=self.httpd.serve_forever)
        t.daemon = True
        t.start()

    def tearDown(self):
        if self.httpd:
            self.httpd.socket.close()

    def test_deploy(self):
        # make sure this works
        f = urllib.request.urlopen("http://localhost:8010/fixtures/helmrepo/index.yaml")
        f.close()

        runner = Runner(YamlManifest(self.manifest))
        run1 = runner.run(JobOptions(verbose=3, startTime=1))
        assert not run1.unexpectedAbort, run1.unexpectedAbort.get_stack_trace()
        mysql_release = runner.manifest.rootResource.find_resource("mysql_release")
        query = ".::.requirements::[.name=host]::.target::name"
        res = mysql_release.query(query)
        assert res == "unfurl-helm-unittest"
        
        # runner.manifest.save_job(run1)
        # print(run1.out.getvalue())

        summary = run1.json_summary()
        # print(run1.json_summary(True))
        # print(run1._jsonPlanSummary(True))

        self.assertEqual(summary["job"], {
                    "id": "A01110000000",
                    "status": "ok",
                    "total": 7,
                    "ok": 3,
                    "error": 0,
                    "unknown": 0,
                    "skipped": 4,
                    "changed": 3,
                    })
        self.assertEqual([t for t in summary["tasks"] if t["status"]], [
                    {
                        "status": "ok",
                        "target": "stable_repo",
                        "operation": "create",
                        "template": "stable_repo",
                        "type": "unfurl.nodes.HelmRepository",
                        "targetStatus": "ok",
                        "targetState": "created",
                        "changed": True,
                        "configurator": "unfurl.configurators.shell.ShellConfigurator",
                        "priority": "required",
                        "reason": "add",
                    },
                    {
                        "status": "ok",
                        "target": "k8sNamespace",
                        "operation": "configure",
                        "template": "k8sNamespace",
                        "type": "unfurl.nodes.K8sNamespace",
                        "targetStatus": "ok",
                        "targetState": "configured",
                        "changed": True,
                        "configurator": "unfurl.configurators.k8s.ResourceConfigurator",
                        "priority": "required",
                        "reason": "add",
                    },
                    {
                        "status": "ok",
                        "target": "mysql_release",
                        "operation": "configure",
                        "template": "mysql_release",
                        "type": "unfurl.nodes.HelmRelease",
                        "targetStatus": "ok",
                        "targetState": "configured",
                        "changed": True,
                        "configurator": "unfurl.configurators.shell.ShellConfigurator",
                        "priority": "required",
                        "reason": "add",
                    },
                ])
        assert all(task.get("targetStatus") == "ok" for task in summary["tasks"]), summary[
            "tasks"
        ]
        # runner.manifest.dump()

    def test_undeploy(self):
        ###########################################
        #### WARNING test_undeploy() will not succeed if called individually because test_deploy
        #### installs the chart repo in a tox's tmp directly and will be deleted after the test run
        #### so test_undeploy will not find it
        ###########################################

        # note! if tests fail may need to run:
        #      helm uninstall mysql-test -n unfurl-helm-unittest
        #  and kubectl delete namespace unfurl-helm-unittest

        cli_runner = CliRunner()
        with cli_runner.isolated_filesystem():
            src_path = os.path.join(
                os.path.dirname(__file__), "examples", "helm-simple-ensemble.yaml"
            )
            path = init_project(cli_runner, src_path)
            # init_project creates home at "./unfurl_home"
            runner = Runner(
                YamlManifest(localEnv=LocalEnv(path, homePath="./unfurl_home"))
            )
            run = runner.run(JobOptions(workflow="check", startTime=2))
            summary = run.json_summary()
            assert not run.unexpectedAbort, run.unexpectedAbort.get_stack_trace()

            # print("check")
            # print(runner.manifest.status_summary())
            # print(run.json_summary(True))
            summary = run.json_summary()
            tasks = summary.pop("tasks")
            self.assertEqual(summary, {
                    "external_jobs": [
                        {
                            "ensemble": summary["external_jobs"][0]["ensemble"],
                            "job": {
                                "id": "A01120000000",
                                "status": "ok",
                                "total": 4,
                                "ok": 4,
                                "error": 0,
                                "unknown": 0,
                                "skipped": 0,
                                "changed": 4,
                            },
                            "outputs": {},
                            "tasks": [
                                {
                                    "changed": True,
                                    "configurator": "unfurl.configurators.DelegateConfigurator",
                                    "operation": "configure",
                                    "priority": "required",
                                    "reason": "add",
                                    "status": "ok",
                                    "target": "__artifact__helm-artifacts--helm",
                                    "targetState": "configured",
                                    "targetStatus": "ok",
                                    "template": "__artifact__helm-artifacts--helm",
                                    "type": "unfurl.nodes.ArtifactInstaller",
                                },
                                {
                                    "changed": True,
                                    "configurator": "unfurl.configurators.shell.ShellConfigurator",
                                    "operation": "configure",
                                    "priority": "required",
                                    "reason": "subtask: for add: Standard.configure",
                                    "status": "ok",
                                    "target": "install",
                                    "targetState": None,
                                    "targetStatus": "unknown",
                                    "template": "install",
                                    "type": "artifact.AsdfTool",
                                },
                                { 'changed': True,
                                  'configurator': 'unfurl.configurators.DelegateConfigurator',
                                  'operation': 'check',
                                  'priority': 'required',
                                  'reason': 'check',
                                  'status': 'ok',
                                  'target': '__artifact__configurator-artifacts--kubernetes.core',
                                  'targetState': 'started',
                                  'targetStatus': 'ok',
                                  'template': '__artifact__configurator-artifacts--kubernetes.core',
                                  'type': 'unfurl.nodes.ArtifactInstaller'},
                               {'changed': True,
                                'configurator': 'unfurl.configurators.shell.ShellConfigurator',
                                'operation': 'check',
                                'priority': 'required',
                                'reason': 'subtask: for check: Install.check',
                                'status': 'ok',
                                'target': 'install',
                                'targetState': 'started',
                                'targetStatus': 'ok',
                                'template': 'install',
                                'type': 'artifact.AnsibleCollection'}
                            ],
                        }
                    ],
                    "job": {
                        "id": "A01120000000",
                        "status": "ok",
                        "total": 7,
                        "ok": 3,
                        "error": 0,
                        "unknown": 0,
                        "skipped": 4,
                        "changed": 3,
                    },
                    "outputs": {}
            })
            self.assertEqual([t for t in tasks if t["status"]],
                [
                    {
                        "status": "ok",
                        "target": "stable_repo",
                        "operation": "check",
                        "template": "stable_repo",
                        "type": "unfurl.nodes.HelmRepository",
                        "targetStatus": "ok",
                        "targetState": "started",
                        "changed": True,
                        "configurator": "unfurl.configurators.shell.ShellConfigurator",
                        "priority": "required",
                        "reason": "check",
                    },
                    {
                        "status": "ok",
                        "target": "k8sNamespace",
                        "operation": "check",
                        "template": "k8sNamespace",
                        "type": "unfurl.nodes.K8sNamespace",
                        "targetStatus": "ok",
                        "targetState": "started",
                        "changed": True,
                        "configurator": "unfurl.configurators.k8s.ResourceConfigurator",
                        "priority": "required",
                        "reason": "check",
                    },
                    {
                        "status": "ok",
                        "target": "mysql_release",
                        "operation": "check",
                        "template": "mysql_release",
                        "type": "unfurl.nodes.HelmRelease",
                        "targetStatus": "ok",
                        "targetState": "started",
                        "changed": True,
                        "configurator": "unfurl.configurators.shell.ShellConfigurator",
                        "priority": "required",
                        "reason": "check",
                    },
                ], tasks)

            # reuse the same runner because the manifest's status has been updated
            run2 = runner.run(
                JobOptions(workflow="undeploy", startTime=3, destroyunmanaged=True)
            )
            assert not run2.unexpectedAbort, run2.unexpectedAbort.get_stack_trace()
            # print("undeploy")
            # print(runner.manifest.status_summary())
            # print(run2.json_summary(True))
            summary2 = run2.json_summary()

            # note: this test relies on stable_repo being place in the helm cache by test_deploy()
            # comment out the repository requirement to run this test standalone
            # assert all(
            #     task["targetStatus"] == "absent" for task in summary2["tasks"]
            # ), list(summary2["tasks"])
            self.assertEqual(
                {
                    "job": {
                        "id": "A01130000000",
                        "status": "ok",
                        "total": 3,
                        "ok": 3,
                        "error": 0,
                        "unknown": 0,
                        "skipped": 0,
                        "changed": 3,
                    },
                    "outputs": {},
                    "tasks": [
                        {
                            "status": "ok",
                            "target": "mysql_release",
                            "operation": "delete",
                            "template": "mysql_release",
                            "type": "unfurl.nodes.HelmRelease",
                            "targetStatus": "absent",
                            "targetState": "deleted",
                            "changed": True,
                            "configurator": "unfurl.configurators.shell.ShellConfigurator",
                            "priority": "required",
                            "reason": "undeploy",
                        },
                        {
                            "status": "ok",
                            "target": "stable_repo",
                            "operation": "delete",
                            "template": "stable_repo",
                            "type": "unfurl.nodes.HelmRepository",
                            "targetStatus": "absent",
                            "targetState": "deleted",
                            "changed": True,
                            "configurator": "unfurl.configurators.shell.ShellConfigurator",
                            "priority": "required",
                            "reason": "undeploy",
                        },
                        {
                            "status": "ok",
                            "target": "k8sNamespace",
                            "operation": "delete",
                            "template": "k8sNamespace",
                            "type": "unfurl.nodes.K8sNamespace",
                            "targetStatus": "absent",
                            "targetState": "deleted",
                            "changed": True,
                            "configurator": "unfurl.configurators.k8s.ResourceConfigurator",
                            "priority": "required",
                            "reason": "undeploy",
                        },
                    ],
                },
                summary2,
            )
