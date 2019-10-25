"""
Project folder structure:

project/spec/.git # has service-template.yaml and manifest-template.yaml
        instances/current/.git # has manifest.yaml
        # subprojects are created by repository declarations in spec or instance
        subproject/spec/
                  instances/current
        # unfurl.yaml merges with ~/.unfurl_home/unfurl.yaml
        unfurl.yaml # might create 'secret' or 'local' subprojects
        revisions/...
"""
import uuid
import os
import os.path
from git import Repo
from . import __version__
from .tosca import TOSCA_VERSION

def writeLocalConfig(projectdir):
  os.makedirs(projectdir)
  filepath = os.path.join(projectdir, 'unfurl.yaml')
  with open(filepath, 'w') as f:
    f.write("""\
unfurl:
  version: %s
instances:
  - file: instances/current/manifest.yaml
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
""" % __version__)
  return filepath

def createHome(path=None):
  """
  Write ~/.unfurl_home/unfurl.yaml if missing
  """
  homedir = path or os.path.expanduser(os.path.join('~', '.unfurl_home'))
  if not os.path.exists(homedir):
    return writeLocalConfig(homedir)
  # XXX when is unfurl.yaml instances deployed?

def createRepo(repotype, gitDir, gitUri=None):
  os.makedirs(gitDir)
  repo = Repo.init( gitDir )
  filename = '.unfurl'
  filepath = os.path.join(gitDir, filename)
  with open(filepath, 'w') as f:
    f.write("""\
  unfurl:
    version: %s
  repo:
    type: %s
    uuid: %s
  """ % (__version__, repotype, uuid.uuid1()))

  repo.index.add([filename])
  repo.index.commit("Initial Commit")
  return repo

def createSpecRepo(gitDir):
  repo = createRepo('spec', gitDir)
  serviceTemplatePath = os.path.join(gitDir, 'service-template.yaml')
  with open(serviceTemplatePath, 'w') as f:
    f.write("""\
tosca_definitions_version: %s
repositories:
  spec:
    url: file:.
    metadata:
      initial-commit: %s
topology_template:
  node_templates:
""" % (TOSCA_VERSION, repo.head.commit.hexsha))
  manifestTemplatePath = os.path.join(gitDir, 'manifest-template.yaml')
  with open(manifestTemplatePath, 'w') as f:
    f.write("""\
  apiVersion: unfurl/v1alpha1
  kind: Manifest
  spec:
    tosca:
      +%include: service-template.yaml
  status: {}
      """)
  repo.index.add(['service-template.yaml', 'manifest-template.yaml'])
  repo.index.commit("Default specification repository boilerplate")
  return repo

def createInstanceRepo(gitDir, specRepo):
  repo = createRepo('instance', gitDir)
  filepath = os.path.join(gitDir, 'manifest.yaml')
  relPathToSpecRepo = os.path.relpath(specRepo.working_tree_dir, os.path.abspath(gitDir))
  specInitialCommit = list(specRepo.iter_commits('HEAD', max_parents=0))[0].hexsha
  with open(filepath, 'w') as f:
    f.write("""\
apiVersion: unfurl/v1alpha1
kind: Manifest
# merge in manifest-template.yaml from spec repo
+%%include:
  file: manifest-template.yaml
  repository: spec
spec:
  tosca:
    repositories:
      spec:
        url: file:%s
        metadata:
          initial-commit: %s
      instance:
        url: file:.
        metadata:
          initial-commit: %s
""" % (relPathToSpecRepo, specInitialCommit, repo.head.commit.hexsha))
  repo.index.add(['manifest.yaml'])
  repo.index.commit("Default instance repository boilerplate")
  return repo

def createProject(projectdir, home=None):
  """
  # creates .unfurl, project/specs/manifest-template.yaml, instances/current/manifest.yaml
  # init git repos with initial commits
  # adds ~/.unfurl_home/unfurl.yaml if missing
  # add project to ~/.unfurl_home/unfurl.yaml
  """
  newHome = createHome(home)
  projectConfigPath = writeLocalConfig(projectdir)
  specRepo = createSpecRepo(os.path.join(projectdir, 'spec'))
  createInstanceRepo(os.path.join(projectdir, 'instances', 'current'), specRepo)
  return newHome, projectConfigPath

def createNewInstance(specRepoDir, targetPath):
  from .repo import Repo
  sourceRepo = Repo.createGitRepoIfExists(specRepoDir)
  if not sourceRepo:
    return None, "No repository exists at " + os.path.abspath(specRepoDir)
  if not sourceRepo.isValidSpecRepo():
    return None, "The respository at '%s' is not valid" % os.path.abspath(specRepoDir)

  # XXX
  #if localEnv.findPathInRepos(targetPath):
  #  return None # "can't create repo in another repo"
  instanceRepo = createInstanceRepo(targetPath, sourceRepo.repo)
  # XXX
  #project = localEnv.findProject(targetPath)
  #if project: # add to project
  return instanceRepo, "created new instance repository at %s" % os.path.abspath(targetPath)

def cloneSpecToNewProject(sourceDir, projectDir):
  from .repo import Repo
  sourceRepo = Repo.createGitRepoIfExists(sourceDir)
  if not sourceRepo:
    return None, "No repository exists at " + os.path.abspath(sourceDir)
  if not sourceRepo.isValidSpecRepo():
    return None, "The respository at '%s' is not valid" % os.path.abspath(sourceDir)

  if os.path.exists(projectDir):
    return None, os.path.abspath(projectDir) + ": file already exists"

  # XXX make sure projectdir is usable
  projectConfigPath = writeLocalConfig(projectDir)
  fullProjectDir = os.path.abspath(projectDir)
  specRepo = sourceRepo.clone(os.path.join(fullProjectDir, 'spec'))
  createInstanceRepo(os.path.join(projectDir, 'instances', 'current'), specRepo.repo)
  return projectConfigPath, "New project created at %s" % fullProjectDir
