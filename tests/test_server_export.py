import json
import os
import pytest
import requests
import tempfile
import unittest.mock
from base64 import b64encode
from click.testing import CliRunner
from git import Repo
from multiprocessing import get_context
from pprint import pformat
from tests.utils import init_project, run_cmd
from unfurl.repo import GitRepo, Repo as UnfurlRepo
from unfurl.server import serve as server
from unfurl.util import clean_output, get_package_digest
from tests.server_utils import (
    RERUN_ON_SERVER_ERROR,
    CLOUD_TEST_SERVER,
    HOST,
    UNFURL_TEST_REDIS_URL,
    _env_for,
    _next_port,
    _poll_rust_log,
    _save_rust_fixtures,
    _terminate_process,
    deployment,
    runner,
    serve_server,
    server_env,
    set_up_deployment,
    start_server_process,
    wait_for_status,
)

pytestmark = RERUN_ON_SERVER_ERROR


@pytest.mark.parametrize("server_env", server_env)
def test_server_export_local(server_env):
    runner = CliRunner()
    port = _next_port()
    with runner.isolated_filesystem() as tmpdir:
        ctx = get_context("spawn")
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
                # The server response carries an extra `latest_commit` echo
                # field (serve.py:1585) that the CLI export doesn't emit;
                # drop it before comparing structurally.
                server_payload = res.json()
                server_payload.pop("latest_commit", None)
                assert server_payload == json.loads(exported.output)

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
        ctx = get_context("spawn")
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
                            # the rust server only builds a cache key for a
                            # request that names its branch -- without one it
                            # proxies rather than guess which branch an entry
                            # belongs to (`export_cache_key` in routes.rs).
                            "branch": "main",
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

                        # `_strip_sourceinfo` returns None, so the comparison
                        # above says nothing about the response keys: assert
                        # the branch it exported at explicitly. `default_branch`
                        # isn't set here -- the request named a branch, so
                        # nothing had to resolve one (see the request below).
                        assert res.json()["branch"] == "main"
                        assert "default_branch" not in res.json()

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
                                "branch": "main",  # see the miss request above
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

            # `application-blueprint/std` is a ProxiedRepo (no .git/)
            # when the proxy serves a semver tag, and a real git
            # checkout otherwise.  `UnfurlRepo.make_repo` returns the
            # right type either way; both expose `.revision`.
            std_repo = UnfurlRepo.make_repo("application-blueprint/std")
            assert std_repo is not None
            dep_commit = std_repo.revision
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

            # A request that names no branch makes the server resolve one, and
            # report both what it picked and the remote's default branch (the
            # `ls-remote` symref). That's the only path that sets
            # `default_branch`, and the rust server can't key a request without
            # a branch, so this response comes from python on every variant.
            # Runs last: it caches under the same key the fixtures came from.
            res = requests.get(
                f"http://{HOST}:{port}/export",
                params={
                    "auth_project": "onecommons/project-templates/dashboard",
                    "format": "environments",
                },
            )
            assert res.status_code == 200, res.text
            resolved = res.json()
            assert resolved["default_branch"] == "main", resolved.get("default_branch")
            assert resolved["branch"] == "main", resolved.get("branch")
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
            ctx = get_context("spawn")
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
