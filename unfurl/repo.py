# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import os
import os.path
import git
from git.repo.fun import is_git_dir
import logging
from six.moves.urllib.parse import urlparse
from unfurl.util import UnfurlError
import toscaparser.repositories
from ruamel.yaml.comments import CommentedMap

logger = logging.getLogger("unfurl")


def normalizeGitUrl(url):
    if url.startswith("git-local://"):  # truncate url after commit digest
        return "git-local://" + urlparse(url).netloc.partition(":")[0]

    if "://" not in url:  # not an absolute URL, convert some common patterns
        if url.startswith("/"):
            return "file://" + url
        elif "@" in url:  # scp style used by git: user@server:project.git
            # convert to ssh://user@server/project.git
            return "ssh://" + url.replace(":", "/", 1)
    return url


def isURLorGitPath(url):
    if "://" in url and not url.startswith("file:"):
        return True
    if "@" in url:
        return True
    candidate, sep, frag = url.partition("#")
    if candidate.rstrip("/").endswith(".git"):
        return True
    return False


def splitGitUrl(url):
    """
    Returns (repoURL, filePath, revision)
    RepoURL will be an empty string if it isn't a path to a git repo
    """
    parts = urlparse(url)
    if parts.scheme == "git-local":
        return parts.scheme + "://" + parts.netloc, parts.path[1:], parts.fragment

    if parts.fragment:
        # treat fragment as a git revision spec; see https://git-scm.com/docs/gitrevisions
        # or https://docs.docker.com/engine/reference/commandline/build/#git-repositories
        # just support <ref>:<path> for now
        # e.g. myrepo.git#mybranch, myrepo.git#pull/42/head, myrepo.git#:myfolder, myrepo.git#master:myfolder
        revision, sep, path = parts.fragment.partition(":")
        giturl, sep, frag = url.partition("#")
        return giturl, path, revision
    return url, "", ""


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
            workingDirs[key] = repo.asRepoView()
            return key
        return None

    @staticmethod
    def ignoreDir(dir):
        parent = Repo.findContainingRepo(os.path.dirname(dir))
        if parent:
            path = parent.findRepoPath(dir)
            if path:  # can be None if dir is already ignored
                parent.addToLocalGitIgnore(path)
                return path
        return None

    def findRepoPath(self, path):
        localPath = self.findPath(path)[0]
        if localPath is not None and not self.isPathExcluded(localPath):
            return localPath
        return None

    def isPathExcluded(self, localPath):
        return False

    def findPath(self, path, importLoader=None):
        base = self.workingDir
        if not base:  # XXX support bare repos
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

    def asRepoView(self, name=""):
        return RepoView(dict(name=name, url=self.url), self)

    def isLocalOnly(self):
        return self.url.startswith("git-local://") or os.path.isabs(self.url)

    @staticmethod
    def getPathForGitRepo(gitUrl):
        parts = urlparse(gitUrl)
        if parts.scheme == "git-local":
            # e.g. extract spec from git-local://0cfeee6571c4276ce1a63dc37aa8cbf8b8085d60:spec
            name = parts.netloc.partition(":")[1]
        else:
            # e.g. extract tosca-parser from https://github.com/onecommons/tosca-parser.git
            name = (
                os.path.splitext(os.path.basename(parts.path.strip("/")))[0]
                or parts.netloc
            )
        assert not name.endswith(".git"), name
        return name

    @classmethod
    def createWorkingDir(cls, gitUrl, localRepoPath, revision=None):
        localRepoPath = localRepoPath or "."
        if os.path.exists(localRepoPath):
            if not os.path.isdir(localRepoPath) or os.listdir(localRepoPath):
                raise UnfurlError(
                    "couldn't create directory, it already exists and isn't empty: %s"
                    % localRepoPath
                )
        branch = revision or "master"  # XXX
        logger.info("Fetching %s (%s) to %s", gitUrl, branch, localRepoPath)
        progress = _ProgressPrinter()
        progress.gitUrl = gitUrl
        try:
            repo = git.Repo.clone_from(
                gitUrl, localRepoPath, progress, branch=branch, recurse_submodules=True
            )
        except git.exc.GitCommandError as err:
            raise UnfurlError(
                "couldn't create working dir, clone failed: " + err._cmdline
            )
        Repo.ignoreDir(localRepoPath)
        return GitRepo(repo)


class RepoView(object):
    # view of Repo optionally filtered by path
    # XXX and revision too
    def __init__(self, repository, repo, path=""):
        if isinstance(repository, dict):
            # required keys: name, url
            tpl = repository.copy()
            name = tpl.pop("name")
            tpl["url"] = normalizeGitUrl(tpl["url"])
            repository = toscaparser.repositories.Repository(name, tpl)
        self.repository = repository
        self.repo = repo
        self.path = path
        self.readOnly = not repo

    @property
    def workingDir(self):
        if self.repo:
            return os.path.join(self.repo.workingDir, self.path)
        else:
            return os.path.join(self.repository.url, self.path)

    @property
    def name(self):
        return self.repository.name if self.repository else ""

    @property
    def url(self):
        return self.repository.url if self.repository else self.repo.url

    @property
    def origin(self):
        if (
            self.repo
            and normalizeGitUrl(self.repo.url) != self.url
            and self.repo.url != self.repo.workingDir
        ):
            return self.repo.url
        return ""

    def isDirty(self):
        if self.readOnly:
            return False
        return self.repo.isDirty(untracked_files=True, path=self.path)

    def addAll(self):
        self.repo.repo.git.add("--all", self.path or ".")

    def commit(self, message, addAll=False):
        if addAll:
            self.addAll()
        return self.repo.repo.index.commit(message)

    def status(self):
        return self.repo.runCmd(["status", self.path or "."])[1]

    def getInitialRevision(self):
        if not self.repo:
            return ""
        return self.repo.getInitialRevision()

    def getCurrentRevision(self):
        if not self.repo:
            return ""
        if self.isDirty():
            return self.repo.revision + "-dirty"
        else:
            return self.repo.revision

    def lock(self):
        record = CommentedMap(
            [
                ("name", self.name),
                ("url", self.url),
                ("revision", self.getCurrentRevision()),
                ("initial", self.getInitialRevision()),
            ]
        )
        if self.origin:
            record["origin"] = self.origin
        return record


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

    def resolveRevSpec(self, revision):
        try:
            return self.repo.commit(revision).hexsha
        except:
            return None

    def getUrlWithPath(self, path):
        if isURLorGitPath(self.url):
            if os.path.isabs(path):
                # get path relative to repository's root
                path = os.path.relpath(path, self.workingDir)
            return self.url + "#:" + path
        else:
            return self.getGitLocalUrl(path)

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

    def isPathExcluded(self, localPath):
        # XXX cache and test
        # excluded = list(self.findExcludedDirs(self.workingDir))
        # success error code means it's ignored
        return not self.runCmd(["check-ignore", "-q", localPath])[0]

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

    def addToLocalGitIgnore(self, rule):
        with open(os.path.join(self.repo.git_dir, "info", "exclude"), "a") as f:
            f.write("\n" + rule + "\n")

    def show(self, path, commitId):
        if self.workingDir and os.path.isabs(path):
            path = os.path.abspath(path)[len(self.workingDir) :]
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

    def addSubModule(self, gitDir):
        gitDir = os.path.abspath(gitDir)
        status, stdout, stderr = self.runCmd(["submodule", "add", gitDir])
        success = not status
        if success:
            logging.debug("added submodule %s: %s %s", gitDir, stdout, stderr)
        else:
            logging.error("failed to add submodule %s: %s %s", gitDir, stdout, stderr)
        return success

    def getInitialRevision(self):
        firstCommit = next(self.repo.iter_commits("HEAD", max_parents=0))
        return firstCommit.hexsha

    def addAll(self, path="."):
        path = os.path.relpath(path, self.workingDir)
        self.repo.git.add("--all", path)

    def commitFiles(self, files, msg):
        # note: this will also commit existing changes in the index
        index = self.repo.index
        index.add([os.path.abspath(f) for f in files])
        return index.commit(msg)

    def isDirty(self, untracked_files=False, path=None):
        # diff = self.repo.git.diff()  # "--abbrev=40", "--full-index", "--raw")
        # https://gitpython.readthedocs.io/en/stable/reference.html?highlight=is_dirty#git.repo.base.Repo.is_dirty
        return self.repo.is_dirty(untracked_files=untracked_files, path=path or None)

    def clone(self, newPath):
        # note: repo.clone uses bare path, which breaks submodule path resolution
        cloned = git.Repo.clone_from(
            self.workingDir, os.path.abspath(newPath), recurse_submodules=True
        )
        Repo.ignoreDir(newPath)
        return GitRepo(cloned)

    def getGitLocalUrl(self, path, name=""):
        if os.path.isabs(path):
            # get path relative to repository's root
            path = os.path.relpath(path, self.workingDir)
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


class RevisionManager(object):
    def __init__(self, manifest, localEnv=None):
        self.manifest = manifest
        self.revisions = None
        self.localEnv = localEnv

    def getRevision(self, change):
        if self.revisions is None:
            self.revisions = {self.manifest.specDigest: self.manifest}
        digest = change["specDigest"]
        commitid = change["startCommit"]
        if digest in self.revisions:
            return self.revisions[digest]
        else:
            from .manifest import SnapShotManifest

            manifest = SnapShotManifest(self.manifest, commitid)
            self.revisions[digest] = manifest
            return manifest
