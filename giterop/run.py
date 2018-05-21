import six
import sys
import datetime
import json
from ruamel.yaml.comments import CommentedMap

from .util import *
from .manifest import *
from .resource import *

class Change(object):
  def __init__(self, job, resource, rootChange=None, **kw):
    self.job = job
    self.resource = resource
    self.rootChange = rootChange
    self.childMetadataChanges = []
    leftOver = self.mergeAttr(ChangeRecord.HeaderAttributes, kw)
    leftOver = self.mergeAttr(ChangeRecord.CommonAttributes, leftOver)
    if rootChange:
      #this is a child Change
      self.masterResource = rootChange.resource.name
      self.startTime = rootChange.startTime
      self.changeId = rootChange.changeId
    else:
      self.masterResource = None
      leftOver = self.mergeAttr(ChangeRecord.RootAttributes, leftOver)
      self.startTime = job.startTime.isoformat()
      self.changeId = job.changeId
      self.action = job.action
      self.configuration = CommentedMap(
        [('name', job.configuration.name),
          ('digest', job.configuration.digest())])
      # self.parameters = job.parameters
      #  update revision when digest changes?
      # }
      #previously: commitid+
      #applied: commitid

  def mergeAttr(self, attrs, kw):
    for (k, v) in attrs.items():
      setattr(self, k, kw.pop(k, v))
    return kw

  def toSource(self):
    """
    convert dictionary suitable for serializing as yaml
    or creating a ChangeRecord.
    """
    items = [(k, getattr(self, k)) for k in ChangeRecord.HeaderAttributes]
    if self.masterResource:
      items.append( ('masterResource', self.masterResource) )
    else:
      items.extend([(k, getattr(self, k)) for k in ChangeRecord.RootAttributes
                            if getattr(self, k)] #skip empty values
                  )
    items.extend([(k, getattr(self, k)) for k in ChangeRecord.CommonAttributes
                      if getattr(self, k)]) #skip empty values
    #CommentedMap so order is preserved in yaml output
    return CommentedMap(items)

  def __repr__(self):
    return "<Change for %s: %s)>" % (self.resource.name, json.dumps(self.toSource()))

  def record(self):
    changeRecord = ChangeRecord(self.resource.definition, self.toSource() )
    self.resource.definition.changes.append( changeRecord )
    return changeRecord

class Task(object):
  def __init__(self, runner, configuration, resource, action, startTime=None):
    self.runner = runner
    self.change = None
    self.configuration = configuration
    self.action = configuration.getAction(action)
    self.resource = resource #configuration.getResource(resource) XXX?
    self.parameters = configuration.getParams(resource)
    self.configurator = configuration.configurator.getConfigurator()
    self.previousRun = self.getLastChange()
    self.messages = []
    self.addedResources = []
    self.removedResources = []
    self.changeId = None
    self.startTime = startTime or datetime.datetime.now()

  def getAddedResources(self):
    return [a[1] for a in self.addedResources]

  def shouldRun(self):
    return self.configurator.shouldRun(self)

  def canRun(self):
    if self.configuration.configurator.findMissingRequirements(self.resource):
      return False
    return self.configurator.canRun(self)

  def findMetadataChanges(self, resource, changes):
    diffs = resource.diffMetadata()
    #for op in diffs:
    #  self.checkForConflict(ValueFrom([resource, op[1]]).getProvence())
    if diffs:
      changes.append( (resource, diffs) )
    for child in resource.resources:
      self.findMetadataChanges(child, changes)

  def _createChangeRecord(self, resource, diff, rootChange, action):
    return Change(self, resource, metadata=diff, rootChange=rootChange, action=action)

  def _createRootChangeRecord(self, status, providedStatus):
    self.changeId = self.runner.incrementChangeId()
    change = Change(self, self.resource, status=status, messages=self.messages)
    if providedStatus:
      change.failedToProvide = providedStatus

    metadataChanges = []
    self.findMetadataChanges(self.resource, metadataChanges)
    if metadataChanges and metadataChanges[0][0] is self.resource:
      change.metadata = metadataChanges.pop(0)[1]

    for (action, resource) in (self.removedResources + self.addedResources):
      change.resources.setdefault(action,[]).append(resource.name)

    change.childMetadataChanges = metadataChanges
    for (resource, diffs) in metadataChanges:
      change.resources.setdefault("modified",[]).append(resource.name)

    return change

  def commitChanges(self, rootChange):
    self.resource.commitMetadata()
    rootChange.record()

    for (resource, diff) in rootChange.childMetadataChanges:
      resource.commitMetadata()
      change = self._createChangeRecord(resource, diff, rootChange, "modified")
      change.record()

    for (action, resource) in self.removedResources:
      parent = resource.parent
      if parent:
        parent = parent.definition
      else:
        if resource.name in self.resource.definition._resources:
          parent = self.resource.definition
        else:
          isRootResource = self.runner.manifest.getRootResources(resource.name)
          if isRootResource:
            parent = self.runner.manifest
      if parent:
        del parent._resources[resource.name]
      #else:
      # warn("cant find removed resource %s" % resource.name)

    for (action, resource) in self.addedResources:
      parent = resource.parent or self.resource
      parent.definitions._resources[resource.name] = resource.definition
      #add an initial change record
      resource.commitMetadata()
      change = self._createChangeRecord(resource, diff, rootChange)
      change.record()

  def run(self):
    try:
      status = self.configurator.run(self)
    except Exception as err:
      raise GitErOpTaskError(self, sys.exc_info())

    missingProvided = self.configuration.configurator.findMissingProvided(self.resource)
    #XXX revert changes if status.failure or have configurator do that?
    self.change = self._createRootChangeRecord(status, missingProvided)
    return self.change

  def addMessage(self, message):
    self.messages.append(message)

  def createResource(self, resourceSpec):
    resource = ResourceDefinition(self.resource.definition, resourceSpec)
    self.addedResources.append(('created', resource.resource))
    return resource.resource

  def discoverResource(self, resourceDef):
    resource = ResourceDefinition(self.resource.definition, resourceSpec)
    self.addedResources.append(('discovered', resource.resource))
    return resource.resource

  def deleteResource(self, resource):
    self.removedResources.append(('deleted', resource))

  def forgetResource(self, resourceName):
    self.removedResources.append(('forgot', resource))

  def getLastChange(self):
    for change in reversed(self.resource.changes):
      if change.configuration and change.configuration['name'] == self.configuration.name:
        return change
    return None

class Runner(object):
  """
  Definition of system changed.
  System has not reached desired state.
  System state has changed.
  """
  def __init__(self, manifest):
    if isinstance(manifest, six.string_types):
      self.manifest = Manifest(manifest)
    else:
      self.manifest = manifest
    self.currentChangeId = self.manifest.findMaxChangeId() + 1
    self.reset()

  def reset(self):
    self.aborted = None
    self.currentTask = None
    self.changes = []
    self.startTime = None

  def incrementChangeId(self):
    #changeids are shared across dependent changes on multiple resources
    #should be unique across manifest and should monotonically increased
    self.currentChangeId += 1
    return self.currentChangeId

  def getRootResources(self, resourceName=None):
    manifest = self.manifest
    resourceCount = len(manifest.resources)
    if not resourceCount:
      raise GitErOpError("no root resources found in manifest")
    elif resourceName is not None:
      resource = manifest.getRootResource(resourceName)
      if not resource:
        raise GitErOpError("couldn't find root resouce %s in manifest" % resourceName)
      return [resource.resource]
    return [r.resource for r in manifest.resources]

  def save(self, task, change):
    task.commitChanges(change)
    #update cluster with last success
    #commit manifest
    self.currentTask = None
    self.changes.append(change)
    self.manifest.save()

  def saveError(self, err, msg=''):
    self.aborted = err
    # rollback metadata changes??
    #XXX

  def saveDone(self):
    pass #xxx

  def getTasksForResource(self, resource, action=None):
    for configuration in resource.definition.spec.configurations:
      # check status, discover or instantiate
      task = Task(self, configuration, resource, action, self.startTime)
      if task.shouldRun():
        yield task
    for task in self.getTasks(resource.resources, action):
      yield task

  def getTasks(self, resources, action=None):
    for resource in resources:
      for task in self.getTasksForResource(resource, action):
        yield task

  def abortRun(self, task):
    return False

  def runTasks(self, resources):
    for task in self.getTasks(resources):
      self.currentTask = task
      if task.canRun():
        task.run()
        change = task.run()
        self.save(task, change)

      if self.abortRun(task):
        return False
      #run tasks from newly generated resources before the next task
      if task.addedResources and not self.runTasks(task.addedResources):
        return False
    return True

  def run(self, **opts):
    self.reset()
    self.startTime = opts.get('startTime')
    action = 'discover' if opts.get('readonly') else None
    #XXX before running commit manifest if it has changed, else verify git access to this manifest
    try:
      #XXX resource option shouldn't have to be root
      resources = self.getRootResources(opts.get('resource'))
      self.success = self.runTasks(resources)
    except Exception as e:
      self.saveError(sys.exc_info())
      return False
    else:
      self.saveDone()
      return self.success
