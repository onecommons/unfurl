# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from functools import partial
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Iterable,
    Iterator,
    List,
    Optional,
    TYPE_CHECKING,
    Set,
    Tuple,
    Union,
    cast,
)
from typing_extensions import Literal

if TYPE_CHECKING:
    from .job import JobOptions

from .runtime import (
    InstanceKey,
    NodeInstance,
    EntityInstance,
    Operational,
    TopologyInstance,
)
from .util import UnfurlError
from .result import ChangeRecord
from .support import Status, NodeState, Reason
from .planrequests import (
    TaskRequest,
    TaskRequestGroup,
    SetStateRequest,
    JobRequest,
    create_task_request,
    filter_config,
    filter_task_request,
    ConfigurationSpec,
    find_parent_resource,
    find_resources_from_template_name,
    _find_implementation,
)
from .spec import (
    NodeSpec,
    TopologySpec,
    Workflow,
    find_standard_interface,
    EntitySpec,
    ToscaSpec,
)
from .logs import getLogger
from toscaparser.nodetemplate import NodeTemplate

logger = getLogger("unfurl")
MAX_MISSING_PARENT_INSTANCES = 20


class Plan:
    @staticmethod
    def get_plan_class_for_workflow(workflow):
        return dict(
            deploy=DeployPlan,
            undeploy=UndeployPlan,
            stop=UndeployPlan,
            run=RunNowPlan,
            check=ReadOnlyPlan,
            discover=ReadOnlyPlan,
        ).get(workflow, WorkflowPlan)

    interface: Optional[str] = None

    def __init__(
        self, root: TopologyInstance, toscaSpec: ToscaSpec, jobOptions: "JobOptions"
    ) -> None:
        self.jobOptions = jobOptions
        self.workflow = jobOptions.workflow
        self.root = root
        self.tosca = toscaSpec
        self._checked_connection_task = False
        self._ran_workflow = False
        assert self.tosca
        if jobOptions.template:
            filterTemplate = self.tosca.get_template(jobOptions.template)
            if not filterTemplate:
                raise UnfurlError(
                    f"specified template not found: {jobOptions.template}"
                )
            self.filterTemplate: Optional[EntitySpec] = filterTemplate
        else:
            self.filterTemplate = None

    def find_shadow_instance(self, template: EntitySpec) -> Optional[EntityInstance]:
        imported = template.tpl.get("imported")
        if imported:
            assert self.root.imports is not None
            _external = self.root.imports.find_instance(imported)
            if not _external:
                return None
            if _external.shadow and _external.root is self.root:
                # shadowed instance already created
                return _external
            else:
                import_name = imported.partition(":")[0]
                return self.create_shadow_instance(_external, import_name, template)
        return None

    def create_shadow_instance(
        self, external: EntityInstance, import_name: str, template: EntitySpec
    ) -> EntityInstance:
        # create a local instance that "shadows" the external one we imported
        assert self.root.imports is not None
        name = import_name + ":" + external.name
        instance_name = template.name
        # XXX import the parent too by creating a template and setting its name to name?
        parent = self.root
        logger.debug(
            "creating local instance %s for external instance %s",
            instance_name or name,
            external.name,
        )
        # attributes can be empty because AttributeManager delegates to the imported shadow instance
        shadowInstance = external.__class__(
            instance_name, {}, parent, template, external.status
        )
        shadowInstance.imported = name
        self.root.imports.set_shadow(name, shadowInstance, external)
        return shadowInstance

    def find_resources_from_template(
        self, template: NodeSpec
    ) -> Iterator[NodeInstance]:
        if template.abstract == "select":
            shadowInstance = self.find_shadow_instance(template)
            if shadowInstance:
                logger.debug('found imported instance "%s"', shadowInstance.name)
                yield cast(NodeInstance, shadowInstance)
            else:
                logger.warning(
                    "could not find external instance for template %s", template.name
                )
            # XXX also yield newly created parents that needed to be checked?
        else:
            if self.root.template.topology is not template.topology:
                name = ":" + template.topology.nested_name
                assert self.root.imports is not None
                root = self.root.imports.find_instance(name)
                if not root:
                    self.root.create_nested_topology(template.topology)
                    return
            else:
                root = self.root
            for resource in find_resources_from_template_name(root, template.name):
                yield cast(NodeInstance, resource)

    def create_resource(self, template: NodeSpec, missing=0) -> Optional[NodeInstance]:
        parent, parent_template = find_parent_resource(self.root, template)
        if not parent and parent_template:
            parent_node = self.tosca.node_from_template(parent_template)
            if parent_node:
                if missing > MAX_MISSING_PARENT_INSTANCES:
                    raise UnfurlError(
                        f"more than {MAX_MISSING_PARENT_INSTANCES} missing ancestors (last was {parent_node.name}) -- is there a cycle?"
                    )
                logger.trace(
                    "creating missing parent %s for %s %s",
                    parent_node.name,
                    template.name,
                    missing,
                )
                parent = self.create_resource(parent_node, missing + 1)
        if not parent:
            return None

        if self.jobOptions.check or "check" in template.directives:
            status = Status.unknown
        else:
            status = Status.pending
        instance = NodeInstance(template.name, None, parent, template, status)
        if template.substitution:
            # set shadow to inner node instance
            assert template.substitution.substitution_node
            assert self.root.imports is not None
            # get the nested TopologyInstance
            nested_root_name = ":" + template.substitution.nested_name
            nested_root = self.root.imports.find_instance(nested_root_name)
            assert nested_root, f"{nested_root_name} should already have been created"
            name = template.substitution.substitution_node.name
            inner = nested_root.find_instance(name)
            assert inner, (
                f"{name} in {nested_root_name} should have already been created before {instance.name}"
            )
            assert inner is not instance
            instance.imported = (
                ":" + template.substitution.substitution_node.nested_name
            )
            self.root.imports.set_shadow(instance.imported, instance, inner)
        return instance

    def _run_operation(
        self,
        startState: Optional[NodeState],
        op: str,
        resource,
        reason=None,
        inputs=None,
    ) -> Iterator[TaskRequest]:
        req = create_task_request(
            self.jobOptions, op, resource, reason, inputs, startState
        )
        if req:
            self._ran_workflow = True
            yield req

    def _execute_default_configure(
        self, resource: NodeInstance, reason: Optional[str] = None, inputs=None
    ) -> Iterator[TaskRequest]:
        # 5.8.5.4 Node-Relationship configuration sequence p. 229
        # Depending on which side (i.e., source or target) of a relationship a node is on, the orchestrator will:
        # Invoke either the pre_configure_source or pre_configure_target operation as supplied by the relationship on the node.

        template = cast(NodeSpec, resource.template)
        targetConfigOps = template.get_capability_interfaces()
        # test for targetConfigOps to avoid creating unnecessary instances
        if targetConfigOps:
            for capability in resource.capabilities:
                # Operation to pre-configure the target endpoint.
                for relationship in capability.relationships:
                    # we're the target, source may not have been created yet
                    yield from self._run_operation(
                        NodeState.configuring,
                        "Configure.pre_configure_target",
                        relationship,
                        reason,
                    )

        # we're the source, target has already started
        sourceConfigOps = template.get_requirement_interfaces()
        if sourceConfigOps:
            if template.get_requirement_interfaces():
                # Operation to pre-configure the target endpoint
                for relationship in resource.requirements:
                    yield from self._run_operation(
                        NodeState.configuring,
                        "Configure.pre_configure_source",
                        relationship,
                        reason,
                    )

        yield from self._run_operation(
            NodeState.configuring, "Standard.configure", resource, reason, inputs
        )

        if sourceConfigOps:
            # we're the source and we just ran configure, now configure any relationships
            for requirement in resource.requirements:
                yield from self._run_operation(
                    NodeState.configuring,
                    "Configure.post_configure_source",
                    requirement,
                    reason,
                )

        if targetConfigOps:
            for capability in resource.capabilities:
                # we're the target, source may not have been created yet
                # Operation to post-configure the target endpoint.
                for relationship in capability.relationships:
                    # XXX if not relationship.source create the instance
                    yield from self._run_operation(
                        NodeState.configuring,
                        "Configure.post_configure_target",
                        relationship,
                        reason,
                    )

    def execute_default_deploy(
        self, resource: NodeInstance, reason: Optional[str] = None, inputs=None
    ) -> Iterator[TaskRequest]:
        # 5.8.5.2 Invocation Conventions p. 228
        # 7.2 Declarative workflows p.249
        status = resource.local_status or Status.unknown
        missing = (
            status in [Status.unknown, Status.absent, Status.pending]
            and resource.state != NodeState.stopped  # stop sets Status back to pending
        )
        # if the resource doesn't exist or failed while creating:
        initialState = not resource.state or resource.state == NodeState.creating
        self._ran_workflow = False
        if (
            missing
            or self.jobOptions.force
            or (status == Status.error and initialState)
        ):
            req = create_task_request(
                self.jobOptions,
                "Standard.create",
                resource,
                reason,
                inputs,
                NodeState.creating,
            )
            if req:
                self._ran_workflow = True
                yield req
            else:
                # no create operation defined, run configure instead
                initialState = True

        if (
            initialState
            or resource.state is None
            or resource.state < NodeState.configured
            or (self.jobOptions.force and resource.state != NodeState.started)
            or status == Status.error
        ):
            yield from self._execute_default_configure(resource, reason, inputs)

            # XXX if the resource had already existed, call target_changed
            # "Operation to notify source some property or attribute of the target changed"
            # if not missing:
            #   for requirement in requirements:
            #     call target_changed

        if initialState or resource.state != NodeState.started or self.jobOptions.force:
            # configured or if no configure operation exists then node just needs to have been created
            yield from self._run_operation(
                NodeState.starting, "Standard.start", resource, reason, inputs
            )
        if not self._ran_workflow:
            errorMsg = (
                f'unable to find a deploy operation on node "{resource.template.name}"'
            )
            logger.debug(errorMsg)
        # XXX these are only called when adding instances
        # add_source: Operation to notify the target node of a source node which is now available via a relationship.
        # add_target: Operation to notify source some property or attribute of the target changed

    def execute_default_undeploy(
        self, resource: NodeInstance, reason: Optional[str] = None, inputs=None
    ) -> Iterator[TaskRequest]:
        # XXX run check if joboption set?
        # XXX don't delete if dirty
        if (
            resource.state in [NodeState.starting, NodeState.started]
            or self.workflow == "stop"
        ):
            nodeState: Optional[NodeState] = NodeState.stopping
            op = "Standard.stop"

            yield from self._run_operation(nodeState, op, resource, reason, inputs)

        self._ran_workflow = False
        if self.workflow == "stop":
            return

        template = cast(NodeSpec, resource.template)
        targetConfigOps = template.get_capability_interfaces()
        # test for targetConfigOps to avoid creating unnecessary instances
        if targetConfigOps:
            # if there are relationships targeting us we need to remove them first
            for capability in resource.capabilities:
                for relationship in capability.relationships:
                    # we're the target that is about to be deleted
                    yield from self._run_operation(
                        NodeState.configuring,
                        "Configure.remove_target",
                        relationship,
                        reason,
                    )

        sourceConfigOps = template.get_requirement_interfaces()
        if sourceConfigOps:
            # before we are deleted, remove any relationships we have
            if template.get_requirement_interfaces():
                for relationship in resource.requirements:
                    yield from self._run_operation(
                        NodeState.configuring,
                        "Configure.remove_source",
                        relationship,
                        reason,
                    )

        # note: filter logic should have already been applied by generate_delete_configurations()
        if resource.created or self.jobOptions.destroyunmanaged:
            nodeState = NodeState.deleting
            op = "Standard.delete"
        else:  # XXX filter logic filters out this case so this never happens
            nodeState = None
            op = "Install.revert"

        yield from self._run_operation(nodeState, op, resource, reason, inputs)
        if not self._ran_workflow:
            errorMsg = f'unable to find a teardown operation on node "{resource.template.name}"'
            logger.debug(errorMsg)
        self._ran_workflow = False

    def execute_default_install_op(
        self,
        operation: str,
        resource: NodeInstance,
        reason: Optional[str] = None,
        inputs=None,
    ) -> Iterator[TaskRequest]:
        # add target or add source
        req = create_task_request(
            self.jobOptions, "Install." + operation, resource, reason, inputs
        )
        if req:
            yield req

        template = cast(NodeSpec, resource.template)
        if operation != "check":
            # we're the source, target has already started
            sourceConfigOps = template.get_requirement_interfaces()
            if sourceConfigOps:
                if template.get_requirement_interfaces():
                    # we're the source
                    for relationship in resource.requirements:
                        req = create_task_request(
                            self.jobOptions,
                            "Configure.add_source",
                            relationship,
                            reason,
                            inputs,
                        )
                        if req:
                            yield req

            targetConfigOps = template.get_capability_interfaces()
            # test for targetConfigOps to avoid creating unnecessary instances
            if targetConfigOps:
                for capability in resource.capabilities:
                    for relationship in capability.relationships:
                        # we're the target
                        req = create_task_request(
                            self.jobOptions,
                            "Configure.add_target",
                            relationship,
                            reason,
                            inputs,
                        )
                        if req:
                            yield req

    def generate_delete_configurations(
        self, include_test: Callable[[EntityInstance], Optional[str]]
    ) -> Iterator[Union[TaskRequest, TaskRequestGroup]]:
        # called by prune and the undeploy plan
        seen: Set[int] = set()
        test = partial(self.should_delete, include_test)
        for child, reason in select_dependents(self.root, test, seen):
            if reason != "cancelled":
                # eventually calls execute_default_undeploy() unless custom workflow
                yield from self._generate_delete_configurations(child, reason)

    def should_delete(
        self,
        include: Callable[[EntityInstance], Optional[str]],
        resource: EntityInstance,
    ) -> Optional[str]:
        # If the resource
        """
        If the resource should be deleted, return the reason.
        Otherwise, return None or "cancelled" if its dependencies' deletions should be cancelled.
        """
        skip = None
        if resource is self.root:
            return None
        # NB: the order of these tests is important!
        virtual = False
        if resource.shadow or resource.template.abstract:
            skip = "read-only instance"
        elif "protected" in resource.template.directives:
            skip = 'instance with "protected" directive'
        elif resource.protected:
            skip = "protected instance"
        elif (
            resource.template.aggregate_only()
            or "virtual" in resource.template.directives
        ):
            skip = "virtual instance"
            virtual = True
        elif not self.jobOptions.destroyunmanaged:
            if not resource.created:
                skip = "instance wasn't created by this ensemble (use --destroyunmanaged to override)"
            elif resource.is_managed():
                skip = f"creation and deletion is managed by another instance {resource.created} (use --destroyunmanaged to override)"
        elif resource.local_status in [Status.absent, Status.pending]:
            skip = "instance doesn't exist"

        reason = include(resource)  # returns a Reason to include
        if not reason:
            skip = "didn't meet removal criteria"
        if skip:
            cancelling = (
                not virtual  # instance doesn't need to be preserved
                and not self.jobOptions.force
                and resource.operational
                and resource.required
            )
            logger.verbose(
                "skip instance '%s' for removal: '%s' %s",
                resource.name,
                skip,
                "(cancelling)" if cancelling else "",
            )
            if cancelling:
                return "cancelled"
            else:
                return None
        return reason

    def _generate_delete_configurations(self, resource: EntityInstance, reason: str):
        # if the resource doesn't instantiate itself just mark it as absent because otherwise will be in error
        # (because it will remain with status ok while its dependents are absent)
        if resource.template.aggregate_only():
            resource._localStatus = Status.absent
        if isinstance(resource, NodeInstance):
            workflow = "undeploy" if reason == Reason.prune else self.workflow
            yield from self._generate_configurations(resource, reason, workflow)

    def _get_default_generator(
        self,
        workflow: str,
        resource: NodeInstance,
        reason: Optional[str] = None,
        inputs=None,
    ):
        if workflow == "deploy":
            return self.execute_default_deploy(resource, reason, inputs)
        elif workflow == "undeploy" or workflow == "stop":
            return self.execute_default_undeploy(resource, reason, inputs)
        elif workflow == "check" or workflow == "discover" or workflow == "connect":
            return self.execute_default_install_op(workflow, resource, reason, inputs)
        return None

    def _get_connection_task(self, taskRequest) -> Iterator[TaskRequest]:
        if not self._checked_connection_task:
            self._checked_connection_task = True
            if self.tosca.topology and self.tosca.topology.primary_provider:
                for rel in self.root.default_relationships:
                    if rel.name == "primary_provider":
                        req = create_task_request(self.jobOptions, "Install.check", rel)
                        if req:
                            yield req
                        return
                assert False, self.root.default_relationships

    def _generate_configurations(
        self,
        resource: "NodeInstance",
        reason: Optional[str],
        workflow: Optional[str] = None,
    ) -> Iterator[Union[TaskRequest, TaskRequestGroup]]:
        # note: workflow parameter might be an installOp
        workflow = workflow or self.workflow
        # check if this workflow has been delegated to one explicitly declared
        configGenerator = self.execute_workflow(workflow, resource)
        if configGenerator:
            custom_workflow = True
        else:
            configGenerator = self._get_default_generator(workflow, resource, reason)
            if not configGenerator:
                raise UnfurlError("can not get default for workflow " + workflow)
            custom_workflow = False

        # if the workflow is one that can modify a target, create a TaskRequestGroup
        if workflow != "check":
            group = TaskRequestGroup(resource, workflow)
        else:
            group = None
        task_found = False
        for taskRequest in configGenerator:
            if taskRequest:
                task_found = True
                yield from self._get_connection_task(taskRequest)
                if group and not isinstance(taskRequest, JobRequest):
                    # if taskRequest.target == group.target:
                    group.add_request(taskRequest)
                    if isinstance(taskRequest, TaskRequest):
                        taskRequest.set_final_for_workflow(
                            bool(self.is_last_workflow_op(taskRequest))
                        )
                else:
                    yield taskRequest
        if not task_found:
            logger.verbose(
                f'No operations for workflow "{workflow}" defined for instance "{resource.nested_name}" (type "{resource.type}")'
            )
        if group:
            if custom_workflow:
                group.set_final_for_workflow(True)
            yield group

    def execute_workflow(
        self, workflowName: str, resource
    ) -> Optional[Iterator[TaskRequestGroup]]:
        workflow = self.tosca.get_workflow(workflowName)
        if not workflow:
            return None
        if not workflow.match_preconditions(resource):  # check precondition
            return None
        steps = [
            step
            for step in workflow.initial_steps()
            # XXX check target_relationship too
            # XXX target can be a group name too
            if resource.template.is_compatible_target(step.target)
        ]
        if not steps:
            return None
        try:
            # push resource._workflow_inputs
            return self.execute_steps(workflow, steps, resource)
        finally:
            pass  # pop _workflow_inputs

    def execute_steps(
        self, workflow: Workflow, steps: list, resource: NodeInstance
    ) -> Generator[TaskRequestGroup, Any, None]:
        queue = steps[:]
        while queue:
            step = queue.pop()
            if not workflow.match_step_filter(step.name, resource):
                logger.debug(
                    "step did not match filter %s with %s", step.name, resource.name
                )
                continue
            stepGenerator = self.execute_step(step, resource, workflow)
            result = None
            try:
                while True:
                    task = stepGenerator.send(result)
                    if isinstance(task, list):  # more steps
                        queue.extend([workflow.get_step(stepName) for stepName in task])
                        break
                    else:
                        result = yield task
            except StopIteration:
                pass

    def execute_step(
        self, step, resource: NodeInstance, workflow: Workflow
    ) -> Generator[Union[TaskRequestGroup, list], Any, None]:
        logger.debug("executing step %s for %s", step.name, resource.name)
        reqGroup = TaskRequestGroup(resource, workflow.workflow.name)
        for activity in step.activities:
            if activity.type == "inline":
                # XXX inputs
                workflowGenerator = self.execute_workflow(activity.inline, resource)
                if not workflowGenerator:
                    continue
                for result in workflowGenerator:
                    if result:
                        reqGroup.add_request(result)
            elif activity.type == "call_operation":
                # XXX need to pass operation_host (see 3.6.27 Workflow step definition p188)
                # if target is a group can be value can be node_type or node template name
                # if its a node_type select nodes matching the group
                req = create_task_request(
                    self.jobOptions,
                    activity.call_operation,
                    resource,
                    "step:" + step.name,
                    activity.inputs,
                )
                if req:
                    reqGroup.add_request(req)
            elif activity.type == "set_state":
                reqGroup.add_request(SetStateRequest(resource, activity.set_state))
            elif activity.type == "delegate":
                # XXX inputs
                configGenerator = self._get_default_generator(
                    activity.delegate, resource, activity.delegate
                )
                if not configGenerator:
                    continue
                for req in filter(None, configGenerator):
                    reqGroup.add_request(req)

        # XXX  yield step.on_failure  # list of steps
        yield reqGroup
        yield step.on_success  # list of steps

    def _get_templates(self) -> List[NodeSpec]:
        assert self.tosca.topology
        filter = self.filterTemplate and self.filterTemplate.name
        seen: Dict[EntitySpec, EntitySpec.ReferenceType] = {}
        # order by ancestors
        return list(
            self._get_templates_from_topology(self.tosca.topology, seen, filter)
        )

    def _get_templates_from_topology(
        self,
        topology: TopologySpec,
        seen: Dict[EntitySpec, EntitySpec.ReferenceType],
        filter=None,
    ) -> Iterator[NodeSpec]:
        templates = topology.node_templates.values()
        # order by ancestors
        return self.order_templates(
            {
                t.name: t
                for t in templates
                if "virtual" not in t.directives
                and interface_requirements_ok(self.root, t)
                # only include conditional templates if all their requirements are met
                and (
                    "conditional" not in t.directives
                    or not t.toscaEntityTemplate.missing_requirements
                )
                and (not filter or t.name == filter)
            },
            seen,
        )

    def order_templates(
        self,
        templates: Dict[str, NodeSpec],
        seen: Dict[EntitySpec, EntitySpec.ReferenceType],
    ) -> Iterator[NodeSpec]:
        """Order templates so dependencies are yielded first."""
        for source in templates.values():
            if self.interface:
                for operation_host in find_explicit_operation_hosts(
                    source, self.interface
                ):
                    operationHostSpec = templates.get(operation_host)
                    if operationHostSpec:
                        if operationHostSpec is not source:
                            operationHostSpec.add_reference(
                                source, operationHostSpec.ReferenceType.OperationHost
                            )
                        if operationHostSpec in seen:
                            continue
                        seen[operationHostSpec] = (
                            operationHostSpec.ReferenceType.OperationHost
                        )
                        yield operationHostSpec

            # skip if we already encountered source while running get_ancestor_templates()
            if EntitySpec._has_reference(
                seen, source, EntitySpec.ReferenceType.Requirement
            ):
                continue

            for nodespec in get_ancestor_templates([source], templates):
                if nodespec is not source:
                    # nodespec ancestor is required by source
                    nodespec.add_reference(source, nodespec.ReferenceType.Requirement)
                flags = seen.get(nodespec)
                if not flags or not flags & EntitySpec.ReferenceType.Requirement:
                    # setting nodespec now means we won't add nodespec references to other nodes
                    # (because of the _has_reference check above)
                    # so essentially, given a lineage, only the first one encountered in the topology will be in other node's _isReferencedBy
                    # (which fine if we only use it to see if a node connected to the root node (see _get_roots()))
                    seen[nodespec] = nodespec.ReferenceType.Requirement
                    if nodespec.substitution:
                        yield from self._get_templates_from_topology(
                            nodespec.substitution, seen
                        )
                    if not flags:  # skip if already yielded as a operation host
                        yield nodespec

    def include_not_found(self, template):
        return True

    def is_last_workflow_op(self, taskrequest: TaskRequest) -> str:
        return ""

    def _generate_workflow_configurations(
        self, instance: NodeInstance, oldTemplate: Optional[NodeSpec]
    ):
        yield from self._generate_configurations(instance, self.workflow)

    def execute_plan(self) -> Iterator[Union[TaskRequest, TaskRequestGroup]]:
        """
        Generate candidate tasks

        yields TaskRequests
        """
        opts = self.jobOptions
        templates = self._get_templates()

        logger.verbose(
            "checking for tasks for templates %s", [t.nested_name for t in templates]
        )
        visited = set()
        for template in templates:
            found = False
            resource: Optional[NodeInstance]
            for resource in self.find_resources_from_template(template):
                found = True
                visited.add(id(resource))
                yield from self._generate_workflow_configurations(resource, template)

            if not found:
                if not template._isReferencedBy and "dependent" in template.directives:
                    continue
                abstract = template.abstract
                if abstract == "select":
                    continue
                include = abstract == "substitute" or self.include_not_found(template)
                if include:
                    resource = self.create_resource(template)
                    if not resource:
                        raise UnfurlError(
                            f'could not create instance from template "{template.nested_name}"'
                        )
                    visited.add(id(resource))
                    if abstract != "substitute":
                        yield from self._generate_workflow_configurations(
                            resource, None
                        )

        if opts.prune:
            # XXX warn or error if prune used with a filter option
            test = lambda resource: (
                Reason.prune if id(resource) not in visited else None
            )
            yield from self.generate_delete_configurations(test)


class DeployPlan(Plan):
    interface = "Standard"

    def include_not_found(self, template):
        if self.jobOptions.add or self.jobOptions.force:
            return Reason.add
        return None

    def include_instance(
        self, template: EntitySpec, instance: EntityInstance
    ) -> Optional[str]:
        """Return whether or not the given instance should be included in the current plan,
        based on the current job's options and whether the template changed or the instance in need of repair?

        Args:
            template (EntitySpec): The template to be deployed.
            instance (EntityInstance): The instance as it last deployed.

        Returns:
            (Reason): Returns a string from the `:class:Reason` enumeration or `None` if it should not be included.
        """
        assert template and instance
        jobOptions = self.jobOptions
        if jobOptions.force:
            return Reason.force

        status = instance.local_status or Status.unknown
        if jobOptions.add and not jobOptions.skip_new and status != Status.ok:
            if not instance.last_change:  # never instantiated before
                return Reason.add

            if status in [Status.unknown, Status.pending, Status.absent]:
                return Reason.missing

        # if the specification changed:
        old_template = instance.template
        if jobOptions.change_detection != "skip" or jobOptions.upgrade:
            # XXX currently old_template is the same as template (we don't load the instance's version)
            if template != old_template:
                # only apply the new configuration if doesn't result in a major version change
                if True:  # XXX if isMinorDifference(template, oldTemplate)
                    return Reason.update
                elif jobOptions.upgrade:
                    return Reason.upgrade

        reason = self.check_for_repair(instance)
        # there isn't a new config to run, see if the last applied config needs to be re-run
        if not reason:
            if "check" in instance.template.directives:
                # always triggers "check" operation if set
                instance.local_status = Status.unknown
                reason = Reason.check
            elif status == Status.pending and self.jobOptions.check:
                reason = Reason.check
            elif jobOptions.change_detection != "skip" and instance.last_config_change:
                # customized is only set if created first!
                # when should reconfigure run on discovered resources? (currently never runs because no config changeset is found)
                # discover would have to calculate digest for configure!
                if (
                    not instance.customized and not instance.is_managed()
                ) or jobOptions.change_detection == "always":
                    return Reason.reconfigure
        return reason

    def check_for_repair(self, instance) -> Optional[str]:
        jobOptions = self.jobOptions
        assert instance
        if jobOptions.repair == "none":
            return None
        status = instance.local_status or Status.unknown

        if status in [Status.unknown, Status.pending]:
            if instance.required:
                status = Status.error  # treat as error
            else:
                return None

        if status not in [Status.degraded, Status.error]:
            return None

        if jobOptions.repair == "degraded":
            assert status > Status.ok, status
            return Reason.degraded  # repair this
        elif status == Status.degraded:
            assert jobOptions.repair == "error", jobOptions.repair
            return None  # skip repairing this
        else:
            assert jobOptions.repair == "error", (
                f"repair: {jobOptions.repair} status: {status}"
            )
            return Reason.repair  # repair this

    def is_instance_read_only(self, instance):
        return instance.shadow or "discover" in instance.template.directives

    def _generate_workflow_configurations(
        self, instance: NodeInstance, oldTemplate: Optional[NodeSpec]
    ):
        filter_reason = filter_config(self.jobOptions, "", instance)
        if filter_reason:
            if not filter_reason.startswith("instance"):
                logger.debug(
                    "skipping %s task: doesn't match filter: '%s'",
                    instance.nested_name,
                    filter_reason,
                )
            return
        if instance.shadow:
            reason: Optional[str] = Reason.connect
        elif oldTemplate:
            # this is an existing instance, so check if we should include it
            reason = self.include_instance(oldTemplate, instance)
            if not reason:
                logger.debug(
                    "not including task for %s:%s (status: %s)",
                    instance.name,
                    oldTemplate.name,
                    instance.status,
                )
                return
        else:  # this is newly created resource
            reason = Reason.add

        status = instance.local_status or Status.unknown
        if instance.shadow:
            installOp = "connect"
        elif status == Status.unknown or (
            status == Status.pending and self.jobOptions.check
        ):
            installOp = "check"
        elif "discover" in instance.template.directives and not instance.operational:
            installOp = "discover"
        else:
            installOp = None

        if installOp:
            yield from self._generate_configurations(instance, installOp, installOp)

            if self.is_instance_read_only(instance):
                return  # we're done

        if reason in (Reason.reconfigure, Reason.update, Reason.upgrade):
            yield from self._generate_reconfigure(instance, reason)
        else:
            yield from self._generate_configurations(instance, reason)

    def _generate_reconfigure(self, instance, reason):
        # XXX generate configurations: may need to stop, start, etc.
        if "discover" in instance.template.directives:
            op = "Install.discover"
            startState = None
        else:
            op = "Standard.configure"
            startState = NodeState.configuring
        req = create_task_request(
            self.jobOptions,
            op,
            instance,
            reason,
            startState=startState,
        )
        if req:
            yield req
        else:
            logger.verbose(
                f'No operation for "reconfigure" defined for instance "{instance.nested_name}" (type "{instance.type}")'
            )

    def is_last_workflow_op(self, taskrequest: TaskRequest) -> str:
        interface = taskrequest.configSpec.interface
        if interface == "Standard":
            order = ["create", "configure", "start"]
        elif interface == "Configure":
            order = [
                "pre_configure_target",
                "pre_configure_source",
                "post_configure_target",
                "post_configure_source",
            ]
        else:
            return ""
        implemented = [
            op
            for op in order
            if _find_implementation(interface, op, taskrequest.target.template)
        ]
        if taskrequest.configSpec.operation in implemented:
            index = implemented.index(taskrequest.configSpec.operation)
            if len(implemented) == index + 1:
                return implemented[index]
        return ""


class UndeployPlan(Plan):
    def execute_plan(self) -> Iterator[Union[TaskRequest, TaskRequestGroup]]:
        """
        yields configSpec, target, reason
        """
        yield from self.generate_delete_configurations(self.include_for_deletion)

    def include_for_deletion(self, instance) -> Optional[str]:
        if self.filterTemplate and instance.template != self.filterTemplate:
            return None

        # return value is used as "reason"
        return self.workflow

    def is_last_workflow_op(self, taskrequest: TaskRequest) -> str:
        interface = taskrequest.configSpec.interface
        if interface == "Standard":
            if self.workflow == "stop":
                order = ["stop"]
            else:
                order = ["stop", "delete"]
        elif interface == "Configure":
            order = ["remove_target", "remove_source"]
        else:
            return ""
        implemented = [
            op
            for op in order
            if _find_implementation(interface, op, taskrequest.target.template)
        ]
        if taskrequest.configSpec.operation in implemented:
            index = implemented.index(taskrequest.configSpec.operation)
            if len(implemented) == index + 1:
                return implemented[index]
        return ""


class ReadOnlyPlan(Plan):
    interface = "Install"


class WorkflowPlan(Plan):
    def execute_plan(self) -> Iterator[Union[TaskRequest, TaskRequestGroup]]:
        """
        yields configSpec, target, reason
        """
        workflow = self.tosca.get_workflow(self.jobOptions.workflow)
        if not workflow:
            raise UnfurlError(f'workflow not found: "{self.jobOptions.workflow}"')
        for step in workflow.initial_steps():
            if self.filterTemplate and not self.filterTemplate.is_compatible_target(
                step.target
            ):
                continue
            if self.tosca.is_type_name(step.target):
                templates: Iterable[NodeSpec] = (
                    workflow.topology.find_matching_templates(step.target)
                )
            else:
                template = workflow.topology.get_node_template(step.target)
                if not template:
                    continue
                templates = [template]

            for template in templates:
                assert template
                for resource in self.find_resources_from_template(template):
                    gen = self.execute_steps(workflow, [step], resource)
                    result = None
                    try:
                        while True:
                            configuration = gen.send(result)
                            result = yield configuration
                    except StopIteration:
                        pass


class RunNowPlan(Plan):
    def _create_configurator(self, args, action, inputs=None, timeout=None):
        if args.get("module") or args.get("host"):
            className = "unfurl.configurators.ansible.AnsibleConfigurator"
            module = args.get("module") or "command"
            module_args = " ".join(args["cmdline"])
            # creates a playbook with a single ansible task:
            params = dict(playbook=[{module: module_args}])
        else:
            className = "unfurl.configurators.shell.ShellConfigurator"
            params = dict(command=list(args["cmdline"]))

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

    def execute_plan(self) -> Iterator[Union[TaskRequest, TaskRequestGroup]]:
        instances = self.jobOptions.instances
        if instances:
            resources = []
            for instance_name in instances:
                assert isinstance(instance_name, str)
                resource = self.root.find_resource(instance_name)
                if not resource:
                    # see if there's a template with the same name and create the resource
                    template = self.tosca.get_template(instance_name)
                    if template:
                        assert isinstance(template, NodeSpec)
                        resource = self.create_resource(template)
                    if not resource:
                        raise UnfurlError(
                            f"specified instance not found: {instance_name}"
                        )
                resources.append(resource)

        # userConfig has the job options explicitly set by the user
        operation = cast(Optional[str], self.jobOptions.userConfig.get("operation"))
        operation_host = self.jobOptions.userConfig.get("host")
        if not operation:
            configSpec = self._create_configurator(self.jobOptions.userConfig, "run")
            if not instances:
                resources = [self.root]
        else:
            configSpec = None
            interface, sep, action = operation.rpartition(".")
            if not interface and find_standard_interface(operation):  # shortcut
                operation = find_standard_interface(operation) + "." + operation
            if not instances:
                resources = [
                    r
                    for t in self._get_templates()
                    for r in self.find_resources_from_template(t)
                ]

        for resource in resources:
            if configSpec:
                req = filter_task_request(
                    self.jobOptions, TaskRequest(configSpec, resource, Reason.run)
                )
                if req:
                    yield req
            else:
                assert operation
                req = create_task_request(
                    self.jobOptions,
                    operation,
                    resource,
                    Reason.run,
                    operation_host=operation_host,
                )
                if req:  # if operation was found:
                    yield req


def find_explicit_operation_hosts(template, interface):
    for iDef in template.get_interfaces():
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


def interface_requirements_ok(root: TopologyInstance, template: NodeSpec):
    reqs = template.get_interface_requirements()
    if reqs:
        assert isinstance(reqs, list)
        for req in reqs:
            if not any(
                [
                    rel.template.is_compatible_type(req)
                    for rel in root.default_relationships
                ]
            ):
                logger.debug(
                    'Skipping template "%s": could not find a connection for interface requirements: %s',
                    template.name,
                    reqs,
                )
                return False
    return True


def get_ancestor_templates(
    stack: List[NodeSpec], templates: Dict[str, NodeSpec]
) -> Iterator[NodeSpec]:
    """Yield transitive requirements from topmost to self"""
    # maintain a stack to avoid circular dependencies
    source = stack[-1]
    if not source.toscaEntityTemplate.is_replaced_by_outer():
        if source.abstract != "select":
            for req in source.requirements:
                target = req.relationship and req.relationship.target
                if target and target not in stack:
                    for ancestor in get_ancestor_templates(stack + [target], templates):
                        yield ancestor
        yield source


def get_operational_dependents(
    resource: EntityInstance, seen: Set[int]
) -> Iterator[EntityInstance]:
    if seen is None:
        seen = set()
    for dep in resource.get_operational_dependents():
        if id(dep) not in seen:
            seen.add(id(dep))
            assert isinstance(dep, EntityInstance)
            for child in get_operational_dependents(dep, seen):
                yield child
            yield dep


def select_dependents(
    resource: EntityInstance, include_test, seen: Set[int]
) -> Iterator[Tuple[EntityInstance, str]]:
    # yields resource, include_reason
    cancelled = []
    root_include_reason = include_test(resource)
    for dep in resource.get_operational_dependents():
        assert isinstance(dep, EntityInstance)
        if id(dep) in seen:
            continue
        seen.add(id(dep))
        for child, include_reason in select_dependents(dep, include_test, seen):
            if include_reason != "cancelled":
                yield child, include_reason
            else:
                cancelled.append(child)
    if cancelled:
        # this resource has a dependent we don't want to delete, so cancel deleting the resource
        if root_include_reason and root_include_reason != "cancelled":
            logger.verbose(
                "skip instance '%s' for removal: required instances depend on it: %s (use --force to override)",
                resource.name,
                [c.nested_name for c in cancelled],
            )
        yield resource, "cancelled"
    elif root_include_reason:
        yield resource, root_include_reason
    # otherwise don't yield this resource
