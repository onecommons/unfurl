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
This history is stored in resource definition, git history is used to retrieve previous versions of configurator.
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

#cmd=update find and run configurations that need to be applied
#for running and recording adhoc changes:
#cmd=add resource configuration action params
def run(manifestPath, opts=None):
  manifest = Manifest(path=manifestPath)
  action = opts.get('action')
  #XXX before run commit manifest if it has changed, else verify git access to this manifest
  resources = getRootResources(manifest, opts.get('resource'))
  while resources:
    resource = resources.pop()
    for configuration in resource.configuration.configurations:
      # check status, discover or instantiate
      configurator = configuration.configurator
      parameters = configuration.getParams()
      if configurator.shouldRun(resource, action, parameters):
        changes = configurator.run(resource, action, parameters)
        if changes:
          save(manifest, changes)
          if changes.resources:
            #run them before the next root resource
            resources.insert(changes.resources)
