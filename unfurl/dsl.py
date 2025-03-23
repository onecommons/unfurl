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
    Generator,
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
from tosca import InstanceProxy, ToscaType, DataEntity, ToscaFieldType, TypeInfo
import tosca.loader
from tosca._tosca import (
    _Tosca_Field,
    _ToscaType,
    Node,
    global_state,
    FieldProjection,
    EvalData,
    _get_field_from_prop_ref,
)
from .logs import getLogger
from .eval import RefContext, _map_value, set_eval_func, Ref
from .result import ResultsItem, CollectionProxy
from .runtime import (
    ArtifactInstance,
    CapabilityInstance,
    EntityInstance,
    NodeInstance,
    RelationshipInstance,
)
from .spec import EntitySpec, NodeSpec, RelationshipSpec, PolicySpec, GroupSpec
from .support import Status
from .repo import RepoView
from .util import UnfurlError, UnfurlTaskError, get_base_dir
from tosca.python2yaml import _OperationProxy, python_src_to_yaml_obj, WritePolicy
from .configurator import Configurator, ConfiguratorResult, TaskView
import sys

if TYPE_CHECKING:
    from .yamlloader import ImportResolver

logger = getLogger("unfurl")

_N = TypeVar("_N", bound=tosca.Namespace)


def maybe_reconvert(
    import_resolver: "ImportResolver",
    yaml_contents: str,
    path: str,
    repo_view: Optional[RepoView],
    base_dir: str,
    yaml_dict=dict,
) -> Optional[dict]:
    "If the YAML was generated from a Python file, regenerate if Python file is newer."
    if not WritePolicy.is_auto_generated(yaml_contents):  # or don't overwrite
        return None
    yaml_path = Path(path)
    python_path = yaml_path.parent / (yaml_path.stem + ".py")
    if not python_path.exists():
        return None
    if not WritePolicy["older"].can_overwrite(str(python_path), path):
        return None
    if not WritePolicy.ok_to_modify_auto_generated(yaml_contents, path):
        return None
    with open(python_path) as f:
        contents = f.read()
    return convert_to_yaml(
        import_resolver, contents, str(python_path), repo_view, base_dir, yaml_dict
    )


def convert_to_yaml(
    import_resolver: "ImportResolver",
    contents: str,
    path: str,
    repo_view: Optional[RepoView],
    base_dir: str,
    yaml_dict=dict,
) -> dict:
    from .yamlloader import yaml_dict_type

    tosca._tosca.yaml_cls = yaml_dict
    path = os.path.abspath(path)
    namespace: Dict[str, Any] = dict(__file__=path)
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
    safe_mode = import_resolver.get_safe_mode()
    write_policy = WritePolicy[os.getenv("UNFURL_OVERWRITE_POLICY") or "never"]
    module_name = package + "." + Path(path).stem
    tosca.loader.install(import_resolver, base_dir)
    safe_mode_override = os.getenv("UNFURL_TEST_SAFE_LOADER")
    if safe_mode_override:
        safe_mode = safe_mode_override != "never"
    if import_resolver.manifest.modules is None:
        import_resolver.manifest.modules = tosca.loader.get_allowed_modules()
    yaml_src = python_src_to_yaml_obj(
        contents,
        namespace,
        base_dir,
        module_name,
        # can't use readonly yaml since we might write out yaml files with it
        yaml_dict_type(False),
        safe_mode,
        # if not safe_mode then __import__ is used, which updates sys.modules
        import_resolver.manifest.modules if safe_mode else sys.modules,
        write_policy,
        import_resolver,
    )
    if os.getenv("UNFURL_TEST_PRINT_YAML_SRC"):
        logger.debug(
            "converted %s to:\n%s",
            path,
            pprint.pformat(yaml_src),
            extra=dict(truncate=0),
        )
    return yaml_src


def find_template(template: EntitySpec) -> Optional[ToscaType]:
    if isinstance(template, NodeSpec):
        section = "node_templates"
    elif isinstance(template, RelationshipSpec):
        section = "relationship_templates"
    elif isinstance(template, PolicySpec):
        section = "policies"
    elif isinstance(template, GroupSpec):
        section = "groups"
    else:
        return None
    metadata = template.toscaEntityTemplate.entity_tpl.get("metadata")
    module_name = metadata and metadata.get("module")
    if module_name and ToscaType._all_templates:
        return ToscaType._all_templates[section].get((module_name, template.name))  # type: ignore
    return None


def proxy_instance(
    instance: EntityInstance, cls: Type[_ToscaType], context: RefContext
):
    if instance.proxy and instance.proxy._context.vars == context.vars:
        # XXX better comparison of contexts
        return instance.proxy
    if instance.parent and isinstance(instance, (ArtifactInstance, CapabilityInstance)):
        obj = None
        owner = find_template(instance.parent.template)
        if owner:
            if issubclass(cls, Node):  # class is the owner node
                # this happens when references to the owner's methods are parsed to computed eval expressions in yaml
                return proxy_instance(instance.parent, owner.__class__, context)
            field_type = ToscaFieldType(
                instance.parentRelation[1:]
            )  # strip leading '_'
            field = owner.get_field_from_tosca_name(instance.template.name, field_type)
            if field:
                obj = getattr(owner, field.name)
    else:
        obj = find_template(instance.template)
    if obj:
        found_cls: Optional[Type[_ToscaType]] = obj.__class__
    else:
        # if target is subtype of cls, use the subtype
        found_cls = cls._all_types.get(instance.template.type, cls)

    proxy = get_proxy_class(found_cls)(instance, obj, context)
    instance.proxy = proxy
    return proxy


class DslMethodConfigurator(Configurator):
    def __init__(self, cls: Type[ToscaType], func: Callable, action: str):
        self.cls = cls
        self.func = func
        self.action = action
        self.configurator: Optional[Configurator] = None
        self._generator: Optional[Generator] = None

    def render(self, task: TaskView) -> Any:
        if self.action == "render" or self.action == "parse":
            # the operation couldn't be evaluated at yaml generation time, run it now
            obj = proxy_instance(task.target, self.cls, task.inputs.context)
            execute_proxy = _OperationProxy()
            try:
                global_state._type_proxy = execute_proxy
                # XXX pass task
                result = obj._invoke(self.func)
            finally:
                global_state._type_proxy = None

            if isinstance(result, Generator):
                self._generator = result
                # render the yielded configurator
                result = result.send(None)
                assert isinstance(result, Configurator), (
                    "Only yielding configurators is currently support"
                )
            if isinstance(result, Configurator):
                self.configurator = result
                # task.inputs get reset after render phase
                # so we need to set configSpec.inputs in order to preserve them for run()
                task.configSpec.inputs.update(result._inputs)
                # regenerate task.inputs:
                task._inputs = None
                task.inputs
                if not self._generator:
                    return self.configurator.render(task)
            elif callable(result):
                self.func = result
            elif execute_proxy._artifact_executed:
                name = execute_proxy.get_artifact_name()
                artifact = task.target.artifacts.get(name)
                if not artifact:
                    raise UnfurlTaskError(
                        task,
                        f"Implementation artifacts set during render must be declared on node template: {execute_proxy._artifact_executed}",
                    )
                task.configSpec.artifact = artifact.template
                task.configSpec.className = artifact.template.properties.get(
                    "className"
                )
                if execute_proxy._inputs:
                    task.configSpec.inputs.update(execute_proxy._inputs)
                    task.configSpec.arguments = list(execute_proxy._inputs)
                self.configurator = task.configSpec.create()
                # regenerate task.inputs:
                task._inputs = None
                task.inputs
                return self.configurator.render(task)
            else:
                raise UnfurlError(
                    f"unsupported configurator type: {type(result)} {result._obj} {result._cls}"
                )
        return super().render(task)

    def _is_generator(self) -> bool:
        # Note: this needs to called after configurator is set in render()
        if self._generator:
            return True
        if self.configurator:
            return self.configurator._is_generator()
        return inspect.isgeneratorfunction(self.func)

    def run(
        self, task: "TaskView"
    ) -> Union[Generator, ConfiguratorResult, "Status", bool]:
        # XXX
        # if self._generator:
        #     # XXX render in render() with subtask, assign subtask to yield'd TaskRequest
        #     subtask = yield task.create_sub_task(task.configSpec)
        #     assert subtask
        #     outputs = self._generator.send(subtask.outputs)
        #     yield task.done(outputs=outputs)
        #     return
        # XXX live artifact.execute() should return a configurator with _inputs set
        if self.configurator:
            return self.configurator.run(task)
        obj = proxy_instance(task.target, self.cls, task.inputs.context)
        return obj._invoke(self.func, task)

    def can_dry_run(self, task: "TaskView") -> bool:
        if self.configurator:
            return self.configurator.can_dry_run(task)
        can_dry_run = getattr(self.func, "can_dry_run", False)
        if callable(can_dry_run):
            return can_dry_run(task)  # type: ignore
        return can_dry_run


def eval_computed(arg, ctx: RefContext):
    """
    eval:
       computed: mod:class.computed_property

    or

    eval:
      computed: mod:func
    """
    if isinstance(arg, list):
        arg, args = arg[0], arg[1:]
    else:
        args = []
    if len(ctx.kw) > 1:
        kw = {k: v for k, v in ctx.kw.items() if k != "computed"}
    else:
        kw = {}
    module_name, sep, qualname = arg.partition(":")
    module = importlib.import_module(module_name)
    cls_name, sep, func_name = qualname.rpartition(".")
    if cls_name:
        cls = getattr(module, cls_name)
        func = getattr(cls, func_name)
        proxy = proxy_instance(cast(EntityInstance, ctx.currentResource), cls, ctx)
        return proxy._invoke(func, *args, **kw)
    else:
        return getattr(module, func_name)(*args, **kw)


set_eval_func("computed", eval_computed)


def eval_validate(arg, ctx: RefContext):
    """
    eval:
       validate: mod:class.method

    or

    eval:
       computed: mod:func
    """
    module_name, sep, qualname = arg.partition(":")
    module = importlib.import_module(module_name)
    cls_name, sep, func_name = qualname.rpartition(".")
    if cls_name:
        cls = getattr(module, cls_name)
        func = getattr(cls, func_name)
        proxy = proxy_instance(cast(EntityInstance, ctx.currentResource), cls, ctx)
        return proxy._invoke(func, ctx.vars["value"])
    else:
        return getattr(module, func_name)(ctx.vars["value"])


set_eval_func("validate", eval_validate)


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
        if issubclass(prop_type, tosca.DataEntity):
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


def _proxy_prop(type_info: tosca.TypeInfo, value: Any, obj: Optional[ToscaType] = None):
    # converts a value set on an instance to one usable when globalstate.mode == "runtime"
    prop_type = type_info.types[0]
    if type_info.collection is dict or dict in type_info.types:
        return ProxyMap(value, type_info)
    elif type_info.collection is list or list in type_info.types:
        return ProxyList(value, type_info)
    elif issubclass(prop_type, tosca.DataEntity):
        if isinstance(value, MutableMapping):
            return get_proxy_class(prop_type, DataTypeProxyBase)(value, obj)
        elif isinstance(value, (type(None), prop_type)):
            return value
        else:
            raise TypeError(f"can't convert value to {prop_type}: {value}")
    elif not type_info.instance_check(value):
        raise TypeError(f"value of type {type(value)} is not type {prop_type}: {value}")
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
    _obj: Optional[PT]

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
        path = getattr(sys.modules[self._cls.__module__], "__file__", __file__)
        assert path
        self._context.base_dir = os.path.dirname(path)

    def _get_field(self, name):
        if self._obj:
            return self._obj.get_instance_field(name)
        else:
            return self._cls.__dataclass_fields__.get(name)

    def _invoke(self, func, *args, **kwargs):
        saved_context = global_state.context
        global_state.context = self._context.copy(self._instance)
        try:
            return func(self, *args, **kwargs)
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
        Expected: Optional[Type[tosca.Node]] = None,
    ) -> tosca.Node:
        """
        Runtime version of Node (and RelationshipType)'s find_required_by, executes the eval expression returned by that method.
        """
        ref = cast(Type[tosca.Node], self._obj or self._cls).find_required_by(
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
        Runtime version of Node (and RelationshipType)'s find_required_by, executes the eval expression returned by that method.
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
            cls_or_obj=cast(Type[tosca.Node], self._obj or self._cls),
        )
        if field:
            _type = field.get_type_info_checked()
        else:
            _type = None
        if _type and _type.is_sequence():
            return cast(
                T,
                self._execute_resolve_all(
                    cast(EvalData, ref), prop_name, _type.types[0]
                ),
            )
        else:
            return cast(
                T, self._execute_resolve_one(cast(EvalData, ref), prop_name, _type)
            )

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

    def from_owner(
        self,
        prop_ref: T,
    ) -> T:
        return self._search(prop_ref, tosca.from_owner)

    def find_all_required_by(
        self,
        requirement: Union[str, FieldProjection],
        Expected: Optional[Type[tosca.Node]] = None,
    ) -> List[tosca.Node]:
        """
        Runtime version of Node (and RelationshipType)'s find_all_required_by, executes the eval expression returned by that method.
        """
        ref = cast(Type[tosca.Node], self._obj or self._cls).find_required_by(
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
            elif issubclass(t, tosca.Node):
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
                return getattr(self._instance.template, name[1:])
                # elif if name[0] == "_": not self._obj
                # return getattr(self.instance, name.lstrip("_")) or getattr(
                #     self.instance.template.type_definition, name.lstrip("_")
                # )
            elif hasattr(self._obj or self._cls, name):
                return super()._getattr(name)
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
                if (
                    type_info.optional
                    and field.tosca_name not in self._instance.attributes
                ):
                    val = None  # don't raise KeyError if the property isn't required
                else:
                    val = _proxy_prop(
                        type_info,
                        self._instance.attributes[field.tosca_name],
                        getattr(self._obj, name) if self._obj else None,
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


DT = TypeVar("DT", bound="DataEntity")


class DataTypeProxyBase(InstanceProxy, Generic[DT]):
    def __init__(self, values, obj: Optional[DT] = None):
        self._values = values
        self._cache: Dict[str, Any] = {}
        self._obj = obj

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

    def to_yaml(self, dict_cls=dict):
        return dict_cls(self._values)

    def _getattr(self, name):
        if name in self._cache:
            return self._cache[name]
        field = self._cls.__dataclass_fields__.get(name)
        if isinstance(field, _Tosca_Field):
            proxy = _proxy_prop(field.get_type_info(), self._values[name])
            self._cache[name] = proxy
            return proxy
        elif hasattr(self._obj or self._cls, name):
            # XXX if val is method, proxy and set context
            return super()._getattr(name)
        elif name in self._values:  # untyped properties
            return self._values[name]
        raise AttributeError(name)
