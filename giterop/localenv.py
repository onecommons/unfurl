"""
Classes for managing the local environment.

Repositories can optionally be organized into projects that have a local configuration.

There is always a home project that contains the local secret instances.
"""
import os
import os.path
# import six
from .repo import Repo
from .util import (GitErOpError)
from .yamlloader import YamlConfig

DefaultManifestName = 'manifest.yaml'
DefaultLocalConfigName = 'giterop.yaml'
DefaultHomeDirectory = '.giterop_home'
HiddenMarkerName = '.giterop'

class Project(object):
  """
  A GitErOp project folder is a folder that contains

  The spec and instance repo and an local configuration file (giterop.yaml)

  project/spec/.git # has template.yaml and manifest-template.yaml
         instances/current/.git # has manifest.yaml
          # subprojects are created by repository declarations in spec or instance
          subproject/spec/
                    instances/current
          giterop.yaml # might create 'secret' or 'local' subprojects
          revisions/...
  """
  def __init__(self, path, localEnv):
    if os.path.isdir(path):
      self.projectRoot = path
      test = os.path.join(self.projectRoot, localEnv.DefaultLocalConfigName)
      # XXX merge or copy homeConfig
      if os.path.exists(test):
        self.localConfig = LocalConfig(test)
      elif localEnv.homeProject:
        self.localConfig = localEnv.homeProject.localConfig
      else:
        self.localConfig = LocalConfig()
    else:
      self.projectRoot = os.path.dirname(path)
      # XXX merge homeConfig
      self.localConfig = LocalConfig(path)

    self.workingDirs = Repo.findGitWorkingDirs(self.projectRoot)

  def getCurrentInstanceRepo(self):
    return os.path.join(self.projectRoot, 'instances', 'current')

  def findDefaultInstanceManifest(self):
    fullPath = self.localConfig.getDefaultManifestPath()
    if fullPath:
      if not os.path.exists(fullPath):
        raise GitErOpError("The default manifest found in %s does not exist: %s" % (self.localConfig.config.path, os.path.abspath(fullPath)))
    else:
      fullPath = os.path.join(self.getCurrentInstanceRepo(), DefaultManifestName)
      if not os.path.exists(fullPath):
        raise GitErOpError("The default manifest does not exist: %s" % os.path.abspath(fullPath))
    return fullPath

  def isPathInProject(self, path):
    return os.path.abspath(self.projectRoot)+os.sep in os.path.abspath(path)+os.sep

  def _createPathForGitRepo(self, gitUrl):
    basename = name = os.path.splitext(os.path.basename(gitUrl))[0]
    counter = 1
    while os.path.exists(os.path.join(self.projectRoot, name)):
      name = basename + str(counter)
      counter += 1
    return os.path.join(self.projectRoot, name)

  def findGitRepo(self, repoURL, revision=None):
    candidate = None
    for dir, (url, repo) in self.workingDirs.items():
      if repoURL == url:
        if not revision or revision == repo.revision:
          return repo
        else:
          candidate = repo
    return candidate

  def findPathInRepos(self, path, importLoader=None):
    candidate = None
    for dir, (url, repo) in self.workingDirs.items():
      filePath, revision, bare = repo.findPath(path, importLoader)
      if filePath:
        if not bare:
          return repo, filePath, revision, bare
        else:
          candidate = (repo, filePath, revision, bare)
    return candidate or None, None, None, None

  def createWorkingDir(self, gitUrl, revision='HEAD'):
    localRepoPath = self._createPathForGitRepo(gitUrl)
    repo = Repo.createWorkingDir(gitUrl, localRepoPath, revision)
    # add to workingDirs
    self.workingDirs[localRepoPath] = (gitUrl, repo)
    return repo

class LocalConfig(object):
  """
  The local configuration.

  - a list of top-level projects
  - list of instance manifests with their local configuration
  - the default local and secret instances

giterop:
  version:

instances:
  - file:
    repository:
    # default instance if there are multiple instances in that project
    # (only applicable when config is local to a project)
    default: true
    local:
      file: path
      repository:
      resource: root
      # or:
      attributes:
        #XXX: inheritFrom:
    secret:

defaults: # used if the manifest isn't defined above
 local:
 secret:

projects:
  - path:
    default: True
    instance: instances/current
    spec: spec
"""
  def __init__(self, path=None):
    defaultConfig = {}
    # XXX define schema and validate
    self.config = YamlConfig(defaultConfig, path=path)
    self.manifests = self.config.config.get('instances', [])
    self.defaults = self.config.config.get('defaults', {})

  def adjustPath(self, path):
    """
    Makes sure relative paths are relative to the location of this local config
    """
    return os.path.join(self.config.getBaseDir(), path)

  def getDefaultManifestPath(self):
    if len(self.manifests) == 1:
      return self.adjustPath(self.manifests[0]['file'])
    else:
      for spec in self.manifests:
        if spec.get('default'):
          return self.adjustPath(spec['file'])
    return None

  def getLocalResource(self, manifestPath, localName, importSpec):
    """
    localName is either 'local' or 'secret'
    """
    from .runtime import Resource
    localRepo = None
    for spec in self.manifests:
      if manifestPath == self.adjustPath(spec['file']):
        localRepo = spec.get(localName)
    if not localRepo:
      localRepo = self.defaults.get(localName)

    if localRepo:
      attributes = localRepo.get('attributes')
      if attributes is not None:
        #XXX if inheritFrom or defaults in attributes: add .interface
        return Resource(localName, attributes)
      else:
        # the local or secret is a resource defined in a local manifest
        # set the url and resource name so the importing manifest loads it
        # XXX but should load here and save for re-use
        importSpec.update(localRepo)
        if 'file' in localRepo:
          importSpec['file'] = self.adjustPath(localRepo['file'])
        if 'repository' in localRepo:
          importSpec['repository'] = self.adjustPath(localRepo['repository'])
        return None

    # none found, return empty resource
    return Resource(localName)

class LocalEnv(object):
  """
  The class represents the local environment that a instance manifest runs in.

  The instance manifest and/or the current project
  The local configuration
  """
  homeProject = None

  def __init__(self, manifestPath=None, homepath=None):
    """
    If manifestPath is None find the first .giterop or manifest.yaml
    starting from the current directory.
    """
    # XXX need to save local config when changed
    self.homeConfigPath = self.getHomeConfigPath(homepath)
    self.homeProject = Project(self.homeConfigPath, self)

    if manifestPath:
      pathORproject = self.findManifestPath(manifestPath)
    else:
      # not specified: search current directory and parents for either a manifest or a project
      pathORproject = self.searchForManifestOrProject('.')

    if isinstance(pathORproject, Project):
      self.project = pathORproject
      self.manifestPath = pathORproject.findDefaultInstanceManifest()
    else:
      self.manifestPath = pathORproject
      self.project = self.findProject(os.path.dirname(pathORproject))

    self.instanceRepo = self._getInstanceRepo()
    self.config = self.project and self.project.localConfig or self.homeProject.localConfig

  # manifestPath specified
  #  doesn't exist: error
  #  is a directory: either instance repo or a project
  def findManifestPath(self, manifestPath):
    if not os.path.exists(manifestPath):
      raise GitErOpError("Manifest file does not exist: %s" % os.path.abspath(manifestPath))

    if os.path.isdir(manifestPath):
      test = os.path.join(manifestPath, DefaultManifestName)
      if os.path.exists(test):
        return test
      else:
        test = os.path.join(manifestPath, DefaultLocalConfigName)
        if os.path.exists(test):
          return Project(test, self)
        else:
          message = "Can't find a giterop manifest or project in folder: %s" % manifestPath
          raise GitErOpError(message)
    else:
      return manifestPath

  def _getInstanceRepo(self):
    instanceDir = os.path.dirname(self.manifestPath)
    if self.project and instanceDir in self.project.workingDirs:
      return self.project.workingDirs[instanceDir][1]
    else:
      return Repo.createGitRepoIfExists(instanceDir)

  def searchForManifestOrProject(self, dir):
    current = os.path.abspath(dir)
    while current and current != os.sep:
      test = os.path.join(current, DefaultManifestName)
      if os.path.exists(test):
        return test

      test = os.path.join(current, DefaultLocalConfigName)
      if os.path.exists(test):
        return Project(test, self)

      current = os.path.dirname(current)

    message = "Can't find a giterop repository in current directory (or any of the parent directories)"
    raise GitErOpError(message)

  def findProject(self, testPath):
    """
    Walk parents looking for giterop.yaml
    """
    current = os.path.abspath(testPath)
    while current and current != os.sep:
      test = os.path.join(current, DefaultLocalConfigName)
      if os.path.exists(test):
        return Project(test, self)
      current = os.path.dirname(current)
    return None

  def findMarker(self, testPath):
    test = os.path.join(testPath, HiddenMarkerName)
    if os.path.exists(test):
      return YamlConfig(test)
    else:
      return None

  def getHomeConfigPath(self, homepath):
    if homepath:
      if os.path.isdir(homepath):
        return os.path.abspath(os.path.join(homepath, DefaultLocalConfigName))
      else:
        return os.path.abspath(homepath)
    return os.path.expanduser(os.path.join('~', DefaultHomeDirectory, DefaultLocalConfigName))

  def getLocalResource(self, name, importSpec):
    if name != 'local' and name != 'secret':
      return None
    return self.config.getLocalResource(self.manifestPath, name, importSpec)

  def findGitRepo(self, repoURL, isFile=True, revision=None):
    if self.project:
      repo = self.project.findGitRepo(repoURL, revision)
      if not repo:
        return self.homeProject.findGitRepo(repoURL, revision)

  def findOrCreateWorkingDir(self, repoURL, isFile=True, revision=None, basepath=None):
    repo = self.findGitRepo(repoURL, revision)
    if not repo:
      if self.project and (basepath is None or self.project.isPathInProject(basepath)):
        project = self.project
      else:
        project = self.homeProject
      repo = project.createWorkingDir(repoURL, revision, basepath)
    return repo, repo.workingDir, repo.revision, revision and repo.revision != revision

  def findPathInRepos(self, path, importLoader=None):
    candidate = None
    if self.project:
      repo, filePath, revision, bare = self.project.findPathInRepos(path, importLoader)
      if repo:
        if not bare:
          return repo, filePath, revision, bare
        else:
          candidate = (repo, filePath, revision, bare)

    repo, filePath, revision, bare = self.homeProject.findPathInRepos(path, importLoader)
    if repo:
      if bare and candidate:
        return candidate
      else:
        return repo, filePath, revision, bare
    return None, None, None, None
