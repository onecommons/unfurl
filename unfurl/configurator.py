# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import inspect
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Generator,
    List,
    MutableMapping,
    Optional,
    Tuple,
    Union,
    ValuesView,
    cast,
    MutableSequence,
)
from typing_extensions import TypedDict
from collections.abc import Mapping
import os
import copy
from tosca import ToscaInputs, ToscaOutputs
from toscaparser.elements.interfaces import OperationDef
from toscaparser.properties import Property
from .logs import UnfurlLogger, Levels, LogExtraLevels, SensitiveFilter, truncate

if TYPE_CHECKING:
    from .manifest import Manifest, ChangeRecordRecord
    from .job import ConfigTask, Job


from .support import (
    AttributeManager,
    NodeState,
    Status,
    ResourceChanges,
    Priority,
    set_context_vars,
)
from .result import (
    ChangeRecord,
    ResultsList,
    serialize_value,
    ChangeAware,
    Results,
    ResultsMap,
    get_digest,
    Result,
)
from .util import (
    ChainMap,
    register_class,
    validate_schema,
    UnfurlTaskError,
    UnfurlAddingResourceError,
    filter_env,
    to_enum,
    wrap_sensitive_value,
    sensitive,
)
from . import merge
from .eval import Ref, map_value, RefContext
from .runtime import (
    ArtifactInstance,
    EntityInstance,
    HasInstancesInstance,
    NodeInstance,
    RelationshipInstance,
    Operational,
)
from .yamlloader import yaml
from .projectpaths import WorkFolder, Folders
from .planrequests import (
    TaskRequest,
    JobRequest,
    ConfigurationSpec,
    _find_implementation,
    create_task_request,
    find_operation_host,
    create_instance_from_spec,
)
from .spec import find_env_vars, EntitySpec

import logging

# Tell mypy the logger is of type UnfurlLogger
logger = cast(UnfurlLogger, logging.getLogger("unfurl.task"))


class ConfiguratorResult:
    """
    Represents the result of a task that ran.

    See :py:meth:`TaskView.done` for more documentation.
    """

    def __init__(
        self,
        success: bool,
        modified: Optional[bool],
        status: Optional[Status] = None,
        result: Optional[Union[dict, str]] = None,
        outputs: Optional[dict] = None,
        exception: Optional[UnfurlTaskError] = None,
    ) -> None:
        self.modified = modified
        self.status = to_enum(Status, status)
        self.result = result
        self.success = success
        self.outputs = outputs
        self.exception = exception

    def __str__(self) -> str:
        result = (
            ""
            if self.result is None
            else truncate(str(SensitiveFilter.redact(self.result)), 240, "...")
        )
        return (
            "changes: "
            + (
                " ".join(
                    filter(
                        None,
                        [
                            self.success and "success" or "",
                            self.modified and "modified" or "",
                            self.status is not None and self.status.name or "",
                        ],
                    )
                )
                or "none"
            )
            + "\n   "
            + result
        )


class AutoRegisterClass(type):
    def __new__(mcls, name: str, bases: tuple, dct: dict) -> "type":
        cls = type.__new__(mcls, name, bases, dct)
        if cls.short_name:  # type: ignore
            name = cls.short_name  # type: ignore
        elif name.endswith("Configurator"):
            name = name[: -len("Configurator")]
        if name:
            register_class(cls.__module__ + "." + cls.__name__, cls, name)
        return cls


class Configurator(metaclass=AutoRegisterClass):
    """
    Base class for implementing `Configurators`.
    Subclasses should at least implement a :py:meth:`~Configurator.run`, for example:

    .. code-block:: python

        class MinimalConfigurator(Configurator):
            def run(self, task):
                assert self.can_run(task)
                return Status.ok

    """

    short_name: Optional[str] = None
    """shortName can be used to customize the "short name" of the configurator
    as an alternative to using the full name ("module.class") when setting the implementation on an operation.
    (Titlecase recommended)"""

    exclude_from_digest: Tuple[str, ...] = ()

    attribute_output_metadata_key: Optional[str] = None

    __is_generator: Optional[bool] = None

    @classmethod
    def set_config_spec_args(cls, kw: dict, template: EntitySpec) -> dict:
        return kw

    @classmethod
    def get_dry_run(cls, inputs, template: EntitySpec) -> bool:
        # default: defer to mock implementation if present otherwise defer to runtime check (can_dry_run())
        return False

    def __init__(self, *args: ToscaInputs, **kw) -> None:
        self._inputs: Dict[str, Any] = ToscaInputs._get_inputs(*args, **kw)

    def _run(self, task: "TaskView") -> Generator:
        # internal function to convert user defined run() to a generator
        result = self.run(task)
        if isinstance(result, ConfiguratorResult):
            yield result
        elif isinstance(result, Status):
            yield task.done(None, None, result)
        elif isinstance(result, (bool, type(None))):
            yield task.done(result)
        else:
            raise UnfurlTaskError(task, "bad return valuate")

    def _is_generator(self):
        if self.__is_generator is None:
            self.__is_generator = inspect.isgeneratorfunction(self.run)
        return self.__is_generator

    def get_generator(self, task: "TaskView") -> Generator:
        if self._is_generator():
            return self.run(task)  # type: ignore
        else:
            return self._run(task)

    def render(self, task: "TaskView") -> Any:
        """
        This method is called during the planning phase to give the configurator an
        opportunity to do early validation and error detection and generate any plan information or configuration files that the user may want to review before the running the deployment task.

        Property access and writes will be tracked and used to establish dynamic dependencies between instances so the plan can be ordered properly. Any updates made to instances maybe reverted if it has dependencies on attributes that might be changed later in the plan, so this method should be idempotent.

        Returns:
            The value returned here will subsequently be available as ``task.rendered``
        """
        return None

    # yields a JobRequest, TaskRequest or a ConfiguratorResult
    def run(
        self, task: "TaskView"
    ) -> Union[Generator, ConfiguratorResult, Status, bool]:
        """
        Subclasses of Configurator need to implement this method.
        It should perform the operation specified in the :class:`ConfigurationSpec`
        on the :obj:`task.target`. It can be either a generator that yields one or more :class:`JobRequest` or :class:`TaskRequest`
        or a regular function.

        Args:
            task (:class:`TaskView`): The task currently running.

        Yields:
            Optionally ``run`` can yield either a :class:`JobRequest`, :class:`TaskRequest` to run subtasks
            and finally a :class:`ConfiguratorResult` when done

        Returns:
            If ``run`` is not defined as a generator it must return either a :py:class:`~unfurl.support.Status`, a ``bool`` or a :class:`ConfiguratorResult` to indicate if the task succeeded and any changes to the target's state.
        """
        yield task.done(False)
        return False

    def can_dry_run(self, task: "TaskView") -> bool:
        """
        Returns whether this configurator can handle a dry-runs for the given task.
        (And should check :attr:`.TaskView.dry_run` in during run().

        Args:
            task (:obj:`TaskView`): The task about to be run.

        Returns:
            bool
        """
        return self.get_dry_run(task.inputs, task.target.template)

    def can_run(self, task: "TaskView") -> Union[bool, str]:
        """
        Return whether or not the configurator can execute the given task
        depending on if this configurator support the requested action and parameters
        and given the current state of the target instance?

        Args:
            task (:class:`TaskView`): The task that is about to be run.

        Returns:
            (bool or str): Should return True or a message describing why the task couldn't be run.
        """
        return True

    def should_run(self, task: "TaskView") -> Union[bool, Priority]:
        """Does this configuration need to be run?"""
        return task.configSpec.should_run()

    def save_digest(self, task: "TaskView") -> dict:
        """
        Generate a compact, deterministic representation of the current configuration.
        This is saved in the job log and used by `check_digest` in subsequent jobs to
        determine if the configuration changed the operation needs to be re-run.

        The default implementation calculates a SHA1 digest of the values of the inputs
        that where accessed while that task was run, with the exception of
        the input parameters listed in `exclude_from_digest`.

        Args:
            task (:class:`TaskView`) The task that executed this operation.

        Returns:
            dict: A dictionary whose keys are strings that start with "digest"
        """
        # XXX user definition should be able to exclude inputs from digest
        # XXX might throw AttributeError
        inputs = cast("ConfigTask", task)._resolved_inputs

        # sensitive values are always redacted so no point in including them in the digest
        # (for cleaner output and security-in-depth)
        keys: List[str] = [
            k
            for k in inputs.keys()
            if k not in self.exclude_from_digest
            and not isinstance(inputs[k].resolved, sensitive)
        ]
        values: List[Any] = [inputs[key] for key in keys]

        changed = set(task._resourceChanges.get_changes_as_expr())
        for dep in task.dependencies:
            assert isinstance(dep, Dependency)
            if (
                dep.write_only
            ):  # include values that were set but might not have changed
                changed.add(str(dep.expr))
            elif not isinstance(dep.expected, sensitive):
                keys.append(str(dep.expr))
                values.append(dep.expected)

        if keys:
            inputdigest = get_digest(values, manifest=task._manifest)
        else:
            inputdigest = ""

        digest = dict(
            digestKeys=",".join(keys),
            digestValue=inputdigest,
            digestPut=",".join(changed),
        )
        task.logger.debug(
            "digest for %s: %s=%s", task.target.name, digest["digestKeys"], inputdigest
        )
        if changed:
            task.logger.debug(
                "digestPut for %s: %s", task.target.name, digest["digestPut"]
            )
        return digest

    def check_digest(self, task: "TaskView", changeset: "ChangeRecordRecord") -> bool:
        """
        Examine the previous :class:`ChangeRecord` generated by the previous time this operation
        was performed on the target instance and return whether it should be rerun or not.

        The default implementation recalculates the digest of input parameters that
        were accessed in the previous run.

        Args:
            task (:class:`TaskView`): The task that might execute this operation.
            changeset (:class:`ChangeRecordRecord`): The task that might execute this operation.

        Returns:
            bool: True if configuration's digest has changed, False if it is the same.
        """
        _parameters: str = getattr(changeset, "digestKeys", "")
        if not _parameters:
            task.logger.debug(
                "skipping digest check for %s, previous task did not record any changes",
                task.target.name,
            )
            return False
        current_inputs = {
            k for k in task.inputs.keys() if k not in self.exclude_from_digest
        }
        task.logger.debug("checking digest for %s: %s", task.target.name, _parameters)
        old_keys = _parameters.split(",")
        old_inputs = {key for key in old_keys if "::" not in key}
        if old_inputs - current_inputs:
            # an old input was removed
            task.logger.verbose(
                "digest keys changed for %s: these inputs were removed: %s",
                task.target.name,
                old_inputs - current_inputs,
            )
            return True
        # only resolve the inputs and dependencies that were resolved before
        # inputs should be lazily resolved by the task, so we need to avoid resolving them all now
        # (we also won't know if there are new dependencies until the task runs)
        results: List[Result] = []
        for key in old_keys:
            if "::" in key:
                results.extend(Ref(key).resolve(task.inputs.context, wantList="result"))
            else:
                results.append(task.inputs._getresult(key))
        new_inputs = current_inputs - old_inputs
        if new_inputs and new_inputs.intersection(task.inputs.get_resolved()):
            # a new input was added and accessed while resolving old inputs and dependencies
            # XXX this check is incomplete if another new input is accessed while the task runs
            task.logger.verbose(
                "digest keys changed for %s: old %s, new %s",
                task.target.name,
                old_inputs,
                current_inputs,
            )
            return True
        newDigest = get_digest(results, manifest=task._manifest)
        # note: digestValue attribute is set in Manifest.load_config_change
        mismatch = changeset.digestValue != newDigest
        if mismatch:
            task.logger.verbose(
                "digests didn't match for %s with %s: old %s, new %s",
                task.target.name,
                _parameters,
                changeset.digestValue,
                newDigest,
            )
        return mismatch


class MockConfigurator(Configurator):
    def run(self, task: "TaskView") -> Generator:
        yield task.done(True)

    def can_dry_run(self, task: "TaskView") -> bool:
        return True


class _ConnectionsMap(dict):
    def by_type(self) -> ValuesView:
        # return unique connection by type
        # reverse so nearest relationships replace less specific ones that have matching names
        # XXX why is rel sometimes a Result?
        by_type = {  # the list() is for Python 3.7
            (
                rel.resolved.template.global_type
                if isinstance(rel, Result)
                else rel.template.global_type
            ): rel
            for rel in reversed(list(self.values()))
        }
        return by_type.values()

    def copy(self):
        return self

    def __missing__(self, key: object) -> object:
        # the more specific connections are inserted first so this should find
        # the most relevant connection of the given type
        for value in self.values():
            # XXX why is value sometimes a Result?
            if isinstance(value, Result):
                value = value.resolved
            # hackish: match the local name of type
            if key == value.type.rpartition(".")[2]:
                return value
            if value.template.is_compatible_type(key):
                return value
        raise KeyError(key)


class TaskLoggerAdapter(logging.LoggerAdapter, LogExtraLevels):
    def log(self, level, msg, *args, **kwargs):
        """
        Delegate a log call to the underlying logger, after adding
        contextual information from this adapter instance.
        """
        # we override this instead of process() because we need to change the log level
        # note that `self.extra` does NOT replace kwargs['extra']
        if self.isEnabledFor(level):
            task = cast(TaskView, self.extra)
            # e.g. Task configure for test-node (reason: missing)
            task_id = f"{task.configSpec.operation} for {task.target.name}"
            if task.reason:
                task_id += f" (reason: {task.reason})"
            if task._rendering:
                msg = f"Rendering task {task_id} (errors expected): {msg}"
                if level >= Levels.VERBOSE:
                    level = Levels.VERBOSE
            else:
                msg = f"Task {task_id}: {msg}"
            self.logger.log(level, msg, *args, **kwargs)


_initializing_environ = object()


class TaskView:
    """The interface presented to configurators.

    The following public attributes are available:

    Attributes:
        target: The instance this task is operating on.
        reason (str): The reason this operation was planned. See :class:`~unfurl.support.Reason`
        cwd (str): Current working directory
        dry_run (bool): Dry run only
        verbose (int): Verbosity level set for this job (-1 error, 0 normal, 1 verbose, 2 debug)
    """

    def __init__(
        self,
        manifest: "Manifest",
        configSpec: ConfigurationSpec,
        target: EntityInstance,
        reason: Optional[str] = None,
        dependencies: Optional[List["Operational"]] = None,
    ) -> None:
        # public:
        self.configSpec = configSpec
        self.target = target
        self.reason = reason
        self.logger = TaskLoggerAdapter(logger, self)  # type: ignore
        self.cwd: str = os.path.abspath(self.target.base_dir)
        self.rendered: Any = None
        self.dry_run: Optional[bool] = None
        self.verbose = 0  # set by ConfigView
        # private:
        self._errors: List[
            UnfurlTaskError
        ] = []  # UnfurlTaskError objects appends themselves to this list
        self._inputs: Optional[ResultsMap] = None
        self._manifest = manifest
        self.messages: List[Any] = []
        self._addedResources: List[NodeInstance] = []
        self._dependenciesChanged = False
        self.dependencies: List["Operational"] = dependencies or []
        self._resourceChanges = ResourceChanges()
        self._workFolders: Dict[str, WorkFolder] = {}
        self._failed_paths: List[str] = []
        self._rendering = False
        self._execute_op: Optional[OperationDef] = None
        self._artifact: Optional[ArtifactInstance] = None
        # (_environ type is object because of _initializing_environ)
        self._environ: Optional[object] = None
        self._attributeManager: AttributeManager = None  # type: ignore
        self.job: Optional["Job"] = None
        # public:
        self.operation_host: Optional[HasInstancesInstance] = find_operation_host(
            target, configSpec.operation_host
        )

    @property
    def environ(self) -> Dict[str, str]:
        if self._inputs is None or self._environ is _initializing_environ:
            # not ready yet
            return self.target.environ
        if self._environ is None:
            self._environ = _initializing_environ
            self._environ = self.get_environment(False)
        return cast(Dict[str, str], self._environ)

    def set_envvars(self):
        """
        Update os.environ with the task's environ and save the current one so it can be restored by ``restore_envvars``
        """
        # self.logger.trace("update os.environ with %s", self.environ)
        for key in os.environ:
            current = self.environ.get(key)
            if current is None:
                del os.environ[key]
        for key, value in self.environ.items():
            if value is not None:
                os.environ[key] = str(value)

    def restore_envvars(self):
        # restore the environ set on root resource
        # self.logger.trace("restoring os.environ with %s", self.target.root.environ)
        for key in os.environ:
            current = self.target.root.environ.get(key)
            if current is None:
                del os.environ[key]
        for key, value in self.target.environ.items():
            if value is not None:
                os.environ[key] = str(value)

    @property
    def inputs(self) -> ResultsMap:
        """
        Exposes inputs and task settings as expression variables, so they can be accessed like:

        eval: $inputs::param

        or in jinja2 templates:

        {{ inputs.param }}
        """
        if self._inputs is None:
            assert self._attributeManager
            assert self.target.root.attributeManager is self._attributeManager  # type: ignore
            ctx = self.target.attributes.context
            ctx.task = self
            ctx.referenced = self._attributeManager.tracker
            relationship = None
            if isinstance(self.target, RelationshipInstance):
                relationship = self.target
                if self.target.target:
                    target: EntityInstance = self.target.target
                else:
                    target = self.target.root
            else:
                target = self.target
            HOST = (target.parent or target).attributes
            ORCHESTRATOR = target.root.find_instance_or_external("localhost")
            vars: Dict[str, Any] = dict(
                task=self.get_settings(),
                connections=self._get_connections(),
                SELF=self.target.attributes,
                HOST=HOST,
                ORCHESTRATOR=ORCHESTRATOR and ORCHESTRATOR.attributes or {},
                OPERATION_HOST=self.operation_host
                and self.operation_host.attributes
                or {},
            )
            set_context_vars(vars, target)
            if relationship:
                if relationship.source:
                    vars["SOURCE"] = relationship.source.attributes  # type: ignore
                vars["TARGET"] = target.attributes
            # expose inputs lazily to allow self-reference
            ctx.vars = vars
            self._artifact = artifact = self._get_artifact()

            if self.configSpec.artifact and self.configSpec.artifact.base_dir:
                ctx.base_dir = self.configSpec.artifact.base_dir
            elif self.configSpec.base_dir:
                ctx.base_dir = self.configSpec.base_dir
            self._inputs = self._to_resultsmap(ctx)
            if artifact:
                vars["implementation"] = artifact
                artifact.attributes.context.vars = self._inputs.context.vars
            else:
                vars["implementation"] = None
            vars["inputs"] = self._inputs
            vars["arguments"] = dict(eval="$inputs::arguments")
        return self._inputs

    def _get_artifact(self) -> Optional[ArtifactInstance]:
        artifact = None
        if self.configSpec.artifact and self.configSpec.artifact.parentNode:
            parent = self.target.root.find_instance(
                self.configSpec.artifact.parentNode.nested_name
            )
            if parent:
                artifact = parent.artifacts.get(self.configSpec.artifact.name)
                if not artifact:
                    artifact = ArtifactInstance(
                        self.configSpec.artifact.name,
                        template=self.configSpec.artifact,
                        parent=parent,
                    )
        return artifact

    def _to_resultsmap(self, ctx) -> ResultsMap:
        inputs: MutableMapping = self.configSpec.inputs
        artifact = self._artifact
        if artifact:
            # copy the ChainMap and insert inputs at the beginning
            attributes = artifact.attributes
            defs = artifact.template.propertyDefs.copy()
            self._execute_op = executeOp = _find_implementation(
                "unfurl.interfaces.Executable", "execute", artifact.template, True
            )
            if executeOp and executeOp.input_defs:
                # add defs to provide validation for operation inputs with the same name
                for name, prop in executeOp.get_declared_inputs().items():
                    defs[name] = prop
            if self.configSpec.input_defs:
                defs.update(self.configSpec.input_defs)
            inputs = ChainMap(inputs, attributes)
        else:
            defs = self.configSpec.input_defs or {}
        rm = ResultsMap(inputs, ctx, defs=defs)
        if "arguments" not in rm:
            # add to inputs as lazily evaluated expression function
            # this way "arguments" is recorded as a input digest key when accessed
            rm._attributes["arguments"] = dict(eval=dict(_arguments=None))
        return rm

    def _match_metadata_key(self, name, val):
        # check this metadata should be applied to this task's operation
        # if its value is a bool return that
        # if its value is a string or a list of strings, compare with the artifact name
        # and matching metadata on the artifact's execute operation if it set
        if not val:
            return False
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            val = [val]
        if self._artifact and self._artifact.name in val:
            return True
        keys = (
            self._execute_op
            and self._execute_op.metadata
            and self._execute_op.metadata.get(name)
        )
        if keys:
            if isinstance(keys, str):
                return keys in val
            for key in keys:
                if key in val:
                    return True
        return False

    def _get_inputs_from_properties(
        self, attributes: ResultsMap, name: str
    ) -> Dict[str, Any]:
        return {
            p.name: attributes[p.name]
            for p in attributes.defs.values()
            if p.schema.get("metadata", {}).get(name)
        }

    def _arguments(self) -> Dict[str, Any]:
        """
        Return the "arguments" variable.

        The item sources from lowest to highest priority are:
        * inputs set on the artifact's execute operation
        * artifact properties with the ToscaInputs._metadata_key (input_match)
        * target properties with ToscaInputs._metadata_key
        * operation inputs listed in "arguments" metadata key, if set
        * if "arguments" metadata key is missing, operation inputs with the same name as above inputs or the execute operation's input defs
        """
        key = ToscaInputs._metadata_key
        if self._execute_op and self._execute_op.inputs:
            execute_inputs = self._execute_op.inputs
        else:
            execute_inputs = {}
        if self._artifact:  # simple match
            execute_inputs.update(
                self._get_inputs_from_properties(self._artifact.attributes, key)
            )
            # don't include artifact properties:
            op_inputs = cast(ChainMap, self.inputs._attributes)._maps[0]
        else:
            op_inputs = self.inputs
        # full match
        execute_inputs.update({
            p.name: self.target.attributes[p.name]
            for p in self.target.attributes.defs.values()
            if self._match_metadata_key(key, p.schema.get("metadata", {}).get(key))
        })
        if self.configSpec.arguments is not None:
            execute_names = set(self.configSpec.arguments)
        else:
            input_defs = self._execute_op and self._execute_op.input_defs
            execute_names = set(execute_inputs) | set(input_defs or [])
        # operation inputs override execute inputs
        for name in execute_names:
            if name in op_inputs:
                execute_inputs[name] = self.inputs[name]
        return execute_inputs

    @property
    def vars(self) -> dict:
        """
        A dictionary of the same variables that are available to expressions when evaluating inputs.
        """
        return self.inputs.context.vars

    @property
    def connections(self) -> _ConnectionsMap:
        return self.inputs.context.vars["connections"]

    @staticmethod
    def _get_connection(
        source: HasInstancesInstance, target: Optional[NodeInstance], seen: dict
    ) -> None:
        """
        Find the requirements on source that match the target
        If source is root, requirements will be the connections that are the default_for the target.
        """
        if source is target:
            return None
        for rel in source.get_requirements(target):
            if id(rel) not in seen:
                seen[id(rel)] = rel

    def _get_connections(self) -> _ConnectionsMap:
        """
        Build a dictionary of connections by looking for instances that the task's implementation
        might to connect to (transitively following the target's hostedOn relationship)
        and adding any connections (relationships) that the operation_host has with those instances.
        Then add any default connections, prioritizing default connections to those instances.
        (Connections that explicity set a ``default_for`` key that matches those instances.)
        """
        seen: Dict[int, Any] = {}
        for parent in self.target.ancestors:
            if not isinstance(parent, NodeInstance):
                continue
            if parent is self.target.root:
                break
            if self.operation_host:
                self._get_connection(self.operation_host, parent, seen)
            self._get_connection(self.target.root, parent, seen)
        # get the rest of the default connections
        self._get_connection(self.target.root, None, seen)

        # reverse so nearest relationships replace less specific ones that have matching names
        connections = _ConnectionsMap(  # the list() is for Python 3.7
            (rel.name, rel) for rel in reversed(list(seen.values()))
        )
        return connections

    def _find_relationship_env_vars(self) -> dict:
        """
        Collect any environment variables set by the connections returned by ``_get_connections()``.

        Motivating example:
        Consider an operation whose target is a Kubernetes cluster hosted on GCP.
        The operation_host's connections to those instances might set KUBECONFIG and GOOGLE_APPLICATION_CREDENTIALS
        respectively and the operation's implementation will probably need both those set when it executes.
        """
        env = {}
        for rel in self.connections.by_type():  # only one per connection type
            env.update(rel.merge_props(find_env_vars, True))

        return env

    def get_environment(self, addOnly: bool, env: Optional[dict] = None) -> dict:
        """Return a dictionary of environment variables applicable to this task.

        Args:
          addOnly (bool): If addOnly is False all variables in the current os environment will be included
              otherwise only variables added will be included.

        Returns:
           :dict:

        Variable sources (by order of preference, lowest to highest):
        1. The ensemble's environment
        2. Variables set by the connections that are available to this operation.
        3. Variables declared in the operation's ``environment`` section.
        """
        if env is None:
            env = self.target.environ

        # build rules by order of preference (last overrides first):
        # 1. ensemble's environment
        # 2. variables set by connections
        # 3. operation's environment

        rules = self._find_relationship_env_vars()
        if self.configSpec.environment:
            rules.update(self.configSpec.environment)

        # apply rules
        # NB: Context.environ() calls TaskView.environ() which calls this method during initiation
        # If an expression being evaluated below invokes Context.environ a task error will be raised
        rules = serialize_value(
            map_value(rules, self.inputs.context), resolveExternal=True
        )
        env = filter_env(rules, env, addOnly=addOnly)

        # add the variables required by TOSCA 1.3 spec
        targets: List[str] = []
        if (
            isinstance(self.target, RelationshipInstance)
            and self.target.capability
            and self.target.target
        ):
            targets = [
                c.tosca_id
                for c in self.target.target.get_capabilities(
                    self.target.capability.template.name
                )
            ]
            env.update(
                dict(
                    TARGETS=",".join(targets),
                    TARGET=self.target.target.tosca_id,
                )
            )
            if self.target.source:
                SOURCES = ",".join([
                    r.tosca_id
                    for r in self.target.source.get_requirements(
                        self.target.template.name
                    )
                ])
                SOURCE = self.target.source.tosca_id
        return env

    def get_settings(self) -> dict:
        return dict(
            verbose=self.verbose,
            name=self.configSpec.name,
            dryrun=self.dry_run,
            workflow=self.configSpec.workflow,
            operation=self.configSpec.operation,
            timeout=self.configSpec.timeout,
            target=self.target.name,
            reason=self.reason,
            cwd=self.cwd,
        )

    @staticmethod
    def find_connection(
        ctx: RefContext,
        target: EntityInstance,
        relation: str = "tosca.relationships.ConnectsTo",
    ) -> Optional[RelationshipInstance]:
        """
        Find a relationship that this task can use to connect to the given instance.
        First look for relationship between the task's target instance and the given instance.
        If none is found, see if there a default connection of the given type.

        Args:
            target (NodeInstance): The instance to connect to.
            relation (str, optional): The relationship type. Defaults to ``tosca.relationships.ConnectsTo``.

        Returns:
            RelationshipInstance or None: The connection instance.
        """
        connection: Optional[RelationshipInstance] = None
        if ctx.vars.get("OPERATION_HOST"):
            operation_host = ctx.vars["OPERATION_HOST"].context.currentResource
            connection = cast(
                Optional[RelationshipInstance],
                Ref(
                    f"$operation_host::.requirements::*[.type={relation}][.target=$target]",
                    vars=dict(target=target, operation_host=operation_host),
                ).resolve_one(ctx),
            )

        # alternative query: [.type=unfurl.nodes.K8sCluster]::.capabilities::.relationships::[.type=unfurl.relationships.ConnectsTo.K8sCluster][.source=$OPERATION_HOST]
        if not connection:
            # no connection, see if there's a default relationship template defined for this target
            endpoints = target.get_default_relationships(relation)
            if endpoints:
                connection = endpoints[0]
        if connection:
            assert isinstance(connection, RelationshipInstance)
            return connection
        return None

    def sensitive(self, value: object) -> Union[sensitive, object]:
        """Mark the given value as sensitive. Sensitive values will be encrypted or redacted when outputed.

        Returns:
          sensitive: A copy of the value converted the appropriate subtype of :class:`unfurl.logs.sensitive` value or the value itself if it can't be converted.

        """
        return wrap_sensitive_value(value)

    def add_message(self, message: object) -> None:
        self.messages.append(message)

    def find_instance(self, name: str) -> Optional[NodeInstance]:
        root = self._manifest.get_root_resource()
        if root:
            return cast(Optional[NodeInstance], root.find_instance_or_external(name))
        return None

    # XXX
    # def pending(self, modified=None, sleep=100, waitFor=None, outputs=None):
    #     """
    #     >>> yield task.pending(60)
    #
    #     set modified to True to advise that target has already been modified
    #
    #     outputs to share operation outputs so far
    #     """

    def _set_outputs(self, outputs: Dict[str, Any]) -> None:
        metadata_key = self.configurator.attribute_output_metadata_key  # type: ignore
        for key, value in outputs.items():
            mapping = self.configSpec.outputs.get(key)
            if isinstance(mapping, dict):  # output type definition
                # XXX validate output value with output property
                mapping = mapping.get("mapping")
            if mapping:
                invalid = False
                if isinstance(mapping, list):
                    # XXX support more TOSCA mapping forms, e.g. to capabilities
                    # (see 3.6.15 Attribute Mapping definitions in TOSCA 1.3 spec)
                    if len(mapping) == 2 and mapping[0] == "SELF":
                        mapping = mapping[1]
                    else:
                        invalid = True
                if not isinstance(mapping, str):
                    invalid = True
                if invalid:
                    UnfurlTaskError(
                        self,
                        f"invalid or unsupported mapping for output '{key}': {mapping}",
                    )
                elif mapping == ".status":
                    if value:
                        self.target.local_status = to_enum(Status, value)
                elif mapping == ".state":
                    if value:
                        self.target.state = to_enum(NodeState, value)
                else:
                    self.target.attributes[mapping] = value
            else:
                attr_def = self.target.template.attributeDefs.get(key)
                if attr_def and (metadata := attr_def.schema.get("metadata")):
                    mkey = ToscaOutputs._metadata_key
                    metadata_value = metadata.get(mkey)
                    if self._match_metadata_key(mkey, metadata_value):
                        self.target.attributes[key] = value
                    elif metadata_key:
                        metadata_value = metadata.get(metadata_key)
                        if self._match_metadata_key(metadata_key, metadata_value):
                            self.target.attributes[key] = value

    def done(
        self,
        success: Optional[bool] = None,
        modified: Optional[Union[Status, bool]] = None,
        status: Optional[Status] = None,
        result: Optional[Union[dict, str]] = None,
        outputs: Optional[dict] = None,
        captureException: Optional[object] = None,
    ) -> ConfiguratorResult:
        """:py:meth:`unfurl.configurator.Configurator.run` should call this method and return or yield its return value before terminating.

        >>> yield task.done(True)

        Args:

          success (bool):  indicates if this operation completed without an error.
          modified (bool): (optional) indicates whether the physical instance was modified by this operation.
          status (Status): (optional) should be set if the operation changed the operational status of the target instance.
                   If not specified, the runtime will updated the instance status as needed, based
                   the operation preformed and observed changes to the instance (attributes changed).
          result (dict):  (optional) A dictionary that will be serialized as YAML into the changelog, can contain any useful data about these operation.
          outputs (dict): (optional) Operation outputs, as specified in the topology template.

        Returns:
              :class:`ConfiguratorResult`
        """
        if success is None:
            success = not self._errors
        if isinstance(modified, Status):
            status = modified
            modified = True

        if captureException is not None:
            logLevel = logging.DEBUG if success else logging.ERROR
            exception = UnfurlTaskError(self, captureException, logLevel)
        else:
            exception = None

        if outputs:
            self._set_outputs(outputs)
        return ConfiguratorResult(
            success,
            modified,
            status,
            result,
            outputs,
            exception,
        )

    # updates can be marked as dependencies (changes to dependencies changed) or required (error if changed)
    # configuration has cumulative set of changes made it to resources
    # updates update those changes
    # other configurations maybe modify those changes, triggering a configuration change
    def query(
        self,
        query: Union[str, dict],
        dependency: bool = False,
        name: Optional[str] = None,
        required: bool = False,
        wantList: bool = False,
        resolveExternal: bool = True,
        strict: bool = True,
        vars: Optional[dict] = None,
        throw: bool = False,
        trace: Optional[int] = None,
    ) -> Union[Any, Result, List[Result], None]:
        # XXX pass resolveExternal to context?
        try:
            result = Ref(query, vars=vars, trace=trace).resolve(
                self.inputs.context, wantList, strict
            )
        except Exception:
            if not throw:
                UnfurlTaskError(
                    self, f"error while evaluating query: {query}", logging.WARNING
                )
                return None
            raise

        if dependency:
            self.add_dependency(
                query, result, name=name, required=required, wantList=wantList
            )
        return result

    def add_dependency(
        self,
        expr: Union[str, Mapping],
        expected: Optional[Union[list, ResultsList, Result]] = None,
        schema: Optional[Mapping] = None,
        name: Optional[str] = None,
        required: bool = True,
        wantList: bool = False,
        target: Optional[EntityInstance] = None,
        write_only: Optional[bool] = None,
    ) -> "Dependency":
        getter = getattr(expr, "as_ref", None)
        if getter:
            # expr is a configuration or resource or ExternalValue
            expr = Ref(getter()).source

        dependency = Dependency(
            expr, expected, schema, name, required, wantList, target, write_only
        )
        for i, dep in enumerate(self.dependencies):
            assert isinstance(dep, Dependency)
            if dep.expr == expr or dep.name == name:
                self.dependencies[i] = dependency
                break
        else:
            self.dependencies.append(dependency)
        self._dependenciesChanged = True
        return dependency

    def remove_dependency(self, name: str) -> Optional["Dependency"]:
        for i, dep in enumerate(self.dependencies):
            assert isinstance(dep, Dependency)
            if dep.name == name:
                self.dependencies.pop(i)
                self._dependenciesChanged = True
                return dep
        return None

    # def createConfigurationSpec(self, name, configSpec):
    #     if isinstance(configSpec, str):
    #         configSpec = yaml.load(configSpec)
    #     return self._manifest.loadConfigSpec(name, configSpec)

    def create_sub_task(
        self,
        operation: Optional[Union[str, ConfigurationSpec]] = None,
        resource: Optional[EntityInstance] = None,
        inputs: Optional[dict] = None,
        persist: bool = False,
        required: Optional[bool] = None,
    ) -> Optional[TaskRequest]:
        """Create a subtask that will be executed if yielded by :py:meth:`unfurl.configurator.Configurator.run`

        Args:
          operation (str): The operation call (like ``interface.operation``)
          resource (:class:`NodeInstance`) The current target if missing.

        Returns:
           :class:`TaskRequest`
        """
        if resource is None:
            resource = self.target

        if inputs is None:
            inputs = self.configSpec.inputs

        if not operation:
            operation = f"{self.configSpec.interface}.{self.configSpec.operation}"
        if isinstance(operation, str):
            assert self.job
            taskRequest = create_task_request(
                self.job.jobOptions,
                operation,
                resource,
                "subtask: " + self.configSpec.name,
                inputs,
                # filter has matched this parent task, don't apply it again
                skip_filter=True,
            )
            if not taskRequest or taskRequest.error:
                return None
            else:
                taskRequest.persist = persist
                taskRequest.required = required
                return taskRequest

        # XXX:
        # # Configurations created by subtasks are transient insofar as the are not part of the spec,
        # # but they are recorded as part of the resource's configuration state.
        # # Marking as persistent or required will create a dependency on the new configuration.
        # if persist or required:
        #  expr = "::%s::.configurations::%s" % (configSpec.target, configSpec.name)
        #  self.add_dependency(expr, required=required)

        # otherwise operation should be a ConfigurationSpec
        return TaskRequest(operation, resource, "subtask", persist, required)

    def _update_instance(
        self, existingResource: EntityInstance, resourceSpec: Mapping
    ) -> bool:
        from .manifest import Manifest

        updated = False
        # XXX2 if spec is defined (not just status), there should be a way to
        # indicate this should replace an existing resource or throw an error
        if "readyState" in resourceSpec:
            # we need to set this explicitly for the attribute manager to track status
            # XXX track all status attributes (esp. state and created) and remove this hack
            operational = Manifest.load_status(resourceSpec)
            if operational.local_status is not None:
                existingResource.local_status = operational.local_status
                updated = True
            if operational.state is not None:
                existingResource.state = operational.state
                updated = True

        protected = resourceSpec.get("protected")
        if protected is not None and existingResource.protected != bool(protected):
            existingResource.protected = bool(protected)
            updated = True

        if (
            "customized" in resourceSpec
            and existingResource.customized != resourceSpec["customized"]
        ):
            existingResource.customized = resourceSpec["customized"]
            updated = True

        attributes = resourceSpec.get("attributes")
        if attributes:
            ctx = self.inputs.context.copy(existingResource)
            for key, value in attributes.items():
                existingResource.attributes[key] = map_value(value, ctx)
                self.logger.debug(
                    "setting attribute %s on %s with %s",
                    key,
                    existingResource.name,
                    value,
                )
            updated = True

        if resourceSpec.get("artifacts"):
            for key, val in resourceSpec["artifacts"].items():
                self._manifest._create_entity_instance(
                    ArtifactInstance, key, val, existingResource
                )

        template = resourceSpec.get("template")
        if template:
            assert self._manifest.tosca
            if isinstance(template, dict):
                tname = existingResource.template.name
                existingResource.template = (
                    existingResource.template.topology.add_template(
                        tname, template
                    )
                )
            elif (
                isinstance(template, str) and template != existingResource.template.name
            ):
                nodeSpec = existingResource.template.topology.get_node_template(
                    template
                )
                if not nodeSpec:
                    raise UnfurlTaskError(
                        self,
                        f"couldn't update TOSCA template for {existingResource.name}: '{template}' is not defined",
                    )
                existingResource.template = nodeSpec
            updated = True

        return updated

    def _parse_instances_tpl(
        self, instances: Union[str, Mapping, MutableSequence]
    ) -> Tuple[Optional[MutableSequence[Mapping]], Optional[UnfurlTaskError]]:
        if isinstance(instances, str):
            try:
                instances = yaml.load(instances)
            except:
                err = UnfurlTaskError(self, f"unable to parse as YAML: {instances}")
                return None, err

        if isinstance(instances, Mapping):
            instances = [instances]
        elif not isinstance(instances, MutableSequence):
            err = UnfurlTaskError(
                self,
                f"update_instances requires a list of updates, not a {type(instances)}",
            )
            return None, err
        return instances, None

    # # XXX how can we explicitly associate relations with target resources etc.?
    # # through capability attributes and dependencies/relationship attributes
    def update_instances(
        self, instances: Union[str, List[Dict[str, Any]]]
    ) -> Tuple[Optional[JobRequest], List[UnfurlTaskError]]:
        """Notify Unfurl of new or changed instances made while the task is running.

        This will queue a new child job if needed. To immediately run the child job based on the supplied spec, yield the returned JobRequest.

        Args:
          instances: Either a list or a string that is parsed as YAML.

        For example, this snipped creates a new instance and modifies the current target instance.

        .. code-block:: YAML

          # create a new instance:
          - name:     name-of-new-instance
            parent:   HOST # or SELF or <instance name>
            # all other fields should match the YAML in an ensemble's status section
            template: aNodeTemplate
            attributes:
               anAttribute: aValue
            readyState:
              local: ok
              state: started
          # modify an existing instance:
          - name: SELF
            # the following fields are supported (all are optional):
            template: aNodeTemplate
            attributes:
                anAttribute: aNewValue
                ...
            artifacts:
                artifact1:
                  ...
            readyState:
              local: ok
              state: started
            protected: true
            customized: true
        """
        instances, err = self._parse_instances_tpl(instances)  # type: ignore
        if err:
            return None, [err]

        errors: List[UnfurlTaskError] = []
        newResources = []
        newResourceSpecs = []
        updated_resources = []
        for resourceSpec in instances:
            # we might have items that aren't resource specs
            if not isinstance(resourceSpec, MutableMapping):
                continue
            # XXX deepcopy fails in test_terraform
            # originalResourceSpec = copy.deepcopy(resourceSpec)
            originalResourceSpec = copy.copy(resourceSpec)
            rname = map_value(resourceSpec.get("name", "SELF"), self.inputs.context)
            if rname == ".self" or rname == "SELF":
                existingResource: Optional[EntityInstance] = self.target
                rname = self.target.name
            elif rname == "HOST":
                existingResource = cast(
                    EntityInstance, self.target.parent or self.target.root
                )
            else:
                existingResource = self.find_instance(rname)

            newResource = None
            try:
                if existingResource:
                    updated = self._update_instance(existingResource, resourceSpec)
                    discovered = "" if existingResource is self.target else " dynamic "
                    if updated:
                        self.logger.info(
                            f'updating{discovered}instance "{existingResource.name}"',
                        )
                    else:
                        self.logger.debug(
                            f"no change to{discovered}instance %s",
                            existingResource.name,
                        )
                    if not discovered:
                        updated_resources.append(existingResource)
                else:
                    newResource = create_instance_from_spec(
                        self._manifest, self.target, rname, resourceSpec
                    )
                    if newResource and "priority" not in resourceSpec:
                        # set priority so this resource isn't ignored even if no one if referencing it
                        newResource.priority = Priority.required

                # XXX
                # if resource.required or resourceSpec.get("dependent"):
                #    self.add_dependency(resource, required=resource.required)
            except Exception:
                errors.append(
                    UnfurlAddingResourceError(self, originalResourceSpec, rname)
                )
            else:
                if newResource:
                    newResourceSpecs.append(originalResourceSpec)
                    newResources.append(newResource)

        if newResourceSpecs:
            self._resourceChanges.add_resources(newResourceSpecs)
            self._addedResources.extend(newResources)
            self.logger.info("add resources %s", newResources)
        if newResourceSpecs:  # XXX or updated_resources:
            jobRequest = JobRequest(newResources, errors)
            if self.job:
                self.job.jobRequestQueue.append(jobRequest)
            return jobRequest, errors
        return None, errors

    def set_work_folder(
        self,
        location: str = "operation",
        preserve: Optional[bool] = None,
        always_apply: bool = False,
    ) -> WorkFolder:
        if location in self._workFolders:
            return self._workFolders[location]
        if preserve is None:
            preserve = True if location in Folders.Persistent else False
        wf = WorkFolder(self, location, preserve)
        wf.always_apply = always_apply
        self._workFolders[location] = wf
        return wf
        # XXX multiple tasks can be accessing the same workfolder, so:
        # return self.job.setFolder(
        #     self, location, preserve
        # )

    def get_work_folder(self, location: Optional[str] = None) -> WorkFolder:
        # return self.job.getFolder(self, location)
        if location is None:
            if not self._workFolders:
                raise UnfurlTaskError(self, f"No task folder was set.")
            # XXX error if there is more than one?
            return next(iter(self._workFolders.values()))
        else:
            if location not in self._workFolders:
                raise UnfurlTaskError(self, f'missing "{location}" task folder')
            return self._workFolders[location]

    def discard_work_folders(self) -> None:
        while self._workFolders:
            _, wf = self._workFolders.popitem()
            wf.discard()

    def fail_work_folders(self) -> None:
        while self._workFolders:
            _, wf = self._workFolders.popitem()
            failed_path = wf.failed()
            if failed_path:
                self._failed_paths.append(failed_path)

    def apply_work_folders(self, *names: str) -> None:
        if not names:  # no args were passed, apply them all
            names = self._workFolders.keys()  # type: ignore
        for name in names:
            wf = self._workFolders.get(name)
            if wf and (wf.always_apply or self.status == Status.ok):  # type: ignore
                wf.apply()


class Dependency(Operational):
    """Represents a runtime dependency for a configuration.

    Dependencies are used to determine if a configuration needs re-run.
    They are automatically created when configurator accesses live attributes
    while handling a task. They also can be created when the configurator
    invokes these apis: `create_sub_task, `update_instances`, query`, `add_dependency`.
    """

    def __init__(
        self,
        expr: Union[str, Mapping],
        expected: Optional[Union[ResultsList, list, Result, None]] = None,
        schema: Optional[Mapping] = None,
        name: Optional[str] = None,
        required: bool = False,
        wantList: bool = False,
        target: Optional[EntityInstance] = None,
        write_only: Optional[bool] = None,
    ) -> None:
        """
        if schema is not None, validate the result using schema
        if expected is not None, test that result equals expected
        otherwise test that result isn't empty has not changed since the last attempt
        """
        assert not (expected and schema)
        self.expr = expr

        self.expected = expected
        self.schema = schema
        self._required = required
        if name:
            self.name = name
        elif isinstance(expr, str):
            self.name = str(expr)
        elif target:
            self.name = f"{target.name}:{dict(expr)}"
        else:
            self.name = f"{dict(expr)}"
        self.wantList = wantList
        self.target = target
        self.write_only = write_only

    def __repr__(self) -> str:
        return f"Dependency('{self.name}')"

    @property
    def local_status(self) -> Optional[Status]:
        if (
            self.target
            and self.target is not self.target.root
            and not self.target.is_computed()
        ):
            # (only care about local status of instances with live attribute, not their full operational status)
            # (reduces circular dependencies)
            return self.target.local_status
        else:  # the root has inputs which don't have operational status
            return Status.ok

    @property
    def priority(self) -> Priority:
        return Priority.required if self._required else Priority.optional

    def _is_unexpected(self) -> bool:
        if self.target is None:
            return True
        if isinstance(self.expected, Result):
            result = Ref(self.expr).resolve(RefContext(self.target), "result")
            if not result:
                return True
            return result[0] != self.expected
        else:
            return self.expected != Ref(self.expr).resolve(
                RefContext(self.target), self.wantList
            )

    def refresh(self, config: "ConfigTask") -> None:
        if self.expected is not None:
            changeId = config.changeId
            context = RefContext(
                config.target, dict(val=self.expected, changeId=changeId)
            )
            result = Ref(self.expr).resolve(context, wantList=self.wantList)
            assert isinstance(context._lastResource, EntityInstance)
            self.target = context._lastResource
            self.expected = result

    def validate(self):
        if not self.schema or not self.target:
            return True
        value = Ref(self.expr).resolve(RefContext(self.target))
        if isinstance(self.schema, dict):
            return not validate_schema(value, self.schema)
        else:
            assert isinstance(self.schema, Property), type(self.schema)
            try:
                self.schema._validate(value)
            except Exception:
                return False
        return True

    @staticmethod
    def has_value_changed(
        value: Union[Results, Mapping, MutableSequence, tuple, ChangeAware, Any],
        changeset: ChangeRecord,
    ) -> bool:
        if isinstance(value, Results):
            return Dependency.has_value_changed(value._attributes, changeset)
        elif isinstance(value, Mapping):
            if any(Dependency.has_value_changed(v, changeset) for v in value.values()):
                return True
        elif isinstance(value, (MutableSequence, tuple)):
            if any(Dependency.has_value_changed(v, changeset) for v in value):
                return True
        elif isinstance(value, ChangeAware):
            return value.has_changed(changeset)
        else:
            return False
        return False  # assuming this is what is meant by the function

    def has_changed(self, config: Optional["ChangeRecord"] = None) -> bool:
        if config is None:
            return self._is_unexpected()
        changeId = config.changeId
        if self.target and isinstance(config, ChangeRecordRecord):
            # Manifest.load_config_change sets config.target
            target_instance = self.target.query(config.target)
            if not isinstance(target_instance, EntityInstance):
                return False
            context = RefContext(
                target_instance, dict(val=self.expected, changeId=changeId)
            )
            result = Ref(self.expr).resolve_one(context)

            if self.schema:
                # result isn't as expected, something changed
                if not validate_schema(result, self.schema):
                    return False
            else:
                if self.expected is not None:
                    expected = map_value(self.expected, context)
                    if result != expected:
                        logger.debug("has_changed: %s != %s", result, expected)
                        return True
                elif not result:
                    # if expression no longer true (e.g. a resource wasn't found), then treat dependency as changed
                    return True

            if self.has_value_changed(result, config):
                return True

        return False
