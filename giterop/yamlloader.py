import os.path
import sys
import six
import codecs
from six.moves import urllib

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from .util import expandDoc, GitErOpValidationError, findSchemaErrors
from .repo import findGitRepo
from toscaparser.common.exception import ExceptionCollector
from toscaparser.common.exception import URLException
from toscaparser.utils.gettextutils import _

import logging
logger = logging.getLogger('giterup')
yaml = YAML()

def load_yaml(path, isFile=True, importLoader=None):
    from .localenv import LocalEnv
    # check if this path is to a git repo
    repoURL, filePath, revision = findGitRepo(path, isFile, importLoader)
    if repoURL: # it's a git repo
      # find the project that the loading file is in
      workingDir = LocalEnv.active.findOrCreateWorkingDir(repoURL, revision, importLoader and importLoader.path)
      path = os.path.join(workingDir, filePath)
      isFile = True

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
  def __init__(self, config=None, path=None, validate=True, schema=None, loadFromRepo=None):
    self.schema = schema
    if path:
      self.path = os.path.abspath(path)
      if os.path.isfile(self.path):
        with open(self.path, 'r') as f:
          config = f.read()
    else:
      self.path = None

    if isinstance(config, six.string_types):
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
    self.loadFromRepo = loadFromRepo

    self.includes, config = expandDoc(self.config, cls=CommentedMap)
    self.expanded = config
    # print('expanded')
    # yaml.dump(config, sys.stdout)
    errors = schema and self.validate(config)
    if errors and validate:
      raise GitErOpValidationError(*errors)
    else:
      self.valid = not not errors

  def loadYaml(self, path):
    path = os.path.abspath(os.path.join(self.getBaseDir(), path))
    with open(path, 'r') as f:
      config = f.read()
    return yaml.load(config)

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
    if isinstance(templatePath, dict):
      value = templatePath.get('merge')
      key = templatePath['file']
    else:
      key = templatePath

    if key in self._cachedDocIncludes:
      return value, self._cachedDocIncludes[key]

    if self.loadFromRepo:
      template = self.loadFromRepo(templatePath, self.getBaseDir())
    else:
      template = self.loadYaml(key)

    self._cachedDocIncludes[key] = template
    return value, template

def loadFromRepo(import_name, import_uri_def, basePath, repositories, repoType):
  """
  Returns (url or fullpath, parsed yaml)
  """
  context = import_uri_def.copy()
  context['repoType'] =  repoType
  context['base'] = basePath
  context['repositories'] = repositories
  uridef = {k: v for k, v in import_uri_def.items() if k in ['file', 'repository']}
  # this will invoke load_yaml above
  return (toscaparser.imports.ImportsLoader(None, basePath, tpl=context)
            ._load_import_template(import_name, uridef))
