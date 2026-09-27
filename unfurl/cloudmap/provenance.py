# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""Provenance: which URL a cloud map record came from.

Analyzers produce records from a URL or from a file in a repository. Recording
that URL on each record is what lets a later run tell what a given source
contributed, and so notice when it stops contributing something -- see
:py:meth:`~unfurl.cloudmap.CloudMap.analyze_url`'s ``replace`` argument.

:py:class:`ProvenanceTrackingContext` wraps the :py:class:`AnalyzerContext`
analyzers are given and does that bookkeeping for them, so no analyzer has to
opt in.

Provenance is stored in the schema's existing ``metadata.discovery.sources``
field, alongside the URLs consulted for a record's metadata. Hence the split in
naming here: the tracking machinery is named for the concept, while anything
reading or writing the document keeps the document's own vocabulary
(:py:func:`~unfurl.tosca_plugins.cloudmap_defs.discovery_sources` and friends).
"""

from contextlib import contextmanager
import dataclasses
from typing import (
    Any,
    Dict,
    Generator,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    cast,
)
from typing_extensions import Literal

from ..logs import UnfurlLogger
from ..tosca_plugins.cloudmap_defs import (
    AnalyzerContext,
    AnalyzerLogger,
    Artifact,
    CloudMapRecord,
    CommonMetadata,
    CloudType,
    Component,
    Discovery,
    Instantiation,
    Repository,
    Service,
    get_repository_url,
    section_of,
)
from ..localenv import LocalEnv
from .db import CloudMapStore
from ..repo import split_git_url_with_commit, sanitize_url
from ..support import ContainerImage


def record_identity(record: "CloudMapRecord") -> Tuple[str, str]:
    """Identify a record across the cloudmap as ``(record type, key)``.

    Keys are only unique within a section (and ``CloudType`` is keyed by name
    rather than url), so the type name disambiguates -- a Service and an
    Instantiation can legitimately share a URL.
    """
    return (type(record).__name__, record.key)


def discovery_sources(record: "CloudMapRecord") -> List[str]:
    """The URLs recorded in ``record.metadata.discovery.sources``.

    Returns an empty list when the record has no discovery metadata. This is the
    single accessor for the shape, so callers don't repeat the ``None`` handling
    for the optional ``discovery``.

    Args:
        record: The record to read.

    Returns:
        The record's discovery sources, empty when it has none.
    """
    metadata = cast(CommonMetadata, record.metadata)  # type: ignore[attr-defined]
    return metadata.discovery.sources if metadata.discovery else []


def _replace_discovery_sources(record: "CloudMapRecord", sources: List[str]) -> None:
    """Replace the record's discovery sources, dropping the ``applied`` fields
    of any source that's gone.

    The counterpart to :py:func:`discovery_sources`; a no-op when the record has
    no discovery metadata, since there would be nothing to replace.
    """
    metadata = getattr(record, "metadata", None)
    discovery = getattr(metadata, "discovery", None)
    metadata = cast(CommonMetadata, record.metadata)  # type: ignore[attr-defined]
    if discovery is not None:
        discovery.sources = sources
        discovery.applied = {
            source: fields
            for source, fields in discovery.applied.items()
            if source in sources or _source_matches_key(source, record)
        }


def _escape_pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _unescape_pointer(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


# fields that are one leaf however they're structured: a repository host's
# refs, which change wholesale and aren't edited one at a time
_WHOLE_FIELDS = ("branches", "tags")


def field_pointers(record: Dict[str, Any]) -> Dict[str, Any]:
    """The leaves of a serialized record, by JSON pointer without the leading
    "/", as ``metadata.discovery.applied`` names them: every value that isn't a
    non-empty object -- so a list is one leaf, and so are the top-level
    ``branches`` and ``tags`` -- except ``metadata.discovery``.

    Must match ``field_pointers`` in ``rust/git-sync/src/formats/cloudmap.rs``.
    """
    fields: Dict[str, Any] = {}

    def walk(value: Any, tokens: List[str]) -> None:
        whole = len(tokens) == 1 and tokens[0] in _WHOLE_FIELDS
        if isinstance(value, dict) and value and not whole:
            for key, member in value.items():
                if tokens == ["metadata"] and key == "discovery":
                    continue
                walk(member, tokens + [key])
        else:
            fields["/".join(_escape_pointer(t) for t in tokens)] = value

    for key, value in record.items():
        walk(value, [key])
    return fields


def _tokens(pointer: str) -> List[str]:
    return [_unescape_pointer(t) for t in pointer.split("/")]


def _assemble(fields: Dict[str, Any]) -> Dict[str, Any]:
    """The inverse of :py:func:`field_pointers`."""
    record: Dict[str, Any] = {}
    for pointer, value in fields.items():
        *parents, last = _tokens(pointer)
        container = record
        for token in parents:
            container = container.setdefault(token, {})
        container[last] = value
    return record


def _default_leaves(record_type: type) -> Dict[str, Any]:
    """The serialized default value of each field of ``record_type`` that has
    one, by pointer, descending into fields whose default is a dataclass."""
    leaves: Dict[str, Any] = {}

    def walk(cls: type, prefix: str) -> None:
        for f in dataclasses.fields(cls):
            if f.default is not dataclasses.MISSING:
                value = f.default
            elif f.default_factory is not dataclasses.MISSING:
                value = f.default_factory()
            else:
                continue
            pointer = prefix + _escape_pointer(f.name)
            if pointer == "metadata/discovery":
                continue
            if dataclasses.is_dataclass(value) and not isinstance(value, type):
                walk(type(value), pointer + "/")
            else:
                asdict = getattr(value, "asdict", None)
                leaves[pointer] = asdict() if callable(asdict) else value

    walk(record_type, "")
    return leaves


def _related(pointer: str, pointers: Iterable[str]) -> bool:
    """Is one of ``pointers`` ``pointer`` itself, or inside or around it?"""
    return any(
        p == pointer or p.startswith(pointer + "/") or pointer.startswith(p + "/")
        for p in pointers
    )


def _applied(record: Dict[str, Any]) -> Dict[str, List[str]]:
    discovery = (record.get("metadata") or {}).get("discovery") or {}
    return discovery.get("applied") or {}


def _changed(before: Dict[str, Any], after: Dict[str, Any]) -> Set[str]:
    """The pointers added, removed or changed between ``before`` and ``after``."""
    return {
        p
        for p in before.keys() | after.keys()
        if p not in before or p not in after or before[p] != after[p]
    }


def strip_applied(previous: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
    """``record`` without the fields it changes from ``previous`` in its
    ``metadata.discovery.applied``, so analysis leaves them alone. The objects
    on the path are copied rather than modified."""
    applied = _applied(record)
    if not applied:
        return record
    changed = _changed(field_pointers(previous), field_pointers(record))
    # a source left with no fields keeps its entry: a record without
    # `applied` is one analysis has never tracked (see `_merge_owned`)
    remaining = {
        source: [p for p in fields if p not in changed]
        for source, fields in applied.items()
    }
    if remaining == applied:
        return record
    record = dict(record)
    metadata = dict(record["metadata"])
    discovery = dict(metadata["discovery"])
    discovery["applied"] = remaining
    metadata["discovery"] = discovery
    record["metadata"] = metadata
    return record


def _ownership(
    previous: Dict[str, Any],
    pointers: Dict[str, Any],
    source: str,
    produced_before: bool,
) -> Tuple[Set[str], Set[str]]:
    """The keys of ``previous`` (whose leaves are ``pointers``) that ``source``
    applied, and those any source applied. A record with no ``applied`` at all
    counts as applied entirely by ``source`` if it ``produced_before``."""
    applied = _applied(previous)
    if applied:
        owned = set(applied.get(source, []))
        return owned, {p for keys in applied.values() for p in keys}
    return (set(pointers) if produced_before else set()), set()


def _take_over(
    previous: Dict[str, Any],
    record: Dict[str, Any],
    source: str,
    produced_before: bool,
) -> Dict[str, List[str]]:
    """The ``applied`` lists after ``source`` writes ``record`` over
    ``previous`` unmerged: it applied the keys it changed along with the ones
    it had, and the other sources lose the keys it changed."""
    old, new = field_pointers(previous), field_pointers(record)
    changed = _changed(old, new)
    owned, _ = _ownership(previous, old, source, produced_before)
    applied = {
        s: [p for p in keys if p not in changed]
        for s, keys in _applied(previous).items()
        if s != source
    }
    applied[source] = sorted((owned & new.keys()) | (changed & new.keys()))
    return applied


def _merge_owned(
    previous: Dict[str, Any],
    incoming: Dict[str, Any],
    source: str,
    defaults: Dict[str, Any],
    produced_before: bool,
) -> Tuple[Dict[str, Any], List[str]]:
    """Merge what analyzing ``source`` produced into the record as it was.

    ``source`` can add keys the record doesn't have, but can only update or
    delete keys it applied last time, or keys no source has applied that
    still have their default value (``defaults``); any other key keeps its
    value. A record with no ``applied`` bookkeeping at all -- one written
    before it was kept -- counts as applied entirely by ``source`` if
    ``source`` produced it (``produced_before``).

    Returns:
        The merged record (without ``metadata.discovery``) and the keys
        ``source`` wrote.
    """
    old, new = field_pointers(previous), field_pointers(incoming)
    owned, claimed = _ownership(previous, old, source, produced_before)

    def changeable(pointer: str) -> bool:
        return pointer in owned or (
            pointer not in claimed
            and pointer in defaults
            and old[pointer] == defaults[pointer]
        )

    merged = {p: value for p, value in old.items() if not changeable(p)}
    written: List[str] = []
    for pointer, value in new.items():
        if pointer in old:
            if not changeable(pointer):
                continue
        elif _related(pointer, merged):
            continue  # it would replace or go inside a key that's kept
        merged[pointer] = value
        written.append(pointer)
    return _assemble(merged), sorted(written)


def _set_applied(record: "CloudMapRecord", applied: Dict[str, List[str]]) -> None:
    metadata = getattr(record, "metadata", None)
    if not isinstance(metadata, CommonMetadata):
        return
    if metadata.discovery is None:
        if not applied:
            return
        metadata.discovery = Discovery()
    metadata.discovery.applied = applied


def _transplant(record: "CloudMapRecord", data: Dict[str, Any]) -> None:
    """Set ``record``'s fields to the serialized ``data``, keeping its own
    ``metadata.discovery``, without replacing the object callers may hold."""
    assert dataclasses.is_dataclass(record) and not isinstance(record, type)
    data = dict(data)
    metadata = data.setdefault("metadata", {})
    discovery = getattr(getattr(record, "metadata", None), "discovery", None)
    if discovery is not None:
        metadata["discovery"] = discovery.asdict()
    record_fields = dataclasses.fields(record)
    # the key field comes first: `url`, or `name` for a CloudType
    data[record_fields[0].name] = record.key
    rebuilt = type(record)(**data)
    for f in record_fields:
        setattr(record, f.name, getattr(rebuilt, f.name))


def _source_matches_key(source: str, record: "CloudMapRecord") -> bool:
    if source == record.key:
        return True

    def canonical(url: str) -> Tuple[str, str]:
        repo_url, file_path, revision, commit = split_git_url_with_commit(url)
        repo = get_repository_url(repo_url)
        if repo.startswith("git://") and not repo.endswith(".git"):
            # repository keys carry the suffix but references often omit it
            repo += ".git"
        return repo, file_path

    # Comparing the strings isn't enough: a record is keyed by the canonical
    # form of its URL, so analyzing ``https://host/repo.git#:f`` yields an
    # artifact keyed ``git://host/repo.git#:f``.
    source_parts = canonical(source)
    record_parts = canonical(record.key)
    if source_parts == record_parts:
        return True
    # the record key is the repository that the source is in
    return (
        source_parts[0] == record_parts[0]
        and not record_parts[1]
        and bool(source_parts[1])
    )


def record_discovery_source(
    record: "CloudMapRecord",
    sources: Union[str, Sequence[str]],
    previous: Optional["CloudMapRecord"] = None,
) -> None:
    """Record ``sources`` as URLs this record was discovered from.

    Appends to ``record.metadata.discovery.sources``, creating the
    :class:`Discovery` when the record has none. Existing sources are preserved:
    a record can be discovered from several places, and
    :py:meth:`unfurl.cloudmap.CloudMap.analyze_url` only garbage-collects a
    record once *every* source has stopped producing it.

    Idempotent -- re-analyzing the same URL doesn't duplicate the entry.

    Args:
        record: The record to stamp. Its ``metadata`` is always present (each
            record's ``__post_init__`` coerces it to the right subclass), but
            ``metadata.discovery`` may be ``None``.
        sources: The URLs being analyzed, outermost first. Analysis nests -- a
            repository's files are analyzed as part of analyzing the
            repository -- and every enclosing URL is recorded, so a record can
            be collected by replacing either the file it came from or the
            repository that file is in. Each must be a valid URL:
            :py:meth:`Discovery.__post_init__` validates them.
        previous: The record being replaced, if any. Its sources are merged in
            so provenance survives a rebuild -- analyzers that construct a
            record from scratch (e.g. :py:func:`unfurl.cloudmap.oci.create_oci_artifact`,
            which assigns a fresh ``Discovery``) would otherwise drop the URLs
            that other analyzers had contributed.
    """
    if isinstance(sources, str):
        sources = [sources]
    # copy: `discovery_sources` returns the record's own list, and `previous`
    # is usually the record being re-added -- extending it in place would
    # duplicate its sources
    incoming = list(discovery_sources(previous)) if previous is not None else []
    # only add a source if its different from the record's key
    incoming.extend(s for s in sources if not _source_matches_key(s, record))
    existing = discovery_sources(record)
    new: List[str] = []
    for url in incoming:
        # `incoming` can repeat a url (the source may already be one of
        # `previous`'s), so dedupe against what's been collected as well
        if url not in existing and url not in new:
            new.append(url)
    if not new:
        return  # nothing to record; don't add an empty `discovery` block
    metadata = record.metadata  # type: ignore[attr-defined]
    if metadata.discovery is None:
        metadata.discovery = Discovery()
    metadata.discovery.sources.extend(new)


class Provenance:
    """What one analysis run produced.

    Attributes:
        touched: :py:func:`~unfurl.tosca_plugins.cloudmap_defs.record_identity`
            of every record the run added or confirmed is still in use.
        errors: How many analyzers raised. Non-zero means the run is
            incomplete, so records missing from ``touched`` can't be assumed
            orphaned.
    """

    def __init__(self) -> None:
        self.touched: Set[Tuple[str, str]] = set()
        self.errors: int = 0

    def __repr__(self) -> str:
        return f"Provenance(touched={len(self.touched)}, errors={self.errors})"


class AnalyzerLogFacade:
    """The only logging surface analyzers get.

    Wraps the real logger so a sandboxed analyzer can emit messages without
    reaching what a `logging.Logger` otherwise exposes -- ``handlers`` (and the
    live file objects behind them), ``root``, ``manager``, ``removeHandler``,
    ``setLevel``. None of those names trip the sandbox's policy, which denies a
    name only when it starts with "_" *and* contains "__", so an analyzer given
    the logger itself could forge entries or silence the log that records what
    it did -- the audit trail for the records it adds and deletes.
    """

    def __init__(self, logger: UnfurlLogger) -> None:
        # mangled to _AnalyzerLogFacade__logger, so the sandbox denies it
        self.__logger = logger

    def trace(self, msg: str, *args: Any, exc_info: Any = None) -> None:
        self.__logger.trace(msg, *args, exc_info=exc_info)

    def debug(self, msg: str, *args: Any, exc_info: Any = None) -> None:
        self.__logger.debug(msg, *args, exc_info=exc_info)

    def verbose(self, msg: str, *args: Any, exc_info: Any = None) -> None:
        self.__logger.verbose(msg, *args, exc_info=exc_info)

    def info(self, msg: str, *args: Any, exc_info: Any = None) -> None:
        self.__logger.info(msg, *args, exc_info=exc_info)

    def warning(self, msg: str, *args: Any, exc_info: Any = None) -> None:
        self.__logger.warning(msg, *args, exc_info=exc_info)

    def error(self, msg: str, *args: Any, exc_info: Any = None) -> None:
        self.__logger.error(msg, *args, exc_info=exc_info)


class ProvenanceTrackingContext(AnalyzerContext):
    """An :py:class:`AnalyzerContext` that attributes records to a source URL.

    Wraps the context analyzers would otherwise use and adds two things:

    * records added through it are stamped with the URL being analyzed;
    * records merely *looked up* through it are marked as still in use.

    The second is what makes the bookkeeping invisible to analyzers. They
    routinely skip re-adding a record that already exists -- ``if
    ctx.get_type(name) is None: ...`` -- and a record that is never mentioned
    looks orphaned to :py:meth:`replace_from_source`, which would delete it and
    leave the next run to recreate it. Marking on lookup means "I checked, it's
    still needed" is recorded without the analyzer having to say so.
    """

    def __init__(self, context: CloudMapStore) -> None:
        # no cloudmap of its own: `analyze_url` and `_local__env` delegate to
        # the store this wraps, which is the one that has it
        super().__init__()
        self.__context = context
        self._sources: List[str] = []
        # One per tracker, shared by every nested block: what a nested
        # analysis touches counts for the enclosing one too, or the outer
        # sweep would collect records the run just confirmed are in use.
        self.provenance = Provenance()
        # Each record as it was when first read through this context. Callers
        # modify the records they read before adding them back, so the store
        # can't tell what a write changes -- this can.
        self._snapshots: Dict[Tuple[str, str], Dict[str, Any]] = {}

    # --- Tracking ---

    @contextmanager
    def _tracking_provenance(self, source: str) -> Generator[Provenance, None, None]:
        """Also attribute records added within this block to ``source``.

        Analysis nests -- analyzing a repository analyzes its files, and an
        analyzer can analyze another URL -- so sources are kept on a stack and
        :py:meth:`add_record` stamps *all* of them. A record produced while
        analyzing a repository's file is discovered from that file and from the
        repository, so replacing either one can collect it.

        Args:
            source: The URL being analyzed.

        Yields:
            The :py:class:`Provenance` for the analysis in progress. A fresh
            one is started at the outermost block: this tracker outlives any
            single analysis, and carrying `touched` across two of them would
            spare records the later one never confirmed.
        """
        if not self._sources:
            self.provenance = Provenance()
        self._sources.append(source)
        try:
            yield self.provenance
        finally:
            self._sources.pop()

    def _mark_seen(self, record: Optional[CloudMapRecord]) -> None:
        """Note that ``record`` is still in use by the analysis in progress."""
        if record is not None:
            self.provenance.touched.add(record_identity(record))

    def _read(self, record: Optional[CloudMapRecord]) -> None:
        """Note that ``record`` was looked up: it's still in use, and this is
        how it was before whoever looked it up changes it."""
        if record is not None:
            self._mark_seen(record)
            self._snapshots.setdefault(record_identity(record), record.asdict())

    def _mark_failed(self) -> None:
        """Note that an analyzer raised, making this analysis incomplete.

        Analyzer errors are caught and logged rather than propagated, so
        without this a failed run would look like "produced nothing" and
        :py:meth:`replace_from_source` would delete every record attributed to
        the URL.
        """
        self.provenance.errors += 1

    # --- AnalyzerContext methods: ---

    def add_record(self, record: CloudMapRecord) -> None:
        """Add ``record``, attributing it to every URL being analyzed.

        During analysis the record is merged with the one it replaces by field
        ownership (see ``metadata.discovery.applied``): the innermost source
        being analyzed only overwrites the fields it wrote last time, unless
        ``record`` is the stored record itself, retrieved and updated. Any other
        write gives up the ownership of the fields it changes.
        """
        identity = record_identity(record)
        stored = self.__context.get_record(section_of(record), record.key)
        previous = self._snapshots.get(identity)
        if previous is None and stored is not None:
            previous = stored.asdict()
        if self._sources:
            # the url being analyzed, which can be the record's own
            source = self._sources[-1]
            if previous is None:
                applied = {source: sorted(field_pointers(record.asdict()))}
            else:
                incoming = record.asdict()
                discovery = (previous.get("metadata") or {}).get("discovery") or {}
                produced_before = _source_matches_key(
                    source, record
                ) or source in discovery.get("sources", [])
                if record is stored:
                    # the analyzer updated the record it retrieved rather than
                    # producing a new one, so its changes are deliberate
                    applied = _take_over(previous, incoming, source, produced_before)
                else:
                    merged, written = _merge_owned(
                        previous,
                        incoming,
                        source,
                        _default_leaves(type(record)),
                        produced_before,
                    )
                    if field_pointers(merged) != field_pointers(incoming):
                        _transplant(record, merged)
                    applied = {
                        s: fields
                        for s, fields in _applied(previous).items()
                        if s != source
                    }
                    applied[source] = written
            record_discovery_source(
                record,
                self._sources,
                # Analyzers may rebuild a record from scratch; merge the
                # sources of the one being replaced so other analyzers'
                # provenance isn't dropped.
                previous=stored,
            )
            _set_applied(record, applied)
        elif previous is not None:
            data = record.asdict()
            stripped = strip_applied(previous, data)
            if stripped is not data:
                _set_applied(record, _applied(stripped))
        self._mark_seen(record)
        self._snapshots[identity] = record.asdict()
        self.__context.add_record(record)

    def delete_record(self, record: CloudMapRecord) -> None:
        self._snapshots.pop(record_identity(record), None)
        self.__context.delete_record(record)

    def add_image_artifact(self, image: ContainerImage) -> Artifact:
        # Not attributed to the url being analyzed: the image's provenance is
        # the registry it was read from, which `create_oci_artifact` already
        # records, and whatever referenced it says so in its `references`.
        # Attributing it here would also be unstable -- the artifact is built
        # once, by whichever analyzer reaches it first.
        return self.__context.add_image_artifact(image)

    def analyze_url(
        self,
        url: str,
        analyze: Literal["yes", "no", "save-only", "default"] = "default",
        replace: bool = False,
    ) -> Optional[CloudMapRecord]:
        return self.__context.analyze_url(url, analyze, replace)

    # --- Look up records (marking whatever is found as still in use) ---

    def get_record(self, section: str, key: str) -> Optional[CloudMapRecord]:
        return self.__context.get_record(section, key)

    def get_repository(self, r: Union[str, Repository]) -> Optional[Repository]:
        found = self.__context.get_repository(r)
        self._read(found)
        return found

    def get_artifact(self, url: str) -> Optional[Artifact]:
        found = self.__context.get_artifact(url)
        self._read(found)
        return found

    def get_service(self, url: str) -> Optional[Service]:
        found = self.__context.get_service(url)
        self._read(found)
        return found

    def get_component(self, url: str) -> Optional[Component]:
        found = self.__context.get_component(url)
        self._read(found)
        return found

    def get_instantiation(self, url: str) -> Optional[Instantiation]:
        found = self.__context.get_instantiation(url)
        self._read(found)
        return found

    def get_type(self, name: str) -> Optional[CloudType]:
        found = self.__context.get_type(name)
        self._read(found)
        return found

    # --- Iterate records ---

    def find_repositories(self) -> Iterable[Repository]:
        return self.__context.find_repositories()

    def find_artifacts(self, type: str = "") -> Iterable[Artifact]:
        return self.__context.find_artifacts(type)

    def find_services(self, type: str = "") -> Iterable[Service]:
        return self.__context.find_services(type)

    def find_components(self, type: str = "") -> Iterable[Component]:
        return self.__context.find_components(type)

    def find_instantiations(self, type: str = "") -> Iterable[Instantiation]:
        return self.__context.find_instantiations(type)

    def find_types(self) -> Iterable[CloudType]:
        return self.__context.find_types()

    # --- Pass-throughs ---

    @property
    def logger(self) -> AnalyzerLogger:
        # a facade, not the store's logger: see `AnalyzerLogFacade`. Built per
        # access rather than cached, so it can't go stale if the store's logger
        # is replaced.
        return AnalyzerLogFacade(cast(UnfurlLogger, self.__context.logger))

    @logger.setter
    def logger(self, value: UnfurlLogger) -> None:
        self.__context.logger = value

    @property
    def do_analysis(self) -> bool:
        # read through rather than snapshot: analysis can turn it off partway
        # (a url naming one file shouldn't analyze the whole repository)
        return self.__context.do_analysis

    @do_analysis.setter
    def do_analysis(self, value: bool) -> None:
        self.__context.do_analysis = value

    @property
    def _local__env(self) -> Optional[LocalEnv]:
        return self.__context._local__env

    # --- Replace records a source no longer produces ---

    @staticmethod
    def matches_source(source: str, url: str) -> bool:
        """Was ``source`` contributed by analyzing ``url``?

        Exact matches, plus the artifacts of a repository's own files:
        analyzing ``git://host/repo.git`` runs the file analyzers, and those
        records are attributed to ``git://host/repo.git#:<path>`` -- or, for a
        branch, analyzing ``git://host/repo.git#<branch>`` attributes them to
        ``git://host/repo.git#<branch>:<path>``. Without the prefix a
        repository-level replace would never collect the records for files that
        have since been deleted.
        """
        if source == url or source.startswith(url + "#:"):
            return True
        # likewise the files of a branch: ``repo#branch`` -> ``repo#branch:<path>``
        revision = url.partition("#")[2]
        return bool(revision) and ":" not in revision and source.startswith(url + ":")

    def find_by_source(self, url: str) -> Dict[Tuple[str, str], CloudMapRecord]:
        """Every record attributed to ``url``, keyed by ``record_identity``.

        Walks each section: records are indexed by key only, so there is no way
        to look them up by their discovery metadata. Version variants are
        skipped -- they're stored under their parent, which is what gets added
        and removed.
        """
        found: Dict[Tuple[str, str], CloudMapRecord] = {}
        for records in (
            self.find_repositories(),
            self.find_artifacts(),
            self.find_components(),
            self.find_services(),
            self.find_instantiations(),
            self.find_types(),
        ):
            for record in records:
                if getattr(record, "_parent", None):
                    continue
                if any(
                    self.matches_source(source, url)
                    for source in discovery_sources(record)
                ):
                    found[record_identity(record)] = record
        return found

    def replace_from_source(self, url: str, provenance: Provenance) -> None:
        """Drop ``url`` from records it used to produce but no longer does.

        A record is deleted only once *every* source has stopped producing it,
        so records that other URLs still contribute to survive with their
        remaining sources.

        The sweep is skipped when the analysis didn't complete: analyzer errors
        are logged rather than raised, so a failed run looks the same as one
        that produced nothing, and deleting on that basis would discard records
        that are still valid.
        """
        if provenance.errors:
            self.logger.warning(
                "not replacing records discovered from %s: %s analyzer(s) failed",
                sanitize_url(url),
                provenance.errors,
            )
            return
        if not provenance.touched:
            self.logger.warning(
                "not replacing records discovered from %s: it produced no records",
                sanitize_url(url),
            )
            return
        for identity, record in self.find_by_source(url).items():
            if identity in provenance.touched:
                continue
            remaining = [
                source
                for source in discovery_sources(record)
                # Symmetric, unlike the search above: a record analyzing
                # `repo#:file.yaml` produced carries `repo` too, and that
                # claim was made by this same analysis, so replacing the file
                # drops it as well. A record another file also produces keeps
                # its own `repo#:other.yaml`, so it still survives.
                if not (
                    self.matches_source(source, url) or self.matches_source(url, source)
                )
            ]
            if remaining:
                _replace_discovery_sources(record, remaining)
                self.logger.verbose(
                    "%s %s is no longer discovered from %s",
                    identity[0],
                    identity[1],
                    sanitize_url(url),
                )
            else:
                self.logger.verbose(
                    "removing %s %s: no longer discovered from %s",
                    identity[0],
                    identity[1],
                    sanitize_url(url),
                )
                self.delete_record(record)
