# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
"""
This module implements creating and cloning project and ensembles as well Unfurl runtimes.
"""
import uuid
import os
import os.path
import datetime
import sys
import shutil
from . import DefaultNames, getHomeConfigPath, __version__
from .tosca import TOSCA_VERSION
from .repo import Repo, GitRepo, splitGitUrl, isURLorGitPath
from .util import UnfurlError
from .localenv import LocalEnv, Project
import random
import string

_templatePath = os.path.join(os.path.abspath(os.path.dirname(__file__)), "templates")


def renameForBackup(dir):
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


def _writeFile(folder, filename, content):
    if not os.path.isdir(folder):
        os.makedirs(os.path.normpath(folder))
    filepath = os.path.join(folder, filename)
    with open(filepath, "w") as f:
        f.write(content)
    return filepath


def writeTemplate(folder, filename, template, vars, templateDir=None):
    from .runtime import NodeInstance
    from .eval import RefContext
    from .support import applyTemplate

    if templateDir and not os.path.isabs(templateDir):
        templateDir = os.path.join(_templatePath, templateDir)
    if not templateDir or not os.path.exists(os.path.join(templateDir, template)):
        templateDir = _templatePath  # use default file if missing from templateDir

    with open(os.path.join(templateDir, template)) as f:
        source = f.read()
    instance = NodeInstance()
    instance._baseDir = _templatePath
    content = applyTemplate(source, RefContext(instance, vars))
    return _writeFile(folder, filename, content)


def writeProjectConfig(
    projectdir,
    filename=DefaultNames.LocalConfig,
    templatePath=DefaultNames.LocalConfig + ".j2",
    vars=None,
    templateDir=None,
):
    _vars = dict(include="", manifestPath=None)
    if vars:
        _vars.update(vars)
    return writeTemplate(projectdir, filename, templatePath, _vars, templateDir)


def createHome(home=None, render=False, replace=False, **kw):
    """
    Create the home project if missing
    """
    homePath = getHomeConfigPath(home)
    if not homePath:
        return None
    exists = os.path.exists(homePath)
    if exists and not replace:
        return None

    homedir, filename = os.path.split(homePath)
    if render:  # just render
        repo = Repo.findContainingRepo(homedir)
        # XXX if repo and update: git stash; git checkout rendered
        ensembleDir = os.path.join(homedir, DefaultNames.EnsembleDirectory)
        ensembleRepo = Repo.findContainingRepo(ensembleDir)
        configPath = renderProject(homedir, repo, ensembleRepo, "home")
        # XXX if repo and update: git commit -m"updated"; git checkout master; git stash pop
        return configPath
    else:
        if exists:
            renameForBackup(homedir)

    newHome, configPath, repo = createProject(
        homedir,
        template="home",
        runtime=kw.get("runtime") or "venv:",
        no_runtime=kw.get("no_runtime"),
        msg="Create the unfurl home repository",
    )
    if repo:
        repo.repo.git.branch("rendered")  # now create a branch
    return configPath


def _createRepo(gitDir, ignore=True):
    import git

    if not os.path.isdir(gitDir):
        os.makedirs(gitDir)
    repo = git.Repo.init(gitDir)
    repo.index.add(addHiddenGitFiles(gitDir))
    repo.index.commit("Initial Commit for %s" % uuid.uuid1())

    if ignore:
        Repo.ignoreDir(gitDir)
    return GitRepo(repo)


def writeServiceTemplate(projectdir):
    vars = dict(version=TOSCA_VERSION)
    return writeTemplate(
        projectdir, "service-template.yaml", "service-template.yaml.j2", vars
    )


def writeEnsembleManifest(
    destDir, manifestName, specRepo, specDir=None, extraVars=None, templateDir=None
):
    if specDir:
        specDir = os.path.abspath(specDir)
    else:
        specDir = ""
    vars = dict(specRepoUrl=specRepo.getGitLocalUrl(specDir, "spec"))
    if extraVars:
        vars.update(extraVars)
    return writeTemplate(destDir, manifestName, "manifest.yaml.j2", vars, templateDir)


def addHiddenGitFiles(gitDir):
    # write .gitignore and  .gitattributes
    gitIgnorePath = writeTemplate(gitDir, ".gitignore", "gitignore.j2", {})
    gitAttributesContent = "**/*%s merge=union\n" % DefaultNames.JobsLog
    gitAttributesPath = _writeFile(gitDir, ".gitattributes", gitAttributesContent)
    return [os.path.abspath(gitIgnorePath), os.path.abspath(gitAttributesPath)]


def renderProject(
    projectdir,
    repo,
    ensembleRepo,
    templateDir=None,
    names=DefaultNames,
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

    vars = dict(vaultpass=get_random_password())
    writeProjectConfig(
        os.path.join(projectdir, "local"),
        localConfigFilename,
        "unfurl.local.yaml.j2",
        vars,
        templateDir,
    )

    localInclude = "+?include: " + os.path.join("local", localConfigFilename)
    vars = dict(include=localInclude)
    projectConfigPath = writeProjectConfig(
        projectdir,
        names.LocalConfig,
        "unfurl.yaml.j2",
        vars,
        templateDir,
    )

    # write ensemble-template.yaml
    writeTemplate(
        projectdir,
        names.EnsembleTemplate,
        "manifest-template.yaml.j2",
        {},
        templateDir,
    )

    if ensembleRepo:
        ensembleDir = os.path.join(projectdir, names.EnsembleDirectory)
        manifestName = names.Ensemble
        extraVars = dict(
            ensembleUri=ensembleRepo.getUrlWithPath(
                os.path.join(ensembleDir, manifestName)
            ),
            # include the ensembleTemplate in the root of the specDir
            ensembleTemplate=names.EnsembleTemplate,
        )
        # write ensemble/ensemble.yaml
        writeEnsembleManifest(
            ensembleDir,
            manifestName,
            repo,
            extraVars=extraVars,
            templateDir=templateDir,
        )
    return projectConfigPath


def createProject(
    projectdir,
    home=None,
    mono=False,
    existing=False,
    empty=False,
    template=None,
    submodule=False,
    **kw
):
    if existing:
        repo = Repo.findContainingRepo(projectdir)
        if not repo:
            raise UnfurlError("Could not find an existing repository")
    else:
        repo = None

    newHome = ""
    homePath = getHomeConfigPath(home)
    # don't try to create the home project if we are already creating the home project
    if projectdir != os.path.dirname(homePath):
        # create the home project (but only if it doesn't exist already)
        newHome = createHome(home, **kw)

    if repo:
        addHiddenGitFiles(projectdir)
    else:
        repo = _createRepo(projectdir)

    ensembleDir = os.path.join(projectdir, DefaultNames.EnsembleDirectory)
    if mono:
        ensembleRepo = repo
    else:
        ensembleRepo = _createRepo(ensembleDir, not submodule)

    projectConfigPath = renderProject(
        projectdir,
        repo,
        not empty and ensembleRepo,
        template,
    )
    if not mono:
        ensembleRepo.addAll(ensembleDir)
        ensembleRepo.repo.index.commit("Default ensemble repository boilerplate")

    if submodule:
        repo.addSubModule(ensembleDir)

    if not newHome and not kw.get("no_runtime") and kw.get("runtime"):
        # if runtime was explicitly set and we aren't creating the home project
        # then initialize the runtime here
        try:
            initEngine(projectdir, kw.get("runtime"))
        except:
            pass  # don't stop even if this fails

    repo.addAll(projectdir)
    repo.repo.index.commit(kw.get("msg") or "Create a new Unfurl project")

    return newHome, projectConfigPath, repo


def cloneLocalRepos(manifest, sourceProject, targetProject):
    # We need to clone repositories that are local to the source project
    # otherwise we won't be able to find them
    for repoSpec in manifest.tosca.template.repositories.values():
        if repoSpec.name == "self":
            continue
        repo = sourceProject.findGitRepoFromRepository(repoSpec)
        if repo:
            targetProject.findOrClone(repo)


def _createEnsembleRepo(manifest, repo):
    destDir = os.path.dirname(manifest.manifest.path)
    if not repo:
        repo = _createRepo(destDir)
    elif not os.path.isdir(destDir):
        os.makedirs(destDir)

    manifest.metadata["uri"] = repo.getUrlWithPath(manifest.manifest.path)
    with open(manifest.manifest.path, "w") as f:
        manifest.dump(f)

    repo.repo.index.add([manifest.manifest.path])
    repo.repo.index.commit("Default ensemble repository boilerplate")
    return repo


def _looksLike(path, name):
    # in case path is a directory:
    if os.path.isfile(os.path.join(path, name)):
        return path, name
    if path.endswith(name):  # name is explicit so don't need to check if file exists
        return os.path.split(path)
    return None


def _getEnsemblePaths(sourcePath, sourceProject):
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
    if not os.path.exists(sourcePath or "."):
        return {}
    isServiceTemplate = sourcePath.endswith(DefaultNames.ServiceTemplate)
    if not isServiceTemplate:
        # we only support cloning TOSCA service templates if their names end in "service-template.yaml"
        try:
            localEnv = LocalEnv(sourcePath, project=sourceProject)
            sourceDir = sourceProject.getRelativePath(
                os.path.dirname(localEnv.manifestPath)
            )
            return dict(sourceDir=sourceDir, localEnv=localEnv)
        except:
            pass

    # didn't find the specified file (or the default ensemble if none was specified)
    # so if sourcePath was a directory try for one of the default template files
    if isServiceTemplate or os.path.isdir(sourcePath):
        # look for an ensemble-template or service-template in source path
        template = _looksLike(sourcePath, DefaultNames.EnsembleTemplate)
        if template:
            sourceDir = sourceProject.getRelativePath(template[0])
            return dict(sourceDir=sourceDir, ensembleTemplate=template[1])
        template = _looksLike(sourcePath, DefaultNames.ServiceTemplate)
        if template:
            sourceDir = sourceProject.getRelativePath(template[0])
            return dict(sourceDir=sourceDir, serviceTemplate=template[1])
        # nothing valid found
    return {}


def createNewEnsemble(templateVars, project, targetPath, mono):
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
    # choose a destDir that doesn't conflict with an existing folder
    # (i.e. if default ensemble already exists)
    destDir = project.getUniquePath(destDir)
    # destDir is now absolute
    targetPath = os.path.normpath(os.path.join(destDir, manifestName))

    if "localEnv" not in templateVars:
        # we found a template file to clone
        assert project
        sourceDir = os.path.normpath(
            os.path.join(project.projectRoot, templateVars["sourceDir"])
        )
        specRepo, relPath, revision, bare = project.findPathInRepos(sourceDir)
        if not specRepo:
            raise UnfurlError(
                '"%s" is not in a git repository. Cloning from plain file directories not yet supported'
                % os.path.abspath(sourceDir)
            )
        manifestPath = writeEnsembleManifest(
            os.path.join(project.projectRoot, destDir),
            manifestName,
            specRepo,
            sourceDir,
            templateVars,
        )
        localEnv = LocalEnv(manifestPath, project=project)
        manifest = yamlmanifest.ReadOnlyManifest(localEnv=localEnv)
    elif templateVars:
        # didn't find a template file
        # look for an ensemble at the given path or use the source project's default
        manifest = yamlmanifest.clone(templateVars["localEnv"], targetPath)
    else:
        raise UnfurlError("can't find anything to clone")
    _createEnsembleRepo(manifest, mono and project.projectRepo)
    return destDir, manifest
    # XXX need to add manifest to unfurl.yaml


def cloneRemoteProject(source, destDir):
    # check if source is a git url
    repoURL, filePath, revision = splitGitUrl(source)
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
            'Can not clone project into "%s": folder is not empty' % destDir
        )
    Repo.createWorkingDir(repoURL, destDir, revision)  # clone source repo
    targetDir = os.path.join(destDir, filePath)
    sourceRoot = Project.findPath(targetDir)
    if not sourceRoot:
        raise UnfurlError(
            'Error: cloned "%s" to "%s" but couldn\'t find an Unfurl project'
            % (source, destDir)
        )
    sourceProject = Project(sourceRoot)
    return sourceProject, targetDir


def getSourceProject(source):
    sourceRoot = Project.findPath(source)
    if sourceRoot:
        return Project(sourceRoot)
    return None


def isEnsembleInProjectRepo(project, paths):
    # check if source points to an ensemble that is part of the project repo
    if not project.projectRepo or "localEnv" not in paths:
        return False
    sourceDir = paths["sourceDir"]
    assert not os.path.isabs(sourceDir)
    pathToEnsemble = os.path.join(project.projectRoot, sourceDir)
    if not os.path.isdir(pathToEnsemble):
        return False
    if project.projectRepo.isPathExcluded(sourceDir):
        return False
    return True


def clone(source, dest, includeLocal=False, **options):
    """
    Clone the `source` ensemble to `dest`. If `dest` isn't in a project, create one.
    `source` can point to an ensemble_template, a service_template, an existing ensemble
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
        dest = Repo.getPathForGitRepo(source)  # choose dest based on source url
    # XXX else: # we're assuming dest is directory, handle case where filename is included

    mono = "mono" in options or "existing" in options
    isRemote = isURLorGitPath(source)
    if isRemote:
        clonedProject, source = cloneRemoteProject(source, dest)
        # source is now a path inside the cloned project
        paths = _getEnsemblePaths(source, clonedProject)
    else:
        sourceProject = getSourceProject(source)
        sourceNotInProject = not sourceProject
        if sourceNotInProject:
            # source wasn't in a project
            raise UnfurlError(
                "Can't clone \"%s\": it isn't in an Unfurl project or repository"
                % source
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

        relDestDir = sourceProject.getRelativePath(dest)
        paths = _getEnsemblePaths(source, sourceProject)
        if not relDestDir.startswith(".."):
            # dest is in the source project (or its a new project)
            # so don't need to clone, just need to create an ensemble
            destDir, manifest = createNewEnsemble(
                paths, sourceProject, relDestDir, mono
            )
            return 'Created ensemble in %s project: "%s"' % (
                "new" if sourceNotInProject else "existing",
                os.path.abspath(destDir),
            )
        else:
            # XXX we are not trying to adjust the clone location to be a parent of dest
            sourceProject.projectRepo.clone(dest)
            relPathToProject = sourceProject.projectRepo.findRepoPath(
                sourceProject.projectRoot
            )
            # adjust if project is not at the root of its repo:
            dest = Project.normalizePath(os.path.join(dest, relPathToProject))
            clonedProject = Project(dest)

    # pass in "" as dest because we already "consumed" dest by cloning the project to that location
    manifest, message = _createInClonedProject(paths, clonedProject, "", mono)
    if not isRemote and manifest:
        # we need to clone referenced local repos so the new project has access to them
        cloneLocalRepos(manifest, sourceProject, clonedProject)
    return message


def _createInClonedProject(paths, clonedProject, dest, mono):
    """
    Called by `clone` when cloning an ensemble.

    ================================   ========================
    source ensemble                    result
    ================================   ========================
    project root or ensemble in repo   git clone only
    local ensemble or template         git clone + new ensemble
    ================================   ========================

    """
    from unfurl import yamlmanifest

    ensembleInProjectRepo = isEnsembleInProjectRepo(clonedProject, paths)
    if ensembleInProjectRepo:
        # the ensemble is already part of the source project repository or a submodule
        # we're done
        manifest = yamlmanifest.ReadOnlyManifest(localEnv=paths["localEnv"])
        return manifest, "Cloned project to " + clonedProject.projectRoot
    else:
        # create local/unfurl.yaml in the new project
        # XXX vaultpass should only be set for the new ensemble being created
        writeProjectConfig(
            os.path.join(clonedProject.projectRoot, "local"),
            DefaultNames.LocalConfig,
            "unfurl.local.yaml.j2",
            dict(vaultpass=get_random_password()),
        )
        # dest: should be a path relative to the clonedProject's root
        assert not os.path.isabs(dest)
        destDir, manifest = createNewEnsemble(paths, clonedProject, dest, mono)
        return manifest, 'Created new ensemble at "%s" in cloned project at "%s"' % (
            destDir,
            clonedProject.projectRoot,
        )


def _getUnfurlRequirementUrl(spec):
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


def initEngine(projectDir, runtime):
    runtime = runtime or "venv:"
    kind, sep, rest = runtime.partition(":")
    if kind == "venv":
        pipfileLocation, sep, unfurlLocation = rest.partition(":")
        return createVenv(
            projectDir, pipfileLocation, _getUnfurlRequirementUrl(unfurlLocation)
        )
    # elif kind == 'docker'
    return "unrecognized runtime uri"


def _runPipEnv(do_install, kw):
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


def createVenv(projectDir, pipfileLocation, unfurlLocation):
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
        from pipenv.core import do_install, ensure_python
        from pipenv.utils import python_version
        from pipenv import environments

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
            return 'Pipfile location is not a valid directory: "%s"' % pipfileLocation

        # copy Pipfiles to project root
        if os.path.abspath(projectDir) != os.path.abspath(pipfileLocation):
            for filename in ["Pipfile", "Pipfile.lock"]:
                path = os.path.join(pipfileLocation, filename)
                if os.path.isfile(path):
                    shutil.copy(path, projectDir)

        kw = dict(python=pythonPath)
        # need to run without args first so lock isn't overwritten
        retcode = _runPipEnv(do_install, kw)
        if retcode:
            return "Pipenv (step 1) failed: %s" % retcode

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
        retcode = _runPipEnv(do_install, kw)
        if retcode:
            return "Pipenv (step 2) failed: %s" % retcode

        return ""
    finally:
        if VIRTUAL_ENV:
            os.environ["VIRTUAL_ENV"] = VIRTUAL_ENV
        else:
            os.environ.pop("VIRTUAL_ENV", None)
        os.chdir(cwd)
