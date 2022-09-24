# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Generator,
    List,
    Optional,
    Tuple,
    Union,
    ValuesView,
    cast,
)
from typing_extensions import TypedDict
import six
from collections.abc import Mapping, MutableSequence
import os
import copy

from unfurl.logs import UnfurlLogger

if TYPE_CHECKING:
    from unfurl.manifest import Manifest
    from unfurl.job import ConfigTask


from .support import Status, ResourceChanges, Priority, set_context_vars
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
    register_class,
    validate_schema,
    UnfurlTaskError,
    UnfurlAddingResourceError,
    filter_env,
    to_enum,
    wrap_sensitive_value,
    sensitive,
    truncate_str,
)
from . import merge
from .eval import Ref, map_value, RefContext
from .runtime import EntityInstance, NodeInstance, RelationshipInstance, Operational
from .yamlloader import yaml
from .projectpaths import WorkFolder, Folders
from .planrequests import (
    TaskRequest,
    JobRequest,
    ConfigurationSpec,  # import used by unit tests
    create_task_request,
    find_operation_host,
    create_instance_from_spec,
)
from .tosca import find_env_vars

import logging

# Tell mypy the logger is of type UnfurlLogger
logger = cast(UnfurlLogger, logging.getLogger("unfurl.task"))


class ConfiguratorResult:
    """
    Modified indicates whether the underlying state of configuration,
    was changed i.e. the physically altered the system this configuration represents.

    status reports the Status of the current configuration.
    """

    def __init__(
        self,
        success: bool,
        modified: Optional[bool],
        status: Optional[Status] = None,
        configChanged: bool = None,
        result: object = None,
        outputs: Optional[dict] = None,
        exception: Optional[UnfurlTaskError] = None,
    ) -> None:
        self.modified = modified
        self.status = to_enum(Status, status)
        self.configChanged = configChanged
        self.result = result
        self.success = success
        self.outputs = outputs
        self.exception = None

    def __str__(self) -> str:
        result = "" if self.result is None else str(self.result)[:240] + "..."
        return (
            "changes: "
            + (
                " ".join(
                    filter(
                        None,
                        [
                            # mypy isn't happy with the following lines, but they should work fine
                            self.success and "success",  # type: ignore
                            self.modified and "modified",  # type: ignore
                            self.status is not None and self.status.name,  # type: ignore
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


@six.add_metaclass(AutoRegisterClass)
class Configurator:

    short_name: Optional[str] = None
    """shortName can be used to customize the "short name" of the configurator
    as an alternative to using the full name ("module.class") when setting the implementation on an operation.
    (Titlecase recommended)"""

    exclude_from_digest = ()

    @classmethod
    def set_config_spec_args(klass, kw: dict, target: None) -> dict:
        return kw

    def __init__(self, configurationSpec: ConfigurationSpec) -> None:
        self.configSpec = configurationSpec

    def get_generator(self, task: "TaskView") -> Generator:
        return self.run(task)

    def render(self, task: "TaskView") -> None:
        """
        This method is called is called during the planning phase to give the configurator an
        opportunity to do early validation and error detection and generate any plan information or configuration files that the user may want to review before the running the deployment task.

        Property access and writes will be tracked and used to establish dynamic dependencies between instances so the plan can be ordered properly. Any updates made to instances maybe reverted if it has dependencies on attributes that might be changed later in the plan, so this method should be idempotent.

        Returns:
            The value returned here will subsequently be available as ``task.rendered``
        """
        return None

    # yields a JobRequest, TaskRequest or a ConfiguratorResult
    def run(self, task: "TaskView") -> Generator:
        """
        This should perform the operation specified in the :class:`ConfigurationSpec`
        on the :obj:`task.target`.

        Args:
            task (:class:`TaskView`) The task currently running.

        Yields:
            Should yield either a :class:`JobRequest`, :class:`TaskRequest`
            or a :class:`ConfiguratorResult` when done
        """
        yield task.done(False)

    def can_dry_run(self, task: "TaskView") -> bool:
        """
        Returns whether this configurator can handle a dry-runs for the given task.
        (And should check :attr:`.TaskView.dry_run` in during run().

        Args:
            task (:obj:`TaskView`) The task about to be run.

        Returns:
            bool
        """
        return False

    def can_run(self, task: "TaskView") -> Union[bool, str]:
        """
        Return whether or not the configurator can execute the given task
        depending on if this configurator support the requested action and parameters
        and given the current state of the target instance?

        Args:
            task (:class:`TaskView`) The task that is about to be run.

        Returns:
            (bool or str): Should return True or a message describing why the task couldn't be run.
        """
        return True

    def should_run(self, task: "TaskView") -> bool:
        """Does this configuration need to be run?"""
        return self.configSpec.should_run()

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
        inputs = task._resolved_inputs  # type: ignore

        # sensitive values are always redacted so no point in including them in the digest
        # (for cleaner output and security-in-depth)
        keys = [
            k
            for k in inputs.keys()
            if k not in self.exclude_from_digest
            and not isinstance(inputs[k].resolved, sensitive)
        ]
        values = [inputs[key] for key in keys]

        for dep in task.dependencies:
            if not isinstance(dep.expected, sensitive):
                keys.append(dep.expr)
                values.append(dep.expected)

        if keys:
            inputdigest = get_digest(values, manifest=task._manifest)
        else:
            inputdigest = ""

        digest = dict(digestKeys=",".join(keys), digestValue=inputdigest)
        task.logger.debug(
            "digest for %s: %s=%s", task.target.name, digest["digestKeys"], inputdigest
        )
        return digest

    def check_digest(self, task: "TaskView", changeset: ChangeRecord) -> bool:
        """
        Examine the previous :class:`ChangeRecord` generated by the previous time this operation
        was performed on the target instance and return whether it should be rerun or not.

        The default implementation recalculates the digest of input parameters that
        were accessed in the previous run.

        Args:
            task (:class:`TaskView`) The task that might execute this operation.

        Args:
            changest (:class:`ChangeRecord`) The task that might execute this operation.

        Returns:
            bool: True if configuration's digest has changed, False if it is the same.
        """
        _parameters = getattr(changeset, "digestKeys", "")
        newKeys = {k for k in task.inputs.keys() if k not in self.exclude_from_digest}
        task.logger.debug("checking digest for %s: %s", task.target.name, _parameters)
        if not _parameters:
            if newKeys:
                task.logger.verbose(
                    "digest keys for %s was previously empty, now found %s",
                    task.target.name,
                    newKeys,
                )
                return True
            else:
                return False
        else:
            keys = _parameters.split(",")
        oldInputs = {key for key in keys if "::" not in key}
        # XXX this check is incomplete because this isn't considering new inputs because we don't know if they will be resolved or not
        # we could fix this by having digest keys include unresolved inputs
        if oldInputs - newKeys:
            task.logger.verbose(
                "digest keys changed for %s: old %s, new %s",
                task.target.name,
                oldInputs,
                newKeys,
            )
            return True  # an old input was removed

        # only resolve the inputs and dependencies that were resolved before
        results = []
        for key in keys:
            if "::" in key:
                results.append(Ref(key).resolve(task.inputs.context, wantList="result"))
            else:
                results.append(task.inputs._getresult(key))

        newDigest = get_digest(results, manifest=task._manifest)
        # I can't find somewhere where digestValue is defined on ChangeRecord, but I'll assume the use is correct
        mismatch = changeset.digestValue != newDigest  # type: ignore
        if mismatch:
            task.logger.verbose(
                "digests didn't match for %s with %s: old %s, new %s",
                task.target.name,
                _parameters,
                changeset.digestValue,  # type: ignore
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
            rel.resolved.type if isinstance(rel, Result) else rel.type: rel
            for rel in reversed(list(self.values()))
        }
        return by_type.values()

    def __missing__(self, key: object) -> object:
        # the more specific connections are inserted first so this should find
        # the most relevant connection of the given type
        for value in self.values():
              # XXX why is value sometimes a Result?
            if isinstance(value, Result):
                value = value.resolved
            if (
                value.template.is_compatible_type(key)
                # hackish: match the local name of type
                or key == value.type.rpartition(".")[2]
            ):
                return value
        raise KeyError(key)


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
        dependencies: Optional[List["Dependency"]] = None,
    ) -> None:
        # public:
        self.configSpec = configSpec
        self.target = target
        self.reason = reason
        self.logger = logger
        self.cwd = os.path.abspath(self.target.base_dir)
        self.rendered: Any = None
        self.dry_run = None
        # private:
        self._errors: List[
            UnfurlTaskError
        ] = []  # UnfurlTaskError objects appends themselves to this list
        self._inputs: Optional[ResultsMap] = None
        self._environ = None
        self._manifest = manifest
        self.messages: List[object] = []
        self._addedResources: List[NodeInstance] = []
        self._dependenciesChanged = False
        self.dependencies = dependencies or []
        self._resourceChanges = ResourceChanges()
        self._workFolders: dict = {}
        self._rendering = False
        # public:
        self.operation_host = find_operation_host(target, configSpec.operation_host)

    @property
    def inputs(self) -> ResultsMap:
        """
        Exposes inputs and task settings as expression variables, so they can be accessed like:

        eval: $inputs::param

        or in jinja2 templates:

        {{ inputs.param }}
        """
        if self._inputs is None:
            assert self._attributeManager  # type: ignore
            assert self.target.root.attributeManager is self._attributeManager  # type: ignore
            ctx = RefContext(self.target, task=self)
            # deepcopy because ResultsMap might modify interior maps and lists
            inputs = copy.deepcopy(self.configSpec.inputs)
            relationship = isinstance(self.target, RelationshipInstance)
            if relationship:
                target = self.target.target  # type: ignore  # checked with isinstance above
                if not target:
                    target = self.target.root
            else:
                target = self.target
            HOST = (target.parent or target).attributes
            ORCHESTRATOR = target.root.find_instance_or_external("localhost")
            vars = dict(
                inputs=inputs,
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
                if self.target.source:
                    vars["SOURCE"] = self.target.source.attributes  # type: ignore
                vars["TARGET"] = target.attributes
            # expose inputs lazily to allow self-referencee
            ctx.vars = vars
            if self.configSpec.artifact and self.configSpec.artifact.base_dir:
                ctx.base_dir = self.configSpec.artifact.base_dir
            self._inputs = ResultsMap(inputs, ctx)
        return self._inputs

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
        source: NodeInstance, target: Optional[NodeInstance], seen: dict
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

    def get_environment(self, addOnly: bool) -> dict:
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

        env = os.environ.copy()
        # build rules by order of preference (last overrides first):
        # 1. ensemble's environment
        # 2. variables set by connections
        # 3. operation's environment

        # we use merge.copy() to preserve basedir
        rules = merge.copy(self.target.root.envRules)

        rules.update(self._find_relationship_env_vars())

        if self.configSpec.environment:
            rules.update(self.configSpec.environment)

        # apply rules
        rules = serialize_value(
            map_value(rules, self.inputs.context), resolveExternal=True
        )
        env = filter_env(rules, env, addOnly=addOnly)

        # add the variables required by TOSCA 1.3 spec
        targets: List[str] = []
        if isinstance(self.target, RelationshipInstance):
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
                    SOURCES=",".join(
                        [
                            r.tosca_id
                            for r in self.target.source.get_requirements(
                                self.target.requirement.template.name
                            )
                        ]
                    ),
                    SOURCE=self.target.source.tosca_id,
                )
            )
        return env

    def get_settings(self) -> dict:
        return dict(
            verbose=self.verbose,  # type: ignore
            name=self.configSpec.name,
            dryrun=self.dry_run,  # type: ignore
            workflow=self.configSpec.workflow,
            operation=self.configSpec.operation,
            timeout=self.configSpec.timeout,
            target=self.target.name,
            reason=self.reason,
            cwd=self.cwd,
        )

    def find_connection(
        self, target: NodeInstance, relation: str = "tosca.relationships.ConnectsTo"
    ) -> RelationshipInstance:
        """
        Find a relationship that this task can use to connect to the given instance.
        First look for relationship between the task's target instance and the given instance.
        If none is found, see if there a default connection of the given type.

        Args:
            target (NodeInstance): The instance to connect to.
            relation (str, optional): The relationship type. Defaults to "tosca.relationships.ConnectsTo".

        Returns:
            RelationshipInstance: The connection instance.
        """
        connection = self.query(
            f"$OPERATION_HOST::.requirements::*[.type={relation}][.target=$target]",
            vars=dict(target=target),
        )
        # alternative query: [.type=unfurl.nodes.K8sCluster]::.capabilities::.relationships::[.type=unfurl.relationships.ConnectsTo.K8sCluster][.source=$OPERATION_HOST]
        if not connection:
            # no connection, see if there's a default relationship template defined for this target
            endpoints = target.get_default_relationships(relation)
            if endpoints:
                connection = endpoints[0]
        return connection

    def sensitive(self, value: object) -> Union[sensitive, object]:
        """Mark the given value as sensitive. Sensitive values will be encrypted or redacted when outputed.

        Returns:
          sensitive: A copy of the value converted the appropriate subtype of :class:`unfurl.logs.sensitive` value or the value itself if it can't be converted.

        """
        return wrap_sensitive_value(value)

    def add_message(self, message: object) -> None:
        self.messages.append(message)

    def find_instance(self, name: str) -> NodeInstance:
        return self._manifest.get_root_resource().find_instance_or_external(name)

    # XXX
    # def pending(self, modified=None, sleep=100, waitFor=None, outputs=None):
    #     """
    #     >>> yield task.pending(60)
    #
    #     set modified to True to advise that target has already been modified
    #
    #     outputs to share operation outputs so far
    #     """

    def _set_outputs(self, outputs):
        for key, value in outputs.items():
            mapping = self.configSpec.outputs.get(key)
            if mapping:
                invalid = False
                if isinstance(mapping, list):
                    # XXX support more TOSCA mapping forms, e.g. to capabilities
                    if len(mapping) == 2 and mapping[0] == "SELF":
                        mapping = mapping[1]
                    else:
                        invalid = True
                if not isinstance(mapping, str):
                    invalid = True
                if invalid:
                    UnfurlTaskError(self, f"invalid or unsupported mapping for output '{key}': {mapping}")
                else:
                    self.target.attributes[mapping] = value

    def done(
        self,
        success: Optional[bool] = None,
        modified: Optional[bool] = None,
        status: Optional[Status] = None,
        result: Optional[Union[dict, str]] = None,
        outputs: Optional[
            dict
        ] = None,  # so the docstring says dict, but ConfiguratorResult
        captureException: Optional[object] = None,
    ) -> ConfiguratorResult:
        """:py:meth:`unfurl.configurator.Configurator.run` should call this method and yield its return value before terminating.

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

        kw = dict(result=result, outputs=outputs)  # type: kwType
        if captureException is not None:
            logLevel = logging.DEBUG if success else logging.ERROR
            kw["exception"] = UnfurlTaskError(self, captureException, logLevel)  # type: ignore

        # Typechecking
        class kwTypeBase(TypedDict):
            result: Optional[dict]
            outputs: Optional[dict]

        class kwType(kwTypeBase, total=False):
            exception: UnfurlTaskError

        kw = cast(kwType, kw)
        if outputs:
            self._set_outputs(outputs)
        return ConfiguratorResult(success, modified, status, **kw)

    # updates can be marked as dependencies (changes to dependencies changed) or required (error if changed)
    # configuration has cumulative set of changes made it to resources
    # updates update those changes
    # other configurations maybe modify those changes, triggering a configuration change
    def query(
        self,
        query: str,
        dependency: bool = False,
        name: Optional[str] = None,
        required: bool = False,
        wantList: bool = False,
        resolveExternal: bool = True,
        strict: bool = True,
        vars: Optional[dict] = None,
        throw: bool = False,
    ) -> Optional[Ref]:
        # XXX pass resolveExternal to context?
        try:
            result = Ref(query, vars=vars).resolve(
                self.inputs.context, wantList, strict
            )
        except:
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
        target: Optional[NodeInstance] = None,
    ) -> "Dependency":
        getter = getattr(expr, "as_ref", None)
        if getter:
            # expr is a configuration or resource or ExternalValue
            expr = Ref(getter()).source

        dependency = Dependency(
            expr, expected, schema, name, required, wantList, target
        )
        for i, dep in enumerate(self.dependencies):
            if dep.expr == expr or dep.name == name:
                self.dependencies[i] = dependency
                break
        else:
            self.dependencies.append(dependency)
        self._dependenciesChanged = True
        return dependency

    def remove_dependency(self, name: str) -> Optional["Dependency"]:
        for i, dep in enumerate(self.dependencies):
            if dep.name == name:
                self.dependencies.pop(i)
                self._dependenciesChanged = True
                return dep
        return None

    # def createConfigurationSpec(self, name, configSpec):
    #     if isinstance(configSpec, six.string_types):
    #         configSpec = yaml.load(configSpec)
    #     return self._manifest.loadConfigSpec(name, configSpec)

    def create_sub_task(
        self,
        operation: Optional[str] = None,
        resource: Optional[NodeInstance] = None,
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
        if isinstance(operation, six.string_types):
            taskRequest = create_task_request(
                self.job.jobOptions,  # type: ignore
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
        self, existingResource: NodeInstance, resourceSpec: Mapping
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
                existingResource.local_status = operational.local_status  # type: ignore
            if operational.state is not None:
                existingResource.state = operational.state  # type: ignore
            updated = True

        protected = resourceSpec.get("protected")
        if protected is not None:
            existingResource.protected = bool(protected)
            updated = True

        attributes = resourceSpec.get("attributes")
        if attributes:
            ctx = self.inputs.context.copy(existingResource)
            for key, value in attributes.items():
                existingResource.attributes[key] = map_value(value, existingResource)
                self.logger.debug(
                    "setting attribute %s on %s with %s",
                    key,
                    existingResource.name,
                    truncate_str(value),
                )
            updated = True
        return updated

    def _parse_instances_tpl(
        self, instances: Union[str, Mapping, MutableSequence]
    ) -> Tuple[Optional[List[Mapping]], Optional[UnfurlTaskError]]:
        if isinstance(instances, six.string_types):
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
        self, instances: Union[str, List]
    ) -> Tuple[Optional[JobRequest], List[UnfurlTaskError]]:
        """Notify Unfurl of new or changes to instances made while the configurator was running.

        Operational status indicates if the instance currently exists or not.
        This will queue a new child job if needed.

        To run the job based on the supplied spec immediately, yield the returned JobRequest.

        .. code-block:: YAML

          - name:     aNewInstance
            template: aNodeTemplate
            parent:   HOST
            attributes:
               anAttribute: aValue
            readyState:
              local: ok
              state: state
          - name:     SELF
            attributes:
                anAttribute: aNewValue

        Args:
          instances (list or str): Either a list or string that is parsed as YAML.

        """
        instances, err = self._parse_instances_tpl(instances)  # type: ignore
        if err:
            return None, [err]

        errors = []
        newResources = []
        newResourceSpecs = []
        for resourceSpec in instances:
            # we might have items that aren't resource specs
            if not isinstance(resourceSpec, Mapping):
                continue
            # XXX deepcopy fails in test_terraform
            # originalResourceSpec = copy.deepcopy(resourceSpec)
            originalResourceSpec = copy.copy(resourceSpec)
            rname = resourceSpec.get("name", "SELF")
            if rname == ".self" or rname == "SELF":
                existingResource = self.target
                rname = existingResource.name
            else:
                existingResource = self.find_instance(rname)

            newResource = None
            try:
                if existingResource:
                    updated = self._update_instance(existingResource, resourceSpec)
                    if updated:
                        self.logger.info("updating dynamic instance %s", existingResource.name)
                    else:
                        self.logger.debug("no change to dynamic instance %s", existingResource.name)
                else:
                    newResource = create_instance_from_spec(
                        self._manifest, self.target, rname, resourceSpec
                    )
                    if "priority" not in resourceSpec:
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

            jobRequest = JobRequest(newResources, errors)
            if self.job:  # type: ignore
                self.job.jobRequestQueue.append(jobRequest)  # type: ignore
            return jobRequest, errors
        return None, errors

    def set_work_folder(
        self, location: str = "operation", preserve: Optional[bool] = None
    ) -> WorkFolder:
        if location in self._workFolders:
            return self._workFolders[location]
        if preserve is None:
            preserve = True if location in Folders.Persistent else False
        wf = WorkFolder(self, location, preserve)
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
            wf.failed()

    def apply_work_folders(self, *names: str) -> None:
        if not names:  # no args were passed, apply them all
            names = self._workFolders.keys()  # type: ignore
        for name in names:
            wf = self._workFolders.get(name)
            if wf:
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
        expr: Union[str, dict],
        expected: Optional[Union[ResultsList, list, Result, None]] = None,
        schema: Optional[Mapping] = None,
        name: Optional[str] = None,
        required: bool = False,
        wantList: bool = False,
        target: Optional[NodeInstance] = None,
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
        self.name = name or expr
        self.wantList = wantList
        self.target = target

    @property
    def local_status(self) -> Status:
        if (
            self.target
            and self.target is not self.target.root
            and not self.target.is_computed()
        ):
            # (only care about local status of instances with live attribute, not their full operational status)
            # (reduces circular dependencies)
            return self.target.local_status  # type: ignore  # mypy has trouble with @property calls
        else:  # the root has inputs which don't have operational status
            return Status.ok

    @property
    def priority(self) -> Priority:
        return Priority.required if self._required else Priority.optional

    def refresh(self, config: "ConfigTask") -> None:
        if self.expected is not None:
            changeId = config.changeId
            context = RefContext(
                config.target, dict(val=self.expected, changeId=changeId)
            )
            result = Ref(self.expr).resolve(context, wantList=self.wantList)
            self.target = context._lastResource
            self.expected = result

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

    def has_changed(self, config: "ConfigTask") -> bool:
        changeId = config.changeId
        context = RefContext(config.target, dict(val=self.expected, changeId=changeId))
        result = Ref(self.expr).resolve_one(context)  # resolve(context, self.wantList)

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
