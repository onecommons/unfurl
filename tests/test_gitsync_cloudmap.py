"""``GitSyncCloudMapDB`` against ``CloudMapDB``: the same cloudmap loads into the
same records, and the same changes save to the same document."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import git
from click.testing import Result
import pytest

from unfurl.cloudmap.db import CloudMapDB
from tests.utils import cloudmap_document
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


def test_the_same_changes_save_the_samecloudmap_document(both) -> None:
    python, git_sync = both
    _change(python)
    _change(git_sync)
    assert python.save() and git_sync.save()
    assert cloudmap_document(git_sync.file) == cloudmap_document(python.file)
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
    assert cloudmap_document(git_sync.file)["metadata"] == cloudmap_document(python.file)["metadata"]
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
    document = cloudmap_document(git_sync.file)
    assert document == cloudmap_document(python.file)
    assert document["artifacts"]["pkg:npm/example"]["versions"]["@1.0"] == {
        "digest": "sha256:2"
    }
    assert list(document["artifacts"]) == ["pkg:npm/example"]


from unfurl.cloudmap.gitsync_db import Backend, open_cloudmap_db  # noqa: E402
from unfurl.localenv import LocalEnv  # noqa: E402
from unfurl.logs import UnfurlLogger, getLogger  # noqa: E402
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


@pytest.mark.parametrize("case", ["backend", "no extension", "notable"])
def test_falls_back_to_cloudmap_db(tmp_path: Path, monkeypatch, case: str) -> None:
    import unfurl._rust

    repo = _tracked(tmp_path)
    path = tmp_path / "repo" / FILE
    backend: Backend = "git-sync"
    if case == "backend":
        backend = "python"
    elif case == "no extension":
        monkeypatch.delattr(unfurl._rust, "GitSyncDB")
    elif case == "notable":
        path.write_text(
            VERSIONED + "repositories:\n  git://e.com/r.git:\n    path: r\n    notable: {}\n"
        )
    db = open_cloudmap_db(str(path), None, LOGGER, backend=backend)
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
    # the database was created, so where it goes isn't why
    assert "db_dir" not in message


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
            assert "db_dir" in warning.getMessage()
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
        assert FILE in message and "backend: python" in message, message
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
    with pytest.raises(UnfurlError, match="backend: python"):
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
    with pytest.raises(UnfurlError, match="broken") as raised:
        open_cloudmap_db(str(path), None, LOGGER)
    # the database was created, and CloudMapDB can't read markdown
    assert "db_dir" not in str(raised.value)
    assert "backend: python" not in str(raised.value)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes to read-only directories")
def test_a_markdown_cloudmap_in_a_read_only_directory_names_db_dir(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    path = plain / "cloudmap.md"
    path.write_text(LITERATE)
    try:
        plain.chmod(0o555)
        with pytest.raises(UnfurlError) as raised:
            open_cloudmap_db(str(path), None, LOGGER)
    finally:
        plain.chmod(0o755)
    message = str(raised.value)
    assert "db_dir" in message, message
    assert "backend: python" not in message, message
    assert [p.name for p in plain.iterdir()] == ["cloudmap.md"]


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


def test_db_dir_with_a_read_only_cloudmap_directory(tmp_path: Path) -> None:
    """Both a YAML and a markdown cloudmap load through git-sync, and nothing
    is created beside them."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / FILE).write_text(VERSIONED)
    (plain / "cloudmap.md").write_text(LITERATE)
    db_dir = tmp_path / "dbs"
    try:
        plain.chmod(0o555)
        for name in (FILE, "cloudmap.md"):
            db = open_cloudmap_db(str(plain / name), None, LOGGER, db_dir=str(db_dir))
            assert isinstance(db, GitSyncCloudMapDB), name
        assert sorted(p.name for p in plain.iterdir()) == ["cloudmap.md", FILE]
    finally:
        plain.chmod(0o755)
    assert len(list(db_dir.glob(".cloudmap-*.sqlite"))) == 2


def test_two_cloudmaps_in_one_db_dir_have_their_own_databases(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    db_dir = tmp_path / "dbs"
    for name, url in (("a.yaml", "git://e.com/a.git"), ("b.yaml", "git://e.com/b.git")):
        (plain / name).write_text(
            f"apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n  {url}:\n    path: x\n"
        )
    loaded = {
        name: list(open_cloudmap_db(str(plain / name), None, LOGGER, db_dir=str(db_dir)).repositories)
        for name in ("a.yaml", "b.yaml")
    }
    assert loaded == {"a.yaml": ["git://e.com/a.git"], "b.yaml": ["git://e.com/b.git"]}
    assert len(list(db_dir.glob(".cloudmap-*.sqlite"))) == 2


def test_the_db_dir_key_places_both_databases(tmp_path: Path) -> None:
    """``cloudmaps.repositories.<name>.db_dir`` puts the cloudmap's and its
    ``local_cloudmap``'s git-sync databases there, each its own."""
    from unfurl.cloudmap import CloudMap

    maps, project, db_dir = tmp_path / "maps", tmp_path / "project", tmp_path / "dbs"
    local_env = _cloudmap_project(tmp_path, f"db_dir: {db_dir}")
    cloud_map = CloudMap.from_name(local_env, "cloudmap", None, "", True, False)
    assert isinstance(cloud_map.directory.store, GitSyncCloudMapDB)
    assert cloud_map.directory.store.artifacts
    assert len(list(db_dir.glob(".cloudmap-*.sqlite"))) == 1

    cloud_map.directory.save_local_repositories(str(maps / "local-cloudmap.yaml"))
    assert len(list(db_dir.glob(".cloudmap-*.sqlite"))) == 2
    assert not (project / "local").exists() and not (maps / "local").exists()


def _cloudmap_project(tmp_path: Path, keys: str) -> LocalEnv:
    """A project in ``tmp_path/project`` whose cloudmap ``cloudmap`` is
    ``tmp_path/maps/cloudmap.yaml``, its repository configuration with ``keys``
    too."""
    maps = tmp_path / "maps"
    maps.mkdir()
    (maps / FILE).write_text(VERSIONED)
    project = tmp_path / "project"
    project.mkdir()
    (project / "unfurl.yaml").write_text(
        f"""apiVersion: unfurl/v1.0.0
kind: Project
environments:
  defaults:
    cloudmaps:
      repositories:
        cloudmap:
          url: {maps / FILE}
          {keys}
"""
    )
    return LocalEnv(str(project / "unfurl.yaml"), can_be_empty=True)


def test_the_backend_key_keeps_both_cloudmaps_in_cloudmap_db(tmp_path: Path, monkeypatch) -> None:
    """``backend: python`` loads the cloudmap and its local cloudmap with
    CloudMapDB, and creates no git-sync database."""
    import unfurl.cloudmap.gitsync_db as module
    from unfurl.cloudmap import CloudMap

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
    local_env = _cloudmap_project(tmp_path, "backend: python")
    cloud_map = CloudMap.from_name(local_env, "cloudmap", None, "", True, False)
    cloud_map.directory.save_local_repositories(str(tmp_path / "maps" / "local.yaml"))
    assert [type(db) for db in opened] == [CloudMapDB, CloudMapDB]
    assert cloud_map.directory.store.artifacts
    assert not list(tmp_path.rglob("*.sqlite"))


def test_an_unknown_backend_is_an_error(tmp_path: Path) -> None:
    from unfurl.cloudmap import CloudMap

    local_env = _cloudmap_project(tmp_path, "backend: rust")
    with pytest.raises(UnfurlError) as raised:
        CloudMap.from_name(local_env, "cloudmap", None, "", True, False)
    message = str(raised.value)
    assert "cloudmaps.repositories.cloudmap.backend" in message, message
    assert '"git-sync"' in message and '"python"' in message and "rust" in message, message


def test_a_markdown_cloudmap_with_the_python_backend_raises(tmp_path: Path) -> None:
    path = tmp_path / "cloudmap.md"
    path.write_text(LITERATE)
    with pytest.raises(UnfurlError, match="backend: python"):
        open_cloudmap_db(str(path), None, LOGGER, backend="python")


def _validate_project(tmp_path: Path, keys: str = "") -> Tuple[Path, Path]:
    """A project with ``maps/cloudmap.yaml`` configured as cloudmap
    ``cloudmap``, its repository configuration with ``keys`` too, and
    ``maps/other.yaml`` configured as nothing."""
    project = tmp_path / "project"
    maps = project / "maps"
    maps.mkdir(parents=True)
    (maps / FILE).write_text(VERSIONED)
    (maps / "other.yaml").write_text(VERSIONED)
    (project / "unfurl.yaml").write_text(
        f"""apiVersion: unfurl/v1.0.0
kind: Project
environments:
  defaults:
    cloudmaps:
      repositories:
        cloudmap:
          url: {maps / FILE}
          {keys}
"""
    )
    return project, maps


class _Validated:
    """What ``unfurl validate`` did: the environment it reached
    ``_validate_cloudmap`` with and the git-sync stores it built."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import unfurl.__main__ as main

        self.local_envs: List[Optional[LocalEnv]] = []
        self.git_sync_stores = 0
        validate = main._validate_cloudmap
        init = GitSyncCloudMapDB.__init__

        def spy_validate(path: str, local_env: Optional[LocalEnv]) -> None:
            self.local_envs.append(local_env)
            validate(path, local_env)

        def spy_init(db: GitSyncCloudMapDB, repo_root: str, file_path: str, db_path: str) -> None:
            self.git_sync_stores += 1
            init(db, repo_root, file_path, db_path)

        monkeypatch.setattr(main, "_validate_cloudmap", spy_validate)
        monkeypatch.setattr(GitSyncCloudMapDB, "__init__", spy_init)

    def run(self, path: Path) -> Result:
        from click.testing import CliRunner

        from unfurl.__main__ import cli

        return CliRunner().invoke(cli, ["validate", str(path)])


def test_validate_reaches_a_cloudmap_with_its_project(tmp_path: Path, monkeypatch) -> None:
    project, maps = _validate_project(tmp_path)
    validated = _Validated(monkeypatch)
    result = validated.run(maps / FILE)
    assert result.exit_code == 0, (result.output, result.exception)
    [local_env] = validated.local_envs
    assert local_env and local_env.project
    assert os.path.realpath(local_env.project.projectRoot) == os.path.realpath(project)


@pytest.mark.parametrize("keys,git_sync", [("backend: python", 0), ("", 1)])
def test_validate_uses_the_configured_backend(
    tmp_path: Path, monkeypatch, keys: str, git_sync: int
) -> None:
    """Without a database left anywhere, and with the default only where no
    configured cloudmap is the file."""
    project, maps = _validate_project(tmp_path, keys)
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    validated = _Validated(monkeypatch)
    result = validated.run(maps / FILE)
    assert result.exit_code == 0, (result.output, result.exception)
    assert validated.git_sync_stores == git_sync
    result = validated.run(maps / "other.yaml")
    assert result.exit_code == 0, (result.output, result.exception)
    assert validated.git_sync_stores == git_sync + 1
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


def test_validate_uses_the_local_cloudmaps_backend(tmp_path: Path, monkeypatch) -> None:
    project, maps = _validate_project(
        tmp_path, f"local_cloudmap: {maps_local(tmp_path)}\n          backend: python"
    )
    maps_local(tmp_path).write_text(VERSIONED)
    validated = _Validated(monkeypatch)
    result = validated.run(maps_local(tmp_path))
    assert result.exit_code == 0, (result.output, result.exception)
    assert validated.git_sync_stores == 0


def maps_local(tmp_path: Path) -> Path:
    return tmp_path / "project" / "maps" / "local.yaml"


def test_validate_never_clones_a_remote_cloudmap(tmp_path: Path, monkeypatch) -> None:
    """A remote cloudmap with no checkout here matches nothing: the file takes
    the default."""
    from unfurl.cloudmap import CloudMap

    def no_clone(*args: object, **kwargs: object) -> None:
        raise AssertionError("validate tried to clone or fetch")

    monkeypatch.setattr(CloudMap, "_checkout_cloudmap", no_clone)
    monkeypatch.setattr(git.Repo, "clone_from", no_clone)
    monkeypatch.setattr(git.Remote, "fetch", no_clone)
    project, maps = _validate_project(tmp_path)
    (project / "unfurl.yaml").write_text(
        (project / "unfurl.yaml")
        .read_text()
        .replace(f"url: {maps / FILE}", "url: https://example.com/cloudmap.git#:cloudmap.yaml")
        + "          backend: python\n"
    )
    validated = _Validated(monkeypatch)
    result = validated.run(maps / FILE)
    assert result.exit_code == 0, (result.output, result.exception)
    assert validated.git_sync_stores == 1


def test_validate_reports_a_bad_backend(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from unfurl.__main__ import cli

    project, maps = _validate_project(tmp_path, "backend: rust")
    result = CliRunner().invoke(cli, ["validate", str(maps / FILE)])
    assert result.exit_code != 0
    assert "cloudmaps.repositories.cloudmap.backend" in str(result.exception), result.exception


def test_validate_without_the_projects_environment(tmp_path: Path, monkeypatch) -> None:
    """When the project's environment can't be built for the cloudmap, it's
    validated as one in no project."""
    import unfurl.__main__ as main

    project, maps = _validate_project(tmp_path, "backend: python")
    built: List[str] = []
    real = main.LocalEnv

    def local_env(path: str, *args: object, **kwargs: object) -> LocalEnv:
        built.append(path)
        if len(built) > 1:
            raise UnfurlError("unusable project")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(main, "LocalEnv", local_env)
    validated = _Validated(monkeypatch)
    result = validated.run(maps / FILE)
    assert result.exit_code == 0, (result.output, result.exception)
    assert len(built) == 2 and validated.local_envs == [None]
    assert validated.git_sync_stores == 1


TWO_REPOSITORIES = (
    "apiVersion: unfurl/v1.0.0\nkind: CloudMap\nrepositories:\n"
    "  git://e.com/a.git:\n    path: committed\n"
    "  git://e.com/other.git:\n    path: other\n"
)
A = "git://e.com/a.git"


def _ours(caplog: pytest.LogCaptureFixture) -> List[str]:
    """The warnings logged through Unfurl's logger, not git-sync's."""
    return [r.getMessage() for r in caplog.records if r.name == "unfurl" and r.levelname == "WARNING"]


def _tracked_store(tmp_path: Path, text: str = TWO_REPOSITORIES) -> Tuple[GitSyncCloudMapDB, Path]:
    _tracked(tmp_path, FILE, text)
    path = tmp_path / "repo" / FILE
    db = open_cloudmap_db(str(path), None, LOGGER)
    assert isinstance(db, GitSyncCloudMapDB)
    return db, path


def _lines_of(path: Path, key: str) -> List[str]:
    """The lines of the record at ``key`` in the file at ``path``."""
    lines = path.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{key}:")
    end = next(
        (i for i in range(start + 1, len(lines)) if not lines[i].startswith("    ")), len(lines)
    )
    return lines[start:end]


def test_a_hand_edit_of_a_saved_record_wins(tmp_path: Path, caplog) -> None:
    """A record Unfurl saved and hasn't committed, then edited in the file,
    loads as the file has it, with a warning, and saves don't fail on it."""
    db, path = _tracked_store(tmp_path)
    db.add_record(Repository(url=A, path="saved"))
    assert db.save()
    path.write_text(path.read_text().replace("path: saved", "path: by-hand"))

    db.reload()
    assert db.get_repository(A).path == "by-hand"
    [warning] = _ours(caplog)
    assert str(path) in warning and "repositories" in warning and A in warning, warning
    assert "file's version replaces" in warning, warning
    by_hand = _lines_of(path, A)

    # an edit, not an addition, which would rewrite the section's keys
    db.add_record(Repository(url="git://e.com/other.git", path="changed"))
    assert db.save()
    assert _lines_of(path, A) == by_hand
    assert "path: changed" in path.read_text()
    caplog.clear()
    db.reload()
    assert _ours(caplog) == [] and db._git_sync.scan()["conflicts"] == []
    reopened = open_cloudmap_db(str(path), None, LOGGER)
    assert reopened.get_repository(A).path == "by-hand"


def test_a_hand_deleted_record_unfurl_saved_stays_deleted(tmp_path: Path, caplog) -> None:
    db, path = _tracked_store(tmp_path)
    db.add_record(Repository(url=A, path="saved"))
    assert db.save()
    path.write_text(path.read_text().replace(f"  {A}:\n    path: saved\n", ""))

    db.reload()
    assert db.get_repository(A) is None
    [warning] = _ours(caplog)
    assert A in warning, warning
    db.add_record(Repository(url="git://e.com/b.git", path="b"))
    assert db.save()
    assert A not in path.read_text()
    caplog.clear()
    db.reload()
    assert _ours(caplog) == [] and db.get_repository(A) is None


def test_a_record_unfurl_deleted_and_hand_restored_is_kept(tmp_path: Path, caplog) -> None:
    db, path = _tracked_store(tmp_path)
    del db.repositories[A]
    assert db.save()
    assert A not in path.read_text()
    path.write_text(path.read_text() + f"  {A}:\n    path: by-hand\n")

    db.reload()
    assert db.get_repository(A).path == "by-hand"
    [warning] = _ours(caplog)
    assert A in warning, warning
    db.add_record(Repository(url="git://e.com/b.git", path="b"))
    assert db.save()
    assert "path: by-hand" in path.read_text()
    caplog.clear()
    db.reload()
    assert _ours(caplog) == [] and db.get_repository(A).path == "by-hand"


def test_hand_edited_metadata_wins(tmp_path: Path, caplog) -> None:
    db, path = _tracked_store(tmp_path, TWO_REPOSITORIES + "metadata:\n  owner: committed\n")
    db.metadata["owner"] = "saved"
    assert db.save()
    path.write_text(path.read_text().replace("owner: saved", "owner: by-hand"))

    db.reload()
    assert db.metadata["owner"] == "by-hand"
    [warning] = _ours(caplog)
    assert "metadata" in warning, warning
    db.add_record(Repository(url="git://e.com/b.git", path="b"))
    assert db.save()
    assert "owner: by-hand" in path.read_text()
    caplog.clear()
    db.reload()
    assert _ours(caplog) == [] and db.metadata["owner"] == "by-hand"


def test_a_hand_edit_of_another_record_isnt_a_conflict(tmp_path: Path, caplog) -> None:
    db, path = _tracked_store(tmp_path)
    db.add_record(Repository(url=A, path="saved"))
    assert db.save()
    path.write_text(path.read_text().replace("path: other", "path: by-hand"))

    db.reload()
    assert db.get_repository(A).path == "saved"
    assert db.get_repository("git://e.com/other.git").path == "by-hand"
    assert _ours(caplog) == []
    assert db.save() is False


def test_a_hand_edit_of_a_saved_untracked_record_isnt_a_conflict(tmp_path: Path, caplog) -> None:
    """A save settles what it writes to a file the index doesn't have, so a
    hand edit after it is simply taken in."""
    _tracked(tmp_path, "other.yaml", TWO_REPOSITORIES)
    path = tmp_path / "repo" / FILE
    path.write_text(TWO_REPOSITORIES)
    db = open_cloudmap_db(str(path), None, LOGGER)
    assert isinstance(db, GitSyncCloudMapDB)
    db.add_record(Repository(url=A, path="saved"))
    assert db.save()
    path.write_text(path.read_text().replace("path: saved", "path: by-hand"))

    db.reload()
    assert db.get_repository(A).path == "by-hand"
    assert _ours(caplog) == []
    assert not any("diverges" in r.getMessage() for r in caplog.records)
