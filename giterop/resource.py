from .util import *
from .configuration import *

class Resource(object):
  """
  apiVersion: giterop/v1alpha1
  kind: KeyStore
  metadata:
    name: name
    foo: "bar"
    labels:
      blah: blah
    annotations:
      buz: 2
  spec:
    attributes
    templates:
      - "base"
    configurations:
      - "component"
  status:
    state: discovered
          created
          deleted
    by:
    resources:
    changes:
  """
  def __init__(self, manifest, src, name=None, validate=True):
    self.manifest = manifest
    if not isinstance(src, dict):
      raise GitErOpError('Malformed resource definition: %s' % src)
    self.src = src

    manifestName = src.get('metadata',{}).get("name")
    if manifestName and name and (name != manifestName):
      raise GitErOpError('Resource key and name do not match: %s %s' % (name, manifestName) )
    self.name = name or manifestName
    if not self.name:
      raise GitErOpError('Resource is missing name: %s' % src)

    self.spec = TemplateDefinition(manifest, src.get("spec", {}))

    defaults = self.spec.attributes.getDefaults()
    defaults.update(src.get("metadata", {}))
    self.metadata = defaults
    if validate:
      self.spec.attributes.validateParameters(self.metadata, False)
    # XXX status, changes, resources
    klass = lookupClass(src.get('kind', 'Resource'), src.get('apiVersion'))
    self.attributes = klass(self)

def isKeyReference(value): #XXX
  return False

class AttributeMarshaller(object):
  """
  Default implementation
  Automatically stores attributes marked as secret in kms
  """
  def __init__(self, resource):
    self.__dict__['resource'] = resource

  def __getattr__(self, name):
    resource = object.__getattribute__(self, 'resource')
    paramdef = resource.spec.attributes.attributes.get(name)
    if paramdef:
      if paramdef.secret:
        # if this is a secret we don't store the value in the metadata
        value = resource.kms.get(name, resource.metadata.get(name))
      else:
        value = resource.metadata.get(name)
      if value is None and paramdef.hasDefault:
        return paramdef.default
      else:
        return value
    else:
      value = resource.metadata.get(name)
      if isKeyReference(value): #XXX
        return resource.kms.get(name, value)
      else:
        return value

  def __setattr__(self, name, value):
    paramdef = self.resource.spec.attributes.attributes.get(name)
    if paramdef:
      paramdef.validateValue(value)
      if paramdef.secret:
        # store key reference as value
        value = resource.kms.update(name, value)
    self.resource.metadata[name] = value

  def __delattr__(self, name):
    del self.resource.metadata[name]

registerClass(VERSION, "Resource", AttributeMarshaller)

class ResourceUpdater(object):
  """
  Keeps track of provence of change to the resource
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
