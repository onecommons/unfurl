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
    self.inherited = 'inherited' in obj
    if isinstance(self.type, dict):
      templateName = self.type.get('template')
      if templateName:
        self.type = 'template'
        template = manifest.templates.get(templateName)
        self.template = template.attributes

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

  def isValueCompatible(self, value, resolver=None, item=False):
    quantity = 0 if item else getattr(self, 'list', 0)
    isList = isinstance(value, list)
    if quantity > 1 and not isList:
      return False
    elif isList:
      if quantity:
        return all([self.isValueCompatible(v, resolver, True) for v in value])
      else:
        return False
    #else: fallthrough

    if not self.type:
      return True

    if self.type == 'always':
      return self.default == value

    if self.type == 'resource':
      from .resource import Resource
      return isinstance(value, Resource)

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

    if ValueFrom.isRef(value):
      if resolver:
        return self.isValueCompatible(ValueFrom.resolve(value, resolver), resolver)
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

def lookupPath(source, keys):
  value = source
  for key in keys:
    if isinstance(value, list):
      key = int(key)
    value = value[key]
  return value

#cf. valueFrom: {"fieldRef": {"fieldPath": "metadata.namespace"}}
#resourceFieldRef, secretRef, configmapRef
# use case: default parameter: :clusterkms:aSecret
# but default doesn't know the actual resource name, clusterkms is a well-known variable
# valueFrom: .:resourceref:path which resolves to another valuefrom
class ValueFrom(object):
  """
  valueFrom: "resourcename:attribute:child:0"

  Use ":" as delimiters, list index or dictionary keys

  '.' '..' '', relative resources, empty search for nearest match
  Resolves to resource or a value
  """
  def __init__(self, path):
    if isinstance(path, dict):
      path = path.get('valueFrom','')
    #use : as delimiters because attribute names can look like: kops.k8s.io/cluster
    parts = path if isinstance(path, (list,tuple)) else path.split(':')
    self.resourceName = parts[0]
    self.attributePath = parts[1:]

  def __repr__(self):
    return "ValueFrom('%s:%s')" % (self.resourceName, ':'.join(self.attributePath))

  def findResource(self, currentResource):
    resourceName = self.resourceName
    if not resourceName:
      search = self.attributePath[0]
      resource = currentResource
      while resource:
        if search in resource:
          return resource
        resource = resource.parent
      return None
    elif resourceName == '.':
      return currentResource
    elif resourceName == '..':
      return currentResource.parent
    else:
      return currentResource.definition.findResource(self.resourceName)

  def resolve(self, currentResource):
    resource = self.findResource(currentResource)
    if not resource:
      if self.resourceName:
        raise GitErOpError("valueFrom to unknown resource %s" % self.resourceName)
      else:
        raise GitErOpError("valueFrom can't find %s"
                              % (self.attributePath and self.attributePath[0]))

    if self.attributePath:
      try:
        return lookupPath(resource, self.attributePath)
      except Exception as e:
        raise GitErOpError("bad path in %r" % self)
    else:
      return resource

  @staticmethod
  def resolveIfRef(value, resolver):
    if isinstance(value, ValueFrom):
      return value.resolve(resolver)
    elif ValueFrom.isRef(value):
       return ValueFrom(value).resolve(resolver)
    else:
      return value

  @staticmethod
  def isRef(value):
    if isinstance(value, dict):
      return 'valueFrom' in value and len(value) == 1
    return isinstance(value, ValueFrom)

  # def getProvenence(self):
  #   """
  #   Return the who and when about the referenced value
  #   """
  #   #look through the resource changes, if not found hasn't changed since the resource's creation
