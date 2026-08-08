# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""Unit tests for :class:`unfurl.cloudmap.CloudMapProxy` with HTTP mocked.

Integration coverage that exercises the live rust server lives in
``tests/test_server.py`` (see the ``test_cloudmap_proxy_*`` cases).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from unfurl.cloudmap.proxy import (
    CloudMapProxy,
    CloudMapProxyConflict,
    CloudMapProxyError,
)
from unfurl.tosca_plugins.cloudmap_defs import (
    Artifact,
    ArtifactMetadata,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _artifact_payload(
    url: str = "pkg:oci/example/image@1.0",
    *,
    version: int = 5,
    commit: Optional[str] = "deadbeef",
    type_name: str = "unfurl.artifacts.OCIImage",
) -> Dict[str, Any]:
    """Build a JSON-shaped artifact record (same shape the server emits)."""
    payload: Dict[str, Any] = {
        "type": {type_name: None},
        "metadata": {"title": "example"},
        "unfurl.server.version": version,
    }
    if commit is not None:
        payload["unfurl.server.commit"] = commit
    return payload


def _service_payload(
    url: str = "https://example.com/svc",
    *,
    version: int = 7,
) -> Dict[str, Any]:
    return {
        "type": {"unfurl.services.HTTP": None},
        "unfurl.server.version": version,
        "unfurl.server.commit": "cafef00d",
    }


def _make_response(payload: Any, *, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.json.return_value = payload
    resp.text = ""
    return resp


def _make_proxy(
    get_returns: Optional[List[MagicMock]] = None,
    post_returns: Optional[List[MagicMock]] = None,
) -> Tuple[CloudMapProxy, MagicMock]:
    """Construct a CloudMapProxy with a mocked requests.Session.

    ``auth_project`` is passed via the base URL's query string so the
    proxy preserves it verbatim on every request.
    """
    session = MagicMock()
    if get_returns:
        session.get.side_effect = get_returns
    if post_returns:
        session.post.side_effect = post_returns
    proxy = CloudMapProxy(
        "https://api.example.com/services/unfurl-server?auth_project=acme/prod",
        username="bot",
        private_token="tok",
        session=session,
    )
    return proxy, session


# ---------------------------------------------------------------------------
# Tests — reads
# ---------------------------------------------------------------------------


def test_get_artifact_fetches_then_caches() -> None:
    """A miss issues one GET; the second call is fully local."""
    url = "pkg:oci/example/image@1.0"
    fetched_pair = [
        {"artifacts": {url: _artifact_payload(url, version=5)}},
        {},
    ]
    proxy, session = _make_proxy(get_returns=[_make_response(fetched_pair)])

    art = proxy.get_artifact(url)
    assert isinstance(art, Artifact)
    assert session.get.call_count == 1
    call = session.get.call_args
    assert call.args[0] == "https://api.example.com/services/unfurl-server/cloudmap"
    # ``params`` is a list of (k, v) pairs so multi-valued base_url query
    # entries round-trip; convert to dict for comparison.
    assert dict(call.kwargs["params"]) == {
        "auth_project": "acme/prod",
        "kind": "artifacts",
        "key": url,
        "follow": "1024",
    }

    # Second call is a local cache hit.
    art2 = proxy.get_artifact(url)
    assert art2 is art
    assert session.get.call_count == 1


def test_get_artifact_populates_followed_records() -> None:
    """Followed records pre-warm the cache for unrelated kinds."""
    art_url = "pkg:oci/example/image@1.0"
    svc_url = "https://example.com/svc"
    pair = [
        {"artifacts": {art_url: _artifact_payload(art_url, version=5)}},
        {"services": {svc_url: _service_payload(svc_url, version=7)}},
    ]
    proxy, session = _make_proxy(get_returns=[_make_response(pair)])

    proxy.get_artifact(art_url)
    # The service was in `followed` — should be a cache hit, no extra GET.
    svc = proxy.get_service(svc_url)
    assert svc is not None
    assert session.get.call_count == 1


def test_get_record_by_cloudmap_url() -> None:
    """The endpoint identifies the cloudmap document the proxy mirrors."""
    endpoint = "https://api.example.com/services/unfurl-server/cloudmap"
    svc_url = "https://example.com/svc"
    pair = [{"services": {svc_url: _service_payload(svc_url, version=7)}}, {}]
    proxy, session = _make_proxy(get_returns=[_make_response(pair)])
    assert proxy._cache.path == endpoint

    # a reference to the served cloudmap resolves, by url or relative to it
    assert proxy.get_service(f"cloudmap:[{endpoint}]:service:{svc_url}") is not None
    assert session.get.call_count == 1
    assert dict(session.get.call_args.kwargs["params"])["key"] == svc_url
    assert proxy.get_service(f"cloudmap:[cloudmap]:service:{svc_url}") is not None
    # a reference to another cloudmap doesn't, and isn't fetched
    assert proxy.get_service("cloudmap:[file:other.yaml]:service:" + svc_url) is None
    assert proxy.get_service(f"cloudmap:[{endpoint}/other]:service:{svc_url}") is None
    assert session.get.call_count == 1


def test_get_returns_none_on_404_and_caches_negative() -> None:
    proxy, session = _make_proxy(get_returns=[_make_response(None, status=404)])

    assert proxy.get_artifact("pkg:oci/missing") is None
    # Second call for the same key shouldn't refetch.
    assert proxy.get_artifact("pkg:oci/missing") is None
    assert session.get.call_count == 1


def test_find_artifacts_per_section_fetch_and_iterator() -> None:
    art_url = "pkg:oci/example/image@1.0"
    pair = [
        {"artifacts": {art_url: _artifact_payload(art_url)}},
        {},
    ]
    proxy, session = _make_proxy(get_returns=[_make_response(pair)])

    result = proxy.find_artifacts()
    # Must be an iterator (forward-compatible with paging).
    assert iter(result) is result
    items = list(result)
    assert len(items) == 1
    assert isinstance(items[0], Artifact)

    # Verify the request shape: kind=artifacts, no key, no follow.
    call = session.get.call_args
    assert dict(call.kwargs["params"]) == {
        "auth_project": "acme/prod",
        "kind": "artifacts",
    }

    # Second call doesn't refetch.
    list(proxy.find_artifacts())
    assert session.get.call_count == 1


def test_find_filters_by_type() -> None:
    pair = [
        {
            "artifacts": {
                "pkg:oci/a": {
                    "type": {"unfurl.artifacts.OCIImage": None},
                    "unfurl.server.version": 1,
                },
                "pkg:helm/b": {
                    "type": {"unfurl.artifacts.HelmChart": None},
                    "unfurl.server.version": 2,
                },
            }
        },
        {},
    ]
    proxy, _ = _make_proxy(get_returns=[_make_response(pair)])

    helm = list(proxy.find_artifacts(type="unfurl.artifacts.HelmChart"))
    assert len(helm) == 1
    assert helm[0].url == "pkg:helm/b"


def test_max_version_tracked_from_records() -> None:
    pair = [
        {
            "artifacts": {
                "pkg:oci/a": _artifact_payload("pkg:oci/a", version=4),
                "pkg:oci/b": _artifact_payload("pkg:oci/b", version=11),
            }
        },
        {},
    ]
    proxy, _ = _make_proxy(get_returns=[_make_response(pair)])

    list(proxy.find_artifacts())
    assert proxy._cache._max_version == 11
    # OCC tokens live as private attrs on the cached dataclass — no
    # parallel payload dict.
    record = proxy.get_artifact("pkg:oci/b")
    assert record is not None
    assert record._unfurl_server_version == 11
    assert record._unfurl_server_commit == "deadbeef"


# ---------------------------------------------------------------------------
# Tests — writes
# ---------------------------------------------------------------------------


def test_save_no_pending_writes_is_noop() -> None:
    proxy, session = _make_proxy()
    assert proxy.save() is None
    assert session.post.call_count == 0


def test_save_includes_envelope_and_round_trips_occ_for_known_key() -> None:
    """Updating a previously-fetched record carries forward
    unfurl.server.{version,commit}.
    """
    url = "pkg:oci/example/image@1.0"
    pair = [
        {"artifacts": {url: _artifact_payload(url, version=5)}},
        {},
    ]
    proxy, session = _make_proxy(
        get_returns=[_make_response(pair)],
        post_returns=[_make_response({"commit": "newoid", "queueid": 6})],
    )

    # Fetch the existing record so the OCC tokens land on the dataclass.
    fetched = proxy.get_artifact(url)
    assert fetched is not None
    # Mutate by re-adding a record with the same URL.
    fetched.metadata = ArtifactMetadata(title="renamed")
    proxy.add_record(fetched)

    new_commit = proxy.save()
    assert new_commit == "newoid"
    assert proxy._cache._latest_commit == "newoid"
    # The response's queueid is folded into _max_version (same monotonic
    # counter as unfurl.server.version, per cloudmap.rs:329-333).
    assert proxy._cache._max_version == 6

    # The POST body should carry envelope + the section payload, with
    # OCC keys copied forward from the cached payload.
    post_call = session.post.call_args
    assert post_call.args[0].endswith("/cloudmap")
    body = post_call.kwargs["json"]
    assert body["latest_commit"] == "deadbeef"  # captured on prior GET
    # Credentials are sent as the X-Git-Credentials header (matching the
    # JS client) and additionally echoed in the body for backward
    # compatibility with the Python server's pre-header path.
    assert body["username"] == "bot"
    assert body["private_token"] == "tok"
    headers = post_call.kwargs["headers"]
    import base64 as _b64

    assert _b64.b64decode(headers["X-Git-Credentials"]).decode() == "bot:tok"
    # The proxy no longer sends a top-level queueid — that field is
    # only relevant on the rust proxy's redis-batched-write path,
    # which the proxy doesn't drive.
    assert "queueid" not in body
    record = body["artifacts"][url]
    assert record["unfurl.server.version"] == 5
    assert record["unfurl.server.commit"] == "deadbeef"
    # Pending buffer is cleared on success.
    assert proxy._pending_writes == {}

    # The just-posted dataclass now carries the response's OCC tokens
    # as private attrs, so a follow-up add_artifact + save() round-
    # trips them back to the server (instead of the stale pre-POST
    # values).
    record = proxy.get_artifact(url)
    assert record is not None
    assert record._unfurl_server_version == 6
    # The response carried a queueid, so that is the token stamped and the
    # response's `commit` is left off the record. `commit` reports where the
    # *repository* is, which is not the same as where this row landed: the rust
    # handler answers with HEAD whether it committed the row or left it staged.
    # A `Pending(version)` token is valid in both cases (it survives
    # `commit_repository` rolling the commit forward), while a `Commit(oid)`
    # token only matches a row that really carries that commit -- and the
    # server's `pop_commit_ref` prefers the commit key when both are sent, so
    # stamping it would be the riskier of the two.
    assert record._unfurl_server_commit is None


def test_save_brand_new_record_has_no_occ_keys() -> None:
    """Adding a record we've never fetched omits OCC keys (none to send)."""
    proxy, session = _make_proxy(
        post_returns=[_make_response({"commit": "abc123", "queueid": 1})],
    )

    art = Artifact(
        url="pkg:oci/brand/new@1.0",
        metadata=ArtifactMetadata(title="new"),
    )
    proxy.add_record(art)
    proxy.save()

    body = session.post.call_args.kwargs["json"]
    record = body["artifacts"]["pkg:oci/brand/new@1.0"]
    assert "unfurl.server.version" not in record
    assert "unfurl.server.commit" not in record


def test_save_default_does_not_send_atomic_field() -> None:
    """``atomic=True`` is the server default, so ``save()`` omits it
    from the body to keep payloads minimal."""
    proxy, session = _make_proxy(
        post_returns=[_make_response({"commit": "x", "queueid": 1, "applied": []})]
    )
    proxy.add_record(Artifact(url="pkg:oci/x", metadata=ArtifactMetadata(title="x")))
    proxy.save()
    assert "atomic" not in session.post.call_args.kwargs["json"]


def test_save_atomic_false_sends_flag() -> None:
    proxy, session = _make_proxy(
        post_returns=[_make_response({"commit": "x", "queueid": 1, "applied": []})]
    )
    proxy.add_record(Artifact(url="pkg:oci/x", metadata=ArtifactMetadata(title="x")))
    proxy.save(atomic=False)
    assert session.post.call_args.kwargs["json"].get("atomic") is False


def test_save_non_atomic_partial_stamps_applied_and_evicts_failed() -> None:
    """In non-atomic mode the 409 body lists ``applied`` (records that
    landed) and ``failed`` (records that didn't). The proxy stamps OCC
    tokens on the applied records so retries don't double-apply, and
    evicts the failed ones from cache + pending writes."""
    proxy, session = _make_proxy(
        post_returns=[
            _make_response(
                {
                    "error": "conflict",
                    "section": "artifacts",
                    "key": "pkg:oci/bad",
                    "actual": "newer",
                    "applied": [
                        {"section": "artifacts", "key": "pkg:oci/good", "version": 17}
                    ],
                    "failed": [
                        {
                            "section": "artifacts",
                            "key": "pkg:oci/bad",
                            "actual": "newer",
                            "error": "conflict",
                        }
                    ],
                },
                status=409,
            )
        ]
    )
    good = Artifact(url="pkg:oci/good", metadata=ArtifactMetadata(title="good"))
    bad = Artifact(url="pkg:oci/bad", metadata=ArtifactMetadata(title="bad"))
    proxy.add_record(good)
    proxy.add_record(bad)

    with pytest.raises(CloudMapProxyConflict) as excinfo:
        proxy.save(atomic=False)

    err = excinfo.value
    assert err.applied == [("artifacts", "pkg:oci/good", 17)]
    assert err.failed == [("artifacts", "pkg:oci/bad", "newer", "conflict")]
    # Both records dropped from pending writes (good was applied; bad
    # was evicted).
    assert proxy._pending_writes == {}
    # Good record is still in the cache (it was committed) and now
    # carries the server's OCC version.
    cached_good = proxy._cache.get_artifact("pkg:oci/good")
    assert cached_good is not None
    assert cached_good._unfurl_server_version == 17
    # Bad record was evicted from the cache so the next get_* refetches.
    assert proxy._cache.get_artifact("pkg:oci/bad") is None


def test_save_conflict_drops_conflicting_keeps_others() -> None:
    """On 409 the conflicting record is dropped from ``_pending_writes``
    and evicted from the cache; other pending writes survive so the
    caller can retry them after reconciling the conflict.
    """
    proxy, session = _make_proxy(
        post_returns=[
            _make_response(
                {
                    "error": "conflict",
                    "section": "artifacts",
                    "key": "pkg:oci/x",
                    "actual": "deadbeef",
                    "applied": [],
                    "failed": [
                        {
                            "section": "artifacts",
                            "key": "pkg:oci/x",
                            "actual": "deadbeef",
                            "error": "conflict",
                        }
                    ],
                },
                status=409,
            )
        ]
    )

    conflicted = Artifact(
        url="pkg:oci/x",
        metadata=ArtifactMetadata(title="x"),
    )
    other = Artifact(
        url="pkg:oci/y",
        metadata=ArtifactMetadata(title="y"),
    )
    proxy.add_record(conflicted)
    proxy.add_record(other)

    with pytest.raises(CloudMapProxyConflict) as excinfo:
        proxy.save()

    assert excinfo.value.section == "artifacts"
    assert excinfo.value.key == "pkg:oci/x"
    assert excinfo.value.actual == "deadbeef"
    # Conflicting record is gone from pending writes and from the cache.
    assert "pkg:oci/x" not in proxy._pending_writes.get("artifacts", {})
    assert proxy._cache.artifacts.get("pkg:oci/x") is None
    # Non-conflicting pending write survives.
    assert "pkg:oci/y" in proxy._pending_writes["artifacts"]


def test_save_conflict_logs_warning(caplog) -> None:
    """A 409 logs a warning with section/key/actual before raising."""
    proxy, _ = _make_proxy(
        post_returns=[
            _make_response(
                {
                    "error": "conflict",
                    "section": "artifacts",
                    "key": "pkg:oci/x",
                    "actual": "newer",
                },
                status=409,
            )
        ]
    )
    proxy.add_record(Artifact(url="pkg:oci/x", metadata=ArtifactMetadata(title="x")))
    with caplog.at_level("WARNING", logger="unfurl.cloudmap.proxy"):
        with pytest.raises(CloudMapProxyConflict):
            proxy.save()
    record_messages = [r.getMessage() for r in caplog.records]
    assert any(
        "cloudmap conflict on artifacts/pkg:oci/x" in m and "newer" in m
        for m in record_messages
    ), record_messages


def test_save_propagates_other_http_errors() -> None:
    proxy, _ = _make_proxy(post_returns=[_make_response({"error": "boom"}, status=500)])
    art = Artifact(
        url="pkg:oci/y",
        metadata=ArtifactMetadata(title="y"),
    )
    proxy.add_record(art)
    with pytest.raises(CloudMapProxyError):
        proxy.save()


def test_refresh_uses_since_version() -> None:
    """After ingesting some records, refresh() must pass the highest
    observed version as `since_version`.
    """
    pair = [
        {
            "artifacts": {
                "pkg:oci/a": _artifact_payload("pkg:oci/a", version=11),
            }
        },
        {},
    ]
    delta_pair = [
        {
            "artifacts": {
                "pkg:oci/b": _artifact_payload("pkg:oci/b", version=12),
            }
        },
        {},
    ]
    proxy, session = _make_proxy(
        get_returns=[
            _make_response(pair),
            _make_response(delta_pair),
        ]
    )

    list(proxy.find_artifacts())
    proxy.refresh()

    second_call = session.get.call_args_list[1]
    assert dict(second_call.kwargs["params"]) == {
        "auth_project": "acme/prod",
        "since_version": "11",
    }
    assert proxy._cache._max_version == 12


def test_base_url_query_preserved_on_every_call() -> None:
    """Query params on the base URL ride along on every GET and POST."""
    pair = [{"artifacts": {}}, {}]
    proxy, session = _make_proxy(
        get_returns=[_make_response(pair)],
        post_returns=[_make_response({"commit": "x", "queueid": 1})],
    )

    list(proxy.find_artifacts())
    art = Artifact(
        url="pkg:oci/z",
        metadata=ArtifactMetadata(title="z"),
    )
    proxy.add_record(art)
    proxy.save()

    assert dict(session.get.call_args.kwargs["params"])["auth_project"] == "acme/prod"
    assert dict(session.post.call_args.kwargs["params"]) == {
        "auth_project": "acme/prod"
    }
    # Credentials are sent in the X-Git-Credentials header (base64
    # username:token), matching the JS client. They're also echoed in
    # the body for the Python server's pre-header path.
    post_body = session.post.call_args.kwargs["json"]
    assert post_body["username"] == "bot"
    assert post_body["private_token"] == "tok"
    import base64 as _b64

    assert (
        _b64.b64decode(
            session.post.call_args.kwargs["headers"]["X-Git-Credentials"]
        ).decode()
        == "bot:tok"
    )
    # And on the GET path too.
    assert (
        _b64.b64decode(
            session.get.call_args.kwargs["headers"]["X-Git-Credentials"]
        ).decode()
        == "bot:tok"
    )


def test_credentials_only_when_both_set() -> None:
    """Header is omitted unless both username and private_token are set."""
    session = MagicMock()
    session.get.return_value = _make_response([{"artifacts": {}}, {}])
    proxy = CloudMapProxy(
        "https://api.example.com/services/unfurl-server?auth_project=acme/prod",
        username="bot",
        # no private_token
        session=session,
    )
    list(proxy.find_artifacts())
    assert "X-Git-Credentials" not in session.get.call_args.kwargs["headers"]


def test_save_does_not_acquire_latest_commit_for_a_write_only_client() -> None:
    """A POST response's ``commit`` is not a read-set marker.

    It reports where the repository is, so echoing it back on the next write
    asks the server to reject that write if *anything* was committed meanwhile
    -- including changes to records this client never looked at. A client that
    has only ever written has no read set to assert, so it shouldn't start
    asserting one.
    """
    proxy, session = _make_proxy(
        post_returns=[
            _make_response({"commit": "headoid", "queueid": 1, "applied": []}),
            _make_response({"commit": "headoid", "queueid": 2, "applied": []}),
        ],
    )
    proxy.add_record(Artifact(url="pkg:oci/x", metadata=ArtifactMetadata(title="x")))
    assert proxy.save() == "headoid"
    # The repo's HEAD came back, but the client didn't adopt it...
    assert proxy._cache._latest_commit is None
    # ... so the next write carries no repository-level OCC token.
    proxy.add_record(Artifact(url="pkg:oci/y", metadata=ArtifactMetadata(title="y")))
    proxy.save()
    assert "latest_commit" not in session.post.call_args.kwargs["json"]


def test_save_refreshes_an_existing_latest_commit() -> None:
    """A client that *does* hold a read-set token keeps it current.

    Without this the client's own commit would move HEAD past the token it is
    still sending, and its next write would conflict with itself.
    """
    proxy, _session = _make_proxy(
        post_returns=[_make_response({"commit": "newoid", "queueid": 1, "applied": []})]
    )
    proxy._cache._latest_commit = "oldoid"  # as a prior GET would have set it
    proxy.add_record(Artifact(url="pkg:oci/x", metadata=ArtifactMetadata(title="x")))
    proxy.save()
    assert proxy._cache._latest_commit == "newoid"
