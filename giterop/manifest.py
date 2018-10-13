import six
from .util import *
from .runtime import *
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from codecs import open
import sys
yaml = YAML()

schema = """
"""

class YamlManifest(Manifest):
  """
Loads and saves a GitErOp manifest with the following format:

apiVersion: VERSION
root: #root resource is always named 'root'
 attributes:
 resources:
   child1:
     <resource>
 configurations:
    name1:
      spec:
        className
        version
        intent
        priority: XXX
        version
        parameters:
      status:
        jobid
        operational
        action
        priority
        parameters:
          param1: value
 status:
  operational
  action
  priority
jobs:
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
      self.manifest = yaml.load(open(path).read())
    elif isinstance(manifest, six.string_types):
      self.manifest = yaml.load(manifest)
    else:
      self.manifest = manifest

    #schema should include defaults but can't validate because it doesn't understand includes
    #but should work most of time
    # XXX2 schema.validate
    manifest = expandDoc(self.manifest, cls=CommentedMap)

    messages = self.getValidateErrors()
    if messages and validate:
      raise GitErOpError(messages)
    else:
      self.valid = not not messages

    rootResource = self.loadResource('root', manifest['root'], None)
    specs = list(config.configurationSpec for config in rootResource.getAllConfigurationsDeep())
    templates = None
    super(YamlManifest, self).__init__(rootResource, specs, templates)

  def createDependency(self, configurationSpec, dependencyTemplateName, args=None):
    return None

  def loadResource(self, name, spec, parent):
    resource = Resource(name, spec.get('attributes'), parent)

    for key, val in spec['configurations'].items():
      configSpec = ConfigurationSpec(key, name, val['className'], val['majorVersion'], val.get('minorVersion',''),
                      intent=toEnum(Action, val.get('intent', Defaults.intent)))
      config = Configuration(configSpec, resource,
          toEnum(Status, val.get('status',{}).get('operational', Status.notapplied)))
      resource.setConfiguration(config)

    for key, val in spec['resources'].items():
      resource.addResources( self.loadResource(key, val, resource) )
    return resource

  def saveStatus(self, operational):
    return dict(
      status=operational.status.name,
      priority=operational.priority.name
    )

  def saveResource(self, resource):
    return (resource.name, dict(
      status = self.saveStatus(resource),
      attributes=resource.attributes,
      resources=dict(map(self.saveResource, resource.resources)),
      configurations=dict(map(self.saveConfiguration, resource.allConfigurations))
    ))

  def saveConfiguration(self, config):
    spec = config.configurationSpec
    status = self.saveStatus(config)
    if config.parameters is not None:
      status['parameters'] = config.parameters
    # dependencies.values(), self.configurationSpec.getPostConditions()
    return (config.name, dict(
      status=status,
      className=spec.className,
      majorVersion=spec.majorVersion,
      minorVersion=spec.minorVersion,
      intent=spec.intent.name
      ))

  def saveJob(self, job, workDone):
    # XXX job, workDone??
    changed = {'apiVersion': VERSION}
    changed.update([self.saveResource(self.rootResource)])
    self.manifest = updateDoc(self.manifest, changed, cls=CommentedMap)
    self.dump(job.out)

  def dump(self, out=sys.stdout):
    yaml.dump(self.manifest, out)

  def getValidateErrors(self):
    # XXX2 replace with schema.validate
    version = self.manifest.get('apiVersion')
    if version is None:
      return "missing version"
    elif version != VERSION:
      return "unknown version: %s" % version
    return ''

def run(manifestPath, opts=None):
  manifest = YamlManifest(path=manifestPath)
  runner = Runner(manifest)
  kw = opts or {}
  return runner.run(JobOptions(**kw))
