"""Loads and saves a GitErOp manifest with the following format:

apiVersion: VERSION
kind: Manifest

locals:
 properties:
   name1:
     type: string
     default
 required:
secrets:
   name1:
     type: string
     default
   # save-digest: true
   # prompt: true
   # readonly: true
 required:

root: #root resource is always named 'root'
  spec:
    attributes:
      .interfaces:
        - foo.bar.Interface
    attributesSchema:
    configurations:
      name1:
        className
        version
        intent
        parameters: #declared
        priority:
        provides:
          .self: #.self describes the attributes that this configuration will instantiate
            attributes
            attributesSchema
          .configurations: #configurations that maybe used by this configurator
            configTemplate1:
          resourceTemplate1: # may create resources like this
            template:
            attributes:
              foo: bar
              .interfaces:
                - module.barManager
            configurations:
              config1:
                template:
        lastAttempt: changeid
  status:
    readyState:
      effective:
      local:
      lastChange: changeId
    priority
    lastConfigChange: changeId
    attributes: # modifications to spec/attributes
    configurations:
      name:
        readyState:
          effective:
          local:
        lastConfigChange: changeId
        lastStateChange: changeId
        priority
        parameters: #actual
          param1: value
        dependencies:
          - ref: ::resource1::key[~$val]
            expected: "value"
          - name: named1
            ref: .configurations::foo[.operational]
            required: true
            schema:
              type: array
        modifications:
          resource1:
            .added: # set if added resource
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

import six
import copy
from .util import (GitErOpError, VERSION,
  expandDoc, restoreIncludes, patchDict, validateSchema)
from .runtime import (JobOptions, Status, Priority, serializeValue,
  Action, Defaults, Runner, Manifest, ResourceChanges, ChangeRecord)
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from codecs import open
from six.moves import reduce
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
    "secret": {
      "type": "object",
      "properties": {
        "ref": {
          "type": "object",
          "properties": {
            "secret": {
              "type": "string",
              "pattern": r"^[A-Za-z_][A-Za-z0-9_\-]*$"
            },
          },
          "required": ["secret"]
        },
      },
      "required": ["ref"]
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
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                    "name":      { "type": "string" },
                    "ref":       { "type": "string" },
                    "expected":  {},
                    "schema":    { "$ref": "#/definitions/schema" },
                    "required":  { "type": "boolean"},
                },
              },
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
            "effective": { "enum": list(Status.__members__) },
            "local":    { "enum": list(Status.__members__) }
          }
        },
        "priority": { "enum": list(Priority.__members__) },
        "lastStateChange": {"$ref": "#/definitions/changeId"},
        "lastConfigChange": {"$ref": "#/definitions/changeId"},
      },
      "additionalProperties": True,
    },
    "changeId": {"type": "number"},
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
    saved["lastAttempt"] = spec.lastAttempt.changeId
  return saved

def saveDependency(dep):
  saved = CommentedMap()
  if dep.name:
    saved['name'] = dep.name
  saved['ref'] = dep.expr
  if dep.expected is not None:
    saved['expected'] = serializeValue(dep.expected)
  if dep.schema is not None:
    saved['schema'] = dep.schema
  if dep.required:
    saved['required'] = dep.required
  if dep.wantList:
    saved['wantList'] = dep.wantList
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
    ("effective", operational.status.name),
  ])
  if operational.localStatus is not None:
    readyState['local'] =  operational.localStatus.name

  status = CommentedMap([("readyState", readyState)])
  if operational.priority and operational.priority != Defaults.shouldRun:
    status["priority"] = operational.priority.name
  if operational.lastStateChange:
    status["lastStateChange"] = operational.lastStateChange
  if operational.lastConfigChange:
    status["lastConfigChange"] = operational.lastConfigChange

  return status

class Changeset(object):
  HeaderAttributes = CommentedMap([
   ('changeId', 0),
   ('parentId', None),
   ('commitId', ''),
   ('startTime', ''),
   ('resourceName', ''),
   ('configName', ''),
  ])
  CommonAttributes = CommentedMap([
    # ('readyState', {}),
    # ('priority', {}),
  ])
  RootAttributes = CommentedMap([
    # ('action', ''), # XXX
    ('spec', {}),
    ('parameters', {}),
    ('results', {}),
    # ('dependencies', {}), # XXX
    ('changes', {}),
    ('messages', []),
  ])

  def __init__(self, task):
    self.changeRecord = ChangeRecord()
    if isinstance(task, dict):
      self._initFromDict(task)
    else:
      self._initFromTask(task)

  def _initFromTask(self, task):
    for (k,v) in self.HeaderAttributes.items():
      setattr(self.changeRecord, k, getattr(task, k, v))

    self.status = task.pendingConfig

    for (k,v) in self.RootAttributes.items():
      if k == 'spec':
        self.spec = saveConfigSpec(task.pendingConfig.configurationSpec)
      elif k == 'changes':
        self.changes = saveResourceChanges(task.resourceChanges)
      elif k == 'messages':
        self.messages = task.messages
      elif k == 'parameters':
        self.parameters = serializeValue(task.pendingConfig.parameters)
      else:
        setattr(self, k, getattr(task.pendingConfig, k, v))

  def _initFromDict(self, src):
    for (k,v) in self.HeaderAttributes.items():
      setattr(self.changeRecord, k, src.get(k, v))
    self.status = Manifest.createStatus(src)
    for (k,v) in self.CommonAttributes.items():
      setattr(self, k, src.get(k, v))
    for (k,v) in self.RootAttributes.items():
      setattr(self, k, src.get(k, v))

  def dump(self):
    """
    convert dictionary suitable for serializing as yaml
    or creating a Changeset.
    """
    items = [(k, getattr(self.changeRecord, k)) for k in Changeset.HeaderAttributes]
    items.extend( saveStatus(self.status).items() )
    items.extend([(k, getattr(self, k)) for k in Changeset.CommonAttributes
                      if getattr(self, k)]) #skip empty values
    items.extend([(k, getattr(self, k)) for k in Changeset.RootAttributes
                          if getattr(self, k)] #skip empty values
                  )
    #CommentedMap so order is preserved in yaml output
    return CommentedMap(items)

# +./ +../ +/
# +%: +url:ffff#fff:
class YamlManifest(Manifest):
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
    self.changeSets = dict((c['changeId'], Changeset(c)) for c in  manifest.get('changes', []))
    lastChangeId = self.changeSets and max(self.changeSets.keys()) or 0

    templates = manifest.get('templates', {})
    rootResource = self.createResource('root', manifest['root'], None,
                    templates.get('resources'), templates.get('configurations'))
    super(YamlManifest, self).__init__(rootResource, self.specs, lastChangeId)

  @staticmethod
  def _getResourcePath(resource):
    return reduce(lambda x, y: x + y,
        ((p.name, 'resources') for p in resource.parents), ()) + (resource.name,)

  def _getConfigPath(self, changeset):
    assert changeset
    resource = self.rootResource.findResource(changeset.resourceName)
    assert resource
    return self._getResourcePath(resource) + ('spec', 'configurations', changeset.configName)

  def saveResource(self, resource, workDone):
    status = saveStatus(resource)
    status['attributes'] = resource._attributes
    if resource.createdOn: #will be a ChangeRecord
      status['createdOn'] = resource.createdOn.changeId
    if resource.createdFrom:
      insertKey = self._getResourcePath(resource) + ('spec',)
      if insertKey not in self.includes:
        # if this resource is being added for the first time and it's from a template
        # create a merge include directive pointing at the template
        configPath = '/'.join(self._getConfigPath(resource.createdOn))
        includePath = '+' + configPath + '/provides/' + resource.createdFrom
        self.includes[insertKey] = [(includePath, None)]
      status['createdFrom'] = resource.createdFrom

    status['configurations'] = CommentedMap(map(self.saveConfiguration, resource.effectiveConfigurations.values()))
    spec = saveResourceSpec(resource.spec)
    return (resource.name, copy.deepcopy(CommentedMap([("spec", spec),
      ("resources", CommentedMap(map(lambda r: self.saveResource(r, workDone), resource.resources))),
      ("status", status),
    ])))

  def saveConfiguration(self, config):
    spec = config.configurationSpec
    status = saveStatus(config)
    status['parameters'] = serializeValue(config.parameters)
    status["spec"] = saveConfigSpec(spec)
    status['modifications'] = saveResourceChanges(config.resourceChanges)
    status['dependencies'] = [saveDependency(val) for val in config.dependencies.values()]
    return (config.name, status)

  def saveTask(self, task):
    """
    convert dictionary suitable for serializing as yaml
    """
    return Changeset(task).dump()

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
