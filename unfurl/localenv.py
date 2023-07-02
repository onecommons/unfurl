# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
Classes for managing the local environment.

Repositories can optionally be organized into projects that have a local configuration.

By convention, the "home" project defines a localhost instance and adds it to its context.
"""
import os
import os.path
import re
from typing import (
    Any,
    Callable,
    Iterable,
    Dict,
    List,
    Optional,
    OrderedDict,
    Tuple,
    Union,
    TYPE_CHECKING,
    cast,
)
from ansible.parsing.vault import VaultLib
from unfurl.runtime import NodeInstance

from .repo import (
    GitRepo,
    Repo,
    add_user_to_url,
    split_git_url,
    RepoView,
    normalize_git_url_hard,
)
from .util import (
    UnfurlError,
    filter_env,
    substitute_env,
    wrap_sensitive_value,
    save_to_tempfile,
    is_sensitive,
)
from .merge import merge_dicts
from .yamlloader import UnfurlVaultLib, YamlConfig, make_vault_lib_ex, make_yaml
from . import DefaultNames, get_home_config_path
from urllib.parse import urlparse, urlunsplit, urlsplit
from ruamel.yaml.comments import CommentedMap
from toscaparser.repositories import Repository

if TYPE_CHECKING:
    from unfurl.yamlmanifest import YamlManifest


_basepath = os.path.abspath(os.path.dirname(__file__))


from .logs import getLogger

logger = getLogger("unfurl")


class Project:
    """
    A Unfurl project is a folder that contains at least a local configuration file (unfurl.yaml),
    one or more ensemble.yaml files which maybe optionally organized into one or more git repositories.
    """

    def __init__(
        self,
        path: str,
        homeProject: Optional["Project"] = None,
        overrides: Optional[dict] = None,
        readonly: Optional[bool] = False,
    ):
        assert isinstance(path, str), path
        self.projectRoot = os.path.abspath(os.path.dirname(path))
        self.overrides = overrides or {}
        if os.path.exists(path):
            self.localConfig = LocalConfig(
                path, yaml_include_hook=self.load_yaml_include, readonly=readonly
            )
        else:
            self.localConfig = LocalConfig(readonly=readonly)
        self._set_repos()
        # XXX this updates and saves the local config to disk -- constructing a Project object shouldn't do that
        # especially since we localenv might not have found the ensemble yet
        self._set_parent_project(homeProject)

    def _set_parent_project(self, parentProject: Optional["Project"]) -> None:
        assert not parentProject or parentProject.projectRoot != self.projectRoot, (
            parentProject.projectRoot,
            self.projectRoot,
        )
        self.parentProject = parentProject
        if parentProject:
            parentProject.register_project(self)
        self._set_contexts()
        # depends on _set_contexts():
        self.project_repoview.yaml = make_yaml(self.make_vault_lib())  # type: ignore

    def _set_project_repoview(self) -> None:
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

    def _set_repos(self) -> None:
        # abspath => RepoView:
        self.workingDirs = Repo.find_git_working_dirs(self.projectRoot, True)
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
    def normalize_path(path: str) -> str:
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
    def find_path(testPath: str, stopPath: Optional[str] = None) -> Optional[str]:
        """
        Walk parents looking for unfurl.yaml
        """
        current = os.path.abspath(testPath)
        if not stopPath:
            stopPath = os.sep
        else:
            stopPath = os.path.abspath(stopPath)
        while current and len(current) >= len(stopPath):
            test = os.path.join(current, DefaultNames.LocalConfig)
            if os.path.exists(test):
                return test
            test = os.path.join(
                current, DefaultNames.ProjectDirectory, DefaultNames.LocalConfig
            )
            if os.path.exists(test):
                return test
            if current == os.sep:
                break
            current = os.path.dirname(current)
        return None

    @property
    def venv(self) -> Optional[str]:
        venv = os.path.join(self.projectRoot, ".venv")
        if os.path.isdir(venv):
            return venv
        return None

    @staticmethod
    def get_asdf_paths(projectRoot, asdfDataDir, toolVersions={}) -> List[str]:
        paths = []
        toolVersionsFilename = (
            os.getenv("ASDF_DEFAULT_TOOL_VERSIONS_FILENAME") or ".tool-versions"
        )
        versionConf = os.path.join(projectRoot, toolVersionsFilename)
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

    def _get_git_repos(self) -> List[GitRepo]:
        repos = [r.repo for r in self.workingDirs.values() if r.repo]
        if self.project_repoview.repo and self.project_repoview.repo not in repos:
            repos.append(self.project_repoview.repo)
        return repos

    def search_for_manifest(self, can_be_empty: bool) -> Optional[str]:
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

    def get_relative_path(self, path: str) -> str:
        # TODO: consider asserting that `path` is an absolute path so this method is not ambiguous
        # assert os.path.isabs(path)
        # return os.path.relpath(path, self.projectRoot)

        # NOTE: `os.path.abspath` constructs the absolute path from the current working directory
        # This may not be the desired behavior (unfurl-server being a counterexample)
        return os.path.relpath(os.path.abspath(path), self.projectRoot)

    def is_path_in_project(self, path: str) -> bool:
        return not self.get_relative_path(path).startswith("..")

    def _create_path_for_git_repo(self, gitUrl: str) -> str:
        name = Repo.get_path_for_git_repo(gitUrl)
        return self.get_unique_path(name)

    def get_unique_path(self, name: str) -> str:
        basename = name
        counter = 1
        while os.path.exists(os.path.join(self.projectRoot, name)):
            name = basename + str(counter)
            counter += 1
        return os.path.join(self.projectRoot, name)

    def _find_git_repo(
        self, repoURL: str, revision: Optional[str] = None
    ) -> Union[RepoView, str]:
        # if repo isn't found, return the repo url, possibly rewritten to include server credentials
        apply_credentials = self.overrides.get("apply_url_credentials")
        candidate = None
        repourl_parts = urlsplit(repoURL)
        normalized = normalize_git_url_hard(repoURL)
        for dir, repository in self.workingDirs.items():
            repo = repository.repo
            assert repo
            if repoURL.startswith("git-local://"):
                initialCommit = urlparse(repoURL).netloc.partition(":")[0]
                match = initialCommit == repo.get_initial_revision()
            else:
                match = normalized == normalize_git_url_hard(repo.url)
            if match:
                if revision:
                    if repo.revision == repo.resolve_rev_spec(revision):
                        return repository
                    # revisions don't match but we'll return it if we don't find a better candidate
                    candidate = repository
                else:
                    return repository
            elif apply_credentials:
                # if an existing repository has the same hostname as the new repository's url
                # and has credentials, update the new url with those credentials
                candidate_parts = urlsplit(repo.url)
                password = candidate_parts.password
                if password:
                    if (
                        candidate_parts.hostname == repourl_parts.hostname
                        and candidate_parts.port == repourl_parts.port
                    ):
                        # rewrite repoUrl to add credentials
                        repoURL = add_user_to_url(
                            repoURL, candidate_parts.username, password
                        )
        return candidate or repoURL

    def find_git_repo(
        self, repoURL: str, revision: Optional[str] = None
    ) -> Optional[GitRepo]:
        repo_or_url = self._find_git_repo(repoURL, revision)
        if isinstance(repo_or_url, str):
            return None
        return repo_or_url.repo

    def find_path_in_repos(
        self, path: str, importLoader: Optional[Any] = None
    ) -> Tuple[Optional[GitRepo], Optional[str], Optional[str], Optional[bool]]:
        """If the given path is part of the working directory of a git repository
        return that repository and a path relative to it"""
        # importloader is unused until pinned revisions are supported
        candidate = None
        for dir in sorted(self.workingDirs.keys()):
            repo = self.workingDirs[dir].repo
            assert repo
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

    def create_working_dir(self, gitUrl: str, ref: Optional[str] = None) -> GitRepo:
        localRepoPath = self._create_path_for_git_repo(gitUrl)
        repo = Repo.create_working_dir(gitUrl, localRepoPath, ref)
        # add to workingDirs
        self.workingDirs[os.path.abspath(localRepoPath)] = repo.as_repo_view()
        return repo

    def find_git_repo_from_repository(self, repoSpec: Repository) -> Optional[Repo]:
        repoUrl = repoSpec.url
        return self.find_git_repo(split_git_url(repoUrl)[0])

    def find_or_clone(self, repo: GitRepo) -> GitRepo:
        for dir, repository in self.workingDirs.items():
            if (
                repository.repo
                and repo.get_initial_revision()
                == repository.repo.get_initial_revision()
            ):
                return repository.repo

        gitUrl = repo.url
        localRepoPath = os.path.abspath(
            self._create_path_for_git_repo(repo.working_dir or gitUrl)
        )
        logger.trace('Cloning "%s" to: %s', repo.working_dir or gitUrl, localRepoPath)
        newRepo = repo.clone(localRepoPath)
        # use gitUrl to preserve original origin
        self.workingDirs[localRepoPath] = RepoView(dict(name="", url=gitUrl), newRepo)
        return newRepo

    def find_or_create_working_dir(
        self, repoURL: str, revision: Optional[str] = None
    ) -> Optional[GitRepo]:
        repo = self.find_git_repo(repoURL, revision)
        if not repo:
            repo = self.create_working_dir(repoURL, revision)
        return repo

    def get_manifest_path(
        self, localEnv: "LocalEnv", manifestPath: Optional[str], can_be_empty: bool
    ) -> Tuple[Optional["Project"], str, str]:
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

    def _set_contexts(self) -> None:
        # merge project contexts with parent contexts
        contexts = self.localConfig.config.expanded.get("environments") or {}
        if self.parentProject:
            parentContexts = self.parentProject.contexts  # type: ignore
            contexts = merge_dicts(
                parentContexts, contexts, replaceKeys=LocalConfig.replaceKeys
            )
        self.contexts = contexts

    def get_context(
        self, contextName: Optional[str], context: Optional[dict] = None
    ) -> dict:
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

    @staticmethod
    def _check_vault_env_var(context, vaultId, default=None):
        env_var = f"UNFURL_VAULT_{vaultId.upper()}_PASSWORD"
        secret = os.getenv(env_var, default)
        if secret:
            return secret
        return context.get("variables", {}).get(env_var, default)

    def get_vault_password(
        self, contextName: Optional[str] = None, vaultId: str = "default"
    ) -> Optional[str]:
        context = self.get_context(contextName)  # type: ignore
        secret = self._check_vault_env_var(context, vaultId)
        if secret:
            return secret
        secrets = context.get("secrets", {}).get("vault_secrets")
        if secrets:
            return secrets.get(vaultId)
        return None

    def get_vault_passwords(
        self, contextName: Optional[str] = None
    ) -> Iterable[Tuple[str, Union[str, bytes]]]:
        # we want to support multiple vault ids
        context = self.get_context(contextName)  # type: ignore
        secrets = context.get("secrets", {}).get("vault_secrets")
        if not secrets:
            return
        for vaultId, password in secrets.items():
            password = self._check_vault_env_var(context, vaultId, password)
            if password:
                yield vaultId, password

    def make_vault_lib(self, contextName: Optional[str] = None) -> Optional[VaultLib]:
        if self.overrides.get("UNFURL_SKIP_VAULT_DECRYPT"):
            vault = UnfurlVaultLib(secrets=None)
            vault.skip_decode = True
            return vault
        secrets = list(self.get_vault_passwords(contextName))
        if secrets:
            return make_vault_lib_ex(secrets)
        return None

    @staticmethod
    def _find_ensemble_by_path(ensembles, projectRoot, path: str) -> Optional[dict]:
        path = os.path.abspath(path)
        match = None
        for tpl in ensembles:
            file = tpl.get("file")
            if file and os.path.normpath(os.path.join(projectRoot, file)) == path:
                if not match:
                    match = tpl
                else:  # merge duplicate ensembles
                    match.update(tpl)
        return match

    def find_ensemble_by_path(self, path: str) -> Optional[dict]:
        return self._find_ensemble_by_path(
            self.localConfig.ensembles, self.projectRoot, path
        )

    @staticmethod
    def _find_ensemble_by_name(ensembles, name: str) -> Optional[dict]:
        for tpl in ensembles:
            alias = tpl.get("alias")
            if alias and alias == name:
                return tpl
        return None

    def find_ensemble_by_name(self, name: str) -> Optional[dict]:
        return self._find_ensemble_by_name(self.localConfig.ensembles, name)

    @property
    def name(self) -> str:
        return self.get_name_from_dir(self.projectRoot)

    @staticmethod
    def get_name_from_dir(projectRoot: str) -> str:
        dirname, name = os.path.split(projectRoot)
        if name == DefaultNames.ProjectDirectory:
            name = os.path.basename(dirname)
        # make sure this conforms to project name syntax in json-schema:
        #       ^[A-Za-z._][A-Za-z0-9._:\\-]*$
        name = re.sub(r"[^A-Za-z0-9._:\\-]", "_", name)
        if not name[0].isalpha() and name[0] not in "._":
            name = "_" + name
        return name

    def get_default_context(self) -> Any:
        return self.localConfig.config.expanded.get("default_environment")

    def get_default_project_path(self, context_name: str) -> Optional[Any]:
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
            project = project.parentProject  # type: ignore

        raise UnfurlError(
            'Could not find path to default project "%s" in context "%s"'
            % (default_project, context_name)
        )

    def get_managed_project(
        self, location: dict, localEnv: "LocalEnv"
    ) -> Optional["Project"]:
        project_tpl = self.localConfig.projects[location["managedBy"]]
        path = self.localConfig.find_repository_path(project_tpl)
        externalProject = None
        if path is not None:
            externalProject = localEnv.find_project(path)
            if externalProject:
                externalProject._set_parent_project(self)
                return externalProject
            localEnv.logger.warning(
                'Could not find the project "%s" which manages "%s"',
                location["managedBy"],
                path,
            )
        return self

    def register_ensemble(
        self,
        manifestPath: str,
        *,
        project: "Project" = None,
        managedBy: Optional["Project"] = None,
        context: Optional[str] = None,
    ) -> None:
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
            self.register_project(managedBy or project, changed=True)
        else:
            self.localConfig.config.config["ensembles"] = self.localConfig.ensembles
            if not self.localConfig.config.readonly:
                self.localConfig.config.save()

    def register_project(
        self, project, for_context=None, changed=False, save_project=True
    ):
        self.localConfig.register_project(project, for_context, changed, save_project)
        self.workingDirs[
            project.project_repoview.working_dir
        ] = project.project_repoview

    def load_yaml_include(
        self,
        yamlConfig: YamlConfig,
        templatePath: Union[str, dict],
        baseDir,
        warnWhenNotFound=False,
        expanded=None,
        action=None,
    ):
        """
        This is called while the YAML config is being loaded.
        Returns (url or fullpath, parsed yaml)
        """
        if isinstance(templatePath, dict):
            key = templatePath["file"]
            merge = templatePath.get("merge")
        else:
            key = templatePath
            merge = None

        url_vars = {}
        url_vars.update(self.overrides)
        if action:
            if action.get_key:
                return substitute_env(action.get_key, url_vars)
            if action.check_include:
                return True
            return None  # unsupported action

        if merge == "maplist" and "ENVIRONMENT" not in self.overrides:
            # don't load remote includes for LocalEnv that don't specify an environment
            # XXX (hackish way to avoid loads for transitory LocalEnv objects)
            logger.trace("skipping retrieving url with ENVIRONMENT: %s", key)
            return key, None

        if not key:
            return key, None
        assert isinstance(key, str)
        key = substitute_env(key, url_vars)
        includekey, template = yamlConfig.load_yaml(key, baseDir, warnWhenNotFound)
        if merge == "maplist" and template is not None:
            environment = url_vars.get("ENVIRONMENT")
            if not environment:
                environment = expanded.get("default_environment")
                ensembles = expanded.get("ensembles") or []
                if "manifest_path" in url_vars:
                    ensemble_tpl = self._find_ensemble_by_path(
                        ensembles, self.projectRoot, url_vars["manifest_path"]
                    )
                elif "manifest_alias" in url_vars:
                    ensemble_tpl = self._find_ensemble_by_name(
                        ensembles, url_vars["manifest_alias"]
                    )
                else:
                    ensemble_tpl = LocalConfig._get_default_manifest_tpl(ensembles)
                if ensemble_tpl:
                    environment = ensemble_tpl.get("environment", environment)
            template = CommentedMap(_maplist(template, environment))
            logger.debug("retrieved remote environment vars: %s", template)
        return includekey, template


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
        "instances",
    ]

    def __init__(
        self, path=None, validate=True, yaml_include_hook=None, readonly=False
    ):
        defaultConfig = {"apiVersion": "unfurl/v1alpha1", "kind": "Project"}
        self.config = YamlConfig(
            defaultConfig,
            path,
            validate,
            os.path.join(_basepath, "unfurl-schema.json"),
            yaml_include_hook,
            readonly=readonly,
        )
        self.ensembles = self.config.expanded.get("ensembles") or []
        self.projects = self.config.expanded.get("projects") or {}
        self.localRepositories = self.config.expanded.get("localRepositories") or {}

    def adjust_path(self, path):
        """
        Makes sure relative paths are relative to the location of this local config
        """
        return os.path.join(self.config.get_base_dir(), path)

    @staticmethod
    def _get_default_manifest_tpl(ensembles):
        if len(ensembles) == 1:
            return ensembles[0]
        else:
            for tpl in ensembles:
                if tpl.get("default"):
                    return tpl
        return None

    def get_default_manifest_tpl(self):
        return self._get_default_manifest_tpl(self.ensembles)

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
        url = normalize_git_url_hard(repourl)
        for path, tpl in self.localRepositories.items():
            if normalize_git_url_hard(tpl["url"]) == url:
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
            if normalize_git_url_hard(val["url"]) == normalize_git_url_hard(repo.url):
                return name

        # if project isn't already in projects, use generated name
        # XXX need to replace characters that don't match our namedObjects pattern manifest-schema.json
        name = project.name
        counter = 0
        while name in self.projects:
            counter += 1
            name += "-%s" % counter

        return name

    def register_project(
        self, project, for_context=None, changed=False, save_project=True
    ):
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

        if save_project:
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
            if key and not self.config.readonly:
                self.config.save_include(key)
            self.config.config["ensembles"] = self.ensembles
            if not self.config.readonly:
                self.config.save()
        return lock


def _maplist(template, environment_scope=None):
    for var in template:
        scope = var.get("environment_scope", "*")
        if scope != "*" and scope != environment_scope:
            # match or if environment_scope is None skip any != *
            continue
        value = var["value"]
        if var.get("variable_type") == "file":
            value = save_to_tempfile(value, var["key"]).name
        elif var.get("masked"):
            value = wrap_sensitive_value(value)
        yield var["key"], value


class LocalEnv:
    """
    This class represents the local environment that an ensemble runs in, including
    the local project it is part of and the home project.
    """

    project: Optional[Project] = None
    parent: Optional["LocalEnv"] = None

    def _find_external_project(
        self, manifestPath: str, project: Project
    ) -> Tuple[Optional[Project], Optional[str]]:
        # We're pointing directly at a manifest path, check if it is might be part of an external project
        context_name = None
        location = project.find_ensemble_by_path(manifestPath)
        if location:
            context_name = location.get("environment")
            if "managedBy" in location:
                # this ensemble is managed by another project
                return project.get_managed_project(location, self), context_name
        return project, context_name

    def _resolve_path_and_project(self, manifestPath: str, can_be_empty: bool) -> None:
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
                # set this because the yaml loader needs access to this while the project is being instantiated
                self.overrides["manifest_path"] = foundManifestPath
                project = self.find_project(os.path.dirname(foundManifestPath))
        elif project:
            # the manifestPath was pointing to a project, not a manifest
            manifestPath = ""
        else:
            assert manifestPath
            # set this because the yaml loader needs access to this while the project is being instantiated
            self.overrides["manifest_alias"] = manifestPath
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
        manifestPath: str = None,
        homePath: Optional[str] = None,
        parent: Optional["LocalEnv"] = None,
        project: Optional[Project] = None,
        can_be_empty: bool = False,
        override_context: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        readonly: Optional[bool] = False,
    ) -> None:
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
        self.overrides: dict = overrides or (parent and parent.overrides.copy()) or {}
        self.readonly = readonly
        if override_context is not None:
            # aka the --use-environment option
            # hackishly, "" is a valid option used by load_yaml_include
            self.overrides["ENVIRONMENT"] = override_context
        if os.getenv("UNFURL_SKIP_VAULT_DECRYPT"):
            self.overrides["UNFURL_SKIP_VAULT_DECRYPT"] = True
        if os.getenv("UNFURL_SKIP_UPSTREAM_CHECK"):
            self.overrides["UNFURL_SKIP_UPSTREAM_CHECK"] = True

        if parent:
            self.parent = parent
            self._projects: dict = parent._projects
            self._manifests: dict = parent._manifests
            self.homeConfigPath: Optional[str] = parent.homeConfigPath
            self.homeProject: Optional[Project] = parent.homeProject
            self.make_resolver: Optional[Callable] = parent.make_resolver
        else:
            self._projects = {}
            self._manifests = {}
            self.homeConfigPath = get_home_config_path(homePath)
            self.homeProject = self._get_home_project()
            self.make_resolver = None

        self._resolve_path_and_project(manifestPath, can_be_empty)  # type: ignore
        if override_context:
            # set after _resolve_path_and_project() is called
            self.manifest_context_name = override_context
        if project:
            # this arg is used in init.py when creating a project
            # overrides what was set by _resolve_path_and_project()
            self._projects[project.localConfig.config.path] = project
            self.project = project

        if self.project and not parent:  # only log once
            logger.info("Loaded project at %s", self.project.localConfig.config.path)
        self.toolVersions: dict = {}
        self.instanceRepo = self._get_instance_repo()
        self.config = (
            self.project
            and self.project.localConfig
            or self.homeProject
            and self.homeProject.localConfig
            or LocalConfig()
        )
        if override_context and override_context != "defaults":
            assert (
                override_context == self.manifest_context_name
            ), self.manifest_context_name
            project = self.project or self.homeProject
            if not project or self.manifest_context_name not in project.contexts:
                raise UnfurlError(
                    f'No environment named "{self.manifest_context_name}" found.'
                )

    def _get_home_project(self) -> Optional[Project]:
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

    def get_vault_password(self, vaultId: str = "default") -> Optional[str]:
        # used by __main__.vaultclient
        project = self.project or self.homeProject
        if project:
            return project.get_vault_password(self.manifest_context_name, vaultId)
        return None

    def get_vault(self):
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
        return vault

    def get_manifest(
        self,
        path: Optional[str] = None,
        skip_validation: bool = False,
        safe_mode: Optional[bool] = None,
    ) -> "YamlManifest":
        from .yamlmanifest import YamlManifest

        if path and path != self.manifestPath:
            # share projects and ensembles
            localEnv = LocalEnv(path, parent=self, readonly=self.readonly)
            return localEnv.get_manifest()
        else:
            assert self.manifestPath
            manifest = self._manifests.get(self.manifestPath)
            if not manifest:
                # should load vault ids from context
                vault = self.get_vault()
                manifest = YamlManifest(
                    localEnv=self,
                    vault=vault,
                    skip_validation=skip_validation,
                    safe_mode=bool(safe_mode),
                )
                self._manifests[self.manifestPath] = manifest
            return manifest

    def get_project(self, path: str, homeProject: Optional[Project]) -> Project:
        path = Project.normalize_path(path)
        project = self._projects.get(path)
        if not project:
            project = Project(path, homeProject, self.overrides, readonly=self.readonly)
            self._projects[path] = project
        return project

    def get_external_manifest(
        self,
        location: dict,
        skip_validation: bool,
        safe_mode: bool,
    ) -> Optional["YamlManifest"]:
        localEnv = self._get_external_localenv(location)
        if localEnv:
            return localEnv.get_manifest(
                skip_validation=skip_validation, safe_mode=safe_mode
            )
        else:
            return None

    def _get_external_localenv(
        self, location: dict, currentProject: Optional[Project] = None
    ) -> Optional["LocalEnv"]:
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
                repoview = self.homeProject.project_repoview
                if not repoview:
                    return None
                repo = repoview.repo
                file = ""

        if project:
            url, file, revision = split_git_url(project["url"])
            if currentProject:
                repo = currentProject.find_git_repo(url)
            if not repo:
                repo = self.find_git_repo(url)
            if project.get("file"):
                file = os.path.join(file, project.get("file"))

        if not repo:
            return None

        projectRoot = os.path.join(repo.working_dir, file)
        return LocalEnv(
            os.path.join(projectRoot, location.get("file") or ""),
            parent=self,
            readonly=self.readonly,
        )

    def _find_given_manifest_or_project(
        self, manifestPath: str
    ) -> Tuple[Optional[str], Optional[Project]]:
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
            assert os.path.isfile(manifestPath)
            if os.path.basename(manifestPath) == DefaultNames.LocalConfig:
                # assume unfurl.yaml is a project file
                return None, self.get_project(manifestPath, self.homeProject)
            # assume its a pointing to an ensemble
            return manifestPath, None

    def _get_instance_repo(self) -> Optional[GitRepo]:
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
    def _get_git_repos(self) -> List[GitRepo]:
        if self.project:
            repos = self.project._get_git_repos()
        else:
            repos = []
        if self.instanceRepo and self.instanceRepo not in repos:
            return repos + [self.instanceRepo]
        else:
            return repos

    def _search_for_manifest_or_project(
        self, dir: str
    ) -> Tuple[str, Optional[Project]]:
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

    def find_project(self, testPath: str) -> Optional[Project]:
        """
        Walk parents looking for unfurl.yaml
        """
        path = Project.find_path(testPath)
        if path is not None:
            return self.get_project(path, self.homeProject)
        return None

    def get_context(self, context: Optional[dict] = None) -> dict:
        """
        Return a new context that merges the given context with the local context.
        """
        context = context or {}
        project = self.project or self.homeProject
        if project:
            return project.get_context(self.manifest_context_name, context)
        return context

    def get_runtime(self) -> Optional[str]:
        # XXX replace this with top-level section for runtime
        project = self.project or self.homeProject
        while project:
            if project.venv:
                return "venv:" + project.venv
            project = project.parentProject
        return None

    def get_local_instance(self, name: str, context: dict) -> Tuple[NodeInstance, dict]:
        # returns NodeInstance, spec
        assert name in ["locals", "secrets", "local", "secret"]
        attributes = dict(context.get(name) or {})
        # schema is reserved property name
        spec = dict(schema=attributes.pop("schema", None))
        return (
            self.config.create_local_instance(name.rstrip("s"), attributes),
            spec,
        )

    def _find_git_repo(
        self, repoURL: str, revision: Optional[str] = None
    ) -> Union[RepoView, str]:
        project = self.project or self.homeProject
        count = 0
        while project:
            count += 1
            assert count < 4, (
                project.projectRoot,
                project.parentProject and project.parentProject.projectRoot,
            )
            candidate = project._find_git_repo(repoURL, revision)
            if isinstance(candidate, RepoView):
                return candidate
            elif candidate != repoURL:
                repoURL = candidate
            project = project.parentProject
        return repoURL

    def find_git_repo(
        self, repoURL: str, revision: Optional[str] = None
    ) -> Optional[GitRepo]:
        repo_or_url = self._find_git_repo(repoURL, revision)
        if isinstance(repo_or_url, str):
            return None
        return repo_or_url.repo

    def _create_working_dir(self, repoURL, revision, basepath):
        project = self.project or self.homeProject
        if not project:
            logger.warning(
                "Can not create clone repository, ensemble is not in an Unfurl project."
            )
            return None
        while project:
            if basepath is None or project.is_path_in_project(basepath):
                repo = project.create_working_dir(repoURL, revision)
                break
            project = project.parentProject
        else:  # no break
            repo = (self.project or self.homeProject).create_working_dir(  # type: ignore
                repoURL, revision
            )
        return repo

    def find_or_create_working_dir(
        self,
        repoURL: str,
        revision: Optional[str] = None,
        basepath: Optional[str] = None,
        checkout_args: dict = {},
    ) -> Tuple[Optional[GitRepo], Optional[str], Optional[bool]]:
        repoview_or_url = self._find_git_repo(repoURL, revision)
        if isinstance(repoview_or_url, RepoView):
            repo = repoview_or_url.repo
            assert repo
            logger.debug(
                "Using existing repository at %s for %s", repo.working_dir, repoURL
            )
            if (
                not self.overrides.get("UNFURL_SKIP_UPSTREAM_CHECK")
                and not repo.is_dirty()
            ):
                repo.pull(revision=revision)
        else:
            assert isinstance(
                repoview_or_url, str
            ), repoview_or_url  # it's the repoUrl (possibly rewritten) at this point
            url = repoview_or_url
            # git-local repos must already exist locally
            if url.startswith("git-local://"):
                return None, None, None

            repo = self._create_working_dir(url, revision, basepath)
            if not repo:
                return None, None, None

        assert isinstance(repo, GitRepo)
        if revision:
            if repo.revision != repo.resolve_rev_spec(revision) or checkout_args:
                if repo.is_dirty():
                    logger.warning(
                        f"{repo.working_dir} is dirty, skipping checking out revision {revision}"
                    )
                else:
                    repo.checkout(revision, **checkout_args)
            return repo, repo.revision, False
        else:
            return repo, repo.revision, False

    def find_path_in_repos(
        self, path: str, importLoader: Optional[Any] = None
    ) -> Tuple[Optional[GitRepo], Optional[str], Optional[str], Optional[bool]]:
        """If the given path is part of the working directory of a git repository
        return that repository and a path relative to it"""
        # importloader is unused until pinned revisions are supported
        if self.instanceRepo:
            repo = self.instanceRepo
            filePath = repo.find_repo_path(path)
            if filePath is not None:
                return repo, filePath, repo.revision, False

        candidate = None
        repo = None  # type: ignore
        project = self.project or self.homeProject
        while project:
            repo, filePath, revision, bare = project.find_path_in_repos(  # type: ignore
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

    def map_value(self, val: Any, env_rules: Optional[dict]) -> Any:
        """
        Evaluate using project home as a base dir.
        """
        from .runtime import NodeInstance
        from .eval import map_value, RefContext

        instance = NodeInstance()
        instance._baseDir = self.config.config.get_base_dir()
        if env_rules is not None:
            instance._environ = filter_env(env_rules, instance.environ)
        return map_value(val, RefContext(instance))

    def get_paths(self) -> List[str]:
        """
        If asdf is installed, build a PATH list from .toolversions
        found in the current project and the home project.
        """
        paths: List[str] = []
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
            os.environ["ASDF_DATA_DIR"] = asdfDataDir
            # current project has higher priority over home project
            project = self.project or self.homeProject
            count = 0
            while project:
                paths += Project.get_asdf_paths(
                    project.projectRoot, asdfDataDir, self.toolVersions
                )
                count += 1
                assert count < 4
                project = project.parentProject
            if os.getenv("HOME"):
                paths += Project.get_asdf_paths(
                    os.getenv("HOME"), asdfDataDir, self.toolVersions
                )
        return paths
