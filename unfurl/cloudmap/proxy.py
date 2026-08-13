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
from .db import CloudMapStore, extends_children, subtype_closure
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


def _matches(record: Any, names: Optional[Set[str]]) -> bool:
    """Whether ``record`` satisfies a resolved type filter.

    ``names`` is the subtype closure of the requested type, or ``None``
    when no type was asked for — in which case every record matches.
    """
    if names is None:
        return True
    return not names.isdisjoint(record.type.types)


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
    - ``_type_loaded`` — ``(section, type)`` pairs fetched with the
      server's ``type=`` filter. A fully loaded section supersedes
      every typed load of it.
    - ``_section_cursor`` — for a walk that stopped early,
      ``(section, type) -> (resume token, last key ingested)``. Every
      record up to that key is already cached, so the next walk replays
      the prefix locally and asks the server only for the remainder.
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
        self._type_loaded: Set[Tuple[str, str]] = set()
        self._section_cursor: Dict[Tuple[str, str], Tuple[str, str]] = {}
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

    def _hydrate_one(self, section: str, key: str, payload: Dict[str, Any]) -> bool:
        """Construct the dataclass for ``payload``, stash it via the
        inherited ``add_record``, and stamp the per-record OCC tokens
        onto the new instance.

        Returns whether a record was created — ``False`` for a section
        this client doesn't know, so a caller collecting keys doesn't
        report one it can't look up.
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
            return False
        self.add_record(record)

        _set_occ_tokens(
            record,
            version if isinstance(version, int) else None,
            commit if isinstance(commit, str) and commit else None,
            rid if isinstance(rid, int) else None,
        )
        return True

    def ingest_document(
        self, doc: Optional[Dict[str, Any]]
    ) -> List[Tuple[str, str]]:
        """Merge a CloudMap-shaped dict into the cache.

        Returns the ``(section, key)`` pairs it hydrated, in document
        order, so a paged caller can yield just what this document held.
        """
        hydrated: List[Tuple[str, str]] = []
        if not isinstance(doc, dict):
            return hydrated
        for section in _SECTIONS:
            entries = doc.get(section)
            if not isinstance(entries, dict):
                continue
            for key, payload in entries.items():
                if not isinstance(payload, dict):
                    continue
                self._note_tokens(payload)
                if self._hydrate_one(section, key, payload):
                    hydrated.append((section, key))
                self._negative.discard((section, key))
        return hydrated

    def ingest_response(self, body: Any) -> Tuple[List[Tuple[str, str]], Optional[str]]:
        """Merge one ``GET /cloudmap`` response.

        Returns the ``(section, key)`` pairs this response actually
        delivered, and its page cursor — ``None`` when there isn't one,
        which is what ends a paged walk. The pairs let a caller iterating
        page by page yield exactly what each page brought, without
        rescanning a cache that also holds earlier pages.

        The response is ``{"result": <doc>, "followed": <doc>,
        "next_page_token": <str>}``, the last two present only when the
        request asked for what they carry.

        A list body is a server predating the object response, which
        answered ``[result, followed]``; merging both halves keeps such a
        server usable. ``None`` (a 404, via
        :meth:`CloudMapProxy._get`) merges nothing.
        """
        keys: List[Tuple[str, str]] = []
        if isinstance(body, dict):
            keys += self.ingest_document(body.get("result"))
            keys += self.ingest_document(body.get("followed"))
            token = body.get("next_page_token")
            return keys, (token if isinstance(token, str) and token else None)
        if isinstance(body, list):
            for half in body:
                keys += self.ingest_document(half)
        return keys, None


DEFAULT_REQUEST_TIMEOUT = 30.0

# Budget for the serialized `exclude` query parameter. The server
# refuses more than 10000 ids outright (git-sync's `MAX_EXCLUDE_IDS`),
# but the binding constraint is the URL: `exclude` rides in the query
# string, and proxies commonly cap a request line near 8 KB, which a
# cache of a few thousand records would blow through long before the
# server ever saw it. Staying well under that leaves room for the rest
# of the query and keeps the id count far below the server's limit.
_MAX_EXCLUDE_BYTES = 2000

# Records per request when enumerating a section. Paging keeps a single
# response bounded on a large cloudmap; the walk is transparent to callers,
# who see one iterator either way. Pass ``page_size=0`` to fetch each
# section in one request instead.
DEFAULT_PAGE_SIZE = 500


class CloudMapProxy(CloudMapStore):
    """:class:`CloudMapView` implementation backed by a remote
    unfurl-server.

    Reads:
      - :meth:`get_X` first consults the local :class:`CloudMapCache`;
        on a miss it issues
        ``GET /cloudmap?kind=<section>&key=<url>&follow=<N>`` and
        ingests both the primary record and any followed records.
      - :meth:`find_X` returns an iterator that walks the endpoint's
        pages *on demand*: taking one record costs one request, not the
        whole section, and a consumer that stops early stops paying.
        Given a ``type`` filter it asks the server for only the matching
        records (``&type=<name>``). A walk that runs to the end records
        the ``(section, type)`` pair — or the section itself, when
        untyped — so a later call is served locally; one abandoned
        partway records nothing and is re-walked.

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

    Paging:
      - Section walks fetch ``page_size`` records per request
        (:data:`DEFAULT_PAGE_SIZE`; ``0`` disables). A server predating
        paging ignores the parameter and answers with the whole section,
        which the same code path handles.
      - Every record a walk sees stays in the cache, so a *complete*
        iteration ends up holding the section however it was fetched.
        Paging bounds each response and the cost of a partial read, not
        the footprint of reading everything.
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
        page_size: int = DEFAULT_PAGE_SIZE,
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
        self._page_size = page_size
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

    def _exclude_param(self) -> Optional[str]:
        """The ``exclude`` value for a follow fetch: ids the cache holds.

        The server skips those records during the walk, so it doesn't
        re-send what the proxy already has. Records are duplicated under
        each version's url, so dedupe.

        Truncated to :data:`_MAX_EXCLUDE_BYTES`. Excluding is only an
        optimization — anything not excluded simply comes back and is
        re-ingested — so a short list costs bandwidth, never
        correctness, which is what makes truncating the right response
        to a cache that has outgrown the parameter.
        """
        ids: Set[int] = set()
        for section in _SECTIONS:
            for record in getattr(self._cache, section).values():
                rid = _get_occ_id(record)
                if rid is not None:
                    ids.add(rid)
        if not ids:
            return None
        parts: List[str] = []
        budget = _MAX_EXCLUDE_BYTES
        for rid in sorted(ids):
            token = str(rid)
            budget -= len(token) + 1  # the separator
            if budget < 0:
                self.logger.debug(
                    "cache holds %d record ids; sending %d in `exclude` "
                    "(the rest will be re-sent and re-ingested)",
                    len(ids),
                    len(parts),
                )
                break
            parts.append(token)
        return ",".join(parts) or None

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
        params = self._query_params(
            kind=section,
            key=key,
            follow=self._follow_depth,
            exclude=self._exclude_param(),
        )
        body = self._get(params)
        if body is None:
            self._cache._negative.add((section, key))
            return
        self._cache.ingest_response(body)  # keys unused: this is a point lookup

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

    def _pages(
        self,
        section: Optional[str] = None,
        type: Optional[str] = None,
        since_version: Optional[int] = None,
        start_token: Optional[str] = None,
    ) -> Iterator[Tuple[List[Tuple[str, str]], Optional[str], bool]]:
        """Walk the endpoint's pages, one request at a time.

        Yields ``(keys, resume, complete)`` per page: the
        ``(section, key)`` pairs that page delivered (already merged into
        the cache), the cursor that would resume *after* this page, and
        whether the walk finished — ``complete`` is ``True`` only on the
        last page of a natural exhaustion, so a caller knows when it may
        record the section as fully loaded. ``resume`` is ``None`` on any
        page there is no continuing from.

        ``start_token`` resumes a walk an earlier consumer abandoned.

        Nothing is requested until the consumer asks for a page, which is
        what makes :meth:`find_artifacts` and friends cost one request
        rather than the whole section. ``section`` is sent as ``kind``;
        ``None`` means the whole document (what :meth:`refresh` wants).
        """
        token: Optional[str] = start_token
        while True:
            params = self._query_params(
                kind=section,
                type=type or None,
                since_version=since_version,
                limit=self._page_size or None,
                page_token=token,
            )
            keys, next_token = self._cache.ingest_response(self._get(params))
            if not next_token:
                yield keys, None, True
                return
            if next_token == token:
                # A cursor names the last record of the page it came with,
                # so it can never repeat. A server that returns one anyway
                # would spin this loop forever; stop with what we have —
                # and report the walk as incomplete, so the caller doesn't
                # record a truncated section as fully loaded.
                self.logger.warning(
                    "GET %s returned the same page_token twice (kind=%s); "
                    "stopping the walk, results may be incomplete",
                    self._endpoint,
                    section,
                )
                yield keys, None, False
                return
            yield keys, next_token, False
            token = next_token

    def _mark_loaded(self, section: str, type: str) -> None:
        """Record that a completed walk covered ``section`` (for ``type``)."""
        self._cache._section_cursor.pop((section, type), None)
        if type:
            self._cache._type_loaded.add((section, type))
            return
        self._cache._section_loaded.add(section)
        # A full load answers every typed question about this section, so
        # neither a typed mark nor a typed resume point means anything now.
        self._cache._type_loaded = {
            pair for pair in self._cache._type_loaded if pair[0] != section
        }
        for pair in [p for p in self._cache._section_cursor if p[0] == section]:
            del self._cache._section_cursor[pair]

    def _ensure_section(self, section: str) -> None:
        """Fetch the named section in full; subsequent calls are local.

        The eager counterpart to :meth:`_iter_section`, for the callers
        that need the whole section present before they can do anything
        with it — the subtype closure, which can't filter page 1 without
        every ``extends`` edge. Draining the lazy walk rather than
        duplicating it means this resumes an abandoned one too.
        """
        if section in self._cache._section_loaded:
            return
        for _record in self._iter_section(section):
            pass

    def _iter_section(self, section: str, type: str = "") -> Iterator[Any]:
        """Yield the records of ``section`` matching ``type``, paging lazily.

        Each page is fetched only when the consumer exhausts the last, so
        a caller that stops early pays for what it read. Records come
        from the pages themselves rather than a rescan of the cache,
        which by page two also holds page one.

        A walk that stopped early leaves a cursor, so the next one
        replays the prefix it already cached and requests only the pages
        beyond it. The prefix is served from the cache, so it is as fresh
        as when it was fetched rather than as fresh as this call.

        Once the walk completes, any record the cache holds that no page
        delivered is yielded too: records staged locally by
        :meth:`add_record` but not yet saved, and any pulled in earlier
        by a ``get_*``. That keeps a full iteration equal to what the old
        fetch-everything-then-filter did, so an analyzer can add a record
        and still find it.
        """
        cache_section: Dict[str, Any] = getattr(self._cache, section)
        if section in self._cache._section_loaded or (
            type and (section, type) in self._cache._type_loaded
        ):
            # Everything is already here; no request needed.
            yield from self._local_matches(
                cache_section, self._subtype_names(type), set()
            )
            return

        # The server's `type=` matches subtypes, so the local filter needs
        # every `extends` edge before it can judge page one — otherwise it
        # narrows to exact names and drops records the server deliberately
        # sent. Loading `types` is itself a (small, complete) walk.
        if type:
            self._ensure_section("types")

        # Resolved once: the closure is O(types), and re-deriving it per
        # record would make a section scan O(records x types).
        names = self._subtype_names(type)
        cursor_key = (section, type)
        yielded: Set[str] = set()

        # Pick up where an abandoned walk stopped. Records are ordered by
        # key, so everything at or before the cursor's key is already
        # cached: replay that prefix locally and ask the server only for
        # the rest. Locally added records in the same range come along,
        # since they are in the cache too.
        resume = self._cache._section_cursor.get(cursor_key)
        start_token: Optional[str] = None
        if resume is not None:
            start_token, last_key = resume
            for key in sorted(k for k in cache_section if k <= last_key):
                record = cache_section[key]
                if not _matches(record, names):
                    continue
                yielded.add(key)
                yield record

        complete = False
        for keys, next_token, complete in self._pages(
            section, type=type or None, start_token=start_token
        ):
            # Record the resume point before handing out the page's
            # records: they are already cached, so a consumer that stops
            # midway still leaves a cursor that covers all of them.
            if next_token:
                page_keys = [k for got, k in keys if got == section]
                if page_keys:
                    self._cache._section_cursor[cursor_key] = (
                        next_token,
                        page_keys[-1],
                    )
            for got_section, key in keys:
                if got_section != section or key in yielded:
                    continue
                record = cache_section.get(key)
                if record is None or not _matches(record, names):
                    continue
                yielded.add(key)
                yield record
        if not complete:
            # A truncated walk hasn't seen the whole section, so neither
            # the local sweep nor the loaded mark would be honest.
            return
        yield from self._local_matches(cache_section, names, yielded)
        self._mark_loaded(section, type)

    def _subtype_names(self, type: str) -> Optional[Set[str]]:
        """``type`` and its subtypes, or ``None`` for "no type filter"."""
        if not type:
            return None
        return subtype_closure(extends_children(self._cache.types), type)

    def _local_matches(
        self, cache_section: Dict[str, Any], names: Optional[Set[str]], skip: Set[str]
    ) -> Iterator[Any]:
        """Records already in the cache, minus those a caller has seen."""
        for key, record in list(cache_section.items()):
            if key in skip or not _matches(record, names):
                continue
            yield record

    def find_artifacts(self, type: str = "") -> Iterator[Artifact]:
        return self._iter_section("artifacts", type)

    def find_services(self, type: str = "") -> Iterator[Service]:
        return self._iter_section("services", type)

    def find_components(self, type: str = "") -> Iterator[Component]:
        return self._iter_section("components", type)

    def find_instantiations(self, type: str = "") -> Iterator[Instantiation]:
        return self._iter_section("instantiations", type)

    def find_types(self) -> Iterator[CloudType]:
        return self._iter_section("types")

    def find_repositories(self) -> Iterator[Repository]:
        return self._iter_section("repositories")

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
        #
        # Read the watermark once, before the walk: ingesting a page raises
        # `_max_version`, so recomputing it per page would keep moving the
        # floor and skip records the later pages were meant to carry.
        for _keys, _resume, _complete in self._pages(
            since_version=self._cache._max_version
        ):
            pass
