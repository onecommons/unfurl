import six
import hashlib
import json
from .util import *
from .configurator import *

class Configuration(object):
  """
  name
  intent: discover instantiate revert
  configurator: ref or inline (use name as ref if omitted)
  parameters
  target    #XXX
  when: evaluates to true, (default: true)
  # applies to current resource, but causes cascade
  # continue, abort, rollback
  # default: abort if failed but no changes made
  # rollback if failed and dirty
  # abortOnFailure: none, run, resource
  onfailure: abort, continue, revert #note: when action=revert always abort
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
    defaults = self.mergeDefaults(self.configurator, self.parameters)
    if merge:
      defaults.update(merge)
    if validate:
      self.configurator.parameterSchema.validateParameters(defaults, currentResource)
    if currentResource:
      dict((k, ValueFrom.resolveIfRef(value, currentResource)) for (k, v) in defaults.items())
    else:
      return defaults

  @staticmethod
  def mergeDefaults(configurator, parameters):
    defaults = configurator.getDefaults()
    defaults.update(parameters)
    return defaults

  def digest(self):
    m = hashlib.sha256()
    m.update(json.dumps(self.getCanonicalConfig()).encode("utf-8"))
    return m.hexdigest()

  def getCanonicalConfig(self):
    #excludes action and name
    config = self.configurator.getCanonicalConfig()
    params = self.mergeDefaults(self.configurator, self.parameters)
    config.append( sorted(params.items(), key=lambda t: t[0]) )
    return config
