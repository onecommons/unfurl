import json
import os
import threading
import time
import unittest
import urllib.request
from functools import partial
from multiprocessing import Process, set_start_method

import requests
from click.testing import CliRunner
from git import Repo
from unfurl import server

import pytest
from tests.utils import init_project, run_cmd
from unfurl.repo import GitRepo
from unfurl.yamlloader import yaml
from base64 import b64encode

# mac defaults to spawn, switch to fork so the subprocess inherits our stdout and stderr so we can see its log output
set_start_method("fork")

UNFURL_TEST_REDIS_URL = os.getenv("UNFURL_TEST_REDIS_URL")
if UNFURL_TEST_REDIS_URL:
    # e.g. "unix:/home/user/gdk/redis/redis.socket?db=2"
    os.environ["CACHE_TYPE"] = "RedisCache"
    os.environ["CACHE_REDIS_URL"] = UNFURL_TEST_REDIS_URL
    os.environ["CACHE_KEY_PREFIX"] = "test" + str(int(time.time())) + "::"
    # time out in 2 minutes so we don't fill up the cache with cruft:
    os.environ["CACHE_DEFAULT_TIMEOUT"] = "120"


# Very minimal deployment
deployment = """
apiVersion: unfurl/v1alpha1
kind: Ensemble
spec:
  service_template:
    topology_template:
      node_templates:
        container_service:
          type: tosca:Root
          properties:
            container:
              environment:
                VAR: "{0}"
"""              

patch = """
[{{
    "name": "container_service",
    "type": "tosca:Root",
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
    "name": "container_service",
    "__deleted": true
}]
"""

_static_server_port = 8090
_server_port = 8091
CLOUD_TEST_SERVER = "https://gitlab.com/"


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
        # server.serve('localhost', _static_server_port, 'secret', 'ensemble', {})
        # "url": ,
        os.environ["UNFURL_LOGGING"] = "TRACE"
        server_process = Process(
            target=server.serve,
            args=("localhost", _static_server_port, "secret", ".", "", {}, CLOUD_TEST_SERVER),
        )
        assert start_server_process(server_process, _static_server_port)

        yield server_process

        server_process.terminate()   # Gracefully shutdown the server (SIGTERM)
        server_process.join()   # Wait for the server to terminate


def set_up_deployment(runner, deployment):
    init_project(
        runner,
        args=["init", "--mono", "--var", "vaultpass", "", "remote"],
        env=dict(UNFURL_HOME=""),
    )
    # Create a mock deployment
    with open("remote/ensemble/ensemble.yaml", "w") as f:
        f.write(deployment)

    repo = Repo.init('remote')
    repo = GitRepo(repo)
    repo.add_all('remote')
    repo.commit_files(["remote/ensemble/ensemble.yaml"], "Add deployment")

    # we need a bare repo for push to work
    os.system("git clone --bare remote remote.git")

    port = _next_port()

    os.makedirs("server")
    p = Process(
        target=server.serve,
        args=("localhost", port, None, "server", ".", {"home": ""}, "remote.git"),
    )
    assert start_server_process(p, port)

    return p, port


def test_server_health(runner):
    res = requests.get("http://localhost:8090/health", params={"secret": "secret"})

    assert res.status_code == 200
    assert res.content == b"OK"


def test_server_authentication(runner):
    res = requests.get("http://localhost:8090/health")
    assert res.status_code == 401
    assert res.json()["code"] == "UNAUTHORIZED"

    res = requests.get("http://localhost:8090/health", params={"secret": "secret"})
    assert res.status_code == 200
    assert res.content == b"OK"

    res = requests.get("http://localhost:8090/health", params={"secret": "wrong"})
    assert res.status_code == 401
    assert res.json()["code"] == "UNAUTHORIZED"

    res = requests.get(
        "http://localhost:8090/health", headers={"Authorization": "Bearer secret"}
    )
    assert res.status_code == 200
    assert res.content == b"OK"

    res = requests.get(
        "http://localhost:8090/health", headers={"Authorization": "Bearer wrong"}
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
            for export_format in ["deployment", "environments"]:
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
    with runner.isolated_filesystem():
        port = _next_port()
        p = Process(
            target=server.serve,
            args=("localhost", port, None, ".", ".", {"home": ""}, CLOUD_TEST_SERVER),
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
                ],
            )
            last_commit = GitRepo(Repo("dashboard")).revision
            # compare the export request output to the export command output
            for export_format in ["deployment", "environments"]:
                # try twice, second attempt should be cached
                cleaned_output = "0"
                for msg in ("cache miss for", "cache hit for"):
                    # test caching
                    project_id = "onecommons/project-templates/dashboard"
                    res = requests.get(
                        f"http://localhost:{port}/export",
                        params={
                            "auth_project": project_id,
                            "latest_commit": last_commit,  # enable caching but just get the latest in the cache
                            "format": export_format,
                        },
                        headers={
                          "If-None-Match": server._make_etag(last_commit),
                          "X-Git-Credentials": b64encode("username:token".encode())
                        }
                    )
                    if msg == "cache miss for":
                        assert res.status_code == 200
                        # don't bother re-exporting the second time
                        exported = run_cmd(
                            runner,
                            ["--home", "", "export", "dashboard", "--format", export_format],
                            env={"UNFURL_LOGGING": "critical"},
                        )
                        assert exported
                        # Strip out output from the http server
                        output = exported.output
                        cleaned_output = output[max(output.find("{"), 0):]
                        assert res.json() == json.loads(cleaned_output)
                    else:
                        # cache hit
                        assert res.status_code == 304

                    file_path = server._get_filepath(export_format, None)
                    key = server.CacheEntry(project_id, "", file_path, export_format).cache_key()

                    # XXX
                    # caplog, capsys, capfd capture log messages from uvicorn but not from the request workers
                    # pytest -s does output those messages to the console if set_start_method is set to "fork"
                    # visually confirmed this assert:
                    # assert f"{msg} {key}" in caplog.text
            
            # test with a blueprint
            run_cmd(
                runner,
                [
                    "--home", "",
                    "clone",
                    "--empty",
                    "https://gitlab.com/onecommons/project-templates/application-blueprint",
                ]
            )
            res = requests.get(
                f"http://localhost:{port}/export",
                params={
                    "auth_project": "onecommons/project-templates/application-blueprint",
                    "latest_commit": last_commit,  # enable caching but just get the latest in the cache
                    "format": "blueprint",
                },
            )
            assert res.status_code == 200
            # don't bother re-exporting the second time
            exported = run_cmd(
                runner,
                ["--home", "", "export", "--format", "blueprint", "application-blueprint/ensemble-template.yaml"],
                env={"UNFURL_LOGGING": "critical"},
            )
            assert exported
            # Strip out output from the http server
            output = exported.output
            cleaned_output = output[max(output.find("{"), 0):]
            assert res.json() == json.loads(cleaned_output)
        finally:
            p.terminate()
            p.join()


def test_populate_cache(runner):
    project_ids = ["onecommons/project-templates/dashboard", "onecommons/project-templates/dashboard",
                  "onecommons/project-templates/application-blueprint"]
    files = ["unfurl.yaml", "ensemble/ensemble.yaml", "ensemble-template.yaml"]
    port = _static_server_port
    for file_path, project_id in zip(files, project_ids):
        res = requests.post(
            f"http://localhost:{port}/populate_cache",
            params={
                "secret": "secret",
                "auth_project": project_id,
                "latest_commit": "HEAD",
                "path": file_path,
                "visibility": "public",
            },
        )
        assert res.status_code == 200
        assert res.content == b"OK"


def test_server_update_deployment():
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            initial_deployment = deployment.format("initial")
            p, port = set_up_deployment(runner, initial_deployment)

            target_patch = patch.format("target")
            res = requests.post(
                f"http://localhost:{port}/update_ensemble?auth_project=remote",
                json={
                    "patch": json.loads(target_patch),
                },
            )
            assert res.status_code == 200
            last_commit = res.json()["commit"]

            res = requests.get(
                f"http://localhost:{port}/export",
                params={
                    "auth_project": "remote",
                    "latest_commit": last_commit,  # enable caching but just get the latest in the cache
                    "format": "deployment",
                },
            )
            assert res.status_code == 200
            assert res.json()["ResourceTemplate"]["container_service"]["properties"][0]["name"] == "container"

            os.chdir("remote")
            # server pushed to remote.git
            os.system("git pull ../remote.git")
            with open("ensemble/ensemble.yaml", "r") as f:
                data = yaml.load(f.read())
                assert (data['spec']
                            ['service_template']
                            ['topology_template']
                            ['node_templates']
                            ['container_service']
                            ['properties']
                            ['container']
                            ['environment']
                            ['VAR']
                        ) == "target"

            res = requests.post(
                f"http://localhost:{port}/update_ensemble?auth_project=remote",
                json={
                    "patch": json.loads(delete_patch),
                }
            )
            assert res.status_code == 200
            assert res.content.startswith(b'{"commit":')

            # server pushes to remote.git which needs to be a bare repository
            # so pull from there to verify the push
            os.system("git pull ../remote.git")
            with open("ensemble/ensemble.yaml", "r") as f:
                data = yaml.load(f.read())
                assert not data['spec']['service_template']['topology_template']['node_templates']

            provider_patch = [{
              "name": "gcp",
              "primary_provider": {
                "name": "primary_provider",
                "type": "unfurl.relationships.ConnectsTo.GoogleCloudProject",
                "__typename": "ResourceTemplate"
              },
              "connections": {
                "primary_provider": {
                  "name": "primary_provider",
                  "type": "unfurl.relationships.ConnectsTo.GoogleCloudProject",
                  "__typename": "ResourceTemplate"
                }
              },
              "__typename": "DeploymentEnvironment"
            }]
            res = requests.post(
                f"http://localhost:{port}/create_provider?auth_project=remote",
                json={
                    "environment":"gcp", "deployment_blueprint":None, "deployment_path": "environments/gcp/primary_provider",
                    "patch": provider_patch,
                    "commit_msg": "Create environment gcp"
                }
            )
            assert res.status_code == 200
            assert res.content.startswith(b'{"commit":')

            os.system("git pull ../remote.git")
            with open("unfurl.yaml", "r") as f:
                data = yaml.load(f.read())
                # check that the environment was added and an ensemble was created
                assert data["environments"]["gcp"]["connections"]["primary_provider"]["type"] == "unfurl.relationships.ConnectsTo.GoogleCloudProject"
                assert data["ensembles"][0]["alias"] == "primary_provider"

            res = requests.post(
                f"http://localhost:{port}/clear_project_file_cache?auth_project=remote",
            )
            assert res.content == b'1'  # one key deleted
            assert res.status_code == 200

        finally:
            if p:
                p.terminate()
                p.join()
