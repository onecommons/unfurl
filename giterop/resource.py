from .util import *
from .templatedefinition import *

class ResourceDefinition(object):
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

    self.kms = DummyKMS()
    klass = lookupClass(src.get('kind', 'Resource'), src.get('apiVersion'))
    self.resource = klass(self)

class Resource(object):
  def __init__(self, resourceDef):
    self.definition = resourceDef
    self.metadata = self.makeMetadata()

  def makeMetadata(self):
    return MetadataDict(self.definition)

  # XXX status, changes, resources

registerClass(VERSION, "Resource", Resource)

class MetadataDict(dict):
  """
  Updates the metadata in the underlying resource definition or in the kms if it marked secret
  Validates values based on the attribute definition
  """
  def __init__(self, definition):
    #copy metadata dict and then assign this as the metadata dict
    super(MetadataDict, self).__init__(definition.metadata)
    definition.metadata = self
    self.definition = definition

  def __getitem__(self, name):
    value = super(MetadataDict, self).__getitem__(name)
    resource = self.definition
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
      if resource.kms.isKMSValueReference(value):
        return resource.kms.get(name, value)
      else:
        return value

  def __setitem__(self, name, value):
    paramdef = self.definition.spec.attributes.attributes.get(name)
    if paramdef:
      paramdef.validateValue(value)
      if paramdef.secret:
        # store key reference as value
        value = resource.kms.update(name, value)
    super(MetadataDict, self).__setitem__(name, value)

class DummyKMS(dict):
  def isKMSValueReference(self, value):
    return False
