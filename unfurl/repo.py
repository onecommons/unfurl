# Copyright (c) 2020 Adam Souzis
# SPDX-License-Identifier: MIT
import abc
import os
import os.path
from pathlib import Path
import re
import sys
from functools import lru_cache
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    NamedTuple,
    Optional,
    Tuple,
    Union,
    cast,
    Iterator,
)
from typing_extensions import Literal, TypedDict
import git
import git.exc
from git.objects import Commit

from .logs import getLogger, PY_COLORS
from urllib.parse import urlparse, unquote
from .util import (
    UnfurlError,
    assert_not_none,
    change_cwd,
    is_relative_to,
    save_to_file,
    split_url_fragment,
)
from toscaparser.repositories import Repository
from toscaparser.imports import normalize_path
from ruamel.yaml.comments import CommentedMap
import logging

if TYPE_CHECKING:
    from .packages import Package

logger = getLogger("unfurl")


def is_git_worktree(path, gitDir=".git"):
    # NB: if work tree is a submodule .git will be a file that looks like "gitdir: ./relative/path"
    return os.path.exists(os.path.join(path, gitDir))


def add_user_to_url(url: str, username: str, password: str) -> str:
    assert username
    parts = urlparse(url)
    if parts.scheme != "https" and parts.scheme != "http":
        return url
    user, sep, host = parts.netloc.rpartition("@")
    if password:
        netloc = f"{username}:{password}@{host}"
    else:
        netloc = f"{username}@{host}"

    return parts._replace(netloc=netloc).geturl()


def normalize_git_url(url: str, hard: int = 0):
    if url.startswith("git-local://"):
        # truncate netloc after commit digest
        url, path, revision = split_git_url(url)
        base_url = "git-local://" + urlparse(url).netloc.partition(":")[0]
        if path:  # move path to fragment
            return f"{base_url}#{revision}:{path}"
        elif revision:
            return f"{base_url}#{revision}"
        else:
            return base_url

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
        # remove password and .git
        parts = urlparse(url)
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
    if sep or candidate.rstrip("/").endswith(".git"):
        return True
    return False


def split_git_url_with_commit(url: str) -> Tuple[str, str, str, str]:
    """
    Returns (repository_url, file_path, revision, commit)
    repository_url will be an empty string if it isn't a path to a git repo
    """
    if url.startswith("--"):
        # security: see https://github.com/gitpython-developers/GitPython/issues/1517
        return "", "", "", ""
    # a "#" inside a URI template expression is part of the expression, not the
    # start of the fragment, so don't let urlparse find it
    giturl, fragment = split_url_fragment(url)
    parts = urlparse(giturl)
    commit = ""
    if parts.scheme == "git-local":
        giturl, path = parts.scheme + "://" + parts.netloc, parts.path[1:]
        if fragment:
            revision, sep, frag_path = fragment.partition(":")
            path = os.path.join(path, frag_path)
        else:
            revision = ""
        revision, sep, commit = revision.partition("~")
        return giturl, unquote(path), revision, commit

    if fragment:
        # support <ref>~<commit>:<path>
        # e.g. myrepo.git#mybranch, myrepo.git#pull/42/head, myrepo.git#:myfolder, myrepo.git#master:myfolder
        revision, sep, path = fragment.partition(":")
        revision, sep, commit = revision.partition("~")
        return giturl, unquote(path), revision, commit
    return url, "", "", ""


def split_git_url(url: str) -> Tuple[str, str, str]:
    """
    Returns (repository_url, file_rath, revision)
    repository_url will be an empty string if it isn't a path to a git repo
    """
    return split_git_url_with_commit(url)[:3]


def git_url_join(url: str, path: str, revision: str, commit: str = "") -> str:
    if commit:
        revision = f"{revision}~{commit}"
    if revision and path:
        return f"{url}#{revision}:{path}"
    elif revision:
        return f"{url}#{revision}"
    elif path:
        return f"{url}#:{path}"
    else:
        return url


@lru_cache(None)
def memoized_remote_tags(url: str, pattern: str = "*") -> List[str]:
    return get_remote_tags(url, pattern)


class RemoteRefs(NamedTuple):
    """What one ``git ls-remote`` call tells us about a remote repository."""

    tags: List[str]
    """Tag names matching the requested pattern, version-sorted descending."""

    default_branch: str
    """The branch the remote's HEAD points at, or "" if it advertised no
    symref. Empty is "unknown", never a guess -- a caller that needs a
    fallback has to choose one itself."""


# git fetch <remote> 'refs/tags/*:refs/tags/*' if our clones are shallow
def get_remote_refs(url: str, pattern: str = "*") -> RemoteRefs:
    """Query ``url`` for its tags and its default branch in a single request.

    The default branch is the target of the remote's symbolic HEAD, which
    GitHub and GitLab both advertise. It comes back on the same connection as
    the tags: passing ``--tags`` would filter HEAD out of the response (it
    keeps only ``refs/tags/*``), so the tag filter goes in as a ref pattern
    instead and ``HEAD`` rides along as a second one.
    """
    # tags are returned in descending order: [v1.0.0, v0.1.0]
    # https://github.com/gitpython-developers/GitPython/issues/1071
    # https://myshittycode.com/2020/10/02/git-querying-tags-without-cloning-the-repository/
    # -v:refname is version sort in reverse order
    # -c versionsort.suffix=- ensures 1.0.0-XXXXXX comes before 1.0.0.
    blob = git.cmd.Git()(c="versionsort.suffix=-").ls_remote(
        url, f"refs/tags/{pattern}", "HEAD", symref=True, sort="-v:refname"
    )
    tags: List[str] = []
    default_branch = ""
    for line in blob.splitlines():
        # each line is "<oid>\t<refname>", or "ref: <target>\t<refname>" for
        # the symref HEAD that --symref adds ahead of HEAD's own oid line
        value, sep, ref = line.partition("\t")
        # filter out ^{} references (see https://stackoverflow.com/questions/12938972/what-does-mean-in-git)
        if not sep or ref.endswith("^{}"):
            continue
        if ref == "HEAD":
            # absent unless the remote advertised a symbolic HEAD; a remote
            # that doesn't leaves the default branch unknown.
            if value.startswith("ref: "):
                target = value[len("ref: ") :]
                if target.startswith("refs/heads/"):
                    target = target[len("refs/heads/") :]
                default_branch = target
        elif ref.startswith("refs/tags/"):
            tags.append(ref[len("refs/tags/") :])
    logger.debug(
        "got %s remote tags with pattern %s and default branch %s from %s",
        len(tags),
        pattern,
        default_branch or "(unknown)",
        sanitize_url(url),
    )
    return RemoteRefs(tags, default_branch)


def get_remote_tags(url: str, pattern: str = "*") -> List[str]:
    return get_remote_refs(url, pattern).tags


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
    def find_containing_git_repo(
        rootDir, gitDir=".git", stop_at: str = ""
    ) -> Optional["GitRepo"]:
        """
        Walk parents looking for a git repository.

        Args:
            stop_at: If set, stop searching before reaching this directory.
        """
        current = os.path.abspath(rootDir)
        stop = os.path.abspath(stop_at) if stop_at else ""
        while current and current != os.sep:
            if stop and current == stop:
                return None
            if is_git_worktree(current, gitDir):
                return GitRepo(git.Repo(current))
            current = os.path.dirname(current)
        return None

    @staticmethod
    def find_containing_repo(
        rootDir: str, gitDir=".git", stop_at: str = ""
    ) -> Optional["Repo"]:
        """
        Walk parents looking for a git or proxied repository.

        Args:
            stop_at: If set, stop searching before reaching this directory.
        """
        current = os.path.abspath(rootDir)
        stop = os.path.abspath(stop_at) if stop_at else ""
        while current and current != os.sep:
            if stop and current == stop:
                return None
            repo = Repo.make_repo(current, gitDir)
            if repo:
                return repo
            current = os.path.dirname(current)
        return None

    @staticmethod
    def find_containing_repo_with_url(
        path: str, url: str, gitDir=".git", stop_at: str = ""
    ) -> Optional["Repo"]:
        """
        Like ``find_containing_repo`` but also checks that the repo has a git remote that matches the url or that the url is a local path that matches the repo's working dir.
        """
        repo = Repo.find_containing_repo(path, gitDir, stop_at)
        # if repo has a git remote that matches the url or the url is a local path that matches the repo's working dir
        if repo and (
            repo.find_remote_url(url=url)
            or normalize_path(url.partition("#")[0]).rstrip("/")
            == repo.working_dir.rstrip("/")
        ):
            return repo
        return None

    @staticmethod
    def find_working_dirs(
        rootDir,
        include_root,
        skip_dir=None,
        gitDir=".git",
    ) -> Dict[str, "RepoView"]:
        # includes ProxiedRepos
        working_dirs: Dict[str, "RepoView"] = {}
        for root, dirs, files in os.walk(rootDir):
            if skip_dir and root == os.path.join(rootDir, skip_dir):
                del dirs[:]  # don't visit sub directories
                continue
            if Repo.update_working_dirs(working_dirs, root, dirs, gitDir):
                if not include_root or rootDir != root:
                    del dirs[:]  # don't visit sub directories
        return working_dirs

    @staticmethod
    def find_repos_in_directory(
        working_dirs: Dict[str, "RepoView"], parent_dir: str, gitDir: str = ".git"
    ) -> None:
        """Find repos among the immediate children of parent_dir, following symlinks.

        For each child directory:
        - Resolve symlinks and use the real path as the working dir key.
        - If the child is itself a git repo, add it directly.
        - Otherwise, find the containing git repo and add a RepoView
          with a path relative to the git root.
        """
        # includes ProxiedRepos
        if not os.path.isdir(parent_dir):
            return
        for entry in os.listdir(parent_dir):
            child = os.path.join(parent_dir, entry)
            if not os.path.isdir(child):
                continue
            real_child = os.path.realpath(child)
            child_contents = os.listdir(real_child)
            if gitDir in child_contents or ".proxied" in child_contents:
                if real_child not in working_dirs:
                    repo = Repo.make_repo(real_child, gitDir)
                    if repo:
                        working_dirs[real_child] = repo.as_repo_view()
            elif os.path.islink(child):
                # Symlink pointing into a subdirectory of a git repo (not a repo root);
                # stop before reaching parent_dir to avoid finding the project repo itself
                containing = Repo.find_containing_repo(
                    real_child, gitDir, stop_at=os.path.realpath(parent_dir)
                )
                if containing:
                    rel_path = os.path.relpath(real_child, containing.working_dir)
                    if containing.working_dir not in working_dirs:
                        working_dirs[containing.working_dir] = containing.as_repo_view(
                            path=rel_path
                        )

    @staticmethod
    def update_working_dirs(working_dirs, root, dirs, gitDir=".git") -> Optional[str]:
        # includes ProxiedRepos
        key = os.path.abspath(root)
        repo = Repo.make_repo(root, gitDir)
        if repo:
            working_dirs[key] = repo.as_repo_view()
            if gitDir in dirs:
                # Submodules and linked worktrees have .git as a *file*, so it
                # won't appear in os.walk's dirs list — skip the remove in that case.
                dirs.remove(gitDir)  # don't visit .git directory
            return key
        else:
            return None

    @staticmethod
    def make_repo(root: str, gitDir=".git") -> Optional["Repo"]:
        key = os.path.abspath(root)
        if is_git_worktree(root, gitDir):
            return GitRepo(git.Repo(key))
        elif os.path.exists(os.path.join(root, ".proxied")):
            from .packages import ProxiedRepo

            return ProxiedRepo(key)
        return None

    @staticmethod
    def ignore_dir(dir):
        parent = Repo.find_containing_git_repo(os.path.dirname(dir))
        if parent:
            path = parent.find_repo_path(dir)
            if path:  # can be None if dir is already ignored
                parent.add_to_local_git_ignore("/" + path)
                return path
        return None

    @property
    @abc.abstractmethod
    def working_dir(self) -> str: ...

    @property
    @abc.abstractmethod
    def revision(self) -> str: ...

    @property
    @abc.abstractmethod
    def revision_time(self) -> float: ...

    @property
    @abc.abstractmethod
    def current_tag(self) -> str: ...

    @abc.abstractmethod
    def resolve_rev_spec(self, revision) -> Optional[str]: ...

    @abc.abstractmethod
    def find_remote_url(self, *, url=None, host=None) -> Optional[str]:
        """This repository's remote url for *url* or *host*, or None if it has none.

        Answers "is this repository a clone of that url -- or of anything on
        that host?". The url returned is the one the repository has configured,
        which can be spelled differently to the one asked about: comparison
        ignores whatever doesn't change which repository is named (scheme,
        credentials, a ".git" suffix, a trailing slash, a "#fragment").

        Pass *url* or *host*; passing neither is a programming error.
        """

    @abc.abstractmethod
    def clone(self, newPath: str) -> "Repo": ...

    @abc.abstractmethod
    def add_all(self, path) -> None:
        """Stage all changes (tracked and untracked) under *path*."""
        ...

    @abc.abstractmethod
    def add_relative_path(self, path: str) -> None:
        """Stage the given *files*."""
        ...

    @abc.abstractmethod
    def commit(self, msg: str, author: Optional[str] = None) -> Optional[Commit]:
        """Create a commit from the current index with *msg*, optionally attributed to
        *author* (``"Name <email>"``, a bare name, or a bare email address).
        Return None if there are no changes to commit."""
        ...

    def is_dirty(
        self, untracked_files: bool = False, path: Optional[str] = None
    ) -> bool:
        """Check if the working directory has been modified.

        Args:
            untracked_files: If True, files not listed in ``files.json``
                (and not matched by any ``.gitignore``) are considered dirty.
            path: Optional absolute path to restrict the check to.

        Returns:
            True if any tracked file has been modified or deleted, or (when
            *untracked_files* is True) if untracked files exist.
        """
        return False

    def get_url_with_path(self, path: str, sanitize: bool = False, revision: str = ""):
        hard = 2 if sanitize else 0
        if os.path.isabs(path):
            # get path relative to repository's root
            path = os.path.relpath(path, self.working_dir)
            if path.startswith(".."):
                # outside of the repo, don't include it in the url
                if revision:
                    revision = "#" + revision
                return normalize_git_url(self.url, hard) + revision
        return normalize_git_url(self.url, hard) + "#" + revision + ":" + path

    @property
    def safe_url(self):
        return sanitize_url(self.url, True)

    def find_repo_path(self, path):
        localPath = self.find_path(path)[0]
        if localPath is not None and not self.is_path_excluded(localPath):
            return localPath
        return None

    def is_path_excluded(self, localPath) -> bool:
        """Check whether *localPath* is excluded by ``.gitignore`` rules.

        Args:
            localPath: A path relative to the working directory.

        Returns:
            True if the path matches any ``.gitignore`` pattern found in the
            working tree.
        """
        return False

    def find_path(
        self, path: str, importLoader=None
    ) -> Tuple[Optional[str], Optional[str], Optional[bool]]:
        base = self.working_dir
        if not base:  # XXX support bare repos
            return None, None, None
        repoRoot = os.path.abspath(base)
        abspath = os.path.abspath(path).rstrip("/")
        if is_relative_to(abspath, repoRoot):
            # XXX find pinned
            # if importLoader:
            #   revision = importLoader.getRevision(self)
            # else:
            if True:
                revision = self.revision
            bare = not self.working_dir or revision != self.revision
            return abspath[len(repoRoot) + 1 :], revision, bare
        return None, None, None

    def as_repo_view(self, name="", path="") -> "RepoView":
        return RepoView(dict(name=name, url=self.url), self, path)

    def is_local_only(self):
        return self.url.startswith("git-local://") or os.path.isabs(self.url)

    @staticmethod
    def get_path_for_git_repo(gitUrl: str, name_only=True) -> str:
        parts = urlparse(normalize_git_url(gitUrl))
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
        cls,
        gitUrl,
        localRepoPath,
        revision=None,
        depth=1,
        shallow_since=None,
        username=None,
        password=None,
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
        if shallow_since or depth:
            if shallow_since:
                kwargs["shallow_since"] = shallow_since
            else:
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


def commit_secrets(working_dir, yaml, repo: Optional["Repo"]) -> List[Path]:
    vault = yaml and getattr(yaml.representer, "vault", None)
    if not vault or not vault.secrets:
        return []
    saved: List[Path] = []
    for filepath, dotsecrets in find_dirty_secrets(working_dir, repo):
        with open(filepath, "r") as vf:
            vaultContents = vf.read()
        encoding = None if vaultContents.startswith("$ANSIBLE_VAULT;") else "vault"
        secretpath = dotsecrets / filepath.name
        logger.verbose("encrypting file to %s with %s", secretpath, vault.secrets[0][0])
        save_to_file(str(secretpath), vaultContents, yaml, encoding)
        saved.append(secretpath)
    return saved


def find_dirty_secrets(
    working_dir: str, repo: Optional["Repo"]
) -> Iterator[Tuple[Path, Path]]:
    for root, dirs, files in os.walk(working_dir):
        if "secrets" not in Path(root).parts:
            continue
        for filename in files:
            dotsecrets = Path(root.replace("secrets", ".secrets"))
            filepath = Path(root) / filename
            local_path = str((dotsecrets / filename).relative_to(working_dir))
            if repo and repo.is_path_excluded(local_path):
                continue
            # compare .secrets with secrets
            if (
                not dotsecrets.is_dir()
                or filename not in list([p.name for p in dotsecrets.iterdir()])
                or filepath.stat().st_mtime > (dotsecrets / filename).stat().st_mtime
            ):
                yield filepath, dotsecrets


class RepoLockDict(TypedDict, total=False):
    """Dict returned by RepoView.lock() representing a locked repository state."""

    url: str
    "Repository git URL"
    package_id: str
    "Set if repository is a package"
    name: str
    "Name or repository (set if package_id is missing)"
    commit: str
    "Current git commit hash"
    initial: str
    """Initial git commit hash if available"""
    discovered_revision: str
    '''Discovered branch or tag or "(MISSING)" or ""'''
    revision: str
    """Intended revision (branch or tag) declared by user (or restored from previous lock)"""
    branch: str
    "Branch the current commit is on"
    tag: str
    "Tag the current commit is on"
    origin: str
    "Origin Remote URL"
    project: str
    "Project associated with the repository"


class RepoView:
    # view of Repo optionally filtered by path (relative to the repo root)
    # XXX and revision too
    def __init__(
        self, repository: Union[dict, Repository], repo: Optional[Repo], path=""
    ) -> None:
        if isinstance(repository, dict):
            # required keys: name, url
            tpl = repository.copy()
            name = tpl.pop("name")
            tpl["url"] = normalize_git_url(tpl["url"])
            repository = Repository(name, tpl)
        assert repository or repo
        self.repository: Repository = repository
        self.yaml: Any = None
        self.revision: Optional[str] = None
        self.file_refs: List[str] = []
        self.repo = repo
        if (
            is_url_or_git_path(self.repository.url)
            and "file:" not in self.repository.url
            and self.repository.url[0] != "/"
        ):
            _, filepath, revision = split_git_url(self.repository.url)
            if filepath:
                path = os.path.normpath(os.path.join(filepath, path))
            if revision:
                self.revision = revision
        path = os.path.normpath(path) if path else ""
        self.path = "" if path == "." else path
        if repo and path and self.repository:
            # XXX check that repo.url and repository.url match
            # and neither have fragments
            self.repository.url = repo.get_url_with_path(
                path, False, self.revision or ""
            )
        self.read_only = False
        self.package: Optional[Union[Literal[False], "Package"]] = None
        self._loaded_secrets = False

    def __getstate__(self):
        state = self.__dict__.copy()
        state["yaml"] = None
        return state

    @property
    def working_dir(self) -> str:
        if self.repo:
            return os.path.join(self.repo.working_dir, self.path)
        else:  # XXX wrong unless url is just a file path not an url
            return os.path.join(self.repository.url, self.path)

    @property
    def gitrepo(self) -> Optional["GitRepo"]:
        if isinstance(self.repo, GitRepo):
            return self.repo
        return None

    @property
    def name(self):
        return self.repository.name if self.repository else ""

    @property
    def python_name(self):
        return re.sub(r"\W", "_", self.name)

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

    @property
    def safe_url(self):
        return sanitize_url(self.url, True)

    def has_credentials(self):
        parts = urlparse(self.url)
        return "@" in parts.netloc

    def as_git_url(self, sanitize=False) -> str:
        hard = 2 if sanitize else 0
        url, path, revision = split_git_url(self.url)
        return normalize_git_url(url, hard) + "#" + self.revision_tag + ":" + self.path

    @property
    def revision_tag(self):
        if self.package:
            return self.package.revision_tag
        return self.revision or ""

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

    def is_dirty(self, path: Optional[str] = None) -> bool:
        if self.read_only or not self.repo:
            return False
        if self.repo.is_dirty(untracked_files=True, path=path or self.working_dir):
            return True
        for filepath, dotsecrets in find_dirty_secrets(self.working_dir, self.repo):
            return True
        return False

    def add_file_ref(self, file_name: str):
        if file_name not in self.file_refs:
            self.file_refs.append(file_name)

    def add_all(self):
        assert not self.read_only and self.repo
        self.repo.add_all(os.path.abspath(self.working_dir))

    def load_secrets(self, _loader):
        if self._loaded_secrets or not self.gitrepo:
            return
        logger.trace("looking for secrets %s", self.working_dir)
        excluded = set(self.gitrepo.find_excluded_dirs(self.working_dir))
        failed = False
        for root, dirs, files in os.walk(self.working_dir):
            for d in dirs[:]:
                if (
                    d == ".git"
                    or os.path.join(os.path.normpath(root), d, "") in excluded
                ):
                    dirs.remove(d)
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
                        if contents is None:
                            raise Exception("decrypting returned None")
                    except Exception as err:
                        logger.warning("could not decrypt %s: %s", filepath, err)
                        failed = True
                        continue
                    target_path = str(target)
                    dir = os.path.dirname(target_path)
                    if dir and not os.path.isdir(dir):
                        os.makedirs(dir)
                    with open(target_path, "w") as f:
                        f.write(contents)
                    os.utime(target, (stinfo.st_atime, stinfo.st_mtime))
                    logger.verbose("decrypted secret file to %s", target)
        self._loaded_secrets = not failed

    def save_secrets(self) -> List[Path]:
        return commit_secrets(self.working_dir, self.yaml, self.repo)

    def commit(
        self,
        msg: str,
        add_all: bool = False,
        save_secrets=True,
        author: Optional[str] = None,
    ) -> int:
        assert not self.read_only
        repo = assert_not_none(self.repo)
        if self.yaml and save_secrets:
            for saved in self.save_secrets():
                local_path = str(saved.relative_to(repo.working_dir))
                repo.add_relative_path(local_path)
        if add_all:
            self.add_all()
        # `repo.commit` returns None when the index matches HEAD, e.g. when this view
        # was dirty only in the working tree and `add_all` didn't stage anything.
        return 1 if repo.commit(msg, author) else 0

    def git_status(self):
        assert self.gitrepo
        return self.gitrepo.run_cmd(["status", self.path or "."])[1]

    def _secrets_status(self):
        assert self.gitrepo
        modified = "\n   ".join(
            [
                str(filepath.relative_to(self.gitrepo.working_dir))
                for filepath, dotsecrets in find_dirty_secrets(
                    self.working_dir, self.gitrepo
                )
            ]
        )
        if modified:
            return f"\n\nSecrets to be committed:\n   {modified}"
        return ""

    def get_repo_status(self, dirty=False):
        if self.gitrepo and (not dirty or self.is_dirty()):
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
        if not self.gitrepo:
            return ""
        return self.gitrepo.get_initial_revision()

    def get_current_commit(self):
        if not self.repo:
            return ""
        if self.is_dirty():
            return self.repo.revision + "-dirty"
        else:
            return self.repo.revision

    def lock(self) -> "RepoLockDict":
        record: RepoLockDict = CommentedMap(  # type: ignore[assignment]
            [
                ("url", normalize_git_url(self.url, 1)),
                ("commit", self.get_current_commit()),
            ]
        )
        initial = self.get_initial_revision()
        if initial:
            record["initial"] = initial
        record["discovered_revision"] = ""  # default: no search occurred
        if self.package:
            record["package_id"] = self.package.package_id
            if self.package.revision:
                # intended revision (branch or tag) declared by user (or restored from previous lock)
                if self.package.discovered:
                    record["discovered_revision"] = self.package.revision
                else:
                    record["revision"] = self.package.revision
            if self.package.missing:
                record["discovered_revision"] = "(MISSING)"
        if self.gitrepo and self.gitrepo.active_branch:
            # current commit is on this branch
            record["branch"] = self.gitrepo.active_branch
        if self.repo and self.repo.current_tag:
            # current commit is on this tag
            record["tag"] = self.repo.current_tag
        if not self.package and self.name:
            record["name"] = self.name
        if self.origin:
            record["origin"] = normalize_git_url(self.origin, 1)
        return record

    def get_link(self, base_path: str, name: str = "") -> Tuple[str, str]:
        """Find or create a symlink to this repository in "tosca_repositories"

        Args:
            base_path (str): Location of "tosca_repositories"
            name (str, optional): name of the symlink. Defaults to repository.name.

        Raises:
            UnfurlError: if a file exists and

        Returns:
            Tuple[str, str]: symlink file name, target path
        """
        assert name or self.repository.name, (base_path, self.repository.tpl)
        name = re.sub(r"\W", "_", name or self.repository.name)
        assert name.isidentifier(), name
        target_path = self.working_dir
        if not Path(target_path).is_dir():
            raise UnfurlError(
                f"Can not create symlink to {target_path}: it isn't a directory."
            )
        tosca_repos_root = Path(base_path) / "tosca_repositories"
        # ensure t_r and its gitignore exist
        if not tosca_repos_root.exists():
            try:
                os.mkdir(tosca_repos_root)
                with open(tosca_repos_root / ".gitignore", "w") as gi:
                    gi.write("*")
            except Exception:
                logger.error(
                    f"Error creating tosca_repositories at {base_path}", exc_info=True
                )

        symlink = tosca_repos_root / name
        # remove/recreate to ensure symlink is correct
        if symlink.is_symlink():
            target = os.path.join(os.path.dirname(symlink), os.readlink(symlink))
            if os.path.abspath(target) == os.path.abspath(
                target_path
            ):  # already exists
                return name, target_path
            symlink.unlink()

        # use os.path.relpath as Path.relative_to only accepts strict subpaths
        rel_repo_path = os.path.relpath(target_path, tosca_repos_root)
        try:
            symlink.symlink_to(rel_repo_path, target_is_directory=True)
        except FileExistsError:
            raise UnfurlError(
                f"Can not create symlink at {symlink}: it already exists but is not a symlink"
            )
        return name, target_path


def add_transient_credentials(git, url, username, password):
    transient_url = add_user_to_url(url, username, password)
    if transient_url == url:
        return transient_url
    replacement = f'url."{transient_url}".insteadOf="{url}"'
    # _git_options get cleared after next git command is issued
    git._git_options = git.transform_kwargs(
        split_single_char_options=True, c=replacement
    )
    return transient_url


def make_actor(
    actor: Optional[str],
    gitrepo: Optional[git.Repo] = None,
    role: Literal["author", "committer"] = "committer",
) -> Optional[git.Actor]:
    """Convert an actor string into a git ``Actor``.

    Accepts either ``"Name <email@example.com>"``, a bare name, or a bare email address.

    Args:
        actor: the string to parse; if empty, None is returned so git's defaults apply.
        gitrepo: repository whose git configuration supplies any missing name or email
          (git records the literal string "None" otherwise).
        role: whether the actor is the commit's author or its committer -- it selects
          which of ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` provides the defaults.
    """
    if not actor or not actor.strip():
        return None
    actor = actor.strip()
    match = re.match(r"^(.*?)\s*<([^>]*)>$", actor)
    if match:
        name, email = match.group(1).strip(), match.group(2).strip()
    elif "@" in actor and " " not in actor:
        name, email = "", actor
    else:
        name, email = actor, ""
    if not name or not email:
        config_reader = gitrepo.config_reader() if gitrepo is not None else None
        get_default = git.Actor.author if role == "author" else git.Actor.committer
        default = get_default(config_reader)
        name = name or default.name
        email = email or default.email
    return git.Actor(name, email)


class GitRepo(Repo):
    _default_branch: Optional[str] = None
    # (class attribute so we can restore old pickled instances without a default branch)

    def __init__(self, gitrepo: git.Repo):
        self.repo = gitrepo
        self.url = self.working_dir or str(gitrepo.git_dir)
        remote = self.remote
        if remote:
            # note: these might not look like absolute urls, e.g. git@github.com:onecommons/unfurl.git
            self.url = remote.url
        self.push_url: Optional[str] = None

    def __getstate__(self):
        state = self.__dict__.copy()
        state["repo"] = self.working_dir  # git.Repo might have file handles
        return state

    def __setstate__(self, state):
        if isinstance(state.get("repo"), str):  # restore from working_dir
            state["repo"] = git.Repo(state["repo"])
        self.__dict__.update(state)

    def add_transient_push_credentials(self, username: str, password: str) -> str:
        if not self.remote:
            return ""
        if self.push_url is None:
            self.push_url = self.repo.git.remote("get-url", "--push", self.remote.name)
        return add_transient_credentials(
            self.repo.git, self.push_url, username, password
        )

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
            if new_url == remote.url:
                return
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
        """Return the current commit hash, or an empty string if there is no valid head (e.g. in an empty repository)"""
        if not self.repo.head.is_valid():
            return ""
        return self.repo.head.commit.hexsha

    @property
    def revision_time(self) -> float:
        if not self.repo.head.is_valid():
            return 0
        return self.repo.head.commit.committed_date

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
    def default_branch(self) -> str:
        """The repository's default branch, or "" when this clone records none.

        Read from ``refs/remotes/origin/HEAD``, the symref ``git clone`` writes
        from the default branch the remote advertises (GitPython has no
        default-branch accessor of its own; this is
        ``origin.refs.HEAD.ref.remote_head``, or the first remote's when the
        repository has no ``origin`` -- see ``remote``). A clone can lack it
        entirely --
        ``--bare``/``--mirror``, ``init`` + ``fetch`` before git 2.48,
        ``--single-branch`` of a non-default branch -- and it is not refreshed
        when the remote renames its default, so "" means "not recorded here",
        not "the remote has none". A symref left pointing at a
        deleted branch still reports that name, which is what the remote last
        advertised.

        A repository with no remote at all returns "".
        """
        if self._default_branch is not None:
            return self._default_branch
        remote = self.remote
        if not remote:
            return ""  # don't set _default_branch, so we try again next time
        try:
            self._default_branch = remote.refs.HEAD.ref.remote_head
        except AttributeError:
            # this clone has no <remote>/HEAD ref
            self._default_branch = ""
        return self._default_branch

    @property
    def current_tag(self) -> str:
        try:
            return self.repo.git.describe(exact_match=True)
        except Exception:
            # e.g.:
            # git.exc.GitCommandError: Cmd('git') failed due to: exit code(128)
            #    stderr: 'fatal: no tag exactly matches 'ed915a383336a085eaabeb8f2a461e656ec8a5c9''
            # or stderr: 'fatal: No names found, cannot describe anything.'
            return ""

    def resolve_rev_spec(self, revision) -> Optional[str]:
        """Resolve a revision specifier (e.g. branch or tag name) to a commit hash in this repository, or return None if it can't be resolved."""
        try:
            return self.repo.commit(revision).hexsha
        except Exception:
            return None

    def find_remote_url(self, *, url=None, host=None) -> Optional[str]:
        """The configured url of the remote `find_remote` matches, or None.

        The url comes from the remote, so it is this repository's own spelling
        of it rather than the one that was asked about.
        """
        remote = self.find_remote(url=url, host=host)
        if remote:
            return remote.url
        return None

    def find_remote(self, *, url=None, host=None) -> Optional[git.Remote]:
        """The first configured remote matching *url* or *host*, or None.

        Matching a url ignores everything that doesn't change which repository
        it names -- scheme, credentials, a ".git" suffix, a trailing slash, a
        "#fragment" -- so ``https://example.com/foo/bar.git`` matches a remote
        spelled ``git@example.com:foo/bar``. Matching a host compares hostnames
        only, finding any remote on that server whatever the repository.

        Pass *url* or *host*; passing neither is a programming error, and
        *host* wins if both are given. Remotes are searched in the order git
        lists them, which does not necessarily start with "origin".
        """
        if url:
            url = normalize_git_url_hard(url)
        else:
            assert host, "Must specify url or host"
        for remote in self.repo.remotes:
            if host:
                if host == urlparse(normalize_git_url(remote.url)).hostname:
                    return remote
            elif normalize_git_url_hard(remote.url) == url:
                return remote
        return None

    def get_url_with_path(self, path: str, sanitize: bool = False, revision: str = ""):
        if is_url_or_git_path(self.url):
            return super().get_url_with_path(path, sanitize, revision)
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

    def is_path_excluded(self, localPath: str) -> bool:
        # XXX cache and test
        # excluded = list(self.find_excluded_dirs(self.working_dir))
        # success error code means it's ignored
        return not self.run_cmd(["check-ignore", "-q", localPath])[0]

    def reset(self, args: str = "--hard HEAD~1") -> bool:
        return not self.run_cmd(("reset " + args).split())[0]

    def run_cmd(
        self, args, with_exceptions: bool = False, **kw
    ) -> Tuple[int, str, str]:
        """
        :return:
          tuple(status, stdout, stderr)
        """
        gitcmd = self.repo.git
        call: List[str] = [gitcmd.GIT_PYTHON_GIT_EXECUTABLE or "git"]
        # add persistent git options
        call.extend(gitcmd._persistent_git_options)
        call.extend(list(args))
        call.extend(gitcmd.transform_kwargs(**kw))

        # execute() sets cwd to working_dir, so use change_cwd() to restore it
        with change_cwd():
            return gitcmd.execute(  # type: ignore
                call,
                with_extended_output=True,
                with_exceptions=with_exceptions,
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

    def checkout(self, revision="", fetch_first=False, **kw):
        # if revision isn't specified and repo is not pinned:
        #  save the ref of current head
        if fetch_first and self.repo.remotes:
            self.run_cmd(["fetch", revision or "HEAD", "--tags", "--update-shallow"])
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
        initial = self.resolve_rev_spec("INITIAL")
        if initial:
            return initial
        if not self.repo.head.is_valid():
            return ""  # an uninitialized repo
        firstCommit = next(self.repo.iter_commits("HEAD", max_parents=0))
        return firstCommit.hexsha

    def add_all(self, path: str) -> None:
        # local files risk confusion: local to repo or local to current working dir?, so require absolute path
        assert os.path.isabs(path), "expected absolute path: " + path
        path = os.path.relpath(path, self.working_dir)
        # --all adds, modifies, and removes index entries to match the working tree.
        self.repo.git.add("--all", path)

    def add_relative_path(self, path: str) -> None:
        self.repo.git.add(path)

    def commit(self, msg: str, author: Optional[str] = None) -> Optional[Commit]:
        if self.repo.head.is_valid():
            changed = bool(self.repo.index.diff("HEAD"))
        else:
            # An unborn HEAD has no tree to diff against (`index.diff("HEAD")` raises
            # BadName), so ask whether anything is staged for this first commit.
            changed = bool(self.repo.index.entries)
        if changed:
            return self.repo.index.commit(
                msg, author=make_actor(author, self.repo, "author")
            )
        return None

    def commit_files(
        self, files: List[str], msg: str, author: Optional[str] = None
    ) -> Commit:
        """Add ``files`` to the index and commit them.

        Args:
            files: absolute paths of the files to commit.
            msg: the commit message.
            author: optional git author, either ``"Name <email>"`` or a bare name.
        """
        # note: this will also commit existing changes in the index
        index = self.repo.index
        # local files risk confusion: local to repo or local to current working dir?, so require absolute path
        assert all(os.path.isabs(f) for f in files), "expected absolute paths: " + str(
            files
        )
        index.add(files)
        return index.commit(msg, author=make_actor(author, self.repo, "author"))

    def is_dirty(self, untracked_files=False, path: Optional[str] = None) -> bool:
        # diff = self.repo.git.diff()  # "--abbrev=40", "--full-index", "--raw")
        # https://gitpython.readthedocs.io/en/stable/reference.html?highlight=is_dirty#git.repo.base.Repo.is_dirty
        if path:
            path = os.path.relpath(path, self.working_dir)
        return self.repo.is_dirty(untracked_files=untracked_files, path=path or None)
        # note: if you get git.exc.GitCommandError with "git diff: unknown option `cached'"
        # it's because: https://stackoverflow.com/questions/69470009/git-diff-cached-unknown-option-cached

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

    def clone(self, newPath: str) -> "GitRepo":
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

    def is_lfs_enabled(self, url=None) -> bool:
        if self.repo.remotes:
            status, out, err = self.run_cmd(["lfs", "locks"], remote=url)
            if status:
                logger.warning(
                    "git lfs on %s not available, `git lfs locks` says: %s",
                    self.safe_url,
                    err,
                )
            else:
                logger.debug(
                    "git lfs on %s available, `git lfs locks` says: %s",
                    self.safe_url,
                    out,
                )
                return True
        return False

    def lock_lfs(self, lockfilepath: str, url=None) -> bool:
        try:
            # note: file doesn't have to exist or added to the repo or have its .gitattributes set
            self.run_cmd(
                ["lfs", "lock", lockfilepath], remote=url, with_exceptions=True
            )
        except git.exc.GitCommandError as e:
            if "already locked" in e.stderr:
                return False
            else:
                raise
        return True

    def unlock_lfs(self, lockfilepath: str, url=None) -> bool:
        try:
            # note: file doesn't have to exist or added to the repo or have its .gitattributes set
            self.run_cmd(
                ["lfs", "unlock", lockfilepath], remote=url, with_exceptions=True
            )
        except git.exc.GitCommandError as e:
            if "no matching locks found" in e.stderr:
                return False
            else:
                raise
        return True

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
