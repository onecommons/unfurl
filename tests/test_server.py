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
from git import Repo
from unfurl import server

import pytest
from tests.utils import init_project, run_cmd
from unfurl.repo import GitRepo

# Very minimal deployment
deployment = """
{{
    "ResourceTemplate": {{
        "container_service": {{
            "properties": [
                {{
                    "value": {{
                        "environment": {{
                            "VAR": "{0}"
                        }}
                    }}
                }}
            ]
        }}
    }}
}}
"""

patch = """
[{{
    "name": "container_service",
    "type": "unfurl.nodes.ContainerService",
    "title": "container_service",
    "description": "",
    "directives": [],
    "properties": [{{
        "name": "container",
        "value": {{
            "image": "",
            "environment": {{ "VAR": "{0}" }}
        }}
    }}],
    "__typename": "ResourceTemplate",
    "computedProperties": []
}}]
"""

delete_patch = """
[{
    "__typename": "ResourceTemplate",
    "__deleted": "container_service"
}]
"""

_server_port = 8091


#  Increment port just in case server ports aren't closed in time for next test
#  NB: if server processes aren't terminated: pkill -fl spawn_main
def _next_port():
    global _server_port
    _server_port += 1
    return _server_port


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

    env_var_url = "http://localhost:8011/envlist.json"
    # make sure this works
    f = urllib.request.urlopen(env_var_url)
    f.close()
    return httpd, env_var_url

@pytest.fixture()
def runner():
    runner = CliRunner()
    with runner.isolated_filesystem() as tmpdir:
        # server.serve('localhost', 8081, 'secret', 'ensemble', {})
        server_process = Process(
            target=server.serve,
            args=("localhost", 8081, "secret", ".", "", {}),
        )
        assert start_server_process(server_process, 8081)

        yield server_process

        server_process.terminate()   # Gracefully shutdown the server (SIGTERM)
        server_process.join()   # Wait for the server to terminate


def set_up_deployment(runner, deployment):
    init_project(
        runner,
        args=["init", "--mono"],
        env=dict(UNFURL_HOME=""),
    )

    # Create a mock deployment
    os.makedirs("dashboard/deployments/dev")
    with open("dashboard/deployments/dev/deployment.json", "w") as f:
        f.write(deployment)

    repo = Repo.init('.')
    repo = GitRepo(repo)
    repo.add_all()
    repo.commit_files(["dashboard/deployments/dev/deployment.json"], "Add deployment")

    port = _next_port()
    p = Process(
        target=server.serve,
        args=("localhost", port, None, ".", ".", {"home": ""}),
    )
    assert start_server_process(p, port)

    return p, port


def test_server_health(runner):
    res = requests.get("http://localhost:8081/health", params={"secret": "secret"})

    assert res.status_code == 200
    assert res.content == b"OK"


def test_server_authentication(runner):
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


def test_server_export_local():
    runner = CliRunner()
    port = _next_port()
    with runner.isolated_filesystem() as tmpdir:
        p = Process(
            target=server.serve,
            args=("localhost", port, None, ".", f"{tmpdir}", {"home": ""}),
        )
        assert start_server_process(p, port)

        try:
            init_project(
                runner,
                args=["init", "--mono"],
                env=dict(UNFURL_HOME=""),
            )
            # compare the export request output to the export command output
            for export_format in ["deployment", "environments", "blueprint"]:
                res = requests.get(
                    f"http://localhost:{port}/export?format={export_format}"
                )
                assert res.status_code == 200
                exported = run_cmd(
                    runner,
                    ["--home", "", "export", "--format", export_format],
                    env={"UNFURL_LOGGING": "critical"},
                )
                assert exported
                assert res.json() == json.loads(exported.output)
        finally:
            p.terminate()
            p.join()

@unittest.skipIf(
    "slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set"
)
def test_server_export_remote():
    runner = CliRunner()
    httpd, env_var_url = start_envvar_server(8011)
    if httpd is None:
        httpd, env_var_url = start_envvar_server(8012)
    with runner.isolated_filesystem():
        port = _next_port()
        p = Process(
            target=server.serve,
            args=("localhost", port, None, ".", ".", {"home": ""}),
        )
        assert start_server_process(p, port)
        try:
            run_cmd(
                runner,
                [
                    "--home", "",
                    "clone",
                    "--empty",
                    "https://gitlab.com/onecommons/project-templates/dashboard",
                    "--var", "UNFURL_CLOUD_VARS_URL", env_var_url,
                ],
            )
            # compare the export request output to the export command output
            for export_format in ["deployment", "environments", "blueprint"]:
                res = requests.get(
                    f"http://localhost:{port}/export",
                    params={
                        "url": "https://gitlab.com/onecommons/project-templates/dashboard",
                        "format": export_format,
                        "cloud_vars_url": env_var_url,
                    },
                )
                assert res.status_code == 200
                # print(export_format)
                # print(json.dumps(res.json(), indent=2)

                exported = run_cmd(
                    runner,
                    ["--home", "", "export", "dashboard", "--format", export_format],
                    env={"UNFURL_LOGGING": "critical"},
                )
                assert exported
                # Strip out output from the http server
                output = exported.output
                cleaned_output = output[max(output.find("{"), 0) :]
                assert res.json() == json.loads(cleaned_output)
        finally:
            p.terminate()
            p.join()

    if httpd:
        httpd.socket.close()


def test_server_update_deployment():
    runner = CliRunner()
    with runner.isolated_filesystem():
        try:
            initial_deployment = deployment.format("initial")
            p, port = set_up_deployment(runner, initial_deployment)

            target_patch = patch.format("target")
            res = requests.post(
                f"http://localhost:{port}/update_deployment",
                json={
                    "projectPath": ".",
                    "path": "dashboard/deployments/dev/deployment.json",
                    "patch": json.loads(target_patch),
                }
            )
            assert res.status_code == 200

            with open("dashboard/deployments/dev/deployment.json", "r") as f:
                data = json.load(f)
                assert (data['ResourceTemplate']
                            ['container_service']
                            ['properties'][0]
                            ['value']
                            ['environment']
                            ['VAR']
                        ) == "target"   

        finally:
            p.terminate()
            p.join()


def test_server_update_deployment_delete():
    runner = CliRunner()
    with runner.isolated_filesystem():
        try:
            initial_deployment = deployment.format("initial")
            p, port = set_up_deployment(runner, initial_deployment)

            res = requests.post(
                f"http://localhost:{port}/update_deployment",
                json={
                    "projectPath": ".",
                    "path": "dashboard/deployments/dev/deployment.json",
                    "patch": json.loads(delete_patch),
                }
            )
            
            assert res.status_code == 200

            with open("dashboard/deployments/dev/deployment.json", "r") as f:
                data = json.load(f)
                assert data == {
                    "ResourceTemplate": {}
                }
        finally:
            p.terminate()
            p.join()

# XXX test patching with remote url
