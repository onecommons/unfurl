"""
Internal classes supporting the runtime.
"""
import collections
from collections import Mapping, MutableSequence
import copy
import os
import os.path
import six
import re
import ast
from enum import IntEnum

from .eval import RefContext, setEvalFunc, Ref, mapValue
from .result import Results, ResultsMap, Result, ExternalValue, serializeValue
from .util import (
    ChainMap,
    findSchemaErrors,
    UnfurlError,
    UnfurlValidationError,
    assertForm,
    sensitive_str,
    saveToTempfile,
    saveToFile,
    filterEnv,
)
from .merge import intersectDict, mergeDicts
import ansible.template
from ansible.parsing.dataloader import DataLoader
from ansible.utils.unsafe_proxy import wrap_var, AnsibleUnsafeText, AnsibleUnsafeBytes

import logging

logger = logging.getLogger("unfurl")

Status = IntEnum(
    "Status", "unknown ok degraded error pending absent", start=0, module=__name__
)

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


class Defaults(object):
    shouldRun = Priority.required
    workflow = "deploy"


class File(ExternalValue):
    """
  Represents a local file.
  get() returns the given file path (usually relative)
  """

    def __init__(self, arg, baseDir=""):
        write = False
        if isinstance(arg, dict):
            name = arg["path"]
            write = True
        else:
            name = arg

        super(File, self).__init__("file", name)
        self.baseDir = baseDir or ""
        if write:
            saveToFile(self.getFullPath(), arg["contents"])

    def getFullPath(self):
        return os.path.abspath(os.path.join(self.baseDir, self.get()))

    def resolveKey(self, name=None, currentResource=None):
        """
    path # absolute path
    contents # file contents (None if it doesn't exist)
    """
        if not name:
            return self.get()

        if name == "path":
            return self.getFullPath()
        elif name == "contents":
            with open(self.getFullPath(), "r") as f:
                return f.read()
        else:
            raise KeyError(name)


setEvalFunc("file", lambda arg, ctx: File(mapValue(arg, ctx), ctx.baseDir))


class TempFile(ExternalValue):
    """
  Represents a local file.
  get() returns the given file path (usually relative)
  """

    def __init__(self, obj, suffix=""):
        tp = saveToTempfile(obj, suffix)
        super(TempFile, self).__init__("tempfile", tp.name)
        self.tp = tp

    def resolveKey(self, name=None, currentResource=None):
        """
    path # absolute path
    contents # file contents (None if it doesn't exist)
    """
        if not name:
            return self.get()

        if name == "path":
            return self.tp.name
        elif name == "contents":
            with open(self.tp.name, "r") as f:
                return f.read()
        else:
            raise KeyError(name)


setEvalFunc(
    "tempfile", lambda arg, ctx: TempFile(mapValue(arg, ctx), ctx.kw.get("suffix", ""))
)

# see abspath in filter_plugins.ref
setEvalFunc(
    "abspath",
    lambda arg, ctx: os.path.abspath(
        os.path.join(ctx.currentResource.baseDir, mapValue(arg, ctx))
    ),
)

# XXX need an api check if an object was marked sensitive
# _secrets = weakref.WeakValueDictionary()
# def addSecret(secret):
#   _secrets[id(secret.get())] = secret
# def isSecret(obj):
#    return id(obj) in _secrets


class SensitiveValue(ExternalValue):
    def __init__(self, value):
        super(SensitiveValue, self).__init__("sensitive", value)

    def asRef(self, options=None):
        return sensitive_str(self.get())


setEvalFunc("sensitive", lambda arg, ctx: SensitiveValue(mapValue(arg, ctx)))


def isSensitive(obj):
    return isinstance(obj, (sensitive_str, SensitiveValue, SecretResource))


class Templar(ansible.template.Templar):
    def template(self, variable, **kw):
        if isinstance(variable, Results):
            # template() will eagerly evaluate template strings in lists and dicts
            # defeating the lazy evaluation ResultsMap and ResultsList is intending to provide
            return variable
        return super(Templar, self).template(variable, **kw)

    @staticmethod
    def findOverrides(data, overrides=None):
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

    def _applyTemplarOverrides(self, overrides):
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


def _getTemplateTestRegEx():
    return Templar(DataLoader())._clean_regex


_clean_regex = _getTemplateTestRegEx()


def isTemplate(val, ctx):
    if isinstance(val, (AnsibleUnsafeText, AnsibleUnsafeBytes)):
        # already evaluated in a template, don't evaluate again
        return False
    return isinstance(val, six.string_types) and not not _clean_regex.search(val)


class _VarTrackerDict(dict):
    def __getitem__(self, key):
        try:
            val = super(_VarTrackerDict, self).__getitem__(key)
        except KeyError:
            logger.debug('Missing variable "%s" in template', key)
            raise

        try:
            return self.ctx.resolveReference(key)
        except KeyError:
            return val


def applyTemplate(value, ctx, overrides=None):
    if not isinstance(value, six.string_types):
        msg = "Error rendering template: source must be a string, not %s" % type(value)
        if ctx.strict:
            raise UnfurlError(msg)
        else:
            return "<<%s>>" % msg
    value = value.strip()

    # implementation notes:
    #   see https://github.com/ansible/ansible/test/units/template/test_templar.py
    #   dataLoader is only used by _lookup and to set _basedir (else ./)
    if not ctx.templar or (ctx.baseDir and ctx.templar._basedir != ctx.baseDir):
        # we need to create a new templar
        loader = DataLoader()
        if ctx.baseDir:
            loader.set_basedir(ctx.baseDir)
        templar = Templar(loader)
        ctx.templar = templar
    else:
        templar = ctx.templar

    overrides = Templar.findOverrides(value, overrides)
    if overrides:
        # returns the original values
        overrides = templar._applyTemplarOverrides(overrides)

    templar.environment.trim_blocks = False
    # templar.environment.lstrip_blocks = False
    fail_on_undefined = True

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
            value = "<<Error rendering template: %s>>" % str(e)
            if ctx.strict:
                raise UnfurlError(value)
            else:
                logger.warning(value[2:100] + "... see debug log for full report")
                logger.debug(value, exc_info=True)
        else:
            if value != oldvalue:
                ctx.trace("successfully processed template:", value)
                for result in ctx.referenced.getReferencedResults(index):
                    if isSensitive(result.external or result.resolved):
                        ctx.trace("setting template result as sensitive")
                        return SensitiveValue(
                            value
                        )  # mark the template result as sensitive
                # otherwise wrap result as AnsibleUnsafeText so it isn't evaluated again
                return wrap_var(value)
    finally:
        ctx.referenced.stop()
        if overrides:
            # restore original values
            templar._applyTemplarOverrides(overrides)
    return value


setEvalFunc(
    "template", lambda args, ctx: (applyTemplate(args, ctx, ctx.kw.get("overrides")))
)


def runLookup(name, templar, *args, **kw):
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


def lookupFunc(arg, ctx):
    """
  Runs an ansible lookup plugin. Usage:

  lookup:
     lookupFunctionName: 'arg' or ['arg1', 'arg2']

  Or if you need to pass keyword arguments to the lookup function:

  lookup:
    - lookupFunctionName: 'arg' or ['arg1', 'arg2']
    - kw1: value
    - kw2: value
  """
    arg = mapValue(arg, ctx)
    if isinstance(arg, Mapping):
        assert len(arg) == 1
        name, args = list(arg.items())[0]
        kw = {}
    else:
        assertForm(arg, MutableSequence)
        name, args = list(assertForm(arg[0]).items())[0]
        kw = dict(list(assertForm(kw).items())[0] for kw in arg[1:])

    if not isinstance(args, MutableSequence):
        args = [args]
    return runLookup(name, ctx.templar, *args, **kw)


setEvalFunc("lookup", lookupFunc)


def getInput(arg, ctx):
    try:
        return ctx.currentResource.root.findResource("inputs").attributes[arg]
    except KeyError:
        raise UnfurlError("undefined input '%s'" % arg)


setEvalFunc("get_input", getInput, True)


def concat(args, ctx):
    return "".join([str(a) for a in mapValue(args, ctx)])


setEvalFunc("concat", concat, True)


def token(args, ctx):
    args = mapValue(args, ctx)
    return args[0].split(args[1])[args[2]]


setEvalFunc("token", token, True)

# XXX this doesn't work with node_filters, need an instance to get a specific result
def getToscaProperty(args, ctx):
    from toscaparser.functions import get_function

    tosca_tpl = ctx.currentResource.root.template.toscaEntityTemplate
    node_template = ctx.currentResource.template.toscaEntityTemplate
    return get_function(tosca_tpl, node_template, {"get_property": args}).result()


setEvalFunc("get_property", getToscaProperty, True)


def hasEnv(arg, ctx):
    """
    {has_env: foo}
    """
    if not ctx.currentResource.root.envRules:
        return False
    return arg in filterEnv(ctx.currentResource.root.envRules)


setEvalFunc("has_env", hasEnv, True)


def getEnv(args, ctx):
    """
    Return the value of the given environment variable name.
    If not name is missing, return the given default value if supplied or return None.

    e.g. {get_env: NAME} or {get_env: [NAME, default]}

    If the value of its argument is empty (e.g. [] or null), return the entire dictionary.
    """
    env = filterEnv(ctx.currentResource.root.envRules or {})
    if not args:
        return env

    if isinstance(args, list):
        name = args[0]
        default = args[1]
    else:
        name = args
        default = None

    return env.get(name, default)


setEvalFunc("get_env", getEnv, True)


def get_artifact(ctx, entity_name, artifact_name, location=None, remove=None):
    """
    Returns either an URL or local path to the artifact
    See section "4.8.1 get_artifact" in TOSCA 1.3 (p. 189)

    If the artifact is a Docker image, return the image name in the form of
    "registry/repository/name:tag" or "registry/repository/name@sha256:digest"

    If entity_name or artifact_name is not found return None.
    """
    # XXX if HOST search all parents
    keywords = ["SELF", "HOST", "SOURCE", "TARGET", "OPERATION_HOST", "ORCHESTRATOR"]
    if entity_name in keywords:
        if entity_name in ctx.vars:
            instance = ctx.vars[entity_name].context.currentResource
        else:
            instance = None
    else:
        instance = ctx.currentResource.root.findResource(entity_name)

    if not instance:
        ctx.trace("entity_name not found", entity_name)
        return None
    else:
        # XXX implement instance.artifacts
        artifact = instance.template.artifacts.get(artifact_name)
        if not artifact:
            ctx.trace("artifact not found", artifact_name)
            return None

    artifactDef = artifact.artifact
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


setEvalFunc("get_artifact", lambda args, ctx: get_artifact(ctx, *args), True)


def getImport(arg, ctx):
    """
  Returns the external resource associated with the named import
  """
    try:
        imported = ctx.currentResource.root.imports[arg]
    except KeyError:
        raise UnfurlError("Can't find import '%s'" % arg)
    if arg == "secret":
        return SecretResource(arg, imported)
    else:
        return ExternalResource(arg, imported)


setEvalFunc("external", getImport)


_Import = collections.namedtuple("_Import", ["resource", "spec"])


class Imports(collections.OrderedDict):
    def findImport(self, name):
        if name in self:
            return self[name].resource
        iName, sep, rName = name.partition(":")
        if iName not in self:
            return None
        imported = self[iName].resource.findResource(rName or "root")
        if imported:
            # add for future reference
            self[name] = imported  # see __setitem__
        return imported

    def setShadow(self, key, instance):
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
        return super(Imports, self).__setitem__(key, value)


class ExternalResource(ExternalValue):
    """
  Wraps a foreign resource
  """

    def __init__(self, name, importSpec):
        super(ExternalResource, self).__init__("external", name)
        self.resource = importSpec.resource
        self.schema = importSpec.spec.get("properties")

    def _validate(self, obj, schema, name):
        if schema:
            messages = findSchemaErrors(serializeValue(obj), schema)
            if messages:
                (message, schemaErrors) = messages
                raise UnfurlValidationError(
                    "schema validation failed for attribute '%s': %s"
                    % (name, schemaErrors),
                    schemaErrors,
                )

    def _getSchema(self, name):
        return self.schema and self.schema.get(name, {})

    def get(self):
        return self.resource

    def resolveKey(self, name=None, currentResource=None):
        if not name:
            return self.resource

        schema = self._getSchema(name)
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
    def resolveKey(self, name=None, currentResource=None):
        # raises KeyError if not found
        return super(SecretResource, self).resolveKey(name, currentResource)


# shortcuts for local and secret
def shortcut(arg, ctx):
    return Ref(dict(ref=dict(external=ctx.currentFunc), foreach=arg)).resolve(
        ctx, wantList="result"
    )


setEvalFunc("local", shortcut)
setEvalFunc("secret", shortcut)


class DelegateAttributes(object):
    def __init__(self, interface, resource):
        self.interface = interface
        self.resource = resource
        if interface == "inherit":
            self.inheritFrom = resource.attributes["inheritFrom"]
        if interface == "default":
            self.default = resource.attributes["default"]

    def __call__(self, key):
        if self.interface == "inherit":
            return self.inheritFrom.attributes[key]
        elif self.interface == "default":
            result = Ref(self.default).resolve(
                RefContext(self.resource, vars=dict(key=key))
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

    def getAttributeChanges(self, key):
        record = self.get(key)
        if record:
            return record[self.attributesIndex]
        return {}

    def sync(self, resource, changeId=None):
        """ Update self to only include changes that are still live"""
        for k, v in list(self.items()):
            current = Ref(k).resolveOne(RefContext(resource))
            if current:
                attributes = v[self.attributesIndex]
                if attributes:
                    v[self.attributesIndex] = intersectDict(
                        attributes, current._attributes
                    )
                if v[self.statusIndex] != current._localStatus:
                    v[self.statusIndex] = None

                if changeId and (v[0] or v[1] or v[2]):
                    current._lastConfigChange = changeId
            else:
                del self[k]

    def addChanges(self, changes):
        for name, change in changes.items():
            old = self.get(name)
            if old:
                old[self.attributesIndex] = mergeDicts(
                    old[self.attributesIndex], change
                )
            else:
                self[name] = [None, None, change]

    def addStatuses(self, changes):
        for name, change in changes.items():
            assert not isinstance(change[1], six.string_types)
            old = self.get(name)
            if old:
                old[self.statusIndex] = change[1]
            else:
                self[name] = [change[1], None, {}]

    def addResources(self, resources):
        for resource in resources:
            self["::" + resource["name"]] = [None, resource, None]

    def updateChanges(self, changes, statuses, resource, changeId=None):
        self.addChanges(changes)
        self.addStatuses(statuses)
        if resource:
            self.sync(resource, changeId)

    def rollback(self, resource):
        # XXX need to actually rollback
        self.clear()


class AttributeManager(object):
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
    def __init__(self):
        self.attributes = {}
        self.statuses = {}

    def getStatus(self, resource):
        if resource.key not in self.statuses:
            return resource._localStatus, resource._localStatus
        return self.statuses[resource.key]

    def setStatus(self, resource, newvalue):
        assert newvalue is None or isinstance(newvalue, Status)
        if resource.key not in self.statuses:
            self.statuses[resource.key] = [resource._localStatus, newvalue]
        else:
            self.statuses[resource.key][1] = newvalue

    def getAttributes(self, resource):
        if resource.key not in self.attributes:
            if resource.template:
                specd = resource.template.properties
                defaultAttributes = resource.template.defaultAttributes
                _attributes = ChainMap(
                    copy.deepcopy(resource._attributes), specd, defaultAttributes
                )
            else:
                _attributes = ChainMap(copy.deepcopy(resource._attributes))

            attributes = ResultsMap(_attributes, RefContext(resource))
            self.attributes[resource.key] = (resource, attributes)
            return attributes
        else:
            return self.attributes[resource.key][1]

    # def revertChanges(self):
    #   self.attributes = {}
    #   # for resource, old, new in self.statuses.values():
    #   #   resource._localStatus = old

    def commitChanges(self):
        changes = {}
        for resource, attributes in self.attributes.values():
            # save in _attributes in serialized form
            overrides, specd = attributes._attributes.split()
            resource._attributes = {}
            defs = resource.template and resource.template.attributeDefs or {}
            foundSensitive = []
            for key, value in overrides.items():
                if not isinstance(value, Result):
                    # hasn't been touched so keep it as is
                    resource._attributes[key] = value
                elif key not in specd or value.hasDiff():
                    # value is a Result and it is either new or different from the original value, so save
                    defSchema = (key in defs and defs[key].schema) or {}
                    defMeta = defSchema.get("metadata", {})

                    # XXX if defMeta.get('immutable') and key in specd:
                    #  error('value of attribute "%s" changed but is marked immutable' % key)

                    if defMeta.get("sensitive"):
                        # attribute marked as sensitive and value isn't a secret so mark value as sensitive
                        if (
                            not value.external
                        ):  # externalvalues are ok since they don't reveal much
                            # we won't be able to restore this value since we can't save it
                            resource._attributes[key] = sensitive_str(value.resolved)
                            foundSensitive.append(key)
                            continue
                    resource._attributes[key] = value.asRef()  # serialize Result

            # save changes
            diff = attributes.getDiff()
            if not diff:
                continue
            for key in foundSensitive:
                if key in diff:
                    diff[key] = resource._attributes[key]
            changes[resource.key] = diff

        self.attributes = {}
        # self.statuses = {}
        return changes
