# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import os
import os.path
from pathlib import Path
import git
from git.repo.fun import is_git_dir
import logging
from six.moves.urllib.parse import urlparse
from .util import UnfurlError, save_to_file, to_text
import toscaparser.repositories
from ruamel.yaml.comments import CommentedMap

logger = logging.getLogger("unfurl")


def normalize_git_url(url):
    if url.startswith("git-local://"):  # truncate url after commit digest
        return "git-local://" + urlparse(url).netloc.partition(":")[0]

    if "://" not in url:  # not an absolute URL, convert some common patterns
        if url.startswith("/"):
            return "file://" + url
        elif "@" in url:  # scp style used by git: user@server:project.git
            # convert to ssh://user@server/project.git
            return "ssh://" + url.replace(":", "/", 1)
    return url


def is_url_or_git_path(url):
    if "://" in url and not url.startswith("file:"):
        return True
    if "@" in url:
        return True
    candidate, sep, frag = url.partition("#")
    if frag or candidate.rstrip("/").endswith(".git"):
        return True
    return False


def split_git_url(url):
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
        # we use print instead of logging because we don't want to clutter logs with this message
        if message and logger.getEffectiveLevel() <= logging.INFO:
            print(f"fetching from {self.gitUrl}, received: {message} ")


class Repo:
    @staticmethod
    def find_containing_repo(rootDir, gitDir=".git"):
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
    def find_git_working_dirs(rootDir, gitDir=".git"):
        working_dirs = {}
        for root, dirs, files in os.walk(rootDir):
            if Repo.update_git_working_dirs(working_dirs, root, dirs, gitDir):
                del dirs[:]  # don't visit sub directories
        return working_dirs

    @staticmethod
    def update_git_working_dirs(working_dirs, root, dirs, gitDir=".git"):
        if gitDir in dirs and is_git_dir(os.path.join(root, gitDir)):
            assert os.path.isdir(root), root
            repo = GitRepo(git.Repo(root))
            key = os.path.abspath(root)
            working_dirs[key] = repo.as_repo_view()
            return key
        return None

    @staticmethod
    def ignore_dir(dir):
        parent = Repo.find_containing_repo(os.path.dirname(dir))
        if parent:
            path = parent.find_repo_path(dir)
            if path:  # can be None if dir is already ignored
                parent.add_to_local_git_ignore("/" + path)
                return path
        return None

    def find_repo_path(self, path):
        localPath = self.find_path(path)[0]
        if localPath is not None and not self.is_path_excluded(localPath):
            return localPath
        return None

    def is_path_excluded(self, localPath):
        return False

    def find_path(self, path, importLoader=None):
        base = self.working_dir
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
            bare = not self.working_dir or revision != self.revision
            return abspath[len(repoRoot) + 1 :], revision, bare
        return None, None, None

    def as_repo_view(self, name=""):
        return RepoView(dict(name=name, url=self.url), self)

    def is_local_only(self):
        return self.url.startswith("git-local://") or os.path.isabs(self.url)

    @staticmethod
    def get_path_for_git_repo(gitUrl):
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
    def create_working_dir(cls, gitUrl, localRepoPath, revision=None):
        localRepoPath = localRepoPath or "."
        if os.path.exists(localRepoPath):
            if not os.path.isdir(localRepoPath) or os.listdir(localRepoPath):
                raise UnfurlError(
                    f"couldn't create directory, it already exists and isn't empty: {localRepoPath}"
                )
        logger.info("Fetching %s %s to %s", gitUrl, revision or "", localRepoPath)
        progress = _ProgressPrinter()
        progress.gitUrl = gitUrl
        try:
            kwargs = dict(recurse_submodules=True)
            if revision:
                kwargs["branch"] = revision
            repo = git.Repo.clone_from(gitUrl, localRepoPath, progress, **kwargs)
        except git.exc.GitCommandError as err:
            raise UnfurlError(
                f'couldn\'t create working directory, clone failed: "{err._cmdline}"\nTry re-running that command to diagnose the problem.'
            )
        Repo.ignore_dir(localRepoPath)
        return GitRepo(repo)


def commit_secrets(working_dir, yaml):
    vault = yaml and getattr(yaml.representer, "vault", None)
    if not vault:
        return []
    saved = []
    for filepath, dotsecrets in find_dirty_secrets(working_dir):
        with open(filepath, "r") as vf:
            vaultContents = vf.read()
        encoding = None if vaultContents.startswith("$ANSIBLE_VAULT;") else "vault"
        secretpath = dotsecrets / filepath.name
        logger.verbose("encrypting file to %s with %s", secretpath, vault.secrets[0][0])
        save_to_file(str(secretpath), vaultContents, yaml, encoding)
        saved.append(secretpath)
    return saved


def find_dirty_secrets(working_dir):
    # compare .secrets with secrets
    for root, dirs, files in os.walk(working_dir):
        if "secrets" not in Path(root).parts:
            continue
        for filename in files:
            dotsecrets = Path(root.replace("secrets", ".secrets"))
            filepath = Path(root) / filename
            if (
                not dotsecrets.is_dir()
                or filename not in list([p.name for p in dotsecrets.iterdir()])
                or filepath.stat().st_mtime > (dotsecrets / filename).stat().st_mtime
            ):
                yield filepath, dotsecrets


class RepoView:
    # view of Repo optionally filtered by path
    # XXX and revision too
    def __init__(self, repository, repo, path=""):
        if isinstance(repository, dict):
            # required keys: name, url
            tpl = repository.copy()
            name = tpl.pop("name")
            tpl["url"] = normalize_git_url(tpl["url"])
            repository = toscaparser.repositories.Repository(name, tpl)
        assert repository or repo
        self.repository = repository
        self.repo = repo
        self.path = path
        if repo and path and repository:
            self.repository.url = repo.get_url_with_path(path)
        self.readOnly = not repo
        self.yaml = None

    @property
    def working_dir(self):
        if self.repo:
            return os.path.join(self.repo.working_dir, self.path)
        else:
            return os.path.join(self.repository.url, self.path)

    @property
    def name(self):
        return self.repository.name if self.repository else ""

    @property
    def url(self):
        return self.repository.url if self.repository else self.repo.url

    def is_local_only(self):
        # if it doesn't have a repo then it most be local
        return not self.repo or self.repo.is_local_only()

    @property
    def origin(self):
        if (
            self.repo
            and normalize_git_url(self.repo.url) != split_git_url(self.url)[0]
            and self.repo.url != self.repo.working_dir
        ):
            return self.repo.url
        return ""

    def is_dirty(self):
        if self.readOnly:
            return False
        for filepath, dotsecrets in find_dirty_secrets(self.working_dir):
            return True
        return self.repo.is_dirty(untracked_files=True, path=self.path)

    def add_all(self):
        assert not self.readOnly
        self.repo.repo.git.add("--all", self.path or ".")

    def load_secrets(self, _loader):
        logger.trace("looking for secrets %s", self.working_dir)
        for root, dirs, files in os.walk(self.working_dir):
            if ".secrets" not in Path(root).parts:
                continue
            logger.trace("checking if secret files where changed or added %s", files)
            for filename in files:
                secretsdir = Path(root.replace(".secrets", "secrets"))
                filepath = Path(root) / filename
                stinfo = filepath.stat()
                target = secretsdir / filename
                if not target.is_file() or stinfo.st_mtime > target.stat().st_mtime:
                    target = secretsdir / filename
                    try:
                        contents = _loader.load_from_file(str(filepath))
                    except Exception as err:
                        logger.warning("could not decrypt %s: %s", filepath, err)
                        continue
                    with open(str(target), "w") as f:
                        f.write(contents)
                    os.utime(target, (stinfo.st_atime, stinfo.st_mtime))
                    logger.verbose("decrypted secret file to %s", target)

    def save_secrets(self):
        return commit_secrets(self.working_dir, self.yaml)

    def commit(self, message, addAll=False):
        assert not self.readOnly
        if self.yaml:
            for saved in self.save_secrets():
                self.repo.repo.git.add(str(saved.relative_to(self.repo.working_dir)))
        if addAll:
            self.add_all()
        self.repo.repo.index.commit(message)
        return 1

    def get_default_commit_message(self):
        return "Commit by Unfurl"

    def git_status(self):
        assert not self.readOnly
        return self.repo.run_cmd(["status", self.path or "."])[1]

    def _secrets_status(self):
        modified = "\n   ".join(
            [
                str(filepath.relative_to(self.repo.working_dir))
                for filepath, dotsecrets in find_dirty_secrets(self.working_dir)
            ]
        )
        if modified:
            return f"\n\nSecrets to be committed:\n   {modified}"
        return ""

    def get_repo_status(self, dirty=False):
        if self.repo and (not dirty or self.is_dirty()):
            git_status = self.git_status()
            if self.name:
                header = f"for {self.name} at {self.working_dir}"
            else:
                header = f"for {self.working_dir}"
            secrets_status = self._secrets_status()
            return f"Status {header}:\n{git_status}{secrets_status}\n\n"
        else:
            return ""

    def get_initial_revision(self):
        if not self.repo:
            return ""
        return self.repo.get_initial_revision()

    def get_current_revision(self):
        if not self.repo:
            return ""
        if self.is_dirty():
            return self.repo.revision + "-dirty"
        else:
            return self.repo.revision

    def lock(self):
        record = CommentedMap(
            [
                ("url", self.url),
                ("revision", self.get_current_revision()),
                ("initial", self.get_initial_revision()),
            ]
        )
        if self.name:
            record["name"] = self.name
        if self.origin:
            record["origin"] = self.origin
        return record


class GitRepo(Repo):
    def __init__(self, gitrepo):
        self.repo = gitrepo
        self.url = self.working_dir or gitrepo.git_dir
        if gitrepo.remotes:
            # note: these might not look like absolute urls, e.g. git@github.com:onecommons/unfurl.git
            try:
                remote = gitrepo.remotes["origin"]
            except:
                remote = gitrepo.remotes[0]
            self.url = remote.url

    @property
    def working_dir(self):
        dir = self.repo.working_tree_dir
        if not dir or dir[-1] == "/":
            return dir
        else:
            return dir + "/"

    @property
    def revision(self):
        if not self.repo.head.is_valid():
            return ""
        return self.repo.head.commit.hexsha

    def resolve_rev_spec(self, revision):
        try:
            return self.repo.commit(revision).hexsha
        except:
            return None

    def get_url_with_path(self, path):
        if is_url_or_git_path(self.url):
            if os.path.isabs(path):
                # get path relative to repository's root
                path = os.path.relpath(path, self.working_dir)
            return normalize_git_url(self.url) + "#:" + path
        else:
            return self.get_git_local_url(path)

    def find_excluded_dirs(self, root):
        root = os.path.relpath(root, self.working_dir)
        status, stdout, stderr = self.run_cmd(
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
            path = os.path.join(self.working_dir, file)
            yield path

    def is_path_excluded(self, localPath):
        # XXX cache and test
        # excluded = list(self.findExcludedDirs(self.working_dir))
        # success error code means it's ignored
        return not self.run_cmd(["check-ignore", "-q", localPath])[0]

    def run_cmd(self, args, **kw):
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

    def add_to_local_git_ignore(self, rule):
        with open(os.path.join(self.repo.git_dir, "info", "exclude"), "a") as f:
            f.write("\n" + rule + "\n")

    def show(self, path, commitId):
        if self.working_dir and os.path.isabs(path):
            path = os.path.abspath(path)[len(self.working_dir) :]
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
            self.working_dir,
        )
        return self.working_dir

    def add_sub_module(self, gitDir):
        gitDir = os.path.abspath(gitDir)
        status, stdout, stderr = self.run_cmd(["submodule", "add", gitDir])
        success = not status
        if success:
            logging.debug("added submodule %s: %s %s", gitDir, stdout, stderr)
        else:
            logging.error("failed to add submodule %s: %s %s", gitDir, stdout, stderr)
        return success

    def get_initial_revision(self):
        if not self.repo.head.is_valid():
            return ""  # an uninitialized repo
        firstCommit = next(self.repo.iter_commits("HEAD", max_parents=0))
        return firstCommit.hexsha

    def add_all(self, path="."):
        path = os.path.relpath(path, self.working_dir)
        self.repo.git.add("--all", path)

    def commit_files(self, files, msg):
        # note: this will also commit existing changes in the index
        index = self.repo.index
        index.add([os.path.abspath(f) for f in files])
        return index.commit(msg)

    def is_dirty(self, untracked_files=False, path=None):
        # diff = self.repo.git.diff()  # "--abbrev=40", "--full-index", "--raw")
        # https://gitpython.readthedocs.io/en/stable/reference.html?highlight=is_dirty#git.repo.base.Repo.is_dirty
        return self.repo.is_dirty(untracked_files=untracked_files, path=path or None)

    def clone(self, newPath):
        # note: repo.clone uses bare path, which breaks submodule path resolution
        cloned = git.Repo.clone_from(
            self.working_dir, os.path.abspath(newPath), recurse_submodules=True
        )
        Repo.ignore_dir(newPath)
        return GitRepo(cloned)

    def get_git_local_url(self, path, name=""):
        if os.path.isabs(path):
            # get path relative to repository's root
            path = os.path.relpath(path, self.working_dir)
        return f"git-local://{self.get_initial_revision()}:{name}/{path}"

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


class RevisionManager:
    def __init__(self, manifest, localEnv=None):
        self.manifest = manifest
        self.revisions = None
        self.localEnv = localEnv

    def get_revision(self, change):
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
