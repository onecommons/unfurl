# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
This module defines the core model and implements the runtime operations of the model.

The state of the system is represented as a collection of Instances.
Each instance have a status; attributes that describe its state; and a TOSCA template
which describes its capabilities, relationships and available interfaces for configuring and interacting with it.
"""
import six
import collections

from ansible.parsing.dataloader import DataLoader

from .util import UnfurlError, loadClass, toEnum, makeTempDir, ChainMap
from .result import ResourceRef, ChangeAware

from .support import AttributeManager, Defaults, Status, Priority, NodeState, Templar
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

    def getOperationalDependents(self):
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
    def present(self):
        return self.operational or self.status == Status.error

    @property
    def missing(self):
        return self.status == Status.pending or self.status == Status.absent

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
            # return error, pending, or absent
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
    """
    A concrete implementation of Operational
    """

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
        return iter(r.name for r in self.resource.getSelfAndDescendents())

    def __len__(self):
        return len(tuple(self.resource.getSelfAndDescendents()))


class EntityInstance(OperationalInstance, ResourceRef):
    attributeManager = None
    created = None
    shadow = None
    imports = None
    envRules = None
    _baseDir = ""

    def __init__(
        self, name="", attributes=None, parent=None, template=None, status=Status.ok
    ):
        # default to Status.ok because that has the semantics of only relying on dependents
        # note: NodeInstances always a explicit status and so instead default to unknown
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

    def query(self, expr, vars=None, wantList=False):
        from .eval import Ref, RefContext

        return Ref(expr).resolve(RefContext(self, vars=vars), wantList)

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

    def getOperationalDependencies(self):
        if self.parent and self.parent is not self.root:
            yield self.parent

    @property
    def key(self):
        return "%s::.%s::%s" % (self.parent.key, self.parentRelation[1:], self.name)

    def asRef(self, options=None):
        return {"ref": self.key}

    @property
    def tosca_id(self):
        return self.key

    @property
    def tosca_name(self):
        return self.template.name

    @property
    def type(self):
        return self.template.type

    @property
    def baseDir(self):
        if self.shadow:
            return self.shadow.baseDir
        else:
            return self.root._baseDir

    @property
    def artifacts(self):
        return self.template.artifacts

    @property
    def attributes(self):
        """attributes are live values but _attributes will be the serialized value"""
        if not self.root.attributeManager:
            if not self.attributeManager:
                # inefficient but create a local one for now
                self.attributeManager = AttributeManager()
            # XXX3 changes to self.attributes aren't saved
            return self.attributeManager.getAttributes(self)

        # returned registered attribute or create a new one
        # attribute class getter resolves references
        return self.root.attributeManager.getAttributes(self)

    @property
    def names(self):
        return self.attributes

    def __eq__(self, other):
        if self is other:
            return True
        if self.__class__ != other.__class__:
            return False
        if self.root is not other.root:
            if self.shadow and self.shadow == other:
                return True
            if other.shadow and other.shadow == self:
                return True
            else:
                return False
        if not self.lastChange:
            # only support equality if resource has a changeid
            return False
        return self.lastChange == other.lastChange and self.key == other.key

    def __getstate__(self):
        state = self.__dict__.copy()
        # Remove the unpicklable entries.
        if state.get("_templar"):
            del state["_templar"]
        if state.get("_interfaces"):
            state["_interfaces"] = {}
        if "attributeManager" in state:
            del state["attributeManager"]
        return state

    def __repr__(self):
        return "%s('%s')" % (self.__class__, self.name)


# both have occurrences
# only need to configure capabilities as required by a relationship
class CapabilityInstance(EntityInstance):
    # 3.7.2 Capability definition p. 97
    # 3.8.1 Capability assignment p. 114
    parentRelation = "_capabilities"
    templateType = CapabilitySpec
    _relationships = None

    @property
    def relationships(self):
        # create relationship instances from the corresponding relationship templates
        if self._relationships is None:
            self._relationships = []
            for template in self.template.relationships:
                # template will be a RelationshipSpec
                assert template.source
                # the constructor will add itself to _relationships
                rel = RelationshipInstance(
                    template.name, parent=self, template=template
                )
                # find the node instance that uses this requirement
                sourceNode = self.root.findResource(template.source.name)
                if sourceNode:
                    rel.source = sourceNode

        return self._relationships

    @property
    def key(self):
        # XXX implement something like _ChildResources to enable ::name instead of [.name]
        return "%s::.%s::[.name=%s]" % (self.parent.key, "capabilities", self.name)


class RelationshipInstance(EntityInstance):
    # 3.7.3 Requirements definition p. 99
    # 3.8.2 Requirements assignment p. 115
    parentRelation = "_relationships"
    templateType = RelationshipSpec
    source = None

    @property
    def target(self):
        # parent is a capability, return it's parent (a Node)
        return self.parent.parent if self.parent else None

    @property
    def key(self):
        # XXX implement something like _ChildResources to enable ::name instead of [.name=name]
        if self.source:
            return "%s::.requirements::[.name=%s]" % (self.source.key, self.name)
        elif self.parent is self.root:  # it's a default relationship
            return "::.requirements::[.name=%s]" % self.name
        else:  # capability is parent
            return "%s::.relationships::[.name=%s]" % (self.parent.key, self.name)

    def mergeProps(self, matchfn):
        env = {}
        capability = self.parent
        for name, val in capability.template.findProps(capability.attributes, matchfn):
            if val is not None:
                env[name] = val
        for name, val in self.template.findProps(self.attributes, matchfn):
            if val is not None:
                env[name] = val
        return env


class NodeInstance(EntityInstance):
    templateType = NodeSpec
    parentRelation = "instances"

    def __init__(
        self, name="", attributes=None, parent=None, template=None, status=None
    ):
        if parent:
            # only node instances have unique names
            if parent.root.findResource(name):
                raise UnfurlError(
                    'can not create node instance "%s", its name is already in use'
                    % name
                )

        # next two maybe initialized either by Manifest.createNodeInstance or by its property
        self._capabilities = None
        self._requirements = None
        self.instances = []
        EntityInstance.__init__(self, name, attributes, parent, template, status)

        if self.root is self:
            self._all = _ChildResources(self)
            self._templar = Templar(DataLoader())

        self._interfaces = {}
        # preload
        self.getInterface("inherit")
        self.getInterface("default")

    @property
    def requirements(self):
        if self._requirements is None:

            def findRelationship(name, template):
                assert template.relationship  # template is a RequirementSpec
                relationship = template.relationship
                assert relationship and relationship.capability and relationship.target
                # find the Capability instance that corresponds to this relationship template's capability
                targetNodeInstance = self.root.findResource(relationship.target.name)
                assert targetNodeInstance, (
                    "target instance %s should have been already created"
                    % relationship.target.name
                )
                # calling getCapabilities() will create any needed capability instances
                capabilities = targetNodeInstance.getCapabilities(
                    relationship.capability.name
                )
                if capabilities:  # they should have already been created
                    # invoking the relationships property will create any needed relationship instances
                    for relInstance in capabilities[0].relationships:
                        if relInstance.source:
                            if relInstance.source is self:
                                return relInstance
                        else:
                            if relInstance.template.source == self.template:
                                # this instance didn't exist when relInstance was created, assign source now
                                relInstance.source = self
                                return relInstance

                raise UnfurlError(
                    'can not find relation instance for requirement "%s" on node "%s"'
                    % (name, self.name)
                )

            self._requirements = [
                findRelationship(k, v) for (k, v) in self.template.requirements.items()
            ]

        return self._requirements

    def getRequirements(self, match):
        if isinstance(match, six.string_types):
            return [r for r in self.requirements if r.template.name == match]
        elif isinstance(match, NodeInstance):
            return [r for r in self.requirements if r.target == match]
        elif isinstance(match, CapabilityInstance):
            return [r for r in self.requirements if r.parent == match]
        else:
            raise UnfurlError('invalid match for getRequirements: "%s"' % match)

    @property
    def capabilities(self):
        if self._capabilities is None:
            self._capabilities = []
            for name, template in self.template.capabilities.items():
                # if capabilities:
                #    # append index to name if multiple Capabilities exist for that name
                #    name += str(len(capabilities))
                # the new instance will be added to _capabilities
                CapabilityInstance(template.name, parent=self, template=template)

        return self._capabilities

    def getCapabilities(self, name):
        return [
            capability
            for capability in self.capabilities
            if capability.template.name == name
        ]

    def _getDefaultRelationships(self, relation=None):
        if self.root is self:
            return
        for rel in self.root.getDefaultRelationships(relation):
            for capability in self.capabilities:
                if rel.template.matches_target(capability.template):
                    yield rel

    def getDefaultRelationships(self, relation=None):
        return list(self._getDefaultRelationships(relation))

    @property
    def names(self):
        # matches TOSCA logic for get_attribute lookup
        return ChainMap(
            self.attributes,
            {r.name: r.parent for r in self.requirements},
            {c.name: c for c in self.capabilities},
        )

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
        parent = None
        if self.parent and self.parent is not self.root:
            parent = self.parent
            yield parent

        for instance in self.requirements:
            if instance is not parent:
                yield instance

    def getOperationalDependents(self):
        seen = set()
        for cap in self.capabilities:
            for rel in cap.relationships:
                dep = rel.source
                if dep and id(dep) not in seen:
                    seen.add(id(dep))
                    yield dep

        for instance in self.instances:
            if id(instance) not in seen:
                seen.add(id(instance))
                yield instance

    def getSelfAndDescendents(self):
        "Recursive descendent including self"
        yield self
        for r in self.instances:
            for descendent in r.getSelfAndDescendents():
                yield descendent

    @property
    def descendents(self):
        return list(self.getSelfAndDescendents())

    def findResource(self, resourceid):
        if self.name == resourceid:
            return self
        for r in self.instances:
            child = r.findResource(resourceid)
            if child:
                return child
        return None

    def findInstanceOrExternal(self, resourceid):
        instance = self.findResource(resourceid)
        if instance:
            return instance
        if self.imports:
            return self.imports.findImport(resourceid)
        return None

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

    def __repr__(self):
        return "NodeInstance('%s')" % self.name


class TopologyInstance(NodeInstance):
    templateType = TopologySpec

    def __init__(self, template, status=None):
        NodeInstance.__init__(self, "root", template=template, status=status)
        # add these as special child resources so they can be accessed like "::inputs::foo"
        self.inputs = NodeInstance("inputs", template.inputs, self)
        self.outputs = NodeInstance("outputs", template.outputs, self)
        self._capabilities = []
        self._relationships = None
        self._tmpDir = None

    def setBaseDir(self, baseDir):
        self._baseDir = baseDir
        if not self._templar or self._templar._basedir != baseDir:
            loader = DataLoader()
            loader.set_basedir(baseDir)
            self._templar = Templar(loader)

    @property
    def requirements(self):
        """
        The root node returns RelationshipInstances representing default relationship templates
        """
        if self._relationships is None:
            self._relationships = []
            relTemplates = self.template.spec.relationshipTemplates
            for name, template in relTemplates.items():
                # template will be a RelationshipSpec
                if template.toscaEntityTemplate.default_for:
                    # the constructor will add itself to _relationships
                    RelationshipInstance(name, parent=self, template=template)

        return self._relationships

    def getDefaultRelationships(self, relation=None):
        # for root, this is the same as self.requirements
        if not relation:
            return self.requirements
        return [
            rel for rel in self.requirements if rel.template.isCompatibleType(relation)
        ]

    def getOperationalDependencies(self):
        for instance in self.instances:
            if instance.name not in ["inputs", "outputs"]:
                yield instance

    @property
    def tmpDir(self):
        if not self._tmpDir:
            self._tmpDir = makeTempDir()
        return self._tmpDir
