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
from .util import (intersectDict, mergeDicts, ChainMap, findSchemaErrors,
                GitErOpError, GitErOpValidationError, YamlConfig, assertForm)

# XXX3 doc: notpresent is a positive assertion of non-existence while notapplied just indicates non-liveness
# notapplied is therefore the default initial state
Status = IntEnum("Status", "ok degraded stopped error pending notapplied notpresent", module=__name__)

# ignore may must
Priority = IntEnum("Priority", "ignore optional required", module=__name__)

# omit discover exist
Action = IntEnum("Action", "discover instantiate revert", module=__name__)

class Defaults(object):
  shouldRun = Priority.optional
  canRun = True
  intent = Action.instantiate

class File(ExternalValue):
  """
  Represents a local file.
  get() returns the given file path (usually relative)
  """
  def __init__(self, name):
    super(File, self).__init__('file', name)

  def getFullPath(self, baseDir):
    return os.path.abspath(os.path.join(baseDir, self.get()))

  def resolveKey(self, name=None, currentResource=None):
    """
    path # absolute path
    contents # file contents (None if it doesn't exist)
    """
    if not name:
      return self.get()

    baseDir = currentResource.root.baseDir
    if name == 'path':
      return self.getFullPath(baseDir)
    elif name == 'contents':
      with open(self.getFullPath(baseDir), 'r') as f:
        return f.read()
    else:
      raise KeyError(name)

setEvalFunc('file', lambda arg, ctx: File(arg))

def runLookup(name, templar, *args, **kw):
  from ansible.plugins.loader import lookup_loader
  # https://docs.ansible.com/ansible/latest/plugins/lookup.html
  # "{{ lookup('url', 'https://toshio.fedorapeople.org/one.txt', validate_certs=True) }}"
  #       would end up calling the lookup plugin named url's run method like this::
  #           run(['https://toshio.fedorapeople.org/one.txt'], variables=available_variables, validate_certs=True)
  instance = lookup_loader.get(name, loader = templar._loader, templar = templar)
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
  lookup:
    - file: 'foo' or []
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
  return runLookup(name, ctx.currentResource.templar, *args, **kw)

setEvalFunc('lookup', lookupFunc)

def getImport(arg, ctx):
  """
  Returns the external resource associated with the named import
  """
  try:
    imported = ctx.currentResource.root.imports[arg]
  except KeyError:
    raise GitErOpError("Can't find import '%s'" % arg)
  if arg == 'secret':
    return SecretResource(arg, imported)
  else:
    return ExternalResource(arg, imported)

class ExternalResource(ExternalValue):
  """
  Wraps a foreign resource
  """
  def __init__(self, name, importSpec):
    super(ExternalResource, self).__init__('external', name)
    self.resource = importSpec.resource
    self.schema = importSpec.spec.get('properties')

  def _validate(self, obj, schema, name):
    if schema:
      messages = findSchemaErrors(serializeValue(obj), schema)
      if messages:
        raise GitErOpValidationError("schema validation failed for attribute '%s': %s" % (name, messages[1]), messages[1])

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
      if schema and 'default' in schema:
        return schema['default']
    if schema:
      self._validate(value, schema, name)
    # we don't want to return a result across boundaries
    return value

class Secret(object):
  def __init__(self, _secret):
    self._reveal = _secret

  @property
  def reveal(self):
    if isinstance(self._reveal, Result):
      return self._reveal.resolved
    else:
      return self._reveal

  def __reflookup__(self, key):
    if key == 'reveal':
      return self._reveal
    raise KeyError(key)

class SecretResource(ExternalResource):
  def resolveKey(self, name=None, currentResource=None):
    val = super(SecretResource, self).resolveKey(name, currentResource)
    if isinstance(val, Secret):
      return val
    return Secret(val)

setEvalFunc('external', getImport)
# shortcuts for local and secret
def shortcut(arg, ctx):
  return Ref(dict(ref=dict(external=ctx.currentFunc), foreach=arg)).resolve(ctx, wantList='result')
setEvalFunc('local', shortcut)
setEvalFunc('secret', shortcut)

class DelegateAttributes(object):
  def __init__(self, interface, resource):
    self.interface = interface
    self.resource = resource
    self.inheritFrom = resource.attributes.get('inheritFrom', {})
    self.default = resource.attributes.get('default', {})

  def __call__(self, key):
    if self.interface == 'inherit':
      return self.inheritFrom.attributes[key]
    elif self.interface == 'default':
      result = Ref(self.default).resolve(RefContext(self.resource, vars=dict(key=key)))
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

  modifications:
    resource1:
      attribute1: newvalue
      attribute2: %delete # if deleted
      .added: # set if resource was added
      .status: # set when status changes, including when removed (Status.notpresent)
  """
  statusIndex = 0
  addedIndex = 1
  attributesIndex = 2

  def sync(self, resource):
    """ Update self to only include changes that are still live"""
    for k, v in list(self.items()):
      current = Ref(k).resolveOne(RefContext(resource))
      if current:
        attributes = v[self.attributesIndex]
        if attributes:
          v[self.attributesIndex] = intersectDict(attributes, current._attributes)
        if v[self.statusIndex] != current._localStatus:
          v[self.statusIndex] = None
      else:
        del self[k]

  def addChanges(self, changes):
    for name, change in changes.items():
      old = self.get(name)
      if old:
        old[self.attributesIndex] = mergeDicts(old[self.attributesIndex], change)
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
      self['::'+resource['name']] = [None, resource, None]

  def updateChanges(self, changes, statuses, resource):
    self.addChanges(changes)
    self.addStatuses(statuses)
    if resource:
      self.sync(resource)

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

  def setStatus(self, resource, newvalue):
    assert newvalue is None or isinstance(newvalue, Status)
    if resource.key not in self.statuses:
      self.statuses[resource.key] = [resource._localStatus, newvalue]
    else:
      self.statuses[resource.key][1] = newvalue

  def getAttributes(self, resource):
    if resource.key not in self.attributes:
      specd = resource.template.properties if resource.template else {}
      attributes = ResultsMap(ChainMap(copy.deepcopy(resource._attributes), specd), RefContext(resource))
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
    # current and original don't have external values
    for resource, attributes in self.attributes.values():
      # save in _attributes in serialized form
      overrides, specd = attributes._attributes._maps
      resource._attributes = {}
      for key, value in overrides.items():
        if not isinstance(value, Result):
          # hasn't been touched so keep it as is
          resource._attributes[key] = value
        elif key not in specd or value.hasDiff():
          resource._attributes[key] = value.asRef()

      # save changes
      diff = attributes.getDiff()
      if not diff:
        continue
      changes[resource.key] = diff
    self.attributes = {}
    # self.statuses = {}
    return changes

class LocalConfig(object):
  """
  the local configuration.

  Contains a map of repos

  Each repo can have the following:
  path
  local:
  secret:

manifests:
  - path:
    local:
      path: path
      resource: root
      # or:
      attributes:
        #XXX: inheritFrom:
    secret:
    default: true

defaults: #used by manifests not defined above
 local:
 secret:

"""
  def __init__(self, path=None):
    defaultConfig = {}
    # XXX define schema and validate
    self.config = YamlConfig(defaultConfig, path=path)
    self.manifests = self.config.config.get('manifests', [])
    self.defaults = self.config.config.get('defaults', {})

  def adjustPath(self, path):
    """
    Makes sure relative paths are relative to the location of this local config
    """
    return os.path.join(self.config.getBaseDir(), path)

  def getDefaultManifestPath(self):
    if len(self.manifests) == 1:
      return self.adjustPath(self.manifests[0]['path'])
    else:
      for spec in self.manifests:
        if spec.get('default'):
          return self.adjustPath(spec['path'])
    return None

  def getLocalResource(self, manifestPath, localName, importSpec):
    """
    localName is either 'local' or 'secret'
    """
    from .runtime import Resource
    localRepo = None
    for spec in self.manifests:
      if manifestPath == self.adjustPath(spec['path']):
        localRepo = spec.get(localName)
    if not localRepo:
      localRepo = self.defaults.get(localName)

    if localRepo:
      attributes = localRepo.get('attributes')
      if attributes is not None:
        #XXX if inheritFrom or defaults in attributes: add .interface
        return Resource(localName, attributes)
      else:
        # its local or secret is resource defined in a manifest
        # set the url and resource name so the manifest loads it
        # XXX but should load here and save for re-use
        importSpec.update(localRepo)
        if 'path' in localRepo:
          importSpec['path'] = self.adjustPath(localRepo['path'])
        return None

    # none found, return empty resource
    return Resource(localName)

class LocalEnv(object):
  """
  Represents the local environment that a manifest runs in.
  """
  DefaultManifestName = 'manifest.yaml'
  DefaultLocalConfigName = 'giterop.yaml'
  DefaultHomeDirectory = '.giterop_home'

  def __init__(self, manifestPath=None, homepath=None):
    # XXX need to load and save local config
    self.homeConfigPath = self.getHomeConfigPath(homepath)
    self.config, self.path = self.findConfigAndManifestPaths(manifestPath)

  def getHomeConfigPath(self, homepath):
    if homepath:
      if os.path.isdir(homepath):
        return os.path.abspath(os.path.join(homepath, self.DefaultLocalConfigName))
      else:
        return os.path.abspath(homepath)
    return os.path.expanduser(os.path.join('~', self.DefaultHomeDirectory, self.DefaultLocalConfigName))

  def getLocalResource(self, name, importSpec):
    if name != 'local' and name != 'secret':
      return None
    return self.config.getLocalResource(self.path, name, importSpec)

  def findConfigAndManifestPaths(self, manifestPath):
    """
    Return absolute paths to the manifest and the local config
    """
    fullPath = None
    if not manifestPath:
      localConfigPath, fullPath = self.findConfigPaths('.') # search for manifest or .giterop in current directory
    elif not os.path.exists(manifestPath):
      raise GitErOpError("Manifest file does not exist: %s" % os.path.abspath(manifestPath))
    else:
      if os.path.isdir(manifestPath):
        localConfigPath, fullPath = self.findConfigPaths(manifestPath)
      else:
        localConfigPath, fullPath = self.findConfigPaths(os.path.dirname(manifestPath), os.path.abspath(manifestPath))

    if localConfigPath:
      localConfig = LocalConfig(localConfigPath)
    else:
      # XXX load and merge home config even if local config exists
      if os.path.exists(self.homeConfigPath):
        localConfig = LocalConfig(self.homeConfigPath)
      else:
        localConfig = LocalConfig()

    if not fullPath:
      if localConfigPath:
        fullPath = localConfig.getDefaultManifestPath()
        if fullPath and not os.path.exists(fullPath):
          raise GitErOpError("The default manifest found in the local config does not exist: %s" % os.path.abspath(fullPath))
        else:
          return localConfig, fullPath

      message = "Can't find a giterop repository in current directory (or any of the parent directories)"
      raise GitErOpError(message)

    return localConfig, fullPath

  def findConfigPaths(self, dir, manifestPath = None):
    """
    Return absolute paths to the local config and the manifest
    (giterop.yaml and manifest.yaml).
    If manifestPath isn't specified, find the nearest manifest.yaml
    """
    current = os.path.abspath(dir)
    while current and current != os.sep:
      if manifestPath is None:
        test = os.path.join(current, self.DefaultManifestName)
        if os.path.exists(test):
          manifestPath = test

      test = os.path.join(current, self.DefaultLocalConfigName)
      if os.path.exists(test):
        return test, manifestPath
      current = os.path.dirname(current)
    return None, manifestPath
