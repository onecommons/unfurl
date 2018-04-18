import six
from .util import *

actions = "discover instantiate revert".split()
class Configurator(object):

  # does this need to be run on this?
  # returns yes, no, unsupported state
  def shouldRun(self, action, resource, parameters):
    configuratorRunStatus = resource.getLastStatus()
    #was run before? if no, do can we handle requested action?
    #if yes, has version or config changed?
    #  if yes, can we update?
    #  if no, did it succeed before?
    return False

  def run(self, action, resource, parameters):
    configuratorRunStatus = resource.getLastStatus()
    resource.update(metadata, spec)
    resource.updateResource(changedResource)
    resource.addResource(newresource)
    return status

class ParameterDefinition(object):
  def __init__(self, obj, validate=True):
    if validate:
      for k in "name enum".split():
        if not obj.get(k):
          if k != "enum" or obj.get('type') == "enum":
            raise GitErOpError('parameter definition missing "%s"' % k)
    for key in "name type required enum".split():
      setattr(self, key, obj.get(key))
    if 'default' in obj:
      self.default = obj['default']

  @property
  def hasDefault(self):
    return hasattr(self, 'default')

  def isValueCompatible(self, value):
    if isinstance(value, (int, long, float)):
      if self.type == 'int':
        return round(value) == value
      return self.type == 'number'
    if isinstance(value, six.string_types):
      if self.type == 'enum':
        return value in self.enum
      return not self.type or self.type == 'string'
    return isinstance(value, bool) and self.type == 'boolean'

class ConfiguratorSpec(object):
  """
  Configurator:
    name
    version
    parameters
      - name: my_param # secret_
        ref: type or connection or cluster or app
        #XXX type: enum
        #XXX enum: ['X', 'Y', 'Z']
        required: True
        default:
          clusterkms: name
    src
  """
  def __init__(self, parent, defs, validate=True):
    self.parent = parent
    if isinstance(defs, dict):
      self.localDef = defs
    else: #its a name
      self.localDef = parent.manifest['configurators'][defs]
      if not isinstance(self.localDef, dict):
        raise GitErOpError('malformed spec %s' % defs)
    self.parameters = [ParameterDefinition(obj, validate)
                          for obj in self.localDef.get('parameters', [])]

  def getDefaults(self):
    return dict([(paramdef.name, paramdef.default)
      for paramdef in self.parameters if paramdef.hasDefault])

  def validateParams(self, params):
    params = params.copy()
    for paramdef in self.parameters:
      value = params.pop(paramdef.name, None)
      if value is None:
        if paramdef.required:
          raise GitErOpError("missing required parameter: %s" % paramdef.name)
      elif not paramdef.isValueCompatible(value):
        raise GitErOpError("invalid value: %s" % paramdef.name)
    if params:
        raise GitErOpError("unexpected parameter(s): %s" % params.keys())
    return True

def findComponentType(component):
  for klass in ComponentTypes:
    if klass.isComponentType(component):
      return klass(component)
  raise GitterOpError('Unsupported component type')

def _getRef(uri, commitRef=None):
  '''
  current repo
  remote repo
  container image

  Returns: (local file path, commit id)
  '''
