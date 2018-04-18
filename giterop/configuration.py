import six
from .util import *
from .configurator import *

class ConfigurationSpec(object):
  """
  A list of configurations and base templates

  Assumes a dictionary with two keys:
    configuration
    template

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
    self.localComponentsDict = self._getLocalConfigurationDict()

  @property
  def templates(self):
    return self._getTemplates({}).values()

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
        component = {'name': component}
      return (component['name'], Configuration(component, self))
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
    localComponents = self.localComponentsDict
    for t in self.templates:
      for c in t.configurations:
        match = localComponents.pop(c.fqName, None)
        if match:
          #replace the ancestor with the decendent
          match.ancestors.append(c)
          components.append(match)
        else:
          components.append(c)
    components.extend(localComponents.values())
    self._allComponents = components
    return components

class Configuration(object):
  """
  name
  spec: ref or inline (use name as ref if omitted)
  parameters
  action
  onfailure
  """
  def __init__(self, localDef, parent):
    self.name = localDef['name']
    # parent is ConfigurationSpec
    self.parent = parent
    self.localDef = localDef
    self.ancestors = []
    self._spec = None

  # merge components before getting spec because spec can change with merge
  @property
  def spec(self):
    # merge spec with local
    if self._spec:
      return self._spec
    specName = self.localDef.get('spec')
    if specName:
      if isinstance(specName, six.string_types):
        spec = self.parent.manifest.configurators.get(specName)
        if not spec:
          raise GitErOpError('configurator not found: %s', specName)
        return spec
      else:
        #inline spec
        if not isinstance(specName, dict):
          raise GitErOpError('malformed spec in %s' % self.name)
        self._spec = ConfiguratorSpec(self, specName)
        return self._spec
    else:
      self.parent.configurations #call this to make sure ancestors has been calculated
      if not self.ancestors:
        raise GitErOpError('configuration %s must reference or define a spec' % self.name)
      self._spec = self.ancestors[-1].spec
      return self._spec
    raise GitErOpError('configuration %s must reference or define a spec' % self.name)

  @property
  def connectionType(self):
    return (self.manifest.get('connection')
      or self.componentType.getConnectionType(self)
      or defaultConnectionType(self.phase))

  @property
  def fqName(self):
    return self.parent.name + '.' + self.name

  def _getInheritedProp(self, name):
    return self.localDef.get(name) or self.ancestors and getattr(self.ancestors[-1], name)

  @property
  def action(self):
    return self._getInheritedProp('action')

  @property
  def onfailure(self):
    return self._getInheritedProp('onfailure')

  def diff(self, other):
    '''
    Compare name, params and action value and commit id
    '''
    return False #XXX

  def __eq__(self, other):
    if self is other:
      return True
    return not not self.diff(other)

  def apply(self, action='install', params=None):
    # XXX action
    findComponentType(self).run(action, params)

  @property
  def params(self):
    #XXX resolve references in values etc.
    return self.localDef.get('parameters', {})

  # hierarchy: componentSpec defaults, componentType defaults and params
  # fully qualify to reference above componentSpec otherwise shadows name
  # error if component references undeclared param
  #we get defaults from componentSpec, update with parent components depth-first
  def getParams(self, merge=None, validate=True):
    defaults = self.spec.getDefaults()
    #depth first list so closed ancestor is updated last
    [defaults.update(ancestor.params) for ancestor in self.ancestors]
    defaults.update(self.params)
    if merge:
      defaults.update(merge)
    if validate:
      self.spec.validateParams(defaults)
    return defaults
