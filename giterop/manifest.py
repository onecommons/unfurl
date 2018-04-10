import six
from .util import *
from .component import *
from ruamel.yaml import YAML
from codecs import open
yaml = YAML()

VERSION = '0.1'
class Manifest(object):
  def __init__(self, manifest=None, path=None, validate=True):
    if path:
      self.manifest = yaml.load(open(manifestPath).read())
    elif isinstance(manifest, six.string_types):
      self.manifest = yaml.load(manifest)
    else:
      self.manifest = manifest
    messages = self.getValidateErrors()
    if messages and validate:
      raise GitErOpError(messages)
    else:
      self.valid = not not messages
    self.clusterTemplates = dict([(k, ClusterTemplate(self, k)) for k in (self.manifest.get('clusterTemplates') or {}).keys()])
    self._clusters = dict([(k, ClusterSpec(self, k)) for k in (self.manifest.get('clusters') or {}).keys()])
    self.componentSpecs = dict([(k, ComponentSpec(self, k)) for k in (self.manifest.get('componentSpecs') or {}).keys()])
    if validate:
      map(lambda x: [c.getParams() for c in x.components], self._clusters.values())

  def getValidateErrors(self):
    version = self.manifest.get('version')
    if version is None:
      return "missing version"
    elif version != VERSION:
      return "unknown version: %s" % version
    return ''

  @property
  def clusters(self):
    return self._clusters.values()

  def getCluster(self, clusterid):
    return self._clusters.get(clusterid)

  # @property
  # def installed(self):
  #   '''Return list of components'''
  #   return find(self.manifest, '*/not action or action=installed')

class ClusterDescription(object):
  def __init__(self, manifest, name=None):
    self.manifest = manifest
    if name:
      manifest = self._findManifest(name)
    manifestName = manifest.get('name')
    if manifestName and name and (name != manifestName):
      raise GitErOpError('%s key and name do not match: %s %s' % (self.key, name, manifestName) )
    self.name = name or manifestName
    self._allComponents = None
    #make sure Component objects are created early
    self.localComponentsDict = self._getLocalComponentsDict()

  def _findManifest(self, name=None):
    """
    Find the dictionary that describes this ClusterDescription
    """
    return self.manifest.manifest[self.key][name or self.name]

  @property
  def templates(self):
    return self._getTemplates({}).values()

  def _getTemplates(self, seen):
    manifest = self._findManifest()
    names = manifest.get('clusterTemplates')
    if not names:
      return seen
    elif isinstance(names, six.string_types):
      names = [names]
    for name in names:
      if name not in seen:
        template = self.manifest.clusterTemplates.get(name)
        if not template:
          raise GitErOpError("Cluster template reference is not defined: %s" % name)
        #depth first
        seen.update(template._getTemplates(seen))
        seen[name] = template
    return seen

  # components explicitly defined on this object
  def _getLocalComponentsDict(self):
    components = self._findManifest().get('components')
    if not components:
      return {}
    def makeComponents(component):
      if isinstance(component, six.string_types):
        component = {'name': component}
      return (component['name'], Component(component, self))
    componentDict = dict(map(makeComponents, components))
    if len(componentDict) != len(components):
      raise GitErOpError("Duplicate names in component list for %s" % self.name)
    return componentDict

  @property
  def components(self):
    if self._allComponents is not None:
      return self._allComponents
    # first add the templates' components
    components = []
    localComponents = self.localComponentsDict
    for t in self.templates:
      for c in t.components:
        match = localComponents.pop(c.fqName, None)
        if match:
          #replace the ancestor with the decendent
          match.ancestors.append(c)
          components.append(match)
        else:
          components.append(c)
    components.extend(localComponents.values())
    self._allComponents = components
    return components

class ClusterTemplate(ClusterDescription):
  key = 'clusterTemplates'

class ClusterSpec(ClusterDescription):
  key = 'clusters'
