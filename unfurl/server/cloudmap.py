# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""
CloudMap HTTP endpoints for unfurl server.
"""

import json
import os
from itertools import product
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
    Union,
    cast,
)
from urllib.parse import urlparse

from flask import Response, current_app, jsonify, request
from flask.typing import ResponseReturnValue

from ..cloudmap.db import CloudMapDB, CloudMapStore, extends_children, subtype_closure
from ..localenv import LocalEnv
from ..logs import getLogger
from ..repo import GitRepo
from ..util import API_VERSION, UnfurlError, assert_not_none
from ..yamlloader import yaml

from .schemas import (
    CloudMapAnalyzeRequest,
    CloudMapAnalyzeResponse,
    CloudMapDocQuery,
    CloudMapQuery,
    CloudMapResponse,
    CloudMapResult,
    FacetsQuery,
    FacetsResult,
    PatchResponse,
    PostCloudmapRequest,
    ProjectAuthQuery,
)

# Like endpoints.py, imported by serve.py once everything these names
# refer to is defined.
from .serve import (
    CacheEntry,
    app,
    create_error_response,
    get_project_id,
    get_project_id_or_abort,
    serving_local_path,
)
from .endpoints import _commit_and_push, _get_author, _get_body, _get_commit_msg

if TYPE_CHECKING:
    from ..cloudmap import CloudMap
    from ..tosca_plugins.cloudmap_defs import CloudMapRecord

logger = getLogger("unfurl.server")


CLOUDMAP_PROJECT = "onecommons/cloudmap"


def _cloudmap_project_id(request) -> str:
    """Which project to read the cloudmap from.

    Unlike the other endpoints, the cloudmap reads have somewhere to go when a request
    names no project, so they use this instead of `get_project_id_or_abort`: a server
    started on a local path serves that project's own ``cloudmap.yaml``, and any other
    server falls back to the public cloudmap.
    """
    project_id = get_project_id(request)
    if project_id:
        return project_id
    return "" if serving_local_path() else CLOUDMAP_PROJECT


def _subtype_names(types_section: Dict[str, Any], type_name: str) -> Set[str]:
    """Expand ``type_name`` to itself plus every subtype — every type
    record whose ``extends`` list (transitively) contains it.

    Mirrors the rust server's ``CloudMapState::subtype_names``:
    ``extends`` lists are often pre-flattened (full ancestor closure)
    but the BFS also handles direct-parents-only producers, and
    ``type_name`` need not have a type record. Shares its walk with
    ``CloudMapDB.find_*`` so the three implementations can't drift.
    """
    return subtype_closure(extends_children(types_section), type_name)


def _encode_page_token(path: str, key: str) -> str:
    """Mint the cursor naming ``(path, key)`` as the last record of a page.

    Just ``<section>/<key>``. Section names are a fixed set and contain no
    "/", so the first one separates the two halves however many the key
    itself has; nothing needs escaping because the whole token is
    URL-encoded as a query parameter anyway. ``path`` is the record path
    the rust backend stores (``"/artifacts"``), so a token means the same
    thing whichever server issued it.
    """
    return f"{path.lstrip('/')}/{key}"


def _decode_page_token(token: str) -> Tuple[str, str]:
    """Inverse of :func:`_encode_page_token`, returning ``(path, key)``.

    Raises:
        ValueError: If the token isn't ``<known section>/<non-empty key>``.
            Checking the section matters: an unrecognised one would
            otherwise silently resume from the wrong place in the ordering
            rather than report the bad cursor. The anchor record needn't
            still exist -- the bound is a value, not a row reference -- so
            this never rejects a merely stale token.
    """
    section, sep, key = token.partition("/")
    if not sep or not key or section not in _CLOUDMAP_SECTIONS:
        raise ValueError(
            f"page_token {token!r} is not a valid cursor: expected <section>/<key>"
        )
    return "/" + section, key


def _declares_type(record: Any, type_names: Set[str]) -> bool:
    """True when the record's ``type`` typeRef object declares one of
    ``type_names`` as a key."""
    if not isinstance(record, dict):
        return False
    type_ref = record.get("type")
    return isinstance(type_ref, dict) and any(k in type_names for k in type_ref)


def _pointer_tokens(path: str) -> List[str]:
    """Parse a JSON Pointer (RFC 6901) into unescaped reference tokens.

    A leading ``/`` is optional.

    Raises:
        ValueError: If the path is empty or has an empty segment.
    """
    if not path.startswith("/"):
        path = "/" + path
    tokens = [t.replace("~1", "/").replace("~0", "~") for t in path.split("/")[1:]]
    if not tokens or not all(tokens):
        raise ValueError(f"path {path!r} needs non-empty segments")
    return tokens


def _pointer_text(tokens: List[str]) -> str:
    """Render reference tokens back to normalized JSON Pointer text,
    re-escaping per RFC 6901 -- the inverse of :func:`_pointer_tokens`."""
    return "/" + "/".join(t.replace("~", "~0").replace("/", "~1") for t in tokens)


def _json_literal(raw: str) -> Any:
    """Parse a ``filter`` value the way JSON would, defaulting to a string.

    ``"42"`` (quoted) stays the string ``42`` -- the escape hatch for values
    that would otherwise be read as a number, a keyword or an array.

    Raises:
        ValueError: If the value is an object literal (address a member by
            putting its key in the path instead) or a malformed array.
    """
    if len(raw) > 1 and raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    if raw.startswith("{"):
        raise ValueError(
            "filter value can't be an object: address a member by putting its "
            "key in the path"
        )
    if raw.startswith("["):
        # an array literal is an exact match; a bad one is an error rather
        # than a string, which would silently never match
        try:
            return json.loads(raw)
        except ValueError as err:
            raise ValueError(f"filter value {raw!r} is not valid JSON: {err}") from err
    if raw in ("true", "false"):
        return raw == "true"
    if raw == "null":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _parse_json_filter(expr: str) -> Tuple[List[str], Any, str]:
    """Split a ``filter`` param into JSON Pointer tokens and the value to match.

    Args:
        expr: ``<json pointer>=<value>``, e.g. ``/metadata/version=1.0``;
            ``<json pointer>^=<prefix>`` for prefix matching; or a bare
            ``<json pointer>`` to test that the path exists. A leading "/" is
            optional; tokens are unescaped per RFC 6901.

    Returns:
        The pointer's reference tokens, the parsed value, and the operator:
        ``"eq"``, ``"prefix"`` or ``"exists"``.

    Raises:
        ValueError: If the filter has an empty path segment.
    """
    path, sep, raw = expr.partition("=")
    path = path.strip()
    # "^=" is prefix matching; the "^" is at the end of the path half
    prefix = path.endswith("^")
    if prefix:
        path = path[:-1]
    try:
        tokens = _pointer_tokens(path)
    except ValueError:
        raise ValueError(
            f"filter {expr!r} needs a path of non-empty segments"
        ) from None
    if not sep:
        # a bare path: the filter is satisfied when the path resolves
        return tokens, None, "exists"
    if prefix:
        # a prefix is textual, so it is never parsed as JSON -- only unquoted,
        # so a prefix that looks like a number can be written either way
        if len(raw) > 1 and raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        return tokens, raw, "prefix"
    return tokens, _json_literal(raw), "eq"


def _json_filter_matches(
    record: Any, tokens: List[str], value: Any, op: str = "eq"
) -> bool:
    """Whether ``value`` is at ``tokens`` in ``record``.

    Mirrors what the rust path pushes into SQL: a scalar ``value`` matches if
    the value at the path equals it or is an array containing it, while an
    array ``value`` is an exact match (same elements, same order). With
    ``op="prefix"``, a string value at the path -- or any string element of an
    array there -- has to start with ``value``; with ``op="exists"`` the path
    just has to resolve. A member of
    an object is addressed by putting its key in the path, not by searching the
    object's values -- postgres can't serve that from a GIN index. A path that
    doesn't resolve never matches.
    """
    current: Any = record
    for token in tokens:
        if not isinstance(current, dict) or token not in current:
            return False
        current = current[token]
    if op == "exists":
        # the walk above resolved, so the path is there -- a null or an empty
        # container counts, only a missing path doesn't
        return True
    if op == "prefix":
        # strings only: a number never matches a prefix, matching postgres'
        # `starts with` and the sqlite clause's `jq.type = 'text'` guard
        candidates = current if isinstance(current, list) else [current]
        return any(isinstance(c, str) and c.startswith(value) for c in candidates)
    if isinstance(value, list):
        return current == value
    if isinstance(current, list):
        return value in current
    return current == value


def _record_matcher(
    doc: Dict[str, Any], type_name: Optional[str], filter_exprs: List[str]
) -> Optional[Callable[[Any], bool]]:
    """Build the record predicate for the shared selection parameters.

    One construction serves ``/cloudmap`` and ``/cloudmap/facets`` so the
    ``type`` and ``filter`` semantics (and any future extension of them)
    can't drift between the endpoints. ``filter`` repeats: every
    expression must match, each independently (two filters may be
    satisfied by different elements of the same array) -- mirroring the
    ANDed SQL clauses the rust fast-path emits. Returns ``None`` when
    nothing filters, which callers can use to keep their unfiltered
    fast paths.

    Raises:
        ValueError: If a filter expression doesn't parse (callers
            return it as a 400).
    """
    type_names: Optional[Set[str]] = None
    if type_name:
        type_names = _subtype_names(doc.get("types") or {}, type_name)
    json_filters: List[Tuple[List[str], Any, str]] = [
        _parse_json_filter(expr) for expr in filter_exprs if expr.strip()
    ]
    if type_names is None and not json_filters:
        return None

    def _matches(record: Any) -> bool:
        if type_names is not None and not _declares_type(record, type_names):
            return False
        return all(
            _json_filter_matches(record, *json_filter) for json_filter in json_filters
        )

    return _matches


def _selected_records(
    doc: Dict[str, Any],
    kind: Optional[str],
    matcher: Optional[Callable[[Any], bool]],
) -> Iterator[Tuple[str, str, Any]]:
    """Yield ``(section, key, record)`` for every record selected by
    ``kind`` and a :func:`_record_matcher` predicate, in canonical
    section order. The iteration also skips the document's envelope
    keys (``apiVersion``, ``kind``, ``metadata``), which aren't
    records."""
    sections = (kind,) if kind else _CLOUDMAP_SECTIONS
    for section_name in sections:
        section = doc.get(section_name)
        if not isinstance(section, dict):
            continue
        for record_key, record in section.items():
            if matcher is None or matcher(record):
                yield section_name, record_key, record


# Sentinel distinguishing "pointer didn't resolve" from a legitimate
# ``None``/``null`` value at the pointed-to location.
_MISSING = object()

# A parsed ``select`` entry: ``None`` for the special ``$key`` item,
# otherwise the JSON Pointer's unescaped reference tokens.
SelectPath = Optional[List[str]]


def _parse_select(raw: str) -> List[SelectPath]:
    """Parse the ``select`` query param into a list of paths.

    Comma-separated JSON Pointers (RFC 6901); items are stripped and
    empties dropped. A missing leading ``/`` is prepended. ``$key`` is
    kept as the ``None`` marker. Pointers that have a strict prefix
    also in the list are dropped — the ancestor pointer already
    selects the whole subtree.
    """
    items: List[SelectPath] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "$key":
            items.append(None)
            continue
        if not part.startswith("/"):
            part = "/" + part
        items.append([
            t.replace("~1", "/").replace("~0", "~") for t in part.split("/")[1:]
        ])
    pointers = [i for i in items if i is not None]
    return [
        i
        for i in items
        if i is None or not any(len(p) < len(i) and i[: len(p)] == p for p in pointers)
    ]


def _resolve_pointer(value: Any, tokens: List[str]) -> Any:
    """Evaluate JSON Pointer reference tokens against ``value``;
    return ``_MISSING`` when the pointer doesn't resolve."""
    for t in tokens:
        if isinstance(value, dict):
            if t not in value:
                return _MISSING
            value = value[t]
        elif isinstance(value, list):
            # RFC 6901 array index: digits without leading zeros.
            if not t.isdigit() or (len(t) > 1 and t.startswith("0")):
                return _MISSING
            idx = int(t)
            if idx >= len(value):
                return _MISSING
            value = value[idx]
        else:
            return _MISSING
    return value


def _project_record(record: Any, key: str, select: List[SelectPath]) -> Dict[str, Any]:
    """Reduce ``record`` to the properties named by ``select``,
    keeping their nested structure (array indices become object keys).
    Unresolvable paths are omitted; ``$key`` adds the record's key."""
    out: Dict[str, Any] = {}
    for tokens in select:
        if tokens is None:
            out["$key"] = key
            continue
        value = _resolve_pointer(record, tokens)
        if value is _MISSING:
            continue
        node = out
        for t in tokens[:-1]:
            node = node.setdefault(t, {})
        node[tokens[-1]] = value
    return out


def _project_document(doc: Dict[str, Any], select: List[SelectPath]) -> Dict[str, Any]:
    """Apply :func:`_project_record` to every record of a
    CloudMap-shaped dict, dropping any non-section envelope keys."""
    return {
        section_name: {k: _project_record(v, k, select) for k, v in section.items()}
        for section_name, section in doc.items()
        if section_name in _CLOUDMAP_SECTIONS and isinstance(section, dict)
    }


@app.get("/cloudmap")
@app.doc(
    summary="CloudMap document",
    description=(
        "Return the CloudMap document under ``result`` — the raw "
        "CloudMap, or the subset selected by ``kind`` / ``key`` / "
        "``type`` / ``filter``.\n\n"
        "Two further keys appear only when the request asked for what "
        "they carry, so a client can tell 'you didn't ask' from 'there "
        "is none': ``followed`` holds the records reached by walking "
        "the graph, and is present only when ``follow`` > 0 with a "
        "``key``; ``next_page_token`` is the cursor for the next page, "
        "and is present only on a ``limit`` request that has one."
    ),
    tags=["Export"],
)
@app.input(CloudMapDocQuery, location="query", arg_name="query")
@app.output(
    CloudMapResult,
    description="The filtered CloudMap document, plus follow/paging keys when requested",
)
def get_cloudmap(query: CloudMapDocQuery) -> ResponseReturnValue:
    from .cache import CLOUDMAP_BRANCH, CLOUDMAP_PATH, load_cloudmap_local

    project_id = _cloudmap_project_id(request)
    branch = query.branch or CLOUDMAP_BRANCH
    kind = query.kind
    key = query.key
    follow = query.follow
    limit = query.limit
    # A key selects a single record, so there is nothing to page through.
    if limit is not None and key:
        return create_error_response("BAD_REQUEST", "limit cannot be combined with key")
    after: Optional[Tuple[str, str]] = None
    if query.page_token:
        try:
            after = _decode_page_token(query.page_token)
        except ValueError as page_err:
            return create_error_response("BAD_REQUEST", str(page_err))
    need_db = follow > 0
    # NB: rust server doesn't filter by CLOUDMAP_PATH when cloudmap_path is not specified
    err, doc, db = load_cloudmap_local(
        project_id,
        branch=branch,
        file_name=query.cloudmap_path or CLOUDMAP_PATH,
        latest_commit=query.latest_commit,
        create_db=need_db,
    )
    if doc is None:
        if isinstance(err, Response):
            return err
        return create_error_response("INTERNAL_ERROR", str(err))

    # `exclude` is a CSV of record primary-key ids the caller already
    # holds (matches the rust handler's contract). Non-numeric tokens
    # are silently dropped — same as the rust side.
    exclude_param = query.exclude or ""
    exclude_ids: Set[int] = set()
    for tok in exclude_param.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            exclude_ids.add(int(tok))
        except ValueError:
            pass

    # `type` and `filter` narrow on each record's contents; the predicate
    # construction is shared with /cloudmap/facets (`_record_matcher`), so
    # their semantics can't drift between the two endpoints. `filter`
    # repeats (every occurrence must match), read via getlist -- the
    # pydantic adapter binds only the first value of a repeated key.
    filter_params = request.args.getlist("filter")
    try:
        matcher = _record_matcher(doc, query.type, filter_params)
    except ValueError as err:
        return create_error_response("BAD_REQUEST", str(err))

    def _matches(record: Any) -> bool:
        """Whether a record passes the `type` and `filter` params (both optional)."""
        return matcher is None or matcher(record)

    filtered = matcher is not None

    primary: Dict[str, Any]
    if not kind:
        if not filtered:
            primary = doc
        else:
            primary = {}
            for section_name in _CLOUDMAP_SECTIONS:
                section = doc.get(section_name)
                if not isinstance(section, dict):
                    continue
                matches = {k: v for k, v in section.items() if _matches(v)}
                if matches:
                    primary[section_name] = matches
    else:
        section = doc.get(kind, {})
        if key is None:
            if not filtered:
                primary = {kind: section}
            else:
                matches = (
                    {k: v for k, v in section.items() if _matches(v)}
                    if isinstance(section, dict)
                    else {}
                )
                primary = {kind: matches} if matches else {}
        elif not isinstance(section, dict) or key not in section:
            return create_error_response(
                "NOT_FOUND", f"key {key!r} not found in {kind!r}"
            )
        elif not _matches(section[key]):
            hint = " with matching type" if query.type else ""
            if filter_params:
                hint += " matching the filter"
            return create_error_response(
                "NOT_FOUND", f"key {key!r} not found in {kind!r}{hint}"
            )
        else:
            primary = {kind: {key: section[key]}}

    # Cut the page before both the walk and `select`: the walk starts from
    # the records being returned, so a page must not report neighbours of a
    # record it doesn't contain, and a projection must not change which
    # records the page holds. `primary` is already narrowed by kind, type
    # and filter here, so paging sees exactly what an unpaged request would.
    next_token: Optional[str] = None
    if limit is not None:
        primary, next_token = _page_document(primary, after, limit)

    followed: Dict[str, Any] = {}
    if db is not None:
        from ..reporting import (
            CollectVisitor,
            walk_cloudmap_graph,
            walk_cloudmap_graph_from,
        )

        if key:
            visitor = CollectVisitor({key}, follow, exclude=exclude_ids)
            walk_cloudmap_graph(db, visitor, key)
        else:
            # Every record being returned is a root, so the walk yields their
            # neighbourhood -- e.g. `kind=artifacts&type=X&follow=10` gives
            # those artifacts plus the repositories they came from. On a paged
            # request the roots are that page
            roots = [
                record_key
                for section_name in _CLOUDMAP_SECTIONS
                if isinstance(primary.get(section_name), dict)
                for record_key in primary[section_name]
            ]
            visitor = CollectVisitor(set(roots), follow, exclude=exclude_ids)
            walk_cloudmap_graph_from(db, visitor, roots)
        followed = visitor.result

    # `select` reduces every returned record to the requested properties.
    if query.select:
        select_paths = _parse_select(query.select)
        if select_paths:
            primary = _project_document(primary, select_paths)
            followed = _project_document(followed, select_paths)

    # `followed` and `next_page_token` are omitted unless the request
    # asked for what they carry, so a client can tell "you didn't ask"
    # from "there is none" without inspecting its own query.
    body: Dict[str, Any] = {"result": primary}
    if need_db:
        body["followed"] = followed
    if next_token is not None:
        body["next_page_token"] = next_token
    return body


def _page_document(
    doc: Dict[str, Any], after: Optional[Tuple[str, str]], limit: int
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Cut one page out of a CloudMap document.

    Records across all sections are ordered by ``(path, key)`` -- the
    same byte-wise ordering the rust backend sorts by, ``path`` being
    ``"/" + section`` -- so a walk is stable and the two server
    implementations agree on where a page ends. Returns the page as a
    CloudMap document plus the cursor for the next one, or ``None`` when
    this was the last.

    Iterating known sections also drops the document's envelope keys
    (``apiVersion``, ``kind``, ``metadata``), which aren't records and
    have no place in a page.
    """
    entries = sorted(
        (
            ("/" + section_name, k, v)
            for section_name in _CLOUDMAP_SECTIONS
            if isinstance(doc.get(section_name), dict)
            for k, v in doc[section_name].items()
        ),
        key=lambda entry: (entry[0], entry[1]),
    )
    if after is not None:
        entries = [e for e in entries if (e[0], e[1]) > after]
    # One extra record is all it takes to know another page exists; without
    # the probe a section whose size is a multiple of `limit` would end on a
    # token pointing at an empty page.
    more = len(entries) > limit
    entries = entries[:limit]
    paged: Dict[str, Any] = {}
    for path, k, v in entries:
        paged.setdefault(path[1:], {})[k] = v
    token = _encode_page_token(entries[-1][0], entries[-1][1]) if more else None
    return paged, token


_CLOUDMAP_SECTIONS: Tuple[str, ...] = (
    "repositories",
    "artifacts",
    "components",
    "services",
    "instantiations",
    "types",
)
_CLOUDMAP_ENVELOPE_KEYS: Tuple[str, ...] = (
    "branch",
    "latest_commit",
    "cloudmap_path",
    "commit",
    "username",
    "private_token",
    "password",
    "commit_msg",
    "atomic",
)


def _facet_values(record: Any, tokens: List[str]) -> List[Any]:
    """The values at ``tokens`` per the facet extraction rule: the
    elements of an array, the keys of an object, or the scalar itself;
    empty when the path doesn't resolve. One level -- container values
    inside an array element stay whole."""
    node: Any = record
    for token in tokens:
        if not isinstance(node, dict) or token not in node:
            return []
        node = node[token]
    if isinstance(node, list):
        return list(node)
    if isinstance(node, dict):
        return list(node)
    return [node]


def _type_ancestors(types_section: Dict[str, Any], type_name: str) -> Set[str]:
    """``type_name`` plus every ancestor reachable through the ``types``
    section's ``extends`` lists -- the up-walk mirror of
    :func:`_subtype_names`, used to roll a declared type's count into its
    base types' buckets. A name without a type record is just itself."""
    out = {type_name}
    queue = [type_name]
    while queue:
        record = types_section.get(queue.pop())
        extends = record.get("extends") if isinstance(record, dict) else None
        if not isinstance(extends, list):
            continue
        for parent in extends:
            if isinstance(parent, str) and parent not in out:
                out.add(parent)
                queue.append(parent)
    return out


def _canonical_facet_key(value: Any) -> str:
    """Render a facet value as a response key: strings stay bare, any
    other value becomes its canonical JSON text (minified, object keys
    sorted, non-ASCII raw) so structured keys parse back to JSON and
    every server implementation produces the same spelling.
    ``ensure_ascii=False`` is load-bearing: the default would spell
    "café" as ``caf\\u00e9`` where the rust server emits raw UTF-8, and
    code-point-sorted keys equal rust's byte-sorted keys only because
    UTF-8 byte order is code-point order."""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@app.get("/cloudmap/facets")
@app.doc(
    summary="CloudMap facet counts",
    description=(
        "Count the selected records grouped by the value at ``group_by``, "
        "with an optional per-group breakdown for each ``facet`` column. "
        "Record selection (``kind`` / ``type`` / ``filter``) works exactly "
        "as on ``GET /cloudmap``; the response carries counts only, no "
        "records."
    ),
    tags=["Export"],
)
@app.input(FacetsQuery, location="query", arg_name="query")
@app.output(FacetsResult, description="Group and facet counts for the selected records")
def get_cloudmap_facets(query: FacetsQuery) -> ResponseReturnValue:
    from .cache import CLOUDMAP_BRANCH, CLOUDMAP_PATH, load_cloudmap_local

    project_id = _cloudmap_project_id(request)
    branch = query.branch or CLOUDMAP_BRANCH
    try:
        group_tokens = _pointer_tokens(query.group_by)
    except ValueError as parse_err:
        return create_error_response("BAD_REQUEST", f"group_by: {parse_err}")

    # Repeated `facet=` params are read straight from the request:
    # APIFlask's pydantic adapter binds query params via
    # `request.args.to_dict()`, which keeps only the FIRST value of a
    # repeated key, so the model's `facet` field documents the parameter
    # but can't carry it.
    columns: List[List[List[str]]] = []
    for raw in request.args.getlist("facet"):
        try:
            members = [
                _pointer_tokens(part.strip()) for part in raw.split(",") if part.strip()
            ]
        except ValueError as parse_err:
            return create_error_response("BAD_REQUEST", f"facet {raw!r}: {parse_err}")
        if members:
            columns.append(members)

    err, doc, _db = load_cloudmap_local(
        project_id,
        branch=branch,
        file_name=query.cloudmap_path or CLOUDMAP_PATH,
        latest_commit=query.latest_commit,
        create_db=False,
    )
    if doc is None:
        if isinstance(err, Response):
            return err
        return create_error_response("INTERNAL_ERROR", str(err))

    try:
        # `filter` repeats like `facet` does; getlist for the same reason.
        matcher = _record_matcher(doc, query.type, request.args.getlist("filter"))
    except ValueError as parse_err:
        return create_error_response("BAD_REQUEST", str(parse_err))

    types_section = doc.get("types") or {}
    # The subtypes rollup applies to every column whose path is exactly
    # `type` -- the group or any facet member -- and is inert elsewhere.
    group_rollup = query.subtypes and group_tokens == ["type"]
    member_rollup = [
        [query.subtypes and tokens == ["type"] for tokens in members]
        for members in columns
    ]
    rollup_applied = group_rollup or any(any(flags) for flags in member_rollup)

    ancestors: Dict[str, Set[str]] = {}

    def _expand_types(values: List[Any]) -> List[Any]:
        """Replace each declared type name with itself plus its ancestors."""
        out: List[Any] = []
        for value in values:
            if isinstance(value, str):
                if value not in ancestors:
                    ancestors[value] = _type_ancestors(types_section, value)
                out.extend(ancestors[value])
            else:
                out.append(value)
        return out

    # Every cell holds a set of (section, key) record identities -- the
    # in-memory analogue of the SQL COUNT(DISTINCT r.id), so a record
    # with duplicate values still counts once per bucket.
    total = 0
    group_ids: Dict[str, Set[Tuple[str, str]]] = {}
    facet_ids: Dict[str, List[Dict[str, Set[Tuple[str, str]]]]] = {}

    for section_name, record_key, record in _selected_records(doc, query.kind, matcher):
        rid = (section_name, record_key)
        total += 1
        group_values = _facet_values(record, group_tokens)
        if group_rollup:
            group_values = _expand_types(group_values)
        group_keys = {_canonical_facet_key(v) for v in group_values}
        if not group_keys:
            continue
        for gkey in group_keys:
            group_ids.setdefault(gkey, set()).add(rid)
            if columns and gkey not in facet_ids:
                facet_ids[gkey] = [{} for _ in columns]
        for index, members in enumerate(columns):
            member_values: List[List[Any]] = []
            missing = False
            for tokens, rollup in zip(members, member_rollup[index]):
                values = _facet_values(record, tokens)
                if rollup:
                    values = _expand_types(values)
                if not values:
                    # a record missing any member path is absent from
                    # this column
                    missing = True
                    break
                member_values.append(values)
            if missing:
                continue
            if len(member_values) == 1:
                cell_keys = {_canonical_facet_key(v) for v in member_values[0]}
            else:
                # composite column: the per-record cross product of the
                # member values, keyed by the canonical JSON array
                # (ensure_ascii=False: see _canonical_facet_key)
                cell_keys = {
                    json.dumps(
                        list(combo),
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                    for combo in product(*member_values)
                }
            for gkey in group_keys:
                cells = facet_ids[gkey][index]
                for cell_key in cell_keys:
                    cells.setdefault(cell_key, set()).add(rid)

    groups_body: Dict[str, Any] = {}
    for gkey in sorted(group_ids):
        entry: Dict[str, Any] = {"count": len(group_ids[gkey])}
        if columns:
            entry["facets"] = [
                {ck: len(ids) for ck, ids in sorted(cells.items())}
                for cells in facet_ids[gkey]
            ]
        groups_body[gkey] = entry
    body = {
        "meta": {
            "group_by": _pointer_text(group_tokens),
            "facets": [
                [_pointer_text(member) for member in members] for members in columns
            ],
            "subtypes": rollup_applied,
        },
        "total": total,
        "groups": groups_body,
    }
    # Return a Response: APIFlask passes it through untouched, so the
    # optional `facets` key stays omitted (output serialization through
    # the model would re-add it as null).
    return jsonify(body)


@app.post("/cloudmap")
@app.doc(
    summary="Modify the CloudMap document",
    description=(
        "Apply a batch of add / update / delete operations to "
        "``cloudmap.yaml``. Top-level keys split between an envelope "
        "(``latest_commit`` / ``cloudmap_path`` / ``username`` / "
        "``private_token`` / ``commit_msg``) and the cloudmap "
        "sections (``repositories``, ``artifacts``, ``services``, "
        "``instantiations``, ``components``, ``types``).\n\n"
        "Each section maps record keys to a JSON object that "
        "schema-validates as the corresponding cloudmap entity. To "
        "delete a record, send the object with "
        "``unfurl.server.deleted: true``.\n\n"
        "``unfurl.server.if_exists: true`` applies the write only if the "
        "record already exists; otherwise the record is skipped and left out "
        "of ``applied``. ``unfurl.server.merge: true`` merges the object into "
        "the existing record instead of replacing it: objects are merged "
        "recursively and any other value replaces the existing one. Instead "
        "of true, ``unfurl.server.merge`` can be an object of merge directives: "
        "``delete``, a list of field names or JSON pointers, removes those "
        "fields after merging.\n\n"
        "When an edit POSTed to this endpoint is contradicted by a change in the "
        "file itself, neither side overwrites the other: a GET keeps "
        "returning the edit, the file keeps its own version, and the "
        "record is not written back to the file until the conflict "
        "is settled. Send ``unfurl.server.resolve: true`` on the "
        "record to resolve any conflicting changes in favour of this "
        "write. Leaving the flag off updates the record but leaves "
        "the conflict outstanding.\n\n"
        "The body is validated against "
        "``docs/cloudmap-schema.json`` (a 422 is returned on schema "
        "violation). On success the file is committed locally (no "
        "push) and the new commit oid is returned."
    ),
    tags=["Export"],
)
@app.input(ProjectAuthQuery, location="query", arg_name="query")
@app.input(PostCloudmapRequest, location="json", arg_name="body")
@app.output(
    PatchResponse,
    description="commit and list of applied changes (mirrors the rust handler's per-record response)",
)
def post_cloudmap(
    query: ProjectAuthQuery, body: PostCloudmapRequest
) -> ResponseReturnValue:
    """
    Unlike the Rust server, the atomic flag is ignored, posts are always atomic.
    Also per-record optimistic concurrency (via ``unfurl.server.{commit,version}`` keys) is not supported by this handler,
    so the ``latest_commit`` check is the only concurrency control in place.
    """
    from .cache import CLOUDMAP_BRANCH

    raw = _get_body(request)
    cloudmap_path = raw.get("cloudmap_path") or "cloudmap.yaml"
    # None means "this handler's default", which is to commit.
    commit_requested = raw.get("commit")
    latest_commit = raw.get("latest_commit")
    username = raw.get("username")
    password = raw.get("private_token", raw.get("password"))
    project_id = get_project_id_or_abort(request)
    branch = raw.get("branch", CLOUDMAP_BRANCH)

    # Split the body: envelope keys vs cloudmap sections.
    body_sections: Dict[str, Dict[str, Any]] = {}
    for section, entries in raw.items():
        if section in _CLOUDMAP_ENVELOPE_KEYS:
            continue
        if section not in _CLOUDMAP_SECTIONS:
            return create_error_response("BAD_REQUEST", f"unknown section {section!r}")
        if not isinstance(entries, dict):
            return create_error_response(
                "BAD_REQUEST", f"section {section!r} must be a JSON object"
            )
        body_sections[section] = entries

    return _apply_cloudmap_sections(
        project_id,
        branch,
        cloudmap_path,
        body_sections,
        commit_requested,
        latest_commit,
        cast(str, username or ""),
        cast(str, password or ""),
        _get_commit_msg(raw, f"Update {cloudmap_path}"),
    )


def _merge_record(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """``patch`` merged into ``base``: objects are merged recursively, any other
    value replaces the one in ``base``."""
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = _merge_record(current, value)
        else:
            merged[key] = value
    return merged


def _merge_deletes(directive: Union[bool, Dict[str, Any]]) -> List[List[str]]:
    """The fields an ``unfurl.server.merge`` directive deletes, as JSON
    pointer tokens. ``true`` merges and deletes nothing.

    Raises:
        ValueError: if the directive isn't ``true``, ``false`` or an object
            with only a ``delete`` list of field names or JSON pointers.
    """
    if isinstance(directive, bool):
        return []
    if not isinstance(directive, dict):
        raise ValueError("unfurl.server.merge must be true or an object")
    unknown = set(directive) - {"delete"}
    if unknown:
        raise ValueError(f"unknown unfurl.server.merge directive {sorted(unknown)}")
    fields = directive.get("delete", [])
    if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        raise ValueError("unfurl.server.merge delete must be a list of strings")
    return [_pointer_tokens(f) for f in fields]


def _without_field(record: Dict[str, Any], tokens: List[str]) -> Dict[str, Any]:
    """``record`` without the field at ``tokens``, copying the objects on the
    path rather than modifying them."""
    head, rest = tokens[0], tokens[1:]
    if head not in record:
        return record
    copy = dict(record)
    if not rest:
        del copy[head]
    elif isinstance(copy[head], dict):
        copy[head] = _without_field(copy[head], rest)
    return copy


def _apply_cloudmap_sections(
    project_id: str,
    branch: str,
    cloudmap_path: str,
    body_sections: Dict[str, Dict[str, Any]],
    commit_requested: Optional[bool],
    latest_commit: Optional[str],
    username: str,
    password: str,
    commit_msg: str,
) -> ResponseReturnValue:
    """Write ``body_sections`` into the cloudmap at ``cloudmap_path`` and commit it,
    as ``POST /cloudmap`` does. See :func:`post_cloudmap` for the request semantics."""
    from .cache import load_yaml_from_cache

    # Resolve the on-disk path and the GitRepo for `_commit_and_push` first, so a
    # `cloudmap_path` that doesn't exist yet can be told apart from a load failure.
    cache_entry = CacheEntry(
        project_id, branch, cloudmap_path, "load_yaml", do_clone=True
    )
    cache_entry._set_project_repo()
    repo = cache_entry.checked_repo
    if not isinstance(repo, GitRepo):
        return create_error_response(
            "INTERNAL_ERROR", "cloudmap repository not available"
        )
    full_path = os.path.join(repo.working_dir, cloudmap_path)
    starting_revision = repo.revision

    if os.path.exists(full_path):
        err, doc = load_yaml_from_cache(
            project_id, branch, cloudmap_path, latest_commit=latest_commit
        )
        if doc is None:
            if isinstance(err, Response):
                return err
            return create_error_response("INTERNAL_ERROR", str(err))
        if not isinstance(doc, dict):
            return create_error_response(
                "INTERNAL_ERROR", f"{cloudmap_path} is not a YAML mapping"
            )
    else:
        # Missing file -- start a new cloudmap rather than failing.
        logger.info("creating new cloudmap at %s", cloudmap_path)
        doc = dict(apiVersion=API_VERSION, kind="CloudMap")
    if starting_revision and latest_commit and starting_revision != latest_commit:
        return create_error_response(
            "CONFLICT",
            f"cloudmap has changed since latest_commit {latest_commit}, "
            f"current revision is {starting_revision}",
        )

    # Apply the body to `doc`. A record with `unfurl.server.deleted:
    # true` is removed; any other object replaces (or inserts). Track
    # which records actually changed so the response can list them in
    # ``applied`` (mirrors the rust handler's per-record response).
    # See the docstring above for the OCC / `atomic` story.
    applied: List[Dict[str, Any]] = []
    for section, entries in body_sections.items():
        section_doc: Dict[str, Any] = doc.setdefault(section, {})
        for key, value in entries.items():
            if not isinstance(value, dict):
                return create_error_response(
                    "BAD_REQUEST", f"{section}.{key}: value must be a JSON object"
                )
            payload = dict(value)
            if_exists = payload.pop("unfurl.server.if_exists", False)
            merge = payload.pop("unfurl.server.merge", False)
            try:
                deletes = _merge_deletes(merge)
            except ValueError as e:
                return create_error_response("BAD_REQUEST", f"{section}.{key}: {e}")
            existing = section_doc.get(key)
            if if_exists and existing is None:
                continue
            if payload.pop("unfurl.server.deleted", False):
                if section_doc.pop(key, None) is not None:
                    applied.append({"section": section, "key": key, "version": 0})
            else:
                # Strip OCC keys so they don't leak into the persisted YAML.
                payload.pop("unfurl.server.commit", None)
                payload.pop("unfurl.server.version", None)
                if merge and isinstance(existing, dict):
                    payload = _merge_record(existing, payload)
                for tokens in deletes:
                    payload = _without_field(payload, tokens)
                if section_doc.get(key) != payload:
                    section_doc[key] = payload
                    applied.append({"section": section, "key": key, "version": 0})

    if applied:
        try:
            # A new `cloudmap_path` may name a directory the repo doesn't have yet.
            parent = os.path.dirname(full_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(full_path, "w") as f:
                yaml.dump(doc, f)
        except OSError as e:
            return create_error_response(
                "INTERNAL_ERROR", f"could not write {cloudmap_path}: {e}"
            )
    elif not (commit_requested and repo.is_dirty(True, full_path)):
        # Nothing to write. A body with no records is still meaningful when the
        # caller asked to commit -- it means "commit what earlier `commit: false`
        # requests left in the working tree" -- but only if there is in fact
        # something uncommitted there.
        return {"commit": repo.revision or None, "applied": []}

    if commit_requested is False:
        # Leave the change in the working tree for a later request to commit.
        # `commit` reports where the repository is, as everywhere else -- here
        # that's the unchanged HEAD, which is what the caller should send as
        # `latest_commit` on the follow-up request that does commit.
        return {"commit": repo.revision or None, "applied": applied}

    # Commit locally (no push). Everything above guarantees the working tree
    # is dirty, so no further `is_dirty()` check is required here.
    commit_err = _commit_and_push(
        repo,
        full_path,
        commit_msg,
        username,
        password,
        starting_revision,
        project_id,
        branch,
        batched=True,
        author=_get_author(request),
    )
    if commit_err:
        return commit_err
    new_commit = repo.revision

    return {"commit": new_commit, "applied": applied}



def _analyzable_url(url: str) -> Optional[str]:
    """``url`` as ``POST /cloudmap/analyze`` passes it to ``CloudMap.analyze_url``,
    or None if it names the server's own filesystem.

    ``analyze_url`` treats a bare name as a local path when one exists, so bare
    names are converted to the container image PURL it would otherwise make of
    them -- unconditionally, so a response doesn't reveal which paths exist.
    """
    from ..support import ContainerImageParts
    from ..tosca_plugins.cloudmap_defs import build_oci_purl

    scheme = urlparse(url).scheme
    if not scheme:
        return build_oci_purl(ContainerImageParts.split(url))
    if scheme in ("file", "git-local") or scheme.endswith("+file"):
        return None
    return url


def _cloudmap_local_env() -> Optional[LocalEnv]:
    """The environment ``POST /cloudmap/analyze`` finds repository hosts and
    custom analyzers in: the server's own, not the cloudmap project's, which
    needn't be an Unfurl project. None if the server has neither a project nor
    a home project."""
    gui_env = current_app.config.get("UNFURL_GUI_MODE")
    if isinstance(gui_env, LocalEnv):
        return gui_env
    options = current_app.config.get("UNFURL_OPTIONS") or {}
    try:
        return LocalEnv(
            current_app.config.get("UNFURL_CURRENT_WORKING_DIR"),
            options.get("home"),
            can_be_empty=True,
            readonly=True,
        )
    except UnfurlError as e:
        logger.verbose("analyzing without repository hosts: %s", e)
        return None


def _analysis_clone_root(local_env: Optional[LocalEnv]) -> str:
    """Where ``POST /cloudmap/analyze`` clones the repositories it analyzes: the
    ``clone_root`` configured for the cloudmap, as ``CloudMap.from_name`` uses."""
    from ..cloudmap import CloudMap

    if local_env:
        clone_root = CloudMap.get_config(local_env, "cloudmap")[3].get("clone_root")
        if clone_root:
            return str(clone_root)
    return os.path.join(
        current_app.config.get("UNFURL_CLONE_ROOT", "."), ".cloudmap-repos"
    )


def _cloudmap_sections(db: CloudMapDB) -> Dict[str, Dict[str, Any]]:
    """Copy of ``db``'s record sections in the form they're saved in."""
    import copy

    db.save()  # a cloudmap loaded from the cache has no file: this only serializes
    return copy.deepcopy(
        {section: dict(db.db.get(section) or {}) for section in _CLOUDMAP_SECTIONS}
    )


def _changed_sections(
    before: Dict[str, Dict[str, Any]], after: Dict[str, Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    """The ``POST /cloudmap`` body that turns ``before`` into ``after``."""
    changes: Dict[str, Dict[str, Any]] = {}
    for section in _CLOUDMAP_SECTIONS:
        old, new = before.get(section, {}), after.get(section, {})
        changed = {key: value for key, value in new.items() if old.get(key) != value}
        changed.update(
            (key, {"unfurl.server.deleted": True}) for key in old if key not in new
        )
        if changed:
            changes[section] = changed
    return changes


def _analyze_urls(
    cloud_map: "CloudMap",
    requested: List[Tuple[str, bool]],
    body: CloudMapAnalyzeRequest,
) -> Tuple[List[Dict[str, str]], List[str], str]:
    """Analyze each ``(url, replacing)`` as ``unfurl cloudmap --add/--replace``
    does, then record the ``deleted``, ``private`` and ``moved`` repositories.
    Returns the records added or updated, the urls skipped and a commit message."""
    from ..tosca_plugins.cloudmap_defs import section_of

    added: List[Dict[str, str]] = []
    skipped: List[str] = []

    def report(url: str, record: Optional["CloudMapRecord"]) -> None:
        if record is None:
            skipped.append(url)
        else:
            added.append(dict(url=url, section=section_of(record), key=record.key))

    for url, replacing in requested:
        analyzable = assert_not_none(_analyzable_url(url))
        report(url, cloud_map.analyze_url(analyzable, body.analyze, replacing))
    for url in body.deleted:
        report(url, cloud_map.update_repository(url, status="deleted"))
    for url in body.private:
        report(url, cloud_map.update_repository(url, private=True))
    for move in body.moved:
        report(
            move.from_,
            cloud_map.update_repository(move.from_, status="moved", moved_to=move.to),
        )
    only_added = not (body.replace or body.deleted or body.private or body.moved)
    verb = "Added" if only_added else "Updated"
    names = ", ".join(r["key"] for r in added)
    return added, skipped, body.commit_msg or f"{verb} {len(added)} record(s): {names}"


@app.post("/cloudmap/analyze")
@app.doc(
    summary="Add records to the CloudMap by analyzing URLs",
    description=(
        "Like ``unfurl cloudmap --add`` / ``--replace``: analyze each URL and add "
        "the records it produces to ``cloudmap.yaml``. Records are written the "
        "same way as ``POST /cloudmap``, through the rust cloudmap server when "
        "one is configured. ``file:`` and ``git-local:`` URLs are rejected, and a "
        "bare name is always taken to be a container image.\n\n"
        "``deleted``, ``private`` and ``moved`` report what happened to "
        "repositories already in the cloudmap; their records are updated, "
        "never removed."
    ),
    tags=["Export"],
)
@app.input(ProjectAuthQuery, location="query", arg_name="query")
@app.input(CloudMapAnalyzeRequest, location="json", arg_name="body")
@app.output(CloudMapAnalyzeResponse, description="the records added and the resulting commit")
def post_cloudmap_analyze(
    query: ProjectAuthQuery, body: CloudMapAnalyzeRequest
) -> ResponseReturnValue:
    from .cache import CLOUDMAP_BRANCH, get_cloudmap_proxy, load_cloudmap_db
    from ..cloudmap import CloudMap
    from ..cloudmap.proxy import CloudMapProxyConflict, CloudMapProxyError

    project_id = get_project_id_or_abort(request)
    cloudmap_path = body.cloudmap_path or "cloudmap.yaml"
    # a url given to both is replaced, which is a superset of adding it
    requested = [(u, False) for u in body.add if u not in body.replace]
    requested += [(u, True) for u in body.replace]
    events = body.deleted + body.private + [m.from_ for m in body.moved]
    if not requested and not events:
        return create_error_response("BAD_REQUEST", "no urls given")
    urls = [u for u, _ in requested] + events + [m.to for m in body.moved]
    for url in urls:
        if _analyzable_url(url) is None:
            return create_error_response(
                "BAD_REQUEST", f"{url}: local file urls can't be analyzed"
            )

    branch = body.branch or CLOUDMAP_BRANCH
    proxy = get_cloudmap_proxy(project_id, cloudmap_path, body.latest_commit)
    db: Optional[CloudMapDB] = None
    before: Dict[str, Dict[str, Any]] = {}
    store: CloudMapStore
    if proxy is not None:
        store = proxy
    else:
        err, db = load_cloudmap_db(
            project_id,
            branch,
            cloudmap_path,
            latest_commit=body.latest_commit,
            copy_doc=True,
        )
        if db is None:
            if isinstance(err, Response):
                return err
            return create_error_response("INTERNAL_ERROR", str(err))
        store = db
        before = _cloudmap_sections(db)

    local_env = _cloudmap_local_env()
    cloud_map = CloudMap(
        None,  # records are saved through the store, not committed by the CloudMap
        "",
        localrepo_root=_analysis_clone_root(local_env),
        skip_analysis=body.analyze in ("no", "metadata"),
        commit=body.commit is not False,
        logger=logger,
        local_env=local_env,
        db=store,
    )
    added, skipped, commit_msg = _analyze_urls(cloud_map, requested, body)

    if proxy is not None:
        new_commit = None
        if added:
            try:
                new_commit = proxy.save(commit_msg, commit=body.commit is not False)
            except CloudMapProxyConflict as e:
                return create_error_response("CONFLICT", str(e))
            except CloudMapProxyError as e:
                return create_error_response("INTERNAL_ERROR", str(e))
        return {"commit": new_commit, "added": added, "skipped": skipped}

    db = assert_not_none(db)  # loaded above when there's no proxy
    result = _apply_cloudmap_sections(
        project_id,
        branch,
        cloudmap_path,
        _changed_sections(before, _cloudmap_sections(db)),
        body.commit,
        body.latest_commit,
        body.username or "",
        body.private_token or "",
        commit_msg,
    )
    if not isinstance(result, dict):
        return result  # an error response
    return {"commit": result.get("commit"), "added": added, "skipped": skipped}


@app.get("/graph")
@app.doc(
    summary="CloudMap graph",
    description="Return the CloudMap dependency graph as JSON, optionally filtered to a single URL.",
    tags=["Export"],
)
@app.input(CloudMapQuery, location="query", arg_name="query")
@app.output(CloudMapResponse, description="CloudMap dependency graph as JSON")
def get_cloudmap_graph(query: CloudMapQuery) -> ResponseReturnValue:
    from .cache import CLOUDMAP_PATH, get_cloudmap_view
    from ..reporting import cloudmap_graph_json

    project_id = _cloudmap_project_id(request)
    # NB: rust server doesn't filter by CLOUDMAP_PATH when cloudmap_path is not specified
    err, db = get_cloudmap_view(project_id, file_name=query.cloudmap_path)
    if db is None:
        if isinstance(err, Response):
            return err
        return create_error_response("INTERNAL_ERROR", str(err))
    url = request.args.get("url") or ""
    result = cloudmap_graph_json(db, url)
    if "error" in result:
        # a record the graph couldn't find is reported as that key alone
        return create_error_response("NOT_FOUND", str(result["error"]))
    return result
