import json
import os
from pprint import pformat
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
from unfurl.util import change_cwd, get_package_digest
from base64 import b64encode

# mac defaults to spawn, switch to fork so the subprocess inherits our stdout and stderr so we can see its log output
# (with -s only)
# but fork doesn't inherit the environment so UNFURL_TEST_REDIS_URL breaks
# set_start_method("fork")

UNFURL_TEST_REDIS_URL = os.getenv("UNFURL_TEST_REDIS_URL")
if UNFURL_TEST_REDIS_URL:
    # e.g. "unix:///home/user/gdk/redis/redis.socket?db=2"
    os.environ["CACHE_TYPE"] = "RedisCache"
    os.environ["CACHE_REDIS_URL"] = UNFURL_TEST_REDIS_URL
    os.environ["CACHE_KEY_PREFIX"] = "test" + str(int(time.time())) + "::"
    # time out in 2 minutes so we don't fill up the cache with cruft:
    os.environ["CACHE_DEFAULT_TIMEOUT"] = "120"
os.environ["CACHE_CLEAR_ON_START"] = "1"
os.environ["UNFURL_SET_GIT_USER"] = "unittest"
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
    "type": "ContainerService@gitlab.com/onecommons/unfurl-types",
    "title": "container_service",
    "description": "",
    "_sourceinfo": {{
        "prefix": null,
        "url": "https://gitlab.com/onecommons/unfurl-types.git",
        "repository": "types",
        "file": "service-template.yaml"
    }},
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
CLOUD_TEST_SERVER = "https://unfurl.cloud"  # if changed, need to set package rules # "https://unfurl.cloud"


#  Increment port just in case server ports aren't closed in time for next test
#  NB: if server processes aren't terminated: pkill -fl spawn_main
def _next_port():
    global _server_port
    _server_port += 1
    return _server_port


def start_server_process(proc, port):
    proc.start()
    for _ in range(10):
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


def commit_foo(val: str):
    with open("foo", "w") as foo:
        foo.write(val)
    os.system("git add foo")
    os.system(f"git commit -m'{val}'")

def set_up_deployment(runner, deployment):
    # create git repo in "remote" and bare clone of it in "remote.git"
    # configure the server to clone into "server" and push into "remote.git"
    init_project(
        runner,
        args=["init", "--mono", "--var", "vaultpass", "", "remote"],
        env=dict(UNFURL_HOME=""),
    )
    # Create a mock deployment
    with open("remote/ensemble/ensemble.yaml", "w") as f:
        f.write(deployment)

    repo = GitRepo(Repo.init('remote'))
    repo.add_all('remote')
    repo.commit_files(["remote/ensemble/ensemble.yaml"], "Add deployment")

    # we need a bare repo for push to work
    os.system("git clone --bare remote remote.git")
    assert repo.repo.create_remote("origin", "../remote.git")
    port = _next_port()

    os.makedirs("server")
    p = Process(
        target=server.serve,
        args=("localhost", port, None, "server", ".", {"home": ""}, os.path.abspath("remote.git")),
    )
    assert start_server_process(p, port)

    assert repo.revision
    return p, port, repo.revision


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


def _strip_sourceinfo(export, log=False):
    for name, typedef in export["ResourceType"].items():
        _sourceinfo = typedef.pop("_sourceinfo", None)
        if _sourceinfo and log:
            print(name, _sourceinfo)


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
                    f"{CLOUD_TEST_SERVER}/onecommons/project-templates/dashboard",
                ],
            )
            last_commit = GitRepo(Repo("dashboard")).revision
            # compare the export request output to the export command output
            for export_format in ["deployment", "environments"]:
                # try twice, second attempt should be cached
                cleaned_output = "0"
                etag = ""
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
                          "If-None-Match": etag,
                          "X-Git-Credentials": b64encode("username:token".encode())
                        }
                    )
                    if res.status_code == 200:
                        etag = res.headers.get("Etag") or ""
                        assert etag
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
                        expected = _strip_sourceinfo(json.loads(cleaned_output))
                        assert _strip_sourceinfo(res.json()) == expected #, f"{pformat(res.json(), depth=2, compact=True)}\n != \n{pformat(expected, depth=2, compact=True)}"
                    else:
                        # cache hit
                        assert res.status_code == 304, (res.headers.get("Etag") == etag, etag)

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
                    f"{CLOUD_TEST_SERVER}/onecommons/project-templates/application-blueprint",
                ]
            )
            last_commit = GitRepo(Repo("application-blueprint")).revision
            res = requests.get(
                f"http://localhost:{port}/export",
                params={
                    "auth_project": "onecommons/project-templates/application-blueprint",
                    "latest_commit": last_commit,  # enable caching but just get the latest in the cache
                    "format": "blueprint",
                    "branch": "(MISSING)"
                },
            )
            # branch=(MISSING) will log: Package unfurl.cloud/onecommons/project-templates/application-blueprint is looking for earliest remote tags v* on https://unfurl.cloud/onecommons/project-templates/application-blueprint.git
            assert res.status_code == 200
            # assert res.status_code == 304
            # etag = res.headers.get("Etag") or ""
            exported = run_cmd(
                runner,
                ["--home", "", "export", "--format", "blueprint", "application-blueprint/ensemble-template.yaml"],
                env={"UNFURL_LOGGING": "critical"},
            )
            assert exported
            # Strip out output from the http server
            output = exported.output
            cleaned_output = output[max(output.find("{"), 0):]
            expected = _strip_sourceinfo(json.loads(cleaned_output))
            assert _strip_sourceinfo(res.json(), True) == expected, f"{pformat(res.json(), depth=2, compact=True)}\n != \n{pformat(expected, depth=2, compact=True)}"

            dep_commit = GitRepo(Repo("application-blueprint/unfurl-types")).revision
            etag = server._make_etag(hex(int(last_commit, 16) 
                                         ^ int(get_package_digest(True), 16)
                                         ^ int(dep_commit, 16)))
            # # check that this public project (no auth header sent) was cached
            res = requests.get(
                f"http://localhost:{port}/export",
                params={
                    "auth_project": "onecommons/project-templates/application-blueprint",
                    "latest_commit": last_commit,  # enable caching but just get the latest in the cache
                    "format": "blueprint",
                },
                headers={
                  "If-None-Match": etag,
                }
            )
            assert res.status_code == 304
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

@unittest.skipIf(
    "slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set"
)
def test_server_update_deployment():
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            initial_deployment = deployment.format("initial")
            p, port, last_commit = set_up_deployment(runner, initial_deployment)

            target_patch = patch.format("target")
            res = requests.post(
                f"http://localhost:{port}/update_ensemble?auth_project=remote",
                json={
                    "patch": json.loads(target_patch),
                    "latest_commit": last_commit
                },
            )
            assert res.status_code == 200
            new_commit = res.json()["commit"]
            assert last_commit != new_commit
            last_commit = new_commit
            # os.system("git --git-dir server/public/remote/main/.git log -p")

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
            # server pushes to remote.git which needs to be a bare repository
            # so pull from there to verify the push
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

            # test that the server recovers from a bad repo before trying to patch
            # by creating a conflict between the server's local repo and the remote repo
            commit_foo("bar")
            os.system("git push ../remote.git")  # push to remote.git
            client_repo = GitRepo(Repo.init('.'))
            last_commit = client_repo.revision

            os.chdir("../server/public/remote")
            commit_foo("foo")
            os.chdir("../../../remote")

            # test deleting

            res = requests.post(
                f"http://localhost:{port}/update_ensemble?auth_project=remote",
                json={
                    "patch": json.loads(delete_patch),
                    "latest_commit": last_commit,
                }
            )
            assert res.status_code == 200
            last_commit = res.json()["commit"]
            assert last_commit

            # server pushes to remote.git which needs to be a bare repository
            # so pull from there to verify the push
            os.system("git pull  ../remote.git")
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
                    "commit_msg": "Create environment gcp",
                    "latest_commit": last_commit,
                }
            )
            assert res.status_code == 200
            assert res.content.startswith(b'{"commit":')

            assert not os.system("git pull --commit --no-edit origin main")
            with open("unfurl.yaml", "r") as f:
                data = yaml.load(f.read())
                # check that the environment was added and an ensemble was created
                assert data["environments"]["gcp"]["connections"]["primary_provider"]["type"] == "unfurl.relationships.ConnectsTo.GoogleCloudProject"
                assert data["ensembles"][0]["alias"] == "primary_provider", data

            res = requests.post(
                f"http://localhost:{port}/clear_project_file_cache?auth_project=remote",
            )
            # 'remote:main::localenv', 'remote:pull:...', 'remote:main:ensemble/ensemble.yaml:deployment'
            assert res.content == b'3'  # 3 keys deleted
            assert res.status_code == 200

        finally:
            if p:
                p.terminate()
                p.join()

# XXX test that server recovers from an upstream repo that had a force push or tags that changed
# def test_force_push():
#   assert repo.repo.create_tag("v1.0", message="tag v1")
    # ...
    # change a tag ref, which will cause GitRepo.pull() to fail like so:
    # git.exc.GitCommandError: Cmd('git') failed due to: exit code(1)
    #   cmdline: git pull origin v1.0.0 --tags --update-shallow --ff-only --shallow-since=1648458328
    #   stderr: 'From http://tunnel.abreidenbach.com:3000/onecommons/blueprints/wordpress
    # * tag               v1.0.0     -> FETCH_HEAD
    # ! [rejected]        v1.0.0     -> v1.0.0  (would clobber existing tag)'
#   assert not os.system("git tag -d v1.0")
#   assert not os.system("git push --delete origin v1.0")
#   assert not os.system("git tag v1.0 -m'retag'")
#   assert not os.system("git push --tags origin")
