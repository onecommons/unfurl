# Make coding more python3-ish
from __future__ import absolute_import, division, print_function

__metaclass__ = type

import pbr.version

__version__ = pbr.version.VersionInfo(__name__).version_string()

import os
import os.path
import sys

vendor_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "vendor")
sys.path.insert(0, vendor_dir)

import logging

logging.captureWarnings(True)
_logHandler = None

DefaultManifestName = "manifest.yaml"
DefaultLocalConfigName = "unfurl.yaml"
DefaultHomeDirectory = ".unfurl_home"


def getHomeConfigPath(homepath):
    # if homepath is explicitly it overrides UNFURL_HOME
    # (set it to empty string to disable the homepath)
    # otherwise use UNFURL_HOME or the default location
    if homepath is None:
        if "UNFURL_HOME" in os.environ:
            homepath = os.getenv("UNFURL_HOME")
        else:
            homepath = os.path.join("~", DefaultHomeDirectory)
    if homepath:
        homepath = os.path.expanduser(homepath)
        if not os.path.exists(homepath):
            isdir = not homepath.endswith(".yml") and not homepath.endswith(".yaml")
        else:
            isdir = os.path.isdir(homepath)
        if isdir:
            return os.path.abspath(os.path.join(homepath, DefaultLocalConfigName))
        else:
            return os.path.abspath(homepath)
    return None


def initLogging(level, logfile=None):
    rootLogger = logging.getLogger()
    rootLogger.setLevel(logging.DEBUG)  # need to set this first

    if logfile:
        ch = logging.FileHandler(logfile)
        formatter = logging.Formatter(
            "[%(asctime)s] %(name)s:%(levelname)s: %(message)s"
        )
        ch.setFormatter(formatter)
        ch.setLevel(logging.DEBUG)
        rootLogger.addHandler(ch)

    # global _logHandler so we can call initLogging multiple times
    global _logHandler
    if not _logHandler:
        _logHandler = logging.StreamHandler()
        formatter = logging.Formatter("%(name)s:%(levelname)s: %(message)s")
        _logHandler.setFormatter(formatter)
        rootLogger.addHandler(_logHandler)

    _logHandler.setLevel(level)
    return _logHandler


_logEnv = os.getenv("UNFURL_LOGGING")
if _logEnv is not None:
    initLogging(_logEnv.upper())
