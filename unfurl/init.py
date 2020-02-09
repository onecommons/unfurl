import uuid
import os
import os.path
from . import __version__
from .tosca import TOSCA_VERSION
from .repo import Repo, GitRepo
from .util import UnfurlError


def _writeFile(dir, filename, content):
    if not os.path.isdir(dir):
        os.makedirs(dir)
    filepath = os.path.join(dir, filename)
    with open(filepath, "w") as f:
        f.write(content)
    return filepath


def writeProjectConfig(
    projectdir,
    filename="unfurl.yaml",
    defaultManifestPath="manifest.yaml",
    localInclude="",
):
    content = """\
unfurl:
  version: %s
%s
manifests:
  - file: %s
    default: true

# this is the default behavior, so not needed:
# defaults: # used if the instance isn't defined above
#   local:
#     # local and secret can have "attributes" instead of declaring an import
#     attributes:
#       inheritFrom: home
#   secret:
#     attributes:
#       inheritFrom: home
""" % (
        __version__,
        localInclude,
        defaultManifestPath,
    )
    return _writeFile(projectdir, filename, content)


def createHome(path=None):
    """
  Write ~/.unfurl_home/unfurl.yaml if missing
  """
    homedir = path or os.path.expanduser(os.path.join("~", ".unfurl_home"))
    if not os.path.exists(homedir):
        content = (
            """\
    unfurl:
      version: %s
    """
            % __version__
        )
        return _writeFile(homedir, "unfurl.yaml", content)


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
    serviceTemplatePath = os.path.join(projectdir, "service-template.yaml")
    relPathToSpecRepo = os.path.relpath(repo.workingDir, os.path.abspath(projectdir))
    with open(serviceTemplatePath, "w") as f:
        f.write(
            """\
tosca_definitions_version: %s
repositories:
  spec:
    url: file:%s
    metadata:
      initial-commit: %s
topology_template:
  node_templates: {}
"""
            % (TOSCA_VERSION, relPathToSpecRepo, repo.getInitialRevision())
        )
    return serviceTemplatePath


def createSpecRepo(gitDir):
    repo = _createRepo("spec", gitDir)
    writeServiceTemplate(gitDir, repo)
    manifestTemplatePath = os.path.join(gitDir, "manifest-template.yaml")
    with open(manifestTemplatePath, "w") as f:
        f.write(
            """\
  apiVersion: unfurl/v1alpha1
  kind: Manifest
  spec:
    service_template:
      +include: service-template.yaml
"""
        )
    repo.repo.index.add(["service-template.yaml", "manifest-template.yaml"])
    repo.repo.index.commit("Default specification repository boilerplate")
    return repo


def createInstanceRepo(gitDir, specRepo):
    repo = _createRepo("instance", gitDir)
    filepath = os.path.join(gitDir, "manifest.yaml")
    relPathToSpecRepo = os.path.relpath(specRepo.workingDir, os.path.abspath(gitDir))
    specInitialCommit = specRepo.getInitialRevision()
    with open(filepath, "w") as f:
        f.write(
            """\
apiVersion: unfurl/v1alpha1
kind: Manifest
# merge in manifest-template.yaml from spec repo
+include:
  file: manifest-template.yaml
  repository: spec
spec:
  service_template:
    repositories:
      spec:
        url: file:%s
        metadata:
          initial-commit: %s
      instance:
        url: file:.
        metadata:
          initial-commit: %s
"""
            % (relPathToSpecRepo, specInitialCommit, repo.revision)
        )
    repo.repo.index.add(["manifest.yaml"])
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
    manifestContent = """\
  apiVersion: unfurl/v1alpha1
  kind: Manifest
  spec:
    tosca:
      +include: service-template.yaml
  status: {}
    """
    manifestPath = _writeFile(projectdir, "manifest.yaml", manifestContent)
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


def createNewInstance(specRepoDir, targetPath):
    sourceRepo = Repo.findContainingRepo(specRepoDir)
    if not sourceRepo:
        return None, "No repository exists at " + os.path.abspath(specRepoDir)
    if not sourceRepo.isValidSpecRepo():
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
    if not sourceRepo.isValidSpecRepo():
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
