import six
from .util import *
from .configuration import *

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
    self.configurations
    self._attributes = None

  @property
  def templates(self):
    return list(self._getTemplates({}).values())

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
        #use the configurator name
        name = component.get('configurator')
        #if configurator is inline:
        if isinstance(name, dict):
          name = name.get('name')
      return (name, component)
    componentDict = dict(map(makeComponents, components))
    # XXX this isn't true anymore! can repeat configurations (test!)
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
