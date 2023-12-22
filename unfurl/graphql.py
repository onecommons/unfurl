from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    NewType,
    Optional,
    Tuple,
    cast,
)
from typing_extensions import TypedDict, NotRequired, Required
from collections.abc import Mapping, MutableSequence
import os
import json
from toscaparser.common.exception import ExceptionCollector
from toscaparser.elements.constraints import Schema
from toscaparser.elements.statefulentitytype import StatefulEntityType
from toscaparser.elements.artifacttype import ArtifactTypeDef
from toscaparser.elements.relationshiptype import RelationshipType
from toscaparser.elements.nodetype import NodeType
from toscaparser.nodetemplate import NodeTemplate
from toscaparser.properties import Property
from toscaparser.elements.scalarunit import get_scalarunit_class
from toscaparser.elements.property_definition import PropertyDef
from toscaparser.elements.portspectype import PortSpec
from .runtime import EntityInstance
from .logs import sensitive, is_sensitive, getLogger
from .spec import TopologySpec, is_function
from .support import NodeState, Status
from .localenv import LocalEnv, Project

logger = getLogger("unfurl")

JsonType = Dict[str, Any]


class GraphqlObject(TypedDict, total=False):
    name: Required[str]
    __typename: Required[str]
    title: NotRequired[str]
    description: NotRequired[str]
    visibility: NotRequired[str]
    metadata: NotRequired[JsonType]


ResourceTemplateName = str

GraphqlObjectsByName = Dict[str, GraphqlObject]

TypeName = NewType("TypeName", str)
# XXX RequirementConstraintName = NewType("RequirementConstraintName", str)
RequirementConstraintName = str


class Deployment(GraphqlObject, total=False):
    """
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
    """

    primary: str
    resources: List[str]
    deploymentTemplate: str
    url: str
    status: Status
    summary: str
    workflow: str
    deployTime: str
    packages: JsonType


class RequirementConstraint(GraphqlObject):
    """
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
    """

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
    """
    type Requirement {
      name: String!
      constraint: RequirementConstraint!
      match: ResourceTemplate
      target: Resource
      visibility: String
    }
    """

    constraint: RequirementConstraint
    match: Optional[ResourceTemplateName]
    target: NotRequired[str]


class ResourceType(GraphqlObject, total=False):
    """
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
    """

    extends: Required[List[TypeName]]
    badge: NotRequired[str]
    icon: NotRequired[str]
    details_url: NotRequired[str]
    inputsSchema: JsonType
    computedPropertiesSchema: JsonType
    outputsSchema: JsonType
    requirements: Required[List[RequirementConstraint]]
    implementations: List[str]
    implementation_requirements: List[TypeName]
    _sourceinfo: JsonType
    directives: List[str]


class ResourceTemplate(GraphqlObject, total=False):
    """
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
    """

    type: TypeName
    directives: List[str]
    imported: str
    # Maps to an object that conforms to type.inputsSchema
    properties: List[dict]
    dependencies: List[Requirement]


ResourceTemplatesByName = Dict[str, ResourceTemplate]


class DeploymentEnvironment(TypedDict, total=False):
    """
    type DeploymentEnvironment {
      name: String!
      connections: [ResourceTemplate!]
      instances: [ResourceTemplate!]
      primary_provider: ResourceTemplate
      repositories: JSON!
    }
    """

    name: str
    connections: ResourceTemplatesByName
    instances: Required[ResourceTemplatesByName]
    primary_provider: Optional[ResourceTemplateName]
    repositories: JsonType


class ResourceTypesByName(Dict[TypeName, ResourceType]):
    def __init__(self, qualifier: str = ""):
        self.qualifier = qualifier

    def expand_typename(self, nodetype: str) -> TypeName:
        # XXX idempotent
        # XXX lookup type and expand
        return TypeName(nodetype.replace("tosca:", "tosca.nodes."))  # XXX

    def _get_extends(
        self,
        topology: TopologySpec,
        typedef: StatefulEntityType,
        extends: List[TypeName],
        convert: Optional[Callable],
    ) -> None:
        if not typedef:
            return
        name = self.expand_typename(typedef.type)
        if name not in extends:
            extends.append(name)
        if convert and name not in self:
            convert(topology, typedef, self)
        ExceptionCollector.collecting = True
        for p in typedef.parent_types():
            self._get_extends(topology, p, extends, convert)

    def get_type(self, typename: str) -> Optional[ResourceType]:
        return self.get(self.expand_typename(typename))

    def get_typename(self, typedef: Optional[StatefulEntityType]) -> TypeName:
        # typedef might not be in types yet
        assert typedef
        return TypeName(typedef.type)

    def _make_typedef(
        self, typename: str, custom_defs, all=False
    ) -> Optional[StatefulEntityType]:
        # XXX handle fully qualified types
        typedef = None
        # prefix is only used to expand "tosca:Type"
        test_typedef = StatefulEntityType(
            typename, StatefulEntityType.NODE_PREFIX, custom_defs
        )
        if not test_typedef.defs:
            logger.warning("Missing type definition for %s", typename)
            return typedef
        elif "derived_from" not in test_typedef.defs:
            _source = test_typedef.defs.get("_source")
            section = isinstance(_source, dict) and _source.get("section")
            if _source and not section:
                logger.warning(
                    'Unable to determine type of %s: missing "derived_from" key',
                    typename,
                )
            elif section == "node_types":
                custom_defs[typename]["derived_from"] = "tosca.nodes.Root"
            elif section == "relationship_types":
                custom_defs[typename]["derived_from"] = "tosca.relationships.Root"
        if test_typedef.is_derived_from("tosca.nodes.Root"):
            typedef = NodeType(typename, custom_defs)
        elif test_typedef.is_derived_from("tosca.relationships.Root"):
            typedef = RelationshipType(typename, custom_defs)
        elif all:
            if test_typedef.is_derived_from("tosca.artifacts.Root"):
                typedef = ArtifactTypeDef(typename, custom_defs)
            else:
                return test_typedef
        return typedef


class DeploymentPath(GraphqlObject):
    """
    type DeploymentPath {
      name: String!
      environment: String!
      project_id: String
      pipelines: [JSON!]
      incremental_deploy: boolean!
    }
    """

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
        project: Project, existing: Optional[str] = None
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


class Resource(GraphqlObject):
    """
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
    """

    url: str
    template: ResourceTemplateName
    status: Optional[Status]
    state: Optional[NodeState]
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


def is_computed(p) -> bool:  # p: Property | PropertyDef
    # XXX be smarter about is_computed() if the user should be able to override the default
    if isinstance(p.schema, Schema):
        metadata = p.schema.metadata
    else:
        metadata = p.schema.get("metadata") or {}
    return (
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
    pending = not instance.status or instance.status == Status.pending
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
        status=instance.status,
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
