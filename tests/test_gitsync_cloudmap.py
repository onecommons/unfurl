"""``GitSyncCloudMapDB`` against ``CloudMapDB``: the same cloudmap loads into the
same records, and the same changes save to the same document."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

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


from unfurl.cloudmap.gitsync_db import open_cloudmap_db  # noqa: E402
from unfurl.logs import getLogger  # noqa: E402
from unfurl.util import UnfurlError  # noqa: E402

LOGGER = getLogger("unfurl")


def _tracked(tmp_path: Path, name: str = FILE, text: str = VERSIONED) -> GitRepo:
    root = tmp_path / "repo"
    root.mkdir()
    (root / name).write_text(text)
    repo = git.Repo.init(root, initial_branch="main")
    repo.index.add([name])
    repo.index.commit("initial")
    return GitRepo(repo)


def test_a_tracked_cloudmap_uses_git_sync(tmp_path: Path) -> None:
    repo = _tracked(tmp_path)
    project = tmp_path / "project"
    db = open_cloudmap_db(str(tmp_path / "repo" / FILE), str(project), LOGGER)
    assert isinstance(db, GitSyncCloudMapDB)
    # with SQLite's -wal and -shm beside it
    [made] = [p for p in (project / "local").iterdir() if p.suffix == ".sqlite"]
    assert made.name.startswith(".cloudmap-")
    # without a project: local/ beside the cloudmap
    db = open_cloudmap_db(str(tmp_path / "repo" / FILE), None, LOGGER)
    assert isinstance(db, GitSyncCloudMapDB)
    assert (tmp_path / "repo" / "local" / ".cloudmap.yaml.sqlite").exists()


@pytest.mark.parametrize("case", ["environment", "no extension", "notable"])
def test_falls_back_to_cloudmap_db(tmp_path: Path, monkeypatch, case: str) -> None:
    import unfurl._rust

    repo = _tracked(tmp_path)
    path = tmp_path / "repo" / FILE
    if case == "environment":
        monkeypatch.setenv("UNFURL_CLOUDMAP_BACKEND", "python")
    elif case == "no extension":
        monkeypatch.delattr(unfurl._rust, "GitSyncDB")
    elif case == "notable":
        path.write_text(
            VERSIONED + "repositories:\n  git://e.com/r.git:\n    path: r\n    notable: {}\n"
        )
    db = open_cloudmap_db(str(path), None, LOGGER)
    assert type(db) is CloudMapDB, case


def test_a_git_sync_failure_raises(tmp_path: Path, monkeypatch) -> None:
    """With its database already there, a failure raises."""
    import unfurl.cloudmap.gitsync_db as module

    def broken(*args: Any) -> None:
        raise RuntimeError("broken")

    _tracked(tmp_path)
    path = str(tmp_path / "repo" / FILE)
    assert isinstance(open_cloudmap_db(path, None, LOGGER), GitSyncCloudMapDB)
    monkeypatch.setattr(module, "GitSyncCloudMapDB", broken)
    with pytest.raises(UnfurlError, match="broken"):
        open_cloudmap_db(path, None, LOGGER)


def test_a_failure_with_a_new_database_falls_back(tmp_path: Path, monkeypatch, caplog) -> None:
    """A load that fails after creating its database leaves nothing behind
    and loads with CloudMapDB, with a warning."""

    def broken(self: GitSyncCloudMapDB) -> None:
        raise RuntimeError("broken")

    monkeypatch.setattr(GitSyncCloudMapDB, "_load_records", broken)
    _tracked(tmp_path)
    path = tmp_path / "repo" / FILE
    before = sorted(p.name for p in path.parent.iterdir())
    db = open_cloudmap_db(str(path), None, LOGGER)
    assert type(db) is CloudMapDB
    assert db.artifacts
    assert sorted(p.name for p in path.parent.iterdir()) == before
    [warning] = [r for r in caplog.records if r.levelname == "WARNING"]
    message = warning.getMessage()
    assert str(path) in message and ".cloudmap.yaml.sqlite" in message and "broken" in message


def _edit_and_reload(db: CloudMapDB, path: Path) -> None:
    """Add a repository, save it, and find it in the file and in a reload."""
    db.add_record(Repository(url="git://e.com/added.git", path="added"))
    assert db.save()
    assert "e.com/added.git" in path.read_text()
    db.reload()
    assert db.get_repository("git://e.com/added.git")


@pytest.mark.parametrize("case", ["staged", "untracked", "no repository", "project"])
def test_an_uncommitted_cloudmap_uses_git_sync(tmp_path: Path, case: str) -> None:
    """``case`` names where the file is: staged, untracked, or in no
    repository, alone or in a project."""
    path = tmp_path / "plain" / "sub" / FILE
    repo: Optional[GitRepo] = None
    project = None
    if case in ("staged", "untracked"):
        repo = _tracked(tmp_path, "other.yaml")
        path = tmp_path / "repo" / "sub" / FILE
    path.parent.mkdir(parents=True)
    path.write_text(VERSIONED)
    if case == "staged":
        assert repo
        repo.repo.index.add([str(path.relative_to(tmp_path / "repo"))])
    elif case == "project":
        project = str(tmp_path / "plain")
    db = open_cloudmap_db(str(path), project, LOGGER)
    assert isinstance(db, GitSyncCloudMapDB), case
    _edit_and_reload(db, path)
    if repo:
        assert db.commit(repo, "commit"), case
    else:
        assert isinstance(db, GitSyncCloudMapDB)
        assert db._git_sync.commit("nothing to commit to") is None, case


def test_a_cloudmap_only_git_sync_reads_has_no_fallback(tmp_path: Path, monkeypatch) -> None:
    import unfurl._rust

    repo = _tracked(tmp_path, "cloudmap.md", "# a literate cloudmap\n")
    monkeypatch.delattr(unfurl._rust, "GitSyncDB")
    with pytest.raises(UnfurlError, match="cloudmap-sync"):
        open_cloudmap_db(str(tmp_path / "repo" / "cloudmap.md"), None, LOGGER)


def test_the_root_git_sync_opens(tmp_path: Path) -> None:
    from unfurl.cloudmap.gitsync_db import _root

    real = Path(os.path.realpath(tmp_path))
    repo = _tracked(tmp_path)
    (tmp_path / "repo" / "sub").mkdir()
    in_repo = str(tmp_path / "repo" / "sub" / FILE)
    assert _root(in_repo, str(tmp_path / "repo" / "sub")) == os.path.realpath(repo.working_dir)
    project = tmp_path / "project"
    (project / "maps").mkdir(parents=True)
    in_project = str(project / "maps" / FILE)
    assert _root(in_project, str(project)) == str(real / "project")
    assert _root(in_project, str(tmp_path / "elsewhere")) == str(real / "project" / "maps")
    assert _root(in_project, None) == str(real / "project" / "maps")


def test_a_cloudmap_in_a_nested_repository(tmp_path: Path) -> None:
    """It loads from, and commits to, its own repository, not the one around
    it."""
    _tracked(tmp_path)
    inner = tmp_path / "repo" / "cloudmap-repo"
    inner.mkdir()
    (inner / FILE).write_text(VERSIONED)
    inner_repo = git.Repo.init(inner, initial_branch="main")
    inner_repo.index.add([FILE])
    inner_repo.index.commit("inner")
    db = open_cloudmap_db(str(inner / FILE), None, LOGGER)
    assert isinstance(db, GitSyncCloudMapDB)
    assert db.artifacts, "the cloudmap loaded empty"
    _edit_and_reload(db, inner / FILE)
    assert db.commit(GitRepo(inner_repo), "in the inner repository")
    assert inner_repo.head.commit.message.startswith("in the inner repository")


@pytest.mark.parametrize("case", ["no repository", "repository"])
def test_a_symlinked_cloudmap(tmp_path: Path, case: str) -> None:
    """Opened by its link, the cloudmap loads from the file it names, saves
    to that file, and leaves the link a link."""
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    target = root / "real" / FILE
    target.write_text(VERSIONED)
    if case == "repository":
        git.Repo.init(root, initial_branch="main")
    link = root / FILE
    link.symlink_to(Path("real") / FILE)
    db = open_cloudmap_db(str(link), None, LOGGER)
    assert isinstance(db, GitSyncCloudMapDB)
    assert db.artifacts, "the cloudmap loaded empty"
    db.add_record(Repository(url="git://e.com/added.git", path="added"))
    assert db.save()
    assert link.is_symlink()
    assert "e.com/added.git" in target.read_text()


def test_the_root_is_found_through_a_symlinked_path(tmp_path: Path) -> None:
    """The project root and the file reached by different spellings of the
    same directory."""
    real = tmp_path / "real"
    (real / "maps").mkdir(parents=True)
    (real / "maps" / FILE).write_text(VERSIONED)
    link = tmp_path / "link"
    link.symlink_to(real)
    from unfurl.cloudmap.gitsync_db import _root

    assert _root(str(link / "maps" / FILE), str(real)) == os.path.realpath(real)
    assert _root(str(real / "maps" / FILE), str(link)) == os.path.realpath(real)


HOLD_LOCK = """
import sqlite3, sys, time
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("CREATE TABLE held (x)")
db.execute("BEGIN EXCLUSIVE")
db.execute("INSERT INTO held VALUES (1)")
print("locked", flush=True)
time.sleep(60)
"""


@pytest.mark.parametrize("case", ["read-only directory", "locked database", "unparsed"])
def test_a_load_git_sync_can_t_make(tmp_path: Path, case: str, caplog) -> None:
    """A read-only directory has no database yet, so it loads with
    CloudMapDB; a locked database exists, so it raises; unparsed YAML has
    none, so it goes to CloudMapDB, which can't load it either."""
    plain = tmp_path / "plain"
    plain.mkdir()
    path = plain / FILE
    path.write_text(VERSIONED)
    lock = None
    if case == "read-only directory":
        plain.chmod(0o555)
    elif case == "locked database":
        db_path = plain / "local" / ".cloudmap.yaml.sqlite"
        db_path.parent.mkdir()
        # another process's: SQLite's locks don't conflict within one
        lock = subprocess.Popen(
            [sys.executable, "-c", HOLD_LOCK, str(db_path)],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert lock.stdout and lock.stdout.readline().strip() == "locked"
    else:
        path.write_text(VERSIONED + "repositories: [unclosed\n")
    try:
        if case == "read-only directory":
            db = open_cloudmap_db(str(path), None, LOGGER)
            assert type(db) is CloudMapDB
            assert db.artifacts
            assert [p.name for p in plain.iterdir()] == [FILE]
            [warning] = [r for r in caplog.records if r.levelname == "WARNING"]
            assert str(path) in warning.getMessage()
            return
        with pytest.raises(Exception) as raised:
            open_cloudmap_db(str(path), None, LOGGER)
    finally:
        plain.chmod(0o755)
        if lock:
            lock.kill()
            lock.wait()
    message = str(raised.value)
    if case == "locked database":
        assert isinstance(raised.value, UnfurlError)
        assert FILE in message and "UNFURL_CLOUDMAP_BACKEND=python" in message, message
    else:
        # git-sync's failure is a warning, then CloudMapDB's own error
        assert any(r.levelname == "WARNING" for r in caplog.records)
        assert [p.name for p in plain.iterdir()] == [FILE]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes to read-only directories")
def test_a_read_only_directory_with_a_database_loads(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    path = plain / FILE
    path.write_text(VERSIONED)
    assert isinstance(open_cloudmap_db(str(path), None, LOGGER), GitSyncCloudMapDB)
    try:
        plain.chmod(0o555)
        (plain / "local").chmod(0o555)
        db = open_cloudmap_db(str(path), None, LOGGER)
        assert isinstance(db, GitSyncCloudMapDB)
        assert db.artifacts
    finally:
        (plain / "local").chmod(0o755)
        plain.chmod(0o755)


def test_an_unparsed_file_with_a_database_raises(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    path = plain / FILE
    path.write_text(VERSIONED)
    assert isinstance(open_cloudmap_db(str(path), None, LOGGER), GitSyncCloudMapDB)
    path.write_text(VERSIONED + "repositories: [unclosed\n")
    with pytest.raises(UnfurlError, match="UNFURL_CLOUDMAP_BACKEND=python"):
        open_cloudmap_db(str(path), None, LOGGER)


@pytest.mark.parametrize("case", ["new database", "existing database"])
def test_a_markdown_cloudmap_never_falls_back(tmp_path: Path, monkeypatch, case: str) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    path = plain / "cloudmap.md"
    path.write_text(LITERATE)
    if case == "existing database":
        assert isinstance(open_cloudmap_db(str(path), None, LOGGER), GitSyncCloudMapDB)

    def broken(self: GitSyncCloudMapDB) -> None:
        raise RuntimeError("broken")

    monkeypatch.setattr(GitSyncCloudMapDB, "_load_records", broken)
    with pytest.raises(UnfurlError, match="broken"):
        open_cloudmap_db(str(path), None, LOGGER)


@pytest.mark.parametrize("case", ["no project", "project", "project elsewhere"])
def test_a_cloudmap_in_a_new_directory(tmp_path: Path, case: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = (project if case == "project" else tmp_path) / "new" / "dir" / FILE
    db = open_cloudmap_db(str(path), None if case == "no project" else str(project), LOGGER)
    assert isinstance(db, GitSyncCloudMapDB), case
    db.add_record(Repository(url="git://e.com/added.git", path="added"))
    assert db.save()
    assert "e.com/added.git" in path.read_text()


def test_a_yaml_and_a_markdown_cloudmap_side_by_side(tmp_path: Path) -> None:
    """Each has its own database."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / FILE).write_text(VERSIONED)
    (plain / "cloudmap.md").write_text(LITERATE)
    for name in (FILE, "cloudmap.md", FILE):
        db = open_cloudmap_db(str(plain / name), None, LOGGER)
        assert isinstance(db, GitSyncCloudMapDB), name
        assert db.file == str(plain / name)
    assert sorted(p.name for p in (plain / "local").glob("*.sqlite")) == [
        ".cloudmap.md.sqlite",
        ".cloudmap.yaml.sqlite",
    ]


def test_only_front_matter_makes_a_literate_cloudmap(tmp_path: Path) -> None:
    from unfurl.__main__ import _is_literate_cloudmap

    for name, text, expected in [
        ("front.md", LITERATE, True),
        ("body.md", "---\ntitle: notes\n---\n\nliterate-yaml: cloudmap@1.0\n", False),
        ("no-front.md", "# notes\n---\nliterate-yaml: cloudmap@1.0\n", False),
    ]:
        (tmp_path / name).write_text(text)
        assert _is_literate_cloudmap(str(tmp_path / name)) == expected, name


LITERATE = (Path(__file__).parent.parent / "rust/git-sync/tests/fixtures/literate_cloudmap.md").read_text()


@pytest.mark.parametrize(
    "name,text,valid",
    [
        ("cloudmap.yaml", VERSIONED, True),
        ("cloudmap.yaml", VERSIONED + "repositories:\n  git://e.com/r.git:\n    no_such_field: 1\n", False),
        ("cloudmap.md", LITERATE, True),
    ],
)
def test_validate_a_cloudmap(tmp_path: Path, name: str, text: str, valid: bool) -> None:
    from click.testing import CliRunner

    from unfurl.__main__ import cli

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / name).write_text(text)
    result = CliRunner().invoke(cli, ["validate", str(plain / name)])
    assert (result.exit_code == 0) == valid, (result.output, result.exception)
    # nothing left beside the file: no git-sync database
    assert sorted(p.name for p in plain.iterdir()) == [name]


@pytest.mark.parametrize("name,text,valid", [(FILE, VERSIONED, True), ("cloudmap.md", LITERATE, False)])
def test_validate_when_git_sync_fails(
    tmp_path: Path, monkeypatch, name: str, text: str, valid: bool
) -> None:
    """A YAML cloudmap passes on the schema check alone; a markdown one fails."""
    from click.testing import CliRunner

    from unfurl.__main__ import cli

    def broken(self: GitSyncCloudMapDB) -> None:
        raise RuntimeError("broken")

    monkeypatch.setattr(GitSyncCloudMapDB, "_load_records", broken)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / name).write_text(text)
    result = CliRunner().invoke(cli, ["validate", str(plain / name)])
    assert (result.exit_code == 0) == valid, (result.output, result.exception)
    assert sorted(p.name for p in plain.iterdir()) == [name]


@pytest.mark.parametrize("case", ["untracked", "ignored"])
def test_a_hand_edit_after_a_save_loads(tmp_path: Path, case: str) -> None:
    """A cloudmap the repository doesn't track: a saved record edited by
    hand loads as edited, and a further save succeeds."""
    _tracked(tmp_path, "README", "r\n")
    root = tmp_path / "repo"
    if case == "ignored":
        (root / ".gitignore").write_text(FILE + "\nlocal/\n")
    path = root / FILE
    path.write_text(VERSIONED)
    db = open_cloudmap_db(str(path), None, LOGGER)
    assert isinstance(db, GitSyncCloudMapDB)
    db.add_record(Repository(url="git://e.com/a.git", path="saved"))
    assert db.save()
    path.write_text(path.read_text().replace("path: saved", "path: by-hand"))
    db = open_cloudmap_db(str(path), None, LOGGER)
    assert isinstance(db, GitSyncCloudMapDB)
    assert db.get_repository("git://e.com/a.git").path == "by-hand"
    db.add_record(Repository(url="git://e.com/b.git", path="b"))
    assert db.save()
    assert "path: by-hand" in path.read_text() and "e.com/b.git" in path.read_text()
