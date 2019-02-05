"""
Internal classes supporting the runtime.
"""
import collections
import copy
import six
from enum import IntEnum

from .eval import RefContext
from .result import ResultsMap, Result
from .util import intersectDict, mergeDicts, ChainMap

# XXX3 doc: notpresent is a positive assertion of non-existence while notapplied just indicates non-liveness
# notapplied is therefore the default initial state
Status = IntEnum("Status", "ok degraded error pending notapplied notpresent", module=__name__)

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
      attributes = ResultsMap(ChainMap(copy.deepcopy(resource._attributes), specd), RefContext(resource))
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
        if not isinstance(value, Result):
          # hasn't been touched so keep it as is
          resource._attributes[key] = value
        elif key not in specd or value.hasDiff():
          resource._attributes[key] = value.asRef()

      # save changes
      diff = attributes.getDiff()
      if not diff:
        continue
      changes[resource.name] = diff
    self.attributes = {}
    # self.statuses = {}
    return changes
