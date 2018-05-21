from ruamel.yaml.comments import CommentedMap
import six

from .util import *
from .templatedefinition import *

class _ChildResources(dict):
  def __init__(self, resource):
    self.resource = resource

  def __getitem__(self, key):
    res = self.resource.findLocalResource(key)
    return res and res.resource or None

class Resource(object):

  def __init__(self, resourceDef):
    self.definition = resourceDef
    self.metadata = dict(self.definition.metadata)
    self.defaults = self.definition.spec.attributes.getDefaults()
    self.inherited = self.definition.spec.attributes.getInherited()
    self.changes = self.definition.changes
    #Note: kms needs a context associated with this particular resource
    self.kms = DummyKMS()
    self.named = _ChildResources(self.definition)

  @property
  def name(self):
    return self.definition.name

  @property
  def parent(self):
    return self.definition.parent and self.definition.parent.resource or None

  @property
  def resources(self):
    return [r.resource for r in self.definition._resources.values()]

  def yieldDescendents(self):
    """Recursive descendent including self"""
    yield self
    for r in self.resources:
      for descendent in r.yieldDescendents():
        yield descendent

  @property
  def descendents(self):
    return list(self.yieldDescendents())

  @property
  def children(self):
    return self.resources

  def yieldParents(self):
    resource = self
    while resource:
      yield resource
      resource = resource.parent

  @property
  def ancestors(self):
    return list(self.yieldParents())

  @property
  def parents(self):
    return self.ancestors[1:]

  @property
  def root(self):
    return self.ancestors[0]

  def __getitem__(self, name):
    if not name:
      raise KeyError(name)
    if name[0] == '.':
      return self.getProp(name)

    try:
      value = self.metadata[name]
    except KeyError:
      if self.parent and name in self.inherited:
        try:
          value = self.parent[name]
        except:
          value = self.defaults[name]
      else:
        value = self.defaults[name]

    if self.kms.isKMSValueReference(value):
      value = kms.dereference(value)
    return Ref.resolveOneIfRef(value, self)

  def __setitem__(self, name, value):
    # XXX:
    if isinstance(value, Resource):
      value = {'ref': value.name}
    paramdef = self.definition.spec.attributes.attributes.get(name)
    if paramdef:
      #if self.kms.isKMSValueReference(value):
      #  value = self.kms.dereference(value)
      # XXX
      paramdef.isValueCompatible(value, self)
      if paramdef.secret:
        value = self.kms.set(name, value)
    self.metadata[name] = value

  def __delitem__(self, key):
    value = self.metadata.pop(key)
    if self.kms.isKMSValueReference(value):
      self.kms.remove(key, value)

  def __contains__(self, key):
    if key == '.':
      return True
    elif key == '..':
      return not not self.parent

    exists = key in self.metadata or key in self.defaults
    if not exists and self.parent and key in self.inherited:
      return key in self.parent
    else:
      return exists

  def getProp(self, name):
    if name == '.':
      return self
    elif name == '..':
      return self.parent
    name = name[1:]
    # XXX propmap
    return getattr(self, name)
    # .configured
    # .kms
    # .configurations
    # .descendents
    # .byTemplate
    # XXX
    # need to treat as task a resource
    # .configuring returns task as a resource
    # .parameters

  def diffMetadata(self):
    old = six.viewkeys(self.definition.metadata)
    current = six.viewkeys(self.metadata)

    deleted = old - current
    added = current - old
    replaced = {}
    for key in (old & current):
      oldValue = self.definition.metadata[key]
      if self.metadata[key] != oldValue:
        replaced[key] = self.metadata[key]
    return dict(added=list(added), deleted=list(deleted), replaced=replaced)

  def commitMetadata(self):
    old = self.definition.metadata
    deleted = six.viewkeys(old) - six.viewkeys(self.metadata)
    for key in list(deleted):
      del old[key]
    # do an update to preserve order
    old.update(self.metadata)

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
    resources:
      name:
        <resource>
    changes:
      - <change record>
  """
  def __init__(self, manifestOrParent, src, name=None, validate=True):
    if isinstance(manifestOrParent, ResourceDefinition):
      self.parent = manifestOrParent
      self.manifest = manifest = parent.manifest
    else:
      self.parent = None
      self.manifest = manifest = manifestOrParent

    self.src = CommentedMap(assertForm(src).items())
    # we need to modify src so changes to it get saved
    for key in "metadata spec status".split():
      if not src.get(key):
        src[key] = CommentedMap()

    manifestName = src['metadata'].get("name")
    if manifestName and name and (name != manifestName):
      raise GitErOpError('Resource key and name do not match: %s %s' % (name, manifestName) )
    self.name = name or manifestName
    if not self.name:
      raise GitErOpError('Resource is missing name: %s' % src)
    elif not manifestName:
      src['metadata']['name'] = name
    self.spec = TemplateDefinition(manifest, src["spec"])

    self.metadata = src["metadata"]
    if validate:
      self.spec.attributes.validateParameters(self.metadata, None, False)

    self.status = src['status']
    if 'changes' in self.status:
      changes = assertForm(self.status['changes'], list)
    else:
      changes = self.status['changes'] = []
    self.changes = [ChangeRecord(self, c) for c in changes]

    if 'resources' in self.status:
      _resources = assertForm(self.status['resources'])
    else:
      _resources = self.status['resources'] = {}
    self._resources = dict([(k, ResourceDefinition(self, v, k, validate))
                                      for (k,v) in _resources.items()])

    klass = lookupClass(src.get('kind', 'Resource'), src.get('apiVersion'))
    self.resource = klass(self)

  def findResource(self, resourceid):
    if self.name == resourceid:
      return self

    for r in self.resources:
      match = r.name == resourceid
      if match:
        return match

    if self.parent:
      return self.parent.findResource(resourceid)
    else:
      return self.manifest.getRootResource(resourceid)

  def findLocalResource(self, resourceid):
    if self.name == resourceid:
      return self

    for r in self.resources:
      match = r.findLocalResource(resourceid)
      if match:
        return match
    return None

  def findMaxChangeId(self):
    childR = self._resources.values()
    return max(self.changes and max(c.changeId for c in self.changes) or 0,
              childR and max(r.findMaxChangeId() for r in childR) or 0)

  def save(self):
    # below are the only fields that should have changed
    # and need to be save back into the yaml manifest
    # note: metadata is already in sync

    # needs to add new items in changes
    newChanges = self.changes[len(self.status['changes']):]
    self.status['changes'].extend([c.src for c in newChanges])

    # need to reconstruct child resources
    for (k, rd) in self._resources:
      rd.save()
      self.status['resources'][k] = rd.src

class ChangeRecord(object):
  """
    changeid
    date
    commit
    action
    configuration:
      name
      digest
      parameters
    status
    metadata:
      added:
      updated:
        attribute: value
      deleted: [attributes]
    resources:
      deleted:
      created:
      discovered:
      forgot:
      modified: XXX

    changeid
    date
    commit
    masterResource: resource
    action: created | discovered | modified
    metadata:
      added:
        attribute: value
      updated:
        'attribute.path': value
      deleted: [attributes]
  """
  HeaderAttributes = CommentedMap([
   ('changeId', 0),
   ('commitId', ''),
   ('startTime', ''),
   ('action', ''),
  ])
  CommonAttributes = CommentedMap([
    ('metadata', {}),
    ('resources', {}),
  ])
  RootAttributes = CommentedMap([
    ('configuration', {}),
    ('messages', []),
    # XXX
    # 'revision':'',
    # 'previously': '',
    # 'applied': ''
    ('status', ''),
    ('failedToProvide', []),
  ])
  ChildAttributes = {'masterResource':''}

  def __init__(self, resourceDefinition, src):
    self.resource = resourceDefinition
    self.src = src
    for (k,v) in self.HeaderAttributes.items():
      setattr(self, k, src.get(k, v))
    self.masterResource = src.get('masterResource')
    if not self.masterResource:
      for (k,v) in self.RootAttributes.items():
        setattr(self, k, src.get(k, v))
    for (k,v) in self.CommonAttributes.items():
      setattr(self, k, src.get(k, v))

class DummyKMS(dict):
  def isKMSValueReference(self, value):
    return False

  def dereference(self, value):
    return value

  def set(self, key, value):
    self[key] = value
    return value
