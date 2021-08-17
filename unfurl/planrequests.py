# Copyright (c) 2021 Adam Souzis
# SPDX-License-Identifier: MIT
import collections
import re
import six
import shlex
from .util import (
    lookup_class,
    load_module,
    find_schema_errors,
    UnfurlError,
    UnfurlTaskError,
)
from .result import serialize_value
from .support import Defaults
import logging

logger = logging.getLogger("unfurl")

# we want ConfigurationSpec to be standalone and easily serializable
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
    ):
        assert name and className, "missing required arguments"
        self.name = name
        self.operation = operation
        self.className = className
        self.majorVersion = majorVersion
        self.minorVersion = minorVersion
        self.workflow = workflow
        self.timeout = timeout
        self.operationHost = operation_host
        self.environment = environment
        self.inputs = inputs or {}
        self.inputSchema = inputSchema
        self.outputs = outputs or {}
        self.preConditions = preConditions
        self.postConditions = postConditions
        self.artifact = primary

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
        klass = lookup_class(self.className)
        if not klass:
            raise UnfurlError(f"Could not load configurator {self.className}")
        else:
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
        # XXX3 add unit tests
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
        )


class PlanRequest:
    error = None
    future_dependencies = ()

    def __init__(self, target):
        self.target = target

    @property
    def root(self):
        return self.target.root if self.target else None

    def update_future_dependencies(self, completed):
        return self.future_dependencies

    def get_operation_artifacts(self):
        return []


class TaskRequest(PlanRequest):
    """
    Yield this to run a child task. (see :py:meth:`unfurl.configurator.TaskView.create_sub_task`)
    """

    def __init__(
        self,
        configSpec,
        target,
        reason,
        persist=False,
        required=None,
        startState=None,
    ):
        super().__init__(target)
        self.configSpec = configSpec
        self.reason = reason
        self.persist = persist
        self.required = required
        self.error = configSpec.name == "#error"
        self.startState = startState
        self.task = None

    def get_operation_artifacts(self):
        artifact = self.configSpec.artifact
        # XXX self.configSpec.dependencies
        if artifact and artifact.template.get_interfaces():
            # has an artifact with a type that needs installation
            if not artifact.operational:
                return [artifact]
        return []

    @property
    def name(self):
        if self.configSpec.operation:
            name = self.configSpec.operation
        else:
            name = self.configSpec.name
        if self.reason and self.reason not in name:
            return name + " (reason: " + self.reason + ")"
        return name

    def update_future_dependencies(self, completed):
        self.future_dependencies = [
            fr for fr in self.future_dependencies if fr not in completed
        ]
        return self.future_dependencies

    def _summary_dict(self):
        return dict(
            operation=self.configSpec.operation or self.configSpec.name,
            reason=self.reason,
        )

    def __repr__(self):
        return f"TaskRequest({self.target}({self.target.status},{self.target.state}):{self.name})"


class SetStateRequest(PlanRequest):
    def __init__(self, target, state):
        super().__init__(target)
        self.set_state = state

    @property
    def name(self):
        return self.set_state

    def _summary_dict(self):
        return dict(set_state=self.set_state)


class TaskRequestGroup(PlanRequest):
    def __init__(self, target, workflow):
        super().__init__(target)
        self.workflow = workflow
        self.children = []

    @property
    def future_dependencies(self):
        future_dependencies = []
        for req in self.children:
            future_dependencies.extend(req.future_dependencies)
        return future_dependencies

    def update_future_dependencies(self, completed):
        future_dependencies = []
        for req in self.children:
            future_dependencies.extend(req.update_future_dependencies(completed))
        return future_dependencies

    def get_operation_artifacts(self):
        artifacts = []
        for req in self.children:
            artifacts.extend(req.get_operation_artifacts())
        return artifacts

    def __repr__(self):
        return f"TaskRequestGroup({self.target}:{self.workflow}:{self.children})"


class JobRequest:
    """
    Yield this to run a child job.
    """

    def __init__(self, resources, errors):
        self.instances = resources
        self.errors = errors

    @property
    def root(self):
        return self.instances[0].root if self.instances else None

    def __repr__(self):
        return f"JobRequest({self.instances})"


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


def get_render_requests(requests):
    # returns requests that can be rendered grouped by its top-most task group
    for req in requests:
        if isinstance(req, TaskRequestGroup):
            for parent, child in get_render_requests(req.children):
                yield req, child  # yields root as parent
        elif isinstance(req, TaskRequest):
            yield None, req


def _get_deps(parent, req, liveDependencies, future_requests):
    previous = None
    for (root, r) in future_requests:
        if r.target.key in liveDependencies:
            if root:
                if previous is root or parent is root:
                    # only yield root once and
                    # don't consider requests in the same root
                    continue
                previous = root
            yield root or r


def set_fulfilled(requests, completed):
    # requests, completed are top level requests,
    # as is future_dependencies
    ready, notReady = [], []
    for req in requests:
        if req.update_future_dependencies(completed):
            notReady.append(req)
        else:  # list is now empty so request is ready
            ready.append(req)
    return ready, notReady


def _render_request(job, parent, req, future_requests):
    # req is a taskrequests, future_requests are (grouprequest, taskrequest) pairs
    if req.task:
        task = req.task
        task.target.root.attributeManager = task._attributeManager
    else:
        task = req.task = job.create_task(req.configSpec, req.target, reason=req.reason)
    task.logger.debug("rendering %s %s", task.target.name, task.name)
    try:
        task.rendered = task.configurator.render(task)
    except Exception:
        if task._workFolder:
            task._workFolder.failed()
        task._inputs = None
        task._attributeManager.attributes = {}  # rollback changes
        # note: failed rendering may be re-tried later
        return [], UnfurlTaskError(task, "Configurator render failed", logging.DEBUG)
    else:
        if parent and parent.workflow == "undeploy":
            # when removing an instance don't worry about depending values changing in the future
            deps = None
        else:
            # key => (instance, list<attribute>)
            liveDependencies = task._attributeManager.find_live_dependencies()
            # a future request may change the value of these attributes
            deps = list(_get_deps(parent, req, liveDependencies, future_requests))

        if deps:
            req.future_dependencies = deps
            task.logger.debug(
                "%s can not render yet, depends on %s", task.target.name, str(deps)
            )
            # rollback changes:
            task._inputs = None
            task._attributeManager.attributes = {}
            if task._workFolder:
                task._workFolder.discard()
            return deps, None
        else:
            task.commit_changes()
            if task._workFolder:
                task._workFolder.apply()
    return [], None


def _add_to_req_list(reqs, parent, request):
    if parent:  # only add if we haven't already
        if not reqs or reqs[-1] is not parent:
            reqs.append(parent)
    else:
        reqs.append(request)


def do_render_requests(job, requests):
    ready, notReady, errors = [], [], []
    render_requests = collections.deque(get_render_requests(requests))
    while render_requests:
        parent, request = render_requests.popleft()
        deps, error = _render_request(job, parent, request, render_requests)
        if error:
            errors.append(error)
        elif deps:
            # remove if we already added the parent
            if parent and ready and ready[-1] is parent:
                ready.pop()
            _add_to_req_list(notReady, parent, request)
        else:
            if not parent or not notReady or notReady[-1] is not parent:
                # don't add if the parent was placed on the notReady list
                _add_to_req_list(ready, parent, request)
    return ready, notReady, errors


def _filter_config(opts, config, target):
    if opts.readonly and config.workflow != "discover":
        return None, "read only"
    if opts.requiredOnly and not config.required:
        return None, "required"
    if opts.instance and target.name != opts.instance:
        return None, "instance"
    if opts.instances and target.name not in opts.instances:
        return None, "instances"
    return config, None


def filter_task_request(jobOptions, req):
    configSpec = req.configSpec
    configSpecName = configSpec.name
    configSpec, filterReason = _filter_config(jobOptions, configSpec, req.target)
    if not configSpec:
        logger.debug(
            "skipping configspec %s for %s: doesn't match %s filter",
            configSpecName,
            req.target.name,
            filterReason,
        )
        return None  # treat as filtered step

    return req


def _find_implementation(interface, operation, template):
    default = None
    for iDef in template.get_interfaces():
        if iDef.interfacename == interface or iDef.type == interface:
            if iDef.name == operation:
                return iDef
            if iDef.name == "default":
                default = iDef
    return default


def create_task_request(
    jobOptions,
    operation,
    resource,
    reason=None,
    inputs=None,
    startState=None,
    operation_host=None,
):
    """implementation can either be a named artifact (including a python configurator class),
    or a file path"""
    interface, sep, action = operation.rpartition(".")
    iDef = _find_implementation(interface, action, resource.template)
    if iDef and iDef.name != "default":
        # merge inputs
        if inputs:
            inputs = dict(iDef.inputs, **inputs)
        else:
            inputs = iDef.inputs or {}
        kw = _get_config_spec_args_from_implementation(
            iDef, inputs, resource, operation_host
        )
    else:
        kw = None

    if kw:
        if reason:
            name = f"for {reason}: {interface}.{action}"
            if reason == jobOptions.workflow:
                # set the task's workflow instead of using the default ("deploy")
                kw["workflow"] = reason
        else:
            name = f"{interface}.{action}"
        configSpec = ConfigurationSpec(name, action, **kw)
        logger.debug(
            "creating configuration %s with %s to run for %s: %s",
            configSpec.name,
            configSpec.inputs,
            resource.name,
            reason or action,
        )
    else:
        errorMsg = f'unable to find an implementation for operation "{action}" on node "{resource.template.name}"'
        logger.debug(errorMsg)
        return None

    req = TaskRequest(
        configSpec,
        resource,
        reason or action,
        startState=startState,
    )
    return filter_task_request(jobOptions, req)


def _set_default_command(kw, implementation, inputs):
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
        if shell and isinstance(shell, six.string_types):
            playbookTask["executable"] = shell
        shellArgs = dict(playbook=[{module: playbookTask}])

    kw["className"] = className
    if inputs:
        shellArgs.update(inputs)
    kw["inputs"] = shellArgs


def _get_config_spec_args_from_implementation(iDef, inputs, target, operation_host):
    implementation = iDef.implementation
    kw = dict(inputs=inputs, outputs=iDef.outputs, operation_host=operation_host)
    configSpecArgs = ConfigurationSpec.getDefaults()
    artifact = None
    dependencies = None
    if isinstance(implementation, dict):
        operation_instance = find_operation_host(
            target, implementation.get("operation_host") or operation_host
        )
        for name, value in implementation.items():
            if name == "primary":
                artifact = value
            elif name == "dependencies":
                dependencies = value
            elif name in configSpecArgs:
                # sets operation_host, environment, timeout
                kw[name] = value
    else:
        # "either because it refers to a named artifact specified in the artifacts section of a type or template,
        # or because it represents the name of a script in the CSAR file that contains the definition."
        artifact = implementation
        operation_instance = find_operation_host(target, operation_host)

    instance = operation_instance or target
    base_dir = getattr(iDef.value, "base_dir", iDef._source)
    if artifact:
        artifact = instance.find_or_create_artifact(artifact, base_dir)
    kw["primary"] = artifact

    if dependencies:
        kw["dependencies"] = [
            instance.find_or_create_artifact(artifactTpl, base_dir)
            for artifactTpl in dependencies
        ]

    if "className" not in kw:
        if not artifact:  # malformed implementation
            return None
        implementation = artifact.file
        # see if implementation looks like a python class
        if "#" in implementation and len(shlex.split(implementation)) == 1:
            path, fragment = artifact.get_path_and_fragment()
            mod = load_module(path)
            kw["className"] = mod.__name__ + "." + fragment
            return kw
        elif lookup_class(implementation):
            kw["className"] = implementation
            return kw

        # otherwise assume it's a shell command line
        logger.debug(
            "interpreting 'implementation' as a shell command: %s", implementation
        )
        _set_default_command(kw, implementation, inputs)
    return kw
