# Copyright (c) 2025 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Container Image Metadata Resolver

Retrieve OCI metadata from a v2 container image registry.
SBOM artifacts are also retrieved if referenced via annotations or the OCI referrers API

Also, query bespoke metadata APIs from:

* Docker Hub repo metadata (hub.docker.com) for docker.io
* LinuxServer API (lscr.io/linuxserver/*)
* Artifact Hub

Registry Auth Tips:

- GHCR: username=<github user>, password=<PAT with read:packages>
- public.ecr.aws: username="AWS", password=<token from `aws ecr-public get-login-password`>
- gcr.io and *.pkg.dev: username="oauth2accesstoken", password=<oauth access token>
"""

from __future__ import annotations
from functools import cache
from typing import (
    Any,
    Dict,
    NamedTuple,
    Optional,
    Tuple,
    List,
)
import base64
import json
import logging
import requests

from tenacity import (
    retry,
    retry_if_exception,
    retry_if_result,
    stop_after_attempt,
    wait_exponential_jitter,
    before_sleep_log,
)
from ..support import ContainerImageParts, ContainerImage
from ..logs import getLogger
from ..tosca_plugins.cloudmap_defs import (
    ArtifactMappings,
    ArtifactMetadata,
    Artifact,
    Discovery,
    EntitySchema,
    Instantiation,
    TypeRefs,
    TypedUrls,
    build_oci_purl,
)

logger = getLogger("unfurl")

DEFAULT_TIMEOUT = 20  # seconds


# ---------------------------
# Parsing image references
# ---------------------------


class ArtifactFetch(NamedTuple):
    manifest: Dict[str, Any]
    artifact: Dict[str, Any]
    manifest_url: str
    artifact_digest: str
    artifact_bytes: bytes


class ImageMetadataFetch(NamedTuple):
    annotations: Dict[str, Any]
    "merged annotations from the index, manifest, and labels in the config blob"
    platforms: List[Dict[str, str]]
    "list of platform configs from the index or application/vnd.oci.image.config.v1+json blob (which has the same platform keys)"
    manifest_digest: Optional[str]
    "the manifest digest of the selected architecture or the root manifest if single-arch"
    artifact_fetch: Optional[ArtifactFetch] = None


def create_oci_artifact(
    image: ContainerImage, fetch_tags: Optional[bool] = None
) -> Tuple[Artifact, Optional[Instantiation], Optional[ArtifactFetch]]:
    """Create OCI artifact from the given image name.

    Returns:
        Tuple of (Artifact object, Optional Instantiation for build provenance)
    """
    ref = image.parts
    source_urls: List[str] = []
    instantiation: Optional[Instantiation] = None

    purl = build_oci_purl(ref)
    metadata = ArtifactMetadata()
    artifact_type = (
        "application/vnd.in-toto+json"  # support in-toto attestation artifacts
    )
    annotations, platforms, manifest_digest, artifact_fetch = registry_v2_fetch(
        ref,
        username=image.username,
        password=image.password,
        artifact_fetch=artifact_type,
    )

    if manifest_digest:  # Track the manifest URL used
        manifest_url = (
            f"https://{ref.host}/v2/{ref.repository}/manifests/{manifest_digest}"
        )
        source_urls.append(manifest_url)

    # Extract metadata from labels/annotations
    metadata.extract_urls_from_labels(annotations)
    if platforms:
        # attestation-manifests have arch and os set to "unknown"
        metadata.platforms = [
            {"architecture": p.get("architecture", ""), "os": p.get("os", "")}
            for p in platforms
            if "architecture" in p and "os" in p and p["architecture"] != "unknown"
        ]

    # Fetch available tags if the tag is empty or "latest"
    tags: Optional[List[str]] = None
    if fetch_tags is not False and (not ref.tag or ref.tag == "latest"):
        tags = registry_v2_get_tags(
            ref,
            username=image.username,
            password=image.password,
        )

    # Set digest on the artifact
    digest = manifest_digest or ref.digest

    # Handle in-toto artifact metadata - extract VCS info for build instantiation
    if artifact_fetch:
        attestation_artifact = artifact_fetch.artifact
        if isinstance(attestation_artifact, dict):
            # print("attestation_artifact:", json.dumps(attestation_artifact, indent=2))
            inst_type = TypeRefs()
            # in-toto Statement:
            inst_type.add(ArtifactMappings.get(attestation_artifact.get("_type", "")))
            # SLSA provenance or SPDX document:
            inst_type.add(
                ArtifactMappings.get(attestation_artifact.get("predicateType", ""))
            )
            predicate = attestation_artifact.get("predicate")
            if isinstance(predicate, dict):
                # both slsa and spdx use this:
                inst_type.add(
                    ArtifactMappings.get(attestation_artifact.get("buildType", ""))
                )
                instantiation = Instantiation(
                    url=artifact_fetch.manifest_url,
                    type=inst_type,
                    digest=artifact_fetch.artifact_digest,
                    source=metadata.source_url,
                    source_revision=metadata.source_revision,
                    instantiated=TypeRefs.urls_fromdict({
                        purl: None
                    }),  # link instantiation to artifact
                )
                if "metadata" in predicate:  # SlsaProvenance 0.2
                    artifact_metadata = predicate["metadata"].get(
                        "https://mobyproject.org/buildkit@v1#metadata"
                    )
                    metadata.created = predicate["metadata"].get("buildFinishedOn")
                    # predicate["metadata"]["reproducible"] is 0.2 only
                elif "runDetails" in predicate:  # SlsaProvenance 1
                    run_metadata = predicate["runDetails"].get("metadata", {})
                    metadata.created = run_metadata.get("finishedOn")
                    artifact_metadata = run_metadata.get("buildkit_metadata")
                else:
                    artifact_metadata = None
                if isinstance(artifact_metadata, dict):
                    vcs_info = artifact_metadata.get("vcs")
                    if isinstance(vcs_info, dict):
                        # Add the artifact's manifest URL to sources
                        # Extract VCS info for build instantiation
                        source_revision = vcs_info.get("revision", "")
                        source_location = vcs_info.get("source")
                        if source_location or source_revision:
                            instantiation.source = source_location or ""
                            instantiation.source_revision = source_revision
                        if source_location and not metadata.source_url:
                            source_urls.append(artifact_fetch.manifest_url)
                            metadata.source_url = source_location

    if ref.host == "registry-1.docker.io":
        namespace = ref.namespace or "library"
        dockerhub_url = (
            f"https://hub.docker.com/v2/repositories/{namespace}/{ref.name}/"
        )
        raw_urls = dockerhub_repo_metadata(ref)
        if raw_urls:
            source_urls.append(dockerhub_url)
            source = raw_urls.get("repo_url") or raw_urls.get("source")
            if source:
                metadata.source_url = source
            if raw_urls.get("homepage"):
                metadata.homepage_url = raw_urls["homepage"]
            if raw_urls.get("documentation"):
                metadata.documentation_url = raw_urls["documentation"]
            if raw_urls.get("description"):
                metadata.description = raw_urls["description"]

    elif ref.registry == "lscr.io" and ref.namespace == "linuxserver":
        linuxserver_url = "https://api.linuxserver.io/api/v1/images"
        image_info = linuxserver_fetch(ref)
        if image_info:
            source_urls.append(linuxserver_url)
            for k, attr_name in {
                "github_url": "source",
                "project_url": "homepage_url",
                "description": "description",
                "version": "version",
                "category": "topics",
            }.items():
                v = image_info.get(k)
                if v:
                    if "topics" == attr_name:
                        if isinstance(v, str):
                            v = [t.strip() for t in v.split(",") if t.strip()]
                        else:
                            continue
                    setattr(metadata, attr_name, v)
    elif not metadata.homepage_url and ref.host in [
        "registry.gitlab.com",
        "registry.unfurl.cloud",
    ]:
        # set first 2 segments in path as homepage_url (will be a group or project page)
        metadata.homepage_url = f"https://{ref.host[len('registry.') :]}/{'/'.join(ref.full_name.split('/')[:2])}"

    # Create and return Artifact with instantiation
    instantiated_by = TypeRefs.urls_fromdict(
        {instantiation.url: None} if instantiation else {}
    )
    artifact_types = TypeRefs()
    artifact_types.add(EntitySchema.OCIImage)
    artifact = Artifact(
        url=purl,
        type=artifact_types,
        digest=digest,
        tags=tags,
        metadata=metadata,
        instantiated_by=instantiated_by,
        discovery=Discovery(sources=source_urls) if source_urls else None,
    )

    return artifact, instantiation, artifact_fetch


# ---------------------------
# HTTP helpers (with retries)
# ---------------------------


def _is_retryable_exception(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
        ),
    )


@retry(
    retry=retry_if_exception(_is_retryable_exception),
    stop=stop_after_attempt(4),
    wait=wait_exponential_jitter(initial=0.3, max=4.0),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _safe_get_json(
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Optional[Any]:
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


# ---------------------------
# Registry API v2 helpers (auth + bearer challenge + retries)
# ---------------------------

MANIFEST_ACCEPT = (
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.docker.distribution.manifest.v2+json,"
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.docker.distribution.manifest.list.v2+json"
)

CONFIG_ACCEPT = (
    "application/vnd.oci.image.config.v1+json,"
    "application/vnd.docker.container.image.v1+json,"
    "application/octet-stream"
)

REFERRERS_ACCEPT = "application/vnd.oci.image.index.v1+json,application/json"

ARTIFACT_MANIFEST_ACCEPT = (
    "application/vnd.oci.artifact.manifest.v1+json,"
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.docker.distribution.manifest.v2+json,"
    "application/json"
)

ATTESTATION_ARTIFACT_TYPE = "application/vnd.docker.attestation.manifest.v1+json"

SBOM_BLOB_ACCEPT = (
    "application/spdx+json,"
    "application/vnd.cyclonedx+json,"
    "application/vnd.in-toto+json,"
    "application/json,"
    "application/octet-stream"
)


def _basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _parse_www_authenticate(header: str) -> Tuple[str, Dict[str, str]]:
    if not header:
        return "", {}
    parts = header.split(" ", 1)
    scheme = parts[0].strip()
    params: Dict[str, str] = {}
    if len(parts) == 2:
        rest = parts[1]
        for chunk in rest.split(","):
            chunk = chunk.strip()
            if "=" not in chunk:
                continue
            k, v = chunk.split("=", 1)
            params[k.strip()] = v.strip().strip('"')
    return scheme, params


def _get_bearer_token(
    realm: str,
    service: Optional[str],
    scope: Optional[str],
    username: Optional[str],
    password: Optional[str],
    timeout: int = DEFAULT_TIMEOUT,
) -> Optional[str]:
    params = {}
    if service:
        params["service"] = service
    if scope:
        params["scope"] = scope

    headers: Dict[str, str] = {}
    if username and password:
        headers["Authorization"] = _basic_auth_header(username, password)

    try:
        r = requests.get(realm, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return data.get("token") or data.get("access_token")
    except Exception:
        return None


def _is_retryable_response(resp: Optional[requests.Response]) -> bool:
    if resp is None:
        return True
    if resp.status_code == 429:
        return True
    if 500 <= resp.status_code < 600:
        return True
    return False


@retry(
    retry=(
        retry_if_exception(_is_retryable_exception)
        | retry_if_result(_is_retryable_response)
    ),
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=0.5, max=8.0),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=False,
)
def _registry_get(
    url: str,
    accept: Optional[str],
    username: Optional[str],
    password: Optional[str],
    repository: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Optional[requests.Response]:
    headers: Dict[str, str] = {}
    if accept:
        headers["Accept"] = accept
    if username and password:
        headers["Authorization"] = _basic_auth_header(username, password)

    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except Exception:
        logger.error("Registry GET exception for %s", url, exc_info=True)
        raise  # maybe triggers retry

    # Not a Bearer auth challenge
    if r.status_code != 401:
        return r

    www = r.headers.get("WWW-Authenticate", "")
    scheme, params = _parse_www_authenticate(www)
    if scheme.lower() != "bearer":
        logger.info("Registry auth failed (non-bearer) for %s", url)
        return r

    realm = params.get("realm", "")
    service = params.get("service")
    scope = params.get("scope") or (
        f"repository:{repository}:pull" if repository else None
    )

    token = _get_bearer_token(
        realm, service, scope, username, password, timeout=timeout
    )
    if not token:
        logger.info("Registry bearer token acquisition failed for %s", url)
        return r

    headers2 = dict(headers)
    headers2["Authorization"] = f"Bearer {token}"
    r2 = requests.get(url, headers=headers2, timeout=timeout)
    return r2


def _choose_platform_manifest(
    manifests: List[Dict[str, Any]], platform: Optional[str]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[Any, str]]:
    """Choose a manifest descriptor from a multi-arch index/list, default to linux/amd64 or linux/arm64 if platform is not specified or isn't found."""
    platforms = [m["platform"] for m in manifests if "platform" in m]
    attestations = {
        m["annotations"].get("vnd.docker.reference.digest"): m["digest"]
        for m in manifests
        if m.get("annotations", {}).get("vnd.docker.reference.type")
        == "attestation-manifest"
    }
    if not manifests:
        return {}, platforms, attestations

    def matches(m: Dict[str, Any], os_: str, arch: str) -> bool:
        p = m.get("platform") or {}
        return p.get("os") == os_ and p.get("architecture") == arch

    if platform:
        want_os, want_arch = (platform.split("/", 1) + [""])[:2]
        for m in manifests:
            if matches(m, want_os, want_arch):
                return m, platforms, attestations

    for os_, arch in [("linux", "amd64"), ("linux", "arm64")]:
        for m in manifests:
            if matches(m, os_, arch):
                return m, platforms, attestations

    return manifests[0], platforms, attestations


# ---------------------------
# Registry v2
# ---------------------------


@cache
def registry_v2_get_tags(
    ref: ContainerImageParts,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Optional[List[str]]:
    tags_url = f"https://{ref.host}/v2/{ref.repository}/tags/list"
    rt = _registry_get(
        tags_url,
        accept="application/json",
        username=username,
        password=password,
        repository=ref.repository,
        timeout=timeout,
    )
    tags = None
    if rt and rt.ok:
        tags = (rt.json() or {}).get("tags")
        if not isinstance(tags, list):
            logger.info(
                "Failed to parse 'tags' from %s: got %s",
                tags_url,
                tags,
            )
            return None
    else:
        logger.info(
            "Failed to fetch %s (status: %s)",
            tags_url,
            rt.status_code if rt is not None else "no response",
        )
    return tags


@cache
def registry_v2_fetch(
    ref: ContainerImageParts,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    platform: Optional[str] = None,
    artifact_fetch: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> ImageMetadataFetch:
    """
    Returns annotations extracted and platform manifest annotations (if any).

    Supports:
      - application/vnd.oci.image.manifest.v1+json
      - application/vnd.oci.image.index.v1+json  (multi-arch)

    annotations are merged from the following sources:
    - "annotations" in the index if present
    - "annotations" in the image manifest
    - "config"/"labels" in the image manifest's config blob (application/vnd.oci.image.config.v1+json)

    See https://github.com/opencontainers/image-spec/blob/main/config.md#annotations

    Returns an ArtifactFetch named tuple
    """

    host = ref.host
    repository = ref.repository
    reference = ref.reference or "latest"

    annotations: Dict[str, str] = {}
    platforms: List[Dict[str, str]] = []
    fetched = ImageMetadataFetch(annotations, platforms, "")

    # Initial fetch: may be manifest OR index/list
    manifest_url = f"https://{host}/v2/{repository}/manifests/{reference}"
    r = _registry_get(
        manifest_url,
        accept=MANIFEST_ACCEPT,
        username=username,
        password=password,
        repository=repository,
        timeout=timeout,
    )
    if not r or not r.ok:
        logger.info(
            "Failed to fetch manifest for %s/%s:%s (status: %s)",
            host,
            repository,
            reference,
            r.status_code if r is not None else "no response",
        )
        return fetched

    # canonical digest of the returned manifest
    manifest_digest = r.headers.get("Docker-Content-Digest")
    fetched = fetched._replace(manifest_digest=manifest_digest)
    root = r.json()
    root_media_type = (root.get("mediaType") or "").lower()
    logger.debug("Fetched manifests for %s: %s", manifest_url, root)

    # If root is index/list, capture index-level metadata
    is_index = root_media_type.endswith(
        "image.index.v1+json"
    ) or root_media_type.endswith("manifest.list.v2+json")
    if is_index:
        if root.get("annotations"):
            annotations.update(root["annotations"])

        subject_descriptor = root.get("subject") or {}
        subject_digest = subject_descriptor.get("digest")
        if artifact_fetch and subject_digest:
            artifact = fetch_referrers_and_payloads(
                ref,
                subject_digest,
                artifact_fetch,
                username=username,
                password=password,
                timeout=timeout,
            )
            if artifact:
                fetched = fetched._replace(artifact_fetch=artifact)

        # Choose a platform manifest from the index
        desc, platforms, attestations = _choose_platform_manifest(
            root.get("manifests") or [], platform=platform
        )
        fetched = fetched._replace(platforms=platforms)
        if not desc or not desc.get("digest"):
            logger.info(
                "No suitable platform manifest found in index for %s/%s:%s (platform: %s)",
                host,
                repository,
                reference,
                platform or "default",
            )
            return fetched
        chosen_digest = desc["digest"]
        # Fetch the chosen platform manifest by digest
        r2 = _registry_get(
            f"https://{host}/v2/{repository}/manifests/{chosen_digest}",
            accept=MANIFEST_ACCEPT,
            username=username,
            password=password,
            repository=repository,
            timeout=timeout,
        )
        if not r2 or not r2.ok:
            logger.info(
                "Failed to fetch platform manifest %s for %s/%s:%s (status: %s)",
                chosen_digest,
                host,
                repository,
                reference,
                r2.status_code if r2 is not None else "no response",
            )
            return fetched
        manifest = r2.json()
        attestation_digest = attestations.get(chosen_digest)
        if artifact_fetch and attestation_digest:
            artifact = registry_v2_download_referrer_payload(
                ref,
                attestation_digest,
                artifact_fetch,
                username=username,
                password=password,
                timeout=timeout,
            )
            if artifact:
                fetched = fetched._replace(artifact_fetch=artifact)
    else:
        manifest = root

    # Capture manifest-level annotations too (some producers annotate manifests)
    if manifest.get("annotations"):
        annotations.update(manifest["annotations"])

    subject_descriptor = manifest.get("subject") or {}
    subject_digest = subject_descriptor.get("digest")
    if artifact_fetch and subject_digest:
        artifact = fetch_referrers_and_payloads(
            ref,
            subject_digest,
            artifact_fetch,
            username=username,
            password=password,
            timeout=timeout,
        )
        if artifact:
            fetched = fetched._replace(artifact_fetch=artifact)

    config_descriptor = manifest.get("config") or {}
    cfg_digest = config_descriptor.get("digest")
    if (
        not cfg_digest
        or config_descriptor.get("mediaType") == "application/vnd.oci.empty.v1+json"
    ):
        logger.debug(
            "No config digest or empty config for %s/%s:%s (mediaType: %s)",
            host,
            repository,
            reference,
            config_descriptor.get("mediaType", "none"),
        )
        return fetched

    if config_descriptor.get("data"):
        try:
            config = json.loads(base64.b64decode(config_descriptor["data"]))
            assert isinstance(config, dict)
        except Exception:
            logger.info(
                "Failed to parse inline config blob JSON for %s/%s:%s",
                host,
                repository,
                reference,
                exc_info=True,
            )
            return fetched

    else:
        rb = _registry_get(
            f"https://{host}/v2/{repository}/blobs/{cfg_digest}",
            accept=CONFIG_ACCEPT,
            username=username,
            password=password,
            repository=repository,
            timeout=timeout,
        )
        if not rb or not rb.ok:
            logger.info(
                "Failed to fetch config blob %s for %s/%s:%s (status: %s)",
                cfg_digest,
                host,
                repository,
                reference,
                rb.status_code if rb is not None else "no response",
            )
            return fetched

        try:
            config = rb.json()
        except Exception:
            logger.info(
                "Failed to parse config blob JSON for %s/%s:%s",
                host,
                repository,
                reference,
                exc_info=True,
            )
            return fetched

    labels = (config.get("config") or {}).get("Labels")
    if labels:
        annotations.update(labels)

    if not platforms:
        return fetched._replace(platforms=[config])
    return fetched


# ---------------------------
# Metadata APIs
# ---------------------------


@cache
def linuxserver_initial_fetch() -> Optional[Any]:
    return _safe_get_json(
        "https://api.linuxserver.io/api/v1/images",
        params={"include_config": "true", "include_deprecated": "false"},
    )


def linuxserver_fetch(ref: ContainerImageParts) -> Optional[Dict[str, Any]]:
    payload = linuxserver_initial_fetch()
    if not payload:
        logger.info("Failed to fetch LinuxServer API data")
        return None
    images = payload.get("data", {}).get("repositories", {}).get("linuxserver")
    if not isinstance(images, list):
        logger.info("LinuxServer API returned unexpected data structure")
        return None
    target = ref.name
    for img in images:
        if not isinstance(img, dict):
            continue
        if img.get("name") != target:
            continue
        return img
    logger.info("Image %s not found in LinuxServer repository list", target)
    return None


def dockerhub_repo_metadata(ref: ContainerImageParts) -> Optional[Dict[str, str]]:
    namespace = ref.namespace or "library"
    repo_json = _safe_get_json(
        f"https://hub.docker.com/v2/repositories/{namespace}/{ref.name}/"
    )
    if not (repo_json and isinstance(repo_json, dict)):
        logger.info(
            "Failed to fetch Docker Hub metadata for %s/%s",
            namespace,
            ref.name,
        )
        return None

    raw_urls: Dict[str, str] = {}
    for key in ("description", "repo_url", "homepage", "source", "documentation"):
        v = repo_json.get(key)
        if isinstance(v, str) and v.strip():
            raw_urls[key] = v.strip()

    return raw_urls


def artifacthub_metadata(ref: ContainerImageParts) -> Optional[Dict[str, Any]]:
    base = "https://artifacthub.io/api/v1"
    queries: List[str] = []
    if ref.registry == "docker.io":
        queries.append(ref.full_name)
        queries.append(ref.name)
    else:
        queries.append(f"{ref.registry}/{ref.name}")
        queries.append(ref.name)

    best = None
    for q in queries:
        # Artifact Hub search API kind=12 is for container images
        search = _safe_get_json(
            f"{base}/packages/search",
            params={"kind": 12, "ts_query_web": q, "limit": 20},
        )
        if not (search and isinstance(search, dict)):
            continue
        pkgs = search.get("packages")
        if not isinstance(pkgs, list):
            continue
        for p in pkgs:
            if isinstance(p, dict) and p.get("name") == ref.full_name:
                best = p
                break
        if best:
            break

    if not best:
        return None

    repo_obj = best.get("repository") or {}
    repo_name = repo_obj.get("name") if isinstance(repo_obj, dict) else None
    if not repo_name:
        return None

    pkg_name = (
        repo_name  # assume package name is same as repo name for container images
    )
    details = _safe_get_json(f"{base}/packages/container/{repo_name}/{pkg_name}")
    if not (details and isinstance(details, dict)):
        return None

    links_out: Dict[str, str] = {}
    links = details.get("links")
    if isinstance(links, list):
        for l in links:
            if isinstance(l, dict):
                n, u = l.get("name"), l.get("url")
                if (
                    isinstance(n, str)
                    and isinstance(u, str)
                    and n.strip()
                    and u.strip()
                ):
                    links_out[n.strip().lower()] = u.strip()

    urls = {
        "source": links_out.get("source") or links_out.get("repository"),
        "homepage": links_out.get("homepage") or links_out.get("home"),
        "documentation": links_out.get("documentation") or links_out.get("docs"),
    }
    for k, v in links_out.items():
        urls[f"link:{k}"] = v
    urls = {k: v for k, v in urls.items() if isinstance(v, str) and v.strip()}

    return urls


# ---------------------------
# SBOM artifact APIs
# ---------------------------


def registry_v2_referrers(
    ref: ContainerImageParts,
    subject_digest: str,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Optional[List[Dict[str, Any]]]:
    if not subject_digest or "sha256:" not in subject_digest:
        return None

    host = ref.host
    repository = ref.repository

    url = f"https://{host}/v2/{repository}/referrers/{subject_digest}"
    r = _registry_get(
        url,
        accept=REFERRERS_ACCEPT,
        username=username,
        password=password,
        repository=repository,
        timeout=timeout,
    )
    if not r or not r.ok:
        logger.info(
            "Failed to fetch %s (status: %s)",
            url,
            r.status_code if r is not None else "no response",
        )
        return None

    try:
        payload = r.json()
    except Exception:
        return None

    # result is an oci index
    manifests = payload.get("manifests") if isinstance(payload, dict) else None
    if not isinstance(manifests, list):
        return None
    return manifests


def registry_v2_download_referrer_payload(
    ref: ContainerImageParts,
    artifact_digest: str,
    media_type: str,
    *,
    predicate_type: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Optional[ArtifactFetch]:
    "Returns None or the artifact manifest and the artifact as JSON"
    if not artifact_digest or "sha256:" not in artifact_digest:
        return None

    host = ref.host
    repository = ref.repository
    manifest_url = f"https://{host}/v2/{repository}/manifests/{artifact_digest}"
    r = _registry_get(
        manifest_url,
        accept=ARTIFACT_MANIFEST_ACCEPT,
        username=username,
        password=password,
        repository=repository,
        timeout=timeout,
    )
    if not r or not r.ok:
        return None

    try:
        manifest = r.json()
    except Exception:
        return None
    assert isinstance(manifest, dict), manifest
    desc: Optional[Dict[str, Any]] = None
    layers = manifest.get("layers")
    if isinstance(layers, list) and layers:
        for layer in layers:
            if layer.get("mediaType") == media_type:
                desc = layer
                annotations = layer.get("annotations") or {}
                if predicate_type and (
                    annotations.get("in-toto.io/predicate-type") != predicate_type
                ):
                    continue
                break
    if not desc:
        return None

    payload_digest = desc.get("digest")
    if not isinstance(payload_digest, str):
        return None

    payload_bytes = None
    payload_json = None
    if desc.get("data"):
        try:
            payload_bytes = base64.b64decode(desc["data"])
            payload_json = json.loads(payload_bytes.decode("utf-8"))
            assert isinstance(payload_json, dict), payload_json
        except Exception:
            logger.info(
                "Failed to parse inline descriptor data json for %s",
                manifest_url,
                exc_info=True,
            )
            return None
    else:
        manifest_url = f"https://{host}/v2/{repository}/blobs/{payload_digest}"
        rb = _registry_get(
            manifest_url,
            accept=SBOM_BLOB_ACCEPT,
            username=username,
            password=password,
            repository=repository,
            timeout=timeout,
        )
        if not rb or not rb.ok:
            return None

        payload_bytes = rb.content
        try:
            payload_json = json.loads(payload_bytes.decode("utf-8"))
        except Exception:
            return None

    return ArtifactFetch(
        manifest, payload_json, manifest_url, payload_digest, payload_bytes
    )


def fetch_referrers_and_payloads(
    ref: ContainerImageParts,
    subject_digest: str,
    media_type: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Optional[ArtifactFetch]:
    referrers = registry_v2_referrers(
        ref,
        subject_digest,
        username=username,
        password=password,
        timeout=timeout,
    )
    if referrers:
        for item in referrers:
            if item.get("mediaType") != media_type:
                continue
            artifact_digest = item["digest"]
            return registry_v2_download_referrer_payload(
                ref,
                artifact_digest,
                media_type,
                username=username,
                password=password,
                timeout=timeout,
            )
    return None
