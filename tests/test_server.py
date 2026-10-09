import datetime
import html as html_module
import json
import os
import pytest
import re
import requests
import tempfile
import time
import unittest.mock
from click.testing import CliRunner
from git import Repo
from multiprocessing import Process, get_context
from tests.utils import init_project
from unfurl.packages import is_semver_compatible_with
from unfurl.repo import GitRepo
from unfurl.server import gui, serve as server
from werkzeug.exceptions import HTTPException
from tests.server_utils import (
    RERUN_ON_SERVER_ERROR,
    _missing_auth_project_client,
    CLOUD_TEST_SERVER,
    HOST,
    _dump_server_logs,
    _next_port,
    _rust_extra_env,
    _start_gui_server,
    _static_server_port,
    _terminate_process,
    deployment,
    patch,
    runner,
    serve_server,
    set_up_deployment,
    start_server_process,
)

pytestmark = RERUN_ON_SERVER_ERROR


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


def test_cors_preflight(runner: Process):
    """A browser preflight is answered by whichever server is in front.

    ``/export`` is registered GET-only on the rust server, so before the
    cors layer this got a 405 from the method router; python's auth hook
    answered it with a 401. The allowed origin is never in the
    environment: python derives it from UNFURL_CLOUD_SERVER and hands it
    to the rust process it spawns.
    """
    # Deliberately no secret: a preflight carries no credentials, and both
    # servers must answer it anyway or the browser never sends the
    # authenticated request it precedes.
    url = f"http://{HOST}:{_static_server_port}/export"
    res = requests.options(
        url,
        headers={
            "Origin": CLOUD_TEST_SERVER,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert res.status_code < 400, f"{res.status_code}: {res.text}"
    # Exact equality also rules out a duplicate header: requests joins
    # repeats with ", ", and two Allow-Origins make a browser reject the
    # response. The proxied python response already carries one.
    assert res.headers.get("Access-Control-Allow-Origin") == CLOUD_TEST_SERVER

    other = requests.options(
        url,
        headers={
            "Origin": "https://not-allowed.test",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "Access-Control-Allow-Origin" not in other.headers

    # The preflight exemption must not be a way past the secret: the
    # request the browser sends next still has to carry it.
    assert requests.get(url, headers={"Origin": CLOUD_TEST_SERVER}).status_code == 401


    # A bare OPTIONS carrying no Access-Control-Request-Method is not
    # asserted: python authenticates it, while tower-http answers every
    # OPTIONS as a preflight. Neither returns data, so the divergence
    # doesn't matter -- but don't tighten this into an equality.


def test_gui_release():
    assert re.match(gui.release_url_pattern, gui.RELEASE_URL).group(1) == gui.TAG
    assert is_semver_compatible_with(gui.TAG, "v0.1.0-alpha.1")


def _parse_cloudmap_attr(html: str) -> str:
    """Return div#chart's data-cloudmap the way a browser's dataset would."""
    match = re.search(r'<div id="chart"[^>]*\bdata-cloudmap="([^"]*)"', html)
    assert match, f"no data-cloudmap on div#chart in: {html!r}"
    return html_module.unescape(match.group(1))


def test_gui_cloud_page_cloudmap_url():
    """The /cloud page is rewritten to fetch types from this server.

    The page bundle reads ``data-cloudmap`` off ``div#chart`` while it
    initializes, so the attribute has to be in the markup the server sends --
    nothing added to the document afterwards would be early enough.
    """
    # the source page (pretty printed) and the minified build spell the div
    # identically; both have to keep working.
    for markup in (
        '<div id="chart">\n      <div id="map-controls"></div>\n    </div>',
        '<div id="chart"><div id="map-controls"></div></div>',
    ):
        rendered = gui.render_cloud_page(markup)
        assert _parse_cloudmap_attr(rendered) == gui.CLOUDMAP_URL
        # nothing but the attribute changed
        assert re.sub(r' data-cloudmap="[^"]*"', "", rendered) == markup

    # unrecognized markup is passed through rather than silently mangled
    assert gui.render_cloud_page("<div id=chart>") == "<div id=chart>"

    with unittest.mock.patch.dict(
        os.environ, {"UNFURL_GUI_CLOUDMAP_URL": "/cloudmap/facets?x=1&y=2"}
    ):
        rendered = gui.render_cloud_page('<div id="chart"></div>')
    assert _parse_cloudmap_attr(rendered) == "/cloudmap/facets?x=1&y=2"


@pytest.mark.skipif(
    not os.getenv("UNFURL_GUI_DIR"), reason="requires a local unfurl-gui checkout"
)
def test_gui_cloud_page_matches_unfurl_gui():
    """The div#chart marker is a contract with unfurl-gui, not a local constant.

    render_cloud_page keys off markup that lives in the other repo, so a
    hand-written fixture here would only ever agree with itself. Check the real
    page instead when a checkout is available.
    """
    ufgui_dir = os.getenv("UNFURL_GUI_DIR", "")
    found = False
    for subdir in ("public", "dist"):
        path = os.path.join(ufgui_dir, subdir, gui.CLOUD_PAGE)
        if not os.path.isfile(path):
            continue
        found = True
        with open(path) as f:
            rendered = gui.render_cloud_page(f.read())
        assert _parse_cloudmap_attr(rendered) == gui.CLOUDMAP_URL, subdir
    assert found, f"no {gui.CLOUD_PAGE} under {ufgui_dir}"


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


def _branches_entry(port: int, project_root: str) -> dict:
    """Fetch the single /branches entry for `project_root` (an absolute path).

    Asserts its commit carries `id`, `committed_date` and `created_at`
    (gui.py:branches sets all three; both date fields derive from the same
    `repo.revision_time`, so they must match). Returns the whole entry, so a
    caller can also look at `name` and `default`.
    """
    # The Flask route uses <path:project_path> so the colon in `local:` must
    # be URL-encoded; the absolute path's slashes can pass through as-is.
    url = (
        f"http://{HOST}:{port}/api/v4/projects/local%3A{project_root}"
        f"/repository/branches"
    )
    res = requests.get(url)
    assert res.status_code == 200, res.text
    branches = res.json()
    assert branches, f"empty /branches response: {res.text}"
    entry = branches[0]
    commit = entry["commit"]
    assert commit.get("id"), f"missing commit.id: {commit}"
    assert commit.get("committed_date"), f"missing committed_date: {commit}"
    assert commit.get("created_at"), f"missing created_at: {commit}"
    assert commit["committed_date"] == commit["created_at"], (
        f"committed_date {commit['committed_date']!r} != created_at "
        f"{commit['created_at']!r}"
    )
    # ISO 8601: parseable by datetime.fromisoformat (3.7+).
    datetime.datetime.fromisoformat(commit["committed_date"])
    return entry


def _branches_commit(port: int, project_root: str) -> dict:
    """The /branches commit dict for `project_root` (an absolute path)."""
    return _branches_entry(port, project_root)["commit"]


@pytest.mark.parametrize("create_endpoint", ["create_ensemble", "create_provider"])
def test_gui_branches_dirty_subrepo(create_endpoint):
    """/branches reports `<sha>-dirty` when a sub-ensemble repo is dirty.

    When /create_ensemble (or /create_provider) can create a new ensemble
    in a separate repository, so /branches on the outer project must reflect any uncommitted
    state in that repo because the client only sends commits from the outer project repository.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            init_project(
                runner,
                # --use-environment seeds the `environments:` section so
                # /create_provider can register a deployment under it without
                # crashing on a None environments dict.
                args=["init", "--empty", "--use-environment", "test-env", "ufsv"],
                env=dict(UNFURL_HOME=""),
            )
            project_root = os.path.abspath("ufsv")
            outer_repo = GitRepo(Repo(project_root))
            last_commit = outer_repo.revision
            assert last_commit
            # `unfurl init` makes a repo with no remote, which records no
            # default branch, so give it the origin/HEAD a clone would have:
            # /branches reports `default` from it. The remote is never
            # contacted -- gui mode doesn't push (`_commit_and_push`).
            outer_repo.repo.git.remote("add", "origin", "https://example.com/ufsv.git")
            outer_repo.repo.git.symbolic_ref(
                "refs/remotes/origin/HEAD",
                f"refs/remotes/origin/{outer_repo.active_branch}",
            )

            p, port = _start_gui_server(project_root, name=create_endpoint)

            # Sanity: initial /branches returns the clean SHA.
            initial_entry = _branches_entry(port, project_root)
            initial = initial_entry["commit"]
            assert initial["id"] == last_commit, (
                f"expected {last_commit}, got {initial['id']}"
            )
            # the branch it reports is the one origin/HEAD points at
            assert initial_entry["default"] is True, initial_entry

            # POST to the create endpoint to spawn a new sub-ensemble subrepo.
            deployment_path = (
                "deployments/new-app"
                if create_endpoint == "create_ensemble"
                else "environments/test-env/primary_provider"
            )
            body = {
                "patch": [],
                "deployment_path": deployment_path,
                "latest_commit": last_commit,
                # a write has to say which branch it applies to
                "branch": outer_repo.active_branch,
            }
            if create_endpoint == "create_provider":
                body["environment"] = "test-env"
            res = requests.post(
                f"http://{HOST}:{port}/{create_endpoint}"
                f"?auth_project=local:{project_root}",
                json=body,
            )
            assert res.status_code == 200, res.text

            # New subrepo was just committed by the endpoint, so /branches
            # should still report a clean SHA — and a different one than
            # before, since /create_ensemble registered the new ensemble in
            # unfurl.yaml (an outer-repo change that produced a new commit).
            after_create = _branches_commit(port, project_root)
            assert not after_create["id"].endswith("-dirty"), (
                f"unexpected dirty after clean create: {after_create['id']}"
            )
            assert after_create["id"] != initial["id"], (
                f"expected /{create_endpoint} to advance HEAD, "
                f"but /branches still reports {after_create['id']}"
            )
            # The reported date must belong to the commit /branches reports,
            # not a stale one cached from before the create. Compared against
            # git rather than against `initial`: commit timestamps have
            # one-second resolution, so when both commits land in the same
            # second their dates are legitimately equal even though HEAD moved.
            head = GitRepo(Repo(project_root)).repo.head.commit
            expected_date = datetime.datetime.fromtimestamp(
                head.committed_date, tz=datetime.timezone.utc
            )
            assert (
                datetime.datetime.fromisoformat(after_create["committed_date"])
                == expected_date
            ), (
                f"/branches reported committed_date "
                f"{after_create['committed_date']!r} for {after_create['id']}, "
                f"whose actual date is {expected_date.isoformat()}"
            )

            # Modify the new ensemble's ensemble.yaml without committing.
            sub_ensemble = os.path.join(project_root, deployment_path, "ensemble.yaml")
            assert os.path.exists(sub_ensemble), (
                f"missing sub-ensemble file: {sub_ensemble}"
            )
            with open(sub_ensemble, "a") as f:
                f.write("\n# dirty marker added by test_gui_branches_dirty_subrepo\n")

            # /branches must now advertise the dirty state. The -dirty suffix
            # is appended to the same SHA we saw post-create — the outer repo
            # didn't get a new commit; only the subrepo's working tree changed.
            after_dirty = _branches_commit(port, project_root)
            assert after_dirty["id"] == after_create["id"] + "-dirty", (
                f"expected {after_create['id']}-dirty, got: {after_dirty['id']}"
            )
            assert after_dirty["committed_date"] == after_create["committed_date"], (
                f"committed_date shouldn't change when only the working "
                f"tree changed: {after_dirty['committed_date']} vs "
                f"{after_create['committed_date']}"
            )
        finally:
            _dump_server_logs(p, f"gui-branches-{create_endpoint}")
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
        # Force-enable hyper debug logging.  `_start_proxy_server`'s
        # default RUST_LOG for debug-level UNFURL_LOGGING suppresses
        # hyper/reqwest/tower_http to keep ordinary logs readable, but
        # this test asserts on a hyper_util pool message to verify the
        # proxy actually round-tripped the request.
        "RUST_LOG": "debug",
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


def test_errors_report_code_and_message(monkeypatch):
    """Every error is an `ErrorResponse`, including the ones APIFlask raises.

    A validation failure and an unrouted request are answered by APIFlask, not
    by a handler, so they used to arrive in its own `detail`/`message` shape --
    a second spelling next to `create_error_response`'s `code`/`message` (and
    the rust proxy's `error`/`message`, a third), with the spec documenting only
    APIFlask's. `error_response` converts them and
    `HTTP_ERROR_SCHEMA`/`VALIDATION_ERROR_SCHEMA` make the spec say so.
    """
    client = _missing_auth_project_client(monkeypatch)

    # raised by a handler
    res = client.get("/export?format=environments")
    assert res.status_code == 400
    assert res.json == {
        "code": "BAD_REQUEST",
        "message": "Missing required query parameter 'auth_project'",
    }

    # raised by APIFlask: a body that fails validation. The per-field errors
    # travel in `fields`, which is where the schema says to look for them.
    res = client.post(
        "/update_ensemble?auth_project=me/proj",
        json={"patch": [], "latest_commit": "abc123"},
    )
    assert res.status_code == 422
    assert res.json["code"] == "VALIDATION_ERROR", res.json
    assert list(res.json["fields"]) == ["json"], res.json
    assert "branch" in res.json["fields"]["json"], res.json

    # raised by flask: no such route. No `fields`, rather than an empty one.
    res = client.get("/no-such-endpoint")
    assert res.status_code == 404
    assert res.json == {"code": "NOT_FOUND", "message": "Not Found"}


def test_get_project_id_does_not_abort(monkeypatch):
    """The plain getter stays a plain getter -- only the `_or_abort` variant rejects."""
    from werkzeug.test import EnvironBuilder

    monkeypatch.delenv("UNFURL_SERVE_PATH", raising=False)
    monkeypatch.setitem(server.app.config, "UNFURL_CLOUD_SERVER", "https://unfurl.cloud")
    request = EnvironBuilder(query_string="").get_request()
    assert server.get_project_id(request) == ""

    with server.app.test_request_context("/export"):
        with pytest.raises(HTTPException) as exc:
            server.get_project_id_or_abort(request)
        assert exc.value.get_response().status_code == 400

    # a server started on a local path is exempt
    monkeypatch.setenv("UNFURL_SERVE_PATH", ".")
    assert server.get_project_id_or_abort(request) == ""


def test_cors_origins_resolved_and_handed_to_rust_server(monkeypatch, tmp_path):
    """The rust server gets the origins flask-cors was actually configured with.

    With UNFURL_SERVE_CORS unset the origin is derived from
    UNFURL_CLOUD_SERVER, so it exists only in app.config -- the inherited
    environment the child would otherwise read has nothing in it.
    """
    from apiflask import APIFlask

    monkeypatch.setenv("UNFURL_CLOUD_SERVER", "https://cloud.example.com/some/path")
    monkeypatch.delenv("UNFURL_SERVE_CORS", raising=False)

    isolated = APIFlask(__name__)
    server.configure_app(isolated)
    assert isolated.config["UNFURL_SERVE_CORS"] == "https://cloud.example.com"

    fake_bin = tmp_path / "unfurl-server"
    fake_bin.write_text("")
    monkeypatch.setattr(server, "_find_rust_server_bin", lambda: str(fake_bin))
    monkeypatch.setitem(
        server.app.config, "UNFURL_SERVE_CORS", isolated.config["UNFURL_SERVE_CORS"]
    )
    captured = {}

    class FakePopen:
        pid = 1234

        def __init__(self, argv, env=None, stderr=None):
            captured["env"] = env

    monkeypatch.setattr(server.subprocess, "Popen", FakePopen)
    server._start_proxy_server("127.0.0.1", 8080)
    assert captured["env"]["UNFURL_SERVE_CORS"] == "https://cloud.example.com"


def test_secret_handed_to_rust_server(monkeypatch, tmp_path):
    """`--secret` sets only the app config, so it's passed on explicitly: the
    rust server checks it on the writes it queues and replays them with it."""
    fake_bin = tmp_path / "unfurl-server"
    fake_bin.write_text("")
    monkeypatch.setattr(server, "_find_rust_server_bin", lambda: str(fake_bin))
    monkeypatch.delenv("UNFURL_SERVE_SECRET", raising=False)
    monkeypatch.setitem(server.app.config, "UNFURL_SECRET", "s3cret")
    captured = {}

    class FakePopen:
        pid = 1234

        def __init__(self, argv, env=None, stderr=None):
            captured["env"] = env

    monkeypatch.setattr(server.subprocess, "Popen", FakePopen)
    server._start_proxy_server("127.0.0.1", 8080)
    assert captured["env"]["UNFURL_SERVE_SECRET"] == "s3cret"


def test_cloud_server_handed_to_rust_server(monkeypatch, tmp_path):
    """`--cloud-server` sets only the app config, so it's passed on explicitly."""
    fake_bin = tmp_path / "unfurl-server"
    fake_bin.write_text("")
    monkeypatch.setattr(server, "_find_rust_server_bin", lambda: str(fake_bin))
    monkeypatch.delenv("UNFURL_CLOUD_SERVER", raising=False)
    monkeypatch.setitem(server.app.config, "UNFURL_CLOUD_SERVER", "http://gdk.test:3000")
    captured = {}

    class FakePopen:
        pid = 1234

        def __init__(self, argv, env=None, stderr=None):
            captured["env"] = env

    monkeypatch.setattr(server.subprocess, "Popen", FakePopen)
    server._start_proxy_server("127.0.0.1", 8080)
    assert captured["env"]["UNFURL_CLOUD_SERVER"] == "http://gdk.test:3000"


def test_cors_explicit_origins_override_cloud_server(monkeypatch):
    """An explicit UNFURL_SERVE_CORS wins over the UNFURL_CLOUD_SERVER default."""
    from apiflask import APIFlask

    monkeypatch.setenv("UNFURL_CLOUD_SERVER", "https://cloud.example.com")
    monkeypatch.setenv("UNFURL_SERVE_CORS", "https://a.test https://b.test")

    isolated = APIFlask(__name__)
    server.configure_app(isolated)
    assert isolated.config["UNFURL_SERVE_CORS"] == "https://a.test https://b.test"


def test_cloud_vars_url_rejected_with_400():
    """A cloud_vars_url this server won't fetch is a bad request.

    The server fetches that url itself and the client puts a private token
    in its query string, so one pointing anywhere else would hand the token
    away. Dropping it instead would give a request that succeeds while the
    environment's variables are silently missing.
    """
    runner = CliRunner()
    p = None
    with runner.isolated_filesystem():
        try:
            p, port, last_commit = set_up_deployment(
                runner, deployment.format("initial"), name="cloud-vars-url"
            )
            url = f"http://{HOST}:{port}/update_ensemble?auth_project=remote"
            body = {
                "patch": json.loads(patch.format("target")),
                "latest_commit": last_commit,
                "branch": "main",
            }

            res = requests.post(
                url,
                json={
                    **body,
                    "cloud_vars_url": (
                        "https://evil.test/api/v4/projects/42/variables"
                        "?private_token=SECRET"
                    ),
                },
            )
            assert res.status_code == 400, f"{res.status_code}: {res.text!r}"
            assert "cloud_vars_url" in res.text
            # the reason travels back; the url (and its token) must not
            assert "SECRET" not in res.text and "evil.test" not in res.text
        finally:
            if p:
                _terminate_process(p)
