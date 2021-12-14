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
from collections.abc import Mapping, MutableSequence
import os.path
from jsonschema import Draft7Validator, validators, RefResolver
import jsonschema.exceptions
from ruamel.yaml.scalarstring import ScalarString, FoldedScalarString
from ansible.parsing.yaml.objects import AnsibleVaultEncryptedUnicode
from ansible.parsing.vault import VaultEditor
from ansible.module_utils._text import to_text, to_bytes, to_native  # BSD licensed
from ansible.utils.unsafe_proxy import AnsibleUnsafeText, AnsibleUnsafeBytes, wrap_var
import warnings
import codecs
import io
from contextlib import contextmanager
from logging import Logger

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
from .logs import sensitive
import logging

logger = logging.getLogger("unfurl")

API_VERSION = "unfurl/v1alpha1"

try:
    from importlib.metadata import files
except ImportError:
    from importlib_metadata import files

_basepath = os.path.abspath(os.path.dirname(__file__))


def get_package_digest():
    from git import Repo

    basedir = os.path.dirname(_basepath)
    if os.path.isdir(os.path.join(basedir, ".git")):
        return Repo(basedir).git.describe("--dirty", "--always")

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
        super().__init__(message)
        self.stackInfo = (etype, value, traceback) if saveStack and value else None
        if log:
            logger.error(message, exc_info=True)

    def get_stack_trace(self):
        if not self.stackInfo:
            return ""
        return "".join(traceback.format_exception(*self.stackInfo))


class UnfurlValidationError(UnfurlError):
    def __init__(self, message, errors=None, log=False):
        super().__init__(message, log=log)
        self.errors = errors or []


class UnfurlTaskError(UnfurlError):
    def __init__(self, task, message, log=logging.ERROR):
        super().__init__(message, True, False)
        self.task = task
        task._errors.append(self)
        self.severity = log
        if log:
            task.logger.log(log, message, exc_info=True)


class UnfurlAddingResourceError(UnfurlTaskError):
    def __init__(self, task, resourceSpec, name, log=logging.DEBUG):
        message = f"error updating resource {name}"
        super().__init__(task, message, log)
        self.resourceSpec = resourceSpec


def wrap_sensitive_value(obj):
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
    elif isinstance(obj, Mapping):
        return sensitive_dict(obj)
    elif isinstance(obj, MutableSequence):
        return sensitive_list(obj)
    else:
        return obj


def is_sensitive(obj):
    test = getattr(obj, "__sensitive__", None)
    if test:
        return test()
    if isinstance(obj, AnsibleVaultEncryptedUnicode):
        return True
    elif isinstance(obj, Mapping):
        return any(is_sensitive(i) for i in obj.values())
    elif isinstance(obj, MutableSequence):
        return any(is_sensitive(i) for i in obj)
    return False


# also mark AnsibleUnsafe so wrap_var doesn't strip out sensitive status
class sensitive_bytes(AnsibleUnsafeBytes, sensitive):
    """Transparent wrapper class to mark bytes as sensitive"""

    def decode(self, *args, **kwargs):
        """Wrapper method to ensure type conversions maintain sensitive context"""
        return sensitive_str(super().decode(*args, **kwargs))


# also mark AnsibleUnsafe so wrap_var doesn't strip out sensitive status
class sensitive_str(AnsibleUnsafeText, sensitive):
    """Transparent wrapper class to mark a str as sensitive"""

    def encode(self, *args, **kwargs):
        """Wrapper method to ensure type conversions maintain sensitive context"""
        return sensitive_bytes(super().encode(*args, **kwargs))


class sensitive_dict(dict, sensitive):
    """Transparent wrapper class to mark a dict as sensitive"""


class sensitive_list(list, sensitive):
    """Transparent wrapper class to mark a list as sensitive"""


def to_yaml_text(val):
    if isinstance(val, (ScalarString, sensitive)):
        return val
    # convert or copy string (copy to deal with things like AnsibleUnsafeText)
    val = str(val)
    if "\n" in val:
        if six.PY2 and isinstance(val, str):
            val = val.decode("utf-8")
        return FoldedScalarString(val)
    return val


def assert_form(src, types=Mapping, test=True):
    if not isinstance(src, types) or not test:
        raise UnfurlError(f"Malformed definition: {src}")
    return src


_ClassRegistry = {}
_shortNameRegistry = {}


def register_short_names(shortNames):
    _shortNameRegistry.update(shortNames)


def register_class(className, factory, short_name=None, replace=True):
    if short_name:
        _shortNameRegistry[short_name] = className
    if not replace and className in _ClassRegistry:
        if _ClassRegistry[className] is not factory:
            raise UnfurlError(f"class already registered for {className}")
    _ClassRegistry[className] = factory


def load_module(path, full_name=None):
    if full_name is None:
        full_name = re.sub(r"\W", "_", path)  # generate a name from the path
    if full_name in sys.modules:
        return sys.modules[full_name]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        if imp is None:  # Python 3
            spec = importlib.util.spec_from_file_location(full_name, path)
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


def load_class(klass, defaultModule="__main__"):
    import importlib

    prefix, sep, suffix = klass.rpartition(".")
    module = importlib.import_module(prefix or defaultModule)
    return getattr(module, suffix, None)


_shortNameRegistry = {}


def lookup_class(kind, apiVersion=None, default=None):
    if kind in _ClassRegistry:
        return _ClassRegistry[kind]
    elif kind in _shortNameRegistry:
        className = _shortNameRegistry[kind]
    else:
        className = kind
    try:
        klass = load_class(className)
    except ImportError:
        klass = None

    if klass:
        register_class(className, klass)
    return klass


def to_enum(enum, value, default=None):
    # from string: Status[name]; to string: status.name
    if isinstance(value, six.string_types):
        return enum[value]
    elif default is not None and not value:
        return default
    elif isinstance(value, int):
        return enum(value)
    else:
        return value


def dump(obj, tp, suffix="", yaml=None, encoding=None):
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
            json.dump(obj, f, indent=2, sort_keys=True)
    finally:
        if six.PY3:
            f.detach()


def _save_to_vault(path, obj, yaml=None, encoding=None, fd=None):
    vaultExt = path.endswith(".vault")
    if vaultExt or encoding == "vault":
        assert yaml and yaml.representer.vault and yaml.representer.vault.secrets
        vaultlib = yaml.representer.vault
        vault = VaultEditor(vaultlib)
        f = io.BytesIO()
        vpath = path[: -len(".vault")] if vaultExt else path
        dump(obj, f, vpath, yaml, encoding)
        # the first vaultid is the most specific to the current project so encrypt with that one
        vault_id, secret = vaultlib.secrets[0]
        b_vaulttext = vaultlib.encrypt(f.getvalue(), secret=secret, vault_id=vault_id)
        vault.write_data(b_vaulttext, fd or path)
        return True
    return False


def save_to_file(path, obj, yaml=None, encoding=None):
    dir = os.path.dirname(path)
    if dir and not os.path.isdir(dir):
        os.makedirs(dir)
    if not _save_to_vault(path, obj, yaml, encoding):
        with open(path, "wb") as f:
            dump(obj, f, path, yaml, encoding)


def save_to_tempfile(obj, suffix="", delete=True, dir=None, yaml=None, encoding=None):
    tp = tempfile.NamedTemporaryFile(
        "w+b",
        suffix=suffix or "",
        delete=False,
        dir=dir or os.environ.get("UNFURL_TMPDIR"),
    )
    if delete:
        atexit.register(lambda: os.path.exists(tp.name) and os.unlink(tp.name))

    if not _save_to_vault(suffix or "", obj, yaml, encoding, tp.fileno()):
        try:
            dump(obj, tp, suffix or "", yaml, encoding)
        finally:
            tp.close()
    return tp


def make_temp_dir(delete=True, prefix="unfurl"):
    tempDir = tempfile.mkdtemp(prefix, dir=os.environ.get("UNFURL_TMPDIR"))
    if delete:
        atexit.register(lambda: os.path.isdir(tempDir) and shutil.rmtree(tempDir))
    return tempDir


def get_base_dir(path):
    if os.path.exists(path):
        isdir = os.path.isdir(path)
    else:
        isdir = not os.path.splitext(path)[1]
    if isdir:
        return path
    else:
        return os.path.normpath(os.path.dirname(path))


@contextmanager
def change_cwd(new_path: str, log: Logger = None):
    """Temporarily change current working directory"""
    old_path = os.getcwd()
    if new_path:
        if log:
            log.trace("Changing CWD to: %s", new_path)
        os.chdir(new_path)
    try:
        yield
    finally:
        if log:
            log.trace("Restoring CWD back to: %s", old_path)
        os.chdir(old_path)


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


def validate_schema(obj, schema, baseUri=None):
    return not find_schema_errors(obj, schema)


def find_schema_errors(obj, schema, baseUri=None):
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
    message = "%s in %s" % (
        error.message,
        "/".join([str(p) for p in error.absolute_path]),
    )
    return message, errors


class ChainMap(Mapping):
    """
    Combine multiple mappings for sequential lookup.
    """

    def __init__(self, *maps, **kw):
        self._maps = maps
        self._track = kw.get("track")
        self.accessed = set()

    def split(self):
        return self._maps[0], ChainMap(*self._maps[1:])

    def __getitem__(self, key):
        for mapping in self._maps:
            try:
                return mapping[key]
            except KeyError:
                pass
            else:
                if self._track is mapping:
                    self.accessed.add(key)
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


class Generate:
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


def filter_env(rules, env=None, addOnly=False):
    """
    Applies the given list of rules to a dictionary of environment variables and returns a new dictionary.

    Args:
        rules (dict): A dictionary of rules for adding, removing and filtering environment variables.
        env (dict, optional): The environment to apply the give rules to. If ``env`` is None it will be set to ``os.environ``.
          Defaults to None.
        addOnly (bool, optional): If addOnly is False (the default) all variables in ``env`` will be included
          in the returned dict, otherwise only variables added by ``rules`` will be included

    Rules applied in the order they are declared in the ``rules`` dictionary. The following examples show the different patterns for the rules:

        :foo \: bar: Add ``foo=bar``
        :+foo:  Copy ``foo`` from the current environment
        :+foo \: bar: Copy ``foo``, or add ``foo=bar`` if it is not present
        :+!foo*: Copy all name from the current environment except those matching ``foo*``
        :-!foo:  Remove all names except for ``foo``
        :^foo \: /bar/bin: Treat ``foo`` like ``PATH`` and prepend ``/bar/bin:$foo``
    """
    if env is None:
        env = os.environ

    if addOnly:
        start = {}
    else:
        start = env.copy()
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
        elif val is not None:  # don't set if val is None
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
    "VIRTUAL_ENV",
    "UNFURL_TMPDIR",
    "UNFURL_LOGGING",
    "UNFURL_HOME",
    "UNFURL_RUNTIME",
    "UNFURL_NORUNTIME",
    "UNFURL_APPROVE",
]
# hack for sphinx ext documentedlist
_sphinx_envvars = [(i,) for i in required_envvars]
