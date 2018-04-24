import six
from .util import *
from .configuration import *
from .configurator import *
from .resource import *
from ruamel.yaml import YAML
from codecs import open
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
        resouceSpec
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

    self.attributeGroups = dict([(key, ParameterDefinition(value, validate))
            for (key, value) in self.manifest.get('attributeGroups', {}).items()])
    self.configurators = dict([(k, ConfiguratorDefinition(v, self, k))
                      for (k, v) in (self.manifest.get(CONFIGURATORSKEY) or {}).items()])
    templates = self.manifest.get('templates') or {}
    self.templates = dict([(k, self._createTemplateDefinition(templates[k], k))
                                                            for k in templates])
    rootResouces = self.manifest.get('resources') or {}
    self._resources = dict([(k, Resource(self, rootResouces[k], k)) for k in rootResouces])
    if validate:
      map(lambda r: [c.getParams() for c in r.spec.configurations], self.resources)

  def getValidateErrors(self):
    version = self.manifest.get('apiVersion')
    if version is None:
      return "missing version"
    elif version != VERSION:
      return "unknown version: %s" % version
    return ''

  @property
  def resources(self):
    return self._resources.values()

  def getRootResource(self, resourceid):
    return self._resources.get(resourceid)

  def _createTemplateDefinition(self, src, name):
    manifestName = src.get('name')
    if manifestName and name and (name != manifestName):
      raise GitErOpError('template key and name do not match: %s %s' % (name, manifestName) )
    return TemplateDefinition(self, src, name or manifestName)
