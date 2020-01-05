"""
This module defines the core model and implements the runtime operations of the model.

The state of the system is represented as a collection of Resources.
Each resource have a status; attributes that describe its state; and a TOSCA template
which describes its capabilities, relationships and available interfaces for configuring and interacting with it.
"""
import six
import collections

from ansible.template import Templar
from ansible.parsing.dataloader import DataLoader

from .util import UnfurlError, loadClass, toEnum
from .result import ResourceRef, ChangeAware

# from .local import LocalEnv
from .support import AttributeManager, Defaults, Status, Priority, NodeState
from .tosca import CapabilitySpec, RelationshipSpec, NodeSpec, TopologySpec

import logging

logger = logging.getLogger("unfurl")

# CF 3.4.1 Node States p61


class Operational(ChangeAware):
    """
  This is an abstract base class for Jobs, Resources, and Configurations all have a Status associated with them
  and all use the same algorithm to compute their status from their dependent resouces, tasks, and configurations
  """

    # XXX3 add repairable, messages?

    # core properties to override
    @property
    def priority(self):
        return Defaults.shouldRun

    @property
    def localStatus(self):
        return Status.unknown

    @property
    def state(self):
        return NodeState.initial

    def getOperationalDependencies(self):
        return ()

    @property
    def manualOverideStatus(self):
        return None

    # derived properties:
    @property
    def operational(self):
        return self.status == Status.ok or self.status == Status.degraded

    @property
    def active(self):
        return self.status <= Status.error

    @property
    def status(self):
        """
        Return the effective status, considering first the local readyState and
        then the aggregate status' of its dependencies (see `aggregateStatus()`
        and `getOperationalDependencies()`).

        If the localStatus is non-operational, that takes precedence.
        Otherwise the localStatus is compared with its aggregate dependent status
        and worser value is choosen.
        """
        if self.manualOverideStatus is not None:
            status = self.manualOverideStatus
            if status >= Status.error:
                return status
        else:
            status = self.localStatus

        if not status:
            return Status.unknown

        if status >= Status.error:
            # return error, pending, or notpresent
            return status

        dependentStatus = self.aggregateStatus(self.getOperationalDependencies())
        if dependentStatus is None:
            # note if localStatus is a no op (status purely determined by dependents)
            # it should be set to ok
            # then ok + None = ok, which makes sense, since it has no dependendents
            return status  # return local status
        else:
            # local status is ok, degraded
            return max(status, dependentStatus)

    @property
    def required(self):
        return self.priority == Priority.required

    # `lastConfigChange` indicated the last configuration change applied to the instance.
    # `lastStateChange` is last observed change to the internal state of the instance. This may be reflected in attributes of the instance or completely opaque to the service template's model.

    # The computed attribute `lastChange` is whichever of the above two attributes are more recent.

    @property
    def lastStateChange(self):
        return None

    @property
    def lastConfigChange(self):
        return None

    @property
    def lastChange(self):
        if not self.lastStateChange:
            return self.lastConfigChange
        elif not self.lastConfigChange:
            return self.lastStateChange
        else:
            return max(self.lastStateChange, self.lastConfigChange)

    def hasChanged(self, changeset):
        # if changed since the last time we checked
        if not self.lastChange:
            return False
        if not changeset:
            return True
        return self.lastChange > changeset.changeId

    @staticmethod
    def aggregateStatus(statuses):
        """
        Returns: ok, degraded, pending or None

        If there are no instances, return None
        If any required are not operational, return pending or error
        If any other are not operational or degraded, return degraded
        Otherwise return ok.
        (Instances with priority set to "ignore" are ignored.)
        """
        # error if a configuration is required and not operational
        # error if a non-configuration managed child resource is required and not operational
        # degraded if non-required configurations and resources are not operational
        #          or required configurations and resources are degraded
        # ok otherwise

        # any required is pending then aggregate is pending
        # otherwise just set to degraded
        aggregate = None
        for status in statuses:
            assert isinstance(status, Operational), status
            if aggregate is None:
                aggregate = Status.ok
            if status.priority == Priority.ignore:
                continue
            elif status.required and not status.operational:
                if status.status == Status.pending:
                    aggregate = Status.pending
                else:
                    aggregate = Status.error
                    break
            else:
                if aggregate <= Status.degraded:
                    if not status.operational or status.status == Status.degraded:
                        aggregate = Status.degraded
        return aggregate


class OperationalInstance(Operational):
    def __init__(
        self,
        status=None,
        priority=None,
        manualOveride=None,
        lastStateChange=None,
        lastConfigChange=None,
        state=None,
    ):
        if isinstance(status, OperationalInstance):
            self._localStatus = status._localStatus
            self._manualOverideStatus = status._manualOverideStatus
            self._priority = status._priority
            self._lastStateChange = status._lastStateChange
            self._lastConfigChange = status._lastConfigChange
            self._state = status._state
            self.dependencies = status.dependencies
        else:
            self._localStatus = toEnum(Status, status)
            self._manualOverideStatus = toEnum(Status, manualOveride)
            self._priority = toEnum(Priority, priority)
            self._lastStateChange = lastStateChange
            self._lastConfigChange = lastConfigChange
            self._state = state
            self.dependencies = []
        # self.repairable = False # XXX3
        # self.messages = [] # XXX3

    def getOperationalDependencies(self):
        return self.dependencies

    def localStatus():
        doc = "The localStatus property."

        def fget(self):
            return self._localStatus

        def fset(self, value):
            self._localStatus = value

        def fdel(self):
            del self._localStatus

        return locals()

    localStatus = property(**localStatus())

    def manualOverideStatus():
        doc = "The manualOverideStatus property."

        def fget(self):
            return self._manualOverideStatus

        def fset(self, value):
            self._manualOverideStatus = value

        def fdel(self):
            del self._manualOverideStatus

        return locals()

    manualOverideStatus = property(**manualOverideStatus())

    def priority():
        doc = "The priority property."

        def fget(self):
            return Defaults.shouldRun if self._priority is None else self._priority

        def fset(self, value):
            self._priority = value

        def fdel(self):
            del self._priority

        return locals()

    priority = property(**priority())

    @property
    def lastStateChange(self):
        return self._lastStateChange

    @property
    def lastConfigChange(self):
        return self._lastConfigChange

    def state():
        doc = "The state property."

        def fget(self):
            return self._state

        def fset(self, value):
            self._state = toEnum(NodeState, value)

        return locals()

    state = property(**state())


class _ChildResources(collections.Mapping):
    def __init__(self, resource):
        self.resource = resource

    def __getitem__(self, key):
        return self.resource.findResource(key)

    def __iter__(self):
        return self.resource.getSelfAndDescendents()

    def __len__(self):
        return len(tuple(self))


class NodeInstance(OperationalInstance, ResourceRef):
    attributeManager = None
    createdOn = None

    def __init__(
        self, name="", attributes=None, parent=None, template=None, status=None
    ):
        OperationalInstance.__init__(self, status)
        self.name = name
        self._attributes = attributes or {}
        self.parent = parent
        if parent:
            getattr(parent, self.parentRelation).append(self)

        self.template = template or self.templateType()

    def _resolve(self, key):
        # might return a Result
        self.attributes[key]  # force resolve
        return self.attributes._attributes[key]

    def localStatus():
        doc = "The localStatus property."

        def fget(self):
            return self._localStatus

        def fset(self, value):
            if self.root.attributeManager:
                self.root.attributeManager.setStatus(self, value)
            self._localStatus = value

        def fdel(self):
            del self._localStatus

        return locals()

    localStatus = property(**localStatus())

    @property
    def key(self):
        return "%s::.%s::%s" % (self.parent.key, self.parentRelation, self.name)

    def asRef(self, options=None):
        return {"ref": self.key}

    @property
    def attributes(self):
        """
    attributes should be live values but _attributes will be serialized value
    """
        if not self.root.attributeManager:
            if not self.attributeManager:
                # inefficient but create a local one for now
                self.attributeManager = AttributeManager()
            # XXX3 changes to self.attributes aren't saved
            return self.attributeManager.getAttributes(self)

        # returned registered attribute or create a new one
        # attribute class getter resolves references
        return self.root.attributeManager.getAttributes(self)


# both have occurrences
# only need to configure capabilities as required by a relationship
class Capability(NodeInstance):
    # 3.7.2 Capability definition p. 97
    # 3.8.1 Capability assignment p. 114
    parentRelation = "capabilities"
    templateType = CapabilitySpec


class Relationship(NodeInstance):
    # 3.7.3 Requirements definition p. 99
    # 3.8.2 Requirements assignment p. 115
    parentRelation = "requirements"
    templateType = RelationshipSpec
    target = None  # XXX

    # XXX add a target attribute
    def getOperationalDependencies(self):
        if self.target:
            yield self.target


class Resource(NodeInstance):
    """
      capabilities:
      name:
      attributes
      dependencies: #includes requirements
  """

    baseDir = ""
    templateType = NodeSpec
    parentRelation = "resources"
    # createdFrom = None

    # spec is a NodeTemplate
    def __init__(
        self, name="", attributes=None, parent=None, template=None, status=None
    ):
        if parent:
            if parent.root.findResource(name):
                raise UnfurlError(
                    'can not create resource name "%s" is already in use' % name
                )
        self.resources = []
        self.capabilities = []
        self.requirements = []
        NodeInstance.__init__(self, name, attributes, parent, template, status)

        if self.root is self:
            self._all = _ChildResources(self)
            self._templar = Templar(DataLoader())

        self._interfaces = {}  # XXX2 exclude when pickling
        # preload
        self.getInterface("inherit")
        self.getInterface("default")

    def getCapabilities(self, name):
        capabilities = [
            capability for capability in self.capabilities if capability.name == name
        ]
        if capabilities:
            return capabilities
        else:
            capabilityTemplate = self.template.getCapability(name)
            if capabilityTemplate:
                # will be added to self.capabilities
                return [Capability(name, parent=self, template=capabilityTemplate)]
        return []

    def _resolve(self, key):
        # might return a Result
        try:
            self.attributes[key]  # force resolve
            return self.attributes._attributes[key]
        except KeyError:
            try:
                inherit = self._interfaces.get("inherit")  # pre-loaded
                if inherit:
                    return inherit(key)
                else:
                    raise
            except KeyError:
                default = self._interfaces.get("default")  # pre-loaded
                if default:
                    return default(key)
                else:
                    raise

    @property
    def key(self):
        return "::%s" % self.name

    def getOperationalDependencies(self):
        # XXX
        # for instance in chain(self.capabilities, self.requirements):
        #  yield instance
        if self.parent and self.parent is not self.root:
            yield self.parent

    def getSelfAndDescendents(self):
        "Recursive descendent including self"
        yield self
        for r in self.resources:
            for descendent in r.getSelfAndDescendents():
                yield descendent

    @property
    def descendents(self):
        return list(self.getSelfAndDescendents())

    def findResource(self, resourceid):
        if self.name == resourceid:
            return self
        for r in self.resources:
            child = r.findResource(resourceid)
            if child:
                return child
        return None

    def setBaseDir(self, baseDir):
        self.baseDir = baseDir
        if not self._templar or self._templar._basedir != baseDir:
            loader = DataLoader()
            loader.set_basedir(baseDir)
            self._templar = Templar(loader)

    def addInterface(self, klass, name=None):
        if not isinstance(klass, six.string_types):
            klass = klass.__module__ + "." + klass.__name__
        current = self._attributes.setdefault(".interfaces", {})
        current[name or klass] = klass
        return current

    def getInterface(self, name):
        # XXX uses TOSCA interfaces instead
        if ".interfaces" not in self._attributes:
            return None  # no interfaces

        if not isinstance(name, six.string_types):
            name = name.__module__ + "." + name.__name__
        instance = self._interfaces.get(name)
        if instance:
            return instance
        else:
            className = self._attributes[".interfaces"].get(name)
            if className:
                instance = loadClass(className)(name, self)
                self._interfaces[name] = instance
                return instance
        return None

    def __eq__(self, other):
        if self is other:
            return True
        if not isinstance(other, Resource):
            return False
        if not self.lastChange:
            # only support equality if resource has a changeid
            return False
        return self.name == other.name and self.lastChange == other.lastChange

    def __repr__(self):
        return "Resource('%s')" % self.name

    def __getstate__(self):
        state = self.__dict__.copy()
        # Remove the unpicklable entries.
        if state.get("_templar"):
            del state["_templar"]
        return state


class TopologyResource(Resource):
    templateType = TopologySpec

    def __init__(self, template, status=None):
        Resource.__init__(self, "root", template=template, status=status)
        # add these as special child resources so they can be accessed like "::inputs::foo"
        self.inputs = Resource("inputs", template.inputs, self)
        self.outputs = Resource("outputs", template.outputs, self)

    def getOperationalDependencies(self):
        for instance in self.resources:
            if instance.name not in ["inputs", "outputs"]:
                yield instance
