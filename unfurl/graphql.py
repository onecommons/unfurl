from typing import (
    Any,
    Dict,
    Iterator,
    List,
    NewType,
    Optional,
    Tuple,
    cast,
)
from typing_extensions import TypedDict, NotRequired, Required
from .support import Status

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
      instances: ResourceTemplatesByName
      primary_provider: ResourceTemplateName
      repositories: JsonType

class ResourceTypesByName(Dict[TypeName, ResourceType]):
    def __init__(self, qualifier: str=""):
        self.qualifier = qualifier

class GraphqlDB(Dict[str, GraphqlObjectsByName]):
    pass

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
