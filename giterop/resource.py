from .util import *
from .templatedefinition import *

#from dictdiffer import diff, patch, swap, revert
#XXX
def diff(a, b):
  return []

class Resource(object):
  def __init__(self, resourceDef):
    self.definition = resourceDef
    self.metadata = self.makeMetadata()
    self.changes = self.definition.changes

  @property
  def name(self):
    return self.definition.name

  @property
  def parent(self):
    return self.definition.parent and self.definition.parent.resource or None

  @property
  def resources(self):
    return [r.resource for r in self.definition._resources.values()]

  def makeMetadata(self):
    return MetadataDict(self.definition)

  def diffMetadata(self):
    return diff(self.metadata, self.definition.metadata)

  def commitMetadata(self):
    self.definition.metadata.update(self.metadata)


registerClass(VERSION, "Resource", Resource)

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
    state:discovered
          created
          deleted
    by:
    resources:
      name:
        <resource>
    changes:
      - <change record>
  """
  def __init__(self, manifest, src, name=None, validate=True):
    if isinstance(manifest, ResourceDefinition):
      self.parent = manifest
      self.manifest = manifest = parent.manifest
    else:
      self.parent = None
      self.manifest = manifest

    self.src = assertForm(src)

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

    #Note: kms needs a context associated with this particular resource
    self.kms = DummyKMS()
    self.status = assertForm(src.get('status',{}))
    changes = assertForm(self.status.get('changes', []), list)
    self.changes = [ChangeRecord(self, c) for c in changes]

    _resources = assertForm(self.status.get('resources',{}))
    self._resources = dict([(k, ResourceDefinition(self, v, k, validate))
                                      for (k,v) in _resources.items()])

    klass = lookupClass(src.get('kind', 'Resource'), src.get('apiVersion'))
    self.resource = klass(self)

  def findMaxChangeId(self):
    childR = self._resources.values()
    return max(self.changes and max(c.changeId for c in self.changes) or 0,
              childR and max(r.findMaxChangeId() for r in childR) or 0)

class ChangeRecord(object):
  """
    metadata:
      attribute: value
      deleted: [attribute]
    resources:
      deleted:
      created:
      observed:
      forgot:
  """
  CommonAttributes = {
   'changeId': 0,
   'date': '',
   'resources': {},
   'metadata': [],
   'messages': [],
  }
  RootAttributes = {
    'configuration': '',
    'failedToProvide': [],
    'status': '',
    'action': '',
    'parameters': [],
    # XXX
    # 'revision':'',
    # 'previously': '',
    # 'applied': ''
  }
  ChildAttributes = {'rootResource':''}

  def __init__(self, resourceDefinition, src):
    self.resource = resourceDefinition
    self.src = src
    for (k,v) in self.CommonAttributes.items():
      setattr(self, k, src.get(k, v))
    self.rootResource = src.get('rootResource')
    if not self.rootResource:
      for (k,v) in self.RootAttributes.items():
        setattr(self, k, src.get(k, v))

class MetadataDict(dict):
  """
  Updates the metadata in the underlying resource definition or in the kms if it marked secret
  Validates values based on the attribute definition
  """
  def __init__(self, definition):
    #copy metadata dict and then assign this as the metadata dict
    super(MetadataDict, self).__init__(definition.metadata)
    self.definition = definition

  def __getitem__(self, name):
    value = super(MetadataDict, self).__getitem__(name)
    kms = self.definition.kms
    paramdef = self.definition.spec.attributes.attributes.get(name)
    if paramdef:
      if paramdef.secret:
        # if this is a secret we don't store the value in the metadata
        value = kms.get(name, value)
      if value is None and paramdef.hasDefault:
        return paramdef.default

    if kms.isKMSValueReference(value):
      return kms.dereference(value)
    else:
      return value

  def __setitem__(self, name, value):
    resource = self.definition
    paramdef = resource.spec.attributes.attributes.get(name)
    if paramdef:
      if resource.kms.isKMSValueReference(value):
        value = resource.kms.dereference(value)
      paramdef.validateValue(value)
      if paramdef.secret:
        value = resource.kms.set(name, value)
        #store the returned key value reference as the clear text value
    super(MetadataDict, self).__setitem__(name, value)

class DummyKMS(dict):
  def isKMSValueReference(self, value):
    return False

  def dereference(self, value):
    return value

  def set(self, key, value):
    self[key] = value
    return value
