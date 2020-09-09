import os
import os.path
import git
from git.repo.fun import is_git_dir
import logging
from six.moves.urllib.parse import urlparse

logger = logging.getLogger("unfurl")


def normalizeGitUrl(url):
    if url.startswith("git-local://"):  # truncate url after commit digest
        return "git-local://" + urlparse(url).netloc.partition(":")[0]
    return url


def findGitRepo(path, isFile=True, importLoader=None):
    """
  Returns (repoURL, filePath, revision)
  RepoURL will be an empty string if it isn't a path to a git repo
  """
    parts = urlparse(path)
    if parts.scheme == "git-local":
        return parts.scheme + "://" + parts.netloc, parts.path[1:], ""

    # XXX if importLoader: find the repository based on the path, and check revision
    # hack for now: if ends in .git
    # XXX path can end in #revision, return it
    # see https://docs.docker.com/engine/reference/commandline/build/ for git urls like:
    # myrepo.git#mybranch, myrepo.git#pull/42/head, myrepo.git#:myfolder, myrepo.git#master:myfolder
    a, b, c = path.partition(".git/")
    if not b:
        return "", a, ""
    else:
        return a + ".git", c, ""


class _ProgressPrinter(git.RemoteProgress):
    def update(self, op_code, cur_count, max_count=None, message=""):
        # print update to stdout but only if logging is INFO or more verbose
        if message and logger.getEffectiveLevel() <= logging.INFO:
            print("fetching from %s, received: %s " % (self.gitUrl, message))


class Repo(object):
    @staticmethod
    def findContainingRepo(rootDir, gitDir=".git"):
        """
    Walk parents looking for a git repository.
    """
        current = os.path.abspath(rootDir)
        while current and current != os.sep:
            if is_git_dir(os.path.join(current, gitDir)):
                return GitRepo(git.Repo(current))
            current = os.path.dirname(current)
        return None

    @staticmethod
    def findGitWorkingDirs(rootDir, gitDir=".git"):
        workingDirs = {}
        for root, dirs, files in os.walk(rootDir):
            if Repo.updateGitWorkingDirs(workingDirs, root, dirs, gitDir):
                del dirs[:]  # don't visit sub directories
        return workingDirs

    @staticmethod
    def updateGitWorkingDirs(workingDirs, root, dirs, gitDir=".git"):
        if gitDir in dirs and is_git_dir(os.path.join(root, gitDir)):
            assert os.path.isdir(root), root
            repo = GitRepo(git.Repo(root))
            key = os.path.abspath(root)
            workingDirs[key] = (repo.url, repo)
            return key
        return None

    def findPath(self, path, importLoader=None):
        base = self.workingDir
        if not base:
            return None, None, None
        repoRoot = os.path.abspath(base)
        abspath = os.path.abspath(path).rstrip("/")
        if repoRoot in abspath:
            # XXX find pinned
            # if importLoader:
            #   revision = importLoader.getRevision(self)
            # else:
            if True:
                revision = self.revision
            bare = not self.workingDir or revision != self.revision
            return abspath[len(repoRoot) + 1 :], revision, bare
        return None, None, None

    @classmethod
    def createWorkingDir(cls, gitUrl, localRepoPath, revision="HEAD"):
        empty_repo = git.Repo.init(localRepoPath)
        origin = empty_repo.create_remote("origin", gitUrl)
        progress = _ProgressPrinter()
        progress.gitUrl = gitUrl
        for fetch_info in origin.fetch(progress=progress):
            # e.g. origin/master to 29373a553e45bcd714e993c3600e867cfbd7b9eb
            logger.info(
                "Updated %s with %s at %s"
                % (localRepoPath, fetch_info.ref, fetch_info.commit)
            )

        # XXX don't assume "master"
        empty_repo.create_head("master", origin.refs.master).set_tracking_branch(
            origin.refs.master
        ).checkout()
        return GitRepo(empty_repo)


class GitRepo(Repo):
    def __init__(self, gitrepo):
        self.repo = gitrepo
        self.url = self.workingDir or gitrepo.git_dir
        if gitrepo.remotes:
            # note: these might not look like absolute urls, e.g. git@github.com:onecommons/unfurl.git
            try:
                remote = gitrepo.remotes["origin"]
            except:
                remote = gitrepo.remotes[0]
            self.url = remote.url

    @property
    def workingDir(self):
        dir = self.repo.working_tree_dir
        if not dir or dir[-1] == "/":
            return dir
        else:
            return dir + "/"

    @property
    def revision(self):
        return self.repo.head.commit.hexsha

    def findExcludedDirs(self, root):
        root = os.path.relpath(root, self.workingDir)
        status, stdout, stderr = self.runCmd(
            [
                "ls-files",
                "--exclude-standard",
                "-o",
                "-i",
                "--full-name",
                "--directory",
                root,
            ]
        )
        for file in stdout.splitlines():
            path = os.path.join(self.workingDir, file)
            yield path

    def runCmd(self, args, **kw):
        """
    :return:
      tuple(int(status), str(stdout), str(stderr))
    """
        gitcmd = self.repo.git
        call = [gitcmd.GIT_PYTHON_GIT_EXECUTABLE]
        # add persistent git options
        call.extend(gitcmd._persistent_git_options)
        call.extend(list(args))

        # note: sets cwd to working_dir
        return gitcmd.execute(
            call, with_exceptions=False, with_extended_output=True, **kw
        )

    def show(self, path, commitId):
        if os.path.abspath(path) and self.workingDir:
            path = path[len(self.workingDir) :]
        # XXX this won't work if path is in a submodule
        # if in path startswith a submodule: git log -1 -p [commitid] --  [submodule]
        # submoduleCommit = re."\+Subproject commit (.+)".group(1)
        # return self.repo.submodules[submodule].git.show(submoduleCommit+':'+path[len(submodule)+1:])
        return self.repo.git.show(commitId + ":" + path)

    def checkout(self, revision=""):
        # if revision isn't specified and repo is not pinned:
        #  save the ref of current head
        self.repo.git.checkout(revision)
        logger.info(
            "checking out '%s' at %s to %s",
            self.url,
            revision or "HEAD",
            self.workingDir,
        )
        return self.workingDir

    def getInitialRevision(self):
        firstCommit = next(self.repo.iter_commits("HEAD", max_parents=0))
        return firstCommit.hexsha

    def commitFiles(self, files, msg):
        # note: this will also commit existing changes in the index
        index = self.repo.index
        index.add([os.path.abspath(f) for f in files])
        return index.commit(msg)

    def isDirty(self, untracked_files=False):
        # diff = self.repo.git.diff()  # "--abbrev=40", "--full-index", "--raw")
        return self.repo.is_dirty(untracked_files=untracked_files)

    def clone(self, newPath):
        cloned = self.repo.clone(newPath)
        return GitRepo(cloned)

    def getGitLocalUrl(self, path, name=""):
        return "git-local://%s:%s/%s" % (self.getInitialRevision(), name, path)

    # XXX: def getDependentRepos()
    # XXX: def canManage()

    # def canMakeClean(self):
    #     for repo in self.getDependentRepos():
    #         if not repo.canMakeClean():
    #             return False
    #         elif repo.isDirty() and not self.canManage(repo):
    #             return False
    #     return True
    #
    # def _commitAll(self, parent=None):
    #     committed = []
    #     for repo in self.getDependentRepos():
    #         if repo.isDirty():
    #             assert self.canManage(repo)
    #             repo._commitAll(self)
    #             committed.append(repo)
    #     self.updateChildCommits(committed)
    #     self._commit()
    #
    # def getDirtyDependents(self):
    #     for repo in self.getDependentRepos():
    #         if repo.isDirty():
    #             yield repo

    # XXX unused.. currently yamlmanifest.commitJob() calls commitFiles()
    # def commit(self):
    #     # before run referenced dirty repos should be committed?
    #     # at the very least the state of any declared repo should be saved
    #     # otherwise two different runs of the same commit could pull different versions
    #     # this is true for the spec repos also -- save in spec's manifest-template?
    #     repo = self.repo
    #     repo.index.add("*")
    #     # commit the manifest first so we can get a commit ref for the changerecord
    #     commit = repo.git.commit("")
    #     changeFiles = self.manifest.saveChanges(commit.hexsha)
    #     repo.index.add(changeFiles)
    #     repo.git.commit("")


# class SimpleRepo(Repo):
#   def __init__(self, lastCommitId):
#     self.lastCommitId = int(lastCommitId or 0)
#
#   def checkout(self, commitid, useCurrent):
#     if useCurrent and commitid == self.lastCommitId:
#       return self.workingDir
#     return './revisions/{commitid}/files'
#
#   def commit(self):
#     self.lastCommitId += 1
#     # copy current workingDir to /revisions/{commitid}/files
#     return self.lastCommitId


class RevisionManager(object):
    def __init__(self, manifest, localEnv=None):
        self.manifest = manifest
        self.revisions = {manifest.specDigest: manifest}
        self.localEnv = localEnv

    def getRevision(self, change):
        digest = change["specDigest"]
        commitid = change["startCommit"]
        if digest in self.revisions:
            return self.revisions[digest]
        else:
            from .manifest import SnapShotManifest

            manifest = SnapShotManifest(self.manifest, commitid)
            self.revisions[digest] = manifest
            return manifest
