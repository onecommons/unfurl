"""
Type-safe equivalents to Unfurl's eval expression functions. 

When called in "spec" mode (e.g. as part of a class definition or in `_class_init_`) they will return eval expression
that will get executed. But note that the type signature will match the result of the expression, not the eval expression itself.
This enables type checking inside 

When called in runtime mode (ie. as a computed property or as operation implementation) they perform the equivalent functionality.

Note that some functions are overloaded with two signatures, 
One that takes a live ToscaType object as an argument and one that takes ``None`` in its place.

The former variant can only be used in runtime mode as live objects are not available outside that mode.
In "spec" mode, the None variant must be used and at runtime the eval expression returned by that function
will be evaluated using the current context's instance.
"""
# put this module is in unfurl/tosca_plugins because modules in this package are whitelisted as safe
# they are safe because they never be evaluated in safe mode -- just return _Refs
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Type,
    TypeVar,
    Union,
    cast,
    overload,
    TYPE_CHECKING,
)
from .. import support

from ..support import _template_func, _urljoin as urljoin, run_lookup
from ..support import (
    to_label,
    to_dns_label,
    to_kubernetes_label,
    to_googlecloud_label,
    _generate,
)
from tosca._tosca import _Ref, ToscaType, global_state, MISSING
import tosca
from ..dsl import InstanceProxyBase, proxy_instance
from ..eval import Ref, RefContext
from ..projectpaths import FilePath, TempFile, _abspath
from ..util import UnfurlError
from ..yamlloader import cleartext_yaml


__all__ = [
    "to_label",
    "to_dns_label",
    "to_kubernetes_label",
    "to_googlecloud_label",
    "urljoin",
    "has_env",
    "get_env",
    "get_input",
    "if_expr",
    "or_expr",
    "and_expr",
    "tempfile",
    "lookup",
    "to_env",
    "get_ensemble_metadata",
    "abspath",
    "get_dir",
    "template",
    "_generate",
    "get_nodes_of_type",
    "negate",
    "as_bool",
    "uri",
    # XXX kubernetes_current_namespace, kubectl, 
    # XXX get_artifact
]


def get_context(obj: ToscaType) -> RefContext:
    if isinstance(obj, InstanceProxyBase) and obj._context:
        return obj._context
    else:
        raise ValueError(
            f"ToscaType object cannot be converted to a RefContext -- executed from a live instance? {obj}"
        )


def get_nodes_of_type(cls: Type[ToscaType]) -> list:
    if global_state.mode == "runtime":
        return [
            proxy_instance(instance, cls, global_state.context)
            for instance in support.get_nodes_of_type(
                cls.tosca_type_name(), global_state.context
            )
        ]
    else:
        return _Ref({"get_nodes_of_type": cls.tosca_type_name()})  # type: ignore


def negate(val) -> bool:
    if global_state.mode == "runtime":
        return not bool(val)
    else:
        if isinstance(val, _Ref):
            val = val.expr
        return cast(bool, _Ref(dict(eval={"not": val})))


def as_bool(val) -> bool:
    if global_state.mode == "runtime":
        return bool(val)
    else:
        if isinstance(val, _Ref):
            val = val.expr
        return cast(bool, _Ref(dict(eval={"not": {"not": val}})))


def get_input(name: str, default: Any = MISSING):
    # only needs root in context
    if default is not MISSING:
        args: Any = [name, default]
    else:
        args = name
    if global_state.mode == "runtime":
        return support.get_input(args, global_state.context)
    else:
        return _Ref({"get_input": args})


def has_env(name: str) -> bool:
    if global_state.mode == "runtime":
        assert global_state.context
        return name in global_state.context.environ
    else:
        return _Ref({"has_env": name})  # type: ignore


def get_env(
    name: Optional[str], default: Optional[str] = None, ctx=None
) -> Union[str, None, Dict[str, str]]:
    # only ctx.environ is used
    if name is None and default is None:
        args = None
    else:
        args = [name, default]
    if global_state.mode == "runtime":
        assert ctx or global_state.context
        return support.get_env(args, ctx or global_state.context)
    else:
        return _Ref({"get_env": args})  # type: ignore


T = TypeVar("T")
U = TypeVar("U")


# repository_id: str = when(cls.project_id, f"{cls.project_id}/{cls.branch}", "")
def if_expr(if_cond, then: T, otherwise: U = None) -> Union[T, U]:
    """Returns an eval expression like:

    {"eval": {"if": if_cond, "then": then, "else": otherwise}

    This will not evaluate at runtime mode because all arguments will evaluated
    before calling this function, defeating eval expressions' (and Python's) short-circuit semantics.
    To avoid unexpected behavior, an error will be raised if invoked during runtime mode.
    Instead just use a Python 'if' statement or expression.
    """
    if global_state.mode == "runtime":
        raise UnfurlError(
            "'if_expr()' can not be valuate in runtime mode, instead just use a Python 'if' statement or expression."
        )
    else:
        return _Ref({"eval": {"if": if_cond, "then": then, "else": otherwise}})  # type: ignore


def or_expr(self, __value: Any) -> "_Ref":
    if global_state.mode == "runtime":
        raise UnfurlError(
            "'or_expr()' can not be valuate in runtime mode, instead just use Python's 'or' operator."
        )
    else:
        return _Ref(dict(eval={"or": [self.expr, __value]}))


def and_expr(self, __value: Any) -> "_Ref":
    if global_state.mode == "runtime":
        raise UnfurlError(
            "'and_expr()' can not be valuate in runtime mode, instead just use a Python 'and' operator."
        )
    else:
        return _Ref(dict(eval={"and": [self.expr, __value]}))


def to_env(args: Dict[str, str], update_os_environ=False) -> Dict[str, str]:
    if global_state.mode == "runtime":
        ctx = global_state.context
        if update_os_environ:
            ctx.kw = dict(update_os_environ=update_os_environ)
        return support.to_env(args, ctx)
    else:
        expr = dict(to_env=args, update_os_environ=update_os_environ)
        return _Ref(dict(eval=expr))  # type: ignore


@overload
def abspath(obj: ToscaType, path: str, relativeTo=None, mkdir=False) -> FilePath:
    ...


@overload
def abspath(obj: None, path: str, relativeTo=None, mkdir=False) -> str:
    ...


def abspath(
    obj: Union[ToscaType, None], path: str, relativeTo=None, mkdir=False
) -> Union[FilePath, str]:
    if obj and global_state.mode == "runtime":
        ctx = get_context(obj)
        return _abspath(ctx, path, relativeTo, mkdir)
    else:  # this will resolve to a str
        return cast(str, _Ref({"eval": {"abspath": [path, relativeTo, mkdir]}}))


@overload
def get_dir(obj: ToscaType, relativeTo=None, mkdir=False) -> FilePath:
    ...


@overload
def get_dir(obj: None, relativeTo=None, mkdir=False) -> str:
    ...


def get_dir(
    obj: Union[ToscaType, None], relativeTo=None, mkdir=False
) -> Union[FilePath, str]:
    if obj and global_state.mode == "runtime":
        ctx = get_context(obj)
        return _abspath(ctx, "", relativeTo, mkdir)
    else:  # this will resolve to a str
        return cast(str, _Ref({"eval": {"get_dir": [relativeTo, mkdir]}}))


def tempfile(contents: Any, suffix="", encoding=None):
    if global_state.mode == "runtime":
        yaml = (
            global_state.context.currentResource.root.attributeManager.yaml
            if encoding == "vault"
            else cleartext_yaml
        )
        return TempFile(contents, suffix, yaml, encoding)
    else:
        return _Ref(
            {"eval": {"tempfile": contents, "suffix": suffix, "encoding": encoding}}
        )


def template(
    obj: Union[ToscaType, None],
    *,
    path: str = "",
    contents: str = "",
    overrides: Optional[Dict[str, str]] = None,
) -> Any:
    if path:
        args: Any = dict(path=path)
    else:
        args = contents
    if obj and global_state.mode == "runtime":
        ctx = get_context(obj)
        if overrides:
            ctx.kw = dict(overrides=overrides)
        return _template_func(args, ctx)
    else:
        return _Ref({"eval": {"template": args, "overrides": overrides}})


def lookup(name: str, *args, **kwargs):
    if global_state.mode == "runtime":
        return run_lookup(name, global_state.context.templar, *args, **kwargs)
    else:
        invoke = {name: args}
        invoke.update(kwargs)
        return _Ref({"eval": {"lookup": invoke}})


@overload
def get_ensemble_metadata(key: None = None) -> Dict[str, str]:
    ...


@overload
def get_ensemble_metadata(key: str) -> str:
    ...


def get_ensemble_metadata(key=None):
    if global_state.mode == "runtime":
        # only need ctx.task
        return support.get_ensemble_metadata(key, global_state.context)
    else:
        return _Ref({"eval": dict(get_ensemble_metadata=key)})


def uri(obj: Union[ToscaType, None] = None) -> Optional[str]:
    ref = _Ref({"eval": ".uri"})
    if obj and global_state.mode == "runtime":
        ctx = get_context(obj)
        assert ref.expr
        return cast(str, Ref(ref.expr).resolve_one(ctx))
    else:  # this will resolve to a str
        return cast(str, ref)
