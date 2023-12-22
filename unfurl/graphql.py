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
from toscaparser.common.exception import ExceptionCollector
from toscaparser.elements.statefulentitytype import StatefulEntityType
from toscaparser.elements.artifacttype import ArtifactTypeDef
from toscaparser.elements.relationshiptype import RelationshipType
from toscaparser.elements.nodetype import NodeType
from .logs import getLogger
from .spec import TopologySpec
from .support import Status

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


class GraphqlDB(Dict[str, GraphqlObjectsByName]):
    def get_types(self) -> ResourceTypesByName:
        return cast(ResourceTypesByName, self["ResourceType"])


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


class Deployments(DeploymentPaths):
    deployments: List[GraphqlDB]
