import json
import os
from functools import cache
import pytest
from dataclasses import replace

from unfurl.cloudmap import oci
from unfurl.tosca_plugins.cloudmap_defs import (
     Artifact,
     ArtifactMetadata,
     Discovery,
    Instantiation,
     TypeRefs,
)
from unfurl.tosca_plugins.functions import ContainerImageParts
from unfurl.support import ContainerImage

UNFURL_TEST_GITHUB_KEY = os.getenv("UNFURL_TEST_GITHUB_KEY")


@cache
def _ghcr_credentials() -> dict:
    """Credentials for ghcr.io, when a token that actually works is available.

    Anonymous pulls of *public* packages work, which is why these tests need
    no credentials -- but the anonymous quota is per IP and shared with
    everything else on a CI runner, so a token is what keeps them off the
    rate limit. GHCR accepts any username alongside a valid token; Actions
    provides the one it expects as GITHUB_ACTOR.

    The token is probed once rather than trusted: one without `packages:
    read` doesn't raise, it just makes `registry_v2_fetch` return nothing, so
    an under-scoped token would turn these tests from passing-anonymously
    into failing. Falling back keeps that a non-event.
    """
    if not UNFURL_TEST_GITHUB_KEY:
        return {}
    credentials = {
        "username": os.getenv("GITHUB_ACTOR", "x-access-token"),
        "password": UNFURL_TEST_GITHUB_KEY,
    }
    probe = ContainerImage.make("ghcr.io/onecommons/unfurl:v1.1.0-server-cached")
    assert probe
    _annotations, platforms, _digest, _fetch = oci.registry_v2_fetch(
        probe.parts, **credentials
    )
    return credentials if platforms else {}


def _registry_credentials(image_url: str) -> dict:
    """Credentials for ``image_url``'s registry, if any are needed and usable."""
    return _ghcr_credentials() if image_url.startswith("ghcr.io/") else {}


artifact_keys = [
    "docker.io/baserow/baserow",
    "lscr.io/linuxserver/wireguard",
    "registry.gitlab.com/gitlab-org/project-templates/express/main",
    "ghcr.io/onecommons/unfurl:v1.1.0-server-cached",
    "ghcr.io/actions/actions-runner:latest",
    "registry.gitlab.com/gitlab-org/build/cng/gitlab-toolbox-ce:master",
    "registry.unfurl.cloud/onecommons/unfurl-gui@sha256:c21af1741b31f33ccd44f096003dfcd576adda854415fffa21290796a0689d32",
]


@pytest.mark.parametrize(
    "image_url,expected_ref,expected_artifact,expected_instantiation",
    [
        (
            "docker.io/baserow/baserow:2.1.2",
            ContainerImageParts(
                full_name="baserow/baserow",
                tag="2.1.2",
                digest="",
                registry="docker.io",
            ),
            Artifact(
                url="pkg:oci/baserow?repository_url=docker.io/baserow/baserow&tag=2.1.2",
                type=TypeRefs({"cloudmap.artifacts.oci.Image": None}),
                digest="sha256:60e2e1215f4e020c36cb1233dab21514c214f0347dc9e2d7f3ae6d1b01d9044c",
                metadata=ArtifactMetadata(
                    source_url="https://github.com/baserow/baserow",
                    description="All in one docker image for Baserow, open source no-code platform tool and Airtable alternative",
                    title="baserow",
                    platforms=[
                        {"architecture": "amd64", "os": "linux"},
                        {"architecture": "arm64", "os": "linux"},
                    ],
                    spdx_licenses="",
                    vendor="",
                    version="",
                    homepage_url="",
                    documentation_url="",
                ),
            ),
            Instantiation(
                type=TypeRefs(
                    {
                        "cloudmap.artifacts.InTotoAttestation": None,
                        "cloudmap.artifacts.SlsaProvenance02": None,
                    }
                ),
                source="https://github.com/baserow/baserow",
                source_revision="22bcfac3b835974a4a0787f5fa8d2d1b06ae58b1",
            ),
        ),
        (
            "lscr.io/linuxserver/wireguard",
            ContainerImageParts(
                full_name="linuxserver/wireguard", tag="", digest="", registry="lscr.io"
            ),
            Artifact(
                url="pkg:oci/wireguard?repository_url=lscr.io/linuxserver/wireguard",
                type=TypeRefs({"cloudmap.artifacts.oci.Image": None}),
                digest="",  # Will be replaced in test
                metadata=ArtifactMetadata(
                    source_url="https://github.com/linuxserver/docker-wireguard",
                    description="[WireGuard®] is an extremely simple yet fast and modern VPN that utilizes state-of-the-art cryptography. It aims to be faster, simpler, leaner, and more useful than IPsec, while avoiding the massive headache. It intends to be considerably more performant than OpenVPN. WireGuard is designed as a general purpose VPN for running on embedded interfaces and super computers alike, fit for many different circumstances. Initially released for the Linux kernel, it is now cross-platform (Windows, macOS, BSD, iOS, Android) and widely deployable. It is currently under heavy development, but already it might be regarded as the most secure, easiest to use, and simplest VPN solution in the industry.",
                    title="Wireguard",
                    platforms=[
                        {"architecture": "amd64", "os": "linux"},
                        {"architecture": "arm64", "os": "linux"},
                    ],
                    spdx_licenses="GPL-3.0-only",
                    vendor="linuxserver.io",
                    version="IGNORE",  # version may change
                    homepage_url="https://www.wireguard.com/",
                    documentation_url="https://docs.linuxserver.io/images/docker-wireguard",
                ),
            ),
            Instantiation(
                type=TypeRefs(
                    {
                        "cloudmap.artifacts.InTotoAttestation": None,
                        "cloudmap.artifacts.SpdxDocument": None,
                    }
                ),
                source="https://github.com/linuxserver/docker-wireguard",
                source_revision="IGNORE",  # extracted from annotations, will change with new builds
            ),  # has https://spdx.dev/Document in-toto artifact (generated by syft)
        ),
        (
            "registry.gitlab.com/gitlab-org/project-templates/express/main",
            ContainerImageParts(
                full_name="gitlab-org/project-templates/express/main",
                tag="",
                digest="",
                registry="registry.gitlab.com",
            ),
            Artifact(
                url="pkg:oci/main?repository_url=registry.gitlab.com/gitlab-org/project-templates/express/main",
                type=TypeRefs({"cloudmap.artifacts.oci.Image": None}),
                digest="",  # Will be replaced in test
                metadata=ArtifactMetadata(
                    source_url="",
                    description="",
                    title="",
                    platforms=[{"architecture": "amd64", "os": "linux"}],
                    spdx_licenses="",
                    vendor="",
                    version="",
                    homepage_url="https://gitlab.com/gitlab-org/project-templates",
                    documentation_url="",
                ),
            ),
            None,  # no artifact
        ),
        (
            "ghcr.io/onecommons/unfurl:v1.1.0-server-cached",
            ContainerImageParts(
                full_name="onecommons/unfurl",
                tag="v1.1.0-server-cached",
                digest="",
                registry="ghcr.io",
            ),
            Artifact(
                url="pkg:oci/unfurl?repository_url=ghcr.io/onecommons/unfurl&tag=v1.1.0-server-cached",
                type=TypeRefs({"cloudmap.artifacts.oci.Image": None}),
                digest="sha256:4410d557ee799971770cf4fadc04e78fa5d2bd68470b6e6c6ebd49f32a59338d",
                metadata=ArtifactMetadata(
                    source_url="https://github.com/onecommons/unfurl",
                    description="",
                    title="",
                    platforms=[{"architecture": "amd64", "os": "linux"}],
                    spdx_licenses="",
                    vendor="",
                    version="",
                    homepage_url="",
                    documentation_url="",
                ),
            ),
            Instantiation(
                type=TypeRefs(
                    {
                        "cloudmap.artifacts.InTotoAttestation": None,
                        "cloudmap.artifacts.SlsaProvenance02": None,
                    }
                ),
                source="https://github.com/onecommons/unfurl",
                source_revision="f5da8de13ae2dcce293508c4ccac9b373e66dd49",
            ),
        ),
        (
            "ghcr.io/actions/actions-runner:latest",
            ContainerImageParts(
                full_name="actions/actions-runner",
                tag="latest",
                digest="",
                registry="ghcr.io",
            ),
            Artifact(
                url="pkg:oci/actions-runner?repository_url=ghcr.io/actions/actions-runner&tag=latest",
                type=TypeRefs({"cloudmap.artifacts.oci.Image": None}),
                digest="",  # Will be replaced in test
                metadata=ArtifactMetadata(
                    source_url="https://github.com/actions/runner",
                    description="IGNORE",  # ignore because this will change
                    title="",
                    platforms=[
                        {"architecture": "amd64", "os": "linux"},
                        {"architecture": "arm64", "os": "linux"},
                    ],
                    spdx_licenses="MIT",
                    vendor="",
                    version="24.04",
                    homepage_url="",
                    documentation_url="",
                ),
            ),
            Instantiation(
                type=TypeRefs(
                    {
                        "cloudmap.artifacts.InTotoAttestation": None,
                        "cloudmap.artifacts.SlsaProvenance1": None,
                    }
                ),
                source="https://github.com/actions/runner",
                source_revision="IGNORE",  # extracted from in-toto artifact, will change with new builds
            ),
        ),
        (
            "registry.gitlab.com/gitlab-org/build/cng/gitlab-toolbox-ce:master",
            ContainerImageParts(
                full_name="gitlab-org/build/cng/gitlab-toolbox-ce",
                tag="master",
                digest="",
                registry="registry.gitlab.com",
            ),
            Artifact(
                url="pkg:oci/gitlab-toolbox-ce?repository_url=registry.gitlab.com/gitlab-org/build/cng/gitlab-toolbox-ce&tag=master",
                type=TypeRefs({"cloudmap.artifacts.oci.Image": None}),
                digest="",  # Will be replaced in test
                metadata=ArtifactMetadata(
                    source_url="",
                    description="",
                    title="",
                    platforms=[
                        {"architecture": "amd64", "os": "linux"},
                        {"architecture": "arm64", "os": "linux"},
                    ],
                    spdx_licenses="",
                    vendor="",
                    version="",
                    homepage_url="https://gitlab.com/gitlab-org/build",
                    documentation_url="",
                ),
            ),
            None,  # no artifact
        ),
        (
            "registry.unfurl.cloud/onecommons/unfurl-gui@sha256:c21af1741b31f33ccd44f096003dfcd576adda854415fffa21290796a0689d32",
            ContainerImageParts(
                full_name="onecommons/unfurl-gui",
                tag="",
                digest="sha256:c21af1741b31f33ccd44f096003dfcd576adda854415fffa21290796a0689d32",
                registry="registry.unfurl.cloud",
            ),
            Artifact(
                url="pkg:oci/unfurl-gui@sha256%3Ac21af1741b31f33ccd44f096003dfcd576adda854415fffa21290796a0689d32?repository_url=registry.unfurl.cloud/onecommons/unfurl-gui",
                type=TypeRefs({"cloudmap.artifacts.oci.Image": None}),
                digest="sha256:c21af1741b31f33ccd44f096003dfcd576adda854415fffa21290796a0689d32",
                metadata=ArtifactMetadata(
                    source_url="",
                    description="",
                    title="",
                    platforms=[{"architecture": "amd64", "os": "linux"}],
                    spdx_licenses="",
                    vendor="",
                    version="",
                    homepage_url="https://unfurl.cloud/onecommons/unfurl-gui",
                    documentation_url="",
                ),
            ),
            None,  # no artifact
        ),
    ],
)
def test_resolve_image_ref(
    image_url, expected_ref, expected_artifact, expected_instantiation
):
    # Test parsing the image reference
    ref = ContainerImage.make(image_url)
    assert ref and ref.parts == expected_ref
    credentials = _registry_credentials(image_url)
    ref.username = credentials.get("username")
    ref.password = credentials.get("password")

    # Test fetching from registry
    annotations, platforms, manifest_digest, artifact_fetch = oci.registry_v2_fetch(
        ref.parts, artifact_fetch="application/vnd.in-toto+json", **credentials
    )
    # print(artifact_fetch.artifact_bytes if artifact_fetch else "no artifact fetch")
    assert platforms
    assert manifest_digest == expected_artifact.digest or expected_artifact.digest == ""

    # Test creating artifact
    artifact, instantiation, artifact_fetch = oci.create_oci_artifact(ref)
    assert artifact is not None, f"Failed to create artifact for {image_url}"
    assert artifact.metadata.discovery is not None, f"Expected discovery info for {image_url}"
    assert len(artifact.metadata.discovery.sources) > 0, (
        f"Expected at least one source URL for {image_url}"
    )

    # Compare instantiation with expected
    if expected_instantiation is not None:
        # Check that instantiation was created for VCS info
        assert instantiation is not None, (
            f"Expected instantiation for {image_url} with VCS info"
        )
        assert instantiation.type.types == expected_instantiation.type.types, (
            f"Instantiation type mismatch {instantiation.type.types} for {image_url}"
        )
        assert instantiation.source == expected_instantiation.source, (
            f"Instantiation source mismatch for {image_url}"
        )
        # Handle IGNORE for source_revision (revision may change)
        if expected_instantiation.source_revision != "IGNORE":
            assert (
                instantiation.source_revision == expected_instantiation.source_revision
            ), f"Instantiation source_revision mismatch for {image_url}"
        assert instantiation.url, f"Expected URL to be set for {image_url}"
    else:
        assert instantiation is None, (
            f"Unexpected instantiation for {image_url} without VCS info"
        )

    # Verify manifest URL is included (will have the manifest digest, not the tag)
    assert any("/manifests/" in url for url in artifact.metadata.discovery.sources), (
        f"No manifest URL found in discovery.sources for {image_url}"
    )

    # Handle variable fields
    if not expected_artifact.digest:
        # digest is unpredictable, just assert it's set
        assert artifact.digest, f"Expected a digest for {image_url}"
        expected_artifact = replace(expected_artifact, digest=artifact.digest)

    if expected_artifact.metadata.description == "IGNORE":
        expected_artifact = replace(
            expected_artifact,
            metadata=replace(
                expected_artifact.metadata, description=artifact.metadata.description
            ),
        )

    if expected_artifact.metadata.version == "IGNORE":
        expected_artifact = replace(
            expected_artifact,
            metadata=replace(
                expected_artifact.metadata, version=artifact.metadata.version
            ),
        )

    # Compare artifacts (excluding discovery which we already checked)
    assert artifact.url == expected_artifact.url, (
        f"Package URL mismatch for {image_url}"
    )
    assert artifact.type.types == expected_artifact.type.types, (
        f"unexpected artifact type for {image_url}: {artifact.type.types} != {expected_artifact.type.types}"
    )
    assert artifact.digest == expected_artifact.digest, (
        f"Digest mismatch for {image_url}"
    )
    # assert artifact.metadata == expected_artifact.metadata, f"Metadata mismatch for {image_url}"

    # Compare metadata source_url field
    if expected_artifact.metadata.source_url:
        assert artifact.metadata.source_url == expected_artifact.metadata.source_url, (
            f"Metadata source_url mismatch for {image_url}"
        )

    # Verify metadata.tags is populated correctly based on tag
    if not ref.parts.tag or ref.parts.tag == "latest":
        # Should have tags when tag is empty or "latest"
        # print(artifact.tags)
        assert isinstance(artifact.tags, list) and len(artifact.tags) > 0, (
            f"Expected tags to be non-empty for {image_url}"
        )
    else:
        # Should not have tags when there's an explicit tag (other than "latest")
        assert artifact.tags is None, (
            f"Expected tags to be empty for {image_url} with explicit tag {ref.parts.tag!r}"
        )


class FakeResponse:
    def __init__(
        self, status_code=200, json_data=None, headers=None, reason="OK", url=""
    ):
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {}
        self.reason = reason
        self.url = url

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data

    def raise_for_status(self):
        if not self.ok:
            raise Exception(f"{self.status_code} {self.reason} for {self.url}")


@pytest.fixture
def mock_requests_get(monkeypatch):
    """
    Monkeypatch requests.get with a router dict:
      routes[(url, auth_header_prefix, accept_prefix)] -> FakeResponse
    If no exact match, tries (url, None, None)
    """
    routes: dict[tuple[str, str | None, str | None], FakeResponse] = {}

    def _get(url, params=None, headers=None, timeout=None):
        headers = headers or {}
        auth = headers.get("Authorization")
        accept = headers.get("Accept")

        # include params in URL for routing simplicity
        if params:
            # stable-ish ordering
            parts = [f"{k}={params[k]}" for k in sorted(params.keys())]
            url_key = f"{url}?{'&'.join(parts)}"
        else:
            url_key = url

        # print("REQ", url_key, "AUTH", auth, "ACCEPT", accept)

        key_exact = (url_key, auth, accept)
        key_loose = (url_key, None, None)

        resp = routes.get(key_exact) or routes.get(key_loose)
        if not resp:
            return FakeResponse(
                status_code=404,
                json_data={"message": "not found"},
                url=url_key,
                reason="Not Found",
            )
        resp.url = url_key
        return resp

    monkeypatch.setattr(oci.requests, "get", _get)
    return routes


def test_registry_v2_fetch_single_manifest_labels(mock_requests_get):
    host = "registry-1.docker.io"
    repo = "library/nginx"

    manifest_url = f"https://{host}/v2/{repo}/manifests/latest"
    blob_url = f"https://{host}/v2/{repo}/blobs/sha256:cfg"

    mock_requests_get[(manifest_url, None, None)] = FakeResponse(
        200,
        json_data={
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": "sha256:cfg"},
        },
        headers={"Docker-Content-Digest": "sha256:man"},
    )
    mock_requests_get[(blob_url, None, None)] = FakeResponse(
        200,
        json_data={
            "config": {
                "Labels": {
                    "org.opencontainers.image.source": "https://github.com/example/repo"
                }
            }
        },
    )

    ann, platforms, manifest_digest, artifact_fetch = oci.registry_v2_fetch(
        ContainerImageParts.split("docker.io/library/nginx:latest")
    )
    assert ann is not None
    assert ann["org.opencontainers.image.source"] == "https://github.com/example/repo"
    assert manifest_digest == "sha256:man"


def test_registry_v2_bearer_challenge_flow(mock_requests_get):
    ref = ContainerImageParts.split("docker.io/library/nginx:latest")
    host = "registry-1.docker.io"
    repo = "library/nginx"

    manifest_url = f"https://{host}/v2/{repo}/manifests/latest"

    # 1) Initial request returns 401 Bearer challenge (no scope -> code falls back)
    challenge = FakeResponse(
        401,
        json_data={"errors": [{"code": "UNAUTHORIZED"}]},
        headers={
            "WWW-Authenticate": 'Bearer realm="https://auth.example/token",service="registry.example"'
        },
        reason="Unauthorized",
    )
    mock_requests_get[(manifest_url, None, oci.MANIFEST_ACCEPT)] = challenge

    # 2) Token request (fixture builds URL with params sorted)
    token_url = "https://auth.example/token?scope=repository:library/nginx:pull&service=registry.example"
    mock_requests_get[(token_url, None, None)] = FakeResponse(
        200, json_data={"token": "t0k3n"}
    )

    # 3) Retried request with Bearer token succeeds
    ok_manifest = FakeResponse(
        200,
        json_data={
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": "sha256:cfg"},
        },
        headers={"Docker-Content-Digest": "sha256:man"},
    )
    mock_requests_get[(manifest_url, "Bearer t0k3n", oci.MANIFEST_ACCEPT)] = ok_manifest

    # 4) Blob fetch: allow anonymous for this unit test (keeps it minimal)
    blob_url = f"https://{host}/v2/{repo}/blobs/sha256:cfg"
    ok_blob = FakeResponse(200, json_data={"config": {"Labels": {}}})
    mock_requests_get[(blob_url, None, oci.CONFIG_ACCEPT)] = ok_blob

    out = oci.registry_v2_fetch(ref)
    assert out[2] == "sha256:man"
