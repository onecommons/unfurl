# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
This module defines the core model and implements the runtime operations of the model.

The state of the system is represented as a collection of Instances.
Each instance have a status; attributes that describe its state; and a TOSCA template
which describes its capabilities, relationships and available interfaces for configuring and interacting with it.
"""
from datetime import datetime
import os
from typing import (
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Union,
    TYPE_CHECKING,
    cast,
    NewType,
    AnyStr,
)
from collections.abc import Mapping

from ansible.parsing.dataloader import DataLoader

from .util import UnfurlError, load_class, to_enum, make_temp_dir, ChainMap
from .result import ResourceRef, ChangeAware, ResultsMap

from .support import (
    AttributeManager,
    Defaults,
    Imports,
    Status,
    Priority,
    NodeState,
    Templar,
)
from .tosca import (
    EntitySpec,
    CapabilitySpec,
    RelationshipSpec,
    NodeSpec,
    TopologySpec,
    ArtifactSpec,
)
from .logs import getLogger
from toscaparser.nodetemplate import NodeTemplate

if TYPE_CHECKING:
    from unfurl.configurator import Dependency
    import toscaparser.repositories
    from .yamlmanifest import YamlManifest


logger = getLogger("unfurl")


InstanceKey = NewType("InstanceKey", str)


class Operational(ChangeAware):
    """
    This is an abstract base class for Jobs, Resources, and Configurations all have a Status associated with them
    and all use the same algorithm to compute their status from their dependent resouces, tasks, and configurations
    """

    # XXX3 add repairable, messages?
    name = ""

    # core properties to override
    @property
    def priority(self) -> Priority:
        return Defaults.shouldRun

    @property
    def local_status(self) -> Optional[Status]:
        return Status.unknown

    @property
    def state(self) -> NodeState:
        return NodeState.initial

    def has_state(self, state: NodeState) -> bool:
        if state == NodeState.initial:
            return True
        if self.state is None:
            # self.state is sometimes None even though it shouldn't be
            return False
        if state == NodeState.error or self.state == NodeState.error:
            return self.state == state
        if state < NodeState.stopping:
            if self.state < NodeState.stopping:
                # progressive states on both sides
                return self.state >= state
            elif self.state in [NodeState.stopping, NodeState.stopped]:
                if state < NodeState.starting:
                    # created, configured, etc. still true even if node is stopped
                    return True
            else:
                assert (
                    self.state
                    in [NodeState.deleting, NodeState.deleted]
                    and state < NodeState.stopping
                ), (self.state.name, state.name)
                return False
        if self.state > NodeState.stopping:
            assert state >= NodeState.stopping
            return self.state >= state
        return False

    def get_operational_dependencies(self) -> Iterable["Operational"]:
        """
        Return an iterator of `Operational` object that this instance depends on to be operational.
        """
        return ()  # type: ignore  # mypy doesn't like empty tuples

    def get_operational_dependents(self) -> List["Operational"]:  # type: ignore
        return ()  # type: ignore  # mypy doesn't like empty tuples

    @property
    def manual_override_status(self) -> None:
        return None

    # derived properties:
    @property
    def operational(self) -> bool:
        return self.status == Status.ok or self.status == Status.degraded

    @property
    def active(self) -> bool:
        # if task.target.has_state(NodeState.created):
        # unknown, ok, degraded
        if self.state and self.state > NodeState.initial:
            return self.has_state(NodeState.created)
        return self.status <= Status.error

    @property
    def present(self) -> bool:
        return self.operational or self.status == Status.error

    @property
    def missing(self) -> bool:
        return self.status == Status.pending or self.status == Status.absent

    def _status(self, seen) -> Status:
        """
        Return the effective status, considering first the local readyState and
        then the aggregate status' of its dependencies (see `aggregate_status()`
        and `get_operational_dependencies()`).

        If the local_status is non-operational, that takes precedence.
        Otherwise the local_status is compared with its aggregate dependent status
        and worser value is choosen.
        """
        if self.manual_override_status is not None:
            status = self.manual_override_status
            if status >= Status.error:
                return status
        else:
            status = self.local_status

        if not status:
            return Status.unknown

        if status == Status.error or status == Status.absent:
            # return error, or absent
            return status

        dependentStatus = self.aggregate_status(
            self.get_operational_dependencies(), seen
        )

        if self.is_computed():
            # if local_status is a no op (status purely determined by dependents) set to ok
            status = Status.ok

        if dependentStatus is None:
            # ok + None = ok, which makes sense, since it has no dependendents
            return status  # return local status
        else:
            # local status is ok, degraded
            return max(status, dependentStatus)

    @property
    def status(self) -> Status:
        return self._status({id(self): self})

    @property
    def required(self) -> bool:
        return self.priority == Priority.required

    # `lastConfigChange` indicated the last configuration change applied to the instance.
    # `lastStateChange` is last observed change to the internal state of the instance. This may be reflected in attributes of the instance or completely opaque to the service template's model.

    # The computed attribute `lastChange` is whichever of the above two attributes are more recent.

    @property
    def last_state_change(self) -> Optional[str]:
        return None

    @property
    def last_config_change(self) -> Optional[str]:
        return None

    @property
    def last_change(self) -> Optional[str]:
        if not self.last_state_change:
            return self.last_config_change
        elif not self.last_config_change:
            return self.last_state_change
        else:
            return max(self.last_state_change, self.last_config_change)

    def has_changed(self, changeset) -> bool:
        # if changed since the last time we checked
        if not self.last_change:
            return False
        if not changeset:
            return True
        return self.last_change > changeset.changeId

    def is_computed(self) -> bool:
        return False

    @staticmethod
    def aggregate_status(
        statuses: Iterable["Operational"], seen: Dict[int, "Operational"]
    ) -> Optional[Status]:
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
            if id(status) in seen:
                logger.debug(
                    f"Circular operational dependency when checking status {status} in {seen}"
                )
                continue
            seen[id(status)] = status
            _status = status._status(seen)
            operational = _status == Status.ok or _status == Status.degraded
            if status.required and not operational:
                if _status == Status.pending:
                    aggregate = Status.pending
                else:
                    aggregate = Status.error
                    break
            else:
                if aggregate <= Status.degraded:
                    if not operational or _status == Status.degraded:
                        aggregate = Status.degraded
        return aggregate


class OperationalInstance(Operational):
    """
    A concrete implementation of Operational
    """

    def __init__(
        self,
        status: Optional[Union["OperationalInstance", int, str]] = None,
        priority: Optional[Union[int, str, Priority]] = None,
        manualOveride: Optional[Union["OperationalInstance", int, str]] = None,
        lastStateChange: Optional[str] = None,
        lastConfigChange: Optional[str] = None,
        state: NodeState = None,
    ) -> None:
        if isinstance(status, OperationalInstance):
            self._localStatus: Optional[Status] = status._localStatus
            self._manualOverideStatus: Optional[Status] = status._manualOverideStatus
            self._priority: Optional[Priority] = status._priority
            self._lastStateChange: Optional[str] = status._lastStateChange
            self._lastConfigChange: Optional[str] = status._lastConfigChange
            self._state: Optional[NodeState] = status._state
            self.dependencies: List["Operational"] = status.dependencies
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

    def get_operational_dependencies(self) -> Iterable["Operational"]:
        return self.dependencies

    def __local_status():  # type: ignore
        doc = "The local_status property."

        def fget(self: "OperationalInstance") -> Optional[Status]:
            return self._localStatus

        def fset(self: "OperationalInstance", value: Status) -> None:
            self._localStatus = value

        def fdel(self: "OperationalInstance") -> None:
            del self._localStatus

        return locals()

    local_status: Optional[Status] = property(**__local_status())  # type: ignore

    def __manual_override_status() -> Dict[str, Any]:  # type: ignore
        doc = "The manualOverideStatus property."

        def fget(self: "OperationalInstance") -> Optional[Status]:
            return self._manualOverideStatus

        def fset(self: "OperationalInstance", value: Status) -> None:
            self._manualOverideStatus = value

        return locals()

    manual_override_status: Optional[Status] = property(**__manual_override_status())  # type: ignore

    def __priority():  # type: ignore
        doc = "The priority property."

        def fget(self: "OperationalInstance") -> Optional[Priority]:
            return self._priority

        def fset(self: "OperationalInstance", value):  # type: ignore
            self._priority = to_enum(Priority, value)

        return locals()

    priority: Optional[Priority] = property(**__priority())  # type: ignore

    @property
    def last_state_change(self) -> Optional[str]:
        return self._lastStateChange

    @property
    def last_config_change(self) -> Optional[str]:
        return self._lastConfigChange

    def __state():  # type: ignore
        doc = "The state property."

        def fget(self: "OperationalInstance") -> Optional[NodeState]:
            return self._state

        def fset(self: "OperationalInstance", value):  # type: ignore
            self._state = to_enum(NodeState, value)

        return locals()

    state: Optional[NodeState] = property(**__state())  # type: ignore


class _ChildResources(Mapping):
    def __init__(self, resource):
        self.resource = resource

    def __getitem__(self, key):
        return self.resource.find_resource(key)

    def __iter__(self):
        return iter(r.name for r in self.resource.get_self_and_descendants())

    def __len__(self):
        return len(tuple(self.resource.get_self_and_descendants()))


class EntityInstance(OperationalInstance, ResourceRef):
    attributeManager: Optional[AttributeManager] = None
    created: Optional[Union[bool, str]] = None
    protected: Optional[bool] = None
    imports: Optional[Imports] = None
    imported: Optional[str] = None
    _baseDir = ""
    templateType = EntitySpec  # must defined by subtype
    parentRelation = ""

    def __init__(
        self, name="", attributes=None, parent=None, template=None, status=Status.ok
    ):
        # default to Status.ok because that has the semantics of only relying on dependents
        # note: NodeInstances always a explicit status and so instead default to unknown
        OperationalInstance.__init__(self, status)
        self.name = name
        self._attributes = attributes or {}
        self.parent = parent
        if parent and self.parentRelation:
            p = getattr(parent, self.parentRelation)
            p.append(self)

        self.template: EntitySpec = template or self.templateType()  # type: ignore
        assert isinstance(self.template, self.templateType)
        self._properties = {}

    @property
    def repository(self) -> Optional["toscaparser.repositories.Repository"]:
        return None

    def _get_prop(self, name):
        # hack because `.repository` isn't a ResourceRef and so can't be evaluated in expressions
        if name == ".repository":
            if self.repository:
                return self.repository.tpl
            else:
                return None
        else:
            return super()._get_prop(name)

    def _resolve(self, key):
        # force resolve, might return a Result
        return self.attributes._getresult(key)

    def query(self, expr, vars=None, wantList=False, trace=None):
        from .eval import Ref, RefContext

        return Ref(expr).resolve(RefContext(self, vars=vars, trace=trace), wantList)

    def __priority():  # type: ignore
        doc = "The priority property."

        def fget(self) -> Optional[Priority]:
            if self._priority is None and self.template:
                return Priority.required if self.template.required else Priority.ignore
            else:
                return self._priority

        def fset(self, value):  # type: ignore
            self._priority = to_enum(Priority, value)

        return locals()

    priority: Optional[Priority] = property(**__priority())  # type: ignore

    def __local_status():  # type: ignore
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

    local_status: Optional[Status] = property(**__local_status())  # type: ignore

    def get_operational_dependencies(self):
        if self.parent and self.parent is not self.root:
            yield self.parent

        for d in self.dependencies:
            yield d

    def is_computed(self):
        return self.template.aggregate_only()

    @property
    def key(self) -> InstanceKey:
        assert self.parent and self.parentRelation
        return cast(
            InstanceKey, f"{self.parent.key}::.{self.parentRelation[1:]}::{self.name}"
        )

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
    def _manifest(self) -> Optional["YamlManifest"]:
        tosca_template = self.template.spec.template
        if tosca_template and tosca_template.import_resolver:
            return tosca_template.import_resolver.manifest
        return None

    @property
    def uri(self) -> str:
        manifest = self._manifest
        if not manifest:
            return "#" + self.key
        if "#" in manifest.uri:
            return manifest.uri + "?" + self.key
        else:
            return manifest.uri + "#" + self.key

    @property
    def deployment(self) -> str:
        manifest = self._manifest
        return manifest.deployment if manifest else ""

    @property
    def artifacts(self):
        return {}

    @property
    def attributes(self) -> ResultsMap:
        """attributes are live values but _attributes will be the serialized value"""
        if not self.root.attributeManager:
            if not self.attributeManager:
                # inefficient but create a local one for now
                self.attributeManager = AttributeManager()
            return self.attributeManager.get_attributes(self)

        # returned registered attribute or create a new one
        # attribute class getter resolves references
        return self.root.attributeManager.get_attributes(self)

    @property
    def names(self):
        return self.attributes

    def get_default_relationships(self, relation=None) -> List["RelationshipInstance"]:
        return self.root.get_default_relationships(relation)

    @property
    def shadow(self) -> Optional["EntityInstance"]:
        if self.imported:
            imports = self.root.imports
            if imports and self.imported in imports:
                return imports[self.imported].external_instance
            else:
                raise UnfurlError(
                    f'could not find imported instance "{self.imported}" for local instance "{self.name}"'
                )
        return None

    @property
    def nested_name(self) -> str:
        if self.template.topology.substitute_of:
            return self.template.topology.substitute_of.nested_name + ":" + self.name
        return self.name

    @property
    def readonly(self) -> bool:
        return bool(self.imported)  # imported instances are readonly

    @property
    def apex(self):
        if isinstance(self.root, TopologyInstance):
            if self.root.parent_topology:  # type: ignore
                return cast(TopologyInstance, self.root.parent_topology).apex  # type: ignore
        return self.root

    def validate(self) -> None:
        """
        Raises UnfurlValidationError on failure.
        """
        if self.shadow:
            self.shadow.validate()
        elif self.template:
            self.template.validate()

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


class HasInstancesInstance(EntityInstance):
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

        self.instances: List[HasInstancesInstance] = []
        EntityInstance.__init__(self, name, attributes, parent, template, status)
        if self.root is self:
            self._all = _ChildResources(self)
            self._templar = Templar(DataLoader())
            self._environ = os.environ.copy()

    @property
    def key(self) -> InstanceKey:
        return cast(InstanceKey, f"::{self.name}")

    def get_self_and_descendants(self) -> Iterator["HasInstancesInstance"]:
        "Recursive descendent including self"
        yield self
        for r in self.instances:
            for descendent in r.get_self_and_descendants():
                yield descendent

    @property
    def descendants(self):
        return list(self.get_self_and_descendants())

    @property
    def descendents(self):
        # backward compatibility
        return self.descendants

    # XXX use find_instance instead and remove find_resource
    def find_resource(self, qualified_name) -> Optional["HasInstancesInstance"]:
        resourceid, sep, inner = qualified_name.partition(":")
        if self.name == resourceid:
            if inner:
                if self.shadow:
                    return self.shadow.root.find_resource(inner)
                return None
            return self
        for r in self.instances:
            child = r.find_resource(resourceid)
            if child:
                if inner:
                    if child.shadow:
                        return child.shadow.root.find_resource(inner)
                    return None
                return child
        return None

    find_instance = find_resource

    def find_instance_or_external(self, resourceid):
        instance = self.find_instance(resourceid)
        if instance:
            return instance
        instance = self.apex.find_instance(resourceid)
        if instance:
            return instance
        if self.imports:
            return self.imports.find_import(resourceid)
        return None

    @property
    def requirements(self) -> List["RelationshipInstance"]:
        return []

    def get_requirements(self, match) -> List["RelationshipInstance"]:
        if match is None:
            return self.requirements
        if isinstance(match, str):
            return [r for r in self.requirements if r.template.name == match]
        elif isinstance(match, NodeInstance):
            return [r for r in self.requirements if r.target == match]
        elif isinstance(match, CapabilityInstance):
            return [r for r in self.requirements if r.parent == match]
        elif isinstance(match, ArtifactInstance):
            return []
        else:
            raise UnfurlError(f'invalid match for get_requirements: "{match}"')

    def get_operational_dependents(self, seen=None):
        if seen is None:
            seen = set()
        for instance in self.instances:
            if id(instance) not in seen:
                seen.add(id(instance))
                yield instance


# both have occurrences
# only need to configure capabilities as required by a relationship
class CapabilityInstance(EntityInstance):
    # 3.7.2 Capability definition p. 97
    # 3.8.1 Capability assignment p. 114
    parentRelation = "_capabilities"
    templateType = CapabilitySpec  # type: ignore
    _relationships: Optional[List["RelationshipInstance"]] = None

    @property
    def relationships(self) -> List["RelationshipInstance"]:
        if self._relationships is None:
            self._relationships = []
        assert isinstance(self.template, CapabilitySpec)
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

        return self._relationships

    @property
    def key(self) -> InstanceKey:
        # XXX implement something like _ChildResources to enable ::name instead of [.name]
        assert self.parent
        return cast(
            InstanceKey, f"{self.parent.key}::.{'capabilities'}::[.name={self.name}]"
        )


class RelationshipInstance(EntityInstance):
    # 3.7.3 Requirements definition p. 99
    # 3.8.2 Requirements assignment p. 115
    parentRelation = "_relationships"
    templateType = RelationshipSpec
    if TYPE_CHECKING:
        template = templateType()
    _source: Optional["NodeInstance"] = None

    def __init__(
        self,
        name="",
        attributes=None,
        parent=None,
        template=None,
        status=Status.pending,
    ):
        EntityInstance.__init__(self, name, attributes, parent, template, status)

    @property
    def target(self) -> Optional["NodeInstance"]:
        # parent is a capability, return it's parent (a Node)
        if self.parent:
            return self.parent.parent
        else:
            return None

    @property
    def source(self) -> Optional["NodeInstance"]:
        if self._source is None:
            if self.template.source:
                sourceNode = self.root.get_root_instance(
                    self.template.source.toscaEntityTemplate
                ).find_instance(self.template.source.name)
                if sourceNode:
                    self._source = sourceNode
        return self._source

    @property
    def capability(self) -> Optional[CapabilityInstance]:
        return self.parent

    @property
    def key(self) -> InstanceKey:
        # XXX implement something like _ChildResources to enable ::name instead of [.name=name]
        if self.source:
            return cast(
                InstanceKey, f"{self.source.key}::.requirements::[.name={self.name}]"
            )
        elif not self.parent or self.parent is self.root:  # it's a default relationship
            return cast(InstanceKey, f"::.requirements::[.name={self.name}]")
        elif self.parent:  # capability is parent
            return cast(
                InstanceKey, f"{self.parent.key}::.relationships::[.name={self.name}]"
            )

    def merge_props(self, matchfn, delete_if_none=False) -> Dict[str, str]:
        env: Dict[str, str] = {}
        capability = self.parent
        if capability:
            for name, val in matchfn(
                capability.template.find_props(capability.attributes)
            ):
                if val is None:
                    if delete_if_none:
                        env.pop(name, None)
                else:
                    env[name] = val
        for name, val in matchfn(self.template.find_props(self.attributes)):
            if val is None:
                if delete_if_none:
                    env.pop(name, None)
            else:
                env[name] = val
        return env

    def __repr__(self):
        return f"RelationshipInstance('{self.name}')"


class ArtifactInstance(EntityInstance):
    parentRelation = "_artifacts"
    templateType = ArtifactSpec  # type: ignore # XXX type error doesn't make sense
    if TYPE_CHECKING:
        template = ArtifactSpec({})

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
    def repository(self) -> Optional["toscaparser.repositories.Repository"]:
        return self.template.repository

    def as_import_spec(self):
        return self.template.as_import_spec()

    def get_operational_dependencies(self):
        # skip dependency on the parent
        for d in self.dependencies:
            yield d

    def __repr__(self):
        return f"ArtifactInstance('{self.name}')"


class NodeInstance(HasInstancesInstance):
    templateType = NodeSpec
    if TYPE_CHECKING:
        template = templateType()

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
        self._requirements: List["RelationshipInstance"] = []
        self._artifacts = []
        self._named_artifacts = None
        HasInstancesInstance.__init__(self, name, attributes, parent, template, status)

        self._interfaces = {}
        # preload
        self.get_interface("inherit")
        self.get_interface("default")

    def _find_relationship(
        self, relationship: RelationshipSpec
    ) -> Optional[RelationshipInstance]:
        """
        Find RelationshipInstance that has the give relationship template
        """
        assert relationship and relationship.capability and relationship.target
        # find the Capability instance that corresponds to this relationship template's capability
        targetNodeInstance = self.root.get_root_instance(
            relationship.target.toscaEntityTemplate
        ).find_resource(relationship.target.name)
        if not targetNodeInstance:
            logger.warning(
                f'target instance "{relationship.target.nested_name}" should have already been created -- is "{self.name}" out of sync with latest templates?'
            )
            return None
        assert isinstance(targetNodeInstance, NodeInstance)
        for cap in targetNodeInstance.capabilities:
            if cap.template is relationship.capability:
                for relInstance in cap.relationships:
                    if relInstance.template is relationship:
                        return relInstance
        return None

    @property
    def requirements(self) -> List[RelationshipInstance]:
        assert isinstance(self.template, NodeSpec)
        if len(self._requirements) != len(self.template.requirements):
            # instantiate missing relationships (they are only added, never deleted)
            instantiated = {id(r.template) for r in self._requirements}
            for name, template in self.template.requirements.items():
                if not template.relationship:
                    # XXX ValidationError
                    continue
                if id(template.relationship) not in instantiated:
                    relInstance = self._find_relationship(template.relationship)
                    if not relInstance:
                        logger.warning(
                            f'can not find relation instance for requirement "{name}" on node "{self.name}"'
                        )
                        continue
                    assert template.relationship is relInstance.template, (
                        template.relationship,
                        relInstance.template,
                    )
                    assert relInstance.template.source
                    assert (
                        self.template is relInstance.template.source
                        or self.template.topology.substitute_of
                        is relInstance.template.source
                        or self.template
                        is relInstance.template.source.topology.substitute_of
                    ), (
                        self.template,
                        relInstance.template.source,
                    )
                    self._requirements.append(relInstance)

        return self._requirements

    @property
    def capabilities(self) -> List[CapabilityInstance]:
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

    def get_capabilities(self, name) -> List[CapabilityInstance]:
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
            if self.template:
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

    @property
    def repository(self) -> Optional["toscaparser.repositories.Repository"]:
        if self.template.is_compatible_type("unfurl.nodes.Repository"):
            # this is a reified repository -- return the Repository with the same name
            return self.template.spec.get_repository(self.template.name)
        return None

    def _get_default_relationships(self, relation=None):
        if self.root is self:
            return
        for rel in self.root.get_default_relationships(relation):
            for capability in self.capabilities:
                if rel.template.matches_target(capability.template):
                    yield rel

    def get_default_relationships(self, relation=None) -> List[RelationshipInstance]:
        return list(self._get_default_relationships(relation))

    @property
    def sources(self):
        dep: Dict[str, Union[EntityInstance, List[EntityInstance]]] = {}
        for cap in self.capabilities:
            for rel in cap.relationships:
                if rel.source:
                    if rel.name in dep:
                        val = dep[rel.name]
                        if isinstance(val, list):
                            val.append(rel.source)
                        else:
                            dep[rel.name] = [val, rel.source]
                    else:
                        dep[rel.name] = rel.source
        return dep

    def _configured_by(self):
        for cap in self.capabilities:
            for rel in cap.relationships:
                if rel.source:
                    if rel.template.is_compatible_type(
                        "unfurl.relationships.Configures"
                    ):
                        yield rel.source
                    yield from rel.source._configured_by()

    @property
    def configured_by(self):
        return list(self._configured_by())

    @property
    def targets(self) -> Dict[str, Union["NodeInstance", List["NodeInstance"]]]:
        dep: Dict[str, Union[NodeInstance, List[NodeInstance]]] = {}
        for rel in self.requirements:
            if rel.target:
                if rel.name in dep:
                    target = dep[rel.name]
                    if isinstance(target, list):
                        target.append(rel.target)
                    else:
                        dep[rel.name] = [target, rel.target]
                else:
                    dep[rel.name] = rel.target
        return dep

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
            # force resolve
            return self.attributes._getresult(key)
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

    def get_operational_dependencies(self):
        yield from super().get_operational_dependencies()

        for instance in self.requirements:
            # super() yields self.parent so skip that one
            if instance.target is not self.parent:
                yield instance

    def get_operational_dependents(self, seen=None):
        if seen is None:
            seen = set()
        for cap in self.capabilities:
            for rel in cap.relationships:
                dep = rel.source
                if dep and id(dep) not in seen:
                    seen.add(id(dep))
                    yield dep

        yield from super().get_operational_dependents(seen)

    def add_interface(self, klass, name=None):
        if not isinstance(klass, str):
            klass = klass.__module__ + "." + klass.__name__
        current = self._attributes.setdefault(".interfaces", {})
        current[name or klass] = klass
        return current

    def get_interface(self, name):
        # XXX uses TOSCA interfaces instead
        if ".interfaces" not in self._attributes:
            return None  # no interfaces

        if not isinstance(name, str):
            name = name.__module__ + "." + name.__name__
        instance = self._interfaces.get(name)
        if instance:
            return instance
        else:
            className = self._attributes[".interfaces"].get(name)
            if className:
                instance = load_class(className)(name, self)  # type: ignore
                self._interfaces[name] = instance
                return instance
        return None

    def __repr__(self):
        return f"NodeInstance('{self.name}')"


class TopologyInstance(HasInstancesInstance):
    templateType = TopologySpec  # type: ignore

    def __init__(
        self,
        template: "TopologySpec",
        status=None,
        parent_topology: Optional["TopologyInstance"] = None,
    ):
        attributes = dict(inputs=template.inputs, outputs=template.outputs)
        HasInstancesInstance.__init__(self, "root", attributes, None, template, status)

        self._relationships: Optional[List["RelationshipInstance"]] = None
        self._tmpDir: Optional[str] = None
        self.parent_topology = parent_topology

    def set_base_dir(self, baseDir: str) -> None:
        self._baseDir = baseDir
        if not self._templar or self._templar._basedir != baseDir:
            loader = DataLoader()
            loader.set_basedir(baseDir)
            self._templar = Templar(loader)

    def set_attribute_manager(
        self, attribute_manager: Optional[AttributeManager]
    ) -> None:
        self.attributeManager = attribute_manager
        if self.parent_topology:
            self.parent_topology.set_attribute_manager(attribute_manager)

    @property
    def requirements(self) -> List[RelationshipInstance]:
        """
        The root node returns RelationshipInstances representing default relationship templates
        """
        if self._relationships is None:
            self._relationships = []
            relTemplates = cast(TopologySpec, self.template).default_relationships
            for template in relTemplates:
                # this constructor will add itself to _relationships
                RelationshipInstance(template.name, parent=self, template=template)

        return self._relationships

    def get_default_relationships(
        self, relation: Optional[str] = None
    ) -> List[RelationshipInstance]:
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

    def create_nested_topology(
        self, topology: TopologySpec, status=None
    ) -> "TopologyInstance":
        nested_root = TopologyInstance(topology, status, self)
        nested_root.set_attribute_manager(self.attributeManager)
        nested_root.set_base_dir(self._baseDir)
        nested_root.imports = self.imports
        nested_root._environ = self._environ
        if self.imports:
            self.imports.add_import(":" + topology.nested_name, nested_root)
        return nested_root

    def get_root_instance(self, source: "NodeTemplate") -> "TopologyInstance":
        topology = self.template.spec.get_topology(source)
        assert topology
        if self.template.topology is not topology:
            assert self.imports is not None
            nested_root = self.imports.find_import(":" + topology.nested_name)
            if nested_root:
                return cast(TopologyInstance, nested_root)
            else:
                return self.create_nested_topology(topology)
        return self
