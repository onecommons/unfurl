# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Public Api:

map_value - returns a copy of the given value resolving any embedded queries or template strings

Ref.resolve given an expression, returns a ResultList
Ref.resolve_one given an expression, return value, none or a (regular) list
Ref.is_ref return true if the given diction looks like a Ref

Internal:

eval_ref() given expression (string or dictionary) return list of Result
Expr.resolve() given expression string, return list of Result
Results._map_value same as map_value but with lazily evaluation
"""
from functools import partial
from typing import (
    Any,
    Dict,
    List,
    Tuple,
    Optional,
    Union,
    Iterable,
    cast,
    TYPE_CHECKING,
)
from typing import Mapping as MappingType
import six
import re
import operator
import sys
import collections
from collections.abc import Mapping, MutableSequence
from ruamel.yaml.comments import CommentedMap

from unfurl.logs import UnfurlLogger

from .util import validate_schema, UnfurlError, assert_form
from .result import ResultsList, Result, Results, ExternalValue, ResourceRef, ResultsMap

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
) -> Any:
    """Resolves any expressions or template strings embedded in the given map or list.
    """
    if not isinstance(resourceOrCxt, RefContext):
        resourceOrCxt = RefContext(resourceOrCxt)
    return _map_value(value, resourceOrCxt, False, applyTemplates)


def _map_value(
    value: Any, ctx: "RefContext", wantList: bool = False, applyTemplates: bool = True
) -> Any:
    from .support import is_template, apply_template

    if Ref.is_ref(value):
        # wantList=False := resolve_one
        return Ref(value).resolve(ctx, wantList=wantList)

    if isinstance(value, Mapping):
        try:
            oldBaseDir = ctx.base_dir
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
        return [_map_value(item, ctx, wantList, applyTemplates) for item in value]
    elif applyTemplates and is_template(value, ctx):
        return apply_template(value, ctx)
    return value


class _Tracker:
    def __init__(self):
        self.count = 0
        self.referenced = []

    def start(self):
        self.count += 1
        return len(self.referenced)

    def stop(self):
        self.count -= 1
        return self.count

    def add_ref_reference(self, ref: "Ref", result: ResultsList):
        if self.count > 0:
            self.referenced.append([ref, result])

    def add_result_reference(self, ref: Union[str, None], result: Result):
        if self.count > 0:
            self.referenced.append([ref, result])

    def getReferencedResults(self, index=0):
        referenced = self.referenced[index:]
        if not referenced:
            return
        for (ref, results) in referenced:
            if not isinstance(ref, Ref):
                assert isinstance(results, Result)
                yield results
            else:
                for obj in results._attributes:
                    assert isinstance(obj, Result)
                    yield obj


_defaultStrictness = True


class RefContext:
    """
    The context of the expression being evaluated.
    """

    DefaultTraceLevel = 0
    _Funcs: Dict = {}

    def __init__(
        self,
        currentResource: "ResourceRef",
        vars: Optional[dict] = None,
        wantList: Optional[Union[bool, str]] = False,
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
        self.kw: MappingType[str, Any] = {}

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
        vars: dict = None,
        wantList: Optional[Union[bool, str]] = None,
        trace: int = 0,
        strict: bool = None,
    ) -> "RefContext":
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
        copy.base_dir = self.base_dir
        copy.templar = self.templar
        copy.referenced = self.referenced
        copy.task = self.task
        return copy

    def trace(self, *msg: Any) -> None:
        if self._trace:
            log = logger.info if self._trace >= 2 else logger.trace
            log(f"{' '.join(str(a) for a in msg)} (ctx: {self._lastResource})")  # type: ignore

    def add_external_reference(self, external: ExternalValue) -> Result:
        result = Result(external)
        self.referenced.add_result_reference(None, result)
        return result

    def add_ref_reference(self, ref: "Ref", result: ResultsList) -> None:
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
        wantList: Union[bool, str] = False,
    ) -> Optional[Union[ResultsList, Result, List[Result]]]:
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
            raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{key}'")

        def _eval_func(*args, **kw):
            self.currentFunc = key
            self.kw = kw
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

    def resolve(self, context) -> List[Result]:
        # returns a list of Result
        currentResource = context.currentResource
        if not self.paths[0].key and not self.paths[0].filters:  # starts with "::"
            currentResource = currentResource.all
            paths = self.paths[1:]
        elif self.paths[0].key and self.paths[0].key[0] == "$":
            # if starts with a var, use that as the start
            currentResource = context.resolve_var(self.paths[0].key)
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
        self, exp: Union[str, Mapping], vars: dict = None, trace: Optional[int] = None
    ) -> None:
        self.vars = {"true": True, "false": False, "null": None}

        self.foreach = None
        self.select = None
        self.strict = None
        trace = RefContext.DefaultTraceLevel if trace is None else trace
        self.trace = trace
        if isinstance(exp, Mapping):
            keys = list(exp)
            if keys and keys[0] not in _FuncsTop:
                self.vars.update(exp.get("vars", {}))
                self.foreach = exp.get("foreach")
                self.select = exp.get("select")
                self.trace = exp.get("trace", trace)
                self.strict = exp.get("strict")
                exp = exp.get("eval", exp.get("ref", exp))

        if vars:
            self.vars.update(vars)
        self.source = exp

    def resolve(
        self,
        ctx: RefContext,
        wantList: Union[bool, str] = True,
        strict: Optional[bool] = None,
    ) -> Optional[Union[ResultsList, Result, List[Result], Any]]:
        """
        If wantList=True (default) returns a ResultList of matches
        Note that values in the list can be a list or None
        If wantList=False return `resolve_one` semantics
        If wantList='result' return a Result
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
            results = ResultsList(ref_results, ctx)
        ctx.add_ref_reference(self, results)
        ctx.trace(f"Ref.resolve(wantList={wantList}) results", self.source, results)
        if self.foreach:
            # foreach always returns a list
            if wantList == "result":
                return Result(results)
            else:
                return results
        if wantList and not wantList == "result":
            return results
        else:
            if not results:
                return None
            elif len(results) == 1:
                if wantList == "result":
                    return results._attributes[0]
                else:
                    return results[0]
            else:
                if wantList == "result":
                    return Result(results)
                else:
                    return list(results)

    def resolve_one(
        self, ctx: RefContext,
        strict: Optional[bool] = None,
    ) -> Optional[Union[ResultsList, Result, List[Result]]]:
        """
        If no match return None
        If more than one match return a list of matches
        Otherwise return the match

        Note: If you want to distinguish between None values and no match
        or between single match that is a list and a list of matches
        use resolve() which always returns a (possible empty) of matches
        """
        return self.resolve(ctx, False, strict)

    @staticmethod
    def is_ref(value: Union[Mapping, "Ref"]) -> bool:
        if isinstance(value, Mapping):
            if not value:
                return False
            first = next(iter(value))

            if "ref" in value or "eval" in value:
                return len(
                    [x for x in ["vars", "trace", "foreach", "select", "strict"] if x in value]
                ) + 1 == len(value)
            if len(value) == 1 and first in _FuncsTop:
                return True
            return False
        return isinstance(value, Ref)


def eval_as_boolean(arg, ctx):
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
    assert_form(args, MutableSequence, len(args) == 2)  # type: ignore
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
) -> List[ResultOrResultList]:
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
                valResult: ResultOrResultList = evalResults[0]
            else:
                valResult = evalResults

            if keyExp:
                yield (key, valResult)
            else:
                yield valResult

    if keyExp:
        return [
            Result(CommentedMap(cast(Iterable[PairResultOrResultList], make_items())))
        ]
    else:
        return list(cast(Iterable[ResultOrResultList], make_items()))


def for_each(
    foreach: Union[Mapping, str], results: list, ctx: RefContext
) -> ResultsList:
    if len(results) == 1 and isinstance(results[0].resolved, MutableSequence):
        result = _for_each(foreach, enumerate(results[0].resolved), ctx)  # hack!
    else:
        result = _for_each(
            foreach, enumerate(r.external or r.resolved for r in results), ctx
        )
    return ResultsList(result, ctx)  # ResultsList[List[ResultOrResultList]]]


def for_each_func(
    foreach: Union[Mapping, str], ctx: RefContext
) -> List[ResultOrResultList]:
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
    "validate": validate_schema_func,
    "foreach": for_each_func,
    "is_function_defined": func_defined_func,
}
RefContext._Funcs = _CoreFuncs.copy()
_FuncsTop = ["q"]


class SafeRefContext(RefContext):
    _Funcs = _CoreFuncs.copy()


def get_eval_func(name):
    return RefContext._Funcs.get(name)


def set_eval_func(name, val, topLevel=False, safe=False):
    RefContext._Funcs[name] = val
    if topLevel:
        _FuncsTop.append(name)
    if safe:
        SafeRefContext._Funcs[name] = val


def eval_ref(
    val: Union[Mapping, str], ctx: RefContext, top: bool = False
) -> List[Result]:
    "val is assumed to be an expression, evaluate and return a list of Result"
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
                ctx.currentFunc = key  # type: ignore
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
                        logger.warning("In safe mode, skipping unsafe eval: " + msg, stack_info=True)
                        return [Result("Error: in safe mode, skipping unsafe eval")]
                    else:
                        msg = "Error: unsafe eval: " + msg
                else:
                    msg = f"Function missing in {dict(val)}"
                raise UnfurlEvalError(msg)
    elif isinstance(val, six.string_types):
        if is_template(val, ctx):
            return [Result(apply_template(val, ctx))]
        else:
            expr = Expr(val, ctx.vars)
            results = expr.resolve(ctx)  # returns a list of Result
            ctx.trace("expr.resolve", results)
            return results
    return _eval_ref_results(val, ctx)


def _eval_ref_results(val, ctx):
    mappedVal = Results._map_value(val, ctx)
    if isinstance(mappedVal, Result):
        return [mappedVal]
    else:
        return [Result(mappedVal)]


def eval_for_func(val, ctx) -> Any:
    "like `eval_ref` except it returns the resolved value"
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
        if context and isinstance(key, six.string_types) and key.startswith("$"):
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
        context.trace(f"lookup {key}, got {value}")

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
        or isinstance(seg.key, six.integer_types)
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
                context.trace("flattening", item)

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

    parts = re.split(r"(=|!=)", segment, 1)
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
    split = re.split(r"(\[|\])", exp, 1)
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
