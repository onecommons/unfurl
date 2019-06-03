"""Loads and saves a GitErOp manifest with the following format:

.. code-block:: YAML

  apiVersion: VERSION
  kind: Manifest
  imports:
    name: # manifest to represent as a resource
      file: # if is missing, manifest must declared in local config
      repository:
      commit:
      resource: name # default is root
      attributes: # queries into manifest
      properties: # expected JSON schema for attributes
    # locals and secrets are special cases that must be defined in local config
    locals:
     properties:
       name1:
         type: string
         default
     required:
    secrets:
     properties:
       name1:
         type: string
         default
       # save-digest: true
       # prompt: true
       # readonly: true
     required:

  spec:
    tosca:
      # <tosca service template>
    inputs:
    instances:
      name:
        template:
        implementations:
          action: implementationName
    implementations:
      name1:
        className
        version
        intent
        inputs:
  status:
    # root resource will tbe replaced with:
    # inputs:
    # outputs:
    # template: repo:topology_template:revision
    # operatingStatus
    # resources:
    root: # root resource is always named "root"
      template: repo:templateName:revision
      attributes:
        .interfaces:
          interfaceName: foo.bar.ClassName
      capabilities:
        - name:
          attributes:
          operatingStatus
          lastChange: changeId
      requirements:
       - name:
         operatingStatus
         lastChange
      readyState:
        effective:
        local:
        lastConfigChange: changeId
        lastStateChange: changeId

      priority
    resources:
      child1:
        # ...
  changes:
    - changeId
      startTime
      commitId
      parentId # allows execution plan order to be reconstructed
      previousId # XXX
      target
      readyState
      priority
      resource
      config
      action
      implementation:
        type: resource | artifact | class
        key: repo:key#commitid | className:version
      inputs
      dependencies:
        - ref: ::resource1::key[~$val]
          expected: "value"
        - name: named1
          ref: .configurations::foo[.operational]
          required: true
          schema:
            type: array
      changes:
        resource1:
          .added: # set if added resource
          .status: # set when adding or removing
          foo: bar
        resource2:
          .spec:
          .status: notpresent
        resource3/child1: +%delete
      messages: []
"""

import six
import copy
import sys
import collections
import os.path

from .util import (GitErOpError, VERSION, restoreIncludes, patchDict)
from .yamlloader import YamlConfig, loadFromRepo
from .result import serializeValue, ChangeRecord
from .support import ResourceChanges, Status, LocalEnv, Priority, Action, Defaults
from .tosca import ToscaSpec
from .job import JobOptions, Runner
from .manifest import Manifest, ChangeRecordAttributes
from .runtime import Resource

from ruamel.yaml.comments import CommentedMap
from codecs import open
from six.moves import reduce

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
      "allOf": [
        { "$ref": "#/definitions/status" },
        {"properties": {
          "attributes": {
            "$ref": "#/definitions/attributes",
            "default": {}
           },
          "resources": {
            "allOf": [
              { "$ref": "#/definitions/namedObjects" },
              {"additionalProperties": { "$ref": "#/definitions/resource" }}
            ],
            'default': {}
          },
        }}
      ]
     },
    "configurationSpec": {
      "type": "object",
      "properties": {
        "className": {"type":"string"},
        "majorVersion": {"anyOf": [{"type":"string"}, {"type":"number"}]},
        "minorVersion": {"type":"string"},
        "intent": { "enum": list(Action.__members__) },
        "inputs": {
          "$ref": "#/definitions/attributes",
          "default": {}
         },
        "preconditions": {
          "$ref": "#/definitions/schema",
          "default": {}
         },
        # "provides": {
        #   "type": "object",
        #   "properties": {
        #     ".self": {
        #       "$ref": "#/definitions/resource/properties/spec" #excepting "configurations"
        #      },
        #     ".configurations": {
        #       "allOf": [
        #         { "$ref": "#/definitions/namedObjects" },
        #         # {"additionalProperties": { "$ref": "#/definitions/configurationSpec" }}
        #       ],
        #       'default': {}
        #     },
        #   },
        #   # "additionalProperties": { "$ref": "#/definitions/resource/properties/spec" },
        #   'default': {}
        # },
      },
      "required": ["className"]
    },
    "configurationStatus": {
      "type": "object",
      "allOf": [
        { "$ref": "#/definitions/status" },
        { "properties": {
            "action": { "enum": list(Action.__members__) },
            "inputs": {
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
  },
  # end definitions
  "type": "object",
  "properties": {
    "apiVersion": { "enum": [ VERSION ] },
    "kind":   { "enum": [ "Manifest" ] },
    "spec":    {"type": "object"},
    "status": {
               "type": "object",
               "properties": {
                  "root": { "$ref": "#/definitions/resource" },
                },
                'default': {}
              },
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
              "implementation":  {"$ref": "#/definitions/configurationSpec" },
            },
            "required": ["changeId"]
          },
        ]
      }
    }
  },
  "required": ["apiVersion", "kind", "spec"]
}

Import = collections.namedtuple('Import', ['resource', 'spec'])

def saveConfigSpec(spec):
  saved = CommentedMap([
    ("intent", spec.intent.name),
    ("className", spec.className),
  ])
  if spec.majorVersion:
    saved["majorVersion"] = spec.majorVersion
  if spec.minorVersion:
    saved["minorVersion"] = spec.minorVersion
  if spec.parameters:
    saved['inputs'] = spec.parameters
  # if spec.provides:
  #   dotSelf = spec.provides.get('.self')
  #   if dotSelf:
  #     # removed defaults put in by schema
  #     dotSelf.pop('configurations', None)
  #     if not dotSelf.get('attributes'):
  #       dotSelf.pop('attributes', None)
  #   saved["provides"] = spec.provides
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

def saveResourceChanges(changes):
  d = CommentedMap()
  for k, v in changes.items():
    d[k] = v[ResourceChanges.attributesIndex] or {}
    if v[ResourceChanges.statusIndex] is not None:
      d[k]['.status'] = v[ResourceChanges.statusIndex].name
    if v[ResourceChanges.addedIndex]:
      d[k]['.added'] = v[ResourceChanges.addedIndex]
  return d

def saveStatus(operational, status=None):
  readyState = CommentedMap([
    ("effective", operational.status.name),
  ])
  if operational.localStatus is not None:
    readyState['local'] =  operational.localStatus.name

  if status is None:
    status = CommentedMap()
  status["readyState"] = readyState
  if operational.priority and operational.priority != Defaults.shouldRun:
    status["priority"] = operational.priority.name
  if operational.lastStateChange:
    status["lastStateChange"] = operational.lastStateChange
  if operational.lastConfigChange:
    status["lastConfigChange"] = operational.lastConfigChange

  return status

def saveTask(task):
  """
  convert dictionary suitable for serializing as yaml
  or creating a Changeset.
  """
  items = [(k, getattr(task, k)) for k in ChangeRecordAttributes]
  items.extend( saveStatus(task).items() )
  output = CommentedMap(items) #CommentedMap so order is preserved in yaml output
  if task.previousConfigChangeId:
    output['previousId'] = task.previousConfigChangeId
  output['implementation'] = saveConfigSpec(task.configSpec)
  changes = saveResourceChanges(task._resourceChanges)
  if changes:
    output['changes'] = changes
  output['messages'] = task.messages
  dependencies = [saveDependency(val) for val in task.dependencies.values()]
  if dependencies:
    output['dependencies'] = dependencies
  result = task.result.result
  if result:
    output['result'] = result

  return output


# +./ +../ +/
# +%: +url:ffff#fff:
class YamlManifest(Manifest):
  def __init__(self, manifest=None, path=None, validate=True, localEnv=None):
    assert not (localEnv and (manifest or path)) # invalid combination of args
    self.localEnv = localEnv
    self.manifest = YamlConfig(manifest, path or localEnv and localEnv.path,
                                    validate, schema, self.loadFromRepo)
    manifest = self.manifest.expanded

    spec = manifest.get('spec', {})
    if 'tosca' in spec:
      toscaDef = spec['tosca']
    elif 'node_templates' in spec:
      # allow node_templates shortcut
      toscaDef = {'node_templates': spec['node_templates']}
    else:
      toscaDef = {}
    super(YamlManifest, self).__init__(ToscaSpec(toscaDef, spec, self.manifest.path))
    assert self.tosca
    inputs = spec.get('inputs', {})

    self.changeSets = dict((c['changeId'], c) for c in  manifest.get('changes', []))
    lastChangeId = self.changeSets and max(self.changeSets.keys()) or 0

    status = manifest.get('status', {})
    rootResource = self.createTopologyResource(status, inputs)
    for name, instance in spec.get('instances', {}).items():
       if not rootResource.findResource(name):
         self.loadResource(name, instance or {}, parent=rootResource)

    importsSpec = manifest.get('imports', {})
    if localEnv:
      importsSpec.setdefault('local', {})
      importsSpec.setdefault('secret', {})
    rootResource.imports = self.loadImports(importsSpec)
    # XXX should baseDir be relative to the tosca template -- isn't that where artifacts would live?
    rootResource.baseDir = self.getBaseDir()

    self._ready(rootResource, lastChangeId)

  def getBaseDir(self):
    return self.manifest.getBaseDir()

  def loadFromRepo(self, templatePath, baseDir):
    name = 'spec'
    if isinstance(templatePath, dict):
      repo = templatePath.get('repository', {}).copy()
      name = repo.pop('name', name)
      repositories = {name: repo}

    context = {}
    path, template = loadFromRepo(name, templatePath, baseDir, repositories, context)
    return template

  def createTopologyResource(self, status, inputs):
    """
    If an instance of the toplogy is recorded in status, load it,
    otherwise create a new resource using the inputs in the spec
    """
    if 'readyState' not in status:
      # create a root resource from the topologies inputs: properties, outputs: attributes
      # XXX use the substitution_mapping (3.8.12) represent the resource
      #XXX: validate values with input.schema
      decl = dict(template = 'self:#topology:0',
                  attributes = dict(inputs=inputs, outputs={}))
    else:
      decl = status.copy()
      decl['attributes'] = dict(inputs=status.get('inputs', {}),
                                outputs=status.get('outputs', {}))
    return self.loadResource('root', decl, None)

  def saveNodeInstance(self, resource):
    status = CommentedMap()
    status['template'] =  resource.template.getUri()
    status['attributes'] = resource._attributes
    saveStatus(resource, status)
    if resource.createdOn: #will be a ChangeRecord
      status['createdOn'] = resource.createdOn.changeId
    return (resource.name, status)

  def saveResource(self, resource, workDone):
    name, status = self.saveNodeInstance(resource)
    status['capabilities'] = CommentedMap(map(self.saveNodeInstance, resource.capabilities))
    status['resources'] = CommentedMap(map(lambda r: self.saveResource(r, workDone), resource.resources))
    return (name, status)

  def saveJob(self, job):
    changes = map(saveTask, job.workDone.values())
    changed = CommentedMap([self.saveResource(self.rootResource, job.workDone)])
    # update changed with includes, this may change objects with references to these objects
    restoreIncludes(self.manifest.includes, self.manifest.config, changed, cls=CommentedMap)
    # modify original to preserve structure and comments
    if 'status' not in self.manifest.config:
      self.manifest.config['status'] = {}

    # XXX imported resources need to include its repo's workingdir commitid its status
    # status and job's changeset also need to save status of repositories
    # that were accessed by loadFromRepo() and add them with commitid and repotype
    # note: initialcommit:requiredcommit means any repo that has at least requiredcommit

    # XXX replace root with inputs and outputs
    if 'root' not in self.manifest.config['status']:
      self.manifest.config['status']['root'] = changed['root']
    else:
      patchDict(self.manifest.config['status']['root'], changed['root'], cls=CommentedMap)
    self.manifest.config.setdefault('changes', []).extend(changes)

    if job.out:
      self.dump(job.out)
    elif self.manifest.path:
      with open(self.manifest.path, 'w') as f:
        self.dump(f)
    else:
      output = six.StringIO()
      self.dump(output)
      job.out = output

  def dump(self, out=sys.stdout):
    try:
      # remove extraneous elements added processing 'copy' element
      self.manifest.config.pop('+copy-contents', None)
      self.manifest.config.pop('copy-contents', None)
      self.manifest.dump(out)
    except:
      raise GitErOpError("Error saving manifest %s" % self.manifest.path, True)

  def loadImports(self, importsSpec):
    """
      file: local/path # for now
      repository: uri or repository name in TOSCA template
      commitId:
      resource: name # default is root
      attributes: # queries into resource
      properties: # expected schema for attributes
    """
    imports = {}
    for name, value in importsSpec.items():
      resource = self.localEnv and self.localEnv.getLocalResource(name, value)
      if not resource:
        if 'file' not in value:
          raise GitErOpError("Can not import '%s': no file specified" % (name))
        # use tosca's loader instead, this can be an url into a repo
        # if repo is url to a git repo, find or create working dir
        repositories = self.tosca.template.tpl.get('repositories',{})
        context = {'repoType': 'instance'} # we know we are loading an instance repo
        path, yamlDict = loadFromRepo(name, value, self.getBaseDir(), repositories, context)
        imported = YamlManifest(yamlDict, path=path)
        rname = value.get('resource', 'root')
        resource = imported.getRootResource().findResource(rname)
      if not resource:
        raise GitErOpError("Can not import '%s': resource '%s' not found" % (name, rname))
      imports[name] = Import(resource, value)
    return imports

def runJob(manifestPath=None, opts=None):
  localEnv = LocalEnv(manifestPath, opts and opts.get('home'))
  manifest = YamlManifest(localEnv=localEnv)
  runner = Runner(manifest)
  kw = opts or {}
  return runner.run(JobOptions(**kw))
