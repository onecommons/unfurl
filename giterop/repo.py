import os
import os.path
import git
from git.repo.fun import (
    find_worktree_git_dir
)
import logging
logger = logging.getLogger('giterop')

def findGitRepo(path, isFile=True, importLoader=None):
  """
  Returns (repoURL, filePath, revision)
  RepoURL will be an empty string if it isn't a path to a git repo
  """
  # XXX if importLoader: find the repository based on the path, and check revision
  # hack for now: if ends in .git
  # XXX path can end in #revision, return it
  # see https://docs.docker.com/engine/reference/commandline/build/ for git urls like:
  # myrepo.git#mybranch, myrepo.git#pull/42/head, myrepo.git#:myfolder, myrepo.git#master:myfolder
  a, b, c = path.partition('.git/')
  if not b:
    return '', a, ''
  else:
    return a + '.git', c, ''

class Repo(object):
  @staticmethod
  def makeRepo(url, repotype, basedir):
    # XXX git or simple based on url
    # basedir is the project/subproject root or local-config root dependening where the import definition lives
    if repotype=='instance':
      dir = os.path.join(basedir, 'instances', 'current')
    else:
      dir = os.path.join(basedir, repotype)
    # XXX error if exists, else mkdirs
    return SimpleRepo(url, dir)

  @staticmethod
  def findGitWorkingDirs(rootDir, gitDir='.git'):
    workingDirs = {}
    for root, dirs, files in os.walk(rootDir):
      if gitDir in dirs:
        dirs.clear()  # don't visit sub directories
        repo = GitRepo(git.Repo(root))
        workingDirs[root] = (repo.url, repo)
    return workingDirs

  @staticmethod
  def createGitRepoIfExists(rootDir, gitDir='.git'):
    if find_worktree_git_dir(os.path.join(rootDir, gitDir)):
      return GitRepo(git.Repo(rootDir))
    return None

  def findPath(self, path, importLoader=None):
    base = self.workingDir # XXX: or self.baseDir
    if not base:
      return None, None, None
    repoRoot = os.path.abspath(base)
    abspath = os.path.abspath(path)
    if repoRoot[-1] != '/':
      repoRoot += '/'
    if repoRoot in abspath:
      # XXX find pinned
      # if importLoader:
      #   revision = importLoader.getRevision(self)
      # else:
      if True:
        revision = self.revision
      bare = not self.workingDir or revision != self.revision
      return abspath[len(repoRoot):], revision, bare
    return None, None, None

  @classmethod
  def createWorkingDir(cls, gitUrl, localRepoPath, revision='HEAD'):
    empty_repo = git.Repo.init(localRepoPath)
    origin = empty_repo.create_remote('origin', gitUrl)
    empty_repo.create_head('master', origin.refs.master)
    empty_repo.set_tracking_branch(origin.refs.master).checkout(revision)
    return GitRepo(empty_repo)

class GitRepo(Repo):
  def __init__(self, gitrepo):
    self.repo = gitrepo
    self.url = self.workingDir
    if gitrepo.remotes:
      try:
        remote = gitrepo.remotes['origin']
      except:
        remote = gitrepo.remotes[0]
      self.url = remote.url

  @property
  def workingDir(self):
    return self.repo.working_tree_dir

  @property
  def revision(self):
    return self.repo.head.commit.hexsha

  def show(self, relPath, commitId):
    return self.repo.git.show(commitId+':'+relPath)

  def checkout(self, revision=''):
    # if revision isn't specified and repo is not pinned:
    #  save the ref of current head
    self.repo.git.checkout(revision)
    logger.info("checking out '%s' at %s to %s", self.url, revision or 'HEAD', self.workingDir)
    return self.workingDir

  def canMakeClean(self):
    for repo in self.getDependentRepos():
      if not repo.canMakeClean():
        return False
      elif repo.isDirty() and not self.canManage(repo):
        return False
    return True

  def _commitAll(self, parent=None):
    committed = []
    for repo in self.getDependentRepos():
      if repo.isDirty():
        assert self.canManage(repo)
        repo._commitAll(self)
        committed.append(repo)
    self.updateChildCommits(committed)
    self._commit()

  def getDirtyDependents(self):
    for repo in self.getDependentRepos():
      if repo.isDirty():
        yield repo

  def commit(self):
    # before run referenced dirty repos should be committed?
    # at the very least the state of any declared repo should be saved
    # otherwise two different runs of the same commit could pull different versions
    # this is true for the spec repos also -- save in spec's manifest-template?
    repo = self.gitRepo
    repo.index.add('*')
    # commit the manifest first so we can get a commit ref for the changerecord
    commit = repo.git.commit('')
    changeFiles = self.manifest.saveChanges(commit.hexsha)
    repo.index.add(changeFiles)
    repo.git.commit('')

class SimpleRepo(Repo):
  def __init__(self, lastCommitId):
    self.lastCommitId = int(lastCommitId or 0)

  def checkout(self, commitid, useCurrent):
    if useCurrent and commitid == self.lastCommitId:
      return self.workingDir
    return './revisions/{commitid}/files'

  def commit(self):
    self.lastCommitId += 1
    # copy current workingDir to /revisions/{commitid}/files
    return self.lastCommitId

class Revision(object):
  def __init__(self, revisionManager, commitid, workingDir):
    self.revisionManager = revisionManager
    self.workingDir = workingDir
    self.commitId = commitid

class RevisionManager(object):
  def __init__(self, manifest, localEnv=None):
    self.manifest = manifest
    # XXX currentCommitId
    self.revisions = {manifest.currentCommitId: manifest}
    self.localEnv = localEnv

  def getRevision(self, commitid):
    if commitid in self.revisions:
      return self.revisions[commitid]
    else:
      from .manifest import SnapShotManifest
      manifest = SnapShotManifest(self.manifest, commitid)
      self.revisions[commitid] = manifest
      return manifest
