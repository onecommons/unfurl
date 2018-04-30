import six
from .util import *
from .configurator import *

class Configuration(object):
  """
  name
  intent: discover instantiate revert
  configurator: ref or inline (use name as ref if omitted)
  parameters
  target    #XXX
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
      parameters = dict(base.parameters)
    else:
      parameters = {}
    parameters.update(self.src.get('parameters', {}))
    self.parameters = parameters
    for prop in ['intent']: #, 'target']:
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
    parameterSchemaOverrides = self.src.get('parameterSchema')
    if specName:
      if isinstance(specName, six.string_types):
        spec = manifest.configurators.get(specName)
        if not spec:
          raise GitErOpError('configurator not found: %s' % specName)
        if parameterSchemaOverrides:
          spec = spec.copy(manifest)
          spec.parameterSchema.merge(parameterSchemaOverrides, manifest)
      else:
        #inline spec
        if not isinstance(specName, dict):
          raise GitErOpError('malformed configurator in %s' % self.name)
        spec = ConfiguratorDefinition(specName, manifest, self.name)
        if parameterSchemaOverrides:
          spec.parameterSchema.merge(parameterSchemaOverrides, manifest)
      return spec
    else:
      return None

  @property
  def fqName(self):
    # XXX what if configuration in a derived templates already has a fqname?
    return self.templateName + '.' + self.name

  # hierarchy: componentSpec defaults, componentType defaults and params
  # fully qualify to reference above componentSpec otherwise shadows name
  # error if component references undeclared param
  #we get defaults from componentSpec, update with parent components depth-first
  def getParams(self, currentResource = None, merge=None, validate=True):
    defaults = self.configurator.getDefaults()
    defaults.update(self.parameters)
    if merge:
      defaults.update(merge)
    if validate:
      self.configurator.parameterSchema.validateParameters(defaults, currentResource)
    if currentResource:
      dict((k, ValueFrom.resolveIfRef(value, currentResource)) for (k, v) in defaults.iteritems())
    else:
      return defaults
