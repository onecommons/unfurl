# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import abc
import os
import os.path
from pathlib import Path
import sys
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union, cast
from typing_extensions import Literal
import git
import git.exc
from git.objects import Commit

from .logs import getLogger, PY_COLORS
from urllib.parse import urlparse
from .util import UnfurlError, save_to_file
from toscaparser.repositories import Repository
from ruamel.yaml.comments import CommentedMap
import logging

if TYPE_CHECKING:
    from .packages import Package

logger = getLogger("unfurl")


def is_git_worktree(path, gitDir=".git"):
    # NB: if work tree is a submodule .git will be a file that looks like "gitdir: ./relative/path"
    return os.path.exists(os.path.join(path, gitDir))


def add_user_to_url(url, username, password):
    assert username
    parts = urlparse(url)
    user, sep, host = parts.netloc.rpartition("@")
    if password:
        netloc = f"{username}:{password}@{host}"
    else:
        netloc = f"{username}@{host}"

    return parts._replace(netloc=netloc).geturl()


def normalize_git_url(url: str, hard: int = 0):
    if url.startswith("git-local://"):  # truncate url after commit digest
        return "git-local://" + urlparse(url).netloc.partition(":")[0]

    if "://" not in url:  # not an absolute URL, convert some common patterns
        if url.startswith("/"):
            # abspath also normalizes the path
            return "file://" + os.path.abspath(url)
        elif url.startswith("~"):
            return "file://" + os.path.abspath(os.path.expanduser(url))
        elif url.startswith("file:"):
            # git doesn't like relative file URLs
            return "file://" + os.path.abspath(os.path.expanduser(url[5:]))
        elif "@" in url:  # scp style used by git: user@server:project.git
            # convert to ssh://user@server/project.git
            url = "ssh://" + url.replace(":", "/", 1)
    if hard:
        parts = urlparse(url)
        # remove password and .git
        user, sep, host = parts.netloc.rpartition("@")
        if sep and hard == 1:
            netloc = f"{user.partition(':')[0]}@{host}"
        else:  # hard >= 2
            netloc = host
        path = parts.path.rstrip("/")
        if hard == 3 and path.endswith(".git"):
            path = path[:-4]
        return parts._replace(netloc=netloc, path=path).geturl()
    return url


def sanitize_url(url: str, redact=True) -> str:
    if "://" in url and "@" in url:  # sanitize
        parts = urlparse(url)
        # XXXX out user and password
        user, sep, host = parts.netloc.rpartition("@")
        if user:
            user, sep, password = user.partition(":")
            if redact:
                netloc = f"XXXXX{':XXXXX' if password else ''}@{host}"
            else:
                netloc = host
            return parts._replace(netloc=netloc).geturl()
    return url


def normalize_git_url_hard(url):
    # remove scheme, .git and fragment
    return normalize_git_url(url, hard=3).rpartition("://")[2].partition("#")[0]


def is_url_or_git_path(url):
    if url.startswith("--"):
        # security: see https://github.com/gitpython-developers/GitPython/issues/1517
        return False
    if "://" in url and not url.startswith("file:"):
        return True
    if "@" in url:
        return True
    candidate, sep, frag = url.partition("#")
    if frag or candidate.rstrip("/").endswith(".git"):
        return True
    return False


def split_git_url(url) -> Tuple[str, str, str]:
    """
    Returns (repoURL, filePath, revision)
    RepoURL will be an empty string if it isn't a path to a git repo
    """
    if url.startswith("--"):
        # security: see https://github.com/gitpython-developers/GitPython/issues/1517
        return "", "", ""
    parts = urlparse(url)
    if parts.scheme == "git-local":
        giturl, path, fragment = (
            parts.scheme + "://" + parts.netloc,
            parts.path[1:],
            parts.fragment,
        )
        if fragment:
            revision, sep, frag_path = parts.fragment.partition(":")
            path = os.path.join(path, frag_path)
        else:
            revision = ""
        return giturl, path, revision

    if parts.fragment:
        # treat fragment as a git revision spec; see https://git-scm.com/docs/gitrevisions
        # or https://docs.docker.com/engine/reference/commandline/build/#git-repositories
        # just support <ref>:<path> for now
        # e.g. myrepo.git#mybranch, myrepo.git#pull/42/head, myrepo.git#:myfolder, myrepo.git#master:myfolder
        revision, sep, path = parts.fragment.partition(":")
        giturl, sep, frag = url.partition("#")
        return giturl, path, revision
    return url, "", ""


@lru_cache(None)
def memoized_remote_tags(url, pattern="*") -> List[str]:
    return get_remote_tags(url, pattern)


# git fetch <remote> 'refs/tags/*:refs/tags/*' if our clones are shallow
def get_remote_tags(url, pattern="*") -> List[str]:
    # https://github.com/gitpython-developers/GitPython/issues/1071
    # https://myshittycode.com/2020/10/02/git-querying-tags-without-cloning-the-repository/
    # -v:refname is version sort in reverse order
    # -c versionsort.suffix=- ensures 1.0.0-XXXXXX comes before 1.0.0.
    blob = git.cmd.Git()(c="versionsort.suffix=-").ls_remote(
        url, pattern, sort="-v:refname", tags=True
    )
    # len("b90df3d12413db22d051db1f7c7286cdd2f00b66\trefs/tags/") == 51
    # filter out ^{} references (see https://stackoverflow.com/questions/12938972/what-does-mean-in-git)
    tags = [line[51:] for line in blob.split("\n") if not line.endswith("^{}")]
    logger.debug("got %s remote tags with pattern %s from %s", len(tags), pattern, sanitize_url(url))
    return tags


class _ProgressPrinter(git.RemoteProgress):
    gitUrl = ""

    def update(self, op_code, cur_count, max_count=None, message=""):
        # we use print instead of logging because we don't want to clutter logs with this message
        if message and logger.getEffectiveLevel() <= logging.INFO:
            url = self.gitUrl
            print(f"fetching from {url}, received: {message} ", file=sys.stderr)


class Repo(abc.ABC):
    url: str = ""

    @staticmethod
    def find_containing_repo(rootDir, gitDir=".git"):
        """
        Walk parents looking for a git repository.
        """
        current = os.path.abspath(rootDir)
        while current and current != os.sep:
            if is_git_worktree(current, gitDir):
                return GitRepo(git.Repo(current))
            current = os.path.dirname(current)
        return None

    @staticmethod
    def find_git_working_dirs(
        rootDir, include_root, gitDir=".git"
    ) -> Dict[str, "RepoView"]:
        working_dirs: Dict[str, "RepoView"] = {}
        for root, dirs, files in os.walk(rootDir):
            if Repo.update_git_working_dirs(working_dirs, root, dirs, gitDir):
                if not include_root or rootDir != root:
                    del dirs[:]  # don't visit sub directories
        return working_dirs

    @staticmethod
    def update_git_working_dirs(
        working_dirs, root, dirs, gitDir=".git"
    ) -> Optional[str]:
        if gitDir in dirs and is_git_worktree(root, gitDir):
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

    @property
    @abc.abstractmethod
    def working_dir(self) -> str:
        ...

    @property
    @abc.abstractmethod
    def revision(self) -> str:
        ...

    @property
    def safe_url(self):
        return sanitize_url(self.url, True)

    def find_repo_path(self, path):
        localPath = self.find_path(path)[0]
        if localPath is not None and not self.is_path_excluded(localPath):
            return localPath
        return None

    def is_path_excluded(self, localPath):
        return False

    def find_path(self, path: str, importLoader=None):
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
        return RepoView(dict(name=name, url=self.url), cast(GitRepo, self))

    def is_local_only(self):
        return self.url.startswith("git-local://") or os.path.isabs(self.url)

    @staticmethod
    def get_path_for_git_repo(gitUrl: str, name_only=True) -> str:
        parts = urlparse(gitUrl)
        if parts.scheme == "git-local":
            # e.g. extract spec from git-local://0cfeee6571c4276ce1a63dc37aa8cbf8b8085d60:spec
            name = parts.netloc.partition(":")[1]
        else:
            path = parts.path.strip("/")
            # e.g. extract tosca-parser from https://github.com/onecommons/tosca-parser.git
            if name_only:
                path = os.path.basename(path)
            name = os.path.splitext(path)[0] or parts.netloc
        assert not name.endswith(".git"), name
        return name

    def project_path(self) -> str:
        return self.get_path_for_git_repo(self.url, False)

    @classmethod
    def create_working_dir(
        cls, gitUrl, localRepoPath, revision=None, depth=1, username=None, password=None
    ):
        localRepoPath = localRepoPath or "."
        if os.path.exists(localRepoPath):
            if not os.path.isdir(localRepoPath) or os.listdir(localRepoPath):
                raise UnfurlError(
                    f"couldn't create directory, it already exists and isn't empty: {localRepoPath}"
                )
        parent_dir = os.path.dirname(localRepoPath)
        if parent_dir.strip("/"):
            os.makedirs(parent_dir, exist_ok=True)
        cleanurl = sanitize_url(gitUrl)
        logger.info("Fetching %s %s to %s", cleanurl, revision or "", localRepoPath)
        kwargs: Dict[str, Any] = dict(recurse_submodules=True, no_single_branch=True)
        if depth:
            kwargs["depth"] = depth
            kwargs["shallow_submodules"] = True
        non_interactive = (
            os.getenv("CI") or not PY_COLORS
        )  # if CI or color output disabled
        if not non_interactive:
            # we're running in an interactive session
            progress = _ProgressPrinter()
            progress.gitUrl = cleanurl
            kwargs["progress"] = progress  # type: ignore
        try:
            if revision:
                kwargs["branch"] = revision
            # equivalent to git.Repo.clone_from() with add_transient_credentials() added
            gitcmd = git.Repo.GitCommandWrapperType(os.getcwd())
            if username:
                add_transient_credentials(gitcmd, gitUrl, username, password)
            repo = git.Repo._clone(
                gitcmd,
                gitUrl,
                localRepoPath,
                git.GitCmdObjectDB,
                **kwargs,
            )
        except git.exc.GitCommandError as err:  # type: ignore
            raise UnfurlError(
                f'couldn\'t create working directory, clone failed: "{err._cmdline}"\nTry re-running that command to diagnose the problem.'
            )
        Repo.ignore_dir(localRepoPath)
        return GitRepo(repo)


def commit_secrets(working_dir, yaml, repo: "GitRepo"):
    vault = yaml and getattr(yaml.representer, "vault", None)
    if not vault or not vault.secrets:
        return []
    saved = []
    for filepath, dotsecrets in find_dirty_secrets(working_dir, repo):
        with open(filepath, "r") as vf:
            vaultContents = vf.read()
        encoding = None if vaultContents.startswith("$ANSIBLE_VAULT;") else "vault"
        secretpath = dotsecrets / filepath.name
        logger.verbose("encrypting file to %s with %s", secretpath, vault.secrets[0][0])
        save_to_file(str(secretpath), vaultContents, yaml, encoding)
        saved.append(secretpath)
    return saved


def find_dirty_secrets(working_dir: str, repo: "GitRepo"):
    for root, dirs, files in os.walk(working_dir):
        if "secrets" not in Path(root).parts:
            continue
        for filename in files:
            dotsecrets = Path(root.replace("secrets", ".secrets"))
            filepath = Path(root) / filename
            local_path = str((dotsecrets / filename).relative_to(working_dir))
            if repo.is_path_excluded(local_path):
                continue
            # compare .secrets with secrets
            if (
                not dotsecrets.is_dir()
                or filename not in list([p.name for p in dotsecrets.iterdir()])
                or filepath.stat().st_mtime > (dotsecrets / filename).stat().st_mtime
            ):
                yield filepath, dotsecrets


class RepoView:
    # view of Repo optionally filtered by path
    # XXX and revision too
    def __init__(
        self, repository: Union[dict, Repository], repo: Optional["GitRepo"], path=""
    ) -> None:
        if isinstance(repository, dict):
            # required keys: name, url
            tpl = repository.copy()
            name = tpl.pop("name")
            tpl["url"] = normalize_git_url(tpl["url"])
            repository = Repository(name, tpl)
        assert repository or repo
        self.repository: Repository = repository
        self.yaml = None
        self.revision: Optional[str] = None
        self.file_refs: List[str] = []
        self.set_repo_and_path(repo, path)
        self.read_only = False
        self.package: Optional[Union[Literal[False], "Package"]] = None

    def set_repo_and_path(self, repo: Optional["GitRepo"], path: str):
        self.repo = repo
        self.path = path
        if repo and path and self.repository:
            self.repository.url = repo.get_url_with_path(
                path, False, self.revision or ""
            )

    @property
    def working_dir(self) -> str:
        if self.repo:
            return os.path.join(self.repo.working_dir, self.path)
        else:
            return os.path.join(self.repository.url, self.path)

    @property
    def name(self):
        return self.repository.name if self.repository else ""

    @property
    def url(self) -> str:
        if self.repository:
            url = self.repository.url
            if self.repository.credential:
                credential = self.repository.credential
                return add_user_to_url(url, credential["user"], credential["token"])
            else:
                return url
        else:
            assert self.repo
            return self.repo.url

    def has_credentials(self):
        parts = urlparse(self.url)
        return "@" in parts.netloc

    def as_git_url(self, sanitize=False) -> str:
        hard = 2 if sanitize else 0
        url, path, revision = split_git_url(self.url)
        if self.package:
            revision = self.package.revision_tag
        else:
            revision = self.revision or ""
        return (
            normalize_git_url(url, hard)
            + "#"
            + revision
            + ":"
            + os.path.join(path, self.path)
        )

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
        if self.read_only or not self.repo:
            return False
        for filepath, dotsecrets in find_dirty_secrets(self.working_dir, self.repo):
            return True
        return self.repo.is_dirty(untracked_files=True, path=self.path)

    def add_file_ref(self, file_name: str):
        if file_name not in self.file_refs:
            self.file_refs.append(file_name)

    def add_all(self):
        assert not self.read_only and self.repo
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
                    target_path = str(target)
                    dir = os.path.dirname(target_path)
                    if dir and not os.path.isdir(dir):
                        os.makedirs(dir)
                    with open(target_path, "w") as f:
                        f.write(contents)
                    os.utime(target, (stinfo.st_atime, stinfo.st_mtime))
                    logger.verbose("decrypted secret file to %s", target)

    def save_secrets(self):
        assert self.repo
        return commit_secrets(self.working_dir, self.yaml, self.repo)

    def commit(self, msg: str, add_all: bool = False) -> int:
        assert not self.read_only
        assert self.repo
        if self.yaml:
            for saved in self.save_secrets():
                local_path = str(saved.relative_to(self.repo.working_dir))
                self.repo.repo.git.add(local_path)
        if add_all:
            self.add_all()
        self.repo.repo.index.commit(msg)
        return 1

    def git_status(self):
        assert self.repo
        return self.repo.run_cmd(["status", self.path or "."])[1]

    def _secrets_status(self):
        assert self.repo
        modified = "\n   ".join(
            [
                str(filepath.relative_to(self.repo.working_dir))
                for filepath, dotsecrets in find_dirty_secrets(
                    self.working_dir, self.repo
                )
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

    def get_current_commit(self):
        if not self.repo:
            return ""
        if self.is_dirty():
            return self.repo.revision + "-dirty"
        else:
            return self.repo.revision

    def lock(self) -> CommentedMap:
        record = CommentedMap(
            [
                ("url", normalize_git_url(self.url, 1)),
                ("commit", self.get_current_commit()),
                ("initial", self.get_initial_revision()),
            ]
        )
        if self.package and self.package.revision:
            # intended revision (branch or tag) declared by user
            record["revision"] = self.package.revision
        if self.repo and self.repo.active_branch:
            # current commit is on this branch
            record["branch"] = self.repo.active_branch
        if self.repo and self.repo.current_tag:
            # current commit is on this tag
            record["tag"] = self.repo.current_tag
        if self.name:
            record["name"] = self.name
        if self.origin:
            record["origin"] = normalize_git_url(self.origin, 1)
        return record


def add_transient_credentials(git, url, username, password):
    transient_url = add_user_to_url(url, username, password)
    replacement = f'url."{transient_url}".insteadOf="{url}"'
    # _git_options get cleared after next git command is issued
    git._git_options = git.transform_kwargs(
        split_single_char_options=True, c=replacement
    )


class GitRepo(Repo):
    def __init__(self, gitrepo: git.Repo):
        self.repo = gitrepo
        self.url = self.working_dir or str(gitrepo.git_dir)
        remote = self.remote
        if remote:
            # note: these might not look like absolute urls, e.g. git@github.com:onecommons/unfurl.git
            self.url = remote.url
        self.push_url: Optional[str] = None

    def add_transient_push_credentials(self, username, password):
        if not self.remote:
            return
        if self.push_url is None:
            self.push_url = self.repo.git.remote("get-url", "--push", self.remote.name)
        add_transient_credentials(self.repo.git, self.push_url, username, password)

    def set_url_credentials(
        self, username: str, password: str, fetch_only=False
    ) -> None:
        remote = self.remote
        if remote:
            if username or password:
                new_url = add_user_to_url(remote.url, username, password)
            else:
                # clear credentials
                new_url = sanitize_url(remote.url, False)
            remote.set_url(new_url, remote.url)
            self.url = new_url
            if fetch_only:
                # exclude credentials from the push url
                self.push_url = sanitize_url(remote.url, False)
                remote.set_url(self.push_url, push=True)

    @property
    def working_dir(self) -> str:
        dir = self.repo.working_tree_dir
        if not dir:
            return ""
        dir = str(dir)
        if dir[-1] == "/":
            return dir
        else:
            return dir + "/"

    @property
    def revision(self) -> str:
        if not self.repo.head.is_valid():
            return ""
        return self.repo.head.commit.hexsha

    @property
    def remote(self) -> Optional[git.Remote]:
        gitrepo = self.repo
        if gitrepo.remotes:
            try:
                return gitrepo.remotes["origin"]
            except Exception:
                return gitrepo.remotes[0]
        return None

    @property
    def active_branch(self) -> str:
        try:
            return self.repo.active_branch.name
        except Exception:
            # no head or detached
            return ""

    @property
    def current_tag(self) -> str:
        try:
            return self.repo.git.describe(self.repo.heads[0].object, exact_match=True)
        except Exception:
            # e.g.:
            # git.exc.GitCommandError: Cmd('git') failed due to: exit code(128)
            #   stderr: 'fatal: no tag exactly matches 'ed915a383336a085eaabeb8f2a461e656ec8a5c9''
            return ""

    def resolve_rev_spec(self, revision):
        try:
            return self.repo.commit(revision).hexsha
        except Exception:
            return None

    def get_url_with_path(self, path: str, sanitize: bool = False, revision: str = ""):
        hard = 2 if sanitize else 0
        if is_url_or_git_path(self.url):
            if os.path.isabs(path):
                # get path relative to repository's root
                path = os.path.relpath(path, self.working_dir)
                if path.startswith(".."):
                    # outside of the repo, don't include it in the url
                    if revision:
                        revision = "#" + revision
                    return normalize_git_url(self.url, hard) + revision
            return normalize_git_url(self.url, hard) + "#" + revision + ":" + path
        else:
            return self.get_git_local_url(path, revision=revision)

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

    def reset(self, args: str = "--hard HEAD~1") -> bool:
        return not self.run_cmd(("reset " + args).split())[0]

    def run_cmd(self, args, with_exceptions=False, **kw):
        """
        :return:
          tuple(int(status), str(stdout), str(stderr))
        """
        gitcmd = self.repo.git
        call: List[str] = [gitcmd.GIT_PYTHON_GIT_EXECUTABLE or "git"]
        # add persistent git options
        call.extend(gitcmd._persistent_git_options)
        call.extend(list(args))
        call.extend(gitcmd.transform_kwargs(**kw))

        # note: sets cwd to working_dir
        return gitcmd.execute(  # type: ignore
            call, with_exceptions=with_exceptions, with_extended_output=True
        )

    def add_to_local_git_ignore(self, rule):
        path = os.path.join(self.repo.git_dir, "info")
        if not os.path.exists(path):
            os.makedirs(path)
        exclude_path = os.path.join(path, "exclude")
        with open(exclude_path, "a") as f:
            f.write("\n" + rule + "\n")
        return exclude_path

    def show(self, path, commitId, stdout_as_string=True):
        if self.working_dir and os.path.isabs(path):
            path = os.path.abspath(path)[len(self.working_dir) :]
        # XXX this won't work if path is in a submodule
        # if in path startswith a submodule: git log -1 -p [commitid] --  [submodule]
        # submoduleCommit = re."\+Subproject commit (.+)".group(1)
        # return self.repo.submodules[submodule].git.show(submoduleCommit+':'+path[len(submodule)+1:])
        return self.repo.git.show(
            commitId + ":" + path, stdout_as_string=stdout_as_string
        )

    def checkout(self, revision="", **kw):
        # if revision isn't specified and repo is not pinned:
        #  save the ref of current head
        self.repo.git.checkout(revision, **kw)
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
            logger.debug("added submodule %s: %s %s", gitDir, stdout, stderr)
        else:
            logger.error("failed to add submodule %s: %s %s", gitDir, stdout, stderr)
        return success

    def get_initial_revision(self):
        if not self.repo.head.is_valid():
            return ""  # an uninitialized repo
        firstCommit = next(self.repo.iter_commits("HEAD", max_parents=0))
        return firstCommit.hexsha

    def add_all(self, path="."):
        path = os.path.relpath(path, self.working_dir)
        self.repo.git.add("--all", path)

    def commit_files(self, files: List[str], msg: str) -> Commit:
        # note: this will also commit existing changes in the index
        index = self.repo.index
        index.add([os.path.abspath(f) for f in files])
        return index.commit(msg)

    def is_dirty(self, untracked_files=False, path: Optional[str] = None) -> bool:
        # diff = self.repo.git.diff()  # "--abbrev=40", "--full-index", "--raw")
        # https://gitpython.readthedocs.io/en/stable/reference.html?highlight=is_dirty#git.repo.base.Repo.is_dirty
        return self.repo.is_dirty(untracked_files=untracked_files, path=path or None)

    def pull(
        self, remote="origin", revision=None, ff_only=True, with_exceptions=False, **kw
    ) -> bool:
        if remote in self.repo.remotes:
            cmd = ["pull", remote, revision or "HEAD", "--tags", "--update-shallow"]
            if ff_only:
                cmd.append("--ff-only")
            code, out, err = self.run_cmd(cmd, with_exceptions=with_exceptions, **kw)
            if code:
                logger.info(
                    "attempt to pull latest from %s %s into %s failed: %s %s",
                    sanitize_url(self.url, True),
                    revision or "",
                    self.working_dir,
                    out,
                    err,
                )
                return False
            else:
                logger.verbose(
                    "pull latest from %s %s into %s: %s %s",
                    sanitize_url(self.url, True),
                    revision or "",
                    self.working_dir,
                    out,
                    err,
                )
                return True
        else:
            return False

    def _push(self, url: Optional[str] = None, **kw) -> None:
        if url:
            self.run_cmd(["push", url], with_exceptions=True, **kw)
        elif self.remote:
            self.remote.push(**kw).raise_if_error()

    def push(self, url: Optional[str] = None, pull_on_rejected=True, **kw) -> None:
        try:
            self._push(url, **kw)
        except git.exc.GitCommandError as e:  # type: ignore
            retry = pull_on_rejected and (
                not url
                # pull() doesn't support alternative urls
                or normalize_git_url_hard(url) == normalize_git_url_hard(self.url)
            )
            if retry and "[rejected]" in e.stderr:
                self.pull(
                    ff_only=False,
                    no_rebase=True,
                    commit=True,
                    no_edit=True,
                    with_exceptions=True,
                )
                self._push(url, **kw)
            else:
                raise e

    def clone(self, newPath):
        # note: repo.clone uses bare path, which breaks submodule path resolution
        cloned = git.Repo.clone_from(
            self.working_dir, os.path.abspath(newPath), recurse_submodules=True
        )
        Repo.ignore_dir(newPath)
        return GitRepo(cloned)

    def get_git_local_url(self, path, name="", revision=""):
        if os.path.isabs(path):
            # get path relative to repository's root
            path = os.path.relpath(path, self.working_dir)
        if revision:
            revision = "#" + revision
        return f"git-local://{self.get_initial_revision()}:{name}/{path}{revision}"

    def delete_dir(self, path, commit=None):
        self.repo.index.remove(os.path.abspath(path), r=True, working_tree=True)
        if commit:
            self.repo.index.commit(commit)

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


# class RevisionManager:
#     def __init__(self, manifest, localEnv=None):
#         self.manifest = manifest
#         self.revisions = None
#         self.localEnv = localEnv

#     def get_revision(self, change):
#         if self.revisions is None:
#             self.revisions = {self.manifest.specDigest: self.manifest}
#         digest = change["specDigest"]
#         commitid = change["startCommit"]
#         if digest in self.revisions:
#             return self.revisions[digest]
#         else:
#             from .manifest import SnapShotManifest

#             manifest = SnapShotManifest(self.manifest, commitid)
#             self.revisions[digest] = manifest
#             return manifest
