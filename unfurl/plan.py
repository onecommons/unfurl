# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import six
from .runtime import NodeInstance
from .util import UnfurlError, Generate, toEnum
from .support import Status, NodeState
from .configurator import (
    ConfigurationSpec,
    getConfigSpecArgsFromImplementation,
    TaskRequest,
)
from .tosca import findStandardInterface

import logging

logger = logging.getLogger("unfurl")


def isExternalTemplateCompatible(external, template):
    # for now, require template names to match
    if external.name == template.name:
        if not external.isCompatibleType(template.type):
            raise UnfurlError(
                'external template "%s" not compatible with local template'
                % template.name
            )
        return True
    return False


class Plan(object):
    @staticmethod
    def getPlanClassForWorkflow(workflow):
        return dict(
            deploy=DeployPlan,
            undeploy=UndeployPlan,
            stop=UndeployPlan,
            run=RunNowPlan,
            check=ReadOnlyPlan,
            discover=ReadOnlyPlan,
        ).get(workflow, WorkflowPlan)

    interface = "None"

    def __init__(self, root, toscaSpec, jobOptions):
        self.jobOptions = jobOptions
        self.workflow = jobOptions.workflow
        self.root = root
        self.tosca = toscaSpec
        assert self.tosca
        if jobOptions.template:
            filterTemplate = self.tosca.getTemplate(jobOptions.template)
            if not filterTemplate:
                raise UnfurlError(
                    "specified template not found: %s" % jobOptions.template
                )
            self.filterTemplate = filterTemplate
        else:
            self.filterTemplate = None

    def findShadowInstance(self, template, match=isExternalTemplateCompatible):
        searchAll = []
        for name, value in self.root.imports.items():
            external = value.resource
            # XXX if external is a Relationship and template isn't, get it's target template
            #  if no target, create with status == unknown

            if match(external.template, template):
                if external.shadow and external.root is self.root:
                    # shadowed instance already created
                    return external
                else:
                    return self.createShadowInstance(external, name)
            if value.spec.get("instance") == "*":
                searchAll.append((name, value.resource))

        # look in the topologies where were are importing everything
        for name, root in searchAll:
            for external in root.getSelfAndDescendents():
                if match(external.template, template):
                    return self.createShadowInstance(external, name)

        return None

    def createShadowInstance(self, external, importName):
        if self.root.imports[importName].resource is external:
            name = importName
        else:
            name = importName + ":" + external.name

        if external.parent and external.parent.parent:
            # assumes one-to-one correspondence instance and template
            parent = self.findShadowInstance(external.parent.template)
            if not parent:  # parent wasn't in imports, add it now
                parent = self.createShadowInstance(external.parent, importName)
        else:
            parent = self.root

        shadowInstance = external.__class__(
            name, external.attributes, parent, external.template, external
        )

        shadowInstance.shadow = external
        # Imports.__setitem__ will add or update:
        self.root.imports[name] = shadowInstance
        return shadowInstance

    def findResourcesFromTemplate(self, template):
        if template.abstract == "select":
            # XXX also match node_filter if present
            shadowInstance = self.findShadowInstance(template)
            if shadowInstance:
                yield shadowInstance
            else:
                logger.info(
                    "could not find external instance for template %s", template.name
                )
            # XXX also yield newly created parents that needed to be checked?
        else:
            for resource in self.findResourcesFromTemplateName(template.name):
                yield resource

    def findResourcesFromTemplateName(self, name):
        # XXX make faster
        for resource in self.root.getSelfAndDescendents():
            if resource.template.name == name:
                yield resource

    def findParentResource(self, source):
        parentTemplate = findParentTemplate(source.toscaEntityTemplate)
        if not parentTemplate:
            return self.root
        for parent in self.findResourcesFromTemplateName(parentTemplate.name):
            # XXX need to evaluate matches
            return parent
        raise UnfurlError(
            "could not find instance of template: %s" % parentTemplate.name
        )

    def createResource(self, template):
        parent = self.findParentResource(template)
        # XXX if joboption.check: status = Status.unknown
        if not parent.parent or parent.missing:
            # parent is root or parent doesn't exist so this can't exist either, set to pending
            status = Status.pending
        else:
            status = Status.unknown
        # Set the initial status of new resources to status instead of defaulting to "unknown"
        return NodeInstance(template.name, None, parent, template, status)

    def findImplementation(self, interface, operation, template):
        default = None
        for iDef in template.getInterfaces():
            if iDef.iname == interface or iDef.type == interface:
                if iDef.name == operation:
                    return iDef
                if iDef.name == "default":
                    default = iDef
        return default

    def _runOperation(self, startState, op, resource, reason=None, inputs=None):
        ran = False
        req = self.createTaskRequest(op, resource, reason, inputs)
        if not req.error:
            if startState is not None and (
                not resource.state or startState > resource.state
            ):
                resource.state = startState
            task = yield req
            if task:
                ran = True
                # if the state hasn't been set by the task, advance the state
                if (
                    startState is not None
                    and task.result.success
                    and resource.state == startState
                ):
                    # task succeeded but didn't update nodestate
                    resource.state = NodeState(resource.state + 1)
        yield ran

    def _executeDefaultConfigure(self, resource, reason=None, inputs=None):
        # 5.8.5.4 Node-Relationship configuration sequence p. 229
        # Depending on which side (i.e., source or target) of a relationship a node is on, the orchestrator will:
        # Invoke either the pre_configure_source or pre_configure_target operation as supplied by the relationship on the node.

        targetConfigOps = resource.template.getCapabilityInterfaces()
        # test for targetConfigOps to avoid creating unnecessary instances
        if targetConfigOps:
            for capability in resource.capabilities:
                # Operation to pre-configure the target endpoint.
                for relationship in capability.relationships:
                    # we're the target, source may not have been created yet
                    # XXX if not relationship.source create the instance
                    gen = self._runOperation(
                        NodeState.configuring,
                        "Configure.pre_configure_target",
                        relationship,
                        reason,
                    )
                    req = gen.send(None)
                    if req:
                        gen.send((yield req))

        # we're the source, target has already started
        sourceConfigOps = resource.template.getRequirementInterfaces()
        if sourceConfigOps:
            if resource.template.getRequirementInterfaces():
                # Operation to pre-configure the target endpoint
                for relationship in resource.requirements:
                    gen = self._runOperation(
                        NodeState.configuring,
                        "Configure.pre_configure_source",
                        relationship,
                        reason,
                    )
                    req = gen.send(None)
                    if req:
                        gen.send((yield req))

        gen = self._runOperation(
            NodeState.configuring, "Standard.configure", resource, reason, inputs
        )
        req = gen.send(None)
        if req:
            gen.send((yield req))

        if sourceConfigOps:
            for requirement in resource.requirements:
                gen = self._runOperation(
                    NodeState.configuring,
                    "Configure.post_configure_source",
                    requirement,
                    reason,
                )
                req = gen.send(None)
                if req:
                    gen.send((yield req))

        if targetConfigOps:
            for capability in resource.capabilities:
                # we're the target, source may not have been created yet
                # Operation to post-configure the target endpoint.
                for relationship in capability.relationships:
                    # XXX if not relationship.source create the instance
                    gen = self._runOperation(
                        NodeState.configuring,
                        "Configure.post_configure_target",
                        relationship,
                        reason,
                    )
                    req = gen.send(None)
                    if req:
                        gen.send((yield req))

    def executeDefaultDeploy(self, resource, reason=None, inputs=None):
        # 5.8.5.2 Invocation Conventions p. 228
        # 7.2 Declarative workflows p.249
        ran = False
        missing = (
            resource.status in [Status.unknown, Status.absent, Status.pending]
            and resource.state != NodeState.stopped  # stop sets Status back to pending
        )
        # if the resource doesn't exist or failed while creating:
        initialState = not resource.state or resource.state == NodeState.creating
        if (
            missing
            or self.jobOptions.force
            or (resource.status == Status.error and initialState)
        ):
            gen = self._runOperation(
                NodeState.creating, "Standard.create", resource, reason, inputs
            )
            req = gen.send(None)
            if req and gen.send((yield req)):
                ran = True
                if resource.created is None:
                    resource.created = True

        if (
            not ran and resource.state != NodeState.stopped
        ) or resource.state == NodeState.created:
            if resource.state and resource.state > NodeState.configured:
                # rerunning configuration, reset state
                assert not ran
                resource.state = NodeState.creating
            gen = Generate(self._executeDefaultConfigure(resource, reason, inputs))
            while gen():
                gen.result = yield gen.next
            ran = gen.next
            if ran and resource.created is None:
                resource.created = True

            # XXX if the resource had already existed, call target_changed
            # "Operation to notify source some property or attribute of the target changed"
            # if not missing:
            #   for requirement in requirements:
            #     call target_changed

        if resource.state in [NodeState.configured, NodeState.stopped] or (
            not ran and resource.state == NodeState.created
        ):
            # configured or if no configure operation exists then node just needs to have been created
            gen = self._runOperation(
                NodeState.starting, "Standard.start", resource, reason, inputs
            )
            req = gen.send(None)
            if req and gen.send((yield req)):
                ran = True

        # XXX these are only called when adding instances
        # add_source: Operation to notify the target node of a source node which is now available via a relationship.
        # add_target: Operation to notify source some property or attribute of the target changed

    def executeDefaultUndeploy(self, resource, reason=None, inputs=None):
        # XXX run check before if defined?
        # XXX don't delete if dirty
        # XXX remove_target: Operation called on source when a target instance is removed
        # (but only called if add_target had been called)

        if (
            resource.state in [NodeState.starting, NodeState.started]
            or self.workflow == "stop"
        ):
            nodeState = NodeState.stopping
            op = "Standard.stop"

            gen = self._runOperation(nodeState, op, resource, reason, inputs)
            req = gen.send(None)
            if req:
                gen.send((yield req))

        if self.workflow == "stop":
            return

        if resource.created or self.jobOptions.destroyunmanaged:
            nodeState = NodeState.deleting
            op = "Standard.delete"
        else:
            nodeState = None
            op = "Install.revert"

        gen = self._runOperation(nodeState, op, resource, reason, inputs)
        req = gen.send(None)
        if req:
            gen.send((yield req))
        # Note: Status.absent is set in _generateConfigurations

    def executeDefaultInstallOp(self, operation, resource, reason=None, inputs=None):
        req = self.createTaskRequest("Install." + operation, resource, reason, inputs)
        if not req.error:
            yield req

    def createTaskRequest(self, operation, resource, reason=None, inputs=None):
        """implementation can either be a named artifact (including a python configurator class),
        or a file path"""
        interface, sep, action = operation.rpartition(".")
        iDef = self.findImplementation(interface, action, resource.template)
        if iDef and iDef.name != "default":
            # merge inputs
            if inputs:
                inputs = dict(iDef.inputs, **inputs)
            else:
                inputs = iDef.inputs or {}
            kw = getConfigSpecArgsFromImplementation(iDef, inputs, resource.template)
        else:
            kw = None

        if kw:
            if reason:
                name = "for %s: %s.%s" % (reason, interface, action)
                if reason == self.workflow:
                    # set the task's workflow instead of using the default ("deploy")
                    kw["workflow"] = reason
            else:
                name = "%s.%s" % (interface, action)
            configSpec = ConfigurationSpec(name, action, **kw)
            logger.debug(
                "creating configuration %s with %s to run for %s: %s",
                configSpec.name,
                configSpec.inputs,
                resource.name,
                reason or action,
            )
        else:
            errorMsg = (
                'unable to find an implementation for operation "%s" on node "%s"'
                % (action, resource.template.name)
            )
            configSpec = ConfigurationSpec("#error", action, className=errorMsg)
            logger.debug(errorMsg)
            reason = "error"

        return TaskRequest(configSpec, resource, reason or action)

    def generateDeleteConfigurations(self, include):
        for resource in getOperationalDependents(self.root):
            # reverse to teardown leaf nodes first
            logger.debug("checking instance for removal: %s", resource.name)
            if resource.shadow or resource.template.abstract:  # readonly resource
                continue
            # check if creation and deletion is managed externally
            if not resource.created and not self.jobOptions.destroyunmanaged:
                continue
            if isinstance(resource.created, six.string_types):
                # creation and deletion is managed by another instance
                continue

            # if resource exists (or unknown)
            if resource.status not in [Status.absent, Status.pending]:
                reason = include(resource)
                if reason:
                    logger.debug("%s instance %s", reason, resource.name)
                    workflow = "undeploy" if reason == "prune" else self.workflow
                    gen = Generate(
                        self._generateConfigurations(resource, reason, workflow)
                    )
                    while gen():
                        gen.result = yield gen.next

    def _getDefaultGenerator(self, workflow, resource, reason=None, inputs=None):
        if workflow == "deploy":
            return self.executeDefaultDeploy(resource, reason, inputs)
        elif workflow == "undeploy" or workflow == "stop":
            return self.executeDefaultUndeploy(resource, reason, inputs)
        elif workflow == "check" or workflow == "discover":
            return self.executeDefaultInstallOp(workflow, resource, reason, inputs)
        return None

    def getSuccessStatus(self, workflow):
        if workflow == "deploy":
            return Status.ok
        elif workflow == "stop":
            return Status.pending
        elif workflow == "undeploy":
            return Status.absent
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
            result = yield gen.next
            gen.result = result
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
        # XXX we should apply ConfigTask.finished() logic here in aggregate (use aggregateStatus?):
        # e.g. if any task failed and any task modified but none didn't explicily set status, set error state
        # use case: configure succeeds but start fails
        if successStatus is not None and successes and not failures:
            resource.localStatus = successStatus
            if oldStatus != successStatus:
                resource._lastConfigChange = task.changeId
                if successStatus == Status.ok and resource.created is None:
                    resource.created = True

    def executeWorkflow(self, workflowName, resource):
        workflow = self.tosca.getWorkflow(workflowName)
        if not workflow:
            return None
        if not workflow.matchPreconditions(resource):  # check precondition
            return None
        steps = [
            step
            for step in workflow.initialSteps()
            # XXX check target_relationship too
            # XXX target can be a group name too
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
            if not workflow.matchStepFilter(step.name, resource):
                logger.debug(
                    "step did not match filter %s with %s", step.name, resource.name
                )
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
        logger.debug("executing step %s for %s", step.name, resource.name)
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
                # XXX need to pass operation_host (see 3.6.27 Workflow step definition p188)
                # if target is a group can be value can be node_type or node template name
                # if its a node_type select nodes matching the group
                result = yield self.createTaskRequest(
                    activity.call_operation,
                    resource,
                    "step:" + step.name,
                    activity.inputs,
                )
            elif activity.type == "set_state":
                if "managed" in activity.set_state:
                    resource.created = (
                        False if activity.set_state == "unmanaged" else True
                    )
                else:
                    try:
                        resource.state = activity.set_state
                    except KeyError:
                        resource.localStatus = toEnum(Status, activity.set_state)
            elif activity.type == "delegate":
                # XXX inputs
                configGenerator = self._getDefaultGenerator(
                    activity.delegate, resource, activity.delegate
                )
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

    def _getTemplates(self):
        templates = (
            []
            if not self.tosca.nodeTemplates
            else [
                t
                for t in self.tosca.nodeTemplates.values()
                if not t.isCompatibleType(self.tosca.ConfiguratorType)
            ]
        )

        # order by ancestors
        return list(
            orderTemplates(
                {t.name: t for t in templates},
                self.filterTemplate and self.filterTemplate.name,
                self.interface,
            )
        )

    def includeNotFound(self, template):
        return True

    def _generateWorkflowConfigurations(self, instance, oldTemplate):
        configGenerator = self._generateConfigurations(instance, self.workflow)
        gen = Generate(configGenerator)
        while gen():
            gen.result = yield gen.next

    def executePlan(self):
        """
        Generate candidate tasks

        yields TaskRequests
        """
        opts = self.jobOptions
        templates = self._getTemplates()

        logger.debug("checking for tasks for templates %s", [t.name for t in templates])
        visited = set()
        for template in templates:
            found = False
            for resource in self.findResourcesFromTemplate(template):
                found = True
                visited.add(id(resource))
                gen = Generate(self._generateWorkflowConfigurations(resource, template))
                while gen():
                    gen.result = yield gen.next

            if (
                not found
                and not template.abstract
                and "dependent" not in template.directives
            ):
                include = self.includeNotFound(template)
                if include:
                    resource = self.createResource(template)
                    visited.add(id(resource))
                    gen = Generate(self._generateWorkflowConfigurations(resource, None))
                    while gen():
                        gen.result = yield gen.next

        if opts.prune:
            test = lambda resource: "prune" if id(resource) not in visited else False
            gen = Generate(self.generateDeleteConfigurations(test))
            while gen():
                gen.result = yield gen.next


class DeployPlan(Plan):
    interface = "Standard"

    def includeNotFound(self, template):
        if self.jobOptions.add or self.jobOptions.force:
            return "add"
        return None

    def includeTask(self, template, resource):
        # XXX doc string woefully out of date
        """Returns whether or not the config should be included in the current job.

        Is it out of date?
        Has its configuration changed?
        Has its dependencies changed?
        Are the resources it modifies in need of repair?

        Reasons include: "force", "add", "upgrade", "update", "re-add", 'prune',
        'missing', "config changed", "failed to apply", "degraded", "error".

        Args:
            config (ConfigurationSpec): The :class:`ConfigurationSpec` candidate
            lastChange (Configuration): The :class:`Configuration` representing the that last time
              the given :class:`ConfigurationSpec` was applied or `None`

        Returns:
            (str, ConfigurationSpec): Returns a pair with reason why the task was included
              and the :class:`ConfigurationSpec` to run or `None` if it shound't be included.
        """
        assert template and resource
        jobOptions = self.jobOptions
        if jobOptions.add and not resource.lastConfigChange:
            # add if it's a new resource
            return "add"

        if jobOptions.force:
            return "force"

        # if the specification changed:
        oldTemplate = resource.template
        if template != oldTemplate:
            if jobOptions.upgrade:
                return "upgrade"
            if jobOptions.update:
                # only apply the new configuration if doesn't result in a major version change
                if True:  # XXX if isMinorDifference(template, oldTemplate)
                    return "update"

        # there isn't a new config to run, see if the last applied config needs to be re-run
        # XXX: if (jobOptions.upgrade or jobOptions.update or jobOptions.force):
        #  if (configTask.hasInputsChanged() or configTask.hasDependenciesChanged()) and
        #    return 'config changed', configTask.configSpec
        return self.checkForRepair(resource)

    def checkForRepair(self, instance):
        jobOptions = self.jobOptions
        assert instance
        if jobOptions.repair == "none":
            return None
        status = instance.status

        if status in [Status.unknown, Status.pending]:
            if jobOptions.repair == "missing":
                return "repair missing"
            elif instance.required:
                status = Status.error  # treat as error
            else:
                return None

        if status not in [Status.degraded, Status.error]:
            return None

        if jobOptions.repair == "degraded":
            assert status > Status.ok, status
            return "repair degraded"  # repair this
        elif status == Status.degraded:
            assert jobOptions.repair == "error", jobOptions.repair
            return None  # skip repairing this
        else:
            assert jobOptions.repair == "error", "repair: %s status: %s" % (
                jobOptions.repair,
                instance.status,
            )
            return "repair error"  # repair this

    def isInstanceReadOnly(self, instance):
        return instance.shadow or "discover" in instance.template.directives

    def _generateWorkflowConfigurations(self, instance, oldTemplate):
        # if oldTemplate is not None this is an existing instance, so check if we should include
        if oldTemplate:
            reason = self.includeTask(oldTemplate, instance)
            if not reason:
                logger.debug(
                    "not including task for %s:%s", instance.name, oldTemplate.name
                )
                return
        else:  # this is newly created resource
            reason = "add"

        if instance.status == Status.unknown or instance.shadow:
            installOp = "check"
        elif "discover" in instance.template.directives:
            installOp = "discover"
        else:
            installOp = None

        if installOp:
            status = instance.status
            configGenerator = self._generateConfigurations(
                instance, installOp, installOp
            )
            if configGenerator:
                gen = Generate(configGenerator)
                while gen():
                    gen.result = yield gen.next

            if self.isInstanceReadOnly(instance):
                return  # we're done

            if instance.operational and status != instance.status:
                return  # it checked out! we're done

        configGenerator = self._generateConfigurations(instance, reason)
        gen = Generate(configGenerator)
        while gen():
            gen.result = yield gen.next


class UndeployPlan(Plan):
    def executePlan(self):
        """
        yields configSpec, target, reason
        """
        gen = Generate(self.generateDeleteConfigurations(self.includeForDeletion))
        while gen():
            gen.result = yield gen.next

    def includeForDeletion(self, resource):
        if self.filterTemplate and resource.template != self.filterTemplate:
            return None
        # return value is used as "reason"
        return self.workflow


class ReadOnlyPlan(Plan):
    interface = "Install"


class WorkflowPlan(Plan):
    def executePlan(self):
        """
        yields configSpec, target, reason
        """
        workflow = self.tosca.getWorkflow(self.jobOptions.workflow)
        if not workflow:
            raise UnfurlError('workflow not found: "%s"' % self.jobOptions.workflow)
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
    def _createConfigurator(self, args, action, inputs=None, timeout=None):
        if args.get("module") or args.get("host"):
            className = "unfurl.configurators.ansible.AnsibleConfigurator"
            module = args.get("module") or "command"
            module_args = " ".join(args["cmdline"])
            params = dict(playbook=[{module: module_args}])
        else:
            className = "unfurl.configurators.shell.ShellConfigurator"
            params = dict(command=args["cmdline"])

        if inputs:
            params.update(inputs)

        return ConfigurationSpec(
            "cmdline",
            action,
            className=className,
            inputs=params,
            operation_host=args.get("host"),
            timeout=timeout,
        )

    def executePlan(self):
        instanceFilter = self.jobOptions.instance
        if instanceFilter:
            resource = self.root.findResource(instanceFilter)
            if not resource:
                # see if there's a template with the same name and create the resource
                template = self.tosca.getTemplate(instanceFilter)
                if template:
                    resource = self.createResource(template)
                else:
                    raise UnfurlError(
                        "specified instance not found: %s" % instanceFilter
                    )
            resources = [resource]
        else:
            resources = [self.root]

        # userConfig has the job options explicitly set by the user
        operation = self.jobOptions.userConfig.get("operation")
        operation_host = self.jobOptions.userConfig.get("host")
        if not operation:
            configSpec = self._createConfigurator(self.jobOptions.userConfig, "run")
        else:
            configSpec = None
            interface, sep, action = operation.rpartition(".")
            if not interface and findStandardInterface(operation):  # shortcut
                operation = findStandardInterface(operation) + "." + operation
        for resource in resources:
            if configSpec:
                yield TaskRequest(configSpec, resource, "run")
            else:
                req = self.createTaskRequest(operation, resource, "run")
                if not req.error:  # if operation was found:
                    req.configSpec.operation_host = operation_host
                    yield req


def findExplicitOperationHosts(template, interface):
    for iDef in template.getInterfaces():
        if isinstance(iDef.implementation, dict):
            operation_host = iDef.implementation.get("operation_host")
            if operation_host and operation_host not in [
                "localhost",
                "ORCHESTRATOR",
                "SELF",
                "HOST",
                "TARGET",
                "SOURCE",
            ]:
                yield operation_host


def orderTemplates(templates, filter=None, interface=None):
    # templates is dict of NodeSpecs
    seen = set()
    for source in templates.values():
        if filter and source.name != filter:
            continue
        if source in seen:
            continue

        if interface:
            for operation_host in findExplicitOperationHosts(source, interface):
                operationHostSpec = templates.get(operation_host)
                if operationHostSpec:
                    if operationHostSpec in seen:
                        continue
                    seen.add(operationHostSpec)
                    yield operationHostSpec

        for ancestor in getAncestorTemplates(source.toscaEntityTemplate):
            spec = templates.get(ancestor.name)
            if spec:
                if spec in seen:
                    continue
                seen.add(spec)
                if spec:
                    yield spec


def getAncestorTemplates(source):
    # note: opposite direction as NodeSpec.relationships
    for (rel, req) in source.relationships:
        for ancestor in getAncestorTemplates(rel.target):
            yield ancestor
    yield source


def findParentTemplate(source):
    for rel, req in source.relationships:
        if rel.type == "tosca.relationships.HostedOn":
            return rel.target
        return None


def getOperationalDependents(resource, seen=None):
    if seen is None:
        seen = set()
    for dep in resource.getOperationalDependents():
        if id(dep) not in seen:
            seen.add(id(dep))
            for child in getOperationalDependents(dep, seen):
                yield child
            yield dep


# XXX!:
# def buildDependencyGraph():
#     """
#   We need to find each executed configuration that is affected by a configuration change
#   and re-execute them
#
#   dependencies map to inbound edges
#   lastConfigChange filters ConfigTasks
#   ConfigTasks.configurationResource dependencies are inbound
#   keys in ConfigTasks.changes map to a outbound edges to resources
#
#   Need to distinguish between runtime dependencies and configuration dependencies?
#   """
#
#
# def buildConfigChangedExecutionPlan():
#     """
#   graph = buildDependencyGraph()
#
#   We follow the edges if a resource's attributes or dependencies have changed
#   First we validate that we can execute the plan by validating configurationResource in the graph
#   If it validates, add to plan
#
#   After executing a task, only follow the dependent outputs that changed no need to follow those dependencies
#   """
#
#
# def validateNode(resource, expectedTemplateOrType):
#     """
#   First make sure the resource conforms to the expected template (type, properties, attributes, capabilities, requirements, etc.)
#   Then for each dependency that corresponds to a required relationship, call validateNode() on those resources with the declared type or template for the relationship
#   """
#
#
# def buildCreateExecutionPlan(self):
#     """
#   move Configurator nodes to separate list
#   start with root node template,
#   configuratorTemplate = find Configurator node for creation,
#   self.add(self.buildCreateExecutionPlan(configuratorTemplate))
#   return # that's all we need, the rest is dynamic, for each new resource, find Configurator for every missing capability and requirement
#   """
#
#
# def buildCreateStaticExecutionPlan(self):
#     """
#   Build a static plan by using the 'provides' property to estimate what the
#   configuration will create and then recursively find missing capabilities and requirements and then the configurators to run
#   """
#     start = self.buildCreateExecutionPlan(self.rootNodeTemplate)
#
#
# def buildUpgradeExecutionPlan():
#     """
#   Same as buildCreateExecutionPlan except look for existing resources
#   if it exists, see if it needs upgrading, if it doesn't then find create configurator
#   Upgrading:
#     compare resource with spec,
#     if different add configurators to replace exisiting configuration
#     for each requirement not in dependencies, add configurator
#     for each dependency on requirement, see if corresponding requirement in spec
#       if not, remove dependency
#       if it is, call buildUpgradeExecutionPlan on the target resource with the target node type or template
#   """
#
#
# def buildUpdateExecutionPlan():
#     """
#   Only apply updates that don't change the currently applied spec,
#   Starting with the start resource compare deployed artifacts and software nodes associate with it with current template
#   and if diffence is no more than a minor version bump,
#   retreive the old version of the topology that is associated with the appropriate configuredBy
#   and with it try to find and queue a configurator that can apply those changes.
#
#   For each resource dependency, call buildUpdateExecutionPlan().
#   """
