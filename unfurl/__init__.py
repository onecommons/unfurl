"""
The basic operation of Unfurl is to apply the specified configuration to a resource
and record the results.

A manifest can contain a reproducible history of changes to a resource.
This history is stored in the resource definition so it doesn't
rely on git history for this. In fact a git repository isn't required at all.
But the intent is for commits in a git repo to correspond to reproducible configuration state of the system.
The git repo is also used to record or archive exact versions of each configurators applied.
"""

import pbr.version
__version__ = pbr.version.VersionInfo(__name__).version_string()

import os
import sys
vendor_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'vendor')
sys.path.insert(0, vendor_dir)

import logging
logging.captureWarnings(True)
_logHandler = None
def initLogging(level, logfile=None):
  rootLogger = logging.getLogger()
  rootLogger.setLevel(logging.DEBUG) # need to set this first

  if logfile:
    ch = logging.FileHandler(logfile)
    formatter = logging.Formatter(
        '[%(asctime)s] %(name)s:%(levelname)s: %(message)s')
    ch.setFormatter(formatter)
    ch.setLevel(logging.DEBUG)
    rootLogger.addHandler(ch)

  # global _logHandler so we can call initLogging multiple times
  global _logHandler
  if not _logHandler:
    _logHandler = logging.StreamHandler()
    formatter = logging.Formatter('%(name)s:%(levelname)s: %(message)s')
    _logHandler.setFormatter(formatter)
    rootLogger.addHandler(_logHandler)

  _logHandler.setLevel(level)
  return _logHandler

_logEnv = os.getenv('UNFURL_LOGGING')
if _logEnv is not None:
  initLogging(_logEnv)
