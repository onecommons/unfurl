# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, cast
from typing_extensions import Literal, Self
from toscaparser.nodetemplate import NodeTemplate
from toscaparser.elements.statefulentitytype import StatefulEntityType

from . import (
    CloudMapDB,
    CloudType,
    RepositoryAnalyzer,
    AnalyzerContext,
    Repository,
    Service,
    get_repository_url,
)
from ..graphql import (
    ResourceType,
    ResourceTypesByName,
    get_deployment_url,
    TypeName,
    Deployment,
    _add_lastjob,
)
from ..spec import NodeSpec, Ref, SafeRefContext, TopologySpec, ToscaSpec, is_function
from ..support import ContainerImage, Status
from ..tosca_plugins.cloudmap_defs import (
    Artifact,
    ArtifactMetadata,
    TypeRefStatus,
    TypeRefConstraint,
    URLAnalyzer,
    CommonMetadata,
    EntitySchema,
    Instantiation,
    TypeRefs,
    TypedUrls,
    filter_dict,
    build_oci_purl,
)
from ..to_json import get_blueprint_path, node_type_to_graphql
from ..util import UnfurlError, assert_not_none
from ..localenv import LocalEnv
from ..logs import getLogger
from .. import DefaultNames
from ..repo import RepoView

if TYPE_CHECKING:
    from ..yamlmanifest import YamlManifest

logger = getLogger("unfurl")

_DEPLOY_STATUS_MAP: Dict[Status, TypeRefStatus] = {
    Status.unknown: "unknown",
    Status.ok: "present",
    Status.degraded: "present",
    Status.error: "failed",
    Status.pending: "absent",
    Status.absent: "absent",
}


class ContainerBuilderAnalyzer(RepositoryAnalyzer):
    files = ("Containerfile", "Dockerfile")
    artifact_type = EntitySchema.ContainerFile

    def __init__(
        self,
        folder: str,
        file: str,
        digest: str = "",
    ) -> None:
        super().__init__(folder, file, digest)


# XXX use this
def find_images_k8s(resources: List[Dict[str, Any]]) -> List[str]:
    images: List[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            image = obj.get("image")
            if isinstance(image, str):
                images.append(image)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for resource in resources:
        walk(resource)

    return images


class UnfurlAnalyzer(RepositoryAnalyzer):
    files = [
        DefaultNames.LocalConfig,
        DefaultNames.EnsembleTemplate,
        DefaultNames.Ensemble,
        "dummy-ensemble.yaml",  # DefaultNames.ServiceTemplate,  # XXX fix unfurl-types hack
    ]
    folders = [
        DefaultNames.ProjectDirectory,
        DefaultNames.HiddenProjectDirectory,
        DefaultNames.EnsembleDirectory,
    ]

    def __init__(
        self,
        folder: str,
        file: str,
        digest: str = "",
    ) -> None:
        super().__init__(folder, file, digest)
        # XXX set readonly=True after adding representers for AnsibleUnicode etc.

    def analyze(
        self, directory: AnalyzerContext, repo_info: Repository, root_path: str
    ) -> Optional[Artifact]:
        logger = directory.logger
        path = os.path.join(root_path, self.folder, self.file)
        logger.verbose("analyzing %s", path)
        localenv = LocalEnv(
            path,
            can_be_empty=True,
            parent=directory._local__env,
        )
        artifact: Optional[Artifact] = None
        analyze: Literal["yes", "no"] = "yes" if directory.do_analysis else "no"
        if localenv.manifestPath:
            self.artifact_type = self._get_artifacttype(localenv.manifestPath)
            if self.artifact_type != EntitySchema.Ensemble:
                localenv.overrides["format"] = "blueprint"
            manifest = localenv.get_manifest(skip_validation=True, safe_mode=True)
            rel_path = str(Path(localenv.manifestPath).relative_to(Path(root_path)))
            self.folder, self.file = os.path.split(rel_path)
            spec = assert_not_none(manifest.tosca)
            if self.artifact_type == EntitySchema.CloudBlueprint:
                self.fragment = spec.fragment
            metadata = cast(dict, spec.template.tpl).get("metadata") or {}

            # Extract template metadata
            template_name = metadata.get("template_name", "")
            template_version = metadata.get("template_version", "")
            template_description = spec.template.description or ""

            # Prepare type_info and dependencies
            type_info = None
            typename = ""
            dependencies: dict[str, TypeName] = {}
            references: TypedUrls = {}
            for name, repo_view in manifest.repositories.items():
                if name not in ("spec", "self", "project", "unfurl"):
                    if repo_view.url.startswith("git-local://") or os.path.isabs(
                        repo_view.url
                    ):
                        continue
                    if repo_view.package is None:
                        if manifest.tosca and manifest.tosca.import_resolver:
                            manifest.tosca.import_resolver._resolve_repoview(repo_view)
                    giturl = self._add_repository_reference(repo_view, references)
                    directory.analyze_url(giturl, analyze)

            node = self._get_root_node(spec)
            if node:
                types = ResourceTypesByName(
                    repo_info.package_id, spec.template.topology_template.custom_defs
                )
                type_info = self.get_type_info(
                    node.topology,
                    types,
                    assert_not_none(node.toscaEntityTemplate.type_definition),
                )
                typename = type_info.get("name", "")
                dependencies = self.find_dependencies(node, types)
                deployment_blueprints = manifest.get_deployment_blueprints()
                dependencies.update(
                    {
                        name: tpl["cloud"]
                        for name, tpl in deployment_blueprints.items()
                        if tpl.get("cloud")
                    }
                )
                # Handle container image dependency
                image = self.find_image_dependency(node)
                if image:
                    # XXX directory.add_credentials(image)
                    image_artifact = directory.add_image_artifact(image)
                    purl = image_artifact.url
                    references[("", purl)] = None

            # Include any URL fragment (e.g. "#spec/service_template") so the
            # artifact URL distinguishes sub-elements of the same file.
            artifact_url = repo_info.artifact_url(self.path)

            # Create main artifact using helper method
            artifact, cloud_type = create_artifact_from_notable(
                artifact_pkg=artifact_url,
                artifact_type=self.artifact_type,
                name=template_name,
                version=template_version,
                description=template_description,
                thumbnail=repo_info.metadata.thumbnail_url,
                references=references,
                dependencies=TypeRefs.urls_fromdict(
                    {name: TypeRefs({v: None}) for name, v in dependencies.items()}
                ),
                type_info=type_info,
                ctx=directory,
                digest=self.digest,
                instantiates_key=node.name if node else "",
            )

            # Add CloudType if created
            if cloud_type:
                directory.add_record(cloud_type)

            # Create CloudTypes for dependencies
            if node:
                for dep_typename in dependencies.values():
                    # Check if type already exists
                    if directory.get_type(dep_typename) is None:
                        # Get type information for dependency type
                        type_def = node.topology.find_type(dep_typename)
                        if type_def:
                            dep_type_info = self.get_type_info(
                                node.topology, types, type_def
                            )

                            dep_cloud_type = create_cloud_type_from_type_info(
                                dep_type_info, directory
                            )
                            if dep_cloud_type:
                                directory.add_record(dep_cloud_type)

            # Create Instantiation and Service for Ensemble artifacts
            if self.artifact_type == EntitySchema.Ensemble and typename:
                self._create_ensemble_instantiation_and_service(
                    manifest, repo_info, directory, typename, artifact
                )
        else:
            self.artifact_type = EntitySchema.UnfurlProject
        return artifact

    def _add_repository_reference(
        self,
        repo_view: RepoView,
        references: TypedUrls,
    ) -> str:
        giturl = get_repository_url(repo_view.url)
        type_ref = None
        if repo_view.package:
            #  treat as a artifact
            giturl += f"#{repo_view.revision_tag}:."
            type_ref = TypeRefs().add(
                EntitySchema.Package, version=repo_view.package.revision or ""
            )
        elif repo_view.path:
            giturl += f"#{repo_view.revision}:{repo_view.path}"
        else:
            if repo_view.revision:
                giturl += f"#{repo_view.revision}"
        references[("", giturl)] = type_ref
        return giturl

    def get_type_info(
        self,
        topology: TopologySpec,
        types: ResourceTypesByName,
        entity_type: StatefulEntityType,
    ) -> ResourceType:
        dep_type_info = node_type_to_graphql(
            topology,
            entity_type,
            types,
            True,
        )

        # Fallback to minimal CloudType if type not found in custom_defs
        if not dep_type_info:
            dep_type_info = ResourceType(
                __typename="ResourceType",
                name=entity_type.global_name,
                title=entity_type.local_name,
                extends=[],
                inputsSchema={},
                requirements=[],
            )

        return dep_type_info

    @classmethod
    def init(
        cls,
        folder: str,
        file: str,
        digest: str = "",
    ) -> Optional[UnfurlAnalyzer]:
        try:
            return UnfurlAnalyzer(folder, file, digest)
        except UnfurlError:
            logger.info("analysis failed for %s", file, exc_info=True)
            return None

    def find_dependencies(
        self, node: NodeSpec, types: ResourceTypesByName
    ) -> Dict[str, TypeName]:
        return {
            name: types.expand_typename(req.get("node"))
            for name, req in cast(
                NodeTemplate, node.toscaEntityTemplate
            ).missing_requirements.items()
        }

    def find_image_dependency(self, node: NodeSpec) -> Optional[ContainerImage]:
        # hacky, only works with ContainerServices
        container_service = node.get_relationship("container")
        if container_service and container_service.target:
            image_name = container_service.target.properties.get("container", {}).get(
                "image"
            )
            if image_name:
                if is_function(image_name):
                    # treat default like a constraint
                    # evaluate expression as a template expression and if it resolves to
                    image_name = Ref(image_name).resolve(SafeRefContext(node))
                if isinstance(image_name, str):
                    # remove any {{ }} templating
                    image = ContainerImage.make(re.sub(r"\{\{.*\}\}", "", image_name))
                    if image:
                        return image
        return None

    def _create_ensemble_instantiation_and_service(
        self,
        manifest: YamlManifest,
        repo_info: Repository,
        directory: AnalyzerContext,
        typename: str,
        artifact: Artifact,
    ) -> None:
        """
        Create an Instantiation and Service for Ensemble artifacts.

        Args:
            manifest: The YamlManifest object for the ensemble
            repo_info: Repository information
            directory: CloudMapView to add instantiation and service to
            artifact: The ensemble artifact
        """

        # Get spec repository for source information
        spec_repo_view = manifest.repositories.get("spec")
        analyze: Literal["yes", "no"] = "yes" if directory.do_analysis else "no"

        # XXX add inputs from lock section
        instantiation = Instantiation(
            url=artifact.url,
            revision=repo_info.get_current_commit(),
            inputs=artifact.references,
        )

        self._apply_lastjob(manifest, instantiation)

        if spec_repo_view:
            blueprint_path = get_blueprint_path(manifest)
            # Include the spec template's fragment (e.g. ``spec/service_template``)
            # so the source URL identifies the sub-element within the blueprint file
            fragment = manifest.tosca.fragment if manifest.tosca else ""
            if fragment:
                blueprint_path = blueprint_path + "#" + fragment
            instantiation.source = (
                get_repository_url(spec_repo_view.url)
                + f"#:{quote(blueprint_path)}"
            )
            instantiation.source_revision = spec_repo_view.get_current_commit()

        # Get deployment URL from manifest
        deployment_url = get_deployment_url(manifest, None)
        if not deployment_url:
            deployment_url = manifest.uri

        # Create Service if we have a deployment URL
        # XXX add connections
        if deployment_url:
            service = Service(
                url=deployment_url,
                type=TypeRefs(types={typename: None}),
                instantiated_by={("", instantiation.url): None},
            )
            directory.add_record(service)
            instantiation.instantiated = {("", deployment_url): None}

        directory.add_record(instantiation)
        if instantiation.source and not directory.get_artifact(instantiation.source):
            directory.analyze_url(instantiation.source, analyze)

    def _apply_lastjob(
        self,
        manifest: YamlManifest,
        instantiation: Instantiation,
    ) -> None:
        """Set the instantiation's status/created/description from the
        ensemble's last job, reusing the GraphQL export's ``_add_lastjob``."""
        if not manifest.lastJob:
            instantiation.type = TypeRefs(types={EntitySchema.Ensemble: None})
            return
        deployment = cast(Deployment, {})
        _add_lastjob(manifest.lastJob, deployment)
        deploy_status = None
        op_status = deployment.get("status")
        if op_status is not None:
            deploy_status = _DEPLOY_STATUS_MAP.get(op_status)
        instantiation.type = TypeRefs(
            types={EntitySchema.Ensemble: TypeRefConstraint(status=deploy_status)}
        )

        # Record when the deployment last ran and a summary of it.
        deploy_time = deployment.get("deployTime", "")
        if deploy_time:
            instantiation.metadata.created = deploy_time
        summary = deployment.get("summary", "")
        workflow = deployment.get("workflow", "")
        if summary or workflow:
            instantiation.metadata.description = (
                f"{workflow}: {summary}" if workflow else summary
            )

    def _get_artifacttype(self, path: str) -> str:
        if path.endswith(DefaultNames.EnsembleTemplate):
            return EntitySchema.CloudBlueprint
        elif path.endswith("dummy-ensemble.yaml"):
            return EntitySchema.TOSCASchema
        else:
            return EntitySchema.Ensemble

    def _get_root_node(self, spec: ToscaSpec) -> Optional[NodeSpec]:
        topology = spec.template.topology_template
        assert topology
        node = topology.substitution_mappings and topology.substitution_mappings.node
        if node:
            assert spec.topology
            return spec.topology.get_node_template(node)
        return None


def create_cloud_type_from_type_info(
    type_info: ResourceType, ctx: Optional[AnalyzerContext] = None
) -> Optional[CloudType]:
    """
    Create a CloudType from type_info dict if it doesn't already exist.

    Args:
        type_info: Dict with 'name', 'title', 'extends' keys
        ctx: Optional CloudMapView used to check for an existing type

    Returns:
        CloudType if created, None if type_info is empty or type already exists
    """
    type_name = type_info.get("name", "")
    if not type_name:
        return None

    # Don't create if it already exists
    if ctx is not None and ctx.get_type(type_name) is not None:
        return None

    metadata = CommonMetadata()
    if type_info.get("title"):
        metadata.title = type_info["title"]

    # XXX include properties schema
    # properties_schema = type_info.get("computedPropertiesSchema", {})
    # properties_schema.update(type_info.get("inputsSchema", {}))
    return CloudType(
        name=type_name,
        kind="Component",  # XXX inferred from artifact_type
        metadata=metadata,
        # properties=properties_schema,
        extends=cast(List[str], type_info.get("extends", [])),
    )


def create_artifact_from_notable(
    artifact_pkg: str,
    artifact_type: str,
    name: str = "",
    version: str = "",
    description: str = "",
    thumbnail: str = "",
    contains: Optional[TypedUrls] = None,
    references: Optional[TypedUrls] = None,
    dependencies: Optional[TypedUrls] = None,
    type_info: Optional[ResourceType] = None,
    ctx: Optional[AnalyzerContext] = None,
    digest: str = "",
    instantiates_key: str = "",
) -> Tuple[Artifact, Optional[CloudType]]:
    """
    Create an Artifact from notable metadata fields.

    Args:
        artifact_pkg: Package URL for the artifact
        artifact_type: Type identifier for the artifact
        name: Human-readable name (maps to metadata.title)
        version: Version string (maps to metadata.version)
        description: Description (maps to metadata.description)
        thumbnail: Icon or thumbnail URL (maps to metadata.thumbnail)
        contains: Map of artifact URLs this artifact embeds or incorporates
        references: Map of artifact IDs this artifact references (maps to references)
        dependencies: List of dependencies (maps to requires)
        type_info: Type definition dict with 'name', 'title', 'extends' (creates CloudType)
        ctx: Optional CloudMapView used to check for existing types
        digest: Digest of the artifact (e.g., "git:blob:abc123")

    Returns:
        Tuple of (Artifact, Optional[CloudType]) - CloudType is returned if created
    """
    # Build artifact metadata
    metadata = ArtifactMetadata(
        title=name,
        version=version,
        description=description,
        thumbnail_url=thumbnail,
    )

    # Handle type field: create CloudType and add to instantiates.
    # Keyed by a caller-supplied URL/label; normalized when passed to Artifact.
    instantiates: Dict[str, Optional[TypeRefs]] = {}
    cloud_type = None
    if type_info and isinstance(type_info, dict):
        cloud_type = create_cloud_type_from_type_info(type_info, ctx)
        type_name = type_info.get("name", "")
        if type_name:
            # Add to artifact's instantiates under the caller-supplied URL/label key
            instantiates[instantiates_key] = TypeRefs().add(type_name)

    # Create the artifact
    artifact = Artifact(
        url=artifact_pkg,
        type=TypeRefs().add(artifact_type, version=version),
        contains=contains or {},
        references=references or {},
        instantiates=TypeRefs.urls_fromdict(instantiates),
        dependencies=dependencies or {},
        metadata=metadata,
        digest=digest,
    )

    return artifact, cloud_type


def migrate_old_notable_format(
    db: CloudMapDB, repo: Repository, old_notable: Dict[str, dict]
) -> List[str]:
    """
    Migrate old Repository.notable dictionary format to the new
    ``Repository.contains`` TypedUrls format.

    Creates Artifact instances from old inline notable definitions and adds them
    to db.artifacts and db.types.

    Args:
        db: CloudMapDB instance to add artifacts and types to
        repo: Repository whose ``contains`` should be populated
        old_notable: Old ``notable`` dict mapping file paths to either an
            inline-artifact definition (with ``artifact_type``) or a
            ``{type, artifact}`` entry.

    Returns:
        Artifact URLs that were migrated.
    """
    migrated_artifact_ids: List[str] = []
    for file_path, notable_dict in old_notable.items():
        if "artifact_type" in notable_dict:
            # Create artifact pkg from repository URL + file path
            artifact_pkg = repo.artifact_url(file_path)

            # Create artifact using helper method
            type_info = notable_dict.pop("type", None)
            notable_name = notable_dict.pop("name", "")
            artifact, cloud_type = create_artifact_from_notable(
                artifact_pkg=artifact_pkg,
                artifact_type=notable_dict.pop("artifact_type", ""),
                name=notable_name,
                version=str(notable_dict.pop("version", "") or ""),
                description=notable_dict.pop("description", ""),
                thumbnail=notable_dict.pop("thumbnail_url", "")
                or repo.metadata.thumbnail_url,
                contains={
                    ("", build_oci_purl(ContainerImage.split(ref))): None
                    for ref in notable_dict.pop("artifacts", [])
                },
                dependencies=TypeRefs.urls_fromdict(
                    {
                        "": TypeRefs(
                            {v: None for v in notable_dict.pop("dependencies", [])}
                        )
                    }
                ),
                type_info=type_info,
                ctx=None,
                instantiates_key=notable_name,
            )

            # Add CloudType if created
            if cloud_type:
                db.add_record(cloud_type)

            # Add to artifacts dict
            db.artifacts[artifact_pkg] = artifact
            repo.contains[("", file_path)] = artifact.type
            migrated_artifact_ids.append(artifact_pkg)
        else:
            # `{type, artifact}` entry — key by file path, value is the type
            type_dict = notable_dict.get("type")
            repo.contains[("", file_path)] = (
                TypeRefs(types=type_dict) if type_dict else None
            )

    return migrated_artifact_ids


class GitLabPipelineAnalyzer(RepositoryAnalyzer):
    files = (".gitlab-ci.yml",)
    artifact_type = EntitySchema.GitLabPipeline


class GitHubWorkflowAnalyzer(RepositoryAnalyzer):
    folders = (".github",)
    artifact_type = EntitySchema.GitHubWorkflow

    def analyze(
        self, directory: AnalyzerContext, repo_info: Repository, root_path: str
    ) -> Optional[Artifact]:
        # Emit a separate artifact and `contains` entry for each workflow file
        # in .github/workflows.
        workflows_rel = os.path.join(self.folder, "workflows")
        if workflows_rel.startswith("./"):
            workflows_rel = workflows_rel[2:]
        workflows_dir = os.path.join(root_path, workflows_rel)
        # populated even when empty so add_notables records no spurious
        # directory-level entry (self.contains overrides the default self.path).
        self.contains = {}
        if not os.path.isdir(workflows_dir):
            return None
        for entry in sorted(os.listdir(workflows_dir)):
            if not entry.endswith((".yml", ".yaml")):
                continue
            if not os.path.isfile(os.path.join(workflows_dir, entry)):
                continue
            rel_path = os.path.join(workflows_rel, entry)
            type_refs = TypeRefs({self.artifact_type: None})
            directory.add_record(
                Artifact(
                    url=repo_info.artifact_url(rel_path),
                    type=TypeRefs({self.artifact_type: None}),
                )
            )
            self.contains[rel_path] = type_refs
        # artifacts were added above via directory.add_record
        return None


Analyzers = (
    UnfurlAnalyzer,
    ContainerBuilderAnalyzer,
    GitLabPipelineAnalyzer,
    GitHubWorkflowAnalyzer,
)


# ---------------------------------------------------------------------------
# URL-based analyzers
# ---------------------------------------------------------------------------


class OCIArtifactAnalyzer(URLAnalyzer):
    """Handle ``pkg:oci`` and ``pkg:docker`` URLs.

    Parses the PURL into a :class:`ContainerImage` and delegates to the
    existing :func:`unfurl.oci.create_oci_artifact` to construct the artifact
    and (optionally) an :class:`Instantiation` describing how the image was
    built. The instantiation is written to the cloudmap as a side effect.
    """

    url_schemes = ("pkg:oci", "pkg:docker")
    artifact_type = EntitySchema.OCIImage

    def __init__(self, url: str, image: ContainerImage):
        self.url = url
        self.image = image

    @classmethod
    def init_from_url(cls, url, parsed) -> Optional[Self]:
        # path is e.g. "oci/nginx@sha256:..." or "docker/library/nginx@latest"
        pkg_type, _, name_and_version = parsed.path.partition("/")
        if pkg_type not in ("oci", "docker"):
            return None
        name_part, _, version = name_and_version.partition("@")
        version = unquote(version)  # sha256%3A... → sha256:...
        query = parse_qs(parsed.query)
        is_digest = version.startswith("sha256:")
        digest = version if is_digest else None
        tag: Optional[str]
        registry_host: str
        image_name: str

        if pkg_type == "oci":
            # pkg:oci/<name>@<digest>?repository_url=<registry>/<repo>&tag=<tag>
            repository_url = query.get("repository_url", [""])[0]
            tag = query.get("tag", [None])[0]
            if not is_digest and version:
                tag = tag or version
            if repository_url:
                repo_parts = urlparse("https://" + repository_url)
                registry_host = repo_parts.hostname or ""
                image_name = repo_parts.path.lstrip("/")
            else:
                registry_host = ""
                image_name = name_part
        else:
            # pkg:docker/<namespace>/<name>@<version>?repository_url=<registry>
            registry_host = query.get("repository_url", [""])[0]
            tag = None if is_digest else (version or None)
            image_name = name_part

        image = ContainerImage(
            name=image_name,
            tag=tag,
            digest=digest,
            registry_host=registry_host or None,
        )
        return cls(url, image)

    def analyze_url(self, directory: AnalyzerContext) -> Optional[Artifact]:
        # Local import avoids a top-level circular dep with unfurl.oci.
        from .oci import create_oci_artifact

        artifact, instantiation, _artifact_fetch = create_oci_artifact(self.image)
        if instantiation:
            directory.add_record(instantiation)
        return artifact


class GenericPkgArtifactAnalyzer(URLAnalyzer):
    """Fallback handler for any ``pkg:`` URL not matched by a more specific analyzer."""

    url_schemes = ("pkg:",)
    artifact_type = EntitySchema.GenericPackage

    def __init__(self, artifact: Artifact):
        self.artifact = artifact

    @classmethod
    def init_from_url(cls, url, parsed) -> Optional[Self]:
        # pkg:<type>/<namespace>/<name>@<version>?<qualifiers>#<subpath>
        _pkg_type, _, name_and_version = parsed.path.partition("/")
        name_part, _, version = name_and_version.partition("@")
        version = unquote(version)
        # name_part may include namespace: "namespace/name"
        name = name_part.rpartition("/")[2]
        metadata_kwargs = {}
        if name:
            metadata_kwargs["title"] = name
        if version:
            metadata_kwargs["version"] = version
        artifact = Artifact(
            url=url,
            type=TypeRefs({cls.artifact_type: None}),
            metadata=ArtifactMetadata(**metadata_kwargs),
        )
        return cls(artifact)

    def analyze_url(self, directory: AnalyzerContext) -> Optional[Artifact]:
        return self.artifact


URLAnalyzers = (
    OCIArtifactAnalyzer,
    GenericPkgArtifactAnalyzer,
)
