import six
import re
import operator
import collections
from collections import Mapping, MutableSequence
from ruamel.yaml.comments import CommentedMap
from .util import validateSchema, assertForm #, GitErOpError

class ExternalValue(object):

  def _setResolved(self, key, value):
    self.getter = key

  def get(self):
    pass

# XXX __setstate__

  def resolve(self, key=None):
    key = key or self.getter
    if key:
      value = self.get()
      getter = getattr(value, '__reflookup__', None)
      if getter:
        return getter(key)
      else:
        return value[key]
    else:
      return self.get()

  def asRef(self, options=None):
    if options and options.get('resolveExternal'):
      return serializeValue(self.resolve(), **options)
    # external:local external:secret
    serialized = {self.type: self.key}
    if self.getter:
      serialized['get'] = self.getter
    return {'ref': serialized}

  @staticmethod
  def resolveAll(value):
    if isinstance(value, Mapping):
      return dict((key, ExternalValue.resolveAll(v)) for key, v in value.items())
    elif isinstance(value, (list, tuple)):
      return [ExternalValue.resolveAll(item) for item in value]
    elif isinstance(value, ExternalValue):
      return value.resolve()
    else:
      return value

def runTemplate(data, vars=None, dataLoader=None):
  from ansible.template import Templar
  from ansible.parsing.dataloader import DataLoader
  # implementation notes:
  #   see https://github.com/ansible/ansible/test/units/template/test_templar.py
  #   see ansible.template.vars.AnsibleJ2Vars,
  #   dataLoader is only used by _lookup and to set _basedir (else ./)
  return Templar(dataLoader or DataLoader(), variables=vars).template(data)

def mapValue(value, resourceOrCxt):
  if not isinstance(resourceOrCxt, RefContext):
    resourceOrCxt = RefContext(resourceOrCxt)
  return _mapValue(value, resourceOrCxt)

def _mapValue(value, ctx):
  if Ref.isRef(value):
    value = Ref(value).resolveOne(ctx)

  if isinstance(value, Mapping):
    return dict((key, _mapValue(v, ctx)) for key, v in value.items())
  elif isinstance(value, (list, tuple)):
    return [_mapValue(item, ctx) for item in value]
  elif isinstance(value, six.string_types):
    templar = ctx.currentResource.templar
    vars = dict(__giterop = ctx)
    vars.update(ctx.vars)
    templar.set_available_variables(vars)
    value = templar.template(value)
    if Ref.isRef(value):
      value = Ref(value).resolveOne(ctx)
    return value
  elif ctx.resolveExternal and isinstance(value, ExternalValue):
    return value.resolve()
  return value

def serializeValue(value, **kw):
  getter = getattr(value, 'asRef', None)
  if getter:
    return getter(kw)
  if isinstance(value, Mapping):
    return dict((key, serializeValue(v, **kw)) for key, v in value.items())
  elif isinstance(value, (list, tuple)):
    return [serializeValue(item, **kw) for item in value]
  else:
    return value

class RefContext(object):
  def __init__(self, currentResource, vars=None, wantList=False, resolveExternal=True):
    self.vars = vars or {}
    # the original context:
    self.currentResource = currentResource
    # the last resource encountered while evaluating:
    self._lastResource = currentResource
    self.lastKeys = []
    # current segment is the final segment:
    self._rest = None
    self.wantList = wantList
    self.resolveExternal = resolveExternal

  def copy(self, resource=None, vars=None, wantList=None):
    copy = RefContext(resource or self.currentResource, self.vars, self.wantList, self.resolveExternal)
    if vars:
      copy.vars.update(vars)
    if wantList is not None:
      copy.wantList = wantList
    return copy

class Expr(object):
  def __init__(self, exp, vars = None):
    self.vars = {
     'true': True, 'false': False, 'null': None
    }

    if vars:
      self.vars.update(vars)

    self.source = exp
    paths = list(parseExp(exp))
    if (not paths[0].key or paths[0].key[0] not in '.$') and (paths[0].key or paths[0].filters):
      # unspecified relative path: prepend segment to select ancestors
      paths[:0] = [Segment('.ancestors', [], '?', [])]
    self.paths = paths

  def __repr__(self):
    # XXX vars
    return "Expr('%s')" % self.source

  def resolve(self, ctx):
    currentResource = ctx.currentResource
    vars = mapValue(self.vars, currentResource)
    vars['start'] = currentResource
    context = ctx.copy(currentResource, vars)
    if not self.paths[0].key and not self.paths[0].filters: # starts with "::"
      currentResource = currentResource.all
      paths = self.paths[1:]
    elif self.paths[0].key and self.paths[0].key[0] == '$':
      #if starts with a var, use that as the start
      varName = self.paths[0].key[1:]
      currentResource = context.vars[varName]
      if len(self.paths) == 1:
        # bare reference to a var, just return it's value
        return [currentResource]
      paths = [self.paths[0]._replace(key='')] + self.paths[1:]
    else:
      paths = self.paths
    return evalExp([currentResource], paths, context)

class Ref(object):
  """
  A Ref objects describes a path to metadata associated with a resource.

  The syntax for a Ref path expression is:

  expr:  segment? ('::' segment)*

  segment: key? ('[' filter ']')* '?'?

  key: name | integer | var | '*'

  filter: '!'? expr? (('!=' | '=') test)?

  test: var | (^[$[]:?])*

  var: '$' name

  Semantics

  Each segment specifies a key in a resource or JSON/YAML object.
  "::" is used as the segment deliminated to allow for keys that contain "." and "/"

  Path expressions evaluations always start with a list of one or more Resources.
  and each segment selects the value associated with that key.
  If segment has one or more filters
  each filter is applied to that value -- each is treated as a predicate
  that decides whether value is included or not in the results.
  If the filter doesn't include a test the filter tests the existence or non-existence of the expression,
  depending on whether the expression is prefixed with a "!".
  If the filter includes a test the left side of the test needs to match the right side.
  If the right side is not a variable, that string will be coerced to left side's type before comparing it.
  If the left-side expression is omitted, the value of the segment's key is used and if that is missing, the current value is used.

  If the current value is a list and the key looks like an integer
  it will be treated like a zero-based index into the list.
  Otherwise the segment is evaluated again all values in the list and resulting value is a list.
  If the current value is a dictionary and the key is "*", all values will be selected.

  If a segment ends in "?", it will only include the first match.
  In other words, "a?::b::c" is a shorthand for "a[b::c]::0::b::c".
  This is useful to guarantee the result of evaluating expression is always a single result.

  The first segment:
  If the first segment is a variable reference the current value is set to that variable's value.
  If the key in the first segment is empty (e.g. the expression starts with '::') the current value will be set to the evaluation of '.all'.
  If the key in the first segment starts with '.' it is evaluated against the initial "current resource".
  Otherwise, the current value is set to the evaluation of ".ancestors?". In other words,
  the expression will be the result of evaluating it against the first ancestor of the current resource that it matches.

  If key or test needs to be a non-string type or contains a unallowed character use a var reference instead.

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
  .children    child resources
  .descendents (including self)
  .all       dictionary of child resources with their names as keys
  .configurations

  Special resource names:

  root
  kms
  """

  def __init__(self, exp, vars = None):
    self.vars = {
     'true': True, 'false': False, 'null': None
    }

    self.foreach = None
    if isinstance(exp, Mapping):
      if 'q' in exp:
        self.source = exp
        return

      self.vars.update(exp.get('vars', {}))
      if 'foreach' in exp:
        self.foreach = exp
      exp = exp.get('eval', exp.get('ref',''))

    if vars:
      self.vars.update(vars)
    self.source = exp

  def resolve(self, ctx, wantList=True):
    """
    Return a list of matches
    Note that values in the list can be a list or None
    """
    ctx = ctx.copy(vars=self.vars, wantList=wantList)
    results = eval(self.source, ctx, True)

    if results is None or not self.foreach:
      return results
    return eval(self.foreach, ctx.copy(results, self.vars, wantList), True)

  def resolveOne(self, ctx):
    """
    If no match return None
    If more than one match return a list of matches
    Otherwise return the match

    Note: If you want to distinguish between None values and no match
    or between single match that is a list and a list of matches
    use resolve() which always returns a (possible empty) of matches
    """
    return self.resolve(ctx, False)

  @staticmethod
  def isRef(value):
    if isinstance(value, Mapping):
      if 'ref' in value or 'eval' in value:
        return len([x for x in ['vars', 'foreach'] if x in value]) + 1 == len(value)
      if 'q' in value:
        return len(value) == 1
      return False
    return isinstance(value, Ref)

def ifFunc(arg, ctx):
  kw = ctx.kw
  result = eval(arg, ctx)
  if result:
    return eval(kw.get('then'), ctx)
  else:
    return eval(kw.get('else'), ctx)

def orFunc(arg, ctx):
  args = eval(arg, ctx)
  assert isinstance(args, list)
  for arg in args:
    val = eval(arg, ctx)
    if val:
      return val

def notFunc(arg, ctx):
  result = eval(arg, ctx)
  return not result

def andFunc(arg, ctx):
  args = eval(arg, ctx)
  assert isinstance(args, list)
  for arg in args:
    val = eval(arg, ctx)
    if not val:
      return val
  return val

def quoteFunc(arg, ctx):
  return arg

def eqFunc(arg, ctx):
  args = eval(arg, ctx)
  assert isinstance(args, list) and len(args) == 2
  return eval(args[0], ctx) == eval(args[1], ctx)

def validateSchemaFunc(arg, ctx):
  args = eval(arg, ctx)
  assert isinstance(args, list) and len(args) == 2
  return not not validateSchema(eval(args[0], ctx), eval(args[1], ctx))

def _forEach(results, ctx):
  keyExp = ctx.kw['foreach'].get('key')
  valExp = ctx.kw['foreach']['value']
  ictx = RefContext(ctx.currentResource, ctx.vars.copy())
  ictx.resolveExternal = ctx.resolveExternal

  Break = object()
  Continue = object()
  def makeItems():
    for i, (k, v) in enumerate(results):
      ictx.currentResource = v
      ictx.vars['collection'] =  ctx.currentResource
      ictx.vars['index'] = i
      ictx.vars['key'] = k
      ictx.vars['item'] = v
      ictx.vars['break'] = Break
      ictx.vars['continue'] = Continue
      if keyExp:
        key = eval(keyExp, ictx)
        if key is Break:
          break
        elif key is Continue:
          continue
      val = eval(valExp, ictx)
      if val is Break:
        break
      elif val is Continue:
        continue
      if keyExp:
        yield (key, val)
      else:
        yield val

  if keyExp:
    return CommentedMap(makeItems())
  else:
    return list(makeItems())

def forEachFunc(arg, ctx):
  results = ctx.currentResource
  if results:
    if isinstance(results, Mapping):
      return _forEach(results.items(), ctx)
    elif isinstance(results, MutableSequence):
      return _forEach(enumerate(results), ctx)
    else:
      return _forEach([(0, results)], ctx)
  else:
    return results

_Funcs = {
  'if': ifFunc,
  'and': andFunc,
  'or': orFunc,
  'not': notFunc,
  'q': quoteFunc,
  'eq': eqFunc,
  'validate': validateSchemaFunc,
  'foreach': forEachFunc,
}

def getEvalFuncs(name):
  return _Funcs.get(name)

def setEvalFuncs(name, val):
  _Funcs[name] = val

def eval(val, ctx, top=False):
  if isinstance(val, Mapping):
    for key in val:
      func = _Funcs.get(key)
      if func:
        break
    else:
      return _mapValue(val, ctx)

    if not top:
      # functions assume this
      ctx = ctx.copy()
      ctx.resolveExternal = True
    args = val[key]
    ctx.kw = val
    ctx.currentFunc = key
    wantList = ctx.wantList
    # functions assume resolveOne semantics
    ctx.wantList = False

    results = func(args, ctx)
    if ctx.resolveExternal:
      results = ExternalValue.resolveAll(results)
    if wantList:
      if results is None:
        return []
      elif not isinstance(results, list):
        return [results]
    return results
  elif isinstance(val, six.string_types):
    expr = Expr(val, ctx.vars)
    results = expr.resolve(ctx)
    if not ctx.wantList:
      if not results:
        return None
      elif len(results) == 1:
        return results[0]
    return results
  else:
    return _mapValue(val, ctx)

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

def lookup(value, key, context, external):
  try:
    # if key == '.':
    #   key = context.currentKey

    if context and isinstance(key, six.string_types) and key.startswith('$'):
      key = context.vars[key[1:]]

    if external:
      value = external.resolve(key)
      context.lastKeys = []
    else:
      getter = getattr(value, '__reflookup__', None)
      if getter:
        # XXX check if value really is a resource
        context._lastResource = value
        value = getter(key)
      else:
        value = value[key]
    context.lastKeys.append(key)

    if not context._rest:
      # this will be in the final result so map the whole object
      final = _mapValue(value, context.copy(context._lastResource))
      if external:
        external._setResolved(key, final)
      return [final]
    elif Ref.isRef(value):
      return Ref(value).resolve(context.copy(context._lastResource))
    else:
      return [value]
  except (KeyError, IndexError, TypeError, ValueError):
    return []

def evalItem(v, seg, context):
  """
    apply current item to current segment, return [] or [value]
  """
  external = isinstance(v, ExternalValue)
  if external:
    v = v.get()

  if seg.key != '':
    v = lookup(v, seg.key, context, external)
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
  # use external if this is that last segment
  yield external if external and not context._rest else v

def _treatAsSingular(item, seg):
  if seg.key == '*':
    return False
  return not isinstance(item, list) or isinstance(seg.key, six.integer_types)

def recursiveEval(v, exp, context):
  """
  given a list of (previous) results,
  yield a list of results
  """
  matchFirst = exp[0].modifier == '?'
  useValue = exp[0].key == '*'

  for item in v:
    assert item.__class__.__name__ != 'Resolved', v
    if _treatAsSingular(item, exp[0]):
      rest = exp[1:]
      context._rest = rest
      iv = evalItem(item, exp[0], context)
    else:
      if useValue:
        if not isinstance(item, Mapping):
          continue
        iv = item.values()
        rest = exp[1:] # advance past "*" segment
      else:
        # flattens
        iv = item
        rest = exp

    #iv will be a generator or list
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
  paths = path.split('::')
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

def runLookup(name, templar, *args, **kw):
  from ansible.plugins.loader import lookup_loader
  # "{{ lookup('url', 'https://toshio.fedorapeople.org/one.txt', validate_certs=True) }}"
  #       would end up calling the lookup plugin named url's run method like this::
  #           run(['https://toshio.fedorapeople.org/one.txt'], variables=available_variables, validate_certs=True)
  instance = lookup_loader.get(name, loader = templar._loader, templar = templar)
  # ansible_search_path = []
  result = instance.run(args, variables=templar._available_variables, **kw)
  # XXX check for wantList
  if not result:
    return None
  if len(result) == 1:
    return result[0]
  else:
    return result

def lookupFunc(arg, ctx):
  """
  lookup:
    - file: 'foo' or []
    - blah:
  """
  if isinstance(arg, Mapping):
    assert len(arg) == 1
    name, args = list(arg.items())[0]
    kw = {}
  else:
    assertForm(arg, list)
    name, args = list(assertForm(arg[0]).items())[0]
    kw = dict(list(assertForm(kw).items())[0] for kw in arg[1:])

  if not isinstance(args, list):
    args = [args]
  return runLookup(name, ctx.currentResource.templar, *args, **kw)

_Funcs['lookup'] = lookupFunc
