# Copyright (c) 2023 Adam Souzis
# SPDX-License-Identifier: MIT
import os.path
import sys
import datetime
import re
from types import ModuleType
from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
    Iterable,
    Tuple,
    TypedDict,
    TypeVar,
    Union,
    Callable,
    overload,
    Dict,
)
from typing_extensions import Unpack, NotRequired
from enum import Enum

try:
    import toscaparser
except ImportError:
    vendor_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "vendor")
    sys.path.insert(0, vendor_dir)
    import toscaparser
from ._tosca import *
from ._fields import *

InputOption = PropertyOptions({ToscaInputs._metadata_key: True})
OutputOption = AttributeOptions({ToscaOutputs._metadata_key: True})

from .builtin_types import nodes
from .builtin_types import interfaces
from .builtin_types import relationships
from .builtin_types import capabilities
from .builtin_types import datatypes
from .builtin_types import artifacts
from .builtin_types import policies
from .builtin_types import groups

# mypy: enable-incomplete-feature=Unpack

_PT = TypeVar("_PT", bound="ToscaType")


@overload
def find_node(name: str, node_type: None = None) -> nodes.Root: ...


@overload
def find_node(name: str, node_type: Type[_PT]) -> _PT: ...


def find_node(name, node_type=None):
    "Use this to refer to TOSCA node templates that are not visible to your Python code."
    if node_type is None:
        node_type = nodes.Root
    return node_type(name)


@overload
def find_relationship(
    name: str, relationship_type: None = None
) -> relationships.Root: ...


@overload
def find_relationship(name: str, relationship_type: Type[_PT]) -> _PT: ...


def find_relationship(name, relationship_type=None):
    "Use this to refer to TOSCA relationship templates are not visible to your Python code"
    if relationship_type is None:
        relationship_type = relationships.Root
    return relationship_type(name, _metadata=dict(__templateref=True))


class StandardOperationsKeywords(TypedDict):
    """The names of tosca and unfurl's built-in operations.
    `set_operations` uses this to provide type hint for its keyword arguments, ignore this when setting custom operations.
    """

    # tosca.interfaces.node.lifecycle.Standard
    create: NotRequired[Union[Callable, ArtifactEntity]]
    configure: NotRequired[Union[Callable, ArtifactEntity]]
    start: NotRequired[Union[Callable, ArtifactEntity]]
    stop: NotRequired[Union[Callable, ArtifactEntity]]
    delete: NotRequired[Union[Callable, ArtifactEntity]]
    # tosca.interfaces.relationship.Configure:
    pre_configure_source: NotRequired[Union[Callable, ArtifactEntity]]
    pre_configure_target: NotRequired[Union[Callable, ArtifactEntity]]
    post_configure_source: NotRequired[Union[Callable, ArtifactEntity]]
    post_configure_target: NotRequired[Union[Callable, ArtifactEntity]]
    add_target: NotRequired[Union[Callable, ArtifactEntity]]
    remove_target: NotRequired[Union[Callable, ArtifactEntity]]
    add_source: NotRequired[Union[Callable, ArtifactEntity]]
    remove_source: NotRequired[Union[Callable, ArtifactEntity]]
    target_changed: NotRequired[Union[Callable, ArtifactEntity]]
    # unfurl.interfaces.Install
    check: NotRequired[Union[Callable, ArtifactEntity]]
    discover: NotRequired[Union[Callable, ArtifactEntity]]
    connect: NotRequired[Union[Callable, ArtifactEntity]]
    restart: NotRequired[Union[Callable, ArtifactEntity]]
    revert: NotRequired[Union[Callable, ArtifactEntity]]


@overload
def set_operations(
    obj: None = None, **kw: Unpack[StandardOperationsKeywords]
) -> nodes.Root: ...


@overload
def set_operations(obj: _PT, **kwargs: Unpack[StandardOperationsKeywords]) -> _PT: ...


def set_operations(obj=None, **kwargs: Unpack[StandardOperationsKeywords]):
    """
    Helper method to set operations on a TOSCA template using its keyword arguments' as the operation names.

    Args:
      obj (Optional[nodes.Root]): An optional object to configure. If not provided, a new ``tosca.nodes.Root`` object is created.
      **kwargs (Unpack[StandardOperationsKeywords]): A mapping of operation names to a callable or
        `ArtifactEntity` instance. If an `ArtifactEntity` is provided, it is converted into an operation via :py:func:`tosca.operation`.

    Returns:
      ToscaType: The given or created template with the specified operations set.
    """
    if obj is None:
        obj = nodes.Root()
    for k, v in kwargs.items():
        if isinstance(v, ArtifactEntity):
            v = operation(v, name=k)
        obj.set_operation(cast(Callable, v), k)
    return obj


class Repository(ToscaObject):
    """
    Create a TOSCA  `Repository <tosca_repositories>` object. Repositories can represent, for example,
    a git repository, a container image or artifact registry or a file path to a local directory.

    Repositories need to be declared before any import statements that refer to them
    so an orchestrator like Unfurl can map them to ``tosca_repositories`` imports.

    Args:
      name (str]): The name of the repository.
      url (str): The url of the repository.
      revision (Optional[str]): The revision of the repository to use (e.g. a semantic version or git tag or branch). Defaults to None.
      description (Optional[str]): A human-readable description of the repository. Defaults to None.
      credential (Optional[tosca.datatypes.Credential]): The credential to use when accessing the repository. Defaults to None.
      metadata (Optional[Dict[str, Any]]): Additional metadata about the repository. Defaults to None.

    Example:

    .. code-block:: python

        std = tosca.Repository("std", "https://unfurl.cloud/onecommons/std",
                    credential=tosca.datatypes.Credential(user="a_user", token=expr.get_env("MY_TOKEN")))
        from tosca_repositories.std import Service
    """

    _template_section = "repositories"

    def __init__(
        self,
        name: str,
        url: Optional[str] = None,
        *,
        revision: Optional[str] = None,
        credential: Optional[datatypes.Credential] = None,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        if url is None:
            # support old signature: if only one positional argument assume it was an url
            url = name
            self._tosca_name = ""
        else:
            self._tosca_name = name or ""
        self._name = ""  # set by python converter from its python name
        self._tpl: Dict[str, Any] = dict(url=url)
        if revision is not None:
            self._tpl["revision"] = revision
        if description is not None:
            self._tpl["description"] = description
        if metadata is not None:
            self._tpl["metadata"] = metadata
        self.credential = credential
        from .loader import import_resolver

        if import_resolver and self._tosca_name:
            import_resolver.add_repository(self._tosca_name, self._tpl)

    def to_yaml(self, dict_cls=dict) -> Optional[Dict]:
        tpl = dict_cls(**self._tpl)
        if self.credential is not None:
            tpl["credential"] = self.credential.to_yaml(dict_cls)
        name = self._tosca_name or self._name
        assert name
        return dict_cls({name: tpl})


class WritePolicy(Enum):
    older = "older"
    never = "never"
    always = "always"
    auto = "auto"

    def deny_message(self, unchanged=False) -> str:
        if unchanged:
            return (
                f'overwrite policy is "{self.name}" but the contents have not changed'
            )
        if self == WritePolicy.auto:
            return 'overwrite policy is "auto" and the file was last modified by another process'
        if self == WritePolicy.never:
            return 'overwrite policy is "never" and the file already exists'
        elif self == WritePolicy.older:
            return (
                'overwrite policy is "older" and the file is newer than the source file'
            )
        else:
            return ""

    def generate_comment(self, processor: str, path: str) -> str:
        ts_stamp = datetime.datetime.now().isoformat("T", "seconds")
        return f'# Generated by {processor} from {os.path.relpath(path)} at {ts_stamp} overwrite not modified (change to "overwrite ok" to allow)\n'

    @staticmethod
    def is_auto_generated(contents: str) -> bool:
        return contents.startswith("# Generated by ")

    def can_overwrite(self, input_path: str, output_path: str) -> bool:
        return self.can_overwrite_compare(input_path, output_path)[0]

    def can_overwrite_compare(
        self, input_path: str, output_path: str, new_src: Optional[str] = None
    ) -> Tuple[bool, bool]:
        if self == WritePolicy.always:
            if new_src and os.path.exists(output_path):
                with open(output_path) as out:
                    contents = out.read()
                return True, self.has_contents_unchanged(new_src, contents)
            return True, False
        if self == WritePolicy.never:
            return not os.path.exists(output_path), False
        elif self == WritePolicy.older:
            # only overwrite if the output file is older than the input file
            return not is_newer_than(output_path, input_path), False
        else:  # auto
            # if this file is autogenerated, parse out the modified time and make sure it matches
            if not os.path.exists(output_path):
                return True, False
            with open(output_path) as out:
                contents = out.read()
            if self.ok_to_modify_auto_generated(contents, output_path):
                return True, self.has_contents_unchanged(new_src, contents)
            return False, False

    @staticmethod
    def ok_to_modify_auto_generated(contents: str, output_path: str) -> bool:
        match = re.search(r"# Generated by .+? at (\S+) overwrite (ok)?", contents)
        if not match:  # not autogenerated
            return False
        if match.group(2):  # found "ok" to modify
            return True
        time = datetime.datetime.fromisoformat(match.group(1)).timestamp()
        if abs(time - os.stat(output_path).st_mtime) < 5:
            return True  # not modified after embedded timestamp
        # the file was modified at different time than when it was generated, so don't overwrite
        return False

    def has_contents_unchanged(self, new_src: Optional[str], old_src: str) -> bool:
        if new_src is None:
            return False
        new_lines = [
            l.strip()
            for l in new_src.splitlines()
            if l.strip() and not l.startswith("#")
        ]
        old_lines = [
            l.strip()
            for l in old_src.splitlines()
            if l.strip() and not l.startswith("#")
        ]
        if len(new_lines) == len(old_lines):
            return new_lines == old_lines
        return False


def is_newer_than(output_path, input_path):
    "Is output_path newer than input_path?"
    if not os.path.exists(input_path) or not os.path.exists(output_path):
        return True  # assume that if it doesn't exist yet its definitely newer
    if os.stat(output_path).st_mtime_ns > os.stat(input_path).st_mtime_ns:
        return True
    return False


def patch_template(
    module: ModuleType, template_name: str, patch: "ToscaType"
) -> Optional["ToscaType"]:
    """Apply the give patch object to the template found in the given module.

    Must be called before yaml generation, so care must be taken to make sure the module that calls this method is imported before a TOSCA import or include of the patched template.
    """
    from . import loader

    if loader.import_resolver:
        return loader.import_resolver.patch_template(module, template_name, patch)
    return None


__all__ = [
    "EvalData",
    "safe_mode",
    "global_state_mode",
    "global_state_context",
    "nodes",
    "capabilities",
    "relationships",
    "interfaces",
    "datatypes",
    "artifacts",
    "policies",
    "groups",
    "Artifact",
    "ArtifactEntity",
    "ArtifactType",
    "Attribute",
    "B",
    "BPS",
    "Bitrate",
    "Capability",
    "CapabilityEntity",
    "CapabilityType",
    "D",
    "DataEntity",
    "DataType",
    "DeploymentBlueprint",
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
    "Group",
    "GroupType",
    "H",
    "HZ",
    "Hz",
    "InputOption",
    "Interface",
    "InterfaceType",
    "JsonType",
    "JsonObject",
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
    "AttributeOptions",
    "PATCH",
    "patch_template",
    "PropertyOptions",
    "Namespace",
    "NodeTemplateDirective",
    "Node",
    "NodeType",
    "OpenDataEntity",
    "Options",
    "OutputOption",
    "Policy",
    "PolicyType",
    "Property",
    "REQUIRED",
    "MISSING",
    "DEFAULT",
    "CONSTRAINED",
    "Relationship",
    "RelationshipType",
    "Repository",
    "Requirement",
    "S",
    "select_node",
    "ServiceTemplate",
    "Size",
    "StandardOperationsKeywords",
    "substitute_node",
    "T",
    "TagWriter",
    "TB",
    "TBPS",
    "TIB",
    "TIBPS",
    "Tbps",
    "TiB",
    "Tibps",
    "Time",
    "TopologyInputs",
    "TopologyOutputs",
    "ToscaParserDataType",
    "ToscaType",
    "ToscaInputs",
    "ToscaOutputs",
    "US",
    "ValueType",
    "b",
    "bps",
    "Computed",
    "d",
    "equal",
    "field",
    "find_all_required_by",
    "find_configured_by",
    "find_hosted_on",
    "find_required_by",
    "find_relationship",
    "find_node",
    "from_owner",
    "gb",
    "gbps",
    "ghz",
    "gib",
    "gibps",
    "greater_or_equal",
    "greater_than",
    "h",
    "has_function",
    "hz",
    "in_range",
    "jinja_template",
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
    "m",
    "set_operations",
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
    "placeholder",
    "reset_safe_mode",
    "s",
    "scalar",
    "scalar_value",
    "tb",
    "tbps",
    "tib",
    "tibps",
    "tosca_timestamp",
    "tosca_version",
    "us",
    "unit",
    "valid_values",
]

__safe__ = __all__
