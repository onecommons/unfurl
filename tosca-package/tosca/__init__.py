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
    "B_Scalar",
    "Bitrate",
    "Capability",
    "CapabilityType",
    "D",
    "DataType",
    "Eval",
    "Frequency",
    "GB",
    "GBPS",
    "GB_Scalar",
    "GHZ",
    "GHz",
    "GHz_Scalar",
    "GIB",
    "GIBPS",
    "Gbps",
    "Gbps_Scalar",
    "GiB",
    "GiB_Scalar",
    "Gibps",
    "Gibps_Scalar",
    "GroupType",
    "H",
    "HZ",
    "Hz",
    "Hz_Scalar",
    "InterfaceType",
    "KB",
    "KBPS",
    "KHZ",
    "KIB",
    "KIBPS",
    "Kbps",
    "Kbps_Scalar",
    "KiB",
    "KiB_Scalar",
    "Kibps",
    "Kibps_Scalar",
    "M",
    "MB",
    "MBPS",
    "MB_Scalar",
    "MHZ",
    "MHz",
    "MHz_Scalar",
    "MIB",
    "MIBPS",
    "MS",
    "Mapping",
    "Mbps",
    "Mbps_Scalar",
    "MiB",
    "MiB_Scalar",
    "Mibps",
    "Mibps_Scalar",
    "NS",
    "Namespace",
    "NodeType",
    "PolicyType",
    "Property",
    "REQUIRED",
    "RelationshipType",
    "Requirement",
    "S",
    "Size",
    "T",
    "TB",
    "TBPS",
    "TB_Scalar",
    "TIB",
    "TIBPS",
    "Tbps",
    "Tbps_Scalar",
    "TiB",
    "TiB_Scalar",
    "Tibps",
    "Tibps_Scalar",
    "Time",
    "ToscaDataType",
    "US",
    "b",
    "bps",
    "bps_Scalar",
    "d",
    "d_Scalar",
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
    "h_Scalar",
    "hz",
    "in_range",
    "kB",
    "kB_Scalar",
    "kHz",
    "kHz_Scalar",
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
    "m_Scalar",
    "max_length",
    "mb",
    "mbps",
    "metadata_to_yaml",
    "mhz",
    "mib",
    "mibps",
    "min_length",
    "ms",
    "ms_Scalar",
    "ns",
    "ns_Scalar",
    "operation",
    "s",
    "s_Scalar",
    "tb",
    "tbps",
    "tib",
    "tibps",
    "tosca_timestamp",
    "tosca_version",
    "us",
    "us_Scalar",
    "valid_values",
]

__safe__ = _tosca_types + __all__


def __getattr__(name):
    if name in _tosca_types:
        from . import builtin_types

        return getattr(builtin_types, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
