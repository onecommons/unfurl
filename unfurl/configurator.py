import six
import collections
import re
import os
from .support import Status, Defaults, ResourceChanges, Priority
from .result import serializeValue, ChangeAware, Results, ResultsMap
from .util import (
    AutoRegisterClass,
    lookupClass,
    loadModule,
    validateSchema,
    findSchemaErrors,
    UnfurlError,
    UnfurlTaskError,
    UnfurlAddingResourceError,
)
from .eval import Ref, mapValue, RefContext
from .runtime import RelationshipInstance
from .tosca import Artifact
from ruamel.yaml import YAML

yaml = YAML()

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


class Environment(object):
    def __init__(self, vars=None, isolate=False, passvars=None, addinputs=False, **kw):
        """
        environment:
          isolate: true
          addinputs: true
          passvars:
            - ANSIBLE_VERBOSITY
            - UNFURL_LOGGING
            - ANDROID_*
          vars:
            FOO: "{{}}"
      """
        self.vars = vars or {}
        self.isolate = isolate
        self.passvars = passvars
        self.addinputs = addinputs

    # XXX add default passvars:
    # see https://tox.readthedocs.io/en/latest/config.html#tox-environment-settings list of default passenv
    # also SSH_AUTH_SOCK for ssh_agent
    def getSystemVars(self):
        # this need to execute on the operation_host the task is running on!
        if self.isolate:
            if self.passvars:  # XXX support glob, support UNFURL_PASSENV
                env = {k: v for k, v in os.environ.items() if k in self.passvars}
            else:
                env = {}
        else:
            env = os.environ.copy()
        return env

    def __eq__(self, other):
        if not isinstance(other, Environment):
            return False
        return (
            self.vars == other.vars
            and self.isolate == other.isolate
            and self.passvars == other.passvars
            and self.addinputs == other.addinputs
        )


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
        self.environment = Environment(**(environment or {}))
        self.inputs = inputs or {}
        self.inputSchema = inputSchema
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
        # XXX2 throw clearer exception if couldn't load class
        return lookupClass(self.className)(self)

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
            and self.inputSchema == self.inputSchema
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
        self.status = status
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


@six.add_metaclass(AutoRegisterClass)
class Configurator(object):
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
    """The interface presented to configurators.
  """

    def __init__(self, manifest, configSpec, target, reason=None, dependencies=None):
        # public:
        self.configSpec = configSpec
        self.target = target
        self.reason = reason
        self.logger = logger
        self.errors = []
        host = self._findOperationHost(target, configSpec.operationHost)
        self.operationHost = host
        # private:
        self._inputs = None
        self._environ = None
        self._manifest = manifest
        self.messages = []
        self._addedResources = []
        self._dependenciesChanged = False
        self.dependencies = dependencies or {}
        self._resourceChanges = ResourceChanges()

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
            # XXX why .attributes??
            HOST = (target.parent or target).attributes
            ORCHESTRATOR = target.root.imports.findImport("localhost")
            vars = dict(
                inputs=inputs,
                task=self.getSettings(),
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
        if not self.operationHost:
            return env
        t = lambda datatype: datatype.type == "unfurl.datatypes.EnvVar"
        for parent in reversed(self.target.ancestors):
            # use reversed() so nearer overrides farther
            for rel in self.operationHost.getRequirements(parent):
                # examine both the relationship's properties and its capability's properties
                capability = rel.parent
                for name, val in capability.template.findProps(
                    capability.attributes, t
                ):
                    if val is not None:
                        env[name] = val
                for name, val in rel.template.findProps(rel.attributes, t):
                    if val is not None:
                        env[name] = val
        return env

    @property
    def environ(self):
        if self._environ is None:
            env = self.configSpec.environment.getSystemVars()
            env.update(self._findRelationshipEnvVars())
            specvars = serializeValue(
                mapValue(self.configSpec.environment.vars, self.inputs.context),
                resolveExternal=True,
            )
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
            if self.configSpec.environment.addinputs:
                inputVars = serializeValue(self.inputs)
                env.update(inputVars, resolveExternal=True)
                for t in targets:
                    env.update({t + "_" + k: v for k, v in inputVars.items()})

            # XXX validate that all vars are bytes or string (json serialize if not?)
            env.update(specvars)
            self._environ = env

        return self._environ

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
        )

    def _findOperationHost(self, target, operation_host):
        # SELF, HOST, ORCHESTRATOR, SOURCE, TARGET
        if not operation_host or operation_host in ["localhost", "ORCHESTRATOR"]:
            return target.root.imports.findImport("localhost")
        if operation_host == "SELF":
            return target
        if operation_host == "HOST":
            return target.parent
        if operation_host == "SOURCE":
            return target.source
        if operation_host == "TARGET":
            return target.target
        host = target.root.findResource(operation_host)
        if host:
            return host
        raise UnfurlTaskError(self, "can not find operation_host: %s" % operation_host)

    def addMessage(self, message):
        self.messages.append(message)

    def findResource(self, name):
        return self._manifest.getRootResource().findResource(name)

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
        success,
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
        if isinstance(modified, Status):
            status = modified
            modified = True

        kw = dict(result=result, outputs=outputs)
        if captureException is not None:
            kw["exception"] = UnfurlTaskError(self, captureException, True)

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
        # XXX refcontext should include TARGET HOST etc
        # XXX pass resolveExternal to context?
        try:
            result = Ref(query, vars=vars).resolve(
                self.inputs.context, wantList, strict
            )
        except:
            UnfurlTaskError(self, "error evaluating query", True)
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

    def createSubTask(self, operation, resource=None, persist=False, required=False):
        """Create a subtask that will be executed if yielded by `run()`

           Args:
             operation (str): The operation call (like `interface.operation`)
             resource (:class:`NodeInstance`) The current target if missing.

           Returns:
              :class:`TaskRequest`
        """
        if resource is None:
            resource = self.target

        if isinstance(operation, six.string_types):
            # XXX add option to pass different inputs
            taskRequest = self.job.plan.generateConfiguration(
                operation,
                resource,
                "for subtask: " + self.configSpec.name,
                self.configSpec.inputs,
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
        """Notifies Unfurl new or changed resources made while the configurator was running.

        Operational state indicates if the resource currently exists or not.
        This will queue a new child job if needed.

        .. code-block:: YAML

          - name:     aNewResource
            template: aNodeTemplate
            parent:   HOST
            attributes:
               anAttribute: aValue
            status:
                readyState: ok
          - name:     SELF
            attributes:
                anAttribute: aNewValue

        Args:
          resources (list or str): Either a list or string that is parsed as YAML.

        Returns:
          :class:`JobRequest`: To run the job based on the supplied spec
              immediately, yield the returned JobRequest.
    """
        # XXX if template isn't specified deduce from provides and template keys
        from .manifest import Manifest

        if isinstance(resources, six.string_types):
            try:
                resources = yaml.load(resources)
            except:
                UnfurlTaskError(self, "unable to parse as YAML: %s" % resources, True)
                return None

        errors = []
        newResources = []
        newResourceSpecs = []
        for resourceSpec in resources:
            originalResourceSpec = resourceSpec
            try:
                rname = resourceSpec["name"]
                if rname == ".self" or rname == "SELF":
                    existingResource = self.target
                else:
                    existingResource = self.findResource(rname)
                if existingResource:
                    # XXX2 if spec is defined (not just status), there should be a way to
                    # indicate this should replace an existing resource or throw an error
                    status = resourceSpec.get("status")
                    operational = Manifest.loadStatus(status)
                    if operational.localStatus:
                        existingResource.localStatus = operational.localStatus
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

                resource = self._manifest.createNodeInstance(
                    rname, resourceSpec, parent=self.target.root
                )

                # XXX wrong... these need to be operational instances
                # if resource.required or resourceSpec.get("dependent"):
                #    self.addDependency(resource, required=resource.required)
            except:
                errors.append(
                    UnfurlAddingResourceError(self, originalResourceSpec, True)
                )
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


def getConfigSpecArgsFromImplementation(iDef, inputs, template):
    implementation = iDef.implementation
    kw = dict(inputs=inputs)
    configSpecArgs = ConfigurationSpec.getDefaults()
    artifact = None
    if isinstance(implementation, dict):
        for name, value in implementation.items():
            if name == "primary":
                artifact = Artifact(value, template, path=iDef._source)
            elif name == "dependencies":
                kw[name] = [
                    Artifact(artifactTpl, template, path=iDef._source)
                    for artifactTpl in value
                ]
            elif name in configSpecArgs:
                kw[name] = value

    else:
        artifact = Artifact(implementation, template, path=iDef._source)
    kw["primary"] = artifact

    if "className" not in kw:
        if not artifact:  # malformed implementation
            return None
        implementation = artifact.file
        try:
            if "#" in implementation:
                path, fragment = artifact.getPath()
                mod = loadModule(path)
                kw["className"] = mod.__name__ + "." + fragment
            else:
                lookupClass(implementation)
                kw["className"] = implementation
        except UnfurlError:
            # is it a shell script or a command line?
            # assume its a command line, create a ShellConfigurator
            kw["className"] = "unfurl.configurators.shell.ShellConfigurator"
            shell = inputs and inputs.get("shell")
            if shell is False or re.match(r"[\w.-]+\Z", implementation):
                # don't use the shell
                shellArgs = dict(command=[implementation])
            else:
                shellArgs = dict(command=implementation)
            if inputs:
                shellArgs.update(inputs)
            kw["inputs"] = shellArgs

    return kw
