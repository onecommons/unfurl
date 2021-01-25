# SPDX-License-Identifier: MIT
# Copyright (c) 2020 Adam Souzis
from __future__ import absolute_import, division, print_function

__metaclass__ = type

import pbr.version


def __version__(release=False):
    # a function because this is expensive
    if release:
        return pbr.version.VersionInfo(__name__).release_string()
    else:
        return pbr.version.VersionInfo(__name__).version_string()


import os
import os.path
import sys

vendor_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "vendor")
sys.path.insert(0, vendor_dir)


class DefaultNames(object):
    SpecDirectory = "spec"
    EnsembleDirectory = "ensemble"
    Ensemble = "ensemble.yaml"
    EnsembleTemplate = "ensemble-template.yaml"
    ServiceTemplate = "service-template.yaml"
    LocalConfig = "unfurl.yaml"
    HomeDirectory = ".unfurl_home"
    JobsLog = "jobs.tsv"


def getHomeConfigPath(homepath):
    # if homepath is explicitly it overrides UNFURL_HOME
    # (set it to empty string to disable the homepath)
    # otherwise use UNFURL_HOME or the default location
    if homepath is None:
        if "UNFURL_HOME" in os.environ:
            homepath = os.getenv("UNFURL_HOME")
        else:
            homepath = os.path.join("~", DefaultNames.HomeDirectory)
    if homepath:
        homepath = os.path.expanduser(homepath)
        if not os.path.exists(homepath):
            isdir = not homepath.endswith(".yml") and not homepath.endswith(".yaml")
        else:
            isdir = os.path.isdir(homepath)
        if isdir:
            return os.path.abspath(os.path.join(homepath, DefaultNames.LocalConfig))
        else:
            return os.path.abspath(homepath)
    return None


### logging initialization
import logging

logging.captureWarnings(True)
_logHandler = None


class sensitive(object):
    redacted_str = "<<REDACTED>>"

    def __sensitive__(self):
        return True


class SensitiveLogFilter(logging.Filter):
    def filter(self, record):
        # redact any sensitive value
        record.args = tuple(
            sensitive.redacted_str if isinstance(a, sensitive) else a
            for a in record.args
        )
        return True


# def addFileLogger():


def initLogging(level=None, logfile=None):
    rootLogger = logging.getLogger()
    oldLevel = rootLogger.getEffectiveLevel()
    f = SensitiveLogFilter()

    # global _logHandler so we can call initLogging multiple times
    global _logHandler
    if not _logHandler:
        rootLogger.setLevel(logging.DEBUG)  # need to set this first
        rootLogger.addFilter(f)

        _logHandler = logging.StreamHandler()
        formatter = logging.Formatter("%(name)s:%(levelname)s: %(message)s")
        _logHandler.setFormatter(formatter)
        _logHandler.addFilter(f)
        rootLogger.addHandler(_logHandler)

    if logfile:
        ch = logging.FileHandler(logfile)
        formatter = logging.Formatter(
            "[%(asctime)s] %(name)s:%(levelname)s: %(message)s"
        )
        ch.setFormatter(formatter)
        ch.setLevel(logging.DEBUG)
        ch.addFilter(f)
        rootLogger.addHandler(ch)

    if level is not None:
        _logHandler.setLevel(level)
    return _logHandler, oldLevel


_logEnv = os.getenv("UNFURL_LOGGING")
if _logEnv is not None:
    initLogging(_logEnv.upper())

### Ansible initialization
if "ANSIBLE_CONFIG" not in os.environ:
    os.environ["ANSIBLE_CONFIG"] = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "configurators", "ansible.cfg")
    )
try:
    import ansible
except ImportError:
    pass
else:
    import ansible.constants as C

    if "ANSIBLE_NOCOWS" not in os.environ:
        C.ANSIBLE_NOCOWS = 1
    if "ANSIBLE_JINJA2_NATIVE" not in os.environ:
        C.DEFAULT_JINJA2_NATIVE = 1

    import ansible.utils.display

    ansible.utils.display.logger = logging.getLogger("unfurl.ansible")
    display = ansible.utils.display.Display()

    # Display is a singleton which we can't subclass so monkey patch instead
    _super_display = ansible.utils.display.Display.display

    def _display(self, msg, color=None, stderr=False, screen_only=False, log_only=True):
        if screen_only:
            return
        return _super_display(self, msg, color, stderr, screen_only, log_only)

    ansible.utils.display.Display.display = _display

    from ansible.plugins.loader import lookup_loader, filter_loader

    lookup_loader.add_directory(os.path.abspath(os.path.dirname(__file__)), True)
    filter_loader.add_directory(os.path.abspath(os.path.dirname(__file__)), True)
