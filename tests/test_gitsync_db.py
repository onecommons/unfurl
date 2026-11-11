"""The git-sync binding for cloudmaps, ``unfurl._rust.GitSyncDB``."""

import json
from pathlib import Path
from urllib.parse import quote

import git
import pytest

_rust = pytest.importorskip("unfurl._rust")
if not hasattr(_rust, "GitSyncDB"):
    pytest.skip("built without the cloudmap-sync feature", allow_module_level=True)

from unfurl._rust import GitSyncDB, GitSyncError  # noqa: E402

CLOUDMAP = "cloudmap.yaml"
KEPT = "git://example.com/kept.git"
ADDED = "git://example.com/added.git"


@pytest.fixture
def repo(tmp_path: Path) -> git.Repo:
    repo = git.Repo.init(tmp_path / "repo", initial_branch="main")
    (Path(repo.working_dir) / CLOUDMAP).write_text(
        f"apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  {KEPT}:\n    path: kept\n"
    )
    repo.index.add([CLOUDMAP])
    repo.index.commit("initial")
    return repo


def _url(path: Path) -> str:
    return "sqlite://" + quote(str(path)) + "?mode=rwc"


def _open(repo: git.Repo, tmp_path: Path) -> GitSyncDB:
    return GitSyncDB.open(repo.working_dir, _url(tmp_path / ".cloudmap.sqlite"), CLOUDMAP)


def test_read_write_save_and_commit(repo: git.Repo, tmp_path: Path) -> None:
    db = _open(repo, tmp_path)
    outcome = db.scan()
    assert outcome["files_seen"] == 1, outcome
    assert [(s, k, json.loads(j)) for s, k, j in db.find("/repositories")] == [
        ("/repositories", KEPT, {"path": "kept"})
    ]

    db.upsert("/repositories", ADDED, json.dumps({"path": "added"}))
    db.delete("/repositories", KEPT)
    # reads see the pending changes
    assert [k for _, k, _ in db.find("/repositories")] == [ADDED]
    assert db.find("/repositories", key=KEPT) == []

    saved = db.save()
    assert saved["conflicts"] == [] and saved["failed"] == [], saved
    text = (Path(repo.working_dir) / CLOUDMAP).read_text()
    assert ADDED in text and KEPT not in text

    commit = db.commit("update cloudmap")
    assert commit == repo.head.commit.hexsha
    assert repo.head.commit.message.startswith("update cloudmap\n")
    # nothing left to commit
    assert db.commit("again") is None

    # a new handle on the same database agrees
    reopened = _open(repo, tmp_path)
    assert reopened.scan()["records_upserted"] == 0
    assert [k for _, k, _ in reopened.find("/repositories")] == [ADDED]


def test_a_hand_commit_of_saved_changes_is_taken_in(repo: git.Repo, tmp_path: Path) -> None:
    db = _open(repo, tmp_path)
    db.scan()
    db.upsert("/repositories", ADDED, json.dumps({"path": "added"}))
    db.save()
    repo.index.add([CLOUDMAP])
    repo.index.commit("by hand")
    assert db.commit("update cloudmap") is None
    assert repo.head.commit.message == "by hand"


def test_type_filters(repo: git.Repo, tmp_path: Path) -> None:
    db = _open(repo, tmp_path)
    db.scan()
    db.upsert(
        "/artifacts",
        "git://example.com/a.git#:t.yaml",
        json.dumps({"type": {"cloudmap.artifacts.tosca.ServiceTemplate": None}}),
    )
    found = db.find("/artifacts", type_names=["cloudmap.artifacts.tosca.ServiceTemplate"])
    assert [k for _, k, _ in found] == ["git://example.com/a.git#:t.yaml"]
    assert db.find("/artifacts", type_names=["no.such.Type"]) == []


def test_errors(repo: git.Repo, tmp_path: Path) -> None:
    db = _open(repo, tmp_path)
    db.scan()
    with pytest.raises(ValueError):
        db.upsert("/repositories", ADDED, "{not json")
    # a pathspec outside the working tree
    with pytest.raises(GitSyncError):
        GitSyncDB.open(repo.working_dir, _url(tmp_path / "other.sqlite"), "../outside.yaml")
    with pytest.raises(ValueError):
        GitSyncDB.open(repo.working_dir, "postgres://localhost/db", CLOUDMAP)


def test_a_database_path_with_url_characters(repo: git.Repo, tmp_path: Path) -> None:
    odd = tmp_path / "a #dir? 100%"
    odd.mkdir()
    db = GitSyncDB.open(repo.working_dir, _url(odd / ".cloudmap.sqlite"), CLOUDMAP)
    db.scan()
    # the database is where the path says, not at a truncated one
    assert (odd / ".cloudmap.sqlite").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([odd.name, "repo"])
