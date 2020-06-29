import uuid
import os
import os.path
import sys
import shutil
from . import (
    __version__,
    DefaultManifestName,
    DefaultLocalConfigName,
    getHomeConfigPath,
)
from .tosca import TOSCA_VERSION
from .repo import Repo, GitRepo
from .util import UnfurlError, subprocess

_templatePath = os.path.join(os.path.abspath(os.path.dirname(__file__)), "templates")


def _writeFile(folder, filename, content):
    if not os.path.isdir(folder):
        os.makedirs(folder)
    filepath = os.path.join(folder, filename)
    with open(filepath, "w") as f:
        f.write(content)
    return filepath


def writeTemplate(folder, filename, templatePath, vars):
    from .runtime import NodeInstance
    from .eval import RefContext
    from .support import applyTemplate

    with open(os.path.join(_templatePath, templatePath)) as f:
        source = f.read()
    instance = NodeInstance()
    instance.baseDir = _templatePath
    content = applyTemplate(source, RefContext(instance, vars))
    return _writeFile(folder, filename, content)


def writeProjectConfig(
    projectdir,
    filename=DefaultLocalConfigName,
    defaultManifestPath=DefaultManifestName,
    localInclude="",
    templatePath=DefaultLocalConfigName + ".j2",
):
    vars = dict(
        version=__version__, include=localInclude, manifestPath=defaultManifestPath
    )
    return writeTemplate(projectdir, filename, templatePath, vars)


def createHome(path=None, **kw):
    """
    Write ~/.unfurl_home/unfurl.yaml if missing
    """
    homePath = getHomeConfigPath(path)
    if not homePath or os.path.exists(homePath):
        return None
    homedir, filename = os.path.split(homePath)
    writeTemplate(homedir, DefaultManifestName, "home-manifest.yaml.j2", {})
    configPath = writeProjectConfig(
        homedir, filename, templatePath="home-unfurl.yaml.j2"
    )
    if not kw.get("no_engine"):
        initEngine(homedir, kw.get("engine") or "venv:")
    return configPath


def _createRepo(repotype, gitDir, gitUri=None):
    from git import Repo

    if not os.path.isdir(gitDir):
        os.makedirs(gitDir)
    repo = Repo.init(gitDir)
    filename = ".unfurl"
    filepath = os.path.join(gitDir, filename)
    with open(filepath, "w") as f:
        f.write(
            """\
  unfurl:
    version: %s
  repo:
    type: %s
    uuid: %s
  """
            % (__version__, repotype, uuid.uuid1())
        )

    repo.index.add([filename])
    repo.index.commit("Initial Commit")
    return GitRepo(repo)


def writeServiceTemplate(projectdir, repo):
    relPathToSpecRepo = os.path.relpath(repo.workingDir, os.path.abspath(projectdir))
    vars = dict(
        version=TOSCA_VERSION,
        specRepoPath=relPathToSpecRepo,
        initialCommit=repo.getInitialRevision(),
    )
    return writeTemplate(
        projectdir, "service-template.yaml", "service-template.yaml.j2", vars
    )


def createSpecRepo(gitDir):
    repo = _createRepo("spec", gitDir)
    writeServiceTemplate(gitDir, repo)
    writeTemplate(gitDir, "manifest-template.yaml", "manifest-template.yaml.j2", {})
    repo.repo.index.add(["service-template.yaml", "manifest-template.yaml"])
    repo.repo.index.commit("Default specification repository boilerplate")
    return repo


def writeManifest(gitDir, manifestName, specRepo, instanceRepo):
    relPathToSpecRepo = os.path.relpath(specRepo.workingDir, os.path.abspath(gitDir))
    relPathToInstanceRepo = os.path.relpath(
        specRepo.workingDir, os.path.abspath(gitDir)
    )
    vars = dict(
        specRepoPath=relPathToSpecRepo,
        instanceRepoPath=relPathToInstanceRepo or ".",
        specInitialCommit=specRepo.getInitialRevision(),
        instanceInitialCommit=instanceRepo.getInitialRevision(),
    )
    return writeTemplate(gitDir, manifestName, "manifest.yaml.j2", vars)


def createInstanceRepo(gitDir, specRepo):
    manifestName = DefaultManifestName
    repo = _createRepo("instance", gitDir)
    writeManifest(gitDir, manifestName, specRepo, repo)
    repo.repo.index.add([manifestName])
    repo.repo.index.commit("Default instance repository boilerplate")
    return repo


def createMultiRepoProject(projectdir):
    """
  Creates a project folder with two git repositories:
  a specification repository that contains "service-template.yaml" and "manifest-template.yaml" in a "spec" folder.
  and an instance repository containing a "manifest.yaml" in a "instances/current" folder.
  """
    defaultManifestPath = os.path.join("instances", "current", "manifest.yaml")
    projectConfigPath = writeProjectConfig(
        projectdir, defaultManifestPath=defaultManifestPath
    )
    specRepo = createSpecRepo(os.path.join(projectdir, "spec"))
    createInstanceRepo(os.path.join(projectdir, "instances", "current"), specRepo)
    return projectConfigPath


def createMonoRepoProject(projectdir, repo):
    """
    Creates a folder named `projectdir` with a git repository with the following files:

    unfurl.yaml
    unfurl.local.example.yaml
    .gitignore
    manifest.yaml

    Returns the absolute path to unfurl.yaml
    """
    localConfigFilename = "unfurl.local.yaml"
    writeProjectConfig(
        projectdir, localConfigFilename, templatePath="unfurl.local.yaml.j2"
    )
    localInclude = "+?include: " + localConfigFilename
    projectConfigPath = writeProjectConfig(projectdir, localInclude=localInclude)
    gitIgnoreContent = """%s\nlocal\n""" % localConfigFilename
    gitIgnorePath = _writeFile(projectdir, ".gitignore", gitIgnoreContent)
    serviceTemplatePath = writeServiceTemplate(projectdir, repo)
    # write manifest
    manifestPath = writeTemplate(
        projectdir, DefaultManifestName, "manifest-template.yaml.j2", {}
    )
    repo.commitFiles(
        [projectConfigPath, gitIgnorePath, serviceTemplatePath, manifestPath],
        "Create a new unfurl repository",
    )
    return projectConfigPath


def createProject(projectdir, home=None, mono=False, existing=False, **kw):
    if existing:
        repo = Repo.findContainingRepo(projectdir)
        if not repo:
            raise UnfurlError("Could not find an existing repository")
    else:
        repo = None
    # creates home if it doesn't exist already:
    newHome = createHome(home, **kw)
    # XXX add project to ~/.unfurl_home/unfurl.yaml
    if mono or existing:
        if not repo:
            repo = _createRepo("mono", projectdir)
        return newHome, createMonoRepoProject(projectdir, repo)
    else:
        return newHome, createMultiRepoProject(projectdir)

    if not newHome and not kw.get("no_engine") and kw.get("engine"):
        # if engine was explicitly set and we aren't creating the home project
        # then initialize the engine here
        initEngine(projectdir, kw.get("engine"))


def _isValidSpecRepo(repo):
    return os.path.isfile(os.path.join(repo.workingDir, "manifest-template.yaml"))


def createNewInstance(specRepoDir, targetPath):
    sourceRepo = Repo.findContainingRepo(specRepoDir)
    if not sourceRepo:
        return None, "No repository exists at " + os.path.abspath(specRepoDir)
    if not _isValidSpecRepo(sourceRepo):
        return (
            None,
            "The respository at '%s' is not valid" % os.path.abspath(specRepoDir),
        )

    currentRepo = Repo.findContainingRepo(targetPath)
    if currentRepo:
        writeManifest(targetPath, DefaultManifestName, sourceRepo, None)
        return (
            sourceRepo,
            'created a new instance at "%s" in the current repository located at'
            % targetPath,
        )

    instanceRepo = createInstanceRepo(targetPath, sourceRepo)
    # XXX
    # project = localEnv.findProject(targetPath)
    # if project: # add to project
    return (
        instanceRepo,
        "created new instance repository at %s" % os.path.abspath(targetPath),
    )


def cloneSpecToNewProject(sourceDir, projectDir):
    sourceRepo = Repo.findContainingRepo(sourceDir)
    if not sourceRepo:
        return None, "No repository exists at " + os.path.abspath(sourceDir)
    if not _isValidSpecRepo(sourceRepo):
        return None, "The respository at '%s' is not valid" % os.path.abspath(sourceDir)

    if os.path.exists(projectDir):
        return None, os.path.abspath(projectDir) + ": file already exists"

    # XXX make sure projectdir is usable
    defaultManifestPath = os.path.join(
        projectDir, "instances", "current", "manifest.yaml"
    )
    projectConfigPath = writeProjectConfig(
        projectDir, defaultManifestPath=defaultManifestPath
    )
    fullProjectDir = os.path.abspath(projectDir)
    specRepo = sourceRepo.clone(os.path.join(fullProjectDir, "spec"))
    createInstanceRepo(os.path.join(projectDir, "instances", "current"), specRepo)
    return projectConfigPath, "New project created at %s" % fullProjectDir


# def cloneInstanceLocal():
# delete status, changes, latestChange, set changeLog


def initEngine(projectDir, engine):
    kind, sep, rest = engine.partition(":")
    if kind == "venv":
        return createVenv(projectDir, rest)
    # elif kind == 'docker'
    # XXX return 'unrecoginized engine string: "%s"'
    return False


def _addUnfurlToVenv(projectdir):
    # this is hacky
    # can cause confusion if it exposes more packages than unfurl
    base = os.path.dirname(os.path.dirname(_templatePath))
    sitePackageDir = None
    libDir = os.path.join(projectdir, os.path.join(".venv", "lib"))
    for name in os.listdir(libDir):
        sitePackageDir = os.path.join(libDir, name, "site-packages")
        if os.path.isdir(sitePackageDir):
            break
    else:
        # XXX report error: can't find site-packages folder
        return
    _writeFile(sitePackageDir, "unfurl.pth", base)
    _writeFile(sitePackageDir, "unfurl.egg-link", base)


def createVenv(projectDir, pipfileLocation):
    """Create a virtual python environment for the given project."""
    os.environ["PIPENV_IGNORE_VIRTUALENVS"] = "1"
    os.environ["PIPENV_VENV_IN_PROJECT"] = "1"
    if "PIPENV_PYTHON" not in os.environ:
        os.environ["PIPENV_PYTHON"] = sys.executable

    try:
        cwd = os.getcwd()
        os.chdir(projectDir)
        # need to set env vars and change current dir before importing pipenv
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
            # XXX 'Pipfile location is not a valid directory: "%s" % pipfileLocation'
            return False

        # copy Pipfiles to project root
        if os.path.abspath(projectDir) != os.path.abspath(pipfileLocation):
            for filename in ["Pipfile", "Pipfile.lock"]:
                path = os.path.join(pipfileLocation, filename)
                if os.path.isfile(path):
                    shutil.copy(path, projectDir)

        # create the virtualenv and install the dependencies specified in the Pipefiles
        sys_exit = sys.exit
        try:
            retcode = -1

            def noexit(code):
                retcode = code

            sys.exit = noexit

            do_install(python=pythonPath)
            # this doesn't actually install the unfurl so link to this one
            _addUnfurlToVenv(projectDir)
        finally:
            sys.exit = sys_exit

        return not retcode  # retcode means error
    finally:
        os.chdir(cwd)
