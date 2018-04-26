import six

from .util import *
from .manifest import *
from . import ansible

#from dictdiffer import diff, patch, swap, revert

class Change(object):
  def __init__(self, *args):
    pass

  def getNewResources(self):
    return []

class Task(object):
  def __init__(self, runner, configuration, resource, action):
    self.runner = runner
    self.changes = None
    self.configuration = configuration
    self.action = configuration.getAction(action)
    self.resource = resource #configuration.getResource(resource)# XXX .copy()
    self.parameters = configuration.getParams()
    self.configurator = configuration.configurator.getConfigurator()
    self.previousRun = self.getLastChange()
    self.messages = []

  def shouldRun(self):
    return self.configurator.shouldRun(self)

  def canRun(self):
    if self.configuration.configurator.findMissingRequirements(self.resource):
      return False
    return self.configurator.canRun(self)

  def _createChangeRecord(self, status, providedStatus):
    #compare resource
    diff = None #diffdict(self.resource.metadata, self.__resource.metadata)
    #for op in diff:
    #  self.checkForConflict(ValueRef([resource, op[1]]).getProvence())
    resourceChanges = None #self.resourceChanges
    return Change(status, providedStatus, diff, resourceChanges)

  def run(self):
    status = self.configurator.run(self)
    notProvided = self.configuration.configurator.findMissingProvided(self.resource)
    #XXX revert changes if status.failure or have configurator do that?
    self.changes = self._createChangeRecord(status, notProvided)
    #XXX self.changes.applyToResource(self.__resource)
    return self.changes

  def addMessage(self, message): pass
  def createResource(self, resource): pass
  def deleteResource(self, resource): pass
  def discoverResource(self, resource): pass
  def forgetResource(self, resource): pass

  def getLastChange(self):
    # for change in reversed(self.resource.history):
    #   if change.configurator == self.configurator: #XXX
    #     return change
    return None

# XXX need this??
#  def commit(self):
#    return resource.applyUpdates(self.updates)

class Runner(object):
  def __init__(self, manifest):
    if isinstance(manifest, six.string_types):
      self.manifest = Manifest(manifest)
    else:
      self.manifest = manifest
    self.reset()

  def reset(self):
    self.aborted = None
    self.currentTask = None
    self.changes = []

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

  def save(self, task, changes):
    #update cluster with last success
    #commit manifest
    self.currentTask = None
    self.changes.append(changes)

  def saveError(self, err, msg=''):
    self.aborted = err

  def getNeededTasksForResource(self, resource, action=None):
    tasks = []
    for configuration in resource.definition.spec.configurations:
      # check status, discover or instantiate
      task = Task(self, configuration, resource, action)
      if task.shouldRun():
        tasks.append(task)
    return tasks

  def abortIfCantRun(self, tasks):
    for task in tasks:
      if not task.canRun():
        self.saveError(GitErOpTaskError(task, "cannot run"))
        return True
    return False

  def getNeededTasks(self, resources, action=None):
    allTasks = []
    for resource in resources:
      tasks = self.getNeededTasksForResource(resource, action)
      if self.abortIfCantRun(tasks):
        return []
      allTasks.extend(tasks)
    return allTasks

  def run(self, **opts):
    self.reset()
    try:
      manifest = self.manifest
      action = 'discover' if opts.get('readonly') else None
      #XXX before running commit manifest if it has changed, else verify git access to this manifest
      #XXX resource option shouldn't have to be root
      resources = self.getRootResources(opts.get('resource'))
      tasks = self.getNeededTasks(resources, action)
      if self.aborted:
        return False
      while tasks:
        task = tasks.pop(0)
        self.currentTask = task
        changes = task.run()
        self.save(task, changes)
        if changes:
          # examine new (XXX what about changed?) resources for configurations
          updatedResources = changes.getNewResources()
          if updatedResources:
            moreTasks = self.getNeededTasks(updatedResources, action)
            if self.aborted:
              return False
            if moreTasks:
              #run them before the next configuration
              tasks[0:0] = moreTasks
    except Exception as e:
      self.saveError(e)
      return False
    else:
      return True
