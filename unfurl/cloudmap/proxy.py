# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""Client-side :class:`~unfurl.tosca_plugins.cloudmap_defs.CloudMapView`
implementation that talks to a remote unfurl-server's ``/cloudmap``
endpoint.

The proxy holds a :class:`CloudMapCache` (an internal
:class:`CloudMapDB` subclass) for the in-memory mirror of records seen
from the server. Reads fall through to a remote ``GET /cloudmap`` when
the cache misses; writes are buffered until :meth:`CloudMapProxy.save`
POSTs the diff. Per-record OCC tokens (``unfurl.server.version`` /
``unfurl.server.commit``) round-trip on the cached payload.
"""

from __future__ import annotations

import base64
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)
from urllib.parse import parse_qsl, urlparse, urlunparse

import requests

from . import CloudMapDB
from .db import CloudMapStore
from ..logs import getLogger, UnfurlLogger
from ..tosca_plugins.cloudmap_defs import (
    section_of,
    Artifact,
    CloudMapRecord,
    CloudMapView,
    CloudType,
    Component,
    Instantiation,
    Repository,
    Service,
)
from ..util import UnfurlError

if TYPE_CHECKING:
    from . import CloudMap
    from ..support import ContainerImage

logger = getLogger("unfurl.cloudmap.proxy")

__all__ = [
    "CloudMapCache",
    "CloudMapProxy",
    "CloudMapProxyError",
    "CloudMapProxyConflict",
]


# Tokens stamped onto every record returned by GET /cloudmap. The
# server expects clients to echo back ``version`` and ``commit`` per
# record on POST so the rust handler can run its OCC check
# (cloudmap.rs:280-289). ``id`` is the row's primary key — stamped
# alongside ``version`` so callers have a stable identifier they can
# pass back via the ``find_records_follow`` ``exclude`` argument; the
# rust POST handler discards it before persisting.
_OCC_VERSION_KEY = "unfurl.server.version"
_OCC_COMMIT_KEY = "unfurl.server.commit"
_OCC_ID_KEY = "unfurl.server.id"

# Private attribute names used to stash the per-record OCC tokens on
# the cached dataclass instances. Stashing them as instance attrs
# avoids keeping a parallel `(section, key) -> payload` dict alongside
# CloudMapDB's storage; ``_stage_write`` reads them off the record on
# its way to the POST and ``save()`` writes them back in place after
# a successful response. Names are leading-underscore so they aren't
# round-tripped through ``record.asdict()`` (which uses
# ``dataclasses.asdict`` — only declared fields).
_OCC_VERSION_ATTR = "_unfurl_server_version"
_OCC_COMMIT_ATTR = "_unfurl_server_commit"
_OCC_ID_ATTR = "_unfurl_server_id"

# All cloudmap sections, in load order matching CloudMapDB._load.
# Each section name is also the attribute name on CloudMapDB
# (`self.repositories`, `self.artifacts`, ...) so we can resolve a
# (section, key) lookup with `getattr(cache, section)[key]`.
_SECTIONS: Tuple[str, ...] = (
    "types",
    "artifacts",
    "repositories",
    "services",
    "components",
    "instantiations",
)


def _set_occ_tokens(
    record: Any,
    version: Optional[int],
    commit: Optional[str],
    id: Optional[int] = None,
) -> None:
    """Stash OCC tokens on a record as private instance attrs.

    ``version`` / ``commit`` are stored even when ``None`` (matches
    the server's notion that an in-flight rust-local write has
    commit=null). ``id`` is the row's primary key — used to populate
    the ``exclude`` query parameter on subsequent ``follow``
    fetches, so the server skips records the proxy already holds.
    """
    setattr(record, _OCC_VERSION_ATTR, version)
    setattr(record, _OCC_COMMIT_ATTR, commit)
    setattr(record, _OCC_ID_ATTR, id)


def _get_occ_tokens(
    record: Any,
) -> Tuple[Optional[int], Optional[str]]:
    """Read OCC tokens off a record. Returns ``(None, None)`` for
    records the proxy has never seen from the server (so a brand-new
    ``add_*`` doesn't echo stale or fabricated tokens)."""
    return (
        getattr(record, _OCC_VERSION_ATTR, None),
        getattr(record, _OCC_COMMIT_ATTR, None),
    )


def _get_occ_id(record: Any) -> Optional[int]:
    """Read the server-side primary-key id off a record, if known."""
    id_ = getattr(record, _OCC_ID_ATTR, None)
    return id_ if isinstance(id_, int) else None


class CloudMapProxyError(UnfurlError):
    """Base class for CloudMapProxy errors."""


class CloudMapProxyConflict(CloudMapProxyError):
    """Raised on HTTP 409 from POST /cloudmap.

    Carries the (section, key, actual) triple identifying the *first*
    conflicting record (the only one in atomic mode), plus
    :attr:`applied` and :attr:`failed` from the response body so the
    caller can reconcile per-record results when ``atomic=False``.

    In atomic mode (the default) the server rolls back the whole
    batch, so :attr:`applied` is empty and :attr:`failed` always
    contains exactly one entry (matching the singleton section/key).
    In non-atomic mode :attr:`applied` lists every record that
    committed despite the failure, and :attr:`failed` lists every
    record that did not.
    """

    def __init__(
        self,
        section: str,
        key: str,
        actual: Any,
        message: Optional[str] = None,
        applied: Optional[List[Tuple[str, str, int]]] = None,
        failed: Optional[List[Tuple[str, str, Optional[str], Optional[str]]]] = None,
    ) -> None:
        super().__init__(message or f"cloudmap conflict on {section}/{key}")
        self.section = section
        self.key = key
        self.actual = actual
        self.applied: List[Tuple[str, str, int]] = applied or []
        self.failed: List[Tuple[str, str, Optional[str], Optional[str]]] = failed or []


class CloudMapCache(CloudMapDB):
    """Internal in-memory cache used by :class:`CloudMapProxy`.

    Extends :class:`CloudMapDB` with:

    - ``_section_loaded`` — sections that have been fully enumerated
      (so :meth:`CloudMapProxy.find_*` doesn't refetch).
    - ``_negative`` — keys that 404'd on the server (negative cache).
    - ``path`` — the url of the cloudmap the server serves (the proxy's
      endpoint), so references that name a cloudmap document can be told
      apart from references to it.
    - ``_max_version`` / ``_latest_commit`` — highest OCC tokens
      observed across the whole cache.

    OCC tokens *for individual records* live on the dataclass
    instances themselves, set via :func:`_set_occ_tokens` at hydrate
    time. The proxy reads them off the record on its way to the POST
    and writes them back in place after a successful response — no
    parallel ``(section, key) -> payload`` dict.
    """

    def __init__(self, url: str = "") -> None:
        # Empty in-memory document; nothing is read from disk and the schema
        # validator never runs on the cache because the server is the source
        # of truth. ``url`` is where the cloudmap this mirrors is served, so
        # it identifies this document (see CloudMapDB._matches_cloudmap_url).
        super().__init__(path=url, contents=self.make_empty_cloudmap(), validate=False)
        self._section_loaded: Set[str] = set()
        self._negative: Set[Tuple[str, str]] = set()
        self._max_version: int = 0
        self._latest_commit: Optional[str] = None

    # -- ingest helpers -------------------------------------------------

    def _note_tokens(self, payload: Dict[str, Any]) -> None:
        """Update ``_max_version`` / ``_latest_commit`` from a fetched
        payload. Does *not* mutate the payload.
        """
        version = payload.get(_OCC_VERSION_KEY)
        commit = payload.get(_OCC_COMMIT_KEY)
        if isinstance(version, int) and version > self._max_version:
            self._max_version = version
            # only set latest commit if the version advanced
            if commit:
                self._latest_commit = commit
        elif commit and self._latest_commit is None:
            self._latest_commit = commit

    def _hydrate_one(self, section: str, key: str, payload: Dict[str, Any]) -> None:
        """Construct the dataclass for ``payload``, stash it via the
        inherited ``add_record``, and stamp the per-record OCC tokens
        onto the new instance.
        """
        # The OCC keys aren't valid dataclass kwargs, so pop them
        # off (capturing the values) before passing the rest to the
        # constructor. Work on a fresh copy because some constructors
        # mutate the dict via `.pop("url", key)`.
        clean = dict(payload)
        version = clean.pop(_OCC_VERSION_KEY, None)
        commit = clean.pop(_OCC_COMMIT_KEY, None)
        rid = clean.pop(_OCC_ID_KEY, None)

        record: Any
        if section == "repositories":
            # Backwards compatibility: migrate the deprecated `notable` key to
            # `contains`, same as CloudMapDB._load() does for local YAML.
            old_notable = clean.pop("notable", None)
            record = Repository(url=clean.pop("git", key), **clean)
            if isinstance(old_notable, dict):
                from .analyzers import migrate_old_notable_format

                migrate_old_notable_format(self, record, old_notable)
        elif section == "artifacts":
            record = Artifact(url=clean.pop("url", key), **clean)
        elif section == "services":
            record = Service(url=clean.pop("url", key), **clean)
        elif section == "components":
            record = Component(url=clean.pop("url", key), **clean)
        elif section == "instantiations":
            record = Instantiation(url=clean.pop("url", key), **clean)
        elif section == "types":
            record = CloudType(name=clean.pop("name", key), **clean)
        else:
            # Server may add new sections before the client knows
            # about them — silently ignore.
            return
        self.add_record(record)

        _set_occ_tokens(
            record,
            version if isinstance(version, int) else None,
            commit if isinstance(commit, str) and commit else None,
            rid if isinstance(rid, int) else None,
        )

    def ingest_document(self, doc: Optional[Dict[str, Any]]) -> None:
        """Merge a CloudMap-shaped dict into the cache."""
        if not isinstance(doc, dict):
            return
        for section in _SECTIONS:
            entries = doc.get(section)
            if not isinstance(entries, dict):
                continue
            for key, payload in entries.items():
                if not isinstance(payload, dict):
                    continue
                self._note_tokens(payload)
                self._hydrate_one(section, key, payload)
                self._negative.discard((section, key))

    def ingest_pair(self, pair: Any) -> None:
        """``GET /cloudmap`` returns ``[primary, followed]`` — merge both."""
        if not isinstance(pair, list):
            return
        for half in pair:
            self.ingest_document(half)


DEFAULT_REQUEST_TIMEOUT = 30.0


class CloudMapProxy(CloudMapStore):
    """:class:`CloudMapView` implementation backed by a remote
    unfurl-server.

    Reads:
      - :meth:`get_X` first consults the local :class:`CloudMapCache`;
        on a miss it issues
        ``GET /cloudmap?kind=<section>&key=<url>&follow=<N>`` and
        ingests both the primary record and any followed records.
      - :meth:`find_X` lazily fetches the section once
        (``GET /cloudmap?kind=<section>``) and returns an iterator. The
        endpoint will gain paging soon — the iterator shape lets us
        thread that through without a public API change.

    Writes:
      - :meth:`add_X` updates the cache and stages the dict form in
        ``_pending_writes``. If the section/key was previously
        fetched, the cached payload's
        ``unfurl.server.{version,commit}`` keys are copied onto the
        pending payload so they round-trip back on the next POST.
      - :meth:`save` POSTs the staged diff (envelope + sections) and
        clears the buffer on success.

    Refreshing:
      - :meth:`refresh` issues
        ``GET /cloudmap?since_version=<self._cache._max_version>`` and
        merges any deltas. Useful for long-lived proxy instances.
    """

    def __init__(
        self,
        base_url: str,
        *,
        username: Optional[str] = None,
        private_token: Optional[str] = None,
        follow_depth: int = 1024,
        session: Optional[requests.Session] = None,
        timeout: Optional[float] = None,
        logger: UnfurlLogger = logger,
    ) -> None:
        # ``base_url`` may carry query parameters (e.g.
        # ``http://server/?auth_project=foo``); split them out so each
        # request preserves them while still allowing per-call extras.
        parsed = urlparse(base_url)
        path = parsed.path.rstrip("/")
        self._endpoint = urlunparse(
            parsed._replace(path=f"{path}/cloudmap", query="", fragment="")
        )
        self._base_query: List[Tuple[str, str]] = parse_qsl(
            parsed.query, keep_blank_values=True
        )
        self._username = username
        self._private_token = private_token
        self._follow_depth = follow_depth
        self._session = session or requests.Session()
        self._timeout = timeout if timeout is not None else DEFAULT_REQUEST_TIMEOUT
        self.logger: UnfurlLogger = logger

        # In-memory mirror of records observed from the server. The endpoint
        # identifies the cloudmap document it mirrors.
        self._cache = CloudMapCache(self._endpoint)

        # Buffered writes: section -> key -> payload dict (already
        # carries OCC keys when applicable).
        self._pending_writes: Dict[str, Dict[str, Dict[str, Any]]] = {}

    # -----------------------------------------------------------------
    # HTTP plumbing
    # -----------------------------------------------------------------

    def _query_params(self, **extra: Any) -> List[Tuple[str, str]]:
        # Merge the base_url's query params with per-call extras. A list
        # of pairs is used (not a dict) so that base_url query keys with
        # multiple values round-trip unchanged.
        params: List[Tuple[str, str]] = list(self._base_query)
        for k, v in extra.items():
            if v is not None:
                params.append((k, str(v)))
        return params

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self._username and self._private_token:
            creds = f"{self._username}:{self._private_token}".encode("utf-8")
            headers["X-Git-Credentials"] = base64.b64encode(creds).decode("ascii")
        return headers

    def _get(self, params: List[Tuple[str, str]]) -> Any:
        r = self._session.get(
            self._endpoint,
            params=params,
            headers=self._headers(),
            timeout=self._timeout,
        )
        if r.status_code == 404:
            return None
        if not r.ok:
            raise CloudMapProxyError(
                f"GET {self._endpoint} -> {r.status_code}: {r.text}"
            )
        return r.json()

    def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        # ``cloudmap_path`` scopes reads via the query string but writes via the
        # request body, so carry it over from ``base_url``. Without this a proxy
        # built for one file would read from it and write to the default one.
        if "cloudmap_path" not in body:
            for key, value in self._base_query:
                if key == "cloudmap_path" and value:
                    body = dict(body, cloudmap_path=value)
                    break
        r = self._session.post(
            self._endpoint,
            params=self._query_params(),
            json=body,
            headers=self._headers(),
            timeout=self._timeout,
        )
        if r.status_code == 409:
            try:
                detail = r.json()
            except ValueError:
                detail = {"error": r.text}
            applied_raw = detail.get("applied") or []
            failed_raw = detail.get("failed") or []
            applied: List[Tuple[str, str, int]] = []
            for entry in applied_raw:
                if not isinstance(entry, dict):
                    continue
                applied.append(
                    (
                        str(entry.get("section", "")),
                        str(entry.get("key", "")),
                        int(entry.get("version") or 0),
                    )
                )
            failed: List[Tuple[str, str, Optional[str], Optional[str]]] = []
            for entry in failed_raw:
                if not isinstance(entry, dict):
                    continue
                actual = entry.get("actual")
                err_kind = entry.get("error")
                failed.append(
                    (
                        str(entry.get("section", "")),
                        str(entry.get("key", "")),
                        str(actual) if actual is not None else None,
                        str(err_kind) if err_kind is not None else None,
                    )
                )
            raise CloudMapProxyConflict(
                section=detail.get("section", ""),
                key=detail.get("key", ""),
                actual=detail.get("actual"),
                message=str(detail),
                applied=applied,
                failed=failed,
            )
        if not r.ok:
            raise CloudMapProxyError(
                f"POST {self._endpoint} -> {r.status_code}: {r.text}"
            )
        return r.json()

    # -----------------------------------------------------------------
    # Get-by-key — try cache, fall back to the server
    # -----------------------------------------------------------------

    def _cached_record_ids(self) -> List[int]:
        """Collect every ``unfurl.server.id`` known to the cache.

        Used for the ``exclude`` query parameter on ``follow`` fetches
        — the server then skips records the proxy already holds.
        Records are duplicated under each version's url, so dedupe.
        """
        ids: Set[int] = set()
        for section in _SECTIONS:
            for record in getattr(self._cache, section).values():
                rid = _get_occ_id(record)
                if rid is not None:
                    ids.add(rid)
        return sorted(ids)

    def _fetch_by_key(self, section: str, key: str) -> None:
        # the server looks up records by their key, so resolve pseudo-URLs and
        # json pointer fragments the same way the local cache does
        # (a key path into the record is dropped, the record itself is fetched)
        cloudmap_url, key, _path = CloudMapDB._normalize_url(key, section)
        if not self._cache._matches_cloudmap_url(cloudmap_url):
            # the record lives in another cloudmap document, not the one this
            # server serves at self._endpoint
            return
        if (section, key) in self._cache._negative:
            return
        cached_ids = self._cached_record_ids()
        params = self._query_params(
            kind=section,
            key=key,
            follow=self._follow_depth,
            exclude=",".join(str(i) for i in cached_ids) if cached_ids else None,
        )
        body = self._get(params)
        if body is None:
            self._cache._negative.add((section, key))
            return
        self._cache.ingest_pair(body)

    def get_artifact(self, url: str) -> Optional[Artifact]:
        hit = self._cache.get_artifact(url)
        if hit is not None:
            return hit
        self._fetch_by_key("artifacts", url)
        return self._cache.get_artifact(url)

    def get_service(self, url: str) -> Optional[Service]:
        hit = self._cache.get_service(url)
        if hit is not None:
            return hit
        self._fetch_by_key("services", url)
        return self._cache.get_service(url)

    def get_component(self, url: str) -> Optional[Component]:
        hit = self._cache.get_component(url)
        if hit is not None:
            return hit
        self._fetch_by_key("components", url)
        return self._cache.get_component(url)

    def get_instantiation(self, url: str) -> Optional[Instantiation]:
        hit = self._cache.get_instantiation(url)
        if hit is not None:
            return hit
        self._fetch_by_key("instantiations", url)
        return self._cache.get_instantiation(url)

    def get_type(self, name: str) -> Optional[CloudType]:
        hit = self._cache.get_type(name)
        if hit is not None:
            return hit
        self._fetch_by_key("types", name)
        return self._cache.get_type(name)

    def get_repository(self, r: Union[str, Repository]) -> Optional[Repository]:
        hit = self._cache.get_repository(r)
        if hit is not None:
            return hit
        # Mirror CloudMapDB.get_repository's URL coercion before going
        # to the network so the cache key matches the server's
        # (_fetch_by_key normalizes fragment and pseudo-URL references).
        url = r.url if isinstance(r, Repository) else str(r)
        self._fetch_by_key("repositories", url)
        return self._cache.get_repository(r)

    # -----------------------------------------------------------------
    # find_* — per-section fetch, returns an iterator
    # -----------------------------------------------------------------

    def _ensure_section(self, section: str) -> None:
        """Fetch the named section once; subsequent calls are local.

        Paging hook: when the server gains ``page`` / ``page_token``
        query params, loop here and pass each page through
        :meth:`CloudMapCache.ingest_document` before marking the
        section loaded.
        """
        if section in self._cache._section_loaded:
            return
        params = self._query_params(kind=section)
        body = self._get(params)
        # Body is `[primary, followed]`; with no `key` and `follow=0`,
        # `followed` is `{}` — `ingest_pair` handles both.
        self._cache.ingest_pair(body)
        self._cache._section_loaded.add(section)

    def find_artifacts(self, type: str = "") -> Iterator[Artifact]:
        self._ensure_section("artifacts")
        yield from self._cache.find_artifacts(type)

    def find_services(self, type: str = "") -> Iterator[Service]:
        self._ensure_section("services")
        yield from self._cache.find_services(type)

    def find_components(self, type: str = "") -> Iterator[Component]:
        self._ensure_section("components")
        yield from self._cache.find_components(type)

    def find_instantiations(self, type: str = "") -> Iterator[Instantiation]:
        self._ensure_section("instantiations")
        yield from self._cache.find_instantiations(type)

    def find_types(self) -> Iterator[CloudType]:
        self._ensure_section("types")
        yield from self._cache.find_types()

    def find_repositories(self) -> Iterator[Repository]:
        self._ensure_section("repositories")
        yield from self._cache.find_repositories()

    # -----------------------------------------------------------------
    # add_* — local update + buffered write
    # -----------------------------------------------------------------

    def _stage_write(self, section: str, key: str, record: Any) -> None:
        payload = record.asdict()
        # Read OCC tokens from the dataclass instance (set by
        # `_set_occ_tokens` at ingest time, or by an earlier `save()`
        # via the response's queueid/commit). Brand-new records the
        # proxy has never seen from the server have no tokens — those
        # POSTs go through with no OCC fields and the server stamps
        # fresh ones.
        version, commit = _get_occ_tokens(record)
        if version is not None:
            payload[_OCC_VERSION_KEY] = version
        if commit is not None:
            payload[_OCC_COMMIT_KEY] = commit
        self._pending_writes.setdefault(section, {})[key] = payload

    @staticmethod
    def _section_for(record: CloudMapRecord) -> str:
        """Map a record's concrete type to its cloudmap section name."""
        return section_of(record)

    # --- Stand in for CloudMapDB ---

    def get_record(self, section: str, key: str) -> Optional[Any]:
        """Return the cached record under ``(section, key)``, if any."""
        return self._cache.get_record(section, key)

    def add_image_artifact(self, image: "ContainerImage") -> Artifact:
        """Add an OCI artifact (and its build instantiation) for ``image``."""
        from .oci import create_oci_artifact

        artifact, instantiation, _fetch = create_oci_artifact(image)
        if instantiation:
            self.add_record(instantiation)
        self.add_record(artifact)
        return artifact

    def add_record(self, record: CloudMapRecord) -> None:
        # Update the local cache and stage the dict form for the next
        # POST. The (section, key) pair needed by `_stage_write` is
        # derived from the concrete record type — same dispatch that
        # `CloudMapDB.add_record` runs internally.
        section = self._section_for(record)
        self._cache.add_record(record)
        self._stage_write(section, record.key, record)

    def delete_record(self, record: CloudMapRecord) -> None:
        # Inverse of add_record: drop from the local cache and stage a
        # delete marker so the next POST tombstones the record on the
        # server. The endpoint signals deletes via
        # `unfurl.server.deleted: true` on the record payload (see
        # endpoints.py post_cloudmap); OCC tokens still round-trip so
        # the server's per-record concurrency check applies to the
        # delete the same way it does to a write.
        section = self._section_for(record)
        self._cache.delete_record(record)
        payload: Dict[str, Any] = {"unfurl.server.deleted": True}
        version, commit = _get_occ_tokens(record)
        if version is not None:
            payload[_OCC_VERSION_KEY] = version
        if commit is not None:
            payload[_OCC_COMMIT_KEY] = commit
        self._pending_writes.setdefault(section, {})[record.key] = payload

    # -----------------------------------------------------------------
    # save / refresh
    # -----------------------------------------------------------------

    def _evict_conflicted(self, section: str, key: str) -> None:
        """Drop a conflicting (section, key) from pending writes and the
        cache so the next refetch sees the server's current state."""
        if not section or not key:
            return
        section_pending = self._pending_writes.get(section)
        if section_pending is not None:
            section_pending.pop(key, None)
            if not section_pending:
                self._pending_writes.pop(section, None)
        section_dict = getattr(self._cache, section, None)
        if isinstance(section_dict, dict):
            section_dict.pop(key, None)
        # Also drop any negative-cache entry so the next get_* really
        # hits the server.
        self._cache._negative.discard((section, key))

    def _stamp_applied(self, section: str, key: str, version: int) -> None:
        """Stamp OCC tokens on a record the server *did* commit and
        drop it from pending writes.

        Used in non-atomic mode after a partial 409 (and after a
        successful save) so the cached record's ``unfurl.server.*``
        attrs match the server's current state.
        """
        if not section or not key:
            return
        section_pending = self._pending_writes.get(section)
        if section_pending is not None:
            section_pending.pop(key, None)
            if not section_pending:
                self._pending_writes.pop(section, None)
        record = self._cache.get_record(section, key)
        if record is not None:
            _set_occ_tokens(record, version, None, _get_occ_id(record))

    def save(
        self,
        msg: Optional[str] = None,
        commit: bool = True,
        *,
        atomic: bool = True,
    ) -> Optional[str]:
        """POST buffered writes to the server. Returns the repository's commit
        oid after the write -- the new commit when the server made one, the
        unchanged HEAD when it only staged the records, and ``None`` for a
        repository with no commits at all.

        ``atomic`` (default ``True``)
        If atomic is true, individual record failures, including concurrency conflicts rollback all changes.
        If atomic is false, individual record failures are skipped, the rest commits, and a CloudMapProxyConflict is still raised with details on which records applied and which failed.

        ``commit`` asks the server to commit the records to its clone rather
        than leave them staged. It is always sent, never omitted: the two
        server implementations disagree on what an absent key means -- the
        python handler commits (`endpoints.py`, ``raw.get("commit")`` is None)
        while the rust one only stages (`cloudmap.rs`,
        ``body.commit.unwrap_or(false)``) -- so the intent has to be explicit.

        With ``commit`` set and nothing buffered this still POSTs: an empty
        body means "commit whatever is already staged", which is how records
        left behind by an earlier ``commit=False`` save get committed.

        On *successful* save, the cached payload of every just-posted
        record is updated with concurrency tokens from the response.
        """
        if not self._pending_writes and not commit:
            return None

        body: Dict[str, Any] = {}
        if self._cache._latest_commit:
            body["latest_commit"] = self._cache._latest_commit
        if self._username:
            body["username"] = self._username
        if self._private_token:
            body["private_token"] = self._private_token
        if msg:
            body["commit_msg"] = msg
        # Only include ``atomic`` when overriding the server default (true).
        if not atomic:
            body["atomic"] = False
        body["commit"] = commit
        for section, entries in self._pending_writes.items():
            body[section] = entries

        try:
            resp = self._post(body)
        except CloudMapProxyConflict as exc:
            # In atomic mode the server rolled the whole batch back, so
            # ``exc.applied`` is empty and ``exc.failed`` carries just
            # the first conflicting record. In non-atomic mode
            # ``exc.applied`` lists every record that *did* land — we
            # stamp OCC tokens on those (so retries don't double-apply)
            # and drop them from ``_pending_writes``. Failed records
            # are evicted from the cache + pending buffer so the next
            # ``get_*`` / ``save_with_retry`` refetches the server's
            # current state.
            self.logger.warning(
                "cloudmap conflict on %s/%s (server actual=%r); applied=%d "
                "failed=%d; dropping conflicting writes from this batch",
                exc.section,
                exc.key,
                exc.actual,
                len(exc.applied),
                len(exc.failed),
            )
            for section, key, version in exc.applied:
                self._stamp_applied(section, key, version)
            for section, key, _actual, _err in exc.failed:
                self._evict_conflicted(section, key)
            raise

        new_commit = resp.get("commit")  # str or None
        new_queueid = resp.get("queueid")  # int — largest unfurl.server.version

        if isinstance(new_queueid, int) and new_queueid > self._cache._max_version:
            self._cache._max_version = new_queueid
        # Refresh an existing `latest_commit`, but never acquire one here. The
        # response's ``commit`` reports where the repository is, which is not
        # evidence about what *this* client has read -- and sending it back on
        # the next write asks the server to reject that write if anything at all
        # was committed meanwhile, including changes to unrelated records. A
        # client that has done a GET is asserting a read set it really holds; one
        # that has only ever written shouldn't start asserting one. Keeping an
        # existing token current still matters: without it, this client's own
        # commit would make its next write conflict with itself.
        if self._cache._latest_commit and isinstance(new_commit, str) and new_commit:
            self._cache._latest_commit = new_commit

        # Stamp the OCC tokens from the response onto every record in
        # this batch so a subsequent `add_*` on the same key round-
        # trips them without needing a refresh(). Tokens live as
        # private attrs on the dataclass instance (no parallel payload
        # cache).
        version_to_set = new_queueid if isinstance(new_queueid, int) else None
        # ``commit`` says where the *repository* is, not where these records
        # landed. A ``queueid`` in the response means the server staged them
        # in-flight, so their rows still carry no commit.
        # So only save the new commit when new_queueid is None.
        if version_to_set is None and new_commit:
            commit_to_set = new_commit
        else:
            commit_to_set = None
        for section, entries in self._pending_writes.items():
            for key in entries:
                record = self._cache.get_record(section, key)
                if record is not None:
                    # Preserve any existing server-assigned id —
                    # POST responses don't echo it, but the row keeps
                    # the same primary key after the write so the
                    # cached attr stays accurate.
                    _set_occ_tokens(
                        record,
                        version_to_set,
                        commit_to_set,
                        _get_occ_id(record),
                    )

        self._pending_writes.clear()
        return new_commit if isinstance(new_commit, str) else None

    def refresh(self) -> None:
        """Pull records changed since the last sync into the cache.

        Issues ``GET /cloudmap?since_version=<self._cache._max_version>``.
        Requires the rust git-sync backend on the server; the Python
        YAML fallback ignores the parameter and returns the full
        document.
        """
        # last_commit is different, any deletes after _max_version won't be observed
        # otherwise can get delete records back
        params = self._query_params(since_version=self._cache._max_version)
        body = self._get(params)
        self._cache.ingest_pair(body)
