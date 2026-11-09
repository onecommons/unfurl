from pathlib import Path

import git
import pytest

from unfurl.cloudmap.db import CloudMapDB
from unfurl.cloudmap.upstream import compare_with_upstream, upstream_fetch_url
from unfurl.tosca_plugins.cloudmap_defs import Repository, UpstreamRef, UpstreamRefs
from unfurl.util import API_VERSION, UnfurlSchemaError

UPSTREAM = "git://example.com/upstream/project.git"


def _commit(repo: git.Repo, name: str) -> str:
    path = Path(repo.working_dir) / name
    path.write_text(name)
    repo.index.add([str(path)])
    return repo.index.commit(name).hexsha


@pytest.fixture
def repos(tmp_path):
    """An upstream and a fork of it whose refs cover every status."""
    upstream = git.Repo.init(tmp_path / "upstream", initial_branch="main")
    base = _commit(upstream, "base")
    upstream.create_head("same")
    upstream.create_head("behind")
    upstream.create_head("ahead")
    upstream.create_head("diverged")
    upstream.create_tag("v1", message="annotated")
    upstream.create_tag("moved")

    fork = git.Repo.clone_from(upstream.working_dir, tmp_path / "fork")

    upstream.heads.behind.checkout()
    _commit(upstream, "upstream-behind")
    upstream.heads.diverged.checkout()
    upstream_diverged = _commit(upstream, "upstream-diverged-1")
    _commit(upstream, "upstream-diverged-2")
    upstream.git.checkout("--orphan", "orphan")
    upstream_orphan = _commit(upstream, "upstream-orphan")

    branches = {}
    for name in ("main", "same", "behind", "ahead", "diverged", "orphan", "fork-only"):
        if name == "main":
            fork.heads.main.checkout()
        elif name == "orphan":
            fork.git.checkout("--orphan", name)
            _commit(fork, "fork-orphan")
        else:
            fork.git.checkout("-B", name, base)
            if name in ("ahead", "diverged", "fork-only"):
                _commit(fork, f"fork-{name}")
        branches[name] = fork.head.commit.hexsha
    fork.git.checkout("main")
    fork.git.tag("-f", "moved", branches["ahead"])
    record = Repository(
        url="git://example.com/fork/project.git",
        path="fork/project",
        fork_of=UPSTREAM,
        branches=branches,
        tags={"v1": base, "moved": branches["ahead"], "fork-tag": base},
    )
    return fork, upstream, record, base, upstream_diverged, upstream_orphan


def test_compare_with_upstream(repos):
    fork, upstream, record, base, upstream_diverged, upstream_orphan = repos
    refs = compare_with_upstream(fork, record, UPSTREAM, upstream.working_dir)
    head = upstream.heads
    assert refs.branches == {
        "main": UpstreamRef("identical", commit=base, merge_base=base, ahead=0, behind=0),
        "same": UpstreamRef("identical", commit=base, merge_base=base, ahead=0, behind=0),
        "behind": UpstreamRef(
            "behind", commit=head.behind.commit.hexsha, merge_base=base, ahead=0, behind=1
        ),
        "ahead": UpstreamRef("ahead", commit=base, merge_base=base, ahead=1, behind=0),
        "diverged": UpstreamRef(
            "diverged",
            commit=head.diverged.commit.hexsha,
            merge_base=base,
            ahead=1,
            behind=2,
        ),
        "orphan": UpstreamRef("unrelated", commit=upstream_orphan),
        "fork-only": UpstreamRef("missing"),
    }
    # the annotated upstream tag compares by the commit it points to
    assert refs.tags == {
        "v1": UpstreamRef("identical", commit=base, merge_base=base, ahead=0, behind=0),
        "moved": UpstreamRef("ahead", commit=base, merge_base=base, ahead=1, behind=0),
        "fork-tag": UpstreamRef("missing"),
    }
    # fetching the upstream leaves the fork's own refs alone
    assert {h.name: h.commit.hexsha for h in fork.heads} == record.branches
    assert fork.tags["moved"].commit.hexsha == record.tags["moved"]
    assert not fork.remotes[0].refs or all(
        r.commit.hexsha != upstream_orphan for r in fork.remotes[0].refs
    )


def test_compare_keeps_previous_for_refs_missing_locally(repos):
    fork, upstream, record, *_ = repos
    record.branches["same"] = "0" * 40
    record.branches["behind"] = "1" * 40
    previous = UpstreamRefs(branches={"same": UpstreamRef("ahead", commit="c", ahead=1)})
    refs = compare_with_upstream(fork, record, UPSTREAM, upstream.working_dir, previous)
    assert refs.branches["same"] == previous.branches["same"]
    assert "behind" not in refs.branches
    assert refs.branches["main"].status == "identical"


def test_upstream_fetch_url():
    assert upstream_fetch_url(UPSTREAM, None) == "https://example.com/upstream/project.git"
    assert upstream_fetch_url("https://example.com/a/b.git", None) == "https://example.com/a/b.git"
    upstream = Repository(url=UPSTREAM, path="upstream/project", protocols=["ssh"])
    assert upstream_fetch_url(UPSTREAM, upstream) == "git@example.com:upstream/project.git"


def test_upstream_refs_round_trip():
    serialized = {
        UPSTREAM: {
            "branches": {
                "main": {"status": "diverged", "commit": "b", "merge_base": "a", "ahead": 2, "behind": 0},
                "gh-pages": {"status": "unrelated", "commit": "c"},
            },
            "tags": {"v1": {"status": "missing"}},
        },
        "git://example.com/empty/project.git": {},
    }
    record = Repository(url="git://example.com/fork/project.git", path="p", upstream_refs=serialized)
    assert record.upstream_refs[UPSTREAM].branches["main"].behind == 0
    # zero counts are kept and empty comparisons dropped
    assert record.asdict()["upstream_refs"] == {UPSTREAM: serialized[UPSTREAM]}
    doc = {
        "apiVersion": API_VERSION,
        "kind": "CloudMap",
        "repositories": {record.url: record.asdict()},
    }
    CloudMapDB("cloudmap.yaml", contents=doc, validate=True)


@pytest.mark.parametrize(
    "ref",
    [
        {"commit": "a"},  # status is required
        {"status": "stale"},
        {"status": "ahead", "ahead": -1},
        {"status": "ahead", "extra": 1},
    ],
)
def test_upstream_refs_schema_rejects(ref):
    doc = {
        "apiVersion": API_VERSION,
        "kind": "CloudMap",
        "repositories": {
            "git://example.com/fork/project.git": {
                "path": "p",
                "upstream_refs": {UPSTREAM: {"branches": {"main": ref}}},
            }
        },
    }
    with pytest.raises(UnfurlSchemaError):
        CloudMapDB("cloudmap.yaml", contents=doc, validate=True)
