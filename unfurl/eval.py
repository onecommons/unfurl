# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Public Api:

mapValue - returns a copy of the given value resolving any embedded queries or template strings

Ref.resolve given an expression, returns a ResultList
Ref.resolveOne given an expression, return value, none or a (regular) list
Ref.isRef return true if the given diction looks like a Ref

Internal:

evalRef() given expression (string or dictionary) return list of Result
Expr.resolve() given expression string, return list of Result
Results._mapValue same as mapValue but with lazily evaluation
"""
import six
import re
import operator
import collections
from collections import Mapping, MutableSequence
from ruamel.yaml.comments import CommentedMap
from .util import validateSchema, UnfurlError, assertForm
from .result import ResultsList, Result, Results, ExternalValue, ResourceRef


def mapValue(value, resourceOrCxt, applyTemplates=True):
    if not isinstance(resourceOrCxt, RefContext):
        resourceOrCxt = RefContext(resourceOrCxt)
    return _mapValue(value, resourceOrCxt, False, applyTemplates)


def _mapValue(value, ctx, wantList=False, applyTemplates=True):
    from .support import isTemplate, applyTemplate

    if Ref.isRef(value):
        # wantList=False := resolveOne
        return Ref(value).resolve(ctx, wantList=wantList)

    if isinstance(value, Mapping):
        try:
            oldBaseDir = ctx.baseDir
            ctx.baseDir = getattr(value, "baseDir", oldBaseDir)
            if ctx.baseDir and ctx.baseDir != oldBaseDir:
                ctx.trace("found baseDir", ctx.baseDir)
            return dict(
                (key, _mapValue(v, ctx, wantList, applyTemplates))
                for key, v in value.items()
            )
        finally:
            ctx.baseDir = oldBaseDir
    elif isinstance(value, (MutableSequence, tuple)):
        return [_mapValue(item, ctx, wantList, applyTemplates) for item in value]
    elif applyTemplates and isTemplate(value, ctx):
        return applyTemplate(value, ctx)
    return value


class _Tracker(object):
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


class RefContext(object):
    def __init__(
        self,
        currentResource,
        vars=None,
        wantList=False,
        resolveExternal=False,
        trace=0,
        strict=_defaultStrictness,
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
        self._trace = trace
        self.strict = strict
        self.baseDir = currentResource.baseDir
        self.templar = currentResource.templar
        self.referenced = _Tracker()

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
        copy.baseDir = self.baseDir
        copy.templar = self.templar
        copy.referenced = self.referenced
        return copy

    def trace(self, *msg):
        if self._trace:
            print("%s (ctx: %s)" % (" ".join(str(a) for a in msg), self._lastResource))

    def addReference(self, ref, result):
        self.referenced.addReference(ref, result)

    def resolveVar(self, key):
        return self._resolveVar(key[1:]).resolved

    def _resolveVar(self, key):
        value = self.vars[key]
        if isinstance(value, Result):
            return value
        else:
            # lazily resolve maps and lists to avoid circular references
            val = Results._mapValue(value, self)
            if not isinstance(val, Result):
                val = Result(val)
            self.vars[key] = val
            return val

    def resolveReference(self, key):
        val = self._resolveVar(key)
        self.addReference(key, val)
        assert not isinstance(val.resolved, Result)
        return val.resolved

    def query(self, expr, vars=None, wantList=False):
        return Ref(expr, vars).resolve(self, wantList)

    def __getstate__(self):
        # Remove the unpicklable entries.
        state = self.__dict__.copy()
        state["templar"] = None
        del state["referenced"]
        return state

    def __setstate__(self, d):
        self.__dict__ = d
        self.referenced = _Tracker()


class Expr(object):
    def __init__(self, exp, vars=None):
        self.vars = {"true": True, "false": False, "null": None}

        if vars:
            self.vars.update(vars)

        self.source = exp
        paths = list(parseExp(exp))
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
        return "Expr('%s')" % self.source

    def resolve(self, context):
        # returns a list of Result
        currentResource = context.currentResource
        if not self.paths[0].key and not self.paths[0].filters:  # starts with "::"
            currentResource = currentResource.all
            paths = self.paths[1:]
        elif self.paths[0].key and self.paths[0].key[0] == "$":
            # if starts with a var, use that as the start
            currentResource = context.resolveVar(self.paths[0].key)
            if len(self.paths) == 1:
                # bare reference to a var, just return it's value
                return [Result(currentResource)]
            paths = [self.paths[0]._replace(key="")] + self.paths[1:]
        else:
            paths = self.paths
        return evalExp([currentResource], paths, context)


class Ref(object):
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
        If wantList=False return `resolveOne` semantics
        If wantList='result' return a Result
        """
        ctx = ctx.copy(
            vars=self.vars, wantList=wantList, trace=self.trace, strict=strict
        )
        ctx.trace(
            "Ref.resolve(wantList=%s) start strict %s" % (wantList, ctx.strict),
            self.source,
        )
        results = evalRef(self.source, ctx, True)
        ctx.trace("Ref.resolve(wantList=%s) evalRef" % wantList, self.source, results)
        if results and self.foreach:
            results = forEach(self.foreach, results, ctx)
        assert not isinstance(results, ResultsList), results
        results = ResultsList(results, ctx)
        ctx.addReference(self, results)
        ctx.trace("Ref.resolve(wantList=%s) results" % wantList, self.source, results)
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

    def resolveOne(self, ctx, strict=_defaultStrictness):
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
    def isRef(value):
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


def evalAsBoolean(arg, ctx):
    result = evalRef(arg, ctx)
    return not not result[0].resolved if result else False


def ifFunc(arg, ctx):
    kw = ctx.kw
    result = evalAsBoolean(arg, ctx)
    if result:
        if "then" in kw:
            return evalForFunc(kw["then"], ctx)
        else:
            return result
    else:
        if "else" in kw:
            return evalForFunc(kw["else"], ctx)
        else:
            return result


def orFunc(arg, ctx):
    args = evalForFunc(arg, ctx)
    assertForm(args, MutableSequence)
    for arg in args:
        val = evalForFunc(arg, ctx)
        if val:
            return val
    return False


def notFunc(arg, ctx):
    result = evalAsBoolean(arg, ctx)
    return not result


def andFunc(arg, ctx):
    args = evalForFunc(arg, ctx)
    assertForm(args, MutableSequence)
    for arg in args:
        val = evalForFunc(arg, ctx)
        if not val:
            return val
    return val


def quoteFunc(arg, ctx):
    return arg


def eqFunc(arg, ctx):
    args = mapValue(arg, ctx)
    assertForm(args, MutableSequence, len(args) == 2)
    return args[0] == args[1]


def validateSchemaFunc(arg, ctx):
    args = mapValue(arg, ctx)
    assertForm(args, MutableSequence, len(args) == 2)
    return validateSchema(args[0], args[1])


def _forEach(foreach, results, ctx):
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

    def makeItems():
        for i, (k, v) in enumerate(results):
            ictx.currentResource = v
            ictx.vars["collection"] = results
            ictx.vars["index"] = i
            ictx.vars["key"] = k
            ictx.vars["item"] = v
            ictx.vars["break"] = Break
            ictx.vars["continue"] = Continue
            if keyExp:
                key = evalForFunc(keyExp, ictx)
                if key is Break:
                    break
                elif key is Continue:
                    continue
            valResults = evalRef(valExp, ictx)
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
        return [CommentedMap(makeItems())]
    else:
        return list(makeItems())


def forEach(foreach, results, ctx):
    # results will be list of Result
    return _forEach(foreach, enumerate(r.external or r.resolved for r in results), ctx)


def forEachFunc(foreach, ctx):
    results = ctx.currentResource
    if results:
        if isinstance(results, Mapping):
            return _forEach(foreach, results.items(), ctx)
        elif isinstance(results, MutableSequence):
            return _forEach(foreach, enumerate(results), ctx)
        else:
            return _forEach(foreach, [(0, results)], ctx)
    else:
        return results


_Funcs = {
    "if": ifFunc,
    "and": andFunc,
    "or": orFunc,
    "not": notFunc,
    "q": quoteFunc,
    "eq": eqFunc,
    "validate": validateSchemaFunc,
    "foreach": forEachFunc,
}
_FuncsTop = ["q"]


def getEvalFunc(name):
    return _Funcs.get(name)


def setEvalFunc(name, val, topLevel=False):
    _Funcs[name] = val
    if topLevel:
        _FuncsTop.append(name)


def evalRef(val, ctx, top=False):
    "val is assumed to be an expression, evaluate and return a list of Result"
    from .support import isTemplate, applyTemplate

    # functions and ResultsMap assume resolveOne semantics
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
                        "unexpected '%s' found, did you intend it for the parent?"
                        % unexpected
                    )
                val = func(args, ctx)
                if key == "q":
                    if isinstance(val, Result):
                        return [val]
                    else:
                        return [Result(val)]
                break
    elif isinstance(val, six.string_types):
        if isTemplate(val, ctx):
            return [Result(applyTemplate(val, ctx))]
        else:
            expr = Expr(val, ctx.vars)
            results = expr.resolve(ctx)  # returns a list of Result
            ctx.trace("expr.resolve", results)
            return results

    mappedVal = Results._mapValue(val, ctx)
    if isinstance(mappedVal, Result):
        return [mappedVal]
    else:
        return [Result(mappedVal)]


def evalForFunc(val, ctx):
    "like `evalRef` except it returns the resolved value"
    results = evalRef(val, ctx)
    if not results:
        return None
    if len(results) == 1:
        return results[0].resolved
    else:
        return [r.resolved for r in results]


# return a segment
Segment = collections.namedtuple("Segment", ["key", "test", "modifier", "filters"])
defaultSegment = Segment("", [], "", [])


def evalTest(value, test, context):
    comparor = test[0]
    key = test[1]
    try:
        if context and isinstance(key, six.string_types) and key.startswith("$"):
            compare = context.resolveVar(key)
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
            key = context.resolveVar(key)

        if isinstance(result.resolved, ResourceRef):
            context._lastResource = result.resolved

        ctx = context.copy(context._lastResource)
        result = result.project(key, ctx)
        value = result.resolved
        context.trace("lookup %s, got %s" % (key, value))

        if not context._rest:
            assert not Ref.isRef(value)
            result.resolved = Results._mapValue(value, ctx)
            assert not isinstance(result.resolved, (ExternalValue, Result))

        return result
    except (KeyError, IndexError, TypeError, ValueError):
        if context._trace:
            context.trace("lookup return None due to exception:")
            import traceback

            traceback.print_exc()
        return None


# given a Result, yields the result
def evalItem(result, seg, context):
    """
    apply current item to current segment, return [] or [value]
    """
    if seg.key != "":
        result = lookup(result, seg.key, context)
        if not result:
            return

    value = result.resolved
    for filter in seg.filters:
        if _treatAsSingular(result, filter[0]):
            resultList = [value]
        else:
            resultList = value
        results = evalExp(resultList, filter, context)
        negate = filter[0].modifier == "!"
        if negate and results:
            return
        elif not negate and not results:
            return

    if seg.test and not evalTest(value, seg.test, context):
        return
    assert isinstance(result, Result), result
    yield result


def _treatAsSingular(result, seg):
    if seg.key == "*":
        return False
    # treat external values as single item even if they resolve to a list
    # treat lists as a single item if indexing into it
    return (
        result.external
        or not isinstance(result.resolved, MutableSequence)
        or isinstance(seg.key, six.integer_types)
    )


def recursiveEval(v, exp, context):
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

        if _treatAsSingular(result, exp[0]):
            rest = exp[1:]
            context._rest = rest
            context.trace("evaluating item %s with key %s" % (item, exp[0].key))
            iv = evalItem(
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
            results = recursiveEval(iv, rest, context)
            found = False
            for r in results:
                found = True
                context.trace("recursive result", r)
                assert isinstance(r, Result), r
                yield r
            context.trace("found recursive %s matchFirst: %s" % (found, matchFirst))
            if found and matchFirst:
                return
        else:
            for r in iv:
                assert isinstance(r, Result), r
                yield r
                if matchFirst:
                    return


def evalExp(start, paths, context):
    "Returns a list of Result"
    context.trace("evalexp", start, paths)
    assertForm(start, MutableSequence)
    return list(recursiveEval((Result(i) for i in start), paths, context))


def _makeKey(key):
    try:
        return int(key)
    except ValueError:
        return key


def parsePathKey(segment):
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
        return Segment(_makeKey(key), [op, parts[2]], modifier, [])
    else:
        return Segment(_makeKey(segment), [], modifier, [])


def parsePath(path, start):
    paths = path.split("::")
    segments = [parsePathKey(k.strip()) for k in paths]
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


def parseExp(exp):
    # return list of steps
    rest = exp
    last = None

    while rest:
        steps, rest = parseStep(rest, last)
        last = None
        if steps:
            # we might need merge the next step into the last
            last = steps.pop()
            for step in steps:
                yield step

    if last:
        yield last


def parseStep(exp, start=None):
    split = re.split(r"(\[|\])", exp, 1)
    if len(split) == 1:  # not found
        return parsePath(split[0], start), ""
    else:
        path, sep, rest = split

    paths = parsePath(path, start)

    filterExps = []
    while sep == "[":
        filterExp, rest = parseStep(rest)
        filterExps.append(filterExp)
        # rest will be anything after ]
        sep = rest and rest[0]

    # add filterExps to last Segment
    paths[-1] = paths[-1]._replace(filters=filterExps)
    return paths, rest
