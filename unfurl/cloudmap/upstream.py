# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""Compare a repository's branches and tags with those of the repository it
was forked or mirrored from."""

import os
import tempfile
from contextlib import contextmanager
from typing import Dict, Generator, Iterable, List, Optional, Sequence, Set, Tuple

import git

from ..repo import GitRepo
from ..tosca_plugins.cloudmap_defs import (
    Repository,
    UpstreamRef,
    UpstreamRefs,
    UpstreamRefStatus,
)

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


def _clone_refs(clone: GitRepo, url: str) -> Optional[Tuple[Refs, Refs]]:
    """The commits of the branches of ``clone``'s remote for ``url`` and of its
    tags, leaving out tags of anything else; None if it has no such remote."""
    remote = clone.find_remote(url=url)
    if remote is None:
        return None
    heads = f"refs/remotes/{remote.name}/"
    out = clone.repo.git.for_each_ref(
        "--format=%(refname) %(objecttype) %(objectname) %(*objecttype) %(*objectname)",
        heads,
        "refs/tags/",
    )
    branches: Refs = {}
    tags: Refs = {}
    for line in out.splitlines():
        refname, kind, commit, peeled_kind, peeled = (line.split(" ") + ["", ""])[:5]
        if kind == "tag":
            kind, commit = peeled_kind, peeled
        if kind != "commit":
            continue
        if refname.startswith(heads):
            name = refname[len(heads) :]
            if name != "HEAD":
                branches[name] = commit
        else:
            tags[refname[len("refs/tags/") :]] = commit
    return branches, tags


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
        elif repo is not None and commit in present and upstream_commit in present:
            compared[name] = compare_ref(repo, commit, upstream_commit)
        elif commit == upstream_commit:
            compared[name] = _identical(commit)
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
    ``upstream``.

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
