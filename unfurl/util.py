import sys
import optparse
import six
import traceback
import itertools
import tempfile
import atexit
import json
import re

from collections import Mapping
import os.path
from jsonschema import Draft4Validator, validators, RefResolver
from ruamel.yaml.scalarstring import ScalarString, FoldedScalarString
from ruamel.yaml import YAML
import logging

logger = logging.getLogger("unfurl")

# import pickle
pickleVersion = 2  # pickle.DEFAULT_PROTOCOL

from ansible.plugins.loader import lookup_loader, filter_loader, strategy_loader

lookup_loader.add_directory(os.path.abspath(os.path.dirname(__file__)), True)
filter_loader.add_directory(os.path.abspath(os.path.dirname(__file__)), True)
strategy_loader.add_directory(
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "vendor",
            "ansible_mitogen",
            "plugins",
            "strategy",
        )
    ),
    False,
)


class AnsibleDummyCli(object):
    def __init__(self):
        self.options = optparse.Values()


ansibleDummyCli = AnsibleDummyCli()
from ansible.utils import display

display.logger = logging.getLogger("unfurl.ansible")


class AnsibleDisplay(display.Display):
    def display(self, msg, color=None, stderr=False, screen_only=False, log_only=False):
        if screen_only:
            return
        log_only = True
        return super(AnsibleDisplay, self).display(
            msg, color, stderr, screen_only, log_only
        )


import ansible.constants as C

if "ANSIBLE_NOCOWS" not in os.environ:
    C.ANSIBLE_NOCOWS = 1
if "ANSIBLE_JINJA2_NATIVE" not in os.environ:
    C.DEFAULT_JINJA2_NATIVE = 1
ansibleDisplay = AnsibleDisplay()


def initializeAnsible():
    main = sys.modules.get("__main__")
    # XXX make sure ansible.executor.playbook_executor hasn't been loaded already
    main.display = ansibleDisplay
    main.cli = ansibleDummyCli


initializeAnsible()

VERSION = (
    "unfurl/v1alpha1"
)  # XXX rename to api_version, to distinguish from __version__ and TOSCA_VERSION


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
    def __init__(self, task, message, log=False):
        super(UnfurlTaskError, self).__init__(message, True, log)
        self.task = task
        task.errors.append(self)


class UnfurlAddingResourceError(UnfurlTaskError):
    def __init__(self, task, resourceSpec, log=False):
        resourcename = isinstance(resourceSpec, Mapping) and resourceSpec.get(
            "name", ""
        )
        message = "error creating resource %s" % resourcename
        super(UnfurlAddingResourceError, self).__init__(task, message, log)
        self.resourceSpec = resourceSpec


class sensitive_str(str):
    pass


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
        registerClass(VERSION, name, cls)
        return cls


import warnings

try:
    import importlib.util

    imp = None
except ImportError:
    import imp
from ansible.module_utils._text import to_bytes, to_native  # BSD licensed


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
    version = apiVersion or VERSION
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


def saveToTempfile(obj, suffix="", delete=True):
    tp = tempfile.NamedTemporaryFile("w+t", suffix=suffix, delete=False)
    if delete:
        atexit.register(lambda: os.unlink(tp.name))
    try:
        if suffix.endswith(".yml") or suffix.endswith(".yaml"):
            YAML().dump(obj, tp)
        elif suffix.endswith(".json") or not isinstance(obj, six.string_types):
            json.dump(obj, tp)
        else:
            tp.write(obj)
    finally:
        tp.close()
    return tp


# https://python-jsonschema.readthedocs.io/en/latest/faq/#why-doesn-t-my-schema-s-default-property-set-the-default-on-my-instance
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


DefaultValidatingLatestDraftValidator = extend_with_default(Draft4Validator)


def validateSchema(obj, schema, baseUri=None):
    return not findSchemaErrors(obj, schema)


def findSchemaErrors(obj, schema, baseUri=None):
    # XXX2 have option that includes definitions from manifest's schema
    if baseUri is not None:
        resolver = RefResolver(base_uri=baseUri, referrer=schema)
    else:
        resolver = None
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
