# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
"""
A repository definition can also reference a ``package`` which is a more abstract collection of artifacts or imports. The contents of a package share a semantic version and package references are resolved to a specific repository when an ensemble is loaded.
When the ``url`` field in a repository is set to an identifier that doesn't look like an absolute URL (e.g. doesn't include "://") or a relative file path (doesn't start with a ".") it is treated as a package.

Some examples of package ids:
```
unfurl.cloud/onecommons/unfurl-types
example.org
example.org/mypackage/v2
```

If the package references to a path in a git repository we follow Go's convention for including the path after ".git/" in the name. For example:

```
onecommons.org/unfurl-type.git/anotherpackage
gitlab.com/onecommons/unfurl-types.git/v2
```

Package identifiers resolve to a git repository following the algorthims for [Go modules](https://go.dev/ref/mod). Repository declarations can include required version either by including a ``revision`` field or by including it as a URL fragment in the package identifier (e.g ``#v1.1.0``).

If multiple repository declarations refer to the same package and they specify versions those versions need to be compatible. If the version look like a semantic version the semantic versioning rules for compatibility will be applied otherwise the version specifiers need to be identical.

If no revision was set, the package will retrieve the revision that matches the latest git tag that looks like a semantic version tag (see https://go.dev/ref/mod#vcs-version for the algorithm). If none is found the latest revision from the repository's default branch will be used.

If the keys in a `repositories` section look like package identifiers
that block is used as a rule override the location or version of a package
or replace the package with another package.

```
environments:
  defaults:
    repositories:
      # set the repository URL and optionally the version for the given package
      unfurl.cloud/onecommons/blueprints/wordpress:
        url: https://unfurl.cloud/user/repo.git#main # set the package to a specific repository url that also sets the branch

      # if url is set to a package identifier, replace a package with another
      unfurl.cloud/onecommons/unfurl-types:
        url: github.com/user1/myfork

      # A trailing * applies the rule to all packages that match
      unfurl.cloud/onecommons/*:
          url: https://staging.unfurl.cloud/onecommons/*

      # replace for a particular package, version combination
      unfurl.cloud/onecommons/blueprints/ghost#v1.6.0:
        url: github.com/user1/myforks.git/ghost
        revision: 1.6.1 # e.g. a security patch
```

You can also set these rules in ``UNFURL_PACKAGE_RULES`` environment variable where the key value pairs are separated by spaces. This example defines two rules:

```UNFURL_PACKAGE_RULES="unfurl.cloud/onecommons/* #main unfurl.cloud/onecommons/unfurl-types github.com/user1/myfork"```

The first rule sets the revision of matching packages to the branch "main", the second replaces one package with another package.
"""

import json
import os.path
import re
import shutil
import zlib
import zipfile
import datetime
from io import BytesIO
from typing import Dict, List, NamedTuple, Optional, Tuple, Union, cast
from git import GitCommandError
from typing_extensions import Literal, Self
from urllib.parse import urlparse
import pathspec
from .repo import (
    RepoLockDict,
    RepoView,
    Repo,
    GitRepo,
    git,
    Commit,
    split_git_url,
    get_remote_tags,
    sanitize_url,
    is_url_or_git_path,
    normalize_git_url_hard,
)
from .logs import getLogger
from .util import UnfurlError
from toscaparser.utils.validateutils import TOSCAVersionProperty
from toscaparser.imports import normalize_path

logger = getLogger("unfurl")


class UnfurlPackageUpdateNeeded(UnfurlError):
    pass


def is_semver(revision: Optional[str], include_unreleased=False) -> bool:
    """Return true if ``revision`` looks like a semver (with major version >= 1 unless include_unreleased is True)."""
    if not revision:
        return False
    revision = str(revision)
    return bool(
        (include_unreleased or not revision.lstrip("v").startswith("0"))
        and TOSCAVersionProperty.VERSION_RE.match(revision) is not None
    )


def is_semver_compatible_with(expected: str, test: str) -> bool:
    """
    Return true if ``expected`` and ``test``'s major versions are equal and ``expected``'s minor version is less than or equal to ``test``'s minor version.
    Raise an InvalidTOSCAVersionPropertyException if either ``expected`` or ``test`` doesn't match the semantic version syntax.
    """
    return TOSCAVersionProperty(expected).is_semver_compatible_with(
        TOSCAVersionProperty(test)
    )


class Package_Url_Info(NamedTuple):
    package_id: Optional[str]
    url: Optional[str]
    revision: Optional[str]


class PackageSpec:
    def __init__(
        self,
        package_spec: str,
        url_ish: Optional[str],
        minimum_version: Optional[str] = None,
    ) -> None:
        # url can be package id, a url prefix, or an url with a revision or branch
        # minimum_version if set overrides revision in url_ish
        self.package_spec = package_spec
        if url_ish:
            self.package_id, self.url, revision = get_package_id_from_url(url_ish)
            # if url_ish was a full url, preserve the original
            if self.url and "*" not in url_ish:
                if revision == "HEAD":
                    url_ish = url_ish.replace("#HEAD", "#")
                    if url_ish.endswith("#"):
                        url_ish = url_ish[:-1]
                self.url = url_ish
        else:
            self.url = None
            self.package_id = None
            revision = None
        self.revision = minimum_version or revision
        if ":" in self.package_spec or "#" in self.package_spec:
            raise UnfurlError(
                f"Malformed package spec: {self.package_spec} must be a package id not an URL"
            )
        if not (self.url or self.package_id or self.revision):
            raise UnfurlError(
                f"Malformed package spec: {self.package_spec}: missing url or package id"
            )

    @property
    def safe_url(self) -> str:
        if not self.url:
            return ""
        return sanitize_url(self.url, True)

    def as_env_value(self) -> str:
        if self.url:
            replacement = self.safe_url
            if self.revision and "#" not in self.url:
                replacement += "#" + self.revision
        elif self.package_id:
            replacement = self.package_id
        elif self.revision:
            replacement = "#" + self.revision
        else:
            replacement = ""
        return self.package_spec + " " + replacement

    def __repr__(self):
        return f"PackageSpec({self.package_spec}:{self.package_id} {self.revision} {self.safe_url})"

    def __eq__(self, other):
        if not isinstance(other, PackageSpec):
            return False
        return (
            self.package_spec == other.package_spec
            and self.package_id == other.package_id
            and self.url == other.url
            and self.revision == other.revision
        )

    def matches(self, package: "Package") -> bool:
        # * use the package name (or prefix) as the name of the repository to specify replacement or name resolution
        candidate = package.package_id
        # print("match", candidate, self.package_spec, candidate.startswith(self.package_spec.rstrip("*")))
        if self.package_spec.endswith("*"):
            return candidate.startswith(self.package_spec.rstrip("*"))
        else:
            return candidate == self.package_spec

    @staticmethod
    def _replace(match: str, replace: str, candidate: str):
        # if `candidate` startswith `match`, replace the matching segment with replace
        match = match.rstrip("*")
        if candidate.startswith(match):
            # substitute the * in replace with the remainder of 'candidate'
            return replace.replace("*", candidate[len(match) :])
        return candidate

    def replace(self, replace: str, package_id: str) -> str:
        # replaces package_id if package_spec.endswith("*")
        return self._replace(self.package_spec, replace, package_id)

    def update(self, package: "Package") -> str:
        # if the package's package_id was replaced return that
        # assume package already matched
        if self.package_spec.endswith("*"):
            if self.url:
                package.url = self.replace(self.url, package.package_id)
                if self.revision:
                    package.revision = self.revision
                    # PackageSpec.url used above will always have revision stripped off
                    package.url = package.url + "#" + package.revision_tag
                return ""
            else:
                if self.package_id:
                    replaced_id = package.package_id
                    package.package_id = self.replace(
                        self.package_id, package.package_id
                    )
                    package.url = ""
                    return replaced_id
                if self.revision:
                    # if (only) the revision was set and the package didn't set one itself, set it
                    if not package.revision:
                        package.revision = self.revision
                        if package.url and "#" not in package.url:
                            package.url += "#" + self.revision
                    return ""
                # package_specs
                raise UnfurlError(
                    f"Malformed package spec: {self.package_spec}: missing url or package id"
                )
        if self.url:
            package.url = self.url
        if self.revision == "HEAD":
            package.locked = True
            package.revision = None
        elif self.revision:
            package.revision = self.revision
            if package.url and "#" not in package.url:
                package.url += "#" + self.revision
        if self.package_id:
            replaced_id = package.package_id
            if replaced_id != self.package_id:
                package.package_id = self.package_id
                return replaced_id
        return ""

    @staticmethod
    def update_package(package_specs: List["PackageSpec"], package: "Package") -> bool:
        """
        Apply package rules to the given package.

        Args:
            package_specs (PackageSpec): Rules to apply to the package.
            package (Package): Package will be updated in-place if there are rules that apply to it.

        Returns:
            bool: True if the package was updated
        """
        old: List[str] = []
        changed = False
        replaced = True
        # if the package_id changes, start over
        while replaced:
            replaced = False
            for pkg_spec in package_specs:
                if pkg_spec.matches(package):
                    replaced_id = pkg_spec.update(package)
                    logger.trace(
                        "updated package %s using rule %s%s",
                        package,
                        pkg_spec,
                        f"(old package_id was {replaced_id})" if replaced_id else "",
                    )
                    changed = True
                    if not replaced_id:
                        # same package_id, only url or revision changed
                        # if not package.url:
                        #    # use default url pattern for the package_id
                        if not package.url:
                            package.set_url_from_package_id()
                        continue
                    if replaced_id in old:
                        # we've already replaced this package_id
                        logger.trace(
                            "circular reference in package rules: %s, %s, %s",
                            replaced_id,
                            old,
                            package_specs,
                        )
                        continue
                    replaced = True
                    old.append(replaced_id)
                    break  # package_id replaced start over
            else:
                # package_id wasn't replaced, make sure url is set
                if not package.url:
                    # use default url pattern for the package_id
                    package.set_url_from_package_id()
        return changed


def find_canonical(
    package_specs: List["PackageSpec"], canonical: str, namespace_id: str
):
    # if namespace_id is not already part of the canonical package, apply package rules that may map them to the package
    # for example, consider these package rules:
    # gitlab.com/onecommons/* staging.unfurl.cloud/onecommons/* unfurl.cloud/onecommons/* staging.unfurl.cloud/onecommons/*
    if not namespace_id.startswith(canonical):
        reverse_rules = reverse_rules_for_canonical(package_specs, canonical)
        if reverse_rules:
            package = Package(namespace_id, None, None)
            # first applies rules that might map the given namespace_id to the host
            PackageSpec.update_package(package_specs, package)
            # then apply the reverse rules to map the host to the canonical
            PackageSpec.update_package(reverse_rules, package)
            return package.package_id
    return namespace_id


def reverse_rules_for_canonical(package_specs: List["PackageSpec"], canonical: str):
    # find the package_specs that map the canonical package_id to a different host
    # the reverse them to return a list of package_specs that map a host to back to the canonical package_id
    # gitlab.com/onecommons/* staging.unfurl.cloud/onecommons/* unfurl.cloud/onecommons/* staging.unfurl.cloud/onecommons/*
    reverse_rules = [
        PackageSpec(spec.package_id, spec.package_spec)
        for spec in package_specs
        if spec.package_id
        and "*" in spec.package_id
        and spec.package_spec.rstrip("*").startswith(canonical)
    ]
    return reverse_rules


def get_package_id_from_url(url: str) -> Package_Url_Info:
    if (
        url.startswith(".")
        or url.startswith("/")
        or url.startswith("file:")
        or url.startswith("git-local")
    ):
        # this isn't a package id or a non-local git url
        return Package_Url_Info(None, url, None)

    # package_ids can have a revision in the fragment
    url, repopath, revision = split_git_url(url)
    parts = urlparse(url)
    path = parts.path
    if path:  # avoid normpath turning "" into "."
        path = os.path.normpath(parts.path).strip("/")
    if path.endswith(".git"):
        path = path[: len(path) - 4]
    if parts.hostname:
        package_id = parts.hostname + "/" + path
    else:
        package_id = path
    if repopath == ".":
        repopath = ""
    # follow Go's convention for including the path part of git url fragment in package_ids:
    if repopath:
        package_id += ".git/" + repopath
        url += "#:" + repopath

    # don't set url if url was just a package_id (so it didn't have a scheme)
    return Package_Url_Info(package_id, url if parts.scheme else None, revision)


def normalize_url(url: str) -> str:
    """Normalize url to a package_id, a git URL, or a file path, depending on the URL."""
    package_id, _, _ = get_package_id_from_url(url)
    if package_id:
        return package_id
    else:
        return normalize_git_url_hard(normalize_path(url))


def package_id_to_url(package_id: str, minimum_version: Optional[str] = ""):
    # XXX assumes host url look like https://.*.git
    # if /v? strip that
    match = re.match(r".*(/v\d+)$", package_id)
    version = ""
    if match:
        version = match.group(1)[1:]  # strip leading "/"
        package_id = package_id[: -len(match.group(1))]  # strip "/vN"

    # github.com, bitbucket.org, git.openstack.org (org/repo) git.apache.org (repo)
    repoloc, sep, repopath = package_id.partition(".git/")
    if repopath and version:
        version = repopath + "/" + version
    revision = minimum_version or version or ""
    if repopath:
        return f"https://{repoloc}.git#{revision}:{repopath}"
    else:
        parts = package_id.split("/")
        if len(parts) > 2:
            # see vcsPaths in https://go.dev/src/cmd/go/internal/vcs/vcs.go for special cases:
            # XXX "git.apache.org" (repo),
            for host in (
                "github.com",
                "bitbucket.org",
                "git.openstack.org",
            ):
                if package_id.startswith(host + "/"):
                    subpath = "/".join(parts[3:])
                    fragment = (
                        f"#{revision}:{subpath}"
                        if subpath
                        else (f"#{revision}" if revision else "")
                    )
                    return f"https://{'/'.join(parts[:3])}.git{fragment}"
        return f"https://{repoloc}.git{'#' + revision if revision else ''}"


def get_package_from_url(url_: str):
    package_id, url, revision = get_package_id_from_url(url_)
    if package_id is None:
        return None
    return Package(package_id, url, revision)


def get_url_with_latest_revision(url: str, get_remote_tags=get_remote_tags) -> str:
    pkg = get_package_from_url(url)
    if not pkg:
        return url
    pkg.set_version_from_repo(get_remote_tags)
    pkg.set_url_from_package_id()
    return pkg.url


class Package:
    def __init__(
        self, package_id: str, url: Optional[str], minimum_version: Optional[str]
    ):
        self.package_id = package_id
        if minimum_version == "HEAD":
            minimum_version = None
            self.locked = True  # use current state of package
        else:
            self.locked = False
        self.revision = minimum_version
        if url is None:
            # set self.url now because set_url_from_package_id() calls version_tag_prefix()
            self.url = ""
            self.set_url_from_package_id()
        else:
            self.url = url
        self.repositories: List[RepoView] = []
        # flags:
        self.discovered = False  # the current revision was discovered
        self.missing = False  # if set, failed to find a version tag
        self.original_id = package_id

    @property
    def safe_url(self):
        if not self.url:
            return ""
        return sanitize_url(self.url, True)

    def __str__(self):
        return f"Package({self.package_id} {self.revision} {self.safe_url})"

    def version_tag_prefix(self, major_version: str = "") -> str:
        # see https://go.dev/ref/mod#vcs-version
        if self.url:
            url, repopath, urlrevision = split_git_url(self.url)
            # return tag prefix to match version tags with
            if repopath:
                # if repopath has a major version suffix use that to only match tags with that major version
                # otherwise return "foo/v" to find version tags for this package only
                if re.match(r".*/v\d+$", repopath):
                    return repopath
                return repopath + "/v" + major_version
        return "v" + major_version

    def find_latest_semver_from_repo(
        self, get_remote_tags, major_version: str = ""
    ) -> Optional[str]:
        prefix = self.version_tag_prefix(major_version)
        order = "earliest" if self.missing else "latest"
        # get an sorted list of tags and strip the prefix from them
        url, repopath, urlrevision = split_git_url(self.url)
        remote_tags = get_remote_tags(url, prefix + "*")
        if remote_tags is None:  # didn't check
            return None
        logger.debug(
            f"Package {self.package_id} searched for {order} remote tags {prefix}* on {self.safe_url}"
        )
        vtags = [tag[len(prefix) :] for tag in remote_tags]
        # only include tags look like a semver with major version of 1 or higher
        # (We exclude unreleased versions because we want to treat the repository
        # as if it didn't specify a semver at all. Unreleased versions have no backwards compatibility
        # guarantees so we don't want to treat the repository as pinned to a particular revision.
        tags = [vtag for vtag in vtags if is_semver(vtag, True)]
        if tags:
            if self.missing:
                # if this is set then there wasn't a version tag when the lock file saved
                # so assume that the oldest tag is the best one to grab
                return tags[-1]
            else:
                # otherwise return the latest version
                return tags[0]
        return ""  # none found

    def set_version_from_repo(
        self, get_remote_tags, default_tag=None
    ) -> Optional[bool]:
        """Returns True if version found, False if checked but not found, None if not checked."""
        try:
            revision = self.find_latest_semver_from_repo(get_remote_tags)
        except GitCommandError as e:
            logger.warning(
                "failed to look up version tags on remote git at %s: %s",
                self.safe_url,
                e.stderr,
            )
            return False
        if revision is None:
            # get_remote_tags didn't check
            if default_tag:
                prefix = self.version_tag_prefix()
                if default_tag.startswith(prefix):
                    revision = default_tag[len(prefix) :]
            if revision is None:
                return None
        # remember the result of the search even if we don't set the revision
        if revision:
            self.discovered = True
            self.missing = False
            self.revision = revision
            return True
        else:
            self.missing = True
        return False

    def set_url_from_package_id(self):
        self.url = package_id_to_url(self.package_id, self.revision_tag)

    @property
    def revision_tag(self) -> str:
        if not self.revision:
            return ""
        if not self.has_semver(True):
            return self.revision
        else:
            prefix = self.version_tag_prefix()
            while prefix[-1:].isdigit():
                # remove trailing digit from prefix
                prefix = prefix[:-1]
            # since "^v" is in the semver regex, make sure don't end up with "vv"
            return self.version_tag_prefix() + self.revision.lstrip("v")

    def is_mutable_ref(self) -> bool:
        # is this package pointing to ref that could change?
        if self.locked:
            return False
        if self.discovered or not self.revision:
            return True
        return not self.has_semver(
            True
        )  # if set to an explicit version tag, assume it wont change

    def add_reference(self, repoview: RepoView) -> bool:
        """
        Add the RepoView to the package and update the RepoView if necessary.
        """
        if repoview not in self.repositories:
            self.repositories.append(repoview)
            repoview.package = self
            # we need to set the path, url, and revision to match the package
            if self.revision and is_url_or_git_path(self.url):
                url, repopath, urlrevision = split_git_url(self.url)
                repoview.path = repopath
                repoview.revision = self.revision_tag
                repoview.repository.url = f"{url}#{self.revision_tag}:{repopath}"
            else:
                repoview.repository.url = normalize_path(self.url)
            return True
        return False

    def has_semver(self, include_unreleased=False) -> bool:
        return is_semver(self.revision, include_unreleased)

    def is_compatible_with(self, package: "Package") -> bool:
        """
        If both the current package and the given package has a semantic version,
        return true if the current packages' major version is equal and minor version is less than or equal to the given package.
        If either package doesn't specify a version, return true.
        Otherwise only return true if the packages revisions match exactly.
        """
        return not bool(self.compare_versions(package))

    def compare_versions(self, package: "Package") -> Union[bool, str]:
        """
        If it returns a string, the package is compatible but with a caveat.
        """
        if not self.revision or not package.revision:
            # there aren't two revisions to compare so skip compatibility check
            return "missing"
        # if either revision wasn't explicitly specified, skip compatibility check
        if self.discovered or package.discovered:
            return "discovered"
        if not self.has_semver(True):
            # require an exact match for non-semver revisions
            return self.revision == package.revision
        if not package.has_semver(True):
            return False  # the other package doesn't have a semver and doesn't match
        # # if given revision is newer than current packages we need to reload (error for now?)
        package_version = TOSCAVersionProperty(package.revision)
        version = TOSCAVersionProperty(self.revision)
        unreleased = int(package_version.major_version) == 0
        if unreleased and int(version.major_version) == 1:
            return "unreleased"
        compat = package_version.is_semver_compatible_with(version)
        if compat and unreleased and version.minor_version != package_version.minor_version:
            return "unreleased" # more recent unreleased version, not an exact match
        return compat


PackagesType = Dict[str, Union[Literal[False], Package]]


def extract_package(repoview: RepoView):
    package_id, url, revision = get_package_id_from_url(repoview.url)
    if not package_id:
        repoview.package = False
        return None

    # if repository.revision is set it overrides the revision in the url fragment
    minimum_version = repoview.repository.revision or revision
    return Package(package_id, url or "", minimum_version)


def resolve_package(
    repoview: RepoView,
    packages: PackagesType,
    package_specs: List[PackageSpec],
    get_remote_tags=get_remote_tags,
    lock_dict: Optional["RepoLockDict"] = None,
) -> Optional["Package"]:
    """
    If repository references a package, register it with existing package or create a new one.
    A error is raised if a package's version conflicts with the repository's version requirement.
    """
    package = extract_package(repoview)
    if not package:
        return None
    # possibly change the package info if we match a PackageSpec
    changed = PackageSpec.update_package(package_specs, package)
    if lock_dict:
        apply_lock(lock_dict, package)
    if package.package_id not in packages:
        if not package.url:
            # the repository didn't specify a full url and there wasn't already an existing package or package spec
            raise UnfurlError(
                f'Could not find a repository that matched package "{package.package_id}"'
            )
        if not package.locked and not package.revision:
            skipped = True
            if get_remote_tags:
                # no version specified, use the latest version tagged in the remote repository
                # if upstream check is disabled use the local current tag
                tag = repoview.repo.current_tag if repoview.repo else None
                result = package.set_version_from_repo(get_remote_tags, tag)
                skipped = result is None  # upstream check was skipped
            if not skipped and not changed and not package.revision:
                # don't treat repository as a package
                repoview.package = False
                packages[package.package_id] = False
                return None
        packages[package.package_id] = package
    else:
        existing = packages[package.package_id]
        if not existing:  # the repository isn't a package
            return None
        # We don't want different implementations of the same package so use the one we already have.
        # But first check if it compatible with the version requested here.
        compatible = existing.compare_versions(package)
        if not compatible:
            # XXX if we need a later version, update the existing package and reload any content from it
            # XXX update existing.repositories and invalidate associated file_refs in the cache
            # XXX switch to raising UnfurlPackageUpdateNeeded after updating repositories and cache
            if package.package_id == "github.com/onecommons/unfurl":
                msg = f"Unfurl version {package.revision} was specified but the running version is {existing.revision}."
            else:
                msg = f"Package {package.package_id} has version {package.revision} but incompatible version {existing.revision} is already in use."
            raise UnfurlError(msg)
        elif compatible == "unreleased":
            if package.package_id == "github.com/onecommons/unfurl":
                msg = f"Unreleased Unfurl version {package.revision} was specified but the running version is {existing.revision}."
            else:
                msg = "Package {package.package_id} has an unreleased version {package.revision} but version {existing.revision} is already in use."
            logger.warning(msg)
        package = existing

    package.add_reference(repoview)  # updates repoview
    return package


def apply_lock(lock_dict, package: Package) -> bool:
    # note that repo_dict might refer to a different git repository than the one the current package rules use.
    # so the tag here might be missing on the new git repository -- that's ok we don't want to avoid that error
    discovered = lock_dict.get("discovered_revision")
    if discovered:
        if discovered == "(MISSING)":
            package.missing = True
    else:
        package.discovered = True
    # note that tag might not be a semver tag so missing can still be true even if there's a tag
    tag = lock_dict.get("tag")
    if tag:
        # a tag was used at lock time
        package.locked = True
        package.revision = tag
        logger.verbose(
            "setting package %s to revision %s from lock section",
            package.package_id,
            tag,
        )
        return True
    if "discovered_revision" not in lock_dict:
        # old version of lock section YAML, set missing to True
        package.missing = True
    # otherwise don't try to set the revision or mark this as locked
    # and so if the package doesn't have an explicit revision set
    # resolve_package will search for tags and reset missing and discovered
    return False


def _file_crc32(filepath: str) -> int:
    """Compute CRC-32 of a file, matching the format used by zipfile."""
    crc = 0
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def _load_gitignore_spec(root: str) -> Optional["pathspec.PathSpec"]:
    """Load all .gitignore files under *root* into a single PathSpec matcher.

    Returns None if no .gitignore files are found.
    """
    patterns: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        if ".gitignore" in filenames:
            gi_path = os.path.join(dirpath, ".gitignore")
            rel_dir = os.path.relpath(dirpath, root)
            with open(gi_path) as f:
                for line in f:
                    line = line.rstrip("\n\r")
                    if not line or line.startswith("#"):
                        continue
                    # Prefix pattern with its directory so it matches correctly.
                    if rel_dir != ".":
                        line = os.path.join(rel_dir, line)
                    patterns.append(line)
    if not patterns:
        return None
    return pathspec.PathSpec.from_lines("gitignore", patterns)


class ProxiedRepo(Repo):
    """
    A package downloaded via a Go module proxy.

    .proxied/info.json contains metadata about the package and its origin, for example:

    {"Version":"v1.0.0","Time":"2024-01-12T16:15:59Z",
    "Origin":{
      "VCS":"git",
      "URL":"https://unfurl.cloud/onecommons/blueprints/baserow.git",
      "Hash":"f3d902ae0ec52d4f869f8dbeb0c6438eeeb83030",
      "Ref":"refs/tags/v1.0.0"}
    }

    (where Hash is the commit for the Ref)
    """

    _git_repo: Optional["GitRepo"] = None
    _files: Optional[Dict[str, int]] = None

    def __init__(self, working_dir: str):
        self._working_dir = working_dir
        info_path = os.path.join(working_dir, ".proxied", "info.json")
        with open(info_path) as f:
            self._info: dict = json.load(f)
        self.url: str = self._info.get("Origin", {}).get("URL", "")

    @property
    def working_dir(self) -> str:
        return self._working_dir

    @property
    def revision(self) -> str:
        return self._info.get("Origin", {}).get("Hash", "")

    @property
    def current_tag(self) -> str:
        ref = self._info.get("Origin", {}).get("Ref", "")
        if ref.startswith("refs/tags/"):
            return ref[len("refs/tags/") :]
        return ref

    @property
    def files(self) -> Optional[Dict[str, int]]:
        if self._files is None:
            files_path = os.path.join(self._working_dir, ".proxied", "files.json")
            if not os.path.exists(files_path):
                self._files = None
            else:
                with open(files_path) as f:
                    self._files = json.load(f)
        return self._files

    @property
    def revision_time(self) -> float:
        timestamp = self._info.get("Time")
        if not timestamp:
            return 0
        return datetime.datetime.fromisoformat(timestamp).timestamp()

    def resolve_rev_spec(self, revision: str) -> Optional[str]:
        """Resolve a revision specifier (e.g. branch or tag name) to a commit hash in this repository, or return None if it can't be resolved."""
        hash = self.revision
        tag = self.current_tag
        version = self._info.get("Version", "")
        if revision in (hash, tag, version):
            return hash
        if hash.startswith(revision) and len(revision) >= 5:
            return hash
        return None

    def find_remote_url(
        self, *, url: Optional[str] = None, host: Optional[str] = None
    ) -> Optional[str]:
        """The origin url recorded for this package, if *url* or *host* names it.

        A proxied package was downloaded rather than cloned, so it has no git
        remotes to search: the single "Origin" url from ``.proxied/info.json``
        stands in for them. Matching is otherwise as `Repo.find_remote_url`
        describes, and *host* likewise wins if both arguments are given.
        """
        if url:
            url = normalize_git_url_hard(url)
        else:
            assert host, "Must specify url or host"
        origin_url = self.url
        if host:
            parsed = urlparse(origin_url)
            if parsed.hostname == host:
                return origin_url
        elif normalize_git_url_hard(origin_url) == url:
            return origin_url
        return None

    def clone(self, newPath: str) -> "Repo":
        if self._git_repo:
            return self._git_repo.clone(newPath)
        shutil.copytree(self._working_dir, newPath)
        Repo.ignore_dir(newPath)
        return ProxiedRepo(newPath)

    def is_dirty(
        self, untracked_files: bool = False, path: Optional[str] = None
    ) -> bool:
        """Check if the working directory has been modified.

        If the repo has been converted to git, delegates to GitRepo.
        Otherwise compares CRC-32 checksums against ``.proxied/files.json``.

        Args:
            untracked_files: If True, files not listed in ``files.json``
                (and not matched by any ``.gitignore``) are considered dirty.
            path: Optional absolute path to restrict the check to.

        Returns:
            True if any tracked file has been modified or deleted, or (when
            *untracked_files* is True) if untracked files exist.
        """
        if self._git_repo:
            return self._git_repo.is_dirty(untracked_files=untracked_files, path=path)

        files = self.files
        if files is None:
            return True

        if path:
            path = os.path.relpath(path, self._working_dir)
            if path.startswith(".."):
                return False # path is outside the working dir, so ignore it
            elif path == ".":
                path = None

        # Check tracked files for modifications or deletions.
        for rel, expected_crc in files.items():
            if path and not rel.startswith(path):
                continue
            abs_path = os.path.join(self._working_dir, rel)
            if not os.path.exists(abs_path):
                return True  # deleted
            crc = _file_crc32(abs_path)
            if crc != expected_crc:
                return True  # modified

        if not untracked_files:
            return False

        # Walk the tree looking for files not in files.json.
        for dirpath, dirnames, filenames in os.walk(self._working_dir):
            # Skip .proxied metadata directory.
            if ".proxied" in dirnames:
                dirnames.remove(".proxied")
            rel_dir = os.path.relpath(dirpath, self._working_dir)
            if rel_dir == ".":
                rel_dir = ""
            for fname in filenames:
                rel = os.path.join(rel_dir, fname) if rel_dir else fname
                if path and not rel.startswith(path):
                    continue
                if rel in files:
                    continue
                if self.is_path_excluded(rel):
                    continue
                return True  # untracked file found
        return False

    def is_path_excluded(self, localPath: str) -> bool:
        """Check whether *localPath* is excluded by ``.gitignore`` rules.

        Args:
            localPath: A path relative to the working directory.

        Returns:
            True if the path matches any ``.gitignore`` pattern found in the
            working tree.
        """
        spec = _load_gitignore_spec(self._working_dir)
        return spec is not None and spec.match_file(localPath)

    def _ensure_git(self) -> "GitRepo":
        """Return the underlying GitRepo, converting on first call."""
        if not self._git_repo:
            self._git_repo = self.convert_to_git()
        return self._git_repo

    def convert_to_git(self) -> "GitRepo":
        """Convert this proxied working directory into a real git repo.

        Clones origin as a bare repo into ``.git/``, flips to
        non-bare, and overwrites the working tree with HEAD (origin's
        default branch) so index and working tree both match HEAD.

        We deliberately *don't* park HEAD on the proxied snapshot's
        pinned commit, even though that's where the working tree
        files came from.  The only caller of this method
        (``LocalEnv.find_or_create_working_dir``) is converting
        precisely because it's about to switch the checkout to a
        different ref via ``repo.pull(revision=…)``.  If the pinned
        commit isn't an ancestor of the target ref (e.g. v1.1.1 was
        tagged on a release branch that diverged from main), a
        subsequent ``pull --ff-only`` would fail and HEAD would be
        stranded on a ref nobody asked for.  Landing on origin's
        default avoids that — pull can then advance to any branch
        reachable from there, and the proxied snapshot's contents
        are discarded along with ``.proxied/``.
        """
        git_dir = os.path.join(self._working_dir, ".git")
        git.Repo.clone_from(self.url, git_dir, bare=True)

        # Use raw git commands — the Repo object from clone_from still
        # sees core.bare=true, so we drive everything through git directly.
        g = git.cmd.Git(self._working_dir)  # type: ignore
        g.execute(["git", "config", "--local", "--bool", "core.bare", "false"])

        # Populate the index from HEAD (origin's default branch) and
        # overwrite the proxied snapshot in the working tree.  `-f`
        # forces overwrite of the existing files.  After this, index
        # and working tree both match HEAD → `is_dirty()` returns
        # False and `pull(revision=…)` runs cleanly.
        g.execute(["git", "checkout", "-f", "HEAD", "--", "."])

        # Clean up proxied metadata.
        proxied_dir = os.path.join(self._working_dir, ".proxied")
        if os.path.isdir(proxied_dir):
            shutil.rmtree(proxied_dir)

        return GitRepo(git.Repo(self._working_dir))

    def add_all(self, path: str) -> None:
        assert os.path.isabs(path), "Path %s must be absolute" % path
        if self.is_dirty(untracked_files=True, path=path):
            # only convert to git if there are changes to add
            self._ensure_git().add_all(path)

    def add_relative_path(self, path: str) -> None:
        # always converts to git
        self._ensure_git().add_relative_path(path)

    def commit(self, msg: str, author: Optional[str] = None) -> Optional[Commit]:
        # commit always converts to git even if there are no changes to commit
        return self._ensure_git().commit(msg, author)

    @staticmethod
    def get_proxy_url(git_url: str, proxy_url: Optional[str] = None) -> Optional[str]:
        if GOPROXY := os.getenv("GOPROXY"):
            if "direct" == GOPROXY:
                return None
            else:
                proxy_url = GOPROXY.split(",")[0]
        else:
            proxy_url = "https://proxy.golang.org/"

        package_info = get_package_id_from_url(git_url)
        if not package_info.package_id:
            return None
        return f"{proxy_url}{package_info.package_id}/"

    @classmethod
    def create_repo(
        cls, url: str, working_dir: str, revision: str
    ) -> Optional["ProxiedRepo"]:
        from .yamlloader import urlopen

        base_url = cls.get_proxy_url(url)
        if not base_url:
            return None

        # Download metadata
        info_url = f"{base_url}@v/{revision}.info"
        try:
            info_data = urlopen(info_url).read()
        except Exception:
            logger.debug("Failed to download proxy metadata from %s", info_url)
            return None

        # Download and extract zip
        zip_url = f"{base_url}@v/{revision}.zip"
        try:
            zip_data = urlopen(zip_url).read()
        except Exception:
            logger.debug("Failed to download proxy zip from %s", zip_url)
            return None

        os.makedirs(os.path.join(working_dir, ".proxied"), exist_ok=True)
        info_path = os.path.join(working_dir, ".proxied", "info.json")
        with open(info_path, "wb") as f:
            f.write(info_data)

        package_info = get_package_id_from_url(url)
        prefix = f"{package_info.package_id}@{revision}/"
        file_checksums: Dict[str, int] = {}
        with zipfile.ZipFile(BytesIO(zip_data)) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                # Strip the package_id@version/ prefix
                if member.filename.startswith(prefix):
                    rel_path = member.filename[len(prefix) :]
                else:
                    rel_path = member.filename
                if not rel_path:
                    continue
                dest = os.path.join(working_dir, rel_path)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(member) as src:
                    content = src.read()
                with open(dest, "wb") as dst:
                    dst.write(content)
                # Restore the executable bit.  Two layers:
                #
                # 1. If the zip entry carries Unix mode bits in
                #    `external_attr` (high 16), use them.  Python's
                #    `zipfile` writes 0o644 by default and ignores
                #    these bits, so we apply them ourselves.
                # 2. Go's module proxy normalises file permissions
                #    when building zips (see the module zip spec at
                #    https://pkg.go.dev/golang.org/x/mod/zip):
                #    `external_attr` is always 0 on proxy-served
                #    zips, so (1) is a no-op for the common case.
                #    Fall back to content sniffing — files that
                #    start with `#!` are scripts that need `+x`
                #    (e.g. asdf's `bin/asdf`, which gets `exec`'d
                #    out of the package install shell), and the
                #    ELF / Mach-O magic numbers catch compiled
                #    binaries.
                unix_mode = (member.external_attr >> 16) & 0o7777
                if unix_mode:
                    os.chmod(dest, unix_mode)
                elif content[:2] == b"#!" or content[:4] in (
                    b"\x7fELF",          # Linux ELF
                    b"\xfe\xed\xfa\xce",  # Mach-O 32-bit big-endian
                    b"\xce\xfa\xed\xfe",  # Mach-O 32-bit little-endian
                    b"\xfe\xed\xfa\xcf",  # Mach-O 64-bit big-endian
                    b"\xcf\xfa\xed\xfe",  # Mach-O 64-bit little-endian
                ):
                    os.chmod(dest, 0o755)
                file_checksums[rel_path] = member.CRC

        # Save CRC-32 checksums for dirty-checking without git.
        files_path = os.path.join(working_dir, ".proxied", "files.json")
        with open(files_path, "w") as f:
            json.dump(file_checksums, f, indent=2)

        Repo.ignore_dir(working_dir)
        logger.debug("Created proxied repo from %s in %s", zip_url, working_dir)
        return cls(working_dir)