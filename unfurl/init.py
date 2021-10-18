# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
This module implements creating and cloning project and ensembles as well Unfurl runtimes.
"""
import datetime
import os
import os.path
import random
import shutil
import string
import sys
import uuid
import logging
from jinja2.loaders import FileSystemLoader

from . import DefaultNames, __version__, get_home_config_path
from .localenv import LocalEnv, Project, LocalConfig
from .repo import GitRepo, Repo, is_url_or_git_path, split_git_url, commit_secrets
from .util import UnfurlError
from .yamlloader import make_yaml, make_vault_lib

_templatePath = os.path.join(os.path.abspath(os.path.dirname(__file__)), "templates")


def rename_for_backup(dir):
    ctime = datetime.datetime.fromtimestamp(os.stat(dir).st_ctime)
    new = dir + "." + ctime.strftime("%Y-%m-%d-%H-%M-%S")
    os.rename(dir, new)
    return new


def get_random_password(count=12, prefix="uv"):
    srandom = random.SystemRandom()
    start = string.ascii_letters + string.digits
    source = string.ascii_letters + string.digits + "%&()*+,-./:<>?=@^_`~"
    return prefix + "".join(
        srandom.choice(source if i else start) for i in range(count)
    )


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
        configPath, ensembleDir, password = render_project(
            homedir, repo, ensembleRepo, None, "home"
        )
        # XXX if repo and update: git commit -m"updated"; git checkout master; git stash pop
        return configPath
    else:
        if exists:
            rename_for_backup(homedir)

    newHome, configPath, repo = create_project(
        homedir,
        template="home",
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
    repo.index.commit(f"Initial Commit for {uuid.uuid1()}")

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
    destDir, manifestName, specRepo, specDir=None, extraVars=None, templateDir=None
):
    if specDir:
        specDir = os.path.abspath(specDir)
    else:
        specDir = ""
    vars = dict(specRepoUrl=specRepo.get_url_with_path(specDir))
    if extraVars:
        vars.update(extraVars)
    return write_template(destDir, manifestName, "manifest.yaml.j2", vars, templateDir)


def add_hidden_git_files(gitDir):
    # write .gitignore and  .gitattributes
    gitIgnorePath = write_template(gitDir, ".gitignore", "gitignore.j2", {})
    gitAttributesContent = f"**/*{DefaultNames.JobsLog} merge=union\n"
    gitAttributesPath = _write_file(gitDir, ".gitattributes", gitAttributesContent)
    return [os.path.abspath(gitIgnorePath), os.path.abspath(gitAttributesPath)]


def _set_ensemble_vars(vars, externalProject, ensemblePath, context):
    if externalProject:
        vars["manifestPath"] = externalProject.get_relative_path(ensemblePath)
        vars["external"] = externalProject.name
    vars["context"] = context


def render_project(
    projectdir,
    repo,
    ensembleRepo,
    homePath,
    templateDir=None,
    names=DefaultNames,
    use_context=None,
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
            relPath = externalProject.get_relative_path(
                os.path.join(ensembleRepo.working_dir, os.path.basename(projectdir))
            )
            ensembleDir = externalProject.get_unique_path(relPath)
        manifestName = names.Ensemble
        ensemblePath = os.path.join(ensembleDir, manifestName)

    vaultpass = get_random_password()
    # XXX vaultid should match the project name so we can see which password was used to encrypt a file
    vars = dict(vaultpass=vaultpass, vaultid="default")
    if ensembleRepo and ensembleRepo.is_local_only():
        _set_ensemble_vars(vars, externalProject, ensemblePath, use_context)
    localProjectConfig = write_project_config(
        os.path.join(projectdir, "local"),
        localConfigFilename,
        "unfurl.local.yaml.j2",
        vars,
        templateDir,
    )
    logger = logging.getLogger("unfurl")
    logger.warning(
        "A password was generated and included in the local config file at %s -- please keep this password safe, without it you will not be able to decrypt any encrypt files committed to the repository.",
        localProjectConfig,
    )

    write_project_config(
        os.path.join(projectdir, "secrets"),
        names.SecretsConfig,
        "secrets.yaml.j2",
        vars,
        templateDir,
    )

    localInclude = "+?include-local: " + os.path.join("local", localConfigFilename)
    secretsInclude = "+?include-secrets: " + os.path.join(
        "secrets", names.SecretsConfig
    )

    # note: local overrides secrets
    vars = dict(include=secretsInclude + "\n" + localInclude)
    if use_context:
        # since this is specified while creating the project set this as the default context
        vars["default_context"] = use_context
    if ensembleRepo and not ensembleRepo.is_local_only():
        _set_ensemble_vars(vars, externalProject, ensemblePath, use_context)
    projectConfigPath = write_project_config(
        projectdir,
        names.LocalConfig,
        "unfurl.yaml.j2",
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
            ensembleUri=ensembleRepo.get_url_with_path(ensemblePath),
            # include the ensembleTemplate in the root of the specDir
            ensembleTemplate=names.EnsembleTemplate,
        )
        # write ensemble/ensemble.yaml
        write_ensemble_manifest(
            ensembleDir,
            manifestName,
            repo,
            extraVars=extraVars,
            templateDir=templateDir,
        )
    if externalProject:
        # add the external project to the project and localRepositories configuration sections
        LocalConfig(projectConfigPath).register_project(externalProject)
        externalProject.register_ensemble(
            ensemblePath, managedBy=find_project(projectdir, homePath)
        )
    return projectConfigPath, ensembleDir, vaultpass


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
    template=None,
    creating_home=False,
    **kw,
):
    create_context = kw.get("create_environment")
    use_context = kw.get("use_environment")
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
    if mono:
        ensembleRepo = repo
    else:
        ensembleRepo = _find_ensemble_repo(
            projectdir, shared, submodule, names.EnsembleDirectory
        )

    projectConfigPath, ensembleDir, password = render_project(
        projectdir,
        repo,
        not empty and ensembleRepo,
        homePath,
        template,
        names,
        create_context or use_context,
    )
    if homePath and create_context:
        newProject = find_project(projectConfigPath, homePath)
        assert newProject
        homeProject = newProject.parentProject
        assert homeProject
        homeProject.localConfig.register_project(newProject, create_context)

    if password:
        yaml = make_yaml(make_vault_lib(password, "default"))
        commit_secrets(os.path.dirname(projectConfigPath), yaml)

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
        repo = sourceProject.find_git_repo_from_repository(repoSpec)
        if repo:
            targetProject.find_or_clone(repo)


def _create_ensemble_repo(manifest, repo):
    destDir = os.path.dirname(manifest.manifest.path)
    if not repo:
        repo = _create_repo(destDir)
    elif not os.path.isdir(destDir):
        os.makedirs(destDir)

    manifest.metadata["uri"] = repo.get_url_with_path(manifest.manifest.path)
    with open(manifest.manifest.path, "w") as f:
        manifest.dump(f)

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


def _get_ensemble_paths(sourcePath, sourceProject):
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
    isServiceTemplate = sourcePath.endswith(DefaultNames.ServiceTemplate)
    if not isServiceTemplate:
        # we only support cloning TOSCA service templates if their names end in "service-template.yaml"
        try:
            localEnv = LocalEnv(relPath, project=sourceProject)
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
    if isServiceTemplate or os.path.isdir(sourcePath):
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


def _create_ensemble_from_template(templateVars, project, destDir, manifestName):
    from unfurl import yamlmanifest

    assert project
    sourceDir = os.path.normpath(
        os.path.join(project.projectRoot, templateVars["sourceDir"])
    )
    specRepo, relPath, revision, bare = project.find_path_in_repos(sourceDir)
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
        templateVars,
    )
    localEnv = LocalEnv(manifestPath, project=project)
    manifest = yamlmanifest.ReadOnlyManifest(localEnv=localEnv)
    return localEnv, manifest


def create_new_ensemble(
    templateVars, project, targetPath, mono, context, shared_repo, homePath
):
    """
    If "localEnv" is in templateVars, clone that ensemble;
    otherwise create one from a template with templateVars
    """
    # targetPath is relative to the project root
    from unfurl import yamlmanifest

    assert not os.path.isabs(targetPath)
    if not targetPath or targetPath == ".":
        destDir, manifestName = DefaultNames.EnsembleDirectory, DefaultNames.Ensemble
    elif targetPath.endswith(".yaml") or targetPath.endswith(".yml"):
        destDir, manifestName = os.path.split(targetPath)
    else:
        destDir = targetPath
        manifestName = DefaultNames.Ensemble

    if shared_repo:
        destProject = find_project(shared_repo.working_dir, homePath)
        assert destProject
    else:
        destProject = project

    # choose a destDir that doesn't conflict with an existing folder
    # (i.e. if default ensemble already exists)
    destDir = destProject.get_unique_path(destDir)
    # destDir is now absolute
    targetPath = os.path.normpath(os.path.join(destDir, manifestName))

    if not templateVars:
        raise UnfurlError("can't find anything to clone")
    if "localEnv" not in templateVars:
        # we found a template file to clone
        localEnv, manifest = _create_ensemble_from_template(
            templateVars, destProject, destDir, manifestName
        )
    else:
        # didn't find a template file
        # look for an ensemble at the given path or use the source project's default
        localEnv = templateVars["localEnv"]
        manifest = yamlmanifest.clone(localEnv, targetPath)

    _create_ensemble_repo(manifest, shared_repo or mono and project.projectRepo)
    if destProject.projectRoot != project.projectRoot:
        # cross reference each other
        destProject.register_ensemble(manifest.path, managedBy=project, context=context)
        project.register_ensemble(manifest.path, project=destProject, context=context)
    else:
        project.register_ensemble(manifest.path, context=context)
    return destDir, manifest


def clone_remote_project(source, destDir, homePath):
    # check if source is a git url
    repoURL, filePath, revision = split_git_url(source)
    # not yet supported: add repo to local project
    # destRoot = Project.findPath(destDir)
    # if destRoot:
    #     # destination is in an existing project, use that one
    #     sourceProject = Project(destRoot)
    #     repo = sourceProject.findGitRepo(repoURL, revision)
    #     if not repo:
    #         repo = sourceProject.createWorkingDir(repoURL, revision)
    #     source = os.path.join(repo.workingDir, filePath)
    # else: # otherwise clone to dest
    if os.path.exists(destDir) and os.listdir(destDir):
        raise UnfurlError(
            f'Can not clone project into "{destDir}": folder is not empty'
        )
    Repo.create_working_dir(repoURL, destDir, revision)  # clone source repo
    targetDir = os.path.join(destDir, filePath)
    sourceRoot = Project.find_path(targetDir)
    if not sourceRoot:
        raise UnfurlError(
            f'Error: cloned "{source}" to "{destDir}" but couldn\'t find an Unfurl project'
        )
    sourceProject = find_project(sourceRoot, homePath)
    return sourceProject, targetDir


def find_project(source, home_path):
    sourceRoot = Project.find_path(source)
    if sourceRoot:
        if home_path:
            return Project(sourceRoot, Project(home_path))
        return Project(sourceRoot)
    return None


def is_ensemble_in_project_repo(project, paths):
    # check if source points to an ensemble that is part of the project repo
    if not project.projectRepo or "localEnv" not in paths:
        return False
    sourceDir = paths["sourceDir"]
    assert not os.path.isabs(sourceDir)
    pathToEnsemble = os.path.join(project.projectRoot, sourceDir)
    if not os.path.isdir(pathToEnsemble):
        return False
    if project.projectRepo.is_path_excluded(sourceDir):
        return False
    return True


def _get_context_and_shared_repo(project, options):
    # when creating ensemble, get the default project for the given context if set
    # XXX if not --new-repository
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


def clone(source, dest, ensemble_name=DefaultNames.EnsembleDirectory, **options):
    """
    Clone the ``source`` ensemble to ``dest``. If ``dest`` isn't in a project, create one.
    ``source`` can point to an ensemble_template, a service_template, an existing ensemble
    or a folder containing one of those. If it points to a project its default ensemble will be cloned.

    Referenced `repositories` will be cloned if a git repository or copied if a regular file folder,
    If the folders already exist they will be copied to new folder unless the git repositories have the same HEAD.
    but the local repository names will remain the same.

    ================ =============================================
    dest             result
    ================ =============================================
    Inside project   new ensemble
    new or empty dir clone or create project (depending on source)
    another project  error (not yet supported)
    other            error
    ================ =============================================

    """
    if not dest:
        dest = Repo.get_path_for_git_repo(source)  # choose dest based on source url
    # XXX else: # we're assuming dest is directory, handle case where filename is included
    mono = options.get("mono") or options.get("existing")
    homePath = get_home_config_path(options.get("home"))
    isRemote = is_url_or_git_path(source)
    if isRemote:
        if options.get("shared_repository"):
            raise UnfurlError(
                "shared_repository option not yet supported when cloning remote repositories"
            )

        clonedProject, source = clone_remote_project(source, dest, homePath)
        # source is now a path inside the cloned project
        templateVars = _get_ensemble_paths(source, clonedProject)
        if not templateVars:
            raise UnfurlError(f'Can\'t find anything to clone in "{source}"')
        context, shared_repo = _get_context_and_shared_repo(clonedProject, options)
    else:
        sourceProject = find_project(source, homePath)
        sourceNotInProject = not sourceProject
        if sourceNotInProject:
            # source wasn't in a project
            raise UnfurlError(
                f"Can't clone \"{source}\": it isn't in an Unfurl project or repository"
            )
            # XXX create a new project from scratch for the ensemble
            # if os.path.exists(dest) and os.listdir(dest):
            #     raise UnfurlError(
            #         'Can not create a project in "%s": folder is not empty' % dest
            #     )
            # newHome, projectConfigPath, repo = createProject(
            #     dest, emtpy=True, **options
            # )
            # sourceProject = Project(projectConfigPath)

        context, shared_repo = _get_context_and_shared_repo(sourceProject, options)
        relDestDir = sourceProject.get_relative_path(dest)
        templateVars = _get_ensemble_paths(source, sourceProject)
        if not relDestDir.startswith(".."):
            # dest is in the source project (or its a new project)
            # so don't need to clone, just need to create an ensemble
            if not relDestDir or relDestDir == ".":
                relDestDir = ensemble_name
            destDir, manifest = create_new_ensemble(
                templateVars,
                sourceProject,
                relDestDir,
                mono,
                context,
                shared_repo,
                homePath,
            )
            return 'Created ensemble in %s project: "%s"' % (
                "new" if sourceNotInProject else "existing",
                os.path.abspath(destDir),
            )
        else:
            # XXX we are not trying to adjust the clone location to be a parent of dest
            sourceProject.projectRepo.clone(dest)
            relPathToProject = sourceProject.projectRepo.find_repo_path(
                sourceProject.projectRoot
            )
            # adjust if project is not at the root of its repo:
            dest = Project.normalize_path(os.path.join(dest, relPathToProject))
            clonedProject = find_project(dest, homePath)

    # pass in "" as dest because we already "consumed" dest by cloning the project to that location
    manifest, message = _create_in_cloned_project(
        templateVars, clonedProject, "", mono, context, shared_repo, homePath
    )
    if not isRemote and manifest:
        # we need to clone referenced local repos so the new project has access to them
        clone_local_repos(manifest, sourceProject, clonedProject)
    return message


def _create_in_cloned_project(
    templateVars, clonedProject, dest, mono, context, shared_repo, homePath
):
    """
    Called by :py:func:`unfurl.init.clone` when cloning an ensemble.

    ================================   ========================
    source ensemble                    result
    ================================   ========================
    project root or ensemble in repo   git clone only
    local ensemble or template         git clone + new ensemble
    ================================   ========================

    """
    from unfurl import yamlmanifest

    ensembleInProjectRepo = not shared_repo and is_ensemble_in_project_repo(
        clonedProject, templateVars
    )
    if ensembleInProjectRepo:
        # the ensemble is already part of the source project repository or a submodule
        # we're done
        manifest = yamlmanifest.ReadOnlyManifest(localEnv=templateVars["localEnv"])
        return manifest, "Cloned project to " + clonedProject.projectRoot
    else:
        # create local/unfurl.yaml in the new project
        # XXX vaultpass should only be set for the new ensemble being created
        write_project_config(
            os.path.join(clonedProject.projectRoot, "local"),
            DefaultNames.LocalConfig,
            "unfurl.local.yaml.j2",
            dict(vaultpass=get_random_password()),
        )
        # dest: should be a path relative to the clonedProject's root
        assert not os.path.isabs(dest)
        destDir, manifest = create_new_ensemble(
            templateVars, clonedProject, dest, mono, context, shared_repo, homePath
        )
        return (
            manifest,
            f'Created new ensemble at "{destDir}" in cloned project at "{clonedProject.projectRoot}"',
        )


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
    return "unrecognized runtime uri"


def _run_pip_env(do_install, kw):
    # create the virtualenv and install the dependencies specified in the Pipefiles
    sys_exit = sys.exit
    try:
        retcode = 0

        def noexit(code):
            retcode = code

        sys.exit = noexit

        do_install(**kw)
    finally:
        sys.exit = sys_exit

    return retcode


# XXX provide an option for an uUfurl installation can be shared across runtimes.
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
        # XXX report error: can't find site-package folder
        return
    _write_file(sitePackageDir, "unfurl.pth", base)
    _write_file(sitePackageDir, "unfurl.egg-link", base)


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
        from pipenv.core import do_install, ensure_python
        from pipenv.utils import python_version

        pythonPath = str(ensure_python())
        assert pythonPath, pythonPath
        if not pipfileLocation:
            versionStr = python_version(pythonPath)
            assert versionStr, versionStr
            version = versionStr.rpartition(".")[0]  # 3.8.1 => 3.8
            # version = subprocess.run([pythonPath, "-V"]).stdout.decode()[
            #     7:10
            # ]  # e.g. Python 3.8.1 => 3.8
            pipfileLocation = os.path.join(
                _templatePath, "python" + version
            )  # e.g. templates/python3.8

        if not os.path.isdir(pipfileLocation):
            return f'Pipfile location is not a valid directory: "{pipfileLocation}"'

        # copy Pipfiles to project root
        if os.path.abspath(projectDir) != os.path.abspath(pipfileLocation):
            for filename in ["Pipfile", "Pipfile.lock"]:
                path = os.path.join(pipfileLocation, filename)
                if os.path.isfile(path):
                    shutil.copy(path, projectDir)

        kw = dict(python=pythonPath)
        # need to run without args first so lock isn't overwritten
        retcode = _run_pip_env(do_install, kw)
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
            kw["packages"] = [
                "unfurl==" + __version__()
            ]  # use the same version as current
        retcode = _run_pip_env(do_install, kw)
        if retcode:
            return f"Pipenv (step 2) failed: {retcode}"

        return ""
    finally:
        if VIRTUAL_ENV:
            os.environ["VIRTUAL_ENV"] = VIRTUAL_ENV
        else:
            os.environ.pop("VIRTUAL_ENV", None)
        os.chdir(cwd)
