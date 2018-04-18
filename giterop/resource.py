from .util import *
from .configuration import *

class Resource(object):
  """
  name
  metadata
  spec:
    templates:
      - "base"
    configurations:
      - "component"
  status:
  """
  def __init__(self, manifest, src, name=None):
    self.manifest = manifest
    self.src = src
    manifestName = src.get('metadata',{}).get("name")
    if manifestName and name and (name != manifestName):
      raise GitErOpError('Resource key and name do not match: %s %s' % (name, manifestName) )
    self.name = name or manifestName
    self.configuration = ConfigurationSpec(manifest, src.get("spec", {}))

  #kind
  #metadata attribute values
  #configuration returns list of ConfigurationSpecs
  #resources return list of Resources
  #status, #history

def findChanges(new, old):
  '''
  Given old and new version of XXX
  '''
  changed = []
  if not old:
    return [('add', installed) for installed in new]
  else:
    for installed in old:
      manifestComponent = new.get(installed)
      if not manifestComponent:
        changed.append(('remove', installed))
      elif manifestComponent != installed:
        changed.append(('update', manifestComponent, installed))

    for manifestComponent in new:
      if manifestComponent not in old:
        changed.append(('add', manifestComponent))
  return changed

class ResourceUpdater(object):
  """
  Updates a resource and tracks
  """

  def __init__(self, resource, configurator):
    self.resource = resource
    self.configurator = configurator
    self.updates = []

  def getLastStatus(self):
    for change in reversed(self.resource.history):
      if change.configurator == self.configurator: #XXX
        return change
    return None

  def update(self, metadata, spec):
    pass

  def updateResource(self, changedResource):
    pass

  def addResource(self, newresource):
    pass

  def getChanges(self):
    return Change(self.configurator, self.updates)

  def commit(self):
    return resource.applyUpdates(self.updates)
