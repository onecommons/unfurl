import json
import os
import pytest
import requests
import time
import unittest.mock
from click.testing import CliRunner
from git import Repo
from multiprocessing import Process
from typing import List
from unfurl.repo import GitRepo
from unfurl.server import serve as server
from tests.server_utils import (
    RERUN_ON_SERVER_ERROR,
    HOST,
    UNFURL_TEST_REDIS_URL,
    _dump_server_logs,
    _fixture_is_current,
    _static_server_port,
    _terminate_process,
    _variant_prefix,
    deployment,
    runner,
    set_up_deployment,
)

pytestmark = RERUN_ON_SERVER_ERROR


def test_remote_refs_cached_as_plain_tuple(monkeypatch):
    """``get_remote_refs_cached`` stores ``(tags, default_branch)`` as a plain
    tuple and rebuilds a RemoteRefs from it, without a second ls-remote.

    Storing the NamedTuple itself would record ``unfurl.repo.RemoteRefs`` as
    the class to reconstruct, so moving or renaming it would make warm entries
    unreadable -- the same reason ``CacheValue`` is stored as a tuple. The
    cache here is a stand-in, so what's checked is the type handed to it, not
    the bytes the real serializer would produce.
    """
    from unfurl.repo import RemoteRefs
    from unfurl.server import cache as server_cache

    stored = {}

    class _Cache:
        def get(self, key):
            return stored.get(key)

        def set(self, key, value, timeout=None):
            stored[key] = value

    monkeypatch.setattr(server_cache, "get_cache", lambda: _Cache())
    monkeypatch.setattr(server_cache, "get_tags_from_proxy", lambda *a, **kw: None)
    monkeypatch.setattr(
        server_cache,
        "get_remote_refs",
        lambda url, pattern: RemoteRefs(["v1.0"], "main"),
    )
    monkeypatch.setitem(server.app.config, "UNFURL_CLOUD_SERVER", "")
    monkeypatch.setitem(server.app.config, "CACHE_DEFAULT_REMOTE_TAGS_TIMEOUT", 300)

    url = "https://unfurl.cloud/onecommons/std.git"
    with server.app.app_context():
        refs = server_cache.get_remote_refs_cached(url, "*", None)
        assert refs == RemoteRefs(["v1.0"], "main")

        (cached,) = stored.values()
        assert type(cached) is tuple, cached  # not a RemoteRefs subclass
        assert cached == (["v1.0"], "main")

        # served from the cache next time, reconstructed as RemoteRefs
        monkeypatch.setattr(
            server_cache,
            "get_remote_refs",
            lambda url, pattern: pytest.fail("should have hit the cache"),
        )
        again = server_cache.get_remote_refs_cached(url, "*", None)
        assert isinstance(again, RemoteRefs)
        assert again.default_branch == "main"


def test_default_branch(tmp_path):
    """GitRepo.default_branch reads origin/HEAD, and says "" when there is none.

    /branches only claims ``default: True`` when this matches the branch it
    reports, so the distinction between "not recorded" and "not the default"
    matters.
    """

    def _repo(name) -> GitRepo:
        path = tmp_path / name
        path.mkdir()
        repo = Repo.init(path)
        (path / "f.txt").write_text("x\n")
        repo.git.add(A=True)
        repo.git.commit("-m", "init")
        return GitRepo(repo)

    # no remote: nothing advertised a default, so it stays unknown rather than
    # assuming the one branch that exists locally is it
    solo = _repo("solo")
    assert solo.active_branch  # not the reason the next line is ""
    assert solo.default_branch == ""

    # "" from a repository with no remote isn't cached, so adding one later
    # (e.g. `unfurl init` then pushing the project somewhere) is picked up
    solo.repo.git.remote("add", "origin", "https://unfurl.cloud/onecommons/std.git")
    solo.repo.git.symbolic_ref(
        "refs/remotes/origin/HEAD", f"refs/remotes/origin/{solo.active_branch}"
    )
    assert solo.default_branch == solo.active_branch

    # a remote whose advertised HEAD the clone recorded. A dangling symref is
    # legal and is what `git clone` leaves behind; `git remote set-head -a`
    # would need the network.
    cloned = _repo("cloned")
    cloned.repo.git.remote("add", "origin", "https://unfurl.cloud/onecommons/std.git")
    on_a_branch = cloned.active_branch
    cloned.repo.git.symbolic_ref(
        "refs/remotes/origin/HEAD", f"refs/remotes/origin/{on_a_branch}"
    )
    assert cloned.default_branch == on_a_branch

    # a remote that never recorded one, e.g. an `init` + `fetch` checkout
    no_head = _repo("no-origin-head")
    no_head.repo.git.remote("add", "origin", "https://unfurl.cloud/onecommons/std.git")
    assert no_head.default_branch == ""

    # cached: the answer survives the symref being rewritten underneath it
    cloned.repo.git.symbolic_ref(
        "refs/remotes/origin/HEAD", "refs/remotes/origin/something-else"
    )
    assert cloned.default_branch == on_a_branch
    assert GitRepo(cloned.repo).default_branch == "something-else"


def test_cache_value_shape_tolerance():
    """A cache entry is stored as a plain tuple and read back through CacheValue.

    Entries written by a different revision of the code therefore have to be
    tolerated: an extra trailing field is dropped, and anything that can't be
    turned into a CacheValue is a miss rather than an exception. ``cachelib``
    only converts ``PickleError``, so a class it can't import raises
    ``AttributeError`` and a wrong shape ``TypeError`` -- neither reaches the
    caller as ``None`` on its own.
    """

    class _Cache:
        """Stands in for flask-caching; ``get_cache`` only calls ``get``."""

        def __init__(self, value):
            self._value = value

        def get(self, key):
            if isinstance(self._value, Exception):
                raise self._value
            return self._value

    entry = server.CacheEntry("proj", "main", "f.yaml", "deployment")
    stored = ("payload", "abc123", "def456", {}, 1700000000, 1786000000.0, 0)

    value, _stale = entry.get_cache(_Cache(stored), "def456")
    assert entry.hit and value is not None
    assert value.value == "payload"
    assert value.created == 1786000000.0

    # an entry from a newer revision carrying a field this one doesn't know
    value, _stale = entry.get_cache(_Cache(stored + ("from the future",)), "def456")
    assert entry.hit and value is not None
    assert value.queueid == 0

    # too few fields, and an entry naming a class this revision can't import
    for bad in (
        ("payload", "abc123", "def456", {}),
        AttributeError("module has no attribute 'CacheItemDependency'"),
    ):
        value, stale = entry.get_cache(_Cache(bad), "def456")
        assert value is None, bad
        assert stale is None, bad
        assert not entry.hit, bad


def test_failed_export_is_not_cached():
    """An export that reports a failure in its payload must not be cached.

    ``to_environments`` records a per-environment exception as an ``error``
    entry instead of raising, so the export itself succeeds. The cache key is
    invalidated only by a new commit, so storing that result replays the
    failure until one arrives -- long after whatever caused it was fixed.
    """
    broken = {"DeploymentEnvironment": {"prod": {"error": "Internal Error"}}}
    clean = {"DeploymentEnvironment": {"prod": {"name": "prod"}}}

    assert server._has_embedded_errors(broken)
    assert not server._has_embedded_errors(clean)
    assert not server._has_embedded_errors({"DeploymentPath": []})
    assert not server._has_embedded_errors("not a dict")

    class _Cache:
        """Stands in for flask-caching; ``set_cache`` only calls these two."""

        def __init__(self):
            self.stored: List[str] = []
            self.deleted: List[str] = []

        def set(self, key, value, timeout=None):
            self.stored.append(key)

        def delete(self, key):
            self.deleted.append(key)
            return True

    entry = server.CacheEntry("proj", "main", "unfurl.yaml", "environments")
    entry.last_commit = "abc123"  # skip the git lookup in _set_commit_info
    cache = _Cache()

    entry.set_cache(
        cache,
        server.CacheDirective(latest_commit="def456", store=False),
        broken,
    )

    # deleted, not marked: whatever was already under the key would otherwise
    # outlive the condition that made this result uncacheable, and the rust
    # front end reads these entries without knowing any python-side marker.
    assert cache.deleted == [entry.cache_key()]
    assert not cache.stored


def test_fixture_is_current(tmp_path):
    """The rust fixtures are rewritten only when the cached document changed.

    `CacheValue.created` is set from the clock, so every redis-enabled run of
    `test_server_export_remote` stores different bytes for the same document.
    Comparing bytes would rewrite the fixtures each run and leave them showing
    as modified in git forever.
    """
    import pickle

    def entry(doc, created) -> bytes:
        # what `_save_rust_fixtures` copies out of redis: a CacheValue's fields
        # behind the "!" the redis serializer prefixes
        fields = (doc, "abc123", "abc123", {}, created, 0)
        return b"!" + pickle.dumps(fields, protocol=5)

    now = 1786969954.348405
    dest = tmp_path / "blueprint.pkl"
    # nothing saved yet
    assert not _fixture_is_current(str(dest), entry({"a": 1}, now))

    dest.write_bytes(entry({"a": 1}, now))
    # same document, later run
    assert _fixture_is_current(str(dest), entry({"a": 1}, now + 46.2))
    # the document itself changed
    assert not _fixture_is_current(str(dest), entry({"a": 2}, now))
    # a last float that isn't a timestamp means the field order this relies on
    # changed, so don't claim the fixture is current
    assert not _fixture_is_current(str(dest), entry({"a": 1}, 0.5))
    # unreadable bytes are new, not a match
    assert not _fixture_is_current(str(dest), b"!not an entry")


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
        assert res.status_code == 200, res.text
        assert res.content == b"OK"


def test_populate_cache_accepts_a_files_batch(runner: Process):
    """A push is one request carrying every file it touched.

    The per-file query form above still works, so a caller that predates the
    batch keeps functioning; this is the form the GitLab worker sends.
    """
    port = _static_server_port
    res = requests.post(
        f"http://{HOST}:{port}/populate_cache",
        params={
            "secret": "secret",
            "auth_project": "onecommons/project-templates/dashboard",
            "latest_commit": "HEAD",
            "visibility": "public",
        },
        json={
            "files": [
                {"path": "unfurl.yaml"},
                {"path": "ensemble/ensemble.yaml"},
            ]
        },
    )
    assert res.status_code == 200, res.text
    assert res.content == b"OK"


def test_populate_cache_deletes_the_entries_a_batch_marks_removed(runner: Process):
    """A push that deletes a file and changes another carries both outcomes.

    Only one flag existed per request before, so a push doing both needed two
    requests and the deletion could not travel with the change.
    """
    port = _static_server_port
    res = requests.post(
        f"http://{HOST}:{port}/populate_cache",
        params={
            "secret": "secret",
            "auth_project": "onecommons/project-templates/dashboard",
            "latest_commit": "HEAD",
            "visibility": "public",
        },
        json={
            "files": [
                {"path": "unfurl.yaml", "removed": True},
                {"path": "ensemble/ensemble.yaml", "removed": False},
            ]
        },
    )
    assert res.status_code == 200, res.text
    assert res.content == b"OK"


def test_populate_cache_refuses_an_empty_files_batch(runner: Process):
    """The body path gives the same answer as the query path.

    The test below sends no body at all; this one sends a well-formed body that
    names nothing, which reaches the same check by a different route.
    """
    port = _static_server_port
    res = requests.post(
        f"http://{HOST}:{port}/populate_cache",
        params={
            "secret": "secret",
            "auth_project": "onecommons/project-templates/dashboard",
            "latest_commit": "HEAD",
            "visibility": "public",
        },
        json={"files": []},
    )
    assert res.status_code == 400


def test_populate_cache_refuses_a_request_naming_no_files(runner: Process):
    port = _static_server_port
    res = requests.post(
        f"http://{HOST}:{port}/populate_cache",
        params={
            "secret": "secret",
            "auth_project": "onecommons/project-templates/dashboard",
            "latest_commit": "HEAD",
            "visibility": "public",
        },
    )
    assert res.status_code == 400


@unittest.skipIf(not UNFURL_TEST_REDIS_URL, "UNFURL_TEST_REDIS_URL not set")
@unittest.skipIf("slow" in os.getenv("UNFURL_TEST_SKIP", ""), "UNFURL_TEST_SKIP set")
def test_populate_cache_sets_the_branch_head_only_when_asked():
    """`sethead` is what writes the head key, and the rust proxy reads it back.

    Two halves of one claim, so one server serves both. The gate: nothing
    is written unless asked, because any caller can reach /populate_cache
    and an untrue head tells every watcher of that branch to refetch. And
    the round trip: python and the proxy build that key from a comment
    telling each to match the other, so only running them against one
    Redis proves they agree -- the `moved` frame arrives if and only if
    the shapes are identical.

    The variant is picked rather than skipped. `/events` needs the proxy,
    but the gate does not, so a run with the proxy turned off still
    checks it against python alone.
    """
    import redis as _redis

    proxied = os.getenv("UNFURL_TEST_RUST_SERVER") != "0"
    variant = "queue-rust" if proxied else "redis"

    runner = CliRunner()
    with runner.isolated_filesystem():
        p = None
        try:
            p, port, first_commit = set_up_deployment(
                runner,
                deployment.format("initial"),
                server_env=variant,
                name="head-q",
            )
            base = f"http://{HOST}:{port}"
            prefix = _variant_prefix(f"head-q-{variant}")
            client = _redis.Redis.from_url(UNFURL_TEST_REDIS_URL)
            # Must match `Config::head_key` in the rust proxy.
            head_key = f"{prefix}head:remote:main"
            client.delete(head_key)

            def populate(**extra):
                params = {
                    "auth_project": "remote",
                    "branch": "refs/heads/main",
                    "latest_commit": first_commit,
                    "path": "ensemble/ensemble.yaml",
                    "visibility": "public",
                    # `removed` returns early, which is where the head write
                    # has to already have happened: a push that only deleted
                    # files still moved the branch.
                    "removed": "1",
                }
                params.update(extra)
                params = {k: v for k, v in params.items() if v is not None}
                return requests.post(f"{base}/populate_cache", params=params)

            res = populate()
            assert res.status_code == 200, res.text
            assert client.get(head_key) is None, (
                "populate_cache wrote the branch head without being asked to"
            )

            # The literal values GitLab's ProjectUnfurlCacheWorker puts on
            # the wire: its gate is a boolean, so a push it declines to
            # report arrives as "false", not as an absent parameter.
            for declined in ["false", "0", ""]:
                res = populate(sethead=declined)
                assert res.status_code == 200, res.text
                assert client.get(head_key) is None, (
                    f"sethead={declined!r} must not write the branch head"
                )

            # A request that named no branch falls back to DEFAULT_BRANCH,
            # which is a guess -- not something to publish as a head.
            res = populate(sethead="true", branch=None)
            assert res.status_code == 200, res.text
            assert client.get(head_key) is None, (
                "an implicit branch must not be published as a head"
            )

            res = populate(sethead="true")
            assert res.status_code == 200, res.text
            raw = client.get(head_key)
            assert raw and raw.decode() == first_commit, (
                f"expected {first_commit} at {head_key}, got {raw!r}"
            )
            assert client.ttl(head_key) > 0, "the head key must expire"

            # A branch watch is a trailing empty queueid: nothing in flight.
            stale = "0" * 40
            frames = []
            # A watch with nothing to report is held open for the whole
            # events budget, so bound the read rather than sit out a
            # failure for two minutes. Keep-alive comments arrive every
            # 15s, which is what lets the deadline be checked at all.
            deadline = time.monotonic() + 20
            with requests.get(
                f"{base}/events",
                params={"auth_project": "remote", "watch": f"main:{stale}:"},
                stream=True,
                timeout=30,
            ) as res:
                assert res.status_code == 200, res.text
                for line in res.iter_lines(decode_unicode=True):
                    if line and line.startswith("data:"):
                        frames.append(json.loads(line[len("data:") :]))
                        if frames[-1].get("status") == "done":
                            break
                    if time.monotonic() > deadline:
                        break

            if not proxied:
                # No proxy, so nothing serves a watch: python's /events is a
                # no-op that reports the stream finished and closes.
                assert [f.get("status") for f in frames] == ["done"], frames
                return
            moved = [f for f in frames if f.get("status") == "moved"]
            assert moved, (
                "no 'moved' frame: the rust proxy did not read the head key "
                f"python wrote, so the two key shapes disagree. Frames: {frames}"
            )
            assert moved[0]["new_commit"] == first_commit, moved[0]
            assert moved[0]["branch"] == "main", moved[0]
        finally:
            _dump_server_logs(p, "head-q")
            if p:
                _terminate_process(p)


def test_clone_repo_leaves_another_clones_lock(monkeypatch, tmp_path):
    """A clone that finds the repository locked by another fails, and leaves
    that lock in place."""
    monkeypatch.setitem(server.app.config, "UNFURL_CLONE_ROOT", str(tmp_path))
    monkeypatch.setitem(server.app.config, "UNFURL_CLOUD_SERVER", "https://unfurl.cloud")
    with server.app.app_context():
        repo_path = server._get_project_repo_dir("org/proj", "main", {})
        lock = f"{repo_path}.lock"
        os.makedirs(os.path.dirname(lock))
        with open(lock, "w") as f:
            f.write("12345")
        with pytest.raises(FileExistsError):
            server._clone_repo("org/proj", "main", None, {})
    with open(lock) as f:
        assert f.read() == "12345"


def _git(cwd, *args):
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _remote(tmp_path):
    """A repository standing in for the project's remote, on ``main``."""
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "init", "-q", "-b", "main")
    (remote / "cloudmap.yaml").write_text("repositories: {}\n")
    _git(remote, "add", ".")
    _git(remote, "commit", "-q", "-m", "first")
    return remote


@pytest.fixture
def clone_root(monkeypatch, tmp_path):
    monkeypatch.setitem(server.app.config, "UNFURL_CLONE_ROOT", str(tmp_path / "clones"))
    monkeypatch.setitem(server.app.config, "UNFURL_CLOUD_SERVER", "https://unfurl.cloud")
    monkeypatch.setattr(server, "get_cache", lambda: object())
    monkeypatch.setattr(server, "clear_cache", lambda cache, prefix: [])
    return tmp_path


def test_clear_project_keeps_clones_with_unpushed_work(clone_root):
    """Clearing a project removes its clones, but for one holding work its
    remote lacks -- an uncommitted change, or a commit, as a cloudmap write
    leaves -- which a clone made again wouldn't have; the response says
    which, and why. ``force`` removes them too."""
    remote = _remote(clone_root)
    with server.app.app_context():
        clones = {}
        for name in ["clean", "dirty", "ahead"]:
            clones[name] = server._get_project_repo_dir("org/proj", name, {})
            _git(clone_root, "clone", "-q", str(remote), clones[name])
        with open(os.path.join(clones["dirty"], "cloudmap.yaml"), "a") as f:
            f.write("# an edit not committed yet\n")
        with open(os.path.join(clones["ahead"], "cloudmap.yaml"), "a") as f:
            f.write("# an edit not pushed\n")
        _git(clones["ahead"], "commit", "-q", "-am", "not pushed")

        got = server._clear_project("org/proj")
        assert got["found"] is True
        assert got["removed"] == ["public/org/proj/clean"]
        assert sorted((k["path"], k["reason"]) for k in got["kept"]) == [
            ("public/org/proj/ahead", "1 commit not pushed"),
            ("public/org/proj/dirty", "uncommitted changes"),
        ]
        assert not os.path.exists(clones["clean"])
        assert os.path.isdir(clones["dirty"])
        assert os.path.isdir(clones["ahead"])

        got = server._clear_project("org/proj", force=True)
        assert got["kept"] == []
        assert not os.path.exists(clones["dirty"])
        assert not os.path.exists(clones["ahead"])

        got = server._clear_project("org/proj")
        assert got["found"] is False, "nothing left to clear"


def test_a_failed_pull_serves_a_clone_with_unpushed_work(clone_root):
    """A pull that can't fast-forward a clone holding a commit its remote
    lacks keeps it and serves it as it is; another branch's clone is left
    alone."""
    from cachelib import SimpleCache

    remote = _remote(clone_root)
    with server.app.app_context():
        main = server._get_project_repo_dir("org/proj", "main", {})
        other = server._get_project_repo_dir("org/proj", "other", {})
        for path in [main, other]:
            _git(clone_root, "clone", "-q", str(remote), path)
        with open(os.path.join(main, "cloudmap.yaml"), "a") as f:
            f.write("# ours\n")
        _git(main, "commit", "-q", "-am", "ours")
        (remote / "theirs").write_text("theirs")
        _git(remote, "add", ".")
        _git(remote, "commit", "-q", "-m", "theirs")

        entry = server.CacheEntry("org/proj", "main", "cloudmap.yaml", "load_yaml")
        repo = entry.pull(SimpleCache())
        assert entry.pull_state == "diverged"
        assert repo.working_dir.rstrip("/") == main
        assert os.path.isdir(other), "another branch's clone isn't touched"


def test_a_failed_pull_removes_only_its_own_clean_clone(clone_root):
    """A clean clone whose pull fails is removed to be cloned again, and
    only it: another branch's clone stays."""
    from cachelib import SimpleCache

    remote = _remote(clone_root)
    with server.app.app_context():
        main = server._get_project_repo_dir("org/proj", "main", {})
        other = server._get_project_repo_dir("org/proj", "other", {})
        for path in [main, other]:
            _git(clone_root, "clone", "-q", str(remote), path)
        _git(main, "remote", "set-url", "origin", str(clone_root / "gone"))

        entry = server.CacheEntry(
            "org/proj", "main", "cloudmap.yaml", "load_yaml", do_clone=False
        )
        with pytest.raises(Exception):
            entry.pull(SimpleCache())
        assert not os.path.exists(main)
        assert os.path.isdir(other)


def test_concurrent_pulls_pull_once(clone_root, monkeypatch):
    """Two requests that find a checkout's pull stale at once: one pulls,
    the other waits for it and takes what it left, rather than both
    pulling into one working tree."""
    import threading
    from cachelib import SimpleCache

    remote = _remote(clone_root)
    with server.app.app_context():
        main = server._get_project_repo_dir("org/proj", "main", {})
        _git(clone_root, "clone", "-q", str(remote), main)

    pulls = []

    def slow_pull(repo, branch, shallow_since=None):
        pulls.append(branch)
        time.sleep(0.5)
        return "pulled"

    monkeypatch.setattr(server, "pull", slow_pull)
    # both read the pull's state before either claims it: the race
    both_read = threading.Barrier(2)
    seen = threading.local()

    class RacingCache(SimpleCache):
        def get(self, key):
            value = super().get(key)
            if ":pull:" in key and not key.startswith("_pull_lock") and not getattr(seen, "once", False):
                seen.once = True
                both_read.wait(timeout=5)
            return value

    cache = RacingCache()
    got = []

    def request():
        with server.app.app_context():
            entry = server.CacheEntry("org/proj", "main", "cloudmap.yaml", "load_yaml")
            got.append(entry.pull(cache).working_dir)

    threads = [threading.Thread(target=request) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert pulls == ["main"]
    assert len(got) == 2 and got[0] == got[1]


def test_clone_repo_without_project_id(monkeypatch, tmp_path):
    """A server serving a local path that isn't a repo must fail cleanly.

    `get_project_id_or_abort` lets a request through when UNFURL_SERVE_PATH is set, so it can
    still reach `_stage` -> `_clone_repo` with an empty project_id. There is nothing to
    clone at that point; it must raise UnfurlError (which `_stage` turns into "no repo")
    rather than the FileNotFoundError that `os.makedirs("")` used to raise.
    """
    from unfurl.util import UnfurlError

    monkeypatch.setenv("UNFURL_SERVE_PATH", str(tmp_path))
    monkeypatch.setitem(server.app.config, "UNFURL_CLOUD_SERVER", "https://unfurl.cloud")
    monkeypatch.chdir(tmp_path)  # not a git worktree
    with server.app.app_context():
        with pytest.raises(UnfurlError, match="auth_project"):
            server._clone_repo("", "main", None, {})
        # ... and _stage reports "no repo" instead of propagating
        assert server._stage("", "main", {}, False) is None


def test_get_default_branch_local_project(tmp_path, monkeypatch):
    """A local project's branch comes from its checkout, not the remote's tags.

    A local project is exported from its working directory as-is, so the latest
    remote tag isn't what's being served -- and a developer's clone can be
    shallow, in which case that tag isn't a revision the repo can resolve at
    all. `git rev-list <tag>` then fails and `CacheEntry.set_cache` gives up,
    which silently disables caching for the whole project.
    """
    repo_dir = tmp_path / "std"
    repo_dir.mkdir()
    repo = Repo.init(repo_dir)
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("user", "name", "test")
    (repo_dir / "dummy-ensemble.yaml").write_text("{}\n")
    repo.git.add(A=True)
    repo.git.commit("-m", "init")
    repo.git.tag("v1.0.3")

    def _no_remote_tags(*args, **kw):
        raise AssertionError("must not check remote tags for a local project")

    monkeypatch.setattr(server, "set_version_from_remote_tags", _no_remote_tags)
    monkeypatch.setitem(
        server.app.config, "UNFURL_LOCAL_PROJECTS", {"onecommons/std": str(repo_dir)}
    )

    on_a_branch = repo.active_branch.name
    assert server.get_local_branch("onecommons/std") == on_a_branch
    # a local project reports no default branch: no remote is consulted
    assert server.get_latest_tag_or_default_branch("onecommons/std") == (
        on_a_branch,
        "",
    )

    # A shallow clone of a tag lands on a detached HEAD. Report "HEAD" rather
    # than the tag: every repo can resolve HEAD, but a clone that was made
    # without a tag (or before it existed) can't resolve the tag name.
    repo.git.checkout("v1.0.3")
    assert server.get_local_branch("onecommons/std") == "HEAD"
    assert server.get_latest_tag_or_default_branch("onecommons/std") == ("HEAD", "")
    assert repo.git.rev_list("--max-count=1", "HEAD", "--", "dummy-ensemble.yaml")

    # projects that aren't local still go through remote tag resolution
    assert server.get_local_branch("onecommons/unfurl-types") == ""


@pytest.mark.parametrize(
    "visibility, branch, configured, analyzed",
    [
        ("public", "main", True, True),
        ("public", "feature", True, True),
        ("private", "main", True, False),
        ("public", "main", False, False),
    ],
)
def test_populate_cache_analyzes_exported_files(
    monkeypatch, tmp_path, visibility, branch, configured, analyzed
):
    """A cache miss in populate_cache analyzes the exported files, in the
    checkout it already has, into the server's default cloudmap -- here the
    rust server's worktree; a server without one analyzes nothing."""
    import uuid
    from unittest.mock import MagicMock
    from flask_caching import Cache

    project_id = f"test/populate-{uuid.uuid4().hex}"
    monkeypatch.delenv("UNFURL_SERVE_PATH", raising=False)
    monkeypatch.setitem(server.app.config, "UNFURL_CLOUD_SERVER", "https://unfurl.cloud")
    monkeypatch.setitem(server.app.config, "UNFURL_CLONE_ROOT", str(tmp_path))
    monkeypatch.setitem(
        server.app.config,
        "UNFURL_LOCAL_CLOUDMAP_URL",
        "http://127.0.0.1:8081" if configured else None,
    )

    # the project's checkout, where populate_cache expects it
    project_dir = tmp_path / visibility / project_id / branch
    git_repo = Repo.init(project_dir, initial_branch=branch)
    names = ["ensemble/ensemble.yaml", "a b.yaml", "c.yaml"]
    for name in names:
        (project_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (project_dir / name).write_text("{}")
    git_repo.index.add(names)
    latest_commit = git_repo.index.commit("push").hexsha

    def set_repo(self):
        self.repo = GitRepo(git_repo)
        return self.repo

    monkeypatch.setattr(server.CacheEntry, "_set_project_repo", set_repo)
    monkeypatch.setattr(
        server, "_export_cache_work", lambda entry, commit: (None, {"ok": 1}, True)
    )
    update_cloudmap = MagicMock(return_value={"commit": None, "added": [], "skipped": []})
    import unfurl.server.cloudmap as server_cloudmap_module

    monkeypatch.setattr(server_cloudmap_module, "update_cloudmap", update_cloudmap)
    cache = Cache(config={"CACHE_TYPE": "SimpleCache"})
    cache.init_app(server.app)
    monkeypatch.setattr(server, "_cache", cache)
    client = server.app.test_client()

    def push(files=("ensemble/ensemble.yaml", "a b.yaml")):
        res = client.post(
            "/populate_cache",
            query_string=dict(
                auth_project=project_id,
                latest_commit=latest_commit,
                branch=branch,
                visibility=visibility,
            ),
            json={"files": [{"path": path} for path in files]},
        )
        assert res.status_code == 200, res.get_data(as_text=True)

    push()
    if not analyzed:
        update_cloudmap.assert_not_called()
        return
    update_cloudmap.assert_called_once()
    cloudmap_project, body, update = update_cloudmap.call_args.args
    assert cloudmap_project == ""  # the rust server's worktree
    assert body.analyze == "yes"
    # the update analyzes the checkout populate_cache has
    cloud_map = MagicMock()
    cloud_map.analyze_checkout.return_value.key = "git://unfurl.cloud/x.git"
    update(cloud_map)
    repo_url, repo, pushed_branch, paths = cloud_map.analyze_checkout.call_args.args
    assert repo_url == f"git://unfurl.cloud/{project_id}.git"
    assert os.path.samefile(repo.working_dir, project_dir)
    assert pushed_branch == branch
    assert paths == ["ensemble/ensemble.yaml", "a b.yaml"]

    # the same push again is a cache hit: nothing exported, nothing analyzed
    update_cloudmap.reset_mock()
    push()
    update_cloudmap.assert_not_called()

    # a failed analysis doesn't fail the push
    update_cloudmap.side_effect = RuntimeError("cloudmap unavailable")
    push(["c.yaml"])
    update_cloudmap.assert_called_once()
