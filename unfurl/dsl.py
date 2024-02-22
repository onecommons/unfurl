# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
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
import pprint
import re
import types
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    MutableMapping,
    MutableSequence,
    Optional,
    Type,
    TypeVar,
    Union,
    cast,
    TYPE_CHECKING,
    Tuple,
)

import tosca
from tosca import InstanceProxy, ToscaType, DataType, ToscaFieldType, TypeInfo
import tosca.loader
from tosca._tosca import (
    _Tosca_Field,
    _ToscaType,
    global_state,
    FieldProjection,
    EvalData,
    _BaseDataType,
    _get_field_from_prop_ref,
    _get_expr_prefix,
)
from toscaparser.elements.portspectype import PortSpec
from toscaparser.nodetemplate import NodeTemplate
from .logs import getLogger
from .eval import RefContext, _map_value, set_eval_func, Ref
from .result import Results, ResultsItem, ResultsMap, CollectionProxy
from .runtime import EntityInstance, NodeInstance, RelationshipInstance
from .spec import EntitySpec, NodeSpec, RelationshipSpec
from .repo import RepoView
from .util import UnfurlError, check_class_registry, get_base_dir, register_class
from tosca.python2yaml import python_src_to_yaml_obj, WritePolicy, PythonToYaml
from unfurl.configurator import Configurator, TaskView
import sys
import toscaparser.capabilities
from toscaparser.entity_template import EntityTemplate
from toscaparser.nodetemplate import NodeTemplate
from unfurl.yamlmanifest import YamlManifest

if TYPE_CHECKING:
    from .yamlloader import ImportResolver
    from .job import Runner

logger = getLogger("unfurl")

_N = TypeVar("_N", bound=tosca.Namespace)


def convert_to_yaml(
    import_resolver: "ImportResolver",
    contents: str,
    path: str,
    repo_view: Optional[RepoView],
    base_dir: str,
) -> dict:
    from .yamlloader import yaml_dict_type

    namespace: Dict[str, Any] = {}
    logger.trace(
        f"converting Python to YAML: {path} {'in ' + repo_view.repository.name if repo_view and repo_view.repository else ''} (base: {base_dir})"
    )
    if repo_view and repo_view.repository:
        if repo_view.repo:
            root_path = repo_view.working_dir
        else:
            root_path = base_dir
        package_path = Path(get_base_dir(path)).relative_to(root_path)
        relpath = str(package_path).strip("/").replace("/", ".")
        if repo_view.repository.name == "unfurl":
            package = "unfurl."
        else:
            name = re.sub(r"\W", "_", repo_view.repository.name)
            assert name.isidentifier(), name
            package = "tosca_repositories." + name
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
    tosca.loader.install(import_resolver, base_dir)
    if os.getenv("UNFURL_TEST_SAFE_LOADER") == "never":
        safe_mode = False
    yaml_src = python_src_to_yaml_obj(
        contents,
        namespace,
        base_dir,
        module_name,
        # can't use readonly yaml since we might write out yaml files with it
        yaml_dict_type(False),
        safe_mode,
        import_resolver.manifest.modules if safe_mode else sys.modules,
        write_policy,
        import_resolver,
    )
    if os.getenv("UNFURL_TEST_PRINT_YAML_SRC"):
        logger.debug("converted %s to:\n%s", path, pprint.pformat(yaml_src), extra=dict(truncate=0))
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


def proxy_instance(instance, cls: Type[_ToscaType], context: RefContext):
    if instance.proxy:
        # XXX make sure existing proxy context matches context argument
        return instance.proxy
    obj = find_template(instance.template)
    if obj:
        found_cls: Optional[Type[_ToscaType]] = obj.__class__
    else:
        # if target is subtype of cls, use the subtype
        found_cls = cls._all_types.get(instance.template.type)

    proxy = get_proxy_class(found_cls or cls)(instance, obj, context)
    instance.proxy = proxy
    return proxy


class DslMethodConfigurator(Configurator):
    def __init__(self, cls: Type[ToscaType], func: Callable, action: str):
        self.cls = cls
        self.func = func
        self.action = action
        self.configurator: Optional[Configurator] = None

    def render(self, task: TaskView) -> Any:
        if self.action == "render":
            obj = proxy_instance(task.target, self.cls, task.inputs.context)
            result = obj._invoke(self.func)
            if isinstance(result, Configurator):
                self.configurator = result
                # configurators rely on task.inputs, so update them
                task.inputs.update(result.inputs)
                # task.inputs get reset after render phase
                # so we need to set configSpec.inputs too in order to preserve them for run()
                task.configSpec.inputs.update(result.inputs)
                return self.configurator.render(task)
            else:
                assert callable(result), result
                self.func = result
        return super().render(task)

    def _is_generator(self):
        # Note: this needs to called after configurator is set in render()
        if self.configurator:
            return self.configurator._is_generator()
        return inspect.isgeneratorfunction(self.func)

    def run(self, task):
        if self.configurator:
            return self.configurator.run(task)
        obj = proxy_instance(task.target, self.cls, task.inputs.context)
        return obj._invoke(self.func, task)

    def can_dry_run(self, task):
        if self.configurator:
            return self.configurator.can_dry_run(task)
        return False

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
    proxy = proxy_instance(ctx.currentResource, cls, ctx)
    return proxy._invoke(func)


set_eval_func("computed", eval_computed)


class ProxyCollection(CollectionProxy):
    """
    For proxying lists and maps set on a ToscaType template to a value usable by the runtime.
    It will create proxies of Tosca datatypes on demand.
    """
    def __init__(self, collection, type_info: tosca.TypeInfo):
        self._values = collection
        self._type_info = type_info
        self._cache: Dict[str, Any] = {}

    def __getitem__(self, key):
        if key in self._cache:
            return self._cache[key]
        val = self._values[key]
        prop_type = self._type_info.types[0]
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
            if isinstance(val, tosca.ToscaObject):
                val = val.to_yaml(tosca.yaml_cls)
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
        elif isinstance(val, tosca.ToscaObject):
            val = val.to_yaml(tosca.yaml_cls)
        self._values.insert(index, val)


def _proxy_prop(type_info: tosca.TypeInfo, value: Any):
    # converts a value set on an instance to one usable when globalstate.mode == "runtime"
    prop_type = type_info.types[0]
    if type_info.collection == dict:
        return ProxyMap(value, type_info)
    elif type_info.collection == list:
        return ProxyList(value, type_info)
    elif issubclass(prop_type, tosca.DataType):
        if isinstance(value, MutableMapping):
            return get_proxy_class(prop_type, DataTypeProxyBase)(value)
        elif isinstance(value, (type(None), prop_type)):
            return value
        else:
            raise TypeError(f"can't convert value to {prop_type}: {value}")
    elif not type_info.instance_check(value):
        raise TypeError(f"value is not type {prop_type}: {value}")
    return value


def _proxy_eval_result(
    value: Any, _context, Expected: Union[None, type, tosca.TypeInfo] = None
) -> Any:
    # convert a value obtained from an eval expression to one usable when globalstate.mode == "runtime"
    if isinstance(value, EntityInstance):
        if not Expected:
            ExpectedToscaType = tosca.ToscaType
        elif isinstance(Expected, tosca.TypeInfo):
            ExpectedToscaType = Expected.types[0]
        elif issubclass(Expected, tosca.ToscaType):
            ExpectedToscaType = Expected
        else:
            raise TypeError(f"expected value to be of type {Expected}")
        return proxy_instance(value, ExpectedToscaType, _context)
    elif Expected:
        if not isinstance(Expected, tosca.TypeInfo):
            Expected = tosca.pytype_to_tosca_type(Expected)
        return _proxy_prop(Expected, value)
    else:
        return value


PT = TypeVar("PT", bound="ToscaType")

T = TypeVar("T")


class Cache:
    def __init__(self, ctx: RefContext) -> None:
        self.context = ctx
        self._cache: Dict[str, ResultsItem] = {}

    def __getitem__(self, key) -> Any:
        item = self._cache[key]
        if item.last_computed < self.context.referenced.change_count:
            raise KeyError(key)
        return item.resolved

    def __setitem__(self, key, value):
        self._cache[key] = ResultsItem(value, seen=self.context.referenced.change_count)


class InstanceProxyBase(InstanceProxy, Generic[PT]):
    """
    Stand-in for a ToscaType template during runtime that proxy values from the instance.
    """

    _cls: Type[PT]

    def __init__(
        self, instance: EntityInstance, obj: Optional[PT], context: RefContext
    ):
        self._instance = instance
        # templates can have attributes assigned directly to it so see if we have the template
        self._obj = obj
        self._context = context.copy(instance)
        self._cache = Cache(self._context)
        # when calling into python code context won't have the basedir for the location of the code
        # (since its not imported as yaml). So set it now.
        if self._cls.__module__ == "builtins":
            path = __file__
        else:
            path = cast(str, sys.modules[self._cls.__module__].__file__)
        assert path
        self._context.base_dir = os.path.dirname(path)

    def _get_field(self, name):
        if self._obj:
            return self._obj.get_instance_field(name)
        else:
            return self._cls.__dataclass_fields__.get(name)

    def _invoke(self, func, *args):
        saved_context = global_state.context
        global_state.context = self._context.copy(self._instance.root)
        try:
            return func(self, *args)
        finally:
            global_state.context = saved_context

    def __str__(self):
        if self._obj:
            return str(self._obj)
        else:
            return self._cls.__str__(self)  # type: ignore

    def find_required_by(
        self,
        requirement: Union[str, FieldProjection],
        Expected: Optional[Type[tosca.NodeType]] = None,
    ) -> tosca.NodeType:
        """
        Runtime version of NodeType (and RelationshipType)'s find_required_by, executes the eval expression returned by that method.
        """
        ref = cast(Type[tosca.NodeType], self._obj or self._cls).find_required_by(
            requirement, Expected
        )
        assert isinstance(ref, EvalData)  # actually it will be a EvalData
        expected = Expected or tosca.nodes.Root
        return self._execute_resolve_one(ref, requirement, expected)

    def _execute_resolve_one(
        self,
        ref: EvalData,
        field_ref: Union[str, FieldProjection],
        Expected: Union[None, type, tosca.TypeInfo] = None,
    ) -> Any:
        """
        Runtime version of NodeType (and RelationshipType)'s find_required_by, executes the eval expression returned by that method.
        """
        result = _map_value(ref.expr, self._context)
        if Expected and not isinstance(Expected, tosca.TypeInfo):
            Expected = tosca.pytype_to_tosca_type(Expected)
        if result is None:
            if Expected and not cast(tosca.TypeInfo, Expected).optional:
                raise UnfurlError(f"No results found for {ref.expr}")
            return result
        if isinstance(result, MutableMapping):
            if Expected and not cast(tosca.TypeInfo, Expected).is_sequence():
                # if item is not expected to be a list then the expression unexpectedly returned more than one match
                # emulate eval expression's matchfirst (?) semantics
                result = result[0]
        return _proxy_eval_result(result, self._context, Expected)

    def _search(self, prop_ref: T, search_func: Callable) -> T:
        field, prop_name = _get_field_from_prop_ref(prop_ref)
        ref = search_func(
            field or prop_name,
            cls_or_obj=cast(Type[tosca.NodeType], self._obj or self._cls),
        )
        if field:
            _type = field.get_type_info_checked()
        else:
            _type = None
        if _type and _type.is_sequence():
            return cast(
                T, self._execute_resolve_all(cast(EvalData, ref), prop_name, _type.types[0])
            )
        else:
            return cast(T, self._execute_resolve_one(cast(EvalData, ref), prop_name, _type))

    def find_configured_by(
        self,
        prop_ref: T,
    ) -> T:
        return self._search(prop_ref, tosca.find_configured_by)

    def find_hosted_on(
        self,
        prop_ref: T,
    ) -> T:
        return self._search(prop_ref, tosca.find_hosted_on)

    def find_all_required_by(
        self,
        requirement: Union[str, FieldProjection],
        Expected: Optional[Type[tosca.NodeType]] = None,
    ) -> List[tosca.NodeType]:
        """
        Runtime version of NodeType (and RelationshipType)'s find_all_required_by, executes the eval expression returned by that method.
        """
        ref = cast(Type[tosca.NodeType], self._obj or self._cls).find_required_by(
            requirement, Expected
        )
        return self._execute_resolve_all(
            cast(EvalData, ref), requirement, Expected or tosca.nodes.Root
        )

    def _execute_resolve_all(
        self,
        ref: EvalData,
        field_ref: Union[str, FieldProjection],
        Expected: Union[None, type, tosca.TypeInfo],
    ) -> List[Any]:
        result = Ref(cast(Union[str, dict], ref.expr)).resolve(self._context)
        return [_proxy_eval_result(item, self._context, Expected) for item in result]

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
            return proxy_instance(instance, cls, self._context)
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
        #  you shouldn't be able to modify the dsl at this point (there probably isn't even an object to modify)
        #  so only modify the instance
        if name in ("_instance", "_obj", "_cache", "_context"):
            object.__setattr__(self, name, val)
            return
        field = self._get_field(name)
        if isinstance(val, (DataTypeProxyBase, ProxyCollection)):
            val = val._values
        elif isinstance(val, tosca.ToscaObject):
            val = val.to_yaml(tosca.yaml_cls)
        attr_name = field.tosca_name if isinstance(field, _Tosca_Field) else name
        self._instance.attributes[attr_name] = val

    # XXX
    # def __delattr__(self, __name: str) -> None:
    #     return None

    def _getattr(self, name):
        # if the attribute refers to a instance field, return the value from the instance
        # otherwise try to get the value from the obj, if set, or from the cls
        # note that when accessing viva instance.attributes, any evaluation of expressions
        # will be done via the yaml generated from python using the instance's attribute manager context.
        try:
            return self._cache[name]
        except KeyError:
            pass
        field = self._get_field(name)
        if not field or not isinstance(field, _Tosca_Field):
            if name in ["_name", "_metadata", "_directives"]:
                # XXX other special attributes
                return getattr(self._instance.template.toscaEntityTemplate, name[1:])
                # elif if name[0] == "_": not self._obj
                # return getattr(self.instance, name.lstrip("_")) or getattr(
                #     self.instance.template.type_definition, name.lstrip("_")
                # )
            elif hasattr(self._obj or self._cls, name):
                if self._obj:
                    val = getattr(self._obj, name)
                else:
                    val = getattr(self._cls, name, None)
                if callable(val):
                    if isinstance(val, types.MethodType):
                        # so we can call with proxy as self
                        val = val.__func__
                    # should have been set by _invoke() earlier in the call stack.
                    assert global_state.context
                    return functools.partial(val, self)
                return val
            elif name in self._instance.attributes:  # untyped properties
                return self._instance.attributes[name]
        else:
            if field.tosca_field_type in (
                ToscaFieldType.property,
                ToscaFieldType.attribute,
            ):
                # self._context._trace=2
                # self._instance.attributes.context._trace=2
                type_info = field.get_type_info()
                if type_info.optional and field.tosca_name not in self._instance.attributes:
                    val = None  # don't raise KeyError if the property isn't required
                else:
                    val = _proxy_prop(
                        type_info, self._instance.attributes[field.tosca_name]
                    )
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
                    proxy = proxy_instance(
                        artifact, field.get_type_info().types[0], self._context
                    )
                    self._cache[field.name] = proxy
                    return proxy
                return None
            elif field.tosca_field_type == ToscaFieldType.capability:
                type_info = field.get_type_info()
                capabilities = cast(NodeInstance, self._instance).get_capabilities(
                    field.tosca_name
                )
                proxies = [
                    proxy_instance(cap, type_info.types[0], self._context)
                    for cap in capabilities
                ]
                if len(proxies) <= 1 and not type_info.collection:
                    proxies = proxies[0] if proxies else None  # type: ignore
                self._cache[field.name] = proxies
                return proxies
        raise AttributeError(name)


_proxies = {}


def get_proxy_class(cls, base: type = InstanceProxyBase):
    # we need to create a real subclass for issubclass() to work
    # (InstanceProxyBase[_cls] returns typing._GenericAlias, not a real subclass)
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
            proxy = _proxy_prop(field.get_type_info(), self._values[name])
            self._cache[name] = proxy
            return proxy
        elif hasattr(self._cls, name):
            # XXX if val is method, proxy and set context
            return getattr(self._cls, name)
        elif name in self._values:  # untyped properties
            return self._values[name]
        raise AttributeError(name)
