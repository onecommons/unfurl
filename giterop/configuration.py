import six
from .util import *
from .configurator import *

class TemplateDefinition(object):
  """
  A list of configurations and base templates

  Assumes a dictionary with these keys:
    attributes
    configurations
    templates

  Only templates have a name
  """
  def __init__(self, manifest, src, name=None):
    self.manifest = manifest
    self.src = src or {}
    if not isinstance(self.src, dict):
      raise GitErOpError("invalid configuration spec")
    self.name = name
    self._allComponents = None
    #make sure Component objects are created early
    self.localConfigurationDict = self._getLocalConfigurationDict()
    self._attributes = None

  @property
  def templates(self):
    return self._getTemplates({}).values()

  @property
  def attributes(self):
    if self._attributes:
      return self._attributes
    start = {}
    for t in self.templates:
      start.update(t.attributes.attributes)
    ag = AttributeDefinitionGroup(self.src.get('attributes', {}), self.manifest, True, start)
    self._attributes = ag
    return ag

  def _getTemplates(self, seen):
    names = self.src.get(TEMPLATESKEY)
    if not names:
      return seen
    elif isinstance(names, six.string_types):
      names = [names]
    for name in names:
      if name not in seen:
        template = self.manifest.templates.get(name)
        if not template:
          raise GitErOpError("template reference is not defined: %s" % name)
        #depth first
        seen.update(template._getTemplates(seen))
        seen[name] = template
    return seen

  # components explicitly defined on this object
  def _getLocalConfigurationDict(self):
    components = self.src.get('configurations')
    if not components:
      return {}
    def makeComponents(component):
      if isinstance(component, six.string_types):
        component = {'configurator': component}
      name = component.get('name')
      if not name:
        name = component.get('configurator')
        if isinstance(name, dict):
          name = name.get('name')
      return (name, component)
    componentDict = dict(map(makeComponents, components))
    if len(componentDict) != len(components):
      raise GitErOpError("Duplicate names in component list for %s" % self.name)
    return componentDict

  @property
  def configurations(self):
    if self._allComponents is not None:
      return self._allComponents
    # first add the templates' components
    components = []
    localComponents = self.localConfigurationDict
    for t in self.templates:
      for c in t.configurations:
        match = localComponents.pop(c.fqName, None)
        if match:
          # override base configuration with derived
          #replace the ancestor with the decendent
          components.append(Configuration(self.manifest, match, self.name, c))
        else:
          components.append(c)
    components.extend(
      [Configuration(self.manifest, l, self.name) for l in localComponents.values()])
    self._allComponents = components
    return components

class Configuration(object):
  """
  name
  intent: discover instantiate revert
  configurator: ref or inline (use name as ref if omitted)
  parameters
  target
  onfailure #XXX
  """
  def __init__(self, manifest, src, templateName='', base=None):
    self.templateName = templateName
    self.src = src
    self.configurator = self._getConfigurator(manifest)
    if not self.configurator:
      if base:
        self.configurator = base.configurator
      else:
        raise GitErOpError('configuration %s must reference or define a configurator' % self.name)
    if base and base.parameters:
      parameters = base.parameters.copy()
    else:
      parameters = {}
    parameters.update(self.src.get('parameters', {}))
    self.parameters = parameters
    for prop in ['intent', 'target']:
      self._setInheritableProp(prop, base)

  def _setInheritableProp(self, name, base):
    if name in self.src:
      value = self.src[name]
    elif base:
      value = getattr(base, name, None)
    else:
      value = None
    setattr(self, name, value)

  def getResource(self, resource):
    # XXX
    return resource

  def getAction(self, action=None):
    return 'discover' #XXX

  @property
  def name(self):
    return self.src.get('name') or self.configurator.name

  def _getConfigurator(self, manifest):
    # merge spec with local
    specName = self.src.get('configurator')
    if specName:
      if isinstance(specName, six.string_types):
        spec = manifest.configurators.get(specName)
        if not spec:
          raise GitErOpError('configurator not found: %s' % specName)
        return spec
      else:
        #inline spec
        if not isinstance(specName, dict):
          raise GitErOpError('malformed configurator in %s' % self.name)
        return ConfiguratorDefinition(specName, manifest, self.name)
    else:
      return None

  @property
  def fqName(self):
    # what if configuratio in derived templates has a fqname?
    return self.templateName + '.' + self.name

  # hierarchy: componentSpec defaults, componentType defaults and params
  # fully qualify to reference above componentSpec otherwise shadows name
  # error if component references undeclared param
  #we get defaults from componentSpec, update with parent components depth-first
  def getParams(self, merge=None, validate=True):
    defaults = self.configurator.getDefaults()
    defaults.update(self.parameters)
    if merge:
      defaults.update(merge)
    if validate:
      self.configurator.parameters.validateParameters(defaults)
    return defaults
