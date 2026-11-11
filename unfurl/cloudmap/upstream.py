# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""Compare a repository's branches and tags with those of the repository it
was forked or mirrored from."""

import os
import tempfile
from contextlib import contextmanager
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    Generator,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import git

if TYPE_CHECKING:
    from ..logs import UnfurlLogger

from ..repo import GitRepo, sanitize_url
from .db import CloudMapDB
from ..tosca_plugins.cloudmap_defs import (
    Repository,
    UpstreamRef,
    UpstreamRefs,
    UpstreamRefStatus,
    get_repository_url,
)

DIRTY_SUFFIX = "-dirty"

Refs = Dict[str, str]


def _identical(commit: str) -> UpstreamRef:
    return UpstreamRef(
        "identical",
        commit=commit,
        local_commit=commit,
        merge_base=commit,
        ahead=0,
        behind=0,
    )


def _merge_base(repo: git.Repo, commit: str, upstream_commit: str) -> Optional[str]:
    """The merge base, or None if the commits share no history."""
    status, out, err = repo.git.merge_base(
        commit, upstream_commit, with_extended_output=True, with_exceptions=False
    )
    if status == 0:
        return out.strip()
    if status == 1 and not err:
        return None
    raise git.GitCommandError(
        ["git", "merge-base", commit, upstream_commit], status, err
    )


def _ahead_behind(repo: git.Repo, commit: str, upstream_commit: str) -> Tuple[int, int]:
    behind, ahead = repo.git.rev_list(
        "--left-right", "--count", f"{upstream_commit}...{commit}"
    ).split()
    return int(ahead), int(behind)


def compare_ref(repo: git.Repo, commit: str, upstream_commit: str) -> UpstreamRef:
    """How ``commit`` compares with ``upstream_commit``, both in ``repo``."""
    if commit == upstream_commit:
        return _identical(commit)
    merge_base = _merge_base(repo, commit, upstream_commit)
    if merge_base is None:
        return UpstreamRef("unrelated", commit=upstream_commit, local_commit=commit)
    ahead, behind = _ahead_behind(repo, commit, upstream_commit)
    status: UpstreamRefStatus
    if ahead and behind:
        status = "diverged"
    elif ahead:
        status = "ahead"
    elif behind:
        status = "behind"
    else:
        status = "identical"
    return UpstreamRef(
        status,
        commit=upstream_commit,
        local_commit=commit,
        merge_base=merge_base,
        ahead=ahead,
        behind=behind,
    )


def _present(repo: Optional[git.Repo], commits: Iterable[str]) -> Set[str]:
    """Those of ``commits`` that are commits in ``repo``."""
    wanted = sorted(set(commits))
    if repo is None or not wanted:
        return set()
    with tempfile.TemporaryFile() as stdin:
        stdin.write("".join(f"{commit}\n" for commit in wanted).encode())
        stdin.seek(0)
        out = repo.git.cat_file(
            "--batch-check=%(objectname) %(objecttype)", istream=stdin
        )
    found = (line.partition(" ") for line in out.splitlines())
    return {name for name, _, kind in found if kind == "commit"}


def _commits_of(repo: git.Repo, prefix: str) -> Refs:
    """The commits of the refs under ``prefix``, by their names after it,
    peeling tags and leaving out refs to anything but a commit."""
    out = repo.git.for_each_ref(
        "--format=%(refname) %(objecttype) %(objectname) %(*objecttype) %(*objectname)",
        prefix,
    )
    commits: Refs = {}
    for line in out.splitlines():
        refname, kind, commit, peeled_kind, peeled = (line.split(" ") + ["", ""])[:5]
        if kind == "tag":
            kind, commit = peeled_kind, peeled
        if kind == "commit":
            commits[refname[len(prefix) :]] = commit
    return commits


def _clone_refs(clone: GitRepo, url: str) -> Optional[Tuple[Refs, Refs]]:
    """The commits of the branches of ``clone``'s remote for ``url`` and of its
    tags; None if it has no such remote."""
    remote = clone.find_remote(url=url)
    if remote is None:
        return None
    branches = _commits_of(clone.repo, f"refs/remotes/{remote.name}/")
    branches.pop("HEAD", None)
    return branches, _commits_of(clone.repo, "refs/tags/")


def _git_dir(clone: GitRepo) -> str:
    return os.path.realpath(clone.repo.common_dir)


@contextmanager
def _reading_all(
    clones: Sequence[GitRepo],
) -> Generator[Optional[git.Repo], None, None]:
    """The first of ``clones``, able to read the others' commits too, without
    changing any of them; None if there are none."""
    if not clones:
        yield None
        return
    first, *others = clones
    if not others:
        yield first.repo
        return
    alternates = os.pathsep.join(os.path.join(_git_dir(o), "objects") for o in others)
    with first.repo.git.custom_environment(GIT_ALTERNATE_OBJECT_DIRECTORIES=alternates):
        yield first.repo


def _compare_refs(
    repo: Optional[git.Repo],
    present: Set[str],
    refs: Refs,
    upstream: Refs,
    upstream_complete: bool,
    previous: Dict[str, UpstreamRef],
) -> Dict[str, UpstreamRef]:
    compared: Dict[str, UpstreamRef] = {}
    for name, commit in refs.items():
        upstream_commit = upstream.get(name)
        if upstream_commit is None:
            status: UpstreamRefStatus = "missing" if upstream_complete else "unknown"
            compared[name] = UpstreamRef(status, local_commit=commit)
        elif commit == upstream_commit:
            continue
        elif repo is not None and commit in present and upstream_commit in present:
            compared[name] = compare_ref(repo, commit, upstream_commit)
        else:
            last = previous.get(name)
            if last and (last.local_commit, last.commit) == (commit, upstream_commit):
                # neither ref has moved since it was compared
                compared[name] = last
            else:
                compared[name] = UpstreamRef(
                    "differs", commit=upstream_commit, local_commit=commit
                )
    return compared


def compare_with_upstream(
    record: Repository,
    upstream: Repository,
    clone: Optional[GitRepo],
    upstream_clone: Optional[GitRepo],
    max_refs: int,
    previous: Optional[UpstreamRefs] = None,
) -> UpstreamRefs:
    """Compare ``record``'s branches and tags with the same-named ones of
    ``upstream``, leaving out those at the same commit.

    The upstream's refs come from ``upstream_clone`` if it has every commit
    ``upstream``'s record names, otherwise from the record, which lists at
    most ``max_refs`` of each: a ref missing from a list that long is unknown
    rather than missing. Its tags come from the record too when the clone is
    also ``clone``, whose tags may be the fork's. Refs whose commits the clones
    have are compared in full, the rest by commit alone, keeping a
    ``previous`` comparison of the same two commits.
    """
    clones: List[GitRepo] = []
    for c in (upstream_clone, clone):
        if c is not None and all(_git_dir(c) != _git_dir(o) for o in clones):
            clones.append(c)
    shared = (
        upstream_clone is not None
        and clone is not None
        and _git_dir(upstream_clone) == _git_dir(clone)
    )
    cloned = _clone_refs(upstream_clone, upstream.url) if upstream_clone else None
    commits = [
        *record.branches.values(),
        *record.tags.values(),
        *upstream.branches.values(),
        *upstream.tags.values(),
    ]
    if cloned:
        commits += [*cloned[0].values(), *cloned[1].values()]
    previous = previous or UpstreamRefs()
    with _reading_all(clones) as repo:
        present = _present(repo, commits)
        named = {*upstream.branches.values(), *upstream.tags.values()}
        if cloned and named <= present:
            branches, tags = cloned
            complete = [True, True]
            if shared:
                tags = upstream.tags
                complete[1] = len(tags) < max_refs
        else:
            branches, tags = upstream.branches, upstream.tags
            complete = [len(branches) < max_refs, len(tags) < max_refs]
        return UpstreamRefs(
            branches=_compare_refs(
                repo, present, record.branches, branches, complete[0], previous.branches
            ),
            tags=_compare_refs(
                repo, present, record.tags, tags, complete[1], previous.tags
            ),
        )


def _remotes_by_url(repo: git.Repo) -> Dict[str, List[git.Remote]]:
    """``repo``'s remotes, grouped by the url of the repository they name."""
    groups: Dict[str, List[git.Remote]] = {}
    for remote in repo.remotes:
        try:
            url = remote.url
        except AttributeError:  # no url, only a pushurl
            continue
        if url:
            groups.setdefault(get_repository_url(url), []).append(remote)
    return groups


def _nearest(
    repo: git.Repo, commit: str, candidates: Sequence[Tuple[str, str]]
) -> Tuple[str, UpstreamRef]:
    """Of the ``(name, upstream commit)`` ``candidates``, the one fewest
    commits from ``commit``, with how it compares."""
    compared = {up: compare_ref(repo, commit, up) for _, up in candidates}

    def distance(candidate: Tuple[str, str]) -> float:
        ref = compared[candidate[1]]
        if ref.ahead is None or ref.behind is None:  # unrelated
            return float("inf")
        return ref.ahead + ref.behind

    name, upstream_commit = min(candidates, key=distance)
    return name, compared[upstream_commit]


RecordLookup = Callable[[str], Optional[Repository]]


def _is_ancestor(repo: git.Repo, commit: str, of: str) -> bool:
    status, _, _ = repo.git.merge_base(
        "--is-ancestor", commit, of, with_extended_output=True, with_exceptions=False
    )
    return status == 0


def _newer(
    repo: git.Repo, present: Set[str], fetched: Optional[str], recorded: Optional[str]
) -> Optional[str]:
    """The later of a branch's commit as last fetched and as its repository's
    record names it: the record's if the clone doesn't have it, since then it
    was never fetched."""
    if recorded is None or recorded == fetched:
        return fetched
    if fetched is None or recorded not in present:
        return recorded
    return recorded if _is_ancestor(repo, fetched, recorded) else fetched


def remote_urls(clone: GitRepo) -> Set[str]:
    """The urls of the repositories ``clone``'s remotes name."""
    return set(_remotes_by_url(clone.repo))


def compare_with_remotes(
    clone: GitRepo,
    remote_record: Optional[RecordLookup] = None,
    max_refs: int = 0,
    siblings: Sequence[GitRepo] = (),
) -> Dict[str, UpstreamRefs]:
    """How ``clone``'s branches and tags compare with those of the
    repositories its remotes name, by their url, leaving out refs at the same
    commit: an empty entry is a repository every ref matches. A repository
    none of whose remotes has been fetched and that has no record isn't
    compared, so has no entry.

    A branch is compared with the later of its remote branch as last fetched
    and as the repository's record ``remote_record`` finds names it; tags are
    compared with the record's alone. A record lists at most ``max_refs`` of
    each (0 for no limit): a tag missing from a list that long is unknown
    rather than missing.

    Remotes naming the same repository are compared as one: a branch any of
    them has at the same commit is in step with it, otherwise it's compared
    with the remote it tracks, if that's one of them, or else with whichever
    is fewest commits from it. A branch is compared with the branch it tracks
    on its tracking remote, and the same-named branch on others; one tracking
    another repository that these don't have isn't compared with them.
    Tracking a local branch, or a remote that's gone, counts as tracking
    nothing. Commits the clone doesn't have are looked for in ``siblings``,
    other clones of the same repositories, without changing either.
    """
    with _reading_all([clone, *siblings]) as repo:
        assert repo is not None
        return _compare_with_remotes(clone, repo, remote_record, max_refs)


def _compare_with_remotes(
    clone: GitRepo,
    repo: git.Repo,
    remote_record: Optional[RecordLookup],
    max_refs: int,
) -> Dict[str, UpstreamRefs]:
    remote_names = {remote.name for remote in repo.remotes}
    local_tags = _commits_of(repo, "refs/tags/")
    compared: Dict[str, UpstreamRefs] = {}
    for url, remotes in _remotes_by_url(repo).items():
        refs = {
            remote.name: {
                ref.remote_head: ref.commit.hexsha
                for ref in remote.refs
                if ref.remote_head != "HEAD"
            }
            for remote in remotes
        }
        record = remote_record(url) if remote_record else None
        if record is None and not any(refs.values()):
            continue  # never fetched and not in the cloudmap: nothing to compare
        recorded_refs = record.branches if record is not None else {}
        present = _present(repo, [*recorded_refs.values()])
        branches: Dict[str, UpstreamRef] = {}
        for head in repo.heads:
            commit = head.commit.hexsha
            tracking = head.tracking_branch()
            if tracking is not None and tracking.remote_name not in remote_names:
                tracking = None  # tracks a local branch, or a removed remote
            tracked = tracking.remote_name if tracking is not None else None
            tracked_name = tracking.remote_head if tracking is not None else head.name
            # (remote, upstream branch name, its commit) for each remote that has it
            candidates: List[Tuple[str, str, str]] = []
            for remote in remotes:
                name = tracked_name if remote.name == tracked else head.name
                upstream_commit = refs[remote.name].get(name)
                if upstream_commit is not None:
                    candidates.append((remote.name, name, upstream_commit))
            fetched: Optional[Tuple[str, str]] = None  # (name, commit)
            if any(up == commit for *_, up in candidates):
                fetched = (head.name, commit)
            elif candidates:
                chosen = [c for c in candidates if c[0] == tracked]
                if chosen:
                    fetched = chosen[0][1:]
                else:
                    name, nearest = _nearest(
                        repo, commit, [(name, up) for _, name, up in candidates]
                    )
                    fetched = (name, nearest.commit)
            record_name = tracked_name if tracked in refs else head.name
            recorded = recorded_refs.get(record_name)
            upstream = _newer(repo, present, fetched and fetched[1], recorded)
            if upstream is None:
                if tracking is None or tracked in refs:
                    branches[head.name] = UpstreamRef("missing", local_commit=commit)
                    if tracked_name != head.name:
                        branches[head.name].ref = tracked_name
                continue
            if upstream == commit:
                continue
            name = fetched[0] if fetched and fetched[1] == upstream else record_name
            if upstream == recorded and upstream not in present:
                result = UpstreamRef("differs", commit=upstream, local_commit=commit)
            else:
                result = compare_ref(repo, commit, upstream)
            if name != head.name:
                result.ref = name
            branches[head.name] = result
        tags: Dict[str, UpstreamRef] = {}
        if record is not None and local_tags:
            complete = not max_refs or len(record.tags) < max_refs
            present = _present(repo, [*local_tags.values(), *record.tags.values()])
            tags = _compare_refs(repo, present, local_tags, record.tags, complete, {})
        compared[url] = UpstreamRefs(branches=branches, tags=tags)
    return compared


def local_repository(
    clone: GitRepo,
    repos_root: str = "",
    remote_record: Optional[RecordLookup] = None,
    max_refs: int = 0,
    siblings: Sequence[GitRepo] = (),
) -> Repository:
    """A record of the local clone ``clone``: keyed by its path, with its
    branches and tags, its comparisons with its remotes (see
    :func:`compare_with_remotes`), and ``-dirty`` added to the commit of the
    branch checked out in a worktree with uncommitted changes."""
    repo = clone.repo
    working_dir = os.path.abspath(clone.working_dir)
    root = os.path.abspath(repos_root) if repos_root else ""
    in_root = root and os.path.commonpath([root, working_dir]) == root
    record = Repository(
        url=working_dir,
        path=os.path.relpath(working_dir, root) if in_root else working_dir,
        name=os.path.basename(working_dir),
        protocols=["file"],
        branches={head.name: head.commit.hexsha for head in repo.heads},
        tags=_commits_of(repo, "refs/tags/"),
        upstream_refs=compare_with_remotes(clone, remote_record, max_refs, siblings),
    )
    if not repo.head.is_detached and clone.is_dirty():
        branch = repo.active_branch.name
        if branch in record.branches:  # not on a branch with no commits yet
            record.branches[branch] += DIRTY_SUFFIX
    return record


def save_local_clones(
    path: str,
    loaded: Iterable[GitRepo],
    repos_root: str,
    remote_record: RecordLookup,
    max_refs: int,
    logger: "UnfurlLogger",
) -> None:
    """Record in the cloudmap file at ``path`` each local clone with
    uncommitted changes or refs out of step with its remotes', removing the
    record of one that has neither any more, or that's gone. The ``loaded``
    clones are checked, and the clones the file records, wherever they are.
    The file is only written if a record changed.

    ``repos_root`` is what the loaded clones' paths are relative to;
    ``remote_record`` finds a remote repository's record in the main cloudmap,
    which lists at most ``max_refs`` of each ref."""
    db = CloudMapDB(path)
    changed = False
    # each clone to check, with the path its record has if it's one the
    # file records that this directory didn't load
    clones: Dict[str, Tuple[GitRepo, str]] = {
        os.path.abspath(clone.working_dir): (clone, "") for clone in loaded
    }
    for record in list(db.repositories.values()):
        if not record.url.startswith("git:///"):
            continue
        local_path = record.url[len("git://") :]
        if local_path in clones:
            continue
        try:
            clones[local_path] = (GitRepo(git.Repo(local_path)), record.path)
        except (git.NoSuchPathError, git.InvalidGitRepositoryError):
            logger.verbose(
                "removing local clone %s: it's gone", sanitize_url(local_path)
            )
            db.delete_record(record)
            changed = True
    # the clones of each remote repository, to read each other's commits
    urls: Dict[str, Set[str]] = {}
    by_remote: Dict[str, List[GitRepo]] = {}
    for local_path, (clone, _) in clones.items():
        try:
            urls[local_path] = remote_urls(clone)
        except Exception:
            urls[local_path] = set()  # _save_local_clone reports it
        for url in urls[local_path]:
            by_remote.setdefault(url, []).append(clone)
    for local_path, (clone, recorded_path) in clones.items():
        siblings = {
            id(other): other
            for url in urls[local_path]
            for other in by_remote[url]
            if other is not clone
        }
        changed |= _save_local_clone(
            db,
            clone,
            recorded_path or repos_root,
            bool(recorded_path),
            remote_record,
            max_refs,
            list(siblings.values()),
            logger,
        )
    if changed:
        db.save()


def _save_local_clone(
    db: CloudMapDB,
    clone: GitRepo,
    path: str,
    recorded: bool,
    remote_record: RecordLookup,
    max_refs: int,
    siblings: Sequence[GitRepo],
    logger: "UnfurlLogger",
) -> bool:
    """Record ``clone`` in ``db`` if it has uncommitted changes or refs out of
    step with its remotes', otherwise remove its record; whether ``db``
    changed. ``path`` is what its record's path is relative to, or if
    ``recorded``, the path its record has already; ``siblings`` are other
    clones of its remote repositories, whose commits it can read."""
    local_path = sanitize_url(clone.working_dir)
    try:
        record = local_repository(
            clone, "" if recorded else path, remote_record, max_refs, siblings
        )
    except Exception:
        logger.warning("Could not record local clone %s", local_path, exc_info=True)
        return False
    if recorded:
        record.path = path
    existing = db.get_repository(record.url)
    reasons = []
    if any(c.endswith(DIRTY_SUFFIX) for c in record.branches.values()):
        reasons.append("has uncommitted changes")
    differing = [
        url for url, refs in record.upstream_refs.items() if refs.branches or refs.tags
    ]
    if differing:
        remotes = ", ".join(sanitize_url(u) for u in differing)
        reasons.append(f"differs from {remotes}")
    if reasons:
        if existing is not None and existing.asdict() == record.asdict():
            logger.verbose("local clone %s is recorded already", local_path)
            return False
        logger.verbose(
            "recording local clone %s: it %s", local_path, " and ".join(reasons)
        )
        db.add_record(record)
        return True
    if existing is not None:
        logger.verbose(
            "removing local clone %s: it matches its remotes",
            local_path,
        )
        db.delete_record(existing)
        return True
    logger.verbose(
        "skipping local clone %s: it matches its remotes",
        local_path,
    )
    return False
