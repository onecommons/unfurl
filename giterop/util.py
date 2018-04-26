import six

VERSION = 'giterops/v1alpha1'
TEMPLATESKEY = 'templates'
CONFIGURATORSKEY = 'configurators'

class GitErOpError(Exception):
  def __init__(self, message, errors=None):
    super(GitErOpError, self).__init__(message)
    self.errors = errors or []

class GitErOpValidationError(GitErOpError):
  pass

class GitErOpTaskError(GitErOpError):
  def __init__(self, task, message):
    super(GitErOpTaskError, self).__init__(message, [task])

def assertForm(src, types=dict):
  if not isinstance(src, types):
    raise GitErOpError('Malformed definition: %s' % src)
  return src

_ClassRegistry = {}
def registerClass(apiVersion, kind, factory):
  api = _ClassRegistry.setdefault(apiVersion, {})
  api[kind] = factory

def lookupClass(kind, apiVersion, default=None):
  version = apiVersion or VERSION
  api = _ClassRegistry.get(version)
  if api:
    klass = api.get(kind, default)
  else:
    klass = default
  if not klass:
    raise GitErOpError('Can not find class %s.%s' % (version, kind))
  return klass

#XXX ansible potential other types: manifests, templates, helmfiles, service broker bundles
ConfiguratorTypes = []

class AttributeDefinition(object):
  def __init__(self, obj, manifest, validate=True):
    if validate:
      for k in "name enum".split():
        if not obj.get(k):
          if k != "enum" or obj.get('type') == "enum":
            raise GitErOpError('attribute definition missing "%s"' % k)
    for key in "name type required enum list".split():
      setattr(self, key, obj.get(key))
    self.secret = self.name.startswith('secret_') or obj.get('secret')
    if 'default' in obj:
      self.default = obj['default']
    elif 'always' in obj:
      self.type == 'always'
      self.default = obj['always']
    if isinstance(self.type, dict):
      templateName = self.type.get('template')
      if templateName:
        self.type = 'template'
        template = manifest.templates.get(templateName)
        self.template = template.attributes
    if not self.type:
      self.type = 'string'

  def merge(self, defs):
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

  def isValueCompatible(self, value, item=False):
    quantity = 0 if item else getattr(self, 'list', 0)
    isList = isinstance(value, list)
    if quantity > 1 and not isList:
      return False
    elif isList:
      if quantity:
        return all([self.isValueCompatible(v, True) for v in value])
      else:
        return False

    if isinstance(value, dict):
      if 'valueref' in value and len(value) == 1:
        return self.isValueCompatible(resolveValueRef(value))
      if self.type == 'template':
        return not self.template.validateParams(value)

    if self.type == 'always':
      return self.default == value

    if isinstance(value, (int, long, float)):
      if self.type == 'int':
        return round(value) == value
      return self.type == 'number'

    if isinstance(value, six.string_types):
      if self.type == 'enum':
        return value in self.enum
      return self.type == 'string'

    if isinstance(value, bool):
      return self.type == 'boolean'
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

  def merge(self, overrides, manifest):
    for defs in overrides:
      name = defs.get('name')
      attrDef = self.attributes.get(name)
      if attrDef:
        attrDef.merge(defs)
      else:
        self.attributes[name] = AttributeDefinition(defs, manifest)

  def validateParameters(self, params, includeUnexpected=True):
    status = self.checkParameters(params, includeUnexpected)
    if status:
      raise GitErOpValidationError("bad parameters: %s" % status, status)

  def checkParameters(self, params, includeUnexpected=True):
    params = params.copy()
    status = []
    for paramdef in self.attributes.values():
      value = params.pop(paramdef.name, None)
      if value is None:
        if paramdef.required:
          status.append(("missing required parameter", paramdef.name))
      elif not paramdef.isValueCompatible(value):
        status.append(("invalid value", (paramdef.name, value)))
    if includeUnexpected and params:
      status.append( ("unexpected parameters", params.keys()) )
    return status

def lookupPath(source, keys):
  value = source
  for key in keys:
    if isinstance(value, list):
      key = int(key)
    value = value[key]
  return value

class ValueRef(object):
  """
  valueref: "resourcename:attribute:child:0"

  Use ":" as delimiters, list index or dictionary keys

  Resolves to resource or a value
  """
  def __init__(self, path):
    #use : as delimiters because attribute names can look like: kops.k8s.io/cluster
    parts = path if isinstance(path, (list,tuple)) else path.split(':')
    self.resourcename = parts[0]
    self.attributePath = parts[1:]

  def resolve(self, findResource):
    resource = findResource(self.resourcename)
    if not resource:
      raise GitErOpError("valueref to unknown resource %s" % self.resourcename)

    if self.attributePath:
      return lookupPath(resource.metadata, self.attributePath)
    else:
      return resource

  def getProvence(self):
    """
    Return the who and when about the referenced value
    """
    #look through the resource changes, if not found hasn't changed since the resource's creation
