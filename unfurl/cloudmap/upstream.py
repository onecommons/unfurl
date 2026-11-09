# Copyright (c) 2026 Adam Souzis
# SPDX-License-Identifier: MIT
"""Compare a repository's branches and tags with an upstream repository's using a local clone."""

import hashlib
from typing import Dict, Optional, Tuple

import git

from ..tosca_plugins.cloudmap_defs import (
    Repository,
    UpstreamRef,
    UpstreamRefs,
    UpstreamRefStatus,
)

UPSTREAM_REFS_PREFIX = "refs/unfurl-upstream"


def upstream_fetch_url(upstream_url: str, upstream: Optional[Repository]) -> str:
    """A URL to fetch the upstream repository with."""
    if upstream:
        url = upstream.git_url(("https", "http", "ssh"))
        if url:
            return url
    if upstream_url.startswith("git://"):
        return "https://" + upstream_url[len("git://") :]
    return upstream_url


def fetch_upstream(repo: git.Repo, upstream_url: str, fetch_url: str) -> str:
    """Fetch the upstream's branches and tags into a private ref namespace, returning it."""
    namespace = (
        f"{UPSTREAM_REFS_PREFIX}/{hashlib.sha1(upstream_url.encode()).hexdigest()[:12]}"
    )
    repo.git.fetch(
        "--no-tags",
        "--prune",
        "--quiet",
        fetch_url,
        f"+refs/heads/*:{namespace}/heads/*",
        f"+refs/tags/*:{namespace}/tags/*",
        env={"GIT_TERMINAL_PROMPT": "0"},
    )
    return namespace


def _upstream_commits(repo: git.Repo, namespace: str, kind: str) -> Dict[str, str]:
    """Upstream ref names to commits, peeling annotated tags."""
    prefix = f"{namespace}/{kind}/"
    out = repo.git.for_each_ref(
        "--format=%(refname) %(objectname) %(*objectname)", prefix
    )
    commits: Dict[str, str] = {}
    for line in out.splitlines():
        refname, objectname, peeled = (line.split(" ") + [""])[:3]
        commits[refname[len(prefix) :]] = peeled or objectname
    return commits


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
    """How ``commit`` compares with ``upstream_commit``."""
    if commit == upstream_commit:
        return UpstreamRef(
            "identical", commit=upstream_commit, merge_base=commit, ahead=0, behind=0
        )
    merge_base = _merge_base(repo, commit, upstream_commit)
    if merge_base is None:
        return UpstreamRef("unrelated", commit=upstream_commit)
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
        merge_base=merge_base,
        ahead=ahead,
        behind=behind,
    )


def _compare_refs(
    repo: git.Repo,
    refs: Dict[str, str],
    upstream: Dict[str, str],
    previous: Dict[str, UpstreamRef],
) -> Dict[str, UpstreamRef]:
    compared: Dict[str, UpstreamRef] = {}
    for name, commit in refs.items():
        upstream_commit = upstream.get(name)
        if upstream_commit is None:
            compared[name] = UpstreamRef("missing")
            continue
        try:
            compared[name] = compare_ref(repo, commit, upstream_commit)
        except git.GitCommandError:
            # the clone doesn't have the commit the record names
            if name in previous:
                compared[name] = previous[name]
    return compared


def compare_with_upstream(
    repo: git.Repo,
    record: Repository,
    upstream_url: str,
    fetch_url: str,
    previous: Optional[UpstreamRefs] = None,
) -> UpstreamRefs:
    """Compare ``record``'s branches and tags with the same-named refs of the
    repository at ``upstream_url``, whose history is fetched into ``repo``.

    A ref whose commit ``repo`` doesn't have keeps its ``previous`` comparison.
    """
    namespace = fetch_upstream(repo, upstream_url, fetch_url)
    previous = previous or UpstreamRefs()
    return UpstreamRefs(
        branches=_compare_refs(
            repo,
            record.branches,
            _upstream_commits(repo, namespace, "heads"),
            previous.branches,
        ),
        tags=_compare_refs(
            repo,
            record.tags,
            _upstream_commits(repo, namespace, "tags"),
            previous.tags,
        ),
    )
