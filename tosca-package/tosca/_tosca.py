# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
from abc import ABC
import collections.abc
from contextlib import contextmanager
import dataclasses
from enum import Enum, auto
import functools
import inspect
import json
from pathlib import Path
import typing
import os.path
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    ForwardRef,
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
)
import sys

from toscaparser import topology_template
from toscaparser.elements.datatype import DataType as ToscaDataType
from .scalars import *

# XXX
# __all__ = [
#     "Root"
# ]

yaml_cls = dict


class ToscaObject:
    _tosca_name: str = ""

    @classmethod
    def tosca_type_name(cls) -> str:
        _tosca_name = cls.__dict__.get("_tosca_name")
        return _tosca_name if _tosca_name else cls.__name__

    def to_yaml(self, dict_cls=dict) -> Optional[Dict]:
        return None


class _Constraint(ToscaObject):
    def __init__(self, constraint):
        self.constraint = constraint

    def to_yaml(self, dict_cls=dict) -> Optional[Dict]:
        return {self.__class__.__name__: to_tosca_value(self.constraint)}


class equal(_Constraint):
    pass


class greater_than(_Constraint):
    pass


class greater_or_equal(_Constraint):
    pass


class less_than(_Constraint):
    pass


class less_or_equal(_Constraint):
    pass


class in_range(_Constraint):
    pass


class valid_values(_Constraint):
    pass


class length(_Constraint):
    pass


class min_length(_Constraint):
    pass


class max_length(_Constraint):
    pass


class pattern(_Constraint):
    pass


class schema(_Constraint):
    pass


class Namespace(types.SimpleNamespace):
    @classmethod
    def get_defs(cls):
        ignore = ("__doc__", "__module__")
        obj = cls.__dict__
        # obj._namespace = cls.__dict__
        return {k: v for k, v in cls.__dict__.items() if k not in ignore}

    location: str


# XXX operation_host, environment, timeout, dependencies, invoke, preConditions
def operation(name="", apply_to=()):
    def decorator_operation(func):
        func.operation_name = name or func.__name__
        func.apply_to = apply_to
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
        return {k: to_tosca_value(v, dict_cls) for k, v in obj.items()}
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
        declare_attribute: bool = False,
        owner: Optional[Type["_ToscaType"]] = None,
    ):
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
        # its a concrete value or its a ref and the field the is property or attribute
        # either way, set the default
        # XXX validate that val is compatible type
        # XXX mark default as a constraint
        # XXX default is shared across template instances and subtypes -- what about mutable values like dicts and basically all Toscatypes?
        self.default = val

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

    def add_node_filter(self, val, prop_name: Optional[str] = None):
        assert self._tosca_field_type == ToscaFieldType.requirement
        if self.node_filter is None:
            self.node_filter = {}
        if prop_name:
            prop_filters = self.node_filter.setdefault("properties", [])
            if not isinstance(val, _Ref):
                val = {
                    "q": val
                }  # quote the value to distinguish from built-in tosca node_filters
            prop_filters.append({prop_name: val})
        elif isinstance(val, _Ref):
            self.node_filter = dict(match=val)
        else:
            # val is concrete value, just replace the default
            # XXX we can be smarter based on val type, e.g. node or relationship template and merge with the existing default values
            # XXX validate that val is compatible type
            # XXX mark default as a constraint
            self.default = val

    def as_ref_expr(self) -> str:
        if self._tosca_field_type in [
            ToscaFieldType.property,
            ToscaFieldType.attribute,
        ]:
            return ".::" + self.tosca_name
        elif self._tosca_field_type == ToscaFieldType.requirement:
            return ".targets::" + self.tosca_name
            # but if accessing the relationship template, need to use the form below
        else:
            assert self.tosca_field_type
            return f".{self.tosca_field_type.name}::[.name={self.tosca_name}]"

    def _resolve_class(self, _type):
        assert self.owner, (self, _type)
        return self.owner._resolve_class(_type)

    def get_type_info(self) -> TypeInfo:
        type_info = pytype_to_tosca_type(self.type)
        types = tuple(self._resolve_class(t) for t in type_info.types)
        return type_info._replace(types=types)

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

    def to_yaml(self, dict_cls=dict) -> dict:
        if self.tosca_field_type == ToscaFieldType.property:
            field_def = self._to_property_yaml()
        elif self.tosca_field_type == ToscaFieldType.attribute:
            field_def = self._to_attribute_yaml()
        elif self.tosca_field_type == ToscaFieldType.requirement:
            field_def = self._to_requirement_yaml()
        elif self.tosca_field_type == ToscaFieldType.capability:
            field_def = self._to_capability_yaml()
        else:
            assert False
        # note: description needs to be set when parsing ast
        if self.description:
            field_def["description"] = self.description
        if self.metadata:
            field_def["metadata"] = metadata_to_yaml(self.metadata)
        return {self.tosca_name: field_def}

    def _add_occurrences(self, field_def: dict, info: TypeInfo) -> None:
        occurrences = [1, 1]
        if info.optional or self.default == ():
            occurrences[0] = 0
        if info.collection is list:
            occurrences[1] = "UNBOUNDED"  # type: ignore

        if occurrences != [1, 1]:
            field_def["occurrences"] = occurrences

    def _to_requirement_yaml(self) -> Dict[str, Any]:
        req_def = {}
        info = self.get_type_info()
        for _type in info.types:
            if issubclass(_type, RelationshipType):
                req_def["relationship"] = _type.tosca_type_name()
            elif issubclass(_type, CapabilityType):
                req_def["capability"] = _type.tosca_type_name()
            elif issubclass(_type, NodeType):
                req_def["node"] = _type.tosca_type_name()
        # XXX set node or relationship name if defaultValue is node or relationship template
        self._add_occurrences(req_def, info)
        return req_def

    def _to_capability_yaml(self) -> Dict[str, Any]:
        info = self.get_type_info()
        assert len(info.types) == 1
        _type = info.types[0]
        assert issubclass(_type, _ToscaType), (self, _type)
        cap_def: dict = dict(type=_type.tosca_type_name())
        self._add_occurrences(cap_def, info)

        if self.valid_source_types:  # is not None: XXX only set to [] if declared
            cap_def["valid_source_types"] = self.valid_source_types
        return cap_def

    def pytype_to_tosca_schema(self, _type) -> Tuple[dict, bool]:
        info = pytype_to_tosca_type(_type)
        assert len(info.types) == 1, info
        _type = info.types[0]
        if info.collection is dict:
            tosca_type = "map"
        elif info.collection is list:
            tosca_type = "list"
        else:
            _type = self._resolve_class(_type)
            tosca_type = PYTHON_TO_TOSCA_TYPES.get(_get_type_name(_type), "")
            if not tosca_type:  # it must be a datatype
                tosca_type = _type.tosca_type_name()
        schema: Dict[str, Any] = dict(type=tosca_type)
        if info.collection:
            schema["entry_schema"] = self.pytype_to_tosca_schema(_type)[0]
        if info.metadata:
            schema["constraints"] = [c.to_yaml() for c in info.metadata]
        return schema, info.optional

    def _to_attribute_yaml(self) -> dict:
        # self.type is from __annotations__
        prop_def, optional = self.pytype_to_tosca_schema(self.type)
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


def Property(
    *,
    default=dataclasses.MISSING,
    default_factory=dataclasses.MISSING,
    name: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    title="",
    status="",
    attribute: bool = False,
) -> Any:
    field = _Tosca_Field(
        ToscaFieldType.property,
        default=default,
        default_factory=default_factory,
        name=name,
        metadata=metadata,
        title=title,
        status=status,
        declare_attribute=attribute,
    )
    return field


def Attribute(
    *,
    default=None,
    default_factory=dataclasses.MISSING,
    name: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    title="",
    status="",
    # attributes are excluded from __init__,
    # this tricks the static checker, see pep 681:
    init: Literal[False] = False,
) -> Any:
    field = _Tosca_Field(
        ToscaFieldType.attribute,
        default,
        default_factory,
        name,
        metadata,
        title,
        status,
    )
    field.title = title
    return field


def Requirement(
    *,
    default=dataclasses.MISSING,
    default_factory=dataclasses.MISSING,
    name: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    node_filter: Optional[Dict[str, Any]] = None,
) -> Any:
    field = _Tosca_Field(
        ToscaFieldType.requirement, default, default_factory, name, metadata
    )
    field.node_filter = node_filter
    return field


T = TypeVar("T")


def Capability(
    *,
    default=dataclasses.MISSING,
    factory=dataclasses.MISSING,
    name: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    valid_source_types: Optional[List[str]] = None,
    # init: Literal[False] = False,
) -> Any:
    field = _Tosca_Field(ToscaFieldType.capability, default, factory, name, metadata)
    field.valid_source_types = valid_source_types or []
    return field


# XXX Artifact()


class _Ref:
    def __init__(self, expr):
        self.expr = expr

    def __str__(self):
        return str(self.expr)

    def to_yaml(self, dict_cls=dict):
        return self.expr


def Ref(
    expr=None,
    *,
    default=dataclasses.MISSING,
    default_factory=dataclasses.MISSING,
    init=False,
    name: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Any:
    return _Ref(expr)


# XXX class RefList(Ref)


class FieldProjection(_Ref):
    "A reference to a tosca field or projection off a tosca field"
    # created by _DataclassTypeProxy, invoked via _set_constraint

    def __init__(self, field: _Tosca_Field, name: Optional[str]):
        # currently don't support projections that are requirements
        expr = field.as_ref_expr()
        if name:
            # XXX map to tosca name but we can't do this now because it might be too early to resolve the attribute's type
            expr += "::" + name
        super().__init__(dict(eval=expr))
        self.field = field
        self.name = name

    def __getattr__(self, name):
        # unfortunately _set_constraints is called during class construction type
        # so _resolve_class might not work with forward references defined in the same module
        # cls = self._resolve_class(self.field.type)
        ti = self.field.get_type_info()
        field = ti.types[0].tosca_names[name]  # XXX
        return FieldProjection(field, name)

        if self.name:
            try:
                ti = self.field.get_type_info()
                field = ti.types[0].tosca_names[name]  # XXX
                return FieldProjection(field, name)
            except:
                # couldn't resolve the type
                raise
                raise Exception(
                    f"Can't project {name} from {self}: Only one level of field projection currently supported"
                )
        return FieldProjection(self.field, name)

    def __setattr__(self, name, val):
        if name in  ["expr", "field", "name"]:
            object.__setattr__(self, name, val)
            return
        self.field.set_property_constraint(name, val)


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
        unsafe_hash=False,
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
    for name, type in cls.__dict__.get("__annotations__", {}).items():
        if name[0] != "_":
            field = None
            default = getattr(cls, name, dataclasses.MISSING)
            if not isinstance(
                default, dataclasses.Field
            ):  # XXX or not InitVar or ClassVar
                field = _Tosca_Field(None, default, owner=cls)
                setattr(cls, name, field)
            elif isinstance(default, _Tosca_Field):
                default.owner = cls
                field = default
            if field:
                field.name = name
                field.type = type
    _set_constraints = cls.__dict__.get("_set_constraints")
    if _set_constraints:
        _set_constraints.__get__(None, _DataclassTypeProxy(cls))()
    if not getattr(cls, "__doc__"):
        cls.__doc__ = " "  # suppress dataclass doc string generation
    # note: _process_class will replace each field with its default value (or delete the attribute)
    return dataclasses._process_class(cls, **kw)  # type: ignore


class _DataclassType(type):
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


class _Tosca_Names_Getter:
    def __get__(self, obj, objtype=None) -> Dict[str, _Tosca_Field]:
        return {f.tosca_name: f for f in (obj or objtype).tosca_fields}


class _Set_ConfigSpec_Method:
    def __get__(self, obj, objtype) -> Callable:
        if obj:
            return obj._set_config_spec_
        else:
            return objtype._class_set_config_spec


# Ref and RefList aren't actually field specifiers but they are listed here so static type checking works
@dataclass_transform(
    kw_only_default=True,
    field_specifiers=(Attribute, Property, Capability, Requirement, Ref),
)
class _ToscaType(ToscaObject, metaclass=_DataclassType):
    # we need this intermediate type because the base class with the @dataclass_transform can't specify fields

    tosca_fields = _Tosca_Fields_Getter()  # list of _ToscaFields
    tosca_names = _Tosca_Names_Getter()  # map of tosca names to _ToscaFields
    set_config_spec_args = _Set_ConfigSpec_Method()

    # def __getattribute__(self, name):
    #     # the only times we want the actual value of a tosca field returned
    #     # is during yaml generation or directly executing a plan
    #     # but when constructing a topology return Refs instead
    #     if self.__dict__['__mode']:
    #         field = self.as_ref(name)
    #         if field:
    #             return field
    #     return object.__getattribute__(self, name)

    @classmethod
    def _class_set_config_spec(cls, kw: dict, target) -> dict:
        return kw

    def _set_config_spec_(self, kw: dict, target) -> dict:
        return self._class_set_config_spec(kw, target)

    _namespace: Optional[Dict[str, Any]] = None
    _globals: Optional[Dict[str, Any]] = None
    _interface_requirements: Optional[List[str]] = None
    _docstrings: Optional[Dict[str, str]] = None

    @classmethod
    def _resolve_class(cls, _type):
        origin = get_origin(_type)
        if origin is Annotated:
            _type = get_args(_type)[0]
        if isinstance(_type, str):
            return cls._lookup_class(_type)
        elif isinstance(_type, ForwardRef):
            return cls._lookup_class(_type.__forward_arg__)
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
            raise NameError(f"{qname} not found")
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

    @classmethod
    def _interfaces_yaml(cls) -> Dict[str, dict]:
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
            ] or cls.tosca_type_name().startswith("tosca."):
                # built-in interfaces don't need their type declared
                i_def["type"] = name
            if cls._interface_requirements:
                i_def["requirements"] = cls._interface_requirements
            interfaces[shortname] = i_def
            if c in cls.__bases__:
                direct_bases.append(shortname)
            for methodname in c.__dict__:
                if methodname[0] == "_":
                    continue
                interface_ops[methodname] = i_def
                interface_ops[shortname + "." + methodname] = i_def
        cls._find_operations(interface_ops, interfaces)
        # filter out interfaces with no operations declared unless inheriting the interface directly
        return {
            k: v
            for k, v in interfaces.items()
            if k == "defaults" or k in direct_bases or v.get("operations")
        }

    @classmethod
    def _find_operations(cls, interface_ops, interfaces) -> None:
        for methodname, operation in cls.__dict__.items():
            if methodname[0] == "_":
                continue
            if callable(operation):
                if hasattr(operation, "apply_to"):
                    apply_to = operation.apply_to
                    for name in apply_to:
                        interface = interface_ops.get(name)
                        if interface is not None:
                            # set to null to so they use the default operation
                            interface.setdefault("operations", {})[
                                name.split(".")[-1]
                            ] = None
                    interfaces["defaults"] = cls._operation2yaml(operation)
                else:
                    name = getattr(operation, "operation_name", methodname)
                    interface = interface_ops.get(name)
                    if interface is not None:
                        interface.setdefault("operations", {})[
                            name
                        ] = cls._operation2yaml(operation)

    @classmethod
    def _operation2yaml(cls, operation):
        result = operation(_ToscaTypeProxy(cls))
        if result is None:
            return result
        if isinstance(result, _ArtifactProxy):
            implementation = dict(primary=result.name_or_tpl)
        else:
            className = f"{result.__class__.__module__}.{result.__class__.__name__}"
            implementation = dict(className=className)

        # XXX add to implementation: operation_host, environment, timeout, dependencies, invoke, preConditions
        op_def: Dict[str, Any] = {"implementation": implementation}
        if result.inputs:
            op_def["inputs"] = to_tosca_value(result.inputs)
        description = getattr(operation, "__doc__", "")
        if description and description.strip():
            op_def["description"] = description.strip()
        # XXX add to op_def: outputs, entry_state
        return op_def

    @classmethod
    def _shared_cls_to_yaml(cls) -> dict:
        # XXX _type_metadata, version

        body: Dict[str, Any] = {}
        tosca_name = cls.tosca_type_name()
        bases: Union[list, str] = [b for b in cls.tosca_bases() if b != tosca_name]
        if bases:
            if len(bases) == 1:
                bases = bases[0]
            body["derived_from"] = bases

        doc = cls.__doc__ and cls.__doc__.strip()
        if doc:
            body["description"] = doc

        for field in cls.tosca_fields:
            assert field.name, field
            if cls._docstrings:
                field.description = cls._docstrings.get(field.name)
            item = field.to_yaml()
            if field.section == "requirements":
                body.setdefault("requirements", []).append(item)
            else:  # properties, attribute, capabilities
                body.setdefault(field.section, {}).update(item)
                if field.declare_attribute:
                    # a property that is also declared as an attribute
                    item = {field.tosca_name: field._to_attribute_yaml()}
                    body.setdefault("attributes", {}).update(item)

        interfaces = cls._interfaces_yaml()
        if interfaces:
            body["interfaces"] = interfaces

        # XXX interfaces, operations
        if not body:  # skip this
            return {}
        tpl = {tosca_name: body}
        return tpl


class ToscaType(_ToscaType):
    _type_section: ClassVar[str] = ""
    _template_section: ClassVar[str] = ""

    _type_metadata: ClassVar[Optional[Dict[str, str]]] = None

    _metadata: Dict[str, str] = dataclasses.field(default_factory=dict)

    _name: str = ""

    # XXX version (type and template?)

    def to_yaml(self, dict_cls=dict) -> Optional[dict]:
        # XXX directives, metadata, everything else
        # TOSCA templates can add requirements, capabilities and operations that are not defined on the type
        # so we need to look for _ToscaFields and operation function in the object's __dict__ and generate yaml for them too
        return {self._name: dict(type=self.tosca_type_name())}

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

    @classmethod
    def _cls_to_yaml(cls) -> dict:
        yaml = cls._shared_cls_to_yaml()
        # XXX add artifacts
        return yaml

    def find_artifact(self, name_or_tpl) -> Optional["ArtifactType"]:
        # XXX
        return None


class DataType(ToscaType):
    _type_section: ClassVar[str] = "data_types"
    _type: ClassVar[Optional[str]] = None
    _constraints: ClassVar[Optional[List[dict]]] = None

    @classmethod
    def _cls_to_yaml(cls, dict_cls=dict) -> dict:
        yaml = cls._shared_cls_to_yaml()
        if cls._type:
            yaml[cls.tosca_type_name()]["type"] = cls._type
        if cls._constraints:
            yaml[cls.tosca_type_name()]["constraints"] = cls._constraints
        return yaml

    @classmethod
    def get_tosca_datatype(cls):
        custom_defs = cls._cls_to_yaml()
        return ToscaDataType(cls.tosca_type_name(), custom_defs)


class CapabilityType(ToscaType):
    _type_section: ClassVar[str] = "capability_types"

    @classmethod
    def _cls_to_yaml(cls) -> dict:
        return cls._shared_cls_to_yaml()


class RelationshipType(ToscaType):
    _type_section: ClassVar[str] = "relationship_types"
    _template_section: ClassVar[str] = "relationship_templates"

    _valid_target_types: ClassVar[Optional[List[str]]] = None
    _default_for: Optional[str] = None

    @classmethod
    def _cls_to_yaml(cls, dict_cls=dict) -> dict:
        yaml = cls._shared_cls_to_yaml()
        # XXX add interfaces
        # don't include inherited declarations
        _valid_target_types = cls.__dict__.get("_valid_target_types")
        if _valid_target_types:
            target_types = [
                cls._resolve_class(t).tosca_type_name() for t in _valid_target_types
            ]
            yaml[cls.tosca_type_name()]["valid_target_types"] = target_types
        return yaml


class ArtifactType(ToscaType):
    _type_section: ClassVar[str] = "artifact_types"
    _mime_type: ClassVar[Optional[str]] = None
    _file_ext: ClassVar[Optional[List[str]]] = None
    # XXX
    # file
    # repository
    # deploy_path
    # version
    # checksum
    # checksum_algorithm

    @classmethod
    def _cls_to_yaml(cls, dict_cls=dict) -> dict:
        yaml = cls._shared_cls_to_yaml()
        if cls._mime_type:
            yaml[cls.tosca_type_name()]["mime_type"] = cls._mime_type
        if cls._file_ext:
            yaml[cls.tosca_type_name()]["file_ext"] = cls._file_ext
        return yaml

    # def to_yaml(self, dict_cls=dict) -> Dict[str, Any]:
    #     yaml = super().to_yaml()
    #     typedef = yaml[self.tosca_type_name()]
    #     return yaml

    def execute(self, **kw):
        self.inputs = kw
        return self


class InterfaceType(ToscaType):
    # "Note: Interface types are not derived from ToscaType"
    _type_section: ClassVar[str] = "interface_types"

    _type_metadata: ClassVar[Optional[Dict[str, str]]] = None

    @classmethod
    def _cls_to_yaml(cls, dict_cls=dict) -> dict:
        body: Dict[str, Any] = {}
        tosca_name = cls.tosca_type_name()
        for name, obj in cls.__dict__.items():
            if name[0] != "_" and callable(obj):
                doc = obj.__doc__ and obj.__doc__.strip()
                if doc:
                    body[obj.__name__] = dict(description=doc)
                else:
                    body[obj.__name__] = None
        yaml = cls._shared_cls_to_yaml()
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
    def _cls_to_yaml(cls, dict_cls=dict) -> dict:
        return cls._shared_cls_to_yaml()


class GroupType(ToscaType):
    _type_section: ClassVar[str] = "group_types"
    _template_section: ClassVar[str] = "groups"

    @classmethod
    def _cls_to_yaml(cls, dict_cls=dict) -> dict:
        return cls._shared_cls_to_yaml()


class _ArtifactProxy:
    def __init__(self, name_or_tpl):
        self.name_or_tpl = name_or_tpl

    def execute(self, **kw):
        self.inputs = kw
        return self


class _ToscaTypeProxy:
    """
    Stand-in for ToscaTypes when generating yaml
    """

    def __init__(self, cls):
        self.proxy_cls = cls

    def __getattr__(self, name):
        # XXX check attribute in proxy_cls.__annotations__
        # including inherited
        #     # attribute access as Refs (refs need to chain)
        #     return _AttrRef(name)
        # for now, assume its an artifact
        return _ArtifactProxy(name)

    def find_artifact(self, name_or_tpl):
        return _ArtifactProxy(name_or_tpl)
