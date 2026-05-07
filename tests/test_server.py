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
from unfurl.server import endpoints as server_endpoints
from unfurl.server import serve as server
from unfurl.server import gui
from unfurl.packages import is_semver_compatible_with

import pytest
from tests.utils import init_project, run_cmd
from unfurl.repo import GitRepo
from unfurl.yamlloader import yaml
from unfurl.util import change_cwd, get_package_digest, clean_output
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


def _poll_rust_log(
    log_file: str, offset: int, pattern: str, timeout: float = 10.0
) -> str:
    """Read new log entries from *offset*, polling until *pattern* appears.

    The Rust server writes to stderr which is piped to a file.  On Linux the
    pipe may be fully buffered, so the log entry can lag behind the HTTP
    response.  Poll for up to *timeout* seconds before giving up.
    """
    deadline = time.time() + timeout
    while True:
        with open(log_file) as f:
            f.seek(offset)
            text = f.read()
        if pattern in text or time.time() >= deadline:
            if not text:
                # Diagnostic: read full file to distinguish "empty file"
                # from "nothing new after offset".
                with open(log_file) as f:
                    full = f.read()
                if full:
                    text = (
                        f"[poll_rust_log] nothing after offset={offset}, "
                        f"but file has {len(full)} bytes total. "
                        f"Last 500 chars: {full[-500:]}"
                    )
            return text
        time.sleep(0.25)


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


def _terminate_process(p: Process, timeout: float = 10.0) -> None:
    """Terminate a process and wait for it to exit, forcibly killing if needed."""
    p.terminate()
    p.join(timeout=timeout)
    if p.is_alive():
        p.kill()
        p.join(timeout=5.0)


#  Increment port just in case server ports aren't closed in time for next test
#  NB: if server processes aren't terminated: pkill -fl spawn_main
def _next_port():
    global _server_port
    # When the Rust proxy is active each server occupies TWO ports (N=Rust front-end,
    # N+1=Python backend).  Always increment by 2 so parametrized redis-rust variants
    # never conflict with the next test's port.
    _server_port += 2
    return _server_port


def _save_rust_fixtures(cache_prefix: str = "") -> None:
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

    if not cache_prefix:
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


def _rust_extra_env(name: str = "") -> dict:
    """Extra env vars to enable the Rust proxy when UNFURL_TEST_RUST_SERVER=1.

    Redis is required for correct Rust proxy operation:
    - Write endpoints are queued via Redis (without Redis the query string is dropped)
    - Read endpoints use Redis for caching

    Raises RuntimeError if UNFURL_TEST_RUST_SERVER=1 but UNFURL_TEST_REDIS_URL is not set.
    Forwards Redis config explicitly so spawn-based child processes and the Rust
    subprocess all use the same cache backend and key prefix.
    """
    rust_env = os.environ.get("UNFURL_TEST_RUST_SERVER")
    if rust_env == "0":
        print("UNFURL_TEST_RUST_SERVER=0, running server without Rust proxy")
        return {"UNFURL_RUST_SERVER": "0"}
    if not UNFURL_TEST_REDIS_URL:
        raise RuntimeError(
            "UNFURL_TEST_RUST_SERVER=1 requires UNFURL_TEST_REDIS_URL to be set. "
            "The Rust proxy requires Redis for correct operation of write endpoints."
        )
    print("running server with Rust proxy")
    return {
        "UNFURL_RUST_SERVER": "1",
        "CACHE_TYPE": "RedisCache",
        "CACHE_REDIS_URL": UNFURL_TEST_REDIS_URL,
        # Forward the unique per-run prefix set by module-level code so all
        # processes (Python server, Rust subprocess) share the same namespace.
        "CACHE_KEY_PREFIX": _variant_prefix(name),
        "CACHE_DEFAULT_TIMEOUT": "120",
        # Forward UNFURL_LOGGING so _start_rust_server can map it to RUST_LOG.
        "UNFURL_LOGGING": os.environ.get("UNFURL_LOGGING", "debug"),
    }


def serve_server(
    *args, error_queue: Queue = None, extra_env: dict = None, py_log_file=None, **kw
):
    """Wrapper around server.serve that forwards child start errors to a Queue.

    extra_env: env vars to set in the child process before starting the server.
    Use this instead of relying on os.environ inheritance, which is unreliable
    with the forkserver start method (the default on Linux since Python 3.14).
    """
    if extra_env:
        os.environ.update(extra_env)
        # unfurl's logs.initialize_logging() ran at import time (possibly in a
        # forkserver template before UNFURL_LOGGING was set). Re-apply the level
        # now so the in-process LOGGING dict — and anything that reads it via
        # get_console_log_level(), like _start_proxy_server's RUST_LOG mapping —
        # reflects the updated env.
        loglevel_env = extra_env.get("UNFURL_LOGGING")
        if loglevel_env:
            from unfurl.logs import Levels, set_console_log_level

            try:
                set_console_log_level(Levels[loglevel_env.upper()])
            except KeyError:
                pass
    # With forkserver/spawn, the child's logging isn't captured by pytest.
    # If a log file path is provided, add a FileHandler so Python server logs
    # are written to the same file as the Rust server logs (or a separate one).
    if py_log_file:
        fh = logging.FileHandler(py_log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(levelname)-8s %(name)s %(message)s"))
        logging.getLogger().addHandler(fh)
    try:
        return server.serve(*args, **kw)
    except Exception:
        tb = traceback.format_exc()
        if error_queue is not None:
            error_queue.put(tb)
        logging.warning("server.serve unexpectedly failed", exc_info=True)
        raise


def start_server_process(
    process_obj, port, hosts=(HOST, "::1"), timeout=12.0, is_rust=False
):
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
                    if is_rust:
                        backend_port = port + 1
                        backend_deadline = time.time() + timeout
                        backend_connected = False
                        while time.time() < backend_deadline:
                            try:
                                with socket.create_connection(
                                    (HOST, backend_port), timeout=1.0
                                ):
                                    backend_connected = True
                                    break
                            except OSError:
                                time.sleep(0.1)
                        if not backend_connected:
                            raise RuntimeError(
                                f"Python backend for the Rust server not reachable on port "
                                f"{backend_port} after {timeout}s — "
                                "unfurl-server binary may not have started correctly"
                            )
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


def _get_server_params():
    """Pytest.param variants: no-redis, redis, redis-rust, queue-rust."""
    if not os.getenv("UNFURL_TEST_REDIS_URL"):
        return ["no-redis"]
    if os.getenv("UNFURL_TEST_RUST_SERVER") == "0":
        return ["redis", "no-redis"]
    return ["no-redis", "redis", "redis-rust", "queue-rust"]


def _variant_prefix(name: str) -> str:
    """Build a unique cache key prefix by appending `name` to the base module-level prefix."""
    base = os.environ.get("CACHE_KEY_PREFIX", "ufsv::").rstrip(":")
    return f"{base}-{name}::" if name else f"{base}::"


def _env_for(variant: str, name: str = "") -> dict:
    """Convert a server variant string to an env dict for serve_server.

    `name` is appended to CACHE_KEY_PREFIX so each test/variant has an isolated
    Redis namespace and cannot read stale entries written by a different test.
    """
    prefix = _variant_prefix(f"{name}-{variant}" if name else variant)
    base_redis = {
        "CACHE_TYPE": "RedisCache",
        "CACHE_REDIS_URL": UNFURL_TEST_REDIS_URL or "",
        "CACHE_KEY_PREFIX": prefix,
        "CACHE_DEFAULT_TIMEOUT": "120",
    }
    if "rust" in variant:
        return {
            **base_redis,
            "UNFURL_RUST_SERVER": "1",
            "UNFURL_LOGGING": os.environ.get("UNFURL_LOGGING", "debug"),
            "UNFURL_BATCH_WINDOW_SECS": "1",
        }
    if variant == "redis":
        return {**base_redis, "UNFURL_RUST_SERVER": "0"}
    assert variant == "no-redis"
    return {
        "UNFURL_RUST_SERVER": "0",
        "CACHE_TYPE": "simple",
        "CACHE_REDIS_URL": "",
        "CACHE_KEY_PREFIX": prefix,
    }


QUEUE_SLEEP = 3.5  # seconds to wait for batch queue drain + backend processing (window=1s + poll + processing)


def _dump_server_logs(p, label=""):
    """Print server log files stored on the process object for diagnostics."""
    prefix = f"[{label}] " if label else ""
    for attr, name in [("_py_log_file", "Python"), ("_rust_log_file", "Rust")]:
        path = getattr(p, attr, None)
        if path and os.path.exists(path):
            with open(path) as f:
                contents = f.read()
            if contents:
                print(f"\n=== {prefix}{name} server log ({path}) ===")
                print(contents[-5000:])
                print(f"=== end {name} server log ===\n")


def _post_write(url, json_body, server_env, queueid=0):
    """POST a write request, adding queueid for queue-rust variant."""
    if server_env == "queue-rust":
        json_body = {**json_body, "queueid": str(queueid)}
    return requests.post(url, json=json_body)


def _assert_commit(res, last_commit, server_env):
    """Assert the POST response has a new commit, or a queueid for queue-rust.

    Returns (new_commit, new_queueid) for queue-rust,
    or (new_commit, None) for other variants.
    """
    assert res.status_code == 200, res.json()
    data = res.json()
    if server_env == "queue-rust":
        new_queueid = data.get("queueid")
        assert new_queueid is not None, f"expected 'queueid' in response: {data}"
        # The response may include a new latest_commit if batch_patch produced one.
        new_commit = data.get("commit")
        return new_commit, new_queueid
    new_commit = data["commit"]
    assert new_commit and new_commit != last_commit
    return new_commit, None


def _wait_for_queue(server_env):
    """Sleep to let the batch queue drain if using queue-rust variant."""
    if server_env == "queue-rust":
        time.sleep(QUEUE_SLEEP)


def _get_latest_commit(bare_repo_path="remote.git"):
    """Read the latest commit from the bare repo after a queued batch has been processed."""
    return GitRepo(Repo(bare_repo_path)).revision


def _wait_for_new_commit(
    bare_repo_path: str,
    before_commit: str,
    timeout: float = 15.0,
    poll_interval: float = 0.5,
) -> str:
    """Poll the bare repo until its HEAD differs from before_commit or timeout expires.

    Returns the new commit hash. Fails the test on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        commit = _get_latest_commit(bare_repo_path)
        if commit != before_commit:
            return commit
        time.sleep(poll_interval)
    raise AssertionError(
        f"Timed out after {timeout}s waiting for a new commit in {bare_repo_path!r} "
        f"(still at {before_commit})"
    )


server_env = _get_server_params()


@pytest.fixture(params=_get_server_params())
def runner(request):
    server_env = request.param
    runner = CliRunner()
    with runner.isolated_filesystem() as tmpdir:
        os.environ["UNFURL_LOGGING"] = "TRACE"
        ctx = get_context()
        error_queue = ctx.Queue()
        server_process = ctx.Process(
            target=serve_server,
            args=(HOST, _static_server_port, "secret", ".", "", {}, CLOUD_TEST_SERVER),
            kwargs={
                "error_queue": error_queue,
                "extra_env": _env_for(server_env, "runner"),
            },
        )
        server_process._error_queue = error_queue
        try:
            start_server_process(
                server_process,
                _static_server_port,
                is_rust=("rust" in server_env),
            )

            yield server_process
        finally:
            _terminate_process(server_process)


def commit_foo(val: str):
    with open("foo", "w") as foo:
        foo.write(val)
    os.system("git add foo")
    os.system(f"git commit -m'{val}'")


def set_up_deployment(runner, deployment, server_env=None, name=""):
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
    repo.add_all(repo.working_dir)
    repo.commit("Add deployment")

    # we need a bare repo for push to work
    os.system("git clone --bare remote remote.git")
    assert repo.repo.create_remote("origin", "../remote.git")
    port = _next_port()

    use_rust = server_env and "rust" in server_env
    extra_env = _env_for(server_env or "no-redis", name)

    # Capture server logs to temp files for diagnostics.
    rust_log_file = None
    py_log_file = None
    py_log_fd, py_log_file = tempfile.mkstemp(prefix="py-server-", suffix=".log")
    os.close(py_log_fd)
    if use_rust:
        rust_log_fd, rust_log_file = tempfile.mkstemp(
            prefix="rust-server-", suffix=".log"
        )
        os.close(rust_log_fd)
        extra_env["UNFURL_LOGFILE"] = rust_log_file
        extra_env["UNFURL_LOGGING"] = os.environ.get("UNFURL_LOGGING", "debug")

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
        kwargs={
            "error_queue": error_queue,
            "extra_env": extra_env,
            "py_log_file": py_log_file,
        },
    )
    p._error_queue = error_queue
    # Stash log file paths on the process for callers to read.
    p._py_log_file = py_log_file
    p._rust_log_file = rust_log_file
    try:
        start_server_process(p, port, is_rust=use_rust)

        assert repo.revision
        return p, port, repo.revision
    except Exception:
        _terminate_process(p)
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


@pytest.mark.parametrize("server_env", server_env)
def test_server_export_local(server_env):
    runner = CliRunner()
    port = _next_port()
    with runner.isolated_filesystem() as tmpdir:
        ctx = get_context()
        error_queue = ctx.Queue()
        p = ctx.Process(
            target=serve_server,
            args=(HOST, port, None, ".", f"{tmpdir}", {"home": ""}),
            kwargs={
                "error_queue": error_queue,
                "extra_env": _env_for(server_env, "export-local"),
            },
        )
        p._error_queue = error_queue
        try:
            start_server_process(p, port, is_rust=("rust" in server_env))
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

            # Error: invalid format (rejected by schema validation)
            res = requests.get(
                f"http://{HOST}:{port}/export?format=invalid"
            )
            assert res.status_code == 422
        finally:
            _terminate_process(p)


def _strip_sourceinfo(export, log=False):
    for name, typedef in export["ResourceType"].items():
        _sourceinfo = typedef.pop("_sourceinfo", None)
        if _sourceinfo and log:
            print(name, _sourceinfo)


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
@pytest.mark.parametrize("server_env", server_env)
def test_server_export_remote(server_env):
    runner = CliRunner()
    use_rust = "rust" in server_env
    with runner.isolated_filesystem():
        port = _next_port()
        ctx = get_context()
        error_queue = ctx.Queue()
        # When the Rust proxy is active, redirect its logs to a temp file
        # so we can assert on cache hit/miss messages.
        rust_log_file = None
        py_log_file = None
        extra_env = _env_for(server_env, "export-remote")
        # Always capture Python server logs to a file so they appear in CI
        # output (the child process's logging is not captured by pytest with
        # forkserver/spawn start methods) and caplog, capsys, capfd fixtures won't work
        py_log_fd, py_log_file = tempfile.mkstemp(prefix="py-server-", suffix=".log")
        os.close(py_log_fd)
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
            kwargs={
                "error_queue": error_queue,
                "extra_env": extra_env,
                "py_log_file": py_log_file,
            },
        )
        p._error_queue = error_queue
        try:
            start_server_process(p, port, is_rust=("rust" in server_env))
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
                # SimpleCache ignores CACHE_KEY_PREFIX; only RedisCache prepends it.
                _pfx = (
                    extra_env.get("CACHE_KEY_PREFIX", "ufsv::")
                    if extra_env.get("CACHE_TYPE") == "RedisCache"
                    else ""
                )
                project_id = "onecommons/project-templates/dashboard"
                file_path = server._get_filepath(export_format, "")
                key = server.CacheEntry(
                    project_id, "main", file_path, export_format
                ).cache_key()
                for msg in ("cache miss for", "cache hit for"):
                    # test caching
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

                        # check that export matches the server response (after stripping _sourceinfo which includes non-deterministic file paths)
                        output = exported.output
                        cleaned_output = output[max(output.find("{"), 0) :]
                        expected = _strip_sourceinfo(json.loads(cleaned_output))
                        assert (
                            _strip_sourceinfo(res.json()) == expected
                        )  # , f"{pformat(res.json(), depth=2, compact=True)}\n != \n{pformat(expected, depth=2, compact=True)}"

                        # Verify Python actually stored the cache entry in Redis.
                        if use_rust and UNFURL_TEST_REDIS_URL:
                            import redis as _redis_mod

                            _r = _redis_mod.from_url(UNFURL_TEST_REDIS_URL)
                            _full = f"{_pfx}{key}"
                            _val = _r.get(_full)
                            _keys = _r.keys(f"{_pfx}*")
                            assert _val is not None, (
                                f"Python server did not store cache entry in Redis.\n"
                                f"  Expected key: {_full}\n"
                                f"  Keys with prefix: {_keys}"
                            )
                            _r.close()
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
                    with open(py_log_file) as _f:
                        py_log = _f.read()
                        if not ("cache hit for" and use_rust):
                            log_msg = f"{msg} {_pfx}{key}"
                            assert log_msg in py_log, (
                                f"{log_msg} not found in Python log:\n{py_log}"
                            )

                    if use_rust:
                        # Rust key includes the cache prefix
                        cache_prefix = extra_env.get("CACHE_KEY_PREFIX", "ufsv::")
                        rust_key = f"{cache_prefix}{key}"
                        assert rust_log_file, (
                            "Rust log file should be set when UNFURL_TEST_RUST_SERVER=1"
                        )
                        # Poll the log file: the Rust server writes to stderr
                        # which is piped to a file; on Linux the pipe is fully
                        # buffered so the entry may not appear immediately after
                        # the HTTP response arrives.
                        if msg == "cache miss for":
                            expected_pattern = (
                                f"cache miss (no entry / nil): {rust_key}"
                            )
                        else:
                            expected_pattern = f"cache hit, etag matched: {rust_key}"
                        new_log = _poll_rust_log(
                            rust_log_file, log_offset, expected_pattern
                        )
                        # print(new_log)
                        new_log = clean_output(new_log)
                        if msg != "cache miss for":
                            # Check for etag mismatch first to give a diagnostic message
                            mismatch_marker = f"cache hit etag mismatch: {rust_key}"
                            assert mismatch_marker not in new_log, (
                                f"Rust ETag mismatch for {export_format} "
                                f"(if_none_match={etag!r}): "
                                + next(
                                    (
                                        line
                                        for line in new_log.splitlines()
                                        if "etag mismatch" in line
                                    ),
                                    mismatch_marker,
                                )
                            )
                        if not new_log:
                            # Rust log is empty — check if the Rust server is
                            # actually running by inspecting the Python log.
                            with open(py_log_file) as _pf:
                                _py = _pf.read()
                            _diag = (
                                f"rust_log_file={rust_log_file} "
                                f"size={os.path.getsize(rust_log_file)} "
                                f"log_offset={log_offset} server_env={server_env}"
                                f"\nPython log tail:\n{_py}"
                            )
                            assert False, f"Rust log file is empty\n{_diag}"
                        assert expected_pattern in new_log, (
                            f"Expected {expected_pattern!r} in Rust log for {export_format}:\n{new_log}"
                        )

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
            if use_rust:
                _save_rust_fixtures(extra_env.get("CACHE_KEY_PREFIX", ""))
        finally:
            _terminate_process(p)
            # Print Python server logs so they appear in CI output.
            # if os.path.exists(py_log_file):
            #     with open(py_log_file) as _f:
            #         py_log = _f.read()
            #     if py_log:
            #         print(f"\n=== Python server log ({py_log_file}) ===")
            #         print(py_log[-5000:])  # last 5000 chars
            #         print("=== end Python server log ===\n")
            #     os.unlink(py_log_file)
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
@pytest.mark.parametrize("server_env", server_env)
def test_server_update_deployment(server_env):
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            initial_deployment = deployment.format("initial")
            p, port, last_commit = set_up_deployment(
                runner,
                initial_deployment,
                server_env=server_env,
                name="update-deployment",
            )

            target_patch = patch.format("target")
            queueid = 0
            res = _post_write(
                f"http://{HOST}:{port}/update_ensemble?auth_project=remote",
                {"patch": json.loads(target_patch), "latest_commit": last_commit},
                server_env,
                queueid=queueid,
            )
            new_commit, queueid = _assert_commit(res, last_commit, server_env)
            # For queue-rust, new_commit may be None (only queueid returned).
            if new_commit:
                last_commit = new_commit

            if server_env == "queue-rust":
                last_commit = _wait_for_new_commit("remote.git", last_commit)
            else:
                _wait_for_queue(server_env)

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
            assert not os.waitstatus_to_exitcode(os.system("git pull ../remote.git"))

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
            # push to remote.git
            assert not os.waitstatus_to_exitcode(os.system("git push ../remote.git"))
            client_repo = GitRepo(Repo.init("."))
            last_commit = client_repo.revision

            os.chdir("../server/public/remote")
            commit_foo("foo")
            os.chdir("../../../remote")

            # test deleting
            # For queue-rust, reset queueid since last_commit changed (new key).
            queueid = 0
            res = _post_write(
                f"http://{HOST}:{port}/update_ensemble?auth_project=remote",
                {
                    "patch": json.loads(delete_patch),
                    "latest_commit": last_commit,
                },
                server_env,
                queueid=queueid,
            )
            new_commit, queueid = _assert_commit(res, last_commit, server_env)
            if new_commit:
                last_commit = new_commit

            if server_env == "queue-rust":
                last_commit = _wait_for_new_commit("../remote.git", last_commit)
            else:
                _wait_for_queue(server_env)

            # server pushes to remote.git which needs to be a bare repository
            # so pull from there to verify the push
            assert not os.waitstatus_to_exitcode(os.system("git pull  ../remote.git"))
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
            # For queue-rust, reset queueid since last_commit changed again.
            queueid = 0
            res = _post_write(
                f"http://{HOST}:{port}/create_provider?auth_project=remote",
                {
                    "environment": "gcp",
                    "deployment_blueprint": None,
                    "deployment_path": "environments/gcp/primary_provider",
                    "patch": provider_patch,
                    "commit_msg": "Create environment gcp",
                    "latest_commit": last_commit,
                },
                server_env,
                queueid=queueid,
            )
            new_commit, queueid = _assert_commit(res, last_commit, server_env)
            if new_commit:
                last_commit = new_commit

            _wait_for_queue(server_env)

            assert not os.waitstatus_to_exitcode(
                os.system("git pull --commit --no-edit origin main")
            )
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
                _terminate_process(p)


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
@pytest.mark.parametrize("server_env", server_env)
def test_get_types(server_env):
    """GET /types returns a GraphQL-style JSON database of TOSCA resource types."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            p, port, last_commit = set_up_deployment(
                runner,
                deployment.format("initial"),
                server_env=server_env,
                name="get-types",
            )
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
                _terminate_process(p)


@pytest.mark.parametrize("server_env", server_env)
def test_empty_cache(server_env):
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
                    "extra_env": {
                        "UNFURL_SERVER_ADMIN_PROJECT": "admin/project",
                        **_env_for(server_env, "empty-cache"),
                    },
                },
            )
            p._error_queue = error_queue
            start_server_process(p, port, is_rust=("rust" in server_env))

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
                _terminate_process(p)


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
@pytest.mark.parametrize("server_env", server_env)
def test_update_environment(server_env):
    """POST /update_environment adds an environment to unfurl.yaml."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            p, port, last_commit = set_up_deployment(
                runner,
                deployment.format("initial"),
                server_env=server_env,
                name="update-env",
            )

            env_patch = [{"name": "staging", "__typename": "DeploymentEnvironment"}]
            res = _post_write(
                f"http://{HOST}:{port}/update_environment?auth_project=remote",
                {"patch": env_patch, "latest_commit": last_commit},
                server_env,
            )
            new_commit, _ = _assert_commit(res, last_commit, server_env)
            _wait_for_queue(server_env)

            os.chdir("remote")
            os.system("git pull ../remote.git")
            with open("unfurl.yaml") as f:
                data = yaml.load(f.read())
            envs = data.get("environments", {})
            if envs is None:
                _dump_server_logs(p, "update-env")
            assert envs and "staging" in envs, data

            # Error: reserved environment name
            bad_patch = [{"name": "tasks", "__typename": "DeploymentEnvironment"}]
            res = requests.post(
                f"http://{HOST}:{port}/update_environment?auth_project=remote",
                json={"patch": bad_patch, "latest_commit": new_commit},
            )
            assert res.status_code == 400
            assert res.json()["code"] == "BAD_REQUEST"
            assert "reserved" in res.json()["message"]
        finally:
            _dump_server_logs(p, "update-env")
            if p:
                _terminate_process(p)


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
@pytest.mark.parametrize("server_env", server_env)
def test_delete_environment(server_env):
    """POST /delete_environment removes a previously created environment from unfurl.yaml."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            p, port, last_commit = set_up_deployment(
                runner,
                deployment.format("initial"),
                server_env=server_env,
                name="delete-env",
            )

            # First create the environment
            queueid = 0
            env_patch = [{"name": "staging", "__typename": "DeploymentEnvironment"}]
            res = _post_write(
                f"http://{HOST}:{port}/update_environment?auth_project=remote",
                {"patch": env_patch, "latest_commit": last_commit},
                server_env,
                queueid=queueid,
            )
            new_commit, new_queueid = _assert_commit(res, last_commit, server_env)
            if new_commit:
                last_commit = new_commit
            if new_queueid is not None:
                queueid = new_queueid

            # Now delete it
            del_patch = [
                {"name": "staging", "__typename": "DeploymentEnvironment", "__deleted": True}
            ]
            res = _post_write(
                f"http://{HOST}:{port}/delete_environment?auth_project=remote",
                {"patch": del_patch, "latest_commit": last_commit},
                server_env,
                queueid=queueid,
            )
            _assert_commit(res, last_commit, server_env)
            _wait_for_queue(server_env)

            # Verify both patches were batched together in a single batch_patch call.
            if server_env == "queue-rust":
                assert p._rust_log_file
                with open(p._rust_log_file) as f:
                    rust_log = f.read()
                assert "requests=2" in rust_log, (
                    f"expected requests=2 in Rust log:\n{rust_log[-2000:]}"
                )

            os.chdir("remote")
            os.system("git pull ../remote.git")
            with open("unfurl.yaml") as f:
                data = yaml.load(f.read())
            assert "staging" not in data.get("environments", {}), data
        finally:
            _dump_server_logs(p, "delete-env")
            if p:
                _terminate_process(p)


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
@pytest.mark.parametrize("server_env", server_env)
def test_delete_deployment(server_env):
    """POST /delete_deployment removes an ensemble registration from unfurl.yaml."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            p, port, last_commit = set_up_deployment(
                runner,
                deployment.format("initial"),
                server_env=server_env,
                name="delete-deployment",
            )

            # Create a provider so there is a registered deployment path to delete
            queueid = 0
            provider_patch = [{"name": "gcp", "__typename": "DeploymentEnvironment"}]
            res = _post_write(
                f"http://{HOST}:{port}/create_provider?auth_project=remote",
                {
                    "environment": "gcp",
                    "deployment_path": "environments/gcp/primary_provider",
                    "patch": provider_patch,
                    "latest_commit": last_commit,
                },
                server_env,
                queueid=queueid,
            )
            new_commit, new_queueid = _assert_commit(res, last_commit, server_env)
            if new_commit:
                last_commit = new_commit
            if new_queueid is not None:
                queueid = new_queueid

            # Remove the ensemble registration via /delete_deployment
            del_patch = [
                {
                    "name": "environments/gcp/primary_provider",
                    "__typename": "DeploymentPath",
                    "__deleted": True,
                }
            ]
            res = _post_write(
                f"http://{HOST}:{port}/delete_deployment?auth_project=remote",
                {"patch": del_patch, "latest_commit": last_commit},
                server_env,
                queueid=queueid,
            )
            _assert_commit(res, last_commit, server_env)
            _wait_for_queue(server_env)

            # Verify both patches were batched together in a single batch_patch call.
            if server_env == "queue-rust":
                assert p._rust_log_file
                with open(p._rust_log_file) as f:
                    rust_log = f.read()
                assert "requests=2" in rust_log, (
                    f"expected requests=2 in Rust log:\n{rust_log[-2000:]}"
                )

            os.chdir("remote")
            os.system("git pull ../remote.git")
            with open("unfurl.yaml") as f:
                data = yaml.load(f.read())
            ensemble_files = [e.get("file", "") for e in data.get("ensembles", [])]
            assert not any("primary_provider" in f for f in ensemble_files), (
                ensemble_files,
                data,
            )
        finally:
            _dump_server_logs(p, "delete-deployment")
            if p:
                _terminate_process(p)


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
@pytest.mark.parametrize("server_env", server_env)
def test_create_ensemble(server_env):
    """POST /create_ensemble creates a new ensemble at the given deployment path."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            p, port, last_commit = set_up_deployment(
                runner,
                deployment.format("initial"),
                server_env=server_env,
                name="create-ensemble",
            )

            res = _post_write(
                f"http://{HOST}:{port}/create_ensemble?auth_project=remote",
                {
                    "patch": [],
                    "deployment_path": "deployments/new-app",
                    "latest_commit": last_commit,
                },
                server_env,
                queueid=0,
            )
            _assert_commit(res, last_commit, server_env)
            # result unused; single write doesn't need chaining
            _wait_for_queue(server_env)

            os.chdir("remote")
            os.system("git pull ../remote.git")
            assert os.path.exists("deployments/new-app/ensemble.yaml"), os.listdir(".")
        finally:
            _dump_server_logs(p, "create-ensemble")
            if p:
                _terminate_process(p)


def test_find_rust_server_bin():
    """Verify _find_rust_server_bin() locates the unfurl-server binary.

    Build it first with: cd rust && cargo build -p unfurl-server
    """
    if os.environ.get("UNFURL_TEST_RUST_SERVER") == "0":
        pytest.skip("Skipping Rust server tests, UNFURL_TEST_RUST_SERVER=0 is set")
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

    if os.environ.get("UNFURL_TEST_RUST_SERVER") == "0":
        pytest.skip("Skipping Rust server tests, UNFURL_TEST_RUST_SERVER=0 is set")
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
    """Verify Rust proxy forwards /health correctly"""
    if os.environ.get("UNFURL_TEST_RUST_SERVER") == "0":
        pytest.skip("Skipping Rust server tests, UNFURL_TEST_RUST_SERVER=0 is set")
    if not os.environ.get("UNFURL_TEST_REDIS_URL"):
        pytest.skip("Skipping Rust server proxy test, UNFURL_TEST_REDIS_URL is not set")

    port = _next_port()
    backend_port = port + 1  # Python waitress shifts to port+1 when Rust proxy is active
    ctx = get_context("spawn")
    error_queue = ctx.Queue()
    rust_log_fd, rust_log_file = tempfile.mkstemp(prefix="rust-proxy-", suffix=".log")
    os.close(rust_log_fd)
    # _rust_extra_env() already includes Redis config and errors if Redis is absent.
    # Capture Rust server stderr to a log file so we can assert on log messages.
    extra_env = {
        **_rust_extra_env("rust-proxy"),
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
    start_server_process(p, port, is_rust=True)
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
        _terminate_process(p)
        if os.path.exists(rust_log_file):
            os.unlink(rust_log_file)


def test_server_cloudmap():
    """Test /cloudmap endpoint returns expected JSON graph."""
    from pathlib import Path

    fixture_dir = Path(__file__).parent / "fixtures"
    cloudmap_content = (fixture_dir / "expected_cloudmap.yaml").read_text()

    runner = CliRunner()
    port = _next_port()
    with runner.isolated_filesystem() as tmpdir:
        # Create a git repo with cloudmap.yaml at CWD
        with open("cloudmap.yaml", "w") as f:
            f.write(cloudmap_content)
        repo = GitRepo(Repo.init("."))
        repo.add_all(os.path.abspath("."))
        repo.commit_files([os.path.abspath("cloudmap.yaml")], "Add cloudmap")

        ctx = get_context()
        error_queue = ctx.Queue()
        p = ctx.Process(
            target=serve_server,
            args=(HOST, port, None, ".", f"{tmpdir}", {"home": ""}),
            kwargs={"error_queue": error_queue},
        )
        p._error_queue = error_queue
        try:
            start_server_process(p, port)
            base = f"http://{HOST}:{port}/graph"

            # Full graph
            res = requests.get(base)
            assert res.status_code == 200
            expected_full = json.loads(
                (fixture_dir / "cloudmap_graph.json").read_text()
            )
            assert res.json() == expected_full

            # Single artifact query
            artifact_url = "git://unfurl.cloud/onecommons/blueprints/odoo.git#:ensemble-template.yaml"
            res = requests.get(base, params={"url": artifact_url})
            assert res.status_code == 200
            expected_artifact = json.loads(
                (fixture_dir / "cloudmap_graph_artifact.json").read_text()
            )
            assert res.json() == expected_artifact

            # Dual record query (URL in both artifacts and instantiations)
            dual_url = "git://unfurl.cloud/feb20a/dashboard.git#:environments/aws/onecommons/blueprints/odoo/odoo-aws-1/ensemble.yaml"
            res = requests.get(base, params={"url": dual_url})
            assert res.status_code == 200
            expected_dual = json.loads(
                (fixture_dir / "cloudmap_graph_dual.json").read_text()
            )
            assert res.json() == expected_dual

            # Not found
            res = requests.get(base, params={"url": "nonexistent://url"})
            assert res.status_code == 404

            # ----- POST /cloudmap end-to-end -----
            cloudmap_url = f"http://{HOST}:{port}/cloudmap"
            cloudmap_path = os.path.abspath("cloudmap.yaml")

            # 1. Upsert: rewrite an existing repository entry. The
            #    fixture's `repository` schema requires `path`, so
            #    include it alongside `name`.
            existing_key = "git://unfurl.cloud/onecommons/std.git"
            res = requests.post(
                cloudmap_url,
                json={
                    "repositories": {
                        existing_key: {
                            "path": "onecommons/std",
                            "name": "renamed-via-post",
                        }
                    }
                },
            )
            assert res.status_code == 200, res.text
            response = res.json()
            assert "commit" in response
            assert isinstance(response["commit"], str)
            assert response["commit"], "commit oid should be non-empty"
            on_disk = Path(cloudmap_path).read_text()
            assert "renamed-via-post" in on_disk

            # 2. Delete via `unfurl.server.deleted: true`.
            import yaml as _yaml

            delete_key = "git://unfurl.cloud/feb20a/dashboard.git"
            res = requests.post(
                cloudmap_url,
                json={
                    "repositories": {
                        delete_key: {"unfurl.server.deleted": True}
                    }
                },
            )
            assert res.status_code == 200, res.text
            assert "commit" in res.json()
            on_disk_doc = _yaml.safe_load(Path(cloudmap_path).read_text())
            assert delete_key not in on_disk_doc.get("repositories", {})

            # 3. Unknown section → 400.
            res = requests.post(cloudmap_url, json={"flarp": {}})
            assert res.status_code == 400

            # 4. Schema-violating record → 422. The repository schema
            #    requires `protocols` to be an array of strings.
            res = requests.post(
                cloudmap_url,
                json={
                    "repositories": {
                        existing_key: {
                            "path": "onecommons/std",
                            "protocols": "not-an-array",
                        }
                    }
                },
            )
            assert res.status_code == 422, (
                f"expected 422 schema violation, got {res.status_code}: {res.text}"
            )
            # APIFlask wraps Pydantic validation errors under
            # `detail.json._schema`; the message includes
            # 'cloudmap schema violation' from our model_validator.
            assert "cloudmap schema violation" in res.text
        finally:
            _terminate_process(p)


@pytest.mark.parametrize("server_env", server_env)
def test_cloudmap_proxy_round_trip(server_env):
    """End-to-end CloudMapProxy round-trip against every server variant.

    Runs against the four server flavours from :data:`server_env`:

    - ``no-redis`` / ``redis``: pure Python server. The /cloudmap
      handler reads / writes ``cloudmap.yaml`` directly. No
      ``unfurl.server.{id,version,commit}`` annotations on records;
      ``since_version`` / ``exclude`` are accepted but ignored. Each
      successful POST produces a real git commit oid in the response.
    - ``redis-rust``: rust proxy in front of Python, with the rust
      cloudmap fast-path enabled (``UNFURL_CLOUDMAP_REPO`` +
      ``UNFURL_CLOUDMAP_DB_URL``). Records carry the OCC + id
      annotations; POST stages an in-flight write (commit=None,
      queueid bumped).
    """
    from pathlib import Path
    from unfurl.cloudmap.proxy import CloudMapProxy
    from unfurl.tosca_plugins.cloudmap_defs import (
        Artifact,
        ArtifactMetadata,
    )

    if server_env == "queue-rust":
        pytest.skip(
            "cloudmap endpoints on a server using git-sync doesn't queue writes, so this test is redundant."
        )

    is_rust = "rust" in server_env

    fixture_dir = Path(__file__).parent / "fixtures"
    cloudmap_content = (fixture_dir / "expected_cloudmap.yaml").read_text()

    runner = CliRunner()
    port = _next_port()
    with runner.isolated_filesystem() as tmpdir:
        # cloudmap repo: a real git worktree with cloudmap.yaml.
        cloudmap_path = os.path.abspath("cloudmap.yaml")
        with open(cloudmap_path, "w") as f:
            f.write(cloudmap_content)
        repo = GitRepo(Repo.init("."))
        repo.add_all(os.path.abspath("."))
        repo.commit_files([cloudmap_path], "Add cloudmap")

        extra_env = _env_for(server_env, "cloudmap-proxy")
        if is_rust:
            # sqlite db file for the rust SyncedRepo backend.
            # `?mode=rwc` tells sqlx to create the file if it
            # doesn't exist.
            sqlite_path = os.path.abspath("cloudmap-sync.sqlite")
            extra_env["UNFURL_CLOUDMAP_REPO"] = os.path.abspath(".")
            extra_env["UNFURL_CLOUDMAP_DB_URL"] = (
                f"sqlite://{sqlite_path}?mode=rwc"
            )

        ctx = get_context()
        error_queue = ctx.Queue()
        p = ctx.Process(
            target=serve_server,
            args=(HOST, port, None, ".", f"{tmpdir}", {"home": ""}),
            kwargs={
                "error_queue": error_queue,
                "extra_env": extra_env,
            },
        )
        p._error_queue = error_queue
        try:
            start_server_process(p, port, is_rust=is_rust)

            base_url = f"http://{HOST}:{port}"
            proxy = CloudMapProxy(base_url)

            # find_repositories triggers a per-section fetch and returns
            # an iterator (not a list) — paging-ready.
            repos_iter = proxy.find_repositories()
            assert iter(repos_iter) is repos_iter
            repos = list(repos_iter)
            assert repos, "expected fixture cloudmap to have repositories"

            # OCC tokens are only stamped on the rust path; the
            # Python YAML fallback returns records without them.
            if is_rust:
                assert proxy._cache._max_version > 0
            else:
                assert proxy._cache._max_version == 0

            # Second find_repositories call is local-only (no HTTP).
            list(proxy.find_repositories())
            assert "repositories" in proxy._cache._section_loaded

            # find_artifacts is a separate fetch.
            artifacts = list(proxy.find_artifacts())
            assert artifacts, "expected fixture cloudmap to have artifacts"

            # get_artifact for one we already have is a cache hit.
            first_url = artifacts[0].url
            assert proxy.get_artifact(first_url) is artifacts[0]

            # Stage a new artifact and save().
            initial_max = proxy._cache._max_version
            new = Artifact(
                url="pkg:oci/proxy-test/new@1.0",
                metadata=ArtifactMetadata(title="proxy-injected"),
            )
            proxy.add_record(new)
            commit_oid = proxy.save()

            if is_rust:
                # Rust local handler stages to its in-flight db;
                # commit is null but the response's queueid
                # (== largest unfurl.server.version stamped) is
                # folded into the cache's _max_version.
                assert commit_oid is None
                assert proxy._cache._max_version > initial_max
                cached_record = proxy.get_artifact(new.url)
                assert cached_record is not None
                assert (
                    cached_record._unfurl_server_version
                    == proxy._cache._max_version
                )
                # In-flight: no commit yet.
                assert cached_record._unfurl_server_commit is None
            else:
                # Python handler commits to git and returns the oid.
                assert isinstance(commit_oid, str) and commit_oid
                # _max_version doesn't advance — Python doesn't stamp
                # version tokens on records.
                assert proxy._cache._max_version == 0

            # Refresh: server returns the latest. With no since_version
            # filter (Python ignores it; rust's since=0 returns
            # everything > 0), at minimum the cache stays consistent.
            after_save_max = proxy._cache._max_version
            proxy.refresh()
            assert proxy._cache._max_version >= after_save_max

            # The new artifact should now be retrievable from a fresh
            # proxy instance — tests the server-side persistence.
            proxy2 = CloudMapProxy(base_url)
            fetched = proxy2.get_artifact(new.url)
            assert fetched is not None
            assert fetched.url == new.url
        finally:
            _terminate_process(p)


class TestDoPatch:
    """Unit tests for serve._do_patch.

    Mirrors the Rust port in rust/server/src/patch.rs so behavior stays in
    sync. ``target`` is a 2-level dict ``{__typename: {name: GraphqlObject}}``;
    see the docstring on _do_patch for the patch entry schema.
    """

    @staticmethod
    def _apply(patches, target):
        # _do_patch mutates target in place; return it for assertion convenience.
        server_endpoints._do_patch(patches, target)
        return target

    def test_insert_into_typename_bucket(self):
        result = self._apply(
            [{"__typename": "ResourceTemplate", "name": "db", "type": "Database"}],
            {},
        )
        assert result == {
            "ResourceTemplate": {
                "db": {
                    "__typename": "ResourceTemplate",
                    "name": "db",
                    "type": "Database",
                }
            }
        }

    def test_insert_into_existing_bucket(self):
        result = self._apply(
            [{"__typename": "T", "name": "b", "v": 2}],
            {"T": {"a": {"name": "a", "v": 1}}},
        )
        assert result["T"] == {
            "a": {"name": "a", "v": 1},
            "b": {"__typename": "T", "name": "b", "v": 2},
        }

    def test_delete_named_entry(self):
        result = self._apply(
            [{"__typename": "T", "__deleted": "a", "name": "a"}],
            {"T": {"a": {"v": 1}, "b": {"v": 2}}},
        )
        assert result == {"T": {"b": {"v": 2}}}

    def test_delete_uses_deleted_field_when_name_absent(self):
        result = self._apply(
            [{"__typename": "T", "__deleted": "a"}],
            {"T": {"a": {"v": 1}, "b": {"v": 2}}},
        )
        assert result == {"T": {"b": {"v": 2}}}

    def test_delete_wildcard_clears_typename_bucket(self):
        result = self._apply(
            [{"__typename": "T", "__deleted": "*"}],
            {"T": {"a": {"v": 1}}, "U": {"x": 1}},
        )
        assert result == {"U": {"x": 1}}

    def test_delete_missing_name_is_a_noop(self):
        result = self._apply(
            [{"__typename": "T", "__deleted": "ghost"}],
            {"T": {"a": {"v": 1}}},
        )
        assert result == {"T": {"a": {"v": 1}}}

    def test_insert_with_name_wildcard_is_skipped(self):
        # `name == "*"` is only valid in delete entries.
        result = self._apply(
            [{"__typename": "T", "name": "*", "v": 1}],
            {"T": {"a": {"v": 1}}},
        )
        assert result == {"T": {"a": {"v": 1}}}

    def test_malformed_entry_missing_typename_is_skipped(self):
        result = self._apply(
            [{"name": "a", "v": 1}],
            {"T": {"a": {"v": 1}}},
        )
        assert result == {"T": {"a": {"v": 1}}}

    def test_malformed_entry_missing_name_is_skipped(self):
        result = self._apply(
            [{"__typename": "T", "v": 1}],
            {"T": {"a": {"v": 1}}},
        )
        assert result == {"T": {"a": {"v": 1}}}

    def test_multiple_patches_applied_in_order(self):
        result = self._apply(
            [
                {"__typename": "T", "name": "a", "v": 1},
                {"__typename": "T", "name": "b", "v": 2},
                {"__typename": "T", "__deleted": "a"},
                {"__typename": "U", "name": "x", "v": 99},
            ],
            {},
        )
        assert result == {
            "T": {"b": {"__typename": "T", "name": "b", "v": 2}},
            "U": {"x": {"__typename": "U", "name": "x", "v": 99}},
        }


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
