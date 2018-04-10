import six
from .util import *
"""
ComponentSpec:
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
  actions:
    install:
    uninstall:
    update:
    configchange:
    test:
  type
  connection
  src
  phase
  onfailure
"""

"""
cluster-params:
  nodes:
    type:
"""

def defaultConnectionType(phase):
  if phase == 'cloud':
    return 'cloud'
  elif phase == 'bootstrap':
    return 'hosts'
  else:
    return 'kubectl'

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

class ComponentSpec(object):
  def __init__(self, parent, defs, validate=True):
    self.parent = parent
    if isinstance(defs, dict):
      self.localDef = defs
    else: #its a name
      self.localDef = parent.manifest['componentSpecs'][defs]
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

class Component(object):
  """
  name
  spec: ref or inline (use name as ref if omitted)
  parameters
  actiom
  onfailure
  """
  def __init__(self, localDef, parent):
    self.name = localDef['name']
    # parent is ClusterDescription
    self.parent = parent
    self.localDef = localDef
    self.ancestors = []
    self._spec = None

  # merge components before getting spec because spec can change with merge
  @property
  def spec(self):
    # merge spec with local
    if self._spec:
      return self._spec
    specName = self.localDef.get('spec')
    if specName:
      if isinstance(specName, six.string_types):
        spec = self.parent.manifest.componentSpecs.get(specName)
        if not spec:
          raise GitErOpError('component spec not found: %s', specName)
        return spec
      else:
        #inline spec
        if not isinstance(specName, dict):
          raise GitErOpError('malformed spec in %s' % self.name)
        self._spec = ComponentSpec(self, specName)
        return self._spec
    else:
      self.parent.components #call this to make sure ancestors has been calculated
      if not self.ancestors:
        raise GitErOpError('component %s must reference or define a spec' % self.name)
      self._spec = self.ancestors[-1].spec
      return self._spec
    raise GitErOpError('component %s must reference or define a spec' % self.name)

  @property
  def connectionType(self):
    return (self.manifest.get('connection')
      or self.componentType.getConnectionType(self)
      or defaultConnectionType(self.phase))

  @property
  def fqName(self):
    return self.parent.name + '.' + self.name

  def _getInheritedProp(self, name):
    return self.localDef.get(name) or self.ancestors and getattr(self.ancestors[-1], name)

  @property
  def action(self):
    return self._getInheritedProp('action')

  @property
  def onfailure(self):
    return self._getInheritedProp('onfailure')

  def diff(self, other):
    '''
    Compare name, params and action value and commit id
    '''
    return False #XXX

  def __eq__(self, other):
    if self is other:
      return True
    return not not self.diff(other)

  def apply(self, action='install', params=None):
    # XXX action
    findComponentType(self).run(action, params)

  @property
  def params(self):
    #XXX resolve references in values etc.
    return self.localDef.get('parameters', {})

  # hierarchy: componentSpec defaults, componentType defaults and params
  # fully qualify to reference above componentSpec otherwise shadows name
  # error if component references undeclared param
  #we get defaults from componentSpec, update with parent components depth-first
  def getParams(self, merge=None, validate=True):
    defaults = self.spec.getDefaults()
    #depth first list so closed ancestor is updated last
    [defaults.update(ancestor.params) for ancestor in self.ancestors]
    defaults.update(self.params)
    if merge:
      defaults.update(merge)
    if validate:
      self.spec.validateParams(defaults)
    return defaults
