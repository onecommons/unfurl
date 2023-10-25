# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
import collections.abc
import dataclasses
from enum import Enum
import inspect
import threading
import typing
import os.path
import datetime
import re
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    ForwardRef,
    Generic,
    Iterator,
    Mapping,
    NamedTuple,
    Sequence,
    Set,
    Union,
    List,
    Optional,
    Type,
    TypeVar,
    cast,
    overload,
    Tuple,
)
import types
from typing_extensions import (
    dataclass_transform,
    get_args,
    get_origin,
    Annotated,
    Literal,
    Self,
)
import sys
import logging

logger = logging.getLogger("tosca")

from toscaparser.elements.datatype import DataType as ToscaDataType
from .scalars import *


if typing.TYPE_CHECKING:
    from .python2yaml import PythonToYaml


class _LocalState(threading.local):
    def __init__(self, **kw):
        self.mode = "spec"
        self._in_process_class = False
        self.__dict__.update(kw)


global_state = _LocalState()

yaml_cls = dict


class ToscaObject:
    _tosca_name: str = ""

    @classmethod
    def tosca_type_name(cls) -> str:
        _tosca_name = cls.__dict__.get("_type_name")
        return _tosca_name if _tosca_name else cls.__name__

    def to_yaml(self, dict_cls=dict) -> Optional[Dict]:
        return None


class DataConstraint(ToscaObject):
    def __init__(self, constraint):
        self.constraint = constraint

    def to_yaml(self, dict_cls=dict) -> Optional[Dict]:
        return {self.__class__.__name__: to_tosca_value(self.constraint)}

    def apply_constraint(self, val: Any) -> bool:
        assert isinstance(val, FieldProjection), val
        val.apply_constraint(self)
        return True


class equal(DataConstraint):
    pass


class greater_than(DataConstraint):
    pass


class greater_or_equal(DataConstraint):
    pass


class less_than(DataConstraint):
    pass


class less_or_equal(DataConstraint):
    pass


T = TypeVar("T")


class in_range(DataConstraint, Generic[T]):
    def __init__(self, min: T, max: T):
        super().__init__([min, max])

    def apply_constraint(self, val: Optional[T]) -> bool:
        return super().apply_constraint(val)


class valid_values(DataConstraint):
    pass


class length(DataConstraint):
    pass


class min_length(DataConstraint):
    pass


class max_length(DataConstraint):
    pass


class pattern(DataConstraint):
    pass


class schema(DataConstraint):
    pass


class Namespace(types.SimpleNamespace):
    @classmethod
    def get_defs(cls):
        ignore = ("__doc__", "__module__", "__dict__", "__weakref__")
        return {k: v for k, v in cls.__dict__.items() if k not in ignore}

    location: str


# XXX dependencies, invoke, preConditions
def operation(
    name="",
    apply_to: Sequence[str] = (),
    timeout: Optional[float] = None,
    operation_host: Optional[str] = None,
    environment: Optional[Dict[str, str]] = None,
) -> Callable:
    """Function decorator that marks a function or methods as a TOSCA operation.

    Args:
        name (str, optional): Name of the TOSCA operation. Defaults to the name of the function.
        apply_to (Sequence[str], optional): List of TOSCA operations to apply this method to. If omitted, match by the operation name.
        timeout (Optional[float], optional): Timeout for the operation (in seconds). Defaults to None.
        operation_host (Optional[str], optional): The name of host where this operation will be executed. Defaults to None.
        environment (Optional[Dict[str, str]], optional): A map of environment variables to use while executing the operation. Defaults to None.

    This example marks a method a implementing the ``create`` and ``delete`` operations on the ``Standard`` TOSCA interface.

    .. code-block:: python

        @operation(apply_to=["Standard.create","Standard.delete"])
        def default(self):
            return self.my_artifact.execute()

    """

    def decorator_operation(func):
        func.operation_name = name or func.__name__
        func.apply_to = apply_to
        func.timeout = timeout
        func.operation_host = operation_host
        func.environment = environment
        return func

    return decorator_operation


class tosca_timestamp(str):
    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({super().__repr__()})"


class tosca_version(str):
    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({super().__repr__()})"


#  see constraints.PROPERTY_TYPES
TOSCA_SIMPLE_TYPES: Dict[str, str] = dict(
    integer="int",
    string="str",
    boolean="bool",
    float="float",
    number="float",
    timestamp="tosca_timestamp",
    map="Dict",
    list="List",
    version="tosca_version",
    any="Any",
    range="Tuple[int, int]",
)
TOSCA_SIMPLE_TYPES.update(
    {
        "scalar-unit.size": "Size",
        "scalar-unit.frequency": "Frequency",
        "scalar-unit.time": "Time",
        "scalar-unit.bitrate": "Bitrate",
    }
)

PYTHON_TO_TOSCA_TYPES = {v: k for k, v in TOSCA_SIMPLE_TYPES.items()}
PYTHON_TO_TOSCA_TYPES["Tuple"] = "range"

TOSCA_SHORT_NAMES = {
    "PortDef": "tosca.datatypes.network.PortDef",
    "PortSpec": "tosca.datatypes.network.PortSpec",
}


def _to_union(types):
    if len(types) == 3:
        return Union[types[0], types[1], types[2]]
    elif len(types) == 2:
        return Union[types[0], types[1]]
    else:
        return ForwardRef(types[0])


def _get_type_name(_type):
    # work-around _SpecialType limitations in older Python versions
    return getattr(_type, "__name__", getattr(_type, "_name", ""))


def get_optional_type(_type) -> Tuple[bool, Any]:
    # if not optional return false, type
    # else return true, type or type
    if isinstance(_type, str):
        union = [t.strip() for t in _type.split("|")]
        try:
            union.remove("None")
            return True, _to_union(union)
        except ValueError:
            return False, _to_union(union)
    args = get_args(_type)
    origin = get_origin(_type)
    if (
        origin
        and _get_type_name(origin) in ["Union", "UnionType"]
        and type(None) in args
    ):
        _types = [arg for arg in args if arg is not type(None)]
        if not _types:
            return True, type(None)
        elif len(_types) > 1:  # return origin type
            return True, _type
        else:
            return True, _types[0]
    return False, _type


class TypeInfo(NamedTuple):
    optional: bool
    collection: Optional[type]
    types: tuple
    metadata: Any


def pytype_to_tosca_type(_type, as_str=False) -> TypeInfo:
    optional, _type = get_optional_type(_type)
    origin = get_origin(_type)
    if origin is Annotated:
        metadata = _type.__metadata__[0]
        _type = get_args(_type)[0]
    else:
        metadata = None
    origin = get_origin(_type)
    collection = None
    collection_types = (list, collections.abc.Sequence, dict)
    if origin in collection_types:
        if origin == collections.abc.Sequence:
            collection = list
        else:
            collection = origin
        args = get_args(_type)
        if args:
            _type = get_args(_type)[1 if origin is dict else 0]
        else:
            _type = Any
        origin = get_origin(_type)

    if _get_type_name(origin) in ["Union", "UnionType"]:
        types = get_args(_type)
    else:
        types = (_type,)

    def to_str(_type):
        if isinstance(_type, ForwardRef):
            return _type.__forward_arg__
        elif not isinstance(_type, str):
            return _type.__name__
        else:
            return _type

    if as_str:
        types = tuple(to_str(t) for t in types)
    return TypeInfo(optional, collection, types, metadata)


def to_tosca_value(obj, dict_cls=dict):
    if isinstance(obj, dict):
        return dict_cls((k, to_tosca_value(v, dict_cls)) for k, v in obj.items())
    elif isinstance(obj, list):
        return [to_tosca_value(v, dict_cls) for v in obj]
    else:
        to_yaml = getattr(obj, "to_yaml", None)
        if to_yaml:  # e.g. datatypes, _Scalar
            return to_yaml(dict_cls)
        else:
            return obj


def metadata_to_yaml(metadata: Mapping):
    return dict(metadata)


class ToscaFieldType(Enum):
    property = "properties"
    attribute = "attributes"
    capability = "capabilities"
    requirement = "requirements"
    artifact = "artifacts"


class _REQUIRED_TYPE:
    pass


REQUIRED = _REQUIRED_TYPE()


MISSING = dataclasses.MISSING


class Options(frozenset):
    def asdict(self):
        return {n: True for n in self}


class PropertyOptions(Options):
    pass


class AttributeOptions(Options):
    pass


class _Tosca_Field(dataclasses.Field):
    title = None
    node_filter: Optional[Dict[str, Any]] = None
    valid_source_types: Optional[List[str]] = None

    def __init__(
        self,
        field_type: Optional[ToscaFieldType],
        default=dataclasses.MISSING,
        default_factory=dataclasses.MISSING,
        name: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        title: str = "",
        status: str = "",
        constraints: Optional[List[DataConstraint]] = None,
        options: Optional[Options] = None,
        declare_attribute: bool = False,
        owner: Optional[Type["_ToscaType"]] = None,
    ):
        if metadata is None:
            metadata = {}
        if options:
            metadata.update(options.asdict())
        args = [
            self,
            default,
            default_factory,
            field_type != ToscaFieldType.attribute,  # init
            True,  # repr
            None,  # hash
            True,  # compare
            metadata or {},
        ]
        _has_default = (
            default is not dataclasses.MISSING
            or default_factory is not dataclasses.MISSING
        )
        if sys.version_info.minor > 9:
            args.append(True)  # kw_only
        elif not _has_default:
            # we have to have all fields have a default value
            # because the ToscaType base classes have init fields with default values
            # and python < 3.10 dataclasses will raise an error
            # XXX add  __post_init that raises error if value is REQUIRED (i.e. keyword argument was missing)
            args[1] = REQUIRED  # set default
        dataclasses.Field.__init__(*args)
        self._tosca_field_type = field_type
        self._tosca_name = name
        # note self.name and self.type are set later (in dataclasses._get_field)
        self.description: Optional[str] = None  # set in _shared_cls_to_yaml
        self.title = title
        self.status = status
        self.declare_attribute = declare_attribute
        self.constraints: List[DataConstraint] = constraints or []
        self.owner = owner

    def set_constraint(self, val):
        # this called via _set_constraints
        if isinstance(val, _Ref):
            if self._tosca_field_type in [
                ToscaFieldType.capability,
                ToscaFieldType.artifact,
            ]:
                raise AttributeError(
                    "can not set {val} on {self}: {self._tosca_field_type} attributes can't be references"
                )
            if self._tosca_field_type == ToscaFieldType.requirement:
                # if requirement and value is a Ref, set a node filter
                self.add_node_filter(val)
                return
        # val is a concrete value or self is a property or attribute
        # either way, set the default
        self._set_default(val)

    def set_property_constraint(self, name: str, val: Any):
        # this called via _set_constraints
        # if self is a requirement, name is a property on the target node or the relationship
        if self._tosca_field_type == ToscaFieldType.requirement:
            # if requirement, set a node filter (val can be Ref or concrete value)
            self.add_node_filter(val, name)
            return

        # if self is a capability or artifact, name is a property on the capability or artifact
        # if self is property or attribute, name is a field on the value (which must be a datatype or map)
        if (
            self.default is dataclasses.MISSING
            or self.default is None
            or isinstance(self.default, _Ref)
        ):
            # there's no value to set the attribute on!
            raise AttributeError(
                "can not set {name} on {self}: property constraints require a concrete default value"
            )
        else:
            # XXX validate name is valid property and that val is compatible type
            # XXX mark default as a constraint
            # XXX default is shared across template instances and subtypes -- what about mutable values like dicts and basically all Toscatypes?
            setattr(self.default, name, val)

    def add_node_filter(
        self, val, prop_name: Optional[str] = None, capability: Optional[str] = None
    ):
        assert self._tosca_field_type == ToscaFieldType.requirement
        if self.node_filter is None:
            self.node_filter = {}
        if capability:
            cap_filters = self.node_filter.setdefault("capabilities", [])
            for cap_filter in cap_filters:
                if list(cap_filter)[0] == capability:
                    node_filter = cap_filter[capability]
                    break
            else:
                node_filter = {}
                cap_filters.append({capability: node_filter})
        else:
            node_filter = self.node_filter
        if prop_name:
            prop_filters = node_filter.setdefault("properties", [])
            if isinstance(val, _Ref):
                val.set_source()
            elif isinstance(val, DataConstraint):
                val = val.to_yaml()
            else:
                # XXX validate that val is compatible type
                val = {
                    "q": val
                }  # quote the value to distinguish from built-in tosca node_filters
            prop_filters.append({prop_name: val})
        elif isinstance(val, _Ref):
            match_filters = self.node_filter.setdefault("match", [])
            match_filters.append(val)
        else:
            # val is concrete value, just replace the default
            self._set_default(val)

    def _set_default(self, val):
        if isinstance(val, DataConstraint):
            if self.tosca_field_type not in [
                ToscaFieldType.property,
                ToscaFieldType.attribute,
            ]:
                raise ValueError(
                    "Value constraints can not be assigned to a TOSCA "
                    + self.tosca_field_type.name
                )
            else:
                self.constraints.append(val)
        else:
            # XXX we can be smarter based on val type, e.g. node or relationship template and merge with the existing default values
            # XXX validate that val is compatible type
            # XXX mark default as a constraint
            # XXX default is shared across template instances and subtypes -- what about mutable values like dicts and basically all Toscatypes?
            self.default = val

    def as_ref_expr(self) -> str:
        if self.tosca_field_type in [
            ToscaFieldType.property,
            ToscaFieldType.attribute,
        ]:
            return self.tosca_name
        elif self.tosca_field_type == ToscaFieldType.requirement:
            return ".targets::" + self.tosca_name
            # but if accessing the relationship template, need to use the form below
        else:
            assert self.tosca_field_type
            return f".{self.tosca_field_type.value}[.name={self.tosca_name}]"

    def _resolve_class(self, _type):
        assert self.owner, (self, _type)
        return self.owner._resolve_class(_type)

    def get_type_info(self) -> TypeInfo:
        type_info = pytype_to_tosca_type(self.type)
        types = tuple(self._resolve_class(t) for t in type_info.types)
        return type_info._replace(types=types)

    def get_type_info_checked(self) -> Optional[TypeInfo]:
        try:
            return self.get_type_info()
        except NameError as e:
            logger.warning("error while converting python to yaml: " + str(e))
            return None

    def guess_field_type(self) -> ToscaFieldType:
        type_info = self.get_type_info()
        has_capability = False
        field_type = ToscaFieldType.property
        for _type in type_info.types:
            if not isinstance(_type, type):
                continue
            if issubclass(_type, NodeType) or issubclass(_type, RelationshipType):
                field_type = ToscaFieldType.requirement
                break
            elif issubclass(_type, ArtifactType):
                field_type = ToscaFieldType.artifact
                break
            elif issubclass(_type, CapabilityType):
                has_capability = True
        else:
            if has_capability:
                field_type = ToscaFieldType.capability
        return field_type

    @property
    def tosca_field_type(self) -> ToscaFieldType:
        if self._tosca_field_type is None:
            self._tosca_field_type = self.guess_field_type()
        return self._tosca_field_type

    @property
    def tosca_name(self) -> str:
        return self._tosca_name or self.name

    @property
    def section(self) -> str:
        return self.tosca_field_type.value

    def to_yaml(self, converter: Optional["PythonToYaml"]) -> dict:
        if self.tosca_field_type == ToscaFieldType.property:
            field_def = self._to_property_yaml()
        elif self.tosca_field_type == ToscaFieldType.attribute:
            field_def = self._to_attribute_yaml()
        elif self.tosca_field_type == ToscaFieldType.requirement:
            field_def = self._to_requirement_yaml(converter)
        elif self.tosca_field_type == ToscaFieldType.capability:
            field_def = self._to_capability_yaml()
        elif self.tosca_field_type == ToscaFieldType.artifact:
            field_def = self._to_artifact_yaml(converter)
        else:
            assert False
        # note: description needs to be set when parsing ast
        if self.description:
            field_def["description"] = self.description
        if self.metadata:
            field_def.setdefault("metadata", {}).update(metadata_to_yaml(self.metadata))
        return {self.tosca_name: field_def}

    def _add_occurrences(self, field_def: dict, info: TypeInfo) -> None:
        occurrences = [1, 1]
        if info.optional or (not self.default and self.default == ()):
            occurrences[0] = 0
        if info.collection is list:
            occurrences[1] = "UNBOUNDED"  # type: ignore

        if occurrences != [1, 1]:
            field_def["occurrences"] = occurrences

    def _to_requirement_yaml(
        self, converter: Optional["PythonToYaml"]
    ) -> Dict[str, Any]:
        req_def: Dict[str, Any] = yaml_cls()
        info = self.get_type_info_checked()
        if not info:
            return req_def
        for _type in info.types:
            if issubclass(_type, RelationshipType):
                req_def["relationship"] = _type.tosca_type_name()
            elif issubclass(_type, CapabilityType):
                req_def["capability"] = _type.tosca_type_name()
            elif issubclass(_type, NodeType):
                req_def["node"] = _type.tosca_type_name()
        if self.node_filter:
            req_def["node_filter"] = to_tosca_value(self.node_filter)
        if converter:
            # set node or relationship name if default value is a node or relationship template
            converter.set_requirement_value(req_def, self.default, self.name)
        self._add_occurrences(req_def, info)
        return req_def

    def _to_capability_yaml(self) -> Dict[str, Any]:
        info = self.get_type_info_checked()
        if not info:
            return yaml_cls()
        assert len(info.types) == 1
        _type = info.types[0]
        assert issubclass(_type, _ToscaType), (self, _type)
        cap_def: dict = yaml_cls(type=_type.tosca_type_name())
        self._add_occurrences(cap_def, info)

        if self.valid_source_types:  # is not None: XXX only set to [] if declared
            cap_def["valid_source_types"] = self.valid_source_types
        return cap_def

    def _to_artifact_yaml(self, converter: Optional["PythonToYaml"]) -> Dict[str, Any]:
        if self.default and self.default is not dataclasses.MISSING:
            return self.default.to_template_yaml(converter)
        info = self.get_type_info_checked()
        if not info:
            return yaml_cls()
        assert len(info.types) == 1
        _type = info.types[0]
        assert issubclass(_type, _ToscaType), (self, _type)
        cap_def: dict = yaml_cls(type=_type.tosca_type_name())
        return cap_def

    def pytype_to_tosca_schema(self, _type) -> Tuple[dict, bool]:
        # dict[str, list[int, constraint], constraint]
        info = pytype_to_tosca_type(_type)
        assert len(info.types) == 1, info
        _type = info.types[0]
        schema: Dict[str, Any] = {}
        if info.collection is dict:
            tosca_type = "map"
        elif info.collection is list:
            tosca_type = "list"
        else:
            _type = self._resolve_class(_type)
            tosca_type = PYTHON_TO_TOSCA_TYPES.get(_get_type_name(_type), "")
            if not tosca_type:  # it must be a datatype
                assert issubclass(_type, DataType), _type
                tosca_type = _type.tosca_type_name()
                metadata = _type._get_property_metadata()
                if metadata:
                    schema["metadata"] = metadata
        schema["type"] = tosca_type
        if info.collection:
            schema["entry_schema"] = self.pytype_to_tosca_schema(_type)[0]
        if info.metadata:
            schema["constraints"] = [
                c.to_yaml() for c in info.metadata if isinstance(c, DataConstraint)
            ]
        return schema, info.optional

    def _to_attribute_yaml(self) -> dict:
        # self.type is from __annotations__
        prop_def, optional = self.pytype_to_tosca_schema(self.type)
        if self.constraints:
            prop_def.setdefault("constraints", []).extend(
                c.to_yaml() for c in self.constraints
            )
        if self.default_factory and self.default_factory is not dataclasses.MISSING:
            prop_def["default"] = to_tosca_value(self.default_factory())
        elif (
            self.default is not dataclasses.MISSING
            and self.default is not REQUIRED
            and self.default is not None
        ):
            # only set the default to null if required (not optional)
            prop_def["default"] = to_tosca_value(self.default)
        if self.title:
            prop_def["title"] = self.title
        if self.status:
            prop_def["status"] = self.status
        return prop_def

    def _to_property_yaml(self) -> dict:
        # self.type is from __annotations__
        prop_def, optional = self.pytype_to_tosca_schema(self.type)
        if optional:  # omit if required is True (the default)
            prop_def["required"] = False
        if self.constraints:
            prop_def.setdefault("constraints", []).extend(
                c.to_yaml() for c in self.constraints
            )
        if self.default_factory and self.default_factory is not dataclasses.MISSING:
            prop_def["default"] = to_tosca_value(self.default_factory())
        elif self.default is not dataclasses.MISSING and self.default is not REQUIRED:
            if self.default is not None or not optional:
                # only set the default to null when if property is required
                prop_def["default"] = to_tosca_value(self.default)
        if self.title:
            prop_def["title"] = self.title
        if self.status:
            prop_def["status"] = self.status
        return prop_def

    @staticmethod
    def infer_field(owner_class, name, value):
        if isinstance(value, _Tosca_Field):
            value.name = name
            if (
                not value.type
                and value.default is not dataclasses.MISSING
                and value.default is not REQUIRED
            ):
                value.type = type(value)
            return value
        field = _Tosca_Field(None, owner=owner_class, default=value)
        field.name = name
        if isinstance(value, FieldProjection):
            field.type = value.field.type
            field._tosca_field_type = value.field._tosca_field_type
        else:
            field.type = type(value)
        if field.tosca_field_type != ToscaFieldType.property:
            return field
        return None


def _make_field_doc(func, status=False, extra: Sequence[str] = ()) -> None:
    name = func.__name__.lower()
    doc = f"""Field specifier for declaring a TOSCA {name}.

    Args:
        default (Any, optional): Default value. Set to None if the {name} isn't required. Defaults to MISSING.
        factory (Callable, optional): Factory function to initialize the {name} with a unique value per template. Defaults to MISSING.
        name (str, optional): TOSCA name of the field, overrides the {name}'s name when generating YAML. Defaults to "".
        metadata (Dict[str, str], optional): Dictionary of metadata to associate with the {name}.\n"""
    indent = "        "
    if status:
        doc += f"{indent}constraints (List[DataConstraints], optional): List of TOSCA property constraints to apply to the {name}.\n"
        doc += f"{indent}status (str, optional): TOSCA status of the {name}.\n"
        doc += f"{indent}options ({func.__name__}Options, optional): Typed metadata to apply.\n"
    for arg in extra:
        doc += f"{indent}{arg}\n"
    func.__doc__ = doc


def Attribute(
    *,
    default=None,
    factory=MISSING,
    name: str = "",
    constraints: Optional[List[DataConstraint]] = None,
    metadata: Optional[Dict[str, str]] = None,
    title="",
    status="",
    options: Optional[AttributeOptions] = None,
    # attributes are excluded from __init__,
    # this tricks the static checker, see pep 681:
    init: Literal[False] = False,
) -> Any:
    field = _Tosca_Field(
        ToscaFieldType.attribute,
        default,
        factory,
        name,
        metadata,
        title,
        status,
        constraints=constraints,
        options=options,
    )
    return field


_make_field_doc(Attribute, True)


def Property(
    *,
    default=MISSING,
    factory=MISSING,
    name: str = "",
    constraints: Optional[List[DataConstraint]] = None,
    metadata: Optional[Dict[str, str]] = None,
    title: str = "",
    status: str = "",
    options: Optional[PropertyOptions] = None,
    attribute: bool = False,
) -> Any:
    field = _Tosca_Field(
        ToscaFieldType.property,
        default=default,
        default_factory=factory,
        name=name,
        metadata=metadata,
        title=title,
        status=status,
        constraints=constraints,
        options=options,
        declare_attribute=attribute,
    )
    return field


_make_field_doc(
    Property,
    True,
    [
        "attribute (bool, optional): Indicate that the property is also a TOSCA attribute. Defaults to False."
    ],
)


def Requirement(
    *,
    default=MISSING,
    factory=MISSING,
    name: str = "",
    metadata: Optional[Dict[str, str]] = None,
    node_filter: Optional[Dict[str, Any]] = None,
) -> Any:
    field = _Tosca_Field(ToscaFieldType.requirement, default, factory, name, metadata)
    field.node_filter = node_filter
    return field


_make_field_doc(
    Requirement,
    False,
    [
        "node_filter (Dict[str, Any], optional): The TOSCA node_filter for this requirement."
    ],
)


def Capability(
    *,
    default=MISSING,
    factory=MISSING,
    name: str = "",
    metadata: Optional[Dict[str, str]] = None,
    valid_source_types: Optional[List[str]] = None,
) -> Any:
    field = _Tosca_Field(ToscaFieldType.capability, default, factory, name, metadata)
    field.valid_source_types = valid_source_types or []
    return field


_make_field_doc(
    Capability,
    False,
    [
        "valid_source_types (List[str], optional): List of TOSCA type names to set as the capability's valid_source_types"
    ],
)


def Artifact(
    *,
    default=MISSING,
    factory=MISSING,
    name: str = "",
    metadata: Optional[Dict[str, str]] = None,
) -> Any:
    field = _Tosca_Field(ToscaFieldType.artifact, default, factory, name, metadata)
    return field


_make_field_doc(Artifact)


class _Ref:
    def __init__(self, expr: Optional[Dict[str, Any]]):
        self.expr: Optional[Dict[str, Any]] = expr

    def set_source(self):
        if self.expr:
            expr = self.expr.get("eval")
            if expr and isinstance(expr, str) and expr[0] not in ["$", ":"]:
                self.expr["eval"] = "$SOURCE::" + expr

    def __repr__(self):
        return f"_Ref({self.expr})"

    def to_yaml(self, dict_cls=dict):
        return self.expr

    # note: we need this to prevent dataclasses error on 3.11+: mutable default for field
    def __hash__(self) -> int:
        return hash(str(self.expr))

    def __eq__(self, __value: object) -> bool:
        if isinstance(__value, type(self.expr)):
            return self.expr == __value
        elif isinstance(__value, _Ref):
            return self.expr == __value.expr
        return False


def Eval(expr=None) -> Any:
    return _Ref(expr)


# XXX class RefList(Ref)


class _TemplateRef:
    def __init__(self, name: str):
        self.name = name

    def to_yaml(self, *ignore) -> str:
        # assume this will be used in contexts where the template is referenced by name
        return self.name


class NodeTemplateRef(_TemplateRef):
    "Use this to refer to TOSCA node templates that are not visible to your Python code."


class RelationshipTemplateRef(_TemplateRef):
    "Use this to refer to TOSCA relationship templates are not visible to your Python code"


def find_node(name: str) -> Any:
    return NodeTemplateRef(name)


def find_relationship(name: str) -> Any:
    return RelationshipTemplateRef(name)


class FieldProjection(_Ref):
    "A reference to a tosca field or projection off a tosca field"
    # created by _DataclassTypeProxy, invoked via _set_constraint

    def __init__(self, field: _Tosca_Field, parent: Optional["FieldProjection"] = None):
        # currently don't support projections that are requirements
        expr = field.as_ref_expr()
        if parent and parent.expr:
            # XXX map to tosca name but we can't do this now because it might be too early to resolve the attribute's type
            expr = parent.expr["eval"] + "::" + expr
        super().__init__(dict(eval=expr))
        self.field = field
        self.parent = parent

    def __getattr__(self, name):
        # unfortunately _set_constraints is called during class construction type
        # so _resolve_class might not work with forward references defined in the same module
        # cls = self._resolve_class(self.field.type)
        try:
            ti = self.field.get_type_info()
            field = ti.types[0].__dataclass_fields__[name]
            return FieldProjection(field, self)
        except:
            # couldn't resolve the type
            # XXX if current field type isn't a requirement we can assume name is property
            raise AttributeError(
                f"Can't project {name} from {self}: Only one level of field projection currently supported"
            )

    def __setattr__(self, name, val):
        if name in ["expr", "field", "parent"]:
            object.__setattr__(self, name, val)
            return
        if self.parent:
            raise ValueError(
                f"Can't set {name} on {self}: Only one level of field projection currently supported"
            )
        try:
            ti = self.field.get_type_info()
            field = ti.types[0].__dataclass_fields__[name]
        except:
            # couldn't resolve the type
            # XXX if current field type isn't a requirement we can assume name is property
            raise AttributeError(
                f"Can't project {name} from {self}: Only one level of field projection currently supported"
            )
        else:
            if field.tosca_field_type in [
                ToscaFieldType.property,
                ToscaFieldType.attribute,
            ]:
                self.field.set_property_constraint(name, val)
            else:
                raise ValueError(
                    f'{ti.types} Can not set "{name}" on {self}: "{name}" is a {field.tosca_field_type.name}, not a TOSCA property'
                )

    def __delattr__(self, name):
        raise AttributeError(name)

    def apply_constraint(self, c: DataConstraint):
        if self.field.tosca_field_type in [
            ToscaFieldType.property,
            ToscaFieldType.attribute,
        ]:
            if (
                self.parent
                and self.parent.field.tosca_field_type == ToscaFieldType.capability
            ):
                capability = self.parent.field.tosca_name
                parent = self.parent.parent
                if (
                    not parent
                    or parent.field.tosca_field_type != ToscaFieldType.requirement
                ):
                    raise ValueError(
                        "Can not create node filter on capability '{capability}', expression doesn't reference a requirement."
                    )
            else:
                parent = self.parent
                capability = None
            if parent and parent.field.tosca_field_type == ToscaFieldType.requirement:
                parent.field.add_node_filter(c, self.field.tosca_name, capability)
            else:
                self.field.constraints.append(c)
        else:
            raise ValueError(
                "Value constraints can not be assigned to a TOSCA "
                + self.field.tosca_field_type.name
            )


def get_annotations(o):
    # return __annotations__ (but not on base classes)
    # see https://docs.python.org/3/howto/annotations.html
    if hasattr(inspect, "get_annotations"):
        return inspect.get_annotations(o)  # 3.10 and later
    if isinstance(o, type):  # < 3.10
        return o.__dict__.get("__annotations__", None)
    else:
        return getattr(o, "__annotations__", None)


class _DataclassTypeProxy:
    # this is wraps the data type class passed to _set_constraint
    # we need this to because __setattr__ and __set__ descriptors don't work on cls attributes
    # (and __set_name__ isn't called after class initialization)

    def __init__(self, cls):
        self.cls = cls

    def __getattr__(self, name):
        # we need to check the base class's __dataclass_fields__ first
        fields = getattr(self.cls, "__dataclass_fields__", {})
        val = fields.get(name)
        if not val:
            # but our __dataclass_fields__ isn't updated yet, do a regular getattr
            val = getattr(self.cls, name)
        if isinstance(val, _Tosca_Field):
            return FieldProjection(val, None)
        return val

    def __setattr__(self, name, val):
        if name == "cls":
            object.__setattr__(self, name, val)
        elif not hasattr(self.cls, name):
            setattr(self.cls, name, val)
        else:
            attr = getattr(self.cls, name)
            if isinstance(attr, _Tosca_Field):
                attr.set_constraint(val)
            else:
                setattr(self.cls, name, val)


def _make_dataclass(cls):
    kw = dict(
        init=True,
        repr=True,
        eq=True,
        order=False,
        unsafe_hash=True,
        frozen=False,
    )
    if sys.version_info.minor > 9:
        kw["match_args"] = True
        kw["kw_only"] = True
        kw["slots"] = False
    if sys.version_info.minor > 10:
        kw["weakref_slot"] = False
    # we need _Tosca_Fields not dataclasses.Fields
    # so for any declarations of tosca fields (properties, requirements, etc)
    # missing a _Tosca_Fields, set one before calling _process_class()
    global_state._in_process_class = True
    try:
        annotations = cls.__dict__.get("__annotations__", {})
        for name, type in annotations.items():
            if name[0] != "_":
                field = None
                default = getattr(cls, name, REQUIRED)
                if not isinstance(default, dataclasses.Field):
                    # XXX or not InitVar or ClassVar
                    field = _Tosca_Field(None, default, owner=cls)
                    setattr(cls, name, field)
                elif isinstance(default, _Tosca_Field):
                    default.owner = cls
                    field = default
                if field:
                    field.name = name
                    field.type = type
        if cls.__module__ != __name__:
            for name, value in cls.__dict__.items():
                if name[0] != "_" and name not in annotations and not callable(value):
                    # for unannotated class attributes try to infer if they are TOSCA fields
                    field = _Tosca_Field.infer_field(cls, name, value)
                    if field:
                        annotations[name] = field.type
                        setattr(cls, name, field)
        _set_constraints = cls.__dict__.get("_set_constraints")
        if _set_constraints:
            global_state._in_process_class = False
            # _set_constraints should be a classmethod descriptor
            _set_constraints.__get__(None, _DataclassTypeProxy(cls))()
            global_state._in_process_class = True
        if not getattr(cls, "__doc__"):
            cls.__doc__ = " "  # suppress dataclass doc string generation
        assert (
            cls.__module__ in sys.modules
        ), cls.__module__  # _process_class checks this
        cls = dataclasses._process_class(cls, **kw)  # type: ignore
        # note: _process_class replaces each field with its default value (or delete the attribute)
        # replace those with _FieldDescriptors to allow class level attribute access to be customized
        for name in annotations:
            if name[0] != "_":
                field = cls.__dataclass_fields__.get(name)
                if field and isinstance(field, _Tosca_Field):
                    setattr(cls, name, _FieldDescriptor(field))
    finally:
        global_state._in_process_class = False
    return cls


class _DataclassType(type):
    def __set_name__(self, owner, name):
        if issubclass(owner, Namespace):
            # this will set the class attribute on the class being declared in the Namespace
            self._namespace = owner.get_defs()

    def __new__(cls, name, bases, dct):
        x = super().__new__(cls, name, bases, dct)
        return _make_dataclass(x)


class _Tosca_Fields_Getter:
    def __get__(self, obj, objtype=None) -> List[_Tosca_Field]:
        # only get the fields explicitly declared on the obj or class
        target = obj or objtype
        annotations = target.__dict__.get("__annotations__", {})
        return [
            f
            for f in dataclasses.fields(target)
            if isinstance(f, _Tosca_Field) and f.name in annotations
        ]


class _Set_ConfigSpec_Method:
    def __get__(self, obj, objtype) -> Callable:
        if obj:
            return obj._set_config_spec_
        else:
            return objtype._class_set_config_spec


class _FieldDescriptor:
    def __init__(self, field: _Tosca_Field):
        self.field = field

    def __get__(self, obj, obj_type):
        if obj or global_state._in_process_class:
            return self.field.default
        else:  # attribute access on the class
            projection = FieldProjection(self.field, None)
            # XXX add validation key to eval to assert one result only
            projection.expr = {
                "eval": f"::[.type={obj_type.tosca_type_name()}]::{self.field.as_ref_expr()}"
            }
            return projection


def field(
    *,
    default=dataclasses.MISSING,
    default_factory=dataclasses.MISSING,
    kw_only=dataclasses.MISSING,
):
    kw: Dict[str, Any] = dict(default=default, default_factory=default_factory)
    if sys.version_info.minor > 9:
        kw["kw_only"] = kw_only
        if default is REQUIRED:
            # we don't need this default placeholder set if Python supports kw_only fields
            kw["default"] = dataclasses.MISSING
    return dataclasses.field(**kw)


@dataclass_transform(
    kw_only_default=True,
    field_specifiers=(
        Attribute,
        Property,
        Capability,
        Requirement,
        Artifact,
        field,
        dataclasses.field,
    ),
)
class _ToscaType(ToscaObject, metaclass=_DataclassType):
    # we need this intermediate type because the base class with the @dataclass_transform can't specify fields

    explicit_tosca_fields = _Tosca_Fields_Getter()  # list of _ToscaFields
    set_config_spec_args = _Set_ConfigSpec_Method()

    @classmethod
    def get_field_from_tosca_name(
        cls, tosca_name, tosca_type: ToscaFieldType
    ) -> Optional[_Tosca_Field]:
        for field in cls.__dataclass_fields__.values():  # type: ignore
            if (
                isinstance(field, _Tosca_Field)
                and field.tosca_name == tosca_name
                and field.tosca_field_type == tosca_type
            ):
                return field
        return None

    if not typing.TYPE_CHECKING:

        def __getattribute__(self, name: str):
            # the only times we want the actual value of a tosca field returned
            # is during yaml generation or directly executing a plan
            # but when constructing a topology return absolute Refs to fields instead
            if global_state.mode == "spec":
                fields = object.__getattribute__(self, "__dataclass_fields__")
                field = fields.get(name)
                if field and isinstance(field, _Tosca_Field):
                    return _Ref(dict(eval=f"::{self._name}::{field.as_ref_expr()}"))
            val = object.__getattribute__(self, name)
            if isinstance(val, _ToscaType):
                val._set_parent(self, name)
            return val

    @classmethod
    def _class_set_config_spec(cls, kw: dict, target) -> dict:
        return kw

    def _set_config_spec_(self, kw: dict, target) -> dict:
        return self._class_set_config_spec(kw, target)

    def _set_parent(self, parent: "_ToscaType", name: str):
        pass

    _namespace: ClassVar[Optional[Dict[str, Any]]] = None
    _globals: ClassVar[Optional[Dict[str, Any]]] = None
    _docstrings: Optional[Dict[str, str]] = dataclasses.field(
        default=None, init=False, repr=False
    )

    @classmethod
    def _resolve_class(cls, _type):
        origin = get_origin(_type)
        if origin in [Annotated, list, collections.abc.Sequence]:
            _type = get_args(_type)[0]
        if isinstance(_type, str):
            if "[" in _type:
                match = re.search(r"\[(\w+)\]", _type)
                if match and match.group(1):
                    _type = match.group(1)
                else:
                    raise NameError(f"invalid type annotation: {_type}")
            return cls._lookup_class(_type)
        elif isinstance(_type, ForwardRef):
            return cls._resolve_class(_type.__forward_arg__)
        else:
            return _type

    @classmethod
    def _lookup_class(cls, qname: str):
        names = qname.split(".")
        name = names.pop(0)
        if cls._globals:
            globals = cls._globals
        elif cls.__module__ and cls.__module__ != "builtins":
            globals = sys.modules[cls.__module__].__dict__
        else:
            globals = {}
        locals = cls._namespace or {}
        obj = locals.get(name, globals.get(name))
        if obj is None:
            raise NameError(f"{qname} not found in {cls}'s scope")
        while names:
            name = names.pop(0)
            ns = obj
            obj = getattr(obj, name, None)
            if obj is None:
                raise AttributeError(f"can't find {name} in {qname}")
        return obj

    @classmethod
    def tosca_bases(cls, section=None) -> Iterator[str]:
        for c in cls.__bases__:
            # only include classes of the same tosca type as this class
            # and exclude the base class defined in this module
            if issubclass(c, ToscaType):
                if c._type_section == (section or cls._type_section) and c.__module__ != __name__:  # type: ignore
                    yield c.tosca_type_name()


class ToscaInputs(_ToscaType):
    @classmethod
    def _shared_cls_to_yaml(cls, converter: Optional["PythonToYaml"]) -> dict:
        dict_cls = converter and converter.yaml_cls or yaml_cls
        body: Dict[str, Any] = dict_cls()
        for field in cls.explicit_tosca_fields:
            assert field.name, field
            item = field.to_yaml(converter)
            body.update(item)
        return body

    @staticmethod
    def _get_inputs(*args: "ToscaInputs", **kw):
        inputs = yaml_cls()
        for arg in args:
            assert isinstance(arg, ToscaInputs), arg
            for field in arg.__dataclass_fields__.values():
                if isinstance(field, _Tosca_Field):
                    val = getattr(arg, field.name, dataclasses.MISSING)
                    if val != dataclasses.MISSING and val != REQUIRED:
                        if val is not None or field.default is REQUIRED:
                            # only set field with None if the field is required
                            if val != field.default:
                                # don't set if matches default
                                inputs[field.tosca_name] = val
        inputs.update(kw)
        return inputs


class ToscaOutputs(_ToscaType):
    pass


class ToscaType(_ToscaType):
    # _name needs to come first for python < 3.10
    _name: str = field(default="", kw_only=False)
    _type_name: ClassVar[str] = ""
    _type_section: ClassVar[str] = ""
    _template_section: ClassVar[str] = ""

    _type_metadata: ClassVar[Optional[Dict[str, str]]] = None
    _metadata: Dict[str, str] = dataclasses.field(default_factory=dict)
    _interface_requirements: Optional[List[str]] = dataclasses.field(
        default=None, init=False, repr=False
    )

    # XXX version (type and template?)

    def __set_name__(self, owner, name):
        # called when a template is declared as a default value (owner will be class)
        if not self._name:
            self._name = owner.__name__ + "_" + name

    @classmethod
    def set_source(cls, requirement: Any, reference: Any) -> None:
        """
        Create a constraint that sets a requirement to the source of the property reference.

        Should be called from ``_set_constraints(cls)``. For example,

        ``cls._set_source(cls.requirement, cls.property)``

        is equivalent to:

        ``cls.requirement = cls.property``

        But this method can be used to avoid static type checker complaints with that assignment.
        """
        if isinstance(requirement, FieldProjection):
            requirement = requirement.field
        if isinstance(requirement, _Tosca_Field):
            if requirement.owner != cls:
                raise ValueError(f"Field {requirement} isn't owned by {cls}")
            return requirement.set_constraint(reference)
        raise TypeError(
            f"{requirement} isn't a TOSCA field -- this method should be called from _set_constraints()"
        )

    @staticmethod
    def _interfaces_yaml(cls_or_self, cls, dict_cls) -> Dict[str, dict]:
        # interfaces are inherited
        interfaces = {}
        interface_ops = {}
        direct_bases = []
        for c in cls.__mro__:
            if not issubclass(c, ToscaType) or c._type_section != "interface_types":
                continue
            name = c.tosca_type_name()
            shortname = name.split(".")[-1]
            i_def: Dict[str, Any] = {}
            if shortname not in [
                "Standard",
                "Configure",
                "Install",
            ] or cls_or_self.tosca_type_name().startswith("tosca."):
                # built-in interfaces don't need their type declared
                i_def["type"] = name
            if cls_or_self._interface_requirements:
                i_def["requirements"] = cls_or_self._interface_requirements
            interfaces[shortname] = i_def
            if c in cls.__bases__:
                direct_bases.append(shortname)
            for methodname in c.__dict__:
                if methodname[0] == "_":
                    continue
                interface_ops[methodname] = i_def
                interface_ops[shortname + "." + methodname] = i_def
        cls_or_self._find_operations(cls_or_self, interface_ops, interfaces, dict_cls)
        # filter out interfaces with no operations declared unless inheriting the interface directly
        return dict_cls(
            (k, v)
            for k, v in interfaces.items()
            if k == "defaults" or k in direct_bases or v.get("operations")
        )

    @staticmethod
    def is_operation(operation) -> bool:
        # exclude Input and Output classes
        return callable(operation) and not isinstance(operation, _DataclassType)

    @staticmethod
    def _find_operations(cls_or_self, interface_ops, interfaces, dict_cls) -> None:
        for methodname, operation in cls_or_self.__dict__.items():
            if methodname[0] == "_":
                continue
            if cls_or_self.is_operation(operation):
                if hasattr(operation, "apply_to"):
                    apply_to = operation.apply_to
                    for name in apply_to:
                        interface = interface_ops.get(name)
                        if interface is not None:
                            # set to null to so they use the default operation
                            interface.setdefault("operations", {})[
                                name.split(".")[-1]
                            ] = None
                    interfaces["defaults"] = cls_or_self._operation2yaml(
                        cls_or_self, operation, dict_cls
                    )
                else:
                    name = getattr(operation, "operation_name", methodname)
                    interface = interface_ops.get(name)
                    if interface is not None:
                        interface.setdefault("operations", {})[
                            name
                        ] = cls_or_self._operation2yaml(
                            cls_or_self, operation, dict_cls
                        )

    @staticmethod
    def _operation2yaml(cls_or_self, operation, dict_cls):
        # XXX
        # signature = inspect.signature(operation)
        # for name, param in signature.parameters.items():
        #     inputs[name] = parameter_to_tosca(param)
        result = operation(_ToscaTypeProxy(cls_or_self))
        if result is None:
            return result
        if isinstance(result, _ArtifactProxy):
            implementation = dict_cls(primary=result.name_or_tpl)
        else:
            className = f"{result.__class__.__module__}.{result.__class__.__name__}"
            implementation = dict_cls(className=className)
        # XXX add to implementation: dependencies, invoke, preConditions
        for key in ("operation_host", "environment", "timeout"):
            impl_val = getattr(operation, key, None)
            if impl_val is not None:
                implementation[key] = impl_val
        op_def: Dict[str, Any] = dict_cls(
            implementation=to_tosca_value(implementation, dict_cls)
        )
        if result.inputs:
            op_def["inputs"] = to_tosca_value(result.inputs, dict_cls)
        description = getattr(operation, "__doc__", "")
        if description and description.strip():
            op_def["description"] = description.strip()
        # XXX add to op_def: outputs, entry_state
        return op_def

    @classmethod
    def _shared_cls_to_yaml(cls, converter: Optional["PythonToYaml"]) -> dict:
        # XXX version
        dict_cls = converter and converter.yaml_cls or yaml_cls
        body: Dict[str, Any] = dict_cls()
        tosca_name = cls.tosca_type_name()
        bases: Union[list, str] = [b for b in cls.tosca_bases() if b != tosca_name]
        if bases:
            if len(bases) == 1:
                bases = bases[0]
            body["derived_from"] = bases

        doc = cls.__doc__ and cls.__doc__.strip()
        if doc:
            body["description"] = doc
        if cls._type_metadata:
            body["metadata"] = metadata_to_yaml(cls._type_metadata)

        for field in cls.explicit_tosca_fields:
            assert field.name, field
            if cls._docstrings:
                field.description = cls._docstrings.get(field.name)
            item = field.to_yaml(converter)
            if field.section == "requirements":
                body.setdefault("requirements", []).append(item)
            else:  # properties, attribute, capabilities, artifacts
                body.setdefault(field.section, {}).update(item)
                if field.declare_attribute:
                    # a property that is also declared as an attribute
                    item = {field.tosca_name: field._to_attribute_yaml()}
                    body.setdefault("attributes", {}).update(item)

        if not converter or not converter.safe_mode:
            # safe mode skips adding interfaces because it executes operations to generate the yaml
            interfaces = cls._interfaces_yaml(cls, cls, dict_cls)
            if interfaces:
                body["interfaces"] = interfaces

        if not body:  # skip this
            return {}
        tpl = dict_cls({tosca_name: body})
        return tpl

    def to_yaml(self, dict_cls=dict):
        return self._name

    def get_fields(self):
        fields = object.__getattribute__(self, "__dataclass_fields__")
        for name, value in self.__dict__.items():
            field = fields.get(name)
            if isinstance(value, _Tosca_Field):
                # field assigned directly to the object
                field = value
                if field.default_factory is not dataclasses.MISSING:
                    value = field.default_factory()
                elif field.default is not dataclasses.MISSING:
                    value = field.default
                else:
                    continue
                yield field, value
            elif not field and name[0] != "_" and not callable(value):
                # attribute is not part of class definition, try to deduce from the value's type
                field = _Tosca_Field.infer_field(self.__class__, name, value)
                if not field:
                    # the value was a data value or unrecognized, nothing to convert
                    continue
                yield field, value
            elif isinstance(field, _Tosca_Field):
                yield field, value

    def to_template_yaml(self, converter: "PythonToYaml") -> dict:
        # XXX directives, metadata, everything else
        # TOSCA templates can add requirements, capabilities and operations that are not defined on the type
        # so we need to look for _ToscaFields and operation function in the object's __dict__ and generate yaml for them too
        dict_cls = converter.yaml_cls
        body = dict_cls(type=self.tosca_type_name())
        for field, value in self.get_fields():
            if field.section == "requirements":
                # XXX node_filter if value is ref
                # XXX handle case where value is a type not an instance
                req = dict_cls()
                shorthand = converter.set_requirement_value(
                    req, value, self._name + "_" + field.name
                )
                if shorthand or req:
                    body.setdefault("requirements", []).append(
                        {field.tosca_name: shorthand or req}
                    )
            elif field.section in ["capabilities", "artifacts"]:
                if value:
                    assert isinstance(value, (CapabilityType, ArtifactType))
                    if (
                        field.default_factory
                        and field.default_factory is not dataclasses.MISSING
                    ):
                        default_value = field.default_factory()
                    else:
                        default_value = field.default
                    if value._local_name:
                        compare = dataclasses.replace(
                            value, _local_name=None, _node=None  # type: ignore
                        )
                    else:
                        compare = value
                    if compare != default_value:
                        tpl = value.to_template_yaml(converter)
                        body.setdefault(field.section, {})[field.tosca_name] = tpl
            elif field.section in ["properties", "attributes"]:
                if field.default == value:
                    # XXX datatype values don't compare properly, should have logic like CapabilityType above
                    continue
                body.setdefault(field.section, {})[field.tosca_name] = to_tosca_value(
                    value
                )

        if not converter.safe_mode:
            # safe mode skips adding interfaces because it executes operations to generate the yaml
            # this only adds interfaces defined directly on this object
            interfaces = self._interfaces_yaml(self, self.__class__, converter.yaml_cls)
            if interfaces:
                body["interfaces"] = interfaces

        return body

    def load_class(self, module_path: str, class_name: str):
        from unfurl.util import load_module

        current_mod = sys.modules[self.__class__.__module__]
        assert current_mod.__file__
        path = os.path.join(os.path.dirname(current_mod.__file__), module_path)
        loaded = load_module(path)
        return getattr(loaded, class_name)


class NodeType(ToscaType):
    _type_section: ClassVar[str] = "node_types"
    _template_section: ClassVar[str] = "node_templates"

    _directives: List[str] = dataclasses.field(default_factory=list)
    "List of this node template's TOSCA directives"

    @classmethod
    def _cls_to_yaml(cls, converter: "PythonToYaml") -> dict:
        yaml = cls._shared_cls_to_yaml(converter)
        # XXX add artifacts
        return yaml

    def find_artifact(self, name_or_tpl) -> Optional["ArtifactType"]:
        # XXX
        return None


class _OwnedToscaType(ToscaType):
    _local_name: Optional[str] = field(default=None)
    _node: Optional[NodeType] = field(default=None)

    def _set_parent(self, parent: "_ToscaType", name: str):
        # only set once
        if not self._local_name and isinstance(parent, NodeType):
            self._node = parent
            self._local_name = name


class DataType(_OwnedToscaType):
    _type_section: ClassVar[str] = "data_types"
    _type: ClassVar[Optional[str]] = None
    _constraints: ClassVar[Optional[List[dict]]] = None

    @classmethod
    def _cls_to_yaml(cls, converter: Optional["PythonToYaml"]) -> dict:
        yaml = cls._shared_cls_to_yaml(converter)
        if cls._type:
            yaml[cls.tosca_type_name()]["type"] = cls._type
        if cls._constraints:
            yaml[cls.tosca_type_name()]["constraints"] = cls._constraints
        return yaml

    @classmethod
    def get_tosca_datatype(cls):
        custom_defs = cls._cls_to_yaml(None)
        return ToscaDataType(cls.tosca_type_name(), custom_defs)

    @classmethod
    def _get_property_metadata(cls) -> Optional[Dict[str, Any]]:
        return None

    def to_yaml(self, dict_cls=dict):
        body = dict_cls()
        for field, value in self.get_fields():
            body[field.tosca_name] = to_tosca_value(value, dict_cls)
        return body


class OpenDataType(DataType):
    "Properties don't need to be declared with TOSCA data types derived from this class."

    _type_metadata = dict(additionalProperties=True)  # type: ignore

    def __init__(self, _name="", **kw):
        for k in list(kw):
            if k[0] != "_":
                self.__dict__[k] = kw.pop(k)
        super().__init__(_name, **kw)


class CapabilityType(_OwnedToscaType):
    _type_section: ClassVar[str] = "capability_types"

    @classmethod
    def _cls_to_yaml(cls, converter: "PythonToYaml") -> dict:
        return cls._shared_cls_to_yaml(converter)

    def to_template_yaml(self, converter: "PythonToYaml") -> dict:
        tpl = super().to_template_yaml(converter)
        del tpl["type"]
        return tpl


class RelationshipType(ToscaType):
    _type_section: ClassVar[str] = "relationship_types"
    _template_section: ClassVar[str] = "relationship_templates"

    _valid_target_types: ClassVar[Optional[List[str]]] = None
    _default_for: Optional[str] = field(default=None)
    _target: Optional[Union[NodeType, CapabilityType]] = field(default=None)

    @classmethod
    def _cls_to_yaml(cls, converter: "PythonToYaml") -> dict:
        yaml = cls._shared_cls_to_yaml(converter)
        # don't include inherited declarations
        _valid_target_types = cls.__dict__.get("_valid_target_types")
        if _valid_target_types:
            target_types = [
                cls._resolve_class(t).tosca_type_name() for t in _valid_target_types
            ]
            yaml[cls.tosca_type_name()]["valid_target_types"] = target_types
        return yaml

    def __set_name__(self, owner, name):
        pass  # override super implementation -- we don't want to set the name, indicate this is inline

    def to_template_yaml(self, converter: "PythonToYaml") -> dict:
        tpl = super().to_template_yaml(converter)
        if self._default_for:
            tpl["default_for"] = self._default_for
        return tpl

    def __getitem__(self, target: Union[NodeType, CapabilityType]) -> Self:
        if self._target:
            return dataclasses.replace(self, _target=target)  # type: ignore
        self._target = target
        return self


class ArtifactType(_OwnedToscaType):
    _type_section: ClassVar[str] = "artifact_types"
    _mime_type: ClassVar[Optional[str]] = None
    _file_ext: ClassVar[Optional[List[str]]] = None
    file: str = field(default=REQUIRED)
    repository: Optional[str] = field(default=None)
    # XXX
    # deploy_path
    # version
    # checksum
    # checksum_algorithm

    @classmethod
    def _cls_to_yaml(cls, converter: "PythonToYaml") -> dict:
        yaml = cls._shared_cls_to_yaml(converter)
        if cls._mime_type:
            yaml[cls.tosca_type_name()]["mime_type"] = cls._mime_type
        if cls._file_ext:
            yaml[cls.tosca_type_name()]["file_ext"] = cls._file_ext
        return yaml

    def to_template_yaml(self, converter: "PythonToYaml") -> dict:
        tpl = super().to_template_yaml(converter)
        tpl["file"] = self.file
        if self.repository:
            tpl["repository"] = self.repository
        return tpl

    def execute(self, *args: ToscaInputs, **kw):
        self.inputs = ToscaInputs._get_inputs(*args, **kw)
        return self


class InterfaceType(ToscaType):
    # "Note: Interface types are not derived from ToscaType"
    _type_section: ClassVar[str] = "interface_types"

    _type_metadata: ClassVar[Optional[Dict[str, str]]] = None

    @classmethod
    def _cls_to_yaml(cls, converter: "PythonToYaml") -> dict:
        body: Dict[str, Any] = converter.yaml_cls()
        tosca_name = cls.tosca_type_name()
        for name, obj in cls.__dict__.items():
            if name[0] != "_" and cls.is_operation(obj):
                doc = obj.__doc__ and obj.__doc__.strip()
                if doc:
                    op = converter.yaml_cls(description=doc)
                else:
                    op = None
                # body[obj.__name__] = op
                body.setdefault("operations", converter.yaml_cls())[obj.__name__] = op
            elif isinstance(obj, _DataclassType) and issubclass(obj, ToscaInputs):
                body["inputs"] = obj._shared_cls_to_yaml(converter)
        yaml = cls._shared_cls_to_yaml(converter)
        if not yaml:
            if not body:
                return yaml
            yaml = {tosca_name: body}
        else:
            yaml[tosca_name].pop("interfaces", None)
            yaml[tosca_name].update(body)
        return yaml


class PolicyType(ToscaType):
    _type_section: ClassVar[str] = "policy_types"
    _template_section: ClassVar[str] = "policies"

    @classmethod
    def _cls_to_yaml(cls, converter: "PythonToYaml") -> dict:
        return cls._shared_cls_to_yaml(converter)


class GroupType(ToscaType):
    _type_section: ClassVar[str] = "group_types"
    _template_section: ClassVar[str] = "groups"

    @classmethod
    def _cls_to_yaml(cls, converter: "PythonToYaml") -> dict:
        return cls._shared_cls_to_yaml(converter)


class _ArtifactProxy:
    def __init__(self, name_or_tpl):
        self.name_or_tpl = name_or_tpl

    def execute(self, *args: ToscaInputs, **kw):
        self.inputs = ToscaInputs._get_inputs(*args, **kw)
        return self

    def to_yaml(self, dict_cls=dict) -> Optional[Dict]:
        return dict_cls(get_artifact=["SELF", self.name_or_tpl])


class _ToscaTypeProxy:
    """
    Stand-in for ToscaTypes when generating yaml
    """

    def __init__(self, cls):
        self.proxy_cls = cls

    def __getattr__(self, name):
        attr = getattr(self.proxy_cls, name)
        if isinstance(attr, FieldProjection):
            # _FieldDescriptor.__get__ returns a FieldProjection
            if isinstance(attr.field.default, ArtifactType):
                return _ArtifactProxy(name)
            else:
                # this is only called when defining an operation on a type so reset query to be relative
                attr.expr = {"eval": f".::{attr.field.as_ref_expr()}"}
        return attr

    def find_artifact(self, name_or_tpl):
        return _ArtifactProxy(name_or_tpl)


class WritePolicy(Enum):
    older = "older"
    never = "never"
    always = "always"
    auto = "auto"

    def deny_message(self) -> str:
        if self == WritePolicy.auto:
            return 'overwrite policy is "auto" and the file was last modified by another process'
        if self == WritePolicy.never:
            return 'overwrite policy is "never" and the file already exists'
        elif self == WritePolicy.older:
            return (
                'overwrite policy is "older" and the file is newer than the source file'
            )
        else:
            return ""

    def generate_comment(self, processor: str, path: str) -> str:
        return f'# Generated by {processor} from {os.path.relpath(path)} at {datetime.datetime.now().isoformat("T", "seconds")} overwrite not modified (change to "ok" to allow)\n'

    def can_overwrite(self, input_path: str, output_path: str):
        if self == WritePolicy.always:
            return True
        if self == WritePolicy.never:
            return not os.path.exists(output_path)
        elif self == WritePolicy.older:
            # only overwrite if the output file is older than the input file
            return not is_newer_than(output_path, input_path)
        else:  # auto
            # if this file is autogenerated, parse out the modified time and make sure it matches
            if not os.path.exists(output_path):
                return True
            with open(output_path) as out:
                contents = out.read()
                match = re.search(
                    r"# Generated by .+? at (\S+) overwrite (ok)?", contents
                )
                if not match:
                    return False
                if match.group(2):
                    return True  # ok!
                time = datetime.datetime.fromisoformat(match.group(1)).timestamp()
            if abs(time - os.stat(output_path).st_mtime) < 1:
                return True
            return False


def is_newer_than(output_path, input_path):
    "Is output_path newer than input_path?"
    if not os.path.exists(input_path) or not os.path.exists(output_path):
        return True  # assume that if it doesn't exist yet its definitely newer
    if os.stat(output_path).st_mtime_ns > os.stat(input_path).st_mtime_ns:
        return True
    return False
