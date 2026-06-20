"""Minimal setuptools shim.

All package metadata lives in ``pyproject.toml`` and the version is
derived from git tags by ``setuptools-scm``. This file exists only to:

1. Keep the legacy ``setup.py`` commands working — notably
   ``python setup.py build_rust --debug --inplace`` (used by tox and the
   dev docs) and ``python setup.py sdist``.
2. Provide a CHANGELOG fallback version for git checkouts where no tag is
   reachable (e.g. a shallow ``git clone --depth 1``), since setuptools-scm
   otherwise guesses a bogus ``0.x`` version in that case.
3. Record the build's git short sha inside the package (``unfurl/_installed_sha.py``)
   so ``unfurl.util.get_package_digest()`` can report it for installed,
   non-git builds.
4. Materialize the ``unfurl/vendor/tosca`` symlink into a real directory for
   distribution builds (see ``_materialize_vendor_symlinks``).
"""

import os
import re
import shutil
import subprocess
import sys

from setuptools import setup

_here = os.path.dirname(os.path.abspath(__file__))


def _tag_reachable() -> bool:
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=_here,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def _changelog_version() -> str:
    try:
        with open(os.path.join(_here, "CHANGELOG.md")) as fh:
            for line in fh:
                m = re.match(r"^##\s+v(\d+\.\d+\.\d+)", line)
                if m:
                    return m.group(1) + ".dev0"
    except OSError:
        pass
    return ""


def _git_short_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "log", "-n1", "--pretty=format:%h"],
            cwd=_here,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


# Package directories that are committed as symlinks for dev convenience
# (e.g. unfurl/vendor/tosca -> ../../tosca-package/tosca). A symlinked
# directory inside the package tree breaks build_py's data-file copy
# ("can't copy ...: doesn't exist or not a regular file"), so for
# distribution builds we replace the symlink with a real copy of its target.
_VENDOR_SYMLINKS = [os.path.join("unfurl", "vendor", "tosca")]


def _restore_symlink(link: str, link_target: str) -> None:
    try:
        if os.path.isdir(link) and not os.path.islink(link):
            shutil.rmtree(link)
            os.symlink(link_target, link)
    except OSError:
        pass


def _materialize_vendor_symlinks() -> list:
    # Replace each symlink with a real copy of its target, returning the
    # (link, original_target) pairs so the caller can restore them afterward.
    restores = []
    for rel in _VENDOR_SYMLINKS:
        link = os.path.join(_here, rel)
        if not os.path.islink(link):
            continue  # already a real dir (e.g. building from an sdist)
        link_target = os.readlink(link)  # preserve original (relative) link for restore
        source = os.path.realpath(link)  # absolute resolved target dir
        if not os.path.isdir(source):
            continue  # broken symlink — leave it for setuptools to report
        os.remove(link)
        shutil.copytree(source, link, symlinks=False)
        restores.append((link, link_target))
    return restores


# Only materialize for distribution builds. Editable installs and in-place
# dev builds (`build_rust --inplace`) must keep the symlink so the working
# tree isn't clobbered. PEP 517 runs these via `setup.py <cmd>`, so argv
# carries the command name. The symlinks are restored in the finally below,
# after the build has read the real files.
_materialized = (
    _materialize_vendor_symlinks() if {"sdist", "bdist_wheel"} & set(sys.argv) else []
)

_in_git_checkout = os.path.isdir(os.path.join(_here, ".git"))

# setuptools-scm derives the version from `git describe`. In a checkout with
# no reachable tag, fall back to the latest CHANGELOG version via setuptools-scm's
# highest-precedence override (the `_FOR_UNFURL` suffix scopes it to this dist).
if _in_git_checkout and not _tag_reachable():
    fallback = _changelog_version()
    if fallback:
        os.environ.setdefault("SETUPTOOLS_SCM_PRETEND_VERSION_FOR_UNFURL", fallback)

# Record the build's git short sha inside the package so get_package_digest()
# can report it for installed wheels (where there is no .git). Written into
# unfurl/ (a package module), not metadata, so it ships as part of the package.
# When building from an sdist there is no .git, but the file is already present
# (written when the sdist was created), so don't clobber it with an empty value.
if _in_git_checkout:
    _sha = _git_short_sha()
    if _sha:
        with open(os.path.join(_here, "unfurl", "_installed_sha.py"), "w") as f:
            f.write("# Generated at build time by setup.py. Do not edit.\n")
            f.write(f'GIT_SHA = "{_sha}"\n')

try:
    setup()
finally:
    # Restore any symlinks materialized above, once the build has read the
    # real files. Matters only for non-isolated builds run directly in a
    # working tree; PEP 517 builds run in a throwaway copy.
    for _link, _target in _materialized:
        _restore_symlink(_link, _target)
