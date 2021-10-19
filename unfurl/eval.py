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
import six
import re
import operator
import collections
from collections.abc import Mapping, MutableSequence
from ruamel.yaml.comments import CommentedMap
from .util import validate_schema, UnfurlError, assert_form
from .result import ResultsList, Result, Results, ExternalValue, ResourceRef

import logging

logger = logging.getLogger("unfurl.eval")


def map_value(value, resourceOrCxt, applyTemplates=True):
    if not isinstance(resourceOrCxt, RefContext):
        resourceOrCxt = RefContext(resourceOrCxt)
    return _map_value(value, resourceOrCxt, False, applyTemplates)


def _map_value(value, ctx, wantList=False, applyTemplates=True):
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

    def addReference(self, ref, result):
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

    def __init__(
        self,
        currentResource,
        vars=None,
        wantList=False,
        resolveExternal=False,
        trace=None,
        strict=_defaultStrictness,
        task=None,
    ):
        self.vars = vars or {}
        # the original context:
        self.currentResource = currentResource
        # the last resource encountered while evaluating:
        self._lastResource = currentResource
        # current segment is the final segment:
        self._rest = None
        self.wantList = wantList
        self.resolveExternal = resolveExternal
        self._trace = self.DefaultTraceLevel if trace is None else trace
        self.strict = strict
        self.base_dir = currentResource.base_dir
        self.templar = currentResource.templar
        self.referenced = _Tracker()
        self.task = task

    def copy(self, resource=None, vars=None, wantList=None, trace=0, strict=None):
        copy = RefContext(
            resource or self.currentResource,
            self.vars,
            self.wantList,
            self.resolveExternal,
            max(self._trace, trace),
            self.strict if strict is None else strict,
        )
        if not isinstance(copy.currentResource, ResourceRef) and isinstance(
            self._lastResource, ResourceRef
        ):
            copy._lastResource = self._lastResource
        if vars:
            copy.vars.update(vars)
        if wantList is not None:
            copy.wantList = wantList
        copy.base_dir = self.base_dir
        copy.templar = self.templar
        copy.referenced = self.referenced
        copy.task = self.task
        return copy

    def trace(self, *msg):
        if self._trace:
            logger.trace(f"{' '.join(str(a) for a in msg)} (ctx: {self._lastResource})")

    def add_external_reference(self, external):
        result = Result(external)
        self.referenced.addReference(None, result)
        return result

    def add_reference(self, ref, result):
        self.referenced.addReference(ref, result)

    def resolve_var(self, key):
        return self._resolve_var(key[1:]).resolved

    def _resolve_var(self, key):
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

    def resolve_reference(self, key):
        val = self._resolve_var(key)
        self.add_reference(key, val)
        assert not isinstance(val.resolved, Result)
        return val.resolved

    def query(self, expr, vars=None, wantList=False):
        return Ref(expr, vars).resolve(self, wantList)

    def __getstate__(self):
        # Remove the unpicklable entries.
        state = self.__dict__.copy()
        state["templar"] = None
        state["task"] = None
        del state["referenced"]
        return state

    def __setstate__(self, d):
        self.__dict__ = d
        self.referenced = _Tracker()


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

    def resolve(self, context):
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

    def __init__(self, exp, vars=None):
        self.vars = {"true": True, "false": False, "null": None}

        self.foreach = None
        self.trace = 0
        if isinstance(exp, Mapping):
            keys = list(exp)
            if keys and keys[0] not in _FuncsTop:
                self.vars.update(exp.get("vars", {}))
                self.foreach = exp.get("foreach", exp.get("select"))
                self.trace = exp.get("trace", 0)
                exp = exp.get("eval", exp.get("ref", exp))

        if vars:
            self.vars.update(vars)
        self.source = exp

    def resolve(self, ctx, wantList=True, strict=_defaultStrictness):
        """
        If wantList=True (default) returns a ResultList of matches
        Note that values in the list can be a list or None
        If wantList=False return `resolve_one` semantics
        If wantList='result' return a Result
        """
        ctx = ctx.copy(
            vars=self.vars, wantList=wantList, trace=self.trace, strict=strict
        )
        base_dir = getattr(self.source, "base_dir", None)
        if base_dir:
            ctx.base_dir = base_dir

        ctx.trace(
            f"Ref.resolve(wantList={wantList}) start strict {ctx.strict}",
            self.source,
        )
        results = eval_ref(self.source, ctx, True)
        ctx.trace(f"Ref.resolve(wantList={wantList}) evalRef", self.source, results)
        if results and self.foreach:
            results = for_each(self.foreach, results, ctx)
        assert not isinstance(results, ResultsList), results
        results = ResultsList(results, ctx)
        ctx.add_reference(self, results)
        ctx.trace(f"Ref.resolve(wantList={wantList}) results", self.source, results)
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

    def resolve_one(self, ctx, strict=_defaultStrictness):
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
    def is_ref(value):
        if isinstance(value, Mapping):
            if not value:
                return False
            first = next(iter(value))

            if "ref" in value or "eval" in value:
                return len(
                    [x for x in ["vars", "trace", "foreach", "select"] if x in value]
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


def eq_func(arg, ctx):
    args = map_value(arg, ctx)
    assert_form(args, MutableSequence, len(args) == 2)
    return args[0] == args[1]


def validate_schema_func(arg, ctx):
    args = map_value(arg, ctx)
    assert_form(args, MutableSequence, len(args) == 2)
    return validate_schema(args[0], args[1])


def _for_each(foreach, results, ctx):
    if isinstance(foreach, six.string_types):
        keyExp = None
        valExp = foreach
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

    def make_items():
        for i, (k, v) in enumerate(results):
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
            valResults = eval_ref(valExp, ictx)
            if not valResults:
                continue
            if len(valResults) == 1:
                val = valResults[0].resolved
                if val is Break:
                    break
                elif val is Continue:
                    continue
                valResults = valResults[0]

            if keyExp:
                yield (key, valResults)
            else:
                yield valResults

    if keyExp:
        # use CommentedMap to preserve order
        return [CommentedMap(make_items())]
    else:
        return list(make_items())


def for_each(foreach, results, ctx):
    # results will be list of Result
    return _for_each(foreach, enumerate(r.external or r.resolved for r in results), ctx)


def for_each_func(foreach, ctx):
    results = ctx.currentResource
    if results:
        if isinstance(results, Mapping):
            return _for_each(foreach, results.items(), ctx)
        elif isinstance(results, MutableSequence):
            return _for_each(foreach, enumerate(results), ctx)
        else:
            return _for_each(foreach, [(0, results)], ctx)
    else:
        return results


_Funcs = {
    "if": if_func,
    "and": and_func,
    "or": or_func,
    "not": not_func,
    "q": quote_func,
    "eq": eq_func,
    "validate": validate_schema_func,
    "foreach": for_each_func,
}
_FuncsTop = ["q"]


def get_eval_func(name):
    return _Funcs.get(name)


def set_eval_func(name, val, topLevel=False):
    _Funcs[name] = val
    if topLevel:
        _FuncsTop.append(name)


def eval_ref(val, ctx, top=False):
    "val is assumed to be an expression, evaluate and return a list of Result"
    from .support import is_template, apply_template

    # functions and ResultsMap assume resolve_one semantics
    if top:
        vars = ctx.vars.copy()
        vars["start"] = ctx.currentResource
        ctx = ctx.copy(ctx.currentResource, vars, wantList=False)

    if isinstance(val, Mapping):
        for key in val:
            func = _Funcs.get(key)
            if func:
                args = val[key]
                ctx.kw = val
                ctx.currentFunc = key
                if "var" in val:
                    unexpected = "var"
                elif key != "foreach" and "foreach" in val:
                    unexpected = "foreach"
                else:
                    unexpected = False
                if unexpected:
                    raise UnfurlError(
                        f"unexpected '{unexpected}' found, did you intend it for the parent?"
                    )
                val = func(args, ctx)
                if key == "q":
                    if isinstance(val, Result):
                        return [val]
                    else:
                        return [Result(val)]
                break
    elif isinstance(val, six.string_types):
        if is_template(val, ctx):
            return [Result(apply_template(val, ctx))]
        else:
            expr = Expr(val, ctx.vars)
            results = expr.resolve(ctx)  # returns a list of Result
            ctx.trace("expr.resolve", results)
            return results

    mappedVal = Results._map_value(val, ctx)
    if isinstance(mappedVal, Result):
        return [mappedVal]
    else:
        return [Result(mappedVal)]


def eval_for_func(val, ctx):
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


def eval_test(value, test, context):
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


# given a Result and a key, return None or new Result
def lookup(result, key, context):
    try:
        # if key == '.':
        #   key = context.currentKey
        if context and isinstance(key, six.string_types) and key.startswith("$"):
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
            context.trace("lookup return None due to exception:")
            import traceback

            traceback.print_exc()
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


def eval_exp(start, paths, context):
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
