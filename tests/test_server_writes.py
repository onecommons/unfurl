import glob
import os
import pytest
import requests
import unittest.mock
from click.testing import CliRunner
from git import Repo
from unfurl.repo import GitRepo
from unfurl.server import endpoints as server_endpoints, serve as server
from tests.server_utils import (
    _missing_auth_project_client,
    HOST,
    UNFURL_TEST_REDIS_URL,
    _dump_server_logs,
    _post_write,
    _terminate_process,
    _variant_prefix,
    deployment,
    runner,
    set_up_deployment,
)


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


# --- auth_project guard ------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/export?format=environments",
        "/types",
        "/cloudmap",  # POST: the write path still requires a project
    ],
)
def test_missing_auth_project_rejected(monkeypatch, path):
    """Without `auth_project` a hosted server must not fall back to its own cwd."""
    client = _missing_auth_project_client(monkeypatch)
    res = client.post(path, json={}) if path == "/cloudmap" else client.get(path)
    assert res.status_code == 400, res.get_data(as_text=True)
    assert res.json["code"] == "BAD_REQUEST"
    assert "auth_project" in res.json["message"]


@pytest.mark.parametrize(
    "path,body",
    [
        # goes through _patch_ensemble
        ("/update_ensemble", {"patch": [], "latest_commit": "abc123"}),
        # ... and _patch_environment
        ("/update_environment", {"patch": [], "latest_commit": "abc123"}),
        # the batch the rust worker forwards
        (
            "/batch_patch",
            {
                "latest_commit": "abc123",
                "requests": [{"endpoint": "update_ensemble", "patch": []}],
            },
        ),
    ],
)
def test_write_requires_branch(monkeypatch, path, body):
    """A write that names no branch is rejected, not applied to `main`.

    `main` needn't be the branch the client read: `GET /export` resolves an
    unnamed branch from the remote's advertised default and reports it back, so
    a write that omits one could land somewhere the client never looked.

    An absent `branch` fails schema validation (the field is required); an empty
    or blank one deserializes fine, so the handlers check it themselves. Neither
    reaches the repository -- `me/proj` doesn't exist, so a clone attempt or a
    500 would mean the check ran too late.
    """
    client = _missing_auth_project_client(monkeypatch)
    url = f"{path}?auth_project=me/proj"

    assert client.post(url, json=body).status_code == 422

    for empty in ("", "   "):
        res = client.post(url, json={**body, "branch": empty})
        assert res.status_code == 400, res.get_data(as_text=True)
        assert res.json["code"] == "BAD_REQUEST"
        assert "branch" in res.json["message"]


def test_batch_patch_checks_every_request_branch(monkeypatch):
    """One branchless request rejects the whole batch, before any of it applies.

    Each queued request carries the body it was submitted with, and the rust
    worker groups them by branch -- so a batch's own `branch` says nothing about
    what the requests in it named. Applying the good ones first and then failing
    would commit changes the client is told nothing about.
    """
    client = _missing_auth_project_client(monkeypatch)
    res = client.post(
        "/batch_patch?auth_project=me/proj",
        json={
            "branch": "main",
            "latest_commit": "abc123",
            "requests": [
                {"endpoint": "update_ensemble", "patch": [], "branch": "main"},
                {"endpoint": "update_ensemble", "patch": []},  # named none
            ],
        },
    )
    assert res.status_code == 400, res.get_data(as_text=True)
    assert res.json["code"] == "BAD_REQUEST"
    assert "branch" in res.json["message"]


@unittest.skipIf(not UNFURL_TEST_REDIS_URL, "UNFURL_TEST_REDIS_URL not set")
@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
def test_noop_batch_records_the_unchanged_commit():
    """A batch that commits nothing still writes the queue key, naming HEAD.

    The value looks like a redirect to itself, and that is deliberate: the
    proxy reads it as "batch finished, HEAD unchanged" -- INC_QUEUEID_SCRIPT
    restarts the queue there and readers proceed at `latest_commit`. Skipping
    the write instead leaves a bare counter, which strands writers at a
    queueid no one can advance and makes every reader poll to its deadline.

    `_update_queue_key`'s docstring says so; this is what makes it fail if
    someone acts on the other reading.
    """
    import redis as _redis

    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            p, port, first_commit = set_up_deployment(
                runner, deployment.format("initial"), server_env="redis", name="noop-q"
            )
            base = f"http://{HOST}:{port}"
            prefix = _variant_prefix("noop-q-redis")
            client = _redis.Redis.from_url(UNFURL_TEST_REDIS_URL)

            def queue_value(commit, branch="main"):
                # The branch is part of the key: two branches off one
                # commit are two batches recording their own new commits,
                # and sharing a key had the second overwrite the first.
                raw = client.get(f"{prefix}queue:remote:{branch}:{commit}")
                return raw.decode() if raw else None

            def head_value(branch="main"):
                # Must match `Config::head_key` in the rust proxy.
                raw = client.get(f"{prefix}head:remote:{branch}")
                return raw.decode() if raw else None

            def batch(reqs, latest_commit, queueid):
                return requests.post(
                    f"{base}/batch_patch?auth_project=remote",
                    json={
                        "branch": "main",
                        "latest_commit": latest_commit,
                        "queueid": queueid,
                        "requests": reqs,
                    },
                )

            def env_request(name, latest_commit):
                return {
                    "endpoint": "update_environment",
                    "patch": [{"name": name, "__typename": "DeploymentEnvironment"}],
                    "latest_commit": latest_commit,
                    "branch": "main",
                }

            # A real write first, so there is a commit to be unchanged from.
            res = batch([env_request("staging", first_commit)], first_commit, 1)
            assert res.status_code == 200, res.text
            second_commit = res.json()["commit"]
            assert second_commit != first_commit
            assert queue_value(first_commit) == f"{second_commit},1"
            assert head_value() == second_commit, (
                "a committed batch must record the branch head, or an idle "
                f"client watching the branch never hears it move: {head_value()!r}"
            )

            # The same patch again changes nothing, so nothing is committed.
            res = batch([env_request("staging", second_commit)], second_commit, 2)
            assert res.status_code == 200, res.text
            assert res.json()["commit"] == second_commit, (
                f"expected a no-op, but it committed: {res.text}"
            )
            assert queue_value(second_commit) == f"{second_commit},2", (
                "a no-op batch must still record HEAD, or writers deadlock and "
                f"readers poll to their deadline: {queue_value(second_commit)!r}"
            )
            assert head_value() == second_commit, (
                f"a no-op must leave the head where it is: {head_value()!r}"
            )

            # A batch can carry requests queued against different commits, and
            # the key is written per commit. With HEAD unmoved, the entry for
            # HEAD self-references while an older one is an ordinary redirect
            # to it -- same value, different meaning, so asserting only one
            # would miss the other going wrong.
            res = batch(
                [
                    env_request("staging", second_commit),
                    env_request("staging", first_commit),
                ],
                second_commit,
                3,
            )
            assert res.status_code == 200, res.text
            assert res.json()["commit"] == second_commit, res.text
            assert queue_value(second_commit) == f"{second_commit},3"
            assert queue_value(first_commit) == f"{second_commit},3"
        finally:
            _dump_server_logs(p, "noop-q")
            if p:
                _terminate_process(p)


@unittest.skipIf(not UNFURL_TEST_REDIS_URL, "UNFURL_TEST_REDIS_URL not set")
@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
def test_an_unbatched_write_records_the_branch_head():
    """A write that doesn't go through the queue still records the head.

    The `redis` variant has no write queue, so these commits reach
    `_commit_and_push` and `_push_changes` directly. Nothing else records
    the head on that path, and an idle client watching the branch is told
    to refetch by that key alone.
    """
    import redis as _redis

    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            p, port, last_commit = set_up_deployment(
                runner, deployment.format("initial"), server_env="redis", name="unb-q"
            )
            base = f"http://{HOST}:{port}"
            prefix = _variant_prefix("unb-q-redis")
            client = _redis.Redis.from_url(UNFURL_TEST_REDIS_URL)

            def head_value(branch="main"):
                raw = client.get(f"{prefix}head:remote:{branch}")
                return raw.decode() if raw else None

            # _commit_and_push: /update_environment writes unfurl.yaml.
            res = _post_write(
                f"{base}/update_environment?auth_project=remote",
                {
                    "patch": [
                        {"name": "staging", "__typename": "DeploymentEnvironment"}
                    ],
                    "latest_commit": last_commit,
                },
                "redis",
            )
            assert res.status_code == 200, res.text
            env_commit = res.json()["commit"]
            assert env_commit != last_commit, res.text
            assert head_value() == env_commit, (
                "_commit_and_push must record the head it pushed: "
                f"{head_value()!r} != {env_commit!r}"
            )

            # _push_changes: /update_ensemble commits the ensemble, then pushes.
            res = _post_write(
                f"{base}/update_ensemble?auth_project=remote",
                {
                    "patch": [
                        {
                            "name": "container_service",
                            "__typename": "ResourceTemplate",
                            "title": "renamed",
                        }
                    ],
                    "latest_commit": env_commit,
                },
                "redis",
            )
            assert res.status_code == 200, res.text
            ens_commit = res.json()["commit"]
            assert ens_commit != env_commit, res.text
            assert head_value() == ens_commit, (
                "_push_changes must record the head it pushed: "
                f"{head_value()!r} != {ens_commit!r}"
            )
        finally:
            _dump_server_logs(p, "unb-q")
            if p:
                _terminate_process(p)


def test_a_failed_push_discards_more_than_the_commit(tmp_path, monkeypatch):
    """`_commit_and_push` cleans what it could not push, not just the commit.

    It reset only tracked files until it shared `_discard_local_commits`
    with the batch path. New files survive `reset --hard`, and the next
    write commits with add_all, so a rejected push left them to be
    carried along by whoever wrote next.
    """
    path = tmp_path / "repo"
    path.mkdir()
    git_repo = Repo.init(path)
    (path / "f.yaml").write_text("one\n")
    (path / ".gitignore").write_text("jobs/\n")
    git_repo.git.add(A=True)
    git_repo.git.commit("-m", "first")
    repo = GitRepo(git_repo)
    start_revision = repo.revision

    # The change to commit, plus what a half-applied patch left around it.
    (path / "f.yaml").write_text("two\n")
    (path / "new-ensemble").mkdir()
    (path / "new-ensemble" / "ensemble.yaml").write_text("half written\n")
    (path / "jobs").mkdir()
    (path / "jobs" / "job.yaml").write_text("mid-write\n")

    # A remote that isn't there fails the push the way a rejected one does.
    # Without a remote at all `GitRepo._push` is a silent no-op.
    git_repo.create_remote("origin", str(tmp_path / "nowhere.git"))

    monkeypatch.setitem(server.app.config, "UNFURL_GUI_MODE", False)
    with server.app.app_context():
        err = server_endpoints._commit_and_push(
            repo,
            str(path / "f.yaml"),
            "a write that cannot be pushed",
            "",
            "",
            start_revision,
            "remote",
            "main",
        )

    assert err is not None, "a repo with no remote must fail to push"
    assert repo.revision == start_revision, "the unpushable commit should be gone"
    assert (path / "f.yaml").read_text() == "one\n"
    assert not (path / "new-ensemble").exists(), (
        "new files survive reset --hard, so a later write's add_all commits them"
    )
    assert not (path / "jobs").exists(), "-x should take ignored state too"


@pytest.mark.parametrize("gui_mode", [False, True])
def test_rollback_skipped_in_gui_mode(tmp_path, monkeypatch, gui_mode):
    """Gui mode keeps what a failed batch left; hosted mode discards it.

    Following the push logic: gui mode never pushes, so the local
    repository is the record rather than a staging area for a remote.
    Resetting there would throw away the user's work rather than protect
    a remote from a write that was reported as discarded.
    """
    path = tmp_path / "repo"
    path.mkdir()
    git_repo = Repo.init(path)
    (path / "f.yaml").write_text("one\n")
    (path / ".gitignore").write_text("jobs/\n")
    git_repo.git.add(A=True)
    git_repo.git.commit("-m", "first")
    repo = GitRepo(git_repo)
    start_revision = repo.revision

    # What the batch committed before it failed.
    (path / "f.yaml").write_text("two\n")
    git_repo.git.add(A=True)
    git_repo.git.commit("-m", "what the failed batch committed")
    mid_batch = repo.revision
    assert mid_batch != start_revision

    # ...and what it left uncommitted. A `create_ensemble` that errored
    # before committing leaves a new directory behind; `reset --hard`
    # doesn't touch it and `_patch_ensemble` commits with add_all, so a
    # later batch would commit it.
    (path / "new-ensemble").mkdir()
    (path / "new-ensemble" / "ensemble.yaml").write_text("half written\n")
    # Ignored state a patch writes goes back too -- that is what `-x` is for.
    (path / "jobs").mkdir()
    (path / "jobs" / "job.yaml").write_text("mid-batch\n")

    # A cloned dependency, in the layout unfurl actually builds: the clone
    # sits in the project and `tosca_repositories/<name>` is a symlink to
    # it (see RepoView.get_link).
    dep = path / "std"
    dep.mkdir()
    Repo.init(dep)
    (dep / "types.yaml").write_text("cloned\n")
    links = path / "tosca_repositories"
    links.mkdir()
    (links / ".gitignore").write_text("*")
    (links / "std").symlink_to("../std", target_is_directory=True)

    monkeypatch.setitem(server.app.config, "UNFURL_GUI_MODE", gui_mode)
    server_endpoints._rollback_batch(repo, start_revision)

    if gui_mode:
        assert repo.revision == mid_batch, "gui mode must not discard commits"
        assert (path / "f.yaml").read_text() == "two\n"
        assert (path / "new-ensemble").exists(), "nor untracked files"
        assert (path / "jobs" / "job.yaml").exists(), "nor ignored ones"
    else:
        assert repo.revision == start_revision, "the commit should be gone"
        assert (path / "f.yaml").read_text() == "one\n"
        assert not (path / "new-ensemble").exists(), (
            "new files survive reset --hard, so a later batch's add_all commits them"
        )
        assert not (path / "jobs").exists(), "-x should take ignored state too"
        # The symlink goes -- `-d` only spares a directory that is itself a
        # repository, and a symlink isn't one. It is not followed, so the
        # clone survives, and get_link recreates the link (and
        # tosca_repositories) on the next manifest load.
        assert not (links / "std").is_symlink()

    # Whatever the mode, the clone itself must not be destroyed: re-cloning
    # is expensive and nothing in a failed batch justifies it.
    assert (dep / "types.yaml").read_text() == "cloned\n"
    assert (dep / ".git").is_dir()

    # The flag the queue worker reads has to agree with what just happened
    # to the repository, or a batch left half-applied is reported as safe
    # to replay.
    with server.app.test_request_context():  # create_error_response jsonifies
        err = server.create_error_response("BAD_REQUEST", "boom")
        marked = server_endpoints._mark_rolled_back(err)
    assert marked.get_json()["rolled_back"] is not gui_mode


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
def test_failed_batch_leaves_a_dirty_working_copy_alone():
    """The dirtiness is observed before the batch applies, not after.

    Pins the `repo.is_dirty()` read in `batch_patch` rather than
    `_rollback_batch`'s handling of it: taken any later, the batch's own
    writes are what make the repo dirty, and the answer is about the wrong
    thing. Here a tracked file is edited behind the server's back, which is
    what the `was_dirty` branch in `_patch_environment` leaves behind for
    real -- a patch written to disk, uncommitted, and served by /export.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            p, port, last_commit = set_up_deployment(
                runner, deployment.format("initial"), name="dirty-rollback"
            )
            base = f"http://{HOST}:{port}"

            def env_request(name, commit):
                return {
                    "endpoint": "update_environment",
                    "patch": [{"name": name, "__typename": "DeploymentEnvironment"}],
                    "latest_commit": commit,
                    "branch": "main",
                }

            # One good batch so the server has cloned and has a clean copy.
            res = requests.post(
                f"{base}/batch_patch?auth_project=remote",
                json={
                    "branch": "main",
                    "latest_commit": last_commit,
                    "requests": [env_request("staging", last_commit)],
                },
            )
            assert res.status_code == 200, res.text
            last_commit = res.json()["commit"]

            # Uncommitted work in the server's copy, the way a was_dirty
            # patch leaves it.
            found = glob.glob("server/**/unfurl.yaml", recursive=True)
            assert len(found) == 1, f"expected one server clone, got {found}"
            server_config = found[0]
            marker = "# uncommitted work the batch never made\n"
            with open(server_config, "a") as f:
                f.write(marker)

            # ...and a batch that fails part way through. "tasks" is a
            # reserved name, so the second request is rejected after the
            # first has been applied.
            res = requests.post(
                f"{base}/batch_patch?auth_project=remote",
                json={
                    "branch": "main",
                    "latest_commit": last_commit,
                    "requests": [
                        env_request("prod", last_commit),
                        env_request("tasks", last_commit),
                    ],
                },
            )
            assert res.status_code == 400, res.text
            assert res.json().get("rolled_back") is False, (
                f"nothing was rolled back, so the worker must not be told it was: {res.text}"
            )
            assert marker in open(server_config).read(), (
                "the rollback discarded uncommitted work that predated the batch"
            )
        finally:
            _dump_server_logs(p, "dirty-rollback")
            if p:
                _terminate_process(p)


@pytest.mark.parametrize("started_dirty", [False, True])
def test_rollback_skipped_when_the_repo_was_already_dirty(tmp_path, started_dirty):
    """A rollback discards this batch's work, not what it found.

    `_patch_environment` writes the patch to disk and then skips the commit
    when it finds the repository dirty, returning success anyway, and
    /export serves that on-disk state -- so the user sees changes git
    doesn't have. Resetting over them would destroy work the batch never
    made and nothing would say so.
    """
    path = tmp_path / "repo"
    path.mkdir()
    git_repo = Repo.init(path)
    (path / "f.yaml").write_text("committed\n")
    git_repo.git.add(A=True)
    git_repo.git.commit("-m", "first")
    repo = GitRepo(git_repo)

    # Uncommitted work already here when the batch arrives, tracked and not.
    (path / "f.yaml").write_text("edited but never committed\n")
    (path / "stray.yaml").write_text("also never committed\n")
    assert repo.is_dirty()

    start_revision = repo.revision
    # ...and what this batch commits before failing.
    (path / "g.yaml").write_text("this batch\n")
    git_repo.git.add("g.yaml")
    git_repo.git.commit("-m", "what the failed batch committed")
    assert repo.revision != start_revision

    server_endpoints._rollback_batch(repo, start_revision, started_dirty)

    if started_dirty:
        assert repo.revision != start_revision, "left alone entirely"
        assert (path / "f.yaml").read_text() == "edited but never committed\n"
        assert (path / "stray.yaml").exists()
    else:
        assert repo.revision == start_revision
        assert (path / "f.yaml").read_text() == "committed\n"
        assert not (path / "stray.yaml").exists()

    # The flag has to agree, as in gui mode: a worker told the batch was
    # rolled back would retry writes that are still sitting in the repo.
    with server.app.test_request_context():
        err = server.create_error_response("BAD_REQUEST", "boom")
        marked = server_endpoints._mark_rolled_back(err, started_dirty)
    assert marked.get_json()["rolled_back"] is not started_dirty


@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
def test_batch_patch_rolls_back_a_mid_batch_failure():
    """A batch that fails part way through leaves nothing behind.

    The requests in a batch commit as they are applied and a single push
    happens at the end, so an error after the first one has committed used
    to leave that commit in the server's persistent working copy, unpushed
    and unreported. The next batch to push carried it along -- landing a
    write the client was told was discarded.

    The second batch here is what makes that observable. Without the
    rollback the leftover commit has already moved HEAD, so that batch is
    answered 409 against the `latest_commit` the client was told to keep
    using -- and had it been sent with the newer commit instead, its push
    would have carried "staging" along.
    """
    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            p, port, last_commit = set_up_deployment(
                runner, deployment.format("initial"), name="batch-rollback"
            )
            base = f"http://{HOST}:{port}"

            def env_request(name):
                return {
                    "endpoint": "update_environment",
                    "patch": [{"name": name, "__typename": "DeploymentEnvironment"}],
                    "latest_commit": last_commit,
                    "branch": "main",
                }

            # "tasks" is a reserved folder name, so the second request is
            # rejected by _patch_environment after the first has committed --
            # and the third never runs at all.
            res = requests.post(
                f"{base}/batch_patch?auth_project=remote",
                json={
                    "branch": "main",
                    "latest_commit": last_commit,
                    "requests": [
                        env_request("staging"),
                        env_request("tasks"),
                        env_request("never-applied"),
                    ],
                },
            )
            assert res.status_code == 400, res.text
            assert res.json()["code"] == "BAD_REQUEST", res.text
            assert "reserved" in res.json()["message"], res.text
            assert res.json().get("rolled_back") is True, (
                f"the batch worker reads this to decide a retry is safe: {res.text}"
            )
            # Which request failed, and that a third never ran. The error is
            # the failing request's own and says nothing about its position,
            # so without this a client sees one message and cannot tell the
            # batch held two other writes.
            assert res.json().get("failed_request") == {
                "endpoint": "update_environment",
                "index": 1,
                "count": 3,
                "skipped": 1,
            }, res.text
            # What ran before the failure. In server mode the rollback
            # undoes it, but the report is what lets a client in gui mode
            # -- where nothing is rolled back -- name the write that
            # survived instead of calling the whole batch discarded.
            assert res.json().get("applied") == [
                {"endpoint": "update_environment", "index": 0}
            ], res.text

            # A later batch must not carry the discarded commit with it.
            res = requests.post(
                f"{base}/batch_patch?auth_project=remote",
                json={
                    "branch": "main",
                    "latest_commit": last_commit,
                    "requests": [env_request("prod")],
                },
            )
            assert res.status_code == 200, res.text
            assert res.json()["commit"] != last_commit, res.text

            res = requests.get(
                f"{base}/export?format=environments&auth_project=remote&branch=main"
            )
            assert res.status_code == 200, res.text
            environments = res.json()["DeploymentEnvironment"]
            assert "prod" in environments, res.text
            assert "staging" not in environments, (
                f"the rolled back write landed anyway: {sorted(environments)}"
            )
        finally:
            _dump_server_logs(p, "batch-rollback")
            if p:
                _terminate_process(p)
