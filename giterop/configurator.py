import six
from .util import *

def buildEnum(klass, names):
  [setattr(klass, name, name) for name in names]

class status(object): pass
buildEnum(status, "success failed pending running timeout".split())

class actions(object): pass
buildEnum(actions, "discover instantiate revert".split())

class Configurator(object):
  def __init__(self, configuratorDef):
    pass

  def run(self, job):
    if not self.canRun(job):
      return status.failed
    # no-op
    return status.success

  def isCompatibleWithCurrentState(self, job):
    """
    can we run this action given the previous state?
    e.g. can we upgrade from the previous configuration
    """
    return False

  def canHandleJob(job):
    """Does this configurator support the requested action and parameters?"""
    return True

  def canRun(self, job):
    if not self.canHandleJob(job):
      return False
    if job.previousRun:
      return self.isCompatibleWithCurrentState(job)
    return True

  # does this configurator need to be run?
  def shouldRun(self, job):
    if job.action == 'discover':
      return self.shouldRunDiscover(job)
    elif job.action == 'instantiate':
      return self.shouldRunInstantiate(job)
    elif job.action == 'revert':
      return self.shouldRunRevert(job)
    else:
      assert "unexpected action " + job.action

  def shouldRunInstantiate(self, job):
    if not job.previousRun:
      return True
    return self.hasChanged(job)

  def shouldRunRevert(self, job):
    # XXX revert needs a data loss flag configurations might know whether or not dataloss may happen if reverted
    if not job.previousRun:
      return False
    return True

  def shouldRunDiscover(self, job):
    if job.previousRun:
      #if the version or config has changed since last applied re-run discover
      return not self.hasChanged(job)
    return True

  # if the configuration is different from the last time it was run
  def hasChanged(self, job):
    if not job.previousRun:
      return True
    #compare configuration and parameters
    # XXX
    return job.previousRun.configuration == job.configuration

registerClass(VERSION, "Configurator", Configurator)

class ConfiguratorDefinition(object):
  """
  Configurator:
    name
    version
    class
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
    self.src = defs
    localName = defs.get('name')
    self.name = name or localName
    if not self.name:
      raise GitErOpError('configurator missing name: %s' % defs)
    if localName and name and name != localName:
      raise GitErOpError('configurator names do not match %s and %s' % (name, localName))

    self.apiVersion = defs.get('apiVersion')
    self.kind = defs.get('kind', "Configurator")
    self.parameterSchema = AttributeDefinitionGroup(defs.get('parameterSchema',[]), manifest, validate)
    self.requires = AttributeDefinitionGroup(defs.get('requires',[]), manifest, validate)
    # avoid footgun by having attributes all declared required
    self.requires.merge([dict(name=k,required=True) for k in self.requires.attributes], manifest)
    self.provides = AttributeDefinitionGroup(defs.get('provides',[]), manifest, validate)
    if validate:
      lookupClass(self.kind, self.apiVersion)

  def copy(self, manifest):
    return ConfiguratorDefinition(self.src, manifest, self.name)

  def getDefaults(self):
    return self.parameterSchema.getDefaults()

  def getConfigurator(self):
    return lookupClass(self.kind, self.apiVersion)(self)

  def validateParams(self, params):
    self.parameterSchema.validateParameters(params, True)

  def hasBadParameters(self, params):
    return self.parameterSchema.checkParameters(params, True)

  def findMissingRequirements(self, resource):
    return self.requires.checkParameters(resource.metadata, False)

  def findMissingProvided(self, resource):
    return self.provides.checkParameters(resource.metadata, False)
