# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""Garbage collection: removing the records analysis created only because
another record referred to them (``metadata.discovery.dependent``), once
nothing does."""

from collections import deque
from typing import Deque, Dict, Iterator, List, Set, Tuple

from ..logs import getLogger
from ..tosca_plugins.cloudmap_defs import (
    CloudMapRecord,
    CloudType,
    CommonMetadata,
    Repository,
)
from ..util import JsonValue
from .db import CloudMapDB, CloudMapStore
from .provenance import _canonical, discovery_sources, record_identity

logger = getLogger("unfurl")

Identity = Tuple[str, str]

_SECTIONS = (
    "repositories",
    "artifacts",
    "components",
    "services",
    "instantiations",
    "types",
)


def _forms(text: str) -> Set[str]:
    """The keys ``text`` could name a record by: as written, without a trailing
    "/", as a ``#/<section>/...`` pointer or pseudo-URL names it, and a git
    URL as :py:func:`~unfurl.cloudmap.provenance._canonical` gives it."""
    forms = {text, text.rstrip("/")}
    forms.update(CloudMapDB._normalize_url(text, section)[1] for section in _SECTIONS)
    for form in list(forms):
        if "://" in form:
            try:
                repo, path = _canonical(form)
            except ValueError:
                continue
            forms.add(f"{repo}#:{path}" if path and path != "." else repo)
    return forms


def _strings(value: JsonValue, parent: str = "") -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if parent == "metadata" and key == "discovery":
                # where the record came from, not what it uses
                continue
            yield key
            yield from _strings(item, key)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, parent)


def _mentions(record: CloudMapRecord) -> Set[str]:
    """Every string in ``record`` but its ``metadata.discovery``, its key and,
    for a repository, the URLs of the files it ``contains``."""
    mentions = set(_strings(record.asdict()))
    mentions.add(record.key)
    if isinstance(record, Repository):
        mentions.update(
            record.contains_artifact_url(path)
            for _label, path in record.contains
            if path
        )
    return mentions


def _is_dependent(record: CloudMapRecord) -> bool:
    metadata = getattr(record, "metadata", None)
    if not isinstance(metadata, CommonMetadata) or metadata.discovery is None:
        return False
    return metadata.discovery.dependent


class _Index:
    """The records each string leads to."""

    def __init__(self, records: Dict[Identity, CloudMapRecord]) -> None:
        self.named: Dict[str, Set[Identity]] = {}
        self.produced: Dict[str, Set[Identity]] = {}
        self.types: Dict[str, Set[Identity]] = {}
        self._found: Dict[str, Set[Identity]] = {}
        for identity, record in records.items():
            for form in _forms(record.key):
                self.named.setdefault(form, set()).add(identity)
            if isinstance(record, CloudType):
                self.types.setdefault(record.key.split("@")[0], set()).add(identity)
            for source in discovery_sources(record):
                for form in _forms(source):
                    self.produced.setdefault(form, set()).add(identity)

    def leads_to(self, text: str) -> Set[Identity]:
        """The records ``text`` names, those analyzing it produced, a file's
        repository and, for a type name, every type of that name in any
        namespace (``@...``)."""
        found = self._found.get(text)
        if found is None:
            found = set()
            for form in _forms(text):
                found |= self.named.get(form, set()) | self.produced.get(form, set())
                if "#:" in form:
                    found |= self.named.get(form.split("#:")[0], set())
                if "://" not in form:
                    found |= self.types.get(form.split("@")[0], set())
            self._found[text] = found
        return found


def find_garbage(store: CloudMapStore) -> List[CloudMapRecord]:
    """The records marked ``dependent`` that no unmarked record leads to, a
    record leading to what any string in it leads to (:py:meth:`_Index.leads_to`)."""
    records = {record_identity(record): record for record in store.records()}
    index = _Index(records)
    reached = {
        identity for identity, record in records.items() if not _is_dependent(record)
    }
    queue: Deque[Identity] = deque(reached)
    while queue:
        record = records[queue.popleft()]
        for text in _mentions(record):
            for target in index.leads_to(text):
                if target not in reached:
                    reached.add(target)
                    queue.append(target)
    return [record for identity, record in records.items() if identity not in reached]


def collect(store: CloudMapStore) -> List[CloudMapRecord]:
    """Delete what :py:func:`find_garbage` finds from ``store``, returning
    what's gone: a record stored under a key other than its own isn't."""
    garbage = find_garbage(store)
    for record in garbage:
        store.delete_record(record)
    left = {record_identity(record) for record in store.records()}
    removed = []
    for record in garbage:
        if record_identity(record) in left:
            logger.warning(
                "couldn't remove %s %s: it's stored under another key",
                type(record).__name__,
                record.key,
            )
        else:
            removed.append(record)
    return removed
