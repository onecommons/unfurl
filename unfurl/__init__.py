# SPDX-License-Identifier: MIT
# Copyright (c) 2020 Adam Souzis
import logging
import os
import sys

import pbr.version


def __version__(release=False):
    # a function because this is expensive
    if release:  # appends .devNNN
        return pbr.version.VersionInfo(__name__).release_string()
    else:  # semver only
        return pbr.version.VersionInfo(__name__).version_string()


def version_tuple(v=None):
    if v is None:
        v = __version__(True)
    return tuple(int(x.lstrip("dev") or 0) for x in v.split("."))


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
    ProjectDirectory = ".unfurl"


def get_home_config_path(homepath):
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
logging.captureWarnings(True)
_logHandler = None
_logLevel = logging.NOTSET

logging.addLevelName(5, "TRACE")
logging.TRACE = 5


def __trace(self, msg, *args, **kwargs):
    if self.isEnabledFor(logging.TRACE):
        self._log(logging.TRACE, msg, args, **kwargs)


logging.Logger.trace = __trace

logging.addLevelName(15, "VERBOSE")
logging.VERBOSE = 15


def __verbose(self, msg, *args, **kwargs):
    if self.isEnabledFor(logging.VERBOSE):
        self._log(logging.VERBOSE, msg, args, **kwargs)


logging.Logger.verbose = __verbose


class sensitive(object):
    """Base class for marking a value as sensitive. Depending on the context, sensitive values will either be encrypted or redacted when outputed."""

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


def get_log_level():
    global _logLevel
    return _logLevel


def init_logging(level=None, logfile=None):
    rootLogger = logging.getLogger()
    global _logLevel
    if _logLevel == logging.NOTSET:
        _logLevel = rootLogger.getEffectiveLevel()
    f = SensitiveLogFilter()

    # global _logHandler so we can call initLogging multiple times
    global _logHandler
    if not _logHandler:
        rootLogger.setLevel(logging.TRACE)  # need to set this first
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
        ch.setLevel(logging.TRACE)
        ch.addFilter(f)
        rootLogger.addHandler(ch)

    oldLevel = _logLevel
    if level is not None:
        _logHandler.setLevel(level)
        _logLevel = _logHandler.level
    return _logHandler, oldLevel


_logEnv = os.getenv("UNFURL_LOGGING")
if _logEnv is not None:
    init_logging(_logEnv.upper())

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

    from ansible.plugins.loader import filter_loader, lookup_loader

    lookup_loader.add_directory(os.path.abspath(os.path.dirname(__file__)), True)
    filter_loader.add_directory(os.path.abspath(os.path.dirname(__file__)), True)
