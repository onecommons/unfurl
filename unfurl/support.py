"""
Internal classes supporting the runtime.
"""
import collections
from collections import Mapping, MutableSequence
import copy
import os
import os.path
import six
from enum import IntEnum

from .eval import RefContext, setEvalFunc, Ref, mapValue
from .result import ResultsMap, Result, ExternalValue, serializeValue
from .util import (
    ChainMap,
    findSchemaErrors,
    UnfurlError,
    UnfurlValidationError,
    assertForm,
    sensitive_str,
    saveToTempfile,
)
from .merge import intersectDict, mergeDicts
from ansible.template import Templar
from ansible.parsing.dataloader import DataLoader

import logging

logger = logging.getLogger("unfurl")

Status = IntEnum(
    "Status", "unknown ok degraded error pending notpresent", start=0, module=__name__
)

# see "3.4.1 Node States" p74
NodeState = IntEnum(
    "NodeState",
    "initial creating created configuring configured starting started stopping deleting error",
    module=__name__,
)

# ignore may must
Priority = IntEnum("Priority", "ignore optional required critical", start=0, module=__name__)


class Defaults(object):
    shouldRun = Priority.required
    workflow = "deploy"


class File(ExternalValue):
    """
  Represents a local file.
  get() returns the given file path (usually relative)
  """

    def __init__(self, name, baseDir=""):
        super(File, self).__init__("file", name)
        self.baseDir = baseDir or ""

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


def _getTemplateTestRegEx():
    return Templar(DataLoader())._clean_regex


_clean_regex = _getTemplateTestRegEx()


def isTemplate(val, ctx):
    return isinstance(val, six.string_types) and not not _clean_regex.search(val)


class _VarTrackerDict(dict):
    def __getitem__(self, key):
        val = super(_VarTrackerDict, self).__getitem__(key)
        try:
            return self.ctx.resolveReference(key)
        except KeyError:
            return val


def applyTemplate(value, ctx):
    if not isinstance(value, six.string_types):
        msg = "Error rendering template: source must be a string, not %s" % type(value)
        if ctx.strict:
            raise UnfurlError(msg)
        else:
            return "<<%s>>" % msg

    # implementation notes:
    #   see https://github.com/ansible/ansible/test/units/template/test_templar.py
    #   dataLoader is only used by _lookup and to set _basedir (else ./)
    if ctx.baseDir and ctx.templar._basedir != ctx.baseDir:
        # we need to create a new templar
        loader = DataLoader()
        if ctx.baseDir:
            loader.set_basedir(ctx.baseDir)
        templar = Templar(loader)
        ctx.templar = templar

    ctx.templar.environment.trim_blocks = False
    # ctx.templar.environment.lstrip_blocks = False
    fail_on_undefined = True

    vars = _VarTrackerDict(__unfurl=ctx)
    vars.update(ctx.vars)
    vars.ctx = ctx

    # replaces current vars
    # don't use setter to avoid isinstance(dict) check
    ctx.templar._available_variables = vars

    oldvalue = value
    index = ctx.referenced.start()
    # set referenced to track references (set by Ref.resolve)
    # need a way to turn on and off
    try:
        # strip whitespace so jinija native types resolve even with extra whitespace
        # disable caching so we don't need to worry about the value of a cached var changing
        try:
            value = ctx.templar.template(
                value.strip(), cache=False, fail_on_undefined=fail_on_undefined
            )
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
    finally:
        ctx.referenced.stop()
    return value


setEvalFunc("template", applyTemplate)


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
        assertForm(arg, list)
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


class Imports(collections.OrderedDict):
    Import = collections.namedtuple("Import", ["resource", "spec"])

    def findImport(self, name):
        if name in self:
            return self[name].resource
        iName, sep, rName = name.partition(":")
        if iName not in self:
            return None
        imported = self[iName].resource.findResource(rName or "root")
        if imported:
            # add for future reference
            self[name] = imported
        return imported

    def setShadow(self, key, instance):
        instance.shadow = self[key].resource
        self[key] = instance

    def __setitem__(self, key, value):
        if isinstance(value, tuple):
            value = self.Import(*value)
        else:
            if key in self:
                value = self[key]._replace(resource=value)
            else:
                value = self.Import(value, {})
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
      .status: # set when status changes, including when removed (Status.notpresent)
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
