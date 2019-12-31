from .runtime import Resource
from .util import UnfurlError, Generate
from .support import Status, NodeState
from .configurator import (
    ConfigurationSpec,
    getConfigSpecArgsFromImplementation,
    TaskRequest,
)

import logging

logger = logging.getLogger("unfurl")


class Plan(object):
    """
  add:  template or unapplied resource
  update: resource
  remove:  resource
  check:   resource
  discover: resource
  """

    @staticmethod
    def getPlanClassForWorkflow(workflow):
        return dict(deploy=DeployPlan, undeploy=UndeployPlan, run=RunNowPlan).get(
            workflow
        )

    def __init__(self, root, toscaSpec, jobOptions):
        self.jobOptions = jobOptions
        self.workflow = jobOptions.workflow
        self.root = root
        self.tosca = toscaSpec
        assert self.tosca
        if jobOptions.template:
            filterTemplate = self.tosca.getTemplate(jobOptions.template)
            if not filterTemplate:
                raise UnfurlError("specified template not found %s" % filterTemplate)
            self.filterTemplate = filterTemplate
        else:
            self.filterTemplate = None

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
        # XXX if requirement with HostedOn relationship, target is the parent not root
        parent = self.findParentResource(template)
        assert parent, "parent should have already been created"
        # Set the initial status of new resources to "pending" instead of defaulting to "unknown"
        return Resource(template.name, None, parent, template, Status.pending)

    def findImplementation(self, interface, operation, template):
        default = None
        for iDef in template.getInterfaces():
            if iDef.iname == interface or iDef.type == interface:
                if iDef.name == operation:
                    return iDef
                if iDef.name == "default":
                    default = iDef
        return default

    def executeDefaultDeploy(self, resource, reason=None, inputs=None):
        # 5.8.5.2 Invocation Conventions p. 228
        # call create
        # for each dependent: call pre_configure_target
        # for each dependency: call pre_configure_source
        # call configure
        # for each dependent: call post_configure_target
        # for each dependency: call post_configure_source
        # call start
        ran = False
        if resource.status in [Status.unknown, Status.notpresent, Status.pending]:
            req = self.generateConfiguration(
                "Standard.create", resource, reason, inputs
            )
            if not req.error:
                resource.state = NodeState.creating
                task = yield req
                if task:
                    ran = True
                    if task.result.success and resource.state == NodeState.creating:
                        # task succeeded but didn't update nodestate
                        resource.state = NodeState.created

        if resource.state == NodeState.created:
            req = self.generateConfiguration(
                "Standard.configure", resource, reason, inputs
            )
            if not req.error:
                resource.state = NodeState.configuring
                task = yield req
                if task:
                    ran = True
                    if task.result.success and resource.state == NodeState.configuring:
                        # task succeeded but didn't update nodestate
                        resource.state = NodeState.configured

        if resource.state == "configured":
            req = self.generateConfiguration("Standard.start", resource, reason, inputs)
            if not req.error:
                resource.state = NodeState.starting
                task = yield req
                if task:
                    ran = True
                    if task.result.success and resource.state == NodeState.starting:
                        # task succeeded but didn't update nodestate
                        resource.state = NodeState.started

        if not ran:
            # if none were selected, run configure (eg. if resource is in a error state)
            yield self.generateConfiguration(
                "Standard.configure", resource, reason, inputs
            )

    def executeDefaultUndeploy(self, resource, reason=None, inputs=None):
        req = self.generateConfiguration("Standard.delete", resource, reason, inputs)
        if not req.error:
            resource.state = NodeState.deleting
        yield req
        # Note: there is no NodeState.deleted and Status.notpresent set by TaskConfig.finished()

    def generateConfiguration(self, operation, resource, reason=None, inputs=None):
        """implementation can either be a named artifact (including a python configurator class),
        configurator node template, or a file path"""
        interface, sep, action = operation.rpartition(".")
        iDef = self.findImplementation(interface, action, resource.template)
        if iDef and iDef.name != "default":
            # merge inputs
            if inputs:
                inputs = dict(iDef.inputs, **inputs)
            else:
                inputs = iDef.inputs
            kw = getConfigSpecArgsFromImplementation(
                iDef.implementation, inputs, self.tosca
            )
            name = "for %s: %s.%s" % (reason, interface, action)
            configSpec = ConfigurationSpec(name, action, **kw)
        else:
            errorMsg = (
                'unable to find an implementation for operation "%s" on node "%s"'
                % (action, resource.template.name)
            )
            configSpec = ConfigurationSpec("#error", action, className=errorMsg)
            reason = "error"

        logger.debug(
            "creating configuration %s with %s to run for %s: %s",
            configSpec.name,
            configSpec.inputs,
            resource.name,
            reason or action,
        )
        return TaskRequest(configSpec, resource, reason or action)

    def generateDeleteConfigurations(self, include):
        for instance in self.root.getOperationalDependencies():
            # reverse to teardown leaf nodes first
            for resource in reversed(instance.descendents):
                # if resource exists (or unknown)
                if resource.status not in [Status.notpresent, Status.pending]:
                    reason = include(resource)
                    if reason:
                        logger.debug("removing instance %s", resource.name)
                        # it's an orphaned config
                        gen = Generate(
                            self._generateConfigurations(resource, reason, "undeploy")
                        )
                        while gen():
                            gen.result = yield gen.next

    def _getDefaultGenerator(self, workflow, resource, reason=None, inputs=None):
        if workflow == "deploy":
            return self.executeDefaultDeploy(resource, reason, inputs)
        elif workflow == "undeploy":
            return self.executeDefaultUndeploy(resource, reason, inputs)
        # elif workflow == 'check'
        return None

    @staticmethod
    def getSuccessStatus(workflow):
        if workflow == "deploy":
            return Status.ok
        elif workflow == "undeploy":
            return Status.notpresent
        return None

    def _generateConfigurations(self, resource, reason, workflow=None):
        workflow = workflow or self.workflow
        # check if this workflow has been delegated to one explicitly declared
        configGenerator = self.executeWorkflow(workflow, resource)
        if not configGenerator:
            configGenerator = self._getDefaultGenerator(workflow, resource, reason)
            if not configGenerator:
                raise UnfurlError("can not get default for workflow " + workflow)

        oldStatus = resource.localStatus
        successes = 0
        failures = 0
        successStatus = self.getSuccessStatus(workflow)
        gen = Generate(configGenerator)
        while gen():
            gen.result = yield gen.next
            task = gen.result
            if not task:  # this was skipped (not shouldRun() or filtered step)
                continue
            if task.configSpec.workflow == workflow and task.target is resource:
                if task.result.success:
                    successes += 1
                    # if task explicitly set the status use that
                    if task.result.status is not None:
                        successStatus = task.result.status
                else:
                    failures += 1

        # note: in ConfigTask.finished():
        # if any task failed and (maybe) modified, target.localStatus will be set to error or unknown
        # if any task succeeded and modified, target.lastStateChange will be set, but not localStatus
        if successStatus is not None and successes and not failures:
            resource.localStatus = successStatus
            if oldStatus != successStatus:
                resource._lastConfigChange = task.changeId

    def executeWorkflow(self, workflowName, resource):
        workflow = self.tosca.getWorkflow(workflowName)
        if not workflow:
            return None
        if workflow.filter(resource):  # check precondition
            return None
        steps = [
            step
            for step in workflow.initialSteps()
            if resource.template.isCompatibleTarget(step.target)
        ]
        if not steps:
            return None
        try:
            # push resource._workflow_inputs
            return self.executeSteps(workflow, steps, resource)
        finally:
            pass  # pop _workflow_inputs

    def executeSteps(self, workflow, steps, resource):
        queue = steps[:]
        while queue:
            step = queue.pop()
            if workflow.filterStep(step, resource):
                continue
            stepGenerator = self.executeStep(step, resource)
            result = None
            try:
                while True:
                    task = stepGenerator.send(result)
                    if isinstance(task, list):  # more steps
                        queue.extend([workflow.getStep(stepName) for stepName in task])
                        break
                    else:
                        result = yield task
            except StopIteration:
                pass

    def executeStep(self, step, resource):
        logging.debug("executing step %s for %s", step.name, resource.name)
        result = None
        for activity in step.activities:
            if activity.type == "inline":
                # XXX inputs
                workflowGenerator = self.executeWorkflow(activity.inline, resource)
                if not workflowGenerator:
                    continue
                gen = Generate(workflowGenerator)
                while gen():
                    gen.result = yield gen.next
                if gen.result:
                    result = gen.result
            elif activity.type == "call_operation":
                result = yield self.generateConfiguration(
                    activity.call_operation,
                    resource,
                    "step:" + step.name,
                    activity.inputs,
                )
            elif activity.type == "set_state":
                resource.state = activity.set_state
            elif activity.type == "delegate":
                # XXX inputs
                configGenerator = self._getDefaultGenerator(activity.delegate, resource)
                if not configGenerator:
                    continue
                gen = Generate(configGenerator)
                while gen():
                    gen.result = yield gen.next
                if gen.result:
                    result = gen.result

            if not result or not result.result.success:
                yield step.on_failure
                break
        else:
            yield step.on_success


class DeployPlan(Plan):
    def includeTask(self, template, resource):
        """Returns whether or not the config should be included in the current job.

        Reasons include: "all", "add", "upgrade", "update", "re-add", 'prune',
        'missing', "config changed", "failed to apply", "degraded", "error".

        Args:
            config (ConfigurationSpec): The :class:`ConfigurationSpec` candidate
            lastChange (Configuration): The :class:`Configuration` representing the that last time
              the given :class:`ConfigurationSpec` was applied or `None`

        Returns:
            (str, ConfigurationSpec): Returns a pair with reason why the task was included
              and the :class:`ConfigurationSpec` to run or `None` if it shound't be included.
    """
        assert template
        jobOptions = self.jobOptions
        oldTemplate = resource.template
        if jobOptions.all:
            return "all", template

        if jobOptions.add and not resource.lastConfigChange:
            # add if it's a new resource
            return "add", template

        # if the specification changed:
        if template != oldTemplate:
            if jobOptions.upgrade:
                return "upgrade", template
            if jobOptions.update:
                # only apply the new configuration if doesn't result in a major version change
                if True:  # XXX if isMinorDifference(template, oldTemplate)
                    return "update", template

        # there isn't a new config to run, see if the last applied config needs to be re-run
        # XXX: if (jobOptions.upgrade or jobOptions.update or jobOptions.all):
        #  if (configTask.hasInputsChanged() or configTask.hasDependenciesChanged()) and
        #    return 'config changed', configTask.configSpec
        return self.checkForRepair(resource)

    def checkForRepair(self, instance):
        jobOptions = self.jobOptions
        assert instance
        lastTemplate = instance.template
        if jobOptions.repair == "none":
            return None
        status = instance.status

        # repair should only apply to configurations that are active and in need of repair
        if status in [Status.unknown, Status.pending]:
            if instance.required:
                status = Status.error  # treat as error
            elif jobOptions.repair == "missing":
                return "missing", lastTemplate
            else:
                return None

        if status not in [Status.degraded, Status.error]:
            return None

        if jobOptions.repair == "degraded":
            assert status > Status.ok, status
            return "degraded", lastTemplate  # repair this
        elif status == Status.degraded:
            assert jobOptions.repair == "error", jobOptions.repair
            return None  # skip repairing this
        else:
            assert jobOptions.repair == "error", "repair: %s status: %s" % (
                jobOptions.repair,
                instance.status,
            )
            return "error", lastTemplate  # repair this

    def _generateConfigurations(self, resource, reason, workflow=None):
        if resource.status == Status.unknown:
            configGenerator = self.executeWorkflow("check", resource)
            if configGenerator:
                gen = Generate(configGenerator)
                while gen():
                    gen.result = yield gen.next

        configGenerator = super(DeployPlan, self)._generateConfigurations(
            resource, reason, workflow
        )
        gen = Generate(configGenerator)
        while gen():
            gen.result = yield gen.next

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
        templates = (
            []
            if not self.tosca.nodeTemplates
            else [
                t
                for t in self.tosca.nodeTemplates.values()
                if not t.isCompatibleType(self.tosca.ConfiguratorType)
                and not t.isCompatibleType(self.tosca.InstallerType)
            ]
        )

        # order by ancestors
        templates = list(
            orderTemplates(
                self.tosca.template.topology_template.graph,
                {t.name: t for t in templates},
                self.filterTemplate and self.filterTemplate.name,
            )
        )

        logger.debug("checking for tasks for templates %s", [t.name for t in templates])
        visited = set()
        for template in templates:
            found = False
            for resource in self.findResourcesFromTemplate(template):
                found = True
                visited.add(id(resource))
                include = self.includeTask(template, resource)
                if include:
                    reason, template = include
                    gen = Generate(self._generateConfigurations(resource, reason))
                    while gen():
                        gen.result = yield gen.next
                else:
                    logger.debug(
                        "skipping task for %s:%s", resource.name, template.name
                    )

            if not found and (opts.add or opts.all):
                reason = "add"
                # XXX create NodeInstance instead to include relationships
                # XXX initial status pending or unknown depending on joboption.check
                resource = self.createResource(template)
                visited.add(id(resource))
                gen = Generate(self._generateConfigurations(resource, reason))
                while gen():
                    gen.result = yield gen.next

            # XXX? retrieve from resource.capabilities
            # for configSpec, oldConfigSpec in getConfigurations(
            #     resource, operation
            # ):  # XXX
            #     if self.includeTask(configSpec, resource, oldConfigSpec):
            #         yield (configSpec, resource, reason or operation)

        if opts.prune:
            check = lambda resource: "prune" if id(resource) not in visited else False
            for taskRequest in self.generateDeleteConfigurations(check):
                yield taskRequest


class UndeployPlan(Plan):
    def executePlan(self):
        """
    yields configSpec, target, reason
    """
        for taskRequest in self.generateDeleteConfigurations(self.includeForDeletion):
            yield taskRequest

    def includeForDeletion(self, resource):
        if self.filterTemplate and resource.template != self.filterTemplate:
            return None
        return "undeploy"


class WorkflowPlan(Plan):
    def executePlan(self):
        """
    yields configSpec, target, reason
    """
        workflow = self.tosca.getWorkflow(self.jobOptions.workflow)
        for step in workflow.initialSteps():
            if self.filterTemplate and not self.filterTemplate.isCompatibleTarget(
                step.target
            ):
                continue
            if self.tosca.isTypeName(step.target):
                templates = self.tosca.findMatchingTemplates(step.target)
            else:
                template = self.tosca.findTemplate(step.target)
                if not template:
                    continue
                templates = [template]

            for template in templates:
                for resource in self.findResourcesFromTemplate(template):
                    gen = self.executeSteps(workflow, [step], resource)
                    result = None
                    try:
                        while True:
                            configuration = gen.send(result)
                            result = yield configuration
                    except StopIteration:
                        pass


class RunNowPlan(Plan):
    def executePlan(self):
        resource = self.root.findResource(self.jobOptions.instance or "root")
        if resource:
            yield self.generateConfiguration("Install.run", resource, "run")


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
        if relation.type == "tosca.relationships.HostedOn":
            for ancestor in getAncestorTemplates(target):
                yield ancestor
            break
    yield source


def findParentTemplate(source):
    for target, relation in source.related.items():
        if relation.type == "tosca.relationships.HostedOn":
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
