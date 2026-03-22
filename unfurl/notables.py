# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, cast
from typing_extensions import Literal
from toscaparser.nodetemplate import NodeTemplate
from toscaparser.elements.statefulentitytype import StatefulEntityType

from .cloudmap import (
    CloudMapDB,
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
    EntitySchema,
    Instantiation,
    TypeRefs,
    TypedUrls,
    filter_dict,
)
from .to_json import get_blueprint_path, node_type_to_graphql
from .util import UnfurlError, assert_not_none
from .localenv import LocalEnv
from .logs import getLogger
from . import DefaultNames

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

            node = self._get_root_node(spec)
            # schema_repo = manifest.repositories.get("types")
            # schema = schema_repo.url.strip(":") if schema_repo else ""

            # Prepare type_info and dependencies
            type_info = None
            typename = ""
            dependencies: dict[str, TypeName] = {}
            notables: TypedUrls = {}
            for name, repo_view in manifest.repositories.items():
                if name not in ("spec", "self", "project", "unfurl"):
                    if repo_view.url.startswith("git-local://") or os.path.isabs(
                        repo_view.url
                    ):
                        continue
                    giturl = get_repository_url(repo_view.url)
                    notables[giturl] = None
                    directory.cloudmap.add_record(giturl, analyze)

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
                # if self.artifact_type != EntitySchema.Ensemble:
                #     # ensembles are instantiations so don't add instantiates key
                #     type_info = _type_info

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
                    notables[purl] = None

            artifact_url = repo_info.artifact_url(os.path.join(self.folder, self.file))

            # Create main artifact using helper method
            artifact, cloud_type = CloudMapDB.create_artifact_from_notable(
                artifact_pkg=artifact_url,
                artifact_type=self.artifact_type,
                name=template_name,
                version=template_version,
                description=template_description,
                thumbnail=repo_info.metadata.thumbnail_url,
                notables=notables,
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

                            dep_cloud_type = (
                                CloudMapDB.create_cloud_type_from_type_info(
                                    dep_type_info, directory.db.types
                                )
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
            inputs=artifact.notable,
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
                instantiated_by=[instantiation.url],
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

Notables = (UnfurlNotable, ContainerBuilderNotable)
