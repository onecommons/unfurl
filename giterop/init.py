"""
Project folder structure:

project/spec/.git # has template.yaml and manifest-template.yaml
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
from git import Git
from . import __version__

def writeLocalConfig(projectdir):
  os.makedirs(projectdir)
  filepath = os.path.join(projectdir, 'giterop.yaml')
  with open(filepath, 'w') as f:
    f.write("""\
giterop:
  version: %s
projectroot: true
""" % __version__)

def createHome(path=None):
  """
  Write ~/.giterop_home/giterop.yaml if missing
  """
  writeLocalConfig(path or os.path.expanduser(os.path.join('~', '.giterop_home')))
  # XXX when is giterop.yaml instances deployed?

def createRepo(repotype, gitDir, gitUri=None):
  os.makedirs(gitDir)
  g = Git( gitDir )
  repo = g.init()
  filepath = os.path.join(gitDir, '.giterop')
  with open(filepath, 'w') as f:
    f.write("""\
  giterop:
    version: %s
  repo:
    type: %s
    uuid: %s
  """ % (__version__, repotype, uuid.uuid1()))
  repo.index.add([filepath])
  repo.index.commit("Initial Commit")
  return repo

def createSpecRepo(gitDir):
  repo = createRepo('spec', gitDir)
  filepath = os.path.join(gitDir, 'service-template.yaml')
  with open(filepath, 'w') as f:
    f.write("""\
tosca_definitions_version: tosca_simple_yaml_1_0
repositories:
  spec:
    url: .
    initial-commit: %s
""" % repo.head.ref.hexsha)
  filepath = os.path.join(gitDir, 'manifest-template.yaml')
  with open(filepath, 'w') as f:
    f.write("""\
  apiVersion: giterops/v1alpha1
  kind: Manifest
  spec:
    tosca:
      +%include: service-template.yaml
  status: {}
      """)
  # XXX add service-template.yaml
  repo.index.add([filepath])
  repo.index.commit("Default Boilerplate")

def createInstanceRepo(gitDir, specRepo):
  repo = createRepo('instance', gitDir)
  filepath = os.path.join(gitDir, 'manifest.yaml')
  with open(filepath, 'w') as f:
    f.write("""\
apiVersion: giterops/v1alpha1
kind: Manifest
# merge in manifest-template.yaml from spec repo
# (we need to declare the repository inline since the configuration hasn't been loaded yet)
# we include initial-commit so the repo could be reconstructed solely from this instance repo
+%include:
  file: manifest-template.yaml
  repository:
    name: spec
    url: ../../spec
    initial-commit: %s
spec:
  tosca:
    # add this repository to the list
    repositories:
      instance:
        url: .
        initial-commit: %s
""" % (specRepo.head.ref.hexsha, repo.head.ref.hexsha))

def createProject(projectdir, home=None):
  """
  # creates .giterop, project/specs/manifest-template.yaml, instances/current/manifest.yaml
  # init git repos with initial commits
  # adds ~/.giterop_home/giterop.yaml if missing
  # add project to ~/.giterop_home/giterop.yaml
  """
  createHome(home)
  writeLocalConfig(projectdir)
  specRepo = createSpecRepo(os.path.join(projectdir, 'spec'))
  createInstanceRepo(os.path.join(projectdir, 'instances', 'current'), specRepo)
