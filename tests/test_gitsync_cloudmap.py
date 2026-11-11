"""``GitSyncCloudMapDB`` against ``CloudMapDB``: the same cloudmap loads into the
same records, and the same changes save to the same document."""

import shutil
from pathlib import Path
from typing import Any, Dict

import git
import pytest
from ruamel.yaml import YAML

from unfurl.cloudmap.db import CloudMapDB
from unfurl.repo import GitRepo
from unfurl.tosca_plugins.cloudmap_defs import Repository

_rust = pytest.importorskip("unfurl._rust")
if not hasattr(_rust, "GitSyncDB"):
    pytest.skip("built without the cloudmap-sync feature", allow_module_level=True)

from unfurl.cloudmap.gitsync_db import SECTIONS, GitSyncCloudMapDB  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "expected_cloudmap.yaml"
FILE = "cloudmap.yaml"


def _repo(path: Path) -> Path:
    path.mkdir()
    shutil.copy(FIXTURE, path / FILE)
    repo = git.Repo.init(path, initial_branch="main")
    repo.index.add([FILE])
    repo.index.commit("initial")
    return path


def _open(tmp_path: Path) -> GitSyncCloudMapDB:
    return GitSyncCloudMapDB(
        str(tmp_path / "git_sync"), FILE, str(tmp_path / ".cloudmap.sqlite")
    )


@pytest.fixture
def both(tmp_path: Path):
    """The fixture cloudmap in two repositories, one through each class."""
    python = CloudMapDB(str(_repo(tmp_path / "python") / FILE))
    _repo(tmp_path / "git_sync")
    return python, _open(tmp_path)


def _records(db: CloudMapDB) -> Dict[str, Dict[str, Any]]:
    return {
        section: {
            key: record.asdict() if hasattr(record, "asdict") else record
            for key, record in getattr(db, section).items()
        }
        for section in SECTIONS
    }


def _document(path: str) -> Dict[str, Any]:
    """The saved document, without empty sections: CloudMapDB writes an empty
    `repositories`, git-sync doesn't add one."""
    document = YAML(typ="safe").load(Path(path).read_text())
    return {k: v for k, v in document.items() if v != {}}


def test_the_same_records_load(both) -> None:
    python, git_sync = both
    assert _records(git_sync) == _records(python)
    assert git_sync.metadata == python.metadata


def _change(db: CloudMapDB) -> None:
    db.add_record(Repository(url="git://example.com/new/repo.git", path="new/repo"))
    artifact = next(a for a in db.artifacts.values() if not a._parent)
    artifact.metadata.description = "changed"
    db.add_record(artifact)
    db.delete_record(next(iter(db.repositories.values())))


def test_the_same_changes_save_the_same_document(both) -> None:
    python, git_sync = both
    _change(python)
    _change(git_sync)
    assert python.save() and git_sync.save()
    assert _document(git_sync.file) == _document(python.file)
    # nothing more to save
    assert not git_sync.save()


def test_an_unchanged_load_saves_nothing(both) -> None:
    _, git_sync = both
    before = Path(git_sync.file).read_bytes()
    assert not git_sync.save()
    assert Path(git_sync.file).read_bytes() == before


def test_saved_changes_commit_and_reload(both, tmp_path: Path) -> None:
    _, git_sync = both
    git_sync.add_record(Repository(url="git://example.com/x.git", path="x"))
    git_sync.save()
    repo = git.Repo(Path(git_sync.file).parent)
    assert git_sync.commit(GitRepo(repo), "add x")
    assert repo.head.commit.message.startswith("add x\n")
    git_sync.reload()
    assert git_sync.get_repository("git://example.com/x.git")
    # a new handle on the same database agrees
    assert _records(_open(tmp_path)) == _records(git_sync)


def test_metadata_round_trips(both, tmp_path: Path) -> None:
    python, git_sync = both
    for db in both:
        db.metadata["title"] = "A cloudmap"
        db.save()
    assert _document(git_sync.file)["metadata"] == _document(python.file)["metadata"]
    assert _open(tmp_path).metadata["title"] == "A cloudmap"


VERSIONED = """apiVersion: unfurl/v1.0.0
kind: CloudMap
artifacts:
  pkg:npm/example:
    digest: sha256:0
    versions:
      "@1.0":
        digest: sha256:1
components:
  label@example.com:
    status: production
    versions:
      v1:
        status: beta
"""


def test_versions_save_inside_their_parent(tmp_path: Path) -> None:
    for name in ("python", "git_sync"):
        path = tmp_path / name
        path.mkdir()
        (path / FILE).write_text(VERSIONED)
        repo = git.Repo.init(path, initial_branch="main")
        repo.index.add([FILE])
        repo.index.commit("initial")
    dbs = [CloudMapDB(str(tmp_path / "python" / FILE)), _open(tmp_path)]
    python, git_sync = dbs
    assert _records(git_sync) == _records(python)
    for db in dbs:
        version = db.artifacts["pkg:npm/example@1.0"]
        assert version._parent
        version.digest = "sha256:2"
        db.add_record(version)
        db.save()
    document = _document(git_sync.file)
    assert document == _document(python.file)
    assert document["artifacts"]["pkg:npm/example"]["versions"]["@1.0"] == {
        "digest": "sha256:2"
    }
    assert list(document["artifacts"]) == ["pkg:npm/example"]
