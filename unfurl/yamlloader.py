import os.path
import sys
import six
import codecs
from six.moves import urllib

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.representer import RepresenterError

from .util import sensitive_str, UnfurlError, UnfurlValidationError
from .util import expandDoc, findSchemaErrors, makeMapWithBase
from toscaparser.common.exception import ExceptionCollector
from toscaparser.common.exception import URLException
from toscaparser.utils.gettextutils import _

import logging
logger = logging.getLogger('unfurl')
yaml = YAML()
# monkey patch for better error message
def represent_undefined(self, data):
    raise RepresenterError('cannot represent an object: %s of type %s' % (data, type(data)))
yaml.representer.represent_undefined = represent_undefined
yaml.representer.add_representer(None, represent_undefined)

def represent_sensitive(dumper, data):
  return dumper.represent_scalar(u'tag:yaml.org,2002:str', "[[REDACTED]]")
yaml.representer.add_representer(sensitive_str, represent_sensitive)

def load_yaml(path, isFile=True, importLoader=None):
  try:
    originalPath = path
    bare = False
    manifest = importLoader and getattr(importLoader.tpl, 'manifest', None)
    # check if this path is into a git repo
    if manifest:
      repo, filePath, revision, bare = manifest.findRepoFromGitUrl(path, isFile, importLoader, True)
      if repo: # it's a git repo
        if bare:
          return yaml.load(repo.show(filePath, revision))
        # find the project that the loading file is in
        # tracks which commit was used, returns a workingDir
        workingDir = repo.checkout(revision)
        path = os.path.join(workingDir, filePath)
        isFile = True
      else:
        # if it's a file, check if it's only in the repo
        if isFile:
          repo, filePath, revision, bare = manifest.findPathInRepos(path, importLoader, True)
          if repo:
            if bare:
              return yaml.load(repo.show(filePath, revision))
            else:
              path = os.path.join(repo.workingDir, filePath)

    # XXX urls and files outside of a repo should be saved and commited to the repo the importLoader is in
    f = None
    try:
        f = codecs.open(path, encoding='utf-8', errors='strict') if isFile \
            else urllib.request.urlopen(path)
    except urllib.error.URLError as e:
        if hasattr(e, 'reason'):
            msg = (_('Failed to reach server "%(path)s". Reason is: '
                     '%(reason)s.')
                   % {'path': path, 'reason': e.reason})
            ExceptionCollector.appendException(URLException(what=msg))
            return
        elif hasattr(e, 'code'):
            msg = (_('The server "%(path)s" couldn\'t fulfill the request. '
                     'Error code: "%(code)s".')
                   % {'path': path, 'code': e.code})
            ExceptionCollector.appendException(URLException(what=msg))
            return
    except Exception as e:
        raise
    return yaml.load(f.read())
  except:
    if bare:
      msg = 'Could not retrieve %s from repo (originally %s)' % (filePath, originalPath)
    elif path != originalPath:
      msg = 'Could not load "%s" (originally "%s")' % (path, originalPath)
    else:
      msg = 'Could not load "%s"' % path
    raise UnfurlError(msg, True)

import toscaparser.imports
toscaparser.imports.YAML_LOADER = load_yaml

class YamlConfig(object):
  def __init__(self, config=None, path=None, validate=True, schema=None, loadHook=None):
    try:
      self.schema = schema
      if path:
        self.path = os.path.abspath(path)
        if os.path.isfile(self.path):
          with open(self.path, 'r') as f:
            config = f.read()
        # otherwise use default config
      else:
        self.path = None

      if isinstance(config, six.string_types):
        if self.path:
          # set name on a StringIO so parsing error messages include the path
          config = six.StringIO(config)
          config.name = self.path
        self.config = yaml.load(config)
      elif isinstance(config, dict):
        self.config = CommentedMap(config.items())
      else:
        self.config = config
      if not isinstance(self.config, CommentedMap):
        raise UnfurlValidationError('invalid YAML document: %s' % self.config)

      self._cachedDocIncludes = {}
      #schema should include defaults but can't validate because it doesn't understand includes
      #but should work most of time
      self.config.loadTemplate = self.loadInclude
      self.loadHook = loadHook

      self.baseDirs = [self.getBaseDir()]
      self.includes, expandedConfig = expandDoc(self.config, cls=makeMapWithBase(self.baseDirs[0]))
      self.expanded = expandedConfig
      # print('expanded')
      # yaml.dump(config, sys.stdout)
      errors = schema and self.validate(expandedConfig)
      if errors and validate:
        (message, schemaErrors) = errors
        raise UnfurlValidationError("JSON Schema validation failed: " + message, errors)
      else:
        self.valid = not not errors
    except:
      if self.path:
        msg = "Unable to load yaml config at %s" % self.path
      else:
        msg = "Unable to parse yaml config"
      raise UnfurlError(msg, True)

  def loadYaml(self, path, baseDir=None):
    path = os.path.abspath(os.path.join(baseDir or self.getBaseDir(), path))
    with open(path, 'r') as f:
      config = yaml.load(f)
    return path, config

  def getBaseDir(self):
    if self.path:
      return os.path.dirname(self.path)
    else:
      return '.'

  def dump(self, out=sys.stdout):
    yaml.dump(self.config, out)

  def validate(self, config):
    return findSchemaErrors(config, self.schema)

  def loadInclude(self, templatePath):
    if templatePath == self.baseDirs[-1]:
      self.baseDirs.pop()
      return

    if isinstance(templatePath, dict):
      value = templatePath.get('merge')
      if 'file' not in templatePath:
        raise UnfurlError('file missing from document %%include: %s' % templatePath)
      key = templatePath['file']
    else:
      value = None
      key = templatePath

    if key in self._cachedDocIncludes:
      path, template = self._cachedDocIncludes[key]
      baseDir = os.path.dirname(path)
      self.baseDirs.append(baseDir)
      return value, template, baseDir

    try:
      baseDir = self.baseDirs[-1]
      if self.loadHook:
        path, template = self.loadHook(self, templatePath, baseDir)
      else:
        path, template = self.loadYaml(key, baseDir)
      newBaseDir = os.path.dirname(path)
    except:
      raise UnfurlError('unable to load document %%include: %s (base: %s)' % (templatePath, baseDir), True, True)
    self.baseDirs.append(newBaseDir)

    self._cachedDocIncludes[key] = [path, template]
    return value, template, newBaseDir

def loadFromRepo(import_name, import_uri_def, basePath, repositories, manifest):
  """
  Returns (url or fullpath, parsed yaml)
  """
  context = CommentedMap(import_uri_def.items())
  context['base'] = basePath
  context['repositories'] = repositories
  context.manifest = manifest
  uridef = {k: v for k, v in import_uri_def.items() if k in ['file', 'repository']}
  # _load_import_template will invoke load_yaml above
  loader = toscaparser.imports.ImportsLoader(None, basePath, tpl=context)
  return (loader._load_import_template(import_name, uridef))
