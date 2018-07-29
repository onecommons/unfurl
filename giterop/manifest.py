import six
from .util import *
from .templatedefinition import *
from .configurator import *
from .resource import *
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from codecs import open
import sys
yaml = YAML()

class Manifest(object):
  """
  represents a GitErOp manifest with
    version
    configurators
    templates:
      "name":
        attributes
        templates
        configurations
    resources:
      "name":
        resourceSpec
    status:
      changes:
        - changeid
          date
          commit
          action
          status
          messages:

  """
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

    for key in "templates resources".split():
      if not self.manifest.get(key):
        self.manifest[key] = CommentedMap()

    self.configurators = dict([(k, ConfiguratorDefinition(v, self, k))
                      for (k, v) in (self.manifest.get(CONFIGURATORSKEY) or {}).items()])
    templates = self.manifest['templates']
    self.templates = dict([(k, self._createTemplateDefinition(templates[k], k))
                                                            for k in templates])
    rootResources = self.manifest['resources']
    self._resources = dict([(k, ResourceDefinition(self, rootResources[k], k)) for k in rootResources])
    if validate:
      list(map(lambda r: [c.getParams() for c in r.spec.configurations], self.resources))

  def save(self):
    [rr.save() for rr in self.resources]

  def dump(self, out=sys.stdout):
    yaml.dump(self.manifest, out)

  def getValidateErrors(self):
    version = self.manifest.get('apiVersion')
    if version is None:
      return "missing version"
    elif version != VERSION:
      return "unknown version: %s" % version
    return ''

  @property
  def resources(self):
    return list(self._resources.values())

  def getRootResource(self, resourceid):
    return self._resources.get(resourceid)

  def findResource(self, resourceid):
    for r in self.resources:
      match = r.findLocalResource(resourceid)
      if match:
        return match
    return None

  def _createTemplateDefinition(self, src, name):
    manifestName = src.get('name')
    if manifestName and name and (name != manifestName):
      raise GitErOpError('template key and name do not match: %s %s' % (name, manifestName) )
    return TemplateDefinition(self, src, name or manifestName)

  def findMaxChangeId(self):
    return (self.resources and
      max(r.findMaxChangeId() for r in self.resources) or 0)
