from .runtime import Resource
from .util import GitErOpError
from .support import Status
from .configurator import ConfigurationSpec

import logging
logger = logging.getLogger('giterop')

class Plan(object):
  """
  create:  template or unapplied resource
  upgrade: resource
  delete:  resource
  check:   resource

  options:
  --append with create to avoid error if exists
  in the future, should run with previous command on this resource or template
  use:configurator use that configurator
  """
  rootConfigurator = None # XXX3

  def __init__(self, root, toscaSpec, jobOptions):
    self.jobOptions = jobOptions
    self.root = root
    self.tosca = toscaSpec
    assert self.tosca

  def findResourcesFromTemplate(self, nodeTemplate):
    for resource in self.root.getSelfAndDescendents():
      if resource.template.name == nodeTemplate.name:
        yield resource

  def findResourceFromTemplate(self, nodeTemplate):
    for resource in self.root.getSelfAndDescendents():
      if resource.template.name == nodeTemplate.name:
        return resource

  def createResource(self, template):
    # XXX create capabilities and requirements too?
    return Resource(template.name, template=template, parent=self.root)

  def getConfigurationSpecFromInterface(self, iDef):
    '''implementation can either be a named artifact (including a python configurator class),
      configurator node template, or a file path'''

    implementation = iDef.implementation
    if isinstance(implementation, dict):
      implementation = implementation.get('primary')
    configuratorTemplate = self.tosca.configurators.get(implementation)
    if configuratorTemplate:
      attributes = configuratorTemplate.properties
      kw = { k : attributes[k] for k in set(attributes) & set(ConfigurationSpec.getDefaults()) }
      if 'parameters' not in kw:
        kw['parameters'] = iDef.inputs
      if 'className' not in kw:
        kw['className'] = configuratorTemplate.getInterfaces()[0].implementation
      return ConfigurationSpec(configuratorTemplate.name, iDef.name, **kw)
    else:
      # for now assume its a configurator class
      # XXX: see if its a artifact, if its a executable file, create a ShellConfigurator
      return ConfigurationSpec(implementation, iDef.name, className=implementation, parameters = iDef.inputs)

  def findImplementation(self, interfaceType, operation, template):
    for iDef in template.getInterfaces():
      if iDef.type == interfaceType and iDef.name == operation:
        return self.getConfigurationSpecFromInterface(iDef)
    return None

  def includeTask(self, template, resource, oldTemplate):
    """ Returns whether or not the config should be included in the current job.

Reasons include: "all", "add", "upgrade", "update", "re-add", 'revert obsolete',
'never applied', "config changed", "failed to apply", "degraded", "error".

Args:
    config (ConfigurationSpec): The :class:`ConfigurationSpec` candidate
    lastChange (Configuration): The :class:`Configuration` representing the that last time
      the given :class:`ConfigurationSpec` was applied or `None`

Returns:
    (str, ConfigurationSpec): Returns a pair with reason why the task was included
      and the :class:`ConfigurationSpec` to run or `None` if it shound't be included.
    """
    jobOptions = self.jobOptions

    assert template or resource
    if jobOptions.all and template:
      return 'all', template
    if template:
      if not resource:
        if jobOptions.add:
          # XXX3 what if config.intent == A.revert? return None?
          return 'add', template
        else:
          return None
      elif resource.status == Status.notapplied and not resource.lastConfigChange and jobOptions.add:
        return 'add', template

    if resource and not template:
      if jobOptions.revertObsolete:
        return 'revert obsolete', oldTemplate
      if jobOptions.all:
        return 'all', oldTemplate
      # use lastAttempt to distinguish between a change that was never applied and one that failed to apply
      if resource.status == Status.notapplied and jobOptions.add:
        return 'never applied', oldTemplate
    elif template != oldTemplate:
      # the user changed the configuration (including parameters):
      if jobOptions.upgrade:
        return 'upgrade', template
      if resource.status == Status.notpresent and jobOptions.add:
        # this case is essentially a re-added config, so re-run it
        return 're-add', template
      if jobOptions.update:
        # apply the new configuration unless it will trigger a major version change
        if False:  # XXX if isMinorDifference(template, oldTemplate)
          return 'update', template

    # XXX
    # there isn't a new config to run, see if the last applied config needs to be re-run
    #assert resource
    #if (lastChange.hasParametersChanged() or lastChange.hasDependenciesChanged()) and (jobOptions.upgrade or jobOptions.update or jobOptions.all):
    #  return 'config changed', lastChange.configurationSpec

    return self.checkForRepair(resource, oldTemplate)

  def checkForRepair(self, lastChange, lastTemplate):
    jobOptions = self.jobOptions

    assert lastChange
    # spec = lastChange.configurationSpec

    if jobOptions.repair=="none":
      return None
    status = lastChange.status
    if status == Status.notapplied and lastChange.required:
      status = Status.error # treat as error

    # repair should only apply to configurations that are active and in need of repair
    # XXX2 what about pending??
    if status not in [Status.degraded, Status.error, Status.notapplied]:
      return None

    if status == Status.notapplied:
      if jobOptions.repair == 'notapplied':
        return 'failed to apply', lastTemplate
      else:
        return None
    if jobOptions.repair == "degraded":
      assert status > Status.ok, status
      return 'degraded', lastTemplate # repair this
    elif status == Status.degraded:
      assert jobOptions.repair == 'error', jobOptions.repair
      return None # skip repairing this
    else:
      assert jobOptions.repair == 'error', "repair: %s status: %s" % (jobOptions.repair, lastChange.status)
      return 'error', lastTemplate # repair this

  def executePlan(self):
    """
    Find candidate tasks

    Given declared spec, current status, and job options, generate selector

    does the config apply to the action?
    is it out of date?
    is it in a ok state?
    has its configuration changed?
    has its dependencies changed?
    are the resources it modifies in need of repair?
    manual override (include / skip)

    yields configSpec, target, reason
    """
    opts = self.jobOptions
    if opts.template:
      template = self.tosca.getTemplate(opts.template)
      if not template:
        raise GitErOpError('template not found %s' % template)
      templates = [template]
    else:
      templates = [] if not self.tosca.nodeTemplates else [
        t for t in self.tosca.nodeTemplates.values()
          if not t.isCompatibleType(self.tosca.ConfiguratorType)]

    visited = set()
    for template in templates:
      resource = self.findResourceFromTemplate(template)
      if resource:
        visited.add(id(resource))
        oldTemplate = template # XXX get the old version of the template
      else:
        oldTemplate = None
      include = self.includeTask(template, resource, oldTemplate)
      if include:
        reason, template = include
        operation = 'create'
        if resource:
          if not resource.status.notapplied and not resource.status.notpresent:
            operation = 'configure'
        else:
          # XXX NodeInstance instead to include relationships
          resource = self.createResource(template)
        yield self.generateConfiguration(operation, resource, reason, opts.useConfigurator)
      else:
        logger.info("skipping config %s:%s", resource.key, template.name)

    if opts.all or opts.revertObsolete:
      for resource in self.root.getSelfAndDescendents():
        if id(resource) not in visited:
          # it's an orphaned config
          include = self.includeTask(None, resource, resource.template)
          if not include:
            continue
          reason, config = include
          yield self.generateConfiguration('delete', resource, reason)
    # #XXX opts.create, opts.append, opts.cmdline, opts.useConfigurator

  def generateConfiguration(self, action, resource, reason=None, cmdLine=None, useConfigurator=None):
    # XXX update joboptions, useConfigurator
    if cmdLine:
      params = dict(command=cmdLine)
      configSpec = ConfigurationSpec('cmdline', action, className='ShellConfigurator', parameters = params)
    else:
      configSpec = self.findImplementation('Standard', action, resource.template)
    if not configSpec:
      raise GitErOpError('unable to find an implementation to "%s" "%s"' % (action, resource.name) )
    return (configSpec, resource, reason or action)

# XXX!:
def buildDependencyGraph():
  """
  We need to find each executed configuration that is affected by a configuration change
  and re-execute them

  dependencies map to inbound edges
  lastConfigChange filters ConfigTasks
  ConfigTasks.configurationResource dependencies are inbound
  keys in ConfigTasks.changes map to a outbound edges to resources

  Need to distinguish between runtime dependencies and configuration dependencies?
  """

def buildConfigChangedExecutionPlan():
  """
  graph = buildDependencyGraph()

  We follow the edges if a resource's attributes or dependencies have changed
  First we validate that we can execute the plan by validating configurationResource in the graph
  If it validates, add to plan

  After executing a task, only follow the dependent outputs that changed no need to follow those dependencies
  """

def validateNode(resource, expectedTemplateOrType):
  """
  First make sure the resource conforms to the expected template (type, properties, attributes, capabilities, requirements, etc.)
  Then for each dependency that corresponds to a required relationship, call validateNode() on those resources with the declared type or template for the relationship
  """

def buildCreateExecutionPlan(self):
  """
  move Configurator nodes to separate list
  start with root node template,
  configuratorTemplate = find Configurator node for creation,
  self.add(self.buildCreateExecutionPlan(configuratorTemplate))
  return # that's all we need, the rest is dynamic, for each new resource, find Configurator for every missing capability and requirement
  """

def buildCreateStaticExecutionPlan(self):
  """
  Build a static plan by using the 'provides' property to estimate what the
  configuration will create and then recursively find missing capabilities and requirements and then the configurators to run
  """
  start = self.buildCreateExecutionPlan(self.rootNodeTemplate)

def buildUpgradeExecutionPlan():
  """
  Same as buildCreateExecutionPlan except look for existing resources
  if it exists, see if it needs upgrading, if it doesn't then find create configurator
  Upgrading:
    compare resource with spec,
    if different add configurators to replace exisiting configuration
    for each requirement not in dependencies, add configurator
    for each dependency on requirement, see if corresponding requirement in spec
      if not, remove dependency
      if it is, call buildUpgradeExecutionPlan on the target resource with the target node type or template
  """

def buildUpdateExecutionPlan():
  """
  Only apply updates that don't change the currently applied spec,
  Starting with the start resource compare deployed artifacts and software nodes associate with it with current template
  and if diffence is no more than a minor version bump,
  retreive the old version of the topology that is associated with the appropriate configuredBy
  and with it try to find and queue a configurator that can apply those changes.

  For each resource dependency, call buildUpdateExecutionPlan().
  """
