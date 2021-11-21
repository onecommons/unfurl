# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
This module defines the core model and implements the runtime operations of the model.

The state of the system is represented as a collection of Instances.
Each instance have a status; attributes that describe its state; and a TOSCA template
which describes its capabilities, relationships and available interfaces for configuring and interacting with it.
"""
import six
from collections.abc import Mapping

from ansible.parsing.dataloader import DataLoader

from .util import UnfurlError, load_class, to_enum, make_temp_dir, ChainMap
from .result import ResourceRef, ChangeAware

from .support import AttributeManager, Defaults, Status, Priority, NodeState, Templar
from .tosca import (
    CapabilitySpec,
    RelationshipSpec,
    NodeSpec,
    TopologySpec,
    ArtifactSpec,
)

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
    def local_status(self):
        return Status.unknown

    @property
    def state(self):
        return NodeState.initial

    def get_operational_dependencies(self):
        """
        Return an iterator of `Operational` object that this instance depends on to be operational.
        """
        return ()

    def get_operational_dependents(self):
        return ()

    @property
    def manual_overide_status(self):
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
        then the aggregate status' of its dependencies (see `aggregate_status()`
        and `get_operational_dependencies()`).

        If the local_status is non-operational, that takes precedence.
        Otherwise the local_status is compared with its aggregate dependent status
        and worser value is choosen.
        """
        if self.manual_overide_status is not None:
            status = self.manual_overide_status
            if status >= Status.error:
                return status
        else:
            status = self.local_status

        if not status:
            return Status.unknown

        if status >= Status.error:
            # return error, pending, or absent
            return status

        dependentStatus = self.aggregate_status(self.get_operational_dependencies())
        if dependentStatus is None:
            # note if local_status is a no op (status purely determined by dependents)
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
    def last_state_change(self):
        return None

    @property
    def last_config_change(self):
        return None

    @property
    def last_change(self):
        if not self.last_state_change:
            return self.last_config_change
        elif not self.last_config_change:
            return self.last_state_change
        else:
            return max(self.last_state_change, self.last_config_change)

    def has_changed(self, changeset):
        # if changed since the last time we checked
        if not self.last_change:
            return False
        if not changeset:
            return True
        return self.last_change > changeset.changeId

    @staticmethod
    def aggregate_status(statuses):
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
            self._localStatus = to_enum(Status, status)
            self._manualOverideStatus = to_enum(Status, manualOveride)
            self._priority = to_enum(Priority, priority)
            self._lastStateChange = lastStateChange
            self._lastConfigChange = lastConfigChange
            self._state = state
            self.dependencies = []
        # self.repairable = False # XXX3
        # self.messages = [] # XXX3

    def get_operational_dependencies(self):
        return self.dependencies

    def local_status():
        doc = "The local_status property."

        def fget(self):
            return self._localStatus

        def fset(self, value):
            self._localStatus = value

        def fdel(self):
            del self._localStatus

        return locals()

    local_status = property(**local_status())

    def manual_overide_status():
        doc = "The manualOverideStatus property."

        def fget(self):
            return self._manualOverideStatus

        def fset(self, value):
            self._manualOverideStatus = value

        def fdel(self):
            del self._manualOverideStatus

        return locals()

    manual_overide_status = property(**manual_overide_status())

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
    def last_state_change(self):
        return self._lastStateChange

    @property
    def last_config_change(self):
        return self._lastConfigChange

    def state():
        doc = "The state property."

        def fget(self):
            return self._state

        def fset(self, value):
            self._state = to_enum(NodeState, value)

        return locals()

    state = property(**state())


class _ChildResources(Mapping):
    def __init__(self, resource):
        self.resource = resource

    def __getitem__(self, key):
        return self.resource.find_resource(key)

    def __iter__(self):
        return iter(r.name for r in self.resource.get_self_and_descendents())

    def __len__(self):
        return len(tuple(self.resource.get_self_and_descendents()))


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
            p = getattr(parent, self.parentRelation)
            p.append(self)

        self.template = template or self.templateType()

    def _resolve(self, key):
        # might return a Result
        self.attributes[key]  # force resolve
        return self.attributes._attributes[key]

    def query(self, expr, vars=None, wantList=False):
        from .eval import Ref, RefContext

        return Ref(expr).resolve(RefContext(self, vars=vars), wantList)

    def local_status():
        doc = "The working_dir property."

        def fget(self):
            return self._localStatus

        def fset(self, value):
            if self.root.attributeManager:
                self.root.attributeManager.set_status(self, value)
            self._localStatus = value

        def fdel(self):
            del self._localStatus

        return locals()

    local_status = property(**local_status())

    def get_operational_dependencies(self):
        if self.parent and self.parent is not self.root:
            yield self.parent

        for d in self.dependencies:
            yield d

    @property
    def key(self):
        return f"{self.parent.key}::.{self.parentRelation[1:]}::{self.name}"

    def as_ref(self, options=None):
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
    def base_dir(self):
        if self.shadow:
            return self.shadow.base_dir
        else:
            return self.root._baseDir

    @property
    def artifacts(self):
        return {}

    @property
    def attributes(self):
        """attributes are live values but _attributes will be the serialized value"""
        if not self.root.attributeManager:
            if not self.attributeManager:
                # inefficient but create a local one for now
                self.attributeManager = AttributeManager()
            # XXX3 changes to self.attributes aren't saved
            return self.attributeManager.get_attributes(self)

        # returned registered attribute or create a new one
        # attribute class getter resolves references
        return self.root.attributeManager.get_attributes(self)

    @property
    def names(self):
        return self.attributes

    def get_default_relationships(self, relation=None):
        return self.root.get_default_relationships(relation)

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
        if not self.last_change:
            # only support equality if resource has a changeid
            return False
        return self.last_change == other.last_change and self.key == other.key

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
        return f"{self.__class__}('{self.name}')"


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
        if self._relationships is None:
            self._relationships = []
        if len(self._relationships) != len(self.template.relationships):
            # instantiate missing relationships (they are only added, never deleted)
            instantiated = {id(c.template) for c in self._relationships}
            for template in self.template.relationships:
                if id(template) not in instantiated:
                    rel = RelationshipInstance(
                        template.name, parent=self, template=template
                    )
                    assert rel in self._relationships
                    # find the node instance that uses this requirement
                    sourceNode = self.root.find_resource(template.source.name)
                    if sourceNode:
                        rel.source = sourceNode

        return self._relationships

    @property
    def key(self):
        # XXX implement something like _ChildResources to enable ::name instead of [.name]
        return f"{self.parent.key}::.{'capabilities'}::[.name={self.name}]"


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
            return f"{self.source.key}::.requirements::[.name={self.name}]"
        elif self.parent is self.root:  # it's a default relationship
            return f"::.requirements::[.name={self.name}]"
        else:  # capability is parent
            return f"{self.parent.key}::.relationships::[.name={self.name}]"

    def merge_props(self, matchfn, delete_if_none=False):
        env = {}
        capability = self.parent
        for name, val in capability.template.find_props(capability.attributes, matchfn):
            if val is None:
                if delete_if_none:
                    env.pop(name, None)
            else:
                env[name] = val
        for name, val in self.template.find_props(self.attributes, matchfn):
            if val is None:
                if delete_if_none:
                    env.pop(name, None)
            else:
                env[name] = val
        return env


class ArtifactInstance(EntityInstance):
    parentRelation = "_artifacts"
    templateType = ArtifactSpec

    def __init__(
        self, name="", attributes=None, parent=None, template=None, status=None
    ):
        EntityInstance.__init__(self, name, attributes, parent, template, status)

    @property
    def base_dir(self):
        return self.template.base_dir

    @property
    def file(self):
        return self.template.file

    @property
    def repository(self):
        return self.template.repository

    def get_path(self, resolver=None):
        return self.template.get_path_and_fragment(resolver)

    def get_path_and_fragment(self, resolver=None, tpl=None):
        return self.template.get_path_and_fragment(resolver, tpl)

    def as_import_spec(self):
        return self.template.as_import_spec()

    def get_operational_dependencies(self):
        # skip dependency on the parent
        for d in self.dependencies:
            yield d


class NodeInstance(EntityInstance):
    templateType = NodeSpec
    parentRelation = "instances"

    def __init__(
        self, name="", attributes=None, parent=None, template=None, status=None
    ):
        if parent:
            # only node instances have unique names
            if parent.root.find_resource(name):
                raise UnfurlError(
                    f'can not create node instance "{name}", its name is already in use'
                )

        # next three may be initialized either by Manifest.createNodeInstance or by its property
        self._capabilities = []
        self._requirements = []
        self._artifacts = []
        self._named_artifacts = None
        self.instances = []
        EntityInstance.__init__(self, name, attributes, parent, template, status)

        if self.root is self:
            self._all = _ChildResources(self)
            self._templar = Templar(DataLoader())

        self._interfaces = {}
        # preload
        self.get_interface("inherit")
        self.get_interface("default")

    def _find_relationship(self, relationship):
        """
        Find RelationshipInstance that has the give relationship template
        """
        assert relationship and relationship.capability and relationship.target
        # find the Capability instance that corresponds to this relationship template's capability
        targetNodeInstance = self.root.find_resource(relationship.target.name)
        assert (
            targetNodeInstance
        ), f"target instance {relationship.target.name} should have been already created"
        for cap in targetNodeInstance.capabilities:
            if cap.template is relationship.capability:
                for relInstance in cap.relationships:
                    if relInstance.template is relationship:
                        return relInstance
        return None

    @property
    def requirements(self):
        # if self._requirements is None:
        if len(self._requirements) != len(self.template.requirements):
            # instantiate missing relationships (they are only added, never deleted)
            instantiated = {id(r.template) for r in self._requirements}
            for name, template in self.template.requirements.items():
                assert template.relationship
                if id(template.relationship) not in instantiated:
                    relInstance = self._find_relationship(template.relationship)
                    if not relInstance:
                        raise UnfurlError(
                            f'can not find relation instance for requirement "{name}" on node "{self.name}"'
                        )
                    assert template.relationship is relInstance.template
                    assert self.template is relInstance.template.source
                    self._requirements.append(relInstance)

        return self._requirements

    def get_requirements(self, match):
        if match is None:
            return self.requirements
        if isinstance(match, six.string_types):
            return [r for r in self.requirements if r.template.name == match]
        elif isinstance(match, NodeInstance):
            return [r for r in self.requirements if r.target == match]
        elif isinstance(match, CapabilityInstance):
            return [r for r in self.requirements if r.parent == match]
        elif isinstance(match, ArtifactInstance):
            return []
        else:
            raise UnfurlError(f'invalid match for get_requirements: "{match}"')

    @property
    def capabilities(self):
        if len(self._capabilities) != len(self.template.capabilities):
            # instantiate missing capabilities (they are only added, never deleted)
            instantiated = {id(c.template) for c in self._capabilities}
            for name, template in self.template.capabilities.items():
                if id(template) not in instantiated:
                    # if capabilities:
                    #    # append index to name if multiple Capabilities exist for that name
                    #    name += str(len(capabilities))
                    # the new instance will be added to _capabilities
                    cap = CapabilityInstance(
                        template.name, parent=self, template=template
                    )
                    assert cap in self._capabilities

        return self._capabilities

    def get_capabilities(self, name):
        return [
            capability
            for capability in self.capabilities
            if capability.template.name == name
        ]

    @property
    def artifacts(self) -> dict:
        # only include named artifacts
        if self._named_artifacts is None:
            self._named_artifacts = {}
            instantiated = {a.name: a for a in self._artifacts}
            for name, template in self.template.artifacts.items():
                artifact = instantiated.get(name)
                if not artifact:
                    artifact = ArtifactInstance(
                        template.name, parent=self, template=template
                    )
                    assert artifact in self._artifacts

                self._named_artifacts[template.name] = artifact
        return self._named_artifacts

    def _get_default_relationships(self, relation=None):
        if self.root is self:
            return
        for rel in self.root.get_default_relationships(relation):
            for capability in self.capabilities:
                if rel.template.matches_target(capability.template):
                    yield rel

    def get_default_relationships(self, relation=None):
        return list(self._get_default_relationships(relation))

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
        return f"::{self.name}"

    def get_operational_dependencies(self):
        for dep in super().get_operational_dependencies():
            yield dep

        for instance in self.requirements:
            if instance is not self.parent:
                yield instance

    def get_operational_dependents(self):
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

    def get_self_and_descendents(self):
        "Recursive descendent including self"
        yield self
        for r in self.instances:
            for descendent in r.get_self_and_descendents():
                yield descendent

    @property
    def descendents(self):
        return list(self.get_self_and_descendents())

    # XXX use find_instance instead and remove find_resource
    def find_resource(self, resourceid):
        if self.name == resourceid:
            return self
        for r in self.instances:
            child = r.find_resource(resourceid)
            if child:
                return child
        return None

    find_instance = find_resource

    def find_instance_or_external(self, resourceid):
        instance = self.find_resource(resourceid)
        if instance:
            return instance
        if self.imports:
            return self.imports.find_import(resourceid)
        return None

    def add_interface(self, klass, name=None):
        if not isinstance(klass, six.string_types):
            klass = klass.__module__ + "." + klass.__name__
        current = self._attributes.setdefault(".interfaces", {})
        current[name or klass] = klass
        return current

    def get_interface(self, name):
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
                instance = load_class(className)(name, self)
                self._interfaces[name] = instance
                return instance
        return None

    def __repr__(self):
        return f"NodeInstance('{self.name}')"


class TopologyInstance(NodeInstance):
    templateType = TopologySpec

    def __init__(self, template, status=None):
        attributes = dict(inputs=template.inputs, outputs=template.outputs)
        NodeInstance.__init__(
            self, "root", attributes, template=template, status=status
        )

        self._relationships = None
        self._tmpDir = None

    def set_base_dir(self, baseDir):
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

    def get_default_relationships(self, relation=None):
        # for root, this is the same as self.requirements
        if not relation:
            return self.requirements
        return [
            rel
            for rel in self.requirements
            if rel.template.is_compatible_type(relation)
        ]

    def get_operational_dependencies(self):
        for instance in self.instances:
            yield instance

    @property
    def tmp_dir(self):
        if not self._tmpDir:
            self._tmpDir = make_temp_dir()
        return self._tmpDir
