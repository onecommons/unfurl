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


def _edit_save_rescan(db: GitSyncDB, path: Path) -> None:
    """A hand edit is taken in without a conflict; an edit saves, and a
    rescan finds nothing new."""
    assert db.scan()["records_upserted"] == 1
    path.write_text(path.read_text().replace("path: kept", "path: hand-edited"))
    scanned = db.scan()
    assert scanned["conflicts"] == [], scanned
    assert [json.loads(j) for _, _, j in db.find("/repositories")] == [{"path": "hand-edited"}]
    db.upsert("/repositories", ADDED, json.dumps({"path": "added"}))
    saved = db.save()
    assert saved["conflicts"] == [] and saved["failed"] == [], saved
    assert ADDED in path.read_text()
    assert db.save()["written"] == []
    rescanned = db.scan()
    assert rescanned["conflicts"] == [] and rescanned["records_upserted"] == 0, rescanned


@pytest.mark.parametrize("case", ["staged", "untracked"])
def test_an_uncommitted_cloudmap(tmp_path: Path, case: str) -> None:
    repo = git.Repo.init(tmp_path / "repo", initial_branch="main")
    path = Path(repo.working_dir) / CLOUDMAP
    path.write_text(
        f"apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  {KEPT}:\n    path: kept\n"
    )
    if case == "staged":
        repo.index.add([CLOUDMAP])
    db = _open(repo, tmp_path)
    _edit_save_rescan(db, path)
    commit = db.commit("add the cloudmap")
    assert commit == repo.head.commit.hexsha
    assert CLOUDMAP in [b.path for b in repo.head.commit.tree.blobs]


def test_a_cloudmap_in_no_repository(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    (root / "sub").mkdir(parents=True)
    path = root / "sub" / CLOUDMAP
    path.write_text(
        f"apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  {KEPT}:\n    path: kept\n"
    )
    db = GitSyncDB.open(str(root), _url(tmp_path / ".cloudmap.sqlite"), f"sub/{CLOUDMAP}")
    _edit_save_rescan(db, path)
    assert db.commit("nothing to commit to") is None
    assert not (root / ".git").exists()


def test_the_file_name_is_literal(tmp_path: Path) -> None:
    """A name with glob characters names only that file, and is taken in
    though ``.gitignore`` excludes it."""
    root = tmp_path / "plain"
    root.mkdir()
    text = "apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  {}:\n    path: p\n"
    (root / "cloud[1].yaml").write_text(text.format(KEPT))
    (root / "cloud1.yaml").write_text(text.format(ADDED))
    (root / ".gitignore").write_text("cloud*.yaml\n")
    db = GitSyncDB.open(str(root), _url(tmp_path / ".cloudmap.sqlite"), "cloud[1].yaml")
    outcome = db.scan()
    assert outcome["unparsed"] == [] and outcome["failed"] == [], outcome
    assert [k for _, k, _ in db.find("/repositories")] == [KEPT]


def test_resolve_takes_the_files_value(repo: git.Repo, tmp_path: Path) -> None:
    """A conflict between a saved, uncommitted change and a hand edit settles
    with the file's value."""
    db = _open(repo, tmp_path)
    db.scan()
    db.upsert("/repositories", KEPT, json.dumps({"path": "saved"}))
    assert db.save()["conflicts"] == []
    path = Path(repo.working_dir) / CLOUDMAP
    path.write_text(path.read_text().replace("path: saved", "path: by-hand"))
    assert db.scan()["conflicts"] == [(CLOUDMAP, "/repositories", KEPT)]

    db.resolve("/repositories", KEPT)
    assert [json.loads(j) for _, _, j in db.find("/repositories")] == [{"path": "by-hand"}]
    saved = db.save()
    assert saved["conflicts"] == [] and saved["written"] == [], saved
    assert db.scan()["conflicts"] == []
    with pytest.raises(GitSyncError):
        db.resolve("/repositories", KEPT)


def test_sql_statements_arent_logged(repo: git.Repo, tmp_path: Path, caplog) -> None:
    """sqlx logs every statement at debug; only its warnings reach Python."""
    import logging

    with caplog.at_level(logging.DEBUG):
        db = _open(repo, tmp_path)
        db.scan()
        db.find("/repositories")
    names = {record.name for record in caplog.records}
    assert not [name for name in names if name.startswith("sqlx")]
