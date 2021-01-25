# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Internal classes supporting the runtime.
"""
import collections
from collections import MutableSequence
import copy
import os
import os.path
import six
import re
import ast
import codecs
from enum import IntEnum

from .eval import RefContext, setEvalFunc, Ref, mapValue
from .result import Results, ResultsMap, Result, ExternalValue, serializeValue
from .util import (
    ChainMap,
    findSchemaErrors,
    UnfurlError,
    UnfurlValidationError,
    assertForm,
    wrapSensitiveValue,
    saveToTempfile,
    saveToFile,
    isSensitive,
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


def _mapArgs(args, ctx):
    args = mapValue(args, ctx)
    if not isinstance(args, MutableSequence):
        return [args]
    else:
        return args


class File(ExternalValue):
    """
    Represents a local file.
    get() returns the given file path (usually relative)
    `encoding` can be "binary", "vault", "json", "yaml" or an encoding registered with the Python codec registry
    """

    def __init__(self, name, baseDir="", loader=None, yaml=None, encoding=None):
        super(File, self).__init__("file", name)
        self.baseDir = baseDir or ""
        self.loader = loader
        self.yaml = yaml
        self.encoding = encoding

    def write(self, obj):
        encoding = self.encoding if self.encoding != "binary" else None
        path = self.getFullPath()
        logger.debug("writing to %s", path)
        saveToFile(path, obj, self.yaml, encoding)

    def getFullPath(self):
        return os.path.abspath(os.path.join(self.baseDir, self.get()))

    def getContents(self):
        path = self.getFullPath()
        with open(path, "rb") as f:
            contents = f.read()
        if self.loader:
            contents, show = self.loader._decrypt_if_vault_data(contents, path)
        else:
            show = True
        if self.encoding != "binary":
            try:
                # convert from bytes to string
                contents = codecs.decode(contents, self.encoding or "utf-8")
            except ValueError:
                pass  # keep at bytes
        if not show:  # it was encrypted
            return wrapSensitiveValue(contents)
        else:
            return contents

    def resolveKey(self, name=None, currentResource=None):
        """
        Key can be one of:

        path # absolute path
        contents # file contents (None if it doesn't exist)
        encoding
        """
        if not name:
            return self.get()

        if name == "path":
            return self.getFullPath()
        elif name == "encoding":
            return self.encoding or "utf-8"
        elif name == "contents":
            return self.getContents()
        else:
            raise KeyError(name)


def writeFile(ctx, obj, path, relativeTo=None, encoding=None):
    file = File(
        abspath(ctx, path, relativeTo),
        ctx.baseDir,
        ctx.templar and ctx.templar._loader,
        ctx.currentResource.root.attributeManager.yaml,
        encoding,
    )
    file.write(obj)
    return file.getFullPath()


def _fileFunc(arg, ctx):
    kw = mapValue(ctx.kw, ctx)
    file = File(
        mapValue(arg, ctx),
        ctx.baseDir,
        ctx.templar and ctx.templar._loader,
        ctx.currentResource.root.attributeManager.yaml,
        kw.get("encoding"),
    )
    if "contents" in kw:
        file.write(kw["contents"])
    return file


setEvalFunc("file", _fileFunc)


class TempFile(ExternalValue):
    """
    Represents a temporary local file.
    get() returns the given file path (usually relative)
    """

    def __init__(self, obj, suffix="", yaml=None, encoding=None):
        tp = saveToTempfile(obj, suffix, yaml=yaml, encoding=encoding)
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
    "tempfile",
    lambda arg, ctx: TempFile(
        mapValue(mapValue(arg, ctx), ctx),  # XXX
        ctx.kw.get("suffix"),
        ctx.currentResource.root.attributeManager.yaml,
        ctx.kw.get("encoding"),
    ),
)


def _getbaseDir(ctx, name=None):
    """
    Returns an absolute path based on the given folder name:

    ".":   directory that contains the current instance's the ensemble
    "src": directory of the source file this expression appears in
    "home" The "home" directory for the current instance (committed to repository)
    "local": The "local" directory for the current instance (excluded from repository)
    "tmp":   A temporary directory for the instance (removed after unfurl exits)
    "spec.src": The directory of the source file the current instance's template appears in.
    "spec.home": The "home" directory of the source file the current instance's template.
    "spec.local": The "local" directory of the source file the current instance's template.

    Otherwise look for a repository with the given name and return its path or None if not found.
    """
    instance = ctx.currentResource
    if not name or name == ".":
        # the folder of the current resource's ensemble
        return instance.baseDir
    elif name == "src":
        # folder of the source file
        return ctx.baseDir
    elif name == "tmp":
        return os.path.join(instance.root.tmpDir, instance.name)
    elif name == "home":
        return os.path.join(instance.baseDir, instance.name)
    elif name == "local":
        return os.path.join(instance.baseDir, instance.name, "local")
    elif name == "project":
        return instance.template.spec._getProjectDir() or instance.baseDir
    else:
        start, sep, rest = name.partition(".")
        if sep:
            if start == "spec":
                template = instance.template
                specHome = os.path.join(template.spec.baseDir, "spec", template.name)
                if rest == "src":
                    return template.baseDir
                if rest == "home":
                    return specHome
                elif rest == "local":
                    return os.path.join(specHome, "local")
            # XXX elif start == 'project' and rest == 'local'
        return instance.template.spec.getRepositoryPath(name)


def abspath(ctx, path, relativeTo=None, mkdir=True):
    if os.path.isabs(path):
        return path

    base = _getbaseDir(ctx, relativeTo)
    if base is None:
        raise UnfurlError('Named directory or repository "%s" not found' % relativeTo)
    fullpath = os.path.join(base, path)
    if mkdir:
        dir = os.path.dirname(fullpath)
        if len(dir) < len(base):
            dir = base
        if not os.path.exists(dir):
            os.makedirs(dir)
    return os.path.abspath(fullpath)


def getdir(ctx, folder, mkdir=True):
    return abspath(ctx, "", folder, mkdir)


# see also abspath in filter_plugins.ref
setEvalFunc("abspath", lambda arg, ctx: abspath(ctx, *_mapArgs(arg, ctx)))

setEvalFunc("get_dir", lambda arg, ctx: getdir(ctx, *_mapArgs(arg, ctx)))


# XXX need an api check if an object was marked sensitive
# _secrets = weakref.WeakValueDictionary()
# def addSecret(secret):
#   _secrets[id(secret.get())] = secret
# def isSecret(obj):
#    return id(obj) in _secrets

setEvalFunc(
    "sensitive",
    lambda arg, ctx: wrapSensitiveValue(
        mapValue(arg, ctx), ctx.templar and ctx.templar._loader._vault
    ),
)


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
        if ctx.templar and ctx.templar._loader._vault.secrets:
            loader.set_vault_secrets(ctx.templar._loader._vault.secrets)
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
            value = "<<Error rendering template: %s>>" % str(e)
            if ctx.strict:
                logger.debug(value, exc_info=True)
                raise UnfurlError(value)
            else:
                logger.warning(value[2:100] + "... see debug log for full report")
                logger.debug(value, exc_info=True)
        else:
            if value != oldvalue:
                ctx.trace("successfully processed template:", value)
                for result in ctx.referenced.getReferencedResults(index):
                    if isSensitive(result):
                        # note: even if the template rendered a list or dict
                        # we still need to wrap the entire result as sensitive because we
                        # don't know how the referenced senstive results were transformed by the template
                        ctx.trace("setting template result as sensitive")
                        return wrapSensitiveValue(
                            value, templar._loader._vault
                        )  # mark the template result as sensitive

                if isinstance(value, Results):
                    # mapValue now because wrap_var() fails with Results types
                    value = mapValue(value, ctx, applyTemplates=False)

                # wrap result as AnsibleUnsafe so it isn't evaluated again
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

    .. code-block:: YAML

      lookup:
          lookupFunctionName: 'arg' or ['arg1', 'arg2']
          kw1: value
          kw2: value
    """
    arg = mapValue(arg, ctx)
    assertForm(arg, test=arg)  # a map with at least one element
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

    return runLookup(name, ctx.templar, *args, **kwargs)


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
    return arg in os.environ


setEvalFunc("has_env", hasEnv, True)


def getEnv(args, ctx):
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


setEvalFunc("get_env", getEnv, True)

_toscaKeywordsToExpr = {
    "SELF": ".",
    "SOURCE": ".source",
    "TARGET": ".target",
    "ORCHESTRATOR": "::localhost",
    "HOST": ".parents",
    "OPERATION_HOST": "$OPERATION_HOST",
}


def get_attribute(args, ctx):
    args = mapValue(args, ctx)
    entity_name = args.pop(0)
    candidate_name = args.pop(0)
    ctx = ctx.copy(ctx._lastResource)

    start = _toscaKeywordsToExpr.get(entity_name, "::" + entity_name)
    if args:
        attribute_name = args.pop(0)
        # need to include candidate_name as a test in addition to selecting it
        # so that the HOST search looks for that and not just ".names" (which all entities have)
        query = "%s::.names[%s]?::%s::%s?" % (
            start,
            candidate_name,
            candidate_name,
            attribute_name,
        )
        if args:  # nested attribute or list index lookup
            query += "::" + "::".join(args)
    else:  # simple attribute lookup
        query = start + "::" + candidate_name
    return ctx.query(query)


setEvalFunc("get_attribute", get_attribute, True)


def get_nodes_of_type(type_name, ctx):
    return [
        r
        for r in ctx.currentResource.root.getSelfAndDescendents()
        if r.template.isCompatibleType(type_name)
        and r.name not in ["inputs", "outputs"]
    ]


setEvalFunc("get_nodes_of_type", get_nodes_of_type, True)


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
        self.schema = importSpec.spec.get("schema")

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
        val = super(SecretResource, self).resolveKey(name, currentResource)
        if isinstance(val, Result):
            val.resolved = wrapSensitiveValue(val.resolved)
            return val
        else:
            return wrapSensitiveValue(val)

    def __sensitive__(self):
        return True


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
            # use '_attributes' so we don't evaluate "default"
            self.default = resource._attributes.get("default")

    def __call__(self, key):
        if self.interface == "inherit":
            return self.inheritFrom.attributes[key]
        elif self.interface == "default":
            result = mapValue(
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
                            resource._attributes[key] = wrapSensitiveValue(
                                value.resolved, resource.templar._loader._vault
                            )
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
