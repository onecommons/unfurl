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
from .yamlloader import YamlConfig, make_vault_lib_ex, make_yaml
from . import DefaultNames, get_home_config_path
from six.moves.urllib.parse import urlparse
from toscaparser.repositories import Repository

_basepath = os.path.abspath(os.path.dirname(__file__))


import logging

logger = logging.getLogger("unfurl")


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
        # XXX this updates and saves the local config to disk -- constructing a Project object shouldn't do that
        # especially since we localenv might not have found the ensemble yet
        self._set_parent_project(homeProject)

    def _set_parent_project(self, parentProject):
        assert not parentProject or parentProject.projectRoot != self.projectRoot, (
            parentProject.projectRoot,
            self.projectRoot,
        )
        self.parentProject = parentProject
        if parentProject:
            parentProject.localConfig.register_project(self)
        self._set_contexts()
        # depends on _set_contexts():
        self.project_repoview.yaml = make_yaml(self.make_vault_lib())

    def _set_project_repoview(self):
        path = self.projectRoot
        if path in self.workingDirs:
            self.project_repoview = self.workingDirs[path]
            return

        # project maybe part of a containing repo (if created with --existing option)
        repo = Repo.find_containing_repo(path)
        # make sure projectroot isn't excluded from the containing repo
        if not repo or repo.is_path_excluded(path):
            repo = None
            url = "file:" + path
        else:
            url = repo.get_url_with_path(path)
            path = os.path.relpath(path, repo.working_dir)

        self.project_repoview = RepoView(
            Repository("project", dict(url=url)),
            repo,
            path,
        )
        if repo:
            self.workingDirs[repo.working_dir] = self.project_repoview

    def _set_repos(self):
        # abspath => RepoView:
        self.workingDirs = Repo.find_git_working_dirs(self.projectRoot)
        self._set_project_repoview()

        if self.project_repoview.repo:
            # Repo.findGitWorkingDirs() doesn't look inside git working dirs
            # so look for repos in dirs that might be excluded from git
            for dir in self.project_repoview.repo.find_excluded_dirs(self.projectRoot):
                if self.projectRoot in dir and os.path.isdir(dir):
                    Repo.update_git_working_dirs(self.workingDirs, dir, os.listdir(dir))

        # add referenced local repositories outside of the project
        for path, tpl in self.localConfig.localRepositories.items():
            if os.path.isdir(path):
                repo = Repo.find_containing_repo(path)
                if repo:  # make sure it's a git repo
                    # XXX validate that repo matches url and metadata in tpl
                    self.workingDirs[path] = RepoView(
                        dict(url=repo.url, name=tpl.get("name", "")),
                        repo,
                        split_git_url(tpl["url"])[1],
                    )

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

    def _get_git_repos(self):
        repos = [r.repo for r in self.workingDirs.values()]
        if self.project_repoview.repo and self.project_repoview.repo not in repos:
            repos.append(self.project_repoview.repo)
        return repos

    def search_for_manifest(self, can_be_empty):
        fullPath = os.path.join(
            self.projectRoot, DefaultNames.EnsembleDirectory, DefaultNames.Ensemble
        )
        if os.path.exists(fullPath):
            return fullPath
        fullPath2 = os.path.join(self.projectRoot, DefaultNames.Ensemble)
        if os.path.exists(fullPath2):
            return fullPath2
        if not can_be_empty:
            raise UnfurlError(
                'Can not find an ensemble in a default location: "%s" or "%s"'
                % (fullPath, fullPath2)
            )
        return None

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

    def get_manifest_path(self, localEnv, manifestPath, can_be_empty):
        if manifestPath:
            # at this point a named manifest
            location = self.find_ensemble_by_name(manifestPath)
            if not location:
                raise UnfurlError(f'Could not find ensemble "{manifestPath}"')
        else:
            location = self.localConfig.get_default_manifest_tpl()

        if location:
            contextName = location.get("environment", self.get_default_context())
            if "project" in location and location["project"] != self.name:
                externalLocalEnv = localEnv._get_external_localenv(location, self)
                if externalLocalEnv:
                    # if the manifest lives in an external projects repo,
                    # _get_external_localenv() above will have set that project
                    # as the parent project and register our project with it
                    return self, externalLocalEnv.manifestPath, contextName
                else:
                    raise UnfurlError(
                        'Could not find external project "%s" referenced by ensemble "%s"'
                        % (location["project"], manifestPath or location["file"])
                    )
            else:
                fullPath = self.localConfig.adjust_path(location["file"])
                if not os.path.exists(fullPath):
                    raise UnfurlError(
                        "The default ensemble found in %s does not exist: %s"
                        % (self.localConfig.config.path, os.path.abspath(fullPath))
                    )
                if "managedBy" in location:
                    project = self.get_managed_project(location, localEnv)
                else:
                    project = self
                return project, fullPath, contextName
        else:
            # no manifest specified in the project config so check the default locations
            assert not manifestPath
            fullPath = self.search_for_manifest(can_be_empty)  # raises if not found
            return self, fullPath, self.get_default_context()

    def _set_contexts(self):
        # merge project contexts with parent contexts
        contexts = self.localConfig.config.expanded.get("environments") or {}
        if self.parentProject:
            parentContexts = self.parentProject.contexts
            contexts = merge_dicts(
                parentContexts, contexts, replaceKeys=LocalConfig.replaceKeys
            )
        self.contexts = contexts

    def get_context(self, contextName, context=None):
        # merge the named context with defaults and the given context
        localContext = self.contexts.get("defaults") or {}
        if contextName and contextName != "defaults" and self.contexts.get(contextName):
            localContext = merge_dicts(
                localContext,
                self.contexts[contextName],
                replaceKeys=LocalConfig.replaceKeys,
            )

        return merge_dicts(
            context or {}, localContext, replaceKeys=LocalConfig.replaceKeys
        )

    def get_vault_password(self, contextName=None, vaultId="default"):
        secret = os.getenv(f"UNFURL_VAULT_{vaultId.upper()}_PASSWORD")
        if secret:
            return secret
        context = self.get_context(contextName)
        secrets = context.get("secrets", {}).get(f"vault_secrets")
        if secrets:
            return secrets.get(vaultId)

    def get_vault_passwords(self, contextName=None):
        # we want to support multiple vault ids
        context = self.get_context(contextName)
        secrets = context.get("secrets", {}).get(f"vault_secrets")
        if not secrets:
            return
        for vaultId, password in secrets.items():
            password = os.getenv(f"UNFURL_VAULT_{vaultId.upper()}_PASSWORD", password)
            yield vaultId, password

    def make_vault_lib(self, contextName=None):
        secrets = list(self.get_vault_passwords(contextName))
        if secrets:
            return make_vault_lib_ex(secrets)
        return None

    def find_ensemble_by_path(self, path):
        path = os.path.abspath(path)
        for tpl in self.localConfig.ensembles:
            file = tpl.get("file")
            if file and os.path.normpath(os.path.join(self.projectRoot, file)) == path:
                return tpl

    def find_ensemble_by_name(self, name):
        for tpl in self.localConfig.ensembles:
            alias = tpl.get("alias")
            if alias and alias == name:
                return tpl

    @property
    def name(self):
        return self.get_name_from_dir(self.projectRoot)

    @staticmethod
    def get_name_from_dir(projectRoot):
        dirname, name = os.path.split(projectRoot)
        if name == DefaultNames.ProjectDirectory:
            name = os.path.basename(dirname)
        return name

    def get_default_context(self):
        return self.localConfig.config.expanded.get("default_environment")

    def get_default_project_path(self, context_name):
        # place new ensembles in the shared project for this context
        default_project = self.get_context(context_name).get("defaultProject")
        if not default_project:
            return None

        # search for the default_project
        project = self
        while project:
            project_info = project.localConfig.projects.get(default_project)
            if project_info:
                return project.localConfig.find_repository_path(project_info)
            project = project.parentProject

        raise UnfurlError(
            'Could not find path to default project "%s" in context "%s"'
            % (default_project, context_name)
        )

    def get_managed_project(self, location, localEnv):
        project_tpl = self.localConfig.projects[location["managedBy"]]
        path = self.localConfig.find_repository_path(project_tpl)
        externalProject = None
        if path is not None:
            externalProject = localEnv.find_project(path)
            if externalProject:
                externalProject._set_parent_project(self)
                return externalProject
        if not externalProject:
            localEnv.logger.warning(
                'Could not find the project "%s" which manages "%s"',
                location["managedBy"],
                path,
            )
            return self

    def register_ensemble(
        self, manifestPath, *, project=None, managedBy=None, context=None
    ):
        relPath = (project if project else self).get_relative_path(manifestPath)
        props = dict(file=relPath)
        props["alias"] = os.path.basename(os.path.dirname(manifestPath))
        if managedBy:
            props["managedBy"] = managedBy.name
        if project:
            props["project"] = project.name
        if context:
            props["environment"] = context
        location = self.find_ensemble_by_path(manifestPath)
        if location:
            location.update(props)
        else:
            self.localConfig.ensembles.append(props)
        if managedBy or project:
            assert not (managedBy and project)
            self.localConfig.register_project(managedBy or project, changed=True)
        elif context:
            self.localConfig.config.config["ensembles"] = self.localConfig.ensembles
            self.localConfig.config.save()


class LocalConfig:
    """
    Represents the local configuration file, which provides the environment that ensembles run in, including:
      instances imported from other ensembles, inputs, environment variables, secrets and local configuration.
    """

    # don't merge the value of the keys of these dicts:
    replaceKeys = [
        "inputs",
        "secrets",
        "locals",
        "external",
        "connections",
        "variables",
        "repositories",
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

    def find_repository_path(self, project_info: dict):
        # prioritize matching by url
        path = self.find_repository_path_by_url(project_info["url"])
        if path:
            return path
        if project_info.get("initial"):
            return self.find_repository_path_by_revision(project_info["initial"])
        return None

    def find_repository_path_by_url(self, repourl):
        url = normalize_git_url(repourl)
        for path, tpl in self.localRepositories.items():
            if normalize_git_url(tpl["url"]) == url:
                return path
        return None

    def find_repository_path_by_revision(self, initial_revision):
        for path, tpl in self.localRepositories.items():
            if tpl.get("initial") == initial_revision:
                return path
        return None

    def _get_project_name(self, project):
        repo = project.project_repoview
        for name, val in self.projects.items():
            if normalize_git_url(val["url"]) == normalize_git_url(repo.url):
                return name

        # if project isn't already in projects, use generated name
        # XXX need to replace characters that don't match our namedObjects pattern manifest-schema.json
        name = project.name
        counter = 0
        while name in self.projects:
            counter += 1
            name += "-%s" % counter

        return name

    def register_project(self, project, for_context=None, changed=False):
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

        repo = project.project_repoview
        localRepositories = local.setdefault("localRepositories", {})
        lock = repo.lock()
        name = self._get_project_name(project)
        lock["project"] = name
        if localRepositories.get(repo.working_dir) != lock:
            localRepositories[repo.working_dir] = lock
            changed = True

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
        project_tpl = projectConfig.setdefault("projects", {})
        if project_tpl.get(name) != externalProject:
            project_tpl[name] = externalProject
            changed = True

        if for_context:
            # set the project to be the default project for the given context
            context = projectConfig.setdefault("environments", {}).setdefault(
                for_context, {}
            )
            if context.get("defaultProject") != name:
                context["defaultProject"] = name
                changed = True

        if changed:
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

    project = None
    parent = None

    def _find_external_project(self, manifestPath, project):
        # We're pointing directly at a manifest path, check if it is might be part of an external project
        context_name = None
        location = project.find_ensemble_by_path(manifestPath)
        if location:
            context_name = location.get("environment")
            if "managedBy" in location:
                # this ensemble is managed by another project
                return project.get_managed_project(location, self), context_name
        return project, context_name

    def _resolve_path_and_project(self, manifestPath, can_be_empty):
        if manifestPath:
            # raises if manifestPath is a directory without either a manifest or project
            foundManifestPath, project = self._find_given_manifest_or_project(
                os.path.abspath(manifestPath)
            )
        else:
            # manifestPath not specified so search current directory and parents for either a manifest or a project
            # raises if not found
            foundManifestPath, project = self._search_for_manifest_or_project(".")

        if foundManifestPath:
            self.manifestPath = foundManifestPath
            if not project:
                project = self.find_project(os.path.dirname(foundManifestPath))
        elif project:
            # the manifestPath was pointing to a project, not a manifest
            manifestPath = ""
        else:
            assert manifestPath
            # if manifestPath doesn't point to a project or ensemble,
            # look for a project in the current directory and then see if the project has a manifest with that name
            project = self.find_project(".")
            if not project:
                raise UnfurlError(
                    "Ensemble manifest does not exist: '%s'" % manifestPath
                )

        if project:
            if foundManifestPath:
                # We're pointing directly at a manifest path,
                # look up project info to get its context
                # if it is managedBy by another project, switch to that project
                (
                    self.project,
                    self.manifest_context_name,
                ) = self._find_external_project(foundManifestPath, project)
            else:
                # not found, see if the manifest if it is located in a shared project or is referenced by name
                # raises if not found
                (
                    self.project,
                    self.manifestPath,
                    self.manifest_context_name,
                ) = project.get_manifest_path(self, manifestPath, can_be_empty)
        else:
            self.project = None

    def __init__(
        self,
        manifestPath=None,
        homePath=None,
        parent=None,
        project=None,
        can_be_empty=False,
    ):
        """
        If manifestPath is None find the first unfurl.yaml or ensemble.yaml
        starting from the current directory.

        If homepath is set it overrides $UNFURL_HOME
        (and an empty string disable the home path).
        Otherwise the home path will be set to $UNFURL_HOME or the default home location.
        """
        import logging

        logger = logging.getLogger("unfurl")
        self.logger = logger
        self.manifest_context_name = None

        if parent:
            self.parent = parent
            self._projects = parent._projects
            self._manifests = parent._manifests
            self.homeConfigPath = parent.homeConfigPath
        else:
            self._projects = {}
            self._manifests = {}
            self.homeConfigPath = get_home_config_path(homePath)
        self.homeProject = self._get_home_project()

        self._resolve_path_and_project(manifestPath, can_be_empty)
        if project:
            # this arg is used in init.py when creating a project
            # overrides what was set by _resolve_path_and_project()
            self._projects[project.localConfig.config.path] = project
            self.project = project

        if self.project:
            logger.info("Loaded project at %s", self.project.localConfig.config.path)
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
                    'UNFURL_HOME environment variable is set but does not exist: "%s"',
                    self.homeConfigPath,
                )
            else:
                homeProject = self.get_project(self.homeConfigPath, None)
                if not homeProject:
                    self.logger.warning(
                        'Could not load home project at: "%s"', self.homeConfigPath
                    )
                else:
                    self.logger.info('Using home project at: "%s"', self.homeConfigPath)
        return homeProject

    def get_vault_password(self, vaultId="default"):
        # used by __main__.vaultclient
        project = self.project or self.homeProject
        if project:
            return project.get_vault_password(self.manifest_context_name, vaultId)
        return None

    def get_manifest(self, path=None):
        from .yamlmanifest import YamlManifest

        if path and path != self.manifestPath:
            # share projects and ensembles
            localEnv = LocalEnv(path, parent=self)
            return localEnv.get_manifest()
        else:
            assert self.manifestPath
            manifest = self._manifests.get(self.manifestPath)
            if not manifest:
                # should load vault ids from context
                project = self.project or self.homeProject
                if project:
                    vault = project.make_vault_lib(self.manifest_context_name)
                    if vault:
                        self.logger.info(
                            "Vault password found, configuring vault ids: %s",
                            [s[0] for s in vault.secrets],
                        )
                else:
                    vault = None
                if not vault:
                    msg = "No vault password found"
                    if self.manifest_context_name:
                        msg += f" for environment {self.manifest_context_name}"
                    self.logger.debug(msg)
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
        if localEnv:
            return localEnv.get_manifest()
        else:
            return None

    def _get_external_localenv(self, location, currentProject=None):
        # currentProject because this method might be called before self.project was set
        assert "project" in location
        project = None
        repo = None
        projectName = location["project"]
        if not currentProject:
            currentProject = self.project
        if currentProject:
            project = currentProject.localConfig.projects.get(projectName)

        if not project and self.homeProject:
            project = self.homeProject.localConfig.projects.get(projectName)
            # allow "home" to refer to the home project
            if not project and projectName == "home":
                repo = self.homeProject.project_repoview
                file = ""

        if project:
            url, file, revision = split_git_url(project["url"])
            repo = self.find_git_repo(url)
            if project.get("file"):
                file = os.path.join(file, project.get("file"))

        if not repo:
            return None

        projectRoot = os.path.join(repo.working_dir, file)
        return LocalEnv(
            os.path.join(projectRoot, location.get("file") or ""), parent=self
        )

    def _find_given_manifest_or_project(self, manifestPath):
        if not os.path.exists(manifestPath):
            return None, None
        if os.path.isdir(manifestPath):
            test = os.path.join(manifestPath, DefaultNames.Ensemble)
            if os.path.exists(test):
                return test, None
            else:
                test = os.path.join(manifestPath, DefaultNames.LocalConfig)
                if os.path.exists(test):
                    return "", self.get_project(test, self.homeProject)
                else:
                    test = os.path.join(manifestPath, DefaultNames.ProjectDirectory)
                    if os.path.exists(test):
                        return "", self.get_project(test, self.homeProject)
                    else:
                        message = f"Can't find an Unfurl ensemble or project in folder '{manifestPath}'"
                        raise UnfurlError(message)
        else:
            test = os.path.join(manifestPath, DefaultNames.LocalConfig)
            if os.path.exists(test):
                return None, self.get_project(test, self.homeProject)
            # assume its a pointing to an ensemble
            return manifestPath, None

    def _get_instance_repo(self):
        if not self.manifestPath:
            return None
        instanceDir = os.path.dirname(self.manifestPath)
        if self.project:
            repo = self.project.find_path_in_repos(instanceDir)[0]
            if repo:
                return repo
        return Repo.find_containing_repo(instanceDir)

    # NOTE this currently isn't used and returns repos outside of this LocalEnv
    # (every repo found in localRepositories)
    def _get_git_repos(self):
        if self.project:
            repos = self.project._get_git_repos()
        else:
            repos = []
        if self.instanceRepo and self.instanceRepo not in repos:
            return repos + [self.instanceRepo]
        else:
            return repos

    def _search_for_manifest_or_project(self, dir):
        current = os.path.abspath(dir)
        while current and current != os.sep:
            test = os.path.join(current, DefaultNames.LocalConfig)
            if os.path.exists(test):
                return "", self.get_project(test, self.homeProject)

            test = os.path.join(current, DefaultNames.Ensemble)
            if os.path.exists(test):
                return test, None

            test = os.path.join(current, DefaultNames.ProjectDirectory)
            if os.path.exists(test):
                return "", self.get_project(test, self.homeProject)

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
            return project.get_context(self.manifest_context_name, context)
        return context

    def get_runtime(self):
        # XXX replace this with top-level section for runtime
        project = self.project or self.homeProject
        while project:
            if project.venv:
                return "venv:" + project.venv
            project = project.parentProject
        return None

    def get_local_instance(self, name, context):
        # returns NodeInstance, spec
        assert name in ["locals", "secrets", "local", "secret"]
        attributes = dict(context.get(name) or {})
        # schema is reserved property name
        spec = dict(schema=attributes.pop("schema", None))
        return (
            self.config.create_local_instance(name.rstrip("s"), attributes),
            spec,
        )

    def find_git_repo(self, repoURL, revision=None):
        project = self.project or self.homeProject
        count = 0
        while project:
            count += 1
            assert count < 4, (
                project.projectRoot,
                project.parentProject and project.parentProject.projectRoot,
            )
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
            assert project
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
            count = 0
            while project:
                paths += project.get_asdf_paths(asdfDataDir, self.toolVersions)
                count += 1
                assert count < 4
                project = project.parentProject
        return paths
