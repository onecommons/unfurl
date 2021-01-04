# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
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
import collections

if os.name == "posix" and sys.version_info[0] < 3:
    import subprocess32 as subprocess
else:
    import subprocess

from collections import Mapping
import os.path
from jsonschema import Draft7Validator, validators, RefResolver
import jsonschema.exceptions
from ruamel.yaml.scalarstring import ScalarString, FoldedScalarString
from ansible.parsing.yaml.objects import AnsibleVaultEncryptedUnicode
from ansible.parsing.vault import VaultEditor
from ansible.module_utils._text import to_text, to_bytes, to_native  # BSD licensed
import warnings
import codecs
import io

try:
    from shutil import which
except ImportError:
    from distutils import spawn

    def which(executable, mode=os.F_OK | os.X_OK, path=None):
        executable = spawn.find_executable(executable, path)
        if executable:
            if os.access(executable, mode):
                return executable
        return None


try:
    import importlib.util

    imp = None
except ImportError:
    import imp
from . import sensitive
import logging

logger = logging.getLogger("unfurl")

API_VERSION = "unfurl/v1alpha1"

try:
    from importlib.metadata import files
except ImportError:
    from importlib_metadata import files

_basepath = os.path.abspath(os.path.dirname(__file__))


def getPackageDigest():
    from git import Repo

    basedir = os.path.dirname(_basepath)
    if os.path.isdir(os.path.join(basedir, ".git")):
        return Repo(basedir).git.describe("--dirty")

    try:
        pbr = [p for p in files("unfurl") if "pbr.json" in str(p)][0]
        return json.loads(pbr.read_text())["git_version"]
    except:
        return ""


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


def wrapSensitiveValue(obj, vault=None):
    # we don't remember the vault and vault id associated with this value
    # so the value will be rekeyed with whichever vault is associated with the serializing yaml
    if isinstance(obj, bytes):
        if six.PY3:
            return sensitive_bytes(obj)
        else:
            try:
                return sensitive_str(unicode(obj, "utf-8"))
            except UnicodeDecodeError:
                return sensitive_bytes(obj)
    elif isinstance(obj, six.string_types):
        return sensitive_str(obj)
    elif isinstance(obj, collections.Mapping):
        return sensitive_dict(obj)
    elif isinstance(obj, collections.MutableSequence):
        return sensitive_list(obj)
    else:
        return obj


def isSensitive(obj):
    test = getattr(obj, "__sensitive__", None)
    if test:
        return test()
    if isinstance(obj, AnsibleVaultEncryptedUnicode):
        return True
    elif isinstance(obj, collections.Mapping):
        return any(isSensitive(i) for i in obj.values())
    elif isinstance(obj, collections.MutableSequence):
        return any(isSensitive(i) for i in obj)
    return False


class sensitive_bytes(six.binary_type, sensitive):
    """Transparent wrapper class to mark bytes as sensitive"""


if six.PY3:

    class sensitive_str(str, sensitive):
        """Transparent wrapper class to mark a str as sensitive"""


else:

    class sensitive_str(unicode, sensitive):
        """Transparent wrapper class to mark a str as sensitive"""


class sensitive_dict(dict, sensitive):
    """Transparent wrapper class to mark a dict as sensitive"""


class sensitive_list(list, sensitive):
    """Transparent wrapper class to mark a list as sensitive"""


def toYamlText(val):
    if isinstance(val, (ScalarString, sensitive)):
        return val
    # convert or copy string (copy to deal with things like AnsibleUnsafeText)
    val = str(val)
    if "\n" in val:
        return FoldedScalarString(val)
    return val


def assertForm(src, types=Mapping, test=True):
    if not isinstance(src, types) or not test:
        raise UnfurlError("Malformed definition: %s" % src)
    return src


_ClassRegistry = {}
_shortNameRegistry = {}


def registerShortNames(shortNames):
    _shortNameRegistry.update(shortNames)


def registerClass(className, factory, shortName=None, replace=True):
    if shortName:
        _shortNameRegistry[shortName] = className
    if not replace and className in _ClassRegistry:
        if _ClassRegistry[className] is not factory:
            raise UnfurlError("class already registered for %s" % className)
    _ClassRegistry[className] = factory


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


_shortNameRegistry = {}


def lookupClass(kind, apiVersion=None, default=None):
    if kind in _ClassRegistry:
        return _ClassRegistry[kind]
    elif kind in _shortNameRegistry:
        className = _shortNameRegistry[kind]
    else:
        className = kind
    try:
        klass = loadClass(className)
    except ImportError:
        klass = None

    if klass:
        registerClass(className, klass)
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


def _dump(obj, tp, suffix="", yaml=None, encoding=None):
    from .yamlloader import yaml as _yaml

    try:
        if six.PY3:
            textEncoding = (
                "utf-8" if encoding in [None, "yaml", "vault", "json"] else encoding
            )
            f = io.TextIOWrapper(tp, textEncoding)
        else:
            f = tp
        if suffix.endswith(".yml") or suffix.endswith(".yaml") or encoding == "yaml":
            (yaml or _yaml).dump(obj, f)
            return
        elif suffix.endswith(".json") or encoding == "json":
            json.dump(obj, f, indent=2)  # str to bytes
            return

        if six.PY3:
            if isinstance(obj, str):
                obj = codecs.encode(obj, encoding or "utf-8")
            if isinstance(obj, bytes):
                tp.write(obj)
                return
        else:
            if isinstance(obj, unicode):
                obj = codecs.encode(obj, encoding or "utf-8")
            if isinstance(obj, str):
                tp.write(obj)
                return

        # try to dump any other object as json
        if obj is not None:  # treat None as 0 byte file
            json.dump(obj, f, indent=2)
    finally:
        if six.PY3:
            f.detach()


def _saveToVault(path, obj, yaml=None, encoding=None, fd=None):
    vaultExt = path.endswith(".vault")
    if vaultExt or encoding == "vault":
        assert yaml and yaml.representer.vault
        vault = VaultEditor(yaml.representer.vault)
        f = io.BytesIO()
        vpath = path[: -len(".vault")] if vaultExt else path
        _dump(obj, f, vpath, yaml, encoding)
        b_vaulttext = yaml.representer.vault.encrypt(f.getvalue())
        vault.write_data(b_vaulttext, fd or path)
        return True
    return False


def saveToFile(path, obj, yaml=None, encoding=None):
    dir = os.path.dirname(path)
    if dir and not os.path.isdir(dir):
        os.makedirs(dir)
    if not _saveToVault(path, obj, yaml, encoding):
        with open(path, "wb") as f:
            _dump(obj, f, path, yaml, encoding)


def saveToTempfile(obj, suffix="", delete=True, dir=None, yaml=None, encoding=None):
    tp = tempfile.NamedTemporaryFile(
        "w+b",
        suffix=suffix or "",
        delete=False,
        dir=dir or os.environ.get("UNFURL_TMPDIR"),
    )
    if delete:
        atexit.register(lambda: os.path.exists(tp.name) and os.unlink(tp.name))

    if not _saveToVault(suffix or "", obj, yaml, encoding, tp.fileno()):
        try:
            _dump(obj, tp, suffix or "", yaml, encoding)
        finally:
            tp.close()
    return tp


def makeTempDir(delete=True, prefix="unfurl"):
    tempDir = tempfile.mkdtemp(prefix, dir=os.environ.get("UNFURL_TMPDIR"))
    if delete:
        atexit.register(lambda: os.path.isdir(tempDir) and shutil.rmtree(tempDir))
    return tempDir


def getBaseDir(path):
    if os.path.exists(path):
        isdir = os.path.isdir(path)
    else:
        isdir = not os.path.splitext(path)[1]
    if isdir:
        return path
    else:
        return os.path.normpath(os.path.dirname(path))


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
    Draft7Validator  # extend_with_default(Draft4Validator)
)


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
    error = jsonschema.exceptions.best_match(errors)
    if not error:
        return None
    message = "%s in %s" % (error.message, "/".join(error.absolute_path))
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
    If 'env' is None it will be set to os.environ

    If addOnly is False (the default) all variables in `env` will be included
    in the returned dict, otherwise only variables added by `rules` will be included

    foo: bar # add foo=bar
    +foo: # copy foo
    +foo: bar # copy foo, set it bar if not present
    +!foo*: # copy all except keys matching "foo*"
    -!foo: # remove all except foo
    ^foo: /bar/bin # treat foo like a PATH and prepend value: /bar/bin:$foo
    """
    if env is None:
        env = os.environ

    start = {} if addOnly else env.copy()
    for name, val in rules.items():
        if name[:1] in "+-^":
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
                [
                    start.pop(k, None)
                    for k in match
                    # required_envars need to an explicit rule to be removed
                    if k not in required_envvars or name in required_envvars
                ]
            else:
                if match:
                    if name[1] == "^" and val:
                        # treat as PATH-like and prepend val
                        match = {k: (val + os.pathsep + v) for k, v in match.items()}
                    start.update(match)
                elif val is not None:  # name isn't in existing, use default is set
                    start[name] = val
        else:
            start[name] = val
    return start


# copied from tox
required_envvars = [
    "TMPDIR",
    "CURL_CA_BUNDLE",
    "PATH",
    "LANG",
    "LANGUAGE",
    "LD_LIBRARY_PATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "PYTHONPATH",
    "UNFURL_TMPDIR",
    "UNFURL_LOGGING",
    "UNFURL_HOME",
    "UNFURL_RUNTIME",
    "UNFURL_NORUNTIME",
]
# hack for sphinx ext documentedlist
_sphinx_envvars = [(i,) for i in required_envvars]
