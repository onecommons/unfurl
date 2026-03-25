# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, cast
from typing_extensions import Literal
from toscaparser.nodetemplate import NodeTemplate
from toscaparser.elements.statefulentitytype import StatefulEntityType

from .cloudmap import (
    CloudMapDB,
    CloudType,
    CloudTypeDict,
    Notable,
    Repository,
    Service,
    Directory,
    get_repository_url,
)
from .graphql import ResourceTypesByName, get_deployment_url, TypeName
from .spec import NodeSpec, Ref, SafeRefContext, TopologySpec, ToscaSpec, is_function
from .support import ContainerImage
from .oci import (
    Artifact,
    ArtifactMetadata,
    CommonMetadata,
    EntitySchema,
    Instantiation,
    TypeRefs,
    TypedUrls,
    TypeRefConstraint,
    build_oci_purl,
    filter_dict,
)
from .to_json import get_blueprint_path, node_type_to_graphql
from .util import UnfurlError, assert_not_none
from .localenv import LocalEnv
from .logs import getLogger
from . import DefaultNames
from .repo import RepoView

if TYPE_CHECKING:
    from .yamlmanifest import YamlManifest

logger = getLogger("unfurl")


class ContainerBuilderNotable(Notable):
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


class UnfurlNotable(Notable):
    files = [
        DefaultNames.LocalConfig,
        DefaultNames.EnsembleTemplate,
        DefaultNames.Ensemble,
        "dummy-ensemble.yaml",  # DefaultNames.ServiceTemplate,  # XXX fix unfurl-types hack
    ]
    folders = [DefaultNames.ProjectDirectory, DefaultNames.EnsembleDirectory]

    def __init__(
        self,
        folder: str,
        file: str,
        digest: str = "",
    ) -> None:
        super().__init__(folder, file, digest)
        # XXX set readonly=True after adding representers for AnsibleUnicode etc.

    def analyze(
        self, directory: Directory, repo_info: Repository, root_path: str
    ) -> Optional[Artifact]:
        logger = directory.logger
        path = os.path.join(root_path, self.folder, self.file)
        logger.verbose("analyzing %s", path)
        localenv = LocalEnv(
            path,
            can_be_empty=True,
            parent=directory.cloudmap.local_env,
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
                    directory.cloudmap.add_record(giturl, analyze)

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
                    image_artifact = directory.db.add_image_artifact(image)
                    purl = image_artifact.url
                    references[purl] = None

            artifact_url = repo_info.artifact_url(os.path.join(self.folder, self.file))

            # Create main artifact using helper method
            artifact, cloud_type = create_artifact_from_notable(
                artifact_pkg=artifact_url,
                artifact_type=self.artifact_type,
                name=template_name,
                version=template_version,
                description=template_description,
                thumbnail=repo_info.metadata.thumbnail_url,
                references=references,
                dependencies={
                    name: TypeRefs({v: None}) for name, v in dependencies.items()
                },
                type_info=type_info,
                types_dict=directory.db.types,
                digest=self.digest,
            )

            # Add CloudType if created
            if cloud_type:
                directory.db.types[cloud_type.name] = cloud_type

            # Create CloudTypes for dependencies
            if node:
                for dep_typename in dependencies.values():
                    # Check if type already exists
                    if dep_typename not in directory.db.types:
                        # Get type information for dependency type
                        type_def = node.topology.find_type(dep_typename)
                        if type_def:
                            dep_type_info = self.get_type_info(
                                node.topology, types, type_def
                            )

                            dep_cloud_type = create_cloud_type_from_type_info(
                                dep_type_info, directory.db.types
                            )
                            if dep_cloud_type:
                                directory.db.types[dep_cloud_type.name] = dep_cloud_type

            # Create Instantiation and Service for Ensemble artifacts
            if self.artifact_type == EntitySchema.Ensemble and typename:
                self._create_ensemble_instantiation_and_service(
                    manifest, repo_info, directory, typename, artifact
                )

            # Store artifact ID for repository notable
            self.artifact_id = artifact_url
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
        references[giturl] = type_ref
        return giturl

    def get_type_info(
        self,
        topology: TopologySpec,
        types: ResourceTypesByName,
        entity_type: StatefulEntityType,
    ):
        dep_type_info = None
        dep_type_dict = cast(
            Optional[dict],
            node_type_to_graphql(
                topology,
                entity_type,
                types,
                True,
            ),
        )
        if dep_type_dict:
            dep_type_dict.pop("__typename", None)
            dep_type_info = filter_dict(dep_type_dict)

        # Fallback to minimal CloudType if type not found in custom_defs
        if not dep_type_info:
            dep_type_info = {
                "name": entity_type.global_name,
                "title": entity_type.local_name,
            }

        return dep_type_info

    @classmethod
    def init(
        cls,
        folder: str,
        file: str,
        digest: str = "",
    ) -> Optional[UnfurlNotable]:
        try:
            return UnfurlNotable(folder, file, digest)
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
        directory: Directory,
        typename: str,
        artifact: Artifact,
    ) -> None:
        """
        Create an Instantiation and Service for Ensemble artifacts.

        Args:
            manifest: The YamlManifest object for the ensemble
            repo_info: Repository information
            directory: Directory object to add instantiation and service to
            artifact: The ensemble artifact
        """
        # Get spec repository for source information
        spec_repo_view = manifest.repositories.get("spec")
        analyze: Literal["yes", "no"] = "yes" if directory.do_analysis else "no"

        # XXX add inputs from lock section
        instantiation = Instantiation(
            url=artifact.url,
            revision=repo_info.get_current_commit(),
            type=TypeRefs(types={EntitySchema.Ensemble: None}),
            inputs=artifact.references,
        )
        if spec_repo_view:
            instantiation.source = (
                get_repository_url(spec_repo_view.url)
                + f"#:{get_blueprint_path(manifest)}"
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
                instantiated_by={instantiation.url: None},
            )
            directory.db.services[deployment_url] = service
            instantiation.instantiated = {deployment_url: None}

        directory.db.add_instantiation(instantiation)
        if instantiation.source and not directory.db.get_artifact(instantiation.source):
            directory.cloudmap.add_record(instantiation.source, analyze)

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
    type_info: Dict[str, Any], types_dict: Optional[CloudTypeDict] = None
) -> Optional[CloudType]:
    """
    Create a CloudType from type_info dict if it doesn't already exist.

    Args:
        type_info: Dict with 'name', 'title', 'extends' keys
        types_dict: Optional dict to check for existing types

    Returns:
        CloudType if created, None if type_info is empty or type already exists
    """
    type_name = type_info.get("name", "")
    if not type_name:
        return None

    # Don't create if it already exists
    if types_dict and type_name in types_dict:
        return None

    metadata = CommonMetadata()
    if type_info.get("title"):
        metadata.title = type_info["title"]

    return CloudType(
        name=type_name,
        kind="Component",  # XXX inferred from artifact_type
        metadata=metadata,
        extends=type_info.get("extends", []),
    )


def create_artifact_from_notable(
    artifact_pkg: str,
    artifact_type: str,
    name: str = "",
    version: str = "",
    description: str = "",
    thumbnail: str = "",
    notables: Optional[TypedUrls] = None,
    references: Optional[TypedUrls] = None,
    dependencies: Optional[TypedUrls] = None,
    type_info: Optional[Dict[str, Any]] = None,
    types_dict: Optional[CloudTypeDict] = None,
    digest: str = "",
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
        notables: Map of artifact IDs this artifact references (maps to notable)
        references: Map of artifact IDs this artifact references (maps to references)
        dependencies: List of dependencies (maps to requires)
        type_info: Type definition dict with 'name', 'title', 'extends' (creates CloudType)
        types_dict: Optional dict to check for existing types
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

    # Handle type field: create CloudType and add to instantiates
    instantiates = TypeRefs()
    cloud_type = None
    if type_info and isinstance(type_info, dict):
        cloud_type = create_cloud_type_from_type_info(type_info, types_dict)
        type_name = type_info.get("name", "")
        if type_name:
            # Add to artifact's instantiates
            instantiates.add(type_name)

    # Create the artifact
    artifact = Artifact(
        url=artifact_pkg,
        type=TypeRefs().add(artifact_type, version=version),
        notable=notables or {},
        references=references or {},
        instantiates=instantiates,
        dependencies=dependencies or {},
        metadata=metadata,
        digest=digest,
    )

    return artifact, cloud_type


def migrate_old_notable_format(db: CloudMapDB, repo: Repository) -> List[str]:
    """
    Migrate old Repository.notable dictionary format to new List[str] format.

    Creates Artifact instances from old inline notable definitions and adds them
    to db.artifacts and db.types.

    Args:
        db: CloudMapDB instance to add artifacts and types to
        repo: Repository with potentially old-format notable dict

    Returns:
        Notable converted to artifact IDs
    """
    migrated_artifact_ids: List[str] = []
    for file_path, notable_dict in cast(Dict[str, dict], repo.notable).items():
        if "artifact_type" in notable_dict:
            # Create artifact pkg from repository URL + file path
            artifact_pkg = repo.artifact_url(file_path)

            # Create artifact using helper method
            artifact, cloud_type = create_artifact_from_notable(
                artifact_pkg=artifact_pkg,
                artifact_type=notable_dict.pop("artifact_type", ""),
                name=notable_dict.pop("name", ""),
                version=str(notable_dict.pop("version", "") or ""),
                description=notable_dict.pop("description", ""),
                thumbnail=notable_dict.pop("thumbnail_url", "")
                or repo.metadata.thumbnail_url,
                notables={
                    build_oci_purl(ContainerImage.split(ref)): None
                    for ref in notable_dict.pop("artifacts", [])
                },
                dependencies={
                    "": TypeRefs(
                        {v: None for v in notable_dict.pop("dependencies", [])}
                    )
                },
                type_info=notable_dict.pop("type", None),
                types_dict=db.types,
            )
            while notable_dict:
                notable_dict.popitem()  # remove any remaining old fields
            # update notable to new format:
            notable_dict["type"] = artifact.type
            notable_dict["artifact"] = artifact_pkg

            # Add CloudType if created
            if cloud_type:
                db.types[cloud_type.name] = cloud_type

            # Add to artifacts dict
            db.artifacts[artifact_pkg] = artifact
            migrated_artifact_ids.append(artifact_pkg)

    return migrated_artifact_ids


Notables = (UnfurlNotable, ContainerBuilderNotable)
