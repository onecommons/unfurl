import json
import pytest
from dataclasses import replace

from unfurl import oci
from unfurl.support import ContainerImageParts

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
    "image_url,expected_ref,expected_metadata,has_artifact_fetch",
    [
        (
            "docker.io/baserow/baserow",
            ContainerImageParts(
                full_name="baserow/baserow", tag="", digest="", registry="docker.io"
            ),
            oci.ArtifactMetadata(
                purl="pkg:oci/baserow?repository_url=registry-1.docker.io/baserow/baserow",
                source="https://github.com/baserow/baserow",
                description="All in one docker image for Baserow, open source no-code platform tool and Airtable alternative",
                title="baserow",
                repository_id="",
                digest="",  # Will be replaced in test
                platforms=[
                    {"architecture": "amd64", "os": "linux"},
                    {"architecture": "arm64", "os": "linux"},
                ],
                spdx_licenses="",
                vendor="",
                version="",
                revision="2790886f0d68669327793d46ee8989f92a9459c6",
                homepage_url="",
                documentation_url="",
            ),
            True,
        ),
        (
            "lscr.io/linuxserver/wireguard",
            ContainerImageParts(
                full_name="linuxserver/wireguard", tag="", digest="", registry="lscr.io"
            ),
            oci.ArtifactMetadata(
                purl="pkg:oci/wireguard?repository_url=lscr.io/linuxserver/wireguard",
                source="https://github.com/linuxserver/docker-wireguard",
                description="[WireGuard®] is an extremely simple yet fast and modern VPN that utilizes state-of-the-art cryptography. It aims to be faster, simpler, leaner, and more useful than IPsec, while avoiding the massive headache. It intends to be considerably more performant than OpenVPN. WireGuard is designed as a general purpose VPN for running on embedded interfaces and super computers alike, fit for many different circumstances. Initially released for the Linux kernel, it is now cross-platform (Windows, macOS, BSD, iOS, Android) and widely deployable. It is currently under heavy development, but already it might be regarded as the most secure, easiest to use, and simplest VPN solution in the industry.",
                title="Wireguard",
                repository_id="",
                digest="",  # Will be replaced in test
                platforms=[
                    {"architecture": "amd64", "os": "linux"},
                    {"architecture": "arm64", "os": "linux"},
                ],
                spdx_licenses="GPL-3.0-only",
                vendor="linuxserver.io",
                version="1.0.20250521-r1-ls97",
                revision="4951f20e686ae1109b3be25abfbf9b712aa53e81",
                homepage_url="https://www.wireguard.com/",
                documentation_url="https://docs.linuxserver.io/images/docker-wireguard",
            ),
            True,
        ),
        (
            "registry.gitlab.com/gitlab-org/project-templates/express/main",
            ContainerImageParts(
                full_name="gitlab-org/project-templates/express/main",
                tag="",
                digest="",
                registry="registry.gitlab.com",
            ),
            oci.ArtifactMetadata(
                purl="pkg:oci/main?repository_url=registry.gitlab.com/gitlab-org/project-templates/express/main",
                source="",
                description="",
                title="",
                repository_id="",
                digest="",  # Will be replaced in test
                platforms=[{"architecture": "amd64", "os": "linux"}],
                spdx_licenses="",
                vendor="",
                version="",
                revision="",
                homepage_url="https://gitlab.com/gitlab-org/project-templates",
                documentation_url="",
            ),
            False,
        ),
        (
            "ghcr.io/onecommons/unfurl:v1.1.0-server-cached",
            ContainerImageParts(
                full_name="onecommons/unfurl",
                tag="v1.1.0-server-cached",
                digest="",
                registry="ghcr.io",
            ),
            oci.ArtifactMetadata(
                purl="pkg:oci/unfurl?repository_url=ghcr.io/onecommons/unfurl&tag=v1.1.0-server-cached",
                source="",
                description="",
                title="",
                repository_id="",
                digest="sha256:4410d557ee799971770cf4fadc04e78fa5d2bd68470b6e6c6ebd49f32a59338d",
                platforms=[{"architecture": "amd64", "os": "linux"}],
                spdx_licenses="",
                vendor="",
                version="",
                revision="",
                homepage_url="",
                documentation_url="",
            ),
            True,
        ),
        (
            "ghcr.io/actions/actions-runner:latest",
            ContainerImageParts(
                full_name="actions/actions-runner",
                tag="latest",
                digest="",
                registry="ghcr.io",
            ),
            oci.ArtifactMetadata(
                purl="pkg:oci/actions-runner?repository_url=ghcr.io/actions/actions-runner&tag=latest",
                source="https://github.com/actions/runner",
                description="IGNORE",  # ignore because this will change: "https://github.com/actions/runner/releases/tag/v2.331.0",
                title="",
                repository_id="",
                digest="",
                platforms=[
                    {"architecture": "amd64", "os": "linux"},
                    {"architecture": "arm64", "os": "linux"},
                ],
                spdx_licenses="MIT",
                vendor="",
                version="24.04",
                revision="",
                homepage_url="",
                documentation_url="",
            ),
            True,
        ),
        (
            "registry.gitlab.com/gitlab-org/build/cng/gitlab-toolbox-ce:master",
            ContainerImageParts(
                full_name="gitlab-org/build/cng/gitlab-toolbox-ce",
                tag="master",
                digest="",
                registry="registry.gitlab.com",
            ),
            oci.ArtifactMetadata(
                purl="pkg:oci/gitlab-toolbox-ce?repository_url=registry.gitlab.com/gitlab-org/build/cng/gitlab-toolbox-ce&tag=master",
                source="",
                description="",
                title="",
                repository_id="",
                digest="",
                platforms=[
                    {"architecture": "amd64", "os": "linux"},
                    {"architecture": "arm64", "os": "linux"},
                ],
                spdx_licenses="",
                vendor="",
                version="",
                revision="",
                homepage_url="https://gitlab.com/gitlab-org/build",
                documentation_url="",
            ),
            False,
        ),
        (
            "registry.unfurl.cloud/onecommons/unfurl-gui@sha256:c21af1741b31f33ccd44f096003dfcd576adda854415fffa21290796a0689d32",
            ContainerImageParts(
                full_name="onecommons/unfurl-gui",
                tag="",
                digest="sha256:c21af1741b31f33ccd44f096003dfcd576adda854415fffa21290796a0689d32",
                registry="registry.unfurl.cloud",
            ),
            oci.ArtifactMetadata(
                purl="pkg:oci/unfurl-gui@sha256%3Ac21af1741b31f33ccd44f096003dfcd576adda854415fffa21290796a0689d32?repository_url=registry.unfurl.cloud/onecommons/unfurl-gui",
                source="",
                description="",
                title="",
                repository_id="",
                digest="sha256:c21af1741b31f33ccd44f096003dfcd576adda854415fffa21290796a0689d32",
                platforms=[{"architecture": "amd64", "os": "linux"}],
                spdx_licenses="",
                vendor="",
                version="",
                revision="",
                homepage_url="https://unfurl.cloud/onecommons/unfurl-gui",
                documentation_url="",
            ),
            False,
        ),
    ],
)
def test_parse_image_ref(
    image_url, expected_ref, expected_metadata, has_artifact_fetch
):
    # Test parsing the image reference
    ref = oci.ContainerImageParts.split(image_url)
    assert ref == expected_ref

    # Test fetching from registry
    annotations, platforms, manifest_digest, artifact_fetch = oci.registry_v2_fetch(ref)
    assert platforms
    assert manifest_digest == expected_metadata.digest or expected_metadata.digest == ""

    # Assert artifact_fetch presence based on expected value
    if has_artifact_fetch:
        assert artifact_fetch is not None, (
            f"artifact_fetch should be returned for {image_url}"
        )
    else:
        assert artifact_fetch is None, f"artifact_fetch should be None for {image_url}"

    # Test creating artifact metadata
    artifact_metadata = oci.create_oci_artifact(image_url)
    assert artifact_metadata is not None, (
        f"Failed to create artifact metadata for {image_url}"
    )

    if not expected_metadata.digest:
        # digest is unpredictable, just assert it's set
        assert artifact_metadata.digest is not None, (
            f"Expected a digest for {image_url}"
        )
        expected_metadata = replace(expected_metadata, digest=artifact_metadata.digest)
    if expected_metadata.description == "IGNORE":
        expected_metadata = replace(
            expected_metadata, description=artifact_metadata.description
        )
    assert artifact_metadata == expected_metadata, f"Metadata mismatch for {image_url}"


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
    # ref = oci.parse_image_ref("docker.io/library/nginx:latest")
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
        oci.ContainerImageParts.split("docker.io/library/nginx:latest")
    )
    assert ann is not None
    assert ann["org.opencontainers.image.source"] == "https://github.com/example/repo"
    assert manifest_digest == "sha256:man"


def test_registry_v2_bearer_challenge_flow(mock_requests_get):
    ref = oci.ContainerImageParts.split("docker.io/library/nginx:latest")
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
