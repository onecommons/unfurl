# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import six
import collections
import os
from .support import Status, ResourceChanges, Priority, TopologyMap
from .result import serialize_value, ChangeAware, Results, ResultsMap, get_digest
from .util import (
    register_class,
    validate_schema,
    UnfurlTaskError,
    UnfurlAddingResourceError,
    filter_env,
    to_enum,
    wrap_sensitive_value,
)
from . import merge
from .eval import Ref, map_value, RefContext
from .runtime import RelationshipInstance, Operational
from .yamlloader import yaml
from .projectpaths import WorkFolder
from .planrequests import (
    TaskRequest,
    JobRequest,
    ConfigurationSpec,  # used by unit tests
    create_task_request,
    find_operation_host,
)
from .plan import find_parent_resource

import logging

logger = logging.getLogger("unfurl.task")


class ConfiguratorResult:
    """
    Modified indicates whether the underlying state of configuration,
    was changed i.e. the physically altered the system this configuration represents.

    status reports the Status of the current configuration.
    """

    def __init__(
        self,
        success,
        modified,
        status=None,
        configChanged=None,
        result=None,
        outputs=None,
        exception=None,
    ):
        self.modified = modified
        self.status = to_enum(Status, status)
        self.configChanged = configChanged
        self.result = result
        self.success = success
        self.outputs = outputs
        self.exception = None

    def __str__(self):
        result = "" if self.result is None else str(self.result)[:240] + "..."
        return (
            "changes: "
            + (
                " ".join(
                    filter(
                        None,
                        [
                            self.success and "success",
                            self.modified and "modified",
                            self.status is not None and self.status.name,
                        ],
                    )
                )
                or "none"
            )
            + "\n   "
            + result
        )


class AutoRegisterClass(type):
    def __new__(mcls, name, bases, dct):
        cls = type.__new__(mcls, name, bases, dct)
        if cls.shortName:
            name = cls.shortName
        elif name.endswith("Configurator"):
            name = name[: -len("Configurator")]
        if name:
            register_class(cls.__module__ + "." + cls.__name__, cls, name)
        return cls


@six.add_metaclass(AutoRegisterClass)
class Configurator:

    shortName = None
    """shortName can be used to customize the "short name" of the configurator
    as an alternative to using the full name ("module.class") when setting the implementation on an operation.
    (Titlecase recommended)"""

    exclude_from_digest = ()

    def __init__(self, configurationSpec):
        self.configSpec = configurationSpec

    def get_generator(self, task):
        return self.run(task)

    def render(self, task):
        return None

    # yields a JobRequest, TaskRequest or a ConfiguratorResult
    def run(self, task):
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

    def can_dry_run(self, task):
        """
        Returns whether this configurator can handle a dry-runs for the given task.
        (And should check :py:attribute::`task.dry_run` in during run()"".

        Args:
            task (:obj:`TaskView`) The task about to be run.

        Returns:
            bool
        """
        return False

    def can_run(self, task):
        """
        Return whether or not the configurator can execute the given task.

        Does this configurator support the requested action and parameters
        and given the current state of the target instance?

        Args:
            task (:class:`TaskView`) The task that is about to be run.

        Returns:
            (bool or str): Should return True or a message describing why the task couldn't be run.
        """
        return True

    def should_run(self, task):
        """Does this configuration need to be run?"""
        return self.configSpec.should_run()

    def save_digest(self, task):
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
        inputs = task._resolved_inputs
        keys = [k for k in inputs.keys() if k not in self.exclude_from_digest]

        values = [inputs[key] for key in keys]

        keys += [dep.expr for dep in task.dependencies]

        values += [dep.expected for dep in task.dependencies]
        if keys:
            inputdigest = get_digest(values, manifest=task._manifest)
        else:
            inputdigest = ""

        digest = dict(digestKeys=",".join(keys), digestValue=inputdigest)
        task.logger.debug(
            "digest for %s: %s=%s", task.target.name, digest["digestKeys"], inputdigest
        )
        return digest

    def check_digest(self, task, changeset):
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
            return bool(newKeys)
        keys = _parameters.split(",")
        oldInputs = {key for key in keys if "::" not in key}
        if oldInputs - newKeys:
            return True  # an old input was removed

        # only resolve the inputs and dependencies that were resolved before
        results = []
        for key in keys:
            if "::" in key:
                results.append(Ref(key).resolve(task.inputs.context, wantList="result"))
            else:
                results.append(task.inputs._getresult(key))

        newDigest = get_digest(results, manifest=task._manifest)
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


class TaskView:
    """The interface presented to configurators."""

    def __init__(self, manifest, configSpec, target, reason=None, dependencies=None):
        # public:
        self.configSpec = configSpec
        self.target = target
        self.reason = reason
        self.logger = logger
        self.cwd = os.path.abspath(self.target.base_dir)
        self.rendered = None
        # private:
        self._errors = []  # UnfurlTaskError objects appends themselves to this list
        self._inputs = None
        self._environ = None
        self._manifest = manifest
        self.messages = []
        self._addedResources = []
        self._dependenciesChanged = False
        self.dependencies = dependencies or []
        self._resourceChanges = ResourceChanges()
        self._workFolder = None
        # public:
        self.operationHost = find_operation_host(target, configSpec.operationHost)

    @property
    def inputs(self):
        """
        Exposes inputs and task settings as expression variables, so they can be accessed like:

        eval: $inputs::param

        or in jinja2 templates:

        {{ inputs.param }}
        """
        if self._inputs is None:
            inputs = self.configSpec.inputs.copy()
            relationship = isinstance(self.target, RelationshipInstance)
            if relationship:
                target = self.target.target
            else:
                target = self.target
            HOST = (target.parent or target).attributes
            ORCHESTRATOR = target.root.find_instance_or_external("localhost")
            vars = dict(
                inputs=inputs,
                task=self.get_settings(),
                connections=list(self._get_connections()),
                allConnections=self._get_all_connections(),
                TOPOLOGY=dict(inputs=target.root.inputs._attributes),
                NODES=TopologyMap(target.root),
                SELF=self.target.attributes,
                HOST=HOST,
                ORCHESTRATOR=ORCHESTRATOR and ORCHESTRATOR.attributes or {},
                OPERATION_HOST=self.operationHost
                and self.operationHost.attributes
                or {},
            )
            if relationship:
                vars["SOURCE"] = self.target.source.attributes
                vars["TARGET"] = target.attributes
            # expose inputs lazily to allow self-referencee
            ctx = RefContext(self.target, vars, task=self)
            if self.configSpec.artifact and self.configSpec.artifact.base_dir:
                ctx.base_dir = self.configSpec.artifact.base_dir
            self._inputs = ResultsMap(inputs, ctx)
        return self._inputs

    @property
    def vars(self):
        """
        A dictionary of the same variables that are available to expressions when evaluating inputs.
        """
        return self.inputs.context.vars

    def _get_connections(self):
        seen = set()
        for parent in reversed(self.target.ancestors):
            # use reversed() so nearer overrides farther
            # XXX broken if multiple requirements point to same parent (e.g. dev and prod connections)
            # XXX test if operationHost is external (e.g locahost) get_requirements() matches local parent
            found = False
            if self.operationHost:
                for rel in self.operationHost.get_requirements(parent):
                    # examine both the relationship's properties and its capability's properties
                    found = True
                    if id(rel) not in seen:
                        seen.add(id(rel))
                        yield rel

            if not found:
                # not found, see if there's a default connection
                # XXX this should use the same relationship type as findConnection()
                for rel in parent.get_default_relationships():
                    if id(rel) not in seen:
                        seen.add(id(rel))
                        yield rel

    def _find_relationship_env_vars(self):
        """
        We look for instances that the task's implementation might to connect to
        (by following the targets hostedOn relationships)
        and check if the operation_host has any relationships with those instances too.
        If it does, collect any environment variables set by those connections.

        For example, consider an implementation whose target is a Kubernetes cluster hosted on GCP.
        The operation_host's connections to those instances might set KUBECONFIG and GOOGLE_APPLICATION_CREDENTIALS
        respectively and the implementation will probably need both those set when it executes.
        """
        env = {}
        t = lambda datatype: datatype.type == "unfurl.datatypes.EnvVar"
        # XXX broken if multiple requirements point to same parent (e.g. dev and prod connections)
        for rel in self._get_connections():
            env.update(rel.merge_props(t))

        return env

    def get_environment(self, addOnly):
        # If addOnly is False (the default) all variables in `env` will be included
        # in the returned dict, otherwise only variables added will be returned

        # get the environment from the context, the operation
        # the relationship between the operationhost and the target and its hosts
        # and any default (ambient) connections

        # XXX order of preference (last overrides first):
        # external connections
        # context
        # host connections
        # operation

        env = os.environ.copy()
        # XXX externalEnvVars = self._findRelationshipEnvVarsExternal()
        # env.update(externalEnvVars)

        # note: inputs should be evaluated before environment
        # use merge.copy to preserve basedir
        rules = merge.copy(self.target.root.envRules)

        relEnvVars = self._find_relationship_env_vars()

        if self.configSpec.environment:
            rules.update(self.configSpec.environment)

        rules = serialize_value(
            map_value(rules, self.inputs.context), resolveExternal=True
        )
        env = filter_env(rules, env, addOnly=addOnly)
        env.update(relEnvVars)  # XXX should been updated when retrieved above

        targets = []
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

    def get_settings(self):
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

    def _get_all_connections(self):
        cons = {}
        if self.operationHost:
            for rel in self.operationHost.requirements:
                cons.setdefault(rel.name, []).append(rel)
        for rel in self.target.root.requirements:
            if rel.name not in cons:
                cons[rel.name] = [rel]

        return cons

    def find_connection(self, target, relation="tosca.relationships.ConnectsTo"):
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

    def sensitive(self, value):
        """Mark the given value as sensitive. Sensitive values will be encrypted or redacted when outputed.

        Returns:
          sensitive: A copy of the value converted the appropriate subtype of :class:`unfurl.sensitive` value or the value itself if it can't be converted.

        """
        return wrap_sensitive_value(
            value, self.operationHost and self.operationHost.templar._loader._vault
        )

    def add_message(self, message):
        self.messages.append(message)

    def find_instance(self, name):
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

    def done(
        self,
        success=None,
        modified=None,
        status=None,
        result=None,
        outputs=None,
        captureException=None,
    ):
        """`run()` should call this method and yield its return value before terminating.

        >>> yield task.done(True)

        Args:

          success (bool):  indicates if this operation completed without an error.
          modified (bool): (optional) indicates whether the physical instance was modified by this operation.
          status (Status): (optional) should be set if the operation changed the operational status of the target instance.
                   If not specified, the runtime will updated the instance status as needed, based
                   the operation preformed and observed changes to the instance (attributes changed).
          result (dict):  (optional) A dictionary that will be serialized as YAML into the changelog, can contain any useful data about these operation.
          outputs (dict): (optional) Operation outputs, as specified in the toplogy template.

        Returns:
              :class:`ConfiguratorResult`
        """
        if success is None:
            success = not self._errors
        if isinstance(modified, Status):
            status = modified
            modified = True

        kw = dict(result=result, outputs=outputs)
        if captureException is not None:
            logLevel = logging.DEBUG if success else logging.ERROR
            kw["exception"] = UnfurlTaskError(self, captureException, logLevel)

        return ConfiguratorResult(success, modified, status, **kw)

    # updates can be marked as dependencies (changes to dependencies changed) or required (error if changed)
    # configuration has cumulative set of changes made it to resources
    # updates update those changes
    # other configurations maybe modify those changes, triggering a configuration change
    def query(
        self,
        query,
        dependency=False,
        name=None,
        required=False,
        wantList=False,
        resolveExternal=True,
        strict=True,
        vars=None,
        throw=False,
    ):
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
        expr,
        expected=None,
        schema=None,
        name=None,
        required=True,
        wantList=False,
        target=None,
    ):
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

    def remove_dependency(self, name):
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
        self, operation, resource=None, inputs=None, persist=False, required=False
    ):
        """Create a subtask that will be executed if yielded by `run()`

        Args:
          operation (str): The operation call (like `interface.operation`)
          resource (:class:`NodeInstance`) The current target if missing.

        Returns:
           :class:`TaskRequest`
        """
        if resource is None:
            resource = self.target

        if inputs is None:
            inputs = self.configSpec.inputs

        if isinstance(operation, six.string_types):
            taskRequest = create_task_request(
                self.job.jobOptions,
                operation,
                resource,
                "for subtask: " + self.configSpec.name,
                inputs,
            )
            if taskRequest.error:
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

        # operation should be a ConfigurationSpec
        return TaskRequest(operation, resource, "subtask", persist, required)

    # # XXX how can we explicitly associate relations with target resources etc.?
    # # through capability attributes and dependencies/relationship attributes
    def update_resources(self, resources):
        """Notifies Unfurl of new or changes to instances made while the configurator was running.

        Operational status indicates if the instance currently exists or not.
        This will queue a new child job if needed.

        .. code-block:: YAML

          - name:     aNewResource
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
          resources (list or str): Either a list or string that is parsed as YAML.

        Returns:
          :class:`JobRequest`: To run the job based on the supplied spec
              immediately, yield the returned JobRequest.
        """
        from .manifest import Manifest

        if isinstance(resources, six.string_types):
            try:
                resources = yaml.load(resources)
            except:
                err = UnfurlTaskError(self, f"unable to parse as YAML: {resources}")
                return None, [err]

        if isinstance(resources, collections.Mapping):
            resources = [resources]
        elif not isinstance(resources, collections.MutableSequence):
            err = UnfurlTaskError(
                self,
                f"update_resources requires a list of updates, not a {type(resources)}",
            )
            return None, [err]

        errors = []
        newResources = []
        newResourceSpecs = []
        for resourceSpec in resources:
            # we might have items that aren't resource specs
            if not isinstance(resourceSpec, collections.Mapping):
                continue
            originalResourceSpec = resourceSpec
            rname = resourceSpec.get("name", "SELF")
            if rname == ".self" or rname == "SELF":
                existingResource = self.target
                rname = existingResource.name
            else:
                existingResource = self.find_instance(rname)
            try:

                if existingResource:
                    updated = False
                    # XXX2 if spec is defined (not just status), there should be a way to
                    # indicate this should replace an existing resource or throw an error
                    if "readyState" in resourceSpec:
                        # we need to set this explicitly for the attribute manager to track status
                        # XXX track all status attributes (esp. state and created) and remove this hack
                        operational = Manifest.load_status(resourceSpec)
                        if operational.local_status is not None:
                            existingResource.local_status = operational.local_status
                        if operational.state is not None:
                            existingResource.state = operational.state
                        updated = True

                    attributes = resourceSpec.get("attributes")
                    if attributes:
                        for key, value in map_value(
                            attributes, existingResource
                        ).items():
                            existingResource.attributes[key] = value
                            self.logger.debug(
                                "setting attribute %s with %s on %s",
                                key,
                                value,
                                existingResource.name,
                            )
                        updated = True

                    if updated:
                        self.logger.info("updating resources %s", existingResource.name)
                    continue

                pname = resourceSpec.get("parent")
                if pname in [".self", "SELF"]:
                    resourceSpec["parent"] = self.target.name
                elif pname == "HOST":
                    resourceSpec["parent"] = (
                        self.target.parent.name if self.target.parent else "root"
                    )

                if isinstance(resourceSpec.get("template"), dict):
                    # inline node template, add it to the spec
                    tname = resourceSpec["template"].pop("name", rname)
                    nodeSpec = self._manifest.tosca.add_node_template(
                        tname, resourceSpec["template"]
                    )
                    resourceSpec["template"] = nodeSpec.name

                if resourceSpec.get("readyState") and "created" not in resourceSpec:
                    # setting "created" to the target's key indicates that
                    # the target is responsible for deletion
                    # if "created" is not defined, set it if readyState is set
                    resourceSpec["created"] = self.target.key

                if (
                    self.job
                    and "parent" not in resourceSpec
                    and "template" in resourceSpec
                ):
                    nodeSpec = self._manifest.tosca.get_template(
                        resourceSpec["template"]
                    )
                    parent = find_parent_resource(self.target.root, nodeSpec)
                else:
                    parent = self.target.root
                # note: if resourceSpec[parent] is set it overrides the parent keyword
                resource = self._manifest.create_node_instance(
                    rname, resourceSpec, parent=parent
                )

                # XXX
                # if resource.required or resourceSpec.get("dependent"):
                #    self.add_dependency(resource, required=resource.required)
            except:
                errors.append(
                    UnfurlAddingResourceError(self, originalResourceSpec, rname)
                )
            else:
                newResourceSpecs.append(originalResourceSpec)
                newResources.append(resource)

        if newResourceSpecs:
            self._resourceChanges.add_resources(newResourceSpecs)
            self._addedResources.extend(newResources)
            self.logger.info("add resources %s", newResources)

            jobRequest = JobRequest(newResources, errors)
            if self.job:
                self.job.jobRequestQueue.append(jobRequest)
            return jobRequest, errors
        return None, errors

    # XXX multiple task can be accessing the same workfolder
    def set_work_folder(self, location="operation", preserve=False) -> WorkFolder:
        self._workFolder = WorkFolder(self, location, preserve)
        return self._workFolder
        # return self.job.setFolder(
        #     self, location, preserve
        # )

    def get_work_folder(self):
        return self._workFolder
        # return self.job.getFolder(self)


class Dependency(Operational):
    """Represents a runtime dependency for a configuration.

    Dependencies are used to determine if a configuration needs re-run.
    They are automatically created when configurator accesses live attributes
    while handling a task. They also can be created when the configurator
    invokes these apis: `create_sub_task, `update_resources`, query`, `add_dependency`.
    """

    def __init__(
        self,
        expr,
        expected=None,
        schema=None,
        name=None,
        required=False,
        wantList=False,
        target=None,
    ):
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
    def local_status(self):
        if self.target:
            return self.target.status
        else:
            return Status.ok

    @property
    def priority(self):
        return Priority.required if self._required else Priority.optional

    def refresh(self, config):
        if self.expected is not None:
            changeId = config.changeId
            context = RefContext(
                config.target, dict(val=self.expected, changeId=changeId)
            )
            result = Ref(self.expr).resolve(context, wantList=self.wantList)
            self.target = context._lastResource
            self.expected = result

    @staticmethod
    def has_value_changed(value, changeset):
        if isinstance(value, Results):
            return Dependency.has_value_changed(value._attributes, changeset)
        elif isinstance(value, collections.Mapping):
            if any(Dependency.has_value_changed(v, changeset) for v in value.values()):
                return True
        elif isinstance(value, (collections.MutableSequence, tuple)):
            if any(Dependency.has_value_changed(v, changeset) for v in value):
                return True
        elif isinstance(value, ChangeAware):
            return value.has_changed(changeset)
        else:
            return False

    def has_changed(self, config):
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
