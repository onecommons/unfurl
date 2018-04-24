import json
import os
import os.path
import sys
import tempfile
import subprocess
from .util import *
from .manifest import *
from . import ansible

"""
Basic operation of GitErOp is to apply the specified configuration to a resource
and record the results of the application.

A manifest can contain a reproducible history of changes to a resource.
This history is stored in the resource definition so it doesn't rely on git history for thisself.
It is mainly used to retrieve previous versions of a configurator.
"""

def getRootResources(manifest, resourceName=None):
  clusterCount = len(manifest.resources)
  if not clusterCount:
    raise GitErOpError("no root resources found in manifest")
  elif resourceName is not None:
    cluster = manifest.getRootResource(resourceName)
    if not cluster:
      raise GitErOpError("couldn't find root resouce %s in manifest" % resourceName)
    return [cluster]
  return manifest.resources[:]

def save(manifest, changes):
  #update cluster with last success
  #commit manifest
  pass

def getNeededTasks(configurations, action=None):
  tasks = []
  for configuration in configurations:
    # check status, discover or instantiate
    if action:
      configuration.overrideAction(action)
    configurator = configuration.getConfigurator()
    if not configurator:
      raise CantRunErr(configuration)
    parameters = configuration.getParams()
    # configuration could be for child resource
    if configurator.shouldRun(configuration.getResource(), configuration.getAction(), parameters):
      if not configurator.canRun(configuration.getResource(), configuration.getAction(), parameters):
        raise CantRunErr(configuration)
      else:
        tasks.append(configuration)
  return tasks

#cmd=update find and run configurations that need to be applied
#for running and recording adhoc changes:
#cmd=add resource configuration action params
def run(manifestPath, opts=None):
  manifest = Manifest(path=manifestPath)
  action = 'discover' if opts.get('readonly') else None
  #XXX before running commit manifest if it has changed, else verify git access to this manifest
  #XXX resource option shouldn't have to be root
  resources = getRootResources(manifest, opts.get('resource'))
  configurations = []
  for resource in resources:
    configurations.extend(getNeededTasks(resource.configuration.configurations, action))

  while configurations:
    configuration = configuration.pop(0)
    changes = configuration.run()
    if changes:
      save(manifest, changes)
      # examine new / changed resources for changed configurations
      updatedConfigurations = changes.getUpdatedConfigurations()
      if updatedConfigurations:
        moreConfigurations = []
        try:
          moreConfigurations = getNeededTasks(updatedConfigurations, action)
        except (CantRunErr, e):
          saveError(e)
          break
        #run them before the next configuration
        configurations[0:0] = moreConfigurations
