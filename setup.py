"""A setuptools based setup module.
"""
from setuptools import setup

# XXX stop depending on pbr.json and delete this hack
# this fails (can't load pbr.packaging module):
# [project.entry-points."egg_info.writers"]
#     "pbr.json" = "pbr.packaging.write_pbr_json"
# so emulate:
import pbr.packaging
import os.path

egg_dir = os.path.join(os.path.dirname(__file__), "unfurl.egg-info")
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
