# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
from pathlib import Path
import sys
from types import ModuleType
from typing import (
    IO,
    Any,
    Dict,
    Generator,
    Iterable,
    List,
    MutableMapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
    Iterator,
    TYPE_CHECKING,
    cast,
    overload,
)
from typing_extensions import SupportsIndex
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
from ansible.parsing.vault import VaultEditor
from ansible.module_utils._text import (
    to_text,
    to_bytes,
    to_native,
)  # BSD licensed  # noqa: F401
from ansible.utils.unsafe_proxy import AnsibleUnsafeText, AnsibleUnsafeBytes, wrap_var  # noqa: F401
import warnings
import codecs
import io
from contextlib import contextmanager
from click.termui import unstyle

from shutil import which  # noqa: F401
import importlib
import importlib.util
from .logs import LogExtraLevels, sensitive, is_sensitive
import logging

# Used for python typing, prevents circular imports
# TYPE_CHECKING is always false at runtime
if TYPE_CHECKING:
    from .job import ConfigTask
    from unfurl.configurator import TaskView

logger = logging.getLogger("unfurl")

API_VERSION = "unfurl/v1.0.0"

try:
    from importlib.metadata import files
except ImportError:
    from importlib_metadata import files  # type: ignore

_basepath = os.path.abspath(os.path.dirname(__file__))

_package_digest: Optional[str] = None


def get_package_digest() -> str:
    global _package_digest
    if _package_digest is not None:
        return _package_digest

    from git import Repo

    basedir = os.path.dirname(_basepath)
    if os.path.isdir(os.path.join(basedir, ".git")):
        repo = Repo(basedir)
        # same as pbr's git_version
        _package_digest = repo.git.log(n=1, pretty="format:%h")
    else:
        _package_digest = ""
        pkg_files = files("unfurl")
        if pkg_files:
            try:
                pbr = [p for p in pkg_files if "pbr.json" in str(p)][0]
                _package_digest = json.loads(pbr.read_text())["git_version"]
            except Exception:  # no git or pbr.json
                _package_digest = ""
    return str(_package_digest)


class UnfurlError(Exception):
    def __init__(
        self, message: object, saveStack: bool = False, log: bool = False
    ) -> None:
        stackInfo = None
        if saveStack:
            (e_type, e, traceback) = sys.exc_info()
            if e:
                message = str(message) + ": " + str(e)
                stackInfo = (e_type, e, traceback)
        super().__init__(message)
        self.stackInfo = stackInfo
        if log:
            logger.error(message, exc_info=True)

    def get_stack_trace(self) -> str:
        if not self.stackInfo:
            return ""
        return "".join(traceback.format_exception(*self.stackInfo))


class UnfurlValidationError(UnfurlError):
    def __init__(
        self,
        message: object,
        errors: Optional[List[Exception]] = None,
        log: bool = False,
    ) -> None:
        super().__init__(message, log=log)
        self.errors = errors or []


class UnfurlBadDocumentError(UnfurlError):
    def __init__(
        self,
        message: object,
        errors: Optional[Tuple[str, List[object]]] = None,
        doc: Optional[dict] = None,
        saveStack: bool = False,
    ) -> None:
        super().__init__(message, saveStack=saveStack)
        self.errors = cast(Tuple[str, List[object]], errors or [])
        self.doc = doc


class UnfurlSchemaError(UnfurlBadDocumentError):
    pass


class UnfurlTaskError(UnfurlError):
    def __init__(
        self,
        task: "TaskView",
        message: object,
        log: int = logging.ERROR,
        dependency=None,
    ):
        task = cast("ConfigTask", task)
        super().__init__(message, True, False)
        self.task = task
        task._errors.append(self)
        self.severity = log
        self.dependency = dependency  # what caused the error
        if log:
            task.logger.log(log, message, exc_info=True)


class UnfurlAddingResourceError(UnfurlTaskError):
    def __init__(
        self,
        task: "TaskView",
        resourceSpec: Mapping,
        name: str,
        log: int = logging.DEBUG,
    ) -> None:
        message = f'error updating resource "{name}"'
        super().__init__(task, message, log)
        self.resourceSpec = resourceSpec


def wrap_sensitive_value(obj: object):
    # convert to sensitive obj if possible or return the original object
    # Note: we don't remember the vault and vault id associated with this value
    # so the value will be rekeyed with whichever vault is associated with the serializing yaml
    if isinstance(obj, sensitive):
        return obj
    elif isinstance(obj, bytes):
        return sensitive_bytes(obj)
    elif isinstance(obj, str):
        return sensitive_str(obj)
    elif isinstance(obj, Mapping):
        return sensitive_dict(obj)
    elif isinstance(obj, MutableSequence):
        return sensitive_list(obj)
    else:
        return obj


# also mark AnsibleUnsafe so wrap_var doesn't strip out sensitive status
class sensitive_bytes(AnsibleUnsafeBytes, sensitive):
    """Transparent wrapper class to mark bytes as sensitive"""

    def decode(self, *args: List[object], **kwargs: Mapping) -> "sensitive_str":
        """Wrapper method to ensure type conversions maintain sensitive context"""
        return sensitive_str(super().decode(*args, **kwargs))


# also mark AnsibleUnsafe so wrap_var doesn't strip out sensitive status
class sensitive_str(AnsibleUnsafeText, sensitive):
    """Transparent wrapper class to mark a str as sensitive"""

    def encode(self, *args: List[object], **kwargs: Mapping) -> "sensitive_bytes":
        """Wrapper method to ensure type conversions maintain sensitive context"""
        return sensitive_bytes(super().encode(*args, **kwargs))


class sensitive_dict(dict, sensitive):
    """Transparent wrapper class to mark a dict as sensitive"""


class sensitive_list(list, sensitive):
    """Transparent wrapper class to mark a list as sensitive"""


def to_yaml_text(
    val: object,
) -> Union[sensitive, ScalarString, FoldedScalarString, str]:
    if isinstance(val, (ScalarString, sensitive)):
        return val
    # convert or copy string (copy to deal with things like AnsibleUnsafeText)
    val = str(val)
    if "\n" in val:
        return FoldedScalarString(val)
    return val


_T = TypeVar("_T")


def assert_not_none(val: Optional[_T]) -> _T:
    if val is None:
        raise TypeError("Value can't be None")
    return val


@overload
def assert_form(src: Any, *, test: bool = True) -> Mapping: ...


@overload
def assert_form(src: Any, types: Type[_T], test: bool = True) -> _T: ...


# abstract classes can't be Type[_T], fallback to this hack
@overload
def assert_form(src: Any, types: Any, test: bool = True) -> MutableSequence: ...


def assert_form(src: Any, types=Mapping, test: bool = True):
    if not isinstance(src, types) or not test:
        raise TypeError(f"Wrong shape: {src} isn't a {types}")
    return src


_ClassRegistry = {}  # type: ignore
_shortNameRegistry = {}  # type: ignore


def register_short_names(shortNames: Union[Mapping, Iterable]) -> None:
    _shortNameRegistry.update(shortNames)


def register_class(
    className: str, factory: object, short_name: str = None, replace: bool = True
) -> None:
    if short_name:
        _shortNameRegistry[short_name] = className
    if not replace and className in _ClassRegistry:
        if _ClassRegistry[className] is not factory:
            raise UnfurlError(f"class already registered for {className}")
    _ClassRegistry[className] = factory


def load_module(path: str, full_name: str = None) -> ModuleType:
    if full_name is None:
        full_name = re.sub(r"\W", "_", path)  # generate a name from the path
    if full_name in sys.modules:
        return sys.modules[full_name]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        spec = importlib.util.spec_from_file_location(full_name, os.path.abspath(path))
        # XXX: spec might be None
        module = importlib.util.module_from_spec(spec)  # type: ignore
        spec.loader.exec_module(module)  # type: ignore
        sys.modules[full_name] = module
    return module


def load_class(klass: str, defaultModule: str = "__main__") -> object:
    prefix, sep, suffix = klass.rpartition(".")
    module = importlib.import_module(prefix or defaultModule)
    return getattr(module, suffix, None)


_shortNameRegistry = {}


def check_class_registry(kind: str) -> bool:
    return kind in _ClassRegistry or kind in _shortNameRegistry


def lookup_class(kind: str) -> object:
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


def to_enum(enum, value, default=None):  # type: ignore
    # from string: Status[name]; to string: status.name
    if isinstance(value, str):
        return enum[value]
    elif default is not None and not value:
        return default
    elif isinstance(value, int):
        return enum(value)
    else:
        return value


def to_dotenv(env: dict) -> str:
    # https://hexdocs.pm/dotenvy/dotenv-file-format.html
    keys = []
    for k, v in env.items():
        # k should be [a-zA-Z_]+[a-zA-Z0-9_]*
        value = str(v)
        if value.isalnum():
            # always single quote to prevent interpolation
            keys.append(f"{k}='{value}'")
        else:
            keys.append(f"{k}={repr(value)}")
    return "\n".join(keys)


def dump(
    obj: object,
    tp: IO[bytes],
    suffix: str = "",
    yaml: Optional[ModuleType] = None,
    encoding: Optional[str] = None,
) -> None:
    from .yamlloader import yaml as _yaml

    try:
        textEncoding = (
            "utf-8" if encoding in [None, "yaml", "vault", "json", "env"] else encoding
        )
        f = io.TextIOWrapper(tp, textEncoding)
        explicit = encoding is not None
        if encoding == "yaml" or (
            not explicit and (suffix.endswith(".yml") or suffix.endswith(".yaml"))
        ):
            (yaml or _yaml).dump(obj, f)
            return
        elif encoding == "json" or (not explicit and suffix.endswith(".json")):
            json.dump(obj, f, indent=2)  # str to bytes
            return

        if isinstance(obj, str):
            obj = codecs.encode(obj, encoding or "utf-8")
        if isinstance(obj, bytes):
            tp.write(obj)
            return

        if (suffix.endswith(".env") or encoding == "env") and isinstance(obj, dict):
            tp.write(codecs.encode(to_dotenv(obj), "utf-8"))  # type: ignore
            return

        # try to dump any other object as json
        if obj is not None:  # treat None as 0 byte file
            json.dump(obj, f, indent=2, sort_keys=True)
    finally:
        f.detach()


def _save_to_vault(
    path: str,
    obj: object,
    yaml: Optional[ModuleType] = None,
    encoding: Optional[str] = None,
    fd: Union[str, int] = None,
) -> bool:
    vaultExt = path.endswith(".vault")
    if vaultExt or encoding == "vault":
        assert yaml and yaml.representer.vault and yaml.representer.vault.secrets
        vaultlib = yaml.representer.vault
        vault = VaultEditor(vaultlib)
        f = io.BytesIO()
        vpath = path[: -len(".vault")] if vaultExt else path
        dump(obj, f, vpath, yaml, None if encoding == "vault" else encoding)
        # the first vaultid is the most specific to the current project so encrypt with that one
        vault_id, secret = vaultlib.secrets[0]
        b_vaulttext = vaultlib.encrypt(f.getvalue(), secret=secret, vault_id=vault_id)
        vault.write_data(b_vaulttext, fd or path)
        return True
    return False


def save_to_file(
    path: str,
    obj: object,
    yaml: Optional[ModuleType] = None,
    encoding: Optional[str] = None,
) -> None:
    dir = os.path.dirname(path)
    if dir and not os.path.isdir(dir):
        os.makedirs(dir)
    if not _save_to_vault(path, obj, yaml, encoding):
        with open(path, "wb") as f:
            dump(obj, f, path, yaml, encoding)


def save_to_tempfile(
    obj: object,
    suffix: str = "",
    delete: bool = True,
    dir: Optional[str] = None,
    yaml: Optional[ModuleType] = None,
    encoding: Optional[str] = None,
) -> tempfile._TemporaryFileWrapper:
    tp = tempfile.NamedTemporaryFile(
        "w+b",
        suffix=suffix or "",
        delete=False,
        dir=dir or os.environ.get("UNFURL_TMPDIR"),
    )
    if delete:
        atexit.register(lambda: os.path.exists(tp.name) and os.unlink(tp.name))  # type: ignore

    if not _save_to_vault(suffix or "", obj, yaml, encoding, tp.fileno()):
        try:
            dump(obj, tp, suffix or "", yaml, encoding)
        finally:
            tp.close()
    return tp


def make_temp_dir(delete: bool = True, prefix: str = "unfurl") -> str:
    tempDir = tempfile.mkdtemp(prefix, dir=os.environ.get("UNFURL_TMPDIR"))
    if delete:
        atexit.register(lambda: os.path.isdir(tempDir) and shutil.rmtree(tempDir))  # type: ignore
    return tempDir


def get_base_dir(path: str) -> str:
    if os.path.exists(path):
        isdir = os.path.isdir(path)
    else:
        isdir = not os.path.splitext(path)[1]
    if isdir:
        return path
    else:
        return os.path.normpath(os.path.dirname(path))


def clean_output(value: str) -> str:
    return re.sub(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]", "", unstyle(value))


@contextmanager
def change_cwd(
    new_path: Optional[str] = None, log: Optional[LogExtraLevels] = None
) -> Iterator:
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
def extend_with_default(validator_class: Draft7Validator) -> Draft7Validator:
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

    def set_defaults(
        validator: Draft7Validator,
        properties: Mapping,
        instance: Draft7Validator,
        schema: Mapping,
    ) -> Iterator:
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


def validate_schema(obj: Any, schema: Mapping, baseUri: Optional[str] = None) -> bool:
    return not find_schema_errors(obj, schema)


def find_schema_errors(
    obj: Any, schema: Mapping, baseUri: Optional[str] = None
) -> Optional[Tuple[str, List[object]]]:
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


class ChainMap(MutableMapping):
    """
    Combine multiple mappings for sequential lookup.
    """

    def __init__(self, *maps: MutableMapping) -> None:
        self._maps = maps

    def copy(self):
        # assume _map[0] implements copy(), the remaining dicts are read-only
        return ChainMap(self._maps[0].copy(), *self._maps[1:])  # type: ignore

    def split(self) -> Tuple[MutableMapping, "ChainMap"]:
        return self._maps[0], ChainMap(*self._maps[1:])

    def __getitem__(self, key: Union[SupportsIndex, slice]) -> object:
        for mapping in self._maps:
            try:
                return mapping[key]
            except KeyError:
                pass
        raise KeyError(key)

    def __contains__(self, key) -> bool:
        for mapping in self._maps:
            if key in mapping:
                return True
        return False

    def __setitem__(self, key, value: object) -> None:
        self._maps[0][key] = value

    def __delitem__(self, key) -> None:
        del self._maps[0][key]

    def __iter__(self) -> Iterator:
        return iter(frozenset(itertools.chain(*self._maps)))

    def __len__(self) -> int:
        return len(frozenset(itertools.chain(*self._maps)))

    def __nonzero__(self) -> bool:
        return all(self._maps)

    def __repr__(self) -> str:
        return "ChainMap(%r)" % (self._maps,)


class Generate:
    """
    Roughly equivalent to "yield from" but works in Python < 3.3

    Usage:

    >>>  gen = Generate(generator())
    >>>  while gen():
    >>>    gen.result = yield gen.next
    """

    def __init__(self, generator: Generator) -> None:
        self.generator = generator
        self.result = None
        self.next = None

    def __call__(self) -> bool:
        try:
            self.next = self.generator.send(self.result)
            return True
        except StopIteration:
            return False


def taketwo(seq: Iterable[_T]) -> Iterator[Tuple[_T, Optional[_T]]]:
    last: _T = cast(_T, None)
    for i, x in enumerate(seq):
        if (i + 1) % 2 == 0:
            yield last, x
        else:
            last = x


def unique_name(name: str, existing: Sequence) -> str:
    counter = 1
    basename = name
    while name in existing:
        # create Repository instance with a unique name
        name = basename + str(counter)
        counter += 1
    return name


# python < 3.9 doesn't  support Path.is_relative_to
def is_relative_to(p, *other) -> bool:
    """Return True if the path is relative to another path or False. """
    try:
        Path(p).relative_to(*other)
        return True
    except ValueError:
        return False

def should_include_path(include_paths: List[str], exclude_paths: List[str], target_path: str) -> bool:
    """
    Determine if a given path should be included or excluded based on exclusion and inclusion lists.
    If no match is found, the path is included by default (use "/" in exclude_paths to exclude by default).
    If exclude takes precedence over include if the same path appears in both lists. Paths are compared as absolute paths.

    Args:
        include_paths (list[str]): List of paths to include.
        exclude_paths (list[str]): List of paths to exclude.
        target_path (str): The path to check.

    Returns:
        bool: True if the path should be included, False if it should be excluded.
    """
    # Merge paths and sort by length (longest to shortest)
    all_paths = sorted(exclude_paths + include_paths, key=lambda p: len(os.path.abspath(p)), reverse=True)

    # Check the target path against the sorted list
    target_path = os.path.abspath(target_path)
    for path in all_paths:
        if is_relative_to(target_path, os.path.abspath(path)):
            # If the path is in the exclude list, return False
            if path in exclude_paths:
                return False
            # If the path is in the include list, return True
            if path in include_paths:
                return True

    # Default to include if no match is found
    return True

def substitute_env(
    contents: str, env: Optional[Mapping] = None, preserve_missing: bool = False
):
    r"""
    Replace ${NAME} or ${NAME:default value} with the value of the environment variable $NAME
    Use \${NAME} to ignore
    """
    if env is None:
        env = os.environ

    def replace(m: re.Match) -> str:
        if m.group(1):  # \ found
            return m.group(0)[len(m.group(1)) - 1 or 1 :]
        for name in m.group(2).split("|"):
            if name in env:
                value = env[name]
                if callable(value):
                    return cast(str, value())
                return value
        # can't resolve, use default
        if preserve_missing:
            return m.group(0)
        else:  # return the default value or an empty ""
            return m.group(3)[1:] if m.group(3) else ""

    return re.sub(r"(\\+)?\$\{([\w|]+)(\:.+?)?\}", replace, contents)


def env_var_value(val, sub=None):
    if val is None:
        return None
    if isinstance(val, bool):
        return val and "true" or ""
    sensitive = is_sensitive(val)
    val = str(val)
    if sub is not None:
        val = substitute_env(val, sub)
    if sensitive:
        return wrap_sensitive_value(val)
    else:
        return val


def filter_env(
    rules: Mapping,
    env: Optional[Union[Dict, "os._Environ[str]"]] = None,
    addOnly: bool = False,
    sub: Optional[MutableMapping] = None,
) -> Dict[str, str]:
    r"""
    Applies the given list of rules to a dictionary of environment variables and returns a new dictionary.

    Args:
        rules (dict): A dictionary of rules for adding, removing and filtering environment variables.
        env (dict, optional): The environment to apply the give rules to. If ``env`` is None it will be set to ``os.environ``.
          Defaults to None.
        addOnly (bool, optional): If addOnly is False (the default) all variables in ``env`` will be included
          in the returned dict, otherwise only variables added by ``rules`` will be included

    Rules applied in the order they are declared in the ``rules`` dictionary. The following examples show the different patterns for the rules:

      :foo\: bar: Add ``foo=bar``
      :+foo:  Copy ``foo`` from the current environment
      :+foo\: bar: Copy ``foo``, or add ``foo=bar`` if it is not present
      :+foo*: Copy all name from the current environment that match ``foo*``
      :+!foo*: Copy all name from the current environment except those matching ``foo*``
      :-!foo:  Remove all names except for ``foo``
      :^foo\: /bar/bin: Treat ``foo`` like ``PATH`` and prepend ``/bar/bin:$foo``
    """
    if env is None:
        env = os.environ

    if addOnly:
        start: Dict[str, str] = {}
    else:
        start = env.copy()
    if sub is not None:
        # include start so substitutions can refer to the new entries being defined
        sub = ChainMap(start, sub)

    for name, val in rules.items():
        if name[:1] in "+-^":
            # add or remove from env
            remove = name[0] == "-"
            prepend = name[0] == "^"
            name = name[1:]
            neg = name[:1] == "!"
            if neg:
                name = name[1:]
            if remove:
                source = start  # if remove, look in the current set, not original
            else:
                source = env  # type: ignore
            match = {k for k in source if fnmatch.fnmatchcase(k, name) ^ neg}
            if remove:
                [
                    start.pop(k, None)
                    for k in match
                    # required_envars need to an explicit rule to be removed
                    if k not in required_envvars or name in required_envvars
                ]
            else:
                if match:
                    if prepend and val:
                        # treat as PATH-like and prepend val
                        update = {k: (val + os.pathsep + source[k]) for k in match}
                    else:
                        update = {k: source[k] for k in match}
                    start.update(update)
                elif val is not None:  # name isn't in existing, use default if set
                    start[name] = env_var_value(val, sub)
        elif val is not None:  # don't set if val is None
            start[name] = env_var_value(val, sub)
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
    "UNFURL_MOCK_DEPLOY",
    "UNFURL_LOGFILE",
    "UNFURL_SKIP_VAULT_DECRYPT",
    "UNFURL_SKIP_UPSTREAM_CHECK",
    "UNFURL_SKIP_SAVE",
    "UNFURL_SEARCH_ROOT",
    "UNFURL_CLOUD_SERVER",
    "UNFURL_VALIDATION_MODE",
    "UNFURL_PACKAGE_RULES",
    "UNFURL_LOG_FORMAT",
    "UNFURL_RAISE_LOGGING_EXCEPTIONS",
    "PY_COLORS",
    "UNFURL_OVERWRITE_POLICY",
    "UNFURL_EXPORT_LOCALNAMES",
    "UNFURL_GLOBAL_NAMESPACE_PACKAGES",
]
# hack for sphinx ext documentedlist
_sphinx_envvars = [(i,) for i in required_envvars]
