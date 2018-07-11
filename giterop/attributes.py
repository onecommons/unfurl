import six
import re
import operator
import collections
from .util import *

class AttributeDefinition(object):
  def __init__(self, obj, manifest, validate=True):
    if validate:
      for k in "name enum".split():
        if not obj.get(k):
          if k != "enum" or obj.get('type') == "enum":
            raise GitErOpError('attribute definition missing "%s"' % k)
    for key in "name type required enum count".split():
      setattr(self, key, obj.get(key))
    self.secret = self.name.startswith('secret_') or obj.get('secret')
    if 'default' in obj:
      self.default = obj['default']
    elif 'value' in obj:
      self.type = 'value'
      self.default = obj['value']
    self.inherited = 'inherited' in obj
    if isinstance(self.type, list):
      self.type = self.type[0]
      self.count = 2

    templateName = None
    if isinstance(self.type, dict):
      templateName = self.type.get('template')
      if templateName:
        self.type = 'template'
    elif obj.get('template'):
      templateName = obj['template']
    if templateName:
      template = manifest.templates.get(templateName)
      self.template = template and template.attributes

  def merge(self, defs):
    # we only want to merge attributes that further constrain type
    for key in ['default', 'required', 'secret']:
      if key in defs:
        setattr(self, key, defs[key])

  @property
  def hasDefault(self):
    return hasattr(self, 'default')

  def __str__(self):
    return "<AttrDef %s: type: %s>" % (self.name, self.type)

  def __repr__(self):
    return "<AttrDef %s: type: %s>" % (self.name, self.type)

  def isValueCompatible(self, value, resolver=None, item=False):
    #count is 0, 1 or "many"
    #count = number, 0 means None is ok, > 1 means should be list
    #default is 0
    #note: 1 is different from required when property is present
    quantity = 0 if item else getattr(self, 'count', 0)
    isList = isinstance(value, list)
    if quantity and quantity > 1 and not isList:
      #must be a list, we don't want to coerce because
      #then we can not distinguish list of values and a value that is a list
      return False
    elif isList:
      if quantity:
        if quantity == 1 and len(value) > 1:
          return False
        return all([self.isValueCompatible(v, resolver, True) for v in value])
      else:
        return False
    #else: fallthrough

    if not self.type:
      return True

    if self.type == 'value':
      return self.default == value

    if self.type == 'resource':
      from .resource import Resource
      if not isinstance(value, Resource):
        return False
      if self.template:
        if not self.template.checkParameters(value, resolver):
          return False
      return True

    if isinstance(value, six.integer_types):
      return self.type == 'number'

    if isinstance(value, (float)):
      if self.type == 'int':
        return round(value) == value
      return self.type == 'number'

    if isinstance(value, six.string_types):
      if self.type == 'enum':
        return value in self.enum
      return self.type == 'string'

    if isinstance(value, bool):
      return self.type == 'boolean'

    if Ref.isRef(value):
      if resolver:
        return self.isValueCompatible(Ref.resolve(value, resolver), resolver)
      else:
        return True

    if isinstance(value, dict):
      if self.type == 'template':
        return not self.template.checkParameters(value, resolver)
      return False

    return False

class AttributeDefinitionGroup(object):
  """
  """
  def __init__(self, localDef, manifest, validate=True, base=None):
    self.attributes = dict(base or {})
    for obj in localDef:
      self.attributes.update(self._getAttributes(obj, manifest, validate))

  def __str__(self):
    return "<AttrGroup: %s>" % (self.attributes.values())

  def _getAttributes(self, obj, manifest, validate):
    templateName = obj.get('template')
    if templateName:
      #include this templates attributes in the group
      template = manifest.templates.get(templateName)
      return template.attributes.attributes
    else:
      attr = AttributeDefinition(obj, manifest, validate)
      return dict( [(attr.name, attr)] )

  def getDefaults(self):
    return dict([(paramdef.name, paramdef.default)
      for paramdef in self.attributes.values() if paramdef.hasDefault])

  def getInherited(self):
    return dict([(paramdef.name, True)
      for paramdef in self.attributes.values() if paramdef.inherited])

  def merge(self, overrides, manifest):
    for defs in overrides:
      name = defs.get('name')
      attrDef = self.attributes.get(name)
      if attrDef:
        attrDef.merge(defs)
      else:
        self.attributes[name] = AttributeDefinition(defs, manifest)

  def validateParameters(self, params, resolver=None, includeUnexpected=True):
    status = self.checkParameters(params, resolver, includeUnexpected)
    if status:
      raise GitErOpValidationError("bad parameters: %s" % status, status)

  def checkParameters(self, pparams, resolver=None, includeUnexpected=True):
    params = dict(pparams)
    status = []
    for paramdef in self.attributes.values():
      value = params.pop(paramdef.name, None)
      if value is None:
        if paramdef.required:
          status.append(("missing required parameter", paramdef.name))
      elif not paramdef.isValueCompatible(value, resolver):
        status.append(("invalid value", (paramdef.name, value)))
    if includeUnexpected and params:
      status.append( ("unexpected parameters", params.keys()) )
    return status

class _RefContext(object):
  def __init__(self, vars):
    self.vars = vars

class Ref(object):
  """
  A Ref objects describes a path to metadata associated with a resource.

  The syntax for a Ref path expression is:

  expr:  segment? (':' segment)*

  segment: key? ('[' filter ']')* '?'?

  key: name | integer | var

  filter: '!'? expr? (('!=' | '=') test)?

  test: var | (^[$[]:?])*

  var: '$' name

  Semantics

  Each segment specifies a key in a resource or JSON/YAML object.
  ":" is used as the segment deliminated to allow for keys that contain "." and "/"

  Path expressions evaluations always start with a list of one or more Resources.
  and each segment selects the value associated that key. If segment has one or more filters
  each filter is applied to that value -- each first is treated as a predicate
  that decides whether value is included or not in the results.
  If the filter doesn't include a test the filter tests the existence or non-existence of the expression,
  depending on whether the expression is prefixed with a "!".
  If the filter includes a test the left side of the test needs to match the right side.
  If the right side is not a variable, tries to coerce the string to left side's type before comparing it.
  If the left-side expression is omitted the value of the segment's key is used and if that is missing, the current value is used.

  If the current value is a list and the key looks like an integer
  it will be treated like a zero-based index into the list.
  Otherwise the segment is evaluated again all values in the list and resulting value is a list.

  If segment ends in "?", it will only include the first match.
  In other words, "a?:b:c" is a shorthand for "a[b:c]:0:b:c".
  This is useful to guarantee the result of evaluating expression is always a single result.

  The first segment is evaluated against the "current resource" unless the first segment is a variable,
  which case it evaluates against the value of the variable.
  If the first segment is empty (i.e. the expression starts with ':') the first segment will be set to ".ancestors?",
  in otherwords the expression will be the result of valuating if against first ancestor of the current resource that it matches.

  If key or test needs to be be non-string type or contains a unallowed character use a var reference instead.

  When multiple steps resolve to lists the resultant lists are flattened.
  However if the final set of matches contain values that are lists those values are not flattened.

  For example, given:

  {x: [ {
          a: [{c:1}, {c:2}]
        },
        {
          a: [{c:3}, {c:4}]
        }
      ]
  }

  x:a:c resolves to:
    [1,2,3,4]
  not
    [[1,2], [3,4]])

  (Justification: It is inconvenient and fragile to tie data structures to the particular form of a query.
  If you want preserve structure (e.g. to know which values are part
  of which parent value or resource) use a less deep path and iterate over results.)

  Resources have a special set of keys:

  .            self
  ..           parent
  .parents     list of parents
  .ancestors   self and parents
  .root        root ancestor
  .children    child resources
  .descendents (including self)
  .named       dictionary child resources with name as key
  .kms

    # XXX
    # .configured
    # .configurations
    # .byShape
  """

  def __init__(self, exp, vars = None):
    defaultvars = {
     'true': True, 'false': False, 'null': None
    }
    if isinstance(exp, dict):
      self.vars = exp.get('vars', defaultvars)
      if vars:
        self.vars.update(vars)
      exp = exp.get('ref', '')
    else:
      self.vars = defaultvars
    self.source = exp
    paths = list(parseExp(exp))
    if not paths[0].key:
      paths[:0] = [Segment('.ancestors', [], '?', [])]
    self.paths = paths

  def resolve(self, currentResource):
    #always return a list of matches
    #values in results list can be a list or None
    context = _RefContext(dict((k, self.resolveIfRef(v, currentResource)) for (k, v) in self.vars.items()))
    if self.paths[0][0] == '$':
      #if starts with a var, use that as the start
      varName = self.paths[0][1:]
      currentResource = self.resolveIfRef(context.vars[varName], currentResource)
      paths = [self.paths[0]._replace(key='')] + self.paths[1:]
    else:
      paths = self.paths
    return evalExp([currentResource], paths, context)

  def __repr__(self):
    return "Ref('%s')" % self.source

  def resolveOne(self, currentResource):
    #if no match return None
    #if more than one match return a list of matches
    #otherwise return match
    #if you want to distinguish between None values and no match
    #or between single match that is a list and a list of matches
    #use resolve() which always returns a (possible empty) of matches
    results = self.resolve(currentResource)
    if results is None:
      return None
    if len(results) == 1:
      return results[0]
    else:
      return results

  @staticmethod
  def resolveIfRef(value, currentResource):
    if isinstance(value, Ref):
      return value.resolve(currentResource)
    elif Ref.isRef(value):
      return Ref(value).resolve(currentResource)
    else:
      return value

  @staticmethod
  def resolveOneIfRef(value, currentResource):
    if isinstance(value, Ref):
      return value.resolveOne(currentResource)
    elif Ref.isRef(value):
      return Ref(value).resolveOne(currentResource)
    else:
      return value

  @staticmethod
  def isRef(value):
    if isinstance(value, dict):
      if 'ref' in value:
        return len([x for x in ['vars', 'foreach'] if x in value]) + 1 == len(value)
      return False
    return isinstance(value, Ref)

#return a segment
Segment = collections.namedtuple('Segment', ['key', 'test', 'modifier', 'filters'])
defaultSegment = Segment('', [], '', [])

def evalTest(value, test, context):
  comparor = test[0]
  key = test[1]
  try:
    if context and isinstance(key, six.string_types) and key.startswith('$'):
      compare = context.vars[key[1:]]
    else:
      # try to coerce string to value type
      compare = type(value)(key)
    if comparor(value, compare):
      return True
  except:
    if comparor is operator.ne:
      return True
  return False

def lookup(value, key, context):
  try:
    # if key == '.':
    #   key = context.currentKey
    if context and isinstance(key, six.string_types) and key.startswith('$'):
      key = context.vars[key[1:]]

    value = value[key]

    # if Ref.isRef(value):
    #   value = Ref.resolveIfRef(value, self)
    #   if not value:
    #     return []

    return [value]
  except (KeyError, IndexError, TypeError, ValueError):
    return []

def evalItem(v, seg, context):
  """
    apply current item to current segment, return [] or [value]
  """
  if seg.key:
    v = lookup(v, seg.key, context)
    if not v:
      return
    v = v[0]

  for filter in seg.filters:
    results = evalExp([v] if _treatAsSingular(v, filter[0]) else v, filter, context)
    negate = filter[0].modifier == '!'
    if negate and results:
      return
    elif not negate and not results:
      return

  if seg.test and not evalTest(v, seg.test, context):
    return
  yield v

def _treatAsSingular(item, seg):
  return not isinstance(item, list) or isinstance(seg.key, six.integer_types)

def recursiveEval(v, exp, context):
  """
  given a list of (previous) results,
  yield a list of results
  """
  matchFirst = exp[0].modifier == '?'
  for item in v:
    if _treatAsSingular(item, exp[0]):
      iv = evalItem(item, exp[0], context)
      rest = exp[1:]
    else:
      iv = item
      rest = exp

    #if iv is empty, it won't yield
    results = recursiveEval(iv, rest, context) if rest else iv
    for r in results:
      yield r
    if matchFirst:
      break

def evalExp(start, paths, context):
  assert isinstance(start, list), start
  return list(recursiveEval(start, paths, context))

def _makeKey(key):
  try:
    return int(key)
  except ValueError:
    return key

def parsePathKey(segment):
  #key, negation, test, matchFirst
  if not segment:
    return defaultSegment

  modifier = ''
  if segment[0] == '!':
    segment = segment[1:]
    modifier = '!'
  elif segment[-1] == '?':
    segment = segment[:-1]
    modifier = '?'

  parts = re.split(r'(=|!=)', segment, 1)
  if len(parts) == 3:
    key = parts[0]
    op = operator.eq if parts[1] == '=' else operator.ne
    return Segment(_makeKey(key), [op, parts[2]], modifier, [])
  else:
    return Segment(_makeKey(segment), [], modifier, [])

def parsePath(path, start):
  paths = path.split(':')
  segments = [parsePathKey(k.strip()) for k in paths]
  if start:
    if paths and paths[0]:
      # if the path didn't start with ':' merge with the last segment
      # e.g. foo[]? d=test[d]?
      segments[0] = start._replace(test=segments[0].test or start.test,
                    modifier=segments[0].modifier or start.modifier)
    else:
      return [start] + segments
  return segments

def parseExp(exp):
  #return list of steps
  rest = exp
  last = None

  while rest:
    steps, rest = parseStep(rest, last)
    last = None
    if steps:
      #we might need merge the next step into the last
      last = steps.pop()
      for step in steps:
        yield step

  if last:
    yield last

def parseStep(exp, start=None):
  split = re.split(r'(\[|\])', exp, 1)
  if len(split) == 1: #not found
    return parsePath(split[0], start), ''
  else:
    path, sep, rest = split

  paths = parsePath(path, start)

  filterExps = []
  while sep == '[':
    filterExp, rest = parseStep(rest)
    filterExps.append(filterExp)
    #rest will be anything after ]
    sep = rest and rest[0]

  #add filterExps to last Segment
  paths[-1] = paths[-1]._replace(filters = filterExps)
  return paths, rest
