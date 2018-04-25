import six
from .util import *

actions = "discover instantiate revert".split()
class Configurator(object):
  def __init__(self, configuratorDefinition):
    self.configuratorDefinition = configuratorDefinition

  # does this need to be run on this?
  # returns yes, no, unsupported state
  def shouldRun(self, action, resource, parameters):
    #configuratorRunStatus = resource.getLastStatus()

    #was run before? if no, do can we handle requested action?
    #if yes, has version or config changed?
    #  if yes, can we update?
    #  if no, did it succeed before?
    return False

  # revert needs a data loss flag configurations might know whether or not dataloss may happen if reverted
  def canRun(self, action, resource, parameters):
    if self.configuratorDefinition.missingRequirements(resource):
      return False
    #configuratorRunStatus = resource.getLastStatus()

    # can we run this action given the previous state?
    # e.g. can we upgrade
    return self.canHandleAction(action, runBefore, self.getState(resource, parameters))

  def run(self, action, resource, parameters):
    if not self.canRun(action, resource, parameters):
      return False
    # configuratorRunStatus = resource.getLastStatus()
    # status = self._run()
    # resource.update(metadata, spec)
    # resource.updateResource(changedResource)
    # resource.addResource(newresource)
    #return status
    return True

  def shouldRunInstantiate(self, resource, parameters):
    configuratorRunStatus = resource.getLastStatus()
    if not runBefore:
      return True
    return not hasntChanged

  def shouldRunRevert(self, resource, parameters):
    configuratorRunStatus = resource.getLastStatus()
    if not runBefore:
      return False
    return True

  def shouldRunDiscover(self, resource, parameters):
    configuratorRunStatus = resource.getLastStatus()
    if runBefore:
      #if the version or config has changed since last applied re-run discover
      return hasntChanged
    return True

class ConfiguratorDefinition(object):
  """
  Configurator:
    name
    version
    class
    parameters
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
    localName = defs.get('name')
    self.name = name or localName
    if not self.name:
      raise GitErOpError('configurator missing name: %s' % defs)
    if localName and name and name != localName:
      raise GitErOpError('configurator names do not match %s and %s' % (name, localName))
    self.apiVersion = defs.get('apiVersion')
    self.kind = defs.get('kind')
    self.parameters = AttributeDefinitionGroup(defs.get('parameters',[]), manifest, validate)
    self.requires = AttributeDefinitionGroup(defs.get('requires',[]), manifest, validate)
    self.provides = AttributeDefinitionGroup(defs.get('provides',[]), manifest, validate)
    #if validate:
    #  lookupClass(self.kind, self.apiVersion)

  def getDefaults(self):
    return self.parameters.getDefaults()

  def getConfigurator(self):
    return lookupClass(self.kind, self.apiVersion)(self)

  def validateParams(self, params):
    self.parameters.validateParameters(params, True)

  def hasBadParameters(self, params):
    return self.parameters.checkParameters(params, True)

  def missingRequirements(self, resource):
    return self.requires.checkParameters(resource.metadata, False)

  def missingExpected(self, resource):
    return self.provides.checkParameters(resource.metadata, False)

# XXX
def _getRef(uri, commitRef=None):
  '''
  current repo
  remote repo
  container image

  Returns: (local file path, commit id)
  '''
