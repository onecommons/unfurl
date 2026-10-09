import json
import os
import pytest
import requests
import unittest.mock
from click.testing import CliRunner
from git import Repo
from unfurl.repo import GitRepo
from unfurl.yamlloader import yaml
from tests.server_utils import (
    RERUN_ON_SERVER_ERROR,
    HOST,
    _assert_commit,
    _dump_server_logs,
    _get_latest_commit,
    _post_write,
    _terminate_process,
    _wait_for_new_commit,
    _wait_for_queue,
    commit_foo,
    delete_patch,
    deployment,
    patch,
    runner,
    server_env,
    set_up_deployment,
)

pytestmark = RERUN_ON_SERVER_ERROR


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
                # Don't pre-wait for the worker — let the rust proxy resolve
                # the queue itself by passing the queueid + the pre-commit
                # latest_commit.  This exercises resolve_queued_request's
                # check → kick_worker → poll-loop → UseNewCommit path, which
                # is otherwise dead code in the test suite (every other test
                # either drops the queueid or waits the queue out first).
                res = requests.get(
                    f"http://{HOST}:{port}/export",
                    params={
                        "auth_project": "remote",
                        # The queue key is per branch, so a queueid
                        # without one cannot be resolved and is refused.
                        "branch": "main",
                        "latest_commit": last_commit,  # old commit
                        "queueid": queueid,
                        "format": "deployment",
                    },
                    timeout=15,
                )
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

            # An update_ensemble with a stale/wrong latest_commit must be rejected
            # synchronously with 409 CONFLICT:
            #   * no-redis / redis / redis-rust → Python's localenv check fails
            #     (repo.revision != latest_commit).
            #   * queue-rust → inc_queueid sees a missing queue key for the bogus
            #     commit with queueid > 0 and returns "error" from the Rust proxy.
            stale_commit = "0" * 40
            stale_res = _post_write(
                f"http://{HOST}:{port}/update_ensemble?auth_project=remote",
                {
                    "patch": json.loads(target_patch),
                    "latest_commit": stale_commit,
                },
                server_env,
                queueid=queueid,
            )
            assert stale_res.status_code == 409, (
                f"expected 409 CONFLICT for stale latest_commit "
                f"(server_env={server_env}, queueid={queueid}); "
                f"got {stale_res.status_code}: {stale_res.text}"
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

            # We're in the local "remote" clone here; the bare repo we want
            # to poll is at "../remote.git" relative to cwd.
            if server_env == "queue-rust":
                last_commit = _wait_for_new_commit("../remote.git", last_commit)
            else:
                _wait_for_queue(server_env)

            assert not os.waitstatus_to_exitcode(
                os.system("git pull --commit --no-edit origin main")
            )
            with open("unfurl.yaml", "r") as f:
                contents = f.read()
                data = yaml.load(contents)
                # check that the environment was added and an ensemble was created
                assert data.get("environments") is not None, (
                    f"missing 'environments' in unfurl.yaml ({server_env}); "
                    f"file contents:\n{contents}"
                )
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
            # Two keys are cleared: 'remote:pull:server/public/remote/main'
            # and 'remote:main:ensemble/ensemble.yaml:localenv'.
            assert res.status_code == 200, res.content
            assert res.json()["cleared"] == 2, res.content

        finally:
            if p:
                _terminate_process(p)


def _read_sse(url, params, timeout=25):
    """Read `data:` frames from an SSE response until its terminal one.

    Returns the decoded payloads in order. Stops at `status: done` rather
    than at EOF so a server that forgot to close cannot hang the test.
    """
    frames = []
    with requests.get(url, params=params, stream=True, timeout=timeout) as res:
        assert res.status_code == 200, res.text
        for line in res.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            frames.append(json.loads(line[len("data:") :].strip()))
            if frames[-1].get("status") == "done":
                break
    return frames


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
def test_events_reports_a_real_queued_write():
    """`/events` reports a queued write settling, end to end.

    The whole chain rather than a planted Redis key: a real write is
    queued by the rust proxy, drained by its batch worker, applied and
    committed by Python, and the commit recorded by ``_update_queue_key``
    -- and the event's ``new_commit`` is checked against what the bare
    repo actually ends up at.

    Deliberately never calls ``/export``. The old path already notifies a
    client by blocking its read, so a test that exported first would pass
    whether or not the subscription did anything.

    Also the cross-language check on the queue key's shape: the watch
    names a branch, so an event only arrives if the key rust builds is
    the key ``_update_queue_key`` wrote. The two are constructed in
    different languages from the same parts, and nothing else would
    notice them drifting.
    """
    if os.getenv("UNFURL_TEST_RUST_SERVER") == "0":
        pytest.skip("/events is served by the rust proxy, which is disabled")
    server_env = "queue-rust"
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            p, port, last_commit = set_up_deployment(
                runner,
                deployment.format("initial"),
                server_env=server_env,
                name="events",
            )

            res = _post_write(
                f"http://{HOST}:{port}/update_ensemble?auth_project=remote",
                {
                    "patch": json.loads(patch.format("target")),
                    "latest_commit": last_commit,
                },
                server_env,
                queueid=0,
            )
            _, queueid = _assert_commit(res, last_commit, server_env)
            assert queueid, f"expected a queueid from the queued write: {res.json()}"

            frames = _read_sse(
                f"http://{HOST}:{port}/events",
                params={
                    "auth_project": "remote",
                    # Repeated param, `{branch}:{latest_commit}:{queueid}`.
                    "watch": [f"main:{last_commit}:{queueid}"],
                },
            )

            settled = [f for f in frames if f.get("status") == "ok"]
            assert len(settled) == 1, frames
            event = settled[0]
            assert event["branch"] == "main", event
            assert event["latest_commit"] == last_commit, event
            assert event["queueid"] == queueid, event
            assert event["new_commit"] != last_commit, event
            # The commit the client is sent to is the one the write
            # actually produced, not merely a plausible-looking value.
            assert event["new_commit"] == _get_latest_commit(), (
                event,
                _get_latest_commit(),
            )
            assert frames[-1] == {"status": "done"}, frames

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
            # cwd is still the test sandbox root → bare repo is "remote.git".
            if server_env == "queue-rust":
                last_commit = _wait_for_new_commit("remote.git", last_commit)
            else:
                _wait_for_queue(server_env)

            os.chdir("remote")
            os.system("git pull ../remote.git")
            # For queue-rust the synchronous response carries only a
            # queueid, not the eventual commit hash, so look it up from
            # the local checkout after the queue has drained.  Other
            # variants already get a real commit from the response.
            if not new_commit:
                new_commit = GitRepo(Repo(".")).revision
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
                json={
                    "patch": bad_patch,
                    "latest_commit": new_commit,
                    "branch": "main",
                },
            )
            assert res.status_code == 400
            assert res.json()["code"] == "BAD_REQUEST"
            assert "reserved" in res.json()["message"]

            # Error: a write that names no branch, rejected rather than applied
            # to `main`. Sent through `_post_write` so the queue-rust variant
            # carries a queueid and takes the rust proxy's queue path: that is
            # the case the proxy has to reject itself, because a queued write is
            # answered before python ever sees it, so a later rejection would
            # reach nobody. (Without a queueid the write is proxied and python
            # answers.) Both servers report errors as `ErrorResponse`, so the
            # same assertions hold whichever one answered.
            res = _post_write(
                f"http://{HOST}:{port}/update_environment?auth_project=remote",
                {"patch": [], "latest_commit": new_commit, "branch": ""},
                server_env,
            )
            assert res.status_code == 400, res.text
            assert res.json()["code"] == "BAD_REQUEST", res.text
            assert "branch" in res.json()["message"], res.text
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
            # The rust-log assertion below needs the batch to have been
            # forwarded to Python.  Active-poll on the bare repo so this
            # finishes as soon as `_push_changes` commits.
            if server_env == "queue-rust":
                last_commit = _wait_for_new_commit("remote.git", last_commit)
            else:
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
            # Active-poll for the new commit instead of a flat sleep.
            if server_env == "queue-rust":
                last_commit = _wait_for_new_commit("remote.git", last_commit)
            else:
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
            # cwd is the sandbox root → bare repo is "remote.git".
            if server_env == "queue-rust":
                last_commit = _wait_for_new_commit("remote.git", last_commit)
            else:
                _wait_for_queue(server_env)

            os.chdir("remote")
            os.system("git pull ../remote.git")
            assert os.path.exists("deployments/new-app/ensemble.yaml"), os.listdir(".")
        finally:
            _dump_server_logs(p, "create-ensemble")
            if p:
                _terminate_process(p)
