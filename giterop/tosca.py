"""
TOSCA implementation

Differences with TOSCA 1.1:

 * Entity type can allow properties that don't need to be declared
 * Added "any" datatype
 * Interface "implementation" values can be a node template name, and the corresponding
   instance will be used to execute the operation.
"""

import toscaparser
from toscaparser.tosca_template import ToscaTemplate
from toscaparser.topology_template import TopologyTemplate
from toscaparser.elements.capabilitytype import CapabilityTypeDef

tosca_ext = """
tosca_definitions_version: tosca_simple_yaml_1_0
node_types:
  giterop.nodes.Configurator:
    derived_from: tosca.nodes.Root
    interfaces:
      Provides:
        type: giterop.interfaces.Provides

  giterop.nodes.Default:
    derived_from: tosca.nodes.Root

interface_types:
  giterop.interfaces.Configurator:
    instantiate:
      description: Coming soon!
    revert:
      description: Coming soon!
    discover:
      description: Coming soon!

  giterop.interfaces.Provides:
    run:
      description: Coming soon!
    step1:
      description: Coming soon!
    step2:
      description: Coming soon!
    step3:
      description: Coming soon!
    step4:
    step5:
    step6:
    step7:
    step8:
    step9:
"""
from ruamel.yaml import YAML
yaml = YAML()
tosca_ext_tpl = yaml.load(tosca_ext)

class ToscaSpec(object):
  ConfiguratorType = 'giterop.nodes.Configurator'

  def __init__(self, toscaDef, instances=None, path=None):

    if "node_templates" in toscaDef:
      # shortcut
      toscaDef = dict(tosca_definitions_version='tosca_simple_yaml_1_0',
                  topology_template=toscaDef)
    else:
      # make sure this is present
      toscaDef['tosca_definitions_version']='tosca_simple_yaml_1_0'

    toscaDef.update(tosca_ext_tpl)
    if instances:
      self.loadInstances(toscaDef, instances)

    # need to set a path for the import loader
    self.template = ToscaTemplate(path=path, yaml_dict_tpl=toscaDef)
    self.nodeTemplates = {}
    self.configurators = {}
    self.relationshipTemplates = {}
    if hasattr(self.template, 'nodetemplates'):
      for template in self.template.nodetemplates:
        nodeTemplate = NodeSpec(template)
        if template.is_derived_from(self.ConfiguratorType):
          # XXX 'configurator-' is a hack
          self.configurators[template.name[len('configurator-'):]
            if template.name.startswith('configurator-') else template.name] = nodeTemplate
        self.nodeTemplates[template.name] = nodeTemplate
    self.topology = TopologySpec(self.template.topology_template)

  def getTemplate(self, name):
    if name == '#topology':
      return self.topology
    return self.nodeTemplates.get(name, self.relationshipTemplates.get(name))

  def loadInstances(self, toscaDef, tpl):
    """

    .. code-block:: YAML

      spec:
            instances:
              test:
                implementations:
                  create: test
            implementations:
              test:
                className:    TestSubtaskConfigurator
                inputs:
"""
    node_templates = toscaDef.setdefault('topology_template',{}).setdefault("node_templates", {})
    for name, impl in tpl.get('implementations', {}).items():
      node_templates['configurator-'+name] = self.loadImplementation(impl)

    for name, impl in tpl.get('instances', {}).items():
      if name not in node_templates and impl is not None:
        node_templates[name] = self.loadInstance(impl)

  # load implementations
  #  create configurator template for each implementation
  def loadImplementation(self, _impl):
    impl = _impl.copy()
    implementation = impl.pop('className')
    impl['parameters'] = impl.pop('inputs', {})
    return dict(type=self.ConfiguratorType,
      properties=impl,
      interfaces={
        'Provides':
          {'run': {
                  'implementation': implementation,
                  }
          }
        })

  def loadInstance(self, impl):
    template = {'type': 'tosca.nodes.Root'} #'giterop.nodes.Default'}
    implementations = impl.get('implementations')
    if implementations:
      template['interfaces'] = {
        'Standard': dict(pair for pair in implementations.items())
      }
    return template

_defaultTopology = TopologyTemplate(dict(
  node_templates = {'_default': {'type': 'tosca.nodes.Root'}},
  relationship_templates= {'_default': {'type': 'tosca.relationships.Root'}}
  ), {})


# represents a node, capability or relationship
class EntitySpec(object):
  def __init__(self, toscaNodeTemplate):
    self.toscaEntityTemplate = toscaNodeTemplate
    self.name = toscaNodeTemplate.name
    # nodes have both properties and attributes
    # as do capability properties and relationships
    # but only property values are declared
    # XXX attribute definitions are found on StatefulEntityType (but does not support default values)
    self.properties = {prop.name: prop.value
        for prop in toscaNodeTemplate.get_properties_objects()}

  def getInterfaces(self):
    return self.toscaEntityTemplate.interfaces

  def isCompatibleTarget(self, targetStr):
    if self.name == targetStr:
      return True
    return self.toscaEntityTemplate.is_derived_from(targetStr)

  def isCompatibleType(self, typeStr):
    return self.toscaEntityTemplate.is_derived_from(typeStr)

  def getUri(self):
    return self.name # XXX

  def __repr__(self):
    return "EntitySpec('%s')" % self.name

class NodeSpec(EntitySpec):
# has attributes: tosca_id, tosca_name, state, (3.4.1 Node States p.61)
  def __init__(self, template=None):
    if not template:
      template = _defaultTopology.nodetemplates[0]
    EntitySpec.__init__(self, template)

class RelationshipSpec(EntitySpec):
# has attributes: tosca_id, tosca_name
  def __init__(self, template=None):
    if not template:
      template = _defaultTopology.relationship_templates[0]
    EntitySpec.__init__(self, template)

class TopologySpec(EntitySpec):
# has attributes: tosca_id, tosca_name, state, (3.4.1 Node States p.61)
  def __init__(self, template=None):
    if not template:
      template = _defaultTopology
    self.toscaEntityTemplate = template
    self.name = '#topology'
    self.properties = {}

# capabilities.Capability isn't an EntityTemplate but duck types with it
class CapabilitySpec(EntitySpec):
  def __init__(self, name=None, nodeTemplate=None):
    if not nodeTemplate:
      self.nodeTemplate = _defaultTopology.nodetemplates[0]
      cap = self.nodeTemplate.get_capabilities_objects()[0]
      name = 'feature'
    else:
      self.nodeTemplate = nodeTemplate
      cap = nodeTemplate.get_capabilities()[name]
    self.type = nodeTemplate.type_definition.get_capabilities().get(name,
          CapabilityTypeDef(name, 'tosca.capabilities.Node', nodeTemplate.type))
    EntitySpec.__init__(self, cap)

  def isCompatibleTarget(self, targetStr):
    if self.name == targetStr:
      return True
    return self.type.is_derived_from(targetStr)

  def isCompatibleType(self, typeStr):
    return self.type.is_derived_from(typeStr)

  def getInterfaces(self):
    # capabilities don't have their own interfaces
    return self.nodeTemplate.interfaces
