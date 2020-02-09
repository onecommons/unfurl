"""
Classes for managing the local environment.

Repositories can optionally be organized into projects that have a local configuration.

There is always a "home" project that contains the localhost instance.
"""
import os
import os.path

# import six
from .repo import Repo
from .util import UnfurlError
from .yamlloader import YamlConfig
from . import __version__

DefaultManifestName = "manifest.yaml"
DefaultLocalConfigName = "unfurl.yaml"
DefaultHomeDirectory = ".unfurl_home"
HiddenMarkerName = ".unfurl"


class Project(object):
    """
  A Unfurl project is a folder that contains at least a local configuration file (unfurl.yaml),
  one or more manifest.yaml files which maybe optionally organized into one or more git repositories.
  """

    def __init__(self, path, localEnv):
        if not os.path.exists(path):
            isdir = not path.endswith(".yml") and not path.endswith(".yaml")
        else:
            isdir = os.path.isdir(path)

        if isdir:
            self.projectRoot = path
            test = os.path.join(self.projectRoot, DefaultLocalConfigName)
            if os.path.exists(test):
                self.localConfig = LocalConfig(
                    test, localEnv.homeProject and localEnv.homeProject.localConfig
                )
            elif localEnv.homeProject:
                self.localConfig = localEnv.homeProject.localConfig
            else:
                self.localConfig = LocalConfig()
        else:
            self.projectRoot = os.path.dirname(path)
            self.localConfig = LocalConfig(
                path, localEnv.homeProject and localEnv.homeProject.localConfig
            )

        self.workingDirs = Repo.findGitWorkingDirs(self.projectRoot)
        # the project repo if it exists manages the project config (unfurl.yaml)
        if self.projectRoot in self.workingDirs:
            self.projectRepo = self.workingDirs[self.projectRoot][1]
        else:
            self.projectRepo = Repo.findContainingRepo(self.projectRoot)

    def getRepos(self):
        return [repo for (gitUrl, repo) in self.workingDirs.values()]

    def getCurrentInstanceRepo(self):
        return os.path.join(self.projectRoot, "instances", "current")

    def findDefaultInstanceManifest(self):
        fullPath = self.localConfig.getDefaultManifestPath()
        if fullPath:
            if not os.path.exists(fullPath):
                raise UnfurlError(
                    "The default manifest found in %s does not exist: %s"
                    % (self.localConfig.config.path, os.path.abspath(fullPath))
                )
        else:
            fullPath = os.path.join(self.getCurrentInstanceRepo(), DefaultManifestName)
            if not os.path.exists(fullPath):
                raise UnfurlError(
                    "The default manifest does not exist: %s"
                    % os.path.abspath(fullPath)
                )
        return fullPath

    def isPathInProject(self, path):
        return (
            os.path.abspath(self.projectRoot) + os.sep in os.path.abspath(path) + os.sep
        )

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

    def createWorkingDir(self, gitUrl, revision="HEAD"):
        localRepoPath = self._createPathForGitRepo(gitUrl)
        repo = Repo.createWorkingDir(gitUrl, localRepoPath, revision)
        # add to workingDirs
        self.workingDirs[localRepoPath] = (gitUrl, repo)
        return repo


_basepath = os.path.abspath(os.path.dirname(__file__))


class LocalConfig(object):
    """
  Represents the local configuration file, which provides the environment that manifests run in, including:
    instances imported from other ensembles, inputs, environment variables, secrets and local configuration.

  It consists of:
  * a list of instance manifests with their local configuration
  * the default local and secret instances
"""

    # XXX add list of projects to config
    # projects:
    #   - path:
    #     default: True
    #     instance: instances/current
    #     spec: spec

    def __init__(self, path=None, parentConfig=None, validate=True):
        defaultConfig = {"unfurl": dict(version=__version__)}
        self.config = YamlConfig(
            defaultConfig, path, validate, os.path.join(_basepath, "unfurl-schema.json")
        )
        self.manifests = self.config.config.get(
            "manifests", self.config.config.get("instances", [])  # backward compat
        )
        self.defaults = self.config.config.get("defaults", {})
        self.parentConfig = parentConfig

    def adjustPath(self, path):
        """
    Makes sure relative paths are relative to the location of this local config
    """
        return os.path.join(self.config.getBaseDir(), path)

    def getDefaultManifestPath(self):
        if len(self.manifests) == 1:
            return self.adjustPath(self.manifests[0]["file"])
        else:
            for spec in self.manifests:
                if spec.get("default"):
                    return self.adjustPath(spec["file"])
        return None

    def getLocalResource(self, manifestPath, localName, importSpec):
        """
        manifestPath (str): The currently loading manifest
        localName (str): Local name for the imported resource; either 'localhost', 'local' or 'secret'
        importSpec (dict): Metadata about the imported manifest; will be updated if necessary.
        """
        from .runtime import NodeInstance

        localRepo = None
        for spec in self.manifests:
            if manifestPath == self.adjustPath(spec["file"]):
                localRepo = spec.get(localName)
        if not localRepo:
            localRepo = self.defaults.get(localName)
        if not localRepo and self.parentConfig:
            return self.parentConfig.getLocalResource(
                manifestPath, localName, importSpec
            )

        if localRepo:
            attributes = localRepo.get("attributes")
            if attributes is not None:
                if "default" in attributes:
                    if not "default" in attributes.get(".interfaces", {}):
                        attributes.setdefault(".interfaces", {})[
                            "default"
                        ] = "unfurl.support.DelegateAttributes"
                if "inheritFrom" in attributes:
                    if not "inherit" in attributes.get(".interfaces", {}):
                        attributes.setdefault(".interfaces", {})[
                            "inherit"
                        ] = "unfurl.support.DelegateAttributes"
                    if attributes["inheritFrom"] == "home" and self.parentConfig:
                        parent = self.parentConfig.getLocalResource(
                            manifestPath, localName, importSpec
                        )
                        localResource = NodeInstance(localName, attributes)
                        if parent:
                            localResource._attributes["inheritFrom"] = parent
                            return localResource
                        else:
                            importSpec["inheritHack"] = localResource
                            return None
                repoResource = NodeInstance(localName, attributes)
                repoResource.baseDir = self.config.getBaseDir()
                return repoResource
            else:
                # the local or secret is a resource defined in a local manifest
                # set the url and resource name so the importing manifest loads it
                importSpec.update(localRepo)
                if "file" in localRepo:
                    importSpec["file"] = self.adjustPath(localRepo["file"])
                if "repository" in localRepo:
                    importSpec["repository"] = self.adjustPath(localRepo["repository"])
                return None

        # none found, return empty resource
        return NodeInstance(localName)


class LocalEnv(object):
    """
  The class represents the local environment that a instance manifest runs in.

  The instance manifest and/or the current project
  The local configuration
  """

    homeProject = None

    def __init__(self, manifestPath=None, homePath=None):
        """
    If manifestPath is None find the first .unfurl or manifest.yaml
    starting from the current directory.

    If homepath is set it overrides UNFURL_HOME
    (and an empty string disable the home path).
    Otherwise the home path will be set to UNFURL_HOME or the default home location.
    """
        self.homeConfigPath = self.getHomeConfigPath(homePath)
        if self.homeConfigPath:
            self.homeProject = Project(self.homeConfigPath, self)
        self.manifestPath = None
        if manifestPath:
            # if manifestPath does not exist check project config
            if not os.path.exists(manifestPath):
                # XXX check if the manifest is named in the project config
                # pathORproject = self.findProject(os.path.dirname(manifestPath))
                # if pathORproject:
                #    self.manifestPath = pathORproject.getInstance(manifestPath)
                # else:
                raise UnfurlError(
                    "Manifest file does not exist: '%s'" % os.path.abspath(manifestPath)
                )
            else:
                pathORproject = self.findManifestPath(manifestPath)
        else:
            # not specified: search current directory and parents for either a manifest or a project
            pathORproject = self.searchForManifestOrProject(".")

        if isinstance(pathORproject, Project):
            self.project = pathORproject
            if not self.manifestPath:
                self.manifestPath = pathORproject.findDefaultInstanceManifest()
        else:
            self.manifestPath = pathORproject
            self.project = self.findProject(os.path.dirname(pathORproject))

        self.instanceRepo = self._getInstanceRepo()
        self.config = (
            self.project
            and self.project.localConfig
            or self.homeProject
            and self.homeProject.localConfig
            or LocalConfig()
        )

    # manifestPath specified
    #  doesn't exist: error
    #  is a directory: either instance repo or a project
    def findManifestPath(self, manifestPath):
        if not os.path.exists(manifestPath):
            raise UnfurlError(
                "Manifest file does not exist: '%s'" % os.path.abspath(manifestPath)
            )

        if os.path.isdir(manifestPath):
            test = os.path.join(manifestPath, DefaultManifestName)
            if os.path.exists(test):
                return test
            else:
                test = os.path.join(manifestPath, DefaultLocalConfigName)
                if os.path.exists(test):
                    return Project(test, self)
                else:
                    message = (
                        "Can't find a unfurl manifest or project in folder '%s'"
                        % manifestPath
                    )
                    raise UnfurlError(message)
        else:
            return manifestPath

    def _getInstanceRepo(self):
        instanceDir = os.path.dirname(self.manifestPath)
        if self.project and instanceDir in self.project.workingDirs:
            return self.project.workingDirs[instanceDir][1]
        else:
            return Repo.findContainingRepo(instanceDir)

    def getRepos(self):
        if self.project:
            return self.project.getRepos()
        else:
            return [self.instanceRepo]

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

        message = "Can't find an Unfurl manifest or repository in current directory (or any of the parent directories)"
        raise UnfurlError(message)

    def findProject(self, testPath):
        """
    Walk parents looking for unfurl.yaml
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
        # if homepath is explicitly it overrides UNFURL_HOME
        # (set it to empty string to disable the homepath)
        # otherwise use UNFURL_HOME or the default location
        if homepath is None:
            if "UNFURL_HOME" in os.environ:
                homepath = os.getenv("UNFURL_HOME")
            else:
                homepath = os.path.join("~", DefaultHomeDirectory)
        if homepath:
            homepath = os.path.expanduser(homepath)
            if not os.path.exists(homepath):
                isdir = not homepath.endswith(".yml") and not homepath.endswith(".yaml")
            else:
                isdir = os.path.isdir(homepath)
            if isdir:
                return os.path.abspath(os.path.join(homepath, DefaultLocalConfigName))
            else:
                return os.path.abspath(homepath)
        return None

    def getLocalResource(self, name, importSpec):
        if name not in ["local", "localhost", "secret"]:
            return None
        return self.config.getLocalResource(self.manifestPath, name, importSpec)

    def findGitRepo(self, repoURL, isFile=True, revision=None):
        repo = None
        if self.project:
            repo = self.project.findGitRepo(repoURL, revision)
        if not repo:
            if self.homeProject:
                return self.homeProject.findGitRepo(repoURL, revision)
        return repo

    def findOrCreateWorkingDir(
        self, repoURL, isFile=True, revision=None, basepath=None
    ):
        repo = self.findGitRepo(repoURL, revision)
        if not repo:
            if self.project and (
                basepath is None or self.project.isPathInProject(basepath)
            ):
                project = self.project
            else:
                project = self.homeProject
            if project:
                repo = project.createWorkingDir(repoURL, revision, basepath)
        if not repo:
            return None, None, None, None
        return (
            repo,
            repo.workingDir,
            repo.revision,
            revision and repo.revision != revision,
        )

    def findPathInRepos(self, path, importLoader=None):
        candidate = None
        repo = None
        if self.project:
            repo, filePath, revision, bare = self.project.findPathInRepos(
                path, importLoader
            )
            if repo:
                if not bare:
                    return repo, filePath, revision, bare
                else:
                    candidate = (repo, filePath, revision, bare)

        if self.homeProject:
            repo, filePath, revision, bare = self.homeProject.findPathInRepos(
                path, importLoader
            )
        if repo:
            if bare and candidate:
                return candidate
            else:
                return repo, filePath, revision, bare
        return None, None, None, None
