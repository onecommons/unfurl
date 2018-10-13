import sys
import optparse
import six
from six.moves import reduce
from jsonschema import Draft4Validator, validators

class AnsibleDummyCli(object):
  def __init__(self):
    self.options = optparse.Values()
ansibleDummyCli = AnsibleDummyCli()
from ansible.utils.display import Display
ansibleDisplay = Display()

def initializeAnsible():
  main = sys.modules.get('__main__')
  # XXX make sure ansible.executor.playbook_executor hasn't been loaded already
  main.display = ansibleDisplay
  main.cli = ansibleDummyCli
initializeAnsible()

VERSION = 'giterops/v1alpha1'
TEMPLATESKEY = 'templates'
CONFIGURATORSKEY = 'configurators'

class GitErOpError(Exception):
  def __init__(self, message, saveStack=False):
    super(GitErOpError, self).__init__(message)
    self.stackInfo = sys.exc_info() if saveStack else None

class GitErOpValidationError(GitErOpError):
  def __init__(self, message, errors=None):
    super(GitErOpValidationError, self).__init__(message)
    self.errors = errors or []

class GitErOpTaskError(GitErOpError):
  def __init__(self, task, message):
    super(GitErOpTaskError, self).__init__(message, True)
    self.task = task
    task.errors.append(self)

def assertForm(src, types=dict):
  if not isinstance(src, types):
    raise GitErOpError('Malformed definition: %s' % src)
  return src

_ClassRegistry = {}
# only one class can be associated with an api interface
def registerClass(apiVersion, kind, factory, replace=False):
  api = _ClassRegistry.setdefault(apiVersion, {})
  if not replace and kind in api:
    if api[kind] is not factory:
      raise GitErOpError('class already registered for %s.%s' % (apiVersion, kind))
  api[kind] = factory

class AutoRegisterClass(type):
  def __new__(mcls, name, bases, dct):
    cls = type.__new__(mcls, name, bases, dct)
    registerClass(VERSION, name, cls)
    return cls

def lookupClass(kind, apiVersion=None, default=None):
  version = apiVersion or VERSION
  api = _ClassRegistry.get(version)
  if api:
    klass = api.get(kind, default)
  else:
    klass = default
  if not klass:
    raise GitErOpError('Can not find class %s.%s' % (version, kind))
  return klass

mergeStrategy = 'mergeStrategy'
# b is base, a overrides
def merge(b, a, cls=dict):
  cp = cls()
  for key, val in a.items():
    if key == mergeStrategy:
      continue
    if key in b and isinstance(val, (dict, cls)) and isinstance(b[key], (dict, cls)):
      strategy = a.get(mergeStrategy, b.get(mergeStrategy, 'merge'))
      if strategy == 'merge':
        cp[key] = merge(b[key], val, cls)
      # otherwise a replaces b
    else:
      cp[key] = val

  for key, val in b.items():
    if key == mergeStrategy:
      continue
    if key not in cp:
      cp[key] = val
  return cp

def getTemplate(doc, key, value, cls):
  template = doc
  for segment in key.split('.'):
    template = template.get(segment)
    if template is None:
      break
  if value != 'raw' and isinstance(template, (cls, dict)): # raw means no further processing
    template = expandDoc(doc, template, cls)
  return template

def expandDoc(doc, current=None, cls=dict):
  """
  Return a copy of `doc` that expands include directives.
  Include directives look like "+path.to.value"
  When appearing as a key in a map it will merge the result with the current dictionary.
  When appearing as a string or map in a list it will insert the result in the list;
  if result is also a list, each item will be inserted separately.
  (If you don't want that behavior just wrap include in another list, e.g "[+list1]")
  """
  if current is None:
    current = doc
  cp = cls()
  # first merge any includes includes into cp
  includes = []
  for (key, value) in current.items():
    if key.startswith('+'):
      template = getTemplate(doc, key[1:], value, cls)
      if isinstance(template, (cls, dict)):
        includes.append( template )
      else:
        if len(current) > 1:
          raise 'can not merge non-map value %s' % template #XXX2
        else:
          return template # replaces current
    elif key.startswith('q+'):
      cp[key[2:]] = value
    elif isinstance(value, (dict, cls)):
      cp[key] = expandDoc(doc, value, cls)
    elif isinstance(value, list):
      cp[key] = list(expandList(doc, value))
    else:
      cp[key] = value

  if includes:
    includes.append(cp)
    # e,g, merge(merge(a, b), cp)
    return reduce(lambda accum, next: merge(accum, next, cls), includes)
  else:
    return cp

def expandList(doc, value, cls=dict):
  for item in value:
    if isinstance(item, six.string_types):
      if item.startswith('+'):
        template = getTemplate(doc, item[1:], None, cls)
        if isinstance(template, list):
          for i in template:
            yield i
        else:
          yield template
      elif item.startswith('q+'):
        yield item[1:]
      else:
        yield item
    elif isinstance(item, (dict, cls)):
      doc = expandDoc(doc, item, cls)
      if isinstance(doc, list):
        for i in doc:
          yield i
      else:
        yield doc
    else:
      yield item

def diffDicts(old, new, cls=dict):
  """
  return a dict where old + diff = new
  """
  diff = cls()
  # start with old to preserve original order
  for key, val in old.items():
    newval = new.get(key)
    # key not found then will be set to None
    if val != newval:
      diff[key] = newval
    elif isinstance(val, (dict, cls)):
      assert isinstance(newval, (dict, cls))
      diff[key] = diffDicts(val, newval, cls)

  for key in new:
    if key not in diff:
      diff[key] = new[key]
  return diff

def updateDoc(originalDoc, changedDoc, original=None, changed=None, cls=dict):
  """
  Assumes `original` contains include directives and `changed` does not.
  """
  if original is None:
    original = originalDoc
  if changed is None:
    changed = changedDoc
  expanded = expandDoc(originalDoc, original, cls)
  # descend, looking for dict and lists that may have includes
  for key, val in list(changed.items()):
    if key in expanded:
      oldval = expanded[key]
      if val != oldval:
        if isinstance(val, (dict, cls)) and isinstance(oldval, (dict, cls)):
          changed[key] = updateDoc(originalDoc, changedDoc, oldval, val, cls)
        elif isinstance(val, list) and isinstance(expanded[key], list):
          changed[key] = updateList(doc, oldval, val, cls)

  # if original map has includes, diff the keys in the includes and update map with diff plus includes
  includes, mergedIncludes = findincludes(originalDoc, changedDoc, original) #XXX2
  if includes:
    diff = diffDicts(mergedIncludes, changed)
    diff.update(includes)
    return diff
  return changed

def findincludes(originalDoc, changedDoc, original):
  # if include doesn't exist in changed add it
  # if include exists but doesn't match original, error
  return {}, {} #XXX2

def updateList(doc, original, changed, cls=dict):
  #if original list has includes, find values in changed list, replace them with include
  return changed

# https://python-jsonschema.readthedocs.io/en/latest/faq/#why-doesn-t-my-schema-s-default-property-set-the-default-on-my-instance
def extend_with_default(validator_class):
  """
  # Example usage:
  obj = {}
  schema = {'properties': {'foo': {'default': 'bar'}}}
  # Note jsonschema.validate(obj, schema, cls=DefaultValidatingDraft7Validator)
  # will not work because the metaschema contains `default` directives.
  DefaultValidatingDraft7Validator(schema).validate(obj)
  assert obj == {'foo': 'bar'}
  """
  validate_properties = validator_class.VALIDATORS["properties"]

  def set_defaults(validator, properties, instance, schema):
    for key, subschema in properties.iteritems():
      if "default" in subschema:
        instance.setdefault(key, subschema["default"])

    for error in validate_properties(
        validator, properties, instance, schema,
    ):
        yield error

  # new validator class
  return validators.extend(
    validator_class, {"properties" : set_defaults},
  )

DefaultValidatingLatestDraftValidator = extend_with_default(Draft4Validator)

def validateSchema(obj, schema):
  return DefaultValidatingLatestDraftValidator(schema).validate(obj)
