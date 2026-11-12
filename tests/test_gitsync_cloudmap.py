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


def _relative_project(tmp_path: Path, keys: str) -> Path:
    """A project whose cloudmap ``cloudmap`` is ``maps/cloudmap.yaml``, given
    relative to it, its repository configuration with ``keys`` too, and a
    sibling directory ``elsewhere`` to run from."""
    project = tmp_path / "project"
    (project / "maps").mkdir(parents=True)
    (project / "maps" / FILE).write_text(VERSIONED)
    (project / "maps" / "local.yaml").write_text(VERSIONED)
    (tmp_path / "elsewhere").mkdir()
    (project / "unfurl.yaml").write_text(
        f"""apiVersion: unfurl/v1.0.0
kind: Project
environments:
  defaults:
    cloudmaps:
      repositories:
        cloudmap:
          url: maps/{FILE}
          {keys}
"""
    )
    return project


class _Opened:
    """The ``(file, db_dir)`` of each ``open_cloudmap_db`` call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import unfurl.cloudmap.gitsync_db as module

        self.calls: List[Tuple[str, str]] = []
        real = module.open_cloudmap_db

        def spy(
            filepath: str,
            project_root: Optional[str],
            logger: UnfurlLogger,
            *,
            db_dir: Optional[str] = None,
            backend: Backend = "git-sync",
        ) -> CloudMapDB:
            self.calls.append((os.path.realpath(filepath), os.path.realpath(db_dir or "")))
            return real(filepath, project_root, logger, db_dir=db_dir, backend=backend)

        monkeypatch.setattr(module, "open_cloudmap_db", spy)


def test_relative_settings_resolve_against_the_project(tmp_path: Path, monkeypatch) -> None:
    """Run from outside the project, a relative ``url``, ``db_dir``,
    ``local_cloudmap`` and ``clone_root`` each name what's under it."""
    from unfurl.cloudmap import CloudMap
    from unfurl.util import change_cwd

    project = _relative_project(
        tmp_path, "local_cloudmap: maps/local.yaml\n          db_dir: dbs\n          clone_root: repos"
    )
    opened = _Opened(monkeypatch)
    real = os.path.realpath
    with change_cwd(str(tmp_path / "elsewhere")):
        local_env = LocalEnv(str(project / "unfurl.yaml"), can_be_empty=True)
        cloud_map = CloudMap.from_name(local_env, "cloudmap", None, "", True, False)
        assert real(cloud_map.directory.local_cloudmap) == real(project / "maps" / "local.yaml")
        assert real(cloud_map.directory.repos_root) == real(project / "repos")
        cloud_map.directory.save_local_repositories(cloud_map.directory.local_cloudmap)
    assert opened.calls == [
        (real(project / "maps" / FILE), real(project / "dbs")),
        (real(project / "maps" / "local.yaml"), real(project / "dbs")),
    ]
    assert not list((tmp_path / "elsewhere").iterdir())


def test_absolute_and_home_settings_are_unchanged(tmp_path: Path, monkeypatch) -> None:
    from unfurl.cloudmap import CloudMap
    from unfurl.util import change_cwd

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = _relative_project(
        tmp_path, f"db_dir: ~/dbs\n          clone_root: {tmp_path / 'clones'}"
    )
    opened = _Opened(monkeypatch)
    with change_cwd(str(tmp_path / "elsewhere")):
        local_env = LocalEnv(str(project / "unfurl.yaml"), can_be_empty=True)
        cloud_map = CloudMap.from_name(local_env, "cloudmap", None, "", True, False)
    real = os.path.realpath
    assert opened.calls == [(real(project / "maps" / FILE), real(tmp_path / "home" / "dbs"))]
    assert real(cloud_map.directory.repos_root) == real(tmp_path / "clones")


def test_validate_from_elsewhere_matches_relative_settings(tmp_path: Path, monkeypatch) -> None:
    """``unfurl validate`` run outside the project honours ``backend: python``
    for a cloudmap and a local cloudmap given relative to the project."""
    from unfurl.util import change_cwd

    project = _relative_project(
        tmp_path, "local_cloudmap: maps/local.yaml\n          backend: python"
    )
    validated = _Validated(monkeypatch)
    with change_cwd(str(tmp_path / "elsewhere")):
        for name in (FILE, "local.yaml"):
            result = validated.run(project / "maps" / name)
            assert result.exit_code == 0, (name, result.output, result.exception)
    assert validated.git_sync_stores == 0


def test_a_local_hosts_clone_root_resolves_against_the_project(tmp_path: Path) -> None:
    """A configured ``local`` host's relative ``clone_root`` names a directory
    under the project, run from outside it; a ``repos_root`` passed in wins."""
    from unfurl.cloudmap import CloudMap
    from unfurl.cloudmap.host import LocalRepositoryHost
    from unfurl.util import change_cwd

    project = _relative_project(tmp_path, "")
    (project / "unfurl.yaml").write_text(
        (project / "unfurl.yaml").read_text()
        + "      hosts:\n        mine:\n          type: local\n          clone_root: repos\n"
    )
    real = os.path.realpath
    with change_cwd(str(tmp_path / "elsewhere")):
        local_env = LocalEnv(str(project / "unfurl.yaml"), can_be_empty=True)
        host = CloudMap.get_host(local_env, "mine", "", "")
        assert isinstance(host, LocalRepositoryHost)
        assert real(host.repos_root) == real(project / "repos")
        given = CloudMap.get_host(local_env, "mine", "", "given")
        assert isinstance(given, LocalRepositoryHost)
        assert real(given.repos_root) == real(tmp_path / "elsewhere" / "given")


@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize("backend", ["git-sync", "python"])
def test_a_directory_names_its_cloudmap_file(tmp_path: Path, backend: Backend, existing: bool) -> None:
    """A directory is the cloudmap file ``CloudMapDB.DEFAULT_NAME`` in it,
    there or not yet, and git-sync keeps one database for both names."""
    maps = tmp_path / "maps"
    maps.mkdir()
    file = maps / CloudMapDB.DEFAULT_NAME
    if existing:
        file.write_text(VERSIONED)
    db = open_cloudmap_db(str(maps), None, LOGGER, backend=backend)
    assert type(db) is (GitSyncCloudMapDB if backend == "git-sync" else CloudMapDB)
    assert os.path.realpath(db.path) == os.path.realpath(file)
    assert bool(db.artifacts) == existing
    db.add_record(Repository(url="git://e.com/added.git", path="added"))
    db.save()
    assert "e.com/added.git" in file.read_text()
    if backend == "git-sync":
        again = open_cloudmap_db(str(file), None, LOGGER)
        assert again.get_repository("git://e.com/added.git")
        assert len(list((maps / "local").glob("*.sqlite"))) == 1


def test_validate_matches_a_cloudmap_configured_as_a_directory(tmp_path: Path, monkeypatch) -> None:
    project = _relative_project(tmp_path, "backend: python")
    (project / "maps" / CloudMapDB.DEFAULT_NAME).write_text(VERSIONED)
    (project / "unfurl.yaml").write_text(
        (project / "unfurl.yaml").read_text().replace(f"url: maps/{FILE}", "url: maps")
    )
    validated = _Validated(monkeypatch)
    result = validated.run(project / "maps" / CloudMapDB.DEFAULT_NAME)
    assert result.exit_code == 0, (result.output, result.exception)
    assert validated.git_sync_stores == 0


def test_records_added_on_two_branches_merge(tmp_path: Path) -> None:
    """A record added on each of two branches, as a host sync does, changes
    only its own lines, so git merges the branches."""
    root = tmp_path / "repo"
    root.mkdir()
    repo = git.Repo.init(root, initial_branch="main")
    path = root / FILE
    base = CloudMapDB(str(path))
    for name in ("alpha", "charlie", "echo", "golf", "india"):
        base.add_record(Repository(url=f"git://e.com/team/{name}.git", path=name))
    base.save()
    (root / ".gitignore").write_text("local/\n")
    repo.index.add([FILE, ".gitignore"])
    repo.index.commit("base")
    for branch, name in (("hosts/a", "delta"), ("hosts/b", "hotel")):
        repo.git.checkout("-q", "-b", branch, "main")
        db = open_cloudmap_db(str(path), None, LOGGER, db_dir=str(tmp_path / branch))
        assert isinstance(db, GitSyncCloudMapDB)
        db.add_record(Repository(url=f"git://e.com/team/{name}.git", path=name))
        assert db.save()
        # two lines: the record's key and its path
        assert repo.git.diff("--numstat", FILE).split()[:2] == ["2", "0"], branch
        repo.git.commit("-qam", branch)
    repo.git.checkout("-q", "hosts/a")
    repo.git.merge("-q", "--no-edit", "hosts/b")
    merged = CloudMapDB(str(path))
    assert {"delta", "hotel"} <= {r.path for r in merged.repositories.values()}


def test_reload_follows_a_checkout_of_another_branch(tmp_path: Path) -> None:
    """As a host sync does: records saved and committed on the host's branch,
    the source branch checked out and the host's merged into it with git, with
    a reload after each, then a save and commit on the source branch."""
    root = tmp_path / "repo"
    root.mkdir()
    repo = git.Repo.init(root, initial_branch="main")
    path = root / FILE
    path.write_text(VERSIONED)
    (root / ".gitignore").write_text("local/\n")
    repo.index.add([FILE, ".gitignore"])
    repo.index.commit("base")
    repo.git.checkout("-q", "-b", "hosts/a")
    db = open_cloudmap_db(str(path), None, LOGGER)
    assert isinstance(db, GitSyncCloudMapDB)
    db.add_record(Repository(url="git://e.com/host.git", path="host"))
    assert db.save()
    assert db.commit(GitRepo(repo), "from the host")

    repo.git.checkout("-q", "main")
    db.reload()
    assert not db.get_repository("git://e.com/host.git")
    repo.git.merge("-q", "hosts/a", m="merge")
    db.reload()
    assert db.get_repository("git://e.com/host.git")
    db.add_record(Repository(url="git://e.com/main.git", path="main"))
    assert db.save()
    assert db.commit(GitRepo(repo), "on main")
    assert repo.active_branch.name == "main"
    assert not repo.is_dirty()
    assert "e.com/main.git" in repo.git.show(f"main:{FILE}")
    assert "e.com/main.git" not in repo.git.show(f"hosts/a:{FILE}")


def _file_url_project(tmp_path: Path, url: str, source: str, keys: str = "") -> Path:
    """A project whose cloudmap ``cloudmap`` has ``url``, given in
    ``cloudmaps.repositories`` or, for ``source`` "repositories", in the
    environment's ``repositories``; and a sibling directory ``elsewhere``."""
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    (tmp_path / "elsewhere").mkdir(exist_ok=True)
    if source == "cloudmaps":
        config = f"    cloudmaps:\n      repositories:\n        cloudmap:\n          url: {url}\n{keys}"
    else:
        config = (
            f"    repositories:\n      cloudmap:\n        url: {url}\n"
            f"    cloudmaps:\n      repositories:\n        cloudmap:\n{keys or '          {}' + chr(10)}"
        )
    (project / "unfurl.yaml").write_text(
        "apiVersion: unfurl/v1.0.0\nkind: Project\nenvironments:\n  defaults:\n" + config
    )
    return project


@pytest.mark.parametrize("source", ["cloudmaps", "repositories"])
@pytest.mark.parametrize("fragment", ["", "#main:c.yaml"])
@pytest.mark.parametrize(
    "url,expected",
    [
        ("file:repos/cm", "{project}/repos/cm"),
        ("file:../cm", "{tmp}/cm"),
        ("file:~/cm", "{tmp}/home/cm"),
        ("file:{tmp}/abs/cm", "{tmp}/abs/cm"),
        ("file://{tmp}/abs/cm", "{tmp}/abs/cm"),
    ],
)
def test_a_file_url_resolves_against_the_project(
    tmp_path: Path, monkeypatch, source: str, fragment: str, url: str, expected: str
) -> None:
    """Run from outside the project, a relative ``file:`` cloudmap url names
    a directory under it, from either source; absolute and ``~`` ones are
    as written."""
    from unfurl.cloudmap import CloudMap
    from unfurl.util import change_cwd

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = _file_url_project(tmp_path, url.format(tmp=tmp_path) + fragment, source)
    with change_cwd(str(tmp_path / "elsewhere")):
        local_env = LocalEnv(str(project / "unfurl.yaml"), can_be_empty=True)
        config = CloudMap.get_config(local_env, "cloudmap")
    assert config.url == "file://" + expected.format(project=project, tmp=tmp_path)
    assert config.path == ("c.yaml" if fragment else "")


def _cloudmap_repository(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "cloudmap.yaml").write_text(VERSIONED)
    repo = git.Repo.init(path, initial_branch="main")
    repo.index.add(["cloudmap.yaml"])
    repo.index.commit("cloudmap")


@pytest.mark.parametrize("source", ["cloudmaps", "repositories"])
def test_from_name_opens_a_relative_file_url_under_the_project(
    tmp_path: Path, monkeypatch, source: str
) -> None:
    from unfurl.cloudmap import CloudMap
    from unfurl.util import change_cwd

    project = _file_url_project(tmp_path, "file:repos/cm#main:cloudmap.yaml", source)
    _cloudmap_repository(project / "repos" / "cm")
    opened = _Opened(monkeypatch)
    with change_cwd(str(tmp_path / "elsewhere")):
        local_env = LocalEnv(str(project / "unfurl.yaml"), can_be_empty=True)
        cloud_map = CloudMap.from_name(local_env, "cloudmap", None, "", True, False)
    assert cloud_map.directory.store.artifacts
    [(file, _)] = opened.calls
    assert file == os.path.realpath(project / "repos" / "cm" / "cloudmap.yaml")


@pytest.mark.parametrize("source", ["cloudmaps", "repositories"])
def test_validate_from_elsewhere_matches_a_relative_file_url(
    tmp_path: Path, monkeypatch, source: str
) -> None:
    from unfurl.util import change_cwd

    project = _file_url_project(
        tmp_path, "file:repos/cm#main:cloudmap.yaml", source, "          backend: python\n"
    )
    _cloudmap_repository(project / "repos" / "cm")
    validated = _Validated(monkeypatch)
    with change_cwd(str(tmp_path / "elsewhere")):
        result = validated.run(project / "repos" / "cm" / "cloudmap.yaml")
    assert result.exit_code == 0, (result.output, result.exception)
    assert validated.git_sync_stores == 0


# strings ruamel and git-sync write alike: plain, single- or double-quoted
WRITTEN_ALIKE = [
    "it's", 'say "hi"', "https://e.com/a?b=c&d", "git://e.com/a.git", "1:30", "a:b",
    "a#b", "x#", "a :b", "-dash", "?q", ":colon", "a,b", "a[b]", "1.2.3", "v1.2", "nan",
    "\U0001f600", "\U0001f468\U0000200d\U0001f469\U0000200d\U0001f467",
    "\U00002764\U0000fe0f", "\U0001f1fa\U0001f1f8", "\U0001f600 first", "a\xa0b",
    "a\U0000200bb", "e\xe9", "a: b", "a #b", " lead", "trail ", "", "*star", "- dash",
    "-", "{brace", "@at", "%pct", "#hash", "\x60tick", '"dq', "a:", "---", "...", "=",
    "<<", "true", "null", "~", "1", "-1", "010", "1.5", ".5", "1e3", "0x1F", "0o17",
    "1_000", ".inf", "2001-12-14", "2001-12-14T21:59:43Z", "'quote", "it's: x", "'both\"",
    "tab\there", "bell\x07", "nul\x00", "del\x7f", "bom\U0000feff",
]


# long strings, which both wrap past column 80 when plain or single-quoted
LONG_ALIKE = [
    " ".join(["word"] * 40),
    "*" + " ".join(["word"] * 40),
    " ".join(["word"] * 40) + " ",
    " " + " ".join(["word"] * 40),
    "x" * 100 + " " + " ".join(["word"] * 20),
    "  ".join(["word"] * 30),
    " ".join(["\U0001f600" * 3] * 30),
    " ".join(["\xe9" * 5] * 30),
    ("x" * 10 + "\xa0") * 9 + " y",
    "aaa " * 19 + "- bbb ccc",
    "aaa " * 19 + "#x y",
]


def _add_with_both(
    tmp_path: Path, extensions: Dict[str, Any], url: str = "git://e.com/team/charlie.git"
) -> Tuple[Path, Path]:
    """The files CloudMapDB and git-sync save after each adds a repository
    at ``url`` with ``extensions`` to the same CloudMapDB-written cloudmap."""
    files = []
    for name in ("python", "git_sync"):
        path = tmp_path / name / FILE
        path.parent.mkdir()
        base = CloudMapDB(str(path))
        for repo in ("alpha", "echo"):
            base.add_record(Repository(url=f"git://e.com/team/{repo}.git", path=repo))
        base.save()
        files.append(path)
    python = CloudMapDB(str(files[0]))
    git_sync = GitSyncCloudMapDB(str(files[1].parent), FILE, str(tmp_path / "db.sqlite"))
    for db in (python, git_sync):
        db.add_record(Repository(url=url, path="c", **extensions))
        assert db.save()
    return files[0], files[1]


def test_an_added_record_is_written_as_cloudmap_db_writes_it(tmp_path: Path) -> None:
    python, git_sync = _add_with_both(
        tmp_path,
        {
            "org.foo.values": {f"v{i}": s for i, s in enumerate(WRITTEN_ALIKE)},
            "org.foo.keys": {s: i for i, s in enumerate(WRITTEN_ALIKE)},
            "org.foo.list": WRITTEN_ALIKE,
            "org.foo.nested": [{"a": [1, 2.5]}, [True, False], {}, [], {"b": None}],
            "org.foo.long": {f"l{i}": s for i, s in enumerate(LONG_ALIKE)},
            "org.foo.long-list": LONG_ALIKE,
            "org.foo.deep": {"a": {"b": [{"c": LONG_ALIKE[0], "d": [LONG_ALIKE[1]]}]}},
        },
    )
    assert git_sync.read_text() == python.read_text()


@pytest.mark.parametrize("length", [1018, 1019])
def test_a_long_key_is_written_as_cloudmap_db_writes_it(tmp_path: Path, length: int) -> None:
    """ruamel writes a key over 1018 characters as an explicit key."""
    url = "git://e.com/" + "x" * (length - 16) + ".git"
    keys = {"k" * length: 1, "k" * (length - 1) + ":": 2}
    if length <= 1018:
        # on its value's line, where ruamel doesn't wrap it either
        keys[("k" * 50 + " ") * (length // 51) + "k" * (length % 51)] = 3
    python, git_sync = _add_with_both(tmp_path, {"org.foo.keys": keys}, url)
    text = git_sync.read_text()
    assert (f"  ? {url}\n" in text) == (length > 1018)
    assert text == python.read_text()


def test_a_long_key_is_never_wrapped(tmp_path: Path) -> None:
    """ruamel wraps an explicit key; git-sync writes it on one line, which
    CloudMapDB reads back. (A repository's own url can't hold a space.)"""
    url = "git://e.com/" + " ".join(["seg"] * 272) + "x"
    assert len(url) == 1100
    python, git_sync = _add_with_both(tmp_path, {"org.foo.urls": {url: {"a": 1}}})
    assert f"      ? {url}\n      : a: 1\n" in git_sync.read_text()
    assert f"      ? {url}\n" not in python.read_text()
    record = CloudMapDB(str(git_sync)).repositories["git://e.com/team/charlie.git"]
    assert record.extensions["org.foo.urls"] == {url: {"a": 1}}


def test_an_edited_long_value_is_written_as_cloudmap_db_writes_it(tmp_path: Path) -> None:
    """An edit that makes a value long wraps it where ruamel does, and one
    that makes it short again puts it back on one line."""
    long = " ".join(["word"] * 30)
    python_path, git_sync_path = _add_with_both(tmp_path, {"org.foo.note": "short"})
    python = CloudMapDB(str(python_path))
    git_sync = GitSyncCloudMapDB(str(git_sync_path.parent), FILE, str(tmp_path / "db.sqlite"))
    for note in (long, "short", long + " more", long):
        for db in (python, git_sync):
            record = db.repositories["git://e.com/team/charlie.git"]
            record.extensions["org.foo.note"] = note
            db.add_record(record)
            assert db.save()
        assert git_sync_path.read_text() == python_path.read_text(), note


def test_what_git_sync_writes_unlike_cloudmap_db(tmp_path: Path) -> None:
    """git-sync quotes YAML 1.1's booleans, writes a multi-line string as a
    literal block, doesn't wrap a double-quoted string, escapes U+0085,
    U+2028 and U+2029, writes a null in a list as ``null`` and a float's
    exponent after ``.0``; ruamel doesn't. CloudMapDB reads back what
    git-sync writes."""
    long = "it's: " + " ".join(["word"] * 40)
    values = {
        "bool": "yes",
        "multi": "a\nb\n",
        "long": long,
        "nel": "a\x85b",
        "ls": "a\U00002028b",
        "ps": "a\U00002029b",
        "nulls": [None],
        "huge": 1e300,
        "on": 1,
    }
    python, git_sync = _add_with_both(tmp_path, {"org.foo.diff": values})
    lines = {
        "bool": ("      bool: yes\n", "      bool: 'yes'\n"),
        "multi": ('      multi: "a\\nb\\n"\n', "      multi: |\n        a\n        b\n"),
        "long": ('      long: "it\'s: word word', f'      long: "{long}"\n'),
        "nel": ("      nel: 'a\x85 ", '      nel: "a\\Nb"\n'),
        "ls": ("      ls: 'a\U00002028 ", '      ls: "a\\Lb"\n'),
        "ps": ("      ps: 'a\U00002029 ", '      ps: "a\\Pb"\n'),
        "nulls": ("      nulls:\n      - \n", "      nulls:\n      - null\n"),
        "huge": ("      huge: 1e+300\n", "      huge: 1.0e+300\n"),
        "on": ("      on: 1\n", "      'on': 1\n"),
    }
    python_text, git_sync_text = python.read_text(), git_sync.read_text()
    for name, (ruamel, ours) in lines.items():
        assert ruamel in python_text, (name, python_text)
        assert ours in git_sync_text, (name, git_sync_text)
    assert f'      long: "{long}"\n' not in python_text, "ruamel wraps it"
    record = CloudMapDB(str(git_sync)).repositories["git://e.com/team/charlie.git"]
    assert record.extensions["org.foo.diff"] == values


# strings that need a literal block, an escape, a quote ruamel doesn't add,
# or an explicit key
WRITTEN_DIFFERENTLY = [
    "y", "Y", "yes", "yEs", "n", "no", "on", "oN", "off", "OFF", "tRUE", "nULL",
    ".5e5", "\xa00", "a\nb", "a\nb\n", "a\n\nb\n\n", "a\n b", "\tx\ny", "\nlead",
    " lead\nx", "trail \nx", "a\r\nb", "nel\x85x", "ls\U00002028x", "ps\U00002029x",
    "esc\x1b", "c1\x80", "non\U0000fffe", "\U0001f600\t", "k" * 1025,
]


def test_cloudmap_db_reads_back_what_git_sync_writes(tmp_path: Path) -> None:
    strings = WRITTEN_ALIKE + WRITTEN_DIFFERENTLY
    extensions = {
        "org.foo.values": {f"v{i}": s for i, s in enumerate(strings)},
        "org.foo.keys": {s: i for i, s in enumerate(strings)},
        "org.foo.list": strings,
    }
    _, git_sync = _add_with_both(tmp_path, extensions)
    record = CloudMapDB(str(git_sync)).repositories["git://e.com/team/charlie.git"]
    assert record.extensions == extensions
