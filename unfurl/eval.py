# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
API for evaluating `eval expressions`. See also `unfurl.configurator.TaskView.query`.
"""

from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Tuple,
    Optional,
    Union,
    Iterable,
    cast,
    TYPE_CHECKING,
    overload,
)
from typing import Mapping as MappingType
from typing_extensions import Literal
import re
import operator
import sys
import collections
from collections.abc import Mapping, MutableSequence
from ruamel.yaml.comments import CommentedMap
from toscaparser.common.exception import ExceptionCollector
from toscaparser.elements.statefulentitytype import StatefulEntityType
from unfurl.logs import UnfurlLogger

from .util import validate_schema, UnfurlError, assert_form
from .result import (
    ResultsList,
    Result,
    Results,
    ExternalValue,
    ResourceRef,
    ResultsMap,
)

import logging

if TYPE_CHECKING:
    from .configurator import TaskView


logger = cast(UnfurlLogger, logging.getLogger("unfurl.eval"))


class UnfurlEvalError(UnfurlError):
    pass


def map_value(
    value: Any,
    resourceOrCxt: Union["RefContext", "ResourceRef"],
    applyTemplates: bool = True,
    as_list: bool = False,
    flatten: bool = False,
) -> Any:
    """
    Return a copy of the given string, dict, or list, resolving any expressions or template strings embedded in it.

    Args:
      value (Any): The value to be processed, which can be a string, dictionary, or list.
      resourceOrCxt (Union["RefContext", "ResourceRef"]): The context or resource instance used for resolving expressions.
      applyTemplates (bool, optional): Whether to evaluate Jinja2 templates embedded in strings. Defaults to True.
      as_list (bool, optional): Whether to return the result as a list. Defaults to False.

    Returns:
      Any: The processed value with resolved expressions or template strings. If ``as_list`` is True, the result is always a list.
    """
    # as_list always returns a list but preserves wantList semantics for internal evaluation
    if not isinstance(resourceOrCxt, RefContext):
        resourceOrCxt = RefContext(resourceOrCxt)
    ret_val = _map_value(
        value, resourceOrCxt, resourceOrCxt.wantList or False, applyTemplates, flatten
    )
    if as_list and not isinstance(ret_val, list):
        if ret_val is None:
            ret_val = []
        else:
            ret_val = [ret_val]
    return ret_val


def _map_value(
    value: Any,
    ctx: "RefContext",
    wantList: Union[bool, Literal["result"]] = False,
    applyTemplates: bool = True,
    flatten=False,
) -> Any:
    from .support import is_template, apply_template

    if Ref.is_ref(value):
        # wantList=False := resolve_one
        return Ref(value).resolve(ctx, wantList=wantList)

    if isinstance(value, Mapping):
        oldBaseDir = ctx.base_dir
        try:
            ctx.base_dir = getattr(value, "base_dir", oldBaseDir)
            if ctx.base_dir and ctx.base_dir != oldBaseDir:
                ctx.trace("found base_dir", ctx.base_dir)
            return {
                key: _map_value(v, ctx, wantList, applyTemplates)
                for key, v in value.items()
            }
        finally:
            ctx.base_dir = oldBaseDir
    elif isinstance(value, (MutableSequence, tuple)):
        if flatten:
            result = []
            for item in value:
                item_val = _map_value(item, ctx, wantList, applyTemplates)
                if isinstance(item_val, list):
                    result.extend(item_val)
                else:
                    result.append(item_val)
            return result
        else:
            return [_map_value(item, ctx, wantList, applyTemplates) for item in value]
    elif applyTemplates and is_template(value, ctx):
        return apply_template(value, ctx.copy(wantList=wantList))
    return value


class _Tracker:
    def __init__(self):
        self.count = 0
        self.referenced: List[Tuple[Union[str, None, "Ref"], List[Result]]] = []
        self.change_count = 0

    def start(self):
        self.count += 1
        return len(self.referenced)

    def stop(self):
        self.count -= 1
        return self.count

    def add_ref_reference(self, ref: "Ref", result: List[Result]):
        if self.count > 0:
            self.referenced.append((ref, result))

    def add_result_reference(self, ref: Union[str, None], result: Result):
        if self.count > 0:
            self.referenced.append((ref, [result]))

    def getReferencedResults(self, index=0) -> Iterator[Result]:
        referenced = self.referenced[index:]
        if not referenced:
            return
        for ref, results in referenced:
            for obj in results:
                assert isinstance(obj, Result)
                yield obj


_defaultStrictness = True

ResolveOneUnion = Union[None, Any, List[Any]]


class RefContext:
    """
    The context of the expression being evaluated.
    """

    DefaultTraceLevel = 0
    _Funcs: Dict = {}
    currentFunc: Optional[str] = None

    def __init__(
        self,
        currentResource: "ResourceRef",
        vars: Optional[dict] = None,
        wantList: Optional[Union[bool, Literal["result"]]] = False,
        resolveExternal: bool = False,
        trace: Optional[int] = None,
        strict: Optional[bool] = None,
        task: Optional["TaskView"] = None,
    ) -> None:
        self.vars = vars or {}
        # the original context:
        self.currentResource = currentResource
        assert isinstance(currentResource, ResourceRef)
        # the last resource encountered while evaluating:
        self._lastResource = currentResource
        # current segment is the final segment:
        self._rest = None
        self.wantList = wantList
        self.resolveExternal = resolveExternal
        self._trace = self.DefaultTraceLevel if trace is None else trace
        self._strict = strict
        self.base_dir = currentResource.base_dir
        self.templar = currentResource.templar
        self.referenced = _Tracker()
        self.task = task
        self.kw: Mapping[str, Any] = {}
        self.tosca_type: Optional[StatefulEntityType] = None

    @property
    def strict(self) -> bool:
        if self._strict is not None:
            return self._strict
        elif self.task:
            return not self.task._rendering
        else:
            return _defaultStrictness

    @property
    def environ(self) -> Dict[str, str]:
        if self.task:
            return self.task.environ
        else:
            return self.currentResource.environ

    def copy(
        self,
        resource: Optional["ResourceRef"] = None,
        vars: Optional[dict] = None,
        wantList: Optional[Union[bool, Literal["result"]]] = None,
        trace: int = 0,
        strict: Optional[bool] = None,
        tosca_type: Optional[StatefulEntityType] = None,
    ) -> "RefContext":
        """
        Create a copy of the current RefContext with optional modifications.

        Args:
          resource (Optional[ResourceRef]): The resource reference to use for the copy. Defaults to None.
          vars (Optional[dict]): A dictionary of variables to update in the copy. Defaults to None.
          wantList (Optional[Union[bool, Literal["result"]]]): Determines if a list is desired. Defaults to None.
          trace (int): The trace level for the copy. Defaults to 0.
          strict (Optional[bool]): Whether to enforce strict mode. Defaults to None.
          tosca_type (Optional[StatefulEntityType]): The TOSCA type for the copy. Defaults to None.

        Returns:
          RefContext: A new instance of RefContext with the specified modifications.
        """
        if not isinstance(resource or self.currentResource, ResourceRef) and isinstance(
            self._lastResource, ResourceRef
        ):
            resource = self._lastResource
        copy = self.__class__(
            resource or self.currentResource,
            self.vars,
            self.wantList,
            self.resolveExternal,
            max(self._trace, trace),
            self._strict if strict is None else strict,
        )
        if vars:
            copy.vars = copy.vars.copy()
            copy.vars.update(vars)
        if wantList is not None:
            copy.wantList = wantList
        base_dir = getattr(self.kw, "base_dir", None)
        if base_dir is not None:
            copy.base_dir = base_dir
        else:
            copy.base_dir = self.base_dir
        copy.templar = self.templar
        copy.referenced = self.referenced
        copy.task = self.task

        if tosca_type:
            copy.tosca_type = tosca_type
        else:
            copy.tosca_type = self.tosca_type
        return copy

    def trace(self, *msg: Any) -> None:
        if self._trace:
            log = logger.info if self._trace >= 2 else logger.trace
            log(
                f"{' '.join(str(a) for a in msg)} (ctx: {self._lastResource} {self.tosca_type and self.tosca_type.type or ''})"
            )  # type: ignore

    def add_external_reference(self, external: ExternalValue) -> Result:
        result = Result(external)
        self.referenced.add_result_reference(None, result)
        return result

    def add_ref_reference(self, ref: "Ref", result: List[Result]) -> None:
        self.referenced.add_ref_reference(ref, result)

    def add_result_reference(self, ref: str, result: Result) -> None:
        self.referenced.add_result_reference(ref, result)

    def resolve_var(self, key: str) -> Any:  # Result asserts resolved is not a Result
        return self._resolve_var(key[1:]).resolved

    def _resolve_var(self, key: str) -> Result:
        value = self.vars[key]
        if isinstance(value, Result):
            return value
        else:
            # lazily resolve maps and lists to avoid circular references
            val = Results._map_value(value, self)
            if not isinstance(val, Result):
                val = Result(val)
            self.vars[key] = val
            return val

    def resolve_reference(
        self, key: str
    ) -> Union[Results, ResultsList, List[Result], ResultsMap]:
        val = self._resolve_var(key)
        self.add_result_reference(key, val)
        assert not isinstance(val.resolved, Result)
        return val.resolved

    def query(
        self,
        expr: Union[str, Mapping],
        vars: Optional[dict] = None,
        wantList: Union[bool, Literal["result"]] = False,
    ) -> Union[List[Result], List[Any], ResolveOneUnion]:
        return Ref(expr, vars).resolve(self, wantList)

    def __getstate__(self) -> Dict[str, Any]:
        # Remove the unpicklable entries.
        state = self.__dict__.copy()
        state["templar"] = None
        state["task"] = None
        del state["referenced"]
        return state

    def __setstate__(self, d: dict) -> None:
        self.__dict__ = d
        self.referenced = _Tracker()

    def __getattr__(self, key):
        func = self._Funcs.get(key)
        if not func:
            raise AttributeError(
                f"'{self.__class__.__name__}' object has no attribute '{key}'"
            )

        def _eval_func(*args, **kw):
            self.currentFunc = key
            self.kw = kw  # XXX set base_dir to preserve
            assert func
            return func(args, self)

        return _eval_func


class Expr:
    def __init__(self, exp, vars=None):
        self.vars = {"true": True, "false": False, "null": None}

        if vars:
            self.vars.update(vars)

        self.source = exp
        paths = list(parse_exp(exp))
        if "break" not in self.vars:
            # hack to check that we aren't a foreach expression
            if (not paths[0].key or paths[0].key[0] not in ".$") and (
                paths[0].key or paths[0].filters
            ):
                # unspecified relative path: prepend a segment to select ancestors
                # and have the first segment only choose the first match
                # note: the first match modifier is set on the first segment and not .ancestors
                # because .ancestors is evaluated against the initial context resource and so
                # its first match is always going to be the result of the whole query (since the initial context is just one item)
                paths[:1] = [
                    Segment(".ancestors", [], "", []),
                    paths[0]._replace(modifier="?"),
                ]
        self.paths = paths

    def __repr__(self):
        # XXX vars
        return f"Expr('{self.source}')"

    def resolve(self, context: RefContext) -> List[Result]:
        currentResource = context.currentResource
        if not self.paths[0].key and not self.paths[0].filters:  # starts with "::"
            currentResource = currentResource.all
            paths = self.paths[1:]
        elif self.paths[0].key and self.paths[0].key[0] == "$":
            # if starts with a var, use that as the start
            try:
                currentResource = context.resolve_var(self.paths[0].key)
            except KeyError:
                context.trace("initial variable in expression not found", self.source)
                return []
            if len(self.paths) == 1:
                # bare reference to a var, just return it's value
                return [Result(currentResource)]
            paths = [self.paths[0]._replace(key="")] + self.paths[1:]
        else:
            paths = self.paths
        return eval_exp([currentResource], paths, context)


class Ref:
    """A Ref objects describes a path to metadata associated with a resource."""

    def __init__(
        self,
        exp: Union[str, Mapping],
        vars: Optional[dict] = None,
        trace: Optional[int] = None,
    ) -> None:
        self.vars = {"true": True, "false": False, "null": None}

        self.foreach = None
        self.select = None
        self.strict = None
        self.validation = None
        trace = RefContext.DefaultTraceLevel if trace is None else trace
        self.trace = trace
        if isinstance(exp, Mapping):
            keys = list(exp)
            if keys and keys[0] not in _FuncsTop:
                self.vars.update(exp.get("vars", {}))
                self.foreach = exp.get("foreach")
                self.select = exp.get("select")
                _trace = exp.get("trace", trace)
                if _trace == "break":
                    self.trace = trace
                    breakpoint()
                else:
                    self.trace = _trace
                strict = exp.get("strict")
                if strict in ["required", "notnull"]:
                    self.validation = strict
                else:
                    self.strict = strict
                exp = exp.get("eval", exp.get("ref", exp))

        if vars:
            self.vars.update(vars)
        self.source = exp

    @overload
    def resolve(
        self,
        ctx: RefContext,
        wantList: Literal[True] = True,
        strict: Optional[bool] = None,
    ) -> List[Any]: ...

    @overload
    def resolve(
        self,
        ctx: RefContext,
        wantList: Literal[False],
        strict: Optional[bool] = None,
    ) -> ResolveOneUnion: ...

    @overload
    def resolve(
        self,
        ctx: RefContext,
        wantList: Literal["result"],
        strict: Optional[bool] = None,
    ) -> List[Result]: ...

    @overload
    def resolve(
        self,
        ctx: RefContext,
        wantList: Union[bool, Literal["result"]] = True,
        strict: Optional[bool] = None,
    ) -> Union[List[Result], List[Any], ResolveOneUnion]: ...

    def resolve(
        self,
        ctx: RefContext,
        wantList: Union[bool, Literal["result"]] = True,
        strict: Optional[bool] = None,
    ) -> Union[List[Result], List[Any], ResolveOneUnion]:
        """
        Given an expression, returns a value, a list of values, or or list of Result, depending on ``wantList``:

        If wantList=True (default), return a list of matches.

        Note that values in the list can be a list or None.

        If wantList=False, return `resolve_one` semantics.

        If wantList='result', return a list of Result.
        """
        if self.strict is not None:
            # overrides RefContext's strict
            strict = self.strict
        ctx = ctx.copy(
            vars=self.vars, wantList=wantList, trace=self.trace, strict=strict
        )
        base_dir = getattr(self.source, "base_dir", "")
        if base_dir:
            ctx.base_dir = base_dir
        # $start is set in eval_ref but the user use that to override currentResource
        if "start" in self.vars:
            ctx.currentResource = ctx.resolve_var("$start")
        ctx.trace(
            f"Ref.resolve(wantList={wantList}) start strict {ctx.strict}",
            self.source,
        )
        ref_results = eval_ref(self.source, ctx, True)
        assert isinstance(ref_results, list)
        ctx.trace(f"Ref.resolve(wantList={wantList}) evalRef", self.source, ref_results)
        select = self.foreach or self.select
        if ref_results and select:
            results = for_each(select, ref_results, ctx)
        else:
            results = ref_results  # , ctx)
        ctx.add_ref_reference(self, results)
        ctx.trace(f"Ref.resolve(wantList={wantList}) results", self.source, results)
        if wantList == "result":
            return results
        values = [r.resolved for r in results]
        if wantList or self.foreach:
            # foreach always returns a list
            return values
        # resolve_one semantics
        if not values:
            if self.validation:
                raise UnfurlError(f"Expression {self.source} must return results")
            return None
        elif len(values) == 1:
            if self.validation == "notnull" and values[0] is None:
                raise UnfurlError(f"Expression {self.source} must not be null")
            return values[0]
        else:
            return values

    def resolve_one(
        self,
        ctx: RefContext,
        strict: Optional[bool] = None,
    ) -> ResolveOneUnion:
        """
        Given an expression, return a value, None or a list of values.

        If there is no match, return None.

        If there is more than one match, return a list of matches.

        Otherwise return the match.

        Note: If you want to distinguish between None values and no match
        or between single match that is a list and a list of matches
        use `Ref.resolve`, which always returns a (possible empty) of matches
        """
        return self.resolve(ctx, False, strict)

    @staticmethod
    def is_ref(value: Union[Mapping, "Ref"]) -> bool:
        """
        Return true if the given value looks like a Ref.
        """
        if isinstance(value, Mapping):
            if not value:
                return False
            first = next(iter(value))

            if "ref" in value or "eval" in value:
                return len([
                    x
                    for x in ["vars", "trace", "foreach", "select", "strict"]
                    if x in value
                ]) + 1 == len(value)
            if len(value) == 1 and first in _FuncsTop:
                return True
            return False
        return isinstance(value, Ref)


def eval_as_boolean(arg, ctx):
    if ctx.kw.get("map_value"):
        return bool(map_value(arg, ctx))
    else:
        result = eval_ref(arg, ctx)
    return not not result[0].resolved if result else False


def if_func(arg, ctx):
    kw = ctx.kw
    result = eval_as_boolean(arg, ctx)
    if result:
        if "then" in kw:
            return eval_for_func(kw["then"], ctx)
        else:
            return result
    else:
        if "else" in kw:
            return eval_for_func(kw["else"], ctx)
        else:
            return result


def or_func(arg, ctx):
    args = eval_for_func(arg, ctx)
    assert_form(args, MutableSequence)
    for arg in args:
        val = eval_for_func(arg, ctx)
        if val:
            return val
    return False


def not_func(arg, ctx):
    result = eval_as_boolean(arg, ctx)
    return not result


def and_func(arg, ctx):
    args = eval_for_func(arg, ctx)
    assert_form(args, MutableSequence)
    val = False
    for arg in args:
        val = eval_for_func(arg, ctx)
        if not val:
            return val
    return val


def quote_func(arg, ctx):
    return arg


def func_defined_func(arg, ctx):
    return map_value(arg, ctx) in ctx._Funcs


def eq_func(arg, ctx):
    args = map_value(arg, ctx)
    assert_form(args, MutableSequence, len(args) == 2)
    return args[0] == args[1]


def validate_schema_func(arg, ctx):
    args = map_value(arg, ctx)
    assert_form(args, MutableSequence, len(args) == 2)
    return validate_schema(args[0], args[1])


if sys.version_info >= (3, 9):
    MutableSequence_Result = MutableSequence[Result]
else:
    MutableSequence_Result = MutableSequence

ResultOrResultList = Union[Result, MutableSequence_Result]
PairResultOrResultList = Tuple[Any, ResultOrResultList]
MaybePairOrResultOrResultList = Union[PairResultOrResultList, ResultOrResultList]


def _for_each(
    foreach: Union[MappingType[str, Any], str],
    results: Iterable[Tuple[Any, Any]],
    ctx: RefContext,
) -> List[Result]:
    if isinstance(foreach, str):
        keyExp: Union[Mapping, str, None] = None
        valExp: Union[Mapping, str, None] = foreach
    else:
        keyExp = foreach.get("key")
        valExp = foreach.get("value")
        if not valExp and not keyExp:
            # it's a dict that needs to be evaluated
            valExp = foreach

    ictx = ctx.copy(wantList=False)
    # ictx._trace = 1
    Break = object()
    Continue = object()

    def make_items() -> Iterable[MaybePairOrResultOrResultList]:
        for i, (k, v) in enumerate(results):
            # XXX stop setting any kind of value to currentResource
            # assert isinstance(v, ResourceRef)
            ictx.currentResource = v
            ictx.vars["collection"] = results
            ictx.vars["index"] = i
            ictx.vars["key"] = k
            ictx.vars["item"] = v
            ictx.vars["break"] = Break
            ictx.vars["continue"] = Continue
            if keyExp:
                key = eval_for_func(keyExp, ictx)
                if key is Break:
                    break
                elif key is Continue:
                    continue
            assert valExp
            if isinstance(valExp, str):
                evalResults = eval_ref(valExp, ictx)
            else:
                evalResults = _eval_ref_results(valExp, ictx)
            if not evalResults:
                continue
            if len(evalResults) == 1:
                val = evalResults[0].resolved
                if val is Break:
                    break
                elif val is Continue:
                    continue
                valResult: Union[Result, List[Result]] = evalResults[0]
            else:
                valResult = evalResults

            if keyExp:
                yield (key, valResult)
            else:
                yield valResult

    if keyExp:
        return [Result(CommentedMap(cast(Iterable[Tuple[Any, Result]], make_items())))]
    else:
        return list(cast(Iterable[Result], make_items()))


def for_each(
    foreach: Union[Mapping, str], results: List[Result], ctx: RefContext
) -> List[Result]:
    if foreach == "$true":
        return results
    if len(results) == 1 and isinstance(results[0].resolved, MutableSequence):
        result = _for_each(foreach, enumerate(results[0].resolved), ctx)  # hack!
    else:
        result = _for_each(
            foreach, enumerate(r.external or r.resolved for r in results), ctx
        )
    return result


def for_each_func(foreach: Union[Mapping, str], ctx: RefContext) -> List[Result]:
    results = ctx.currentResource
    if isinstance(results, Mapping):
        return _for_each(foreach, results.items(), ctx)
    elif isinstance(results, MutableSequence):
        return _for_each(foreach, enumerate(results), ctx)
    else:
        return _for_each(foreach, [(0, results)], ctx)


_CoreFuncs = {
    "if": if_func,
    "and": and_func,
    "or": or_func,
    "not": not_func,
    "q": quote_func,
    "eq": eq_func,
    "validate_json": validate_schema_func,
    "foreach": for_each_func,
    "is_function_defined": func_defined_func,
}


def _make_op_func(op):
    op_func = getattr(operator, op)
    return lambda arg, ctx: op_func(*map_value(arg, ctx))


for op in "ne gt ge lt le add mul pow truediv floordiv mod sub".split():
    _CoreFuncs[op] = _make_op_func(op)
_CoreFuncs["div"] = _CoreFuncs["truediv"]
RefContext._Funcs = _CoreFuncs.copy()
_FuncsTop = ["q"]


class SafeRefContext(RefContext):
    _Funcs = _CoreFuncs.copy()


class AnyRef(ResourceRef):
    "Used by eval.analyze_expr to analyze expressions"

    def __init__(self, name: str, parent=None):
        self.parent = parent
        while parent:
            if not parent.parent:
                parent.children.append(self)
                break
            else:
                parent = parent.parent
        self._key = name
        self.children: List[AnyRef] = []

    @property
    def key(self):
        return self._key

    class _ChildResources(Mapping):
        def __init__(self, resource):
            self.parent = resource

        def __getitem__(self, key):
            return AnyRef("::" + key, self.parent)

        def __iter__(self):
            return iter(["*"])

        def __len__(self):
            return 1

    @property
    def all(self):
        # called by Expr.resolve when expression starts with "::"
        return AnyRef._ChildResources(self)

    def _get_prop(self, name: str) -> Optional["AnyRef"]:
        if name == ".":
            return self
        elif name == "..":
            return cast(AnyRef, self.parent)
        return AnyRef(name, self)

    def _resolve(self, key):
        return AnyRef(key, self)

    def get_keys(self) -> List[str]:
        head = self
        while head.parent:
            head = cast(AnyRef, head.parent)
        return [head.key] + [c.key for c in head.children]

    def __repr__(self) -> str:
        return f"AnyRef({hex(id(self))} {self.key})"

    def __eq__(self, other):
        # assumes this is called from an expression's filter test and we want to proceed for analysis
        if isinstance(other, AnyRef):
            if self.key == ".name":
                self._key = other.key
            return True
        return False

    def __ne__(self, other):
        # assumes this is called from an expression's filter test and we want to proceed for analysis
        return True


def analyze_expr(expr, var_list=(), ctx_cls=SafeRefContext) -> Optional["AnyRef"]:
    start = AnyRef("$start")
    # use SafeRefContext to avoid side effects
    ctx = ctx_cls(start, vars={n: AnyRef(n) for n in var_list})
    ctx._strict = False
    try:
        ExceptionCollector.pause()
        result = Ref(expr).resolve(ctx)
    except:
        return ctx.currentResource  # type: ignore
    finally:
        ExceptionCollector.resume()
    # return the last AnyRef
    if result and isinstance(result[-1], AnyRef):
        return result[-1]
    return ctx.currentResource  # type: ignore


def get_eval_func(name):
    return RefContext._Funcs.get(name)


EvalFunc = Callable[[Any, RefContext], Any]


def set_eval_func(name, val: EvalFunc, topLevel=False, safe=False):
    RefContext._Funcs[name] = val
    if topLevel:
        _FuncsTop.append(name)
    if safe:
        SafeRefContext._Funcs[name] = val


def eval_ref(
    val: Union[Mapping, str], ctx: RefContext, top: bool = False
) -> List[Result]:
    "Evaluate and return a list of Result. ``val`` is assumed to be an expression."
    from .support import is_template, apply_template

    # functions and ResultsMap assume resolve_one semantics
    if top:
        vars = ctx.vars.copy()
        vars["start"] = ctx.currentResource
        ctx = ctx.copy(ctx.currentResource, vars, wantList=False)

    if isinstance(val, Mapping):
        for key in val:
            func = ctx._Funcs.get(key)
            if func:
                args = val[key]
                ctx.kw = val
                ctx.currentFunc = key
                if "var" in val:
                    unexpected: Union[bool, str] = "var"
                elif key != "foreach" and "foreach" in val:
                    unexpected = "foreach"
                else:
                    unexpected = False
                if unexpected:
                    raise UnfurlEvalError(
                        f"unexpected '{unexpected}' found, did you intend it for the parent?"
                    )
                val = func(args, ctx)
                if key == "q":
                    if isinstance(val, Result):
                        return [val]
                    else:
                        return [Result(val)]
                break
        else:
            if "ref" not in val and "eval" not in val:
                if isinstance(ctx, SafeRefContext):
                    msg = f"function unsafe or missing in {dict(val)}"
                    if not ctx.strict:
                        logger.warning(
                            "In safe mode, skipping unsafe eval: " + msg,
                            stack_info=True,
                        )
                        return [Result("Error: in safe mode, skipping unsafe eval")]
                    else:
                        msg = "Error: unsafe eval: " + msg
                else:
                    msg = f"Function missing in {dict(val)}"
                raise UnfurlEvalError(msg)
    elif isinstance(val, str):
        if is_template(val, ctx):
            return [Result(apply_template(val, ctx))]
        else:
            expr = Expr(val, ctx.vars)
            results = expr.resolve(ctx)  # returns a list of Result
            ctx.trace("expr.resolve", results)
            return results
    return _eval_ref_results(val, ctx)


def _eval_ref_results(val, ctx) -> List[Result]:
    mappedVal = Results._map_value(val, ctx)
    if isinstance(mappedVal, ResultsList):
        return [r if isinstance(r, Result) else Result(r) for r in mappedVal]
    if isinstance(mappedVal, Result):
        return [mappedVal]
    elif mappedVal is None:
        return []
    else:
        return [Result(mappedVal)]


def eval_for_func(val, ctx) -> Any:
    "like `eval_ref` except it returns the resolved value"
    if ctx.kw.get("map_value"):
        return map_value(val, ctx)
    else:
        results = eval_ref(val, ctx)
    if not results:
        return None
    if len(results) == 1:
        return results[0].resolved
    else:
        return [r.resolved for r in results]


# return a segment
Segment = collections.namedtuple("Segment", ["key", "test", "modifier", "filters"])
defaultSegment = Segment("", [], "", [])


def eval_test(value, test, context) -> bool:
    comparor = test[0]
    key = test[1]
    try:
        if context and isinstance(key, str) and key.startswith("$"):
            compare = context.resolve_var(key)
        else:
            # try to coerce string to value type
            compare = type(value)(key)
        context.trace("compare", value, compare, comparor(value, compare))
        if comparor(value, compare):
            return True
    except:
        context.trace("compare exception, ne:", comparor is operator.ne)
        if comparor is operator.ne:
            return True
    return False


def lookup(result: Result, key: Any, context: RefContext) -> Optional[Result]:
    try:
        # if key == '.':
        #   key = context.currentKey
        if context and isinstance(key, str) and key.startswith("$"):
            key = context.resolve_var(key)

        if isinstance(result.resolved, ResourceRef):
            context._lastResource = result.resolved

        ctx = context.copy(context._lastResource)
        result = result.project(key, ctx)
        value = result.resolved
        context.trace(f"lookup {key}, got value of type {type(value)}")

        if not context._rest:
            assert not Ref.is_ref(value)
            result.resolved = Results._map_value(value, ctx)
            assert not isinstance(result.resolved, (ExternalValue, Result))

        return result
    except (KeyError, IndexError, TypeError, ValueError):
        if context._trace:
            import traceback

            context.trace(
                f"lookup of '{key}' returned None due to exception:\n",
                traceback.format_exc(),
            )
        return None


# given a Result, yields the result
def eval_item(result, seg, context):
    """
    apply current item to current segment, return [] or [value]
    """
    if seg.key != "":
        result = lookup(result, seg.key, context)
        if not result:
            return

    value = result.resolved
    for filter in seg.filters:
        if _treat_as_singular(result, filter[0]):
            resultList = [value]
        else:
            resultList = value
        results = eval_exp(resultList, filter, context)
        negate = filter[0].modifier == "!"
        if negate and results:
            return
        elif not negate and not results:
            return

    if seg.test and not eval_test(value, seg.test, context):
        return
    assert isinstance(result, Result), result
    yield result


def _treat_as_singular(result, seg):
    if seg.key == "*":
        return False
    # treat external values as single item even if they resolve to a list
    # treat lists as a single item if indexing into it
    return (
        result.external
        or not isinstance(result.resolved, MutableSequence)
        or isinstance(seg.key, int)
    )


def recursive_eval(v, exp, context):
    """
    given a iterator of (previous) Result,
    yields Result
    """
    context.trace("recursive evaluating", exp)
    matchFirst = exp[0].modifier == "?"
    useValue = exp[0].key == "*"
    for result in v:
        assert isinstance(result, Result), result
        item = result.resolved

        if _treat_as_singular(result, exp[0]):
            rest = exp[1:]
            context._rest = rest
            context.trace(f"evaluating item {item} with key {exp[0].key}")
            iv = eval_item(
                result, exp[0], context
            )  # returns a generator that yields up to one result
        else:
            iv = result._values()
            if useValue:
                if not isinstance(item, Mapping):
                    context.trace("* is skipping", item)
                    continue
                rest = exp[1:]  # advance past "*" segment
            else:
                # flattens
                rest = exp
                context.trace("flattening", item, "rest:", rest)

        # iv will be a generator or list
        if rest:
            results = recursive_eval(iv, rest, context)
            found = False
            for r in results:
                found = True
                context.trace("recursive result", r)
                assert isinstance(r, Result), r
                yield r
            context.trace(f"found recursive {found} matchFirst: {matchFirst}")
            if found and matchFirst:
                return
        else:
            for r in iv:
                assert isinstance(r, Result), r
                yield r
                if matchFirst:
                    return


def eval_exp(start, paths, context) -> List[Result]:
    "Returns a list of Result"
    context.trace("evalexp", start, paths)
    assert_form(start, MutableSequence)
    return list(recursive_eval((Result(i) for i in start), paths, context))


def _make_key(key):
    try:
        return int(key)
    except ValueError:
        return key


def parse_path_key(segment):
    # key, negation, test, matchFirst
    if not segment:
        return defaultSegment

    modifier = ""
    if segment[0] == "!":
        segment = segment[1:]
        modifier = "!"
    elif segment[-1] == "?":
        segment = segment[:-1]
        modifier = "?"

    parts = re.split(r"(=|!=)", segment, maxsplit=1)
    if len(parts) == 3:
        key = parts[0]
        op = operator.eq if parts[1] == "=" else operator.ne
        return Segment(_make_key(key), [op, parts[2]], modifier, [])
    else:
        return Segment(_make_key(segment), [], modifier, [])


def parse_path(path, start):
    paths = path.split("::")
    segments = [parse_path_key(k.strip()) for k in paths]
    if start:
        if paths and paths[0]:
            # if the path didn't start with ':' merge with the last segment
            # e.g. foo[]? d=test[d]?
            segments[0] = start._replace(
                test=segments[0].test or start.test,
                modifier=segments[0].modifier or start.modifier,
            )
        else:
            return [start] + segments
    return segments


def parse_exp(exp):
    # return list of steps
    rest = exp
    last = None

    while rest:
        steps, rest = parse_step(rest, last)
        last = None
        if steps:
            # we might need merge the next step into the last
            last = steps.pop()
            for step in steps:
                yield step

    if last:
        yield last


def parse_step(exp, start=None):
    split = re.split(r"(\[|\])", exp, maxsplit=1)
    if len(split) == 1:  # not found
        return parse_path(split[0], start), ""
    else:
        path, sep, rest = split

    paths = parse_path(path, start)

    filterExps = []
    while sep == "[":
        filterExp, rest = parse_step(rest)
        filterExps.append(filterExp)
        # rest will be anything after ]
        sep = rest and rest[0]

    # add filterExps to last Segment
    paths[-1] = paths[-1]._replace(filters=filterExps)
    return paths, rest
