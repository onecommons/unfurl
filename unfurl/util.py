import sys
import six
import traceback
import itertools
import tempfile
import atexit
import json
import re
import os
import fnmatch
import shutil

if os.name == "posix" and sys.version_info[0] < 3:
    import subprocess32 as subprocess
else:
    import subprocess

from collections import Mapping
import os.path
from jsonschema import Draft7Validator, validators, RefResolver
from ruamel.yaml.scalarstring import ScalarString, FoldedScalarString
from ruamel.yaml import YAML
import logging

logger = logging.getLogger("unfurl")

# import pickle
pickleVersion = 2  # pickle.DEFAULT_PROTOCOL

API_VERSION = "unfurl/v1alpha1"


class UnfurlError(Exception):
    def __init__(self, message, saveStack=False, log=False):
        if saveStack:
            (etype, value, traceback) = sys.exc_info()
            if value:
                message = str(message) + ": " + str(value)
        super(UnfurlError, self).__init__(message)
        self.stackInfo = (etype, value, traceback) if saveStack and value else None
        if log:
            logger.error(message, exc_info=True)

    def getStackTrace(self):
        if not self.stackInfo:
            return ""
        return "".join(traceback.format_exception(*self.stackInfo))


class UnfurlValidationError(UnfurlError):
    def __init__(self, message, errors=None):
        super(UnfurlValidationError, self).__init__(message)
        self.errors = errors or []


class UnfurlTaskError(UnfurlError):
    def __init__(self, task, message, log=logging.ERROR):
        super(UnfurlTaskError, self).__init__(message, True, False)
        self.task = task
        task._errors.append(self)
        self.severity = log
        if log:
            task.logger.log(log, message, exc_info=True)


class UnfurlAddingResourceError(UnfurlTaskError):
    def __init__(self, task, resourceSpec, log=logging.WARNING):
        resourcename = isinstance(resourceSpec, Mapping) and resourceSpec.get(
            "name", ""
        )
        message = "error creating resource %s" % resourcename
        super(UnfurlAddingResourceError, self).__init__(task, message, log)
        self.resourceSpec = resourceSpec


class sensitive_str(str):
    redacted_str = "<<REDACTED>>"


def toYamlText(val):
    if isinstance(val, (ScalarString, sensitive_str)):
        return val
    # convert or copy string (copy to deal with things like AnsibleUnsafeText)
    val = str(val)
    if "\n" in val:
        return FoldedScalarString(val)
    return val


def assertForm(src, types=Mapping):
    if not isinstance(src, types):
        raise UnfurlError("Malformed definition: %s" % src)
    return src


# map< apiversion, map<kind, ctor> >
_ClassRegistry = {}
# only one class can be associated with an api interface
def registerClass(apiVersion, kind, factory, replace=False):
    api = _ClassRegistry.setdefault(apiVersion, {})
    if not replace and kind in api:
        if api[kind] is not factory:
            raise UnfurlError("class already registered for %s.%s" % (apiVersion, kind))
    api[kind] = factory


class AutoRegisterClass(type):
    def __new__(mcls, name, bases, dct):
        cls = type.__new__(mcls, name, bases, dct)
        registerClass(API_VERSION, name, cls)
        return cls


import warnings

try:
    import importlib.util

    imp = None
except ImportError:
    import imp
from ansible.module_utils._text import to_bytes, to_text, to_native  # BSD licensed


def loadModule(path, full_name=None):
    if full_name is None:
        full_name = re.sub(r"\W", "_", path)  # generate a name from the path
    if full_name in sys.modules:
        return sys.modules[full_name]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        if imp is None:
            spec = importlib.util.spec_from_file_location(
                to_native(full_name), to_native(path)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            sys.modules[full_name] = module
        else:
            with open(to_bytes(path), "rb") as module_file:
                # to_native is used here because imp.load_source's path is for tracebacks and python's traceback formatting uses native strings
                module = imp.load_source(
                    to_native(full_name), to_native(path), module_file
                )
    return module


def loadClass(klass, defaultModule="__main__"):
    import importlib

    prefix, sep, suffix = klass.rpartition(".")
    module = importlib.import_module(prefix or defaultModule)
    return getattr(module, suffix, None)


def lookupClass(kind, apiVersion=None, default=None):
    version = apiVersion or API_VERSION
    api = _ClassRegistry.get(version)
    if api:
        klass = api.get(kind, default)
    else:
        klass = default

    if not klass:
        try:
            klass = loadClass(kind)
        except ImportError:
            klass = None

        if klass:
            registerClass(version, kind, klass, True)
        else:
            raise UnfurlError("Can not find class %s.%s" % (version, kind))
    return klass


def toEnum(enum, value, default=None):
    # from string: Status[name]; to string: status.name
    if isinstance(value, six.string_types):
        return enum[value]
    elif default is not None and not value:
        return default
    elif isinstance(value, int):
        return enum(value)
    else:
        return value


def _dump(obj, tp, suffix="", yaml=None):
    from .yamlloader import yaml as _yaml

    if suffix.endswith(".yml") or suffix.endswith(".yaml"):
        (yaml or _yaml).dump(obj, tp)
    elif suffix.endswith(".json") or not isinstance(obj, six.string_types):
        json.dump(obj, tp, indent=2)
    else:
        tp.write(obj)


def saveToFile(path, obj, yaml=None):
    dir = os.path.dirname(path)
    if dir and not os.path.isdir(dir):
        os.makedirs(dir)
    with open(path, "w") as f:
        _dump(obj, f, path, yaml)


def saveToTempfile(obj, suffix="", delete=True, dir=None):
    tp = tempfile.NamedTemporaryFile(
        "w+t", suffix=suffix, delete=False, dir=dir or os.environ.get("UNFURL_TMPDIR")
    )
    if delete:
        atexit.register(lambda: os.path.exists(tp.name) and os.unlink(tp.name))
    try:
        _dump(obj, tp, suffix)
    finally:
        tp.close()
    return tp


def makeTempDir(delete=True, prefix="unfurl"):
    tempDir = tempfile.mkdtemp(prefix, dir=os.environ.get("UNFURL_TMPDIR"))
    if delete:
        atexit.register(lambda: os.path.isdir(tempDir) and shutil.rmtree(tempDir))
    return tempDir


# https://python-jsonschema.readthedocs.io/en/latest/faq/#why-doesn-t-my-schema-s-default-property-set-the-default-on-my-instance
# XXX unused because this breaks check_schema
def extend_with_default(validator_class):
    """
  # Example usage:
  obj = {}
  schema = {'properties': {'foo': {'default': 'bar'}}}
  # Note jsonschema.validate(obj, schema, cls=DefaultValidatingDraft7Validator)
  # will not work because the metaschema contains `default` directives.
  DefaultValidatingDraft7Validator(schema).validate(obj)
  assert obj == {'foo': 'bar'}
  """
    validate_properties = validator_class.VALIDATORS["properties"]

    def set_defaults(validator, properties, instance, schema):
        if not validator.is_type(instance, "object"):
            return

        for key, subschema in properties.items():
            if "default" in subschema:
                instance.setdefault(key, subschema["default"])

        for error in validate_properties(validator, properties, instance, schema):
            yield error

    # new validator class
    return validators.extend(validator_class, {"properties": set_defaults})


DefaultValidatingLatestDraftValidator = (
    Draft7Validator
)  # extend_with_default(Draft4Validator)


def validateSchema(obj, schema, baseUri=None):
    return not findSchemaErrors(obj, schema)


def findSchemaErrors(obj, schema, baseUri=None):
    # XXX2 have option that includes definitions from manifest's schema
    if baseUri is not None:
        resolver = RefResolver(base_uri=baseUri, referrer=schema)
    else:
        resolver = None
    DefaultValidatingLatestDraftValidator.check_schema(schema)
    validator = DefaultValidatingLatestDraftValidator(schema, resolver=resolver)
    errors = list(validator.iter_errors(obj))
    if not errors:
        return None
    message = "\n".join(e.message for e in errors[:1])
    return message, errors


class ChainMap(Mapping):
    """
  Combine multiple mappings for sequential lookup.
  """

    def __init__(self, *maps):
        self._maps = maps

    def split(self):
        return self._maps[0], ChainMap(*self._maps[1:])

    def __getitem__(self, key):
        for mapping in self._maps:
            try:
                return mapping[key]
            except KeyError:
                pass
        raise KeyError(key)

    def __setitem__(self, key, value):
        self._maps[0][key] = value

    def __iter__(self):
        return iter(frozenset(itertools.chain(*self._maps)))

    def __len__(self):
        return len(frozenset(itertools.chain(*self._maps)))

    def __nonzero__(self):
        return all(self._maps)

    def __repr__(self):
        return "ChainMap(%r)" % (self._maps,)


class Generate(object):
    """
        Roughly equivalent to "yield from" but works in Python < 3.3

        Usage:

        >>>  gen = Generate(generator())
        >>>  while gen():
        >>>    gen.result = yield gen.next
    """

    def __init__(self, generator):
        self.generator = generator
        self.result = None
        self.next = None

    def __call__(self):
        try:
            self.next = self.generator.send(self.result)
            return True
        except StopIteration:
            return False


def filterEnv(rules, env=None, addOnly=False):
    """
    foo: bar # add foo=bar
    +!foo*: # add all except keys matching "foo*"
    -!foo: # remove all except foo
    """
    if env is None:
        env = os.environ

    start = {} if addOnly else env.copy()
    for name, val in rules.items():
        if name[:1] in "+-":
            # add or remove from env
            remove = name[1] == "-"
            name = name[1:]
            neg = name[:1] == "!"
            if neg:
                name = name[1:]
            if remove:
                source = start  # if remove, look in the current set, not original
            else:
                source = env
            match = {
                k: v for k, v in source.items() if fnmatch.fnmatchcase(k, name) ^ neg
            }
            if remove:
                [start.pop(k, None) for k in match]
            else:
                start.update(match)
        else:
            start[name] = val
    return start
