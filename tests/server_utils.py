"""Helpers and fixtures shared by the tests of `unfurl serve` (tests/test_server*.py).

Importing this module also sets up the environment those tests run servers in.
"""

import datetime
import glob
import html as html_module
import json
import os
from pprint import pformat
import re
import socket
import threading
import time
import traceback
import unittest
import unittest.mock
import urllib.request
from functools import partial
from multiprocessing import Process, set_start_method, get_context, Queue

# Every process below that runs `serve()` uses get_context("spawn"), not the
# platform default. Under fork (the default on Linux) the child inherits this
# process's module-level Flask `app`; if anything in this xdist worker has
# already driven a request through it -- test_cloudmap.py's app.test_client()
# calls, say -- `configure_app`'s `CORS(app, ...)` trips flask's "The setup
# method 'after_request' can no longer be called on the application"
# assertion, the server exits before it binds, and the test fails with
# "server process exited prematurely". Spawn re-imports serve.py, so the
# child always gets a clean app.
from typing import List, Optional

import requests
from click.testing import CliRunner
from git import Repo
from werkzeug.exceptions import HTTPException
from unfurl.server import endpoints as server_endpoints
from unfurl.server import cloudmap as server_cloudmap
from unfurl.server import serve as server
from unfurl.server import gui
from unfurl.packages import is_semver_compatible_with

import pytest
from tests.utils import init_project, run_cmd
from unfurl.repo import GitRepo, Repo as UnfurlRepo
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
    # the worker too: each server clears its prefix when it starts
    os.environ["CACHE_KEY_PREFIX"] = (
        f"test{os.getenv('PYTEST_XDIST_WORKER', '')}{int(time.time())}::"
    )
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

def _free_port_pair(port: int) -> int:
    """``port``, or the first port after it that's free along with the one
    after it: a server takes both when the rust proxy is in front of it."""

    def free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
            except OSError:
                return False
        return True

    while not (free(port) and free(port + 1)):
        port += 2
    return port


# pytest-xdist workers run the test files in parallel: each gets its own ports
_worker = int(os.getenv("PYTEST_XDIST_WORKER", "gw0")[2:] or 0)
_static_server_port = _free_port_pair(8090 + 200 * _worker)
_server_port = _static_server_port + 1
CLOUD_TEST_SERVER = "https://unfurl.cloud"

# unfurl.cloud sometimes answers a clone with a 5xx: a test failing that way is
# rerun (pytest-rerunfailures), and one failing any other way isn't
RERUN_ON_SERVER_ERROR = pytest.mark.flaky(
    reruns=2, reruns_delay=5, only_rerun=[r"returned error: 50[234]"]
)


def _terminate_process(p: Process, timeout: float = 10.0) -> None:
    """Terminate a process and wait for it to exit, forcibly killing if needed."""
    p.terminate()
    p.join(timeout=timeout)
    if p.is_alive():
        p.kill()
        p.join(timeout=5.0)


#  Increment port just in case server ports aren't closed in time for next test
#  NB: if server processes aren't terminated: pkill -fl spawn_main
def _canonical(value):
    """Sort every list in a nested structure, for backend-independent compares.

    Postgres stores records as ``jsonb``, which normalises object key order,
    while sqlite's JSONB preserves it. The graph builds its ``rels`` lists by
    iterating those maps, so the list order follows the storage order. Dicts
    compare order-insensitively in python already; this makes the lists do the
    same so a postgres-backed run can be compared against the same fixture.
    """
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted(
            (_canonical(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True)
        )
    return value


def _cloudmap_db_url() -> str:
    """The ``UNFURL_CLOUDMAP_DB_URL`` for the rust cloudmap fast-path.

    ``UNFURL_TEST_PG_URL`` (the same variable the git-sync crate's own tests
    use) points the rust server at postgres, so the fast-path can be exercised
    against either backend; without it, a scratch sqlite file in the test's
    isolated filesystem. ``?mode=rwc`` tells sqlx to create that file if it
    doesn't exist. Must be called with the isolated filesystem as the cwd.
    """
    pg_url = os.getenv("UNFURL_TEST_PG_URL")
    if pg_url:
        return pg_url
    return f"sqlite://{os.path.abspath('cloudmap-sync.sqlite')}?mode=rwc"


def _next_port():
    global _server_port
    # When the Rust proxy is active each server occupies TWO ports (N=Rust front-end,
    # N+1=Python backend).  Always increment by 2 so parametrized redis-rust variants
    # never conflict with the next test's port.
    _server_port = _free_port_pair(_server_port + 2)
    return _server_port


_TIMESTAMP_FLOOR = 1_000_000_000.0  # 2001-09-09, below any plausible `created`


def _entry_ops_without_created(data: bytes) -> Optional[list]:
    """A stored cache entry's opcode stream, with its `created` value masked.

    `CacheValue.created` is wall clock, so two entries holding the same
    document still differ in those 8 bytes. It is the stream's last float --
    `queueid`, an int, is the only field after it -- so masking that one
    argument leaves a representation that changes only when the document does.

    Returns None when the entry can't be read that way: unreadable bytes, or a
    last float that isn't a timestamp (so the field order this relies on has
    changed). Callers then treat the entry as new rather than assume it matches.

    `pickletools.genops` walks the opcodes without running them, so nothing
    here reconstructs the stored value.
    """
    import pickletools

    body = data[1:] if data[:1] == b"!" else data  # redis serializer prefix
    try:
        ops = [(op.name, arg) for op, arg, _pos in pickletools.genops(body)]
    except Exception:
        return None
    floats = [i for i, (_name, arg) in enumerate(ops) if isinstance(arg, float)]
    if not floats:
        return None
    created_at = floats[-1]
    if ops[created_at][1] < _TIMESTAMP_FLOOR:
        return None
    ops[created_at] = (ops[created_at][0], None)
    return ops


def _fixture_is_current(dest: str, value: bytes) -> bool:
    """Whether `dest` already holds `value`, ignoring its `created` timestamp.

    Every run of a redis-enabled test produces a new `created`, so writing
    unconditionally would leave the fixtures permanently modified in git even
    when the cached documents are identical.
    """
    if not os.path.exists(dest):
        return False
    with open(dest, "rb") as f:
        stored = f.read()
    if stored == value:
        return True
    ops = _entry_ops_without_created(value)
    return ops is not None and ops == _entry_ops_without_created(stored)


def _save_rust_fixtures(cache_prefix: str = "") -> None:
    """Dump the cached deployment and blueprint values from Redis to
    rust/server/tests/fixtures/ so the Rust unit tests can use them.

    Requires UNFURL_TEST_REDIS_URL to be set.  Silently returns if Redis
    is not configured or the expected keys are not found. A fixture that
    already matches what Redis holds is left alone -- see
    `_fixture_is_current`.
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
                    if _fixture_is_current(dest, value):
                        print(f"_save_rust_fixtures: {key_str} unchanged, kept {dest}")
                        continue
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

    `args` is passed through positionally to `unfurl.server.serve.serve()`:
    (host, port, secret, clone_root, project_path, options, cloud_server, gui).
    Most callers below stop at `options`; those that pass a 7th set the
    server's UNFURL_CLOUD_SERVER.

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


QUEUE_SLEEP = 2.0  # seconds to wait for batch queue drain + backend processing.
# Budget: 1s batch window + ~100ms worker pickup (WORKER_POLL_INTERVAL)
# + ~500ms batch_patch processing + ~50ms file:// push ≈ 1.65s; 350ms slack.
# Many call sites would be more precise as `_wait_for_new_commit` (active
# polling on the bare repo HEAD) — see comments at each call site.


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


def _post_write(url, json_body, server_env, queueid=0, branch="main"):
    """POST a write request, adding queueid for queue-rust variant.

    `queueid` is sent as an integer to match the `i64` declared in the
    OpenAPI schema (PatchEnsembleBody / PatchEnvironmentBody).
    """
    if "branch" not in json_body:
        json_body = {**json_body, "branch": branch}
    if server_env == "queue-rust":
        json_body = {**json_body, "queueid": int(queueid)}
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
        ctx = get_context("spawn")
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
    ctx = get_context("spawn")
    error_queue = ctx.Queue()
    p = ctx.Process(
        target=serve_server,
        args=(
            HOST,  # host
            port,  # port
            None,  # secret
            "server",  # clone_root
            ".",  # project_path
            {"home": ""},  # options
            # cloud_server: a local path, not a url. serve() skips the hostname
            # check for a value starting with "/" and stores it as
            # UNFURL_CLOUD_SERVER, so get_project_url() builds every repository
            # url by joining onto it -- "/tmp/.../remote.git" rather than
            # "https://unfurl.cloud/...". Anything that treats a repository url
            # as a url-with-a-scheme behaves differently here than in
            # production because of this.
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


def _start_gui_server(project_dir, name=""):
    """Start a server in --gui mode against a local project at `project_dir`.

    Returns (process, port). The caller is responsible for terminating.
    """
    port = _next_port()
    py_log_fd, py_log_file = tempfile.mkstemp(
        prefix=f"py-gui-{name or 'server'}-", suffix=".log"
    )
    os.close(py_log_fd)
    ctx = get_context("spawn")
    error_queue = ctx.Queue()
    p = ctx.Process(
        target=serve_server,
        args=(HOST, port, None, ".", project_dir, {"home": ""}, None),
        kwargs={
            "error_queue": error_queue,
            # Skip the rust proxy: /branches is a Python-only endpoint and
            # rust adds complexity (extra port, image dependency) without
            # exercising the code we want to test.
            "extra_env": {"UNFURL_RUST_SERVER": "0", "UNFURL_LOGGING": "debug"},
            "py_log_file": py_log_file,
            "gui": True,
        },
    )
    p._error_queue = error_queue
    p._py_log_file = py_log_file
    p._rust_log_file = None
    try:
        start_server_process(p, port)
        return p, port
    except Exception:
        _terminate_process(p)
        raise


def _missing_auth_project_client(monkeypatch, serve_path=None):
    """A test client for a server that isn't serving a local path.

    That's how the server runs when hosted (no ``UNFURL_SERVE_PATH``), and it's the
    case where a request without ``auth_project`` has no project to resolve to.
    """
    if serve_path is None:
        monkeypatch.delenv("UNFURL_SERVE_PATH", raising=False)
    else:
        monkeypatch.setenv("UNFURL_SERVE_PATH", serve_path)
    monkeypatch.setitem(server.app.config, "UNFURL_CLOUD_SERVER", "https://unfurl.cloud")
    return server.app.test_client()
