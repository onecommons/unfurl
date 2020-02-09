"""
TOSCA implementation
"""
from .tosca_plugins import TOSCA_VERSION
from .util import UnfurlValidationError
from .eval import Ref
from .yamlloader import resolveIfInRepository
from toscaparser.tosca_template import ToscaTemplate
from toscaparser.properties import Property
from toscaparser.elements.entity_type import EntityType
import toscaparser.workflow
import toscaparser.imports
import toscaparser.artifacts
from toscaparser.common.exception import ExceptionCollector, ValidationError
import six
import logging

logger = logging.getLogger("unfurl")

from toscaparser import functions


class RefFunc(functions.Function):
    def result(self):
        return {self.name: self.args}

    def validate(self):
        pass


functions.function_mappings["eval"] = RefFunc
functions.function_mappings["ref"] = RefFunc

toscaIsFunction = functions.is_function


def is_function(function):
    return toscaIsFunction(function) or Ref.isRef(function)


functions.is_function = is_function


def createDefaultTopology():
    tpl = dict(
        tosca_definitions_version=TOSCA_VERSION,
        topology_template=dict(
            node_templates={"_default": {"type": "tosca.nodes.Root"}},
            relationship_templates={"_default": {"type": "tosca.relationships.Root"}},
        ),
    )
    return ToscaTemplate(yaml_dict_tpl=tpl)


class ToscaSpec(object):
    ConfiguratorType = "unfurl.nodes.Configurator"
    InstallerType = "unfurl.nodes.Installer"

    def __init__(self, toscaDef, inputs=None, instances=None, path=None):
        if isinstance(toscaDef, ToscaTemplate):
            self.template = toscaDef
        else:
            topology_tpl = toscaDef.get("topology_template")
            if not topology_tpl:
                toscaDef["topology_template"] = dict(
                    node_templates={}, relationship_templates={}
                )
            else:
                for section in ["node_templates", "relationship_templates"]:
                    if not topology_tpl.get(section):
                        topology_tpl[section] = {}

            if instances:
                self.loadInstances(toscaDef, instances)

            logger.info("Validating TOSCA template at %s", path)
            try:
                # need to set a path for the import loader
                self.template = ToscaTemplate(
                    path=path, parsed_params=inputs, yaml_dict_tpl=toscaDef
                )
            except ValidationError:
                message = "\n".join(ExceptionCollector.getExceptionsReport(False))
                raise UnfurlValidationError(
                    "TOSCA validation failed for %s: \n%s" % (path, message),
                    ExceptionCollector.getExceptions(),
                )

        self.nodeTemplates = {}
        self.installers = {}
        self.relationshipTemplates = {}
        if hasattr(self.template, "nodetemplates"):
            for template in self.template.nodetemplates:
                nodeTemplate = NodeSpec(template, self)
                if template.is_derived_from(self.InstallerType):
                    self.installers[template.name] = nodeTemplate
                self.nodeTemplates[template.name] = nodeTemplate

            # user-declared RelationshipTemplates, source and target will be None
            for template in self.template.relationship_templates:
                relTemplate = RelationshipSpec(template)
                self.relationshipTemplates[template.name] = relTemplate

        self.topology = TopologySpec(self.template.topology_template, inputs)
        self.load_workflows()

    def load_workflows(self):
        # we want to let different types defining standard workflows like deploy
        # so we need support importing workflows
        workflows = {
            name: [Workflow(w)]
            for name, w in self.template.topology_template.workflows.items()
        }
        for import_tpl in self.template.nested_tosca_tpls.values():
            importedWorkflows = import_tpl.get("topology_template", {}).get("workflows")
            if importedWorkflows:
                for name, val in importedWorkflows.items():
                    workflows.setdefault(name, []).append(
                        Workflow(toscaparser.workflow.Workflow(name, val))
                    )

        self._workflows = workflows

    def getWorkflow(self, workflow):
        # XXX need api to get all the workflows with the same name
        wfs = self._workflows.get(workflow)
        if wfs:
            return wfs[0]
        else:
            return None

    def getTemplate(self, name):
        if name == "#topology":
            return self.topology
        elif "#c#" in name:
            nodeName, capability = name.split("#c#")
            nodeTemplate = self.nodeTemplates.get(nodeName)
            if not nodeTemplate:
                return None
            return nodeTemplate.getCapability(capability)
        elif "#r#" in name:
            nodeName, requirement = name.split("#r#")
            if nodeName:
                nodeTemplate = self.nodeTemplates.get(nodeName)
                if not nodeTemplate:
                    return None
                return nodeTemplate.getRequirement(requirement)
            else:
                return self.relationshipTemplates.get(name)
        else:
            return self.nodeTemplates.get(name)

    def isTypeName(self, typeName):
        return (
            typeName in self.template.topology_template.custom_defs
            or typeName in EntityType.TOSCA_DEF
        )

    def findMatchingTemplates(self, typeName):
        for template in self.nodeTemplates:
            if template.isCompatibleType(typeName):
                yield template

    def loadInstances(self, toscaDef, tpl):
        """
    Creates node templates for any instances defined in the spec

    .. code-block:: YAML

      spec:
            instances:
              test:
                install: test
            installers:
              test:
                operations:
                  default:
                    implementation: TestConfigurator
                    inputs:
"""
        node_templates = toscaDef["topology_template"]["node_templates"]
        for name, impl in tpl.get("installers", {}).items():
            if name not in node_templates:
                node_templates[name] = dict(type=self.InstallerType, properties=impl)
            else:
                raise UnfurlValidationError(
                    'can not add installer "%s", there is already a node template with that name'
                    % name
                )

        for name, impl in tpl.get("instances", {}).items():
            if name not in node_templates and impl is not None:
                node_templates[name] = self.loadInstance(impl.copy())

    def loadInstance(self, impl):
        if "type" not in impl:
            impl["type"] = "unfurl.nodes.Default"
        installer = impl.pop("install", None)
        if installer:
            impl["requirements"] = [{"install": installer}]
        return impl


_defaultTopology = createDefaultTopology()


def findProps(attributes, attributeDefs, matchfn):
    if not attributes:
        return
    for propdef in attributeDefs.values():
        if propdef.name not in attributes:
            continue
        match = matchfn(propdef.entry_schema_entity or propdef.entity)
        if not propdef.entry_schema and not propdef.entity.properties:
            # it's a simple value type
            if match:
                yield propdef.name, attributes[propdef.name]
            continue

        if not propdef.entry_schema:
            # it's complex datatype
            value = attributes[propdef.name]
            if match:
                yield propdef.name, value
            elif value:
                # descend into its properties
                for name, v in findProps(value, propdef.entity.properties, matchfn):
                    yield name, v
            continue

        properties = propdef.entry_schema_entity.properties
        if not match and not properties:
            # entries are simple value types and didn't match
            continue

        value = attributes[propdef.name]
        if not value:
            continue
        if propdef.type == "map":
            for key, val in value.items():
                if match:
                    yield key, val
                elif properties:
                    for name, v in findProps(val, properties, matchfn):
                        yield name, v
        elif propdef.type == "list":
            for val in value:
                if match:
                    yield None, val
                elif properties:
                    for name, v in findProps(val, properties, matchfn):
                        yield name, v


# represents a node, capability or relationship
class EntitySpec(object):
    def __init__(self, toscaNodeTemplate):
        self.toscaEntityTemplate = toscaNodeTemplate
        self.name = toscaNodeTemplate.name
        self.type = toscaNodeTemplate.type
        # nodes have both properties and attributes
        # as do capability properties and relationships
        # but only property values are declared
        self.attributeDefs = toscaNodeTemplate.get_properties()
        self.properties = {
            prop.name: prop.value for prop in self.attributeDefs.values()
        }
        if toscaNodeTemplate.type_definition:
            # add attributes definitions
            attrDefs = toscaNodeTemplate.type_definition.get_attributes_def()
            self.defaultAttributes = {
                prop.name: prop.default
                for prop in attrDefs.values()
                if prop.default is not None
            }
            for name, aDef in attrDefs.items():
                self.attributeDefs[name] = Property(
                    name, aDef.default, aDef.schema, toscaNodeTemplate.custom_def
                )
        else:
            self.defaultAttributes = {}

    def getInterfaces(self):
        return self.toscaEntityTemplate.interfaces

    def getGroups(self):
        # XXX return the groups this entity is in
        return []

    def isCompatibleTarget(self, targetStr):
        if self.name == targetStr:
            return True
        return self.toscaEntityTemplate.is_derived_from(targetStr)

    def isCompatibleType(self, typeStr):
        return self.toscaEntityTemplate.is_derived_from(typeStr)

    def getUri(self):
        return self.name  # XXX

    def __repr__(self):
        return "%s('%s')" % (self.__class__.__name__, self.name)

    def getArtifact(self, name):
        return None

    @property
    def abstract(self):
        return None

    @property
    def directives(self):
        return []

    def findProps(self, attributes, matchfn):
        for name, val in findProps(attributes, self.attributeDefs, matchfn):
            yield name, val


class NodeSpec(EntitySpec):
    # has attributes: tosca_id, tosca_name, state, (3.4.1 Node States p.61)
    def __init__(self, template=None, spec=None):
        if not template:
            template = _defaultTopology.topology_template.nodetemplates[0]
            self.spec = ToscaSpec(_defaultTopology)
        else:
            assert spec
            self.spec = spec
        EntitySpec.__init__(self, template)
        self._capabilities = None
        self._requirements = None
        self._relationships = None

    def getArtifact(self, name):
        return self.toscaEntityTemplate.artifacts.get(name)

    @property
    def requirements(self):
        if self._requirements is None:
            self._requirements = {}
            nodeTemplate = self.toscaEntityTemplate
            for req, targetNode in zip(
                nodeTemplate.requirements, nodeTemplate.relationships.values()
            ):
                name, values = list(req.items())[0]
                reqSpec = RequirementSpec(name, values, self)
                nodeSpec = self.spec.getTemplate(targetNode.name)
                assert nodeSpec
                nodeSpec.addRelationship(reqSpec)
                self._requirements[name] = reqSpec
        return self._requirements

    def getRequirement(self, name):
        return self.requirements.get(name)

    @property
    def relationships(self):
        """
        returns a list of RelationshipSpecs that are targeting this node template.
        """
        for r in self.toscaEntityTemplate.relationship_tpl:
            assert r.source
            # calling requirement property will ensure the RelationshipSpec is property linked
            self.spec.getTemplate(r.source.name).requirements
        return self._getRelationshipSpecs()

    def _getRelationshipSpecs(self):
        if self._relationships is None:
            # relationship_tpl is a list of RelationshipTemplates that target the node
            self._relationships = [
                RelationshipSpec(r) for r in self.toscaEntityTemplate.relationship_tpl
            ]
        return self._relationships

    def getCapabilityInterfaces(self):
        idefs = [r.getInterfaces() for r in self._getRelationshipSpecs()]
        return [i for elem in idefs for i in elem if i.name != "default"]

    def getRequirementInterfaces(self):
        idefs = [r.getInterfaces() for r in self.requirements.values()]
        return [i for elem in idefs for i in elem if i.name != "default"]

    @property
    def capabilities(self):
        if self._capabilities is None:
            self._capabilities = {
                c.name: CapabilitySpec(self, c)
                for c in self.toscaEntityTemplate.get_capabilities_objects()
            }
        return self._capabilities

    def getCapability(self, name):
        return self.capabilities.get(name)

    def addRelationship(self, reqSpec):
        # find the relationship for this requirement:
        for relSpec in self._getRelationshipSpecs():
            # the RelationshipTemplate should have had the source node assigned by the tosca parser
            # XXX this won't distinguish between more than one relationship between the same two nodes
            # to fix this have the RelationshipTemplate remember the name of the requirement
            if (
                relSpec.toscaEntityTemplate.source
                is reqSpec.parentNode.toscaEntityTemplate
            ):
                assert not reqSpec.relationship or reqSpec.relationship is relSpec
                reqSpec.relationship = relSpec
                assert not relSpec.requirement or relSpec.requirement is reqSpec
                relSpec.requirement = reqSpec
                break
        else:
            raise UnfurlValidationError(
                "relationship not found for requirement %s" % reqSpec.name
            )

        # figure out which capability the relationship targets:
        for capability in self.capabilities.values():
            if reqSpec.isCapable(capability):
                assert reqSpec.relationship
                assert (
                    not reqSpec.relationship.capability
                    or reqSpec.relationship.capability is capability
                )
                reqSpec.relationship.capability = capability
                break
        else:
            raise UnfurlValidationError(
                "capability not found for requirement %s" % reqSpec.name
            )

    @property
    def abstract(self):
        for name in ("select", "substitute"):
            if name in self.toscaEntityTemplate.directives:
                return name
        return None

    @property
    def directives(self):
        return self.toscaEntityTemplate.directives


class RelationshipSpec(EntitySpec):
    def __init__(self, template=None, capability=None, requirement=None):
        # template is a RelationshipTemplate
        # It is a full-fledged entity with a name, type, properties, attributes, interfaces, and metadata.
        # its RelationshipType has valid_target_types (and (hackish) capability_name)
        if not template:
            template = _defaultTopology.topology_template.relationship_templates[0]
        EntitySpec.__init__(self, template)
        self.requirement = requirement
        self.capability = capability

    @property
    def source(self):
        return self.requirement.parentNode if self.requirement else None

    @property
    def target(self):
        return self.capability.parentNode if self.capability else None

    def getUri(self):
        return "#r#" + self.name


class RequirementSpec(object):
    """
    A Requirement shares a Relationship with a Capability.
    """

    def __init__(self, name, req, parent):
        self.source = self.parentNode = parent  # NodeSpec
        self.spec = parent.spec
        self.name = name
        self.requirements_tpl = req
        self.relationship = None
        # requirements_tpl may specify:
        # capability (definition name or type name), node (template name or type name), and node_filter,
        # relationship (template name or type name or inline relationship template)
        # occurrences

    def getArtifact(self, name):
        return self.parentNode.getArtifact(name)

    def getUri(self):
        return self.parentNode.name + "#r#" + self.name

    def getInterfaces(self):
        return self.relationship.getInterfaces() if self.relationship else []

    def isCapable(self, capability):
        # XXX consider self.requirements_tpl.name, capability, node_filter
        if self.relationship:
            t = self.relationship.toscaEntityTemplate.type_definition
            return (
                t.capability_name == capability.name
                or capability.type in t.valid_target_types
            )
        return False


class CapabilitySpec(EntitySpec):
    def __init__(self, parent=None, capability=None):
        if not parent:
            parent = NodeSpec()
            capability = parent.toscaEntityTemplate.get_capabilities_objects()[0]
        self.parentNode = parent
        self.spec = parent.spec
        assert capability
        # capabilities.Capability isn't an EntityTemplate but duck types with it
        EntitySpec.__init__(self, capability)
        self._relationships = None

    def getArtifact(self, name):
        return self.parentNode.getArtifact(name)

    def getInterfaces(self):
        # capabilities don't have their own interfaces
        return self.parentNode.interfaces

    def getUri(self):
        # capabilities aren't standalone templates
        # this is demanagled by getTemplate()
        return self.parentNode.name + "#c#" + self.name

    @property
    def relationships(self):
        return [r for r in self.parentNode.relationships if r.capability is self]


# XXX
# class GroupSpec(EntitySpec):
#  getNodeTemplates() getInstances(), getChildren()


class TopologySpec(EntitySpec):
    # has attributes: tosca_id, tosca_name, state, (3.4.1 Node States p.61)
    def __init__(self, template=None, inputs=None):
        if not template:
            template = _defaultTopology.topology_template
        inputs = inputs or {}

        self.toscaEntityTemplate = template
        self.name = "#topology"
        self.type = "#topology"
        self.inputs = {
            input.name: inputs.get(input.name, input.default)
            for input in template.inputs
        }
        self.outputs = {output.name: output.value for output in template.outputs}
        self.properties = {}
        self.defaultAttributes = {}
        self.attributeDefs = {}

    def getInterfaces(self):
        # doesn't have any interfaces
        return []


class Workflow(object):
    def __init__(self, workflow):
        self.workflow = workflow

    def initialSteps(self):
        preceeding = set()
        for step in self.workflow.steps.values():
            preceeding.update(step.on_success + step.on_failure)
        return [
            step for step in self.workflow.steps.values() if step.name not in preceeding
        ]

    def getStep(self, stepName):
        return self.workflow.steps.get(stepName)

    def matchStepFilter(self, stepName, resource):
        step = self.getStep(stepName)
        if step:
            return all(filter.evaluate(resource.attributes) for filter in step.filter)
        return None

    def matchPreconditions(self, resource):
        for precondition in self.workflow.preconditions:
            target = resource.root.findResource(precondition.target)
            # XXX if precondition.target_relationship
            if not target:
                # XXX target can be a group
                return False
            if not all(
                filter.evaluate(target.attributes) for filter in precondition.condition
            ):
                return False
        return True


class Artifact(object):
    def __init__(self, artifact_tpl, template=None, spec=None, path=None):
        self.spec = template.spec if template else spec
        custom_defs = (
            self.spec and self.spec.template.topology_template.custom_defs or {}
        )
        if isinstance(artifact_tpl, six.string_types):
            artifact = template and template.getArtifact(artifact_tpl)
            if not artifact:
                artifact = toscaparser.artifacts.Artifact(
                    artifact_tpl, dict(file=artifact_tpl), custom_defs, path
                )
        else:
            # inline artifact
            artifact = toscaparser.artifacts.Artifact(
                artifact_tpl["file"], artifact_tpl, custom_defs, path
            )
        self.artifact = artifact
        self.baseDir = (
            artifact._source or (self.spec and self.spec.template.path) or None
        )

    @property
    def file(self):
        return self.artifact.file

    def getPath(self):
        """
      returns path, fragment
      """
        loader = toscaparser.imports.ImportsLoader(
            None, self.baseDir, tpl=self.spec.template.tpl if self.spec else None
        )
        path, isFile, fragment = loader._resolve_import_template(
            None, self.asImportSpec()
        )
        manifest = getattr(loader.tpl, "manifest", None)
        if manifest:
            newpath, f = resolveIfInRepository(manifest, path, isFile, loader)
            return path if f else newpath, fragment
        else:
            return path, fragment

    def asImportSpec(self):
        return dict(file=self.file, repository=self.artifact.repository)
