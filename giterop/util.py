import sys
import optparse
import six
import traceback
from six.moves import reduce
from jsonschema import Draft4Validator, validators
from ruamel.yaml.comments import CommentedMap

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

  def getStackTrace(self):
    if not self.stackInfo:
      return ''
    return ''.join(traceback.format_exception(*self.stackInfo))

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

def toEnum(enum, value):
  #from string: Status[name]; to string: status.name
  if isinstance(value, six.string_types):
    return enum[value]
  else:
    return value

mergeStrategy = 'mergeStrategy'
# b is base, a overrides
def mergeDicts(b, a, cls=dict):
  cp = cls()
  for key, val in a.items():
    if key == mergeStrategy:
      continue
    if key in b and isinstance(val, (dict, cls)) and isinstance(b[key], (dict, cls)):
      strategy = a.get(mergeStrategy, b.get(mergeStrategy, 'merge'))
      if strategy == 'merge':
        cp[key] = mergeDicts(b[key], val, cls)
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
    if not isinstance(template, (cls, dict)) or segment not in template:
      raise GitErOpError('can not find "%s" in document' % key)
    template = template[segment]
  if value != 'raw' and isinstance(template, (cls, dict)): # raw means no further processing
    includes, template = expandDoc(doc, template, cls)
  return template

def hasTemplate(doc, key, value, cls):
  template = doc
  for segment in key.split('.'):
    if not isinstance(template, (cls, dict)):
      raise GitErOpError('included templates changed')
    if segment not in template:
      return False
    template = template[segment]
  return True

def expandDict(doc, path, includes, current, cls=dict):
  """
  Return a copy of `doc` that expands include directives.
  Include directives look like "+path.to.value"
  When appearing as a key in a map it will merge the result with the current dictionary.
  When appearing as a string or map in a list it will insert the result in the list;
  if result is also a list, each item will be inserted separately.
  (If you don't want that behavior just wrap include in another list, e.g "[+list1]")
  """
  cp = cls()
  # first merge any includes includes into cp
  templates = []
  assert isinstance(current, (dict, cls)), current
  for (key, value) in current.items():
    if key.startswith('+'):
      includes.setdefault(path, []).append( (key, value) )
      template = getTemplate(doc, key[1:], value, cls)
      if isinstance(template, (cls, dict)):
        templates.append( template )
      else:
        if len(current) > 1:
          raise GitErOpError('can not merge non-map value %s' % template)
        else:
          return template # current dict is replaced with a value
    elif key.startswith('q+'):
      cp[key[2:]] = value
    elif isinstance(value, (dict, cls)):
      cp[key] = expandDict(doc, path + (key,), includes, value, cls)
    elif isinstance(value, list):
      cp[key] = list(expandList(doc, path + (key,), includes, value, cls))
    else:
      cp[key] = value

  if templates:
    accum = templates.pop(0)
    templates.append(cp)
    while templates:
      accum = mergeDicts(accum, templates.pop(0), cls)
    return accum
  else:
    return cp
  # e,g, mergeDicts(mergeDicts(a, b), cp)
  #return includes, reduce(lambda accum, next: mergeDicts(accum, next, cls), templates, {}), cp

def expandDoc(doc, current=None, cls=dict):
  includes = CommentedMap()
  if current is None:
    current = doc
  return includes, expandDict(doc, (), includes, current, cls)

def expandList(doc, path, includes, value, cls=dict):
  for i, item in enumerate(value):
    if isinstance(item, six.string_types):
      if item.startswith('+'):
        includes.setdefault(path+(i,), []).append( (item, None) )
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
      doc = expandDict(doc, path+(i,), includes, item, cls)
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
    # keys not found will be set to None
    if val != newval:
      if isinstance(val, (dict, cls)) and isinstance(newval, (dict, cls)):
        diff[key] = diffDicts(val, newval, cls)
      else:
        diff[key] = newval

  for key in new:
    if key not in old:
      diff[key] = new[key]
  return diff

def lookupPath(doc, path, cls=dict):
  template = doc
  for segment in path:
    if not isinstance(template, (cls, dict)) or segment not in template:
      return None
    template = template[segment]
  return template

def replacePath(doc, key, value, cls=dict):
  path = key[:-1]
  last = key[-1]
  ref = lookupPath(doc, path, cls)
  ref[last] = value

def addTemplate(changedDoc, path, template):
  current = changedDoc
  key = path.split('.')
  path = key[:-1]
  last = key[-1]
  for segment in path:
    current = current.setdefault(segment, {})
  current[last] = template

def restoreIncludes(includes, originalDoc, changedDoc, cls=dict):
  # if the path to the include still exists
  # resolve the include
  # if the include doesn't exist in the current doc, re-add it
  # create a diff between the current object and the merged includes
  for key, value in includes.items():
    ref = lookupPath(changedDoc, key, cls)
    if ref is None:
      continue

    mergedIncludes = {}
    for (includeKey, includeValue) in value:
      stillHasTemplate = hasTemplate(changedDoc, includeKey[1:], includeValue, cls)
      if stillHasTemplate:
        template = getTemplate(changedDoc, includeKey[1:], includeValue, cls)
      else:
        template = getTemplate(originalDoc, includeKey[1:], includeValue, cls)

      if not isinstance(ref, (dict, cls)):
        #XXX3 if isinstance(ref, list) lists not yet implemented
        if ref == template:
          #ref still resolves to the template's value so replace it with the include
          replacePath(changedDoc, key, {includeKey: includeValue}, cls)
        # ref isn't a map anymore so can't include a template
        break

      if not isinstance(template, (dict, cls)):
        # ref no longer includes that template
        continue
      else:
        mergedIncludes = mergeDicts(mergedIncludes, template, cls)
        ref[includeKey] = includeValue

      if not stillHasTemplate:
        if includeValue != 'raw':
          template = getTemplate(originalDoc, includeKey[1:], 'raw', cls)
        addTemplate(changedDoc, includeKey[1:], template)

    if isinstance(ref, (dict, cls)):
      diff = diffDicts(mergedIncludes, ref, cls)
      replacePath(changedDoc, key, diff, cls)

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
    for key, subschema in properties.items():
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
