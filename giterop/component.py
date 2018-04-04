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

def defaultConnectionType(phase):
  if phase == 'cloud':
    return 'cloud'
  elif phase == 'bootstrap':
    return 'hosts'
  else:
    return 'kubectrl'

class ComponentSpec(object):
  def __init__(self, parent, name):
    self.parent = parent

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
    self.parent = parent
    self.localDef = localDef

  # merge components before getting spec because spec can change with merge
  def getDef(self):
    # merge spec with local
    specName = self.localDef.get('spec')
    if specName:
      if isinstance(specName, six.string_types):
        spec = self.parent.manifest.componentSpecs.get(specName)
        if not spec:
          raise GitErOpError('component spec not found: %s', specName)
      else:
        #inline spec
        spec = ComponentSpec(spec, self)
      return spec.getDef().clone().update(self.localDef)
    return self.localDef

  @property
  def connectionType(self):
    return (self.manifest.get('connection')
      or self.componentType.getConnectionType(self)
      or defaultConnectionType(self.phase))

  @property
  def fqName(self):
    return self.parent.name + '.' + self.name

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

  # hierarchy: componentSpec defaults, componentType defaults and params, connection params, cluster params, component params, repo params
  # fully qualify to reference above componentSpec otherwise shadows name
  # error if component references undeclared param
  def getParams(self, merge):
    return self.params
