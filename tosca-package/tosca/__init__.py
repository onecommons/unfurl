# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
import os.path
import sys
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from .builtin_types import nodes
    from .builtin_types import interfaces
    from .builtin_types import relationships
    from .builtin_types import capabilities
    from .builtin_types import datatypes
    from .builtin_types import artifacts
    from .builtin_types import policies
    from .builtin_types import groups


def __getattr__(name):
    if name in _tosca_types_str:
        from . import builtin_types

        return getattr(builtin_types, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
