import dataclasses
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from unittest.mock import Mock, patch

from click.testing import CliRunner

import git
import pytest

from unfurl.cloudmap.db import CloudMapDB
from unfurl.cloudmap.gitsync_db import Backend
from unfurl.logs import UnfurlLogger
from unfurl.__main__ import cli
from unfurl.cloudmap import Directory
from unfurl.cloudmap.upstream import (
    compare_with_remotes,
    compare_with_upstream,
    local_repository,
)
from unfurl.repo import GitRepo
from unfurl.tosca_plugins.cloudmap_defs import (
    Repository,
    UpstreamRef,
    UpstreamRefs,
    get_repository_url,
)
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
    # refs at the same commit are left out
    assert _upstream_side(refs.branches, record.branches) == {
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
    assert _upstream_side(refs.branches, record.branches) == {
        "behind": UpstreamRef("differs", commit=head.behind.commit.hexsha),
        "ahead": UpstreamRef("differs", commit=base),
        "diverged": UpstreamRef("differs", commit=head.diverged.commit.hexsha),
        "orphan": UpstreamRef("differs", commit=upstream_orphan),
        "fork-only": UpstreamRef("missing"),
    }
    assert _upstream_side(refs.tags, record.tags) == {
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
    assert "main" not in refs.branches
    assert refs.branches["fork-only"].status == "missing"


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
    assert "v1" not in refs.tags


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
    # zero counts are kept, and so is an empty comparison: every ref matched
    assert record.asdict()["upstream_refs"] == serialized
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


@pytest.fixture
def work_clone(tmp_path):
    """An upstream and a clone of it doing local work."""
    upstream = git.Repo.init(tmp_path / "remote", initial_branch="main")
    base = _commit(upstream, "base")
    upstream.create_head("release")
    clone = git.Repo.clone_from(upstream.working_dir, tmp_path / "repos" / "work")
    # tracks origin/release under another name, one commit ahead of it
    mine = clone.create_head("mine", "origin/release")
    mine.set_tracking_branch(clone.remotes.origin.refs.release)
    mine.checkout()
    ahead = _commit(clone, "mine")
    # never pushed
    clone.create_head("local-only", base)
    clone.heads.main.checkout()
    return upstream, clone, base, ahead


def test_compare_with_remotes(work_clone):
    upstream, clone, base, ahead = work_clone
    compared = compare_with_remotes(GitRepo(clone))
    assert list(compared) == [get_repository_url(upstream.working_dir)]
    # main matches origin/main, so it's left out
    assert compared[get_repository_url(upstream.working_dir)].branches == {
        "mine": UpstreamRef(
            "ahead",
            ref="release",
            commit=base,
            local_commit=ahead,
            merge_base=base,
            ahead=1,
            behind=0,
        ),
        "local-only": UpstreamRef("missing", local_commit=base),
    }


def test_another_remote_is_compared_by_branch_name(work_clone, tmp_path):
    upstream, clone, base, ahead = work_clone
    other = git.Repo.clone_from(upstream.working_dir, tmp_path / "other")
    clone.create_remote("other", other.working_dir).fetch()
    compared = compare_with_remotes(GitRepo(clone))[get_repository_url(other.working_dir)]
    # mine tracks origin, and other has no branch named mine
    assert compared.branches == {"local-only": UpstreamRef("missing", local_commit=base)}


def test_a_local_repository_record(work_clone, tmp_path):
    upstream, clone, base, ahead = work_clone
    record = local_repository(GitRepo(clone), str(tmp_path / "repos"))
    assert record.url == "git://" + clone.working_dir
    assert record.path == "work"
    assert record.git_url() == "file://" + clone.working_dir
    assert record.branches == {"main": base, "mine": ahead, "local-only": base}
    # uncommitted changes mark the checked-out branch
    Path(clone.working_dir, "base").write_text("changed")
    record = local_repository(GitRepo(clone), str(tmp_path / "repos"))
    assert record.branches["main"] == base + "-dirty"


def _directory(repos: Dict[str, object]) -> Mock:
    """A stand-in Directory with ``repos``."""
    directory = Mock(repos=repos, repos_root="")
    directory.store.get_repository.return_value = None  # remotes aren't recorded
    directory.cloudmap.local_env = None  # in no project
    directory.cloudmap.db_dir = None
    directory.cloudmap.backend = "git-sync"
    return directory


def test_save_local_repositories(work_clone, tmp_path):
    upstream, clone, base, ahead = work_clone
    clone.delete_head("mine", "local-only", force=True)
    path = str(tmp_path / "local-cloudmap.yaml")
    directory = _directory({clone.working_dir: GitRepo(clone)})
    key = get_repository_url(clone.working_dir)

    def logged() -> str:
        (message, *args), _ = directory.logger.verbose.call_args
        return message % tuple(args)

    # in step with its remote and clean: not recorded
    Directory.save_local_repositories(directory, path)
    assert key not in CloudMapDB(path).repositories
    assert logged().startswith("skipping local clone")

    Path(clone.working_dir, "base").write_text("changed")
    Directory.save_local_repositories(directory, path)
    assert CloudMapDB(path).repositories[key].branches["main"] == base + "-dirty"
    assert logged().endswith("it has uncommitted changes")

    # committed and pushed: its record is removed
    upstream.heads.release.checkout()  # so it accepts a push to main
    clone.git.commit("-am", "changed")
    clone.remotes.origin.push("main")
    clone.remotes.origin.fetch()
    Directory.save_local_repositories(directory, path)
    assert key not in CloudMapDB(path).repositories
    assert logged().startswith("removing local clone")


def test_compare_forks_saves_the_local_cloudmap():
    directory = Mock(_forks=set(), local_cloudmap="local.yaml")
    Directory.compare_forks(directory)
    directory.save_local_repositories.assert_called_once_with("local.yaml")
    directory = Mock(_forks=set(), local_cloudmap="")
    Directory.compare_forks(directory)
    directory.save_local_repositories.assert_not_called()


def test_a_failure_saving_local_clones_doesnt_sink_the_import():
    directory = Mock(_forks=set(), local_cloudmap="local.yaml")
    directory.save_local_repositories.side_effect = OSError("read-only")
    Directory.compare_forks(directory)
    directory.logger.warning.assert_called_once()


def test_a_dirty_branch_with_no_commits_yet(tmp_path):
    repo = git.Repo.init(tmp_path / "new", initial_branch="main")
    Path(repo.working_dir, "a").write_text("a")
    repo.index.add(["a"])
    record = local_repository(GitRepo(repo))
    assert record.branches == {}


def test_a_clone_that_cant_be_recorded_is_skipped(work_clone, tmp_path):
    upstream, clone, base, ahead = work_clone
    path = str(tmp_path / "local-cloudmap.yaml")
    broken = Mock(working_dir=str(tmp_path / "broken"))
    directory = _directory({"broken": broken, "work": GitRepo(clone)})
    Directory.save_local_repositories(directory, path)
    assert list(CloudMapDB(path).repositories) == [get_repository_url(clone.working_dir)]
    directory.logger.warning.assert_called_once()


def test_a_branch_tracking_a_local_branch_counts_as_untracked(work_clone):
    upstream, clone, base, ahead = work_clone
    clone.git.branch("--track", "feature", "main")
    clone.heads.feature.checkout()
    _commit(clone, "feature")
    branches = compare_with_remotes(GitRepo(clone))[
        get_repository_url(upstream.working_dir)
    ].branches
    assert branches["feature"].status == "missing"


def test_the_record_of_a_deleted_clone_is_removed(work_clone, tmp_path):
    upstream, clone, base, ahead = work_clone
    path = str(tmp_path / "local-cloudmap.yaml")
    directory = _directory({"work": GitRepo(clone)})
    Directory.save_local_repositories(directory, path)
    key = get_repository_url(clone.working_dir)
    assert key in CloudMapDB(path).repositories
    shutil.rmtree(clone.working_dir)
    directory.repos = {}
    Directory.save_local_repositories(directory, path)
    assert key not in CloudMapDB(path).repositories


def test_local_clones_are_only_written_when_they_change(work_clone, tmp_path):
    upstream, clone, base, ahead = work_clone
    path = tmp_path / "local-cloudmap.yaml"
    directory = _directory({"work": GitRepo(clone)})
    Directory.save_local_repositories(directory, str(path))
    path.write_text(path.read_text() + "# untouched\n")
    Directory.save_local_repositories(directory, str(path))
    assert path.read_text().endswith("# untouched\n")


def test_an_upstream_every_ref_matches_has_an_empty_entry(repos):
    fork, upstream, record, *_ = repos
    record.branches = {"main": record.branches["main"]}
    record.tags = {}
    refs = compare_with_upstream(record, _upstream_record(upstream), None, None, MAX_REFS)
    record.upstream_refs = {UPSTREAM: refs}
    assert record.asdict()["upstream_refs"] == {UPSTREAM: {}}


def _local_cloudmap_project(tmp_path: Path, configured: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "unfurl.yaml").write_text(
        f"""apiVersion: {API_VERSION}
kind: Project
environments:
  defaults:
    cloudmaps:
      repositories:
        cloudmap:
          url: {tmp_path / "cloudmap.yaml"}
          clone_root: {tmp_path / "repos"}
          local_cloudmap: {configured}
"""
    )
    return project


def test_the_cli_saves_local_clones(work_clone, tmp_path):
    upstream, clone, base, ahead = work_clone
    configured = tmp_path / "configured.yaml"
    project = _local_cloudmap_project(tmp_path, configured)
    key = get_repository_url(clone.working_dir)
    runner = CliRunner()
    given = tmp_path / "given.yaml"
    for args, path in (([str(given)], given), ([], configured)):
        result = runner.invoke(
            cli,
            ["cloudmap", "--project", str(project), "--local-cloudmap", *args],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert key in CloudMapDB(str(path)).repositories


def test_the_cli_uses_the_path_for_an_import(work_clone, tmp_path):
    upstream, clone, base, ahead = work_clone
    configured = tmp_path / "configured.yaml"
    project = _local_cloudmap_project(tmp_path, configured)
    given = tmp_path / "given.yaml"
    result = CliRunner().invoke(
        cli,
        [
            "cloudmap",
            "--project",
            str(project),
            "--add",
            upstream.working_dir,
            "--local-cloudmap",
            str(given),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    # saved by the import, in place of the configured file
    assert get_repository_url(clone.working_dir) in CloudMapDB(str(given)).repositories
    assert not configured.exists()
    # a record with no path loads
    assert CloudMapDB(str(tmp_path / "cloudmap.yaml")).repositories


@pytest.fixture
def several_remotes(tmp_path):
    """A clone up to date with origin, with two other remotes for the same
    repository: one last fetched long ago, one never fetched."""
    upstream = git.Repo.init(tmp_path / "remote", initial_branch="main")
    base = _commit(upstream, "base")
    clone = git.Repo.clone_from(upstream.working_dir, tmp_path / "work")
    first = _commit(upstream, "first")
    second = _commit(upstream, "second")
    clone.remotes.origin.fetch()
    clone.git.merge("--ff-only", "origin/main")
    clone.create_remote("stale", upstream.working_dir)
    clone.git.update_ref("refs/remotes/stale/main", base)
    clone.create_remote("canonical", upstream.working_dir)
    return upstream, clone, base, first, second


def test_remotes_for_the_same_repository_are_compared_as_one(several_remotes):
    upstream, clone, base, first, second = several_remotes
    # main is where origin has it: stale's older main and canonical's lack of
    # one don't make it ahead or missing, so the repository's entry is empty
    assert compare_with_remotes(GitRepo(clone)) == {
        get_repository_url(upstream.working_dir): UpstreamRefs()
    }


def test_the_nearest_remote_is_compared_with(several_remotes):
    upstream, clone, base, first, second = several_remotes
    clone.create_head("topic", second)  # tracks nothing
    clone.git.update_ref("refs/remotes/origin/topic", first)
    clone.git.update_ref("refs/remotes/stale/topic", base)
    branches = compare_with_remotes(GitRepo(clone))[
        get_repository_url(upstream.working_dir)
    ].branches
    assert branches == {
        "topic": UpstreamRef(
            "ahead",
            commit=first,
            local_commit=second,
            merge_base=first,
            ahead=1,
            behind=0,
        )
    }


def test_the_tracking_remote_is_compared_with_even_if_another_is_nearer(
    several_remotes,
):
    upstream, clone, base, first, second = several_remotes
    clone.git.update_ref("refs/remotes/origin/main", base)
    clone.git.update_ref("refs/remotes/stale/main", first)
    branches = compare_with_remotes(GitRepo(clone))[
        get_repository_url(upstream.working_dir)
    ].branches
    # main tracks origin
    assert branches["main"].commit == base
    assert branches["main"].ahead == 2


def test_recorded_clones_are_checked_again(work_clone, tmp_path):
    """Wherever they are, even if this directory didn't load them."""
    upstream, clone, base, ahead = work_clone
    path = str(tmp_path / "local-cloudmap.yaml")
    key = get_repository_url(clone.working_dir)
    loaded = _directory({"work": GitRepo(clone)})
    loaded.repos_root = str(tmp_path / "repos")
    Directory.save_local_repositories(loaded, path)
    assert CloudMapDB(path).repositories[key].path == "work"

    # still differs: kept, with its path
    elsewhere = _directory({})
    Directory.save_local_repositories(elsewhere, path)
    assert CloudMapDB(path).repositories[key].path == "work"

    # pushed and in step: removed
    clone.delete_head("local-only", force=True)
    clone.remotes.origin.push("mine:release")
    clone.remotes.origin.fetch()
    Directory.save_local_repositories(elsewhere, path)
    assert key not in CloudMapDB(path).repositories


def test_tags_are_compared_with_the_remotes_record(work_clone):
    upstream, clone, base, ahead = work_clone
    url = get_repository_url(upstream.working_dir)
    clone.create_tag("pushed", base)
    clone.create_tag("unpushed", base)
    clone.create_tag("moved", ahead)
    remote = Repository(url=url, tags={"pushed": base, "moved": base})
    lookup = {url: remote}.get
    tags = compare_with_remotes(GitRepo(clone), lookup)[url].tags
    assert tags == {
        "unpushed": UpstreamRef("missing", local_commit=base),
        "moved": UpstreamRef(
            "ahead",
            commit=base,
            local_commit=ahead,
            merge_base=base,
            ahead=1,
            behind=0,
        ),
    }
    record = local_repository(GitRepo(clone), remote_record=lookup)
    assert record.tags == {"pushed": base, "unpushed": base, "moved": ahead}
    # a cut-off tag list can't say a tag isn't there
    tags = compare_with_remotes(GitRepo(clone), lookup, max_refs=2)[url].tags
    assert tags["unpushed"].status == "unknown"


def test_tags_arent_compared_without_a_record(work_clone):
    upstream, clone, base, ahead = work_clone
    clone.create_tag("unpushed", base)
    compared = compare_with_remotes(GitRepo(clone), {}.get)
    assert not compared[get_repository_url(upstream.working_dir)].tags


def test_saving_compares_tags_with_the_cloudmaps_records(work_clone, tmp_path):
    upstream, clone, base, ahead = work_clone
    clone.delete_head("mine", "local-only", force=True)
    clone.create_tag("unpushed", base)
    url = get_repository_url(upstream.working_dir)
    store = CloudMapDB("", contents={}, validate=False)
    store.add_record(Repository(url=url, tags={}))
    directory = _directory({"work": GitRepo(clone)})
    directory.store = store
    path = str(tmp_path / "local-cloudmap.yaml")
    Directory.save_local_repositories(directory, path)
    record = CloudMapDB(path).repositories[get_repository_url(clone.working_dir)]
    assert record.upstream_refs[url].tags["unpushed"].status == "missing"


@pytest.fixture
def stale_clone(tmp_path):
    """A clone last fetched before the upstream's main moved on twice."""
    upstream = git.Repo.init(tmp_path / "remote", initial_branch="main")
    base = _commit(upstream, "base")
    clone = git.Repo.clone_from(upstream.working_dir, tmp_path / "work")
    first = _commit(upstream, "first")
    second = _commit(upstream, "second")
    url = get_repository_url(upstream.working_dir)
    return clone, url, base, first, second


def _main_against(clone: git.Repo, url: str, recorded_main: str) -> UpstreamRef:
    record = Repository(url=url, branches={"main": recorded_main})
    return compare_with_remotes(GitRepo(clone), {url: record}.get)[url].branches["main"]


def test_a_branch_never_fetched_is_compared_with_its_record(stale_clone):
    clone, url, base, first, second = stale_clone
    # origin/main is where main is, but the record knows better
    assert _main_against(clone, url, second) == UpstreamRef(
        "differs", commit=second, local_commit=base
    )


def test_a_newer_recorded_commit_the_clone_has_is_counted(stale_clone):
    clone, url, base, first, second = stale_clone
    clone.remotes.origin.fetch()
    clone.git.update_ref("refs/remotes/origin/main", base)
    ref = _main_against(clone, url, second)
    assert (ref.status, ref.commit, ref.behind) == ("behind", second, 2)


def test_a_fetch_newer_than_the_record_wins(stale_clone):
    clone, url, base, first, second = stale_clone
    clone.remotes.origin.fetch()
    ref = _main_against(clone, url, first)
    assert (ref.status, ref.commit, ref.behind) == ("behind", second, 2)


def test_a_remote_never_fetched_and_not_recorded_isnt_compared(work_clone):
    upstream, clone, base, ahead = work_clone
    clone.create_remote("typo", "https://aging.example.com/project.git")
    compared = compare_with_remotes(GitRepo(clone), {}.get)
    assert "git://aging.example.com/project.git" not in compared
    # with a record, it is
    url = "git://aging.example.com/project.git"
    record = Repository(url=url, branches={"main": base})
    assert url in compare_with_remotes(GitRepo(clone), {url: record}.get)


def test_a_sibling_clones_commits_are_counted(stale_clone, tmp_path):
    clone, url, base, first, second = stale_clone
    sibling = git.Repo.clone_from(clone.remotes.origin.url, tmp_path / "sibling")
    record = Repository(url=url, branches={"main": second})
    lookup = {url: record}.get
    alone = compare_with_remotes(GitRepo(clone), lookup)[url].branches["main"]
    assert alone.status == "differs"
    together = compare_with_remotes(GitRepo(clone), lookup, siblings=[GitRepo(sibling)])
    main = together[url].branches["main"]
    assert (main.status, main.commit, main.behind) == ("behind", second, 2)
    # read, not copied
    assert not _has_commit(clone, second)

    # saving finds the sibling
    store = CloudMapDB("", contents={}, validate=False)
    store.add_record(record)
    directory = _directory({"a": GitRepo(clone), "b": GitRepo(sibling)})
    directory.store = store
    path = str(tmp_path / "local-cloudmap.yaml")
    Directory.save_local_repositories(directory, path)
    saved = CloudMapDB(path).repositories[get_repository_url(clone.working_dir)]
    assert saved.upstream_refs[url].branches["main"].behind == 2


def test_the_cli_saves_local_clones_before_an_export(work_clone, tmp_path):
    from unfurl.cloudmap import CloudMap

    upstream, clone, base, ahead = work_clone
    project = _local_cloudmap_project(tmp_path, tmp_path / "configured.yaml")
    given = tmp_path / "given.yaml"
    with patch.object(CloudMap, "get_host", return_value=Mock(host_branch="")), patch.object(
        CloudMap, "to_host", return_value=False
    ) as to_host:
        result = CliRunner().invoke(
            cli,
            [
                "cloudmap",
                "--project",
                str(project),
                "--export",
                "somehost",
                "--local-cloudmap",
                str(given),
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0, result.output
    assert get_repository_url(clone.working_dir) in CloudMapDB(str(given)).repositories
    to_host.assert_called_once()


def test_the_local_cloudmap_loads_through_git_sync(work_clone, tmp_path, monkeypatch):
    """Created by the first save, changed by the second, loaded through
    git-sync each time."""
    import unfurl._rust

    if not hasattr(unfurl._rust, "GitSyncDB"):
        pytest.skip("built without the cloudmap-sync feature")
    import unfurl.cloudmap.gitsync_db as module

    opened: List[CloudMapDB] = []
    real = module.open_cloudmap_db

    def spy(
        filepath: str,
        project_root: Optional[str],
        logger: UnfurlLogger,
        *,
        db_dir: Optional[str] = None,
        backend: Backend = "git-sync",
    ) -> CloudMapDB:
        db = real(filepath, project_root, logger, db_dir=db_dir, backend=backend)
        opened.append(db)
        return db

    monkeypatch.setattr(module, "open_cloudmap_db", spy)
    upstream, clone, base, ahead = work_clone
    path = tmp_path / "not-yet" / "local-cloudmap.yaml"
    directory = _directory({clone.working_dir: GitRepo(clone)})
    key = get_repository_url(clone.working_dir)

    Path(clone.working_dir, "base").write_text("changed")
    Directory.save_local_repositories(directory, str(path))
    assert isinstance(opened[-1], module.GitSyncCloudMapDB)
    assert CloudMapDB(str(path)).repositories[key].branches["main"] == base + "-dirty"

    clone.git.commit("-am", "changed")
    Directory.save_local_repositories(directory, str(path))
    assert isinstance(opened[-1], module.GitSyncCloudMapDB)
    assert CloudMapDB(str(path)).repositories[key].branches["main"] == clone.head.commit.hexsha
