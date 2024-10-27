# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
This module implements creating and cloning project and ensembles as well Unfurl runtimes.
"""

import datetime
import os
import os.path
from typing import Any, Dict, Optional, cast, TYPE_CHECKING
import uuid
import zipfile
from jinja2.loaders import FileSystemLoader
from pathlib import Path

from . import DefaultNames, __version__, get_home_config_path, is_version_unreleased
from .localenv import LocalEnv, Project, LocalConfig
from .repo import (
    GitRepo,
    Repo,
    is_url_or_git_path,
    normalize_git_url,
    split_git_url,
    commit_secrets,
    sanitize_url,
)
from .util import (
    UnfurlError,
    assert_not_none,
    get_base_dir,
    is_relative_to,
    substitute_env,
)
from .tosca_plugins.functions import get_random_password
from .yamlloader import make_yaml, make_vault_lib, yaml
from .venv import init_engine
from .logs import getLogger
from toscaparser.prereq.csar import CSAR, TOSCA_META

if TYPE_CHECKING:
    from . import yamlmanifest
logger = getLogger("unfurl")

_skeleton_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), "skeletons")


def rename_for_backup(dir):
    ctime = datetime.datetime.fromtimestamp(os.stat(dir).st_ctime)
    new = dir + "." + ctime.strftime("%Y-%m-%d-%H-%M-%S")
    os.rename(dir, new)
    return new


def _write_file(folder, filename, content) -> str:
    if not os.path.isdir(folder):
        os.makedirs(os.path.normpath(folder))
    filepath = os.path.join(folder, filename)
    with open(filepath, "w") as f:
        f.write(content)
    return filepath


def write_template(folder, filename, template, vars, templateDir=None) -> str:
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
    if not source:
        return source
    instance = NodeInstance()
    instance._baseDir = _skeleton_path

    overrides = dict(loader=FileSystemLoader(searchPath))
    content = apply_template(source, RefContext(instance, vars), overrides)
    if folder is None:
        return cast(str, content)
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

    skeleton = kw.pop("skeleton", "home")
    homedir, filename = os.path.split(homePath)
    if render:  # just render
        repo = Repo.find_containing_repo(homedir)
        # XXX if repo and update: git stash; git checkout rendered
        ensembleDir = os.path.join(homedir, DefaultNames.EnsembleDirectory)
        ensembleRepo = Repo.find_containing_repo(ensembleDir)
        configPath, ensembleDir, password_vault = render_project(
            homedir, repo, ensembleRepo, None, skeleton
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
        mono=not kw.get("poly"),
        skeleton=skeleton,
        runtime=runtime or "venv:",
        no_runtime=no_runtime,
        msg="Create the unfurl home repository",
        creating_home=True,
        **kw,
    )
    if repo:
        repo.repo.git.branch("rendered")  # now create a branch
    return configPath


def _create_repo(gitDir, ignore=True) -> GitRepo:
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
    specRepo: Optional[GitRepo],
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
    if specRepo:
        vars = dict(specRepoUrl=specRepo.get_url_with_path(specDir, True, revision))
    else:
        vars = {}
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
    projectdir: str,
    repo: Optional[GitRepo],
    ensembleRepo: Optional[GitRepo],
    homePath: Optional[str],
    templateDir: Optional[str] = None,
    names: Any = DefaultNames,
    use_context: Optional[str] = None,
    mono=False,
    use_vault=True,
    skeleton_vars: Optional[Dict[str, Any]] = None,
    ensemble_template=True,
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
        if not is_relative_to(ensembleRepo.working_dir, projectdir):
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
    if use_vault:
        if not vars.get("VAULT_PASSWORD"):
            vars["VAULT_PASSWORD"] = get_random_password()
        vaultpass = vars["VAULT_PASSWORD"]
        if "vaultid" not in vars:
            # use project name plus a couple of random digits to avoid collisions
            vars["vaultid"] = (
                Project.get_name_from_dir(projectdir)
                + get_random_password(2, "", "").upper()
            )
        vaultid = vars["vaultid"]
    else:
        vaultpass = vars["VAULT_PASSWORD"] = ""
        vaultid = ""

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

    if localProjectConfig:
        if use_vault and (not skeleton_vars or not skeleton_vars.get("VAULT_PASSWORD")):
            _warn_about_new_password(localProjectConfig)

        localInclude = "+?include-local: " + os.path.join("local", localConfigFilename)
    else:
        # no local config
        use_vault = False
        localInclude = ""

    if use_vault:
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
    else:
        secretsInclude = ""

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

    if ensemble_template:
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
            extraVars["inputs"] = get_input_vars(skeleton_vars)
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


def _find_or_create_ensemble_repo(projectdir, shared, ensemble_name, kw):
    if shared:
        ensembleRepo = Repo.find_containing_repo(shared)
        if not ensembleRepo:
            raise UnfurlError("can not find shared repository " + shared)
    else:
        ensembleDir = os.path.join(projectdir, ensemble_name)
        ensembleRepo = _create_repo(ensembleDir, not kw.get("submodule"))
        _add_ensemble_specific_unfurl_config(ensembleRepo, kw)
    return ensembleRepo


def _commit_repos(
    projectdir,
    repo: GitRepo,
    ensembleRepo: Optional[GitRepo],
    shared,
    kw,
    ensembleDir,
    runtime,
):
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


def _get_shared(kw, homePath) -> Optional[str]:
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
    creating_home=False,
    ensemble_template=True,
    **kw,
):
    # check both new and old name for option flag
    skeleton = kw.get("skeleton")
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
    if mono and not shared:
        ensembleRepo = repo
    elif empty:
        ensembleRepo = None
    else:
        ensembleRepo = _find_or_create_ensemble_repo(
            projectdir, shared, names.EnsembleDirectory, kw
        )

    logger.info(f"Creating Unfurl project at {projectdir}...")
    if "VAULT_PASSWORD" in skeleton_vars:
        use_vault = True
    elif empty:
        use_vault = False
    else:
        use_vault = kw.get("submodule") or mono
    projectConfigPath, ensembleDir, password_vault = render_project(
        projectdir,
        repo,
        ensembleRepo,
        homePath,
        skeleton,
        names,
        create_context or use_context,
        mono,
        use_vault,
        skeleton_vars,
        ensemble_template,
    )
    if homePath and not creating_home:
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
            ensembleRepo if not mono else None,
            shared,
            kw,
            ensembleDir,
            create_runtime,
        )

    return newHome, projectConfigPath, repo


def get_input_vars(skeleton_vars):
    s = ""
    for i, v in skeleton_vars.items():
        if i.startswith("input_"):
            s += f"{i[len('input_'):]}: {v}\n"
    return s


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


def _add_ensemble_specific_unfurl_config(repo: GitRepo, kw: dict):
    # when creating a separate repo, add a stub unfurl.yaml too
    skeleton_vars = dict((n, v) for n, v in kw.get("var", []))
    use_context = kw.get("use_environment")
    if use_context:
        skeleton_vars["default_context"] = use_context
    skeleton_vars["ensemble_repository_config"] = (
        "# The configuration here is specific to the ensemble.yaml found in the same directory of this file.\n"
        "# The project-level unfurl.yaml configuration can override these and environment-specific configuration should be set there."
    )
    project_config_path, ensembleDir, password_vault = render_project(
        repo.working_dir,
        repo,
        None,
        None,
        kw.get("skeleton"),
        DefaultNames,
        use_context,
        False,
        not kw.get("submodule"),  # if submodule, use parent repo's vault
        skeleton_vars,
        False,
    )
    if password_vault:
        yaml = make_yaml(password_vault)
        commit_secrets(repo.working_dir, yaml, repo)
    repo.add_all(repo.working_dir)


def _create_ensemble_repo(
    manifest: "yamlmanifest.ReadOnlyManifest", repo: Optional[GitRepo], kw: dict
):
    manifest_path = manifest.manifest.path
    assert manifest_path
    destDir = os.path.dirname(manifest_path)
    if not repo:
        repo = _create_repo(destDir)
        _add_ensemble_specific_unfurl_config(repo, kw)
    elif not os.path.isdir(destDir):
        os.makedirs(destDir)

    manifest.metadata["uri"] = repo.get_url_with_path(manifest_path, True)
    with open(manifest_path, "w") as f:
        manifest.dump(f)

    if not kw.get("render"):  # don't commit if --render set
        repo.repo.index.add([manifest_path])
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
    sourceProject: Project,
    sourcePath: str,
    use_environment: Optional[str],
    home_path: Optional[str],
) -> Optional[dict]:
    try:
        overrides = dict(ENVIRONMENT=use_environment)
        localEnv = LocalEnv(sourcePath, home_path, overrides=overrides)
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
    if os.path.isfile(os.path.join(sourcePath, TOSCA_META)):
        service_template = yaml.load(Path(sourcePath) / TOSCA_META)['Entry-Definitions']
        sourceDir = sourceProject.get_relative_path(sourcePath)
        return dict(sourceDir=sourceDir, serviceTemplate=service_template)
    return None


def find_project(source: str, home_path: Optional[str]):
    src_dir = get_base_dir(source)
    sourceRoot = Project.find_path(src_dir)
    if sourceRoot:
        if home_path:
            return Project(sourceRoot, Project(home_path))
        return Project(sourceRoot)
    return None


def _get_context_and_shared_repo(project: Project, options):
    # when creating ensemble, get the default project for the given context if set
    shared_repo = None
    shared = options.get("shared_repository")
    context = options.get("use_environment")
    if not context:
        context = project.get_default_context()
    if not shared and context:
        shared = project.get_default_project_path(context)
    if shared:
        shared_repo = Repo.find_containing_repo(shared)
        if not shared_repo:
            raise UnfurlError("can not find shared repository " + shared)

    return context, shared_repo


class EnsembleBuilder:
    def __init__(self, source: str, ensemble_name: str, options: Dict[str, Any]):
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

        self.template_vars: Optional[Dict[str, Any]] = None  # step 2
        self.environment: Optional[str] = None  # step 2 environment name
        self.shared_repo: Optional[GitRepo] = None  # step 2

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

    def configure(self, remote_source: bool):
        # set self.template_vars for creating a new ensemble
        assert not self.template_vars
        # source is a path into the project relative to the current directory
        assert self.source_path
        source_project = assert_not_none(self.source_project)
        source_path = os.path.join(source_project.projectRoot, self.source_path)
        if not os.path.exists(source_path):
            raise UnfurlError(
                f'Given source "{os.path.abspath(source_path)}" {source_project.projectRoot} ... {self.source_path} does not exist.'
            )
        specific_file = self.source_path != "." and not self.source_path.endswith(
            "unfurl.yaml"
        )
        source_has_ensembles = source_project.has_ensembles()
        if (
            not specific_file
            and not self.options.get("want_init")
            and remote_source  # always create a new ensemble if cloning from self or another local project
            and source_has_ensembles  # we want a new ensemble if source is a blueprint project with no ensembles
        ):
            self.template_vars = {}  # don't create an ensemble
        else:
            template_vars = _find_templates(source_project, source_path)
            if not template_vars:
                # if none are found then try to treat source_path as an ensemble to clone
                template_vars = _from_localenv(
                    source_project,
                    source_path,
                    self.options.get("use_environment"),
                    self.home_path,
                )
                if not template_vars:
                    if specific_file and os.path.isfile(source_path):
                        # assume its an ensemble-template
                        sourceDir, ensembleTemplate = os.path.split(source_path)
                        template_vars = dict(
                            sourceDir=sourceDir, ensembleTemplate=ensembleTemplate
                        )
                    elif self.options.get("want_init"):
                        # can't find anything to do, so raise an error
                        raise UnfurlError(
                            f"Can't find suitable template files or ensemble to clone in source {source_path}"
                        )
                    else:
                        template_vars = {}  # otherwise, don't create an ensemble
            self.template_vars = template_vars
        logger.trace(
            f"creating ensemble from {'remote' if remote_source else 'local'} {'' if source_has_ensembles else 'blueprint '}repo at {source_path} with %s",
            self.template_vars,
        )
        assert self.template_vars is not None

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

    def _get_inputs_template(self) -> str:
        project = assert_not_none(self.source_project)
        local_template = os.path.join(project.projectRoot, DefaultNames.InputsTemplate)
        if os.path.isfile(local_template):
            with open(local_template) as f:
                return f.read()
        else:
            return get_input_vars(self.skeleton_vars)

    def _create_ensemble_from_template(self, project: Project, destDir, manifestName):
        from unfurl import yamlmanifest

        specProject = assert_not_none(self.dest_project)
        source_project = assert_not_none(self.source_project)
        assert self.template_vars is not None
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

    def create_new_ensemble(self) -> str:
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
        repo = self.shared_repo or self.mono and dest_project.project_repoview.repo
        _create_ensemble_repo(
            manifest,
            repo,
            self.options,
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
        elif repo:  # register if added in the same repo
            destProject.register_ensemble(manifest_path, context=self.environment)
        if not self.options.get("render") and destProject.localConfig.config.saved:
            msg = f"Add ensemble at {destProject.get_relative_path(manifest_path)}"
            assert_not_none(destProject.project_repoview.repo).commit_files(
                [assert_not_none(destProject.localConfig.config.path)], msg
            )
        self.manifest = manifest
        return destDir

    def clone_local_project(
        self, currentProject: Optional[Project], sourceProject: Project, dest_dir: str
    ) -> Project:
        self.source_path = sourceProject.get_relative_path(self.input_source)
        assert self.source_path
        assert not self.source_path.startswith(
            ".."
        ), f"{self.source_path} should be inside the project"
        if not sourceProject.project_repoview.repo:
            raise UnfurlError(
                f"Only local projects with a git repository can be cloned."
            )
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
        if not self.source_project:
            raise UnfurlError(
                f"project not found in {search}, cloned to {newrepo.working_dir}"
            )
        return self.source_project

    def resolve_input_source(self, current_project) -> str:
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

    def clone_remote_project(
        self, currentProject: Optional[Project], destDir: str
    ) -> Project:
        # check if source is a git url
        repoURL, filePath, revision = split_git_url(self.input_source)
        self.source_revision = revision
        repoURL = normalize_git_url(repoURL)
        if currentProject:
            # XXX use currentProject.get_relative_path(destDir) as clone destination
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

    def _needs_local_config(self, clonedProject: Project) -> bool:
        return bool(self.skeleton_vars) and not os.path.exists(
            os.path.join(clonedProject.projectRoot, "local", DefaultNames.LocalConfig)
        )

    def set_dest_project_and_path(
        self,
        existingSourceProject: Optional[Project],
        existingDestProject: Optional[Project],
        dest: str,
    ) -> None:
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

        assert self.dest_project
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
        (self.environment, self.shared_repo) = _get_context_and_shared_repo(
            self.dest_project, self.options
        )
        if (
            existingDestProject
            and not self.shared_repo
            and (self.options.get("empty") or self.mono)
        ):
            # ensemble will be created in the same repo as existingDestProject, add environment too
            self.add_missing_environment(existingDestProject)

    def has_existing_ensemble(self, sourceProject: Optional[Project]) -> bool:
        if self.source_project is not sourceProject and not self.shared_repo:
            assert self.template_vars is not None
            if "localEnv" in self.template_vars and os.path.exists(
                Path(assert_not_none(self.dest_project).projectRoot)
                / assert_not_none(self.dest_path)
            ):
                # the ensemble is already part of the source project repository or a submodule
                return True
        return False

    def set_source(self, sourceProject: Project) -> None:
        self.source_project = sourceProject
        # make source relative to the source project
        source_path = sourceProject.get_relative_path(self.input_source)
        assert not source_path.startswith("..")
        self.source_path = source_path

    def add_missing_environment(self, existing_project: Project) -> bool:
        kw = self.options
        use_context = cast(Optional[str], kw.get("use_environment"))
        if use_context and use_context not in existing_project.contexts:
            skeleton_vars = dict((n, v) for n, v in kw.get("var", []))
            skeleton_vars["default_context"] = use_context
            content = write_project_config(
                None,
                "",
                "unfurl.yaml.j2",
                skeleton_vars,
                kw.get("skeleton"),
            )
            existing_project.add_context(
                use_context, yaml.load(content)["environments"][use_context]
            )
            logger.info(
                f"Added new environment {use_context} to project {existing_project.projectRoot}"
            )
            if not self.mono:  # save now, not gonna be saved later
                existing_project.localConfig.config.save()
            return True
        return False

    def set_ensemble(
        self,
        remote_source: bool,
        existingSourceProject: Optional[Project],
    ) -> str:
        sourceWasCloned = self.source_project is not existingSourceProject
        assert self.dest_project
        assert self.source_project
        if not self.template_vars:
            if self.source_project.has_ensembles():
                # XXX if not destIsNew and use_environment warn: that setting is ignored with existing deployments
                return (
                    "Cloned project with a pre-existing ensemble(s) to "
                    + self.dest_project.projectRoot
                )
            else:
                # source wasn't pointing to an ensemble to clone
                return "Cloned empty project to " + self.dest_project.projectRoot

        if self.options.get("use_deployment_blueprint"):
            self.template_vars["deployment_blueprint"] = self.options[
                "use_deployment_blueprint"
            ]
        destDir = self.create_new_ensemble()
        if not remote_source and sourceWasCloned and existingSourceProject:
            # we need to clone the referenced local repos so the new project has access to them
            clone_local_repos(
                self.manifest,
                existingSourceProject,
                assert_not_none(self.dest_project),
            )
        return f'Created new ensemble at "{os.path.abspath(destDir)}"'

    def load_ensemble_template(self):
        if not self.template_vars or not self.template_vars.get("ensembleTemplate"):
            raise UnfurlError(
                "Clone with --design not supported: no ensemble template found."
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

    def clone_from_csar(
        self, source: str, currentProject: Optional[Project], dest: str
    ) -> Optional[str]:
        if not os.path.isfile(source) or not zipfile.is_zipfile(source):
            return None
        if currentProject:
            dest = os.path.abspath(dest) # needs to be absolute for creating ensemble later
            if dest == currentProject.projectRoot:
                # don't uncompress into root of existing project
                dest = os.path.join(dest, os.path.splitext(os.path.basename(source))[0])
        if (
            not self.options.get("overwrite")
            and os.path.exists(dest)
            and os.listdir(dest)
        ):
            raise UnfurlError(
                f'Can not decompress TOSCA CSAR into "{dest}": folder is not empty'
            )

        csar = CSAR(source, True, dest)
        assert csar.unzip_dir
        logger.info(
            f'Decompressing TOSCA CSAR "{source}" into "{os.path.abspath(csar.unzip_dir)}"'
        )
        csar.validate()  # this will decompress and raise error if validation fails
        if currentProject:
            # set overwrite so we create the ensemble in the same directory as the CSAR
            self.options["overwrite"] = True
            self.source_project = currentProject
            repo = currentProject.project_repoview.repo
            if repo and (self.mono or self.options.get("empty")):
                repo.add_all(csar.unzip_dir)
                repo.repo.index.commit(
                    f"Adding TOSCA CSAR {os.path.basename(source)} to project."
                )
        else:
            options = self.options.copy()
            options.pop("empty", None)
            homePath, projectPath, repo = create_project(
                os.path.abspath(dest), empty=True, ensemble_template=False, **options
            )
            self.source_project = find_project(projectPath, self.home_path)
        assert self.source_project

        assert csar.unzip_dir
        assert csar.main_template_file_name
        service_template = self.source_project.get_relative_path(
            os.path.join(csar.unzip_dir, csar.main_template_file_name)
        )
        # set template_vars to create ensemble that includes the service template
        self.template_vars = dict(sourceDir=".", serviceTemplate=service_template)
        return dest


def clone(
    source: str,
    dest: str,
    ensemble_name: str = DefaultNames.EnsembleDirectory,
    **options: Any,
) -> str:
    """
    Clone the ``source`` project or ensemble to ``dest``. If ``dest`` isn't in a project, create a new one.

    ``source`` can be a git URL or a path inside a local git repository.
    Git URLs can specify a particular file in the repository using an URL fragment like ``#<branch_or_tag>:<file/path>``.
    You can use cloudmap url like ``cloudmap:<package_id>``, which will resolve to a git URL.

    If ``source`` can point to an Unfurl project, an ensemble template, a service template, an existing ensemble, or a folder containing one of those.

    The result of the clone depends on the destination:

    ======================= ===============================================
    ``dest``                Result
    ======================= ===============================================
    Inside source project   New or forked ensemble (depending on source)
    Missing or empty folder Clone project, create new ensemble if missing
    Another project         See below
    Non-empty folder        Error, abort
    ======================= ===============================================

    When creating a new ensemble from a source, if the source points to:

    * an ensemble: fork the ensemble (clone without status and new uri)
    * an ensemble template or TOSCA service template: a create new ensemble from the template.
    * a project: If the project includes a ensemble-template.yaml, use that; if missing, fork the project's default ensemble.

    When dest is set to another project, clone's behavior depends on source:

    If the source is a local file path, the project and local repository is registered in the destination project and a new ensemble is created based on the source.

    If the source is a git URL, the repository is cloned inside the destination project. A new ensemble is only created if the source specified a specific ensemble or template or if the source was blueprint project (i.e. it contains an ensemble template but doesn't contain any ensembles).

    When deploying an ensemble that is in project that was cloned into another project, the environment setting in each unfurl.yaml are merged, with the top-level project's settings taking precedence.
    """
    builder = EnsembleBuilder(source, ensemble_name, options)
    if not dest:
        dest = Repo.get_path_for_git_repo(source)  # choose dest based on source url
        if os.path.exists(dest):
            raise UnfurlError(
                'Can not clone to "' + dest + '": file already exists with that name'
            )
    currentProject = find_project(dest, builder.home_path)
    if currentProject:
        logger.trace(f"Cloning into project at {currentProject.projectRoot}")
    source = builder.resolve_input_source(currentProject)

    ### step 1: clone the source repository and set the the source path
    sourceProject: Optional[Project] = None
    isRemote = is_url_or_git_path(source)
    from_csar = False
    if isRemote:
        builder.clone_remote_project(currentProject, dest)
    elif (
        csar_dest := builder.clone_from_csar(source, currentProject, dest)
    ) is not None:
        dest = csar_dest
        from_csar = True
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

    ##### step 2: create destination project if necessary and determine shared project
    builder.set_dest_project_and_path(sourceProject, currentProject, dest)
    if options.get("empty") and not options.get("design"):
        # don't create an ensemble, so we're done
        project_root = assert_not_none(builder.dest_project).projectRoot
        if from_csar:
            if currentProject:
                return f"Cloned TOSCA CSAR to {dest}"
            else:
                return "Created project from TOSCA CSAR at " + project_root
        if builder.source_project is sourceProject:
            return "Project already exists."
        return "Cloned project to " + project_root

    ##### step 3: examine source for template details
    if not builder.template_vars:
        builder.configure(isRemote or from_csar)

    if options.get("design"):
        # don't create an ensemble but fully instantiate the ensemble-template
        # to prepare the project for development
        return builder.load_ensemble_template()

    ##### step 4 create ensemble in destination project if needed
    return builder.set_ensemble(isRemote or from_csar, sourceProject)


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
