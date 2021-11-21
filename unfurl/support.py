# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Internal classes supporting the runtime.
"""
import collections
from collections.abc import MutableSequence, Mapping
import copy
import os
import os.path
import six
import re
import ast
from enum import IntEnum
from .eval import RefContext, set_eval_func, Ref, map_value
from .result import (
    Results,
    ResultsMap,
    ResultsList,
    Result,
    ExternalValue,
    serialize_value,
)
from .util import (
    ChainMap,
    find_schema_errors,
    UnfurlError,
    UnfurlValidationError,
    assert_form,
    wrap_sensitive_value,
    is_sensitive,
    load_module,
    load_class,
    sensitive,
)
from .merge import intersect_dict, merge_dicts
from unfurl.projectpaths import get_path
import ansible.template
from ansible.parsing.dataloader import DataLoader
from ansible.utils import unsafe_proxy
from ansible.utils.unsafe_proxy import wrap_var, AnsibleUnsafeText, AnsibleUnsafeBytes

import logging

logger = logging.getLogger("unfurl")


class Status(IntEnum):
    unknown = 0
    ok = 1
    degraded = 2
    error = 3
    pending = 4
    absent = 5


# see "3.4.1 Node States" p74
NodeState = IntEnum(
    "NodeState",
    "initial creating created configuring configured starting started stopping stopped deleting deleted error",
    module=__name__,
)

# ignore may must
Priority = IntEnum(
    "Priority", "ignore optional required critical", start=0, module=__name__
)


class Reason:
    pass


for r in "add reconfigure force upgrade update missing error degraded prune".split():
    setattr(Reason, r, r)


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


def is_template(val, ctx):
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

    vars = _VarTrackerDict(__unfurl=ctx)
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
            match = re.search(r"has no attribute '(\w+)'", msg)
            if match:
                msg = f'missing attribute or key: "{match.group(1)}"'
            value = f"<<Error rendering template: {msg}>>"
            if ctx.strict:
                logger.debug(value, exc_info=True)
                raise UnfurlError(value)
            else:
                logger.warning(value[2:100] + "... see debug log for full report")
                logger.debug(value, exc_info=True)
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
    try:
        return ctx.currentResource.root.attributes["inputs"][arg]
    except KeyError:
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


def get_env(args, ctx):
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

    return env.get(name, default)


set_eval_func("get_env", get_env, True)

_toscaKeywordsToExpr = {
    "SELF": ".",
    "SOURCE": ".source",
    "TARGET": ".target",
    "ORCHESTRATOR": "::localhost",
    "HOST": ".parents",
    "OPERATION_HOST": "$OPERATION_HOST",
}


def get_attribute(args, ctx):
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


def get_nodes_of_type(type_name, ctx):
    return [
        r
        for r in ctx.currentResource.root.get_self_and_descendents()
        if r.template.is_compatible_type(type_name)
        and r.name not in ["inputs", "outputs"]
    ]


set_eval_func("get_nodes_of_type", get_nodes_of_type, True)


def get_artifact(ctx, entity_name, artifact_name, location=None, remove=None):
    """
    Returns either an URL or local path to the artifact
    See section "4.8.1 get_artifact" in TOSCA 1.3 (p. 189)

    If the artifact is a Docker image, return the image name in the form of
    "registry/repository/name:tag" or "registry/repository/name@sha256:digest"

    If entity_name or artifact_name is not found return None.
    """
    ctx = ctx.copy(ctx._lastResource)
    query = _toscaKeywordsToExpr.get(entity_name, "::" + entity_name)
    instances = ctx.query(query, wantList=True)

    if not instances:
        ctx.trace("entity_name not found", entity_name)
        return None
    else:
        for instance in instances:
            # XXX implement instance.artifacts
            artifact = instance.template.artifacts.get(artifact_name)
            if artifact:
                break
        else:
            ctx.trace("artifact not found", artifact_name)
            return None

    artifactDef = artifact.toscaEntityTemplate
    if artifactDef.is_derived_from("tosca.artifacts.Deployment.Image.Container.Docker"):
        # if artifact is an image in a registry return image path and location isn't specified
        if artifactDef.checksum:
            name = (
                artifactDef.file
                + "@"
                + artifactDef.checksum_algorithm
                + ":"
                + artifactDef.checksum
            )
        elif "tag" in artifact.properties:
            name = artifactDef.file + ":" + artifact.properties["tag"]
        else:
            name = artifactDef.file
        if artifact.repository:
            return artifact.repository.hostname + "/" + name
        return name
    raise UnfurlError("get_artifact not implemented")
    # XXX
    # if not location:
    #     #if  deploy_path # update artifact instance
    #     return artifact.getUrl()
    # else:
    #     # if location is set, update the artifact instance's copies: and remove attributes if necessary
    #     # if location == 'LOCAL_FILE':


set_eval_func("get_artifact", lambda args, ctx: get_artifact(ctx, *args), True)


def get_import(arg, ctx):
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


_Import = collections.namedtuple("_Import", ["resource", "spec"])


class Imports(collections.OrderedDict):
    def find_import(self, name):
        if name in self:
            return self[name].resource
        iName, sep, rName = name.partition(":")
        if iName not in self:
            return None
        imported = self[iName].resource.find_resource(rName or "root")
        if imported:
            # add for future reference
            self[name] = imported  # see __setitem__
        return imported

    def set_shadow(self, key, instance):
        instance.shadow = self[key].resource
        self[key] = instance

    def __setitem__(self, key, value):
        if isinstance(value, tuple):
            value = _Import(*value)
        else:
            if key in self:
                value = self[key]._replace(resource=value)
            else:
                value = _Import(value, {})
        return super().__setitem__(key, value)


class ExternalResource(ExternalValue):
    """
    Wraps a foreign resource
    """

    def __init__(self, name, importSpec):
        super().__init__("external", name)
        self.resource = importSpec.resource
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
    return Ref(dict(ref=dict(external=ctx.currentFunc), foreach=arg)).resolve(
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


def _is_sensitive_schema(defs, key, value):
    defSchema = (key in defs and defs[key].schema) or {}
    defMeta = defSchema.get("metadata", {})
    # attribute marked as sensitive and value isn't a secret so mark value as sensitive
    # but externalvalues are ok since they don't reveal much
    return defMeta.get("sensitive") and not value.external


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
    def __init__(self, yaml=None):
        self.attributes = {}
        self.statuses = {}
        self._yaml = yaml  # hack to safely expose the yaml context

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

    def get_attributes(self, resource):
        if resource.key not in self.attributes:
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

            vars = dict(NODES=TopologyMap(resource.root))
            if "inputs" in resource.root._attributes:
                vars["TOPOLOGY"] = dict(
                    inputs=resource.root._attributes["inputs"],
                    outputs=resource.root._attributes["outputs"],
                )
            ctx = RefContext(resource, vars)
            attributes = ResultsMap(_attributes, ctx)
            self.attributes[resource.key] = (resource, attributes)
            return attributes
        else:
            return self.attributes[resource.key][1]

    # def revertChanges(self):
    #   self.attributes = {}
    #   # for resource, old, new in self.statuses.values():
    #   #   resource._localStatus = old

    @staticmethod
    def _save_sensitive(defs, key, value, instance):
        sensitive = _is_sensitive_schema(defs, key, value)
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
        for resource, attributes in self.attributes.values():
            overrides, specd = attributes._attributes.split()
            # overrides will only contain:
            #  - properties accessed or added while running a task
            #  - properties loaded from the ensemble status yaml (which implies it was previously added or changed)
            _attributes = {}
            defs = resource.template and resource.template.propertyDefs or {}
            foundSensitive = []
            live = {}
            # items in overrides of type Result have been accessed during this transaction
            for key, value in overrides.items():
                if not isinstance(value, Result):
                    # hasn't been accessed so keep it as is
                    _attributes[key] = value
                else:
                    changed, isLive = self._check_attribute(specd, key, value, resource)
                    savedValue = self._save_sensitive(defs, key, value, resource)
                    is_sensitive = isinstance(savedValue, sensitive)
                    # save the Result not savedValue because we need the ExternalValue
                    live[key] = (isLive, savedValue if is_sensitive else value)
                    if not isLive:
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
                    diff[key] = resource._attributes[key]
            changes[resource.key] = diff

        self.attributes = {}
        # self.statuses = {}
        return changes, liveDependencies
