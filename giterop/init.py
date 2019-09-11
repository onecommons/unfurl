"""
Project folder structure:

project/spec/.git # has service-template.yaml and manifest-template.yaml
        instances/current/.git # has manifest.yaml
        # subprojects are created by repository declarations in spec or instance
        subproject/spec/
                  instances/current
        # giterop.yaml merges with ~/.giterop_home/giterop.yaml
        giterop.yaml # might create 'secret' or 'local' subprojects
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
  filepath = os.path.join(projectdir, 'giterop.yaml')
  with open(filepath, 'w') as f:
    f.write("""\
giterop:
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
  Write ~/.giterop_home/giterop.yaml if missing
  """
  homedir = path or os.path.expanduser(os.path.join('~', '.giterop_home'))
  if not os.path.exists(homedir):
    return writeLocalConfig(homedir)
  # XXX when is giterop.yaml instances deployed?

def createRepo(repotype, gitDir, gitUri=None):
  os.makedirs(gitDir)
  repo = Repo.init( gitDir )
  filename = '.giterop'
  filepath = os.path.join(gitDir, filename)
  with open(filepath, 'w') as f:
    f.write("""\
  giterop:
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
  apiVersion: giterops/v1alpha1
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
  with open(filepath, 'w') as f:
    f.write("""\
apiVersion: giterops/v1alpha1
kind: Manifest
# merge in manifest-template.yaml from spec repo
+%%include:
  file: manifest-template.yaml
  repository: spec
spec:
  tosca:
    repositories:
      spec:
        url: file:../../spec
        metadata:
          initial-commit: %s
      instance:
        url: file:.
        metadata:
          initial-commit: %s
""" % (specRepo.head.commit.hexsha, repo.head.commit.hexsha))
  repo.index.add(['manifest.yaml'])
  repo.index.commit("Default instance repository boilerplate")
  return repo

def createProject(projectdir, home=None):
  """
  # creates .giterop, project/specs/manifest-template.yaml, instances/current/manifest.yaml
  # init git repos with initial commits
  # adds ~/.giterop_home/giterop.yaml if missing
  # add project to ~/.giterop_home/giterop.yaml
  """
  newHome = createHome(home)
  projectConfigPath = writeLocalConfig(projectdir)
  specRepo = createSpecRepo(os.path.join(projectdir, 'spec'))
  createInstanceRepo(os.path.join(projectdir, 'instances', 'current'), specRepo)
  return newHome, projectConfigPath
