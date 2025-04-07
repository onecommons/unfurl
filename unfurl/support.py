# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Internal classes supporting the runtime.
"""

import base64
import collections
from collections.abc import MutableSequence, Mapping
import math
import os
import sys
import os.path
import re
import ast
import time
from typing import (
    TYPE_CHECKING,
    Iterable,
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
    OrderedDict,
)
from typing_extensions import NoReturn
from enum import Enum
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .manifest import Manifest
    from .yamlmanifest import YamlManifest
    from .runtime import (
        EntityInstance,
        InstanceKey,
        HasInstancesInstance,
        TopologyInstance,
        RelationshipInstance,
    )
    from .configurator import Dependency

from .logs import getLogger
from .eval import _Tracker, RefContext, set_eval_func, Ref, map_value, SafeRefContext
from .result import (
    Results,
    ResultsList,
    ResultsMap,
    ResultsItem,
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
)
from .merge import intersect_dict, merge_dicts
from .projectpaths import FilePath, get_path

import ansible.template
from ansible.parsing.dataloader import DataLoader
from ansible.utils import unsafe_proxy
from ansible.utils.unsafe_proxy import wrap_var, AnsibleUnsafeText, AnsibleUnsafeBytes
from jinja2.runtime import DebugUndefined
from toscaparser.elements.portspectype import PortSpec
from toscaparser.elements import constraints
from toscaparser.nodetemplate import NodeTemplate
from toscaparser.properties import Property

from .tosca_plugins.functions import (
    urljoin,
    to_label,
    to_dns_label,
    to_kubernetes_label,
    to_googlecloud_label,
    generate_string,
)

logger = getLogger("unfurl")


class Status(int, Enum):
    unknown = 0
    "The operational state of the instance is unknown."
    ok = 1
    "Instance is operational"
    degraded = 2
    "Instance is operational but in a degraded state."
    error = 3
    "Instance is not operational."
    pending = 4
    "Instance is being brought up or hasn't been created yet."
    absent = 5
    "Instance confirmed to not exist."

    @property
    def color(self):
        ":meta private:"
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
    """
    An enumeration representing :tosca_spec:`TOSCA Node States <_Toc50125214>`.
    """

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


class Reason(str, Enum):
    add = "add"
    reconfigure = "reconfigure"
    force = "force"
    upgrade = "upgrade"
    update = "update"
    missing = "missing"
    repair = "repair"
    degraded = "degraded"
    prune = "prune"
    run = "run"
    check = "check"
    undeploy = "undeploy"
    stop = "stop"
    connect = "connect"
    subtask = "subtask"
    error = "error"
    """Synonym for repair. Deprecated."""

    def __str__(self) -> str:
        return self.value


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


def reload_collections(ctx=None):
    # collections may have been installed while the job is running, need reset the loader to pick those up
    import ansible.utils.collection_loader._collection_finder
    import ansible.template
    import ansible.plugins.loader

    AnsibleCollectionConfig = (
        ansible.utils.collection_loader._collection_finder.AnsibleCollectionConfig
    )
    AnsibleCollectionConfig._collection_finder = None
    if hasattr(ansible.plugins.loader, "init_plugin_loader"):
        collection_path_var = os.getenv("ANSIBLE_COLLECTIONS_PATH")
        if collection_path_var:
            collection_path = collection_path_var.split()
        else:
            collection_path = []
        ansible.plugins.loader.init_plugin_loader(collection_path)
    else:
        ansible.plugins.loader._configure_collection_loader()
    for pkg in ["ansible_collections", "ansible_collections.ansible"]:
        AnsibleCollectionConfig._collection_finder._reload_hack(pkg)  # type: ignore
    if hasattr(ansible.template, "_get_collection_metadata"):
        # jinja2 templates won't get the updated collection finder without this:
        ansible.template._get_collection_metadata = (
            ansible.utils.collection_loader._collection_finder._get_collection_metadata
        )
    logger.trace("reloaded ansible collections finder")


reload_collections()


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


def apply_template(value: str, ctx: RefContext, overrides=None) -> Union[Any, Result]:
    if not isinstance(value, str):
        msg = f"Error rendering template: source must be a string, not {type(value)}"  # type: ignore[unreachable]
        if ctx.strict:
            raise UnfurlError(msg)
        else:
            return f"<<{msg}>>"
    value = value.strip()
    if ctx.task:
        log = ctx.task.logger
    else:
        log = logger  # type: ignore

    # local class to bind with logger and ctx
    class _UnfurlUndefined(DebugUndefined):
        __slots__ = ()

        def _log_message(self) -> None:
            msg = "Template: %s" % self._undefined_message
            # XXX? if self._undefined_obj is a Results then add its ctx._lastResource to the msg
            log.debug("%s\nTemplate source:\n%s", msg, value)
            log.warning(msg)
            if ctx.task:  # already logged, so don't log
                UnfurlTaskError(ctx.task, msg, False)

        def _fail_with_undefined_error(  # type: ignore
            self, *args: Any, **kwargs: Any
        ) -> "NoReturn":
            try:
                super()._fail_with_undefined_error(*args, **kwargs)
            except self._undefined_exception as e:
                msg = "Template: %s" % self._undefined_message
                log.debug("%s\nTemplate source:\n%s", msg, value)
                log.warning(msg)
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
    if (
        not ctx.templar
        or not ctx.templar._loader
        or (ctx.base_dir and ctx.templar._loader.get_basedir() != ctx.base_dir)
    ):
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
        __unfurl=ctx,
        __python_executable=sys.executable,
        __now=time.time(),
    )
    if hasattr(ctx.currentResource, "attributes"):
        vars["SELF"] = ctx.currentResource.attributes  # type: ignore
    if os.getenv("UNFURL_TEST_DEBUG_EX"):
        log.debug("template vars for %s: %s", value[:300], list(ctx.vars))
    vars.update(ctx.vars)
    vars.ctx = ctx

    # replaces current vars
    # don't use setter to avoid isinstance(dict) check
    templar._available_variables = vars

    oldvalue = value
    ctx.trace("evaluating template", value)
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
                log.debug("%s\nTemplate source:\n%s", value, oldvalue, exc_info=True)
                raise UnfurlError(value)
            else:
                log.warning(value[2:100] + "... see debug log for full report")
                log.debug("%s\nTemplate source:\n%s", value, oldvalue, exc_info=True)
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
                        # don't know how the referenced sensitive results were transformed by the template
                        ctx.trace("setting template result as sensitive")
                        # mark the template result as sensitive
                        return wrap_sensitive_value(value)
                    if result.external:
                        external_result = result

                if (
                    external_result
                    and ctx.wantList == "result"
                    and external_result.external
                    and value == external_result.external.get()
                ):
                    # return a Result with the external value instead
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


def _template_func(args, ctx: RefContext):
    args = map_value(args, ctx, False)  # don't apply templates yet
    if isinstance(args, Mapping) and "path" in args:
        path = args["path"]
        if is_template(path, ctx):  # path could be a template expression
            path = apply_template(path, ctx)
        if isinstance(path, FilePath):
            path = path.get()
        if not os.path.isabs(path):
            path = get_path(ctx, path, "src")
        if not os.path.isabs(path):
            path = get_path(ctx, path, "src")
        ctx.trace("loading template from", path)
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
    arg = assert_form(arg, test=arg)  # a map with at least one element
    name = None
    args = None
    kwargs: Dict[str, Any] = {}
    for key, value in arg.items():
        if not name:
            name = key
            args = value
        else:
            kwargs[key] = value  # type: ignore[unreachable]

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
    """
    The token function is used within a TOSCA service template on a string to
    parse out (tokenize) substrings separated by one or more token characters
    within a larger string.


    Arguments:

    * The composite string that contains one or more substrings separated by
      token characters.
    * The string that contains one or more token characters that separate
      substrings within the composite string.
    * The integer indicates the index of the substring to return from the
      composite string.  Note that the first substring is denoted by using
      the '0' (zero) integer value.

    Example:

     [ get_attribute: [ my_server, data_endpoint, ip_address ], ':', 1 ]
    """
    args = map_value(args, ctx)
    return args[0].split(args[1])[args[2]]


set_eval_func("token", token, True, True)


# XXX this doesn't work with node_filters, need an instance to get a specific result
def get_tosca_property(args, ctx):
    from toscaparser.functions import get_function
    from .spec import EntitySpec

    if isinstance(ctx.currentResource, EntitySpec):
        tosca_tpl = ctx.currentResource.topology.toscaEntityTemplate
        node_template = ctx.currentResource.toscaEntityTemplate
    else:
        tosca_tpl = ctx.currentResource.root.template.toscaEntityTemplate
        node_template = ctx.currentResource.template.toscaEntityTemplate
    return get_function(tosca_tpl, node_template, {"get_property": args}).result()


set_eval_func("get_property", get_tosca_property, True, True)


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

set_eval_func(
    "_arguments",
    lambda arg, ctx: ctx.task and ctx.task._arguments(),
)


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
    app_template = root.template.topology.substitution_node
    if app_template:
        app = root.find_instance(app_template.name)
        if app:
            ROOT["app"] = app.attributes
        for name, req in app_template.requirements.items():
            if req.relationship and req.relationship.target:
                target = root.get_root_instance(
                    cast(NodeTemplate, req.relationship.target.toscaEntityTemplate)
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

    def __contains__(self, key) -> bool:
        try:
            self[key]  # needs to be resolved
            return True
        except KeyError:
            return False

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

set_eval_func(
    "to_label",
    lambda arg, ctx: to_label(
        map_value(arg, ctx, flatten=True), **map_value(ctx.kw, ctx)
    ),
    safe=True,
)


set_eval_func(
    "to_dns_label",
    lambda arg, ctx: to_dns_label(
        map_value(arg, ctx, flatten=True), **map_value(ctx.kw, ctx)
    ),
    safe=True,
)


set_eval_func(
    "to_kubernetes_label",
    lambda arg, ctx: to_kubernetes_label(
        map_value(arg, ctx, flatten=True), **map_value(ctx.kw, ctx)
    ),
    safe=True,
)

set_eval_func(
    "to_googlecloud_label",
    lambda arg, ctx: to_googlecloud_label(
        map_value(arg, ctx, flatten=True), **map_value(ctx.kw, ctx)
    ),
    safe=True,
)


def get_ensemble_metadata(arg, ctx: RefContext):
    if not ctx.task or not ctx.task.job:
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
            parts = os.path.dirname(ensemble.repo.project_path().lower()).split("/")
            return ".".join(reversed([to_dns_label(p) for p in parts]))
        return metadata.get(key, "")
    else:
        return metadata


set_eval_func("get_ensemble_metadata", get_ensemble_metadata)


_toscaKeywordsToExpr = {
    "SELF": ".",
    "SOURCE": ".source",
    "TARGET": ".target",
    "ORCHESTRATOR": "::localhost",
    "HOST": ".hosted_on",
    "OPERATION_HOST": "$OPERATION_HOST",
}


def resolve_function_keyword(node_name: str):
    """see 4.1 Reserved Function Keywords"""
    return _toscaKeywordsToExpr.get(node_name, "::" + node_name)


def get_attribute(args, ctx: RefContext):
    args = map_value(args, ctx)
    entity_name = args.pop(0)
    candidate_name = args.pop(0)
    ctx = ctx.copy(ctx._lastResource)

    start = resolve_function_keyword(entity_name)
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
    from .spec import EntitySpec

    if isinstance(ctx.currentResource, EntitySpec):
        return list(ctx.currentResource.topology.find_matching_templates(type_name))
    else:
        return [
            r
            for r in ctx.currentResource.root.get_self_and_descendants()
            if r.template.is_compatible_type(type_name)
            and r.name not in ["inputs", "outputs"]
        ]


set_eval_func("get_nodes_of_type", get_nodes_of_type, True, True)


# use this alias for compatibility with unfurl-gui (note that arg are kw args)
# (generate_string is wrapped by the eval_func decorator)
set_eval_func(
    "_generate",
    lambda arg, ctx: generate_string._func(**map_value(arg, ctx)),  # type:ignore [attr-defined]
    True,
    True,
)


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
        digest: Optional[str]
        name, sep, digest = name.partition("@")
        if not sep:
            digest = None
            name, sep, qualifier = name.partition(":")
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
    query = resolve_function_keyword(entity_name)
    instances = cast(ResultsList, ctx.query(query, wantList=True))
    if instances:
        return instances
    else:
        ctx.trace("entity_name not found", entity_name)
        return None


if TYPE_CHECKING:
    from .spec import ArtifactSpec


def _find_artifact(instances, artifact_name) -> "Optional[ArtifactSpec]":
    for instance in instances:
        # XXX implement instance.artifacts
        artifact = instance.template.artifacts.get(artifact_name)
        if artifact:
            return artifact
    return None


def _get_container_image_from_repository(
    entity: "EntityInstance", artifact_name: str
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


def get_artifact(
    ctx: RefContext,
    entity: Union[None, str, "EntityInstance"],
    artifact_name: Union[str, Dict[str, str]],
    location=None,
    remove=None,
) -> Optional[ExternalValue]:
    """
    Returns either an URL or local path to the artifact
    See section "4.8.1 get_artifact" in TOSCA 1.3 (p. 189)

    If the artifact is a container image, return the image name in the form of
    "registry/repository/name:tag" or "registry/repository/name@sha256:digest"

    If entity_name or artifact_name is not found return None.
    """
    from .runtime import NodeInstance, ArtifactInstance

    if entity == "ANON":
        current = ctx.currentResource.template
        if not current:
            return None
        artifact = current.find_or_create_artifact(artifact_name, predefined=False)
        if artifact:
            return artifact.as_value()
        else:
            return None
    elif not isinstance(artifact_name, str):
        return None
    elif isinstance(entity, str):
        instances = _get_instances_from_keyname(ctx, entity)
    elif not entity:
        return ContainerImage.make(
            artifact_name
        )  # XXX this assumes its a container image
    elif isinstance(entity, ArtifactInstance):
        return cast(ArtifactSpec, entity.template).as_value()
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


set_eval_func(
    "get_artifact", lambda args, ctx: get_artifact(ctx, *(map_value(args, ctx))), True
)  # type: ignore


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


def register_custom_constraint(key, func):
    class CustomConstraint(constraints.Constraint):
        constraint_key = key
        valid_prop_types = constraints.Schema.PROPERTY_TYPES

        def __init__(self, property_name, property_type, constraint):
            self.property_name = property_name
            self.property_type = property_type
            self.constraint_value = constraint[self.constraint_key]
            self.constraint_value_msg = self.constraint_value

        def _is_valid(self, value):
            return func(value)

    constraints.constraint_mapping[key] = CustomConstraint
    return CustomConstraint


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


class Imports(OrderedDict[str, _Import]):
    """
    Track shadowed nodes from external manifests and nested topologies.
    The key syntax is import_name[":"node_name] for imported nodes and ":"nested_name for nested nodes.

    external instance   key syntax                    created by
    -----------------   ----------------------------  ---------------------------------------------------
    local instances     "locals", "secrets"           YamlManifest.__init__
    external root       import_name                   load_external_ensemble
    external nodes      import_name:external_name     Plan.create_shadow_instance
    nested topologies   :outer_node_name:"root"       TopologyInstance.create_nested_topology
    nested root nodes   :outer_node_name:nested_node  _create_substituted_topology, Plan.create_resource
    """

    manifest: Optional["Manifest"] = None

    def find_import(self, qualified_name: str) -> Optional[_Import]:
        imported = self._find_import(qualified_name)
        if imported:
            return imported
        iName, sep, rName = qualified_name.partition(":")
        if not iName:
            return None
        assert self.manifest
        localEnv = self.manifest.localEnv
        if localEnv:
            project = localEnv.project or localEnv.homeProject
            tpl = project and project.find_ensemble_by_name(iName)
            if tpl:
                cast("YamlManifest", self.manifest).load_external_ensemble(
                    iName, dict(manifest=tpl)
                )
                return self._find_import(qualified_name)
        return None

    def find_instance(self, qualified_name: str) -> Optional["HasInstancesInstance"]:
        # return a local shadow of the imported instance
        # or the imported instance itself if no local shadow exist (yet).
        imported = self.find_import(qualified_name)
        if imported:
            return imported.local_instance or imported.external_instance
        iName, sep, rName = qualified_name.partition(":")
        if not iName:
            # name is in the current ensemble (but maybe a different topology)
            assert self.manifest and self.manifest.rootResource
            assert sep and rName
            return self.manifest.rootResource.find_instance(rName)
        return None

    def _find_import(self, name: str) -> Optional[_Import]:
        if name in self:
            # fully qualified name already added
            return self[name]
        iName, sep, rName = name.partition(":")
        if not iName or iName not in self:
            return None
        # do a unqualified look up to find the declared import
        imported = self[iName].external_instance.root.find_instance(rName or "root")
        if imported:
            return self.add_import(iName, imported)
        return None

    def set_shadow(self, key: str, local_instance, external_instance) -> _Import:
        if key not in self:
            record = self.add_import(key, external_instance)
        else:
            record = self[key]

        if record.local_instance:
            raise UnfurlError(f"already imported {key}")
        record.local_instance = local_instance
        return record

    def add_import(self, key, external_instance, spec=None) -> _Import:
        # Adds an external (imported or nested) instance
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


def find_connection(
    ctx: RefContext,
    target: "EntityInstance",
    relation: str = "tosca.relationships.ConnectsTo",
) -> Optional["RelationshipInstance"]:
    connection: Optional["RelationshipInstance"] = None
    if ctx.vars.get("OPERATION_HOST"):
        operation_host = ctx.vars["OPERATION_HOST"].context.currentResource
        connection = cast(
            Optional["RelationshipInstance"],
            Ref(
                f"$operation_host::.requirements::*[.type={relation}][.target=$target]",
                vars=dict(target=target, operation_host=operation_host),
            ).resolve_one(ctx),
        )

    # alternative query: [.type=unfurl.nodes.K8sCluster]::.capabilities::.relationships::[.type=unfurl.relationships.ConnectsTo.K8sCluster][.source=$OPERATION_HOST]
    if not connection:
        # no connection, see if there's a default relationship template defined for this target
        endpoints = target.get_default_relationships(relation)
        if endpoints:
            connection = endpoints[0]
    if connection:
        from .runtime import RelationshipInstance

        assert isinstance(connection, RelationshipInstance)
        return connection
    return None


def _find_connection(arg, ctx):
    return find_connection(
        ctx, map_value(arg, ctx), relation=map_value(ctx.kw.get("relation"), ctx)
    )


set_eval_func("find_connection", _find_connection)


AttributeChange = MutableMapping[str, Any]
AttributesChanges = NewType("AttributesChanges", Dict["InstanceKey", AttributeChange])

ResourceChange = List[
    Union[
        Optional[Status], Optional[MutableMapping[str, Any]], Optional[AttributeChange]
    ]
]

StatusMap = Dict["InstanceKey", List[Optional[Status]]]


class ResourceChanges(OrderedDict["InstanceKey", ResourceChange]):
    """
    Records changes made by configurations.
    Serialized as the "changes" property on change records.

    changes:
      resource1:
        attribute1: newvalue
        attribute2: %delete # if deleted
        # special keys:
        .added: # resource spec if this resource was added by the task
        .status: # set when status changes, including when removed (Status.absent)
    """

    statusIndex = 0
    addedIndex = 1
    attributesIndex = 2

    def get_attribute_changes(self, key: "InstanceKey") -> Optional[AttributeChange]:
        record = self.get(key)
        if record:
            return cast(Optional[AttributeChange], record[self.attributesIndex])
        return None

    def get_changes_as_expr(self) -> Iterator[str]:
        for key, record in self.items():
            attribute_changes = cast(
                Optional[AttributeChange], record[self.attributesIndex]
            )
            if attribute_changes:
                yield from (key + "::" + attr for attr in attribute_changes)

    def sync(self, resource: "EntityInstance", changeId=None) -> None:
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

    def add_changes(self, changes: AttributesChanges) -> None:
        for name, change in changes.items():
            old = self.get(name)
            if old:
                old_change = cast(AttributesChanges, old[self.attributesIndex])
                old[self.attributesIndex] = cast(
                    AttributeChange, merge_dicts(old_change, change)
                )
            else:
                self[name] = [None, None, change]

    def add_statuses(self, changes: StatusMap) -> None:
        for name, change in changes.items():
            assert not isinstance(change[1], str)
            old = self.get(name)
            if old:
                old[self.statusIndex] = change[1]
            else:
                self[name] = [change[1], None, {}]

    def add_resources(self, resources: Iterable[Dict[str, Any]]) -> None:
        for resource in resources:
            self["::" + resource["name"]] = [None, resource, None]

    def update_changes(
        self, changes: AttributesChanges, statuses, resource, changeId=None
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


AttributeInfo = Tuple[bool, Union[ResultsItem, Any], bool]  # live, value, accessed
_Dependencies = NewType(
    "_Dependencies",
    Dict[
        "InstanceKey",
        Tuple["EntityInstance", Dict[str, AttributeInfo]],
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
        self.statuses: StatusMap = {}
        self.super_maps: Dict[Tuple[str, str], ResultsMap] = {}
        self._yaml = yaml  # hack to safely expose the yaml context
        self.task = task
        self._context_vars = None
        self.tracker = _Tracker()

    @property
    def yaml(self):
        return self._yaml

    def __getstate__(self):
        state = self.__dict__.copy()
        state["_yaml"] = None
        state["tracker"] = _Tracker()
        return state

    def get_status(self, resource: "EntityInstance") -> List[Optional[Status]]:
        if resource.key not in self.statuses:
            return [resource._localStatus, resource._localStatus]
        return self.statuses[resource.key]

    def set_status(
        self, resource: "EntityInstance", newvalue: Optional[Status]
    ) -> None:
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

    def _get_context(self, resource) -> RefContext:
        if (
            self._context_vars is None
            or self._context_vars["NODES"].resource is not resource.root
        ):
            self._context_vars = {}
            set_context_vars(self._context_vars, resource)
        ctor = SafeRefContext if self.safe_mode else RefContext
        ctx = ctor(resource, self._context_vars, task=self.task, strict=self.strict)
        ctx.referenced = self.tracker
        return ctx

    def get_attributes(self, resource: "EntityInstance") -> ResultsMap:
        if resource.nested_key not in self.attributes:
            if resource.shadow:
                return resource.shadow.attributes

            if resource.template:
                specAttributes = resource.template.defaultAttributes
                properties = resource.template.properties
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
            self.attributes[resource.nested_key] = (resource, attributes)
            return attributes
        else:
            attributes = self.attributes[resource.nested_key][1]
            return attributes

    def get_super(self, ctx: RefContext) -> Optional[ResultsMap]:
        if (
            not ctx._lastResource.template
            or not ctx._lastResource.template.toscaEntityTemplate.type_definition
        ):
            return None
        key = (
            ctx._lastResource.key,
            ctx.tosca_type and ctx.tosca_type.global_name or "",
        )
        super_map = self.super_maps.get(key)
        if super_map is not None:
            return super_map
        base_defs = None
        if ctx.tosca_type:
            tosca_type = (
                ctx._lastResource.template.toscaEntityTemplate.type_definition.super(
                    ctx.tosca_type
                )
            )
        else:
            tosca_type = ctx._lastResource.template.toscaEntityTemplate.type_definition
            base_type = ctx._lastResource.template.toscaEntityTemplate.type_definition.parent_type
            if base_type:
                base_defs = base_type.get_properties_def()
        if not tosca_type:  # no more super classes
            return None
        _attributes = {}
        defs = {}
        for p_def in tosca_type.get_properties_def_objects():
            if p_def.schema and "default" in p_def.schema:
                if not ctx.tosca_type:
                    # if a property is not defined on the template its value will be set to its type's default value
                    # so super() should that type's superclass's default instead of the type's default value
                    if (
                        p_def.name
                        not in ctx._lastResource.template.toscaEntityTemplate._properties_tpl
                    ):
                        if not base_defs or p_def.name not in base_defs:
                            continue
                        p_def = base_defs[p_def.name]
                        if not p_def.schema or "default" not in p_def.schema:
                            continue
                _attributes[p_def.name] = p_def.default
                defs[p_def.name] = Property(
                    p_def.name, p_def.default, p_def.schema, tosca_type.custom_def
                )
        super_map = ResultsMap(
            _attributes, ctx.copy(tosca_type=tosca_type), self.validate, defs
        )
        self.super_maps[
            (ctx._lastResource.key, tosca_type and tosca_type.global_name or "")
        ] = super_map
        return super_map

    def _reset(self):
        self.attributes = {}
        # self.statuses = {}
        self.tracker = _Tracker()

    # def revertChanges(self):
    #   self.attributes = {}
    #   # for resource, old, new in self.statuses.values():
    #   #   resource._localStatus = old

    @staticmethod
    def _save_sensitive(defs, key: str, value: Result):
        # attribute marked as sensitive and value isn't a secret so mark value as sensitive
        # but externalvalues are ok since they don't reveal much
        sensitive = is_sensitive_schema(defs, key) and not value.external
        if sensitive:
            savedValue = wrap_sensitive_value(value.resolved)
        else:
            savedValue = value.as_ref()  # serialize Result
        return savedValue

    @staticmethod
    def _check_attribute(specd, key: str, value: ResultsItem, instance):
        changed = value.has_diff()
        live = (
            changed  # modified by this task
            # explicitly declared an attribute:
            or key in instance.template.attributeDefs
            or key not in specd  # not declared as a property
            or key in instance._attributes  # previously modified
        )
        return changed, live, value.get_before_set()

    def find_live_dependencies(self) -> Dict["InstanceKey", List["Dependency"]]:
        from .configurator import Dependency

        dependencies: Dict[InstanceKey, List["Dependency"]] = {}
        for resource, attributes in self.attributes.values():
            overrides, specd = cast(ChainMap, attributes._attributes).split()
            # items in overrides with type ResultsItem imply they have been accessed during this transaction
            for key, value in overrides.items():
                if isinstance(value, ResultsItem):
                    changed, isLive, accessed = self._check_attribute(
                        specd, key, value, resource
                    )
                    if isLive:
                        dep = Dependency(
                            resource.key + "::" + key, value, target=resource
                        )
                        dependencies.setdefault(resource.key, []).append(dep)
        return dependencies

    def commit_changes(self) -> Tuple[AttributesChanges, _Dependencies]:
        changes: AttributesChanges = cast(AttributesChanges, {})
        _dependencies = cast(_Dependencies, {})
        for resource, attributes in list(self.attributes.values()):
            overrides, specd = cast(ChainMap, attributes._attributes).split()
            # overrides will only contain:
            #  - properties accessed or added while running a task
            #  - properties loaded from the ensemble status yaml (which implies it was previously added or changed)
            _attributes = {}
            defs = resource.template and resource.template.propertyDefs or {}
            foundSensitive = []
            touched: Dict[str, Any] = {}
            # items in overrides of type ResultsItem have been accessed during this transaction
            for key, value in list(overrides.items()):
                if not isinstance(value, ResultsItem):
                    # hasn't been accessed so keep it as is
                    _attributes[key] = value
                else:
                    changed, isLive, accessed = self._check_attribute(
                        specd, key, value, resource
                    )
                    savedValue = self._save_sensitive(defs, key, value)
                    is_sensitive = isinstance(savedValue, sensitive)
                    # save the ResultsItem not savedValue because we need the ExternalValue
                    touched[key] = (
                        isLive,
                        savedValue if is_sensitive else value,
                        accessed,
                    )
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

            if touched:
                _dependencies[resource.key] = (resource, touched)
            # save changes
            diff = attributes.get_diff()
            if not diff:
                continue
            for key in foundSensitive:
                if key in diff:
                    diff[key] = _attributes[key]
            changes[resource.key] = diff

        self._reset()
        return changes, _dependencies
