"""
Binding between Unfurl's runtime and the TOSCA Python DSL.
"""
#  We need a way to map class and templates in defined in the Python DSL to the instances
#  created when running a job and have the DSL objects reflect those values.
#
# Design constraints

# * class and template may exist for types and templates defined in yaml but haven't been loaded
# * tosca names don't necessarily map to a module
# * untrusted python code needs to be partitioned so definitions don't intermingle
# * we only need to register class and template when running jobs (which requires trust)
# * util.register_class is global so will not be partitioned but is only used when running jobs (which requires trust)

# Implementation

# * don't register types and templates in safe mode
# * template names need to be per module (assumes module maps to a topology)
# * instead of register configurator class just generate key that maps to function on the "class: module:class.method"
# * ConfiguratorSpec.create() uses that key to create a DslMethodConfigurator
# * use the same mechanism to enable calling methods on objs and expose that as a "computed" expression function.
# * TODO? special case tosca and unfurl packages to load modules as needed

import functools
import inspect
import os
from pathlib import Path
import importlib
import types
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    ForwardRef,
    Generic,
    List,
    MutableMapping,
    MutableSequence,
    Optional,
    Type,
    TypeVar,
    cast,
    TYPE_CHECKING,
    Tuple,
)

import tosca
from tosca import InstanceProxy, ToscaType, DataType, ToscaFieldType, TypeInfo
from tosca._tosca import _Tosca_Field, _ToscaType, ComputedDescriptor
from toscaparser.elements.portspectype import PortSpec
from toscaparser.nodetemplate import NodeTemplate
from .eval import set_eval_func
from .result import Results
from .runtime import EntityInstance, NodeInstance, RelationshipInstance
from .spec import EntitySpec, NodeSpec, RelationshipSpec
from .repo import RepoView
from .util import check_class_registry, get_base_dir, register_class
from tosca.python2yaml import python_src_to_yaml_obj, WritePolicy
from unfurl.configurator import Configurator
from typing_extensions import (
    dataclass_transform,
    get_args,
    get_origin,
    Annotated,
    Literal,
    Self,
    get_type_hints,
)
import sys
import logging
import toscaparser.properties
import toscaparser.capabilities
from toscaparser.entity_template import EntityTemplate
from toscaparser.nodetemplate import NodeTemplate

if TYPE_CHECKING:
    from .yamlloader import ImportResolver


def convert_to_yaml(
    import_resolver: "ImportResolver",
    contents: str,
    path: str,
    repo_view: Optional[RepoView],
    base_dir: str,
) -> dict:
    from .yamlloader import yaml_dict_type

    import_resolver.expand = False
    namespace: Dict[str, Any] = {}
    if repo_view and repo_view.repository:
        package_path = Path(get_base_dir(path)).relative_to(repo_view.working_dir)
        relpath = str(package_path).strip("/").replace("/", ".")
        if repo_view.repository.name == "unfurl":
            package = "unfurl."
        else:
            # make sure tosca_repository symlink exists
            name = repo_view.get_link(base_dir)[0]
            package = "tosca_repository." + name
    else:
        package_path = Path(get_base_dir(path)).relative_to(base_dir)
        relpath = str(package_path).replace("/", ".").strip(".")
        package = "service_template"
    if relpath:
        package += "." + relpath
    if import_resolver.manifest.modules is None:
        import_resolver.manifest.modules = {}
    safe_mode = import_resolver.get_safe_mode()
    write_policy = WritePolicy[os.getenv("UNFURL_OVERWRITE_POLICY") or "never"]
    module_name = package + "." + Path(path).stem
    yaml_src = python_src_to_yaml_obj(
        contents,
        namespace,
        base_dir,
        module_name,
        # can't use readonly yaml since we might write out yaml files with it
        yaml_dict_type(False),
        safe_mode,
        import_resolver.manifest.modules,
        write_policy,
        import_resolver,
    )
    return yaml_src


def find_template(template: EntitySpec) -> Optional[ToscaType]:
    if isinstance(template, NodeSpec):
        section = "node_templates"
    elif isinstance(template, RelationshipSpec):
        section = "relationship_templates"
    else:
        return None
    metadata = template.toscaEntityTemplate.entity_tpl.get("metadata")
    module_name = metadata and metadata.get("module")
    if module_name and ToscaType._all_templates:
        return ToscaType._all_templates[section].get((module_name, template.name))  # type: ignore
    return None


def proxy_instance(instance, cls: Type[_ToscaType]):
    obj = find_template(instance.template)
    if obj:
        found_cls: Optional[Type[_ToscaType]] = obj.__class__
    else:
        # if target is subtype of cls, use the subtype
        found_cls = cls._all_types.get(instance.template.type)
    return get_proxy_class(found_cls or cls)(instance, obj)


class DslMethodConfigurator(Configurator):
    def __init__(self, cls: Type[ToscaType], fun: Callable):
        self.cls = cls
        self.fun = fun

    def run(self, task):
        obj = proxy_instance(task.target, self.cls)
        return self.fun(obj, task)

def eval_computed(arg, ctx):
    """
    eval:
       computed: mod:class.computed_property
    """
    module_name, sep, qualname = arg.partition(":")
    module = importlib.import_module(module_name)
    cls_name, sep, func_name = qualname.rpartition(".")
    cls = getattr(module, cls_name)
    func = getattr(cls, func_name)
    proxy = proxy_instance(ctx.currentResource, cls)
    if isinstance(func, ComputedDescriptor):
        return func.func(proxy)
    return func(proxy)


set_eval_func("computed", eval_computed)


class ProxyCollection:
    def __init__(self, collection, field: _Tosca_Field):
        self._values = collection
        self._field = field
        self._cache: Dict[str, Any] = {}

    def __getitem__(self, key):
        if key in self._cache:
            return self._cache[key]
        val = self._values[key]
        type_info = self._field.get_type_info()
        prop_type = type_info.types[0]
        if issubclass(prop_type, tosca.DataType):
            proxy = get_proxy_class(prop_type, DataTypeProxyBase)(val)
            self._cache[key] = proxy
            return proxy
        return val

    def __setitem__(self, key, val):
        # unwrap proxied datatypes
        if isinstance(val, (DataTypeProxyBase, ProxyCollection)):
            self._cache[key] = val
            val = val._values
        else:
            self._cache.pop(key, None)
        self._values[key] = val

    def __delitem__(self, key):
        del self._values[key]
        self._cache.pop(key, None)

    def __len__(self):
        return len(self._values)

    def __getattr__(self, name):
        return getattr(self._values, name)


class ProxyMap(ProxyCollection, MutableMapping):
    def __iter__(self):
        return iter(self._values)


class ProxyList(ProxyCollection, MutableSequence):
    def insert(self, index, val):
        # unwrap proxied datatypes
        if isinstance(val, (DataTypeProxyBase, ProxyCollection)):
            val = val._values
        elif isinstance(val, tosca.DataType):
            val = val.__dict__
        self._values.insert(index, val)


def _proxy_prop(field: _Tosca_Field, value: Any):
    type_info = field.get_type_info()
    prop_type = type_info.types[0]
    if type_info.collection == dict:
        return ProxyMap(value, field)
    elif type_info.collection == list:
        return ProxyList(value, field)
    elif issubclass(prop_type, tosca.DataType):
        if isinstance(value, (type(None), prop_type)):
            return value
        elif isinstance(value, MutableMapping):
            return get_proxy_class(prop_type, DataTypeProxyBase)(value)
        else:
            raise ValueError("can't convert to {prop_type} on {field.name}")
    return value


PT = TypeVar("PT", bound="ToscaType")


class InstanceProxyBase(InstanceProxy, Generic[PT]):
    """
    Stand-in for a ToscaType template during runtime that proxy values from the instance.
    """

    _cls: Type[PT]

    def __init__(self, instance: EntityInstance, obj: Optional[PT] = None):
        self._instance = instance
        # templates can have attributes assigned directly to it so see if we have the template
        self._obj = obj
        self._cache: Dict[str, Any] = {}

    def _get_field(self, name):
        if self._obj:
            return self._obj.get_field(name)
        else:
            return self._cls.__dataclass_fields__.get(name)

    def __str__(self):
        if self._obj:
            return str(self._obj)
        else:
            return self._cls.__str__(self)  # type: ignore

    if not TYPE_CHECKING:

        def __getattr__(self, name):
            return self._getattr(name)

    def _proxy_requirement(
        self, field: _Tosca_Field, type_info: TypeInfo, rel: RelationshipInstance
    ) -> Optional["InstanceProxyBase"]:
        # choose the right type to proxy from the field.type (if union)
        req_spec = cast(RelationshipSpec, rel.template).requirement
        has_relationship_template = req_spec and req_spec.has_relationship_template()
        instance: Optional[EntityInstance] = None
        reltype = None
        captype = None
        nodetype = None
        for t in type_info.types:
            if issubclass(t, tosca.RelationshipType):
                reltype = t
            elif issubclass(t, tosca.CapabilityType):
                captype = t
            elif issubclass(t, tosca.NodeType):
                nodetype = t
        if has_relationship_template and reltype:
            instance = rel
            cls = reltype
        elif captype:
            instance = rel.capability
            cls = captype
        else:
            instance = rel.target
            cls = nodetype
        if instance:
            return proxy_instance(instance, cls)
        return None

    def _proxy_requirements(
        self, field: _Tosca_Field, rels: List[RelationshipInstance]
    ):
        type_info = field.get_type_info()
        proxies = list(
            filter(
                None, [self._proxy_requirement(field, type_info, rel) for rel in rels]
            )
        )
        if len(proxies) <= 1 and not type_info.collection:
            val = proxies[0] if proxies else None
        else:
            val = proxies  # type: ignore
        self._cache[field.name] = val
        return val

    def __setattr__(self, name, val):
        #  you should be able to modify the dsl at this point (there probably isn't even an object to modify)
        #  so only modify the instance
        if name in ("_instance", "_obj", "_cache"):
            object.__setattr__(self, name, val)
            return
        if self._obj is not None and hasattr(self._obj, name):
            setattr(self._obj, name, val)
        if isinstance(val, (DataTypeProxyBase, ProxyCollection)):
            val = val._values
        self._instance.attributes[name] = val
        self._cache.pop(name, None)

    def _getattr(self, name):
        # if the attribute refers to a instance field, return the value from the instance
        # otherwise try to get the value from the obj, if set, or from the cls
        if name in self._cache:
            return self._cache[name]
        field = self._get_field(name)
        if not field:
            if name in ["_name", "_metadata", "_directives"]:
                # XXX other special attributes
                return getattr(self._instance.template.toscaEntityTemplate, name[1:])
                # elif if name[0] == "_": not self._obj
                # return getattr(self.instance, name.lstrip("_")) or getattr(
                #     self.instance.template.type_definition, name.lstrip("_")
                # )
            elif hasattr(self._obj or self._cls, name):
                cls_val = getattr(self._cls, name, None)
                if isinstance(cls_val, ComputedDescriptor):
                    return cls_val.func(self)
                elif self._obj:
                    val = getattr(self._obj, name)
                else:
                    val = cls_val
                if callable(val):
                    if isinstance(val, types.MethodType):
                        # so we can call with proxy as self
                        val = val.__func__
                    return functools.partial(val, self)
                return val
            elif name in self._instance.attributes:  # untyped properties
                return self._instance.attributes[name]
        elif isinstance(field, _Tosca_Field):
            if field.tosca_field_type in (
                ToscaFieldType.property,
                ToscaFieldType.attribute,
            ):
                val = _proxy_prop(field, self._instance.attributes[name])
                self._cache[name] = val
                return val
            elif field.tosca_field_type == ToscaFieldType.requirement:
                rels = [
                    rel
                    for rel in cast(NodeInstance, self._instance).requirements
                    if rel.name == field.tosca_name
                ]
                return self._proxy_requirements(field, rels)
            elif field.tosca_field_type == ToscaFieldType.artifact:
                artifact = cast(NodeInstance, self._instance).artifacts.get(
                    field.tosca_name
                )
                if artifact:
                    proxy = proxy_instance(artifact, field.get_type_info().types[0])
                    self._cache[field.name] = proxy
                    return proxy
                return None
            elif field.tosca_field_type == ToscaFieldType.capability:
                type_info = field.get_type_info()
                capabilities = cast(NodeInstance, self._instance).get_capabilities(
                    field.tosca_name
                )
                proxies = [
                    proxy_instance(cap, type_info.types[0]) for cap in capabilities
                ]
                if len(proxies) <= 1 and not type_info.collection:
                    proxies = proxies[0] if proxies else None  # type: ignore
                self._cache[field.name] = proxies
                return proxies
        raise AttributeError(name)


_proxies = {}


def get_proxy_class(cls, base: type = InstanceProxyBase):
    # we need to create a real subclass for issubclass() to work
    # (InstanceProxyBase[_cls] returns typing._GenericAlias not a real subclass)
    if cls not in _proxies:

        class _P(base):
            _cls = cls

        _proxies[cls] = _P
        return _P
    return _proxies[cls]


DT = TypeVar("DT", bound="DataType")


class DataTypeProxyBase(InstanceProxy, Generic[DT]):
    def __init__(self, values):
        self._values = values
        self._cache: Dict[str, Any] = {}

    def __str__(self):
        return f"<{self._cls.__name__}({self._values})>"

    if not TYPE_CHECKING:

        def __getattr__(self, name):
            return self._getattr(name)

    def __setattr__(self, name, val):
        if name in ("_values", "_obj", "_cache"):
            object.__setattr__(self, name, val)
            return
        if isinstance(val, (DataTypeProxyBase, ProxyCollection)):
            val = val._values
            self._cache[name] = val
        else:
            self._cache.pop(name, None)
            if isinstance(val, tosca.ToscaObject):
                val = val.to_yaml(tosca.yaml_cls)
        self._values[name] = val

    def _getattr(self, name):
        if name in self._cache:
            return self._cache[name]
        field = self._cls.__dataclass_fields__.get(name)
        if isinstance(field, _Tosca_Field):
            proxy = _proxy_prop(field, self._values[name])
            self._cache[name] = proxy
            return proxy
        elif hasattr(self._cls, name):
            return getattr(self._cls, name)
        elif name in self._values:  # untyped properties
            return self._values[name]
        raise AttributeError(name)
