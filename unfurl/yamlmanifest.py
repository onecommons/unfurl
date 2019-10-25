"""Loads and saves a Unfurl manifest with the following format:

.. code-block:: YAML

  apiVersion: VERSION
  kind: Manifest
  imports:
    name: # manifest to represent as a resource
      file: # if is missing, manifest must declared in local config
      repository:
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
    topology: repo:topology_template:revision
    inputs:
    outputs:
    readyState: #, etc.
    instances:
      name: # root resource is always named "root"
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
  repositories
  changeLog: changes.yaml
  latestChanges:
    jobId: 1
    startCommit: ''
    startTime:
    specDigest:
    lastChangeId:
    readyState:
      effective: error
      local: ok

# in changes.yaml:
  - jobId: 1
    startCommit:
    endCommit: ''
    startTime:
    specDigest:
    tasksRun:
    readyState:
      effective: error
      local: ok
  - changeId: 2
    startTime
    parentId: 1 # allows execution plan order to be reconstructed
    previousId # XXX last time this configuration ran
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
import sys
import collections
import numbers
import os.path
import itertools

from .util import (UnfurlError, VERSION, toYamlText, restoreIncludes, patchDict)
from .yamlloader import YamlConfig, loadFromRepo, load_yaml, yaml
from .result import serializeValue
from .support import ResourceChanges, Status, Priority, Action
from .localenv import LocalEnv
from .job import JobOptions, Runner
from .manifest import Manifest, ChangeRecordAttributes
from .runtime import TopologyResource, Resource

from ruamel.yaml.comments import CommentedMap
from codecs import open

import logging
logger = logging.getLogger('unfurl')

# XXX3 add as file to package data
#schema=open(os.path.join(os.path.dirname(__file__), 'manifest-v1alpha1.json')).read()
schema = {
  "$schema": "http://json-schema.org/draft-04/schema#",
  "$id": "https://www.onecommons.org/schemas/unfurl/v1alpha1.json",
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
          "template": {"type": "string"},
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
      "allOf": [
        {"properties": {
          "topology": { "type": "string" },
          "inputs": {
            "$ref": "#/definitions/attributes",
           },
          "outputs": {
            "$ref": "#/definitions/attributes",
          },
          "instances": {
            "allOf": [
              { "$ref": "#/definitions/namedObjects" },
              {"additionalProperties": { "$ref": "#/definitions/resource" }}
            ],
          },
        }},
        { "$ref": "#/definitions/status" },
      ],
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
  if status is None:
    status = CommentedMap()
  if not operational.lastChange and operational.status == Status.notapplied:
    # skip status
    return status

  readyState = CommentedMap([
    ("effective", operational.status.name),
  ])
  if operational.localStatus is not None:
    readyState['local'] =  operational.localStatus.name
  status["readyState"] = readyState
  if operational.priority: #and operational.priority != Defaults.shouldRun:
    status["priority"] = operational.priority.name
  if operational.lastStateChange:
    status["lastStateChange"] = operational.lastStateChange
  if operational.lastConfigChange:
    status["lastConfigChange"] = operational.lastConfigChange

  return status

def saveConfigChange(configChange):
  items = [(k, getattr(configChange, k)) for k in ChangeRecordAttributes]
  items.extend( saveStatus(configChange).items() )
  return CommentedMap(items) #CommentedMap so order is preserved in yaml output

def saveResult(value):
  if isinstance(value, collections.Mapping):
    return CommentedMap((key, saveResult(v)) for key, v in value.items())
  elif isinstance(value, (collections.MutableSequence, tuple)):
    return [saveResult(item) for item in value]
  elif value is not None and not isinstance(value, (numbers.Real, bool)):
    return toYamlText(value)
  else:
    return value

def saveTask(task):
  """
  convert dictionary suitable for serializing as yaml
  or creating a Changeset.
  """
  output = CommentedMap()
  output['changeId'] = task.changeId
  output['parentId'] = task.parentId
  if task.target:
    output['target'] = task.target.key
  saveStatus(task, output)
  output['implementation'] = saveConfigSpec(task.configSpec)
  if task.inputs:
    output['inputs'] = serializeValue(task.inputs)
  changes = saveResourceChanges(task._resourceChanges)
  if changes:
    output['changes'] = changes
  if task.messages:
    output['messages'] = task.messages
  dependencies = [saveDependency(val) for val in task.dependencies.values()]
  if dependencies:
    output['dependencies'] = dependencies
  if task.result:
    result = task.result.result
    if result:
      output['result'] = saveResult(result)
  else:
    output['result'] = "skipped"

  return output

# +./ +../ +/
class YamlManifest(Manifest):

  def __init__(self, manifest=None, path=None, validate=True, localEnv=None):
    assert not (localEnv and (manifest or path)) # invalid combination of args
    self.localEnv = localEnv
    self.repo = localEnv and localEnv.instanceRepo
    self.manifest = YamlConfig(manifest, path or localEnv and localEnv.manifestPath,
                                    validate, schema, self.loadHook)
    manifest = self.manifest.expanded
    spec = manifest.get('spec', {})
    super(YamlManifest, self).__init__(spec, self.manifest.path, localEnv)
    assert self.tosca
    status = manifest.get('status', {})

    self.changeLogPath = manifest.get('changeLog')
    if self.changeLogPath:
      fullPath = os.path.join(self.getBaseDir(), self.changeLogPath)
      if os.path.exists(fullPath):
        changelog = load_yaml(fullPath)
        changes = changelog.get('changes', [])
      else:
        logger.warning("missing changelog: %s", fullPath)
        changes = manifest.get('changes', [])
    else:
      changes = manifest.get('changes', [])
      if localEnv:
        # save changes to a separate file if we're in a local environment
        self.changeLogPath = 'changes.yaml'

    self.changeSets = dict( (c.get('changeId', c.get('jobId', 0)), c) for c in changes)
    lastChangeId = self.changeSets and max(self.changeSets.keys()) or 0

    rootResource = self.createTopologyResource(status)
    for name, instance in spec.get('instances', {}).items():
      if not rootResource.findResource(name):
        # XXX like Plan.createResource() parent should be hostedOn target if defined
        self.loadResource(name, instance or {}, parent=rootResource)

    importsSpec = manifest.get('imports', {})
    if localEnv:
      importsSpec.setdefault('local', {})
      importsSpec.setdefault('secret', {})
    rootResource.imports = self.loadImports(importsSpec)
    rootResource.setBaseDir(self.getBaseDir())

    self._ready(rootResource, lastChangeId)

  def getBaseDir(self):
    return self.manifest.getBaseDir()

  def createTopologyResource(self, status):
    """
    If an instance of the toplogy is recorded in status, load it,
    otherwise create a new resource using the the topology as its template
    """
    # XXX use the substitution_mapping (3.8.12) represent the resource
    template = self.tosca.topology
    operational = self.loadStatus(status)
    root = TopologyResource(template, operational)
    for key, val in status.get('instances', {}).items():
      self._createNodeInstance(Resource, key, val, root)
    return root

  def saveNodeInstance(self, resource):
    status = CommentedMap()
    status['template'] = resource.template.getUri()

    # only save the attributes that were set by the instance, not spec properties or attribute defaults
    # particularly, because these will get loaded in later runs and mask any spec properties with the same name
    if resource._attributes:
      status['attributes'] = resource._attributes
    saveStatus(resource, status)
    if resource.createdOn: #will be a ChangeRecord
      status['createdOn'] = resource.createdOn.changeId
    return (resource.name, status)

  def saveResource(self, resource, workDone):
    name, status = self.saveNodeInstance(resource)
    if resource.capabilities:
      status['capabilities'] = CommentedMap(map(self.saveNodeInstance, resource.capabilities))
    if resource.resources:
      status['resources'] = CommentedMap(map(lambda r: self.saveResource(r, workDone), resource.resources))
    return (name, status)

  def saveRootResource(self, workDone):
    resource = self.rootResource
    status = CommentedMap()

    # record the input and output values
    # XXX make sure sensative values are redacted (and need to check for 'sensitive' metadata)
    status['inputs'] = serializeValue(resource.inputs.attributes)
    status['outputs'] = serializeValue(resource.outputs.attributes)

    saveStatus(resource, status)
    # getOperationalDependencies() skips inputs and outputs
    status['instances'] = CommentedMap(map(lambda r: self.saveResource(r, workDone),
                                                resource.getOperationalDependencies()))
    return status

  def saveJobRecord(self, job):
    """
    jobId: 1 # should be last that ran, instead of first?
    startCommit: '' #commitId when job began
    startTime:
    specDigest:
    lastChangeId:
    readyState:
      effective: error
      local: ok
    """
    output = CommentedMap()
    output['jobId'] = job.changeId
    output['startTime'] = job.startTime
    if self.currentCommitId:
      output['startCommit'] = self.currentCommitId
    output['specDigest'] = self.specDigest
    output['lastChangeId'] = job.runner.lastChangeId
    return saveStatus(job, output)

  def saveJob(self, job):
    changed = self.saveRootResource(job.workDone)
    # XXX imported resources need to include its repo's workingdir commitid in their status
    # status and job's changeset also need to save status of repositories
    # that were accessed by loadFromRepo() and add them with commitid and repotype
    # note: initialcommit:requiredcommit means any repo that has at least requiredcommit

    # update changed with includes, this may change objects with references to these objects
    restoreIncludes(self.manifest.includes, self.manifest.config, changed, cls=CommentedMap)
    # modify original to preserve structure and comments
    if 'status' not in self.manifest.config:
      self.manifest.config['status'] = {}

    if not self.manifest.config['status']:
      self.manifest.config['status'] = changed
    else:
      patchDict(self.manifest.config['status'], changed, cls=CommentedMap)

    jobRecord = self.saveJobRecord(job)
    if job.workDone:
      self.manifest.config['latestChange'] = jobRecord
      changes = map(saveTask, job.workDone.values())
      if self.changeLogPath:
        self.manifest.config['changeLog'] = self.changeLogPath
      else:
        self.manifest.config.setdefault('changes', []).extend(changes)
    else:
      # no work was done, so bother recording this job
      changes = []

    if job.out:
      self.dump(job.out)
    else:
      output = six.StringIO()
      self.dump(output)
      job.out = output
      if self.manifest.path:
        with open(self.manifest.path, 'w') as f:
          f.write(output.getvalue())
    return jobRecord, changes

  def dump(self, out=sys.stdout):
    try:
      self.manifest.dump(out)
    except:
      raise UnfurlError("Error saving manifest %s" % self.manifest.path, True)

  def commitJob(self, job):
    if job.planOnly:
      return
    if job.dryRun:
      logger.info("printing results from dry run")
      if not job.out and self.manifest.path:
        job.out = sys.stdout
    jobRecord, changes = self.saveJob(job)
    if not changes:
      logger.info("job run didn't make any changes; nothing to commit")
      return
    if job.dryRun:
      return

    if self.repo:
      self.repo.repo.index.add([self.manifest.path])
      self.repo.repo.index.commit("Updating status for job %s" % job.changeId)
      jobRecord['endCommit'] = self.repo.revision
    if self.changeLogPath:
      self.saveChangeLog(jobRecord, changes)
      if self.repo:
        self.repo.repo.index.add([os.path.join(self.getBaseDir(), self.changeLogPath)])
        self.repo.repo.index.commit("Updating changelog for job %s" % job.changeId)
        logger.info("committed instance repo changes: %s", self.repo.revision)

  def saveChangeLog(self, jobRecord, newChanges):
    """
    manifest: manifest.yaml
    changes:
    """
    try:
      changelog = CommentedMap()
      changelog['manifest'] = os.path.basename(self.manifest.path)
      # put jobs before their child tasks
      key = lambda r: r.get('lastChangeId', r.get('changeId',0))
      changes = itertools.chain([jobRecord], newChanges, self.changeSets.values())
      changelog['changes'] = sorted(changes, key=key, reverse = True)
      output = six.StringIO()
      yaml.dump(changelog, output)
      fullPath = os.path.join(self.getBaseDir(), self.changeLogPath)
      logger.info("saving changelog to %s", fullPath)
      with open(fullPath, 'w') as f:
        f.write(output.getvalue())
    except:
      raise UnfurlError("Error saving changelog %s" % self.changeLogPath, True)

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
          raise UnfurlError("Can not import '%s': no file specified" % (name))
        # use tosca's loader instead, this can be an url into a repo
        # if repo is url to a git repo, find or create working dir
        repositories = self.tosca.template.tpl.get('repositories',{})
        # load an instance repo
        importDef = value.copy()
        path, yamlDict = loadFromRepo(name, importDef, self.getBaseDir(), repositories, self)
        imported = YamlManifest(yamlDict, path=path)
        rname = value.get('resource', 'root')
        resource = imported.getRootResource().findResource(rname)
        if 'inheritHack' in value:
          value['inheritHack']._attributes['inheritFrom'] = resource
          resource = value.pop('inheritHack')
      if not resource:
        raise UnfurlError("Can not import '%s': resource '%s' not found" % (name, rname))
      imports[name] = Import(resource, value)
    return imports

def runJob(manifestPath=None, _opts=None):
  _opts = _opts or {}
  localEnv = LocalEnv(manifestPath, _opts.get('home'))
  opts = JobOptions(**_opts)
  path = localEnv.manifestPath
  if opts.planOnly:
    logger.info("creating plan for %s", path)
  else:
    logger.info("running job for %s", path)

  logger.info('loading manifest at %s', path)
  try:
    manifest = YamlManifest(localEnv=localEnv)
  except Exception as e:
    logger.error('failed to load manifest at %s: %s', path, str(e),
                                  exc_info=opts.verbose >= 2)
    return None

  runner = Runner(manifest)
  return runner.run(opts)
