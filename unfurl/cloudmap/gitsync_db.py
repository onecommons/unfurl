# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""A cloudmap kept in a git-sync database: see
``rust/git-sync/docs/cloudmapdb-on-git-sync.md``."""

import hashlib
import json
import os
import re
from typing import TYPE_CHECKING, Any, Dict, Optional
from urllib.parse import quote

from ..repo import GitRepo, Repo
from ..util import UnfurlError
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
    """The records of the cloudmap at ``file_path`` in the git working tree at
    ``repo_root``, loaded from the git-sync database at ``db_path`` and saved
    back through it.

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


def _db_path(filepath: str, project_root: Optional[str]) -> str:
    """Where the git-sync database for the cloudmap at ``filepath`` lives: the
    project's ``local/``, named for the cloudmap's path (a project can use
    more than one), or without a project, ``local/`` beside the cloudmap."""
    if project_root:
        digest = hashlib.sha1(os.path.abspath(filepath).encode()).hexdigest()[:12]
        return os.path.join(project_root, "local", f".cloudmap-{digest}.sqlite")
    directory, name = os.path.split(filepath)
    return os.path.join(directory, "local", f".{os.path.splitext(name)[0]}.sqlite")


def _python_reason(filepath: str, repo: Optional[GitRepo]) -> str:
    """Why the cloudmap at ``filepath`` can't use git-sync, or ""."""
    if os.getenv("UNFURL_CLOUDMAP_BACKEND") == "python":
        return "UNFURL_CLOUDMAP_BACKEND is python"
    try:
        from .._rust import GitSyncDB  # noqa: F401
    except ImportError:
        return "the unfurl._rust extension has no cloudmap-sync"
    if repo is None:
        return "it isn't in a git repository"
    relpath = os.path.relpath(filepath, repo.working_dir)
    if (relpath, 0) not in repo.repo.index.entries:
        # a scan only reads tracked files; once it's committed, git-sync is used
        return "it isn't tracked by git yet"
    with open(filepath) as f:
        if re.search(r"^\s+notable:", f.read(), re.MULTILINE):
            return "it has the deprecated notable key, which only CloudMapDB migrates"
    return ""


def open_cloudmap_db(
    filepath: str,
    repo: Optional[GitRepo],
    project_root: Optional[str],
    logger: "UnfurlLogger",
) -> CloudMapDB:
    """The store for the cloudmap at ``filepath``: a
    :py:class:`GitSyncCloudMapDB` where it can be, otherwise a
    :py:class:`CloudMapDB`, except for a file only git-sync reads."""
    only_git_sync = filepath.endswith(".md")
    if repo is None and os.path.exists(filepath):
        # the cloudmap's owner may not commit it, but it can still be tracked
        repo = Repo.find_containing_git_repo(os.path.dirname(os.path.abspath(filepath)))
    reason = _python_reason(filepath, repo)
    if not reason:
        assert repo
        db_path = _db_path(filepath, project_root)
        if not os.path.exists(db_path):
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            logger.verbose("creating the git-sync database %s", db_path)
        try:
            return GitSyncCloudMapDB(
                repo.working_dir,
                os.path.relpath(filepath, repo.working_dir),
                db_path,
            )
        except Exception as e:
            reason = f"git-sync failed to load it: {e}"
    if only_git_sync:
        raise UnfurlError(f"can't load {filepath}: {reason}")
    logger.debug("loading %s with CloudMapDB: %s", filepath, reason)
    return CloudMapDB(filepath)
