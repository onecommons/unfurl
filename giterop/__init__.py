import json
import os
import os.path
import sys
import tempfile
import subprocess
from .util import *
from .manifest import *
from . import ansible
import six

"""
Basic operation of GitErOp is to apply the specified configuration to a resource
and record the results of the application.

A manifest can contain a reproducible history of changes to a resource.
This history is stored in the resource definition so it doesn't
rely on git history for this.
But the intent is for commits in a git repo to correspond to reproducible configuration states of the system.
The git repo is also used to record or archive exact versions of each configurators applied.
"""

class Task(object):
  def __init__(self, runner, configuration, resource, action):
    self.runner = runner
    self.changes = None
    self.configuration = configuration
    # configuration might be for child resource
    self.action = action
    self.resource = configuration.getResource(resource)
    self.parameters = configuration.getParams()
    self.configurator = configuration.configurator.getConfigurator()

  def shouldRun(self):
    return self.configurator.shouldRun(
        self.configuration.getAction(self.action),
        self.resource,
        self.parameters)

  def canRun(self):
    if self.configuration.configurator.missingRequirements(self.resource):
      return False
    return self.configurator.canRun(
        self.configuration.getAction(self.action),
        self.resource,
        self.parameters)

  def run(self):
    status = self.configurator.run(
        self.configuration.getAction(self.action),
        self.resource,
        self.parameters)
    provided = self.configuration.configurator.missingExpected(self.resource)
    #XXX
    #self.changes = self.resource.getChanges()
    #return self.changes
    return False

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
    clusterCount = len(manifest.resources)
    if not clusterCount:
      raise GitErOpError("no root resources found in manifest")
    elif resourceName is not None:
      cluster = manifest.getRootResource(resourceName)
      if not cluster:
        raise GitErOpError("couldn't find root resouce %s in manifest" % resourceName)
      return [cluster]
    return manifest.resources[:]

  def save(self, task, changes):
    #update cluster with last success
    #commit manifest
    self.currentTask = None
    self.changes.append(changes)

  def saveError(self, err, msg=''):
    self.aborted = err

  def getNeededTasksForResource(self, resource, action=None):
    tasks = []
    for configuration in resource.spec.configurations:
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

#cmd=update find and run configurations that need to be applied
#for running and recording adhoc changes:
#cmd=add resource configuration action params
def run(manifestPath, opts=None):
  manifest = Manifest(path=manifestPath)
  runner = Runner(manifest)
  kw = opts or {}
  runner.run(**kw)
