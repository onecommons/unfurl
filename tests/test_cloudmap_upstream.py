import dataclasses
from pathlib import Path
from typing import Dict

import git
import pytest

from unfurl.cloudmap.db import CloudMapDB
from unfurl.cloudmap.upstream import compare_with_upstream
from unfurl.repo import GitRepo
from unfurl.tosca_plugins.cloudmap_defs import Repository, UpstreamRef, UpstreamRefs
from unfurl.util import API_VERSION, UnfurlSchemaError

UPSTREAM = "git://example.com/upstream/project.git"
MAX_REFS = 100


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
    fork.remotes.origin.set_url("https://example.com/fork/project.git")
    record = Repository(
        url="git://example.com/fork/project.git",
        path="fork/project",
        fork_of=UPSTREAM,
        branches=branches,
        tags={"v1": base, "moved": branches["ahead"], "fork-tag": base},
    )
    return fork, upstream, record, base, upstream_diverged, upstream_orphan


def _upstream_record(upstream: git.Repo) -> Repository:
    return Repository(
        url=UPSTREAM,
        path="upstream/project",
        branches={h.name: h.commit.hexsha for h in upstream.heads},
        tags={t.name: t.commit.hexsha for t in upstream.tags},
    )


def _upstream_clone(upstream: git.Repo, path: Path) -> GitRepo:
    """A clone of the upstream whose remote is its url, holding none of the
    fork's commits."""
    clone = git.Repo.clone_from(upstream.working_dir, path)
    clone.remotes.origin.set_url("https://example.com/upstream/project.git")
    return GitRepo(clone)


def _has_commit(repo: git.Repo, commit: str) -> bool:
    return repo.git.cat_file("-t", commit, with_exceptions=False) == "commit"


def _upstream_side(
    compared: Dict[str, UpstreamRef], own: Dict[str, str]
) -> Dict[str, UpstreamRef]:
    """``compared``, checked to record each of ``own``'s commits, without them."""
    for name, ref in compared.items():
        assert ref.local_commit == own[name], name
    return {n: dataclasses.replace(r, local_commit="") for n, r in compared.items()}


def test_compare_with_upstream_in_clones(repos, tmp_path):
    fork, upstream, record, base, upstream_diverged, upstream_orphan = repos
    upstream_clone = _upstream_clone(upstream, tmp_path / "upstream-clone")
    # the upstream's record is out of date: the clone's refs are used
    upstream_record = Repository(url=UPSTREAM, path="upstream/project")
    refs = compare_with_upstream(
        record, upstream_record, GitRepo(fork), upstream_clone, MAX_REFS
    )
    head = upstream.heads
    assert _upstream_side(refs.branches, record.branches) == {
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
    assert _upstream_side(refs.tags, record.tags) == {
        "v1": UpstreamRef("identical", commit=base, merge_base=base, ahead=0, behind=0),
        "moved": UpstreamRef("ahead", commit=base, merge_base=base, ahead=1, behind=0),
        "fork-tag": UpstreamRef("missing"),
    }
    # each clone only read the other's commits
    assert not _has_commit(fork, upstream_orphan)
    assert not _has_commit(upstream_clone.repo, record.branches["orphan"])
    assert [r.name for r in fork.remotes] == ["origin"]


def test_compare_by_records_alone(repos):
    fork, upstream, record, base, _, upstream_orphan = repos
    refs = compare_with_upstream(record, _upstream_record(upstream), None, None, MAX_REFS)
    head = upstream.heads
    identical = UpstreamRef("identical", commit=base, merge_base=base, ahead=0, behind=0)
    assert _upstream_side(refs.branches, record.branches) == {
        "main": identical,
        "same": identical,
        "behind": UpstreamRef("differs", commit=head.behind.commit.hexsha),
        "ahead": UpstreamRef("differs", commit=base),
        "diverged": UpstreamRef("differs", commit=head.diverged.commit.hexsha),
        "orphan": UpstreamRef("differs", commit=upstream_orphan),
        "fork-only": UpstreamRef("missing"),
    }
    assert _upstream_side(refs.tags, record.tags) == {
        "v1": identical,
        "moved": UpstreamRef("differs", commit=base),
        "fork-tag": UpstreamRef("missing"),
    }


def test_compare_in_the_upstream_clone_alone(repos, tmp_path):
    """Refs whose fork commits it doesn't have are compared by commit."""
    fork, upstream, record, base, *_ = repos
    upstream_clone = _upstream_clone(upstream, tmp_path / "upstream-clone")
    refs = compare_with_upstream(
        record, _upstream_record(upstream), None, upstream_clone, MAX_REFS
    )
    statuses = {name: ref.status for name, ref in refs.branches.items()}
    assert statuses == {
        "main": "identical",
        "same": "identical",
        "behind": "behind",
        "ahead": "differs",
        "diverged": "differs",
        "orphan": "differs",
        "fork-only": "missing",
    }


def test_a_ref_past_a_cut_off_upstream_list_is_unknown(repos):
    fork, upstream, record, *_ = repos
    upstream_record = _upstream_record(upstream)
    refs = compare_with_upstream(
        record, upstream_record, None, None, len(upstream_record.branches)
    )
    assert refs.branches["fork-only"].status == "unknown"
    # the tag list isn't cut off
    assert refs.tags["fork-tag"].status == "missing"


def test_an_upstream_clone_missing_the_records_commits_isnt_used(repos, tmp_path):
    fork, upstream, record, base, *_ = repos
    upstream_clone = _upstream_clone(upstream, tmp_path / "upstream-clone")
    # the upstream moved on since the clone was last fetched
    upstream.heads.main.checkout()
    newer = _commit(upstream, "newer")
    upstream_record = _upstream_record(upstream)
    refs = compare_with_upstream(
        record, upstream_record, GitRepo(fork), upstream_clone, MAX_REFS
    )
    assert refs.branches["main"].commit == newer


def test_an_upstream_clone_without_a_remote_for_it_isnt_used(repos, tmp_path):
    fork, upstream, record, *_ = repos
    upstream_clone = _upstream_clone(upstream, tmp_path / "upstream-clone")
    upstream_clone.repo.remotes.origin.set_url("https://example.com/other/project.git")
    refs = compare_with_upstream(
        record, _upstream_record(upstream), None, upstream_clone, MAX_REFS
    )
    # from the record, which has main
    assert refs.branches["main"].status == "identical"


def test_tags_of_anything_but_commits_are_skipped(repos, tmp_path):
    fork, upstream, record, base, *_ = repos
    tree = upstream.heads.main.commit.tree.hexsha
    upstream.git.tag("tree-tag", tree)
    upstream.git.tag("-a", "-m", "annotated tree", "annotated-tree-tag", tree)
    upstream_clone = _upstream_clone(upstream, tmp_path / "upstream-clone")
    record.tags["tree-tag"] = base
    refs = compare_with_upstream(
        record, Repository(url=UPSTREAM, path="u"), None, upstream_clone, MAX_REFS
    )
    assert refs.tags["tree-tag"].status == "missing"
    assert refs.tags["v1"].status == "identical"


def test_a_shared_checkout_takes_tags_from_the_upstream_record(repos):
    """One checkout with remotes for both: its tags may be the fork's."""
    fork, upstream, record, base, *_ = repos
    url = "https://example.com/upstream/project.git"
    fork.git.config("--add", f"url.{upstream.working_dir}.insteadOf", url)
    fork.create_remote("upstream", url)
    fork.git.fetch("--no-tags", "upstream", "+refs/heads/*:refs/remotes/upstream/*")
    fork.create_tag("fork-tag", base)
    checkout = GitRepo(fork)
    upstream_record = _upstream_record(upstream)
    refs = compare_with_upstream(record, upstream_record, checkout, checkout, MAX_REFS)
    # the checkout's tags are the fork's: fork-tag isn't the upstream's, and
    # the upstream's moved is at base
    assert refs.tags["fork-tag"].status == "missing"
    assert refs.tags["moved"].status == "ahead"
    assert refs.branches["diverged"].status == "diverged"


def test_a_comparison_of_the_same_commits_is_kept(repos):
    fork, upstream, record, base, *_ = repos
    upstream_record = _upstream_record(upstream)
    upstream_behind = upstream_record.branches["behind"]
    previous = UpstreamRefs(
        branches={
            "behind": UpstreamRef(
                "behind",
                commit=upstream_behind,
                local_commit=record.branches["behind"],
                merge_base=base,
                ahead=0,
                behind=1,
            ),
            # compared when the fork's ref was elsewhere
            "ahead": UpstreamRef("ahead", commit=base, local_commit=base, ahead=0),
        }
    )
    refs = compare_with_upstream(
        record, upstream_record, None, None, MAX_REFS, previous
    )
    assert refs.branches["behind"] == previous.branches["behind"]
    assert refs.branches["ahead"].status == "differs"


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
