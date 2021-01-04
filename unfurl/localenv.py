# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Classes for managing the local environment.

Repositories can optionally be organized into projects that have a local configuration.

By convention, the "home" project defines a localhost instance and adds it to its context.
"""
import os
import os.path
import datetime

import six
from .repo import Repo, normalizeGitUrl, splitGitUrl, RepoView
from .util import UnfurlError
from .merge import mergeDicts
from .yamlloader import YamlConfig, makeVaultLib
from . import DefaultNames, getHomeConfigPath
from six.moves.urllib.parse import urlparse

_basepath = os.path.abspath(os.path.dirname(__file__))


class Project(object):
    """
    A Unfurl project is a folder that contains at least a local configuration file (unfurl.yaml),
    one or more ensemble.yaml files which maybe optionally organized into one or more git repositories.
    """

    def __init__(self, path, homeProject=None):
        assert isinstance(path, six.string_types), path
        parentConfig = homeProject and homeProject.localConfig or None
        self.projectRoot = os.path.abspath(os.path.dirname(path))
        if os.path.exists(path):
            self.localConfig = LocalConfig(path, parentConfig)
        else:
            self.localConfig = LocalConfig(parentConfig=parentConfig)
        self._setRepos()
        if self.projectRepo and homeProject:
            homeProject.localConfig.registerProject(self)

    def _setRepos(self):
        # abspath => RepoView:
        self.workingDirs = Repo.findGitWorkingDirs(self.projectRoot)
        # the project repo if it exists manages the project config (unfurl.yaml)
        projectRoot = self.projectRoot
        if projectRoot in self.workingDirs:
            self.projectRepo = self.workingDirs[projectRoot].repo
        else:
            # project maybe part of a containing repo (if created with --existing option)
            repo = Repo.findContainingRepo(self.projectRoot)
            # make sure projectroot isn't excluded from the containing repo
            if repo and not repo.isPathExcluded(self.projectRoot):
                self.projectRepo = repo
                self.workingDirs[repo.workingDir] = repo.asRepoView()
            else:
                self.projectRepo = None

        if self.projectRepo:
            # Repo.findGitWorkingDirs() doesn't look inside git working dirs
            # so look for repos in dirs that might be excluded from git
            for dir in self.projectRepo.findExcludedDirs(self.projectRoot):
                if projectRoot in dir and os.path.isdir(dir):
                    Repo.updateGitWorkingDirs(self.workingDirs, dir, os.listdir(dir))

        # add referenced local repositories outside of the project
        for path in self.localConfig.localRepositories:
            if os.path.isdir(path):
                # XXX assumes its a git repo, should compare and validate lock metadata
                Repo.updateGitWorkingDirs(self.workingDirs, path, os.listdir(path))

    @staticmethod
    def normalizePath(path):
        path = os.path.abspath(path)
        if not os.path.exists(path):
            isdir = not path.endswith(".yml") and not path.endswith(".yaml")
        else:
            isdir = os.path.isdir(path)

        if isdir:
            return os.path.join(path, DefaultNames.LocalConfig)
        else:
            return path

    @staticmethod
    def findPath(testPath):
        """
        Walk parents looking for unfurl.yaml
        """
        current = os.path.abspath(testPath)
        while current and current != os.sep:
            test = os.path.join(current, DefaultNames.LocalConfig)
            if os.path.exists(test):
                return test
            current = os.path.dirname(current)
        return None

    @property
    def venv(self):
        venv = os.path.join(self.projectRoot, ".venv")
        if os.path.isdir(venv):
            return venv
        return None

    def getAsdfPaths(self, asdfDataDir, toolVersions={}):
        paths = []
        toolVersionsFilename = (
            os.getenv("ASDF_DEFAULT_TOOL_VERSIONS_FILENAME") or ".tool-versions"
        )
        versionConf = os.path.join(self.projectRoot, toolVersionsFilename)
        if os.path.exists(versionConf):
            with open(versionConf) as conf:
                for line in conf.readlines():
                    line = line.strip()
                    if line and line[0] != "#":
                        tokens = line.split()
                        plugin = tokens.pop(0)
                        versions = toolVersions.setdefault(plugin, set())
                        for version in tokens:
                            if version != "system":
                                path = os.path.join(
                                    asdfDataDir, "installs", plugin, version, "bin"
                                )
                                versions.add(version)
                                if os.path.isdir(path):
                                    paths.append(path)
        return paths

    def getRepos(self):
        repos = [r.repo for r in self.workingDirs.values()]
        if self.projectRepo and self.projectRepo not in repos:
            repos.append(self.projectRepo)
        return repos

    def getDefaultEnsemblePath(self):
        fullPath = self.localConfig.getDefaultManifestPath()
        if fullPath:
            return fullPath
        return os.path.join(
            self.projectRoot, DefaultNames.EnsembleDirectory, DefaultNames.Ensemble
        )

    def findDefaultInstanceManifest(self):
        fullPath = self.localConfig.getDefaultManifestPath()
        if fullPath:
            if not os.path.exists(fullPath):
                raise UnfurlError(
                    "The default ensemble found in %s does not exist: %s"
                    % (self.localConfig.config.path, os.path.abspath(fullPath))
                )
            return fullPath
        else:
            # no manifest specified in the project config so check the default locations
            fullPath = os.path.join(
                self.projectRoot, DefaultNames.EnsembleDirectory, DefaultNames.Ensemble
            )
            if os.path.exists(fullPath):
                return fullPath
            fullPath2 = os.path.join(self.projectRoot, DefaultNames.Ensemble)
            if os.path.exists(fullPath2):
                return fullPath2
            raise UnfurlError(
                'The can not find an ensemble in a default location: "%s" or "%s"'
                % (fullPath, fullPath2)
            )

    def getRelativePath(self, path):
        return os.path.relpath(os.path.abspath(path), self.projectRoot)

    def isPathInProject(self, path):
        return not self.getRelativePath(path).startswith("..")

    def _createPathForGitRepo(self, gitUrl):
        name = Repo.getPathForGitRepo(gitUrl)
        return self.getUniquePath(name)

    def getUniquePath(self, name):
        basename = name
        counter = 1
        while os.path.exists(os.path.join(self.projectRoot, name)):
            name = basename + str(counter)
            counter += 1
        return os.path.join(self.projectRoot, name)

    def findGitRepo(self, repoURL, revision=None):
        candidate = None
        for dir, repository in self.workingDirs.items():
            repo = repository.repo
            if repoURL.startswith("git-local://"):
                initialCommit = urlparse(repoURL).netloc.partition(":")[0]
                match = initialCommit == repo.getInitialRevision()
            else:
                match = normalizeGitUrl(repoURL) == normalizeGitUrl(repo.url)
            if match:
                if revision:
                    if repo.revision == repo.resolveRevSpec(revision):
                        return repo
                    candidate = repo
                else:
                    return repo
        return candidate

    def findPathInRepos(self, path, importLoader=None):
        """If the given path is part of the working directory of a git repository
        return that repository and a path relative to it"""
        # importloader is unused until pinned revisions are supported
        candidate = None
        for dir in sorted(self.workingDirs.keys()):
            repo = self.workingDirs[dir].repo
            filePath = repo.findRepoPath(path)
            if filePath is not None:
                return repo, filePath, repo.revision, False
            #  XXX support bare repo and particular revisions
            #  need to make sure path isn't ignored in repo or compare candidates
            # filePath, revision, bare = repo.findPath(path, importLoader)
            # if filePath is not None:
            #     if not bare:
            #         return repo, filePath, revision, bare
            #     else:  # if it's bare see if we can find a better candidate
            #         candidate = (repo, filePath, revision, bare)
        return candidate or None, None, None, None

    def createWorkingDir(self, gitUrl, ref=None):
        localRepoPath = self._createPathForGitRepo(gitUrl)
        repo = Repo.createWorkingDir(gitUrl, localRepoPath, ref)
        # add to workingDirs
        self.workingDirs[os.path.abspath(localRepoPath)] = repo.asRepoView()
        return repo

    def findGitRepoFromRepository(self, repoSpec):
        repoUrl = repoSpec.url
        return self.findGitRepo(splitGitUrl(repoUrl)[0])

    def findOrClone(self, repo):
        gitUrl = repo.url
        existingRepo = self.findGitRepo(gitUrl)
        if existingRepo:
            return existingRepo

        # if not found:
        localRepoPath = os.path.abspath(
            self._createPathForGitRepo(repo.workingDir or gitUrl)
        )
        newRepo = repo.clone(localRepoPath)
        # use gitUrl to preserve original origin
        self.workingDirs[localRepoPath] = RepoView(dict(name="", url=gitUrl), newRepo)
        return newRepo


class LocalConfig(object):
    """
    Represents the local configuration file, which provides the environment that manifests run in, including:
      instances imported from other ensembles, inputs, environment variables, secrets and local configuration.

    It consists of:
    * a list of ensemble manifests with their local configuration
    * the default local and secret instances"""

    # don't merge the value of the keys of these dicts:
    replaceKeys = [
        "inputs",
        "attributes",
        "schemas",
        "connections",
        "manifest",
        "environment",
    ]

    def __init__(self, path=None, parentConfig=None, validate=True):
        defaultConfig = {"apiVersion": "unfurl/v1alpha1", "kind": "Project"}
        self.config = YamlConfig(
            defaultConfig, path, validate, os.path.join(_basepath, "unfurl-schema.json")
        )
        self.manifests = self.config.expanded.get("manifests") or []
        self.projects = self.config.expanded.get("projects") or {}
        contexts = self.config.expanded.get("contexts", {})
        if parentConfig:
            parentContexts = parentConfig.config.expanded.get("contexts", {})
            contexts = mergeDicts(
                parentContexts, contexts, replaceKeys=self.replaceKeys
            )
        self.contexts = contexts
        self.parentConfig = parentConfig
        self.localRepositories = self.config.expanded.get("localRepositories") or {}

    def getContext(self, manifestPath, context):
        localContext = self.contexts.get("defaults") or {}
        contextName = "defaults"
        for spec in self.manifests:
            if manifestPath == self.adjustPath(spec["file"]):
                # use the context associated with the given manifest
                contextName = spec.get("context", contextName)
                break

        if contextName != "defaults" and self.contexts.get(contextName):
            localContext = mergeDicts(
                localContext, self.contexts[contextName], replaceKeys=self.replaceKeys
            )

        return mergeDicts(context, localContext, replaceKeys=self.replaceKeys)

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

    def createLocalInstance(self, localName, attributes):
        # local or secret
        from .runtime import NodeInstance

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
        instance = NodeInstance(localName, attributes)
        instance._baseDir = self.config.getBaseDir()
        return instance

    def registerProject(self, project):
        # update, if necessary, localRepositories and projects
        key, local = self.config.searchIncludes(key="localRepositories")
        if not key and "localRepositories" not in self.config.config:
            # localRepositories doesn't exist, see if we are including a file inside "local"
            pathPrefix = os.path.join(self.config.getBaseDir(), "local")
            key, local = self.config.searchIncludes(pathPrefix=pathPrefix)
        if not key:
            local = self.config.config

        repo = project.projectRepo
        localRepositories = local.setdefault("localRepositories", {})
        lock = repo.asRepoView().lock()
        if localRepositories.get(repo.workingDir) != lock:
            localRepositories[repo.workingDir] = lock
        else:
            return False  # no change

        if not repo.isLocalOnly():
            for name, val in self.projects.items():
                if normalizeGitUrl(val["url"]) == normalizeGitUrl(repo.url):
                    break
            else:
                # if project isn't already in projects, use generated name
                name = "_" + os.path.basename(project.projectRoot)
                counter = 0
                while name in self.projects:
                    counter += 1
                    name += "-%s" % counter

            externalProject = dict(
                url=repo.url,
                initial=repo.getInitialRevision(),
            )
            file = os.path.relpath(project.projectRoot, repo.workingDir)
            if file and file != ".":
                externalProject["file"] = file

            self.projects[name] = externalProject
            self.config.config.setdefault("projects", {})[name] = externalProject

        if key:
            self.config.saveInclude(key)
        self.config.save()
        return True


class LocalEnv(object):
    """
    This class represents the local environment that an ensemble runs in, including
    the local project it is part of and the home project.
    """

    homeProject = None

    def __init__(self, manifestPath=None, homePath=None, parent=None, project=None):
        """
        If manifestPath is None find the first unfurl.yaml or ensemble.yaml
        starting from the current directory.

        If homepath is set it overrides UNFURL_HOME
        (and an empty string disable the home path).
        Otherwise the home path will be set to UNFURL_HOME or the default home location.
        """
        import logging

        logger = logging.getLogger("unfurl")
        self.logger = logger

        if parent:
            self._projects = parent._projects
            self._manifests = parent._manifests
            self.homeConfigPath = parent.homeConfigPath
        else:
            self._projects = {}
            if project:
                self._projects[project.localConfig.config.path] = project
            self._manifests = {}
            self.homeConfigPath = getHomeConfigPath(homePath)

        if self.homeConfigPath:
            if not os.path.exists(self.homeConfigPath):
                logger.warning(
                    'UNFURL_HOME is set but does not exist: "%s"', self.homeConfigPath
                )
            else:
                self.homeProject = self.getProject(self.homeConfigPath, None)
                if not self.homeProject:
                    logger.warning(
                        'Could not load home project at: "%s"', self.homeConfigPath
                    )

        self.manifestPath = None
        if manifestPath:
            # if manifestPath does not exist check project config
            manifestPath = os.path.abspath(manifestPath)
            if not os.path.exists(manifestPath):
                # XXX check if the manifest is named in the project config
                # pathORproject = self.findProject(os.path.dirname(manifestPath))
                # if pathORproject:
                #    self.manifestPath = pathORproject.getInstance(manifestPath)
                # else:
                raise UnfurlError(
                    "Ensemble manifest does not exist: '%s'" % manifestPath
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
            if project:
                self.project = project
            else:
                self.project = self.findProject(os.path.dirname(pathORproject))

        self.toolVersions = {}
        self.instanceRepo = self._getInstanceRepo()
        self.config = (
            self.project
            and self.project.localConfig
            or self.homeProject
            and self.homeProject.localConfig
            or LocalConfig()
        )

    def getVaultPassword(self, vaultId="default"):
        secret = os.getenv("UNFURL_VAULT_%s_PASSWORD" % vaultId.upper())
        if not secret:
            context = self.getContext()
            secret = (
                context.get("secrets", {})
                .get("attributes", {})
                .get("vault_%s_password" % vaultId)
            )
        return secret

    def getManifest(self, path=None):
        from .yamlmanifest import YamlManifest

        if path and path != self.manifestPath:
            # share projects and ensembles
            localEnv = LocalEnv(path, parent=self)
            return localEnv.getManifest()
        else:
            manifest = self._manifests.get(self.manifestPath)
            if not manifest:
                vaultId = "default"
                vault = makeVaultLib(self.getVaultPassword(vaultId), vaultId)
                if vault:
                    self.logger.info(
                        "Vault password found, configuring vault id: %s", vaultId
                    )
                manifest = YamlManifest(localEnv=self, vault=vault)
                self._manifests[self.manifestPath] = manifest
            return manifest

    def getProject(self, path, homeProject):
        path = Project.normalizePath(path)
        project = self._projects.get(path)
        if not project:
            project = Project(path, homeProject)
            self._projects[path] = project
        return project

    def getExternalManifest(self, location):
        assert "project" in location
        project = None
        if self.project:
            project = self.project.localConfig.projects.get(location["project"])
        if not project and self.homeProject:
            project = self.homeProject.localConfig.projects.get(location["project"])
        if not project:
            return None
        repo = self.findGitRepo(project["url"])
        if not repo:
            return None

        projectRoot = os.path.join(repo.workingDir, project.get("file") or "")
        localEnv = LocalEnv(
            os.path.join(projectRoot, location.get("file") or ""), parent=self
        )
        return localEnv.getManifest()

    # manifestPath specified
    #  doesn't exist: error
    #  is a directory: either instance repo or a project
    def findManifestPath(self, manifestPath):
        if not os.path.exists(manifestPath):
            raise UnfurlError(
                "Manifest file does not exist: '%s'" % os.path.abspath(manifestPath)
            )

        if os.path.isdir(manifestPath):
            test = os.path.join(manifestPath, DefaultNames.Ensemble)
            if os.path.exists(test):
                return test
            else:
                test = os.path.join(manifestPath, DefaultNames.LocalConfig)
                if os.path.exists(test):
                    return self.getProject(test, self.homeProject)
                else:
                    message = (
                        "Can't find an Unfurl ensemble or project in folder '%s'"
                        % manifestPath
                    )
                    raise UnfurlError(message)
        else:
            return manifestPath

    def _getInstanceRepo(self):
        instanceDir = os.path.dirname(self.manifestPath)
        if self.project and instanceDir in self.project.workingDirs:
            return self.project.workingDirs[instanceDir].repo
        else:
            return Repo.findContainingRepo(instanceDir)

    def getRepos(self):
        if self.project:
            repos = self.project.getRepos()
        else:
            repos = []
        if self.instanceRepo and self.instanceRepo not in repos:
            return repos + [self.instanceRepo]
        else:
            return repos

    def searchForManifestOrProject(self, dir):
        current = os.path.abspath(dir)
        while current and current != os.sep:
            test = os.path.join(current, DefaultNames.Ensemble)
            if os.path.exists(test):
                return test

            test = os.path.join(current, DefaultNames.LocalConfig)
            if os.path.exists(test):
                return self.getProject(test, self.homeProject)

            current = os.path.dirname(current)

        message = "Can't find an Unfurl ensemble or project in the current directory (or any of the parent directories)"
        raise UnfurlError(message)

    def findProject(self, testPath):
        """
        Walk parents looking for unfurl.yaml
        """
        path = Project.findPath(testPath)
        if path is not None:
            return self.getProject(path, self.homeProject)
        return None

    def getContext(self, context=None):
        """
        Return a new context that merges the given context with the local context.
        """
        return self.config.getContext(self.manifestPath, context or {})

    def getRuntime(self):
        context = self.getContext()
        runtime = context.get("runtime")
        if runtime:
            return runtime
        if self.project and self.project.venv:
            return "venv:" + self.project.venv
        if self.homeProject and self.homeProject.venv:
            return "venv:" + self.homeProject.venv
        return None

    def getLocalInstance(self, name, context):
        assert name in ["locals", "secrets", "local", "secret"]
        local = context.get(name, {})
        return (
            self.config.createLocalInstance(
                name.rstrip("s"), local.get("attributes", {})
            ),
            local,
        )

    def findGitRepo(self, repoURL, revision=None):
        repo = None
        if self.project:
            repo = self.project.findGitRepo(repoURL, revision)
        if not repo:
            if self.homeProject:
                return self.homeProject.findGitRepo(repoURL, revision)
        return repo

    def findOrCreateWorkingDir(self, repoURL, revision=None, basepath=None):
        repo = self.findGitRepo(repoURL, revision)
        # git-local repos must already exist
        if not repo and not repoURL.startswith("git-local://"):
            if self.project and (
                basepath is None or self.project.isPathInProject(basepath)
            ):
                project = self.project
            else:
                project = self.homeProject
            if project:
                repo = project.createWorkingDir(repoURL, revision)
        if not repo:
            return None, None, None

        if revision:
            # bare if HEAD isn't at the requested revision
            bare = repo.revision != repo.resolveRevSpec(revision)
            return repo, repo.revision, bare
        else:
            return repo, repo.revision, False

    def findPathInRepos(self, path, importLoader=None):
        """If the given path is part of the working directory of a git repository
        return that repository and a path relative to it"""
        # importloader is unused until pinned revisions are supported
        if self.instanceRepo:
            repo = self.instanceRepo
            filePath = repo.findRepoPath(path)
            if filePath is not None:
                return repo, filePath, repo.revision, False

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

    def mapValue(self, val):
        """
        Evaluate using project home as a base dir.
        """
        from .runtime import NodeInstance
        from .eval import mapValue

        instance = NodeInstance()
        instance._baseDir = self.config.config.getBaseDir()
        return mapValue(val, instance)

    def getPaths(self):
        paths = []
        asdfDataDir = os.getenv("ASDF_DATA_DIR")
        if not asdfDataDir:
            # check if an ensemble previously installed asdf via git
            repo = self.findGitRepo("https://github.com/asdf-vm/asdf.git/")
            if repo:
                asdfDataDir = repo.workingDir
            else:
                homeAsdf = os.path.expanduser("~/.asdf")
                if os.path.isdir(homeAsdf):
                    asdfDataDir = homeAsdf
        if asdfDataDir:
            # project has higher priority over home project
            if self.project:
                paths = self.project.getAsdfPaths(asdfDataDir, self.toolVersions)
            if self.homeProject:
                paths += self.homeProject.getAsdfPaths(asdfDataDir, self.toolVersions)
        return paths
