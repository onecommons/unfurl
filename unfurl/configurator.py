import six
import collections
from .support import Status, Defaults, ResourceChanges
from .result import serializeValue, ChangeAware, Results
from .util import AutoRegisterClass, lookupClass, validateSchema, findSchemaErrors, UnfurlTaskError, toEnum, UnfurlAddingResourceError
from .eval import Ref, mapValue, RefContext
from .runtime import NodeInstance

from ruamel.yaml import YAML
yaml = YAML()

import logging
logger = logging.getLogger('unfurl')

# we want ConfigurationSpec to be standalone and easily serializable
class ConfigurationSpec(object):

  @classmethod
  def getDefaults(self):
      return dict(className=None, majorVersion=0, minorVersion='', intent=Defaults.intent,
      inputs=None, inputSchema=None, preConditions=None, postConditions=None)

  def __init__(self, name, action, className=None, majorVersion=0, minorVersion='',
      intent=Defaults.intent,
      inputs=None, inputSchema=None, preConditions=None, postConditions=None):
    assert name and className, "missing required arguments"
    self.name = name
    self.action = action
    self.className = className
    self.majorVersion = majorVersion
    self.minorVersion = minorVersion
    self.intent = intent
    self.inputs = inputs or {}
    self.inputSchema = inputSchema or {}
    self.preConditions = preConditions
    self.postConditions = postConditions

  def findInvalidateInputs(self, inputs):
    return findSchemaErrors(serializeValue(inputs), self.inputSchema)

  # XXX same for postConditions
  def findInvalidPreconditions(self, target):
    if not self.preConditions:
      return []
    # XXX this should be like a Dependency object
    expanded = serializeValue(target.attributes)
    return findSchemaErrors(expanded, self.preConditions)

  def create(self):
    # XXX2 throw clearer exception if couldn't load class
    return lookupClass(self.className)(self)

  def shouldRun(self):
    return Defaults.shouldRun

  def copy(self, **mods):
    args = self.__dict__.copy()
    args.update(mods)
    return ConfigurationSpec(**args)

  def __eq__(self, other):
    if not isinstance(other, ConfigurationSpec):
      return False
    # XXX3 add unit tests
    return (self.name == other.name and self.className == other.className
      and self.majorVersion == other.majorVersion and self.minorVersion == other.minorVersion
      and self.intent == other.intent and self.inputs == other.inputs and self.inputSchema == self.inputSchema
      and self.preConditions == other.preConditions and self.postConditions == other.postConditions)

class ConfiguratorResult(object):
  """
  If applied is True,
  the current pending configuration is set to the effective, active one
  and the previous configuration is no longer in effect.

  Modified indicates whether the underlying state of configuration,
  was changed i.e. the physically altered the system this configuration represents.

  Readystate reports the Status of the current configuration.
  """
  def __init__(self, applied, modified, readyState=None, configChanged=None, result=None):
    self.applied = applied
    self.modified = modified
    self.readyState = readyState
    self.configChanged = configChanged
    self.result = result

  def __str__(self):
    result = '' if self.result is None else str(self.result)
    return 'changes: ' + (' '.join(filter(None,[
      self.applied and 'applied',
      self.modified and 'modified',
      self.readyState and self.readyState.name,
      self.configChanged and 'config'])) or 'none') + ' ' + result

@six.add_metaclass(AutoRegisterClass)
class Configurator(object):
  def __init__(self, configurationSpec):
    self.configSpec = configurationSpec

  # yields a JobRequest, TaskRequest or a ConfiguratorResult
  def run(self, task):
    yield task.createResult(False, False)

  def dryRun(self, task):
    yield task.createResult(False, False)

  def cantRun(self, task):
    """
    Does this configurator support the requested action and parameters
    given the current state of the resource?
    (e.g. can we upgrade from the previous configuration?)

    Returns False or an error message (list or string)
    """
    return False

  def shouldRun(self, task):
    """Does this configuration need to be run?"""
    return self.configSpec.shouldRun()

  # XXX3 should be called during when checking dependencies
  # def checkConfigurationStatus(self, task):
  #   """Is this configuration still valid?"""
  #   return Status.ok

class TaskView(object):
  """
  The interface presented to configurators.
  """
  def __init__(self, manifest, configSpec, target, reason = None, dependencies=None):
    # public:
    self.configSpec = configSpec
    self.target = target
    self.reason = reason
    # XXX refcontext should include TARGET HOST etc.
    self.inputs = mapValue(self.configSpec.inputs, target)
    # private:
    self._manifest = manifest
    self.messages = []
    self._addedResources = []
    self._dependenciesChanged = False
    self.dependencies = dependencies or {}
    self._resourceChanges = ResourceChanges()

  def addMessage(self, message):
    self.messages.append(message)

  def findResource(self, name):
     return self._manifest.getRootResource().findResource(name)

  def createResult(self, applied, modified, readyState=None, configChanged=None, result=None):
    readyState = toEnum(Status, readyState)
    if applied and (not readyState or readyState == Status.notapplied):
        raise UnfurlTaskError(self, "need to set readyState if configuration was applied")

    return ConfiguratorResult(applied, modified, readyState, configChanged, result)

  # updates can be marked as dependencies (changes to dependencies changed) or required (error if changed)
  # configuration has cumulative set of changes made it to resources
  # updates update those changes
  # other configurations maybe modify those changes, triggering a configuration change
  def query(self, query, dependency=False, name=None, required=False, wantList=False, resolveExternal=True):
    # XXX refcontext should include TARGET HOST etc.
    result = Ref(query).resolve(RefContext(self.target, resolveExternal=resolveExternal), wantList)
    if dependency:
      self.addDependency(query, result, name=name, required=required, wantList=wantList)
    return result

  def addDependency(self, expr, expected=None, schema=None, name=None, required=False, wantList=False):
    getter = getattr(expr, 'asRef', None)
    if getter:
      # expr is a configuration or resource or ExternalValue
      expr = Ref(getter()).source

    dependency = Dependency(expr, expected, schema, name, required, wantList)
    self.dependencies[name or expr] = dependency
    self.dependenciesChanged = True
    return dependency

  def removeDependency(self, name):
    old = self.dependencies.pop(name, None)
    if old:
      self.dependenciesChanged = True
    return old

  def createConfigurationSpec(self, name, configSpec):
    if isinstance(configSpec, six.string_types):
      configSpec = yaml.load(configSpec)
    return self.manifest.loadConfigSpec(name, configSpec)

  def createSubTask(self, configSpec, resource=None, persist=False, required=False):
    from .job import TaskRequest
    # XXX:
    #if persist or required:
    #  expr = "::%s::.configurations::%s" % (configSpec.target, configSpec.name)
    #  self.addDependency(expr, required=required)

    if resource is None:
      resource = self.target
    return TaskRequest(configSpec, resource, persist, required)

  # # XXX how???
  # # Configurations created by subtasks are transient insofar as the are not part of the spec,
  # # but they are recorded as part of the resource's configuration state.
  # # Marking as persistent or required will create a dependency on the new configuration.
  # # XXX3 have a way to update spec attributes to trigger config updates e.g. add dns entries via attributes on a dns
  # def createSubTask(self, configSpec, persist=False, required=False):
  #   if persist or required:
  #     expr = "::%s::.configurations::%s" % (configSpec.target, configSpec.name)
  #     self.addDependency(expr, required=required)
  #   return TaskRequest(configSpec, persist, required)
  #
  # # XXX how can we explicitly associate relations with target resources etc.?
  # # through capability attributes and dependencies/relationship attributes
  def updateResources(self, resources):
    """
    Either a list or string that is parsed as YAML
    Operational state indicates if it current exists or not
    Will instantiate a new job, yield the return value to run that job right away

    .. code-block:: YAML

      - name:
        template: # name of node template
        priority: required
        dependent: boolean
        parent:
        attributes:
        status:
          readyState: ok
    """
    from .manifest import Manifest
    from .job import JobRequest
    if isinstance(resources, six.string_types):
      resources = yaml.load(resources)

    errors = []
    newResources = []
    newResourceSpecs = []
    for resourceSpec in resources:
      originalResourceSpec = resourceSpec
      try:
        rname = resourceSpec['name']
        if rname == '.self':
          existingResource = self.target
        else:
          existingResource = self.findResource(rname)
        if existingResource:
          # XXX2 if spec is defined (not just status), there should be a way to
          # indicate this should replace an existing resource or throw an error
          status = resourceSpec.get('status')
          operational = Manifest.loadStatus(status)
          if operational.localStatus:
            existingResource.localStatus = operational.localStatus
          attributes = resourceSpec.get('attributes')
          if attributes:
            for key, value in mapValue(attributes, existingResource).items():
               existingResource.attributes[key] = value
               logger.debug('setting attribute %s with %s on %s', key, value, existingResource.name)
          logger.info("updating resources %s", existingResource.name)
          continue

        resource = self.manifest.loadResource(rname, resourceSpec, parent=self.target.root)

        if resource.required or resourceSpec.get('dependent'):
          self.addDependency(resource, required=resource.required)
      except:
        errors.append(UnfurlAddingResourceError(self, originalResourceSpec))
      else:
        newResourceSpecs.append(originalResourceSpec)
        newResources.append(resource)

    if newResourceSpecs:
      self._resourceChanges.addResources(newResourceSpecs)
      self._addedResources.extend(newResources)
      logger.info("add resources %s", newResources)

      jobRequest = JobRequest(newResources, errors)
      if self.job:
        self.job.jobRequestQueue.append(jobRequest)
      return jobRequest
    return None

class Dependency(ChangeAware):
  """
  Represents a runtime dependency for a configuration.

  Dependencies are used to determine if a configuration needs re-run as follows:

  * They are dynamically created when evaluating and comparing the configuration spec's attributes with the previous
    values

  * Persistent dependencies can be created when the configurator invoke these apis: `createConfiguration`, `addResources`, `query`, `addDependency`
  """

  def __init__(self, expr, expected=None, schema=None, name=None, required=False, wantList=False):
    """
    if schema is not None, validate the result using schema
    if expected is not None, test that result equals expected
    otherwise test that result isn't empty has not changed since the last attempt
    """
    assert not (expected and schema)
    self.expr = expr

    self.expected = expected
    self.schema = schema
    self.required = required
    self.name = name
    self.wantList= wantList

  def refresh(self, config):
    if self.expected is not None:
      changeId = config.changeId
      context = RefContext(config.target, dict(val=self.expected, changeId=changeId))
      result = Ref(self.expr).resolve(context, wantList=self.wantList)
      self.expected = result

  @staticmethod
  def hasValueChanged(value, changeset):
    if isinstance(value, Results):
      return Dependency.hasValueChanged(value._attributes, changeset)
    elif isinstance(value, collections.Mapping):
      if any(Dependency.hasValueChanged(v, changeset) for v in value.values()):
        return True
    elif isinstance(value, (collections.MutableSequence, tuple)):
      if any(Dependency.hasValueChanged(v, changeset) for v in value):
        return True
    elif isinstance(value, ChangeAware):
        return value.hasChanged(changeset)
    else:
      return False

  def hasChanged(self, config):
    changeId = config.changeId
    context = RefContext(config.target, dict(val=self.expected, changeId=changeId))
    result = Ref(self.expr).resolveOne(context) #resolve(context, self.wantList)

    if self.schema:
      # result isn't as expected, something changed
      if not validateSchema(result, self.schema):
        return False
    else:
      if self.expected is not None:
        expected = mapValue(self.expected, context)
        if result != expected:
          logger.debug("hasChanged: %s != %s", result, expected)
          return True
      elif not result:
        # if expression no longer true (e.g. a resource wasn't found), then treat dependency as changed
        return True

    if self.hasValueChanged(result, config):
      return True

    return False
