import uuid
import os
import os.path
from . import (
    __version__,
    DefaultManifestName,
    DefaultLocalConfigName,
    getHomeConfigPath,
)
from .tosca import TOSCA_VERSION
from .repo import Repo, GitRepo
from .util import UnfurlError

from ansible.template import Templar
from ansible.parsing.dataloader import DataLoader


_templatePath = os.path.join(os.path.abspath(os.path.dirname(__file__)), "templates")


def processTemplate(template, **vars):
    loader = DataLoader()
    templar = Templar(loader, variables=vars)
    # use do_template() instead of template() because is_template test can fail if variable_start_string is set
    return templar.do_template(template, disable_lookups=True)


def _writeFile(folder, filename, content):
    if not os.path.isdir(folder):
        os.makedirs(folder)
    filepath = os.path.join(folder, filename)
    with open(filepath, "w") as f:
        f.write(content)
    return filepath


def writeTemplate(folder, filename, templatePath, vars):
    with open(os.path.join(_templatePath, templatePath)) as f:
        source = f.read()
    content = processTemplate(source, **vars)
    return _writeFile(folder, filename, content)


def writeProjectConfig(
    projectdir,
    filename=DefaultLocalConfigName,
    defaultManifestPath=DefaultManifestName,
    localInclude="",
):
    templatePath = DefaultLocalConfigName + ".j2"
    vars = dict(
        version=__version__, include=localInclude, manifestPath=defaultManifestPath
    )
    return writeTemplate(projectdir, filename, templatePath, vars)


def createHome(path=None):
    """
  Write ~/.unfurl_home/unfurl.yaml if missing
  """
    homedir = getHomeConfigPath(path)
    if not homedir:
        return None
    if not os.path.exists(homedir):
        content = (
            """\
    unfurl:
      version: %s
    """
            % __version__
        )
        return _writeFile(homedir, DefaultLocalConfigName, content)


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


def createInstanceRepo(gitDir, specRepo):
    manifestName = DefaultManifestName
    repo = _createRepo("instance", gitDir)
    relPathToSpecRepo = os.path.relpath(specRepo.workingDir, os.path.abspath(gitDir))
    vars = dict(
        specRepoPath=relPathToSpecRepo,
        specInitialCommit=specRepo.getInitialRevision(),
        instanceInitialCommit=repo.getInitialRevision(),
    )
    writeTemplate(gitDir, manifestName, "manifest.yaml.j2", vars)
    repo.repo.index.add([manifestName])
    repo.repo.index.commit("Default instance repository boilerplate")
    return repo


def createMultiRepoProject(projectdir):
    """
  Creates a project folder with two git repositories:
  a specification repository that contains "service-template.yaml" and "manifest-template.yaml" in a "spec" folder.
  and an instance repository containing a "manifest.yaml" in a "instances/current" folder.
  """
    defaultManifestPath = os.path.join(
        projectdir, "instances", "current", "manifest.yaml"
    )
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
    localContent = """\
        # copy this to unfurl.local.yaml and
        # add configuration that you don't want commited to this repository,
        # such as secrets, local settings, and local instances.
        """
    exampleLocalConfigPath = writeProjectConfig(
        projectdir, "unfurl.local.example.yaml", localInclude=localContent
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
        [
            projectConfigPath,
            exampleLocalConfigPath,
            gitIgnorePath,
            serviceTemplatePath,
            manifestPath,
        ],
        "Create an unfurl deployment",
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
    newHome = createHome(home)
    # XXX add project to ~/.unfurl_home/unfurl.yaml
    if mono or existing:
        if not repo:
            repo = _createRepo("mono", projectdir)
        return newHome, createMonoRepoProject(projectdir, repo)
    else:
        return newHome, createMultiRepoProject(projectdir)


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

    if Repo.findContainingRepo(targetPath):
        return None, "Can't create repository inside another repository"
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
