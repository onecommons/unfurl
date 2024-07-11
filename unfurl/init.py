# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
This module implements creating and cloning project and ensembles as well Unfurl runtimes.
"""

import datetime
import os
import os.path
from typing import Any, Dict, Optional, cast
import uuid
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
from .util import UnfurlError, assert_not_none, substitute_env
from .tosca_plugins.functions import get_random_password
from .yamlloader import make_yaml, make_vault_lib
from .venv import init_engine
from .logs import getLogger

logger = getLogger("unfurl")

_skeleton_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), "skeletons")


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
        templateDir = os.path.join(_skeleton_path, templateDir)

    if templateDir:
        searchPath = [templateDir, _skeleton_path]
    else:
        searchPath = [_skeleton_path]

    if not templateDir or not os.path.exists(os.path.join(templateDir, template)):
        # use default file if missing from templateDir
        templateDir = _skeleton_path

    with open(os.path.join(templateDir, template)) as f:
        source = f.read()
    instance = NodeInstance()
    instance._baseDir = _skeleton_path

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
        else:
            logger.info(f"Missing Unfurl home project at {homedir}, creating it now...")

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


def write_service_template(projectdir, templateDir=None):
    return write_template(
        projectdir, "service_template.py", "service_template.py.j2", {}, templateDir
    )


def write_ensemble_manifest(
    destDir: str,
    manifestName: str,
    specRepo: GitRepo,
    specDir=None,
    extraVars=None,
    templateDir=None,
):
    if specDir:
        specDir = os.path.abspath(specDir)
    else:
        specDir = ""
    if extraVars:
        revision = extraVars.get("revision") or ""
    else:
        revision = ""
    vars = dict(specRepoUrl=specRepo.get_url_with_path(specDir, True, revision))
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
    logger.warning(
        "A password was generated and included in the local config file at %s -- "
        "please keep this password safe, you will need it to decrypt any encrypted files "
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
    # write service_template.py
    write_service_template(projectdir, templateDir)

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


def _commit_repos(projectdir, repo, ensembleRepo, shared, kw, ensembleDir, runtime):
    if ensembleRepo:
        ensembleRepo.add_all(ensembleDir)
        if shared:
            message = "Adding ensemble"
        else:
            message = "Default ensemble repository boilerplate"
        ensembleRepo.repo.index.commit(message)

    if kw.get("submodule"):
        repo.add_sub_module(ensembleDir)

    if runtime:
        try:
            error_message = init_engine(projectdir, runtime)
            if error_message:
                logger.error(
                    "Unable to create Unfurl runtime %s: %s", runtime, error_message
                )
        except Exception:
            logger.error("Unable to create Unfurl runtime %s", runtime, exc_info=True)

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
    # check both new and old name for option flag
    create_context = kw.get("as_shared_environment") or kw.get("create_environment")
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

    logger.info(f"Creating Unfurl project at {projectdir}...")
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

        # if runtime was explicitly set and we didn't already create it in the home project
        # then create the runtime in this project
        create_runtime = not newHome and not kw.get("no_runtime") and kw.get("runtime")
        _commit_repos(
            projectdir,
            repo,
            not mono and ensembleRepo,
            shared,
            kw,
            ensembleDir,
            create_runtime,
        )

    return newHome, projectConfigPath, repo


def clone_local_repos(manifest, sourceProject: Project, targetProject: Project):
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



def _from_localenv(
    sourceProject: Project, sourcePath: str, use_environment: Optional[str]
) -> Optional[dict]:
    try:
        overrides = dict(ENVIRONMENT=use_environment, format="blueprint")
        localEnv = LocalEnv(sourcePath, project=sourceProject, overrides=overrides)
        assert localEnv.manifestPath
        sourceDir = sourceProject.get_relative_path(
            os.path.dirname(localEnv.manifestPath)
        )
        # note: if sourceDir.startswith("..") then ensemble lives in another's project's repo
        return dict(sourceDir=sourceDir, localEnv=localEnv)
    except UnfurlError:
        # XXX if UnfurlError is "could not find external project", reraise
        # sourcePath wasn't a project or ensemble
        logger.trace(f"Init could not load {sourcePath}", exc_info=1)
        return None


def _find_templates(sourceProject: Project, sourcePath: str):
    # look for an ensemble-template or service_template in source path
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
    return None


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

        self.source_project: Optional[Project] = None  # step 1
        self.source_path: Optional[str] = None  # step 1 relative path in source_project
        self.source_revision: Optional[str] = None  # step 1

        self.template_vars: Any = None  # step 2
        self.environment: Any = None  # step 2 environment name
        self.shared_repo: Any = None  # step 2

        self.dest_project: Optional[Project] = None  # step 3
        self.dest_path: Optional[str] = None  # step 3 relative path in dest_project

        self.manifest = None  # final step
        self.logger = logger

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
        assert not self.template_vars

        # source is a path into the project relative to the current directory
        assert self.source_path
        source_project = assert_not_none(self.source_project)
        source_path = os.path.join(source_project.projectRoot, self.source_path)
        if not os.path.exists(source_path):
            raise UnfurlError(
                f'Given source "{os.path.abspath(source_path)}" does not exist.'
            )
        if self.options.get("want_init"):  # if creating a new project or ensemble
            #  if source is a directory look for well-known template files, otherwise
            template_vars = _find_templates(source_project, source_path)
            if not template_vars:
                # if none are found then try to treat as an ensemble to clone
                template_vars = _from_localenv(
                    source_project, source_path, self.options.get("use_environment")
                )
        else:  # clone an ensemble or template
            template_vars = None
            if not source_path.endswith(DefaultNames.ServiceTemplate):
                template_vars = _from_localenv(
                    source_project, source_path, self.options.get("use_environment")
                )
            if not template_vars:
                # failed to load, look for well-known template files
                template_vars = _find_templates(source_project, source_path)
        if not template_vars:
            raise UnfurlError(f"Can't find suitable template files or ensemble from source: {source_path}")
        self.template_vars = template_vars
        if self.options.get("use_deployment_blueprint"):
            self.template_vars["deployment_blueprint"] = self.options[
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
        project = assert_not_none(self.source_project)
        local_template = os.path.join(project.projectRoot, DefaultNames.InputsTemplate)
        if os.path.isfile(local_template):
            with open(local_template) as s:
                return s.read()
        return None

    def _create_ensemble_from_template(self, project: Project, destDir, manifestName):
        from unfurl import yamlmanifest

        specProject = assert_not_none(self.dest_project)
        source_project = assert_not_none(self.source_project)
        sourceDir = os.path.normpath(
            os.path.join(source_project.projectRoot, self.template_vars["sourceDir"])
        )
        spec_repo_view, relPath, bare = specProject.find_path_in_repos(sourceDir)
        if not spec_repo_view or not spec_repo_view.repo:
            raise UnfurlError(
                '"%s" is not in a git repository. Cloning from plain file directories not yet supported'
                % os.path.abspath(sourceDir)
            )
        self.template_vars["revision"] = self.source_revision or ""
        manifestPath = write_ensemble_manifest(
            os.path.join(project.projectRoot, destDir),
            manifestName,
            spec_repo_view.repo,
            sourceDir,
            self.template_vars,
            self.options.get("skeleton"),
        )
        localEnv = LocalEnv(
            manifestPath,
            project=project,
            override_context=self.options.get("use_environment"),
            parent=self.options.get("parent_localenv"),
        )
        manifest = yamlmanifest.ReadOnlyManifest(
            localEnv=localEnv, vault=localEnv.get_vault()
        )
        return localEnv, manifest

    def create_new_ensemble(self):
        """
        If "localEnv" is in template_vars, clone that ensemble;
        otherwise create one from a template with template_vars
        """
        from unfurl import yamlmanifest

        if self.shared_repo:
            destProject: Project = assert_not_none(
                find_project(self.shared_repo.working_dir, self.home_path)
            )
        else:
            destProject = assert_not_none(self.dest_project)

        assert self.template_vars
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

        template_vars = self.template_vars
        if "localEnv" not in template_vars:
            # we found a template file to clone
            template_vars["inputs"] = self._get_inputs_template()
            localEnv, manifest = self._create_ensemble_from_template(
                destProject,
                destDir,
                manifestName,
            )
        else:
            # look for an ensemble at the given path or use the source project's default
            localEnv = template_vars["localEnv"]
            manifest = yamlmanifest.clone(localEnv, targetPath)

        dest_project = assert_not_none(self.dest_project)
        _create_ensemble_repo(
            manifest,
            self.shared_repo or self.mono and dest_project.project_repoview.repo,
            not self.options.get("render"),
        )

        manifest_path: str = assert_not_none(manifest.path)
        if destProject.projectRoot != dest_project.projectRoot:
            # cross reference each other
            destProject.register_ensemble(
                manifest_path, managedBy=self.dest_project, context=self.environment
            )
            dest_project.register_ensemble(
                manifest_path, project=destProject, context=self.environment
            )
        else:
            destProject.register_ensemble(manifest_path, context=self.environment)
        assert_not_none(destProject.project_repoview.repo).commit_files(
            [assert_not_none(destProject.localConfig.config.path)], "Add ensemble"
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

    def resolve_input_source(self, current_project):
        if self.input_source.startswith("cloudmap:"):
            from .cloudmap import CloudMap

            local_env = LocalEnv(
                can_be_empty=True,
                homePath=self.home_path,
                project=current_project,
                override_context=self.options.get("use_environment"),
            )
            cloudmap = CloudMap.get_db(local_env)
            repo_key = self.input_source[len("cloudmap:") :]
            repo_record = cloudmap.repositories.get(repo_key)
            if repo_record:
                self.input_source = repo_record.git_url()
            else:
                raise UnfurlError(f"Could not find {repo_key} in the cloudmap.")
        return self.input_source

    def clone_remote_project(self, currentProject, destDir):
        # check if source is a git url
        repoURL, filePath, revision = split_git_url(self.input_source)
        self.source_revision = revision
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
            relDestDir = assert_not_none(self.dest_project).get_relative_path(dest)
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
            if "localEnv" in self.template_vars and os.path.exists(
                Path(assert_not_none(self.dest_project).projectRoot)
                / assert_not_none(self.dest_path)
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

        if not self.template_vars:
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
        if not isRemote and existingSourceProject is not self.source_project:
            # we need to clone the referenced local repos so the new project has access to them
            clone_local_repos(
                self.manifest,
                existingSourceProject,
                assert_not_none(self.source_project),
            )
        return f'Created new ensemble at "{os.path.abspath(destDir)}"'

    def load_ensemble_template(self):
        if not self.template_vars or not self.template_vars.get("ensembleTemplate"):
            raise UnfurlError(
                f"Clone with --design not supported: no ensemble template found."
            )
        assert isinstance(self.template_vars["sourceDir"], str)
        sourceDir = os.path.normpath(
            os.path.join(
                assert_not_none(self.source_project).projectRoot,
                self.template_vars["sourceDir"],
            )
        )
        # assert ensemble_template_path in dest_project
        assert self.dest_project and Path(sourceDir).relative_to(
            self.dest_project.projectRoot
        )
        ensemble_template_path = os.path.join(
            sourceDir, cast(str, self.template_vars["ensembleTemplate"])
        )
        # load the template, cloning referenced repositories as needed
        LocalEnv(ensemble_template_path).get_manifest()
        return "Cloned blueprint to " + ensemble_template_path


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
    source = builder.resolve_input_source(currentProject)

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

    ##### step 2: create destination project if necessary
    builder.set_dest_project_and_path(sourceProject, currentProject, dest)
    if options.get("empty") and not options.get("design"):
        # don't create an ensemble
        return "Cloned project to " + assert_not_none(builder.dest_project).projectRoot

    ##### step 3: examine source for template details and determine shared project
    builder.configure()

    if options.get("design"):
        # don't create an ensemble but fully instantiate the ensemble-template
        # to prepare the project for development
        return builder.load_ensemble_template()

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
