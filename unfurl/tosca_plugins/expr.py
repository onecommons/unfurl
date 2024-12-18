"""
Type-safe equivalents to Unfurl's Eval `Expression Functions`.

When called in `"parse" mode <global_state_mode>` (e.g. as part of a class definition or in ``_class_init_``) they will return eval expression
that will get executed. But note that the type signature will match the result of the expression, not the eval expression itself.
(This type punning enables effective static type checking).

When called in runtime mode (ie. as a computed property or as operation implementation) they perform the equivalent functionality.

These functions can be executed in the safe mode Python sandbox as it always executes in "parse" mode.

Note that some functions are overloaded with two signatures,
One that takes a live ToscaType object as an argument and one that takes ``None`` in its place.

The former variant can only be used in runtime mode as live objects are not available outside that mode.
In "parse" mode, the None variant must be used and at runtime the eval expression returned by that function
will be evaluated using the current context's instance.

User-defined functions can be made available as an expression functions by the `runtime_func` decorator.
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

from tosca import (
    EvalData,
    ToscaType,
    MISSING,
    safe_mode,
    global_state_mode,
    global_state_context,
)
import tosca

tfvar = tosca.PropertyOptions(
    dict(tfvar=True)
)  # override inputs["tfvar"], ignored if tfvar is a string
tfoutput = tosca.AttributeOptions(dict(tfoutput=True))
sensitive = tosca.Options(dict(sensitive=True))


def validate(factory):
    return tosca.Options(
        dict(
            validation={
                "eval": dict(validate=f"{factory.__module__}:{factory.__qualname__}")
            }
        )
    )


if TYPE_CHECKING or not safe_mode():
    # these imports aren't safe
    from .. import support
    from ..dsl import InstanceProxyBase, proxy_instance
    from ..eval import Ref, RefContext, map_value
    from ..util import UnfurlError
    from ..yamlloader import cleartext_yaml
    from ..projectpaths import FilePath, TempFile, _abspath
    from tosca.yaml2python import has_function

    def get_context(obj: ToscaType, kw: Optional[Dict[str, Any]] = None) -> RefContext:
        if isinstance(obj, InstanceProxyBase) and obj._context:
            if kw is not None:
                ctx = obj._context.copy()
                ctx.kw = kw
                return ctx
            return obj._context
        else:
            raise ValueError(
                f"ToscaType object cannot be converted to a RefContext -- executed from a live instance? {obj}"
            )

else:
    # if this module is loaded in safe_mode these will never by referenced:
    support = object()
    UnfurlError = RuntimeError
    get_context = None
    FilePath = Any


__all__ = [
    "tfoutput",
    "tfvar",
    "sensitive",
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
    "get_nodes_of_type",
    "not_",
    "as_bool",
    "uri",
    "concat",
    "token",
    # XXX get_property
    # XXX get_attribute
    # XXX File
    # XXX kubernetes_current_namespace
    # XXX kubectl,
    # XXX get_artifact
    # XXX python
    "runtime_func",
]

F = TypeVar("F", bound=Callable[..., Any])


@overload
def runtime_func(_func: None = None) -> Callable[[F], F]: ...


@overload
def runtime_func(_func: F) -> F: ...


def runtime_func(_func: Optional[F] = None) -> Union[F, Callable[[F], F]]:
    """
    A decorator for making a function invocable as a runtime expression.
    When the decorated function invoked in `"parse" mode <global_state_mode>`, if any of its arguments contain :py:class:`tosca.EvalData`,
    then the function will return :py:class:`tosca.EvalData` containing a JSON representation of the invocation as an `expression function <Expression functions>` that will be evaluated at runtime.
    Otherwise, the function will eagerly execute as a normal Python function.
    """

    def _make_computed(func: F) -> F:
        def wrapped(*args, **kwargs):
            if global_state_mode() == "runtime":
                ctx = global_state_context()
                return func(*map_value(args, ctx), **map_value(kwargs, ctx))
            elif (
                not safe_mode() and not has_function(args) and not has_function(kwargs)
            ):
                return func(*args, **kwargs)
            else:
                kwargs["computed"] = [f"{func.__module__}:{func.__qualname__}", *args]
                return EvalData({"eval": kwargs})

        return cast(F, wrapped)

    if _func is None:
        return _make_computed
    else:
        return _make_computed(_func)


def get_nodes_of_type(cls: Type[ToscaType]) -> list:
    if global_state_mode() == "runtime":
        return [
            proxy_instance(instance, cls, global_state_context())
            for instance in support.get_nodes_of_type(
                cls.tosca_type_name(), global_state_context()
            )
        ]
    else:
        return EvalData({"get_nodes_of_type": cls.tosca_type_name()})  # type: ignore


def not_(val) -> bool:
    if global_state_mode() == "runtime":
        return not bool(val)
    else:
        return cast(bool, EvalData(dict(eval={"not": val, "map_value": 1})))


def as_bool(val) -> bool:
    if global_state_mode() == "runtime":
        return bool(val)
    else:
        return cast(bool, EvalData(dict(eval={"not": {"not": val, "map_value": 1}})))


TI = TypeVar("TI")


@overload
def get_input(name: str, default: TI) -> TI: ...


@overload
def get_input(name: str) -> Any: ...


def get_input(name, default=MISSING):
    # only needs root in context
    if default is not MISSING:
        args: Any = [name, default]
    else:
        args = name
    if global_state_mode() == "runtime":
        return support.get_input(args, global_state_context())
    else:
        return EvalData({"get_input": args})


def concat(*args: str, sep="") -> str:
    # evaluate now if in parse mode and no EvalData args
    if global_state_mode() == "runtime" or not any(
        map(lambda a: isinstance(a, EvalData), args)
    ):
        return sep.join([str(a) for a in args])
    else:
        expr: dict = {"concat": list(args)}
        if sep:
            expr["sep"] = sep
        return EvalData(expr)  # type: ignore


def token(string: str, token: str, index: int) -> str:
    # evaluate now if in parse mode and no EvalData args
    if global_state_mode() == "runtime" or (
        not isinstance(string, EvalData)
        and not isinstance(token, EvalData)
        and not isinstance(index, EvalData)
    ):
        return string.split(token)[index]
    else:
        return EvalData({"token": [string, token, index]})  # type: ignore


def has_env(name: str) -> bool:
    if global_state_mode() == "runtime":
        assert global_state_context()
        return name in global_state_context().environ
    else:
        return EvalData({"has_env": name})  # type: ignore


@overload
def get_env(name: str, default: str, *, ctx=None) -> str: ...


@overload
def get_env(name: str, *, ctx=None) -> Optional[str]: ...


@overload
def get_env(*, ctx=None) -> Dict[str, str]: ...


def get_env(name=None, default=None, *, ctx=None):
    # only ctx.environ is used
    if name is None and default is None:
        args = None
    else:
        args = [name, default]
    if global_state_mode() == "runtime":
        assert ctx or global_state_context()
        return support.get_env(args, ctx or global_state_context())
    else:
        return EvalData({"get_env": args})


T = TypeVar("T")
U = TypeVar("U")


def if_expr(if_cond, then: T, otherwise: U = None) -> Union[T, U]:
    """Returns an eval expression like:

    ``{"eval": {"if": if_cond, "then": then, "else": otherwise}``

    This will not evaluate at `runtime mode <global_state_mode>` because all arguments will evaluated
    before calling this function, defeating eval expressions' (and Python's) short-circuit semantics.
    To avoid unexpected behavior, an error will be raised if invoked during runtime mode.
    Instead just use a Python 'if' statement or expression.
    """
    if global_state_mode() == "runtime":
        raise UnfurlError(
            "'if_expr()' can not be valuate in runtime mode, instead just use a Python 'if' statement or expression."
        )
    else:
        return EvalData({
            "eval": {"if": if_cond, "then": then, "else": otherwise, "map_value": 1}
        })  # type: ignore


def or_expr(left: T, right: U) -> Union[T, U]:
    if global_state_mode() == "runtime":
        raise UnfurlError(
            "'or_expr()' can not be valuate in runtime mode, instead just use Python's 'or' operator."
        )
    else:
        return EvalData(dict(eval={"or": [left, right], "map_value": 1}))  # type: ignore


def fallback(left: Optional[T], right: T) -> T:
    if global_state_mode() == "runtime":
        raise UnfurlError(
            "'fallback()' can not be valuate in `runtime mode <global_state_mode>`, instead just use Python's 'or' operator."
        )
    else:
        return EvalData(dict(eval={"or": [left, right], "map_value": 1}))  # type: ignore


def and_expr(left: T, right: U) -> Union[T, U]:
    if global_state_mode() == "runtime":
        raise UnfurlError(
            "'and_expr()' can not be valuate in runtime mode, instead just use a Python 'and' operator."
        )
    else:
        return EvalData(dict(eval={"or": [left, right], "map_value": 1}))  # type: ignore


def to_env(args: Dict[str, str], update_os_environ=False) -> Dict[str, str]:
    if global_state_mode() == "runtime":
        ctx = global_state_context()
        if update_os_environ:
            ctx = ctx.copy()
            ctx.kw = dict(update_os_environ=update_os_environ)
        return support.to_env(args, ctx)
    else:
        expr = dict(to_env=args, update_os_environ=update_os_environ)
        return EvalData(dict(eval=expr))  # type: ignore


@overload
def abspath(obj: ToscaType, path: str, relativeTo=None, mkdir=False) -> FilePath: ...


@overload
def abspath(obj: None, path: str, relativeTo=None, mkdir=False) -> str: ...


def abspath(
    obj: Union[ToscaType, None], path: str, relativeTo=None, mkdir=False
) -> Union[FilePath, str]:
    if obj and global_state_mode() == "runtime":
        ctx = get_context(obj)
        return _abspath(ctx, path, relativeTo, mkdir)
    else:  # this will resolve to a str
        return cast(str, EvalData({"eval": {"abspath": [path, relativeTo, mkdir]}}))


@overload
def get_dir(obj: ToscaType, relativeTo=None, mkdir=False) -> FilePath: ...


@overload
def get_dir(obj: None, relativeTo=None, mkdir=False) -> str: ...


def get_dir(
    obj: Union[ToscaType, None], relativeTo=None, mkdir=False
) -> Union[FilePath, str]:
    if obj and global_state_mode() == "runtime":
        ctx = get_context(obj)
        return _abspath(ctx, "", relativeTo, mkdir)
    else:  # this will resolve to a str
        return cast(str, EvalData({"eval": {"get_dir": [relativeTo, mkdir]}}))


def tempfile(contents: Any, suffix="", encoding=None):
    if global_state_mode() == "runtime":
        yaml = (
            global_state_context().currentResource.root.attributeManager.yaml
            if encoding == "vault"
            else cleartext_yaml
        )
        return TempFile(contents, suffix, yaml, encoding)
    else:
        return EvalData({
            "eval": {"tempfile": contents, "suffix": suffix, "encoding": encoding}
        })


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
    if obj and global_state_mode() == "runtime":
        ctx = get_context(obj)
        if overrides:
            ctx = ctx.copy()
            ctx.kw = dict(overrides=overrides)
        return support._template_func(args, ctx)
    else:
        return EvalData({"eval": {"template": args, "overrides": overrides}})


def lookup(name: str, *args, **kwargs):
    if global_state_mode() == "runtime":
        return support.run_lookup(name, global_state_context().templar, *args, **kwargs)
    else:
        invoke = {name: args}
        invoke.update(kwargs)
        return EvalData({"eval": {"lookup": invoke}})


@overload
def get_ensemble_metadata(key: None = None) -> Dict[str, str]: ...


@overload
def get_ensemble_metadata(key: str) -> str: ...


def get_ensemble_metadata(key=None):
    if global_state_mode() == "runtime":
        # only need ctx.task
        return support.get_ensemble_metadata(key, global_state_context())
    else:
        return EvalData({"eval": dict(get_ensemble_metadata=key)})


def uri(obj: Union[ToscaType, None] = None) -> Optional[str]:
    expr = {"eval": ".uri"}
    if obj and global_state_mode() == "runtime":
        ctx = get_context(obj)
        return cast(str, Ref(expr).resolve_one(ctx))
    else:  # this will resolve to a str
        return cast(str, EvalData(expr))
