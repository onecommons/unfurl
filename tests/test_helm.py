import os
import os.path
import sys
import threading
import unittest
from functools import partial

from six.moves import urllib

from unfurl.job import JobOptions, Runner
from unfurl.yamlmanifest import YamlManifest


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
            if sys.version_info[0] >= 3:
                from http.server import HTTPServer, SimpleHTTPRequestHandler

                handler = partial(SimpleHTTPRequestHandler, directory=directory)
                self.httpd = HTTPServer(server_address, handler)
            else:  # for python 2.7
                import urllib

                import SocketServer
                from SimpleHTTPServer import SimpleHTTPRequestHandler

                class RootedHTTPRequestHandler(SimpleHTTPRequestHandler):
                    def translate_path(self, path):
                        path = os.path.normpath(urllib.unquote(path))
                        words = path.split("/")
                        words = filter(None, words)
                        path = directory
                        for word in words:
                            drive, word = os.path.splitdrive(word)
                            head, word = os.path.split(word)
                            if word in (os.curdir, os.pardir):
                                continue
                            path = os.path.join(path, word)
                        return path

                self.httpd = SocketServer.TCPServer(
                    server_address, RootedHTTPRequestHandler
                )
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

        summary = run1.json_summary()
        # print(runner.manifest.statusSummary())
        # print(run1.jsonSummary(True))
        # print(run1._jsonPlanSummary(True))

        if sys.version_info[0] < 3:
            return  # task order not guaranteed in python 2.7
        self.assertEqual(
            summary,
            {
                "job": {
                    "id": "A01110000000",
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
                        "configurator": "unfurl.configurators.DelegateConfigurator",
                        "priority": "required",
                        "reason": "add",
                    },
                    {
                        "status": "ok",
                        "target": "mysql_release",
                        "operation": "execute",
                        "template": "mysql_release",
                        "type": "unfurl.nodes.HelmRelease",
                        "targetStatus": "ok",
                        "targetState": "configuring",
                        "changed": True,
                        "configurator": "unfurl.configurators.shell.ShellConfigurator",
                        "priority": "required",
                        "reason": "for subtask: for add: Standard.configure",
                    },
                ],
            },
        )
        assert all(task["targetStatus"] == "ok" for task in summary["tasks"]), summary[
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

        runner = Runner(YamlManifest(self.manifest))
        run = runner.run(JobOptions(workflow="check", startTime=2))
        summary = run.json_summary()
        assert not run.unexpectedAbort, run.unexpectedAbort.get_stack_trace()

        # print("check")
        # print(runner.manifest.statusSummary())
        # print(run.jsonSummary(True))
        summary = run.json_summary()
        if sys.version_info[0] < 3:
            return  # task order not guaranteed in python 2.7
        self.assertEqual(
            summary,
            {
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
                        "configurator": "unfurl.configurators.DelegateConfigurator",
                        "priority": "required",
                        "reason": "check",
                    },
                    {
                        "status": "ok",
                        "target": "mysql_release",
                        "operation": "execute",
                        "template": "mysql_release",
                        "type": "unfurl.nodes.HelmRelease",
                        "targetStatus": "ok",
                        "targetState": None,
                        "changed": True,
                        "configurator": "unfurl.configurators.shell.ShellConfigurator",
                        "priority": "required",
                        "reason": "for subtask: for check: Install.check",
                    },
                ],
            },
        )
        # reuse the same runner because the manifest's status has been updated
        run2 = runner.run(
            JobOptions(workflow="undeploy", startTime=3, destroyunmanaged=True)
        )
        assert not run2.unexpectedAbort, run2.unexpectedAbort.get_stack_trace()
        # print("undeploy")
        # print(runner.manifest.statusSummary())
        # print(run2.jsonSummary(True))
        summary2 = run2.json_summary()

        # note: this test relies on stable_repo being place in the helm cache by test_deploy()
        # comment out the repository requirement to run this test standalone
        assert all(
            task["targetStatus"] == "absent" for task in summary2["tasks"]
        ), summary2["tasks"]
        self.assertEqual(
            summary2,
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
        )
