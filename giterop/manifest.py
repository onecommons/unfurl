import six
from .util import (GitErOpError, toEnum, VERSION,
  DefaultValidatingLatestDraftValidator, expandDoc, restoreIncludes, patchDict)
from .runtime import JobOptions, Configuration, ConfigurationSpec, Status, Action, Defaults, Resource, Runner, Manifest
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from codecs import open
import sys
yaml = YAML()

# XXX3 add as file to package data
#schema=open(os.path.join(os.path.dirname(__file__), 'manifest-v1alpha1.json')).read()
schema = {
  "$schema": "http://json-schema.org/draft-04/schema#",
  "$id": "https://www.onecommons.org/schemas/giterop/v1alpha1.json",
  "definitions": {
    "atomic": {
      "type": "object",
      "properties": {
        "+%": {
          "default": "replaceProps"
        }
      },
    },
    "namedObjects": {
      "type": "object",
      "propertyNames": {
          "pattern": r"^[A-Za-z_][A-Za-z0-9_\-]*$"
        },
    },
    "attributes": {
      "allOf": [
        { "$ref": "#/definitions/namedObjects" },
        { "$ref": "#/definitions/atomic" }
      ],
      'default': {}
    },
    "resource": {
      "type": "object",
      "properties": {
        "spec": {
          "type": "object",
          "properties": {
            "attributes": {
              "$ref": "#/definitions/attributes",
              "default": {}
             },
            "configurations": {
              "allOf": [
                { "$ref": "#/definitions/namedObjects" },
                {"additionalProperties": { "$ref": "#/definitions/configurationSpec" }}
              ],
              'default': {}
            },
          },
          'default': {}
        },
        "status": {
          "allOf": [
            {"$ref": "#/definitions/status" },
            {"properties": {
              "attributes": {
                "$ref": "#/definitions/attributes",
                "default": {}
              },
              "configurations": {
                "allOf": [
                  { "$ref": "#/definitions/namedObjects" },
                  {"additionalProperties": { "$ref": "#/definitions/configurationStatus" }}
                ],
                'default': {}
              }
            }
          }],
          'default': {}
        },
        "resources": {
          "allOf": [
            { "$ref": "#/definitions/namedObjects" },
            {"additionalProperties": { "$ref": "#/definitions/resource" }}
          ],
          'default': {}
        },
      },
    },
    "configurationSpec": {
      "type": "object",
      "properties": {
        "className": {"type":"string"},
        "majorVersion": {"anyOf": [{"type":"string"}, {"type":"number"}]},
        "minorVersion": {"type":"string"},
        "intent": { "enum": list(Action.__members__) },
        "parameters": {
          "$ref": "#/definitions/attributes",
          "default": {}
         },
         "lastAttempt": {"$ref": "#/definitions/changeId"}
      },
      "required": ["className", "majorVersion"]
    },
    "configurationStatus": {
      "type": "object",
      "allOf": [
        { "$ref": "#/definitions/status" },
        { "properties": {
            "action": { "enum": list(Action.__members__) },
            "parameters": {
              "$ref": "#/definitions/attributes",
              "default": {}
            },
            "changes": {
              "allOf": [
                { "$ref": "#/definitions/namedObjects" },
                {"additionalProperties": { "$ref": "#/definitions/namedObjects" }}
              ],
              'default': {}
            },
            "dependencies": {
              "allOf": [
                { "$ref": "#/definitions/namedObjects" },
                {"additionalProperties": { "$ref": "#/definitions/namedObjects" }}
              ],
              'default': {}
            },
          }
      }],
    },
    "status": {
      "type": "object",
      "properties": {
        "operational": { "enum": list(Status.__members__) },
        "changeId":    {"$ref": "#/definitions/changeId"},
      },
      "additionalProperties": True,
    },
    "changeId": {"type":"number"}
  }, # end definitions

  "type": "object",
  "properties": {
    "apiVersion": { "enum": [ VERSION ] },
    "kind": { "enum": [ "Manifest" ] },
    "root": { "$ref": "#/definitions/resource" },
    "changes": { "type": "array",
      "additionalItems": {
        "type": "object",
        "allOf": [
          { "$ref": "#/definitions/status" },
          { "$ref": "#/definitions/configurationStatus" },
          { "properties": {
              "parentId": {"$ref": "#/definitions/changeId"},
              'commitId': {"type": "string"},
              'startTime':{"type": "string"},
              "spec":     {"$ref": "#/definitions/configurationSpec" },
            },
            "required": ["changeId"]
          },
        ]
      }
    }
  },
  "required": ["apiVersion", "kind", "root"]
}

def saveConfigSpec(spec):
  # XXX parameters
  return CommentedMap([
    ("intent", spec.intent.name),
    ("className", spec.className),
    ("majorVersion", spec.majorVersion),
    ("minorVersion", spec.minorVersion),
  ])

class ChangeRecord(object):
  HeaderAttributes = CommentedMap([
   ('changeId', 0),
   ('parentId', None),
   ('commitId', ''),
   ('startTime', ''),
  ])
  CommonAttributes = CommentedMap([
    ('operational', {}),
    ('priority', {}),
    ('resourceName', ''),
    ('configName', ''),
  ])
  RootAttributes = CommentedMap([
    # ('action', ''), # XXX
    ('spec', {}),
    ('parameters', {}),
    ('results', {}),
    ('dependencies', {}),
    ('changes', {}),
    ('messages', []),
  ])

  def __init__(self, task):
    for (k,v) in self.HeaderAttributes.items():
      setattr(self, k, getattr(task, k, v))

    self.operational = task.newConfiguration.status.name
    self.priority = task.newConfiguration.priority.name
    self.resourceName = task.newConfiguration.resource.name
    self.configName = task.newConfiguration.name

    for (k,v) in self.RootAttributes.items():
      if k == 'spec':
        self.spec = saveConfigSpec(task.newConfiguration.configurationSpec)
      elif k == 'messages':
        self.messages = task.messages
      else:
        setattr(self, k, getattr(task.newConfiguration, k, v))

  def load(self, src):
    for (k,v) in self.HeaderAttributes.items():
      setattr(self, k, src.get(k, v))
    for (k,v) in self.RootAttributes.items():
      setattr(self, k, src.get(k, v))
    for (k,v) in self.CommonAttributes.items():
      setattr(self, k, src.get(k, v))

  def dump(self):
    """
    convert dictionary suitable for serializing as yaml
    or creating a ChangeRecord.
    """
    items = [(k, getattr(self, k)) for k in ChangeRecord.HeaderAttributes]
    items.extend([(k, getattr(self, k)) for k in ChangeRecord.RootAttributes
                          if getattr(self, k)] #skip empty values
                  )
    items.extend([(k, getattr(self, k)) for k in ChangeRecord.CommonAttributes
                      if getattr(self, k)]) #skip empty values
    #CommentedMap so order is preserved in yaml output
    return CommentedMap(items)

# +./ +../ +/
# +%: +url:ffff#fff:
class YamlManifest(Manifest):
  """
Loads and saves a GitErOp manifest with the following format:

apiVersion: VERSION
kind: Manifest
include:
  +url:foo:

root: #root resource is always named 'root'
 attributes:
 resources:
   child1:
     +/resourceSpecs/node
  spec:
   attributes:
   configurations:
      name1:
        className
        version
        intent
        priority: XXX
        parameters: #declared
        lastAttempt #changeid #XXX
 status:
  attributes: #merged from config changes and spec/attributes
  operational
  configurations:
    name:
      changeid:
      operational:
      parameters: #actual
        param1: value
      dependencies:
        resource1:
          configuration:foo:
            ok
      changes: #optional
        resource1:
          foo: bar
        resource2:
          status: notpresent
        resource3/child1: +%delete

changes:
  - changeId
    startTime
    commitId
    parentid
    operational
    priority
    resource
    config
    action
    spec
    parameters
    dependencies
    changes
    messages:
"""
  def __init__(self, manifest=None, path=None, validate=True):
    if path:
      with open(path, 'r') as f:
        manifest = f.read()
    if isinstance(manifest, six.string_types):
      self.manifest = yaml.load(manifest)
    else:
      self.manifest = manifest

    #schema should include defaults but can't validate because it doesn't understand includes
    #but should work most of time
    self.includes, manifest = expandDoc(self.manifest, cls=CommentedMap)

    messages = self.validate(manifest)
    if messages and validate:
      raise GitErOpError(messages)
    else:
      self.valid = not not messages

    self.specs = []
    self.changes = manifest.get('changes', [])
    self.lastChangeId = self.changes and max(c['changeId'] for c in self.changes) or 0
    rootResource = self.loadResource('root', manifest['root'], None)
    templates = None
    super(YamlManifest, self).__init__(rootResource, self.specs, templates)

  def createDependency(self, configurationSpec, dependencyTemplateName, args=None):
    return None

  def _makeConfigSpec(self, configName, resourceName, spec):
    return ConfigurationSpec(configName, resourceName, spec['className'], spec['majorVersion'], spec.get('minorVersion',''),
          intent=toEnum(Action, spec.get('intent', Defaults.intent)),
          lastAttempt=spec.get('lastAttempt'))

  def loadResource(self, name, decl, parent):
    status = decl['status']
    specs = decl['spec']
    configSpecs = [self._makeConfigSpec(key, name, val) for key, val in specs['configurations'].items()]
    self.specs.extend(configSpecs)
    resource = Resource(name, status.get('attributes'), parent,
          spec=CommentedMap([
            ("attributes", specs.get('attributes')),
            ("configurations", specs['configurations'])
        ]))

    for key, val in status['configurations'].items():
      # change = val['changeid']; change['spec']
      configSpec = self._makeConfigSpec(key, name, val['spec'])
      config = Configuration(configSpec, resource,
          toEnum(Status, val.get('operational', Status.notapplied)))
      resource.setConfiguration(config)

    for key, val in decl['resources'].items():
      resource.addResource( self.loadResource(key, val, resource) )

    # XXX set status!
    return resource

  def saveStatus(self, operational):
    return CommentedMap([
      ("status", operational.status.name),
      ("priority", operational.priority.name)
    ])

  def saveResource(self, resource, workDone):
    configSpecMap = resource.spec['configurations']
    for name, configSpec in configSpecMap.items():
      task = workDone.get( (resource.name, name ) )
      if task:
        configSpec['lastAttempt'] = task.changeId

    status = self.saveStatus(resource)
    status['configurations'] = CommentedMap(map(self.saveConfiguration, resource.allConfigurations))
    status['attributes'] = resource.attributes
    return (resource.name, CommentedMap([("spec", resource.spec),
      ("resources", CommentedMap(map(lambda r: self.saveResource(r, workDone), resource.resources))),
      ("status", status),
    ]))

  def saveConfiguration(self, config):
    spec = config.configurationSpec
    status = self.saveStatus(config)
    if config.parameters is not None:
      status['parameters'] = config.parameters
    # dependencies.values(), self.configurationSpec.getPostConditions()
    return (config.name, CommentedMap([
            ("spec", saveConfigSpec(spec)),
            ("status", status)
          ]))

  def saveTask(self, task):
    """
    convert dictionary suitable for serializing as yaml
    """
    return ChangeRecord(task).dump()

  def saveJob(self, job, workDone):
    changes = map(self.saveTask, workDone.values())
    changed = CommentedMap([('apiVersion', VERSION), ('kind', 'Manifest')])
    changed.update(CommentedMap([self.saveResource(self.rootResource, workDone)]))
    restoreIncludes(self.includes, self.manifest, changed, cls=CommentedMap)
    # modify original to preserve structure and comments
    patchDict(self.manifest, changed, CommentedMap)
    self.manifest.setdefault('changes', []).extend(changes)
    self.dump(job.out)

  def dump(self, out=sys.stdout):
    yaml.dump(self.manifest, out)

  def validate(self, manifest):
    validator = DefaultValidatingLatestDraftValidator(schema)
    return list(validator.iter_errors(manifest))

def runJob(manifestPath, opts=None):
  manifest = YamlManifest(path=manifestPath)
  runner = Runner(manifest, manifest.lastChangeId)
  kw = opts or {}
  return runner.run(JobOptions(**kw))
