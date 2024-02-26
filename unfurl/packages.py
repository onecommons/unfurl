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
onecommons.org/unfurl-type.git/anotherpackage/v2
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
import os.path
import re
from typing import Dict, List, NamedTuple, Optional, Tuple, Union, cast
from typing_extensions import Literal
from urllib.parse import urlparse

from .repo import (
    RepoView,
    split_git_url,
    get_remote_tags,
    sanitize_url,
    is_url_or_git_path,
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
        self.package_spec = package_spec
        if url_ish:
            self.package_id, self.url, revision = get_package_id_from_url(url_ish)
            # if url_ish was a full url, preserve the original
            if self.url and "*" not in url_ish:
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
        elif "#" in self.package_spec:
            package_id, revision = self.package_spec.split("#")
            # match exact match with package and revision
            return candidate == package_id and revision == package.revision
        else:
            return candidate == self.package_spec

    @staticmethod
    def _replace(match, replace, candidate):
        # if `candidate` startswith `match`, replace the matching segment with replace
        match = match.rstrip("*")
        if candidate.startswith(match):
            # substitute the * in replace with the remainder of 'candidate'
            return replace.replace("*", candidate[len(match) :])
        return candidate

    def replace(self, replace, package):
        return self._replace(self.package_spec, replace, package.package_id)

    def update(self, package: "Package") -> str:
        # if the package's package_id was replaced return that
        # assume package already matched
        if self.package_spec.endswith("*"):
            if self.url:
                package.url = self.replace(self.url, package)
                if self.revision:
                    package.revision = self.revision
                    # PackageSpec.url used above will always have revision stripped off
                    package.url = package.url + "#" + package.revision_tag
                return ""
            else:
                if self.package_id:
                    replaced_id = package.package_id
                    package.package_id = self.replace(self.package_id, package)
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
        if self.revision:
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
        Args:
            package_specs (PackageSpec): Rules to apply to the package.
            package (Package): Package will be updated in-place if there are rules that apply to it.

        Raises:
            UnfurlError: If applying the rules creates a circular reference.

        Returns:
            bool: True if the package was updated
        """
        old = []
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
                        raise UnfurlError(
                            f"Circular reference in package rules: {replaced_id}"
                        )
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

    # don't set url if url was just a package_id (so it didn't have a scheme)
    return Package_Url_Info(package_id, url if parts.scheme else None, revision)


def package_id_to_url(package_id: str, minimum_version: Optional[str] = ""):
    # XXX assumes .git and https
    package_id, sep, revision = package_id.partition(".git/")
    repoloc, sep, repopath = package_id.partition(".git/")
    if repopath or revision or minimum_version:
        return f"https://{repoloc}.git#{minimum_version or revision or ''}:{repopath}"
    else:
        return f"https://{repoloc}.git"


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
        self.locked = False  # current revision set from lock
        self.original_id = package_id

    @property
    def safe_url(self):
        if not self.url:
            return ""
        return sanitize_url(self.url, True)

    def __str__(self):
        return f"Package({self.package_id} {self.revision} {self.safe_url})"

    def version_tag_prefix(self) -> str:
        # see https://go.dev/ref/mod#vcs-version
        if self.url:
            url, repopath, urlrevision = split_git_url(self.url)
            # return tag prefix to match version tags with
            if repopath:
                # strip out major version suffix:
                # if repopath looks "foo" or "foo/v2", return "foo/v"
                return re.sub(r"(/v\d+)?$", "", repopath) + "/v"
        return "v"

    def find_latest_semver_from_repo(self, get_remote_tags) -> Optional[str]:
        prefix = self.version_tag_prefix()
        order = "earliest" if self.missing else "latest"
        logger.debug(
            f"Package {self.package_id} is looking for {order} remote tags {prefix}* on {self.safe_url}"
        )
        # get an sorted list of tags and strip the prefix from them
        url, repopath, urlrevision = split_git_url(self.url)
        vtags = [tag[len(prefix) :] for tag in get_remote_tags(url, prefix + "*")]
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
        return None

    def set_version_from_repo(self, get_remote_tags) -> bool:
        try:
            revision = self.find_latest_semver_from_repo(get_remote_tags)
        except Exception:
            logger.warning(
                "failed to look up version tags on remote git at %s",
                self.safe_url,
                exc_info=True,
            )
            return False
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
            # since "^v" is in the semver regex, make sure don't end up with "vv"
            return self.version_tag_prefix() + self.revision.lstrip("v")

    def is_mutable_ref(self) -> bool:
        # is this package pointing to ref that could change?
        return not self.locked
        # XXX:
        # treat tags immutable unless it looks like a non-exact semver tag:
        # return not self.revision or self.has_semver() or self.revision_is_branch()

    def add_reference(self, repoview: RepoView) -> bool:
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
        if not self.revision or not package.revision:
            # there aren't two revisions to compare so skip compatibility check
            return True
        # if either revision wasn't explicitly specified, skip compatibility check
        if self.discovered or package.discovered:
            return True
        if not self.has_semver(True):
            # require an exact match for non-semver revisions
            return self.revision == package.revision
        if not package.has_semver(True):
            return False  # the other package doesn't have a semver and doesn't match
        # # if given revision is newer than current packages we need to reload (error for now?)
        return TOSCAVersionProperty(package.revision).is_semver_compatible_with(
            TOSCAVersionProperty(self.revision)
        )


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
    lock_dict: Optional[dict] = None,
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
        if not package.revision and get_remote_tags:
            # no version specified, use the latest version tagged in the repository
            package.set_version_from_repo(get_remote_tags)
        if not changed and not package.revision:
            # don't treat repository as a package
            repoview.package = False
            packages[package.package_id] = False
            return None
        packages[package.package_id] = package
    else:
        existing = packages[package.package_id]
        if not existing:  # the repository isn't a package
            return None
        # we don't want different implementations of the same package so use the one
        # we already have. But we need to check if it compatible with the version requested here.
        if existing.repositories and not package.is_compatible_with(existing):
            # XXX if we need a later version, update the existing package and reload any content from it
            # XXX update existing.repositories and invalidate associated file_refs in the cache
            # XXX switch to raising UnfurlPackageUpdateNeeded after updating repositories and cache
            raise UnfurlError(
                f"{package.package_id} has version {package.revision} but incompatible version {existing.revision} is already in use."
            )
        package = existing

    package.add_reference(repoview)
    return package


def apply_lock(lock_dict, package: Package) -> bool:
    # note that repo_dict might refer to a different git repository than the one the current package rules use.
    # so the tag here might be missing on the new git repository -- that's ok we don't want to avoid that error
    if lock_dict.get("discovered_revision") == "(MISSING)":
        package.missing = True
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
