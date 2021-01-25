# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import six
import collections
import re
import os
from .support import Status, Defaults, ResourceChanges
from .result import serializeValue, ChangeAware, Results, ResultsMap
from .util import (
    registerClass,
    lookupClass,
    loadModule,
    validateSchema,
    findSchemaErrors,
    UnfurlError,
    UnfurlTaskError,
    UnfurlAddingResourceError,
    filterEnv,
    toEnum,
    wrapSensitiveValue,
)
from . import merge
from .eval import Ref, mapValue, RefContext
from .runtime import RelationshipInstance
from .yamlloader import yaml

import logging

logger = logging.getLogger("unfurl.task")


class TaskRequest(object):
    """
    Yield this to run a child task. (see :py:meth:`unfurl.configurator.TaskView.createSubTask`)
    """

    def __init__(self, configSpec, resource, reason, persist=False, required=None):
        self.configSpec = configSpec
        self.target = resource
        self.reason = reason
        self.persist = persist
        self.required = required
        self.error = configSpec.name == "#error"


class JobRequest(object):
    """
    Yield this to run a child job.
    """

    def __init__(self, resources, errors):
        self.instances = resources
        self.errors = errors

    def __repr__(self):
        return "JobRequest(%s)" % (self.instances,)


# we want ConfigurationSpec to be standalone and easily serializable
class ConfigurationSpec(object):
    @classmethod
    def getDefaults(self):
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

    def findInvalidateInputs(self, inputs):
        if not self.inputSchema:
            return []
        return findSchemaErrors(serializeValue(inputs), self.inputSchema)

    # XXX same for postConditions
    def findInvalidPreconditions(self, target):
        if not self.preConditions:
            return []
        # XXX this should be like a Dependency object
        expanded = serializeValue(target.attributes)
        return findSchemaErrors(expanded, self.preConditions)

    def create(self):
        klass = lookupClass(self.className)
        if not klass:
            raise UnfurlError("Could not load configurator %s" % self.className)
        else:
            return klass(self)

    def shouldRun(self):
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


class ConfiguratorResult(object):
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
        self.status = toEnum(Status, status)
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
            registerClass(cls.__module__ + "." + cls.__name__, cls, name)
        return cls


@six.add_metaclass(AutoRegisterClass)
class Configurator(object):

    shortName = None
    """shortName can be used to customize the "short name" of the configurator
    as an alternative to using the full name ("module.class") when setting the implementation on an operation.
    (Titlecase recommended)"""

    def __init__(self, configurationSpec):
        self.configSpec = configurationSpec

    def getGenerator(self, task):
        return self.run(task)

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

    def canDryRun(self, task):
        """
        Returns whether this configurator can handle a dry-runs for the given task.
        (And should check `task.dryRun` in during run()"".

        Args:
            task (:obj:`TaskView`) The task about to be run.

        Returns:
            bool
        """
        return False

    def canRun(self, task):
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

    def shouldRun(self, task):
        """Does this configuration need to be run?"""
        return self.configSpec.shouldRun()

    # XXX3 should be called during when checking dependencies
    # def checkConfigurationStatus(self, task):
    #   """Is this configuration still valid?"""
    #   return Status.ok


class TaskView(object):
    """The interface presented to configurators."""

    def __init__(self, manifest, configSpec, target, reason=None, dependencies=None):
        # public:
        self.configSpec = configSpec
        self.target = target
        self.reason = reason
        self.logger = logger
        self.cwd = os.path.abspath(self.target.baseDir)
        # private:
        self._errors = []  # UnfurlTaskError objects appends themselves to this list
        self._inputs = None
        self._environ = None
        self._manifest = manifest
        self.messages = []
        self._addedResources = []
        self._dependenciesChanged = False
        self.dependencies = dependencies or {}
        self._resourceChanges = ResourceChanges()
        # public:
        self.operationHost = self._findOperationHost(target, configSpec.operationHost)

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
            ORCHESTRATOR = target.root.findInstanceOrExternal("localhost")
            vars = dict(
                inputs=inputs,
                task=self.getSettings(),
                connections=list(self._getConnections()),
                allConnections=self._getAllConnections(),
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
            self._inputs = ResultsMap(inputs, RefContext(self.target, vars))
        return self._inputs

    @property
    def vars(self):
        """
        A dictionary of the same variables that are available to expressions when evaluating inputs.
        """
        return self.inputs.context.vars

    def _getConnections(self):
        seen = set()
        for parent in reversed(self.target.ancestors):
            # use reversed() so nearer overrides farther
            # XXX broken if multiple requirements point to same parent (e.g. dev and prod connections)
            # XXX test if operationHost is external (e.g locahost) getRequirements() matches local parent
            found = False
            if self.operationHost:
                for rel in self.operationHost.getRequirements(parent):
                    # examine both the relationship's properties and its capability's properties
                    found = True
                    if id(rel) not in seen:
                        seen.add(id(rel))
                        yield rel

            if not found:
                # not found, see if there's a default connection
                # XXX this should use the same relationship type as findConnection()
                for rel in parent.getDefaultRelationships():
                    if id(rel) not in seen:
                        seen.add(id(rel))
                        yield rel

    def _findRelationshipEnvVars(self):
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
        for rel in self._getConnections():
            env.update(rel.mergeProps(t))

        return env

    def getEnvironment(self, addOnly):
        # note: inputs should be evaluated before environment
        # use merge.copy to preserve basedir
        rules = merge.copy(self.target.root.envRules)

        if self.configSpec.environment:
            rules.update(self.configSpec.environment)
        rules = serializeValue(
            mapValue(rules, self.inputs.context), resolveExternal=True
        )
        env = filterEnv(rules, addOnly=addOnly)
        relEnvVars = self._findRelationshipEnvVars()
        env.update(relEnvVars)
        targets = []
        if isinstance(self.target, RelationshipInstance):
            targets = [
                c.tosca_id
                for c in self.target.target.getCapabilities(
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
                            for r in self.target.source.getRequirements(
                                self.target.requirement.template.name
                            )
                        ]
                    ),
                    SOURCE=self.target.source.tosca_id,
                )
            )
        return env

    def getSettings(self):
        return dict(
            verbose=self.verbose,
            name=self.configSpec.name,
            dryrun=self.dryRun,
            workflow=self.configSpec.workflow,
            operation=self.configSpec.operation,
            timeout=self.configSpec.timeout,
            target=self.target.name,
            reason=self.reason,
            cwd=self.cwd,
        )

    def _findOperationHost(self, target, operation_host):
        # SELF, HOST, ORCHESTRATOR, SOURCE, TARGET
        if not operation_host or operation_host in ["localhost", "ORCHESTRATOR"]:
            return target.root.findInstanceOrExternal("localhost")
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
        host = target.root.findInstanceOrExternal(operation_host)
        if host:
            return host
        raise UnfurlTaskError(self, "can not find operation_host: %s" % operation_host)

    def _getAllConnections(self):
        cons = {}
        if self.operationHost:
            for rel in self.operationHost.requirements:
                cons.setdefault(rel.name, []).append(rel)
        for rel in self.target.root.requirements:
            if rel.name not in cons:
                cons[rel.name] = [rel]

        return cons

    def findConnection(self, target, relation="tosca.relationships.ConnectsTo"):
        connection = self.query(
            "$OPERATION_HOST::.requirements::*[.type=%s][.target=$target]" % relation,
            vars=dict(target=target),
        )
        # alternative query: [.type=unfurl.nodes.K8sCluster]::.capabilities::.relationships::[.type=unfurl.relationships.ConnectsTo.K8sCluster][.source=$OPERATION_HOST]
        if not connection:
            # no connection, see if there's a default relationship template defined for this target
            endpoints = target.getDefaultRelationships(relation)
            if endpoints:
                connection = endpoints[0]
        return connection

    def sensitive(self, value):
        """Mark the given value as sensitive. Sensitive values will be encrypted or redacted when outputed.

        Returns:
          sensitive: A subtype of `sensitive` appropriate for the value or the value itself if it can't be converted.

        """
        return wrapSensitiveValue(
            value, self.operationHost and self.operationHost.templar._loader._vault
        )

    def addMessage(self, message):
        self.messages.append(message)

    def findInstance(self, name):
        return self._manifest.getRootResource().findInstanceOrExternal(name)

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
    ):
        # XXX pass resolveExternal to context?
        try:
            result = Ref(query, vars=vars).resolve(
                self.inputs.context, wantList, strict
            )
        except:
            UnfurlTaskError(
                self, "error while evaluating query: %s" % query, logging.WARNING
            )
            return None

        if dependency:
            self.addDependency(
                query, result, name=name, required=required, wantList=wantList
            )
        return result

    def addDependency(
        self,
        expr,
        expected=None,
        schema=None,
        name=None,
        required=False,
        wantList=False,
    ):
        getter = getattr(expr, "asRef", None)
        if getter:
            # expr is a configuration or resource or ExternalValue
            expr = Ref(getter()).source

        dependency = Dependency(expr, expected, schema, name, required, wantList)
        self.dependencies[name or expr] = dependency
        self.dependenciesChanged = True
        return dependency

    def removeDependency(self, name):
        old = self.dependencies.pop(name, None)
        if old:
            self.dependenciesChanged = True
        return old

    # def createConfigurationSpec(self, name, configSpec):
    #     if isinstance(configSpec, six.string_types):
    #         configSpec = yaml.load(configSpec)
    #     return self._manifest.loadConfigSpec(name, configSpec)

    def createSubTask(
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
            taskRequest = self.job.plan.createTaskRequest(
                operation, resource, "for subtask: " + self.configSpec.name, inputs
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
        #  self.addDependency(expr, required=required)

        # operation should be a ConfigurationSpec
        return TaskRequest(operation, resource, "subtask", persist, required)

    # # XXX how can we explicitly associate relations with target resources etc.?
    # # through capability attributes and dependencies/relationship attributes
    def updateResources(self, resources):
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
                UnfurlTaskError(self, "unable to parse as YAML: %s" % resources)
                return None

        if isinstance(resources, collections.Mapping):
            resources = [resources]
        elif not isinstance(resources, collections.MutableSequence):
            UnfurlTaskError(
                self,
                "updateResources requires a list of updates, not a %s"
                % type(resources),
            )
            return None

        errors = []
        newResources = []
        newResourceSpecs = []
        for resourceSpec in resources:
            originalResourceSpec = resourceSpec
            try:
                rname = resourceSpec.get("name", "SELF")
                if rname == ".self" or rname == "SELF":
                    existingResource = self.target
                else:
                    existingResource = self.findInstance(rname)

                if existingResource:
                    # XXX2 if spec is defined (not just status), there should be a way to
                    # indicate this should replace an existing resource or throw an error
                    if "readyState" in resourceSpec:
                        # we need to set this explicitly for the attribute manager to track status
                        # XXX track all status attributes (esp. state and created) and remove this hack
                        operational = Manifest.loadStatus(resourceSpec)
                        if operational.localStatus is not None:
                            existingResource.localStatus = operational.localStatus
                        if operational.state is not None:
                            existingResource.state = operational.state

                    attributes = resourceSpec.get("attributes")
                    if attributes:
                        for key, value in mapValue(
                            attributes, existingResource
                        ).items():
                            existingResource.attributes[key] = value
                            logger.debug(
                                "setting attribute %s with %s on %s",
                                key,
                                value,
                                existingResource.name,
                            )
                    logger.info("updating resources %s", existingResource.name)
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
                    nodeSpec = self._manifest.tosca.addNodeTemplate(
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
                    nodeSpec = self._manifest.tosca.getTemplate(
                        resourceSpec["template"]
                    )
                    parent = (
                        self.job.plan.findParentResource(nodeSpec) or self.target.root
                    )
                else:
                    parent = self.target.root
                # note: if resourceSpec[parent] is set it overrides the parent keyword
                resource = self._manifest.createNodeInstance(
                    rname, resourceSpec, parent=parent
                )

                # XXX wrong... these need to be operational instances
                # if resource.required or resourceSpec.get("dependent"):
                #    self.addDependency(resource, required=resource.required)
            except:
                errors.append(UnfurlAddingResourceError(self, originalResourceSpec))
            else:
                newResourceSpecs.append(originalResourceSpec)
                newResources.append(resource)

        if newResourceSpecs:
            self._resourceChanges.addResources(newResourceSpecs)
            self._addedResources.extend(newResources)
            logger.info("add resources %s", newResources)

            jobRequest = JobRequest(newResources, errors)
            if self.job:
                self.job.jobRequestQueue.append(jobRequest)
            return jobRequest
        return None


class Dependency(ChangeAware):
    """Represents a runtime dependency for a configuration.


      Dependencies are used to determine if a configuration needs re-run as follows:

    * Tosca `DependsOn`

      * They are dynamically created when evaluating and comparing the
        configuration spec's attributes with the previous values

      * Persistent dependencies can be created when the configurator
        invokes these apis: `createSubTask`, `updateResources`, `query`, `addDependency`
    """

    def __init__(
        self,
        expr,
        expected=None,
        schema=None,
        name=None,
        required=False,
        wantList=False,
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
        self.required = required
        self.name = name
        self.wantList = wantList

    def refresh(self, config):
        if self.expected is not None:
            changeId = config.changeId
            context = RefContext(
                config.target, dict(val=self.expected, changeId=changeId)
            )
            result = Ref(self.expr).resolve(context, wantList=self.wantList)
            self.expected = result

    @staticmethod
    def hasValueChanged(value, changeset):
        if isinstance(value, Results):
            return Dependency.hasValueChanged(value._attributes, changeset)
        elif isinstance(value, collections.Mapping):
            if any(Dependency.hasValueChanged(v, changeset) for v in value.values()):
                return True
        elif isinstance(value, (collections.MutableSequence, tuple)):
            if any(Dependency.hasValueChanged(v, changeset) for v in value):
                return True
        elif isinstance(value, ChangeAware):
            return value.hasChanged(changeset)
        else:
            return False

    def hasChanged(self, config):
        changeId = config.changeId
        context = RefContext(config.target, dict(val=self.expected, changeId=changeId))
        result = Ref(self.expr).resolveOne(context)  # resolve(context, self.wantList)

        if self.schema:
            # result isn't as expected, something changed
            if not validateSchema(result, self.schema):
                return False
        else:
            if self.expected is not None:
                expected = mapValue(self.expected, context)
                if result != expected:
                    logger.debug("hasChanged: %s != %s", result, expected)
                    return True
            elif not result:
                # if expression no longer true (e.g. a resource wasn't found), then treat dependency as changed
                return True

        if self.hasValueChanged(result, config):
            return True

        return False


def _setDefaultCommand(kw, implementation, inputs):
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


def getConfigSpecArgsFromImplementation(iDef, inputs, template):
    # XXX template should be operation_host's template!
    implementation = iDef.implementation
    kw = dict(inputs=inputs, outputs=iDef.outputs)
    configSpecArgs = ConfigurationSpec.getDefaults()
    artifact = None
    if isinstance(implementation, dict):
        for name, value in implementation.items():
            if name == "primary":
                artifact = template.findOrCreateArtifact(value, path=iDef._source)
            elif name == "dependencies":
                kw[name] = [
                    template.findOrCreateArtifact(artifactTpl, path=iDef._source)
                    for artifactTpl in value
                ]
            elif name in configSpecArgs:
                kw[name] = value

    else:
        # "either because it refers to a named artifact specified in the artifacts section of a type or template,
        # or because it represents the name of a script in the CSAR file that contains the definition."
        artifact = template.findOrCreateArtifact(implementation, path=iDef._source)
    kw["primary"] = artifact
    assert artifact or "className" in kw

    if "className" not in kw:
        if not artifact:  # malformed implementation
            return None
        implementation = artifact.file
        try:
            # see if implementation looks like a python class
            if "#" in implementation:
                path, fragment = artifact.getPathAndFragment()
                mod = loadModule(path)
                kw["className"] = mod.__name__ + "." + fragment
                return kw
            elif lookupClass(implementation):
                kw["className"] = implementation
                return kw
        except:
            pass
        # assume it's a command line
        logger.debug(
            "interpreting 'implementation' as a shell command: %s", implementation
        )
        _setDefaultCommand(kw, implementation, inputs)
    return kw
