"""A setuptools based setup module.
"""
import os
import os.path
import re
import subprocess

from setuptools import setup

_here = os.path.dirname(os.path.abspath(__file__))


def _ensure_pbr_version() -> None:
    # PBR derives the version from `git describe`. On a shallow clone
    # (e.g. `git clone --depth 1` in CI) no tags are reachable and PBR
    # returns "0.0.0". If we're in a git checkout but no tag is visible,
    # fall back to the latest version listed in CHANGELOG.md and expose
    # it via PBR_VERSION (which takes precedence over git inspection).
    if os.environ.get("PBR_VERSION"):
        return
    if not os.path.isdir(os.path.join(_here, ".git")):
        return  # sdist / wheel install — pbr reads PKG-INFO instead
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=_here, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return  # tags are present, let pbr do its normal thing
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        with open(os.path.join(_here, "CHANGELOG.md"), "r") as fh:
            for line in fh:
                m = re.match(r"^##\s+v(\d+\.\d+\.\d+)", line)
                if m:
                    os.environ["PBR_VERSION"] = m.group(1) + ".dev0"
                    return
    except OSError:
        pass


_ensure_pbr_version()

# XXX stop depending on pbr.json and delete this hack
# this fails (can't load pbr.packaging module):
# [project.entry-points."egg_info.writers"]
#     "pbr.json" = "pbr.packaging.write_pbr_json"
# so emulate:
import pbr.packaging

egg_dir = os.path.join(_here, "unfurl.egg-info")
if not os.path.isdir(egg_dir):
    os.mkdir(egg_dir)

class _MockCmd:
  
  def __hasattr__(self, name):
      return True

  def __getattr__(self, name):
      return _MockCmd()
  
  def write_file(self, ignore, filename, contents):
      with open(filename, "w") as f:
        f.write(contents)

cmd = _MockCmd()
assert hasattr(cmd.distribution, 'pbr') and cmd.distribution.pbr
pbr.packaging.write_pbr_json(cmd, "pbr.json", "unfurl.egg-info/pbr.json")

setup(
    version=pbr.packaging.get_version("unfurl"),
    # this can't be set in pyproject.toml:
    # (see https://github.com/pypa/wheel/issues/582#issuecomment-1807234132)
    options={
        "bdist_wheel": {"py_limited_api": "cp38"},
     },
)
