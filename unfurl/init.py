# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
This module implements creating and cloning project and ensembles as well Unfurl runtimes.
"""
import datetime
import os
import os.path
import shutil
import sys
import json
from typing import Any, Dict, Optional
import uuid
import logging
from jinja2.loaders import FileSystemLoader
from pathlib import Path

from . import DefaultNames, __version__, get_home_config_path, is_version_unreleased
from .localenv import LocalEnv, Project, LocalConfig
from .repo import (
    GitRepo,
    Repo,
    is_url_or_git_path,
    split_git_url,
    commit_secrets,
    sanitize_url,
)
from .util import UnfurlError, get_random_password, substitute_env
from .yamlloader import make_yaml, make_vault_lib

_templatePath = os.path.join(os.path.abspath(os.path.dirname(__file__)), "templates")


def rename_for_backup(dir):
    ctime = datetime.datetime.fromtimestamp(os.stat(dir).st_ctime)
    new = dir + "." + ctime.strftime("%Y-%m-%d-%H-%M-%S")
    os.rename(dir, new)
    return new


def _write_file(folder, filename, content):
    if not os.path.isdir(folder):
        os.makedirs(os.path.normpath(folder))
    filepath = os.path.join(folder, filename)
    with open(filepath, "w") as f:
        f.write(content)
    return filepath


def write_template(folder, filename, template, vars, templateDir=None):
    from .eval import RefContext
    from .runtime import NodeInstance
    from .support import apply_template

    if templateDir and not (os.path.isabs(templateDir) or templateDir[0] == "."):
        # built-in template
        templateDir = os.path.join(_templatePath, templateDir)

    if templateDir:
        searchPath = [templateDir, _templatePath]
    else:
        searchPath = [_templatePath]

    if not templateDir or not os.path.exists(os.path.join(templateDir, template)):
        # use default file if missing from templateDir
        templateDir = _templatePath

    with open(os.path.join(templateDir, template)) as f:
        source = f.read()
    instance = NodeInstance()
    instance._baseDir = _templatePath

    overrides = dict(loader=FileSystemLoader(searchPath))
    content = apply_template(source, RefContext(instance, vars), overrides)
    return _write_file(folder, filename, content)


def write_project_config(
    projectdir,
    filename=DefaultNames.LocalConfig,
    templatePath=DefaultNames.LocalConfig + ".j2",
    vars=None,
    templateDir=None,
):
    _vars = dict(include="", manifestPath=None)
    if vars:
        _vars.update(vars)
    return write_template(projectdir, filename, templatePath, _vars, templateDir)


def create_home(
    home=None, render=False, replace=False, runtime=None, no_runtime=None, **kw
):
    """
    Create the home project if missing
    """
    homePath = get_home_config_path(home)
    if not homePath:
        return None
    exists = os.path.exists(homePath)
    if exists and not replace:
        return None

    homedir, filename = os.path.split(homePath)
    if render:  # just render
        repo = Repo.find_containing_repo(homedir)
        # XXX if repo and update: git stash; git checkout rendered
        ensembleDir = os.path.join(homedir, DefaultNames.EnsembleDirectory)
        ensembleRepo = Repo.find_containing_repo(ensembleDir)
        configPath, ensembleDir, password_vault = render_project(
            homedir, repo, ensembleRepo, None, "home"
        )
        # XXX if repo and update: git commit -m"updated"; git checkout master; git stash pop
        return configPath
    else:
        if exists:
            rename_for_backup(homedir)

    newHome, configPath, repo = create_project(
        homedir,
        skeleton="home",
        runtime=runtime or "venv:",
        no_runtime=no_runtime,
        msg="Create the unfurl home repository",
        creating_home=True,
    )
    if repo:
        repo.repo.git.branch("rendered")  # now create a branch
    return configPath


def _create_repo(gitDir, ignore=True):
    import git

    if not os.path.isdir(gitDir):
        os.makedirs(gitDir)
    repo = git.Repo.init(gitDir)
    repo.index.add(add_hidden_git_files(gitDir))
    msg = f"Initial Commit for {uuid.uuid1()}"
    repo.index.commit(msg)
    repo.create_tag("INITIAL", message=msg)

    if ignore:
        Repo.ignore_dir(gitDir)
    return GitRepo(repo)


def write_service_template(projectdir):
    from .tosca import TOSCA_VERSION

    vars = dict(version=TOSCA_VERSION)
    return write_template(
        projectdir, "service-template.yaml", "service-template.yaml.j2", vars
    )


def write_ensemble_manifest(
    destDir: str,
    manifestName: str,
    specRepo,
    specDir=None,
    extraVars=None,
    templateDir=None,
):
    if specDir:
        specDir = os.path.abspath(specDir)
    else:
        specDir = ""
    vars = dict(specRepoUrl=specRepo.get_url_with_path(specDir, True))
    if extraVars:
        vars.update(extraVars)
    return write_template(destDir, manifestName, "manifest.yaml.j2", vars, templateDir)


def add_hidden_git_files(gitDir):
    # write .gitignore and  .gitattributes
    gitIgnorePath = write_template(gitDir, ".gitignore", "gitignore.j2", {})
    gitAttributesContent = (
        f"**/*{DefaultNames.JobsLog} merge=union\n*.remotelock lockable\n"
    )
    gitAttributesPath = _write_file(gitDir, ".gitattributes", gitAttributesContent)
    return [os.path.abspath(gitIgnorePath), os.path.abspath(gitAttributesPath)]


def _set_ensemble_vars(vars, externalProject, ensemblePath, context):
    if externalProject:
        vars["manifestPath"] = externalProject.get_relative_path(ensemblePath)
        vars["external"] = externalProject.name
    vars["context"] = context


def _warn_about_new_password(localProjectConfig):
    logger = logging.getLogger("unfurl")
    logger.warning(
        "A password was generated and included in the local config file at %s -- "
        "please keep this password safe, without it you will not be able to decrypt any encrypted files "
        "committed to the repository.",
        localProjectConfig,
    )


def render_project(
    projectdir,
    repo,
    ensembleRepo,
    homePath,
    templateDir=None,
    names=DefaultNames,
    use_context=None,
    mono=False,
    no_secrets=False,
    skeleton_vars: Optional[Dict[str, Any]] = None,
):
    """
    Creates a folder named `projectdir` with a git repository with the following files:

    unfurl.yaml
    local/unfurl.yaml
    ensemble-template.yaml
    ensemble/ensemble.yaml

    Returns the absolute path to unfurl.yaml
    """
    assert os.path.isabs(projectdir), projectdir + " must be an absolute path"
    # write the project files
    localConfigFilename = names.LocalConfig

    externalProject = None
    ensembleDir = os.path.join(projectdir, names.EnsembleDirectory)
    if ensembleRepo:
        if ensembleRepo.working_dir not in projectdir:
            externalProject = find_project(ensembleRepo.working_dir, homePath)
        if externalProject:
            dirname, ensembleDirName = os.path.split(projectdir)
            if ensembleDirName == DefaultNames.ProjectDirectory:
                ensembleDirName = os.path.basename(dirname)
            relPath = externalProject.get_relative_path(
                os.path.join(ensembleRepo.working_dir, ensembleDirName)
            )
            ensembleDir = externalProject.get_unique_path(relPath)
        manifestName = names.Ensemble
        ensemblePath = os.path.join(ensembleDir, manifestName)

    if skeleton_vars:
        vars = skeleton_vars.copy()
    else:
        vars = {}
    if "vaultpass" not in vars:
        vars["vaultpass"] = get_random_password()
    vaultpass = vars["vaultpass"]
    if "vaultid" not in vars:
        # use project name plus a couple of random digits to avoid collisions
        vars["vaultid"] = (
            Project.get_name_from_dir(projectdir)
            + get_random_password(2, "", "").upper()
        )
    vaultid = vars["vaultid"]

    # only commit external ensembles references if we are creating a mono repo
    # otherwise record them in the local config:
    localExternal = use_context and externalProject and not mono
    if ensembleRepo and (ensembleRepo.is_local_only() or localExternal):
        _set_ensemble_vars(vars, externalProject, ensemblePath, use_context)
    if localExternal:
        # since this is specified while creating the project set this as the default context
        vars["default_context"] = use_context

    localProjectConfig = write_project_config(
        os.path.join(projectdir, "local"),
        localConfigFilename,
        "unfurl.local.yaml.j2",
        vars,
        templateDir,
    )

    if not skeleton_vars or "vaultpass" not in skeleton_vars:
        _warn_about_new_password(localProjectConfig)

    localInclude = "+?include-local: " + os.path.join("local", localConfigFilename)

    if no_secrets:
        secretsInclude = ""
    else:
        write_project_config(
            os.path.join(projectdir, "secrets"),
            names.SecretsConfig,
            "secrets.yaml.j2",
            vars,
            templateDir,
        )
        secretsInclude = "+?include-secrets: " + os.path.join(
            "secrets", names.SecretsConfig
        )

    # note: local overrides secrets
    vars = dict(include=secretsInclude + "\n" + localInclude, vaultid=vaultid)
    if skeleton_vars:
        vars.update(skeleton_vars)
    if use_context and not localExternal:
        # since this is specified while creating the project set this as the default context
        vars["default_context"] = use_context
    if ensembleRepo and not (ensembleRepo.is_local_only() or localExternal):
        _set_ensemble_vars(vars, externalProject, ensemblePath, use_context)
    projectConfigPath = write_project_config(
        projectdir,
        names.LocalConfig,
        "unfurl.yaml.j2",
        vars,
        templateDir,
    )
    write_project_config(
        projectdir,
        names.LocalConfigTemplate,
        "local-unfurl-template.yaml.j2",
        vars,
        templateDir,
    )

    # write ensemble-template.yaml
    write_template(
        projectdir,
        names.EnsembleTemplate,
        "manifest-template.yaml.j2",
        {},
        templateDir,
    )

    if ensembleRepo:
        extraVars = dict(
            ensembleUri=ensembleRepo.get_url_with_path(ensemblePath, True),
            # include the ensembleTemplate in the root of the specDir
            ensembleTemplate=names.EnsembleTemplate,
        )
        if skeleton_vars:
            extraVars.update(skeleton_vars)
        # write ensemble/ensemble.yaml
        write_ensemble_manifest(
            ensembleDir,
            manifestName,
            repo,
            projectdir,
            extraVars=extraVars,
            templateDir=templateDir,
        )
    if externalProject:
        # add the external project to the project and localRepositories configuration sections
        # split repos should not have references to ensembles
        # so register it with the local project config if not a mono repo
        configPath = localProjectConfig if localExternal else projectConfigPath
        LocalConfig(configPath).register_project(externalProject)
        externalProject.register_ensemble(
            ensemblePath, managedBy=find_project(projectdir, homePath)
        )
    return projectConfigPath, ensembleDir, make_vault_lib(vaultpass, vaultid)


def _find_project_repo(projectdir):
    repo = Repo.find_containing_repo(projectdir)
    if not repo:
        raise UnfurlError("Could not find an existing repository")
    if not repo.repo.head.is_valid():
        raise UnfurlError(
            "Existing repository is empty: unable to create project in empty git repositories"
        )
    return repo


def _find_ensemble_repo(projectdir, shared, submodule, ensemble_name):
    if shared:
        ensembleRepo = Repo.find_containing_repo(shared)
        if not ensembleRepo:
            raise UnfurlError("can not find shared repository " + shared)
    else:
        ensembleDir = os.path.join(projectdir, ensemble_name)
        ensembleRepo = _create_repo(ensembleDir, not submodule)
    return ensembleRepo


def _commit_repos(projectdir, repo, ensembleRepo, shared, kw, ensembleDir, newHome):
    if ensembleRepo:
        ensembleRepo.add_all(ensembleDir)
        if shared:
            message = "Adding ensemble"
        else:
            message = "Default ensemble repository boilerplate"
        ensembleRepo.repo.index.commit(message)

    if kw.get("submodule"):
        repo.add_sub_module(ensembleDir)

    if not newHome and not kw.get("no_runtime") and kw.get("runtime"):
        # if runtime was explicitly set and we aren't creating the home project
        # then initialize the runtime here
        try:
            init_engine(projectdir, kw.get("runtime"))
        except:
            pass  # don't stop even if this fails

    repo.add_all(projectdir)
    repo.repo.index.commit(kw.get("msg") or "Create a new Unfurl project")


def _get_shared(kw, homePath):
    shared = kw.get("shared_repository")
    if shared:
        return shared
    context = kw.get("use_environment")
    if context and homePath:
        homeProject = find_project(homePath, None)
        assert homeProject
        return homeProject.get_default_project_path(context)
    return None


def create_project(
    projectdir,
    ensemble_name=None,
    home=None,
    mono=False,
    existing=False,
    empty=False,
    skeleton=None,
    creating_home=False,
    **kw,
):
    create_context = kw.get("create_environment")
    use_context = kw.get("use_environment")
    skeleton_vars = dict((n, v) for n, v in kw.get("var", []))
    if existing:
        repo = _find_project_repo(projectdir)
    else:
        repo = None

    if create_context:
        # set context to the project name
        create_context = os.path.basename(projectdir)
        # defaults for a repository for an entire context
        mono = True
        if not ensemble_name:
            empty = True

    names = DefaultNames(EnsembleDirectory=ensemble_name)
    newHome = ""
    homePath = get_home_config_path(home)
    # don't try to create the home project if we are already creating the home project
    if (
        not creating_home
        and homePath is not None
        and projectdir != os.path.dirname(homePath)
    ):
        # create the home project (but only if it doesn't exist already)
        newHome = create_home(
            home, runtime=kw.get("runtime"), no_runtime=kw.get("no_runtime")
        )

    if repo:
        add_hidden_git_files(projectdir)
    else:
        repo = _create_repo(projectdir)

    shared = _get_shared(kw, homePath)
    submodule = kw.get("submodule")
    if mono and not shared:
        ensembleRepo = repo
    else:
        ensembleRepo = _find_ensemble_repo(
            projectdir, shared, submodule, names.EnsembleDirectory
        )

    projectConfigPath, ensembleDir, password_vault = render_project(
        projectdir,
        repo,
        not empty and ensembleRepo,
        homePath,
        skeleton,
        names,
        create_context or use_context,
        mono,
        False,
        skeleton_vars,
    )
    if homePath and create_context:
        newProject = find_project(projectConfigPath, homePath)
        assert newProject
        homeProject = newProject.parentProject
        assert homeProject
        homeProject.register_project(newProject, create_context)

    if not kw.get("render"):
        if password_vault:
            yaml = make_yaml(password_vault)
            commit_secrets(os.path.dirname(projectConfigPath), yaml, repo)

        _commit_repos(
            projectdir,
            repo,
            not mono and ensembleRepo,
            shared,
            kw,
            ensembleDir,
            newHome,
        )

    return newHome, projectConfigPath, repo


def clone_local_repos(manifest, sourceProject, targetProject):
    # We need to clone repositories that are local to the source project
    # otherwise we won't be able to find them
    for repoView in manifest.repositories.values():
        repoSpec = repoView.repository
        if repoSpec.name == "self":
            continue
        # XXX should look in home project too
        repo = sourceProject.find_git_repo_from_repository(repoSpec)
        if repo:
            targetProject.find_or_clone(repo)


def _create_ensemble_repo(manifest, repo, commit=True):
    destDir = os.path.dirname(manifest.manifest.path)
    if not repo:
        repo = _create_repo(destDir)
    elif not os.path.isdir(destDir):
        os.makedirs(destDir)

    manifest.metadata["uri"] = repo.get_url_with_path(manifest.manifest.path, True)
    with open(manifest.manifest.path, "w") as f:
        manifest.dump(f)

    if commit:
        repo.repo.index.add([manifest.manifest.path])
        repo.repo.index.commit("Default ensemble repository boilerplate")
    return repo


def _looks_like(path, name):
    # in case path is a directory:
    if os.path.isfile(os.path.join(path, name)):
        return path, name
    if path.endswith(name):  # name is explicit so don't need to check if file exists
        return os.path.split(path)
    return None


def _get_ensemble_paths(sourcePath, sourceProject, want_init, use_environment):
    """
    Returns either a pointer to the ensemble to clone
    or a dict of variables to pass to an ensemble template to create a new one

    if sourcePath doesn't exist, return {}

    look for an ensemble given sourcePath (unless sourcePath looks like a service template)
    if that fails look for (ensemble-template, service-template) if sourcePath is a directory
    otherwise
        return {}
    """
    template = None
    relPath = sourcePath or "."
    if not os.path.exists(relPath):
        raise UnfurlError(
            f'Given clone source "{os.path.abspath(relPath)}" does not exist.'
        )

    # we only support cloning TOSCA service templates if their names end in "service-template.yaml"
    isServiceTemplate = sourcePath.endswith(DefaultNames.ServiceTemplate)
    if not want_init and not isServiceTemplate:
        try:
            localEnv = LocalEnv(
                relPath, project=sourceProject, override_context=use_environment
            )
            sourceDir = sourceProject.get_relative_path(
                os.path.dirname(localEnv.manifestPath)
            )
            # note: if sourceDir.startswith("..") then ensemble lives in another's project's repo
            return dict(sourceDir=sourceDir, localEnv=localEnv)
        except UnfurlError:
            # XXX if UnfurlError is "could not find external project", reraise
            pass

    # didn't find the specified file (or the default ensemble if none was specified)
    # so if sourcePath was a directory try for one of the default template files
    if isServiceTemplate or os.path.isdir(relPath):
        # look for an ensemble-template or service-template in source path
        if os.path.isdir(os.path.join(sourcePath, DefaultNames.ProjectDirectory)):
            sourcePath = os.path.join(sourcePath, DefaultNames.ProjectDirectory)
        template = _looks_like(sourcePath, DefaultNames.EnsembleTemplate)
        if template:
            sourceDir = sourceProject.get_relative_path(template[0])
            return dict(sourceDir=sourceDir, ensembleTemplate=template[1])
        template = _looks_like(sourcePath, DefaultNames.ServiceTemplate)
        if template:
            sourceDir = sourceProject.get_relative_path(template[0])
            return dict(sourceDir=sourceDir, serviceTemplate=template[1])
        # nothing valid found
    return {}


def find_project(source, home_path):
    sourceRoot = Project.find_path(source)
    if sourceRoot:
        if home_path:
            return Project(sourceRoot, Project(home_path))
        return Project(sourceRoot)
    return None


def _get_context_and_shared_repo(project, options):
    # when creating ensemble, get the default project for the given context if set
    shared_repo = None
    shared = options.get("shared_repository")
    context = options.get("use_environment")
    if not context:
        context = project.get_default_context()
    if not shared and context:
        shared = project.get_default_project_path(context)
        if not shared and context not in project.contexts:
            raise UnfurlError(
                f'environment "{context}" not found in project at "{project.projectRoot}"'
            )
    if shared:
        shared_repo = Repo.find_containing_repo(shared)
        if not shared_repo:
            raise UnfurlError("can not find shared repository " + shared)

    return context, shared_repo


class EnsembleBuilder:
    def __init__(self, source: str, ensemble_name: str, options: dict):
        # user specified url or path
        self.input_source = source
        self.options = options
        self.ensemble_name = ensemble_name
        self.mono = options.get("mono") or options.get("existing")
        self.home_path = get_home_config_path(options.get("home"))
        self.skeleton_vars = dict((n, v) for n, v in options.get("var", []))

        self.source_project = None  # step 1
        self.source_path = None  # step 1 relative path in source_project

        self.templateVars = None  # step 2
        self.environment = None  # step 2 environment name
        self.shared_repo = None  # step 2

        self.dest_project = None  # step 3
        self.dest_path = None  # step 3 relative path in dest_project

        self.manifest = None  # final step
        self.logger = logging.getLogger("unfurl")

    def create_project_from_ensemble(self, dest):
        # XXX create a new project from scratch for the ensemble
        #     if os.path.exists(dest) and os.listdir(dest):
        #         raise UnfurlError(
        #             'Can not create a project in "%s": folder is not empty' % dest
        #         )
        #     newHome, projectConfigPath, repo = createProject(
        #         dest, empty=True, **options
        #     )
        #     return Project(projectConfigPath)
        raise UnfurlError(
            f"Can't clone \"{self.input_source}\": it isn't in an Unfurl project or repository"
        )

    def configure(self):
        assert not self.templateVars

        # source is a path into the project relative to the current directory
        assert self.source_path
        source_path = os.path.join(self.source_project.projectRoot, self.source_path)
        self.templateVars = _get_ensemble_paths(
            source_path,
            self.source_project,
            self.options.get("want_init"),
            self.options.get("use_environment"),
        )
        if self.options.get("use_deployment_blueprint"):
            self.templateVars["deployment_blueprint"] = self.options[
                "use_deployment_blueprint"
            ]
        (self.environment, self.shared_repo) = _get_context_and_shared_repo(
            self.dest_project, self.options
        )

    @staticmethod
    def _get_ensemble_dir(targetPath):
        assert not os.path.isabs(targetPath), targetPath
        if not targetPath or targetPath == ".":
            destDir, manifestName = (
                DefaultNames.EnsembleDirectory,
                DefaultNames.Ensemble,
            )
        elif targetPath.endswith(".yaml") or targetPath.endswith(".yml"):
            destDir, manifestName = os.path.split(targetPath)
        else:
            destDir = targetPath
            manifestName = DefaultNames.Ensemble
        return destDir, manifestName

    def _get_inputs_template(self):
        assert self.source_project
        local_template = os.path.join(
            self.source_project.projectRoot, DefaultNames.InputsTemplate
        )
        if os.path.isfile(local_template):
            with open(local_template) as s:
                return s.read()
        return None

    def _create_ensemble_from_template(self, project, destDir, manifestName):
        from unfurl import yamlmanifest

        specProject = self.dest_project
        assert project
        assert self.source_project
        sourceDir = os.path.normpath(
            os.path.join(
                self.source_project.projectRoot, self.templateVars["sourceDir"]
            )
        )
        specRepo, relPath, revision, bare = specProject.find_path_in_repos(sourceDir)
        if not specRepo:
            raise UnfurlError(
                '"%s" is not in a git repository. Cloning from plain file directories not yet supported'
                % os.path.abspath(sourceDir)
            )
        manifestPath = write_ensemble_manifest(
            os.path.join(project.projectRoot, destDir),
            manifestName,
            specRepo,
            sourceDir,
            self.templateVars,
            self.options.get("skeleton"),
        )
        localEnv = LocalEnv(
            manifestPath,
            project=project,
            override_context=self.options.get("use_environment"),
        )
        manifest = yamlmanifest.ReadOnlyManifest(
            localEnv=localEnv, vault=localEnv.get_vault()
        )
        return localEnv, manifest

    def create_new_ensemble(self):
        """
        If "localEnv" is in templateVars, clone that ensemble;
        otherwise create one from a template with templateVars
        """
        from unfurl import yamlmanifest

        if self.shared_repo:
            destProject = find_project(self.shared_repo.working_dir, self.home_path)
            assert destProject
        else:
            destProject = self.dest_project

        assert destProject
        assert self.templateVars
        assert not self.manifest
        assert self.dest_path is not None

        destDir, manifestName = self._get_ensemble_dir(self.dest_path)
        # choose a destDir that doesn't conflict with an existing folder
        # (i.e. if default ensemble already exists)
        if self.options.get("overwrite"):
            destDir = os.path.join(destProject.projectRoot, destDir)
        else:
            destDir = destProject.get_unique_path(destDir)

        # destDir is now absolute
        targetPath = os.path.normpath(os.path.join(destDir, manifestName))
        assert (not self.shared_repo) or targetPath.startswith(
            self.shared_repo.working_dir
        ), (
            targetPath,
            self.shared_repo.working_dir,
        )

        templateVars = self.templateVars
        if "localEnv" not in templateVars:
            # we found a template file to clone
            templateVars["inputs"] = self._get_inputs_template()
            localEnv, manifest = self._create_ensemble_from_template(
                destProject,
                destDir,
                manifestName,
            )
        else:
            # look for an ensemble at the given path or use the source project's default
            localEnv = templateVars["localEnv"]
            manifest = yamlmanifest.clone(localEnv, targetPath)

        _create_ensemble_repo(
            manifest,
            self.shared_repo or self.mono and self.dest_project.project_repoview.repo,
            not self.options.get("render"),
        )

        if destProject.projectRoot != self.dest_project.projectRoot:
            # cross reference each other
            destProject.register_ensemble(
                manifest.path, managedBy=self.dest_project, context=self.environment
            )
            self.dest_project.register_ensemble(
                manifest.path, project=destProject, context=self.environment
            )
        else:
            destProject.register_ensemble(manifest.path, context=self.environment)
        destProject.project_repoview.repo.commit_files(
            [destProject.localConfig.config.path], "Add ensemble"
        )
        self.manifest = manifest
        return destDir

    def clone_local_project(self, currentProject, sourceProject, dest_dir):
        self.source_path = sourceProject.get_relative_path(self.input_source)
        assert self.source_path
        assert not self.source_path.startswith(
            ".."
        ), f"{self.source_path} should be inside the project"
        if currentProject:
            repoURL = sourceProject.project_repoview.repo.url
            if currentProject.find_git_repo(repoURL):
                # if the repo has already been cloned into this project, just use that one
                newrepo = currentProject.find_or_create_working_dir(repoURL)
                search = os.path.join(newrepo.working_dir, self.source_path)
            else:
                # just register the source project's repo as a localRepository
                currentProject.register_project(sourceProject, save_project=False)
                self.source_project = sourceProject
                return sourceProject
        else:
            # clone the source project's git repo
            newrepo = sourceProject.project_repoview.repo.clone(dest_dir)
            search = os.path.join(
                dest_dir, sourceProject.project_repoview.path, self.source_path
            )

        self.source_project = find_project(search, self.home_path)
        assert (
            self.source_project
        ), f"project not found in {search}, cloned to {newrepo.working_dir}"
        return self.source_project

    def clone_remote_project(self, currentProject, destDir):
        # check if source is a git url
        repoURL, filePath, revision = split_git_url(self.input_source)

        if currentProject:
            repo = currentProject.find_or_create_working_dir(repoURL, revision)
            destDir = repo.working_dir
        else:
            if os.path.exists(destDir) and os.listdir(destDir):
                raise UnfurlError(
                    f'Can not clone project into "{destDir}": folder is not empty'
                )
            # clone the remote repo to destDir
            Repo.create_working_dir(repoURL, destDir, revision)

        targetDir = os.path.join(destDir, filePath)
        sourceProjectRoot = Project.find_path(targetDir, destDir)
        clean_url = sanitize_url(self.input_source)
        self.logger.debug(f'cloned "{clean_url}" to "{destDir}"')
        if not sourceProjectRoot:
            raise UnfurlError(
                f'Error: cloned "{clean_url}" to "{destDir}" but couldn\'t find an Unfurl project'
            )

        self.source_project = find_project(sourceProjectRoot, self.home_path)
        # set source to point to the cloned project
        assert self.source_project
        self.source_path = self.source_project.get_relative_path(targetDir)
        return self.source_project

    def _needs_local_config(self, clonedProject):
        return self.skeleton_vars and not os.path.exists(
            os.path.join(clonedProject.projectRoot, "local", DefaultNames.LocalConfig)
        )

    def set_dest_project_and_path(
        self, existingSourceProject, existingDestProject, dest
    ):
        assert self.dest_project is None
        if existingDestProject:
            #  set that as the dest_project
            self.dest_project = existingDestProject
        else:
            # otherwise set source_project as the dest_project
            self.dest_project = self.source_project
            if self.source_project is not existingSourceProject:
                # set "" as dest because we already "consumed" dest by cloning the project to that location
                dest = ""

        assert self.dest_project is not None
        if not existingDestProject or self._needs_local_config(self.dest_project):
            # create local/unfurl.yaml in the new project
            if _create_local_config(self.dest_project, self.logger, self.skeleton_vars):
                # reload project with the new local project config
                self.dest_project = find_project(
                    self.dest_project.projectRoot, self.home_path
                )

        if os.path.isabs(dest):
            relDestDir = self.dest_project.get_relative_path(dest)
            assert not relDestDir.startswith(".."), relDestDir
        else:
            relDestDir = dest.lstrip("./")
        if (
            self.ensemble_name
            and self.ensemble_name != DefaultNames.EnsembleDirectory
            or relDestDir == "."
            or not relDestDir
        ):
            relDestDir = self.ensemble_name
        self.dest_path = relDestDir

    def has_existing_ensemble(self, sourceProject):
        if self.source_project is not sourceProject and not self.shared_repo:
            assert self.dest_project
            if "localEnv" in self.templateVars and os.path.exists(
                Path(self.dest_project.projectRoot) / self.dest_path
            ):
                # the ensemble is already part of the source project repository or a submodule
                return True
        return False

    def set_source(self, sourceProject):
        self.source_project = sourceProject
        # make source relative to the source project
        source_path = sourceProject.get_relative_path(self.input_source)
        assert not source_path.startswith("..")
        self.source_path = source_path

    def set_ensemble(self, isRemote, existingSourceProject, existingDestProject):
        sourceWasCloned = self.source_project is not existingSourceProject
        destIsNew = not existingDestProject
        assert self.dest_project
        if destIsNew and self.has_existing_ensemble(existingSourceProject):
            # if dest_project is new (we just cloned it)
            # check if we cloned the ensemble already
            # if so we done, we don't need to create a new one
            return (
                "Cloned project with a pre-existing ensemble to "
                + self.dest_project.projectRoot
            )

        if not self.templateVars:
            # source wasn't pointing to an ensemble to clone
            if sourceWasCloned:
                # but we cloned a project
                return "Cloned empty project to " + self.dest_project.projectRoot
            else:
                # can't find anything to do, so raise an error
                raise UnfurlError(
                    f'Can\'t find anything to clone in "{self.input_source}"'
                )

        destDir = self.create_new_ensemble()
        assert self.manifest
        if not isRemote and existingSourceProject is not self.source_project:
            # we need to clone the referenced local repos so the new project has access to them
            clone_local_repos(self.manifest, existingSourceProject, self.source_project)
        return f'Created new ensemble at "{os.path.abspath(destDir)}"'


def clone(
    source: str,
    dest: str,
    ensemble_name: str = DefaultNames.EnsembleDirectory,
    **options: Any,
) -> str:
    """
    Clone the ``source`` ensemble to ``dest``. If ``dest`` isn't in a project, create one.
    ``source`` can point to an ensemble_template, a service_template, an existing ensemble
    or a folder containing one of those. If it points to a project its default ensemble will be cloned.

    Referenced `repositories` will be cloned if a git repository or copied if a regular file folder,
    If the folders already exist they will be copied to new folder unless the git repositories have the same HEAD.
    but the local repository names will remain the same.

    ======================= =============================================
    dest                    result
    ======================= =============================================
    Inside source project   new ensemble
    missing or empty folder clone project, new or cloned ensemble
    another project         new or cloned ensemble with source as spec
    non-empty folder        error
    ======================= =============================================

    """
    builder = EnsembleBuilder(source, ensemble_name, options)
    if not dest:
        dest = Repo.get_path_for_git_repo(source)  # choose dest based on source url
        if os.path.exists(dest):
            raise UnfurlError(
                'Can not clone project to "'
                + dest
                + '": file already exists with that name'
            )
    currentProject = find_project(dest, builder.home_path)

    ### step 1: clone the source repository and set the the source path
    sourceProject = None
    isRemote = is_url_or_git_path(source)

    if isRemote:
        builder.clone_remote_project(currentProject, dest)
    else:
        sourceProject = find_project(source, builder.home_path)
        if not sourceProject or not sourceProject.project_repoview.repo:
            # source wasn't in a project in a repo
            # XXX currently just raises error
            return builder.create_project_from_ensemble(dest)

        relDestDir = sourceProject.get_relative_path(dest)
        if relDestDir.startswith("..") and (
            not currentProject
            or not currentProject.project_repoview.repo
            or currentProject.project_repoview.repo.working_dir
            != sourceProject.project_repoview.repo.working_dir
        ):
            # dest is outside the source project and is in a different repo than the current project
            # so clone the source project
            builder.clone_local_project(currentProject, sourceProject, dest)
        else:
            # dest is in the source project's repo
            # so don't need to clone, just need to create an ensemble
            builder.set_source(sourceProject)
            dest = os.path.abspath(dest)

    assert builder.source_project

    ##### step 2: create destination project if neccessary
    builder.set_dest_project_and_path(sourceProject, currentProject, dest)
    if options.get("empty"):
        # don't create an ensemble
        return "Cloned empty project to " + builder.dest_project.projectRoot

    ##### step 3: examine source for template details and determine shared project
    builder.configure()

    ##### step 4 create ensemble in destination project if needed
    return builder.set_ensemble(isRemote, sourceProject, currentProject)


def _create_local_config(clonedProject, logger, vars):
    local_template = os.path.join(
        clonedProject.projectRoot, DefaultNames.LocalConfigTemplate
    )
    if os.path.isfile(local_template):
        with open(local_template) as s:
            contents = s.read()

        contents = "\n".join(
            [line for line in contents.splitlines() if not line.startswith("##")]
        )
        dest = os.path.join(
            clonedProject.projectRoot, "local", DefaultNames.LocalConfig
        )
        # log warning if invoked
        vars["generate_new_vault_password"] = (
            lambda: _warn_about_new_password(dest) or get_random_password()
        )
        # replace ${var} or preserve if not set
        contents = substitute_env(contents, vars, True)

        _write_file(
            os.path.join(clonedProject.projectRoot, "local"),
            DefaultNames.LocalConfig,
            contents,
        )

        logger.info(
            f'Generated new a local project configuration file at "{dest}"\n'
            "Please review it for any instructions on configuring this project."
        )
        return True
    return False


def _get_unfurl_requirement_url(spec):
    """Expand the given string in an URL for installing the local Unfurl package.

    If @ref is omitted the tag for the current release will be used,
    if empty ("@") the latest revision will be used
    If no path or url is specified https://github.com/onecommons/unfurl.git will be used.

    For example:

    @tag
    ./path/to/local/repo
    ./path/to/local/repo@tag
    ./path/to/local/repo@
    git+https://example.com/forked/unfurl.git
    @

    Args:
        spec (str): can be a path to a git repo, git url or just a revision or tag.

    Returns:
      str: Description of returned object.

    """
    if not spec:
        return spec
    if "egg=unfurl" in spec:
        # looks fully specified, just return it
        return spec

    url, sep, ref = spec.rpartition("@")
    if sep:
        if ref:
            ref = "@" + ref
    else:
        ref = "@" + __version__()

    if not url:
        return "git+https://github.com/onecommons/unfurl.git" + ref + "#egg=unfurl"
    if not url.startswith("git+"):
        return "git+file://" + os.path.abspath(url) + ref + "#egg=unfurl"
    else:
        return url + ref + "#egg=unfurl"


def init_engine(projectDir, runtime):
    runtime = runtime or "venv:"
    kind, sep, rest = runtime.partition(":")
    if kind == "venv":
        pipfileLocation, sep, unfurlLocation = rest.partition(":")
        return create_venv(
            projectDir, pipfileLocation, _get_unfurl_requirement_url(unfurlLocation)
        )
    # XXX else kind == 'docker':
    return "unrecognized runtime uri"


def _run_pip_env(do_install, pipenv_project, kw):
    # create the virtualenv and install the dependencies specified in the Pipefiles
    sys_exit = sys.exit
    try:
        retcode = 0

        def noexit(code):
            retcode = code

        sys.exit = noexit  # type: ignore

        do_install(pipenv_project, **kw)
    finally:
        sys.exit = sys_exit

    return retcode


# XXX provide an option for an unfurl installation can be shared across runtimes.
def _add_unfurl_to_venv(projectdir):
    """
    Set the virtualenv inside `projectdir` to use the unfurl package currently being executed.
    """
    # this should only be used when the current unfurl is installed in editor mode
    # otherwise it will be exposing all packages in the current python's site-packages
    base = os.path.dirname(os.path.dirname(_templatePath))
    sitePackageDir = None
    libDir = os.path.join(projectdir, os.path.join(".venv", "lib"))
    for name in os.listdir(libDir):
        sitePackageDir = os.path.join(libDir, name, "site-packages")
        if os.path.isdir(sitePackageDir):
            break
    else:
        return "Pipenv failed: can't find site-package folder"
    _write_file(sitePackageDir, "unfurl.pth", base)
    _write_file(sitePackageDir, "unfurl.egg-link", base)
    return ""


def pipfile_template_dir(pythonPath):
    from pipenv.utils import python_version

    versionStr = python_version(pythonPath)
    assert versionStr, versionStr
    version = versionStr.rpartition(".")[0]  # 3.8.1 => 3.8
    # version = subprocess.run([pythonPath, "-V"]).stdout.decode()[
    #     7:10
    # ]  # e.g. Python 3.8.1 => 3.8
    return os.path.join(_templatePath, "python" + version)  # e.g. templates/python3.8


def copy_pipfiles(pipfileLocation, projectDir):
    # copy Pipfiles to project root
    if os.path.abspath(projectDir) != os.path.abspath(pipfileLocation):
        for filename in ["Pipfile", "Pipfile.lock"]:
            path = os.path.join(pipfileLocation, filename)
            if os.path.isfile(path):
                shutil.copy(path, projectDir)


def create_venv(projectDir, pipfileLocation, unfurlLocation):
    """Create a virtual python environment for the given project."""

    os.environ["PIPENV_IGNORE_VIRTUALENVS"] = "1"
    VIRTUAL_ENV = os.environ.get("VIRTUAL_ENV")
    os.environ["PIPENV_VENV_IN_PROJECT"] = "1"
    if "PIPENV_PYTHON" not in os.environ:
        os.environ["PIPENV_PYTHON"] = sys.executable

    if pipfileLocation:
        pipfileLocation = os.path.abspath(pipfileLocation)

    try:
        cwd = os.getcwd()
        os.chdir(projectDir)
        # need to set env vars and change current dir before importing pipenv
        from pipenv import environments
        try:
            from pipenv.routines.install import do_install
            kw: Dict = dict(categories=[], extra_pip_args=[])
        except ImportError:
            from pipenv.core import do_install
            kw = dict(extra_index_url=[])

        from pipenv.project import Project as PipEnvProject

        pythonPath = os.environ["PIPENV_PYTHON"]
        assert pythonPath, pythonPath
        if not pipfileLocation:
            pipfileLocation = pipfile_template_dir(pythonPath)
        if not os.path.isdir(pipfileLocation):
            return f'Pipfile location is not a valid directory: "{pipfileLocation}"'
        copy_pipfiles(pipfileLocation, projectDir)

        kw["python"] = pythonPath
        pipenv_project = PipEnvProject()
        # need to run without args first so lock isn't overwritten
        retcode = _run_pip_env(do_install, pipenv_project, kw)
        if retcode:
            return f"Pipenv (step 1) failed: {retcode}"

        # we need to set these so pipenv doesn't try to recreate the virtual environment
        environments.PIPENV_USE_SYSTEM = 1
        environments.PIPENV_IGNORE_VIRTUALENVS = False
        os.environ["VIRTUAL_ENV"] = os.path.join(projectDir, ".venv")
        environments.PIPENV_VIRTUALENV = os.path.join(projectDir, ".venv")

        # we need to set skip_lock or pipenv will not honor the existing lock
        kw["skip_lock"] = True
        if unfurlLocation:
            kw["editable_packages"] = [unfurlLocation]
        else:
            if is_version_unreleased():
                return _add_unfurl_to_venv(projectDir)
            else:
                kw["packages"] = [
                    "unfurl==" + __version__()
                ]  # use the same version as current
        retcode = _run_pip_env(do_install, pipenv_project, kw)
        if retcode:
            return f"Pipenv (step 2) failed: {retcode}"

        return ""
    finally:
        if VIRTUAL_ENV:
            os.environ["VIRTUAL_ENV"] = VIRTUAL_ENV
        else:
            os.environ.pop("VIRTUAL_ENV", None)
        os.chdir(cwd)
