# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
import os.path
import sys

try:
    import toscaparser
except ImportError:
    vendor_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "vendor")
    sys.path.insert(0, vendor_dir)
    import toscaparser
from ._tosca import *
from . import loader

loader.install()

_tosca_types_str = (
    "nodes capabilities relationships interfaces datatypes artifacts policies groups"
)


def __getattr__(name):
    if name in _tosca_types_str:
        from . import builtin_types

        return getattr(builtin_types, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
