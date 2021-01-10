import unittest
import os
import sys
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions
from six.moves import urllib

manifest = """
apiVersion: unfurl/v1alpha1
kind: Manifest
spec:
  service_template:
    imports:
      - repository: unfurl
        file: configurators/helm-template.yaml

    topology_template:
      node_templates:
        stable_repo:
          type: unfurl.nodes.HelmRepository
          properties:
            name: stable
            url:  http://localhost:8010/fixtures/helmrepo/

        k8sNamespace:
         type: unfurl.nodes.K8sNamespace
         # requirements: # XXX
         #   - host: k8sCluster
         properties:
           name: unfurl-helm-unittest

        mysql_release:
          type: unfurl.nodes.HelmRelease
          requirements:
            - repository:
                node: stable_repo
            - host:
                node: k8sNamespace
          properties:
            chart: stable/mysql
            release_name: mysql-test
            chart_values:
              args: []
"""

import threading
import os.path
from functools import partial

# http://localhost:8000/fixtures/helmrepo
@unittest.skipIf("helm" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
class HelmTest(unittest.TestCase):
    def setUp(self):
        server_address = ("", 8010)
        directory = os.path.dirname(__file__)
        if sys.version_info[0] >= 3:
            from http.server import HTTPServer, SimpleHTTPRequestHandler

            handler = partial(SimpleHTTPRequestHandler, directory=directory)
            self.httpd = HTTPServer(server_address, handler)
        else:  # for python 2.7
            from SimpleHTTPServer import SimpleHTTPRequestHandler
            import SocketServer
            import urllib

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

            try:
                self.httpd = SocketServer.TCPServer(
                    server_address, RootedHTTPRequestHandler
                )
            except:  # close() doesn't work on 2.7 so address might still be in use
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

        runner = Runner(YamlManifest(manifest))

        run1 = runner.run(JobOptions(dryrun=False, verbose=3, startTime=1))
        assert not run1.unexpectedAbort, run1.unexpectedAbort.getStackTrace()
        summary = run1.jsonSummary()
        # runner.manifest.statusSummary()
        # print(summary)
        self.assertEqual(
            summary["job"],
            {
                "id": "A01110000000",
                "status": "ok",
                "total": 4,
                "ok": 4,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 4,
            },
        )
        assert all(task["targetStatus"] == "ok" for task in summary["tasks"]), summary[
            "tasks"
        ]
        # runner.manifest.dump()

    def test_undeploy(self):
        runner = Runner(YamlManifest(manifest))
        # runner.manifest.statusSummary()
        run = runner.run(JobOptions(workflow="check", startTime=2))
        summary = run.jsonSummary()
        assert not run.unexpectedAbort, run.unexpectedAbort.getStackTrace()

        # runner.manifest.statusSummary()
        run2 = runner.run(
            JobOptions(workflow="undeploy", startTime=3, destroyunmanaged=True)
        )

        assert not run2.unexpectedAbort, run2.unexpectedAbort.getStackTrace()
        summary = run2.jsonSummary()

        # note! if tests fail may need to run:
        #      helm uninstall mysql-test -n unfurl-helm-unittest
        #  and kubectl delete namespace unfurl-helm-unittest

        # note: this test relies on stable_repo being place in the helm cache by test_deploy()
        # comment out the repository requirement to run this test standalone
        assert all(
            task["targetStatus"] == "absent" for task in summary["tasks"]
        ), summary["tasks"]
        self.assertEqual(
            summary["job"],
            {
                "id": "A01130000000",
                "status": "ok",
                "total": 3,
                "ok": 3,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 3,
            },
        )
