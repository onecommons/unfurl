import json
import os
from pprint import pformat
import re
import socket
import threading
import time
import traceback
import unittest
import urllib.request
from functools import partial
from multiprocessing import Process, set_start_method, get_context, Queue

import requests
from click.testing import CliRunner
from git import Repo
from unfurl.server import serve as server
from unfurl.server import gui
from unfurl.packages import is_semver_compatible_with

import pytest
from tests.utils import init_project, run_cmd
from unfurl.repo import GitRepo
from unfurl.yamlloader import yaml
from unfurl.util import change_cwd, get_package_digest
from base64 import b64encode
import logging
import tempfile

# Prefer explicit IPv4 loopback for tests to avoid getaddrinfo resolution ordering differences
HOST = "127.0.0.1"


def wait_for_status(
    url, params=None, headers=None, expected=304, timeout=10.0, poll_interval=0.25
):
    """Poll `url` until it returns `expected` status or `timeout` elapses.

    On timeout, fail the test with diagnostic information including last response headers.
    """
    deadline = time.time() + timeout
    last_res = None
    while time.time() < deadline:
        try:
            last_res = requests.get(url, params=params, headers=headers, timeout=2.0)
        except requests.RequestException:
            last_res = None
            time.sleep(poll_interval)
            continue
        if last_res.status_code == expected:
            return last_res
        time.sleep(poll_interval)

    if last_res is None:
        pytest.fail(
            f"Timed out waiting for status {expected} from {url}: no successful response seen within {timeout}s"
        )
    else:
        pytest.fail(
            f"cache expected {expected} for {url} after {timeout}s, last status {last_res.status_code}, headers: {dict(last_res.headers)}"
        )


def wait_for_log(log_file, pattern, request_fn, timeout=15.0, poll_interval=0.25):
    """Poll until `pattern` appears in new log entries, calling `request_fn` each iteration.

    Records the current log file position before starting so only new entries are checked,
    avoiding false positives from earlier test activity.  Returns the last response returned
    by `request_fn`.  Fails the test on timeout.
    """
    with open(log_file) as _f:
        _f.seek(0, 2)
        offset = _f.tell()
    deadline = time.time() + timeout
    last_res = None
    while time.time() < deadline:
        last_res = request_fn()
        with open(log_file) as _f:
            _f.seek(offset)
            new_log = _f.read()
        if pattern in new_log:
            return last_res
        time.sleep(poll_interval)
    with open(log_file) as _f:
        log_contents = _f.read()
    pytest.fail(
        f"Timed out waiting for {pattern!r} in log after {timeout}s.\nLog tail:\n{log_contents[-3000:]}"
    )


# mac defaults to spawn, switch to fork so the subprocess inherits our stdout and stderr so we can see its log output
# (with -s only)
# but fork doesn't inherit the environment so UNFURL_TEST_REDIS_URL breaks
# set_start_method("fork")

UNFURL_TEST_REDIS_URL = os.getenv("UNFURL_TEST_REDIS_URL")
if UNFURL_TEST_REDIS_URL:
    # e.g. "unix:///home/user/gdk/redis/redis.socket?db=2" or redis://[[username]:[password]]@127.0.0.1:6379/0
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
CLOUD_TEST_SERVER = "https://unfurl.cloud"


#  Increment port just in case server ports aren't closed in time for next test
#  NB: if server processes aren't terminated: pkill -fl spawn_main
def _next_port():
    global _server_port
    # When the Rust proxy is active each server occupies TWO ports (N=Rust front-end,
    # N+1=Python backend).  Increment by 2 so consecutive tests don't race on the
    # previous test's backend port.
    _server_port += 2 if os.environ.get("UNFURL_TEST_RUST_SERVER") else 1
    return _server_port


def _save_rust_fixtures() -> None:
    """Dump cached deployment and blueprint pickle values from Redis to
    rust/server/tests/fixtures/ so the Rust unit tests can use them.

    Requires UNFURL_TEST_REDIS_URL to be set.  Silently returns if Redis
    is not configured or the expected keys are not found.
    """
    if not UNFURL_TEST_REDIS_URL:
        return
    import redis as _redis

    fixtures_dir = os.path.join(
        os.path.dirname(__file__), "..", "rust", "server", "tests", "fixtures"
    )
    os.makedirs(fixtures_dir, exist_ok=True)

    cache_prefix = os.environ.get("CACHE_KEY_PREFIX", "ufsv::")
    r = _redis.Redis.from_url(UNFURL_TEST_REDIS_URL)
    try:
        keys = r.keys(f"{cache_prefix}*")
    except Exception as e:
        print(f"_save_rust_fixtures: Redis error: {e}")
        return

    suffix_to_file = {
        ":deployment": "deployment.pkl",
        ":blueprint": "blueprint.pkl",
    }
    for key in keys:
        key_str = key.decode("utf-8") if isinstance(key, bytes) else key
        for suffix, filename in suffix_to_file.items():
            if key_str.endswith(suffix):
                value = r.get(key)
                if value:
                    dest = os.path.join(fixtures_dir, filename)
                    with open(dest, "wb") as f:
                        f.write(value)
                    print(f"_save_rust_fixtures: saved {key_str} -> {dest}")


def _rust_extra_env() -> dict:
    """Extra env vars to enable the Rust proxy when UNFURL_TEST_RUST_SERVER=1.

    Redis is required for correct Rust proxy operation:
    - Write endpoints are queued via Redis (without Redis the query string is dropped)
    - Read endpoints use Redis for caching

    Raises RuntimeError if UNFURL_TEST_RUST_SERVER=1 but UNFURL_TEST_REDIS_URL is not set.
    Forwards Redis config explicitly so spawn-based child processes and the Rust
    subprocess all use the same cache backend and key prefix.
    """
    if not os.environ.get("UNFURL_TEST_RUST_SERVER"):
        print("UNFURL_TEST_RUST_SERVER not set, running server without Rust proxy")
        return {"UNFURL_RUST_SERVER": "0"}
    if not UNFURL_TEST_REDIS_URL:
        raise RuntimeError(
            "UNFURL_TEST_RUST_SERVER=1 requires UNFURL_TEST_REDIS_URL to be set. "
            "The Rust proxy requires Redis for correct operation of write endpoints."
        )
    print("UNFURL_TEST_RUST_SERVER set, running server with Rust proxy")
    return {
        "UNFURL_RUST_SERVER": "1",
        "CACHE_TYPE": "RedisCache",
        "CACHE_REDIS_URL": UNFURL_TEST_REDIS_URL,
        # Forward the unique per-run prefix set by module-level code so all
        # processes (Python server, Rust subprocess) share the same namespace.
        "CACHE_KEY_PREFIX": os.environ.get("CACHE_KEY_PREFIX", "ufsv::"),
        "CACHE_DEFAULT_TIMEOUT": "120",
        # Forward UNFURL_LOGGING so _start_rust_server can map it to RUST_LOG.
        "UNFURL_LOGGING": os.environ.get("UNFURL_LOGGING", "debug"),
    }


def serve_server(*args, error_queue: Queue = None, extra_env: dict = None, **kw):
    """Wrapper around server.serve that forwards child start errors to a Queue.

    extra_env: env vars to set in the child process before starting the server.
    Use this instead of relying on os.environ inheritance, which is unreliable
    with the forkserver start method (the default on Linux since Python 3.14).
    """
    if extra_env:
        os.environ.update(extra_env)
    try:
        return server.serve(*args, **kw)
    except Exception:
        tb = traceback.format_exc()
        if error_queue is not None:
            error_queue.put(tb)
        logging.warning("server.serve unexpectedly failed", exc_info=True)
        raise


def start_server_process(process_obj, port, hosts=(HOST, "::1"), timeout=12.0):
    """Start a server process and wait for it to be reachable.

    Args:
        process_obj: Process object to start. The Process must have been created with
                     serve_server as target and kwargs={"error_queue": queue}, and must
                     have process_obj._error_queue set to the same queue for error retrieval.
        port: Port number the server should bind to
        hosts: Tuple of hosts to try connecting to
        timeout: Maximum time to wait for the server to be reachable

    Returns:
        The process object if successful

    Raises:
        RuntimeError: If the server process exits prematurely or is not reachable
    """
    process_obj.start()
    start = time.time()
    last_exc = None

    # Helper to retrieve any exception traceback from the child process.
    # Uses closure to access process_obj from enclosing scope.
    def _child_traceback():
        eq = getattr(process_obj, "_error_queue", None)
        if not eq:
            return None
        try:
            return eq.get_nowait()
        except Exception:
            return None

    while time.time() - start < timeout:
        if not process_obj.is_alive():
            tb = _child_traceback()
            if tb:
                raise RuntimeError(
                    f"server process exited prematurely; traceback:\n{tb}"
                )
            else:
                raise RuntimeError(
                    f"server process exited prematurely with exitcode {process_obj.exitcode}"
                )
        for h in hosts:
            try:
                with socket.create_connection((h, port), timeout=1):
                    # When the Rust proxy is active it binds the front-end port
                    # almost instantly, but Python/waitress takes longer on port+1.
                    # Wait for the backend port too so the first proxied request
                    # doesn't arrive before waitress is ready.
                    if os.environ.get("UNFURL_TEST_RUST_SERVER"):
                        backend_port = port + 1
                        deadline = time.time() + timeout
                        while time.time() < deadline:
                            try:
                                with socket.create_connection(
                                    (HOST, backend_port), timeout=1.0
                                ):
                                    break
                            except OSError:
                                time.sleep(0.1)
                    return process_obj
            except Exception as e:
                last_exc = e
        time.sleep(0.1)

    tb = _child_traceback()
    if tb:
        raise RuntimeError(
            f"server not reachable on port {port} after {timeout}s; server traceback:\n{tb}"
        )
    raise RuntimeError(
        f"server not reachable on port {port} after {timeout}s; last error: {last_exc}"
    )


def start_envvar_server(port):
    server_address = ("", port)
    directory = os.path.join(os.path.dirname(__file__), "fixtures")
    try:
        from http.server import HTTPServer, SimpleHTTPRequestHandler

        handler = partial(SimpleHTTPRequestHandler, directory=directory)
        httpd = HTTPServer(server_address, handler)
    except Exception:  # address might still be in use
        httpd = None
        return None, None
    t = threading.Thread(name="http_thread", target=httpd.serve_forever)
    t.daemon = True
    t.start()

    env_var_url = "http://127.0.0.1:8011/envlist.json"
    # make sure this works
    f = urllib.request.urlopen(env_var_url)
    f.close()
    return httpd, env_var_url


@pytest.fixture()
def runner():
    runner = CliRunner()
    with runner.isolated_filesystem() as tmpdir:
        # server.serve(HOST, _static_server_port, 'secret', 'ensemble', {})
        # "url": ,
        os.environ["UNFURL_LOGGING"] = "TRACE"
        ctx = get_context()
        error_queue = ctx.Queue()
        server_process = ctx.Process(
            target=serve_server,
            args=(HOST, _static_server_port, "secret", ".", "", {}, CLOUD_TEST_SERVER),
            kwargs={"error_queue": error_queue, "extra_env": _rust_extra_env()},
        )
        server_process._error_queue = error_queue
        try:
            start_server_process(server_process, _static_server_port)

            yield server_process
        finally:
            server_process.terminate()  # Gracefully shutdown the server (SIGTERM)
            server_process.join()  # Wait for the server to terminate


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
        args=["init", "--mono", "--var", "VAULT_PASSWORD", "", "remote"],
        env=dict(UNFURL_HOME=""),
    )
    # Create a mock deployment
    with open("remote/ensemble/ensemble.yaml", "w") as f:
        f.write(deployment)

    repo = GitRepo(Repo.init("remote"))
    repo.add_all("remote")
    repo.commit_files(["remote/ensemble/ensemble.yaml"], "Add deployment")

    # we need a bare repo for push to work
    os.system("git clone --bare remote remote.git")
    assert repo.repo.create_remote("origin", "../remote.git")
    port = _next_port()

    os.makedirs("server")
    ctx = get_context()
    error_queue = ctx.Queue()
    p = ctx.Process(
        target=serve_server,
        args=(
            HOST,
            port,
            None,
            "server",
            ".",
            {"home": ""},
            os.path.abspath("remote.git"),
        ),
        kwargs={"error_queue": error_queue, "extra_env": _rust_extra_env()},
    )
    p._error_queue = error_queue
    try:
        start_server_process(p, port)

        assert repo.revision
        return p, port, repo.revision
    except Exception:
        p.terminate()
        p.join()
        raise


def test_server_health(runner: Process):
    res = requests.get(
        f"http://{HOST}:{_static_server_port}/health", params={"secret": "secret"}
    )

    assert res.status_code == 200
    assert res.content == b"OK"


def test_server_version(runner: Process):
    res = requests.get(
        f"http://{HOST}:{_static_server_port}/version", params={"secret": "secret"}
    )

    assert res.status_code == 200
    assert re.match(rb"^1\..+\+\w+$", res.content) is not None


def test_gui_release():
    assert re.match(gui.release_url_pattern, gui.RELEASE_URL).group(1) == gui.TAG
    assert is_semver_compatible_with(gui.TAG, "v0.1.0-alpha.1")


def test_server_authentication(runner: Process):
    res = requests.get(f"http://{HOST}:{_static_server_port}/health")
    assert res.status_code == 401
    assert res.json()["code"] == "UNAUTHORIZED"

    res = requests.get(
        f"http://{HOST}:{_static_server_port}/health", params={"secret": "secret"}
    )
    assert res.status_code == 200
    assert res.content == b"OK"

    res = requests.get(
        f"http://{HOST}:{_static_server_port}/health", params={"secret": "wrong"}
    )
    assert res.status_code == 401
    assert res.json()["code"] == "UNAUTHORIZED"

    res = requests.get(
        f"http://{HOST}:{_static_server_port}/health",
        headers={"Authorization": "Bearer secret"},
    )
    assert res.status_code == 200
    assert res.content == b"OK"

    res = requests.get(
        f"http://{HOST}:{_static_server_port}/health",
        headers={"Authorization": "Bearer wrong"},
    )
    assert res.status_code == 401
    assert res.json()["code"] == "UNAUTHORIZED"


def test_server_export_local():
    runner = CliRunner()
    port = _next_port()
    with runner.isolated_filesystem() as tmpdir:
        ctx = get_context()
        error_queue = ctx.Queue()
        p = ctx.Process(
            target=serve_server,
            args=(HOST, port, None, ".", f"{tmpdir}", {"home": ""}),
            kwargs={"error_queue": error_queue, "extra_env": _rust_extra_env()},
        )
        p._error_queue = error_queue
        try:
            start_server_process(p, port)
            init_project(
                runner,
                args=["init", "--mono"],
                env=dict(UNFURL_HOME=""),
            )
            # compare the export request output to the export command output
            for export_format in ["deployment", "environments"]:
                res = requests.get(
                    f"http://{HOST}:{port}/export?format={export_format}"
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


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
def test_server_export_remote():
    runner = CliRunner()
    use_rust = bool(os.environ.get("UNFURL_TEST_RUST_SERVER"))
    with runner.isolated_filesystem():
        port = _next_port()
        ctx = get_context()
        error_queue = ctx.Queue()
        # When the Rust proxy is active, redirect its logs to a temp file
        # so we can assert on cache hit/miss messages.
        rust_log_file = None
        extra_env = _rust_extra_env()
        if use_rust:
            rust_log_fd, rust_log_file = tempfile.mkstemp(
                prefix="rust-server-", suffix=".log"
            )
            os.close(rust_log_fd)
            extra_env["UNFURL_LOGFILE"] = rust_log_file
            # Ensure the Rust server logs at DEBUG level so cache messages appear.
            extra_env["UNFURL_LOGGING"] = "debug"
        p = ctx.Process(
            target=serve_server,
            args=(HOST, port, None, ".", ".", {"home": ""}, CLOUD_TEST_SERVER),
            kwargs={"error_queue": error_queue, "extra_env": extra_env},
        )
        p._error_queue = error_queue
        try:
            start_server_process(p, port)
            run_cmd(
                runner,
                [
                    "--home",
                    "",
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
                    # Snapshot log position before this iteration so assertions
                    # only inspect entries produced by the current request(s).
                    log_offset = 0
                    if use_rust and rust_log_file:
                        with open(rust_log_file) as _f:
                            _f.seek(0, 2)
                            log_offset = _f.tell()
                    res = requests.get(
                        f"http://{HOST}:{port}/export",
                        params={
                            "auth_project": project_id,
                            "latest_commit": last_commit,  # enable caching but just get the latest in the cache
                            "format": export_format,
                        },
                        headers={
                            "If-None-Match": etag,
                            "X-Git-Credentials": b64encode("username:token".encode()),
                        },
                    )
                    if msg == "cache miss for":
                        assert res.status_code == 200
                        etag = res.headers.get("Etag") or ""
                        assert etag
                        # don't bother re-exporting the second time
                        exported = run_cmd(
                            runner,
                            [
                                "--home",
                                "",
                                "export",
                                "dashboard",
                                "--format",
                                export_format,
                            ],
                            env={"UNFURL_LOGGING": "critical"},
                        )
                        assert exported
                        # Strip out output from the http server
                        output = exported.output
                        cleaned_output = output[max(output.find("{"), 0) :]
                        expected = _strip_sourceinfo(json.loads(cleaned_output))
                        assert (
                            _strip_sourceinfo(res.json()) == expected
                        )  # , f"{pformat(res.json(), depth=2, compact=True)}\n != \n{pformat(expected, depth=2, compact=True)}"
                    else:
                        # Cache hit: poll until server returns 304 via ETag match.
                        # Rust computes the same ETag as Python and honours If-None-Match,
                        # so both paths converge on 304 once Redis is populated.
                        res = wait_for_status(
                            f"http://{HOST}:{port}/export",
                            params={
                                "auth_project": project_id,
                                "latest_commit": last_commit,
                                "format": export_format,
                            },
                            headers={
                                "If-None-Match": etag,
                                "X-Git-Credentials": b64encode(
                                    "username:token".encode()
                                ),
                            },
                            expected=304,
                            timeout=15.0,
                        )

                    file_path = server._get_filepath(export_format, "")
                    key = server.CacheEntry(
                        project_id, "main", file_path, export_format
                    ).cache_key()
                    if use_rust:
                        # Rust key includes the cache prefix
                        cache_prefix = os.environ.get("CACHE_KEY_PREFIX", "ufsv::")
                        rust_key = f"{cache_prefix}{key}"
                        assert rust_log_file, (
                            "Rust log file should be set when UNFURL_TEST_RUST_SERVER=1"
                        )
                        with open(rust_log_file) as _f:
                            _f.seek(log_offset)
                            new_log = _f.read()
                        print(new_log)
                        if msg == "cache miss for":
                            assert f"cache miss (no entry): {rust_key}" in new_log, (
                                f"Expected 'cache miss (no entry): {rust_key}' in Rust log for {export_format}:\n{new_log}"
                            )
                        else:
                            assert f"cache hit etag match: {rust_key}" in new_log, (
                                f"Expected 'cache hit etag match: {rust_key}' in Rust log for {export_format}:\n{new_log}"
                            )

            # XXX
            # caplog, capsys, capfd capture log messages from uvicorn but not from the request workers
            # pytest -s does output those messages to the console if set_start_method is set to "fork"
            # visually confirmed this assert:
            # assert f"{msg} {key}" in caplog.text

            # test with a blueprint
            run_cmd(
                runner,
                [
                    "--home",
                    "",
                    "clone",
                    "--empty",
                    f"{CLOUD_TEST_SERVER}/onecommons/project-templates/application-blueprint",
                ],
            )
            last_commit = GitRepo(Repo("application-blueprint")).revision
            res = requests.get(
                f"http://{HOST}:{port}/export",
                params={
                    "auth_project": "onecommons/project-templates/application-blueprint",
                    "latest_commit": last_commit,  # enable caching but just get the latest in the cache
                    "format": "blueprint",
                    "branch": "(MISSING)",
                },
            )
            # branch=(MISSING) will log: Package unfurl.cloud/onecommons/project-templates/application-blueprint is looking for earliest remote tags v* on https://unfurl.cloud/onecommons/project-templates/application-blueprint.git
            assert res.status_code == 200
            # assert res.status_code == 304
            # etag = res.headers.get("Etag") or ""
            exported = run_cmd(
                runner,
                [
                    "--home",
                    "",
                    "export",
                    "--format",
                    "blueprint",
                    "application-blueprint/ensemble-template.yaml",
                ],
                env={"UNFURL_LOGGING": "critical"},
            )
            assert exported
            # Strip out output from the http server
            output = exported.output
            cleaned_output = output[max(output.find("{"), 0) :]
            expected = _strip_sourceinfo(json.loads(cleaned_output))
            assert _strip_sourceinfo(res.json()) == expected, (
                f"{pformat(res.json(), depth=2, compact=True)}\n != \n{pformat(expected, depth=2, compact=True)}"
            )

            dep_commit = GitRepo(Repo("application-blueprint/std")).revision
            etag = server._make_etag(
                hex(
                    int(last_commit, 16)
                    ^ int(get_package_digest(), 16)
                    ^ int(dep_commit, 16)
                )
            )
            # Poll for cached response to make test robust against async cache population in CI
            res = wait_for_status(
                f"http://{HOST}:{port}/export",
                params={
                    "auth_project": "onecommons/project-templates/application-blueprint",
                    "latest_commit": last_commit,  # enable caching but just get the latest in the cache
                    "format": "blueprint",
                },
                headers={"If-None-Match": etag},
                expected=304,
                timeout=15.0,
            )
            # Save Redis cache entries as fixtures for Rust unit tests.
            _save_rust_fixtures()
        finally:
            p.terminate()
            p.join()
            if rust_log_file and os.path.exists(rust_log_file):
                os.unlink(rust_log_file)


def test_populate_cache(runner: Process):
    project_ids = [
        "onecommons/project-templates/dashboard",
        "onecommons/project-templates/dashboard",
        "onecommons/project-templates/application-blueprint",
    ]
    files = ["unfurl.yaml", "ensemble/ensemble.yaml", "ensemble-template.yaml"]
    port = _static_server_port
    for file_path, project_id in zip(files, project_ids):
        res = requests.post(
            f"http://{HOST}:{port}/populate_cache",
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


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
def test_server_update_deployment():
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            initial_deployment = deployment.format("initial")
            p, port, last_commit = set_up_deployment(runner, initial_deployment)

            target_patch = patch.format("target")
            res = requests.post(
                f"http://{HOST}:{port}/update_ensemble?auth_project=remote",
                json={"patch": json.loads(target_patch), "latest_commit": last_commit},
            )
            assert res.status_code == 200
            new_commit = res.json()["commit"]
            assert last_commit != new_commit
            last_commit = new_commit
            # os.system("git --git-dir server/public/remote/main/.git log -p")

            res = requests.get(
                f"http://{HOST}:{port}/export",
                params={
                    "auth_project": "remote",
                    "latest_commit": last_commit,  # enable caching but just get the latest in the cache
                    "format": "deployment",
                },
            )
            assert res.status_code == 200
            assert (
                res.json()["ResourceTemplate"]["container_service"]["properties"][0][
                    "name"
                ]
                == "container"
            )

            os.chdir("remote")
            # server pushes to remote.git which needs to be a bare repository
            # so pull from there to verify the push
            os.system("git pull ../remote.git")

            with open("ensemble/ensemble.yaml", "r") as f:
                data = yaml.load(f.read())
                assert (
                    (
                        data["spec"]["service_template"]["topology_template"][
                            "node_templates"
                        ]["container_service"]["properties"]["container"][
                            "environment"
                        ]["VAR"]
                    )
                    == "target"
                )

            # test that the server recovers from a bad repo before trying to patch
            # by creating a conflict between the server's local repo and the remote repo
            commit_foo("bar")
            os.system("git push ../remote.git")  # push to remote.git
            client_repo = GitRepo(Repo.init("."))
            last_commit = client_repo.revision

            os.chdir("../server/public/remote")
            commit_foo("foo")
            os.chdir("../../../remote")

            # test deleting

            res = requests.post(
                f"http://{HOST}:{port}/update_ensemble?auth_project=remote",
                json={
                    "patch": json.loads(delete_patch),
                    "latest_commit": last_commit,
                },
            )
            assert res.status_code == 200
            last_commit = res.json()["commit"]
            assert last_commit

            # server pushes to remote.git which needs to be a bare repository
            # so pull from there to verify the push
            os.system("git pull  ../remote.git")
            with open("ensemble/ensemble.yaml", "r") as f:
                data = yaml.load(f.read())
                assert not data["spec"]["service_template"]["topology_template"][
                    "node_templates"
                ]

            provider_patch = [
                {
                    "name": "gcp",
                    "primary_provider": {
                        "name": "primary_provider",
                        "type": "unfurl.relationships.ConnectsTo.GoogleCloudProject",
                        "__typename": "ResourceTemplate",
                    },
                    "connections": {
                        "primary_provider": {
                            "name": "primary_provider",
                            "type": "unfurl.relationships.ConnectsTo.GoogleCloudProject",
                            "__typename": "ResourceTemplate",
                        }
                    },
                    "__typename": "DeploymentEnvironment",
                }
            ]
            res = requests.post(
                f"http://{HOST}:{port}/create_provider?auth_project=remote",
                json={
                    "environment": "gcp",
                    "deployment_blueprint": None,
                    "deployment_path": "environments/gcp/primary_provider",
                    "patch": provider_patch,
                    "commit_msg": "Create environment gcp",
                    "latest_commit": last_commit,
                },
            )
            assert res.status_code == 200
            assert res.content.startswith(b'{"commit":')

            assert not os.system("git pull --commit --no-edit origin main")
            with open("unfurl.yaml", "r") as f:
                data = yaml.load(f.read())
                # check that the environment was added and an ensemble was created
                assert (
                    data["environments"]["gcp"]["connections"]["primary_provider"][
                        "type"
                    ]
                    == "unfurl.relationships.ConnectsTo.GoogleCloudProject"
                )
                assert data["ensembles"][-1]["alias"] == "primary_provider", data

            res = requests.post(
                f"http://{HOST}:{port}/clear_project_file_cache?auth_project=remote",
            )
            # 'remote:main::localenv', 'remote:pull:...', 'remote:main:ensemble/ensemble.yaml:deployment'
            assert res.content == b"3"  # 3 keys deleted
            assert res.status_code == 200

        finally:
            if p:
                p.terminate()
                p.join()


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
def test_get_types():
    """GET /types returns a GraphQL-style JSON database of TOSCA resource types."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            p, port, last_commit = set_up_deployment(runner, deployment.format("initial"))
            res = requests.get(
                f"http://{HOST}:{port}/types",
                params={
                    "auth_project": "remote",
                    "latest_commit": last_commit,
                    "file": "ensemble/ensemble.yaml",
                },
            )
            assert res.status_code == 200, res.json()
            data = res.json()
            assert "ResourceType" in data, list(data.keys())
            assert len(data["ResourceType"]) > 0
        finally:
            if p:
                p.terminate()
                p.join()


def test_empty_cache():
    """POST /empty_cache clears all cache entries when called with the admin project."""
    runner = CliRunner()
    port = _next_port()
    with runner.isolated_filesystem():
        p = None
        try:
            ctx = get_context()
            error_queue = ctx.Queue()
            p = ctx.Process(
                target=serve_server,
                args=(HOST, port, "secret", ".", "", {}, CLOUD_TEST_SERVER),
                kwargs={
                    "error_queue": error_queue,
                    # Pass via extra_env so it reaches the child regardless of
                    # multiprocessing start method (forkserver on Linux py3.14+
                    # does not inherit os.environ changes made after the forkserver starts).
                    "extra_env": {"UNFURL_SERVER_ADMIN_PROJECT": "admin/project", **_rust_extra_env()},
                },
            )
            p._error_queue = error_queue
            start_server_process(p, port)

            # Authorized: correct admin project → 200 OK
            res = requests.post(
                f"http://{HOST}:{port}/empty_cache",
                params={"secret": "secret", "auth_project": "admin/project"},
            )
            assert res.status_code == 200, res.json()
            assert res.content == b"OK"

            # Unauthorized: wrong project → 401
            res = requests.post(
                f"http://{HOST}:{port}/empty_cache",
                params={"secret": "secret", "auth_project": "wrong/project"},
            )
            assert res.status_code == 401
            assert res.json()["code"] == "UNAUTHORIZED"

            # Missing auth_project → 422 (APIFlask input validation: auth_project is required)
            res = requests.post(
                f"http://{HOST}:{port}/empty_cache",
                params={"secret": "secret"},
            )
            assert res.status_code == 422
        finally:
            if p:
                p.terminate()
                p.join()


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
def test_update_environment():
    """POST /update_environment adds an environment to unfurl.yaml."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            p, port, last_commit = set_up_deployment(runner, deployment.format("initial"))

            env_patch = [{"name": "staging", "__typename": "DeploymentEnvironment"}]
            res = requests.post(
                f"http://{HOST}:{port}/update_environment?auth_project=remote",
                json={"patch": env_patch, "latest_commit": last_commit},
            )
            assert res.status_code == 200, res.json()
            new_commit = res.json()["commit"]
            assert new_commit and new_commit != last_commit

            os.chdir("remote")
            os.system("git pull ../remote.git")
            with open("unfurl.yaml") as f:
                data = yaml.load(f.read())
            assert "staging" in data.get("environments", {}), data
        finally:
            if p:
                p.terminate()
                p.join()


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
def test_delete_environment():
    """POST /delete_environment removes a previously created environment from unfurl.yaml."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            p, port, last_commit = set_up_deployment(runner, deployment.format("initial"))

            # First create the environment
            env_patch = [{"name": "staging", "__typename": "DeploymentEnvironment"}]
            res = requests.post(
                f"http://{HOST}:{port}/update_environment?auth_project=remote",
                json={"patch": env_patch, "latest_commit": last_commit},
            )
            assert res.status_code == 200, res.json()
            last_commit = res.json()["commit"]
            assert last_commit

            # Now delete it
            del_patch = [
                {"name": "staging", "__typename": "DeploymentEnvironment", "__deleted": True}
            ]
            res = requests.post(
                f"http://{HOST}:{port}/delete_environment?auth_project=remote",
                json={"patch": del_patch, "latest_commit": last_commit},
            )
            assert res.status_code == 200, res.json()
            new_commit = res.json()["commit"]
            assert new_commit and new_commit != last_commit

            os.chdir("remote")
            os.system("git pull ../remote.git")
            with open("unfurl.yaml") as f:
                data = yaml.load(f.read())
            assert "staging" not in data.get("environments", {}), data
        finally:
            if p:
                p.terminate()
                p.join()


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
def test_delete_deployment():
    """POST /delete_deployment removes an ensemble registration from unfurl.yaml."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            p, port, last_commit = set_up_deployment(runner, deployment.format("initial"))

            # Create a provider so there is a registered deployment path to delete
            provider_patch = [{"name": "gcp", "__typename": "DeploymentEnvironment"}]
            res = requests.post(
                f"http://{HOST}:{port}/create_provider?auth_project=remote",
                json={
                    "environment": "gcp",
                    "deployment_path": "environments/gcp/primary_provider",
                    "patch": provider_patch,
                    "latest_commit": last_commit,
                },
            )
            assert res.status_code == 200, res.json()
            last_commit = res.json()["commit"]
            assert last_commit

            # Remove the ensemble registration via /delete_deployment
            del_patch = [
                {
                    "name": "environments/gcp/primary_provider",
                    "__typename": "DeploymentPath",
                    "__deleted": True,
                }
            ]
            res = requests.post(
                f"http://{HOST}:{port}/delete_deployment?auth_project=remote",
                json={"patch": del_patch, "latest_commit": last_commit},
            )
            assert res.status_code == 200, res.json()
            new_commit = res.json()["commit"]
            assert new_commit and new_commit != last_commit

            os.chdir("remote")
            os.system("git pull ../remote.git")
            with open("unfurl.yaml") as f:
                data = yaml.load(f.read())
            ensemble_files = [e.get("file", "") for e in data.get("ensembles", [])]
            assert not any("primary_provider" in f for f in ensemble_files), data
        finally:
            if p:
                p.terminate()
                p.join()


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
def test_create_ensemble():
    """POST /create_ensemble creates a new ensemble at the given deployment path."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            p, port, last_commit = set_up_deployment(runner, deployment.format("initial"))

            res = requests.post(
                f"http://{HOST}:{port}/create_ensemble?auth_project=remote",
                json={
                    "patch": [],
                    "deployment_path": "deployments/new-app",
                    "latest_commit": last_commit,
                },
            )
            assert res.status_code == 200, res.json()
            new_commit = res.json()["commit"]
            assert new_commit and new_commit != last_commit

            os.chdir("remote")
            os.system("git pull ../remote.git")
            assert os.path.exists("deployments/new-app/ensemble.yaml"), os.listdir(".")
        finally:
            if p:
                p.terminate()
                p.join()


def test_find_rust_server_bin():
    """Verify _find_rust_server_bin() locates the unfurl-server binary.

    Build it first with: cd rust && cargo build -p unfurl-server
    """
    if not os.environ.get("UNFURL_TEST_RUST_SERVER"):
        pytest.skip("Set UNFURL_TEST_RUST_SERVER=1 to run Rust server tests")
    from unfurl.server.serve import _find_rust_server_bin

    bin_path = _find_rust_server_bin()
    assert bin_path is not None, (
        "unfurl-server binary not found; build it with: "
        "cd rust && cargo build -p unfurl-server"
    )
    assert os.path.isfile(bin_path), f"path {bin_path!r} is not a file"
    assert os.access(bin_path, os.X_OK), f"{bin_path!r} is not executable"


def test_rust_server_bad_redis():
    """Rust server must exit non-zero and log an error when Redis is unreachable.

    Runs the binary directly so we capture its stderr without needing the full
    Python server stack.  The binary exits before binding any port.
    """
    import subprocess

    if not os.environ.get("UNFURL_TEST_RUST_SERVER"):
        pytest.skip("Set UNFURL_TEST_RUST_SERVER=1 to run Rust server tests")
    from unfurl.server.serve import _find_rust_server_bin

    bin_path = _find_rust_server_bin()
    if not bin_path:
        pytest.skip("unfurl-server binary not found")

    result = subprocess.run(
        [bin_path],
        env={
            **os.environ,
            # Point at a TCP port where nothing is listening.
            "CACHE_REDIS_URL": "redis://127.0.0.1:19999",
            "UNFURL_HOST": "127.0.0.1",
            "UNFURL_PORT": "19998",
        },
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0, (
        "Expected non-zero exit code when Redis is unreachable"
    )
    stderr = result.stderr.decode()
    assert "Redis" in stderr and ("failed" in stderr or "invalid" in stderr), (
        f"Expected Redis error message in stderr, got:\n{stderr}"
    )


def test_rust_server_proxy():
    """Verify Rust proxy forwards /health correctly. Requires UNFURL_TEST_RUST_SERVER=1."""
    if not os.environ.get("UNFURL_TEST_RUST_SERVER"):
        pytest.skip("Set UNFURL_TEST_RUST_SERVER=1 to run Rust server tests")

    port = _next_port()
    backend_port = port + 1  # Python waitress shifts to port+1 when Rust proxy is active
    ctx = get_context("spawn")
    error_queue = ctx.Queue()
    rust_log_fd, rust_log_file = tempfile.mkstemp(prefix="rust-proxy-", suffix=".log")
    os.close(rust_log_fd)
    # _rust_extra_env() already includes Redis config and errors if Redis is absent.
    # Capture Rust server stderr to a log file so we can assert on log messages.
    extra_env = {
        **_rust_extra_env(),
        "UNFURL_LOGGING": "debug",
        "UNFURL_LOGFILE": rust_log_file,
    }
    p = ctx.Process(
        target=serve_server,
        args=(HOST, port, "secret", ".", "", {}),
        kwargs={
            "error_queue": error_queue,
            "extra_env": extra_env,
        },
    )
    p._error_queue = error_queue
    start_server_process(p, port)
    try:
        # /health requires Authorization when a secret is configured.
        resp = requests.get(
            f"http://{HOST}:{port}/health",
            headers={"Authorization": "Bearer secret"},
            timeout=5,
        )
        assert resp.status_code == 200
        # Allow time for the Rust server to flush log output to the file.
        time.sleep(0.5)
        with open(rust_log_file, "r") as f:
            log_contents = f.read()
        # Verify the Rust server logged a hyper connection pool message,
        # confirming it actually proxied the request through to the Python backend.
        assert (
            "hyper_util" in log_contents and "pooling idle connection" in log_contents
        ), f"Expected hyper_util pool log in Rust server output, got:\n{log_contents}"
    finally:
        p.terminate()
        p.join()
        if os.path.exists(rust_log_file):
            os.unlink(rust_log_file)


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
