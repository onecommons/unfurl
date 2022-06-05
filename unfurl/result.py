# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from collections.abc import Mapping, MutableSequence, MutableMapping
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Match, Optional
import six
import hashlib
import re
from toscaparser.common.exception import ValidationError

from .merge import diff_dicts
from .util import (
    UnfurlError,
    UnfurlTaskError,
    is_sensitive,
    sensitive_dict,
    sensitive_list,
    dump,
)
from .logs import sensitive


def _get_digest(value, kw):
    getter = getattr(value, "__digestable__", None)
    if getter:
        value = getter(kw)
    isSensitive = isinstance(value, sensitive)
    if isSensitive:
        yield sensitive.redacted_str
    else:
        if isinstance(value, Results):
            # since we don't have a way to record which keys were resolved or not
            # resolve them all now, otherwise we can't compare reliable compare digests
            value.resolve_all()
            value = value._attributes
        if isinstance(value, Mapping):
            for k in sorted(value.keys()):
                yield k
                for d in _get_digest(value[k], kw):
                    yield d
        elif isinstance(value, (MutableSequence, tuple)):
            for v in value:
                for d in _get_digest(v, kw):
                    yield d
        else:
            out = six.BytesIO()
            dump(serialize_value(value, redact=True), out)
            yield out.getvalue()


def get_digest(tpl, **kw):
    m = hashlib.sha1()  # use same digest function as git
    for contents in _get_digest(tpl, kw):
        if not isinstance(contents, bytes):
            contents = str(contents).encode("utf-8")
        m.update(contents)
    return m.hexdigest()


def serialize_value(value, **kw):
    getter = getattr(value, "as_ref", None)
    if getter:
        return getter(kw)
    isSensitive = isinstance(value, sensitive)
    if isSensitive and kw.get("redact"):
        return sensitive.redacted_str
    if isinstance(value, Mapping):
        ctor = sensitive_dict if isSensitive else dict
        return ctor((key, serialize_value(v, **kw)) for key, v in value.items())
    if isinstance(value, (MutableSequence, tuple)):
        ctor = sensitive_list if isSensitive else list
        return ctor(serialize_value(item, **kw) for item in value)
    else:
        return value


class ResourceRef(ABC):

    parent = None  # parent must be defined by subclass

    @abstractmethod
    def _resolve(self, key):
        ...

    _templar = None

    def _get_prop(self, name):
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
            return self._get_prop(key)

        return self._resolve(key)

    def yield_parents(self):
        "yield self and ancestors starting from self"
        resource = self
        while resource:
            yield resource
            resource = resource.parent

    @property
    def ancestors(self):
        return list(self.yield_parents())

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


class ChangeRecord:
    """
    A ChangeRecord represents a job or task in the change log file.
    It consists of a change ID and named attributes.

    A change ID is an identifier with this sequence of 12 characters:
    - "A" serves as a format version identifier
    - 7 alphanumeric characters (0-9, A-Z, and a-z) encoding the date and time the job ran.
    - 4 hexadecimal digits encoding the task id
    """

    EpochStartTime = datetime(2020, 1, 1, tzinfo=None)
    LogAttributes = ("previousId",)
    DateTimeFormat = "%Y-%m-%d-%H-%M-%S-%f"

    def __init__(
        self,
        jobId: Optional[str]=None,
        startTime: Optional[datetime]=None,
        taskId: int=0,
        previousId: Optional[str]=None,
        parse: Optional[str]=None
    ):
        if parse:
            self.parse(parse)
            self.set_start_time(getattr(self, "startTime", startTime))
        else:
            self.set_start_time(startTime)
            self.taskId = taskId
            self.previousId = previousId
            if jobId:
                self.changeId = self.update_change_id(jobId, taskId)
            else:
                self.changeId = self.make_change_id(self.startTime, taskId, previousId)

    def set_start_time(self, startTime: Optional[datetime]=None) -> None:
        if not startTime:
            self.startTime = datetime.utcnow()
        elif isinstance(startTime, datetime):
            self.startTime = startTime
        else:
            try:
                startTime = int(startTime)  # helper for deterministic testing
                self.startTime = self.EpochStartTime.replace(hour=startTime)
            except ValueError:
                try:
                    self.startTime = datetime.strptime(startTime, self.DateTimeFormat)
                except ValueError:
                    self.startTime = self.EpochStartTime

    def get_start_time(self) -> str:
        return self.startTime.strftime(self.DateTimeFormat)

    def set_task_id(self, taskId: int) -> None:
        self.taskId = taskId
        self.changeId = self.update_change_id(self.changeId, taskId)

    @staticmethod
    def get_job_id(changeId: str) -> str:
        return ChangeRecord.update_change_id(changeId, 0)

    @staticmethod
    def update_change_id(changeId: str, taskId: int) -> str:
        return changeId[:-4] + "{:04x}".format(taskId)

    @staticmethod
    def decode(changeId: str) -> str:
        def _decode_chr(i: int, c: str) -> str:
            offset = 48 if c < "A" else 55
            val = ord(c) - offset
            return str(val + (2020 if i == 0 else 0))

        return (
            "-".join([_decode_chr(*e) for e in enumerate(changeId[1:7])])
            + "."
            + _decode_chr(7, changeId[7])
        )

    @staticmethod
    def is_change_id(test: str) -> Optional[Match]:
        if not isinstance(test, six.string_types):
            return None
        return re.match("^A[A-Za-z0-9]{11}$", test)

    @classmethod
    def make_change_id(cls, timestamp: Optional[datetime]=None, taskid: int=0, previousId: Optional[str]=None) -> str:
        b62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        if not timestamp:
            timestamp = datetime.utcnow()

        year = timestamp.year - cls.EpochStartTime.year  # 2020
        if year < 0:
            raise UnfurlError(f"changeId timestamp too far in the past: {timestamp}")
        if year > len(b62):
            raise UnfurlError(f"changeId timestamp too far in the future: {timestamp}")

        # year, month, day, hour, minute, second, wday, yday, dst
        jobIdFragment = "".join([b62[n] for n in timestamp.utctimetuple()[1:6]])
        fraction = b62[timestamp.microsecond // 16200]
        changeId = "A{}{}{}{:04x}".format(b62[year], jobIdFragment, fraction, taskid)

        if previousId:
            if previousId[:8] == changeId[:8]:
                # in case last job started less than 1/62nd of a second ago
                return cls.make_change_id(
                    timestamp + timedelta(milliseconds=16200), taskid, previousId
                )
            if previousId > changeId:
                raise UnfurlError(
                    f"New changeId is earlier than the previous changeId: {changeId} ({cls.decode(changeId)}) < {previousId} ({cls.decode(previousId)}) Is time set correctly?"
                )
        return changeId

    def parse(self, log: str) -> None:
        terms = log.split("\t")
        if not terms:
            raise UnfurlError(f'can not parse ChangeRecord from "{log}"')
        attributes = dict(startTime=None)
        for i, term in enumerate(terms):
            if i == 0:
                self.changeId = term
                self.taskId = int(term[-4:], 16)
            # elif i == 1 and '=' not in term:
            #     self.parentId=ChangeId(term)
            else:
                left, sep, right = term.partition("=")
                attributes[left] = right  # type: ignore
        self.__dict__.update(attributes)

    @classmethod
    def format_log(cls, changeId: str, attributes: dict) -> str:
        r"format: changeid\tkey=value\tkey=value"
        terms = [changeId] + ["{}={}".format(k, v) for k, v in attributes.items()]
        return "\t".join(terms) + "\n"

    def log(self, attributes: Optional[dict]=None) -> str:
        r"changeid\tkey=value\tkey=value"
        default = {
            k: getattr(self, k)
            for k in self.LogAttributes
            if getattr(self, k, None) is not None
        }
        if attributes:
            default.update(attributes)
        return self.format_log(self.changeId, default)


class ChangeAware:
    def has_changed(self, changeRecord: ChangeRecord) -> bool:
        """
        Whether or not this object changed since the give ChangeRecord.
        """
        return False


class ExternalValue(ChangeAware):
    __slots__ = ("type", "key")

    def __init__(self, type, key):
        self.type = type
        self.key = key

    def get(self):
        return self.key

    def __digestable__(self, options):
        return self.get()

    def __eq__(self, other):
        if isinstance(other, ExternalValue):
            return self.get() == other.get()
        return self.get() == other

    def resolve_key(self, key=None, currentResource=None):
        if key:
            value = self.get()
            getter = getattr(value, "__reflookup__", None)
            if getter:
                return getter(key)
            else:
                return value[key]
        else:
            return self.get()

    def as_ref(self, options=None):
        if options and options.get("resolveExternal"):
            return serialize_value(self.get(), **options)
        serialized = {self.type: self.key}
        return {"eval": serialized}


_Deleted = object()
_Get = object()


class Result(ChangeAware):
    # ``original`` is managed by the Results that owns this Result
    # in __getitem__ and __setitem__
    __slots__ = ("original", "resolved", "external", "select")

    def __init__(self, resolved):
        self.select = ()
        self.original = _Deleted  # assume this is new to start
        if isinstance(resolved, ExternalValue):
            self.resolved = resolved.get()
            assert not isinstance(self.resolved, Result), self.resolved
            self.external = resolved
        else:
            assert not isinstance(resolved, Result), resolved
            self.resolved = resolved
            self.external = None

    def as_ref(self, options=None):
        options = options or {}
        if self.external:
            ref = self.external.as_ref(options)
            if self.select and not options.get("resolveExternal"):
                ref["select"] = "." + "::".join(self.select)
            return ref
        else:
            val = serialize_value(self.resolved, **options)
            return val

    def __digestable__(self, options):
        if self.external:
            return self.external.__digestable__(options)
        return self.resolved

    def has_diff(self):
        if self.original is not _Get:
            # this Result is a new or modified
            if isinstance(self.resolved, Results):
                return self.resolved.has_diff()
            else:
                return self.original != self.resolved
        return False

    def get_diff(self):
        if isinstance(self.resolved, Results):
            return self.resolved.getDiff()
        else:
            new = self.as_ref()
            if isinstance(self.resolved, Mapping) and isinstance(
                self.original, Mapping
            ):
                old = serialize_value(self.original)
                return diff_dicts(old, new)
            return new

    def __sensitive__(self):
        if self.external:
            return is_sensitive(self.external)
        else:
            return is_sensitive(self.resolved)

    def _values(self):
        resolved = self.resolved
        if isinstance(resolved, ResultsList):
            # iterate on list to make sure __getitem__ was called
            return (resolved._attributes[i] for (i, v) in enumerate(resolved))
        elif isinstance(resolved, ResultsMap):
            # use items() to make sure __getitem__ was called
            return (resolved._attributes[k] for (k, v) in resolved.items())
        elif isinstance(resolved, Mapping):
            return (Result(i) for i in resolved.values())
        elif isinstance(resolved, MutableSequence):
            return (Result(i) for i in resolved)
        else:
            return resolved

    def _resolve_key(self, key, currentResource):
        # might return a Result
        if self.external:
            value = self.external.resolve_key(key, currentResource)
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

        value = self._resolve_key(key, ctx._lastResource)
        if isinstance(value, Result):
            result = value
        elif Ref.is_ref(value):
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

    def has_changed(self, changeset):
        if self.external:
            return self.external.has_changed(changeset)
        elif isinstance(self.resolved, ChangeAware):
            return self.resolved.has_changed(changeset)
        else:
            return False

    def __eq__(self, other):
        if isinstance(other, Result):
            return self.resolved == other.resolved
        else:
            return self.resolved == other

    def __repr__(self):
        return "Result(%r, %r, %r)" % (self.resolved, self.external, self.select)


class Results(ABC):
    """
    Evaluating expressions are not guaranteed to be idempotent (consider quoting)
    and resolving the whole tree up front can lead to evaluations of circular references unless the
    order is carefully chosen. So evaluate lazily and memoize the results.
    This also allows us to track changes to the returned structure.
    """

    __slots__ = ("_attributes", "context", "_deleted")

    doFullResolve = False
    applyTemplates = True

    @abstractmethod
    def _values(self):
        ...

    @abstractmethod
    def resolve_all(self):
        ...

    def __init__(self, serializedOriginal, resourceOrCxt, validate=False):
        from .eval import RefContext

        assert not isinstance(serializedOriginal, Results), serializedOriginal
        self._attributes = serializedOriginal
        self._deleted = {}
        if not isinstance(resourceOrCxt, RefContext):
            ctx = RefContext(resourceOrCxt)
        else:
            ctx = resourceOrCxt

        oldBaseDir = ctx.base_dir
        newBaseDir = getattr(serializedOriginal, "base_dir", oldBaseDir)
        if newBaseDir and newBaseDir != oldBaseDir:
            ctx = ctx.copy()
            ctx.base_dir = newBaseDir
            ctx.trace("found baseDir", newBaseDir, "old", oldBaseDir)
        self.context = ctx
        self.validate = validate

    def get_copy(self, key, default=None):
        # return a copy of value or default if not found
        from .eval import map_value

        try:
            return map_value(self._getitem(key), self.context)
        except (KeyError, IndexError):
            return default

    @staticmethod
    def _map_value(val, context, applyTemplates=True):
        "Recursively and lazily resolves any references in a value"
        from .eval import map_value, Ref

        if isinstance(val, Results):
            return val
        elif Ref.is_ref(val):
            return Ref(val).resolve(context, wantList="result")
        elif isinstance(val, sensitive):
            return val
        elif isinstance(val, Mapping):
            return ResultsMap(val, context)
        elif isinstance(val, list):
            return ResultsList(val, context)
        else:
            # at this point, just evaluates templates in strings or returns val
            return map_value(val, context.copy(wantList="result"), applyTemplates)

    def __sensitive__(self):
        # only check resolved values
        return any(isinstance(x, Result) and is_sensitive(x) for x in self._values())

    def has_diff(self):
        # only check resolved values
        return any(isinstance(x, Result) and x.has_diff() for x in self._values())

    def __getitem__(self, key):
        return self._getitem(key)

    def _getitem(self, key):
        return self._getresult(key).resolved

    def _getresult(self, key):
        from .eval import map_value

        val = self._attributes[key]
        if isinstance(val, Result):
            # already resolved
            assert not isinstance(val.resolved, Result), val
            return val
        else:
            if self.doFullResolve:
                if isinstance(val, Results):
                    resolved = val
                else:  # evaluate records that aren't Results
                    resolved = map_value(val, self.context, self.applyTemplates)
            else:
                # lazily evaluate lists and dicts
                self.context.trace("Results._mapValue", val)
                resolved = self._map_value(val, self.context, self.applyTemplates)
            # will return a Result if val was an expression that was evaluated
            if isinstance(resolved, Result):
                result = resolved
                resolved = result.resolved
            else:
                result = Result(resolved)
            result.original = _Get
            if self.validate:
                self._validate(key, result.resolved)
            self._attributes[key] = result
            assert not isinstance(resolved, Result), val
            return result

    def _validate(self, key, value):
        resource = self.context.currentResource
        defs = resource.template and resource.template.propertyDefs or {}
        propDef = defs.get(key)
        if not propDef:
            return
        try:
            if value is None:
                # required attributes might be null depending on the state of the resource
                if propDef.required and key not in resource.template.attributeDefs:
                  msg = f'Property "{key}" on "{resource.template.name}" cannot be null.'
                  raise ValidationError(message=msg)
            else:
                propDef._validate(value)
        except Exception as err:
            msg = f'Error while evaluating "{key}" on "{resource.name}": {err}'
            if self.context.task:
                UnfurlTaskError(self.context.task, msg)
            elif self.context.strict:
                raise UnfurlError(msg, True)
            else:
                self.context.trace(msg)

    def _haskey(self, key):
        # can't use "in" operator for lists
        try:
            self._attributes[key]
            return True
        except:  # IndexError or KeyError
            return False

    def __setitem__(self, key, value):
        assert not isinstance(value, Result), (key, value)
        if self._haskey(key):
            resolved = self[key]
            if resolved != value:
                if self.validate:
                    self._validate(key, value)
                # exisiting value changed
                result = self._attributes[key]
                if result.original is _Get:
                    # we haven't saved the original value yet
                    result.original = result.resolved
                result.resolved = value
        else:
            if self.validate:
                self._validate(key, value)
            self._attributes[key] = Result(value)

        # remove from deleted if it's there
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
            self.resolve_all()
            return self._attributes == other

    def __str__(self):
        return str(self._attributes)

    def __repr__(self):
        return "Results(%r)" % self._attributes


class ResultsMap(Results, MutableMapping):
    def __iter__(self):
        return iter(self._attributes)

    def resolve_all(self):
        list(self.values())

    def get_resolved(self):
        return {key: v for key, v in self._attributes.items() if isinstance(v, Result)}

    def __contains__(self, key):
        return key in self._attributes

    def _values(self):
        return self._attributes.values()

    def get_diff(self, cls=dict):
        # returns a dict with the same semantics as diffDicts
        diffDict = cls()
        for key, val in self._attributes.items():
            if isinstance(val, Result) and val.has_diff():
                diffDict[key] = val.get_diff()

        for key in self._deleted:
            diffDict[key] = {"+%": "delete"}

        return diffDict


class ResultsList(Results, MutableSequence):
    def insert(self, index, value):
        assert not isinstance(value, Result), value
        self._attributes.insert(index, Result(value))

    def _values(self):
        return self._attributes

    def get_diff(self, cls=list):
        # we don't have patchList yet so just returns the whole list
        return cls(
            val.get_diff() if isinstance(val, Result) else val
            for val in self._attributes
        )

    def resolve_all(self):
        list(self)
