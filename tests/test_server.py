import json
import os
import threading
import time
import unittest
import urllib.request
from functools import partial
from multiprocessing import Process

import requests
from click.testing import CliRunner
from unfurl import server

from tests.utils import init_project, run_cmd

manifest = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
+include:
  file: ensemble-template.yaml
  repository: spec
metadata:
  uri: git-local://67ebee6347690a0b60c045281ab460d067527b6a:/ensemble.yaml
spec:
  service_template:
    repositories:
    # Files that are shared across ensemble instances should be placed in this "spec" repository
      spec:
        url: git-local://0ebc2d5b5204bd6592286dbb28f0c641fc2ac5c2:/.
"""


def start_server_process(proc, port):
    proc.start()
    for _ in range(5):
        time.sleep(0.2)
        try:
            url = f"http://localhost:{port}/health?secret=secret"
            urllib.request.urlopen(url)
        except Exception as e:
            pass
        else:
            return proc
    return None


def start_envvar_server(port):
    server_address = ("", port)
    directory = os.path.join(os.path.dirname(__file__), "fixtures")
    try:
        from http.server import HTTPServer, SimpleHTTPRequestHandler

        handler = partial(SimpleHTTPRequestHandler, directory=directory)
        httpd = HTTPServer(server_address, handler)
    except:  # address might still be in use
        httpd = None
        return None, None
    t = threading.Thread(name="http_thread", target=httpd.serve_forever)
    t.daemon = True
    t.start()

    env_var_url = f"http://localhost:{port}/envlist.json"
    # make sure this works
    f = urllib.request.urlopen(env_var_url)
    f.close()
    return httpd, env_var_url


class TestServer(unittest.TestCase):
    def setUp(self) -> None:
        cli_runner = CliRunner()
        self.runner = cli_runner
        with cli_runner.isolated_filesystem() as tmpdir:
            os.mkdir("ensemble")
            with open("ensemble/ensemble.yaml", "w") as f:
                f.write(manifest)

            # server.serve('localhost', 8081, 'secret', 'ensemble', {})
            p = Process(
                target=server.serve,
                args=("localhost", 8081, "secret", ".", f"{tmpdir}", {}),
            )
            self.server_process = p
            assert start_server_process(p, 8081)

    def test_server_health(self):
        res = requests.get("http://localhost:8081/health", params={"secret": "secret"})

        assert res.status_code == 200
        assert res.content == b"OK"

    def test_server_authentication(self):
        res = requests.get("http://localhost:8081/health")
        assert res.status_code == 401
        assert res.json()["code"] == "UNAUTHORIZED"

        res = requests.get("http://localhost:8081/health", params={"secret": "secret"})
        assert res.status_code == 200
        assert res.content == b"OK"

        res = requests.get("http://localhost:8081/health", params={"secret": "wrong"})
        assert res.status_code == 401
        assert res.json()["code"] == "UNAUTHORIZED"

        res = requests.get(
            "http://localhost:8081/health", headers={"Authorization": "Bearer secret"}
        )
        assert res.status_code == 200
        assert res.content == b"OK"

        res = requests.get(
            "http://localhost:8081/health", headers={"Authorization": "Bearer wrong"}
        )
        assert res.status_code == 401
        assert res.json()["code"] == "UNAUTHORIZED"

    def test_server_export_local(self):
        with self.runner.isolated_filesystem() as tmpdir:
            p = Process(
                target=server.serve,
                args=("localhost", 8082, None, ".", f"{tmpdir}", {"home": ""}),
            )
            self.server_process = p
            assert start_server_process(p, 8082)

            init_project(
                self.runner,
                args=["init", "--mono"],
                env=dict(UNFURL_HOME=""),
            )
            for export_format in ["deployment", "environments", "blueprint"]:
                res = requests.get(
                    f"http://localhost:8082/export?format={export_format}"
                )
                assert res.status_code == 200
                exported = run_cmd(
                    self.runner,
                    ["--home", "", "export", "--format", export_format],
                    env={"UNFURL_LOGGING": "critical"},
                )
                assert exported
                assert res.json() == json.loads(exported.output)

            p.terminate()
            p.join()

    def test_server_export_remote(self):
        httpd, env_var_url = start_envvar_server(8011)
        if httpd is None:
            httpd, env_var_url = start_envvar_server(8012)

        with self.runner.isolated_filesystem():
            init_project(
                self.runner,
                args=["init", "--mono"],
                env=dict(UNFURL_HOME=""),
            )

            p = Process(
                target=server.serve,
                args=("localhost", 8082, None, ".", ".", {"home": ""}),
            )
            assert start_server_process(p, 8082)

            run_cmd(
                self.runner,
                [
                    "--home", "",
                    "clone",
                    "--empty",
                    "https://gitlab.com/onecommons/project-templates/dashboard",
                    "--var", "UNFURL_CLOUD_VARS_URL", env_var_url,
                ],
            )


            for export_format in ["deployment", "environments", "blueprint"]:
                res = requests.get(
                    "http://localhost:8082/export",
                    params={
                        "url": "https://gitlab.com/onecommons/project-templates/dashboard",
                        "format": export_format,
                        "cloud_vars_url": env_var_url,
                    },
                )
                assert res.status_code == 200

                exported = run_cmd(
                    self.runner,
                    ["--home", "", "export", "dashboard", "--format", export_format],
                    env={"UNFURL_LOGGING": "critical"},
                )

                assert exported
                # Strip out output from the http server
                output = exported.output
                cleaned_output = output[max(output.find("{"), 0) :]
                assert res.json() == json.loads(cleaned_output)
            
            p.terminate()
            p.join()

        if httpd:
            httpd.socket.close()

    def tearDown(self) -> None:
        self.server_process.terminate()  # Gracefully shutdown the server (SIGTERM)
        self.server_process.join()  # Wait for the server to terminate
        return super().tearDown()
