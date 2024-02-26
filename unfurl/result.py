# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from collections.abc import Mapping, MutableSequence, MutableMapping
from abc import ABC, abstractmethod, ABCMeta
import datetime
import io
import logging
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterator,
    List,
    Match,
    Optional,
    Tuple,
    Union,
    cast,
)
import hashlib
import re
from tosca.yaml2python import has_function
from toscaparser.common.exception import ValidationError
from toscaparser.elements.portspectype import PortSpec
from toscaparser.properties import Property

if TYPE_CHECKING:
    from .spec import EntitySpec
    from .support import Templar

from .merge import diff_dicts
from .util import (
    UnfurlError,
    UnfurlTaskError,
    is_sensitive,
    sensitive_dict,
    sensitive_list,
    dump,
    wrap_sensitive_value,
)
from .logs import sensitive, getLogger

logger = getLogger("unfurl")


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
            out = io.BytesIO()
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
    parent = None  # must be defined by subclass
    template: Optional["EntitySpec"] = None
    name = ""

    @abstractmethod
    def _resolve(self, key): ...

    _templar: Optional["Templar"] = None

    @property
    def base_dir(self) -> str:
        return ""

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
        resource: Optional[ResourceRef] = self
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

    @property
    def readonly(self) -> bool:
        return False

    @property
    def environ(self):
        return self.root._environ


class AnyRef(ResourceRef):
    "Use this to analyze expressions"

    def __init__(self, name: str, parent=None):
        self.parent = parent
        self.key = name

    def _get_prop(self, name: str) -> Optional["AnyRef"]:
        if name == ".":
            return self
        elif name == "..":
            return self.parent
        return AnyRef(name, self)

    def _resolve(self, key):
        return AnyRef(key, self)

    def get_keys(self):
        return [p.key for p in reversed(self.ancestors)]


class ChangeRecord:
    """
    A ChangeRecord represents a job or task in the change log file.
    It consists of a change ID and named attributes.

    A change ID is an identifier with this sequence of 12 characters:
    - "A" serves as a format version identifier
    - 7 alphanumeric characters (0-9, A-Z, and a-z) encoding the date and time the job ran.
    - 4 hexadecimal digits encoding the task id
    """

    EpochStartTime = datetime.datetime(2020, 1, 1, tzinfo=None)
    LogAttributes = ("previousId",)
    DateTimeFormat = "%Y-%m-%d-%H-%M-%S-%f"

    def __init__(
        self,
        jobId: Optional[str] = None,
        startTime: Optional[datetime.datetime] = None,
        taskId: int = 0,
        previousId: Optional[str] = None,
        parse: Optional[str] = None,
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

    def set_start_time(self, startTime: Optional[datetime.datetime] = None) -> None:
        if not startTime:
            self.startTime = datetime.datetime.now(datetime.timezone.utc)
        elif isinstance(startTime, datetime.datetime):
            self.startTime = startTime
        else:
            try:
                startTime = int(startTime)  # helper for deterministic testing
                self.startTime = self.EpochStartTime.replace(hour=startTime)
            except ValueError:
                try:
                    self.startTime = datetime.datetime.strptime(
                        startTime, self.DateTimeFormat
                    )
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
    def is_change_id(test: Any) -> Optional[Match]:
        if not isinstance(test, str):
            return None
        return re.match("^A[A-Za-z0-9]{11}$", test)

    @classmethod
    def make_change_id(
        cls,
        timestamp: Optional[datetime.datetime] = None,
        taskid: int = 0,
        previousId: Optional[str] = None,
    ) -> str:
        b62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        if not timestamp:
            timestamp = datetime.datetime.now(datetime.timezone.utc)

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
                    timestamp + datetime.timedelta(milliseconds=16200),
                    taskid,
                    previousId,
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

    def log(self, attributes: Optional[dict] = None) -> str:
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

    def __init__(self, type: str, key: Any):
        self.type = type
        self.key = key

    def get(self) -> Any:
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


class Result(ChangeAware):
    # Result optionally maintains a shadow "external" value
    __slots__ = ("resolved", "external", "select")

    def __init__(self, resolved: Any):
        self.select: Tuple = ()
        if isinstance(resolved, ExternalValue):
            self.resolved = resolved.get()
            assert not isinstance(self.resolved, Result), self.resolved
            self.external: Optional[ExternalValue] = resolved
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

    def project(self, key: Any, ctx) -> "Result":
        from .eval import Ref

        value = self._resolve_key(key, ctx._lastResource)
        if isinstance(value, Result):
            result = value
        elif Ref.is_ref(value):
            _result = cast(Optional[Result], Ref(value).resolve(ctx, wantList="result"))
            if not _result:
                raise KeyError(key)
            result = _result
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


def is_sensitive_schema(defs, key):
    defSchema = (key in defs and defs[key].schema) or {}
    defMeta = defSchema.get("metadata", {})
    return defMeta.get("sensitive")


def _validation_error(src, context, prop_def, msg):
    from .eval import Ref
    from .configurator import Dependency

    if src and Ref.is_ref(src):
        dep = Dependency(src, target=context.currentResource, schema=prop_def)
    else:
        dep = None
    UnfurlTaskError(context.task, msg, dependency=dep)


def is_computed(val) -> bool:
    if isinstance(val, Results):
        val = val._attributes
    return has_function(val)


class _Sentinel:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "<Sentinel: " + self.name + ">"


_Missing = _Sentinel("_Missing")
_RecursionGuard = _Sentinel("_RecursionGuard")
MAX_CHANGE_COUNT = 0xFFFFFFFFFFFF


class ResultsItem(Result):
    "Internal representation of an item stored in Results"

    __slots__ = ("original", "last_computed")

    def __init__(
        self, resolved: Any, original: Any = _Missing, seen: int = MAX_CHANGE_COUNT
    ):
        if isinstance(resolved, Result):
            self.select: Tuple = ()
            self.external = resolved.external
            assert not isinstance(resolved.resolved, Result)
            resolved = self.resolved = resolved.resolved
            assert not isinstance(resolved, Result)
        else:
            super().__init__(resolved)
        self.last_computed = seen
        assert not isinstance(original, Result)
        self.original = original

    def set_resolved(self, result: Result, seen: int):
        # the value was computed again
        assert (
            self.is_computed()
        ), "computed items are mutually exclusive with update_value()"
        self.select = result.select
        self.external = result.external
        self.resolved = result.resolved
        assert not isinstance(result.resolved, Result)
        self.last_computed = seen

    def is_computed(self):
        return self.last_computed < MAX_CHANGE_COUNT

    def update_value(self, newvalue):
        # replace with a concrete value
        self.last_computed = MAX_CHANGE_COUNT
        self.resolved = newvalue

    def has_diff(self):
        "Was the item modified?"
        if isinstance(self.resolved, Results):
            # we need to checks these even if they are computed because
            # unlike simple computed value which can not be changed
            # these object have interior mutability
            return self.original is _Missing or self.resolved.has_diff()
        elif not self.is_computed():
            # value was set, see if original changed
            return self.original != self.resolved
        return False

    def get_diff(self):
        if isinstance(self.resolved, (ResultsList, ResultsMap)):
            return self.resolved.get_diff()
        else:
            new = self.as_ref()
            if isinstance(self.resolved, Mapping) and isinstance(
                self.original, Mapping
            ):
                old = serialize_value(self.original)
                return diff_dicts(old, new)
            return new


class CollectionProxy:
    _values: Any


class ProxyableType(ABCMeta):
    def __instancecheck__(cls, inst):
        """Implement isinstance(inst, cls)."""
        if isinstance(inst, CollectionProxy):
            return isinstance(inst._values, cls)
        return ABCMeta.__instancecheck__(cls, inst)


class Results(ABC, metaclass=ProxyableType):
    """
    Evaluating expressions are not guaranteed to be idempotent (consider quoting)
    and resolving the whole tree up front can lead to evaluations of circular references unless the
    order is carefully chosen. So evaluate lazily and memoize the results.
    This also allows us to track changes to the returned structure.
    """

    __slots__ = (
        "_attributes",
        "context",
        "_deleted",
        "validate",
        "defs",
        "applyTemplates",
    )

    @abstractmethod
    def _values(self) -> Iterator: ...

    @abstractmethod
    def resolve_all(self): ...

    @abstractmethod
    def is_compatible(self, other) -> bool: ...

    def __init__(
        self,
        serializedOriginal,
        resourceOrCxt,
        validate=False,
        defs: Optional[Dict[str, Property]] = None,
    ):
        from .eval import RefContext

        assert not isinstance(serializedOriginal, Results), serializedOriginal
        self._attributes = serializedOriginal.copy()
        self._deleted: dict = {}
        self.applyTemplates = True
        if not isinstance(resourceOrCxt, RefContext):
            ctx = RefContext(resourceOrCxt)
        else:
            ctx = resourceOrCxt
        assert not any(
            isinstance(x, Result) and not isinstance(x, ResultsItem)
            for x in self._attributes
        )

        oldBaseDir = ctx.base_dir
        newBaseDir = getattr(serializedOriginal, "base_dir", oldBaseDir)
        if newBaseDir and newBaseDir != oldBaseDir:
            ctx = ctx.copy()
            ctx.base_dir = newBaseDir
            ctx.trace("found baseDir", newBaseDir, "old", oldBaseDir)
        self.context = ctx
        resource = ctx.currentResource
        self.validate = validate
        if defs is None:
            self.defs = resource.template and resource.template.propertyDefs or {}
        else:
            self.defs = defs

    def get_copy(self, key, default=None):
        # return a copy of value or default if not found
        from .eval import map_value

        try:
            return map_value(self._get(key), self.context)
        except (KeyError, IndexError):
            return default

    @property
    def change_count(self) -> int:
        # change_count is at least shared across _map_values()
        return self.context.referenced.change_count

    def bump_change_count(self) -> int:
        self.context.referenced.change_count += 1
        return self.context.referenced.change_count

    @staticmethod
    def _map_value(
        val, context, applyTemplates=True, defs=None
    ) -> Union[Result, "Results", Any]:
        "Recursively and lazily resolves any references in a value"
        from .eval import map_value, Ref

        if isinstance(val, Results):
            return val
        elif Ref.is_ref(val):
            results = Ref(val).resolve(context, wantList="result")
            if "foreach" in val or len(results) > 1:
                # the whole list was computed, so mark items as computed (by setting the change_count)
                items = [
                    ResultsItem(r, seen=context.referenced.change_count)
                    for r in results
                ]
                return ResultsList(items, context, False, defs or {})
            elif len(results) == 1:
                return results[0]
            else:
                return None
        elif isinstance(val, sensitive):
            return val
        elif isinstance(val, PortSpec):
            return val
        elif isinstance(val, Mapping):
            # already validated
            # always explicitly set defs
            return ResultsMap(val, context, False, defs or {})
        elif isinstance(val, list):
            # already validated
            # always explicitly set defs
            if len(val) == 1 and isinstance(val[0], Result):
                # XXX! see test_localConfig in test_cli.py
                return val[0]
            items = [ResultsItem(r) if isinstance(r, Result) else r for r in val]
            return ResultsList(val, context, False, defs or {})
        else:
            from .support import is_template, apply_template

            if applyTemplates and is_template(val, context):
                return apply_template(val, context.copy(wantList="result"))
            else:
                return val

    def __sensitive__(self):
        # only check resolved values
        return any(isinstance(x, Result) and is_sensitive(x) for x in self._values())

    def has_diff(self):
        # only check resolved values
        # XXX also check if items were added or removed
        return any(isinstance(x, ResultsItem) and x.has_diff() for x in self._values())

    def __getitem__(self, key):
        return self._get(key)

    def _get(self, key):
        return self._getresult(key).resolved

    def _getresult(self, key, validate: Optional[bool] = None) -> ResultsItem:
        val = self._attributes[key]
        if val is _RecursionGuard:
            self.context.trace("Recursion guard set in Results, returning None", key)
            return ResultsItem(None, val, self.change_count)
        else:
            self._attributes[key] = _RecursionGuard
            try:
                if isinstance(val, ResultsItem):
                    # previously resolved
                    if (
                        val.last_computed < self.change_count
                        and val.original is not _Missing
                    ):
                        # need to re-evaluate
                        result = self.resolve(key, val.original, validate)
                        val.set_resolved(result, self.change_count)
                else:
                    assert not isinstance(val, Result), val
                    result = self.resolve(key, val, validate)
                    if is_computed(val):
                        computed = self.change_count
                    else:  # marks as not computed:
                        computed = MAX_CHANGE_COUNT
                    val = ResultsItem(result, val, computed)
                return val
            finally:
                # set the new val or restore the old one
                self._attributes[key] = val

    def __setitem__(self, key, value) -> None:
        # value is treated as a concrete value, it is never evaluated again
        if self.validate:
            self._validate(key, value)
        if self.defs and is_sensitive_schema(self.defs, key):
            value = wrap_sensitive_value(value)

        assert not isinstance(value, Result), value
        if key not in self._attributes:
            self._attributes[key] = ResultsItem(value, _Missing)
        else:
            old_val = self._attributes[key]
            if isinstance(old_val, ResultsItem):
                if old_val.resolved != value:  # only set if values are different
                    if self.validate:
                        self._validate(key, value)
                    old_val.update_value(value)
                    self.bump_change_count()
            else:
                # hasn't been evaluate yet
                if value != old_val:
                    # if old_val is computed we don't know if it's unequal without evaluating old_val
                    # but don't bother with that, the caller can compare the resolved item if they care
                    if self.validate:
                        self._validate(key, value)
                    self._attributes[key] = ResultsItem(value, old_val)

        # remove from deleted if it's there
        self._deleted.pop(key, None)

    def resolve(self, key, val, validate: Optional[bool] = None) -> Result:
        # lazily evaluate lists and dicts
        self.context.trace("Results._mapValue", key, val)
        defs = self.get_datatype_defs(key)
        resolved = self._map_value(val, self.context, self.applyTemplates, defs)
        # will return a Result if it was val was an expression that was evaluated
        if isinstance(resolved, Result):
            result = resolved
        else:
            result = Result(resolved)
        resolved = result.resolved = self._transform(key, result.resolved)
        if isinstance(resolved, MutableSequence) and resolved:
            # make sure we don't have a List[Result]
            assert not isinstance(resolved[0], Result), resolved[0]

        if self.validate if validate is None else validate:
            self._validate(key, resolved, val)
        if self.defs and is_sensitive_schema(self.defs, key):
            result.resolved = wrap_sensitive_value(resolved)

        assert not isinstance(result.resolved, Result)
        return result

    def get_datatype_defs(self, key) -> Optional[Dict[str, Any]]:
        property = self.defs.get(key)
        if property:
            # if property is not a complex datatype this will return {}
            return property.entity.properties
        return None

    def _transform(self, key, value):
        from .eval import map_value

        property = self.defs.get(key)
        if property:
            transform = self._get_prop_metadata_key(property, "transform")
            if transform:
                logger.trace(
                    "running transform on %s.%s", self.context.currentResource.name, key
                )
                try:
                    return map_value(
                        transform, self.context.copy(vars=dict(value=value))
                    )
                except Exception:
                    logger.error(
                        "transform on %s.%s failed.",
                        self.context.currentResource.name,
                        key,
                        exc_info=True,
                    )
        return value

    @staticmethod
    def _get_prop_metadata_key(property_def: Property, key):
        value = property_def.schema.metadata.get(key)
        if not value and property_def.entity.datatype.defs:
            metadata = property_def.entity.datatype.defs.get("metadata")
            if metadata:
                value = metadata.get(key)
        return value

    def _validate(self, key: str, value, src=None, propDef: Optional[Property] = None):
        from .eval import Ref

        propDef = propDef or self.defs.get(key)
        if not propDef:
            return True
        resource = self.context.currentResource
        try:
            if value is None:
                # required attributes might be null depending on the state of the resource
                if (
                    propDef.required
                    and resource.template
                    and key not in resource.template.attributeDefs
                ):
                    msg = f'Property "{key}" on "{resource.template.name}" cannot be null.'
                    raise ValidationError(message=msg)
            else:
                propDef._validate(value)
                validate_expr = self._get_prop_metadata_key(propDef, "validation")
                if validate_expr:
                    if not Ref(
                        validate_expr, vars=dict(property=key, value=value)
                    ).resolve_one(self.context):
                        raise UnfurlError(f"validation failed for {validate_expr}")
            self.context.trace(f'Validated "{key}" on "{resource.name}')
        except Exception as err:
            msg = f'Validation failure while evaluating "{key}" on "{resource.name}": {err}'
            if self.context.task:
                _validation_error(src, self.context, propDef, msg)
            elif self.context.strict:
                raise UnfurlError(msg, True)
            else:
                self.context.trace(msg)
            return False
        return True

    def _haskey(self, key):
        # can't use "in" operator for lists
        try:
            self._attributes[key]
            return True
        except:  # IndexError or KeyError
            return False

    def __delitem__(self, index):
        if self.context.currentResource.readonly:
            raise UnfurlError(
                "Attempting to delete item {item} on a readonly instance {self.context.currentResource}"
            )
        val = self._attributes[index]
        self._deleted[index] = val
        del self._attributes[index]
        if isinstance(val, ResultsItem):
            self.bump_change_count()

    def __len__(self):
        return len(self._attributes)

    def __eq__(self, other):
        if isinstance(other, Results):
            return self._attributes == other._attributes
        else:
            # try to avoid resolve_all()
            if not self.is_compatible(other):
                return False
            if self._attributes == other:
                return True
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

    def get_resolved(self) -> Dict[str, ResultsItem]:
        return {
            key: v for key, v in self._attributes.items() if isinstance(v, ResultsItem)
        }

    def __contains__(self, key):
        return key in self._attributes

    def _values(self):
        return self._attributes.values()

    def get_diff(self, cls=dict):
        # returns a dict with the same semantics as diffDicts
        diffDict = cls()
        for key, val in self._attributes.items():
            if isinstance(val, ResultsItem) and val.has_diff():
                diffDict[key] = val.get_diff()

        for key in self._deleted:
            diffDict[key] = {"+%": "delete"}
        return diffDict

    def is_compatible(self, other):
        return isinstance(other, MutableMapping)


class ResultsList(Results, MutableSequence):
    def insert(self, index, value):
        assert not isinstance(value, Result), value
        self._attributes.insert(index, ResultsItem(value, _Missing))

    def _values(self):
        return self._attributes

    def get_diff(self, cls=list):
        # we don't have patchList yet so just returns the whole list
        return cls(
            val.get_diff() if isinstance(val, ResultsItem) else val
            for val in self._attributes
        )

    def resolve_all(self):
        list(self)

    def is_compatible(self, other):
        return isinstance(other, MutableSequence)
