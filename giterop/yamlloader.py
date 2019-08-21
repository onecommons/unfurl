import os.path
import sys
import six
import codecs
from six.moves import urllib

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from .util import expandDoc, GitErOpValidationError, findSchemaErrors
from toscaparser.common.exception import ExceptionCollector
from toscaparser.common.exception import URLException
from toscaparser.utils.gettextutils import _

import logging
logger = logging.getLogger('giterup')
yaml = YAML()

def load_yaml(path, isFile=True, importLoader=None):
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

import toscaparser.imports
toscaparser.imports.YAML_LOADER = load_yaml

class YamlConfig(object):
  def __init__(self, config=None, path=None, validate=True, schema=None, loadHook=None):
    self.schema = schema
    if path:
      self.path = os.path.abspath(path)
      if os.path.isfile(self.path):
        with open(self.path, 'r') as f:
          config = f.read()
    else:
      self.path = None

    if isinstance(config, six.string_types):
      if path:
        # set name on a StringIO so parsing error messages include the path
        config = six.StringIO(config)
        config.name = path
      self.config = yaml.load(config)
    elif isinstance(config, dict):
      self.config = CommentedMap(config.items())
    else:
      self.config = config
    if not isinstance(self.config, CommentedMap):
      raise GitErOpValidationError('invalid YAML document: %s' % self.config)

    self._cachedDocIncludes = {}
    #schema should include defaults but can't validate because it doesn't understand includes
    #but should work most of time
    self.config.loadTemplate = self.loadInclude
    self.loadHook = loadHook

    self.baseDirs = [self.getBaseDir()]
    self.includes, config = expandDoc(self.config, cls=CommentedMap)
    self.expanded = config
    # print('expanded')
    # yaml.dump(config, sys.stdout)
    errors = schema and self.validate(config)
    if errors and validate:
      # errors = (message, errors)
      raise GitErOpValidationError(*errors)
    else:
      self.valid = not not errors

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
    if templatePath is expandDoc:
      self.baseDirs.pop()
      return

    if isinstance(templatePath, dict):
      value = templatePath.get('merge')
      key = templatePath['file']
    else:
      value = None
      key = templatePath

    if key in self._cachedDocIncludes:
      path, template = self._cachedDocIncludes[key]
      self.baseDirs.append(os.path.dirname(path))
      return value, template

    if self.loadHook:
      path, template = self.loadHook(self, templatePath, self.baseDirs[-1])
    else:
      path, template = self.loadYaml(key, self.baseDirs[-1])
    self.baseDirs.append(os.path.dirname(path))

    self._cachedDocIncludes[key] = [path, template]
    return value, template

def loadFromRepo(import_name, import_uri_def, basePath, repositories, manifest):
  """
  Returns (url or fullpath, parsed yaml)
  """
  context = CommentedMap(import_uri_def.items())
  context['base'] = basePath
  context['repositories'] = repositories
  context.manifest = manifest
  uridef = {k: v for k, v in import_uri_def.items() if k in ['file', 'repository']}
  # this will invoke load_yaml above
  loader = toscaparser.imports.ImportsLoader(None, basePath, tpl=context)
  return (loader._load_import_template(import_name, uridef))
