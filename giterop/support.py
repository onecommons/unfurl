"""
Internal classes supporting the runtime.
"""
import collections
from collections import Mapping, MutableSequence
import copy
import six
from enum import IntEnum

from .eval import mapValue, serializeValue, ExternalValue, RefContext, Ref #, _Funcs
from .util import diffDicts, intersectDict, mergeDicts, ChainMap

# XXX3 doc: notpresent is a positive assertion of non-existence while notapplied just indicates non-liveness
# notapplied is therefore the default initial state
Status = IntEnum("Status", "ok degraded error pending notapplied notpresent", module=__name__)

_Deleted = object()
class Resolved(object):
  __slots__ = ('original', 'resolved', 'external')

  def __init__(self, resolved, original = _Deleted):
    self.original = original
    if isinstance(resolved, ExternalValue):
      self.resolved = resolved.resolve()
      assert not isinstance(self.resolved, Resolved), self.resolved
      self.external = resolved
    else:
      assert not isinstance(resolved, Resolved), resolved
      self.resolved = resolved
      self.external = None

  def asRef(self, options=None):
    if self.external:
      return self.external.asRef()
    else:
      val = serializeValue(self.resolved, **(options or {}))
      return val

  def hasChanged(self):
    if self.original is _Deleted: # this is a new item
      return True
    else:
      if isinstance(self.resolved, _Lazy):
        return self.resolved.hasChanged()
      else:
        newval = self.asRef()
        if self.original != newval:
          return True
    return False

  def getDiff(self):
    if isinstance(self.resolved, _Lazy):
      return self.resolved.getDiff()
    else:
      val = self.asRef()
      if isinstance(val, Mapping):
        old = serializeValue(self.original)
        if isinstance(old, Mapping):
          return diffDicts(old, val)
      return val

  def __eq__(self, other):
    if isinstance(other, Resolved):
      return self.resolved == other.resolved
    else:
      return self.resolved == other

  def __repr__(self):
    return "Resolved(%r)" % self.resolved

class _Lazy(object):
  __slots__ = ('_attributes', 'context', '_deleted')

  def __init__(self, serializedOriginal, context):
      self._attributes = serializedOriginal
      self._deleted = {}
      self.context = context

  def hasChanged(self):
    return any(isinstance(x, Resolved) and x.hasChanged() for x in self._attribute)

  def _serializeItem(self, val):
    if isinstance(val, Resolved):
      return val.asRef()
    else: # never resolved, so already in serialized form
      return val

  def _mapValue(self, val):
    if isinstance(val, Resolved):
      return val.resolved
    elif isinstance(val, _Lazy):
      return val
    elif Ref.isRef(val):
      return mapValue(val, self.context)
    elif isinstance(val, Mapping):
      return LazyDict(val, self.context)
    elif isinstance(val, (list,tuple)):
      return LazyList(val, self.context)
    else:
      return mapValue(val, self.context)

  def __getitem__(self, key):
    val = self._attributes[key]
    if isinstance(val, Resolved):
      assert not isinstance(val.resolved, Resolved), val
      return val.resolved
    else:
      resolved = self._mapValue(val)
      self._attributes[key] = Resolved(resolved, val)
      assert not isinstance(resolved, Resolved), val
      return resolved

  def __setitem__(self, key, value):
    assert not isinstance(value, Resolved), (key, value)
    self._attributes[key] = Resolved(value)
    self._deleted.pop(key, None)

  def __delitem__(self, index):
    val = self._attributes[index]
    self._deleted[index] = val
    del self._attributes[index]

  def __len__(self):
    return len(self._attributes)

  def __eq__(self, other):
    if isinstance(other, _Lazy):
      return self._attributes == other._attributes
    else:
      return self._attributes == other

  def __repr__(self):
    return "Lazy(%r)" % self._attributes

class LazyDict(_Lazy, Mapping):
  """
  Evaluating expressions are not guaranteed to be idempotent (consider quoting)
  and resolving the whole tree up front can lead to evaluations of cicular references unless the
  order is carefully chosen. So evaluate lazily and memoize the results.
  """

  # def __delitem__(self, key):
  #   val = self._attributes[key]
  #   self._deleted[key] = val
  #   self._attributes[key] = _Deleted

  def __iter__(self):
      return iter(self._attributes)

  def asRef(self, options=None):
    return dict((key, self._serializeItem(val)) for key, val in self._attributes.items())

  def getDiff(self, cls=dict):
    # returns a dict with the same semantics as diffDicts
    diffDict = cls()
    for key, val in self._attributes.items():
      if isinstance(val, Resolved) and val.hasChanged():
        diffDict[key] = val.getDiff()

    for key in self._deleted:
      diffDict[key]= {'+%': 'delete'}

    return diffDict

class LazyList(_Lazy, MutableSequence):

  # def __delitem__(self, index):
  #   # val = self._attributes[index]
  #   # self._deleted[index] = val
  #   del self._attributes[index]

  # def __getitem__(self, key):
  #   print ('__getitem__', key, 'val', self._attributes[key])
  #   val = super(LazyList, self).__getitem__(key)
  #   print ('__getitem__ result', val)
  #   return val

  def insert(self, index, value):
    assert not isinstance(value, Resolved), value
    self._attributes.insert(index, Resolved(value))

  def asRef(self, options=None):
    return [self._serializeItem(val) for val in self._attributes]

  def getDiff(self, cls=list):
    # we don't have patchList yet so just returns the whole list
    return cls(val.getDiff() if isinstance(val, Resolved) else val for val in self._attributes)

class ResourceChanges(collections.OrderedDict):
  """
  Records changes made by configurations.
  Serialized as the "modifications" properties

  modifications:
    resource1:
      attribute1: newvalue
      attribute2: %delete # if deleted
      .added: # set if resource was added
      .status: # set when status changes, including when removed (Status.notpresent)
  """
  statusIndex = 0
  addedIndex = 1
  attributesIndex = 2

  def sync(self, resource):
    """ Update self to only include changes that are still live"""
    root = resource.root
    for k, v in list(self.items()):
      current = root.findResource(k)
      if current:
        attributes = v[self.attributesIndex]
        if attributes:
          v[self.attributesIndex] = intersectDict(attributes, current._attributes)
        if v[self.statusIndex] != current._localStatus:
          v[self.statusIndex] = None
      else:
        del self[k]

  def addChanges(self, changes):
    for name, change in changes.items():
      old = self.get(name)
      if old:
        old[self.attributesIndex] = mergeDicts(old[self.attributesIndex], change)
      else:
        self[name] = [None, None, change]

  def addStatuses(self, changes):
    for name, change in changes.items():
      assert not isinstance(change[1], six.string_types)
      old = self.get(name)
      if old:
        old[self.statusIndex] = change[1]
      else:
        self[name] = [change[1], None, {}]

  def addResources(self, resources):
    for resource in resources:
      self[resource['name']] = [None, resource, None]

  def updateChanges(self, changes, statuses, resource):
    self.addChanges(changes)
    self.addStatuses(statuses)
    if resource:
      self.sync(resource)

class AttributeManager(object):
  """
  Tracks changes made to Resources

  Configurator set attributes override spec attributes.
  A configurator can delete an attribute but it will not affect the spec attributes
  so deleting an attribute is essentially restoring the spec's definition of the attribute
  (if it is defined in a spec.)
  Changing an overridden attribute definition in the spec will have no effect
  -- if a configurator wants to re-evaluate that attribute, it can create a dependency on it
  so to treat that as changed configuration.
  """
  # what about an attribute that is added to the spec that already exists in status?
  # XXX2 tests for the above behavior
  def __init__(self):
    self.attributes = {}
    self.statuses = {}

  def setStatus(self, resource, newvalue):
    assert newvalue is None or isinstance(newvalue, Status)
    if resource.name not in self.statuses:
      self.statuses[resource.name] = [resource._localStatus, newvalue]
    else:
      self.statuses[resource.name][1] = newvalue

  def getAttributes(self, resource):
    if resource.name not in self.attributes:
      specd = resource.spec.get('attributes', {})
      attributes = LazyDict(ChainMap(copy.deepcopy(resource._attributes), specd), RefContext(resource))
      self.attributes[resource.name] = (resource, attributes)
      return attributes
    else:
      return self.attributes[resource.name][1]

  def revertChanges(self):
    self.attributes = {}
    # for resource, old, new in self.statuses.values():
    #   resource._localStatus = old

  def commitChanges(self):
    changes = {}
    # current and original don't have external values
    for resource, attributes in self.attributes.values():
      # save in _attributes in serialized form
      overrides, specd = attributes._attributes._maps
      resource._attributes = {}
      for key, value in overrides.items():
        if not isinstance(value, Resolved):
          # hasn't been touched so keep it as is
          resource._attributes[key] = value
        elif key not in specd or value.hasChanged():
          resource._attributes[key] = value.asRef()

      # save changes
      diff = attributes.getDiff()
      if not diff:
        continue
      changes[resource.name] = diff
    self.attributes = {}
    # self.statuses = {}
    return changes
