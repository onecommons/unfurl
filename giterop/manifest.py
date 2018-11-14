import six
import copy
from .util import (GitErOpError, VERSION,
  expandDoc, restoreIncludes, patchDict, validateSchema)
from .runtime import (JobOptions, Status, Priority, serializeValue,
  Action, Defaults, Runner, Manifest, ResourceChanges)
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
        # XXX
        # "+%": {
        #   "default": "replaceProps"
        # }
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
            "attributesSchema": {
              "$ref": "#/definitions/schema",
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
        "parameterSchema": {
          "$ref": "#/definitions/schema",
          "default": {}
         },
        "requires": {
          "$ref": "#/definitions/schema",
          "default": {}
         },
        "provides": {
          "type": "object",
          "properties": {
            ".self": {
              "$ref": "#/definitions/resource/properties/spec" #excepting "configurations"
             },
            ".configurations": {
              "allOf": [
                { "$ref": "#/definitions/namedObjects" },
                # {"additionalProperties": { "$ref": "#/definitions/configurationSpec" }}
              ],
              'default': {}
            },
          },
          # "additionalProperties": { "$ref": "#/definitions/resource/properties/spec" },
          'default': {}
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
            "modifications": {
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
        "readyState": {
          "type": "object",
          "properties": {
            "computed": { "enum": list(Status.__members__) },
            "local":    { "enum": list(Status.__members__) }
          }
        },
        "priority": { "enum": list(Priority.__members__) },
        "changeId": {"$ref": "#/definitions/changeId"},
      },
      "additionalProperties": True,
    },
    "changeId": {"type":"number"},
    "schema":   {"type": "object"}
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
  saved = CommentedMap([
    ("intent", spec.intent.name),
    ("className", spec.className),
    ("majorVersion", spec.majorVersion),
    ("minorVersion", spec.minorVersion),
    ("parameters", spec.parameters),
    ("parameterSchema", spec.parameterSchema),
    ("requires", spec.requires),
  ])
  if spec.provides:
    dotSelf = spec.provides.get('.self')
    if dotSelf:
      # removed defaults put in by schema
      dotSelf.pop('configurations', None)
      if not dotSelf.get('attributes'):
        dotSelf.pop('attributes', None)
    saved["provides"] = spec.provides

  if spec.lastAttempt:
    saved["lastAttempt"] = spec.lastAttempt
  return saved

def saveResourceSpec(rspec):
    spec = CommentedMap()
    if 'attributes' in rspec:
      spec['attributes'] = rspec['attributes']
    spec['configurations'] = CommentedMap([(cspec.name, saveConfigSpec(cspec))
          for cspec in rspec['configurations'].values()])
    return spec

def saveResourceChanges(changes):
  d = CommentedMap()
  for k, v in changes.items():
    d[k] = v[ResourceChanges.attributesIndex] or {}
    if v[ResourceChanges.statusIndex] is not None:
      d[k]['.status'] = v[ResourceChanges.statusIndex].name
    if v[ResourceChanges.addedIndex]:
      d[k]['.added'] = v[ResourceChanges.addedIndex]
  return d

def saveStatus(operational):
  readyState = CommentedMap([
    ("computed", operational.status.name),
    ('local', operational.localStatus.name)
  ])
  status = CommentedMap([("readyState", readyState)])
  if operational.priority and operational.priority != Defaults.shouldRun:
    status["priority"] = operational.priority.name
  return status

class ChangeRecord(object):
  HeaderAttributes = CommentedMap([
   ('changeId', 0),
   ('parentId', None),
   ('commitId', ''),
   ('startTime', ''),
  ])
  CommonAttributes = CommentedMap([
    # ('readyState', {}),
    # ('priority', {}),
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

    self.status = task.newConfiguration
    self.resourceName = task.newConfiguration.resource.name
    self.configName = task.newConfiguration.name

    for (k,v) in self.RootAttributes.items():
      if k == 'spec':
        self.spec = saveConfigSpec(task.newConfiguration.configurationSpec)
      elif k == 'changes':
        self.changes = saveResourceChanges(task.resourceChanges)
      elif k == 'messages':
        self.messages = task.messages
      elif k == 'parameters':
        self.parameters = serializeValue(task.newConfiguration.parameters)
      else:
        setattr(self, k, getattr(task.newConfiguration, k, v))

  def load(self, src):
    for (k,v) in self.HeaderAttributes.items():
      setattr(self, k, src.get(k, v))
    self.status = Manifest.createStatus(src)
    for (k,v) in self.CommonAttributes.items():
      setattr(self, k, src.get(k, v))
    for (k,v) in self.RootAttributes.items():
      setattr(self, k, src.get(k, v))

  def dump(self):
    """
    convert dictionary suitable for serializing as yaml
    or creating a ChangeRecord.
    """
    items = [(k, getattr(self, k)) for k in ChangeRecord.HeaderAttributes]
    items.extend( saveStatus(self.status).items() )
    items.extend([(k, getattr(self, k)) for k in ChangeRecord.CommonAttributes
                      if getattr(self, k)]) #skip empty values
    items.extend([(k, getattr(self, k)) for k in ChangeRecord.RootAttributes
                          if getattr(self, k)] #skip empty values
                  )
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
  spec:
    attributes:
    configurations:
      name1:
        className
        version
        intent
        priority:
        provides:
          .self: #.self describes the attributes that will
            attributes
            attributesSchema
          .configurations: #configurations that maybe used by this configurator
            configTemplate1:
          resourceTemplate1:
            attributes:
              foo: bar
            configurations:
              config1:
        parameters: #declared
        lastAttempt #changeid
  status:
    lastChange: changeId
    readyState:
      computed:
      local:
    priority
    attributes: #merged from config changes and spec/attributes
    configurations:
      name:
        changeId:
        readyState:

        parameters: #actual
          param1: value
        dependencies:
          resource1:
            configuration:foo:
              ok
        modifications:
          resource1:
            .spec: # set if added resource
            .status: # set when adding or removing
            foo: bar
          resource2:
            .spec:
            .status: notpresent
          resource3/child1: +%delete
  resources:
    child1:
      +/resourceSpecs/node
      status:
       createdBy: changeid
       createdFrom: templateName

changes:
  - changeId
    startTime
    commitId
    parentChange
    previousChange #XXX
    readyState
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
      self.path = path
      with open(path, 'r') as f:
        manifest = f.read()
    else:
      self.path = None
    if isinstance(manifest, six.string_types):
      self.manifest = yaml.load(manifest)
    else:
      self.manifest = manifest

    #schema should include defaults but can't validate because it doesn't understand includes
    #but should work most of time
    self.includes, manifest = expandDoc(self.manifest, cls=CommentedMap)
    #print('expanded')
    #yaml.dump(manifest, sys.stdout)
    messages = self.validate(manifest)
    # XXX dont print, log validation errors
    #for error in messages:
    #  print(error)
    if messages and validate:
      raise GitErOpError(messages)
    else:
      self.valid = not not messages

    self.specs = []
    self.changeSets = dict((c['changeId'], c) for c in  manifest.get('changes', []))
    lastChangeId = self.changeSets and max(self.changeSets.keys()) or 0
    rootResource = self.createResource('root', manifest['root'], None)
    templates = None #XXX3
    super(YamlManifest, self).__init__(rootResource, self.specs, templates, lastChangeId)

  def saveResource(self, resource, workDone):
    configSpecMap = resource.spec['configurations']
    for name, configSpec in configSpecMap.items():
      task = workDone.get( (resource.name, name ) )
      if task:
        configSpec.lastAttempt = task.changeId
      # XXX:
      # elif 'lastAttempt' in configSpec and configSpec had changed
      # del configSpec['lastAttempt']

    status = saveStatus(resource)
    status['attributes'] = resource._attributes
    if resource.createdOn:
      status['createdOn'] = resource.createdOn
    if resource.createdFrom:
      status['createdFrom'] = resource.createdFrom

    status['configurations'] = CommentedMap(map(self.saveConfiguration, resource.configurations.values()))
    spec = saveResourceSpec(resource.spec)
    return (resource.name, copy.deepcopy(CommentedMap([("spec", spec),
      ("resources", CommentedMap(map(lambda r: self.saveResource(r, workDone), resource.resources))),
      ("status", status),
    ])))

  def saveConfiguration(self, config):
    spec = config.configurationSpec
    status = saveStatus(config)
    #status['parameters'] = config.parameters
    status["spec"] = saveConfigSpec(spec)
    status['modifications'] = saveResourceChanges(config.resourceChanges)
    # XXX dependencies.values(), self.configurationSpec.getPostConditions()
    return (config.name, status)

  def saveTask(self, task):
    """
    convert dictionary suitable for serializing as yaml
    """
    return ChangeRecord(task).dump()

  def saveJob(self, job):
    changes = map(self.saveTask, job.workDone.values())
    changed = CommentedMap([self.saveResource(self.rootResource, job.workDone)])
    # update changed with includes, this may change objects with references to these objects
    restoreIncludes(self.includes, self.manifest, changed, cls=CommentedMap)
    # modify original to preserve structure and comments
    patchDict(self.manifest['root'], changed['root'], cls=CommentedMap)
    self.manifest.setdefault('changes', []).extend(changes)
    if job.out:
      self.dump(job.out)
    elif self.path:
      with open(self.path, 'w') as f:
        self.dump(f)
    else:
      output = six.StringIO()
      self.dump(output)
      job.out = output

  def dump(self, out=sys.stdout):
    yaml.dump(self.manifest, out)

  def validate(self, manifest):
    return validateSchema(manifest, schema)

def runJob(manifestPath, opts=None):
  manifest = YamlManifest(path=manifestPath)
  runner = Runner(manifest)
  kw = opts or {}
  return runner.run(JobOptions(**kw))
