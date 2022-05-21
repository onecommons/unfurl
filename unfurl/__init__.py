# SPDX-License-Identifier: MIT
# Copyright (c) 2020 Adam Souzis
import logging
import os
import sys
from typing import Dict, Union

import pbr.version

from unfurl import logs


# We need to initialize logging before any logger is created
logs.initialize_logging()


def __version__(release: bool=False) -> str:
    # a function because this is expensive
    if release:  # appends .devNNN
        return pbr.version.VersionInfo(__name__).release_string()
    else:  # semver only
        return pbr.version.VersionInfo(__name__).version_string()


def version_tuple(v: Union[None, str]=None) -> tuple:
    if v is None:
        v = __version__(True)
    return tuple(int(x.lstrip("dev") or 0) for x in v.split("."))


def is_version_unreleased(v: Union[None, str]=None) -> bool:
    return len(version_tuple(v)) > 3


vendor_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "vendor")
sys.path.insert(0, vendor_dir)


class DefaultNames:
    SpecDirectory = "spec"
    EnsembleDirectory = "ensemble"
    Ensemble = "ensemble.yaml"
    EnsembleTemplate = "ensemble-template.yaml"
    ServiceTemplate = "service-template.yaml"
    LocalConfig = "unfurl.yaml"
    SecretsConfig = "secrets.yaml"
    HomeDirectory = ".unfurl_home"
    JobsLog = "jobs.tsv"
    ProjectDirectory = ".unfurl"
    LocalConfigTemplate = ".unfurl-local-template.yaml"
    InputsTemplate = "inputs-template.yaml"

    def __init__(self, **names: Dict[str, str]) -> None:
        self.__dict__.update({name: value for name, value in names.items() if value})


def get_home_config_path(homepath: Union[None, str]) -> Union[None, str]:
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

    def _display(self: ansible.utils.display.Display.display,
        msg: str,
        color: Union[None, str]=None, 
        stderr: bool=False, 
        screen_only: bool=False, 
        log_only: bool=True
    ) -> Union[None, ansible.utils.display.Display]:
        if screen_only:
            return None
        return _super_display(self, msg, color, stderr, screen_only, log_only)

    ansible.utils.display.Display.display = _display

    from ansible.plugins.loader import filter_loader, lookup_loader

    lookup_loader.add_directory(os.path.abspath(os.path.dirname(__file__)), True)
    filter_loader.add_directory(os.path.abspath(os.path.dirname(__file__)), True)
