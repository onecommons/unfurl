# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Internal classes supporting the runtime.
"""
import collections
from collections.abc import MutableSequence, Mapping
import copy
import os
import sys
import os.path
import six
import re
import ast
import time
from typing import TYPE_CHECKING, cast, Dict, Optional, Any
from enum import Enum
from urllib.parse import urlsplit


from .eval import RefContext, set_eval_func, Ref, map_value
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
from jinja2.runtime import DebugUndefined, make_logging_undefined
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
    "sensitive",
    lambda arg, ctx: wrap_sensitive_value(map_value(arg, ctx)),
)


set_eval_func(
    "portspec",
    lambda arg, ctx: PortSpec.make(map_value(arg, ctx)),
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
    return isinstance(val, six.string_types) and not not _clean_regex.search(val)


class _VarTrackerDict(dict):
    def __getitem__(self, key):
        try:
            val = super().__getitem__(key)
        except KeyError:
            logger.debug('Missing variable "%s" in template', key)
            raise

        try:
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


def apply_template(value, ctx, overrides=None):
    if not isinstance(value, six.string_types):
        msg = f"Error rendering template: source must be a string, not {type(value)}"
        if ctx.strict:
            raise UnfurlError(msg)
        else:
            return f"<<{msg}>>"
    value = value.strip()
    if ctx.task:
        logger = ctx.task.logger
    else:
        logger = logging.getLogger("unfurl")

    # local class to bind with logger and ctx
    class _UnfurlUndefined(DebugUndefined):
        __slots__ = ()

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
            msg = "Template: %s", self._undefined_message
            # XXX override _undefined_message: if self._undefined_obj is a Results then add its ctx._lastResource to the msg
            #     see https://github.com/pallets/jinja/blob/7d72eb7fefb7dce065193967f31f805180508448/src/jinja2/runtime.py#L824
            logger.warning(msg)
            if ctx.task:  # already logged, so don't log
                ctx.task._errors.append(UnfurlTaskError(ctx.task, msg, False))

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
    if not fail_on_undefined:
        templar.environment.undefined = _UnfurlUndefined

    vars = _VarTrackerDict(
        __unfurl=ctx, __python_executable=sys.executable, __now=time.time()
    )
    if hasattr(ctx.currentResource, "attributes"):
        vars["SELF"] = ctx.currentResource.attributes
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
                    ctx.task._errors.append(UnfurlTaskError(ctx.task, msg))
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
        value = args
    return apply_template(value, ctx, ctx.kw.get("overrides"))


set_eval_func("template", _template_func)


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
    kwargs = {}
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


set_eval_func("concat", concat, True)


def token(args, ctx):
    args = map_value(args, ctx)
    return args[0].split(args[1])[args[2]]


set_eval_func("token", token, True)

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
    return arg in os.environ


set_eval_func("has_env", has_env, True)


def get_env(args, ctx: RefContext):
    """
    Return the value of the given environment variable name.
    If NAME is not present in the environment, return the given default value if supplied or return None.

    e.g. {get_env: NAME} or {get_env: [NAME, default]}

    If the value of its argument is empty (e.g. [] or null), return the entire dictionary.
    """
    env = os.environ
    if not args:
        return env

    if isinstance(args, list):
        name = args[0]
        default = args[1] if len(args) > 1 else None
    else:
        name = args
        default = None

    return env.get(name, map_value(default, ctx))


set_eval_func("get_env", get_env, True)


def set_context_vars(vars, resource):
    root = resource.root
    ROOT = {}
    vars.update(dict(NODES=TopologyMap(root), ROOT=ROOT, TOPOLOGY=ROOT))
    if "inputs" in root._attributes:
        ROOT.update(
            dict(
                inputs=root._attributes["inputs"],
                outputs=root._attributes["outputs"],
            )
        )
    app_template = root.template.spec.substitution_template
    if app_template:
        app = root.find_instance(app_template.name)
        if app:
            ROOT["app"] = app.attributes
        for name, req in app_template.requirements.items():
            if req.relationship and req.relationship.target:
                target = root.find_instance(req.relationship.target.name)
                if target:
                    ROOT[name] = target.attributes
    return vars


class _EnvMapper(dict):
    """Resolve environment variable name to instance properties via the root template's requirements.
    Pattern should match _generate_env_names in to_json.py and set_context_vars above.
    """

    def __missing__(self, key):
        objname, sep, prop = key.partition("_")
        root = self.ctx.currentResource.root
        app = root.template.spec.substitution_template
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
    env = None
    if ctx.task:
        env = ctx.task.get_environment(False)
    sub = _EnvMapper(env or {})
    sub.ctx = ctx  # type: ignore

    rules = map_value(args or {}, ctx)
    assert isinstance(rules, Mapping)
    result = filter_env(rules, env, True, sub)
    if ctx.kw.get("update_os_environ"):
        os.environ.update(result)
        for key, value in rules.items():
            if value is None and key in os.environ:
                del os.environ[key]
    return result


set_eval_func("to_env", to_env)


def to_label(arg, extra="", max=63, lower=False, replace=""):
    if isinstance(arg, Mapping):
        return {
            to_label(n, extra, max, lower, replace): to_label(
                v, extra, max, lower, replace
            )
            for n, v in arg.items()
            if v is not None
        }
    elif isinstance(arg, str):
        val = re.sub(f"[^\\w{extra}]", replace, arg)
        if lower:
            val = val.lower()
        return val[:max]


def to_dns_label(arg):
    """
    The maximum length of each label is 63 characters, and a full domain name can have a maximum of 253 characters.
    Alphanumeric characters and hyphens can be used in labels, but a domain name must not commence or end with a hyphen.
    """
    return to_label(arg, extra="-", replace="--")


set_eval_func(
    "to_dns_label",
    lambda arg, ctx: to_dns_label(map_value(arg, ctx)),
)


def to_kubernetes_label(arg):
    """
    See https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#syntax-and-character-set
    """
    return to_label(arg, extra="_.-", replace="__")


set_eval_func(
    "to_kubernetes_label",
    lambda arg, ctx: to_kubernetes_label(map_value(arg, ctx)),
)


def to_googlecloud_label(arg):
    """
    See https://cloud.google.com/resource-manager/docs/creating-managing-labels#requirements
    """
    return to_label(arg, extra="_-", lower=True, replace="__")


set_eval_func(
    "to_googlecloud_label",
    lambda arg, ctx: to_googlecloud_label(map_value(arg, ctx)),
)

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


set_eval_func("get_attribute", get_attribute, True)


def get_nodes_of_type(type_name, ctx: RefContext):
    return [
        r
        for r in ctx.currentResource.root.get_self_and_descendents()
        if r.template.is_compatible_type(type_name)
        and r.name not in ["inputs", "outputs"]
    ]


set_eval_func("get_nodes_of_type", get_nodes_of_type, True)


set_eval_func("_generate", lambda arg, ctx: get_random_password(10, ""), True)


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
        tag=None,
        digest=None,
        registry_host=None,
        username=None,
        password=None,
        source_digest=None,
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

    @staticmethod
    def split(artifact_name):
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
            digest = None
            name, sep, qualifier = artifact_name.partition(":")
            if sep:
                tag = qualifier
        return name.lower(), tag, digest, hostname

    @staticmethod
    def make(artifact_name):
        return ContainerImage(*ContainerImage.split(artifact_name))

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


def _get_container_image_from_repository(entity, artifact_name):
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
        hostname = attr["registry_url"]
        if "//" in hostname:
            hostname = urlsplit(attr["registry_url"]).netloc
    username = attr.get("username")
    password = attr.get("password")
    source_digest = attr.get("revision")
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
        return ContainerImage.make(artifact_name)  # XXX assume its a container image
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
    if TYPE_CHECKING:
        from .runtime import EntityInstance

    def __init__(
        self,
        external_instance: "EntityInstance",
        spec: dict,
        local_instance: Optional["EntityInstance"] = None,
    ):
        self.external_instance = external_instance
        self.spec = spec
        self.local_instance = local_instance


class Imports(collections.OrderedDict):
    def find_import(self, qualified_name):
        # return a local shadow of the imported instance
        # or the imported instance itself if no local shadow exist (yet).
        imported = self._find_import(qualified_name)
        if imported:
            return imported
        iName, sep, rName = qualified_name.partition(":")
        localEnv = self.manifest.localEnv
        if iName not in self and localEnv:
            project = localEnv.project or localEnv.homeProject
            tpl = project and project.find_ensemble_by_name(iName)
            if tpl:
                self.manifest.load_external_ensemble(iName, dict(manifest=tpl))
                return self._find_import(qualified_name)
        return None

    def _find_import(self, name):
        if name in self:
            # fully qualified name already added
            return self[name].local_instance or self[name].external_instance
        iName, sep, rName = name.partition(":")
        if iName not in self:
            return None
        # do a unqualified look up to find the declared import
        imported = self[iName].external_instance.root.find_resource(rName or "root")
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
                    schemaErrors,
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

    def get_attribute_changes(self, key):
        record = self.get(key)
        if record:
            return record[self.attributesIndex]
        return {}

    def sync(self, resource, changeId=None):
        """Update self to only include changes that are still live"""
        for k, v in list(self.items()):
            current = Ref(k).resolve_one(RefContext(resource))
            if current:
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

    def add_changes(self, changes):
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
            assert not isinstance(change[1], six.string_types)
            old = self.get(name)
            if old:
                old[self.statusIndex] = change[1]
            else:
                self[name] = [change[1], None, {}]

    def add_resources(self, resources):
        for resource in resources:
            self["::" + resource["name"]] = [None, resource, None]

    def update_changes(self, changes, statuses, resource, changeId=None):
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

    def __getitem__(self, key):
        r = self.resource.find_resource(key)
        if r:
            return r.attributes
        else:
            raise KeyError(key)

    def __iter__(self):
        return iter(r.name for r in self.resource.get_self_and_descendents())

    def __len__(self):
        return len(tuple(self.resource.get_self_and_descendents()))


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

    # what about an attribute that is added to the spec that already exists in status?
    # XXX2 tests for the above behavior
    def __init__(self, yaml=None, task=None):
        self.attributes = {}
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
        for (resource, attr) in self.attributes.values():
            if (
                resource.template is not template
                and template not in resource.template._isReferencedBy
            ):
                resource.template._isReferencedBy.append(template)

    def _get_context(self, resource):
        if self._context_vars is None:
            self._context_vars = {}
            set_context_vars(self._context_vars, resource)
        return RefContext(resource, self._context_vars, task=self.task)

    def get_attributes(self, resource):
        if resource.key not in self.attributes:
            if resource.shadow:
                return resource.shadow.attributes

            # deepcopy() because lazily created ResultMaps and ResultLists will mutate
            # the underlying nested structures when resolving values
            if resource.template:
                specAttributes = resource.template.defaultAttributes
                _attributes = ChainMap(
                    copy.deepcopy(resource._attributes),
                    copy.deepcopy(resource.template.properties),
                    copy.deepcopy(specAttributes),
                )
            else:
                _attributes = ChainMap(copy.deepcopy(resource._attributes))
            ctx = self._get_context(resource)
            validate = True
            mode = os.getenv("UNFURL_VALIDATION_MODE")
            if mode is not None and "nopropcheck" in mode:
                validate = False
            attributes = ResultsMap(_attributes, ctx, validate=validate)
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

    def find_live_dependencies(self):
        liveDependencies = {}
        for resource, attributes in self.attributes.values():
            overrides, specd = attributes._attributes.split()
            live = []
            # items in overrides of type Result have been accessed during this transaction
            for key, value in overrides.items():
                if isinstance(value, Result):
                    changed, isLive = self._check_attribute(specd, key, value, resource)
                    if isLive:
                        live.append(key)

            if live:
                liveDependencies[resource.key] = (resource, live)

        return liveDependencies

    def commit_changes(self):
        changes = {}
        liveDependencies = {}
        for resource, attributes in list(self.attributes.values()):
            overrides, specd = attributes._attributes.split()
            # overrides will only contain:
            #  - properties accessed or added while running a task
            #  - properties loaded from the ensemble status yaml (which implies it was previously added or changed)
            _attributes = {}
            defs = resource.template and resource.template.propertyDefs or {}
            foundSensitive = []
            live = {}
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
