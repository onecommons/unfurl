# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""A cloudmap kept in a git-sync database: see
``rust/git-sync/docs/cloudmapdb-on-git-sync.md``."""

import gc
import hashlib
import json
import os
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from urllib.parse import quote

from ..repo import GitRepo, Repo
from ..util import UnfurlError, is_relative_to
from .db import CloudMapDB

if TYPE_CHECKING:
    from ..logs import UnfurlLogger

SECTIONS = (
    "types",
    "artifacts",
    "repositories",
    "services",
    "components",
    "instantiations",
)
# the document's top-level `metadata`, a singleton record in git-sync
METADATA = ("/metadata", "metadata")

Saved = Dict[str, Dict[str, Any]]


def sqlite_url(path: str) -> str:
    """An sqlx URL for the SQLite database at ``path``, created if missing."""
    return "sqlite://" + quote(os.path.abspath(path)) + "?mode=rwc"


class GitSyncCloudMapDB(CloudMapDB):
    """The records of the cloudmap at ``file_path`` under ``repo_root``, a git
    working tree's root or a directory in no repository, loaded from the
    git-sync database at ``db_path`` and saved back through it.

    Every record is loaded into the same section dicts :py:class:`CloudMapDB`
    keeps, so its methods work unchanged; :py:meth:`save` writes back the
    records that changed since.
    """

    def __init__(self, repo_root: str, file_path: str, db_path: str) -> None:
        from .._rust import GitSyncDB

        self._file = os.path.join(repo_root, file_path)
        self._git_sync = GitSyncDB.open(repo_root, sqlite_url(db_path), file_path)
        self._scan()
        self._load_records()

    @property
    def file(self) -> Optional[str]:
        return self._file

    def _scan(self) -> None:
        from .._rust import CommitMissing

        try:
            outcome = self._git_sync.scan()
        except CommitMissing:
            # the database is a cache of the file: rebuild it from HEAD
            outcome = self._git_sync.scan(rebuild_missing=True)
        # the files git-sync's format rejected records of
        self.invalid: List[str] = outcome["invalid"]
        if outcome["unparsed"] or outcome["failed"]:
            raise UnfurlError(
                f"git-sync couldn't read {self._file}: "
                f"{outcome['unparsed'] or outcome['failed']}"
            )

    def _load_records(self) -> None:
        document: Dict[str, Any] = {}
        found = self._git_sync.find(METADATA[0], key=METADATA[1], alias=False)
        if found:
            document["metadata"] = json.loads(found[0][2])
        for section in SECTIONS:
            records = self._git_sync.find("/" + section, subtypes=False, alias=False)
            document[section] = {key: json.loads(value) for _, key, value in records}
        self._load_document(document)
        self.path = self._file
        self._saved = self._saved_records()
        self._saved_metadata = dict(self.metadata)

    def _saved_records(self) -> Saved:
        """Each section's records as :py:meth:`CloudMapDB.save` writes them:
        versions are written inside their parent record."""
        saved: Saved = {}
        for section in SECTIONS:
            records: Dict[str, Any] = getattr(self, section)
            saved[section] = {
                key: record.asdict() if hasattr(record, "asdict") else record
                for key, record in records.items()
                if not getattr(record, "_parent", None)
            }
        return saved

    def save(self, msg: str = "", commit: bool = True) -> bool:
        """Write the records changed since they were loaded or last saved into
        the file; whether anything changed. ``msg`` and ``commit`` are unused,
        as for :py:meth:`CloudMapDB.save`."""
        current = self._saved_records()
        changed = False
        for section in SECTIONS:
            old, new = self._saved[section], current[section]
            for key, value in new.items():
                if old.get(key) != value:
                    self._git_sync.upsert("/" + section, key, json.dumps(value))
                    changed = True
            for key in old.keys() - new.keys():
                self._git_sync.delete("/" + section, key)
                changed = True
        if self.metadata != self._saved_metadata:
            if self.metadata:
                self._git_sync.upsert(*METADATA, json.dumps(self.metadata))
            else:
                self._git_sync.delete(*METADATA)
            changed = True
        outcome = self._git_sync.save()
        if outcome["conflicts"] or outcome["failed"]:
            raise UnfurlError(
                f"couldn't save {self._file}: conflicts {outcome['conflicts']}, "
                f"failures {outcome['failed']}"
            )
        self._saved = current
        self._saved_metadata = dict(self.metadata)
        return changed

    def commit(self, repo: GitRepo, msg: str) -> bool:
        return self._git_sync.commit(msg) is not None

    def reload(self) -> None:
        self._scan()
        self._load_records()


def _db_path(filepath: str, project_root: Optional[str], db_dir: Optional[str]) -> str:
    """Where the git-sync database for the cloudmap at ``filepath`` lives:
    ``db_dir`` if given, else the project's ``local/``, named for the
    cloudmap's path (a project can use more than one), or without a project,
    ``local/`` beside the cloudmap."""
    if db_dir:
        return os.path.join(db_dir, ".cloudmap.sqlite")
    if project_root:
        digest = hashlib.sha1(os.path.abspath(filepath).encode()).hexdigest()[:12]
        return os.path.join(project_root, "local", f".cloudmap-{digest}.sqlite")
    directory, name = os.path.split(filepath)
    return os.path.join(directory, "local", f".{name}.sqlite")


def _python_reason(filepath: str) -> str:
    """Why the cloudmap at ``filepath`` can't use git-sync, or ""."""
    if os.getenv("UNFURL_CLOUDMAP_BACKEND") == "python":
        return "UNFURL_CLOUDMAP_BACKEND is python"
    try:
        from .._rust import GitSyncDB  # noqa: F401
    except ImportError:
        return "the unfurl._rust extension has no cloudmap-sync"
    if os.path.exists(filepath):
        with open(filepath) as f:
            if re.search(r"^\s+notable:", f.read(), re.MULTILINE):
                return "it has the deprecated notable key, which only CloudMapDB migrates"
    return ""


FALLBACK_HINT = "set UNFURL_CLOUDMAP_BACKEND=python to load it without git-sync"


def _root(filepath: str, project_root: Optional[str]) -> str:
    """The directory git-sync opens for the cloudmap at ``filepath``: the
    innermost git working tree's root, else the project's root when it's
    under it, else its own directory."""
    path = os.path.realpath(filepath)
    repo = Repo.find_containing_git_repo(os.path.dirname(path))
    if repo is not None:
        return os.path.realpath(repo.working_dir)
    if project_root and is_relative_to(path, os.path.realpath(project_root)):
        return os.path.realpath(project_root)
    return os.path.dirname(path)


def _makedirs(path: str) -> List[str]:
    """Create directory ``path`` and its missing parents; those it created,
    outermost first."""
    missing: List[str] = []
    while path and not os.path.exists(path):
        missing.insert(0, path)
        path = os.path.dirname(path)
    for directory in missing:
        os.mkdir(directory)
    return missing


def _remove_created(db_path: str, directories: List[str]) -> None:
    """Remove the database at ``db_path`` with its SQLite side files, then
    ``directories`` where they're empty, innermost first."""
    gc.collect()  # drop the failed handle's connections
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(db_path + suffix)
        except FileNotFoundError:
            pass
    for directory in reversed(directories):
        try:
            os.rmdir(directory)
        except OSError:
            pass


def open_cloudmap_db(
    filepath: str,
    project_root: Optional[str],
    logger: "UnfurlLogger",
    *,
    db_dir: Optional[str] = None,
) -> CloudMapDB:
    """The store for the cloudmap at ``filepath``: a
    :py:class:`GitSyncCloudMapDB`, or a :py:class:`CloudMapDB` where git-sync
    can't be used. git-sync opens the innermost working tree holding the
    file, creating the file's directory if it's missing. ``db_dir`` puts the
    git-sync database there instead of under ``local/``.

    If git-sync fails to open or load the file and its database didn't exist
    before, the database and directories this call created are removed and a
    YAML cloudmap loads with :py:class:`CloudMapDB`, with a warning. Otherwise
    the failure raises :py:class:`UnfurlError`, as does a markdown cloudmap
    git-sync can't load."""
    reason = _python_reason(filepath)
    if reason:
        if filepath.endswith(".md"):
            raise UnfurlError(f"can't load {filepath}: {reason}")
        logger.debug("loading %s with CloudMapDB: %s", filepath, reason)
        return CloudMapDB(filepath)
    root = _root(filepath, project_root)
    db_path = _db_path(filepath, project_root, db_dir)
    existed = os.path.exists(db_path)
    created: List[str] = []
    try:
        created += _makedirs(os.path.dirname(db_path))
        created += _makedirs(root)
        if not existed:
            logger.verbose("creating the git-sync database %s", db_path)
        return GitSyncCloudMapDB(root, os.path.relpath(os.path.realpath(filepath), root), db_path)
    except Exception as e:
        failure = f"git-sync couldn't load {filepath} with its database {db_path}: {e}"
        if existed or filepath.endswith(".md"):
            raise UnfurlError(f"{failure}; {FALLBACK_HINT}") from e
    _remove_created(db_path, created)
    logger.warning("%s; loading it with CloudMapDB instead", failure)
    return CloudMapDB(filepath)
