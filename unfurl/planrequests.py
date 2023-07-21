# Copyright (c) 2021 Adam Souzis
# SPDX-License-Identifier: MIT
import collections
import re
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    cast,
)
import shlex
import sys
import os
import os.path
from toscaparser.elements.interfaces import OperationDef
from toscaparser.nodetemplate import NodeTemplate
from .tosca import EntitySpec

if TYPE_CHECKING:
    from .job import Job, ConfigTask, JobOptions
    from .configurator import Dependency
    from .manifest import Manifest

from .util import (
    lookup_class,
    load_module,
    find_schema_errors,
    UnfurlError,
    UnfurlTaskError,
    to_enum,
)
from .result import Result, ResultsList, serialize_value
from .support import Defaults, NodeState, Priority, Status
from .runtime import EntityInstance, InstanceKey, HasInstancesInstance, TopologyInstance
from .logs import getLogger
import logging

logger = getLogger("unfurl")


# we want ConfigurationSpec to be independent of our object model and easily serializable
class ConfigurationSpec:
    @classmethod
    def getDefaults(cls):
        return dict(
            className=None,
            majorVersion=0,
            minorVersion="",
            workflow=Defaults.workflow,
            timeout=None,
            operation_host=None,
            environment=None,
            inputs=None,
            inputSchema=None,
            preConditions=None,
            postConditions=None,
            primary=None,
            dependencies=None,
            outputs=None,
            interface=None,
            entry_state=None,
            base_dir=None,
        )

    def __init__(
        self,
        name,
        operation,
        className=None,
        majorVersion=0,
        minorVersion="",
        workflow=Defaults.workflow,
        timeout=None,
        operation_host=None,
        environment=None,
        inputs=None,
        inputSchema=None,
        preConditions=None,
        postConditions=None,
        primary=None,
        dependencies=None,
        outputs=None,
        interface=None,
        entry_state=None,
        base_dir=None,
    ):
        assert name and className, "missing required arguments"
        self.name = name
        self.operation = operation
        self.className = className
        self.majorVersion = majorVersion
        self.minorVersion = minorVersion
        self.workflow = workflow
        self.timeout = timeout
        self.operation_host = operation_host
        self.environment = environment
        self.inputs = inputs or {}
        self.inputSchema = inputSchema
        self.outputs = outputs or {}
        self.preConditions = preConditions
        self.postConditions = postConditions
        self.artifact = primary
        self.dependencies = dependencies
        self.interface = interface
        self.entry_state = to_enum(NodeState, entry_state, NodeState.creating)
        self.base_dir = base_dir

    def find_invalidate_inputs(self, inputs):
        if not self.inputSchema:
            return []
        return find_schema_errors(serialize_value(inputs), self.inputSchema)

    # XXX same for postConditions
    def find_invalid_preconditions(self, target):
        if not self.preConditions:
            return []
        # XXX this should be like a Dependency object
        expanded = serialize_value(target.attributes)
        return find_schema_errors(expanded, self.preConditions)

    def create(self):
        className = self.className
        klass = lookup_class(className)
        if not klass:
            raise UnfurlError(f"Could not load configurator {self.className}")
        else:
            assert callable(klass)
            return klass(self)

    def should_run(self):
        return Defaults.shouldRun

    def copy(self, **mods):
        args = self.__dict__.copy()
        args.update(mods)
        return ConfigurationSpec(**args)

    def __eq__(self, other):
        if not isinstance(other, ConfigurationSpec):
            return False
        return (
            self.name == other.name
            and self.operation == other.operation
            and self.className == other.className
            and self.majorVersion == other.majorVersion
            and self.minorVersion == other.minorVersion
            and self.workflow == other.workflow
            and self.timeout == other.timeout
            and self.environment == other.environment
            and self.inputs == other.inputs
            and self.inputSchema == other.inputSchema
            and self.outputs == other.outputs
            and self.preConditions == other.preConditions
            and self.postConditions == other.postConditions
            and self.interface == other.interface
        )


class PlanRequest:
    error: Union[bool, str] = False
    task: Optional["ConfigTask"] = None
    render_errors: Optional[List[UnfurlTaskError]] = None
    completed = False
    group: Optional["TaskRequestGroup"] = None
    required: Optional[bool] = None
    is_final_for_workflow: Optional[bool] = None

    def __init__(self, target: "EntityInstance"):
        self.target = target
        self.dependencies: List["Dependency"] = []

    def set_error(self, msg: str):
        self.error = msg
        if self.task:
            self.task.local_status = Status.error
            self.task.result = msg

    @property
    def root(self) -> Optional["EntityInstance"]:
        return self.target.root if self.target else None

    def add_dependencies(self, dependencies: List["Dependency"]) -> List["Dependency"]:
        self.dependencies.extend(dependencies)
        return self.dependencies

    def get_operation_artifacts(self):
        return []

    def include_in_plan(self):
        if self.task and self.task.priority == Priority.critical:
            return True  # XXX hackish, just used for primary_provider
        # calls self.target.template.required if _priority is None
        priority = self.target.priority
        if priority is not None:
            return priority >= Priority.required
        if self.target.created:
            #  if already created then always include the resource
            return True
        return False

    def has_unfulfilled_refs(self) -> bool:
        for dep in self.get_unfulfilled_refs():
            return True
        return False

    def get_unfulfilled_refs(self, check_target="") -> Iterator["Dependency"]:
        for dep in self.dependencies:
            if check_target:
                if not dep.validate():
                    yield dep
                if (
                    check_target == "operational"
                    and dep.target
                    and dep.target != self.target
                ):
                    if dep.target.local_status not in [Status.ok, Status.degraded]:
                        logger.trace(
                            f"{dep} not operational with local status {dep.target.local_status and dep.target.local_status.name}"
                        )
                        yield dep
                # otherwise, dep is fulfilled
            else:
                yield dep
        if not self.render_errors:
            return
        for error in self.render_errors:
            # result.py::_validation_error() creates a UnfurlTaskError with a Dependency
            more_dep = cast(Optional["Dependency"], getattr(error, "dependency", None))
            if more_dep:
                yield more_dep

    def update_unfulfilled_errors(self) -> bool:
        # remove unfulfilled dependencies and errors caused by an unfulfilled dependency
        self.render_errors = list(self._revalidate_unfulfilled_errors())
        # note: this isn't called during the last round because we don't want the has_changed() check
        self.dependencies = [
            dep
            for dep in self.dependencies
            if not dep.validate() or not dep.has_changed()
        ]
        # logger.trace(
        #     f"update_unfulfilled_errors {self.target.name}: {self.dependencies}"
        # )
        return self.has_unfulfilled_refs()

    def _revalidate_unfulfilled_errors(self) -> Iterator["UnfurlTaskError"]:
        if not self.render_errors:
            return
        for error in self.render_errors:
            dep = cast(Optional["Dependency"], getattr(error, "dependency", None))
            if not dep or not dep.validate():
                # if dep:
                #     logger.trace(
                #         f"in render_errors: invalid dep {dep} for {self.target.name}"
                #     )
                yield error

    def _set_dependencies(
        self,
        live_dependencies: Dict[InstanceKey, List["Dependency"]],
        requests: Sequence["PlanRequest"],
    ) -> None:
        # iterate through the live attributes the given task depends
        # if the attribute's instance might be modified by another task, mark that task as a dependency
        self.dependencies = []  # reset
        for r in requests:
            if self.target.key == r.target.key:
                continue  # skip self
            if r.task and r.task.completed:
                continue  # already ran
            if self.required is not None and not self.required:
                logger.trace(f"skipping not required {self}")
                continue  # skip requests that aren't going to run
            dependencies = live_dependencies.get(r.target.key)
            if dependencies:
                logger.trace(
                    "%s dependencies on future task %s: %s",
                    self.target.key,
                    r.target.key,
                    dependencies,
                )
                self.add_dependencies(dependencies)

    @property
    def previous(self) -> Optional["PlanRequest"]:
        return self.group and self.group.get_previous(self) or None

    @property
    def not_ready(self) -> bool:
        if self.previous and self.previous.not_ready:
            return True
        return bool(self.has_unfulfilled_refs())

    @property
    def name(self):
        return type(self).__name__

    def get_notready_message(self) -> str:
        start = "Never ran:"
        msg = start
        if self.render_errors:
            render_deps = [
                error.dependency.name  # type: ignore
                for error in self.render_errors
                if getattr(error, "dependency", None)
            ]
            msg += f" invalid dependencies: {render_deps}"
        if self.dependencies:
            not_operational = [
                dep.name
                for dep in self.dependencies
                if dep.target
                and dep.target.local_status not in [Status.ok, Status.degraded]
            ]
            invalid_deps = [
                dep.name for dep in self.dependencies if dep not in not_operational
            ]
            if msg != start:
                msg += " and "
            if not_operational:
                msg += f"non-operational dependencies: {not_operational}."
            if msg != start:
                msg += " and "
            if invalid_deps:
                msg += f" unfulfilled dependencies: {invalid_deps}."
        if msg != start:
            return msg
        if self.group and self.group.target.name != self.target.name:
            return f"Never ran: parent resource in error: {self.group.target.name}"
        elif self.previous:
            return f'Never ran: previous operation "{self.previous}" failed or could not run'
        else:
            return "Couldn't run"

    def set_final_for_workflow(self, is_final: bool):
        self.is_final_for_workflow = is_final

    def __str__(self) -> str:
        return f'Planned task {self.name} for "{self.target.name}"'


class TaskRequest(PlanRequest):
    """
    Yield this to run a child task. (see :py:meth:`unfurl.configurator.TaskView.create_sub_task`)
    """

    def __init__(
        self,
        configSpec: ConfigurationSpec,
        target: EntityInstance,
        reason: str,
        persist: bool = False,
        required: Optional[bool] = None,
        startState: Optional[NodeState] = None,
    ):
        super().__init__(target)
        self.configSpec = configSpec
        self.reason = reason
        self.persist = persist
        self.required = required
        self.error = configSpec.name == "#error"
        self.startState = startState
        self.task = None
        self._completed = False

    def __completed():  # type: ignore
        def fget(self) -> bool:
            return bool(self._completed or self.task and self.task.completed)

        def fset(self, value: bool):
            self._completed = value

        return locals()

    completed: bool = property(**__completed())  # type: ignore

    def reassign_final_for_workflow(self) -> Optional["TaskRequest"]:
        req = self
        group = req.group
        if group and req.is_final_for_workflow:
            previous = group.get_previous(req)
            while previous:
                if (
                    previous.target == req.target
                    and previous.required != False
                    and isinstance(previous, TaskRequest)
                ):
                    previous.is_final_for_workflow = True
                    return previous
                previous = group.get_previous(previous)
        return None

    def finish_workflow(self) -> None:
        # This is called on the last task apply to a resource in a work flow.
        # If the all the task succeeded and if target hasn't had its status set already,
        # set the target status to workflow's desired status.
        task = self.task
        assert task
        assert task.completed  # task.finished() has been called
        if task.local_status == Status.error:
            # if failed then task._update_status() set error state on the target
            return
        workflow = self.group and self.group.workflow
        if not workflow:  # not in a group or not in a mutable workflow
            return
        explicit_status = task.result and task.result.status
        if explicit_status is None:
            # even though we didn't modify the target this is the last op and the task succeeded
            # so assume the target is that the workflow's final state
            explicit_status = get_success_status(workflow)
            # target's status needs to change
        task.logger.trace(
            "finish workflow with %s status %s for workflow %s with local_status %s created: %s",
            task.target.name,
            "None" if explicit_status is None else explicit_status.name,
            workflow,
            task.target.local_status,  # type: ignore
            task.target.created,
        )
        task._finished_workflow(explicit_status, workflow)

    def _get_artifact_plan(self, artifact):
        # the artifact has an interface so it needs to be installed on the operation_host
        if artifact and artifact.get_interfaces():
            # the same global artifact can have different local names when declared on a node template
            # but are uniquely identified by (file, repository) so use that to generate a unique node template name
            name = "__artifact__" + artifact.get_name_from_artifact_spec(
                artifact.as_import_spec()
            )
            operation_host = (
                find_operation_host(self.target, self.configSpec.operation_host)
                or self.target.apex
            )
            existing = operation_host.root.find_instance(name)
            if existing:
                if existing.operational:
                    return None
                else:
                    return JobRequest([existing])
            else:
                if not operation_host.template.get_template(name):
                    # template isn't defined, define inline
                    artifact_tpl = artifact.toscaEntityTemplate.entity_tpl
                    template = dict(
                        name=name,
                        directives=["protected"],
                        type="unfurl.nodes.ArtifactInstaller",
                        artifacts={"install": artifact_tpl},
                    )
                    artifact_type = artifact_tpl["type"]
                    if (
                        artifact_type
                        not in operation_host.template.topology.topology_template.custom_defs
                    ):
                        # operation_host must be in an external ensemble that doesn't have the type def
                        artifact_type_def = self.target.template.topology.topology_template.custom_defs[
                            artifact_type
                        ]
                        template["custom_types"] = {artifact_type: artifact_type_def}
                else:
                    template = name

                return JobRequest(
                    [operation_host],
                    update=dict(
                        name=name,
                        parent=operation_host.name,
                        template=template,
                        attributes=artifact.properties,
                    ),
                )
        return None

    def get_operation_artifacts(self):
        artifacts = []
        if self.configSpec.dependencies:
            for artifact in self.configSpec.dependencies:
                jobRequest = self._get_artifact_plan(artifact)
                if jobRequest:
                    artifacts.append(jobRequest)
        jobRequest = self._get_artifact_plan(self.configSpec.artifact)
        if jobRequest:
            artifacts.append(jobRequest)
        return artifacts

    @property
    def name(self):
        if self.configSpec.operation:
            name = self.configSpec.operation
        else:
            name = self.configSpec.name
        if self.reason and self.reason not in name:
            return name + " (reason: " + self.reason + ")"
        return name

    def _summary_dict(self, include_rendered=True):
        summary = dict(
            operation=self.configSpec.operation or self.configSpec.name,
            reason=self.reason,
        )
        rendered = {}
        if self.task and self.task._workFolders:
            for name, wf in self.task._workFolders.items():
                rendered[name] = wf.cwd
        if include_rendered:
            summary["rendered"] = rendered
        return summary

    def __repr__(self):
        state = " " + (self.target.state and self.target.state.name or "")
        return (
            f"TaskRequest({self.target}({self.target.status.name}{state}):{self.name})"
        )


class SetStateRequest(PlanRequest):
    def __init__(self, target, state):
        super().__init__(target)
        self.set_state = state

    @property
    def name(self):
        return self.set_state

    def _summary_dict(self):
        return dict(set_state=self.set_state)

    def set_final_for_workflow(self, is_final: bool):
        assert self.group
        previous = self.group.get_previous(self)
        if previous:
            previous.set_final_for_workflow(is_final)


class TaskRequestGroup(PlanRequest):
    """
    A TaskRequestGroup is a list of task requests that need to succeed to advance
    the target NodeInstance to the workflow's desired status (e.g. ok or absent)
    It is created by TOSCA's standard operations or by a custom workflow.
    """

    def __init__(self, target: EntityInstance, workflow: str):
        super().__init__(target)
        self.workflow = workflow
        self.starting_status = target.local_status
        self.children: List[PlanRequest] = []

    def get_operation_artifacts(self):
        artifacts = []
        for req in self.children:
            artifacts.extend(req.get_operation_artifacts())
        return artifacts

    def add_request(self, req: PlanRequest) -> None:
        self.children.append(req)
        req.group = self

    def get_previous(self, req) -> Optional[PlanRequest]:
        siblings = self.children
        if req in siblings and siblings[0] != req:
            return siblings[siblings.index(req) - 1]
        return None

    def has_errors(self):
        for req in self.children:
            if req.error or req.task and req.task.local_status == Status.error:
                return True
        return False

    def set_final_for_workflow(self, is_final: bool):
        if self.children:
            self.children[-1].set_final_for_workflow(is_final)

    # XXX unused
    def get_unfulfilled_refs(self, check_target=""):
        for req in self.children:
            yield from req.get_unfulfilled_refs(check_target)

    def update_unfulfilled_errors(self) -> bool:
        for req in self.children:
            req.update_unfulfilled_errors()
        return self.has_unfulfilled_refs()

    def __str__(self) -> str:
        if self.children:
            return f"TaskRequestGroup{self.children}"
        else:
            return f'Planned task group {self.workflow} for "{self.target.name}"'

    def __repr__(self):
        return f"TaskRequestGroup({self.target}:{self.workflow}:{self.children})"


class JobRequest:
    """
    Yield this to run a child job.
    """

    def __init__(
        self,
        resources: List[EntityInstance],
        errors: Optional[Sequence[UnfurlError]] = None,
        update=None,
    ):
        self.instances = resources
        self.errors = errors or []
        self.update = update

    def set_error(self, msg: str):
        self.errors.append(UnfurlError(msg))  # type: ignore

    def get_instance_specs(self):
        if self.update:
            return [self.update]
        else:
            return [r.name for r in self.instances]

    @property
    def name(self):
        if self.update:
            return self.update["name"]
        elif self.instances:
            return self.instances[0].name
        else:
            return ""

    @property
    def target(self):
        # XXX replace instances with target
        if self.instances:
            return self.instances[0]
        else:
            return None

    @property
    def root(self):
        if self.instances:
            # all instances need the same root
            assert (
                len(self.instances) == 1
                or len(set(id(i.root) for i in self.instances)) == 1
            )
            return self.instances[0].root
        else:
            return None

    def __repr__(self):
        return f"JobRequest({self.name})"


def find_operation_host(target, operation_host):
    # SELF, HOST, ORCHESTRATOR, SOURCE, TARGET
    if not operation_host or operation_host in ["localhost", "ORCHESTRATOR"]:
        return target.root.find_instance_or_external("localhost")
    if operation_host == "SELF":
        return target
    if operation_host == "HOST":
        # XXX should search all ancestors to find parent that can handle the given operation
        # e.g. ansible configurator should find ancestor compute node
        return target.parent
    if operation_host == "SOURCE":
        return target.source
    if operation_host == "TARGET":
        return target.target
    return target.root.find_instance_or_external(operation_host)


def get_render_requests(
    requests: Sequence[PlanRequest],
) -> Iterator[PlanRequest]:
    # returns requests that can be rendered grouped by its top-most task group
    for req in requests:
        if isinstance(req, TaskRequestGroup):
            for child in get_render_requests(req.children):
                yield child  # yields root as parent
        elif isinstance(req, (TaskRequest, SetStateRequest)):
            yield req
        else:
            assert not req, f"unexpected type of request: {req}"

def set_fulfilled(
    upcoming: List[PlanRequest], completed: List[PlanRequest]
) -> Tuple[List[PlanRequest], List[PlanRequest]]:
    # find the requests that are ready to run
    # requests, completed are top level requests

    # Note:
    # to avoid circular dependencies we want to be eager and run as soon as a task changed required attribute to a valid state
    # but then another task could change the attribute later in the job.
    # to fix, the user can declare an explicit dependency on the resource
    # so that the task won't run after all the resource operations have run (i.e. its task group finished)

    ready, notReady = [], []
    for req in upcoming:
        if req.completed:
            continue
        assert req not in completed, f"{req} already completed"
        _not_ready = False
        previous = req.previous
        if previous and previous.not_ready:
            logger.trace(
                f"Previous {previous} not ready: {previous.get_notready_message()}"
            )
            _not_ready = True
        # see if these changes fulfilled the dependencies on a pending task
        elif req.update_unfulfilled_errors():
            _not_ready = True  # still has unfulfilled dependencies
        if _not_ready:
            notReady.append(req)
        else:  # list is now empty so request is ready
            ready.append(req)
    return ready, notReady


def set_fulfilled_stragglers(
    upcoming: List[PlanRequest],
    check_target: str,
) -> Tuple[List[PlanRequest], List[PlanRequest]]:
    # we ran all the tasks we could so now we can run left-over tasks
    # that depend on live attributes that we no longer have to worry about changing
    ready, notReady = [], []
    for req in upcoming:
        if req.completed:
            continue
        ok = True
        previous = req.previous
        if previous and previous.not_ready:
            # logger.trace(
            #     f"Previous {previous} not ready in final round: {previous.get_notready_message()}."
            # )
            ok = False
        elif check_target == "operational":
            for dep in req.get_unfulfilled_refs(check_target):
                ok = False  # not ready yet
                # logger.trace(
                #     f"Dependency for {req} not ready in final round: {req.get_notready_message()}."
                # )
                break
        if ok:
            ready.append(req)
            continue
        # found one, so not ready
        notReady.append(req)
    return ready, notReady


def _prepare_request(job: "Job", req: PlanRequest, errors: List[PlanRequest]) -> bool:
    if not isinstance(req, TaskRequest):
        return True
    if req.task:
        task = req.task
        task._attributeManager.attributes = {}
        task.target.root.set_attribute_manager(task._attributeManager)
    else:
        task = req.task = job.create_task(req.configSpec, req.target, reason=req.reason)
    error = None
    try:
        task.set_envvars()
        proceed, msg = job.should_run_task(task)
        if not proceed:
            req.required = False
            reassigned = req.reassign_final_for_workflow()
            if task._errors:
                error = task._errors[0]
            logger.debug(
                "skipping task %s for instance %s with state %s and status %s (reassigned: %s): %s",
                req.configSpec.operation,
                req.target.name,
                req.target.state,
                req.target.status,
                reassigned,
                msg,
            )
            # we want to track and save skipped tasks
            job.add_work(task)
    except Exception:
        proceed = False
        # note: failed rendering may be re-tried later if it has dependencies
        error = UnfurlTaskError(task, "should_run_task failed", logging.DEBUG)
    finally:
        task.restore_envvars()
    if error:
        errors.append(req)
        task._reset()
        task._attributeManager.attributes = {}  # rollback changes
    else:
        task.commit_changes()
    return proceed


def _render_request(
    job: "Job",
    req: PlanRequest,
    requests: Sequence[PlanRequest],
    check_target: str,
) -> Tuple[Optional[bool], Optional[UnfurlError]]:
    # req is a taskrequests, future_requests are (grouprequest, taskrequest) pairs
    assert req.task
    task = req.task
    task._attributeManager.attributes = {}
    task.target.root.set_attribute_manager(task._attributeManager)

    error = None
    error_info = None
    try:
        task.logger.debug("rendering %s %s", task.target.name, task.name)
        task._rendering = True
        task.inputs
        task.set_envvars()
        assert task._inputs is not None and not task._inputs.context.strict
        task.rendered = task.configurator.render(task)
    except Exception:
        # note: failed rendering may be re-tried later if it has dependencies
        error_info = sys.exc_info()
        error = UnfurlTaskError(task, "Configurator render failed", False)
    finally:
        task.restore_envvars()
    if task._errors:
        # we turned off strictness so templating errors got saved here instead
        req.render_errors = task._errors
        error = task._errors[0]
        error_info = error.stackInfo  # type: ignore
        task._errors = []
    task._rendering = False
    task._attributeManager.mark_referenced_templates(task.target.template)

    workflow = req.group and req.group.workflow
    if workflow != "undeploy":
        # when removing an instance don't worry about depending values changing in the future
        # key => (instance, list<attribute name>)
        liveDependencies = task._attributeManager.find_live_dependencies()
        task.logger.trace(f"live dependencies for {task.target}: {liveDependencies}")
        # a future request may change the value of these attributes
        req._set_dependencies(liveDependencies, requests)
        dependent_refs = [dep.name for dep in req.get_unfulfilled_refs(check_target)]
    else:
        dependent_refs = req.render_errors  # type: ignore
    if dependent_refs:
        task.logger.debug(
            "%s:%s can not render yet, depends on %s",
            task.target.name,
            req.name,
            dependent_refs,
            exc_info=error_info,
        )
        # rollback changes:
        task._errors = []
        task._reset()
        task._attributeManager.attributes = {}
        task.discard_work_folders()
        return bool(dependent_refs), None
    elif error:
        task.fail_work_folders()
        task._reset()
        task.logger.warning("Configurator render failed", exc_info=error_info)
        task._attributeManager.attributes = {}  # rollback changes
        return None, error
    else:
        task.logger.trace(f"committing changes from rendering task {task.target}")
        task.commit_changes()
        return None, None


def _add_to_req_list(reqs, request):
    if request not in reqs:
        reqs.append(request)


def _reevaluate_not_required(not_required: List[PlanRequest], render_requests) -> List[PlanRequest]:
    # keep rendering if a not_required template was referenced and is now required
    new_not_required: List[PlanRequest] = []
    for request in not_required:
        if request.include_in_plan():
            request.target.validate()
            render_requests.append(request)
        else:
            new_not_required.append(request)
    return new_not_required


def do_render_requests(
    job,
    requests: Sequence[PlanRequest],
    future_requests: List[PlanRequest] = [],
    check_target: str = "",
) -> Tuple[List[PlanRequest], List[PlanRequest], List[PlanRequest]]:
    ready: List[PlanRequest] = []
    notReady: List[PlanRequest] = []
    errors: List[PlanRequest] = []
    flattened_requests = list(
        r for r in get_render_requests(requests) if _prepare_request(job, r, errors)
    )
    not_required: List[PlanRequest] = []
    render_requests = collections.deque(flattened_requests)

    notready_group = None
    while render_requests:
        request = render_requests.popleft()
        if request.completed:
            continue
        # we dont require default templates that aren't referenced
        # (but skip this check if the job already specified specific instances)
        required = (
            job.workflow != "deploy" or job.is_filtered() or request.include_in_plan()
        )
        if not required:
            logger.trace("request isn't required by the plan, skipping: %s", request)
            not_required.append(request)
        else:
            request.target.validate()
            if notready_group and notready_group == request.group:
                # group children run sequentially
                _add_to_req_list(notReady, request)
                continue
            if not request.task:
                # i.e (for now): a setstate request
                _add_to_req_list(ready, request)
                continue
            deps, error = _render_request(
                job, request, flattened_requests + future_requests, check_target
            )
            if error:
                errors.append(request)
            elif deps:
                notready_group = request.group
                _add_to_req_list(notReady, request)
            else:
                _add_to_req_list(ready, request)
            not_required = _reevaluate_not_required(not_required, render_requests)

    for request in not_required:
        _add_to_req_list(notReady, request)
    return ready, notReady, errors


def _filter_config(opts, config, target):
    if opts.readonly and config.workflow != "discover":
        return None, "read only"
    if opts.requiredOnly and not config.required:
        return None, "required"
    if opts.instance and target.name != opts.instance:
        return None, f"instance {opts.instance}"
    if opts.instances and target.name not in opts.instances:
        return None, f"instances {opts.instances}"
    return config, None


def filter_task_request(jobOptions, req):
    configSpec = req.configSpec
    configSpecName = configSpec.name
    configSpec, filterReason = _filter_config(jobOptions, configSpec, req.target)
    if not configSpec:
        logger.debug(
            "skipping configspec '%s' for '%s': doesn't match filter: '%s'",
            configSpecName,
            req.target.name,
            filterReason,
        )
        return None  # treat as filtered step

    return req


def _find_implementation(interface: str, operation: str, template: EntitySpec) -> Optional[OperationDef]:
    default = None
    for iDef in template.get_interfaces():
        if iDef.interfacename == interface or iDef.type == interface:
            if iDef.name == operation:
                return iDef
    return default


def find_resources_from_template_name(root: HasInstancesInstance, name: str):
    # XXX make faster
    for resource in root.get_self_and_descendants():
        if resource.template.name == name:
            yield resource


def find_parent_template(source: NodeTemplate) -> Optional[NodeTemplate]:
    for rel, req, reqDef in source.relationships:
        # special case "host" so it can be declared without full set of relationship / capability types
        if rel.is_derived_from("tosca.relationships.HostedOn") or "host" in req:
            return rel.target
    return None


def find_parent_resource(root: TopologyInstance, source: EntitySpec) -> HasInstancesInstance:
    source_nodetemplate = cast(NodeTemplate, source.toscaEntityTemplate)
    parentTemplate = find_parent_template(source_nodetemplate)
    source_root = root.get_root_instance(source_nodetemplate)
    if not parentTemplate:
        return source_root
    root = root.get_root_instance(parentTemplate)
    if root is not source_root:
        # parent must be in the same topology
        return source_root
    for parent in find_resources_from_template_name(root, parentTemplate.name):
        # XXX need to evaluate matches
        return parent
    raise UnfurlError(
        f"could not find parent instance {parentTemplate.name} for child {source.name}"
    )


def create_instance_from_spec(_manifest: "Manifest", target: EntityInstance, rname: str, resourceSpec):
    pname = resourceSpec.get("parent")
    # get the actual parent if pname is a reserved name:
    if pname in [".self", "SELF"]:
        resourceSpec["parent"] = target.name
    elif pname == "HOST":
        if target.parent:
            resourceSpec["parent"] = target.parent.name
        else:
            resourceSpec["parent"] = "root"

    if isinstance(resourceSpec.get("template"), dict):
        # inline node template, add it to the spec
        tname = resourceSpec["template"].pop("name", rname)
        nodeSpec = target.template.topology.add_node_template(tname, resourceSpec["template"])
        resourceSpec["template"] = nodeSpec.name

    if resourceSpec.get("readyState") and "created" not in resourceSpec:
        # setting "created" to the target's key indicates that
        # the target is responsible for deletion
        # if "created" is not defined, set it if readyState is set
        resourceSpec["created"] = target.key

    if "parent" not in resourceSpec and "template" in resourceSpec:
        tname = resourceSpec["template"]
        nodeSpec = target.template.topology.get_template(tname)
        if not nodeSpec:
            raise UnfurlError(
                f'Can not find template "{tname}" when trying to create instance "{rname}".'
            )
        parent = find_parent_resource(cast(TopologyInstance, target.root), nodeSpec)
    else:
        parent = target.root
    # note: if resourceSpec[parent] is set it overrides the parent keyword
    return _manifest.create_node_instance(rname, resourceSpec, parent=parent)


def _maybe_mock(iDef, template):
    if not os.getenv("UNFURL_MOCK_DEPLOY"):
        return iDef
    mock = _find_implementation("Mock", iDef.name, template)
    if mock:
        return mock
    # mock operation not found, so patch iDef
    if not isinstance(iDef.implementation, dict):
        # it's a string naming an artifact
        iDef.implementation = dict(primary=iDef.implementation)
    iDef.implementation["className"] = "unfurl.configurator.MockConfigurator"
    return iDef


def get_success_status(workflow):
    if workflow == "deploy":
        return Status.ok
    elif workflow == "stop":
        return Status.pending
    elif workflow == "undeploy":
        return Status.absent
    return None


def create_task_request(
    jobOptions: "JobOptions",
    operation: str,
    resource: "EntityInstance",
    reason: Optional[str] = None,
    inputs: Optional[Mapping] = None,
    startState=None,
    operation_host=None,
    skip_filter=False,
):
    """implementation can either be a named artifact (including a python configurator class),
    or a file path"""
    interface, sep, action = operation.rpartition(".")
    iDef = _find_implementation(interface, action, resource.template)
    if iDef and iDef.name != "default":
        iDef = _maybe_mock(iDef, resource.template)
        assert iDef
        # merge inputs
        if inputs:
            cls = getattr(iDef.inputs, "mapCtor", iDef.inputs.__class__)
            inputs = cls(iDef.inputs, **inputs)
        else:
            inputs = iDef.inputs or {}
        if iDef.invoke:
            # get the implementation from the operation specified with the "invoke" key
            iinterface, sep, iaction = iDef.invoke.rpartition(".")
            iDef = _find_implementation(iinterface, iaction, resource.template)
            if iDef:
                cls = getattr(iDef.inputs, "mapCtor", iDef.inputs.__class__)
                inputs = cls(iDef.inputs, **inputs)

    kw = None
    if iDef:
        kw = _get_config_spec_args_from_implementation(
            iDef, inputs, resource, operation_host
        )
    if kw:
        kw["interface"] = interface
        if reason:
            name = f"for {reason}: {interface}.{action}"
            if reason == jobOptions.workflow:
                # set the task's workflow instead of using the default ("deploy")
                kw["workflow"] = reason
        else:
            name = f"{interface}.{action}"
        configSpec = ConfigurationSpec(name, action, **kw)
        logger.debug(
            "creating configuration %s with %s for %s: %s",
            configSpec.name,
            tuple(f"{n}: {str(v)[:50]}" for n, v in configSpec.inputs.items())
            if configSpec.inputs
            else (),
            resource.name,
            reason or action,
        )
    else:
        errorMsg = f'unable to find an implementation for operation "{action}" on node "{resource.template.name}"'
        logger.trace(errorMsg)
        return None

    req = TaskRequest(
        configSpec,
        resource,
        reason or action,
        startState=startState or iDef.entry_state,
    )
    if skip_filter:
        return req
    else:
        return filter_task_request(jobOptions, req)


def set_default_command(kw, implementation):
    inputs = kw["inputs"]
    if not implementation:
        implementation = inputs.get("cmd")
    # is it a shell script or a command line?
    shell = inputs.get("shell")
    if shell is None:
        # no special shell characters
        shell = not re.match(r"[\w.-]+\Z", implementation)

    operation_host = kw.get("operation_host")
    implementation = implementation.lstrip()
    if not operation_host or operation_host == "localhost":
        className = "unfurl.configurators.shell.ShellConfigurator"
        if shell:
            shellArgs = dict(command=implementation)
        else:
            shellArgs = dict(command=[implementation])
    else:
        className = "unfurl.configurators.ansible.AnsibleConfigurator"
        module = "shell" if shell else "command"
        playbookTask = dict(cmd=implementation)
        cwd = inputs.get("cwd")
        if cwd:
            playbookTask["chdir"] = cwd
        if shell and isinstance(shell, str):
            playbookTask["executable"] = shell
        shellArgs = dict(playbook=[{module: playbookTask}])

    kw["className"] = className
    if inputs:
        shellArgs.update(inputs)
    kw["inputs"] = shellArgs
    return kw


def _set_config_spec_args(kw, target, base_dir):
    # if no artifact or className, an error
    artifact = kw["primary"]
    className = kw.get("className")
    if not className and not artifact:  # malformed implementation
        logger.warning(
            "no artifact or className set on operation for %s: %s", target.name, kw
        )
        return None
    guessing = False
    if not className:
        className = artifact.properties.get("className")
        if not className:
            className = artifact.file
            guessing = className

    # load the configurator class
    try:
        if "#" in className and len(shlex.split(className)) == 1:
            path, sep, fragment = className.partition("#")
            fullpath = os.path.join(base_dir, path)
            mod = load_module(fullpath)
            klass = getattr(mod, fragment)  # raise if missing
        else:
            klass = lookup_class(className)
    except:
        klass = None
        logger.debug("exception while instantiating %s", className, exc_info=True)

    # invoke configurator classmethod to give configurator a chance to customize configspec (e.g. add dependencies)
    if klass:
        kw["className"] = f"{klass.__module__}.{klass.__name__}"
        return klass.set_config_spec_args(kw, target)
    elif guessing:
        # otherwise assume it's a shell command line
        logger.debug("interpreting 'implementation' as a shell command: %s", guessing)
        return set_default_command(kw, guessing)
    else:
        # error couldn't resolve className
        logger.warning("could not find configurator class: %s", className)
        return None


def _get_config_spec_args_from_implementation(iDef: OperationDef, inputs, target, operation_host):
    implementation = iDef.implementation
    kw = dict(
        inputs=inputs,
        outputs=iDef.outputs,
        operation_host=operation_host,
        entry_state=iDef.entry_state,
    )
    configSpecArgs = ConfigurationSpec.getDefaults()
    artifactTpl = None
    dependencies = None
    predefined = False
    if isinstance(implementation, dict):
        # operation_instance = find_operation_host(
        #     target, implementation.get("operation_host") or operation_host
        # )
        for name, value in implementation.items():
            if name == "primary":
                artifactTpl = value
                predefined = True
            elif name == "dependencies":
                dependencies = value
            elif name in configSpecArgs:
                # sets operation_host, environment, timeout, className
                kw[name] = value
    else:
        # "either because it refers to a named artifact specified in the artifacts section of a type or template,
        # or because it represents the name of a script in the CSAR file that contains the definition."
        artifactTpl = implementation
        # operation_instance = find_operation_host(target, operation_host)

    # if not operation_instance:
    #     operation_instance = operation_instance or target.root
    base_dir = getattr(iDef.value, "base_dir", iDef._source)
    kw["base_dir"] = base_dir
    if artifactTpl:
        artifact = target.template.find_or_create_artifact(
            artifactTpl, base_dir, predefined
        )
    else:
        artifact = None
    kw["primary"] = artifact

    if dependencies:
        kw["dependencies"] = [
            target.template.find_or_create_artifact(artifactTpl, base_dir, True)
            for artifactTpl in dependencies
        ]
    else:
        kw["dependencies"] = []

    return _set_config_spec_args(kw, target, base_dir)
