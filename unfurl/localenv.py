# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Classes for managing the local environment.

Repositories can optionally be organized into projects that have a local configuration.

By convention, the "home" project defines a localhost instance and adds it to its context.
"""
import os
import os.path

import six
from .repo import Repo, normalize_git_url, split_git_url, RepoView
from .util import UnfurlError
from .merge import merge_dicts
from .yamlloader import YamlConfig, make_vault_lib
from . import DefaultNames, get_home_config_path
from six.moves.urllib.parse import urlparse

_basepath = os.path.abspath(os.path.dirname(__file__))


class Project:
    """
    A Unfurl project is a folder that contains at least a local configuration file (unfurl.yaml),
    one or more ensemble.yaml files which maybe optionally organized into one or more git repositories.
    """

    def __init__(self, path, homeProject=None):
        assert isinstance(path, six.string_types), path
        self.projectRoot = os.path.abspath(os.path.dirname(path))
        if os.path.exists(path):
            self.localConfig = LocalConfig(path)
        else:
            self.localConfig = LocalConfig()
        self._set_repos()
        self.contextName = "defaults"
        self._set_parent_project(homeProject)

    def _set_parent_project(self, parentProject):
        self.parentProject = parentProject
        if self.projectRepo and parentProject:
            parentProject.localConfig.register_project(self)
        self._set_contexts()

    def _set_repos(self):
        # abspath => RepoView:
        self.workingDirs = Repo.find_git_working_dirs(self.projectRoot)
        # the project repo if it exists manages the project config (unfurl.yaml)
        projectRoot = self.projectRoot
        if projectRoot in self.workingDirs:
            self.projectRepo = self.workingDirs[projectRoot].repo
        else:
            # project maybe part of a containing repo (if created with --existing option)
            repo = Repo.find_containing_repo(self.projectRoot)
            # make sure projectroot isn't excluded from the containing repo
            if repo and not repo.is_path_excluded(self.projectRoot):
                self.projectRepo = repo
                self.workingDirs[repo.working_dir] = repo.as_repo_view()
            else:
                self.projectRepo = None

        if self.projectRepo:
            # Repo.findGitWorkingDirs() doesn't look inside git working dirs
            # so look for repos in dirs that might be excluded from git
            for dir in self.projectRepo.find_excluded_dirs(self.projectRoot):
                if projectRoot in dir and os.path.isdir(dir):
                    Repo.update_git_working_dirs(self.workingDirs, dir, os.listdir(dir))

        # add referenced local repositories outside of the project
        for path in self.localConfig.localRepositories:
            if os.path.isdir(path):
                # XXX assumes its a git repo, should compare and validate lock metadata
                Repo.update_git_working_dirs(self.workingDirs, path, os.listdir(path))

    @staticmethod
    def normalize_path(path):
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
    def find_path(testPath):
        """
        Walk parents looking for unfurl.yaml
        """
        current = os.path.abspath(testPath)
        while current and current != os.sep:
            test = os.path.join(current, DefaultNames.LocalConfig)
            if os.path.exists(test):
                return test
            test = os.path.join(
                current, DefaultNames.ProjectDirectory, DefaultNames.LocalConfig
            )
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

    def get_asdf_paths(self, asdfDataDir, toolVersions={}):
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

    def get_repos(self):
        repos = [r.repo for r in self.workingDirs.values()]
        if self.projectRepo and self.projectRepo not in repos:
            repos.append(self.projectRepo)
        return repos

    def search_for_manifest(self):
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

    def get_relative_path(self, path):
        return os.path.relpath(os.path.abspath(path), self.projectRoot)

    def is_path_in_project(self, path):
        return not self.get_relative_path(path).startswith("..")

    def _create_path_for_git_repo(self, gitUrl):
        name = Repo.get_path_for_git_repo(gitUrl)
        return self.get_unique_path(name)

    def get_unique_path(self, name):
        basename = name
        counter = 1
        while os.path.exists(os.path.join(self.projectRoot, name)):
            name = basename + str(counter)
            counter += 1
        return os.path.join(self.projectRoot, name)

    def find_git_repo(self, repoURL, revision=None):
        candidate = None
        for dir, repository in self.workingDirs.items():
            repo = repository.repo
            if repoURL.startswith("git-local://"):
                initialCommit = urlparse(repoURL).netloc.partition(":")[0]
                match = initialCommit == repo.get_initial_revision()
            else:
                match = normalize_git_url(repoURL) == normalize_git_url(repo.url)
            if match:
                if revision:
                    if repo.revision == repo.resolve_rev_spec(revision):
                        return repo
                    candidate = repo
                else:
                    return repo
        return candidate

    def find_path_in_repos(self, path, importLoader=None):
        """If the given path is part of the working directory of a git repository
        return that repository and a path relative to it"""
        # importloader is unused until pinned revisions are supported
        candidate = None
        for dir in sorted(self.workingDirs.keys()):
            repo = self.workingDirs[dir].repo
            filePath = repo.find_repo_path(path)
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

    def create_working_dir(self, gitUrl, ref=None):
        localRepoPath = self._create_path_for_git_repo(gitUrl)
        repo = Repo.create_working_dir(gitUrl, localRepoPath, ref)
        # add to workingDirs
        self.workingDirs[os.path.abspath(localRepoPath)] = repo.as_repo_view()
        return repo

    def find_git_repo_from_repository(self, repoSpec):
        repoUrl = repoSpec.url
        return self.find_git_repo(split_git_url(repoUrl)[0])

    def find_or_clone(self, repo):
        gitUrl = repo.url
        existingRepo = self.find_git_repo(gitUrl)
        if existingRepo:
            return existingRepo

        # if not found:
        localRepoPath = os.path.abspath(
            self._create_path_for_git_repo(repo.working_dir or gitUrl)
        )
        newRepo = repo.clone(localRepoPath)
        # use gitUrl to preserve original origin
        self.workingDirs[localRepoPath] = RepoView(dict(name="", url=gitUrl), newRepo)
        return newRepo

    def get_manifest_path(self, localEnv, manifestPath):
        if manifestPath:
            location = self.localConfig.find_ensemble_by_name(manifestPath)
            if not location:
                raise UnfurlError(
                    'Could not find ensemble "%s" in "%s"'
                    % (manifestPath, self.localConfig.config.path)
                )
        else:
            location = self.localConfig.get_default_manifest_tpl()

        if location:
            self.contextName = location.get("context", self.contextName)
            if "project" in location:
                externalLocalEnv = localEnv._get_external_localenv(location)
                if externalLocalEnv:
                    # if the manifest lives in an external projects repo,
                    # set that project as the parent project and register our project with it
                    if externalLocalEnv.project:
                        self._set_parent_project(externalLocalEnv.project)
                    return externalLocalEnv.manifestPath
                else:
                    raise UnfurlError(
                        'Could not find external project "%s" referenced by ensemble "%s"'
                        % (location["project"], manifestPath)
                    )
            else:
                fullPath = self.localConfig.adjust_path(location["file"])
                if not os.path.exists(fullPath):
                    raise UnfurlError(
                        "The default ensemble found in %s does not exist: %s"
                        % (self.localConfig.config.path, os.path.abspath(fullPath))
                    )
                return fullPath
        else:
            # no manifest specified in the project config so check the default locations
            fullPath = self.search_for_manifest()  # raises if not found
            return fullPath

    def _set_contexts(self):
        contexts = self.localConfig.config.expanded.get("contexts", {})
        if self.parentProject:
            parentContexts = self.parentProject.contexts
            contexts = merge_dicts(
                parentContexts, contexts, replaceKeys=LocalConfig.replaceKeys
            )
        self.contexts = contexts

    def get_context(self, context):
        contextName = self.contextName
        localContext = self.contexts.get("defaults") or {}
        if contextName != "defaults" and self.contexts.get(contextName):
            localContext = merge_dicts(
                localContext,
                self.contexts[contextName],
                replaceKeys=LocalConfig.replaceKeys,
            )

        return merge_dicts(context, localContext, replaceKeys=LocalConfig.replaceKeys)

    def find_ensemble_by_path(self, path):
        path = os.path.abspath(path)
        for tpl in self.localConfig.ensembles:
            file = tpl.get("file")
            if file and os.path.abspath(file) == path:
                return tpl

    def find_ensemble_by_name(self, name):
        for tpl in self.localConfig.ensembles:
            alias = tpl.get("alias")
            if alias == name:
                return tpl

    @property
    def name(self):
        dirname, name = os.path.split(self.projectRoot)
        if name == DefaultNames.ProjectDirectory:
            name = os.path.basename(dirname)
        return name

    def register_ensemble(self, manifestPath, *, project=None, managedBy=None):
        relPath = (project if project else self).get_relative_path(manifestPath)
        props = dict(file=relPath)
        if managedBy:
            props["managedBy"] = managedBy.name
        if project:
            props["project"] = project.name
        self.localConfig.ensembles.append(props)
        if managedBy or project:
            self.localConfig.register_project(managedBy or project)


class LocalConfig:
    """
    Represents the local configuration file, which provides the environment that ensembles run in, including:
      instances imported from other ensembles, inputs, environment variables, secrets and local configuration.
    """

    # don't merge the value of the keys of these dicts:
    replaceKeys = [
        "inputs",
        "attributes",
        "schemas",
        "connections",
        "manifest",
        "environment",
    ]

    def __init__(self, path=None, validate=True):
        defaultConfig = {"apiVersion": "unfurl/v1alpha1", "kind": "Project"}
        self.config = YamlConfig(
            defaultConfig, path, validate, os.path.join(_basepath, "unfurl-schema.json")
        )
        self.ensembles = self.config.expanded.get("ensembles") or []
        self.projects = self.config.expanded.get("projects") or {}
        self.localRepositories = self.config.expanded.get("localRepositories") or {}

    def adjust_path(self, path):
        """
        Makes sure relative paths are relative to the location of this local config
        """
        return os.path.join(self.config.get_base_dir(), path)

    def get_default_manifest_tpl(self):
        """
        ensembles:
          default:
            file:
            project:
            context
        """
        if len(self.ensembles) == 1:
            return self.ensembles[0]
        else:
            for tpl in self.ensembles:
                if tpl.get("default"):
                    return tpl
        return None

    def create_local_instance(self, localName, attributes):
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
        instance._baseDir = self.config.get_base_dir()
        return instance

    def find_repository_path(self, repourl):
        for path, tpl in self.localRepositories.items():
            if normalize_git_url(tpl["url"]) == normalize_git_url(repourl):
                return path

    def register_project(self, project):
        """
        Register an external project with current project.
        If the external project's repository is only local (without a remote origin repository) then save it in the local config if it exists.
        Otherwise, add it to the "projects" section of the current project's config.
        """
        # update, if necessary, localRepositories and projects
        key, local = self.config.search_includes(key="localRepositories")
        if not key and "localRepositories" not in self.config.config:
            # localRepositories doesn't exist, see if we are including a file inside "local"
            pathPrefix = os.path.join(self.config.get_base_dir(), "local")
            key, local = self.config.search_includes(pathPrefix=pathPrefix)
        if not key:
            local = self.config.config

        repo = project.projectRepo
        localRepositories = local.setdefault("localRepositories", {})
        lock = repo.as_repo_view().lock()
        if localRepositories.get(repo.working_dir) != lock:
            localRepositories[repo.working_dir] = lock
        else:
            return False  # no change

        for name, val in self.projects.items():
            if normalize_git_url(val["url"]) == normalize_git_url(repo.url):
                break
        else:
            # if project isn't already in projects, use generated name
            # XXX need to replace characters that don't match our namedObjects pattern manifest-schema.json
            name = project.name
            counter = 0
            while name in self.projects:
                counter += 1
                name += "-%s" % counter

        externalProject = dict(
            url=repo.url,
            initial=repo.get_initial_revision(),
        )
        file = os.path.relpath(project.projectRoot, repo.working_dir)
        if file and file != ".":
            externalProject["file"] = file

        self.projects[name] = externalProject
        if repo.is_local_only():
            projectConfig = local
        else:
            projectConfig = self.config.config
        projectConfig.setdefault("projects", {})[name] = externalProject

        if key:
            self.config.save_include(key)
        self.config.config["ensembles"] = self.ensembles
        self.config.save()
        return name


class LocalEnv:
    """
    This class represents the local environment that an ensemble runs in, including
    the local project it is part of and the home project.
    """

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
            if project:  # this arg is used in init.py when creating a project
                self._projects[project.localConfig.config.path] = project
            self._manifests = {}
            self.homeConfigPath = get_home_config_path(homePath)
        self.homeProject = self._get_home_project()

        if manifestPath:
            pathORproject = self._find_path_or_project(manifestPath)
            if pathORproject:
                manifestPath = ""  # consumed by _find_path_or_project
            else:
                # if manifestPath doesn't point to a project or ensemble,
                # look for a project in the current directory and then see if the project has a manifest with that name
                pathORproject = self.find_project(".")
                if not pathORproject:
                    raise UnfurlError(
                        "Ensemble manifest does not exist: '%s'" % manifestPath
                    )
        else:
            # not specified: search current directory and parents for either a manifest or a project
            # raises if not found
            pathORproject = self.search_for_manifest_or_project(".")

        if isinstance(pathORproject, Project):
            self.project = pathORproject
            # raises if not found:
            self.manifestPath = pathORproject.get_manifest_path(self, manifestPath)
        else:
            self.manifestPath = pathORproject
            if project:  # this arg is used in init.py when creating a project
                self.project = project
            else:
                self.project = self.find_project(os.path.dirname(pathORproject))
                if self.project:
                    # We're pointing directly at a manifest path, check if it is might be part of an external project
                    location = self.project.find_ensemble_by_path(manifestPath)
                    if (
                        location and "managedBy" in location
                    ):  # this ensemble is managed by another project
                        project_tpl = project[location["managedBy"]]
                        path = self.project.localConfig.find_repository_path(
                            project_tpl["url"]
                        )
                        externalProject = self.find_project(path)
                        externalProject._set_parent_project(self.project)
                        self.project = externalProject

        if self.project:
            logging.info("Loaded project at %s", self.project.localConfig.config.path)
        self.toolVersions = {}
        self.instanceRepo = self._get_instance_repo()
        self.config = (
            self.project
            and self.project.localConfig
            or self.homeProject
            and self.homeProject.localConfig
            or LocalConfig()
        )

    def _get_home_project(self):
        homeProject = None
        if self.homeConfigPath:
            if not os.path.exists(self.homeConfigPath):
                self.logger.warning(
                    'UNFURL_HOME is set but does not exist: "%s"', self.homeConfigPath
                )
            else:
                homeProject = self.get_project(self.homeConfigPath, None)
                if not homeProject:
                    self.logger.warning(
                        'Could not load home project at: "%s"', self.homeConfigPath
                    )
        return homeProject

    def _find_path_or_project(self, manifestPath):
        manifestPath = os.path.abspath(manifestPath)
        if os.path.exists(manifestPath):
            pathORproject = self.find_manifest_path(manifestPath)
            assert pathORproject
        else:
            # if manifestPath does not exist check project config
            pathORproject = self.find_project(".")
        return pathORproject

    def get_vault_password(self, vaultId="default"):
        secret = os.getenv(f"UNFURL_VAULT_{vaultId.upper()}_PASSWORD")
        if not secret:
            context = self.get_context()
            secret = (
                context.get("secrets", {})
                .get("attributes", {})
                .get(f"vault_{vaultId}_password")
            )
        return secret

    def get_manifest(self, path=None):
        from .yamlmanifest import YamlManifest

        if path and path != self.manifestPath:
            # share projects and ensembles
            localEnv = LocalEnv(path, parent=self)
            return localEnv.get_manifest()
        else:
            manifest = self._manifests.get(self.manifestPath)
            if not manifest:
                vaultId = "default"
                vault = make_vault_lib(self.get_vault_password(vaultId), vaultId)
                if vault:
                    self.logger.info(
                        "Vault password found, configuring vault id: %s", vaultId
                    )
                manifest = YamlManifest(localEnv=self, vault=vault)
                self._manifests[self.manifestPath] = manifest
            return manifest

    def get_project(self, path, homeProject):
        path = Project.normalize_path(path)
        project = self._projects.get(path)
        if not project:
            project = Project(path, homeProject)
            self._projects[path] = project
        return project

    def get_external_manifest(self, location):
        localEnv = self._get_external_localenv(location)
        return localEnv.get_manifest()

    def _get_external_localenv(self, location):
        assert "project" in location
        project = None
        repo = None
        projectName = location["project"]
        if self.project:
            project = self.project.localConfig.projects.get(projectName)
        if not project and self.homeProject:
            project = self.homeProject.localConfig.projects.get(projectName)
            # allow "home" to refer to the home project
            if not project and projectName == "home":
                repo = self.homeProject.repo
                file = ""
        if project:
            repo = self.find_git_repo(project["url"])
            file = project.get("file") or ""

        if not repo:
            return None

        projectRoot = os.path.join(repo.working_dir, file)
        return LocalEnv(
            os.path.join(projectRoot, location.get("file") or ""), parent=self
        )

    # manifestPath specified
    #  doesn't exist: error
    #  is a directory: either instance repo or a project
    def find_manifest_path(self, manifestPath):
        if not os.path.exists(manifestPath):
            raise UnfurlError(
                f"Manifest file does not exist: '{os.path.abspath(manifestPath)}'"
            )

        if os.path.isdir(manifestPath):
            test = os.path.join(manifestPath, DefaultNames.Ensemble)
            if os.path.exists(test):
                return test
            else:
                test = os.path.join(manifestPath, DefaultNames.LocalConfig)
                if os.path.exists(test):
                    return self.get_project(test, self.homeProject)
                else:
                    test = os.path.join(manifestPath, DefaultNames.ProjectDirectory)
                    if os.path.exists(test):
                        return self.get_project(test, self.homeProject)
                    else:
                        message = f"Can't find an Unfurl ensemble or project in folder '{manifestPath}'"
                        raise UnfurlError(message)
        else:
            return manifestPath

    def _get_instance_repo(self):
        instanceDir = os.path.dirname(self.manifestPath)
        if self.project and instanceDir in self.project.workingDirs:
            return self.project.workingDirs[instanceDir].repo
        else:
            return Repo.find_containing_repo(instanceDir)

    def get_repos(self):
        if self.project:
            repos = self.project.get_repos()
        else:
            repos = []
        if self.instanceRepo and self.instanceRepo not in repos:
            return repos + [self.instanceRepo]
        else:
            return repos

    def search_for_manifest_or_project(self, dir):
        current = os.path.abspath(dir)
        while current and current != os.sep:
            test = os.path.join(current, DefaultNames.LocalConfig)
            if os.path.exists(test):
                return self.get_project(test, self.homeProject)

            test = os.path.join(current, DefaultNames.Ensemble)
            if os.path.exists(test):
                return test

            test = os.path.join(current, DefaultNames.ProjectDirectory)
            if os.path.exists(test):
                return self.get_project(test, self.homeProject)

            current = os.path.dirname(current)

        message = "Can't find an Unfurl ensemble or project in the current directory (or any of the parent directories)"
        raise UnfurlError(message)

    def find_project(self, testPath):
        """
        Walk parents looking for unfurl.yaml
        """
        path = Project.find_path(testPath)
        if path is not None:
            return self.get_project(path, self.homeProject)
        return None

    def get_context(self, context=None):
        """
        Return a new context that merges the given context with the local context.
        """
        context = context or {}
        project = self.project or self.homeProject
        if project:
            return project.get_context(context)
        return context

    def get_runtime(self):
        context = self.get_context()
        runtime = context.get("runtime")
        if runtime:
            return runtime
        project = self.project or self.homeProject
        while project:
            if project.venv:
                return "venv:" + project.venv
            project = project.parentProject
        return None

    def get_local_instance(self, name, context):
        assert name in ["locals", "secrets", "local", "secret"]
        local = context.get(name, {})
        return (
            self.config.create_local_instance(
                name.rstrip("s"), local.get("attributes", {})
            ),
            local,
        )

    def find_git_repo(self, repoURL, revision=None):
        project = self.project or self.homeProject
        while project:
            repo = project.find_git_repo(repoURL, revision)
            if repo:
                return repo
            project = project.parentProject
        return None

    def find_or_create_working_dir(self, repoURL, revision=None, basepath=None):
        repo = self.find_git_repo(repoURL, revision)
        # git-local repos must already exist
        if not repo and not repoURL.startswith("git-local://"):
            project = self.project or self.homeProject
            while project:
                if basepath is None or self.project.is_path_in_project(basepath):
                    repo = project.create_working_dir(repoURL, revision)
                    break
                project = project.parentProject
        if not repo:
            return None, None, None

        if revision:
            # bare if HEAD isn't at the requested revision
            bare = repo.revision != repo.resolve_rev_spec(revision)
            return repo, repo.revision, bare
        else:
            return repo, repo.revision, False

    def find_path_in_repos(self, path, importLoader=None):
        """If the given path is part of the working directory of a git repository
        return that repository and a path relative to it"""
        # importloader is unused until pinned revisions are supported
        if self.instanceRepo:
            repo = self.instanceRepo
            filePath = repo.find_repo_path(path)
            if filePath is not None:
                return repo, filePath, repo.revision, False

        candidate = None
        repo = None
        project = self.project or self.homeProject
        while project:
            repo, filePath, revision, bare = project.find_path_in_repos(
                path, importLoader
            )
            if repo:
                if not bare:
                    return repo, filePath, revision, bare
                else:
                    candidate = (repo, filePath, revision, bare)
                    break
            project = project.parentProject
        if repo:
            if bare and candidate:
                return candidate
            else:
                return repo, filePath, revision, bare
        return None, None, None, None

    def map_value(self, val):
        """
        Evaluate using project home as a base dir.
        """
        from .runtime import NodeInstance
        from .eval import map_value

        instance = NodeInstance()
        instance._baseDir = self.config.config.get_base_dir()
        return map_value(val, instance)

    def get_paths(self):
        """
        If asdf is installed, build a PATH list from .toolversions
        found in the current project and the home project.
        """
        paths = []
        # check if asdf is installed
        asdfDataDir = os.getenv("ASDF_DATA_DIR")
        if not asdfDataDir:
            # check if an ensemble previously installed asdf via git
            repo = self.find_git_repo("https://github.com/asdf-vm/asdf.git/")
            if repo:
                asdfDataDir = repo.working_dir
            else:
                homeAsdf = os.path.expanduser("~/.asdf")
                if os.path.isdir(homeAsdf):
                    asdfDataDir = homeAsdf
        if asdfDataDir:  # asdf is installed
            # current project has higher priority over home project
            project = self.project or self.homeProject
            while project:
                paths += project.get_asdf_paths(asdfDataDir, self.toolVersions)
                project = project.parentProject
        return paths
