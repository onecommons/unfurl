"""
Classes for managing the local environment.

Repositories can optionally be organized into projects that have a local configuration.

There is always a home project that contains the local secret instances.
"""
import os
import os.path
# import six
import git

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
  def getCurrentInstanceRepo(self):
    return os.path.join(self.projectRoot, 'instances', 'current')

  def findDefaultInstanceManifest(self):
    fullPath = self.localConfig.getDefaultManifestPath()
    if fullPath:
      if not os.path.exists(fullPath):
        raise GitErOpError("The default manifest found in %s does not exist: %s" % (self.localConfig.config.path, os.path.abspath(fullPath)))
    else:
      fullPath = os.path.join(self.getCurrentInstanceRepo(), self.DefaultManifestName)
      if not os.path.exists(fullPath):
        raise GitErOpError("The default manifest does not exist: %s" % os.path.abspath(fullPath))
    return fullPath

  def __init__(self, path, localEnv):
    if os.path.isdir(path):
      self.projectRoot = path
      test = os.path.join(self.projectRoot, localEnv.DefaultLocalConfigName)
      # XXX merge or copy homeConfig
      if os.path.exists(test):
        self.localConfig = LocalConfig(test)
      else:
        self.localConfig = localEnv.homeConfig
    else:
      self.projectRoot = os.path.dirname(path)
      # XXX merge homeConfig
      self.localConfig = LocalConfig(path)

  def _createPathForGitRepo(self, gitUrl):
    basename = name = os.path.splitext(os.path.basename(gitUrl))[0]
    counter = 1
    while os.path.exists(os.path.join(self.projectRoot, name)):
      name = basename + str(counter)
      counter += 1
    return os.path.join(self.projectRoot, name)

  def createWorkingDir(self, gitUrl, revision='HEAD'):
    localRepoPath = self._createPathForGitRepo(gitUrl)
    empty_repo = git.Repo.init(localRepoPath)
    origin = empty_repo.create_remote('origin', gitUrl)
    empty_repo.create_head('master', origin.refs.master)
    empty_repo.set_tracking_branch(origin.refs.master).checkout(revision)
    return localRepoPath

  def findGitWorkingDirs(self):
    workingDirs = {}
    for root, dirs, files in os.walk(self.projectRoot):
      if '.git' in dirs:
        dirs.clear()  # don't visit CVS directories
        # XXX get git url
        repo = git.Repo(root)
        if repo.remotes:
          try:
            remote = repo.remotes['origin']
          except:
            remote = repo.remotes[0]
          workingDirs[root] = (remote.url, repo.head.ref.hexsha)
        else:
          workingDirs[root] = (root, repo.head.ref.hexsha)
    return workingDirs

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
  active = None

  def __init__(self, manifestPath=None, homepath=None):
    """
    If manifestPath is None find the first .giterop or manifest.yaml
    starting from the current directory.
    """
    # XXX need to save local config when changed
    self.homeConfigPath = self.getHomeConfigPath(homepath)
    if os.path.exists(self.homeConfigPath):
      self.homeConfig = LocalConfig(self.homeConfigPath)
    else:
      self.homeConfig = LocalConfig()

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

    self.config = self.project and self.project.localConfig or self.homeConfig

    # XXX map<initialcommits, [repos]>, map<urls, [repos]>
    LocalEnv.active = self

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

  #XXX def findGitRepo(self, repoURL, basepath=None):
    # can be anywhere, not just in current project

  def findOrCreateWorkingDir(self, repoURL, revision, basepath=None):
    workingDir = self.findGitRepo(repoURL, revision, basepath)
    if not workingDir:
      project = self.project or self.homeProject
      workingDir = project.createWorkingDir(repoURL, revision, basepath)
      # add to workingDirs
    return workingDir
