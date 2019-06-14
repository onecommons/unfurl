import os
import os.path

def findGitRepo(path, isFile, importLoader):
  """
  Returns (repoURL, filePath, revision)
  RepoURL will be an empty string if it isn't a path to a git repo
  """
  # XXX if importLoader: find the repository based on the path, and check revision
  # hack for now: if ends in .git
  # XXX path can end in #revision, return it
  a, b, c = path.partition('.git/')
  if not b:
    return '', a, ''
  else:
    return a + '.git', c, 'HEAD'

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

  def checkout(self, commitid):
    return workingDir

  def commit(self):
    return commitid

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
  def __init__(self, currentToscaTemplate, localEnv=None):
    self.revisions = {}
    self.currentToscaTemplate = currentToscaTemplate
    self.localEnv = localEnv

  def getRevision(self, repo, commitid):
    key = (repo, commitid)
    if key in self.revisions:
      return self.revisions[key]
    else:
      workingDir = '.' # XXX self.repos[repo].checkout(commitid)
      revision = Revision(self, commitid, workingDir)
      revision.template = self.currentToscaTemplate
      self.revisions[key] = revision
      return revision

  def getRepoWorkingDir(self, uri, commitid=None):
    localEnv = self.localEnv
    if 'uri' not in localEnv:
      repo = Repo.makeRepo(uri)
      localEnv.addRepo(repo)
    return localEnv[uri].checkout(commitid, True)
