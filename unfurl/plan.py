import six
from .runtime import Resource
from .util import UnfurlError, lookupClass
from .support import Status
from .configurator import ConfigurationSpec

import logging
logger = logging.getLogger('unfurl')

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

  def findParentResource(self, source):
    parentTemplate = findParentTemplate(source.toscaEntityTemplate)
    if not parentTemplate:
      return self.root
    for parent in self.findResourcesFromTemplate(parentTemplate):
      # XXX need to evaluate matches
      return parent

  def createResource(self, template):
    # XXX create capabilities and requirements too?
    # XXX if requirement with HostedOn relationshio, target is the parent not root
    parent = self.findParentResource(template)
    assert parent, "parent should have already been created"
    return Resource(template.name, template=template, parent=parent)

  def createShellConfigurator(self, cmdLine, action, inputs=None, timeout=None):
      params = dict(command=cmdLine, timeout=timeout)
      if inputs:
        params.update(inputs)
      return ConfigurationSpec('cmdline', action, className='unfurl.shellconfigurator.ShellConfigurator', inputs = params)

  def getConfigurationSpecFromInterface(self, iDef):
    '''implementation can either be a named artifact (including a python configurator class),
      configurator node template, or a file path'''
    implementation = iDef.implementation
    timeout = None
    if isinstance(implementation, dict):
      timeout = implementation.get('timeout')
      implementation = implementation.get('primary')
      if isinstance(implementation, dict):
        # it's an artifact definition
        # XXX retrieve from repository if defined
        implementation = implementation.get('file')
    if not implementation or not isinstance(implementation, six.string_types):
      raise UnfurlError('invalid implementation spec %s' % implementation)
    configuratorTemplate = self.tosca.configurators.get(implementation)
    if configuratorTemplate:
      attributes = configuratorTemplate.properties
      kw = { k : attributes[k] for k in set(attributes) & set(ConfigurationSpec.getDefaults()) }
      if 'inputs' not in kw:
        kw['inputs'] = iDef.inputs
      if 'className' not in kw:
        kw['className'] = configuratorTemplate.getInterfaces()[0].implementation
      return ConfigurationSpec(configuratorTemplate.name, iDef.name, **kw)
    else:
      try:
        lookupClass(implementation)
        return ConfigurationSpec(implementation, iDef.name, className=implementation, inputs = iDef.inputs)
      except UnfurlError:
        # assume its executable file, create a ShellConfigurator
        return self.createShellConfigurator([implementation], iDef.name, iDef.inputs, timeout=timeout)

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
    assert resource
    if jobOptions.all and template:
      return 'all', template
    if template and resource.status == Status.notapplied and not resource.lastConfigChange and jobOptions.add:
        return 'add', template

    if not template:
      if jobOptions.revertObsolete:
        return 'revert obsolete', oldTemplate
      if jobOptions.all:
        return 'all', oldTemplate
      if resource.status == Status.notapplied and jobOptions.add:
        return 'never applied', oldTemplate
    elif template != oldTemplate:
      # the user changed the configuration:
      if jobOptions.upgrade:
        return 'upgrade', template
      if resource.status == Status.notpresent and jobOptions.add:
        # this case is essentially a re-added config, so re-run it
        return 're-add', template
      if jobOptions.update:
        # apply the new configuration unless it will trigger a major version change
        if False:  # XXX if isMinorDifference(template, oldTemplate)
          return 'update', template

    # there isn't a new config to run, see if the last applied config needs to be re-run
    # XXX: if (jobOptions.upgrade or jobOptions.update or jobOptions.all):
    #  if (configTask.hasInputsChanged() or configTask.hasDependenciesChanged()) and
    #    return 'config changed', configTask.configSpec
    return self.checkForRepair(resource, oldTemplate)

  def checkForRepair(self, instance, lastTemplate):
    jobOptions = self.jobOptions

    assert instance
    # spec = lastChange.configurationSpec

    if jobOptions.repair=="none":
      return None
    status = instance.status
    if status == Status.notapplied and instance.required:
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
      assert jobOptions.repair == 'error', "repair: %s status: %s" % (jobOptions.repair, instance.status)
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
    templates = [] if not self.tosca.nodeTemplates else [
      t for t in self.tosca.nodeTemplates.values()
        if not t.isCompatibleType(self.tosca.ConfiguratorType)]

    if opts.template:
      filterTemplate = self.tosca.getTemplate(opts.template)
      if not filterTemplate:
        raise UnfurlError('specified template not found %s' % filterTemplate)
    else:
      filterTemplate = None

    # order by ancestors
    templates = list(orderTemplates(
        self.tosca.template.topology_template.graph,
        {t.name : t for t in templates}, filterTemplate and filterTemplate.name))

    logger.debug('checking for tasks for templates %s', [t.name for t in templates])
    visited = set()
    for template in templates:
      found = False
      for resource in self.findResourcesFromTemplate(template):
        found = True
        visited.add(id(resource))
        include = self.includeTask(template, resource, resource.template)
        if include:
          reason, template = include
          if resource.status.notapplied or resource.status.notpresent:
            operation = 'create'
          else:
            operation = 'configure'
          yield self.generateConfiguration(operation, resource, reason, opts.useConfigurator)
        else:
          logger.debug('skipping task for %s:%s', resource.name, template.name),

      if not found and (opts.add or opts.all):
        reason = 'add'
        operation = 'create'
        # XXX create NodeInstance instead to include relationships
        resource = self.createResource(template)
        visited.add(id(resource))
        yield self.generateConfiguration(operation, resource, reason, opts.useConfigurator)

    if opts.revertObsolete: #XXX expose option in cli (as --prune ?)
      for instance in self.root.getOperationalDependencies():
        for resource in instance.getSelfAndDescendents():
          if id(resource) not in visited:
            logger.debug('checking for tasks for orphaned resource %s', resource.name)
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
      configSpec = self.createShellConfigurator(cmdLine, action)
    else:
      configSpec = self.findImplementation('Standard', action, resource.template)
    if not configSpec:
      raise UnfurlError('unable to find an implementation to "%s" "%s" on ""%s"' % (action, resource.template.name, resource.template.name) )
    logger.debug('creating configuration %s with %s to run for %s: %s', configSpec.name, configSpec.inputs, resource.name, reason or action)
    return (configSpec, resource, reason or action)

def orderTemplates(graph, templates, filter=None):
  seen = set()
  for source in graph:
    if filter and source.name != filter:
      continue
    if source in seen:
      continue
    for ancestor in getAncestorTemplates(source):
      if ancestor in seen:
        continue
      seen.add(ancestor)
      template = templates.get(ancestor.name)
      if template:
        yield template

def getAncestorTemplates(source):
  for target, relation in source.related.items():
    if relation.type == 'tosca.relationships.HostedOn':
      for ancestor in getAncestorTemplates(target):
        yield ancestor
      break
  yield source

def findParentTemplate(source):
  for target, relation in source.related.items():
    if relation.type == 'tosca.relationships.HostedOn':
      return target
    return None


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
