# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Internal classes supporting the runtime.
"""
import base64
import collections
from collections.abc import MutableSequence, Mapping
import hashlib
import math
import os
from random import choice
import sys
import os.path
import re
import ast
import time
from typing import (
    TYPE_CHECKING,
    Iterator,
    List,
    MutableMapping,
    Tuple,
    Union,
    cast,
    Dict,
    Optional,
    Any,
    NewType,
)
from typing_extensions import Protocol, NoReturn
from enum import Enum
from urllib.parse import quote, quote_plus, urlsplit

if TYPE_CHECKING:
    from .manifest import Manifest
    from .runtime import EntityInstance, InstanceKey, HasInstancesInstance, TopologyInstance
    from .configurator import Dependency

from .eval import RefContext, set_eval_func, Ref, map_value, SafeRefContext
from .result import (
    Results,
    ResultsList,
    ResultsMap,
    Result,
    ExternalValue,
    serialize_value,
    is_sensitive_schema,
)
from .util import (
    ChainMap,
    find_schema_errors,
    UnfurlError,
    UnfurlValidationError,
    UnfurlTaskError,
    assert_form,
    wrap_sensitive_value,
    is_sensitive,
    load_module,
    load_class,
    sensitive,
    filter_env,
    env_var_value,
    get_random_password,
)
from .merge import intersect_dict, merge_dicts
from unfurl.projectpaths import get_path
import ansible.template
from ansible.parsing.dataloader import DataLoader
from ansible.utils import unsafe_proxy
from ansible.utils.unsafe_proxy import wrap_var, AnsibleUnsafeText, AnsibleUnsafeBytes
from jinja2.runtime import DebugUndefined
from toscaparser.elements.portspectype import PortSpec

import logging

logger = logging.getLogger("unfurl")


class Status(int, Enum):
    unknown = 0
    ok = 1
    degraded = 2
    error = 3
    pending = 4
    absent = 5

    @property
    def color(self):
        return {
            Status.unknown: "white",
            Status.ok: "green",
            Status.degraded: "yellow",
            Status.error: "red",
            Status.pending: "white",
            Status.absent: "yellow",
        }[self]


# see "3.4.1 Node States" p74
class NodeState(int, Enum):
    initial = 1
    creating = 2
    created = 3
    configuring = 4
    configured = 5
    starting = 6
    started = 7
    stopping = 8
    stopped = 9
    deleting = 10
    deleted = 11
    error = 12


class Priority(int, Enum):
    ignore = 0
    optional = 1
    required = 2
    critical = 3


class Reason:
    add = "add"
    reconfigure = "reconfigure"
    force = "force"
    upgrade = "upgrade"
    update = "update"
    missing = "missing"
    error = "error"
    degraded = "degraded"
    prune = "prune"
    run = "run"
    check = "check"


class Defaults:
    shouldRun = Priority.required
    workflow = "deploy"


def eval_python(arg, ctx):
    """
    eval:
      python: path/to/src.py#func

    or

    eval:
      python: mod.func

    Where ``func`` is python function that receives a `RefContext` argument.
    If path is a relative, it will be treated as relative to the current source file.
    """
    arg = map_value(arg, ctx)
    if "#" in arg:
        path, sep, fragment = arg.partition("#")
        # if path is relative, treat as relative to current src location
        path = get_path(ctx, path, "src", False)
        mod = load_module(path)
        funcName = mod.__name__ + "." + fragment
    else:
        funcName = arg
    func = load_class(funcName)
    if not func:
        raise UnfurlError(f"Could not find python function {funcName}")
    if not callable(func):
        raise UnfurlError(
            f"Invalid python function {funcName}: {type(funcName)} is not callable."
        )

    kw = ctx.kw
    if "args" in kw:
        args = map_value(kw["args"], ctx)
        return func(ctx, args)
    else:
        return func(ctx)


set_eval_func("python", eval_python)

# XXX need an api check if an object was marked sensitive
# _secrets = weakref.WeakValueDictionary()
# def addSecret(secret):
#   _secrets[id(secret.get())] = secret
# def isSecret(obj):
#    return id(obj) in _secrets

set_eval_func(
    "sensitive", lambda arg, ctx: wrap_sensitive_value(map_value(arg, ctx)), safe=True
)


set_eval_func(
    "portspec", lambda arg, ctx: PortSpec.make(map_value(arg, ctx)), safe=True
)


class Templar(ansible.template.Templar):
    def template(self, variable, **kw):
        if isinstance(variable, Results):
            # template() will eagerly evaluate template strings in lists and dicts
            # defeating the lazy evaluation ResultsMap and ResultsList is intending to provide
            return variable
        return super().template(variable, **kw)

    @staticmethod
    def find_overrides(data, overrides=None):
        overrides = overrides or {}
        JINJA2_OVERRIDE = "#jinja2:"
        # Get jinja env overrides from template
        if hasattr(data, "startswith") and data.startswith(JINJA2_OVERRIDE):
            eol = data.find("\n")
            line = data[len(JINJA2_OVERRIDE) : eol]
            data = data[eol + 1 :]
            for pair in line.split(","):
                (key, val) = pair.split(":")
                key = key.strip()
                overrides[key] = ast.literal_eval(val.strip())
        return overrides

    def _apply_templar_overrides(self, overrides):
        # we need to update the environments so the same overrides are
        # applied to each template evaluate through j2_concat (e.g. inside variables)
        from ansible import constants as C

        original = {}
        for key, value in overrides.items():
            original[key] = getattr(self.environment, key)
            setattr(self.environment, key, value)

        self.environment.__dict__.update(overrides)
        # copied from Templar.__init__:
        self.SINGLE_VAR = re.compile(
            r"^%s\s*(\w*)\s*%s$"
            % (
                self.environment.variable_start_string,
                self.environment.variable_end_string,
            )
        )

        self._clean_regex = re.compile(
            r"(?:%s|%s|%s|%s)"
            % (
                self.environment.variable_start_string,
                self.environment.block_start_string,
                self.environment.block_end_string,
                self.environment.variable_end_string,
            )
        )
        self._no_type_regex = re.compile(
            r".*?\|\s*(?:%s)(?:\([^\|]*\))?\s*\)?\s*(?:%s)"
            % ("|".join(C.STRING_TYPE_FILTERS), self.environment.variable_end_string)
        )
        return original


def _get_template_test_reg_ex():
    environment = Templar(DataLoader()).environment
    return re.compile(
        r"(?:%s|%s|%s|%s)"
        % (
            environment.variable_start_string,
            environment.block_start_string,
            environment.block_end_string,
            environment.variable_end_string,
        )
    )


_clean_regex = _get_template_test_reg_ex()


def is_template(val, ctx=None):
    if isinstance(val, (AnsibleUnsafeText, AnsibleUnsafeBytes)):
        # already evaluated in a template, don't evaluate again
        return False
    return isinstance(val, str) and not not _clean_regex.search(val)


class _VarTrackerDict(dict):
    ctx: Optional[RefContext] = None

    def __getitem__(self, key):
        try:
            val = super().__getitem__(key)
        except KeyError:
            logger.debug('Missing variable "%s" in template', key)
            raise

        try:
            assert self.ctx
            return self.ctx.resolve_reference(key)
        except KeyError:
            return val


def _wrap_dict(v):
    if isinstance(v, Results):
        # wrap_var() fails with Results types, this is equivalent:
        v.applyTemplates = False
        return v
    return dict((wrap_var(k), wrap_var(item)) for k, item in v.items())


unsafe_proxy._wrap_dict = _wrap_dict


def _wrap_sequence(v):
    if isinstance(v, Results):
        # wrap_var() fails with Results types, this is equivalent:
        v.applyTemplates = False
        return v
    v_type = type(v)
    return v_type(wrap_var(item) for item in v)


unsafe_proxy._wrap_sequence = _wrap_sequence

def _sandboxed_template(value: str, ctx: SafeRefContext, vars, _UnfurlUndefined):
    from jinja2.sandbox import SandboxedEnvironment
    from jinja2.nativetypes import NativeCodeGenerator, native_concat
    from .filter_plugins import ref

    SandboxedEnvironment.code_generator_class = NativeCodeGenerator
    SandboxedEnvironment.concat = staticmethod(native_concat)  # type: ignore
    env = SandboxedEnvironment()

    if not ctx.strict:
        env.undefined = _UnfurlUndefined
    ctx.templar = None
    env.filters.update(ref.SAFE_FILTERS)
    return env.from_string(value).render(vars)


def apply_template(value: str, ctx: RefContext, overrides=None) -> Any:
    if not isinstance(value, str):
        msg = f"Error rendering template: source must be a string, not {type(value)}"
        if ctx.strict:
            raise UnfurlError(msg)
        else:
            return f"<<{msg}>>"
    value = value.strip()
    if ctx.task:
        logger = ctx.task.logger
    else:
        logger = logging.getLogger("unfurl")  # type: ignore

    # local class to bind with logger and ctx
    class _UnfurlUndefined(DebugUndefined):
        __slots__ = ()

        def _fail_with_undefined_error(  # type: ignore
            self, *args: Any, **kwargs: Any
        ) -> "NoReturn":
            try:
                super()._fail_with_undefined_error(*args, **kwargs)
            except self._undefined_exception as e:
                msg = "Template: %s" % self._undefined_message
                logger.warning(msg)
                if ctx.task:  # already logged, so don't log
                    UnfurlTaskError(ctx.task, msg, False)
                raise e

        # copied from Undefined, we need to reset _fail_with_undefined_error
        __add__ = __radd__ = __sub__ = __rsub__ = _fail_with_undefined_error
        __mul__ = __rmul__ = __div__ = __rdiv__ = _fail_with_undefined_error
        __truediv__ = __rtruediv__ = _fail_with_undefined_error
        __floordiv__ = __rfloordiv__ = _fail_with_undefined_error
        __mod__ = __rmod__ = _fail_with_undefined_error
        __pos__ = __neg__ = _fail_with_undefined_error
        __call__ = _fail_with_undefined_error
        __lt__ = __le__ = __gt__ = __ge__ = _fail_with_undefined_error
        __int__ = __float__ = __complex__ = _fail_with_undefined_error
        __pow__ = __rpow__ = _fail_with_undefined_error

        def __getattr__(self, name):
            if name == "__UNSAFE__":
                # see AnsibleUndefined
                # self should never be assumed to be unsafe
                # This prevents ``hasattr(val, '__UNSAFE__')`` from evaluating to ``True``
                raise AttributeError(name)
            # Return original Undefined object to preserve the first failure context
            return self  # see ChainableUndefined

        __getitem__ = __getattr__  # type: ignore

        def _log_message(self) -> None:
            msg = "Template: %s" % self._undefined_message
            # XXX? if self._undefined_obj is a Results then add its ctx._lastResource to the msg
            logger.warning(msg)
            if ctx.task:  # already logged, so don't log
                UnfurlTaskError(ctx.task, msg, False)

        # see LoggingUndefined:
        def __str__(self) -> str:
            self._log_message()
            return super().__str__()  # type: ignore

        def __iter__(self):
            self._log_message()
            return super().__iter__()  # type: ignore

        def __bool__(self) -> bool:
            self._log_message()
            return super().__bool__()  # type: ignore

        # see ChainableUndefined
        def __html__(self) -> str:
            return str(self)

    # implementation notes:
    #   see https://github.com/ansible/ansible/test/units/template/test_templar.py
    #   dataLoader is only used by _lookup and to set _basedir (else ./)
    if not ctx.templar or (ctx.base_dir and ctx.templar._basedir != ctx.base_dir):
        # we need to create a new templar
        loader = DataLoader()
        if ctx.base_dir:
            loader.set_basedir(ctx.base_dir)
        if ctx.templar and ctx.templar._loader._vault.secrets:
            loader.set_vault_secrets(ctx.templar._loader._vault.secrets)
        templar = Templar(loader)
        ctx.templar = templar
    else:
        templar = ctx.templar

    overrides = Templar.find_overrides(value, overrides)
    if overrides:
        # returns the original values
        overrides = templar._apply_templar_overrides(overrides)

    templar.environment.trim_blocks = False
    # templar.environment.lstrip_blocks = False
    fail_on_undefined = ctx.strict
    if not fail_on_undefined:  # note: strict is on by default
        templar.environment.undefined = _UnfurlUndefined

    vars = _VarTrackerDict(
        __unfurl=ctx, __python_executable=sys.executable, __now=time.time()
    )
    if hasattr(ctx.currentResource, "attributes"):
        vars["SELF"] = ctx.currentResource.attributes  # type: ignore
    if os.getenv("UNFURL_TEST_DEBUG_EX"):
        logger.debug("template vars for %s: %s", value[:300], list(ctx.vars))
    vars.update(ctx.vars)
    vars.ctx = ctx

    # replaces current vars
    # don't use setter to avoid isinstance(dict) check
    templar._available_variables = vars

    oldvalue = value
    index = ctx.referenced.start()
    # set referenced to track references (set by Ref.resolve)
    # need a way to turn on and off
    try:
        # strip whitespace so jinija native types resolve even with extra whitespace
        # disable caching so we don't need to worry about the value of a cached var changing
        # use do_template because we already know it's a template
        try:
            if isinstance(ctx, SafeRefContext):
                value = _sandboxed_template(value, ctx, vars, _UnfurlUndefined)
            else:
                value = templar.template(value, fail_on_undefined=fail_on_undefined)
        except Exception as e:
            msg = str(e)
            # XXX have _UnfurlUndefined throw an exception with the missing obj and key
            match = re.search(r"has no attribute '(\w+)'", msg)
            if match:
                msg = f'missing attribute or key: "{match.group(1)}"'
            else:
                match = re.search(r"'(\w+)' is undefined", msg)
                if match:
                    msg = f'missing variable: "{match.group(1)}"'
            value = f"<<Error rendering template: {msg}>>"
            if ctx.strict:
                logger.debug(value, exc_info=True)
                raise UnfurlError(value)
            else:
                logger.warning(value[2:100] + "... see debug log for full report")
                logger.debug(value, exc_info=True)
                if ctx.task:
                    UnfurlTaskError(ctx.task, msg)
        else:
            if value != oldvalue:
                ctx.trace("successfully processed template:", value)
                external_result = None
                for result in ctx.referenced.getReferencedResults(index):
                    if is_sensitive(result):
                        # note: even if the template rendered a list or dict
                        # we still need to wrap the entire result as sensitive because we
                        # don't know how the referenced senstive results were transformed by the template
                        ctx.trace("setting template result as sensitive")
                        # mark the template result as sensitive
                        return wrap_sensitive_value(value)
                    if result.external:
                        external_result = result

                if (
                    external_result
                    and ctx.wantList == "result"
                    and value == external_result.external.get()
                ):
                    # return the external value instead
                    return external_result

                # wrap result as AnsibleUnsafe so it isn't evaluated again
                return wrap_var(value)
            else:
                ctx.trace("no modification after processing template:", value)
    finally:
        ctx.referenced.stop()
        if overrides:
            # restore original values
            templar._apply_templar_overrides(overrides)
    return value


def _template_func(args, ctx):
    args = map_value(args, ctx, False)  # don't apply templates yet
    if isinstance(args, Mapping) and "path" in args:
        path = args["path"]
        if is_template(path, ctx):  # path could be a template expression
            path = apply_template(path, ctx)
        with open(path) as f:
            value = f.read()
    else:
        value = cast(str, args)
    return apply_template(value, ctx, ctx.kw.get("overrides"))


set_eval_func("template", _template_func, safe=True)


def run_lookup(name, templar, *args, **kw):
    from ansible.plugins.loader import lookup_loader

    # https://docs.ansible.com/ansible/latest/plugins/lookup.html
    # "{{ lookup('url', 'https://toshio.fedorapeople.org/one.txt', validate_certs=True) }}"
    #       would end up calling the lookup plugin named url's run method like this::
    #           run(['https://toshio.fedorapeople.org/one.txt'], variables=available_variables, validate_certs=True)
    instance = lookup_loader.get(name, loader=templar._loader, templar=templar)
    # ansible_search_path = []
    result = instance.run(args, variables=templar._available_variables, **kw)
    # XXX check for wantList
    if not result:
        return None
    if len(result) == 1:
        return result[0]
    else:
        return result


def lookup_func(arg, ctx):
    """
    Runs an ansible lookup plugin. Usage:

    .. code-block:: YAML

      lookup:
          lookupFunctionName: 'arg' or ['arg1', 'arg2']
          kw1: value
          kw2: value
    """
    arg = map_value(arg, ctx)
    assert_form(arg, test=arg)  # a map with at least one element
    name = None
    args = None
    kwargs: Dict[str, Any] = {}
    for key, value in arg.items():
        if not name:
            name = key
            args = value
        else:
            kwargs[key] = value

    if not isinstance(args, MutableSequence):
        args = [args]

    return run_lookup(name, ctx.templar, *args, **kwargs)


set_eval_func("lookup", lookup_func)


def get_input(arg, ctx):
    if isinstance(arg, list):
        name = arg[0]
        default = arg[1]
        has_default = True
    else:
        name = arg
        has_default = False

    try:
        return ctx.currentResource.root.attributes["inputs"][name]
    except KeyError:
        if has_default:
            return map_value(default, ctx)
        raise UnfurlError(f"undefined input '{arg}'")


set_eval_func("get_input", get_input, True)


def concat(args, ctx):
    result = map_value(args, ctx)
    if not isinstance(result, MutableSequence):
        return result
    sep = ctx.kw.get("sep", "")
    return sep.join([str(a) for a in result])


set_eval_func("concat", concat, True, True)


def token(args, ctx):
    args = map_value(args, ctx)
    return args[0].split(args[1])[args[2]]


set_eval_func("token", token, True, True)


# XXX this doesn't work with node_filters, need an instance to get a specific result
def get_tosca_property(args, ctx):
    from toscaparser.functions import get_function

    tosca_tpl = ctx.currentResource.root.template.toscaEntityTemplate
    node_template = ctx.currentResource.template.toscaEntityTemplate
    return get_function(tosca_tpl, node_template, {"get_property": args}).result()


set_eval_func("get_property", get_tosca_property, True)


def has_env(arg, ctx):
    """
    {has_env: foo}
    """
    return arg in ctx.environ


set_eval_func("has_env", has_env, True)


def get_env(args, ctx: RefContext) -> Union[str, None, Dict[str, str]]:
    """
    Return the value of the given environment variable name.
    If NAME is not present in the environment, return the given default value if supplied or return None.

    e.g. {get_env: NAME} or {get_env: [NAME, default]}

    If the value of its argument is empty (e.g. [] or null), return the entire dictionary.
    """
    env = ctx.environ
    if not args:
        return env

    if isinstance(args, list):
        name = args[0]
        default: Optional[str] = args[1] if len(args) > 1 else None
    else:
        name = args
        default = None

    if name in env:
        return env[name]
    else:
        default = cast(Optional[str], map_value(default, ctx))
        return default


set_eval_func("get_env", get_env, True)


def set_context_vars(vars, resource: "EntityInstance"):
    root = cast("TopologyInstance", resource.root)
    ROOT: Dict[str, Any] = {}
    vars.update(dict(NODES=TopologyMap(root), ROOT=ROOT, TOPOLOGY=ROOT))
    if "inputs" in root._attributes:
        ROOT.update(
            dict(
                inputs=root._attributes["inputs"],
                outputs=root._attributes["outputs"],
            )
        )
    app_template = root.template.topology.substitution_node  # type: ignore
    if app_template:
        app = root.find_instance(app_template.name)
        if app:
            ROOT["app"] = app.attributes
        for name, req in app_template.requirements.items():
            if req.relationship and req.relationship.target:
                target = root.get_root_instance(
                    req.relationship.target.toscaEntityTemplate  # type: ignore
                ).find_instance(req.relationship.target.name)
                if target:
                    ROOT[name] = target.attributes

    return vars


class _EnvMapper(dict):
    """Resolve environment variable name to instance properties via the root template's requirements.
    Pattern should match _generate_env_names in to_json.py and set_context_vars above.
    """

    ctx: Optional[RefContext] = None

    def copy(self):
        return _EnvMapper(self)

    def __missing__(self, key):
        objname, sep, prop = key.partition("_")
        assert self.ctx
        root = self.ctx.currentResource.root
        app = root.template.spec.substitution_node
        if app and objname and prop:
            obj = None
            if objname == "APP":
                obj = app
            else:
                for name, req in app.requirements.items():
                    if name.upper() == objname:
                        if req.relationship and req.relationship.target:
                            obj = req.relationship.target
            if obj:
                instance = root.find_instance(obj.name)
                if instance:
                    for key in instance.attributes:
                        if key.upper() == prop:
                            return env_var_value(instance.attributes[key], self)
        raise KeyError(key)


def to_env(args, ctx: RefContext):
    env = ctx.environ
    sub = _EnvMapper(env or {})
    sub.ctx = ctx

    rules = map_value(args or {}, ctx)
    assert isinstance(rules, Mapping)
    result = filter_env(rules, env, True, sub)
    if ctx.kw.get("update_os_environ"):
        log = ctx.task and ctx.task.logger or logger
        log.debug("to_env is updating os.environ with %s using rules %s", result, rules)
        # update all the copies of environ
        envs = [ctx.environ, ctx.currentResource.root._environ, os.environ]
        for env in envs:
            env.update(result)
            for key, value in rules.items():
                if value is None and key in env:
                    del env[key]
    return result


set_eval_func("to_env", to_env)


def _digest(arg: str, case: str, digest: Optional[str] = None) -> str:
    m = hashlib.sha1()  # use same digest function as git
    m.update(arg.encode("utf-8"))
    if digest:
        m.update(digest.encode("utf-8"))
    if case == "any":
        digest = (
            base64.urlsafe_b64encode(m.digest())
            .decode()
            .strip("=")
            .replace("_", "")
            .replace("-", "")
        )
    else:
        digest = base64.b32encode(m.digest()).decode().strip("=")
        if case == "lower":
            return digest.lower()
        elif case == "upper":
            return digest.upper()
    return digest


def _mid_truncate(label: str, replace: str, trunc: int) -> str:
    if len(label) > trunc:
        replace_len = len(replace)
        mid = (trunc - replace_len) / 2
        if mid <= 4:
            return label[:trunc]
        # trunc is odd, take one more from the beginning
        return label[: math.ceil(mid)] + replace + label[-math.floor(mid) :]
    return label


_label_defaults = dict(
    allowed=r"\w",
    max=63,
    case="any",
    replace="",
    start="a-zA-Z",
    start_prepend="x",
    end=r"\w",
    sep="",
    digest=None,
    digestlen=-1,
)


def to_label(arg, **kw):
    r"""Convert a string to a label with the given constraints.
        If a dictionary, all keys and string values are converted.
        If list, to_label is applied to each item and concatenated using ``sep``

    Args:
        arg (str or dict or list): Convert to label
        allowed (str, optional): Allowed characters. Regex character ranges and character classes.
                               Defaults to "\w"  (equivalent to [a-zA-Z0-9_])
        replace (str, optional): String Invalidate. Defaults to "" (remove the characters).
        start (str, optional): Allowed characters for the first character. Regex character ranges and character classes.
                               Defaults to "a-zA-Z"
        start_prepend (str, optional): If the start character is invalid, prepend with this string (Default: "x")
        end (str, optional): Allowed trailing characters. Regex character ranges and character classes.
                            Invalid characters are stripped. Defaults to "\w"  (equivalent to [a-zA-Z0-9_])
        max (int, optional): max length of label. Defaults to 63 (the maximum for a DNS name).
        case (str, optional): "upper", "lower" or "any" (no conversion). Defaults to "any".
        sep (str, optional): Separator to use when concatenating a list. Defaults to ""
        digestlen (int, optional): If a label is truncated, the length of the digest to include in the label. 0 to disable.
                                Default: 3 or 2 if max < 32
    """
    case: str = kw.get("case", _label_defaults["case"])
    sep: str = kw.get("sep", _label_defaults["sep"])
    digest: Optional[str] = kw.pop("digest", _label_defaults["digest"])
    replace: str = kw.get("replace", _label_defaults["replace"])
    elide_chars = replace

    if isinstance(arg, Mapping):
        # convert keys and string values of the mapping, filtering out nulls
        # only apply digest to values
        return {
            to_label(n, **kw): to_label(v, digest=digest, **kw)
            for n, v in arg.items()
            if v is not None
        }

    trunc = int(kw.pop("max", _label_defaults["max"]))
    assert trunc >= 1, trunc
    checksum: int = kw.pop("digestlen", _label_defaults["digestlen"])

    if checksum == -1:
        checksum = 2 if trunc < 32 else 3
    if checksum:
        maxchecksum = min(trunc, checksum)
    else:
        maxchecksum = 0
    if isinstance(arg, list):
        if not arg:
            return ""
        # concatentate list
        # adjust max for length of separators
        trunc_chars = trunc - min(len(sep) * (len(arg) - 1), trunc - 1)
        seg_max = max(trunc_chars // len(arg), 1)
        segments = [str(n) for n in arg]
        labels = [to_label(n, digestlen=0, max=9999, **kw) for n in segments]
        length = sum(map(len, labels))
        if length > trunc_chars or digest is not None:
            # needs truncation and/or digest
            # redistribute space from short segments
            leftover = sum(map(lambda n: max(seg_max - len(n), 0), labels))
            seg_max += leftover // len(labels)
            labels = [_mid_truncate(seg, elide_chars, seg_max) for seg in labels]
            if checksum:
                # one of the labels was truncated, add a digest
                trunc -= len(sep) + maxchecksum
                hash = _digest("".join(segments), case, digest)[:maxchecksum]
                if trunc <= 0:
                    return hash
                return sep.join(labels)[:trunc] + sep + hash
        return sep.join(labels)[:trunc]
    elif isinstance(arg, str):
        start: str = kw.get("start", _label_defaults["start"])
        start_prepend: str = kw.get("start_prepend", _label_defaults["start_prepend"])
        allowed: str = kw.get("allowed", _label_defaults["allowed"])
        end: str = kw.get("end", _label_defaults["end"])

        if arg and re.match(rf"[^{start}]", arg[0]):
            val = start_prepend + arg
        else:
            val = arg
        if len(val) > trunc:
            # heuristic: if we need to truncate, just remove the invalid characters
            replace = ""
        val = re.sub(rf"[^{allowed}]", replace, val)
        while val and re.match(rf"[^{end}]", val[-1]):
            val = val[:-1]
        if case == "lower":
            val = val.lower()
        elif case == "upper":
            val = val.upper()

        if maxchecksum and (digest is not None or len(val) > trunc):
            trunc -= min(trunc, maxchecksum)
            return (
                _mid_truncate(val, elide_chars, trunc)
                + _digest(arg, case, digest)[:maxchecksum]
            )
        else:
            return _mid_truncate(val, elide_chars, trunc)
    else:
        return arg


set_eval_func(
    "to_label",
    lambda arg, ctx: to_label(map_value(arg, ctx), **map_value(ctx.kw, ctx)),
    safe=True,
)


def to_dns_label(
    arg, allowed=r"\w-", start=r"\w", replace="--", case="lower", max=63, **kw
):
    """
    Convert the given argument (see `to_label` for full description) to a DNS label (a label is the name separated by "." in a domain name).
    The maximum length of each label is 63 characters and can include
    alphanumeric characters and hyphens but a domain name must not commence or end with a hyphen.

    Invalid characters are replaced with "--".
    """
    return to_label(
        arg, allowed=allowed, start=start, replace=replace, case=case, max=max, **kw
    )


set_eval_func(
    "to_dns_label",
    lambda arg, ctx: to_dns_label(map_value(arg, ctx), **map_value(ctx.kw, ctx)),
    safe=True,
)


def to_kubernetes_label(arg, allowed=r"\w_.-", replace="__", **kw):
    """
    See https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#syntax-and-character-set

    Invalid characters are replaced with "__".
    """
    return to_label(arg, allowed=allowed, replace=replace, **kw)


set_eval_func(
    "to_kubernetes_label",
    lambda arg, ctx: to_kubernetes_label(map_value(arg, ctx), **map_value(ctx.kw, ctx)),
    safe=True,
)


def to_googlecloud_label(
    arg, allowed=r"\w_-", case="lower", replace="__", max=63, **kw
):
    """
    See https://cloud.google.com/resource-manager/docs/creating-managing-labels#requirements

    Invalid characters are replaced with "__".
    """
    return to_label(arg, allowed=allowed, case=case, replace=replace, max=max, **kw)


set_eval_func(
    "to_googlecloud_label",
    lambda arg, ctx: to_googlecloud_label(
        map_value(arg, ctx), **map_value(ctx.kw, ctx)
    ),
    safe=True,
)


def get_ensemble_metadata(arg, ctx):
    if not ctx.task:
        return {}
    ensemble = ctx.task._manifest
    metadata = dict(
        deployment=ensemble.deployment,
        job=ctx.task.job.changeId,
    )
    if ensemble.repo:
        metadata["unfurlproject"] = ensemble.repo.project_path()
        metadata["revision"] = ensemble.repo.revision[:8]
    environment = ensemble.localEnv and ensemble.localEnv.manifest_context_name
    if environment:
        metadata["environment"] = environment
    if arg:
        key = map_value(arg, ctx)
        if key == "project_namespace_subdomain" and ensemble.repo:
            return ".".join(
                reversed(os.path.dirname(ensemble.repo.project_path()).split("/"))
            )
        return metadata.get(key, "")
    else:
        return metadata


set_eval_func("get_ensemble_metadata", get_ensemble_metadata)


_toscaKeywordsToExpr = {
    "SELF": ".",
    "SOURCE": ".source",
    "TARGET": ".target",
    "ORCHESTRATOR": "::localhost",
    "HOST": ".parents",
    "OPERATION_HOST": "$OPERATION_HOST",
}


def get_attribute(args, ctx: RefContext):
    args = map_value(args, ctx)
    entity_name = args.pop(0)
    candidate_name = args.pop(0)
    ctx = ctx.copy(ctx._lastResource)

    start = _toscaKeywordsToExpr.get(entity_name, "::" + entity_name)
    if args:
        attribute_name = args.pop(0)
        # need to include candidate_name as a test in addition to selecting it
        # so that the HOST search looks for that and not just ".names" (which all entities have)
        query = (
            f"{start}::.names[{candidate_name}]?::{candidate_name}::{attribute_name}?"
        )
        if args:  # nested attribute or list index lookup
            query += "::" + "::".join(args)
    else:  # simple attribute lookup
        query = start + "::" + candidate_name
    return ctx.query(query)


set_eval_func("get_attribute", get_attribute, True, True)


def get_nodes_of_type(type_name: str, ctx: RefContext):
    return [
        r
        for r in ctx.currentResource.root.get_self_and_descendants()
        if r.template.is_compatible_type(type_name)
        and r.name not in ["inputs", "outputs"]
    ]


set_eval_func("get_nodes_of_type", get_nodes_of_type, True, True)


set_eval_func("_generate", lambda arg, ctx: get_random_password(10, ""), True, True)


def _urljoin(scheme, host, port=None, path=None, query=None, frag=None):
    """
    Evaluate a list of url components to a relative or absolute URL,
    where the list is ``[scheme, host, port, path, query, fragment]``.

    The list must have at least two items (``scheme`` and ``host``) present
    but if either or both are empty a relative or scheme-relative URL is generated.
    If all items are empty, ``null`` is returned.
    The ``path``, ``query``, and ``fragment`` items are url-escaped if present.
    Default ports (80 and 443 for ``http`` and ``https`` URLs respectively) are omitted even if specified.
    """
    if not scheme and not host and not path and not query and not frag:
        return None

    if port and (
        not (scheme == "https" and int(port) == 443)
        and not (scheme == "http" and int(port) == 80)
    ):
        netloc = f"{host}:{port}"
    else:
        # omit default ports
        netloc = host or ""
    if path:
        path = quote(path)
    if netloc and path and path[0] != "/":
        path = "/" + path
    if query:
        query = "?" + quote_plus(query)
    else:
        query = ""
    if frag:
        frag = "#" + quote(frag)
    else:
        frag = ""

    prefix = ""  # relative url
    if scheme:
        # absolute url or relative url with scheme
        prefix = scheme + ":"
    if netloc:
        # its an absolute url or scheme-relative url if scheme is missing
        prefix += "//"

    return prefix + netloc + (path or "") + query + frag


set_eval_func("urljoin", lambda args, ctx: _urljoin(*args), False, True)


class ContainerImage(ExternalValue):
    """
    Represents a container image.
    get() returns name of the image, which may be qualified
    with the registry url, repository name, tag, or digest

    All of the following are valid:

    "name"
    "repository/name"
    "name:tag"
    "repository/name:tag"
    "registry.docker.io/repository/name
    "registry.example.com:8080/repository/name:tag"
    "registry-1.docker.io/repository/name@sha256:digest"
    """

    # https://docs.docker.com/engine/reference/commandline/tag/
    # An image name is made up of slash-separated name components,
    # optionally prefixed by a registry hostname. The hostname must comply with standard DNS rules,
    # but may not contain underscores. If a hostname is present, it may optionally be followed by
    # a port number in the format :8080. If not present, the command uses Docker’s public registry
    # located at registry-1.docker.io by default. Name components may contain lowercase letters, digits
    # and separators. A separator is defined as a period, one or two underscores, or one or more dashes.
    # A name component may not start or end with a separator.
    # A tag name must be valid ASCII and may contain lowercase and uppercase letters, digits, underscores, periods and dashes.
    # # A tag name may not start with a period or a dash and may contain a maximum of 128 characters.

    def __init__(
        self,
        name: str,
        tag: Optional[str] = None,
        digest: Optional[str] = None,
        registry_host: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        source_digest: Optional[str] = None,
    ):
        self.name = name.lstrip("/").lower()
        self.tag = tag
        self.digest = digest
        self.registry_host = registry_host
        self.username = username
        self.password = password
        self.source_digest = source_digest
        super().__init__("container_image", self.get())

    # XXX
    # def resolve_key(self, name=None, currentResource=None):
    #     # hostname: registry-1.docker.io, name, tag, digest

    def get(self) -> str:
        if self.registry_host:
            name = os.path.join(self.registry_host, self.name)
        else:
            name = self.name

        if self.tag:
            return f"{name}:{self.tag}"
        if self.digest:
            if "@" == self.digest[0]:
                return name + self.digest
            else:
                return f"{name}@sha256:{self.digest}"
        return name

    def full_name(self, default_hostname="docker.io") -> str:
        if not self.registry_host:
            return os.path.join(default_hostname, self.get())
        else:
            return self.get()

    @staticmethod
    def split(
        artifact_name: str,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        if not artifact_name:
            return None, None, None, None
        hostname = None
        namespace, sep, name = artifact_name.partition("/")
        if sep and (":" in namespace or artifact_name.count("/") > 1):
            # heuristic because name can look like a hostname
            hostname = namespace
        else:
            name = artifact_name

        tag = None
        name, sep, digest = name.partition("@")
        if not sep:
            digest = None  # type: ignore
            name, sep, qualifier = artifact_name.partition(":")
            if sep:
                tag = qualifier
        return name.lower(), tag, digest, hostname

    @staticmethod
    def make(artifact_name: str) -> Optional["ContainerImage"]:
        parts = ContainerImage.split(artifact_name)
        if parts[0]:
            return ContainerImage(*parts)  # type: ignore
        else:
            return None

    @staticmethod
    def resolve_name(base_name: str, artifact_name: str):
        if not base_name:
            return artifact_name
        # support more qualified name such as image name or tag
        name, sep, qualifier = artifact_name.partition("@")
        if not sep:
            name, sep, qualifier = artifact_name.partition(":")

        # if beginning of name overlaps with end of self.name discard it
        segs, new_segs = base_name.split("/"), name.split("/")
        while segs[-len(new_segs) :] == new_segs:
            new_segs.pop(0)
        if new_segs:
            name = base_name + "/" + "/".join(new_segs)
        else:
            name = base_name
        return name.lower() + sep + qualifier

    def __digestable__(self, options):
        if self.digest:
            return self.digest
        else:
            return (self.source_digest, self.get())


set_eval_func(
    "container_image", lambda arg, ctx: ContainerImage.make(map_value(arg, ctx))
)


def _get_instances_from_keyname(ctx, entity_name):
    ctx = ctx.copy(ctx._lastResource)
    query = _toscaKeywordsToExpr.get(entity_name, "::" + entity_name)
    instances = cast(ResultsList, ctx.query(query, wantList=True))
    if instances:
        return instances
    else:
        ctx.trace("entity_name not found", entity_name)
        return None


if TYPE_CHECKING:
    from .tosca import ArtifactSpec


def _find_artifact(instances, artifact_name) -> "Optional[ArtifactSpec]":
    for instance in instances:
        # XXX implement instance.artifacts
        artifact = instance.template.artifacts.get(artifact_name)
        if artifact:
            return artifact
    return None


def _get_container_image_from_repository(
    entity, artifact_name
) -> Optional["ContainerImage"]:
    # aka get_artifact_as_value
    name, tag, digest, hostname = ContainerImage.split(artifact_name)
    attr = entity.attributes

    repository_id = attr.get("repository_id")
    if repository_id and name:
        name = ContainerImage.resolve_name(repository_id, name)
    else:
        name = repository_id or name

    tag = tag or attr.get("repository_tag")

    if attr.get("registry_url"):
        hostname = cast(str, attr["registry_url"])
        if "//" in hostname:
            hostname = urlsplit(attr["registry_url"]).netloc
    username = attr.get("username")
    password = attr.get("password")
    source_digest = attr.get("revision")
    if not name:
        return None
    return ContainerImage(
        name, tag, digest, hostname, username, password, source_digest
    )


def get_artifact(ctx: RefContext, entity, artifact_name, location=None, remove=None):
    """
    Returns either an URL or local path to the artifact
    See section "4.8.1 get_artifact" in TOSCA 1.3 (p. 189)

    If the artifact is a container image, return the image name in the form of
    "registry/repository/name:tag" or "registry/repository/name@sha256:digest"

    If entity_name or artifact_name is not found return None.
    """
    from .runtime import NodeInstance, ArtifactInstance

    if not entity:
        return ContainerImage.make(
            artifact_name
        )  # XXX this assumes its a container image
    if isinstance(entity, ArtifactInstance):
        return entity.template.as_value()
    if isinstance(entity, str):
        instances = _get_instances_from_keyname(ctx, entity)
    elif isinstance(entity, NodeInstance):
        if entity.template.is_compatible_type("unfurl.nodes.Repository"):
            # XXX retrieve method from template definition
            return _get_container_image_from_repository(entity, artifact_name)
        else:
            instances = [entity]
    else:
        return None

    if instances:
        artifact = _find_artifact(instances, artifact_name)
        if artifact:
            return artifact.as_value()
        else:
            ctx.trace("artifact not found", artifact_name)
    return None
    # XXX TOSCA 1.3 stuff:
    # if not location:
    #     #if  deploy_path # update artifact instance
    #     return artifact.getUrl()
    # else:
    #     # if location is set, update the artifact instance's copies: and remove attributes if necessary
    #     # if location == 'LOCAL_FILE':


set_eval_func("get_artifact", lambda args, ctx: get_artifact(ctx, *(map_value(args, ctx))), True)  # type: ignore


def get_import(arg: RefContext, ctx):
    """
    Returns the external resource associated with the named import
    """
    try:
        imported = ctx.currentResource.root.imports[arg]
    except KeyError:
        raise UnfurlError(f"Can't find import '{arg}'")
    if arg == "secret":
        return SecretResource(arg, imported)
    else:
        return ExternalResource(arg, imported)


set_eval_func("external", get_import)


class _Import:
    def __init__(
        self,
        external_instance: "HasInstancesInstance",
        spec: dict,
        local_instance: Optional["HasInstancesInstance"] = None,
    ):
        self.external_instance = external_instance
        self.spec = spec
        self.local_instance = local_instance


if TYPE_CHECKING:  # for python 3.7
    ImportsBase = collections.OrderedDict[str, _Import]
else:
    ImportsBase = collections.OrderedDict


class Imports(ImportsBase):
    manifest: Optional["Manifest"] = None

    def find_import(self, qualified_name: str) -> Optional["HasInstancesInstance"]:
        # return a local shadow of the imported instance
        # or the imported instance itself if no local shadow exist (yet).
        imported = self._find_import(qualified_name)
        if imported:
            return imported
        iName, sep, rName = qualified_name.partition(":")
        if not iName:
            # name is in the current ensemble (but maybe a different topology)
            return None
        assert self.manifest
        localEnv = self.manifest.localEnv
        if iName not in self and localEnv:
            project = localEnv.project or localEnv.homeProject
            tpl = project and project.find_ensemble_by_name(iName)
            if tpl:
                self.manifest.load_external_ensemble(iName, dict(manifest=tpl))  # type: ignore
                return self._find_import(qualified_name)
        return None

    def _find_import(self, name: str) -> Optional["HasInstancesInstance"]:
        if name in self:
            # fully qualified name already added
            return self[name].local_instance or self[name].external_instance
        iName, sep, rName = name.partition(":")
        if not iName:
            # name is in the current ensemble (but maybe a different topology)
            assert self.manifest and self.manifest.rootResource
            assert sep and rName
            return self.manifest.rootResource.find_instance(rName)
        if iName not in self:
            return None
        # do a unqualified look up to find the declared import
        imported = self[iName].external_instance.root.find_instance(rName or "root")
        if imported:
            self.add_import(iName, imported)
        return imported

    def set_shadow(self, key, local_instance, external_instance):
        if key not in self:
            record = self.add_import(key, external_instance)
        else:
            record = self[key]

        if record.local_instance:
            raise UnfurlError(f"already imported {key}")
        record.local_instance = local_instance
        return record

    def add_import(self, key, external_instance, spec=None):
        self[key] = _Import(external_instance, spec or {})
        return self[key]


class ExternalResource(ExternalValue):
    """
    Wraps a foreign resource
    """

    def __init__(self, name, importSpec):
        super().__init__("external", name)
        self.resource = importSpec.external_instance
        self.schema = importSpec.spec.get("schema")

    def _validate(self, obj, schema, name):
        if schema:
            messages = find_schema_errors(serialize_value(obj), schema)
            if messages:
                (message, schemaErrors) = messages
                raise UnfurlValidationError(
                    f"schema validation failed for attribute '{name}': {schemaErrors}",
                    schemaErrors,  # type: ignore
                )

    def _get_schema(self, name):
        return self.schema and self.schema.get(name, {})

    def get(self):
        return self.resource

    def resolve_key(self, name=None, currentResource=None):
        if not name:
            return self.resource

        schema = self._get_schema(name)
        try:
            value = self.resource._resolve(name)
            # value maybe a Result
        except KeyError:
            if schema and "default" in schema:
                return schema["default"]
            raise

        if schema:
            self._validate(value, schema, name)
        # we don't want to return a result across boundaries
        return value


class SecretResource(ExternalResource):
    def resolve_key(self, name=None, currentResource=None):
        # raises KeyError if not found
        val = super().resolve_key(name, currentResource)
        if isinstance(val, Result):
            val.resolved = wrap_sensitive_value(val.resolved)
            return val
        else:
            return wrap_sensitive_value(val)

    def __sensitive__(self):
        return True


# shortcuts for local and secret
def shortcut(arg, ctx):
    return Ref(dict(ref=dict(external=ctx.currentFunc), select=arg)).resolve(
        ctx, wantList="result"
    )


set_eval_func("local", shortcut)
set_eval_func("secret", shortcut)


class DelegateAttributes:
    def __init__(self, interface, resource):
        self.interface = interface
        self.resource = resource
        if interface == "inherit":
            self.inheritFrom = resource.attributes["inheritFrom"]
        if interface == "default":
            # use '_attributes' so we don't evaluate "default"
            self.default = resource._attributes.get("default")

    def __call__(self, key):
        if self.interface == "inherit":
            return self.inheritFrom.attributes[key]
        elif self.interface == "default":
            result = map_value(
                self.default,
                RefContext(self.resource, vars=dict(key=key), wantList=True),
            )
            if not result:
                raise KeyError(key)
            elif len(result) == 1:
                return result[0]
            else:
                return result


AttributeChanges = NewType(
    "AttributeChanges", Dict["InstanceKey", MutableMapping[str, Any]]
)


class ResourceChanges(collections.OrderedDict):
    """
    Records changes made by configurations.
    Serialized as the "modifications" properties

    changes:
      resource1:
        attribute1: newvalue
        attribute2: %delete # if deleted
        .added: # set if resource was added
        .status: # set when status changes, including when removed (Status.absent)
    """

    statusIndex = 0
    addedIndex = 1
    attributesIndex = 2

    def get_attribute_changes(self, key: "InstanceKey"):
        record = self.get(key)
        if record:
            return record[self.attributesIndex]
        return {}

    def get_changes_as_expr(self) -> Iterator[str]:
        for key, record in self.items():
            attribute_changes = record[self.attributesIndex]
            if attribute_changes:
                yield from (key + "::" + attr for attr in attribute_changes)

    def sync(self, resource: "EntityInstance", changeId=None):
        """Update self to only include changes that are still live"""
        for k, v in list(self.items()):
            current = cast(
                Optional["EntityInstance"], Ref(k).resolve_one(RefContext(resource))
            )
            if current is not None:
                attributes = v[self.attributesIndex]
                if attributes:
                    v[self.attributesIndex] = intersect_dict(
                        attributes, current._attributes
                    )
                if v[self.statusIndex] != current._localStatus:
                    v[self.statusIndex] = None

                if changeId and (v[0] or v[1] or v[2]):
                    current._lastConfigChange = changeId
            else:
                del self[k]

    def add_changes(self, changes: AttributeChanges):
        for name, change in changes.items():
            old = self.get(name)
            if old:
                old[self.attributesIndex] = merge_dicts(
                    old[self.attributesIndex], change
                )
            else:
                self[name] = [None, None, change]

    def add_statuses(self, changes):
        for name, change in changes.items():
            assert not isinstance(change[1], str)
            old = self.get(name)
            if old:
                old[self.statusIndex] = change[1]
            else:
                self[name] = [change[1], None, {}]

    def add_resources(self, resources):
        for resource in resources:
            self["::" + resource["name"]] = [None, resource, None]

    def update_changes(
        self, changes: AttributeChanges, statuses, resource, changeId=None
    ):
        self.add_changes(changes)
        self.add_statuses(statuses)
        if resource:
            self.sync(resource, changeId)

    def rollback(self, resource):
        # XXX need to actually rollback
        self.clear()


class TopologyMap(dict):
    # need to subtype dict directly to make jinja2 happy
    def __init__(self, resource):
        self.resource = resource

    def copy(self):
        return self

    def __getitem__(self, key):
        r = self.resource.find_resource(key)
        if r:
            return r.attributes
        else:
            raise KeyError(key)

    def __iter__(self):
        return iter(r.name for r in self.resource.get_self_and_descendants())

    def __len__(self):
        return len(tuple(self.resource.get_self_and_descendants()))


LiveDependencies = NewType(
    "LiveDependencies",
    Dict[
        "InstanceKey",
        Tuple["EntityInstance", Dict[str, Tuple[bool, Union[Result, Any]]]],
    ],
)


class AttributeManager:
    """
    Tracks changes made to Resources

    Configurator set attributes override spec attributes.
    A configurator can delete an attribute but it will not affect the spec attributes
    so deleting an attribute is essentially restoring the spec's definition of the attribute
    (if it is defined in a spec.)
    Changing an overridden attribute definition in the spec will have no effect
    -- if a configurator wants to re-evaluate that attribute, it can create a dependency on it
    so to treat that as changed configuration.
    """

    validate = True
    safe_mode = False
    strict: Optional[bool] = None

    # what about an attribute that is added to the spec that already exists in status?
    # XXX2 tests for the above behavior
    def __init__(self, yaml=None, task=None):
        self.attributes: Dict[str, Tuple[EntityInstance, ResultsMap]] = {}
        self.statuses = {}
        self._yaml = yaml  # hack to safely expose the yaml context
        self.task = task
        self._context_vars = None

    @property
    def yaml(self):
        return self._yaml

    def __getstate__(self):
        state = self.__dict__.copy()
        state["_yaml"] = None
        return state

    def get_status(self, resource):
        if resource.key not in self.statuses:
            return resource._localStatus, resource._localStatus
        return self.statuses[resource.key]

    def set_status(self, resource, newvalue):
        assert newvalue is None or isinstance(newvalue, Status)
        if resource.key not in self.statuses:
            self.statuses[resource.key] = [resource._localStatus, newvalue]
        else:
            self.statuses[resource.key][1] = newvalue

    def mark_referenced_templates(self, template):
        for resource, attr in self.attributes.values():
            if (
                resource.template is not template
                and template not in resource.template._isReferencedBy
            ):
                resource.template._isReferencedBy.append(template)  # type: ignore

    def _get_context(self, resource):
        if (
            self._context_vars is None
            or self._context_vars["NODES"].resource is not resource.root
        ):
            self._context_vars = {}
            set_context_vars(self._context_vars, resource)
        ctor = SafeRefContext if self.safe_mode else RefContext
        return ctor(resource, self._context_vars, task=self.task, strict=self.strict)

    def get_attributes(self, resource: "EntityInstance") -> ResultsMap:
        if resource.key not in self.attributes:
            if resource.shadow:
                return resource.shadow.attributes

            if resource.template:
                specAttributes = resource.template.defaultAttributes
                properties = resource.template.properties  # type: ignore
                _attributes = ChainMap(
                    resource._attributes,
                    properties,
                    specAttributes,
                )
            else:
                _attributes = ChainMap(resource._attributes)
            ctx = self._get_context(resource)
            mode = os.getenv("UNFURL_VALIDATION_MODE")
            if mode is not None and "nopropcheck" in mode:
                self.validate = False
            attributes = ResultsMap(_attributes, ctx, validate=self.validate)
            self.attributes[resource.key] = (resource, attributes)
            return attributes
        else:
            attributes = self.attributes[resource.key][1]
            return attributes

    # def revertChanges(self):
    #   self.attributes = {}
    #   # for resource, old, new in self.statuses.values():
    #   #   resource._localStatus = old

    @staticmethod
    def _save_sensitive(defs, key, value):
        # attribute marked as sensitive and value isn't a secret so mark value as sensitive
        # but externalvalues are ok since they don't reveal much
        sensitive = is_sensitive_schema(defs, key) and not value.external
        if sensitive:
            savedValue = wrap_sensitive_value(value.resolved)
        else:
            savedValue = value.as_ref()  # serialize Result
        return savedValue

    @staticmethod
    def _check_attribute(specd, key, value, instance):
        changed = value.has_diff()
        live = (
            changed  # modified by this task
            # explicitly declared an attribute:
            or key in instance.template.attributeDefs
            or key not in specd  # not declared as a property
            or key in instance._attributes  # previously modified
        )
        return changed, live

    def find_live_dependencies(self) -> Dict["InstanceKey", List["Dependency"]]:
        from .configurator import Dependency

        dependencies: Dict[InstanceKey, List["Dependency"]] = {}
        for resource, attributes in self.attributes.values():
            overrides, specd = attributes._attributes.split()
            # items in overrides of type Result have been accessed during this transaction
            for key, value in overrides.items():
                if isinstance(value, Result):
                    changed, isLive = self._check_attribute(specd, key, value, resource)
                    if isLive:
                        dep = Dependency(
                            resource.key + "::" + key, value, target=resource
                        )
                        dependencies.setdefault(resource.key, []).append(dep)
        return dependencies

    def commit_changes(self) -> Tuple[AttributeChanges, LiveDependencies]:
        changes: AttributeChanges = cast(AttributeChanges, {})
        liveDependencies = cast(LiveDependencies, {})
        for resource, attributes in list(self.attributes.values()):
            overrides, specd = attributes._attributes.split()
            # overrides will only contain:
            #  - properties accessed or added while running a task
            #  - properties loaded from the ensemble status yaml (which implies it was previously added or changed)
            _attributes = {}
            defs = resource.template and resource.template.propertyDefs or {}
            foundSensitive = []
            live: Dict[str, Any] = {}
            # items in overrides of type Result have been accessed during this transaction
            for key, value in list(overrides.items()):
                if not isinstance(value, Result):
                    # hasn't been accessed so keep it as is
                    _attributes[key] = value
                else:
                    changed, isLive = self._check_attribute(specd, key, value, resource)
                    savedValue = self._save_sensitive(defs, key, value)
                    is_sensitive = isinstance(savedValue, sensitive)
                    # save the Result not savedValue because we need the ExternalValue
                    live[key] = (isLive, savedValue if is_sensitive else value)
                    if not isLive:
                        resource._properties[key] = savedValue
                        assert not changed  # changed implies isLive
                        continue  # it hasn't changed and it is part of the spec so don't save it as an attribute

                    if changed and is_sensitive:
                        foundSensitive.append(key)
                        # XXX if defMeta.get('immutable') and key in specd:
                        #  error('value of attribute "%s" changed but is marked immutable' % key)

                    # save in serialized form
                    _attributes[key] = savedValue
            resource._attributes = _attributes

            if live:
                liveDependencies[resource.key] = (resource, live)
            # save changes
            diff = attributes.get_diff()
            if not diff:
                continue
            for key in foundSensitive:
                if key in diff:
                    diff[key] = _attributes[key]
            changes[resource.key] = diff

        self.attributes = {}
        # self.statuses = {}
        return changes, liveDependencies
