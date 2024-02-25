# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
"""
This module defines GraphQL representations of ensembles and Unfurl environments,
including simplified representations of TOSCA types and node templates as documented by the GraphQL schema below.

These objects are exported as JSON by the `export` command and by unfurl server API endpoints.

.. code-block::

    type DeploymentTemplate {
      name: String!
      title: String!
      slug: String!
      description: String

      blueprint: ApplicationBlueprint!
      primary: ResourceTemplate!
      resourceTemplates: [ResourceTemplate!]
      cloud: ResourceType
      environmentVariableNames: [String!]
      source: String
      projectPath: String!
      commitTime: String
    }

    type Deployment {
      title: String!
      primary: Resource
      resources: [Resource!]
      deploymentTemplate: DeploymentTemplate!
      url: url
      status: Status
      summary: String
      workflow: String
      deployTime: String
      packages: JSON
    }

    type ResourceType {
        name: String!
        title: String
        extends: [ResourceType!]
        description: String
        badge: String
        icon: String
        visibility: String
        details_url: String
        inputsSchema: JSON
        computedPropertiesSchema: JSON
        outputsSchema: JSON
        requirements: [RequirementConstraint!]
        implementations: [String]
        implementation_requirements: [String]
        directives: [String]
        metadata: JSON
        _sourceinfo: JSON
    }

    type RequirementConstraint {
        name: String!
        title: String
        description: String
        resourceType: ResourceType!
        match: ResourceTemplate
        min: Int
        max: Int
        badge: String
        visibility: String
        icon: String
        inputsSchema: Required[JSON]
        requirementsFilter: [RequirementConstraint!]
    }

    type ResourceTemplate {
        name: String!
        title: String
        type: ResourceType!
        visibility: String
        directives: [String!]
        imported: String
        metadata: JSON

        description: string

        # Maps to an object that conforms to type.inputsSchema
        properties: [Input!]

        dependencies: [Requirement!]
    }

    type Requirement {
      name: String!
      constraint: RequirementConstraint!
      match: ResourceTemplate
      target: Resource
      visibility: String
    }

    type Resource {
      name: String!
      title: String!
      url: String
      template: ResourceTemplate!
      status: Status
      state: State
      attributes: [Input!]
      computedProperties: [Input!]
      connections: [Requirement!]
      protected: Boolean
      imported: String
    }

    type DeploymentEnvironment {
        name: String!
        connections: [ResourceTemplate!]
        instances: [ResourceTemplate!]
        primary_provider: ResourceTemplate
        repositories: JSON!
    }

    type DeploymentPath {
      name: String!
      environment: String!
      project_id: String
      pipelines: [JSON!]
      incremental_deploy: boolean!
    }

    type ApplicationBlueprint {
      name: String!
      title: String
      description: String
      primary: ResourceType!
      primaryDeploymentBlueprint: String
      deploymentTemplates: [DeploymentTemplate!]

      livePreview: String
      sourceCodeUrl: String
      image: String
      projectIcon: String
    }
"""
import datetime
import re
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    NewType,
    Optional,
    Tuple,
    TypeVar,
    Union,
    cast,
    TYPE_CHECKING,
)
from typing_extensions import TypedDict, NotRequired, Required
from collections.abc import Mapping, MutableSequence
import os
import json
from urllib.parse import urlparse
from toscaparser.common.exception import ExceptionCollector
from toscaparser.elements.constraints import Schema
from toscaparser.elements.entity_type import Namespace
from toscaparser.elements.statefulentitytype import StatefulEntityType
from toscaparser.imports import is_url, SourceInfo
from toscaparser.nodetemplate import NodeTemplate
from toscaparser.properties import Property
from toscaparser.elements.scalarunit import get_scalarunit_class
from toscaparser.elements.property_definition import PropertyDef
from toscaparser.elements.portspectype import PortSpec
from toscaparser.topology_template import find_type
from .repo import normalize_git_url_hard
from .packages import get_package_id_from_url
from .result import ChangeRecord
from .runtime import EntityInstance, TopologyInstance
from .logs import sensitive, is_sensitive, getLogger
from .spec import NodeSpec, TopologySpec, is_function
from .lock import Lock
from .util import to_enum, UnfurlError, unique_name
from .support import NodeState, Status

if TYPE_CHECKING:
    from .yamlmanifest import YamlManifest
    from .localenv import Project

logger = getLogger("unfurl")

# setting UNFURL_EXPORT_LOCALNAMES disables using fulling qualified names when exporting
EXPORT_QUALNAME = not bool(os.getenv("UNFURL_EXPORT_LOCALNAMES"))

JsonType = Dict[str, Any]


class GraphqlObject(TypedDict, total=False):
    name: Required[str]
    __typename: Required[str]
    title: NotRequired[str]
    description: NotRequired[str]
    visibility: NotRequired[str]
    metadata: NotRequired[JsonType]


ResourceTemplateName = str

_GraphqlObject_T = TypeVar("_GraphqlObject_T", bound=GraphqlObject, covariant=True)
GraphqlObjectsByName = Dict[str, _GraphqlObject_T]

TypeName = NewType("TypeName", str)
RequirementConstraintName = str


class ApplicationBlueprint(GraphqlObject, total=False):
    primary: TypeName
    primaryDeploymentBlueprint: Optional[str]
    deploymentTemplates: List[str]
    livePreview: Optional[str]
    sourceCodeUrl: Optional[str]
    image: Optional[str]
    projectIcon: Optional[str]


class DeploymentTemplate(GraphqlObject, total=False):
    slug: str
    blueprint: str
    primary: ResourceTemplateName
    resourceTemplates: List[ResourceTemplateName]
    cloud: TypeName
    environmentVariableNames: List[str]
    source: NotRequired[Optional[str]]
    projectPath: str
    commitTime: str
    ResourceTemplate: "ResourceTemplatesByName"


class Deployment(GraphqlObject, total=False):
    primary: str
    resources: List[str]
    deploymentTemplate: str
    url: str
    status: "Status"
    summary: str
    workflow: str
    deployTime: str
    packages: JsonType


class RequirementConstraint(GraphqlObject):
    resourceType: TypeName
    match: Optional[ResourceTemplateName]
    min: int
    max: int
    badge: str
    icon: str
    inputsSchema: JsonType
    requirementsFilter: NotRequired[List["RequirementConstraint"]]
    node_filter: NotRequired[Dict[str, Any]]


class Requirement(GraphqlObject):
    constraint: RequirementConstraint
    match: Optional[ResourceTemplateName]
    target: NotRequired[str]


class ImportDef(TypedDict):
    file: str
    repository: NotRequired[str]
    prefix: NotRequired[str]
    url: NotRequired[str]  # added after creation
    incomplete: NotRequired[bool]  # added by cloudmap in server


class ResourceType(GraphqlObject, total=False):
    extends: Required[List[TypeName]]
    badge: NotRequired[str]
    icon: NotRequired[str]
    details_url: NotRequired[str]
    inputsSchema: Required[JsonType]
    computedPropertiesSchema: JsonType
    outputsSchema: JsonType
    requirements: Required[List[RequirementConstraint]]
    implementations: List[str]
    implementation_requirements: List[TypeName]
    directives: List[str]
    _sourceinfo: Optional[ImportDef]


class ResourceTemplate(GraphqlObject, total=False):
    type: TypeName
    directives: List[str]
    imported: str
    # Maps to an object that conforms to type.inputsSchema
    properties: List[dict]
    dependencies: List[Requirement]


ResourceTemplatesByName = Dict[str, ResourceTemplate]


class DeploymentEnvironment(TypedDict, total=False):
    name: str
    connections: ResourceTemplatesByName
    instances: Required[ResourceTemplatesByName]
    primary_provider: Optional[ResourceTemplate]
    repositories: JsonType


def add_unique_prefix(namespace: Namespace, import_def: ImportDef, namespace_id: str):
    # make sure prefix is unique in the namespace
    repository = import_def.get("repository")
    prefix = import_def.get("prefix", repository)
    if not prefix:
        url = import_def.get("url") or ""
        prefix = re.sub(r"\W", "_", namespace_id)
    import_def["prefix"] = unique_name(
        prefix, [n.partition(".")[0] for n in namespace.imports.values()]
    )
    return prefix


def get_local_type(
    namespace: Optional[Namespace], global_name: str, import_def: Optional[ImportDef]
) -> Tuple[str, Optional[ImportDef]]:
    local = None
    if namespace:
        local = namespace.get_local_name(global_name)
        if local:
            import_def = None
        elif import_def:  # namespace_id not found, need to import
            add_unique_prefix(namespace, import_def, global_name.split("@")[1])
            # XXX else log.warning
    if not local:
        local = global_name.split("@")[0]
        if import_def:
            prefix = import_def.get("prefix")
            if prefix:
                local = f"{prefix}.{local}"
    return local, import_def


def get_import_def(source_info: SourceInfo) -> ImportDef:
    url, file = _get_url_from_namespace(source_info)
    import_def = ImportDef(file=file)
    if source_info["repository"]:
        import_def["repository"] = source_info["repository"]
    if url == "unfurl":
        url = "github.com/onecommons/unfurl"
    if url:  # otherwise import relative to main service template
        import_def["url"] = url
    return import_def


def _get_url_from_namespace(source_info: SourceInfo) -> Tuple[str, str]:
    url_or_path = source_info["root"]
    if source_info["repository"] == "unfurl":
        url = "unfurl"
    elif url_or_path and is_url(url_or_path):
        url = url_or_path
    else:  # otherwise import relative to main service template
        url = ""
    return url, source_info["file"]


def get_package_url(url: str) -> str:
    "Convert the given url to a package id or if it can't be converted, normalize the URL."
    package_id, purl, revision = get_package_id_from_url(url)
    if package_id:
        return package_id
    else:
        return normalize_git_url_hard(url)


def to_type_name(name: str, package_id: str, path: str) -> TypeName:
    """Example  of namespace_ids:

    Package id:
    "ContainerComputeHost@unfurl.cloud/onecommons/unfurl-types"

    Package id with filename:
    "ContainerComputeHost@unfurl.cloud/onecommons/unfurl-types:aws"
    """
    if path and path != "service-template.yaml":
        return TypeName(name + "@" + package_id + ":" + os.path.splitext(path)[0])
    else:
        return TypeName(name + "@" + package_id)


# called by get_repository_url
def get_namespace_id(root_url: str, info: SourceInfo) -> str:
    namespace_id = info.get("namespace_uri")
    if namespace_id:
        return namespace_id
    url, path = _get_url_from_namespace(info)
    # use root_url if no repository
    package_id = get_package_url(url or root_url)
    if path and path not in [
        ".",
        "service-template.yaml",
        "ensemble.yaml",
        "ensemble-template.yaml",
        "dummy-ensemble.yaml",
    ]:
        return package_id + ":" + os.path.splitext(path)[0]
    else:
        return package_id


class ResourceTypesByName(Dict[TypeName, ResourceType]):
    def __init__(self, qualifier: str, custom_defs: Namespace):
        self.qualifier = get_package_url(qualifier)
        self.custom_defs = custom_defs

    def expand_typename(self, nodetype: str) -> TypeName:
        if not EXPORT_QUALNAME:
            # expansion disabled
            return TypeName(nodetype)
        if "@" in nodetype:
            # already fully qualified
            return TypeName(nodetype)
        nodetype = nodetype.replace("tosca:", "tosca.nodes.")  # special case
        # XXX lookup type and expand
        if nodetype in StatefulEntityType.TOSCA_DEF:
            # starts with "tosca." or "unfurl."
            return TypeName(nodetype)
        if nodetype in self.custom_defs:
            return TypeName(self.custom_defs.get_global_name(nodetype))
        else:
            logger.warning("could not find %s types namespace", nodetype)
        return to_type_name(nodetype, self.qualifier, "")

    def _get_extends(
        self,
        topology: TopologySpec,
        typedef: StatefulEntityType,
        extends: List[TypeName],
        convert: Optional[Callable],
    ) -> None:
        if not typedef:
            return
        name = self.get_typename(typedef)
        if name not in extends:
            extends.append(name)
        if convert and name not in self:
            convert(topology, typedef, self)
        ExceptionCollector.collecting = True
        for p in typedef.parent_types():
            self._get_extends(topology, p, extends, convert)
        for alias in typedef.aliases:
            if alias not in extends:
                extends.append(TypeName(alias))

    def get_type(self, typename: str) -> Optional[ResourceType]:
        return self.get(self.expand_typename(typename))

    def get_typename(self, typedef: StatefulEntityType) -> TypeName:
        return TypeName(typedef.global_name if EXPORT_QUALNAME else typedef.type)
        # typedef might not be in types yet
        # return self.expand_typename(typedef.type)

    @staticmethod
    def get_localname(typename: str):
        return typename.partition("@")[0]

    def _make_typedef(self, name: str, all=False) -> Optional[StatefulEntityType]:
        local_name = self.custom_defs.get_local_name(name)
        if local_name:
            typedef = find_type(local_name, self.custom_defs)
        else:
            typedef = None
        if not typedef:
            logger.warning("Missing type definition for %s", name)
        return typedef


class DeploymentPath(GraphqlObject):
    environment: str
    project_id: NotRequired[str]
    pipelines: List[JsonType]
    incremental_deploy: bool


class DeploymentPaths(TypedDict):
    DeploymentPath: Dict[str, DeploymentPath]
    deployments: NotRequired[List["GraphqlDB"]]


class GraphqlDB(Dict[str, GraphqlObjectsByName]):
    def get_types(self) -> ResourceTypesByName:
        return cast(ResourceTypesByName, self["ResourceType"])

    @staticmethod
    def load_db(existing: Optional[str]) -> "GraphqlDB":
        if existing and os.path.exists(existing):
            with open(existing) as f:
                return cast(GraphqlDB, json.load(f))
        else:
            return GraphqlDB({})

    @staticmethod
    def get_deployment_paths(
        project: "Project", existing: Optional[str] = None
    ) -> "DeploymentPaths":
        """
        Deployments identified by their file path.
        """
        db = cast(DeploymentPaths, GraphqlDB.load_db(existing))
        deployment_paths = db.setdefault("DeploymentPath", {})
        for ensemble_info in project.localConfig.ensembles:
            if "environment" in ensemble_info and "project" not in ensemble_info:
                # exclude external ensembles
                path = os.path.dirname(ensemble_info["file"])
                if os.path.isabs(path):
                    path = project.get_relative_path(path)
                obj = DeploymentPath(
                    __typename="DeploymentPath",
                    name=path,
                    project_id=ensemble_info.get("project_id"),
                    pipelines=ensemble_info.get("pipelines", []),
                    environment=ensemble_info["environment"],
                    incremental_deploy=ensemble_info.get("incremental_deploy", False),
                )
                if path in deployment_paths:
                    # merge duplicate entries
                    deployment_paths[path].update(obj)  # type: ignore # python 3.7 needs this
                else:
                    deployment_paths[path] = obj
        return db

    def add_graphql_deployment(
        self,
        manifest: "YamlManifest",
        dtemplate: DeploymentTemplate,
        nodetemplate_to_json: Callable,
    ) -> Deployment:
        name = dtemplate["name"]
        title = dtemplate.get("title") or name
        deployment = Deployment(name=name, title=title, __typename="Deployment")
        templates = cast(ResourceTemplatesByName, self["ResourceTemplate"])
        relationships: Dict[str, List[Requirement]] = {}
        for t in templates.values():
            if "dependencies" in t:
                for c in t["dependencies"]:
                    if c.get("match"):
                        relationships.setdefault(c["match"], []).append(c)  # type: ignore
        assert manifest.rootResource
        resources: List[GraphqlObject] = []
        add_resources(
            self,
            relationships,
            manifest.rootResource,
            resources,
            None,
            nodetemplate_to_json,
        )
        self["Resource"] = {r["name"]: r for r in resources}
        deployment["resources"] = list(self["Resource"])
        primary_name = deployment["primary"] = dtemplate["primary"]
        deployment["deploymentTemplate"] = dtemplate["name"]
        if manifest.lastJob:
            _add_lastjob(manifest.lastJob, deployment)

        primary_resource = self["Resource"].get(primary_name)
        _set_deployment_url(manifest, deployment, primary_resource)
        if primary_resource and primary_resource["title"] == primary_name:
            primary_resource["title"] = deployment["title"]
        packages = {}
        for package_id, repo_dict in Lock(manifest).find_packages():
            # lock packages to the last deployed version
            # note: discovered_revision maybe "(MISSING)" if no remote tags were found at lock time
            version = (
                repo_dict.get("tag")
                or repo_dict.get("revision")  # tag or branch explicitly set
                or repo_dict.get("discovered_revision")
            )
            if not version and "discovered_revision" not in repo_dict:
                # old version of lock section YAML, set missing to True
                version = "(MISSING)"
            if version:
                packages[_project_path(repo_dict["url"])] = dict(version=version)
        if packages:
            deployment["packages"] = packages

        self["Deployment"] = {name: deployment}
        return deployment


def _project_path(url):
    pp = urlparse(url).path.lstrip("/")
    if pp.endswith(".git"):
        return pp[:-4]
    return pp


def js_timestamp(ts: datetime.datetime) -> str:
    jsts = ts.isoformat(" ", "seconds")
    if ts.utcoffset() is None:
        # if no timezone assume utc and append the missing offset
        return jsts + "+00:00"
    return jsts


def _add_lastjob(last_job: dict, deployment: Deployment) -> None:
    readyState = last_job.get("readyState")
    workflow = last_job.get("workflow")
    if workflow:
        deployment["workflow"] = workflow
    if isinstance(readyState, dict):
        deployment["status"] = to_enum(
            Status, readyState.get("effective", readyState.get("local"))
        )
        if workflow == "undeploy" and deployment["status"] == Status.ok:
            deployment["status"] = Status.absent
    deployment["summary"] = last_job.get("summary", "")
    deployTimeString = last_job.get("endTime", last_job["startTime"])
    deployment["deployTime"] = js_timestamp(
        datetime.datetime.strptime(deployTimeString, ChangeRecord.DateTimeFormat)
    )


def _set_deployment_url(
    manifest, deployment: Deployment, primary_resource: Optional[GraphqlObject]
):
    outputs = manifest.get_saved_outputs()
    if outputs and "url" in outputs:
        url = outputs["url"]
    else:
        # computed outputs might not be saved, so try to evaluate it now
        try:
            url = manifest.rootResource.attributes["outputs"].get("url")
        except UnfurlError as e:
            url = None  # this can be raised if the evaluation is unsafe
            logger.warning(f"export could not evaluate output 'url': {e}")

    if url:
        deployment["url"] = url
    elif primary_resource and primary_resource.get("attributes"):
        for prop in primary_resource["attributes"]:  # type: ignore
            if prop["name"] == "url":
                deployment["url"] = prop["value"]
                break


class Resource(GraphqlObject):
    url: str
    template: ResourceTemplateName
    status: Optional["Status"]
    state: Optional["NodeState"]
    attributes: List[dict]
    computedProperties: List[dict]
    connections: List[Requirement]
    protected: Optional[bool]
    imported: Optional[str]


def _is_front_end_expression(value) -> bool:
    if isinstance(value, dict):
        if "eval" in value:
            expr = value["eval"]
            return "abspath" in expr or "get_dir" in expr
        else:
            return "get_env" in value or "secret" in value or "_generate" in value
    return False


def is_server_only_expression(value) -> bool:
    if isinstance(value, list):
        return any(is_function(item) for item in value)
    if _is_front_end_expression(value):  # special case for client
        return False
    return is_function(value)


def is_computed(p: Union[Property, PropertyDef]) -> bool:
    # XXX be smarter about is_computed() if the user should be able to override the default
    if isinstance(p.schema, Schema):
        metadata = p.schema.metadata
    else:
        metadata = p.schema.get("metadata") or {}
    return bool(
        p.name in ["tosca_id", "state", "tosca_name"]
        or is_server_only_expression(p.value)
        or (p.value is None and is_server_only_expression(p.default))
        or metadata.get("computed")
    )


def is_property_user_visible(p: PropertyDef) -> bool:
    user_settable = p.schema.get("metadata", {}).get("user_settable")
    if user_settable is not None:
        return user_settable
    if p.default is not None or is_computed(p):
        return False
    return True


class PropertyVisitor:
    redacted = False
    user_settable = False

    def redact_if_sensitive(self, value):
        if isinstance(value, Mapping):
            return {key: self.redact_if_sensitive(v) for key, v in value.items()}
        elif isinstance(value, (MutableSequence, tuple)):
            return [self.redact_if_sensitive(item) for item in value]
        elif is_sensitive(value):
            self.redacted = True
            return sensitive.redacted_str
        else:
            return value

    def attribute_value_to_json(self, p, value):
        if p.schema.metadata.get("sensitive"):
            if is_server_only_expression(value) or _is_front_end_expression(value):
                return value
            self.redacted = True
            return sensitive.redacted_str

        if isinstance(value, PortSpec):
            return value.spec
        elif isinstance(value, dict) and p.type in [
            "tosca.datatypes.network.PortSpec",
            "PortSpec",
        ]:
            return PortSpec.make(value).spec
        scalar_class = get_scalarunit_class(p.type)
        if scalar_class:
            unit = p.schema.metadata.get("default_unit")
            if unit:  # convert to the default_unit
                return scalar_class(value).get_num_from_scalar_unit(unit)
        return self.redact_if_sensitive(value)

    def template_properties_to_json(self, nodetemplate: NodeTemplate):
        # if they aren't only include ones with an explicity value
        for p in nodetemplate.get_properties_objects():
            computed = is_computed(p)
            if computed and not _is_front_end_expression(p.value):
                # don't expose values that are expressions to the user
                value = None
            else:
                value = self.attribute_value_to_json(p, p.value)
            user_visible = is_property_user_visible(p)
            if user_visible:
                self.user_settable = True
            else:
                if p.value is None or p.value == p.default:
                    # assumed to not be set, just use the default value and skip
                    continue
                if computed:
                    # preserve the expression
                    value = p.value
            yield dict(name=p.name, value=value)


def attribute_value_to_json(p, value):
    return PropertyVisitor().attribute_value_to_json(p, value)


def always_export(p: Property) -> bool:
    if isinstance(p.schema, Schema):
        metadata = p.schema.metadata
    else:
        metadata = p.schema.get("metadata") or {}
    return bool(metadata.get("export"))


def maybe_export_value(prop: Property, instance: EntityInstance, attrs: List[dict]):
    export = always_export(prop)
    if export:
        # evaluate computed property now
        try:
            value = instance.attributes[prop.name]
        except Exception as e:
            # this can be raised if the evaluation is unsafe
            logger.warning(
                f"export could not evaluate property {prop.name} on {instance}: {e}"
            )
        else:
            attrs.append(
                dict(
                    name=prop.name,
                    value=attribute_value_to_json(prop, value),
                )
            )
    return export


def add_attributes(instance: EntityInstance) -> List[Dict[str, Any]]:
    attrs = []
    attributeDefs = instance.template.attributeDefs.copy()
    # instance._attributes only has values that were set by the instance, not spec properties or attribute defaults
    # instance._attributes should already be serialized
    if instance.shadow:
        _attributes = instance.shadow._attributes
    else:
        _attributes = instance._attributes
    if _attributes:
        for name, value in _attributes.items():
            p = attributeDefs.pop(name, None)
            if not p:
                # check if this attribute is shadowing a property
                p = instance.template.propertyDefs.get(name)
                if not p:
                    # same as EntityTemplate._create_properties()
                    p = Property(
                        name,
                        value,
                        dict(type="any"),
                        instance.template.toscaEntityTemplate.custom_def,
                    )
            if not p.schema.get("metadata", {}).get("internal"):
                attrs.append(dict(name=p.name, value=attribute_value_to_json(p, value)))
    # add leftover attribute defs that have a default value
    for prop in attributeDefs.values():
        if prop.default is not None:
            if not maybe_export_value(prop, instance, attrs) and not is_computed(prop):
                attrs.append(
                    dict(
                        name=prop.name,
                        value=attribute_value_to_json(prop, prop.default),
                    )
                )
    return attrs


def add_computed_properties(instance: EntityInstance) -> List[Dict[str, Any]]:
    attrs = []
    if instance.shadow:
        _attributes = instance.shadow._attributes
    else:
        _attributes = instance._attributes
    # instance._properties should already be serialized
    _properties = instance._properties or {}
    if _properties:
        for name, value in _properties.items():
            if name in _attributes:  # shadowed, don't include both
                continue
            p = instance.template.propertyDefs.get(name)
            if not p:
                # same as EntityTemplate._create_properties()
                p = Property(
                    name,
                    value,
                    dict(type="any"),
                    instance.template.toscaEntityTemplate.custom_def,
                )
            if p.schema.get("metadata", {}).get("visibility") != "hidden":
                if is_sensitive(value) and _is_front_end_expression(p.value):
                    value = p.value
                else:
                    value = attribute_value_to_json(p, value)
                attrs.append(dict(name=p.name, value=value))

    for prop in instance.template.propertyDefs.values():
        if prop.default is not None and prop.name not in instance._properties:
            if prop.name not in instance.template.attributeDefs:
                maybe_export_value(prop, instance, attrs)

    return attrs


def is_resource_real(instance):
    for resource_types in ["unfurl.nodes.CloudObject", "unfurl.nodes.K8sRawResource"]:
        if instance.template.is_compatible_type(resource_types):
            return True
    if (
        "id" in instance.attributes
        or "console_url" in instance.attributes
        or "url" in instance.attributes
    ):
        return True
    return False


def add_resources(
    db: GraphqlDB,
    relationships: Dict[str, List[Requirement]],
    root: TopologyInstance,
    resources: List[GraphqlObject],
    shadowed: Optional[EntityInstance],
    nodetemplate_to_json: Callable,
) -> None:
    for instance in root.get_self_and_descendants():
        # don't include the nested template that is a shadow of the outer template
        if instance is not root and instance is not shadowed:
            resource = to_graphql_resource(
                instance, db, relationships, nodetemplate_to_json
            )
            if not resource:
                continue
            resources.append(resource)
            if cast(NodeSpec, instance.template).substitution and instance.shadow:
                assert instance.shadow.root is not instance.root
                add_resources(
                    db,
                    relationships,
                    cast(TopologyInstance, instance.shadow.root),
                    resources,
                    instance.shadow,
                    nodetemplate_to_json,
                )


def to_graphql_resource(
    instance: EntityInstance,
    db: GraphqlDB,
    relationships: Dict[str, List[Requirement]],
    nodetemplate_to_json: Callable,
) -> Optional[GraphqlObject]:
    # XXX url
    template = cast(
        Optional[ResourceTemplate], db["ResourceTemplate"].get(instance.template.name)
    )
    try:
        if instance._lastStatus is not None:  # "effective" status in the YAML
            status = instance._lastStatus
        else:
            status = instance.status
    except:
        status = Status.error
        logger.error(f"Error getting live status for resource {instance.nested_name}", exc_info=True)

    pending = not status or status == Status.pending
    if not template:
        if pending or "virtual" in instance.template.directives:
            return None
        template = nodetemplate_to_json(
            instance.template,
            db.get_types(),
            True,
        )
        if template.get("visibility") == "omit":
            return None
        db["ResourceTemplate"][instance.template.name] = template

    resource = Resource(  # type: ignore
        name=instance.nested_name,
        title=template.get("title", instance.name),
        template=ResourceTemplateName(instance.template.name),
        state=instance.state,
        status=status,
        __typename="Resource",
        imported=instance.imported,
    )

    if "visibility" in template and template["visibility"] != "inherit":
        resource["visibility"] = template["visibility"]
    else:
        if instance.template.name in relationships:
            visibilities = set(
                reqjson["constraint"].get("visibility", "inherit")
                for reqjson in relationships[instance.template.name]
            )
            # visible visibilities override hidden
            if len(visibilities) == 1 and "hidden" in visibilities:
                # if the relationship was hidden only include the resource if its "real"
                if not is_resource_real(instance):
                    resource["visibility"] = "hidden"
        else:
            if pending:
                if instance.template.aggregate_only():
                    resource["status"] = Status.ok
                else:
                    # this resource wasn't referenced, don't include if it wasn't part of the plan
                    # XXX: have a more accurate way to figure this out
                    return None
    logger.debug(
        f"exporting resource {instance.name} with visibility {resource.get('visibility')}"
    )

    resource["protected"] = instance.protected
    resource["attributes"] = add_attributes(instance)
    resource["computedProperties"] = add_computed_properties(instance)

    if template.get("dependencies"):
        assert "dependencies" in template
        requirements = {r["name"]: r.copy() for r in template["dependencies"]}

        # XXX there is a bug where this can't find instances sometimes:
        # so for now just copy match if it exists
        # for req in instance.template.requirements.values():
        #     # test because it might not be set if the template is incomplete
        #     if req.relationship and req.relationship.target:
        #         requirements[req.name]["target"] = req.relationship.target.name
        for req in requirements.values():
            if req.get("match"):
                req["target"] = req["match"]  # type: ignore
        resource["connections"] = list(requirements.values())
    else:
        resource["connections"] = []
    return cast(GraphqlObject, resource)
