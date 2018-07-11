import six
import hashlib
import json
from .attributes import *

def buildEnum(klass, names):
  [setattr(klass, name, name) for name in names]

"""
3 ways to track changes to a configurator:
version controlled by configurator
revision automatically incremented GitErOp on changes
current git commit recorded when configurator changes
"""
class Configurator(object):
  class status(object): pass
  buildEnum(status, "success failed pending running timeout".split())

  class actions(object): pass
  buildEnum(actions, "discover instantiate revert".split())

  def __init__(self, configuratorDef):
    pass

  def getVersion(self):
    return 0

  def getCustomProperties(self):
    return {}

  def run(self, task):
    if not self.canRun(task):
      return self.status.failed
    # no-op
    return self.status.success

  def canRun(self, task):
    """
    Does this configurator support the requested action and parameters
    given the current state of the resource
    (e.g. can we upgrade from the previous configuration?)
    """
    return True

  # does this configurator need to be run?
  def shouldRun(self, task):
    if task.action == 'discover':
      return self.shouldRunDiscover(task)
    elif task.action == 'instantiate':
      return self.shouldRunInstantiate(task)
    elif task.action == 'revert':
      return self.shouldRunRevert(task)
    else:
      assert "unexpected action " + task.action

  def shouldRunInstantiate(self, task):
    if not task.previousRun:
      return True
    if previousRun.action == 'revert':
      return True
    return self.hasChanged(task)

  def shouldRunRevert(self, task):
    # XXX revert needs a data loss flag configurations might know whether or not dataloss may happen if reverted
    if not task.previousRun:
      return False
    return True

  def shouldRunDiscover(self, task):
    if task.previousRun:
      #if the version or config has changed since last applied re-run discover
      return self.hasChanged(task)
    return True

  # if the configuration is different from the last time it was run
  def hasChanged(self, task):
    if not task.previousRun:
      return True

    # previousRun is a ChangeRecord
    if task.previousRun.configuration.get('digest') != task.configuration.digest():
      return True

registerClass(VERSION, "Configurator", Configurator)

class ConfiguratorDefinition(object):
  """
  Configurator:
    apiVersion
    kind
    name
    version
    parameterSchema:
      - name: my_param # secret_
        ref: type or connection or cluster or app
        #XXX type: enum
        #XXX enum: ['X', 'Y', 'Z']
        required: True
        default:
          clusterkms: name
    src
    requires
    provides
  """
  def __init__(self, defs, manifest, name=None, validate=True):
    if not isinstance(defs, dict):
      raise GitErOpError('malformed configurator %s' % defs)
    self.definition = defs
    localName = defs.get('name')
    self.name = name or localName
    if not self.name:
      raise GitErOpError('configurator missing name: %s' % defs)
    if localName and name and name != localName:
      raise GitErOpError('configurator names do not match %s and %s' % (name, localName))

    self.apiVersion = defs.get('apiVersion', VERSION)
    self.kind = defs.get('kind', "Configurator")
    self.version = defs.get('version', 0)
    self.parameterSchema = AttributeDefinitionGroup(defs.get('parameterSchema',[]), manifest, validate)
    self.requires = AttributeDefinitionGroup(defs.get('requires',[]), manifest, validate)
    # avoid footgun by having attributes all declared required
    self.requires.merge([dict(name=k,required=True) for k in self.requires.attributes], manifest)
    self.provides = AttributeDefinitionGroup(defs.get('provides',[]), manifest, validate)
    if validate:
      lookupClass(self.kind, self.apiVersion)

  def copy(self, manifest):
    return ConfiguratorDefinition(self.definition, manifest, self.name)

  def getDefaults(self):
    return self.parameterSchema.getDefaults()

  def getConfigurator(self):
    return lookupClass(self.kind, self.apiVersion)(self)

  def validateParams(self, params, resolver=None):
    self.parameterSchema.validateParameters(params, resolver, True)

  def hasBadParameters(self, params, resolver=None):
    return self.parameterSchema.checkParameters(params, resolver, True)

  def findMissingRequirements(self, resource, resolver=None):
    return self.requires.checkParameters(resource.metadata, resolver, False)

  def findMissingProvided(self, resource, resolver=None):
    return self.provides.checkParameters(resource.metadata, resolver, False)

  def digest(self):
    m = hashlib.sha256()
    m.update(json.dumps(self.configurator.getCanonicalConfig()))
    return m.hexdigest()

  def getCanonicalConfig(self):
    # excludes name, parameterSchema, provides, requires
    configurator = self.getConfigurator()
    return [self.apiVersion, self.kind, self.version, configurator.getVersion(),
      [(key, self.definition.get(key))
        for key in sorted(configurator.getCustomProperties().keys())]
    ]

  def compare(self, other):
    return self.getCanonicalConfig() == other.getCanonicalConfig()
