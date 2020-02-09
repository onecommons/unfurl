# Make coding more python3-ish
from __future__ import absolute_import, division, print_function

__metaclass__ = type

import pbr.version

__version__ = pbr.version.VersionInfo(__name__).version_string()

import os
import sys

vendor_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "vendor")
sys.path.insert(0, vendor_dir)

import logging

logging.captureWarnings(True)
_logHandler = None


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
