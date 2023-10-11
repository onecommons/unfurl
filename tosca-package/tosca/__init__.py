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

if TYPE_CHECKING:
    from .builtin_types import nodes
    from .builtin_types import interfaces
    from .builtin_types import relationships
    from .builtin_types import capabilities
    from .builtin_types import datatypes
    from .builtin_types import artifacts
    from .builtin_types import policies
    from .builtin_types import groups

_tosca_types = [
    "nodes",
    "capabilities",
    "relationships",
    "interfaces",
    "datatypes",
    "artifacts",
    "policies",
    "groups",
]

__all__ = [
    "ArtifactType",
    "Attribute",
    "B",
    "BPS",
    "Bitrate",
    "Capability",
    "CapabilityType",
    "D",
    "DataType",
    "Eval",
    "Frequency",
    "GB",
    "GBPS",
    "GHZ",
    "GHz",
    "GIB",
    "GIBPS",
    "Gbps",
    "GiB",
    "Gibps",
    "GroupType",
    "H",
    "HZ",
    "Hz",
    "InterfaceType",
    "KB",
    "KBPS",
    "KHZ",
    "KIB",
    "KIBPS",
    "Kbps",
    "KiB",
    "Kibps",
    "M",
    "MB",
    "MBPS",
    "MHZ",
    "MHz",
    "MIB",
    "MIBPS",
    "MS",
    "Mbps",
    "MiB",
    "Mibps",
    "NS",
    "Namespace",
    "NodeType",
    "PolicyType",
    "Property",
    "REQUIRED",
    "MISSING",
    "RelationshipType",
    "Requirement",
    "S",
    "Size",
    "T",
    "TB",
    "TBPS",
    "TIB",
    "TIBPS",
    "Tbps",
    "TiB",
    "Tibps",
    "Time",
    "ToscaDataType",
    "ToscaInputs",
    "ToscaOutputs",
    "US",
    "b",
    "bps",
    "d",
    "equal",
    "field",
    "gb",
    "gbps",
    "ghz",
    "gib",
    "gibps",
    "greater_or_equal",
    "greater_than",
    "h",
    "hz",
    "in_range",
    "kB",
    "kHz",
    "kb",
    "kbps",
    "khz",
    "kib",
    "kibps",
    "length",
    "less_or_equal",
    "less_than",
    "loader",
    "m",
    "max_length",
    "mb",
    "mbps",
    "metadata_to_yaml",
    "mhz",
    "mib",
    "mibps",
    "min_length",
    "ms",
    "ns",
    "operation",
    "pattern",
    "s",
    "tb",
    "tbps",
    "tib",
    "tibps",
    "tosca_timestamp",
    "tosca_version",
    "us",
    "valid_values",
]

__safe__ = _tosca_types + __all__


def __getattr__(name):
    if name in _tosca_types:
        from . import builtin_types

        return getattr(builtin_types, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
