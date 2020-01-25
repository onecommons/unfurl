from collections import Mapping, MutableSequence, MutableMapping
from .merge import diffDicts


def serializeValue(value, **kw):
    getter = getattr(value, "asRef", None)
    if getter:
        return getter(kw)
    if isinstance(value, Mapping):
        return dict((key, serializeValue(v, **kw)) for key, v in value.items())
    elif isinstance(value, (MutableSequence, tuple)):
        return [serializeValue(item, **kw) for item in value]
    else:
        return value


class ResourceRef(object):
    # ABC requires 'parent', and '_resolve'

    def _getProp(self, name):
        if name == ".":
            return self
        elif name == "..":
            return self.parent
        name = name[1:]
        # XXX3 use propmap
        return getattr(self, name)

    def __reflookup__(self, key):
        if not key:
            raise KeyError(key)
        if key[0] == ".":
            return self._getProp(key)

        return self._resolve(key)

    def yieldParents(self):
        "yield self and ancestors starting from self"
        resource = self
        while resource:
            yield resource
            resource = resource.parent

    @property
    def ancestors(self):
        return list(self.yieldParents())

    @property
    def parents(self):
        """list of parents starting from root"""
        return list(reversed(self.ancestors))[:-1]

    @property
    def root(self):
        return self.ancestors[-1]

    @property
    def all(self):
        return self.root._all

    @property
    def templar(self):
        return self.root._templar


class ChangeRecord(object):
    def __init__(self, changeId=0, parentId=None):
        self.changeId = changeId
        self.parentId = parentId


class ChangeAware(object):
    def hasChanged(self, changeRecord):
        return False


class ExternalValue(ChangeAware):
    __slots__ = ("type", "key")

    def __init__(self, type, key):
        self.type = type
        self.key = key

    def get(self):
        return self.key

    # XXX def __setstate__

    def __eq__(self, other):
        if isinstance(other, ExternalValue):
            return self.get() == other.get()
        return self.get() == other

    def resolveKey(self, key=None, currentResource=None):
        if key:
            value = self.get()
            getter = getattr(value, "__reflookup__", None)
            if getter:
                return getter(key)
            else:
                return value[key]
        else:
            return self.get()

    def asRef(self, options=None):
        if options and options.get("resolveExternal"):
            return serializeValue(self.get(), **options)
        serialized = {self.type: self.key}
        return {"eval": serialized}


_Deleted = object()


class Result(ChangeAware):
    __slots__ = ("original", "resolved", "external", "select")

    def __init__(self, resolved, original=_Deleted):
        self.select = ()
        self.original = original
        if isinstance(resolved, ExternalValue):
            self.resolved = resolved.get()
            assert not isinstance(self.resolved, Result), self.resolved
            self.external = resolved
        else:
            assert not isinstance(resolved, Result), resolved
            self.resolved = resolved
            self.external = None

    def asRef(self, options=None):
        options = options or {}
        if self.external:
            ref = self.external.asRef(options)
            if self.select and not options.get("resolveExternal"):
                ref["foreach"] = "." + "::".join(self.select)
            return ref
        else:
            val = serializeValue(self.resolved, **options)
            return val

    def hasDiff(self):
        if self.original is _Deleted:  # this is a new item
            return True
        else:
            if isinstance(self.resolved, Results):
                return self.resolved.hasDiff()
            else:
                newval = self.asRef()
                if self.original != newval:
                    return True
        return False

    def getDiff(self):
        if isinstance(self.resolved, Results):
            return self.resolved.getDiff()
        else:
            val = self.asRef()
            if not self.external and isinstance(val, Mapping):
                old = serializeValue(self.original)
                if isinstance(old, Mapping):
                    return diffDicts(old, val)
            return val

    def _values(self):
        resolved = self.resolved
        if isinstance(resolved, ResultsList):
            # iterate on list to make sure __getitem__ was called
            return (resolved._attributes[i] for (i, v) in enumerate(resolved))
        elif isinstance(resolved, ResultsMap):
            # use items() to make sure __getitem__ was called
            return (resolved._attributes[k] for (k, v) in resolved.items())
        elif isinstance(resolved, Mapping):
            return resolved.values()
        elif isinstance(resolved, MutableSequence):
            return (Result(i) for i in resolved)
        else:
            return resolved

    def _resolveKey(self, key, currentResource):
        # might return a Result
        if self.external:
            value = self.external.resolveKey(key, currentResource)
        else:
            getter = getattr(self.resolved, "__reflookup__", None)
            if getter:
                value = getter(key)
            else:
                value = self.resolved[key]
        return value

    def project(self, key, ctx):
        # returns a Result
        from .eval import Ref

        value = self._resolveKey(key, ctx._lastResource)
        if isinstance(value, Result):
            result = value
        elif Ref.isRef(value):
            result = Ref(value).resolve(ctx, wantList="result")
            if not result:
                raise KeyError(key)
        else:
            result = Result(value)
        if self.external:
            # if value is an ExternalValue this will overwrite it
            result.external = self.external
            result.select = self.select + (key,)
        return result

    def hasChanged(self, changeset):
        if self.external:
            return self.external.hasChanged(changeset)
        elif isinstance(self.resolved, ChangeAware):
            return self.resolved.hasChanged(changeset)
        else:
            return False

    def __eq__(self, other):
        if isinstance(other, Result):
            return self.resolved == other.resolved
        else:
            return self.resolved == other

    def __repr__(self):
        return "Result(%r, %r, %r)" % (self.resolved, self.external, self.select)


class Results(object):
    """
  Evaluating expressions are not guaranteed to be idempotent (consider quoting)
  and resolving the whole tree up front can lead to evaluations of circular references unless the
  order is carefully chosen. So evaluate lazily and memoize the results.
  This also allows us to track changes to the returned structure.
  """

    __slots__ = ("_attributes", "context", "_deleted")

    doFullResolve = False

    def __init__(self, serializedOriginal, resourceOrCxt):
        from .eval import RefContext

        assert not isinstance(serializedOriginal, Results), serializedOriginal
        self._attributes = serializedOriginal
        self._deleted = {}
        if not isinstance(resourceOrCxt, RefContext):
            ctx = RefContext(resourceOrCxt)
        else:
            ctx = resourceOrCxt

        oldBaseDir = ctx.baseDir
        newBaseDir = getattr(serializedOriginal, "baseDir", oldBaseDir)
        if newBaseDir and newBaseDir != oldBaseDir:
            ctx = ctx.copy()
            ctx.baseDir = newBaseDir
            ctx.trace("found baseDir", newBaseDir, "old", oldBaseDir)
        self.context = ctx
        # setting __UNSAFE__ will prevent ansible templates from trying evaluate template strings
        # in its items. It does so eagerly defeating the lazy evaluation this class is intending to provide
        self.__UNSAFE__ = True

    def hasDiff(self):
        return any(isinstance(x, Result) and x.hasDiff() for x in self._attributes)

    def getCopy(self, key, default=None):
        from .eval import mapValue

        try:
            val = self._attributes[key]
        except:
            val = default
        else:
            if isinstance(val, Result):
                assert not isinstance(val.resolved, Result), val
                if val.original is _Deleted:
                    val = val.asRef()
                else:
                    val = val.original
        return mapValue(val, self.context)

    @staticmethod
    def _mapValue(val, context):
        "Recursively and lazily resolves any references in a value"
        from .eval import mapValue, Ref

        if isinstance(val, Results):
            return val
        elif Ref.isRef(val):
            return Ref(val).resolve(context, wantList="result")
        elif isinstance(val, Mapping):
            return ResultsMap(val, context)
        elif isinstance(val, list):
            return ResultsList(val, context)
        else:
            # at this point, just evaluates templates in strings or returns val
            return mapValue(val, context)

    def __getitem__(self, key):
        from .eval import mapValue

        val = self._attributes[key]
        if isinstance(val, Result):
            assert not isinstance(val.resolved, Result), val
            return val.resolved
        else:
            if self.doFullResolve:
                resolved = mapValue(val, self.context)
            else:
                self.context.trace("Results._mapValue", val)
                resolved = self._mapValue(val, self.context)
            # will return a Result if val was an expression that was evaluated
            if isinstance(resolved, Result):
                result = resolved
                result.original = val
                resolved = result.resolved
            else:
                result = Result(resolved, val)
            self._attributes[key] = result
            assert not isinstance(resolved, Result), val
            return resolved

    def __setitem__(self, key, value):
        assert not isinstance(value, Result), (key, value)
        self._attributes[key] = Result(value)
        self._deleted.pop(key, None)

    def __delitem__(self, index):
        val = self._attributes[index]
        self._deleted[index] = val
        del self._attributes[index]

    def __len__(self):
        return len(self._attributes)

    def __eq__(self, other):
        if isinstance(other, Results):
            return self._attributes == other._attributes
        else:
            self.resolveAll()
            return self._attributes == other

    def __repr__(self):
        return "Results(%r)" % self._attributes


class ResultsMap(Results, MutableMapping):
    def __iter__(self):
        return iter(self._attributes)

    def resolveAll(self):
        list(self.values())

    def getDiff(self, cls=dict):
        # returns a dict with the same semantics as diffDicts
        diffDict = cls()
        for key, val in self._attributes.items():
            if isinstance(val, Result) and val.hasDiff():
                diffDict[key] = val.getDiff()

        for key in self._deleted:
            diffDict[key] = {"+%": "delete"}

        return diffDict


class ResultsList(Results, MutableSequence):
    def insert(self, index, value):
        assert not isinstance(value, Result), value
        self._attributes.insert(index, Result(value))

    def getDiff(self, cls=list):
        # we don't have patchList yet so just returns the whole list
        return cls(
            val.getDiff() if isinstance(val, Result) else val
            for val in self._attributes
        )

    def resolveAll(self):
        list(self)
